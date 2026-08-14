"""PR-gate ledger close carve-out — "may the loop file this thread away?"

`T-pr-gate-ledger-debt` (msg-978 §1 / msg-1001 §2, Tier-C P-A② + P-C).

## The problem this closes

PR-review threads are opened by the **driver** under ``owner="orchestrator"``
(``spirrow_mindwire.orchestrator.Orchestrator.fire_pr_review``: deterministic
``T-pr-review-<n>``, ``tags=["pr-review", "naysayer", "stage3"]``). The driver
has no close path, and ADR-2026-06-04-19 D-5 grants owner-override to *humans
only*, so none of the loop's three roles could close one — a
``closeable_roles``-clearing identity still fell at Conclair's
``assert_owner_can_close``. Filing a finished PR-review thread is bookkeeping,
not judgement, yet the only route to it ran through the loop's scarcest
resource. 22 threads silted up behind that; a full audit found **18 of them
required no human judgement at all**.

## The rule

Permission is decided by a **provable state**, not by the thread's origin:

    owner == "orchestrator"  AND  "pr-review" in tags
      AND the PR named in the title is MERGED
      AND that PR carries an APPROVED review whose ``commit_id``
          is exactly the merged head

Only then may a non-human ``closeable_roles`` identity close. Everything else
stays exactly as it is today — human-only — so the threads that *do* need eyes
are the ones that survive.

## Why the artifact, and only the artifact

Three near-miss sources were rejected, each for a measured reason:

- **``reviewDecision`` (GitHub's roll-up)** — goes stale the moment the head
  moves; the audit found it disagreeing with the per-review record repeatedly
  (msg-949 §4-2). We read per-review ``state`` × ``commit_id`` instead.
- **The naysayer's critique text relayed into the chatroom** — tempting,
  because ``_enforce_close_policies`` has the messages in hand already and it
  would need no network. It is *wrong*: on ``spirrow-mindwire#135`` the review
  body ends ``VERDICT: APPROVE`` while the submitted artifact is
  ``CHANGES_REQUESTED`` (the driver force-RCs a review made on a truncated
  diff). Trusting the prose would have filed away a PR whose last 26 000 diff
  chars nobody had read. The artifact is the gate; the prose is a copy of an
  intention.
- **Merged-ness alone** — that is the bug this exists to catch. #114 and #184
  both shipped with the last commit pushed *after* the last review, so the
  head that merged had never been reviewed.

## P-C is the same predicate

msg-978 proposed P-C (catch a merged head with no APPROVE artifact) as a
separate pre-merge check; msg-1001 §2 collapsed it into this one, because it
is the same question asked at a different moment. Note the honest consequence,
recorded here rather than left implied: this repository has **no branch
protection available on the current GitHub plan** (measured in
``spirrow-mindwire#135``'s own PR body), so nothing can *prevent* an
unreviewed head from merging. What P-C buys is that such a PR can no longer be
quietly filed away — its ledger thread stays open, in view, until a human
rules on it.

## Failure policy: fail-closed, and never worse than today

Any uncertainty — unparseable title, unreachable github-mcp, malformed
payload, missing PAT — yields "not closable". The carve-out simply does not
apply and the caller lands on the pre-existing human-only behaviour. There is
no path here that grants a close it could not prove.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from magickit.utils.logging import get_logger

logger = get_logger(__name__)

#: Thread owner the PR-review driver opens under.
PR_GATE_THREAD_OWNER = "orchestrator"

#: Tag the PR-review driver stamps on the threads it opens.
PR_GATE_THREAD_TAG = "pr-review"

#: GitHub's review state for an approval.
_APPROVED = "APPROVED"

# Same grammar as ``spirrow_mindwire.github.client.parse_pr_ref`` — the titles
# we parse are produced by that module's callers, so the two must agree on what
# a PR reference looks like.
_PR_URL_RE = re.compile(r"github\.com/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)/pull/(\d+)")
_PR_SHORT_RE = re.compile(r"\b([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)#(\d+)\b")


@dataclass(frozen=True)
class PrRef:
    """A parsed ``owner/repo#number``."""

    owner: str
    repo: str
    number: int

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}#{self.number}"


