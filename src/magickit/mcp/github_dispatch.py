"""Passthrough dispatcher for the proxied github-mcp tools.

Claude connectors freeze the MCP tool list at connection time and ignore
tools/list_changed, so dynamically revealing toolsets is impossible. To keep
the context cost low while retaining full GitHub capability, the 35 github-mcp
tools are collapsed into a single dispatcher tool plus an on-demand schema
lookup, instead of being exposed individually.

- ``github(operation, arguments)``: Claude picks the operation name and builds
  the arguments; the call is forwarded to the github-mcp container.
- ``github_operations(name_filter)``: returns the exact upstream input schemas
  for matching operations, so Claude can confirm argument shapes on demand.
  This is a normal tool result, so it works under the connector's fixed tool
  list (unlike tools/list_changed).
"""

from __future__ import annotations

import os
from typing import Any

from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport

from magickit.utils.logging import get_logger

logger = get_logger(__name__)

# Compact operation catalog. Toolsets are fixed at repos,issues,pull_requests;
# names are the upstream github-mcp tool names (no prefix). Keep this in sync
# if the container's --toolsets change.
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


def install_github_dispatch(mcp: FastMCP) -> None:
    """Register the github dispatcher and schema-lookup tools.

    No-op unless GITHUB_MCP_PAT is set, so the no-auth tailnet instance and
    the test suite are unaffected.

    Args:
        mcp: The Magickit FastMCP server.
    """
    pat = os.environ.get("GITHUB_MCP_PAT")
    if not pat:
        logger.info("github dispatcher disabled (GITHUB_MCP_PAT unset)")
        return

    url = os.environ.get("GITHUB_MCP_URL", "http://127.0.0.1:8116/mcp")
    transport = StreamableHttpTransport(
        url, headers={"Authorization": f"Bearer {pat}"}
    )

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
            async with Client(transport) as c:
                res = await c.call_tool(operation, arguments or {})
        except Exception as e:  # noqa: BLE001 - surface as guidance to caller
            return {
                "error": f"{type(e).__name__}: {e}",
                "hint": (
                    "Check the operation name, or call "
                    "github_operations(name_filter) for the exact input schema."
                ),
            }
        if getattr(res, "data", None) is not None:
            return res.data
        return [getattr(b, "text", str(b)) for b in (res.content or [])]

    @mcp.tool(
        name="github_operations",
        description=(
            "List github-mcp operations with their exact JSON input schemas. "
            "Optional `name_filter` is a case-insensitive substring "
            "(e.g. 'pull_request', 'issue', 'file')."
        ),
    )
    async def github_operations(name_filter: str | None = None) -> list[dict]:
        async with Client(transport) as c:
            tools = await c.list_tools()
        nf = (name_filter or "").lower()
        return [
            {
                "operation": t.name,
                "description": (t.description or "")[:200],
                "input_schema": t.inputSchema,
            }
            for t in tools
            if nf in t.name.lower()
        ]

    logger.info("github dispatcher installed", url=url)
