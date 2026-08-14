"""Unit tests for the chatroom design-decide naysayer gate.

Two layers:
  - pure assessment (`_assess_naysayer_gate`): the DoD truth table, no IO.
  - wrapper integration (`chatroom_close_thread` / `chatroom_post_message`):
    the gate actually blocks/allows the underlying adapter call, and the
    human override note reaches the persisted decide body.

Backward-compat is asserted explicitly: a thread without the gate tag closes
exactly as before.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from magickit.config import Settings
from magickit.mcp.tools import chatroom as chatroom_tools

GATE_TAG = "gate:naysayer"
NAYSAYERS = ("Einstein",)
HUMANS = chatroom_tools.HUMAN_IDENTITY_NAMES  # ("human",)


def _msg(
    msg_id: str,
    author: str,
    msg_type: str = "report",
    content: str = "",
    tags: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "msg_id": msg_id,
        "author": author,
        "type": msg_type,
        "content": content,
        "tags": tags or [],
    }


def _assess(
    *,
    tags: list[str],
    messages: list[dict[str, Any]],
    author: str = "Bohr",
    override_reason: str = "",
) -> dict[str, Any]:
    return chatroom_tools._assess_naysayer_gate(
        thread={"tags": tags},
        messages=messages,
        naysayer_identities=NAYSAYERS,
        gate_tag=GATE_TAG,
        human_identities=HUMANS,
        author=author,
        override_reason=override_reason,
    )


# ======================================================================
# Pure assessment — the DoD truth table
# ======================================================================


def test_non_gated_thread_is_allowed_unchanged() -> None:
    """non-gated thread の close → 一切不変(後方互換)."""
    out = _assess(tags=["design"], messages=[_msg("msg-001", "Bohr", "propose")])
    assert out["action"] == "allow"
    assert out["gated"] is False


def test_gated_without_review_is_blocked() -> None:
    """gated + review 無 → blocked."""
    out = _assess(
        tags=[GATE_TAG],
        messages=[
            _msg("msg-001", "Bohr", "propose"),
            _msg("msg-002", "Heisenberg", "report"),
        ],
    )
    assert out["action"] == "block"
    assert out["envelope"]["error_type"] == "NaysayerReviewRequiredError"


def test_gated_fresh_approve_is_allowed() -> None:
    """gated + fresh APPROVE → allowed."""
    out = _assess(
        tags=[GATE_TAG],
        messages=[
            _msg("msg-001", "Bohr", "propose"),
            _msg("msg-002", "Einstein", "report", tags=["verdict:approve"]),
        ],
    )
    assert out["action"] == "allow"
    assert out["gated"] is True
    assert out["review_msg_id"] == "msg-002"


def test_gated_stale_review_is_blocked() -> None:
    """gated + stale review(後続 substantive あり)→ blocked."""
    out = _assess(
        tags=[GATE_TAG],
        messages=[
            _msg("msg-001", "Bohr", "propose"),
            _msg("msg-002", "Einstein", "report", tags=["verdict:approve"]),
            # proposer posts substantive content after the review:
            _msg("msg-003", "Bohr", "answer", content="actually let's change X"),
        ],
    )
    assert out["action"] == "block"
    assert out["envelope"]["error_type"] == "NaysayerReviewStaleError"
    assert out["envelope"]["details"]["review_msg_id"] == "msg-002"
    assert out["envelope"]["details"]["stale_by_msg_id"] == "msg-003"


def test_gated_request_changes_without_override_is_blocked() -> None:
    """gated + fresh REQUEST_CHANGES + override 無 → blocked."""
    out = _assess(
        tags=[GATE_TAG],
        messages=[
            _msg("msg-001", "Bohr", "propose"),
            _msg("msg-002", "Einstein", "report", tags=["verdict:request_changes"]),
        ],
    )
    assert out["action"] == "block"
    assert out["envelope"]["error_type"] == "NaysayerChangesRequestedError"


def test_gated_request_changes_with_human_override_is_allowed() -> None:
    """gated + fresh REQUEST_CHANGES + human override → allowed (override 記録)."""
    out = _assess(
        tags=[GATE_TAG],
        messages=[
            _msg("msg-001", "Bohr", "propose"),
            _msg("msg-002", "Einstein", "report", tags=["verdict:request_changes"]),
        ],
        author="human",
        override_reason="time-boxed; ship and follow up in T31",
    )
    assert out["action"] == "override"
    assert "[naysayer-gate-override]" in out["note"]
    assert "author=human" in out["note"]
    assert "T31" in out["note"]


def test_override_by_non_human_is_rejected() -> None:
    """gated + override by non-human → rejected."""
    out = _assess(
        tags=[GATE_TAG],
        messages=[_msg("msg-001", "Bohr", "propose")],
        author="Bohr",
        override_reason="I think it's fine",
    )
    assert out["action"] == "block"
    assert out["envelope"]["error_type"] == "NaysayerOverrideForbiddenError"


# ---- verdict parsing variants ----------------------------------------


def test_verdict_via_body_fallback() -> None:
    """No verdict tag, but a `VERDICT:` body line is honored."""
    out = _assess(
        tags=[GATE_TAG],
        messages=[
            _msg("msg-001", "Bohr", "propose"),
            _msg(
                "msg-002", "Einstein", "report",
                content="Looks good overall.\n\nVERDICT: approve",
            ),
        ],
    )
    assert out["action"] == "allow"


def test_endorse_synonym_maps_to_approve() -> None:
    out = _assess(
        tags=[GATE_TAG],
        messages=[
            _msg("msg-001", "Bohr", "propose"),
            _msg("msg-002", "Einstein", "answer", tags=["verdict:endorse"]),
        ],
    )
    assert out["action"] == "allow"


def test_latest_review_wins_request_then_approve() -> None:
    """A later approving review supersedes an earlier request_changes."""
    out = _assess(
        tags=[GATE_TAG],
        messages=[
            _msg("msg-001", "Bohr", "propose"),
            _msg("msg-002", "Einstein", "report", tags=["verdict:request_changes"]),
            _msg("msg-003", "Bohr", "answer", content="fixed"),
            _msg("msg-004", "Einstein", "report", tags=["verdict:approve"]),
        ],
    )
    assert out["action"] == "allow"
    assert out["review_msg_id"] == "msg-004"


def test_naysayer_followup_does_not_make_review_stale() -> None:
    """The naysayer's own later non-verdict msg must not stale its approval."""
    out = _assess(
        tags=[GATE_TAG],
        messages=[
            _msg("msg-001", "Bohr", "propose"),
            _msg("msg-002", "Einstein", "report", tags=["verdict:approve"]),
            _msg("msg-003", "Einstein", "answer", content="one more note"),
        ],
    )
    assert out["action"] == "allow"


