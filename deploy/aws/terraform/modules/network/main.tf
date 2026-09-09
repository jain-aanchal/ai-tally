# SPDX-License-Identifier: Apache-2.0
#
# Networking (CTO-335): VPC, two public and two private subnets across two AZs, egress, and the four
# security groups the topology in docs/aws-bundle-scope.md describes.
#
# EGRESS, AND WHY NAT IS ON BY DEFAULT. The scope doc floats VPC endpoints "instead of" a NAT
# gateway. That is not available to this workload, and the distinction matters before anyone plans a
# bill around it. Interface endpoints cover AWS service traffic only. The gateway task has to reach
# ClickHouse Cloud on 8443 and the edge proxy exists to reach api.openai.com and friends, both of
# which are the public internet. With `assignPublicIp: DISABLED` in both service definitions, a
# private subnet with no NAT reaches neither, and the tasks fail at runtime rather than at apply.
#
# So the two are additive, not alternatives:
#   * NAT gateway: required. Meter is per NAT-hour plus per GB processed.
#   * S3 gateway endpoint: always on. It has no hourly charge and it keeps ECR layer downloads (which
#     are S3 GETs) off the NAT's per-GB meter, which is the single largest AWS-bound flow here.
#   * Interface endpoints for ECR API/DKR, Logs, Secrets Manager, STS and KMS: off by default. Meter
#     is per endpoint per AZ per hour plus per GB. Whether they pay for themselves depends on how
#     often tasks pull images and how chatty the logs are, and neither is measured for this workload.
#     Turning them on also tightens the blast radius, because that traffic stops leaving the VPC.
#
# No dollar figure appears here or anywhere in this module set, deliberately, matching the cost
# section of docs/aws-bundle-scope.md.

locals {
  az_count = length(var.availability_zones)

  # A /16 split into /20s gives four subnets with room left over, and keeps the arithmetic readable
  # for anyone reviewing a plan against their own CIDR.
  public_subnet_cidrs  = [for i in range(local.az_count) : cidrsubnet(var.vpc_cidr, 4, i)]
  private_subnet_cidrs = [for i in range(local.az_count) : cidrsubnet(var.vpc_cidr, 4, i + local.az_count)]

  nat_count = var.enable_nat_gateway ? (var.single_nat_gateway ? 1 : local.az_count) : 0

  interface_endpoint_services = var.enable_interface_endpoints ? [
    "ecr.api",
    "ecr.dkr",
    "logs",
    "secretsmanager",
    "sts",
    "kms",
  ] : []
}

resource "aws_vpc" "this" {
  cidr_block = var.vpc_cidr

  # Both are required for interface endpoints to resolve to their private addresses, and harmless
  # when the endpoints are off.
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = merge(var.tags, { Name = var.name_prefix })
}

# Every VPC comes with a default security group that allows all traffic between anything attached to
# it. Nothing here attaches to it, but it stays a standing "allow all" that the next person to launch
# something quickly will land in. Adopting it with no rules closes that.
resource "aws_default_security_group" "this" {
  vpc_id = aws_vpc.this.id
  tags   = merge(var.tags, { Name = "${var.name_prefix}-default-deny-all" })
}

# Flow logs. Off by default, and named rather than silently omitted: the meter is CloudWatch Logs GB
# ingested plus GB stored, and for a VPC carrying LLM proxy traffic the record count tracks request
# volume. Turn it on when you need to answer "did this task reach that endpoint", which is the
# question the security groups here cannot answer after the fact.
resource "aws_flow_log" "this" {
  count = var.enable_flow_logs ? 1 : 0

  vpc_id               = aws_vpc.this.id
  traffic_type         = "ALL"
  log_destination_type = "cloud-watch-logs"
  log_destination      = aws_cloudwatch_log_group.flow_logs[0].arn
  iam_role_arn         = aws_iam_role.flow_logs[0].arn

  tags = merge(var.tags, { Name = "${var.name_prefix}-flow-logs" })
}

resource "aws_cloudwatch_log_group" "flow_logs" {
  count = var.enable_flow_logs ? 1 : 0

  name              = "/vpc/${var.name_prefix}-flow-logs"
  retention_in_days = var.flow_logs_retention_days
  tags              = var.tags
}

resource "aws_iam_role" "flow_logs" {
  count = var.enable_flow_logs ? 1 : 0

  name = "${var.name_prefix}-vpc-flow-logs"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "vpc-flow-logs.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = var.tags
}

