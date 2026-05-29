"""Chatroom MCP tools — thin wrappers around ChatroomAdapter.

Exposes spirrow-conclair's HTTP endpoints as MCP tools so AI sessions
(Claude.ai / Claude Code) can post / read chatroom messages without
hand-crafting HTTP calls. Each tool delegates to ChatroomAdapter and
forwards the response as-is, including conclair's structured error
envelope (`{error_type, error, details}`) when the upstream returned
4xx/5xx.
"""

from __future__ import annotations

from typing import Any, Literal

from fastmcp import FastMCP

from magickit.adapters.chatroom import ChatroomAdapter
from magickit.config import Settings
from magickit.utils.logging import get_logger

logger = get_logger(__name__)

_settings: Settings | None = None

# ADR-2026-05-29-12 §3: identity_names that are exempt from the
# mandatory embodiment declaration on state-transitioning msgs
# ({handoff, ack, decide} per §4). The "human" identity does not
# operate Magickit as a calling agent (ADR-10 §2.7 "human is the
# above-loop approval layer"); they normally act via Bohr / Heisenberg /
# Einstein. The set is intentionally tiny -- if other actor classes
# need the exemption later, treat that as a separate ADR.
HUMAN_IDENTITY_NAMES = ("human",)

# msg types whose post requires an embodiment declaration (state-
# transitioning per msg-325 §4 N-2 取り込み). close_thread emits a
# decide internally so it's enforced separately by the close wrapper.
MANDATORY_EMBODIMENT_MSG_TYPES = ("handoff", "ack", "decide")


def _adapter() -> ChatroomAdapter:
    if _settings is None:
        raise RuntimeError("Settings not initialized")
    return ChatroomAdapter(
        base_url=_settings.conclair_url,
        timeout=_settings.conclair_timeout,
    )


def _embodiment_required_error(*, msg_kind: str) -> dict[str, Any]:
    """Magickit-side embodiment-required rejection envelope.

    Shape matches the project ``error_type`` convention (msg-002 §1.4 /
    msg-010 D-9) so callers can branch on the error class without
    parsing the human-readable message.
    """
    return {
        "error_type": "EmbodimentRequiredError",
        "error": (
            f"embodiment is required for {msg_kind} "
            "(ADR-2026-05-29-12 mandatory-on-state-transition; "
            "humans are exempt)"
        ),
        "details": {
            "msg_kind": msg_kind,
            "mandatory_msg_types": list(MANDATORY_EMBODIMENT_MSG_TYPES),
            "exempt_identity_names": list(HUMAN_IDENTITY_NAMES),
        },
    }


