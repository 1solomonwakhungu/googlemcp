"""Confirm the secret store holds the Desktop OAuth client from a downloaded JSON.

Compatibility wrapper around ``googlemcp auth verify-client`` so existing
scripts and invocations keep their exact CLI contract:

    uv run python scripts/acceptance/verify_stored_client.py --json FILE [--secret-backend NAME]

Exit codes: 0 match, 1 missing or different, 2 unusable file or arguments
(including a JSON that is not a Desktop client). No secret value is printed.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

PREFIX = "verify_stored_client"


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

    from googlemcp.auth import AuthError, verify_stored_client
    from googlemcp.config import load_settings
    from googlemcp.secretstore import open_store

    overrides = {"secret_backend": args.secret_backend} if args.secret_backend else {}
    store = open_store(load_settings(**overrides))
    try:
        result = verify_stored_client(store, args.json)
    except AuthError:
        print(
            f"{PREFIX}: {args.json} is not a Desktop OAuth client JSON (top-level key 'installed')",
            file=sys.stderr,
        )
        return 2
    if not result.matches:
        for problem in result.problems:
            print(f"{PREFIX}: {problem}", file=sys.stderr)
        print(f"{PREFIX}: KEEP the file; store it again, then rerun this check", file=sys.stderr)
        return 1
    print(f"{PREFIX}: Desktop client stored and matches; the file can be deleted", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
