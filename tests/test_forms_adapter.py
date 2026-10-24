from urllib.parse import parse_qs, urlparse

import pytest

from googlemcp.services.base import GoogleMCPError, UpstreamError
from googlemcp.services.forms import ADAPTER, MAX_ANSWER_CHARS
from googlemcp.testing import FakeContext, call, registry_for

FORM = {
    "formId": "f1",
    "info": {"title": "Survey", "documentTitle": "Survey doc", "description": "Tell us"},
    "responderUri": "https://docs.google.com/forms/d/e/x/viewform",
    "revisionId": "r1",
    "publishSettings": {"publishState": {"isPublished": True, "isAcceptingResponses": False}},
    "items": [
        {
            "itemId": "i1",
            "title": "Color?",
            "questionItem": {
                "question": {
                    "questionId": "q1",
                    "required": True,
                    "choiceQuestion": {
                        "type": "RADIO",
                        "options": [{"value": "Red"}, {"value": "Blue"}],
                    },
                }
            },
        },
        {
            "itemId": "i2",
            "title": "Why?",
            "questionItem": {"question": {"questionId": "q2", "textQuestion": {"paragraph": True}}},
        },
        {"itemId": "i3", "title": "Next", "pageBreakItem": {}},
    ],
}


def test_get_form_summarizes_items() -> None:
    ctx = FakeContext().respond(FORM)
    out = call(registry_for(ADAPTER, ctx), "forms_get_form", form_id="f1")

    assert out["title"] == "Survey"
    assert out["is_published"] is True and out["is_accepting_responses"] is False
    assert out["item_count"] == 3
    assert out["items"] == [
        {
            "item_id": "i1",
            "title": "Color?",
            "question_id": "q1",
            "required": True,
            "kind": "choiceQuestion",
            "choice_type": "RADIO",
            "options": ["Red", "Blue"],
        },
        {
            "item_id": "i2",
            "title": "Why?",
            "question_id": "q2",
            "required": False,
            "kind": "textQuestion",
            "paragraph": True,
        },
        {"item_id": "i3", "title": "Next", "kind": "pageBreakItem"},
    ]


def test_list_responses_filter_and_shape() -> None:
    ctx = FakeContext().respond(
        {
            "responses": [
                {
                    "responseId": "r1",
                    "createTime": "2026-10-01T00:00:00Z",
                    "lastSubmittedTime": "2026-10-01T00:00:01Z",
                    "answers": {
                        "q1": {"questionId": "q1", "textAnswers": {"answers": [{"value": "Red"}]}},
                        "q9": {
                            "questionId": "q9",
                            "fileUploadAnswers": {
                                "answers": [{"fileId": "F", "fileName": "a.pdf", "mimeType": "x"}]
                            },
                        },
                    },
                }
            ],
            "nextPageToken": "n2",
        }
    )
    out = call(
        registry_for(ADAPTER, ctx),
        "forms_list_responses",
        form_id="f1",
        page_size=9999,
        submitted_after="2026-09-30T00:00:00Z",
    )
    assert out["next_page_token"] == "n2"
    assert out["responses"][0]["answers"] == {
        "q1": ["Red"],
        "q9": [{"file_id": "F", "file_name": "a.pdf"}],
    }
    q = parse_qs(urlparse(ctx.requests[0].uri).query)
    assert q["pageSize"] == ["100"]
    assert q["filter"] == ["timestamp > 2026-09-30T00:00:00Z"]


def test_get_response() -> None:
    ctx = FakeContext().respond({"responseId": "r1", "respondentEmail": "a@b.c", "answers": {}})
    out = call(registry_for(ADAPTER, ctx), "forms_get_response", form_id="f1", response_id="r1")
    assert out["respondent_email"] == "a@b.c"
    assert "/v1/forms/f1/responses/r1" in ctx.requests[0].uri


