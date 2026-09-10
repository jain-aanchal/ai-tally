# SPDX-License-Identifier: Apache-2.0

output "cluster_name" {
  value = aws_ecs_cluster.this.name
}

output "alb_dns_name" {
  description = "Use this for the smoke test when DNS lives outside the account and the alias records were not created here."
  value       = aws_lb.this.dns_name
}

output "alb_zone_id" {
  value = aws_lb.this.zone_id
}

output "gateway_url" {
  value = local.gateway_url
}

output "edge_proxy_url" {
  value = "https://${var.llm_hostname}"
}

output "gateway_task_definition_arn" {
  value = aws_ecs_task_definition.gateway.arn
}

output "edge_proxy_task_definition_arn" {
  value = aws_ecs_task_definition.edge_proxy.arn
}

output "certificate_arn" {
  description = "The certificate the HTTPS listener uses, whether Terraform requested it or it was passed in."
  value       = local.certificate_arn
}

output "log_group_names" {
  value = [aws_cloudwatch_log_group.gateway.name, aws_cloudwatch_log_group.edge_proxy.name]
}
