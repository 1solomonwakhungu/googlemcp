# Google Tasks (local)

GoogleMCP runs on your machine and talks to your MCP client over stdio.
This page sets up Google Tasks only: which OAuth scopes each mode requests, the
settings that turn writes and deletes on, and how to check each step. It
assumes the base setup is done: the Tasks API enabled
([manual-steps.md](manual-steps.md#enable-the-apis)) and the consent screen
and Desktop OAuth client created ([manual-steps.md](manual-steps.md) steps 2
to 5).

## Tools and modes

| Tool | What it does | Mode needed |
|---|---|---|
| `tasks_list_tasklists` | List task lists | read-only (default) |
| `tasks_list_tasks` | List tasks in a list (`@default` is the default list); `show_completed=true` also returns tasks completed in Google's apps | read-only |
| `tasks_get_task` | Get one task | read-only |
| `tasks_create_task` | Create a task with title, notes, due date | write |
| `tasks_update_task` | Change title, notes, due date, or status (`needsAction` or `completed`); only the fields you pass change | write |
| `tasks_complete_task` | Mark a task completed | write |
| `tasks_delete_task` | Delete a task permanently | write and destructive |

Due dates take `YYYY-MM-DD` or an RFC 3339 timestamp. Google stores only the
date, so `2026-10-07` is sent as `2026-10-07T00:00:00.000Z`. Passing an empty
string to `tasks_update_task` clears the due date.

| Mode | Settings | Tasks tools exposed | OAuth scopes for Tasks |
|---|---|---|---|
| Read-only (default) | `GOOGLEMCP_PRODUCTS=tasks` | list task lists, list tasks, get task | `https://www.googleapis.com/auth/tasks.readonly` |
| Write | add `GOOGLEMCP_WRITE_PRODUCTS=tasks` | the three reads plus create, update, complete | `tasks.readonly` and `https://www.googleapis.com/auth/tasks` |
| Write and destructive | add `GOOGLEMCP_ALLOW_DESTRUCTIVE=true` | all of the above plus delete | same as write |

`GOOGLEMCP_ALLOW_DESTRUCTIVE=true` on its own does nothing for Tasks: delete
also needs Tasks in `GOOGLEMCP_WRITE_PRODUCTS`. The delete tool carries the
MCP `destructiveHint`, so a client can ask for confirmation before calling it.
Leaving `GOOGLEMCP_PRODUCTS` unset enables every default product; Tasks is
then still read-only unless listed in `GOOGLEMCP_WRITE_PRODUCTS`.

The same settings can live in `~/.config/googlemcp/config.toml`
(`products = ["tasks"]`, `write_products = ["tasks"]`,
`allow_destructive = true`); environment variables take precedence. See
[development.md](development.md) for the full configuration rules.

## Setup

1. Pick the mode and export its settings in the shell you will log in from.
   Use the same settings your MCP client will use.

   ```sh
   export GOOGLEMCP_PRODUCTS=tasks
   export GOOGLEMCP_WRITE_PRODUCTS=tasks        # write mode only
   export GOOGLEMCP_ALLOW_DESTRUCTIVE=true      # destructive mode only
   ```

2. Print the scopes this configuration requests:

   ```sh
   uv run googlemcp scopes
   uv run googlemcp tools      # the tools the server will expose
   ```

3. Add those scopes on the consent screen's Data access page
   ([manual step 4](manual-steps.md#4-google-auth-platform-data-access-scopes)).
   For Tasks that is `https://www.googleapis.com/auth/tasks.readonly`, plus
   `https://www.googleapis.com/auth/tasks` if you will ever enable writes.
   Neither is a restricted scope.

4. Authorize your Google account:

   ```sh
   uv run googlemcp auth login --client-secrets ~/Downloads/client_secret_XXXX.json
   ```

   After the first login the client is stored, so later logins drop
   `--client-secrets`. The command prints the scopes Google granted and lists
   any requested scope that was not granted.

### Changing modes later

- **Turning writes on** (read-only to write): the stored token only has
  `tasks.readonly`, so write calls fail until you consent again. Set
  `GOOGLEMCP_WRITE_PRODUCTS=tasks` and run `uv run googlemcp auth login`.
- **Turning writes off** (write to read-only): login uses incremental
  authorization (`include_granted_scopes=true`), so a new login keeps the
  `tasks` scope already granted. To narrow the token, revoke it first, then
  log in with the read-only settings:

  ```sh
  uv run googlemcp auth revoke
  unset GOOGLEMCP_WRITE_PRODUCTS GOOGLEMCP_ALLOW_DESTRUCTIVE
  uv run googlemcp auth login
  ```

  The server hides write tools as soon as your client restarts it with the
  new settings, even before the token is narrowed.
- `GOOGLEMCP_ALLOW_DESTRUCTIVE` changes no scopes, so toggling it needs no
  new login; restart the server from your client.

## Client entry

Use the client entry from [clients.md](clients.md) (or
[hermes.md](hermes.md)) with this `env` block, shown here in the JSON form
Claude Desktop and Cursor use:

```json
"env": {
  "GOOGLEMCP_PRODUCTS": "tasks",
  "GOOGLEMCP_WRITE_PRODUCTS": "",
  "GOOGLEMCP_ALLOW_DESTRUCTIVE": "false"
}
```

Set `GOOGLEMCP_WRITE_PRODUCTS` to `"tasks"` for write mode, and
`GOOGLEMCP_ALLOW_DESTRUCTIVE` to `"true"` as well to expose
`tasks_delete_task`.

`googlemcp serve` speaks MCP over stdio and logs to stderr only. The
refresh token comes from the same secret store `auth login` wrote to (the OS
keyring by default).

## Verification checklist

### 1. Offline (no Google account involved)

```sh
uv run pytest tests/test_tasks_local.py tests/test_tasks_adapter.py
```

`tests/test_tasks_local.py` builds the real server from `GOOGLEMCP_*`
settings and checks each mode: which Tasks tools are listed, their read-only
and destructive hints, and the scopes `googlemcp scopes` prints. It then calls
every Tasks tool through an MCP client and asserts the exact request that
would reach Google (method, URL, query, body), using canned responses.

### 2. Against your account, read-only

These read from the real account and change nothing.

```sh
export GOOGLEMCP_PRODUCTS=tasks
uv run googlemcp auth status
uv run googlemcp doctor --probe
```

Expect `[  ok] probe tasks: ok: task lists readable` from `doctor --probe`
(the probe calls `tasklists.list` with `maxResults=1`). Then either ask
your client "List my Google task lists" (it calls `tasks_list_tasklists`),
or run the live smoke test limited to Tasks:

```sh
GOOGLEMCP_LIVE=1 GOOGLEMCP_PRODUCTS=tasks uv run pytest tests/live -v -rs
```

### 3. Write check (optional, mutating)

This creates, changes, and deletes real data. Use a throwaway list so
nothing else is touched.

1. In Google Tasks (web or app), create a list named `GoogleMCP test`.
2. Set `GOOGLEMCP_WRITE_PRODUCTS=tasks` and
   `GOOGLEMCP_ALLOW_DESTRUCTIVE=true` in the client's `env`, run
   `uv run googlemcp auth login` with the same settings, and restart the
   server from your client.
3. Ask your client, one step at a time, naming the `GoogleMCP test` list:
   1. "List my task lists" and note the id of `GoogleMCP test`.
   2. "Create a task 'probe' due 2026-12-31 in that list" (`tasks_create_task`).
   3. "Rename it to 'probe updated' and add the note 'ok'" (`tasks_update_task`).
   4. "Mark it complete" (`tasks_complete_task`); check it shows as done in
      Google Tasks.
   5. "Delete it" (`tasks_delete_task`); a client that honors
      `destructiveHint` asks for confirmation first.
4. Delete the `GoogleMCP test` list in Google Tasks, then set
   `GOOGLEMCP_ALLOW_DESTRUCTIVE` back to `false` and, if you want read-only
   again, follow [Turning writes off](#changing-modes-later).
