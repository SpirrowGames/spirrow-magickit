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

from typing import Any

import pytest

from magickit.core import pr_watch
from magickit.core.pr_watch import (
    NON_APPROVED_REFETCH_SECONDS,
    SOFT_CAP_CALLS_PER_HOUR,
    PrSnapshot,
    PrWatchState,
    collect_pr_snapshots,
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

    snapshots, _ = await collect_pr_snapshots(
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

    snapshots, _ = await collect_pr_snapshots(
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
    snapshots, _ = await collect_pr_snapshots(
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
    snapshots, _ = await collect_pr_snapshots(
        [("O", "R")], state,
        fetch_open_prs=list_v2, fetch_reviews=reviews_v2, now=60.0,
    )
    assert snapshots[0].artifact_approved is False

    # t=60 + 15min - 1s: fallback timer has not fired; cache hit.
    list_v3, reviews_v3, counts3 = _make_fetchers(
        prs=[_pr(updated_at="U2", head="H")],
        reviews_by_pr={1: [_review("APPROVED", "H")]},  # replica caught up
    )
    snapshots, _ = await collect_pr_snapshots(
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
    snapshots, _ = await collect_pr_snapshots(
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
    snapshots, _ = await collect_pr_snapshots(
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
    snapshots, _ = await collect_pr_snapshots(
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
