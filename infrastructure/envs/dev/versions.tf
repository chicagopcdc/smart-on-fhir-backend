terraform {
  required_version = ">= 1.9.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # Partial configuration: the bucket, key and region are in backend.hcl, which
  # is not committed because it names an account's bucket. Initialise with
  #
  #   terraform init -backend-config=backend.hcl
  #
  # The state holds the database password and the token encryption key in
  # cleartext, so the bucket behind this has to be encrypted, versioned and
  # readable by the people who deploy and nobody else. See backend.hcl.example.
  backend "s3" {}
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = var.project
      Environment = var.environment
      ManagedBy   = "terraform"
      Repository  = "chicagopcdc/smart-on-fhir-backend"
    }
  }
}
