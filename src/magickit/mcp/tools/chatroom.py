"""Chatroom MCP tools — thin wrappers around ChatroomAdapter.

Exposes spirrow-conclair's HTTP endpoints as MCP tools so AI sessions
(Claude.ai / Claude Code) can post / read chatroom messages without
hand-crafting HTTP calls. Each tool delegates to ChatroomAdapter and
forwards the response as-is, including conclair's structured error
envelope (`{error_type, error, details}`) when the upstream returned
4xx/5xx.
"""

from __future__ import annotations

from typing import Any, Literal, NamedTuple

from fastmcp import FastMCP

from magickit.adapters.chatroom import ChatroomAdapter
from magickit.adapters.prismind import PrismindAdapter
from magickit.config import Settings
from magickit.mcp.pr_gate_ledger import (
    LedgerVerdict,
    fetch_ledger_verdict,
    format_ledger_close_note,
    is_pr_gate_ledger_thread,
    parse_pr_ref,
)
from magickit.utils.logging import get_logger

logger = get_logger(__name__)

_settings: Settings | None = None

# ADR-2026-05-29-12 §3: identity_names that are exempt from the
# mandatory embodiment declaration on state-transitioning msgs
# ({handoff, ack, decide} per §4). The "human" identity does not
# operate Magickit as a calling agent (ADR-10 §2.7 "human is the
# above-loop approval layer"); they normally act via Bohr / Heisenberg /
# Einstein. The set is intentionally tiny -- if other actor classes
# need the exemption later, treat that as a separate ADR.
HUMAN_IDENTITY_NAMES = ("human",)

# msg types whose post requires an embodiment declaration (state-
# transitioning per msg-325 §4 N-2 取り込み). close_thread emits a
# decide internally so it's enforced separately by the close wrapper.
#
# G-01 (Einstein, msg-014 / accepted msg-015, msg-016 §1): why `ack` is in
# the mandatory set is not self-evident, so record it here rather than let a
# future reader infer "ack is passive, make it optional". All three entries
# move thread.status (msg-325 §4 / ADR-2026-05-29-12):
#   handoff: active          -> awaiting_reply
#   ack:     awaiting_reply  -> active          (the reverse transition)
#   decide:  active/awaiting_reply -> resolved  (paired with closes_thread)
# `ack` earns its place by being a state transition, not by being effortful.
MANDATORY_EMBODIMENT_MSG_TYPES = ("handoff", "ack", "decide")

# --- second-stage close gate (P3) --------------------------------------
#
# F-07 (Einstein): record *why* this set is what it is, next to the set, so a
# later reader does not have to reconstruct it from a 37-message thread.
#
# Decided in T-magickit-identity-extension msg-003 D-3 / msg-005: closing a
# thread is an integrative act, so it belongs to the roles that carry the work
# forward. `reviewer` and `dogfooder` are deliberately excluded -- both observe
# rather than integrate, and neither should be able to declare a thread
# settled. `Einstein` is excluded structurally rather than by name: its record
# is `allowed_roles=["naysayer"]`, which does not intersect this set, so the
# advisory naysayer cannot close the threads it reviews (the same separation
# the naysayer gate enforces from the other direction).
#
# `human` is NOT in this set and must never be added to it. The human record is
# `allowed_roles=["human"]` (verified live 2026-08-02), which intersects
# nothing here -- so the human is exempted from this stage explicitly (I-8,
# see `_check_close_permitted`) rather than by widening the set. Widening it
# would put "human" into every identity's closeable vocabulary; the exemption
# keeps the effect on the one identity it is meant for.
#
# What this stage is, precisely (msg-041 Q5): a misconfiguration guard, not an
# authorization boundary. `author` is an unauthenticated string at the MCP
# layer, so anyone determined to bypass this can pass a name that is exempt.
# It stops a correctly-behaving actor from closing what its role does not
# cover; it does not stop a dishonest one, and no wording here should suggest
# otherwise. Authenticating `author` is a separate, pre-existing concern and
# is deliberately not in this change's scope.
CLOSEABLE_ROLES = ("implementer", "integrator", "proposer")

# --- design-decide naysayer gate ---------------------------------------
#
# Binding design threads (those carrying the configured gate tag) may only
# be closed (decide) when a *fresh* independent-naysayer review approves, or
# a human supplies an explicit override. This enforces "proposer cannot
# silently overrule the advisory naysayer to land a binding design
# decision" structurally, at Magickit's single role/enforcement point.
#
# A "substantive" message — one whose appearance *after* a review makes that
# review stale — is any proposer/implementer-origin message except a bare
# `ack` / read-cursor bookkeeping. We approximate "proposer/implementer
# origin" as "not the naysayer and not the human": the naysayer's own
# follow-ups and human override notes must not invalidate their own review.
SUBSTANTIVE_MSG_TYPES = (
    "propose", "question", "answer", "decide", "report", "handoff",
)

# Reserved per-message tag carrying the naysayer's verdict (messages have a
# `tags` list but no free-form metadata dict in the Conclair schema, so the
# verdict rides on a reserved tag; a `VERDICT:` body line is the fallback).
VERDICT_TAG_PREFIX = "verdict:"

_VERDICT_APPROVE = "approve"
_VERDICT_REQUEST_CHANGES = "request_changes"
_VERDICT_SYNONYMS = {
    "approve": _VERDICT_APPROVE,
    "approved": _VERDICT_APPROVE,
    "endorse": _VERDICT_APPROVE,
    "endorsed": _VERDICT_APPROVE,
    "request_changes": _VERDICT_REQUEST_CHANGES,
    "request-changes": _VERDICT_REQUEST_CHANGES,
    "changes": _VERDICT_REQUEST_CHANGES,
    "reject": _VERDICT_REQUEST_CHANGES,
    "rejected": _VERDICT_REQUEST_CHANGES,
}


def _normalize_verdict(raw: str) -> str | None:
    """Map a raw verdict token to ``approve`` / ``request_changes`` / None."""
    return _VERDICT_SYNONYMS.get(raw.strip().lower())


def _parse_msg_verdict(msg: dict[str, Any]) -> str | None:
    """Extract a naysayer verdict from a message, or None if absent.

    Primary form: a reserved tag ``verdict:<value>`` on the message.
    Fallback: the last ``VERDICT: <value>`` line in the body (case-
    insensitive). Returns the normalized verdict or None when neither a
    tag nor a body line yields a recognized verdict.
    """
    for tag in msg.get("tags") or []:
        if isinstance(tag, str) and tag.strip().lower().startswith(VERDICT_TAG_PREFIX):
            verdict = _normalize_verdict(tag.split(":", 1)[1])
            if verdict is not None:
                return verdict
    # Body fallback: scan lines bottom-up for a `VERDICT: x` marker.
    content = msg.get("content") or ""
    for line in reversed(content.splitlines()):
        stripped = line.strip()
        if stripped.lower().startswith("verdict:"):
            verdict = _normalize_verdict(stripped.split(":", 1)[1])
            if verdict is not None:
                return verdict
    return None


def _latest_naysayer_review(
    messages: list[dict[str, Any]], naysayer_identities: tuple[str, ...]
) -> tuple[int, dict[str, Any], str] | None:
    """Return (index, msg, verdict) of the latest reviewable naysayer msg.

    A reviewable message is authored by an identity in ``naysayer_identities``
    *and* carries a parseable verdict. None when no such message exists.
    Messages are assumed to be in chronological (msg_id) order.
    """
    found: tuple[int, dict[str, Any], str] | None = None
    for idx, msg in enumerate(messages):
        if msg.get("author") in naysayer_identities:
            verdict = _parse_msg_verdict(msg)
            if verdict is not None:
                found = (idx, msg, verdict)
    return found


def _first_substantive_after(
    messages: list[dict[str, Any]],
    after_index: int,
    naysayer_identities: tuple[str, ...],
    human_identities: tuple[str, ...],
) -> dict[str, Any] | None:
    """First proposer/implementer substantive msg after ``after_index``.

    Used for freshness: such a message means the review predates new
    substantive discussion and must be re-issued. Naysayer and human
    messages are ignored (they don't invalidate the naysayer's own review).
    """
    for msg in messages[after_index + 1:]:
        if msg.get("type") not in SUBSTANTIVE_MSG_TYPES:
            continue
        author = msg.get("author")
        if author in naysayer_identities or author in human_identities:
            continue
        return msg
    return None


def _gate_error(error_type: str, message: str, **details: Any) -> dict[str, Any]:
    """Build a naysayer-gate rejection envelope (project error_type convention)."""
    return {"error_type": error_type, "error": message, "details": details}


def _format_override_note(author: str, reason: str) -> str:
    """Machine-readable override line appended to the decide msg body.

    Recorded in the persisted decide message (and thus the audit trail) so a
    human override of the gate is never silent (req: override は理由付きで
    decide msg / 監査イベントに記録).
    """
    return (
        f"\n\n---\n[naysayer-gate-override] author={author} "
        f"reason={reason.strip()}"
    )


