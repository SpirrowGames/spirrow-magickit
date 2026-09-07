"""Unit tests for the 稼働状況 (ops) view.

The page makes exactly one claim -- "this project is / is not moving" --
and everything worth testing is about that claim being right, or being
withheld. The classifier tests pin the precedence between the states, and
the rendering tests pin the two ways the page is allowed to be wrong:
never silently, and never by inventing a default when the data is missing.
"""

from __future__ import annotations

import asyncio
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from magickit.config import Settings
from magickit.main import create_app
from magickit.web import deps, ops
from magickit.web.deps import humanize_age, parse_ts
from tests.route_table import route_table

NOW = datetime(2026, 8, 10, 16, 0, 0, tzinfo=timezone.utc)
STALL = 30 * 60


# ---------------------------------------------------------------------------
# The recording clock.
#
# There are two clocks in this file if the production side reads the wall:
# a ``NOW`` literal that the fixtures anchor to, and ``datetime.now(...)``
# inside ``magickit.web.ops.collect`` (line 362, hit by every HTTP route
# below) and ``magickit.web.deps.age_seconds`` (line 62, reached through the
# ``|ago`` filter in ``partials/ops_table.html``). Fresh CI stayed green for
# exactly as long as both clocks happened to agree; ``test_board.py`` was
# the same fault in the same shape (see msg-343 §1). The fix is to move
# ownership of both production reads to this file so a wall-clock shift
# cannot change what an assertion sees.
#
# The clock is a recorder rather than an exception-thrower: we want to know
# *whether* the seam was read, not to shatter tests that happen to swallow
# exceptions. Every read is stamped with the caller's ``file:line`` -- so
# the reach proof at the end of the module can verify both that the tests
# listed as "reaches" actually reach it (guards against a decoy patch) and
# that no unlisted test does (guards against a hidden time dependency, the
# way 9 tests in ``test_board.py`` sat quietly on the same seam until the
# 10th failed).
#
# ``monkeypatch.setattr`` on the module's ``datetime`` reference is used
# rather than ``unittest.mock.patch`` on ``.now``: the latter injects
# ``unittest/mock.py`` frames between the production call site and this
# recorder, and skipping them here just to work around a needless indirection
# is exactly the "列挙して潰す" shape msg-422 argues against.

_CLOCK_READS: list[tuple[str, int]] = []


class _RecordingClock(datetime):
    """``datetime`` whose ``now()`` returns :data:`NOW` and records the caller.

    Subclassing ``datetime`` keeps ``isinstance(x, datetime)`` checks in
    production code true; the only override is ``now``. Every read appends
    ``(caller file, caller line)`` to :data:`_CLOCK_READS`, which the fixture
    below resets per test and the proof at the end of the module reads back.
    """

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        frame = sys._getframe(1)
        _CLOCK_READS.append((frame.f_code.co_filename, frame.f_lineno))
        if tz is None:
            return NOW.astimezone(timezone.utc).replace(tzinfo=None)
        return NOW.astimezone(tz)


#: Tests that must reach the production wall-clock seam at least once. A
#: test not listed here must reach it zero times. Declared BEFORE the run
#: (in source order that also puts it above the fixture that reads it) so
#: a mismatch is a finding rather than a self-fulfilling classification --
#: see msg-420 §3 for why "declare after the fact" would let a decoy patch
#: silently satisfy the proof.
_REACHES_PRODUCTION_SEAM: frozenset[str] = frozenset(
    {
        # `_get` -> GET /dashboard/_ops -> collect(get_settings()) with no
        # `now=`, so `magickit.web.ops:362` reads. Some also render row
        # timestamps, which reach `magickit.web.deps:62` through `|ago`.
        "test_fragment_renders_the_state_and_the_elapsed_time",
        "test_project_name_is_escaped",
        "test_outage_notice_does_not_read_as_a_verdict",
        "test_labels_match_headers",
        "test_table_opts_into_stacking",
        "test_the_digest_is_escaped_in_the_rendered_page",
        # `_post_control` -> POST .../control -> collect(settings) after the
        # write, same read.
        "test_control_button_sets_desired_and_records_the_actor",
        "test_an_empty_actor_still_writes",
        "test_control_post_returns_the_whole_table",
        "test_a_rejected_control_write_renders_inside_the_table",
        "test_a_control_write_that_raises_still_answers_with_the_page",
        # The sanity test drives the seam on purpose.
        "test_the_recording_clock_reports_the_production_module_as_caller",
    }
)

