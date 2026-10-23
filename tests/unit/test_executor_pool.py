"""ADR-2026-10-09-23 unit 3: executor reports, turn requests, reservations.

Driven through the HTTP surface (``create_app`` with the pool on) against a
store on a temp SQLite file and a fake clock. Rows are compared by reading
SQLite directly, so "409 and the row is untouched" means byte-for-byte.
"""

from __future__ import annotations

import random
import re
import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from magickit import main as main_module
from magickit.config import Settings
from magickit.executor_pool import api as api_module
from magickit.executor_pool.store import (
    EXECUTOR_RESERVED,
    STATES,
    TERMINAL_STATES,
    ExecutorPoolStore,
    format_ts,
)
from magickit.mcp.tools import executor as executor_tools

POOL_TABLES = {"executor_reports", "turn_requests", "executor_reservations"}
TS_RE = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z")
BASE = "/v1/executor-pool"
T0 = datetime(2026, 10, 23, 6, 0, 0, tzinfo=UTC)  # microsecond == 0


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


class Pool:
    def __init__(self, client: httpx.AsyncClient, store: ExecutorPoolStore,
                 clock: Clock, db: str) -> None:
        self.client, self.store, self.clock, self.db = client, store, clock, db

    def row(self, request_id: str) -> tuple | None:
        with sqlite3.connect(self.db) as conn:
            return conn.execute(
                "SELECT * FROM turn_requests WHERE id = ?", (request_id,)
            ).fetchone()

    def rows(self, sql: str, *params: Any) -> list[tuple]:
        with sqlite3.connect(self.db) as conn:
            return conn.execute(sql, params).fetchall()

    async def create(self, thread: str = "T-1", executor: str = "ex-a",
                     project: str = "proj") -> httpx.Response:
        return await self.client.post(f"{BASE}/turn-requests", json={
            "project": project, "thread_id": thread, "executor": executor,
            "tag": "executor/v1.0.0",
        })

    async def queued(self, thread: str = "T-1", executor: str = "ex-a") -> str:
        r = await self.create(thread, executor)
        assert r.status_code == 201, r.text
        return r.json()["id"]

    async def claim(self, rid: str, executor: str = "ex-a",
                    inst: str = "inst-1") -> httpx.Response:
        return await self.client.post(f"{BASE}/turn-requests/{rid}/claim", json={
            "executor": executor, "instance_id": inst, "identity": "Heisenberg",
        })

    async def reject(self, rid: str, executor: str = "ex-a") -> httpx.Response:
        return await self.client.post(f"{BASE}/turn-requests/{rid}/reject", json={
            "executor": executor, "reason": "clone_dirty",
        })

    async def heartbeat(self, rid: str, executor: str = "ex-a",
                        inst: str = "inst-1") -> httpx.Response:
        return await self.client.post(f"{BASE}/turn-requests/{rid}/heartbeat", json={
            "executor": executor, "instance_id": inst,
        })

    async def complete(self, rid: str, executor: str = "ex-a",
                       inst: str = "inst-1") -> httpx.Response:
        return await self.client.post(f"{BASE}/turn-requests/{rid}/complete", json={
            "executor": executor, "instance_id": inst, "result": {"ok": True},
        })

    async def withdraw(self, rid: str) -> httpx.Response:
        return await self.client.post(f"{BASE}/turn-requests/{rid}/withdraw")

    async def lost(self, rid: str) -> httpx.Response:
        return await self.client.post(f"{BASE}/turn-requests/{rid}/lost")

    async def reserve(self, executor: str = "ex-a", ttl: Any = 60) -> httpx.Response:
        body: dict[str, Any] = {"executor": executor, "reserved_by": "Fermi",
                                "reason": "maintenance"}
        if ttl is not None:
            body["ttl_minutes"] = ttl
        return await self.client.post(f"{BASE}/reservations", json=body)

    async def extend(self, executor: str = "ex-a", ttl: Any = 60) -> httpx.Response:
        body = {} if ttl is None else {"ttl_minutes": ttl}
        return await self.client.post(f"{BASE}/reservations/{executor}/extend", json=body)

    async def release(self, executor: str = "ex-a") -> httpx.Response:
        return await self.client.post(f"{BASE}/reservations/{executor}/release")

    async def report(self, executor: str = "ex-a", **over: Any) -> httpx.Response:
        body: dict[str, Any] = {
            "awake": True, "version": "executor/v1.0.0", "free_slots": 2,
            "clones": {"proj": {"clone_dirty": False}}, "sync_failure": None,
        }
        body.update(over)
        return await self.client.put(f"{BASE}/executors/{executor}/report", json=body)


