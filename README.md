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
  -d '{"provider": "CERNER_SANDBOX", "iss": "https://fhir-myrecord.cerner.com/r4/ec2458f2-1e24-41c8-b71b-0e701af7583d"}'
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
| `CERNER_SANDBOX` | Cerner / Oracle Health sandbox           | confidential |

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

### Checking an endpoint before starting a login

`smartCapable` on a search row is ONC's answer, not ours, recorded whenever it last
probed the endpoint. Each row carries `smartCapableAsOf` for the date of the file it came
from, and that date can be months in the past — long enough for an endpoint to have
moved, let its certificate lapse, or stopped publishing a SMART configuration while still
reading as connectable.

`GET /providers/endpoint-check?iss=…` asks the endpoint itself, now. It reads the same
`.well-known/smart-configuration` the authorization flow would, and answers with the
authorize and token endpoints the server advertises:

| `status` | `reachable` | Means |
| --- | --- | --- |
| `ok` | true | Publishes a configuration this backend can use |
| `no_smart_configuration` | true | Answers, but does not do SMART — a settled no |
| `invalid_smart_configuration` | true | Publishes something that cannot be used to authorize |
| `unreachable` | false | Could not be reached, or answered an error |

All four are a `200`: an unusable endpoint is the answer, not a failure to produce one.
Only an issuer this backend will not fetch is a `400`, and `checkedAt` says when the
configuration was actually read rather than when the response was built, so a reused
answer reports its real age.

Three things it does not tell you. It does not say this backend could *authorize* against
the endpoint — that needs a client registration held with that specific tenant, which is
what `configured` on a search row answers. A passing check is a fact about a moment; a
server can still be down by the time the user clicks through.

And `unreachable` means "could not be confirmed from here", not "broken". Several large
vendors answer `403` to an unauthenticated request for a SMART configuration, including
some serving thousands of the endpoints in the national list, so their endpoints read as
unreachable here while working normally for a registered client. Worth surfacing to a user
as a warning rather than a closed door.

Because the endpoint fetches a URL the caller chooses, it accepts `http` and `https`
only, refuses issuers carrying credentials or resolving to an address reachable only from
inside the network the backend runs in, and never repeats anything the endpoint said back
to the caller. It is throttled separately from the auth routes
(`ENDPOINT_CHECK_RATE_LIMIT`).

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

For the Docker Compose path, which is the shortest way to a running backend:
Docker with Compose v2.24 or newer, and Git. Nothing else.

For running it locally: Python 3.10 or newer, Poetry, a PostgreSQL to point it
at, and Git.

## Setup