_RECORDED_READS: dict[str, list[tuple[str, int]]] = {}


@pytest.fixture(autouse=True)
def _own_the_production_wall_clock(monkeypatch, request):
    """Install :class:`_RecordingClock` on the two production modules whose
    clock reads this file exercises.

    Both are patched even when a given test would only reach one of them:
    the reach proof below reads a single per-test bucket, and patching
    both keeps the *unlisted* side honest -- a future edit that starts
    hitting the deps seam from a currently quiet test will be flagged as
    a declaration mismatch rather than absorbed silently.
    """
    monkeypatch.setattr(ops, "datetime", _RecordingClock)
    monkeypatch.setattr(deps, "datetime", _RecordingClock)
    _CLOCK_READS.clear()
    yield
    _RECORDED_READS[request.node.name] = list(_CLOCK_READS)


def _row(**kw) -> ops.ProjectOps:
    row = ops.ProjectOps(project=kw.pop("project", "p"))
    for key, value in kw.items():
        setattr(row, key, value)
    return row


def _classify(**kw) -> ops.ProjectOps:
    row = _row(**kw)
    ops.classify(row, stall_seconds=STALL, now=NOW)
    return row


def _ago(minutes: float) -> datetime:
    return NOW - timedelta(minutes=minutes)


# --- the classifier -------------------------------------------------------


def test_recent_heartbeat_is_running():
    assert _classify(observed_at=_ago(2), configured=True, desired="run").status == (
        "running"
    )


def test_silence_past_the_threshold_is_stalled():
    assert _classify(observed_at=_ago(90), configured=True, desired="run").status == (
        "stalled"
    )


def test_chatroom_activity_counts_as_liveness():
    """A loop that reports rarely is not down while its AIs are talking.

    The heartbeat here is two hours old; the conversation is two minutes
    old. Something is plainly running, and calling that stalled would
    teach the reader to ignore the state.
    """
    row = _classify(
        observed_at=_ago(120), last_activity_at=_ago(2), configured=True, desired="run"
    )

    assert row.status == "running"
    assert row.heartbeat_at == _ago(2)


def test_heartbeat_counts_as_liveness_when_the_room_is_quiet():
    """The converse: a long implementation turn posts nothing for a while."""
    row = _classify(
        observed_at=_ago(3), last_activity_at=_ago(120), configured=True, desired="run"
    )

    assert row.status == "running"


def test_hold_outranks_staleness():
    """A project someone stopped is not a project that died.

    Painting an intentional stop red is how a reader learns to stop
    reading red.
    """
    row = _classify(
        desired="hold", observed_at=_ago(600), configured=True, observed="hold"
    )

    assert row.status == "held"
    assert row.diverged is False


def test_hold_not_yet_read_by_the_loop_is_flagged_as_pending():
    row = _classify(
        desired="hold", observed="run", observed_at=_ago(1), configured=True
    )

    assert row.status == "held"
    assert row.diverged is True


def test_a_failed_control_read_is_unknown_not_running():
    """Callers must treat a failed read as `hold`; the page must not
    upgrade it to a guess in the other direction."""
    row = _classify(control_error="boom", observed_at=_ago(1), last_activity_at=_ago(1))

    assert row.status == "unknown"


def test_a_project_the_loop_never_touched_is_unmanaged_not_stalled():
    """Old scratch projects have no conductor and never will.

    Calling them stalled fills the page with alarms nobody can act on,
    which costs the alarms that matter.
    """
    row = _classify(last_activity_at=_ago(60 * 24 * 30))

    assert row.status == "unmanaged"


def test_holding_a_project_with_no_loop_is_not_pending():
    """There is nothing to catch up: no conductor has ever touched it.

    Left as `diverged`, the row reads 停止 (HOLD) 反映待ち forever, which
    describes a lagging loop rather than an absent one.
    """
    row = _classify(desired="hold", configured=True, observed=None, observed_at=None)

    assert row.status == "held"
    assert row.diverged is False


def test_unconfigured_but_running_project_is_not_diverged():
    """`configured: false` means nobody set a desired value, so there is
    nothing for the loop to catch up to."""
    row = _classify(configured=False, desired="run", observed=None, observed_at=_ago(1))

    assert row.diverged is False


