"""scripts/backup.sh and scripts/restore.sh (M5-02, ADR-0022), with Docker replaced by a script that
answers like the database container would. The real restore, with real roles and a real database,
is rehearsed in scripts/compose-smoke.sh; this file proves the rules the scripts keep: what a
backup is allowed to call good, what is kept, where the secret file may go, and when a restore must
refuse."""

import datetime
import fcntl
import hashlib
import os
import shutil
import stat
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BACKUP = ROOT / "scripts" / "backup.sh"
RESTORE = ROOT / "scripts" / "restore.sh"

LISTING = (
    "; Archive created at 2026-11-04\n"
    "215; 1259 16401 TABLE public alembic_version pharos_owner\n"
    "216; 1259 16410 TABLE public items pharos_owner\n"
    "3; 3079 16385 EXTENSION - vector\n"
    "4; 0 0 COMMENT - EXTENSION vector\n"
)

# What scripts/restore/catalog.sql prints, in short (the real query runs in compose-smoke.sh).
SHAPE = (
    "acl|relation|public.items|pharos_app:SELECT/pharos_owner\n"
    "policy|public.items.items_org|cmd=* permissive=true roles=PUBLIC using=x check=\n"
    "relation|public.items|kind=r owner=pharos_owner rls=true force=false columns=abc\n"
)

FAKE_DOCKER = r"""#!/bin/bash
cfg="$FAKE_DIR"
echo "$*" >> "$cfg/calls.log"
case "$*" in
  *'--username="$POSTGRES_USER"'*pharos_roles_digest*)
    # The superuser's digest of the roles: one line of roles_digest per call (before, after).
    n=$(( $(cat "$cfg/roles_digest_calls" 2>/dev/null || echo 0) + 1 ))
    echo "$n" > "$cfg/roles_digest_calls"
    sed -n "${n}p" "$cfg/roles_digest" ;;
  *'--username="$POSTGRES_USER"'*)
    # The superuser: making or dropping the scratch databases of the reference.
    if [[ "$*" != *"--command"* ]]; then cat >> "$cfg/super.sql"; fi
    [ -f "$cfg/fail_super" ] && exit 1
    : ;;
  *"--entrypoint sh migrate"*)
    [ -f "$cfg/fail_migrate" ] && { echo "alembic: Can't locate revision" >&2; exit 1; }
    : ;;
  *"pg_dump --username=pharos_owner --format=custom pharos_restore_build"*)
    [ -f "$cfg/fail_reference" ] && exit 1
    : ;;
  *"--dbname=pharos_restore_reference --no-psqlrc --quiet --tuples-only"*)
    cat > /dev/null
    cat "$cfg/shape_reference" ;;
  *"--dbname=pharos --no-psqlrc --quiet --tuples-only"*)
    cat > /dev/null
    cat "$cfg/shape_restored" ;;
  *"FROM public.alembic_version"*) cat "$cfg/version" ;;
  *"e.extowner"*) cat "$cfg/odd_extensions" ;;
  *"pg_extension"*) cat "$cfg/extensions" ;;
  *"pg_dump"*)
    [ -f "$cfg/fail_dump" ] && exit 1
    cat "$cfg/dump" ;;
  *"pg_restore --list"*)
    cat > /dev/null
    [ -f "$cfg/fail_list" ] && exit 1
    cat "$cfg/listing" ;;
  *"pg_restore --file"*)
    cat > /dev/null
    [ -f "$cfg/fail_full" ] && exit 1
    : ;;
  *"pg_restore "*)
    cat > "$cfg/restore.stdin"
    echo "$*" > "$cfg/restore.args"
    [ -f "$cfg/fail_restore" ] && exit 1
    : ;;
  *"pg_db_role_setting"*) cat "$cfg/strange" ;;
  *"pg_auth_members"*) cat "$cfg/members" ;;
  *"pg_class"*) cat "$cfg/tables" ;;
  *"rolbypassrls"*) cat "$cfg/plain" ;;
  *"pg_roles"*) cat "$cfg/roles" ;;
  *"FROM information_schema.tables"*) cat "$cfg/tables" ;;
  *"cat > /tmp/pharos-restore.list"*) cat > "$cfg/list" ;;
esac
exit 0
"""


