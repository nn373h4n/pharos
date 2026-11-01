"""The commands of `python -m pharos.evaluation` that need no provider and no database."""

import asyncio
import csv
from pathlib import Path

import pytest

from pharos.evaluation import __main__ as cli
from pharos.evaluation import corpus as corpora
from pharos.evaluation import runner

from .support import corpus, provider


async def make(tmp_path: Path) -> Path:
    items = corpus()
    corpora.dump(items, tmp_path / "corpus.json")
    result = await runner.run(
        items, runner.Plan({"insight": ["good"]}), ["az"], provider(),
        provider_name="anthropic", max_cost_micro_usd=10**9, prices={"good": (10, 10)},
    )  # fmt: skip
    runner.write(result, tmp_path / "eval")
    return tmp_path


def prepared(tmp_path: Path) -> Path:
    return asyncio.run(make(tmp_path))


def test_sheet_import_and_report_work_from_the_command_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = prepared(tmp_path)
    work, corpus_file = base / "eval", base / "corpus.json"
    assert (
        cli.main(
            [
                "sheet",
                "--dir",
                str(work),
                "--corpus",
                str(corpus_file),
                "--language",
                "az",
                "--out",
                str(base / "sheet-az.csv"),
            ]
        )
        == 0
    )
    sheet_file = base / "sheet-az.csv"
    with sheet_file.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 4
    for row in rows:
        row.update(rater="Anna", fluency="5", accuracy="5", usefulness="5", safe="yes")
    with sheet_file.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    assert cli.main(["import", "--dir", str(work), str(sheet_file)]) == 0
    out = base / "report.md"
    code = cli.main(
        ["report", "--dir", str(work), "--min-items", "2", "--min-raters", "1", "--out", str(out)]
    )
    assert code == 0
    assert "PHAROS_LLM_MODEL_INSIGHT=good" in out.read_text(encoding="utf-8")
    assert "Recommended: `good`" in capsys.readouterr().out


def test_a_mistake_is_a_message_and_a_status_not_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = prepared(tmp_path)
    bad = base / "bad.csv"
    bad.write_text("code,rater\nx,a\n", encoding="utf-8")
    assert cli.main(["import", "--dir", str(base / "eval"), str(bad)]) == 1
    assert "error:" in capsys.readouterr().err
    assert (
        cli.main(
            [
                "sheet",
                "--dir",
                str(base / "eval"),
                "--corpus",
                str(base / "corpus.json"),
                "--language",
                "tr",
                "--out",
                str(base / "sheet-tr.csv"),
            ]
        )
        == 1
    )
    (base / "empty.json").write_text("[]", encoding="utf-8")
    assert (
        cli.main(
            [
                "sheet",
                "--dir",
                str(base / "eval"),
                "--corpus",
                str(base / "empty.json"),
                "--language",
                "az",
                "--out",
                str(base / "sheet-az.csv"),
            ]
        )
        == 1
    )


def test_prices_are_read_strictly() -> None:
    assert cli._prices(["a=3:15", "b-2=1:2"]) == {"a": (3, 15), "b-2": (1, 2)}
    for bad in ("a", "a=3", "a=x:y", "a=3:"):
        with pytest.raises(cli.CliError):
            cli._prices([bad])


def test_export_corpus_does_not_overwrite_a_file_without_force(tmp_path: Path) -> None:
    existing = tmp_path / "c.json"
    existing.write_text("[]", encoding="utf-8")
    with pytest.raises(cli.CliError, match="--force"):
        asyncio.run(
            cli.execute(
                cli.build_parser().parse_args(
                    ["export-corpus", "--professions", "accountant", "--out", str(existing)]
                )
            )
        )
    assert existing.read_text(encoding="utf-8") == "[]"
