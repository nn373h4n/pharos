"""Run, sheet, import, report: the whole path with a scripted provider and no network."""

import csv
from pathlib import Path

import pytest

from pharos.evaluation import ratings as rating_files
from pharos.evaluation import report as reports
from pharos.evaluation import runner, sheet

from .support import corpus, provider

pytestmark = pytest.mark.anyio

PRICES = {"good": (10, 10), "cheap": (1, 1), "leaky": (1, 1), "liar": (1, 1), "rude": (1, 1)}


async def do_run(models: dict[str, list[str]], **changes: object) -> runner.RunResult:
    arguments: dict[str, object] = {
        "provider_name": "anthropic",
        "max_cost_micro_usd": 10**9,
        "prices": PRICES,
    }
    arguments.update(changes)
    return await runner.run(
        corpus(),
        runner.Plan(models),
        ["az", "en"],
        provider(),
        **arguments,  # type: ignore[arg-type]
    )


async def test_every_model_item_and_language_gets_one_answer_with_the_products_own_checks() -> None:
    result = await do_run(
        {"filter": ["good"], "enrichment": ["good"], "insight": ["good", "leaky"]}
    )
    stages = [(a["stage"], a["model"]) for a in result.answers]
    assert stages.count(("filter", "good")) == 4  # once per article
    assert stages.count(("enrichment", "good")) == 8  # per article and language
    assert stages.count(("insight", "leaky")) == 8
    good = [a for a in result.answers if a["stage"] == "insight" and a["model"] == "good"]
    leaky = [a for a in result.answers if a["stage"] == "insight" and a["model"] == "leaky"]
    assert all(a["checked"] for a in good) and not any(a["checked"] for a in leaky)
    assert all(a["checked"] for a in result.answers if a["stage"] == "enrichment")
    codes = [a["code"] for a in result.answers]
    assert len(set(codes)) == len(codes)
    assert result.meta["prompt_versions"]["insight"] == "v2" and result.meta["incomplete"] is False


async def test_the_filter_is_judged_against_the_human_labels() -> None:
    result = await do_run({"filter": ["good", "liar"]})
    right = {m: [a["correct"] for a in result.answers if a["model"] == m] for m in ("good", "liar")}
    assert all(right["good"])  # relevant exactly for the even articles
    assert right["liar"] == [True, False, True, False]  # says relevant for everything


async def test_a_failure_is_a_result_not_a_reason_to_stop() -> None:
    result = await do_run({"insight": ["rude", "good"]})
    rude = [a for a in result.answers if a["model"] == "rude"]
    assert len(rude) == 8 and all(a["failed"] and a["output"] is None for a in rude)
    assert all(a["failed"] is None for a in result.answers if a["model"] == "good")


async def test_a_call_that_fails_is_not_retried_behind_the_operators_back() -> None:
    from pharos.llm.errors import ProviderFailed
    from pharos.llm.provider import FakeProvider, ProviderAnswer

    calls: list[int] = []

    def flaky(model: str, system: str, user: str) -> ProviderAnswer:
        calls.append(1)
        if len(calls) == 1:
            raise ProviderFailed(
                sent=False
            )  # a provider hiccup the gateway would retry in the worker
        return provider().script(model, system, user)  # type: ignore[misc, return-value]

    result = await runner.run(
        corpus(2), runner.Plan({"insight": ["good"]}), ["en"], FakeProvider(script=flaky),
        provider_name="x", max_cost_micro_usd=10**9, prices=PRICES,
    )  # fmt: skip
    assert len(calls) == 2 == len(result.answers)  # one call per answer, no second try
    assert [bool(a["failed"]) for a in result.answers] == [True, False]


async def test_the_cost_cap_stops_the_run_and_says_so() -> None:
    result = await do_run({"insight": ["good"]}, max_cost_micro_usd=20000)
    assert result.meta["incomplete"] is True
    assert 0 < len(result.answers) < 8
    assert result.meta["spent_micro_usd"] <= 20000


async def test_costs_are_kept_per_stage_and_model(tmp_path: Path) -> None:
    result = await do_run({"insight": ["good", "cheap"]})
    costs = result.meta["costs_micro_usd"]
    assert costs["insight:good"] > costs["insight:cheap"] > 0
    runner.write(result, tmp_path)
    again = runner.read(tmp_path)
    assert again.answers == result.answers and again.meta == result.meta