def _app(monkeypatch, *, enabled: bool, db: str):
    settings = Settings(executor_pool_enabled=enabled, db_path=db)
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    return main_module.create_app()


@pytest.fixture
async def pool(monkeypatch, tmp_path: Path):
    db = str(tmp_path / "pool.db")
    clock = Clock()
    store = ExecutorPoolStore(db, clock=clock)
    monkeypatch.setattr(api_module, "_get_store", lambda: store)
    app = _app(monkeypatch, enabled=True, db=db)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://testserver") as client:
        yield Pool(client, store, clock, db)


# --- INV-0 -----------------------------------------------------------------


async def test_off_every_pool_path_is_404_and_no_table_is_created(monkeypatch, tmp_path):
    db = str(tmp_path / "off.db")

    def _boom():
        raise AssertionError("store constructed with the pool off")

    monkeypatch.setattr(api_module, "_get_store", _boom)
    app = _app(monkeypatch, enabled=False, db=db)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://testserver") as client:
        calls = [
            client.put(f"{BASE}/executors/ex-a/report", json={}),
            client.get(f"{BASE}/executors/reports?max_age_seconds=60"),
            client.post(f"{BASE}/turn-requests", json={}),
            client.get(f"{BASE}/turn-requests"),
            client.post(f"{BASE}/turn-requests/x/claim", json={}),
            client.post(f"{BASE}/turn-requests/x/withdraw"),
            client.post(f"{BASE}/reservations", json={}),
            client.post(f"{BASE}/reservations/ex-a/extend", json={}),
            client.post(f"{BASE}/reservations/ex-a/release"),
        ]
        for call in calls:
            assert (await call).status_code == 404
    if Path(db).exists():
        with sqlite3.connect(db) as conn:
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        assert not names & POOL_TABLES


def test_off_mcp_tool_list_is_unchanged():
    mcp = MagicMock()
    executor_tools.register_tools(mcp, Settings())
    mcp.tool.assert_not_called()
    # This unit adds no MCP tool at all: nothing under mcp/ reaches the pool.
    src = Path(executor_tools.__file__).resolve().parents[1]
    for path in src.rglob("*.py"):
        assert "magickit.executor_pool" not in path.read_text(encoding="utf-8"), path


# --- dynamic report --------------------------------------------------------


async def test_report_is_overwritten_whole(pool):
    r = await pool.report(clones={"p1": {"clone_dirty": False}, "p2": {"clone_dirty": True}})
    assert r.status_code == 200, r.text
    r = await pool.report(clones={"p1": {"clone_dirty": True}}, free_slots=0)
    assert r.status_code == 200
    got = (await pool.client.get(f"{BASE}/executors/reports?max_age_seconds=60")).json()
    assert len(got["reports"]) == 1
    rep = got["reports"][0]
    assert rep["clones"] == {"p1": {"clone_dirty": True}}
    assert rep["free_slots"] == 0 and rep["executor"] == "ex-a"
    assert TS_RE.fullmatch(rep["reported_at"])


async def test_stale_reports_are_not_returned(pool):
    await pool.report("ex-old")
    pool.clock.advance(seconds=90)
    await pool.report("ex-new")
    got = (await pool.client.get(f"{BASE}/executors/reports?max_age_seconds=60")).json()
    assert [r["executor"] for r in got["reports"]] == ["ex-new"]
    got = (await pool.client.get(f"{BASE}/executors/reports?max_age_seconds=120")).json()
    assert [r["executor"] for r in got["reports"]] == ["ex-new", "ex-old"]


async def test_report_max_age_is_required(pool):
    assert (await pool.client.get(f"{BASE}/executors/reports")).status_code == 422


@pytest.mark.parametrize("extra", [
    {"capabilities": ["gpu"]},
    {"slots": 4},
    {"wol": {"mac": "aa:bb"}},
    {"clone_paths": {"proj": "C:/x"}},
    {"clones": {"proj": {"clone_dirty": False, "path": "C:/x"}}},
])
async def test_ledger_keys_in_report_are_422(pool, extra):
    await pool.store.list_reports(60)  # create tables, so "no row" is checkable
    assert (await pool.report(**extra)).status_code == 422
    assert pool.rows("SELECT * FROM executor_reports") == []


@pytest.mark.parametrize("name", ["*", "a b", "ex.a"])
async def test_executor_name_is_checked_for_form(pool, name):
    assert (await pool.report(name)).status_code in (404, 422)
    assert (await pool.create(executor=name)).status_code == 422


# --- transitions -----------------------------------------------------------


