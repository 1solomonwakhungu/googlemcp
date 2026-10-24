import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import google_crc32c
import pytest
from google.api_core import exceptions as gexc
from keyring.errors import KeyringError, PasswordDeleteError

from googlemcp.config import load_settings
from googlemcp.secretstore import (
    DirStore,
    EnvStore,
    FileStore,
    KeyringStore,
    SecretKey,
    SecretManagerStore,
    SecretStore,
    SecretStoreError,
    open_store,
)

VALUE = "super-secret-value-123"


def test_secret_ids_match_terraform_contract() -> None:
    assert sorted(k.value for k in SecretKey) == sorted(
        [
            "oauth-client-id",
            "oauth-client-secret",
            "refresh-token",
        ]
    )
    assert SecretKey.REFRESH_TOKEN.env_var == "GOOGLEMCP_REFRESH_TOKEN"


# File backend


def test_file_store_roundtrip_with_private_modes(tmp_path: Path) -> None:
    path = tmp_path / "cfg" / "secrets.json"
    store = FileStore(path)
    assert store.get(SecretKey.REFRESH_TOKEN) is None
    store.set(SecretKey.REFRESH_TOKEN, VALUE)
    assert store.get(SecretKey.REFRESH_TOKEN) == VALUE
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    store.delete(SecretKey.REFRESH_TOKEN)
    store.delete(SecretKey.REFRESH_TOKEN)
    assert store.get(SecretKey.REFRESH_TOKEN) is None
    assert not list(path.parent.glob(".secrets-*"))


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604])
def test_file_store_refuses_group_or_world_access(tmp_path: Path, mode: int) -> None:
    path = tmp_path / "secrets.json"
    path.write_text(f'{{"refresh-token": "{VALUE}"}}')
    path.chmod(mode)
    with pytest.raises(SecretStoreError, match="chmod 600") as info:
        FileStore(path).get(SecretKey.REFRESH_TOKEN)
    assert VALUE not in str(info.value)


