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
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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

# --- ledger-attached state (bounded closed-ledger discovery) ---------------
#
# The four-state UI was collapsed to three (msg-843 Option xxi) once the
# audit found that ``Tier-C 決着`` cannot be distinguished from ``gate
# 進行中`` structurally: both are cases where a ledger thread exists but no
# APPROVE artifact does. The board now shows one ``ledger_attached`` state
# in place of the two, primary-linking straight to the chatroom thread so
# the human answers "what did I decide?" in one click.
#
# Making that state trustworthy costs one extra chatroom read per unseen
# repo, and — for closed (resolved/superseded) ledger threads whose PR
# stays open — a bounded pagination through the closed thread list. This
# module owns that pagination's algebra (``compute_was_truncated_per_pr``
# / ``resolve_old_prs_by_deep_pagination``) so the board can consume the
# 3-way outcome (``found`` / ``definitive_absence`` / ``bounded_
# ambiguity``) without re-implementing it.

#: Key by which the ledger caches identify a PR.
LedgerKey = tuple[str, str, int]

#: TTL for a "we looked and didn't find a ledger" cache entry. Matched to
#: the non-approved refetch cadence (msg-828) so both signals refresh in
#: the same cycle rather than at drifting phases.
_NEGATIVE_LEDGER_TTL = timedelta(minutes=15)

#: Clock-skew buffer between GitHub's ``created_at`` and Conclair's
#: ``last_activity_at`` (msg-859 Option xxvii). ADD this to the page
#: horizon to make the definitive-absence condition *harder* to satisfy;
#: subtracting would generate false absence for genuinely-gated PRs whose
#: card would silently flip to 「未依頼」 — the exact silent failure this
#: feature exists to detect. Never subtract.
_CLOSED_LEDGER_HORIZON_SKEW = timedelta(minutes=5)

#: Hard cap on additional pages the deep pagination will fetch beyond
#: page 1. Env-overridable so operations can tune it against Conclair's
#: closed-ledger backlog. Five pages × 100 items = 500 closed threads
#: covered per PR before falling back to ``bounded_ambiguity``.
_MAX_CLOSED_LEDGER_PAGES = int(os.environ.get("MAGICKIT_CLOSED_LEDGER_PAGES", "5"))

#: Page size for Pass B and deep pagination. Matches ``list_threads``'s
#: default; kept as a constant so tests can override without threading
#: it through every call site.
_LIST_THREADS_PAGE_LIMIT = 100

# Ledger owner/tag constants live in :mod:`magickit.mcp.pr_gate_ledger`
# as the single source of truth (:data:`PR_GATE_THREAD_OWNER` / :data:
# `PR_GATE_THREAD_TAG`). Imported at module level here so any driver-
# side rename fails loudly at import rather than diverging silently
# (PR-gate ADVISORY at fa7a7d3 §2).
from magickit.mcp.pr_gate_ledger import (  # noqa: E402 - post-const import ok
    PR_GATE_THREAD_OWNER as _LEDGER_OWNER,
    PR_GATE_THREAD_TAG as _LEDGER_TAG,
)


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
    #: PR creation time (ISO-8601 UTC). Used by the closed-ledger deep
    #: pagination's chronological fence — a PR **newer** than a page's
    #: oldest thread must be on that page or an earlier one, so failing
    #: to find it on the fetched pages proves definitive ledger absence
    #: without reading further pages. (The condition in
    #: :func:`compute_was_truncated_per_pr` reads ``pr.created_at >
    #: threshold``, i.e. the PR is more recent than the horizon plus
    #: margin.) Optional / defaults to empty for backward compat with
    #: mocked snapshots that predate this feature; empty ``created_at``
    #: skips the chronological fence but is otherwise harmless.
    created_at: str = ""
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
    """One review-fetch cache line, keyed by PR identity (not identity+updated_at).

    ``updated_at`` used to live in the *key* — one dict entry per PR event
    — which leaked memory (every comment / review / push forever) and
    broke the rate-cap fallback: a stale entry becomes unreachable the
    moment ``updated_at`` bumps, so a live-approved PR could flicker
    non-approved on the board during a rate cap because the "stale but
    honest" entry could not be found under the new key. Storing
    ``updated_at`` inside the entry fixes both: the key stays stable at
    ``(owner, repo, number)`` and the field is compared per read to
    decide whether the entry is fresh.

    ``fetched_at`` is the wall-clock time of the ``/reviews`` fetch, used
    by the 15-minute mandatory refetch (see module docstring).
    """

    updated_at: str
    reviews: list[dict[str, Any]]
    artifact_approved: bool
    approving_review_id: int | None
    fetched_at: float


