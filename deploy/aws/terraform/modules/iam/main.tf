# SPDX-License-Identifier: Apache-2.0
#
# Workload and execution roles (CTO-335).
#
# The four documents in deploy/aws/ecs/iam/ are the reviewed artifact and stay the source of truth.
# This module reads them, substitutes the placeholders the runbook tells a human to `sed`, and
# optionally removes whole statements by Sid. It does not restate any policy in HCL, because two
# copies of a least-privilege policy diverge and the divergence is always discovered as a 403 in
# production.
#
# The three ARN lists that deploy/aws/README.md warns must agree (task role, execution role, task
# definition) now agree because all three are rendered from files in this repository on one apply.

data "aws_caller_identity" "current" {}

locals {
  iam_dir = "${var.ecs_dir}/iam"

  # Statements to remove. Two are automatic and the rest come from the caller.
  #
  # The KMS statements go when there is no customer-managed key: deploy/aws/README.md section 6 is
  # explicit that a policy naming a key that does not exist is a policy nobody can read, and the
  # AWS-managed keys need no grant at all.
  #
  # Four statements, not two (CTO-361). The two ViaService=secretsmanager grants were here already.
  # `ReplayBucketKmsUse` and `EcrPullKmsDecrypt` are new, and both close a hole that fails at
  # RUNTIME rather than at apply, which is the expensive direction:
  #
  #   * The replay bucket has SSE-KMS with this key by default, so every `S3ReplayBlobStore` PUT
  #     needs kms:GenerateDataKey and every GET needs kms:Decrypt, both ViaService s3. The task role
  #     had KMS only ViaService secretsmanager, so replay writes would have returned AccessDenied
  #     from S3 with the key named nowhere in the message.
  #   * ECR repositories are created with encryption_type = KMS against this key. ECR normally uses
  #     a grant it holds on your behalf, so this may be unearned; it is scoped to one key through
  #     one service and it is here because the failure it prevents is CannotPullContainerError on
  #     every task with no indication that KMS is involved. Drop it with drop_execution_policy_sids
  #     once a real pull has been observed to work without it.
  auto_dropped_sids = var.kms_key_id == null || var.kms_key_id == "" ? [
    "SecretsManagerKmsUse",
    "SecretsInjectionKmsDecrypt",
    "ReplayBucketKmsUse",
    "EcrPullKmsDecrypt",
  ] : []

  dropped_task_sids      = distinct(concat(local.auto_dropped_sids, var.drop_task_policy_sids))
  dropped_execution_sids = distinct(concat(local.auto_dropped_sids, var.drop_execution_policy_sids))

  # `replace` on a file, not `templatefile`: the JSON carries `__DELIMITED__` placeholder words
  # rather than ${...} interpolation, and rewriting them into Terraform syntax would break the `sed`
  # recipes in deploy/aws/README.md that operate on the same files.
  #
  # The delimiters are the point (CTO-360). A bare `REGION` token is a substring of the environment
  # variable names `AWS_REGION` and `TALLY_REPLAY_S3_REGION`, so a blanket replace corrupted those
  # names in the task definitions. `__REGION__` cannot occur inside an identifier, so one blanket
  # replace per token is correct in every file and nobody has to remember which files the shortcut
  # is safe in.
  render = {
    for name in ["task-role-policy", "execution-role-policy", "ecs-tasks-trust-policy"] :
    name => replace(
      replace(
        replace(
          replace(file("${local.iam_dir}/${name}.json"), "__REPLAY_BUCKET__", var.replay_bucket_name),
          "__KMS_KEY_ID__", var.kms_key_id == null ? "" : var.kms_key_id
        ),
        "__ACCOUNT__", data.aws_caller_identity.current.account_id
      ),
      "__REGION__", var.aws_region
    )
  }

  task_policy_raw      = jsondecode(local.render["task-role-policy"])
  execution_policy_raw = jsondecode(local.render["execution-role-policy"])

  task_policy = jsonencode({
    Version   = local.task_policy_raw.Version
    Statement = [for s in local.task_policy_raw.Statement : s if !contains(local.dropped_task_sids, lookup(s, "Sid", ""))]
  })

  execution_policy = jsonencode({
    Version   = local.execution_policy_raw.Version
    Statement = [for s in local.execution_policy_raw.Statement : s if !contains(local.dropped_execution_sids, lookup(s, "Sid", ""))]
  })
}

