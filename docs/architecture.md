# GoogleMCP architecture

## Goal

One repository that (1) provisions every Google Cloud resource that
Terraform can safely own for a personal Google integration, (2) documents the
human steps that have no API, and (3) ships a local MCP server that exposes
scoped Google APIs to any stdio MCP client on your machine. Each user brings
their own Google Cloud project and Desktop OAuth client.

## Layout

```
catalog/services.json        Product catalog: single source of truth (keys, API services, defaults)
infra/terraform/             Root module (API enablement, opt-in secret containers) + state bucket bootstrap
src/googlemcp/               Python MCP server
  catalog.py                 Catalog loader
  services/base.py           Adapter contract (ServiceSpec, ToolDef, ToolRegistry, ServiceContext)
  services/<key>.py          One adapter per product, exposes module-level ADAPTER
  testing.py                 Offline FakeContext (static discovery + HttpMockSequence)
  config, auth/, secretstore/, context, policy, server, cli
                             Core: settings, Desktop OAuth, secret backends, stdio server
tests/                       pytest; tests/test_contract.py cross-checks adapters vs catalog
scripts/                     Inventory generator, offline acceptance checks
docs/                        Setup, client, and design docs (manual steps, longevity, clients, Tasks, acceptance)
.github/workflows/           CI
```

## Key decisions

1. **Local stdio only.** The server runs on your machine as a child
   process of the MCP client and speaks MCP over stdin and stdout.
   It opens no network listener and has no HTTP transport, so there is no
   MCP-level authentication to manage: the trust boundary is the local user
   account and the client process that starts the server. Google API access
   uses one stored refresh token for your account, obtained by a local
   installed-app OAuth flow (loopback redirect + PKCE) and kept in the OS
   keyring by default.
2. **Adapters never import the MCP framework.** They register `ToolDef`s on a
   `ToolRegistry`. The core applies policy and wires tools into FastMCP. This
   makes adapters offline-testable and insulates them from MCP SDK churn.
3. **Least privilege by default.** Only `read_scopes` are requested unless
   writes are enabled per product. Write tools are hidden unless writes are on;
   destructive tools (delete, trash, send) need a second explicit opt-in.
   Products with `default_enabled=false` in the catalog need explicit opt-in
   in both Terraform and server config.
4. **Catalog drives both sides.** Terraform reads `catalog/services.json` to
   enable APIs; `tests/test_contract.py` asserts each adapter matches it.
5. **Secrets never touch Terraform state.** Terraform creates Secret Manager
   secret containers only, and only when `enable_secret_manager = true`. Secret versions (OAuth client
   secret, refresh token) are written by the CLI or `gcloud`, never by Terraform.
6. **No hosting infrastructure.** Terraform manages API enablement, the state
   bucket, and, only with `enable_secret_manager = true`, the Secret Manager
   containers for the optional `secretmanager` backend. There is no container
   image, hosted service, image registry, or runtime service account.
   ChatGPT custom connectors need a public HTTPS server, which this project
   intentionally does not provide; ChatGPT's built-in Google connectors are
   the supported path there.
7. **Access longevity.** Testing-mode OAuth apps issue refresh tokens that
   expire after 7 days. The supported path is to set the publishing status to
   In production (personal-use, unverified, under the user cap) and keep the
   token in use. See `docs/access-longevity.md`.

## Adapter contract (summary)

- Module `src/googlemcp/services/<key>.py`, `<key>` from the catalog.
- `ADAPTER.spec = ServiceSpec(key, title, api_services, read_scopes, write_scopes)`.
- `register(registry, ctx)` defines closures decorated with
  `@registry.tool("<key>_<verb>_<noun>", write=..., destructive=...)`.
- Tools are synchronous, keyword-typed, docstring-described, return small
  JSON dicts (use `pick`, `clamp_page_size`), and call Google only through
  `ctx.client(api, version)` + `ctx.execute(request)` or
  `ctx.authorized_session()` for REST-only APIs.
- Tests use `googlemcp.testing.FakeContext`; no network, no credentials.
- See `services/tasks.py` and `tests/test_tasks_adapter.py` for the pattern.

## History

Development versions before 0.1.0 also had an HTTP transport, MCP-level
access control, and an optional hosted runtime. They were removed before the
first public release to keep the server local and single-user. That is why
the server rejects the old transport, HTTP host and port, and access-control
settings at startup with a "no longer supported" error instead of ignoring
them.