def test_file_store_rejects_malformed_json(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    path.write_text("[1, 2]")
    path.chmod(0o600)
    with pytest.raises(SecretStoreError, match="JSON object"):
        FileStore(path).get(SecretKey.REFRESH_TOKEN)


# Dir backend (Cloud Run secret volumes)


def test_dir_store_reads_flat_and_nested_layouts(tmp_path: Path) -> None:
    (tmp_path / "oauth-client-id").write_text("cid\n")
    nested = tmp_path / "refresh-token"
    nested.mkdir()
    (nested / "value").write_text(VALUE)
    store = DirStore(tmp_path)
    assert store.get(SecretKey.OAUTH_CLIENT_ID) == "cid"
    assert store.get(SecretKey.REFRESH_TOKEN) == VALUE
    assert store.get(SecretKey.OAUTH_CLIENT_SECRET) is None


def test_dir_store_rereads_on_every_access(tmp_path: Path) -> None:
    store = DirStore(tmp_path)
    (tmp_path / "refresh-token").write_text("v1")
    assert store.get(SecretKey.REFRESH_TOKEN) == "v1"
    (tmp_path / "refresh-token").write_text("v2")
    assert store.get(SecretKey.REFRESH_TOKEN) == "v2"


def test_dir_store_is_read_only(tmp_path: Path) -> None:
    with pytest.raises(SecretStoreError, match="read-only"):
        DirStore(tmp_path).set(SecretKey.REFRESH_TOKEN, VALUE)


# Env backend


def test_env_store_reads_and_is_read_only() -> None:
    store = EnvStore({"GOOGLEMCP_OAUTH_CLIENT_SECRET": VALUE, "GOOGLEMCP_REFRESH_TOKEN": ""})
    assert store.get(SecretKey.OAUTH_CLIENT_SECRET) == VALUE
    assert store.get(SecretKey.REFRESH_TOKEN) is None
    with pytest.raises(SecretStoreError, match="read-only"):
        store.set(SecretKey.REFRESH_TOKEN, "x")
    with pytest.raises(SecretStoreError, match="read-only"):
        store.delete(SecretKey.REFRESH_TOKEN)


# Keyring backend


class FakeKeyring:
    def __init__(self) -> None:
        self.data: dict[tuple[str, str], str] = {}
        self.fail: Exception | None = None

    def get_password(self, service_name: str, username: str) -> str | None:
        if self.fail:
            raise self.fail
        return self.data.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        if self.fail:
            raise self.fail
        self.data[(service_name, username)] = password

    def delete_password(self, service_name: str, username: str) -> None:
        if (service_name, username) not in self.data:
            raise PasswordDeleteError("not found")
        del self.data[(service_name, username)]


def test_keyring_store_namespaces_by_prefix() -> None:
    backend = FakeKeyring()
    store = KeyringStore(prefix="googlemcp", backend=backend)
    store.set(SecretKey.OAUTH_CLIENT_SECRET, VALUE)
    assert backend.data == {("googlemcp", "oauth-client-secret"): VALUE}
    assert store.get(SecretKey.OAUTH_CLIENT_SECRET) == VALUE
    store.delete(SecretKey.OAUTH_CLIENT_SECRET)
    store.delete(SecretKey.OAUTH_CLIENT_SECRET)
    assert backend.data == {}


def test_keyring_errors_are_actionable_and_secret_free() -> None:
    backend = FakeKeyring()
    backend.fail = KeyringError(f"backend exploded with {VALUE}")
    store = KeyringStore(backend=backend)
    with pytest.raises(SecretStoreError) as info:
        store.set(SecretKey.REFRESH_TOKEN, VALUE)
    assert VALUE not in str(info.value)
    assert "GOOGLEMCP_SECRET_BACKEND=file" in str(info.value)


# Secret Manager backend


class FakeSecretManager:
    def __init__(self) -> None:
        self.versions: dict[str, list[dict[str, Any]]] = {}
        self.existing: set[str] = set()
        self.fail_access: Exception | None = None

    def add_secret_version(self, request: dict[str, Any]) -> Any:
        parent = request["parent"]
        if parent not in self.existing:
            raise gexc.NotFound("secret missing")
        versions = self.versions.setdefault(parent, [])
        name = f"{parent}/versions/{len(versions) + 1}"
        versions.append({"name": name, "payload": request["payload"], "state": "ENABLED"})
        return SimpleNamespace(name=name)

    def access_secret_version(self, request: dict[str, Any]) -> Any:
        if self.fail_access:
            raise self.fail_access
        parent = request["name"].rsplit("/versions/", 1)[0]
        if parent not in self.existing:
            raise gexc.NotFound("no secret")
        enabled = [v for v in self.versions.get(parent, []) if v["state"] == "ENABLED"]
        if not enabled:
            raise gexc.FailedPrecondition("no enabled versions")
        payload = enabled[-1]["payload"]
        return SimpleNamespace(
            payload=SimpleNamespace(data=payload["data"], data_crc32c=payload.get("data_crc32c"))
        )

    def list_secret_versions(self, request: dict[str, Any]) -> list[Any]:
        assert request["filter"] == "state:ENABLED"
        return [
            SimpleNamespace(name=v["name"])
            for v in self.versions.get(request["parent"], [])
            if v["state"] == "ENABLED"
        ]

    def destroy_secret_version(self, request: dict[str, Any]) -> None:
        for versions in self.versions.values():
            for v in versions:
                if v["name"] == request["name"]:
                    v["state"] = "DESTROYED"


def _gsm() -> tuple[SecretManagerStore, FakeSecretManager]:
    fake = FakeSecretManager()
    fake.existing.add("projects/p1/secrets/googlemcp-refresh-token")
    return SecretManagerStore(project="p1", prefix="googlemcp", client=fake), fake


def test_gsm_adds_versions_with_checksum_and_reads_latest() -> None:
    store, fake = _gsm()
    assert store.get(SecretKey.REFRESH_TOKEN) is None
    store.set(SecretKey.REFRESH_TOKEN, "first")
    store.set(SecretKey.REFRESH_TOKEN, VALUE)
    versions = fake.versions["projects/p1/secrets/googlemcp-refresh-token"]
    assert len(versions) == 2
    assert versions[-1]["payload"]["data_crc32c"] == google_crc32c.value(VALUE.encode())
    assert store.get(SecretKey.REFRESH_TOKEN) == VALUE


def test_gsm_never_creates_secrets() -> None:
    store, _ = _gsm()
    with pytest.raises(SecretStoreError, match="terraform apply"):
        store.set(SecretKey.OAUTH_CLIENT_SECRET, VALUE)
    assert store.get(SecretKey.OAUTH_CLIENT_SECRET) is None


def test_gsm_delete_destroys_enabled_versions_only() -> None:
    store, fake = _gsm()
    store.set(SecretKey.REFRESH_TOKEN, VALUE)
    store.delete(SecretKey.REFRESH_TOKEN)
    assert store.get(SecretKey.REFRESH_TOKEN) is None
    assert "projects/p1/secrets/googlemcp-refresh-token" in fake.existing


def test_gsm_permission_errors_are_secret_free() -> None:
    store, fake = _gsm()
    fake.fail_access = gexc.PermissionDenied(f"denied {VALUE}")
    with pytest.raises(SecretStoreError, match="IAM") as info:
        store.get(SecretKey.REFRESH_TOKEN)
    assert VALUE not in str(info.value)


def test_gsm_checksum_mismatch_detected() -> None:
    store, fake = _gsm()
    store.set(SecretKey.REFRESH_TOKEN, VALUE)
    fake.versions["projects/p1/secrets/googlemcp-refresh-token"][-1]["payload"]["data"] = b"bad"
    with pytest.raises(SecretStoreError, match="checksum"):
        store.get(SecretKey.REFRESH_TOKEN)


def test_gsm_without_adc_is_actionable(monkeypatch: pytest.MonkeyPatch) -> None:
    from google.auth.exceptions import DefaultCredentialsError
    from google.cloud import secretmanager

    def no_adc(*args: Any, **kwargs: Any) -> Any:
        raise DefaultCredentialsError("File /x was not found.")

    monkeypatch.setattr(secretmanager, "SecretManagerServiceClient", no_adc)
    store = SecretManagerStore(project="p1")
    with pytest.raises(SecretStoreError, match="application-default login"):
        store.get(SecretKey.OAUTH_CLIENT_SECRET)


def test_gsm_requires_project() -> None:
    with pytest.raises(SecretStoreError, match="GOOGLEMCP_GCP_PROJECT"):
        SecretManagerStore(project="")


# Factory


@pytest.mark.parametrize(
    ("backend", "cls"),
    [("keyring", KeyringStore), ("file", FileStore), ("dir", DirStore), ("env", EnvStore)],
)
def test_open_store_selects_backend(backend: str, cls: type) -> None:
    store = open_store(load_settings(secret_backend=backend))
    assert isinstance(store, cls)
    assert isinstance(store, SecretStore)


def test_open_store_secretmanager_uses_project_and_prefix() -> None:
    store = open_store(
        load_settings(secret_backend="secretmanager", gcp_project="p9", secret_prefix="gm")
    )
    assert isinstance(store, SecretManagerStore)
    assert store.secret_id(SecretKey.OAUTH_CLIENT_SECRET) == "gm-oauth-client-secret"
