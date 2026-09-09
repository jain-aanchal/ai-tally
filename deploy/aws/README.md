# Deploying ai-tally on AWS (ECS-Fargate or EKS)

A from-zero runbook for running ai-tally's two application tiers — the **ingest gateway**
(FastAPI/uvicorn) and the **Next.js dashboard** — on AWS, wired to AWS-managed backing stores and
AWS-native secrets + identity. This is the AWS counterpart to the GCP runbook in
[`deploy/gcp/README.md`](../gcp/README.md) (CTO-153); the pipeline shape is identical, only the
backing services and the identity/secrets plumbing change:

```
 send_batch / SDK ──POST /v1/batches──▶  gateway (ECS-Fargate service or EKS Deployment)
                                              │  auth → rate-limit → idempotency →
                                              │  validate → enrich cost → map to row
                                              ▼
                                         ClickHouse  (ClickHouse Cloud, or in-cluster StatefulSet)
                                              ▲
   browser ──▶ Next.js web ──Route Handler──┘   (queries ClickHouse live + calls the gateway)

 control plane:  Postgres  ──▶  RDS for PostgreSQL
 replay blobs:   object store ─▶ S3 bucket (CTO-152)
 secrets:        provider keys / DB creds ─▶ AWS Secrets Manager (consumed via IRSA / task roles)
```

Everything here is **additive** — it does not touch `infra/docker-compose.yml`, the app source, or
the local dev flow. It reuses the **same images** the GCP path uses: the gateway
(`infra/gateway/Dockerfile`) and the web tier (`web/Dockerfile`).

> **There is now Terraform for all of this.** [`terraform/`](terraform/README.md) creates the VPC,
> RDS, S3, ECR, KMS, Secrets Manager entries, IAM roles, ALB and ECS services that every step below
> assumes exist, and consumes `ecs/*.taskdef.json` and `ecs/iam/*.json` as templates rather than
> replacing them. It has never been applied against a real account, and its README says so and says
> what that leaves unproven. This runbook stays the reference for what each resource is for, which
> IAM grants to delete, and how to do it by hand.
>
> **Placeholder tokens are delimited, and that is load-bearing (CTO-360).** Every placeholder in
> `ecs/` is spelled `__LIKE_THIS__`: `__ACCOUNT__`, `__REGION__`, `__REPLAY_BUCKET__`,
> `__KMS_KEY_ID__`, `__CLICKHOUSE_HOST__`, `__CLICKHOUSE_URL__`, `__GATEWAY_URL__`. They used to be
> the bare words `ACCOUNT` and `REGION`, and the `sed` recipes below are blanket substitutions, so
> `s/REGION/$REGION/g` also rewrote the environment variable **names** `AWS_REGION` and
> `TALLY_REPLAY_S3_REGION` in `gateway.taskdef.json` into `AWS_us-east-1` and
> `TALLY_REPLAY_S3_us-east-1`. The output stayed valid JSON, `register-task-definition` accepted it,
> the task started, and both settings were simply absent: the AWS default credential chain lost its
> region and the S3 replay store lost its, with no error naming the cause. `__REGION__` cannot occur
> inside an identifier, so a blanket replace is now safe in every file. If you add a placeholder,
> give it the same delimiters.

## What's in this directory

```
deploy/aws/
├── README.md                       this runbook
├── terraform/                      IaC for everything this runbook creates by hand (CTO-335)
│   ├── README.md                   bootstrap order, what to fill in, rollback, troubleshooting
│   ├── bootstrap/                  state bucket + the CI OIDC role (§1), separate root module
│   └── modules/                    network, data, iam, compute
├── ecs/                            PRIMARY — ECS-on-Fargate task/service definitions
│   ├── gateway.taskdef.json        gateway task definition (Secrets Manager injection, task role)
│   ├── web.taskdef.json            web task definition
│   ├── edge-proxy.taskdef.json     edge-proxy task definition (no container health check, see §7A)
│   ├── gateway.service.json        gateway ECS service (Fargate, ALB target group)
│   ├── web.service.json            web ECS service
│   ├── edge-proxy.service.json     edge-proxy ECS service (two warm tasks, never scales to zero)
│   └── iam/
│       ├── task-role-policy.json          workload identity: S3 replay + Cost Explorer + Bedrock (+Secrets)
│       ├── execution-role-policy.json     ECS execution role: ECR pull + logs + secret injection
│       ├── ecs-tasks-trust-policy.json    trust policy for the ECS task/execution roles
│       ├── irsa-trust-policy.json         trust policy for the EKS IRSA role (OIDC)
│       ├── github-actions-oidc-trust-policy.json  trust policy for the CI image-push role (§1)
│       └── github-actions-ecr-policy.json         push rights on the three ECR repositories (§1)
└── helm/ai-tally-eks/              SECONDARY — EKS Helm chart (gateway + web + optional ClickHouse)
    ├── Chart.yaml
    ├── values.yaml                 all knobs, documented
    ├── values-eks.example.yaml     a filled-in example override file
    └── templates/                  Deployments, Services, ConfigMaps, ServiceAccount (IRSA),
                                    SecretProviderClass (Secrets Manager via CSI), HPAs
```

## ECS-Fargate vs EKS — which to pick

