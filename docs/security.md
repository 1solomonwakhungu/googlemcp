# Security

GoogleMCP is a local-only MCP server. It speaks MCP over stdio to a client
process on your machine and calls Google APIs as the account you signed in
with.
The server never opens a network listener. The only listener in the
project is the one-time `googlemcp auth login` flow, which waits on
127.0.0.1 for Google's OAuth redirect and exits. This page covers the
threat model, the rules for handling secrets, and how automated scans run.
Architecture decisions live in [architecture.md](architecture.md). To report
a vulnerability, see [SECURITY.md](../SECURITY.md).

## Deployment model and trust boundary

- **Transport:** stdio only. `googlemcp serve` reads MCP messages from stdin
  and writes them to stdout; logs go to stderr. There is no HTTP, SSE, or
  loopback listener. Environment settings left over from the former HTTP
  mode (transport, HTTP host and port, MCP auth, and email allowlist
  variables) are rejected at startup instead of being ignored.
- **Trust boundary:** the local OS user account and the MCP client process
  that spawns the server. Anything that can run code as that user can
  already read the user's keyring and files, so the server does not try to
  defend against it. There is no MCP-level authentication because there is
  no network surface to authenticate.
- **Google side:** one Google account (yours), authorized with your own
  Desktop (installed-app) OAuth client through a loopback
  redirect on 127.0.0.1 with PKCE (S256) (`googlemcp auth login`).
- **Not provided:** remote or public exposure. ChatGPT has its own built-in
  Google connectors; a ChatGPT custom MCP connector would need a public HTTPS
  server, which this project intentionally does not host.

## Threat model summary

| Asset | Threat | Primary controls |
|---|---|---|
| Your refresh token (access to every granted scope) | Leak through git, logs, CI, or Terraform state | Stored in the OS keyring by default (`secret_backend = "keyring"`); other local backends are a 0600 file, environment, mounted directory, or Secret Manager. Never in Terraform state: Terraform creates at most the Secret Manager containers, and values are written by the CLI. Access tokens live only in memory. gitleaks rules for Google token formats in pre-commit and CI |
| Desktop OAuth client secret | Leak through a committed `client_secret*.json` | `.gitignore` patterns, the `forbid-credential-files` pre-commit hook, gitleaks `google-oauth-client-secret` and `google-oauth-client-json` rules |
| Google account data | Unintended reads or writes by the model, including instructions injected through email, document, or calendar content the model reads | Least-privilege scopes per product; read-only by default (write tools and write scopes only for products listed in `write_products`); destructive tools (delete, trash, send) need `allow_destructive` as a second opt-in; products marked `default_enabled=false` stay off unless listed; the MCP client's own tool-approval prompts |
| Credentials in tool output | A token or secret echoed to the model | Tool errors carry the HTTP status and Google's reason code. For 401 and 403, Google's free-text message is withheld and replaced by a fixed recovery hint; for other statuses it is shown truncated, with URLs and credential-looking parameters redacted. The request URI, raw response bodies, and local exception text are never included; unexpected exceptions are masked; covered by `tests/e2e/test_e2e_errors.py` |
| Google quota | Runaway tool loops | Page and row caps in every adapter; YouTube search capped per call and per process |
| CI pipeline | A compromised action or tool exfiltrating secrets | Actions pinned to full commit SHAs, `permissions: contents: read`, `persist-credentials: false`, downloaded binaries verified by SHA-256, no repository secrets used by CI |
| Dependencies | Known-vulnerable Python packages | `uv.lock` with hashes; `pip-audit --require-hashes` on the locked set in CI |

Out of scope: a compromised Google account or workstation, malware running
as your OS user, and Google-side incidents.

## Secret handling rules

1. Never commit OAuth client secrets, refresh tokens, access tokens, API keys,
   downloaded credential JSON, `*.tfstate`, `*.tfvars` with real values,
   `.terraform/`, or `.claude/settings.local.json`. Commit only
   `*.tfvars.example` files with placeholders.
