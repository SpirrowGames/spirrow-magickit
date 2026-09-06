"""Unit tests for the やること board (``/dashboard/decisions``).

The board makes one claim per card — **this is still waiting for you** —
and every test here is about that claim being true, or being withheld.
Two properties carry the whole design:

1. A decision card exists only while the parked message is still the
   thread's head. Get this wrong in the permissive direction and the board
   fills with decisions that were answered days ago, which is the exact
   failure that makes a to-do list stop being read.
2. The 完了 column cannot be written to. It is derived from "no longer in
   the live set", so a card sitting there is evidence that something
   actually happened — not that somebody dragged it.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from magickit.config import Settings
from magickit.core import board_lanes
from magickit.core.board_lanes import BoardLaneStore, SeenItem
from magickit.core.decision_materials import DecisionMaterialStore
from magickit.deploy import records
from magickit.main import create_app
from magickit.web import board
from tests.route_table import route_table, sole_handler

NOW = datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc)


def _ago(minutes: float) -> str:
    return (NOW - timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


class _FrozenClock(datetime):
    """``datetime`` whose ``now()`` is :data:`NOW`. Everything else is real."""

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return NOW.astimezone(tz) if tz is not None else NOW.replace(tzinfo=None)


@pytest.fixture(autouse=True)
def _board_reads_the_same_clock_as_the_fixtures(monkeypatch):
    """Give this module **one** clock.

    Every ``last_activity_at`` / ``observed_at`` a mock Conclair returns here
    is anchored to :data:`NOW` (via :func:`_ago`), and the ``_collect`` helper
    passes ``now=NOW`` explicitly. The route handlers do not: ``board_set_lane``
    and ``board_fragment`` call ``collect(settings)`` with no ``now``, so
    ``board.collect`` fell through to ``datetime.now(timezone.utc)``
    (``web/board.py:452``) — the **real** clock. That is two clocks in one
    test, and which one wins depended on what time of day the suite ran.

    It did not stay theoretical. With ``ops_stall_minutes`` defaulting to 30
    (``config.py:123``) and the newest fixture heartbeat at ``NOW - 1min``,
    every route-level test in this file crossed a trip point at
    **2026-09-02T12:29:00Z**. After it, ``ops.classify`` called the fixture
    project ``stalled``, ``_collect_loops`` added a ``loop:`` card,
    ``touch_seen`` recorded *that* key in ``board_seen`` — and the follow-up
    ``_prune`` ran ``DELETE FROM board_lanes WHERE item_key NOT IN (SELECT
    item_key FROM board_seen)`` (``core/board_lanes.py:239-240``), taking out
    the lane row the test had just written. ``test_an_unvouched_move_records_
    no_actor`` then died on ``KeyError: 'k'``. CI last ran green on ``main``
    at 2026-09-02T08:28:52Z — 3h51m *before* the trip point — so the repo's
    gate went red for every PR opened afterwards.

    Freezing the clock the code under test reads (rather than moving the
    fixtures onto the real clock) is what makes this file independent of when
    it runs: the assertions stay pinned to known instants, so a future edit
    cannot re-introduce the drift by picking a new literal.

    ``core.board_lanes`` is frozen to the same instant, because it is the
    *second* half of the same seam: it stamps ``last_seen_at`` from its own
    clock, and ``collect`` then selects against it with
    ``since = now - board_done_days``. Freeze only ``board`` and the 完了
    window is measured from ``NOW`` against rows stamped in real time — which
    still passes today only because the real clock is *ahead* of ``NOW``.
    Freezing both is what makes the file independent of the wall clock in
    both directions; verified by re-running it with the clock shifted
    -400/-5/0/+1/+30/+400/+4000 days.
    """
    monkeypatch.setattr(board, "datetime", _FrozenClock)
    monkeypatch.setattr(board_lanes, "datetime", _FrozenClock)


def _settings(db_path: str, **kw) -> Settings:
    return Settings(db_path=db_path, **kw)


def _thread(thread_id="T-1", last_msg_id="msg-9", **kw):
    entry = {
        "thread_id": thread_id,
        "title": f"title of {thread_id}",
        "status": "active",
        "last_msg_id": last_msg_id,
        "owner": "human",
    }
    entry.update(kw)
    return entry


def _summary(project="p", **kw):
    entry = {
        "project": project,
        "thread_count": 1,
        "threads_by_status": {"active": 1},
        "gated_thread_count": 0,
        "message_count": 1,
        "last_activity_at": _ago(1),
    }
    entry.update(kw)
    return entry


def _adapter(*, summaries=None, threads=None, control=None):
    """A Conclair stand-in. ``threads`` maps project -> list of threads."""
    adapter = AsyncMock()
    if isinstance(summaries, BaseException):
        adapter.list_project_summaries.side_effect = summaries
    else:
        adapter.list_project_summaries.return_value = (
            summaries if summaries is not None else {"items": [_summary()]}
        )

    threads = threads if threads is not None else {}

    async def _list_threads(*, project, **_):
        return {"items": threads.get(project, [])}

    adapter.list_threads.side_effect = _list_threads
    adapter.get_loop_control.return_value = control or {
        "desired_state": "run",
        "configured": True,
        "observed_state": "run",
        "observed_at": _ago(1),
    }
    return adapter


async def _put_material(db_path, *, project="p", thread_id="T-1", head="msg-9", **kw):
    await DecisionMaterialStore(db_path=db_path).put_material(
        project=project,
        thread_id=thread_id,
        head_msg_id=head,
        signature=kw.get("signature"),
        question=kw.get("question", "どちらにしますか"),
        options=kw.get("options"),
        recommendation=kw.get("recommendation"),
        recommendation_reason=None,
        unknowns=None,
    )


async def _collect(adapter, settings, *, now=NOW):
    with patch.object(board, "ChatroomAdapter", return_value=adapter):
        return await board.collect(settings, now=now)


def _cards(context):
    return [c for column in context["columns"].values() for c in column]


def _no_deploys(monkeypatch):
    """Deploy requests come off the real file store; keep it out of the way."""
    store = AsyncMock()
    store.list_requests = lambda **_: []
    monkeypatch.setattr(records, "get_store", lambda: store)


# --- 判断待ち: the freshness rule ----------------------------------------


@pytest.mark.asyncio
async def test_decision_card_appears_while_the_parked_msg_is_still_the_head(
    temp_db_path, monkeypatch
):
    _no_deploys(monkeypatch)
    await _put_material(temp_db_path, head="msg-9")
    adapter = _adapter(threads={"p": [_thread(last_msg_id="msg-9")]})

    context = await _collect(adapter, _settings(temp_db_path))

    cards = _cards(context)
    assert [c.kind for c in cards] == ["decision"]
    assert cards[0].key == "decision:p:T-1"
    assert cards[0].note == "どちらにしますか"


@pytest.mark.asyncio
async def test_answered_decision_does_not_come_back(temp_db_path, monkeypatch):
    """The materials table never deletes: a row outlives the decision.

    Listing on the row's existence alone is the difference between a board
    with 8 things on it and a board with 33, most of them already done.
    """
    _no_deploys(monkeypatch)
    await _put_material(temp_db_path, head="msg-9")
    adapter = _adapter(threads={"p": [_thread(last_msg_id="msg-40")]})

    context = await _collect(adapter, _settings(temp_db_path))

    assert _cards(context) == []


@pytest.mark.asyncio
async def test_thread_missing_from_the_listing_is_not_a_card(
    temp_db_path, monkeypatch
):
    """Resolved threads drop out of the listing. They are not waiting."""
    _no_deploys(monkeypatch)
    await _put_material(temp_db_path, thread_id="T-gone")
    adapter = _adapter(threads={"p": []})

    context = await _collect(adapter, _settings(temp_db_path))

    assert _cards(context) == []


@pytest.mark.asyncio
async def test_a_thread_with_no_last_msg_id_is_not_assumed_fresh(
    temp_db_path, monkeypatch
):
    """Same direction as the judgement page: unreadable falls to *not* fresh."""
    _no_deploys(monkeypatch)
    await _put_material(temp_db_path, head="msg-9")
    adapter = _adapter(threads={"p": [_thread(last_msg_id=None)]})

    context = await _collect(adapter, _settings(temp_db_path))

    assert _cards(context) == []


# --- degradation: unreadable is not zero ---------------------------------


@pytest.mark.asyncio
async def test_conclair_outage_is_named_not_rendered_as_an_empty_board(
    temp_db_path, monkeypatch
):
    _no_deploys(monkeypatch)
    await _put_material(temp_db_path)
    adapter = _adapter(summaries=httpx.ConnectError("down"))

    context = await _collect(adapter, _settings(temp_db_path))

    assert _cards(context) == []
    assert any("conclair が読めない" in n for n in context["notices"])


@pytest.mark.asyncio
async def test_one_projects_thread_read_failing_names_that_project(
    temp_db_path, monkeypatch
):
    """The other projects still render; the broken one is not silently 0."""
    _no_deploys(monkeypatch)
    await _put_material(temp_db_path, project="good", thread_id="T-1", head="msg-9")
    await _put_material(temp_db_path, project="bad", thread_id="T-2", head="msg-9")

    adapter = _adapter(
        summaries={"items": [_summary("good"), _summary("bad")]},
        threads={"good": [_thread(last_msg_id="msg-9")]},
    )

    async def _list_threads(*, project, **_):
        if project == "bad":
            raise httpx.ConnectError("nope")
        return {"items": [_thread(last_msg_id="msg-9")]}

    adapter.list_threads.side_effect = _list_threads

    context = await _collect(adapter, _settings(temp_db_path))

    assert [c.project for c in _cards(context)] == ["good"]
    assert any(n.startswith("bad:") for n in context["notices"])


@pytest.mark.asyncio
async def test_deploy_approvals_survive_a_conclair_outage(
    temp_db_path, monkeypatch
):
    """The approval column reads a local file store, so an outage in the
    chatroom must not take it off the board along with everything else."""
    pending = records.DeployRequest(
        request_id="abc123",
        target="spirrow-cognilens",
        requested_by="claude-code",
        reason="two-face switch",
        created_at=_ago(30),
    )
    store = AsyncMock()
    store.list_requests = lambda **_: [pending]
    monkeypatch.setattr(records, "get_store", lambda: store)

    adapter = _adapter(summaries=httpx.ConnectError("down"))
    context = await _collect(adapter, _settings(temp_db_path))

    cards = _cards(context)
    assert [c.kind for c in cards] == ["deploy"]
    assert cards[0].key == "deploy:abc123"


# --- 止まったループ -------------------------------------------------------


@pytest.mark.asyncio
async def test_only_held_and_stalled_loops_get_a_card(temp_db_path, monkeypatch):
    """`running` needs nothing from me; `unmanaged` never will.

    An old scratch project with no conductor would otherwise sit on the
    board forever, which is how a board stops being read.
    """
    _no_deploys(monkeypatch)
    adapter = _adapter(
        summaries={
            "items": [
                _summary("running-one"),
                _summary("held-one"),
                _summary("stalled-one", last_activity_at=_ago(600)),
                _summary("never-managed", last_activity_at=_ago(99999)),
            ]
        }
    )

    async def _control(*, project):
        if project == "held-one":
            return {
                "desired_state": "hold", "configured": True,
                "observed_state": "hold", "observed_at": _ago(5),
                "desired_actor": "takahito",
            }
        if project == "never-managed":
            return {
                "desired_state": "run", "configured": False,
                "observed_state": None, "observed_at": None,
            }
        return {
            "desired_state": "run", "configured": True,
            "observed_state": "run",
            "observed_at": _ago(600 if project == "stalled-one" else 1),
        }

    adapter.get_loop_control.side_effect = _control

    context = await _collect(adapter, _settings(temp_db_path))

    loops = sorted(c.project for c in _cards(context) if c.kind == "loop")
    assert loops == ["held-one", "stalled-one"]


# --- lanes ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_untouched_cards_are_new_without_writing_a_row(
    temp_db_path, monkeypatch
):
    """`new` is the absence of a row: looking at the board writes no lane."""
    _no_deploys(monkeypatch)
    await _put_material(temp_db_path)
    adapter = _adapter(threads={"p": [_thread()]})

    context = await _collect(adapter, _settings(temp_db_path))

    assert len(context["columns"]["new"]) == 1
    assert await BoardLaneStore(db_path=temp_db_path).read_lanes() == {}


@pytest.mark.asyncio
async def test_moving_to_doing_and_back_to_new_leaves_no_row(temp_db_path):
    store = BoardLaneStore(db_path=temp_db_path)

    await store.set_lane(
        item_key="decision:p:T-1", lane="doing", fingerprint="msg-9", actor="t"
    )
    assert (await store.read_lanes())["decision:p:T-1"]["lane"] == "doing"

    await store.set_lane(
        item_key="decision:p:T-1", lane="new", fingerprint="msg-9", actor="t"
    )
    assert await store.read_lanes() == {}


@pytest.mark.asyncio
async def test_a_card_that_changed_under_you_says_so(temp_db_path, monkeypatch):
    """Moved to 対応中 at msg-9; the thread is now parked at msg-40.

    The card looks the same and is a different question. Leaving it silent
    is how you answer the wrong one.
    """
    _no_deploys(monkeypatch)
    await _put_material(temp_db_path, head="msg-40")
    await BoardLaneStore(db_path=temp_db_path).set_lane(
        item_key="decision:p:T-1", lane="doing", fingerprint="msg-9", actor="t"
    )
    adapter = _adapter(threads={"p": [_thread(last_msg_id="msg-40")]})

    context = await _collect(adapter, _settings(temp_db_path))

    card = context["columns"]["doing"][0]
    assert card.changed is True
    assert card.moved_by == "t"


@pytest.mark.asyncio
async def test_an_unmoved_card_is_not_flagged_as_changed(
    temp_db_path, monkeypatch
):
    _no_deploys(monkeypatch)
    await _put_material(temp_db_path, head="msg-9")
    await BoardLaneStore(db_path=temp_db_path).set_lane(
        item_key="decision:p:T-1", lane="doing", fingerprint="msg-9", actor="t"
    )
    adapter = _adapter(threads={"p": [_thread(last_msg_id="msg-9")]})

    context = await _collect(adapter, _settings(temp_db_path))

    assert context["columns"]["doing"][0].changed is False


# --- 完了列 ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_answering_a_decision_moves_it_to_done(temp_db_path, monkeypatch):
    """The whole point of the column: acting on a card is what moves it.

    First render sees it live; the thread then advances; the second render
    finds it gone and reports *why*.
    """
    _no_deploys(monkeypatch)
    await _put_material(temp_db_path, head="msg-9")

    settings = _settings(temp_db_path)
    await _collect(_adapter(threads={"p": [_thread(last_msg_id="msg-9")]}), settings)

    context = await _collect(
        _adapter(threads={"p": [_thread(last_msg_id="msg-40")]}), settings
    )

    assert _cards(context) == []
    assert [c.key for c in context["done"]] == ["decision:p:T-1"]
    assert context["done"][0].reason == (
        "スレッドが進みました（駐機 msg の後に発言があります）"
    )


@pytest.mark.asyncio
async def test_a_resolved_thread_says_resolved(temp_db_path, monkeypatch):
    _no_deploys(monkeypatch)
    await _put_material(temp_db_path, head="msg-9")
    settings = _settings(temp_db_path)

    await _collect(_adapter(threads={"p": [_thread(last_msg_id="msg-9")]}), settings)
    context = await _collect(
        _adapter(
            threads={"p": [_thread(last_msg_id="msg-40", status="resolved")]}
        ),
        settings,
    )

    assert context["done"][0].reason == "スレッドが resolved になりました"


@pytest.mark.asyncio
async def test_done_never_claims_you_were_the_one_who_did_it(temp_db_path):
    """A card can leave because somebody else answered. The column may only
    report that it left, never that the reader acted."""
    store = BoardLaneStore(db_path=temp_db_path)
    await store.touch_seen([SeenItem(item_key="loop:p", kind="loop", title="p")])

    gone = await store.list_gone(live_keys=set(), since=NOW - timedelta(days=7))
    reason = board._gone_reason(gone[0], board._Live())

    assert "あなた" not in reason
    assert reason == "ループが HOLD / 停止疑いでなくなりました"


@pytest.mark.asyncio
async def test_done_only_looks_back_the_configured_window(temp_db_path):
    store = BoardLaneStore(db_path=temp_db_path)
    await store.touch_seen([SeenItem(item_key="k", kind="loop", title="t")])

    fresh = await store.list_gone(
        live_keys=set(), since=NOW - timedelta(days=7)
    )
    old = await store.list_gone(
        live_keys=set(), since=NOW + timedelta(days=1)
    )

    assert [r["item_key"] for r in fresh] == ["k"]
    assert old == []


@pytest.mark.asyncio
async def test_a_live_card_is_never_in_done(temp_db_path):
    store = BoardLaneStore(db_path=temp_db_path)
    await store.touch_seen([SeenItem(item_key="k", kind="loop", title="t")])

    assert await store.list_gone(
        live_keys={"k"}, since=NOW - timedelta(days=7)
    ) == []


# --- the write endpoint ---------------------------------------------------


def _client(settings, adapter):
    app = create_app()
    return app, settings, adapter


@pytest.mark.asyncio
async def test_done_is_refused_by_the_lane_endpoint(temp_db_path, monkeypatch):
    """A draggable 完了 would let the board disagree with the world.

    The refusal names the real mechanism instead of just saying "no": the
    card leaves when the decision is made, not when it is filed.
    """
    _no_deploys(monkeypatch)
    await _put_material(temp_db_path)
    settings = _settings(temp_db_path)

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    with patch.object(board, "get_settings", return_value=settings), \
         patch.object(board, "ChatroomAdapter", return_value=_adapter(
             threads={"p": [_thread()]})):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            response = await client.post(
                "/dashboard/decisions/_lane",
                data={
                    "item_key": "decision:p:T-1",
                    "lane": "done",
                    "fingerprint": "msg-9",
                },
            )

    assert response.status_code == 200
    assert "完了列にはドラッグで置けません" in response.text
    assert await BoardLaneStore(db_path=temp_db_path).read_lanes() == {}


@pytest.mark.asyncio
async def test_an_unknown_lane_is_refused(temp_db_path, monkeypatch):
    _no_deploys(monkeypatch)
    settings = _settings(temp_db_path)

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    with patch.object(board, "get_settings", return_value=settings), \
         patch.object(board, "ChatroomAdapter", return_value=_adapter()):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            response = await client.post(
                "/dashboard/decisions/_lane",
                data={"item_key": "k", "lane": "archive", "fingerprint": ""},
            )

    assert "知らない列です" in response.text
    assert await BoardLaneStore(db_path=temp_db_path).read_lanes() == {}


@pytest.mark.asyncio
async def test_a_cross_site_post_is_refused(temp_db_path, monkeypatch):
    _no_deploys(monkeypatch)
    settings = _settings(temp_db_path)

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    with patch.object(board, "get_settings", return_value=settings), \
         patch.object(board, "ChatroomAdapter", return_value=_adapter()):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            response = await client.post(
                "/dashboard/decisions/_lane",
                data={"item_key": "k", "lane": "doing", "fingerprint": ""},
                headers={"Sec-Fetch-Site": "cross-site"},
            )

    assert "別サイトからの操作" in response.text
    assert await BoardLaneStore(db_path=temp_db_path).read_lanes() == {}


@pytest.mark.asyncio
async def test_a_lane_move_lands_and_the_whole_board_comes_back(
    temp_db_path, monkeypatch
):
    _no_deploys(monkeypatch)
    await _put_material(temp_db_path)
    settings = _settings(temp_db_path)

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    with patch.object(board, "get_settings", return_value=settings), \
         patch.object(board, "ChatroomAdapter", return_value=_adapter(
             threads={"p": [_thread()]})):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            response = await client.post(
                "/dashboard/decisions/_lane",
                data={
                    "item_key": "decision:p:T-1",
                    "lane": "doing",
                    "fingerprint": "msg-9",
                },
            )

    lanes = await BoardLaneStore(db_path=temp_db_path).read_lanes()
    assert lanes["decision:p:T-1"]["lane"] == "doing"
    # The response is the board, not one card: a move changes which column
    # the card is in, so a single-card swap would leave it under the wrong
    # heading until the next poll.
    assert 'data-dropzone="new"' in response.text
    assert 'data-dropzone="parked"' in response.text


# --- routing --------------------------------------------------------------


def test_the_board_owns_the_decisions_index_and_nothing_else_does():
    """The 302 stub is gone. Two handlers on one path would make the winner
    depend on registration order -- an invisible fact."""
    app = create_app()

    assert sole_handler(app, "GET", "/dashboard/decisions") == (
        "magickit.web.board.board_page"
    )


def test_the_board_fragments_do_not_collide_with_the_judgement_page():
    """`/_board` is one segment, `/{project}/{thread_id}` is two."""
    app = create_app()
    table = route_table(app)

    assert table[("GET", "/dashboard/decisions/_board")] == [
        "magickit.web.board.board_fragment"
    ]
    assert table[("POST", "/dashboard/decisions/_lane")] == [
        "magickit.web.board.board_set_lane"
    ]
    assert table[("GET", "/dashboard/decisions/{project}/{thread_id}")] == [
        "magickit.web.decisions.decision_page"
    ]


# --- rendering ------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_done_column_has_no_dropzone(temp_db_path, monkeypatch):
    """The refusal in the handler is a rule; this is the same rule expressed
    as structure -- there is nowhere on the page to drop a card into 完了."""
    _no_deploys(monkeypatch)
    settings = _settings(temp_db_path)

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    with patch.object(board, "get_settings", return_value=settings), \
         patch.object(board, "ChatroomAdapter", return_value=_adapter()):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            html = (await client.get("/dashboard/decisions/_board")).text

    zones = set(re.findall(r'data-dropzone="([^"]+)"', html))
    assert zones == {"new", "doing", "parked"}


@pytest.mark.asyncio
async def test_a_title_carrying_html_is_escaped(temp_db_path, monkeypatch):
    _no_deploys(monkeypatch)
    await _put_material(temp_db_path, question="<script>alert(1)</script>")
    settings = _settings(temp_db_path)

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    adapter = _adapter(
        threads={"p": [_thread(title="<img src=x onerror=alert(1)>")]}
    )
    with patch.object(board, "get_settings", return_value=settings), \
         patch.object(board, "ChatroomAdapter", return_value=adapter):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            html = (await client.get("/dashboard/decisions/_board")).text

    assert "<img src=x" not in html
    assert "<script>alert(1)</script>" not in html
    assert "&lt;img src=x" in html


@pytest.mark.asyncio
async def test_an_unvouched_move_records_no_actor(temp_db_path, monkeypatch):
    """"nobody named themselves" and "someone named `unknown`" are different.

    `identity.tailnet_name` falls back to the literal "unknown", which would
    print on the card as 「対応中へ たった今（unknown）」 -- a name that is
    not a name.
    """
    _no_deploys(monkeypatch)
    settings = _settings(temp_db_path)

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    with patch.object(board, "get_settings", return_value=settings), \
         patch.object(board, "ChatroomAdapter", return_value=_adapter()):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            await client.post(
                "/dashboard/decisions/_lane",
                data={"item_key": "k", "lane": "doing", "fingerprint": ""},
            )

    lanes = await BoardLaneStore(db_path=temp_db_path).read_lanes()
    assert lanes["k"]["moved_by"] is None


def test_cards_are_ordered_oldest_first_within_a_kind():
    """The board exists to stop things rotting, so what has waited longest
    is what you see first. New arrivals are visible from the count and from
    the age printed on every card."""
    def _card(kind, minutes):
        return board.Card(
            key=f"{kind}:{minutes}", kind=kind, title="t", href="#",
            since=NOW - timedelta(minutes=minutes),
        )

    cards = [
        _card("decision", 10),
        _card("loop", 9999),
        _card("decision", 5000),
        _card("deploy", 1),
    ]
    cards.sort(key=board._sort_key)

    assert [c.key for c in cards] == [
        "deploy:1",        # 承認は本番が止まって待っている種類 ∴ 先頭
        "decision:5000",   # 判断は古い順
        "decision:10",
        "loop:9999",
    ]


def test_a_card_with_no_timestamp_sorts_last_rather_than_first():
    """Unreadable is not "just now" and not "ancient"."""
    dated = board.Card(key="a", kind="decision", title="t", href="#", since=NOW)
    undated = board.Card(key="b", kind="decision", title="t", href="#")

    cards = [undated, dated]
    cards.sort(key=board._sort_key)

    assert [c.key for c in cards] == ["a", "b"]


def test_a_loop_card_does_not_print_its_project_twice():
    """The loop card's title *is* the project name."""
    card = board.Card(
        key="loop:p", kind="loop", title="p", href="/dashboard",
        project="p", detail="停止疑い · 返答待ち 2",
    )

    assert card.subline == "停止疑い · 返答待ち 2"


def test_a_decision_card_keeps_the_project_in_its_subline():
    """There the title is the thread's, so the project is new information."""
    card = board.Card(
        key="decision:p:T-1", kind="decision", title="スレッドのタイトル",
        href="#", project="p",
    )

    assert card.subline == "p"
