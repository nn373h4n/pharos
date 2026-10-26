"""PreToolUse guard for Bash commands (Claude Code hook).

Reads the hook JSON on stdin and exits 2 (block, reason on stderr) when the
command matches a forbidden form. Deny rules in settings.json only match
literal prefixes, so this tokenizes the command and also catches reordered
flags, absolute paths, wrappers and shell -c strings.

Limit: this is a guard rail, not a sandbox. Code run through an interpreter
(for example a script file) cannot be analyzed.
"""

import json
import os
import re
import shlex
import sys

SEPARATORS = {";", "&&", "||", "|", "&", "|&", "(", ")"}
PIPES = {"|", "|&"}
WRAPPERS = {"env", "command", "nohup", "time", "nice", "builtin", "exec", "xargs"}
SHELLS = {"sh", "bash", "zsh", "dash", "ash"}
DOWNLOADERS = {"curl", "wget"}
INTERPRETERS = SHELLS | {"python", "python3", "perl", "ruby", "node"}
ENV_FILE = re.compile(r"^\.env(\..+)?$")
ENV_SAFE = {".env.example", ".env.sample", ".env.template"}
ENV_SAFE_COMMANDS = {"ls", "stat", "test", "["}
DANGEROUS_PY = re.compile(r"rmtree|os\.(system|remove|unlink|rmdir)|subprocess|unlink\(")
BROAD_TARGETS = {"/", "~", "$HOME", "*", ".", "..", "./", "../"}


class Blocked(Exception):
    pass


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


def strip_prefix(words: list[str]) -> list[str]:
    """Drop VAR=value assignments and wrapper commands in front of the real one."""
    while words:
        head = os.path.basename(words[0].lstrip("\\"))
        if "=" in words[0] and not words[0].startswith("-"):
            words = words[1:]
        elif head in WRAPPERS:
            words = words[1:]
            while words and words[0].startswith("-"):
                words = words[1:]
        else:
            break
    return words


def check_rm(args: list[str]) -> None:
    recursive = force = False
    targets = []
    for arg in args:
        if arg == "--recursive":
            recursive = True
        elif arg == "--force":
            force = True
        elif arg.startswith("-") and not arg.startswith("--"):
            recursive = recursive or "r" in arg or "R" in arg
            force = force or "f" in arg
        elif not arg.startswith("--"):
            targets.append(arg)
    if recursive and force:
        raise Blocked("recursive forced delete (rm -r -f in any form)")
    if recursive and any(t.rstrip("/") in BROAD_TARGETS or t in BROAD_TARGETS for t in targets):
        raise Blocked("recursive delete of a broad target")


def check_env_files(command: str, args: list[str]) -> None:
    if command in ENV_SAFE_COMMANDS:
        return
    for arg in args:
        name = os.path.basename(arg.rstrip("/"))
        if name in ENV_SAFE:
            continue
        if ENV_FILE.match(name) or "/secrets/" in f"/{arg}" or arg.rstrip("/").endswith("secrets"):
            raise Blocked(f"access to secrets file or directory: {arg}")


def check_single(words: list[str], depth: int) -> None:
    words = strip_prefix(words)
    if not words:
        return
    command = os.path.basename(words[0].lstrip("\\"))
    args = words[1:]

    if command == "sudo":
        raise Blocked("sudo")
    if command == "rm":
        check_rm(args)
    if command == "find" and ("-delete" in args or "-exec" in args and "rm" in args):
        raise Blocked("find with -delete or -exec rm")
    if command == "git" and "push" in args:
        if any(a in ("-f", "--force", "--force-with-lease") or a.startswith("+") for a in args):
            raise Blocked("force push")
    if command in ("docker", "docker-compose"):
        rest = [a for a in args if not a.startswith("-")]
        if command == "docker" and rest[:1] == ["compose"] and "config" in rest[1:]:
            raise Blocked("docker compose config prints .env values")
        if command == "docker-compose" and "config" in rest:
            raise Blocked("docker-compose config prints .env values")
    if command in SHELLS and "-c" in args and depth < 3:
        index = args.index("-c")
        if index + 1 < len(args):
            check_string(args[index + 1], depth + 1)
    if command in ("python", "python3") and "-c" in args:
        index = args.index("-c")
        if index + 1 < len(args) and DANGEROUS_PY.search(args[index + 1]):
            raise Blocked("python -c with file deletion or process execution")
    if command == "eval":
        raise Blocked("eval")
    check_env_files(command, args)


def check_string(command: str, depth: int = 0) -> None:
    try:
        tokens = tokenize(command)
    except ValueError as exc:
        raise Blocked(f"unparseable command ({exc})")
    # Pipes: a download piped into an interpreter is remote code execution.
    pipeline_has_download = False
    for words, piped in split_commands(tokens):
        if not piped:
            pipeline_has_download = False
        words = strip_prefix(words)
        if not words:
            continue
        name = os.path.basename(words[0].lstrip("\\"))
        if name in DOWNLOADERS:
            pipeline_has_download = True
        elif name in INTERPRETERS and pipeline_has_download and "-c" not in words:
            raise Blocked("downloaded content piped into an interpreter")
        check_single(words, depth)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        command = payload["tool_input"]["command"]
    except (ValueError, KeyError, TypeError) as exc:
        print(f"pre-bash-guard: cannot read hook input ({exc}); blocking", file=sys.stderr)
        return 2
    try:
        check_string(command)
    except Blocked as reason:
        print(f"pre-bash-guard: blocked - {reason}. Ask the user if this is really needed.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
