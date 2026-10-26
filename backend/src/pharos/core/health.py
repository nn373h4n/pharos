"""Liveness and readiness endpoints.

`/healthz` says the process is up and touches nothing else. `/readyz` checks the database, Redis
and the migration version. Failures are logged; the response only says which check failed.
"""

import logging
from pathlib import Path
from typing import Literal, Protocol

from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import APIRouter, Request, Response
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)

CheckResult = Literal["ok", "fail"]


class LivenessResponse(BaseModel):
    status: Literal["ok"]


class ReadinessResponse(BaseModel):
    status: Literal["ok", "unavailable"]
    checks: dict[str, CheckResult]


class ReadinessChecker(Protocol):
    async def check(self) -> ReadinessResponse: ...


def expected_migration_heads(alembic_dir: Path) -> frozenset[str]:
    config = Config()
    config.set_main_option("script_location", str(alembic_dir))
    return frozenset(ScriptDirectory.from_config(config).get_heads())


class Readiness:
    def __init__(self, engine: AsyncEngine, redis: Redis, expected_heads: frozenset[str]) -> None:
        self._engine = engine
        self._redis = redis
        self._expected_heads = expected_heads

    async def _database(self) -> CheckResult:
        try:
            async with self._engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except Exception:
            logger.exception("readiness: database check failed")
            return "fail"
        return "ok"

    async def _database_role(self) -> CheckResult:
        """RLS only protects if the application role can neither bypass it nor own the tables."""
        try:
            async with self._engine.connect() as connection:
                row = (
                    await connection.execute(
                        text(
                            "SELECT (rolsuper OR rolbypassrls) AS privileged, "
                            "EXISTS (SELECT 1 FROM pg_tables WHERE schemaname = 'public' "
                            "AND tableowner = current_user) AS owns_tables "
                            "FROM pg_roles WHERE rolname = current_user"
                        )
                    )
                ).one()
        except Exception:
            logger.exception("readiness: database role check failed")
            return "fail"
        if row.privileged or row.owns_tables:
            logger.error(
                "readiness: application role can bypass row-level security",
                extra={"privileged": row.privileged, "owns_tables": row.owns_tables},
            )
            return "fail"
        return "ok"

    async def _redis_ping(self) -> CheckResult:
        try:
            await self._redis.ping()  # type: ignore[misc]
        except Exception:
            logger.exception("readiness: redis check failed")
            return "fail"
        return "ok"

    async def _migrations(self) -> CheckResult:
        try:
            async with self._engine.connect() as connection:
                rows = await connection.execute(text("SELECT version_num FROM alembic_version"))
                current = frozenset(row[0] for row in rows)
        except Exception:
            logger.exception("readiness: migration check failed")
            return "fail"
        if current != self._expected_heads:
            logger.error(
                "readiness: migration version mismatch",
                extra={"current": sorted(current), "expected": sorted(self._expected_heads)},
            )
            return "fail"
        return "ok"

    async def check(self) -> ReadinessResponse:
        checks: dict[str, CheckResult] = {
            "database": await self._database(),
            "database_role": await self._database_role(),
            "redis": await self._redis_ping(),
            "migrations": await self._migrations(),
        }
        ok = all(result == "ok" for result in checks.values())
        return ReadinessResponse(status="ok" if ok else "unavailable", checks=checks)


router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> LivenessResponse:
    return LivenessResponse(status="ok")


@router.get("/readyz", responses={503: {"model": ReadinessResponse}})
async def readyz(request: Request, response: Response) -> ReadinessResponse:
    checker: ReadinessChecker = request.app.state.readiness
    report = await checker.check()
    if report.status != "ok":
        response.status_code = 503
    return report
