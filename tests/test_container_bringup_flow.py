"""Bringing the stack up: what a clean checkout gets from `docker compose up`.

These read the shipped ``docker-compose.yml`` rather than a copy of its values,
so a setting that becomes required, a password that moves, or a migration job
pointed at the wrong database fails here instead of at someone else's first run.

Interpolation is resolved with nothing set, which is exactly a clean checkout:
Compose reads a ``.env`` beside the file to interpolate ``${VAR}`` and there is
no ``.env`` in a fresh clone, so every default applies. That is the
configuration this project promises works with no setup.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

import respx
import yaml

from app.core.config import Settings
from app.core.crypto import _build_cipher
from app.providers import config
from tests.app_harness import (
    SMART_LAUNCHER,
    app_db as _app_db,
    client as _client,
    connect,
    token_response,
)

_ROOT = Path(__file__).resolve().parent.parent
_COMPOSE_PATH = _ROOT / "docker-compose.yml"

# ``${VAR}``, ``${VAR:-default}`` and ``${VAR-default}``. Compose understands
# more than this; the file is held to the subset below by its own test, so a
# form these cannot read has to arrive with a resolver rather than quietly
# resolving to the wrong string and passing.
_INTERPOLATION = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::?-([^{}]*))?\}")


def _compose() -> dict:
    """Parsed fresh each time, so one test cannot hand another a mutated copy."""
    return yaml.safe_load(_COMPOSE_PATH.read_text(encoding="utf-8"))


def _resolve(value: str) -> str:
    """One compose value with every variable taken to its default.

    Only applied to ``environment`` values. Compose's ``$$`` escape for a
    literal dollar appears once in the file, in the database healthcheck, which
    is read as text rather than resolved.
    """
    return _INTERPOLATION.sub(lambda match: match.group(2) or "", value)


def _port_fields(mapping: str) -> list[str]:
    """A compose port mapping split on its separators, and not on anything else.

    ``${VAR:-default}`` carries a colon of its own, so splitting the raw string
    would cut the mapping in the wrong place: the host field of
    ``"${APP_PORT:-8000}:8000"`` would come out as ``"${APP_PORT"``. Standing a
    colon-free placeholder in for each interpolation first leaves only the real
    separators, whether or not a bind address is present.
    """
    return _INTERPOLATION.sub("${}", mapping).split(":")


def _environment(service: str) -> dict[str, str]:
    """The environment a service starts with, as a clean checkout resolves it."""
    return {
        key: _resolve(value)
        for key, value in _compose()["services"][service]["environment"].items()
    }


def test_the_compose_environment_satisfies_every_required_setting(monkeypatch):
    # Read off the model rather than listed here, so a setting that loses its
    # default in future joins this check without anyone remembering to add it.
    required = {
        name.upper() for name, field in Settings.model_fields.items() if field.is_required()
    }
    environment = _environment("app")

    missing = sorted(required - set(environment))
    assert not missing, f"the compose app service supplies no value for {missing}"

    # Named is not the same as set. An interpolation that lost its default —
    # ${DATABASE_URL} rather than ${DATABASE_URL:-…} — resolves to the empty
    # string on a clean checkout, which every check below would accept.
    blank = sorted(name for name in required if not environment[name])
    assert not blank, f"the compose app service resolves {blank} to nothing"

    # Only what compose provides. The developer's shell and the .env this repo
    # happens to be checked out with must not stand in for something the stack
    # is missing, so both are taken out of the way.
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)

    settings = Settings(_env_file=None)

    assert settings.database_url == environment["DATABASE_URL"]

    # Present is not the same as usable: a typo in the literal key would pass a
    # presence check and only surface when the first token was stored. Built
    # through the application's own path rather than a second reading of it.
    _build_cipher(settings.token_encryption_key)

    # And nothing else. Derived from the model so a vendor added later is
    # covered here too: the stack must come up with no registration at all, or
    # the launcher is not really reachable out of the box.
    credentials = [
        name
        for name in Settings.model_fields
        if name.endswith(("_client_id", "_client_secret"))
    ]
    configured = [name for name in credentials if getattr(settings, name) is not None]
    assert not configured, f"the compose default should set no vendor credentials, got {configured}"


def test_the_app_and_the_migration_reach_the_same_database():
    app_url = _environment("app")["DATABASE_URL"]

    # A migration applied to a different database than the one the app reads is
    # the failure this stack is most able to produce and least able to explain.
    assert app_url == _environment("migrate")["DATABASE_URL"]

    database = _environment("db")
    parsed = urlsplit(app_url)

    assert parsed.scheme == "postgresql+asyncpg"
    assert parsed.hostname == "db", "the containers must reach the db service, not the host"
    assert parsed.username == database["POSTGRES_USER"]
    assert parsed.password == database["POSTGRES_PASSWORD"]
    assert parsed.path.lstrip("/") == database["POSTGRES_DB"]


def test_no_published_port_assumes_one_is_free_on_the_host():
    for name, service in _compose()["services"].items():
        for mapping in service.get("ports", []):
            assert isinstance(mapping, str), (
                f"{name} declares a port in long syntax, which _port_fields does not "
                "read; the check below would silently stop looking at anything"
            )
            # [bind address:]host:container — the host port is always second to last.
            fields = _port_fields(mapping)
            host = fields[-2]
            assert "${" in host, (
                f"{name} publishes {fields[-1]} on a fixed host port {host}; both 5432 "
                "and 8000 are commonly already taken, and a first run should not "
                "collide with whatever is already running"
            )


def test_the_compose_file_uses_no_interpolation_these_tests_cannot_resolve():
    text = _COMPOSE_PATH.read_text(encoding="utf-8")

    # Split on ``$$`` first: Compose's escape for a literal dollar is how the
    # database healthcheck hands ``${POSTGRES_USER}`` to the shell in the
    # container, and that is not an interpolation this file performs.
    for segment in text.split("$$"):
        for expression in re.findall(r"\$\{[^}]*\}", segment):
            assert _INTERPOLATION.fullmatch(expression), (
                f"{expression} is a form _resolve() does not understand, so every "
                "assertion above it would be checking the wrong string"
            )


@respx.mock
async def test_a_stack_with_no_vendor_credentials_can_still_authorize(tmp_path, monkeypatch):
    # The whole registry a fresh stack has. The test above proves the compose
    # environment sets no vendor credentials, and a provider without a client id
    # is dropped from the listing and refused at connect, so the launcher is
    # what is left — reachable because its client id falls back to a literal the
    # launcher does not validate, and its registration carries no secret.
    launcher = config.EHR_CONFIGS["SMART_LAUNCHER"]
    assert launcher["client_id"], "the launcher must not need a registration"
    assert launcher["client_secret"] is None
    monkeypatch.setattr(config, "EHR_CONFIGS", {"SMART_LAUNCHER": launcher})

    url = f"sqlite+aiosqlite:///{tmp_path / 'bringup.db'}"
    async with _app_db(url):
        async with _client() as client:
            offered = await client.get("/providers")
            assert offered.status_code == 200
            assert [entry["provider"] for entry in offered.json()] == ["SMART_LAUNCHER"]

            # Not just listed: authorizing against it has to complete, which is
            # the difference between a stack that starts and one that works.
            body = await connect(client, SMART_LAUNCHER, token_response("launcher-patient"))

    assert body["patientId"].startswith("pat_")
    assert body["sessionId"]
    assert body["connection"]["provider"] == "SMART_LAUNCHER"
