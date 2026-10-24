"""Google Apps Script API adapter.

Off by default in the catalog (risk: code execution). Read tools inspect
projects, source, deployments, and versions. ``appsscript_run_function`` calls
``scripts.run``, which executes arbitrary script code as the user, so it is
registered as both write and destructive.

Requirements Google imposes, none of which Terraform can satisfy:

- The per-user Apps Script API toggle at
  https://script.google.com/home/usersettings must be on (manual; see
  docs/manual-steps.md).
- For ``scripts.run``: "the script project must be deployed as an API
  executable", the caller must "provide a properly scoped OAuth token", and
  the script and the calling OAuth client must "share a common Google Cloud
  project", which must be a standard Cloud project rather than the default
  one Apps Script creates. "This OAuth token must cover all the scopes used
  by the script, not just the ones used by the called function."
  Reference: https://developers.google.com/apps-script/api/how-tos/execute
- Only basic types (strings, numbers, booleans, arrays, objects) cross the
  API boundary, and executions can run up to six minutes.

Scopes: ``script.projects.readonly`` and ``script.deployments.readonly`` for
reads. This adapter requests no extra write scope: ``scripts.run`` needs the
scopes of the target script (for example ``spreadsheets``), so a run succeeds
only when those scopes were already granted through other enabled products.
"""

from __future__ import annotations

import json
from typing import Any

from googlemcp.redaction import redact
from googlemcp.services.base import (
    JSON,
    GoogleMCPError,
    ServiceContext,
    ServiceSpec,
    ToolRegistry,
    clamp_page_size,
    pick,
)

SPEC = ServiceSpec(
    key="appsscript",
    title="Apps Script",
    api_services=("script.googleapis.com",),
    read_scopes=(
        "https://www.googleapis.com/auth/script.projects.readonly",
        "https://www.googleapis.com/auth/script.deployments.readonly",
    ),
)

DEFAULT_MAX_SOURCE_CHARS = 20_000
MAX_SOURCE_CHARS_CEILING = 100_000
# Cap for error strings copied from scripts.run Operation errors.
ERROR_TEXT_LIMIT = 300
MAX_RESULT_CHARS = 50_000


def _clamp_chars(value: int | None) -> int:
    if value is None:
        return DEFAULT_MAX_SOURCE_CHARS
    return max(1, min(int(value), MAX_SOURCE_CHARS_CEILING))


def _deployment(d: JSON) -> JSON:
    config = d.get("deploymentConfig", {})
    entry_points = []
    for ep in d.get("entryPoints", []):
        kind = ep.get("entryPointType")
        item: JSON = {"type": kind}
        if kind == "EXECUTION_API":
            item["access"] = ep.get("executionApi", {}).get("entryPointConfig", {}).get("access")
        elif kind == "WEB_APP":
            item["url"] = ep.get("webApp", {}).get("url")
            item["access"] = ep.get("webApp", {}).get("entryPointConfig", {}).get("access")
        entry_points.append(item)
    return {
        "deployment_id": d.get("deploymentId"),
        "version_number": config.get("versionNumber"),
        "description": config.get("description"),
        "update_time": d.get("updateTime"),
        "entry_points": entry_points,
    }


def _bounded(value: Any) -> tuple[Any, bool]:
    """Return ``value`` unchanged if its JSON form is small, else a truncated string."""
    encoded = json.dumps(value, default=str)
    if len(encoded) <= MAX_RESULT_CHARS:
        return value, False
    return encoded[:MAX_RESULT_CHARS], True


def _error_text(value: Any) -> str | None:
    """Script and Google error text can quote URLs or tokens; scrub and cap it."""
    if value is None:
        return None
    return redact(str(value))[:ERROR_TEXT_LIMIT]


def _script_error(error: JSON) -> JSON:
    out: JSON = {"code": error.get("code"), "message": _error_text(error.get("message"))}
    for detail in error.get("details", []):
        if "errorMessage" in detail:
            out["script_error"] = _error_text(detail.get("errorMessage"))
            out["script_error_type"] = _error_text(detail.get("errorType"))
            out["stack"] = [
                pick(frame, ("function", "lineNumber"))
                for frame in detail.get("scriptStackTraceElements", [])
            ]
            break
    return out


