# SPDX-License-Identifier: Apache-2.0
#
# Compute (CTO-335): ECS cluster, log groups with a retention, ALB and TLS, target groups, the two
# services, and the alarms.
#
# The web tier is deliberately absent. docs/aws-bundle-scope.md settles the dashboard on Vercel and
# keeps web.taskdef.json in the tree as an escape hatch the bundle does not use. Two equally weighted
# options is what that doc says not to ship, so this module has one.

data "aws_caller_identity" "current" {}

locals {
  gateway_url = "https://${var.ingest_hostname}"
}

resource "aws_ecs_cluster" "this" {
  name = var.cluster_name

  setting {
    name  = "containerInsights"
    value = var.container_insights ? "enabled" : "disabled"
  }

  tags = var.tags
}

# ---------------------------------------------------------------------------------------------
# Log groups
#
# Created here rather than by the awslogs driver, which has no retention option: a group it creates
# keeps every line forever at full price and nobody notices until the CloudWatch line on the bill
# does not look like a rounding error. Both task definitions set awslogs-create-group: false to make
# that explicit.
# ---------------------------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "gateway" {
  name              = "/ecs/${var.name_prefix}-gateway"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.log_kms_key_arn
  tags              = var.tags
}

resource "aws_cloudwatch_log_group" "edge_proxy" {
  name              = "/ecs/${var.name_prefix}-edge-proxy"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.log_kms_key_arn
  tags              = var.tags
}

# ---------------------------------------------------------------------------------------------
# TLS
# ---------------------------------------------------------------------------------------------

# Exactly two paths, and both of them terminate. Either the zone is in this account and Terraform
# requests the certificate and writes the validation records itself, or the certificate already
# exists and validated somewhere else and you pass its ARN. The scope doc lists DNS outside the
# account as a normal case; that case is the second path. See the precondition below for the third
# arrangement this module used to offer and why it could not work.
resource "aws_acm_certificate" "this" {
  count = var.acm_certificate_arn == "" ? 1 : 0

  domain_name               = var.ingest_hostname
  subject_alternative_names = [var.llm_hostname]
  validation_method         = "DNS"

  tags = var.tags

  lifecycle {
    create_before_destroy = true

    # THE THIRD PATH DOES NOT WORK, so it fails at plan instead of at the listener (CTO-361).
    #
    # "Terraform requests the certificate AND the DNS zone is somewhere else" used to be offered:
    # apply, read `certificate_validation_records`, create the CNAMEs by hand. It cannot work in
    # that order. A certificate stays PENDING_VALIDATION until the records exist, ELB refuses to
    # attach one that is not ISSUED, so `aws_lb_listener.https` fails the apply, and a failed apply
    # writes no outputs, so the records the operator was told to read are not there to read. What
    # they get is a VPC, a NAT gateway, an RDS instance, an ALB with no listener and no next step.
    #
    # Two paths remain, and both work: hand Terraform a Route 53 zone in this account, or request
    # and validate the certificate out of band and hand over the ARN.
    precondition {
      condition     = var.route53_zone_id != ""
      error_message = "acm_certificate_arn is empty, so Terraform would request a certificate, but route53_zone_id is empty too, so nothing would ever validate it and the HTTPS listener would fail the apply. Either set route53_zone_id to a hosted zone in this account, or request the certificate yourself and pass acm_certificate_arn:\n\n  aws acm request-certificate --domain-name <ingest-hostname> --subject-alternative-names <llm-hostname> --validation-method DNS --region <region>\n  aws acm describe-certificate --certificate-arn <arn> --region <region> --query 'Certificate.DomainValidationOptions'\n\nCreate those CNAMEs in whatever zone holds the domain, wait for Status ISSUED, then set acm_certificate_arn."
    }
  }
}

