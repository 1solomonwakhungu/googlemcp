"""Google Slides adapter.

Reads summarize each slide as its object id plus extracted text (shapes and
tables), not the raw page JSON. Writes use ``presentations.batchUpdate``.

Thumbnail URLs are bearer links: per the API reference, "Anyone with the URL
effectively accesses the image as the original requester" for its default
30 minute lifetime. Treat them as sensitive.
Reference: https://developers.google.com/workspace/slides/api/reference/rest/v1/presentations.pages/getThumbnail

Scopes: ``presentations.readonly`` for reads, ``presentations`` for writes.
"""

from __future__ import annotations

from googlemcp.services.base import (
    JSON,
    GoogleMCPError,
    ServiceContext,
    ServiceSpec,
    ToolRegistry,
)

SPEC = ServiceSpec(
    key="slides",
    title="Google Slides",
    api_services=("slides.googleapis.com",),
    read_scopes=("https://www.googleapis.com/auth/presentations.readonly",),
    write_scopes=("https://www.googleapis.com/auth/presentations",),
)

MAX_SLIDE_CHARS = 2_000
MAX_SLIDES = 100
_THUMBNAIL_SIZES = ("SMALL", "MEDIUM", "LARGE")
_LAYOUTS = (
    "BLANK",
    "CAPTION_ONLY",
    "TITLE",
    "TITLE_AND_BODY",
    "TITLE_AND_TWO_COLUMNS",
    "TITLE_ONLY",
    "SECTION_HEADER",
    "SECTION_TITLE_AND_DESCRIPTION",
    "ONE_COLUMN_TEXT",
    "MAIN_POINT",
    "BIG_NUMBER",
)


def _text_of(text: JSON) -> str:
    return "".join(el.get("textRun", {}).get("content", "") for el in text.get("textElements", []))


def _element_text(element: JSON) -> list[str]:
    if "shape" in element:
        return [_text_of(element["shape"].get("text", {}))]
    if "table" in element:
        return [
            _text_of(cell.get("text", {}))
            for row in element["table"].get("tableRows", [])
            for cell in row.get("tableCells", [])
        ]
    if "elementGroup" in element:
        return [
            t for child in element["elementGroup"].get("children", []) for t in _element_text(child)
        ]
    return []


def summarize_slide(slide: JSON) -> JSON:
    parts = [t for el in slide.get("pageElements", []) for t in _element_text(el) if t.strip()]
    text = "".join(parts)
    return {
        "object_id": slide.get("objectId"),
        "layout": slide.get("slideProperties", {}).get("layoutObjectId"),
        "text": text[:MAX_SLIDE_CHARS],
        "truncated": len(text) > MAX_SLIDE_CHARS,
    }


class SlidesAdapter:
    spec = SPEC

    def register(self, registry: ToolRegistry, ctx: ServiceContext) -> None:
        def batch_update(presentation_id: str, requests: list[JSON]) -> JSON:
            svc = ctx.client("slides", "v1")
            return ctx.execute(
                svc.presentations().batchUpdate(
                    presentationId=presentation_id, body={"requests": requests}
                )
            )

        @registry.tool("slides_get_presentation")
        def get_presentation(presentation_id: str) -> JSON:
            """Summarize a presentation: title and each slide's object id and text.

            At most 100 slides and 2000 characters per slide are returned.
            """
            svc = ctx.client("slides", "v1")
            resp = ctx.execute(svc.presentations().get(presentationId=presentation_id))
            slides = resp.get("slides", [])
            return {
                "presentation_id": resp.get("presentationId", presentation_id),
                "title": resp.get("title"),
                "revision_id": resp.get("revisionId"),
                "slide_count": len(slides),
                "slides": [summarize_slide(s) for s in slides[:MAX_SLIDES]],
            }

        @registry.tool("slides_get_thumbnail")
        def get_thumbnail(presentation_id: str, page_object_id: str, size: str = "MEDIUM") -> JSON:
            """Get a PNG thumbnail URL for one slide.

            The URL works without sign-in for about 30 minutes and acts as the
            signed-in user, so do not share it.
            """
            if size not in _THUMBNAIL_SIZES:
                raise GoogleMCPError(f"size must be one of {', '.join(_THUMBNAIL_SIZES)}")
            svc = ctx.client("slides", "v1")
            resp = ctx.execute(
                svc.presentations()
                .pages()
                .getThumbnail(
                    presentationId=presentation_id,
                    pageObjectId=page_object_id,
                    thumbnailProperties_mimeType="PNG",
                    thumbnailProperties_thumbnailSize=size,
                )
            )
            return {
                "content_url": resp.get("contentUrl"),
                "width": resp.get("width"),
                "height": resp.get("height"),
            }

        @registry.tool("slides_create_presentation", write=True)
        def create_presentation(title: str) -> JSON:
            """Create a new, empty presentation."""
            svc = ctx.client("slides", "v1")
            resp = ctx.execute(svc.presentations().create(body={"title": title}))
            return {
                "presentation_id": resp.get("presentationId"),
                "title": resp.get("title", title),
                "slide_ids": [s.get("objectId") for s in resp.get("slides", [])],
            }

        @registry.tool("slides_replace_all_text", write=True)
        def replace_all_text(
            presentation_id: str,
            find: str,
            replace_with: str,
            match_case: bool = True,
            page_object_ids: list[str] | None = None,
        ) -> JSON:
            """Replace every occurrence of ``find``, optionally only on the given slides."""
            if not find:
                raise GoogleMCPError("find must not be empty")
            request: JSON = {
                "containsText": {"text": find, "matchCase": match_case},
                "replaceText": replace_with,
            }
            if page_object_ids:
                request["pageObjectIds"] = page_object_ids
            resp = batch_update(presentation_id, [{"replaceAllText": request}])
            replies = resp.get("replies") or [{}]
            changed = replies[0].get("replaceAllText", {}).get("occurrencesChanged", 0)
            return {"presentation_id": presentation_id, "occurrences_changed": changed}

        @registry.tool("slides_add_slide", write=True)
        def add_slide(
            presentation_id: str, layout: str = "TITLE_AND_BODY", insertion_index: int | None = None
        ) -> JSON:
            """Add a slide using a predefined layout. Appends unless ``insertion_index`` is set."""
            if layout not in _LAYOUTS:
                raise GoogleMCPError(f"layout must be one of {', '.join(_LAYOUTS)}")
            request: JSON = {"slideLayoutReference": {"predefinedLayout": layout}}
            if insertion_index is not None:
                if insertion_index < 0:
                    raise GoogleMCPError("insertion_index must be 0 or greater")
                request["insertionIndex"] = insertion_index
            resp = batch_update(presentation_id, [{"createSlide": request}])
            replies = resp.get("replies") or [{}]
            return {
                "presentation_id": presentation_id,
                "slide_object_id": replies[0].get("createSlide", {}).get("objectId"),
            }


ADAPTER = SlidesAdapter()
