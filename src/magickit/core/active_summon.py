"""Reader for the ``active_summon`` key of ``loop_control_get``.

The contract is owned by spirrow-mindwire. Its canonical text and cases live
in ``tests/data/summon/active_summon_contract.json`` there; this repository
carries a byte-identical copy (``tests/data/summon/``) and
``tests/unit/test_active_summon_contract.py`` runs every case through
:func:`parse_active_summon`. **Change the canonical file first, then copy it
here** -- the copy's sha256 is pinned in that test so a silent divergence
reds the gate instead of the board.

Contract (verbatim summary of the fixture's ``_contract``):

- key absent or ``null``: no Summon (``kind="none"``);
- ``{thread_id: non-blank string, since: ISO 8601 with an explicit offset
  (Z or +hh:mm), at most 6 fractional digits, by: non-blank string}``: a
  Summon (``kind="valid"``). Extra keys are ignored, and key names match
  case-sensitively, so a case-variant key is an extra key;
- anything else: ``kind="malformed"``. mindwire fails that safe to HOLD, so
  Magickit must *show* it, never hide it as "no Summon".

Why Magickit has its own reader instead of importing mindwire's: Magickit
does not depend on spirrow-mindwire, and the fixture is the shared truth both
readers are held to (Bohr design v1.2 §2).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

#: ISO 8601 with an explicit offset and at most 6 fractional digits. The
#: regex is the gate; ``datetime.fromisoformat`` only converts what it let
#: through (on 3.11 it would also accept forms the contract does not, e.g. a
#: missing offset).
_SINCE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$"
)

SummonKind = Literal["none", "valid", "malformed"]


@dataclass(frozen=True)
class ActiveSummon:
    """The parsed value. ``thread_id`` / ``since`` / ``by`` only when valid."""

    kind: SummonKind
    thread_id: str | None = None
    #: Normalised to UTC.
    since: datetime | None = None
    by: str | None = None


def _non_blank(value: Any) -> bool:
    return isinstance(value, str) and value.strip() != ""


def parse_since(value: Any) -> datetime | None:
    """``since`` as an aware UTC datetime, or ``None`` if it breaks the contract."""
    if not isinstance(value, str) or not _SINCE_RE.match(value):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:  # unreachable behind the regex; kept fail-closed
        return None
    return parsed.astimezone(timezone.utc)


def parse_active_summon(control: dict[str, Any]) -> ActiveSummon:
    """Classify a ``loop_control_get`` response's ``active_summon``.

    Args:
        control: the whole response body (the key is read from it, so that
            "absent" and "null" are both visible to this function).

    Returns:
        :class:`ActiveSummon` with ``kind`` none / valid / malformed.
    """
    if "active_summon" not in control or control["active_summon"] is None:
        return ActiveSummon(kind="none")
    raw = control["active_summon"]
    if not isinstance(raw, dict):
        return ActiveSummon(kind="malformed")
    thread_id = raw.get("thread_id")
    by = raw.get("by")
    since = parse_since(raw.get("since"))
    if not _non_blank(thread_id) or not _non_blank(by) or since is None:
        return ActiveSummon(kind="malformed")
    return ActiveSummon(kind="valid", thread_id=thread_id, since=since, by=by)
