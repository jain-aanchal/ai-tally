# SPDX-License-Identifier: Apache-2.0
#
# Every output here is an identifier or an ARN. None is a credential, and none should become one.

output "kms_key_arn" {
  value = local.kms_key_arn
}

output "kms_key_id" {
  description = "Substituted for REPLACE_KMS_KEY_ID in the two IAM policy documents."
  value       = local.kms_key_id
}

output "ecr_repository_urls" {
  value = { for k, v in aws_ecr_repository.this : k => v.repository_url }
}

output "replay_bucket_name" {
  value = aws_s3_bucket.replay.id
}

output "replay_bucket_arn" {
  value = aws_s3_bucket.replay.arn
}

output "db_endpoint" {
  value = aws_db_instance.this.endpoint
}

output "db_address" {
  value = aws_db_instance.this.address
}

output "db_name" {
  value = aws_db_instance.this.db_name
}

output "db_master_user_secret_arn" {
  description = "The secret RDS mints and owns. Read it to assemble TALLY_POSTGRES_DSN; the password itself never passes through Terraform."
  value       = try(aws_db_instance.this.master_user_secret[0].secret_arn, null)
}

output "secret_arns" {
  description = "Empty containers. Each must be given a value before the gateway will start."
  value       = { for k, v in aws_secretsmanager_secret.this : k => v.arn }
}
