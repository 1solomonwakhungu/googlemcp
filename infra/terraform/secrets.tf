# Optional Secret Manager containers for the local server's secretmanager
# backend. The default backend is the OS keyring, which needs nothing here,
# so this is off unless enable_secret_manager = true.
#
# Containers only. Terraform never writes secret versions: any value passed to
# google_secret_manager_secret_version lands in plain text in Terraform state.
# `googlemcp --secret-backend secretmanager auth login` writes the values.

locals {
  # Logical key => description. Secret ID is <name_prefix>-<key with hyphens>,
  # the names the server reads (GOOGLEMCP_SECRET_PREFIX).
  secrets = var.enable_secret_manager ? {
    oauth_client_id     = "Desktop OAuth client ID used by `googlemcp auth login`."
    oauth_client_secret = "Desktop OAuth client secret used by `googlemcp auth login`."
    refresh_token       = "Owner refresh token for Google APIs, written by the CLI after consent."
  } : {}

  secret_ids = {
    for key in keys(local.secrets) : key => "${var.name_prefix}-${replace(key, "_", "-")}"
  }
}

resource "google_secret_manager_secret" "this" {
  for_each = local.secrets

  project   = var.project_id
  secret_id = local.secret_ids[each.key]

  labels = merge(local.labels, {
    googlemcp-secret = replace(each.key, "_", "-")
  })

  annotations = {
    description = each.value
  }

  deletion_protection = var.secret_deletion_protection

  replication {
    dynamic "auto" {
      for_each = length(var.secret_replica_locations) == 0 ? [1] : []
      content {}
    }

    dynamic "user_managed" {
      for_each = length(var.secret_replica_locations) == 0 ? [] : [1]
      content {
        dynamic "replicas" {
          for_each = var.secret_replica_locations
          content {
            location = replicas.value
          }
        }
      }
    }
  }

  depends_on = [google_project_service.this]
}
