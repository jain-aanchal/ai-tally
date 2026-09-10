# SPDX-License-Identifier: Apache-2.0

variable "name_prefix" {
  type = string
}

variable "environment" {
  description = "Environment name, used only to disambiguate the RDS final-snapshot identifier."
  type        = string
  default     = "prod"
}

variable "repository_namespace" {
  description = "ECR namespace. Must stay ai-tally: the execution role policy and both task definitions name ai-tally/gateway and ai-tally/edge-proxy literally."
  type        = string
  default     = "ai-tally"
}

variable "private_subnet_ids" {
  type = list(string)
}

variable "rds_security_group_id" {
  type = string
}

variable "create_kms_key" {
  description = "Create a customer-managed key. False leaves everything on AWS-managed keys, in which case the kms:ViaService statements in the IAM documents must be dropped rather than pointed at nothing."
  type        = bool
  default     = true
}

variable "replay_bucket_name" {
  description = "Globally unique bucket for replay bodies. Substituted for __REPLAY_BUCKET__ in task-role-policy.json and in gateway.taskdef.json."
  type        = string
}

variable "replay_prefix" {
  description = "Key prefix the gateway writes under (TALLY_REPLAY_S3_PREFIX). The lifecycle rule is scoped to it."
  type        = string
  default     = "replay/"
}

variable "replay_expiry_days" {
  description = "Expiry for replay bodies. The code deliberately does not manage this; nothing else will."
  type        = number
  default     = 30
}

variable "replay_noncurrent_expiry_days" {
  type    = number
  default = 7
}

variable "ecr_untagged_expiry_days" {
  type    = number
  default = 7
}

variable "ecr_image_count_limit" {
  type    = number
  default = 30
}

variable "create_provider_key_secrets" {
  description = "Create the optional OPENAI_API_KEY and ANTHROPIC_API_KEY containers. False when the deployment makes no outbound provider calls from the gateway."
  type        = bool
  default     = true
}

variable "secret_recovery_window_days" {
  description = "Secrets Manager recovery window. 0 deletes immediately, which makes a destroy-and-recreate cycle possible but is not what you want in production."
  type        = number
  default     = 30
}

# No defaults here either, for the reason the root module's variables.tf spells out: both are facts
# about the target region that this repository cannot know, and a wrong one fails at apply after the
# VPC exists. A default here would quietly reintroduce the guess the root module removed.
variable "db_engine_version" {
  description = "RDS for PostgreSQL engine version. Discover with: aws rds describe-db-engine-versions --engine postgres --region <region> --query 'DBEngineVersions[].EngineVersion' --output text"
  type        = string
}

variable "db_instance_class" {
  description = "RDS instance class. Confirm it is orderable for db_engine_version in this region with: aws rds describe-orderable-db-instance-options --engine postgres --engine-version <version> --region <region> --query 'OrderableDBInstanceOptions[].DBInstanceClass' --output text"
  type        = string
}

variable "db_allocated_storage" {
  type    = number
  default = 20
}

variable "db_max_allocated_storage" {
  description = "Storage autoscaling ceiling. The free-storage alarm fires long before this, but a control plane that fills its disk stops accepting tenants."
  type        = number
  default     = 100
}

variable "db_name" {
  type    = string
  default = "tally"
}

variable "db_master_username" {
  type    = string
  default = "tally"
}

variable "db_multi_az" {
  description = "Multi-AZ. Off matches the runbook's suggestion and doubles as the honest default for a control plane whose outage does not lose telemetry."
  type        = bool
  default     = false
}

variable "db_backup_retention_days" {
  type    = number
  default = 7
}

variable "db_backup_window" {
  type    = string
  default = "07:00-08:00"
}

variable "db_maintenance_window" {
  type    = string
  default = "Sun:08:30-Sun:09:30"
}

variable "db_deletion_protection" {
  type    = bool
  default = true
}

variable "db_skip_final_snapshot" {
  type    = bool
  default = false
}

variable "db_performance_insights_enabled" {
  type    = bool
  default = false
}

variable "tags" {
  type    = map(string)
  default = {}
}

# Retention for the RDS postgresql log group this module creates. Shares the root's single
# log_retention_days knob with the compute module rather than adding a second one: both exist for
# the same reason, which is that a group created implicitly by AWS keeps every line forever.
variable "log_retention_days" {
  type    = number
  default = 30
}
