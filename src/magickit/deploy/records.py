"""Deploy records, the audit trail, and the per-target lock.

Three jobs, kept in one module because they share a directory layout:

``<state root>/requests/<id>.json``
    The mutable state of one deploy request. Rewritten as it moves
    pending -> approved -> running -> succeeded/failed.

``<state root>/audit.jsonl``
    Append-only. R-8: who asked, who approved, which target, which sha,
    what happened, and -- if a human overrode the ref -- why. It is
    append-only because the request file is not: the request says what
    is true now, the audit says what was true then, and an investigation
    into a bad deploy needs the second one.

``<state root>/locks/<target>.lock``
    R-9. One deploy per target at a time, enforced by ``flock`` rather
    than by a status field, because the failure being prevented is two
    *processes* pinning the same working tree -- and a status field is
    still "true" when the process holding it has died.

That last property also settles what an interrupted deploy means. If a
runner is killed, the kernel drops its lock, so the next runner takes it
immediately; a request left in ``running`` with the lock free therefore
has no process behind it and is marked ``interrupted``. Nothing polls,
nothing expires on a timer, and there is no state where the system
thinks a dead deploy is still going.

The state root is under magickit's ``data/`` for a reason that is not
tidiness: the MCP server runs with ``ProtectHome=read-only`` and a
single ``ReadWritePaths=.../data``, so this is the only place the
process filing a request is able to write.
"""

from __future__ import annotations

import fcntl
import json
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from magickit.utils.logging import get_logger

logger = get_logger(__name__)

# ── request lifecycle ────────────────────────────────────────────

STATUS_PENDING = "pending_approval"
STATUS_APPROVED = "approved"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_INTERRUPTED = "interrupted"

TERMINAL_STATUSES = frozenset({STATUS_SUCCEEDED, STATUS_FAILED, STATUS_INTERRUPTED})

# ── what is actually running afterwards (R-7) ────────────────────
#
# "The deploy failed" and "the service is down" are different facts and
# the caller needs both. A failed deploy that left the previous version
# serving is a Monday problem; a failed deploy that left nothing serving
# is a right-now problem, and one word has to separate them.

SERVICE_UP_NEW = "running_new"
SERVICE_UP_PREVIOUS = "running_previous"
SERVICE_UP_UNKNOWN_VERSION = "running_unknown_version"
SERVICE_DOWN = "down"
SERVICE_UNKNOWN = "unknown"