@dataclass(frozen=True)
class NegativeLedgerEntry:
    """Records "we looked for PR X's ledger and did not find it".

    A negative cache is the piece that keeps the closed-ledger discovery
    from costing a Pass B every poll cycle. It carries two invalidation
    triggers, both required:

    - ``pr_updated_at``: any PR-side event bumps ``updated_at``, and a
      new ledger thread being opened is exactly one such event (the driver
      writes into chatroom, which does not touch GitHub, but the
      review/comment/push that triggered the driver *did*). Bumps mean
      the cache is stale by construction.
    - ``cached_at``: a TTL fallback that catches cases where the ledger
      appears with no matching PR-side event (e.g. a driver retry).

    ``was_truncated`` splits the two failure modes the board must render
    differently:

    - ``False`` — *definitive absence*. Pass B (or deep pagination)
      proved no ledger exists in the searched window; the "未依頼"
      classification stands. No notice needed.
    - ``True`` — *bounded ambiguity*. We ran out of pages before finding
      the ledger; classification defaults to "未依頼" but a notice tells
      the human that a deep-history ledger could exist. The board keeps
      showing the notice while this entry lives.
    """

    pr_updated_at: str
    cached_at: datetime
    was_truncated: bool


@dataclass(frozen=True)
class DeepPaginationOutcome:
    """Per-PR result from :func:`resolve_old_prs_by_deep_pagination`.

    Exactly one field is populated — enforced by ``__post_init__`` — so
    consumers dispatch on a discriminated union without needing to
    inspect three separate booleans that could collectively lie.

    - ``found``: ``(project, thread_dict)`` for the matched ledger thread.
    - ``definitive_absence``: proved by chronological fence or exhausted
      pool; no notice.
    - ``bounded_ambiguity``: pages exhausted without proof; notice.
    """

    found: tuple[str, dict[str, Any]] | None = None
    definitive_absence: bool = False
    bounded_ambiguity: bool = False

    def __post_init__(self) -> None:
        flags = (
            self.found is not None,
            self.definitive_absence,
            self.bounded_ambiguity,
        )
        if sum(flags) != 1:
            raise ValueError(
                f"exactly one DeepPaginationOutcome field must be set: {self!r}"
            )


@dataclass
class PrWatchState:
    """Per-process state the watcher keeps between calls.

    Held as a plain dataclass (not a global singleton) so tests can pass
    a fresh one and production wires it once per process via the module
    ``_STATE`` sentinel used by :func:`collect_pr_snapshots`.
    """

    #: ``(owner, repo, number)`` → cache entry. ``updated_at`` is
    #: **inside** the entry, not in the key — see :class:`_CacheEntry`.
    cache: dict[LedgerKey, _CacheEntry] = field(default_factory=dict)
    #: Rolling window of API call timestamps (monotonic seconds).
    call_log: list[float] = field(default_factory=list)
    #: Positive ledger cache: ``LedgerKey → (project, thread_id,
    #: pr_updated_at)``. Skips the list_threads round trip on a
    #: subsequent poll when we already know which thread the PR's
    #: ledger lives on. Invalidated on (a) ``get_thread`` failure and
    #: (b) ``pr_updated_at`` mismatch — a new commit closes the old
    #: ledger and opens a new one, and a stale pointer would keep
    #: linking the operator to the superseded thread (PR-gate BLOCKING
    #: at fa7a7d3 §1).
    ledger_pointers: dict[LedgerKey, tuple[str, str, str]] = field(
        default_factory=dict
    )
    #: Negative ledger cache: ``LedgerKey → NegativeLedgerEntry``. Answers
    #: "already looked and did not find" so the board does not fire Pass
    #: B every 5-minute cycle for the same 未依頼 PR. TTL + updated_at
    #: gate the entry.
    negative_ledger_cache: dict[LedgerKey, NegativeLedgerEntry] = field(
        default_factory=dict
    )


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
    """Return (approved, approving_review_id) at ``head_sha``.

    Mirrors GitHub's own review-supersede semantic: ``/reviews`` returns
    events in chronological order (oldest first), and GitHub's branch
    protection judges each reviewer by their LATEST review on the
    commit. A reviewer's later ``CHANGES_REQUESTED`` or ``DISMISSED``
    on the same ``head_sha`` supersedes their earlier ``APPROVED``.

    Aggregate rule:
    - Any active ``CHANGES_REQUESTED`` (from any reviewer) → False.
    - Otherwise, any active ``APPROVED`` → True with that review's id.
    - Else → False.

    ``commit_id`` must equal ``head_sha`` — a review on an earlier
    commit judged a different diff (msg-978 §4-2).

    Reviews without a resolvable ``user.login`` are treated as coming
    from a single synthetic reviewer keyed by ``id`` (test payloads
    often omit ``user``; the real GitHub API always populates it).

    Divergence note: this used to be a strict copy of the simpler
    predicate in ``pr_gate_ledger.evaluate_ledger_verdict`` ("any
    APPROVED at head"), and its docstring claimed the two are always
    in sync. PR-gate objection at 5fa6480 §1 showed the simple form
    silently keeps a stale APPROVED after the same reviewer switches
    to CHANGES_REQUESTED on the same commit, so we upgrade the
    open-PR side here. The ledger predicate has the identical shape
    and needs the same treatment; that fix is out of scope for PR #87
    Part A (``pr_gate_ledger.py`` is not in its declared paths) and
    is tracked as a follow-up (this PR's body §Follow-ups).
    """
    # per_reviewer[key] = (state, review_id) — latest verdict per
    # reviewer at head_sha. COMMENTED/PENDING are non-verdicts and
    # skipped so they cannot displace a real verdict.
    per_reviewer: dict[Any, tuple[str, int | None]] = {}
    for r in reviews:
        if not isinstance(r, dict):
            continue
        if r.get("commit_id") != head_sha:
            continue
        state = r.get("state")
        if state not in {_APPROVED, "CHANGES_REQUESTED", "DISMISSED"}:
            continue
        review_id_raw = r.get("id")
        review_id = review_id_raw if isinstance(review_id_raw, int) else None
        user = r.get("user")
        login = user.get("login") if isinstance(user, dict) else None
        # Synthetic key when the payload lacks a resolvable login: each
        # such review counts as its own reviewer (fail-safe — this only
        # makes the supersede semantic weaker, never falsely stricter).
        key: Any = login if isinstance(login, str) else ("__no_login__", review_id_raw)
        # Chronological order → later entry wins.
        per_reviewer[key] = (state, review_id)

    # Any active CHANGES_REQUESTED blocks approval.
    for state, _ in per_reviewer.values():
        if state == "CHANGES_REQUESTED":
            return False, None

    # Otherwise, the first active APPROVED (iteration order matches the
    # chronological /reviews stream via dict insertion order) wins.
    for state, review_id in per_reviewer.values():
        if state == _APPROVED:
            return True, review_id

    return False, None


