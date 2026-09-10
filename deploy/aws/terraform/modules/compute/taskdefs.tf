# SPDX-License-Identifier: Apache-2.0
#
# Task definitions, rendered from deploy/aws/ecs/*.taskdef.json (CTO-335).
#
# WHY THE JSON IS CONSUMED RATHER THAN REWRITTEN IN HCL, per docs/aws-bundle-scope.md: those files
# are the one artifact that is already reviewed and correct, and they carry facts that are easy to
# lose in a translation. `cpu`, `memory`, `runtimePlatform.cpuArchitecture` (ARM64, moved there by
# PR #351), the gateway's Python health check, the edge proxy's deliberate ABSENCE of one, and the
# dockerLabels that explain why, all come out of the file. Restating them here would create a second
# place to change them and no way to notice they had diverged.
#
# ARM64, since it is now load-bearing. PR #351 moved all three task definitions to
# `cpuArchitecture: ARM64` and publishes arm64 images. Fargate ARM64 is not available in every
# region or with every Fargate feature (Windows and some older platform versions are x86-only), and
# it is metered at a different vCPU-hour and GB-hour rate than x86. The rate card is where that
# number lives; it is not repeated here. The failure mode if the region or the image is wrong is an
# image-manifest error at task start, which is why `runtimePlatform` is read from the file rather
# than defaulted.
#
# WHAT THIS FILE ADDS TO THE JSON, and why it is here rather than there: the three gateway settings
# PR #352 documented but could not add, because gateway.taskdef.json belongs to PR #351. They are
# environment overlays applied at render time, so the file stays owned by one PR and the deployment
# still gets the settings.

locals {
  taskdef_dir = var.ecs_dir

  # Placeholder substitution (CTO-360).
  #
  # Every placeholder in deploy/aws/ecs/ is delimited `__LIKE_THIS__`, which is what makes a blanket
  # replace safe here and in the runbook's `sed`. It did not used to be. The tokens were the bare
  # words `REGION` and `ACCOUNT`, and a blanket `s/REGION/$REGION/g` also rewrote the environment
  # variable NAMES `AWS_REGION` and `TALLY_REPLAY_S3_REGION` into `AWS_us-east-1` and
  # `TALLY_REPLAY_S3_us-east-1`. The task registered fine and the gateway then ran with neither
  # variable set, so the AWS default credential chain lost its region and the S3 replay store lost
  # its, with no error naming the cause. `__REGION__` cannot occur inside an identifier, so the
  # shape-matching this block used to do is no longer needed.
  render_taskdef = {
    for name in ["gateway", "edge-proxy"] :
    name => replace(
      replace(
        replace(
          replace(
            file("${local.taskdef_dir}/${name}.taskdef.json"),
            "__REPLAY_BUCKET__", var.replay_bucket_name
          ),
          "__CLICKHOUSE_HOST__", var.clickhouse_host
        ),
        "__ACCOUNT__", data.aws_caller_identity.current.account_id
      ),
      "__REGION__", var.aws_region
    )
  }

  gateway_raw    = jsondecode(replace(local.render_taskdef["gateway"], "__GATEWAY_URL__", local.gateway_url))
  edge_proxy_raw = jsondecode(replace(local.render_taskdef["edge-proxy"], "__GATEWAY_URL__", local.gateway_url))

  gateway_container_raw    = local.gateway_raw.containerDefinitions[0]
  edge_proxy_container_raw = local.edge_proxy_raw.containerDefinitions[0]

  # The three settings from deploy/aws/README.md "Gateway settings this bundle expects".
  #
  # TALLY_ENV=production makes the gateway refuse to boot with authentication off.
  # TALLY_HMAC_KEY_PROVIDER=kms selects PR #352's Secrets Manager provider; unset means the LOCAL
  #   provider, whose material is derived from a root secret in configuration, which is what
  #   credentials-by-reference forbids on a multi-tenant instance.
  # TALLY_CORS_ALLOWED_ORIGINS is an explicit allowlist. A wildcard is refused at boot, and unset
  #   admits nobody in a deployment, so this fails closed either way.
  # TALLY_REPLAY_S3_PREFIX is here for a fourth reason, and it is a bill rather than a boot (CTO-361).
  #   The gateway's default prefix is the empty string, so with the variable unset the replay store
  #   writes bodies at the ROOT of the bucket. The data module's lifecycle rule filters on
  #   `replay/`. Nothing matched, nothing expired, and the module's own comment claimed that rule
  #   was the only thing standing between a sampled corpus and an unbounded GB-month meter. It is
  #   passed from the same variable the lifecycle filter uses so the two cannot drift again.
  gateway_managed_environment = merge(
    {
      TALLY_ENV                  = var.tally_env
      TALLY_HMAC_KEY_PROVIDER    = var.hmac_key_provider
      TALLY_CORS_ALLOWED_ORIGINS = join(",", var.cors_allowed_origins)
      TALLY_REPLAY_S3_PREFIX     = var.replay_prefix
      TALLY_CLICKHOUSE_PORT      = tostring(var.clickhouse_port)
    },
    # Optional, and only meaningful with a customer-managed key: it encrypts the per-tenant HMAC
    # secrets the application mints. Terraform does not create those secrets (see modules/data).
    var.kms_key_id == null || var.kms_key_id == "" ? {} : { TALLY_HMAC_SECRETS_KMS_KEY_ID = var.kms_key_id },
    var.gateway_extra_environment,
  )

  gateway_environment = merge(
    { for e in local.gateway_container_raw.environment : e.name => e.value },
    local.gateway_managed_environment,
  )

  edge_proxy_environment = merge(
    { for e in local.edge_proxy_container_raw.environment : e.name => e.value },
    var.edge_proxy_extra_environment,
  )
}

