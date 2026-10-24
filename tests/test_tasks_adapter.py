from urllib.parse import parse_qs, urlparse

import pytest

from googlemcp.services.base import GoogleMCPError, UpstreamError
from googlemcp.services.tasks import ADAPTER
from googlemcp.testing import FakeContext, call, registry_for


def test_list_tasks_builds_request_and_trims_fields() -> None:
    ctx = FakeContext().respond(
        {"items": [{"id": "t1", "title": "Buy milk", "etag": "x", "selfLink": "y"}]}
    )
    reg = registry_for(ADAPTER, ctx)

    out = call(reg, "tasks_list_tasks", max_results=500)

    assert out == {"tasks": [{"id": "t1", "title": "Buy milk"}], "next_page_token": None}
    req = ctx.requests[0]
    assert req.method == "GET"
    assert "/tasks/v1/lists/%40default/tasks" in req.uri
    assert "maxResults=100" in req.uri


def test_create_task_is_marked_write() -> None:
    reg = registry_for(ADAPTER, FakeContext())
    tool = next(t for t in reg.tools if t.name == "tasks_create_task")
    assert tool.write and not tool.destructive


def test_create_task_sends_body() -> None:
    ctx = FakeContext().respond({"id": "t2", "title": "Call"})
    reg = registry_for(ADAPTER, ctx)
    call(reg, "tasks_create_task", title="Call", notes="re: invoice")
    assert ctx.requests[0].method == "POST"
    assert ctx.requests[0].body == {"title": "Call", "notes": "re: invoice"}


def test_call_helper_allows_name_kwarg() -> None:
    from googlemcp.services.base import ToolRegistry
    from googlemcp.testing import call

    reg = ToolRegistry()
    reg.tool("x_echo", description="echo")(lambda name: {"name": name})
    assert call(reg, "x_echo", name="n") == {"name": "n"}


def test_get_task_trims_fields() -> None:
    ctx = FakeContext().respond({"id": "t1", "title": "Pay", "etag": "e", "links": []})
    out = call(registry_for(ADAPTER, ctx), "tasks_get_task", task_id="t1", tasklist_id="L1")
    assert out == {"id": "t1", "title": "Pay"}
    assert ctx.requests[0].method == "GET"
    assert "/tasks/v1/lists/L1/tasks/t1" in ctx.requests[0].uri


def test_update_task_patches_only_given_fields() -> None:
    ctx = FakeContext().respond({"id": "t1", "title": "New"})
    call(registry_for(ADAPTER, ctx), "tasks_update_task", task_id="t1", title="New")
    assert ctx.requests[0].method == "PATCH"
    assert ctx.requests[0].body == {"title": "New"}


def test_reopening_task_clears_completed() -> None:
    ctx = FakeContext().respond({"id": "t1", "status": "needsAction"})
    call(registry_for(ADAPTER, ctx), "tasks_update_task", task_id="t1", status="needsAction")
    assert ctx.requests[0].body == {"status": "needsAction", "completed": None}


def test_update_task_validation() -> None:
    ctx = FakeContext()
    reg = registry_for(ADAPTER, ctx)
    with pytest.raises(GoogleMCPError, match="status"):
        call(reg, "tasks_update_task", task_id="t1", status="done")
    with pytest.raises(GoogleMCPError, match="nothing"):
        call(reg, "tasks_update_task", task_id="t1")
    assert ctx.requests == []


def test_complete_task_sets_status() -> None:
    ctx = FakeContext().respond({"id": "t1", "status": "completed"})
    out = call(registry_for(ADAPTER, ctx), "tasks_complete_task", task_id="t1")
    assert out == {"id": "t1", "status": "completed"}
    assert ctx.requests[0].body == {"status": "completed"}


def test_delete_task_is_destructive() -> None:
    ctx = FakeContext().respond("", status=204)
    reg = registry_for(ADAPTER, ctx)
    out = call(reg, "tasks_delete_task", task_id="t1")
    assert out == {"deleted": True, "task_id": "t1", "tasklist_id": "@default"}
    assert ctx.requests[0].method == "DELETE"
    tool = next(t for t in reg.tools if t.name == "tasks_delete_task")
    assert tool.write and tool.destructive
    for name in ("tasks_update_task", "tasks_complete_task"):
        tool = next(t for t in reg.tools if t.name == name)
        assert tool.write and not tool.destructive
    assert not next(t for t in reg.tools if t.name == "tasks_get_task").write


def test_upstream_error_propagates() -> None:
    ctx = FakeContext().respond({"error": {"code": 404}}, status=404)
    with pytest.raises(UpstreamError) as info:
        call(registry_for(ADAPTER, ctx), "tasks_get_task", task_id="gone")
    assert info.value.status == 404


def test_show_completed_also_requests_hidden_tasks() -> None:
    ctx = FakeContext().respond({"items": []}).respond({"items": []})
    reg = registry_for(ADAPTER, ctx)
    call(reg, "tasks_list_tasks", show_completed=True)
    call(reg, "tasks_list_tasks")
    with_completed, default = (parse_qs(urlparse(r.uri).query) for r in ctx.requests)
    assert with_completed["showCompleted"] == ["true"]
    assert with_completed["showHidden"] == ["true"]
    assert default["showCompleted"] == ["false"]
    assert default["showHidden"] == ["false"]


@pytest.mark.parametrize(
    ("given", "sent"),
    [
        ("2026-10-07", "2026-10-07T00:00:00.000Z"),
        ("2026-10-07T15:30:00Z", "2026-10-07T15:30:00Z"),
        ("2026-10-07T15:30:00.5-04:00", "2026-10-07T15:30:00.5-04:00"),
    ],
)
def test_due_accepts_date_or_rfc3339(given: str, sent: str) -> None:
    ctx = FakeContext().respond({"id": "t1"})
    call(registry_for(ADAPTER, ctx), "tasks_create_task", title="Pay", due=given)
    assert ctx.requests[0].body == {"title": "Pay", "due": sent}


@pytest.mark.parametrize(
    "bad", ["tomorrow", "2026-W41-2", "20261007", "2026-02-30", "2026-10-07T10:00"]
)
def test_invalid_due_is_rejected_before_any_request(bad: str) -> None:
    ctx = FakeContext()
    reg = registry_for(ADAPTER, ctx)
    with pytest.raises(GoogleMCPError, match="due"):
        call(reg, "tasks_create_task", title="x", due=bad)
    with pytest.raises(GoogleMCPError, match="due"):
        call(reg, "tasks_update_task", task_id="t1", due=bad)
    assert ctx.requests == []


def test_update_with_empty_due_clears_it() -> None:
    ctx = FakeContext().respond({"id": "t1"})
    call(registry_for(ADAPTER, ctx), "tasks_update_task", task_id="t1", due="")
    assert ctx.requests[0].body == {"due": None}