def _assess_naysayer_gate(
    *,
    thread: dict[str, Any],
    messages: list[dict[str, Any]],
    naysayer_identities: tuple[str, ...],
    gate_tag: str,
    human_identities: tuple[str, ...],
    author: str,
    override_reason: str,
) -> dict[str, Any]:
    """Pure decision for the naysayer gate on a close/decide.

    Returns one of:
    - ``{"action": "allow", "gated": bool, ...}`` — proceed unchanged.
    - ``{"action": "override", "note": str}`` — human override engaged;
      caller appends ``note`` to the decide body, then proceeds.
    - ``{"action": "block", "envelope": {...}}`` — return the error envelope.
    """
    if gate_tag not in (thread.get("tags") or []):
        return {"action": "allow", "gated": False}

    # Gated thread. A human override short-circuits the review requirement
    # but must come from a human identity and carry a reason.
    if override_reason and override_reason.strip():
        if author not in human_identities:
            return {
                "action": "block",
                "envelope": _gate_error(
                    "NaysayerOverrideForbiddenError",
                    "naysayer-gate override is restricted to human identities "
                    f"(author={author!r} is not in {list(human_identities)}); "
                    "a proposer/implementer agent cannot self-override",
                    author=author,
                    human_identities=list(human_identities),
                ),
            }
        return {"action": "override", "note": _format_override_note(author, override_reason)}

    review = _latest_naysayer_review(messages, naysayer_identities)
    if review is None:
        return {
            "action": "block",
            "envelope": _gate_error(
                "NaysayerReviewRequiredError",
                "this binding-design thread requires a fresh independent "
                "naysayer review before it can be closed; none was found. "
                "Summon a naysayer to review, or pass naysayer_override_reason "
                "as a human identity.",
                gate_tag=gate_tag,
                naysayer_identities=list(naysayer_identities),
            ),
        }

    review_index, review_msg, verdict = review
    stale_by = _first_substantive_after(
        messages, review_index, naysayer_identities, human_identities
    )
    if stale_by is not None:
        return {
            "action": "block",
            "envelope": _gate_error(
                "NaysayerReviewStaleError",
                f"the naysayer review ({review_msg.get('msg_id')}) is stale: "
                f"substantive message {stale_by.get('msg_id')} "
                f"(type={stale_by.get('type')}, author={stale_by.get('author')}) "
                "was posted after it. Re-request a naysayer review of the "
                "current state, or pass a human override.",
                review_msg_id=review_msg.get("msg_id"),
                stale_by_msg_id=stale_by.get("msg_id"),
            ),
        }

    if verdict == _VERDICT_APPROVE:
        return {"action": "allow", "gated": True, "review_msg_id": review_msg.get("msg_id")}

    # Fresh review exists but requested changes.
    return {
        "action": "block",
        "envelope": _gate_error(
            "NaysayerChangesRequestedError",
            f"the naysayer review ({review_msg.get('msg_id')}) requested changes "
            "and they are not yet approved. Address them and obtain a fresh "
            "approving review, or pass naysayer_override_reason as a human "
            "identity.",
            review_msg_id=review_msg.get("msg_id"),
            verdict=verdict,
        ),
    }


def _format_owner_override_note(author: str, thread_owner: str | None, reason: str) -> str:
    """Machine-readable line recording a human force-close of a non-owned thread.

    Mirrors the naysayer-override note (ADR-2026-06-04-19 D-5 audit
    requirement): the decide body carries "who force-closed whose thread, and
    why" in addition to the structured Conclair audit event.
    """
    return (
        f"\n\n---\n[owner-override-by-human] author={author} "
        f"owner={thread_owner} reason={reason.strip()}"
    )


async def _enforce_close_policies(
    adapter: ChatroomAdapter,
    *,
    project: str,
    thread_id: str,
    author: str,
    body_content: str,
    naysayer_override_reason: str,
    owner_override_reason: str,
) -> dict[str, Any]:
    """Apply the naysayer gate AND the ownership policies for a close/decide.

    Three independent policies on the same close path, in this order:

    1. **naysayer gate (#9)** — a gated thread needs a fresh approving review
       or a human gate-override. Unchanged semantics. It runs *first* so that
       neither ownership bypass below can double as a gate bypass.
    2. **human owner-override (ADR-2026-06-04-19 D-5)** — a human may
       force-close a non-owned thread. Magickit is the decision point; Conclair
       honours the flag without deciding who may set it.
    3. **PR-gate ledger carve-out (T-pr-gate-ledger-debt, msg-1001 §2)** — a
       non-human ``closeable_roles`` identity may close a *driver-opened
       PR-review thread* when GitHub proves the PR merged with an APPROVED
       review at the merged head. See :mod:`magickit.mcp.pr_gate_ledger` for
       why the artifact — not the relayed critique text — is the evidence.

    (2) and (3) are the same mechanism (set ``owner_override``) reached by two
    different proofs: "you are the human" and "the state is provable". (3) is
    scoped so narrowly that it cannot widen (2): it requires the driver's own
    owner *and* tag, and it refuses on any uncertainty.

    Returns ``{"action": "block", "envelope": {...}}`` or
    ``{"action": "proceed", "content": str, "owner_override": bool,
    "owner_override_reason": str | None}``.
    """
    is_human = author in HUMAN_IDENTITY_NAMES
    gate_enabled = _settings is not None and _settings.naysayer_gate_enabled
    content = body_content
    # Humans may force-close; the flag is harmless when the human is the owner
    # (Conclair only records a bypass when author != owner).
    owner_override = is_human

    # The thread is needed to run the gate, to resolve a human force-close, and
    # to recognise a PR-gate ledger thread — which is every non-trivial case,
    # so the read is now unconditional (it used to be skipped for a non-human
    # author on a deployment with the gate off).
    if _settings is None:
        return {
            "action": "proceed", "content": content,
            "owner_override": owner_override, "owner_override_reason": None,
        }

    view = await adapter.get_thread(project=project, thread_id=thread_id, mode="full")
    if "error_type" in view:
        if gate_enabled or is_human:
            # fail-closed: a gated/forced close must prove its preconditions.
            return {"action": "block", "envelope": view}
        # Legacy path (gate off, non-human): an unreadable thread was never a
        # block here — Conclair's own owner check answers the call. Preserved
        # verbatim so adding the ledger read changes no pre-existing outcome.
        return {
            "action": "proceed", "content": content,
            "owner_override": owner_override, "owner_override_reason": None,
        }
    thread = view.get("thread") or {}
    messages = view.get("messages") or []
    gate_tag = _settings.naysayer_gate_tag
    gated = gate_tag in (thread.get("tags") or [])

    # 1) naysayer gate (ownership-independent; runs first so owner bypass
    #    never doubles as a gate bypass).
    if gate_enabled:
        gate = _assess_naysayer_gate(
            thread=thread, messages=messages,
            naysayer_identities=tuple(_settings.naysayer_identities),
            gate_tag=gate_tag, human_identities=HUMAN_IDENTITY_NAMES,
            author=author, override_reason=naysayer_override_reason,
        )
        if gate["action"] == "block":
            return {"action": "block", "envelope": gate["envelope"]}
        if gate["action"] == "override":
            content = content + gate["note"]

    # 2) human owner-override (force-close of a non-owned thread).
    forwarded_reason: str | None = None
    if is_human:
        thread_owner = thread.get("owner")
        # Only a *confirmed* force-close (owner known and not the author)
        # requires a reason / audit note. An absent owner is not assertable.
        if thread_owner is not None and author != thread_owner:
            # gated force-close reuses the naysayer override reason (D-5);
            # a non-gated force-close requires its own reason.
            reason = (naysayer_override_reason if gated else owner_override_reason) or ""
            if not gated and not reason.strip():
                return {
                    "action": "block",
                    "envelope": _gate_error(
                        "OwnerOverrideReasonRequiredError",
                        "force-closing a non-owned thread as a human requires "
                        "owner_override_reason for the audit trail. Provide a reason.",
                        thread_owner=thread_owner, author=author,
                    ),
                }
            forwarded_reason = reason or None
            content = content + _format_owner_override_note(author, thread_owner, reason)

    # 3) PR-gate ledger carve-out (T-pr-gate-ledger-debt msg-1001 §2). Only for
    #    a non-human closing someone else's thread — the human already has (2),
    #    and the owner never needed a bypass at all.
    if not is_human and author != thread.get("owner") and is_pr_gate_ledger_thread(thread):
        pr = parse_pr_ref(str(thread.get("title") or ""))
        verdict = (
            await fetch_ledger_verdict(pr)
            if pr is not None
            else LedgerVerdict(
                False,
                "this PR-review thread's title carries no parseable "
                "owner/repo#number, so the PR it records cannot be checked.",
            )
        )
        if not verdict.closable:
            return {
                "action": "block",
                "envelope": _gate_error(
                    "PrGateLedgerNotClosableError",
                    f"this PR-gate ledger thread is not mechanically closable: "
                    f"{verdict.reason} Filing it anyway is a judgement call, which "
                    f"is a human's to make (close it as the human identity with "
                    f"owner_override_reason).",
                    thread_owner=thread.get("owner"),
                    author=author,
                    pr=verdict.pr_slug,
                    merged_head=verdict.merged_head,
                ),
            }
        owner_override = True
        forwarded_reason = verdict.reason
        content = content + format_ledger_close_note(verdict, author)

    return {
        "action": "proceed", "content": content,
        "owner_override": owner_override, "owner_override_reason": forwarded_reason,
    }


