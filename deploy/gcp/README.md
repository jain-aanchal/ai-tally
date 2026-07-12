# Deploying ai-tally on GCP (GKE or Cloud Run)

A from-zero runbook for running ai-tally's two application tiers — the **ingest gateway**
(FastAPI/uvicorn) and the **Next.js dashboard** — on Google Cloud, wired to GCP-managed backing
stores and GCP-native secrets + identity. This is the cloud counterpart to the local
[`RUNNING.md`](../../RUNNING.md); the pipeline shape is identical, only the backing services change:

```
 send_batch / SDK ──POST /v1/batches──▶  gateway (Cloud Run svc or GKE Deployment)
                                              │  auth → rate-limit → idempotency →
                                              │  validate → enrich cost → map to row
                                              ▼
                                         ClickHouse  (ClickHouse Cloud, or in-cluster StatefulSet)
                                              ▲
   browser ──▶ Next.js web ──Route Handler──┘   (queries ClickHouse live + calls the gateway)

 control plane:  Postgres  ──▶  Cloud SQL for Postgres
 replay blobs:   object store ─▶ GCS bucket (CTO-152)
 secrets:        provider keys / DB creds ─▶ Secret Manager  (consumed via Workload Identity)
```

Everything here is **additive** — it does not touch `infra/docker-compose.yml`, the app source, or the
local dev flow. It reuses the conventions of the existing `infra/edge-proxy` Helm chart.

## What's in this directory

```
deploy/gcp/
├── README.md                       this runbook
├── helm/ai-tally/                  GKE Helm chart (gateway + web + optional ClickHouse StatefulSet)
│   ├── Chart.yaml
│   ├── values.yaml                 all knobs, documented
│   ├── values-gke.example.yaml     a filled-in example override file
│   └── templates/                  Deployments, Services, ConfigMaps, ServiceAccount (Workload
│                                   Identity), SecretProviderClass (Secret Manager via CSI), HPAs
└── cloudrun/                       Cloud Run service YAMLs
    ├── gateway.service.yaml
    └── web.service.yaml
```

The gateway image already ships (`infra/gateway/Dockerfile`). The web image is built from the
**new** `web/Dockerfile` added by this ticket (multi-stage Next.js standalone build).

## GKE vs Cloud Run — which to pick

| Pick **Cloud Run** when… | Pick **GKE** when… |
|---|---|
| You want the least ops: no cluster to manage, scale-to-N, per-request billing. | You already run a GKE cluster / want pods next to other in-cluster services. |
| Traffic is spiky or low and cold-start latency (~1–2s) is acceptable. | You need an in-cluster ClickHouse StatefulSet, DaemonSets, or fine pod control. |
| You're fine with the two tiers as independent managed services. | You want one Helm release, HPAs, and k8s-native networking/mesh. |

Both paths use the **same images**, the **same Secret Manager secrets**, and the **same
Workload-Identity-style** model (a Google Service Account with no exported JSON key). You can start on
Cloud Run and lift to GKE later without rebuilding anything.

> Backing stores (Cloud SQL, ClickHouse, GCS) are shared by both paths and are provisioned once
> (steps 3–4). Only the compute deploy (step 7) differs.

---

## 0. Prerequisites

- `gcloud` CLI authenticated (`gcloud auth login`) with a project set (`gcloud config set project PROJECT`).
- Billing enabled on the project.
- For GKE: `kubectl` and `helm` (v3).
- Shell variables used throughout (edit and export):

```bash
export PROJECT=my-project
export REGION=us-central1
export AR_REPO=ai-tally                      # Artifact Registry repo name
export IMAGE_TAG=1.0.0                        # or a git sha
export AR=$REGION-docker.pkg.dev/$PROJECT/$AR_REPO
```

## 1. Enable APIs

```bash
gcloud services enable \
  artifactregistry.googleapis.com \
  run.googleapis.com \
  container.googleapis.com \
  sqladmin.googleapis.com \
  secretmanager.googleapis.com \
  cloudbuild.googleapis.com \
  storage.googleapis.com \
  --project "$PROJECT"
```

## 2. Artifact Registry — build & push images

Create the repo, then build both images. The gateway build context is the **repo root** (its
Dockerfile COPYs both the gateway and the SDK it depends on); the web build context is `web/`.

```bash
gcloud artifacts repositories create "$AR_REPO" \
  --repository-format=docker --location="$REGION" \
  --description="ai-tally images"

# Option A — Cloud Build (no local Docker needed):
gcloud builds submit --tag "$AR/gateway:$IMAGE_TAG" --file infra/gateway/Dockerfile .
gcloud builds submit --tag "$AR/web:$IMAGE_TAG" web/

# Option B — local Docker:
gcloud auth configure-docker "$REGION-docker.pkg.dev"
docker build -t "$AR/gateway:$IMAGE_TAG" -f infra/gateway/Dockerfile .
docker build -t "$AR/web:$IMAGE_TAG" web/
docker push "$AR/gateway:$IMAGE_TAG"
docker push "$AR/web:$IMAGE_TAG"
```

