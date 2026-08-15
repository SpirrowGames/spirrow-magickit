"""R-3: requesting and approving are different doors.

The load-bearing test here is the first one. `deploy_approve` is not
registered at all on the unauthenticated instance -- not registered and
refusing, which would still be a tool an automated caller could find and
retry against. The loop's tool list simply does not contain it.

Everything else pins the other half of R-3: filing a request must be
inert. If `deploy_request` ever started the runner directly, every test
below would still pass except the one that watches the launcher.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from magickit.config import Settings
from magickit.deploy import approval, records
from magickit.mcp.tools import deploy as deploy_tools


def _capture(settings: Settings, *, allow_approval: bool) -> dict[str, Any]:
    registered: dict[str, Any] = {}

    def fake_tool(*args: Any, **kwargs: Any):
        def decorator(fn):
            registered[fn.__name__] = fn
            return fn

        return decorator

    mock_mcp = MagicMock()
    mock_mcp.tool = fake_tool
    deploy_tools.register_tools(mock_mcp, settings, allow_approval=allow_approval)
    return registered


@pytest.fixture(autouse=True)
def state_root(tmp_path, monkeypatch):
    monkeypatch.setattr(records, "default_state_root", lambda: tmp_path)
    return tmp_path


@pytest.fixture
def launch(monkeypatch) -> MagicMock:
    """Stand in for the systemd launch, and count the calls."""
    stub = MagicMock(return_value=(True, "magickit-deploy-abc"))
    monkeypatch.setattr(approval.launcher, "launch", stub)
    return stub


@pytest.fixture
def loop_tools() -> dict[str, Any]:
    return _capture(Settings(), allow_approval=False)


@pytest.fixture
def human_tools() -> dict[str, Any]:
    return _capture(Settings(), allow_approval=True)


# ── the split surface ────────────────────────────────────────────


def test_the_unauthenticated_instance_has_no_approval_tool(loop_tools):
    assert "deploy_approve" not in loop_tools
    # ...while everything harmless is still there, so the loop can ask
    # and can watch.
    assert {"deploy_request", "deploy_status", "deploy_history", "deploy_targets"} <= set(
        loop_tools
    )


def test_the_authenticated_instance_has_it(human_tools):
    assert "deploy_approve" in human_tools


def test_mcp_server_gates_approval_on_the_same_flag_as_auth(monkeypatch):
    """The two must not be able to drift apart.

    Read from the source rather than by importing: ``mcp_server`` builds
    the whole server at import time (and demands OAuth credentials when
    auth is on), so importing it here would test the environment more
    than the wiring. What matters is the shape of the call -- an
    instance that skipped OAuth but still registered ``deploy_approve``
    would put a service restart behind an open door.
    """
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[2] / "src" / "magickit" / "mcp_server.py"
    ).read_text(encoding="utf-8")

    assert "deploy.register_tools(mcp, settings, allow_approval=not auth_disabled())" in source
    # ...and the auth provider asks the same question, not its own copy
    # of the environment lookup.
    assert "if auth_disabled():" in source
    assert source.count('os.environ.get("MAGICKIT_AUTH_DISABLED")') == 1


# ── requesting is inert ──────────────────────────────────────────


async def test_requesting_does_not_deploy_anything(loop_tools, launch):
    result = await loop_tools["deploy_request"](
        target="spirrow-conclair", requested_by="loop", reason="conclair#10 is merged"
    )

    assert result["ok"] is True
    assert result["status"] == records.STATUS_PENDING
    assert result["ref"] == "origin/main"
    launch.assert_not_called()


async def test_requesting_magickit_is_refused(loop_tools, launch):
    result = await loop_tools["deploy_request"](
        target="spirrow-magickit", requested_by="loop", reason="ship it"
    )

    assert result["ok"] is False
    assert result["error_type"] == "self_deploy_refused"
    launch.assert_not_called()


async def test_requesting_an_unknown_target_is_refused(loop_tools):
    result = await loop_tools["deploy_request"](
        target="spirrow-voxelworld", requested_by="loop", reason="r"
    )
    assert result["error_type"] == "target_not_allowed"
    assert "spirrow-conclair" in result["allowed"]


async def test_a_request_without_a_reason_is_refused(loop_tools):
    result = await loop_tools["deploy_request"](
        target="spirrow-conclair", requested_by="loop", reason="   "
    )
    assert result["error_type"] == "reason_required"


# ── approving is the deploy ──────────────────────────────────────


async def test_approval_starts_the_runner_and_records_who(human_tools, launch, state_root):
    created = await human_tools["deploy_request"](
        target="spirrow-conclair", requested_by="loop", reason="conclair#10"
    )
    approved = await human_tools["deploy_approve"](
        request_id=created["request_id"], approved_by="Takahito", note="read the diff"
    )

    assert approved["ok"] is True
    assert approved["status"] == records.STATUS_RUNNING
    launch.assert_called_once_with(created["request_id"])

    stored = records.DeployStore(state_root).load(created["request_id"])
    assert stored.approved_by == "Takahito"
    assert stored.approval_note == "read the diff"
    assert stored.override_ref is None

    events = {e["event"] for e in records.DeployStore(state_root).read_audit()}
    assert {"requested", "approved"} <= events


async def test_a_request_can_only_be_approved_once(human_tools, launch):
    created = await human_tools["deploy_request"](
        target="spirrow-conclair", requested_by="loop", reason="r"
    )
    await human_tools["deploy_approve"](request_id=created["request_id"], approved_by="T")
    again = await human_tools["deploy_approve"](request_id=created["request_id"], approved_by="T")

    assert again["error_type"] == "not_pending"
    assert launch.call_count == 1


async def test_a_failed_launch_is_reported_as_a_failure_not_a_start(human_tools, monkeypatch):
    monkeypatch.setattr(
        approval.launcher, "launch", MagicMock(return_value=(False, "systemd said no"))
    )
    created = await human_tools["deploy_request"](
        target="spirrow-conclair", requested_by="loop", reason="r"
    )

    result = await human_tools["deploy_approve"](
        request_id=created["request_id"], approved_by="T"
    )

    assert result["ok"] is False
    assert result["error_type"] == "launch_failed"
    assert "Nothing was deployed" in result["message"]

    status = await human_tools["deploy_status"](request_id=created["request_id"])
    assert status["request"]["status"] == records.STATUS_FAILED


async def test_the_runner_unit_is_recorded_before_the_launch(human_tools, monkeypatch, state_root):
    """After `launch()` returns, the runner owns the request file.

    Writing a copy taken *before* the launch raced the runner's own
    write, and whichever side lost had its fields silently dropped --
    either `runner_unit`, or `status` and `started_at`.
    """
    seen: dict[str, object] = {}

    def spy(request_id: str):
        stored = records.DeployStore(state_root).load(request_id)
        seen["runner_unit"] = stored.runner_unit
        return True, approval.launcher.unit_name(request_id)

    monkeypatch.setattr(approval.launcher, "launch", spy)

    created = await human_tools["deploy_request"](
        target="spirrow-conclair", requested_by="loop", reason="r"
    )
    await human_tools["deploy_approve"](request_id=created["request_id"], approved_by="T")

    assert seen["runner_unit"] == f"magickit-deploy-{created['request_id']}"


# ── the human override (R-1) ─────────────────────────────────────


async def test_asking_for_the_default_ref_explicitly_is_not_an_override(
    human_tools, launch, state_root
):
    """Left as an override, such a run was pinned like a normal deploy
    (on the branch) while its migration gate treated it as an override."""
    created = await human_tools["deploy_request"](
        target="spirrow-conclair", requested_by="loop", reason="r"
    )
    result = await human_tools["deploy_approve"](
        request_id=created["request_id"], approved_by="T", override_ref="origin/main"
    )

    assert result["ok"] is True
    stored = records.DeployStore(state_root).load(created["request_id"])
    assert stored.override_ref is None
    assert stored.is_default_ref is True


async def test_an_override_is_recorded_with_its_reason(human_tools, launch, state_root):
    created = await human_tools["deploy_request"](
        target="spirrow-conclair", requested_by="loop", reason="r"
    )
    result = await human_tools["deploy_approve"](
        request_id=created["request_id"],
        approved_by="Takahito",
        override_ref="fix/hotfix",
        override_reason="prod is down and the fix is not merged yet",
    )

    assert result["ok"] is True
    assert result["ref"] == "fix/hotfix"

    audit = records.DeployStore(state_root).read_audit()
    approved = [e for e in audit if e["event"] == "approved"][0]
    assert approved["override_ref"] == "fix/hotfix"
    assert "not merged" in approved["override_reason"]
    # R-2: overriding the ref does not by itself unlock migrations.
    assert approved["override_allows_migration"] is False


async def test_an_override_without_a_reason_is_refused(human_tools, launch):
    created = await human_tools["deploy_request"](
        target="spirrow-conclair", requested_by="loop", reason="r"
    )
    result = await human_tools["deploy_approve"](
        request_id=created["request_id"], approved_by="T", override_ref="fix/x"
    )

    assert result["error_type"] == "override_reason_required"
    launch.assert_not_called()


async def test_unlocking_migrations_requires_an_override_to_unlock(human_tools, launch):
    created = await human_tools["deploy_request"](
        target="spirrow-conclair", requested_by="loop", reason="r"
    )
    result = await human_tools["deploy_approve"](
        request_id=created["request_id"],
        approved_by="T",
        override_allows_migration=True,
    )

    assert result["error_type"] == "override_migration_without_override"
    launch.assert_not_called()


# ── rollback ─────────────────────────────────────────────────────


async def _finished_deploy(tools, state_root, launch, **result_fields):
    created = await tools["deploy_request"](
        target="spirrow-conclair", requested_by="loop", reason="r"
    )
    store = records.DeployStore(state_root)
    request = store.load(created["request_id"])
    request.status = records.STATUS_SUCCEEDED
    request.result = {
        "ok": True,
        "deployed_sha": "a" * 40,
        "previous_sha": "b" * 40,
        "migration_applied": False,
        **result_fields,
    }
    store.save(request)
    return request


async def test_a_rollback_names_a_past_deploy_not_a_commit(loop_tools, state_root, launch):
    """R-1 again: the sha comes out of magickit's own record."""
    original = await _finished_deploy(loop_tools, state_root, launch)

    params = set(__import__("inspect").signature(loop_tools["deploy_rollback"]).parameters)
    assert params == {"request_id", "requested_by", "reason"}

    result = await loop_tools["deploy_rollback"](
        request_id=original.request_id, requested_by="loop", reason="listing order is wrong"
    )

    assert result["ok"] is True
    assert result["rollback_to_sha"] == "b" * 40
    assert result["status"] == records.STATUS_PENDING
    # Filed only -- a human still has to approve it.
    launch.assert_not_called()


