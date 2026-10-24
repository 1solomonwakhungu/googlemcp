"""Pluggable secret storage. See ``base.SecretStore`` for the contract.

Named ``secretstore`` rather than ``secrets`` so the repository's protective
``secrets/`` ignore rule keeps applying to stray local secret directories.
"""

from __future__ import annotations

from googlemcp.config import Settings
from googlemcp.secretstore.backends import DirStore, EnvStore, FileStore, KeyringStore
from googlemcp.secretstore.base import (
    SecretKey,
    SecretStore,
    SecretStoreError,
    ensure_writable,
    require,
)
from googlemcp.secretstore.secretmanager import SecretManagerStore

__all__ = [
    "DirStore",
    "EnvStore",
    "FileStore",
    "KeyringStore",
    "SecretKey",
    "SecretManagerStore",
    "SecretStore",
    "SecretStoreError",
    "ensure_writable",
    "open_store",
    "require",
]


def open_store(settings: Settings) -> SecretStore:
    """Return the backend selected by ``settings.secret_backend``."""
    backend = settings.secret_backend
    if backend == "keyring":
        return KeyringStore(prefix=settings.secret_prefix)
    if backend == "file":
        return FileStore(settings.secret_file_path())
    if backend == "dir":
        return DirStore(settings.secrets_dir.expanduser())
    if backend == "env":
        return EnvStore()
    if backend == "secretmanager":
        return SecretManagerStore(project=settings.gcp_project or "", prefix=settings.secret_prefix)
    raise SecretStoreError(f"unknown secret backend {backend!r}")  # pragma: no cover
