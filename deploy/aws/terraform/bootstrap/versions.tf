# SPDX-License-Identifier: Apache-2.0

terraform {
  # 1.11 is the floor because the main root module uses S3-native state locking (`use_lockfile`),
  # which went GA there. Bootstrapping with an older CLI would create a bucket the main module
  # cannot lock against, which fails later and confusingly rather than here and clearly.
  required_version = ">= 1.11.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = var.tags
  }
}
