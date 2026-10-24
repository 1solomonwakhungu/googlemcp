"""Google Drive adapter.

Scope choices (per-method scopes from the Drive v3 discovery document,
https://developers.google.com/workspace/drive/api/guides/api-specific-auth):

- Read: ``drive.readonly``. ``files.export`` and ``alt=media`` downloads are
  not available under ``drive.metadata.readonly``, so reading document text
  needs ``drive.readonly``. It cannot change anything.
- Write: ``drive.file``, not full ``drive``. ``drive.file`` grants access
  only to files and folders this app created, or that the user opened with
  or shared to this app (for example by selecting them in Google Picker)
  (https://developers.google.com/workspace/drive/api/guides/api-specific-auth).
  Consequences, all deliberate so an MCP client cannot modify arbitrary
  Drive content:

  - Creating a folder or uploading into My Drive root always works.
  - Creating inside ``parent_id`` works only if the app can access that
    folder under ``drive.file`` (for example one made with
    ``drive_create_folder``); any other folder is invisible to the app and
    Drive answers 404.
  - Renaming or trashing works only on files the app can access under
    ``drive.file``; others 404.

  These 404s are re-raised as ``UpstreamError`` with a message that names
  the ``drive.file`` limit instead of a bare "not found".

Downloads are restricted to text-like files and capped by ``max_chars``;
binary content is never returned.
"""

from __future__ import annotations

import io
from typing import Any

from googleapiclient.http import MediaIoBaseUpload

from googlemcp.services.base import (
    JSON,
    GoogleMCPError,
    ServiceContext,
    ServiceSpec,
    ToolRegistry,
    UpstreamError,
    clamp_page_size,
    pick,
)

SPEC = ServiceSpec(
    key="drive",
    title="Google Drive",
    api_services=("drive.googleapis.com",),
    read_scopes=("https://www.googleapis.com/auth/drive.readonly",),
    write_scopes=("https://www.googleapis.com/auth/drive.file",),
)

