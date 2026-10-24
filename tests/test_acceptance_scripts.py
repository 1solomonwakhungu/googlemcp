"""Offline guards for the acceptance tooling (scripts/acceptance, tests/live)."""

import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from googlemcp.catalog import load_catalog
from googlemcp.services import discover_adapters
from googlemcp.services.base import ToolRegistry

ROOT = Path(__file__).resolve().parents[1]
PREAUTH = ROOT / "scripts" / "acceptance" / "preauth.sh"
LIVE_DIR = ROOT / "tests" / "live"
LIVE_MODULE = LIVE_DIR / "test_live_read_tools.py"

# Verbs that would mean a tool mutates Google state.
WRITE_VERBS = re.compile(
    r"_(create|update|delete|send|trash|modify|upload|run|add|append|insert|replace|complete|"
    r"patch|move|copy|remove|set|import|batch)_"
)
# CLI commands that read the secret store or call Google.
CREDENTIAL_COMMANDS = re.compile(r"googlemcp\s+(auth|secret|token|doctor|serve|tools)\b")


def _load_live() -> ModuleType:
    spec = importlib.util.spec_from_file_location("live_read_tools", LIVE_MODULE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


live = _load_live()


class _NoIOContext:
    def client(self, api: str, version: str) -> Any:
        raise AssertionError("registration must not call Google")

    def execute(self, request: Any) -> dict[str, Any]:
        raise AssertionError("registration must not call Google")

    def authorized_session(self) -> Any:
        raise AssertionError("registration must not call Google")


def _registered_tools() -> dict[str, tuple[str, bool, bool]]:
    tools: dict[str, tuple[str, bool, bool]] = {}
    for key, adapter in discover_adapters().items():
        registry = ToolRegistry()
        adapter.register(registry, _NoIOContext())
        for tool in registry.tools:
            tools[tool.name] = (key, tool.write, tool.destructive)
    return tools


BASH = shutil.which("bash")
SHELLCHECK = shutil.which("shellcheck")


@pytest.mark.skipif(BASH is None, reason="bash not installed")
def test_preauth_script_syntax() -> None:
    assert os.access(PREAUTH, os.X_OK), "preauth.sh must be executable"
    # Fixed argv on a repository file; no untrusted input.
    subprocess.run([str(BASH), "-n", str(PREAUTH)], check=True)  # noqa: S603


@pytest.mark.skipif(SHELLCHECK is None, reason="shellcheck not installed")
def test_preauth_script_shellcheck() -> None:
    subprocess.run([str(SHELLCHECK), "--severity=style", str(PREAUTH)], check=True)  # noqa: S603


def test_preauth_script_never_touches_credentials() -> None:
    text = PREAUTH.read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert not CREDENTIAL_COMMANDS.search(code)
    assert "unset GOOGLEMCP_LIVE" in code
    assert "GOOGLEMCP_LIVE=1" not in code


def test_preauth_never_skips_repository_checks() -> None:
    """Core, inventory, and Terraform are on main: a missing piece is a FAIL.
    SKIP is reserved for optional tools (terraform, gitleaks, gh) and flags."""
    text = PREAUTH.read_text(encoding="utf-8")
    assert "not on this branch" not in text
    skips = re.findall(r'record "([^"]+)" SKIP', text)
    assert set(skips) <= {"terraform verify", "secret scan (gitleaks)", "ci on main"}, skips
    assert 'run_check "cli smoke (help, products, scopes)" cli_smoke' in text


def test_live_suite_is_skipped_without_flag() -> None:
    env = {k: v for k, v in os.environ.items() if k != "GOOGLEMCP_LIVE"}
    proc = subprocess.run(  # noqa: S603 - fixed argv, current interpreter
        [sys.executable, "-m", "pytest", str(LIVE_DIR), "-p", "no:cacheprovider", "-rs"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    summary = proc.stdout.strip().splitlines()[-1]
    assert proc.returncode == 0, proc.stdout[-2000:]
    assert "skipped" in summary
    assert not re.search(r"\b(passed|failed|error)", summary), summary
    assert "GOOGLEMCP_LIVE=1" in proc.stdout


def test_live_suite_fails_without_credentials_when_enabled(tmp_path: Path) -> None:
    """GOOGLEMCP_LIVE=1 with an empty store must fail, not skip. No network:
    the fixture stops before building the server or refreshing a token."""
    env = {
        **os.environ,
        "GOOGLEMCP_LIVE": "1",
        "GOOGLEMCP_SECRET_BACKEND": "file",
        "GOOGLEMCP_SECRET_FILE": str(tmp_path / "missing-secrets.json"),
    }
    proc = subprocess.run(  # noqa: S603 - fixed argv, current interpreter
        [sys.executable, "-m", "pytest", str(LIVE_DIR), "-p", "no:cacheprovider", "-rs"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    summary = proc.stdout.strip().splitlines()[-1]
    assert proc.returncode != 0, proc.stdout[-2000:]
    assert "no stored Google credentials" in proc.stdout
    assert "passed" not in summary and "skipped" not in summary, summary


def test_live_skips_are_only_the_documented_ones() -> None:
    source = LIVE_MODULE.read_text(encoding="utf-8")
    assert "importorskip" not in source
    reasons = re.findall(r"pytest\.skip\(\s*f?\"([^\"]*)", source)
    allowed = (
        "set {env_name}",
        "no {call.product} file",
        "YouTube Analytics needs",
        "is not enabled",
    )
    assert reasons and all(r.startswith(allowed) or allowed[3] in r for r in reasons), reasons


def test_live_tests_are_marked_live() -> None:
    marks = live.pytestmark if isinstance(live.pytestmark, list) else [live.pytestmark]
    assert "live" in {mark.name for mark in marks}


def test_live_plan_covers_every_default_enabled_product_once() -> None:
    planned = [call.product for call in live.LIVE_READ_CALLS]
    assert len(planned) == len(set(planned))
    defaults = {key for key, product in load_catalog().items() if product.default_enabled}
    assert set(planned) == defaults


def test_live_plan_uses_only_read_tools() -> None:
    registered = _registered_tools()
    for call in live.LIVE_READ_CALLS:
        assert call.tool.startswith(f"{call.product}_"), call.tool
        assert not WRITE_VERBS.search(call.tool + "_"), call.tool
        if call.product in discover_adapters():
            assert call.tool in registered, f"{call.tool} is not registered by {call.product}"
            _, write, destructive = registered[call.tool]
            assert not write and not destructive, f"{call.tool} is a write tool"


def test_live_helper_lookups_are_read_tools() -> None:
    """Tools the live suite calls besides the plan (file-id and channel lookups)."""
    registered = _registered_tools()
    for helper in ("drive_search_files", "youtube_list_my_channels"):
        assert not WRITE_VERBS.search(helper + "_")
        if helper in registered:
            _, write, destructive = registered[helper]
            assert not write and not destructive, helper
    source = LIVE_MODULE.read_text(encoding="utf-8")
    called = set(re.findall(r'\.call\(\s*"([a-z_]+)"', source))
    assert called <= {"drive_search_files", "youtube_list_my_channels"}, called


def test_live_settings_force_read_only() -> None:
    source = LIVE_MODULE.read_text(encoding="utf-8")
    assert "write_products=[]" in source
    assert "allow_destructive=False" in source


def _slug(heading: str) -> str:
    """GitHub-style heading anchor."""
    text = re.sub(r"[^\w\- ]", "", heading.strip().lower())
    return text.replace(" ", "-")


def test_runbook_links_resolve() -> None:
    doc = ROOT / "docs" / "acceptance.md"
    links = re.findall(r"\]\(([^)\s]+)\)", doc.read_text(encoding="utf-8"))
    relative = [link for link in links if not link.startswith(("http://", "https://", "#"))]
    assert relative
    for link in relative:
        path, _, anchor = link.partition("#")
        target = (doc.parent / path).resolve()
        assert target.is_file(), f"{link}: {target} missing"
        if anchor:
            headings = re.findall(r"^#+\s+(.*)$", target.read_text(encoding="utf-8"), re.MULTILINE)
            assert anchor in {_slug(h) for h in headings}, f"{link}: no such heading"


VERIFIER = ROOT / "scripts" / "acceptance" / "verify_stored_client.py"


def _load_verifier() -> ModuleType:
    spec = importlib.util.spec_from_file_location("verify_stored_client", VERIFIER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


verifier = _load_verifier()


def _client_json(tmp_path: Path, kind: str, client_id: str, secret: str) -> Path:
    path = tmp_path / f"client_secret_{kind}.json"
    path.write_text(
        f'{{"{kind}": {{"client_id": "{client_id}", "client_secret": "{secret}"}}}}',
        encoding="utf-8",
    )
    return path


@pytest.fixture
def file_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    from googlemcp.secretstore.backends import FileStore

    secrets = tmp_path / "store" / "secrets.json"
    monkeypatch.setenv("GOOGLEMCP_SECRET_BACKEND", "file")
    monkeypatch.setenv("GOOGLEMCP_SECRET_FILE", str(secrets))
    return FileStore(secrets)


def test_verifier_accepts_matching_desktop_client(
    tmp_path: Path, file_store: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    from googlemcp.secretstore import SecretKey

    file_store.set(SecretKey.OAUTH_CLIENT_ID, "id-123.apps.googleusercontent.com")
    file_store.set(SecretKey.OAUTH_CLIENT_SECRET, "GOCSPX-new-secret")
    path = _client_json(
        tmp_path, "installed", "id-123.apps.googleusercontent.com", "GOCSPX-new-secret"
    )

    assert verifier.main(["--json", str(path)]) == 0
    out = capsys.readouterr()
    assert "GOCSPX" not in out.out + out.err and "id-123" not in out.out + out.err


def test_verifier_rejects_old_secret_after_rotation(
    tmp_path: Path, file_store: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    from googlemcp.secretstore import SecretKey

    file_store.set(SecretKey.OAUTH_CLIENT_ID, "id-123.apps.googleusercontent.com")
    file_store.set(SecretKey.OAUTH_CLIENT_SECRET, "GOCSPX-old-secret")
    path = _client_json(
        tmp_path, "installed", "id-123.apps.googleusercontent.com", "GOCSPX-new-secret"
    )

    assert verifier.main(["--json", str(path)]) == 1
    err = capsys.readouterr().err
    assert "does not match the client secret" in err and "KEEP the file" in err
    assert "GOCSPX" not in err


def test_verifier_rejects_web_client_json(
    tmp_path: Path, file_store: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """Local-only: there is no Web OAuth client, so a web JSON is unusable."""
    path = _client_json(tmp_path, "web", "web-id", "web-secret")
    assert verifier.main(["--json", str(path)]) == 2
    assert "Desktop OAuth client JSON" in capsys.readouterr().err


def test_verifier_cli_contract_used_by_manual_steps(tmp_path: Path, file_store: Any) -> None:
    """docs/manual-steps.md pins: --json FILE [--secret-backend NAME], exit 0 on match."""
    from googlemcp.secretstore import SecretKey

    file_store.set(SecretKey.OAUTH_CLIENT_ID, "id-9")
    file_store.set(SecretKey.OAUTH_CLIENT_SECRET, "secret-9")
    path = _client_json(tmp_path, "installed", "id-9", "secret-9")
    assert verifier.main(["--json", str(path), "--secret-backend", "file"]) == 0
    assert VERIFIER == ROOT / "scripts" / "acceptance" / "verify_stored_client.py"


def test_verifier_rejects_unusable_file(tmp_path: Path, file_store: Any) -> None:
    bad = tmp_path / "not-a-client.json"
    bad.write_text('{"type": "service_account"}', encoding="utf-8")
    assert verifier.main(["--json", str(bad)]) == 2
    assert verifier.main(["--json", str(tmp_path / "missing.json")]) == 2


def test_runbook_deletes_client_json_only_after_verification() -> None:
    text = (ROOT / "docs" / "acceptance.md").read_text(encoding="utf-8")
    commands = re.sub(r"\\\n\s*", " ", text)  # join shell line continuations
    for line in commands.splitlines():
        if re.search(r"\brm\b.*CLIENT_JSON|\brm\b.*client_secret", line):
            assert "verify_stored_client.py" in line, line
