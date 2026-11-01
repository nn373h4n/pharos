"""Operator commands of the evaluation (M4-09). Run inside the stack, in the worker's container
(it has the key); see docs/guides/evaluation.md.

    python -m pharos.evaluation export-corpus --professions accountant,hr_specialist --out c.json
    python -m pharos.evaluation run --corpus c.json --dir out --filter a --enrichment a --insight a
    python -m pharos.evaluation sheet --dir out --corpus c.json --language az
    python -m pharos.evaluation import --dir out sheet-az-anna.csv sheet-az-vusal.csv
    python -m pharos.evaluation report --dir out

A real run costs money (the cap is `--max-cost-usd`, five dollars unless you say otherwise). Nothing
here writes to the database.
"""

import argparse
import asyncio
import json
import math
import sys
import uuid
from collections.abc import Collection, Sequence
from decimal import Decimal
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from pharos.core.config import get_settings
from pharos.core.db import create_engine
from pharos.core.organization import OrganizationNotConfigured, OrganizationResolver
from pharos.evaluation import corpus as corpora
from pharos.evaluation import ratings as rating_files
from pharos.evaluation import report as reports
from pharos.evaluation import runner, sheet
from pharos.llm.factory import build_provider


class CliError(Exception):
    """A mistake the operator can fix; printed without a traceback."""


def _models(raw: str | None) -> list[str]:
    return [m.strip() for m in (raw or "").split(",") if m.strip()]


def _prices(raw: Sequence[str], known: Collection[str] | None = None) -> dict[str, tuple[int, int]]:
    """`model=IN:OUT` in US dollars per million tokens, which is the same number as micro-dollars
    per token. A price below one is rounded UP to one (the cost shown is then an upper bound), and
    a price of zero or less is refused: it would switch the cost cap off."""
    found: dict[str, tuple[int, int]] = {}
    for entry in raw:
        try:
            model, rest = entry.split("=", 1)
            low_text, high_text = rest.split(":", 1)
            low, high = Decimal(low_text), Decimal(high_text)
        except (ValueError, ArithmeticError):
            raise CliError(f"--price must look like model=IN:OUT, got {entry!r}") from None
        if not (low.is_finite() and high.is_finite()) or low <= 0 or high <= 0:
            raise CliError(f"--price for {model!r} must be above zero")
        if known is not None and model not in known:
            raise CliError(f"--price names {model!r}, which is not in the plan")
        found[model] = (math.ceil(low), math.ceil(high))
    return found


def _not_over(path: Path, *, force: bool) -> None:
    if path.exists() and not force:
        raise CliError(f"{path} exists; use --force to replace it")


_FIT = (
    "SELECT i.id, i.title, i.excerpt, i.canonical_url, i.lang "
    "FROM items i JOIN item_professions p ON p.org_id = i.org_id "
    "AND p.item_id = i.id AND p.profession_code = :code "
    "WHERE i.org_id = :org AND i.owner_user_id IS NULL AND i.status = 'new' "
    "AND i.enriched_at IS NOT NULL ORDER BY p.relevance DESC, i.id LIMIT :n"
)
_NO_FIT = (
    "SELECT i.id, i.title, i.excerpt, i.canonical_url, i.lang FROM items i "
    "WHERE i.org_id = :org AND i.owner_user_id IS NULL AND i.status = 'new' "
    "AND i.enriched_at IS NOT NULL AND NOT EXISTS (SELECT 1 FROM item_professions p "
    "WHERE p.org_id = i.org_id AND p.item_id = i.id AND p.profession_code = ANY(:codes)) "
    "ORDER BY i.id LIMIT :n"
)


