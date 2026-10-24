output "project_id" {
  description = "Project the module manages."
  value       = var.project_id
}

output "enabled_products" {
  description = "Catalog product keys whose APIs are enabled."
  value       = local.enabled_products
}

output "enabled_services" {
  description = "Every API service this module enables."
  value       = sort(tolist(local.services))
}

output "secret_prefix" {
  description = "Value for GOOGLEMCP_SECRET_PREFIX when using the secretmanager backend."
  value       = var.name_prefix
}

output "secret_ids" {
  description = "Secret Manager secret IDs by logical key (empty unless enable_secret_manager). Containers only; `googlemcp --secret-backend secretmanager auth login` writes the values."
  value       = local.secret_ids
}

output "manual_steps" {
  description = "Steps Terraform cannot manage. Follow docs/manual-steps.md."
  value = [
    "Google Auth Platform branding, audience (External, publishing status), test user, and data access scopes",
    "Desktop OAuth client for `googlemcp auth login`, stored in the keyring (default) or Secret Manager",
    "Apps Script API user setting (only if appsscript is enabled)",
  ]
}
