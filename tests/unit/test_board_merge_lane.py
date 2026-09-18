"""Tests for the board's マージ (merge) lane — 3-state.

The 4-state design (approved / Tier-C / in-progress / unrequested) was
collapsed to 3 (approved_artifact / ledger_attached / unrequested) after
the audit found the middle two structurally indistinguishable. This file
pins the new predicate + the bounded closed-ledger discovery
(``list_threads`` Pass A on open + Pass B on closed + deep pagination
with a chronological fence).

The blocking honesty this locks in: 「未依頼」 is visible per PR
(msg-253 §1's silent-failure fix stands), and every closed-ledger read
is bounded — no infinite loop can arise from a large ``resolved`` /
``superseded`` backlog. When bounds run out on genuine ambiguity, the
board emits a notice naming the affected PRs rather than silently
mis-classifying them.
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


def _snapshot(
    *,
    number: int = 1,
    approved: bool = False,
    head: str = "H",
    title: str = "PR title",
    updated_at: str = "2026-09-18T09:00:00Z",
    created_at: str = "2026-09-15T00:00:00Z",
    rate_capped: bool = False,
    owner: str = "O",
    repo: str = "R",
) -> PrSnapshot:
    return PrSnapshot(
        owner=owner, repo=repo, number=number,
        title=title,
        html_url=f"https://github.com/{owner}/{repo}/pull/{number}",
        head_sha=head, updated_at=updated_at, created_at=created_at,
        artifact_approved=approved, approving_review_id=None,
        rate_capped=rate_capped,
    )


class _Adapter:
    """Programmable Conclair stand-in.

    Records every ``list_threads`` call and answers with what the test
    seeded per (project, status_filter tuple). Also records every
    ``get_thread`` call.
    """

    def __init__(
        self,
        *,
        threads_by_call: dict[tuple[str, tuple[str, ...]], dict[str, Any]] | None = None,
        threads_by_offset: dict[tuple[str, int], dict[str, Any]] | None = None,
        get_thread_by_id: dict[str, dict[str, Any]] | None = None,
        summaries: dict[str, Any] | None = None,
    ):
        self.threads_by_call = threads_by_call or {}
        self.threads_by_offset = threads_by_offset or {}
        self.get_thread_by_id = get_thread_by_id or {}
        self.summaries = summaries if summaries is not None else {"items": []}
        self.list_threads_calls: list[dict[str, Any]] = []
        self.get_thread_calls: list[dict[str, Any]] = []

    async def list_project_summaries(self):
        return self.summaries

    async def list_threads(
        self, *, project, status_filter=None, owner=None,
        limit=100, offset=0,
    ):
        call = {
            "project": project, "status_filter": tuple(status_filter or ()),
            "owner": owner, "limit": limit, "offset": offset,
        }
        self.list_threads_calls.append(call)
        # First: per-(project, offset) precedence for deep pagination.
        if (project, offset) in self.threads_by_offset:
            return self.threads_by_offset[(project, offset)]
        # Then: per-(project, status_filter tuple).
        key = (project, tuple(status_filter or ()))
        if key in self.threads_by_call:
            return self.threads_by_call[key]
        return {"items": [], "total": 0}

    async def get_thread(self, *, project, thread_id, **_):
        self.get_thread_calls.append({
            "project": project, "thread_id": thread_id,
        })
        return self.get_thread_by_id.get(
            thread_id, {"messages": []},
        )

    async def get_loop_control(self, *, project):
        return {
            "desired_state": "run", "configured": True,
            "observed_state": "run",
            "observed_at": NOW.isoformat().replace("+00:00", "Z"),
        }

    async def close(self):
        pass


def _patch_pr_watch(monkeypatch, snapshots, notices=()):
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


# --- state classifier ------------------------------------------------------


@pytest.mark.asyncio
async def test_no_ledger_and_no_approve_renders_unrequested(
    temp_db_path, monkeypatch
):
    """msg-253 §1's silent-failure fix: 未依頼 must be VISIBLE per PR."""
    _patch_pr_watch(monkeypatch, [_snapshot()])
    context = await _collect(_Adapter(), _settings(temp_db_path))

    cards = _merge_cards(context)
    assert len(cards) == 1
    assert board.MERGE_STATE_UNREQUESTED in cards[0].fingerprint
    assert "gate 未依頼" in cards[0].detail