async def test_a_plan_with_a_repeated_or_unknown_model_is_refused() -> None:
    with pytest.raises(ValueError):
        runner.Plan({"insight": ["a", "a"]})
    with pytest.raises(ValueError):
        runner.Plan({"translation": ["a"]})
    with pytest.raises(ValueError):
        await runner.run(corpus(), runner.Plan({"insight": ["a"]}), ["fr"], provider(),
                         provider_name="x", max_cost_micro_usd=1)  # fmt: skip


# --- the sheet ---


async def test_the_sheet_is_blind_shuffled_and_safe_to_open_in_a_spreadsheet(
    tmp_path: Path,
) -> None:
    items = corpus()
    items[0] = items[0].__class__(
        "a0", "=HYPERLINK(\"http://evil.example\")", "+cmd", "https://news.example.org/0", "en",
        "accountant", True,
    )  # fmt: skip
    result = await runner.run(
        items, runner.Plan({"enrichment": ["good"], "insight": ["good", "leaky"]}), ["az"],
        provider(), provider_name="x", max_cost_micro_usd=10**9, prices=PRICES,
    )  # fmt: skip
    table = sheet.rows(result.answers, items, "az", seed=1)
    path = tmp_path / "sheet.csv"
    sheet.write(table, path)
    text = path.read_text(encoding="utf-8-sig")
    for name in ("good", "leaky", "cheap", "filter", "model"):
        assert name not in text.split("\n")[0]  # not a column
    assert "leaky" not in text and "anthropic" not in text  # not anywhere in the file
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")  # a byte-order mark for spreadsheets
    rows = read_rows(path)
    assert len(rows) == 12  # 4 articles x (1 enrichment + 2 insights) in one language
    assert {r["code"] for r in rows} == {a["code"] for a in result.answers if a["language"] == "az"}
    assert all(not r["rater"] and not r["fluency"] for r in rows)
    titles = {r["article"] for r in rows}
    assert '\'=HYPERLINK("http://evil.example")' in titles  # a formula is made harmless
    assert all(not r["excerpt"].startswith(("=", "+", "-", "@")) for r in rows)
    other = sheet.rows(result.answers, items, "az", seed=2)
    assert [r[0] for r in other] != [r[0] for r in table]  # the order is not fixed


async def test_the_sheet_leaves_out_answers_that_failed_and_the_filter() -> None:
    result = await do_run({"filter": ["good"], "insight": ["rude", "good"]})
    table = sheet.rows(result.answers, corpus(), "en", seed=1)
    assert len(table) == 4  # only the good insights: no filter rows, no failed rows


# --- the import ---


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def fill(path: Path, rater: str, marks: tuple[str, str, str, str] = ("5", "4", "4", "yes")) -> None:
    rows = read_rows(path)
    for row in rows:
        row.update(
            rater=rater, fluency=marks[0], accuracy=marks[1], usefulness=marks[2], safe=marks[3]
        )
        row["comment"] = "SECRET-COMMENT"
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=sheet.COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


async def make_sheet(
    tmp_path: Path, name: str = "s.csv"
) -> tuple[Path, set[str], runner.RunResult]:
    result = await do_run({"insight": ["good"]})
    path = tmp_path / name
    sheet.write(sheet.rows(result.answers, corpus(), "az", seed=3), path)
    return path, {a["code"] for a in result.answers}, result


async def test_ratings_are_imported_strictly_and_comments_are_never_kept(tmp_path: Path) -> None:
    path, codes, _ = await make_sheet(tmp_path)
    fill(path, "Anna")
    found = rating_files.parse([path], codes)
    assert len(found) == 4 and all(r.safe and r.fluency == 5 for r in found)
    rating_files.save(found, tmp_path)
    assert "SECRET-COMMENT" not in (tmp_path / "ratings.json").read_text()
    assert rating_files.load(tmp_path) == found


@pytest.mark.parametrize(
    ("marks", "rater", "message"),
    [
        (("6", "4", "4", "yes"), "Anna", "1 to 5"),
        (("0", "4", "4", "yes"), "Anna", "1 to 5"),
        (("x", "4", "4", "yes"), "Anna", "1 to 5"),
        (("", "4", "4", "yes"), "Anna", "1 to 5"),
        (("4", "4", "4", "maybe"), "Anna", "yes or no"),
        (("4", "4", "4", ""), "Anna", "yes or no"),
        (("4", "4", "4", "yes"), "", "rater"),
        (("4", "4", "4", "yes"), "N" * 41, "rater"),
    ],
)
async def test_a_bad_mark_names_its_row_and_nothing_is_imported(
    tmp_path: Path, marks: tuple[str, str, str, str], rater: str, message: str
) -> None:
    path, codes, _ = await make_sheet(tmp_path)
    fill(path, rater, marks)
    with pytest.raises(rating_files.RatingsError) as error:
        rating_files.parse([path], codes)
    assert message in str(error.value) and "row 2" in str(error.value)
    assert not (tmp_path / "ratings.json").exists()


