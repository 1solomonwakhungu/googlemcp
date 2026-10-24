from urllib.parse import parse_qs, urlparse

import pytest

from googlemcp.services.appsscript import ADAPTER, MAX_RESULT_CHARS
from googlemcp.services.base import GoogleMCPError, UpstreamError
from googlemcp.testing import FakeContext, call, registry_for


def test_get_project() -> None:
    ctx = FakeContext().respond(
        {
            "scriptId": "S",
            "title": "Tools",
            "parentId": "sheet1",
            "updateTime": "t",
            "lastModifyUser": {"email": "owner@example.com", "photoUrl": "p"},
            "creator": {"email": "owner@example.com"},
        }
    )
    out = call(registry_for(ADAPTER, ctx), "appsscript_get_project", script_id="S")
    assert out == {
        "scriptId": "S",
        "title": "Tools",
        "parentId": "sheet1",
        "updateTime": "t",
        "last_modify_user": "owner@example.com",
    }
    assert urlparse(ctx.requests[0].uri).path == "/v1/projects/S"


def test_get_content_caps_source_and_lists_functions() -> None:
    ctx = FakeContext().respond(
        {
            "scriptId": "S",
            "files": [
                {
                    "name": "Code",
                    "type": "SERVER_JS",
                    "source": "function a(){}\nfunction b(){}",
                    "functionSet": {"values": [{"name": "a"}, {"name": "b"}]},
                },
                {"name": "appsscript", "type": "JSON", "source": "{}"},
            ],
        }
    )
    out = call(
        registry_for(ADAPTER, ctx),
        "appsscript_get_content",
        script_id="S",
        version_number=3,
        max_chars_per_file=10,
    )
    code = out["files"][0]
    assert code["functions"] == ["a", "b"]
    assert code["source"] == "function a"
    assert code["truncated"] is True
    assert out["files"][1] == {
        "name": "appsscript",
        "type": "JSON",
        "functions": [],
        "source": "{}",
        "truncated": False,
    }
    assert parse_qs(urlparse(ctx.requests[0].uri).query)["versionNumber"] == ["3"]


def test_list_deployments_entry_points() -> None:
    ctx = FakeContext().respond(
        {
            "deployments": [
                {
                    "deploymentId": "D1",
                    "deploymentConfig": {"versionNumber": 2, "description": "api"},
                    "updateTime": "t",
                    "entryPoints": [
                        {
                            "entryPointType": "EXECUTION_API",
                            "executionApi": {"entryPointConfig": {"access": "MYSELF"}},
                        },
                        {
                            "entryPointType": "WEB_APP",
                            "webApp": {
                                "url": "https://script.google.com/x",
                                "entryPointConfig": {"access": "MYSELF"},
                            },
                        },
                    ],
                }
            ]
        }
    )
    out = call(registry_for(ADAPTER, ctx), "appsscript_list_deployments", script_id="S")
    assert out["deployments"][0] == {
        "deployment_id": "D1",
        "version_number": 2,
        "description": "api",
        "update_time": "t",
        "entry_points": [
            {"type": "EXECUTION_API", "access": "MYSELF"},
            {"type": "WEB_APP", "url": "https://script.google.com/x", "access": "MYSELF"},
        ],
    }


def test_list_versions() -> None:
    ctx = FakeContext().respond(
        {"versions": [{"scriptId": "S", "versionNumber": 1, "description": "v1"}]}
    )
    out = call(registry_for(ADAPTER, ctx), "appsscript_list_versions", script_id="S", page_size=0)
    assert out == {"versions": [{"versionNumber": 1, "description": "v1"}], "next_page_token": None}
    assert parse_qs(urlparse(ctx.requests[0].uri).query)["pageSize"] == ["1"]