async def collect(
    connection: AsyncConnection,
    org: uuid.UUID,
    professions: list[str],
    per_profession: int,
    negatives: int,
) -> list[corpora.CorpusItem]:
    """Public, current, enriched articles of the organization: the best fits of each profession,
    and some that fit none (likely negatives). The labels are left empty on purpose: the filter is
    judged against what a person decides, not against what the product's own models said."""
    await connection.execute(text("SELECT set_config('app.org_id', :org, true)"), {"org": str(org)})
    entries: list[corpora.CorpusItem] = []
    seen: set[uuid.UUID] = set()

    def add(row: object, profession: str, prefix: str) -> None:
        lang = getattr(row, "lang")  # noqa: B009 (a database row)
        entries.append(
            corpora.CorpusItem(
                f"{prefix[:12]}-{len(entries) + 1}",
                getattr(row, "title"),  # noqa: B009
                getattr(row, "excerpt"),  # noqa: B009
                getattr(row, "canonical_url"),  # noqa: B009
                lang if lang in corpora.LANGUAGES else None,
                profession,
                None,
            )
        )

    for code in professions:
        for row in await connection.execute(
            text(_FIT), {"org": org, "code": code, "n": per_profession}
        ):
            if row.id not in seen:
                seen.add(row.id)
                add(row, code, code)
    if negatives and professions:
        for row in await connection.execute(
            text(_NO_FIT), {"org": org, "codes": professions, "n": negatives}
        ):
            if row.id not in seen:
                seen.add(row.id)
                add(row, professions[0], "neg")
    return entries


async def export_corpus(
    professions: list[str], per_profession: int, negatives: int, out: Path
) -> int:
    settings = get_settings()
    engine = create_engine(settings.database_url.get_secret_value())
    try:
        try:
            org = await OrganizationResolver(engine, settings.org_slug).resolve()
        except OrganizationNotConfigured:
            raise CliError("the organization of this installation does not exist") from None
        async with engine.begin() as connection:
            entries = await collect(connection, org, professions, per_profession, negatives)
    finally:
        await engine.dispose()
    if not entries:
        raise CliError("no current, enriched articles were found for these professions")
    corpora.dump(entries, out)
    return len(entries)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m pharos.evaluation", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    export = commands.add_parser("export-corpus", help="write a corpus from the public articles")
    export.add_argument("--professions", required=True, help="catalogue codes, comma separated")
    export.add_argument("--per-profession", type=int, default=30)
    export.add_argument("--negatives", type=int, default=10)
    export.add_argument("--out", type=Path, required=True)
    export.add_argument("--force", action="store_true", help="replace an existing corpus file")

    run = commands.add_parser("run", help="run the candidate models (this costs money)")
    run.add_argument("--corpus", type=Path, required=True)
    run.add_argument("--dir", type=Path, required=True)
    run.add_argument("--languages", default="az,tr,ru,en")
    run.add_argument("--filter", dest="filter_models")
    run.add_argument("--enrichment", dest="enrichment_models")
    run.add_argument("--insight", dest="insight_models")
    run.add_argument("--max-cost-usd", type=float, default=5.0)
    run.add_argument("--price", action="append", default=[], help="model=IN:OUT, USD per million")
    run.add_argument("--force", action="store_true", help="replace the answers of an earlier run")

    make = commands.add_parser("sheet", help="write the blind rating sheet of one language")
    make.add_argument("--dir", type=Path, required=True)
    make.add_argument("--corpus", type=Path, required=True)
    make.add_argument("--language", required=True)
    make.add_argument(
        "--out",
        type=Path,
        required=True,
        help="where to write it: NOT inside --dir (which unblinds)",
    )
    make.add_argument("--force", action="store_true")

    load = commands.add_parser("import", help="import the filled-in sheets, all at once")
    load.add_argument("--dir", type=Path, required=True)
    load.add_argument("sheets", nargs="+", type=Path)

    show = commands.add_parser("report", help="count the ratings and recommend models")
    show.add_argument("--dir", type=Path, required=True)
    show.add_argument("--out", type=Path)
    show.add_argument("--force", action="store_true")
    defaults = reports.Bar()
    show.add_argument("--min-mean", type=float, default=defaults.mean)
    show.add_argument("--min-safe", type=float, default=defaults.safe_share)
    show.add_argument("--min-checked", type=float, default=defaults.checked_share)
    show.add_argument("--min-filter-accuracy", type=float, default=defaults.filter_accuracy)
    show.add_argument("--min-items", type=int, default=defaults.min_items)
    show.add_argument("--min-raters", type=int, default=defaults.min_raters)
    return parser


