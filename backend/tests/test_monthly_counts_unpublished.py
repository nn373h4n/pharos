"""Guard against anything but the operator command mentioning `interaction_counts_monthly` (F-47).

Next to the daily summary, a month's count would reveal a group smaller than 5 by subtraction. The
table is written by the operator command and read by nobody; this test fails when any other file
of the repository mentions it, so whoever starts to read it has to solve F-47 first. It is a guard
against a future change, not proof that nothing leaks (an administrator who noted the daily counts
earlier could still subtract them from a monthly row that is shown to them).

Docs (`docs/` and the Markdown files at the top, such as TASKS.md and CHANGELOG.md) and the tests
are exempt: they describe the table and check this very rule.
"""

from pathlib import Path

TABLE = "interaction_counts_monthly"
ROOT = Path(__file__).resolve().parents[2]
THIS = Path(__file__).resolve()
ALLOWED = {
    "backend/src/pharos/retention.py",
    "backend/alembic/versions/0011_interaction_counts_monthly.py",
}
SKIPPED_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    ".next",
    "test-results",
    "playwright-report",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "dist",
    "build",
    "coverage",
    ".turbo",
}
# Relative to the root: the descriptions and the tests of this rule.
EXEMPT_PREFIXES = ("docs/", "backend/tests/")
# Each of these must be visited, so a broken walk cannot pass.
MUST_VISIT = (
    "backend/alembic/versions/",
    "backend/src/",
    "frontend/src/",
    "scripts/",
    "deploy/",
)


def _scan() -> tuple[set[str], set[str]]:
    """Return (files visited, files that mention the table)."""
    visited: set[str] = set()
    found: set[str] = set()
    stack = [ROOT]
    while stack:
        folder = stack.pop()
        for path in folder.iterdir():
            if path.is_symlink():
                continue
            relative = path.relative_to(ROOT).as_posix()
            if path.is_dir():
                if path.name not in SKIPPED_DIRS and not f"{relative}/".startswith(EXEMPT_PREFIXES):
                    stack.append(path)
                continue
            top_level_doc = path.parent == ROOT and path.suffix == ".md"
            if path.resolve() == THIS or top_level_doc or relative.startswith(EXEMPT_PREFIXES):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            visited.add(relative)
            if TABLE in text:
                found.add(relative)
    return visited, found


def test_the_scan_finds_the_table_where_it_is_written() -> None:
    """Positive control: a scan that cannot find anything would pass the next test."""
    _, found = _scan()
    assert "backend/src/pharos/retention.py" in found
    assert "backend/alembic/versions/0011_interaction_counts_monthly.py" in found


def test_the_scan_visits_every_part_of_the_repository_that_matters() -> None:
    visited, _ = _scan()
    for prefix in MUST_VISIT:
        assert any(name.startswith(prefix) for name in visited), (
            f"the scan visited no file under {prefix}: the walk is broken"
        )


def test_only_the_operator_command_and_its_migration_mention_the_monthly_table() -> None:
    _, found = _scan()
    stray = sorted(found - ALLOWED)
    assert not stray, (
        f"{', '.join(stray)} mention(s) {TABLE}: the monthly counts must not be published next "
        "to the daily summary, a group smaller than 5 would show by subtraction (F-47)"
    )
