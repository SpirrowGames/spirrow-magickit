"""Shape of the optional ``disposition`` on a chatroom post.

T-magickit-stop-disposition-intake (Bohr DESIGN v6 §2 範囲 1, v8 §2):

    {kind: done} | {kind: blocked_on, trigger: {arm, ref}, wake: <identity>}

A post without ``disposition`` is accepted exactly as before (backward
compatibility). This module is the pure schema half: it only parses the value.
Whether it names ``human`` (as trigger arm, or as a wake whose registered
roles include ``human``) and who may do that (``role = human`` only) are
decided by the gate in ``magickit.mcp.tools.chatroom``, which owns the
identity lookup -- the registry is the one source for "who is human".

Trigger arms. An agent may write ``thread`` / ``pr`` / ``deploy`` /
``queue-empty`` (DESIGN v6 §2 前提); ``human`` is in the schema because a
``role = human`` author may write it, and the gate refuses it from anyone
else. ``{kind: malformed}`` is deliberately absent (DESIGN v9 §2: a malformed
STOP line is a mindwire client error, not a thread lifecycle state).
"""

from __future__ import annotations

from typing import Any, NamedTuple

KIND_DONE = "done"
KIND_BLOCKED_ON = "blocked_on"

HUMAN_ARM = "human"
AGENT_ARMS = ("thread", "pr", "deploy", "queue-empty")
TRIGGER_ARMS = (*AGENT_ARMS, HUMAN_ARM)


class DispositionError(ValueError):
    """The supplied ``disposition`` does not fit the schema."""


class Disposition(NamedTuple):
    """A validated disposition. ``wire`` is what is forwarded to Conclair."""

    kind: str
    arm: str | None
    ref: str | None
    wake: str | None

    @property
    def wire(self) -> dict[str, Any]:
        if self.kind == KIND_DONE:
            return {"kind": KIND_DONE}
        return {
            "kind": KIND_BLOCKED_ON,
            "trigger": {"arm": self.arm, "ref": self.ref},
            "wake": self.wake,
        }


def _require_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DispositionError(f"{field} must be a non-empty string")
    return value.strip()


def parse_disposition(raw: Any) -> Disposition:
    """Validate ``raw`` against the schema. Raises ``DispositionError``.

    Strict: unknown keys are refused rather than dropped, so a caller that
    misspells a field learns about it instead of having it silently ignored.
    """
    if not isinstance(raw, dict):
        raise DispositionError("disposition must be an object")
    kind = raw.get("kind")
    if kind == KIND_DONE:
        extra = set(raw) - {"kind"}
        if extra:
            raise DispositionError(f"unexpected keys for kind=done: {sorted(extra)}")
        return Disposition(KIND_DONE, None, None, None)
    if kind != KIND_BLOCKED_ON:
        raise DispositionError(
            f"kind must be {KIND_DONE!r} or {KIND_BLOCKED_ON!r}, got {kind!r}"
        )
    extra = set(raw) - {"kind", "trigger", "wake"}
    if extra:
        raise DispositionError(f"unexpected keys for kind=blocked_on: {sorted(extra)}")
    trigger = raw.get("trigger")
    if not isinstance(trigger, dict):
        raise DispositionError("trigger must be an object {arm, ref}")
    extra = set(trigger) - {"arm", "ref"}
    if extra:
        raise DispositionError(f"unexpected keys in trigger: {sorted(extra)}")
    arm = _require_str(trigger.get("arm"), "trigger.arm")
    if arm not in TRIGGER_ARMS:
        raise DispositionError(f"trigger.arm must be one of {list(TRIGGER_ARMS)}, got {arm!r}")
    ref = _require_str(trigger.get("ref"), "trigger.ref")
    wake = _require_str(raw.get("wake"), "wake")
    return Disposition(KIND_BLOCKED_ON, arm, ref, wake)
