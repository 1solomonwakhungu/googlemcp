"""Confirm the secret store holds the Desktop OAuth client from a downloaded JSON.

Run it after ``googlemcp auth login --client-secrets FILE`` and before
deleting FILE. It opens the store googlemcp itself uses (keyring by default,
or GOOGLEMCP_SECRET_BACKEND / --secret-backend, including secretmanager) and
compares the stored oauth-client-id and oauth-client-secret with the file.
Exit 0 only when both match; only then is it safe to delete the file, which
holds the only full copy of the secret. ``auth status`` alone is not proof:
an older stored client (for example from before a secret rotation) can still
refresh the token. No value is printed.

Only Desktop (installed-app) client JSON is accepted; GoogleMCP runs locally
and has no Web OAuth client.

Usage:
    uv run python scripts/acceptance/verify_stored_client.py --json FILE [--secret-backend NAME]

Exit codes: 0 match, 1 missing or different, 2 unusable file or arguments
(including a JSON that is not a Desktop client).
"""

from __future__ import annotations

import argparse
import hmac
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

PREFIX = "verify_stored_client"


def read_client(path: Path) -> tuple[str, str] | None:
    """Return (client_id, client_secret) from a Desktop OAuth client JSON."""
    try:
        data: Any = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    block = data.get("installed") if isinstance(data, dict) else None
    if isinstance(block, dict) and block.get("client_id") and block.get("client_secret"):
        return str(block["client_id"]), str(block["client_secret"])
    return None


def _same(stored: str | None, wanted: str) -> bool:
    return stored is not None and hmac.compare_digest(stored.encode(), wanted.encode())


def verify(store: Any, client_id: str, client_secret: str) -> list[str]:
    """Return a list of problems (empty when the store matches the file)."""
    from googlemcp.secretstore import SecretKey

    keys = (SecretKey.OAUTH_CLIENT_ID, SecretKey.OAUTH_CLIENT_SECRET)
    problems = []
    for key, wanted, what in zip(
        keys, (client_id, client_secret), ("client ID", "client secret"), strict=True
    ):
        stored = store.get(key)
        if not stored:
            problems.append(f"{key.value} is missing from the {store.name} store")
        elif not _same(stored, wanted):
            problems.append(f"stored {key.value} does not match the {what} in the file")
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--json", required=True, type=Path, help="downloaded client JSON")
    parser.add_argument(
        "--secret-backend",
        choices=["keyring", "file", "dir", "env", "secretmanager"],
        default=None,
        help="defaults to GOOGLEMCP_SECRET_BACKEND, then keyring",
    )
    args = parser.parse_args(argv)

    client = read_client(args.json)
    if client is None:
        print(
            f"{PREFIX}: {args.json} is not a Desktop OAuth client JSON (top-level key 'installed')",
            file=sys.stderr,
        )
        return 2
    client_id, client_secret = client

    from googlemcp.config import load_settings
    from googlemcp.secretstore import open_store

    overrides = {"secret_backend": args.secret_backend} if args.secret_backend else {}
    store = open_store(load_settings(**overrides))
    problems = verify(store, client_id, client_secret)
    del client_id, client_secret
    if problems:
        for problem in problems:
            print(f"{PREFIX}: {problem}", file=sys.stderr)
        print(f"{PREFIX}: KEEP the file; store it again, then rerun this check", file=sys.stderr)
        return 1
    print(f"{PREFIX}: Desktop client stored and matches; the file can be deleted", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