# --- ordering -------------------------------------------------------------


def test_rows_sort_worst_first_then_freshest():
    rows = [
        _classify(project="ok", observed_at=_ago(1), configured=True, desired="run"),
        _classify(project="old-stall", observed_at=_ago(600), configured=True, desired="run"),
        _classify(project="new-stall", observed_at=_ago(40), configured=True, desired="run"),
        _classify(project="quiet", last_activity_at=_ago(9999)),
        _classify(project="broken", control_error="x", observed_at=_ago(1)),
    ]

    rows.sort(key=ops._sort_key)

    assert [r.project for r in rows] == [
        "broken",      # unknown -- we cannot even say
        "new-stall",   # stalled, and recent enough to still act on
        "old-stall",
        "ok",
        "quiet",       # unmanaged, last
    ]


# --- elapsed-time rendering ----------------------------------------------


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (None, "不明"),
        (5, "たった今"),
        (-30, "たった今"),  # clock skew, not a negative age
        (60, "1分前"),
        (29 * 60, "29分前"),
        (60 * 60, "1時間前"),
        (154 * 60, "2時間34分前"),
        (60 * 60 * 24, "1日前"),
        (60 * 60 * 26, "1日2時間前"),
    ],
)
def test_humanize_age(seconds, expected):
    assert humanize_age(seconds) == expected


@pytest.mark.parametrize(
    "value",
    ["2026-08-10T13:40:07.743630Z", "2026-08-10T13:40:07.743630+00:00"],
)
def test_parse_ts_accepts_conclairs_z_suffix(value):
    """Conclair serialises with a trailing Z. Dropping those rows would
    take the heartbeat -- the one field that proves liveness -- with it."""
    parsed = parse_ts(value)

    assert parsed is not None
    assert parsed.tzinfo is not None


def test_parse_ts_reads_naive_timestamps_as_utc():
    """Guessing local time would age a fresh heartbeat by the offset."""
    assert parse_ts("2026-08-10T13:40:07") == datetime(
        2026, 8, 10, 13, 40, 7, tzinfo=timezone.utc
    )


@pytest.mark.parametrize("value", [None, "", "not-a-date", 42])
def test_parse_ts_returns_none_rather_than_raising(value):
    """A field the backend left out must not 500 the whole page."""
    assert parse_ts(value) is None


# --- collect(): degradation ----------------------------------------------


def _adapter(summaries, *, control=None, events=None, digest=None):
    adapter = AsyncMock()
    if isinstance(summaries, BaseException):
        adapter.list_project_summaries.side_effect = summaries
    else:
        adapter.list_project_summaries.return_value = summaries
    adapter.get_loop_control.return_value = control or {
        "desired_state": "run",
        "configured": False,
        "observed_state": None,
        "observed_at": None,
    }
    adapter.list_events.return_value = events or {"items": []}
    # Explicit rather than left to AsyncMock's auto-attribute, so a test that
    # does not care about digests still gets a deterministic "none stored".
    adapter.get_thread_digest.return_value = digest or {
        "present": False,
        "digest": None,
    }
    return adapter


def _event(thread_id="T-1", **kw):
    entry = {
        "thread_id": thread_id,
        "actor": "Heisenberg",
        "action": "post_message",
        "timestamp": NOW.isoformat(),
    }
    entry.update(kw)
    return {"items": [entry]}


def _digest(text="Bohr が X を提案、Einstein が Y を指摘。", **kw):
    body = {
        "digest": text,
        "stale": False,
        "generated_at": NOW.isoformat(),
        "producer": "magickit-digest-sweeper",
    }
    body.update(kw)
    return {"present": True, "digest": body}


def _summary(**kw):
    entry = {
        "project": "p",
        "thread_count": 1,
        "threads_by_status": {"active": 1},
        "gated_thread_count": 0,
        "message_count": 1,
        "last_activity_at": None,
    }
    entry.update(kw)
    return entry


async def _collect(adapter):
    with patch.object(ops, "ChatroomAdapter", return_value=adapter):
        return await ops.collect(Settings(), now=NOW)


@pytest.mark.asyncio
async def test_conclair_outage_is_reported_not_rendered_as_empty():
    context = await _collect(_adapter(httpx.ConnectError("down")))

    assert context["rows"] == []
    assert "接続できません" in context["unavailable"]


