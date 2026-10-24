"""Static guardrails for infra/terraform that hold without Terraform installed.

`terraform test` (infra/terraform/tests) checks plan behavior with a mock
provider. These checks cover what a plan cannot express: resource types that
must never appear because they would leak secrets into state or create
long-lived credentials.
"""

import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TF_DIR = ROOT / "infra" / "terraform"
TF_FILES = sorted(p for p in TF_DIR.rglob("*.tf") if ".terraform" not in p.parts)
BLOCK_RE = re.compile(r'^\s*(resource|data)\s+"([a-z0-9_]+)"', re.MULTILINE)

SECRET_IN_STATE = "secret values must never be managed by Terraform"
FORBIDDEN_TYPES = {
    # Secret values in Terraform end up in plain text in state.
    "google_secret_manager_secret_version": SECRET_IN_STATE,
    "google_secret_manager_regional_secret_version": SECRET_IN_STATE,
    # Long-lived keys defeat keyless runtime identity.
    "google_service_account_key": "service account keys are forbidden; use attached identity",
    "google_apikeys_key": "API keys are not used; Google access is OAuth only",
    # Project-wide grants break least privilege; bind on the resource instead.
    "google_project_iam_member": "use resource-level IAM bindings",
    "google_project_iam_binding": "use resource-level IAM bindings",
    "google_project_iam_policy": "authoritative project policy can lock out the owner",
}


def _blocks() -> list[tuple[Path, str, str]]:
    found = []
    for path in TF_FILES:
        for kind, rtype in BLOCK_RE.findall(path.read_text(encoding="utf-8")):
            found.append((path, kind, rtype))
    return found


def test_terraform_sources_exist() -> None:
    assert TF_FILES, "infra/terraform has no .tf files"
    assert any(rtype == "google_project_service" for _, _, rtype in _blocks())


@pytest.mark.parametrize("rtype", sorted(FORBIDDEN_TYPES))
def test_forbidden_resource_types_absent(rtype: str) -> None:
    offenders = [str(p.relative_to(ROOT)) for p, _, t in _blocks() if t == rtype]
    assert not offenders, f"{rtype} in {offenders}: {FORBIDDEN_TYPES[rtype]}"


def test_no_secret_payload_attributes() -> None:
    pattern = re.compile(r"^\s*(secret_data|secret_data_wo|private_key)\s*=", re.MULTILINE)
    offenders = [str(p.relative_to(ROOT)) for p in TF_FILES if pattern.search(p.read_text())]
    assert not offenders, f"secret payload attributes in {offenders}"


def test_no_public_iam_members() -> None:
    offenders = [
        str(p.relative_to(ROOT))
        for p in TF_FILES
        if re.search(r'"(allUsers|allAuthenticatedUsers)"', p.read_text())
    ]
    assert not offenders, f"public IAM members in {offenders}; use invoker_iam_disabled"


HOSTING_TYPE_PREFIXES = (
    "google_cloud_run_",
    "google_cloudrun_",
    "google_artifact_registry_",
    "google_service_account",
    "google_compute_",
    "google_app_engine_",
    "google_cloudfunctions",
)
HOSTING_SERVICES = (
    "run.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
)


def test_no_hosting_resources() -> None:
    # The MCP server runs locally only (stdio). Nothing here may host it.
    offenders = [
        f"{p.relative_to(ROOT)}: {t}"
        for p, _, t in _blocks()
        if t.startswith(HOSTING_TYPE_PREFIXES)
    ]
    assert not offenders, f"hosting or runtime identity resources: {offenders}"


def test_no_hosting_apis_enabled() -> None:
    texts = {str(p.relative_to(ROOT)): p.read_text() for p in TF_FILES}
    texts["catalog/services.json"] = (ROOT / "catalog" / "services.json").read_text()
    offenders = [
        f"{name}: {svc}" for name, text in texts.items() for svc in HOSTING_SERVICES if svc in text
    ]
    assert not offenders, f"hosting APIs referenced: {offenders}"


def test_storage_buckets_only_for_terraform_state() -> None:
    state_module = TF_DIR / "modules" / "state_bucket"
    offenders = [
        str(p.relative_to(ROOT))
        for p, kind, t in _blocks()
        if kind == "resource" and t == "google_storage_bucket" and state_module not in p.parents
    ]
    assert not offenders, f"buckets outside modules/state_bucket: {offenders}"


def test_secret_containers_are_desktop_flow_only() -> None:
    text = (TF_DIR / "secrets.tf").read_text()
    assert not re.search(r"\bmcp[-_]", text), "no MCP server access secrets (mcp-*)"
    for key in ("oauth_client_id", "oauth_client_secret", "refresh_token"):
        assert key in text


def test_no_secret_write_roles() -> None:
    # The runtime reads secrets; values are written by the user's CLI or gcloud.
    pattern = re.compile(r"roles/secretmanager\.(secretVersionAdder|secretVersionManager|admin)")
    offenders = [str(p.relative_to(ROOT)) for p in TF_FILES if pattern.search(p.read_text())]
    assert not offenders, f"secret write roles granted in {offenders}"


def test_removed_iap_oauth_resources_absent() -> None:
    # Removed in hashicorp/google 8.0; the IAP OAuth Admin API shut down 2026-03-19.
    offenders = [t for _, _, t in _blocks() if t in {"google_iap_brand", "google_iap_client"}]
    assert not offenders


def test_api_services_are_never_disabled_on_destroy() -> None:
    for path in TF_FILES:
        text = path.read_text()
        for match in re.finditer(r'resource\s+"google_project_service"\s+"\w+"\s*\{', text):
            body = text[match.end() : text.find("\n}", match.end())]
            assert re.search(r"disable_on_destroy\s*=\s*false", body), path


def test_catalog_services_are_valid_names() -> None:
    catalog = json.loads((ROOT / "catalog" / "services.json").read_text())
    names = list(catalog["platform_services"])
    for product in catalog["products"].values():
        names.extend(product["api_services"])
    bad = [n for n in names if not re.fullmatch(r"[a-z0-9-]+\.googleapis\.com", n)]
    assert not bad


def test_no_state_or_real_tfvars_tracked() -> None:
    try:
        tracked = subprocess.run(
            ["git", "ls-files", "infra"],  # noqa: S607
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("git not available")
    bad = [
        f
        for f in tracked
        if f.endswith((".tfstate", ".tfstate.backup", ".tfplan"))
        or (f.endswith(".tfvars") and not f.endswith(".tfvars.example"))
        or "/.terraform/" in f
    ]
    assert not bad, f"state, plans, or real tfvars tracked: {bad}"
