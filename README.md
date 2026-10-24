# GoogleMCP

GoogleMCP is a local MCP server that gives an MCP client (Claude Desktop,
Claude Code, Cursor, Hermes Agent, or any other stdio client) scoped access
to your own Google account. It runs on your machine over stdio: it opens no
network listener, has no HTTP mode, and is not meant to be hosted. Google
API calls use a refresh token from your own Desktop OAuth client, kept in
the OS keyring by default.

- Read-only by default. Write tools need a per-product opt-in, and
  destructive tools (delete, trash, send) need a second opt-in.
- Covers Workspace apps and other Google APIs: Tasks, YouTube, Search
  Console, Google Analytics, Photos, Apps Script, and more (table below).
- Terraform enables the Google APIs. The OAuth consent screen and the
  Desktop OAuth client have no API, so you create them in the console.

Each person who uses GoogleMCP creates their own Google Cloud project and
OAuth client. There is no shared client and no central service.

Documentation index: [docs/README.md](docs/README.md).

## Products

`catalog/services.json` is the source of truth; Terraform and the server both
read it, and `tests/test_readme.py` checks this table against it. Scopes and
tools per product are in [docs/inventory.md](docs/inventory.md).

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

"Default on" means the product is enabled when `GOOGLEMCP_PRODUCTS` is unset,
still read-only. Gmail and Drive use restricted scopes, and YouTube Data has
a daily quota. Google removed the broad Photos Library scopes on 2025-03-31,
so the Library API reaches only media this app created; the Picker API reads
photos you select
([Photos API updates](https://developers.google.com/photos/support/updates)).
Google Tasks setup on its own: [docs/tasks.md](docs/tasks.md).

## Quick start

You need Python 3.12+, [uv](https://docs.astral.sh/uv/), the
[`gcloud` CLI](https://cloud.google.com/sdk/docs/install), and a Google
account. Terraform 1.9+ is optional (it is one of two ways to enable APIs).

### 1. Install

```sh
git clone https://github.com/1solomonwakhungu/googlemcp
cd googlemcp
uv sync
uv run googlemcp products      # what is enabled; no Google call
```

Or run it without a checkout (not published on PyPI):

```sh
uvx --from git+https://github.com/1solomonwakhungu/googlemcp googlemcp products
```

The Terraform module and `scripts/acceptance/verify_stored_client.py` live
in the repository, so clone it if you want either.

### 2. Create a Google Cloud project and enable the APIs

```sh
gcloud auth login
gcloud projects create PROJECT_ID --name="GoogleMCP"
gcloud config set project PROJECT_ID
```

Then enable the APIs for the products you want, either with Terraform
([docs/terraform.md](docs/terraform.md)) or with `gcloud services enable`
([docs/manual-steps.md](docs/manual-steps.md#enable-the-apis)).

### 3. Configure the consent screen and create a Desktop OAuth client

The product and write settings decide which scopes you add here and which
scopes `auth login` requests. Export the settings your MCP client will use
(step 5) in the shell you use for steps 3 and 4. Unset means the 15 default
products, read-only.

```sh
export GOOGLEMCP_PRODUCTS=tasks,calendar,gmail,drive
```

In the Google Auth Platform console
([docs/manual-steps.md](docs/manual-steps.md)):

1. Branding: app name and your email as support and developer contact.
2. Audience: user type **External**, and add your Google account as a test
   user.
3. Publishing status: in **Testing**, Google expires the refresh token 7
   days after consent. **In production** (unverified, personal use) avoids
   that; see [docs/access-longevity.md](docs/access-longevity.md).
4. Data access: add the scopes printed by `uv run googlemcp scopes`.
5. Clients: create a **Desktop app** client and download its JSON once.

Because the app is unverified and asks for sensitive and restricted scopes
(Gmail, Drive), Google shows a "Google hasn't verified this app" warning at
sign-in. You click **Advanced**, then **Go to GoogleMCP (unsafe)**.

### 4. Sign in

```sh
uv run googlemcp auth login --client-secrets ~/Downloads/client_secret_XXXX.json
uv run googlemcp auth status \
  && uv run python scripts/acceptance/verify_stored_client.py --json ~/Downloads/client_secret_XXXX.json \
  && rm ~/Downloads/client_secret_XXXX.json
uv run googlemcp doctor --probe          # one read-only call per enabled product
```

`auth login` stores the client ID, client secret, and refresh token in the
OS keyring. The downloaded file is deleted only after the store is confirmed
to hold exactly this client; the console never shows the full secret again.
To use Google Secret Manager instead, see
[docs/manual-steps.md](docs/manual-steps.md#5-desktop-oauth-client-for-googlemcp-auth-login).

### 5. Connect your MCP client

The client starts the server as a child process. Give it the same settings
you exported in step 3. For Claude Code:

```sh
claude mcp add --env GOOGLEMCP_PRODUCTS=tasks,calendar,gmail,drive \
  --transport stdio googlemcp -- uv run --directory /path/to/googlemcp googlemcp serve
```

To turn on writes later (for example `GOOGLEMCP_WRITE_PRODUCTS=tasks`),
export the new setting, rerun `uv run googlemcp scopes`, add the new scopes
on the consent screen's Data access page, run `uv run googlemcp auth login`
again, and then add the setting to the client entry.

Claude Desktop, Cursor, Hermes Agent, and a generic JSON entry:
[docs/clients.md](docs/clients.md). To check the whole setup end to end,
read-only: [docs/acceptance.md](docs/acceptance.md).

## Safety model

- **Local only.** `googlemcp serve` speaks MCP over stdin and stdout and logs
  to stderr. The only listener is the one-time `auth login` loopback
  redirect on 127.0.0.1.
- **Read-only by default.** Only read scopes are requested and only read
  tools are listed until a product is in `GOOGLEMCP_WRITE_PRODUCTS`.
- **Destructive tools need a second opt-in.** Delete, trash, and send tools
  also need `GOOGLEMCP_ALLOW_DESTRUCTIVE=true`, and carry the MCP
  `destructiveHint` so a client can ask before calling them.
- **Off-by-default products** (Meet, Chat, YouTube Reporting, Apps Script)
  must be listed in `GOOGLEMCP_PRODUCTS` explicitly.
- **Secrets stay out of the repository and Terraform state.** The CLI writes
  them to the keyring (or another backend); no command prints one. Google
  error text is passed to the model with URLs and credential-looking values
  redacted, and withheld entirely for 401 and 403.
- **Prompt injection is a real risk.** Email, documents, and calendar
  entries the model reads can contain instructions. Keep writes off for
  products you only need to read, and keep your client's tool-approval
  prompts on.

Threat model and CI scans: [docs/security.md](docs/security.md). To report a
vulnerability, see [SECURITY.md](SECURITY.md).

## Access longevity

While the publishing status is Testing, every refresh token expires 7 days
after consent. In production, the token lasts until you revoke it, it goes
unused for six months, it is displaced by the 100-tokens-per-client limit,
or (with Gmail scopes) you change your password. To recover, run
`uv run googlemcp auth login` again. Details:
[docs/access-longevity.md](docs/access-longevity.md).

## How this compares

- **Google's Workspace MCP servers**
  ([docs](https://developers.google.com/workspace/guides/configure-mcp-servers)):
  remote servers hosted by Google, one per product (Gmail, Drive, Calendar,
  and other Workspace apps), launched in 2026 and in Developer Preview as of
  October 2026. You need to join the Workspace Developer Preview Program.
- **[taylorwilsdon/google_workspace_mcp](https://github.com/taylorwilsdon/google_workspace_mcp)**:
  a community Python server for Workspace apps with stdio and HTTP
  transports, which can also be hosted for several users.
- **GoogleMCP** runs only as a local stdio process for one account, covers
  Google APIs outside Workspace (Tasks, YouTube, Search Console, Analytics,
  Photos, Apps Script), and starts read-only with per-product write and
  separate destructive opt-ins. It does not offer a remote or multi-user
  mode.

ChatGPT custom connectors need a server on public HTTPS, so GoogleMCP does
not work there; ChatGPT has its own built-in Google connectors.

## Repository layout

```
catalog/services.json     Product catalog shared by Terraform and the server
infra/terraform/          API enablement, opt-in secret containers, state bucket, tests
src/googlemcp/            Local stdio MCP server: adapters in services/, core, CLI
scripts/                  Inventory generator, offline pre-auth checks, stored-client verifier
tests/                    pytest suite (offline; tests/live runs only with GOOGLEMCP_LIVE=1)
docs/                     Setup, client, and design documentation
```

Design: [docs/architecture.md](docs/architecture.md). Development workflow
and checks: [docs/development.md](docs/development.md). Contributions:
[CONTRIBUTING.md](CONTRIBUTING.md). Changes: [CHANGELOG.md](CHANGELOG.md).

## License

MIT. See [LICENSE](LICENSE).
