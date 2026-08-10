# SMART on FHIR Backend

A FastAPI backend that lets a patient securely connect their medical records from an
electronic health record (EHR) system using SMART on FHIR. It runs the OAuth 2.0
authorization flow, stores the resulting tokens encrypted in Postgres, and fetches FHIR
resources for the connected patient.

## How it works

1. A caller posts a provider and a FHIR base URL to `POST /auth/connect`. The backend
   validates both, reads the server's SMART configuration, and returns the URL to send
   the patient to.
2. The patient logs in and approves access, and the EHR sends them back with a
   short-lived `code`. The caller posts that code to `POST /auth/callback`, and the
   backend exchanges it for access and refresh tokens.
3. The tokens are stored encrypted. What comes back is a `patientId` and an opaque
   `sessionId`, which reads the record through `GET /patients/{patientId}/resources`
   and `GET /patients/{patientId}/summary`.

Interactive documentation is at `/docs`.

### Patient records and connections

A stored token is a **connection**: one person, at one provider, on one server. A
**patient record** is the app-level identity those connections hang off, named by an
opaque `pat_…` id.

The distinction matters because a FHIR patient id is opaque *per server*. The id Epic
issues and the id Cerner issues for the same person are unrelated strings, and nothing
stops two servers from issuing the same one to different people. So the record id is
ours, which is what makes it safe to put in a URL and what lets a record span providers.

Connecting a second provider while presenting an existing session says "this is the same
patient as the record I already hold", and the new connection joins it:

```bash
# First provider: no session presented, so this anchors a new record.
curl -X POST localhost:8000/auth/connect \
  -H 'Content-Type: application/json' \
  -d '{"provider": "EPIC_SANDBOX", "iss": "https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4"}'

# Second provider: the session says which record to join.
curl -X POST localhost:8000/auth/connect \
  -H "Authorization: Bearer $SESSION_ID" \
  -H 'Content-Type: application/json' \
  -d '{"provider": "CERNER_SANDBOX", "iss": "https://fhir-ehr-code.cerner.com/r4/ec2458f2-1e24-41c8-b71b-0e701af7583d"}'
```

Only the person authorizing both can make that claim, so nothing infers it. A bearer
that is present but invalid is refused rather than ignored, which would quietly split a
patient's providers across two records.

A session presented at connect time is the **only** thing that places a connection on an
existing record. Authorizing a provider without one always starts a new record, even when
the same EHR account is already held under another. That is deliberate: proving control
of one connection proves nothing about the rest of a record, and the server cannot tell
whether the caller is the person who assembled it. Rejoining on sight would hand a caller
every provider linked to that record on the strength of a single login — trivially so on
a server like the public SMART Launcher, where any caller can select any patient with no
credentials at all.

The cost is that re-authorizing after a session expires starts a fresh record, and the
other providers have to be linked again. The same EHR account can therefore be held under
more than one record, each with its own token. The duplication is what keeps the records
isolated.

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
3. Start the flow: `POST /auth/connect` with `{"provider": "<KEY>", "iss": "<FHIR base URL>"}`.

Endpoints, PKCE, and the client-auth method all come from discovery.

### Reading resources

`GET /patients/{patientId}/resources`, with the session as
`Authorization: Bearer <sessionId>`. The path id is checked against the session, and a
record the session does not hold is a 404 rather than a 403 — confirming that an id
exists is itself a leak, and the caller could do nothing with the distinction.

Resources come back **per connection**, not merged. The same person's Condition at two
hospitals is two resources on two servers, and joining them here would assert something
the data cannot support. The summary below is the merged view.

#### How much of the record

`?type=Condition&type=Immunization` names the resource types to read; repeat it for
several. A type the backend does not fetch is a 422 listing the ones it does, rather
than an empty result that reads as the patient having none of it.

With no `type`, `?include=us-core` (the default) reads the resources a certified server
must support, and `?include=all` adds every other type in `RESOURCE_FETCH_CONFIG`.

`all` is a diagnostic affordance rather than something a client should read on a
patient's behalf. Several rows in the long tail are not scoped to a patient
(`Practitioner`, `Organization`, `ValueSet`, `Substance`, `Schedule`, `Slot`), so
they ask a server for its whole directory and return nothing about the person who
authorized the read.

A type is in the default set only if it is a USCDI v3 data class, is scoped to the
authorized patient, and is useful without a second fetch. The rules are spelled out
above `US_CORE_RESOURCES` in `app/providers/config.py`, and a name there that
matches no fetch config row fails at startup.

#### What comes back

Responses are normalized (`app/fhir/normalize.py`), so a resource type has the
same shape whichever server it came from. Each resource is validated against the
`fhir.resources` R4B models one at a time: one that will not parse is skipped with
a logged warning and counted in `skipped`, rather than costing the whole read.