**ECS-Fargate is the primary/default AWS path** (this ticket's recommendation): it is the closest
AWS analog of the GCP Cloud Run path — fully managed, no cluster to operate, per-task billing, two
independent services. Reach for **EKS** only when you already run a cluster or need in-cluster
workloads (a ClickHouse StatefulSet, DaemonSets, a service mesh).

| Pick **ECS-Fargate** when… | Pick **EKS** when… |
|---|---|
| You want the least ops: no cluster/nodes to manage, Fargate runs the tasks. | You already run an EKS cluster / want pods next to other in-cluster services. |
| You're fine with the two tiers as independent managed services behind an ALB. | You need an in-cluster ClickHouse StatefulSet, DaemonSets, or fine pod control. |
| You want the simplest secrets story: `secrets[].valueFrom` injects at task start. | You want one Helm release, HPAs, and k8s-native networking/mesh. |

Both paths use the **same images**, the **same Secrets Manager secrets**, and the **same workload
IAM role** (`ai-tally-workload`) — ECS attaches it as the task role; EKS binds it to the KSA via
IRSA. You can start on ECS and lift to EKS later without rebuilding anything.

> Backing stores (RDS, ClickHouse, S3) are shared by both paths and are provisioned once (steps
> 3–4). Only the compute deploy (step 7) differs.

### Mapping to the GCP (CTO-153) path

| Concern | GCP (CTO-153) | AWS (this ticket) |
|---|---|---|
| Managed serverless compute | Cloud Run | **ECS-Fargate** (primary) |
| Cluster compute | GKE + Helm | EKS + Helm (`helm/ai-tally-eks`) |
| Control-plane DB | Cloud SQL for Postgres | **RDS for PostgreSQL** |
| Telemetry store | ClickHouse Cloud / in-cluster STS | ClickHouse Cloud / in-cluster STS |
| Streaming buffer (CTO-37) | Pub/Sub-style | **MSK** or self-hosted **Redpanda** |
| Replay blobs (CTO-152) | GCS bucket | **S3 bucket** |
| Secrets | Secret Manager + CSI | **Secrets Manager** + CSI / task injection |
| Workload identity | Workload Identity (GSA↔KSA) | **IRSA** (EKS) / **task role** (ECS) |
| DB connectivity | Cloud SQL Auth Proxy sidecar | RDS direct over the VPC (no sidecar) |

---

## 0. Prerequisites

- `aws` CLI v2 authenticated (`aws sts get-caller-identity` succeeds) with a default region.
- For EKS: `eksctl`, `kubectl`, and `helm` (v3).
- Shell variables used throughout (edit and export):

```bash
export ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
export REGION=us-east-1
export ECR=$ACCOUNT.dkr.ecr.$REGION.amazonaws.com
export IMAGE_TAG=1.0.0                         # or a git sha
```

## 1. ECR and the images

CI builds all three images and publishes two of them (CTO-334), so the ordinary path is to create the
repositories and the push role once and then never build the gateway or the proxy by hand again.

**The web image is the exception and you build it yourself.** Clerk's `NEXT_PUBLIC_` publishable key
is inlined into the client bundle at build time, so a web image built without one can never sign
anybody in, and one built with yours belongs to your deployment and has no business in a shared
registry. CI builds it on every publish run purely to catch the Dockerfile rotting, and pushes it
nowhere. If you are using `ecs/web.taskdef.json` at all (the dashboard's supported home is Vercel,
see `deploy/vercel/README.md`), see "Building by hand" below.

The images are **ARM64** for ECS, because Fargate on ARM64 is cheaper per vCPU-hour and the edge
proxy is deliberately kept warm. All three task definitions under `ecs/` set
`runtimePlatform.cpuArchitecture: ARM64` to match; changing one without the other gives you tasks
that never start. The edge-proxy image is additionally published for amd64 for the Helm chart's
audience.

```bash
for repo in gateway web edge-proxy; do
  aws ecr create-repository --repository-name "ai-tally/$repo" --region "$REGION"
done
```

### The CI push role (OIDC, no access keys)

The `images` job in `.github/workflows/ci.yml` assumes a role through GitHub's OIDC provider. No
long-lived AWS access key is created, stored, or accepted; the job has no access-key fallback and is
supposed to have none. Create the provider and the role once:

```bash
# One OIDC provider per account. Skip if `aws iam list-open-id-connect-providers` already lists it.
aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com

# The trust policy restricts the role to this repository's main branch and its vX.Y.Z tags, so a
# fork or a pull-request run cannot assume it. Substitute ACCOUNT before applying.
sed "s/__ACCOUNT__/$ACCOUNT/g" deploy/aws/ecs/iam/github-actions-oidc-trust-policy.json \
  > /tmp/gha-trust.json
aws iam create-role --role-name ai-tally-ci-ecr-push \
  --assume-role-policy-document file:///tmp/gha-trust.json

# Push rights on exactly the three repositories, and nothing else in the account.
sed -e "s/__ACCOUNT__/$ACCOUNT/g" -e "s/__REGION__/$REGION/g" \
  deploy/aws/ecs/iam/github-actions-ecr-policy.json > /tmp/gha-ecr.json
aws iam put-role-policy --role-name ai-tally-ci-ecr-push \
  --policy-name ai-tally-ci-ecr-push --policy-document file:///tmp/gha-ecr.json
```

Then set three **repository variables** (Settings → Secrets and variables → Actions → Variables).
They are variables and not secrets on purpose: a role ARN and a registry hostname are not
credentials, and leaving them readable makes it auditable which role CI can assume.

| Variable | Value |
|---|---|
| `AWS_ECR_ROLE_ARN` | `arn:aws:iam::$ACCOUNT:role/ai-tally-ci-ecr-push` |
| `AWS_ECR_REGISTRY` | `$ACCOUNT.dkr.ecr.$REGION.amazonaws.com` |
| `AWS_REGION` | `$REGION` |

While `AWS_ECR_ROLE_ARN` is unset the job still publishes to GHCR and prints a notice saying the ECR
half was skipped, so CI is green from the first merge and gains the ECR push the moment the role
exists. Re-run the job for an already-merged commit with **Actions → CI → Run workflow** rather than
pushing an empty commit.

### Tags

Every image carries the **commit SHA**, which is the tag a task definition should pin: it is the only
one that cannot be moved, and it is what makes a running container traceable back to a commit. On a
merge to `main` the image is additionally tagged `main` (moving, for charts and local pulls), and on
a `vX.Y.Z` git tag it is additionally tagged `X.Y.Z`. There is no `latest`.

```bash
export IMAGE_TAG=$(git rev-parse HEAD)     # what the task definitions below should reference
```

### Building by hand

Required for the web tier, optional for the other two. The gateway build context is the **repo root**
(its Dockerfile COPYs both the gateway and the SDK it depends on); the web context is `web/`; the
edge-proxy context is `infra/edge-proxy/`. Pass `--platform linux/arm64` or the task will not start
on the ARM64 platform the task definitions pin.

```bash
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$ECR"

# The web image, with YOUR Clerk publishable key baked in. Without a key here `next build` fails
# prerendering /_not-found; with the TALLY_DEV_TENANT build arg instead it builds but ships an
# unauthenticated dashboard, which is only ever right for a demo. Push it to a repository only you
# pull from.
docker buildx build --platform linux/arm64 --push \
  --build-arg NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=pk_live_... \
  -t "$ECR/ai-tally/web:$IMAGE_TAG" web/

# The two CI already publishes, if you would rather not wait for a merge.
docker buildx build --platform linux/arm64 --push \
  -t "$ECR/ai-tally/gateway:$IMAGE_TAG" -f infra/gateway/Dockerfile .
docker buildx build --platform linux/amd64,linux/arm64 --push \
  -t "$ECR/ai-tally/edge-proxy:$IMAGE_TAG" infra/edge-proxy/
```

> `web/Dockerfile` does not declare a `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` build arg today, so that
> first command needs one added. It is left out of this change on purpose: `web/app/layout.tsx` and
> the `TALLY_DEV_TENANT` guard around it are owned by another in-flight PR, and the web tier is not
> part of the bundle. Track it with the Vercel path, which is where the dashboard actually ships.

## 2. Networking (shared)

Both paths run in a VPC with private subnets for the tasks/pods and RDS, and (for public ingress) an
ALB in public subnets. Use your existing VPC, or `eksctl create cluster` (step 7B) which creates one.
Ensure the compute security group can reach **RDS:5432** and **ClickHouse:8123/8443**.

## 3. Provision RDS (Postgres control plane)

```bash
aws rds create-db-instance \
  --db-instance-identifier ai-tally-pg \
  --engine postgres --engine-version 16 \
  --db-instance-class db.t3.medium \
  --allocated-storage 20 \
  --master-username tally \
  --master-user-password 'CHOOSE_A_STRONG_PASSWORD' \
  --db-name tally \
  --no-publicly-accessible \
  --vpc-security-group-ids sg-YOUR_DB_SG

# Endpoint (host) for the DSN:
aws rds describe-db-instances --db-instance-identifier ai-tally-pg \
  --query 'DBInstances[0].Endpoint.Address' --output text
```

The DSN you store as the `postgresDsn` secret (step 5) is:
`postgresql://tally:PASS@<rds-endpoint>:5432/tally`. Unlike GCP's Cloud SQL, **there is no
auth-proxy sidecar** — the gateway reaches RDS directly over the VPC. (IAM database authentication is
an optional hardening step: enable `--enable-iam-database-authentication`, grant `rds-db:connect` on
the task/IRSA role, and issue a short-lived token instead of a password — left to the operator.)

Apply the control-plane DDL (`db/postgres/*.sql`) once — e.g. connect from a bastion / a one-off task
in the VPC and run the migrations in order.

## 4. Provision ClickHouse + the S3 replay bucket + (optional) MSK/Redpanda

**ClickHouse** — two supported shapes (identical to GCP):

- **ClickHouse Cloud (recommended for production):** create a service on AWS, note its HTTPS host and
  port (usually `:8443`), and the `tally` user's password. The gateway + web talk to it over HTTP(S).
- **In-cluster StatefulSet (EKS staging only):** set `clickhouse.mode=statefulset` in the chart.
  Apply the canonical DDL (`db/clickhouse/*.sql`) via an initdb ConfigMap mounted at
  `/docker-entrypoint-initdb.d`. Single-node, not HA. On ECS-Fargate there is no in-cluster option —
  use ClickHouse Cloud.

**S3 bucket** for replay blobs (CTO-152):

```bash
aws s3 mb "s3://$ACCOUNT-ai-tally-replay" --region "$REGION"
```

**Streaming buffer (optional, CTO-37 `TALLY_INGEST_BUFFERED=true`):** the in-process burst buffer has
no external dependency. For a durable cross-instance buffer, provision **Amazon MSK** (managed Kafka)
or run **Redpanda** (self-hosted, Kafka-API compatible) in the VPC, and point the gateway's buffer at
it. Not required for the synchronous default; documented here as the AWS analog of the GCP note.

## 5. Secrets Manager — create the secrets

Store every secret value here; the task defs / manifests reference them **by ARN** and never contain
the value.

```bash
aws secretsmanager create-secret --name ai-tally-postgres-dsn \
  --secret-string "postgresql://tally:PASS@<rds-endpoint>:5432/tally"
aws secretsmanager create-secret --name ai-tally-clickhouse-password \
  --secret-string 'YOUR_CLICKHOUSE_PASSWORD'

# Provider keys are OPTIONAL — the gateway boots fail-soft without them (CTO-109). Skip if you use
# Bedrock (granted via the workload role) or have no outbound provider calls.
aws secretsmanager create-secret --name ai-tally-openai-api-key    --secret-string 'sk-...'
aws secretsmanager create-secret --name ai-tally-anthropic-api-key --secret-string 'sk-ant-...'
```

### The control-plane service token, and when to create it

The `/v1/tenant/*` control plane is gated on a shared **service token** (Initiative 1 §6) whenever
`gateway.config.requireApiKey` / `TALLY_REQUIRE_API_KEY` is true. The gateway refuses to boot with
the gate on and no token, so **the secret has to exist before the upgrade that turns the gate on**,
not after:

```bash
openssl rand -hex 32 | tr -d '\n' | \
  xargs -0 -I{} aws secretsmanager create-secret --name ai-tally-gateway-service-token \
    --secret-string {}
```

Then, and only then, point the chart at it:

```yaml
secretsManager:
  secrets:
    gatewayServiceToken: ai-tally-gateway-service-token
```

It ships **empty** in `values.yaml` on purpose. A non-empty default makes an ordinary `helm upgrade`
of an existing release mount a secret that does not exist yet, and the pods sit in
`ContainerCreating` with nothing in the logs to explain it. Left empty with `requireApiKey: true`,
the chart instead fails to render with a message naming this value. The **web tier does not need the
token at all** while the gate is off, which is why the ECS `web.taskdef.json` does not list it: add

```json
{ "name": "GATEWAY_SERVICE_TOKEN", "valueFrom": "arn:aws:secretsmanager:REGION:ACCOUNT:secret:ai-tally-gateway-service-token" }
```

to that task def's `secrets` block in the same change that turns the gate on. The gateway reads the
same value as `TALLY_GATEWAY_SERVICE_TOKEN`; the two must match exactly, or the dashboard sends no
`Authorization` and every control-plane read 401s.

> **Not deploy-time secrets:** the **Stripe** webhook signing secret is pasted per-tenant in the
> dashboard and persisted in Postgres (`db/postgres/0003_tenant_stripe_config.sql`) — protect it by
> protecting RDS, not via an env var. Per-tenant **HMAC** user-id keys (CTO-74) live in the gateway's
> runtime `HmacKeyRegistry`, provisioned per-tenant — also not a deploy secret.

## 6. Identity — task role (ECS) / IRSA (EKS)

Create **one workload IAM role** and grant it exactly what the app needs. No static access key is
ever created. The permissions policy is `ecs/iam/task-role-policy.json` (S3 replay + per-tenant HMAC
secrets + KMS + connector role assumption + Cost Explorer + Bedrock + Secrets Manager read); reuse it
verbatim for both paths, only the **trust** differs.

**Every IAM document here carries placeholders and none of them is usable unedited.** They are the
same tokens the task definitions use, so one `sed` line covers a file: `__ACCOUNT__`, `__REGION__`,
`__REPLAY_BUCKET__` (the same bucket `gateway.taskdef.json` names) and `__KMS_KEY_ID__`. The
bucket used to be spelled `my-org-ai-tally-replay`, which reads like a real name and is not one; it
is a placeholder now so nobody grants their role access to somebody else's bucket.

```bash
export KMS_KEY_ID=YOUR_CMK_KEY_ID            # see "Which KMS key" below
export REPLAY_BUCKET=$ACCOUNT-ai-tally-replay

# The permissions policy (shared by both paths):
sed -e "s/__ACCOUNT__/$ACCOUNT/g" -e "s/__REGION__/$REGION/g" \
    -e "s/__REPLAY_BUCKET__/$REPLAY_BUCKET/g" \
    -e "s/__KMS_KEY_ID__/$KMS_KEY_ID/g" \
    deploy/aws/ecs/iam/task-role-policy.json > /tmp/ai-tally-workload.json
aws iam create-policy --policy-name ai-tally-workload \
  --policy-document file:///tmp/ai-tally-workload.json
export WORKLOAD_POLICY_ARN=arn:aws:iam::$ACCOUNT:policy/ai-tally-workload
```

**ECS** — create the task role (trusted by `ecs-tasks.amazonaws.com`) and the execution role. The
trust policy pins `aws:SourceAccount` so no other account's ECS can assume these roles, which is
why it needs substituting too:

```bash
sed "s/__ACCOUNT__/$ACCOUNT/g" deploy/aws/ecs/iam/ecs-tasks-trust-policy.json > /tmp/ecs-trust.json

# Task role = the app's own identity (S3 / tenant HMAC secrets / Cost Explorer / Bedrock).
aws iam create-role --role-name ai-tally-workload \
  --assume-role-policy-document file:///tmp/ecs-trust.json
aws iam attach-role-policy --role-name ai-tally-workload --policy-arn "$WORKLOAD_POLICY_ARN"

# Execution role = what Fargate needs to START a task (ECR pull, logs, secret injection).
sed -e "s/__ACCOUNT__/$ACCOUNT/g" -e "s/__REGION__/$REGION/g" \
    -e "s/__KMS_KEY_ID__/$KMS_KEY_ID/g" \
    deploy/aws/ecs/iam/execution-role-policy.json > /tmp/ai-tally-execution.json
aws iam create-role --role-name ai-tally-ecs-execution \
  --assume-role-policy-document file:///tmp/ecs-trust.json
aws iam put-role-policy --role-name ai-tally-ecs-execution --policy-name ai-tally-ecs-execution \
  --policy-document file:///tmp/ai-tally-execution.json
```

### What each grant is for, and what to delete

| Statement | Who uses it | Drop it when |
|---|---|---|
| `ReplayBucketS3` | `S3ReplayBlobStore` (`TALLY_REPLAY_BLOB_BACKEND=s3`) | you run the `memory` backend |
| `TenantHmacSecrets` | `SecretManagerKeyProvider` (`TALLY_HMAC_KEY_PROVIDER=kms`) | never, on a multi-tenant instance: this is what holds the identifiers-by-hash invariant up |
| `SecretsManagerKmsUse` | the same provider, and any CMK-encrypted secret | your secrets use the AWS-managed `aws/secretsmanager` key (then delete the statement rather than leaving a dangling key id) |
| `AssumeTenantConnectorRoles` | a per-tenant `credentials_ref` holding a role ARN (`docs/connector-aws-cost-explorer.md`) | every tenant uses `aws-default-chain` |
| `CostExplorerRead` | the AWS Cost Explorer compute connector | no tenant enables it |
| `BedrockInvoke` | nothing in the codebase today | now, unless you are staging for Bedrock: it is an unearned privilege |

The execution role's ECR grants are now scoped to the three `ai-tally/*` repositories rather than
`*`; `ecr:GetAuthorizationToken` stays on `*` because it does not support resource-level permissions.

### Which KMS key

`__KMS_KEY_ID__` is the customer-managed key your Secrets Manager secrets are encrypted with. If
you left them on the AWS-managed `aws/secretsmanager` key, delete the `SecretsManagerKmsUse` and
`SecretsInjectionKmsDecrypt` statements instead of substituting: the AWS-managed key is usable by
principals in the account that hold the Secrets Manager permission, so a grant would be redundant,
and a policy naming a key that does not exist is a policy nobody can read. If you DO use a CMK, both
statements are required, they carry a `kms:ViaService` condition so the roles cannot use the key for
anything but Secrets Manager, and the key policy must also allow the account root or these roles.

### Per-tenant HMAC keys (CTO-336)

With `TALLY_HMAC_KEY_PROVIDER=kms`, provisioning a tenant creates one Secrets Manager secret named
`ai-tally/tenant-hmac/<uuid>` holding 32 random bytes, and stores **only** its ARN plus a version
selector (`.../v1`) in `tenants.hash_salt_kek_ref`, whose `no_raw_secret` CHECK bounds it to under
512 characters. That is the credentials-by-reference invariant in `CLAUDE.md`, made real: the
control-plane database holds a pointer that is useless without IAM permission on the ARN.

Two operational notes:

- **Do not enable AWS managed rotation on these secrets.** Rotating a tenant's HMAC key changes every
  hash computed after it, so it is a product decision about historical joins, not a key-store
  operation, and ai-tally does not ship rotation yet. The reference deliberately pins the `v1`
  staging label rather than `AWSCURRENT`, so a rotation that moved `AWSCURRENT` would not silently
  break existing hashes, but it would also not do anything useful.
- **Failure is honest.** If the gateway cannot reach Secrets Manager, or the role lacks
  `TenantHmacSecrets`, `POST /v1/tenant/provision` returns **503** and writes no tenant row, and
  `GET /v1/tenant/hmac-key` returns 503 rather than 404. There is deliberately no fallback to a
  locally generated key: it would produce hashes that look valid and are not the tenant's own.
  Clerk retries the webhook and provisioning is idempotent, so fix the IAM gap and the retry lands.

**EKS** — bind the same permissions to the KSA the chart creates (`ai-tally` in namespace
`ai-tally`) via IRSA. Easiest with `eksctl`, which writes the OIDC trust policy for you:

```bash
eksctl utils associate-iam-oidc-provider --cluster ai-tally --approve
eksctl create iamserviceaccount \
  --cluster ai-tally --namespace ai-tally --name ai-tally \
  --role-name ai-tally-workload \
  --attach-policy-arn "$WORKLOAD_POLICY_ARN" \
  --approve
# Note the role ARN it prints; pass it as serviceAccount.roleArn (step 7B). Because eksctl already
# created the KSA, set serviceAccount.create=false, OR let the chart create it and instead apply the
# trust manually from ecs/iam/irsa-trust-policy.json (replace ACCOUNT/REGION/OIDC_ID).
```

The IRSA role also needs the Secrets Store CSI driver's AWS provider installed (step 7B).

## 7. Deploy

### First, the preflight (CTO-360)

```bash
cd infra && make prod-preflight ENV=../prod.env      # or: scripts/prod-preflight.sh --env prod.env
```

Put both sides in the file: the gateway's settings and the Vercel project's. It checks the handful
of settings whose failures are confusing and unrelated-looking, and each finding names the symptom
it prevents: `TALLY_REQUIRE_API_KEY` false leaves the control plane ungated so an unauthenticated
`POST /v1/tenant/provision` creates a real tenant; the web tier's `GATEWAY_SERVICE_TOKEN` and the
gateway's `TALLY_GATEWAY_SERVICE_TOKEN` differing 401s every control-plane call; a leftover
`TALLY_DEV_TENANT` pins one tenant for whoever loads the dashboard; `TALLY_HMAC_KEY_PROVIDER` unset
falls back to the local provider, which holds per-tenant key material in configuration rather than
by reference; a missing Clerk svix signing secret rejects `organization.created` so no tenant is
ever provisioned; and an unreachable ClickHouse does not error but paints mock data.

It uses no AWS credentials, makes no AWS API call, and never prints a secret value: secrets are
compared and reported by SHA-256 prefix and length. It exits non-zero and says exactly what to fix.
`ARGS=--no-probe` skips the outbound reachability check.

### Option A — ECS-Fargate (primary)

Create the cluster and the CloudWatch log groups, then register the task defs and create the
services. Deploy the **gateway first**, wire an ALB target group to it, capture its URL, then deploy
web pointed at it.

```bash
aws ecs create-cluster --cluster-name ai-tally

# Log groups, WITH A RETENTION. The task definitions set `awslogs-create-group: false` on purpose:
# the awslogs driver has no retention option, so a group it auto-creates keeps every line forever at
# full price and nobody notices. Create the groups yourself and set a retention once.
for g in gateway web edge-proxy; do
  aws logs create-log-group --log-group-name "/ecs/ai-tally-$g" --region "$REGION"
  aws logs put-retention-policy --log-group-name "/ecs/ai-tally-$g" \
    --retention-in-days 30 --region "$REGION"
done

# Substitute placeholders and register the gateway task def:
sed -e "s/__ACCOUNT__/$ACCOUNT/g" -e "s/__REGION__/$REGION/g" \
    -e "s/__CLICKHOUSE_HOST__/YOUR_CLICKHOUSE_HOST/g" \
    -e "s/__REPLAY_BUCKET__/$ACCOUNT-ai-tally-replay/g" \
    deploy/aws/ecs/gateway.taskdef.json > /tmp/gateway.taskdef.json
aws ecs register-task-definition --cli-input-json file:///tmp/gateway.taskdef.json

# Create the gateway service (edit subnets/SGs/targetGroupArn in the file first):
aws ecs create-service --cli-input-json file://deploy/aws/ecs/gateway.service.json

# After the gateway is reachable behind its ALB, capture GATEWAY_URL (the ALB DNS/HTTPS URL), then:
sed -e "s/__ACCOUNT__/$ACCOUNT/g" -e "s/__REGION__/$REGION/g" \
    -e "s#__GATEWAY_URL__#https://YOUR_GATEWAY_ALB#g" \
    -e "s#__CLICKHOUSE_URL__#https://YOUR_CLICKHOUSE_HOST:8443#g" \
    deploy/aws/ecs/web.taskdef.json > /tmp/web.taskdef.json
aws ecs register-task-definition --cli-input-json file:///tmp/web.taskdef.json
aws ecs create-service --cli-input-json file://deploy/aws/ecs/web.service.json
```

Then the edge proxy (CTO-339). It sits in the customer's LLM request path, so it runs on its own
listener rule, at two tasks minimum, and never scales to zero: its outage is an outage of someone
else's product, unlike the gateway (whose SDK buffers and retries) or the dashboard (whose outage
loses nothing).