def utcnow() -> str:
    """UTC, ISO 8601, seconds resolution -- the format the audit uses."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def default_state_root() -> Path:
    """Where deploy state lives.

    ``MAGICKIT_DEPLOY_STATE_DIR`` overrides it; otherwise ``data/deploy``
    relative to the process's working directory, which both magickit
    units set to the repo root.
    """
    override = os.environ.get("MAGICKIT_DEPLOY_STATE_DIR")
    if override:
        return Path(override)
    return Path("data/deploy")


@dataclass
class DeployRequest:
    """One request to deploy one target.

    ``override_ref`` is the only way a ref other than
    :data:`~magickit.deploy.registry.DEPLOY_REF` enters the system. It is
    not settable from the requesting side -- see
    ``tests/unit/test_deploy_ref_is_not_reachable.py`` -- and requires
    ``override_reason``, which is what makes the override auditable
    rather than merely possible.
    """

    request_id: str
    target: str
    requested_by: str
    reason: str
    created_at: str
    status: str = STATUS_PENDING

    approved_by: str | None = None
    approved_at: str | None = None
    approval_note: str | None = None

    override_ref: str | None = None
    override_reason: str | None = None
    #: R-2. A ref override alone does not unlock migrations; the
    #: approver has to say so separately, because the thing that does
    #: not come back from a bad deploy is state, not code.
    override_allows_migration: bool = False

    runner_unit: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    result: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> DeployRequest:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in known})

    @property
    def ref(self) -> str:
        """The ref this request will pin. The constant unless overridden."""
        from magickit.deploy.registry import DEPLOY_REF

        return self.override_ref or DEPLOY_REF

    @property
    def is_default_ref(self) -> bool:
        return self.override_ref is None


@dataclass
class StepResult:
    """One step of a deploy, as the runner observed it.

    R-6: the caller should be able to see which step failed without
    reading a transcript. ``detail`` is short and factual; anything long
    (agent output, command stderr) is truncated into it, not attached.
    """

    name: str
    ok: bool
    detail: str = ""
    started_at: str = field(default_factory=utcnow)
    finished_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DeployResult:
    """What happened, in a shape a caller can compare against a PR.

    ``deployed_sha`` is read back out of the working tree by the runner
    after the fact, never taken from the agent's report: the machine
    check that matters ("is the sha that is live the sha that was
    merged") is worthless if the number came from the thing being
    checked.
    """

    request_id: str
    target: str
    ok: bool
    status: str
    ref: str
    is_default_ref: bool
    requested_sha: str | None = None
    deployed_sha: str | None = None
    previous_sha: str | None = None
    migration_allowed: bool = False
    migration_applied: bool | None = None
    service_state: str = SERVICE_UNKNOWN
    services: list[str] = field(default_factory=list)
    health_ok: bool | None = None
    health_detail: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)
    agent_summary: str = ""
    agent_denials: list[str] = field(default_factory=list)
    diagnosis: str = ""
    error: str | None = None
    started_at: str | None = None
    finished_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class DeployStore:
    """Files under one root. No database: this has to be readable by a
    human over ssh when the thing that broke is magickit itself.
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else default_state_root()
        self.requests_dir = self.root / "requests"
        self.locks_dir = self.root / "locks"
        self.audit_path = self.root / "audit.jsonl"

    def _ensure(self) -> None:
        self.requests_dir.mkdir(parents=True, exist_ok=True)
        self.locks_dir.mkdir(parents=True, exist_ok=True)

    # ── requests ─────────────────────────────────────────────────

    def create(self, *, target: str, requested_by: str, reason: str) -> DeployRequest:
        self._ensure()
        request = DeployRequest(
            request_id=uuid.uuid4().hex[:12],
            target=target,
            requested_by=requested_by,
            reason=reason,
            created_at=utcnow(),
        )
        self.save(request)
        self.audit(
            "requested",
            request_id=request.request_id,
            target=target,
            actor=requested_by,
            reason=reason,
        )
        return request

    def path_for(self, request_id: str) -> Path:
        # request ids are generated here (hex), so a caller-supplied id
        # that is not hex is a lookup miss, not a path to open.
        if not request_id.isalnum():
            raise KeyError(request_id)
        return self.requests_dir / f"{request_id}.json"

    def load(self, request_id: str) -> DeployRequest:
        path = self.path_for(request_id)
        if not path.exists():
            raise KeyError(request_id)
        return DeployRequest.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def save(self, request: DeployRequest) -> None:
        self._ensure()
        path = self.path_for(request.request_id)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(request.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)

    def list_requests(self, *, limit: int = 20, target: str | None = None) -> list[DeployRequest]:
        if not self.requests_dir.exists():
            return []
        requests = []
        for path in self.requests_dir.glob("*.json"):
            try:
                requests.append(
                    DeployRequest.from_dict(json.loads(path.read_text(encoding="utf-8")))
                )
            except (OSError, json.JSONDecodeError, TypeError):
                logger.warning("Unreadable deploy request file", path=str(path))
        if target is not None:
            requests = [r for r in requests if r.target == target]
        requests.sort(key=lambda r: r.created_at, reverse=True)
        return requests[:limit]

    # ── audit (R-8) ──────────────────────────────────────────────

    def audit(self, event: str, **fields: Any) -> None:
        """Append one line. Never rewrites, never truncates."""
        self._ensure()
        record = {"at": utcnow(), "event": event, **fields}
        with self.audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def read_audit(self, *, limit: int = 50, target: str | None = None) -> list[dict[str, Any]]:
        """Most recent last -- the order the file is written in."""
        if not self.audit_path.exists():
            return []
        records = []
        for line in self.audit_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        if target is not None:
            records = [r for r in records if r.get("target") == target]
        return records[-limit:]

    # ── lock (R-9) ───────────────────────────────────────────────

    @contextmanager
    def target_lock(self, target: str) -> Iterator[None]:
        """Hold the per-target lock, or raise :class:`DeployLockedError`.

        Non-blocking on purpose. A deploy that waits behind another one
        finishes minutes later against a tree the first deploy moved,
        and the caller has long since been told "running". Refusing is
        the honest answer.
        """
        self._ensure()
        path = self.locks_dir / f"{target}.lock"
        fh = path.open("a+")
        try:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                fh.seek(0)
                holder = fh.read().strip()
                raise DeployLockedError(
                    f"another deploy of {target} is in flight ({holder or 'holder unknown'})"
                ) from exc
            fh.seek(0)
            fh.truncate()
            fh.write(f"pid={os.getpid()} at={utcnow()}\n")
            fh.flush()
            yield
        finally:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            finally:
                fh.close()

    def reap_interrupted(self, target: str) -> list[str]:
        """Mark still-``running`` requests for a target as interrupted.

        Only correct while the caller holds the target lock: the lock
        being free is precisely the evidence that no runner survives for
        those requests.
        """
        reaped = []
        for request in self.list_requests(limit=1000, target=target):
            if request.status != STATUS_RUNNING:
                continue
            request.status = STATUS_INTERRUPTED
            request.finished_at = utcnow()
            if request.result is None:
                request.result = {}
            request.result["error"] = (
                "the runner did not finish; the deploy was interrupted and the "
                "service state at that moment was not recorded"
            )
            self.save(request)
            self.audit(
                "interrupted",
                request_id=request.request_id,
                target=target,
            )
            reaped.append(request.request_id)
        return reaped


class DeployLockedError(Exception):
    """A deploy of this target is already in flight."""


def get_store() -> DeployStore:
    """The store rooted at :func:`default_state_root`."""
    return DeployStore(default_state_root())
