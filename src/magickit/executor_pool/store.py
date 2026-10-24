"""Executor-pool state on Magickit's SQLite (ADR-2026-10-09-23 unit 3).

Three tables, and only these three:

- ``executor_reports`` -- the executor's *dynamic* report. One row per
  executor, overwritten whole on every tick (``INSERT OR REPLACE``): a
  project present last tick and absent now is gone. Staleness is judged
  at read time against a ``max_age_seconds`` the caller supplies; nothing
  sweeps. Static facts (capabilities, slots, wol, clone paths) live in the
  Conductor's ledger and are refused at the API, never stored here.
- ``turn_requests`` -- a turn request and the occupancy of its thread.
  Six states, four of them terminal (``done``, ``rejected``,
  ``withdrawn``, ``lost``; no transition leaves them). Occupancy is the
  partial unique index on ``(project, thread_id)`` over ``queued`` /
  ``claimed``. There is no FK on ``executor``: the ledger is not in a
  database, and refusing a name the ledger does not know is the
  Conductor's job (ADR §7(f)).
- ``executor_reservations`` -- one row per executor, no history. Whether
  a reservation is in force is decided at read time by
  ``released_at IS NULL AND expires_at > :now``.

Every transition is one ``UPDATE ... WHERE id=:id AND state=<from> ...
RETURNING``. Zero rows means "not transitioned" and the row is untouched;
the store returns ``None`` and the API answers 409 ``{"transitioned":
false}``. Any move into a terminal state writes ``finished_at``.

INVARIANT: no ``queued`` row is addressed to an executor that holds a
reservation in force. It holds because the only way a reservation comes
into force is :meth:`ExecutorPoolStore.create_reservation`, which moves
that executor's ``queued`` rows to ``rejected(executor_reserved)`` in the
same ``BEGIN IMMEDIATE`` transaction, and :meth:`create_turn_request`
refuses (in its own ``BEGIN IMMEDIATE``) while a reservation is in force.
Extension only applies to a reservation already in force; release and
expiry only take one out of force. That is why claim carries no
reservation check: a claim on such a row finds it already ``rejected``
and matches zero rows.

Time: ``:now`` is produced by :func:`format_ts` -- UTC, microseconds,
always ``YYYY-MM-DDTHH:MM:SS.ffffffZ`` (27 characters). SQLite compares
these as strings, so a second format would silently break the expiry
check. ``:now`` is taken *after* ``BEGIN IMMEDIATE`` has the write lock,
so a writer that waited on the lock never judges with a stale clock.

No chatroom producer lives here: nothing in this module posts to a
thread.

INV-0: nothing here runs unless ``executor_pool_enabled`` is true. Tables
are created on first use of a store instance, and with the setting off no
instance is ever constructed (see :mod:`magickit.executor_pool.api`).
"""

from __future__ import annotations

import base64
import json
import sqlite3
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

#: All six states. The last four are terminal.
STATES = ("queued", "claimed", "done", "rejected", "withdrawn", "lost")
TERMINAL_STATES = ("done", "rejected", "withdrawn", "lost")

#: Reason written by reservation creation; nothing else writes it.
EXECUTOR_RESERVED = "executor_reserved"

#: Provisional upper bound on a reservation's TTL (24 hours, D13).
MAX_TTL_MINUTES = 24 * 60

#: How long a writer waits on SQLite's write lock before failing.
BUSY_TIMEOUT_MS = 5000

_TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def format_ts(moment: datetime) -> str:
    """The one timestamp format this module writes and compares."""
    if moment.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return moment.astimezone(UTC).strftime(_TS_FORMAT)


def parse_ts(text: str) -> datetime:
    return datetime.strptime(text, _TS_FORMAT).replace(tzinfo=UTC)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class PoolConflictError(Exception):
    """A create/extend/release refused because of current state (HTTP 409)."""

    def __init__(self, error: str) -> None:
        super().__init__(error)
        self.error = error


_ACTIVE_RESERVATION = (
    "SELECT 1 FROM executor_reservations "
    "WHERE executor = :executor AND released_at IS NULL AND expires_at > :now"
)


#: Page size of ``list_turn_requests`` when none is asked for, and its cap.
DEFAULT_LIST_LIMIT = 200
MAX_LIST_LIMIT = 1000