```bash
sed -e "s/__ACCOUNT__/$ACCOUNT/g" -e "s/__REGION__/$REGION/g" \
    -e "s#__GATEWAY_URL__#https://ingest.YOUR_DOMAIN#g" \
    deploy/aws/ecs/edge-proxy.taskdef.json > /tmp/edge-proxy.taskdef.json
aws ecs register-task-definition --cli-input-json file:///tmp/edge-proxy.taskdef.json
aws ecs create-service --cli-input-json file://deploy/aws/ecs/edge-proxy.service.json
```

### Gateway settings this bundle expects (CTO-336 / CTO-337 / CTO-268)

Three gateway environment variables belong on the ECS gateway task and are not in
`gateway.taskdef.json` yet (that file is owned by the image-publishing PR; add them there, or set
them in your IaC, before you rely on any of the three):

| Variable | Set it to | Why |
|---|---|---|
| `TALLY_ENV` | `production` | The gateway had no notion of a deployment environment, so nothing stopped `TALLY_REQUIRE_API_KEY=false` from shipping. With this set, a gateway with authentication off refuses to boot unless you also set `TALLY_ALLOW_INSECURE_NO_AUTH=1`. It is the gateway half of the web tier's guard, and it reuses the same opt-in variable. `gateway.taskdef.json` already sets `TALLY_REQUIRE_API_KEY=true`, so this is a backstop against a later edit rather than today's exposure. |
| `TALLY_HMAC_KEY_PROVIDER` | `kms` | Selects the AWS Secrets Manager provider above. Left unset, the gateway uses the LOCAL provider, whose per-tenant material is derived from a root secret in configuration, which is exactly what credentials-by-reference forbids on a multi-tenant instance. |
| `TALLY_CORS_ALLOWED_ORIGINS` | your dashboard's origin, e.g. `https://app.YOUR_DOMAIN` | The dashboard runs on Vercel and the gateway behind this ALB, so they are different origins. No browser code calls the gateway directly today (the dashboard's gateway calls all run server-side), so this is preparation rather than a fix for a live break. Unset means the local dev origins only, which admits nobody in a deployment: fail closed, not open. A wildcard is refused at boot. |

