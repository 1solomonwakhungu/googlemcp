<!-- mcp-name: io.github.1solomonwakhungu/googlemcp -->

# GoogleMCP

[![CI](https://github.com/1solomonwakhungu/googlemcp/actions/workflows/ci.yml/badge.svg)](https://github.com/1solomonwakhungu/googlemcp/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

**A local-only, read-only-by-default MCP server giving AI clients scoped access to 19 Google products (98 tools) with zero cloud relay and zero listening ports.**

Connect Claude Code, Claude Desktop, Cursor, Hermes Agent, or any stdio MCP client to your Google Workspace **plus** YouTube, Google Analytics (GA4), Search Console, Drive Activity, Apps Script, and Google Photos. Credentials stay in your OS keyring, and every tool is gated by a 3-tier safety model built with prompt injection in mind.

- **3-tier least-privilege safety:** Read scopes and read tools only by default (45 read tools across 15 default products). Write tools need per-product `GOOGLEMCP_WRITE_PRODUCTS`; destructive tools (delete, trash, send, run code) need a second opt-in (`GOOGLEMCP_ALLOW_DESTRUCTIVE=true`) and carry explicit MCP `destructiveHint` annotations.
- **Strict local stdio transport:** `googlemcp serve` opens no network listener, has no HTTP mode, and is never hosted. Each user brings their own Google Cloud project and Desktop OAuth client (PKCE on `127.0.0.1` during one-time login).
- **19 Google products in one server:** Workspace apps alongside YouTube Data, YouTube Analytics, YouTube Reporting, Google Analytics (GA4), Search Console, Drive Activity, Apps Script, and Google Photos (post-2025 Picker and app-created Library API).
- **Operator-grade diagnostics:** Inspect exact scopes (`googlemcp scopes`), active tools (`googlemcp tools`), token health (`googlemcp auth status`), and run live read-only smoke checks across every enabled product (`googlemcp doctor --probe`).

Full documentation index: [docs/README.md](docs/README.md).

## What you can ask your AI (read-only by default)

- *"Search my Gmail for invoices from last month and summarize the totals."*
- *"Check my Google Calendar free/busy slots tomorrow and list open Google Tasks."*
- *"Pull top Search Console queries with dropping CTR and compare against GA4 traffic."*
- *"Find the Q3 launch doc in Google Drive, read its text, and check recent Drive Activity."*
- *"Fetch my YouTube channel analytics for the last 28 days and list my top videos."*

## Safety tiers

| Tier | Configuration | What the MCP client can see and request |
|---|---|---|
| Read-only (default) | `GOOGLEMCP_PRODUCTS` unset (or product list) | Read-only OAuth scopes and read tools only (45 tools across 15 default products) |
| Scoped writes | Add `GOOGLEMCP_WRITE_PRODUCTS=tasks,docs` | Adds write scopes and non-destructive write tools (drafts, edits, create) for listed products only |
| Destructive actions | Add `GOOGLEMCP_ALLOW_DESTRUCTIVE=true` | Also exposes irreversible tools (send email, delete task/event/contact, trash file, send chat, run Apps Script) with MCP `destructiveHint` |

Secrets live in your OS keyring by default (macOS Keychain, Windows Credential Locker, Linux Secret Service), with `file` (strict `0600`), `dir`, `env`, and optional Google Secret Manager backends. Upstream Google errors have URLs and credential-like values redacted, and 401/403 error bodies are withheld entirely. Threat model and CI scans: [docs/security.md](docs/security.md). Vulnerability reports: [SECURITY.md](SECURITY.md).

## Products (19 adapters, 98 tools)

`catalog/services.json` is the source of truth; Terraform and the server both read it, and `tests/test_readme.py` checks this table against it. Every tool and OAuth scope is listed in [docs/inventory.md](docs/inventory.md).

| Key | Product | Default | Why off by default |
|---|---|---|---|
| `tasks` | Google Tasks | on | |
| `gmail` | Gmail | on | |
| `calendar` | Google Calendar | on | |
| `drive` | Google Drive | on | |
| `docs` | Google Docs | on | |
| `sheets` | Google Sheets | on | |
| `slides` | Google Slides | on | |
| `forms` | Google Forms | on | |
| `people` | People and Contacts | on | |
| `meet` | Google Meet | off | Meet REST API needs a Google Workspace account |
| `driveactivity` | Drive Activity | on | |
| `chat` | Google Chat | off | Needs a Business or Enterprise Workspace account and a Chat app configured in the console |
| `youtube` | YouTube Data | on | |
| `youtube_analytics` | YouTube Analytics | on | |
| `youtube_reporting` | YouTube Reporting | off | Reporting jobs are created and kept on Google's side |
| `analytics` | Google Analytics | on | |
| `searchconsole` | Search Console | on | |
| `appsscript` | Apps Script | off | Can run code in your account; also needs a manual per-user setting |
| `photos` | Google Photos (Library app-created data and Picker) | on | |

"Default on" means enabled read-only when `GOOGLEMCP_PRODUCTS` is unset. Gmail and Drive use restricted scopes; YouTube Data has a daily quota. Google removed broad Photos Library scopes on 2025-03-31, so the Library API reaches only app-created media while the Picker API reads photos you choose ([Photos API updates](https://developers.google.com/photos/support/updates)). Single-product Tasks guide: [docs/tasks.md](docs/tasks.md).

## Quick start

Prerequisites: Python 3.12+, [uv](https://docs.astral.sh/uv/), the [`gcloud` CLI](https://cloud.google.com/sdk/docs/install), and a Google account. Optional: Terraform 1.9+ if you prefer IaC for API enablement ([docs/terraform.md](docs/terraform.md)).

### 1. Install

```sh
git clone https://github.com/1solomonwakhungu/googlemcp
cd googlemcp
uv sync
uv run googlemcp products      # inspect enabled products offline
```

Or run directly without a local checkout (not yet published on PyPI):

```sh
uvx --from git+https://github.com/1solomonwakhungu/googlemcp googlemcp products
```

### 2. Create a Google Cloud project and enable APIs

```sh
gcloud auth login
gcloud projects create PROJECT_ID --name="GoogleMCP"
gcloud config set project PROJECT_ID
```

Enable the APIs for your chosen products with `gcloud services enable` ([docs/manual-steps.md](docs/manual-steps.md#enable-the-apis)) or the optional Terraform module ([docs/terraform.md](docs/terraform.md)).

### 3. Pick products, configure consent, and download a Desktop OAuth client

Export the same settings your MCP client will use so `scopes` and `auth login` match:

```sh
export GOOGLEMCP_PRODUCTS=tasks,calendar,gmail,drive
uv run googlemcp scopes        # exact OAuth scopes to paste in step 4 below
```

In the Google Auth Platform console (no public API exists to automate this; full walkthrough in [docs/manual-steps.md](docs/manual-steps.md)):

1. **Branding:** set app name and your email as support/developer contact.
2. **Audience:** choose **External**, add your Google account as a test user.
3. **Publishing status:** **Testing** expires refresh tokens after 7 days; **In production** (unverified, personal use) avoids weekly re-login ([docs/access-longevity.md](docs/access-longevity.md)).
4. **Data access:** paste the scopes printed by `uv run googlemcp scopes`.
5. **Clients:** create a **Desktop app** OAuth client and download its JSON once.

Because the app is your own unverified personal app, Google shows "Google hasn't verified this app" during sign-in. Click **Advanced**, then **Go to GoogleMCP (unsafe)**.

### 4. Sign in and verify

```sh
uv run googlemcp auth login --client-secrets ~/Downloads/client_secret_XXXX.json
uv run googlemcp auth status \
  && uv run googlemcp auth verify-client ~/Downloads/client_secret_XXXX.json \
  && rm ~/Downloads/client_secret_XXXX.json
uv run googlemcp doctor --probe   # one cheap read-only call per enabled product
```

`auth login` saves the client ID, client secret, and refresh token in your OS keyring (or Google Secret Manager: [docs/manual-steps.md](docs/manual-steps.md#5-desktop-oauth-client-for-googlemcp-auth-login)). The downloaded JSON is deleted only after verifying the stored client matches it.

### 5. Connect your MCP client

**Claude Code:**

```sh
claude mcp add --env GOOGLEMCP_PRODUCTS=tasks,calendar,gmail,drive \
  --transport stdio googlemcp -- uv run --directory /path/to/googlemcp googlemcp serve
```

**Claude Desktop / Cursor (`mcpServers` JSON):**

```json
{
  "mcpServers": {
    "googlemcp": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/googlemcp", "googlemcp", "serve"],
      "env": {
        "GOOGLEMCP_PRODUCTS": "tasks,calendar,gmail,drive"
      }
    }
  }
}
```

Full setup for Claude Desktop, Cursor, generic stdio clients: [docs/clients.md](docs/clients.md). Hermes Agent: [docs/hermes.md](docs/hermes.md). End-to-end verification runbook: [docs/acceptance.md](docs/acceptance.md).

To enable writes later (for example `GOOGLEMCP_WRITE_PRODUCTS=tasks`), export the updated setting, rerun `uv run googlemcp scopes`, add the new scopes on the Data access page, rerun `uv run googlemcp auth login`, and update the client `env` block.

## How this compares

| Server | Scope | Default safety | Transport & hosting |
|---|---|---|---|
| **GoogleMCP** | 19 products (Workspace + YouTube, GA4, Search Console, Photos Picker, Apps Script) | Read-only scopes & tools; per-product writes + separate destructive gate; OS keyring | Local stdio only; zero listening ports; single user |
| [taylorwilsdon/google_workspace_mcp](https://github.com/taylorwilsdon/google_workspace_mcp) | Workspace apps | Optional read-only flag & tool tiers | Local stdio or hosted HTTP / multi-user |
| [Google Workspace MCP servers](https://developers.google.com/workspace/guides/configure-mcp-servers) | Workspace apps (one remote server per product; Developer Preview) | Managed Google OAuth | Remote servers hosted by Google |

ChatGPT custom connectors require a public HTTPS server, so GoogleMCP intentionally does not target them; use ChatGPT's built-in Google connectors there.

## Access longevity

In **Testing** status, Google expires refresh tokens 7 days after consent. Switching your personal app to **In production** (unverified, personal use) removes the 7-day cap; tokens then last until revoked, unused for six months, displaced by the 100-tokens-per-client limit, or invalidated by a password change (when Gmail scopes are enabled). Re-authenticate anytime with `uv run googlemcp auth login`. Details: [docs/access-longevity.md](docs/access-longevity.md).

## Repository layout

```
catalog/services.json     Single source of truth for all 19 Google products
infra/terraform/          Optional API enablement, opt-in Secret Manager containers, state bucket, tests
src/googlemcp/            Local stdio MCP server: 19 adapters in services/, policy, auth, secretstore, CLI
scripts/                  Inventory generator and offline acceptance helpers
tests/                    Offline pytest + socket-guarded stdio e2e suite (tests/live requires GOOGLEMCP_LIVE=1)
docs/                     Setup, client, longevity, acceptance, and security documentation
```

Architecture: [docs/architecture.md](docs/architecture.md). Development & tests: [docs/development.md](docs/development.md). Contributing: [CONTRIBUTING.md](CONTRIBUTING.md). Changelog: [CHANGELOG.md](CHANGELOG.md).

## License

MIT. See [LICENSE](LICENSE).
