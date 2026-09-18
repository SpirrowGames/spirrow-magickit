"""Tests for the GitHub PR watcher (:mod:`magickit.core.pr_watch`).

The board's マージ lane makes 「未依頼」visible per open PR (Bohr msg-829),
and this module answers the question "APPROVED @ head?" cheaply enough
that the board can poll every 5 minutes without draining the shared PAT.

The two properties every test here defends:

1. **Cache correctness under GitHub's replication lag.** The ``updated_at``
   key + 15-minute self-heal is the whole point (Einstein msg-822 → 828):
   a fetch fired in the read-replica window can cache stale ``[no
   reviews]`` under a bumped ``updated_at`` and never re-invalidate. We
   pin the recovery time at 15 minutes.

2. **Approved is sticky.** Once a PR is APPROVED @ current head, we do
   not refetch reviews until ``updated_at`` bumps. This is what keeps
   the steady-state cost bounded (msg-823 §5).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from magickit.core import pr_watch
from magickit.core.pr_watch import (
    NON_APPROVED_REFETCH_SECONDS,
    SOFT_CAP_CALLS_PER_HOUR,
    DeepPaginationOutcome,
    NegativeLedgerEntry,
    PrSnapshot,
    PrWatchState,
    add_minutes,
    clear_negative_ledger,
    collect_pr_snapshots,
    compute_was_truncated_per_pr,
    definitive_absence_threshold,
    find_ledger_in_page,
    get_ledger_pointer,
    get_negative_ledger,
    iter_truncated_prs,
    min_last_activity_at,
    pr_key,
    prune_ledger_pointers,
    prune_negative_ledger,
    prune_review_cache,
    resolve_old_prs_by_deep_pagination,
    set_ledger_pointer,
    set_negative_ledger,
)


def _pr(
    *,
    number: int = 1,
    updated_at: str = "2026-09-18T10:00:00Z",
    head: str = "sha-a",
    title: str = "example",
) -> dict[str, Any]:
    return {
        "number": number,
        "title": title,
        "html_url": f"https://github.com/O/R/pull/{number}",
        "head": {"sha": head},
        "updated_at": updated_at,
    }


def _review(state: str, commit_id: str, review_id: int = 100) -> dict[str, Any]:
    return {"state": state, "commit_id": commit_id, "id": review_id}


def _make_fetchers(
    prs: list[dict[str, Any]] | None = None,
    reviews_by_pr: dict[int, list[dict[str, Any]] | None] | None = None,
):
    """Return (list_fn, reviews_fn, call_counts).

    ``call_counts`` records how many times each fetcher was called, keyed
    by ``"list"`` and ``"reviews"``. Tests assert on these to prove that
    the cache actually skipped the fetch.
    """
    prs = prs or []
    reviews_by_pr = reviews_by_pr or {}
    counts = {"list": 0, "reviews": 0}

    async def _list(owner: str, repo: str) -> list[dict[str, Any]]:
        counts["list"] += 1
        return list(prs)

    async def _reviews(
        owner: str, repo: str, number: int
    ) -> list[dict[str, Any]] | None:
        counts["reviews"] += 1
        return reviews_by_pr.get(number)

    return _list, _reviews, counts


@pytest.mark.asyncio
async def test_approved_at_head_is_reflected_in_snapshot():
    """The predicate matches ``evaluate_ledger_verdict`` — same head SHA."""
    state = PrWatchState()
    list_fn, reviews_fn, counts = _make_fetchers(
        prs=[_pr(head="head-1")],
        reviews_by_pr={1: [_review("APPROVED", "head-1", review_id=42)]},
    )

    snapshots, _, _ = await collect_pr_snapshots(
        [("O", "R")], state,
        fetch_open_prs=list_fn, fetch_reviews=reviews_fn, now=0.0,
    )

    assert len(snapshots) == 1
    assert snapshots[0].artifact_approved is True
    assert snapshots[0].approving_review_id == 42
    assert counts["reviews"] == 1


@pytest.mark.asyncio
async def test_approve_at_earlier_head_does_not_count():
    """A review on an earlier commit judged a different diff."""
    state = PrWatchState()
    list_fn, reviews_fn, _ = _make_fetchers(
        prs=[_pr(head="head-new")],
        reviews_by_pr={1: [_review("APPROVED", "head-old")]},
    )

    snapshots, _, _ = await collect_pr_snapshots(
        [("O", "R")], state,
        fetch_open_prs=list_fn, fetch_reviews=reviews_fn, now=0.0,
    )

    assert snapshots[0].artifact_approved is False


@pytest.mark.asyncio
async def test_approved_snapshot_is_sticky_across_polls_without_bump():
    """No ``updated_at`` bump → cache hit → no ``/reviews`` refetch."""
    state = PrWatchState()
    list_fn, reviews_fn, counts = _make_fetchers(
        prs=[_pr(updated_at="U1", head="H")],
        reviews_by_pr={1: [_review("APPROVED", "H")]},
    )

    # First poll — populates cache.
    await collect_pr_snapshots(
        [("O", "R")], state,
        fetch_open_prs=list_fn, fetch_reviews=reviews_fn, now=0.0,
    )
    assert counts["reviews"] == 1

    # Second poll, 5 minutes later, same ``updated_at`` — no refetch.
    await collect_pr_snapshots(
        [("O", "R")], state,
        fetch_open_prs=list_fn, fetch_reviews=reviews_fn,
        now=5 * 60,
    )
    assert counts["reviews"] == 1, "APPROVED should be sticky without a bump"


@pytest.mark.asyncio
async def test_updated_at_bump_invalidates_cache_and_refetches():
    """The ``updated_at`` bump models any GitHub-side event (review / comment).

    Einstein msg-820: the head_sha would miss review submits; only
    ``updated_at`` picks them up.
    """
    state = PrWatchState()
    # First poll — non-approved.
    _, reviews_fn_first, _ = _make_fetchers()
    list_fn_v1, _, counts1 = _make_fetchers(
        prs=[_pr(updated_at="U1", head="H")],
        reviews_by_pr={1: []},
    )
    await collect_pr_snapshots(
        [("O", "R")], state,
        fetch_open_prs=list_fn_v1, fetch_reviews=reviews_fn_first, now=0.0,
    )

    # Second poll — a review has been submitted; ``updated_at`` bumped.
    list_fn_v2, reviews_fn_v2, counts2 = _make_fetchers(
        prs=[_pr(updated_at="U2", head="H")],
        reviews_by_pr={1: [_review("APPROVED", "H")]},
    )
    snapshots, _, _ = await collect_pr_snapshots(
        [("O", "R")], state,
        fetch_open_prs=list_fn_v2, fetch_reviews=reviews_fn_v2,
        now=60.0,
    )
    assert counts2["reviews"] == 1
    assert snapshots[0].artifact_approved is True


@pytest.mark.asyncio
async def test_replication_lag_self_heals_within_fifteen_minutes():
    """The Einstein msg-828 scenario: stale ``[no reviews]`` cached at bump.

    Trace:
    - t=0:   PR opened, first fetch caches ``[]`` under ``U1``.
    - t=60s: APPROVED review submitted; ``updated_at`` bumps to U2, but
      ``/reviews`` replica still returns ``[]``. Cache stores stale.
    - t=1000s (16.7min): no further bumps; the 15-min fallback fires
      the mandatory refetch and picks up the APPROVE.

    We prove the recovery window is bounded — the design promise the
    entire ``fetched_at`` field exists for.
    """
    state = PrWatchState()

    # t=0: empty reviews.
    list_v1, reviews_v1, _ = _make_fetchers(
        prs=[_pr(updated_at="U1", head="H")],
        reviews_by_pr={1: []},
    )
    await collect_pr_snapshots(
        [("O", "R")], state,
        fetch_open_prs=list_v1, fetch_reviews=reviews_v1, now=0.0,
    )

    # t=60: updated_at bumps but replica still stale.
    list_v2, reviews_v2, _ = _make_fetchers(
        prs=[_pr(updated_at="U2", head="H")],
        reviews_by_pr={1: []},  # still stale
    )
    snapshots, _, _ = await collect_pr_snapshots(
        [("O", "R")], state,
        fetch_open_prs=list_v2, fetch_reviews=reviews_v2, now=60.0,
    )
    assert snapshots[0].artifact_approved is False

    # t=60 + 15min - 1s: fallback timer has not fired; cache hit.
    list_v3, reviews_v3, counts3 = _make_fetchers(
        prs=[_pr(updated_at="U2", head="H")],
        reviews_by_pr={1: [_review("APPROVED", "H")]},  # replica caught up
    )
    snapshots, _, _ = await collect_pr_snapshots(
        [("O", "R")], state,
        fetch_open_prs=list_v3, fetch_reviews=reviews_v3,
        now=60.0 + NON_APPROVED_REFETCH_SECONDS - 1,
    )
    assert counts3["reviews"] == 0, "must not refetch inside the 15-min window"
    assert snapshots[0].artifact_approved is False, "still serving cached stale"

    # t=60 + 15min + 1s: fallback fires; picks up the APPROVE.
    list_v4, reviews_v4, counts4 = _make_fetchers(
        prs=[_pr(updated_at="U2", head="H")],
        reviews_by_pr={1: [_review("APPROVED", "H")]},
    )
    snapshots, _, _ = await collect_pr_snapshots(
        [("O", "R")], state,
        fetch_open_prs=list_v4, fetch_reviews=reviews_v4,
        now=60.0 + NON_APPROVED_REFETCH_SECONDS + 1,
    )
    assert counts4["reviews"] == 1, "15-min mandatory refetch must have fired"
    assert snapshots[0].artifact_approved is True


@pytest.mark.asyncio
async def test_rate_cap_marks_snapshot_and_serves_stale_when_available():
    """Once the hourly cap is hit, we do not lie: rate_capped is truthful."""
    state = PrWatchState()
    # Pre-fill call log with SOFT_CAP entries already recorded.
    for i in range(SOFT_CAP_CALLS_PER_HOUR):
        state.call_log.append(float(i))

    list_fn, reviews_fn, counts = _make_fetchers(
        prs=[_pr()],
        reviews_by_pr={1: [_review("APPROVED", "sha-a")]},
    )
    # Bump now so pruning does not clear the cap.
    snapshots, _, _ = await collect_pr_snapshots(
        [("O", "R")], state,
        fetch_open_prs=list_fn, fetch_reviews=reviews_fn,
        now=float(SOFT_CAP_CALLS_PER_HOUR) - 1.0,
    )
    # The list call is still made (that's the primary index refresh);
    # the /reviews call is skipped.
    assert counts["reviews"] == 0, "rate cap must have suppressed the /reviews fetch"
    assert snapshots[0].rate_capped is True
    assert snapshots[0].artifact_approved is False


@pytest.mark.asyncio
async def test_missing_head_sha_or_updated_at_skips_the_pr():
    """Malformed payload → snapshot skipped rather than a card with a fake id."""
    state = PrWatchState()
    list_fn, reviews_fn, _ = _make_fetchers(
        prs=[
            {"number": 1, "title": "no head", "updated_at": "U", "head": {}},
            {"number": 2, "title": "no upd", "head": {"sha": "H"}},
            _pr(number=3, head="H3"),
        ],
        reviews_by_pr={3: [_review("APPROVED", "H3")]},
    )
    snapshots, _, _ = await collect_pr_snapshots(
        [("O", "R")], state,
        fetch_open_prs=list_fn, fetch_reviews=reviews_fn, now=0.0,
    )
    assert [s.number for s in snapshots] == [3]


@pytest.mark.asyncio
async def test_call_log_prunes_after_an_hour():
    """Rolling window: entries older than 1 hour drop out."""
    state = PrWatchState()
    # Two calls, both an hour + 1s ago.
    state.call_log.extend([0.0, 1.0])

    list_fn, reviews_fn, _ = _make_fetchers(
        prs=[_pr()],
        reviews_by_pr={1: [_review("APPROVED", "sha-a")]},
    )
    await collect_pr_snapshots(
        [("O", "R")], state,
        fetch_open_prs=list_fn, fetch_reviews=reviews_fn,
        now=3602.0,  # 1 second past the strict > 1-hour cutoff for both entries
    )
    # The 2 stale entries pruned; only the 2 new ones remain (list + reviews).
    assert len(state.call_log) == 2


# --- outage sentinel & notice ----------------------------------------------


@pytest.mark.asyncio
async def test_fetch_failure_emits_notice_not_silent_dropdown():
    """A GitHub outage must announce itself, not silently blank the lane.

    Prior behaviour returned [] on exception → indistinguishable from
    "no open PRs". The sentinel path emits a notice so the operator
    learns the merge lane is degraded.
    """
    state = PrWatchState()

    async def failing_list(owner: str, repo: str):
        return pr_watch._FETCH_FAILED

    async def _rev(*_a, **_kw):
        return None

    snapshots, notices, _ = await collect_pr_snapshots(
        [("O", "R")], state,
        fetch_open_prs=failing_list, fetch_reviews=_rev, now=0.0,
    )
    assert snapshots == []
    assert any("縮退" in n for n in notices), notices


@pytest.mark.asyncio
async def test_empty_list_is_silent():
    """A truthy-empty list means "no open PRs" and should NOT emit a notice."""
    state = PrWatchState()

    async def empty_list(owner: str, repo: str):
        return []

    async def _rev(*_a, **_kw):
        return None

    snapshots, notices, _ = await collect_pr_snapshots(
        [("O", "R")], state,
        fetch_open_prs=empty_list, fetch_reviews=_rev, now=0.0,
    )
    assert snapshots == []
    assert notices == []


# --- helpers: cache keys, positive & negative ledger cache -----------------


def _snapshot(
    *,
    owner: str = "O",
    repo: str = "R",
    number: int = 1,
    updated_at: str = "2026-09-18T10:00:00Z",
    created_at: str = "2026-09-01T00:00:00Z",
) -> PrSnapshot:
    return PrSnapshot(
        owner=owner, repo=repo, number=number,
        title=f"PR {number}",
        html_url=f"https://github.com/{owner}/{repo}/pull/{number}",
        head_sha="H", updated_at=updated_at, created_at=created_at,
        artifact_approved=False,
    )


def test_pr_key_reduces_snapshot_to_tuple():
    key = pr_key(_snapshot(owner="foo", repo="bar", number=7))
    assert key == ("foo", "bar", 7)


def test_set_and_get_ledger_pointer_roundtrip():
    state = PrWatchState()
    key = ("O", "R", 1)
    assert get_ledger_pointer(state, key) is None
    set_ledger_pointer(state, key, "proj", "T-1", "U")
    assert get_ledger_pointer(state, key) == ("proj", "T-1")


NOW = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)


def test_negative_cache_returns_entry_when_ttl_and_updated_at_match():
    state = PrWatchState()
    set_negative_ledger(state, ("O", "R", 1), "U1", NOW, was_truncated=False)
    entry = get_negative_ledger(state, ("O", "R", 1), NOW, "U1")
    assert entry is not None
    assert entry.was_truncated is False


def test_negative_cache_invalidates_on_updated_at_change():
    state = PrWatchState()
    set_negative_ledger(state, ("O", "R", 1), "U1", NOW, was_truncated=False)
    # Same time, but the PR's updated_at bumped — must return None.
    assert get_negative_ledger(state, ("O", "R", 1), NOW, "U2") is None


def test_negative_cache_expires_after_ttl():
    state = PrWatchState()
    set_negative_ledger(state, ("O", "R", 1), "U1", NOW, was_truncated=False)
    later = NOW + timedelta(minutes=16)
    assert get_negative_ledger(state, ("O", "R", 1), later, "U1") is None


def test_iter_truncated_prs_lists_only_truncated_entries_by_slug():
    state = PrWatchState()
    set_negative_ledger(state, ("O", "R", 1), "U", NOW, was_truncated=True)
    set_negative_ledger(state, ("O", "R", 2), "U", NOW, was_truncated=False)
    set_negative_ledger(state, ("O", "X", 5), "U", NOW, was_truncated=True)
    out = iter_truncated_prs(state, now=NOW)
    assert out == {"O/R": [1], "O/X": [5]}


# --- helpers: ISO arithmetic & absence threshold ---------------------------


def test_add_minutes_produces_iso_utc_with_z_suffix():
    result = add_minutes("2026-09-18T10:00:00Z", timedelta(minutes=5))
    assert result == "2026-09-18T10:05:00Z"


def test_add_minutes_handles_negative_delta():
    result = add_minutes("2026-09-18T10:05:00Z", timedelta(minutes=-5))
    assert result == "2026-09-18T10:00:00Z"


def test_definitive_absence_threshold_ADDS_the_margin():
    """The safety direction (msg-859 Option xxvii): threshold > horizon.

    ADDING the margin makes the fence *harder* to satisfy — safer.
    SUBTRACTING would produce false-positive absence for genuinely-
    gated PRs. Regression pin: if this test fails, someone flipped the
    sign and gated PRs will silently render as 「未依頼」.
    """
    horizon = "2026-09-18T10:00:00Z"
    threshold = definitive_absence_threshold(horizon, timedelta(minutes=5))
    assert threshold == "2026-09-18T10:05:00Z", (
        "threshold must ADD the margin; subtracting is the fatal correctness "
        "breach (silent 未依頼 for gated PRs)."
    )


def test_min_last_activity_at_empty_returns_none():
    assert min_last_activity_at([]) is None


def test_min_last_activity_at_finds_smallest_iso_string():
    items = [
        {"last_activity_at": "2026-09-18T10:00:00Z"},
        {"last_activity_at": "2026-09-18T09:00:00Z"},
        {"last_activity_at": "2026-09-18T11:00:00Z"},
    ]
    assert min_last_activity_at(items) == "2026-09-18T09:00:00Z"


def test_min_last_activity_at_ignores_missing_field():
    items = [
        {"last_activity_at": "2026-09-18T10:00:00Z"},
        {},  # no field
        {"last_activity_at": "2026-09-18T09:00:00Z"},
    ]
    assert min_last_activity_at(items) == "2026-09-18T09:00:00Z"


# --- helpers: find_ledger_in_page -------------------------------------------


def _thread(
    *, thread_id: str = "T-1", title: str = "O/R#1",
    owner: str = "orchestrator", tags=("pr-review",),
    last_activity_at: str = "2026-09-01T00:00:00Z",
    status: str = "resolved",
) -> dict[str, Any]:
    return {
        "thread_id": thread_id, "title": title, "owner": owner,
        "tags": list(tags), "last_activity_at": last_activity_at,
        "status": status,
    }


def test_find_ledger_in_page_matches_by_owner_repo_number():
    snap = _snapshot(number=1)
    items = [
        _thread(title="unrelated"),
        _thread(title="PR review for O/R#1"),
    ]
    match = find_ledger_in_page(snap, items)
    assert match is not None and match["title"] == "PR review for O/R#1"


def test_find_ledger_in_page_ignores_wrong_owner():
    snap = _snapshot()
    items = [_thread(owner="someone-else", title="O/R#1")]
    assert find_ledger_in_page(snap, items) is None


def test_find_ledger_in_page_ignores_missing_tag():
    snap = _snapshot()
    items = [_thread(tags=(), title="O/R#1")]
    assert find_ledger_in_page(snap, items) is None


def test_find_ledger_in_page_returns_none_when_no_match():
    snap = _snapshot(number=99)
    items = [_thread(title="PR review for O/R#1")]
    assert find_ledger_in_page(snap, items) is None


# --- helpers: compute_was_truncated_per_pr ---------------------------------


def test_was_truncated_false_when_total_matches_items():
    prs = [_snapshot(number=1), _snapshot(number=2)]
    result = compute_was_truncated_per_pr(
        prs, {"items": [_thread(title="unrelated")], "total": 1}
    )
    assert result == {("O", "R", 1): False, ("O", "R", 2): False}


def test_was_truncated_false_when_pr_created_after_horizon_plus_margin():
    prs = [_snapshot(number=1, created_at="2026-09-18T10:15:00Z")]
    result = compute_was_truncated_per_pr(
        prs,
        {
            "items": [_thread(last_activity_at="2026-09-18T10:00:00Z")],
            "total": 500,
        },
    )
    # 10:15 > 10:05 (10:00 + 5m margin) → definitive absence.
    assert result[("O", "R", 1)] is False


def test_was_truncated_true_when_pr_older_than_horizon():
    prs = [_snapshot(number=1, created_at="2026-08-01T00:00:00Z")]
    result = compute_was_truncated_per_pr(
        prs,
        {
            "items": [_thread(last_activity_at="2026-09-18T10:00:00Z")],
            "total": 500,
        },
    )
    assert result[("O", "R", 1)] is True


def test_was_truncated_margin_boundary_flips_at_five_minutes():
    """The 5-min margin: PR 1 min newer than horizon must stay ambiguous.

    Regression pin (Option xxvii): if the margin direction is flipped,
    this PR would be falsely declared definitively-absent.
    """
    prs_inside = [_snapshot(number=1, created_at="2026-09-18T10:01:00Z")]
    prs_outside = [_snapshot(number=2, created_at="2026-09-18T10:10:00Z")]
    payload = {
        "items": [_thread(last_activity_at="2026-09-18T10:00:00Z")],
        "total": 500,
    }
    assert compute_was_truncated_per_pr(prs_inside, payload) == {
        ("O", "R", 1): True,
    }, "1 min newer than horizon is WITHIN the 5-min margin → ambiguous"
    assert compute_was_truncated_per_pr(prs_outside, payload) == {
        ("O", "R", 2): False,
    }, "10 min newer than horizon is OUTSIDE the margin → definitive"


# --- helpers: DeepPaginationOutcome invariants -----------------------------


def test_deep_pagination_outcome_rejects_zero_fields():
    with pytest.raises(ValueError):
        DeepPaginationOutcome()


def test_deep_pagination_outcome_rejects_two_fields():
    with pytest.raises(ValueError):
        DeepPaginationOutcome(
            definitive_absence=True, bounded_ambiguity=True,
        )


def test_deep_pagination_outcome_accepts_exactly_one_field():
    # Any one of the three is fine.
    DeepPaginationOutcome(found=("p", {}))
    DeepPaginationOutcome(definitive_absence=True)
    DeepPaginationOutcome(bounded_ambiguity=True)


# --- helpers: resolve_old_prs_by_deep_pagination ---------------------------


class _FakeListThreads:
    """Programmable list_threads mock keyed by offset.

    Each call returns ``pages[offset]`` (must be seeded); records the
    (project, offset) tuple so tests can assert on the call sequence.
    """

    def __init__(self, pages: dict[int, dict[str, Any]]):
        self.pages = pages
        self.calls: list[tuple[str, int]] = []

    async def __call__(self, *, project, owner, status_filter, limit, offset):
        self.calls.append((project, offset))
        return self.pages.get(offset, {"items": [], "total": 0})


@pytest.mark.asyncio
async def test_deep_pagination_finds_ledger_on_later_page():
    # Old PR, ledger sits on page 3 (offset=200).
    old_pr = _snapshot(number=1, created_at="2026-01-01T00:00:00Z")
    list_fn = _FakeListThreads({
        100: {
            "items": [_thread(title="unrelated#5",
                              last_activity_at="2026-05-01T00:00:00Z")],
            "total": 500,
        },
        200: {
            "items": [
                _thread(title="PR review for O/R#1", thread_id="T-1",
                        last_activity_at="2026-03-01T00:00:00Z"),
            ],
            "total": 500,
        },
    })
    outcomes = await resolve_old_prs_by_deep_pagination(
        [old_pr], "proj", list_fn, max_pages=5,
    )
    outcome = outcomes[("O", "R", 1)]
    assert outcome.found is not None
    assert outcome.found[1]["thread_id"] == "T-1"


@pytest.mark.asyncio
async def test_deep_pagination_definitive_when_pool_exhausted():
    """total <= offset+len(items) exhausts the pool → definitive absence."""
    old_pr = _snapshot(number=1, created_at="2026-01-01T00:00:00Z")
    list_fn = _FakeListThreads({
        100: {"items": [], "total": 100},  # offset=100, no items, total=100
    })
    outcomes = await resolve_old_prs_by_deep_pagination(
        [old_pr], "proj", list_fn, max_pages=5,
    )
    assert outcomes[("O", "R", 1)].definitive_absence is True


@pytest.mark.asyncio
async def test_deep_pagination_bounded_ambiguity_at_max_pages():
    """max_pages=1 with an unresolved PR yields bounded_ambiguity.

    The PR is older than the page horizon (so the chronological fence
    does NOT fire), and the pool is not exhausted. With ``max_pages=1``
    the scan runs out before proving anything.
    """
    old_pr = _snapshot(number=1, created_at="2020-01-01T00:00:00Z")
    list_fn = _FakeListThreads({
        100: {
            "items": [_thread(title="unrelated#9",
                              last_activity_at="2026-05-01T00:00:00Z")],
            "total": 10000,
        },
    })
    outcomes = await resolve_old_prs_by_deep_pagination(
        [old_pr], "proj", list_fn, max_pages=1,
    )
    assert outcomes[("O", "R", 1)].bounded_ambiguity is True


@pytest.mark.asyncio
async def test_deep_pagination_stops_early_on_chronological_fence():
    """A PR newer than a mid-scan page's horizon exits with definitive.

    Once the scan reaches a page whose oldest thread predates the PR
    (plus margin), the PR would be on this page or an earlier one; not
    being there proves absence and we do not fetch further pages.
    """
    old_pr = _snapshot(number=1, created_at="2026-05-01T00:00:00Z")
    list_fn = _FakeListThreads({
        100: {
            "items": [_thread(title="unrelated#5",
                              last_activity_at="2026-04-01T00:00:00Z")],
            "total": 10000,
        },
    })
    outcomes = await resolve_old_prs_by_deep_pagination(
        [old_pr], "proj", list_fn, max_pages=5,
    )
    # 2026-05-01 > 2026-04-01 + 5min → definitive.
    assert outcomes[("O", "R", 1)].definitive_absence is True
    # Should have stopped after page 2 without touching page 3.
    assert list_fn.calls == [("proj", 100)]


# --- PR-gate objection at 916df27: zombie notices & cache-key restructure --


def test_clear_negative_ledger_is_idempotent():
    """Calling clear on a key with no entry must not raise."""
    state = PrWatchState()
    clear_negative_ledger(state, ("O", "R", 1))  # no-op
    set_negative_ledger(state, ("O", "R", 1), "U", NOW, was_truncated=True)
    clear_negative_ledger(state, ("O", "R", 1))
    assert ("O", "R", 1) not in state.negative_ledger_cache
    # Idempotent second call.
    clear_negative_ledger(state, ("O", "R", 1))


def test_iter_truncated_prs_hides_ttl_expired_entries():
    """PR-gate BLOCKING #1: an entry past its TTL must NOT emit a notice.

    Without this, a PR that briefly caused bounded_ambiguity would keep
    its notice on the board long after the underlying condition resolved
    (or the PR closed).
    """
    state = PrWatchState()
    set_negative_ledger(
        state, ("O", "R", 1), "U", NOW, was_truncated=True,
    )
    # 16 minutes later — past the 15-minute TTL.
    later = NOW + timedelta(minutes=16)
    assert iter_truncated_prs(state, now=later) == {}


def test_iter_truncated_prs_filters_by_live_keys_when_provided():
    """PR-gate BLOCKING #1 (b): a PR no longer live must not emit a notice.

    If ``list_pull_requests`` no longer includes a PR (merged / closed /
    de-listed), its lingering negative cache entry must not produce a
    board notice on the next render.
    """
    state = PrWatchState()
    set_negative_ledger(
        state, ("O", "R", 1), "U", NOW, was_truncated=True,
    )
    set_negative_ledger(
        state, ("O", "R", 2), "U", NOW, was_truncated=True,
    )
    # Only PR #1 is live this cycle.
    out = iter_truncated_prs(
        state, now=NOW, live_keys={("O", "R", 1)},
    )
    assert out == {"O/R": [1]}


def test_iter_truncated_prs_live_keys_none_disables_liveness_filter():
    """``live_keys=None`` keeps TTL only (backward compatibility)."""
    state = PrWatchState()
    set_negative_ledger(
        state, ("O", "R", 1), "U", NOW, was_truncated=True,
    )
    assert iter_truncated_prs(state, now=NOW, live_keys=None) == {"O/R": [1]}


def test_prune_negative_ledger_drops_expired_and_offline_entries():
    """The pruning invariant: after prune, cache holds only live & fresh."""
    state = PrWatchState()
    set_negative_ledger(
        state, ("O", "R", 1), "U", NOW, was_truncated=True,
    )
    set_negative_ledger(
        state, ("O", "R", 2), "U", NOW, was_truncated=True,
    )
    # Entry for an already-expired PR (TTL past).
    set_negative_ledger(
        state, ("O", "R", 3), "U",
        NOW - timedelta(minutes=20),
        was_truncated=True,
    )
    prune_negative_ledger(
        state, now=NOW, live_keys={("O", "R", 1)},
    )
    # #2 pruned (not live), #3 pruned (expired), #1 kept.
    assert set(state.negative_ledger_cache) == {("O", "R", 1)}


def test_cache_entry_updated_at_is_inside_entry_not_in_key():
    """PR-gate ADVISORY: cache key stays stable at (owner, repo, number).

    The regression: keying by ``(owner, repo, number, updated_at)`` used
    to (a) leak memory (one new entry per PR event) and (b) break the
    stale-fallback under rate cap (the lookup missed the moment
    ``updated_at`` bumped). Pin: the state's cache is keyed by the
    3-tuple, and the stored entry carries its own ``updated_at``.
    """
    state = PrWatchState()
    # Simulate two polls of the same PR at different updated_at values.
    from magickit.core.pr_watch import _CacheEntry
    key: pr_watch.LedgerKey = ("O", "R", 1)
    state.cache[key] = _CacheEntry(
        updated_at="U1", reviews=[], artifact_approved=False,
        approving_review_id=None, fetched_at=0.0,
    )
    state.cache[key] = _CacheEntry(
        updated_at="U2", reviews=[], artifact_approved=True,
        approving_review_id=42, fetched_at=1.0,
    )
    # Only one entry per PR, regardless of updated_at churn.
    assert len(state.cache) == 1
    entry = state.cache[key]
    assert entry.updated_at == "U2"
    assert entry.artifact_approved is True


@pytest.mark.asyncio
async def test_rate_cap_serves_stale_entry_after_updated_at_bump():
    """PR-gate ADVISORY: rate-cap fallback must be reachable across bumps.

    Under the old key format, once ``updated_at`` bumped the cached
    entry (keyed by the OLD value) became unreachable, so a rate-capped
    poll for an APPROVED PR would falsely show it as un-approved.
    """
    state = PrWatchState()
    # Pre-populate cache with an APPROVED entry under U1.
    from magickit.core.pr_watch import _CacheEntry
    state.cache[("O", "R", 1)] = _CacheEntry(
        updated_at="U1",
        reviews=[{"state": "APPROVED", "commit_id": "H"}],
        artifact_approved=True,
        approving_review_id=42,
        fetched_at=0.0,
    )
    # Fill call log to trigger rate cap.
    for i in range(SOFT_CAP_CALLS_PER_HOUR):
        state.call_log.append(float(i))

    async def _list(owner: str, repo: str):
        # Same PR, but updated_at bumped to U2.
        return [{
            "number": 1, "title": "t",
            "html_url": "https://github.com/O/R/pull/1",
            "head": {"sha": "H"}, "updated_at": "U2",
            "created_at": "2026-09-01T00:00:00Z",
        }]

    async def _reviews(*_a, **_kw):
        # Never called (rate cap suppresses).
        raise AssertionError("must not fetch reviews under rate cap")

    snapshots, _, _ = await collect_pr_snapshots(
        [("O", "R")], state,
        fetch_open_prs=_list, fetch_reviews=_reviews,
        now=float(SOFT_CAP_CALLS_PER_HOUR) - 1.0,
    )
    assert len(snapshots) == 1
    # The card correctly falls back to the cached APPROVED verdict even
    # though updated_at changed — the old-format bug would have made
    # this False (unreachable cache).
    assert snapshots[0].artifact_approved is True
    assert snapshots[0].rate_capped is True


# --- PR-gate objection at 2c92dd9: per-PR memory leak in cache & pointers --


def test_prune_review_cache_drops_non_live_entries():
    """PR-gate ADVISORY #3: merged/closed PRs must be evicted from the
    review cache to bound long-term memory."""
    from magickit.core.pr_watch import _CacheEntry
    state = PrWatchState()
    state.cache[("O", "R", 1)] = _CacheEntry(
        updated_at="U", reviews=[], artifact_approved=False,
        approving_review_id=None, fetched_at=0.0,
    )
    state.cache[("O", "R", 2)] = _CacheEntry(
        updated_at="U", reviews=[], artifact_approved=False,
        approving_review_id=None, fetched_at=0.0,
    )
    prune_review_cache(state, live_keys={("O", "R", 1)})
    assert set(state.cache) == {("O", "R", 1)}


def test_prune_ledger_pointers_drops_non_live_entries():
    """Symmetric with review cache: closed PRs cannot reuse pointers."""
    state = PrWatchState()
    set_ledger_pointer(state, ("O", "R", 1), "proj", "T-1", "U")
    set_ledger_pointer(state, ("O", "R", 2), "proj", "T-2", "U")
    prune_ledger_pointers(state, live_keys={("O", "R", 1)})
    assert set(state.ledger_pointers) == {("O", "R", 1)}


def test_prune_review_cache_empty_live_keys_drops_everything():
    """If no PRs are live, the entire cache empties (bounded upper limit)."""
    from magickit.core.pr_watch import _CacheEntry
    state = PrWatchState()
    for n in range(5):
        state.cache[("O", "R", n)] = _CacheEntry(
            updated_at="U", reviews=[], artifact_approved=False,
            approving_review_id=None, fetched_at=0.0,
        )
    prune_review_cache(state, live_keys=set())
    assert state.cache == {}


@pytest.mark.asyncio
async def test_rate_cap_drops_stale_approval_when_head_moved():
    """PR-gate BLOCKING (26d0634 §2): rate-cap must NOT pair a new
    head_sha with the old cached approval. Re-verify against current
    head; if the approve was on an earlier commit, drop it."""
    state = PrWatchState()
    from magickit.core.pr_watch import _CacheEntry
    # Cached approve on OLD head.
    state.cache[("O", "R", 1)] = _CacheEntry(
        updated_at="U1",
        reviews=[{"state": "APPROVED", "commit_id": "H-OLD"}],
        artifact_approved=True,
        approving_review_id=42,
        fetched_at=0.0,
    )
    for i in range(SOFT_CAP_CALLS_PER_HOUR):
        state.call_log.append(float(i))

    async def _list(owner, repo):
        # New commit pushed → head_sha = H-NEW, updated_at = U2.
        return [{
            "number": 1, "title": "t",
            "html_url": "https://github.com/O/R/pull/1",
            "head": {"sha": "H-NEW"}, "updated_at": "U2",
            "created_at": "2026-09-01T00:00:00Z",
        }]

    async def _reviews(*_a, **_kw):
        raise AssertionError("must not fetch under rate cap")

    snapshots, _, _ = await collect_pr_snapshots(
        [("O", "R")], state,
        fetch_open_prs=_list, fetch_reviews=_reviews,
        now=float(SOFT_CAP_CALLS_PER_HOUR) - 1.0,
    )
    assert len(snapshots) == 1
    # The new head has no APPROVE on it → must NOT be approved.
    assert snapshots[0].artifact_approved is False
    assert snapshots[0].rate_capped is True
    assert snapshots[0].head_sha == "H-NEW"


def test_compute_was_truncated_treats_missing_total_as_ambiguous():
    """PR-gate BLOCKING (7d5aa57 §2): a missing/non-int `total` must NOT
    silently declare definitive absence — it means "cannot determine",
    which is ambiguous by definition."""
    prs = [_snapshot(number=1, created_at="2026-09-18T11:00:00Z")]
    # No `total` key at all → all PRs ambiguous.
    assert compute_was_truncated_per_pr(prs, {"items": []}) == {
        ("O", "R", 1): True,
    }
    # `total` as a string → same (not a trusted int).
    assert compute_was_truncated_per_pr(
        prs, {"items": [], "total": "500"},
    ) == {("O", "R", 1): True}


@pytest.mark.asyncio
async def test_deep_pagination_missing_total_does_not_declare_exhaustion():
    """PR-gate BLOCKING (7d5aa57 §2): when `total` is missing on a page,
    the pool-exhausted check must be skipped — not fire with `total=0`."""
    old_pr = _snapshot(number=1, created_at="2020-01-01T00:00:00Z")
    calls: list[int] = []
    async def _list(**kwargs):
        calls.append(kwargs["offset"])
        # Non-empty page, no `total` key. Under the old fallback
        # `total=0`, `offset+len(items) >= 0` would fire on page 2 and
        # incorrectly declare definitive_absence.
        return {"items": [_thread(title="other",
                                  last_activity_at="2026-09-01T00:00:00Z")]}
    outcomes = await resolve_old_prs_by_deep_pagination(
        [old_pr], "proj", _list, max_pages=2,
    )
    # PR must remain bounded_ambiguity, not definitive_absence.
    assert outcomes[("O", "R", 1)].bounded_ambiguity is True
    # And the loop actually iterated through max_pages (2 calls).
    assert len(calls) == 2


def test_ledger_pointer_invalidated_on_pr_updated_at_bump():
    """PR-gate BLOCKING (fa7a7d3 §1): stale pointer must be ignored when
    the PR's updated_at bumps — a new commit may have created a
    replacement ledger the pointer no longer names."""
    state = PrWatchState()
    set_ledger_pointer(state, ("O", "R", 1), "proj", "T-old", "U1")
    # Same updated_at → hit.
    assert get_ledger_pointer(
        state, ("O", "R", 1), pr_updated_at="U1",
    ) == ("proj", "T-old")
    # Bumped updated_at → miss (pointer ignored, caller falls to Pass B).
    assert get_ledger_pointer(
        state, ("O", "R", 1), pr_updated_at="U2",
    ) is None
    # No updated_at gate → raw lookup still returns it.
    assert get_ledger_pointer(state, ("O", "R", 1)) == ("proj", "T-old")


def test_ledger_owner_and_tag_constants_come_from_pr_gate_ledger():
    """PR-gate ADVISORY (fa7a7d3 §2): the module must not maintain its
    own copies of the driver-side owner / tag constants."""
    from magickit.core.pr_watch import _LEDGER_OWNER, _LEDGER_TAG
    from magickit.mcp.pr_gate_ledger import (
        PR_GATE_THREAD_OWNER, PR_GATE_THREAD_TAG,
    )
    assert _LEDGER_OWNER is PR_GATE_THREAD_OWNER
    assert _LEDGER_TAG is PR_GATE_THREAD_TAG


def test_prune_functions_are_idempotent_on_empty_state():
    """Empty state → no-op (no exceptions)."""
    state = PrWatchState()
    prune_review_cache(state, live_keys={("O", "R", 1)})
    prune_ledger_pointers(state, live_keys={("O", "R", 1)})
    prune_negative_ledger(
        state, now=NOW, live_keys={("O", "R", 1)},
    )
    assert state.cache == {}
    assert state.ledger_pointers == {}
    assert state.negative_ledger_cache == {}