FOLDER_MIME = "application/vnd.google-apps.folder"
GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
# Google Workspace types and the text format each one is exported as.
# Spreadsheets export only the first sheet as CSV (a Drive API limitation).
EXPORT_FORMATS = {
    GOOGLE_DOC_MIME: "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}
TEXT_MIME_EXTRAS = frozenset(
    {
        "application/json",
        "application/xml",
        "application/x-yaml",
        "application/yaml",
        "application/javascript",
        "application/x-sh",
        "application/sql",
    }
)
DEFAULT_TEXT_CHARS = 20_000
MAX_TEXT_CHARS = 200_000
# Refuse to download non-Workspace files larger than this many bytes.
MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024
MAX_UPLOAD_BYTES = 1024 * 1024
FILE_FIELDS = (
    "id",
    "name",
    "mimeType",
    "description",
    "parents",
    "size",
    "createdTime",
    "modifiedTime",
    "webViewLink",
    "starred",
    "trashed",
)
_FIELDS_PARAM = ",".join((*FILE_FIELDS, "owners(displayName,emailAddress)"))
_ORDER_BY = frozenset(
    {
        "createdTime",
        "createdTime desc",
        "folder",
        "modifiedTime",
        "modifiedTime desc",
        "name",
        "name desc",
        "quotaBytesUsed desc",
        "recency desc",
        "starred",
        "viewedByMeTime desc",
    }
)


def quote(value: str) -> str:
    """Quote a literal for the Drive ``q`` language."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def build_query(
    *,
    query: str | None,
    name_contains: str | None,
    mime_type: str | None,
    folder_id: str | None,
    include_trashed: bool,
) -> str | None:
    """Combine a raw ``q`` with safe structured filters.

    The raw query is parenthesized so ``trashed = false`` still applies when
    it contains ``or``.
    """
    clauses: list[str] = []
    if query:
        clauses.append(f"({query})")
    if name_contains:
        clauses.append(f"name contains {quote(name_contains)}")
    if mime_type:
        clauses.append(f"mimeType = {quote(mime_type)}")
    if folder_id:
        clauses.append(f"{quote(folder_id)} in parents")
    if not include_trashed:
        clauses.append("trashed = false")
    return " and ".join(clauses) or None


def shape_file(item: JSON) -> JSON:
    out = pick(item, FILE_FIELDS)
    if "owners" in item:
        out["owners"] = [pick(o, ("displayName", "emailAddress")) for o in item["owners"]]
    return out


def _is_text(mime: str) -> bool:
    return mime.startswith("text/") or mime in TEXT_MIME_EXTRAS


def _decode(content: Any) -> str:
    if isinstance(content, bytes | bytearray):
        return bytes(content).decode("utf-8", errors="replace")
    if isinstance(content, str):
        return content
    raise GoogleMCPError("unexpected non-text response from Drive")


_PARENT_LIMIT = (
    "Drive returned 404 for parent folder {target!r}. Writes use the drive.file scope, "
    "which can only write into folders this app created or that the user opened with or "
    "shared to this app (for example through Google Picker). Use a folder made with "
    "drive_create_folder, or omit parent_id to write to My Drive."
)
_FILE_LIMIT = (
    "Drive returned 404 for file {target!r}: it does not exist, or this app cannot access it. "
    "Writes use the drive.file scope, which can only change files this app created or that "
    "the user opened with or shared to this app (for example through Google Picker)."
)


def _execute_write(ctx: ServiceContext, request: Any, template: str, target: str | None) -> JSON:
    """Execute a write and explain drive.file 404s instead of reporting 'not found'."""
    try:
        return ctx.execute(request)
    except UpstreamError as exc:
        if exc.status == 404 and target:
            raise UpstreamError(
                template.format(target=target), status=404, reason=exc.reason
            ) from exc
        raise


class DriveAdapter:
    spec = SPEC

    def register(self, registry: ToolRegistry, ctx: ServiceContext) -> None:
        def svc() -> Any:
            return ctx.client("drive", "v3")

        @registry.tool("drive_search_files")
        def search_files(
            query: str | None = None,
            name_contains: str | None = None,
            mime_type: str | None = None,
            folder_id: str | None = None,
            include_trashed: bool = False,
            order_by: str = "modifiedTime desc",
            max_results: int = 25,
            page_token: str | None = None,
        ) -> JSON:
            """Search the user's Drive. query is optional raw Drive search syntax
            (for example "fullText contains 'budget'"); name_contains, mime_type and
            folder_id are safe structured filters. Trashed files are excluded by default.
            """
            if order_by not in _ORDER_BY:
                raise GoogleMCPError(f"order_by must be one of {sorted(_ORDER_BY)}")
            resp = ctx.execute(
                svc()
                .files()
                .list(
                    q=build_query(
                        query=query,
                        name_contains=name_contains,
                        mime_type=mime_type,
                        folder_id=folder_id,
                        include_trashed=include_trashed,
                    ),
                    corpora="user",
                    spaces="drive",
                    orderBy=order_by,
                    pageSize=clamp_page_size(max_results),
                    pageToken=page_token,
                    fields=f"nextPageToken,incompleteSearch,files({_FIELDS_PARAM})",
                )
            )
            return {
                "files": [shape_file(f) for f in resp.get("files", [])],
                "next_page_token": resp.get("nextPageToken"),
                "incomplete_search": resp.get("incompleteSearch", False),
            }

        @registry.tool("drive_get_file")
        def get_file(file_id: str) -> JSON:
            """Get metadata for one file or folder."""
            return shape_file(ctx.execute(svc().files().get(fileId=file_id, fields=_FIELDS_PARAM)))

        @registry.tool("drive_read_file_text")
        def read_file_text(file_id: str, max_chars: int = DEFAULT_TEXT_CHARS) -> JSON:
            """Read the text of a Google Doc, Sheet (first sheet as CSV), Slides deck, or
            plain-text file. Output is capped at max_chars. Binary files are refused.
            """
            cap = max(1, min(int(max_chars), MAX_TEXT_CHARS))
            client = svc()
            meta = ctx.execute(client.files().get(fileId=file_id, fields="id,name,mimeType,size"))
            mime = str(meta.get("mimeType", ""))
            export_as = EXPORT_FORMATS.get(mime)
            if export_as:
                content: Any = ctx.execute(
                    client.files().export(fileId=file_id, mimeType=export_as)
                )
            elif _is_text(mime):
                size = int(meta.get("size") or 0)
                if size > MAX_DOWNLOAD_BYTES:
                    raise GoogleMCPError(
                        f"file is {size} bytes; refusing to download more than {MAX_DOWNLOAD_BYTES}"
                    )
                content = ctx.execute(client.files().get_media(fileId=file_id))
            else:
                raise GoogleMCPError(f"cannot read {mime or 'unknown'} files as text")
            text = _decode(content)
            return {
                "id": meta.get("id", file_id),
                "name": meta.get("name"),
                "mimeType": mime,
                "exported_as": export_as,
                "text": text[:cap],
                "total_chars": len(text),
                "truncated": len(text) > cap,
            }

        @registry.tool("drive_create_folder", write=True)
        def create_folder(name: str, parent_id: str | None = None) -> JSON:
            """Create a folder (in My Drive root unless parent_id is given)."""
            if not name.strip():
                raise GoogleMCPError("name must not be empty")
            body: JSON = {"name": name, "mimeType": FOLDER_MIME}
            if parent_id:
                body["parents"] = [parent_id]
            request = svc().files().create(body=body, fields=_FIELDS_PARAM)
            return shape_file(_execute_write(ctx, request, _PARENT_LIMIT, parent_id))

        @registry.tool("drive_upload_text_file", write=True)
        def upload_text_file(
            name: str,
            content: str,
            mime_type: str = "text/plain",
            parent_id: str | None = None,
            as_google_doc: bool = False,
        ) -> JSON:
            """Upload a small text file (at most 1 MiB). as_google_doc converts it to a Doc."""
            if not name.strip():
                raise GoogleMCPError("name must not be empty")
            if not _is_text(mime_type):
                raise GoogleMCPError("mime_type must be a text type")
            data = content.encode("utf-8")
            if len(data) > MAX_UPLOAD_BYTES:
                raise GoogleMCPError(f"content exceeds {MAX_UPLOAD_BYTES} bytes")
            body: JSON = {"name": name}
            if parent_id:
                body["parents"] = [parent_id]
            if as_google_doc:
                body["mimeType"] = GOOGLE_DOC_MIME
            media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime_type, resumable=False)
            request = svc().files().create(body=body, media_body=media, fields=_FIELDS_PARAM)
            return shape_file(_execute_write(ctx, request, _PARENT_LIMIT, parent_id))

        @registry.tool("drive_update_file_metadata", write=True, idempotent=True)
        def update_file_metadata(
            file_id: str,
            name: str | None = None,
            description: str | None = None,
            starred: bool | None = None,
        ) -> JSON:
            """Rename a file or change its description or starred flag.

            Works only on files this app created or the user opened with it (drive.file scope).
            """
            body: JSON = {}
            if name is not None:
                if not name.strip():
                    raise GoogleMCPError("name must not be empty")
                body["name"] = name
            if description is not None:
                body["description"] = description
            if starred is not None:
                body["starred"] = starred
            if not body:
                raise GoogleMCPError("nothing to update")
            request = svc().files().update(fileId=file_id, body=body, fields=_FIELDS_PARAM)
            return shape_file(_execute_write(ctx, request, _FILE_LIMIT, file_id))

        @registry.tool("drive_trash_file", write=True, destructive=True, idempotent=True)
        def trash_file(file_id: str) -> JSON:
            """Move a file to the trash (restorable for 30 days, then deleted by Google).

            Works only on files this app created or the user opened with it (drive.file scope).
            """
            request = (
                svc().files().update(fileId=file_id, body={"trashed": True}, fields=_FIELDS_PARAM)
            )
            return shape_file(_execute_write(ctx, request, _FILE_LIMIT, file_id))


ADAPTER = DriveAdapter()
