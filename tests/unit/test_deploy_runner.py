"""The runner's orchestration: what happens, in what order, and what it
says afterwards.

Patched at the seams -- git, the agent, the backup script, systemctl,
the health probe -- and nowhere else, so the logic under test is the
real sequencing rather than a rehearsal of it. The properties worth
holding are:

- **R-2**: no migration unless the tree is exactly ``origin/main``, and
  if one happens anyway the deploy fails loudly instead of shipping.
- **R-7**: the result distinguishes "the deploy failed and the old
  version is still serving" from "nothing is serving". A run that
  stopped before the restart must never report the service as new.
- Nothing irreversible happens before the things that can refuse. A
  failed pin, a missing backup script or a failed backup must all leave
  the service unrestarted.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from magickit.deploy import records, runner
from magickit.deploy.agent import AgentOutcome
from magickit.deploy.pin import PinRefusedError, PinResult
from magickit.deploy.records import (
    SERVICE_DOWN,
    SERVICE_UP_NEW,
    SERVICE_UP_PREVIOUS,
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    DeployStore,
)
from magickit.deploy.registry import DeployTarget

NEW_SHA = "a" * 40
OLD_SHA = "b" * 40


@pytest.fixture
def target(tmp_path) -> DeployTarget:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "backup.sh").write_text("#!/bin/bash\necho ok\n")
    return DeployTarget(
        name="spirrow-conclair",
        repo_path=repo,
        services=("spirrow-conclair.service",),
        health_url="http://127.0.0.1:8115/health",
        backup_script=Path("scripts/backup.sh"),
        health_grace_s=1.0,
    )


@pytest.fixture
def store(tmp_path) -> DeployStore:
    return DeployStore(tmp_path / "state")


@pytest.fixture
def wiring(monkeypatch, target):
    """Every seam, defaulted to a clean successful deploy."""
    monkeypatch.setattr(runner, "resolve_target", lambda name: target)
    monkeypatch.setattr(
        runner.pin_mod,
        "pin",
        MagicMock(
            return_value=PinResult(
                ref="origin/main", sha=NEW_SHA, previous_sha=OLD_SHA, detached=False
            )
        ),
    )
    monkeypatch.setattr(runner.pin_mod, "matches_remote_main", lambda *a, **k: True)
    monkeypatch.setattr(runner, "_alembic_revision", MagicMock(side_effect=["0005", "0006"]))
    monkeypatch.setattr(runner, "_run_backup", MagicMock(return_value=(True, "backup ok")))
    monkeypatch.setattr(
        runner.agent_mod,
        "run_agent",
        MagicMock(return_value=AgentOutcome(ok=True, summary="synced and migrated")),
    )
    monkeypatch.setattr(runner, "_restart", MagicMock(return_value=(True, "restarted")))
    monkeypatch.setattr(runner, "_is_active", lambda unit: True)
    monkeypatch.setattr(runner, "_poll_health", lambda url, grace_s: (True, f"{url} -> 200"))
    monkeypatch.setattr(runner, "_safe_head", lambda repo: NEW_SHA)
    monkeypatch.setattr(runner, "_diagnose", MagicMock(return_value="diagnosis text"))
    return runner


def _approved(store: DeployStore, **overrides):
    request = store.create(target="spirrow-conclair", requested_by="loop", reason="r")
    request.status = records.STATUS_APPROVED
    request.approved_by = "Takahito"
    for key, value in overrides.items():
        setattr(request, key, value)
    store.save(request)
    return request


# ── the happy path ───────────────────────────────────────────────


def test_a_clean_deploy_reports_the_sha_it_actually_landed(store, wiring):
    request = _approved(store)

    result = runner.run(request.request_id, store=store)

    assert result.ok is True
    assert result.status == STATUS_SUCCEEDED
    # R-6: read back from git, not taken from the agent.
    assert result.deployed_sha == NEW_SHA
    assert result.previous_sha == OLD_SHA
    assert result.service_state == SERVICE_UP_NEW
    assert result.health_ok is True
    assert result.migration_allowed is True
    assert result.migration_applied is True

    names = [s["name"] for s in result.steps]
    assert names.index("pin") < names.index("backup") < names.index("agent-prepare")
    assert names.index("agent-prepare") < names.index("restart")


def test_the_result_and_the_audit_agree(store, wiring):
    request = _approved(store)
    result = runner.run(request.request_id, store=store)

    stored = store.load(request.request_id)
    assert stored.status == result.status
    assert stored.result["deployed_sha"] == NEW_SHA

    finished = [e for e in store.read_audit() if e["event"] == "finished"][-1]
    assert finished["ok"] is True
    assert finished["deployed_sha"] == NEW_SHA
    assert finished["service_state"] == SERVICE_UP_NEW


# ── R-2: the migration gate ──────────────────────────────────────


def test_a_tree_that_is_not_origin_main_may_not_migrate(store, wiring, monkeypatch):
    monkeypatch.setattr(runner.pin_mod, "matches_remote_main", lambda *a, **k: False)
    monkeypatch.setattr(runner, "_alembic_revision", MagicMock(return_value="0005"))
    request = _approved(store)

    result = runner.run(request.request_id, store=store)

    assert result.migration_allowed is False
    gate = [s for s in result.steps if s["name"] == "migration-gate"][0]
    assert "blocked" in gate["detail"]
    # The agent is told, *and* the deny rules are tightened for it.
    _, kwargs = wiring.agent_mod.run_agent.call_args
    assert kwargs["migration_allowed"] is False


def test_an_overridden_ref_does_not_unlock_migrations_by_itself(store, wiring, monkeypatch):
    monkeypatch.setattr(runner, "_alembic_revision", MagicMock(return_value="0005"))
    request = _approved(store, override_ref="fix/x", override_reason="prod down")

    result = runner.run(request.request_id, store=store)

    assert result.migration_allowed is False
    gate = [s for s in result.steps if s["name"] == "migration-gate"][0]
    assert "not separately approved" in gate["detail"]


def test_an_overridden_ref_with_explicit_migration_approval_still_needs_to_be_main(
    store, wiring, monkeypatch
):
    """Both conditions, not either."""
    monkeypatch.setattr(runner.pin_mod, "matches_remote_main", lambda *a, **k: False)
    monkeypatch.setattr(runner, "_alembic_revision", MagicMock(return_value="0005"))
    request = _approved(
        store,
        override_ref="fix/x",
        override_reason="prod down",
        override_allows_migration=True,
    )

    result = runner.run(request.request_id, store=store)
    assert result.migration_allowed is False


def test_a_shut_gate_refuses_code_that_has_migrations_waiting(store, wiring, monkeypatch):
    """The restart is the other thing that runs alembic.

    conclair's unit carries `ExecStartPre=alembic upgrade head`, so a
    gate that only denied the *agent* would be walked through by systemd
    at restart. The deploy has to stop before that.
    """
    monkeypatch.setattr(runner.pin_mod, "matches_remote_main", lambda *a, **k: False)
    monkeypatch.setattr(runner, "_alembic_pending", lambda repo: True)
    request = _approved(store)

    result = runner.run(request.request_id, store=store)

    assert result.ok is False
    assert "runs `alembic upgrade head` on start" in result.error
    # Stopped before anything: no backup, no agent, no restart.
    wiring._run_backup.assert_not_called()
    wiring.agent_mod.run_agent.assert_not_called()
    wiring._restart.assert_not_called()


def test_a_shut_gate_still_deploys_code_with_no_migrations_waiting(store, wiring, monkeypatch):
    """A code-only change to a non-main ref is exactly what an override
    is for; it must not be collateral damage of the rule above."""
    monkeypatch.setattr(runner.pin_mod, "matches_remote_main", lambda *a, **k: False)
    monkeypatch.setattr(runner, "_alembic_pending", lambda repo: False)
    monkeypatch.setattr(runner, "_alembic_revision", MagicMock(return_value="0006"))
    request = _approved(store)

    result = runner.run(request.request_id, store=store)

    assert result.ok is True
    assert result.migration_allowed is False
    wiring._restart.assert_called_once()


def test_a_target_without_alembic_is_not_made_undeployable(store, wiring, monkeypatch):
    """"no alembic here" is a real False, not an unknown."""
    monkeypatch.setattr(runner.pin_mod, "matches_remote_main", lambda *a, **k: False)
    monkeypatch.setattr(runner, "_has_alembic", lambda repo: False)
    monkeypatch.setattr(runner, "_alembic_revision", MagicMock(return_value=None))
    request = _approved(store)

    result = runner.run(request.request_id, store=store)

    assert result.ok is True


def test_an_unreadable_alembic_revision_fails_closed(store, wiring, monkeypatch):
    """The venv is created by the agent's `uv sync`, which runs *after*
    this check -- so "cannot read the revision" is the ordinary state of
    a target being deployed for the first time, and it used to be waved
    through as "not proven pending"."""
    monkeypatch.setattr(runner.pin_mod, "matches_remote_main", lambda *a, **k: False)
    monkeypatch.setattr(runner, "_alembic_pending", lambda repo: None)
    request = _approved(store)

    result = runner.run(request.request_id, store=store)

    assert result.ok is False
    assert "could not be determined" in result.error
    wiring._restart.assert_not_called()
    # R-7: the reader is told which unit is involved.
    assert result.services == ["spirrow-conclair.service"]


def test_pending_is_read_from_current_versus_heads(monkeypatch, tmp_path):
    (tmp_path / "alembic.ini").write_text("[alembic]\n")

    def fake(repo, *args):
        return {("current",): "0005 (head)", ("heads",): "0006 (head)"}[args]

    monkeypatch.setattr(runner, "_run_alembic", fake)
    assert runner._alembic_pending(tmp_path) is True

    monkeypatch.setattr(runner, "_run_alembic", lambda repo, *a: "0006 (head)")
    assert runner._alembic_pending(tmp_path) is False

    # alembic is here but unreadable -> unknown, and the caller refuses.
    monkeypatch.setattr(runner, "_run_alembic", lambda repo, *a: None)
    assert runner._alembic_pending(tmp_path) is None


def test_pending_is_false_when_the_repo_has_no_alembic_at_all(tmp_path):
    assert runner._alembic_pending(tmp_path) is False


def test_an_unreadable_revision_is_not_mistaken_for_a_migration(store, wiring, monkeypatch):
    """`None -> "0006"` is the venv appearing, not a migration running."""
    monkeypatch.setattr(runner, "_alembic_revision", MagicMock(side_effect=[None, "0006"]))
    request = _approved(store)

    result = runner.run(request.request_id, store=store)

    assert result.migration_applied is None
    assert result.ok is True


def test_a_migration_applied_behind_a_shut_gate_fails_the_deploy_loudly(
    store, wiring, monkeypatch
):
    """Detection, because prevention by deny-list is best-effort.

    Nothing was pending when the deploy started -- so the pre-flight
    refusal above does not fire -- and the revision moved anyway. That
    is the case only an after-the-fact read can catch.
    """
    monkeypatch.setattr(runner.pin_mod, "matches_remote_main", lambda *a, **k: False)
    monkeypatch.setattr(runner, "_alembic_pending", lambda repo: False)
    monkeypatch.setattr(runner, "_alembic_revision", MagicMock(side_effect=["0005", "0006"]))
    request = _approved(store)

    result = runner.run(request.request_id, store=store)

    assert result.ok is False
    assert result.migration_applied is True
    assert "containment failure" in result.error
    # And the service was never restarted on top of it.
    wiring._restart.assert_not_called()
    assert result.service_state == SERVICE_UP_PREVIOUS


# ── nothing irreversible before the refusals ─────────────────────


def test_a_refused_pin_stops_before_the_backup_and_the_restart(store, wiring, monkeypatch):
    monkeypatch.setattr(
        runner.pin_mod, "pin", MagicMock(side_effect=PinRefusedError("tree is dirty"))
    )
    request = _approved(store)

    result = runner.run(request.request_id, store=store)

    assert result.ok is False
    assert "could not pin" in result.error
    wiring._run_backup.assert_not_called()
    wiring.agent_mod.run_agent.assert_not_called()
    wiring._restart.assert_not_called()


def test_a_missing_backup_script_stops_the_deploy(store, wiring, target):
    (target.repo_path / "scripts" / "backup.sh").unlink()
    request = _approved(store)

    result = runner.run(request.request_id, store=store)

    assert result.ok is False
    assert "backup script" in result.error
    wiring.agent_mod.run_agent.assert_not_called()
    wiring._restart.assert_not_called()


def test_a_failed_backup_stops_the_deploy(store, wiring, monkeypatch):
    monkeypatch.setattr(runner, "_run_backup", MagicMock(return_value=(False, "pg_dump: no")))
    request = _approved(store)

    result = runner.run(request.request_id, store=store)

    assert result.ok is False
    assert "backup failed" in result.error
    wiring.agent_mod.run_agent.assert_not_called()
    wiring._restart.assert_not_called()


# ── R-7: what is actually serving ────────────────────────────────


def test_an_agent_failure_leaves_the_previous_version_serving_and_says_so(
    store, wiring, monkeypatch
):
    monkeypatch.setattr(
        runner.agent_mod,
        "run_agent",
        MagicMock(return_value=AgentOutcome(ok=False, error="uv sync failed")),
    )
    request = _approved(store)

    result = runner.run(request.request_id, store=store)

    assert result.ok is False
    assert result.service_state == SERVICE_UP_PREVIOUS
    wiring._restart.assert_not_called()
    # The half-state this host actually has: the tree already moved.
    assert "already on the new commit" in result.health_detail


def test_an_undetermined_procedure_is_reported_as_such(store, wiring, monkeypatch):
    """Q-4: stop and say so, with nothing half-done."""
    monkeypatch.setattr(
        runner.agent_mod,
        "run_agent",
        MagicMock(
            return_value=AgentOutcome(
                ok=False, undetermined=True, error="no CLAUDE.md, two lockfiles"
            )
        ),
    )
    request = _approved(store)

    result = runner.run(request.request_id, store=store)

    assert result.ok is False
    assert "could not determine" in result.error
    wiring._restart.assert_not_called()


def test_a_service_that_does_not_come_back_is_reported_as_down(store, wiring, monkeypatch):
    monkeypatch.setattr(runner, "_is_active", lambda unit: False)
    monkeypatch.setattr(runner, "_poll_health", lambda url, grace_s: (False, "connection refused"))
    request = _approved(store)

    result = runner.run(request.request_id, store=store)

    assert result.ok is False
    assert result.service_state == SERVICE_DOWN
    assert result.health_ok is False
    # R-7 again: a failure gets a diagnosis attached, not silence.
    assert result.diagnosis == "diagnosis text"


def test_an_unhealthy_but_running_service_is_not_reported_as_new(store, wiring, monkeypatch):
    monkeypatch.setattr(runner, "_poll_health", lambda url, grace_s: (False, "500"))
    request = _approved(store)

    result = runner.run(request.request_id, store=store)

    assert result.ok is False
    assert result.service_state == records.SERVICE_UP_UNKNOWN_VERSION
    assert "did not become healthy" in result.error


def test_a_failed_restart_is_a_failure_even_if_health_passes(store, wiring, monkeypatch):
    monkeypatch.setattr(runner, "_restart", MagicMock(return_value=(False, "unit not found")))
    request = _approved(store)

    result = runner.run(request.request_id, store=store)

    assert result.ok is False
    assert "restart failed" in result.error


def test_a_tree_that_moved_under_the_deploy_is_caught_on_read_back(store, wiring, monkeypatch):
    monkeypatch.setattr(runner, "_safe_head", lambda repo: "c" * 40)
    request = _approved(store)

    result = runner.run(request.request_id, store=store)

    assert result.ok is False
    assert "something moved it" in result.error


# ── two-slot targets ─────────────────────────────────────────────


@pytest.fixture
def release_target(tmp_path, monkeypatch, target):
    """A target converted to the two-slot layout, `a` live."""
    from dataclasses import replace as _replace

    from magickit.deploy import releases

    root = tmp_path / "releases" / "spirrow-conclair"
    for slot in ("a", "b"):
        (root / slot / "scripts").mkdir(parents=True)
        (root / slot / "scripts" / "backup.sh").write_text("#!/bin/bash\necho ok\n")
    (root / "shared").mkdir()
    (root / "current").symlink_to("a")

    converted = _replace(target, releases_root=root)
    monkeypatch.setattr(runner, "resolve_target", lambda name: converted)
    monkeypatch.setattr(runner, "releases", releases)
    return converted


def test_a_release_deploy_prepares_the_standby_and_leaves_the_live_one_alone(
    store, wiring, release_target, monkeypatch
):
    request = _approved(store)

    result = runner.run(request.request_id, store=store)

    assert result.ok is True
    # Pinned into `b`, not into what is serving.
    assert wiring.pin_mod.pin.call_args[0][0] == release_target.releases_root / "b"
    # ...and the agent was pointed there too, so its sandbox cannot
    # write to the live release.
    agent_target = wiring.agent_mod.run_agent.call_args.kwargs["target"]
    assert agent_target.repo_path == release_target.releases_root / "b"


def test_the_switch_happens_after_the_agent_and_before_the_restart(
    store, wiring, release_target
):
    request = _approved(store)

    result = runner.run(request.request_id, store=store)

    names = [s["name"] for s in result.steps]
    assert names.index("agent-prepare") < names.index("switch") < names.index("restart")
    assert (release_target.releases_root / "current").resolve().name == "b"


def test_a_failure_before_the_switch_leaves_the_live_release_serving(
    store, wiring, release_target, monkeypatch
):
    """The reason this layout needs no automatic rollback: everything
    that can refuse happens in a directory nothing is serving."""
    monkeypatch.setattr(
        runner.agent_mod,
        "run_agent",
        MagicMock(return_value=AgentOutcome(ok=False, error="uv sync failed")),
    )
    request = _approved(store)

    result = runner.run(request.request_id, store=store)

    assert result.ok is False
    assert (release_target.releases_root / "current").resolve().name == "a"
    assert "switch" not in [s["name"] for s in result.steps]
    wiring._restart.assert_not_called()


def test_the_backup_is_taken_with_the_live_release_not_the_new_one(
    store, wiring, release_target, monkeypatch
):
    """Taking the safety net with the unverified code is backwards."""
    seen = {}
    monkeypatch.setattr(
        runner,
        "_run_backup",
        lambda script, repo: (seen.setdefault("script", script), (True, "ok"))[1],
    )
    request = _approved(store)

    runner.run(request.request_id, store=store)

    assert seen["script"] == release_target.releases_root / "a" / "scripts" / "backup.sh"


def test_a_broken_layout_stops_the_deploy_rather_than_guessing(
    store, wiring, release_target
):
    (release_target.releases_root / "current").unlink()
    request = _approved(store)

    result = runner.run(request.request_id, store=store)

    assert result.ok is False
    assert "layout is not usable" in result.error
    wiring.pin_mod.pin.assert_not_called()
    wiring._restart.assert_not_called()


def test_a_rollback_onto_an_already_correct_slot_skips_preparation(
    store, wiring, release_target, monkeypatch
):
    """That directory was serving this exact code, venv and all."""
    old = "d" * 40
    monkeypatch.setattr(
        runner.pin_mod,
        "pin",
        MagicMock(
            return_value=PinResult(ref=old, sha=old, previous_sha=old, detached=True)
        ),
    )
    monkeypatch.setattr(runner, "_safe_head", lambda repo: old)
    monkeypatch.setattr(runner, "_alembic_pending", lambda repo: False)
    monkeypatch.setattr(runner, "_alembic_revision", MagicMock(return_value="0006"))
    request = _approved(store, rollback_of="abc123", rollback_to_sha=old)

    result = runner.run(request.request_id, store=store)

    assert result.ok is True
    wiring.agent_mod.run_agent.assert_not_called()
    assert "skipped preparation" in result.agent_summary
    assert (release_target.releases_root / "current").resolve().name == "b"


def test_an_in_place_target_still_works_the_old_way(store, wiring, target):
    """Targets are converted one at a time; the unconverted ones must
    keep deploying exactly as before."""
    assert target.releases_root is None
    request = _approved(store)

    result = runner.run(request.request_id, store=store)

    assert result.ok is True
    assert wiring.pin_mod.pin.call_args[0][0] == target.repo_path
    assert "switch" not in [s["name"] for s in result.steps]


# ── health, on endpoints whose body never ends ───────────────────


def test_health_reads_the_status_line_and_not_the_body(monkeypatch):
    """Two targets answer on an MCP SSE mount. A plain GET reads the body
    and never returns -- measured: cognilens and prismind both time out
    while perfectly healthy, which would have declared a working service
    dead and failed the deploy."""

    def forbidden_get(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("health must not read the response body")

    class _Response:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(runner.httpx, "get", forbidden_get)
    monkeypatch.setattr(runner.httpx, "stream", lambda *a, **k: _Response())

    ok, detail = runner._poll_health("http://127.0.0.1:8111/sse", grace_s=0.0)

    assert ok is True
    assert "200" in detail


def test_a_slow_health_endpoint_is_given_time(monkeypatch):
    """lexora's /health is documented to stall for 20-30s; a 10s
    per-attempt timeout would turn slow into failed."""
    assert runner.HEALTH_REQUEST_TIMEOUT_S >= 30.0


# ── rollback ─────────────────────────────────────────────────────


def test_a_rollback_pins_the_recorded_commit_and_never_migrates(store, wiring, monkeypatch):
    old = "d" * 40
    monkeypatch.setattr(
        runner.pin_mod,
        "pin",
        MagicMock(
            return_value=PinResult(ref=old, sha=old, previous_sha=NEW_SHA, detached=True)
        ),
    )
    monkeypatch.setattr(runner, "_safe_head", lambda repo: old)
    monkeypatch.setattr(runner, "_alembic_pending", lambda repo: False)
    monkeypatch.setattr(runner, "_alembic_revision", MagicMock(return_value="0006"))
    request = _approved(store, rollback_of="abc123", rollback_to_sha=old)

    result = runner.run(request.request_id, store=store)

    # The ref that was pinned came from the record, not from the caller.
    assert runner.pin_mod.pin.call_args[0][1] == old
    assert result.ok is True
    assert result.deployed_sha == old
    assert result.migration_allowed is False

    gate = [s for s in result.steps if s["name"] == "migration-gate"][0]
    assert "rollback" in gate["detail"]


def test_a_rollback_stops_when_the_database_is_ahead_of_the_old_code(
    store, wiring, monkeypatch
):
    """Rolling code back past a migration that has been applied is not a
    rollback, it is a second incident."""
    old = "d" * 40
    monkeypatch.setattr(runner, "_alembic_pending", lambda repo: True)
    request = _approved(store, rollback_of="abc123", rollback_to_sha=old)

    result = runner.run(request.request_id, store=store)

    assert result.ok is False
    assert "does not match the database's revision" in result.error
    wiring._restart.assert_not_called()


# ── R-3 at the runner, and never staying "running" ───────────────


def test_the_runner_refuses_a_request_nobody_approved(store, wiring):
    """`python -m magickit.deploy.runner <id>` must not be an approval."""
    request = store.create(target="spirrow-conclair", requested_by="loop", reason="r")

    result = runner.run(request.request_id, store=store)

    assert result.ok is False
    assert "was not approved" in result.error
    wiring.pin_mod.pin.assert_not_called()
    wiring._restart.assert_not_called()


def test_an_approved_request_with_no_approver_is_refused(store, wiring):
    request = _approved(store, approved_by="   ")

    result = runner.run(request.request_id, store=store)

    assert result.ok is False
    assert "was not approved" in result.error


def test_an_unexpected_crash_becomes_a_recorded_failure(store, wiring, monkeypatch):
    """Otherwise the request keeps `running` forever with nothing behind it."""
    monkeypatch.setattr(
        runner.pin_mod, "pin", MagicMock(side_effect=RuntimeError("something odd"))
    )
    request = _approved(store)

    result = runner.run(request.request_id, store=store)

    assert result.ok is False
    assert result.status == STATUS_FAILED
    assert "failed unexpectedly" in result.error
    assert store.load(request.request_id).status == STATUS_FAILED
    wiring._restart.assert_not_called()


# ── R-9: concurrency ─────────────────────────────────────────────


def test_a_second_deploy_of_the_same_target_is_refused_not_queued(store, wiring):
    request = _approved(store)

    with store.target_lock("spirrow-conclair"):
        result = runner.run(request.request_id, store=store)

    assert result.ok is False
    assert "in flight" in result.error
    assert "Nothing was changed" in result.error
    wiring._restart.assert_not_called()


def test_taking_the_lock_reaps_a_deploy_whose_runner_died(store, wiring):
    stranded = _approved(store)
    stranded.status = records.STATUS_RUNNING
    store.save(stranded)

    request = _approved(store)
    runner.run(request.request_id, store=store)

    assert store.load(stranded.request_id).status == records.STATUS_INTERRUPTED


def test_an_unknown_target_fails_without_touching_anything(store, wiring, monkeypatch):
    from magickit.deploy.registry import TargetNotAllowedError

    monkeypatch.setattr(
        runner, "resolve_target", MagicMock(side_effect=TargetNotAllowedError("nope"))
    )
    request = _approved(store)

    result = runner.run(request.request_id, store=store)

    assert result.ok is False
    assert result.status == STATUS_FAILED
    wiring.pin_mod.pin.assert_not_called()
