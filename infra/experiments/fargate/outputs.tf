output "connection" {
  description = "Non-secret configuration and teardown inventory; export before removing resources."
  value = {
    account                   = local.account
    region                    = local.region
    cluster_name              = aws_eks_cluster.execution.name
    cluster_arn               = aws_eks_cluster.execution.arn
    cluster_oidc_issuer       = aws_eks_cluster.execution.identity[0].oidc[0].issuer
    cluster_endpoint          = aws_eks_cluster.execution.endpoint
    cluster_ca                = aws_eks_cluster.execution.certificate_authority[0].data
    controller_role_arn       = aws_iam_role.controller.arn
    harness_role_arn          = aws_iam_role.harness.arn
    fargate_role_arn          = aws_iam_role.fargate.arn
    operator_role_arn         = var.operator_role_arn
    host_instance_id          = aws_instance.host.id
    host_private_ip           = aws_instance.host.private_ip
    host_security_group_id    = aws_security_group.host.id
    nlb_security_group_id     = aws_security_group.nlb.id
    host_public_ip            = aws_eip.host.public_ip
    host_eip_allocation_id    = aws_eip.host.id
    execution_vpc_id          = aws_vpc.execution.id
    pod_security_group_id     = aws_security_group.pods.id
    cluster_security_group_id = aws_eks_cluster.execution.vpc_config[0].cluster_security_group_id
    gateway_endpoint_id       = aws_vpc_endpoint.completion.id
    gateway_endpoint_enis     = aws_vpc_endpoint.completion.network_interface_ids
    endpoint_service_id       = aws_vpc_endpoint_service.completion.id
    s3_endpoint_id            = aws_vpc_endpoint.s3.id
    aws_endpoint_ids          = { for name, ep in aws_vpc_endpoint.aws : name => ep.id }
    canary_bucket             = aws_s3_bucket.canary.id
    ecr_repository            = aws_ecr_repository.agent.name
    firewall_association_id   = aws_route53_resolver_firewall_rule_group_association.execution.id
    firewall_rule_group_id    = aws_route53_resolver_firewall_rule_group.execution.id
    query_log_config_id       = aws_route53_resolver_query_log_config.execution.id
    dns_log_group             = aws_cloudwatch_log_group.dns.name
  }
}
