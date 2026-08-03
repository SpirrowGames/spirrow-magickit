"""Unit tests for the gated browser write path.

The point of these routes is that a human writing from the browser is
subject to the same enforcement as an AI session writing over MCP. So the
assertions are mostly of the form "the gate said no, and Conclair was
never contacted" -- reaching the adapter at all is the failure.

The one subtlety worth pinning is envelope selection on a closing write:
``_check_close_permitted`` runs both role stages off a single lookup, and
the close path must answer an outage with the stage-2 envelope. Calling
the stage-1 helper first would produce ``RoleValidationUnavailableError``,
whose documented remedy ("retry without role") stage 2 always refuses.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from magickit.config import Settings
from magickit.main import create_app
from magickit.mcp.tools import chatroom as chatroom_tools

PROJECT = "spirrow-magickit"
THREAD = "T-x"


@pytest.fixture(autouse=True)
def _configured():
    """Gates read module-level settings; the app normally binds them at startup."""
    chatroom_tools.configure(Settings())
    yield
    chatroom_tools._settings = None


async def _post(path: str, data: dict[str, str]) -> httpx.Response:
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        return await client.post(path, data=data)


def _blocking_gate(error: dict[str, Any]):
    return AsyncMock(return_value=chatroom_tools._RoleDecision(error=error, role=None))


def _passing_gate(role: str | None = None):
    return AsyncMock(return_value=chatroom_tools._RoleDecision(error=None, role=role))


# --- the gate actually blocks -------------------------------------------


@pytest.mark.asyncio
async def test_role_gate_blocks_post_and_never_reaches_conclair():
    adapter = AsyncMock()
    envelope = {"error_type": "RoleNotAllowed", "error": "naysayer may not propose"}

    with (
        patch.object(chatroom_tools, "_check_role_allowed", _blocking_gate(envelope)),
        patch.object(chatroom_tools, "_adapter", return_value=adapter),
    ):
        response = await _post(
            f"/ui/projects/{PROJECT}/threads/{THREAD}/messages",
            {"type": "question", "author": "Einstein", "content": "hi", "role": "proposer"},
        )

    assert "RoleNotAllowed" in response.text
    adapter.post_message.assert_not_called()


@pytest.mark.asyncio
async def test_role_gate_blocks_open_thread():
    adapter = AsyncMock()
    envelope = {"error_type": "RoleNotAllowed", "error": "nope"}

    with (
        patch.object(chatroom_tools, "_check_role_allowed", _blocking_gate(envelope)),
        patch.object(chatroom_tools, "_adapter", return_value=adapter),
    ):
        response = await _post(
            f"/ui/projects/{PROJECT}/threads",
            {
                "thread_id": THREAD,
                "title": "t",
                "owner": "Einstein",
                "propose_content": "c",
                "role": "proposer",
            },
        )

    assert "RoleNotAllowed" in response.text
    adapter.open_thread.assert_not_called()


@pytest.mark.asyncio
async def test_close_gate_blocks_and_never_reaches_conclair():
    adapter = AsyncMock()
    envelope = {"error_type": "RoleNotAllowedToClose", "error": "naysayer cannot close"}

    with (
        patch.object(chatroom_tools, "_check_close_permitted", _blocking_gate(envelope)),
        patch.object(chatroom_tools, "_adapter", return_value=adapter),
    ):
        response = await _post(
            f"/ui/projects/{PROJECT}/threads/{THREAD}/close",
            {
                "author": "Einstein",
                "summary_content": "done",
                # A close emits a decide, so a non-human author must declare
                # one or the embodiment check answers first.
                "embodiment": "web_ai_chat",
            },
        )

    assert "RoleNotAllowedToClose" in response.text
    adapter.close_thread.assert_not_called()


@pytest.mark.asyncio
async def test_close_requires_embodiment_before_the_role_gate():
    """The cheap local check answers before any identity lookup happens."""
    adapter = AsyncMock()
    close_gate = _passing_gate()

    with (
        patch.object(chatroom_tools, "_check_close_permitted", close_gate),
        patch.object(chatroom_tools, "_adapter", return_value=adapter),
    ):
        response = await _post(
            f"/ui/projects/{PROJECT}/threads/{THREAD}/close",
            {"author": "Einstein", "summary_content": "done"},
        )

    assert "EmbodimentRequiredError" in response.text
    close_gate.assert_not_awaited()
    adapter.close_thread.assert_not_called()


# --- closes_thread is not a way around the close gate -------------------


@pytest.mark.asyncio
async def test_closing_decide_uses_the_close_gate_not_stage_one():
    """`closes_thread` on a decide must not bypass the second stage."""
    adapter = AsyncMock()
    envelope = {"error_type": "RoleNotAllowedToClose", "error": "no"}
    stage_one = _passing_gate()
    close_gate = _blocking_gate(envelope)

    with (
        patch.object(chatroom_tools, "_check_role_allowed", stage_one),
        patch.object(chatroom_tools, "_check_close_permitted", close_gate),
        patch.object(chatroom_tools, "_adapter", return_value=adapter),
    ):
        response = await _post(
            f"/ui/projects/{PROJECT}/threads/{THREAD}/messages",
            {
                "type": "decide",
                "author": "Einstein",
                "content": "decided",
                "closes_thread": THREAD,
                "embodiment": "web_ai_chat",
            },
        )

    assert "RoleNotAllowedToClose" in response.text
    close_gate.assert_awaited_once()
    # Stage 1 must not run separately -- the close gate already ran it.
    stage_one.assert_not_awaited()
    adapter.post_message.assert_not_called()


@pytest.mark.asyncio
async def test_non_closing_message_uses_stage_one_only():
    adapter = AsyncMock()
    adapter.post_message.return_value = {"msg": {"msg_id": "msg-9", "type": "question"}}
    stage_one = _passing_gate()
    close_gate = _passing_gate()

    with (
        patch.object(chatroom_tools, "_check_role_allowed", stage_one),
        patch.object(chatroom_tools, "_check_close_permitted", close_gate),
        patch.object(chatroom_tools, "_adapter", return_value=adapter),
    ):
        await _post(
            f"/ui/projects/{PROJECT}/threads/{THREAD}/messages",
            {"type": "question", "author": "human", "content": "hi"},
        )

    stage_one.assert_awaited_once()
    close_gate.assert_not_awaited()


# --- embodiment ---------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("msg_type", ["handoff", "ack", "decide"])
async def test_embodiment_required_for_state_transitions(msg_type: str):
    adapter = AsyncMock()

    with patch.object(chatroom_tools, "_adapter", return_value=adapter):
        response = await _post(
            f"/ui/projects/{PROJECT}/threads/{THREAD}/messages",
            {"type": msg_type, "author": "Bohr", "content": "c"},
        )

    assert "EmbodimentRequiredError" in response.text
    adapter.post_message.assert_not_called()


@pytest.mark.asyncio
async def test_human_is_exempt_from_embodiment():
    adapter = AsyncMock()
    adapter.post_message.return_value = {"msg": {"msg_id": "msg-1", "type": "handoff"}}

    with (
        patch.object(chatroom_tools, "_check_role_allowed", _passing_gate()),
        patch.object(chatroom_tools, "_adapter", return_value=adapter),
    ):
        response = await _post(
            f"/ui/projects/{PROJECT}/threads/{THREAD}/messages",
            {"type": "handoff", "author": "human", "content": "over to you"},
        )

    assert "EmbodimentRequiredError" not in response.text
    adapter.post_message.assert_called_once()


# --- the validated role is what gets recorded ---------------------------


@pytest.mark.asyncio
async def test_only_the_gate_approved_role_is_recorded():
    """The caller's raw `role` must never reach Conclair unvalidated."""
    adapter = AsyncMock()
    adapter.post_message.return_value = {"msg": {"msg_id": "msg-2", "type": "question"}}

    with (
        patch.object(chatroom_tools, "_check_role_allowed", _passing_gate(role=None)),
        patch.object(chatroom_tools, "_adapter", return_value=adapter),
    ):
        await _post(
            f"/ui/projects/{PROJECT}/threads/{THREAD}/messages",
            {
                "type": "question",
                "author": "unregistered",
                "content": "hi",
                "role": "implementer",
            },
        )

    assert adapter.post_message.call_args.kwargs["role"] is None


