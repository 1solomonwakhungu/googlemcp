variable "project_id" {
  description = "Google Cloud project ID that hosts the GoogleMCP Desktop OAuth client, its APIs, and (optionally) its secrets."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "project_id must be 6 to 30 characters: lowercase letters, digits, and hyphens, starting with a letter and not ending with a hyphen."
  }
}

variable "name_prefix" {
  description = "Secret Manager ID prefix; must match the server's GOOGLEMCP_SECRET_PREFIX (default googlemcp)."
  type        = string
  default     = "googlemcp"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,40}$", var.name_prefix))
    error_message = "name_prefix must start with a lowercase letter and use only lowercase letters, digits, and hyphens (max 41 characters)."
  }
}

variable "labels" {
  description = "Extra labels applied to resources that support labels."
  type        = map(string)
  default     = {}

  validation {
    condition = alltrue([
      for k, v in var.labels :
      can(regex("^[a-z][a-z0-9_-]{0,62}$", k)) && can(regex("^[a-z0-9_-]{0,63}$", v))
    ])
    error_message = "Label keys must start with a lowercase letter and use only lowercase letters, digits, underscores, and hyphens (max 63); values use the same characters."
  }
}

variable "catalog_path" {
  description = "Path to catalog/services.json. Null uses the repository catalog relative to this module."
  type        = string
  default     = null
}

variable "products" {
  description = "Per-product overrides of the catalog default_enabled flag, keyed by catalog product key. Example: { appsscript = true, gmail = false }."
  type        = map(bool)
  default     = {}

  validation {
    condition     = length(setsubtract(keys(var.products), keys(local.catalog.products))) == 0
    error_message = "products contains keys that are not in catalog/services.json."
  }
}

variable "extra_services" {
  description = "Additional API service names to enable beyond the catalog, for example storage.googleapis.com."
  type        = set(string)
  default     = []

  validation {
    condition     = alltrue([for s in var.extra_services : can(regex("^[a-z0-9-]+\\.googleapis\\.com$", s))])
    error_message = "extra_services entries must be service names such as storage.googleapis.com."
  }
}

variable "enable_secret_manager" {
  description = "Create Secret Manager containers for oauth-client-id, oauth-client-secret, and refresh-token, for the optional secretmanager backend. Off by default because the local server stores them in the OS keyring."
  type        = bool
  default     = false
}

variable "secret_replica_locations" {
  description = "Secret Manager replica locations. Empty uses automatic replication; set regions (for example [\"us-central1\"]) for user-managed replication."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for l in var.secret_replica_locations : can(regex("^[a-z]+-[a-z]+[0-9]+$", l))])
    error_message = "secret_replica_locations entries must be regions such as us-central1."
  }
}

variable "secret_deletion_protection" {
  description = "Block terraform destroy from deleting secret containers (and every stored version, including the refresh token)."
  type        = bool
  default     = true
}