2. The server stores exactly three secrets: `oauth-client-id`,
   `oauth-client-secret`, and `refresh-token`. If Google rotates the refresh
   token, the server writes the new one back to a writable store.
3. Terraform creates Secret Manager containers for those three keys only
   when `enable_secret_manager = true` (for the optional `secretmanager`
   backend). It grants no IAM: the project Owner role already has access. Secret
   versions are added with the CLI or `gcloud secrets versions add`, so
   values never enter Terraform state or plan output.
4. The file backend refuses any secret file readable by group or other.
   Rotate its contents before fixing the mode, because they may have leaked.
5. CI uses no repository secrets. The only token in CI is the default
   read-only `GITHUB_TOKEN`, passed to `tflint --init` to download its plugin.
6. If a secret is committed: revoke it at the source first (Google Auth
   Platform for client secrets, `https://myaccount.google.com/permissions`
   for refresh tokens), then remove it from history. Rotation comes before
   cleanup, because pushed history must be treated as public.

## Adapter-level controls (media and analytics)

- YouTube: `youtube.readonly` for reads. Google authorizes
  `commentThreads.list` only with `youtube.force-ssl`, which also grants
  account management, so the comment reader is registered as a write tool and
  appears only when YouTube writes are enabled. `search.list` costs 100 quota
  units: capped at 25 results per call and 20 calls per server process.
- YouTube Reporting: off by default in the catalog because `create_job`
  starts a server-side daily job. It is marked as a write tool although
  Google authorizes it with `yt-analytics.readonly`.
- Google Analytics and Search Console: read-only scopes only; report rows
  capped at 1000 per call; metric, dimension, and date inputs validated
  before any request.
- Google Photos: only `photoslibrary.readonly.appcreateddata`,
  `photospicker.mediaitems.readonly`, and (writes) `photoslibrary.appendonly`.
  The removed full-library scopes are not requested (see
  `developers.google.com/photos/support/updates`). Uploads are base64,
  capped at 20 MB, limited to `image/*` and `video/*`, and filenames with
  path separators are rejected. IDs placed in REST paths are restricted to
  `[A-Za-z0-9_-]` to prevent path injection. Auth and network failures are
  mapped to fixed messages without the original exception text.

## How scans and tests run

Workflows in `.github/workflows/` run on every push to `main`, every pull
request, and on manual dispatch.

| Workflow / job | Tool | What fails the build |
|---|---|---|
| `ci` / `python` | ruff, ruff format, mypy (strict), pytest with coverage | Any lint, format, or type error; any failing test; coverage under 85% |
| `ci` / `dependency-audit` | pip-audit on `uv export --locked` with `--require-hashes` | Any known vulnerability in runtime, extra, or dev dependencies |
| `ci` / `terraform` | `terraform fmt -check`; `init -backend=false` and `validate` for the root and `bootstrap` configurations; `test`; tflint with `infra/terraform/.tflint.hcl` | Any finding. Skipped with a notice when `infra/terraform` has no `.tf` files; `bootstrap` and tflint are skipped when absent. `init` uses `-lockfile=readonly` when a lock file is committed |
| `ci` / `iac-scan` | Checkov (`.checkov.yaml`: Terraform and GitHub Actions frameworks) | Any failed policy |
| `ci` / `secrets` | gitleaks over full git history (`.gitleaks.toml`) | Any finding |
| `ci` / `workflow-lint` | actionlint (with shellcheck, preinstalled on the runner) | Any finding |
| `e2e` | `pytest tests/e2e` | Any failure. On `main`, `E2E_REQUIRE_ALL_ADAPTERS=1` also fails if a catalog product has no adapter |
| `shellcheck` | shellcheck v0.11.0 on tracked `scripts/**/*.sh` and `infra/terraform/scripts/*.sh` | Any finding at style severity or above |

### End-to-end tests

`tests/e2e` runs the real server code with every adapter and a fake Google:

- In-process suites (`test_e2e_policy.py`, `test_e2e_tools.py`,
  `test_e2e_errors.py`) use the real `GoogleServiceContext` and
  `CredentialProvider` with a routed fake transport. They check tool
  exposure under each policy, one read tool per default-enabled product, and
  error mapping without credential leaks.
