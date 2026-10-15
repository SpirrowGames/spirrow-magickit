"""D12: executor switch reports in deploy_history (ADR-2026-10-09-23).

Pinned here:

- INV-0. With ``executor_pool_enabled`` off, the switch tool is absent and
  ``deploy_history`` returns exactly what it returned before: no ``kind``,
  no ``first_switch``, and ``target=`` does not match executor names.
- The check order: name form, then the tag, then the four-case table.
- Every refusal writes nothing to the audit log.
- ``from_tag`` is derived and null only on an accepted first switch.
- The read-previous/append section runs under the deploy store's lock, per
  executor; a held lock refuses with ``switch_in_progress``.

GitHub is replaced by :class:`FakeApi`; the store is rooted in tmp_path.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock

import pytest

from magickit.config import Settings
from magickit.deploy import executor_switch, records
from magickit.deploy.executor_tags import ApiResponse, ExecutorTagError
from magickit.mcp.tools import deploy as deploy_tools
from magickit.mcp.tools import executor as executor_tools

REPO = "SpirrowGames/spirrow-mindwire"
AT = "2026-10-16T09:00:00+00:00"


class FakeApi:
    """``get_ref`` only: the switch report reads one ref and writes nothing."""

    def __init__(self, *, status: int = 200, obj_type: str = "tag") -> None:
        self.status = status
        self.obj_type = obj_type
        self.calls: list[tuple] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def get_ref(self, repo, ref):
        self.calls.append(("get_ref", repo, ref))
        body = {"ref": f"refs/{ref}", "object": {"type": self.obj_type, "sha": "c" * 40}}
        return ApiResponse(self.status, body if self.status == 200 else None)

    async def _no_write(self, *a, **k):
        raise AssertionError("a switch report must not write to GitHub")

    compare = create_tag_object = create_ref = _no_write


@pytest.fixture
def store(tmp_path, monkeypatch) -> records.DeployStore:
    """A store whose lock is a recorder: fcntl is POSIX-only and the gate
    also runs on Windows. The real lock is exercised separately below."""
    monkeypatch.setattr(records, "default_state_root", lambda: tmp_path)
    s = records.DeployStore(tmp_path)
    s.locks_taken = []  # type: ignore[attr-defined]

    @contextmanager
    def lock(target):
        s.locks_taken.append(target)  # type: ignore[attr-defined]
        yield

    s.target_lock = lock  # type: ignore[method-assign]
    monkeypatch.setattr(records, "get_store", lambda: s)
    return s


def _report(store, api=None, **overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "repo": REPO,
        "executor": "ue-box-1",
        "to_tag": "executor/v1.2.3",
        "switched_at": AT,
        "reported_by": "mindwire@ue-box-1",
        "reason": "followed the new tag",
    }
    kwargs.update(overrides)
    return asyncio.run(executor_switch.report_executor_switch(api or FakeApi(), store, **kwargs))


def _refusal(store, api=None, **overrides: Any) -> ExecutorTagError:
    with pytest.raises(ExecutorTagError) as info:
        _report(store, api, **overrides)
    return info.value


def _switches(store) -> list[dict[str, Any]]:
    return [r for r in store.read_audit(limit=1000) if r["event"] == "executor_switch"]


def _capture_executor(settings: Settings) -> dict[str, Any]:
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


def _capture_deploy(settings: Settings) -> dict[str, Any]:
    registered: dict[str, Any] = {}

    def fake_tool(*args: Any, **kwargs: Any):
        def decorator(fn):
            registered[fn.__name__] = fn
            return fn

        return decorator

    mcp = MagicMock()
    mcp.tool = fake_tool
    deploy_tools.register_tools(mcp, settings, allow_approval=False)
    return registered


# --- INV-0: setting OFF ----------------------------------------------------


def test_off_switch_tool_is_absent():
    assert "deploy_report_executor_switch" not in _capture_executor(Settings())


def _seed_mixed_log(store) -> dict[str, Any]:
    """A deploy request plus a switch record, as if the pool had been on."""
    request = store.create(target="spirrow-conclair", requested_by="loop", reason="r")
    store.audit(
        "executor_switch",
        executor="ue-box-1",
        from_tag=None,
        to_tag="executor/v1.0.0",
        first_switch=True,
        switched_at=AT,
        reported_by="m",
        actor="m",
        reason="r",
        reservation_id=None,
    )
    return {"request_id": request.request_id}


def test_off_history_is_exactly_the_pre_pool_output(store):
    seeded = _seed_mixed_log(store)
    history = asyncio.run(_capture_deploy(Settings())["deploy_history"](limit=50))

    raw = store.read_audit(limit=50)
    assert history == {
        "ok": True,
        "events": raw,
        "requests": [r.to_dict() for r in store.list_requests(limit=50)],
    }
    for item in history["events"] + history["requests"]:
        assert "kind" not in item
    assert history["requests"][0]["request_id"] == seeded["request_id"]
    # the deploy events carry no first_switch either
    deploy_events = [e for e in history["events"] if e["event"] != "executor_switch"]
    assert all("first_switch" not in e for e in deploy_events)


def test_off_target_filter_does_not_match_executor_names(store):
    _seed_mixed_log(store)
    history = asyncio.run(
        _capture_deploy(Settings())["deploy_history"](limit=50, target="ue-box-1")
    )
    assert history == {"ok": True, "events": [], "requests": []}


def test_off_history_tool_description_is_unchanged():
    """The description is part of the MCP surface the loop sees."""
    doc = _capture_deploy(Settings())["deploy_history"].__doc__
    assert "executor" not in doc


# --- ON: deploy_history ----------------------------------------------------


def test_on_history_marks_kind_and_first_switch(store):
    _seed_mixed_log(store)
    history = asyncio.run(
        _capture_deploy(Settings(executor_pool_enabled=True))["deploy_history"](limit=50)
    )
    kinds = [(e["event"], e["kind"]) for e in history["events"]]
    assert kinds == [("requested", "deploy"), ("executor_switch", "executor_switch")]
    switch = history["events"][1]
    assert switch["first_switch"] is True
    assert "first_switch" not in history["events"][0]
    assert all(r["kind"] == "deploy" for r in history["requests"])


def test_on_target_filter_matches_executor_names(store):
    _seed_mixed_log(store)
    tools = _capture_deploy(Settings(executor_pool_enabled=True))
    by_executor = asyncio.run(tools["deploy_history"](limit=50, target="ue-box-1"))
    assert [e["event"] for e in by_executor["events"]] == ["executor_switch"]
    assert by_executor["requests"] == []
    by_target = asyncio.run(tools["deploy_history"](limit=50, target="spirrow-conclair"))
    assert [e["event"] for e in by_target["events"]] == ["requested"]


# --- the four-case table ---------------------------------------------------


def test_first_switch_accepted_with_null_from_tag(store):
    out = _report(store, first_switch=True)
    assert out["ok"] is True
    assert out["from_tag"] is None and out["first_switch"] is True
    [rec] = _switches(store)
    assert rec["from_tag"] is None and rec["first_switch"] is True
    assert rec["to_tag"] == "executor/v1.2.3"


def test_later_switch_derives_from_tag(store):
    _report(store, first_switch=True, to_tag="executor/v1.0.0")
    out = _report(store, to_tag="executor/v1.1.0")
    assert out["from_tag"] == "executor/v1.0.0" and out["first_switch"] is False
    assert [r["from_tag"] for r in _switches(store)] == [None, "executor/v1.0.0"]


def test_no_prior_record_without_first_switch_refused(store):
    err = _refusal(store)
    assert err.error_type == "no_prior_switch"
    assert "first_switch=true" in err.message
    assert _switches(store) == []


def test_first_switch_with_prior_record_refused(store):
    _report(store, first_switch=True, to_tag="executor/v1.0.0")
    err = _refusal(store, first_switch=True, to_tag="executor/v1.1.0")
    assert err.error_type == "already_bootstrapped"
    assert "executor/v1.0.0" in err.message
    assert len(_switches(store)) == 1


def test_history_is_per_executor(store):
    _report(store, first_switch=True, executor="ue-box-1")
    # another executor has no history of its own yet
    assert _refusal(store, executor="ue-box-2").error_type == "no_prior_switch"
    assert _report(store, first_switch=True, executor="ue-box-2")["from_tag"] is None


# --- what is recorded ------------------------------------------------------


def test_switched_at_and_received_time_are_kept_apart(store):
    out = _report(store, first_switch=True, switched_at="2020-01-01T00:00:00+09:00")
    [rec] = _switches(store)
    assert rec["switched_at"] == "2020-01-01T00:00:00+09:00"
    assert rec["at"] == out["received_at"] != rec["switched_at"]


def test_reservation_id_is_kept_or_null(store):
    _report(store, first_switch=True, executor="a")
    _report(store, first_switch=True, executor="b", reservation_id="rsv-1")
    recs = {r["executor"]: r for r in _switches(store)}
    assert recs["a"]["reservation_id"] is None
    assert recs["b"]["reservation_id"] == "rsv-1"
    assert recs["b"]["reported_by"] == recs["b"]["actor"] == "mindwire@ue-box-1"


def test_records_are_only_appended(store):
    _report(store, first_switch=True, to_tag="executor/v1.0.0")
    before = store.audit_path.read_text(encoding="utf-8")
    _report(store, to_tag="executor/v1.1.0")
    after = store.audit_path.read_text(encoding="utf-8")
    assert after.startswith(before)


def test_no_edit_or_delete_tool_exists():
    names = set(_capture_executor(Settings(executor_pool_enabled=True)))
    assert names == {"executor_tag_create", "deploy_report_executor_switch"}


# --- refusals: nothing written ---------------------------------------------


@pytest.mark.parametrize(
    "name", ["", "ue box", "ue.box", "ue/box", "ue-box\n", "機械", "a]b", None]
)
def test_malformed_executor_name_refused_before_any_call(store, name):
    api = FakeApi()
    err = _refusal(store, api, executor=name, first_switch=True)
    assert err.error_type == "invalid_executor"
    assert api.calls == [] and _switches(store) == []


def test_any_well_formed_name_is_accepted_without_lookup(store):
    """Magickit holds no ledger (D6 §2.2): it checks form, not membership."""
    assert _report(store, executor="Never_Seen-Before_9", first_switch=True)["ok"] is True


def test_name_is_checked_before_the_tag(store):
    api = FakeApi()
    err = _refusal(store, api, executor="bad name", to_tag="nope", first_switch=True)
    assert err.error_type == "invalid_executor"


@pytest.mark.parametrize(
    "tag",
    ["executor/1.2.3", "executor/v01.2.3", "executor/v1.2", "executor/v1.2.3-rc1",
     "v1.2.3", "executor/v1.2.3\n", None],
)
def test_malformed_tag_refused_before_any_call(store, tag):
    api = FakeApi()
    err = _refusal(store, api, to_tag=tag, first_switch=True)
    assert err.error_type == "invalid_tag"
    assert api.calls == [] and _switches(store) == []


@pytest.mark.parametrize(
    "api, error_type",
    [
        (FakeApi(status=404), "tag_not_found"),
        (FakeApi(obj_type="commit"), "tag_not_annotated"),
        (FakeApi(status=500), "github_error"),
    ],
)
def test_tag_that_is_not_an_existing_annotated_tag_refused(store, api, error_type):
    err = _refusal(store, api, first_switch=True)
    assert err.error_type == error_type
    assert api.calls == [("get_ref", REPO, "tags/executor/v1.2.3")]
    assert _switches(store) == []
    assert store.locks_taken == []  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "field, value",
    [
        ("switched_at", "2026-10-16T09:00:00"),  # no offset
        ("switched_at", "yesterday"),
        ("switched_at", ""),
        ("reported_by", " "),
        ("reason", ""),
        ("reservation_id", None),
    ],
)
def test_bad_arguments_refused_before_any_call(store, field, value):
    api = FakeApi()
    err = _refusal(store, api, first_switch=True, **{field: value})
    assert err.error_type == "invalid_argument"
    assert api.calls == [] and _switches(store) == []


# --- the lock --------------------------------------------------------------


def test_read_and_append_run_under_a_per_executor_lock(store):
    _report(store, first_switch=True, executor="ue-box-1")
    _report(store, first_switch=True, executor="ue-box-2")
    assert store.locks_taken == [  # type: ignore[attr-defined]
        "executor-switch@ue-box-1",
        "executor-switch@ue-box-2",
    ]


def test_held_lock_refuses_and_writes_nothing(store):
    @contextmanager
    def busy(target):
        raise records.DeployLockedError("held")
        yield  # pragma: no cover

    store.target_lock = busy  # type: ignore[method-assign]
    err = _refusal(store, first_switch=True)
    assert err.error_type == "switch_in_progress"
    assert _switches(store) == []


@pytest.mark.skipif(importlib.util.find_spec("fcntl") is None, reason="flock is POSIX-only")
def test_real_lock_refuses_a_concurrent_report(tmp_path):
    real = records.DeployStore(tmp_path)
    with real.target_lock("executor-switch@ue-box-1"):
        with pytest.raises(ExecutorTagError) as info:
            asyncio.run(
                executor_switch.report_executor_switch(
                    FakeApi(), real, repo=REPO, executor="ue-box-1",
                    to_tag="executor/v1.2.3", switched_at=AT, reported_by="m",
                    reason="r", first_switch=True,
                )
            )
    assert info.value.error_type == "switch_in_progress"
    assert real.latest_executor_switch("ue-box-1") is None


# --- the MCP tool ----------------------------------------------------------


def _tool(monkeypatch, api):
    made: list[str] = []

    def make(pat_env):
        made.append(pat_env)
        return api

    monkeypatch.setattr(executor_tools, "_make_api", make)
    tools = _capture_executor(Settings(executor_pool_enabled=True))
    return tools["deploy_report_executor_switch"], made


def test_tool_records_and_shows_in_history(monkeypatch, store):
    tool, _ = _tool(monkeypatch, FakeApi())
    out = asyncio.run(
        tool(executor="ue-box-1", to_tag="executor/v1.2.3", switched_at=AT,
             reported_by="m", reason="r", first_switch=True)
    )
    assert out["ok"] is True and out["from_tag"] is None
    history = asyncio.run(
        _capture_deploy(Settings(executor_pool_enabled=True))["deploy_history"](
            target="ue-box-1"
        )
    )
    assert [e["kind"] for e in history["events"]] == ["executor_switch"]


def test_tool_refusal_is_an_error_dict(monkeypatch, store):
    tool, _ = _tool(monkeypatch, FakeApi())
    out = asyncio.run(
        tool(executor="ue-box-1", to_tag="executor/v1.2.3", switched_at=AT,
             reported_by="m", reason="r")
    )
    assert out["ok"] is False and out["error_type"] == "no_prior_switch"


def test_tool_refuses_malformed_input_before_needing_a_token(monkeypatch, store):
    tool, made = _tool(monkeypatch, FakeApi())
    out = asyncio.run(
        tool(executor="bad name", to_tag="executor/v1.2.3", switched_at=AT,
             reported_by="m", reason="r", first_switch=True)
    )
    assert out["error_type"] == "invalid_executor"
    assert made == []


def test_tool_without_token_is_not_configured(monkeypatch, store):
    def no_pat(pat_env):
        raise RuntimeError("PAT not set")

    monkeypatch.setattr(executor_tools, "_make_api", no_pat)
    tool = _capture_executor(Settings(executor_pool_enabled=True))[
        "deploy_report_executor_switch"
    ]
    out = asyncio.run(
        tool(executor="ue-box-1", to_tag="executor/v1.2.3", switched_at=AT,
             reported_by="m", reason="r", first_switch=True)
    )
    assert out["error_type"] == "not_configured"
    assert _switches(store) == []


def test_audit_lines_stay_one_json_object_each(store):
    _report(store, first_switch=True, reason="multi\nline")
    for line in store.audit_path.read_text(encoding="utf-8").splitlines():
        json.loads(line)
