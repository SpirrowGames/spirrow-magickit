"""Summon adapter methods and MCP tools.

Pinned here:

1. ``set`` and ``clear`` hit different endpoints, and ``clear`` sends the
   thread_id it believes holds the mark (compare-and-clear lives in Conclair,
   but only works if the id is on the wire).
2. Blank ``thread_id`` / ``by`` never reach Conclair.
3. Conclair's 409 envelopes come back unaltered.
4. ``loop_control_get`` passes ``active_summon`` through untouched.
5. The tools live in their own module, not on ``loop_control`` — permission
   is expressed by withholding a tool.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from magickit.adapters.chatroom import ChatroomAdapter
from magickit.config import Settings
from magickit.mcp.tools import loop_control as loop_control_tools
from magickit.mcp.tools import summon as summon_tools

BASE_URL = "http://localhost:8115"

_SUMMON = {"thread_id": "T-a", "since": "2026-10-29T01:02:03.123456+00:00", "by": "takahito"}
_BODY = {"project": "p", "desired_state": "run", "configured": True, "active_summon": _SUMMON}


def _resp(status: int, body: dict) -> httpx.Response:
    return httpx.Response(status, json=body, request=httpx.Request("GET", BASE_URL))


def _patch(adapter: ChatroomAdapter, response: httpx.Response) -> AsyncMock:
    fake = MagicMock()
    fake.is_closed = False
    fake.request = AsyncMock(return_value=response)
    adapter._client = fake
    return fake.request


@pytest.fixture
def adapter() -> ChatroomAdapter:
    return ChatroomAdapter(base_url=BASE_URL, timeout=5.0)


async def test_set_summon_puts_to_the_summon_subpath(adapter: ChatroomAdapter) -> None:
    request = _patch(adapter, _resp(200, _BODY))
    await adapter.set_summon(project="p", thread_id="T-a", by="takahito", note="今これ")
    method, path = request.call_args.args
    assert (method, path) == ("PUT", "/v1/projects/p/control/summon")
    assert request.call_args.kwargs["json"] == {"thread_id": "T-a", "by": "takahito", "note": "今これ"}


async def test_clear_summon_posts_compare_and_clear(adapter: ChatroomAdapter) -> None:
    request = _patch(adapter, _resp(200, {**_BODY, "active_summon": None}))
    await adapter.clear_summon(project="p", thread_id="T-a", by="takahito")
    method, path = request.call_args.args
    assert (method, path) == ("POST", "/v1/projects/p/control/summon/clear")
    assert request.call_args.kwargs["json"] == {"thread_id": "T-a", "by": "takahito"}


@pytest.mark.parametrize("code", ["summon_conflict", "summon_mismatch", "summon_thread_not_open"])
async def test_409_envelopes_are_returned_unaltered(adapter: ChatroomAdapter, code: str) -> None:
    envelope = {"error_type": code, "error": "x", "details": {"active_summon": _SUMMON}}
    _patch(adapter, _resp(409, envelope))
    assert await adapter.set_summon(project="p", thread_id="T-b", by="h") == envelope


# ---- MCP tools ------------------------------------------------------------


def _tools(module: Any) -> dict[str, Any]:
    """Capture the tool functions by intercepting ``@mcp.tool()``.

    Same approach as test_loop_control.py: FastMCP's registry lookup API has
    shifted across 2.x minors.
    """
    registered: dict[str, Any] = {}

    def fake_tool(*args: Any, **kwargs: Any):
        def decorator(fn):
            registered[fn.__name__] = fn
            return fn

        return decorator

    mock_mcp = MagicMock()
    mock_mcp.tool = fake_tool
    module.register_tools(mock_mcp, Settings(conclair_url=BASE_URL, conclair_timeout=5.0))
    return registered


@pytest.fixture
def fake_adapter(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    fake = AsyncMock()
    fake.set_summon.return_value = _BODY
    fake.clear_summon.return_value = {**_BODY, "active_summon": None}
    fake.get_loop_control.return_value = _BODY
    monkeypatch.setattr(summon_tools, "_adapter", lambda: fake)
    monkeypatch.setattr(loop_control_tools, "_adapter", lambda: fake)
    return fake


def test_summon_tools_are_not_on_the_loop_control_module() -> None:
    names = set(_tools(loop_control_tools))
    assert "summon_set" not in names and "summon_clear" not in names
    assert {"summon_set", "summon_clear"} <= set(_tools(summon_tools))


@pytest.mark.parametrize("field", ["thread_id", "by"])
@pytest.mark.parametrize("blank", ["", " ", "\t"])
@pytest.mark.parametrize("tool", ["summon_set", "summon_clear"])
async def test_blank_fields_are_refused_before_conclair(
    fake_adapter: AsyncMock, tool: str, field: str, blank: str
) -> None:
    args = {"project": "p", "thread_id": "T-a", "by": "takahito", field: blank}
    result = await _tools(summon_tools)[tool](**args)
    assert result["error_type"] == "invalid_argument"
    assert result["details"] == {"fields": [field]}
    fake_adapter.set_summon.assert_not_called()
    fake_adapter.clear_summon.assert_not_called()


async def test_summon_set_forwards_and_omits_an_empty_note(fake_adapter: AsyncMock) -> None:
    result = await _tools(summon_tools)["summon_set"](project="p", thread_id="T-a", by="takahito")
    assert result == _BODY
    fake_adapter.set_summon.assert_awaited_once_with(
        project="p", thread_id="T-a", by="takahito", note=None
    )
    fake_adapter.close.assert_awaited()


async def test_summon_clear_forwards(fake_adapter: AsyncMock) -> None:
    await _tools(summon_tools)["summon_clear"](project="p", thread_id="T-a", by="takahito", note="済")
    fake_adapter.clear_summon.assert_awaited_once_with(
        project="p", thread_id="T-a", by="takahito", note="済"
    )


async def test_loop_control_get_passes_active_summon_through(fake_adapter: AsyncMock) -> None:
    result = await _tools(loop_control_tools)["loop_control_get"](project="p")
    assert result["active_summon"] is _SUMMON
