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
3. The tokens are stored encrypted. The frontend then reads the patient's records through
   `/fhir_resources`.

A single discovery-driven provider adapter handles any SMART server by reading its
`.well-known/smart-configuration` at request time, so no server-specific code is needed.

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
| `OAUTH_STATE_TTL_SECONDS` | no | How long an OAuth state row stays valid before it is swept. Defaults to 600. |
| `EPIC_SANDBOX_CLIENT_ID`, `EPIC_SANDBOX_CLIENT_SECRET` | no | Client credentials for the Epic sandbox. Register an app at https://fhir.epic.com to get them. |
| `EPIC_CLIENT_ID`, `EPIC_CLIENT_SECRET` | no | Client credentials for the production Epic provider. |
| `EPIC_ISSUER` | no | FHIR base URL of the production Epic deployment. Authorization for the `EPIC` provider is only allowed against this issuer. |

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
| POST | `/auth/callback` | Exchange the returned `{code, state}` for tokens. Returns the connected patient's id. |
| GET | `/fhir_resources?fhir_patient_id=` | Fetch the patient's FHIR resources using the stored token. |
| GET | `/lantern-endpoints?query=&page=&pageSize=` | Search the national list of FHIR endpoints (from ONC Lantern). |
| GET | `/providers` | The providers this backend is configured for, for the frontend to offer alongside the Lantern list. Each carries the `provider` key `/auth/start` expects. |

## Tests

```bash
poetry run pytest
```

The suite runs offline. To also run the opt-in tests that reach live SMART servers:

```bash
poetry run pytest -m live
```
