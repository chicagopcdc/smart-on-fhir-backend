# tflint configuration for this stack.
#
# `terraform validate` checks that the configuration is internally consistent —
# types line up, attribute names exist in the provider schema. It does not know
# that 256 CPU units cannot be paired with 4096 MiB of memory, or that an
# instance class exists. The AWS ruleset does, which is why this is worth having
# for infrastructure whose first real plan has not run yet.

config {
  # Modules are linted directly by --recursive, which gives better paths than
  # inlining them into each root's report.
  call_module_type = "none"
}

plugin "terraform" {
  enabled = true
  preset  = "recommended"
}

plugin "aws" {
  enabled = true
  version = "0.42.0"
  source  = "github.com/terraform-linters/tflint-ruleset-aws"
}

rule "terraform_required_version" {
  enabled = true
}

rule "terraform_required_providers" {
  enabled = true
}

# Off. Every variable and output in this stack has a description, and several are
# multi-paragraph; the rule only checks presence, which is already true.
rule "terraform_documented_variables" {
  enabled = false
}

rule "terraform_documented_outputs" {
  enabled = false
}
