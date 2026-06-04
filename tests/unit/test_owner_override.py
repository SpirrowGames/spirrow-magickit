"""Unit tests for human Tier-C owner-override on chatroom close/decide.

ADR-2026-06-04-19 D-5: a human may force-close a non-owned thread. Magickit
is the decision point (sets the Conclair ``owner_override`` flag only for
human identities); the naysayer gate is independent (ownership bypass is not
a gate bypass). Covers the acceptance matrix (a)-(g) plus the
reason-required case, at the wrapper layer.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from magickit.config import Settings
from magickit.mcp.tools import chatroom as chatroom_tools

GATE_TAG = "gate:naysayer"


def _msg(msg_id: str, author: str, msg_type: str = "report",
         content: str = "", tags: list[str] | None = None) -> dict[str, Any]:
    return {"msg_id": msg_id, "author": author, "type": msg_type,
            "content": content, "tags": tags or []}


def _capture_tools(settings: Settings) -> dict[str, Any]:
    registered: dict[str, Any] = {}

    def fake_tool(*args: Any, **kwargs: Any):
        def decorator(fn):
            registered[fn.__name__] = fn
            return fn
        return decorator

    mock_mcp = MagicMock()
    mock_mcp.tool = fake_tool
    chatroom_tools.register_tools(mock_mcp, settings)
    return registered


@pytest.fixture
def settings() -> Settings:
    return Settings(
        conclair_url="http://localhost:8115", conclair_timeout=5.0,
        naysayer_gate_enabled=True, naysayer_gate_tag=GATE_TAG,
        naysayer_identities=["Einstein"],
    )


def _adapter(*, owner: str, tags: list[str], messages: list[dict[str, Any]]) -> MagicMock:
    adapter = MagicMock()
    thread = {"thread_id": "T-1", "owner": owner, "tags": tags}
    adapter.get_thread = AsyncMock(
        return_value={"thread": thread, "messages": messages, "mode": "full"}
    )
    adapter.close_thread = AsyncMock(return_value={"thread": {}, "decide_msg": {}})
    adapter.post_message = AsyncMock(
        return_value={"msg": {}, "thread_status_changed_to": "resolved"}
    )
    adapter.close = AsyncMock()
    return adapter


async def _close(tools, adapter, **kw):
    with patch.object(chatroom_tools, "_adapter", return_value=adapter):
        return await tools["chatroom_close_thread"](**kw)


# (a) human, non-owner, gated, override_reason 有 → success + reason/owner_override recorded
@pytest.mark.asyncio
async def test_a_human_gated_with_override_reason(settings: Settings) -> None:
    adapter = _adapter(owner="Bohr", tags=[GATE_TAG],
                       messages=[_msg("msg-001", "Bohr", "propose")])
    await _close(
        _capture_tools(settings), adapter,
        project="p", thread_id="T-1", summary_content="force", author="human",
        naysayer_override_reason="deadlocked; Tier C decides",
    )
    adapter.close_thread.assert_awaited_once()
    kw = adapter.close_thread.call_args.kwargs
    assert kw["owner_override"] is True
    assert kw["owner_override_reason"] == "deadlocked; Tier C decides"
    # both the gate-override note and the owner-override note are recorded
    assert "[naysayer-gate-override]" in kw["summary_content"]
    assert "[owner-override-by-human]" in kw["summary_content"]
    assert "owner=Bohr" in kw["summary_content"]


# (b) human, non-owner, gated, no override + no APPROVE → NaysayerReviewRequiredError
@pytest.mark.asyncio
async def test_b_human_gated_no_review_blocks(settings: Settings) -> None:
    adapter = _adapter(owner="Bohr", tags=[GATE_TAG],
                       messages=[_msg("msg-001", "Bohr", "propose")])
    result = await _close(
        _capture_tools(settings), adapter,
        project="p", thread_id="T-1", summary_content="force", author="human",
    )
    assert result["error_type"] == "NaysayerReviewRequiredError"
    adapter.close_thread.assert_not_awaited()  # owner bypass != gate bypass


# (c) human, non-owner, gated, no override + fresh APPROVE → success
@pytest.mark.asyncio
async def test_c_human_gated_fresh_approve(settings: Settings) -> None:
    adapter = _adapter(
        owner="Bohr", tags=[GATE_TAG],
        messages=[
            _msg("msg-001", "Bohr", "propose"),
            _msg("msg-002", "Einstein", "report", tags=["verdict:approve"]),
        ],
    )
    await _close(
        _capture_tools(settings), adapter,
        project="p", thread_id="T-1", summary_content="force", author="human",
    )
    adapter.close_thread.assert_awaited_once()
    assert adapter.close_thread.call_args.kwargs["owner_override"] is True


# (d) human, non-owner, non-gated → success + record (reason supplied)
@pytest.mark.asyncio
async def test_d_human_non_gated_with_reason(settings: Settings) -> None:
    adapter = _adapter(owner="Bohr", tags=["design"],
                       messages=[_msg("msg-001", "Bohr", "propose")])
    await _close(
        _capture_tools(settings), adapter,
        project="p", thread_id="T-1", summary_content="force", author="human",
        owner_override_reason="stale thread; closing",
    )
    adapter.close_thread.assert_awaited_once()
    kw = adapter.close_thread.call_args.kwargs
    assert kw["owner_override"] is True
    assert kw["owner_override_reason"] == "stale thread; closing"
    assert "[owner-override-by-human]" in kw["summary_content"]


# (d2) human, non-owner, non-gated, NO reason → OwnerOverrideReasonRequiredError
@pytest.mark.asyncio
async def test_d2_human_non_gated_missing_reason_blocks(settings: Settings) -> None:
    adapter = _adapter(owner="Bohr", tags=["design"],
                       messages=[_msg("msg-001", "Bohr", "propose")])
    result = await _close(
        _capture_tools(settings), adapter,
        project="p", thread_id="T-1", summary_content="force", author="human",
    )
    assert result["error_type"] == "OwnerOverrideReasonRequiredError"
    adapter.close_thread.assert_not_awaited()


# (e) non-human agent, non-owner → owner_override NOT granted (Conclair would 403)
@pytest.mark.asyncio
async def test_e_non_human_non_owner_no_override_flag(settings: Settings) -> None:
    adapter = _adapter(owner="Bohr", tags=["design"],
                       messages=[_msg("msg-001", "Bohr", "propose")])
    await _close(
        _capture_tools(settings), adapter,
        project="p", thread_id="T-1", summary_content="x", author="Heisenberg",
        embodiment="terminal_coding_agent",
    )
    # Magickit forwards owner_override=False; Conclair then enforces owner-only.
    assert adapter.close_thread.call_args.kwargs["owner_override"] is False


# (f) non-human + override_reason → NaysayerOverrideForbiddenError (unchanged #9)
@pytest.mark.asyncio
async def test_f_non_human_override_forbidden(settings: Settings) -> None:
    adapter = _adapter(owner="Bohr", tags=[GATE_TAG],
                       messages=[_msg("msg-001", "Bohr", "propose")])
    result = await _close(
        _capture_tools(settings), adapter,
        project="p", thread_id="T-1", summary_content="x", author="Heisenberg",
        embodiment="terminal_coding_agent",
        naysayer_override_reason="I think it's fine",
    )
    assert result["error_type"] == "NaysayerOverrideForbiddenError"
    adapter.close_thread.assert_not_awaited()


# (g) owner agent closes own thread → unchanged success, no override flag
@pytest.mark.asyncio
async def test_g_owner_agent_closes_own(settings: Settings) -> None:
    adapter = _adapter(owner="Bohr", tags=["design"],
                       messages=[_msg("msg-001", "Bohr", "propose")])
    await _close(
        _capture_tools(settings), adapter,
        project="p", thread_id="T-1", summary_content="done", author="Bohr",
        embodiment="terminal_coding_agent",
    )
    adapter.close_thread.assert_awaited_once()
    assert adapter.close_thread.call_args.kwargs["owner_override"] is False


# post_message(decide+closes_thread) honors owner-override identically.
@pytest.mark.asyncio
async def test_post_message_decide_human_force_close(settings: Settings) -> None:
    adapter = _adapter(owner="Bohr", tags=["design"],
                       messages=[_msg("msg-001", "Bohr", "propose")])
    with patch.object(chatroom_tools, "_adapter", return_value=adapter):
        await _capture_tools(settings)["chatroom_post_message"](
            project="p", thread_id="T-1", msg_type="decide", author="human",
            content="force via post", closes_thread="T-1",
            owner_override_reason="Tier C",
        )
    kw = adapter.post_message.call_args.kwargs
    assert kw["owner_override"] is True
    assert kw["owner_override_reason"] == "Tier C"
    assert "[owner-override-by-human]" in kw["content"]
