terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
  backend "s3" {
    bucket       = "beejmaxx-lab-tfstate-dev"
    key          = "dev/experiments/s1-fargate.tfstate"
    region       = "us-east-2"
    profile      = "agent-runtime"
    use_lockfile = true
  }
}
provider "aws" {
  profile             = "agent-runtime"
  region              = "us-east-2"
  allowed_account_ids = ["729608197929"]
  default_tags {
    tags = { lab = "agent-runtime", experiment = "s1" }
  }
}
variable "operator_cidr" {
  description = "Direct IPv4 egress /32, obtained without the Mac proxy."
  type        = string
  validation {
    condition     = can(cidrnetmask(var.operator_cidr)) && endswith(var.operator_cidr, "/32")
    error_message = "Only one IPv4 /32 is allowed."
  }
}
variable "operator_role_arn" {
  type    = string
  default = "arn:aws:iam::729608197929:role/managed/AccountFullAccessRole"
  validation {
    condition     = var.operator_role_arn == "arn:aws:iam::729608197929:role/managed/AccountFullAccessRole"
    error_message = "Use the verified operator role, never a host or execution role."
  }
}
locals {
  account = "729608197929"
  region  = "us-east-2"
  cluster = "lab-exec-s1"
  tags    = { lab = "agent-runtime", experiment = "s1" }
}
data "aws_caller_identity" "operator" {}
data "aws_ssm_parameter" "trusted_vpc" {
  name = "/lab/dev/network/vpc_id"
}
data "aws_ssm_parameter" "public_subnets" {
  name = "/lab/dev/network/public_subnet_ids"
}
data "aws_subnet" "trusted" {
  for_each = toset(split(",", nonsensitive(data.aws_ssm_parameter.public_subnets.value)))
  id       = each.value
}
locals {
  trusted_vpc    = nonsensitive(data.aws_ssm_parameter.trusted_vpc.value)
  trusted_subnet = one([for s in data.aws_subnet.trusted : s.id if s.availability_zone == "us-east-2a"])
}