#: One index per list shape, keyed by (filters on executor, filters on state).
_LIST_INDEX = {
    (True, True): "ix_turn_requests_state_executor_order",
    (False, True): "ix_turn_requests_state_order",
    (True, False): "ix_turn_requests_executor_order",
    (False, False): "ix_turn_requests_order",
}
_LIST_INDEX_COLUMNS = {
    "ix_turn_requests_state_executor_order": "state, executor, created_at, id",
    "ix_turn_requests_state_order": "state, created_at, id",
    "ix_turn_requests_executor_order": "executor, created_at, id",
    "ix_turn_requests_order": "created_at, id",
}


def encode_cursor(created_at: str, request_id: str) -> str:
    """Opaque page cursor: the last row's ``(created_at, id)`` as base64url
    JSON. ``created_at`` is written once at insert and never changes, and
    ``id`` breaks ties between rows of the same instant, so the key is a
    total, stable order."""
    raw = json.dumps([created_at, request_id], separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[str, str]:
    """Inverse of :func:`encode_cursor`; ``ValueError`` on anything else."""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, UnicodeError) as exc:
        raise ValueError("malformed cursor") from exc
    if (
        not isinstance(value, list)
        or len(value) != 2
        or not all(isinstance(v, str) for v in value)
        or not value[1]
    ):
        raise ValueError("malformed cursor")
    created_at, request_id = value
    parse_ts(created_at)  # ValueError unless it is the one format
    if format_ts(parse_ts(created_at)) != created_at:
        raise ValueError("malformed cursor")
    return created_at, request_id