# --- naysayer gate ------------------------------------------------------


@pytest.mark.asyncio
async def test_naysayer_gate_blocks_the_close():
    adapter = AsyncMock()
    envelope = {"error_type": "NaysayerReviewRequired", "error": "no fresh approval"}

    with (
        patch.object(chatroom_tools, "_check_close_permitted", _passing_gate()),
        patch.object(
            chatroom_tools,
            "_enforce_close_policies",
            AsyncMock(return_value={"action": "block", "envelope": envelope}),
        ),
        patch.object(chatroom_tools, "_adapter", return_value=adapter),
    ):
        response = await _post(
            f"/ui/projects/{PROJECT}/threads/{THREAD}/close",
            {"author": "human", "summary_content": "done"},
        )

    assert "NaysayerReviewRequired" in response.text
    adapter.close_thread.assert_not_called()


@pytest.mark.asyncio
async def test_owner_override_reaches_conclair_on_a_human_close():
    """ADR-2026-06-04-19 D-5: the Tier-C force-close must survive this path."""
    adapter = AsyncMock()
    adapter.close_thread.return_value = {"thread": {"status": "resolved"}}

    with (
        patch.object(chatroom_tools, "_check_close_permitted", _passing_gate()),
        patch.object(
            chatroom_tools,
            "_enforce_close_policies",
            AsyncMock(
                return_value={
                    "action": "proceed",
                    "content": "done",
                    "owner_override": True,
                    "owner_override_reason": "above-loop call",
                }
            ),
        ),
        patch.object(chatroom_tools, "_adapter", return_value=adapter),
    ):
        await _post(
            f"/ui/projects/{PROJECT}/threads/{THREAD}/close",
            {
                "author": "human",
                "summary_content": "done",
                "owner_override_reason": "above-loop call",
            },
        )

    kwargs = adapter.close_thread.call_args.kwargs
    assert kwargs["owner_override"] is True
    assert kwargs["owner_override_reason"] == "above-loop call"


