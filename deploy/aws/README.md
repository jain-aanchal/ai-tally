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

## What's in this directory

```
deploy/aws/
├── README.md                       this runbook
├── ecs/                            PRIMARY — ECS-on-Fargate task/service definitions
│   ├── gateway.taskdef.json        gateway task definition (Secrets Manager injection, task role)
│   ├── web.taskdef.json            web task definition
│   ├── gateway.service.json        gateway ECS service (Fargate, ALB target group)
│   ├── web.service.json            web ECS service
│   └── iam/
│       ├── task-role-policy.json          workload identity: S3 replay + Cost Explorer + Bedrock (+Secrets)
│       ├── execution-role-policy.json     ECS execution role: ECR pull + logs + secret injection
│       ├── ecs-tasks-trust-policy.json    trust policy for the ECS task/execution roles
│       └── irsa-trust-policy.json         trust policy for the EKS IRSA role (OIDC)
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

## 1. ECR — build & push images

The gateway build context is the **repo root** (its Dockerfile COPYs both the gateway and the SDK it
depends on); the web build context is `web/`.

```bash
aws ecr create-repository --repository-name ai-tally/gateway --region "$REGION"
aws ecr create-repository --repository-name ai-tally/web --region "$REGION"
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$ECR"

docker build -t "$ECR/ai-tally/gateway:$IMAGE_TAG" -f infra/gateway/Dockerfile .
docker build -t "$ECR/ai-tally/web:$IMAGE_TAG" web/
docker push "$ECR/ai-tally/gateway:$IMAGE_TAG"
docker push "$ECR/ai-tally/web:$IMAGE_TAG"
```

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

> **Not deploy-time secrets:** the **Stripe** webhook signing secret is pasted per-tenant in the
> dashboard and persisted in Postgres (`db/postgres/0003_tenant_stripe_config.sql`) — protect it by
> protecting RDS, not via an env var. Per-tenant **HMAC** user-id keys (CTO-74) live in the gateway's
> runtime `HmacKeyRegistry`, provisioned per-tenant — also not a deploy secret.

## 6. Identity — task role (ECS) / IRSA (EKS)

Create **one workload IAM role** and grant it exactly what the app needs. No static access key is
ever created. The permissions policy is `ecs/iam/task-role-policy.json` (S3 replay + Cost Explorer +
Bedrock + Secrets Manager read); reuse it verbatim for both paths — only the **trust** differs.

```bash
# The permissions policy (shared by both paths):
aws iam create-policy --policy-name ai-tally-workload \
  --policy-document file://deploy/aws/ecs/iam/task-role-policy.json
export WORKLOAD_POLICY_ARN=arn:aws:iam::$ACCOUNT:policy/ai-tally-workload
```

**ECS** — create the task role (trusted by `ecs-tasks.amazonaws.com`) and the execution role:

```bash
# Task role = the app's own identity (S3 / Cost Explorer / Bedrock).
aws iam create-role --role-name ai-tally-workload \
  --assume-role-policy-document file://deploy/aws/ecs/iam/ecs-tasks-trust-policy.json
aws iam attach-role-policy --role-name ai-tally-workload --policy-arn "$WORKLOAD_POLICY_ARN"

# Execution role = what Fargate needs to START a task (ECR pull, logs, secret injection).
aws iam create-role --role-name ai-tally-ecs-execution \
  --assume-role-policy-document file://deploy/aws/ecs/iam/ecs-tasks-trust-policy.json
aws iam put-role-policy --role-name ai-tally-ecs-execution --policy-name ai-tally-ecs-execution \
  --policy-document file://deploy/aws/ecs/iam/execution-role-policy.json
```

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

### Option A — ECS-Fargate (primary)

Create the cluster and the CloudWatch log groups, then register the task defs and create the
services. Deploy the **gateway first**, wire an ALB target group to it, capture its URL, then deploy
web pointed at it.

```bash
aws ecs create-cluster --cluster-name ai-tally

# Substitute placeholders and register the gateway task def:
sed -e "s/ACCOUNT/$ACCOUNT/g" -e "s/REGION/$REGION/g" \
    -e "s/REPLACE_CLICKHOUSE_HOST/YOUR_CLICKHOUSE_HOST/g" \
    deploy/aws/ecs/gateway.taskdef.json > /tmp/gateway.taskdef.json
aws ecs register-task-definition --cli-input-json file:///tmp/gateway.taskdef.json

# Create the gateway service (edit subnets/SGs/targetGroupArn in the file first):
aws ecs create-service --cli-input-json file://deploy/aws/ecs/gateway.service.json

