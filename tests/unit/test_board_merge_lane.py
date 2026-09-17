"""Tests for the board's マージ (merge) lane.

Companion to ``test_board.py``. This file pins the 4-state UI-side
predicate (Bohr msg-829 §2 summary table): given a live open PR, what
does the card look like?

The blocking design honesty the msg-827 turn locked in: `Tier-C 決着`
never says 「approved」, and its primary link goes to chatroom, not
GitHub. The 「未依頼」 state is visible per PR, so nothing silently
merges (this is the whole reason the lane exists — msg-253 §1 measured
2 of the last 3 merges shipped without a gate artifact).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from magickit.config import Settings
from magickit.core import board_lanes, pr_watch
from magickit.core.pr_watch import PrSnapshot, PrWatchState
from magickit.deploy import records
from magickit.web import board

NOW = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)


class _FrozenClock(datetime):
    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return NOW.astimezone(tz) if tz is not None else NOW.replace(tzinfo=None)


@pytest.fixture(autouse=True)
def _freeze_clock(monkeypatch):
    monkeypatch.setattr(board, "datetime", _FrozenClock)
    monkeypatch.setattr(board_lanes, "datetime", _FrozenClock)


@pytest.fixture(autouse=True)
def _no_deploys(monkeypatch):
    store = AsyncMock()
    store.list_requests = lambda **_: []
    monkeypatch.setattr(records, "get_store", lambda: store)


@pytest.fixture(autouse=True)
def _isolated_pr_watch_state(monkeypatch):
    """Each test gets a fresh cache; without this they cross-contaminate."""
    monkeypatch.setattr(pr_watch, "_STATE", PrWatchState())


def _settings(db_path: str, *, repos=("O/R",)) -> Settings:
    return Settings(db_path=db_path, board_pr_repo_allowlist=list(repos))


def _adapter(
    *,
    threads=None,
    messages=None,
    summaries=None,
):
    """Minimal Conclair stand-in; mirrors ``test_board._adapter``."""
    adapter = AsyncMock()
    adapter.list_project_summaries.return_value = (
        summaries if summaries is not None else {"items": []}
    )
    threads = threads if threads is not None else {}
    messages = messages if messages is not None else {}

    async def _list_threads(*, project, **_):
        return {"items": threads.get(project, [])}

    async def _get_thread(*, project, thread_id, **_):
        return messages.get(thread_id, {"messages": []})

    adapter.list_threads.side_effect = _list_threads
    adapter.get_thread.side_effect = _get_thread
    adapter.get_loop_control.return_value = {
        "desired_state": "run",
        "configured": True,
        "observed_state": "run",
        "observed_at": NOW.isoformat().replace("+00:00", "Z"),
    }
    return adapter


def _snapshot(
    *,
    number: int = 1,
    approved: bool = False,
    head: str = "H",
    title: str = "PR title",
    updated_at: str = "2026-09-18T09:00:00Z",
    rate_capped: bool = False,
) -> PrSnapshot:
    return PrSnapshot(
        owner="O",
        repo="R",
        number=number,
        title=title,
        html_url=f"https://github.com/O/R/pull/{number}",
        head_sha=head,
        updated_at=updated_at,
        artifact_approved=approved,
        approving_review_id=None,
        rate_capped=rate_capped,
    )


def _patch_pr_watch(monkeypatch, snapshots: list[PrSnapshot], notices=()):
    """Replace ``collect_pr_snapshots`` with a fixed answer."""
    async def fake(*_a, **_kw):
        return list(snapshots), list(notices)
    monkeypatch.setattr(pr_watch, "collect_pr_snapshots", fake)


async def _collect(adapter, settings):
    with patch.object(board, "ChatroomAdapter", return_value=adapter):
        return await board.collect(settings, now=NOW)


def _merge_cards(context):
    return [
        c for column in context["columns"].values()
        for c in column if c.kind == "merge"
    ]


# --- state predicates ------------------------------------------------------


@pytest.mark.asyncio
async def test_no_ledger_and_no_approve_renders_unrequested(
    temp_db_path, monkeypatch
):
    """The prospective-side failure this whole feature exists to close.

    A PR opened without firing the naysayer must appear as 「未依頼」;
    it must NOT be silent (that is the failure mode from msg-253 §1).
    """
    _patch_pr_watch(monkeypatch, [_snapshot()])
    # No ledger threads for this PR (owner is not 'orchestrator').
    context = await _collect(_adapter(), _settings(temp_db_path))

    cards = _merge_cards(context)
    assert len(cards) == 1
    assert board.MERGE_STATE_UNREQUESTED in cards[0].fingerprint
    assert "gate 未依頼" in cards[0].detail


@pytest.mark.asyncio
async def test_approve_at_head_renders_gate_done_with_github_primary(
    temp_db_path, monkeypatch
):
    """The artifact path — primary link is the GitHub PR (next: merge)."""
    _patch_pr_watch(monkeypatch, [_snapshot(approved=True)])
    context = await _collect(_adapter(), _settings(temp_db_path))

    cards = _merge_cards(context)
    assert len(cards) == 1
    assert "gate 済" in cards[0].detail
    # Primary link points at GitHub PR; chatroom link is not required.
    assert cards[0].href.startswith("https://github.com/O/R/pull/")


@pytest.mark.asyncio
async def test_open_ledger_with_no_verdict_renders_in_progress(
    temp_db_path, monkeypatch
):
    """Ledger exists but neither approved-at-head nor settled → gate 進行中."""
    _patch_pr_watch(monkeypatch, [_snapshot()])
    ledger_thread = {
        "thread_id": "T-pr-review-1",
        "title": "PR review for O/R#1",
        "status": "active",
        "owner": "orchestrator",
        "tags": ["pr-review"],
        "last_msg_id": "msg-3",
    }
    adapter = _adapter(
        threads={"spirrow-r": [ledger_thread]},
        messages={
            "T-pr-review-1": {"messages": [
                {"msg_id": "msg-3", "author": "Bohr", "type": "report"}
            ]}
        },
    )

    context = await _collect(adapter, _settings(temp_db_path))

    cards = _merge_cards(context)
    assert len(cards) == 1
    assert board.MERGE_STATE_IN_PROGRESS in cards[0].fingerprint


@pytest.mark.asyncio
async def test_settled_open_ledger_renders_tier_c_with_chatroom_primary(
    temp_db_path, monkeypatch
):
    """PR #83 pathway — human decide on an open thread.

    Design honesty: subtitle says 「要確認」, primary link is chatroom
    (not GitHub), because we do not know whether the human authorized
    the merge — only that they ruled (Bohr msg-827).
    """
    _patch_pr_watch(monkeypatch, [_snapshot()])
    ledger_thread = {
        "thread_id": "T-pr-review-1",
        "title": "PR review for O/R#1",
        "status": "active",
        "owner": "orchestrator",
        "tags": ["pr-review"],
        "last_msg_id": "msg-4",
    }
    adapter = _adapter(
        threads={"spirrow-r": [ledger_thread]},
        messages={
            "T-pr-review-1": {"messages": [
                {"msg_id": "msg-3", "author": "einstein", "type": "review"},
                {"msg_id": "msg-4", "author": "human", "type": "decide"},
            ]}
        },
    )

    context = await _collect(adapter, _settings(temp_db_path))

    cards = _merge_cards(context)
    assert len(cards) == 1
    assert board.MERGE_STATE_TIER_C_SETTLED in cards[0].fingerprint
    # Subtitle names the ambiguity — never says 「approved」.
    assert "chatroom で確認" in cards[0].detail
    # Primary link is chatroom, not GitHub.
    assert cards[0].href.startswith("/ui/projects/")
    # GitHub is still available as a secondary link.
    assert any("GitHub" in link.label for link in cards[0].links)


@pytest.mark.asyncio
async def test_settled_thread_with_naysayer_after_decide_is_in_progress(
    temp_db_path, monkeypatch
):
    """A naysayer verdict re-opening after a human decide is NOT settled."""
    _patch_pr_watch(monkeypatch, [_snapshot()])
    ledger_thread = {
        "thread_id": "T-pr-review-1",
        "title": "PR review for O/R#1",
        "status": "active",
        "owner": "orchestrator",
        "tags": ["pr-review"],
        "last_msg_id": "msg-5",
    }
    adapter = _adapter(
        threads={"spirrow-r": [ledger_thread]},
        messages={
            "T-pr-review-1": {"messages": [
                {"msg_id": "msg-4", "author": "human", "type": "decide"},
                {"msg_id": "msg-5", "author": "einstein", "type": "review"},
            ]}
        },
    )

    context = await _collect(adapter, _settings(temp_db_path))

    cards = _merge_cards(context)
    assert board.MERGE_STATE_IN_PROGRESS in cards[0].fingerprint


# --- degradation & rate cap ------------------------------------------------


@pytest.mark.asyncio
async def test_rate_capped_snapshot_with_no_ledger_falls_to_in_progress(
    temp_db_path, monkeypatch
):
    """rate_capped + no ledger → 「gate 進行中」 (never falsely 「未依頼」).

    We could not read reviews this cycle, so we do NOT know the PR is
    un-requested. Rendering 「gate 進行中」 is the honest fallback that
    keeps the human clicking through instead of silently ignoring.
    """
    _patch_pr_watch(monkeypatch, [_snapshot(rate_capped=True)])
    context = await _collect(_adapter(), _settings(temp_db_path))

    cards = _merge_cards(context)
    assert len(cards) == 1
    assert board.MERGE_STATE_IN_PROGRESS in cards[0].fingerprint
    assert "rate-cap" in cards[0].detail


@pytest.mark.asyncio
async def test_empty_allowlist_disables_the_lane(temp_db_path, monkeypatch):
    """A repo allowlist of ``[]`` skips GitHub entirely — no calls, no cards."""
    called = []
    async def fake(*a, **_kw):
        called.append(1)
        return [], []
    monkeypatch.setattr(pr_watch, "collect_pr_snapshots", fake)

    context = await _collect(_adapter(), _settings(temp_db_path, repos=()))

    assert _merge_cards(context) == []
    assert called == []


@pytest.mark.asyncio
async def test_pr_watch_exception_degrades_only_the_merge_lane(
    temp_db_path, monkeypatch
):
    """A GitHub outage must not blank other lanes; notice records the failure."""
    async def boom(*_a, **_kw):
        raise RuntimeError("github down")
    monkeypatch.setattr(pr_watch, "collect_pr_snapshots", boom)

    context = await _collect(_adapter(), _settings(temp_db_path))

    assert _merge_cards(context) == []
    assert any("マージ待ち PR" in n for n in context["notices"])


# --- gone reason -----------------------------------------------------------


def test_merge_kind_gets_a_specific_gone_reason(temp_db_path):
    """When a merge card falls off (PR merged / closed), 完了列 says why."""
    from magickit.web.board import _Live, _gone_reason
    reason = _gone_reason(
        {"kind": "merge", "item_key": "merge:O/R#1"}, _Live()
    )
    assert "PR" in reason and "open でなくなり" in reason
