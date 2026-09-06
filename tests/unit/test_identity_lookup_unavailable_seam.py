"""Structural tests for the ``_IdentityLookup`` "unavailable" seam.

Spec: T-unavailable-reason-empty-diagnostic
  - Bohr msg-245 §1 (the transport branch's ``str(e)`` was the last path
    to a *reachable* empty ``unavailable_reason``) and §3 (six consumers
    depend on ``is not None`` -- one truthiness slip flips a fail-closed
    gate to fail-open).
  - Einstein msg-246 (advisory approval + structural counter-proposal:
    swap the six per-consumer pins for a typed ``is_unavailable`` boolean
    on the result object, and use ``str(e).strip() or ...`` so a
    whitespace-only exception message cannot slip past ``or``).

The point is DELIBERATELY not to characterise the six call sites in six
tests (that was Bohr's proposed pin, and it fixes the encoding rather
than the abstraction). The point is to pin the two invariants the type
now carries -- one at the *producer* side, one at the *consumer* side --
so any regression that reintroduces the empty-reason class is caught
without depending on which caller writes which idiom.

I-U-1: ``_IdentityLookup`` exposes a boolean ``is_unavailable`` property
  whose truthiness is independent of the reason string's contents.
  Falsified if a future change routes the branch back through the string
  (e.g. removes the property, or reimplements it as
  ``bool(self.unavailable_reason)`` so an empty reason reads as "usable").

I-U-2: ``_lookup_unusable`` normalises its reason so no construction
  path can produce ``is_unavailable=True`` with an empty or
  whitespace-only ``unavailable_reason``. Falsified if the normalisation
  is dropped and a downstream error envelope surfaces "the identity
  lookup failed ()" to the caller.

I-U-3: The transport-failure branch of ``_lookup_identity`` never carries
  a bare ``str(e)`` into ``_lookup_unusable`` when the exception has an
  empty or whitespace-only message -- ``type(e).__name__`` is used as a
  non-empty fallback. Falsified if a ``raise SomeError()`` (or
  ``raise SomeError(" ")``) produces an ``unavailable_reason`` that is
  merely a placeholder from the constructor rather than the exception's
  type name.

I-U-4: Every consumer that used to read
  ``lookup.unavailable_reason is not None`` now reads ``lookup.is_unavailable``.
  This is enforced not by six behavioural tests but by a source-level
  scan -- one test that greps the two files where the consumers live and
  fails RED if the legacy ``is not None`` idiom returns. Reason: the
  behaviour is already covered by ``test_role_gate`` / ``test_next_participant_gate``
  / ``test_decisions_form_radio_and_target`` in aggregate; the new
  invariant is *how* the branch is written, and Einstein msg-246
  specifically warned against locking in the shape with six per-site
  behavioural pins.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from magickit.mcp.tools import chatroom as chatroom_tools


# ---- I-U-1: is_unavailable is a real boolean, not string truthiness ---


def test_is_unavailable_true_when_reason_is_a_nonempty_string() -> None:
    """The typical ``_lookup_unusable`` output: reason set, gate must refuse."""
    lookup = chatroom_tools._lookup_unusable("prismind is down")
    assert lookup.is_unavailable is True
    assert lookup.unavailable_reason == "prismind is down"


def test_is_unavailable_false_for_the_confirmed_unregistered_verdict() -> None:
    """The legacy skip path: no reason string, and the lookup IS usable."""
    lookup = chatroom_tools._LOOKUP_UNREGISTERED
    assert lookup.is_unavailable is False
    assert lookup.unavailable_reason is None


def test_is_unavailable_false_for_a_successful_verdict() -> None:
    """The happy path: registered + roles known, and the lookup IS usable."""
    lookup = chatroom_tools._IdentityLookup(
        unavailable_reason=None, found=True, allowed_roles=("proposer",)
    )
    assert lookup.is_unavailable is False


def test_is_unavailable_returns_a_real_bool_not_a_truthy_string() -> None:
    """Guards against a refactor that returns ``self.unavailable_reason``
    directly (which would make ``is_unavailable`` a ``str | None`` and let
    ``if lookup.is_unavailable:`` read as truthiness again -- reintroducing
    the exact fail-open Bohr msg-245 §3 identified).
    """
    unusable = chatroom_tools._lookup_unusable("boom")
    assert type(unusable.is_unavailable) is bool

    registered = chatroom_tools._IdentityLookup(
        unavailable_reason=None, found=True, allowed_roles=()
    )
    assert type(registered.is_unavailable) is bool


# ---- I-U-2: _lookup_unusable never produces an empty diagnostic --------


@pytest.mark.parametrize(
    "raw_reason",
    ["", " ", "\t", "\n", "  \n\t "],
    ids=["empty", "space", "tab", "newline", "mixed-whitespace"],
)
def test_lookup_unusable_normalises_empty_and_whitespace_reasons(raw_reason: str) -> None:
    """No matter what the caller hands in, the stored reason is non-empty.

    Guarantees the invariant that when ``is_unavailable`` is True the
    error envelope's ``({reason})`` parenthetical is never blank. Bohr
    msg-245 §5 named this as the DoD: "no input can construct an empty
    unavailable_reason". Einstein msg-246 asked for ``.strip()`` so
    whitespace-only inputs (which ``or`` alone treats as truthy) are
    caught too.
    """
    lookup = chatroom_tools._lookup_unusable(raw_reason)
    assert lookup.is_unavailable is True
    assert lookup.unavailable_reason
    assert lookup.unavailable_reason.strip() == lookup.unavailable_reason


def test_lookup_unusable_preserves_a_meaningful_reason_verbatim() -> None:
    """Normalisation must not mangle a caller's real diagnostic. The
    only transformation is ``.strip()``; an internal message like
    ``"prismind: 502 bad gateway"`` reaches the envelope unchanged.
    """
    lookup = chatroom_tools._lookup_unusable("prismind: 502 bad gateway")
    assert lookup.unavailable_reason == "prismind: 502 bad gateway"


# ---- I-U-3: transport branch uses type(e).__name__ as non-empty fallback


class _FakePrismindAdapter:
    """Minimal test double: ``get_identity`` raises whatever we hand it."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.get_identity = AsyncMock(side_effect=exc)