def _needs_review_refetch(
    entry: _CacheEntry | None,
    *,
    live_updated_at: str,
    now: float | None = None,
) -> bool:
    """Should we refetch reviews for this PR?

    Three conditions to fetch:

    - **No cache entry.** Trivially: fetch.
    - **``updated_at`` bump.** Any GitHub-side event bumps ``updated_at``;
      if the entry's stored value no longer matches the live one, the
      cached review list is definitionally stale — an APPROVED entry
      whose PR just got a new commit must not stay sticky.
    - **Non-approved & 15 minutes stale.** The mandatory self-healing
      window (see module docstring). Approved entries whose ``updated_
      at`` still matches are sticky.
    """
    if entry is None:
        return True
    if entry.updated_at != live_updated_at:
        return True
    if entry.artifact_approved:
        return False
    now = now if now is not None else time.monotonic()
    return (now - entry.fetched_at) > NON_APPROVED_REFETCH_SECONDS


#: Sentinel returned by :func:`_fetch_open_prs` on API failure. Distinct
#: from ``[]`` (which means "we asked and this repo has no open PRs") so
#: the caller can emit a truthful notice rather than silently drop the
#: repo from the lane. Compared by identity (``is``), not equality.
_FETCH_FAILED: object = object()


async def _fetch_open_prs(
    owner: str, repo: str
) -> list[dict[str, Any]] | object:
    """``list_pull_requests(state=open)`` for one repo, or :data:`_FETCH_FAILED`.

    Prior behaviour returned ``[]`` on failure, which silently blanked
    the lane for that repo without a notice — a GitHub outage looked
    identical to "no open PRs", and the operator learned about it only
    when the eventual merge went by unobserved. The sentinel lets
    :func:`collect_pr_snapshots` emit a notice on true failure while
    keeping the "empty repo" case silent.

    Two payload shapes count as success:

    - a bare JSON list of PR dicts (``[{...}, {...}]``), or
    - a wrapped object with ``{"items": [...]}``.

    Any other shape — a GitHub error envelope such as
    ``{"message": "API rate limit exceeded"}``, ``None`` from a
    non-decodable payload, or a scalar — is treated as
    :data:`_FETCH_FAILED`, not as an empty list. The empty-list case is
    reserved for a genuine 200-OK response carrying zero open PRs.
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
    except Exception as exc:  # noqa: BLE001 — sentinel signals failure
        logger.warning(
            "pr_watch: list_pull_requests failed",
            repo=f"{owner}/{repo}",
            err=str(exc),
        )
        return _FETCH_FAILED
    payload = _first_json_payload(result)
    if isinstance(payload, list):
        return [p for p in payload if isinstance(p, dict)]
    # Some backends wrap under {"items": [...]}. Accept either shape.
    if isinstance(payload, dict):
        items = payload.get("items")
        if isinstance(items, list):
            return [p for p in items if isinstance(p, dict)]
    # Any other shape is unrecognisable as a PR list: a GitHub error
    # envelope (``{"message": "API rate limit exceeded"}`` /
    # ``{"message": "Bad credentials"}``), a scalar, or ``None`` from a
    # non-decodable payload. Returning ``[]`` here would tell the caller
    # "0 open PRs", which silently drops the repo's PRs from the board,
    # evicts every cached ``/reviews`` / ledger pointer for them, and
    # forces a rate-limit burst the moment the API recovers — the exact
    # failure the ``failed_repos`` preserve was designed to prevent
    # (PR-gate BLOCKING at 934f2cd §1). Route this through the same
    # sentinel path the exception branch uses so ``collect_pr_snapshots``
    # emits a degradation notice and the pruners spare the cache.
    logger.warning(
        "pr_watch: list_pull_requests unrecognized payload shape",
        repo=f"{owner}/{repo}",
        payload_type=type(payload).__name__,
    )
    return _FETCH_FAILED


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
) -> tuple[list[PrSnapshot], list[str], set[tuple[str, str]]]:
    """Fetch open PRs across ``repos`` and return one snapshot per live PR.

    Args:
        repos: ``[(owner, repo), ...]`` in allowlist order.
        state: per-process cache + call log.
        fetch_open_prs / fetch_reviews: dependency injection for tests.
            Both default to the real github-mcp round trips.
        now: monotonic-clock override (tests only).

    Returns:
        ``(snapshots, notices, failed_repos)``. ``failed_repos`` is the
        set of ``(owner, repo)`` whose ``list_pull_requests`` returned
        :data:`_FETCH_FAILED`; pass it as the ``failed_repos=`` kwarg to
        :func:`prune_review_cache`, :func:`prune_ledger_pointers`, and
        :func:`prune_negative_ledger` so those repos' cache entries are
        preserved across the failing cycle (PR-gate objection at
        7d5aa57 §1 — pruning on transient failure would force a
        rate-burst re-fetch of every PR the moment the API recovers).
        The prune functions own the exclusion; the caller does not need
        to know the shape of the internal cache dicts to uphold the
        invariant.

    The steady-state cost:
    - Always: one ``list_pull_requests`` per repo (per 5-minute cycle in
      production; the caller controls cycle frequency).
    - Only when needed: ``get_reviews`` per PR whose ``updated_at`` moved
      or whose 15-minute self-heal window expired (see module docstring).

    Memory hygiene: ``state.call_log`` is pruned to the last rolling
    hour at the end of every cycle, so long dormant periods (no open PRs
    across any polled repo) cannot cause unbounded growth.
    """
    fetch_open_prs = fetch_open_prs or _fetch_open_prs
    fetch_reviews = fetch_reviews or _fetch_reviews

    snapshots: list[PrSnapshot] = []
    notices: list[str] = []
    failed_repos: set[tuple[str, str]] = set()

    for owner, repo in repos:
        # Every list call costs one API call regardless of the answer.
        _record_call(state, now=now)
        prs = await fetch_open_prs(owner, repo)
        if prs is _FETCH_FAILED:
            # Genuine outage / auth failure. Emit a notice so the board
            # says "the merge lane is degraded" instead of silently
            # dropping the repo. The 200-OK-empty case does not come
            # here — it is a truthy empty list, handled below.
            notices.append(
                f"{owner}/{repo}: GitHub API 応答なし — マージ lane は縮退表示中"
            )
            failed_repos.add((owner, repo))
            continue
        if not prs:
            # Silence is intentional here: an empty list means "repo has
            # no open PRs right now", which is exactly the state where
            # a card would be misleading. Auth failures went to
            # _FETCH_FAILED above.
            continue

        # Narrow type from list | object → list for the loop below.
        assert isinstance(prs, list)
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

    # Decouple call-log hygiene from PR activity (PR-gate objection at
    # d8bc61a §advisory — the pruner was only invoked inside `_rate_capped`,
    # which is reached via `_snapshot_for_pr`; if every polled repo has zero
    # open PRs for a prolonged quiet period, that path is skipped and
    # `call_log` accumulates one float per repo per cycle indefinitely).
    # Pruning once per cycle bounds the log to the last rolling hour
    # regardless of PR activity, and self-heals the moment PRs return.
    _prune_call_log(state, now=now)

    return snapshots, notices, failed_repos


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

    # created_at is optional: the deep-pagination fence tolerates its
    # absence by falling back to "ambiguous" rather than making up a
    # value. Malformed / missing → empty string, not a stack trace.
    created_at_raw = pr.get("created_at")
    created_at = created_at_raw if isinstance(created_at_raw, str) else ""

    title = str(pr.get("title") or f"#{number_raw}")
    html_url = str(
        pr.get("html_url")
        or f"https://github.com/{owner}/{repo}/pull/{number_raw}"
    )

    cache_key: LedgerKey = (owner, repo, number_raw)
    entry = state.cache.get(cache_key)
    should_refetch = _needs_review_refetch(
        entry, live_updated_at=updated_at, now=now,
    )

    if not should_refetch and entry is not None:
        return PrSnapshot(
            owner=owner,
            repo=repo,
            number=number_raw,
            title=title,
            html_url=html_url,
            head_sha=head_sha,
            updated_at=updated_at,
            created_at=created_at,
            artifact_approved=entry.artifact_approved,
            approving_review_id=entry.approving_review_id,
        )

    if _rate_capped(state, now=now):
        # Serve the stale-but-honest entry if we have one; else mark
        # rate_capped so the board can render `gate 進行中` rather than
        # falsely claim `未依頼`. Critically: re-verify the approval
        # against the CURRENT head. If a new commit landed since the
        # cache write, the old ``commit_id`` no longer matches
        # ``head_sha`` and ``_approved_at_head`` will return False —
        # so the stale approval is never paired with the new head
        # (PR-gate objection at 26d0634 §2).
        if entry is not None:
            approved_now, review_id_now = _approved_at_head(
                entry.reviews, head_sha,
            )
            return PrSnapshot(
                owner=owner,
                repo=repo,
                number=number_raw,
                title=title,
                html_url=html_url,
                head_sha=head_sha,
                updated_at=updated_at,
                created_at=created_at,
                artifact_approved=approved_now,
                approving_review_id=review_id_now,
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
            created_at=created_at,
            artifact_approved=False,
            rate_capped=True,
        )

    _record_call(state, now=now)
    reviews = await fetch_reviews(owner, repo, number_raw)
    if reviews is None:
        # Couldn't read reviews. Reuse a stale entry rather than lying;
        # same rule as the rate-cap path: re-verify against the current
        # head so an approve on an earlier commit is not silently
        # paired with a new head.
        if entry is not None:
            approved_now, review_id_now = _approved_at_head(
                entry.reviews, head_sha,
            )
            return PrSnapshot(
                owner=owner,
                repo=repo,
                number=number_raw,
                title=title,
                html_url=html_url,
                head_sha=head_sha,
                updated_at=updated_at,
                created_at=created_at,
                artifact_approved=approved_now,
                approving_review_id=review_id_now,
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
            created_at=created_at,
            artifact_approved=False,
            rate_capped=True,
        )

    approved, review_id = _approved_at_head(reviews, head_sha)
    state.cache[cache_key] = _CacheEntry(
        updated_at=updated_at,
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
        created_at=created_at,
        artifact_approved=approved,
        approving_review_id=review_id,
    )


# --- ledger cache & bounded pagination helpers -----------------------------


def pr_key(snapshot: PrSnapshot) -> LedgerKey:
    """Reduce a snapshot to the tuple both caches key by."""
    return (snapshot.owner, snapshot.repo, int(snapshot.number))


def get_ledger_pointer(
    state: PrWatchState,
    key: LedgerKey,
    *,
    pr_updated_at: str | None = None,
) -> tuple[str, str] | None:
    """Read the positive ledger cache. ``None`` = miss or stale.

    When ``pr_updated_at`` is passed and does not match the value the
    pointer was written under, the entry is treated as a miss (the PR
    has had a GitHub-side event since; a new commit may have created
    a fresh ledger thread that Pass A/B must now discover). Callers
    that only want raw lookup can omit ``pr_updated_at``.
    """
    entry = state.ledger_pointers.get(key)
    if entry is None:
        return None
    project, thread_id, cached_updated_at = entry
    if pr_updated_at is not None and cached_updated_at != pr_updated_at:
        return None
    return (project, thread_id)


def set_ledger_pointer(
    state: PrWatchState,
    key: LedgerKey,
    project: str,
    thread_id: str,
    pr_updated_at: str,
) -> None:
    """Record a ``(project, thread_id, pr_updated_at)`` pointer."""
    state.ledger_pointers[key] = (project, thread_id, pr_updated_at)


def get_negative_ledger(
    state: PrWatchState,
    key: LedgerKey,
    now: datetime,
    pr_updated_at: str,
) -> NegativeLedgerEntry | None:
    """Return a valid entry, or ``None`` if the cache should be skipped.

    Validity is the conjunction of two invariants: the PR's
    ``updated_at`` must still match (any GitHub-side event bumped it, and
    such an event might be what caused a ledger to appear), AND the TTL
    must not have elapsed. Either failing means the caller must run Pass
    B and re-cache — the cached entry stays until it is overwritten, but
    is not honoured on this call.
    """
    entry = state.negative_ledger_cache.get(key)
    if entry is None:
        return None
    if entry.pr_updated_at != pr_updated_at:
        return None
    if entry.cached_at + _NEGATIVE_LEDGER_TTL <= now:
        return None
    return entry


def set_negative_ledger(
    state: PrWatchState,
    key: LedgerKey,
    pr_updated_at: str,
    now: datetime,
    *,
    was_truncated: bool,
) -> None:
    """Record a "we looked and did not find" cache entry."""
    state.negative_ledger_cache[key] = NegativeLedgerEntry(
        pr_updated_at=pr_updated_at,
        cached_at=now,
        was_truncated=was_truncated,
    )


def clear_negative_ledger(state: PrWatchState, key: LedgerKey) -> None:
    """Drop the negative-cache entry for ``key`` if it exists.

    Called when a subsequent poll finds a ledger (via Pass A / positive
    cache / Pass B / deep pagination) — the older "we did not find"
    entry is now known to be wrong, and leaving it would produce two
    contradictory board states at once: a ledger link on the card AND
    a truncation notice claiming the classification is unconfirmed.
    Idempotent: safe to call even if no entry exists.
    """
    state.negative_ledger_cache.pop(key, None)


def _is_from_failed_repo(
    key: LedgerKey, failed_repos: set[tuple[str, str]] | None,
) -> bool:
    """True if ``key`` belongs to a repo whose fetch failed this cycle.

    Encapsulates the ``failed_repos`` skip that
    :func:`prune_review_cache`, :func:`prune_ledger_pointers`, and
    :func:`prune_negative_ledger` all apply identically. The three
    prune functions own this exclusion because :func:`collect_pr_snapshots`
    yields no snapshots for failed repos, so their PRs are missing from
    the caller's ``live_keys`` set — pruning against ``live_keys`` alone
    would evict every cache entry for the failing repo and force a
    rate-burst re-fetch the moment the API recovered (PR-gate objection
    at e7d20da §1).
    """
    if failed_repos is None:
        return False
    return (key[0], key[1]) in failed_repos


def prune_review_cache(
    state: PrWatchState,
    *,
    live_keys: set[LedgerKey],
    failed_repos: set[tuple[str, str]] | None = None,
) -> None:
    """Drop review-cache entries for PRs no longer in the open set.

    Without this, every PR ever polled retains its full ``/reviews``
    JSON payload for the lifetime of the process — a per-PR memory
    leak (PR-gate objection at 2c92dd9 §3). Merged / closed PRs will
    never be polled again, so their cached reviews cannot help the
    rate-cap fallback and only cost memory.

    Kept separate from :func:`prune_negative_ledger` because this
    cache has no TTL: staleness is measured only against the live set.

    ``failed_repos`` (optional): the ``(owner, repo)`` set that
    :func:`collect_pr_snapshots` returns for cycles where a repo's
    ``list_pull_requests`` failed. Entries belonging to those repos are
    kept regardless of ``live_keys`` — the caller does not need to know
    the shape of ``state.cache`` to preserve them.
    """
    stale = [
        k
        for k in state.cache
        if k not in live_keys and not _is_from_failed_repo(k, failed_repos)
    ]
    for key in stale:
        state.cache.pop(key, None)


def prune_ledger_pointers(
    state: PrWatchState,
    *,
    live_keys: set[LedgerKey],
    failed_repos: set[tuple[str, str]] | None = None,
) -> None:
    """Drop positive-cache pointers for PRs no longer in the open set.

    Symmetric with :func:`prune_review_cache`: the ``(project,
    thread_id)`` pointer is useful only while the PR it names is still
    being polled. Retaining pointers for closed PRs would grow the
    ``ledger_pointers`` dict indefinitely with no upside.

    ``failed_repos`` (optional): same semantics as
    :func:`prune_review_cache` — entries belonging to those repos are
    preserved across the failing cycle so the API recovery does not
    trigger a rate-burst re-scan of every PR's ledger.
    """
    stale = [
        k
        for k in state.ledger_pointers
        if k not in live_keys and not _is_from_failed_repo(k, failed_repos)
    ]
    for key in stale:
        state.ledger_pointers.pop(key, None)


def prune_negative_ledger(
    state: PrWatchState,
    *,
    now: datetime,
    live_keys: set[LedgerKey],
    failed_repos: set[tuple[str, str]] | None = None,
) -> None:
    """Drop stale / superseded entries from the negative cache.

    Two dropping conditions:

    - **Not live** (``key not in live_keys``): the PR has fallen out of
      the ``list_pull_requests`` cycle — merged, closed, or moved out
      of the allowlist. Its ledger status no longer needs a notice, and
      leaving the entry would produce a permanent zombie notice about
      a PR nobody is tracking. Filtering at emit time would work too,
      but pruning also keeps the dict from growing without bound over
      a long-running process.
    - **TTL expired**: the entry no longer represents a bounded lookup;
      Pass B would need to re-fire regardless. Dropping it here means
      :func:`iter_truncated_prs` can rely on "still-live entries" as
      the whole set to emit.

    ``failed_repos`` (optional): entries whose repo is in this set are
    preserved even when ``key not in live_keys`` (they only fall out of
    the live set because ``list_pull_requests`` failed transiently). The
    TTL check still applies — a genuinely stale entry drops on its own
    schedule.
    """
    stale_keys = [
        key
        for key, entry in state.negative_ledger_cache.items()
        if (
            key not in live_keys
            and not _is_from_failed_repo(key, failed_repos)
        )
        or entry.cached_at + _NEGATIVE_LEDGER_TTL <= now
    ]
    for key in stale_keys:
        state.negative_ledger_cache.pop(key, None)


def iter_truncated_prs(
    state: PrWatchState,
    *,
    now: datetime,
    live_keys: set[LedgerKey] | None = None,
) -> dict[str, list[int]]:
    """PRs whose 未依頼 verdict is unconfirmed, keyed by ``owner/repo``.

    Feeds the board's notice line. Only entries with ``was_truncated=
    True`` count — definitive absences are silent. Two filters guard
    against zombie notices (PR-gate objection at 916df27):

    - **TTL**: an entry past its TTL no longer describes a bounded
      lookup; the notice must retire with it. TTL is the same 15-min
      window the negative cache honours on read.
    - **Live keys** (optional): when caller supplies the set of PRs
      still in the current ``list_pull_requests`` cycle, we filter to
      those. This eliminates notices for merged / closed PRs whose
      cache entries have not yet been pruned by
      :func:`prune_negative_ledger`.

    Callers that maintain their own pruning invariant can pass
    ``live_keys=None`` to skip the extra filter; the TTL check alone
    still bounds the notice's lifetime.
    """
    out: dict[str, list[int]] = {}
    for (owner, repo, number), entry in state.negative_ledger_cache.items():
        if not entry.was_truncated:
            continue
        if entry.cached_at + _NEGATIVE_LEDGER_TTL <= now:
            continue
        if live_keys is not None and (owner, repo, number) not in live_keys:
            continue
        slug = f"{owner}/{repo}"
        out.setdefault(slug, []).append(number)
    for slug in out:
        out[slug].sort()
    return out


def _parse_iso_utc(iso_ts: str) -> datetime:
    """Parse an ISO-8601 UTC string, tolerating the trailing ``Z``."""
    if iso_ts.endswith("Z"):
        iso_ts = iso_ts[:-1] + "+00:00"
    return datetime.fromisoformat(iso_ts)


def _format_iso_utc(dt: datetime) -> str:
    """Format a UTC datetime as the ISO-8601 ``…Z`` form GitHub uses."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def add_minutes(iso_ts: str, delta: timedelta) -> str:
    """Return ``iso_ts + delta`` as ISO-8601 UTC (Z-suffixed).

    Used only in the definitive-absence threshold below; kept as a named
    helper so the direction of the margin (see :func:`definitive_absence_
    threshold`) is a call, not an inlined ``+``.
    """
    return _format_iso_utc(_parse_iso_utc(iso_ts) + delta)


