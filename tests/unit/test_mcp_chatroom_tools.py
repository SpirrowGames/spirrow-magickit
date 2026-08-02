"""Unit tests for chatroom_* MCP tool wrappers.

The tools are thin delegations: each captures an adapter instance from
the module-level _adapter() factory and forwards kwargs unchanged. We
verify that:
  - register_tools wires all 7 tools onto a FastMCP
  - each tool calls the corresponding adapter method with the right
    arguments
  - empty-string defaults are normalized to None before reaching the
    adapter (per the wrapper semantics)
  - adapter.close() is called on exit (no leaked HTTP clients)
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from magickit.config import Settings
from magickit.mcp.tools import chatroom as chatroom_tools


def _capture_tools(settings: Settings) -> dict[str, Any]:
    """Register chatroom tools and capture the wrapper functions by name.

    Intercepts the @mcp.tool() decorator with a mock rather than touching
    FastMCP's tool registry. FastMCP's tool-lookup API has shifted across
    2.x minor versions (e.g. get_tools/get_tool), so depending on it makes
    the tests version-fragile; see tests/unit/test_smart_read.py for the
    same approach.
    """
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
    )


@pytest.fixture
def fake_adapter() -> MagicMock:
    adapter = MagicMock()
    adapter.open_thread = AsyncMock(return_value={"thread": {}, "msg": {}})
    adapter.post_message = AsyncMock(return_value={"msg": {}, "thread_status_changed_to": None})
    adapter.close_thread = AsyncMock(return_value={"thread": {}, "decide_msg": {}})
    adapter.list_threads = AsyncMock(return_value={"items": [], "total": 0})
    adapter.get_thread = AsyncMock(return_value={"thread": {}, "messages": [], "mode": "full"})
    adapter.list_events = AsyncMock(return_value={"items": [], "total": 0})
    adapter.check_integrity = AsyncMock(
        return_value={"issues": [], "issue_count": 0, "checked_at": "x"}
    )
    adapter.mark_read = AsyncMock(
        return_value={
            "project": "p", "identity_name": "Bohr", "thread_id": "T-1",
            "last_read_msg_id": "msg-001", "updated_at": "2026-05-01T00:00:00Z",
            "advanced": True,
        }
    )
    adapter.list_unread = AsyncMock(
        return_value={"items": [], "total": 0, "limit": 100, "offset": 0}
    )
    adapter.close = AsyncMock()
    return adapter


@pytest.fixture
def fake_prismind() -> MagicMock:
    """Identity lookup, answering "no such identity" for everyone.

    P3 (msg-037 I-9): the close path consults Prismind to decide whether the
    author may close at all, and unregistered authors are exempt. The
    delegation tests here post as ``alice`` -- a legacy actor with no identity
    record -- so this is the accurate answer, not a convenience: it is the
    branch that keeps such callers working.
    """
    adapter = MagicMock()
    adapter.get_identity = AsyncMock(
        return_value={"success": True, "found": False, "identity": None,
                      "message": "not found"}
    )
    return adapter


@pytest.fixture
def registered(settings: Settings, fake_adapter: MagicMock, fake_prismind: MagicMock):
    """Capture the tool fns, with _adapter() patched to the fake."""
    tools = _capture_tools(settings)

    with (
        patch.object(chatroom_tools, "_adapter", return_value=fake_adapter),
        patch.object(chatroom_tools, "_prismind_adapter", return_value=fake_prismind),
    ):
        yield tools, fake_adapter


# ---- registration -----------------------------------------------------


def test_register_tools_attaches_nine_tools(
    settings: Settings,
) -> None:
    tools = _capture_tools(settings)
    expected = [
        "chatroom_open_thread",
        "chatroom_post_message",
        "chatroom_close_thread",
        "chatroom_list_threads",
        "chatroom_get_thread",
        "chatroom_list_events",
        "chatroom_check_integrity",
        "chatroom_mark_read",
        "chatroom_my_unread",
    ]
    assert all(name in tools for name in expected), (sorted(tools), expected)


# ---- open_thread ------------------------------------------------------


@pytest.mark.asyncio
async def test_open_thread_delegates(registered) -> None:
    tools, adapter = registered
    fn = tools["chatroom_open_thread"]
    await fn(
        project="p", thread_id="T-1", title="t",
        owner="alice", propose_content="hi", tags=["x"], commit_ref="abc",
    )
    # ADR-12: open_thread now also forwards embodiment (None when omitted).
    # P2: ...and role, likewise None when omitted (the legacy default --
    # see tests/unit/test_role_gate.py for the gate's own behavior).
    adapter.open_thread.assert_awaited_once_with(
        project="p", thread_id="T-1", title="t",
        owner="alice", propose_content="hi", tags=["x"], commit_ref="abc",
        embodiment=None, role=None,
    )
    adapter.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_open_thread_empty_commit_ref_normalized_to_none(registered) -> None:
    tools, adapter = registered
    fn = tools["chatroom_open_thread"]
    await fn(
        project="p", thread_id="T-1", title="t",
        owner="alice", propose_content="hi", commit_ref="",
    )
    kwargs = adapter.open_thread.call_args.kwargs
    assert kwargs["commit_ref"] is None


@pytest.mark.asyncio
async def test_open_thread_forwards_embodiment_when_supplied(registered) -> None:
    tools, adapter = registered
    fn = tools["chatroom_open_thread"]
    await fn(
        project="p", thread_id="T-1", title="t",
        owner="alice", propose_content="hi",
        embodiment="terminal_coding_agent",
    )
    assert adapter.open_thread.call_args.kwargs["embodiment"] == "terminal_coding_agent"


# ---- post_message -----------------------------------------------------


@pytest.mark.asyncio
async def test_post_message_passes_all_args(registered) -> None:
    tools, adapter = registered
    fn = tools["chatroom_post_message"]
    # MCP-side parameter is `msg_type` (renamed from `type` to avoid
    # JSON Schema reserved-keyword collisions in some MCP clients).
    # Adapter-side still receives kwarg `type=`.
    # ADR-12: handoff is in the mandatory-embodiment set so the test must
    # supply one (or the wrapper would short-circuit with
    # EmbodimentRequiredError before reaching the adapter).
    await fn(
        project="p", thread_id="T-1", msg_type="handoff", author="alice",
        content="go", reply_to="msg-001", references_threads=["T-x"],
        related_tasks=["TSK"], closes_thread="", tags=["t"], commit_ref="abc",
        embodiment="terminal_coding_agent",
    )
    kwargs = adapter.post_message.call_args.kwargs
    assert kwargs["type"] == "handoff"
    assert kwargs["reply_to"] == "msg-001"
    assert kwargs["closes_thread"] is None  # empty -> None
    assert kwargs["references_threads"] == ["T-x"]
    assert kwargs["related_tasks"] == ["TSK"]
    assert kwargs["embodiment"] == "terminal_coding_agent"
    adapter.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_post_message_handoff_without_embodiment_rejected(registered) -> None:
    """ADR-12 §4 mandatory set: handoff/ack/decide need an embodiment
    declaration (human exempt). Reject at Magickit layer with an
    error_type envelope; no adapter call."""
    tools, adapter = registered
    fn = tools["chatroom_post_message"]
    result = await fn(
        project="p", thread_id="T-1", msg_type="handoff", author="alice",
        content="go",
    )
    assert result["error_type"] == "EmbodimentRequiredError"
    assert "handoff" in result["error"]
    assert result["details"]["msg_kind"] == "msg_type=handoff"
    adapter.post_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_message_handoff_by_human_exempt(registered) -> None:
    """Author=='human' bypasses the mandatory embodiment check."""
    tools, adapter = registered
    fn = tools["chatroom_post_message"]
    await fn(
        project="p", thread_id="T-1", msg_type="handoff", author="human",
        content="override",
    )
    adapter.post_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_post_message_report_without_embodiment_allowed(registered) -> None:
    """report is outside the mandatory set; embodiment is optional."""
    tools, adapter = registered
    fn = tools["chatroom_post_message"]
    await fn(
        project="p", thread_id="T-1", msg_type="report", author="alice",
        content="status",
    )
    adapter.post_message.assert_awaited_once()


# ---- close_thread -----------------------------------------------------


@pytest.mark.asyncio
async def test_close_thread_delegates(registered) -> None:
    tools, adapter = registered
    fn = tools["chatroom_close_thread"]
    # close_thread emits a decide internally so embodiment is mandatory
    # for non-human authors (per ADR-12).
    await fn(
        project="p", thread_id="T-1", summary_content="done", author="alice",
        affects_threads=["T-y"], tags=["resolved"],
        embodiment="terminal_coding_agent",
    )
    kwargs = adapter.close_thread.call_args.kwargs
    assert kwargs["summary_content"] == "done"
    assert kwargs["affects_threads"] == ["T-y"]
    assert kwargs["embodiment"] == "terminal_coding_agent"
    adapter.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_close_thread_without_embodiment_rejected(registered) -> None:
    """close_thread emits a decide; non-human authors must declare embodiment."""
    tools, adapter = registered
    fn = tools["chatroom_close_thread"]
    result = await fn(
        project="p", thread_id="T-1", summary_content="done", author="alice",
    )
    assert result["error_type"] == "EmbodimentRequiredError"
    assert "close_thread" in result["error"]
    adapter.close_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_close_thread_human_author_exempt(registered) -> None:
    """Author=='human' may close without declaring embodiment (rare path)."""
    tools, adapter = registered
    fn = tools["chatroom_close_thread"]
    await fn(
        project="p", thread_id="T-1", summary_content="done", author="human",
    )
    adapter.close_thread.assert_awaited_once()


# ---- list_threads -----------------------------------------------------


@pytest.mark.asyncio
async def test_list_threads_owner_normalization(registered) -> None:
    tools, adapter = registered
    fn = tools["chatroom_list_threads"]
    await fn(project="p", owner="", limit=50, offset=10)
    kwargs = adapter.list_threads.call_args.kwargs
    assert kwargs["owner"] is None
    assert kwargs["limit"] == 50
    assert kwargs["offset"] == 10


# ---- get_thread -------------------------------------------------------


@pytest.mark.asyncio
async def test_get_thread_summary_mode(registered) -> None:
    tools, adapter = registered
    fn = tools["chatroom_get_thread"]
    await fn(project="p", thread_id="T-1", mode="summary")
    adapter.get_thread.assert_awaited_once_with(
        project="p", thread_id="T-1", mode="summary"
    )


# ---- list_events ------------------------------------------------------


@pytest.mark.asyncio
async def test_list_events_filter_normalization(registered) -> None:
    tools, adapter = registered
    fn = tools["chatroom_list_events"]
    await fn(
        project="p", thread_id="", action="status_transition",
        since="2026-05-01T00:00:00Z", until="", limit=10,
    )
    kwargs = adapter.list_events.call_args.kwargs
    assert kwargs["thread_id"] is None
    assert kwargs["action"] == "status_transition"
    assert kwargs["since"] == "2026-05-01T00:00:00Z"
    assert kwargs["until"] is None
    assert kwargs["limit"] == 10


# ---- check_integrity --------------------------------------------------


@pytest.mark.asyncio
async def test_check_integrity_delegates(registered) -> None:
    tools, adapter = registered
    fn = tools["chatroom_check_integrity"]
    out = await fn(project="p")
    adapter.check_integrity.assert_awaited_once_with(project="p")
    assert out["issue_count"] == 0
    adapter.close.assert_awaited_once()


# ---- error envelope passthrough ---------------------------------------


@pytest.mark.asyncio
async def test_adapter_error_envelope_passes_through(registered) -> None:
    """When adapter returns an error envelope, MCP tool returns it unchanged."""
    tools, adapter = registered
    adapter.open_thread = AsyncMock(
        return_value={
            "error_type": "ChatroomIntegrityError",
            "error": "Thread already exists",
            "details": {"thread_id": "T-1"},
        }
    )
    fn = tools["chatroom_open_thread"]
    out = await fn(
        project="p", thread_id="T-1", title="t",
        owner="alice", propose_content="hi",
    )
    assert out["error_type"] == "ChatroomIntegrityError"


# ---- adapter close even when adapter raises ---------------------------


@pytest.mark.asyncio
async def test_adapter_close_called_when_adapter_raises(registered) -> None:
    tools, adapter = registered
    adapter.open_thread = AsyncMock(side_effect=RuntimeError("boom"))
    fn = tools["chatroom_open_thread"]
    with pytest.raises(RuntimeError):
        await fn(
            project="p", thread_id="T-1", title="t",
            owner="alice", propose_content="hi",
        )
    adapter.close.assert_awaited_once()


# ---- mark_read --------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_read_forwards_explicit_msg_id(registered) -> None:
    tools, adapter = registered
    fn = tools["chatroom_mark_read"]
    await fn(
        project="p", thread_id="T-1",
        identity_name="Bohr", up_to_msg_id="msg-005",
    )
    adapter.mark_read.assert_awaited_once_with(
        project="p", thread_id="T-1",
        identity_name="Bohr", up_to_msg_id="msg-005",
    )
    adapter.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_mark_read_empty_msg_id_normalized_to_none(registered) -> None:
    """Empty string at the MCP surface (default) is the ergonomic
    "catch up to latest" signal. The wrapper converts it to None before
    calling the adapter so the JSON body stays minimal."""
    tools, adapter = registered
    fn = tools["chatroom_mark_read"]
    await fn(
        project="p", thread_id="T-1", identity_name="Bohr",
    )
    kwargs = adapter.mark_read.call_args.kwargs
    assert kwargs["up_to_msg_id"] is None


@pytest.mark.asyncio
async def test_mark_read_propagates_error_envelope(registered) -> None:
    tools, adapter = registered
    err = {
        "error_type": "ChatroomNotFoundError",
        "error": "Thread 'T-x' not found in project 'p'",
        "details": {"project": "p", "thread_id": "T-x"},
    }
    adapter.mark_read = AsyncMock(return_value=err)
    fn = tools["chatroom_mark_read"]
    out = await fn(project="p", thread_id="T-x", identity_name="Bohr")
    assert out == err


# ---- my_unread --------------------------------------------------------


@pytest.mark.asyncio
async def test_my_unread_forwards_filters(registered) -> None:
    tools, adapter = registered
    fn = tools["chatroom_my_unread"]
    await fn(
        project="p", identity_name="Heisenberg",
        include_resolved=True, limit=50, offset=10,
    )
    adapter.list_unread.assert_awaited_once_with(
        project="p", identity_name="Heisenberg",
        include_resolved=True, limit=50, offset=10,
    )


@pytest.mark.asyncio
async def test_my_unread_defaults(registered) -> None:
    """The MVP defaults must match the docstring: exclude resolved by
    default, limit 100, offset 0. Pin them so a future docstring drift
    surfaces in CI."""
    tools, adapter = registered
    fn = tools["chatroom_my_unread"]
    await fn(project="p", identity_name="Heisenberg")
    kwargs = adapter.list_unread.call_args.kwargs
    assert kwargs["include_resolved"] is False
    assert kwargs["limit"] == 100
    assert kwargs["offset"] == 0