```jsonc
{
  "patientId": "pat_9Fq3TnVb2mKd7sXwLp4RcYhZ",
  "include": "us-core",
  "types": ["Patient", "AllergyIntolerance", "Condition", "..."],
  "connections": [
    {
      "provider": "EPIC_SANDBOX",
      "iss": "https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4",
      "patientFhirId": "erXuFYUfucBZaryVksYEcMg3",
      "status": "ok",           // "degraded" if some types failed, "error" if all did
      "error": null,
      "resources": {
        "Condition": {
          "resourceType": "Condition",
          "status": "ok",       // "error" if the read failed or was not FHIR; keys never change
          "statusCode": 200,
          "count": 2,           // entries on this page
          "total": 17,          // what the server says it holds, null where it does not say
          "truncated": true,    // there is a next page
          "skipped": 0,         // resources that would not parse
          "error": null,
          "entries": [
            {
              "id": "62cf4e59",
              "resourceType": "Condition",
              "title": "Hyperlipidemia",
              "code": { "system": "http://snomed.info/sct", "code": "55822004", "display": "..." },
              "status": "active",
              "date": "2010-03-14",
              "category": "problem-list-item",
              "detail": { "verificationStatus": "confirmed", "severity": null, "abatement": null },
              "resource": { /* the full validated FHIR resource, nulls dropped */ }
            }
          ]
        }
      }
    }
  ]
}
```

`resources` is keyed by fetch config row rather than by FHIR type, so the vital
signs and laboratory searches keep their own buckets while both hold `Observation`
entries. `title`, `code`, `status`, `date` and `category` mean the same thing for
every type; `detail` is what differs between them. An Observation's is
`{value, unit, components, interpretation, referenceRange}`, where a panel such as
a blood pressure carries its numbers in `components` and no `value` of its own.
The full resource travels with each summary, so nothing the server sent is lost.

One page per resource type is read. `total` and `truncated` report that rather than
presenting a partial list as a whole one.

### The clinical summary

`GET /patients/{patientId}/summary` reads the same US Core set and folds it into the
sections a chart is read in: conditions, medications, allergies, immunizations, vital
signs, labs, procedures, encounters, reports. Here merging across providers is the
point, so each item carries the `provider` it came from and each section is sorted
newest first, with undated resources last rather than floated to the top.

`?limit=` caps the items per section (20 by default); a section's `total` still reports
everything the servers hold. `?provider=` narrows to one connection.

```jsonc
{
  "patientId": "pat_9Fq3TnVb2mKd7sXwLp4RcYhZ",
  "generatedAt": "2026-07-26T14:03:11+00:00",
  "demographics": { "name": "Amy V. Shaw", "gender": "female",
                    "birthDate": "1987-02-20", "deceased": false,
                    "sources": ["EPIC_SANDBOX"] },
  "connections": [
    { "provider": "EPIC_SANDBOX",   "iss": "...", "patientFhirId": "...",
      "status": "ok",    "error": null },
    { "provider": "CERNER_SANDBOX", "iss": "...", "patientFhirId": "...",
      "status": "error", "error": "No resource could be read from this provider" }
  ],
  "sections": [
    { "key": "conditions", "title": "Conditions", "resourceTypes": ["Condition"],
      "total": 14,        // across connections; server-claimed where given
      "returned": 14,     // what fitted in `limit`
      "items": [ { "provider": "EPIC_SANDBOX", "title": "Hyperlipidemia", "...": "..." } ] }
  ],
  "issues": [
    { "provider": "CERNER_SANDBOX", "type": "Immunization",
      "error": "Provider returned HTTP 503" }
  ]
}
```

A provider being down does not sink the summary. Its connection is reported as failed,
what could not be read is listed under `issues`, and the rest of the chart comes back
as a normal 200. Sections are always present even when empty, so a consumer reads a
fixed shape rather than probing for keys — and a section that is empty because a read
failed is distinguishable from one that is empty because the patient has nothing, by
looking at `issues`.

### Finding a server

`GET /providers/search` searches ONC's daily list of certified FHIR endpoints. Beyond a
free-text `?query=` over the URL and organization name, it filters on two things the
published file already carries:

- `?vendor=epic` matches the **certified EHR developer**, which is what separates
  endpoints served by Epic from organizations that merely have Epic in their name.
- `?smartOnly=true` keeps the endpoints that served a SMART configuration when ONC last
  probed them, dropping both outright failures and the ones it could not reach.

Rows carry `configured` and `provider` where this backend holds a registration that can
authorize against that exact issuer. That is never inferred from the vendor: two
hospitals running the same EHR are separate tenants with separate logins and separate
client registrations.

