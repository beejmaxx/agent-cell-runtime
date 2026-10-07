resource "aws_vpc" "execution" {
  cidr_block           = "10.30.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = "lab-exec" }
}
resource "aws_default_security_group" "execution" {
  vpc_id = aws_vpc.execution.id
}
resource "aws_vpc_dhcp_options" "execution" {
  domain_name         = "us-east-2.compute.internal"
  domain_name_servers = ["AmazonProvidedDNS"]
  tags                = { Name = "lab-exec-s1" }
}
resource "aws_vpc_dhcp_options_association" "execution" {
  vpc_id          = aws_vpc.execution.id
  dhcp_options_id = aws_vpc_dhcp_options.execution.id
}
resource "aws_subnet" "execution" {
  for_each          = { "us-east-2a" = "10.30.0.0/24", "us-east-2b" = "10.30.1.0/24" }
  vpc_id            = aws_vpc.execution.id
  availability_zone = each.key
  cidr_block        = each.value
  tags              = { Name = "lab-exec-${each.key}" }
}
resource "aws_route_table" "execution" {
  vpc_id = aws_vpc.execution.id
  tags   = { Name = "lab-exec-private-only" }
}
resource "aws_route_table_association" "execution" {
  for_each       = aws_subnet.execution
  subnet_id      = each.value.id
  route_table_id = aws_route_table.execution.id
}
resource "aws_security_group" "pods" {
  name        = "lab-s1-execution-pods"
  description = "Execution egress only to the callback and required AWS/control-plane paths"
  vpc_id      = aws_vpc.execution.id
}
resource "aws_security_group" "endpoints" {
  name   = "lab-s1-aws-endpoints"
  vpc_id = aws_vpc.execution.id
}
resource "aws_security_group" "gateway_endpoint" {
  name   = "lab-s1-gateway-endpoint"
  vpc_id = aws_vpc.execution.id
}
resource "aws_vpc_security_group_egress_rule" "pod_api" {
  security_group_id            = aws_security_group.pods.id
  referenced_security_group_id = aws_eks_cluster.execution.vpc_config[0].cluster_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}
resource "aws_vpc_security_group_ingress_rule" "api_pods" {
  security_group_id            = aws_eks_cluster.execution.vpc_config[0].cluster_security_group_id
  referenced_security_group_id = aws_security_group.pods.id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}
resource "aws_vpc_security_group_ingress_rule" "kubelet" {
  security_group_id            = aws_security_group.pods.id
  referenced_security_group_id = aws_eks_cluster.execution.vpc_config[0].cluster_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 10250
  to_port                      = 10250
}
resource "aws_vpc_security_group_egress_rule" "pod_endpoints" {
  security_group_id            = aws_security_group.pods.id
  referenced_security_group_id = aws_security_group.endpoints.id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}
resource "aws_vpc_security_group_ingress_rule" "endpoints_pods" {
  security_group_id            = aws_security_group.endpoints.id
  referenced_security_group_id = aws_security_group.pods.id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}
# E5's deliberate removal of SecurityGroupPolicy must preserve image bootstrap.
resource "aws_vpc_security_group_ingress_rule" "endpoints_control" {
  security_group_id            = aws_security_group.endpoints.id
  referenced_security_group_id = aws_eks_cluster.execution.vpc_config[0].cluster_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}
resource "aws_vpc_security_group_egress_rule" "pod_gateway" {
  security_group_id            = aws_security_group.pods.id
  referenced_security_group_id = aws_security_group.gateway_endpoint.id
  ip_protocol                  = "tcp"
  from_port                    = 8001
  to_port                      = 8001
}
resource "aws_vpc_security_group_ingress_rule" "gateway_pods" {
  security_group_id            = aws_security_group.gateway_endpoint.id
  referenced_security_group_id = aws_security_group.pods.id
  ip_protocol                  = "tcp"
  from_port                    = 8001
  to_port                      = 8001
}
resource "aws_vpc_security_group_egress_rule" "pod_s3" {
  security_group_id = aws_security_group.pods.id
  prefix_list_id    = aws_vpc_endpoint.s3.prefix_list_id
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}
