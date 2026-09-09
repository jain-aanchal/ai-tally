# SPDX-License-Identifier: Apache-2.0
#
# The minimum set docs/aws-bundle-scope.md asks for. All are metric alarms on metrics AWS already
# publishes, so none of them adds an agent or a sidecar.
#
# HONEST UNDER UNCERTAINTY, applied to alarms: every one below sets
# `treat_missing_data = "breaching"` where a missing datapoint means the thing is not reporting.
# The default, `missing`, renders a service that has stopped emitting entirely as OK, which is the
# alarm equivalent of showing a zero for a number nobody measured.
#
# alarm_actions is empty unless an SNS topic is passed. An alarm nobody is subscribed to is a
# dashboard widget, and the README says so rather than implying paging exists.

locals {
  alarm_actions = var.alarm_sns_topic_arn == "" ? [] : [var.alarm_sns_topic_arn]
}

resource "aws_cloudwatch_metric_alarm" "gateway_unhealthy_hosts" {
  alarm_name          = "${var.name_prefix}-gateway-unhealthy-hosts"
  alarm_description   = "Gateway target group has an unhealthy target. Ingest fails; the SDK buffers 10,000 spans and drops oldest after that."
  namespace           = "AWS/ApplicationELB"
  metric_name         = "UnHealthyHostCount"
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 3
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    TargetGroup  = aws_lb_target_group.gateway.arn_suffix
    LoadBalancer = aws_lb.this.arn_suffix
  }

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions
  tags          = var.tags
}

resource "aws_cloudwatch_metric_alarm" "edge_proxy_unhealthy_hosts" {
  alarm_name          = "${var.name_prefix}-edge-proxy-unhealthy-hosts"
  alarm_description   = "Edge proxy target group has an unhealthy target. This is in a customer's LLM request path, so it is the one alarm here that is somebody else's outage."
  namespace           = "AWS/ApplicationELB"
  metric_name         = "UnHealthyHostCount"
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 2
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    TargetGroup  = aws_lb_target_group.edge_proxy.arn_suffix
    LoadBalancer = aws_lb.this.arn_suffix
  }

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions
  tags          = var.tags
}

resource "aws_cloudwatch_metric_alarm" "alb_5xx" {
  alarm_name          = "${var.name_prefix}-alb-5xx"
  alarm_description   = "The load balancer itself is returning 5xx, which is a different fault from a target returning one."
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HTTPCode_ELB_5XX_Count"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = var.alb_5xx_threshold
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    LoadBalancer = aws_lb.this.arn_suffix
  }

  alarm_actions = local.alarm_actions
  tags          = var.tags
}

# Both task-count alarms exist only when Container Insights is on, because ECS/ContainerInsights
# publishes RunningTaskCount only then. Creating them regardless would leave two alarms sitting in
# INSUFFICIENT_DATA forever, which looks like coverage and is not.
resource "aws_cloudwatch_metric_alarm" "gateway_task_count" {
  count = var.container_insights ? 1 : 0

  alarm_name          = "${var.name_prefix}-gateway-running-tasks"
  alarm_description   = "Fewer gateway tasks running than desired."
  namespace           = "ECS/ContainerInsights"
  metric_name         = "RunningTaskCount"
  statistic           = "Average"
  period              = 60
  evaluation_periods  = 5
  threshold           = var.gateway_desired_count
  comparison_operator = "LessThanThreshold"

  # A service emitting nothing is a service that is not running, not a service that is fine.
  treat_missing_data = "breaching"

  dimensions = {
    ClusterName = aws_ecs_cluster.this.name
    ServiceName = aws_ecs_service.gateway.name
  }

  alarm_actions = local.alarm_actions
  tags          = var.tags
}

resource "aws_cloudwatch_metric_alarm" "edge_proxy_task_count" {
  count = var.container_insights ? 1 : 0

  alarm_name          = "${var.name_prefix}-edge-proxy-running-tasks"
  alarm_description   = "Fewer edge proxy tasks running than desired. Never scales to zero, so any shortfall is a fault."
  namespace           = "ECS/ContainerInsights"
  metric_name         = "RunningTaskCount"
  statistic           = "Average"
  period              = 60
  evaluation_periods  = 3
  threshold           = var.edge_proxy_desired_count
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"

  dimensions = {
    ClusterName = aws_ecs_cluster.this.name
    ServiceName = aws_ecs_service.edge_proxy.name
  }

  alarm_actions = local.alarm_actions
  tags          = var.tags
}

resource "aws_cloudwatch_metric_alarm" "rds_free_storage" {
  alarm_name          = "${var.name_prefix}-rds-free-storage"
  alarm_description   = "Control-plane Postgres is running out of disk. Storage autoscaling has a ceiling and this fires below it."
  namespace           = "AWS/RDS"
  metric_name         = "FreeStorageSpace"
  statistic           = "Minimum"
  period              = 300
  evaluation_periods  = 2
  threshold           = var.rds_free_storage_threshold_bytes
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"

  dimensions = {
    DBInstanceIdentifier = var.db_instance_identifier
  }

  alarm_actions = local.alarm_actions
  tags          = var.tags
}

resource "aws_cloudwatch_metric_alarm" "rds_cpu" {
  alarm_name          = "${var.name_prefix}-rds-cpu"
  alarm_description   = "Control-plane Postgres CPU is saturated."
  namespace           = "AWS/RDS"
  metric_name         = "CPUUtilization"
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 3
  threshold           = 80
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "breaching"

  dimensions = {
    DBInstanceIdentifier = var.db_instance_identifier
  }

  alarm_actions = local.alarm_actions
  tags          = var.tags
}

# ---------------------------------------------------------------------------------------------
# Gateway error rate, from the log stream
#
# There is no application metric to alarm on: nothing in infra/gateway/ emits CloudWatch metrics or
# a trace. A metric filter over the log group is the honest available substitute, and it is honest
# about being a substitute: it counts lines that look like errors, so a change to the log format
# silently zeroes it. Whoever adds real instrumentation should delete this.
# ---------------------------------------------------------------------------------------------

resource "aws_cloudwatch_log_metric_filter" "gateway_errors" {
  name           = "${var.name_prefix}-gateway-errors"
  log_group_name = aws_cloudwatch_log_group.gateway.name
  pattern        = var.gateway_error_log_pattern

  metric_transformation {
    name          = "GatewayErrorLines"
    namespace     = var.metric_namespace
    value         = "1"
    default_value = "0"
    unit          = "Count"
  }
}

resource "aws_cloudwatch_metric_alarm" "gateway_errors" {
  alarm_name          = "${var.name_prefix}-gateway-error-lines"
  alarm_description   = "Gateway error log lines above the threshold. Derived from a log pattern, not from application metrics, because the gateway emits none."
  namespace           = var.metric_namespace
  metric_name         = aws_cloudwatch_log_metric_filter.gateway_errors.metric_transformation[0].name
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = var.gateway_error_threshold
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = local.alarm_actions
  tags          = var.tags
}