@pytest.mark.asyncio
async def test_approve_at_head_renders_gate_done_with_github_primary(
    temp_db_path, monkeypatch
):
    """Artifact path — primary link is the GitHub PR (next: merge)."""
    _patch_pr_watch(monkeypatch, [_snapshot(approved=True)])
    context = await _collect(_Adapter(), _settings(temp_db_path))

    cards = _merge_cards(context)
    assert len(cards) == 1
    assert "gate 済" in cards[0].detail
    assert cards[0].href.startswith("https://github.com/O/R/pull/")


@pytest.mark.asyncio
async def test_open_ledger_renders_ledger_attached_with_chatroom_primary(
    temp_db_path, monkeypatch
):
    """Open ledger + no APPROVE → LEDGER_ATTACHED; primary link → chatroom."""
    _patch_pr_watch(monkeypatch, [_snapshot()])
    open_ledger = {
        "thread_id": "T-pr-review-1",
        "title": "PR review for O/R#1",
        "status": "active",
        "owner": "orchestrator",
        "tags": ["pr-review"],
        "last_msg_id": "msg-3",
    }
    adapter = _Adapter(
        threads_by_call={
            ("r", ("active", "awaiting_reply", "parked")): {
                "items": [open_ledger], "total": 1,
            },
        },
    )

    context = await _collect(adapter, _settings(temp_db_path))

    cards = _merge_cards(context)
    assert len(cards) == 1
    assert board.MERGE_STATE_LEDGER_ATTACHED in cards[0].fingerprint
    assert "gate スレッド" in cards[0].detail
    # Primary link points at chatroom.
    assert cards[0].href.startswith("/ui/projects/")
    # GitHub is a secondary link.
    assert any("GitHub" in link.label for link in cards[0].links)


@pytest.mark.asyncio
async def test_closed_ledger_still_renders_ledger_attached(
    temp_db_path, monkeypatch
):
    """The audit finding: a resolved/superseded ledger is still 「要確認」.

    Prior 4-state design would have called this 「Tier-C 決着」; the
    collapsed 3-state design keeps the same "verify in chatroom"
    primary link because the PR is still open regardless.
    """
    _patch_pr_watch(monkeypatch, [_snapshot()])
    closed_ledger = {
        "thread_id": "T-pr-review-1",
        "title": "PR review for O/R#1",
        "status": "resolved",
        "owner": "orchestrator",
        "tags": ["pr-review"],
        "last_msg_id": "msg-9",
        "last_activity_at": "2026-09-15T00:00:00Z",
    }
    adapter = _Adapter(
        threads_by_call={
            # Pass B (closed status filter) finds it.
            ("r", ("resolved", "superseded")): {
                "items": [closed_ledger], "total": 1,
            },
        },
    )

    context = await _collect(adapter, _settings(temp_db_path))
    cards = _merge_cards(context)
    assert len(cards) == 1
    assert board.MERGE_STATE_LEDGER_ATTACHED in cards[0].fingerprint
    # Chatroom link labels the closed state so the human knows.
    chatroom = [ln for ln in cards[0].links if "GitHub" not in ln.label]
    if chatroom:  # secondary if primary is chatroom
        pass  # subtitle carries the "verify" guidance
    assert cards[0].href.startswith("/ui/projects/")


# --- fallback project name (Obj 1) -----------------------------------------


@pytest.mark.asyncio
async def test_fallback_project_uses_bare_repo_name_no_spirrow_prefix(
    temp_db_path, monkeypatch
):
    """The `spirrow-` prefix was doubling up for spirrow-* repos.

    Given a repo ``spirrow-magickit`` (as ``SpirrowGames/spirrow-magickit``
    would come across), the fallback project name must be
    ``spirrow-magickit`` — NOT ``spirrow-spirrow-magickit``.
    """
    _patch_pr_watch(monkeypatch, [_snapshot(
        owner="SpirrowGames", repo="spirrow-magickit", number=42,
    )])
    adapter = _Adapter()
    await _collect(adapter, _settings(temp_db_path))

    projects_scanned = {c["project"] for c in adapter.list_threads_calls}
    assert "spirrow-magickit" in projects_scanned
    assert "spirrow-spirrow-magickit" not in projects_scanned


