# SMART on FHIR Backend

A FastAPI backend that lets a patient securely connect their medical records from an
electronic health record (EHR) system using SMART on FHIR. It runs the OAuth 2.0
authorization flow, stores the resulting tokens encrypted in Postgres, and fetches FHIR
resources for the connected patient.

## How it works

1. The frontend sends the patient to `/auth/start`, which redirects them to their EHR to
   log in and approve access.
2. The EHR sends them back with a short-lived `code`. The frontend posts that code to
   `/auth/callback`, and the backend exchanges it for access and refresh tokens.
3. The tokens are stored encrypted, and the backend returns an opaque `session_id`. The
   frontend then reads the patient's records through `/fhir_resources`, presenting that
   session id as a bearer token.

## SMART providers

One discovery-driven adapter (`GenericSMARTProvider`) serves every server. It
reads each server's `.well-known/smart-configuration` at request time and adapts
to it — PKCE (S256) turns on when the server advertises it, and token-endpoint
client authentication is chosen from what the server supports. There are no
per-vendor subclasses.

Configured out of the box (`app/providers/config.py`):

| Provider key     | Server                                   | Client     |
|------------------|------------------------------------------|------------|
| `EPIC` / `EPIC_SANDBOX` | Epic                              | confidential |
| `SMART_LAUNCHER` | Public SMART App Launcher                | public (PKCE) |
| `CERNER_SANDBOX` | Cerner / Oracle Health sandbox           | public (PKCE) |

A standalone launch against the public SMART Launcher encodes its launch context
in the FHIR base URL (`…/v/r4/sim/<opts>/fhir`), so `SMART_LAUNCHER` allows any
base under the launcher's host via `allowed_issuer_prefixes`. Real EHRs use the
plain FHIR base and keep exact-match allowlisting.

### Adding a server

No code change is needed — add one entry to `EHR_CONFIGS`:

1. Register the app with the EHR and put its credentials in `.env` (a public
   client has a `client_id` and no secret; PKCE stands in for the secret).
2. Add an `EHR_CONFIGS` row with the `client_id`/`client_secret`, the shared
   `redirect_uri`, the `scopes` to request, and an `allowed_issuers` list pinning
   the FHIR base URL(s) the app may authorize against.
3. Start the flow: `/auth/start?provider=<KEY>&iss=<FHIR base URL>`.

Endpoints, PKCE, and the client-auth method all come from discovery.

### Reading resources

`/auth/callback` returns a `session_id` on success. Read resources by presenting
it as a bearer token — `GET /fhir_resources` with header
`Authorization: Bearer <session_id>`. The patient is taken from the session, so
the endpoint does not accept a patient id from the caller: a caller can only reach
the patient they authenticated as.

## Prerequisites

- Python 3.10 or newer
- Poetry
- PostgreSQL (running one in a container is the easiest option)
- Git

## Setup

Install dependencies:

```bash
poetry install
```

Copy the example environment file and fill in the values:

```bash
cp .env.example .env
```

You must set `DATABASE_URL` and `TOKEN_ENCRYPTION_KEY`. To use the Epic sandbox you also
need its client credentials. See Configuration below.

## Configuration

Settings are read from the environment, and from `.env` in local development.
`.env.example` lists every key.

| Setting | Required | What it does |
| --- | --- | --- |
| `DATABASE_URL` | yes | Async SQLAlchemy connection string for Postgres (asyncpg driver), for example `postgresql+asyncpg://postgres:devpass@localhost:5432/smartfhir`. The app will not start without it. |
| `TOKEN_ENCRYPTION_KEY` | yes | One or more Fernet keys, comma separated, used to encrypt tokens at rest. The first key encrypts, the rest still decrypt, so you rotate by prepending a new key. Generate one with the command below. |
| `FRONTEND_HOSTNAME` | no | Base URL of the frontend that handles the OAuth redirect. Defaults to `http://localhost:3000`. Each provider's redirect URI is this value plus `/auth/callback`. |
| `CORS_ALLOWED_ORIGINS` | no | Comma-separated browser origins allowed to call the API. Defaults to `FRONTEND_HOSTNAME`. A wildcard is not supported. |
| `OAUTH_STATE_TTL_SECONDS` | no | How long an OAuth state row stays valid before it is swept. Defaults to 600. |
| `APP_SESSION_TTL_SECONDS` | no | How long a session (the bearer the frontend uses to read resources) stays valid before the caller must re-authorize. Defaults to 3600. |
| `RATE_LIMIT_ENABLED`, `AUTH_RATE_LIMIT`, `FHIR_RATE_LIMIT` | no | Per-client throttles for the auth and resource endpoints (slowapi `<count>/<window>` syntax). Defaults `10/minute` and `30/minute`; set `RATE_LIMIT_ENABLED=false` for single-user local runs. |
| `EPIC_SANDBOX_CLIENT_ID`, `EPIC_SANDBOX_CLIENT_SECRET` | no | Client credentials for the Epic sandbox. Register an app at https://fhir.epic.com to get them. |
| `EPIC_CLIENT_ID`, `EPIC_CLIENT_SECRET` | no | Client credentials for the production Epic provider. |
| `EPIC_ISSUER` | no | FHIR base URL of the production Epic deployment. Authorization for the `EPIC` provider is only allowed against this issuer. |
| `CERNER_CLIENT_ID` | no | Client id for the Cerner / Oracle Health sandbox (a public client; PKCE stands in for a secret). Unset leaves the provider rejecting every request. |
| `SMART_LAUNCHER_CLIENT_ID` | no | Client id for the public SMART App Launcher. The launcher does not validate it, so a default is used when unset. |

Generate a `TOKEN_ENCRYPTION_KEY`:

```bash
poetry run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Running it

Start Postgres. With Docker:

```bash
docker run -d --name smartfhir -e POSTGRES_PASSWORD=devpass -e POSTGRES_DB=smartfhir \
  -p 5432:5432 postgres:16
```

Apply the database migrations:

```bash
poetry run alembic upgrade head
```

Start the server:

```bash
poetry run uvicorn app.main:app --reload
```

The API is now at http://localhost:8000, with interactive docs at http://localhost:8000/docs.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/auth/start?provider=&iss=` | Start the OAuth flow. Redirects the browser to the EHR login. |
| POST | `/auth/callback` | Exchange the returned `{code, state}` for tokens. Returns the connected patient's id and a `session_id`. |
| GET | `/fhir_resources` | Fetch the patient's FHIR resources using the stored token. Requires `Authorization: Bearer <session_id>`. |
| GET | `/lantern-endpoints?query=&page=&pageSize=` | Search the national list of FHIR endpoints (from ONC Lantern). |
| GET | `/providers` | The providers this backend is configured for, for the frontend to offer alongside the Lantern list. Each carries the `provider` key `/auth/start` expects. |

## Tests

```bash
poetry run pytest
```

The suite runs offline (HTTP mocked). To also run the opt-in tests that reach the live
Epic, SMART Launcher, and Cerner discovery documents:

```bash
poetry run pytest -m live
```
