# SPDX-License-Identifier: Apache-2.0

output "vpc_id" {
  value = aws_vpc.this.id
}

output "public_subnet_ids" {
  value = aws_subnet.public[*].id
}

output "private_subnet_ids" {
  value = aws_subnet.private[*].id
}

output "alb_security_group_id" {
  value = aws_security_group.alb.id
}

output "gateway_security_group_id" {
  value = aws_security_group.gateway.id
}

output "edge_proxy_security_group_id" {
  value = aws_security_group.edge_proxy.id
}

output "rds_security_group_id" {
  value = aws_security_group.rds.id
}

output "has_internet_egress" {
  description = "False means the private subnets cannot reach ClickHouse Cloud or any provider API. The compute module asserts on this rather than letting tasks fail at runtime."
  value       = var.enable_nat_gateway
}