@pytest.mark.asyncio
async def test_error_envelope_is_reported():
    context = await _collect(
        _adapter({"error_type": "ChatroomDBError", "error": "pool exhausted"})
    )

    assert "pool exhausted" in context["unavailable"]


@pytest.mark.asyncio
async def test_a_conclair_without_the_summary_endpoint_is_a_deploy_fact():
    """A 404 body is not an empty chatroom."""
    context = await _collect(_adapter({"detail": "Not Found"}))

    assert context["rows"] == []
    assert "spirrow-conclair.service" in context["unavailable"]


@pytest.mark.asyncio
async def test_a_control_read_that_fails_marks_only_that_row():
    adapter = _adapter({"items": [_summary()], "total": 1})
    adapter.get_loop_control.side_effect = httpx.ConnectError("nope")

    context = await _collect(adapter)

    assert context["rows"][0].status == "unknown"


@pytest.mark.asyncio
async def test_a_conclair_without_the_control_endpoint_is_unknown_not_default():
    """A 200 that lacks `desired_state` is a stale Conclair, and fabricating
    the `run` default there is exactly the mistake `GET` never-404ing exists
    to prevent."""
    adapter = _adapter({"items": [_summary()], "total": 1}, control={"detail": "x"})

    context = await _collect(adapter)

    assert context["rows"][0].status == "unknown"


@pytest.mark.asyncio
async def test_a_failing_event_log_does_not_take_the_control_state_with_it():
    adapter = _adapter(
        {"items": [_summary()], "total": 1},
        control={
            "desired_state": "run",
            "configured": True,
            "observed_state": "run",
            "observed_at": NOW.isoformat(),
        },
    )
    adapter.list_events.side_effect = httpx.ConnectError("nope")

    context = await _collect(adapter)
    row = context["rows"][0]

    assert row.status == "running"
    assert row.last_event == {}


@pytest.mark.asyncio
async def test_truncation_reports_what_it_hid():
    adapter = _adapter(
        {"items": [_summary(project=f"p{i}") for i in range(25)], "total": 25}
    )

    context = await _collect(adapter)

    assert len(context["rows"]) == ops.MAX_ROWS
    assert context["hidden"] == 25 - ops.MAX_ROWS


# --- rendering ------------------------------------------------------------


async def _get(path: str, adapter) -> httpx.Response:
    app = create_app()
    with patch.object(ops, "ChatroomAdapter", return_value=adapter):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            return await client.get(path)


@pytest.mark.asyncio
async def test_fragment_renders_the_state_and_the_elapsed_time():
    adapter = _adapter(
        {"items": [_summary(project="spirrow-voxelworld")], "total": 1},
        control={
            "desired_state": "run",
            "configured": True,
            "observed_state": "run",
            "observed_actor": "mindwire-conductor",
            # NOW rather than ``datetime.now(timezone.utc)``: with the
            # production seam pinned to :data:`NOW` by the module fixture,
            # a real-time offset here would race the pin -- ``idle_seconds``
            # would be measured between two different clocks and this test
            # would swing between "2時間" and "たった今" as the wall drifted
            # against the literal.
            "observed_at": (NOW - timedelta(hours=2)).isoformat(),
        },
    )

    body = (await _get("/dashboard/_ops", adapter)).text

    assert "停止疑い" in body
    assert "2時間" in body
    assert "/ui/projects/spirrow-voxelworld/threads" in body


@pytest.mark.asyncio
async def test_project_name_is_escaped():
    adapter = _adapter({"items": [_summary(project="<script>x</script>")], "total": 1})

    body = (await _get("/dashboard/_ops", adapter)).text

    assert "<script>x</script>" not in body
    assert "&lt;script&gt;" in body


@pytest.mark.asyncio
async def test_outage_notice_does_not_read_as_a_verdict():
    """"Cannot tell" must not render as "nothing is running"."""
    body = (await _get("/dashboard/_ops", _adapter(httpx.ConnectError("down")))).text

    assert "接続できません" in body
    assert "稼働中" not in body
    assert "停止疑い" not in body


@pytest.mark.asyncio
async def test_the_page_itself_renders_without_touching_conclair():
    """The shell must not depend on the backend it reports on -- otherwise
    a Conclair outage takes away the page that would have explained it."""
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/dashboard")

    assert response.status_code == 200
    assert 'hx-get="/dashboard/_ops"' in response.text