resource "aws_route53_record" "certificate_validation" {
  for_each = var.acm_certificate_arn == "" ? {
    for option in aws_acm_certificate.this[0].domain_validation_options :
    option.domain_name => {
      name   = option.resource_record_name
      record = option.resource_record_value
      type   = option.resource_record_type
    }
  } : {}

  zone_id         = var.route53_zone_id
  name            = each.value.name
  type            = each.value.type
  records         = [each.value.record]
  ttl             = 60
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "this" {
  count = var.acm_certificate_arn == "" ? 1 : 0

  certificate_arn         = aws_acm_certificate.this[0].arn
  validation_record_fqdns = [for record in aws_route53_record.certificate_validation : record.fqdn]
}

locals {
  certificate_arn = var.acm_certificate_arn != "" ? var.acm_certificate_arn : aws_acm_certificate.this[0].arn
}

# ---------------------------------------------------------------------------------------------
# ALB
# ---------------------------------------------------------------------------------------------

resource "aws_lb" "this" {
  name               = var.name_prefix
  load_balancer_type = "application"
  internal           = var.internal_alb
  subnets            = var.public_subnet_ids
  security_groups    = [var.alb_security_group_id]

  drop_invalid_header_fields = true
  enable_deletion_protection = var.alb_deletion_protection
  idle_timeout               = var.alb_idle_timeout

  # Off unless a bucket is named. Access logs are the only record of who called the proxy hostname,
  # which is the endpoint that forwards to provider APIs with real keys, so this is worth turning on
  # for that reason rather than for the completeness of a checklist. The bucket needs its own policy
  # allowing the ELB log-delivery principal, which is why the module does not create it here.
  dynamic "access_logs" {
    for_each = var.alb_access_logs_bucket == "" ? [] : [1]
    content {
      bucket  = var.alb_access_logs_bucket
      prefix  = var.name_prefix
      enabled = true
    }
  }

  tags = var.tags
}

resource "aws_lb_target_group" "gateway" {
  name        = "${var.name_prefix}-gateway"
  port        = 8080
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "ip"

  health_check {
    protocol            = "HTTP"
    path                = "/healthz"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
    matcher             = "200"
  }

  # The SDK's BatchingTransport retries with capped backoff, so draining slowly costs nothing here.
  deregistration_delay = 30

  tags = var.tags
}

resource "aws_lb_target_group" "edge_proxy" {
  name        = "${var.name_prefix}-edge-proxy"
  port        = 8088
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "ip"

  # Shallow and fast on purpose. This target group is the proxy's only liveness check (the image is
  # FROM scratch and carries no container health check), and the path it fronts is somebody else's
  # production LLM traffic, so a bad task has to drain quickly.
  health_check {
    protocol            = "HTTP"
    path                = "/healthz"
    interval            = 10
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 2
    matcher             = "200"
  }

  # Long enough not to cut an in-flight completion, which can legitimately run for minutes:
  # EDGE_PROXY_UPSTREAM_TIMEOUT is 10m in the task definition.
  deregistration_delay = var.edge_proxy_deregistration_delay

  tags = var.tags
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.this.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = var.alb_ssl_policy
  certificate_arn   = local.certificate_arn

  # Default deny. Anything that does not match a host rule below is not a route this deployment
  # serves, and answering 404 is better than defaulting onto whichever target group came first.
  default_action {
    type = "fixed-response"
    fixed_response {
      content_type = "text/plain"
      message_body = "not found"
      status_code  = "404"
    }
  }

  depends_on = [aws_acm_certificate_validation.this]
}

resource "aws_lb_listener" "http_redirect" {
  count = var.enable_http_redirect ? 1 : 0

  load_balancer_arn = aws_lb.this.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

resource "aws_lb_listener_rule" "gateway" {
  listener_arn = aws_lb_listener.https.arn
  priority     = 100

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.gateway.arn
  }

  condition {
    host_header {
      values = [var.ingest_hostname]
    }
  }
}

# Its own rule, never shared with ingest: this hostname forwards to provider APIs with real keys, so
# it needs its own protection (a WAF rule, an allowlist, or at minimum a non-guessable hostname) and
# sharing a rule would make that impossible to apply to one and not the other.
resource "aws_lb_listener_rule" "edge_proxy" {
  listener_arn = aws_lb_listener.https.arn
  priority     = 200

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.edge_proxy.arn
  }

  condition {
    host_header {
      values = [var.llm_hostname]
    }
  }
}

resource "aws_route53_record" "ingest" {
  count = var.route53_zone_id != "" ? 1 : 0

  zone_id = var.route53_zone_id
  name    = var.ingest_hostname
  type    = "A"

  alias {
    name                   = aws_lb.this.dns_name
    zone_id                = aws_lb.this.zone_id
    evaluate_target_health = true
  }
}

resource "aws_route53_record" "llm" {
  count = var.route53_zone_id != "" ? 1 : 0

  zone_id = var.route53_zone_id
  name    = var.llm_hostname
  type    = "A"

  alias {
    name                   = aws_lb.this.dns_name
    zone_id                = aws_lb.this.zone_id
    evaluate_target_health = true
  }
}

# ---------------------------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------------------------

resource "aws_ecs_service" "gateway" {
  name            = "${var.name_prefix}-gateway"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.gateway.arn
  desired_count   = var.gateway_desired_count
  launch_type     = "FARGATE"

  # Matches gateway.service.json.
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  health_check_grace_period_seconds  = 30
  enable_execute_command             = true

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [var.gateway_security_group_id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.gateway.arn
    container_name   = "gateway"
    container_port   = 8080
  }

  tags = var.tags

  lifecycle {
    # A private subnet with no default route reaches neither ClickHouse Cloud on 8443 nor any
    # provider API, and the task fails at runtime with a connection timeout rather than at apply
    # with a reason. Catch it here.
    precondition {
      condition     = var.has_internet_egress
      error_message = "The private subnets have no default route. The gateway needs egress to ClickHouse Cloud (8443) and to provider APIs; set enable_nat_gateway = true."
    }
  }

  depends_on = [aws_lb_listener_rule.gateway]
}

resource "aws_ecs_service" "edge_proxy" {
  name            = "${var.name_prefix}-edge-proxy"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.edge_proxy.arn
  desired_count   = var.edge_proxy_desired_count
  launch_type     = "FARGATE"

  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  health_check_grace_period_seconds  = 30
  enable_execute_command             = false

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [var.edge_proxy_security_group_id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.edge_proxy.arn
    container_name   = "edge-proxy"
    container_port   = 8088
  }

  tags = var.tags

  lifecycle {
    # No autoscaling policy is attached to this service anywhere in the module, deliberately. The
    # proxy sits in the customer's LLM request path, so its outage is an outage of somebody else's
    # product, and an idle-based scale-in is an outage waiting for a quiet hour.
    precondition {
      condition     = var.edge_proxy_desired_count >= 2
      error_message = "The edge proxy runs at least two tasks across two AZs and never scales to zero: it is the only component whose outage is an outage of someone else's product."
    }

    precondition {
      condition     = var.has_internet_egress
      error_message = "The private subnets have no default route. The edge proxy exists to forward to provider APIs; set enable_nat_gateway = true."
    }
  }

  depends_on = [aws_lb_listener_rule.edge_proxy]
}