def definitive_absence_threshold(
    page_horizon: str,
    clock_skew_margin: timedelta = _CLOSED_LEDGER_HORIZON_SKEW,
) -> str:
    """Return the threshold ``T`` above which ``PR.created_at`` proves absence.

    Safety direction (msg-859 Option xxvii): **add** the margin to the
    horizon so ``T`` is *later* and the condition ``pr.created_at > T``
    is *harder* to satisfy. The alternative — subtracting the margin —
    would make premature absence declarations, silently reclassifying
    gated PRs as 「未依頼」, which is the exact failure mode this whole
    lane exists to detect. **Never change to subtract.**

    Delaying a definitive-absence declaration by one page is a bounded
    inefficiency (Pass B fires again next poll); declaring it prematurely
    is a fatal correctness breach that we would only notice by watching
    a card silently ship without a gate.
    """
    return add_minutes(page_horizon, clock_skew_margin)


def min_last_activity_at(items: list[dict[str, Any]]) -> str | None:
    """Smallest ``last_activity_at`` in ``items``, or ``None`` if empty.

    ISO-8601 UTC strings sort lexicographically the same way they sort
    chronologically, so ``min`` on the raw string is safe. Missing
    ``last_activity_at`` entries are ignored (rather than treated as
    "earliest", which would silently accept malformed pages).
    """
    values = [
        it["last_activity_at"]
        for it in items
        if isinstance(it, dict)
        and isinstance(it.get("last_activity_at"), str)
        and it["last_activity_at"]
    ]
    return min(values) if values else None