def test_ack_after_review_does_not_make_stale() -> None:
    """A bare ack is not substantive — it must not stale the review."""
    out = _assess(
        tags=[GATE_TAG],
        messages=[
            _msg("msg-001", "Bohr", "propose"),
            _msg("msg-002", "Einstein", "report", tags=["verdict:approve"]),
            _msg("msg-003", "Heisenberg", "ack", content="got it"),
        ],
    )
    assert out["action"] == "allow"


# ======================================================================
# Wrapper integration
# ======================================================================


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
        conclair_url="http://localhost:8115",
        conclair_timeout=5.0,
        naysayer_gate_enabled=True,
        naysayer_gate_tag=GATE_TAG,
        naysayer_identities=["Einstein"],
    )


def _adapter_with_thread(thread: dict[str, Any], messages: list[dict[str, Any]]) -> MagicMock:
    adapter = MagicMock()
    adapter.get_thread = AsyncMock(
        return_value={"thread": thread, "messages": messages, "mode": "full"}
    )
    adapter.close_thread = AsyncMock(return_value={"thread": {}, "decide_msg": {}})
    adapter.post_message = AsyncMock(
        return_value={"msg": {}, "thread_status_changed_to": "resolved"}
    )
    adapter.close = AsyncMock()
    return adapter


@pytest.fixture(autouse=True)
def closeable_identity():
    """P3 (msg-037 I-7): every close asks Prismind whether the author may close.

    These tests are about the *naysayer* gate and their authors are main-chain
    agents, so the identity stage must answer "yes" for the gate under test to
    be reached at all. Left unfaked it would attempt a real connection and
    fail closed, and every assertion here would pass or fail for the wrong
    reason.
    """
    prismind = MagicMock()
    prismind.get_identity = AsyncMock(return_value={
        "success": True, "found": True,
        "identity": {"allowed_roles": ["proposer", "implementer", "integrator"]},
        "message": "ok",
    })
    with patch.object(chatroom_tools, "_prismind_adapter", return_value=prismind):
        yield prismind


@pytest.mark.asyncio
async def test_close_thread_gated_no_review_blocks_adapter(settings: Settings) -> None:
    tools = _capture_tools(settings)
    adapter = _adapter_with_thread(
        {"tags": [GATE_TAG]}, [_msg("msg-001", "Bohr", "propose")]
    )
    with patch.object(chatroom_tools, "_adapter", return_value=adapter):
        result = await tools["chatroom_close_thread"](
            project="p", thread_id="T-1", summary_content="done",
            author="Bohr", embodiment="terminal_coding_agent",
        )
    assert result["error_type"] == "NaysayerReviewRequiredError"
    adapter.close_thread.assert_not_awaited()
    adapter.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_close_thread_gated_fresh_approve_proceeds(settings: Settings) -> None:
    tools = _capture_tools(settings)
    adapter = _adapter_with_thread(
        {"tags": [GATE_TAG]},
        [
            _msg("msg-001", "Bohr", "propose"),
            _msg("msg-002", "Einstein", "report", tags=["verdict:approve"]),
        ],
    )
    with patch.object(chatroom_tools, "_adapter", return_value=adapter):
        await tools["chatroom_close_thread"](
            project="p", thread_id="T-1", summary_content="done",
            author="Bohr", embodiment="terminal_coding_agent",
        )
    adapter.close_thread.assert_awaited_once()


