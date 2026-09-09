# Scope: the AWS bundle

**Status: proposal.** Make AWS a first-class, packaged deployment target, so standing ai-tally up on
AWS is running a bundle rather than reading a runbook and typing forty `aws` commands.

## What this is and is not

This doc is about the **artifact set**: the infrastructure code, images, release pipeline and gates
that turn `deploy/aws/` from a well-researched plan into something reproducible. It is not
`docs/self-hosted-scope.md`, which decides what *we* deploy for *our own* single-tenant instance and
how it links to our website; that doc picks the architecture and this one packages it, so where they
disagree, that one wins on architecture and this one wins on packaging. It is not
`docs/hosted-version-scope.md` either, which scopes a managed multi-tenant service; the bundle is a
prerequisite for that product but is useful long before it exists. The hosting decision (Vercel for
web, ECS Fargate for gateway and edge proxy, ClickHouse Cloud, RDS, S3, Secrets Manager, one region)
is settled and is not reopened here. A secondary section establishes what ai-tally already reads
*from* AWS as a cost source, because "the AWS integration" means both things and confusing them
wastes a planning cycle.

## What exists today, verified

Read from the files on `main` at `18bef84`, not inferred.

`deploy/aws/README.md` is a from-zero runbook: ECR, VPC notes, RDS, ClickHouse, the S3 replay bucket,
Secrets Manager, IAM, deploy, smoke test, teardown. It is detailed and mostly correct. Under it,
`deploy/aws/ecs/` holds `gateway.taskdef.json`, `web.taskdef.json`, `gateway.service.json` and
`web.service.json`, plus four IAM documents in `deploy/aws/ecs/iam/`: `task-role-policy.json`
(S3 replay, Cost Explorer, Bedrock, Secrets Manager read), `execution-role-policy.json` (ECR pull,
CloudWatch Logs, secret injection), `ecs-tasks-trust-policy.json`, and `irsa-trust-policy.json` for
the EKS path. `deploy/aws/helm/ai-tally-eks/` is a complete Helm chart with gateway and web
Deployments, an IRSA-annotated ServiceAccount, a SecretProviderClass driving the Secrets Store CSI
driver, HPAs, and an optional in-cluster ClickHouse StatefulSet.

Both application images build today. `infra/gateway/Dockerfile` builds from the repo root and
installs the gateway with the `[s3]` extra, so boto3 is in the image. `infra/edge-proxy/Dockerfile`
is a two-stage static build shipping on `scratch` as a non-root numeric uid.

S3 replay storage is real and the runbook is stale about it. `S3ReplayBlobStore` exists in
`infra/gateway/src/gateway/replay_store.py`, selected by `replay_blob_backend=s3` and configured by
`replay_s3_bucket`, `replay_s3_prefix`, `replay_s3_region` and `replay_s3_endpoint` in
`infra/gateway/src/gateway/config.py`, using the AWS default credential chain so no key material is
handled in code. `deploy/aws/README.md`'s Open TODOs still say "today the supported backends are
`memory` and `gcs`", and `gateway.taskdef.json` still sets `TALLY_REPLAY_BLOB_BACKEND=memory`. Both
are wrong now and are a one-line fix each.

**There was no infrastructure-as-code anywhere in the repository.** A search across the tree for
`*.tf`, `*.tfvars`, `cdk.json`, `template.yaml`, `*.template.json` and `serverless.yml` returned
nothing. Everything above assumed a VPC, private and public subnets, security groups, an ALB with an
ACM certificate and two target groups, an RDS instance, an S3 bucket, ECR repositories, IAM roles and
Secrets Manager entries already exist. A task definition is not a deployment; it is the last ten
percent of one.

> **Update (CTO-335).** `deploy/aws/terraform/` now exists and creates all of the above, consuming
> `deploy/aws/ecs/*.taskdef.json` and `ecs/iam/*.json` as templates. It is `fmt`-clean, validates
> against `hashicorp/aws` v6.63.0, and passes 189 of 210 checkov checks with every remaining failure
> answered in its README. **It has never been applied against an AWS account**, so nothing in it is
> proven to stand up: no plan has run against a live provider, the RDS engine version and regional
> Fargate ARM64 availability are unchecked, and the one-shot migration task is still absent because
> the `schema_migrations` ledger does not exist yet. The gap this section describes is closed as
> *written*, not as *verified*.

