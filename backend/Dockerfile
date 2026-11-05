# Backend image for the API, the worker, the scheduler and the migration job.
# Build context: the repository root (uv.lock lives there).
#   docker build -f backend/Dockerfile .
FROM python:3.12-slim AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
RUN pip install uv==0.13.5

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY backend/pyproject.toml backend/pyproject.toml
COPY backend/src backend/src
# Only runtime dependencies, exactly as pinned in uv.lock. The package is installed in editable
# mode so that it finds backend/alembic next to backend/src at run time.
RUN uv sync --frozen --no-dev --package pharos

FROM python:3.12-slim

# The base image ships apt 3.0.3, which has a published HIGH vulnerability (CVE-2026-109518: an
# on-path attacker while apt downloads packages; fixed in 3.0.4). Nothing in this image runs apt
# after the build and the containers have no route to a package mirror, but the scanner is right to
# report it until the base image is rebuilt. Take the fixed apt now; remove this layer when
# python:3.12-slim ships apt 3.0.4 or later. Only the two packages that carry the fix are upgraded.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends --only-upgrade apt libapt-pkg7.0 \
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