def find_ledger_in_page(
    pr: PrSnapshot, items: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Locate ``pr``'s ledger among ``items`` by parsing the title.

    Match rule mirrors :func:`magickit.web.board._ledger_thread_for_pr`
    — same owner filter, same tag filter, same
    :func:`magickit.mcp.pr_gate_ledger.parse_pr_ref` on the title. Kept
    here so :func:`resolve_old_prs_by_deep_pagination` does not import
    the board (an inverted dependency).
    """
    from magickit.mcp.pr_gate_ledger import parse_pr_ref  # noqa: PLC0415

    for thread in items:
        if not isinstance(thread, dict):
            continue
        tags = thread.get("tags") or []
        if (
            thread.get("owner") != _LEDGER_OWNER
            or not isinstance(tags, list)
            or _LEDGER_TAG not in tags
        ):
            continue
        ref = parse_pr_ref(str(thread.get("title") or ""))
        if ref is None:
            continue
        if (
            ref.owner == pr.owner
            and ref.repo == pr.repo
            and ref.number == int(pr.number)
        ):
            return thread
    return None


def compute_was_truncated_per_pr(
    unmatched_prs: list[PrSnapshot],
    list_result: dict[str, Any],
    *,
    clock_skew_margin: timedelta = _CLOSED_LEDGER_HORIZON_SKEW,
) -> dict[LedgerKey, bool]:
    """Decide ``was_truncated`` for each Pass B page 1 unmatched PR.

    Returns ``{pr_key: was_truncated}`` where ``False`` means "definitive
    absence proved" (silent 未依頼) and ``True`` means "ambiguity remains
    — a deep-history ledger could exist" (notice needed OR deep
    pagination should try to resolve).

    The two positive-verdict paths:

    - ``total <= len(items)``: we saw every closed ledger there is. No
      ambiguity possible.
    - ``pr.created_at > threshold(horizon)``: the PR is younger than the
      oldest thread on this page (plus the skew margin), so if a ledger
      for it existed, it would have to be on this page. It is not, so
      it does not exist.
    """
    items_raw = list_result.get("items", []) or []
    items: list[dict[str, Any]] = [i for i in items_raw if isinstance(i, dict)]
    total_raw = list_result.get("total")
    # If `total` is missing or not an int (e.g. error envelope, string
    # value), we cannot prove exhaustion — treat every unmatched PR as
    # ambiguous rather than silently declaring definitive absence
    # (PR-gate objection at 7d5aa57 §2).
    if not isinstance(total_raw, int):
        return {pr_key(pr): True for pr in unmatched_prs}
    total = total_raw

    if total <= len(items):
        return {pr_key(pr): False for pr in unmatched_prs}

    horizon = min_last_activity_at(items)
    if horizon is None:
        # Truncated but no horizon to reason against. Falls through to
        # deep pagination / notice — treat as ambiguous.
        return {pr_key(pr): True for pr in unmatched_prs}

    threshold = definitive_absence_threshold(horizon, clock_skew_margin)
    result: dict[LedgerKey, bool] = {}
    for pr in unmatched_prs:
        result[pr_key(pr)] = not (pr.created_at > threshold)
    return result


async def resolve_old_prs_by_deep_pagination(
    unmatched_old_prs: list[PrSnapshot],
    project: str,
    list_threads_fn: Callable[..., Awaitable[dict[str, Any]]],
    *,
    max_pages: int | None = None,
    limit: int = _LIST_THREADS_PAGE_LIMIT,
    clock_skew_margin: timedelta = _CLOSED_LEDGER_HORIZON_SKEW,
) -> dict[LedgerKey, DeepPaginationOutcome]:
    """Walk closed-ledger pages 2..max_pages+1 for horizon-failing PRs.

    Pass B has already fetched page 1 (``offset=0``); this function
    starts at ``offset=limit`` and continues while there are PRs still
    unresolved. Two ways it can exit early:

    - **Pool exhausted** (``offset + len(items) >= total``): every closed
      thread the project has has been read. Remaining PRs' verdict is
      ``definitive_absence``.
    - **Chronological fence at this page's horizon**: if the page's
      oldest thread is older than a PR's ``created_at`` (plus the skew
      margin), the PR would already be on an earlier page. Absence
      proven for that PR without reading further.

    If neither exit fires by ``max_pages``, remaining PRs get
    ``bounded_ambiguity``.
    """
    # Resolve default at call time so an env-var override (or a test
    # monkeypatching :data:`_MAX_CLOSED_LEDGER_PAGES`) takes effect
    # without reloading the module — Python binds default arg values at
    # def time, so a module-level default here would be frozen.
    if max_pages is None:
        max_pages = _MAX_CLOSED_LEDGER_PAGES

    resolved: dict[LedgerKey, DeepPaginationOutcome] = {}
    remaining = list(unmatched_old_prs)

    for page_idx in range(1, max_pages + 1):
        if not remaining:
            break
        offset = page_idx * limit
        try:
            result = await list_threads_fn(
                project=project,
                owner=_LEDGER_OWNER,
                status_filter=["resolved", "superseded"],
                limit=limit,
                offset=offset,
            )
        except Exception as exc:  # noqa: BLE001 — degrade to ambiguity
            logger.warning(
                "pr_watch: deep-pagination list_threads failed",
                project=project, offset=offset, err=str(exc),
            )
            break
        items_raw = result.get("items", []) or []
        items: list[dict[str, Any]] = [
            i for i in items_raw if isinstance(i, dict)
        ]
        total_raw = result.get("total")
        # None = "total unknown", suppresses the pool-exhausted check.
        # Defaulting a missing int to 0 (previous behaviour) would fire
        # `offset + len(items) >= 0` on every page and prematurely
        # declare definitive absence (PR-gate objection at 7d5aa57 §2).
        total = total_raw if isinstance(total_raw, int) else None

        next_remaining: list[PrSnapshot] = []
        page_horizon = min_last_activity_at(items)
        for pr in remaining:
            thread_dict = find_ledger_in_page(pr, items)
            if thread_dict is not None:
                resolved[pr_key(pr)] = DeepPaginationOutcome(
                    found=(project, thread_dict),
                )
                continue
            if page_horizon is not None:
                threshold = definitive_absence_threshold(
                    page_horizon, clock_skew_margin,
                )
                if pr.created_at > threshold:
                    resolved[pr_key(pr)] = DeepPaginationOutcome(
                        definitive_absence=True,
                    )
                    continue
            next_remaining.append(pr)

        remaining = next_remaining

        # Pool exhausted → remaining PRs' absence is definitive.
        # Skip when total is unknown (see comment above).
        if total is not None and offset + len(items) >= total:
            for pr in remaining:
                resolved[pr_key(pr)] = DeepPaginationOutcome(
                    definitive_absence=True,
                )
            remaining = []
            break

    for pr in remaining:
        resolved[pr_key(pr)] = DeepPaginationOutcome(bounded_ambiguity=True)

    return resolved


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
    "NegativeLedgerEntry",
    "DeepPaginationOutcome",
    "LedgerKey",
    "NON_APPROVED_REFETCH_SECONDS",
    "SOFT_CAP_CALLS_PER_HOUR",
    "collect_pr_snapshots",
    "get_state",
    "pr_key",
    "get_ledger_pointer",
    "set_ledger_pointer",
    "get_negative_ledger",
    "set_negative_ledger",
    "clear_negative_ledger",
    "prune_negative_ledger",
    "prune_review_cache",
    "prune_ledger_pointers",
    "iter_truncated_prs",
    "add_minutes",
    "definitive_absence_threshold",
    "min_last_activity_at",
    "find_ledger_in_page",
    "compute_was_truncated_per_pr",
    "resolve_old_prs_by_deep_pagination",
]
