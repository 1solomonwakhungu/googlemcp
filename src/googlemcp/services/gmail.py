"""Gmail adapter.

Scope choices (per-method scopes from the Gmail v1 discovery document,
https://developers.google.com/workspace/gmail/api/auth/scopes):

- Read: ``gmail.readonly``. Needed for ``q`` search and for message bodies.
  ``gmail.metadata`` is narrower but rejects the ``q`` parameter and never
  returns bodies, so it cannot support search or reading mail.
- Write: ``gmail.modify``. ``users.messages.modify`` (label changes) accepts
  only ``gmail.modify`` or full ``mail.google.com``; ``gmail.modify`` also
  covers drafts and sending, so ``gmail.compose`` would add nothing. It does
  not allow permanent deletion, and no tool here deletes mail.

Attachment bytes are never downloaded; tools report attachment metadata only.
Bodies are decoded to plain text and capped by ``max_body_chars``.
"""

from __future__ import annotations

import base64
import binascii
import codecs
import re
from collections.abc import Callable
from email.message import EmailMessage
from email.utils import formatdate
from html.parser import HTMLParser
from typing import Any

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
    key="gmail",
    title="Gmail",
    api_services=("gmail.googleapis.com",),
    read_scopes=("https://www.googleapis.com/auth/gmail.readonly",),
    write_scopes=("https://www.googleapis.com/auth/gmail.modify",),
)

USER = "me"
DEFAULT_BODY_CHARS = 20_000
THREAD_BODY_CHARS = 4_000
# Largest out-of-line text body part fetched with attachments.get.
MAX_BODY_FETCH_BYTES = 512 * 1024
# Total body characters returned for one thread, across all messages.
MAX_THREAD_CHARS = 40_000
MAX_BODY_CHARS = 100_000
# Search enriches each hit with one metadata request, so keep pages small.
MAX_SEARCH_RESULTS = 25
SUMMARY_HEADERS = ("From", "To", "Cc", "Subject", "Date")
DETAIL_HEADERS = (*SUMMARY_HEADERS, "Reply-To", "Message-ID", "In-Reply-To", "References")
# Adding these labels hides or eventually deletes mail, so the non-destructive
# label tool refuses them.
_BLOCKED_ADD_LABELS = frozenset({"TRASH", "SPAM"})
_CHARSET_RE = re.compile(r"charset\s*=\s*\"?([A-Za-z0-9_.:-]+)", re.IGNORECASE)
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def _clamp_chars(value: int | None, default: int) -> int:
    if value is None:
        return default
    return max(0, min(int(value), MAX_BODY_CHARS))


