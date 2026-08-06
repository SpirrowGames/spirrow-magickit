"""Gated write handlers for the browser-facing chatroom UI.

Conclair's ``/ui`` ships its own POST endpoints, and they call Conclair's
own ``/v1`` in-process. That path never touches Magickit, so none of the
role / naysayer / embodiment enforcement applies to it -- the gap recorded
in CLAUDE.md as "UI 直叩きへの効力は現時点要件外 (msg-003 D-2)".

This module closes that gap by claiming the three write routes before the
proxy forwards them. A browser write now runs the *same* helpers the MCP
tools run (``_check_role_allowed``, ``_enforce_close_policies``,
``_check_can_close``) and only then reaches Conclair, via the adapter.
GETs still proxy straight through -- reads have nothing to enforce, and so
does the loop control form post, which carries no role and no msg (see
``chatroom_proxy.chatroom_loop_control``).

Keeping enforcement here rather than in Conclair preserves the service
boundary: Conclair stays an append-only log that validates nothing and
knows nothing about Magickit.

The responses are HTMX fragments styled by Conclair's own stylesheet
(``alert-error`` / ``alert-success``), so a gate rejection renders in the
same flash slot as a Conclair-side validation error.
"""

from __future__ import annotations

import html
from typing import Annotated, Any

from fastapi import APIRouter, Form, Request, Response

from magickit.adapters.chatroom import ChatroomAdapter
from magickit.mcp.tools import chatroom as chatroom_tools
from magickit.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["chatroom-ui"])


def _parse_csv(value: str) -> list[str]:
    """Split a comma-separated form field, mirroring Conclair's helper."""
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _flash(message: str, *, status_code: int = 200) -> Response:
    """Render a success flash into Conclair's alert markup."""
    return Response(
        content=f'<div class="alert alert-success">{html.escape(message)}</div>',
        status_code=status_code,
        media_type="text/html; charset=utf-8",
        headers={"HX-Trigger": "messagePosted"},
    )


def _error_flash(envelope: dict[str, Any], *, status_code: int = 200) -> Response:
    """Render an error envelope into Conclair's alert markup.

    Handles both shapes that reach here: Magickit's gate envelopes and
    Conclair's upstream ``{error_type, error, details}``. They already share
    a schema, which is why one renderer covers both.
    """
    error_type = html.escape(str(envelope.get("error_type", "Error")))
    error = html.escape(str(envelope.get("error", "")))
    body = f'<div class="alert alert-error"><strong>{error_type}</strong>: {error}'
    details = envelope.get("details")
    if details:
        body += f"<pre>{html.escape(str(details))}</pre>"
    body += "</div>"
    return Response(
        content=body,
        status_code=status_code,
        media_type="text/html; charset=utf-8",
    )


def _is_error(result: dict[str, Any]) -> bool:
    """Conclair signals failure by the presence of ``error_type``, not a flag."""
    return "error_type" in result


@router.post("/ui/projects/{project}/threads")
async def open_thread(
    request: Request,
    project: str,
    thread_id: Annotated[str, Form()],
    title: Annotated[str, Form()],
    owner: Annotated[str, Form()],
    propose_content: Annotated[str, Form()],
    tags: Annotated[str, Form()] = "",
    commit_ref: Annotated[str, Form()] = "",
    role: Annotated[str, Form()] = "",
    embodiment: Annotated[str, Form()] = "",
) -> Response:
    """Open a thread from the browser, through the role gate.

    The gate validates ``role`` against ``owner``, who authors the propose
    msg -- the same pairing ``chatroom_open_thread`` uses.
    """
    gate = await chatroom_tools._check_role_allowed(author=owner, role=role)
    if gate.error is not None:
        return _error_flash(gate.error)

    adapter = _adapter()
    try:
        result = await adapter.open_thread(
            project=project,
            thread_id=thread_id,
            title=title,
            owner=owner,
            propose_content=propose_content,
            tags=_parse_csv(tags),
            commit_ref=commit_ref or None,
            embodiment=embodiment or None,
            role=gate.role,
        )
    finally:
        await adapter.close()

    if _is_error(result):
        return _error_flash(result)
    return _flash(f"opened {thread_id}")


