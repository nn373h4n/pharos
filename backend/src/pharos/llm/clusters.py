"""Operator commands for the clusters of near-duplicates (M4-05, ADR-0021). Run inside the stack, in
the worker's container (it holds the database role that may delete vectors and clear clusters):

    python -m pharos.llm.clusters status [--report]
    python -m pharos.llm.clusters reset --confirm [--model NAME]

`reset` deletes the vectors (of one model, or all) and clears every cluster of the organization, so
that the next sweeps embed and cluster the recent articles again. Do it after changing the embedding
model or the threshold, or to undo a wrong cluster. It costs the embedding calls of the recent
articles again. Nothing else is touched, and the commands print numbers only.

`status` also counts the vectors of a model other than the configured one: after a change of model
without a reset they are still there, and so are the clusters they made (F-42). `--report` prints
`name=value` lines for `scripts/status.sh`.
"""

import argparse
import asyncio
import re
import sys
from collections.abc import Sequence

from pharos.core.config import get_settings
from pharos.core.db import create_engine
from pharos.core.organization import OrganizationNotConfigured, OrganizationResolver
from pharos.llm import clustering

_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,99}$")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m pharos.llm.clusters", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    status = commands.add_parser("status", help="count the vectors and the clusters")
    status.add_argument("--report", action="store_true", help="name=value lines for status.sh")
    reset = commands.add_parser("reset", help="delete the vectors and clear the clusters")
    reset.add_argument("--confirm", action="store_true", help="really do it")
    reset.add_argument("--model", help="delete the vectors of this model only")
    return parser


async def execute(args: argparse.Namespace) -> str:
    if args.command == "reset":
        if not args.confirm:
            raise SystemExit("reset deletes vectors and clears clusters: add --confirm")
        if args.model is not None and not _MODEL.fullmatch(args.model):
            raise SystemExit("--model is not a model name")
    settings = get_settings()
    engine = create_engine(settings.database_url.get_secret_value())
    try:
        try:
            org = await OrganizationResolver(engine, settings.org_slug).resolve()
        except OrganizationNotConfigured:
            raise SystemExit("the organization of this installation does not exist") from None
        if args.command == "status":
            model = settings.embedding_model or None  # empty: embeddings are off
            counts = await clustering.status(engine, org, model=model)
            other = counts.get("other_models")
            if args.report:
                return "\n".join(
                    [
                        f"vectors={counts['vectors']}",
                        f"clusters={counts['clusters']}",
                        f"other_model_vectors={'off' if other is None else other}",
                    ]
                )
            line = (
                f"{counts['vectors']} vector(s); {counts['clustered']} article(s) in "
                f"{counts['clusters']} cluster(s); the largest has {counts['largest']}"
            )
            if other:
                line += (
                    f"\n{other} vector(s) of another model than the configured one: their "
                    "clusters are still in place. After a change of model, run "
                    "`python -m pharos.llm.clusters reset --confirm`."
                )
            return line
        deleted, cleared = await clustering.reset(engine, org, model=args.model)
        return (
            f"{deleted} vector(s) deleted, {cleared} article(s) left a cluster; the next sweeps "
            "embed and cluster the recent articles again"
        )
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    print(asyncio.run(execute(args)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
