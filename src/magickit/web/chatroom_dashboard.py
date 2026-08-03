"""Chatroom summary fragment for the dashboard.

The dashboard already answers "what is the task queue doing?". This adds
the other half of a project's state -- the discussion between sessions --
so one page answers "which projects need me?" instead of requiring a trip
into the chatroom UI to find out whether anything is waiting.

Rendered server-side as an HTMX fragment, matching how the rest of the
dashboard's panels work.
"""

from __future__ import annotations

import html
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from magickit.mcp.tools import chatroom as chatroom_tools
from magickit.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["chatroom-ui"])

# Statuses that mean "someone still owes this thread something". Anything
# else (resolved / superseded / parked) is not actionable and is folded away
# so the number on screen is a to-do count, not a total.
OPEN_STATUSES = ("active", "awaiting_reply")

# How many projects the panel shows before it stops. The list is sorted
# most-recently-active first, so the tail is the cold end.
MAX_ROWS = 8


def _open_count(entry: dict[str, Any]) -> int:
    by_status = entry.get("threads_by_status") or {}
    return sum(by_status.get(status, 0) for status in OPEN_STATUSES)


def _row(entry: dict[str, Any]) -> str:
    project = str(entry.get("project", ""))
    safe = html.escape(project)
    open_threads = _open_count(entry)
    awaiting = (entry.get("threads_by_status") or {}).get("awaiting_reply", 0)
    gated = entry.get("gated_thread_count", 0)

    # Reuse the dashboard's existing badge modifiers rather than inventing
    # new colours: "running" is its amber, "failed" its red. Waiting on a
    # reply is in-flight; a gate is blocking.
    badges = ""
    if awaiting:
        badges += (
            f'<span class="status-badge running">{awaiting} awaiting reply</span> '
        )
    if gated:
        badges += f'<span class="status-badge failed">{gated} gated</span> '
    if not badges:
        badges = "—"

    return f"""
        <tr>
            <td><a href="/ui/projects/{safe}/threads">{safe}</a></td>
            <td>{open_threads}</td>
            <td>{entry.get('thread_count', 0)}</td>
            <td>{entry.get('message_count', 0)}</td>
            <td>{badges}</td>
        </tr>
    """


@router.get("/dashboard/chatroom", response_class=HTMLResponse)
async def dashboard_chatroom(request: Request) -> HTMLResponse:
    """Render the per-project chatroom summary."""
    adapter = chatroom_tools._adapter()
    try:
        payload = await adapter.list_project_summaries()
    except Exception as e:  # noqa: BLE001 - a dead panel must not kill the page
        logger.warning("Chatroom summary unavailable", error=str(e))
        return HTMLResponse(
            '<p class="empty-state">chatroom summary unavailable '
            "(<code>spirrow-conclair.service</code>)</p>"
        )
    finally:
        await adapter.close()

    if "error_type" in payload:
        return HTMLResponse(
            f'<p class="empty-state">chatroom summary unavailable: '
            f'{html.escape(str(payload.get("error", "")))}</p>'
        )

    items = payload.get("items", [])
    # Projects with nothing open are noise on a "what needs me" panel, but a
    # project that has gone quiet is worth seeing too -- so rank by open
    # work and keep the ordering the API gave us within that.
    ranked = sorted(items, key=_open_count, reverse=True)
    shown = ranked[:MAX_ROWS]

    if not shown:
        return HTMLResponse('<p class="empty-state">no chatroom activity yet</p>')

    rows = "".join(_row(entry) for entry in shown)
    hidden = len(ranked) - len(shown)
    footer = (
        f'<p class="empty-state">+{hidden} more '
        f'<a href="/ui/">in the chatroom UI</a></p>'
        if hidden > 0
        else '<p class="empty-state"><a href="/ui/">open the chatroom UI</a></p>'
    )

    return HTMLResponse(
        f"""
        <table class="table">
            <thead>
                <tr>
                    <th>project</th>
                    <th>open</th>
                    <th>threads</th>
                    <th>msgs</th>
                    <th>needs attention</th>
                </tr>
            </thead>
            <tbody>{rows}</tbody>
        </table>
        {footer}
        """
    )
