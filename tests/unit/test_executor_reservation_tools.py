"""ADR-2026-10-09-23 D13 (unit 4): reservation MCP tools.

The tools call the pool store directly. Each test shares one store (temp
SQLite file, fake clock) between the MCP tools and the HTTP app, so a
reservation made over MCP is seen by ``POST /turn-requests`` and the other
way round.
"""

from __future__ import annotations

import inspect
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from magickit import main as main_module
from magickit.config import Settings
from magickit.deploy import executor_switch
from magickit.deploy.executor_tags import ExecutorTagError
from magickit.executor_pool import api as api_module
from magickit.executor_pool.store import ExecutorPoolStore, format_ts
from magickit.mcp.tools import executor as executor_tools

BASE = "/v1/executor-pool"
T0 = datetime(2026, 11, 10, 3, 0, 0, tzinfo=UTC)  # microsecond == 0
RESERVATION_TOOLS = {
    "executor_reserve",
    "executor_reservation_extend",
    "executor_reservation_release",
}


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


def _capture(settings: Settings) -> dict[str, Any]:
    registered: dict[str, Any] = {}

    def fake_tool(*args: Any, **kwargs: Any):
        def decorator(fn):
            registered[fn.__name__] = fn
            return fn

        return decorator

    mcp = MagicMock()
    mcp.tool = fake_tool
    executor_tools.register_tools(mcp, settings)
    return registered


def _reservation_rows(db: str) -> list[tuple]:
    if not Path(db).exists():
        return []
    with sqlite3.connect(db) as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        if "executor_reservations" not in names:
            return []
        return conn.execute("SELECT * FROM executor_reservations").fetchall()


class Env:
    def __init__(self, tools: dict[str, Any], client: httpx.AsyncClient,
                 clock: Clock, db: str) -> None:
        self.tools, self.client, self.clock, self.db = tools, client, clock, db

    def reservations(self) -> list[tuple]:
        return _reservation_rows(self.db)

    async def reserve(self, executor: str = "ex-a", **over: Any) -> dict[str, Any]:
        args: dict[str, Any] = {"executor": executor, "reserved_by": "Fermi",
                                "reason": "maintenance", "ttl_minutes": 60}
        args.update(over)
        return await self.tools["executor_reserve"](**args)

    async def extend(self, executor: str = "ex-a", ttl: Any = 30) -> dict[str, Any]:
        return await self.tools["executor_reservation_extend"](
            executor=executor, ttl_minutes=ttl)

    async def release(self, executor: str = "ex-a") -> dict[str, Any]:
        return await self.tools["executor_reservation_release"](executor=executor)

    async def request(self, thread: str, executor: str = "ex-a") -> httpx.Response:
        return await self.client.post(f"{BASE}/turn-requests", json={
            "project": "proj", "thread_id": thread, "executor": executor,
            "tag": "executor/v1.0.0",
        })


@pytest.fixture
async def env(monkeypatch, tmp_path: Path):
    db = str(tmp_path / "pool.db")
    clock = Clock()
    store = ExecutorPoolStore(db, clock=clock)
    monkeypatch.setattr(api_module, "_get_store", lambda: store)
    monkeypatch.setattr(executor_tools, "_make_store", lambda settings: store)
    settings = Settings(executor_pool_enabled=True, db_path=db)
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    tools = _capture(settings)
    app = main_module.create_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://testserver") as client:
        yield Env(tools, client, clock, db)


# --- INV-0 -----------------------------------------------------------------


def test_off_registers_no_tool_and_builds_no_store(monkeypatch):
    def _boom(settings):
        raise AssertionError("store built with the pool off")

    monkeypatch.setattr(executor_tools, "_make_store", _boom)
    assert _capture(Settings()) == {}


def test_on_registration_builds_no_store(monkeypatch):
    def _boom(settings):
        raise AssertionError("store built at registration")

    monkeypatch.setattr(executor_tools, "_make_store", _boom)
    assert RESERVATION_TOOLS <= set(_capture(Settings(executor_pool_enabled=True)))


def test_pool_store_is_not_imported_at_module_level():
    src = Path(executor_tools.__file__).read_text(encoding="utf-8")
    top_level = [ln for ln in src.splitlines()
                 if ln.startswith(("import ", "from ")) and "executor_pool" in ln]
    assert top_level == []


def test_ttl_has_no_default():
    tools = _capture(Settings(executor_pool_enabled=True))
    for name in ("executor_reserve", "executor_reservation_extend"):
        param = inspect.signature(tools[name]).parameters["ttl_minutes"]
        assert param.default is inspect.Parameter.empty, name


# --- reserve ---------------------------------------------------------------


async def test_reserve_rejects_queued_and_returns_their_ids(env):
    q1 = (await env.request("T-1")).json()["id"]
    q2 = (await env.request("T-2")).json()["id"]
    other = (await env.request("T-3", executor="ex-b")).json()["id"]
    out = await env.reserve()
    assert out["ok"] is True
    assert out["rejected_request_ids"] == sorted([q1, q2])
    assert other not in out["rejected_request_ids"]
    res = out["reservation"]
    assert res["executor"] == "ex-a" and res["reserved_by"] == "Fermi"
    assert res["expires_at"] == format_ts(T0 + timedelta(minutes=60))
    assert res["released_at"] is None and "rejected_request_ids" not in res


async def test_mcp_reservation_blocks_http_turn_requests(env):
    assert (await env.reserve())["ok"] is True
    r = await env.request("T-9")
    assert r.status_code == 409 and r.json() == {"error": "executor_reserved"}
    assert (await env.request("T-9", executor="ex-b")).status_code == 201