locals {
  # The task definitions spell secret ARNs without Secrets Manager's six-character suffix, which is
  # correct as documentation and wrong as an ECS `valueFrom`: ECS resolves the full ARN, and a
  # suffix-less one fails at task start with a ResourceNotFoundException that reads like a
  # permissions problem. Rewriting each entry against the ARNs the data module actually created also
  # removes the third copy of the list deploy/aws/README.md warns must stay in agreement.
  # `distinct` is load-bearing: both task definitions reference ai-tally-gateway-service-token, and
  # a for expression over the concatenated lists produces that key twice and fails the apply.
  all_secret_value_froms = distinct(concat(
    [for entry in local.gateway_container_raw.secrets : entry.valueFrom],
    [for entry in local.edge_proxy_container_raw.secrets : entry.valueFrom],
  ))

  resolve_secret = {
    for value_from in local.all_secret_value_froms :
    value_from => lookup(var.secret_arns, element(split(":secret:", value_from), 1), value_from)
  }

  gateway_secrets = [
    for entry in local.gateway_container_raw.secrets : {
      name      = entry.name
      valueFrom = local.resolve_secret[entry.valueFrom]
    }
    if !contains(var.gateway_drop_secrets, entry.name)
  ]

  edge_proxy_secrets = [
    for entry in local.edge_proxy_container_raw.secrets : {
      name      = entry.name
      valueFrom = local.resolve_secret[entry.valueFrom]
    }
  ]

  gateway_container = merge(local.gateway_container_raw, {
    image       = var.gateway_image
    environment = [for k, v in local.gateway_environment : { name = k, value = v }]
    secrets     = local.gateway_secrets
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.gateway.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "gateway"
        # The task definitions already set this to false on purpose: a group the awslogs driver
        # creates has no retention and keeps every line forever at full price. The group above has
        # one.
        "awslogs-create-group" = "false"
      }
    }
  })

  edge_proxy_container = merge(local.edge_proxy_container_raw, {
    image       = var.edge_proxy_image
    environment = [for k, v in local.edge_proxy_environment : { name = k, value = v }]
    secrets     = local.edge_proxy_secrets
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.edge_proxy.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "edge-proxy"
        "awslogs-create-group"  = "false"
      }
    }
  })
}

resource "aws_ecs_task_definition" "gateway" {
  family                   = local.gateway_raw.family
  requires_compatibilities = local.gateway_raw.requiresCompatibilities
  network_mode             = local.gateway_raw.networkMode
  cpu                      = local.gateway_raw.cpu
  memory                   = local.gateway_raw.memory
  execution_role_arn       = var.execution_role_arn
  task_role_arn            = var.workload_role_arn

  runtime_platform {
    cpu_architecture        = local.gateway_raw.runtimePlatform.cpuArchitecture
    operating_system_family = local.gateway_raw.runtimePlatform.operatingSystemFamily
  }

  container_definitions = jsonencode([local.gateway_container])

  tags = var.tags
}

resource "aws_ecs_task_definition" "edge_proxy" {
  family                   = local.edge_proxy_raw.family
  requires_compatibilities = local.edge_proxy_raw.requiresCompatibilities
  network_mode             = local.edge_proxy_raw.networkMode
  cpu                      = local.edge_proxy_raw.cpu
  memory                   = local.edge_proxy_raw.memory
  execution_role_arn       = var.execution_role_arn
  task_role_arn            = var.workload_role_arn

  runtime_platform {
    cpu_architecture        = local.edge_proxy_raw.runtimePlatform.cpuArchitecture
    operating_system_family = local.edge_proxy_raw.runtimePlatform.operatingSystemFamily
  }

  container_definitions = jsonencode([local.edge_proxy_container])

  tags = var.tags

  lifecycle {
    # Guards the failure the dockerLabels in edge-proxy.taskdef.json describe: the image is FROM
    # scratch, so a container health check copied from the gateway kills every task with nothing in
    # the logs that names the cause. Liveness is the ALB target group's job here.
    precondition {
      condition     = !can(local.edge_proxy_container_raw.healthCheck)
      error_message = "edge-proxy.taskdef.json has grown a container healthCheck. The image is FROM scratch and has no shell or HTTP client, so any check fails every attempt and the service cycles forever. Liveness belongs to the ALB target group on /healthz."
    }
  }
}
