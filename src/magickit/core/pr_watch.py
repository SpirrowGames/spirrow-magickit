"""GitHub PR watcher for the board's ``マージ`` (merge) lane.

The board's other lanes read state that already lives in this process
(materials in SQLite, deploys on the file store, loops from ops). This one
reads the **outside world** — a list of open pull requests across a fixed
allowlist of repositories — and asks one question per PR: *did the gate
speak, and if so, did it approve?* The answer becomes a card.

Why this exists (T-merged-to-main-without-gate-artifact)
--------------------------------------------------------

The prospective failure mode this closes is silent: a PR opened without
firing the independent naysayer never makes any noise on any board, so a
human who wants to merge sees the GitHub UI's ``Ready to merge`` and merges
it. The ledger stream that would normally record the gate never opens
because nobody remembered to. Merging under this condition IS the failure —
the observation was measured on real merged PRs (msg-253 §1) and the fix is
to make the ``未依頼`` state visible per-PR, so no live PR can escape it.

Why polling GitHub, not chatroom, is the primary evidence
---------------------------------------------------------

The ledger stream is the *effect* of a fire; the PR itself is the *cause*.
Reading only the ledger stream would keep the ``未依頼`` case invisible
(the whole point of the failure is that the ledger stream is absent). So
this module owns two reads:

1. ``list_pull_requests`` per repo in the allowlist (``state=open``),
   yielding the live set of PRs. This is the source of truth for "what
   exists".
2. ``pull_request_read(get_reviews)`` per PR whose reviews we need,
   yielding the APPROVED-at-head predicate.

The ledger-side check (Tier-C settled) lives in
:mod:`magickit.mcp.pr_gate_ledger` as an independent predicate; the board
composes both.

Cache design (Einstein 7-turn naysayer, msg-820–828)
----------------------------------------------------

Cache key is ``(pr_number, updated_at)`` — **not** ``head_sha``. Reviews
are an event stream applied on top of code state; a review submitted after
a push leaves ``head_sha`` unchanged but bumps ``updated_at``. The naive
``head_sha`` key would permanently ignore new reviews on static heads.

Cache invalidation is **asymmetrical** on purpose:

- **APPROVED PRs are sticky.** ``updated_at`` bumps on any authorized
  transition away from APPROVED (dismiss / new push / RC submit) so we
  can trust the cache while it holds; a false-positive that flips back is
  caught at the GitHub UI when the human actually clicks merge.

- **Non-APPROVED PRs get a mandatory 15-minute refetch** regardless of
  cache hit. This is not an optimization; it is the self-healing mechanism
  for GitHub's read-replica lag between ``list_pull_requests`` (primary
  index) and ``/reviews`` (replica). Without it, a refetch fired in the
  brief window immediately after an APPROVED submit can cache ``[old
  reviews]`` under the new ``updated_at`` and never invalidate — because
  no further event will happen on a PR whose ledger thinks it is approved.
  The 15-minute cap bounds worst-case ``未依頼→gate 済`` latency to ~20
  minutes (Einstein msg-828 traced the arithmetic).

Rate limit ceiling
------------------

Cost budgeted at ~160-200 calls/hour steady state, burst ~350/hour. Hard
soft-cap at 500 calls/hour per process (:data:`SOFT_CAP_CALLS_PER_HOUR`);
when the ceiling is reached, subsequent PRs are marked ``rate_capped`` and
skip their review refetch this cycle — better to serve stale data than to
starve the other MCP operations (github_dispatch, MCP tools) sharing the
same PAT.

What this module does NOT know
------------------------------

- The chatroom ledger thread. Board wiring composes.
- How to render a card. That is :mod:`magickit.web.board`.
- Anything about repos not on the allowlist. Adding a repo is a config
  change, not a discovery one — "which repos matter" is a scope decision.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from magickit.utils.logging import get_logger

logger = get_logger(__name__)

#: 15-minute mandatory refetch window for non-approved PRs (see docstring).
NON_APPROVED_REFETCH_SECONDS = 15 * 60

#: Hard cap on GitHub API calls per rolling hour, per process. Chosen so
#: this module's typical burst load (~350/hour) is comfortably contained
#: while leaving headroom for the shared PAT's other consumers.
SOFT_CAP_CALLS_PER_HOUR = 500

#: The `state` value GitHub returns for an APPROVED review.
_APPROVED = "APPROVED"


@dataclass(frozen=True)
class PrSnapshot:
    """One open PR the board might render, plus its review verdict.

    ``artifact_approved`` answers **only** the ``gate 済 · artifact`` half
    of the board's 4-state classification. The other three states
    (``Tier-C 決着`` / ``gate 進行中`` / ``未依頼``) are composed by the
    board from the ledger side; this dataclass carries no chatroom
    knowledge on purpose.
    """

    owner: str
    repo: str
    number: int
    title: str
    html_url: str
    head_sha: str
    updated_at: str
    artifact_approved: bool
    approving_review_id: int | None = None
    #: True when the ``/reviews`` fetch was skipped due to the soft cap.
    #: Board treats this as ``gate 進行中`` unless the ledger says otherwise
    #: — safer than falsely claiming ``未依頼`` for a PR we could not read.
    rate_capped: bool = False

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}#{self.number}"


@dataclass
class _CacheEntry:
    """One ``(pr_number, updated_at)`` cache line.

    ``fetched_at`` is the wall-clock time of the ``/reviews`` fetch, used
    by the 15-minute mandatory refetch (see module docstring).
    """

    reviews: list[dict[str, Any]]
    artifact_approved: bool
    approving_review_id: int | None
    fetched_at: float


@dataclass
class PrWatchState:
    """Per-process state the watcher keeps between calls.

    Held as a plain dataclass (not a global singleton) so tests can pass
    a fresh one and production wires it once per process via the module
    ``_STATE`` sentinel used by :func:`collect_pr_snapshots`.
    """

    #: ``(owner, repo, number, updated_at)`` → cache entry.
    cache: dict[tuple[str, str, int, str], _CacheEntry] = field(
        default_factory=dict
    )
    #: Rolling window of API call timestamps (monotonic seconds).
    call_log: list[float] = field(default_factory=list)


def _first_json_payload(result: Any) -> Any:
    """Decode the first text block of an MCP result, or None.

    Mirrors :func:`magickit.mcp.pr_gate_ledger._first_json_payload`; the
    two must not diverge or the same upstream shape will parse differently
    depending on which module you ask.
    """
    content = result.get("content") if isinstance(result, dict) else None
    if not content:
        return None
    try:
        return json.loads(content[0].get("text", ""))
    except (ValueError, TypeError, AttributeError, IndexError, KeyError):
        return None


def _prune_call_log(state: PrWatchState, *, now: float | None = None) -> None:
    """Drop call log entries older than one hour."""
    now = now if now is not None else time.monotonic()
    cutoff = now - 3600.0
    state.call_log[:] = [t for t in state.call_log if t >= cutoff]


def _rate_capped(state: PrWatchState, *, now: float | None = None) -> bool:
    """True if the hourly soft cap has been reached this rolling window."""
    _prune_call_log(state, now=now)
    return len(state.call_log) >= SOFT_CAP_CALLS_PER_HOUR


def _record_call(state: PrWatchState, *, now: float | None = None) -> None:
    """Note an API call for the rolling-window cap."""
    state.call_log.append(now if now is not None else time.monotonic())


def _approved_at_head(
    reviews: list[dict[str, Any]], head_sha: str
) -> tuple[bool, int | None]:
    """Same predicate as ``pr_gate_ledger.evaluate_ledger_verdict``.

    APPROVED **and** ``commit_id == head`` — a review on an earlier commit
    judged a different diff. Kept in sync with the ledger's rule so the
    board and the carve-out cannot disagree on what "approved" means.
    """
    for r in reviews:
        if not isinstance(r, dict):
            continue
        if r.get("state") == _APPROVED and r.get("commit_id") == head_sha:
            review_id = r.get("id")
            return True, review_id if isinstance(review_id, int) else None
    return False, None


def _needs_review_refetch(
    entry: _CacheEntry | None, *, now: float | None = None
) -> bool:
    """Should we refetch reviews for a PR whose ``updated_at`` matched cache?

    Two conditions:

    - **No cache entry.** Trivially: fetch.
    - **Non-approved & 15 minutes stale.** The mandatory self-healing
      window (see module docstring). Approved entries are sticky —
      transitions away from APPROVED bump ``updated_at`` and invalidate
      the cache line before this check runs.
    """
    if entry is None:
        return True
    if entry.artifact_approved:
        return False
    now = now if now is not None else time.monotonic()
    return (now - entry.fetched_at) > NON_APPROVED_REFETCH_SECONDS


async def _fetch_open_prs(owner: str, repo: str) -> list[dict[str, Any]]:
    """``list_pull_requests(state=open)`` for one repo, or [] on failure.

    Failure returns [] rather than raising: one bad repo must not blank
    the entire board. The caller records a notice for the user.
    """
    from magickit.mcp.github_dispatch import _mcp_call, _resolve_pat  # noqa: PLC0415

    try:
        pat = _resolve_pat("GITHUB_MCP_PAT_IMPLEMENTER")
        result = await _mcp_call(
            "tools/call",
            {
                "name": "list_pull_requests",
                "arguments": {"owner": owner, "repo": repo, "state": "open"},
            },
            pat,
        )
    except Exception as exc:  # noqa: BLE001 — any read failure yields []
        logger.warning(
            "pr_watch: list_pull_requests failed", repo=f"{owner}/{repo}", err=str(exc)
        )
        return []
    payload = _first_json_payload(result)
    if isinstance(payload, list):
        return [p for p in payload if isinstance(p, dict)]
    # Some backends wrap under {"items": [...]}. Accept either shape.
    if isinstance(payload, dict):
        items = payload.get("items")
        if isinstance(items, list):
            return [p for p in items if isinstance(p, dict)]
    return []


async def _fetch_reviews(
    owner: str, repo: str, number: int
) -> list[dict[str, Any]] | None:
    """``pull_request_read(get_reviews)`` for one PR, or None on failure.

    None is distinct from ``[]`` here: ``[]`` means "we asked and there
    genuinely are no reviews yet", None means "we could not ask". The
    caller propagates None as a rate-cap-adjacent unknown.
    """
    from magickit.mcp.github_dispatch import _mcp_call, _resolve_pat  # noqa: PLC0415

    try:
        pat = _resolve_pat("GITHUB_MCP_PAT_IMPLEMENTER")
        result = await _mcp_call(
            "tools/call",
            {
                "name": "pull_request_read",
                "arguments": {
                    "method": "get_reviews",
                    "owner": owner,
                    "repo": repo,
                    "pullNumber": number,
                },
            },
            pat,
        )
    except Exception as exc:  # noqa: BLE001 — mirror _fetch_open_prs
        logger.warning(
            "pr_watch: get_reviews failed",
            pr=f"{owner}/{repo}#{number}",
            err=str(exc),
        )
        return None
    payload = _first_json_payload(result)
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    return None


async def collect_pr_snapshots(
    repos: list[tuple[str, str]],
    state: PrWatchState,
    *,
    fetch_open_prs: Any = None,
    fetch_reviews: Any = None,
    now: float | None = None,
) -> tuple[list[PrSnapshot], list[str]]:
    """Fetch open PRs across ``repos`` and return one snapshot per live PR.

    Args:
        repos: ``[(owner, repo), ...]`` in allowlist order.
        state: per-process cache + call log.
        fetch_open_prs / fetch_reviews: dependency injection for tests.
            Both default to the real github-mcp round trips.
        now: monotonic-clock override (tests only).

    Returns:
        ``(snapshots, notices)``. Notices name repos whose ``list_pull_
        requests`` came back empty *and* raised (distinguishable in the
        log; here they degrade to zero cards for that repo with a user-
        visible warning).

    The steady-state cost:
    - Always: one ``list_pull_requests`` per repo (per 5-minute cycle in
      production; the caller controls cycle frequency).
    - Only when needed: ``get_reviews`` per PR whose ``updated_at`` moved
      or whose 15-minute self-heal window expired (see module docstring).
    """
    fetch_open_prs = fetch_open_prs or _fetch_open_prs
    fetch_reviews = fetch_reviews or _fetch_reviews

    snapshots: list[PrSnapshot] = []
    notices: list[str] = []

    for owner, repo in repos:
        # Every list call costs one API call regardless of the answer.
        _record_call(state, now=now)
        prs = await fetch_open_prs(owner, repo)
        if not prs:
            # Silence is ambiguous (empty repo vs. auth vs. outage). We
            # cannot distinguish here without more calls, so we say
            # nothing and let missing cards be the signal. Real failures
            # already surface in the log via _fetch_open_prs.
            continue

        for pr in prs:
            try:
                snapshot = await _snapshot_for_pr(
                    pr,
                    owner=owner,
                    repo=repo,
                    state=state,
                    fetch_reviews=fetch_reviews,
                    now=now,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "pr_watch: snapshot failed",
                    repo=f"{owner}/{repo}",
                    pr=pr.get("number"),
                    err=str(exc),
                )
                continue
            if snapshot is not None:
                snapshots.append(snapshot)

    return snapshots, notices


async def _snapshot_for_pr(
    pr: dict[str, Any],
    *,
    owner: str,
    repo: str,
    state: PrWatchState,
    fetch_reviews: Any,
    now: float | None = None,
) -> PrSnapshot | None:
    """Build one :class:`PrSnapshot`, consulting cache and refetch rules.

    ``None`` means the payload was too malformed to render (missing number
    / head SHA / updated_at). The board never sees these; better to skip
    than to draw a card whose identity is unknown.
    """
    number_raw = pr.get("number")
    if not isinstance(number_raw, int):
        return None
    head_obj = pr.get("head") or {}
    head_sha = head_obj.get("sha") if isinstance(head_obj, dict) else None
    updated_at = pr.get("updated_at")
    if not isinstance(head_sha, str) or not head_sha:
        return None
    if not isinstance(updated_at, str) or not updated_at:
        return None

    title = str(pr.get("title") or f"#{number_raw}")
    html_url = str(
        pr.get("html_url")
        or f"https://github.com/{owner}/{repo}/pull/{number_raw}"
    )

    cache_key = (owner, repo, number_raw, updated_at)
    entry = state.cache.get(cache_key)
    should_refetch = _needs_review_refetch(entry, now=now)

    if not should_refetch and entry is not None:
        return PrSnapshot(
            owner=owner,
            repo=repo,
            number=number_raw,
            title=title,
            html_url=html_url,
            head_sha=head_sha,
            updated_at=updated_at,
            artifact_approved=entry.artifact_approved,
            approving_review_id=entry.approving_review_id,
        )

    if _rate_capped(state, now=now):
        # Serve the stale-but-honest entry if we have one; else mark
        # rate_capped so the board can render `gate 進行中` rather than
        # falsely claim `未依頼`.
        if entry is not None:
            return PrSnapshot(
                owner=owner,
                repo=repo,
                number=number_raw,
                title=title,
                html_url=html_url,
                head_sha=head_sha,
                updated_at=updated_at,
                artifact_approved=entry.artifact_approved,
                approving_review_id=entry.approving_review_id,
                rate_capped=True,
            )
        return PrSnapshot(
            owner=owner,
            repo=repo,
            number=number_raw,
            title=title,
            html_url=html_url,
            head_sha=head_sha,
            updated_at=updated_at,
            artifact_approved=False,
            rate_capped=True,
        )

    _record_call(state, now=now)
    reviews = await fetch_reviews(owner, repo, number_raw)
    if reviews is None:
        # Couldn't read reviews. Reuse a stale entry rather than lying;
        # if there is none, mark rate_capped so the board renders it as
        # in-progress and not as un-requested.
        if entry is not None:
            return PrSnapshot(
                owner=owner,
                repo=repo,
                number=number_raw,
                title=title,
                html_url=html_url,
                head_sha=head_sha,
                updated_at=updated_at,
                artifact_approved=entry.artifact_approved,
                approving_review_id=entry.approving_review_id,
                rate_capped=True,
            )
        return PrSnapshot(
            owner=owner,
            repo=repo,
            number=number_raw,
            title=title,
            html_url=html_url,
            head_sha=head_sha,
            updated_at=updated_at,
            artifact_approved=False,
            rate_capped=True,
        )

    approved, review_id = _approved_at_head(reviews, head_sha)
    state.cache[cache_key] = _CacheEntry(
        reviews=reviews,
        artifact_approved=approved,
        approving_review_id=review_id,
        fetched_at=now if now is not None else time.monotonic(),
    )

    return PrSnapshot(
        owner=owner,
        repo=repo,
        number=number_raw,
        title=title,
        html_url=html_url,
        head_sha=head_sha,
        updated_at=updated_at,
        artifact_approved=approved,
        approving_review_id=review_id,
    )


#: Module-level process-lifetime state. The board's :func:`collect` reuses
#: the same instance across polls so the cache persists between requests;
#: tests never touch this — they construct their own :class:`PrWatchState`.
_STATE = PrWatchState()


def get_state() -> PrWatchState:
    """Return the process-singleton :class:`PrWatchState`.

    The board reads through here so tests can monkeypatch this function
    to hand back an isolated instance without disturbing the real cache.
    """
    return _STATE


__all__ = [
    "PrSnapshot",
    "PrWatchState",
    "NON_APPROVED_REFETCH_SECONDS",
    "SOFT_CAP_CALLS_PER_HOUR",
    "collect_pr_snapshots",
    "get_state",
]
