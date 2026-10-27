"""The route-coverage gate (M2 acceptance criterion 3).

Every route the application registers must be either on the explicit public list or authenticated
(it depends on `current_principal`) AND listed in the role matrix with the roles that may call it
and a cross-tenant test. A new endpoint that is none of these fails `test_route_policy.py`, so
nobody can add an unprotected route by forgetting a dependency.

FastAPI 0.162 no longer flattens included routers into `app.routes`; they are `_IncludedRouter`
objects. Walking them uses FastAPI internals, so anything the walker does not recognise (a mounted
sub-application, a WebSocket, a plain Starlette route) is reported as a problem instead of being
skipped, and the real application must show its known routes (a positive control).
"""

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, _IncludedRouter

from pharos.core.auth import current_principal
from pharos.core.principal import Role

TESTS = Path(__file__).resolve().parent

Route = tuple[str, str]  # (METHOD, path)


@dataclass(frozen=True)
class Rule:
    roles: frozenset[Role]
    # "relative/path.py::test_name" under backend/tests; must exist.
    cross_tenant_test: str


# Routes that need no sign-in. Each one needs a reason in a comment.
PUBLIC_ROUTES: frozenset[Route] = frozenset(
    {
        ("GET", "/api/healthz"),  # liveness, touches nothing
        ("GET", "/api/readyz"),  # readiness; Caddy does not serve it to the internet
        ("GET", "/api/auth/config"),  # the sign-in page needs it before anyone is signed in
        ("POST", "/api/auth/local/login"),  # this IS the sign-in; throttled and Origin-checked
        ("POST", "/api/auth/logout"),  # acts only on the caller's own cookie; idempotent
    }
)

# Authenticated routes: who may call them and which test proves another organization cannot.
ROLE_MATRIX: dict[Route, Rule] = {
    ("GET", "/api/me"): Rule(
        frozenset({Role.ADMIN, Role.EDITOR, Role.MEMBER}),
        "integration/test_local_auth.py::test_me_never_shows_another_organizations_user",
    ),
}


def _depends_on(dependant: Dependant, target: object) -> bool:
    return any(dep.call is target or _depends_on(dep, target) for dep in dependant.dependencies)


@dataclass(frozen=True)
class Found:
    key: Route
    dependant: Dependant | None  # None: not a FastAPI route, so it cannot be authenticated
    kind: str


def _walk(routes: Iterable[Any]) -> Iterator[Any]:
    for route in routes:
        if isinstance(route, _IncludedRouter):
            yield from _walk(route.effective_candidates())
            yield from _walk(route.effective_low_priority_routes())
        else:
            yield route


def registered_routes(app: FastAPI) -> dict[Route, Found]:
    found: dict[Route, Found] = {}
    for route in _walk(app.routes):
        original = getattr(route, "original_route", route)
        path = str(getattr(route, "path", None) or getattr(original, "path", repr(route)))
        dependant = getattr(route, "dependant", None)
        if isinstance(original, APIRoute) and dependant is not None:
            methods = set(original.methods or ())
            for method in sorted(methods):
                if method not in ("HEAD", "OPTIONS"):
                    found[(method, path)] = Found((method, path), dependant, "api")
        else:
            kind = type(original).__name__
            found[(f"<{kind}>", path)] = Found((f"<{kind}>", path), None, kind)
    return found


def _test_exists(reference: str) -> bool:
    path, _, name = reference.partition("::")
    file = TESTS / path
    return bool(name) and file.is_file() and f"def {name}(" in file.read_text()


def find_problems(
    app: FastAPI,
    public: frozenset[Route] = PUBLIC_ROUTES,
    matrix: dict[Route, Rule] | None = None,
) -> list[str]:
    matrix = ROLE_MATRIX if matrix is None else matrix
    problems: list[str] = []
    routes = registered_routes(app)
    for key, route in sorted(routes.items()):
        label = f"{key[0]} {key[1]}"
        if route.dependant is None:
            if key not in public:
                problems.append(f"{label} is a {route.kind}, which the gate cannot check")
            continue
        authenticated = _depends_on(route.dependant, current_principal)
        if key in public:
            if authenticated:
                problems.append(f"{label} is listed as public but requires sign-in")
            if key in matrix:
                problems.append(f"{label} is both public and in the role matrix")
        elif key in matrix:
            if not authenticated:
                problems.append(f"{label} is in the role matrix but does not require sign-in")
            rule = matrix[key]
            if not rule.roles:
                problems.append(f"{label} has no roles in the matrix")
            if not _test_exists(rule.cross_tenant_test):
                problems.append(f"{label}: cross-tenant test {rule.cross_tenant_test!r} not found")
        else:
            problems.append(f"{label} is neither public nor in the role matrix")
    for key in sorted((public | matrix.keys()) - routes.keys()):
        problems.append(f"{key[0]} {key[1]} is listed but no such route exists")
    return problems
