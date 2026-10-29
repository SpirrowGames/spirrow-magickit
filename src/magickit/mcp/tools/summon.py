"""Summon (human call) MCP tools — mark / unmark a project's active Summon.

A Summon is the operator's mark that one thread must be worked first:
mindwire's sweep picks it up ahead of everything else and holds the other
threads of that project (and of projects sharing its repo owner) at the
next round boundary until the mark is cleared. The mark is stored on
Conclair's project control row (``/v1/projects/{project}/control/summon``)
and read back through ``loop_control_get``'s ``active_summon`` key; there
is deliberately no separate read tool.

These two tools are **human-only**. They live in their own module, not in
``loop_control.py``, for the same reason ``loop_control_set`` and
``loop_control_report_observed`` are separate tools: permission is
expressed by *withholding a tool*, not an argument. Do not put them on a
conductor or executor allowlist — a loop that could clear a Summon could
resume what a human stopped.

Conclair's error envelopes pass through unaltered (``summon_conflict``,
``summon_mismatch``, ``summon_thread_not_open``, 404, ``invalid_argument``).
Blank ``thread_id`` / ``by`` are refused here first with the same envelope
shape, so the request never reaches Conclair.
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from magickit.adapters.chatroom import ChatroomAdapter
from magickit.config import Settings
from magickit.utils.logging import get_logger

logger = get_logger(__name__)

_settings: Settings | None = None


def _adapter() -> ChatroomAdapter:
    if _settings is None:
        raise RuntimeError("Settings not initialized")
    return ChatroomAdapter(
        base_url=_settings.conclair_url,
        timeout=_settings.conclair_timeout,
    )


def configure(settings: Settings) -> None:
    """Bind the settings ``_adapter()`` reads."""
    global _settings
    _settings = settings


def _blank_error(**fields: str) -> dict[str, Any] | None:
    """Conclair-shaped 422 envelope for blank / whitespace-only fields.

    Mirrors Conclair's check (strip, then empty) and the contract's
    ``blank thread_id`` case (``" "`` is blank). Returns ``None`` when all
    fields are non-blank.
    """
    blank = [name for name, value in fields.items() if not value.strip()]
    if not blank:
        return None
    return {
        "error_type": "invalid_argument",
        "error": f"{', '.join(blank)} must be a non-blank string",
        "details": {"fields": blank},
    }


def register_tools(mcp: FastMCP, settings: Settings) -> None:
    """Register Summon MCP tools."""
    configure(settings)

    @mcp.tool()
    async def summon_set(
        project: str,
        thread_id: str,
        by: str,
        note: str = "",
    ) -> dict[str, Any]:
        """Mark a thread as the project's active Summon (operator action).

        USE THIS WHEN: a human wants one thread worked before anything else.
        mindwire picks it first and holds the project's other threads (and
        those of projects sharing its repo owner) at the next round boundary
        until the mark is cleared.

        At most one Summon per project. If another thread already holds it,
        this is refused with ``summon_conflict`` (the current mark is in the
        envelope) — nothing is overwritten. To move it, ``summon_clear`` the
        current one first. Re-marking the same thread is idempotent and
        keeps the original ``since``.

        Args:
            project: chatroom project.
            thread_id: the thread to call into. It must exist in ``project``
                (404 otherwise) and be open (409 ``summon_thread_not_open``).
            by: who is setting it. A **record, not a credential**: nothing
                authenticates it. It is kept so the history says who said
                they did it.
            note: optional reason, kept in the control history.

        Returns:
            The ``loop_control_get`` shape, with ``active_summon`` set.
            On failure: Conclair's error envelope (``error_type`` present).

        Do NOT call this from inside the loop.
        """
        refused = _blank_error(thread_id=thread_id, by=by)
        if refused is not None:
            return refused
        adapter = _adapter()
        try:
            return await adapter.set_summon(
                project=project, thread_id=thread_id, by=by, note=note or None
            )
        finally:
            await adapter.close()

    @mcp.tool()
    async def summon_clear(
        project: str,
        thread_id: str,
        by: str,
        note: str = "",
    ) -> dict[str, Any]:
        """Clear the project's active Summon (operator action).

        USE THIS WHEN: the called-in task is done, or the Summon should be
        moved elsewhere.

        Compare-and-clear: ``thread_id`` must name the mark currently held,
        otherwise this is refused with ``summon_mismatch`` and nothing is
        cleared (a stale screen cannot erase a newer mark). With no mark set
        it is a 200 no-op. Closing or resolving the thread clears the mark
        on Conclair's side; a ``STOP: done`` does not.

        Args:
            project: chatroom project.
            thread_id: the thread you believe holds the mark.
            by: who is clearing it. A **record, not a credential**.
            note: optional reason, kept in the control history.

        Returns:
            The ``loop_control_get`` shape, with ``active_summon`` null.
            On failure: Conclair's error envelope.

        Do NOT call this from inside the loop.
        """
        refused = _blank_error(thread_id=thread_id, by=by)
        if refused is not None:
            return refused
        adapter = _adapter()
        try:
            return await adapter.clear_summon(
                project=project, thread_id=thread_id, by=by, note=note or None
            )
        finally:
            await adapter.close()
