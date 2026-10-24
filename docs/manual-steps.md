# Manual steps

GoogleMCP runs locally on your machine over stdio. These are the one-time
Google Cloud steps that cannot be done with Terraform or any public API. Do
them in order, signed in to the Google Cloud console with the Google account
you want GoogleMCP to act as. Replace `PROJECT_ID` with your project ID and
`you@example.com` with your Google account everywhere. Console links take
`?project=PROJECT_ID` to open the right project.

## Why these steps are manual

- `google_iap_brand` and `google_iap_client` were deprecated in provider
  6.43.0 and removed in 8.0.0. The provider's
  [8.0 upgrade guide](https://github.com/hashicorp/terraform-provider-google/blob/main/website/docs/guides/version_8_upgrade.html.markdown)
  says: "OAuth consent screens (brands) and OAuth clients can no longer be
  managed programmatically via the API and must be configured directly in the
  Google Cloud Console."
- The IAP OAuth Admin API behind them shut down on 2026-03-19
  ([IAP deprecations](https://cloud.google.com/iap/docs/deprecations),
  [programmatic OAuth clients](https://cloud.google.com/iap/docs/programmatic-oauth-clients)).
  Even before that, it could create only "Organization Internal" brands, never
  the External consent screen a personal @gmail.com account needs.
- `google_iam_oauth_client` and `gcloud iam oauth-clients` are for Workforce
  Identity Federation only and cannot authorize Gmail, Drive, or other consumer
  APIs ([Workforce OAuth apps](https://cloud.google.com/iam/docs/workforce-manage-oauth-app)).
- The open provider request for consent screen support is
  [hashicorp/terraform-provider-google#17649](https://github.com/hashicorp/terraform-provider-google/issues/17649).
- The Apps Script API user setting has no API.

Everything else can be automated: Terraform enables the APIs and,
optionally, creates the Secret Manager containers for the Desktop client and
refresh token ([terraform.md](terraform.md)). If you skip Terraform,
`gcloud services enable` covers the APIs
([Enable the APIs](#enable-the-apis)).

## 1. Create the project

```sh
gcloud auth login
gcloud projects create PROJECT_ID --name="GoogleMCP"
gcloud config set project PROJECT_ID
gcloud auth application-default login
gcloud auth application-default set-quota-project PROJECT_ID
```

The two `application-default` commands give Terraform and the optional
Secret Manager backend credentials; skip them if you use neither.

Link a billing account if you use the remote Terraform state bucket or the
optional Secret Manager backend; both need billing. Typical personal use
stays within their free tiers.

```sh
gcloud billing accounts list
gcloud billing projects link PROJECT_ID --billing-account=BILLING_ACCOUNT_ID
```

### Enable the APIs

Enable the APIs before you configure the consent screen. Use one of:

- **Terraform** ([terraform.md](terraform.md)): enables the APIs for every
  enabled product from `catalog/services.json`, keeps per-product overrides
  in `terraform.tfvars`, and can create the optional Secret Manager
  containers.
- **gcloud**: enable the product APIs directly. For the default product set:

  ```sh
  gcloud services enable --project=PROJECT_ID \
    tasks.googleapis.com gmail.googleapis.com calendar-json.googleapis.com \
    drive.googleapis.com docs.googleapis.com sheets.googleapis.com \
    slides.googleapis.com forms.googleapis.com people.googleapis.com \
    driveactivity.googleapis.com youtube.googleapis.com \
    youtubeanalytics.googleapis.com analyticsdata.googleapis.com \
    analyticsadmin.googleapis.com searchconsole.googleapis.com \
    photoslibrary.googleapis.com photospicker.googleapis.com
  ```

  Each product's API services are listed under `api_services` in
  `catalog/services.json`; with `jq` installed you can derive the list from
  there instead:
  `jq -r '.products[] | select(.default_enabled) | .api_services[]' catalog/services.json | xargs gcloud services enable --project=PROJECT_ID`.
  The opt-in products need `meet.googleapis.com`, `chat.googleapis.com`,
  `youtubereporting.googleapis.com`, or `script.googleapis.com`. Add
  `secretmanager.googleapis.com` only if you use the Secret Manager backend.
  You can leave out APIs for products you will not enable.

## 2. Google Auth Platform: branding

1. Open <https://console.cloud.google.com/auth/branding?project=PROJECT_ID>.
   If the console shows **Get started**, click it; the wizard covers steps 2
   and 3.
2. **App name:** `GoogleMCP`.
3. **User support email:** your Google account (`you@example.com`).
4. **App logo:** leave empty. Uploading a logo requires brand verification.
5. **App domain** fields (home page, privacy policy, terms): leave empty for
   personal use.
6. **Authorized domains:** leave empty. The Desktop client uses a loopback
   redirect, which needs no authorized domain.
7. **Developer contact information:** your email address.
8. Click **Save**.

## 3. Google Auth Platform: audience and test user

1. Open <https://console.cloud.google.com/auth/audience?project=PROJECT_ID>.
2. **User type:** `External`. A personal @gmail.com account has no
   organization, so `Internal` is not available.
3. Under **Test users**, click **Add users**, enter your own Google account
   (`you@example.com`), and click **Save**. While the app is in Testing,
   only listed test users can sign in.
4. **Publishing status:** read [access-longevity.md](access-longevity.md)
   first. Summary:
   - `Testing`: every refresh token expires 7 days after consent, so
     `uv run googlemcp auth login` must be repeated weekly.
   - `In production` (recommended when you are the only user): click
     **Publish app** and confirm. Do not submit the app for verification.
     Refresh tokens then follow the normal lifetime rules.

   In either status, an unverified app that requests sensitive or
   restricted scopes (Gmail, Drive, and most others here) shows a "Google
   hasn't verified this app" screen at sign-in. Click **Advanced**, then
   **Go to GoogleMCP (unsafe)**. This is expected for an app you created
   for yourself.

## 4. Google Auth Platform: data access (scopes)

1. List the scopes the server will request for the products you enabled.
   [inventory.md](inventory.md) lists every product's scopes with Google's
   sensitivity class; the CLI prints the exact set for your configuration.
   First export the same `GOOGLEMCP_*` settings your MCP client will use,
   and keep them exported in this shell for `auth login` in section 5:

   ```sh
   export GOOGLEMCP_PRODUCTS=tasks,calendar,gmail,drive   # unset: catalog defaults
   uv run googlemcp scopes
   ```

   The output reflects `GOOGLEMCP_PRODUCTS` and `GOOGLEMCP_WRITE_PRODUCTS`,
   one scope per line. The commands on this page assume a checkout
   (`git clone` and `uv sync`). With the `uvx` install, replace
   `uv run googlemcp` with
   `uvx --from git+https://github.com/1solomonwakhungu/googlemcp googlemcp`.
2. Open <https://console.cloud.google.com/auth/scopes?project=PROJECT_ID>.
3. Click **Add or remove scopes**, paste the scopes into **Manually add
   scopes**, click **Add to table**, then **Update**, then **Save**.
4. Gmail and full Drive scopes are restricted; Google lists them in the
   **Your restricted scopes** section. That is expected. See
   [access-longevity.md](access-longevity.md#restricted-scopes).

If you later enable more products or writes, export the new settings, rerun
`uv run googlemcp scopes`, add the new scopes on this page, and run
`uv run googlemcp auth login` again (section 5, item 6).

## 5. Desktop OAuth client (for `googlemcp auth login`)

1. Open <https://console.cloud.google.com/auth/clients?project=PROJECT_ID>.
2. Click **Create client**.
3. **Application type:** `Desktop app`. **Name:** `googlemcp-desktop`.
4. Click **Create**.
5. In the dialog, click **Download JSON** before closing it. Since June 2025
   the full client secret is visible and downloadable only at creation;
   afterwards the console shows the last four characters only
   ([Manage OAuth clients](https://support.google.com/cloud/answer/15549257)).
   Keep the downloaded file until item 7 confirms the secret is stored; it is
   your only copy. If you lose it, add a new secret to the client (or create a
   new client) and download that JSON.
6. Store the client and sign in, in one step, in the shell where the
   section 4 settings are exported. The default store is the OS keyring
   (macOS Keychain, Windows Credential Locker, or Secret Service):

   ```sh
   uv run googlemcp auth login --client-secrets ~/Downloads/client_secret_XXXX.json
   ```

   The CLI copies the client ID and secret into the store, opens the browser
   for consent (add `--login-hint you@example.com` to preselect your
   account), and stores the refresh token.
   If the command fails or you close the browser early, fix the cause and run
   it again with the same file. Never commit the file (`.gitignore` blocks
   `client_secret*.json`).

   **Optional Secret Manager backend.** To keep the three secrets in the
   project instead (for example to share them between your machines), the
   secrets must exist first: the backend only adds versions and never creates
   a secret. Either set `enable_secret_manager = true` in Terraform and apply
   ([terraform.md](terraform.md)), or create them with gcloud:

   ```sh
   gcloud services enable secretmanager.googleapis.com --project=PROJECT_ID
   gcloud secrets create googlemcp-oauth-client-id --replication-policy=automatic --project=PROJECT_ID
   gcloud secrets create googlemcp-oauth-client-secret --replication-policy=automatic --project=PROJECT_ID
   gcloud secrets create googlemcp-refresh-token --replication-policy=automatic --project=PROJECT_ID
   ```

   The IDs are `<GOOGLEMCP_SECRET_PREFIX>-<key>`, so change `googlemcp` if
   you set a different prefix. No IAM binding is needed: as project Owner
   your account can already create the secrets and read and add their
   versions. Then add the backend flag to every command in this section. Two
   prerequisites:

   - The Secret Manager client library is an optional extra. The repository's
     dev environment (`uv sync`) already includes it; anywhere else install
     it with `uv sync --extra secretmanager` in a checkout, or run
     `uvx --from 'googlemcp[secretmanager] @ git+https://github.com/1solomonwakhungu/googlemcp' googlemcp ...`.
   - The client authenticates with Application Default Credentials, so run
     `gcloud auth application-default login` (and
     `gcloud auth application-default set-quota-project PROJECT_ID`) with an
     account that can read and add secret versions in the project, as in
     section 1.

   ```sh
   export GOOGLEMCP_GCP_PROJECT=PROJECT_ID
   uv run googlemcp --secret-backend secretmanager auth login --client-secrets ~/Downloads/client_secret_XXXX.json
   ```

   Values land in `googlemcp-oauth-client-id`, `googlemcp-oauth-client-secret`,
   and `googlemcp-refresh-token`.
7. Verify, and only then delete the downloaded file:

   ```sh
   uv run googlemcp auth status \
     && uv run python scripts/acceptance/verify_stored_client.py --json ~/Downloads/client_secret_XXXX.json \
     && rm ~/Downloads/client_secret_XXXX.json
   ```

   With the Secret Manager backend (and `GOOGLEMCP_GCP_PROJECT` still set):

   ```sh
   uv run googlemcp --secret-backend secretmanager auth status \
     && uv run python scripts/acceptance/verify_stored_client.py --json ~/Downloads/client_secret_XXXX.json --secret-backend secretmanager \
     && rm ~/Downloads/client_secret_XXXX.json
   ```

   `auth status` exits 0 only when a client and refresh token are stored and a
   live token refresh succeeds. That alone does not prove the stored secret is
   the one in this file: after a secret rotation the old secret can still
   refresh until you delete it. `verify_stored_client.py` closes that gap. It
   opens the same store the server uses, compares the stored client ID and
   secret with the file without printing either, and exits 0 only when both
   match. If either command fails, keep the file, fix what it reports, and
   rerun item 6. `uv run googlemcp doctor` gives a fuller report.

OAuth clients unused for six months are deleted automatically; Google emails
the project owners 30 days before
([Manage OAuth clients](https://support.google.com/cloud/answer/15549257)).
Regular use of the server keeps the client active.

## 6. Apps Script API user setting (only if `appsscript` is enabled)

Access to Apps Script projects through the API is off by default for every
user ([Apps Script dashboard](https://developers.google.com/apps-script/guides/dashboard)).

1. Sign in with the Google account GoogleMCP uses.
2. Open <https://script.google.com/home/usersettings>.
3. Turn **Google Apps Script API** to **On**.

Without this, Apps Script API calls fail with a permission error saying the
user has not enabled the Apps Script API.

## 7. Product prerequisites

| Product | What must exist for your account |
|---|---|
| YouTube Data, YouTube Analytics | A YouTube channel. Analytics queries use `ids=channel==MINE` ([reference](https://developers.google.com/youtube/analytics/reference/reports/query)) |
| YouTube Reporting (opt-in) | A channel. Reporting jobs are created server side and produce daily reports, which is why it is off by default |
| Google Analytics | Your account has at least Viewer access to the GA4 property ([quickstart](https://developers.google.com/analytics/devguides/reporting/data/v1/quickstart-client-libraries)) |
| Search Console | Your account has permission on a verified property ([prerequisites](https://developers.google.com/webmaster-tools/v1/prereqs)) |
| Google Photos | Since 2025-03-31 the Library API reads only media the app created; whole-library selection goes through the Picker API, where you pick items in a Google-hosted page ([Photos API updates](https://developers.google.com/photos/support/updates)) |
| Chat, Meet (opt-in) | A Google Workspace account; the Chat and Meet REST APIs do not serve personal @gmail.com accounts. Chat also needs its app configuration page filled in for write calls ([Configure the Chat API](https://developers.google.com/workspace/chat/configure-chat-api)) |
| Apps Script (opt-in) | The user setting in section 6 |

## Recovery

Add `--secret-backend secretmanager` (with `GOOGLEMCP_GCP_PROJECT` set) to
each command if you use that backend.

- **Refresh token expired or revoked:** rerun `uv run googlemcp auth login`. It
  stores a new refresh token. Causes are listed in
  [access-longevity.md](access-longevity.md#what-ends-a-refresh-token).
- **Client secret lost or leaked:** on the client's page in
  <https://console.cloud.google.com/auth/clients?project=PROJECT_ID>, add a new
  secret and download the client JSON, store it and sign in again (section 5,
  item 6), and run the section 5, item 7 check. Its `verify_stored_client.py`
  step fails while the old secret is still stored, which `auth status` alone
  would not catch. Delete the old secret in the console only after the check
  passes; until then the old secret is what keeps the server working.
- **Old Secret Manager versions (secretmanager backend only):** first confirm
  the latest version is the new value and is enabled
  (`gcloud secrets versions describe latest --secret=SECRET_ID --project=PROJECT_ID --format='value(state)'`
  prints `ENABLED`, and the section 5, item 7 check passes), then
  `gcloud secrets versions disable VERSION --secret=SECRET_ID --project=PROJECT_ID`.
  Never disable the latest version: the CLI and server read `latest`.