# --- control writes -------------------------------------------------------


async def _post_control(adapter, **form) -> httpx.Response:
    app = create_app()
    with patch.object(ops, "ChatroomAdapter", return_value=adapter):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            return await client.post("/dashboard/_ops/p/control", data=form)


@pytest.mark.asyncio
async def test_control_button_sets_desired_and_records_the_actor():
    adapter = _adapter({"items": [_summary()], "total": 1})
    adapter.set_loop_control.return_value = {"desired_state": "hold"}

    await _post_control(adapter, state="hold", actor="takahito")

    adapter.set_loop_control.assert_awaited_once()
    kwargs = adapter.set_loop_control.await_args.kwargs
    assert kwargs["project"] == "p"
    assert kwargs["state"] == "hold"
    assert kwargs["actor"] == "takahito"


@pytest.mark.asyncio
async def test_an_empty_actor_still_writes():
    """A control action lost to a validation error is worse than one
    attributed to "unknown" -- this is a stop button."""
    adapter = _adapter({"items": [_summary()], "total": 1})
    adapter.set_loop_control.return_value = {"desired_state": "hold"}

    await _post_control(adapter, state="hold", actor="")

    assert adapter.set_loop_control.await_args.kwargs["actor"] == "unknown (ops UI)"


@pytest.mark.asyncio
async def test_control_post_returns_the_whole_table():
    """The row moves when its state changes -- the sort is by severity --
    so swapping one row would leave it under the wrong heading."""
    adapter = _adapter({"items": [_summary()], "total": 1})
    adapter.set_loop_control.return_value = {"desired_state": "hold"}

    body = (await _post_control(adapter, state="hold", actor="t")).text

    assert "<table" in body


@pytest.mark.asyncio
async def test_a_rejected_control_write_renders_inside_the_table():
    """Replacing the table with a flash would take the buttons and the
    poll trigger off the page, leaving no way to retry."""
    adapter = _adapter({"items": [_summary()], "total": 1})
    adapter.set_loop_control.return_value = {
        "error_type": "ChatroomDBError",
        "error": "pool exhausted",
    }

    response = await _post_control(adapter, state="hold", actor="t")

    assert response.status_code == 200
    assert "pool exhausted" in response.text
    assert "ops-control-btn" in response.text


@pytest.mark.asyncio
async def test_a_control_write_that_raises_still_answers_with_the_page():
    adapter = _adapter({"items": [_summary()], "total": 1})
    adapter.set_loop_control.side_effect = httpx.ConnectError("down")

    response = await _post_control(adapter, state="hold", actor="t")

    assert response.status_code == 200
    assert "ops-control-btn" in response.text


def test_the_control_route_the_buttons_post_to_exists():
    """The buttons build their URL from a Jinja expression, so the generic
    template scan in test_dashboard_routes cannot check this one."""
    assert ("POST", "/dashboard/_ops/{project}/control") in route_table(create_app())


# --- backend health strip -------------------------------------------------


async def _get_health() -> httpx.Response:
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        return await client.get("/dashboard/_ops_health")


@pytest.mark.asyncio
async def test_health_strip_constructs_every_adapter():
    """Only ``health_check`` is stubbed, so the real constructors run.

    Two of these four adapters are MCP clients taking ``sse_url`` and two
    are HTTP clients taking ``base_url``; passing the wrong one raises a
    TypeError before any request is made, which is how the strip first
    shipped as a 500. Mocking the adapter classes wholesale would have
    kept that green.
    """
    with (
        patch.object(ops.ChatroomAdapter, "health_check", AsyncMock(return_value=True)),
        patch.object(ops.LexoraAdapter, "health_check", AsyncMock(return_value=True)),
        patch.object(ops.CognilensAdapter, "health_check", AsyncMock(return_value=True)),
        patch.object(ops.PrismindAdapter, "health_check", AsyncMock(return_value=False)),
    ):
        response = await _get_health()

    assert response.status_code == 200
    for name in ("conclair", "lexora", "cognilens", "prismind"):
        assert name in response.text
    assert "down" in response.text


