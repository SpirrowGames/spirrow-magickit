"""Settled-disposition close carve-out -- "may the loop close a settled thread?"

`T-done-disposition-auto-close` (spirrow-mindwire), design v2.4 msg-8185
(naysayer APPROVE msg-8186), slice 1b-ii in msg-8218 §3.

## The problem this closes

A design thread whose head says "done" stays open until its *owner* closes
it. Many owners are not the loop's roles (``orchestrator``, ``Fermi``, a
human), and Conclair's owner check refuses everybody else. On 2026-11-08 the
mindwire chatroom had 66 such threads open (msg-8153 §0).

## The rule (carve-out (4) in ``_enforce_close_policies``)

A non-human, non-owner closer may close a thread when, read by this module:

1. the caller names the head it judged (``settled_head_msg_id``) and that is
   exactly the thread's current head, both as the last message of the
   ``mode="full"`` read and as the thread's own ``last_msg_id`` rollup
   (Einstein msg-8186 note (ii): the grace below is judged on the very message
   Conclair will CAS against);
2. that head stores ``disposition.kind == "done"`` and no
   ``next_participant`` (structured fields only -- no prose is read here,
   msg-8153 §3-2);
3. that head was posted with the verified role ``proposer`` (msg-8185
   Obj-5-1: ADR-10 gives naysayer no close power, implementer done must not
   close the design it implemented, human done can be closed by the human,
   relay posts carry no role);
4. the head is at least ``grace`` old (default 24h; a reply in between moves
   the head and so falls out at step 1);
5. the thread is open and is not a PR-gate ledger thread (carve-out (3) owns
   those);
6. the owner is a registered identity that is not the human (resolved in
   ``chatroom._settled_owner_refusal``, since it needs Prismind).

Conclair re-checks 1-3 under its row lock (``close_sanction.kind ==
"settled_disposition"``, spirrow-conclair#34); this module exists so the grace
and the human-owner exclusion -- which Conclair deliberately does not check,
msg-1087 §0 -- are judged on the same message.

Everything not provable is a refusal. There is no "claim nothing and let
Conclair decide" path here, unlike :func:`pr_gate_ledger.ledger_close_sanction`:
the caller asked for this carve-out by name, so a shortfall is an answer to
report, not a reason to fall back to the owner check.

## What this does not establish

``author`` is not authenticated at the MCP layer (see the note on
``CLOSEABLE_ROLES``). Anyone naming a configured closer can reach this
carve-out without going through mindwire's resolver. The ceiling on that risk
is msg-8179 I-2: everything this admits is already closable today by naming
the owner or the human. Enabling the carve-out (configuring a closer) is a
separate Tier-C decision that must state this.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from magickit.mcp.disposition import KIND_DONE

# Must equal Conclair's ``SETTLED_DISPOSITION_ROLE`` (services/close_sanction.py,
# spirrow-conclair#34). If the two drift the outcome is a 422 from Conclair,
# i.e. a refused close -- fail-closed, never a wider one.
SETTLED_DISPOSITION_ROLE = "proposer"

# The wire kind Conclair checks (schemas/close_sanction_vocab.py).
SETTLED_DISPOSITION_KIND = "settled_disposition"

# Thread statuses Conclair accepts a close on. Anything else (resolved,
# superseded, parked, or a status this code has never seen) is refused here.
OPEN_STATUSES = frozenset({"active", "awaiting_reply"})

DEFAULT_GRACE_SECONDS = 24 * 60 * 60

# Refusal codes, carried in the error envelope's ``details.reason`` so a
# client (mindwire settled_close) can classify without parsing prose.
REASON_HEAD_MOVED = "head_moved"
REASON_HEAD_UNKNOWN = "head_unknown"
REASON_NOT_OPEN = "not_open"
REASON_PR_GATE_LEDGER = "pr_gate_ledger"
REASON_NOT_DONE = "not_done"
REASON_NAMES_SUCCESSOR = "names_successor"
REASON_ROLE_NOT_ELIGIBLE = "role_not_eligible"
REASON_TIMESTAMP_UNREADABLE = "timestamp_unreadable"
REASON_GRACE_PENDING = "grace_pending"
REASON_OWNER_MISSING = "owner_missing"
REASON_OWNER_HUMAN = "owner_human"
REASON_OWNER_UNRESOLVED = "owner_unresolved"
REASON_OWNER_LOOKUP_UNAVAILABLE = "owner_lookup_unavailable"
REASON_OWNER_IS_AUTHOR = "owner_is_author"
REASON_HUMAN_CALLER = "human_caller"
REASON_CLOSER_NOT_CONFIGURED = "closer_not_configured"
REASON_CLOSER_UNREGISTERED = "closer_unregistered"
REASON_CLOSER_LOOKUP_UNAVAILABLE = "closer_lookup_unavailable"
REASON_THREAD_UNREADABLE = "thread_unreadable"
REASON_GATE_DISABLED = "gate_disabled_on_gated_thread"
REASON_SETTINGS_UNAVAILABLE = "settings_unavailable"


@dataclass(frozen=True)
class SettledVerdict:
    """The pure part of the decision. ``closable`` False always has a reason."""

    closable: bool
    reason: str
    detail: str
    head_msg_id: str | None = None
    owner: str | None = None


def _refuse(reason: str, detail: str, **kw: Any) -> SettledVerdict:
    return SettledVerdict(False, reason, detail, **kw)


def parse_timestamp(raw: Any) -> datetime | None:
    """An aware UTC datetime from Conclair's ISO-8601 string, else None.

    A naive timestamp is refused rather than assumed UTC: its age is not
    knowable, and the grace is the one check here that a wrong guess would
    silently pass.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def assess_settled_disposition(
    *,
    thread: dict[str, Any],
    messages: list[dict[str, Any]],
    head_msg_id: str,
    is_ledger_thread: bool,
    now: datetime,
    grace: timedelta,
) -> SettledVerdict:
    """Steps 1-5 of the rule, on one ``mode="full"`` read. No I/O.

    The owner's identity kind (step 6) needs Prismind and is decided by the
    caller; this only refuses an owner that is missing outright.
    """
    claimed = head_msg_id.strip() if isinstance(head_msg_id, str) else ""
    if not claimed:
        return _refuse(REASON_HEAD_UNKNOWN, "settled_head_msg_id is empty.")

    status = thread.get("status")
    if status not in OPEN_STATUSES:
        return _refuse(
            REASON_NOT_OPEN,
            f"thread status is {status!r}; only {sorted(OPEN_STATUSES)} can be closed.",
            head_msg_id=claimed,
        )

    if is_ledger_thread:
        return _refuse(
            REASON_PR_GATE_LEDGER,
            "this is a PR-gate ledger thread; its close is decided by the "
            "pr_gate_ledger carve-out, not by a settled disposition.",
            head_msg_id=claimed,
        )

    if not isinstance(messages, list) or not messages or not isinstance(messages[-1], dict):
        return _refuse(
            REASON_HEAD_UNKNOWN, "the thread read returned no messages.", head_msg_id=claimed
        )
    head = messages[-1]
    actual = head.get("msg_id")
    rollup = thread.get("last_msg_id")
    if not isinstance(actual, str) or not isinstance(rollup, str) or actual != rollup:
        # The read is internally inconsistent (or predates the rollup): which
        # message is the head is not knowable from it.
        return _refuse(
            REASON_HEAD_UNKNOWN,
            f"the read's last message ({actual!r}) and the thread's last_msg_id "
            f"({rollup!r}) do not agree.",
            head_msg_id=claimed,
        )
    if actual != claimed:
        return _refuse(
            REASON_HEAD_MOVED,
            f"the head is {actual}, not {claimed}: something was posted after "
            "the message the caller judged.",
            head_msg_id=claimed,
        )

    disposition = head.get("disposition")
    if not isinstance(disposition, dict) or disposition.get("kind") != KIND_DONE:
        return _refuse(
            REASON_NOT_DONE,
            f"the head {actual} does not store disposition.kind == 'done'.",
            head_msg_id=claimed,
        )
    if head.get("next_participant") is not None:
        return _refuse(
            REASON_NAMES_SUCCESSOR,
            f"the head {actual} is done but also names next_participant "
            f"{head.get('next_participant')!r}; the two contradict.",
            head_msg_id=claimed,
        )
    if head.get("role") != SETTLED_DISPOSITION_ROLE:
        return _refuse(
            REASON_ROLE_NOT_ELIGIBLE,
            f"the head {actual} was posted with role {head.get('role')!r}; only "
            f"{SETTLED_DISPOSITION_ROLE!r} may settle a thread for this carve-out.",
            head_msg_id=claimed,
        )

    posted = parse_timestamp(head.get("timestamp"))
    if posted is None:
        return _refuse(
            REASON_TIMESTAMP_UNREADABLE,
            f"the head {actual} has no readable timezone-aware timestamp "
            f"({head.get('timestamp')!r}).",
            head_msg_id=claimed,
        )
    age = now - posted
    if age < grace:
        return _refuse(
            REASON_GRACE_PENDING,
            f"the head {actual} is {int(age.total_seconds())}s old; the grace is "
            f"{int(grace.total_seconds())}s.",
            head_msg_id=claimed,
        )

    owner = thread.get("owner")
    if not isinstance(owner, str) or not owner.strip():
        return _refuse(
            REASON_OWNER_MISSING, "the thread has no owner to check.", head_msg_id=claimed
        )

    return SettledVerdict(True, "", "", head_msg_id=claimed, owner=owner)


def settled_close_sanction(head_msg_id: str) -> dict[str, str]:
    """The wire ``close_sanction`` Conclair checks under its row lock."""
    return {"kind": SETTLED_DISPOSITION_KIND, "head_msg_id": head_msg_id}


def settled_close_reason(head_msg_id: str, grace: timedelta) -> str:
    """Fixed ``owner_override_reason`` text: no caller prose, no LLM."""
    return (
        f"settled_disposition: proposer done at {head_msg_id}, no later message "
        f"for at least {int(grace.total_seconds())}s"
    )


def format_settled_close_note(
    *, author: str, owner: str, head_msg_id: str, grace: timedelta
) -> str:
    """Machine-readable line recording why a non-owner could close.

    Sibling of ``format_ledger_close_note``: a close that bypassed ownership
    says so in the body it writes, not only in the audit event.
    """
    return (
        f"\n\n---\n[settled-disposition-close] author={author} owner={owner} "
        f"head={head_msg_id} grace_seconds={int(grace.total_seconds())}"
    )