async def test_http_reservation_is_seen_by_mcp(env):
    r = await env.client.post(f"{BASE}/reservations", json={
        "executor": "ex-a", "reserved_by": "Fermi", "reason": "m",
        "ttl_minutes": 30})
    assert r.status_code == 201, r.text
    assert (await env.reserve())["error_type"] == "already_reserved"


async def test_reserve_twice_is_already_reserved(env):
    await env.reserve()
    before = env.reservations()
    out = await env.reserve(reserved_by="someone-else")
    assert out == {"ok": False, "error_type": "already_reserved",
                   "message": "already_reserved"}
    assert env.reservations() == before


@pytest.mark.parametrize("ttl", [0, 1441, -1, True, False, "60", 1.5, None])
async def test_bad_ttl_is_refused_and_nothing_written(env, ttl):
    out = await env.reserve(ttl_minutes=ttl)
    assert out["ok"] is False and out["error_type"] == "invalid_ttl"
    assert env.reservations() == []


@pytest.mark.parametrize("field", ["reserved_by", "reason"])
async def test_empty_field_is_invalid_argument_and_nothing_written(env, field):
    out = await env.reserve(**{field: ""})
    assert out["ok"] is False and out["error_type"] == "invalid_argument"
    assert field in out["message"]
    assert env.reservations() == []


async def test_whitespace_only_field_is_accepted_as_over_http(env):
    out = await env.reserve(reserved_by=" ", reason=" ")
    assert out["ok"] is True and out["reservation"]["reserved_by"] == " "


@pytest.mark.parametrize("name", ["*", "", "a b", "a/b", "ex-a\n"])
async def test_bad_executor_name_is_refused_and_nothing_written(env, name):
    for tool, args in (
        ("executor_reserve", {"reserved_by": "x", "reason": "y", "ttl_minutes": 5}),
        ("executor_reservation_extend", {"ttl_minutes": 5}),
        ("executor_reservation_release", {}),
    ):
        out = await env.tools[tool](executor=name, **args)
        assert out["ok"] is False and out["error_type"] == "invalid_executor", tool
    assert env.reservations() == []


# --- store-level empty check (shared by HTTP and MCP) ----------------------


@pytest.mark.parametrize("field", ["reserved_by", "reason"])
async def test_store_refuses_empty_field_and_writes_nothing(tmp_path, field):
    db = str(tmp_path / "s.db")
    store = ExecutorPoolStore(db)
    args = {"executor": "ex-a", "reserved_by": "x", "reason": "y", "ttl_minutes": 5}
    args[field] = ""
    with pytest.raises(ValueError, match=field):
        await store.create_reservation(**args)
    assert _reservation_rows(db) == []
    args[field] = " "
    assert (await store.create_reservation(**args))[field] == " "


# --- extend ----------------------------------------------------------------


async def test_extend_counts_from_now(env):
    await env.reserve(ttl_minutes=60)
    env.clock.advance(minutes=50)
    out = await env.extend(ttl=30)
    assert out["ok"] is True
    # now (T0+50) + 30, not old expiry (T0+60) + 30
    assert out["reservation"]["expires_at"] == format_ts(T0 + timedelta(minutes=80))


@pytest.mark.parametrize("ttl", [0, 1441, True, "5"])
async def test_extend_bad_ttl_is_invalid_ttl_and_row_untouched(env, ttl):
    await env.reserve()
    before = env.reservations()
    out = await env.extend(ttl=ttl)
    assert out["ok"] is False and out["error_type"] == "invalid_ttl"
    assert env.reservations() == before


async def test_extend_without_reservation_is_not_active(env):
    assert (await env.extend())["error_type"] == "reservation_not_active"


# --- release ---------------------------------------------------------------


async def test_release_succeeds_and_http_requests_are_accepted_again(env):
    await env.reserve()
    assert (await env.request("T-1")).status_code == 409
    env.clock.advance(minutes=5)
    out = await env.release()
    assert out["ok"] is True
    assert out["reservation"]["released_at"] == format_ts(T0 + timedelta(minutes=5))
    r = await env.request("T-1")
    assert r.status_code == 201, r.text


async def test_release_twice_is_not_active(env):
    await env.reserve()
    assert (await env.release())["ok"] is True
    assert await env.release() == {"ok": False, "error_type": "reservation_not_active",
                                   "message": "reservation_not_active"}


# --- expiry ----------------------------------------------------------------


async def test_after_expiry_extend_and_release_are_not_active(env):
    await env.reserve(ttl_minutes=10)
    env.clock.advance(minutes=10)  # in force only while expires_at > now
    assert (await env.extend(ttl=5))["error_type"] == "reservation_not_active"
    assert (await env.release())["error_type"] == "reservation_not_active"
    assert (await env.request("T-1")).status_code == 201
    assert (await env.reserve())["ok"] is True  # a lapsed one does not block


# --- the two executor-name rules agree --------------------------------------


@pytest.mark.parametrize("name", ["ex-a", "EX_1", "*", "", "a b", "a/b", "ex-a\n"])
async def test_http_and_mcp_name_rules_agree(env, name):
    try:
        executor_switch.validate_executor_name(name)
        mcp_ok = True
    except ExecutorTagError:
        mcp_ok = False
    r = await env.client.post(f"{BASE}/reservations", json={
        "executor": name, "reserved_by": "x", "reason": "y", "ttl_minutes": 5})
    assert r.status_code in (201, 422), r.text
    assert (r.status_code == 201) == mcp_ok, (name, r.status_code, mcp_ok)