async def test_each_transition_succeeds(pool):
    a = await pool.queued("T-a")
    r = await pool.claim(a)
    assert r.status_code == 200 and r.json()["transitioned"] is True
    assert r.json()["turn_request"]["state"] == "claimed"
    pool.clock.advance(seconds=5)
    r = await pool.heartbeat(a)
    assert r.status_code == 200
    assert r.json()["turn_request"]["heartbeat_at"] == format_ts(pool.clock.now)
    r = await pool.complete(a)
    assert r.status_code == 200 and r.json()["turn_request"]["state"] == "done"
    assert r.json()["turn_request"]["result"] == {"ok": True}

    b = await pool.queued("T-b")
    r = await pool.reject(b)
    assert r.json()["turn_request"]["state"] == "rejected"
    assert r.json()["turn_request"]["reject_reason"] == "clone_dirty"

    c = await pool.queued("T-c")
    assert (await pool.withdraw(c)).json()["turn_request"]["state"] == "withdrawn"

    d = await pool.queued("T-d")
    await pool.claim(d)
    assert (await pool.lost(d)).json()["turn_request"]["state"] == "lost"


async def _to_state(pool: Pool, state: str, thread: str) -> str:
    rid = await pool.queued(thread)
    if state == "queued":
        return rid
    if state in ("claimed", "done", "lost"):
        assert (await pool.claim(rid)).status_code == 200
        if state == "done":
            assert (await pool.complete(rid)).status_code == 200
        if state == "lost":
            assert (await pool.lost(rid)).status_code == 200
    elif state == "rejected":
        assert (await pool.reject(rid)).status_code == 200
    elif state == "withdrawn":
        assert (await pool.withdraw(rid)).status_code == 200
    return rid


_TRANSITIONS = {
    "claim": ("queued", lambda p, r: p.claim(r, inst="inst-2")),
    "reject": ("queued", lambda p, r: p.reject(r)),
    "withdraw": ("queued", lambda p, r: p.withdraw(r)),
    "heartbeat": ("claimed", lambda p, r: p.heartbeat(r)),
    "complete": ("claimed", lambda p, r: p.complete(r)),
    "lost": ("claimed", lambda p, r: p.lost(r)),
}


@pytest.mark.parametrize("name,state", [
    (n, s) for n, (src, _) in _TRANSITIONS.items() for s in STATES if s != src
])
async def test_wrong_source_state_is_409_and_row_untouched(pool, name, state):
    _, call = _TRANSITIONS[name]
    rid = await _to_state(pool, state, f"T-{name}-{state}")
    before = pool.row(rid)
    pool.clock.advance(seconds=1)
    r = await call(pool, rid)
    assert r.status_code == 409
    assert r.json() == {"transitioned": False}
    assert pool.row(rid) == before


@pytest.mark.parametrize("state", TERMINAL_STATES)
async def test_no_transition_leaves_a_terminal_state(pool, state):
    rid = await _to_state(pool, state, f"T-{state}")
    before = pool.row(rid)
    for name, (_, call) in _TRANSITIONS.items():
        r = await call(pool, rid)
        assert r.status_code == 409, name
    assert pool.row(rid) == before


async def test_every_terminal_move_writes_finished_at(pool):
    rids = [await _to_state(pool, s, f"T-{s}") for s in TERMINAL_STATES]
    victim = await pool.queued("T-res")
    await pool.reserve()
    for rid in rids + [victim]:
        row = (await pool.store.list_turn_requests())
        got = {r["id"]: r for r in row}[rid]
        assert got["state"] in TERMINAL_STATES
        assert got["finished_at"] is not None and TS_RE.fullmatch(got["finished_at"])


async def test_claim_initialises_heartbeat(pool):
    rid = await pool.queued()
    pool.clock.advance(seconds=3)
    tr = (await pool.claim(rid)).json()["turn_request"]
    assert tr["heartbeat_at"] == tr["claimed_at"] == format_ts(pool.clock.now)
    assert tr["instance_id"] == "inst-1" and tr["identity"] == "Heisenberg"


async def test_heartbeat_and_complete_check_instance_and_executor(pool):
    rid = await pool.queued()
    await pool.claim(rid)
    before = pool.row(rid)
    for r in (
        await pool.heartbeat(rid, inst="zombie"),
        await pool.complete(rid, inst="zombie"),
        await pool.heartbeat(rid, executor="ex-b"),
        await pool.complete(rid, executor="ex-b"),
    ):
        assert r.status_code == 409 and r.json() == {"transitioned": False}
    assert pool.row(rid) == before