def _b64decode(data: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except (binascii.Error, ValueError):
        return b""


def _header_map(headers: list[JSON], wanted: tuple[str, ...]) -> JSON:
    lowered = {name.lower(): name for name in wanted}
    out: JSON = {}
    for header in headers:
        canonical = lowered.get(str(header.get("name", "")).lower())
        if canonical and canonical not in out:
            out[canonical] = header.get("value", "")
    return out


def _part_charset(part: JSON) -> str:
    for header in part.get("headers", []):
        if str(header.get("name", "")).lower() == "content-type":
            match = _CHARSET_RE.search(str(header.get("value", "")))
            if match:
                try:
                    return codecs.lookup(match.group(1)).name
                except LookupError:
                    return "utf-8"
    return "utf-8"


class _TextExtractor(HTMLParser):
    _BREAKS = frozenset({"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"})
    _SKIP = frozenset({"script", "style", "head"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BREAKS:
            self.chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self._BREAKS:
            self.chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.chunks.append(data)


def html_to_text(markup: str) -> str:
    """Strip tags from an HTML body. Used only when no text/plain part exists."""
    parser = _TextExtractor()
    parser.feed(markup)
    parser.close()
    lines = (line.strip() for line in "".join(parser.chunks).splitlines())
    return _BLANK_LINES_RE.sub("\n\n", "\n".join(lines)).strip()


def _walk(
    part: JSON,
    plain: list[str],
    html: list[str],
    attachments: list[JSON],
    pending: list[JSON],
) -> None:
    mime = str(part.get("mimeType", ""))
    body = part.get("body", {}) or {}
    filename = part.get("filename") or ""
    is_text = mime in ("text/plain", "text/html")
    if filename or (body.get("attachmentId") and not is_text):
        attachments.append(
            {
                "filename": filename,
                "mime_type": mime,
                "size": body.get("size", 0),
                "attachment_id": body.get("attachmentId"),
            }
        )
    elif is_text and body.get("data"):
        text = _b64decode(str(body["data"])).decode(_part_charset(part), errors="replace")
        (plain if mime == "text/plain" else html).append(text)
    elif is_text and body.get("attachmentId"):
        # Gmail moves large body parts out of line; they must be fetched separately.
        pending.append(
            {
                "mime": mime,
                "attachment_id": body["attachmentId"],
                "size": int(body.get("size") or 0),
                "charset": _part_charset(part),
            }
        )
    for child in part.get("parts", []) or []:
        _walk(child, plain, html, attachments, pending)


BodyFetcher = Callable[[str], str | None]


def shape_message(message: JSON, max_body_chars: int, fetch: BodyFetcher | None = None) -> JSON:
    """Reduce a ``format=full`` message to headers, text body, and attachment metadata.

    Text body parts stored out of line (``attachmentId`` without a filename) are
    fetched through ``fetch`` when they are at most ``MAX_BODY_FETCH_BYTES``;
    otherwise the body is reported as truncated.
    """
    payload = message.get("payload", {}) or {}
    plain: list[str] = []
    html: list[str] = []
    attachments: list[JSON] = []
    pending: list[JSON] = []
    _walk(payload, plain, html, attachments, pending)
    skipped = 0
    for part in pending:
        data = None
        if fetch is not None and max_body_chars > 0 and part["size"] <= MAX_BODY_FETCH_BYTES:
            data = fetch(str(part["attachment_id"]))
        if not data:
            skipped += 1
            continue
        text = _b64decode(data).decode(part["charset"], errors="replace")
        (plain if part["mime"] == "text/plain" else html).append(text)
    if plain:
        text, source = "\n\n".join(plain), "text/plain"
    elif html:
        text, source = html_to_text("\n".join(html)), "text/html"
    else:
        text, source = "", None
    out = pick(message, ("id", "threadId", "labelIds", "snippet", "internalDate", "sizeEstimate"))
    out["headers"] = _header_map(payload.get("headers", []), DETAIL_HEADERS)
    out["body"] = text[:max_body_chars]
    out["body_source"] = source
    out["body_chars"] = len(text)
    out["body_truncated"] = len(text) > max_body_chars or skipped > 0
    if skipped:
        out["body_parts_not_fetched"] = skipped
    out["attachments"] = attachments
    return out


def _summary(message: JSON) -> JSON:
    out = pick(message, ("id", "threadId", "labelIds", "snippet"))
    headers = (message.get("payload", {}) or {}).get("headers", [])
    out.update({k.lower(): v for k, v in _header_map(headers, SUMMARY_HEADERS).items()})
    return out


def _addresses(field: str, values: list[str] | None) -> str | None:
    if not values:
        return None
    for value in values:
        if "@" not in value or "\n" in value or "\r" in value:
            raise GoogleMCPError(f"{field}: invalid email address {value!r}")
    return ", ".join(values)


def build_raw_message(
    *,
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    in_reply_to: str | None = None,
) -> str:
    """Build a base64url RFC 2822 message for the ``raw`` field.

    From is omitted: Gmail sets it to the authenticated account.
    """
    if not to:
        raise GoogleMCPError("to: at least one recipient is required")
    msg = EmailMessage()
    msg["Date"] = formatdate(localtime=True)
    for name, value in (
        ("To", _addresses("to", to)),
        ("Cc", _addresses("cc", cc)),
        ("Bcc", _addresses("bcc", bcc)),
    ):
        if value:
            msg[name] = value
    try:
        msg["Subject"] = subject
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to
    except ValueError as exc:
        raise GoogleMCPError(f"invalid header value: {exc}") from exc
    msg.set_content(body)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


def _message_body(raw: str, thread_id: str | None) -> JSON:
    message: JSON = {"raw": raw}
    if thread_id:
        message["threadId"] = thread_id
    return message


class GmailAdapter:
    spec = SPEC

    def register(self, registry: ToolRegistry, ctx: ServiceContext) -> None:
        def svc() -> Any:
            return ctx.client("gmail", "v1")

        def body_fetcher(message_id: str) -> BodyFetcher:
            def fetch(attachment_id: str) -> str | None:
                resp = ctx.execute(
                    svc()
                    .users()
                    .messages()
                    .attachments()
                    .get(userId=USER, messageId=message_id, id=attachment_id)
                )
                data = resp.get("data")
                return str(data) if data else None

            return fetch

        @registry.tool("gmail_search_messages")
        def search_messages(
            query: str = "",
            max_results: int = 10,
            page_token: str | None = None,
            include_spam_trash: bool = False,
            include_headers: bool = True,
        ) -> JSON:
            """Search Gmail with Gmail search syntax (for example 'from:alice newer_than:7d').

            Returns message ids with From, To, Subject, Date, and snippet when
            include_headers is true. Use gmail_get_message for the body.
            """
            limit = min(clamp_page_size(max_results), MAX_SEARCH_RESULTS)
            client = svc()
            resp = ctx.execute(
                client.users()
                .messages()
                .list(
                    userId=USER,
                    q=query or None,
                    maxResults=limit,
                    pageToken=page_token,
                    includeSpamTrash=include_spam_trash,
                )
            )
            refs = resp.get("messages", [])
            if include_headers:
                messages = [
                    _summary(
                        ctx.execute(
                            client.users()
                            .messages()
                            .get(
                                userId=USER,
                                id=ref["id"],
                                format="metadata",
                                metadataHeaders=list(SUMMARY_HEADERS),
                            )
                        )
                    )
                    for ref in refs
                ]
            else:
                messages = [pick(ref, ("id", "threadId")) for ref in refs]
            return {
                "messages": messages,
                "next_page_token": resp.get("nextPageToken"),
                "result_size_estimate": resp.get("resultSizeEstimate"),
            }

        @registry.tool("gmail_get_message")
        def get_message(message_id: str, max_body_chars: int = DEFAULT_BODY_CHARS) -> JSON:
            """Get one message: headers, plain-text body (size-capped), and attachment metadata.

            Attachment contents are not downloaded.
            """
            resp = ctx.execute(
                svc().users().messages().get(userId=USER, id=message_id, format="full")
            )
            return shape_message(
                resp, _clamp_chars(max_body_chars, DEFAULT_BODY_CHARS), body_fetcher(message_id)
            )

        @registry.tool("gmail_get_thread")
        def get_thread(thread_id: str, max_body_chars: int = THREAD_BODY_CHARS) -> JSON:
            """Get every message in a thread with headers and size-capped plain-text bodies.

            Bodies are capped per message (max_body_chars) and in total across the
            thread; once the total is spent, later bodies are empty and marked truncated.
            """
            cap = _clamp_chars(max_body_chars, THREAD_BODY_CHARS)
            resp = ctx.execute(
                svc().users().threads().get(userId=USER, id=thread_id, format="full")
            )
            remaining = MAX_THREAD_CHARS
            messages = []
            for message in resp.get("messages", []):
                shaped = shape_message(
                    message, min(cap, remaining), body_fetcher(str(message.get("id", "")))
                )
                remaining -= len(shaped["body"])
                messages.append(shaped)
            return {
                "id": resp.get("id"),
                "messages": messages,
                "bodies_truncated": any(m["body_truncated"] for m in messages),
            }

        @registry.tool("gmail_list_labels")
        def list_labels() -> JSON:
            """List Gmail labels (system and user labels) with their ids."""
            resp = ctx.execute(svc().users().labels().list(userId=USER))
            return {"labels": [pick(i, ("id", "name", "type")) for i in resp.get("labels", [])]}

        @registry.tool("gmail_create_draft", write=True)
        def create_draft(
            to: list[str],
            subject: str,
            body: str,
            cc: list[str] | None = None,
            bcc: list[str] | None = None,
            thread_id: str | None = None,
            in_reply_to: str | None = None,
        ) -> JSON:
            """Create a plain-text draft. Nothing is sent.

            To reply in a thread pass thread_id and the original Message-ID
            header as in_reply_to.
            """
            raw = build_raw_message(
                to=to, subject=subject, body=body, cc=cc, bcc=bcc, in_reply_to=in_reply_to
            )
            resp = ctx.execute(
                svc()
                .users()
                .drafts()
                .create(userId=USER, body={"message": _message_body(raw, thread_id)})
            )
            return {
                "draft_id": resp.get("id"),
                "message": pick(resp.get("message", {}), ("id", "threadId")),
            }

        @registry.tool("gmail_send_draft", write=True, destructive=True)
        def send_draft(draft_id: str) -> JSON:
            """Send an existing draft. Sending cannot be undone."""
            resp = ctx.execute(svc().users().drafts().send(userId=USER, body={"id": draft_id}))
            return pick(resp, ("id", "threadId", "labelIds"))

        @registry.tool("gmail_send_message", write=True, destructive=True)
        def send_message(
            to: list[str],
            subject: str,
            body: str,
            cc: list[str] | None = None,
            bcc: list[str] | None = None,
            thread_id: str | None = None,
            in_reply_to: str | None = None,
        ) -> JSON:
            """Send a plain-text email immediately. Sending cannot be undone.

            Prefer gmail_create_draft when the user has not confirmed the exact text.
            """
            raw = build_raw_message(
                to=to, subject=subject, body=body, cc=cc, bcc=bcc, in_reply_to=in_reply_to
            )
            resp = ctx.execute(
                svc().users().messages().send(userId=USER, body=_message_body(raw, thread_id))
            )
            return pick(resp, ("id", "threadId", "labelIds"))

        @registry.tool("gmail_modify_message_labels", write=True, idempotent=True)
        def modify_message_labels(
            message_id: str,
            add_label_ids: list[str] | None = None,
            remove_label_ids: list[str] | None = None,
        ) -> JSON:
            """Add or remove labels on a message (for example remove 'UNREAD' or 'INBOX').

            Adding TRASH or SPAM is refused; this tool never deletes mail.
            """
            add = list(add_label_ids or [])
            remove = list(remove_label_ids or [])
            if not add and not remove:
                raise GoogleMCPError("provide add_label_ids or remove_label_ids")
            blocked = sorted(_BLOCKED_ADD_LABELS.intersection(add))
            if blocked:
                raise GoogleMCPError(
                    f"refusing to add labels {blocked}; this tool never trashes mail"
                )
            resp = ctx.execute(
                svc()
                .users()
                .messages()
                .modify(
                    userId=USER,
                    id=message_id,
                    body={"addLabelIds": add, "removeLabelIds": remove},
                )
            )
            return pick(resp, ("id", "threadId", "labelIds"))


ADAPTER = GmailAdapter()