@pytest.mark.asyncio
async def test_a_probe_that_raises_is_not_reported_as_down():
    """"The check failed" and "the service is down" are different claims,
    and on this page they call for different actions."""
    with (
        patch.object(
            ops.ChatroomAdapter,
            "health_check",
            AsyncMock(side_effect=httpx.ConnectError("boom")),
        ),
        patch.object(ops.LexoraAdapter, "health_check", AsyncMock(return_value=True)),
        patch.object(ops.CognilensAdapter, "health_check", AsyncMock(return_value=True)),
        patch.object(ops.PrismindAdapter, "health_check", AsyncMock(return_value=True)),
    ):
        response = await _get_health()

    assert response.status_code == 200
    assert "確認不可" in response.text


@pytest.mark.asyncio
async def test_the_strip_does_not_invent_a_close_on_the_mcp_adapters():
    """``MCPBaseAdapter.__getattr__`` turns any unknown attribute into an
    MCP tool call, so ``getattr(adapter, "close", None)`` does not find a
    cleanup hook -- it fabricates one and fires a bogus ``close`` tool over
    the wire on every poll. Cleanup has to key on the class, not on duck
    typing. (`test_role_gate` pins the same rule for the MCP tools.)"""
    called: list[str] = []

    def spy(self, name, arguments):  # MCPBaseAdapter.call_tool
        called.append(name)
        raise AssertionError(f"unexpected MCP tool call: {name}")

    with (
        patch.object(ops.ChatroomAdapter, "health_check", AsyncMock(return_value=True)),
        patch.object(ops.LexoraAdapter, "health_check", AsyncMock(return_value=True)),
        patch.object(ops.CognilensAdapter, "health_check", AsyncMock(return_value=True)),
        patch.object(ops.PrismindAdapter, "health_check", AsyncMock(return_value=True)),
        patch("magickit.adapters.mcp_base.MCPBaseAdapter.call_tool", spy),
    ):
        response = await _get_health()

    assert response.status_code == 200
    assert called == []


@pytest.mark.asyncio
async def test_a_hanging_probe_is_capped():
    """The adapters' own timeouts run to 360s, sized for real work. A strip
    that inherits one outlives its 60s poll and stacks requests behind it."""

    async def never() -> bool:
        await asyncio.sleep(3600)
        return True

    with (
        patch.object(ops, "PROBE_TIMEOUT", 0.05),
        patch.object(ops.ChatroomAdapter, "health_check", never),
        patch.object(ops.LexoraAdapter, "health_check", AsyncMock(return_value=True)),
        patch.object(ops.CognilensAdapter, "health_check", AsyncMock(return_value=True)),
        patch.object(ops.PrismindAdapter, "health_check", AsyncMock(return_value=True)),
    ):
        response = await asyncio.wait_for(_get_health(), timeout=10)

    assert response.status_code == 200
    assert "確認不可" in response.text


# --- phone layout ---------------------------------------------------------
#
# The stacked table renders each cell's label from `data-label`, so a
# column's name lives twice. Drift is invisible in a browser: the desktop
# table stays correct while the phone view labels values wrongly.

_TH_RE = re.compile(r"<th(?:\s[^>]*)?>(.*?)</th>", re.DOTALL)
_LABEL_RE = re.compile(r'<td[^>]*\bdata-label="([^"]*)"')


@pytest.mark.asyncio
async def test_labels_match_headers():
    adapter = _adapter({"items": [_summary()], "total": 1})

    body = (await _get("/dashboard/_ops", adapter)).text

    headers = [re.sub(r"\s+", " ", h).strip() for h in _TH_RE.findall(body)]
    assert _LABEL_RE.findall(body) == headers


@pytest.mark.asyncio
async def test_table_opts_into_stacking():
    adapter = _adapter({"items": [_summary()], "total": 1})

    body = (await _get("/dashboard/_ops", adapter)).text

    assert "table-stack" in body


# --- the digest sub-line --------------------------------------------------


@pytest.mark.asyncio
async def test_the_digest_of_the_thread_that_moved_last_is_shown():
    """Which thread: `last_event.thread_id`, which the events read already gave.

    Not the awaiting_reply one -- "awaiting a reply" is already the ブロック軸
    badge, and a second appearance would make the blocked axis louder and the
    稼働軸 quieter. The digest answers a third question (何を話しているのか).
    """
    adapter = _adapter(
        {"items": [_summary()], "total": 1},
        events=_event("T-42"),
        digest=_digest(),
    )

    context = await _collect(adapter)
    row = context["rows"][0]

    assert row.digest is not None
    assert "Bohr が X を提案" in row.digest_line
    adapter.get_thread_digest.assert_awaited_once()
    assert adapter.get_thread_digest.await_args.kwargs["thread_id"] == "T-42"