`TALLY_HMAC_SECRETS_KMS_KEY_ID` is optional and only needed if you want tenant HMAC secrets encrypted
with your CMK rather than the AWS-managed key.

Notes on the task defs:
- **Secrets** are injected from Secrets Manager by Fargate at task start via `secrets[].valueFrom`
  (a secret ARN) — the value never appears in the file or in the registered task def. The **execution
  role** must be able to read them (`execution-role-policy.json`).
- Provider-key secret entries are optional — delete them from `gateway.taskdef.json` if you did not
  create those secrets (or if you use Bedrock via the task role instead).
- **All three pin `cpuArchitecture: ARM64`.** Push ARM64 images (step 1) or every task fails to
  start with an image-manifest error.
- **The edge proxy has no container `healthCheck`, deliberately.** Its image is `FROM scratch`: the
  static binary and the CA roots, and nothing else. There is no `/bin/sh` for `CMD-SHELL` and no
  `curl`, `wget` or `python` for `CMD`, so any container health check copied from
  `gateway.taskdef.json` fails on every attempt, the task is killed as unhealthy, and the service
  cycles forever with nothing in the logs that names the cause. Liveness is the ALB target group's
  job instead, against `GET /healthz` on 8088, which
  `infra/edge-proxy/cmd/edge-proxy/main.go` serves as a plain `200 ok`. The same note is carried in
  the task definition's `dockerLabels` so it is visible in the file being edited.