def _adapter() -> ChatroomAdapter:
    if _settings is None:
        raise RuntimeError("Settings not initialized")
    return ChatroomAdapter(
        base_url=_settings.conclair_url,
        timeout=_settings.conclair_timeout,
    )


def _prismind_adapter() -> PrismindAdapter:
    """Build the identity-lookup adapter for the role gate.

    Deliberately NOT paired with a ``close()`` the way ``_adapter()`` is in
    every write path below. The two adapters have different lifetimes and the
    asymmetry is load-bearing, so it is written down here rather than left to
    look like an omission (T-pr-review-11 msg-020 read it as one):

    - ``ChatroomAdapter`` extends ``BaseAdapter``, which holds an
      ``httpx.AsyncClient`` on ``self._client`` across calls. That client is a
      real resource with a real ``async def close()``; leaving it open leaks a
      connection pool, hence ``try/finally: await adapter.close()``.
    - ``PrismindAdapter`` extends ``MCPBaseAdapter``, which stores only
      ``sse_url`` (str) and ``timeout`` (float). Construction opens nothing.
      Each call enters ``_get_session()``, whose ``async with sse_client(...)``
      / ``async with ClientSession(...)`` open **and close** the SSE connection
      within that one call. There is nothing left to release afterwards.

    ``MCPBaseAdapter`` therefore has no ``close()`` anywhere in its MRO, and
    none of the ~30 ``PrismindAdapter(...)`` sites in this codebase close one.
    Adding ``await prismind.close()`` here would not be a no-op either: the
    base class routes unknown attributes through ``__getattr__`` to dynamic MCP
    tool dispatch, so it would issue ``call_tool("close", {})`` -- an extra SSE
    connect + initialize + Unknown-tool round trip on every validated post.
    ``test_role_gate.py::test_mcp_adapter_has_no_close_to_call`` pins this.
    """
    if _settings is None:
        raise RuntimeError("Settings not initialized")
    return PrismindAdapter(
        sse_url=_settings.prismind_url,
        timeout=_settings.prismind_timeout,
    )


# --- role × allowed_roles gate -----------------------------------------
#
# msg-002 §2.3 / msg-017 I-1..I-4. Magickit is the sole enforcement point
# (F-04): Prismind persists the identity record, Conclair persists the
# per-message `role` column, neither validates.
#
# The gate is deliberately opt-in on the *supply* side and mandatory on the
# *validation* side:
#
#   role omitted   -> no lookup, no check, record null. This is I-3
#                     (legacy compatibility): every caller predating this
#                     change keeps working, and the identity service is not
#                     on the critical path of an unrelated post.
#   role supplied  -> the check MUST actually run. A supplied role that
#                     reaches Conclair is therefore a role that passed
#                     validation -- `messages.role IS NOT NULL` implies "an
#                     allowed_roles check succeeded", with no third state
#                     where the value was recorded but unverified.
#
# That second half is why a failed lookup blocks instead of falling through.
# msg-017 §2 diagnosed the shape of failure this design has to avoid: a gate
# that ships, looks armed, and silently never fires. "Prismind was
# unreachable so we let it through" would reintroduce exactly that, and be
# invisible in the data afterwards (an unverified role is byte-identical to a
# verified one). Callers that genuinely cannot reach the identity service can
# still post -- by omitting `role`, which records the absence honestly.
#
# The unregistered-author case (I-3) is resolved the same way, and this is the
# one place where the gate's two halves pull against each other (T-pr-review-11
# msg-026): the post must still go through (msg-002 §2.3 "legacy actors are
# always let through"), but the role it claimed was never checked. Writing that
# claim into `messages.role` would create exactly the third state the invariant
# above rules out -- and it would be *reachable by choosing an unregistered
# author name*, i.e. the check would bind only the cooperative. So the post is
# allowed and the unverified role is dropped: `role=null`, the same honest
# "unverified" marker every pre-gate message carries. Nothing a legacy caller
# could previously do is refused -- a legacy caller cannot supply `role` at
# all, since this change is what introduces the parameter.
#
# Second-order property, load-bearing enough to write down: this makes
# identity-partition drift *visible*. If Prismind is later restarted resolving
# a different `user_name`, every lookup answers found=false, so main-chain
# roles stop being recorded rather than being recorded unverified -- the
# absence shows up directly in the thread (I-6's own falsification condition)
# instead of degrading into silent all-pass.


def _role_not_allowed_error(
    *, author: str, role: str, allowed_roles: list[str]
) -> dict[str, Any]:
    """I-2 rejection: a registered identity used a role it may not assume.

    ``error_type`` is ``RoleNotAllowed`` verbatim per msg-002 §2.3 and
    msg-017 I-2 (both name that exact string as the falsification target).
    Note it lacks the ``Error`` suffix the sibling envelopes in this module
    use; the spec's literal is preserved over local naming symmetry.
    """
    return {
        "error_type": "RoleNotAllowed",
        "error": (
            f"identity {author!r} is not allowed to act as role {role!r} "
            f"(allowed_roles={allowed_roles}). Post under one of the allowed "
            "roles, or update the identity record with upsert_identity."
        ),
        "details": {
            "author": author,
            "role": role,
            "allowed_roles": list(allowed_roles),
        },
    }


def _role_validation_unavailable_error(*, author: str, role: str, reason: str) -> dict[str, Any]:
    """The gate could not run, so the write is refused rather than waved through.

    Distinct ``error_type`` from ``RoleNotAllowed``: this is not a verdict
    about the author, it is the absence of one. Retrying without ``role``
    succeeds and records ``role=null`` -- an honest "unverified" marker --
    which is the intended fallback when the identity service is down.

    That remedy is only true where this envelope is produced, so the close
    path must not produce it: there the second stage refuses the retry it
    invites (msg-041 Q3). ``_check_close_permitted`` answers an unusable
    lookup with ``_close_validation_unavailable_error`` for exactly that
    reason -- if a future change routes a close back through here, this
    docstring becomes false again.
    """
    return {
        "error_type": "RoleValidationUnavailableError",
        "error": (
            f"cannot validate role {role!r} for identity {author!r}: the identity "
            f"lookup failed ({reason}). The role is not recorded unverified; "
            "retry, or post without `role` to record role=null."
        ),
        "details": {"author": author, "role": role, "reason": reason},
    }


def _role_not_allowed_to_close_error(
    *, author: str, allowed_roles: list[str]
) -> dict[str, Any]:
    """I-7 rejection: a registered identity whose roles cannot close a thread.

    Distinct ``error_type`` from ``RoleNotAllowed`` on purpose. That one means
    "you claimed a role you may not assume"; this one means "the roles you may
    assume do not include a closing role". Same author can hit either, and the
    remedy differs, so they must not collapse into one string.

    It is also distinct from the *first* stage of the close check in msg-002
    §3.1 -- the owner check, which lives in Conclair (``assert_owner_can_close``
    -> 403). This envelope is produced in Magickit before Conclair is contacted
    at all, so "which stage rejected me" is answerable from the error alone.
    """
    return {
        "error_type": "RoleNotAllowedToClose",
        "error": (
            f"identity {author!r} may not close threads: none of its "
            f"allowed_roles={allowed_roles} is a closing role "
            f"({list(CLOSEABLE_ROLES)}). Closing is reserved for the roles "
            "that integrate the work; ask one of them to close, or have a "
            "human close it."
        ),
        "details": {
            "author": author,
            "allowed_roles": list(allowed_roles),
            "closeable_roles": list(CLOSEABLE_ROLES),
        },
    }


def _close_validation_unavailable_error(*, author: str, reason: str) -> dict[str, Any]:
    """The close gate could not run, so the close is refused (fail-closed).

    Separate from ``RoleValidationUnavailableError`` because that envelope's
    remedy -- "retry, or post without `role`" -- is wrong here: this stage does
    not read ``role`` at all, so dropping it changes nothing. There is no
    escape hatch by design; a stage that could be bypassed by making the
    identity service unreachable would only bind callers who cannot.

    Matches the close path's existing posture: ``_enforce_close_policies``
    already fails closed when it cannot read the thread ("a gated/forced close
    must prove its preconditions").
    """
    return {
        "error_type": "CloseRoleValidationUnavailableError",
        "error": (
            f"cannot determine whether identity {author!r} may close threads: "
            f"the identity lookup failed ({reason}). The close is refused "
            "rather than allowed unchecked; retry once the identity service is "
            "reachable."
        ),
        "details": {"author": author, "reason": reason},
    }


_IDENTITY_LOOKUP_CONTRACT = (
    '{"success": bool, "found": bool, "identity": '
    '{"allowed_roles": list[str], ...}|None, "message": str}'
)

_REGISTRATION_UNDETERMINED = "whether the identity is registered"
_ROLES_UNDETERMINED = "which roles the identity may claim"