### How long a connection lasts

An access token from a SMART server lasts about an hour. The refresh token stored beside
it lasts days, and a read spends it: before fetching anything the backend renews a token
that has run out, or is close enough that the fan-out of FHIR calls after it would not
finish in time. A server that hands back a *new* refresh token has retired the one it was
given, so the replacement is stored — that is the difference between one working refresh
and all of them. Concurrent reads of one connection are coalesced so they spend that
token once between them rather than each replaying it.

Concurrent reads are coalesced within one process, the same scope the rate limiter works
at. A deployment running more than one worker should put that on a lock the workers
share, since a server that treats a replayed refresh token as theft revokes the whole
grant rather than just the second call.

Not every failure to renew means the same thing, and the API says which:

| What happened | `status` | `needsReauthorization` |
| --- | --- | --- |
| The provider refused the refresh (`invalid_grant`) | `error` | `true` |
| The token endpoint was unreachable, or answered a 5xx | `error` | `false` |
| The token ran out and there was no refresh token to spend | `error` | `true` |

Only the first and third are worth asking a patient to reconnect over. A 503 or a
rejected client secret says nothing about the patient's authorization, so the stored
refresh token is kept rather than thrown away over a provider's bad minute.

A connection is removed when nothing can reach it any more. That is not "has no live
session" — connections are meant to outlive sessions. Two things keep a connection
outright: a live session on its record, and its own access token for as long as that
still works. Past those, what decides it is whether the connection could be brought back
to life:

- One holding a refresh token the provider has not refused can be renewed whenever a read
  comes, so an expired access token means nothing on its own. It is kept until nothing has
  read it for `CONNECTION_RETENTION_DAYS`. Reading a record resets that clock across every
  connection on it, so the window only ever runs out on one nobody is using.
- One that cannot be renewed was worth exactly its access token, and is now worth nothing.

The sweep runs when an authorization completes, alongside the state and session sweeps, so
it happens as often as the growth it answers. It reaches no provider: revoking belongs on
the deliberate path.

`DELETE /patients/{patientId}/connections/{provider}` is that path. It revokes the token
at the EHR where the server publishes a `revocation_endpoint` (RFC 7009), and removes the
connection whether or not that succeeded — a provider being down must not be able to keep
a patient connected to it. Removing a record's last connection removes the record, and the
session that held it stops resolving.

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
| `TOKEN_REFRESH_LEEWAY_SECONDS` | no | How close to its expiry a stored access token may be before a read renews it first. Defaults to 60. |
| `CONNECTION_RETENTION_DAYS` | no | How long a connection that can still be refreshed is kept after the last time anything read it. Defaults to 30. |
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

Full request and response schemas, with examples, are at `/docs`.

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/auth/connect` | Start the OAuth flow. Returns the authorization URL to send the patient to. An optional session bearer links the new connection to an existing patient record. |
| POST | `/auth/callback` | Exchange the returned `{code, state}` for tokens. Returns a `patientId`, a `sessionId`, and the connection just made. |
| GET | `/patients/{patientId}/resources?type=&include=&provider=` | The patient's normalized resources, per connection. Requires `Authorization: Bearer <sessionId>`. |
| GET | `/patients/{patientId}/summary?limit=&provider=` | A clinical summary merged across the record's connections. Requires the same bearer. |
| DELETE | `/patients/{patientId}/connections/{provider}` | Disconnect one provider, revoking at the EHR where it offers a revocation endpoint. Removes the record with its last connection. Requires the same bearer. |
| GET | `/providers/search?query=&vendor=&smartOnly=&page=&pageSize=` | Search the national list of FHIR endpoints (ONC Lantern), filtered by EHR vendor and SMART capability. |
| GET | `/providers` | The providers this backend is configured for, for a frontend to offer alongside the searched list. Each carries the `provider` key `/auth/connect` expects. |

Every refusal answers `{"detail": "..."}`, including the throttle, which also sends
`Retry-After`. The per-client rate limits are configurable (`AUTH_RATE_LIMIT`,
`FHIR_RATE_LIMIT`) and can be turned off for local single-user runs.

### Deprecated

These keep working, with their original response shapes, until the frontend has moved
off them. They are marked deprecated in `/docs`.

| Method | Path | Superseded by |
| --- | --- | --- |
| GET | `/auth/start?provider=&iss=` | `POST /auth/connect` |
| GET | `/fhir_resources` | `GET /patients/{patientId}/resources` |
| GET | `/lantern-endpoints` | `GET /providers/search` |

## Tests

```bash
poetry run pytest
```

The suite runs offline (HTTP mocked). To also run the opt-in tests that reach the live
Epic, SMART Launcher, and Cerner discovery documents:

```bash
poetry run pytest -m live
```