class ExecutorPoolStore:
    def __init__(
        self, db_path: str, clock: Callable[[], datetime] = _utcnow
    ) -> None:
        self.db_path = db_path
        self._clock = clock
        self._tables_ready = False
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

    def now(self) -> str:
        return format_ts(self._clock())

    # --- connection / transaction -----------------------------------------

    @asynccontextmanager
    async def _connect(self) -> AsyncIterator[aiosqlite.Connection]:
        # isolation_level=None: no implicit BEGIN; every write below opens
        # its own BEGIN IMMEDIATE.
        async with aiosqlite.connect(self.db_path, isolation_level=None) as conn:
            await conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            conn.row_factory = aiosqlite.Row
            if not self._tables_ready:
                await self._create_tables(conn)
                self._tables_ready = True
            yield conn

    @asynccontextmanager
    async def _tx(self) -> AsyncIterator[tuple[aiosqlite.Connection, str]]:
        """``BEGIN IMMEDIATE``, then ``:now`` -- in that order (module doc)."""
        async with self._connect() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn, self.now()
            except BaseException:
                await conn.execute("ROLLBACK")
                raise
            await conn.execute("COMMIT")

    async def _create_tables(self, conn: aiosqlite.Connection) -> None:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS executor_reports (
                executor     TEXT PRIMARY KEY,
                reported_at  TEXT NOT NULL,
                awake        INTEGER NOT NULL CHECK (awake IN (0, 1)),
                version      TEXT,
                free_slots   INTEGER NOT NULL CHECK (free_slots >= 0),
                clones       TEXT NOT NULL,
                sync_failure TEXT
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS turn_requests (
                id            TEXT PRIMARY KEY,
                project       TEXT NOT NULL,
                thread_id     TEXT NOT NULL,
                executor      TEXT NOT NULL,
                tag           TEXT NOT NULL,
                payload       TEXT NOT NULL DEFAULT '{}',
                state         TEXT NOT NULL CHECK (state IN
                    ('queued','claimed','done','rejected','withdrawn','lost')),
                reject_reason TEXT,
                result        TEXT,
                created_at    TEXT NOT NULL,
                claimed_at    TEXT,
                heartbeat_at  TEXT,
                instance_id   TEXT,
                identity      TEXT,
                finished_at   TEXT,
                CHECK (state <> 'claimed' OR (claimed_at IS NOT NULL
                    AND heartbeat_at IS NOT NULL AND instance_id IS NOT NULL
                    AND identity IS NOT NULL)),
                CHECK (state NOT IN ('done','rejected','withdrawn','lost')
                    OR finished_at IS NOT NULL),
                CHECK (state <> 'queued' OR (claimed_at IS NULL
                    AND heartbeat_at IS NULL AND instance_id IS NULL
                    AND identity IS NULL))
            )
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS ux_turn_requests_occupancy
                ON turn_requests (project, thread_id)
                WHERE state IN ('queued', 'claimed')
        """)
        # Terminal rows are kept for good (no sweep: they are the record), so
        # no read may scan or sort them all. Every list shape -- filter on
        # state and executor, state only, executor only, none -- has its own
        # index whose equality columns come first and whose tail is the sort
        # key ``(created_at, id)``: ORDER BY + LIMIT then walk the index and
        # never build a temp B-tree. The queries name their index with
        # ``INDEXED BY`` (see ``_LIST_INDEX``), so the planner cannot drift to
        # a sort for want of statistics, and a missing index is an error, not
        # a silent slowdown. Full indexes, not partial: ``state`` is bound as
        # a parameter, which SQLite cannot prove satisfies a partial WHERE.
        for name, columns in _LIST_INDEX_COLUMNS.items():
            await conn.execute(
                f"CREATE INDEX IF NOT EXISTS {name} ON turn_requests ({columns})"
            )
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS executor_reservations (
                executor       TEXT PRIMARY KEY,
                reservation_id TEXT NOT NULL,
                reserved_by    TEXT NOT NULL,
                reason         TEXT NOT NULL,
                created_at     TEXT NOT NULL,
                expires_at     TEXT NOT NULL,
                released_at    TEXT
            )
        """)

    # --- dynamic report ---------------------------------------------------

    async def put_report(
        self,
        executor: str,
        *,
        awake: bool,
        version: str | None,
        free_slots: int,
        clones: dict[str, dict[str, bool]],
        sync_failure: str | None,
    ) -> dict[str, Any]:
        """Overwrite the executor's whole report. ``reported_at`` is ours."""
        async with self._tx() as (conn, now):
            cur = await conn.execute(
                "INSERT OR REPLACE INTO executor_reports (executor, reported_at,"
                " awake, version, free_slots, clones, sync_failure) VALUES"
                " (?, ?, ?, ?, ?, ?, ?) RETURNING *",
                (
                    executor,
                    now,
                    1 if awake else 0,
                    version,
                    free_slots,
                    json.dumps(clones, sort_keys=True),
                    sync_failure,
                ),
            )
            row = await cur.fetchone()
            await cur.close()
        return _report_dict(row)

    async def list_reports(self, max_age_seconds: int) -> list[dict[str, Any]]:
        """Reports newer than ``max_age_seconds``; older ones count as gone."""
        cutoff = format_ts(self._clock() - timedelta(seconds=max_age_seconds))
        async with self._connect() as conn:
            cur = await conn.execute(
                "SELECT * FROM executor_reports WHERE reported_at > ?"
                " ORDER BY executor",
                (cutoff,),
            )
            rows = await cur.fetchall()
        return [_report_dict(r) for r in rows]

    # --- turn requests ----------------------------------------------------

    async def create_turn_request(
        self,
        *,
        project: str,
        thread_id: str,
        executor: str,
        tag: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Queue a request. Refuses while the executor is reserved, or while
        the thread already has a ``queued``/``claimed`` request."""
        async with self._tx() as (conn, now):
            cur = await conn.execute(
                _ACTIVE_RESERVATION, {"executor": executor, "now": now}
            )
            reserved = await cur.fetchone()
            await cur.close()
            if reserved is not None:
                raise PoolConflictError(EXECUTOR_RESERVED)
            try:
                cur = await conn.execute(
                    "INSERT INTO turn_requests (id, project, thread_id, executor,"
                    " tag, payload, state, created_at) VALUES"
                    " (?, ?, ?, ?, ?, ?, 'queued', ?) RETURNING *",
                    (
                        uuid.uuid4().hex,
                        project,
                        thread_id,
                        executor,
                        tag,
                        json.dumps(payload, sort_keys=True),
                        now,
                    ),
                )
                row = await cur.fetchone()
                await cur.close()
            except sqlite3.IntegrityError as exc:
                if "UNIQUE" in str(exc):
                    raise PoolConflictError("thread_occupied") from exc
                raise
        return _request_dict(row)

    async def list_turn_requests(
        self,
        *,
        executor: str | None = None,
        state: str | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """One page, oldest first: ``{"turn_requests": [...], "next_cursor":
        str | None}``. Both filters optional (the Conductor sweeps
        ``state=claimed`` across the whole pool); the limit always applies.
        Paging is by cursor only -- no offset, which would cost O(N) on a
        table that only grows. ``ValueError`` on a bad limit or cursor."""
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= MAX_LIST_LIMIT
        ):
            raise ValueError(f"limit must be 1..{MAX_LIST_LIMIT}")
        clauses: list[str] = []
        params: list[Any] = []
        if executor is not None:
            clauses.append("executor = ?")
            params.append(executor)
        if state is not None:
            clauses.append("state = ?")
            params.append(state)
        if cursor is not None:
            clauses.append("(created_at, id) > (?, ?)")
            params.extend(decode_cursor(cursor))
        index = _LIST_INDEX[(executor is not None, state is not None)]
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        async with self._connect() as conn:
            cur = await conn.execute(
                f"SELECT * FROM turn_requests INDEXED BY {index}{where}"
                " ORDER BY created_at, id LIMIT ?",
                [*params, limit + 1],
            )
            rows = await cur.fetchall()
        page = [_request_dict(r) for r in rows[:limit]]
        next_cursor = (
            encode_cursor(page[-1]["created_at"], page[-1]["id"])
            if len(rows) > limit
            else None
        )
        return {"turn_requests": page, "next_cursor": next_cursor}

    async def _transition(
        self, sql: str, params: dict[str, Any]
    ) -> dict[str, Any] | None:
        async with self._tx() as (conn, now):
            cur = await conn.execute(sql + " RETURNING *", {**params, "now": now})
            row = await cur.fetchone()
            await cur.close()
        return None if row is None else _request_dict(row)

    async def claim(
        self, request_id: str, *, executor: str, instance_id: str, identity: str
    ) -> dict[str, Any] | None:
        """``queued -> claimed``. No reservation check (module INVARIANT)."""
        return await self._transition(
            "UPDATE turn_requests SET state = 'claimed', claimed_at = :now,"
            " heartbeat_at = :now, instance_id = :inst, identity = :identity"
            " WHERE id = :id AND state = 'queued' AND executor = :me",
            {"id": request_id, "me": executor, "inst": instance_id, "identity": identity},
        )

    async def reject(
        self, request_id: str, *, executor: str, reason: str
    ) -> dict[str, Any] | None:
        """``queued -> rejected`` by the executor, before it took the turn."""
        return await self._transition(
            "UPDATE turn_requests SET state = 'rejected', reject_reason = :reason,"
            " finished_at = :now"
            " WHERE id = :id AND state = 'queued' AND executor = :me",
            {"id": request_id, "me": executor, "reason": reason},
        )

    async def heartbeat(
        self, request_id: str, *, executor: str, instance_id: str
    ) -> dict[str, Any] | None:
        return await self._transition(
            "UPDATE turn_requests SET heartbeat_at = :now"
            " WHERE id = :id AND state = 'claimed' AND executor = :me"
            " AND instance_id = :inst",
            {"id": request_id, "me": executor, "inst": instance_id},
        )

    async def complete(
        self,
        request_id: str,
        *,
        executor: str,
        instance_id: str,
        result: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        return await self._transition(
            "UPDATE turn_requests SET state = 'done', result = :result,"
            " finished_at = :now"
            " WHERE id = :id AND state = 'claimed' AND executor = :me"
            " AND instance_id = :inst",
            {
                "id": request_id,
                "me": executor,
                "inst": instance_id,
                "result": None if result is None else json.dumps(result, sort_keys=True),
            },
        )

    async def withdraw(self, request_id: str) -> dict[str, Any] | None:
        """``queued -> withdrawn``, by the Conductor."""
        return await self._transition(
            "UPDATE turn_requests SET state = 'withdrawn', finished_at = :now"
            " WHERE id = :id AND state = 'queued'",
            {"id": request_id},
        )

    async def mark_lost(self, request_id: str) -> dict[str, Any] | None:
        """``claimed -> lost``, by the Conductor only (ADR D5). No lease
        takeover exists; a lost request is parked, not re-queued."""
        return await self._transition(
            "UPDATE turn_requests SET state = 'lost', finished_at = :now"
            " WHERE id = :id AND state = 'claimed'",
            {"id": request_id},
        )

    # --- reservations -----------------------------------------------------

    async def create_reservation(
        self, *, executor: str, reserved_by: str, reason: str, ttl_minutes: int
    ) -> dict[str, Any]:
        """Reserve ``executor``; reject its ``queued`` requests in the same
        transaction and return their ids. Refuses if one is in force."""
        _check_ttl(ttl_minutes)
        async with self._tx() as (conn, now):
            cur = await conn.execute(
                _ACTIVE_RESERVATION, {"executor": executor, "now": now}
            )
            active = await cur.fetchone()
            await cur.close()
            if active is not None:
                raise PoolConflictError("already_reserved")
            expires = format_ts(parse_ts(now) + timedelta(minutes=ttl_minutes))
            cur = await conn.execute(
                "INSERT OR REPLACE INTO executor_reservations (executor,"
                " reservation_id, reserved_by, reason, created_at, expires_at,"
                " released_at) VALUES (?, ?, ?, ?, ?, ?, NULL) RETURNING *",
                (executor, uuid.uuid4().hex, reserved_by, reason, now, expires),
            )
            row = await cur.fetchone()
            await cur.close()
            cur = await conn.execute(
                "UPDATE turn_requests INDEXED BY"
                f" {_LIST_INDEX[(True, True)]} SET state = 'rejected',"
                " reject_reason = :reason, finished_at = :now"
                " WHERE executor = :executor AND state = 'queued' RETURNING id",
                {"reason": EXECUTOR_RESERVED, "now": now, "executor": executor},
            )
            rejected = sorted(r["id"] for r in await cur.fetchall())
            await cur.close()
        return {**dict(row), "rejected_request_ids": rejected}

    async def extend_reservation(
        self, executor: str, *, ttl_minutes: int
    ) -> dict[str, Any]:
        """Re-count from now (``expires_at = now + ttl``), never add to the
        current expiry -- adding would let the 24h bound grow without end.
        Only a reservation in force can be extended."""
        _check_ttl(ttl_minutes)
        async with self._tx() as (conn, now):
            expires = format_ts(parse_ts(now) + timedelta(minutes=ttl_minutes))
            cur = await conn.execute(
                "UPDATE executor_reservations SET expires_at = :expires"
                " WHERE executor = :executor AND released_at IS NULL"
                " AND expires_at > :now RETURNING *",
                {"expires": expires, "executor": executor, "now": now},
            )
            row = await cur.fetchone()
            await cur.close()
        if row is None:
            raise PoolConflictError("reservation_not_active")
        return dict(row)

    async def release_reservation(self, executor: str) -> dict[str, Any]:
        async with self._tx() as (conn, now):
            cur = await conn.execute(
                "UPDATE executor_reservations SET released_at = :now"
                " WHERE executor = :executor AND released_at IS NULL"
                " AND expires_at > :now RETURNING *",
                {"executor": executor, "now": now},
            )
            row = await cur.fetchone()
            await cur.close()
        if row is None:
            raise PoolConflictError("reservation_not_active")
        return dict(row)


def _check_ttl(ttl_minutes: int) -> None:
    if (
        not isinstance(ttl_minutes, int)
        or isinstance(ttl_minutes, bool)
        or not 1 <= ttl_minutes <= MAX_TTL_MINUTES
    ):
        raise ValueError(f"ttl_minutes must be 1..{MAX_TTL_MINUTES}")


def _report_dict(row: aiosqlite.Row) -> dict[str, Any]:
    d = dict(row)
    d["awake"] = bool(d["awake"])
    d["clones"] = json.loads(d["clones"])
    return d


def _request_dict(row: aiosqlite.Row) -> dict[str, Any]:
    d = dict(row)
    d["payload"] = json.loads(d["payload"])
    d["result"] = None if d["result"] is None else json.loads(d["result"])
    return d
