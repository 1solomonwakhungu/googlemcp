locals {
  catalog_path = coalesce(var.catalog_path, "${path.module}/../../catalog/services.json")
  catalog      = jsondecode(file(local.catalog_path))

  # A product is enabled when an override says so, else by its catalog default.
  enabled_products = sort([
    for key, product in local.catalog.products : key
    if lookup(var.products, key, product.default_enabled)
  ])

  product_services = flatten([
    for key in local.enabled_products : local.catalog.products[key].api_services
  ])

  services = toset(concat(
    local.catalog.platform_services,
    local.product_services,
    tolist(var.extra_services),
  ))

  labels = merge(var.labels, {
    app        = "googlemcp"
    managed-by = "terraform"
  })
}

resource "google_project_service" "this" {
  for_each = local.services

  project = var.project_id
  service = each.value

  # Never switch an API off on destroy: other workloads in the project may use it.
  disable_on_destroy         = false
  disable_dependent_services = false
}
