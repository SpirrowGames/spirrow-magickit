"""Behaviour tests for ``handoff``'s write receipt (D3d-1) and required blockers (D3d-2).

chatroom T-checkpoint-silent-partial-write msg-1188 §2 (design), msg-1190
§2/§3 (rejection marker), endorsed in Einstein's reviews.

* D3d-1: ``handoff`` transcribes the downstream ``saved_to`` exactly as
  ``checkpoint`` does since D1.  ``persisted`` is ``True`` / ``False`` /
  ``None`` for non-empty / empty / missing, ``success = persisted is True``,
  and ``"session"`` lands in ``saved_to`` only when ``persisted`` is ``True``.
* D3d-2: ``blockers`` is required with no default; ``[]`` is forwarded as a
  clear through the tool *and* the adapter; an empty / whitespace element is
  rejected with ``handoff rejected: ... Nothing was written.`` before any I/O.
* msg-1190 §2 (b): the CLAUDE.md ``-REACT`` clarification lets an agent
  fix-and-resend only on that marker.  Tests 8 and 9 pin the marker to both
  tools' messages so the doc and the code cannot drift apart.

The numbers in the test docstrings are the test numbers in msg-1188 §2
"Tests" (1-7) and msg-1190 §3 (8-9).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError

from magickit.adapters.prismind import PrismindAdapter
from magickit.config import Settings
from magickit.mcp.tools import session as session_tools

# The marker msg-1190 §2 (b) documents in CLAUDE.md.
NOTHING_WRITTEN = "Nothing was written."


def _capture_tools(settings: Settings) -> dict[str, Any]:
    registered: dict[str, Any] = {}

    def fake_tool(*args: Any, **kwargs: Any):
        def decorator(fn):
            registered[fn.__name__] = fn
            return fn

        return decorator

    mock_mcp = MagicMock()
    mock_mcp.tool = fake_tool
    session_tools.register_tools(mock_mcp, settings)
    return registered


@pytest.fixture
def settings() -> Settings:
    return Settings(prismind_url="http://localhost:8112", prismind_timeout=5.0)


@pytest.fixture
def tools(settings: Settings) -> dict[str, Any]:
    return _capture_tools(settings)


async def _handoff(
    tools: dict[str, Any], end_session_return: Any, **call: Any
) -> tuple[dict[str, Any], AsyncMock]:
    """Run ``handoff`` against a stubbed adapter; return (response, end_session mock)."""
    with patch.object(session_tools, "PrismindAdapter") as MockAdapter:
        inst = MockAdapter.return_value
        inst.end_session = AsyncMock(return_value=end_session_return)
        inst.add_knowledge = AsyncMock()
        call.setdefault("next_action", "n")
        call.setdefault("blockers", ["b"])
        call.setdefault("save_insights", False)
        result = await tools["handoff"](**call)
        return result, inst.end_session


class TestHandoffReceiptRecordsPersistence:
    """D3d-1: success is fail-closed on the downstream ``saved_to``."""

    @pytest.mark.asyncio
    async def test_1_empty_saved_to_is_not_success(self, tools):
        """1: Prismind answered success but saved nothing (msg-1188 §1)."""
        result, _ = await _handoff(
            tools, {"success": True, "saved_to": [], "message": "x"}
        )

        assert result["success"] is False
        assert result["persisted"] is False
        assert "session" not in result["saved_to"]
        assert "empty saved_to" in result["message"]

    @pytest.mark.asyncio
    async def test_2_missing_saved_to_is_persisted_none(self, tools):
        """2: no answer is not "answered no", and the message says which."""
        result, _ = await _handoff(tools, {"success": True})

        assert result["persisted"] is None
        assert result["success"] is False
        assert "session" not in result["saved_to"]
        assert "saved_to missing" in result["message"]

    @pytest.mark.asyncio
    async def test_2b_malformed_saved_to_is_persisted_none(self, tools):
        result, _ = await _handoff(tools, {"success": True, "saved_to": "yes"})

        assert result["persisted"] is None
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_3_non_empty_saved_to_is_success(self, tools):
        """3: element names are not read (R6), only non-emptiness."""
        result, _ = await _handoff(
            tools, {"success": True, "saved_to": ["some-renamed-store"]}
        )

        assert result["success"] is True
        assert result["persisted"] is True
        assert result["saved_to"] == ["session"]
        assert result["message"] == "Handoff completed successfully"

    @pytest.mark.asyncio
    async def test_end_session_exception_reports_persisted_none(self, tools):
        with patch.object(session_tools, "PrismindAdapter") as MockAdapter:
            MockAdapter.return_value.end_session = AsyncMock(
                side_effect=RuntimeError("boom")
            )
            result = await tools["handoff"](
                next_action="n", blockers=[], save_insights=False
            )

        assert result["success"] is False
        assert result["persisted"] is None
        assert "boom" in result["message"]


class TestHandoffBlockersAreForwarded:
    """D3d-2 at the tool layer."""

    @pytest.mark.asyncio
    async def test_4_empty_blockers_reach_the_adapter_as_a_clear(self, tools):
        """4: ``[]`` used to be dropped by ``if blockers:`` (msg-1188 §1)."""
        _, end_session = await _handoff(
            tools, {"saved_to": ["m"]}, blockers=[]
        )

        assert end_session.await_args.kwargs["blockers"] == []

    @pytest.mark.asyncio
    async def test_a_nonempty_blockers_list_is_forwarded(self, tools):
        _, end_session = await _handoff(
            tools, {"saved_to": ["m"]}, blockers=["B-1"]
        )

        assert end_session.await_args.kwargs["blockers"] == ["B-1"]


class TestHandoffRejectsBrokenBlockers:
    """D3d-2: rejected before any I/O, with the msg-1190 §2 (b) marker."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", [[""], ["ok", " "], ["ok", "\n\t"]])
    async def test_6_and_8_empty_element_is_rejected_before_end_session(
        self, tools, value
    ):
        """6 + 8: ToolError, marker present, nothing called."""
        with patch.object(session_tools, "PrismindAdapter") as MockAdapter:
            inst = MockAdapter.return_value
            inst.end_session = AsyncMock(return_value={"saved_to": ["m"]})
            inst.add_knowledge = AsyncMock()
            with pytest.raises(ToolError) as excinfo:
                await tools["handoff"](
                    next_action="n",
                    blockers=value,
                    project="p",
                    notes="some notes",
                )
            inst.end_session.assert_not_awaited()
            inst.add_knowledge.assert_not_awaited()

        msg = str(excinfo.value)
        assert msg.startswith("handoff rejected:")
        assert NOTHING_WRITTEN in msg
        assert "[]" in msg


