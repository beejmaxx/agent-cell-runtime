variable "sts_get_caller_identity_only" {
  description = "Reviewer-selected STS endpoint baseline; intentionally has no default."
  type        = bool
}
locals {
  ecr_endpoint_policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Principal = { AWS = aws_iam_role.fargate.arn }, Action = "ecr:GetAuthorizationToken", Resource = "*" },
    { Effect = "Allow", Principal = { AWS = aws_iam_role.fargate.arn }, Action = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"], Resource = aws_ecr_repository.agent.arn }
  ] })
  sts_endpoint_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Principal = "*", Action = var.sts_get_caller_identity_only ? "sts:GetCallerIdentity" : "*", Resource = "*"
  }] })
}
resource "aws_vpc_endpoint" "aws" {
  for_each            = toset(["ecr.api", "ecr.dkr", "sts"])
  vpc_id              = aws_vpc.execution.id
  service_name        = "com.amazonaws.us-east-2.${each.key}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [aws_subnet.execution["us-east-2a"].id]
  security_group_ids  = [aws_security_group.endpoints.id]
  private_dns_enabled = true
  policy              = each.key == "sts" ? local.sts_endpoint_policy : local.ecr_endpoint_policy
  tags                = { Name = "lab-s1-${each.key}" }
}
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.execution.id
  service_name      = "com.amazonaws.us-east-2.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.execution.id]
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect   = "Allow", Principal = "*", Action = "s3:GetObject",
    Resource = "arn:aws:s3:::prod-us-east-2-starport-layer-bucket/*"
  }] })
  tags = { Name = "lab-s1-ecr-layers" }
}
resource "aws_s3_bucket" "canary" {
  bucket        = "lab-s1-canary-729608197929-us-east-2"
  force_destroy = true
}
resource "aws_s3_bucket_public_access_block" "canary" {
  bucket                  = aws_s3_bucket.canary.id
  block_public_acls       = true
  ignore_public_acls      = true
  block_public_policy     = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_server_side_encryption_configuration" "canary" {
  bucket = aws_s3_bucket.canary.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}
resource "aws_s3_bucket_policy" "canary" {
  bucket = aws_s3_bucket.canary.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Sid       = "AnonymousOnlyThroughS1Endpoint", Effect = "Allow", Principal = "*", Action = "s3:PutObject",
    Resource  = "${aws_s3_bucket.canary.arn}/*",
    Condition = { StringEquals = { "aws:SourceVpce" = aws_vpc_endpoint.s3.id } }
  }] })
  depends_on = [aws_s3_bucket_public_access_block.canary]
}
