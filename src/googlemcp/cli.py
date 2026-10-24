"""``googlemcp`` command line.

Logs go to stderr so the stdio transport keeps stdout for the MCP protocol.
No command prints a secret value.
"""

from __future__ import annotations

import argparse
import getpass
import importlib
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from googlemcp import __version__
from googlemcp.auth import AuthError
from googlemcp.auth import flow as auth_flow
from googlemcp.config import ConfigError, Settings, load_settings
from googlemcp.policy import product_states, required_scopes, select_tools
from googlemcp.secretstore import SecretKey, SecretStore, SecretStoreError, open_store
from googlemcp.services import discover_adapters
from googlemcp.services.base import GoogleMCPError, ServiceAdapter, ServiceContext

# EXIT_UNVERIFIED: credentials work but granted scopes could not be confirmed.
EXIT_OK, EXIT_FAIL, EXIT_USAGE, EXIT_UNVERIFIED = 0, 1, 2, 3


def _print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, sort_keys=True))


def _settings(args: argparse.Namespace) -> Settings:
    overrides: dict[str, Any] = {}
    for name in ("secret_backend",):
        value = getattr(args, name, None)
        if value is not None:
            overrides[name] = value
    return load_settings(**overrides)


# serve


def cmd_serve(args: argparse.Namespace) -> int:
    from googlemcp.server import serve

    settings = _settings(args)
    serve(settings, open_store(settings))
    return EXIT_OK


# products and scopes


def cmd_products(args: argparse.Namespace) -> int:
    settings = _settings(args)
    states = product_states(settings, discover_adapters())
    if args.json:
        _print_json([{**s.__dict__, "active": s.active} for s in states])
        return EXIT_OK
    print(f"{'product':<20}{'enabled':<9}{'writes':<8}{'adapter':<9}risk")
    for s in states:
        print(f"{s.key:<20}{_yn(s.enabled):<9}{_yn(s.writes):<8}{_yn(s.has_adapter):<9}{s.risk}")
    missing = [s.key for s in states if s.enabled and not s.has_adapter]
    if missing:
        print(f"\nenabled without an adapter (no tools, no scopes): {', '.join(missing)}")
    return EXIT_OK


def _yn(value: bool) -> str:
    return "yes" if value else "no"


def cmd_scopes(args: argparse.Namespace) -> int:
    scopes = required_scopes(_settings(args), discover_adapters())
    if args.json:
        _print_json(scopes)
    else:
        print("\n".join(scopes))
    return EXIT_OK


# auth


def next_auth_step(client_configured: bool, refresh_token_present: bool) -> str | None:
    """What a user with incomplete credentials should run next, or None when nothing is missing."""
    if not client_configured:
        return auth_flow.client_setup_hint()
    if not refresh_token_present:
        return "Run `googlemcp auth login` to authorize your Google account."
    return None


def cmd_auth_login(args: argparse.Namespace) -> int:
    settings = _settings(args)
    store = open_store(settings)
    scopes = required_scopes(settings, discover_adapters())
    result = auth_flow.login(
        store,
        scopes,
        client_secrets=Path(args.client_secrets) if args.client_secrets else None,
        open_browser=not args.no_browser,
        port=args.port,
        login_hint=args.login_hint,
    )
    print(f"Stored refresh token in the {store.name} store.")
    if result.granted_scopes is None:
        print(
            "Google did not report which scopes were granted; "
            "run `googlemcp auth status` to verify.",
            file=sys.stderr,
        )
        return EXIT_UNVERIFIED
    print(f"Granted {len(result.granted_scopes)} scopes.")
    if result.missing_scopes:
        print("Not granted (tools needing them will fail):", file=sys.stderr)
        for scope in result.missing_scopes:
            print(f"  {scope}", file=sys.stderr)
    if args.client_secrets:
        print(f"Client stored. You can now delete {args.client_secrets}.")
    return EXIT_OK


def cmd_auth_status(args: argparse.Namespace) -> int:
    settings = _settings(args)
    store = open_store(settings)
    report = auth_flow.status(
        store, required_scopes(settings, discover_adapters()), check=not args.no_check
    )
    if args.json:
        _print_json(report.as_dict())
    else:
        print(f"secret backend:      {report.backend}")
        print(f"oauth client stored: {_yn(report.client_configured)}")
        print(f"refresh token:       {'present' if report.refresh_token_present else 'missing'}")
        if report.refresh_ok is not None:
            print(f"token refresh:       {'ok' if report.refresh_ok else 'FAILED'}")
        if report.access_token_expiry:
            print(f"access token expiry: {report.access_token_expiry}")
        if report.granted_scopes:
            print(f"granted scopes:      {len(report.granted_scopes)}")
        for scope in report.missing_scopes or ():
            print(f"missing scope:       {scope}")
        if report.refresh_ok and not report.scopes_verified:
            print("scopes:              UNVERIFIED")
        if report.error:
            print(f"error: {report.error}", file=sys.stderr)
        step = next_auth_step(report.client_configured, report.refresh_token_present)
        if step and not report.error:
            print(f"\nnext step: {step}", file=sys.stderr)
    if not (report.client_configured and report.refresh_token_present):
        return EXIT_FAIL
    if report.refresh_ok is False or report.missing_scopes:
        return EXIT_FAIL
    if report.refresh_ok and not report.scopes_verified:
        # Unknown is not healthy: tokeninfo failed, so the grant is unconfirmed.
        return EXIT_UNVERIFIED
    return EXIT_OK


