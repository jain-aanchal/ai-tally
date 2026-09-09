# SPDX-License-Identifier: Apache-2.0

output "workload_role_arn" {
  value = aws_iam_role.workload.arn
}

output "execution_role_arn" {
  value = aws_iam_role.execution.arn
}

output "task_policy_json" {
  description = "The rendered workload policy, so a reviewer can diff what was actually attached against deploy/aws/ecs/iam/task-role-policy.json without an AWS call."
  value       = local.task_policy
}

output "execution_policy_json" {
  value = local.execution_policy
}
