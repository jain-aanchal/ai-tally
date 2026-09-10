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
  description = <<-EOT
    Two AZs in aws_region. Named explicitly rather than read from a data source so that a plan is
    stable across account-specific AZ shuffling. AZ names are account-scoped, and not every AZ in a
    region offers every service, so list the ones your account actually has:

      aws ec2 describe-availability-zones --region "$REGION" \
        --query 'AvailabilityZones[?State==`available`].ZoneName' --output text
  EOT
  type        = list(string)

  validation {
    # An AZ from another region is a mistake nobody catches by reading: the subnet create fails
    # partway through the network module, leaving a VPC and a half-built subnet set behind.
    condition     = alltrue([for az in var.availability_zones : startswith(az, var.aws_region)])
    error_message = "Every availability_zones entry must be in aws_region, e.g. [\"us-east-1a\", \"us-east-1b\"] for aws_region = \"us-east-1\"."
  }

  validation {
    condition     = length(var.availability_zones) >= 2 && length(distinct(var.availability_zones)) == length(var.availability_zones)
    error_message = "availability_zones needs at least two distinct AZs: the RDS subnet group and the ALB both require two."
  }
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

variable "replay_prefix" {
  description = "Key prefix the gateway writes replay bodies under. One variable feeds both TALLY_REPLAY_S3_PREFIX and the bucket lifecycle filter: they used to be set independently, the gateway's default was the empty string, and nothing the gateway wrote ever matched the rule that was supposed to expire it."
  type        = string
  default     = "replay/"
}

variable "replay_expiry_days" {
  type    = number
  default = 30
}

variable "create_provider_key_secrets" {
  type    = bool
  default = true
}

# REQUIRED, AND DELIBERATELY WITHOUT A DEFAULT (CTO-360).
#
# Both of these are facts about your region and your account, not preferences, and neither can be
# known from this repository. This module had `db_engine_version = "16.4"` and
# `db_instance_class = "db.t4g.medium"` as defaults; nobody had ever run a plan against a real
# account, so both were guesses. A Postgres minor version that RDS has deprecated, or an instance
# class that region does not offer for that engine version, fails at APPLY time, after the VPC, the
# NAT gateway and the KMS key already exist. That is the worst moment to find out, and the recovery
# is a half-built stack and a `terraform destroy`.
#
# Requiring them moves the failure to the front: Terraform refuses to plan at all until you have
# looked the values up, and the lookup is one command each. The validation blocks catch a
# malformed value in the same breath. Neither can prove the value exists in your region, and this
# comment does not pretend otherwise.

variable "db_engine_version" {
  description = <<-EOT
    RDS for PostgreSQL engine version, e.g. "16.8". No default: a version that does not exist in
    aws_region fails at apply, after the VPC exists. Discover the valid values for your region:

      aws rds describe-db-engine-versions --engine postgres --region "$REGION" \
        --query 'DBEngineVersions[].EngineVersion' --output text | tr '\t' '\n' | sort -V

    Add --default-only for the one AWS would pick, or filter with
    --engine-version 16 to list only the 16.x line.
  EOT
  type        = string

  validation {
    condition     = can(regex("^[0-9]+(\\.[0-9]+)?$", var.db_engine_version))
    error_message = "db_engine_version must be a Postgres version like \"16\" or \"16.8\". Run: aws rds describe-db-engine-versions --engine postgres --region <region> --query 'DBEngineVersions[].EngineVersion' --output text"
  }
}

variable "db_instance_class" {
  description = <<-EOT
    RDS instance class, e.g. "db.t4g.medium". No default: an instance class this region does not
    offer for this engine version fails at apply, in the same place db_engine_version does. The
    db.t4g.* Graviton family matches the ARM64 posture of the task definitions and is the same size
    as the runbook's db.t3.medium on cheaper hardware, so it is the one to try first. Confirm the
    combination is orderable before you apply:

      aws rds describe-orderable-db-instance-options --engine postgres \
        --engine-version "$DB_ENGINE_VERSION" --region "$REGION" \
        --query 'OrderableDBInstanceOptions[].DBInstanceClass' --output text | tr '\t' '\n' | sort -u
  EOT
  type        = string

  validation {
    condition     = can(regex("^db\\.[a-z0-9]+\\.[a-z0-9]+$", var.db_instance_class))
    error_message = "db_instance_class must look like db.t4g.medium. Run: aws rds describe-orderable-db-instance-options --engine postgres --engine-version <version> --region <region> --query 'OrderableDBInstanceOptions[].DBInstanceClass' --output text"
  }
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

variable "clickhouse_port" {
  description = "TALLY_CLICKHOUSE_PORT. 8443 is ClickHouse Cloud's only HTTP port and is what makes the gateway's client speak TLS; 8123 is the compose stack's plaintext port and reaches nothing in Cloud."
  type        = number
  default     = 8443
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
