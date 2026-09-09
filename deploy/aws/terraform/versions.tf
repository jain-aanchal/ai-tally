# SPDX-License-Identifier: Apache-2.0

terraform {
  # 1.11 is where S3-native state locking (`use_lockfile`) went GA, which is what lets this stack
  # keep its state in a bucket with no DynamoDB table anywhere in the picture.
  required_version = ">= 1.11.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # Partial configuration. The bucket and key come from backend.hcl, which is per-deployment and is
  # not committed, so that `terraform init -backend=false` works for validation with no account and
  # no credentials. See backend.hcl.example and the README.
  backend "s3" {}
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = var.tags
  }
}
