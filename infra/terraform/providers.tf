# Credentials come from Application Default Credentials. Set a quota project
# once with `gcloud auth application-default set-quota-project <project_id>`;
# see docs/terraform.md.
provider "google" {
  project = var.project_id
}