# After the gateway is reachable behind its ALB, capture GATEWAY_URL (the ALB DNS/HTTPS URL), then:
sed -e "s/ACCOUNT/$ACCOUNT/g" -e "s/REGION/$REGION/g" \
    -e "s#REPLACE_GATEWAY_URL#https://YOUR_GATEWAY_ALB#g" \
    -e "s#REPLACE_CLICKHOUSE_URL#https://YOUR_CLICKHOUSE_HOST:8443#g" \
    deploy/aws/ecs/web.taskdef.json > /tmp/web.taskdef.json
aws ecs register-task-definition --cli-input-json file:///tmp/web.taskdef.json
aws ecs create-service --cli-input-json file://deploy/aws/ecs/web.service.json
```

Notes on the task defs:
- **Secrets** are injected from Secrets Manager by Fargate at task start via `secrets[].valueFrom`
  (a secret ARN) — the value never appears in the file or in the registered task def. The **execution
  role** must be able to read them (`execution-role-policy.json`).
- Provider-key secret entries are optional — delete them from `gateway.taskdef.json` if you did not
  create those secrets (or if you use Bedrock via the task role instead).
- Fronting is an **ALB**: create a target group per tier (`ai-tally-gateway` on 8080, `ai-tally-web`
  on 3000, health-check paths `/healthz` and `/`), an HTTPS listener, and put the target-group ARNs
  into the `*.service.json` files. For internal-only, use an internal ALB.

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

# EKS:
kubectl -n ai-tally port-forward svc/ai-tally-gateway 8080:8080 &
curl -s localhost:8080/healthz                      # {"status":"ok"}
```

Then send a batch (the same payload as `RUNNING.md` step 3, pointed at your gateway URL) and confirm
rows land in ClickHouse. Finally open the web URL — the **Cost**, **Features**, **Agents**, and
**Data Quality** pages should render your ingested spans.

## 9. Point the dashboard at production tenants

The web tier defaults to tenant `local-dev`. For real tenants, set `web.config.tenantId` (EKS) or the
`TALLY_TENANT_ID` env (ECS `web.taskdef.json`), and keep `gateway.config.requireApiKey=true` (the
cloud default) so ingest requires `Authorization: Bearer <key>` — seed keys with the gateway's
`seed.py` against RDS.

## 10. Teardown

```bash
# ECS
aws ecs update-service --cluster ai-tally --service ai-tally-web --desired-count 0
aws ecs update-service --cluster ai-tally --service ai-tally-gateway --desired-count 0
aws ecs delete-service --cluster ai-tally --service ai-tally-web --force
aws ecs delete-service --cluster ai-tally --service ai-tally-gateway --force
aws ecs delete-cluster --cluster ai-tally

# EKS
helm uninstall ai-tally -n ai-tally
eksctl delete cluster --name ai-tally --region "$REGION"

# Shared backing stores + identity (irreversible — deletes data):
aws rds delete-db-instance --db-instance-identifier ai-tally-pg --skip-final-snapshot
aws s3 rb "s3://$ACCOUNT-ai-tally-replay" --force
for s in ai-tally-postgres-dsn ai-tally-clickhouse-password ai-tally-openai-api-key ai-tally-anthropic-api-key; do
  aws secretsmanager delete-secret --secret-id "$s" --force-delete-without-recovery
done
aws iam delete-role --role-name ai-tally-workload
aws iam delete-role --role-name ai-tally-ecs-execution
# (detach/delete the ai-tally-workload policy and any ECR repos / ALB / target groups you created.)
```

---

## Open TODOs (documented, out of scope for CTO-159)

- **Terraform/CloudFormation IaC** — this ticket ships ECS task defs + a Helm chart + `aws` CLI docs
  for v1; codify the account bootstrap (VPC, RDS, ClickHouse, Secrets Manager, IAM, ALB) as IaC in a
  follow-up.
- **S3 replay wiring** — the bucket + IAM are provisioned here and the gateway exposes
  `TALLY_REPLAY_BLOB_BACKEND`/`TALLY_REPLAY_S3_BUCKET` knobs, but an S3 replay blob-store backend
  itself (the AWS analog of the GCS backend added under CTO-152) is a follow-up; today the supported
  backends are `memory` and `gcs`.
- **In-cluster ClickHouse DDL bootstrap (EKS)** — the StatefulSet path expects you to mount
  `db/clickhouse` as an initdb ConfigMap; a chart hook to build/apply it automatically is a follow-up.
- **ALB Ingress / TLS + custom domain** — the ECS services expect an ALB target group you create; the
  EKS chart ships ClusterIP Services (port-forward / your own AWS Load Balancer Controller Ingress).
  A managed-cert ALB Ingress and domain mapping are left to the operator.
- **MSK/Redpanda streaming buffer** — provisioning notes are here; binding the gateway's CTO-37 burst
  buffer to a durable Kafka-API broker is a follow-up.
- **Autoscaling tuning / multi-AZ HA for ClickHouse** — single-region defaults; explicitly out of scope.
```