**There was no image build or publish pipeline.** `.github/workflows/ci.yml` had exactly four jobs
(`python-sdk`, `gateway`, `edge-proxy`, `web`) and every one of them lints and tests. Nothing built
a container, tagged one, or pushed to ECR or GHCR. The runbook's `docker build` and
`docker push` were meant to be typed by a human on a laptop. Note also that
`infra/edge-proxy/deploy/helm/edge-proxy/values.yaml` defaults its image to
`ghcr.io/jain-aanchal/ai-tally-edge-proxy`, a registry nothing in this repo published to.

> **Update (phase 0).** CI now builds all three images, tags them with the commit SHA (plus `main`
> on a merge and `X.Y.Z` on a release tag, never `latest`), and pushes to GHCR and to ECR through an
> OIDC role with no long-lived key. The ECR half was gated on an `AWS_ECR_ROLE_ARN` repository
> variable because the role did not exist; `deploy/aws/terraform/bootstrap/` now creates it, from
> the same two IAM documents, so setting that variable is the whole remaining step. Also done in
> phase 0: `TALLY_REPLAY_BLOB_BACKEND` is `s3`, the stale "memory and gcs" line is gone, and
> `ai-tally-gateway-service-token` is in both IAM ARN lists.

**The edge proxy had no artifact under `deploy/` at all.** There was no ECS task definition, no
service definition, no target group, and no template in either Helm chart under `deploy/`. Its only
deployment artifact was `infra/edge-proxy/deploy/helm/edge-proxy/`, inside the component directory,
and it is not in `infra/docker-compose.yml` either. The chosen architecture puts the proxy on ECS
Fargate kept warm because it sits in the LLM hot path, and that component could not be deployed by
any documented path. `docs/self-hosted-scope.md` calls this the single largest undone piece of the
architecture and it is right.

> **Update (phase 0 and 4, partly).** `deploy/aws/ecs/edge-proxy.taskdef.json` and
> `edge-proxy.service.json` exist, with the deliberate absence of a container health check recorded
> in the file's `dockerLabels`, and the Terraform creates the service, its target group and its own
> host-based listener rule. The remaining phase 4 item is the test that a gateway outage does not add
> latency to a proxied request, which is still unwritten.

**Nobody has demonstrably run `deploy/aws/ecs/` end to end.** I could not verify this either way from
the repository. There is no recorded output, no CI job, no `deploy/aws/` changelog entry and no
commit message describing an executed deploy. The artifacts are plausible and internally consistent,
and that is all I can say. Budget for first-run friction accordingly.

## The supported topology

One region, everything in it. Component by component:

| Component | Where | Artifact today |
| --- | --- | --- |
| Dashboard | Vercel, function region pinned to the same AWS region | `deploy/vercel/README.md`, `web/vercel.json`. Not Terraform: see "What was built" below. |
| Ingest gateway | ECS Fargate behind a public ALB | `deploy/aws/ecs/gateway.*.json`, created by `terraform/modules/compute` |
| Edge proxy | ECS Fargate, kept warm, separate ALB listener | `deploy/aws/ecs/edge-proxy.*.json`, created by `terraform/modules/compute` |
| Telemetry | ClickHouse Cloud on AWS, public HTTPS on 8443 | none needed, and deliberately not Terraform |
| Control plane | RDS for PostgreSQL, private subnets | `terraform/modules/data`. Schema still applied by hand: there is no migration runner. |
| Replay bodies | S3 with a lifecycle policy | `S3ReplayBlobStore`, bucket and lifecycle in `terraform/modules/data` |
| Secrets | Secrets Manager, KMS for the HMAC root | `deploy/aws/ecs/iam/*`; `terraform/modules/data` creates the containers and the key, never the values |

Everything above marked as created by Terraform is created by code that has never been applied.


`web.taskdef.json` and `web.service.json` stay in the tree as the escape hatch for anyone who does
not want Vercel, and the bundle does not use them. Say so in the README rather than leaving two
equally weighted options.

### ECS, not EKS

**Ship ECS Fargate as the one supported path. Demote the EKS chart to unsupported and say so in
`deploy/aws/README.md`, or delete it.**

Three reasons, in order of weight. The EKS chart contradicts the decided architecture: its
`templates/clickhouse-statefulset.yaml` runs a single-node ClickHouse in the cluster with no HA and a
DDL bootstrap that the README itself lists as an unfinished follow-up, while the architecture says
managed ClickHouse Cloud. A supported path that ships a different data store than the supported data
store is not a second option, it is a second product. Second, EKS multiplies the IaC surface the
bundle has to own: a cluster, node groups or Fargate profiles, an OIDC provider, the Secrets Store
CSI driver and its AWS provider, the AWS Load Balancer Controller, plus everything ECS needs anyway.
Third, three deployable components across two orchestrators is four artifact sets to keep in step
with every config change, and nothing in CI would catch them drifting apart.