async def test_claiming_another_executors_request_discloses_nothing(pool):
    rid = await pool.queued(executor="ex-b")
    before = pool.row(rid)
    r = await pool.claim(rid, executor="ex-a")
    assert r.status_code == 409
    assert r.json() == {"transitioned": False}
    assert pool.row(rid) == before


async def test_unknown_id_is_409_not_404(pool):
    r = await pool.claim("no-such-id")
    assert r.status_code == 409 and r.json() == {"transitioned": False}


# --- occupancy -------------------------------------------------------------


async def test_second_request_for_the_same_thread_is_409(pool):
    await pool.queued("T-1")
    r = await pool.create("T-1", executor="ex-b")
    assert r.status_code == 409 and r.json() == {"error": "thread_occupied"}
    # same thread id in another project is a different thread
    assert (await pool.create("T-1", project="other")).status_code == 201


async def test_thread_is_free_again_after_a_terminal_state(pool):
    rid = await pool.queued("T-1")
    await pool.claim(rid)
    assert (await pool.create("T-1")).status_code == 409
    await pool.complete(rid)
    assert (await pool.create("T-1")).status_code == 201


_INSERT = ("INSERT INTO turn_requests (id, project, thread_id, executor, tag,"
           " state, created_at, claimed_at, heartbeat_at, instance_id,"
           " identity, finished_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)")


@pytest.mark.parametrize("row", [
    # claimed without identity
    ("c1", "p", "t1", "e", "g", "claimed", "x", "x", "x", "i", None, None),
    # terminal without finished_at
    ("c2", "p", "t2", "e", "g", "done", "x", "x", "x", "i", "id", None),
    # queued with a claim identifier
    ("c3", "p", "t3", "e", "g", "queued", "x", None, None, "i", None, None),
])
async def test_check_constraints_refuse_direct_inserts(pool, row):
    await pool.store.list_turn_requests()  # create tables
    with sqlite3.connect(pool.db) as conn, pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        conn.execute(_INSERT, row)


# --- reservations ----------------------------------------------------------


async def test_reservation_rejects_that_executors_queued_requests(pool):
    q1 = await pool.queued("T-1")
    q2 = await pool.queued("T-2")
    claimed = await pool.queued("T-3")
    await pool.claim(claimed)
    other = await pool.queued("T-4", executor="ex-b")
    r = await pool.reserve()
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["rejected_request_ids"] == sorted([q1, q2])
    assert body["expires_at"] == format_ts(T0 + timedelta(minutes=60))
    for rid in (q1, q2):
        (row,) = pool.rows("SELECT state, reject_reason, finished_at, instance_id,"
                           " heartbeat_at FROM turn_requests WHERE id=?", rid)
        assert row[:2] == ("rejected", EXECUTOR_RESERVED)
        assert row[2] is not None and row[3] is None and row[4] is None
    states = {r["id"]: r["state"] for r in await pool.store.list_turn_requests()}
    assert states[claimed] == "claimed" and states[other] == "queued"


async def test_request_to_reserved_executor_is_409(pool):
    await pool.reserve()
    r = await pool.create("T-9")
    assert r.status_code == 409 and r.json() == {"error": "executor_reserved"}
    assert (await pool.create("T-9", executor="ex-b")).status_code == 201


async def test_claim_after_reservation_is_409_and_row_stays_rejected(pool):
    rid = await pool.queued()
    await pool.reserve()
    before = pool.row(rid)
    r = await pool.claim(rid)
    assert r.status_code == 409 and r.json() == {"transitioned": False}
    assert pool.row(rid) == before
    (row,) = pool.rows("SELECT state, reject_reason FROM turn_requests WHERE id=?", rid)
    assert row == ("rejected", EXECUTOR_RESERVED)


async def test_second_active_reservation_is_409(pool):
    await pool.reserve()
    r = await pool.reserve()
    assert r.status_code == 409 and r.json() == {"error": "already_reserved"}


async def test_extend_recounts_from_now(pool):
    await pool.reserve(ttl=60)
    pool.clock.advance(minutes=50)
    r = await pool.extend(ttl=60)
    assert r.status_code == 200
    assert r.json()["expires_at"] == format_ts(pool.clock.now + timedelta(minutes=60))


async def test_extend_of_expired_or_released_reservation_is_409(pool):
    assert (await pool.extend()).status_code == 409  # none at all
    await pool.reserve(ttl=1)
    pool.clock.advance(minutes=1)  # expires_at == now -> no longer in force
    r = await pool.extend()
    assert r.status_code == 409 and r.json() == {"error": "reservation_not_active"}
    await pool.reserve("ex-b")
    assert (await pool.release("ex-b")).status_code == 200
    assert (await pool.extend("ex-b")).status_code == 409
    assert (await pool.release("ex-b")).status_code == 409


