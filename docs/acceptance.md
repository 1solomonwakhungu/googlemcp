# Acceptance runbook

This runbook takes you from a fresh checkout to a verified local setup in
about 15 minutes once the console steps are done. Replace `you@example.com`
with your Google account. GoogleMCP runs on your machine over stdio only.
Every check here is read-only: nothing is created, sent, or deleted in the Google account.
The optional Tasks write check is the one exception and lives in
[tasks.md](tasks.md).

Work through the parts in order. Each step lists what to run and what you
should see. If a step does not match, stop and use the linked recovery notes.

| Part | What it proves | Needs Google sign-in |
|---|---|---|
| A. Automated pre-auth checks | Code, Terraform, and secret hygiene | No |
| B. Console steps | Consent screen, scopes, Desktop client | Console only |
| C. Login | Refresh token and client stored, file safely deleted | Yes, once |
| D. Status and probes | Token works; each product API answers | No (uses the token) |
| E. Live read-only suite | Each product's read tool works through the real server | No (uses the token) |
| F. MCP client over stdio | A real MCP client can call a read tool | No |
| G. Longevity check | The token survives past 7 days | No |
| H. Revoke and narrow | Access can be removed or reduced cleanly | No |

## Pick one secret store first

Every `googlemcp` command, the stored-client verifier, and the live suite read
the same secret store. Pick it once, in the shell you use for the whole
runbook:

- **Local keyring** (default; macOS Keychain, Windows Credential Locker, or
  Secret Service): nothing to set.
- **Secret Manager** (optional): apply Terraform with
  `enable_secret_manager = true` so the three secret containers exist
  ([terraform.md](terraform.md)), then:

  ```sh
  export GOOGLEMCP_SECRET_BACKEND=secretmanager
  export GOOGLEMCP_GCP_PROJECT=PROJECT_ID
  ```

  Keep both exported for parts C through H. Without them a command silently
  uses the keyring instead and reports a missing token. The command line flag
  works too, but it is global and must come before the subcommand:
  `googlemcp --secret-backend secretmanager auth status`, not
  `googlemcp auth status --secret-backend secretmanager`.

## A. Automated pre-auth checks (no credentials)

```sh
scripts/acceptance/preauth.sh
```

The script runs the unit tests, ruff, mypy, the inventory freshness check,
a CLI smoke test (`googlemcp --help`, `products`, `scopes`), Terraform's
offline verification (`infra/terraform/scripts/verify.sh`), a gitleaks scan,
and the latest CI run on `main` (through `gh`, for your clone's GitHub
repository). It prints a table like:

```
CHECK                                RESULT DETAIL
unit tests (pytest)                  PASS  ... passed, ... skipped in 8.2s
cli smoke (help, products, scopes)   PASS  https://www.googleapis.com/auth/...
terraform verify                     PASS  verify: OK
secret scan (gitleaks)               PASS  ... no leaks found
ci on main                           PASS  latest run: success
preauth: PASS
```

`SKIP` is allowed only for tools you have not installed (terraform, gitleaks,
gh), for a CI system that has not run yet, and for `--no-terraform` or
`--no-ci`. Any `FAIL` stops here; the table names the log file. The script
never reads the secret store, never calls Google, and keeps the live suite
switched off.

## B. Console steps (once)

Follow [manual-steps.md](manual-steps.md). The steps that matter for
acceptance:

1. [Audience](manual-steps.md#3-google-auth-platform-audience-and-test-user):
   user type External, your account (`you@example.com`) listed as a test
   user, and publishing status **In production** (click **Publish app**). Do this
   before the login in part C. A login made while the app is still in Testing
   issues a refresh token that expires 7 days after consent; see
   [access-longevity.md](access-longevity.md).
2. [Data access](manual-steps.md#4-google-auth-platform-data-access-scopes):
   paste the output of `uv run googlemcp scopes`. Gmail and Drive scopes
   appear under restricted scopes; that is expected for a personal app.
   [inventory.md](inventory.md) lists every scope with its sensitivity class.
3. [Desktop client](manual-steps.md#5-desktop-oauth-client-for-googlemcp-auth-login):
   create it and download the JSON once. The console never shows the full
   secret again, so keep that file until part C confirms it is stored.
4. Product prerequisites in [manual-steps.md](manual-steps.md): note which
   products have no data yet (no YouTube channel, no GA4 property, no Search
   Console site). Their reads still succeed with empty results; YouTube
   Analytics is skipped without a channel.

The product APIs must be enabled in the project, with Terraform
([terraform.md](terraform.md)) or `gcloud`
([manual-steps.md](manual-steps.md#enable-the-apis)).

## C. Login

```sh
CLIENT_JSON=~/Downloads/client_secret_XXXX.json
uv run googlemcp auth login --client-secrets "$CLIENT_JSON" --login-hint you@example.com
```

Expected in the browser:

1. Google account chooser: pick `you@example.com`.
2. **"Google hasn't verified this app"**. This is expected for an unverified
   personal app. Click **Advanced**, then **Go to GoogleMCP (unsafe)**.
3. The consent screen lists the requested access. Leave every box checked
   (an unchecked box leaves that product's tools failing with a missing-scope
   error). Do not choose a time limit.
4. The browser shows a success page; the terminal prints
   `Stored refresh token in the keyring store.` (or `secretmanager`) and
   `Granted N scopes.`, with no "Not granted" list on stderr.

If macOS asks whether `python` may access the `googlemcp` keychain items,
choose **Always Allow**.

Delete the downloaded client JSON only after the store is confirmed to hold
exactly this client. Login can fail before the client is stored, and
`auth status` alone is not proof: an older stored client (for example from
before a secret rotation) can still refresh the token.
`scripts/acceptance/verify_stored_client.py` compares the stored
`oauth-client-id` and `oauth-client-secret` with the file without printing
them, so the `rm` runs only when everything passes:

```sh
uv run googlemcp auth status \
  && uv run python scripts/acceptance/verify_stored_client.py --json "$CLIENT_JSON" \
  && rm "$CLIENT_JSON"
```

The verifier reads the store chosen at the top (or pass
`--secret-backend NAME`). It exits `0` on a match, `1` when a value is
missing or different, and `2` when the file is not a Desktop client JSON. If
any command fails, keep the file, fix what it reports, and run the login
again with the same file.

## D. Status and probes

```sh
uv run googlemcp auth status
echo "exit $?"
```

Expected output, exit `0`:

```
secret backend:      keyring
oauth client stored: yes
refresh token:       present
token refresh:       ok
access token expiry: ...
granted scopes:      N
```

Exit codes: `0` healthy; `1` the client or token is missing, the refresh
failed, or a `missing scope:` line is printed; `3` the token refreshes but
Google's tokeninfo check could not confirm the granted scopes (the output
shows `scopes:              UNVERIFIED`). Treat `3` as not accepted: rerun,
and if it persists, check network access to Google.

```sh
uv run googlemcp doctor --probe
```

`doctor --probe` makes one cheap read-only call per enabled product. Each
line reads `[status] probe <product>: <probe status>: <detail>`, where
`[  ok]` passes, `[warn]` is allowed, and `[fail]` fails the run (exit `1`).
Expected for the default product set:

| Product | Expected line |
|---|---|
| tasks | `[  ok] probe tasks: ok: task lists readable` |
| gmail | `[  ok] probe gmail: ok: profile readable ...` |
| calendar | `[  ok] probe calendar: ok: calendar list readable` |
| drive | `[  ok] probe drive: ok: drive about readable` |
| docs, sheets, slides, forms | `[  ok] probe docs: ok: API reachable (probe id not found, as expected)`; these probes read an id that cannot exist, and only a 404 counts as proof |
| people | `[  ok] probe people: ok: own profile readable` |
| driveactivity | `[  ok] probe driveactivity: ok: activity readable` |
| youtube | `[  ok] probe youtube: ok: channel lookup ok`, plus ` (no channel on this account)` if there is none |
| youtube_analytics | `[  ok] probe youtube_analytics: ok: analytics groups readable` |
| analytics | `[  ok] probe analytics: ok: admin and data APIs readable` |
| searchconsole | `[  ok] probe searchconsole: ok: sites readable` |
| photos | `[  ok] probe photos: ok: library albums readable (Picker API not probed: it requires creating a session)` |
| appsscript, youtube_reporting, meet, chat | Not listed; off by default |

Meet and Chat need a Google Workspace account. If you enable them on a
personal @gmail.com account, expect one of:

- `[warn] ... unsupported_account: this API requires a Google Workspace account (Google rejected this account)`
  when Google's error says the API is "only available to Google Workspace
  users". This is the documented consumer-account answer and is allowed.
- `[fail] ... permission: permission denied; Meet/Chat REST APIs require a Google Workspace account, which may be the cause`
  for any other 403. This fails the run because a 403 alone does not prove
  the account type; disable the product or investigate.
- Chat only: `[fail] ... not_enabled: Chat app not configured in the Google Cloud console (Chat API Configuration)`.

Other statuses:

| Status | Meaning | Fix |
|---|---|---|
| `not_enabled` | The API is not enabled in the project | Enable the product in Terraform and apply |
| `permission` | The token lacks a scope, or Google denied access | Update the data access page (part B.2), then revoke and log in again (part H) |
| `auth_error` | The refresh token no longer works | Run part C again; see access-longevity.md |
| `error` | Anything else, including rate limits | Rerun once; then check the server logs |

## E. Live read-only suite

```sh
GOOGLEMCP_LIVE=1 uv run pytest tests/live -v -rs
```

The suite builds the real MCP server in-process (the same tools a client gets
over stdio, through fastmcp's in-memory client) with writes and destructive
tools forced off, checks that every exposed tool carries the read-only hint,
and calls one read tool per default-enabled product (for example
`tasks_list_tasklists`, `gmail_list_labels`, `drive_search_files`,
`youtube_list_my_channels`). It reads the store chosen at the top of this
runbook.

Acceptance requires all of:

1. Exit code `0`, with no `FAILED` or `ERROR` lines.
2. `test_at_least_one_product_read_succeeded` passes. It fails when every
   product test was skipped, so an empty run is never accepted.
3. Every `PASSED` product you expect: at least tasks, gmail, calendar, drive,
   people, driveactivity, youtube, analytics, searchconsole, and photos.
4. Only these skips, each with its reason shown by `-rs`:
   - a product you removed from `GOOGLEMCP_PRODUCTS`;
   - docs, sheets, slides, forms: no file of that type in Drive. To cover
     them, set `GOOGLEMCP_LIVE_DOCS_ID`, `GOOGLEMCP_LIVE_SHEETS_ID`,
     `GOOGLEMCP_LIVE_SLIDES_ID`, or `GOOGLEMCP_LIVE_FORMS_ID` to an id you
     own;
   - youtube_analytics: the account has no YouTube channel.

With `GOOGLEMCP_LIVE=1`, missing credentials or an enabled product whose read
tool is not exposed make the run fail instead of skipping. Without the
variable every test in `tests/live` is skipped, so the normal `uv run pytest`
never touches the account. The tests carry `@pytest.mark.live`; the skip
comes from a `skipif` on the live module itself.

The suite never writes. To check Tasks writes (create, update, complete,
delete on a throwaway task list), follow the optional, mutating checklist in
[tasks.md](tasks.md).

## F. MCP client over stdio

Add the stdio entry for your client from [clients.md](clients.md) (or
[hermes.md](hermes.md) for Hermes Agent) with `GOOGLEMCP_WRITE_PRODUCTS`
empty and `GOOGLEMCP_ALLOW_DESTRUCTIVE` set to `false`, then restart or
reload the client. With Secret Manager, also add `GOOGLEMCP_SECRET_BACKEND`
and `GOOGLEMCP_GCP_PROJECT` to that entry's `env`.

Check:

1. The client lists the server's tools; none are write tools (their names
   contain verbs like create, update, delete, send).
2. Ask: "List my Google task lists." The client calls `tasks_list_tasklists`
   and shows your lists (or an empty list).

The client starts `googlemcp serve` as a child process and talks to it over
stdin and stdout; nothing listens on a network port.

## G. Longevity check (7-day trap)

Do both:

1. **Today:** open
   <https://console.cloud.google.com/auth/audience?project=PROJECT_ID> and
   confirm **Publishing status: In production**. If it says Testing, publish
   now and repeat part C, or the token stops working 7 days after consent.
2. **On day 8 or later**, in a shell with the same store selection as part C:
   run `uv run googlemcp auth status`. It must exit `0` and show
   `token refresh: ok`. An `invalid_grant` error means the token was issued
   in Testing or was revoked; see [access-longevity.md](access-longevity.md).

Normal use also keeps the token alive: Google expires refresh tokens unused
for six months.

## H. Revoke and narrow

### Remove GoogleMCP's access to the account

```sh
uv run googlemcp auth revoke
```

This revokes the refresh token at Google and then deletes it from the store
(if Google cannot be reached, nothing is deleted; retry). Confirm at
<https://myaccount.google.com/connections> that GoogleMCP is gone, and remove
it there if it is still listed. To stop using it from a client, remove the
server entry from that client's configuration.

### Narrow access to fewer products or scopes

Logging in again does not narrow access: the login asks Google to include
previously granted scopes (`include_granted_scopes=true`), so old grants
carry over. To narrow:

1. Revoke first: `uv run googlemcp auth revoke` (or remove GoogleMCP at
   <https://myaccount.google.com/connections>).
2. Disable the products explicitly on both sides. Removing a key is not
   enough on either side:
   - Terraform enables a product when `products` says so and otherwise by its
     catalog default, so a default-on product needs an explicit `false`, for
     example `products = { gmail = false }` in `terraform.tfvars`. Apply.
   - The server enables the catalog defaults while `GOOGLEMCP_PRODUCTS` is
     unset. Set it to the full list you keep, for example every default
     except Gmail:
     `GOOGLEMCP_PRODUCTS=analytics,calendar,docs,drive,driveactivity,forms,people,photos,searchconsole,sheets,slides,tasks,youtube,youtube_analytics`
     (also in the client's `env` block). Check with `uv run googlemcp products`.
3. Update the data access page (part B.2) with the new `googlemcp scopes`.
4. Log in again (part C) and confirm with part D.

If the client secret leaks, follow
[manual-steps.md, Recovery](manual-steps.md#recovery).
