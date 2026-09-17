"""Close-detection SSOT (T-close-detection-truthiness-seam).

Spec: T-close-detection-truthiness-seam. Layered history:

- msg-243 (Bohr, carve out ①): "is this a close?" was evaluated at two
  ingress sites via ``bool(closes_thread)`` — dual management (Principle
  2). Route both sites through a single SSOT.
- msg-244 (Einstein advisory): eliminate the truthiness ambiguity at
  Magickit's *own* ingress; do not paper over the seam by mimicking
  Conclair's edge-case string parsing.
- msg-725 (Einstein blocking): the predicate must be self-defending
  against an unnormalized ``""`` argument.
- msg-731 (PR-gate advisory): a predicate that normalizes internally AND
  a caller that also normalizes for the adapter is itself dual management.
- msg-779 (human direction B): eliminate that dual-management by
  restructuring the predicate to require pre-normalized input.
- msg-783 (Einstein blocking): a raw ``tuple`` return leaves Python's
  default truthiness rules as a footgun (``bool((False, None)) is True``).
- msg-784 (Bohr disposition, msg-782 amended): return a frozen
  ``_CloseClassification`` dataclass with ``__bool__`` overridden to
  return ``is_close``; ``_classify_closes`` fuses normalization and
  classification in one pass.

Two things this file pins:

1. ``_classify_closes`` is the single-source-of-truth close classifier
   on the Magickit side. Both ingress sites (the MCP
   ``chatroom_post_message`` tool and the browser POST handler in
   ``web/chatroom_writes.py``) route their "is this a close?" decision
   and their adapter-forwarded ``closes_thread`` value through
   ``_classify_closes`` — replacing the stray ``bool(closes_thread)``
   copies that msg-243 identified as dual management (Principle 2). An
   AST-level pin also catches a re-introduction of the old
   ``_is_close_post`` API so the two-function seam cannot silently
   return; PR-gate #84 msg-789 flagged the earlier substring pin as
   bypassable by whitespace or a Name rename, so the pin walks the
   parsed tree instead.

2. The Conclair-side baseline that msg-243 Step 1 asked for is
   *documented* here (not mimicked). Conclair's close detection lives at
   ``spirrow_conclair.services.integrity.assert_closes_thread_rule``
   (``closes_thread is None`` short-circuits as "not a close"; anything
   else — including ``""`` — is a close attempt checked against
   ``thread_id``) and ``spirrow_conclair.services.status_transition.
   compute_transition`` (``type == "decide" and closes_thread ==
   thread.thread_id`` — an exact ``thread_id`` match is what actually
   resolves the thread). Magickit does NOT re-encode those rules — it
   normalizes at ingress so Conclair only ever receives ``None`` or a
   non-empty string, and the empty-string edge case Conclair tolerates
   cannot arise on this wire in the first place. That is why the SSOT
   helper is as small as it is.
"""

from __future__ import annotations

import ast
import dataclasses
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from magickit.config import Settings
from magickit.main import create_app
from magickit.mcp.tools import chatroom as chatroom_tools


PROJECT = "spirrow-magickit"
THREAD = "T-1"


# --- pure helper: _classify_closes ------------------------------------