- Fronting is an **ALB**: create a target group per tier (`ai-tally-gateway` on 8080, `ai-tally-web`
  on 3000, `ai-tally-edge-proxy` on 8088; health-check paths `/healthz`, `/` and `/healthz`), an
  HTTPS listener, and put the target-group ARNs into the `*.service.json` files. For internal-only,
  use an internal ALB. Keep the proxy on its own host-based rule
  (`llm.<domain>`, separate from `ingest.<domain>`): it forwards to provider APIs with real keys and
  should never share a rule with ingest.

```bash
# The proxy's health check is shallow and fast on purpose, so a bad task drains quickly out of a
# path that is somebody's production LLM traffic.
aws elbv2 create-target-group \
  --name ai-tally-edge-proxy --protocol HTTP --port 8088 \
  --vpc-id vpc-YOURS --target-type ip \
  --health-check-protocol HTTP --health-check-path /healthz \
  --health-check-interval-seconds 10 --health-check-timeout-seconds 5 \
  --healthy-threshold-count 2 --unhealthy-threshold-count 2 \
  --matcher HttpCode=200
```

### Option B — EKS (Helm)

Create the cluster (with OIDC for IRSA), install the Secrets Store CSI driver + AWS provider, then
install the chart with your overrides.

```bash
eksctl create cluster --name ai-tally --region "$REGION" --nodes 2 --with-oidc

# Secrets Store CSI driver + AWS provider (installs the DaemonSet + SecretProviderClass CRD):
helm repo add secrets-store-csi-driver https://kubernetes-sigs.github.io/secrets-store-csi-driver/charts
helm install csi-secrets-store secrets-store-csi-driver/secrets-store-csi-driver \
  --namespace kube-system --set syncSecret.enabled=true
kubectl apply -f https://raw.githubusercontent.com/aws/secrets-store-csi-driver-provider-aws/main/deployment/aws-provider-installer.yaml

# IRSA (step 6) must be done so the KSA can read Secrets Manager. Then:
cp deploy/aws/helm/ai-tally-eks/values-eks.example.yaml my-values.yaml
$EDITOR my-values.yaml     # fill in aws.region/accountId, serviceAccount.roleArn, image repos, CH host

helm upgrade --install ai-tally deploy/aws/helm/ai-tally-eks \
  --namespace ai-tally --create-namespace \
  -f my-values.yaml
```