resource "aws_iam_role" "workload" {
  name               = "${var.name_prefix}-workload"
  description        = "The application's own identity: replay bucket, per-tenant HMAC secrets, connector roles."
  assume_role_policy = local.render["ecs-tasks-trust-policy"]
  tags               = var.tags

  lifecycle {
    # See bootstrap/main.tf for what an unsubstituted placeholder actually costs. Checked on the
    # first role rather than on each, because all three documents go through the same `render`.
    precondition {
      condition     = alltrue([for name, body in local.render : !can(regex("__[A-Z_]+__", body))])
      error_message = "An __UPPERCASE__ placeholder survived substitution in deploy/aws/ecs/iam/. Add a replace() for it in modules/iam/main.tf, or the role is created naming a resource that does not exist and every call it guards fails with a 403 that does not say why."
    }
  }
}

resource "aws_iam_role_policy" "workload" {
  name   = "${var.name_prefix}-workload"
  role   = aws_iam_role.workload.id
  policy = local.task_policy
}

resource "aws_iam_role" "execution" {
  name               = "${var.name_prefix}-ecs-execution"
  description        = "What Fargate needs to START a task: ECR pull, log streams, secret injection."
  assume_role_policy = local.render["ecs-tasks-trust-policy"]
  tags               = var.tags
}

resource "aws_iam_role_policy" "execution" {
  name   = "${var.name_prefix}-ecs-execution"
  role   = aws_iam_role.execution.id
  policy = local.execution_policy
}

# ---------------------------------------------------------------------------------------------
# Service-linked roles
#
# AWSServiceRoleForECS and AWSServiceRoleForElasticLoadBalancing are created automatically the
# first time an account uses ECS or ELB through the console or the CLI, and an account that has
# ever done either already has them. An account that has never done either does not, and the
# failure lands late: `aws_ecs_service` returns "Unable to assume the service linked role" after
# the VPC, the NAT gateway, RDS and the ALB all exist and are billing.
#
# Default false, because creating one that already exists fails with InvalidInput ("has been
# taken") and that is the likelier case by far. APPLY.md has the one command that tells you which
# way to set it. This is the rare knob where both settings are wrong for somebody, so it is a
# question the operator answers rather than a guess this module makes.
# ---------------------------------------------------------------------------------------------

resource "aws_iam_service_linked_role" "ecs" {
  count = var.create_service_linked_roles ? 1 : 0

  aws_service_name = "ecs.amazonaws.com"
}

resource "aws_iam_service_linked_role" "elasticloadbalancing" {
  count = var.create_service_linked_roles ? 1 : 0

  aws_service_name = "elasticloadbalancing.amazonaws.com"
}

# ECS Exec is enabled on the gateway service (`enableExecuteCommand: true` in
# gateway.service.json), and it fails with a permissions error that names SSM rather than ECS if the
# task role cannot open the channel. Attached separately so that turning Exec off is one variable
# rather than an edit to the reviewed policy document.
resource "aws_iam_role_policy" "ecs_exec" {
  count = var.enable_ecs_exec ? 1 : 0

  name = "${var.name_prefix}-ecs-exec"
  role = aws_iam_role.workload.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid    = "SsmMessagesForEcsExec"
      Effect = "Allow"
      Action = [
        "ssmmessages:CreateControlChannel",
        "ssmmessages:CreateDataChannel",
        "ssmmessages:OpenControlChannel",
        "ssmmessages:OpenDataChannel",
      ]
      Resource = "*"
    }]
  })
}