# --- upstream errors ----------------------------------------------------


@pytest.mark.asyncio
async def test_conclair_error_envelope_is_rendered_not_swallowed():
    adapter = AsyncMock()
    adapter.post_message.return_value = {
        "error_type": "ChatroomStateError",
        "error": "thread is resolved",
    }

    with (
        patch.object(chatroom_tools, "_check_role_allowed", _passing_gate()),
        patch.object(chatroom_tools, "_adapter", return_value=adapter),
    ):
        response = await _post(
            f"/ui/projects/{PROJECT}/threads/{THREAD}/messages",
            {"type": "question", "author": "human", "content": "hi"},
        )

    assert "ChatroomStateError" in response.text
    assert "thread is resolved" in response.text


# --- routing ------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,form",
    [
        (
            f"/ui/projects/{PROJECT}/threads",
            {"thread_id": THREAD, "title": "t", "owner": "a", "propose_content": "c"},
        ),
        (
            f"/ui/projects/{PROJECT}/threads/{THREAD}/messages",
            {"type": "question", "author": "a", "content": "c"},
        ),
        (
            f"/ui/projects/{PROJECT}/threads/{THREAD}/close",
            {"author": "human", "summary_content": "s"},
        ),
    ],
)
async def test_every_write_route_is_claimed_by_the_gated_handler(
    path: str, form: dict[str, str]
):
    """A POST must be handled here, not forwarded to Conclair's ungated one.

    Proven by making the gate refuse and seeing its envelope come back: only
    Magickit's handler consults the gate, so the envelope is evidence the
    request never reached the proxy. Asserted this way rather than by reading
    the route table, whose shape varies across Starlette versions.
    """
    envelope = {"error_type": "RoleNotAllowed", "error": "sentinel"}

    with (
        patch.object(chatroom_tools, "_check_role_allowed", _blocking_gate(envelope)),
        patch.object(chatroom_tools, "_check_close_permitted", _blocking_gate(envelope)),
        patch.object(chatroom_tools, "_adapter", return_value=AsyncMock()),
    ):
        response = await _post(path, form)

    assert response.status_code == 200
    assert "sentinel" in response.text
