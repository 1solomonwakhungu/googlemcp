# Offline tests: the mock provider never calls Google.
mock_provider "google" {}

variables {
  project_id = "googlemcp-test-123"
}

run "defaults_enable_platform_and_default_products" {
  command = plan

  assert {
    condition = alltrue([
      for s in [
        "serviceusage.googleapis.com",
        "cloudresourcemanager.googleapis.com",
        "iam.googleapis.com",
        "iamcredentials.googleapis.com",
        "secretmanager.googleapis.com",
      ] : contains(keys(google_project_service.this), s)
    ])
    error_message = "Platform services from the catalog must always be enabled."
  }

  assert {
    condition = alltrue([
      for s in [
        "tasks.googleapis.com",
        "gmail.googleapis.com",
        "calendar-json.googleapis.com",
        "drive.googleapis.com",
        "docs.googleapis.com",
        "sheets.googleapis.com",
        "slides.googleapis.com",
        "forms.googleapis.com",
        "people.googleapis.com",
        "driveactivity.googleapis.com",
        "youtube.googleapis.com",
        "youtubeanalytics.googleapis.com",
        "analyticsdata.googleapis.com",
        "analyticsadmin.googleapis.com",
        "searchconsole.googleapis.com",
        "photoslibrary.googleapis.com",
        "photospicker.googleapis.com",
      ] : contains(keys(google_project_service.this), s)
    ])
    error_message = "Every default_enabled product must have its APIs enabled."
  }

  assert {
    # Exactly platform services plus default products: nothing else sneaks in.
    condition     = length(google_project_service.this) == 22
    error_message = "Expected 5 platform + 17 default product services, got ${length(google_project_service.this)}."
  }

  assert {
    condition     = alltrue([for s in google_project_service.this : s.disable_on_destroy == false && s.disable_dependent_services == false])
    error_message = "APIs must never be disabled on destroy."
  }
}

run "opt_in_products_stay_off_by_default" {
  command = plan

  assert {
    condition = !anytrue([
      for s in ["chat.googleapis.com", "meet.googleapis.com", "youtubereporting.googleapis.com", "script.googleapis.com"] :
      contains(keys(google_project_service.this), s)
    ])
    error_message = "chat, meet, youtube_reporting, and appsscript must stay off unless opted in."
  }

  assert {
    condition     = !contains(output.enabled_products, "chat") && !contains(output.enabled_products, "appsscript")
    error_message = "enabled_products must not list opt-in products by default."
  }

  assert {
    condition     = !contains(keys(google_project_service.this), "run.googleapis.com") && !contains(keys(google_project_service.this), "artifactregistry.googleapis.com")
    error_message = "Hosting APIs (Cloud Run, Artifact Registry) are never enabled; the server runs locally."
  }
}

run "opt_in_turns_products_on" {
  command = plan

  variables {
    products = {
      chat              = true
      meet              = true
      youtube_reporting = true
      appsscript        = true
    }
  }

  assert {
    condition = alltrue([
      for s in ["chat.googleapis.com", "meet.googleapis.com", "youtubereporting.googleapis.com", "script.googleapis.com"] :
      contains(keys(google_project_service.this), s)
    ])
    error_message = "Opting in must enable the product APIs."
  }

  assert {
    condition     = length(google_project_service.this) == 26
    error_message = "Expected 22 default services plus 4 opt-in services."
  }
}

run "opt_out_turns_default_products_off" {
  command = plan

  variables {
    products = {
      gmail     = false
      analytics = false
    }
  }

  assert {
    condition = !anytrue([
      for s in ["gmail.googleapis.com", "analyticsdata.googleapis.com", "analyticsadmin.googleapis.com"] :
      contains(keys(google_project_service.this), s)
    ])
    error_message = "Opting out must remove the product APIs."
  }

  assert {
    condition     = contains(keys(google_project_service.this), "secretmanager.googleapis.com")
    error_message = "Platform services stay on regardless of product overrides."
  }
}

run "extra_services_are_added" {
  command = plan

  variables {
    extra_services = ["storage.googleapis.com"]
  }

  assert {
    condition     = contains(keys(google_project_service.this), "storage.googleapis.com")
    error_message = "extra_services must be enabled."
  }
}

run "unknown_product_key_is_rejected" {
  command = plan

  variables {
    products = {
      not_a_product = true
    }
  }

  expect_failures = [var.products]
}

run "invalid_project_id_is_rejected" {
  command = plan

  variables {
    project_id = "Bad_Project"
  }

  expect_failures = [var.project_id]
}
