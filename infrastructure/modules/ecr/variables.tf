variable "name_prefix" {
  description = "Prefix applied to the repository name."
  type        = string
}

variable "repository_name" {
  description = "Repository name, appended to name_prefix."
  type        = string
  default     = "backend"
}

variable "image_tag_mutability" {
  description = <<-DESC
    IMMUTABLE refuses to move a tag that already exists.

    That is what makes a deployed tag mean one image forever: a task definition
    records the image identifier it was registered with, and with mutable tags "the
    thing running in prod" and "what :v1.4.0 resolves to today" can silently
    differ. The cost is that CI cannot re-push a tag after a failed run — it has to
    push a new one, which is the correct behaviour anyway.
  DESC
  type        = string
  default     = "IMMUTABLE"

  validation {
    condition     = contains(["MUTABLE", "IMMUTABLE"], var.image_tag_mutability)
    error_message = "image_tag_mutability must be MUTABLE or IMMUTABLE."
  }
}

variable "scan_on_push" {
  description = "Run ECR's basic vulnerability scan when an image is pushed. Free, and the only scan this stack configures."
  type        = bool
  default     = true
}

variable "force_delete" {
  description = "Allow the repository to be destroyed while it still holds images. True in a disposable environment; in prod it turns one `terraform destroy` into the loss of every image that was ever deployed."
  type        = bool
  default     = false
}

variable "untagged_image_expiry_days" {
  description = "Days an untagged image survives. These are layers orphaned when a tag moved or a push was abandoned; nothing can deploy them."
  type        = number
  default     = 7
}

variable "max_tagged_images" {
  description = <<-DESC
    How many tagged images to keep. Old images are the rollback path, so this is
    how far back a rollback can go — not just a storage number. Storage is
    $0.10/GB/month and the runtime image is a few hundred megabytes.
  DESC
  type        = number
  default     = 30
}

variable "pull_principal_arns" {
  description = <<-DESC
    Role ARNs granted pull access through the repository policy.

    The ECS execution role is granted pull by its own IAM policy, which is enough
    within this account. This exists for the case the repository policy is the only
    lever: a build account pushing in, or a second account pulling the image out.
  DESC
  type        = list(string)
  default     = []
}

variable "tags" {
  description = "Tags merged onto every resource this module creates."
  type        = map(string)
  default     = {}
}
