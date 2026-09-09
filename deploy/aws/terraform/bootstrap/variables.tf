# SPDX-License-Identifier: Apache-2.0

variable "aws_region" {
  description = "Region for the state bucket and for the REGION placeholder in the CI ECR policy."
  type        = string
}

variable "name_prefix" {
  description = "Prefix for resource names. Changing it after an apply renames the CI role, which invalidates AWS_ECR_ROLE_ARN in the repository variables."
  type        = string
  default     = "ai-tally"
}

variable "state_bucket_name" {
  description = "Globally unique name for the Terraform state bucket. Convention: <account-id>-ai-tally-tfstate."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", var.state_bucket_name))
    error_message = "state_bucket_name must be a valid S3 bucket name (lowercase, 3-63 characters)."
  }
}

variable "github_repository" {
  description = "owner/repo the CI role may be assumed from. Substituted into the trust policy's subject conditions, which still pin refs/heads/main and refs/tags/v*."
  type        = string
  default     = "jain-aanchal/ai-tally"

  validation {
    condition     = can(regex("^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$", var.github_repository))
    error_message = "github_repository must be owner/repo."
  }
}

variable "create_github_oidc_provider" {
  description = "Create the token.actions.githubusercontent.com OIDC provider. Set false if the account already has one; a second is an error, not a duplicate."
  type        = bool
  default     = true
}

variable "github_oidc_thumbprints" {
  description = "Certificate thumbprints for the GitHub OIDC provider. AWS no longer verifies these for token.actions.githubusercontent.com, so the default is empty and the field is here only for accounts under a policy that still demands one."
  type        = list(string)
  default     = []
}

variable "state_version_retention_days" {
  description = "How long a superseded state version is kept. This is the window in which a bad apply can be rolled back by restoring an object version."
  type        = number
  default     = 90
}

variable "tags" {
  description = "Tags applied to everything this module creates."
  type        = map(string)
  default = {
    Application = "ai-tally"
    ManagedBy   = "terraform"
  }
}