def _malformed_lookup_reason(*, field: str, undetermined: str) -> str:
    """Reason text for a ``200 OK`` that does not satisfy the documented shape.

    A noun phrase, because both unavailable envelopes interpolate it into
    "the identity lookup failed (...)".

    Names the offending field on purpose: from ``error_type`` alone this is
    indistinguishable from an outage -- which is correct, the gate owes the
    same refusal either way -- but the remedies differ (wait for the service
    vs. reconcile a Prismind that no longer speaks the contract), and nothing
    else in the response tells the operator which one they are looking at.

    ``undetermined`` is required rather than fixed because the fields are not
    interchangeable: a broken ``found`` leaves registration unknown, while a
    broken ``allowed_roles`` arrives on a record that *did* confirm
    registration and leaves only the claimable roles unknown. Stating the
    wrong one would send the operator looking at the wrong half of the
    record.
    """
    return (
        "the identity service returned a success response that does not satisfy "
        f"the documented contract {_IDENTITY_LOOKUP_CONTRACT}: {field!r} is "
        f"missing or of the wrong type, so {undetermined} could not be determined"
    )


class _IdentityLookup(NamedTuple):
    """One Prismind identity lookup, shared by both stages of the close gate.

    Extracted so a close performs a single round-trip even though two
    independent questions are asked of the same record ("may you claim this
    role" and "may you close at all").

    - ``unavailable_reason`` set -> the lookup produced no usable verdict.
    - ``found`` False            -> a *confirmed* "no such identity" (legacy).
    - otherwise                  -> ``allowed_roles`` is the record's value.

    ``found=False`` is only ever set from an answer that said so. Both gates
    read it as "unregistered, skip the check" (I-3 / I-9), so a shape that
    merely fails to say "yes" must not land here -- see ``_lookup_identity``.
    """

    unavailable_reason: str | None
    found: bool
    allowed_roles: tuple[str, ...]


_LOOKUP_UNREGISTERED = _IdentityLookup(None, False, ())


def _lookup_unusable(reason: str) -> _IdentityLookup:
    """No usable verdict: neither "registered" nor "not registered".

    ``found=False`` in the returned tuple is filler, never read: every caller
    tests ``unavailable_reason`` first. Constructed through one helper so a
    new failure mode cannot accidentally be spelled as the legacy skip.
    """
    return _IdentityLookup(reason, False, ())


class _RoleDecision(NamedTuple):
    """What the gate decided: block, or the role value that may be recorded.

    Two fields rather than a bare ``error | None`` so that the write paths
    cannot forward the caller's raw ``role``: the only value they are given
    is ``role``, which is non-null exclusively on the verified branch.

    - ``error`` set   -> return it, write nothing.
    - ``role`` str    -> validated against ``allowed_roles``; record it.
    - ``role`` None   -> the write proceeds, recording ``role=null``.
    """

    error: dict[str, Any] | None
    role: str | None


_ALLOW_WITHOUT_ROLE = _RoleDecision(error=None, role=None)


async def _lookup_identity(author: str) -> _IdentityLookup:
    """Fetch one identity record, normalising every failure into one shape.

    Resolution goes through Prismind's ``get_identity`` (single-record,
    cross-project) rather than ``list_context_authors``. The latter is
    project-scoped and enumerates saved session state, so an identity that is
    registered but has never checkpointed in this project is absent from it --
    ``Einstein`` in ``spirrow-magickit`` is exactly that case. Reading the
    gate's input from the project-scoped listing would classify the actor the
    gate exists to stop as "unregistered" and skip the check.

    "Could not answer" (``unavailable_reason``) and "answered: no such record"
    (``found=False``) are kept apart here rather than at each call site,
    because the two gates built on this react to them differently and
    conflating them is what turns a gate into a no-op.

    Which is why a ``200 OK`` is not by itself an answer. ``get_identity`` is
    documented to return ``{"success": bool, "found": bool, "identity":
    dict|None, "message": str}``, so a success response that does not carry
    those fields has not answered the question the gates ask -- and the only
    two things this function may say about a real record are both verdicts.
    Reading a verdict out of a violated contract is what produced the reachable
    fail-open of msg-044 §6.4: ``.get("found", False)`` returned the legacy
    skip for a response that never mentioned ``found``, and ``Einstein``
    (``allowed_roles=["naysayer"]``) passed the close gate it exists to stop.
    Note the direction -- the missing field defaulted to the *permissive*
    branch, so the malformed answer was strictly weaker than an outage.

    So: parse, do not coerce. Anything that does not satisfy the contract is
    the same "no verdict" a dead service gives, and both gates already know
    what to do with that (post: refuse the role, offer role=null; close:
    refuse, fail-closed). The rule is one rule, applied to every field this
    function reads, so neither direction gets a second policy: a malformed
    negative must not skip the check, and a malformed positive must not
    manufacture ``allowed_roles=[]`` and reject in the name of a record that
    was never received.

    ``allowed_roles`` is one of those fields, and it is the one the rule is
    load-bearing on, because it is the value both gates actually compare
    against (msg-048, first-hand reproduced msg-052). The producer emits it
    as ``list[str]`` unconditionally (``IdentityInfo.to_dict``), so anything
    else is a contract violation, and coercing it had three distinct
    outcomes, none of them a verdict: ``tuple(True)`` raised an uncaught
    ``TypeError`` past the ``except`` above; ``tuple("naysayer")`` became
    ``('n', 'a', ...)`` and let an actor claim and *record* the role ``"n"``
    while denying it the role the record actually granted; and a missing key
    became ``()``, the manufactured rejection the paragraph above forbids.
    Element types are checked too -- an element that is not a string can
    never equal a role, so it can only misreport ``allowed_roles`` in the
    error the caller reads.

    An explicitly empty ``[]`` is *not* a violation: Prismind documents it as
    a legal declaration of "no allowed roles", so it stays a verdict and
    still yields ``RoleNotAllowed`` -- which is the discrimination the rule
    is for. ``()`` is now reachable only from a record that said ``[]``.
    """
    prismind = _prismind_adapter()
    try:
        result = await prismind.get_identity(identity_name=author)
    except Exception as e:  # transport failure, unknown tool, timeout, ...
        logger.warning("Identity lookup failed", author=author, error=str(e))
        return _lookup_unusable(str(e))

    if not isinstance(result, dict):
        return _lookup_unusable("unexpected response from Prismind")
    if "error_type" in result:
        return _lookup_unusable(f"{result['error_type']}: {result.get('error', '')}")
    if result.get("success") is not True:
        return _lookup_unusable(
            result.get("message") or "identity lookup did not report success"
        )

    # Below here the response claimed success, so every remaining branch is a
    # statement about the record -- and may only be taken on the field that
    # actually carries it. ``isinstance(..., bool)`` and not truthiness: JSON
    # null / 0 / "" are all falsy, and each would otherwise be read as a
    # confirmed "not registered".
    found = result.get("found")
    if not isinstance(found, bool):
        return _lookup_unusable(
            _malformed_lookup_reason(
                field="found", undetermined=_REGISTRATION_UNDETERMINED
            )
        )
    if not found:
        return _LOOKUP_UNREGISTERED

    identity = result.get("identity")
    if not isinstance(identity, dict):
        return _lookup_unusable(
            _malformed_lookup_reason(
                field="identity", undetermined=_REGISTRATION_UNDETERMINED
            )
        )

    # The value both gates compare against. ``isinstance(..., list)`` and not
    # "is iterable": a bare string is iterable and coerces to its characters,
    # which is the bypass -- not a type error that would announce itself.
    allowed_roles = identity.get("allowed_roles")
    if not isinstance(allowed_roles, list) or not all(
        isinstance(candidate, str) for candidate in allowed_roles
    ):
        return _lookup_unusable(
            _malformed_lookup_reason(
                field="allowed_roles", undetermined=_ROLES_UNDETERMINED
            )
        )
    return _IdentityLookup(None, True, tuple(allowed_roles))


async def _check_role_allowed(
    *, author: str, role: str, lookup: _IdentityLookup | None = None
) -> _RoleDecision:
    """Run the role × allowed_roles gate and decide what may be recorded.

    Outcomes:
    - ``role`` empty -> caller opted out (I-3); no lookup, record null.
    - identity confirmed not registered -> legacy actor: the write is allowed
      (I-3) but the unverified role is **not** recorded (see the section
      comment above). Confirmed, not merely unproven: ``_lookup_identity``
      routes an answer it cannot read to ``unavailable_reason`` instead.
    - role present in ``allowed_roles`` -> record the role.
    - role absent from ``allowed_roles`` -> ``RoleNotAllowed``, write nothing.
    - lookup unusable -> ``RoleValidationUnavailableError``, write nothing.

    ``lookup`` lets a caller that has already fetched the record reuse it (the
    close path asks two questions of one record). When omitted the record is
    fetched here -- and only if ``role`` was supplied, which is what keeps an
    ordinary post off the identity service's critical path (I-3).
    """
    if not role:
        return _ALLOW_WITHOUT_ROLE

    if lookup is None:
        lookup = await _lookup_identity(author)

    if lookup.unavailable_reason is not None:
        return _RoleDecision(
            _role_validation_unavailable_error(
                author=author, role=role, reason=lookup.unavailable_reason
            ),
            None,
        )

    if not lookup.found:
        # Legacy / unregistered author: the post is allowed (I-3), but the
        # role was never validated, so it is dropped rather than persisted as
        # if it had been. WARNING, not INFO: the caller asked for something
        # that did not happen, and if this fires for a main-chain identity it
        # is the signature of identity-partition drift, not of legacy traffic.
        logger.warning(
            "Role not recorded: identity not registered",
            author=author,
            requested_role=role,
            recorded_role=None,
        )
        return _ALLOW_WITHOUT_ROLE

    if role not in lookup.allowed_roles:
        return _RoleDecision(
            _role_not_allowed_error(
                author=author, role=role, allowed_roles=list(lookup.allowed_roles)
            ),
            None,
        )
    return _RoleDecision(None, role)


