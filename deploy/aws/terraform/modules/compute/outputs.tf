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

output "certificate_validation_records" {
  description = "Populated when Terraform requested a certificate but no Route 53 zone was given. Create these records in whatever zone holds the domain, or the certificate never validates and the HTTPS listener never comes up."
  value = var.acm_certificate_arn == "" && var.route53_zone_id == "" ? [
    for option in aws_acm_certificate.this[0].domain_validation_options : {
      name  = option.resource_record_name
      type  = option.resource_record_type
      value = option.resource_record_value
    }
  ] : []
}

output "log_group_names" {
  value = [aws_cloudwatch_log_group.gateway.name, aws_cloudwatch_log_group.edge_proxy.name]
}