resource "aws_iam_role_policy" "flow_logs" {
  count = var.enable_flow_logs ? 1 : 0

  name = "${var.name_prefix}-vpc-flow-logs"
  role = aws_iam_role.flow_logs[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "logs:CreateLogStream",
        "logs:PutLogEvents",
        "logs:DescribeLogGroups",
        "logs:DescribeLogStreams",
      ]
      Resource = "${aws_cloudwatch_log_group.flow_logs[0].arn}:*"
    }]
  })
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = merge(var.tags, { Name = var.name_prefix })
}

resource "aws_subnet" "public" {
  count = local.az_count

  vpc_id            = aws_vpc.this.id
  cidr_block        = local.public_subnet_cidrs[count.index]
  availability_zone = var.availability_zones[count.index]

  # Only the ALB and the NAT gateways live here, and both get an explicit EIP. Nothing should get a
  # public address by default.
  map_public_ip_on_launch = false

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-public-${var.availability_zones[count.index]}"
    Tier = "public"
  })
}

resource "aws_subnet" "private" {
  count = local.az_count

  vpc_id            = aws_vpc.this.id
  cidr_block        = local.private_subnet_cidrs[count.index]
  availability_zone = var.availability_zones[count.index]

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-private-${var.availability_zones[count.index]}"
    Tier = "private"
  })
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  tags   = merge(var.tags, { Name = "${var.name_prefix}-public" })
}

resource "aws_route" "public_default" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.this.id
}

resource "aws_route_table_association" "public" {
  count          = local.az_count
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_eip" "nat" {
  count  = local.nat_count
  domain = "vpc"
  tags   = merge(var.tags, { Name = "${var.name_prefix}-nat-${count.index}" })
}

resource "aws_nat_gateway" "this" {
  count = local.nat_count

  allocation_id = aws_eip.nat[count.index].id
  subnet_id     = aws_subnet.public[count.index].id
  tags          = merge(var.tags, { Name = "${var.name_prefix}-nat-${count.index}" })

  depends_on = [aws_internet_gateway.this]
}

# One route table per private subnet even with a single NAT, so that flipping single_nat_gateway to
# false later is a route change rather than a subnet re-association.
resource "aws_route_table" "private" {
  count = local.az_count

  vpc_id = aws_vpc.this.id
  tags   = merge(var.tags, { Name = "${var.name_prefix}-private-${var.availability_zones[count.index]}" })
}

resource "aws_route" "private_default" {
  count = var.enable_nat_gateway ? local.az_count : 0

  route_table_id         = aws_route_table.private[count.index].id
  destination_cidr_block = "0.0.0.0/0"

  # With a single NAT, every AZ routes through the one in AZ 0. That is a deliberate cost default
  # and a real availability trade: losing that AZ takes egress out for every task, including the
  # edge proxy, whose outage is an outage of somebody else's product. Set single_nat_gateway = false
  # for a production deployment of the proxy.
  nat_gateway_id = var.single_nat_gateway ? aws_nat_gateway.this[0].id : aws_nat_gateway.this[count.index].id
}

resource "aws_route_table_association" "private" {
  count          = local.az_count
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[count.index].id
}

# ---------------------------------------------------------------------------------------------
# VPC endpoints
# ---------------------------------------------------------------------------------------------

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${var.aws_region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = aws_route_table.private[*].id

  tags = merge(var.tags, { Name = "${var.name_prefix}-s3" })
}

resource "aws_security_group" "vpc_endpoints" {
  count = var.enable_interface_endpoints ? 1 : 0

  name        = "${var.name_prefix}-vpc-endpoints"
  description = "Interface VPC endpoints. Accepts 443 from inside the VPC only."
  vpc_id      = aws_vpc.this.id

  tags = merge(var.tags, { Name = "${var.name_prefix}-vpc-endpoints" })
}

