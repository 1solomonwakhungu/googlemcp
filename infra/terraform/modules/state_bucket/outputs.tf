output "bucket_name" {
  description = "State bucket name for backend \"gcs\"."
  value       = google_storage_bucket.state.name
}