The counter-argument is real and worth naming: a team already running EKS would rather add a Helm
release than a new orchestrator, and `deploy/gcp/helm/ai-tally/` is the near-identical GKE chart, so
deleting the EKS one leaves the two clouds asymmetric. That argues for keeping the file, not for
supporting it. Mark it "community, unverified, not covered by the bundle or by CI", point it at
ClickHouse Cloud by making `clickhouse.mode=statefulset` a documented staging-only setting, and stop
treating it as an alternative in the ECS-versus-EKS table at the top of the README.

## The IaC question

**Recommendation: Terraform, one module set under `deploy/aws/terraform/`, with the existing JSON
task and service definitions consumed by it rather than replaced.**

Terraform over CDK because the bundle's audience includes people deploying ai-tally into their own
account, and Terraform is the format they are most likely to already run and review; CDK would put a
Node toolchain and a CloudFormation bootstrap in the path of a Python and Go project that does not
otherwise need either. Terraform over raw CloudFormation because the account bootstrap spans
resources CloudFormation handles awkwardly (ClickHouse Cloud is not an AWS resource at all, and a
Vercel project is not either, whereas both have Terraform providers if we ever want them in the same
plan). Terraform over "neither" because "neither" is what we have, and it is why nobody can say
whether the ECS path works.

What the module set must create, because nothing else does:

- Networking: VPC, two private subnets and two public subnets across two AZs, an internet gateway,
  NAT (or VPC endpoints for ECR, CloudWatch Logs, Secrets Manager and S3, which is cheaper and
  tighter for a workload that mostly talks to AWS), route tables, and four security groups (ALB,
  gateway task, proxy task, RDS).
- ECR repositories for `ai-tally/gateway` and `ai-tally/edge-proxy`, with a lifecycle policy.
- RDS: a Postgres 16 instance in the private subnets, not publicly accessible, with backups and a
  parameter group, plus the generated master password written straight into Secrets Manager so it is
  never a Terraform variable in a state file someone reads.
- S3: the replay bucket, with versioning, public access blocked, SSE, and the lifecycle rule the code
  deliberately does not manage.
- Secrets Manager entries for `ai-tally-postgres-dsn`, `ai-tally-clickhouse-password`,
  `ai-tally-gateway-service-token`, the optional provider keys, and the HMAC root, plus the KMS key
  that encrypts them.
- IAM: the workload role, the execution role and their policies, generated from the four documents in
  `deploy/aws/ecs/iam/` with the account, region and bucket name interpolated instead of the
  hard-coded `my-org-ai-tally-replay` that is in `task-role-policy.json` today.
- ALB, an ACM certificate with DNS validation, an HTTPS listener, and target groups for the gateway
  on 8080 and the proxy on 8088.
- ECS cluster, both services, both task definitions, CloudWatch log groups with a retention setting
  (the task defs use `awslogs-create-group`, which creates groups that never expire).
- The one-shot migration task definition described in the next section.

What stays hand-managed on purpose: the ClickHouse Cloud service, the Clerk production instance, the
Vercel project and its environment variables, and DNS if the zone lives outside the account. Each is
a different vendor's console, each is a once-per-environment action, and wrapping them in Terraform
buys less than it costs. Document them as prerequisites with the exact values the module needs as
inputs.

Keep the JSON files. Terraform's `aws_ecs_task_definition` can take a rendered container definition,
so `deploy/aws/ecs/*.taskdef.json` becomes a template the module fills in rather than a file a human
`sed`s. That preserves the one artifact that is already reviewed and correct.

### What was built (CTO-335)

`deploy/aws/terraform/` implements this section, with the decisions it left open resolved as follows.

**Structure.** Four modules (`network`, `data`, `iam`, `compute`) composed into one root, plus
`bootstrap/` as a genuinely separate root. Four modules because those are four different change
cadences and four different blast radii; one root because the coupling between them is dense enough
that four state files would buy isolation nobody asked for and cost an apply order somebody will
skip.

**State.** An S3 bucket created by `bootstrap/`, which keeps local state because it cannot hold its
own. **No DynamoDB table**: Terraform 1.11 promoted S3-native locking (`use_lockfile`) to GA, so the
lock is a `.tflock` object beside the state object and the chicken-and-egg problem shrinks to one
bucket. `bootstrap/` also carries the GitHub OIDC role, so unblocking CI's ECR push does not require
standing up a VPC first.

