# The Arena64 backend, as one image — A64-027.0.
#
# ## One image, and today one process
#
# `architecture.md` AD-02 describes three runtime profiles over one
# codebase: `api` for HTTP, `gateway` for WebSocket connections, `worker`
# for queue and event consumers. **Only one entrypoint exists.** The
# application mounts the gateway router and starts every background
# scheduler inside its own lifespan, so a single container is what the code
# actually is, and the profiles are selected by command rather than by
# image once those entrypoints are written.
#
# This image is therefore built to run any of them: the code is identical
# and only the command differs. Until the other two exist, the default
# command runs the whole thing, and `docs/01-architecture/architecture.md`
# §AD-02 carries the gap.
#
# ## Why `uv` and not pip
#
# `uv.lock` is the reproducibility contract this repository already keeps —
# `uv sync --frozen` fails rather than resolving, which is the property a
# build needs and `pip install -r` cannot offer.

# --- build -------------------------------------------------------------------
# Python 3.13, matching `requires-python`, `[tool.ruff] target-version`,
# `[tool.mypy] python_version`, `[tool.pyright] pythonVersion` — and, since
# A64-028.3 closed P3-2, `.python-version`, which is what `uv` reads to build
# a developer's virtualenv and CI's. The gap that file closes was real: the
# suite ran on 3.14 everywhere except here, so a green build said nothing
# about the interpreter that ships.
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS build

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /srv

# The lock and the manifest first, so a dependency layer is rebuilt only
# when dependencies change rather than on every source edit.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini main.py ./

# The project itself, now that its sources are present. `--no-dev` twice
# rather than once: the first pass installs dependencies, this one installs
# `arena64-api`, and neither may pull the test toolchain into a runtime image.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# --- runtime -----------------------------------------------------------------
FROM python:3.13-slim-bookworm AS runtime

# Two packages, and each earns its place. Every package in a runtime image
# is a package somebody has to patch.
#
#   curl               the healthcheck below
#   postgresql-client-17  `pg_dump`, `pg_restore` and `psql`, for
#                      `python -m app.operator.backup` — A64-028.3. This is
#                      already the image that runs `alembic upgrade` as the
#                      migration job, so it is the platform's operational
#                      image rather than only its API; a second image for one
#                      command would be a second thing to build, tag and keep
#                      in step with the schema it dumps.
#
# **17, not whatever Debian ships.** Bookworm's `postgresql-client` is 15,
# and `pg_dump` refuses a server newer than itself — so the default package
# produces a version-mismatch error at the first backup rather than at build
# time, which is the worst place to find out. PGDG is PostgreSQL's own apt
# repository and is where the matching major comes from.
RUN apt-get update \
    && apt-get install --no-install-recommends --yes curl ca-certificates gnupg \
    && curl --fail --silent --show-error --location \
        https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        | gpg --dearmor --output /usr/share/keyrings/pgdg.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/pgdg.gpg] \
https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install --no-install-recommends --yes postgresql-client-17 \
    && apt-get purge --yes gnupg \
    && apt-get autoremove --yes \
    && rm -rf /var/lib/apt/lists/*

# A non-root user, created before the copy so the application files are
# owned by the account that runs them. `security.md` is a placeholder, so
# this is the convention rather than a citation — but a service that does
# not need root should not have it.
RUN useradd --create-home --uid 10001 arena64

ENV PATH="/srv/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv
COPY --from=build --chown=arena64:arena64 /srv /srv

USER arena64
EXPOSE 8000

# The readiness endpoint the platform already publishes. It reports the
# database and Redis, so a container that answers this is one whose
# dependencies resolved — which is the only useful definition of healthy
# for a service that is a composition of both.
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD curl --fail --silent http://localhost:8000/api/v1/health || exit 1

# `main.py`, not `uvicorn main:app`.
#
# The repository already defines this entrypoint and it passes
# `log_config=None`, which is what keeps logging configured by
# `app.common.logging` rather than by uvicorn's default dictConfig — the
# structured, correlation-carrying format §8 of CLAUDE.md requires. The CLI
# has no flag for that: `--log-config /dev/null` makes uvicorn call
# `fileConfig` on an empty file and exit.
#
# It binds 0.0.0.0:8000 and enables reload only under `local`, so the same
# file is correct here and on a developer's machine.
#
# Concurrency stays out of the image. `gateway` holds long-lived connections
# and `api` does not, so a worker count baked in here would be the wrong
# number for one of them the day AD-02's profiles are actually split.
CMD ["python", "main.py"]