Only the local path needs this; with Compose there is nothing to install.

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
| `RATE_LIMIT_ENABLED`, `AUTH_RATE_LIMIT`, `FHIR_RATE_LIMIT`, `ENDPOINT_CHECK_RATE_LIMIT` | no | Per-client throttles (slowapi `<count>/<window>` syntax) for the auth routes, the resource reads, and the endpoint check. Defaults `10/minute`, `30/minute` and `20/minute`; set `RATE_LIMIT_ENABLED=false` for single-user local runs. |
| `EPIC_SANDBOX_CLIENT_ID`, `EPIC_SANDBOX_CLIENT_SECRET` | no | Client credentials for the Epic sandbox. Register an app at https://fhir.epic.com to get them. |
| `EPIC_CLIENT_ID`, `EPIC_CLIENT_SECRET` | no | Client credentials for the production Epic provider. |
| `EPIC_ISSUER` | no | FHIR base URL of the production Epic deployment. Authorization for the `EPIC` provider is only allowed against this issuer. |
| `CERNER_CLIENT_ID`, `CERNER_CLIENT_SECRET` | no | Client credentials for the Cerner / Oracle Health sandbox, which is registered as a confidential client. Oracle issues the secret through a Cerner Central system account, reached from the application's page in code Console. Leave either unset and the provider rejects every request, and `GET /providers` omits it. |
| `SMART_LAUNCHER_CLIENT_ID` | no | Client id for the public SMART App Launcher. The launcher does not validate it, so a default is used when unset. |
| `LOG_FORMAT` | no | `text` (the default) writes the line a developer reads; `json` writes one object per line for a deployment shipping logs somewhere that parses them. Neither decides what may appear in a line — see [Logs](#logs). |

Generate a `TOKEN_ENCRYPTION_KEY`:

```bash
poetry run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Running it

Two ways in. Docker Compose brings the API and its database up together and
needs nothing installed but Docker. Running it locally with Poetry gives you
reload-on-save, which is the better inner loop once you are changing code. Both
read the same settings.

### With Docker Compose

```bash
docker compose up
```

That is the whole thing. The stack builds the image, starts Postgres, waits
until it is actually accepting connections, applies the migrations, and serves
the API at http://localhost:8000 with docs at http://localhost:8000/docs.

The public SMART App Launcher works immediately: it is a public client whose id
the launcher does not validate, so it needs no registration. Epic and Cerner
need credentials, so copy `.env.example` to `.env`, fill in the ones you have,
and run `docker compose up` again. Everything in `.env` reaches the containers
except `DATABASE_URL`, which the stack sets for itself.

`docker compose down` stops the stack and keeps the data. `docker compose down -v`
throws the database away, and the next `up` migrates a fresh one.

`up` builds the image the first time and then reuses it, so after changing code
run `docker compose up --build`. Without it the stack comes up healthy on the
previous build, which looks exactly like the change not working.

Five variables tune the stack itself, all optional and all with defaults:

| Variable | Default | What it does |
| --- | --- | --- |
| `APP_PORT` | `8000` | Host port the API is published on. |
| `POSTGRES_PORT` | `5433` | Host port Postgres is published on. Not 5432, so a first run does not collide with a Postgres already there. Nothing in the stack reaches the database this way; it is for `psql`. |
| `POSTGRES_USER` | `smartfhir` | Database user the stack creates and connects as. |
| `POSTGRES_PASSWORD` | `smartfhir` | Its password. |
| `POSTGRES_DB` | `smartfhir` | Database name. |

Postgres reads those last three only while its data directory is empty, so change
them on a fresh volume (`docker compose down -v`) or not at all. Keep them to
letters, digits, hyphens and underscores, since the connection string is assembled
from them without escaping. Changed against an
existing one, the connection string moves and the database does not, and the first
thing to notice is the migration failing to authenticate.

`docker-compose.yml` carries a development encryption key, which is what makes
`up` a single command. It is a working Fernet key that says what it is
(`DEVELOPMENT_ONLY_DO_NOT_USE_IN_PRODUCTION_A=`) and it is not a secret: anything
encrypted under it is readable by anyone holding the repo. Set
`TOKEN_ENCRYPTION_KEY` in your environment or in `.env` and it takes over.

#### What the image is

Two stages. The first installs Poetry and resolves `poetry.lock` into a virtual
environment; the second copies that environment onto a clean base and adds
`app/`, `migrations/` and `alembic.ini`. Poetry, pip and their caches live only
in the first stage and never ship, and no compiler is installed in either, since
every dependency has a prebuilt wheel for both amd64 and arm64. What runs is an
unprivileged user against code it does not own and cannot modify.

Migrations are a one-shot service of their own rather than something the app
does on start, so a failed migration is a failed step instead of an application
that will not stay up. `alembic upgrade head` does nothing when the schema is
already current, which is what makes restarting safe and a fresh volume
self-migrating.

It runs one uvicorn worker, for the reason [How long a connection
lasts](#how-long-a-connection-lasts) gives: refresh coalescing and the rate
limiter are both per-process.

### Locally with Poetry

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

## Deploying it

Two things this backend does not do for itself, both of which a deployment has to.

### TLS, and where it terminates

Nothing here serves HTTPS or redirects to it. uvicorn is started on plain HTTP
and is meant to sit behind a reverse proxy — an ingress controller, a load
balancer, nginx — that terminates TLS and forwards to it on the private network.
That is the ordinary arrangement and the one the container is built for; it is
not an omission, but it is only safe if the proxy is actually there.

It has to be, because the traffic is not optional to protect. A session bearer
travels in an `Authorization` header on every read, the OAuth `code` travels in
the callback, and the responses are a patient's chart. So:

- Terminate TLS at the proxy and refuse plain HTTP there, rather than serving
  both and hoping.
- Publish only the proxy. The compose file publishes the API on all interfaces
  (`APP_PORT`, default 8000) because that is what a local run needs; a deployment
  should bind it to the proxy's network instead.
- `FRONTEND_HOSTNAME` and `CORS_ALLOWED_ORIGINS` are `https://` URLs in any real
  deployment. The first is what each provider's `redirect_uri` is built from, and
  an EHR will reject a redirect that does not match what was registered.

Postgres is already bound to loopback by the compose file
(`127.0.0.1:${POSTGRES_PORT}`), so it is reachable for `psql` on the host and from
nowhere else.

### What the proxy has to forward

Once there is a proxy in front, every request arrives from the proxy's address.
The rate limiter keys on the client address, so without something to tell it
otherwise, every user in the world shares one bucket — the throttle stops being
per-client and becomes a cap on the whole deployment.

uvicorn already handles this and needs one thing set. It runs
`ProxyHeadersMiddleware` by default, which rewrites the client address from
`X-Forwarded-For` — but only for requests whose immediate peer it has been told
to trust, and that list defaults to `127.0.0.1`. A proxy in another container or
on another host is not on it, so its headers are ignored and the limiter goes on
keying by the proxy.

Set `FORWARDED_ALLOW_IPS` to the proxy's address:

```yaml
environment:
  FORWARDED_ALLOW_IPS: "10.0.1.7"     # the proxy, and nothing else
```

It is read by uvicorn rather than by this application, so it belongs in the
container's environment and is not in `.env.example` with the application's own
settings.

Set it to the address you actually have, and never to `*`. `X-Forwarded-For` is
a request header like any other: trust it from an untrusted peer and any caller
can claim any address, which hands them an unlimited rate limit and makes every
log line's client field fiction. Trusting only the proxy is what makes the header
worth reading, because the proxy is the one thing that overwrites it.

Two limits worth knowing before scaling out, both consequences of the same
single-process design the image ships:

- Rate limiting counts in memory, so *n* workers or replicas mean *n* times the
  configured limit. A shared counter is what fixes that properly.
- A refresh is coalesced per process, so two replicas can refresh the same
  connection at once. Both survive it — the rotated token is stored under a row
  lock — but one refresh is wasted.

## Logs

Every line is written by one handler, configured in `app/core/logging.py`.
`LOG_FORMAT=text` gives the line a developer reads and `LOG_FORMAT=json` gives one
object per line; both carry the same fields.

```
2026-08-29T21:14:07.881+00:00 WARNING [app.api.auth] Upstream discovery failed for
  CERNER_SANDBOX: unreachable request_id=9f2c…  event=auth.discovery.failed
  provider=CERNER_SANDBOX iss=https://fhir-myrecord.cerner.com/r4/…  reason=unreachable
  status=403 exception=DiscoveryUnreachableError
```

`request_id` is on every line written while serving one request, and on the
response as `X-Request-ID`, so a caller reporting a failure can quote the id and
have it found. A request that arrives with one of its own keeps it, provided it
is short and alphanumeric.

**What is never written.** A log line names what happened and where, never whose
it was. Access and refresh tokens, `Authorization` values, session ids, an OAuth
`state` or `code`, provider-issued patient ids, and resource content do not appear
in one. Our own `pat_…` record ids do: this application mints them, no server has
ever seen one, and without them a line naming a failure could not be tied to the
record it happened on.

The rule that keeps this true is that sensitive values are not put into log lines
in the first place — including exception *messages*, which are the trap, since a
validation error quotes the field value that failed and an HTTP error quotes the
URL. A failure is logged as its type and the frames it came through instead.

Two mechanisms enforce it rather than leave it to habit, since this application
does not write every line its handler receives. Values are redacted on the way
into a line: bearer prefixes, JWTs, Fernet ciphertext and URL query strings are
masked wherever they appear, and any field whose name says it is sensitive is
masked whatever its value looks like. And the libraries whose output could not be
redacted in principle are silenced by level — a database driver logs the
parameters bound to a statement, and httpx logs URLs with a patient id in the
path, neither of which has a shape to match on. `tests/test_logging_redaction.py`
drives a whole record through the API with the real handler attached and checks
that nothing from it came out.

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
| GET | `/providers/endpoint-check?iss=` | Whether one endpoint is reachable and SMART-capable right now, with the authorize and token endpoints it advertises. |
| GET | `/providers` | The providers this backend is configured for, for a frontend to offer alongside the searched list. Each carries the `provider` key `/auth/connect` expects. |
| GET | `/health` | Whether the service and its database are up. `503` when the database cannot be reached; this is what the container healthcheck reads. |

Every refusal answers `{"detail": "..."}`, including the throttle, which also sends
`Retry-After`. Two responses are not refusals and do not: `GET /health` reports its
own state as a `HealthResponse` whether it is `200` or `503`, and a request whose
shape is wrong gets FastAPI's own `422`, whose `detail` is a list of the fields at
fault rather than a sentence. The per-client rate limits are configurable (`AUTH_RATE_LIMIT`,
`FHIR_RATE_LIMIT`, `ENDPOINT_CHECK_RATE_LIMIT`) and can be turned off for local
single-user runs.

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

The suite runs offline. Every outbound request is mocked, which is what makes it fast and
what makes it fail for one reason only. What it proves is that the application is
internally consistent with the responses recorded under `tests/fixtures/`.

It cannot prove those recordings still describe the servers they came from, and twice they
have not. ONC's endpoint list moved, then the mirror it moved to was pruned, and the
offline suite stayed green through both. The live suite is the other half.

```bash
poetry run pytest -m live -rs
```

It needs a working internet connection and nothing else: no credentials, no database, no
containers. It fetches the discovery document from every configured issuer and checks that
the adapter can still build an authorization URL from it and still recognizes one of the
client authentication methods on offer; re-checks the sixteen captured public servers;
resolves ONC's endpoint list and checks that the columns the parser reads are still in the
published file; and reads a patient record from two servers that need no login, so
normalization runs against a real response.

`-rs` earns its four characters, because a live run reports two things that need opposite
reactions:

- **A failure** is a server that answered with something other than what we read before.
  Someone has to look, because the fixture standing in for it is now fiction.
- **A skip** is a server that did not answer at all. There is nothing to do but run it
  again later. The reason names the server, and `-rs` is what prints the reasons.

A skip that never goes away is not weather. It is a server that has gone for good, or a
URL that has been wrong since the day it was typed, and reading the reasons is the only
way either becomes visible.

### Refreshing what was captured

When the live suite finds a discovery document that has moved on, this is how the change
gets folded back into the fast suite:

```bash
poetry run pytest -m live --refresh-fixtures
git diff tests/fixtures/
poetry run pytest
```

The first command writes each server's current answer over the capture that stands in for
it, and does so before asserting, so a run that fails still records what it saw. The diff
is the report of what drifted. The third command says whether the mocked suite still holds
against the new reality.

The refresh only ever rewrites discovery documents, and never an entry's `id`, `source`,
`kind`, `note` or `usable` in `public_smart_configurations.json`: those say why an entry is
in the corpus, which is not a question a re-fetch can answer. Each entry carries its own
`capturedAt`, and a refresh dates the ones it reached and leaves the rest alone. Expect to
reach most rather than all of them, since one unreachable vendor out of sixteen is the
ordinary case.

It leaves the patient records alone on purpose. `cerner_patient_record.json` and
`launcher_patient_record.json` were captured with `_count=2`, which the application never
sends, and the Cerner one holds a search that genuinely timed out, which one of the tests
exists to exercise. Re-capturing either is a deliberate act rather than a routine one.

One warning worth reading before you commit a refresh. A `regression` entry is kept because
the document it captured is malformed, and a `refused` one because that document cannot be
used at all. Both stop covering what they were captured for the moment the server cleans up
its act, so if `poetry run pytest` fails after a refresh on an entry of either kind, revert
that entry rather than re-baselining against it: they are the only cover the tolerance in
`SMARTConfiguration` and the refusal path have.

That is not hypothetical, and it is worth knowing what the live suite will and will not
tell you about it. The `radysans` entry was captured publishing its endpoints as explicit
nulls, and now publishes real ones. The live check compares the field names a server
publishes rather than their values, deliberately, because an endpoint moving is the
server's business. So it reads that server as unchanged and stays green. The capture still
covers the null-endpoint shape offline, which is the job it is there for, but it no longer
mirrors its source, and its note says so.