class TestClassifyClosesPure:
    """``_classify_closes`` fuses normalization and the close-post
    decision in a single pass and returns both outputs on a
    ``_CloseClassification`` dataclass so they cannot drift."""

    @pytest.mark.parametrize(
        "msg_type",
        ["propose", "question", "answer", "report", "handoff", "ack"],
    )
    def test_non_decide_is_never_close(self, msg_type: str) -> None:
        # Only ``decide`` closes threads in Conclair's status_transition;
        # this classifier must agree on that side of the fence too.
        result = chatroom_tools._classify_closes(msg_type, "T-1")
        assert result.is_close is False
        # A non-decide still preserves its ``closes_thread`` on the wire —
        # Conclair will reject a non-decide carrying ``closes_thread`` at
        # its own layer; we don't second-guess that here, we just don't
        # route to the close gate.
        assert result.closes_thread_norm == "T-1"

    def test_decide_with_none_is_not_close(self) -> None:
        result = chatroom_tools._classify_closes("decide", None)
        assert result.is_close is False
        assert result.closes_thread_norm is None

    def test_decide_with_thread_id_is_close(self) -> None:
        result = chatroom_tools._classify_closes("decide", "T-1")
        assert result.is_close is True
        assert result.closes_thread_norm == "T-1"

    def test_classify_empty_string_returns_false_and_none(self) -> None:
        # Pins Einstein's blocking objection (msg-725) restructured
        # through msg-779 / msg-782 / msg-784: an unnormalized ``""``
        # reaching the classifier must NOT misroute to the close-gate
        # authorization path. It collapses to ``None`` inside the single
        # normalization pass, which drops to the standard role gate —
        # the same gate the value's semantics ("no thread being closed")
        # demand. This flips the earlier
        # ``test_empty_string_evaluates_false`` from a predicate-only
        # assertion to a joint pin: the wire value is also ``None``, so
        # Conclair never sees ``""`` either.
        result = chatroom_tools._classify_closes("decide", "")
        assert result.is_close is False
        assert result.closes_thread_norm is None

    def test_classify_non_empty_passes_through(self) -> None:
        # Guards against a future edit that accidentally over-normalizes:
        # a non-empty string must reach the wire verbatim as data
        # (Conclair compares against ``thread.thread_id``). ``T-1`` is
        # the exact-match case; ``T-other`` is the "reaches Conclair
        # verbatim and Conclair rejects" case — the point here is that
        # Magickit does not try to know that.
        result = chatroom_tools._classify_closes("decide", "T-1")
        assert result.closes_thread_norm == "T-1"

    def test_whitespace_only_string_passes_through(self) -> None:
        # Preserved as-is on purpose: ``bool("   ")`` was True in the
        # old code path, so Conclair already had to cope with this value;
        # we do not silently change its meaning on the wire in this fix.
        # (Conclair strips whitespace at its own schema layer.) Widening
        # normalization to ``str.strip() or None`` is a follow-up outside
        # carve-out ①'s scope.
        result = chatroom_tools._classify_closes("decide", "   ")
        assert result.closes_thread_norm == "   "
        assert result.is_close is True


# --- structural defense on the return value (msg-783 blocking) --------


class TestCloseClassificationStructure:
    """The return object itself must be structurally safe against
    Python's default "non-empty object → True" trap and against silent
    mutation between the gate decision and the adapter-forward step."""

    def test_bool_defense_returns_is_close(self) -> None:
        # msg-783 blocking objection: ``bool((False, None)) is True``
        # for a raw tuple, so a caller who forgets to unpack the return
        # would silently misroute to the close gate. The
        # ``_CloseClassification`` dataclass overrides ``__bool__`` to
        # return ``is_close`` so ``if _classify_closes(...):`` degrades
        # to the gate decision instead. This test pins that defense.
        assert bool(chatroom_tools._classify_closes("reply", "")) is False
        assert bool(chatroom_tools._classify_closes("decide", "")) is False
        assert bool(chatroom_tools._classify_closes("decide", None)) is False
        assert bool(chatroom_tools._classify_closes("decide", "T-1")) is True
        # And the sibling case: a non-decide carrying a thread_id is
        # still not a close, so truthiness must be False.
        assert bool(chatroom_tools._classify_closes("report", "T-1")) is False

    def test_frozen_rejects_mutation(self) -> None:
        # ``frozen=True`` pins the object against mutation between the
        # gate decision and the adapter-forward step. Structural
        # invariant, not caller discipline: a future edit that tries to
        # rewrite the classification after routing would fail loudly here.
        result = chatroom_tools._classify_closes("decide", "T-1")
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.is_close = False  # type: ignore[misc]
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.closes_thread_norm = "T-other"  # type: ignore[misc]