async def test_a_rollback_is_refused_when_the_deploy_applied_a_migration(
    loop_tools, state_root, launch
):
    original = await _finished_deploy(loop_tools, state_root, launch, migration_applied=True)

    result = await loop_tools["deploy_rollback"](
        request_id=original.request_id, requested_by="loop", reason="wrong"
    )

    assert result["ok"] is False
    assert result["error_type"] == "migration_applied"
    assert "database would be ahead of the code" in result["message"]


async def test_a_deploy_that_changed_nothing_has_nothing_to_roll_back(
    loop_tools, state_root, launch
):
    created = await loop_tools["deploy_request"](
        target="spirrow-conclair", requested_by="loop", reason="r"
    )
    store = records.DeployStore(state_root)
    request = store.load(created["request_id"])
    request.status = records.STATUS_FAILED
    request.result = {"ok": False, "error": "could not pin"}
    store.save(request)

    result = await loop_tools["deploy_rollback"](
        request_id=created["request_id"], requested_by="loop", reason="undo"
    )

    assert result["error_type"] == "not_rollbackable"
    assert "nothing was changed" in result["message"]


async def test_a_rollback_cannot_be_approved_with_an_override(
    human_tools, state_root, launch
):
    original = await _finished_deploy(human_tools, state_root, launch)
    rollback = await human_tools["deploy_rollback"](
        request_id=original.request_id, requested_by="loop", reason="wrong"
    )

    result = await human_tools["deploy_approve"](
        request_id=rollback["request_id"],
        approved_by="T",
        override_ref="fix/x",
        override_reason="because",
    )

    assert result["error_type"] == "override_on_rollback"
    launch.assert_not_called()


async def test_the_rollback_is_available_to_the_loop_and_still_needs_approval(loop_tools):
    assert "deploy_rollback" in loop_tools
    assert "deploy_approve" not in loop_tools


# ── reading back (R-6, R-8) ──────────────────────────────────────


async def test_status_of_an_unknown_request_says_so(loop_tools):
    result = await loop_tools["deploy_status"](request_id="deadbeef")
    assert result["error_type"] == "not_found"


async def test_history_returns_events_and_requests(loop_tools, human_tools, launch):
    created = await human_tools["deploy_request"](
        target="spirrow-conclair", requested_by="loop", reason="r"
    )
    await human_tools["deploy_approve"](request_id=created["request_id"], approved_by="T")

    history = await loop_tools["deploy_history"](limit=50)

    assert history["ok"] is True
    assert [e["event"] for e in history["events"]] == ["requested", "approved"]
    assert history["requests"][0]["request_id"] == created["request_id"]


async def test_targets_lists_the_allowlist_and_never_magickit(loop_tools):
    result = await loop_tools["deploy_targets"]()

    names = [t["name"] for t in result["targets"]]
    assert "spirrow-conclair" in names
    assert "spirrow-magickit" not in names
    assert result["ref"] == "origin/main"