def cmd_auth_revoke(args: argparse.Namespace) -> int:
    store = open_store(_settings(args))
    if not store.get(SecretKey.REFRESH_TOKEN):
        print("No refresh token stored; nothing to revoke.")
        return EXIT_OK
    confirmed = auth_flow.revoke(store)
    if confirmed:
        print("Revoked at Google and deleted locally.")
    else:
        print("Google reported the token was already invalid; deleted locally.")
    return EXIT_OK


# secrets and tokens

_SETTABLE = {key.value: key for key in SecretKey}


def cmd_secret_set(args: argparse.Namespace) -> int:
    store = open_store(_settings(args))
    key = _SETTABLE[args.key]
    if args.stdin:
        value = sys.stdin.read().strip()
    else:
        value = getpass.getpass(f"Value for {key.value} (input hidden): ").strip()
    if not value:
        print("empty value; nothing stored", file=sys.stderr)
        return EXIT_USAGE
    store.set(key, value)
    print(f"Stored {key.value} in the {store.name} store.")
    return EXIT_OK


def cmd_secret_delete(args: argparse.Namespace) -> int:
    store = open_store(_settings(args))
    store.delete(_SETTABLE[args.key])
    print(f"Deleted {args.key} from the {store.name} store.")
    return EXIT_OK


def cmd_secret_list(args: argparse.Namespace) -> int:
    store = open_store(_settings(args))
    presence = {key.value: bool(store.get(key)) for key in SecretKey}
    if args.json:
        _print_json({"backend": store.name, "present": presence})
    else:
        print(f"backend: {store.name}")
        for name, present in presence.items():
            print(f"  {name:<26}{'set' if present else 'missing'}")
    return EXIT_OK


# doctor


# ProbeResult.status values (googlemcp.probes) mapped to doctor states.
_PROBE_OK = frozenset({"ok"})
_PROBE_WARN = frozenset({"skipped", "not_enabled", "unsupported_account"})


def _run_probes(ctx: ServiceContext, products: list[str]) -> list[dict[str, Any]]:
    """Delegate to ``googlemcp.probes.run_probes`` when the module is importable."""
    try:
        probes = importlib.import_module("googlemcp.probes")
    except ImportError:
        return [{"product": "all", "ok": None, "detail": "probes module not installed"}]
    try:
        results = probes.run_probes(ctx, products)
    except GoogleMCPError as exc:
        return [{"product": "all", "ok": False, "detail": str(exc)}]
    out = []
    for r in results:
        status = str(r.status)
        ok = True if status in _PROBE_OK else (None if status in _PROBE_WARN else False)
        out.append({"product": r.product, "ok": ok, "detail": f"{status}: {r.detail}"})
    return out


def run_doctor(
    settings: Settings,
    store: SecretStore,
    adapters: dict[str, ServiceAdapter],
    *,
    probe: bool,
    ctx: ServiceContext | None = None,
) -> tuple[list[dict[str, str]], bool]:
    checks: list[dict[str, str]] = []

    def record(name: str, ok: bool | None, detail: str) -> None:
        state = "ok" if ok else ("warn" if ok is None else "fail")
        checks.append({"check": name, "status": state, "detail": detail})

    record("config", True, f"transport=stdio secret_backend={settings.secret_backend}")

    states = product_states(settings, adapters)
    active = [s.key for s in states if s.active]
    record("products", bool(active), f"active: {', '.join(active) or 'none'}")
    orphans = [s.key for s in states if s.enabled and not s.has_adapter]
    if orphans:
        record("adapters", None, f"enabled without adapter: {', '.join(orphans)}")

    scopes = required_scopes(settings, adapters)
    report = auth_flow.status(store, scopes)
    if report.error and not report.refresh_token_present and not report.client_configured:
        record("secret store", False, report.error)
    else:
        record("secret store", True, f"backend {report.backend} reachable")
    step = next_auth_step(report.client_configured, report.refresh_token_present) or ""
    if report.client_configured:
        client_detail = "Desktop client id and secret stored"
        token_detail = "refresh token stored" if report.refresh_token_present else step
    else:
        client_detail = step
        token_detail = "stored" if report.refresh_token_present else "missing; needs the client"
    record("oauth client", report.client_configured, client_detail)
    record("refresh token", report.refresh_token_present, token_detail)
    if report.refresh_ok is not None:
        record("token refresh", report.refresh_ok, report.error or "access token refreshed")
    if report.refresh_ok:
        missing = report.missing_scopes
        if not report.scopes_verified or missing is None:
            record("scopes", False, report.error or "granted scopes could not be verified")
        else:
            record(
                "scopes",
                not missing,
                f"missing: {', '.join(missing)}" if missing else f"{len(scopes)} required granted",
            )

    if probe and report.refresh_ok:
        if ctx is None:
            from googlemcp.auth import CredentialProvider
            from googlemcp.context import GoogleServiceContext

            ctx = GoogleServiceContext(CredentialProvider(store), max_retries=1)
        for result in _run_probes(ctx, active):
            record(f"probe {result['product']}", result["ok"], result["detail"])

    healthy = all(c["status"] != "fail" for c in checks)
    return checks, healthy


