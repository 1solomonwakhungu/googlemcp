import argparse
import io
import json
import types
from pathlib import Path
from typing import Any

import pytest

import googlemcp
from conftest import MemoryStore, NullContext
from googlemcp import cli
from googlemcp.config import load_settings
from googlemcp.secretstore import SecretKey


@pytest.fixture
def file_backend(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "secrets.json"
    monkeypatch.setenv("GOOGLEMCP_SECRET_BACKEND", "file")
    monkeypatch.setenv("GOOGLEMCP_SECRET_FILE", str(path))
    return path


def run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = cli.main(argv)
    out, err = capsys.readouterr()
    return code, out, err


def test_scopes_prints_one_per_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GOOGLEMCP_PRODUCTS", "tasks")
    code, out, _ = run(["scopes"], capsys)
    assert code == 0
    assert out.split() == ["https://www.googleapis.com/auth/tasks.readonly"]
    monkeypatch.setenv("GOOGLEMCP_WRITE_PRODUCTS", "tasks")
    _, out, _ = run(["scopes", "--json"], capsys)
    assert "https://www.googleapis.com/auth/tasks" in json.loads(out)


def test_products_marks_missing_adapters(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Pin the adapter set so the test does not depend on which adapters exist.
    real = cli.discover_adapters()
    monkeypatch.setattr(cli, "discover_adapters", lambda: {"tasks": real["tasks"]})
    monkeypatch.setenv("GOOGLEMCP_PRODUCTS", "tasks,chat")
    code, out, _ = run(["products", "--json"], capsys)
    states = {row["key"]: row for row in json.loads(out)}
    assert code == 0
    assert states["tasks"]["active"]
    assert states["chat"]["enabled"] and not states["chat"]["has_adapter"]


def test_tools_lists_policy_filtered_tools(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GOOGLEMCP_PRODUCTS", "tasks")
    _, out, _ = run(["tools", "--json"], capsys)
    names = {row["name"] for row in json.loads(out)}
    assert "tasks_list_tasks" in names and "tasks_create_task" not in names


def test_invalid_config_exits_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GOOGLEMCP_PRODUCTS", "bogus")
    code, _, err = run(["scopes"], capsys)
    assert code == 2 and "unknown product keys" in err


def test_secret_set_from_stdin_and_list_never_prints_values(
    file_backend: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("cid.apps.googleusercontent.com\n"))
    code, out, _ = run(["secret", "set", "oauth-client-id", "--stdin"], capsys)
    assert code == 0 and "cid" not in out
    code, out, _ = run(["secret", "list", "--json"], capsys)
    listing = json.loads(out)
    assert listing["backend"] == "file"
    assert listing["present"]["oauth-client-id"] is True
    assert listing["present"]["refresh-token"] is False
    assert set(listing["present"]) == {"oauth-client-id", "oauth-client-secret", "refresh-token"}
    assert "cid" not in out
    run(["secret", "delete", "oauth-client-id"], capsys)
    _, out, _ = run(["secret", "list", "--json"], capsys)
    assert json.loads(out)["present"]["oauth-client-id"] is False


def test_token_command_is_gone(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        cli.main(["token", "generate"])
    assert info.value.code == 2
    assert "invalid choice: 'token'" in capsys.readouterr().err


def test_auth_status_without_secrets_fails_cleanly(
    file_backend: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = run(["auth", "status", "--json"], capsys)
    assert code == 1
    report = json.loads(out)
    assert report["refresh_token_present"] is False and report["backend"] == "file"


def test_auth_revoke_with_nothing_stored(
    file_backend: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = run(["auth", "revoke"], capsys)
    assert code == 0 and "nothing to revoke" in out


@pytest.mark.parametrize(
    "argv",
    [
        ["serve", "--transport", "http"],
        ["serve", "--host", "127.0.0.1"],
        ["serve", "--port", "8080"],
    ],
)
def test_serve_has_no_http_flags(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        cli.main(argv)
    assert info.value.code == 2


def test_serve_runs_stdio(monkeypatch: pytest.MonkeyPatch, file_backend: Path) -> None:
    from googlemcp import server

    seen: list[str] = []
    monkeypatch.setattr(server, "serve", lambda settings, store: seen.append(store.name))
    assert cli.main(["serve"]) == 0
    assert cli.main(["serve", "--transport", "stdio"]) == 0
    assert seen == ["file", "file"]


def test_removed_env_settings_fail_before_serving(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GOOGLEMCP_AUTH_MODE", "bearer")
    code, _, err = run(["serve"], capsys)
    assert code == 2
    assert (
        "GOOGLEMCP_AUTH_MODE is no longer supported: GoogleMCP runs locally over stdio only" in err
    )


# doctor


def test_doctor_reports_missing_credentials(file_backend: Path) -> None:
    settings = load_settings(products=["tasks"])
    checks, healthy = cli.run_doctor(settings, MemoryStore(), {}, probe=False)
    by_name = {c["check"]: c for c in checks}
    assert not healthy
    assert by_name["refresh token"]["status"] == "fail"
    assert by_name["products"]["status"] == "fail"
    assert "--client-secrets" in by_name["oauth client"]["detail"]
    assert "console.cloud.google.com" in by_name["oauth client"]["detail"]


def test_doctor_with_client_but_no_token_says_run_login(file_backend: Path) -> None:
    store = MemoryStore({SecretKey.OAUTH_CLIENT_ID: "id", SecretKey.OAUTH_CLIENT_SECRET: "s"})
    checks, healthy = cli.run_doctor(load_settings(products=["tasks"]), store, {}, probe=False)
    by_name = {c["check"]: c for c in checks}
    assert not healthy
    assert by_name["oauth client"]["status"] == "ok"
    assert "googlemcp auth login" in by_name["refresh token"]["detail"]


def test_doctor_probes_through_probes_module(
    monkeypatch: pytest.MonkeyPatch, account_store: MemoryStore, adapters: dict[str, Any]
) -> None:
    from googlemcp.auth import flow

    report = flow.AuthStatus(
        "memory",
        True,
        True,
        refresh_ok=True,
        granted_scopes=["scope.read"],
        missing_scopes=[],
        scopes_verified=True,
    )
    monkeypatch.setattr(flow, "status", lambda *a, **k: report)
    seen: dict[str, Any] = {}

    def run_probes(ctx: Any, products: list[str]) -> list[Any]:
        seen["products"] = list(products)
        result = types.SimpleNamespace
        return [
            result(product="tasks", ok=True, status="ok", detail="listed"),
            result(product="drive", ok=False, status="skipped", detail="no probe"),
            result(product="gmail", ok=False, status="permission", detail="denied"),
        ]

    module = types.ModuleType("googlemcp.probes")
    module.run_probes = run_probes  # type: ignore[attr-defined]
    monkeypatch.setitem(__import__("sys").modules, "googlemcp.probes", module)

    settings = load_settings(products=["tasks", "drive"])
    checks, healthy = cli.run_doctor(
        settings, account_store, adapters, probe=True, ctx=NullContext()
    )
    statuses = {c["check"]: c["status"] for c in checks}
    assert seen["products"] == ["drive", "tasks"]
    assert statuses["probe tasks"] == "ok"
    assert statuses["probe drive"] == "warn"
    assert statuses["probe gmail"] == "fail"
    assert not healthy


def test_doctor_without_probes_module_warns(
    monkeypatch: pytest.MonkeyPatch, account_store: MemoryStore, adapters: dict[str, Any]
) -> None:
    from googlemcp.auth import flow

    report = flow.AuthStatus(
        "memory",
        True,
        True,
        refresh_ok=True,
        granted_scopes=["scope.read"],
        missing_scopes=[],
        scopes_verified=True,
    )
    monkeypatch.setattr(flow, "status", lambda *a, **k: report)
    monkeypatch.setitem(__import__("sys").modules, "googlemcp.probes", None)
    checks, healthy = cli.run_doctor(
        load_settings(products=["tasks"]), account_store, adapters, probe=True, ctx=NullContext()
    )
    assert healthy
    assert {
        "check": "probe all",
        "status": "warn",
        "detail": "probes module not installed",
    } in checks


def test_module_entrypoint_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        cli.main(["--version"])
    assert info.value.code == 0
    assert capsys.readouterr().out.strip() == f"googlemcp {googlemcp.__version__}"


def test_version_comes_from_installed_metadata() -> None:
    from importlib.metadata import version

    assert googlemcp.__version__ == version("googlemcp")


def test_version_falls_back_when_not_installed() -> None:
    assert googlemcp._resolve_version("googlemcp-not-installed") == googlemcp.UNKNOWN_VERSION


def _all_parsers(parser: argparse.ArgumentParser) -> list[argparse.ArgumentParser]:
    found = [parser]
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for child in action.choices.values():
                found += _all_parsers(child)
    return found


def test_help_text_is_client_and_account_neutral() -> None:
    parsers = _all_parsers(cli.build_parser())
    assert len(parsers) > 10
    for parser in parsers:
        text = parser.format_help().lower()
        assert "hermes" not in text and "owner" not in text, parser.prog
        assert "@gmail.com" not in text, parser.prog


def test_login_hint_has_no_default() -> None:
    args = cli.build_parser().parse_args(["auth", "login"])
    assert args.login_hint is None and args.client_secrets is None


def test_secret_keys_offered_by_cli() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["secret", "set", "oauth-client-secret", "--stdin"])
    assert cli._SETTABLE[args.key] is SecretKey.OAUTH_CLIENT_SECRET
    assert set(cli._SETTABLE) == {"oauth-client-id", "oauth-client-secret", "refresh-token"}


@pytest.mark.parametrize(
    "key",
    ["mcp-bearer-token", "mcp-oauth-client-id", "mcp-oauth-client-secret", "mcp-jwt-signing-key"],
)
def test_mcp_secret_keys_are_gone(key: str) -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["secret", "set", key, "--stdin"])


# auth status exit codes


@pytest.fixture
def stored_account(file_backend: Path) -> Path:
    from googlemcp.secretstore import FileStore

    store = FileStore(file_backend)
    store.set(SecretKey.OAUTH_CLIENT_ID, "cid")
    store.set(SecretKey.OAUTH_CLIENT_SECRET, "csecret")
    store.set(SecretKey.REFRESH_TOKEN, "refresh-token-value")
    return file_backend


class _Provider:
    def __init__(self, store: Any) -> None:
        pass

    def force_refresh(self) -> Any:
        import datetime

        return types.SimpleNamespace(token="ya29.x", expiry=datetime.datetime(2030, 1, 1))


def _tokeninfo(monkeypatch: pytest.MonkeyPatch, status_code: int, payload: dict[str, Any]) -> None:
    from googlemcp.auth import flow

    class Session:
        def post(self, *args: Any, **kwargs: Any) -> Any:
            return types.SimpleNamespace(status_code=status_code, json=lambda: payload)

    monkeypatch.setattr(flow, "CredentialProvider", _Provider)
    monkeypatch.setattr(flow.requests, "Session", Session)


def test_auth_status_tokeninfo_503_exits_unverified(
    stored_account: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GOOGLEMCP_PRODUCTS", "tasks")
    _tokeninfo(monkeypatch, 503, {})
    code, out, err = run(["auth", "status"], capsys)
    assert code == cli.EXIT_UNVERIFIED == 3
    assert "UNVERIFIED" in out
    assert "could not be verified" in err
    code, out, _ = run(["auth", "status", "--json"], capsys)
    report = json.loads(out)
    assert code == 3
    assert report["scopes_verified"] is False and report["missing_scopes"] is None
    assert "refresh-token-value" not in out and "ya29" not in out


def test_auth_status_verified_scopes_exit_zero(
    stored_account: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GOOGLEMCP_PRODUCTS", "tasks")
    _tokeninfo(monkeypatch, 200, {"scope": "https://www.googleapis.com/auth/tasks.readonly"})
    code, _, _ = run(["auth", "status"], capsys)
    assert code == 0


def test_auth_status_missing_scope_exits_one(
    stored_account: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GOOGLEMCP_PRODUCTS", "tasks")
    _tokeninfo(monkeypatch, 200, {"scope": "openid"})
    code, out, _ = run(["auth", "status"], capsys)
    assert code == 1 and "tasks.readonly" in out


def test_doctor_fails_when_scopes_unverified(
    stored_account: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from googlemcp.secretstore import FileStore

    _tokeninfo(monkeypatch, 503, {})
    checks, healthy = cli.run_doctor(
        load_settings(products=["tasks"]), FileStore(stored_account), {}, probe=False
    )
    scopes = next(c for c in checks if c["check"] == "scopes")
    assert scopes["status"] == "fail" and "could not be verified" in scopes["detail"]
    assert not healthy


def test_auth_status_before_first_login_names_next_step(
    file_backend: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GOOGLEMCP_PRODUCTS", "tasks")
    code, out, err = run(["auth", "status"], capsys)
    assert code == cli.EXIT_FAIL
    assert "oauth client stored: no" in out
    assert "next step:" in err and "googlemcp auth login --client-secrets" in err


def test_auth_status_with_client_but_no_token_names_login(
    file_backend: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from googlemcp.secretstore import FileStore

    store = FileStore(file_backend)
    store.set(SecretKey.OAUTH_CLIENT_ID, "cid")
    store.set(SecretKey.OAUTH_CLIENT_SECRET, "csecret")
    monkeypatch.setenv("GOOGLEMCP_PRODUCTS", "tasks")
    code, _, err = run(["auth", "status"], capsys)
    assert code == cli.EXIT_FAIL
    assert "next step: Run `googlemcp auth login`" in err and "--client-secrets" not in err


def test_auth_login_without_client_file_is_actionable(
    file_backend: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GOOGLEMCP_PRODUCTS", "tasks")
    code, _, err = run(["auth", "login", "--no-browser"], capsys)
    assert code == cli.EXIT_FAIL
    assert "Desktop app" in err and "--client-secrets" in err


def test_auth_login_unreported_grant_exits_unverified(
    file_backend: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from googlemcp.auth import LoginResult, flow

    monkeypatch.setattr(flow, "login", lambda *a, **k: LoginResult(None, None))
    code, _, err = run(["auth", "login", "--no-browser"], capsys)
    assert code == 3 and "auth status" in err


def test_doctor_reports_stdio_and_no_http_checks(
    monkeypatch: pytest.MonkeyPatch, account_store: MemoryStore
) -> None:
    from googlemcp.auth import flow

    report = flow.AuthStatus(
        "memory", True, True, refresh_ok=True, missing_scopes=[], scopes_verified=True
    )
    monkeypatch.setattr(flow, "status", lambda *a, **k: report)
    checks, _ = cli.run_doctor(load_settings(products=["tasks"]), account_store, {}, probe=False)
    names = {c["check"] for c in checks}
    config = next(c for c in checks if c["check"] == "config")
    assert config["detail"].startswith("transport=stdio")
    assert "oauth state" not in names and "serving policy" not in names
    assert not any(n.startswith("secret mcp-") for n in names)
