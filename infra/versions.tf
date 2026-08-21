# Written in plain HCL, so it runs under either OpenTofu (`tofu`) or
# Terraform (`terraform`) unchanged. OpenTofu is used locally because HashiCorp
# moved Terraform out of Homebrew core when it changed to the BUSL licence.

terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }
}

provider "aws" {
  region = var.region

  # Never the default profile. This project is personal and must not be able to
  # touch a work account that happens to be configured on the same machine.
  profile = var.aws_profile

  default_tags {
    tags = {
      Project   = "pubg-analytics"
      ManagedBy = "opentofu"
    }
  }
}

data "aws_caller_identity" "current" {}
