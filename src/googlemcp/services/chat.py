"""Google Chat adapter (user authentication).

Off by default in the catalog. Limits:

- Calling the Chat API with user credentials requires "A Business or
  Enterprise Google Workspace account with access to Google Chat". A consumer
  @gmail.com account cannot use these tools.
  Reference: https://developers.google.com/workspace/chat/authenticate-authorize-chat-user
- The Chat API must also be configured as a Chat app (name, avatar,
  description) on the Chat API configuration page in the Cloud console. That
  page has no API or Terraform resource; see docs/manual-steps.md.
  Reference: https://developers.google.com/workspace/chat/configure-chat-api
- Messages created with user authentication are sent as the user.

Scopes: ``chat.spaces.readonly`` and ``chat.messages.readonly`` for reads,
``chat.messages.create`` for writes (narrower than ``chat.messages``, which
would also allow edits and deletes).

Sending a message notifies other people and cannot be cleanly undone, so
``chat_create_message`` is registered as destructive and needs the
destructive opt-in in addition to writes.
"""

from __future__ import annotations

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
    key="chat",
    title="Google Chat",
    api_services=("chat.googleapis.com",),
    read_scopes=(
        "https://www.googleapis.com/auth/chat.spaces.readonly",
        "https://www.googleapis.com/auth/chat.messages.readonly",
    ),
    write_scopes=("https://www.googleapis.com/auth/chat.messages.create",),
)

MAX_MESSAGE_CHARS = 4_000
# "The maximum message size (including any text or cards) is 32,000 bytes."
# https://developers.google.com/workspace/chat/create-messages
MAX_SEND_BYTES = 32_000
_SPACE_FIELDS = ("name", "displayName", "spaceType", "spaceThreadingState", "lastActiveTime")


def _space_name(value: str) -> str:
    value = value.strip()
    if not value:
        raise GoogleMCPError("space must not be empty")
    if value.startswith("spaces/") and value.count("/") == 1:
        return value
    if "/" in value:
        raise GoogleMCPError(f"expected 'spaces/<id>' or a bare id, got {value!r}")
    return f"spaces/{value}"


def summarize_message(msg: JSON) -> JSON:
    text = msg.get("text", "")
    sender = msg.get("sender", {})
    return {
        "name": msg.get("name"),
        "sender": sender.get("name"),
        "sender_type": sender.get("type"),
        "create_time": msg.get("createTime"),
        "thread": msg.get("thread", {}).get("name"),
        "text": text[:MAX_MESSAGE_CHARS],
        "truncated": len(text) > MAX_MESSAGE_CHARS,
    }


class ChatAdapter:
    spec = SPEC

    def register(self, registry: ToolRegistry, ctx: ServiceContext) -> None:
        @registry.tool("chat_list_spaces")
        def list_spaces(
            page_size: int = 25,
            page_token: str | None = None,
            space_type: str | None = None,
        ) -> JSON:
            """List Chat spaces the user is a member of.

            ``space_type`` optionally filters to SPACE, GROUP_CHAT, or DIRECT_MESSAGE.
            """
            filter_expr = None
            if space_type is not None:
                if space_type not in ("SPACE", "GROUP_CHAT", "DIRECT_MESSAGE"):
                    raise GoogleMCPError("space_type must be SPACE, GROUP_CHAT, or DIRECT_MESSAGE")
                filter_expr = f'space_type = "{space_type}"'
            svc = ctx.client("chat", "v1")
            resp = ctx.execute(
                svc.spaces().list(
                    pageSize=clamp_page_size(page_size), pageToken=page_token, filter=filter_expr
                )
            )
            return {
                "spaces": [pick(s, _SPACE_FIELDS) for s in resp.get("spaces", [])],
                "next_page_token": resp.get("nextPageToken"),
            }

        @registry.tool("chat_list_messages")
        def list_messages(
            space: str,
            page_size: int = 25,
            page_token: str | None = None,
            newest_first: bool = True,
            filter_expr: str | None = None,
        ) -> JSON:
            """List messages in a space ('spaces/<id>' or bare id).

            ``filter_expr`` uses the API syntax, for example
            'create_time > "2026-10-01T00:00:00Z"'. Message text is capped at 4000 characters.
            """
            svc = ctx.client("chat", "v1")
            resp = ctx.execute(
                svc.spaces()
                .messages()
                .list(
                    parent=_space_name(space),
                    pageSize=clamp_page_size(page_size),
                    pageToken=page_token,
                    orderBy="create_time DESC" if newest_first else "create_time ASC",
                    filter=filter_expr,
                )
            )
            return {
                "messages": [summarize_message(m) for m in resp.get("messages", [])],
                "next_page_token": resp.get("nextPageToken"),
            }

        @registry.tool("chat_create_message", write=True, destructive=True)
        def create_message(space: str, text: str, thread_key: str | None = None) -> JSON:
            """Send a text message to a space as the user.

            With ``thread_key`` the message replies in that thread, or starts it.
            """
            if not text.strip():
                raise GoogleMCPError("text must not be empty")
            if len(text.encode("utf-8")) > MAX_SEND_BYTES:
                raise GoogleMCPError(f"text exceeds the {MAX_SEND_BYTES} byte message limit")
            body: JSON = {"text": text}
            reply_option = None
            if thread_key:
                body["thread"] = {"threadKey": thread_key}
                reply_option = "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
            svc = ctx.client("chat", "v1")
            resp = ctx.execute(
                svc.spaces()
                .messages()
                .create(parent=_space_name(space), body=body, messageReplyOption=reply_option)
            )
            return summarize_message(resp)


ADAPTER = ChatAdapter()