## 3. Provision Cloud SQL (Postgres control plane)

```bash
gcloud sql instances create ai-tally-pg \
  --database-version=POSTGRES_16 --tier=db-custom-1-3840 \
  --region="$REGION" --storage-size=20GB

gcloud sql databases create tally --instance=ai-tally-pg
gcloud sql users create tally --instance=ai-tally-pg --password='CHOOSE_A_STRONG_PASSWORD'

# The value you'll store as the postgresDsn secret depends on the compute target:
#   GKE (Cloud SQL Auth Proxy sidecar, TCP on localhost):
#     postgresql://tally:PASS@127.0.0.1:5432/tally
#   Cloud Run (platform-provided unix socket):
#     postgresql://tally:PASS@/tally?host=/cloudsql/PROJECT:REGION:ai-tally-pg
export INSTANCE_CONNECTION_NAME="$PROJECT:$REGION:ai-tally-pg"
```

Apply the control-plane DDL (`db/postgres/*.sql`) once — e.g. connect through the Cloud SQL Auth
Proxy locally and run the migrations in order, or use `gcloud sql import`.

## 4. Provision ClickHouse + the GCS replay bucket

**ClickHouse** — two supported shapes:

- **ClickHouse Cloud (recommended for production):** create a service, note its HTTPS host and port
  (usually `:8443`), and the `tally` user's password. The gateway + web talk to it over HTTP(S).
- **In-cluster StatefulSet (GKE staging only):** set `clickhouse.mode=statefulset` in the chart.
  Apply the canonical DDL (`db/clickhouse/*.sql`) via an initdb ConfigMap mounted at
  `/docker-entrypoint-initdb.d` — the same files docker-compose mounts. Single-node, not HA.

**GCS bucket** for replay blobs (CTO-152):

```bash
gcloud storage buckets create gs://$PROJECT-ai-tally-replay --location="$REGION"
```

## 5. Secret Manager — create the secrets

Store every secret value here; the manifests reference them **by name** and never contain the value.

```bash
# Postgres DSN (use the GKE or Cloud Run form from step 3, matching your target).
printf 'postgresql://tally:PASS@127.0.0.1:5432/tally' | \
  gcloud secrets create ai-tally-postgres-dsn --data-file=-

printf 'YOUR_CLICKHOUSE_PASSWORD' | \
  gcloud secrets create ai-tally-clickhouse-password --data-file=-

# Provider keys are OPTIONAL — the gateway boots fail-soft without them (CTO-109). Skip if unused.
printf 'sk-...'      | gcloud secrets create ai-tally-openai-api-key    --data-file=-
printf 'sk-ant-...'  | gcloud secrets create ai-tally-anthropic-api-key --data-file=-
```

> **Not deploy-time secrets:** the **Stripe** webhook signing secret is pasted per-tenant in the
> dashboard and persisted in Postgres (`db/postgres/0003_tenant_stripe_config.sql`) — protect it by
> protecting Cloud SQL, not via an env var. Per-tenant **HMAC** user-id keys (CTO-74) live in the
> gateway's runtime `HmacKeyRegistry`, provisioned per-tenant — also not a deploy secret.

## 6. Identity — Workload Identity (GKE) / runtime SA (Cloud Run)

Create one Google Service Account and grant it exactly what the workload needs. No JSON key is ever
created or downloaded.

```bash
gcloud iam service-accounts create ai-tally-workload \
  --display-name="ai-tally workload"
export GSA="ai-tally-workload@$PROJECT.iam.gserviceaccount.com"

# Access to each secret (or grant project-wide secretAccessor if you prefer).
for s in ai-tally-postgres-dsn ai-tally-clickhouse-password ai-tally-openai-api-key ai-tally-anthropic-api-key; do
  gcloud secrets add-iam-policy-binding "$s" \
    --member="serviceAccount:$GSA" --role="roles/secretmanager.secretAccessor" 2>/dev/null || true
done

# Cloud SQL client + GCS on the replay bucket.
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:$GSA" --role="roles/cloudsql.client"
gcloud storage buckets add-iam-policy-binding gs://$PROJECT-ai-tally-replay \
  --member="serviceAccount:$GSA" --role="roles/storage.objectAdmin"
```

**GKE only** — create the cluster with Workload Identity, install the Secrets Store CSI driver, and
bind the KSA the chart creates (`ai-tally` in namespace `ai-tally`) to the GSA:

```bash
gcloud container clusters create ai-tally \
  --region="$REGION" --workload-pool="$PROJECT.svc.id.goog" --num-nodes=2
gcloud container clusters get-credentials ai-tally --region="$REGION"

# Secrets Store CSI driver + GCP provider (installs the DaemonSet + SecretProviderClass CRD):
helm repo add secrets-store-csi-driver https://kubernetes-sigs.github.io/secrets-store-csi-driver/charts
helm install csi-secrets-store secrets-store-csi-driver/secrets-store-csi-driver \
  --namespace kube-system --set syncSecret.enabled=true
kubectl apply -f https://raw.githubusercontent.com/GoogleCloudPlatform/secrets-store-csi-driver-provider-gcp/main/deploy/provider-gcp-plugin.yaml

# Bind the KSA (created by the chart on first install) to the GSA:
kubectl create namespace ai-tally
gcloud iam service-accounts add-iam-policy-binding "$GSA" \
  --role="roles/iam.workloadIdentityUser" \
  --member="serviceAccount:$PROJECT.svc.id.goog[ai-tally/ai-tally]"
```