# --- SSOT: both ingress sites route through _classify_closes ----------


def _identity_response(
    allowed_roles: list[str], *, name: str = "Bohr"
) -> dict[str, Any]:
    return {
        "success": True,
        "found": True,
        "identity": {
            "identity_name": name,
            "user": "sgadmin",
            "allowed_roles": allowed_roles,
            "independence_class": "independent",
            "persona_description": "",
        },
        "message": "ok",
    }


@pytest.fixture(autouse=True)
def _configured():
    """Gates read module-level settings; the app normally binds them at startup."""
    chatroom_tools.configure(Settings())
    yield
    chatroom_tools._settings = None


def _capture_tools(settings: Settings) -> dict[str, Any]:
    registered: dict[str, Any] = {}

    def fake_tool(*args: Any, **kwargs: Any):
        def decorator(fn):
            registered[fn.__name__] = fn
            return fn

        return decorator

    mock_mcp = MagicMock()
    mock_mcp.tool = fake_tool
    chatroom_tools.register_tools(mock_mcp, settings)
    return registered


def _chat_adapter() -> MagicMock:
    a = MagicMock()
    a.open_thread = AsyncMock(return_value={"thread": {}, "msg": {}})
    a.post_message = AsyncMock(
        return_value={"msg": {"msg_id": "m", "type": "report"}, "thread_status_changed_to": None}
    )
    a.close_thread = AsyncMock(return_value={"thread": {}, "decide_msg": {}})
    a.get_thread = AsyncMock(
        return_value={"thread": {"tags": []}, "messages": [], "mode": "full"}
    )
    a.close = AsyncMock()
    return a


def _prismind_adapter(allowed_roles: list[str], name: str = "Bohr") -> MagicMock:
    a = MagicMock()
    a.get_identity = AsyncMock(return_value=_identity_response(allowed_roles, name=name))
    return a


def _call_real(msg_type: str, raw: str | None) -> chatroom_tools._CloseClassification:
    """Standalone re-implementation of the classifier used only by the
    spy; the spy patches the module attribute so it cannot call the
    real one without recursion. If a future edit changes the classifier,
    this stays in lockstep because the tests that depend on it also
    cover the pure helper directly (``TestClassifyClosesPure``)."""
    normalized = raw or None
    return chatroom_tools._CloseClassification(
        is_close=(msg_type == "decide" and normalized is not None),
        closes_thread_norm=normalized,
    )


@pytest.mark.asyncio
async def test_mcp_post_message_routes_close_decision_through_ssot_helper() -> None:
    """The MCP ``chatroom_post_message`` tool must ask ``_classify_closes``
    (not its own inline ``bool(closes_thread)``) whether the post closes."""
    settings = Settings(
        conclair_url="http://localhost:8115",
        conclair_timeout=5.0,
        prismind_url="http://localhost:8002",
        prismind_timeout=5.0,
    )
    tools = _capture_tools(settings)
    chat = _chat_adapter()
    prismind = _prismind_adapter(["proposer"])

    calls: list[tuple[str, str | None]] = []

    def spy(msg_type: str, raw: str | None) -> chatroom_tools._CloseClassification:
        calls.append((msg_type, raw))
        return _call_real(msg_type, raw)

    with (
        patch.object(chatroom_tools, "_adapter", return_value=chat),
        patch.object(chatroom_tools, "_prismind_adapter", return_value=prismind),
        patch.object(chatroom_tools, "_classify_closes", side_effect=spy) as mock_classify,
    ):
        await tools["chatroom_post_message"](
            project=PROJECT, thread_id=THREAD, msg_type="report",
            author="Bohr", content="c", role="proposer",
        )

    assert mock_classify.called, (
        "MCP tool must call the SSOT classifier; a stray bool(closes_thread) "
        "copy would leave the spy uncalled."
    )
    # The MCP tool must have handed the raw ingress string to the
    # classifier — with the ``str = ""`` default, that is ``""`` (which
    # the classifier collapses to ``None`` on the way out).
    assert calls[0] == ("report", "")


