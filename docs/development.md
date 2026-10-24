# Development

## Setup

```sh
uv sync                      # runtime + dev dependencies
uv sync --extra secretmanager  # Secret Manager backend for the CLI (also needs ADC)
```

Python 3.12 or newer. All commands below run through `uv run`.

## Checks

Every change must pass, in this order:

```sh
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
gitleaks git .               # before pushing
```

Tests are offline. Anything that needs real Google credentials is marked
`@pytest.mark.live` and skipped unless `GOOGLEMCP_LIVE=1`.

## Layout of the core

| Module | Role |
|---|---|
| `config.py` | `Settings` from `GOOGLEMCP_*` env, an optional TOML file, and defaults. Never holds secrets. |
| `policy.py` | Active products, scope union, and which tools are exposed. |
| `secretstore/` | `SecretStore` protocol and backends: `keyring`, `file` (0600 JSON), `dir` (one file per key, read-only), `env` (read-only), `secretmanager` (versions only). |
| `auth/` | Login (installed-app flow, PKCE, loopback), status, revoke, and the refreshing `CredentialProvider`. |
| `context.py` | `GoogleServiceContext`: authorized discovery clients, retries, sanitized `UpstreamError`. |
| `server.py` | FastMCP assembly over stdio: policy-filtered tools, explicit annotations, sync tools in worker threads, error mapping. No network listener. |
| `cli.py` | The `googlemcp` command. |

Adapters live in `services/` and follow `docs/architecture.md`; they never
import FastMCP.

## Configuration

Precedence: command-line flags, then environment, then the TOML file
(`$GOOGLEMCP_CONFIG`, else `~/.config/googlemcp/config.toml`), then
defaults. See `.env.example` for every variable; the server does not read a
`.env` file, so export them or set them in your MCP client's `env` block. A
TOML example:

```toml
products = ["tasks", "calendar", "drive"]
write_products = ["tasks"]
allow_destructive = false
secret_backend = "keyring"
```

The server is local and stdio only. Settings left over from the removed HTTP
mode are rejected with a clear error wherever they appear (environment,
TOML, or keyword), so stale configuration cannot be silently ignored.

Policy rules:

- Read scopes are requested for each enabled product that has an adapter.
- Write tools and write scopes need the product in `write_products`.
- Destructive tools also need `allow_destructive = true`.
- Products whose catalog entry has `default_enabled = false` (for example
  `chat`, `meet`, `appsscript`) stay off unless listed in `products`.

`googlemcp products`, `googlemcp scopes`, and `googlemcp tools` show the
effect of the current configuration without touching Google.

## Secret keys

| Key | Used by |
|---|---|
| `oauth-client-id`, `oauth-client-secret` | Desktop OAuth client for `auth login` and token refresh |
| `refresh-token` | Your Google API refresh token |

Secret Manager IDs are `<GOOGLEMCP_SECRET_PREFIX>-<key>` and Terraform creates
the containers (optional; the default store is the OS keyring). The `dir` backend reads `<GOOGLEMCP_SECRETS_DIR>/<key>` or
`<GOOGLEMCP_SECRETS_DIR>/<key>/value` on every access. The `env` backend reads
`GOOGLEMCP_<KEY>` with dashes as underscores, for example
`GOOGLEMCP_REFRESH_TOKEN`.

No command prints a secret value.

`googlemcp auth status` exit codes: 0 healthy, 1 credentials missing,
rejected, or a required scope not granted, 2 configuration error, 3 token
refresh worked but Google could not confirm the granted scopes (retry).
`googlemcp doctor` reports unverified scopes as a failed check.

## Running locally

```sh
uv run googlemcp auth login --client-secrets ~/Downloads/client_secret_DESKTOP.json
uv run googlemcp doctor --probe
uv run googlemcp serve        # MCP over stdio; normally your MCP client starts this
```

Keep the downloaded client JSON until `googlemcp auth status` passes; step 5
of [manual-steps.md](manual-steps.md) shows the verified, gated deletion.

`serve` speaks MCP on stdin and stdout and logs to stderr, so running it by
hand mostly shows startup errors. To exercise it interactively, point an MCP
client at the same command (see [clients.md](clients.md)), or use FastMCP's
client:

```sh
uv run python - <<'PY'
import asyncio
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

async def main():
    transport = StdioTransport(command="uv", args=["run", "googlemcp", "serve"])
    async with Client(transport) as client:
        print([tool.name for tool in await client.list_tools()])

asyncio.run(main())
PY
```

## Testing patterns

- Adapters: `googlemcp.testing.FakeContext` with canned responses.
- Core: fakes in `tests/conftest.py` (`FakeAdapter`, `MemoryStore`,
  `NullContext`).
- Server: FastMCP's in-memory `Client(mcp)`, real `python -m googlemcp serve`
  stdio subprocesses, and guards that no HTTP app, auth provider, or route
  is ever constructed.