async def test_unknown_codes_duplicates_and_a_sheet_that_names_a_model_are_refused(
    tmp_path: Path,
) -> None:
    path, codes, _ = await make_sheet(tmp_path)
    fill(path, "Anna")
    with pytest.raises(rating_files.RatingsError, match="unknown code"):
        rating_files.parse([path], set())
    twin = tmp_path / "twin.csv"
    twin.write_bytes(path.read_bytes())
    with pytest.raises(rating_files.RatingsError, match="already rated"):
        rating_files.parse([path, twin], codes)  # the same rater, the same codes
    leaked = tmp_path / "leaked.csv"
    leaked.write_text("code,model,rater\nx,gpt,a\n", encoding="utf-8")
    with pytest.raises(rating_files.RatingsError, match="not blind"):
        rating_files.parse([leaked], codes)
    broken = tmp_path / "broken.csv"
    broken.write_text("code,rater\nx,a\n", encoding="utf-8")
    with pytest.raises(rating_files.RatingsError, match="columns missing"):
        rating_files.parse([broken], codes)


async def test_a_sheet_nobody_filled_in_is_not_an_import(tmp_path: Path) -> None:
    path, codes, _ = await make_sheet(tmp_path)
    with pytest.raises(rating_files.RatingsError, match="no ratings"):
        rating_files.parse([path], codes)


async def test_one_bad_sheet_stops_the_import_of_the_good_one(tmp_path: Path) -> None:
    good, codes, _ = await make_sheet(tmp_path, "good.csv")
    bad, _, _ = await make_sheet(tmp_path, "bad.csv")
    fill(good, "Anna")
    fill(bad, "Vusal", ("9", "9", "9", "yes"))
    with pytest.raises(rating_files.RatingsError):
        rating_files.parse([good, bad], codes)


# --- the whole path ---


async def test_the_first_real_run_picks_the_cheapest_model_that_clears_the_bar(
    tmp_path: Path,
) -> None:
    result = await do_run(
        {"filter": ["good", "liar"], "enrichment": ["good"], "insight": ["good", "cheap", "leaky"]}
    )
    runner.write(result, tmp_path)
    table = sheet.rows(result.answers, corpus(), "az", seed=5)
    path = tmp_path / "az.csv"
    sheet.write(table, path)
    fill(path, "Anna")
    second = tmp_path / "az2.csv"
    second.write_bytes(path.read_bytes())
    fill(second, "Vusal", ("5", "5", "4", "yes"))
    rating_files.save(
        rating_files.parse([path, second], {a["code"] for a in result.answers}), tmp_path
    )
    # English was run too but nobody rated it: the bar needs raters in every language, so lower it.
    only_az = [a for a in result.answers if a["language"] in (None, "az")]
    meta = {**result.meta, "languages": ["az"]}
    built = reports.build(
        only_az, meta, rating_files.load(tmp_path), reports.Bar(min_items=2, min_raters=2)
    )
    assert built.recommendations == {"filter": "good", "enrichment": "good", "insight": "cheap"}
    assert "PHAROS_LLM_MODEL_INSIGHT=cheap" in built.markdown
    assert built.failures[("insight", "leaky")]  # it fails the product's own checks
    assert built.failures[("filter", "liar")]


# --- nothing is written to a database ---


def test_the_harness_touches_no_database_and_no_redis() -> None:
    import ast

    import pharos.evaluation.runner as runner_module

    root = Path(runner_module.__file__).parent
    for name in ("corpus.py", "ratings.py", "report.py", "runner.py", "sheet.py"):
        imported = {
            part
            for node in ast.walk(ast.parse((root / name).read_text()))
            for part in (
                [alias.name.split(".")[0] for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module.split(".")[0]]
                if isinstance(node, ast.ImportFrom) and node.module
                else []
            )
        }
        assert not imported & {"sqlalchemy", "redis", "asyncpg"}, name


def test_a_model_name_with_a_newline_or_odd_characters_is_refused() -> None:
    for bad in ["a\nPHAROS_LLM_KEY=x", "a b", "a;b", "x" * 101]:
        with pytest.raises(ValueError):
            runner.Plan({"insight": [bad]})
    runner.Plan({"insight": ["claude-sonnet-5-5", "ollama/qwen2.5:7b"]})  # positive control