- `test_e2e_stdio.py` spawns the real `googlemcp serve` process over stdio
  through `stdio_launcher.py`, which refuses and logs calls to the guarded
  socket methods (`connect`, `connect_ex`, `bind`, `listen`, `sendto`,
  `sendmsg`) and name functions (`create_connection`, `getaddrinfo`,
  `gethostbyname`, `gethostbyname_ex`, `gethostbyaddr`, `getnameinfo`); a
  `--probe` mode proves each one is caught. Direct use of the C `_socket`
  module and unlisted socket APIs are out of scope for the guard. With fake
  credentials in a temporary 0600 file store, the process must initialize
  and list the default read-only tools with zero guarded calls. Every
  removed HTTP-era setting must stop startup with exit code 2 and a "no
  longer supported" message. A real tool call is used only to prove the
  guard catches network access, because a subprocess cannot be given the
  in-process fake Google.

### Why Checkov instead of Trivy

Both scan Terraform for Google Cloud misconfigurations. Checkov was chosen
because:

1. It installs as a pinned Python package through `uvx`, matching the
   project's existing toolchain and avoiding another third-party action.
2. On 2026-03-19 an attacker force-pushed 76 of 77 `aquasecurity/trivy-action`
   tags and every `aquasecurity/setup-trivy` tag to credential-stealing
   commits (GHSA-69fq-xp46-6x23, CVE-2026-33634). SHA pinning would mitigate
   that, but the project avoids the dependency entirely.
3. Its GitHub Actions framework also checks the workflow files.

To suppress a Checkov finding, add an inline
`# checkov:skip=CKV_GCP_NN:<reason>` on the resource, never a global skip.

### Gitleaks rules

`.gitleaks.toml` extends the upstream default rules with four Google OAuth
formats: client secrets (`GOCSPX-`), refresh tokens (`1//0`), access tokens
(`ya29.`), and downloaded client JSON (`"client_secret": "..."`). In a local
test against synthetic values, the default rules alone caught one of the four
formats (as `generic-api-key`) and the extended config caught all four. Test
fixtures use deliberately short fake tokens so the rules stay quiet.

### Pinning and updates

- Actions are pinned to full commit SHAs with the tag in a trailing comment.
  To update one, resolve the new tag with
  `gh api repos/<owner>/<repo>/commits/<tag> --jq .sha` and replace both the
  SHA and the comment.
- gitleaks, actionlint, and shellcheck binaries are downloaded from GitHub
  releases and checked against the SHA-256 values in the workflow `env`
  blocks. Take new values from the release's checksums file or asset digest.
- Checkov, Terraform, and tflint are pinned by version in the workflow
  `env` block. The tflint google plugin is pinned in
  `infra/terraform/.tflint.hcl`.

## Local checks

```bash
uv sync --all-extras
uv run --with pre-commit pre-commit install
uv run --with pre-commit pre-commit run --all-files
E2E_REQUIRE_ALL_ADAPTERS=1 uv run pytest tests/e2e
```

The pre-commit hooks (`.pre-commit-config.yaml`, pinned to commit SHAs) mirror
the fast CI checks: ruff, ruff format, mypy, gitleaks, YAML and TOML checks,
private key detection, `terraform fmt`, a block on credential and state
filenames, and the project's rule against em dashes in tracked text. pip-audit, Checkov,
`terraform test`, and tflint run in CI. To run them locally:

```bash
uv export --locked --all-extras --all-groups --no-emit-project \
  --format requirements-txt -o /tmp/req.txt
uv run pip-audit --strict --disable-pip --require-hashes -r /tmp/req.txt
uvx --from checkov==3.3.21 checkov --config-file .checkov.yaml --directory .
gitleaks git --config .gitleaks.toml --redact .
tflint --config="$PWD/infra/terraform/.tflint.hcl" --init
tflint --config="$PWD/infra/terraform/.tflint.hcl" --chdir=infra/terraform --recursive
```
