"""Google Secret Manager backend (optional extra ``googlemcp[secretmanager]``).

Authenticates with Application Default Credentials. Terraform creates the
secret containers and IAM bindings; this backend only adds, reads, and
destroys versions. It never creates or deletes secrets, so a typo in the
project or prefix fails loudly instead of creating stray secrets.
"""

from __future__ import annotations

from typing import Any

from google.auth.exceptions import DefaultCredentialsError

from googlemcp.secretstore.base import SecretKey, SecretStoreError


def _crc32c(data: bytes) -> int | None:
    try:
        import google_crc32c
    except ImportError:  # pragma: no cover - google-crc32c ships with the secretmanager extra
        return None
    return int(google_crc32c.value(data))


class SecretManagerStore:
    name = "secretmanager"
    read_only = False

    def __init__(self, project: str, prefix: str = "googlemcp", client: Any | None = None):
        if not project:
            raise SecretStoreError("secretmanager backend requires GOOGLEMCP_GCP_PROJECT")
        self.project = project
        self.prefix = prefix
        self._client = client

    def _api(self) -> Any:
        if self._client is None:
            try:
                from google.cloud import secretmanager
            except ImportError:
                raise SecretStoreError(
                    "google-cloud-secret-manager is not installed. "
                    "Install the extra: uv sync --extra secretmanager"
                ) from None
            try:
                self._client = secretmanager.SecretManagerServiceClient()
            except DefaultCredentialsError:
                raise SecretStoreError(
                    "Secret Manager needs Application Default Credentials: run "
                    "`gcloud auth application-default login`."
                ) from None
        return self._client

    def secret_id(self, key: SecretKey) -> str:
        return f"{self.prefix}-{key.value}"

    def _secret_path(self, key: SecretKey) -> str:
        return f"projects/{self.project}/secrets/{self.secret_id(key)}"

    def get(self, key: SecretKey) -> str | None:
        from google.api_core import exceptions as gexc

        name = f"{self._secret_path(key)}/versions/latest"
        try:
            response = self._api().access_secret_version(request={"name": name})
        except (gexc.NotFound, gexc.FailedPrecondition):
            # No secret, or no enabled version (latest destroyed or disabled).
            return None
        except gexc.GoogleAPICallError as exc:
            raise SecretStoreError(_failure("read", self.secret_id(key), exc)) from None
        data: bytes = response.payload.data
        expected = getattr(response.payload, "data_crc32c", None)
        actual = _crc32c(data)
        if expected and actual is not None and expected != actual:
            raise SecretStoreError(f"checksum mismatch reading {self.secret_id(key)!r}")
        return data.decode("utf-8")

    def set(self, key: SecretKey, value: str) -> None:
        from google.api_core import exceptions as gexc

        data = value.encode("utf-8")
        payload: dict[str, Any] = {"data": data}
        checksum = _crc32c(data)
        if checksum is not None:
            payload["data_crc32c"] = checksum
        try:
            self._api().add_secret_version(
                request={"parent": self._secret_path(key), "payload": payload}
            )
        except gexc.NotFound:
            raise SecretStoreError(
                f"secret {self.secret_id(key)!r} does not exist in project {self.project!r}. "
                "Create it with terraform apply (infra/terraform)."
            ) from None
        except gexc.GoogleAPICallError as exc:
            raise SecretStoreError(_failure("write", self.secret_id(key), exc)) from None

    def delete(self, key: SecretKey) -> None:
        """Destroy every enabled version. The secret container stays (Terraform owns it)."""
        from google.api_core import exceptions as gexc

        api = self._api()
        try:
            versions = list(
                api.list_secret_versions(
                    request={"parent": self._secret_path(key), "filter": "state:ENABLED"}
                )
            )
            for version in versions:
                api.destroy_secret_version(request={"name": version.name})
        except gexc.NotFound:
            return
        except gexc.GoogleAPICallError as exc:
            raise SecretStoreError(_failure("delete", self.secret_id(key), exc)) from None


def _failure(action: str, secret_id: str, exc: Any) -> str:
    code = getattr(exc, "code", None)
    return (
        f"Secret Manager could not {action} {secret_id!r} "
        f"({type(exc).__name__}{f' {code}' if code else ''}). "
        "Check ADC (gcloud auth application-default login) and IAM bindings."
    )