resource "aws_vpc_security_group_ingress_rule" "endpoints_https" {
  count = var.enable_interface_endpoints ? 1 : 0

  security_group_id = aws_security_group.vpc_endpoints[0].id
  description       = "HTTPS from within the VPC"
  cidr_ipv4         = var.vpc_cidr
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

resource "aws_vpc_endpoint" "interface" {
  for_each = toset(local.interface_endpoint_services)

  vpc_id              = aws_vpc.this.id
  service_name        = "com.amazonaws.${var.aws_region}.${each.value}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.vpc_endpoints[0].id]
  private_dns_enabled = true

  tags = merge(var.tags, { Name = "${var.name_prefix}-${each.value}" })
}

# ---------------------------------------------------------------------------------------------
# Security groups
#
# Rules are separate aws_vpc_security_group_*_rule resources rather than inline blocks so that a
# plan shows one rule changing instead of the whole group being replaced.
# ---------------------------------------------------------------------------------------------

resource "aws_security_group" "alb" {
  name        = "${var.name_prefix}-alb"
  description = "Public ALB fronting the gateway and the edge proxy."
  vpc_id      = aws_vpc.this.id
  tags        = merge(var.tags, { Name = "${var.name_prefix}-alb" })
}

resource "aws_vpc_security_group_ingress_rule" "alb_https" {
  for_each = toset(var.alb_ingress_cidrs)

  security_group_id = aws_security_group.alb.id
  description       = "HTTPS from the internet"
  cidr_ipv4         = each.value
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "alb_to_gateway" {
  security_group_id            = aws_security_group.alb.id
  description                  = "Gateway target group"
  referenced_security_group_id = aws_security_group.gateway.id
  from_port                    = 8080
  to_port                      = 8080
  ip_protocol                  = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "alb_to_edge_proxy" {
  security_group_id            = aws_security_group.alb.id
  description                  = "Edge proxy target group"
  referenced_security_group_id = aws_security_group.edge_proxy.id
  from_port                    = 8088
  to_port                      = 8088
  ip_protocol                  = "tcp"
}

resource "aws_security_group" "gateway" {
  name        = "${var.name_prefix}-gateway"
  description = "Ingest gateway tasks. 8080 from the ALB only."
  vpc_id      = aws_vpc.this.id
  tags        = merge(var.tags, { Name = "${var.name_prefix}-gateway" })
}

resource "aws_vpc_security_group_ingress_rule" "gateway_from_alb" {
  security_group_id            = aws_security_group.gateway.id
  description                  = "8080 from the ALB"
  referenced_security_group_id = aws_security_group.alb.id
  from_port                    = 8080
  to_port                      = 8080
  ip_protocol                  = "tcp"
}

# Egress stays open: the gateway reaches ClickHouse Cloud on 8443, provider APIs, Secrets Manager,
# ECR and the CDP and revenue connectors, and pinning that to a CIDR list would need every vendor's
# egress ranges to be correct forever.
resource "aws_vpc_security_group_egress_rule" "gateway_all" {
  security_group_id = aws_security_group.gateway.id
  description       = "All egress"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

resource "aws_security_group" "edge_proxy" {
  name        = "${var.name_prefix}-edge-proxy"
  description = "Edge proxy tasks. 8088 from the ALB only."
  vpc_id      = aws_vpc.this.id
  tags        = merge(var.tags, { Name = "${var.name_prefix}-edge-proxy" })
}

resource "aws_vpc_security_group_ingress_rule" "edge_proxy_from_alb" {
  security_group_id            = aws_security_group.edge_proxy.id
  description                  = "8088 from the ALB"
  referenced_security_group_id = aws_security_group.alb.id
  from_port                    = 8088
  to_port                      = 8088
  ip_protocol                  = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "edge_proxy_all" {
  security_group_id = aws_security_group.edge_proxy.id
  description       = "All egress: the proxy forwards to provider APIs, which is its entire job"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

resource "aws_security_group" "rds" {
  name        = "${var.name_prefix}-rds"
  description = "Control-plane Postgres. 5432 from the task security groups only."
  vpc_id      = aws_vpc.this.id
  tags        = merge(var.tags, { Name = "${var.name_prefix}-rds" })
}

resource "aws_vpc_security_group_ingress_rule" "rds_from_gateway" {
  security_group_id            = aws_security_group.rds.id
  description                  = "5432 from the gateway tasks"
  referenced_security_group_id = aws_security_group.gateway.id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
}

# The edge proxy does not talk to Postgres today: it reads edge keys from the gateway over HTTP.
# The rule exists because the one-shot migration task (Phase 1, not shipped here) will run in the
# gateway security group, and because a future proxy that needed the control plane would otherwise
# be debugged as a mysterious timeout. Set allow_edge_proxy_to_rds = false to drop it.
resource "aws_vpc_security_group_ingress_rule" "rds_from_edge_proxy" {
  count = var.allow_edge_proxy_to_rds ? 1 : 0

  security_group_id            = aws_security_group.rds.id
  description                  = "5432 from the edge proxy tasks"
  referenced_security_group_id = aws_security_group.edge_proxy.id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
}
