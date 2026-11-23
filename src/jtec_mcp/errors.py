"""Errors the agent should see. The SDK passes a ToolError's message through to
the client; any other exception becomes a bare "Error executing tool"."""

from __future__ import annotations

import httpx
from mcp.server.mcpserver.exceptions import ToolError

__all__ = ["ToolError", "check"]


def check(r: httpx.Response, service: str) -> httpx.Response:
    """Raise a ToolError carrying the service's own error message."""
    if r.status_code < 400:
        return r
    try:
        body = r.json()
        detail = (
            (body.get("message") or body.get("error") or body) if isinstance(body, dict) else body
        )
    except ValueError:
        detail = r.text[:300]
    raise ToolError(f"{service} {r.request.method} {r.request.url.path}: {r.status_code} {detail}")