async def execute(args: argparse.Namespace) -> str:
    if args.command == "export-corpus":
        _not_over(args.out, force=args.force)
        count = await export_corpus(
            _models(args.professions), args.per_profession, args.negatives, args.out
        )
        return (
            f"wrote {count} article(s) to {args.out}. Open it and set `relevant` to false for "
            "every article that is not about practical use of AI at work."
        )
    if args.command == "run":
        settings = get_settings()
        provider = build_provider(settings)
        if provider is None:
            raise CliError("the model layer is off (PHAROS_LLM_PROVIDER)")
        items = corpora.load(args.corpus)
        corpora.require_labels(items)
        plan = runner.Plan(
            {
                "filter": _models(args.filter_models),
                "enrichment": _models(args.enrichment_models),
                "insight": _models(args.insight_models),
            }
        )
        named = {model for models in plan.models.values() for model in models}
        if not named:
            raise CliError("name at least one model with --filter, --enrichment or --insight")
        prices = _prices(args.price, named)
        answers_file = args.dir / "answers.jsonl"
        _not_over(answers_file, force=args.force)
        args.dir.mkdir(parents=True, exist_ok=True)
        # The ratings belong to the codes of the old run.
        (args.dir / "ratings.json").unlink(missing_ok=True)
        with answers_file.open("w", encoding="utf-8") as handle:

            def keep(answer: dict[str, object]) -> None:
                handle.write(json.dumps(answer, ensure_ascii=False) + "\n")
                handle.flush()

            result = await runner.run(
                items,
                plan,
                _models(args.languages),
                provider,
                provider_name=settings.llm_provider,
                max_cost_micro_usd=int(args.max_cost_usd * 1_000_000),
                prices=prices,
                progress=lambda line: print(line, file=sys.stderr),
                on_answer=keep,
            )
        (args.dir / "run.json").write_text(
            json.dumps(result.meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        spent = result.meta["spent_micro_usd"] / 1_000_000
        note = " The run stopped at its cap." if result.meta["incomplete"] else ""
        return (
            f"{len(result.answers)} answer(s) kept in {args.dir}; about ${spent:.2f} spent.{note}"
        )
    if args.command == "sheet":
        result = runner.read(args.dir)
        items = corpora.load(args.corpus)
        table = sheet.rows(result.answers, items, args.language)
        if not table:
            raise CliError(f"no written answers in {args.language!r}")
        out = args.out.resolve()
        if args.dir.resolve() in out.parents:
            raise CliError("write the sheet outside --dir: the files there name the models")
        _not_over(out, force=args.force)
        sheet.write(table, out)
        return (
            f"{len(table)} row(s) written to {out}; give raters this file and nothing from "
            f"{args.dir}"
        )
    if args.command == "import":
        result = runner.read(args.dir)
        found = rating_files.parse(args.sheets, {a["code"] for a in result.answers})
        rating_files.save(found, args.dir)
        raters = len({r.rater.casefold() for r in found})
        return f"{len(found)} rating(s) from {raters} rater(s) imported"
    if args.command == "report":
        result = runner.read(args.dir)
        bar = reports.Bar(
            mean=args.min_mean,
            safe_share=args.min_safe,
            checked_share=args.min_checked,
            filter_accuracy=args.min_filter_accuracy,
            min_items=args.min_items,
            min_raters=args.min_raters,
        )
        try:
            found = rating_files.load(args.dir)
        except OSError:
            found = []
        built = reports.build(result.answers, result.meta, found, bar)
        if args.out:
            _not_over(args.out, force=args.force)
            args.out.write_text(built.markdown, encoding="utf-8")
        return built.markdown
    raise CliError(f"unknown command {args.command!r}")  # pragma: no cover


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        print(asyncio.run(execute(args)))
    except ValidationError as error:
        # The names of the settings that are wrong, never their values (some are secrets).
        fields = sorted({".".join(str(part) for part in e["loc"]) for e in error.errors()})
        print(f"error: the settings are not valid: {', '.join(fields)}", file=sys.stderr)
        return 1
    except (CliError, corpora.CorpusError, rating_files.RatingsError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
