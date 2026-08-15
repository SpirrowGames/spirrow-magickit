"""Two doors to approval, one set of checks.

The second door is a command on the host rather than an endpoint. It
grants nothing new -- a shell here runs as sgadmin, which has NOPASSWD
sudo and can write the request file directly, or skip the mechanism
altogether -- so the thing it changes is not what is possible but what
is *recorded*. An untracked bypass becomes an audited action that says
which door it came through.

What must not drift is the checks. R-1's "a ref override needs a
reason" and R-2's "migrations need separate consent" are enforced at
approval time, and a second copy of them for the second door would
eventually disagree with the first. So both call one function, and the
tests below run the same table against both.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from magickit.deploy import approval, records
from magickit.deploy.approval import VIA_HOST, VIA_MCP, approve_request


@pytest.fixture
def store(tmp_path) -> records.DeployStore:
    return records.DeployStore(tmp_path)


@pytest.fixture(autouse=True)
def launch(monkeypatch) -> MagicMock:
    stub = MagicMock(return_value=(True, "magickit-deploy-abc"))
    monkeypatch.setattr(approval.launcher, "launch", stub)
    return stub


def _pending(store, **fields) -> records.DeployRequest:
    request = store.create(target="spirrow-conclair", requested_by="loop", reason="r")
    for key, value in fields.items():
        setattr(request, key, value)
    store.save(request)
    return request


# ── the checks, identical through either door ────────────────────


@pytest.mark.parametrize("via", [VIA_MCP, VIA_HOST])
def test_a_ref_override_needs_a_reason_through_either_door(store, via, launch):
    request = _pending(store)

    result = approve_request(
        store=store, request_id=request.request_id, approved_by="T", via=via,
        override_ref="fix/x",
    )

    assert result["error_type"] == "override_reason_required"
    launch.assert_not_called()


@pytest.mark.parametrize("via", [VIA_MCP, VIA_HOST])
def test_migrations_need_an_override_to_unlock_through_either_door(store, via, launch):
    request = _pending(store)

    result = approve_request(
        store=store, request_id=request.request_id, approved_by="T", via=via,
        override_allows_migration=True,
    )

    assert result["error_type"] == "override_migration_without_override"
    launch.assert_not_called()


@pytest.mark.parametrize("via", [VIA_MCP, VIA_HOST])
def test_a_rollback_refuses_an_override_through_either_door(store, via, launch):
    request = _pending(store, rollback_of="abc", rollback_to_sha="d" * 40)

    result = approve_request(
        store=store, request_id=request.request_id, approved_by="T", via=via,
        override_ref="fix/x", override_reason="because",
    )

    assert result["error_type"] == "override_on_rollback"
    launch.assert_not_called()


@pytest.mark.parametrize("via", [VIA_MCP, VIA_HOST])
def test_a_request_is_approved_once_through_either_door(store, via, launch):
    request = _pending(store)
    approve_request(store=store, request_id=request.request_id, approved_by="T", via=via)

    again = approve_request(
        store=store, request_id=request.request_id, approved_by="T", via=via
    )

    assert again["error_type"] == "not_pending"
    assert launch.call_count == 1


@pytest.mark.parametrize("via", [VIA_MCP, VIA_HOST])
def test_asking_for_the_default_ref_is_not_an_override_through_either_door(store, via):
    request = _pending(store)

    result = approve_request(
        store=store, request_id=request.request_id, approved_by="T", via=via,
        override_ref="origin/main",
    )

    assert result["ok"] is True
    assert store.load(request.request_id).override_ref is None


# ── the door is recorded ─────────────────────────────────────────


def test_the_audit_says_which_door_the_approval_came_through(store):
    request = _pending(store)

    approve_request(
        store=store, request_id=request.request_id, approved_by="Takahito", via=VIA_HOST
    )

    approved = [e for e in store.read_audit() if e["event"] == "approved"][-1]
    assert approved["via"] == VIA_HOST
    assert approved["actor"] == "Takahito"
    assert store.load(request.request_id).approved_via == VIA_HOST


def test_the_mcp_door_is_recorded_distinctly(store):
    request = _pending(store)

    result = approve_request(
        store=store, request_id=request.request_id, approved_by="Takahito", via=VIA_MCP
    )

    assert result["approved_via"] == VIA_MCP
    assert store.load(request.request_id).approved_via == VIA_MCP


def test_the_two_doors_are_not_the_same_string():
    """Otherwise the record answers "who" but not "how"."""
    assert VIA_MCP != VIA_HOST


# ── the command-line front end ───────────────────────────────────


def test_the_cli_approves_and_reports_json(store, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(records, "default_state_root", lambda: tmp_path)
    request = _pending(store)

    exit_code = approval.main([request.request_id, "--by", "Takahito", "--note", "read it"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["approved_via"] == VIA_HOST

    stored = store.load(request.request_id)
    assert stored.approved_by == "Takahito"
    assert stored.approval_note == "read it"


def test_the_cli_exits_nonzero_on_refusal(store, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(records, "default_state_root", lambda: tmp_path)

    exit_code = approval.main(["deadbeef", "--by", "Takahito"])

    assert exit_code == 1
    assert json.loads(capsys.readouterr().out)["error_type"] == "not_found"


def test_the_cli_requires_an_approver(store, tmp_path, monkeypatch):
    monkeypatch.setattr(records, "default_state_root", lambda: tmp_path)
    request = _pending(store)

    with pytest.raises(SystemExit):
        approval.main([request.request_id])


def test_the_cli_carries_the_override_flags_through_the_same_checks(
    store, tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(records, "default_state_root", lambda: tmp_path)
    request = _pending(store)

    exit_code = approval.main([request.request_id, "--by", "T", "--override-ref", "fix/x"])

    assert exit_code == 1
    assert json.loads(capsys.readouterr().out)["error_type"] == "override_reason_required"
