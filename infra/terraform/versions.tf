terraform {
  # 1.9 is the floor because variable validation below refers to other
  # module objects (the catalog), and `terraform test` mock providers need 1.7.
  required_version = ">= 1.9.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.5"
    }
  }
}