The chart renders: an IRSA-annotated ServiceAccount, a SecretProviderClass that syncs the Secrets
Manager secrets into a Kubernetes Secret, gateway + web Deployments/Services, optional HPAs, and —
if `clickhouse.mode=statefulset` — an in-cluster ClickHouse. See `templates/NOTES.txt` (printed on
install) for the smoke-test commands. If you created the KSA with `eksctl create iamserviceaccount`,
set `serviceAccount.create=false` so Helm reuses it.

## 8. Smoke test

```bash
# ECS (behind the ALB):
curl -s "https://YOUR_GATEWAY_ALB/healthz"          # {"status":"ok"}
curl -s "https://llm.YOUR_DOMAIN/healthz"           # ok      (the edge proxy, plain text)

# EKS:
kubectl -n ai-tally port-forward svc/ai-tally-gateway 8080:8080 &
curl -s localhost:8080/healthz                      # {"status":"ok"}
```

Then send a batch (the same payload as `RUNNING.md` step 3, pointed at your gateway URL) and confirm
rows land in ClickHouse. Finally open the web URL — the **Cost**, **Features**, **Agents**, and
**Data Quality** pages should render your ingested spans.

## 9. Point the dashboard at production tenants

On the product path the dashboard does not pin a tenant at all: it resolves the caller's Clerk
organization to a tenant UUID through the gateway control plane. So leave `web.config.devTenant`
(EKS) and the `TALLY_DEV_TENANT` env (ECS `web.taskdef.json`) **empty**, and keep
`gateway.config.requireApiKey=true` (the cloud default) so ingest requires
`Authorization: Bearer <key>`. Seed keys with the gateway's `seed.py` against RDS.

