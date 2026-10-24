"""Generate docs/inventory.md: products, OAuth scopes, sensitivity, and MCP tools.

Use the output when filling the Google Auth Platform "Data access"
page and when deciding which products to enable. Output is deterministic (no
timestamps) so tests can enforce that the committed file is current.

Usage:
    uv run python scripts/gen_inventory.py           # rewrite docs/inventory.md
    uv run python scripts/gen_inventory.py --check   # exit 1 if it is out of date
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from googlemcp.catalog import Product, load_catalog
from googlemcp.services import discover_adapters
from googlemcp.services.base import ServiceAdapter, ToolRegistry

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = REPO_ROOT / "docs" / "inventory.md"
REGEN_HINT = "run uv run python scripts/gen_inventory.py"

_PREFIX = "https://www.googleapis.com/auth/"

_RESTRICTED_LIST = "https://support.google.com/cloud/answer/13464325"
_NOT_RESTRICTED = "not restricted, class unverified"

# Google's published sensitivity class per scope suffix, grouped by the
# official page where the class was read (checked 2026-10-05). Scopes Google
# classifies only by omission from the restricted list are marked
# "not restricted, class unverified"; the Data access page in the Cloud
# console shows their final label. Unknown scopes render as "unclassified".
_CLASSES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "restricted",
        _RESTRICTED_LIST,
        (
            "https://mail.google.com/",
            "gmail.readonly",
            "gmail.metadata",
            "gmail.modify",
            "gmail.insert",
            "gmail.compose",
            "gmail.settings.basic",
            "gmail.settings.sharing",
            "drive",
            "drive.readonly",
            "drive.activity",
            "drive.activity.readonly",
            "drive.metadata",
            "drive.metadata.readonly",
            "drive.scripts",
            "drive.meet.readonly",
            "chat.messages",
            "chat.messages.readonly",
            "chat.readonly",
            "chat.delete",
            "chat.import",
        ),
    ),
    (
        "sensitive",
        "https://developers.google.com/workspace/gmail/api/auth/scopes",
        ("gmail.send",),
    ),
    (
        "non-sensitive",
        "https://developers.google.com/workspace/gmail/api/auth/scopes",
        ("gmail.labels",),
    ),
    (
        "non-sensitive",
        "https://developers.google.com/workspace/drive/api/guides/api-specific-auth",
        ("drive.file",),
    ),
    (
        "sensitive",
        "https://developers.google.com/workspace/docs/api/auth",
        ("documents", "documents.readonly"),
    ),
    (
        "sensitive",
        "https://developers.google.com/workspace/sheets/api/scopes",
        ("spreadsheets", "spreadsheets.readonly"),
    ),
    (
        "sensitive",
        "https://developers.google.com/workspace/slides/api/scopes",
        ("presentations", "presentations.readonly"),
    ),
    (
        "sensitive",
        "https://developers.google.com/workspace/meet/api/guides/authenticate-authorize",
        ("meetings.space.created", "meetings.space.readonly"),
    ),
    (
        "non-sensitive",
        "https://developers.google.com/workspace/meet/api/guides/authenticate-authorize",
        ("meetings.space.settings",),
    ),
    (
        "sensitive",
        "https://developers.google.com/workspace/chat/authenticate-authorize",
        ("chat.spaces.readonly", "chat.messages.create"),
    ),
    (
        _NOT_RESTRICTED,
        _RESTRICTED_LIST,
        (
            "tasks",
            "tasks.readonly",
            "calendar",
            "calendar.readonly",
            "calendar.events",
            "calendar.events.readonly",
            "calendar.calendarlist.readonly",
            "calendar.freebusy",
            "forms.body",
            "forms.body.readonly",
            "forms.responses.readonly",
            "contacts",
            "contacts.readonly",
            "contacts.other.readonly",
            "directory.readonly",
            "userinfo.profile",
            "userinfo.email",
            "script.projects",
            "script.projects.readonly",
            "script.deployments",
            "script.deployments.readonly",
            "youtube",
            "youtube.readonly",
            "youtube.force-ssl",
            "youtube.upload",
            "yt-analytics.readonly",
            "yt-analytics-monetary.readonly",
            "analytics.readonly",
            "analytics.edit",
            "webmasters",
            "webmasters.readonly",
            "photoslibrary.appendonly",
            "photoslibrary.readonly.appcreateddata",
            "photoslibrary.edit.appcreateddata",
            "photospicker.mediaitems.readonly",
        ),
    ),
)


def _full(scope: str) -> str:
    return scope if scope.startswith("https://") else _PREFIX + scope


SENSITIVITY: dict[str, tuple[str, str]] = {
    _full(scope): (cls, url) for cls, url, scopes in _CLASSES for scope in scopes
}


class _NoIOContext:
    """ServiceContext stand-in: registration must not perform I/O."""

    def client(self, api: str, version: str) -> Any:
        raise RuntimeError("inventory generation must not call Google")

    def execute(self, request: Any) -> dict[str, Any]:
        raise RuntimeError("inventory generation must not call Google")

    def authorized_session(self) -> Any:
        raise RuntimeError("inventory generation must not call Google")


def _short(scope: str) -> str:
    return scope.removeprefix(_PREFIX)


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _first_line(text: str) -> str:
    for line in text.strip().splitlines():
        if line.strip():
            return line.strip()
    return ""


def _sensitivity(scope: str) -> str:
    entry = SENSITIVITY.get(scope)
    return entry[0] if entry else "unclassified"


def _scope_rows(scopes: Sequence[str], access: str) -> list[str]:
    return [f"| `{_short(s)}` | {access} | {_sensitivity(s)} |" for s in scopes]


def _tool_rows(adapter: ServiceAdapter) -> list[str]:
    registry = ToolRegistry()
    adapter.register(registry, _NoIOContext())
    rows = []
    for tool in sorted(registry.tools, key=lambda t: t.name):
        kind = "destructive" if tool.destructive else "write" if tool.write else "read"
        rows.append(f"| `{tool.name}` | {kind} | {_cell(_first_line(tool.description))} |")
    return rows


def _product_section(product: Product, adapter: ServiceAdapter | None) -> list[str]:
    lines = [
        f"### {product.title} (`{product.key}`)",
        "",
        f"- Default enabled: {'yes' if product.default_enabled else 'no'}",
        f"- Risk: `{product.risk}`",
        f"- API services: {', '.join(f'`{s}`' for s in product.api_services)}",
    ]
    if adapter is None:
        lines += ["- Adapter: adapter pending (not on this branch yet)", ""]
        return lines
    spec = adapter.spec
    lines += ["", "| Scope | Access | Sensitivity |", "|---|---|---|"]
    lines += _scope_rows(spec.read_scopes, "read")
    lines += _scope_rows(spec.write_scopes, "write")
    lines += ["", "| Tool | Kind | Description |", "|---|---|---|"]
    lines += _tool_rows(adapter)
    lines.append("")
    return lines


def _scope_list(scopes: set[str]) -> list[str]:
    if not scopes:
        return ["(none)", ""]
    return [f"- `{s}` ({_sensitivity(s)})" for s in sorted(scopes)] + [""]


def render(catalog: Mapping[str, Product], adapters: Mapping[str, ServiceAdapter]) -> str:
    """Render the inventory markdown for ``catalog`` (in catalog order)."""
    read_union: set[str] = set()
    full_union: set[str] = set()
    pending: list[str] = []
    for key, product in catalog.items():
        adapter = adapters.get(key)
        if adapter is None:
            pending.append(key)
            continue
        if product.default_enabled:
            read_union.update(adapter.spec.read_scopes)
            full_union.update(adapter.spec.read_scopes, adapter.spec.write_scopes)

    lines = [
        "# Scope and tool inventory",
        "",
        "Generated by `scripts/gen_inventory.py` from `catalog/services.json` and the",
        f"adapters in `src/googlemcp/services/`. Do not edit by hand; {REGEN_HINT}.",
        "",
        "Kinds: `read` tools are always exposed for an enabled product; `write` tools",
        "need writes enabled; `destructive` tools also need the destructive opt-in.",
        "Sensitivity is Google's published class (non-sensitive, sensitive, restricted);",
        "see Sources. Restricted scopes need a security assessment for public apps.",
        "",
        "## Summary",
        "",
        "Scopes to add on the Google Auth Platform Data access page for the",
        "default-enabled products. Products with a pending adapter contribute nothing yet.",
        "",
        "### Read-only (default-enabled products)",
        "",
        *_scope_list(read_union),
        "### Read and write (default-enabled products)",
        "",
        *_scope_list(full_union),
    ]
    if pending:
        lines += [
            "### Adapters pending",
            "",
            ", ".join(f"`{k}`" for k in pending),
            "",
        ]
    lines += ["## Products", ""]
    for key, product in catalog.items():
        lines += _product_section(product, adapters.get(key))

    used = {
        scope
        for adapter in adapters.values()
        for scope in (*adapter.spec.read_scopes, *adapter.spec.write_scopes)
    }
    cited = sorted({SENSITIVITY[s][1] for s in used if s in SENSITIVITY})
    lines += ["## Sources", ""]
    lines += [f"- {url}" for url in cited] or ["(none)"]
    return "\n".join(lines).rstrip() + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--check", action="store_true", help="exit 1 if the file is out of date")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="markdown path")
    args = parser.parse_args(argv)

    content = render(load_catalog(), discover_adapters())
    output: Path = args.output
    if args.check:
        current = output.read_text(encoding="utf-8") if output.is_file() else None
        if current != content:
            print(f"{output} is out of date; {REGEN_HINT}", file=sys.stderr)
            return 1
        return 0
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(content, encoding="utf-8")
    print(f"wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