@router.post("/ui/projects/{project}/threads/{thread_id}/messages")
async def post_message(
    request: Request,
    project: str,
    thread_id: str,
    type: Annotated[str, Form()],
    author: Annotated[str, Form()],
    content: Annotated[str, Form()],
    reply_to: Annotated[str, Form()] = "",
    references_threads: Annotated[str, Form()] = "",
    related_tasks: Annotated[str, Form()] = "",
    closes_thread: Annotated[str, Form()] = "",
    tags: Annotated[str, Form()] = "",
    commit_ref: Annotated[str, Form()] = "",
    role: Annotated[str, Form()] = "",
    embodiment: Annotated[str, Form()] = "",
    naysayer_override_reason: Annotated[str, Form()] = "",
    owner_override_reason: Annotated[str, Form()] = "",
) -> Response:
    """Post a message from the browser, through every gate that applies.

    Order matters and mirrors ``chatroom_post_message``: embodiment first
    (cheapest, no IO), then the role gate, then -- only for a ``decide``
    that closes -- the close policies and the second-stage close check.
    A ``closes_thread`` here must not be a way around the close gates.
    """
    if _embodiment_missing(msg_type=type, author=author, embodiment=embodiment):
        return _error_flash(
            chatroom_tools._embodiment_required_error(msg_kind=type)
        )

    # A `decide` that closes must go through the close gate, which runs both
    # role stages off a single identity lookup. Calling the stage-1 helper
    # first would not just cost an extra lookup -- on an outage it answers
    # `RoleValidationUnavailableError`, whose documented remedy ("retry
    # without role") stage 2 is guaranteed to refuse. The close path owes the
    # caller the stage-2 envelope instead (msg-041 Q3).
    closes = type == "decide" and bool(closes_thread)
    gate = await (
        chatroom_tools._check_close_permitted(author=author, role=role)
        if closes
        else chatroom_tools._check_role_allowed(author=author, role=role)
    )
    if gate.error is not None:
        return _error_flash(gate.error)

    adapter = _adapter()
    try:
        body_content = content
        owner_override = False
        resolved_override_reason: str | None = None

        if closes:
            decision = await chatroom_tools._enforce_close_policies(
                adapter,
                project=project,
                thread_id=thread_id,
                author=author,
                body_content=content,
                naysayer_override_reason=naysayer_override_reason,
                owner_override_reason=owner_override_reason,
            )
            if decision["action"] == "block":
                return _error_flash(decision["envelope"])
            body_content = decision["content"]
            owner_override = decision.get("owner_override", False)
            resolved_override_reason = decision.get("owner_override_reason")

        result = await adapter.post_message(
            project=project,
            thread_id=thread_id,
            type=type,
            author=author,
            content=body_content,
            reply_to=reply_to or None,
            references_threads=_parse_csv(references_threads),
            related_tasks=_parse_csv(related_tasks),
            closes_thread=closes_thread or None,
            tags=_parse_csv(tags),
            commit_ref=commit_ref or None,
            embodiment=embodiment or None,
            role=gate.role,
            owner_override=owner_override,
            owner_override_reason=resolved_override_reason,
        )
    finally:
        await adapter.close()

    if _is_error(result):
        return _error_flash(result)

    msg = result.get("msg", {})
    text = f"posted {msg.get('msg_id', '?')} ({msg.get('type', type)})"
    if result.get("thread_status_changed_to"):
        text += f" — status → {result['thread_status_changed_to']}"
    return _flash(text)


@router.post("/ui/projects/{project}/threads/{thread_id}/close")
async def close_thread(
    request: Request,
    project: str,
    thread_id: str,
    author: Annotated[str, Form()],
    summary_content: Annotated[str, Form()],
    affects_threads: Annotated[str, Form()] = "",
    related_tasks: Annotated[str, Form()] = "",
    tags: Annotated[str, Form()] = "",
    role: Annotated[str, Form()] = "",
    embodiment: Annotated[str, Form()] = "",
    naysayer_override_reason: Annotated[str, Form()] = "",
    owner_override_reason: Annotated[str, Form()] = "",
) -> Response:
    """Close a thread from the browser, through both close gates.

    ``_check_close_permitted`` is the second stage (``closeable_roles``),
    which fails closed on an identity-lookup outage by design -- there is
    deliberately no escape hatch, so no fallback is attempted here.
    """
    if _embodiment_missing(msg_type="decide", author=author, embodiment=embodiment):
        return _error_flash(
            chatroom_tools._embodiment_required_error(msg_kind="decide")
        )

    # Both role stages on one identity lookup. Deliberately not preceded by
    # the stage-1 helper: see the note in `post_message` -- on a lookup
    # outage stage 1 would hand back an envelope whose suggested retry the
    # close path is certain to reject.
    gate = await chatroom_tools._check_close_permitted(author=author, role=role)
    if gate.error is not None:
        return _error_flash(gate.error)

    adapter = _adapter()
    try:
        decision = await chatroom_tools._enforce_close_policies(
            adapter,
            project=project,
            thread_id=thread_id,
            author=author,
            body_content=summary_content,
            naysayer_override_reason=naysayer_override_reason,
            owner_override_reason=owner_override_reason,
        )
        if decision["action"] == "block":
            return _error_flash(decision["envelope"])

        result = await adapter.close_thread(
            project=project,
            thread_id=thread_id,
            summary_content=decision["content"],
            author=author,
            affects_threads=_parse_csv(affects_threads),
            related_tasks=_parse_csv(related_tasks),
            tags=_parse_csv(tags),
            embodiment=embodiment or None,
            role=gate.role,
            owner_override=decision.get("owner_override", False),
            owner_override_reason=decision.get("owner_override_reason"),
        )
    finally:
        await adapter.close()

    if _is_error(result):
        return _error_flash(result)
    return _flash(f"closed {thread_id} — status → resolved")


def _adapter() -> ChatroomAdapter:
    """Build a chatroom adapter using the tools module's bound settings."""
    return chatroom_tools._adapter()


def _embodiment_missing(*, msg_type: str, author: str, embodiment: str) -> bool:
    """Whether this write needs an embodiment declaration and lacks one.

    Same rule as the MCP tools: mandatory on state-transitioning msg types,
    and humans are exempt (they are the above-loop approval layer, not a
    calling agent).
    """
    if author in chatroom_tools.HUMAN_IDENTITY_NAMES:
        return False
    if msg_type not in chatroom_tools.MANDATORY_EMBODIMENT_MSG_TYPES:
        return False
    return not embodiment
