import pytest

from googlemcp.services.base import GoogleMCPError, UpstreamError
from googlemcp.services.docs import ADAPTER, MAX_HEADING_CHARS, MAX_HEADINGS
from googlemcp.testing import FakeContext, call, registry_for

DOC = {
    "documentId": "d1",
    "title": "Plan",
    "revisionId": "r9",
    "body": {
        "content": [
            {"sectionBreak": {}},
            {
                "paragraph": {
                    "paragraphStyle": {"namedStyleType": "HEADING_1"},
                    "elements": [{"textRun": {"content": "Intro\n"}}],
                }
            },
            {
                "paragraph": {
                    "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                    "elements": [
                        {"textRun": {"content": "Hello "}},
                        {"textRun": {"content": "world\n"}},
                    ],
                }
            },
            {
                "table": {
                    "tableRows": [
                        {
                            "tableCells": [
                                {
                                    "content": [
                                        {
                                            "paragraph": {
                                                "elements": [{"textRun": {"content": "A1\n"}}]
                                            }
                                        }
                                    ]
                                },
                                {
                                    "content": [
                                        {
                                            "paragraph": {
                                                "elements": [{"textRun": {"content": "B1\n"}}]
                                            }
                                        }
                                    ]
                                },
                            ]
                        }
                    ]
                }
            },
        ]
    },
}


def test_get_document_returns_plain_text_and_structure() -> None:
    ctx = FakeContext().respond(DOC)
    out = call(registry_for(ADAPTER, ctx), "docs_get_document", document_id="d1")

    assert out["text"] == "Intro\nHello world\nA1\nB1\n"
    assert out["truncated"] is False
    assert out["structure"] == {
        "paragraphs": 4,
        "tables": 1,
        "heading_count": 1,
        "headings": [{"style": "HEADING_1", "text": "Intro", "truncated": False}],
    }
    assert out["title"] == "Plan" and out["revision_id"] == "r9"
    assert ctx.requests[0].method == "GET"
    assert "/v1/documents/d1" in ctx.requests[0].uri


def test_get_document_caps_text() -> None:
    ctx = FakeContext().respond(DOC)
    out = call(registry_for(ADAPTER, ctx), "docs_get_document", document_id="d1", max_chars=5)
    assert out["text"] == "Intro"
    assert out["truncated"] is True
    assert out["total_chars"] == len("Intro\nHello world\nA1\nB1\n")


def test_create_document_with_text_issues_insert() -> None:
    ctx = FakeContext().respond({"documentId": "new", "title": "T"}).respond({"replies": [{}]})
    out = call(registry_for(ADAPTER, ctx), "docs_create_document", title="T", text="Hi")

    assert out == {"document_id": "new", "title": "T", "text_inserted": True}
    assert ctx.requests[0].body == {"title": "T"}
    assert ctx.requests[1].uri.endswith("/v1/documents/new:batchUpdate?alt=json")
    assert ctx.requests[1].body == {
        "requests": [{"insertText": {"location": {"index": 1}, "text": "Hi"}}]
    }


def test_append_and_insert_bodies() -> None:
    ctx = FakeContext().respond({"replies": [{}]}).respond({"replies": [{}]})
    reg = registry_for(ADAPTER, ctx)
    call(reg, "docs_append_text", document_id="d1", text="tail")
    call(reg, "docs_insert_text", document_id="d1", text="head", index=3)

    assert ctx.requests[0].body == {
        "requests": [{"insertText": {"endOfSegmentLocation": {}, "text": "tail"}}]
    }
    assert ctx.requests[1].body == {
        "requests": [{"insertText": {"location": {"index": 3}, "text": "head"}}]
    }


def test_replace_all_text_reports_count() -> None:
    ctx = FakeContext().respond({"replies": [{"replaceAllText": {"occurrencesChanged": 3}}]})
    out = call(
        registry_for(ADAPTER, ctx),
        "docs_replace_all_text",
        document_id="d1",
        find="foo",
        replace_with="bar",
        match_case=False,
    )
    assert out == {"document_id": "d1", "occurrences_changed": 3}
    assert ctx.requests[0].body == {
        "requests": [
            {
                "replaceAllText": {
                    "containsText": {"text": "foo", "matchCase": False},
                    "replaceText": "bar",
                }
            }
        ]
    }


def test_write_flags() -> None:
    reg = registry_for(ADAPTER, FakeContext())
    flags = {t.name: (t.write, t.destructive) for t in reg.tools}
    assert flags == {
        "docs_get_document": (False, False),
        "docs_create_document": (True, False),
        "docs_append_text": (True, False),
        "docs_insert_text": (True, False),
        "docs_replace_all_text": (True, False),
    }


def test_input_validation() -> None:
    ctx = FakeContext()
    reg = registry_for(ADAPTER, ctx)
    with pytest.raises(GoogleMCPError):
        call(reg, "docs_insert_text", document_id="d1", text="x", index=0)
    with pytest.raises(GoogleMCPError):
        call(reg, "docs_replace_all_text", document_id="d1", find="", replace_with="x")
    assert ctx.requests == []


def test_upstream_error_maps() -> None:
    ctx = FakeContext().respond({"error": {"code": 404, "message": "not found"}}, status=404)
    with pytest.raises(UpstreamError) as info:
        call(registry_for(ADAPTER, ctx), "docs_get_document", document_id="missing")
    assert info.value.status == 404


def test_create_document_without_text_makes_one_call() -> None:
    ctx = FakeContext().respond({"documentId": "new", "title": "T"})
    out = call(registry_for(ADAPTER, ctx), "docs_create_document", title="T")
    assert out == {"document_id": "new", "title": "T"}
    assert len(ctx.requests) == 1


def test_create_document_keeps_id_when_insert_fails() -> None:
    ctx = (
        FakeContext()
        .respond({"documentId": "new", "title": "T"})
        .respond({"error": {"code": 500}}, status=500)
    )
    out = call(registry_for(ADAPTER, ctx), "docs_create_document", title="T", text="Hi")
    assert out["document_id"] == "new"
    assert out["text_inserted"] is False
    assert "document created but inserting text failed" in out["error"]
    assert "500" in out["error"]
    assert len(ctx.requests) == 2


def _heading(text: str) -> dict[str, object]:
    return {
        "paragraph": {
            "paragraphStyle": {"namedStyleType": "HEADING_2"},
            "elements": [{"textRun": {"content": text + "\n"}}],
        }
    }


def test_headings_are_capped_in_count_and_length() -> None:
    long_heading = "H" * 200_000
    doc = {
        "documentId": "d1",
        "body": {"content": [_heading(long_heading)] + [_heading("x")] * (MAX_HEADINGS + 5)},
    }
    ctx = FakeContext().respond(doc)
    out = call(registry_for(ADAPTER, ctx), "docs_get_document", document_id="d1", max_chars=10)

    structure = out["structure"]
    assert structure["heading_count"] == MAX_HEADINGS + 6
    assert len(structure["headings"]) == MAX_HEADINGS
    assert structure["headings"][0] == {
        "style": "HEADING_2",
        "text": "H" * MAX_HEADING_CHARS,
        "truncated": True,
    }
    assert len(str(structure)) < MAX_HEADINGS * (MAX_HEADING_CHARS + 100)
