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
        ("GET", "/api/auth/oidc/login"),  # starts single sign-on; the person is not signed in yet
        ("GET", "/api/auth/oidc/callback"),  # finishes it: state, binding cookie and PKCE decide
        ("POST", "/api/auth/logout"),  # acts only on the caller's own cookie; idempotent
    }
)

# Authenticated routes: who may call them and which test proves another organization cannot.
_ALL_ROLES = frozenset({Role.ADMIN, Role.EDITOR, Role.MEMBER})
_PROFILE_TEST = "integration/test_profile.py::test_one_organization_never_sees_anothers_profiles"

_USERS_TEST = (
    "integration/test_user_admin.py::test_administrators_never_reach_another_organizations_people"
)
_ADMIN_ONLY = frozenset({Role.ADMIN})
_ADMIN_OR_EDITOR = frozenset({Role.ADMIN, Role.EDITOR})
_SOURCES_TEST = (
    "integration/test_sources_api.py::test_administrators_never_reach_another_organizations_sources"
)

_ITEMS_TEST = (
    "integration/test_items_api.py::test_administrators_never_reach_another_organizations_items"
)

_FEEDS_TEST = (
    "integration/test_feeds_api.py::"
    "test_a_person_never_reaches_another_persons_feeds_or_another_organizations"
)

ROLE_MATRIX: dict[Route, Rule] = {
    ("GET", "/api/me/feeds"): Rule(_ALL_ROLES, _FEEDS_TEST),
    ("POST", "/api/me/feeds"): Rule(_ALL_ROLES, _FEEDS_TEST),
    ("GET", "/api/me/feeds/{feed_id}"): Rule(_ALL_ROLES, _FEEDS_TEST),
    ("PATCH", "/api/me/feeds/{feed_id}"): Rule(_ALL_ROLES, _FEEDS_TEST),
    ("DELETE", "/api/me/feeds/{feed_id}"): Rule(_ALL_ROLES, _FEEDS_TEST),
    ("GET", "/api/me/feeds/{feed_id}/runs"): Rule(_ALL_ROLES, _FEEDS_TEST),
    ("GET", "/api/me/feeds/{feed_id}/items"): Rule(_ALL_ROLES, _FEEDS_TEST),
    ("GET", "/api/items"): Rule(_ALL_ROLES, _ITEMS_TEST),
    ("GET", "/api/items/{item_id}"): Rule(_ALL_ROLES, _ITEMS_TEST),
    ("POST", "/api/items/{item_id}/hold"): Rule(_ADMIN_OR_EDITOR, _ITEMS_TEST),
    ("POST", "/api/items/{item_id}/approve"): Rule(_ADMIN_OR_EDITOR, _ITEMS_TEST),
    ("POST", "/api/items/{item_id}/hide"): Rule(_ADMIN_OR_EDITOR, _ITEMS_TEST),
    ("POST", "/api/items/{item_id}/restore"): Rule(_ADMIN_OR_EDITOR, _ITEMS_TEST),
    ("GET", "/api/sources"): Rule(_ADMIN_OR_EDITOR, _SOURCES_TEST),
    ("POST", "/api/sources"): Rule(_ADMIN_ONLY, _SOURCES_TEST),
    ("GET", "/api/sources/{source_id}"): Rule(_ADMIN_OR_EDITOR, _SOURCES_TEST),
    ("PATCH", "/api/sources/{source_id}"): Rule(_ADMIN_ONLY, _SOURCES_TEST),
    ("DELETE", "/api/sources/{source_id}"): Rule(_ADMIN_ONLY, _SOURCES_TEST),
    ("GET", "/api/sources/{source_id}/runs"): Rule(_ADMIN_OR_EDITOR, _SOURCES_TEST),
    ("POST", "/api/sources/{source_id}/test-fetch"): Rule(_ADMIN_ONLY, _SOURCES_TEST),
    ("GET", "/api/sources/{source_id}/test-fetch/{test_id}"): Rule(_ADMIN_ONLY, _SOURCES_TEST),
    ("GET", "/api/users"): Rule(_ADMIN_ONLY, _USERS_TEST),
    ("PATCH", "/api/users/{user_id}/role"): Rule(_ADMIN_ONLY, _USERS_TEST),
    ("PATCH", "/api/users/{user_id}/status"): Rule(_ADMIN_ONLY, _USERS_TEST),
    ("GET", "/api/me/profile"): Rule(_ALL_ROLES, _PROFILE_TEST),
    ("PUT", "/api/me/profile"): Rule(_ALL_ROLES, _PROFILE_TEST),
    ("POST", "/api/me/onboarding/complete"): Rule(_ALL_ROLES, _PROFILE_TEST),
    ("GET", "/api/me/profile/options"): Rule(_ALL_ROLES, _PROFILE_TEST),
    ("GET", "/api/professions"): Rule(_ALL_ROLES, _PROFILE_TEST),
    ("GET", "/api/departments"): Rule(_ALL_ROLES, _PROFILE_TEST),
    ("GET", "/api/me"): Rule(
        frozenset({Role.ADMIN, Role.EDITOR, Role.MEMBER}),
        "integration/test_local_auth.py::test_me_never_shows_another_organizations_user",
    ),
}


ALL_ROLES = frozenset(Role)


def enforced_roles(dependant: Dependant) -> frozenset[Role] | None:
    """The roles a route lets in because of `require_role`, or None when it only needs sign-in."""
    found: list[frozenset[Role]] = []

    def walk(node: Dependant) -> None:
        for dep in node.dependencies:
            roles = getattr(dep.call, "allowed_roles", None)
            if roles is not None:
                found.append(roles)
            walk(dep)

    walk(dependant)
    return frozenset.intersection(*found) if found else None


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
            enforced = enforced_roles(route.dependant)
            if authenticated and rule.roles and (enforced or ALL_ROLES) != rule.roles:
                allowed = sorted(role.value for role in rule.roles)
                actual = (
                    sorted(role.value for role in enforced) if enforced else "any signed-in user"
                )
                problems.append(
                    f"{label}: the matrix lets in {allowed} but the route enforces {actual}"
                )
            if not _test_exists(rule.cross_tenant_test):
                problems.append(f"{label}: cross-tenant test {rule.cross_tenant_test!r} not found")
        else:
            problems.append(f"{label} is neither public nor in the role matrix")
    for key in sorted((public | matrix.keys()) - routes.keys()):
        problems.append(f"{key[0]} {key[1]} is listed but no such route exists")
    return problems