def _check_can_close(*, author: str, lookup: _IdentityLookup) -> dict[str, Any] | None:
    """Second stage (I-7): may this identity close a thread at all?

    Pure given the record. Returns an error envelope to return to the caller,
    or None to proceed.

    - unusable lookup -> refuse (fail-closed, see
      ``_close_validation_unavailable_error``).
    - confirmed unregistered -> skip (I-9). msg-002 §3.2: legacy actors keep
      working. This is the branch a malformed success used to reach, which is
      what made it a bypass rather than a nuisance (msg-044 §6.4); it is now
      reachable only from a response that said ``found=false``.
      Load-bearing, but not for the reason first written here: unregistered
      identities do close threads (``claude-code`` has no identity record --
      verified live 2026-08-02 -- and closed ``T-T183-plan-scope`` / msg-037 in
      spirrow-voxelworld), so binding them changes traffic that exists. The
      earlier justification named the naysayer driver instead, and that was
      false (msg-041 Q4): spirrow-mindwire@4ed9eb4 has no call site for
      ``chatroom_close_thread`` / ``closes_thread`` at all, and the
      ``orchestrator``-owned PR-review threads it opens are closed by ``human``
      under the Tier-C owner-override (e.g. T-pr-review-24 / msg-202).
      **Updated (T-pr-gate-ledger-debt):** those PR-review threads are now also
      closable by a ``closeable_roles`` identity when GitHub proves the PR
      merged with an APPROVED review at the merged head. That makes this stage
      load-bearing for a second population — it is the gate the ledger carve-out
      is layered on top of, not a stage the carve-out skips.
    - allowed_roles ∩ CLOSEABLE_ROLES = ∅ -> ``RoleNotAllowedToClose``.

    The human never reaches here: ``_check_close_permitted`` exempts it (I-8)
    before the lookup, so the exemption is written once, at the layer that can
    also keep the close off the identity service's critical path.

    Note what this deliberately does not read: the ``role`` the caller claimed
    on *this* call. The decided form (msg-002 §3.1) binds the identity's
    standing capability, so a naysayer-only identity cannot close by simply
    omitting ``role``. D-14 asked whether to re-base it on the claimed role;
    the independent review (msg-041 Q1) endorsed keeping the standing form --
    stage 1 already proves claimed ⊆ standing, which makes a claim-based
    capability check equivalent but bypassable by omitting ``role``. The
    recorded decide is Tier-C's (msg-032 §2); this is the baseline it acts on.
    """
    if lookup.unavailable_reason is not None:
        return _close_validation_unavailable_error(
            author=author, reason=lookup.unavailable_reason
        )
    if not lookup.found:
        return None
    if set(lookup.allowed_roles) & set(CLOSEABLE_ROLES):
        return None
    return _role_not_allowed_to_close_error(
        author=author, allowed_roles=list(lookup.allowed_roles)
    )


async def _check_close_permitted(*, author: str, role: str) -> _RoleDecision:
    """Both role stages for a close, on a single identity lookup.

    Stage order is claim-then-capability: a role the identity may not assume is
    ``RoleNotAllowed`` regardless of whether it could have closed, so that the
    error names the thing the caller got wrong first.

    Both stages complete before Conclair is contacted, which is what makes them
    distinguishable from the *owner* check -- that one lives in Conclair
    (``assert_owner_can_close``) and is only reachable after these pass.

    Two things are decided here rather than inside either stage, because both
    are properties of *closing* and neither stage can see the other:

    - An unusable lookup is terminal. Stage 1 would answer it with "retry, or
      post without ``role``", which stage 2 then refuses -- a remedy the code
      knows in advance will fail (msg-041 Q3). The close answers with the
      stage-2 envelope whether or not ``role`` was supplied.
    - The human is exempt (I-8) and its close never depends on Prismind. Not
      only when ``role`` is omitted: a claim that cannot be validated during an
      outage degrades to the value the system already means by "unverified"
      (null), so the above-loop Tier-C force-close of ADR-2026-06-04-19 D-5
      cannot be blocked by a downstream service over an optional argument
      (msg-041 Q6). A claim the record *denies* is still a verdict, not an
      outage, so it stays rejected.
    """
    if author in HUMAN_IDENTITY_NAMES:
        # I-8. See the note on CLOSEABLE_ROLES: the human record is
        # allowed_roles=["human"], which intersects the closing roles nowhere,
        # so without the exemption the decided form would lock the human out of
        # closing anything. The human is the above-loop approval layer; this
        # gate is about roles inside the loop.
        if not role:
            return _ALLOW_WITHOUT_ROLE  # nothing to validate, nothing to ask
        human_lookup = await _lookup_identity(author)
        if human_lookup.unavailable_reason is not None:
            logger.warning(
                "Role not recorded: identity lookup unavailable on a human close",
                author=author,
                requested_role=role,
                recorded_role=None,
                reason=human_lookup.unavailable_reason,
            )
            return _ALLOW_WITHOUT_ROLE
        return await _check_role_allowed(
            author=author, role=role, lookup=human_lookup
        )

    lookup = await _lookup_identity(author)

    # Stage 2 is computed up front for the one outcome stage 1 must not answer.
    # It is pure given the record, so this costs nothing and keeps the
    # fail-closed posture in one place rather than duplicating the condition.
    close_error = _check_can_close(author=author, lookup=lookup)
    if lookup.unavailable_reason is not None:
        return _RoleDecision(close_error, None)

    # Verdict ordering is unchanged: claim-then-capability, so a role the
    # identity may not assume is reported as such even when it also could not
    # have closed.
    decision = await _check_role_allowed(author=author, role=role, lookup=lookup)
    if decision.error is not None:
        return decision
    if close_error is not None:
        return _RoleDecision(close_error, None)
    return decision


def _embodiment_required_error(*, msg_kind: str) -> dict[str, Any]:
    """Magickit-side embodiment-required rejection envelope.

    Shape matches the project ``error_type`` convention (msg-002 §1.4 /
    msg-010 D-9) so callers can branch on the error class without
    parsing the human-readable message.
    """
    return {
        "error_type": "EmbodimentRequiredError",
        "error": (
            f"embodiment is required for {msg_kind} "
            "(ADR-2026-05-29-12 mandatory-on-state-transition; "
            "humans are exempt)"
        ),
        "details": {
            "msg_kind": msg_kind,
            "mandatory_msg_types": list(MANDATORY_EMBODIMENT_MSG_TYPES),
            "exempt_identity_names": list(HUMAN_IDENTITY_NAMES),
        },
    }


# --- next_participant gate (T-handoff-target-structured-field) ---------
#
# msg-068 §4-1 and the review chain that shaped it (msg-069 / msg-070 /
# msg-071 / msg-072 / msg-078). The structured handoff target: a supplied
# value must resolve to a registered identity, an unresolved one is refused
# before any write, and an omitted one is forwarded as null (backwards
# compatibility).
#
# Placement is by explicit decision (msg-072 §1). Identity resolution is
# Magickit's subject -- Prismind holds the registry, Conclair only persists
# a nullable text column -- so validation belongs here. The consumer's
# routing vocabulary (spirrow-mindwire's `NEXT: <token>` line, its
# `pr-review <ref>` sentinel) is deliberately NOT known to this layer: the
# module contains no reference to that syntax and no reserved word (msg-072
# §2 -- "the vocabulary is not shared between owners"). ``human`` is a
# registered identity like any other and needs no special case here; a
# terminal "no next actor" state is represented by closing the thread, not
# by a token this gate has to recognise (msg-072 §2 / msg-078 §3).
#
# Policy differs from the role gate on the one axis msg-072 §3 spells out:
# an unregistered target is REJECTED, not passed through. The role gate
# tolerates unregistered *authors* because refusing them would break legacy
# traffic; there is no legacy caller here (the field is new), and a
# tolerant rule would let a typo (`Einstien`) leak into the routing state
# the whole feature exists to make trustworthy. The lookup-unavailable
# path stays fail-closed but honestly: the caller can retry after omitting
# the field, which is not the "certain-to-fail retry" that ``msg-041 Q3``
# ruled out for the close gate -- omission is a real escape hatch here
# because it degrades to the previous behaviour (routing falls back to the
# consumer's body-line parser).
#
# The lookup itself is reused verbatim: ``_lookup_identity`` carries a
# hard-won contract check (msg-044 §6.4 -- a truthy read of a missing
# ``found`` field turned the close gate into a bypass), and a second
# implementation would drift from it at exactly the field-level detail the
# gate turns on (msg-072 §1 "not `equivalent` -- the function itself").

_NEXT_PARTICIPANT_CONTRACT_URL = "T-handoff-target-structured-field msg-068 / msg-072"


