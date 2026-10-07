resource "aws_iam_role" "controller" {
  name = "lab-s1-controller"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole"
  }] })
}
resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.controller.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}
resource "aws_iam_role_policy" "describe_cluster" {
  name = "s1-describe-cluster"
  role = aws_iam_role.controller.name
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = "eks:DescribeCluster", Resource = "arn:aws:eks:us-east-2:729608197929:cluster/lab-exec-s1"
  }] })
}
resource "aws_iam_instance_profile" "host" {
  name = "lab-s1-controller"
  role = aws_iam_role.controller.name
}
resource "aws_security_group" "host" {
  name   = "lab-s1-trusted-host"
  vpc_id = local.trusted_vpc
}
resource "aws_security_group" "nlb" {
  name   = "lab-s1-completion-nlb"
  vpc_id = local.trusted_vpc
}
resource "aws_vpc_security_group_ingress_rule" "host_completion" {
  security_group_id            = aws_security_group.host.id
  referenced_security_group_id = aws_security_group.nlb.id
  ip_protocol                  = "tcp"
  from_port                    = 8001
  to_port                      = 8001
}
resource "aws_vpc_security_group_egress_rule" "host_https" {
  security_group_id = aws_security_group.host.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}
resource "aws_vpc_security_group_egress_rule" "host_packages" {
  security_group_id = aws_security_group.host.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
}
resource "aws_vpc_security_group_egress_rule" "nlb_host" {
  security_group_id            = aws_security_group.nlb.id
  referenced_security_group_id = aws_security_group.host.id
  ip_protocol                  = "tcp"
  from_port                    = 8001
  to_port                      = 8001
}
data "aws_ssm_parameter" "ubuntu" {
  name = "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
}
resource "aws_instance" "host" {
  ami                         = nonsensitive(data.aws_ssm_parameter.ubuntu.value)
  instance_type               = "t3.small"
  subnet_id                   = local.trusted_subnet
  associate_public_ip_address = false
  vpc_security_group_ids      = [aws_security_group.host.id]
  iam_instance_profile        = aws_iam_instance_profile.host.name
  user_data                   = file("${path.module}/host-init.sh")
  user_data_replace_on_change = true
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }
  root_block_device {
    volume_size           = 16
    volume_type           = "gp3"
    encrypted             = true
    delete_on_termination = true
    tags                  = local.tags
  }
  tags = { Name = "lab-s1-trusted-host" }
}
resource "aws_eip" "host" {
  domain = "vpc"
  tags   = { Name = "lab-s1-trusted-host" }
}
resource "aws_eip_association" "host" {
  instance_id   = aws_instance.host.id
  allocation_id = aws_eip.host.id
}
resource "aws_lb" "completion" {
  name               = "lab-s1-completion"
  internal           = true
  load_balancer_type = "network"
  subnets            = [local.trusted_subnet]
  security_groups    = [aws_security_group.nlb.id]
  # Only accepted PrivateLink traffic bypasses ingress evaluation. No direct ingress rule.
  enforce_security_group_inbound_rules_on_private_link_traffic = "off"
}
resource "aws_lb_target_group" "completion" {
  name        = "lab-s1-completion"
  port        = 8001
  protocol    = "TCP"
  target_type = "instance"
  vpc_id      = local.trusted_vpc
  health_check {
    protocol = "TCP"
    port     = "traffic-port"
  }
}
resource "aws_lb_target_group_attachment" "host" {
  target_group_arn = aws_lb_target_group.completion.arn
  target_id        = aws_instance.host.id
  port             = 8001
}
resource "aws_lb_listener" "completion" {
  load_balancer_arn = aws_lb.completion.arn
  port              = 8001
  protocol          = "TCP"
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.completion.arn
  }
}
resource "aws_vpc_endpoint_service" "completion" {
  acceptance_required        = true
  network_load_balancer_arns = [aws_lb.completion.arn]
  allowed_principals         = [var.operator_role_arn]
}
resource "aws_vpc_endpoint" "completion" {
  vpc_id              = aws_vpc.execution.id
  service_name        = aws_vpc_endpoint_service.completion.service_name
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [aws_subnet.execution["us-east-2a"].id]
  security_group_ids  = [aws_security_group.gateway_endpoint.id]
  private_dns_enabled = false
}
resource "aws_vpc_endpoint_connection_accepter" "completion" {
  vpc_endpoint_service_id = aws_vpc_endpoint_service.completion.id
  vpc_endpoint_id         = aws_vpc_endpoint.completion.id
}
