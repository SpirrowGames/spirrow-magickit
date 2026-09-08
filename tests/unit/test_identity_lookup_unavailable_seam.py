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

I-U-4: No code outside the ``_IdentityLookup`` class body reads the raw
  ``unavailable_reason`` field at all -- consumers branch on
  ``is_unavailable`` and take the string from ``reason_or_raise()``.
  Enforced by parsing the two consumer files and rejecting every
  ``ast.Attribute`` read of the field outside the class's own subtree.
  It is one rule with no exception list, which is the entire reason the
  property earns its place: see ``test_no_consumer_reads_the_raw_...``.
  Falsified by any spelling of a raw read, however punctuated -- the
  ten forms of Bohr msg-558 §2 are pinned as fixtures so the guard
  cannot decay back into matching spellings.

I-U-5: ``_IdentityLookup`` is constructed at exactly three places -- the
  ``_LOOKUP_UNREGISTERED`` singleton, ``_lookup_unusable``, and the
  success path of ``_lookup_identity``. Falsified by a fourth site,
  which is how an un-normalised (possibly empty) reason would get back
  in past I-U-2. This one is what makes msg-319's "an empty reason is
  no longer constructible" a checked fact rather than a convention.
"""

from __future__ import annotations

import ast
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


# ---- I-U-4: no consumer reads the raw field outside the class ---------
#
# One rule, not a catalogue of spellings: *nothing outside the
# ``_IdentityLookup`` class body may read ``unavailable_reason`` at all.*
# The pin this replaces was two line-oriented scans looking for
# ``if ...unavailable_reason:`` and ``if ...unavailable_reason is not None:``.
# Bohr msg-558 §2 measured them by mutating the consumer in
# ``_check_next_participant`` into ten forms: both "plain" spellings were
# caught and all eight realistic variations -- ``elif``, a trailing
# comment, black's line wrapping, ``bool(...)``, a compound condition, a
# local alias -- went GREEN. A guard shaped like the spellings someone
# happened to think of is not a guard.
#
# Reading the field is the thing to forbid, and that is a property of the
# syntax tree rather than of a line: ``ast.Attribute(attr="unavailable_reason")``
# is the same node however the surrounding expression is punctuated or
# wrapped, and prose in a docstring that names the field is a ``Constant``
# rather than an ``Attribute``, so the scan needs no comment-stripping
# heuristics to stay off this module's own prose.

CONSUMER_FILES = (
    Path("src") / "magickit" / "mcp" / "tools" / "chatroom.py",
    Path("src") / "magickit" / "web" / "decisions.py",
)

# The ten rows of Bohr msg-558 §2, kept as fixtures so this guard cannot
# silently regress into the shape of the one it replaced. C1/C2 are the
# controls the old scans did catch; V1-V8 are the ones they did not.
KNOWN_SLIP_FORMS = {
    "C1 plain truthiness": "if lookup.unavailable_reason:\n    pass\n",
    "C2 plain is-not-None": "if lookup.unavailable_reason is not None:\n    pass\n",
    "V1 elif truthiness": (
        "if False:\n    pass\nelif lookup.unavailable_reason:\n    pass\n"
    ),
    "V2 trailing comment": (
        "if lookup.unavailable_reason:  # more Pythonic\n    pass\n"
    ),
    "V3 wrapped truthiness": "if (\n    lookup.unavailable_reason\n):\n    pass\n",
    "V4 elif is-not-None": (
        "if False:\n    pass\n"
        "elif lookup.unavailable_reason is not None:\n    pass\n"
    ),
    "V5 wrapped is-not-None": (
        "if (\n    lookup.unavailable_reason\n    is not None\n):\n    pass\n"
    ),
    "V6 compound condition": (
        "if lookup.unavailable_reason and not lookup.found:\n    pass\n"
    ),
    "V7 bool() wrapper": "if bool(lookup.unavailable_reason):\n    pass\n",
    "V8 local alias": "r = lookup.unavailable_reason\nif r:\n    pass\n",
}


def _raw_field_reads(source: str) -> list[int]:
    """Line numbers of every read of the raw ``unavailable_reason`` field
    that is not inside the ``_IdentityLookup`` class body.

    The class body is exempt because that is where the field legitimately
    lives: ``is_unavailable`` and ``reason_or_raise`` must read it, and
    exposing it safely is what they are for. The exemption is by subtree
    membership rather than by a ``self.`` prefix or a line range, so it
    keeps holding if an accessor is renamed or added, or the class moves
    within the file.
    """
    tree = ast.parse(source)
    exempt: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "_IdentityLookup":
            exempt.update(id(child) for child in ast.walk(node))
    return sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr == "unavailable_reason"
        and id(node) not in exempt
    )


def test_no_consumer_reads_the_raw_unavailable_reason_field() -> None:
    """I-U-4. Consumers branch on ``is_unavailable`` and take the string
    from ``reason_or_raise()``; neither reads the raw field outside the
    class, so a clean tree scores zero offenders.

    This is what makes ``is_unavailable`` load-bearing rather than
    decorative, and the reason it survives msg-319's objection ② even
    though that objection's own premise is correct. The property does not
    make a truthiness misread *impossible* -- ``unavailable_reason`` is
    still a public field on a public NamedTuple and any consumer can still
    reach it. What it buys is that reading the field outside the class
    becomes unconditionally wrong, and "unconditionally wrong" is a rule a
    machine can check without an exception list (Bohr msg-558 §3.2(iii)).
    """
    repo_root = Path(__file__).resolve().parents[2]
    offenders = []
    for rel in CONSUMER_FILES:
        path = repo_root / rel
        for lineno in _raw_field_reads(path.read_text(encoding="utf-8")):
            offenders.append(f"{rel.name}:{lineno}")

    assert not offenders, (
        "Outside `_IdentityLookup` itself, `unavailable_reason` must never "
        "be read: branch on `.is_unavailable`, and take the string from "
        "`.reason_or_raise()`. Reading the raw field is how a fail-closed "
        "gate becomes fail-open (T-unavailable-reason-empty-diagnostic "
        "msg-245 §3). Offending reads:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("form", sorted(KNOWN_SLIP_FORMS))
def test_the_guard_detects_every_known_slip_form(form: str) -> None:
    """The guard guarding the guard.

    Every row of the msg-558 §2 table must be detected. Eight of these ten
    were GREEN under the line-oriented scans this replaced, so if a future
    change reintroduces a spelling-shaped guard, this goes RED and names
    the spelling it stopped seeing.
    """
    assert _raw_field_reads(KNOWN_SLIP_FORMS[form]), (
        f"{form} reads the raw field but the guard did not flag it -- the "
        "rule has drifted back to matching spellings instead of reads"
    )


def test_the_guard_does_not_fire_on_the_legitimate_shapes() -> None:
    """Negative control, so the test above cannot be satisfied by a guard
    that simply flags everything.

    Three things must stay silent: branching on the property, the class's
    own accessor reading the field it exists to expose, and prose that
    merely names the field.
    """
    assert _raw_field_reads("if lookup.is_unavailable:\n    pass\n") == []
    assert _raw_field_reads('"""Prose naming unavailable_reason."""\n') == []
    assert (
        _raw_field_reads(
            "class _IdentityLookup:\n"
            "    @property\n"
            "    def is_unavailable(self) -> bool:\n"
            "        return self.unavailable_reason is not None\n"
        )
        == []
    )


# ---- I-U-5: the normalising constructor is the only way in ------------


def _construction_sites(source: str) -> list[tuple[int, str]]:
    """``(lineno, enclosing scope)`` for each direct ``_IdentityLookup(...)``
    call. Scope is the innermost enclosing function name, or ``"<module>"``.
    """
    tree = ast.parse(source)
    sites: list[tuple[int, str]] = []

    def walk(node: ast.AST, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(child, child.name)
                continue
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id == "_IdentityLookup"
            ):
                sites.append((child.lineno, scope))
            walk(child, scope)

    walk(tree, "<module>")
    return sites


def test_only_the_normalising_helper_constructs_an_unavailable_lookup() -> None:
    """I-U-5 (Bohr msg-558 §4 SHOULD).

    msg-319 argued the consumer-side guard is unnecessary because an empty
    reason is no longer constructible. That premise is true today, and this
    test is what keeps it true. "Every unavailable lookup is built through
    ``_lookup_unusable``" was itself an unguarded convention: nothing but a
    docstring stopped a fourth site from writing
    ``_IdentityLookup("", False, ())`` directly and restoring the empty
    reason the normalisation removed.

    Three construction sites are legitimate: the module-level
    ``_LOOKUP_UNREGISTERED`` singleton (reason ``None``), ``_lookup_unusable``
    (which normalises), and the success path in ``_lookup_identity``
    (reason ``None``). A fourth is an offender.
    """
    repo_root = Path(__file__).resolve().parents[2]
    allowed_scopes = {"_lookup_unusable", "_lookup_identity"}
    offenders = []
    module_level = []
    for rel in CONSUMER_FILES:
        path = repo_root / rel
        for lineno, scope in _construction_sites(path.read_text(encoding="utf-8")):
            if scope == "<module>":
                module_level.append(f"{rel.name}:{lineno}")
            elif scope not in allowed_scopes:
                offenders.append(f"{rel.name}:{lineno} in {scope}()")

    assert not offenders, (
        "`_IdentityLookup(...)` must not be constructed directly: an "
        "unavailable verdict goes through `_lookup_unusable`, which "
        "normalises the reason to a non-empty string (msg-245 §5 DoD). "
        "Offending sites:\n  " + "\n  ".join(offenders)
    )
    assert len(module_level) == 1, (
        "expected exactly one module-level construction (the "
        f"`_LOOKUP_UNREGISTERED` singleton), found {module_level}"
    )
    assert chatroom_tools._LOOKUP_UNREGISTERED.unavailable_reason is None, (
        "the module-level singleton must be the confirmed-unregistered "
        "verdict, not an unavailable one"
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
