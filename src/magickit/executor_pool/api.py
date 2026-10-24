"""HTTP surface of the executor pool (ADR-2026-10-09-23 unit 3).

Machine-facing, under ``/v1/executor-pool``. Executors call report /
claim / reject / heartbeat / complete; the Conductor calls create /
withdraw / lost and the reservation endpoints.

No authentication, following ``PUT /v1/decisions/.../material``. The
``instance_id`` check on heartbeat/complete only protects against a
zombie's mistakes, not against impersonation: this surface must stay
inside the private network. Recorded as a known limit of this unit.

INV-0: :func:`magickit.main.create_app` includes this router only when
``executor_pool_enabled`` is true. Off, every path here is 404 and
:func:`_get_store` is never called, so no table is ever created.

Executor names are checked for form only (letters, digits, ``_``, ``-``
-- the bare TOML key ``[executors.<name>]`` takes). Whether the name is
in the ledger, and refusing a wildcard ``*`` as a matter of policy, is
the Conductor's validation (ADR §7(f)); ``*`` failing the form check
here is incidental.

Every state transition answers 200 ``{"transitioned": true,
"turn_request": ...}`` or 409 ``{"transitioned": false}`` -- the same
shape for every transition, and nothing about the row is disclosed on
409 (the id may belong to another executor).
"""

from __future__ import annotations

import functools
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Path, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from magickit.config import get_settings
from magickit.executor_pool.store import (
    DEFAULT_LIST_LIMIT,
    MAX_LIST_LIMIT,
    MAX_TTL_MINUTES,
    ExecutorPoolStore,
    PoolConflictError,
)

router = APIRouter(prefix="/v1/executor-pool", tags=["executor-pool"])

_NAME_PATTERN = r"^[A-Za-z0-9_-]+$"
ExecutorName = Annotated[str, Field(pattern=_NAME_PATTERN)]
ExecutorPath = Annotated[str, Path(pattern=_NAME_PATTERN)]
NonEmpty = Annotated[str, Field(min_length=1)]
Ttl = Annotated[int, Field(ge=1, le=MAX_TTL_MINUTES, strict=True)]
State = Literal["queued", "claimed", "done", "rejected", "withdrawn", "lost"]


@functools.lru_cache(maxsize=1)
def _get_store() -> ExecutorPoolStore:
    """Process-singleton store on the shared SQLite file. Tests patch this."""
    return ExecutorPoolStore(get_settings().db_path)


class _Strict(BaseModel):
    # Unknown keys are 422. For the report this is what keeps the ledger's
    # static facts (capabilities, slots, wol, clone paths) out of Magickit.
    model_config = ConfigDict(extra="forbid")


class CloneStatus(_Strict):
    clone_dirty: bool = Field(strict=True)


class ExecutorReport(_Strict):
    awake: bool = Field(strict=True)
    version: str | None = None
    free_slots: int = Field(ge=0, strict=True)
    clones: dict[NonEmpty, CloneStatus]
    sync_failure: str | None = None


class TurnRequestCreate(_Strict):
    project: NonEmpty
    thread_id: NonEmpty
    executor: ExecutorName
    tag: NonEmpty
    payload: dict[str, Any] = Field(default_factory=dict)


class ClaimBody(_Strict):
    executor: ExecutorName
    instance_id: NonEmpty
    identity: NonEmpty


class RejectBody(_Strict):
    executor: ExecutorName
    reason: NonEmpty


class InstanceBody(_Strict):
    executor: ExecutorName
    instance_id: NonEmpty


class CompleteBody(InstanceBody):
    result: dict[str, Any] | None = None


class ReservationCreate(_Strict):
    executor: ExecutorName
    reserved_by: NonEmpty
    reason: NonEmpty
    ttl_minutes: Ttl


class ReservationExtend(_Strict):
    ttl_minutes: Ttl


def _transitioned(row: dict[str, Any] | None) -> Any:
    if row is None:
        return JSONResponse(status_code=409, content={"transitioned": False})
    return {"transitioned": True, "turn_request": row}