@dataclass(frozen=True)
class LedgerVerdict:
    """Whether a PR-gate ledger thread is mechanically closable, and why.

    ``reason`` is written to be read by whoever was refused, so it always names
    the missing artifact rather than saying "denied".
    """

    closable: bool
    reason: str
    pr_slug: str | None = None
    merged_head: str | None = None
    approving_review_id: int | None = None


def parse_pr_ref(text: str) -> PrRef | None:
    """Pull an ``owner/repo#n`` (or PR URL) out of ``text``; None if absent.

    The URL form is tried first: ``github.com/o/r/pull/5`` contains no ``#`` so
    the short pattern would not match it anyway, but ordering the checks this
    way keeps the intent obvious rather than incidental.
    """
    url = _PR_URL_RE.search(text or "")
    if url is not None:
        return PrRef(owner=url.group(1), repo=url.group(2), number=int(url.group(3)))
    short = _PR_SHORT_RE.search(text or "")
    if short is not None:
        return PrRef(owner=short.group(1), repo=short.group(2), number=int(short.group(3)))
    return None


def is_pr_gate_ledger_thread(thread: dict[str, Any]) -> bool:
    """True for the driver-opened PR-review threads this carve-out covers.

    Deliberately conjunctive and deliberately narrow. ``owner`` alone would
    sweep in anything else the orchestrator ever opens; the tag alone would let
    a hand-opened thread claim the carve-out by writing one word in its tag
    list. Requiring both means a thread qualifies only if the driver made it.
    """
    if not isinstance(thread, dict):
        return False
    tags = thread.get("tags") or []
    if not isinstance(tags, list):
        return False
    return thread.get("owner") == PR_GATE_THREAD_OWNER and PR_GATE_THREAD_TAG in tags


def evaluate_ledger_verdict(
    pr: dict[str, Any] | None,
    reviews: list[dict[str, Any]] | None,
    *,
    pr_slug: str,
) -> LedgerVerdict:
    """The predicate, pure: merged **and** APPROVED at the merged head.

    Split out from the I/O so the acceptance condition (msg-1001 §2: the three
    ``(b)`` threads and the one ``(c)`` thread must be *mechanically* unable to
    close) can be pinned against the real recorded artifacts of those PRs
    without touching the network.

    Args:
        pr: the ``pull_request_read(get)`` payload, or None if it could not be
            read.
        reviews: the ``pull_request_read(get_reviews)`` payload, or None.
        pr_slug: for the message text only.
    """
    if not isinstance(pr, dict):
        return LedgerVerdict(False, f"could not read {pr_slug} from GitHub", pr_slug=pr_slug)

    if not pr.get("merged"):
        state = pr.get("state") or "unknown"
        return LedgerVerdict(
            False,
            f"{pr_slug} is not merged (state={state}). A PR-gate ledger thread "
            f"is filed only once its PR has shipped.",
            pr_slug=pr_slug,
        )

    head = ((pr.get("head") or {}) if isinstance(pr.get("head"), dict) else {}).get("sha")
    if not isinstance(head, str) or not head:
        return LedgerVerdict(
            False,
            f"{pr_slug} is merged but GitHub did not report a head SHA, so "
            f"'reviewed at the head that shipped' cannot be established.",
            pr_slug=pr_slug,
        )

    if not isinstance(reviews, list):
        return LedgerVerdict(
            False,
            f"could not read the reviews of {pr_slug} from GitHub",
            pr_slug=pr_slug,
            merged_head=head,
        )

    for review in reviews:
        if not isinstance(review, dict):
            continue
        # Exact-head only. A review on an earlier commit is evidence about a
        # diff that is not the one that merged (msg-978 §4-2: #114 and #184
        # both grew a commit after their last review).
        if review.get("state") == _APPROVED and review.get("commit_id") == head:
            review_id = review.get("id")
            return LedgerVerdict(
                True,
                f"{pr_slug} is merged at {head} and carries an APPROVED review "
                f"submitted against exactly that commit.",
                pr_slug=pr_slug,
                merged_head=head,
                approving_review_id=review_id if isinstance(review_id, int) else None,
            )

    approved_elsewhere = sorted(
        {
            str(r.get("commit_id"))
            for r in reviews
            if isinstance(r, dict) and r.get("state") == _APPROVED and r.get("commit_id")
        }
    )
    detail = (
        f" There are APPROVED reviews, but on {approved_elsewhere} — not on the "
        f"merged head, so they judged a different diff."
        if approved_elsewhere
        else " No APPROVED review exists on this PR at any commit."
    )
    return LedgerVerdict(
        False,
        f"{pr_slug} merged at {head} with no APPROVED review submitted against "
        f"that commit.{detail} This thread stays open for a human.",
        pr_slug=pr_slug,
        merged_head=head,
    )


