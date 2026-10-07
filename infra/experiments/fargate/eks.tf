resource "aws_iam_role" "cluster" {
  name = "lab-s1-eks-service"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Principal = { Service = "eks.amazonaws.com" }, Action = "sts:AssumeRole"
  }] })
}
resource "aws_iam_role_policy_attachment" "cluster" {
  role       = aws_iam_role.cluster.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
}
resource "aws_eks_cluster" "execution" {
  name                          = local.cluster
  role_arn                      = aws_iam_role.cluster.arn
  version                       = "1.36"
  bootstrap_self_managed_addons = false
  access_config {
    authentication_mode                         = "API"
    bootstrap_cluster_creator_admin_permissions = false
  }
  vpc_config {
    subnet_ids              = [for s in aws_subnet.execution : s.id]
    endpoint_private_access = true
    endpoint_public_access  = true
    public_access_cidrs     = [var.operator_cidr, "${aws_eip.host.public_ip}/32"]
  }
  depends_on = [aws_iam_role_policy_attachment.cluster, aws_iam_service_linked_role.eks]
}
resource "aws_iam_role" "fargate" {
  name = "lab-s1-fargate-execution"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Principal = { Service = "eks-fargate-pods.amazonaws.com" }, Action = "sts:AssumeRole",
    Condition = {
      StringEquals = { "aws:SourceAccount" = local.account }
      ArnLike      = { "aws:SourceArn" = "arn:aws:eks:us-east-2:729608197929:fargateprofile/lab-exec-s1/agent-exec/*" }
    }
  }] })
}
resource "aws_iam_role_policy" "fargate_pull" {
  name = "s1-one-repository-pull"
  role = aws_iam_role.fargate.name
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = "ecr:GetAuthorizationToken", Resource = "*" },
    { Effect = "Allow", Action = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"], Resource = aws_ecr_repository.agent.arn }
  ] })
}
resource "aws_eks_fargate_profile" "execution" {
  cluster_name           = aws_eks_cluster.execution.name
  fargate_profile_name   = "agent-exec"
  pod_execution_role_arn = aws_iam_role.fargate.arn
  subnet_ids             = [aws_subnet.execution["us-east-2a"].id]
  selector {
    namespace = "agent-exec"
  }
  depends_on = [aws_iam_role_policy.fargate_pull, aws_vpc_endpoint.aws, aws_vpc_endpoint.s3, aws_iam_service_linked_role.fargate]
}
resource "aws_eks_access_entry" "controller" {
  cluster_name      = aws_eks_cluster.execution.name
  principal_arn     = aws_iam_role.controller.arn
  kubernetes_groups = ["agent-runtime-controllers"]
  type              = "STANDARD"
}
resource "aws_eks_access_entry" "operator" {
  cluster_name  = aws_eks_cluster.execution.name
  principal_arn = var.operator_role_arn
  type          = "STANDARD"
}
resource "aws_eks_access_policy_association" "operator" {
  cluster_name  = aws_eks_cluster.execution.name
  principal_arn = aws_eks_access_entry.operator.principal_arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
  access_scope {
    type = "cluster"
  }
}
resource "aws_ecr_repository" "agent" {
  name                 = "agent-runtime/fake-agent"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = true
  encryption_configuration {
    encryption_type = "AES256"
  }
}

# These roles do not exist in the verified baseline. Own their full lifecycle,
# rather than leaving untracked service-created IAM roles after cluster deletion.
resource "aws_iam_service_linked_role" "eks" {
  aws_service_name = "eks.amazonaws.com"
}
resource "aws_iam_service_linked_role" "fargate" {
  aws_service_name = "eks-fargate.amazonaws.com"
}
