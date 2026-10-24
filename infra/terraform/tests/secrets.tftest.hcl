# Offline: the mock provider never calls Google.
mock_provider "google" {}

variables {
  project_id = "googlemcp-test-123"
}

run "no_secret_containers_by_default" {
  command = plan

  assert {
    condition     = length(google_secret_manager_secret.this) == 0 && length(output.secret_ids) == 0
    error_message = "The default local backend is the keyring, so no Secret Manager containers are created."
  }
}

run "opt_in_creates_only_the_desktop_flow_secrets" {
  command = apply

  variables {
    enable_secret_manager = true
  }

  assert {
    condition = toset([for s in google_secret_manager_secret.this : s.secret_id]) == toset([
      "googlemcp-oauth-client-id",
      "googlemcp-oauth-client-secret",
      "googlemcp-refresh-token",
    ])
    error_message = "Only the three Desktop OAuth secrets exist; no mcp-* secrets."
  }

  assert {
    condition     = alltrue([for s in google_secret_manager_secret.this : s.deletion_protection])
    error_message = "Secrets are deletion protected by default."
  }

  assert {
    condition     = alltrue([for s in google_secret_manager_secret.this : length(s.replication[0].auto) == 1 && length(s.replication[0].user_managed) == 0])
    error_message = "Default replication is automatic."
  }

  assert {
    condition     = output.secret_ids["refresh_token"] == "googlemcp-refresh-token"
    error_message = "secret_ids maps logical keys to secret IDs."
  }
}

run "prefix_and_user_managed_replication" {
  command = apply

  variables {
    enable_secret_manager      = true
    name_prefix                = "gmcp"
    secret_replica_locations   = ["us-central1", "us-east1"]
    secret_deletion_protection = false
  }

  assert {
    condition     = google_secret_manager_secret.this["oauth_client_secret"].secret_id == "gmcp-oauth-client-secret"
    error_message = "name_prefix drives secret IDs (GOOGLEMCP_SECRET_PREFIX)."
  }

  assert {
    condition = alltrue([
      for s in google_secret_manager_secret.this :
      length(s.replication[0].auto) == 0 && length(s.replication[0].user_managed[0].replicas) == 2 && !s.deletion_protection
    ])
    error_message = "secret_replica_locations switches to user-managed replication."
  }
}

run "invalid_prefix_is_rejected" {
  command = plan

  variables {
    name_prefix = "Bad_Prefix"
  }

  expect_failures = [var.name_prefix]
}