def test_run_function_returns_result() -> None:
    ctx = FakeContext().respond(
        {
            "done": True,
            "response": {
                "@type": "type.googleapis.com/google.apps.script.v1.ExecutionResponse",
                "result": [1, 2],
            },
        }
    )
    out = call(
        registry_for(ADAPTER, ctx),
        "appsscript_run_function",
        deployment_id="D1",
        function="sum",
        parameters=[1, 1],
    )
    assert out == {"done": True, "result": [1, 2], "truncated": False}
    req = ctx.requests[0]
    assert req.method == "POST"
    assert req.uri.endswith("/v1/scripts/D1:run?alt=json")
    assert req.body == {"function": "sum", "devMode": False, "parameters": [1, 1]}


def test_run_function_truncates_large_result() -> None:
    ctx = FakeContext().respond(
        {"done": True, "response": {"result": "z" * (MAX_RESULT_CHARS + 10)}}
    )
    out = call(
        registry_for(ADAPTER, ctx), "appsscript_run_function", deployment_id="D1", function="big"
    )
    assert out["truncated"] is True
    assert len(out["result"]) == MAX_RESULT_CHARS


def test_run_function_surfaces_script_error() -> None:
    ctx = FakeContext().respond(
        {
            "done": True,
            "error": {
                "code": 3,
                "message": "ScriptError",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.apps.script.v1.ExecutionError",
                        "errorMessage": "TypeError: x is undefined",
                        "errorType": "ScriptError",
                        "scriptStackTraceElements": [{"function": "sum", "lineNumber": 4}],
                    }
                ],
            },
        }
    )
    out = call(
        registry_for(ADAPTER, ctx), "appsscript_run_function", deployment_id="D1", function="sum"
    )
    assert out == {
        "done": True,
        "error": {
            "code": 3,
            "message": "ScriptError",
            "script_error": "TypeError: x is undefined",
            "script_error_type": "ScriptError",
            "stack": [{"function": "sum", "lineNumber": 4}],
        },
    }


def test_run_function_is_write_and_destructive() -> None:
    reg = registry_for(ADAPTER, FakeContext())
    flags = {t.name: (t.write, t.destructive) for t in reg.tools}
    assert flags.pop("appsscript_run_function") == (True, True)
    assert all(f == (False, False) for f in flags.values())
    assert ADAPTER.spec.write_scopes == ()


def test_validation() -> None:
    ctx = FakeContext()
    with pytest.raises(GoogleMCPError):
        call(
            registry_for(ADAPTER, ctx), "appsscript_run_function", deployment_id="D1", function=" "
        )
    assert ctx.requests == []


def test_upstream_error_maps() -> None:
    ctx = FakeContext().respond(
        {"error": {"code": 403, "message": "The caller does not have permission"}}, status=403
    )
    with pytest.raises(UpstreamError) as info:
        call(
            registry_for(ADAPTER, ctx), "appsscript_run_function", deployment_id="D1", function="f"
        )
    assert info.value.status == 403


def test_run_function_script_error_text_is_redacted_and_capped() -> None:
    leak = "Denied https://example.invalid/private?access_token='synthetic'"
    ctx = FakeContext().respond(
        {
            "done": True,
            "error": {
                "code": 3,
                "message": f"ScriptError {leak}",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.apps.script.v1.ExecutionError",
                        "errorMessage": f'Exception: {leak} {{"access_token": "synthetic"}} '
                        + "x" * 1000,
                        "errorType": "ScriptError",
                        "scriptStackTraceElements": [{"function": "sum", "lineNumber": 4}],
                    }
                ],
            },
        }
    )
    out = call(
        registry_for(ADAPTER, ctx), "appsscript_run_function", deployment_id="D1", function="sum"
    )
    error = out["error"]
    assert error["message"] == "ScriptError Denied <url>"
    assert error["script_error"].startswith(
        'Exception: Denied <url> {"access_token": "<redacted>"} x'
    )
    assert len(error["script_error"]) == 300
    assert "synthetic" not in str(out) and "example.invalid" not in str(out)
    assert error["stack"] == [{"function": "sum", "lineNumber": 4}]
