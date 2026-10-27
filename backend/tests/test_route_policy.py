"""The gate itself: it passes for the real application and fails for each way of cheating."""

from fastapi import Depends, FastAPI

from pharos.core.auth import CurrentPrincipal, require_role
from pharos.core.config import Settings
from pharos.core.principal import Principal, Role
from pharos.main import create_app

from .constants import FAKE_SECRET_KEY
from .route_policy import PUBLIC_ROUTES, ROLE_MATRIX, Rule, find_problems, registered_routes

THIS_TEST = "test_route_policy.py::test_the_real_application_passes_the_gate"


def build(environment: str = "test") -> FastAPI:
    return create_app(
        Settings(
            environment=environment,  # type: ignore[arg-type]
            database_url="postgresql+asyncpg://user:Zx9-example-only@db:5432/pharos",  # type: ignore[arg-type]
            redis_url="redis://:Qw7-example-only@cache:6379/0",  # type: ignore[arg-type]
            public_url="https://pharos.example",
            secret_key=FAKE_SECRET_KEY,  # type: ignore[arg-type]
        )
    )


def test_the_real_application_passes_the_gate() -> None:
    assert find_problems(build("production")) == []


def test_development_adds_only_the_documentation_routes() -> None:
    extra = set(registered_routes(build("development"))) - set(
        registered_routes(build("production"))
    )
    assert {path for _, path in extra} == {
        "/api/docs",
        "/docs/oauth2-redirect",  # outside /api, so the proxy never routes it here
        "/api/openapi.json",
    }


def test_an_unlisted_route_fails_the_gate() -> None:
    app = build("production")

    @app.get("/api/forgotten")
    async def forgotten() -> dict[str, str]:
        return {}

    assert find_problems(app) == ["GET /api/forgotten is neither public nor in the role matrix"]


def test_a_route_that_is_not_authenticated_cannot_hide_in_the_matrix() -> None:
    app = build("production")

    @app.get("/api/secret")
    async def secret() -> dict[str, str]:
        return {}

    matrix = {("GET", "/api/secret"): Rule(frozenset({Role.ADMIN}), f"{THIS_TEST}")}
    assert find_problems(app, matrix=matrix) == [
        "GET /api/secret is in the role matrix but does not require sign-in"
    ]


def test_an_authenticated_route_needs_roles_and_a_real_cross_tenant_test() -> None:
    app = build("production")

    @app.get("/api/mine")
    async def mine(principal: CurrentPrincipal) -> dict[str, str]:
        return {}

    key = ("GET", "/api/mine")
    ok = {key: Rule(frozenset({Role.MEMBER}), THIS_TEST)}
    assert find_problems(app, matrix=ok) == []
    assert find_problems(app, matrix={key: Rule(frozenset(), THIS_TEST)}) == [
        "GET /api/mine has no roles in the matrix"
    ]
    for missing in (
        "nope.py::test_x",
        "test_route_policy.py::test_not_there",
        "test_route_policy.py",
    ):
        problems = find_problems(app, matrix={key: Rule(frozenset({Role.MEMBER}), missing)})
        assert len(problems) == 1
        assert "cross-tenant test" in problems[0]


def test_role_checked_routes_count_as_authenticated() -> None:
    app = build("production")

    @app.get("/api/admin-only")
    async def admin_only(principal: Principal = Depends(require_role(Role.ADMIN))) -> None:  # noqa: B008
        return None

    matrix = {("GET", "/api/admin-only"): Rule(frozenset({Role.ADMIN}), THIS_TEST)}
    assert find_problems(app, matrix=matrix) == []


def test_a_public_route_cannot_secretly_require_sign_in_or_be_stale() -> None:
    app = build("production")

    @app.get("/api/pub")
    async def pub(principal: CurrentPrincipal) -> None:
        return None

    public = PUBLIC_ROUTES | {("GET", "/api/pub"), ("GET", "/api/gone")}
    assert find_problems(app, public=public) == [
        "GET /api/pub is listed as public but requires sign-in",
        "GET /api/gone is listed but no such route exists",
    ]


def test_a_route_cannot_be_public_and_in_the_matrix() -> None:
    both = {("GET", "/api/healthz"): Rule(frozenset({Role.ADMIN}), THIS_TEST)}
    problems = find_problems(build("production"), matrix=both)
    assert "GET /api/healthz is both public and in the role matrix" in problems


def test_the_shipped_matrix_only_names_existing_tests() -> None:
    assert ROLE_MATRIX == {} or all(r.cross_tenant_test for r in ROLE_MATRIX.values())