**Secrets.** Terraform creates secret *containers* and never a `secret_version`, because a
`secret_string` in a resource is a `secret_string` in state and state is readable by anyone who can
read the bucket. The RDS master password is minted and held by RDS itself
(`manage_master_user_password`), never by `random_password`. Per-tenant HMAC keys stay entirely with
the application: Terraform owns the KMS key and the IAM grant on `ai-tally/tenant-hmac/*` and nothing
under that prefix, because a Terraform-managed resource there would be destroyed as drift and take
every hash for that tenant with it.

**Not Terraform.** The line is drawn at the AWS account edge: ClickHouse Cloud, Vercel, Clerk and
an out-of-account DNS zone are documented prerequisites whose values become module inputs. Providers
exist for the first two; using them would put a second and third vendor's credentials in the same
plan and the same state to save one console visit per environment.

**NAT versus endpoints, corrected.** The bullet above frames these as alternatives. They are not.
Interface endpoints reach AWS services only, and this workload has to reach ClickHouse Cloud on 8443
and the provider APIs the edge proxy exists to forward to. With `assignPublicIp: DISABLED` a private
subnet without NAT reaches neither, and it fails as a runtime timeout rather than an apply error. So
NAT is required (asserted by a precondition on both services), the free S3 gateway endpoint is always
on because ECR layer pulls are S3 GETs, and the six interface endpoints are an off-by-default
addition whose per-endpoint-per-AZ hourly meter is traded against NAT's per-GB one. Which wins
depends on image-pull and log volume, both unmeasured. Still no dollar figures anywhere.

**Not built.** The one-shot migration task, because it needs the `schema_migrations` ledger from
phase 1 first and creating it here would ship a task that replays 32 files with no record of what
already ran. And a WAF on the proxy listener, which this doc asks for and which stays a named gap.

## Secrets, IAM and least privilege

The four policy documents cover the ordinary runtime. What they miss, concretely:

`task-role-policy.json` hard-codes `arn:aws:s3:::my-org-ai-tally-replay`, which is a placeholder no
reader will notice is not their bucket. It grants `bedrock:InvokeModel` on `Resource: "*"`, which is
broad for a metering service and should be scoped or dropped unless a deployment actually calls
Bedrock. It grants `ce:GetCostAndUsage` and friends, which is correct for the Cost Explorer connector
but only belongs on deployments that use it. Its `secretsmanager:GetSecretValue` list omits
`ai-tally-gateway-service-token`, which `gateway.taskdef.json` references, so the ARN list in the
task role and the execution role and the task definition are three places that must agree and
currently do not. It says nothing about KMS, so if the secrets are encrypted with a customer-managed
key the tasks cannot decrypt them. It grants no `athena:*`, `glue:*` or `s3:*` on the export bucket,
so the Athena export path is unauthorized under this role. It grants no `sts:AssumeRole`, which
`docs/connector-aws-cost-explorer.md` requires for any `credentials_ref` that is a role ARN rather
than the literal `aws-default-chain`.

`execution-role-policy.json` scopes logs to `/ecs/ai-tally-*`, which is right, and ECR to `*`, which
should narrow to the two repositories. Neither role has a `Condition` block; both would benefit from
`aws:SourceAccount` on the trust policies.

The blocker underneath all of this is `SecretManagerKeyProvider` in
`infra/gateway/src/gateway/tenant_provisioning.py`, whose `mint`, `delete` and `material` methods all
raise `NotImplementedError("wire the deployment's Secret Manager / KMS client")`. Only
`LocalKeyMaterialProvider` works. CLAUDE.md's invariant is that per-tenant HMAC keys are held by
reference in Secrets Manager or KMS, and the schema enforces the reference shape
(`tenants.hash_salt_kek_ref` with the `no_raw_secret` CHECK in `db/postgres/0001_control_plane.sql`),
but no code path resolves an AWS reference. Two ways out, and the bundle has to pick one rather than
inherit whichever happens. Implement the provider against KMS: `mint` generates a data key and stores
the ciphertext, `material` decrypts it, `delete` schedules deletion, and the reference is the key ARN
plus the version selector `tenant_hmac_key.py` already parses. Or consciously ship
`LocalKeyMaterialProvider` with the root secret injected from Secrets Manager, which is defensible
for a single tenant and indefensible for a multi-tenant deployment. Implementing it is a small, well
bounded piece of work behind an existing interface with an existing test seam, and the bundle should
ship it rather than ship the decision.

