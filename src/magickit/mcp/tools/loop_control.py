"""Loop control MCP tools — per-project HOLD / RESUME.

Exposes conclair's ``/v1/projects/{project}/control`` endpoints so the
autonomous loop can be stopped and restarted from anywhere claude.ai
reaches, without a tailnet connection and without conclair's own UI
being up.

Three tools, deliberately not two. ``loop_control_set`` writes the
operator's *desired* state; ``loop_control_report_observed`` writes what
the loop actually read. Merging them into one tool with a flag would
undo the separation conclair enforces at the column level and would
remove the only lever that later makes "don't give the loop the setter"
expressible: you withhold a tool, not an argument.

These tools live outside ``chatroom.py`` for the same reason. They share
a backend service, not a subject: a caller granted the chatroom tools
has not thereby been granted the ability to stop or start the loop.
"""

from __future__ import annotations

from typing import Any, Literal

from fastmcp import FastMCP

from magickit.adapters.chatroom import ChatroomAdapter
from magickit.config import Settings
from magickit.utils.logging import get_logger

logger = get_logger(__name__)

_settings: Settings | None = None

#: The three-valued state. `supervised` is what the loop does today --
#: the design loop turns but nothing reaches code without a human decide
#: or a PR-gate REQUEST_CHANGES -- so it has to survive as a value, not
#: be flattened into a two-way on/off.
CONTROL_STATES = ("run", "supervised", "hold")


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


def register_tools(mcp: FastMCP, settings: Settings) -> None:
    """Register loop control MCP tools."""
    configure(settings)

    @mcp.tool()
    async def loop_control_get(project: str) -> dict[str, Any]:
        """Read a project's loop control state (desired + observed).

        USE THIS WHEN: checking whether a project's autonomous loop is
        allowed to run, or confirming that a HOLD has actually reached
        the loop.

        State meanings:
        - "run": fully autonomous. An independent naysayer's proceed
          carries a design through to the implementer.
        - "supervised": the design loop turns, but only a human decide or
          a PR-gate REQUEST_CHANGES reaches code.
        - "hold": the loop does not run. Sweeps do not start it, and a
          running conductor stops at the next round boundary.

        Args:
            project: chatroom project (e.g. "spirrow-voxelworld").

        Returns:
            {"project", "desired_state", "desired_actor", "desired_at",
             "observed_state", "observed_actor", "observed_at",
             "configured"}.

            `configured=false` means nobody has set a state for this
            project; `desired_state` then carries the effective default
            ("run") while `desired_actor` / `desired_at` are null.

            On failure: conclair's error envelope (`error_type` present,
            no `desired_state`), or a raised transport error if conclair
            is unreachable.

        IMPORTANT for automated callers: an unset project is a *success*
        answering "run", never a 404. So an error envelope from this tool
        means the read genuinely failed, and the loop must treat that as
        "hold" -- do not substitute a default. This tool never fabricates
        one on your behalf.
        """
        adapter = _adapter()
        try:
            return await adapter.get_loop_control(project=project)
        finally:
            await adapter.close()

    @mcp.tool()
    async def loop_control_set(
        project: str,
        state: Literal["run", "supervised", "hold"],
        actor: str,
        note: str = "",
    ) -> dict[str, Any]:
        """Set a project's desired loop control state (operator action).

        USE THIS WHEN: a human wants to stop a project's loop ("hold"),
        put it back under review ("supervised"), or let it run
        autonomously again ("run").

        This is the path that works from a phone with no tailnet and with
        conclair's own UI down; it is the redundant operator surface, not
        a convenience alias for it.

        Timing: this sets what the loop *should* do. It takes effect when
        the loop next reads it. A turn already in flight -- an
        implementation run is minutes -- completes first. This tool does
        not stop anything mid-turn, and nothing here should be reported
        to a user as an immediate stop.

        Args:
            project: chatroom project to control.
            state: "run" | "supervised" | "hold".
            actor: who is making the change. This is a **record, not a
                credential**: nothing authenticates it, and anyone who
                can reach this tool can change the value regardless of
                what they put here. It exists so the history says who
                said they did it.
            note: optional reason, kept in the history alongside the
                change.

        Returns:
            The same shape as `loop_control_get`, reflecting the new
            desired state. `observed_*` is left untouched -- setting a
            value does not mean the loop has seen it yet.
            On failure: conclair error envelope.

        Do NOT call this from inside the loop. Reporting what the loop
        read is `loop_control_report_observed`; a loop that could write
        `desired` could resume a project a human had stopped.
        """
        adapter = _adapter()
        try:
            return await adapter.set_loop_control(
                project=project,
                state=state,
                actor=actor,
                # Empty string at the MCP surface -> omitted on the wire,
                # matching the chatroom tools' handling of optional text.
                note=note or None,
            )
        finally:
            await adapter.close()

    @mcp.tool()
    async def loop_control_report_observed(
        project: str,
        state: Literal["run", "supervised", "hold"],
        actor: str,
    ) -> dict[str, Any]:
        """Report the control state the loop just read (loop only).

        USE THIS WHEN: the conductor has read the control state and is
        acting on it, so the operator's view can show that the setting
        landed. Call it when the observed value *changes*, not every
        round.

        This tool cannot change what the loop is supposed to do -- there
        is no way to express a desired state through it. That is the
        point: it is the half of the contract the loop is given, while
        `loop_control_set` is the half it is not.

        Args:
            project: chatroom project the loop is running.
            state: the value the loop read and is acting on.
            actor: which loop component is reporting (e.g.
                "mindwire-conductor").

        Returns:
            The same shape as `loop_control_get`, with `observed_*`
            updated and `desired_*` unchanged.
            On failure: conclair error envelope.
        """
        adapter = _adapter()
        try:
            return await adapter.report_loop_control_observed(
                project=project,
                state=state,
                actor=actor,
            )
        finally:
            await adapter.close()