@pytest.mark.asyncio
async def test_close_thread_human_override_appends_note(settings: Settings) -> None:
    tools = _capture_tools(settings)
    adapter = _adapter_with_thread(
        {"tags": [GATE_TAG]},
        [
            _msg("msg-001", "Bohr", "propose"),
            _msg("msg-002", "Einstein", "report", tags=["verdict:request_changes"]),
        ],
    )
    with patch.object(chatroom_tools, "_adapter", return_value=adapter):
        await tools["chatroom_close_thread"](
            project="p", thread_id="T-1", summary_content="Resolution: ship it",
            author="human", naysayer_override_reason="deadline; follow up later",
        )
    adapter.close_thread.assert_awaited_once()
    summary = adapter.close_thread.call_args.kwargs["summary_content"]
    assert "Resolution: ship it" in summary
    assert "[naysayer-gate-override]" in summary
    assert "deadline" in summary


@pytest.mark.asyncio
async def test_close_thread_non_gated_unchanged(settings: Settings) -> None:
    """Backward compat: a thread without the gate tag closes as before."""
    tools = _capture_tools(settings)
    adapter = _adapter_with_thread(
        {"tags": ["design"]}, [_msg("msg-001", "Bohr", "propose")]
    )
    with patch.object(chatroom_tools, "_adapter", return_value=adapter):
        await tools["chatroom_close_thread"](
            project="p", thread_id="T-1", summary_content="done",
            author="Bohr", embodiment="terminal_coding_agent",
        )
    adapter.close_thread.assert_awaited_once()


@pytest.mark.asyncio
async def test_post_message_decide_close_is_gated(settings: Settings) -> None:
    """The decide+closes_thread path is gated identically (no bypass)."""
    tools = _capture_tools(settings)
    adapter = _adapter_with_thread(
        {"tags": [GATE_TAG]}, [_msg("msg-001", "Bohr", "propose")]
    )
    with patch.object(chatroom_tools, "_adapter", return_value=adapter):
        result = await tools["chatroom_post_message"](
            project="p", thread_id="T-1", msg_type="decide", author="Bohr",
            content="closing", closes_thread="T-1",
            embodiment="terminal_coding_agent",
        )
    assert result["error_type"] == "NaysayerReviewRequiredError"
    adapter.post_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_message_non_closing_decide_not_gated(settings: Settings) -> None:
    """A decide WITHOUT closes_thread is not a close path — not gated."""
    tools = _capture_tools(settings)
    adapter = _adapter_with_thread(
        {"tags": [GATE_TAG]}, [_msg("msg-001", "Bohr", "propose")]
    )
    with patch.object(chatroom_tools, "_adapter", return_value=adapter):
        await tools["chatroom_post_message"](
            project="p", thread_id="T-1", msg_type="decide", author="Bohr",
            content="just a note", embodiment="terminal_coding_agent",
        )
    adapter.post_message.assert_awaited_once()
    adapter.get_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_gate_disabled_skips_enforcement(settings: Settings) -> None:
    """With the gate globally disabled, a gated thread closes unenforced.

    The thread IS read now, which it was not before: the PR-gate ledger
    carve-out (T-pr-gate-ledger-debt) has to see ``owner`` and ``tags`` to
    recognise a driver-opened PR-review thread, and that recognition cannot be
    conditional on the naysayer gate. What this test guards is the part that
    still matters — with the gate off, nothing about the gate is *enforced*:
    the close goes through untouched even though the thread carries the gate
    tag and has no approving review. (The default is ``enabled=True``, so on a
    real deployment the read happened either way; only a gate-disabled
    deployment pays a read it did not pay before.)
    """
    settings.naysayer_gate_enabled = False
    tools = _capture_tools(settings)
    adapter = _adapter_with_thread(
        {"tags": [GATE_TAG]}, [_msg("msg-001", "Bohr", "propose")]
    )
    with patch.object(chatroom_tools, "_adapter", return_value=adapter):
        await tools["chatroom_close_thread"](
            project="p", thread_id="T-1", summary_content="done",
            author="Bohr", embodiment="terminal_coding_agent",
        )
    adapter.close_thread.assert_awaited_once()
    # Unenforced, not merely unblocked: no override note was appended and no
    # ownership bypass was claimed.
    kwargs = adapter.close_thread.await_args.kwargs
    assert kwargs["summary_content"] == "done"
    assert kwargs["owner_override"] is False