def _first_json_payload(result: Any) -> Any:
    """Decode the first text block of an MCP ``tools/call`` result, or None.

    Mirrors ``github_dispatch._pr_base_ref``'s handling: github-mcp answers with
    ``{"content": [{"text": "<json>"}]}`` and anything else is treated as a
    failed read (fail-closed) rather than guessed at.
    """
    content = result.get("content") if isinstance(result, dict) else None
    if not content:
        return None
    try:
        return json.loads(content[0].get("text", ""))
    except (ValueError, TypeError, AttributeError, IndexError, KeyError):
        return None


async def fetch_ledger_verdict(pr: PrRef) -> LedgerVerdict:
    """Read ``pr``'s merge state and reviews from github-mcp, then judge.

    Every failure mode collapses to "not closable": an unset PAT, an
    unreachable container, a malformed payload. The carve-out withholding
    itself leaves the caller on the human-only path that predates it, so a
    GitHub outage costs the loop a bookkeeping convenience and nothing else.
    """
    # Imported lazily: github_dispatch reads its PATs from the environment at
    # call time, and the chatroom tools must stay importable on a deployment
    # that has no GitHub credentials configured at all.
    from magickit.mcp.github_dispatch import _mcp_call, _resolve_pat  # noqa: PLC0415

    args = {"owner": pr.owner, "repo": pr.repo, "pullNumber": pr.number}
    try:
        pat = _resolve_pat("GITHUB_MCP_PAT_IMPLEMENTER")
        pr_payload = _first_json_payload(
            await _mcp_call(
                "tools/call",
                {"name": "pull_request_read", "arguments": {"method": "get", **args}},
                pat,
            )
        )
        reviews_payload = _first_json_payload(
            await _mcp_call(
                "tools/call",
                {"name": "pull_request_read", "arguments": {"method": "get_reviews", **args}},
                pat,
            )
        )
    except Exception as exc:  # noqa: BLE001 — any lookup failure is "unproven"
        logger.warning("pr-gate ledger lookup failed", pr=pr.slug, err=str(exc))
        return LedgerVerdict(
            False,
            f"could not verify {pr.slug} against GitHub ({type(exc).__name__}). "
            f"The close carve-out requires proof, so it does not apply here.",
            pr_slug=pr.slug,
        )

    return evaluate_ledger_verdict(pr_payload, reviews_payload, pr_slug=pr.slug)


def format_ledger_close_note(verdict: LedgerVerdict, author: str) -> str:
    """Machine-readable line recording *why* the loop was allowed to file this.

    Sibling of ``chatroom._format_owner_override_note``: a close that bypassed
    ownership must say so in the body it writes, not only in the audit event.
    """
    return (
        f"\n\n---\n[pr-gate-ledger-close] author={author} pr={verdict.pr_slug} "
        f"merged_head={verdict.merged_head} "
        f"approving_review_id={verdict.approving_review_id}"
    )
