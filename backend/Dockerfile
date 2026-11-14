# Backend image for the API, the worker, the scheduler and the migration job.
# Build context: the repository root (uv.lock lives there).
#   docker build -f backend/Dockerfile .
# uv comes from its own image, pinned by digest (ADR-0024), not from a pip install that follows an
# index. Dependabot's docker updates keep this line and the base images below fresh. Keep the uv
# version equal to UV_VERSION in .github/workflows/ci.yml (a test compares them).
FROM ghcr.io/astral-sh/uv:0.13.8@sha256:5cc1805e2426484abca4ad46ea783bab281fdb225fd82d8ce6d66a81254bac85 AS uv

FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS build

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY --from=uv /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY backend/pyproject.toml backend/pyproject.toml
COPY backend/src backend/src
# Only runtime dependencies, exactly as pinned in uv.lock. The package is installed in editable
# mode so that it finds backend/alembic next to backend/src at run time.
RUN uv sync --frozen --no-dev --package pharos

FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

# The pinned base image (checked 2026-11-09) already ships apt 3.0.4, but still ships zlib1g
# 1:1.3.dfsg+really1.3.1-1+b1 with published HIGH vulnerabilities (CVE-2026-85091, a heap buffer
# overflow in gz_vacate(); CVE-2026-76844; fixed in 1:1.3.dfsg+really1.3.1-1+deb13u1). Nothing in
# this image runs apt after the build and the containers have no route to a package mirror, but
# the scanner is right to report it until the base image is rebuilt. Take the fixed package now;
# remove this layer when the pinned python:3.12-slim ships the fixed zlib1g. This layer reads the
# Debian mirror at build time, so two builds of one commit can differ by this one package.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends --only-upgrade zlib1g \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin pharos
WORKDIR /app
COPY --from=build /app /app
COPY backend/alembic backend/alembic
COPY backend/alembic.ini backend/alembic.ini

ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 10001

EXPOSE 8000
CMD ["uvicorn", "--factory", "pharos.main:create_app", "--host", "0.0.0.0", "--port", "8000"]
