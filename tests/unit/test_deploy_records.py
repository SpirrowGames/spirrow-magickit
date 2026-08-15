"""R-8 and R-9: the audit trail, and one deploy per target at a time.

The lock tests use a real second process rather than a second lock
object, because ``flock`` is per-open-file-description and a same-process
test can pass while the property that matters -- two runners cannot pin
the same tree -- does not hold.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap

import pytest

from magickit.deploy import records
from magickit.deploy.records import (
    STATUS_INTERRUPTED,
    STATUS_RUNNING,
    DeployLockedError,
    DeployStore,
)


@pytest.fixture
def store(tmp_path) -> DeployStore:
    return DeployStore(tmp_path)


# ── where state lives ────────────────────────────────────────────


def test_the_state_root_is_absolute(monkeypatch):
    """The bug this pins made *every* deploy fail.

    The state root becomes the agent's scratch directory, which is
    handed to systemd as `ReadWritePaths`. A relative path there is not
    merely ignored -- measured on this host, the transient unit refuses
    to start: `Failed to start transient service unit: Invalid
    ReadWritePaths`. The symptom was "the agent wrote no report", three
    layers from the cause, and every unit test missed it because they
    all pass `tmp_path`, which is absolute.
    """
    monkeypatch.delenv("MAGICKIT_DEPLOY_STATE_DIR", raising=False)
    assert records.default_state_root().is_absolute()


def test_a_relative_override_is_still_made_absolute(monkeypatch):
    monkeypatch.setenv("MAGICKIT_DEPLOY_STATE_DIR", "data/deploy")
    assert records.default_state_root().is_absolute()


def test_the_store_and_everything_under_it_is_absolute(monkeypatch):
    monkeypatch.delenv("MAGICKIT_DEPLOY_STATE_DIR", raising=False)
    store = records.get_store()

    for path in (store.root, store.requests_dir, store.locks_dir, store.audit_path):
        assert path.is_absolute(), path


# ── requests ─────────────────────────────────────────────────────


def test_a_new_request_is_pending_and_audited(store):
    request = store.create(target="spirrow-conclair", requested_by="loop", reason="because")

    assert request.status == records.STATUS_PENDING
    assert request.approved_by is None
    assert store.load(request.request_id).reason == "because"

    events = store.read_audit()
    assert [e["event"] for e in events] == ["requested"]
    assert events[0]["actor"] == "loop"


def test_the_default_ref_is_what_an_unmodified_request_deploys(store):
    request = store.create(target="spirrow-conclair", requested_by="loop", reason="r")
    assert request.ref == "origin/main"
    assert request.is_default_ref is True

    request.override_ref = "feat/x"
    assert request.ref == "feat/x"
    assert request.is_default_ref is False


def test_a_request_id_cannot_walk_out_of_the_requests_directory(store):
    for attempt in ("../../etc/passwd", "a/b", "..", "a.json"):
        with pytest.raises(KeyError):
            store.load(attempt)


def test_saving_is_atomic_enough_to_never_leave_a_truncated_file(store):
    request = store.create(target="spirrow-conclair", requested_by="loop", reason="r")
    request.status = STATUS_RUNNING
    store.save(request)

    path = store.path_for(request.request_id)
    assert json.loads(path.read_text())["status"] == STATUS_RUNNING
    assert not list(store.requests_dir.glob("*.tmp"))


# ── audit (R-8) ──────────────────────────────────────────────────


def test_the_audit_is_append_only_across_events(store):
    store.audit("requested", request_id="a", target="t")
    store.audit("approved", request_id="a", target="t", actor="Takahito")
    store.audit("finished", request_id="a", target="t", ok=True)

    events = store.read_audit()
    assert [e["event"] for e in events] == ["requested", "approved", "finished"]
    # Every line carries a timestamp; an investigation starts from "when".
    assert all("at" in e for e in events)


def test_audit_can_be_filtered_by_target(store):
    store.audit("requested", request_id="a", target="spirrow-conclair")
    store.audit("requested", request_id="b", target="other")

    assert len(store.read_audit(target="spirrow-conclair")) == 1


def test_an_override_reason_reaches_the_audit(store):
    store.audit(
        "approved",
        request_id="a",
        target="spirrow-conclair",
        override_ref="feat/hotfix",
        override_reason="prod down, fix not merged yet",
    )
    (event,) = store.read_audit()
    assert event["override_reason"] == "prod down, fix not merged yet"


def test_unparseable_audit_lines_do_not_hide_the_rest(store):
    store.audit("requested", request_id="a", target="t")
    with store.audit_path.open("a") as fh:
        fh.write("{ this is not json\n")
    store.audit("finished", request_id="a", target="t")

    assert [e["event"] for e in store.read_audit()] == ["requested", "finished"]


# ── the lock (R-9) ───────────────────────────────────────────────

_HOLDER = textwrap.dedent(
    """
    import sys, time
    sys.path.insert(0, {src!r})
    from magickit.deploy.records import DeployStore
    store = DeployStore({root!r})
    with store.target_lock("spirrow-conclair"):
        print("held", flush=True)
        time.sleep(30)
    """
)


def test_a_second_process_cannot_deploy_the_same_target(store, tmp_path):
    src = str(__import__("pathlib").Path(__file__).resolve().parents[2] / "src")
    script = tmp_path / "holder.py"
    script.write_text(_HOLDER.format(src=src, root=str(tmp_path)))

    holder = subprocess.Popen(
        [sys.executable, str(script)], stdout=subprocess.PIPE, text=True
    )
    try:
        assert holder.stdout.readline().strip() == "held"

        with pytest.raises(DeployLockedError):
            with store.target_lock("spirrow-conclair"):
                pass
    finally:
        holder.kill()
        holder.wait(timeout=10)


def test_a_different_target_is_not_blocked(store, tmp_path):
    with store.target_lock("spirrow-conclair"):
        with store.target_lock("something-else"):
            pass


def test_the_lock_is_released_when_the_holder_dies(store, tmp_path):
    """Why "interrupted" needs no timeout: the kernel does the reaping."""
    src = str(__import__("pathlib").Path(__file__).resolve().parents[2] / "src")
    script = tmp_path / "holder.py"
    script.write_text(_HOLDER.format(src=src, root=str(tmp_path)))

    holder = subprocess.Popen(
        [sys.executable, str(script)], stdout=subprocess.PIPE, text=True
    )
    assert holder.stdout.readline().strip() == "held"
    holder.kill()
    holder.wait(timeout=10)

    with store.target_lock("spirrow-conclair"):
        pass


def test_reaping_marks_a_stranded_running_request_interrupted(store):
    request = store.create(target="spirrow-conclair", requested_by="loop", reason="r")
    request.status = STATUS_RUNNING
    store.save(request)

    reaped = store.reap_interrupted("spirrow-conclair")

    assert reaped == [request.request_id]
    reloaded = store.load(request.request_id)
    assert reloaded.status == STATUS_INTERRUPTED
    # R-7: an interrupted deploy must not read as "the old version is fine".
    assert "not recorded" in reloaded.result["error"]
    assert [e["event"] for e in store.read_audit()][-1] == "interrupted"


def test_reaping_also_catches_a_runner_that_died_before_it_started(store):
    """A runner that dies between systemd-run returning and its first
    write never reaches `running`. Reaping only that status left those
    requests in `approved` forever -- and un-retryable, because approval
    happens once."""
    request = store.create(target="spirrow-conclair", requested_by="loop", reason="r")
    request.status = records.STATUS_APPROVED
    store.save(request)

    assert store.reap_interrupted("spirrow-conclair") == [request.request_id]
    assert store.load(request.request_id).status == STATUS_INTERRUPTED


def test_reaping_does_not_reap_the_caller_itself(store):
    """The runner is legitimately `approved` at the moment it takes the lock."""
    mine = store.create(target="spirrow-conclair", requested_by="loop", reason="r")
    mine.status = records.STATUS_APPROVED
    store.save(mine)

    assert store.reap_interrupted("spirrow-conclair", excluding=mine.request_id) == []
    assert store.load(mine.request_id).status == records.STATUS_APPROVED


def test_reaping_leaves_finished_requests_alone(store):
    request = store.create(target="spirrow-conclair", requested_by="loop", reason="r")
    request.status = records.STATUS_SUCCEEDED
    store.save(request)

    assert store.reap_interrupted("spirrow-conclair") == []
    assert store.load(request.request_id).status == records.STATUS_SUCCEEDED


def test_reaping_is_scoped_to_one_target(store):
    mine = store.create(target="spirrow-conclair", requested_by="loop", reason="r")
    mine.status = STATUS_RUNNING
    store.save(mine)
    theirs = store.create(target="other", requested_by="loop", reason="r")
    theirs.status = STATUS_RUNNING
    store.save(theirs)

    store.reap_interrupted("spirrow-conclair")

    assert store.load(theirs.request_id).status == STATUS_RUNNING
