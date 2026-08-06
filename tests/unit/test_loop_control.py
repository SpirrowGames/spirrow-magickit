"""Unit tests for the loop control adapter methods and MCP tools.

Two things are worth pinning here rather than trusting to review:

1. `set` and `report_observed` hit *different* conclair endpoints. They
   are separate all the way down precisely so a loop given only the
   reporter cannot resume itself, and a wrapper that quietly routed both
   to the same place would look correct in every other test.

2. A failed read surfaces as a failure. Callers are contractually
   required to treat "cannot read the control state" as `hold`; if these
   wrappers invented a default on an upstream error, every caller would
   fail *open* and no test of the caller would catch it.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from magickit.adapters.chatroom import ChatroomAdapter
from magickit.config import Settings
from magickit.mcp.tools import loop_control as loop_control_tools

BASE_URL = "http://localhost:8115"

_STATE_BODY = {
    "project": "p",
    "desired_state": "hold",
    "desired_actor": "human",
    "desired_at": "2026-08-04T02:11:00Z",
    "observed_state": "run",
    "observed_actor": "mindwire-conductor",
    "observed_at": "2026-08-04T02:08:31Z",
    "configured": True,
}


def _resp(status: int, body: dict, *, url: str = BASE_URL) -> httpx.Response:
    return httpx.Response(status, json=body, request=httpx.Request("GET", url))


@pytest.fixture
def adapter() -> ChatroomAdapter:
    return ChatroomAdapter(base_url=BASE_URL, timeout=5.0)


def _patch_request(adapter: ChatroomAdapter, response: httpx.Response) -> AsyncMock:
    fake_client = MagicMock()
    fake_client.is_closed = False
    fake_client.request = AsyncMock(return_value=response)
    adapter._client = fake_client
    return fake_client.request


def _patch_raising(adapter: ChatroomAdapter, error: Exception) -> AsyncMock:
    fake_client = MagicMock()
    fake_client.is_closed = False
    fake_client.request = AsyncMock(side_effect=error)
    adapter._client = fake_client
    return fake_client.request


# ---- adapter: endpoints ---------------------------------------------------


async def test_get_loop_control_hits_control_endpoint(
    adapter: ChatroomAdapter,
) -> None:
    request = _patch_request(adapter, _resp(200, _STATE_BODY))

    result = await adapter.get_loop_control(project="p")

    assert result == _STATE_BODY
    method, path = request.call_args.args
    assert method == "GET"
    assert path == "/v1/projects/p/control"


async def test_set_loop_control_puts_desired(adapter: ChatroomAdapter) -> None:
    request = _patch_request(adapter, _resp(200, _STATE_BODY))

    await adapter.set_loop_control(
        project="p", state="hold", actor="human", note="出先で止めた"
    )

    method, path = request.call_args.args
    assert method == "PUT"
    assert path == "/v1/projects/p/control"
    assert request.call_args.kwargs["json"] == {
        "state": "hold",
        "actor": "human",
        "note": "出先で止めた",
    }


async def test_set_loop_control_omits_absent_note(adapter: ChatroomAdapter) -> None:
    request = _patch_request(adapter, _resp(200, _STATE_BODY))

    await adapter.set_loop_control(project="p", state="run", actor="human")

    assert request.call_args.kwargs["json"] == {"state": "run", "actor": "human"}


async def test_report_observed_posts_to_the_observed_subpath(
    adapter: ChatroomAdapter,
) -> None:
    """The distinct endpoint is the whole mechanism — see module docstring."""
    request = _patch_request(adapter, _resp(200, _STATE_BODY))

    await adapter.report_loop_control_observed(
        project="p", state="hold", actor="mindwire-conductor"
    )

    method, path = request.call_args.args
    assert method == "POST"
    assert path == "/v1/projects/p/control/observed"
    assert request.call_args.kwargs["json"] == {
        "state": "hold",
        "actor": "mindwire-conductor",
    }


# ---- adapter: failures stay failures --------------------------------------


async def test_upstream_5xx_returns_the_error_envelope(
    adapter: ChatroomAdapter,
) -> None:
    envelope = {"error_type": "ChatroomDBError", "error": "boom"}
    _patch_request(adapter, _resp(500, envelope))

    result = await adapter.get_loop_control(project="p")

    assert result == envelope
    # The critical half: no state was invented to stand in for the read.
    assert "desired_state" not in result


async def test_upstream_non_json_5xx_still_reports_an_error(
    adapter: ChatroomAdapter,
) -> None:
    fake_client = MagicMock()
    fake_client.is_closed = False
    fake_client.request = AsyncMock(
        return_value=httpx.Response(
            502, text="<html>bad gateway</html>", request=httpx.Request("GET", BASE_URL)
        )
    )
    adapter._client = fake_client

    result = await adapter.get_loop_control(project="p")

    assert result["error_type"] == "ConclairUpstreamError"
    assert "desired_state" not in result


async def test_unreachable_conclair_raises(adapter: ChatroomAdapter) -> None:
    """Connection failure propagates rather than resolving to a default."""
    _patch_raising(adapter, httpx.ConnectError("no route to host"))

    with pytest.raises(httpx.HTTPError):
        await adapter.get_loop_control(project="p")


# ---- MCP tools ------------------------------------------------------------


def _capture_tools(settings: Settings) -> dict[str, Any]:
    """Register the tools and capture the wrappers by name.

    Intercepts the @mcp.tool() decorator rather than reading FastMCP's
    registry, whose lookup API has shifted across 2.x minors — same
    approach as tests/unit/test_mcp_chatroom_tools.py.
    """
    registered: dict[str, Any] = {}

    def fake_tool(*args: Any, **kwargs: Any):
        def decorator(fn):
            registered[fn.__name__] = fn
            return fn

        return decorator

    mock_mcp = MagicMock()
    mock_mcp.tool = fake_tool
    loop_control_tools.register_tools(mock_mcp, settings)
    return registered


@pytest.fixture
def settings() -> Settings:
    return Settings(conclair_url=BASE_URL, conclair_timeout=5.0)


@pytest.fixture
def fake_adapter() -> MagicMock:
    fake = MagicMock()
    fake.get_loop_control = AsyncMock(return_value=_STATE_BODY)
    fake.set_loop_control = AsyncMock(return_value=_STATE_BODY)
    fake.report_loop_control_observed = AsyncMock(return_value=_STATE_BODY)
    fake.close = AsyncMock()
    return fake


def test_register_tools_registers_all_three(settings: Settings) -> None:
    tools = _capture_tools(settings)

    assert set(tools) == {
        "loop_control_get",
        "loop_control_set",
        "loop_control_report_observed",
    }


def test_setter_and_reporter_are_separate_tools(settings: Settings) -> None:
    """INV-4 at the tool boundary.

    Withholding the setter from the loop is only expressible if it is its
    own tool; a single tool with a `kind` argument could not be withheld
    without withholding the reporting the loop needs to do.
    """
    tools = _capture_tools(settings)

    assert tools["loop_control_set"] is not tools["loop_control_report_observed"]


async def test_get_tool_delegates(settings: Settings, fake_adapter: MagicMock) -> None:
    tools = _capture_tools(settings)

    with patch.object(loop_control_tools, "_adapter", return_value=fake_adapter):
        result = await tools["loop_control_get"](project="p")

    assert result == _STATE_BODY
    fake_adapter.get_loop_control.assert_awaited_once_with(project="p")
    fake_adapter.close.assert_awaited_once()


async def test_set_tool_delegates(settings: Settings, fake_adapter: MagicMock) -> None:
    tools = _capture_tools(settings)

    with patch.object(loop_control_tools, "_adapter", return_value=fake_adapter):
        await tools["loop_control_set"](
            project="p", state="hold", actor="human", note="n"
        )

    fake_adapter.set_loop_control.assert_awaited_once_with(
        project="p", state="hold", actor="human", note="n"
    )
    fake_adapter.close.assert_awaited_once()


async def test_set_tool_normalizes_empty_note_to_none(
    settings: Settings, fake_adapter: MagicMock
) -> None:
    tools = _capture_tools(settings)

    with patch.object(loop_control_tools, "_adapter", return_value=fake_adapter):
        await tools["loop_control_set"](project="p", state="run", actor="human")

    assert fake_adapter.set_loop_control.await_args.kwargs["note"] is None


async def test_report_observed_tool_delegates(
    settings: Settings, fake_adapter: MagicMock
) -> None:
    tools = _capture_tools(settings)

    with patch.object(loop_control_tools, "_adapter", return_value=fake_adapter):
        await tools["loop_control_report_observed"](
            project="p", state="hold", actor="mindwire-conductor"
        )

    fake_adapter.report_loop_control_observed.assert_awaited_once_with(
        project="p", state="hold", actor="mindwire-conductor"
    )
    fake_adapter.close.assert_awaited_once()


async def test_get_tool_forwards_error_envelope_unchanged(
    settings: Settings, fake_adapter: MagicMock
) -> None:
    """A failed read must reach the caller as a failure.

    The caller's fail-closed rule (unreadable -> hold) can only work if
    the failure is visible; a wrapper that supplied `run` here would make
    every caller fail open.
    """
    envelope = {"error_type": "ChatroomDBError", "error": "boom"}
    fake_adapter.get_loop_control = AsyncMock(return_value=envelope)
    tools = _capture_tools(settings)

    with patch.object(loop_control_tools, "_adapter", return_value=fake_adapter):
        result = await tools["loop_control_get"](project="p")

    assert result == envelope
    assert "desired_state" not in result


async def test_get_tool_propagates_transport_errors_and_still_closes(
    settings: Settings, fake_adapter: MagicMock
) -> None:
    fake_adapter.get_loop_control = AsyncMock(
        side_effect=httpx.ConnectError("no route to host")
    )
    tools = _capture_tools(settings)

    with patch.object(loop_control_tools, "_adapter", return_value=fake_adapter):
        with pytest.raises(httpx.HTTPError):
            await tools["loop_control_get"](project="p")

    fake_adapter.close.assert_awaited_once()


def test_adapter_factory_requires_settings() -> None:
    with patch.object(loop_control_tools, "_settings", None):
        with pytest.raises(RuntimeError):
            loop_control_tools._adapter()