@pytest.mark.asyncio
async def test_no_events_means_no_digest_read_at_all():
    """Nothing to label it with, so there is nothing to ask for."""
    adapter = _adapter({"items": [_summary()], "total": 1}, events={"items": []})

    context = await _collect(adapter)

    assert context["rows"][0].digest is None
    adapter.get_thread_digest.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_absent_digest_leaves_the_row_empty_rather_than_substituting():
    """A digest describing thread B under a cell that says thread A is the
    worst possible cell on this page."""
    adapter = _adapter(
        {"items": [_summary()], "total": 1},
        events=_event("T-42"),
        digest={"present": False, "digest": None},
    )

    context = await _collect(adapter)

    assert context["rows"][0].digest is None
    assert context["rows"][0].digest_line == ""


@pytest.mark.asyncio
async def test_a_failing_digest_read_leaves_the_rest_of_the_row_intact():
    """A missing digest is not a reason to stop reporting whether anything runs."""
    adapter = _adapter(
        {"items": [_summary()], "total": 1},
        control={
            "desired_state": "run",
            "configured": True,
            "observed_state": "run",
            "observed_at": NOW.isoformat(),
        },
        events=_event("T-42"),
    )
    adapter.get_thread_digest.side_effect = httpx.ConnectError("nope")

    context = await _collect(adapter)
    row = context["rows"][0]

    assert context["unavailable"] is None
    assert row.status == "running"
    assert row.last_event["thread_id"] == "T-42"
    assert row.digest is None


@pytest.mark.asyncio
async def test_a_digest_error_envelope_is_treated_as_absent():
    adapter = _adapter(
        {"items": [_summary()], "total": 1},
        events=_event("T-42"),
        digest={"error_type": "ChatroomNotFoundError", "error": "gone"},
    )

    context = await _collect(adapter)

    assert context["rows"][0].digest is None


def test_the_digest_line_is_truncated_and_the_full_text_kept():
    row = _row(
        digest={"digest": "あ" * 500, "stale": False},
        digest_chars=160,
    )

    assert len(row.digest_line) == 160
    assert row.digest_line.endswith("…")
    assert len(row.digest_full) == 500


def test_the_digest_line_collapses_newlines():
    """A multi-line digest in a table cell would break the row height."""
    row = _row(digest={"digest": "one\n\ntwo\nthree", "stale": False})

    assert row.digest_line == "one two three"


def test_staleness_is_claimed_only_when_conclair_says_so():
    """This page never invents a verdict.

    An older Conclair that does not send `stale` gets its age shown and no
    claim made -- the same stance as `control` read failures becoming
    `unknown` rather than "probably running".
    """
    assert _row(digest={"digest": "x", "stale": True}).digest_is_stale is True
    assert _row(digest={"digest": "x", "stale": False}).digest_is_stale is False
    assert _row(digest={"digest": "x"}).digest_is_stale is False
    assert _row(digest=None).digest_is_stale is False


@pytest.mark.asyncio
async def test_the_digest_is_escaped_in_the_rendered_page():
    """The digest is model output derived from user-supplied message content."""
    adapter = _adapter(
        {"items": [_summary()], "total": 1},
        events=_event("T-42"),
        digest=_digest("<script>alert(1)</script>"),
    )

    with patch.object(ops, "ChatroomAdapter", return_value=adapter):
        app = create_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.get("/dashboard/_ops")

    assert response.status_code == 200
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;" in response.text


# ---------------------------------------------------------------------------
# The reach proof and its sanity check.
#
# These two tests exist to make the recording clock's guarantees checkable
# rather than asserted. The sanity test proves the recorder catches a call
# from ``magickit/web/ops.py`` when the production ``collect()`` is invoked
# -- without it, a broken recorder could report anything and the proof
# below would happily agree. The reach proof compares the observed reach
# set against :data:`_REACHES_PRODUCTION_SEAM`; a mismatch in either
# direction is a finding, as msg-420 §3 requires.
#
# They are named to sort last within the module because the proof reads
# per-test buckets that the autouse fixture only fills as tests finish, so
# it needs to run after every test it summarises.


