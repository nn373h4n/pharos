"""PreToolUse guard for Bash commands (Claude Code hook).

Reads the hook JSON on stdin and exits 2 (block, reason on stderr) when the
command matches a forbidden form. Deny rules in settings.json only match
literal prefixes, so this tokenizes the command and also catches reordered
flags, absolute paths, wrappers, shell keywords, shell -c strings, command
and process substitution. Here-doc bodies are data and are not parsed as
commands unless they feed an interpreter. Any internal error blocks (fail closed).

Known limits (this is a guard rail, not a sandbox):
- Globs, braces and variables: cat .e*, x=.env; cat $x, $(echo rm).
- Script files and interpreter code other than the -c/-e forms checked here.
- Download then run in two steps: curl -o f URL && sh f.
- Environment dumps: env, printenv, /proc/self/environ, docker compose exec api env.
- Other privilege wrappers (su -c) and git clean -fdx / git rm -rf . (git clean is in the ask list).
- Relative secrets paths are resolved against the hook's working directory.
"""

import json
import os
import re
import shlex
import sys

SEPARATORS = {";", ";;", ";&", "&&", "||", "|", "&", "|&", "(", ")"}
PIPES = {"|", "|&"}
LEADING_KEYWORDS = {"if", "then", "else", "elif", "do", "while", "until", "{", "}", "!", "time"}
SKIPPED_COMMANDS = {"for", "case", "select", "function", "fi", "done", "esac"}
WRAPPERS = {
    "env": {"-u", "-C", "-S", "--unset", "--chdir", "--split-string"},
    "nice": {"-n", "--adjustment"},
    "nohup": set(),
    "command": set(),
    "builtin": set(),
    "exec": {"-a"},
    "xargs": {"-n", "-a", "-I", "-L", "-P", "-s", "-d", "-E", "--max-args", "--arg-file",
              "--max-procs", "--max-lines", "--delimiter", "--replace", "--eof", "--max-chars"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"},
    "stdbuf": {"-i", "-o", "-e"},
    "ionice": {"-c", "-n", "-p", "--class", "--classdata"},
    "setsid": set(),
    "watch": {"-n", "-d", "--interval"},
}
UV_VALUE_FLAGS = {"--with", "--python", "-p", "--directory", "--project", "--package", "--extra", "--group"}
SHELLS = {"sh", "bash", "zsh", "dash", "ash"}
SCRIPT_RUNNERS = {"perl", "ruby", "node", "php", "source", "."}
PYTHON = re.compile(r"^python(\d+(\.\d+)*)?$")
DOWNLOADERS = {"curl", "wget"}
DOWNLOADER_RE = re.compile(r"\b(curl|wget)\b")
SUBST = "__SUBST__"
SUBST_DL = "__SUBST_DL__"
ENV_FILE = re.compile(r"^\.env(\..+)?$")
ENV_SAFE = {".env.example", ".env.sample", ".env.template"}
ENV_SAFE_COMMANDS = {"ls", "stat", "test", "[", "rm", "touch", "mkdir"}
DEST_LAST = {"cp", "mv", "install", "ln"}
PATTERN_FIRST = {"grep", "egrep", "fgrep", "rg", "ag", "sed", "awk", "gawk"}
CODE_FORBIDDEN = re.compile(
    r"rmtree|shutil\.|os\.(system|popen|remove|unlink|rmdir|removedirs)|subprocess|unlink|child_process"
    r"|fs\.|system\(|\bexec\b|\.env\b|secrets/"
)
BROAD_TARGETS = {"/", "~", "$HOME", "*", ".", "..", "./", "../"}
HEREDOC = re.compile(r"(?<!<)<<(?!<)-?\s*(?:'([^']+)'|\"([^\"]+)\"|\\?(\w+))")
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
MAX_DEPTH = 4


class Blocked(Exception):
    pass


def base(word: str) -> str:
    return os.path.basename(word.lstrip("\\"))


def project_dir() -> str:
    return os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()


def is_interpreter(name: str) -> bool:
    return name in SHELLS or name in SCRIPT_RUNNERS or bool(PYTHON.match(name))


def strip_heredocs(command: str, depth: int) -> str:
    """Remove here-doc bodies. A body that feeds an interpreter is checked."""
    lines = command.split("\n")
    out, i = [], 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        for match in HEREDOC.finditer(line):
            delimiter = match.group(1) or match.group(2) or match.group(3)
            start, body = i, []
            while i < len(lines) and lines[i].strip() != delimiter:
                body.append(lines[i])
                i += 1
            if i >= len(lines):
                # No terminator: this fragment was cut out of a larger command (for example the
                # inside of a nested $(...)). Keep the lines so quoting stays balanced.
                i = start
                continue
            i += 1  # terminator
            head = line[: match.start()]
            target = re.search(r"\b(bash|sh|zsh|dash|ash|python[\d.]*|perl|ruby|node|php)\b\s*[^|;&]*$", head)
            if target:
                text = "\n".join(body)
                if target.group(1) in SHELLS:
                    check_string(text, depth + 1)
                elif CODE_FORBIDDEN.search(text):
                    raise Blocked("here-doc code with file deletion, process execution or secrets access")
    return "\n".join(out)


def matching_paren(text: str, start: int) -> int:
    level = 1
    for index in range(start, len(text)):
        if text[index] == "(":
            level += 1
        elif text[index] == ")":
            level -= 1
            if level == 0:
                return index
    return len(text)


def sanitize(command: str, depth: int) -> str:
    """Check $(...), backticks and <(...) bodies; replace them with placeholders."""
    out, i, n = [], 0, len(command)
    in_single = in_double = False
    while i < n:
        char = command[i]
        if in_single:
            out.append(char)
            in_single = char != "'"
            i += 1
            continue
        if char == "\\" and i + 1 < n:
            out.append(command[i : i + 2])
            i += 2
            continue
        if char == "'" and not in_double:
            in_single = True
        elif char == '"':
            in_double = not in_double
        elif command[i : i + 2] == "$(" or (command[i : i + 2] in ("<(", ">(") and not in_double):
            end = matching_paren(command, i + 2)
            body = command[i + 2 : end]
            check_string(body, depth + 1)
            out.append(SUBST_DL if DOWNLOADER_RE.search(body) else SUBST)
            i = end + 1
            continue
        elif char == "`":
            end = command.find("`", i + 1)
            end = len(command) if end == -1 else end
            body = command[i + 1 : end]
            check_string(body, depth + 1)
            out.append(SUBST_DL if DOWNLOADER_RE.search(body) else SUBST)
            i = end + 1
            continue
        out.append(char)
        i += 1
    return "".join(out)


def tokenize(command: str) -> list[str]:
    lexer = shlex.shlex(command.replace("\n", " ; "), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    return list(lexer)


def split_commands(tokens: list[str]) -> list[tuple[list[str], bool]]:
    """Split on separators; each command carries whether a pipe feeds it."""
    commands, current, piped = [], [], False
    for token in tokens:
        if token in SEPARATORS:
            if current:
                commands.append((current, piped))
            current = []
            piped = token in PIPES
        else:
            current.append(token)
    if current:
        commands.append((current, piped))
    return commands


def strip_prefix(words: list[str], depth: int) -> list[str]:
    """Drop assignments, shell keywords and wrapper commands in front of the real command."""
    while words:
        head = base(words[0])
        if head in LEADING_KEYWORDS or ASSIGNMENT.match(words[0]):
            words = words[1:]
        elif head in WRAPPERS:
            takes_value = WRAPPERS[head]
            rest = words[1:]
            while rest and rest[0].startswith("-") and rest[0] != "-":
                flag = rest[0]
                if head == "env" and flag in ("-S", "--split-string") and len(rest) > 1:
                    check_string(rest[1], depth + 1)
                if flag.startswith("--split-string="):
                    check_string(flag.split("=", 1)[1], depth + 1)
                rest = rest[2:] if flag in takes_value else rest[1:]
            if head == "timeout" and rest:
                rest = rest[1:]  # the duration
            words = rest
        else:
            break
    return words


def check_rm(args: list[str]) -> None:
    recursive = force = False
    targets = []
    for arg in args:
        if arg.startswith("--") and len(arg) > 2:
            name = arg.split("=", 1)[0]
            recursive = recursive or "--recursive".startswith(name)
            force = force or "--force".startswith(name)
        elif arg.startswith("-") and len(arg) > 1 and arg != "--":
            recursive = recursive or "r" in arg or "R" in arg
            force = force or "f" in arg
        elif arg != "--":
            targets.append(arg)
    if recursive and force:
        raise Blocked("recursive forced delete (rm -r -f in any form)")
    if recursive and any((t.rstrip("/") or "/") in BROAD_TARGETS or t in BROAD_TARGETS for t in targets):
        raise Blocked("recursive delete of a broad target")


def code_argument(args: list[str], value_flags: tuple[str, ...]) -> str | None:
    """Return the code string of an interpreter -c/-e invocation, if there is one."""
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in value_flags:
            i += 2
            continue
        if arg in ("--command",):
            return args[i + 1] if i + 1 < len(args) else None
        if arg.startswith("-") and not arg.startswith("--") and len(arg) > 1:
            if any(letter in arg[1:] for letter in "ce"):
                return args[i + 1] if i + 1 < len(args) else None
        elif not arg.startswith("-"):
            return None
        i += 1
    return None


def git_subcommand(args: list[str]) -> tuple[str, list[str]]:
    i = 0
    while i < len(args):
        if args[i] in ("-c", "-C", "--git-dir", "--work-tree", "--namespace"):
            i += 2
        elif args[i].startswith("-"):
            i += 1
        else:
            return args[i], args[i + 1 :]
    return "", []


def check_git(args: list[str]) -> None:
    sub, rest = git_subcommand(args)
    global_options = args[: len(args) - len(rest) - (1 if sub else 0)]
    if any("core.hooksPath" in option for option in global_options):
        raise Blocked("git -c core.hooksPath would disable the secret-scan hook")
    if sub == "config" and any("core.hooksPath" in a for a in rest) and "--get" not in rest:
        raise Blocked("changing core.hooksPath would disable the secret-scan hook")
    if sub == "commit":
        for arg in rest:
            if arg == "--no-verify" or (re.fullmatch(r"-[A-Za-z]+", arg) and "n" in arg):
                raise Blocked("git commit --no-verify skips the secret scan")
    if sub == "push":
        for arg in rest:
            if (
                arg.startswith("--force")
                or arg in ("--delete", "--mirror", "--prune")
                or re.fullmatch(r"-[A-Za-z]+", arg) and ("f" in arg or "d" in arg)
                or arg.startswith("+")
                or arg.startswith(":") and len(arg) > 1
            ):
                raise Blocked("force push, remote delete or mirror push")


def check_docker(command: str, args: list[str]) -> None:
    rest = [a for a in args if not a.startswith("-")]
    if command == "docker" and "compose" in rest:
        after = rest[rest.index("compose") + 1 :]
        if "config" in after or "convert" in after:
            raise Blocked("docker compose config prints .env values")
    if command == "docker-compose" and ("config" in rest or "convert" in rest):
        raise Blocked("docker-compose config prints .env values")


def check_env_files(command: str, args: list[str]) -> None:
    if command in ENV_SAFE_COMMANDS:
        return
    cleaned, skip = [], False
    for arg in args:
        if skip:
            skip = False
        elif arg in (">", ">>", "&>", ">|"):
            skip = True  # output redirect target: writing, not reading
        else:
            cleaned.append(arg)
    positional = [a for a in cleaned if not a.startswith("-")]
    ignored: list[str] = []
    if command in DEST_LAST and len(positional) >= 2:
        ignored = [positional[-1]]
    elif command in PATTERN_FIRST and positional and not any(a in ("-e", "-f", "--regexp") for a in cleaned):
        ignored = [positional[0]]
    root = project_dir()
    for arg in cleaned:
        if arg in ignored:
            ignored.remove(arg)
            continue
        for part in re.split(r"[=:]", arg):
            if not part or part.startswith("-"):
                continue
            name = os.path.basename(part.rstrip("/"))
            if name in ENV_SAFE:
                continue
            if ENV_FILE.match(name):
                raise Blocked(f"access to secrets file: {part}")
            relative = os.path.relpath(os.path.abspath(part), root).replace(os.sep, "/")
            if relative.startswith("secrets/"):
                raise Blocked(f"access to secrets directory: {part}")


def check_interpreter(name: str, args: list[str], depth: int) -> None:
    value_flags = ("-o", "+o") if name in SHELLS else ("-W", "-X")
    code = code_argument(args, value_flags)
    if code is not None:
        if SUBST_DL in code:
            raise Blocked("downloaded content executed through command substitution")
        if name in SHELLS:
            check_string(code, depth + 1)
        elif CODE_FORBIDDEN.search(code):
            raise Blocked(f"{name} code with file deletion, process execution or secrets access")
        return
    positional = [a for a in args if not a.startswith("-")]
    if positional and SUBST_DL in positional[0]:
        raise Blocked("downloaded content executed as a script")


def check_single(words: list[str], depth: int) -> None:
    words = strip_prefix(words, depth)
    if not words:
        return
    command = base(words[0])
    args = words[1:]
    if command in SKIPPED_COMMANDS:
        return

    if command in ("sudo", "doas"):
        raise Blocked(command)
    if command == "eval":
        raise Blocked("eval")
    if command == "uv" and args[:1] == ["run"]:
        rest = args[1:]
        while rest and rest[0].startswith("-"):
            rest = rest[2:] if rest[0] in UV_VALUE_FLAGS else rest[1:]
        check_single(rest, depth)
        return
    if command == "rm":
        check_rm(args)
    if command == "find":
        if "-delete" in args:
            raise Blocked("find -delete")
        for index, arg in enumerate(args):
            if arg in ("-exec", "-execdir", "-ok", "-okdir") and index + 1 < len(args):
                if base(args[index + 1]) in ("rm", "unlink", "shred"):
                    raise Blocked("find with -exec rm")
                inner = []
                for item in args[index + 1 :]:
                    if item in (";", "+", "\\;"):
                        break
                    inner.append(item)
                check_single(inner, depth)
    if command == "git":
        check_git(args)
    if command in ("docker", "docker-compose"):
        check_docker(command, args)
    if is_interpreter(command):
        if command in ("source", "."):
            if any(SUBST_DL in a for a in args):
                raise Blocked("downloaded content sourced")
        else:
            check_interpreter(command, args, depth)
    check_env_files(command, args)


def check_string(command: str, depth: int = 0) -> None:
    if depth > MAX_DEPTH:
        raise Blocked("command nesting too deep")
    command = sanitize(strip_heredocs(command, depth), depth)
    try:
        tokens = tokenize(command)
    except ValueError as exc:
        raise Blocked(f"unparseable command ({exc})")
    pipeline_has_download = False
    for words, piped in split_commands(tokens):
        if not piped:
            pipeline_has_download = False
        stripped = strip_prefix(words, depth)
        if stripped:
            name = base(stripped[0])
            if name in DOWNLOADERS:
                pipeline_has_download = True
            elif is_interpreter(name) and pipeline_has_download and code_argument(stripped[1:], ("-o", "-W")) is None:
                raise Blocked("downloaded content piped into an interpreter")
        check_single(words, depth)
    # `bash < <(curl ...)`: the redirect target is the download placeholder
    for index, token in enumerate(tokens):
        if token == "<" and index + 1 < len(tokens) and SUBST_DL in tokens[index + 1]:
            if index > 0 and is_interpreter(base(tokens[index - 1])):
                raise Blocked("downloaded content piped into an interpreter")


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        command = payload["tool_input"]["command"]
        if not isinstance(command, str):
            raise TypeError("command is not a string")
    except Exception as exc:
        print(f"pre-bash-guard: cannot read hook input ({exc}); blocking", file=sys.stderr)
        return 2
    try:
        check_string(command)
    except Blocked as reason:
        print(f"pre-bash-guard: blocked - {reason}. Ask the user if this is really needed.", file=sys.stderr)
        return 2
    except Exception as exc:  # fail closed
        print(f"pre-bash-guard: internal error ({type(exc).__name__}); blocking", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
