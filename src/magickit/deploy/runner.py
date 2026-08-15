"""The deploy runner: lock, pin, back up, run the agent, restart, verify.

Runs as its own transient unit, started by
:mod:`magickit.deploy.launcher` when a human approves a request. It is
outside the MCP server's sandbox, which is the point: the MCP server
cannot restart a service or write to a repo (measured -- ``sudo`` is
blocked by ``NoNewPrivileges`` and the trees are read-only to it), so
the ability to deploy is not reachable from the surface that files
requests. Reaching the unauthenticated MCP tool gets you a queued
request and nothing else.

The order of steps is the design. Everything that can refuse happens
before anything that changes state:

1. lock the target (R-9), and mark any deploy left ``running`` by a
   dead runner as ``interrupted``
2. pin the tree to the approved commit -- refuses on a dirty tree, an
   unresolvable ref, or a non-fast-forward, all before any write
3. read the current migration revision, so a later one can be compared
4. back up (R-2), unconditionally, before the agent exists
5. re-check the migration gate against ``origin/main`` and run the agent
   with migrations allowed or denied accordingly
6. restart -- the runner's step, never the agent's
7. check health, read back the sha from git, and if it is bad, hand the
   wreckage to a read-only agent for a diagnosis

Steps 6 and 7 are the runner's rather than the agent's for two reasons.
The unit name is already magickit's (R-4 puts it in the registry), so
nothing rots by keeping the restart here; and with the restart out of
the agent's job description the agent needs no privilege at all, which
is what lets its unit set ``NoNewPrivileges=true`` and turn "must not
escalate" from an instruction into a kernel refusal.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import httpx

from magickit.deploy import agent as agent_mod
from magickit.deploy import pin as pin_mod
from magickit.deploy import records
from magickit.deploy.records import (
    SERVICE_DOWN,
    SERVICE_UNKNOWN,
    SERVICE_UP_NEW,
    SERVICE_UP_PREVIOUS,
    SERVICE_UP_UNKNOWN_VERSION,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    DeployRequest,
    DeployResult,
    DeployStore,
    StepResult,
    utcnow,
)
from magickit.deploy.registry import DEPLOY_REF, DeployTarget, TargetNotAllowedError, resolve_target
from magickit.utils.logging import get_logger

logger = get_logger(__name__)

BACKUP_TIMEOUT_S = 900.0
RESTART_TIMEOUT_S = 300.0
HEALTH_POLL_INTERVAL_S = 2.0


class _Steps:
    """Accumulates step results so a failure keeps the ones before it."""

    def __init__(self) -> None:
        self.items: list[StepResult] = []

    def record(self, name: str, ok: bool, detail: str = "") -> StepResult:
        step = StepResult(name=name, ok=ok, detail=detail[:2000], finished_at=utcnow())
        self.items.append(step)
        logger.info("Deploy step", step=name, ok=ok, detail=detail[:200])
        return step

    def as_dicts(self) -> list[dict]:
        return [s.to_dict() for s in self.items]


def run(request_id: str, *, store: DeployStore | None = None) -> DeployResult:
    """Execute one approved deploy request. Never raises for deploy failure."""
    store = store or records.get_store()
    request = store.load(request_id)
    steps = _Steps()

    try:
        target = resolve_target(request.target)
    except TargetNotAllowedError as exc:
        return _fail(
            store,
            request,
            steps,
            error=str(exc),
            service_state=SERVICE_UNKNOWN,
        )

    try:
        with store.target_lock(request.target):
            store.reap_interrupted(request.target)
            request.status = STATUS_RUNNING
            request.started_at = utcnow()
            store.save(request)
            store.audit(
                "started",
                request_id=request.request_id,
                target=request.target,
                ref=request.ref,
                approved_by=request.approved_by,
            )
            return _run_locked(store, request, target, steps)
    except records.DeployLockedError as exc:
        return _fail(
            store,
            request,
            steps,
            error=f"{exc}. Nothing was changed.",
            service_state=SERVICE_UNKNOWN,
        )


def _run_locked(
    store: DeployStore,
    request: DeployRequest,
    target: DeployTarget,
    steps: _Steps,
) -> DeployResult:
    repo = target.repo_path

    # ── 1. pin (nothing is restarted if this refuses) ────────────
    try:
        pinned = pin_mod.pin(repo, request.ref)
    except pin_mod.PinRefusedError as exc:
        return _fail(
            store,
            request,
            steps,
            error=f"could not pin {request.ref}: {exc}",
            service_state=_observe_service(target, steps, restarted=False)[0],
        )
    steps.record(
        "pin",
        True,
        f"{pinned.previous_sha[:12]} -> {pinned.sha[:12]} ({pinned.ref})"
        + (" [detached: ref override]" if pinned.detached else ""),
    )

    # ── 2. the migration gate (R-2) ──────────────────────────────
    revision_before = _alembic_revision(repo)
    migration_allowed, gate_reason = _migration_gate(repo, request, pinned.sha)
    steps.record(
        "migration-gate",
        True,
        ("allowed: " if migration_allowed else "blocked: ") + gate_reason,
    )

    # ── 2b. a shut gate must also bind the restart ───────────────
    #
    # Denying alembic to the agent is not sufficient, because the agent
    # is not the only thing that runs it. conclair's unit carries
    # `ExecStartPre=.../alembic upgrade head`, so *restarting the
    # service* applies whatever migrations are in the tree -- systemd
    # would walk straight through a gate that only constrained the
    # agent. So when the gate is shut, the deploy refuses code that has
    # migrations waiting, before anything is backed up, prepared or
    # restarted. That is also the honest position: shipping code that
    # needs a migration without the migration is broken anyway.
    if not migration_allowed:
        pending = _alembic_pending(repo)
        if pending:
            return _fail(
                store,
                request,
                steps,
                error=(
                    "migrations are not allowed for this run "
                    f"({gate_reason}), but this commit has migrations the database "
                    "has not applied. The service unit runs `alembic upgrade head` "
                    "on start, so restarting would apply them anyway -- the deploy "
                    "stopped instead. Nothing was restarted. Approve the ref "
                    "override with migrations explicitly, or deploy origin/main."
                ),
                service_state=_observe_service(target, steps, restarted=False)[0],
                deployed_sha=pinned.sha,
                previous_sha=pinned.previous_sha,
                pinned=pinned,
                migration_allowed=False,
            )
        steps.record(
            "migration-pending-check",
            True,
            "no unapplied migrations in this commit" if pending is False else "not applicable",
        )

    # ── 3. backup, before anything can touch state ───────────────
    backup_detail: str | None = None
    if target.backup_script is not None:
        script = repo / target.backup_script
        if not script.exists():
            return _fail(
                store,
                request,
                steps,
                error=(
                    f"{target.name} declares a backup script at {target.backup_script} "
                    "and it is not there. Refusing to deploy: the snapshot is the only "
                    "thing standing between a bad migration and restoring from a "
                    "day-old dump. Nothing was changed except the pinned tree."
                ),
                service_state=_observe_service(target, steps, restarted=False)[0],
                deployed_sha=pinned.sha,
                previous_sha=pinned.previous_sha,
                pinned=pinned,
            )
        ok, detail = _run_backup(script, repo)
        steps.record("backup", ok, detail)
        if not ok:
            return _fail(
                store,
                request,
                steps,
                error=f"the backup failed, so the deploy stopped before the agent ran: {detail}",
                service_state=_observe_service(target, steps, restarted=False)[0],
                deployed_sha=pinned.sha,
                previous_sha=pinned.previous_sha,
                pinned=pinned,
            )
        backup_detail = detail
    else:
        steps.record("backup", True, "target declares no backup script")

    # ── 4. the agent prepares the code ───────────────────────────
    scratch = store.root / "runs" / request.request_id
    brief = agent_mod.render_prepare_brief(
        target=target,
        sha=pinned.sha,
        ref=pinned.ref,
        default_ref=DEPLOY_REF,
        migration_allowed=migration_allowed,
        migration_block_reason=gate_reason,
        backup_path=backup_detail,
    )
    outcome = agent_mod.run_agent(
        target=target,
        scratch=scratch,
        unit=f"magickit-deploy-agent-{request.request_id}",
        brief=brief,
        migration_allowed=migration_allowed,
    )
    steps.record(
        "agent-prepare",
        outcome.ok,
        outcome.summary or outcome.error or "",
    )
    for step in outcome.steps:
        steps.record(
            f"agent:{step.get('name', '?')}",
            bool(step.get("ok")),
            str(step.get("detail", "")),
        )

    # ── 5. did a migration happen that was not allowed? ──────────
    revision_after = _alembic_revision(repo)
    migration_applied = _migration_applied(revision_before, revision_after, outcome)
    if migration_applied and not migration_allowed:
        return _fail(
            store,
            request,
            steps,
            error=(
                "a migration was applied while the migration gate was shut "
                f"({revision_before} -> {revision_after}). The service was NOT "
                "restarted. This is a containment failure, not a deploy failure: "
                "check the alembic history against origin/main before doing "
                "anything else."
            ),
            service_state=_observe_service(target, steps, restarted=False)[0],
            deployed_sha=pinned.sha,
            previous_sha=pinned.previous_sha,
            pinned=pinned,
            outcome=outcome,
            migration_allowed=migration_allowed,
            migration_applied=True,
        )

    if not outcome.ok:
        state, health_ok, health_detail = _observe_service(target, steps, restarted=False)
        error = outcome.error or "the agent did not report success"
        if outcome.undetermined:
            error = (
                "the agent could not determine this repository's deploy procedure "
                f"and stopped without acting: {error}"
            )
        return _fail(
            store,
            request,
            steps,
            error=error,
            service_state=state,
            health_ok=health_ok,
            health_detail=health_detail,
            deployed_sha=pinned.sha,
            previous_sha=pinned.previous_sha,
            pinned=pinned,
            outcome=outcome,
            migration_allowed=migration_allowed,
            migration_applied=migration_applied,
        )

    # ── 6. restart (the runner's step) ───────────────────────────
    restart_ok, restart_detail = _restart(target)
    steps.record("restart", restart_ok, restart_detail)

    # ── 7. verify ────────────────────────────────────────────────
    state, health_ok, health_detail = _observe_service(target, steps, restarted=restart_ok)
    deployed_sha = _safe_head(repo)

    ok = restart_ok and (health_ok is not False) and deployed_sha == pinned.sha
    diagnosis = ""
    if not ok:
        diagnosis = _diagnose(
            store,
            request,
            target,
            pinned.sha,
            observed=(
                f"restart: {'ok' if restart_ok else 'FAILED'} ({restart_detail}); "
                f"health: {health_detail}; "
                f"tree is on {deployed_sha}, expected {pinned.sha}"
            ),
        )

    result = DeployResult(
        request_id=request.request_id,
        target=request.target,
        ok=ok,
        status=STATUS_SUCCEEDED if ok else STATUS_FAILED,
        ref=pinned.ref,
        is_default_ref=request.is_default_ref,
        requested_sha=pinned.sha,
        deployed_sha=deployed_sha,
        previous_sha=pinned.previous_sha,
        migration_allowed=migration_allowed,
        migration_applied=migration_applied,
        service_state=state,
        services=list(target.services),
        health_ok=health_ok,
        health_detail=health_detail,
        steps=steps.as_dicts(),
        agent_summary=outcome.summary,
        agent_denials=outcome.denials,
        diagnosis=diagnosis,
        error=None if ok else _terminal_error(restart_ok, health_ok, deployed_sha, pinned.sha),
        started_at=request.started_at,
        finished_at=utcnow(),
    )
    return _finish(store, request, result)


# ── the migration gate ───────────────────────────────────────────


def _migration_gate(repo: Path, request: DeployRequest, sha: str) -> tuple[bool, str]:
    """R-2: may this run apply migrations, and why (not)?

    Two conditions, both required. The tree has to be on exactly what
    ``origin/main`` points at -- a migration applied from anywhere else
    forks the revision graph, and the next legitimate deploy inherits
    the fork with no way back except a restore, which throws away
    everything written since. And if a human overrode the ref, they have
    to have said separately that migrations are still in scope: the
    override says "deploy this code", which is recoverable, not "write
    to the database from an unmerged branch", which is not.
    """
    if not pin_mod.matches_remote_main(repo, sha, default_ref=DEPLOY_REF):
        return False, (
            f"the tree is on {sha[:12]}, which is not what {DEPLOY_REF} points at"
        )
    if not request.is_default_ref and not request.override_allows_migration:
        return False, (
            f"ref override to {request.override_ref!r} was approved, but migrations "
            "were not separately approved for it"
        )
    return True, f"the tree is exactly {DEPLOY_REF}"


def _run_alembic(repo: Path, *args: str) -> str | None:
    """Run a read-only alembic command, or ``None`` if not applicable."""
    if not (repo / "alembic.ini").exists():
        return None
    alembic = repo / ".venv" / "bin" / "alembic"
    if not alembic.exists():
        return None
    try:
        proc = subprocess.run(
            [str(alembic), *args],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("Could not run alembic", repo=str(repo), args=args, error=str(exc))
        return None
    if proc.returncode != 0:
        return None
    lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
    return lines[-1] if lines else None


def _alembic_revision(repo: Path) -> str | None:
    """The DB's current migration revision, or ``None`` if not applicable.

    Read before and after the agent phase so that "a migration happened"
    is something the runner *observes* rather than something it trusts
    the agent to have declared. That is what makes the blocked case
    enforceable rather than merely requested.
    """
    return _run_alembic(repo, "current")


def _alembic_id(line: str | None) -> str | None:
    """The revision id out of an alembic line like ``0006 (head)``."""
    if not line:
        return None
    token = line.split()[0].strip()
    return token or None


def _alembic_pending(repo: Path) -> bool | None:
    """Does this commit carry migrations the database has not applied?

    ``None`` when the question does not apply (no alembic here, or the
    revision could not be read) -- the caller treats that as "not
    proven pending" rather than as a refusal, because a target without
    migrations must not be undeployable.
    """
    current = _alembic_id(_alembic_revision(repo))
    head = _alembic_id(_run_alembic(repo, "heads"))
    if current is None or head is None:
        return None
    return current != head


def _migration_applied(before: str | None, after: str | None, outcome) -> bool | None:
    if before is None and after is None:
        # No alembic here at all; fall back to what the agent said,
        # which for a repo without migrations should be false or null.
        return outcome.migration_applied
    return before != after


# ── privileged and observational steps ───────────────────────────


def _run_backup(script: Path, repo: Path) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            ["/bin/bash", str(script)],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=BACKUP_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"backup script did not complete: {exc}"
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode != 0:
        return False, f"exit {proc.returncode}: {output[-600:]}"
    return True, output.splitlines()[-1][:400] if output else "ok"


def _restart(target: DeployTarget) -> tuple[bool, str]:
    details = []
    for unit in target.services:
        try:
            proc = subprocess.run(
                ["sudo", "-n", "systemctl", "restart", unit],
                capture_output=True,
                text=True,
                timeout=RESTART_TIMEOUT_S,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"{unit}: restart did not complete ({exc})"
        if proc.returncode != 0:
            return False, f"{unit}: exit {proc.returncode}: {(proc.stderr or '').strip()[:400]}"
        details.append(f"{unit}: restarted")
    return True, "; ".join(details)


def _is_active(unit: str) -> bool:
    try:
        proc = subprocess.run(
            ["systemctl", "is-active", unit], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return (proc.stdout or "").strip() == "active"


def _observe_service(
    target: DeployTarget, steps: _Steps, *, restarted: bool
) -> tuple[str, bool | None, str]:
    """What is serving right now, and is it answering (R-7).

    The distinction that matters to whoever reads this at 2am is not
    "did the deploy succeed" but "is anything up". A failed deploy that
    never restarted leaves the previous process running -- note the
    working tree is nevertheless already on the new commit, because on
    this host the tree *is* production, so templates and other files
    read at request time have already changed while Python has not.
    """
    active = all(_is_active(unit) for unit in target.services)

    health_ok: bool | None = None
    health_detail = "target declares no health endpoint"
    if target.health_url:
        health_ok, health_detail = _poll_health(
            target.health_url,
            grace_s=target.health_grace_s if restarted else 0.0,
        )

    if not active:
        state = SERVICE_DOWN
    elif not restarted:
        state = SERVICE_UP_PREVIOUS
        health_detail += (
            " -- note the working tree is already on the new commit, so any file "
            "read per-request (templates, static assets) is new while the running "
            "Python is not"
        )
    elif health_ok is True:
        state = SERVICE_UP_NEW
    else:
        state = SERVICE_UP_UNKNOWN_VERSION

    steps.record(
        "verify",
        state in (SERVICE_UP_NEW, SERVICE_UP_PREVIOUS),
        f"{state}: {health_detail}",
    )
    return state, health_ok, health_detail


def _poll_health(url: str, *, grace_s: float) -> tuple[bool, str]:
    deadline = time.monotonic() + max(grace_s, 0.0)
    last = ""
    while True:
        try:
            response = httpx.get(url, timeout=10.0)
            if response.status_code < 400:
                return True, f"{url} -> {response.status_code}"
            last = f"{url} -> {response.status_code}"
        except httpx.HTTPError as exc:
            last = f"{url} -> {type(exc).__name__}: {exc}"
        if time.monotonic() >= deadline:
            return False, last
        time.sleep(HEALTH_POLL_INTERVAL_S)


def _safe_head(repo: Path) -> str | None:
    try:
        return pin_mod.head_sha(repo)
    except pin_mod.PinRefusedError:
        return None


def _diagnose(
    store: DeployStore,
    request: DeployRequest,
    target: DeployTarget,
    sha: str,
    *,
    observed: str,
) -> str:
    """Ask a read-only agent what went wrong. Advisory only."""
    try:
        outcome = agent_mod.run_agent(
            target=target,
            scratch=store.root / "runs" / request.request_id,
            unit=f"magickit-deploy-diagnose-{request.request_id}",
            brief=agent_mod.render_diagnose_brief(target=target, sha=sha, observed=observed),
            migration_allowed=False,
            read_only=True,
            timeout_s=min(agent_mod.AGENT_TIMEOUT_S, 420.0),
        )
    except Exception as exc:  # diagnosis must never mask the real failure
        logger.warning("Diagnosis agent failed", error=str(exc))
        return ""
    return outcome.summary or (outcome.error or "")


def _terminal_error(
    restart_ok: bool, health_ok: bool | None, deployed_sha: str | None, wanted_sha: str
) -> str:
    if not restart_ok:
        return "the restart failed; see the restart step"
    if deployed_sha != wanted_sha:
        return (
            f"the tree is on {deployed_sha} but {wanted_sha} was approved; "
            "something moved it during the deploy"
        )
    return "the service restarted but did not become healthy within its grace period"


# ── persistence ──────────────────────────────────────────────────


def _fail(
    store: DeployStore,
    request: DeployRequest,
    steps: _Steps,
    *,
    error: str,
    service_state: str,
    health_ok: bool | None = None,
    health_detail: str = "",
    deployed_sha: str | None = None,
    previous_sha: str | None = None,
    pinned=None,
    outcome=None,
    migration_allowed: bool = False,
    migration_applied: bool | None = None,
) -> DeployResult:
    result = DeployResult(
        request_id=request.request_id,
        target=request.target,
        ok=False,
        status=STATUS_FAILED,
        ref=pinned.ref if pinned else request.ref,
        is_default_ref=request.is_default_ref,
        requested_sha=pinned.sha if pinned else None,
        deployed_sha=deployed_sha,
        previous_sha=previous_sha,
        migration_allowed=migration_allowed,
        migration_applied=migration_applied,
        service_state=service_state,
        services=[],
        health_ok=health_ok,
        health_detail=health_detail,
        steps=steps.as_dicts(),
        agent_summary=outcome.summary if outcome else "",
        agent_denials=outcome.denials if outcome else [],
        error=error,
        started_at=request.started_at,
        finished_at=utcnow(),
    )
    return _finish(store, request, result)


def _finish(store: DeployStore, request: DeployRequest, result: DeployResult) -> DeployResult:
    request.status = result.status
    request.finished_at = result.finished_at
    request.result = result.to_dict()
    store.save(request)
    store.audit(
        "finished",
        request_id=request.request_id,
        target=request.target,
        ok=result.ok,
        status=result.status,
        ref=result.ref,
        deployed_sha=result.deployed_sha,
        previous_sha=result.previous_sha,
        service_state=result.service_state,
        health_ok=result.health_ok,
        migration_applied=result.migration_applied,
        agent_denials=result.agent_denials,
        error=result.error,
    )
    logger.info(
        "Deploy finished",
        request_id=request.request_id,
        target=request.target,
        ok=result.ok,
        service_state=result.service_state,
    )
    return result


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: python -m magickit.deploy.runner <request_id>", file=sys.stderr)
        return 2
    result = run(args[0])
    print(result.status)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
