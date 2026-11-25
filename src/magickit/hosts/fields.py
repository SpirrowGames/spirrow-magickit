"""Validators that rebuild probe output from values, never pass it through.

msg-1772 Obj-2-2: a field's *name* says nothing about its *content*. An
application that writes to the journal can put any string into
``MESSAGE_ID``; a unit's ``SubState`` comes from systemd but is still read
as text. So every value a probe returns goes through one of these and comes
out as an ``int`` in a range, a member of a closed set, or a string matching
a fixed format -- or as ``None``, counted by the caller as omitted. There is
no "return the string as-is" function here, on purpose.

This guarantees the *form* of the output, not that a form-valid value is not
a secret (Einstein msg-1773): a 32-hex ``MESSAGE_ID`` chosen by an
application could encode anything. That residue is why log bodies stay a
separate human decision (ruling 2, msg-1770).
"""

from __future__ import annotations

import re
from collections.abc import Collection

_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")
_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGITS_RE = re.compile(r"^[0-9]{1,20}$")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


def int_in(value: object, lo: int, hi: int) -> int | None:
    """An integer written as plain decimal digits, within ``[lo, hi]``.

    Accepts ``int`` (not ``bool``) or a string of digits only: no sign, no
    whitespace, no ``0x``, no float -- the shapes ``int()`` would quietly
    accept and a forger would reach for.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        n = value
    elif isinstance(value, str) and _DIGITS_RE.match(value):
        n = int(value)
    else:
        return None
    return n if lo <= n <= hi else None


def one_of(value: object, allowed: Collection[str]) -> str | None:
    """``value`` if it is exactly one of ``allowed``."""
    return value if isinstance(value, str) and value in allowed else None


def slug(value: object) -> str | None:
    """A short lowercase systemd-style word (``running``, ``exit-code``).

    For enum-like systemd properties whose full value set changes between
    systemd versions (``SubState``, ``Result``, ``UnitFileState``); a closed
    list would turn an upgrade into silently omitted fields. 32 characters of
    ``[a-z0-9-]`` starting with a letter cannot carry much, and they never
    come from an application.
    """
    return value if isinstance(value, str) and _SLUG_RE.match(value) else None


def hex32(value: object) -> str | None:
    """A sd-id128 in journal form: 32 lowercase hex digits."""
    return value if isinstance(value, str) and _HEX32_RE.match(value) else None


def sha40(value: object) -> str | None:
    """A full git commit sha (lowercase hex, 40)."""
    return value if isinstance(value, str) and _SHA40_RE.match(value) else None


def env_key(value: object) -> str | None:
    """An environment variable name. Names are visible by design (msg-1750 §2.2)."""
    return value if isinstance(value, str) and _ENV_KEY_RE.match(value) else None
