"""Google Docs adapter.

Reads return plain text (size-capped) plus a small structure summary instead of
the raw document JSON, which is large and mostly layout metadata. Writes use
``documents.batchUpdate``.

Scopes: ``documents.readonly`` for reads, ``documents`` for writes. The
narrower ``drive.file`` scope only covers files this app created or the user
opened with it, so it cannot read arbitrary existing documents.
Reference: https://developers.google.com/workspace/docs/api/auth

Only the first tab's body is read (``includeTabsContent`` is left false), which
matches what the Docs API returns for documents without extra tabs.
"""

from __future__ import annotations

from typing import Any

from googlemcp.services.base import (
    JSON,
    GoogleMCPError,
    ServiceContext,
    ServiceSpec,
    ToolRegistry,
    UpstreamError,
)

SPEC = ServiceSpec(
    key="docs",
    title="Google Docs",
    api_services=("docs.googleapis.com",),
    read_scopes=("https://www.googleapis.com/auth/documents.readonly",),
    write_scopes=("https://www.googleapis.com/auth/documents",),
)

DEFAULT_MAX_CHARS = 20_000
MAX_CHARS_CEILING = 100_000
MAX_HEADINGS = 50
MAX_HEADING_CHARS = 200
_HEADING_STYLES = ("TITLE", "SUBTITLE", "HEADING_1", "HEADING_2", "HEADING_3")


def _clamp_chars(value: int | None) -> int:
    if value is None:
        return DEFAULT_MAX_CHARS
    return max(1, min(int(value), MAX_CHARS_CEILING))


def _paragraph_text(paragraph: JSON) -> str:
    return "".join(el.get("textRun", {}).get("content", "") for el in paragraph.get("elements", []))


def _walk(content: list[JSON], out: list[str], stats: dict[str, Any]) -> None:
    """Collect text from structural elements, recursing into tables."""
    for element in content:
        if "paragraph" in element:
            para = element["paragraph"]
            text = _paragraph_text(para)
            out.append(text)
            stats["paragraphs"] += 1
            style = para.get("paragraphStyle", {}).get("namedStyleType", "")
            if style in _HEADING_STYLES:
                stats["heading_count"] += 1
                if len(stats["headings"]) < MAX_HEADINGS:
                    heading = text.strip()
                    stats["headings"].append(
                        {
                            "style": style,
                            "text": heading[:MAX_HEADING_CHARS],
                            "truncated": len(heading) > MAX_HEADING_CHARS,
                        }
                    )
        elif "table" in element:
            stats["tables"] += 1
            for row in element["table"].get("tableRows", []):
                for cell in row.get("tableCells", []):
                    _walk(cell.get("content", []), out, stats)
        elif "tableOfContents" in element:
            _walk(element["tableOfContents"].get("content", []), out, stats)


def extract_text(document: JSON) -> tuple[str, dict[str, Any]]:
    """Return the document's plain text and a structure summary."""
    stats: dict[str, Any] = {"paragraphs": 0, "tables": 0, "heading_count": 0, "headings": []}
    out: list[str] = []
    _walk(document.get("body", {}).get("content", []), out, stats)
    return "".join(out), stats


class DocsAdapter:
    spec = SPEC

    def register(self, registry: ToolRegistry, ctx: ServiceContext) -> None:
        def batch_update(document_id: str, requests: list[JSON]) -> JSON:
            svc = ctx.client("docs", "v1")
            return ctx.execute(
                svc.documents().batchUpdate(documentId=document_id, body={"requests": requests})
            )

        @registry.tool("docs_get_document")
        def get_document(document_id: str, max_chars: int = DEFAULT_MAX_CHARS) -> JSON:
            """Read a Google Doc as plain text with a structure summary.

            Text is truncated to ``max_chars`` (default 20000, ceiling 100000);
            ``truncated`` reports whether the cap applied.
            """
            svc = ctx.client("docs", "v1")
            doc = ctx.execute(svc.documents().get(documentId=document_id))
            text, stats = extract_text(doc)
            limit = _clamp_chars(max_chars)
            return {
                "document_id": doc.get("documentId", document_id),
                "title": doc.get("title"),
                "revision_id": doc.get("revisionId"),
                "text": text[:limit],
                "total_chars": len(text),
                "truncated": len(text) > limit,
                "structure": stats,
            }

        @registry.tool("docs_create_document", write=True)
        def create_document(title: str, text: str | None = None) -> JSON:
            """Create a new Google Doc, optionally with initial body text.

            If the document is created but inserting ``text`` fails, the result
            still carries ``document_id`` with ``text_inserted`` false and an
            ``error``; retry with docs_append_text instead of creating again.
            """
            svc = ctx.client("docs", "v1")
            doc = ctx.execute(svc.documents().create(body={"title": title}))
            document_id = doc["documentId"]
            out: JSON = {"document_id": document_id, "title": doc.get("title", title)}
            if text:
                try:
                    batch_update(
                        document_id, [{"insertText": {"location": {"index": 1}, "text": text}}]
                    )
                except UpstreamError as exc:
                    out["text_inserted"] = False
                    out["error"] = f"document created but inserting text failed: {exc}"
                    return out
                out["text_inserted"] = True
            return out

        @registry.tool("docs_append_text", write=True)
        def append_text(document_id: str, text: str) -> JSON:
            """Append text to the end of a Google Doc body."""
            if not text:
                raise GoogleMCPError("text must not be empty")
            batch_update(document_id, [{"insertText": {"endOfSegmentLocation": {}, "text": text}}])
            return {"document_id": document_id, "appended_chars": len(text)}

        @registry.tool("docs_insert_text", write=True)
        def insert_text(document_id: str, text: str, index: int = 1) -> JSON:
            """Insert text at a body index. Index 1 is the start of the document."""
            if not text:
                raise GoogleMCPError("text must not be empty")
            if index < 1:
                raise GoogleMCPError("index must be 1 or greater")
            batch_update(
                document_id, [{"insertText": {"location": {"index": index}, "text": text}}]
            )
            return {"document_id": document_id, "index": index, "inserted_chars": len(text)}

        @registry.tool("docs_replace_all_text", write=True)
        def replace_all_text(
            document_id: str, find: str, replace_with: str, match_case: bool = True
        ) -> JSON:
            """Replace every occurrence of ``find`` with ``replace_with``.

            Earlier revisions stay available in the document's version history.
            """
            if not find:
                raise GoogleMCPError("find must not be empty")
            resp = batch_update(
                document_id,
                [
                    {
                        "replaceAllText": {
                            "containsText": {"text": find, "matchCase": match_case},
                            "replaceText": replace_with,
                        }
                    }
                ],
            )
            replies = resp.get("replies") or [{}]
            changed = replies[0].get("replaceAllText", {}).get("occurrencesChanged", 0)
            return {"document_id": document_id, "occurrences_changed": changed}


ADAPTER = DocsAdapter()