@pytest.mark.asyncio
async def test_the_recording_clock_reports_the_production_module_as_caller():
    """When the seam is called from ``ops.collect``, the recorder must
    report ``magickit/web/ops.py`` as the caller -- not this test file, not
    ``unittest/mock.py``. Without this bare-metal check, the reach proof
    below can be satisfied by any decoy that happens to increment the
    counter."""
    _CLOCK_READS.clear()
    with patch.object(
        ops, "ChatroomAdapter", return_value=_adapter(httpx.ConnectError("sanity"))
    ):
        await ops.collect(Settings())  # no `now=`; the seam is exercised.

    assert _CLOCK_READS, "the recording clock captured no reads at all"

    # Component-wise suffix on a POSIX-normalized, repo-root-relative path
    # -- raw `==` would fail on CI's `/app/src/magickit/web/ops.py` vs local
    # `C:\\...\\ops.py`, and a raw string `.endswith` would falsely match
    # `.../vendor/other/web/ops.py`. See rev.6 §2.
    caller_file, _line = _CLOCK_READS[0]
    parts = Path(caller_file).resolve().parts
    assert parts[-3:] == ("magickit", "web", "ops.py"), (
        f"expected recorder caller under magickit/web/ops.py, got {parts[-3:]}"
    )


def test_the_reach_declaration_matches_observation():
    """Every test's observed reach agrees with :data:`_REACHES_PRODUCTION_SEAM`.

    Two mismatches are both findings, not just the first:

    - A declared-reaches test that captured no reads means the patch is a
      decoy: something else is providing the value, or the seam has moved
      out from under this file.
    - An undeclared-reaches test that captured a read means we found a
      time dependency we did not know about -- the same shape as the 9
      quiet-but-mined tests in ``test_board.py``.

    Also asserts DoD (iv): the "reaches" side of the declaration is
    non-empty. An edit that patches the seam but has no test that reads
    it is a patch nobody uses.

    Scoped to tests that actually ran in this session/worker. A test that
    was filtered out (``pytest -k``, an explicit subset) or dispatched to
    a different ``pytest-xdist`` worker is neither a decoy nor a hidden
    dependency -- it is simply absent, and pretending otherwise would
    turn every subset invocation into a false failure. The full-population
    guarantee comes from the gate's single-process invocation of the whole
    file; each partial invocation still verifies its own slice.
    """
    ran: set[str] = set(_RECORDED_READS)
    observed_reaches: set[str] = {
        name
        for name, reads in _RECORDED_READS.items()
        if reads and name != "test_the_reach_declaration_matches_observation"
    }

    # Static check: every declared name must exist as a callable test in
    # this module. Otherwise a typo in the declaration would look like a
    # test that was simply filtered out under subset execution and slip
    # past silently. Cheap because it does not depend on which tests ran.
    module_test_names = {
        name
        for name, obj in globals().items()
        if name.startswith("test_") and callable(obj)
    }
    unknown_declarations = _REACHES_PRODUCTION_SEAM - module_test_names
    assert not unknown_declarations, (
        f"declared reach names are not test functions in this module "
        f"(typo, moved test, or stale entry): "
        f"{sorted(unknown_declarations)}"
    )

    # Only the declared tests that actually ran can be "missed". Tests
    # that were never scheduled cannot be decoys.
    declared_that_ran = _REACHES_PRODUCTION_SEAM & ran
    missed = declared_that_ran - observed_reaches
    # A read from a test that wasn't declared is always a finding, no
    # matter which subset is running.
    unexpected = observed_reaches - _REACHES_PRODUCTION_SEAM

    detail_missed = sorted(missed)
    detail_unexpected = sorted(unexpected)
    assert not missed and not unexpected, (
        f"reach declaration disagrees with observation.\n"
        f"declared but not observed (decoy patch or moved seam): "
        f"{detail_missed}\n"
        f"observed but not declared (unknown time dependency): "
        f"{detail_unexpected}"
    )
    # DoD (iv): editing a file for the recorder means at least one test
    # must actually reach the patched seam -- but only meaningful when a
    # declared reacher was scheduled to run in this slice. In a subset
    # that excludes every declared reacher there is nothing to floor.
    if declared_that_ran:
        assert observed_reaches, (
            "no test observed a production wall-clock read; the patch is a "
            "no-op and the file's `NOW` literal did not need the recorder."
        )