@pytest.fixture
def fake(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    state = tmp_path / "state"
    state.mkdir()
    (state / "dump").write_bytes(b"PGDMP-fake-dump-bytes")
    (state / "listing").write_text(LISTING)
    (state / "roles").write_text("2\n")
    (state / "plain").write_text("2\n")
    (state / "members").write_text("0\n")
    (state / "tables").write_text("0\n")
    (state / "strange").write_text("0\n")
    (state / "version").write_text("0011_interaction_counts_monthly\n")
    (state / "extensions").write_text("vector\n")
    (state / "odd_extensions").write_text("0\n")
    (state / "roles_digest").write_text(f"{'a' * 32}\n{'a' * 32}\n")
    (state / "shape_restored").write_text(SHAPE)
    (state / "shape_reference").write_text(SHAPE)
    return tmp_path


@pytest.fixture(autouse=True)
def nothing_is_left_in_the_repository() -> Iterator[None]:
    """Some tests aim a script at a path inside the repository, which it must refuse. If a refusal
    ever regressed, the directory would be made there: remove it, and fail loudly."""
    yield
    left = [*ROOT.glob("zz-m5-02-*"), *ROOT.glob("pharos-env-*")]
    for path in left:
        shutil.rmtree(path) if path.is_dir() else path.unlink()
    assert not left, f"a script wrote inside the repository: {left}"


def run(
    script: Path,
    fake: Path,
    *args: str,
    now: str | None = None,
    cwd: Path | None = None,
    **extra: str,
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": f"{fake / 'bin'}:/usr/bin:/bin",
        "HOME": str(fake),
        "FAKE_DIR": str(fake / "state"),
        "PHAROS_COMPOSE": "docker",
        **extra,
    }
    if now is not None:
        env["PHAROS_BACKUP_NOW"] = now
    return subprocess.run(  # noqa: S603
        ["bash", str(script), *args],  # noqa: S607
        env=env,
        cwd=cwd or fake,
        capture_output=True,
        text=True,
        check=False,
    )


def calls(fake: Path) -> str:
    log = fake / "state" / "calls.log"
    return log.read_text() if log.exists() else ""


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def private_tree(path: Path) -> None:
    """A directory and its parents as the script wants them: ours and closed to others."""
    missing = [p for p in (path, *path.parents) if not p.exists()]
    for p in reversed(missing):
        p.mkdir(mode=0o700)
        p.chmod(0o700)


def dumps(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.glob("pharos-*.dump"))


# --- backup -------------------------------------------------------------------------------------


def test_a_backup_is_written_private_and_checked(fake: Path) -> None:
    out = fake / "backups"
    done = run(BACKUP, fake, "run", "--dir", str(out), now="20261104T020000Z")
    assert done.returncode == 0, done.stderr
    dump = out / "daily" / "pharos-20261104T020000Z.dump"
    assert dump.read_bytes() == b"PGDMP-fake-dump-bytes"
    assert mode(dump) == 0o600 and mode(out) == 0o700 and mode(out / "daily") == 0o700
    assert not list((out / "daily").glob(".*partial"))
    log = calls(fake)
    # As the owner role through the local socket, and read back with pg_restore --list.
    assert "pg_dump" in log and "--username=pharos_owner" in log and "pg_restore --list" in log
    assert "PASSWORD" not in log.upper() and "PGPASSWORD" not in done.stdout.upper()


@pytest.mark.parametrize(
    ("what", "arrange"),
    [
        ("pg_dump fails", lambda s: (s / "fail_dump").write_text("")),
        ("the dump is empty", lambda s: (s / "dump").write_bytes(b"")),
        ("pg_restore cannot read the dump back", lambda s: (s / "fail_list").write_text("")),
        (
            "the dump has no alembic_version table",
            lambda s: (s / "listing").write_text("216; 1259 16410 TABLE public items x\n"),
        ),
    ],
)
def test_a_dump_that_is_not_good_is_never_kept(fake: Path, what: str, arrange) -> None:  # type: ignore[no-untyped-def]
    arrange(fake / "state")
    out = fake / "backups"
    done = run(BACKUP, fake, "run", "--dir", str(out), now="20261104T020000Z")
    assert done.returncode != 0, what
    leftovers = [p for p in (out / "daily").iterdir()] if (out / "daily").exists() else []
    assert leftovers == [], what  # not even a partial file


def test_a_good_dump_passes_where_the_bad_ones_fail(fake: Path) -> None:
    """The positive control of the test above: the same call with nothing wrong succeeds."""
    out = fake / "backups"
    assert run(BACKUP, fake, "run", "--dir", str(out), now="20261104T020000Z").returncode == 0
    assert len(dumps(out / "daily")) == 1


@pytest.mark.parametrize("day", ["2026-11-01", "2026-11-02", "2026-11-03", "2026-11-08"])
def test_a_sunday_adds_a_weekly_copy_and_the_first_of_the_month_a_monthly_one(
    fake: Path, day: str
) -> None:
    when = datetime.date.fromisoformat(day)
    out = fake / "backups"
    stamp = when.strftime("%Y%m%d") + "T020000Z"
    assert run(BACKUP, fake, "run", "--dir", str(out), now=stamp).returncode == 0
    name = f"pharos-{stamp}.dump"
    assert dumps(out / "daily") == [name]
    assert (dumps(out / "weekly") == [name]) is (when.isoweekday() == 7)
    assert (dumps(out / "monthly") == [name]) is (when.day == 1)


def test_the_day_choices_cover_both_cases() -> None:
    """Guards the test above: the chosen days include a Sunday and a first, and days that are
    neither, so neither branch of the rule can pass by being always true or always false."""
    days = [datetime.date.fromisoformat(d) for d in ("2026-11-01", "2026-11-02", "2026-11-08")]
    assert any(d.isoweekday() == 7 for d in days) and any(d.isoweekday() != 7 for d in days)
    assert any(d.day == 1 for d in days) and any(d.day != 1 for d in days)


def test_pruning_keeps_the_newest_seven_four_and_three_and_nothing_else_is_touched(
    fake: Path,
) -> None:
    out = fake / "backups"
    for kind, count in (("daily", 10), ("weekly", 6), ("monthly", 5)):
        private_tree(out / kind)
        for n in range(count):
            (out / kind / f"pharos-202611{n + 10:02d}T020000Z.dump").write_text(kind)
        (out / kind / "notes.txt").write_text("keep me")
        (out / kind / "pharos-bad.dump").write_text("not a stamp: keep me")
    done = run(BACKUP, fake, "prune", "--dir", str(out))
    assert done.returncode == 0, done.stderr
    for kind, keep, total in (("daily", 7, 10), ("weekly", 4, 6), ("monthly", 3, 5)):
        left = [n for n in dumps(out / kind) if n != "pharos-bad.dump"]
        assert len(left) == keep
        newest = [f"pharos-202611{n + 10:02d}T020000Z.dump" for n in range(total - keep, total)]
        assert left == newest  # the newest ones, by time
        assert (out / kind / "notes.txt").read_text() == "keep me"
        assert (out / kind / "pharos-bad.dump").exists()


def test_a_wrong_clock_value_is_refused(fake: Path) -> None:
    done = run(BACKUP, fake, "run", "--dir", str(fake / "b"), now="yesterday")
    assert done.returncode != 0 and "not a stamp" in done.stderr
    assert calls(fake) == ""  # refused before Docker was started


def test_without_a_directory_or_with_an_unknown_command_it_only_explains(fake: Path) -> None:
    assert run(BACKUP, fake, "run").returncode == 2
    assert run(BACKUP, fake, "frobnicate").returncode == 2
    assert run(BACKUP, fake).returncode == 2
    assert calls(fake) == ""  # Docker was never started


# --- the secret file ----------------------------------------------------------------------------


@pytest.fixture
def env_file(fake: Path) -> Path:
    path = fake / "env-source"
    path.write_text("PHAROS_SECRET_KEY=not-a-real-secret\n")
    return path


def test_the_environment_file_is_copied_apart_from_the_dumps(fake: Path, env_file: Path) -> None:
    dumps_dir, env_dir = fake / "dumps", fake / "secrets"
    done = run(
        BACKUP, fake, "env", "--dir", str(env_dir), "--dumps", str(dumps_dir),
        now="20261104T020000Z", PHAROS_ENV_FILE=str(env_file),
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    copy = env_dir / "pharos-env-20261104T020000Z"
    assert copy.read_text() == env_file.read_text()
    assert mode(copy) == 0o600 and mode(env_dir) == 0o700


@pytest.mark.parametrize(
    ("env_dir", "dumps_dir", "reason"),
    [
        ("same", "same", "is the dump directory or inside it"),
        ("same/inside", "same", "is the dump directory or inside it"),
        ("outer", "outer/dumps", "contains the dump directory"),
    ],
)
def test_the_environment_file_is_never_put_with_the_dumps(
    fake: Path, env_file: Path, env_dir: str, dumps_dir: str, reason: str
) -> None:
    done = run(
        BACKUP, fake, "env", "--dir", str(fake / env_dir), "--dumps", str(fake / dumps_dir),
        PHAROS_ENV_FILE=str(env_file),
    )  # fmt: skip
    assert done.returncode != 0 and reason in done.stderr  # refused for THIS reason
    assert not list(fake.rglob("pharos-env-*"))


def test_a_missing_environment_file_is_reported(fake: Path) -> None:
    done = run(
        BACKUP, fake, "env", "--dir", str(fake / "a"), "--dumps", str(fake / "b"),
        PHAROS_ENV_FILE=str(fake / "nope"),
    )  # fmt: skip
    assert done.returncode != 0 and "does not exist" in done.stderr


# --- restore ------------------------------------------------------------------------------------


@pytest.fixture
def dump_file(fake: Path) -> Path:
    path = fake / "pharos-20261104T020000Z.dump"
    path.write_bytes(b"PGDMP-fake-dump-bytes")
    sign(path)
    return path


def sign(path: Path) -> None:
    """The SHA-256 file that backup.sh writes beside a dump."""
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    (path.parent / (path.name + ".sha256")).write_text(f"{digest}  {path.name}\n")


def restore_calls(fake: Path) -> list[str]:
    return [
        line for line in calls(fake).splitlines() if "pg_restore " in line and "--list" not in line
    ]


def test_a_restore_into_a_new_database_with_its_roles_goes_through_in_one_transaction(
    fake: Path, dump_file: Path
) -> None:
    done = run(RESTORE, fake, str(dump_file))
    assert done.returncode == 0, done.stderr
    args = (fake / "state" / "restore.args").read_text()
    assert "--single-transaction" in args and "--exit-on-error" in args
    assert "--username=pharos_owner" in args and "--use-list=" in args
    assert (fake / "state" / "restore.stdin").read_bytes() == dump_file.read_bytes()
    # The extension entries are left out (the init script made the extension as a superuser).
    listed = (fake / "state" / "list").read_text()
    assert (
        "EXTENSION" not in listed and "alembic_version" in listed and "TABLE public items" in listed
    )
    assert "PASSWORD" not in calls(fake).upper()


@pytest.mark.parametrize("roles", ["0", "1"])
def test_a_restore_without_both_roles_is_refused_before_anything_is_restored(
    fake: Path, dump_file: Path, roles: str
) -> None:
    (fake / "state" / "roles").write_text(roles + "\n")
    done = run(RESTORE, fake, str(dump_file))
    assert done.returncode != 0 and "roles" in done.stderr
    assert restore_calls(fake) == []


def test_a_restore_into_a_database_that_has_tables_is_refused(fake: Path, dump_file: Path) -> None:
    (fake / "state" / "tables").write_text("12\n")
    done = run(RESTORE, fake, str(dump_file))
    assert done.returncode != 0 and "already has 12 tables" in done.stderr
    assert restore_calls(fake) == []


def test_functions_and_default_privileges_count_as_not_empty(fake: Path, dump_file: Path) -> None:
    """A failed hostile restore can leave a function or a default privilege and no table."""
    (fake / "state" / "tables").write_text("1\n")
    done = run(RESTORE, fake, str(dump_file))
    assert done.returncode != 0
    assert "tables, views, sequences, functions or default privileges" in done.stderr
    assert restore_calls(fake) == []
    query = next(line for line in calls(fake).splitlines() if "pg_catalog.pg_class" in line)
    assert "pg_catalog.pg_proc" in query and "pg_catalog.pg_default_acl" in query
    assert "deptype = 'e'" in query  # the extension's own functions do not count


def test_a_file_that_is_not_a_pharos_dump_is_refused(fake: Path, dump_file: Path) -> None:
    (fake / "state" / "listing").write_text("216; 1259 16410 TABLE public items x\n")
    done = run(RESTORE, fake, str(dump_file))
    assert done.returncode != 0 and "not a Pharos dump" in done.stderr
    assert restore_calls(fake) == []


def test_a_missing_file_or_no_argument_is_refused(fake: Path) -> None:
    assert run(RESTORE, fake, str(fake / "nope.dump")).returncode != 0
    assert run(RESTORE, fake).returncode == 2
    assert restore_calls(fake) == []


def test_a_failing_pg_restore_makes_the_script_fail(fake: Path, dump_file: Path) -> None:
    (fake / "state" / "fail_restore").write_text("")
    done = run(RESTORE, fake, str(dump_file))
    # A hostile dump can end the restore's transaction itself: never say nothing was restored.
    assert done.returncode != 0 and "nothing was restored" not in done.stderr
    assert "may be partly changed" in done.stderr and "remove the postgres volume" in done.stderr


def test_a_dump_that_changes_a_role_is_refused(fake: Path, dump_file: Path) -> None:
    (fake / "state" / "roles_digest").write_text(f"{'a' * 32}\n{'b' * 32}\n")
    done = run(RESTORE, fake, str(dump_file))
    assert done.returncode != 0 and "the dump changed a role" in done.stderr
    assert "a" * 32 not in done.stdout + done.stderr  # the digest is never printed
    assert "migrate" not in calls(fake)  # refused before the reference is built


@pytest.mark.parametrize(
    "digests", ["", "not-a-digest\nnot-a-digest\n", f"{'a' * 32}\n"], ids=["none", "bad", "after"]
)
def test_roles_that_cannot_be_read_fail_closed(fake: Path, dump_file: Path, digests: str) -> None:
    (fake / "state" / "roles_digest").write_text(digests)
    done = run(RESTORE, fake, str(dump_file))
    assert done.returncode != 0 and "cannot read the roles" in done.stderr
    # Unreadable before the restore: refused before anything is restored.
    restored = "--single-transaction --use-list" in calls(fake)
    assert restored == (digests.count("\n") == 1)


def test_the_roles_are_read_before_and_after_the_restore_as_the_superuser(
    fake: Path, dump_file: Path
) -> None:
    done = run(RESTORE, fake, str(dump_file))
    assert done.returncode == 0, done.stderr
    lines = calls(fake).splitlines()
    digests = [i for i, line in enumerate(lines) if "pharos_roles_digest" in line]
    restore = next(i for i, line in enumerate(lines) if "--single-transaction --use-list" in line)
    assert len(digests) == 2 and digests[0] < restore < digests[1]
    for i in digests:
        assert '--username="$POSTGRES_USER"' in lines[i]
        for table in ("pg_catalog.pg_authid", "pg_catalog.pg_auth_members", "pg_db_role_setting"):
            assert table in lines[i]


@pytest.mark.parametrize("bad", [0o777, 0o772], ids=["open", "group-writable"])
def test_a_scratch_directory_others_can_write_to_is_refused(
    fake: Path, dump_file: Path, bad: int
) -> None:
    """Whoever may rename the private copy's directory could swap the dump after its hash."""
    tmp = fake / "shared"
    tmp.mkdir()
    tmp.chmod(bad)
    done = run(RESTORE, fake, str(dump_file), PHAROS_RESTORE_TMP=str(tmp))
    assert done.returncode != 0 and "writable by others" in done.stderr
    assert not list(tmp.iterdir()) and "pg_restore" not in calls(fake)
    # Under a parent others can write to, too; with the sticky bit (like /tmp) it is fine.
    inner = tmp / "inner"
    inner.mkdir(mode=0o700)
    done = run(RESTORE, fake, str(dump_file), PHAROS_RESTORE_TMP=str(inner))
    assert done.returncode != 0 and "writable by others" in done.stderr
    tmp.chmod(bad | 0o1000)
    done = run(RESTORE, fake, str(dump_file), PHAROS_RESTORE_TMP=str(inner))
    assert done.returncode == 0, done.stderr


def test_the_scripts_keep_the_safety_settings_and_never_name_a_password() -> None:
    for script in (BACKUP, RESTORE):
        text = script.read_text()
        assert "set -euo pipefail" in text
        assert os.access(script, os.X_OK)
        assert "PGPASSWORD" not in text and "--password" not in text and " -W" not in text
    assert "umask 077" in BACKUP.read_text()


# --- found in the security review of M5-02 -----------------------------------------------------


def test_every_dump_gets_a_checksum_file_that_matches_it(fake: Path) -> None:
    out = fake / "backups"
    assert run(BACKUP, fake, "run", "--dir", str(out), now="20261108T020000Z").returncode == 0
    dump = out / "daily" / "pharos-20261108T020000Z.dump"
    sidecar = Path(str(dump) + ".sha256")
    assert sidecar.read_text() == f"{hashlib.sha256(dump.read_bytes()).hexdigest()}  {dump.name}\n"
    assert mode(sidecar) == 0o600
    # The weekly copy (2026-11-08 is a Sunday) brings its checksum file along.
    assert (out / "weekly" / dump.name).exists() and (
        out / "weekly" / (dump.name + ".sha256")
    ).exists()


def test_pruning_removes_the_checksum_file_of_a_removed_dump_and_only_that(fake: Path) -> None:
    out = fake / "backups"
    private_tree(out / "daily")
    for n in range(9):
        dump = out / "daily" / f"pharos-202611{n + 10:02d}T020000Z.dump"
        dump.write_text("x")
        sign(dump)
    (out / "daily" / "pharos-other.dump.sha256").write_text("not ours")
    assert run(BACKUP, fake, "prune", "--dir", str(out)).returncode == 0
    names = sorted(p.name for p in (out / "daily").iterdir())
    assert len([n for n in names if n.endswith(".dump")]) == 7
    assert (
        len([n for n in names if n.endswith(".dump.sha256") and n.startswith("pharos-2026")]) == 7
    )
    assert "pharos-other.dump.sha256" in names  # not a name of ours: left alone


def test_a_dump_made_after_a_clock_error_is_not_pruned_and_the_clock_is_questioned(
    fake: Path,
) -> None:
    """Eight dumps stamped in the future (a clock that was wrong once) must not make every later
    dump vanish."""
    out = fake / "backups"
    private_tree(out / "daily")
    for n in range(8):
        (out / "daily" / f"pharos-20300101T0{n}0000Z.dump").write_text("future")
    done = run(BACKUP, fake, "run", "--dir", str(out), now="20261104T020000Z")
    assert done.returncode == 0, done.stderr
    assert (out / "daily" / "pharos-20261104T020000Z.dump").exists()
    assert "WARNING" in done.stderr and "clock" in done.stderr


def test_a_relative_directory_is_read_from_where_the_caller_stood(fake: Path) -> None:
    work = fake / "work"
    work.mkdir()
    done = run(BACKUP, fake, "run", "--dir", "dumps", now="20261104T020000Z", cwd=work)
    assert done.returncode == 0, done.stderr
    assert (work / "dumps" / "daily" / "pharos-20261104T020000Z.dump").exists()
    assert not (ROOT / "dumps").exists()


@pytest.mark.parametrize("path", ["zz-m5-02-test-dumps", str(ROOT / "zz-m5-02-test-dumps")])
def test_a_directory_inside_the_repository_is_refused_and_not_even_made(
    fake: Path, path: str
) -> None:
    done = run(BACKUP, fake, "run", "--dir", path, now="20261104T020000Z", cwd=ROOT)
    assert done.returncode != 0 and "inside the repository" in done.stderr
    assert not (ROOT / "zz-m5-02-test-dumps").exists()
    assert calls(fake) == ""


def test_the_secret_copy_is_refused_inside_the_repository_too(fake: Path, env_file: Path) -> None:
    done = run(
        BACKUP, fake, "env", "--dir", str(ROOT / "zz-m5-02-test-secret"),
        "--dumps", str(fake / "d"), PHAROS_ENV_FILE=str(env_file),
    )  # fmt: skip
    assert done.returncode != 0 and "inside the repository" in done.stderr
    assert not (ROOT / "zz-m5-02-test-secret").exists()


@pytest.mark.parametrize("open_mode", [0o777, 0o755, 0o750, 0o705])
def test_a_directory_that_is_open_to_others_in_any_way_is_refused(
    fake: Path, open_mode: int
) -> None:
    out = fake / "shared"
    out.mkdir()
    out.chmod(open_mode)
    done = run(BACKUP, fake, "run", "--dir", str(out), now="20261104T020000Z")
    assert done.returncode != 0 and "open to others" in done.stderr
    assert calls(fake) == "" and not list(out.iterdir())


def test_a_good_existing_directory_is_used_where_the_open_one_is_refused(fake: Path) -> None:
    """The positive control of the test above."""
    out = fake / "mine"
    out.mkdir()
    out.chmod(0o700)
    assert run(BACKUP, fake, "run", "--dir", str(out), now="20261104T020000Z").returncode == 0


def test_a_link_instead_of_a_directory_is_refused(fake: Path) -> None:
    target = fake / "elsewhere"
    target.mkdir(mode=0o700)
    link = fake / "link"
    link.symlink_to(target)
    done = run(BACKUP, fake, "run", "--dir", str(link), now="20261104T020000Z")
    assert done.returncode != 0 and "link" in done.stderr
    assert not list(target.iterdir())
    # A link in place of the daily directory is refused as well.
    out = fake / "backups"
    out.mkdir(mode=0o700)
    (out / "daily").symlink_to(target)
    done = run(BACKUP, fake, "run", "--dir", str(out), now="20261104T020000Z")
    assert done.returncode != 0 and not list(target.iterdir())


def test_a_directory_that_starts_with_a_dash_is_refused(fake: Path) -> None:
    done = run(BACKUP, fake, "run", "--dir", "-", now="20261104T020000Z")
    assert done.returncode != 0 and "may not start with '-'" in done.stderr
    assert calls(fake) == ""


def test_a_second_backup_at_the_same_time_is_refused(fake: Path) -> None:
    out = fake / "backups"
    private_tree(out / "daily")
    with (out / ".lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        done = run(BACKUP, fake, "run", "--dir", str(out), now="20261104T020000Z")
    assert done.returncode != 0 and "another backup or prune is running" in done.stderr
    assert calls(fake) == ""
    # Once the lock is let go the same call works (positive control).
    assert run(BACKUP, fake, "run", "--dir", str(out), now="20261104T020000Z").returncode == 0


def test_a_dump_that_stops_early_is_not_kept_even_if_its_contents_list_is_fine(fake: Path) -> None:
    """`pg_restore --list` reads only the table of contents; the whole dump is read too."""
    (fake / "state" / "fail_full").write_text("")
    out = fake / "backups"
    done = run(BACKUP, fake, "run", "--dir", str(out), now="20261104T020000Z")
    assert done.returncode != 0 and "to its end" in done.stderr
    assert not list((out / "daily").iterdir())


# --- restore: a dump is code --------------------------------------------------------------------


def test_a_dump_that_does_not_match_its_checksum_is_refused_before_the_database_is_asked(
    fake: Path, dump_file: Path
) -> None:
    dump_file.write_bytes(b"PGDMP-changed-after-the-checksum")
    done = run(RESTORE, fake, str(dump_file))
    assert done.returncode != 0 and "does not match its SHA-256 file" in done.stderr
    assert calls(fake) == ""


def test_a_dump_without_a_checksum_file_is_refused_and_no_way_around_is_offered(
    fake: Path, dump_file: Path
) -> None:
    Path(str(dump_file) + ".sha256").unlink()
    refused = run(RESTORE, fake, str(dump_file))
    assert refused.returncode != 0 and "no SHA-256 file" in refused.stderr
    assert "--allow-unverified" not in refused.stderr  # the refusal does not teach the bypass
    assert restore_calls(fake) == [] and calls(fake) == ""
    # There is no such option: it is taken for a file name and refused as a path.
    assert run(RESTORE, fake, "--allow-unverified", str(dump_file)).returncode != 0
    assert restore_calls(fake) == []


@pytest.mark.parametrize(
    "line",
    [
        "{other}  other.dump",  # the hash of ANOTHER file, naming that other file
        "{own}  ../elsewhere/{name}",  # a path, not the dump's own name
        "{own}  /etc/hostname",
        "{own} {name}",  # one space: not the form sha256sum writes
        "{own}",  # no name
        "{upper}  {name}",  # not lower-case hex
    ],
)
def test_a_checksum_file_must_be_the_checksum_of_this_dump_and_nothing_else(
    fake: Path, dump_file: Path, line: str
) -> None:
    """Found in the second review: `sha256sum --check` checks whatever file the sidecar names, so a
    sidecar could vouch for another file while the dump itself was never checked."""
    own = hashlib.sha256(dump_file.read_bytes()).hexdigest()
    sidecar = Path(str(dump_file) + ".sha256")
    sidecar.write_text(
        line.format(
            own=own,
            other=hashlib.sha256(b"another file").hexdigest(),
            upper=own.upper(),
            name=dump_file.name,
        )
        + "\n"
    )
    (dump_file.parent / "other.dump").write_bytes(b"another file")  # exists, so a check would pass
    done = run(RESTORE, fake, str(dump_file))
    assert done.returncode != 0
    assert restore_calls(fake) == [] and calls(fake) == ""


def test_a_checksum_file_with_more_than_one_line_is_refused(fake: Path, dump_file: Path) -> None:
    sidecar = Path(str(dump_file) + ".sha256")
    good = sidecar.read_text()
    sidecar.write_text(good + good)
    done = run(RESTORE, fake, str(dump_file))
    assert done.returncode != 0 and "exactly one line" in done.stderr


def test_the_right_checksum_file_passes_where_the_wrong_ones_fail(
    fake: Path, dump_file: Path
) -> None:
    """The positive control of the two tests above."""
    assert run(RESTORE, fake, str(dump_file)).returncode == 0


def test_a_relative_dump_path_is_read_from_where_the_caller_stood(
    fake: Path, dump_file: Path
) -> None:
    done = run(RESTORE, fake, dump_file.name, cwd=dump_file.parent)
    assert done.returncode == 0, done.stderr
    assert (fake / "state" / "restore.stdin").read_bytes() == dump_file.read_bytes()


def test_roles_that_are_members_of_other_roles_are_refused(fake: Path, dump_file: Path) -> None:
    (fake / "state" / "members").write_text("1\n")
    done = run(RESTORE, fake, str(dump_file))
    assert done.returncode != 0 and "must not be members" in done.stderr
    assert restore_calls(fake) == []


def test_roles_that_are_not_plain_login_roles_are_refused(fake: Path, dump_file: Path) -> None:
    (fake / "state" / "plain").write_text("1\n")
    done = run(RESTORE, fake, str(dump_file))
    assert done.returncode != 0 and "must not be superuser" in done.stderr
    assert restore_calls(fake) == []


def test_a_restore_that_leaves_database_settings_or_extra_schemas_is_flagged(
    fake: Path, dump_file: Path
) -> None:
    (fake / "state" / "strange").write_text("1\n")
    done = run(RESTORE, fake, str(dump_file))
    assert done.returncode != 0 and "Do NOT start the stack" in done.stderr


def test_a_path_that_starts_with_a_dash_is_not_taken_for_a_restore_option(fake: Path) -> None:
    done = run(RESTORE, fake, "--frobnicate")
    assert done.returncode != 0 and calls(fake) == ""


# --- found in the second review of M5-02 --------------------------------------------------------


@pytest.mark.parametrize("which", ["--dir", "--dumps"])
def test_a_secret_copy_path_with_a_leading_dash_is_refused_and_the_repository_stays_clean(
    fake: Path, env_file: Path, which: str
) -> None:
    """Before the fix, `env --dir -x` resolved to an empty path, which is the repository directory:
    the root secret was copied into the working tree."""
    other = {"--dir": "--dumps", "--dumps": "--dir"}[which]
    done = run(
        BACKUP, fake, "env", which, "-x", other, str(fake / "elsewhere"),
        now="20261104T020000Z", PHAROS_ENV_FILE=str(env_file), cwd=ROOT,
    )  # fmt: skip
    assert done.returncode != 0 and "may not start with '-'" in done.stderr
    assert not list(ROOT.glob("pharos-env-*"))
    assert not list(fake.rglob("pharos-env-*"))


def test_a_directory_inside_the_repository_is_refused_when_the_script_is_called_through_a_link(
    fake: Path, env_file: Path
) -> None:
    """The repository directory is compared with links resolved on both sides."""
    link = fake / "repo-link"
    link.symlink_to(ROOT)
    inside = ROOT / "zz-m5-02-test-via-link"
    done = run(
        link / "scripts" / "backup.sh", fake, "run", "--dir", str(inside), now="20261104T020000Z"
    )
    assert done.returncode != 0 and "inside the repository" in done.stderr
    assert not inside.exists()
    # The same refusal for the secret copy.
    script = link / "scripts" / "backup.sh"
    done = run(
        script, fake, "env", "--dir", str(inside), "--dumps", str(fake / "d"),
        PHAROS_ENV_FILE=str(env_file),  # it exists, so the refusal is about the directory
    )  # fmt: skip
    assert done.returncode != 0 and "inside the repository" in done.stderr
    assert not inside.exists()


def test_the_weekly_and_monthly_copies_of_the_new_dump_survive_pruning(fake: Path) -> None:
    """Future-dated weekly and monthly dumps must not delete the new copies right after they are
    made. 2026-11-01 is both a Sunday and the 1st, so both copies are made."""
    assert datetime.date(2026, 11, 1).isoweekday() == 7  # the premise of the test
    out = fake / "backups"
    for kind, count in (("weekly", 5), ("monthly", 4)):
        private_tree(out / kind)
        for n in range(count):
            (out / kind / f"pharos-20300101T0{n}0000Z.dump").write_text("future")
    private_tree(out / "daily")
    done = run(BACKUP, fake, "run", "--dir", str(out), now="20261101T020000Z")
    assert done.returncode == 0, done.stderr
    assert (out / "weekly" / "pharos-20261101T020000Z.dump").exists()
    assert (out / "monthly" / "pharos-20261101T020000Z.dump").exists()


def test_pruning_takes_the_lock_too(fake: Path) -> None:
    out = fake / "backups"
    private_tree(out)
    with (out / ".lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        done = run(BACKUP, fake, "prune", "--dir", str(out))
    assert done.returncode != 0 and "another backup or prune is running" in done.stderr
    assert run(BACKUP, fake, "prune", "--dir", str(out)).returncode == 0


# --- found in the third review of M5-02 ---------------------------------------------------------


def test_a_link_written_with_a_trailing_slash_is_still_refused(fake: Path) -> None:
    """`test -L link/` is false for a link; the slash is taken off before the test."""
    target = fake / "elsewhere"
    target.mkdir(mode=0o700)
    link = fake / "link"
    link.symlink_to(target)
    done = run(BACKUP, fake, "run", "--dir", f"{link}/", now="20261104T020000Z")
    assert done.returncode != 0 and "link" in done.stderr
    assert not list(target.iterdir())
    done = run(BACKUP, fake, "run", "--dir", f"{link}///", now="20261104T020000Z")
    assert done.returncode != 0 and not list(target.iterdir())


def test_a_bad_clock_value_stops_the_secret_copy_before_anything_is_written(
    fake: Path, env_file: Path
) -> None:
    """`local target=$(stamp)` hid a failing stamp: the secret was copied to `pharos-env-` and the
    command exited 0."""
    done = run(
        BACKUP, fake, "env", "--dir", str(fake / "e"), "--dumps", str(fake / "d"),
        now="yesterday", PHAROS_ENV_FILE=str(env_file),
    )  # fmt: skip
    assert done.returncode != 0 and "not a stamp" in done.stderr
    assert not list(fake.rglob("pharos-env-*"))


def test_a_file_name_with_a_line_break_cannot_make_pruning_delete_real_dumps(fake: Path) -> None:
    out = fake / "backups"
    private_tree(out / "daily")
    daily = out / "daily"
    for n in range(7):
        (daily / f"pharos-202611{n + 10:02d}T020000Z.dump").write_text("real")
    # Two names that look like two lines to a parser of `ls`; the second line is a dump name.
    (daily / "x\npharos-20990101T000000Z.dump").write_text("odd")
    (daily / "y\npharos-20990102T000000Z.dump").write_text("odd")
    done = run(BACKUP, fake, "prune", "--dir", str(out))
    assert done.returncode == 0, done.stderr
    assert len(dumps(daily)) == 7  # all seven real dumps stay (a parser of `ls` would delete them)
    assert (daily / "x\npharos-20990101T000000Z.dump").exists()  # and the odd files are left alone
    assert (daily / "y\npharos-20990102T000000Z.dump").exists()


def test_a_directory_with_a_dump_name_does_not_break_a_run(fake: Path) -> None:
    out = fake / "backups"
    private_tree(out / "daily" / "pharos-20200101T000000Z.dump")
    # Seven newer dumps, so that pruning would reach the directory (the oldest "dump") and fail
    # on it if it were counted as one.
    for n in range(7):
        (out / "daily" / f"pharos-202611{n + 10:02d}T020000Z.dump").write_text("real")
    done = run(BACKUP, fake, "run", "--dir", str(out), now="20261104T020000Z")
    assert done.returncode == 0, done.stderr
    assert (out / "daily" / "pharos-20261104T020000Z.dump").is_file()


def test_a_partial_file_left_by_a_killed_run_is_removed_by_the_next_one(fake: Path) -> None:
    out = fake / "backups"
    private_tree(out / "daily")
    stale = out / "daily" / ".pharos-abcdefgh.partial"
    stale.write_text("half a dump after a power cut")
    done = run(BACKUP, fake, "run", "--dir", str(out), now="20261104T020000Z")
    assert done.returncode == 0, done.stderr
    assert not stale.exists()


def test_a_second_restore_check_counts_everything_not_only_public_tables(
    fake: Path, dump_file: Path
) -> None:
    (fake / "state" / "tables").write_text("3\n")
    done = run(RESTORE, fake, str(dump_file))
    assert done.returncode != 0 and "already has 3 tables, views, sequences" in done.stderr
    assert restore_calls(fake) == []
    # The query looks at pg_class across schemas, not at information_schema.
    assert "pg_catalog.pg_class" in calls(fake)


def test_the_private_copy_of_the_dump_is_removed_on_every_way_out(
    fake: Path, dump_file: Path
) -> None:
    tmp = fake / "scratch"
    tmp.mkdir(mode=0o700)
    ok = run(RESTORE, fake, str(dump_file), TMPDIR=str(tmp))
    assert ok.returncode == 0, ok.stderr
    assert not list(tmp.iterdir())
    (fake / "state" / "roles").write_text("0\n")  # a refusal after the copy was made
    refused = run(RESTORE, fake, str(dump_file), TMPDIR=str(tmp))
    assert refused.returncode != 0 and not list(tmp.iterdir())
    (fake / "state" / "roles").write_text("2\n")
    (fake / "state" / "fail_restore").write_text("")  # a failing pg_restore
    failed = run(RESTORE, fake, str(dump_file), TMPDIR=str(tmp))
    assert failed.returncode != 0 and not list(tmp.iterdir())


def test_the_private_copy_goes_where_the_operator_says_and_is_never_made_without_room(
    fake: Path, dump_file: Path
) -> None:
    roomy = fake / "disk"
    roomy.mkdir(mode=0o700)
    seen = run(RESTORE, fake, str(dump_file), PHAROS_RESTORE_TMP=str(roomy))
    assert seen.returncode == 0, seen.stderr
    # A `df` that reports almost no free space.
    df = fake / "bin" / "df"
    header = "Filesystem 1-blocks Used Available Capacity Mounted"
    df.write_text(f"#!/bin/sh\necho '{header}'\necho 'x 100 99 1 99% /'\n")
    df.chmod(df.stat().st_mode | stat.S_IEXEC)
    tight = run(RESTORE, fake, str(dump_file), PHAROS_RESTORE_TMP=str(roomy))
    assert tight.returncode != 0 and "PHAROS_RESTORE_TMP" in tight.stderr
    assert not list(roomy.iterdir()) and "pg_restore" not in calls(fake).split("\n")[-2:][0]


def test_a_name_with_control_characters_is_not_printed_raw(fake: Path, dump_file: Path) -> None:
    sidecar = Path(str(dump_file) + ".sha256")
    own = hashlib.sha256(dump_file.read_bytes()).hexdigest()
    sidecar.write_text(f"{own}  \x1b[31mevil\x1b[0m.dump\n")
    done = run(RESTORE, fake, str(dump_file))
    assert done.returncode != 0 and "\x1b" not in done.stderr


# --- the restored database against what the migrations make (F-44) ------------------------------


def test_a_restore_is_compared_with_a_reference_built_at_the_dump_s_own_version(
    fake: Path, dump_file: Path
) -> None:
    done = run(RESTORE, fake, str(dump_file))
    assert done.returncode == 0, done.stderr
    assert "matches the migrations at schema version 0011_interaction_counts_monthly" in done.stdout
    log = calls(fake)
    # Both scratch databases are made with the installation's extensions, as the superuser through
    # the local socket only.
    made = (fake / "state" / "super.sql").read_text()
    for db in ("pharos_restore_build", "pharos_restore_reference"):
        assert f"CREATE DATABASE {db} OWNER pharos_owner TEMPLATE template0" in made
        assert f"\\connect {db}\nCREATE EXTENSION vector;" in made
    # The migrations of this checkout build the dump's own version, in the migrate image, and the
    # owner's password never reaches a command line.
    assert "--entrypoint sh migrate -c" in log
    assert "sh pharos_restore_build 0011_interaction_counts_monthly\n" in log
    assert "PASSWORD" not in log.upper().replace("PHAROS_MIGRATIONS_DATABASE_URL", "")
    # The reference goes through pg_dump and pg_restore like the real database did.
    assert "pg_dump --username=pharos_owner --format=custom pharos_restore_build" in log
    # The scratch databases are removed at the end.
    last = log.splitlines()[-3:]
    assert any("DROP DATABASE IF EXISTS pharos_restore_reference" in line for line in last)


def test_a_dump_that_differs_from_the_migrations_is_refused_and_named(
    fake: Path, dump_file: Path
) -> None:
    evil = "policy|public.users.evil|cmd=* permissive=true roles=PUBLIC using=true check=\n"
    changed = SHAPE.replace("pharos_app:SELECT", "pharos_app:SELECT,pharos_worker:SELECT")
    (fake / "state" / "shape_restored").write_text(changed + evil)
    done = run(RESTORE, fake, str(dump_file))
    assert done.returncode == 1
    assert "only in the dump:       policy|public.users.evil|" in done.stderr
    assert "only in the dump:       acl|relation|public.items" in done.stderr
    assert "only in the reference:  acl|relation|public.items" in done.stderr
    assert "Do NOT start the stack" in done.stderr
    assert "DROP DATABASE IF EXISTS pharos_restore_reference" in calls(fake).splitlines()[-2]


def test_text_from_the_dump_is_shown_without_control_characters(
    fake: Path, dump_file: Path
) -> None:
    (fake / "state" / "shape_restored").write_text(SHAPE + "policy|public.x.\x1b[2Jp|cmd=*\n")
    done = run(RESTORE, fake, str(dump_file))
    assert done.returncode == 1
    assert "\x1b" not in done.stderr and "public.x.?[2Jp" in done.stderr


@pytest.mark.parametrize(
    "version",
    ["", "0010_ai_tools\n0011_interaction_counts_monthly\n", "0011; DROP TABLE x\n", "a-b\n"],
)
def test_a_schema_version_that_is_not_one_plain_revision_is_refused_before_any_reference(
    fake: Path, dump_file: Path, version: str
) -> None:
    (fake / "state" / "version").write_text(version)
    done = run(RESTORE, fake, str(dump_file))
    assert done.returncode == 1 and "exactly one schema version" in done.stderr
    assert "--entrypoint sh migrate" not in calls(fake)


@pytest.mark.parametrize(
    ("switch", "message"),
    [
        ("fail_super", "cannot make the scratch databases"),
        ("fail_migrate", "could not build schema version 0011_interaction_counts_monthly"),
        ("fail_reference", "cannot write the reference through pg_dump and pg_restore"),
    ],
)
def test_a_reference_that_cannot_be_built_stops_the_restore_and_is_cleaned_up(
    fake: Path, dump_file: Path, switch: str, message: str
) -> None:
    (fake / "state" / switch).write_text("")
    done = run(RESTORE, fake, str(dump_file))
    assert done.returncode == 1 and message in done.stderr
    assert "matches the migrations" not in done.stdout
    assert "DROP DATABASE IF EXISTS pharos_restore_build" in calls(fake)


def test_an_empty_reference_is_never_called_a_match(fake: Path, dump_file: Path) -> None:
    (fake / "state" / "shape_reference").write_text("")
    (fake / "state" / "shape_restored").write_text("")
    done = run(RESTORE, fake, str(dump_file))
    assert done.returncode == 1 and "the reference is empty" in done.stderr


def test_an_extension_with_an_odd_name_is_refused(fake: Path, dump_file: Path) -> None:
    (fake / "state" / "extensions").write_text("vector\nx; DROP DATABASE pharos\n")
    done = run(RESTORE, fake, str(dump_file))
    assert done.returncode == 1 and "unexpected name" in done.stderr
    assert not (fake / "state" / "super.sql").exists()


# --- access lists and the directories above the dumps (F-44) -------------------------------------


def _default_acl_for_nobody(path: Path) -> None:
    """A default POSIX ACL that gives user 65534 everything (what `setfacl -d -m u:nobody:rwx`
    makes), written as the kernel's extended attribute so the test needs no setfacl."""
    import struct

    entries = [(0x01, 7, 0xFFFFFFFF), (0x02, 7, 65534), (0x04, 0, 0xFFFFFFFF)]
    entries += [(0x10, 7, 0xFFFFFFFF), (0x20, 0, 0xFFFFFFFF)]
    blob = struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *e) for e in entries)
    try:
        os.setxattr(path, "system.posix_acl_default", blob)
    except OSError as error:  # pragma: no cover - a file system without POSIX ACLs
        pytest.skip(f"this file system has no POSIX ACLs: {error}")


def test_a_directory_with_a_default_access_list_is_refused_although_its_mode_is_700(
    fake: Path,
) -> None:
    out = fake / "acl"
    out.mkdir()
    out.chmod(0o700)
    _default_acl_for_nobody(out)
    assert mode(out) == 0o700  # the mode alone says nothing is wrong
    done = run(BACKUP, fake, "run", "--dir", str(out), now="20261104T020000Z")
    assert done.returncode != 0 and "access control list" in done.stderr
    assert calls(fake) == "" and not list(out.iterdir())


@pytest.mark.parametrize("parent_mode", [0o777, 0o775, 0o757])
def test_a_directory_under_a_parent_others_can_write_to_is_refused(
    fake: Path, parent_mode: int
) -> None:
    parent = fake / "shared-parent"
    parent.mkdir()
    parent.chmod(parent_mode)
    done = run(BACKUP, fake, "run", "--dir", str(parent / "dumps"), now="20261104T020000Z")
    assert done.returncode != 0 and "is writable by others" in done.stderr
    assert calls(fake) == ""


def test_a_parent_with_the_sticky_bit_is_accepted_like_tmp(fake: Path) -> None:
    """The positive control of the test above: /tmp is 1777, and so may a parent be."""
    parent = fake / "sticky-parent"
    parent.mkdir()
    parent.chmod(0o1777)
    done = run(BACKUP, fake, "run", "--dir", str(parent / "dumps"), now="20261104T020000Z")
    assert done.returncode == 0, done.stderr


def test_every_query_on_the_restored_database_resolves_names_in_pg_catalog_only(
    fake: Path, dump_file: Path
) -> None:
    """A dump can make a schema named after the owner (first in its default search_path) with an
    operator that changes what the checks say; the checks must never look there (F-44 review)."""
    assert run(RESTORE, fake, str(dump_file)).returncode == 0
    owner_queries = [line for line in calls(fake).splitlines() if "--username=pharos_owner" in line]
    owner_queries = [line for line in owner_queries if "psql" in line]
    assert len(owner_queries) >= 8
    assert all("-e PGOPTIONS=-csearch_path=pg_catalog,pg_temp" in q for q in owner_queries)
    catalog = (ROOT / "scripts" / "restore" / "catalog.sql").read_text()
    first = next(line for line in catalog.splitlines() if line and not line.startswith("--"))
    assert first == "SET search_path = pg_catalog, pg_temp;"


def test_an_extension_pharos_does_not_make_or_one_the_owner_made_is_refused(
    fake: Path, dump_file: Path
) -> None:
    (fake / "state" / "odd_extensions").write_text("1\n")
    done = run(RESTORE, fake, str(dump_file))
    assert done.returncode == 1 and "an extension that Pharos does not make" in done.stderr
    assert not (fake / "state" / "super.sql").exists()


def test_the_scratch_databases_get_the_grants_of_the_real_one_and_are_dropped_by_force(
    fake: Path, dump_file: Path
) -> None:
    assert run(RESTORE, fake, str(dump_file)).returncode == 0
    made = (fake / "state" / "super.sql").read_text()
    for db in ("pharos_restore_build", "pharos_restore_reference"):
        assert f"REVOKE ALL ON DATABASE {db} FROM PUBLIC;" in made
        assert f"GRANT CONNECT ON DATABASE {db} TO pharos_app;" in made
        assert f"GRANT CONNECT ON DATABASE {db} TO pharos_worker;" in made
    assert "DROP DATABASE IF EXISTS pharos_restore_reference WITH (FORCE)" in calls(fake)


def test_why_the_migrations_failed_is_shown(fake: Path, dump_file: Path) -> None:
    (fake / "state" / "fail_migrate").write_text("")
    done = run(RESTORE, fake, str(dump_file))
    assert done.returncode == 1 and "  alembic: Can't locate revision" in done.stderr
