terraform {
  required_version = ">= 1.9.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.5"
    }
  }
}

# Remote state bucket. Terraform state for this repo contains resource
# metadata (no secret values, since secret versions are never managed here),
# but it still belongs in a private, versioned bucket and never in git.
resource "google_storage_bucket" "state" {
  # checkov:skip=CKV_GCP_62:Single-operator state bucket; usage logs need a second log bucket. Enable Data Access audit logs for storage.googleapis.com if object-level read history is needed.
  project  = var.project_id
  name     = var.bucket_name
  location = var.location
  labels   = var.labels

  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  versioning {
    enabled = true
  }

  # Keep a bounded history of state versions for recovery.
  lifecycle_rule {
    condition {
      num_newer_versions = var.noncurrent_versions_to_keep
      with_state         = "ARCHIVED"
    }
    action {
      type = "Delete"
    }
  }

  dynamic "encryption" {
    for_each = var.kms_key_name == null ? [] : [var.kms_key_name]
    content {
      default_kms_key_name = encryption.value
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}