def _conflict(exc: PoolConflictError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"error": exc.error})


# --- dynamic report --------------------------------------------------------


@router.put("/executors/{name}/report")
async def put_report(name: ExecutorPath, report: ExecutorReport) -> dict[str, Any]:
    clones = {p: c.model_dump() for p, c in report.clones.items()}
    return await _get_store().put_report(
        name,
        awake=report.awake,
        version=report.version,
        free_slots=report.free_slots,
        clones=clones,
        sync_failure=report.sync_failure,
    )


@router.get("/executors/reports")
async def list_reports(
    max_age_seconds: Annotated[int, Query(ge=1)],
) -> dict[str, Any]:
    """Required ``max_age_seconds``: the liveness interval is the
    Conductor's setting, passed on every read, never held here."""
    return {"reports": await _get_store().list_reports(max_age_seconds)}


# --- turn requests ---------------------------------------------------------


@router.post("/turn-requests", status_code=201)
async def create_turn_request(body: TurnRequestCreate) -> Any:
    try:
        return await _get_store().create_turn_request(**body.model_dump())
    except PoolConflictError as exc:
        return _conflict(exc)


@router.get("/turn-requests")
async def list_turn_requests(
    executor: Annotated[str | None, Query(pattern=_NAME_PATTERN)] = None,
    state: State | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIST_LIMIT)] = DEFAULT_LIST_LIMIT,
    cursor: Annotated[str | None, Query(min_length=1)] = None,
) -> Any:
    """One page, oldest first: ``{"turn_requests": [...], "next_cursor":
    str | null}``. ``limit`` always applies, filters or not. Paging is by the
    opaque ``cursor`` only; there is no ``offset``. A cursor that does not
    decode is 422."""
    try:
        return await _get_store().list_turn_requests(
            executor=executor, state=state, limit=limit, cursor=cursor
        )
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"error": str(exc)})


@router.post("/turn-requests/{request_id}/claim")
async def claim(request_id: str, body: ClaimBody) -> Any:
    return _transitioned(
        await _get_store().claim(
            request_id,
            executor=body.executor,
            instance_id=body.instance_id,
            identity=body.identity,
        )
    )


@router.post("/turn-requests/{request_id}/reject")
async def reject(request_id: str, body: RejectBody) -> Any:
    return _transitioned(
        await _get_store().reject(request_id, executor=body.executor, reason=body.reason)
    )


@router.post("/turn-requests/{request_id}/heartbeat")
async def heartbeat(request_id: str, body: InstanceBody) -> Any:
    return _transitioned(
        await _get_store().heartbeat(
            request_id, executor=body.executor, instance_id=body.instance_id
        )
    )


@router.post("/turn-requests/{request_id}/complete")
async def complete(request_id: str, body: CompleteBody) -> Any:
    return _transitioned(
        await _get_store().complete(
            request_id,
            executor=body.executor,
            instance_id=body.instance_id,
            result=body.result,
        )
    )


@router.post("/turn-requests/{request_id}/withdraw")
async def withdraw(request_id: str) -> Any:
    return _transitioned(await _get_store().withdraw(request_id))


@router.post("/turn-requests/{request_id}/lost")
async def lost(request_id: str) -> Any:
    return _transitioned(await _get_store().mark_lost(request_id))


# --- reservations ----------------------------------------------------------


@router.post("/reservations", status_code=201)
async def create_reservation(body: ReservationCreate) -> Any:
    try:
        return await _get_store().create_reservation(**body.model_dump())
    except PoolConflictError as exc:
        return _conflict(exc)


@router.post("/reservations/{name}/extend")
async def extend_reservation(name: ExecutorPath, body: ReservationExtend) -> Any:
    try:
        return await _get_store().extend_reservation(name, ttl_minutes=body.ttl_minutes)
    except PoolConflictError as exc:
        return _conflict(exc)


@router.post("/reservations/{name}/release")
async def release_reservation(name: ExecutorPath) -> Any:
    try:
        return await _get_store().release_reservation(name)
    except PoolConflictError as exc:
        return _conflict(exc)
