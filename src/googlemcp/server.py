"""MCP server assembly: policy-filtered adapter tools on FastMCP over stdio.

The server runs locally as a child process of its MCP client and speaks MCP
over stdin and stdout only. There is no network listener, so there is no
MCP-level authentication: the client that starts the process owns it. Google
API access uses the signed-in account's locally stored refresh token (see
``googlemcp.auth``).
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable, Mapping
from typing import Any, Final

import requests
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import Tool
from google.auth.exceptions import RefreshError
from mcp.types import ToolAnnotations

from googlemcp import __version__
from googlemcp.auth.credentials import AuthError, CredentialProvider, map_refresh_error
from googlemcp.config import Settings
from googlemcp.context import WITHHELD_STATUSES, GoogleServiceContext, withheld_message
from googlemcp.policy import select_tools
from googlemcp.redaction import redact
from googlemcp.secretstore import SecretStore
from googlemcp.services import discover_adapters
from googlemcp.services.base import (
    JSON,
    GoogleMCPError,
    ServiceAdapter,
    ServiceContext,
    ToolDef,
    UpstreamError,
)

log = logging.getLogger(__name__)

TRANSPORT: Final = "stdio"
INSTRUCTIONS = (
    "Tools for the user's Google account, authorized locally with `googlemcp auth login`. "
    "Read tools are safe to call. Write tools change Google data; destructive tools delete, "
    "trash, or send and cannot be undone. Prefer small page sizes and follow next_page_token "
    "for more results."
)


def annotations_for(tool: ToolDef) -> ToolAnnotations:
    """MCP hints. Every hint is explicit because the spec defaults
    ``destructiveHint`` to true, which would mislabel safe write tools."""
    return ToolAnnotations(
        title=tool.name.replace("_", " "),
        read_only_hint=not tool.write,
        destructive_hint=tool.destructive,
        idempotent_hint=True if not tool.write else tool.idempotent,
        open_world_hint=tool.open_world,
    )


def wrap_tool(tool: ToolDef) -> Callable[..., JSON]:
    """Convert library errors into MCP tool errors without leaking internals.

    Every message is passed through ``redact`` so URLs and credential-looking
    query values from Google, including text that REST-only adapters copy
    from responses into their own ``UpstreamError``, never reach the client.
    """
    fn = tool.fn

    @functools.wraps(fn)
    def run(**kwargs: Any) -> JSON:
        try:
            return fn(**kwargs)
        except UpstreamError as exc:
            # Same rule as the service context, for REST-only adapters that
            # build their own UpstreamError from response text. AuthError
            # messages are written by this package and stay as they are.
            if exc.status in WITHHELD_STATUSES and not isinstance(exc, AuthError):
                raise ToolError(withheld_message(exc.status, exc.reason)) from None
            raise ToolError(redact(str(exc))) from None
        except GoogleMCPError as exc:
            raise ToolError(redact(str(exc))) from None
        except RefreshError as exc:
            # AuthorizedSession (REST-only adapters) refreshes on its own and
            # raises google-auth errors directly.
            raise ToolError(redact(str(map_refresh_error(exc)))) from None
        except requests.RequestException as exc:
            raise ToolError(f"network error calling Google ({type(exc).__name__})") from None
        except Exception:
            log.exception("tool %s failed", tool.name)
            raise ToolError(f"{tool.name} failed with an internal error; see server logs") from None

    return run


def to_fastmcp_tool(product: str, tool: ToolDef) -> Tool:
    return Tool.from_function(
        wrap_tool(tool),
        name=tool.name,
        description=tool.description,
        annotations=annotations_for(tool),
        tags={product, "write" if tool.write else "read"}
        | ({"destructive"} if tool.destructive else set()),
        # Adapters are synchronous; keep them off the event loop.
        run_in_thread=True,
    )


def build_server(
    settings: Settings,
    store: SecretStore,
    *,
    adapters: Mapping[str, ServiceAdapter] | None = None,
    ctx: ServiceContext | None = None,
) -> FastMCP:
    """Assemble the FastMCP server. ``ctx`` and ``adapters`` are injectable for tests.

    Building performs no network or secret I/O: credentials load lazily on
    the first Google call.
    """
    adapters = discover_adapters() if adapters is None else adapters
    if ctx is None:
        ctx = GoogleServiceContext(CredentialProvider(store), max_retries=settings.max_retries)
    mcp: FastMCP = FastMCP(
        name="googlemcp",
        instructions=INSTRUCTIONS,
        version=__version__,
        mask_error_details=True,
    )
    for product, tool in select_tools(settings, adapters, ctx):
        mcp.add_tool(to_fastmcp_tool(product, tool))
    return mcp


def serve(settings: Settings, store: SecretStore) -> None:
    """Serve MCP over stdio until the client closes the pipe."""
    build_server(settings, store).run(transport=TRANSPORT, show_banner=False)
