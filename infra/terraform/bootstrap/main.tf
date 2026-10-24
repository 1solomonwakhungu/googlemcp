# One-time bootstrap: creates the remote state bucket for the main root module.
# This root keeps its own small state locally (terraform.tfstate here is
# git-ignored). Run it once, then configure ../backend.tf from
# ../backend.tf.example. See docs/terraform.md.

terraform {
  required_version = ">= 1.9.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.5"
    }
  }
}

provider "google" {
  project = var.project_id
}

variable "project_id" {
  description = "Project that owns the state bucket."
  type        = string
}

variable "bucket_name" {
  description = "Globally unique bucket name. Null uses <project_id>-tfstate."
  type        = string
  default     = null
}

variable "location" {
  description = "Bucket location."
  type        = string
  default     = "US"
}

resource "google_project_service" "storage" {
  project            = var.project_id
  service            = "storage.googleapis.com"
  disable_on_destroy = false
}

module "state_bucket" {
  source = "../modules/state_bucket"

  project_id  = var.project_id
  bucket_name = coalesce(var.bucket_name, "${var.project_id}-tfstate")
  location    = var.location
  labels = {
    app        = "googlemcp"
    managed-by = "terraform"
  }

  depends_on = [google_project_service.storage]
}

output "state_bucket" {
  description = "Use this as bucket in ../backend.tf."
  value       = module.state_bucket.bucket_name
}
