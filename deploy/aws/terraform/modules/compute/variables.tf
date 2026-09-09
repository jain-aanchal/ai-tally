# SPDX-License-Identifier: Apache-2.0

variable "name_prefix" {
  type = string
}

variable "cluster_name" {
  description = "ECS cluster name. gateway.service.json and edge-proxy.service.json both name `ai-tally`."
  type        = string
  default     = "ai-tally"
}

variable "aws_region" {
  type = string
}

variable "ecs_dir" {
  description = "Path to deploy/aws/ecs. The task definitions are read from here, not copied."
  type        = string
}

# --- network ---------------------------------------------------------------------------------

variable "vpc_id" {
  type = string
}

variable "public_subnet_ids" {
  type = list(string)
}

variable "private_subnet_ids" {
  type = list(string)
}

variable "alb_security_group_id" {
  type = string
}

variable "gateway_security_group_id" {
  type = string
}

variable "edge_proxy_security_group_id" {
  type = string
}

variable "has_internet_egress" {
  description = "Whether the private subnets have a default route. Asserted rather than assumed: without it both services fail at runtime, not at apply."
  type        = bool
}

# --- identity and data -----------------------------------------------------------------------

variable "workload_role_arn" {
  type = string
}

variable "execution_role_arn" {
  type = string
}

variable "secret_arns" {
  description = "Map of secret name to full ARN. Used to rewrite the suffix-less ARNs in the task definitions, which ECS cannot resolve."
  type        = map(string)
  default     = {}
}

variable "replay_bucket_name" {
  type = string
}

variable "kms_key_id" {
  description = "Customer-managed key id. When set, it is also passed to the gateway as TALLY_HMAC_SECRETS_KMS_KEY_ID so the per-tenant HMAC secrets the application mints are encrypted with it."
  type        = string
  default     = null
}

variable "db_instance_identifier" {
  type = string
}

variable "log_kms_key_arn" {
  description = "KMS key for CloudWatch Logs. Requires a key policy that allows the logs service principal, which the module's key does not carry by default, so this is null unless you have added it."
  type        = string
  default     = null
}

# --- images and application settings -----------------------------------------------------------

variable "gateway_image" {
  description = "Full image reference for the gateway, e.g. <account>.dkr.ecr.<region>.amazonaws.com/ai-tally/gateway:<sha>. Must be an arm64 manifest: the task definition pins ARM64."
  type        = string
}

variable "edge_proxy_image" {
  description = "Full image reference for the edge proxy. Must be an arm64 manifest."
  type        = string
}

variable "clickhouse_host" {
  description = "ClickHouse Cloud hostname, substituted for REPLACE_CLICKHOUSE_HOST. Created by hand in the ClickHouse Cloud console; see the README's prerequisites."
  type        = string
}

variable "tally_env" {
  description = "TALLY_ENV. `production` makes the gateway refuse to boot with authentication off."
  type        = string
  default     = "production"
}

variable "hmac_key_provider" {
  description = "TALLY_HMAC_KEY_PROVIDER. `kms` selects the AWS Secrets Manager provider. Anything else uses the local provider, whose per-tenant material derives from a root secret in configuration."
  type        = string
  default     = "kms"

  validation {
    condition     = contains(["kms", "secret-manager", "local"], var.hmac_key_provider)
    error_message = "hmac_key_provider must be kms, secret-manager or local."
  }
}

variable "cors_allowed_origins" {
  description = "TALLY_CORS_ALLOWED_ORIGINS, joined with commas. Explicit origins only; a wildcard is refused at boot."
  type        = list(string)
  default     = []

  validation {
    condition     = !contains(var.cors_allowed_origins, "*")
    error_message = "A wildcard CORS origin is refused by the gateway at boot. List the dashboard origins explicitly."
  }
}

variable "gateway_extra_environment" {
  description = "Additional gateway environment variables, merged over what gateway.taskdef.json sets."
  type        = map(string)
  default     = {}
}

variable "edge_proxy_extra_environment" {
  description = "Additional edge proxy environment variables. EDGE_PROXY_UPSTREAM and EDGE_PROXY_PROVIDER default to OpenAI in the task definition; override them here for another provider."
  type        = map(string)
  default     = {}
}

variable "gateway_drop_secrets" {
  description = "Secret entries to remove from the gateway container, by environment variable name. Drop OPENAI_API_KEY and ANTHROPIC_API_KEY if those containers were not created, or the task will not start."
  type        = list(string)
  default     = []
}

# --- ALB and DNS -----------------------------------------------------------------------------

variable "ingest_hostname" {
  description = "Host header routed to the gateway, e.g. ingest.example.com."
  type        = string
}

variable "llm_hostname" {
  description = "Host header routed to the edge proxy, e.g. llm.example.com. This endpoint forwards to provider APIs with real keys, so give it a non-guessable name or put a WAF in front of it."
  type        = string
}

variable "acm_certificate_arn" {
  description = "Existing certificate covering both hostnames. Leave empty to have Terraform request one, which needs route53_zone_id."
  type        = string
  default     = ""
}

variable "route53_zone_id" {
  description = "Hosted zone for DNS validation and for the two alias records. Empty means the zone is elsewhere and you create the records yourself, in which case acm_certificate_arn is required."
  type        = string
  default     = ""
}

variable "internal_alb" {
  type    = bool
  default = false
}

variable "alb_deletion_protection" {
  type    = bool
  default = true
}

variable "alb_idle_timeout" {
  description = "Seconds. Must exceed the longest streamed completion the proxy forwards; EDGE_PROXY_UPSTREAM_TIMEOUT is 10m."
  type        = number
  default     = 660
}

variable "alb_access_logs_bucket" {
  description = "Existing bucket for ALB access logs, with a policy allowing the ELB log-delivery principal. Empty disables access logging."
  type        = string
  default     = ""
}

variable "alb_ssl_policy" {
  type    = string
  default = "ELBSecurityPolicy-TLS13-1-2-2021-06"
}

variable "enable_http_redirect" {
  type    = bool
  default = true
}

variable "edge_proxy_deregistration_delay" {
  type    = number
  default = 120
}

# --- services --------------------------------------------------------------------------------

variable "gateway_desired_count" {
  type    = number
  default = 2
}

variable "edge_proxy_desired_count" {
  description = "At least two. The service has no autoscaling policy by design: this component never scales to zero."
  type        = number
  default     = 2
}

variable "container_insights" {
  description = "Container Insights. Also gates the two task-count alarms, whose metrics only exist when it is on."
  type        = bool
  default     = true
}

# --- observability ---------------------------------------------------------------------------

variable "log_retention_days" {
  type    = number
  default = 30
}

variable "metric_namespace" {
  type    = string
  default = "ai-tally"
}

variable "alarm_sns_topic_arn" {
  description = "Topic for alarm and OK actions. Empty means the alarms exist and notify nobody."
  type        = string
  default     = ""
}

variable "alb_5xx_threshold" {
  type    = number
  default = 5
}

variable "gateway_error_threshold" {
  type    = number
  default = 10
}

variable "gateway_error_log_pattern" {
  description = "CloudWatch Logs filter pattern for gateway error lines. Coupled to the log format, so it silently zeroes if the format changes."
  type        = string
  default     = "?ERROR ?Traceback ?CRITICAL"
}

variable "rds_free_storage_threshold_bytes" {
  type    = number
  default = 2147483648
}

variable "tags" {
  type    = map(string)
  default = {}
}
