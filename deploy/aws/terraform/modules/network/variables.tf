# SPDX-License-Identifier: Apache-2.0

variable "name_prefix" {
  type = string
}

variable "aws_region" {
  type = string
}

variable "vpc_cidr" {
  description = "VPC CIDR. Split into one /20 per AZ for public and one for private, so a /16 is the assumed shape."
  type        = string
}

variable "availability_zones" {
  description = "AZs to spread across. Two is the documented topology; more works and costs more NAT if single_nat_gateway is false."
  type        = list(string)

  validation {
    condition     = length(var.availability_zones) >= 2
    error_message = "At least two AZs are required: the ALB needs two subnets and the edge proxy runs two tasks across two AZs."
  }
}

variable "enable_nat_gateway" {
  description = "Give the private subnets a default route. Required for ClickHouse Cloud and for the edge proxy's upstream providers; false only makes sense for a network-only test apply."
  type        = bool
  default     = true
}

variable "single_nat_gateway" {
  description = "One NAT for all AZs (cheaper, and a single-AZ failure takes egress out everywhere) rather than one per AZ."
  type        = bool
  default     = true
}

variable "enable_interface_endpoints" {
  description = "Create interface endpoints for ECR API/DKR, Logs, Secrets Manager, STS and KMS. Trades a per-endpoint-per-AZ hourly meter for less NAT per-GB and less internet exposure. Off by default because the ratio that decides it is unmeasured for this workload."
  type        = bool
  default     = false
}

variable "alb_ingress_cidrs" {
  description = "CIDRs allowed to reach the ALB on 443. Narrow this if ingest and the proxy are called from known networks."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "allow_edge_proxy_to_rds" {
  description = "Allow 5432 from the edge proxy security group. Nothing in the proxy uses it today."
  type        = bool
  default     = false
}

variable "enable_flow_logs" {
  description = "VPC flow logs to CloudWatch. Meter is GB ingested plus GB stored, and the record count tracks request volume through the proxy."
  type        = bool
  default     = false
}

variable "flow_logs_retention_days" {
  type    = number
  default = 14
}

variable "tags" {
  type    = map(string)
  default = {}
}
