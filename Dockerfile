# syntax=docker/dockerfile:1

# Two stages. The first resolves poetry.lock into a virtual environment; the
# second copies that environment onto a clean base, so Poetry and its caches
# never reach the image that ships.
#
# The base is pinned to a patch release rather than a floating tag: the lock
# file was resolved against a specific interpreter, and the image should not
# change it without someone deciding to.
ARG PYTHON_IMAGE=python:3.13.14-slim-bookworm


FROM ${PYTHON_IMAGE} AS build

# Poetry writes the environment inside the project directory rather than into a
# shared cache, which is what lets the next stage copy it as a single path. A
# virtual environment records where it lives, so it has to land at the same
# absolute path over there.
#
# no-pip keeps an installer out of the environment being built, so what the
# image runs against is exactly what poetry.lock resolved and nothing that was
# added to it afterwards. The base image's own pip is untouched; this is about
# the application's environment, not about hardening the interpreter.
ENV POETRY_VERSION=2.4.1 \
    POETRY_CACHE_DIR=/var/cache/poetry \
    POETRY_VIRTUALENVS_IN_PROJECT=true \
    POETRY_VIRTUALENVS_OPTIONS_NO_PIP=true \
    POETRY_NO_INTERACTION=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install "poetry==${POETRY_VERSION}"

WORKDIR /app

# Only the manifests, so editing a source file does not re-resolve dependencies.
COPY pyproject.toml poetry.lock ./

# `check --lock` refuses a lock file that no longer describes pyproject.toml, so
# the image can never be built from dependencies nobody resolved. There is no
# --no-root here: the project sets package-mode = false, so Poetry installs the
# dependencies and never the project itself. Every compiled dependency ships a
# manylinux wheel for both amd64 and arm64, which is why no compiler is needed.
RUN --mount=type=cache,target=/var/cache/poetry \
    poetry check --lock && poetry install --only main


FROM ${PYTHON_IMAGE} AS runtime

LABEL org.opencontainers.image.title="SMART on FHIR Backend" \
      org.opencontainers.image.description="Runs the SMART on FHIR authorization flow and serves a connected patient's normalized record." \
      org.opencontainers.image.source="https://github.com/chicagopcdc/smart-on-fhir-backend"

# The application is not installed as a package, and alembic.ini prepends the
# working directory to sys.path, so uvicorn and Alembic both resolve `app` only
# when this is where they are run from.
WORKDIR /app

ENV VIRTUAL_ENV=/app/.venv \
    PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# A fixed uid, used numerically on USER below, so an orchestrator can see the
# container runs unprivileged without resolving a name inside the image.
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid 10001 --no-create-home app

COPY --from=build /app/.venv ./.venv
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./

# Neither the base image nor Poetry's installer leaves any bytecode behind, so
# without this every interpreter start recompiles the standard library and the
# whole dependency tree and throws the result away: measured here, importing the
# application costs 4.3s cold against 1.3s warm, and the healthcheck's own
# import 362ms against 104ms. Paid by the app and the migration on every start,
# and by the probe for as long as the stack is up. Costs about 90MB of image,
# which is a trade worth revisiting if this ever ships somewhere that pulls it.
#
# compileall writes regardless of PYTHONDONTWRITEBYTECODE, which is what makes
# this the only chance to do it — the runtime user cannot write these paths.
RUN python -m compileall -q -j 0 /usr/local/lib /app

# Everything above stays owned by root and readable by all, so the service can
# run its own code but not rewrite it.
USER 10001:10001

EXPOSE 8000

# One worker, deliberately. Token refreshes are coalesced per connection within
# a single process and the rate limiter counts in-process, so a second worker
# would weaken both without a shared lock and a shared counter.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