def test_create_form_unpublished_by_default() -> None:
    ctx = FakeContext().respond(
        {"formId": "f2", "info": {"title": "T"}, "responderUri": "u", "revisionId": "r"}
    )
    out = call(registry_for(ADAPTER, ctx), "forms_create_form", title="T")
    assert ctx.requests[0].body == {"info": {"title": "T"}}
    assert parse_qs(urlparse(ctx.requests[0].uri).query)["unpublished"] == ["true"]
    assert out["form_id"] == "f2"


def test_add_question_appends_after_existing_items() -> None:
    ctx = (
        FakeContext()
        .respond(FORM)
        .respond({"replies": [{"createItem": {"itemId": "i4", "questionId": ["q4"]}}]})
    )
    out = call(
        registry_for(ADAPTER, ctx),
        "forms_add_question",
        form_id="f1",
        title="Size?",
        kind="dropdown",
        options=["S", "M"],
        required=True,
    )
    assert out == {"form_id": "f1", "item_id": "i4", "question_ids": ["q4"], "index": 3}
    assert ctx.requests[0].method == "GET"
    assert ctx.requests[1].uri.endswith("/v1/forms/f1:batchUpdate?alt=json")
    assert ctx.requests[1].body == {
        "requests": [
            {
                "createItem": {
                    "item": {
                        "title": "Size?",
                        "questionItem": {
                            "question": {
                                "required": True,
                                "choiceQuestion": {
                                    "type": "DROP_DOWN",
                                    "options": [{"value": "S"}, {"value": "M"}],
                                },
                            }
                        },
                    },
                    "location": {"index": 3},
                }
            }
        ],
        "includeFormInResponse": False,
    }


def test_add_question_with_index_skips_lookup() -> None:
    ctx = FakeContext().respond({"replies": [{"createItem": {"itemId": "i5"}}]})
    call(
        registry_for(ADAPTER, ctx),
        "forms_add_question",
        form_id="f1",
        title="Notes",
        kind="paragraph",
        index=0,
    )
    assert len(ctx.requests) == 1
    item = ctx.requests[0].body["requests"][0]["createItem"]
    assert item["item"]["questionItem"]["question"]["textQuestion"] == {"paragraph": True}


def test_validation() -> None:
    ctx = FakeContext()
    reg = registry_for(ADAPTER, ctx)
    with pytest.raises(GoogleMCPError):
        call(reg, "forms_add_question", form_id="f1", title="x", kind="checkboxes")
    with pytest.raises(GoogleMCPError):
        call(reg, "forms_add_question", form_id="f1", title="x", kind="slider")
    assert ctx.requests == []


def test_write_flags() -> None:
    reg = registry_for(ADAPTER, FakeContext())
    assert {t.name for t in reg.tools if t.write} == {"forms_create_form", "forms_add_question"}
    assert not any(t.destructive for t in reg.tools)


def test_upstream_error_maps() -> None:
    ctx = FakeContext().respond({"error": {"code": 403}}, status=403)
    with pytest.raises(UpstreamError) as info:
        call(registry_for(ADAPTER, ctx), "forms_list_responses", form_id="f1")
    assert info.value.status == 403


def test_long_text_answers_are_flagged() -> None:
    ctx = FakeContext().respond(
        {
            "responseId": "r1",
            "answers": {
                "q1": {"textAnswers": {"answers": [{"value": "a" * (MAX_ANSWER_CHARS + 1)}]}},
                "q2": {"textAnswers": {"answers": [{"value": "short"}]}},
            },
        }
    )
    out = call(registry_for(ADAPTER, ctx), "forms_get_response", form_id="f1", response_id="r1")
    assert out["truncated"] is True
    assert out["truncated_question_ids"] == ["q1"]
    assert len(out["answers"]["q1"][0]) == MAX_ANSWER_CHARS
    assert out["answers"]["q2"] == ["short"]


def test_short_answers_are_not_flagged() -> None:
    ctx = FakeContext().respond(
        {"responseId": "r1", "answers": {"q1": {"textAnswers": {"answers": [{"value": "ok"}]}}}}
    )
    out = call(registry_for(ADAPTER, ctx), "forms_get_response", form_id="f1", response_id="r1")
    assert out["truncated"] is False
    assert out["truncated_question_ids"] == []
