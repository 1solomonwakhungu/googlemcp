"""Google Forms adapter.

``forms.create`` only accepts a title, so questions are added afterwards with
``forms.batchUpdate`` ``createItem`` requests. New forms are created
unpublished by default (``unpublished=true``) so they do not accept
responses until someone publishes them in the Forms UI.
Reference: https://developers.google.com/workspace/forms/api/reference/rest/v1/forms/create

Scopes: ``forms.body.readonly`` and ``forms.responses.readonly`` for reads,
``forms.body`` for writes.
"""

from __future__ import annotations

from googlemcp.services.base import (
    JSON,
    GoogleMCPError,
    ServiceContext,
    ServiceSpec,
    ToolRegistry,
    clamp_page_size,
)

SPEC = ServiceSpec(
    key="forms",
    title="Google Forms",
    api_services=("forms.googleapis.com",),
    read_scopes=(
        "https://www.googleapis.com/auth/forms.body.readonly",
        "https://www.googleapis.com/auth/forms.responses.readonly",
    ),
    write_scopes=("https://www.googleapis.com/auth/forms.body",),
)

MAX_ITEMS = 200
MAX_ANSWER_CHARS = 2_000
_CHOICE_KINDS = {"multiple_choice": "RADIO", "checkboxes": "CHECKBOX", "dropdown": "DROP_DOWN"}
_TEXT_KINDS = {"short_text": False, "paragraph": True}
_QUESTION_TYPES = (
    "choiceQuestion",
    "textQuestion",
    "scaleQuestion",
    "dateQuestion",
    "timeQuestion",
    "fileUploadQuestion",
    "rowQuestion",
    "ratingQuestion",
)


def summarize_item(item: JSON) -> JSON:
    out: JSON = {"item_id": item.get("itemId"), "title": item.get("title")}
    question = item.get("questionItem", {}).get("question")
    if question is not None:
        out["question_id"] = question.get("questionId")
        out["required"] = bool(question.get("required", False))
        kind = next((k for k in _QUESTION_TYPES if k in question), "unknown")
        out["kind"] = kind
        if kind == "choiceQuestion":
            choice = question["choiceQuestion"]
            out["choice_type"] = choice.get("type")
            out["options"] = [o.get("value", "") for o in choice.get("options", [])]
        elif kind == "textQuestion":
            out["paragraph"] = bool(question["textQuestion"].get("paragraph", False))
    else:
        kind = next(
            (
                k
                for k in (
                    "questionGroupItem",
                    "pageBreakItem",
                    "textItem",
                    "imageItem",
                    "videoItem",
                )
                if k in item
            ),
            "unknown",
        )
        out["kind"] = kind
    return out


def summarize_response(resp: JSON) -> JSON:
    """Shape one response. Text answers longer than MAX_ANSWER_CHARS are cut and
    their question ids listed in ``truncated_question_ids``."""
    answers: JSON = {}
    truncated: list[str] = []
    for qid, answer in resp.get("answers", {}).items():
        if "textAnswers" in answer:
            values = [a.get("value", "") for a in answer["textAnswers"].get("answers", [])]
            if any(len(v) > MAX_ANSWER_CHARS for v in values):
                truncated.append(qid)
            answers[qid] = [v[:MAX_ANSWER_CHARS] for v in values]
        elif "fileUploadAnswers" in answer:
            answers[qid] = [
                {"file_id": a.get("fileId"), "file_name": a.get("fileName")}
                for a in answer["fileUploadAnswers"].get("answers", [])
            ]
        else:
            answers[qid] = []
    return {
        "response_id": resp.get("responseId"),
        "create_time": resp.get("createTime"),
        "last_submitted_time": resp.get("lastSubmittedTime"),
        "respondent_email": resp.get("respondentEmail"),
        "total_score": resp.get("totalScore"),
        "answers": answers,
        "truncated": bool(truncated),
        "truncated_question_ids": sorted(truncated),
    }