@pytest.mark.asyncio
async def test_browser_post_routes_close_decision_through_ssot_helper() -> None:
    """The browser POST handler must ask ``_classify_closes`` too. This
    is the site msg-243 flagged as the duplicate — pinning it here is
    the "the two never drift" invariant."""
    chat = _chat_adapter()
    prismind = _prismind_adapter(["proposer"])

    calls: list[tuple[str, str | None]] = []

    def spy(msg_type: str, raw: str | None) -> chatroom_tools._CloseClassification:
        calls.append((msg_type, raw))
        return _call_real(msg_type, raw)

    app = create_app()
    with (
        patch.object(chatroom_tools, "_adapter", return_value=chat),
        patch.object(chatroom_tools, "_prismind_adapter", return_value=prismind),
        patch.object(chatroom_tools, "_classify_closes", side_effect=spy) as mock_classify,
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            await client.post(
                f"/ui/projects/{PROJECT}/threads/{THREAD}/messages",
                data={
                    "type": "report", "author": "Bohr",
                    "content": "c", "role": "proposer",
                },
            )

    assert mock_classify.called, (
        "Browser POST must call the SSOT classifier; a stray bool(closes_thread) "
        "copy would leave the spy uncalled."
    )
    assert calls[0] == ("report", "")


@pytest.mark.asyncio
async def test_mcp_tool_forwards_normalized_closes_thread_to_adapter() -> None:
    """The classifier's ``closes_thread_norm`` output must reach the
    adapter — the same normalized value the classifier saw goes on the
    wire to Conclair. This is what removes the two-locations-doing-
    ``bool(...)`` seam: there is a single ``str | None`` value flowing
    from ingress to Conclair."""
    settings = Settings(
        conclair_url="http://localhost:8115",
        conclair_timeout=5.0,
        prismind_url="http://localhost:8002",
        prismind_timeout=5.0,
    )
    tools = _capture_tools(settings)
    chat = _chat_adapter()
    prismind = _prismind_adapter(["proposer"])

    with (
        patch.object(chatroom_tools, "_adapter", return_value=chat),
        patch.object(chatroom_tools, "_prismind_adapter", return_value=prismind),
    ):
        # Empty ingress → None on the wire.
        await tools["chatroom_post_message"](
            project=PROJECT, thread_id=THREAD, msg_type="report",
            author="Bohr", content="c", closes_thread="",
            role="proposer",
        )

    assert chat.post_message.call_args.kwargs["closes_thread"] is None


@pytest.mark.asyncio
async def test_browser_post_forwards_normalized_closes_thread_to_adapter() -> None:
    """Twin of the previous test on the browser POST path."""
    chat = _chat_adapter()
    prismind = _prismind_adapter(["proposer"])

    app = create_app()
    with (
        patch.object(chatroom_tools, "_adapter", return_value=chat),
        patch.object(chatroom_tools, "_prismind_adapter", return_value=prismind),
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            await client.post(
                f"/ui/projects/{PROJECT}/threads/{THREAD}/messages",
                data={
                    "type": "report", "author": "Bohr", "content": "c",
                    "closes_thread": "", "role": "proposer",
                },
            )

    assert chat.post_message.call_args.kwargs["closes_thread"] is None


# --- AST pin: no stray copies of the old two-function seam ------------


def _find_bool_of_closes_thread(tree: ast.AST) -> list[ast.Call]:
    """Return every ``bool(<expr>)`` call whose argument subtree
    references a ``Name`` with id ``closes_thread``. AST-based so the
    check is invariant under whitespace, line-breaks, and expression
    shape (``bool( closes_thread )``, ``bool(closes_thread or None)``,
    ``bool((closes_thread))`` all resolve to the same shape here).

    Chosen over a substring match on the source (PR-gate #84 msg-789
    advisory): a substring pin was bypassed by whitespace or an alias
    rename; this walks the parsed tree so formatting cannot silently
    return the seam."""
    hits: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "bool"):
            continue
        for arg in node.args:
            for sub in ast.walk(arg):
                if isinstance(sub, ast.Name) and sub.id == "closes_thread":
                    hits.append(node)
                    break
    return hits


def _find_is_close_post_calls(tree: ast.AST) -> list[ast.Call]:
    """Return every call to the retired ``_is_close_post`` predicate.
    Catches both ``_is_close_post(...)`` and any attribute form
    ``<module>._is_close_post(...)`` — the retired name is what matters,
    not the qualifier."""
    hits: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "_is_close_post":
            hits.append(node)
        elif isinstance(func, ast.Attribute) and func.attr == "_is_close_post":
            hits.append(node)
    return hits


@pytest.mark.parametrize(
    "rel",
    ["mcp/tools/chatroom.py", "web/chatroom_writes.py"],
)
def test_no_bool_closes_thread_copies_in_ingress_sites(rel: str) -> None:
    """AST-level pin: neither ingress site contains a stray
    ``bool(<expr involving closes_thread>)`` or a re-introduction of
    the retired ``_is_close_post`` API. If a future edit brings either
    back, this test fails and the reviewer sees that dual management is
    back. AST walk means whitespace, line-breaks, and parenthesization
    cannot bypass the check the way a substring match could."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "src" / "magickit"
    source = (root / rel).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(root / rel))

    bool_hits = _find_bool_of_closes_thread(tree)
    assert not bool_hits, (
        f"{rel}:{bool_hits[0].lineno} contains a bool(<expr with "
        "closes_thread>) — that's the very dual-management seam "
        "T-close-detection-truthiness-seam removed. Call _classify_closes "
        "on the raw ingress instead and read ``.is_close`` off the "
        "returned _CloseClassification."
    )

    icp_hits = _find_is_close_post_calls(tree)
    assert not icp_hits, (
        f"{rel}:{icp_hits[0].lineno} contains an _is_close_post( call — "
        "that predicate was retired in msg-784 because its ``str`` "
        "argument was an authorization footgun. Call _classify_closes "
        "on the raw ingress instead and read ``.is_close`` / "
        "``.closes_thread_norm`` off the returned _CloseClassification."
    )


def test_ast_pin_helpers_are_wired_correctly() -> None:
    """Self-test for the AST helpers themselves so a bug in the pin
    cannot silently render it inert. If a future edit breaks the AST
    walker (e.g., accidentally matches ``Attribute`` instead of
    ``Name`` for ``bool``), the pin above would go quiet without this
    positive test firing.

    Also demonstrates the whitespace/formatting robustness the PR-gate
    #84 advisory asked for: the same seam expressed three different
    ways all get detected."""
    src = (
        "def f(closes_thread):\n"
        "    a = bool(closes_thread)\n"                 # canonical form
        "    b = bool( closes_thread )\n"               # spaces inside call
        "    c = bool(closes_thread or None)\n"         # expr, not just Name
        "    d = _is_close_post('decide', closes_thread)\n"
        "    return a, b, c, d\n"
    )
    tree = ast.parse(src)
    assert len(_find_bool_of_closes_thread(tree)) == 3
    assert len(_find_is_close_post_calls(tree)) == 1

    # And a negative case: an unrelated bool() on a different Name
    # must NOT match — the pin is scoped to closes_thread specifically,
    # not to every use of bool() in the module.
    clean_src = "def f(x):\n    return bool(x)\n"
    assert _find_bool_of_closes_thread(ast.parse(clean_src)) == []
    assert _find_is_close_post_calls(ast.parse(clean_src)) == []
