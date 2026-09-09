# SPDX-License-Identifier: Apache-2.0

output "state_bucket" {
  description = "Bucket name to put in backend.hcl as `bucket`."
  value       = aws_s3_bucket.state.id
}

output "ci_ecr_role_arn" {
  description = "Set this as the AWS_ECR_ROLE_ARN repository variable in GitHub. Until it is set, the ECR publishing job PR #351 added stays skipped."
  value       = aws_iam_role.ci_ecr_push.arn
}

output "github_oidc_provider_arn" {
  description = "The OIDC provider this module created, or null when create_github_oidc_provider is false because the account already had one."
  value       = var.create_github_oidc_provider ? aws_iam_openid_connect_provider.github[0].arn : null
}
