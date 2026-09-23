# Infrastructure

Terraform for the SMART on FHIR application: the backend in this repository, the
frontend in [`chicagopcdc/smart-on-fhir-frontent`][frontend], and everything the
two of them need to run in AWS.

**This deploys the frontend as well as the backend.** The frontend has no
Terraform of its own and is not expected to grow any — what it needs is a bucket
and a CloudFront distribution, both of which are described here, and the deploy
is `npm run build` followed by an `aws s3 sync` into the bucket this stack
created. See [Deploying the frontend](#deploying-the-frontend).

[frontend]: https://github.com/chicagopcdc/smart-on-fhir-frontent

---

## Why it lives here

Two options were on the table: a separate repository holding the unified
infrastructure for both halves, or everything in the backend repository with a
README explaining that it covers the frontend too. This is the second.

The argument for it is that there is one deployable system and the backend is the
larger half of it. The infrastructure needs the backend's port, health check
path, start command and the exact list of environment variables it reads — all of
which change in this repository, in the same pull requests. Splitting them means
a change to `app/core/config.py` and the change to the Terraform that supplies
the new variable land in two repositories and merge at different times, and the
window between them is a deployment that does not work.

What the frontend needs from here is much smaller and much more stable: a bucket
name and a distribution id, neither of which changes when the frontend does.

The argument against, which is real: a frontend developer now has to know this
repository exists to understand how their code gets served. That is what the
section below is for, and it is the thing to revisit if the frontend grows
infrastructure of its own.

---

## Layout

```
infrastructure/
├── modules/
│   ├── networking/    VPC, subnets, route tables, security groups, App Runner VPC connector
│   ├── database/      Aurora PostgreSQL Serverless v2
│   ├── secrets/       One Secrets Manager secret per secret environment variable
│   ├── ecr/           Private registry for the backend image
│   ├── iam/           Access role, instance role, CI deploy role
│   ├── app_runner/    The backend service
│   ├── frontend/      S3 bucket, CloudFront distribution, security headers
│   └── cognito/       User pool, for the application's own login layer
└── envs/
    ├── dev/           Root config: calls every module
    └── prod/          The same root config, different terraform.tfvars
```

`envs/dev/main.tf` and `envs/prod/main.tf` are byte-identical, and so are their
`variables.tf` and `outputs.tf`. Everything that differs between the two
environments is a variable, and the difference lives in `terraform.tfvars`.

That is a deliberate trade. The alternative — one shared root module the two
environments both call — removes the duplication but adds a layer of indirection
to every change, and makes "what is actually deployed in prod" a question you
answer by reading three files instead of one. Two copies is fine as long as they
do not drift, so **a change to one root belongs in the other in the same
commit**. `diff envs/dev/main.tf envs/prod/main.tf` should always be empty.

---

## What it builds

```
                    ┌─────────────┐
   browser ────────▶│ CloudFront  │────▶ S3 (private, OAC)        the frontend
                    └─────────────┘
       │
       │  XHR
       ▼
   ┌──────────────┐        ┌───────────────┐
   │  App Runner  │───────▶│ VPC connector │──┐                   the backend
   └──────────────┘        └───────────────┘  │
       ▲   pulls image                        │  private subnets
       │                                      ├──▶ Aurora Serverless v2
    ┌──────┐                                  │
    │ ECR  │                                  └──▶ NAT ──▶ Epic / Cerner / IdP
    └──────┘
```

Three things about that picture are worth stating, because they are the
constraints everything else follows from:

**App Runner's egress is all-or-nothing.** Attaching a VPC connector moves *every*
outbound connection into the VPC, not just the ones addressed to it. That is what
puts Aurora in reach, and it is also why the private subnets need a NAT gateway:
without one, the service can reach the database and nothing else, and every SMART
authorization, token refresh and FHIR read fails.

This contradicts the note in the original ticket that the public subnets were
for "NAT-free App Runner egress". They are not, and they cannot be — the
connector's network interfaces are never assigned a public address, so putting
them in a public subnet leaves them with no route out either. The public subnets
here hold the NAT gateways. `enable_nat_gateway` exists so the network can be
built without them, and the App Runner module refuses to deploy against a network
with no egress rather than letting it fail at the first FHIR call.

**Aurora is reachable from exactly one place.** `sg-rds` admits 5432 from
`sg-apprunner-vpc-connector` and from no CIDR at all. There is no bastion and no
public endpoint, which also means there is no way to run `psql` against it
without adding one.

**The database migration runs in the container's start command.** App Runner has
no one-off task primitive, and there is nowhere else inside the VPC to run
`alembic upgrade head` from without standing up a second compute service for it.
So the start command is `alembic upgrade head && uvicorn ...`, which means a
failed migration fails the deployment and App Runner keeps the previous revision
serving. See `run_migrations_on_start` in `modules/app_runner/variables.tf` for
what that costs.

---

## Before the first apply

State holds the generated database password and, in dev, the token encryption
key — in cleartext. Terraform has no way to encrypt values inside state, so the
bucket behind it is the control.

1. Create an S3 bucket for state, by hand or in a separate bootstrap stack:
   encryption on, versioning on, public access blocked, and a bucket policy that
   admits the people who deploy and nobody else.
2. Copy `envs/<env>/backend.hcl.example` to `backend.hcl` and fill it in. It is
   gitignored, because it names a specific account's bucket.
3. `terraform init -backend-config=backend.hcl`

`generate_token_encryption_key = false` in prod keeps the Fernet key out of state
entirely, at the cost of one manual write — see below.

---

## Deploying

The order matters on a new environment, because the service cannot start until
its secrets are real and its image exists.

```bash
cd envs/dev
terraform init -backend-config=backend.hcl
terraform apply
```

The first apply creates everything including the App Runner service, which will
fail to start: there is no image on the tag yet. That is expected.

**Fill in the secrets.** `terraform output secrets_awaiting_values` lists every
secret still holding the `REPLACE_ME` placeholder. For each:

```bash
aws secretsmanager put-secret-value \
  --secret-id smart-on-fhir-dev/EPIC_SANDBOX_CLIENT_SECRET \
  --secret-string 'the value'
```

Terraform never looks at these values again — the version resource has
`ignore_changes` on it — so this survives every later apply.

If Cognito is enabled, its client secret is one of them, and it is read out of
Cognito rather than typed:

```bash
aws cognito-idp describe-user-pool-client \
  --user-pool-id "$(terraform output -raw cognito_user_pool_id)" \
  --client-id <client id> \
  --query 'UserPoolClient.ClientSecret' --output text \
| aws secretsmanager put-secret-value \
    --secret-id smart-on-fhir-dev/OIDC_CLIENT_SECRET --secret-string file:///dev/stdin
```

**Push an image.**

```bash
REPO="$(terraform output -raw ecr_repository_url)"
aws ecr get-login-password | docker login --username AWS --password-stdin "${REPO%%/*}"

cd ../../..                      # the repository root, where the Dockerfile is
docker build --platform linux/amd64 -t "$REPO:$(git rev-parse --short HEAD)" .
docker push "$REPO:$(git rev-parse --short HEAD)"
```

`--platform linux/amd64` is not optional when building on an Apple Silicon
machine. App Runner runs x86_64, and an arm64 image pushed to it fails at
startup with an exec format error that surfaces only in the application log.

**Point the service at it** by setting `image_tag` in `terraform.tfvars` and
applying again. The repository is `IMMUTABLE`, so a tag means one image forever
and `latest` is only useful for the very first push.

```bash
cd infrastructure/envs/dev
terraform apply -var="image_tag=$(git -C ../../.. rev-parse --short HEAD)"
```

**Set log retention**, once per environment. `terraform output
log_retention_commands` prints the two commands. This is the one part of the
stack `terraform apply` does not reproduce on its own — see
[Known gaps](#known-gaps).

### Deploying the frontend

The frontend repository has no infrastructure of its own; this stack owns its
bucket and its distribution.

```bash
ENV=../smart-on-fhir-backend/infrastructure/envs/dev

cd smart-on-fhir-frontent
npm ci && npm run build

BUCKET="$(terraform -chdir=$ENV output -raw frontend_bucket)"
DIST="$(terraform -chdir=$ENV output -raw frontend_distribution_id)"

aws s3 sync dist/ "s3://$BUCKET/" --delete
aws cloudfront create-invalidation --distribution-id "$DIST" --paths '/index.html' '/'
```

**The API base URL is hardcoded in the frontend and this stack cannot set it.**
`src/config.js` sets `API_BASE` to a `http://localhost:<port>` literal, so a
build made as written above points a deployed frontend at whoever built it — which
fails silently in the browser rather than at build time.

Until that is read from `import.meta.env`, deploying the frontend means editing
that line to `terraform output -raw api_url` first. The one-line change that
fixes it properly, on the frontend side:

```js
const config = {
  API_BASE: import.meta.env.VITE_API_BASE ?? 'http://localhost:8000',  // the local default, whatever it is
};
```

after which the build becomes:

```bash
VITE_API_BASE="$(terraform -chdir=$ENV output -raw api_url)" npm run build
```

That belongs in the frontend repository, not here, which is why it is not done —
but nothing deploys correctly until it is.

Only `/index.html` needs invalidating. Vite writes content-hashed filenames under
`assets/`, which the distribution caches for a year precisely because a given URL
there can never change its contents; `index.html` is the one file whose URL is
stable and whose contents are not.

`--delete` is what keeps the bucket matching the build, and it is why the bucket
has versioning on with a thirty-day expiry on non-current versions: a bad sync is
recoverable.

The CI deploy role is granted exactly these four actions on the bucket plus
`CreateInvalidation` on the distribution, so the same commands work unchanged from
a GitHub Actions workflow assuming `terraform output deploy_role_arn`.

---

## Configuration that is not in Terraform

Three things, each for its own reason.

**Client secrets** — Epic's, Cerner's, the identity provider's — are issued by
somebody else and cannot be generated here. Terraform creates the container and a
placeholder; the value is written once with `put-secret-value` and never touched
by an apply again.

**Client ids and issuers** are plain environment variables in `terraform.tfvars`,
not secrets. A client id travels in the authorization URL the browser follows, so
it is public by construction, and paying $0.40 a month to store it in Secrets
Manager buys nothing.

**Redirect URIs registered with each EHR** have to match what the application
builds from `FRONTEND_HOSTNAME`, which is the CloudFront URL — `terraform output
frontend_url`. Epic and Cerner compare it literally, so this is a registration to
update by hand in their consoles whenever the frontend URL changes, and a
mismatch fails at the end of an authorization rather than at the start.

---

## Known gaps

Things this stack does not do, listed so they are decisions rather than
surprises.

**Log retention is a manual step.** App Runner creates its own log groups, named
`/aws/apprunner/<service>/<service-id>/{service,application}`, with retention set
to Never Expire. The service id does not exist until the service does, so
Terraform cannot create those groups first, and App Runner will not adopt a group
it did not make. `terraform output log_retention_commands` turns the environment's
`log_retention_days` into the two commands that apply it; they need running again
if the service is ever recreated.

**The frontend's API base URL is a literal, not configuration.** `src/config.js`
in the frontend repository hardcodes a localhost URL, so no value this stack
produces reaches it. Fixing that is a one-line change in that repository;
until it lands, deploying the frontend involves editing a source file, which is
the kind of step that gets forgotten once and then debugged for an hour.

**There is no path to the database for a human.** No bastion, no VPC endpoint, no
public access. Running a query against prod today means adding one. That is the
correct default and it will need solving the first time something has to be
inspected.

**Nothing is monitored.** No CloudWatch alarms, no dashboard, no alerting on a
failed deployment or a database at its ACU ceiling. The application emits
structured logs and App Runner ships them, which is enough to investigate an
incident and not enough to learn about one.

**Cognito may be the wrong identity provider.** It is here because the ticket
asked for a `cognito` module and because it is one more resource in a stack that
already exists rather than a service somebody has to run. Keycloak is being looked
at separately. The application does not care: it is a plain OIDC relying party
that discovers everything from the issuer's `.well-known/openid-configuration`,
so switching is `enable_cognito = false` plus `oidc_issuer` and `oidc_client_id`
pointed somewhere else. Nothing in the application changes.

**`require_user_login` is `false` in both environments,** including prod. Turning
it on is what makes a patient record reliably reachable on the next sign-in,
because it stops anonymous records — which can never be claimed by an account
later — from being created at all. It also breaks any frontend that has not
adopted login yet, which is the current one. Flip it in dev first.

---

## Cost

Rough monthly figures for `us-east-1`, for an environment that is up and lightly
used. Everything here is dominated by the two things that bill by the hour
whether or not anyone uses them.

| | dev | prod |
|---|---|---|
| NAT gateway | ~$32 (one) | ~$65 (one per AZ) |
| Aurora Serverless v2 | ~$45 (0.5 ACU floor) | ~$175 (1 ACU floor, two instances) |
| App Runner | ~$5–25 (0.25 vCPU, one warm instance) | ~$25–50 (0.5 vCPU) |
| Secrets Manager | ~$2 (5 secrets) | ~$2 |
| CloudFront + S3 | <$1 at this traffic | <$1 |
| ECR | <$1 | ~$2 |

The NAT gateway is the one that surprises people, because it is the single
largest line item in dev and it exists to serve traffic that is a few hundred
API calls a day. `single_nat_gateway = true` halves it in dev; there is no
configuration that removes it, because App Runner attached to a VPC connector has
no other route to Epic.

A dev environment that is not in use can be destroyed and rebuilt —
`database_deletion_protection`, `database_skip_final_snapshot`,
`ecr_force_delete` and `secret_recovery_window_days = 0` are all set to make that
work. Prod's are set to make it not.
