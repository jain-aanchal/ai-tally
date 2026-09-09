# SPDX-License-Identifier: Apache-2.0
#
# Bootstrap root module (CTO-335).
#
# WHY this is a SEPARATE root module with LOCAL state: the main root module keeps its state in an S3
# bucket, and a module cannot create the bucket that holds its own state. Splitting the two is the
# only way out of that loop that does not involve typing `aws s3api create-bucket` by hand and then
# lying about the account being fully codified.
#
# It also carries the GitHub OIDC role, which has nothing to do with the loop but everything to do
# with cadence: PR #351's ECR publishing is gated on an `AWS_ECR_ROLE_ARN` repository variable, and
# an operator should be able to unblock CI without standing up a VPC, an RDS instance and an ALB
# first. Both things here are once-per-account and are never destroyed by a teardown of the
# application stack.

data "aws_caller_identity" "current" {}

locals {
  # The IAM documents live in deploy/aws/ecs/iam/ and are the reviewed artifact. They carry
  # `ACCOUNT` / `REGION` placeholders rather than Terraform interpolation, so they are read and
  # substituted rather than templated. Duplicating them in HCL would create two sources of truth
  # that drift; this way a change to the JSON is picked up on the next plan.
  iam_dir = "${path.module}/../../ecs/iam"

  ci_trust_policy = replace(
    replace(
      file("${local.iam_dir}/github-actions-oidc-trust-policy.json"),
      "ACCOUNT", data.aws_caller_identity.current.account_id
    ),
    "jain-aanchal/ai-tally", var.github_repository
  )

  ci_ecr_policy = replace(
    replace(
      file("${local.iam_dir}/github-actions-ecr-policy.json"),
      "ACCOUNT", data.aws_caller_identity.current.account_id
    ),
    "REGION", var.aws_region
  )
}

# ---------------------------------------------------------------------------------------------
# Terraform state
# ---------------------------------------------------------------------------------------------

# No DynamoDB lock table. Terraform 1.11 promoted S3-native locking (`use_lockfile`) to GA and
# deprecated the DynamoDB path, so the lock is a `.tflock` object beside the state object. That
# removes a table, its capacity mode and one more thing to forget to create. The required_version
# in versions.tf is what makes this safe to assume.
resource "aws_s3_bucket" "state" {
  bucket = var.state_bucket_name

  # Losing this bucket means losing the record of what Terraform created, which is worse than
  # losing any single resource in the main stack.
  lifecycle {
    prevent_destroy = true
  }

  tags = var.tags
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Versioning is on and Terraform writes the whole state on every apply, so noncurrent versions
# accumulate for the life of the deployment with nothing expiring them.
resource "aws_s3_bucket_lifecycle_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    id     = "expire-old-state-versions"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      # Long enough to recover from a bad apply, short enough that the bucket does not grow forever.
      noncurrent_days           = var.state_version_retention_days
      newer_noncurrent_versions = 20
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# State is readable by anyone who can read this bucket, which is the whole reason the main module
# refuses to put secret material into it. Refusing plaintext transport is the cheap half of that.
resource "aws_s3_bucket_policy" "state_tls_only" {
  bucket = aws_s3_bucket.state.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource = [
        aws_s3_bucket.state.arn,
        "${aws_s3_bucket.state.arn}/*",
      ]
      Condition = {
        Bool = { "aws:SecureTransport" = "false" }
      }
    }]
  })
}

# ---------------------------------------------------------------------------------------------
# GitHub Actions OIDC, for the image publishing PR #351 added
# ---------------------------------------------------------------------------------------------

# One provider per account. `create_github_oidc_provider = false` is the right setting when the
# account already has one (`aws iam list-open-id-connect-providers`), because a second one is an
# error rather than a duplicate.
resource "aws_iam_openid_connect_provider" "github" {
  count = var.create_github_oidc_provider ? 1 : 0

  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = var.github_oidc_thumbprints

  tags = var.tags
}

resource "aws_iam_role" "ci_ecr_push" {
  name = "${var.name_prefix}-ci-ecr-push"

  # Straight from deploy/aws/ecs/iam/github-actions-oidc-trust-policy.json: the subject condition
  # pins main and vX.Y.Z tags, so a fork or a pull-request run cannot assume this role.
  assume_role_policy = local.ci_trust_policy

  tags = var.tags

  # The trust policy names the OIDC provider by ARN, so the provider has to exist first. Terraform
  # cannot see that dependency through a string, hence the explicit edge.
  depends_on = [aws_iam_openid_connect_provider.github]
}

resource "aws_iam_role_policy" "ci_ecr_push" {
  name   = "${var.name_prefix}-ci-ecr-push"
  role   = aws_iam_role.ci_ecr_push.id
  policy = local.ci_ecr_policy
}
