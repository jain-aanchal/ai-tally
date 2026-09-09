# Terraform for the ai-tally AWS bundle

Creates the account infrastructure that `deploy/aws/ecs/` has always assumed exists: VPC, subnets,
security groups, NAT, ECR, KMS, S3, RDS, Secrets Manager containers, IAM roles, ALB, ACM, the ECS
cluster, both services, log groups with a retention, and the CloudWatch alarms. It consumes
`deploy/aws/ecs/*.taskdef.json` and `deploy/aws/ecs/iam/*.json` as templates rather than restating
them in HCL.

## Read this first: what has and has not been proven

**This has never been applied against an AWS account.** Not once, not in a scratch account, not
partially. It was written and checked with no AWS credentials in reach.

What was actually run, and passed:

- `terraform fmt -check -recursive` on both root modules.
- `terraform init -backend=false` and `terraform validate` on the root module and on `bootstrap/`,
  against `hashicorp/aws` v6.63.0.
- `checkov -d . --framework terraform`: 189 passed, 21 failed. Every remaining failure is listed and
  answered in [What checkov still flags](#what-checkov-still-flags) below. `tflint` was not
  available.
- The rendering logic (placeholder substitution, statement removal, secret-ARN rewriting, the
  environment overlay) was evaluated in isolation with `terraform console`, because `validate` does
  not evaluate locals. That found two real bugs, both fixed. It is still only an evaluation of
  expressions, not a deployment.

What that does **not** establish, and what will therefore break first:

- No plan has ever been produced against a real provider, so nothing here has been checked against
  live API validation: name-length limits, ACM and ELB naming rules, RDS engine version
  availability, and instance class availability per region are all unverified.
- **`db_engine_version = "16.4"` is a guess about what RDS offers.** Check
  `aws rds describe-db-engine-versions --engine postgres` and set it to a version that exists in
  your region, or the first apply fails on the RDS instance after creating the VPC.
- **Fargate ARM64 availability per region is unverified.** All three task definitions pin
  `cpuArchitecture: ARM64` (PR #351). If ARM64 Fargate is not offered in your region, or the images
  in ECR are not arm64 manifests, every task fails to start with an image-manifest error and the
  service cycles. Confirm before applying.
- No apply ordering has been exercised. Dependencies here are expressed through module outputs, so
  they should be correct, but "should be" is the accurate word.
- Nothing about whether the **application** works once deployed is affected by this module set.
  `docs/aws-bundle-scope.md` records that nobody has demonstrably run `deploy/aws/ecs/` end to end.
  That is still true. This makes it possible for someone with an account to find out, which is a
  different claim from making it work.
- The one-shot migration task is **not here**. `db/postgres/` has no migration runner and no
  `schema_migrations` ledger, so this module creates an RDS instance with an empty schema. Nothing
  applies the migrations. That is phase 1 in `docs/aws-bundle-scope.md` and it is a hard prerequisite
  for the gateway starting successfully.

Budget for first-run friction accordingly. If you are the first person to apply this, the useful
thing to leave behind is a note of what broke.

## What Terraform owns, and what it does not

Terraform owns everything in AWS that this deployment needs, with two carve-outs.

**Not Terraform, on purpose, because they are not AWS:**

| Thing | Why it is a manual prerequisite |
| --- | --- |
| ClickHouse Cloud service | Different vendor, once per environment. A provider exists, but adding it would put a second vendor's credentials in the same plan and the same state to save one console visit. Terraform takes the hostname as `clickhouse_host`. |
| Vercel project and its environment variables | Same reasoning. The dashboard is deployed by Vercel from `deploy/vercel/README.md`, and its production origin comes back here as `cors_allowed_origins`. |
| Clerk production instance | Same. Its keys go into the Vercel project, not into AWS. |
| DNS, when the zone is outside the account | Supported either way: pass `route53_zone_id` and Terraform creates the validation and alias records, or pass `acm_certificate_arn` and create the records yourself. |

The boundary is drawn at the AWS account edge and nowhere else. That is a simpler line to remember
than "Terraform owns the cheap vendors", and it means nobody has to reason about which secrets from
which vendor are in state.

**Not Terraform, because the application owns it:** per-tenant HMAC keys. PR #352's
`SecretManagerKeyProvider` mints one Secrets Manager secret per tenant under
`ai-tally/tenant-hmac/<uuid>` when a tenant is provisioned, and stores only its ARN plus a `v1`
label in `tenants.hash_salt_kek_ref`. **Terraform must never manage anything under that prefix.** A
resource there would be read as drift on the next plan and destroyed, and destroying a tenant's HMAC
key silently invalidates every hash ever computed for that tenant, which is unrecoverable and
produces no error anywhere. Terraform owns the KMS key those secrets can be encrypted with and the
IAM grant on the prefix (`TenantHmacSecrets` in `task-role-policy.json`), and stops there.

## How secret material stays out of state

Terraform state lives in an S3 bucket and is readable in full by anyone who can read that bucket. It
has no write-only attribute for a secret value. So:

- Terraform creates **secret containers** (`aws_secretsmanager_secret`) and never a
  `aws_secretsmanager_secret_version`. You write the values with the AWS CLI in step 4. An empty
  container is honest: the task fails to start with a Secrets Manager error rather than booting on a
  placeholder that looks like a credential.
- The **RDS master password is never generated by Terraform**. `manage_master_user_password = true`
  makes RDS mint and hold it in a secret RDS owns. The usual alternative, `random_password`, writes
  the password into state by construction.
- `terraform.tfvars` holds names, ids and hostnames only. Never a secret: a tfvars value becomes a
  state value.
- Every output is an ARN or an identifier.

## Layout

```
deploy/aws/terraform/
  bootstrap/          separate root, LOCAL state: the state bucket + the GitHub OIDC CI role
  modules/network/    VPC, subnets, NAT, endpoints, four security groups, flow logs
  modules/data/       KMS, ECR, S3 replay bucket, RDS, Secrets Manager containers
  modules/iam/        workload + execution roles, rendered from deploy/aws/ecs/iam/*.json
  modules/compute/    ECS cluster, ALB, ACM, target groups, task definitions, services, alarms
  main.tf             the application stack, composing the four modules
```

Four modules because networking, data, identity and compute have four different change cadences and
four different blast radii: compute changes on every deploy and rolls itself back, data changes
rarely and a destroy is unrecoverable. One root, not four, because the coupling between them is
dense and four state files would trade that for a documented apply order somebody will skip.

`bootstrap/` is the only genuinely separate root, because it creates the bucket the other root's
state lives in. It also carries the CI role, so that unblocking image publishing does not require
standing up a VPC first.

## Prerequisites

- Terraform **1.11 or newer**. The backend uses S3-native locking (`use_lockfile`), which is why
  there is no DynamoDB table anywhere in this module set.
- AWS credentials with administrative rights in the target account, for the first apply.
- A ClickHouse Cloud service, publicly reachable on 8443. It has to be public because Vercel
  functions egress from the public internet; that is a consequence of the hosting decision, not a
  new one. Confirm any IP allowlist story against Vercel's egress ranges **before** provisioning.
- A domain, and either a Route 53 hosted zone in this account or the ability to create CNAME records
  in whatever zone holds it.
- arm64 images in ECR. Which is circular on a first run: ECR repositories are created by step 3, and
  CI cannot push until step 2 exists. Step 3 covers the ordering.

## Bootstrap order

Do these in order. Steps 1 and 2 are once per account; 3 onward are per environment.

### 1. State bucket and CI role

```bash
cd deploy/aws/terraform/bootstrap
terraform init                        # local state, on purpose

terraform apply \
  -var aws_region=us-east-1 \
  -var state_bucket_name=$(aws sts get-caller-identity --query Account --output text)-ai-tally-tfstate
```

If the account already has a GitHub OIDC provider (`aws iam list-open-id-connect-providers` lists
one for `token.actions.githubusercontent.com`), add `-var create_github_oidc_provider=false`. A
second provider is an error, not a duplicate.

This leaves `bootstrap/terraform.tfstate` on your disk. It holds a bucket name, a role ARN and a
provider ARN, and no secret. Two defensible ways to handle it: keep it wherever your team keeps
operational files, or migrate it into the bucket it just created by adding a backend block and
running `terraform init -migrate-state`. What is not defensible is losing it, because the state
bucket has `prevent_destroy` and reconstructing the record means importing by hand.

Then set the repository variable so PR #351's ECR publishing stops skipping:

```bash
terraform output -raw ci_ecr_role_arn
gh variable set AWS_ECR_ROLE_ARN --body "<that ARN>"
gh variable set AWS_ECR_REGISTRY --body "<account>.dkr.ecr.<region>.amazonaws.com"
gh variable set AWS_REGION --body "us-east-1"
```

The trust policy is `deploy/aws/ecs/iam/github-actions-oidc-trust-policy.json` unchanged, so the
role can only be assumed from `refs/heads/main` and `refs/tags/v*` of this repository. A fork or a
pull-request run cannot assume it.

### 2. Configure the backend and the inputs

```bash
cd deploy/aws/terraform
cp backend.hcl.example backend.hcl                # fill in bucket and region
cp terraform.tfvars.example terraform.tfvars      # fill in everything
terraform init -backend-config=backend.hcl
```

Both files are gitignored. Nothing in either is a secret.

What you have to fill in, and where each value comes from:

| Variable | Source |
| --- | --- |
| `aws_region`, `availability_zones` | Your choice. Confirm Fargate ARM64 is offered in the region. |
| `replay_bucket_name` | Globally unique. Convention `<account-id>-ai-tally-replay`. |
| `clickhouse_host` | ClickHouse Cloud console. |
| `ingest_hostname`, `llm_hostname` | Your domain. Give the LLM one a name nobody guesses; it forwards to provider APIs with real keys. |
| `route53_zone_id` **or** `acm_certificate_arn` | Exactly one. Zone in this account, or a certificate covering both hostnames. |
| `cors_allowed_origins` | The dashboard's Vercel origin. A wildcard is refused by the gateway at boot. |
| `gateway_image`, `edge_proxy_image` | See step 3. |

### 3. First apply, in two passes

The circularity: the images have to exist in ECR before the ECS services can start, and the ECR
repositories are created by this module. So the first apply is two passes.

```bash
# Pass one: everything except compute. Creates the ECR repositories among other things.
terraform apply -target=module.network -target=module.data -target=module.iam
```

`-target` is a deliberate exception here, not a habit. Terraform prints a warning about it and the
warning is correct in general.

Then push images. Merge to `main` with `AWS_ECR_ROLE_ARN` set and CI publishes them, or build
locally per `deploy/aws/README.md` section 1. Put the resulting SHA tags into
`gateway_image` and `edge_proxy_image` in `terraform.tfvars`. Pin a SHA, not `main`: a plan should
say what will actually run.

```bash
terraform plan     # read it
terraform apply
```

If DNS is outside the account and Terraform requested the certificate, `terraform output
certificate_validation_records` lists the CNAMEs to create. Until they exist the certificate does
not validate and the HTTPS listener does not come up.

### 4. Write the secret values

Terraform created empty containers. Nothing starts until they hold values.

```bash
REGION=us-east-1

# The DSN. The password lives in the secret RDS owns; read it, assemble the DSN, write it, and let
# it leave your shell history.
DB_SECRET=$(terraform output -raw db_master_user_secret_arn)
DB_HOST=$(terraform output -raw db_address)
PASSWORD=$(aws secretsmanager get-secret-value --secret-id "$DB_SECRET" --region "$REGION" \
  --query SecretString --output text | python3 -c 'import json,sys; print(json.load(sys.stdin)["password"])')
aws secretsmanager put-secret-value --region "$REGION" \
  --secret-id ai-tally-postgres-dsn \
  --secret-string "postgresql://tally:${PASSWORD}@${DB_HOST}:5432/tally?sslmode=require"
unset PASSWORD

aws secretsmanager put-secret-value --region "$REGION" \
  --secret-id ai-tally-clickhouse-password --secret-string 'FROM_CLICKHOUSE_CLOUD'

aws secretsmanager put-secret-value --region "$REGION" \
  --secret-id ai-tally-gateway-service-token --secret-string "$(openssl rand -hex 32)"

# Optional. Skip and set create_provider_key_secrets = false if the gateway makes no provider calls.
aws secretsmanager put-secret-value --region "$REGION" \
  --secret-id ai-tally-openai-api-key --secret-string 'sk-...'
```

Do **not** enable AWS managed rotation on any of these, and especially not on anything under
`ai-tally/tenant-hmac/`. Rotating a tenant's HMAC key changes every hash computed after it, and
ai-tally does not ship rotation.

### 5. Apply the database migrations

There is no automated path yet. `db/postgres/` has 32 numbered files, no runner and no
`schema_migrations` ledger, and RDS sits in a private subnet with no public endpoint. The one-shot
ECS migration task that solves this properly is phase 1 in `docs/aws-bundle-scope.md` and is not in
this module set.

Until it exists, the least bad option is ECS Exec into a gateway task (enabled on that service) and
running `psql` from inside the VPC. That is a manual step performed by a human with production
credentials, which is exactly what the phase 1 design exists to remove. Do not add a bastion or make
RDS publicly accessible to work around it.

### 6. Smoke test

`deploy/aws/README.md` section 8. Then check that the gateway actually picked up the three settings
this module injects, because the whole point of injecting them is that they are easy to lose:

```bash
aws ecs describe-task-definition --task-definition ai-tally-gateway \
  --query 'taskDefinition.containerDefinitions[0].environment' | \
  grep -E 'TALLY_ENV|TALLY_HMAC_KEY_PROVIDER|TALLY_CORS_ALLOWED_ORIGINS|AWS_REGION'
```

`AWS_REGION` is in that list on purpose. See [When it does not work](#when-it-does-not-work).

## NAT gateway versus VPC endpoints

`docs/aws-bundle-scope.md` frames these as alternatives. They are not, and the difference matters
before anyone plans around it.

Interface endpoints reach AWS services only. The gateway has to reach **ClickHouse Cloud on 8443**
and the edge proxy exists to reach **api.openai.com** and its peers. Both are the public internet.
With `assignPublicIp: DISABLED` in both service definitions, a private subnet with no NAT reaches
neither, and the failure is a connection timeout at runtime rather than an error at apply. So NAT is
required, and `enable_nat_gateway` defaults to true with a precondition on both services that fails
the apply if it is turned off.

The endpoints are additive, and the trade is:

- **NAT gateway.** Meter: per NAT-hour, plus per GB processed. `single_nat_gateway = true` is the
  default: one NAT for both AZs, cheaper per hour, and losing that AZ takes egress out for every
  task including the edge proxy. Set it false for a production deployment of the proxy.
- **S3 gateway endpoint.** Always on. No hourly charge, and it keeps ECR layer downloads (which are
  S3 GETs) off the NAT's per-GB meter. This is the largest AWS-bound flow here, so it is the one
  free win.
- **Interface endpoints** for ECR API/DKR, Logs, Secrets Manager, STS and KMS
  (`enable_interface_endpoints`, default false). Meter: per endpoint per AZ per hour, plus per GB.
  Six endpoints across two AZs is twelve hourly meters running whether or not you pull an image.
  Whether they pay for themselves depends on how often tasks start and how chatty the logs are, and
  neither is measured for this workload. They also keep that traffic inside the VPC, which is worth
  something independent of the bill.

**No dollar figure appears anywhere in this module set**, matching the cost section of
`docs/aws-bundle-scope.md`. The inputs above are what you need to price it against a current rate
card; the number is not invented here.

The same applies to ARM64. Fargate meters ARM64 vCPU-hours and GB-hours at a different rate from
x86, and PR #351 moved all three task definitions there. That is a real cost lever with a real
availability constraint, and the rate card is where the number lives.

## Rolling back

**Compute.** Both services have `deployment_circuit_breaker` with rollback enabled, so a task
definition that will not become healthy rolls back to the previous one on its own. To roll back
deliberately, put the previous image SHA in `terraform.tfvars` and apply; that registers a new task
definition revision and rolls the service. Do not delete task definition revisions.

**A bad apply.** The state bucket is versioned with a 90-day window on superseded versions. Restore
the previous object version and run `terraform plan` before anything else, to see what the state you
restored believes about the world.

**Data.** RDS has `deletion_protection = true` and takes a final snapshot. The replay bucket is
versioned. Secrets have a 30-day recovery window, so a deleted secret can be restored with
`aws secretsmanager restore-secret` inside that window. The state bucket has `prevent_destroy`.

**Teardown**, when you actually mean it:

```bash
terraform destroy    # fails on protected resources until you clear the flags below
```

You will have to set `db_deletion_protection = false` and `alb_deletion_protection = false` and
apply before the destroy will complete. That friction is deliberate. `bootstrap/` is destroyed
separately and last, and the state bucket resists it: remove the `prevent_destroy` block by hand if
you genuinely want the record gone. Also unset `AWS_ECR_ROLE_ARN` in the repository variables, or CI
fails on assume.

## When it does not work

Ordered by how often each is likely to be the cause.

**Every task stops immediately, and `aws ecs describe-tasks` says
`CannotPullContainerError ... no match for platform`.** The image in ECR is not an arm64 manifest,
or the region does not offer ARM64 Fargate. All three task definitions pin `cpuArchitecture: ARM64`.
Check the manifest with `docker manifest inspect`, and check the region before assuming the image is
wrong.

**Tasks stop with `ResourceInitializationError ... unable to pull secrets`.** Three separate causes,
in order of likelihood. The secret container is empty because step 4 was skipped. The execution role
cannot read it, which means the ARN list in `execution-role-policy.json` does not cover the name.
Or, with a customer-managed key, the `SecretsInjectionKmsDecrypt` statement is missing or names the
wrong key. `aws secretsmanager get-secret-value` as yourself will succeed in all three cases, so it
proves nothing; check the execution role's policy instead.

**The gateway starts and then exits, with a boot error about authentication.** `TALLY_ENV=production`
with `TALLY_REQUIRE_API_KEY=false` is refused deliberately (PR #352). That is the guard working. Do
not set `TALLY_ALLOW_INSECURE_NO_AUTH=1` to get past it on a deployment with a public URL.

**Everything reaches AWS fine and nothing reaches ClickHouse.** `enable_nat_gateway = false`, or the
NAT is in an AZ that is having a bad day and `single_nat_gateway = true`. Interface endpoints do not
help here; see the section above.

**The gateway logs region errors, or the replay store writes nowhere.** Check that `AWS_REGION` and
`TALLY_REPLAY_S3_REGION` are still spelled that way in the registered task definition. The runbook's
`sed -e "s/REGION/$REGION/g"` recipe rewrites those variable NAMES as well as their values, giving
you `AWS_us-east-1`. The task registers and starts, and both settings are simply absent. This module
substitutes by shape rather than as a bare word specifically to avoid that, so seeing it means
something registered a task definition by hand.

**The edge proxy service cycles forever with nothing useful in the logs.** Somebody added a
container `healthCheck` to `edge-proxy.taskdef.json`. The image is `FROM scratch`: no shell, no
curl, no python, so every check fails and every task is killed as unhealthy. There is a precondition
in `modules/compute/taskdefs.tf` that fails the apply on this, and a `dockerLabels` note in the file
itself. Liveness is the ALB target group's job.

**`terraform apply` hangs on `aws_acm_certificate_validation`.** DNS validation records do not
exist. If `route53_zone_id` is set, the zone is probably not authoritative for the hostname. If it
is not set, you were meant to create the records from `terraform output
certificate_validation_records` yourself.

**`Error acquiring the state lock`.** Another apply is running, or one died holding the lock. The
lock is a `.tflock` object beside the state object in S3, not a DynamoDB row. `terraform
force-unlock <id>` after you have confirmed nothing else is running.

**The plan wants to destroy something under `ai-tally/tenant-hmac/`.** Stop. Something imported an
application-owned secret into state. Remove it with `terraform state rm` rather than applying.
Destroying a tenant's HMAC key invalidates every hash ever computed for that tenant, silently and
permanently.

## What checkov still flags

`checkov -d . --framework terraform`: **189 passed, 21 failed.** Every remaining failure is a
deliberate choice or a limit of the checker. Listed so nobody has to re-derive them:

| Check | Answer |
| --- | --- |
| `CKV2_AWS_5` security groups not attached (x4) | False positive. They are passed to other modules by id, which checkov's graph does not follow. |
| `CKV_AWS_378` target groups use HTTP (x2) | Deliberate. TLS terminates at the ALB; the tasks serve plain HTTP on 8080 and 8088 inside the VPC. |
| `CKV_AWS_51` ECR tags not immutable | Required. CI publishes a moving `main` tag alongside the SHA, so an immutable repository fails every merge after the first. Deployments pin the SHA. |
| `CKV2_AWS_28` no WAF on the public ALB | **A real gap**, and a known one: `docs/aws-bundle-scope.md` says the proxy hostname needs its own protection. The module ships a separate listener rule and `alb_ingress_cidrs` so a WAF or an allowlist can be added; it does not ship one. |
| `CKV2_AWS_57` no secret rotation | Deliberate. Rotation is not implemented for these secrets, and enabling managed rotation on the HMAC secrets would be actively harmful. |
| `CKV_AWS_157`, `CKV_AWS_353`, `CKV_AWS_118` RDS Multi-AZ, Performance Insights, enhanced monitoring | All are variables, all default off. The control plane is small and its outage loses no telemetry. Turn them on if your risk tolerance differs. |
| `CKV_AWS_161` RDS IAM auth | Not enabled. The gateway authenticates with a DSN; turning on a mechanism no code path uses would be a setting nobody exercises. |
| `CKV_AWS_18`, `CKV_AWS_144`, `CKV2_AWS_62` S3 access logging, cross-region replication, event notifications (x2 each) | Accepted. Access logging needs a second bucket to hold it, replication is a cost and complexity decision that belongs to the operator, and nothing consumes bucket events. |
| `CKV_AWS_145` state bucket not KMS-encrypted | Deliberate. The state bucket is created by `bootstrap/`, before any KMS key exists; SSE-S3 avoids putting a key dependency in front of the thing that unblocks everything else. |
| `CKV2_AWS_64` no explicit KMS key policy | Deliberate. The default key policy grants the account root, which is the standard shape. An explicit policy here is a good way to lock yourself out of your own key. |

## Related

- `deploy/aws/README.md`: the by-hand runbook this module set automates, and still the reference for
  what each resource is for and which IAM grants to delete.
- `docs/aws-bundle-scope.md`: why Terraform, why ECS and not EKS, and the phased plan this is
  phase 2 and 3 of.
- `db/postgres/README.md`: the migration sequence, and why there is no runner yet.
