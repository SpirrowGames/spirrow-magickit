"""Chatroom MCP tools — thin wrappers around ChatroomAdapter.

Exposes spirrow-conclair's HTTP endpoints as MCP tools so AI sessions
(Claude.ai / Claude Code) can post / read chatroom messages without
hand-crafting HTTP calls. Each tool delegates to ChatroomAdapter and
forwards the response as-is, including conclair's structured error
envelope (`{error_type, error, details}`) when the upstream returned
4xx/5xx.
"""

from __future__ import annotations

from typing import Any, Literal

from fastmcp import FastMCP

from magickit.adapters.chatroom import ChatroomAdapter
from magickit.adapters.prismind import PrismindAdapter
from magickit.config import Settings
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
    """Apply the naysayer gate AND the human owner-override for a close/decide.

    Two independent policies on the same close path:
    - naysayer gate (#9): a gated thread needs a fresh approving review or a
      human gate-override. Unchanged semantics.
    - owner-override (ADR-2026-06-04-19 D-5): a human may force-close a
      non-owned thread. Magickit is the decision point — it sets the Conclair
      ``owner_override`` flag only for human identities. Ownership bypass is
      independent of the gate (the gate still runs first).

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

    # The thread is needed to run the gate and/or resolve a human force-close.
    if _settings is None or not (gate_enabled or is_human):
        return {
            "action": "proceed", "content": content,
            "owner_override": owner_override, "owner_override_reason": None,
        }

    view = await adapter.get_thread(project=project, thread_id=thread_id, mode="full")
    if "error_type" in view:
        # fail-closed: a gated/forced close must prove its preconditions.
        return {"action": "block", "envelope": view}
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
#   role omitted   -> no lookup, no check, forward nothing. This is I-3
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


async def _check_role_allowed(*, author: str, role: str) -> dict[str, Any] | None:
    """Run the role × allowed_roles gate. Return an error envelope, or None to allow.

    Resolution goes through Prismind's ``get_identity`` (single-record,
    cross-project) rather than ``list_context_authors``. The latter is
    project-scoped and enumerates saved session state, so an identity that is
    registered but has never checkpointed in this project is absent from it --
    ``Einstein`` in ``spirrow-magickit`` is exactly that case. Reading the
    gate's input from the project-scoped listing would classify the actor the
    gate exists to stop as "unregistered" and skip the check.

    Allow cases:
    - ``role`` empty -> caller opted out (I-3); no lookup is performed.
    - identity not registered -> legacy actor, skip (I-3).
    - role present in ``allowed_roles`` -> pass.
    """
    if not role:
        return None

    prismind = _prismind_adapter()
    try:
        result = await prismind.get_identity(identity_name=author)
    except Exception as e:  # transport failure, unknown tool, timeout, ...
        logger.warning(
            "Role validation lookup failed", author=author, role=role, error=str(e)
        )
        return _role_validation_unavailable_error(author=author, role=role, reason=str(e))

    if not isinstance(result, dict):
        return _role_validation_unavailable_error(
            author=author, role=role, reason="unexpected response from Prismind"
        )
    if "error_type" in result:
        return _role_validation_unavailable_error(
            author=author, role=role,
            reason=f"{result['error_type']}: {result.get('error', '')}",
        )
    if not result.get("success", False):
        return _role_validation_unavailable_error(
            author=author, role=role,
            reason=result.get("message") or "identity lookup reported failure",
        )

    if not result.get("found", False):
        # Legacy / unregistered author: skip (I-3). Not a silent hole -- the
        # lookup succeeded and gave a definite "no such identity" answer.
        logger.info(
            "Role check skipped: identity not registered", author=author, role=role
        )
        return None

    identity = result.get("identity") or {}
    allowed_roles = list(identity.get("allowed_roles") or [])
    if role not in allowed_roles:
        return _role_not_allowed_error(
            author=author, role=role, allowed_roles=allowed_roles
        )
    return None


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


def register_tools(mcp: FastMCP, settings: Settings) -> None:
    """Register chatroom MCP tools."""
    global _settings
    _settings = settings

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
                the msg; when omitted nothing is checked or recorded.
                Validated against ``owner``, who authors the propose msg.

        Returns:
            On success: {"thread": {...}, "msg": {...}}.
            On failure: conclair error envelope
            {"error_type": "ChatroomIntegrityError", "error": "...",
             "details": {...}}, or "RoleNotAllowed" /
            "RoleValidationUnavailableError" from the role gate.
        """
        role_error = await _check_role_allowed(author=owner, role=role)
        if role_error is not None:
            return role_error

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
                role=role or None,
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
        - an author with no registered identity is not checked (legacy).
        A recorded role therefore always means "this was verified".

        NOTE: this parameter is named `msg_type` (not `type`) because
        some MCP clients reject schemas that use `type` as a property
        name — they collide with JSON Schema's own `type` keyword.

        Naysayer gate + owner-override: a ``decide`` with ``closes_thread``
        set resolves the thread, so it carries the same policies as
        ``chatroom_close_thread`` — a gated thread requires a fresh approving
        naysayer review unless ``naysayer_override_reason`` (human only) is
        supplied, and a human may force-close a non-owned thread (set
        ``owner_override_reason``; required when non-gated, see
        chatroom_close_thread).

        Returns:
            On success: {"msg": {...}, "thread_status_changed_to":
            null|"awaiting_reply"|"active"|"resolved"}.
            On failure (embodiment missing, role not allowed, naysayer
            gate, conclair error, ...): error_type envelope.
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

        role_error = await _check_role_allowed(author=author, role=role)
        if role_error is not None:
            return role_error

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
                role=role or None,
                owner_override=owner_override,
                owner_override_reason=owner_override_reason_out,
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
                with ``RoleNotAllowed``. NOTE: this is the role×allowed_roles
                check only. The second-stage ``closeable_roles`` check
                ({implementer, integrator, proposer}, msg-003 D-3) is P3 and
                is NOT implemented here; a naysayer-only identity can still
                close a thread it owns as long as it does not claim a role
                outside its own allowed_roles.
            naysayer_override_reason: human-only override of the naysayer
                gate. Non-empty engages the override (reason mandatory);
                ignored on non-gated threads. A non-human author supplying
                it is rejected with NaysayerOverrideForbiddenError.
            owner_override_reason: reason for a human Tier-C force-close of a
                NON-owned thread (ADR-2026-06-04-19 D-5). Required when a
                human closes a thread they do not own and it is NOT gated
                (gated force-close reuses naysayer_override_reason). Recorded
                in the decide msg + Conclair audit event. Has no effect for
                non-human authors (they remain owner-only).

        Returns:
            On success: {"thread": {... status=resolved ...},
                         "decide_msg": {...}}.
            On failure (embodiment missing -> EmbodimentRequiredError,
            role gate -> RoleNotAllowed /
            RoleValidationUnavailableError,
            naysayer gate -> NaysayerReviewRequiredError /
            NaysayerReviewStaleError / NaysayerChangesRequestedError /
            NaysayerOverrideForbiddenError, owner-override reason missing ->
            OwnerOverrideReasonRequiredError, non-owner (non-human) -> 403,
            already resolved -> 409, etc.): error_type envelope.
        """
        # close_thread emits a decide msg internally; same mandatory
        # rule as msg_type="decide" on post_message.
        if author not in HUMAN_IDENTITY_NAMES and not embodiment:
            return _embodiment_required_error(msg_kind="close_thread (emits decide)")

        # I-4: the emitted decide is a message like any other, so the role
        # gate applies here too -- otherwise close would be a bypass.
        role_error = await _check_role_allowed(author=author, role=role)
        if role_error is not None:
            return role_error

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
                role=role or None,
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

        Args:
            status_filter: optional list of statuses to include
                (active / awaiting_reply / resolved / superseded /
                parked). Empty = all.
            owner: optional owner filter (exact match).
            limit: 1..1000, default 100.
            offset: pagination, default 0.

        Returns:
            {"items": [Thread...], "total": int, "limit": int,
             "offset": int}.
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
        - results are sorted "most unread first, then by thread recency"
          so the first page is the actionable surface.
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
