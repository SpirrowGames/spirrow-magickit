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
    adapter.close = AsyncMock()
    return adapter


@pytest.fixture
def registered(settings: Settings, fake_adapter: MagicMock):
    """Capture the tool fns, with _adapter() patched to the fake."""
    tools = _capture_tools(settings)

    with patch.object(chatroom_tools, "_adapter", return_value=fake_adapter):
        yield tools, fake_adapter


# ---- registration -----------------------------------------------------


def test_register_tools_attaches_seven_tools(
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
    adapter.open_thread.assert_awaited_once_with(
        project="p", thread_id="T-1", title="t",
        owner="alice", propose_content="hi", tags=["x"], commit_ref="abc",
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


# ---- post_message -----------------------------------------------------


@pytest.mark.asyncio
async def test_post_message_passes_all_args(registered) -> None:
    tools, adapter = registered
    fn = tools["chatroom_post_message"]
    # MCP-side parameter is `msg_type` (renamed from `type` to avoid
    # JSON Schema reserved-keyword collisions in some MCP clients).
    # Adapter-side still receives kwarg `type=`.
    await fn(
        project="p", thread_id="T-1", msg_type="handoff", author="alice",
        content="go", reply_to="msg-001", references_threads=["T-x"],
        related_tasks=["TSK"], closes_thread="", tags=["t"], commit_ref="abc",
    )
    kwargs = adapter.post_message.call_args.kwargs
    assert kwargs["type"] == "handoff"
    assert kwargs["reply_to"] == "msg-001"
    assert kwargs["closes_thread"] is None  # empty -> None
    assert kwargs["references_threads"] == ["T-x"]
    assert kwargs["related_tasks"] == ["TSK"]
    adapter.close.assert_awaited_once()


# ---- close_thread -----------------------------------------------------


@pytest.mark.asyncio
async def test_close_thread_delegates(registered) -> None:
    tools, adapter = registered
    fn = tools["chatroom_close_thread"]
    await fn(
        project="p", thread_id="T-1", summary_content="done", author="alice",
        affects_threads=["T-y"], tags=["resolved"],
    )
    kwargs = adapter.close_thread.call_args.kwargs
    assert kwargs["summary_content"] == "done"
    assert kwargs["affects_threads"] == ["T-y"]
    adapter.close.assert_awaited_once()


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
