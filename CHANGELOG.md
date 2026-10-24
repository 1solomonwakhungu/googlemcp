# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
uses [Semantic Versioning](https://semver.org/).

## Unreleased

## 0.1.0 - 2026-10-24

First public release.

### Added

- Local MCP server, `googlemcp serve`, that speaks MCP over stdio only and
  opens no network listener.
- Adapters for 19 Google products from `catalog/services.json`: Tasks,
  Gmail, Calendar, Drive, Docs, Sheets, Slides, Forms, People, Meet, Drive
  Activity, Chat, YouTube Data, YouTube Analytics, YouTube Reporting, Google
  Analytics, Search Console, Apps Script, and Photos (app-created media and
  Picker). 15 are enabled by default; Meet and Chat (Workspace accounts
  only), YouTube Reporting (creates server-side jobs), and Apps Script (runs
  code) are opt-in.
- Permission model: read scopes and read tools by default; write tools and
  write scopes per product with `GOOGLEMCP_WRITE_PRODUCTS`; delete, trash,
  and send tools only with `GOOGLEMCP_ALLOW_DESTRUCTIVE=true` as well. Tools
  carry MCP read-only and destructive hints.
- Full Google Tasks support: list task lists and tasks, get, create, update,
  complete, and delete.
- Desktop OAuth sign-in with PKCE on a 127.0.0.1 loopback redirect:
  `googlemcp auth login`, `auth status`, and `auth revoke`.
- Secret backends: OS keyring (default), 0600 file, read-only directory,
  read-only environment, and Google Secret Manager (optional
  `secretmanager` extra).
- CLI commands to inspect the configuration without calling Google
  (`products`, `scopes`, `tools`), health checks (`doctor`, and
  `doctor --probe` for one read-only call per product), and `secret set`,
  `secret list`, `secret delete`.
- Google error text passed to the model has URLs and credential-looking
  values redacted; for 401 and 403 it is withheld and replaced by a fixed
  recovery hint.
- Optional Terraform module that enables the APIs for enabled products,
  can create Secret Manager containers (never secret values), and
  bootstraps a private, versioned state bucket. Policy tests reject secret
  versions, keys, public IAM, and hosting resources.
- Documentation: quick start, Google Cloud console steps, client setup for
  Claude Desktop, Claude Code, Cursor, and Hermes Agent, access longevity,
  an acceptance runbook, and the security model.
- CI: tests with coverage, ruff, strict mypy, end-to-end tests against a
  fake Google, pip-audit, Checkov, gitleaks, actionlint, and shellcheck.

### Known limitations

- Not published on PyPI. Install from a clone with `uv sync`, or run with
  `uvx --from git+<repository URL>`.
- While the OAuth consent screen is in Testing, Google expires refresh
  tokens 7 days after consent. See `docs/access-longevity.md` for the
  In production option.
- The Meet and Chat REST APIs do not serve personal Google accounts.
- Since 2025-03-31 the Photos Library API reaches only media the app
  created; other photos are read through the Picker API.
- No remote, HTTP, or multi-user mode.
