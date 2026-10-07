# Independent guardrails: never read or write bootstrap/foundation state.
terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.7"
    }
  }
  backend "s3" {
    bucket              = "beejmaxx-lab-tfstate-dev"
    key                 = "dev/cost-guard.tfstate"
    region              = "us-east-2"
    allowed_account_ids = ["729608197929"]
    use_lockfile        = true
  }
}

provider "aws" {
  region              = "us-east-2"
  allowed_account_ids = ["729608197929"]
  default_tags {
    tags = local.tags
  }
}

# Billing APIs are global, with their endpoint in us-east-1.
provider "aws" {
  alias               = "billing"
  region              = "us-east-1"
  allowed_account_ids = ["729608197929"]
  default_tags {
    tags = local.tags
  }
}

locals {
  name = "lab-dev-cost-guard"
  tags = {
    Project     = "lab-platform"
    Environment = "dev"
    Stack       = "cost-guard"
    ManagedBy   = "terraform"
    lab         = "agent-runtime"
    experiment  = "cost-guard"
  }
}

variable "alert_emails" {
  description = "Deduplicated EMAIL subscribers from the existing budgets; supply via ignored tfvars."
  type        = list(string)
  sensitive   = true
  validation {
    condition = (
      length(var.alert_emails) > 0 && length(var.alert_emails) <= 10 &&
      length(distinct(var.alert_emails)) == length(var.alert_emails) &&
      alltrue([for email in var.alert_emails : can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", email))])
    )
    error_message = "Provide 1–10 unique email addresses."
  }
}

variable "max_age_hours" {
  description = "Alert on creation/launch age greater than this; unknown age is reported separately."
  type        = number
  default     = 4
  validation {
    condition     = var.max_age_hours > 0 && var.max_age_hours <= 2160
    error_message = "Age must be greater than zero and no more than 90 days (CloudTrail history)."
  }
}

variable "anomaly_threshold_usd" {
  description = "Minimum total anomaly dollar impact for the daily email summary."
  type        = number
  default     = 1
  validation {
    condition     = var.anomaly_threshold_usd >= 0
    error_message = "Anomaly impact threshold cannot be negative."
  }
}

resource "aws_ce_anomaly_monitor" "services" {
  provider          = aws.billing
  name              = "${local.name}-services"
  monitor_type      = "DIMENSIONAL"
  monitor_dimension = "SERVICE"
}

resource "aws_ce_anomaly_subscription" "email" {
  provider         = aws.billing
  name             = "${local.name}-daily"
  frequency        = "DAILY"
  monitor_arn_list = [aws_ce_anomaly_monitor.services.arn]
  threshold_expression {
    dimension {
      key           = "ANOMALY_TOTAL_IMPACT_ABSOLUTE"
      match_options = ["GREATER_THAN_OR_EQUAL"]
      values        = [tostring(var.anomaly_threshold_usd)]
    }
  }
  dynamic "subscriber" {
    for_each = var.alert_emails
    content {
      type    = "EMAIL"
      address = subscriber.value
    }
  }
}

resource "aws_sns_topic" "alerts" {
  name = local.name
}

resource "aws_sns_topic_subscription" "email" {
  count     = nonsensitive(length(var.alert_emails))
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_emails[count.index]
}

resource "aws_cloudwatch_log_group" "watchdog" {
  name              = "/aws/lambda/${local.name}"
  retention_in_days = 7
}

resource "aws_iam_role" "watchdog" {
  name = "${local.name}-lambda"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "watchdog" {
  name = local.name
  role = aws_iam_role.watchdog.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "RegionalInventory"
        Effect = "Allow"
        Action = [
          "eks:ListClusters",
          "ec2:DescribeInstances", "ec2:DescribeNatGateways", "ec2:DescribeVpcEndpoints",
          "ec2:DescribeAddresses",
          "elasticloadbalancing:DescribeLoadBalancers", "elasticloadbalancing:DescribeTags",
          "rds:DescribeDBInstances", "rds:DescribeDBClusters",
          "ecs:ListClusters", "ecs:ListServices",
          "cloudtrail:LookupEvents"
        ]
        Resource  = "*"
        Condition = { StringEquals = { "aws:RequestedRegion" = "us-east-2" } }
      },
      {
        Sid      = "DescribeEksClusters"
        Effect   = "Allow"
        Action   = ["eks:DescribeCluster"]
        Resource = "arn:aws:eks:us-east-2:729608197929:cluster/*"
      },
      {
        Sid      = "DescribeEcsServicesAndTags"
        Effect   = "Allow"
        Action   = ["ecs:DescribeServices", "ecs:ListTagsForResource"]
        Resource = "arn:aws:ecs:us-east-2:729608197929:service/*"
      },
      {
        Sid      = "PublishOnlyGuardAlerts"
        Effect   = "Allow"
        Action   = ["sns:Publish"]
        Resource = aws_sns_topic.alerts.arn
      },
      {
        Sid      = "WriteOnlyGuardLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.watchdog.arn}:*"
      }
    ]
  })
}

data "archive_file" "watchdog" {
  type        = "zip"
  source_file = "${path.module}/lambda/watchdog.py"
  output_path = "${path.module}/.terraform/watchdog.zip"
}

resource "aws_lambda_function" "watchdog" {
  function_name    = local.name
  role             = aws_iam_role.watchdog.arn
  runtime          = "python3.13"
  handler          = "watchdog.handler"
  filename         = data.archive_file.watchdog.output_path
  source_code_hash = data.archive_file.watchdog.output_base64sha256
  memory_size      = 256
  timeout          = 180
  environment {
    variables = {
      TOPIC_ARN     = aws_sns_topic.alerts.arn
      MAX_AGE_HOURS = tostring(var.max_age_hours)
    }
  }
  depends_on = [aws_iam_role_policy.watchdog]
}

# Includes timeouts/unhandled errors; normal partial scans report through the same topic.
resource "aws_lambda_function_event_invoke_config" "watchdog" {
  function_name                = aws_lambda_function.watchdog.function_name
  maximum_event_age_in_seconds = 3600
  maximum_retry_attempts       = 0
  destination_config {
    on_failure {
      destination = aws_sns_topic.alerts.arn
    }
  }
}

resource "aws_scheduler_schedule_group" "watchdog" {
  name = local.name
}

resource "aws_iam_role" "scheduler" {
  name = "${local.name}-scheduler"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = "729608197929" }
        ArnEquals    = { "aws:SourceArn" = aws_scheduler_schedule_group.watchdog.arn }
      }
    }]
  })
}

resource "aws_iam_role_policy" "scheduler" {
  name = local.name
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["lambda:InvokeFunction"]
      Resource = aws_lambda_function.watchdog.arn
    }]
  })
}

resource "aws_scheduler_schedule" "hourly" {
  name                = local.name
  group_name          = aws_scheduler_schedule_group.watchdog.name
  schedule_expression = "rate(1 hour)"
  flexible_time_window {
    mode = "OFF"
  }
  target {
    arn      = aws_lambda_function.watchdog.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = "{}"
    retry_policy {
      maximum_event_age_in_seconds = 3600
      maximum_retry_attempts       = 0
    }
  }
  depends_on = [aws_iam_role_policy.scheduler, aws_lambda_function_event_invoke_config.watchdog]
}

output "watchdog_function_name" {
  value = aws_lambda_function.watchdog.function_name
}

output "alerts_topic_arn" {
  value = aws_sns_topic.alerts.arn
}

output "anomaly_monitor_arn" {
  value = aws_ce_anomaly_monitor.services.arn
}