def _next_participant_unknown_error(*, name: str) -> dict[str, Any]:
    """A supplied handoff target does not resolve to a registered identity.

    Fail-fast rejection per msg-072 §3. The remedy is either to name a
    registered identity or to register the intended actor first with
    ``upsert_identity`` -- the latter is what closes the "``claude-code`` has
    no record" gap msg-072 §3 called out as a precondition of this policy.

    Distinct ``error_type`` from every other envelope in this module so a
    caller can branch on it without parsing the human message. Includes an
    ``Error`` suffix (unlike ``RoleNotAllowed``, which preserves the spec's
    literal string): there is no msg-002-era spec literal to preserve here,
    so the module's default naming applies.
    """
    return {
        "error_type": "NextParticipantUnknownError",
        "error": (
            f"next_participant {name!r} is not a registered identity. "
            "Name a registered actor, or register the intended one with "
            "upsert_identity before handing off. Omit the field to fall "
            "back on the consumer's body-line handoff parser."
        ),
        "details": {"next_participant": name},
    }


def _next_participant_unavailable_error(*, name: str, reason: str) -> dict[str, Any]:
    """The registry lookup for the target could not run; refuse the write.

    Fail-closed (msg-072 §3): a permissive fallback here would let a
    Prismind outage silently disarm the very gate that exists to prevent a
    typo from leaking downstream. Unlike the close gate's equivalent
    envelope, the retry-with-the-field-omitted remedy is genuinely
    available (msg-070 §3 escape hatch): omission lands the message with
    the field null, and routing degrades to the consumer's body-line
    parser exactly as before this feature shipped.
    """
    return {
        "error_type": "NextParticipantValidationUnavailableError",
        "error": (
            f"cannot validate next_participant {name!r}: the identity "
            f"lookup failed ({reason}). The target is not recorded "
            "unvalidated; retry, or omit next_participant to fall back on "
            "the consumer's body-line handoff parser."
        ),
        "details": {"next_participant": name, "reason": reason},
    }


async def _check_next_participant(name: str) -> dict[str, Any] | None:
    """Resolve a supplied handoff target against the identity registry.

    Returns None to signal "proceed with the write"; returns an error
    envelope to signal "refuse before the write". An empty ``name`` is
    caller opt-out and returns None without touching Prismind, which is
    what keeps ordinary posts off the identity service's critical path
    (the same shape as the role gate's opt-out branch).

    Reuses ``_lookup_identity`` -- see the section comment above for why the
    lookup is not reimplemented. Only the ``found`` / ``unavailable_reason``
    verdicts are read here; ``allowed_roles`` is deliberately ignored
    (msg-072 §3 "validate existence only", so the target is not required to
    carry any particular role capability).
    """
    if not name:
        return None
    lookup = await _lookup_identity(name)
    if lookup.unavailable_reason is not None:
        return _next_participant_unavailable_error(
            name=name, reason=lookup.unavailable_reason
        )
    if not lookup.found:
        return _next_participant_unknown_error(name=name)
    return None


def configure(settings: Settings) -> None:
    """Bind the settings every gate helper in this module reads.

    ``register_tools`` is the MCP process's entry point and calls this, but
    the gates are not MCP-specific: the FastAPI process that serves the human
    chatroom UI enforces the same ones on browser writes and never registers
    an MCP tool. Both processes must call this before any gate runs --
    ``_adapter()`` / ``_prismind_adapter()`` raise without it, and
    ``_enforce_close_policies`` reads ``naysayer_gate_enabled`` off it.
    """
    global _settings
    _settings = settings


