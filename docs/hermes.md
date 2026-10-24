# Using GoogleMCP from Hermes Agent

GoogleMCP is a local MCP server. Hermes Agent starts it as a child process
and talks to it over stdio; there is no network listener and nothing to host.
Google API calls use your own refresh token, kept in a local secret store.

Hermes Agent reads MCP servers from `~/.hermes/config.yaml` under the
`mcp_servers` key. A stdio server uses `command`, `args`, and `env`. Secrets
for Hermes itself belong in `~/.hermes/.env` and are referenced as `${VAR}`;
GoogleMCP needs none there because it reads its own secret store. After
editing, run `/reload-mcp` in Hermes. These keys were checked against Hermes
Agent v2026.9.24. Other clients: [clients.md](clients.md).

## 1. Choose products and permissions

Decide this before you sign in. The settings decide which OAuth scopes
`auth login` requests, and the server can only use scopes you granted.

- `GOOGLEMCP_PRODUCTS`: comma-separated catalog keys. Unset means the catalog
  defaults. `uv run googlemcp products` lists them.
- `GOOGLEMCP_WRITE_PRODUCTS`: products whose write tools (and write scopes)
  are enabled, or `all`.
- `GOOGLEMCP_ALLOW_DESTRUCTIVE=true`: additionally exposes delete, trash, and
  send tools for write-enabled products.

Export the same values you will put in the Hermes `env` block (section 3) in
the shell you will sign in from. This page uses Tasks, Calendar, Gmail, and
Drive, with writes for Tasks:

```sh
export GOOGLEMCP_PRODUCTS=tasks,calendar,gmail,drive
export GOOGLEMCP_WRITE_PRODUCTS=tasks        # leave unset for read-only
uv run googlemcp scopes     # the OAuth scopes this selection needs
uv run googlemcp tools      # the tools Hermes will see
```

## 2. Authorize your Google account once

1. Complete the Google Cloud console steps in
   [manual-steps.md](manual-steps.md) (consent screen, test user, Desktop
   OAuth client). On the Data access page, add the scopes
   `uv run googlemcp scopes` printed in section 1.
2. In the same shell, with the section 1 settings still exported, store and
   verify the Desktop client and your refresh token exactly as
   [step 5 of manual-steps.md](manual-steps.md#5-desktop-oauth-client-for-googlemcp-auth-login)
   describes. That step runs `uv run googlemcp auth login --client-secrets <file>`
   and deletes the downloaded client JSON only after `uv run googlemcp auth status`
   and the stored-client check both pass, so a failed login never loses the
   only copy of the client secret. Keep the JSON until then.
3. Confirm everything Hermes needs is in place:

   ```sh
   uv run googlemcp doctor
   ```

`auth login` opens a browser on a loopback redirect, stores the Desktop
client ID and secret plus your refresh token in the secret store below, and
never writes them into the repository.

If you change products or writes later, export the new values, rerun
`uv run googlemcp scopes`, add the new scopes on the consent screen's Data
access page, and run `uv run googlemcp auth login` again before you update
the Hermes `env` block.

### Where the secrets live

| Backend | Select with | Notes |
|---|---|---|
| `keyring` (default) | nothing | macOS Keychain, Windows Credential Locker, or Secret Service |
| `file` | `GOOGLEMCP_SECRET_BACKEND=file` | `~/.config/googlemcp/secrets.json`, created 0600; refused if group or others can read it |
| `dir` | `GOOGLEMCP_SECRET_BACKEND=dir` | read-only, one file per key under `~/.config/googlemcp/secrets.d` |
| `env` | `GOOGLEMCP_SECRET_BACKEND=env` | read-only, `GOOGLEMCP_OAUTH_CLIENT_ID`, `GOOGLEMCP_OAUTH_CLIENT_SECRET`, `GOOGLEMCP_REFRESH_TOKEN` |
| `secretmanager` | `GOOGLEMCP_SECRET_BACKEND=secretmanager` plus `GOOGLEMCP_GCP_PROJECT` | Google Secret Manager through your own `gcloud auth application-default login`; needs `uv sync --extra secretmanager` |

The keys are `oauth-client-id`, `oauth-client-secret`, and `refresh-token`.
`uv run googlemcp secret list` shows which are present without printing
values.

## 3. Configure Hermes

Use the same settings you exported in section 1.

```yaml
# ~/.hermes/config.yaml
mcp_servers:
  google:
    command: uv
    args:
      - run
      - --directory
      - /path/to/googlemcp
      - googlemcp
      - serve
    env:
      GOOGLEMCP_PRODUCTS: "tasks,calendar,gmail,drive"
      GOOGLEMCP_WRITE_PRODUCTS: "tasks"
      GOOGLEMCP_ALLOW_DESTRUCTIVE: "false"
```

`GOOGLEMCP_WRITE_PRODUCTS: "tasks"` needs the Tasks write scope. If you
signed in without it, rerun `uv run googlemcp scopes` with that setting
exported, add the new scopes on the consent screen's Data access page, and
run `uv run googlemcp auth login` again. Set it to `""` for read-only.

For the file backend add `GOOGLEMCP_SECRET_BACKEND: "file"` to `env`. To run
without a checkout, use `command: uvx` and
`args: [--from, "git+https://github.com/1solomonwakhungu/googlemcp", googlemcp, serve]`.
With that install, run the CLI commands on this page as
`uvx --from git+https://github.com/1solomonwakhungu/googlemcp googlemcp ...`
instead of `uv run googlemcp ...`.

Run `/reload-mcp` in Hermes and ask it to list your Google task lists.

The server writes logs to stderr only, so stdout stays clean for the MCP
protocol. If macOS asks whether `python` may read the `googlemcp` keychain
items, choose Always Allow.

Settings left over from the removed HTTP mode (transport, host, port, or MCP
access-control variables) are rejected at startup with an error naming the
variable, so remove any such entries from the `env` block.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Tool error mentions `invalid_grant` | The refresh token expired or was revoked. Run `uv run googlemcp auth login`. If the consent screen is in Testing, tokens expire after 7 days; see [access-longevity.md](access-longevity.md). |
| Tool error mentions a missing scope | You enabled a product or writes after logging in. Rerun `uv run googlemcp scopes`, add the new scopes on the consent screen's Data access page, and run `uv run googlemcp auth login` again with the same settings. |
| Hermes shows the server as failed to start | Run the same command in a terminal (`uv run --directory /path/to/googlemcp googlemcp serve`); configuration errors print to stderr and exit with status 2. |
| A product has no tools | `uv run googlemcp products` shows whether it is enabled and has an adapter. |
| Anything else | `uv run googlemcp doctor --probe` makes one cheap read per product and reports what failed. |