class TestCheckpointRejectionsCarryTheMarker:
    """9: the existing checkpoint messages still match msg-1190 §2 (b)."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "call",
        [
            {"next_action": "", "blockers": ["b"]},
            {"next_action": "  ", "blockers": ["b"]},
            {"next_action": "n", "blockers": [""]},
        ],
    )
    async def test_9_checkpoint_rejection_marker(self, tools, call):
        with patch.object(session_tools, "PrismindAdapter") as MockAdapter:
            inst = MockAdapter.return_value
            inst.save_session = AsyncMock(return_value={"saved_to": ["m"]})
            with pytest.raises(ToolError) as excinfo:
                await tools["checkpoint"](summary="s", auto_extract=False, **call)
            inst.save_session.assert_not_awaited()

        msg = str(excinfo.value)
        assert msg.startswith("checkpoint rejected:")
        assert NOTHING_WRITTEN in msg


class TestHandoffAtTheMcpBoundary:
    """5: through a real FastMCP schema, where "required" is enforced."""

    async def _call(self, settings: Settings, arguments: dict[str, Any]) -> Any:
        from fastmcp import Client, FastMCP

        server = FastMCP("t")
        session_tools.register_tools(server, settings)
        with patch.object(session_tools, "PrismindAdapter") as MockAdapter:
            inst = MockAdapter.return_value
            inst.end_session = AsyncMock(return_value={"saved_to": ["m"]})
            async with Client(server) as client:
                result = await client.call_tool(
                    "handoff", arguments, raise_on_error=False
                )
            return result, inst.end_session

    @pytest.mark.asyncio
    async def test_5_absent_blockers_is_an_error(self, settings):
        result, end_session = await self._call(
            settings, {"next_action": "n", "save_insights": False}
        )

        assert result.is_error is True
        end_session.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_blockers_is_accepted(self, settings):
        result, end_session = await self._call(
            settings, {"next_action": "n", "blockers": [], "save_insights": False}
        )

        assert result.is_error is False
        assert end_session.await_args.kwargs["blockers"] == []
        assert result.data["persisted"] is True

    @pytest.mark.asyncio
    async def test_a_rejected_element_is_an_error_result_with_the_marker(
        self, settings
    ):
        result, end_session = await self._call(
            settings, {"next_action": "n", "blockers": [" "], "save_insights": False}
        )

        assert result.is_error is True
        end_session.assert_not_awaited()
        text = " ".join(getattr(c, "text", "") for c in result.content)
        assert "handoff rejected:" in text
        assert NOTHING_WRITTEN in text


class TestAdapterEndSessionForwardsBlockers:
    """7: the second gate, in adapters/prismind.py."""

    async def _end_session_arguments(self, **call: Any) -> dict[str, Any]:
        adapter = PrismindAdapter(sse_url="http://localhost:8112")
        adapter._call_tool_safe = AsyncMock(
            return_value=(True, {"success": True, "saved_to": ["m"]})
        )

        await adapter.end_session(**call)

        name, arguments = adapter._call_tool_safe.await_args.args
        assert name == "end_session"
        return arguments

    @pytest.mark.asyncio
    async def test_7_empty_blockers_list_is_sent(self):
        arguments = await self._end_session_arguments(next_action="n", blockers=[])

        assert arguments["blockers"] == []

    @pytest.mark.asyncio
    async def test_blockers_none_is_not_sent(self):
        arguments = await self._end_session_arguments(next_action="n", blockers=None)

        assert "blockers" not in arguments