Three things follow from that. The first two bite silently if you get them wrong; the third
stops the deployment dead, on purpose:

- **Control-plane calls need the service token.** Every `/v1/tenant/*` request carries
  `Authorization: Bearer $TALLY_GATEWAY_SERVICE_TOKEN`, and with `requireApiKey` on the gateway
  refuses to boot when the token is empty rather than serve an open control plane. The gateway
  reads it as `TALLY_GATEWAY_SERVICE_TOKEN` and the web tier reads the same secret as
  `GATEWAY_SERVICE_TOKEN`; both taskdefs and both charts reference the
  `ai-tally-gateway-service-token` Secrets Manager entry, so put a real value in it
  (`openssl rand -hex 32`) and never a committed literal. A mismatch surfaces as a `401` on every
  dashboard control-plane write.

- **The dashboard refuses to boot with the escape hatch on.** `TALLY_DEV_TENANT` does not only pin a
  tenant, it turns the dashboard's authentication OFF completely: the Clerk middleware becomes a
  pass-through, no `ClerkProvider` is mounted, and `canManage()` returns true for every visitor, so
  API key mint / rotate / revoke is ungated too. Anyone with the URL sees every number in the system.
  A production build started with it set therefore prints an explanation and exits non-zero before
  it serves a single request, so the task crash-loops with the reason in its logs rather than coming
  up wide open. Serving with no authentication on purpose (a public demo of synthetic data behind
  access control you supply yourself) takes a second, deliberate variable as well,
  `TALLY_ALLOW_INSECURE_NO_AUTH=1` / `web.config.allowInsecureNoAuth: "1"`, and then every boot logs
  a standing warning. The check is `web/lib/authGuard.ts`, run from `web/instrumentation.ts`.

