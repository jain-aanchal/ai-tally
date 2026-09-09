# SPDX-License-Identifier: Apache-2.0
#
# ai-tally on AWS: the application stack (CTO-335).
#
# MODULE BOUNDARIES, and why they are where they are. Four modules, one root.
#
#   network  changes rarely; a mistake here takes everything down and a destroy strands an ALB.
#   data     changes rarely; a destroy is unrecoverable (the control plane and the replay corpus).
#   iam      changes whenever a policy document in deploy/aws/ecs/iam/ changes; a mistake is a 403.
#   compute  changes on every deploy; a mistake rolls back on its own via the circuit breaker.
#
# That is four different blast radii and four different cadences, so they are four modules. They are
# composed into ONE root rather than four, because the alternative is four state files wired
# together with remote-state data sources, and the coupling here is dense: compute needs subnet ids,
# security group ids, role ARNs, secret ARNs, a bucket name, a KMS key id and an RDS identifier.
# Splitting that across states buys isolation nobody asked for and costs a documented apply order
# that is wrong the first time somebody skips a step.
#
# What IS a separate root module is bootstrap/, and only that, because it creates the bucket this
# root's state lives in and the CI role PR #351 needs before any of this exists.

locals {
  # Read from the repository, not copied into the module. A change to a task definition or an IAM
  # document shows up in the next plan.
  ecs_dir = "${path.module}/../ecs"

  tags = merge({
    Application = "ai-tally"
    ManagedBy   = "terraform"
    Environment = var.environment
  }, var.tags)
}

module "network" {
  source = "./modules/network"

  name_prefix        = var.name_prefix
  aws_region         = var.aws_region
  vpc_cidr           = var.vpc_cidr
  availability_zones = var.availability_zones

  enable_nat_gateway         = var.enable_nat_gateway
  single_nat_gateway         = var.single_nat_gateway
  enable_interface_endpoints = var.enable_interface_endpoints
  alb_ingress_cidrs          = var.alb_ingress_cidrs
  enable_flow_logs           = var.enable_flow_logs

  tags = local.tags
}

module "data" {
  source = "./modules/data"

  name_prefix           = var.name_prefix
  environment           = var.environment
  private_subnet_ids    = module.network.private_subnet_ids
  rds_security_group_id = module.network.rds_security_group_id

  create_kms_key              = var.create_kms_key
  replay_bucket_name          = var.replay_bucket_name
  replay_expiry_days          = var.replay_expiry_days
  create_provider_key_secrets = var.create_provider_key_secrets

  db_engine_version      = var.db_engine_version
  db_instance_class      = var.db_instance_class
  db_allocated_storage   = var.db_allocated_storage
  db_multi_az            = var.db_multi_az
  db_deletion_protection = var.db_deletion_protection

  tags = local.tags
}

module "iam" {
  source = "./modules/iam"

  name_prefix           = var.name_prefix
  aws_region            = var.aws_region
  ecs_dir               = local.ecs_dir
  replay_bucket_name    = module.data.replay_bucket_name
  kms_key_id            = module.data.kms_key_id
  drop_task_policy_sids = var.drop_task_policy_sids

  tags = local.tags
}

module "compute" {
  source = "./modules/compute"

  name_prefix  = var.name_prefix
  cluster_name = var.cluster_name
  aws_region   = var.aws_region
  ecs_dir      = local.ecs_dir

  vpc_id                       = module.network.vpc_id
  public_subnet_ids            = module.network.public_subnet_ids
  private_subnet_ids           = module.network.private_subnet_ids
  alb_security_group_id        = module.network.alb_security_group_id
  gateway_security_group_id    = module.network.gateway_security_group_id
  edge_proxy_security_group_id = module.network.edge_proxy_security_group_id
  has_internet_egress          = module.network.has_internet_egress

  workload_role_arn  = module.iam.workload_role_arn
  execution_role_arn = module.iam.execution_role_arn

  secret_arns            = module.data.secret_arns
  replay_bucket_name     = module.data.replay_bucket_name
  kms_key_id             = module.data.kms_key_id
  db_instance_identifier = "${var.name_prefix}-pg"

  gateway_image    = var.gateway_image
  edge_proxy_image = var.edge_proxy_image
  clickhouse_host  = var.clickhouse_host

  tally_env            = var.tally_env
  hmac_key_provider    = var.hmac_key_provider
  cors_allowed_origins = var.cors_allowed_origins

  # When the provider-key containers were not created, the entries that reference them have to come
  # out of the container definition too, or the task fails to start on a secret that does not exist.
  gateway_drop_secrets = var.create_provider_key_secrets ? [] : ["OPENAI_API_KEY", "ANTHROPIC_API_KEY"]

  ingest_hostname     = var.ingest_hostname
  llm_hostname        = var.llm_hostname
  acm_certificate_arn = var.acm_certificate_arn
  route53_zone_id     = var.route53_zone_id

  gateway_desired_count    = var.gateway_desired_count
  edge_proxy_desired_count = var.edge_proxy_desired_count
  log_retention_days       = var.log_retention_days
  alarm_sns_topic_arn      = var.alarm_sns_topic_arn
  alb_access_logs_bucket   = var.alb_access_logs_bucket

  tags = local.tags
}
