"""Passthrough dispatcher for the github-mcp container.

Claude connectors freeze the MCP tool list at connection time and ignore
tools/list_changed, so dynamically revealing toolsets is impossible. To keep
the context cost low while retaining full GitHub capability, the 35 github-mcp
tools are collapsed into a single dispatcher tool plus an on-demand schema
lookup, instead of being exposed individually.

- ``github(operation, arguments)``: Claude picks the operation name and builds
  the arguments; the call is forwarded to the github-mcp container.
- ``github_operations(name_filter)``: returns the exact upstream input schemas
  on demand (a normal tool result, so it works under the connector's fixed
  tool list, unlike tools/list_changed).

Upstream transport: a minimal, **stateless per-call** httpx JSON-RPC client.
We deliberately avoid FastMCP's StreamableHttp client here: its stateful
session + SSE GET stream (which github-mcp answers with 405, triggering an
endless reconnect loop) was the suspected cause of intermittent 400s in the
long-lived service. github-mcp's HTTP endpoint accepts a plain initialize +
request flow, which is all we need.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from magickit.utils.logging import get_logger

logger = get_logger(__name__)

_PROTOCOL_VERSION = "2025-06-18"

# Compact operation catalog. Toolsets are fixed at repos,issues,pull_requests;
# names are the upstream github-mcp tool names (no prefix). Keep in sync if the
# container's --toolsets change.
_CATALOG = """\
Available `operation` values (github-mcp; toolsets repos/issues/pull_requests).
Arguments follow github-mcp's own schema — call github_operations(name_filter)
for the exact input schema of any operation.

repos/files: search_repositories, search_code, create_repository,
  fork_repository, get_file_contents, create_or_update_file, delete_file,
  push_files, create_branch, list_branches, get_commit, list_commits
issues: issue_read, issue_write, add_issue_comment, list_issues,
  list_issue_types, search_issues, sub_issue_write
pull_requests: pull_request_read, create_pull_request, update_pull_request,
  update_pull_request_branch, list_pull_requests, merge_pull_request,
  pull_request_review_write, add_comment_to_pending_review,
  add_reply_to_pull_request_comment, search_pull_requests
releases/tags: get_latest_release, get_release_by_tag, list_releases,
  get_tag, list_tags, get_label"""


class _UpstreamError(RuntimeError):
    """github-mcp returned a non-2xx or a JSON-RPC error."""


def _parse_mcp_response(resp: httpx.Response) -> Any:
    """Extract the JSON-RPC ``result`` from a JSON or SSE response body."""
    ctype = resp.headers.get("content-type", "")
    if "text/event-stream" in ctype:
        payload = None
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                payload = json.loads(line[5:].strip())
        if payload is None:
            raise _UpstreamError("empty SSE stream from github-mcp")
    else:
        payload = resp.json()
    if isinstance(payload, dict) and payload.get("error"):
        raise _UpstreamError(f"JSON-RPC error: {payload['error']}")
    return payload.get("result") if isinstance(payload, dict) else payload


async def _mcp_call(method: str, params: dict[str, Any]) -> Any:
    """One stateless MCP exchange: initialize, then the requested method.

    Raises:
        _UpstreamError: github-mcp returned an HTTP or JSON-RPC error. The
            message includes the upstream status and body snippet so the
            failure is self-diagnosing in logs and tool results.
    """
    url = os.environ.get("GITHUB_MCP_URL", "http://127.0.0.1:8116/mcp")
    pat = os.environ["GITHUB_MCP_PAT"]
    headers = {
        "Authorization": f"Bearer {pat}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Mcp-Protocol-Version": _PROTOCOL_VERSION,
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        init = await client.post(
            url,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": _PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "magickit-dispatch", "version": "1"},
                },
            },
        )
        if init.status_code >= 400:
            raise _UpstreamError(
                f"initialize HTTP {init.status_code}: {init.text[:300]}"
            )
        sid = init.headers.get("mcp-session-id")
        if sid:
            headers["Mcp-Session-Id"] = sid

        await client.post(
            url,
            headers=headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )

        resp = await client.post(
            url,
            headers=headers,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
        if resp.status_code >= 400:
            raise _UpstreamError(
                f"{method} HTTP {resp.status_code}: {resp.text[:300]}"
            )
        return _parse_mcp_response(resp)


def _err(e: Exception) -> dict:
    """Uniform error envelope for both tools."""
    return {
        "error": f"{type(e).__name__}: {str(e)[:400]}",
        "hint": (
            "If this is an HTTP/JSON-RPC error the github-mcp upstream is the "
            "problem (status/body included above), not the operation name. "
            "Otherwise check the operation name or call "
            "github_operations(name_filter) for the exact input schema."
        ),
    }


def install_github_dispatch(mcp) -> None:
    """Register the github dispatcher and schema-lookup tools.

    No-op unless GITHUB_MCP_PAT is set, so the no-auth tailnet instance and
    the test suite are unaffected.

    Args:
        mcp: The Magickit FastMCP server.
    """
    if not os.environ.get("GITHUB_MCP_PAT"):
        logger.info("github dispatcher disabled (GITHUB_MCP_PAT unset)")
        return

    @mcp.tool(
        name="github",
        description=(
            "Call a GitHub operation through the github-mcp server. Set "
            "`operation` to one of the names below and `arguments` to that "
            "operation's parameters.\n\n" + _CATALOG
        ),
    )
    async def github(operation: str, arguments: dict | None = None) -> Any:
        try:
            result = await _mcp_call(
                "tools/call",
                {"name": operation, "arguments": arguments or {}},
            )
        except Exception as e:  # noqa: BLE001 - surfaced as guidance to caller
            logger.warning("github dispatch failed", op=operation, err=str(e))
            return _err(e)
        content = result.get("content") if isinstance(result, dict) else None
        if content:
            return [c.get("text", c) for c in content]
        return result

    @mcp.tool(
        name="github_operations",
        description=(
            "List github-mcp operations with their exact JSON input schemas. "
            "Optional `name_filter` is a case-insensitive substring "
            "(e.g. 'pull_request', 'issue', 'file')."
        ),
    )
    async def github_operations(name_filter: str | None = None) -> Any:
        try:
            result = await _mcp_call("tools/list", {})
        except Exception as e:  # noqa: BLE001 - same envelope as github()
            logger.warning("github_operations failed", err=str(e))
            return _err(e)
        nf = (name_filter or "").lower()
        return [
            {
                "operation": t.get("name"),
                "description": (t.get("description") or "")[:200],
                "input_schema": t.get("inputSchema"),
            }
            for t in result.get("tools", [])
            if nf in (t.get("name") or "").lower()
        ]

    logger.info("github dispatcher installed (stateless httpx client)")