def cmd_doctor(args: argparse.Namespace) -> int:
    settings = _settings(args)
    checks, healthy = run_doctor(
        settings, open_store(settings), discover_adapters(), probe=args.probe
    )
    if args.json:
        _print_json({"healthy": healthy, "checks": checks})
    else:
        for c in checks:
            print(f"[{c['status']:>4}] {c['check']}: {c['detail']}")
    return EXIT_OK if healthy else EXIT_FAIL


def cmd_tools(args: argparse.Namespace) -> int:
    from googlemcp.auth import CredentialProvider
    from googlemcp.context import GoogleServiceContext

    settings = _settings(args)
    # Registration performs no I/O, so the lazy context never touches secrets.
    ctx = GoogleServiceContext(CredentialProvider(open_store(settings)))
    tools = select_tools(settings, discover_adapters(), ctx)
    rows = [
        {"product": p, "name": t.name, "write": t.write, "destructive": t.destructive}
        for p, t in tools
    ]
    if args.json:
        _print_json(rows)
    else:
        for r in rows:
            flags = "destructive" if r["destructive"] else ("write" if r["write"] else "read")
            print(f"{r['name']:<44}{flags}")
    return EXIT_OK


# parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="googlemcp", description=__doc__.splitlines()[0])
    parser.add_argument("--version", action="version", version=f"googlemcp {__version__}")
    parser.add_argument(
        "--secret-backend", choices=["keyring", "file", "dir", "env", "secretmanager"], default=None
    )
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the MCP server over stdio")
    # Accepted for MCP client configs written before the server became
    # stdio-only; any other value is rejected by argparse.
    serve.add_argument("--transport", choices=["stdio"], default=None, help=argparse.SUPPRESS)
    serve.set_defaults(func=cmd_serve)

    for name, func, text in (
        ("products", cmd_products, "show catalog products and their state"),
        ("scopes", cmd_scopes, "print the OAuth scopes the current config needs"),
        ("tools", cmd_tools, "list the tools the current config exposes"),
    ):
        p = sub.add_parser(name, help=text)
        p.add_argument("--json", action="store_true")
        p.set_defaults(func=func)

    auth = sub.add_parser(
        "auth", help="authorize a Google account, check or revoke it"
    ).add_subparsers(dest="auth_command", required=True)
    login = auth.add_parser("login", help="authorize your Google account in a browser")
    login.add_argument(
        "--client-secrets",
        metavar="PATH",
        help="Desktop app OAuth client JSON downloaded from Google Cloud; needed on first "
        "login, read once and copied into the secret store",
    )
    login.add_argument("--no-browser", action="store_true", help="print the URL instead")
    login.add_argument("--port", type=int, default=0, help="loopback port (default: random)")
    login.add_argument(
        "--login-hint", metavar="EMAIL", help="preselect this Google account on the consent page"
    )
    login.set_defaults(func=cmd_auth_login)
    status = auth.add_parser("status", help="show credential health without secrets")
    status.add_argument("--json", action="store_true")
    status.add_argument("--no-check", action="store_true", help="skip the live refresh check")
    status.set_defaults(func=cmd_auth_status)
    revoke = auth.add_parser("revoke", help="revoke the refresh token and delete it")
    revoke.set_defaults(func=cmd_auth_revoke)

    secret = sub.add_parser("secret", help="manage stored secrets").add_subparsers(
        dest="secret_command", required=True
    )
    sset = secret.add_parser("set", help="store a secret from a hidden prompt or stdin")
    sset.add_argument("key", choices=sorted(_SETTABLE))
    sset.add_argument("--stdin", action="store_true", help="read the value from stdin")
    sset.set_defaults(func=cmd_secret_set)
    sdel = secret.add_parser("delete", help="delete a stored secret")
    sdel.add_argument("key", choices=sorted(_SETTABLE))
    sdel.set_defaults(func=cmd_secret_delete)
    slist = secret.add_parser("list", help="show which secrets are present (never values)")
    slist.add_argument("--json", action="store_true")
    slist.set_defaults(func=cmd_secret_list)

    doctor = sub.add_parser("doctor", help="non-destructive health checks")
    doctor.add_argument("--probe", action="store_true", help="call one read tool per product")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(func=cmd_doctor)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        settings_level = _settings(args).log_level
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    logging.basicConfig(
        stream=sys.stderr,
        level=settings_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        code: int = args.func(args)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except (AuthError, SecretStoreError, GoogleMCPError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAIL
    except KeyboardInterrupt:
        return 130
    return code