class AppsScriptAdapter:
    spec = SPEC

    def register(self, registry: ToolRegistry, ctx: ServiceContext) -> None:
        @registry.tool("appsscript_get_project")
        def get_project(script_id: str) -> JSON:
            """Get Apps Script project metadata (title, container parent, timestamps)."""
            svc = ctx.client("script", "v1")
            resp = ctx.execute(svc.projects().get(scriptId=script_id))
            out = pick(resp, ("scriptId", "title", "parentId", "createTime", "updateTime"))
            out["last_modify_user"] = resp.get("lastModifyUser", {}).get("email")
            return out

        @registry.tool("appsscript_get_content")
        def get_content(
            script_id: str,
            version_number: int | None = None,
            max_chars_per_file: int = DEFAULT_MAX_SOURCE_CHARS,
        ) -> JSON:
            """Get a project's files with source and function names.

            Reads the HEAD (latest saved) code unless ``version_number`` is set.
            Each file's source is capped at ``max_chars_per_file``.
            """
            limit = _clamp_chars(max_chars_per_file)
            svc = ctx.client("script", "v1")
            resp = ctx.execute(
                svc.projects().getContent(scriptId=script_id, versionNumber=version_number)
            )
            files = []
            for f in resp.get("files", []):
                source = f.get("source", "")
                files.append(
                    {
                        "name": f.get("name"),
                        "type": f.get("type"),
                        "functions": [
                            fn.get("name") for fn in f.get("functionSet", {}).get("values", [])
                        ],
                        "source": source[:limit],
                        "truncated": len(source) > limit,
                    }
                )
            return {"script_id": resp.get("scriptId", script_id), "files": files}

        @registry.tool("appsscript_list_deployments")
        def list_deployments(
            script_id: str, page_size: int = 25, page_token: str | None = None
        ) -> JSON:
            """List a project's deployments, including API executable entry points."""
            svc = ctx.client("script", "v1")
            resp = ctx.execute(
                svc.projects()
                .deployments()
                .list(scriptId=script_id, pageSize=clamp_page_size(page_size), pageToken=page_token)
            )
            return {
                "deployments": [_deployment(d) for d in resp.get("deployments", [])],
                "next_page_token": resp.get("nextPageToken"),
            }

        @registry.tool("appsscript_list_versions")
        def list_versions(
            script_id: str, page_size: int = 25, page_token: str | None = None
        ) -> JSON:
            """List a project's immutable versions."""
            svc = ctx.client("script", "v1")
            resp = ctx.execute(
                svc.projects()
                .versions()
                .list(scriptId=script_id, pageSize=clamp_page_size(page_size), pageToken=page_token)
            )
            return {
                "versions": [
                    pick(v, ("versionNumber", "description", "createTime"))
                    for v in resp.get("versions", [])
                ],
                "next_page_token": resp.get("nextPageToken"),
            }

        @registry.tool("appsscript_run_function", write=True, destructive=True)
        def run_function(
            deployment_id: str,
            function: str,
            parameters: list[Any] | None = None,
            dev_mode: bool = False,
        ) -> JSON:
            """Execute a function in an API executable deployment. Runs arbitrary code as the user.

            ``deployment_id`` comes from appsscript_list_deployments. ``dev_mode``
            runs the latest saved code instead of the deployed version (owner only).
            Script exceptions are returned under ``error``, not raised.
            """
            if not function.strip():
                raise GoogleMCPError("function must not be empty")
            body: JSON = {"function": function, "devMode": dev_mode}
            if parameters:
                body["parameters"] = parameters
            svc = ctx.client("script", "v1")
            op = ctx.execute(svc.scripts().run(scriptId=deployment_id, body=body))
            if "error" in op:
                return {"done": op.get("done", True), "error": _script_error(op["error"])}
            result, truncated = _bounded(op.get("response", {}).get("result"))
            return {"done": op.get("done", True), "result": result, "truncated": truncated}


ADAPTER = AppsScriptAdapter()