@pytest.fixture
def settings():
    from magickit.config import Settings

    return Settings(
        conclair_url="http://localhost:8115",
        conclair_timeout=5.0,
        prismind_url="http://localhost:8002",
        prismind_timeout=5.0,
    )


@pytest.fixture(autouse=True)
def _configure_module(settings) -> None:
    """``_lookup_identity`` reads its Prismind adapter through
    ``_prismind_adapter()``, which requires ``configure()`` to have run.
    """
    chatroom_tools.configure(settings)


class _NamedFailure(Exception):
    """A distinct exception type so ``type(e).__name__`` is unambiguous."""


@pytest.mark.asyncio
async def test_transport_failure_with_empty_message_falls_back_to_type_name() -> None:
    """``raise _NamedFailure()`` has ``str(e) == ""``. The transport
    branch must not carry that emptiness into ``_lookup_unusable``; the
    exception's *type* is still informative and lands in the envelope's
    parenthetical instead. Bohr msg-245 §4 pinned this as the required
    fallback ("do not drop the type name").
    """
    adapter = _FakePrismindAdapter(_NamedFailure())
    with patch.object(chatroom_tools, "_prismind_adapter", return_value=adapter):
        lookup = await chatroom_tools._lookup_identity("Einstein")

    assert lookup.is_unavailable is True
    assert lookup.unavailable_reason == "_NamedFailure"


@pytest.mark.asyncio
async def test_transport_failure_with_whitespace_message_falls_back_to_type_name() -> None:
    """A message of ``"   "`` is truthy under bare ``or`` but useless
    to a human reading the envelope. Einstein msg-246 asked for
    ``str(e).strip() or ...`` specifically so the fallback catches it.
    """
    adapter = _FakePrismindAdapter(_NamedFailure("   "))
    with patch.object(chatroom_tools, "_prismind_adapter", return_value=adapter):
        lookup = await chatroom_tools._lookup_identity("Einstein")

    assert lookup.is_unavailable is True
    assert lookup.unavailable_reason == "_NamedFailure"


@pytest.mark.asyncio
async def test_transport_failure_with_real_message_preserves_it() -> None:
    """The fallback must not fire when the message IS informative --
    the type-name fallback exists to fill the gap, not to overwrite a
    real diagnostic. ``str(e).strip()`` is preserved verbatim through
    ``_lookup_unusable``'s normalisation.
    """
    adapter = _FakePrismindAdapter(_NamedFailure("connection refused"))
    with patch.object(chatroom_tools, "_prismind_adapter", return_value=adapter):
        lookup = await chatroom_tools._lookup_identity("Einstein")

    assert lookup.is_unavailable is True
    assert lookup.unavailable_reason == "connection refused"