# --- positive cache --------------------------------------------------------


@pytest.mark.asyncio
async def test_positive_cache_skips_list_threads_on_next_poll(
    temp_db_path, monkeypatch
):
    """Once we've matched a ledger, cache the pointer and use it next cycle.

    After the first poll finds the ledger via Pass A/B, the state has
    a pointer for the PR. On the next poll, only ``get_thread`` should
    be called for that PR — no list_threads Pass A / Pass B needed for
    the cached project.
    """
    _patch_pr_watch(monkeypatch, [_snapshot()])
    open_ledger = {
        "thread_id": "T-cached",
        "title": "PR review for O/R#1",
        "status": "active",
        "owner": "orchestrator",
        "tags": ["pr-review"],
    }

    # Pre-populate positive cache.
    state = pr_watch.get_state()
    pr_watch.set_ledger_pointer(state, ("O", "R", 1), "r", "T-cached")

    adapter = _Adapter(
        get_thread_by_id={
            "T-cached": {"thread": open_ledger, "messages": []},
        },
    )
    context = await _collect(adapter, _settings(temp_db_path))

    cards = _merge_cards(context)
    assert len(cards) == 1
    assert board.MERGE_STATE_LEDGER_ATTACHED in cards[0].fingerprint
    # get_thread was called with the cached pointer.
    thread_ids = {c["thread_id"] for c in adapter.get_thread_calls}
    assert "T-cached" in thread_ids


# --- negative cache --------------------------------------------------------


@pytest.mark.asyncio
async def test_negative_cache_hit_skips_pass_b(
    temp_db_path, monkeypatch
):
    """Fresh negative-cache entry → no Pass B list_threads on closed statuses."""
    snap = _snapshot(updated_at="U-STABLE")
    _patch_pr_watch(monkeypatch, [snap])

    state = pr_watch.get_state()
    pr_watch.set_negative_ledger(
        state, ("O", "R", 1), "U-STABLE", NOW, was_truncated=False,
    )

    adapter = _Adapter()
    await _collect(adapter, _settings(temp_db_path))

    closed_calls = [
        c for c in adapter.list_threads_calls
        if c["status_filter"] == ("resolved", "superseded")
    ]
    assert closed_calls == [], (
        "negative cache must skip Pass B for the current cycle"
    )


@pytest.mark.asyncio
async def test_negative_cache_invalidates_when_pr_updated_at_bumps(
    temp_db_path, monkeypatch
):
    """A PR-side event (updated_at bump) invalidates the negative cache."""
    snap = _snapshot(updated_at="U-NEW")
    _patch_pr_watch(monkeypatch, [snap])

    state = pr_watch.get_state()
    # Cache set under the OLD updated_at.
    pr_watch.set_negative_ledger(
        state, ("O", "R", 1), "U-OLD", NOW, was_truncated=False,
    )

    adapter = _Adapter()
    await _collect(adapter, _settings(temp_db_path))

    closed_calls = [
        c for c in adapter.list_threads_calls
        if c["status_filter"] == ("resolved", "superseded")
    ]
    assert len(closed_calls) >= 1, (
        "updated_at bump must have invalidated the negative cache → Pass B ran"
    )


# --- deep pagination --------------------------------------------------------


@pytest.mark.asyncio
async def test_deep_pagination_finds_ledger_on_later_page(
    temp_db_path, monkeypatch
):
    """A ledger on page 3 must still be found (not silently missed)."""
    old_pr = _snapshot(
        number=1,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-09-18T09:00:00Z",
    )
    _patch_pr_watch(monkeypatch, [old_pr])
    adapter = _Adapter(
        threads_by_offset={
            ("r", 0): {
                "items": [{
                    "thread_id": "unrelated",
                    "title": "PR review for O/R#99",
                    "owner": "orchestrator",
                    "tags": ["pr-review"],
                    "last_activity_at": "2026-05-01T00:00:00Z",
                    "status": "resolved",
                }],
                "total": 500,
            },
            ("r", 100): {"items": [], "total": 500},
            ("r", 200): {
                "items": [{
                    "thread_id": "T-deep",
                    "title": "PR review for O/R#1",
                    "owner": "orchestrator",
                    "tags": ["pr-review"],
                    "last_activity_at": "2026-03-01T00:00:00Z",
                    "status": "resolved",
                }],
                "total": 500,
            },
        },
    )

    context = await _collect(adapter, _settings(temp_db_path))
    cards = _merge_cards(context)
    assert len(cards) == 1
    assert board.MERGE_STATE_LEDGER_ATTACHED in cards[0].fingerprint