- **If you do pin a tenant, pin the UUID.** `TALLY_DEV_TENANT` / `web.config.devTenant` is a
  single-tenant demo escape hatch, and its value is bound straight into the ClickHouse read filter
  (`TenantId = ...`) while spans are tagged with the tenant UUID. The name `local-dev` therefore
  matches no rows: the stack comes up green and the dashboard renders empty. `make seed` prints the
  UUID to use. The older `TALLY_TENANT_ID` env is no longer read by the web tier at all.

## 10. Teardown

```bash
# ECS
for s in ai-tally-web ai-tally-gateway ai-tally-edge-proxy; do
  aws ecs update-service --cluster ai-tally --service "$s" --desired-count 0
  aws ecs delete-service --cluster ai-tally --service "$s" --force
done
aws ecs delete-cluster --cluster ai-tally

# EKS
helm uninstall ai-tally -n ai-tally
eksctl delete cluster --name ai-tally --region "$REGION"

# Shared backing stores + identity (irreversible — deletes data):
aws rds delete-db-instance --db-instance-identifier ai-tally-pg --skip-final-snapshot
aws s3 rb "s3://$ACCOUNT-ai-tally-replay" --force
for s in ai-tally-postgres-dsn ai-tally-clickhouse-password ai-tally-gateway-service-token \
         ai-tally-openai-api-key ai-tally-anthropic-api-key; do
  aws secretsmanager delete-secret --secret-id "$s" --force-delete-without-recovery
done
aws iam delete-role --role-name ai-tally-workload
aws iam delete-role --role-name ai-tally-ecs-execution
# CI push role (§1). Unset AWS_ECR_ROLE_ARN in the repository variables too, or CI fails on assume.
aws iam delete-role-policy --role-name ai-tally-ci-ecr-push --policy-name ai-tally-ci-ecr-push
aws iam delete-role --role-name ai-tally-ci-ecr-push
for g in gateway web edge-proxy; do
  aws logs delete-log-group --log-group-name "/ecs/ai-tally-$g"
done
# (detach/delete the ai-tally-workload policy and any ECR repos / ALB / target groups you created.)
```

---

## Open TODOs (documented, out of scope for CTO-159)

- **Terraform/CloudFormation IaC**: done in `terraform/` (CTO-335). VPC, RDS, S3, ECR, KMS, Secrets
  Manager, IAM, ALB, ACM, the ECS cluster and both services, plus the CI OIDC role from §1. Written
  and validated, **never applied against a real account**; see `terraform/README.md` for exactly what
  that leaves unproven. Still not covered there: the one-shot migration task, which needs the
  `schema_migrations` ledger first.
- **In-cluster ClickHouse DDL bootstrap (EKS)** — the StatefulSet path expects you to mount
  `db/clickhouse` as an initdb ConfigMap; a chart hook to build/apply it automatically is a follow-up.
- **ALB Ingress / TLS + custom domain** — the ECS services expect an ALB target group you create; the
  EKS chart ships ClusterIP Services (port-forward / your own AWS Load Balancer Controller Ingress).
  A managed-cert ALB Ingress and domain mapping are left to the operator.
- **MSK/Redpanda streaming buffer** — provisioning notes are here; binding the gateway's CTO-37 burst
  buffer to a durable Kafka-API broker is a follow-up.
- **Autoscaling tuning / multi-AZ HA for ClickHouse** — single-region defaults; explicitly out of scope.
```
