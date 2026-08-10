"""Detect text that arrived as UTF-8 bytes read as latin-1.

Why this can happen at all
--------------------------
Starlette's urlencoded form parser decodes the raw body with ``latin-1``
(``formparsers.py``: ``field_value.decode("latin-1")``) before scanning
for ``%XX``, then ``unquote_plus`` re-reads the escapes as UTF-8. The
latin-1 there is an *identity byte mapping*, not a claim about language:
it is the only codec that maps all 256 byte values to a character without
loss, so the percent scanner can work on a string without dropping bytes.

That is correct per the urlencoded spec, which assumes a percent-encoded
ASCII body -- and browsers always send one. A client that puts raw UTF-8
bytes straight into the body (``curl -d "content=経由"``) sends bytes that
never reach the percent-decode step, so they stay as the latin-1
characters they were mapped to, and the archive stores the mojibake.

Observed 2026-08-11 in ``scratch-ui-write-probe``: three messages from a
2026-08-03 curl probe, the only corrupted rows among 3,246.

Why this warns and does not reject
----------------------------------
Messages are append-only by design -- Conclair has no update endpoint --
so a corrupted body cannot be fixed afterwards, which is an argument for
catching it at write time. But rejecting would make it impossible to post
an *example* of mojibake, and this team discusses encoding incidents in
the chatroom. So the write goes through and the author is told.

False positives
---------------
Narrow, by construction. ``encode("latin-1")`` requires every character
to be U+00FF or below (one CJK character, emoji or curly quote and the
text is out), and ``decode("utf-8")`` then requires each accented byte to
be followed by a continuation byte 0x80-0xBF. Real European text puts an
ASCII letter after "é", which is not a continuation byte, so it fails and
is not flagged. Measured: 0 hits across the 3,243 uncorrupted messages in
the archive, and 0 across French / German / Spanish / Nordic /
Portuguese / currency-symbol / source-code samples (see
``tests/unit/test_mojibake.py``).

What does hit is text that already contains mojibake -- which is the
point.
"""

from __future__ import annotations

#: Control characters that a genuine recovery would not introduce: C0
#: (minus the whitespace people actually type), DEL, and C1. Their
#: presence means the round trip produced bytes rather than text, so the
#: "recovery" is noise and no warning is worth raising. C1 matters most --
#: ``"Â\x81"`` round-trips cleanly to U+0081, which is not a word.
_DISALLOWED_CONTROLS = (
    frozenset(chr(c) for c in range(0x20) if chr(c) not in "\t\n\r")
    | {"\x7f"}
    | frozenset(chr(c) for c in range(0x80, 0xA0))
)


def recover_mojibake(text: str) -> str | None:
    """Return what ``text`` looked like before the mis-decode, else ``None``.

    ``None`` means "no evidence of mojibake" -- either the round trip is
    impossible (the normal case for correct text) or it is a no-op.
    """
    if not text:
        return None

    try:
        recovered = text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return None

    if recovered == text:
        return None
    if any(ch in _DISALLOWED_CONTROLS for ch in recovered):
        return None
    return recovered


def first_mojibake(fields: dict[str, str]) -> tuple[str, str] | None:
    """First ``(field_name, recovered_text)`` among ``fields``, or ``None``.

    One warning is enough to tell the author the body was mangled, and
    listing every field would bury the recovered text that makes the
    warning actionable.
    """
    for name, value in fields.items():
        recovered = recover_mojibake(value or "")
        if recovered is not None:
            return name, recovered
    return None