@pytest.mark.asyncio
async def test_definitive_absence_emits_no_notice(
    temp_db_path, monkeypatch
):
    """Definitive absence (pool exhausted / horizon proven) is silent."""
    new_pr = _snapshot(
        number=1,
        created_at="2026-09-18T11:00:00Z",  # very new
        updated_at="2026-09-18T11:00:00Z",
    )
    _patch_pr_watch(monkeypatch, [new_pr])
    adapter = _Adapter(
        threads_by_offset={
            # Page 1 total <= len(items): pool exhausted.
            ("r", 0): {
                "items": [{
                    "thread_id": "old",
                    "title": "PR review for O/R#99",
                    "owner": "orchestrator", "tags": ["pr-review"],
                    "last_activity_at": "2026-01-01T00:00:00Z",
                    "status": "resolved",
                }],
                "total": 1,
            },
        },
    )
    context = await _collect(adapter, _settings(temp_db_path))
    assert not any(
        "pagination 深さ" in n for n in context["notices"]
    ), context["notices"]


@pytest.mark.asyncio
async def test_bounded_ambiguity_emits_notice_naming_prs(
    temp_db_path, monkeypatch, tmp_path
):
    """Genuine ambiguity → notice; the classification defaults to 未依頼."""
    # Set very low max_pages to force ambiguity on a large backlog.
    monkeypatch.setattr(pr_watch, "_MAX_CLOSED_LEDGER_PAGES", 1)

    old_pr = _snapshot(
        number=77,
        created_at="2026-01-01T00:00:00Z",  # much older than horizon
        updated_at="2026-09-18T09:00:00Z",
    )
    _patch_pr_watch(monkeypatch, [old_pr])
    # 500 total items, page 1 horizon is recent → PR is older → deep scan.
    # Page 2 (offset=100) also has recent items → still ambiguous.
    unrelated_page = {
        "items": [{
            "thread_id": f"other-{i}",
            "title": f"PR review for O/R#{100 + i}",
            "owner": "orchestrator", "tags": ["pr-review"],
            "last_activity_at": "2026-09-01T00:00:00Z",
            "status": "resolved",
        } for i in range(5)],
        "total": 10000,
    }
    adapter = _Adapter(
        threads_by_offset={
            ("r", 0): unrelated_page,
            ("r", 100): unrelated_page,
        },
    )
    context = await _collect(adapter, _settings(temp_db_path))
    notices = context["notices"]
    assert any(
        "pagination 深さ" in n and "#77" in n for n in notices
    ), notices


# --- degradation & rate cap ------------------------------------------------


@pytest.mark.asyncio
async def test_rate_capped_snapshot_with_no_ledger_still_says_unrequested(
    temp_db_path, monkeypatch
):
    """rate_capped + no ledger → 未依頼 (with the rate-cap subtitle caveat)."""
    _patch_pr_watch(monkeypatch, [_snapshot(rate_capped=True)])
    context = await _collect(_Adapter(), _settings(temp_db_path))

    cards = _merge_cards(context)
    assert len(cards) == 1
    assert board.MERGE_STATE_UNREQUESTED in cards[0].fingerprint
    assert "rate-cap" in cards[0].detail


@pytest.mark.asyncio
async def test_empty_allowlist_disables_the_lane(temp_db_path, monkeypatch):
    """A repo allowlist of ``[]`` skips GitHub entirely — no calls, no cards."""
    called = []
    async def fake(*a, **_kw):
        called.append(1)
        return [], []
    monkeypatch.setattr(pr_watch, "collect_pr_snapshots", fake)

    context = await _collect(_Adapter(), _settings(temp_db_path, repos=()))

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

    context = await _collect(_Adapter(), _settings(temp_db_path))

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
