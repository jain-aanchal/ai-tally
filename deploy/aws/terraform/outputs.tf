# SPDX-License-Identifier: Apache-2.0
#
# No output here is a credential. The secret outputs are ARNs of containers this module created but
# never wrote a value into; the RDS master password lives in a secret RDS owns and is not reachable
# from state at all.

output "vpc_id" {
  value = module.network.vpc_id
}

output "private_subnet_ids" {
  value = module.network.private_subnet_ids
}

output "ecr_repository_urls" {
  value = module.data.ecr_repository_urls
}

output "replay_bucket_name" {
  value = module.data.replay_bucket_name
}

output "db_address" {
  value = module.data.db_address
}

output "db_master_user_secret_arn" {
  description = "Read this to assemble TALLY_POSTGRES_DSN. See the README's step 4."
  value       = module.data.db_master_user_secret_arn
}

output "secret_arns" {
  description = "Empty containers until you write values into them. The gateway will not start before you do."
  value       = module.data.secret_arns
}

output "workload_role_arn" {
  value = module.iam.workload_role_arn
}

output "execution_role_arn" {
  value = module.iam.execution_role_arn
}

output "kms_key_arn" {
  value = module.data.kms_key_arn
}

output "alb_dns_name" {
  value = module.compute.alb_dns_name
}

output "gateway_url" {
  value = module.compute.gateway_url
}

output "edge_proxy_url" {
  value = module.compute.edge_proxy_url
}

output "certificate_arn" {
  description = "The certificate the HTTPS listener uses, whether Terraform requested it or it was passed in as acm_certificate_arn."
  value       = module.compute.certificate_arn
}

output "cluster_name" {
  value = module.compute.cluster_name
}