class FormsAdapter:
    spec = SPEC

    def register(self, registry: ToolRegistry, ctx: ServiceContext) -> None:
        @registry.tool("forms_get_form")
        def get_form(form_id: str) -> JSON:
            """Get a form's title, description, publish state, and question summary."""
            svc = ctx.client("forms", "v1")
            form = ctx.execute(svc.forms().get(formId=form_id))
            info = form.get("info", {})
            items = form.get("items", [])
            state = form.get("publishSettings", {}).get("publishState", {})
            return {
                "form_id": form.get("formId", form_id),
                "title": info.get("title"),
                "document_title": info.get("documentTitle"),
                "description": info.get("description"),
                "responder_uri": form.get("responderUri"),
                "linked_sheet_id": form.get("linkedSheetId"),
                "revision_id": form.get("revisionId"),
                "is_published": state.get("isPublished"),
                "is_accepting_responses": state.get("isAcceptingResponses"),
                "item_count": len(items),
                "items": [summarize_item(i) for i in items[:MAX_ITEMS]],
            }

        @registry.tool("forms_list_responses")
        def list_responses(
            form_id: str,
            page_size: int = 25,
            page_token: str | None = None,
            submitted_after: str | None = None,
        ) -> JSON:
            """List form responses. ``submitted_after`` is an RFC 3339 timestamp filter."""
            svc = ctx.client("forms", "v1")
            filter_expr = f"timestamp > {submitted_after}" if submitted_after else None
            resp = ctx.execute(
                svc.forms()
                .responses()
                .list(
                    formId=form_id,
                    pageSize=clamp_page_size(page_size),
                    pageToken=page_token,
                    filter=filter_expr,
                )
            )
            return {
                "responses": [summarize_response(r) for r in resp.get("responses", [])],
                "next_page_token": resp.get("nextPageToken"),
            }

        @registry.tool("forms_get_response")
        def get_response(form_id: str, response_id: str) -> JSON:
            """Get one form response by id."""
            svc = ctx.client("forms", "v1")
            return summarize_response(
                ctx.execute(svc.forms().responses().get(formId=form_id, responseId=response_id))
            )

        @registry.tool("forms_create_form", write=True)
        def create_form(
            title: str, document_title: str | None = None, unpublished: bool = True
        ) -> JSON:
            """Create an empty form. It stays unpublished (not accepting responses) by default."""
            info: JSON = {"title": title}
            if document_title:
                info["documentTitle"] = document_title
            svc = ctx.client("forms", "v1")
            form = ctx.execute(svc.forms().create(body={"info": info}, unpublished=unpublished))
            return {
                "form_id": form.get("formId"),
                "title": form.get("info", {}).get("title", title),
                "responder_uri": form.get("responderUri"),
                "revision_id": form.get("revisionId"),
            }

        @registry.tool("forms_add_question", write=True)
        def add_question(
            form_id: str,
            title: str,
            kind: str = "short_text",
            options: list[str] | None = None,
            required: bool = False,
            index: int | None = None,
        ) -> JSON:
            """Add a question to a form.

            ``kind`` is one of short_text, paragraph, multiple_choice, checkboxes,
            dropdown. Choice kinds need ``options``. Appends unless ``index`` is set.
            """
            question: JSON = {"required": required}
            if kind in _TEXT_KINDS:
                question["textQuestion"] = {"paragraph": _TEXT_KINDS[kind]}
            elif kind in _CHOICE_KINDS:
                if not options:
                    raise GoogleMCPError(f"{kind} questions need at least one option")
                question["choiceQuestion"] = {
                    "type": _CHOICE_KINDS[kind],
                    "options": [{"value": o} for o in options],
                }
            else:
                allowed = ", ".join([*_TEXT_KINDS, *_CHOICE_KINDS])
                raise GoogleMCPError(f"kind must be one of {allowed}")
            if index is not None and index < 0:
                raise GoogleMCPError("index must be 0 or greater")

            svc = ctx.client("forms", "v1")
            if index is None:
                current = ctx.execute(svc.forms().get(formId=form_id))
                index = len(current.get("items", []))
            resp = ctx.execute(
                svc.forms().batchUpdate(
                    formId=form_id,
                    body={
                        "requests": [
                            {
                                "createItem": {
                                    "item": {
                                        "title": title,
                                        "questionItem": {"question": question},
                                    },
                                    "location": {"index": index},
                                }
                            }
                        ],
                        "includeFormInResponse": False,
                    },
                )
            )
            created = (resp.get("replies") or [{}])[0].get("createItem", {})
            return {
                "form_id": form_id,
                "item_id": created.get("itemId"),
                "question_ids": created.get("questionId", []),
                "index": index,
            }


ADAPTER = FormsAdapter()