@pytest.mark.parametrize("ttl", [None, 0, 1441, -5, True, "60"])
async def test_ttl_minutes_out_of_range_is_422(pool, ttl):
    await pool.store.list_reports(60)  # create tables, so "no row" is checkable
    assert (await pool.reserve(ttl=ttl)).status_code == 422
    assert pool.rows("SELECT * FROM executor_reservations") == []
    await pool.reserve("ex-z", ttl=5)
    assert (await pool.extend("ex-z", ttl=ttl)).status_code == 422


async def test_claimed_turn_runs_to_completion_while_reserved(pool):
    rid = await pool.queued()
    await pool.claim(rid)
    await pool.reserve()
    assert (await pool.heartbeat(rid)).status_code == 200
    assert (await pool.complete(rid)).status_code == 200


async def test_after_expiry_or_release_requests_are_accepted_and_claimed(pool):
    await pool.reserve(ttl=10)
    pool.clock.advance(minutes=10, microseconds=1)
    rid = await pool.queued("T-1")
    assert (await pool.claim(rid)).json()["turn_request"]["state"] == "claimed"
    await pool.reserve("ex-b")
    await pool.release("ex-b")
    rid = await pool.queued("T-2", executor="ex-b")
    assert (await pool.claim(rid, executor="ex-b")).status_code == 200


async def test_invariant_no_queued_row_for_a_reserved_executor(pool):
    rng = random.Random(23)
    executors = ["ex-a", "ex-b", "ex-c"]
    thread = 0
    for _ in range(300):
        ex = rng.choice(executors)
        op = rng.choice(["create", "create", "reserve", "extend", "release",
                         "tick", "claim"])
        if op == "create":
            thread += 1
            await pool.create(f"T-{thread}", executor=ex)
        elif op == "reserve":
            await pool.reserve(ex, ttl=rng.randint(1, 30))
        elif op == "extend":
            await pool.extend(ex, ttl=rng.randint(1, 30))
        elif op == "release":
            await pool.release(ex)
        elif op == "tick":
            pool.clock.advance(minutes=rng.randint(1, 20))
        else:
            queued = await pool.store.list_turn_requests(executor=ex, state="queued")
            if queued:
                await pool.claim(queued[0]["id"], executor=ex)
        now = format_ts(pool.clock.now)
        bad = pool.rows(
            "SELECT t.id FROM turn_requests t JOIN executor_reservations r"
            " ON r.executor = t.executor WHERE t.state = 'queued'"
            " AND r.released_at IS NULL AND r.expires_at > ?", now)
        assert bad == [], op


async def test_list_without_executor_sweeps_the_whole_pool(pool):
    a = await pool.queued("T-1", executor="ex-a")
    b = await pool.queued("T-2", executor="ex-b")
    await pool.queued("T-3", executor="ex-c")
    await pool.claim(a, executor="ex-a")
    await pool.claim(b, executor="ex-b")
    r = await pool.client.get(f"{BASE}/turn-requests?state=claimed")
    assert r.status_code == 200
    assert sorted(t["id"] for t in r.json()["turn_requests"]) == sorted([a, b])
    r = await pool.client.get(f"{BASE}/turn-requests?executor=ex-c&state=queued")
    assert len(r.json()["turn_requests"]) == 1
    assert (await pool.client.get(f"{BASE}/turn-requests?state=bogus")).status_code == 422


# --- time ------------------------------------------------------------------


def test_timestamp_format_is_fixed_width():
    assert format_ts(T0) == "2026-10-23T06:00:00.000000Z"
    assert format_ts(T0 + timedelta(microseconds=7)) == "2026-10-23T06:00:00.000007Z"
    jst = timezone(timedelta(hours=9))
    assert format_ts(T0.astimezone(jst)) == format_ts(T0)
    with pytest.raises(ValueError):
        format_ts(datetime(2026, 1, 1))


async def test_every_stored_timestamp_has_the_one_format(pool):
    rid = await pool.queued()
    await pool.claim(rid)
    await pool.complete(rid)
    await pool.reserve()
    await pool.report()
    cols = pool.rows("SELECT created_at, claimed_at, heartbeat_at, finished_at FROM turn_requests")
    cols += pool.rows("SELECT created_at, expires_at FROM executor_reservations")
    cols += pool.rows("SELECT reported_at FROM executor_reports")
    for row in cols:
        for v in row:
            assert TS_RE.fullmatch(v), v


def test_runtime_sqlite_supports_returning():
    assert sqlite3.sqlite_version_info >= (3, 35, 0)
