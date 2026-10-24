variable "project_id" {
  description = "Project that owns the state bucket."
  type        = string
}

variable "bucket_name" {
  description = "Globally unique bucket name, for example <project_id>-tfstate."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9_-]{1,61}[a-z0-9]$", var.bucket_name)) && !can(regex("^goog|google", var.bucket_name))
    error_message = "bucket_name must be 3 to 63 lowercase letters, digits, hyphens, or underscores, and must not start with goog or contain google."
  }
}

variable "location" {
  description = "Bucket location (region or multi-region)."
  type        = string
  default     = "US"
}

variable "noncurrent_versions_to_keep" {
  description = "Older state versions kept for recovery."
  type        = number
  default     = 20

  validation {
    condition     = var.noncurrent_versions_to_keep >= 1
    error_message = "Keep at least one noncurrent version."
  }
}

variable "kms_key_name" {
  description = "Optional Cloud KMS key for customer-managed encryption. Null uses Google-managed encryption."
  type        = string
  default     = null
}

variable "labels" {
  description = "Bucket labels."
  type        = map(string)
  default     = {}
}
