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


@dataclass(frozen=True)
class SettledVerdict:
    """Whether a PR-gate ledger thread has been Tier-C-settled by a human.

    Answers a **different** question than :class:`LedgerVerdict`. The
    ledger verdict rules on "may this be filed away by the loop?"; this
    one rules on "did a human already decide?" — used by the board's
    ``マージ`` lane to display ``Tier-C 決着 (要確認)`` cards for PRs
    whose independent-naysayer round was ended in the chatroom rather
    than on GitHub.

    Prose is deliberately never read (Bohr msg-827): "the human decided"
    is a structural signal on the thread (``write_state == closed`` OR the
    last message is a human ``decide`` with no naysayer verdict after it),
    and *what* the human decided (ship / reject / hold) is not knowable
    from metadata. The card the board draws from this verdict points at
    the chatroom thread and asks the human to confirm — it never claims
    the PR is authorized to merge.

    ``settled_msg_id`` names the message that carried the human's
    decision, and is included so the card subtitle can link back to the
    exact moment of the ruling.
    """

    settled: bool
    reason: str
    settled_msg_id: str | None = None


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


#: Message ``type`` values that count as a naysayer's verdict on the PR.
#: Kept narrow on purpose — a naysayer comment (``type == "comment"``) is
#: not a verdict, so a settled thread that carries an incidental naysayer
#: aside after the human's decide is still settled.
_NAYSAYER_VERDICT_MSG_TYPES = frozenset({"review", "reject", "approve"})

#: Thread status values that mean "closed" for the settled-verdict
#: predicate. Kept in lockstep with :data:`magickit.web.decisions.
#: _THREAD_STATUS_CLOSED` so the board and the ledger cannot disagree
#: on what "closed" means.
_CLOSED_STATUS = frozenset({"resolved", "superseded"})


def _is_human_decide_msg(
    msg: dict[str, Any], human_identities: frozenset[str]
) -> bool:
    """A human-authored ``type == "decide"`` message, or False.

    Mirrors :func:`magickit.web.decisions._is_human_decide` (the two
    modules must agree on what "the human already decided" means; a diff
    between them would put a card in one state on the board and another
    in the judgement page). Author is lowercased for comparison because
    Prismind normalizes identity names on read but chatroom payloads
    round-trip whatever they were written with.
    """
    author = str(msg.get("author") or "").strip().lower()
    if author not in human_identities:
        return False
    return str(msg.get("type") or "") == "decide"


def evaluate_settled_verdict(
    thread: dict[str, Any] | None,
    messages: list[dict[str, Any]] | None,
    *,
    head_sha: str | None,
    human_identities: frozenset[str],
    naysayer_identities: frozenset[str],
) -> SettledVerdict:
    """Has the human ended the naysayer round on this ledger thread?

    Two shapes of settlement, both structural (Bohr msg-825 §3):

    1. **``thread.status`` is closed** (``resolved`` / ``superseded``) —
       Conclair's own terminal states. If the underlying PR is still open,
       the naysayer will never speak again on this thread, so the human
       has definitively taken over the adjudication.

    2. **The last message is a human ``decide``** with no naysayer
       verdict after it. This catches PR #83's real pathway (msg-800 →
       msg-806): the thread was left open (``NEXT: Heisenberg``) but the
       human had already ruled.

    Both are gated by **head-anchor**: the ledger thread's title carries
    the PR reference, but the human's decision applied to whatever
    ``head_sha`` was live at the time. If the PR has since grown a new
    commit, the settlement no longer describes the current diff, and
    this predicate returns ``settled=False``. Symmetric with the
    APPROVED-at-head predicate on the ledger side.

    Prose is never parsed. What the human decided (ship / reject /
    hold) is unrecoverable from metadata; the board carries that
    honestly by rendering ``Tier-C 決着 (要確認)`` and pointing the
    primary link at the chatroom thread rather than at merge.

    Args:
        thread: the ``list_threads`` / ``get_thread`` payload for the
            ledger thread. ``None`` when unreadable.
        messages: the message list from ``get_thread``. ``None`` when
            unreadable.
        head_sha: the current head SHA of the underlying PR, as seen by
            :class:`~magickit.core.pr_watch.PrSnapshot`. ``None`` when we
            could not determine it.
        human_identities: :data:`magickit.mcp.tools.chatroom.
            HUMAN_IDENTITY_NAMES` (a frozenset).
        naysayer_identities: :attr:`magickit.config.Settings.
            naysayer_identities` (a frozenset).
    """
    if thread is None:
        return SettledVerdict(False, "ledger thread not readable")
    if head_sha is None or not head_sha:
        return SettledVerdict(False, "PR head unknown; cannot anchor a settlement")

    status = str(thread.get("status") or "")

    # Shape 1: the thread itself is closed. No naysayer will speak again.
    if status in _CLOSED_STATUS:
        return SettledVerdict(
            True,
            f"ledger thread is {status}; the human has taken over adjudication.",
            settled_msg_id=str(thread.get("last_msg_id") or "") or None,
        )

    if not isinstance(messages, list) or not messages:
        return SettledVerdict(False, "ledger thread has no readable messages")

    # Shape 2: last message is a human decide, and no naysayer verdict
    # has been posted after it. Scan from the tail so we can bail early.
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        author = str(msg.get("author") or "").strip().lower()
        msg_type = str(msg.get("type") or "")
        if author in naysayer_identities and msg_type in _NAYSAYER_VERDICT_MSG_TYPES:
            # A naysayer verdict is the newest thing on the thread; the
            # human has not yet ruled after it. Not settled.
            return SettledVerdict(
                False,
                "the newest message is a naysayer verdict; the human has "
                "not ruled after it.",
            )
        if _is_human_decide_msg(msg, human_identities):
            return SettledVerdict(
                True,
                "the newest human decide on this thread ends the round.",
                settled_msg_id=str(msg.get("msg_id") or "") or None,
            )
        # Anything else (agent comment, orchestrator note, non-decide
        # human msg) — keep scanning back.
    return SettledVerdict(
        False,
        "no human decide message found on the ledger thread.",
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


def ledger_close_sanction(verdict: LedgerVerdict) -> dict[str, str] | None:
    """The wire ``close_sanction`` for a carve-out close, or None to claim nothing.

    Conclair's ``kind="pr_gate_ledger"`` requires ``pr`` / ``merged_head`` /
    ``approving_review_id`` all present and non-empty and refuses ``reason`` on
    that kind: a carve-out record exists so a reader can re-derive the claim
    later against GitHub, and ``chatroom_events`` is append-only, so a hollow
    record could never be repaired. This projects the verdict onto exactly that
    shape.

    Returns None -- "no claim" -- whenever the evidence is short, which Conclair
    records as ``kind="unspecified"``. That is deliberately not a failure path:
    a close that succeeds today keeps succeeding, and the shortfall shows up as
    the ``unclassified_override`` count rather than as a 422 that would stop the
    loop. ``approving_review_id`` is an ``int`` here and a ``str`` on the wire,
    so it is converted; pydantic v2 refuses an int for a ``str`` field.
    """
    if not verdict.closable:
        return None
    review_id = verdict.approving_review_id
    evidence = {
        "pr": verdict.pr_slug or "",
        "merged_head": verdict.merged_head or "",
        "approving_review_id": str(review_id) if review_id is not None else "",
    }
    if not all(evidence.values()):
        return None
    return {"kind": "pr_gate_ledger", **evidence}


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
