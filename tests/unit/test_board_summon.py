"""The Summon card on the やること board (T-human-call-summon-server-side v1.2).

What the card must say, and what it must not:

- a valid ``active_summon`` gives one card, above deploy, with the elapsed
  hours (one decimal) and a link to the thread's judgement page;
- if the called-in thread ended on ``NEXT: none`` the card adds the
  "停止済み — 解除するか close" line;
- a malformed value is *shown* (mindwire treats it as HOLD), never dropped;
- no Summon: no card; Conclair unreadable: the existing 判定不能 notice;
- no "N 本停止中" count anywhere (Fermi msg-1434 removed it).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from magickit.web import board
from tests.unit.test_board import (  # noqa: F401 - the autouse clock fixture
    NOW,
    _adapter,
    _board_reads_the_same_clock_as_the_fixtures,
    _cards,
    _collect,
    _no_deploys,
    _settings,
    _summary,
)


def _control(active_summon=None, *, present=True):
    body = {
        "desired_state": "run",
        "configured": True,
        "observed_state": "run",
        "observed_at": (NOW - timedelta(minutes=1)).isoformat(),
    }
    if present:
        body["active_summon"] = active_summon
    return body


def _summon(hours_ago: float = 2.0, thread_id: str = "T-call") -> dict:
    since = (NOW - timedelta(hours=hours_ago)).isoformat()
    return {"thread_id": thread_id, "since": since, "by": "takahito"}


def _summon_cards(context):
    return [c for c in _cards(context) if c.kind == "summon"]


@pytest.mark.asyncio
async def test_a_valid_summon_is_one_card_with_elapsed_hours(temp_db_path, monkeypatch):
    _no_deploys(monkeypatch)
    adapter = _adapter(control=_control(_summon(hours_ago=2.25)))

    context = await _collect(adapter, _settings(temp_db_path))

    (card,) = _summon_cards(context)
    assert card.title == "Summon 中: T-call"
    assert card.href == "/dashboard/decisions/p/T-call"
    assert card.note == "経過 2.3 時間"
    assert "停止済み" not in card.note
    assert "本" not in card.note  # no collateral count (msg-1434)
    adapter.get_thread.assert_awaited_once_with(project="p", thread_id="T-call")


@pytest.mark.asyncio
async def test_the_summon_card_sits_above_deploy(temp_db_path, monkeypatch):
    assert board._sort_key(board.Card(key="s", kind="summon", title="", href="")) < board._sort_key(
        board.Card(key="d", kind="deploy", title="", href="")
    )


@pytest.mark.asyncio
async def test_a_called_thread_that_stopped_says_so(temp_db_path, monkeypatch):
    _no_deploys(monkeypatch)
    stopped = {"msg_id": "msg-3", "author": "Heisenberg", "content": "done.\n\nSTOP: done\nNEXT: none\n"}
    adapter = _adapter(control=_control(_summon()), messages={"T-call": [stopped]})

    (card,) = _summon_cards(await _collect(adapter, _settings(temp_db_path)))

    assert "呼び出し先のスレッドは停止済み（STOP: done）。解除するか close してください" in card.note


@pytest.mark.asyncio
async def test_an_unreadable_called_thread_still_gives_the_card(temp_db_path, monkeypatch):
    _no_deploys(monkeypatch)
    adapter = _adapter(control=_control(_summon()))
    adapter.get_thread.side_effect = RuntimeError("conclair hiccup")

    (card,) = _summon_cards(await _collect(adapter, _settings(temp_db_path)))

    assert card.note == "経過 2.0 時間"


@pytest.mark.parametrize("control", [_control(None), _control(present=False)], ids=["null", "absent"])
@pytest.mark.asyncio
async def test_no_summon_no_card(temp_db_path, monkeypatch, control):
    _no_deploys(monkeypatch)
    adapter = _adapter(control=control)

    assert _summon_cards(await _collect(adapter, _settings(temp_db_path))) == []


@pytest.mark.asyncio
async def test_a_malformed_summon_is_shown_not_dropped(temp_db_path, monkeypatch):
    _no_deploys(monkeypatch)
    adapter = _adapter(control=_control({"thread_id": " ", "since": "yesterday", "by": ""}))

    (card,) = _summon_cards(await _collect(adapter, _settings(temp_db_path)))

    assert card.title == "Summon: 値が壊れています（mindwire は HOLD 扱い）"
    adapter.get_thread.assert_not_called()


@pytest.mark.asyncio
async def test_conclair_unreadable_is_the_existing_notice(temp_db_path, monkeypatch):
    _no_deploys(monkeypatch)
    adapter = _adapter(summaries=RuntimeError("down"))

    context = await _collect(adapter, _settings(temp_db_path))

    assert _summon_cards(context) == []
    assert any("conclair が読めない" in n for n in context["notices"])


@pytest.mark.asyncio
async def test_a_control_read_failure_gives_no_summon_card(temp_db_path, monkeypatch):
    _no_deploys(monkeypatch)
    adapter = _adapter(summaries={"items": [_summary()]})
    adapter.get_loop_control.return_value = {"error_type": "ChatroomDBError", "error": "x"}

    assert _summon_cards(await _collect(adapter, _settings(temp_db_path))) == []


@pytest.mark.parametrize(
    ("seconds", "shown"),
    [
        (0, "0.0"),
        (179, "0.0"),  # 0.0497 h
        (180, "0.1"),  # 0.05 h exactly rounds half up
        (539, "0.1"),
        (540, "0.2"),
        (3600 * 10 - 181, "9.9"),
        (3600 * 10 - 180, "10.0"),
        (-60, "0.0"),  # clock skew: since in the future
    ],
)
def test_elapsed_hours_rounding_boundaries(seconds, shown):
    now = datetime(2026, 10, 29, tzinfo=timezone.utc)
    assert board.summon_elapsed_hours(now - timedelta(seconds=seconds), now) == shown


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("x\nSTOP: blocked-on pr:o/r#1 wake:Bohr\nNEXT: none", "STOP: blocked-on pr:o/r#1 wake:Bohr"),
        ("x\nNEXT: none", "STOP: …"),
        ("x\nNEXT: Bohr", None),
        ("NEXT: none\nmore text after", None),
        ("", None),
    ],
)
def test_stopped_marker_reads_only_the_last_line(content, expected):
    assert board._stopped_marker([{"content": content}]) == expected
    assert board._stopped_marker([]) is None
