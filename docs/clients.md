# Connecting MCP clients

GoogleMCP is a stdio MCP server. Every client starts the same command as a
child process and talks to it over stdin and stdout; there is no URL, port,
or token to configure. This page gives the entry for several clients. For
Hermes Agent, see [hermes.md](hermes.md).

## Before you connect

1. Choose products and write settings first (see [Settings](#settings)).
   They decide which OAuth scopes `auth login` requests, and the server can
   only use scopes you granted. Export the same `GOOGLEMCP_*` values you will
   put in the client entry in the shell you will sign in from:

   ```sh
   export GOOGLEMCP_PRODUCTS=tasks,calendar,gmail,drive
   uv run googlemcp scopes      # add these on the consent screen's Data access page
   ```

2. In that shell, finish the Google Cloud setup and sign in once
   ([manual-steps.md](manual-steps.md)). The server never runs the OAuth
   flow itself; it reads the refresh token that `uv run googlemcp auth login`
   stored.
3. Check what the client will see:

   ```sh
   uv run googlemcp tools       # tools for the current settings
   uv run googlemcp doctor      # secret store, token, and scope checks
   ```

If you enable a product or writes later, export the new values, rerun
`uv run googlemcp scopes`, add the new scopes on the consent screen's Data
access page, and run `uv run googlemcp auth login` again before you change
the client entry.

With the `uvx` install, replace `uv run googlemcp` in these commands with
`uvx --from git+https://github.com/1solomonwakhungu/googlemcp googlemcp`.

## The server command

From a checkout (`git clone` and `uv sync`):

```sh
uv run --directory /path/to/googlemcp googlemcp serve
```

Without a checkout, uv fetches and caches the package from GitHub:

```sh
uvx --from git+https://github.com/1solomonwakhungu/googlemcp googlemcp serve
```

Append `@<tag or commit>` to the `git+https` URL to pin a version.

Desktop apps often start with a shorter `PATH` than your shell and may not
find `uv`. If the client reports that the command was not found, use the
absolute path printed by `command -v uv` (or `command -v uvx`).

## Settings

The server reads `GOOGLEMCP_*` environment variables, which every client
below can set in an `env` block. Environment variables override the
optional TOML file (`$GOOGLEMCP_CONFIG`, else
`~/.config/googlemcp/config.toml`; see [development.md](development.md)).

| Variable | Default | Meaning |
|---|---|---|
| `GOOGLEMCP_PRODUCTS` | catalog defaults | Comma-separated product keys to enable (`uv run googlemcp products` lists them) |
| `GOOGLEMCP_WRITE_PRODUCTS` | empty | Products whose write tools and write scopes are enabled, or `all` |
| `GOOGLEMCP_ALLOW_DESTRUCTIVE` | `false` | Also expose delete, trash, and send tools for write-enabled products |
| `GOOGLEMCP_SECRET_BACKEND` | `keyring` | `keyring`, `file`, `dir`, `env`, or `secretmanager` |
| `GOOGLEMCP_SECRET_FILE` | `~/.config/googlemcp/secrets.json` | File for the `file` backend |
| `GOOGLEMCP_SECRETS_DIR` | `~/.config/googlemcp/secrets.d` | Directory for the `dir` backend |
| `GOOGLEMCP_GCP_PROJECT` | unset | Project for the `secretmanager` backend |
| `GOOGLEMCP_SECRET_PREFIX` | `googlemcp` | Secret Manager ID prefix |
| `GOOGLEMCP_MAX_RETRIES` | `4` | Retries for Google API calls (0 to 10) |
| `GOOGLEMCP_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, or `ERROR`; logs go to stderr |
| `GOOGLEMCP_CONFIG` | unset | Path to a TOML settings file |

The examples below enable Tasks, Calendar, Gmail, and Drive, read-only.
To let the client create and update tasks, add
`"GOOGLEMCP_WRITE_PRODUCTS": "tasks"`. That setting needs the Tasks write
scope: export it, rerun `uv run googlemcp scopes`, add the new scopes on the
consent screen's Data access page, and run `uv run googlemcp auth login`
again.

## Claude Desktop

Edit `claude_desktop_config.json` (Settings, Developer, Edit Config). On
macOS it is in `~/Library/Application Support/Claude/`; on Windows, in
`%APPDATA%\Claude\`.

```json
{
  "mcpServers": {
    "googlemcp": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/googlemcp", "googlemcp", "serve"],
      "env": {
        "GOOGLEMCP_PRODUCTS": "tasks,calendar,gmail,drive",
        "GOOGLEMCP_WRITE_PRODUCTS": "",
        "GOOGLEMCP_ALLOW_DESTRUCTIVE": "false"
      }
    }
  }
}
```

Restart Claude Desktop after saving.

## Claude Code

```sh
claude mcp add --env GOOGLEMCP_PRODUCTS=tasks,calendar,gmail,drive \
  --transport stdio --scope user googlemcp \
  -- uv run --directory /path/to/googlemcp googlemcp serve
```

Options for `claude mcp add` go before the server name and `--`; the server
command goes after `--`. `--scope user` makes the server available in every
project; leave it out to add it to the current project only. Check the
connection with `claude mcp get googlemcp` or `/mcp` inside Claude Code.

## Cursor

Add the entry to `.cursor/mcp.json` in a project, or to `~/.cursor/mcp.json`
for every project:

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

The server then appears under Cursor's MCP settings, where you can enable
or disable it.

## Other stdio clients

Any client that launches stdio servers needs three things:

- **command:** `uv` (or `uvx`), as an absolute path if the client's `PATH`
  lacks it.
- **args:** `run --directory /path/to/googlemcp googlemcp serve`, or
  `--from git+https://github.com/1solomonwakhungu/googlemcp googlemcp serve`
  for `uvx`.
- **env:** the `GOOGLEMCP_*` settings above.

Many clients accept the same `mcpServers` JSON shape shown for Claude
Desktop and Cursor.

## Troubleshooting

| Symptom | Fix |
|---|---|
| The client says the server failed to start | Run the same command in a terminal. Configuration errors print to stderr and exit with status 2. |
| `command not found` or `spawn uv ENOENT` | Use the absolute path to `uv` as `command`. |
| A tool error mentions `invalid_grant` | The refresh token expired or was revoked. Run `uv run googlemcp auth login`. In Testing mode tokens expire after 7 days; see [access-longevity.md](access-longevity.md). |
| A tool error mentions a missing scope | You enabled a product or writes after signing in. Rerun `uv run googlemcp scopes`, add the new scopes on the consent screen's Data access page, and run `uv run googlemcp auth login` again with the same settings. |
| macOS asks whether `python` may read the `googlemcp` keychain items | Choose Always Allow. |
| A setting such as `GOOGLEMCP_HTTP_PORT` is rejected | Settings from the removed HTTP mode stop startup; delete them from `env`. |
| Anything else | `uv run googlemcp doctor --probe` makes one cheap read per product and reports what failed. |

## ChatGPT

ChatGPT custom MCP connectors need a server on public HTTPS. GoogleMCP runs
only as a local stdio process, so it cannot be added there. ChatGPT has its
own built-in connectors for Gmail, Google Calendar, Google Contacts, and
Google Drive, depending on plan and region.
