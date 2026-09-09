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
  # AWS-managed aws/secretsmanager key needs no grant at all.
  auto_dropped_sids = var.kms_key_id == null || var.kms_key_id == "" ? ["SecretsManagerKmsUse", "SecretsInjectionKmsDecrypt"] : []

  dropped_task_sids      = distinct(concat(local.auto_dropped_sids, var.drop_task_policy_sids))
  dropped_execution_sids = local.auto_dropped_sids

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
