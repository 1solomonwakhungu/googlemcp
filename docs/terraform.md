# Terraform

The GoogleMCP server runs locally on your machine over stdio. Google Cloud
is used only for the Desktop OAuth client, the APIs that client calls,
and, optionally, Secret Manager storage for the client and refresh token.
`infra/terraform` manages every part of that with a supported Terraform
resource. Everything else (OAuth consent screen, the OAuth client, the Apps
Script user toggle) is a documented manual step in
[manual-steps.md](manual-steps.md). Terraform is optional: if you only need
the APIs enabled, `gcloud services enable` does the same job
([manual-steps.md](manual-steps.md#enable-the-apis)).

Nothing here hosts the server: there is no Cloud Run, Artifact Registry,
runtime service account, or public endpoint, and
`tests/test_terraform_policy.py` fails the build if any appears.

## What Terraform manages

| Area | Resources | Default |
|---|---|---|
| APIs | `google_project_service` for catalog `platform_services` plus the `api_services` of every enabled product in `catalog/services.json` | On. Products with `default_enabled = false` (chat, meet, youtube_reporting, appsscript) stay off unless opted in |
| Secrets | Secret Manager containers `googlemcp-oauth-client-id`, `googlemcp-oauth-client-secret`, `googlemcp-refresh-token` for the optional `secretmanager` backend; automatic replication, deletion protection | Off (`enable_secret_manager = false`) |
| State bucket | `bootstrap/` root: versioned GCS bucket, uniform access, public access prevention | Separate one-time apply |

What Terraform deliberately does not manage:

- **Secret values.** Any value given to `google_secret_manager_secret_version`
  is stored in plain text in Terraform state. `googlemcp auth login` writes the
  values. `tests/test_terraform_policy.py` fails the build if a secret version
  resource appears.
- **OAuth consent screen and OAuth client.** `google_iap_brand` and
  `google_iap_client` were removed in provider 8.0.0 and the IAP OAuth Admin
  API shut down on 2026-03-19. No public API replaces them. See
  [manual-steps.md](manual-steps.md#why-these-steps-are-manual).
- **The project and its billing link.** The project also holds the state
  bucket, so it is created once with `gcloud` and protected from
  `terraform destroy`.
- **IAM.** Your account, as project Owner, already has every permission
  the local CLI needs, including reading and adding versions of the three
  secrets. Nothing else needs access, so the module grants nothing.

## Secret storage: keyring by default

The server's default secret store is the OS keyring on your machine,
which needs no cloud resources. Set `enable_secret_manager = true` to create
the three containers instead and run the CLI with
`--secret-backend secretmanager` and `GOOGLEMCP_GCP_PROJECT` (for example to
use the same token from more than one machine; one shared token also stays
clear of Google's limit of 100 refresh tokens per client and account). Secret
IDs are `<name_prefix>-<key>`, matching the server's `GOOGLEMCP_SECRET_PREFIX`
(default `googlemcp`).

| Secret ID | Written by | Purpose |
|---|---|---|
| `googlemcp-oauth-client-id` | `googlemcp auth login --client-secrets ...` | Desktop OAuth client ID |
| `googlemcp-oauth-client-secret` | `googlemcp auth login --client-secrets ...` | Desktop OAuth client secret |
| `googlemcp-refresh-token` | `googlemcp auth login` | Your refresh token for Google APIs |

## Prerequisites

- Terraform 1.9 or newer (`terraform test` mock providers need 1.7, and
  variable validation that reads the catalog needs 1.9). Tested with 1.15.7.
- `gcloud` CLI, signed in with an account that owns the project.
- A Google Cloud project
  ([manual-steps.md](manual-steps.md#1-create-the-project)). Billing must be
  linked for the remote state bucket and for Secret Manager.

Credentials come from Application Default Credentials. Set the quota project
so user-credential API calls are attributed to your project:

```sh
gcloud auth application-default login
gcloud auth application-default set-quota-project PROJECT_ID
```

No service account key is needed or supported.

## State

State never goes in git. `.gitignore` excludes `*.tfstate`, `*.tfstate.*`,
`*.tfplan`, `*.tfvars` (except `*.tfvars.example`), and `.terraform/`.

State for this module holds enabled API names and, with
`enable_secret_manager`, secret container metadata. It holds no secret
values because Terraform never manages secret versions. Keep it in a private
remote bucket anyway. Every command block below runs from the repository
root and uses `terraform -chdir`, so the blocks work top to bottom in one
shell:

1. Create the bucket once with the bootstrap root (its own small state stays
   local in `infra/terraform/bootstrap/terraform.tfstate`, which is git-ignored;
   back it up or re-import the bucket if lost):

   ```sh
   terraform -chdir=infra/terraform/bootstrap init
   terraform -chdir=infra/terraform/bootstrap apply -var project_id=PROJECT_ID
   ```

   The bucket has object versioning (20 older versions kept), uniform
   bucket-level access, enforced public access prevention, Google-managed
   encryption at rest (set `kms_key_name` on `modules/state_bucket` for a
   customer-managed key), and `prevent_destroy`.

2. Point the main root at it:

   ```sh
   cp infra/terraform/backend.tf.example infra/terraform/backend.tf   # then set bucket = "PROJECT_ID-tfstate"
   terraform -chdir=infra/terraform init
   ```

   To keep the bucket name out of every file, write `backend.tf` with only an
   empty backend block and pass the values at init time instead:

   ```sh
   printf 'terraform {\n  backend "gcs" {}\n}\n' > infra/terraform/backend.tf
   terraform -chdir=infra/terraform init \
     -backend-config="bucket=PROJECT_ID-tfstate" \
     -backend-config="prefix=googlemcp/main"
   ```

   `-backend-config` needs that `backend "gcs"` block; without a
   `backend.tf`, Terraform prints a warning, ignores the values, and keeps
   local state.

   For a first trial run without a bucket, skip `backend.tf`; Terraform keeps
   local state, which is still git-ignored. Move it later with
   `terraform init -migrate-state`.

## Plan and apply

From the repository root:

```sh
cp infra/terraform/terraform.tfvars.example infra/terraform/terraform.tfvars   # set project_id; git-ignored
terraform -chdir=infra/terraform init
terraform -chdir=infra/terraform plan -out=googlemcp.tfplan
terraform -chdir=infra/terraform apply googlemcp.tfplan
terraform -chdir=infra/terraform output
```

Then follow [manual-steps.md](manual-steps.md) for the consent screen, the
Desktop OAuth client, and signing in.

Enabling or disabling products later: edit `products` in `terraform.tfvars`
and apply. Disabling a product removes it from Terraform but leaves the API
enabled in the project (`disable_on_destroy = false`), because turning APIs
off can break other workloads. Disable it in the console if you want it off.

## Operator permissions

The simplest operator is the project Owner. For a narrower account, grant
these project roles outside this module:

- `roles/serviceusage.serviceUsageAdmin` (enable APIs)
- `roles/secretmanager.admin` (only with `enable_secret_manager`)
- `roles/storage.admin` on the state bucket only

## Variables

| Name | Type | Default | Description |
|---|---|---|---|
| `project_id` | `string` | required | Google Cloud project ID that hosts the GoogleMCP Desktop OAuth client, its APIs, and (optionally) its secrets. |
| `name_prefix` | `string` | `"googlemcp"` | Secret Manager ID prefix; must match the server's GOOGLEMCP_SECRET_PREFIX (default googlemcp). |
| `labels` | `map(string)` | `{}` | Extra labels applied to resources that support labels. |
| `catalog_path` | `string` | `null` | Path to catalog/services.json. Null uses the repository catalog relative to this module. |
| `products` | `map(bool)` | `{}` | Per-product overrides of the catalog default_enabled flag, keyed by catalog product key. Example: { appsscript = true, gmail = false }. |
| `extra_services` | `set(string)` | `[]` | Additional API service names to enable beyond the catalog, for example storage.googleapis.com. |
| `enable_secret_manager` | `bool` | `false` | Create Secret Manager containers for oauth-client-id, oauth-client-secret, and refresh-token, for the optional secretmanager backend. Off by default because the local server stores them in the OS keyring. |
| `secret_replica_locations` | `list(string)` | `[]` | Secret Manager replica locations. Empty uses automatic replication; set regions (for example ["us-central1"]) for user-managed replication. |
| `secret_deletion_protection` | `bool` | `true` | Block terraform destroy from deleting secret containers (and every stored version, including the refresh token). |

## Outputs

| Name | Description |
|---|---|
| `project_id` | Project the module manages |
| `enabled_products` | Catalog product keys whose APIs are enabled |
| `enabled_services` | Every API service this module enables |
| `secret_prefix` | Value for `GOOGLEMCP_SECRET_PREFIX` with the secretmanager backend |
| `secret_ids` | Secret IDs by logical key (empty unless `enable_secret_manager`) |
| `manual_steps` | Reminder list of the manual steps |

## Verification

All checks run offline with no Google credentials:

```sh
infra/terraform/scripts/verify.sh   # fmt, init -backend=false, validate (root and bootstrap), terraform test, tflint if installed
uv run pytest tests/test_terraform_policy.py
```

`terraform test` uses `mock_provider "google"` and covers: default products
enable the expected services; opt-in products stay off by default and turn on
when opted in; opt-outs remove services; unknown product keys and invalid
inputs are rejected; no Secret Manager containers exist by default; opting in
creates exactly the three Desktop flow secrets with deletion protection and
the chosen replication.

`tests/test_terraform_policy.py` statically rejects secret version resources,
secret payload attributes, secret write roles, service account keys, API
keys, project-level IAM, public IAM members, the removed IAP OAuth resources,
any hosting or runtime identity resource (Cloud Run, Artifact Registry,
service accounts, Compute, App Engine, Cloud Functions), hosting APIs
(`run`, `artifactregistry`, `cloudbuild`) in Terraform or the catalog, storage
buckets outside `modules/state_bucket`, `mcp-*` secrets, and tracked state or
tfvars files.

tflint uses `.tflint.hcl` with the `terraform` recommended preset and the
`google` ruleset 0.40.0. Install tflint locally (`brew install tflint`) or rely
on CI; `verify.sh` skips it with a message when it is missing. checkov runs in
CI with the repository `.checkov.yaml`; the one inline skip (state bucket
access logging) carries its reason.

Provider: `hashicorp/google ~> 8.5` (8.5.0 at the time of writing). The lock
files carry hashes for linux_amd64, linux_arm64, darwin_amd64, darwin_arm64,
and windows_amd64.
