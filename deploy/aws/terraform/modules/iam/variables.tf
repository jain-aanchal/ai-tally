# SPDX-License-Identifier: Apache-2.0

variable "name_prefix" {
  type = string
}

variable "aws_region" {
  type = string
}

variable "ecs_dir" {
  description = "Path to deploy/aws/ecs. The IAM documents and the task definitions are read from here rather than copied, so a change there lands on the next plan."
  type        = string
}

variable "replay_bucket_name" {
  description = "Substituted for REPLACE_REPLAY_BUCKET in task-role-policy.json, which is the placeholder PR #352 left in place of the old hard-coded my-org-ai-tally-replay."
  type        = string
}

variable "kms_key_id" {
  description = "Customer-managed key id. Null or empty removes both kms:ViaService statements rather than leaving them pointed at a key that does not exist."
  type        = string
  default     = null
}

variable "drop_task_policy_sids" {
  description = "Statement Sids to remove from task-role-policy.json. BedrockInvoke is dropped by default: nothing in the codebase calls Bedrock, so it is unearned privilege. Add CostExplorerRead if no tenant enables the compute connector, and AssumeTenantConnectorRoles if every credentials_ref is aws-default-chain."
  type        = list(string)
  default     = ["BedrockInvoke"]
}

variable "enable_ecs_exec" {
  description = "Grant the ssmmessages actions ECS Exec needs. gateway.service.json sets enableExecuteCommand: true."
  type        = bool
  default     = true
}

variable "tags" {
  type    = map(string)
  default = {}
}
