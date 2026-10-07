resource "aws_route53_resolver_firewall_domain_list" "allow" {
  name = "lab-s1-required-aws-names"
  domains = [for domain in [
    "api.ecr.us-east-2.amazonaws.com",
    "ecr.us-east-2.amazonaws.com",
    "729608197929.dkr.ecr.us-east-2.amazonaws.com",
    "sts.us-east-2.amazonaws.com",
    "s3.us-east-2.amazonaws.com",
    "s3-us-east-2.amazonaws.com",
    "s3-r-w.us-east-2.amazonaws.com",
    "prod-us-east-2-starport-layer-bucket.s3.us-east-2.amazonaws.com",
    "prod-us-east-2-starport-layer-bucket.s3.amazonaws.com",
    trimsuffix(trimprefix(aws_eks_cluster.execution.endpoint, "https://"), "/"),
  ] : "${lower(trimsuffix(domain, "."))}."]
}
resource "aws_route53_resolver_firewall_domain_list" "block" {
  name    = "lab-s1-block-everything-else"
  domains = ["*."]
}
resource "aws_route53_resolver_firewall_rule_group" "execution" {
  name = "lab-s1-execution"
}
resource "aws_route53_resolver_firewall_rule" "allow" {
  name                               = "required-aws-names"
  action                             = "ALLOW"
  firewall_domain_list_id            = aws_route53_resolver_firewall_domain_list.allow.id
  firewall_rule_group_id             = aws_route53_resolver_firewall_rule_group.execution.id
  priority                           = 100
  firewall_domain_redirection_action = "INSPECT_REDIRECTION_DOMAIN"
}
resource "aws_route53_resolver_firewall_rule" "block" {
  name                               = "block-everything-else"
  action                             = "BLOCK"
  block_response                     = "NXDOMAIN"
  firewall_domain_list_id            = aws_route53_resolver_firewall_domain_list.block.id
  firewall_rule_group_id             = aws_route53_resolver_firewall_rule_group.execution.id
  priority                           = 200
  firewall_domain_redirection_action = "INSPECT_REDIRECTION_DOMAIN"
}
resource "aws_route53_resolver_firewall_config" "execution" {
  resource_id        = aws_vpc.execution.id
  firewall_fail_open = "DISABLED"
}
resource "aws_route53_resolver_firewall_rule_group_association" "execution" {
  name                   = "lab-s1-execution"
  firewall_rule_group_id = aws_route53_resolver_firewall_rule_group.execution.id
  vpc_id                 = aws_vpc.execution.id
  priority               = 101
  mutation_protection    = "DISABLED"
  depends_on             = [aws_route53_resolver_firewall_rule.allow, aws_route53_resolver_firewall_rule.block]
}
# CloudWatch's default encryption at rest; no extra KMS resource beyond the S1 contract.
resource "aws_cloudwatch_log_group" "dns" {
  name              = "/lab/s1/dns"
  retention_in_days = 1
}
resource "aws_cloudwatch_log_resource_policy" "dns" {
  policy_name = "lab-s1-dns"
  policy_document = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Principal = { Service = "route53resolver.amazonaws.com" },
    Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.dns.arn}:*",
    Condition = {
      StringEquals = { "aws:SourceAccount" = local.account }
      ArnLike      = { "aws:SourceArn" = "arn:aws:route53resolver:us-east-2:729608197929:resolver-query-log-config/*" }
    }
  }] })
}
resource "aws_route53_resolver_query_log_config" "execution" {
  name            = "lab-s1-execution"
  destination_arn = aws_cloudwatch_log_group.dns.arn
  depends_on      = [aws_cloudwatch_log_resource_policy.dns]
}
resource "aws_route53_resolver_query_log_config_association" "execution" {
  resolver_query_log_config_id = aws_route53_resolver_query_log_config.execution.id
  resource_id                  = aws_vpc.execution.id
}