# ---- I-U-4: no consumer reads unavailable_reason via `is not None` -----


def test_no_source_consumer_uses_the_legacy_is_not_none_idiom() -> None:
    """Source-level pin (not behavioural): if any consumer of
    ``_IdentityLookup`` reverts to ``lookup.unavailable_reason is not None``,
    this test fails RED and the reviewer is pointed at the six-consumer
    fail-open surface that motivated ``is_unavailable`` in the first place.

    Deliberately scoped to consumer files only -- the DEFINITION of
    ``is_unavailable`` inside ``chatroom.py`` uses the ``is not None``
    form (``return self.unavailable_reason is not None``), and that line
    is legitimate: it is the ONE place the property is allowed to
    reference the field's null-ness. The scan excludes the class body
    by looking for ``lookup.unavailable_reason`` / ``.unavailable_reason``
    patterns (call sites), not the bare field access inside the property.
    """
    repo_root = Path(__file__).resolve().parents[2]
    files_to_scan = [
        repo_root / "src" / "magickit" / "mcp" / "tools" / "chatroom.py",
        repo_root / "src" / "magickit" / "web" / "decisions.py",
    ]
    offenders: list[str] = []
    for path in files_to_scan:
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            # A CONSUMER read is characterised by ``if <name>.unavailable_reason
            # is not None:`` -- the branch statement that used to guard every
            # gate. The property body (``return self.unavailable_reason is not
            # None``) is the ONE legitimate use of the ``is not None`` form and
            # is not a branch statement, so restricting the scan to ``if``
            # lines excludes it structurally rather than by name.
            if (
                stripped.startswith("if ")
                and ".unavailable_reason is not None" in stripped
            ):
                offenders.append(f"{path.name}:{lineno}: {stripped}")

    assert not offenders, (
        "Consumers of _IdentityLookup must branch on `.is_unavailable`, "
        "not on `.unavailable_reason is not None` -- the property is the "
        "typed seam that structurally rules out a truthiness slip "
        "(T-unavailable-reason-empty-diagnostic / Einstein msg-246). "
        "Offending lines:\n  " + "\n  ".join(offenders)
    )


def test_no_source_consumer_reads_reason_via_truthiness() -> None:
    """The tighter form of the same pin: the ``if lookup.unavailable_reason:``
    truthiness idiom -- the one Bohr msg-245 §3 specifically warned about
    -- must not appear anywhere in the consumer files. If a reviewer
    "cleans up" the ``is not None`` to a bare truthy check, this catches
    them before the empty-string class becomes fail-open.
    """
    repo_root = Path(__file__).resolve().parents[2]
    files_to_scan = [
        repo_root / "src" / "magickit" / "mcp" / "tools" / "chatroom.py",
        repo_root / "src" / "magickit" / "web" / "decisions.py",
    ]
    offenders: list[str] = []
    for path in files_to_scan:
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"'):
                continue
            if "``" in stripped:
                continue
            # ``if <name>.unavailable_reason:`` or
            # ``if not <name>.unavailable_reason:`` -- both are wrong.
            if (
                stripped.startswith("if ")
                and stripped.endswith(".unavailable_reason:")
            ):
                offenders.append(f"{path.name}:{lineno}: {stripped}")

    assert not offenders, (
        "The truthiness idiom `if <lookup>.unavailable_reason:` reads an "
        "empty reason as `usable` and turns the gate fail-open. Branch "
        "on `.is_unavailable` instead. Offending lines:\n  "
        + "\n  ".join(offenders)
    )


# ---- reason_or_raise: the typed narrowing helper -----------------------


def test_reason_or_raise_returns_the_stored_reason_when_unavailable() -> None:
    lookup = chatroom_tools._lookup_unusable("prismind: 502")
    assert lookup.reason_or_raise() == "prismind: 502"


def test_reason_or_raise_asserts_when_lookup_is_usable() -> None:
    """Programming-error assertion: the caller must have branched on
    ``is_unavailable`` before asking for the reason. The method exists
    so consumers can hand ``reason: str`` to error-envelope constructors
    without a defensive ``or ""`` at every call site; it must not
    silently return a filler when misused.
    """
    lookup = chatroom_tools._IdentityLookup(
        unavailable_reason=None, found=True, allowed_roles=()
    )
    with pytest.raises(AssertionError):
        lookup.reason_or_raise()
