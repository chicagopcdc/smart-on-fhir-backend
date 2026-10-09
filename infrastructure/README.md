# Infrastructure

Terraform for the SMART on FHIR application: the backend in this repository, the
frontend in [`chicagopcdc/smart-on-fhir-frontent`][frontend], and everything the
two of them need to run in AWS.

**This deploys the frontend as well as the backend.** The frontend has no Terraform
of its own and is not expected to grow any — what it needs is a bucket and a
CloudFront distribution, both described here. See
[Deploying the frontend](#deploying-the-frontend).

[frontend]: https://github.com/chicagopcdc/smart-on-fhir-frontent

---

## Layout

```
infrastructure/
├── modules/
│   ├── networking/    VPC, subnets, route tables, the three security groups
│   ├── database/      Aurora PostgreSQL Serverless v2
│   ├── secrets/       One Secrets Manager secret per secret environment variable
│   ├── ecr/           Private registry for the backend image
│   ├── iam/           Execution role, task role, CI deploy role
│   ├── ecs/           Fargate cluster, service, load balancer, migration task
│   ├── frontend/      S3 bucket, CloudFront distribution, security headers
│   └── cognito/       User pool, for the application's own login layer
└── envs/
    ├── dev/           Root config: calls every module
    └── prod/          The same root config, different terraform.tfvars
```

`envs/dev/{main,variables,outputs,versions}.tf` and `envs/prod/`'s are
byte-identical. Everything that differs between the environments is a variable, and
the difference lives in `terraform.tfvars`.

That is a deliberate trade. One shared root module both environments call would
remove the duplication and add a layer of indirection to every change, making "what
is actually deployed in prod" a question you answer by reading three files instead
of one. Two copies is fine as long as they do not drift, so **a change to one root
belongs in the other in the same commit** — and `.github/workflows/terraform.yml`
fails the build if they diverge.

---

## What it builds

```
                    ┌─────────────┐
   browser ────────▶│ CloudFront  │────▶ S3 (private, OAC)        the frontend
                    └─────────────┘
       │
       │  XHR (HTTPS)
       ▼
   ┌─────────────────┐
   │       ALB       │   public subnets, terminates TLS
   └─────────────────┘
       │
       ▼
   ┌─────────────────┐                                            the backend
   │  Fargate tasks  │──── public IP ──▶ IGW ──▶ Epic / Cerner / IdP
   │  public subnets │
   └─────────────────┘
       │  local route
       ▼
   ┌─────────────────┐
   │ Aurora (private)│
   └─────────────────┘

   ECR ──▶ image pulled by the execution role
```

No NAT gateway anywhere. That is the point of the shape, and it is worth two
paragraphs because it is the one decision here that overrode the written ticket.

### Why not App Runner

The first version of this stack ran the backend on App Runner, as the ticket
described. It does not work without a NAT gateway, and the ticket's note that the
public subnets were for "NAT-free App Runner egress" cannot be satisfied.

Attaching a VPC connector to an App Runner service — which is the only way to reach
a private Aurora cluster — moves **all** of the service's outbound traffic into the
VPC, not just the traffic addressed to it. The connector's network interfaces are
never assigned a public IP, and an internet gateway works by translating between a
private address and a public one, so an interface with no public address has nothing
it can translate. Putting the connector in a public subnet changes nothing. The only
exit left is a NAT gateway, at roughly $32/month per AZ, for traffic amounting to a
few hundred API calls a day.

### Why ECS

A Fargate task **can** be given a public IP. That single capability is the whole
reason for the move: a task in a public subnet with `assign_public_ip = true`
reaches Epic and Cerner through the internet gateway, and reaches Aurora in the
private subnets over the VPC's local route. Both destinations work and neither needs
a NAT.

Nothing is exposed by sitting in a public subnet. "Public subnet" names a routing
property; reachability is decided by the security group, and the tasks' group admits
the load balancer on port 8000 and nothing else. `enable_nat_gateway = true` buys the
classic private-subnet layout for anyone who wants the stronger guarantee — tasks
with no public address at all, which cannot be exposed by a later mistake in that
group — and it is one variable.

Three things came free with the move, each of which had been a gap:

- **Migrations are a real task.** `aws ecs run-task` against the migration task
  definition runs `alembic upgrade head` once and exits, before the service is
  touched. Under App Runner there was nowhere to run it from, so it was folded into
  the container's start command and every task that started raced the others.
- **Log retention is set at apply time.** The awslogs driver writes to a group
  Terraform creates. App Runner named its groups after a service id that could not
  exist yet, which made retention a manual step forever.
- **There is a way into the VPC.** `aws ecs execute-command` opens a shell in a
  running task, from which `psql` reaches Aurora through the same security group the
  application uses. No bastion, no public database endpoint, no inbound port.
  `terraform output exec_command` prints the invocation.

What it cost: the load balancer, at about $16/month plus capacity units, and it is
now the thing that terminates TLS. In prod that replaces two NAT gateways and is a
clear saving; in dev it replaces one and roughly breaks even. The honest summary is
that the money was a wash in dev and the operational gains were not.

One alternative was considered and rejected: keep App Runner and reach Aurora over
the **RDS Data API**, which is HTTP and needs no VPC connector at all — so no NAT
*and* no load balancer. It does not fit. The Data API has no asyncpg driver, and
`app/core/db.py` builds an async SQLAlchemy engine; adopting it would mean rewriting
the data layer as synchronous. That is a large application change to save $17/month.

---

## Before the first apply

State holds the generated database password and, in dev, the token encryption key —
in cleartext. Terraform cannot encrypt values inside state, so the bucket behind it
is the control.

1. Create an S3 bucket for state: encryption on, versioning on, public access
   blocked, and a policy admitting the people who deploy and nobody else.
2. Copy `envs/<env>/backend.hcl.example` to `backend.hcl` and fill it in. It is
   gitignored, because it names a specific account's bucket.
3. `terraform init -backend-config=backend.hcl`

`generate_token_encryption_key = false` in prod keeps the Fernet key out of state
entirely, at the cost of one manual write.

---

## Deploying

### First time, per environment

```bash
cd envs/dev
terraform init -backend-config=backend.hcl
terraform apply
```

The service comes up unhealthy: there is no image on the tag yet. Expected.

**Fill in the secrets.** `terraform output secrets_awaiting_values` lists every
secret still holding the `REPLACE_ME` placeholder.

```bash
aws secretsmanager put-secret-value \
  --secret-id smart-on-fhir-dev/EPIC_SANDBOX_CLIENT_SECRET \
  --secret-string 'the value'
```

Terraform never reads these values again — the version resource has
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

**Give CI the role.** Set `github_repository` in `terraform.tfvars`, apply, then put
`terraform output -raw deploy_role_arn` into the repository's
`AWS_DEPLOY_ROLE_ARN` secret. Exactly one environment per AWS account should set
`create_github_oidc_provider = true`; the other points at
`github_oidc_provider_arn`.

### Every time after that

Run the **deploy** workflow (Actions → deploy → Run workflow → pick an
environment). It builds the image, pushes it, runs the migration as a one-off task,
waits for it to exit 0, and only then rolls the service — so a failed migration
stops the deploy without having replaced a healthy task.

By hand, if you need to:

```bash
cd infrastructure/envs/dev
eval "$(terraform output -raw exec_command)"   # a shell in a running task
```

**Note what `terraform apply` does and does not do.** It owns every piece of
configuration — environment variables, secrets, sizing, networking — and registers a
new task definition revision when any of them change. It does **not** deploy: the
service has `ignore_changes` on its task definition so CI can own which image runs.
A configuration change made in Terraform reaches the service on the next deploy,
because the workflow renders its revision from the latest one rather than from a
file in the repo.

### Deploying the frontend

The frontend repository has no infrastructure of its own; this stack owns its bucket
and its distribution.

```bash
ENV=../smart-on-fhir-backend/infrastructure/envs/dev

cd smart-on-fhir-frontent
npm ci && npm run build

BUCKET="$(terraform -chdir=$ENV output -raw frontend_bucket)"
DIST="$(terraform -chdir=$ENV output -raw frontend_distribution_id)"

aws s3 sync dist/ "s3://$BUCKET/" --delete
aws cloudfront create-invalidation --distribution-id "$DIST" --paths '/index.html' '/'
```

Only `/index.html` needs invalidating. Vite writes content-hashed filenames under
`assets/`, which the distribution caches for a year precisely because a given URL
there can never change its contents; `index.html` is the one file whose URL is stable
and whose contents are not.

`--delete` is what keeps the bucket matching the build, and it is why the bucket has
versioning on with a thirty-day expiry on non-current versions.

The deploy role is granted exactly these four S3 actions plus `CreateInvalidation`,
so the same commands work unchanged from a workflow in the frontend repository
assuming `terraform output deploy_role_arn`. That workflow is not in this repo
because the code it builds is not either.

**The API base URL is hardcoded in the frontend and this stack cannot set it.**
`src/config.js` sets `API_BASE` to a `http://localhost:<port>` literal, so a build
made as written above points a deployed frontend at whoever built it — which fails
silently in the browser rather than at build time. The fix, on the frontend side:

```js
const config = {
  API_BASE: import.meta.env.VITE_API_BASE ?? 'http://localhost:8000',
};
```

after which the build becomes:

```bash
VITE_API_BASE="$(terraform -chdir=$ENV output -raw api_url)" npm run build
```

That belongs in the frontend repository, which is why it is not done here — but
nothing deploys correctly until it is.

---

## Configuration that is not in Terraform

**Client secrets** — Epic's, Cerner's, the identity provider's — are issued by
somebody else and cannot be generated here. Terraform creates the container and a
placeholder; the value is written once with `put-secret-value` and never touched by
an apply again.

**Client ids and issuers** are plain environment variables in `terraform.tfvars`,
not secrets. A client id travels in the authorization URL the browser follows, so it
is public by construction, and paying $0.40 a month to store it buys nothing.

**Redirect URIs registered with each EHR** have to match what the application builds
from `FRONTEND_HOSTNAME`, which is the CloudFront URL — `terraform output
frontend_url`. Epic and Cerner compare it literally, so this is a registration to
update by hand in their consoles whenever the frontend URL changes.

**DNS.** The load balancer's hostname is an output; the record pointing at it is not
managed here, because the zone is not.

---

## Known gaps

**The API has no HTTPS until a certificate is configured, and nothing works without
it.** This is the blocker, not a nicety. The frontend is served from CloudFront over
HTTPS, and a browser refuses to let an HTTPS page make requests to an HTTP origin —
so with `api_certificate_arn` unset, the API answers curl and nothing else. A
certificate cannot be issued for an ALB's own `*.elb.amazonaws.com` hostname, so
this needs a domain. Two ways:

- **A domain plus a regional ACM certificate** (not us-east-1 like CloudFront's —
  the ALB reads certificates from its own region). Set `api_certificate_arn` and
  `api_domain`, point a record at `alb_dns_name`. The proper answer.
- **CloudFront in front of the ALB**, which gives valid HTTPS on a
  `*.cloudfront.net` name with no domain at all. Free, and the hop from CloudFront
  to the ALB is plaintext over the public internet unless the ALB also has a
  certificate — which is the thing being avoided. Acceptable for a sandbox
  environment, not for one holding real records.

Nothing else in the stack can be finished until someone decides which.

**Nothing is monitored.** No CloudWatch alarms, no dashboard, no alerting on a
failed deployment, a database at its ACU ceiling, or a target group with no healthy
targets. Container Insights is on in prod so the metrics exist; nothing reads them.

**The tasks carry public IPs, including in prod.** Nothing can reach them — the
security group admits the load balancer and nothing else — but the addresses are
routable, so the security group is the only thing standing between a mistake and
exposure. `enable_nat_gateway = true` removes the public addresses for ~$65/month in
prod. Left off; worth revisiting before the first real patient record.

**Cognito may be the wrong identity provider.** It is built because the ticket asked
for a `cognito` module, and **Keycloak is now the confirmed direction**. The
application does not care: it is a plain OIDC relying party that discovers
everything from the issuer's `.well-known/openid-configuration`, so the switch is
`enable_cognito = false` plus `oidc_issuer` and `oidc_client_id` pointed at the
realm. No application change. What is *not* here is anywhere for Keycloak to run —
if it is self-hosted rather than managed, it needs its own service, database and
certificate, and that is not in this stack.

**`require_user_login` is `false` in both environments,** including prod. Turning it
on is what makes a patient record reliably reachable on the next sign-in, because it
stops anonymous records — which can never be claimed by an account later — from
being created at all. It also breaks any frontend that has not adopted login yet,
which is the current one. Flip it in dev first.

---

## Cost

Rough monthly figures for `us-east-1`, for an environment that is up and lightly
used. Dominated by the things that bill hourly whether or not anyone uses them.

| | dev | prod |
|---|---|---|
| NAT gateway | **$0** | **$0** |
| Application Load Balancer | ~$17 | ~$19 |
| Fargate | ~$3 (Spot, 1 task, 0.25 vCPU) | ~$36 (2 tasks, 0.5 vCPU) |
| Aurora Serverless v2 | ~$45 (0.5 ACU floor) | ~$175 (1 ACU floor, 2 instances) |
| Secrets Manager | ~$2 (5 secrets) | ~$2 |
| CloudFront + S3 | <$1 | ~$2 |
| ECR | <$1 | ~$2 |
| Container Insights | off | ~$5–10 |
| **total** | **~$68** | **~$245** |

Against the App Runner version this saves roughly $50/month in prod, where one load
balancer replaced two NAT gateways, and roughly breaks even in dev, where it
replaced one. Aurora's floor is the largest line item in both and is untouched by
any of this: `database_min_capacity = 0` would let dev's cluster pause entirely and
pay a cold start on the first connection, which is worth considering separately.

A dev environment that is not in use can be destroyed and rebuilt —
`database_deletion_protection`, `database_skip_final_snapshot`, `ecr_force_delete`,
`alb_deletion_protection` and `secret_recovery_window_days = 0` are all set to make
that work. Prod's are set to make it not.