def register_tools(mcp: FastMCP, settings: Settings) -> None:
    """Register chatroom MCP tools."""
    configure(settings)

    @mcp.tool()
    async def chatroom_open_thread(
        project: str,
        thread_id: str,
        title: str,
        owner: str,
        propose_content: str,
        tags: list[str] | None = None,
        commit_ref: str = "",
        embodiment: str = "",
        role: str = "",
        next_participant: str = "",
    ) -> dict[str, Any]:
        """Open a new chatroom thread (creates the thread + propose msg).

        USE THIS WHEN: you want to start a new discussion / handoff /
        decision thread between AI sessions.

        Args:
            project: project identifier (e.g. "spirrow-voxelworld").
            thread_id: thread id, must be unique within the project
                (convention: "T-" prefix + descriptive slug, e.g.
                "T-D4-radius").
            title: 1-line thread title.
            owner: author string (e.g. "claude.ai" / "claude-code" /
                "human"). The owner is the only one allowed to close.
            propose_content: markdown body of the propose message.
            tags: optional thread-level tags.
            commit_ref: optional git hash to record on the propose msg.
            embodiment: ADR-2026-05-29-12 self-declared runtime form
                (web_ai_chat / terminal_coding_agent / unknown). Optional
                for ``propose`` (Einstein N-3 / msg-325 §4: propose is
                covered by the receiver here but not in the mandatory
                set). Recorded on the propose msg if supplied.
            role: role ``owner`` is acting under for the propose msg (e.g.
                "proposer"). Optional. When supplied it is validated
                against the identity's ``allowed_roles`` and recorded on
                the msg; when omitted nothing is checked or recorded. An
                unregistered ``owner`` still opens the thread, but the
                unvalidated role is recorded as null rather than as given.
                Validated against ``owner``, who authors the propose msg.
            next_participant: structured handoff target for the propose msg
                (T-handoff-target-structured-field). Optional. When
                supplied it must resolve to an identity Prismind knows;
                otherwise the write is refused with
                ``NextParticipantUnknownError`` (fail-fast) before the
                thread is created. Only existence is checked -- roles are
                not required of the target. Omit to fall back on the
                consumer's body-line handoff parser.

        Returns:
            On success: {"thread": {...}, "msg": {...}}.
            On failure: conclair error envelope
            {"error_type": "ChatroomIntegrityError", "error": "...",
             "details": {...}}, "RoleNotAllowed" /
            "RoleValidationUnavailableError" from the role gate, or
            "NextParticipantUnknownError" /
            "NextParticipantValidationUnavailableError" from the
            next_participant gate.
        """
        gate = await _check_role_allowed(author=owner, role=role)
        if gate.error is not None:
            return gate.error

        # Structured handoff target check. Runs after the role gate on
        # purpose: both stages fail-fast before any write, so ordering is
        # semantic rather than performance. The role gate is about ``owner``
        # (who authors the propose msg); this one is about the *target* of
        # the handoff, a different identity. Both can round-trip to Prismind
        # when supplied; there is no shared lookup to reuse.
        next_error = await _check_next_participant(next_participant)
        if next_error is not None:
            return next_error

        adapter = _adapter()
        try:
            return await adapter.open_thread(
                project=project,
                thread_id=thread_id,
                title=title,
                owner=owner,
                propose_content=propose_content,
                tags=tags,
                commit_ref=commit_ref or None,
                embodiment=embodiment or None,
                role=gate.role,
                next_participant=next_participant or None,
            )
        finally:
            await adapter.close()

    @mcp.tool()
    async def chatroom_post_message(
        project: str,
        thread_id: str,
        msg_type: Literal[
            "propose", "question", "answer", "decide", "report", "handoff", "ack"
        ],
        author: str,
        content: str,
        reply_to: str = "",
        references_threads: list[str] | None = None,
        related_tasks: list[str] | None = None,
        closes_thread: str = "",
        tags: list[str] | None = None,
        commit_ref: str = "",
        embodiment: str = "",
        role: str = "",
        naysayer_override_reason: str = "",
        owner_override_reason: str = "",
        next_participant: str = "",
    ) -> dict[str, Any]:
        """Post a message to an existing thread.

        USE THIS WHEN: replying inside a thread, asking a question,
        reporting progress, or handing off / acknowledging a handoff.
        For closing a thread (decide + closes_thread), prefer
        chatroom_close_thread which also enforces the owner check.

        msg_type semantics (per chatroom spec):
        - propose: rejected here (only the first msg of a thread can be
          propose; that one is created by chatroom_open_thread)
        - question / answer: normal Q&A
        - report: progress / outcome notes
        - handoff: thread.status -> awaiting_reply
        - ack: thread.status (awaiting_reply) -> active
        - decide: declarative decision; with closes_thread set, must be
          authored by the owner and resolves the thread

        embodiment (ADR-2026-05-29-12 self-declared runtime form):
        - mandatory for msg_type in {handoff, ack, decide} (state-
          transitioning posts per msg-325 §4 N-2)
        - exempt when ``author`` is the human identity (msg-325 §3:
          humans don't operate Magickit as a calling agent)
        - optional for question / answer / report (recorded if supplied)

        role (ADR-2026-05-27-09 / msg-002 §2.3): the role ``author`` is
        acting under for THIS message (e.g. "proposer" / "implementer" /
        "naysayer"). Optional, but not merely decorative:
        - omitted -> nothing is validated and ``role`` is recorded as null.
        - supplied -> checked against the identity's ``allowed_roles``
          before anything is written. A role the identity may not assume is
          rejected with ``RoleNotAllowed`` and no message is created.
        - supplied by an author with no registered identity -> the message is
          posted (legacy actors are never refused) but the unverified role is
          dropped, i.e. recorded as null.
        A recorded role therefore always means "this was verified".

        NOTE: this parameter is named `msg_type` (not `type`) because
        some MCP clients reject schemas that use `type` as a property
        name — they collide with JSON Schema's own `type` keyword.

        Naysayer gate + owner-override + closeable_roles: a ``decide`` with
        ``closes_thread`` set resolves the thread, so it carries the same
        policies as ``chatroom_close_thread`` — a gated thread requires a fresh
        approving naysayer review unless ``naysayer_override_reason`` (human
        only) is supplied, a human may force-close a non-owned thread (set
        ``owner_override_reason``; required when non-gated, see
        chatroom_close_thread), and the author's identity must be able to act
        as a closing role (``RoleNotAllowedToClose`` otherwise). One caller-
        visible difference from an ordinary post: while the identity service is
        unreachable a closing ``decide`` is refused with
        ``CloseRoleValidationUnavailableError`` whether or not ``role`` was
        supplied -- dropping ``role`` is a remedy on a post, not on a close.

        next_participant (T-handoff-target-structured-field): the identity
        expected to take the next turn on this thread. Optional. When
        supplied it must resolve to an identity Prismind knows; a name that
        does not is refused with ``NextParticipantUnknownError`` and no
        message is created. A registry outage refuses with
        ``NextParticipantValidationUnavailableError``; the remedy is to
        retry, or to omit the field and let the consumer fall back on its
        body-line handoff parser. Only existence is checked -- roles are
        not required of the target.

        Returns:
            On success: {"msg": {...}, "thread_status_changed_to":
            null|"awaiting_reply"|"active"|"resolved"}.
            On failure (embodiment missing, role not allowed, not allowed to
            close, naysayer gate, next_participant unknown / unavailable,
            conclair error, ...): error_type envelope.
        """
        # Magickit-side enforcement (F-04: Magickit is the sole role/
        # embodiment validation point; Conclair only persists).
        # Ordered cheapest-first: the embodiment rule is a pure parameter
        # check, the role gate costs one Prismind round-trip, and the close
        # policies cost a Conclair read. All three precede any write.
        if (
            msg_type in MANDATORY_EMBODIMENT_MSG_TYPES
            and author not in HUMAN_IDENTITY_NAMES
            and not embodiment
        ):
            return _embodiment_required_error(msg_kind=f"msg_type={msg_type}")

        # A decide that closes the thread IS a close, so it takes the close
        # gate (both role stages), not just the per-message one. Anything less
        # would leave `closes_thread` as the documented way around the second
        # stage -- the same reasoning that already routes this path through
        # `_enforce_close_policies` below.
        closes = msg_type == "decide" and bool(closes_thread)
        gate = await (
            _check_close_permitted(author=author, role=role)
            if closes
            else _check_role_allowed(author=author, role=role)
        )
        if gate.error is not None:
            return gate.error

        # Structured handoff target check. Runs after the role gate so a
        # rejected role is still reported as such (both are fail-fast
        # pre-write; there is no traffic in which one masks the other).
        next_error = await _check_next_participant(next_participant)
        if next_error is not None:
            return next_error

        adapter = _adapter()
        try:
            # A decide that closes the thread is a close path; apply the same
            # naysayer gate + owner-override policies as chatroom_close_thread
            # so it can't be a bypass.
            owner_override = False
            owner_override_reason_out: str | None = None
            if msg_type == "decide" and closes_thread:
                policy = await _enforce_close_policies(
                    adapter,
                    project=project,
                    thread_id=thread_id,
                    author=author,
                    body_content=content,
                    naysayer_override_reason=naysayer_override_reason,
                    owner_override_reason=owner_override_reason,
                )
                if policy["action"] == "block":
                    return policy["envelope"]
                content = policy["content"]
                owner_override = policy["owner_override"]
                owner_override_reason_out = policy["owner_override_reason"]

            return await adapter.post_message(
                project=project,
                thread_id=thread_id,
                type=msg_type,
                author=author,
                content=content,
                reply_to=reply_to or None,
                references_threads=references_threads,
                related_tasks=related_tasks,
                closes_thread=closes_thread or None,
                tags=tags,
                commit_ref=commit_ref or None,
                embodiment=embodiment or None,
                role=gate.role,
                owner_override=owner_override,
                owner_override_reason=owner_override_reason_out,
                next_participant=next_participant or None,
            )
        finally:
            await adapter.close()

    @mcp.tool()
    async def chatroom_close_thread(
        project: str,
        thread_id: str,
        summary_content: str,
        author: str,
        affects_threads: list[str] | None = None,
        related_tasks: list[str] | None = None,
        tags: list[str] | None = None,
        commit_ref: str = "",
        embodiment: str = "",
        role: str = "",
        naysayer_override_reason: str = "",
        owner_override_reason: str = "",
    ) -> dict[str, Any]:
        """Close an active thread by posting a decide msg (owner-only).

        USE THIS WHEN: the thread reaches a conclusion and the owner
        wants to record a summary post. Only the original owner may
        call this; non-owner attempts return ChatroomPermissionError.

        Two bypasses of that ownership rule exist, both decided here rather
        than in Conclair: a human's Tier-C force-close
        (``owner_override_reason`` below), and the PR-gate ledger carve-out —
        a driver-opened PR-review thread whose PR GitHub reports as merged
        **with an APPROVED review at the merged head** may be filed by any
        ``closeable_roles`` identity (T-pr-gate-ledger-debt msg-1001 §2; see
        :mod:`magickit.mcp.pr_gate_ledger`). A PR-review thread that fails
        that proof returns ``PrGateLedgerNotClosableError`` and stays open
        for a human — which is the point of it.

        Who may close (two independent stages, both before the owner check):
        1. ``role`` × ``allowed_roles`` -- as on every write path.
        2. ``closeable_roles`` -- the author's identity must be able to act
           as one of {implementer, integrator, proposer}; otherwise
           ``RoleNotAllowedToClose``. This binds the identity's standing
           ``allowed_roles``, not the ``role`` claimed on this call, so it
           cannot be sidestepped by omitting ``role``. Unregistered authors
           are exempt (legacy compatibility) and so is the human identity.
           It is a misconfiguration guard rather than an authorization
           boundary: ``author`` is not authenticated here (see the note on
           ``CLOSEABLE_ROLES``).
        When the identity service cannot be reached, a close is refused with
        ``CloseRoleValidationUnavailableError`` regardless of ``role`` -- stage
        2 does not read ``role``, so there is no "retry without it" remedy on
        this path. The human is unaffected: its close does not consult the
        identity service, and a role it cannot validate is recorded as null.

        embodiment (ADR-2026-05-29-12 self-declared runtime form):
        - mandatory because close emits a ``decide`` msg internally
          (msg-325 §4 mandatory set)
        - exempt when ``author`` is the human identity

        Naysayer gate: if the thread carries the configured gate tag
        (default ``gate:naysayer``), the close is blocked unless a fresh
        independent-naysayer review approves it. Pass
        ``naysayer_override_reason`` (human identity only) to override a
        missing / changes-requested review; the reason is recorded in the
        decide msg. Non-gated threads are unaffected.

        Args:
            summary_content: markdown body of the decide msg. Should
                contain a clear conclusion + decision points so the
                summary stands on its own.
            affects_threads: optional list of thread_ids this decision
                impacts; recorded on the thread row.
            embodiment: see above. Mandatory for non-human authors.
            role: role ``author`` is acting under. Optional; when supplied
                it is validated against the identity's ``allowed_roles``
                and recorded on the emitted decide msg. Close emits a
                decide, so it carries the same role gate as post_message
                (msg-017 I-4) -- an out-of-allowed_roles close is rejected
                with ``RoleNotAllowed``, and an unregistered author's
                unvalidated role is recorded as null rather than as given.
            naysayer_override_reason: human-only override of the naysayer
                gate. Non-empty engages the override (reason mandatory);
                ignored on non-gated threads. A non-human author supplying
                it is rejected with NaysayerOverrideForbiddenError.
            owner_override_reason: reason for a human Tier-C force-close of a
                NON-owned thread (ADR-2026-06-04-19 D-5). Required when a
                human closes a thread they do not own and it is NOT gated
                (gated force-close reuses naysayer_override_reason). Recorded
                in the decide msg + Conclair audit event. Has no effect for
                non-human authors — their only route past ownership is the
                PR-gate ledger carve-out, which supplies its own machine-
                derived reason and takes no caller-provided one.

        Returns:
            On success: {"thread": {... status=resolved ...},
                         "decide_msg": {...}}.
            On failure (embodiment missing -> EmbodimentRequiredError,
            role gate -> RoleNotAllowed,
            closeable_roles gate -> RoleNotAllowedToClose /
            CloseRoleValidationUnavailableError (this path never returns
            RoleValidationUnavailableError -- an unusable lookup is answered
            by the close-specific envelope),
            naysayer gate -> NaysayerReviewRequiredError /
            NaysayerReviewStaleError / NaysayerChangesRequestedError /
            NaysayerOverrideForbiddenError, owner-override reason missing ->
            OwnerOverrideReasonRequiredError, PR-review thread without a
            provable APPROVE at its merged head ->
            PrGateLedgerNotClosableError, non-owner (non-human) -> 403,
            already resolved -> 409, etc.): error_type envelope.
        """
        # close_thread emits a decide msg internally; same mandatory
        # rule as msg_type="decide" on post_message.
        if author not in HUMAN_IDENTITY_NAMES and not embodiment:
            return _embodiment_required_error(msg_kind="close_thread (emits decide)")

        # I-4: the emitted decide is a message like any other, so the role
        # gate applies here too -- otherwise close would be a bypass. I-7 adds
        # the second stage (closeable_roles) on the same lookup. Both run
        # before any Conclair call, so a rejection here is unambiguously not
        # the owner check.
        gate = await _check_close_permitted(author=author, role=role)
        if gate.error is not None:
            return gate.error

        adapter = _adapter()
        try:
            policy = await _enforce_close_policies(
                adapter,
                project=project,
                thread_id=thread_id,
                author=author,
                body_content=summary_content,
                naysayer_override_reason=naysayer_override_reason,
                owner_override_reason=owner_override_reason,
            )
            if policy["action"] == "block":
                return policy["envelope"]

            return await adapter.close_thread(
                project=project,
                thread_id=thread_id,
                summary_content=policy["content"],
                author=author,
                affects_threads=affects_threads,
                related_tasks=related_tasks,
                tags=tags,
                commit_ref=commit_ref or None,
                embodiment=embodiment or None,
                role=gate.role,
                owner_override=policy["owner_override"],
                owner_override_reason=policy["owner_override_reason"],
            )
        finally:
            await adapter.close()

    @mcp.tool()
    async def chatroom_list_threads(
        project: str,
        status_filter: list[str] | None = None,
        owner: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List threads in a project with optional filters.

        USE THIS WHEN: starting a session and you want the list of
        active / awaiting_reply threads owned by you, or surveying the
        project's open work.

        Sorted by **last activity** — the thread holding the newest msg
        comes first — not by when the thread was opened: an old thread
        posted to today is above a younger silent one. The rank is the
        server-allocated msg sequence, so `last_activity_at` (a
        caller-supplied timestamp) is a display value, not the order.

        Args:
            status_filter: optional list of statuses to include
                (active / awaiting_reply / resolved / superseded /
                parked). Empty = all.
            owner: optional owner filter (exact match).
            limit: 1..1000, default 100.
            offset: pagination, default 0.

        Returns:
            {"items": [Thread...], "total": int, "limit": int,
             "offset": int}, where each Thread is:
            {project, thread_id, title, owner, status, created_at,
             created_by_msg, resolved_by_msg, affects_threads, tags,
             last_msg_id, msg_count, last_activity_at}.

            **`created_by_msg` is the FIRST msg (the propose), not the
            latest.** It never moves, and it is not a measure of how
            much is in the thread -- read `msg_count` /
            `last_msg_id` / `last_activity_at` for that. A listing row
            showing `created_by_msg: "msg-867"` says nothing about
            whether the thread has 1 msg or 40; assuming the former is
            how a live thread got taken for a leftover on 2026-08-15.

            `last_msg_id` is the same value the inbox reports as
            `latest_msg_id`.
        """
        adapter = _adapter()
        try:
            return await adapter.list_threads(
                project=project,
                status_filter=status_filter,
                owner=owner or None,
                limit=limit,
                offset=offset,
            )
        finally:
            await adapter.close()

    @mcp.tool()
    async def chatroom_get_thread(
        project: str,
        thread_id: str,
        mode: Literal["full", "summary"] = "full",
    ) -> dict[str, Any]:
        """Fetch a thread plus its messages.

        USE THIS WHEN: you need to read the conversation. For resolved
        threads use mode="summary" to load only the decide msg
        (saves tokens).

        Args:
            mode: "full" (default) returns every msg in numeric msg_id
                order. "summary" on a resolved thread returns only the
                decide msg; on active / awaiting_reply / superseded /
                parked it behaves the same as "full".

        Returns:
            {"thread": Thread, "messages": [Message...], "mode":
             "full"|"summary"}.

            `thread` carries the same activity rollup as the listing
            (`msg_count` / `last_msg_id` / `last_activity_at`). In
            `summary` mode `messages` is filtered to the decide msg
            while `msg_count` still counts the whole thread -- so the
            two disagreeing is expected, not a bug.
        """
        adapter = _adapter()
        try:
            return await adapter.get_thread(
                project=project, thread_id=thread_id, mode=mode
            )
        finally:
            await adapter.close()

    @mcp.tool()
    async def chatroom_list_events(
        project: str,
        thread_id: str = "",
        action: Literal[
            "", "open_thread", "post_message", "status_transition", "mark_read",
        ] = "",
        since: str = "",
        until: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List audit log events (thread / message activity).

        USE THIS WHEN: tracing what changed in the chatroom over a time
        window, or auditing a specific thread's history.

        Args:
            thread_id: optional filter to a single thread.
            action: optional filter — "open_thread" / "post_message" /
                "status_transition" / "mark_read". Empty = all actions.
            since: ISO 8601 inclusive lower bound (e.g.
                "2026-05-01T00:00:00Z"). Empty = no lower bound.
            until: ISO 8601 exclusive upper bound. Empty = no upper bound.
            limit: 1..1000, default 100.

        Returns:
            {"items": [Event...], "total": int, "limit": int,
             "offset": int}, ordered (timestamp DESC, id DESC).
        """
        adapter = _adapter()
        try:
            return await adapter.list_events(
                project=project,
                thread_id=thread_id or None,
                action=action or None,
                since=since or None,
                until=until or None,
                limit=limit,
                offset=offset,
            )
        finally:
            await adapter.close()

    @mcp.tool()
    async def chatroom_check_integrity(project: str) -> dict[str, Any]:
        """Audit chatroom invariants for a project.

        USE THIS WHEN: troubleshooting suspected data corruption, or
        running a periodic health audit.

        Returns:
            {"issues": [IntegrityIssue...], "issue_count": int,
             "checked_at": ISO 8601 timestamp}. Always 200 even when
             issues exist (this is a report endpoint, not enforcement).
        """
        adapter = _adapter()
        try:
            return await adapter.check_integrity(project=project)
        finally:
            await adapter.close()

    @mcp.tool()
    async def chatroom_mark_read(
        project: str,
        thread_id: str,
        identity_name: str,
        up_to_msg_id: str = "",
    ) -> dict[str, Any]:
        """Advance my read cursor on a thread (the only way to advance it).

        USE THIS WHEN: I've finished reading a thread (or up to a point in
        it) and want to record that, so a later `chatroom_my_unread` call
        doesn't surface it again.

        Cursor model (see CLAUDE.md "Read cursor" section):
        - per (project, identity_name, thread_id), records
          `last_read_msg_id`.
        - **`chatroom_get_thread` and `chatroom_list_threads` do NOT advance
          the cursor** -- only this tool does, to prevent "I just opened
          the thread to peek" from silently being recorded as "I've read
          everything in it".
        - monotonic forward only: a request older than the current cursor
          is a silent no-op (`advanced=false`); the response always
          reflects the *current* cursor state.

        Args:
            project: chatroom project (e.g. "spirrow-magickit").
            thread_id: target thread.
            identity_name: whose cursor (e.g. "Heisenberg"). The cursor
                is per-identity; another identity's cursor is unaffected.
            up_to_msg_id: optional. Empty (default) = catch up to the
                thread's current latest msg. Pass a specific msg_id to
                bookmark "I've read up to here".

        Returns:
            {"project", "identity_name", "thread_id", "last_read_msg_id",
             "updated_at", "advanced"}. `advanced=true` means the cursor
            moved and a `mark_read` audit event was emitted; `false` is
            the same-position-or-rewind no-op.
            On failure (thread not found / msg_id not in thread):
            conclair error envelope.
        """
        adapter = _adapter()
        try:
            return await adapter.mark_read(
                project=project,
                thread_id=thread_id,
                identity_name=identity_name,
                # Empty string at the MCP surface -> None on the wire so
                # the server interprets "catch up to latest". Matches the
                # pattern already used in this file for `commit_ref` etc.
                up_to_msg_id=up_to_msg_id or None,
            )
        finally:
            await adapter.close()

    @mcp.tool()
    async def chatroom_my_unread(
        project: str,
        identity_name: str,
        include_resolved: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Inbox: threads with at least one msg I have not read.

        USE THIS WHEN: starting a session, to triage what needs
        attention before doing anything else. Pair with
        `chatroom_get_thread` to read the new msgs, then
        `chatroom_mark_read` to advance the cursor once handled.

        Inbox semantics:
        - a thread the identity has never `mark_read`'d shows up with
          `last_read_msg_id=null` and `unread_count` = the whole thread
          size (handoff-safety default). Catch up with
          `chatroom_mark_read(thread_id=..., up_to_msg_id="")`.
        - results are sorted most unread first, then by the newest msg
          in the thread (not by when the thread was created), so the
          first page is the actionable surface.
        - resolved threads are excluded by default. Pass
          `include_resolved=True` to see them (e.g. for archive review).

        Args:
            project: chatroom project to triage.
            identity_name: whose inbox (e.g. "Heisenberg"). Required --
                there is no implicit "current actor" at the MCP layer.
            include_resolved: include `status="resolved"` threads.
                Default False.
            limit: 1..1000, default 100.
            offset: pagination, default 0.

        Returns:
            {"items": [{thread_id, title, status, owner, latest_msg_id,
             last_read_msg_id, unread_count}, ...], "total", "limit",
             "offset"}.
        """
        adapter = _adapter()
        try:
            return await adapter.list_unread(
                project=project,
                identity_name=identity_name,
                include_resolved=include_resolved,
                limit=limit,
                offset=offset,
            )
        finally:
            await adapter.close()
