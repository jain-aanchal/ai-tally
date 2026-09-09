# SPDX-License-Identifier: Apache-2.0

variable "aws_region" {
  description = "The one region everything runs in. Confirm Fargate ARM64 is available here before applying: all three task definitions pin cpuArchitecture ARM64 and a region without it fails every task at start with an image-manifest error."
  type        = string
}

variable "environment" {
  type    = string
  default = "prod"
}

variable "name_prefix" {
  description = "Prefix for resource names. Leave it as ai-tally unless you also change the IAM documents, which name ai-tally-* secrets and repositories literally."
  type        = string
  default     = "ai-tally"
}

variable "cluster_name" {
  type    = string
  default = "ai-tally"
}

# --- network ---------------------------------------------------------------------------------

variable "vpc_cidr" {
  type    = string
  default = "10.60.0.0/16"
}

variable "availability_zones" {
  description = "Two AZs in aws_region. Named explicitly rather than read from a data source so that a plan is stable across account-specific AZ shuffling."
  type        = list(string)
}

variable "enable_nat_gateway" {
  description = "Required in practice: the gateway reaches ClickHouse Cloud and the proxy reaches provider APIs, neither of which a VPC endpoint covers."
  type        = bool
  default     = true
}

variable "single_nat_gateway" {
  description = "One NAT for both AZs. Cheaper per hour, and a single-AZ failure takes egress out for every task including the edge proxy."
  type        = bool
  default     = true
}

variable "enable_interface_endpoints" {
  description = "Interface endpoints for ECR, Logs, Secrets Manager, STS and KMS. Additive to NAT, not a replacement: they move AWS-bound bytes off the NAT per-GB meter onto a per-endpoint-per-AZ hourly one."
  type        = bool
  default     = false
}

variable "alb_ingress_cidrs" {
  type    = list(string)
  default = ["0.0.0.0/0"]
}

variable "enable_flow_logs" {
  description = "VPC flow logs. Off by default; the meter is CloudWatch Logs GB ingested and stored, and the record count tracks proxied request volume."
  type        = bool
  default     = false
}

variable "alb_access_logs_bucket" {
  description = "Existing bucket for ALB access logs. Empty disables them. Worth setting: it is the only record of who called the proxy hostname."
  type        = string
  default     = ""
}

# --- data ------------------------------------------------------------------------------------

variable "create_kms_key" {
  type    = bool
  default = true
}

variable "replay_bucket_name" {
  description = "Globally unique. Convention: <account-id>-ai-tally-replay."
  type        = string
}

variable "replay_expiry_days" {
  type    = number
  default = 30
}

variable "create_provider_key_secrets" {
  type    = bool
  default = true
}

variable "db_engine_version" {
  type    = string
  default = "16.4"
}

variable "db_instance_class" {
  description = "db.t4g.* is the Graviton family, which matches the ARM64 posture of the task definitions. The runbook suggests db.t3.medium; t4g.medium is the same size on cheaper hardware."
  type        = string
  default     = "db.t4g.medium"
}

variable "db_allocated_storage" {
  type    = number
  default = 20
}

variable "db_multi_az" {
  type    = bool
  default = false
}

variable "db_deletion_protection" {
  type    = bool
  default = true
}

# --- iam -------------------------------------------------------------------------------------

variable "drop_task_policy_sids" {
  description = "Statements removed from task-role-policy.json. BedrockInvoke by default: nothing in the codebase calls Bedrock."
  type        = list(string)
  default     = ["BedrockInvoke"]
}

# --- compute ---------------------------------------------------------------------------------

variable "gateway_image" {
  description = "arm64 image reference. CI publishes SHA tags; pin one rather than a moving tag so a plan says what will actually run."
  type        = string
}

variable "edge_proxy_image" {
  type = string
}

variable "clickhouse_host" {
  description = "ClickHouse Cloud hostname. Created by hand: see the README's prerequisites, and note it must be publicly reachable because Vercel functions egress from the public internet."
  type        = string
}

variable "tally_env" {
  type    = string
  default = "production"
}

variable "hmac_key_provider" {
  type    = string
  default = "kms"
}

variable "cors_allowed_origins" {
  description = "The dashboard's origins. Vercel preview deployments get generated hostnames, so list the production origin and any preview origin you actually need; a wildcard is refused at boot."
  type        = list(string)
  default     = []
}

variable "ingest_hostname" {
  type = string
}

variable "llm_hostname" {
  type = string
}

variable "acm_certificate_arn" {
  type    = string
  default = ""
}

variable "route53_zone_id" {
  type    = string
  default = ""
}

variable "gateway_desired_count" {
  type    = number
  default = 2
}

variable "edge_proxy_desired_count" {
  type    = number
  default = 2
}

variable "log_retention_days" {
  type    = number
  default = 30
}

variable "alarm_sns_topic_arn" {
  type    = string
  default = ""
}

variable "tags" {
  description = "Extra tags merged over the defaults. Cost allocation tags belong here: the AWS Cost Explorer connector filters by one."
  type        = map(string)
  default     = {}
}