def register_tools(mcp: FastMCP, settings: Settings) -> None:
    """Register chatroom MCP tools."""
    global _settings
    _settings = settings

    @mcp.tool()
    async def chatroom_open_thread(
        project: str,
        thread_id: str,
        title: str,
        owner: str,
        propose_content: str,
        tags: list[str] | None = None,
        commit_ref: str = "",
        embodiment: str = "",
    ) -> dict[str, Any]:
        """Open a new chatroom thread (creates the thread + propose msg).

        USE THIS WHEN: you want to start a new discussion / handoff /
        decision thread between AI sessions.

        Args:
            project: project identifier (e.g. "spirrow-voxelworld").
            thread_id: thread id, must be unique within the project
                (convention: "T-" prefix + descriptive slug, e.g.
                "T-D4-radius").
            title: 1-line thread title.
            owner: author string (e.g. "claude.ai" / "claude-code" /
                "human"). The owner is the only one allowed to close.
            propose_content: markdown body of the propose message.
            tags: optional thread-level tags.
            commit_ref: optional git hash to record on the propose msg.
            embodiment: ADR-2026-05-29-12 self-declared runtime form
                (web_ai_chat / terminal_coding_agent / unknown). Optional
                for ``propose`` (Einstein N-3 / msg-325 §4: propose is
                covered by the receiver here but not in the mandatory
                set). Recorded on the propose msg if supplied.

        Returns:
            On success: {"thread": {...}, "msg": {...}}.
            On failure: conclair error envelope
            {"error_type": "ChatroomIntegrityError", "error": "...",
             "details": {...}}.
        """
        adapter = _adapter()
        try:
            return await adapter.open_thread(
                project=project,
                thread_id=thread_id,
                title=title,
                owner=owner,
                propose_content=propose_content,
                tags=tags,
                commit_ref=commit_ref or None,
                embodiment=embodiment or None,
            )
        finally:
            await adapter.close()

    @mcp.tool()
    async def chatroom_post_message(
        project: str,
        thread_id: str,
        msg_type: Literal[
            "propose", "question", "answer", "decide", "report", "handoff", "ack"
        ],
        author: str,
        content: str,
        reply_to: str = "",
        references_threads: list[str] | None = None,
        related_tasks: list[str] | None = None,
        closes_thread: str = "",
        tags: list[str] | None = None,
        commit_ref: str = "",
        embodiment: str = "",
    ) -> dict[str, Any]:
        """Post a message to an existing thread.

        USE THIS WHEN: replying inside a thread, asking a question,
        reporting progress, or handing off / acknowledging a handoff.
        For closing a thread (decide + closes_thread), prefer
        chatroom_close_thread which also enforces the owner check.

        msg_type semantics (per chatroom spec):
        - propose: rejected here (only the first msg of a thread can be
          propose; that one is created by chatroom_open_thread)
        - question / answer: normal Q&A
        - report: progress / outcome notes
        - handoff: thread.status -> awaiting_reply
        - ack: thread.status (awaiting_reply) -> active
        - decide: declarative decision; with closes_thread set, must be
          authored by the owner and resolves the thread

        embodiment (ADR-2026-05-29-12 self-declared runtime form):
        - mandatory for msg_type in {handoff, ack, decide} (state-
          transitioning posts per msg-325 §4 N-2)
        - exempt when ``author`` is the human identity (msg-325 §3:
          humans don't operate Magickit as a calling agent)
        - optional for question / answer / report (recorded if supplied)

        NOTE: this parameter is named `msg_type` (not `type`) because
        some MCP clients reject schemas that use `type` as a property
        name — they collide with JSON Schema's own `type` keyword.

        Returns:
            On success: {"msg": {...}, "thread_status_changed_to":
            null|"awaiting_reply"|"active"|"resolved"}.
            On failure (embodiment missing, conclair error, ...):
            error_type envelope.
        """
        # Magickit-side enforcement (F-04: Magickit is the sole role/
        # embodiment validation point; Conclair only persists).
        if (
            msg_type in MANDATORY_EMBODIMENT_MSG_TYPES
            and author not in HUMAN_IDENTITY_NAMES
            and not embodiment
        ):
            return _embodiment_required_error(msg_kind=f"msg_type={msg_type}")

        adapter = _adapter()
        try:
            return await adapter.post_message(
                project=project,
                thread_id=thread_id,
                type=msg_type,
                author=author,
                content=content,
                reply_to=reply_to or None,
                references_threads=references_threads,
                related_tasks=related_tasks,
                closes_thread=closes_thread or None,
                tags=tags,
                commit_ref=commit_ref or None,
                embodiment=embodiment or None,
            )
        finally:
            await adapter.close()

    @mcp.tool()
    async def chatroom_close_thread(
        project: str,
        thread_id: str,
        summary_content: str,
        author: str,
        affects_threads: list[str] | None = None,
        related_tasks: list[str] | None = None,
        tags: list[str] | None = None,
        commit_ref: str = "",
        embodiment: str = "",
    ) -> dict[str, Any]:
        """Close an active thread by posting a decide msg (owner-only).

        USE THIS WHEN: the thread reaches a conclusion and the owner
        wants to record a summary post. Only the original owner may
        call this; non-owner attempts return ChatroomPermissionError.

        embodiment (ADR-2026-05-29-12 self-declared runtime form):
        - mandatory because close emits a ``decide`` msg internally
          (msg-325 §4 mandatory set)
        - exempt when ``author`` is the human identity

        Args:
            summary_content: markdown body of the decide msg. Should
                contain a clear conclusion + decision points so the
                summary stands on its own.
            affects_threads: optional list of thread_ids this decision
                impacts; recorded on the thread row.
            embodiment: see above. Mandatory for non-human authors.

        Returns:
            On success: {"thread": {... status=resolved ...},
                         "decide_msg": {...}}.
            On failure (embodiment missing -> EmbodimentRequiredError,
            non-owner -> 403, already resolved -> 409, etc.):
            error_type envelope.
        """
        # close_thread emits a decide msg internally; same mandatory
        # rule as msg_type="decide" on post_message.
        if author not in HUMAN_IDENTITY_NAMES and not embodiment:
            return _embodiment_required_error(msg_kind="close_thread (emits decide)")

        adapter = _adapter()
        try:
            return await adapter.close_thread(
                project=project,
                thread_id=thread_id,
                summary_content=summary_content,
                author=author,
                affects_threads=affects_threads,
                related_tasks=related_tasks,
                tags=tags,
                commit_ref=commit_ref or None,
                embodiment=embodiment or None,
            )
        finally:
            await adapter.close()

    @mcp.tool()
    async def chatroom_list_threads(
        project: str,
        status_filter: list[str] | None = None,
        owner: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List threads in a project with optional filters.

        USE THIS WHEN: starting a session and you want the list of
        active / awaiting_reply threads owned by you, or surveying the
        project's open work.

        Args:
            status_filter: optional list of statuses to include
                (active / awaiting_reply / resolved / superseded /
                parked). Empty = all.
            owner: optional owner filter (exact match).
            limit: 1..1000, default 100.
            offset: pagination, default 0.

        Returns:
            {"items": [Thread...], "total": int, "limit": int,
             "offset": int}.
        """
        adapter = _adapter()
        try:
            return await adapter.list_threads(
                project=project,
                status_filter=status_filter,
                owner=owner or None,
                limit=limit,
                offset=offset,
            )
        finally:
            await adapter.close()

    @mcp.tool()
    async def chatroom_get_thread(
        project: str,
        thread_id: str,
        mode: Literal["full", "summary"] = "full",
    ) -> dict[str, Any]:
        """Fetch a thread plus its messages.

        USE THIS WHEN: you need to read the conversation. For resolved
        threads use mode="summary" to load only the decide msg
        (saves tokens).

        Args:
            mode: "full" (default) returns every msg in numeric msg_id
                order. "summary" on a resolved thread returns only the
                decide msg; on active / awaiting_reply / superseded /
                parked it behaves the same as "full".

        Returns:
            {"thread": Thread, "messages": [Message...], "mode":
             "full"|"summary"}.
        """
        adapter = _adapter()
        try:
            return await adapter.get_thread(
                project=project, thread_id=thread_id, mode=mode
            )
        finally:
            await adapter.close()

    @mcp.tool()
    async def chatroom_list_events(
        project: str,
        thread_id: str = "",
        action: Literal[
            "", "open_thread", "post_message", "status_transition", "mark_read",
        ] = "",
        since: str = "",
        until: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List audit log events (thread / message activity).

        USE THIS WHEN: tracing what changed in the chatroom over a time
        window, or auditing a specific thread's history.

        Args:
            thread_id: optional filter to a single thread.
            action: optional filter — "open_thread" / "post_message" /
                "status_transition" / "mark_read". Empty = all actions.
            since: ISO 8601 inclusive lower bound (e.g.
                "2026-05-01T00:00:00Z"). Empty = no lower bound.
            until: ISO 8601 exclusive upper bound. Empty = no upper bound.
            limit: 1..1000, default 100.

        Returns:
            {"items": [Event...], "total": int, "limit": int,
             "offset": int}, ordered (timestamp DESC, id DESC).
        """
        adapter = _adapter()
        try:
            return await adapter.list_events(
                project=project,
                thread_id=thread_id or None,
                action=action or None,
                since=since or None,
                until=until or None,
                limit=limit,
                offset=offset,
            )
        finally:
            await adapter.close()

    @mcp.tool()
    async def chatroom_check_integrity(project: str) -> dict[str, Any]:
        """Audit chatroom invariants for a project.

        USE THIS WHEN: troubleshooting suspected data corruption, or
        running a periodic health audit.

        Returns:
            {"issues": [IntegrityIssue...], "issue_count": int,
             "checked_at": ISO 8601 timestamp}. Always 200 even when
             issues exist (this is a report endpoint, not enforcement).
        """
        adapter = _adapter()
        try:
            return await adapter.check_integrity(project=project)
        finally:
            await adapter.close()

    @mcp.tool()
    async def chatroom_mark_read(
        project: str,
        thread_id: str,
        identity_name: str,
        up_to_msg_id: str = "",
    ) -> dict[str, Any]:
        """Advance my read cursor on a thread (the only way to advance it).

        USE THIS WHEN: I've finished reading a thread (or up to a point in
        it) and want to record that, so a later `chatroom_my_unread` call
        doesn't surface it again.

        Cursor model (see CLAUDE.md "Read cursor" section):
        - per (project, identity_name, thread_id), records
          `last_read_msg_id`.
        - **`chatroom_get_thread` and `chatroom_list_threads` do NOT advance
          the cursor** -- only this tool does, to prevent "I just opened
          the thread to peek" from silently being recorded as "I've read
          everything in it".
        - monotonic forward only: a request older than the current cursor
          is a silent no-op (`advanced=false`); the response always
          reflects the *current* cursor state.

        Args:
            project: chatroom project (e.g. "spirrow-magickit").
            thread_id: target thread.
            identity_name: whose cursor (e.g. "Heisenberg"). The cursor
                is per-identity; another identity's cursor is unaffected.
            up_to_msg_id: optional. Empty (default) = catch up to the
                thread's current latest msg. Pass a specific msg_id to
                bookmark "I've read up to here".

        Returns:
            {"project", "identity_name", "thread_id", "last_read_msg_id",
             "updated_at", "advanced"}. `advanced=true` means the cursor
            moved and a `mark_read` audit event was emitted; `false` is
            the same-position-or-rewind no-op.
            On failure (thread not found / msg_id not in thread):
            conclair error envelope.
        """
        adapter = _adapter()
        try:
            return await adapter.mark_read(
                project=project,
                thread_id=thread_id,
                identity_name=identity_name,
                # Empty string at the MCP surface -> None on the wire so
                # the server interprets "catch up to latest". Matches the
                # pattern already used in this file for `commit_ref` etc.
                up_to_msg_id=up_to_msg_id or None,
            )
        finally:
            await adapter.close()

    @mcp.tool()
    async def chatroom_my_unread(
        project: str,
        identity_name: str,
        include_resolved: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Inbox: threads with at least one msg I have not read.

        USE THIS WHEN: starting a session, to triage what needs
        attention before doing anything else. Pair with
        `chatroom_get_thread` to read the new msgs, then
        `chatroom_mark_read` to advance the cursor once handled.

        Inbox semantics:
        - a thread the identity has never `mark_read`'d shows up with
          `last_read_msg_id=null` and `unread_count` = the whole thread
          size (handoff-safety default). Catch up with
          `chatroom_mark_read(thread_id=..., up_to_msg_id="")`.
        - results are sorted "most unread first, then by thread recency"
          so the first page is the actionable surface.
        - resolved threads are excluded by default. Pass
          `include_resolved=True` to see them (e.g. for archive review).

        Args:
            project: chatroom project to triage.
            identity_name: whose inbox (e.g. "Heisenberg"). Required --
                there is no implicit "current actor" at the MCP layer.
            include_resolved: include `status="resolved"` threads.
                Default False.
            limit: 1..1000, default 100.
            offset: pagination, default 0.

        Returns:
            {"items": [{thread_id, title, status, owner, latest_msg_id,
             last_read_msg_id, unread_count}, ...], "total", "limit",
             "offset"}.
        """
        adapter = _adapter()
        try:
            return await adapter.list_unread(
                project=project,
                identity_name=identity_name,
                include_resolved=include_resolved,
                limit=limit,
                offset=offset,
            )
        finally:
            await adapter.close()
