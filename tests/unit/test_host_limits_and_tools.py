"""Rate limits of the unauthenticated door, and the tool registration."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from magickit.config import Settings
from magickit.hosts import probes
from magickit.hosts.limits import RateLimiter, Window
from magickit.mcp.tools import host as host_tools


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_global_window():
    clock = Clock()
    lim = RateLimiter(global_windows=(Window(2, 60),), probe_windows={}, clock=clock)
    assert lim.admit("a") == 0 and lim.admit("b") == 0
    wait = lim.admit("c")
    assert 59 <= wait <= 60
    clock.t += 61
    assert lim.admit("c") == 0


def test_per_probe_window_on_top_of_global():
    clock = Clock()
    lim = RateLimiter(
        global_windows=(Window(100, 60),),
        probe_windows={"journal_summary": (Window(1, 60),)},
        clock=clock,
    )
    assert lim.admit("journal_summary") == 0
    assert lim.admit("journal_summary") > 0
    assert lim.admit("unit_state") == 0  # others are not held up


def test_a_refused_call_is_not_counted():
    clock = Clock()
    lim = RateLimiter(global_windows=(Window(1, 60),), probe_windows={}, clock=clock)
    assert lim.admit("a") == 0
    for _ in range(10):
        assert lim.admit("a") > 0
    clock.t += 61
    assert lim.admit("a") == 0


def test_hour_window():
    clock = Clock()
    lim = RateLimiter(
        global_windows=(Window(100, 60), Window(3, 3600)), probe_windows={}, clock=clock
    )
    for _ in range(3):
        assert lim.admit("a") == 0
        clock.t += 61
    assert lim.admit("a") > 3000


def _capture() -> dict[str, Any]:
    registered: dict[str, Any] = {}

    def fake_tool(*args: Any, **kwargs: Any):
        def decorator(fn):
            registered[fn.__name__] = fn
            return fn

        return decorator

    mcp = MagicMock()
    mcp.tool = fake_tool
    host_tools.register_tools(mcp, Settings())
    return registered


def test_step_one_registers_exactly_two_read_only_tools():
    # Step 1 of the design: no request / approve / apply tool exists yet.
    assert set(_capture()) == {"host_task_targets", "host_probe"}


async def test_host_probe_tool_delegates_to_run_probe(monkeypatch):
    seen: dict[str, Any] = {}

    async def fake_run(host, probe, target, params, *, limiter):
        seen.update(host=host, probe=probe, target=target, params=params, limiter=limiter)
        return {"ok": True}

    monkeypatch.setattr(probes, "run_probe", fake_run)
    tools = _capture()
    assert await tools["host_probe"]("services", "unit_state", "spirrow-lexora", None) == {
        "ok": True
    }
    assert seen["host"] == "services" and isinstance(seen["limiter"], RateLimiter)


async def test_host_task_targets_tool():
    out = await _capture()["host_task_targets"]()
    assert out["ok"] and out["hosts"][0]["name"] == "services"


def test_mcp_server_registers_the_host_tools():
    # Both instances: registration is not conditional on auth. Read from the
    # source rather than imported -- importing mcp_server builds the server
    # (same approach as test_mcp_deploy_tools).
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "src" / "magickit" / "mcp_server.py").read_text(
        encoding="utf-8"
    )
    assert "    host.register_tools(mcp, settings)\n" in src
