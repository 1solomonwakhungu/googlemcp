from urllib.parse import parse_qs, urlparse

import pytest

from googlemcp.services.base import GoogleMCPError, UpstreamError
from googlemcp.services.slides import ADAPTER, MAX_SLIDE_CHARS
from googlemcp.testing import FakeContext, call, registry_for


def _run(text: str) -> dict[str, object]:
    return {"textElements": [{"paragraphMarker": {}}, {"textRun": {"content": text}}]}


PRESENTATION = {
    "presentationId": "p1",
    "title": "Deck",
    "revisionId": "rev",
    "slides": [
        {
            "objectId": "s1",
            "slideProperties": {"layoutObjectId": "L1"},
            "pageElements": [
                {"objectId": "e1", "shape": {"text": _run("Title\n")}},
                {"objectId": "img", "image": {"contentUrl": "x"}},
                {
                    "objectId": "t1",
                    "table": {
                        "tableRows": [
                            {"tableCells": [{"text": _run("c1\n")}, {"text": _run("c2\n")}]}
                        ]
                    },
                },
            ],
        },
        {
            "objectId": "s2",
            "pageElements": [
                {
                    "objectId": "g",
                    "elementGroup": {
                        "children": [
                            {"objectId": "e2", "shape": {"text": _run("x" * (MAX_SLIDE_CHARS + 5))}}
                        ]
                    },
                }
            ],
        },
    ],
}


def test_get_presentation_summarizes_slides() -> None:
    ctx = FakeContext().respond(PRESENTATION)
    out = call(registry_for(ADAPTER, ctx), "slides_get_presentation", presentation_id="p1")

    assert out["title"] == "Deck" and out["slide_count"] == 2
    assert out["slides"][0] == {
        "object_id": "s1",
        "layout": "L1",
        "text": "Title\nc1\nc2\n",
        "truncated": False,
    }
    assert out["slides"][1]["truncated"] is True
    assert len(out["slides"][1]["text"]) == MAX_SLIDE_CHARS
    assert "/v1/presentations/p1" in ctx.requests[0].uri


def test_get_thumbnail_params() -> None:
    ctx = FakeContext().respond({"contentUrl": "https://lh3/x", "width": 800, "height": 450})
    out = call(
        registry_for(ADAPTER, ctx),
        "slides_get_thumbnail",
        presentation_id="p1",
        page_object_id="s1",
        size="LARGE",
    )
    assert out == {"content_url": "https://lh3/x", "width": 800, "height": 450}
    uri = ctx.requests[0].uri
    assert "/v1/presentations/p1/pages/s1/thumbnail" in uri
    q = parse_qs(urlparse(uri).query)
    assert q["thumbnailProperties.thumbnailSize"] == ["LARGE"]
    assert q["thumbnailProperties.mimeType"] == ["PNG"]


def test_create_presentation() -> None:
    ctx = FakeContext().respond(
        {"presentationId": "p2", "title": "New", "slides": [{"objectId": "p"}]}
    )
    out = call(registry_for(ADAPTER, ctx), "slides_create_presentation", title="New")
    assert ctx.requests[0].body == {"title": "New"}
    assert out == {"presentation_id": "p2", "title": "New", "slide_ids": ["p"]}


def test_replace_all_text_scoped_to_pages() -> None:
    ctx = FakeContext().respond({"replies": [{"replaceAllText": {"occurrencesChanged": 2}}]})
    out = call(
        registry_for(ADAPTER, ctx),
        "slides_replace_all_text",
        presentation_id="p1",
        find="{{name}}",
        replace_with="Ada",
        page_object_ids=["s1"],
    )
    assert out["occurrences_changed"] == 2
    assert ctx.requests[0].body == {
        "requests": [
            {
                "replaceAllText": {
                    "containsText": {"text": "{{name}}", "matchCase": True},
                    "replaceText": "Ada",
                    "pageObjectIds": ["s1"],
                }
            }
        ]
    }


def test_add_slide() -> None:
    ctx = FakeContext().respond({"replies": [{"createSlide": {"objectId": "new_slide"}}]})
    out = call(
        registry_for(ADAPTER, ctx),
        "slides_add_slide",
        presentation_id="p1",
        layout="BLANK",
        insertion_index=1,
    )
    assert out == {"presentation_id": "p1", "slide_object_id": "new_slide"}
    assert ctx.requests[0].uri.endswith("/v1/presentations/p1:batchUpdate?alt=json")
    assert ctx.requests[0].body == {
        "requests": [
            {
                "createSlide": {
                    "slideLayoutReference": {"predefinedLayout": "BLANK"},
                    "insertionIndex": 1,
                }
            }
        ]
    }


def test_validation() -> None:
    ctx = FakeContext()
    reg = registry_for(ADAPTER, ctx)
    with pytest.raises(GoogleMCPError):
        call(reg, "slides_add_slide", presentation_id="p1", layout="NOPE")
    with pytest.raises(GoogleMCPError):
        call(reg, "slides_get_thumbnail", presentation_id="p1", page_object_id="s1", size="HUGE")
    with pytest.raises(GoogleMCPError):
        call(reg, "slides_replace_all_text", presentation_id="p1", find="", replace_with="x")
    assert ctx.requests == []


def test_write_flags() -> None:
    reg = registry_for(ADAPTER, FakeContext())
    writes = {t.name for t in reg.tools if t.write}
    assert writes == {"slides_create_presentation", "slides_replace_all_text", "slides_add_slide"}
    assert not any(t.destructive for t in reg.tools)


def test_upstream_error_maps() -> None:
    ctx = FakeContext().respond({"error": {"code": 404}}, status=404)
    with pytest.raises(UpstreamError) as info:
        call(registry_for(ADAPTER, ctx), "slides_get_presentation", presentation_id="gone")
    assert info.value.status == 404