## Migrations on RDS

`db/postgres/` holds `0001` through `0032`, with `0005` used twice and `0017` never allocated.
`infra/docker-compose.yml` mounts them into `/docker-entrypoint-initdb.d/`, which fires only on a
first boot against an empty data directory, and four of them (`0009`, `0010`, `0020`, `0021`) are not
mounted at all. There is no runner, no `schema_migrations` table, and nothing recording what has run.
Branch `fix/migration-sequence` (PR #323, unmerged) mounts the missing four, documents the sequence in
`db/postgres/README.md`, and adds a `make pg-migrate` target that replays the directory in `LC_ALL=C`
order and stops on first failure.

On RDS none of the compose machinery exists at all, so the bundle needs a real answer.

**Ship a one-shot ECS task.** Add `deploy/aws/ecs/migrate.taskdef.json` running the gateway image
with an override command that executes the same replay `make pg-migrate` performs, in the same VPC
and security group as the gateway, with the same `TALLY_POSTGRES_DSN` secret. Terraform creates the
task definition; the deploy runs `aws ecs run-task` and waits for exit code zero before updating the
gateway service. That keeps migrations inside the VPC (no bastion, no public RDS endpoint, no laptop
with production credentials), gives them the same image and therefore the same file set as the
running code, and puts them in front of the deploy where a failure stops the rollout.

The prerequisite is a `schema_migrations` ledger, because a replay of thirty-two files that is
idempotent by accident is not idempotent. Merge PR #323 first, then make the runner record each
applied filename in a table and skip what it has already applied. A CI step is the wrong shape here
because it needs network reach into a private subnet, and a bastion is the wrong shape because it
needs a human.

ClickHouse is easier: `make ch-migrate` replays idempotent DDL against a running instance and works
against ClickHouse Cloud over HTTPS unchanged. The two deliberately excluded one-shots
(`ch-migrate-otel-engine` and the rollup rebuild) stay manual and stay documented, because ClickHouse
cannot alter an engine or sort order in place.

## Networking, TLS and CORS

Two private subnets hold the ECS tasks and RDS; two public subnets hold the ALB. The gateway task
security group accepts 8080 only from the ALB security group; the proxy task accepts 8088 only from
the ALB; the RDS security group accepts 5432 only from the two task security groups. Nothing gets a
public IP (`assignPublicIp: DISABLED` is already correct in both service definitions). Egress from
the tasks reaches ClickHouse Cloud on 8443, the provider APIs, and the AWS control plane; VPC
endpoints for ECR, Logs, Secrets Manager and S3 remove most of the NAT bill and most of the internet
exposure.

One ALB, one ACM certificate, two host-based listener rules: `ingest.<domain>` to the gateway target
group and `llm.<domain>` to the proxy target group. The proxy hostname forwards to provider APIs with
real keys, so it needs its own protection (a WAF rule, an allowlist, or a non-guessable hostname) and
should never be the same rule as ingest.

The edge-proxy target group must use an ALB health check against `/healthz`, which
`infra/edge-proxy/cmd/edge-proxy/main.go` serves. It cannot use an ECS container `healthCheck` block
the way `gateway.taskdef.json` does, because the image is `FROM scratch` and has no shell for
`CMD-SHELL` and no `wget`. Whoever writes that task definition will otherwise write a health check
that fails every task on startup.

ClickHouse Cloud has to be publicly reachable because Vercel functions egress from the public
internet. That is a consequence of the hosting decision, not a new choice, and it is mitigated by TLS
and credentials plus whatever IP allowlisting is workable against Vercel's egress ranges. Confirm
that allowlist story before provisioning, not after.

**CORS.** There is no `CORSMiddleware` and no `add_middleware` call anywhere in
`infra/gateway/src/`, and no `Access-Control-*` header is ever set. This is currently fine: the
dashboard's Route Handlers call the gateway server to server from Vercel's Node runtime, so no
browser preflight happens. It stops being fine the moment anything runs in the browser against the
gateway, including a browser SDK, a direct control-plane fetch from a client component, or a debug
page. The bundle should add the middleware with an explicit origin allowlist driven by an env var
(the Vercel production domain plus preview domains), never `*`, and never with credentials against a
wildcard. Preview deployments get generated hostnames, so the allowlist needs a documented pattern
rather than a fixed string.

## Observability of ai-tally itself, and the failure modes

Today: CloudWatch Logs via the `awslogs` driver, with `awslogs-create-group: true` and no retention
set, which means log groups accumulate forever at full price. Set retention in the Terraform. There
are no CloudWatch alarms, no dashboard, no metric filters and no tracing of the gateway itself
anywhere in the repo.

The minimum the bundle should ship: ALB target-group unhealthy-host alarms for both services, an ALB
5xx rate alarm, an ECS running-task-count alarm per service, an RDS free-storage and CPU alarm, and a
metric filter on the gateway's error log lines. All are Terraform resources and all are cheap.

The failure modes are asymmetric and the bundle must treat them that way. If the **dashboard** is
down, nobody can read numbers and no data is lost. If the **gateway** is down, ingest fails; the
Python SDK's `BatchingTransport` retries with capped backoff and drops oldest from a 10,000-span
buffer, so a short outage costs nothing and a long one costs telemetry, never the caller's request.
If the **edge proxy** is down, the customer's LLM calls fail, because the proxy is in their request
path. That is the only component whose outage is an outage of someone else's product.

Which sets its requirements, none of which the bundle currently meets. Two tasks minimum across two
AZs with `minimumHealthyPercent: 100` (already correct in the pattern the gateway service uses).
Never scale to zero, so no idle-based scaling policy. A deliberately shallow ALB health check with a
short interval so a bad task is drained fast. And the proxy must stay up when the gateway is down:
`values.yaml` shows telemetry shipping is already optional (an empty `telemetryURL` disables it) and
the edge-key cache is a local delta-fed map, so the design supports it, but nothing in the repo tests
that a gateway outage does not add latency to a proxied request. That test is worth writing before
the first warm deploy, because the p99 budget the Go suite already enforces is measured against a
healthy gateway.

## Cost

Every line below names its meter and its inputs. **No dollar figure appears in this section**, and
none should be added until someone prices it against a current rate card, because a made-up total in
this repository would be the exact failure the product exists to prevent.

**ECS Fargate, gateway.** Meter: vCPU-hours plus GB-hours per running task. Inputs: `cpu: "512"` and
`memory: "1024"` from `gateway.taskdef.json`, `desiredCount: 2` from `gateway.service.json`, hours in
the month, and the regional Fargate rates. **Unknown in dollars, fully derivable from those four.**
Note that both task definitions pin `cpuArchitecture: X86_64`; Fargate on ARM64 is cheaper per unit,
and both images could build multi-arch (Go trivially, Python with a slower build), so this is a
standing cost lever the bundle currently forecloses.

**ECS Fargate, edge proxy.** Same meter. Inputs: a task size that does not exist yet because there is
no task definition, a desired count of at least two, and the requirement that it never scales down.
**Unknown**, and unlike the gateway it is a fixed floor rather than a variable, by design.

**ALB.** Meter: hourly charge plus LCU-hours, where an LCU is the max of new connections, active
connections, processed bytes and rule evaluations. **Unknown.** The proxy path drives processed bytes
in proportion to LLM request and response sizes, which is the one input nobody can estimate without
real traffic.

**NAT gateway, or VPC endpoints instead.** Meter: NAT is per hour plus per GB processed; interface
endpoints are per endpoint per AZ per hour plus per GB. **Unknown**, and which is cheaper depends
entirely on the ratio of AWS-bound to internet-bound egress, which for this workload is unmeasured.

**RDS Postgres.** Meter: instance-hours plus provisioned storage plus backup storage above the free
allotment plus IOPS on some storage classes. Inputs: the runbook suggests `db.t3.medium` with 20 GB
and no Multi-AZ. The control plane is small and low traffic. **Unknown in dollars.**

**ClickHouse Cloud.** Meter: compute (a minimum always-on service size unless the idling development
tier is acceptable) plus compressed storage plus data transfer. **Unknown, and the largest single
line.** The storage input is bounded for `otel_spans` by the 7/30/90 day TTL rendered from
`sdk/python/src/tally/storage_tiering.py`, and **unbounded** for the rollup and attribution tables:
`db/clickhouse/rollups.sql`, `attribution.sql`, `last_touch_index.sql` and `eval_runs.sql` carry no
TTL at all. On a managed service that is a bill that grows on its own with nobody watching it.
Choosing a retention for those tables is a prerequisite for any cost estimate, not a follow-up.

**S3.** Meter: GB-month, PUT and GET requests, and any cross-region transfer. Inputs: the replay
sampling rate and the lifecycle expiry, both operator-chosen, and zero if replay sampling is off.
**Unknown but small at low sampling rates.**

**Secrets Manager and KMS.** Meter: per secret per month, plus API calls, plus KMS key-months and
requests. Inputs: five to seven secrets for the deploy plus one HMAC reference per tenant, which is
the line that scales with tenant count. **Unknown, small at single-tenant scale.**

**ECR.** Meter: GB-month of stored images plus data transfer out. Inputs: image sizes and how many
tags the lifecycle policy retains. **Unknown, small.**

**CloudWatch Logs.** Meter: GB ingested plus GB stored per month. Inputs: log volume per request and
the retention that is currently unset, meaning infinite. **Unknown, and one of the easiest to
accidentally make large.**

**Vercel and Clerk.** Off-AWS and covered by `docs/self-hosted-scope.md`. Both **unknown**; Vercel
meters seats, function invocations, function GB-hours and egress, and Clerk gates organizations,
which this deployment requires, behind a paid plan on some tiers.

The honest summary is that the recurring floor is dominated by three always-on items (ClickHouse
Cloud compute, the ALB, and the two warm proxy tasks) and that all three are knowable within an hour
of someone opening the rate cards. That hour has not been spent.

## Phased plan

Status as of CTO-335. Phase 0 is implemented. Phases 2 and 3 are implemented as code and **applied
nowhere**, which is a weaker claim than done and is stated that way on purpose: the work that
remains on them is running them against a real account and writing down what breaks. Phases 1, 4, 5
and 6 are partly done, per the notes on each below.

**Phase 0, a weekend, and genuinely shippable.** Add the container build and push to CI, and fix the
three stale facts. A fifth job in `.github/workflows/ci.yml` that builds `infra/gateway/Dockerfile`
from the repo root and `infra/edge-proxy/Dockerfile`, tags both with the commit SHA, and pushes to
ECR via an OIDC role (no long-lived access key). Alongside it: flip
`TALLY_REPLAY_BLOB_BACKEND` to `s3` in `gateway.taskdef.json` with the bucket env var, delete the
"supported backends are memory and gcs" line from the README's Open TODOs, and add
`ai-tally-gateway-service-token` to the two IAM policy ARN lists. That is a weekend, it is the
prerequisite for every later phase, and on its own it turns "build it on your laptop" into a
reproducible artifact with a provenance.

**Phase 1.** Merge PR #323, then add the `schema_migrations` ledger to the runner and
`deploy/aws/ecs/migrate.taskdef.json`. Add the boot-time assertion that refuses to start the web tier
in a production deployment with `TALLY_DEV_TENANT` set (see the release gate below).

**Phase 2. Written, not applied.** `deploy/aws/terraform/` covers network and data: VPC, subnets,
security groups, endpoints, RDS, S3, ECR, KMS, Secrets Manager, IAM. What remains is the second half
of this phase as originally written: applying it into a scratch account. That has not happened.

**Phase 3. Written, not applied, and one piece missing.** The compute module creates the cluster,
ALB, ACM, target groups, log groups with a retention, and the gateway service rendered from the
existing task definition. The migration task is deliberately absent until phase 1's ledger exists.
Nobody has deployed the gateway, run the §8 smoke test, or written down what broke, because nobody
has run it.

**Phase 4. Mostly done.** The edge-proxy task and service definitions exist with the ALB health
check on `/healthz`, no container health check, two tasks and no autoscaling policy, and the
Terraform creates the service, its target group and its own listener rule. Still open: the test that
a gateway outage does not add latency to a proxied request.

**Phase 5. Mostly done.** `SecretManagerKeyProvider` is implemented against AWS Secrets Manager, the
CORS middleware ships with an explicit allowlist, the CloudWatch alarms are in the compute module
(unhealthy hosts per target group, ALB 5xx, running-task count per service, RDS free storage and
CPU, and a metric filter on gateway error lines), and the derived ClickHouse tables have a stated
retention. The alarms notify nobody unless an SNS topic is passed, which the README says rather than
implying paging exists.

**Phase 6. Not started, and now partly moot.** `deploy/aws/README.md` points at the Terraform and
keeps its by-hand steps, which is the right shape while the Terraform is unapplied: the runbook is
still the only path anyone has evidence for. Demoting the EKS chart is untouched.

### The release gate

No AWS deployment ships without a boot-time check that fails hard when `TALLY_DEV_TENANT` is set
alongside a production marker. Setting it makes `web/middleware.ts` export a pass-through,
`web/app/layout.tsx` mount no `ClerkProvider`, `web/lib/getTenant.ts` short-circuit to the pinned
tenant, and `canManage()` return true unconditionally. `deploy/aws/ecs/web.taskdef.json` correctly
sets it to `""` and `deploy/vercel/README.md` warns about it, but nothing enforces either. One
variable stands between a public URL and every number in the system, and a warning in a runbook is
not a control.

## Open questions and risks

I could not verify that `deploy/aws/ecs/` has been run end to end by anyone. Everything in this doc
about that path is read from files, not from a working deployment.

I could not price a single line item, and the doc says unknown for every line item as a result. That is a
real gap in the scope rather than a stylistic choice. Closing it needs current rate cards and a
traffic estimate, and the traffic estimate is the harder half.

Whether to keep the EKS chart at all is a call I have made one way (keep the file, drop the support)
but it deserves a second opinion from whoever would maintain it, because the GCP side is symmetric
and asymmetry between the two cloud directories is its own maintenance cost.

The Athena export path (`infra/gateway/src/gateway/athena_export.py`,
`tenant_athena_export.py`, migrations `0020` and `0021`) is unauthorized under the current
`task-role-policy.json` and its two migrations are among the four never mounted anywhere. I have not
determined whether that path has ever run against real AWS.

Whether the edge proxy actually holds its latency budget with the gateway unreachable is untested.
The design says it should. Nothing proves it.

~~Terraform state has no home in this plan.~~ **Resolved (CTO-335):** an S3 bucket created by
`deploy/aws/terraform/bootstrap/`, a separate root module with local state, using S3-native locking
rather than DynamoDB. See "What was built" above.

**Nothing in `deploy/aws/terraform/` has been applied against an AWS account.** It was written and
checked without credentials: `fmt`, `validate` against `hashicorp/aws` v6.63.0, checkov, and an
isolated evaluation of its rendering logic (which found two real bugs). No plan has run against a
live provider. The RDS engine version, regional Fargate ARM64 availability, and every AWS-side name
and shape validation are unverified. The module set makes the ECS path *verifiable* by someone with
an account; it does not make it verified.

---

## Appendix: what ai-tally already reads from AWS

Separate axis, mostly built, included so nobody confuses it with the bundle above.

**Cost Explorer, compute and egress.** `infra/gateway/src/gateway/connectors/compute.py`
(`AwsCostExplorerClient`, `parse_aws_cost_response`) and `connectors/egress.py`
(`AwsEgressCostExplorerClient`, which reuses compute's parser) pull daily `UnblendedCost` through one
`ce:GetCostAndUsage` call per run, filtered by cost-allocation tag for compute and by the
`DataTransfer-Out-Bytes` usage type for egress. `connectors/base.py` owns the emitter, the
idempotency guard and the run recorder. Config lives in `tenant_compute_config` (`db/postgres/0011`,
`0015`) and `tenant_egress_config` (`0012`). Money is integer micro-USD, days with no cost are
dropped rather than emitted as zero, and span ids are derived deterministically so a re-run is safe.
`docs/connector-aws-cost-explorer.md` is accurate. boto3 is lazily imported, so the gateway and the
test suite import the module without it. Untested against a real account as far as the repo shows;
the parser is unit-tested against recorded fixtures, which is a different thing.

**Athena and S3 export.** `athena_export.py` and `tenant_athena_export.py` mirror a tenant's
telemetry into their own S3 bucket as partitioned Parquet, reusing the BigQuery exporter's specs,
reader and body-stripping so both sinks emit identical facts, with per-day-partition idempotency.
`pyarrow` and `boto3` sit behind the `[athena]` extra. Its two migrations (`0020`, `0021`) are not
mounted in `infra/docker-compose.yml` and the workload IAM policy grants it nothing, so this is the
least exercised of the AWS paths.

**S3 replay blobs.** `S3ReplayBlobStore` in `replay_store.py`, covered above. Works against S3 and
against MinIO through `replay_s3_endpoint`, which is how the local stack uses it, so this one has
real exercise behind it even though it is not S3 proper.

**Bedrock.** The workload IAM policy grants `bedrock:InvokeModel`. I found no Bedrock client in the
gateway or the SDK; the grant appears to be forward-looking. Treat it as unearned privilege until
something uses it.

The relationship to the bundle is narrow and worth stating plainly. These integrations run inside the
gateway and need exactly two things from the deployment: credentials resolved through the task role
or an assumed role (`aws-default-chain` or an ARN in `credentials_ref`), and the IAM grants listed in
the least-privilege section above. They do not change the topology, they do not change the IaC, and
they are not the reason to build the bundle.