**Cloud Run only** — use a runtime SA (`ai-tally-run@...`) instead; grant it the same
`secretAccessor` + `cloudsql.client` roles. The service YAMLs already set `serviceAccountName` to it.

## 7. Deploy

### Option A — GKE (Helm)

Copy the example overrides and fill in the CAPS values (project, GSA, image repos, Cloud SQL
instance, ClickHouse host):

```bash
cp deploy/gcp/helm/ai-tally/values-gke.example.yaml my-values.yaml
$EDITOR my-values.yaml

helm upgrade --install ai-tally deploy/gcp/helm/ai-tally \
  --namespace ai-tally --create-namespace \
  -f my-values.yaml
```

The chart renders: a Workload-Identity-annotated ServiceAccount, a SecretProviderClass that syncs the
Secret Manager secrets into a Kubernetes Secret, gateway + web Deployments/Services (the gateway with
a Cloud SQL Auth Proxy sidecar), optional HPAs, and — if `clickhouse.mode=statefulset` — an
in-cluster ClickHouse. See `templates/NOTES.txt` (printed on install) for the smoke-test commands.

### Option B — Cloud Run

Substitute the placeholders in the service YAMLs and apply. Deploy the gateway first, capture its
URL, then deploy the web service pointed at it:

```bash
sed -e "s/PROJECT/$PROJECT/g" -e "s/REGION/$REGION/g" -e "s/TAG/$IMAGE_TAG/g" \
    -e "s#PROJECT:REGION:INSTANCE#$INSTANCE_CONNECTION_NAME#g" \
    -e "s/REPLACE_CLICKHOUSE_HOST/YOUR_CLICKHOUSE_HOST/g" \
    deploy/gcp/cloudrun/gateway.service.yaml \
  | gcloud run services replace - --region "$REGION"

GATEWAY_URL=$(gcloud run services describe ai-tally-gateway --region "$REGION" --format='value(status.url)')

sed -e "s/PROJECT/$PROJECT/g" -e "s/REGION/$REGION/g" -e "s/TAG/$IMAGE_TAG/g" \
    -e "s#REPLACE_GATEWAY_URL#$GATEWAY_URL#g" \
    -e "s#REPLACE_CLICKHOUSE_URL#https://YOUR_CLICKHOUSE_HOST:8443#g" \
    deploy/gcp/cloudrun/web.service.yaml \
  | gcloud run services replace - --region "$REGION"
```

Grant invoker access as your access model requires (e.g. `--member=allUsers` for a public dashboard,
or an IAP/identity-aware setup for internal-only).

## 8. Smoke test

```bash
# Cloud Run:
curl -s "$GATEWAY_URL/healthz"                      # {"status":"ok"}

# GKE:
kubectl -n ai-tally port-forward svc/ai-tally-gateway 8080:8080 &
curl -s localhost:8080/healthz                      # {"status":"ok"}
```

Then send a batch (the same payload as `RUNNING.md` step 3, pointed at your gateway URL) and confirm
rows land in ClickHouse. Finally open the web service URL — the **Cost**, **Features**, **Agents**,
and **Data Quality** pages should render your ingested `local-dev` spans.

## 9. Point the dashboard at production tenants

The web tier defaults to tenant `local-dev` (matching local dev). For real tenants, set
`web.config.tenantId` (GKE) or the `TALLY_TENANT_ID` env (Cloud Run), and enable
`gateway.config.requireApiKey=true` (the cloud default) so ingest requires
`Authorization: Bearer <key>` — seed keys with the gateway's `seed.py` against Cloud SQL.

---

## Open TODOs (documented, out of scope for CTO-153)

- **Terraform/Pulumi IaC** — this ticket ships Helm + `gcloud` docs for v1; codify the project
  bootstrap (APIs, Cloud SQL, ClickHouse, Secret Manager, IAM) as IaC in a follow-up.
- **In-cluster ClickHouse DDL bootstrap** — the StatefulSet path expects you to mount `db/clickhouse`
  as an initdb ConfigMap; a chart hook to build/apply that ConfigMap automatically is a follow-up.
- **Ingress/TLS + custom domain** — the chart ships ClusterIP Services (port-forward / your own
  Ingress). A managed-cert Ingress (GKE) and domain mapping (Cloud Run) are left to the operator.
- **GCS replay wiring** — the bucket + IAM are provisioned here; binding the gateway's replay blob
  store to GCS is tracked under CTO-152.
- **Autoscaling tuning / multi-region HA** — explicitly out of scope (single-region, default HPAs).
