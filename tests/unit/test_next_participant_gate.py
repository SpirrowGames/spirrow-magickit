"""Unit tests for the structured handoff-target gate (next_participant).

Spec: T-handoff-target-structured-field msg-068 §4-1 (the field itself),
msg-072 §1 / §3 (Magickit is the sole enforcement point, existence-only
check, unregistered → fail-fast reject, lookup-unavailable → distinct
error type, ``_lookup_identity`` is REUSED not reimplemented), msg-078 §3
+ Einstein's final review (``none`` is NOT a special-cased reserved word:
it is simply an unknown identity name and the gate refuses it as such).

The gate lives at Magickit because Magickit is the only component that
knows the identity registry: Prismind holds it and Conclair persists a
nullable text column that carries a Conclair-side ``closes_thread``
invariant but does no cross-service validation. See the section comment
above ``_check_next_participant`` for the placement rationale.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from magickit.config import Settings
from magickit.main import create_app
from magickit.mcp.tools import chatroom as chatroom_tools


PROJECT = "spirrow-magickit"
THREAD = "T-x"


# ---- MCP-tool wiring, mirrored from test_role_gate.py -----------------


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


def _identity_response(*, name: str, allowed_roles: list[str] | None = None) -> dict:
    """A well-formed ``get_identity`` positive response.

    ``allowed_roles`` defaults to ``[]`` because the next_participant gate
    is not supposed to read it -- it is included solely so the response
    satisfies the ``_lookup_identity`` contract check (otherwise the
    lookup would fail-closed as "malformed", masking the actual verdict).
    """
    return {
        "success": True,
        "found": True,
        "identity": {
            "identity_name": name,
            "user": "sgadmin",
            "allowed_roles": allowed_roles if allowed_roles is not None else [],
            "independence_class": "cooperative",
            "persona_description": "",
        },
        "message": "ok",
    }


_UNREGISTERED = {
    "success": True, "found": False, "identity": None, "message": "none",
}


@pytest.fixture
def settings() -> Settings:
    return Settings(
        conclair_url="http://localhost:8115",
        conclair_timeout=5.0,
        prismind_url="http://localhost:8002",
        prismind_timeout=5.0,
    )


@pytest.fixture
def chatroom_adapter() -> MagicMock:
    adapter = MagicMock()
    adapter.open_thread = AsyncMock(return_value={"thread": {}, "msg": {}})
    adapter.post_message = AsyncMock(
        return_value={"msg": {}, "thread_status_changed_to": None}
    )
    adapter.close_thread = AsyncMock(return_value={"thread": {}, "decide_msg": {}})
    adapter.get_thread = AsyncMock(
        return_value={"thread": {"tags": []}, "messages": [], "mode": "full"}
    )
    adapter.close = AsyncMock()
    return adapter


@pytest.fixture
def prismind_adapter() -> MagicMock:
    adapter = MagicMock()
    # Default: whoever is looked up is unregistered. Individual tests
    # override with ``side_effect`` when they need per-name answers.
    adapter.get_identity = AsyncMock(return_value=_UNREGISTERED)
    return adapter


@pytest.fixture
def wired(settings: Settings, chatroom_adapter: MagicMock, prismind_adapter: MagicMock):
    tools = _capture_tools(settings)
    with (
        patch.object(chatroom_tools, "_adapter", return_value=chatroom_adapter),
        patch.object(
            chatroom_tools, "_prismind_adapter", return_value=prismind_adapter
        ),
    ):
        yield tools, chatroom_adapter, prismind_adapter


def _by_name(mapping: dict[str, dict]):
    """Build a ``get_identity`` ``side_effect`` that answers per-name.

    Missing names fall through to ``_UNREGISTERED`` so a test that only
    lists the *registered* actors still models Prismind honestly.
    """

    async def _fn(*, identity_name: str, **_: Any) -> dict:
        return mapping.get(identity_name, _UNREGISTERED)

    return _fn


# ---- omission: current behaviour, no lookup ---------------------------


@pytest.mark.asyncio
async def test_next_participant_omitted_skips_lookup_and_forwards_null(wired) -> None:
    """The whole feature is opt-in on the caller side (msg-068 §6): a caller
    that does not supply the field must not depend on Prismind being up, and
    the field must be sent to Conclair as null so a stale-schema receipt at
    the DB level is indistinguishable from a legacy post."""
    tools, chat, prismind = wired

    await tools["chatroom_post_message"](
        project="p", thread_id="T-1", msg_type="report", author="Bohr",
        content="c",
    )

    prismind.get_identity.assert_not_awaited()
    chat.post_message.assert_awaited_once()
    assert chat.post_message.call_args.kwargs["next_participant"] is None


@pytest.mark.asyncio
async def test_omit_on_open_thread_also_skips_lookup(wired) -> None:
    tools, chat, prismind = wired

    await tools["chatroom_open_thread"](
        project="p", thread_id="T-1", title="t", owner="Bohr",
        propose_content="hi",
    )

    prismind.get_identity.assert_not_awaited()
    chat.open_thread.assert_awaited_once()
    assert chat.open_thread.call_args.kwargs["next_participant"] is None


# ---- happy path: registered target is forwarded -----------------------


@pytest.mark.asyncio
async def test_registered_target_is_validated_and_forwarded(wired) -> None:
    """msg-068 §4-1. The value reaches Conclair unchanged after passing the
    identity-registry check."""
    tools, chat, prismind = wired
    prismind.get_identity = AsyncMock(side_effect=_by_name({
        "Heisenberg": _identity_response(name="Heisenberg"),
    }))

    result = await tools["chatroom_post_message"](
        project="p", thread_id="T-1", msg_type="handoff", author="Bohr",
        content="over to you", embodiment="web_ai_chat",
        next_participant="Heisenberg",
    )

    assert "error_type" not in result
    assert prismind.get_identity.call_args.kwargs["identity_name"] == "Heisenberg"
    assert chat.post_message.call_args.kwargs["next_participant"] == "Heisenberg"


@pytest.mark.asyncio
async def test_registered_target_on_open_thread(wired) -> None:
    tools, chat, prismind = wired
    prismind.get_identity = AsyncMock(side_effect=_by_name({
        "Einstein": _identity_response(name="Einstein"),
    }))

    result = await tools["chatroom_open_thread"](
        project="p", thread_id="T-1", title="t", owner="Bohr",
        propose_content="over to you", next_participant="Einstein",
    )

    assert "error_type" not in result
    assert chat.open_thread.call_args.kwargs["next_participant"] == "Einstein"


# ---- fail-fast: unknown target ----------------------------------------


@pytest.mark.asyncio
async def test_unknown_target_is_rejected_before_any_write(wired) -> None:
    """msg-072 §3. An unregistered target must be refused before any
    Conclair write, so a typo (``Einstien`` for ``Einstein``) does not
    leak into the routing state the whole feature exists to make
    trustworthy. The distinction from the role gate (which passes
    unregistered *authors* through) is deliberate -- next_participant is
    a new field with no legacy caller."""
    tools, chat, prismind = wired
    # Any lookup returns _UNREGISTERED via the default fixture.

    result = await tools["chatroom_post_message"](
        project="p", thread_id="T-1", msg_type="handoff", author="Bohr",
        content="c", embodiment="web_ai_chat", next_participant="Einstien",
    )

    assert result["error_type"] == "NextParticipantUnknownError"
    assert result["details"]["next_participant"] == "Einstien"
    chat.post_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_target_blocks_open_thread(wired) -> None:
    tools, chat, prismind = wired

    result = await tools["chatroom_open_thread"](
        project="p", thread_id="T-1", title="t", owner="Bohr",
        propose_content="c", next_participant="ghost",
    )

    assert result["error_type"] == "NextParticipantUnknownError"
    chat.open_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_reserved_token_none_is_not_special_cased(wired) -> None:
    """msg-078 §3 + Einstein's final review picked (a): the string
    ``none`` is not a reserved word in this layer. It goes through the
    same identity-registry check as anything else, and since no identity
    is registered under that name it is refused as unknown. Terminal
    state is expressed by closing the thread, not by a token this gate
    would have to recognise (msg-072 §2)."""
    tools, chat, prismind = wired

    result = await tools["chatroom_post_message"](
        project="p", thread_id="T-1", msg_type="handoff", author="Bohr",
        content="c", embodiment="web_ai_chat", next_participant="none",
    )

    assert result["error_type"] == "NextParticipantUnknownError"
    chat.post_message.assert_not_awaited()


# ---- fail-closed: lookup unavailable ----------------------------------


@pytest.mark.asyncio
async def test_lookup_transport_failure_blocks_with_distinct_error(wired) -> None:
    """msg-072 §3. An unusable lookup fails closed, and with a *distinct*
    error_type so a caller can branch on it without parsing text. The
    escape hatch (msg-070 §3) is real: omitting the field lands the
    message and lets the consumer fall back on its body-line parser."""
    tools, chat, prismind = wired
    prismind.get_identity = AsyncMock(side_effect=RuntimeError("connection refused"))

    result = await tools["chatroom_post_message"](
        project="p", thread_id="T-1", msg_type="handoff", author="Bohr",
        content="c", embodiment="web_ai_chat", next_participant="Heisenberg",
    )

    assert result["error_type"] == "NextParticipantValidationUnavailableError"
    assert "connection refused" in result["details"]["reason"]
    chat.post_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_lookup_upstream_error_envelope_blocks(wired) -> None:
    """A Prismind that rejects the call (e.g. too old to have get_identity)
    is a failed lookup, not a permissive verdict -- same treatment as the
    role gate's ``test_lookup_error_envelope_blocks_the_write``."""
    tools, chat, prismind = wired
    prismind.get_identity.return_value = {
        "error_type": "UpstreamValidationError",
        "error": "Unknown tool: get_identity",
        "details": {},
    }

    result = await tools["chatroom_post_message"](
        project="p", thread_id="T-1", msg_type="handoff", author="Bohr",
        content="c", embodiment="web_ai_chat", next_participant="Heisenberg",
    )

    assert result["error_type"] == "NextParticipantValidationUnavailableError"
    assert "Unknown tool" in result["details"]["reason"]
    chat.post_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_malformed_success_response_blocks(wired) -> None:
    """msg-072 §3 requirement: the ``_lookup_identity`` contract check is
    exercised here because it is REUSED, not reimplemented. A success
    response missing ``found`` must not skip the gate -- msg-044 §6.4 is
    the shape of failure this reuse exists to avoid."""
    tools, chat, prismind = wired
    prismind.get_identity.return_value = {
        # `found` deliberately omitted.
        "success": True,
        "identity": None,
        "message": "shape drift",
    }

    result = await tools["chatroom_post_message"](
        project="p", thread_id="T-1", msg_type="handoff", author="Bohr",
        content="c", embodiment="web_ai_chat", next_participant="Heisenberg",
    )

    assert result["error_type"] == "NextParticipantValidationUnavailableError"
    chat.post_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_malformed_allowed_roles_still_blocks(wired) -> None:
    """The contract check is exercised on the nested field too. A bare
    string for ``allowed_roles`` used to grant the role ``'n'`` under the
    coercion bug msg-048 caught; here it is reachable only because we
    reuse ``_lookup_identity`` and that function refuses to coerce."""
    tools, chat, prismind = wired
    prismind.get_identity.return_value = {
        "success": True,
        "found": True,
        "identity": {
            "identity_name": "Heisenberg",
            "user": "sgadmin",
            # non-list, contract-violating value.
            "allowed_roles": "naysayer",
            "independence_class": "cooperative",
            "persona_description": "",
        },
        "message": "ok",
    }

    result = await tools["chatroom_post_message"](
        project="p", thread_id="T-1", msg_type="handoff", author="Bohr",
        content="c", embodiment="web_ai_chat", next_participant="Heisenberg",
    )

    assert result["error_type"] == "NextParticipantValidationUnavailableError"
    chat.post_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_escape_hatch_omit_field_succeeds_on_outage(wired) -> None:
    """msg-070 §3 + msg-072 §3: fail-closed with a real remedy. Omitting
    the field is not the same as the close-gate's "certain-to-fail
    retry" (msg-041 Q3) -- it lands the write with next_participant null
    and lets the consumer fall back on its body-line parser."""
    tools, chat, prismind = wired
    prismind.get_identity = AsyncMock(side_effect=RuntimeError("down"))

    result = await tools["chatroom_post_message"](
        project="p", thread_id="T-1", msg_type="report", author="Bohr",
        content="c",
    )

    assert "error_type" not in result
    chat.post_message.assert_awaited_once()
    assert chat.post_message.call_args.kwargs["next_participant"] is None


# ---- ``human`` passes without special-case code -----------------------


@pytest.mark.asyncio
async def test_human_target_passes_via_the_identity_registry(wired) -> None:
    """msg-072 §2. The rule is one sentence: ``next_participant`` must be
    a registered identity. ``human`` is registered
    (``allowed_roles=["human"]``, verified 2026-08-02); it passes without
    any special-case branch. This test is the falsifier for "someone
    added a reserved-word list": if a synonym set were introduced
    ``human`` would pass without the lookup, and this asserts the
    opposite -- Prismind IS consulted for it."""
    tools, chat, prismind = wired
    prismind.get_identity = AsyncMock(side_effect=_by_name({
        "human": _identity_response(name="human", allowed_roles=["human"]),
    }))

    result = await tools["chatroom_post_message"](
        project="p", thread_id="T-1", msg_type="handoff", author="Bohr",
        content="please decide", embodiment="web_ai_chat",
        next_participant="human",
    )

    assert "error_type" not in result
    assert prismind.get_identity.call_args.kwargs["identity_name"] == "human"
    assert chat.post_message.call_args.kwargs["next_participant"] == "human"


@pytest.mark.asyncio
async def test_target_roles_are_not_required(wired) -> None:
    """msg-072 §3: existence-only. An identity with ``allowed_roles=[]``
    (a legal declaration on Prismind) is a valid target -- the gate does
    not require the target to be able to *act* under any particular
    role, only to exist. Falsifies "we accidentally applied the role
    gate to next_participant too"."""
    tools, chat, prismind = wired
    prismind.get_identity = AsyncMock(side_effect=_by_name({
        "reader-only": _identity_response(name="reader-only", allowed_roles=[]),
    }))

    result = await tools["chatroom_post_message"](
        project="p", thread_id="T-1", msg_type="handoff", author="Bohr",
        content="c", embodiment="web_ai_chat", next_participant="reader-only",
    )

    assert "error_type" not in result
    chat.post_message.assert_awaited_once()


# ---- ordering / non-interference with the role gate -------------------


@pytest.mark.asyncio
async def test_role_gate_rejection_precedes_next_participant_lookup(wired) -> None:
    """The role gate fires first (see the placement in
    chatroom_post_message). A rejection there costs no Prismind lookup
    for next_participant -- both are fail-fast, but reporting the role
    problem the caller can actually fix comes first."""
    tools, chat, prismind = wired
    prismind.get_identity = AsyncMock(side_effect=_by_name({
        "Einstein": _identity_response(name="Einstein", allowed_roles=["naysayer"]),
        "Heisenberg": _identity_response(name="Heisenberg"),
    }))

    result = await tools["chatroom_post_message"](
        project="p", thread_id="T-1", msg_type="report", author="Einstein",
        content="c", role="implementer", next_participant="Heisenberg",
    )

    assert result["error_type"] == "RoleNotAllowed"
    chat.post_message.assert_not_awaited()
    # Only one lookup -- the role gate's -- reached Prismind.
    prismind.get_identity.assert_awaited_once()
    assert prismind.get_identity.call_args.kwargs["identity_name"] == "Einstein"


@pytest.mark.asyncio
async def test_embodiment_check_still_precedes_lookup(wired) -> None:
    """The pure parameter check runs before any network round-trip -- the
    same discipline the role gate follows."""
    tools, chat, prismind = wired
    prismind.get_identity = AsyncMock(side_effect=_by_name({
        "Heisenberg": _identity_response(name="Heisenberg"),
    }))

    result = await tools["chatroom_post_message"](
        project="p", thread_id="T-1", msg_type="handoff", author="Bohr",
        content="c", next_participant="Heisenberg",
    )

    assert result["error_type"] == "EmbodimentRequiredError"
    prismind.get_identity.assert_not_awaited()


# ---- language / vocabulary hygiene ------------------------------------
#
# msg-072 §1 explicitly forbids Magickit and Conclair from knowing the
# consumer's routing syntax. The gate here validates an identity name and
# nothing else -- no reserved words, no sentinel line syntax.
#
# The check is spelled as a repository grep rather than a per-file line
# scan so a future refactor cannot smuggle the vocabulary in by moving
# the reference to a sibling file. Only the code paths that
# ``chatroom.py`` (adapter + tool + UI) participates in are covered; a
# doc/comment mentioning ``NEXT:`` is not the bug this test exists to
# catch (the bug is *executing* code that recognises it, or *storing*
# ``"none"`` as a sentinel).


_MAGICKIT_ENFORCEMENT_FILES = (
    "src/magickit/mcp/tools/chatroom.py",
    "src/magickit/adapters/chatroom.py",
    "src/magickit/web/chatroom_writes.py",
)


def _repo_root() -> Path:
    """Locate the repo root by walking up from this test file."""
    here = Path(__file__).resolve()
    for candidate in [here, *here.parents]:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise RuntimeError("Could not locate repository root")


def _find_forbidden_literals(path: Path, needles: tuple[str, ...]) -> list[str]:
    """Return code lines (index + text) that mention any forbidden literal.

    Docstring / comment / raw text triple-quoted lines are excluded from
    the report: the ban is on *code that acts on* the vocabulary, not on
    prose that describes what the gate refuses. A cheap heuristic covers
    both single- and triple-quoted docstrings and comment lines.
    """
    hits: list[str] = []
    in_triple = False
    triple_quote: str | None = None
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw
        # Track triple-quoted string spans (docstrings / block strings).
        stripped = line.strip()
        if in_triple:
            assert triple_quote is not None
            if triple_quote in stripped:
                in_triple = False
                triple_quote = None
            continue
        for q in ('"""', "'''"):
            if stripped.count(q) == 1:
                in_triple = True
                triple_quote = q
                break
        if in_triple:
            continue
        # Skip pure comment lines.
        if stripped.startswith("#"):
            continue
        for needle in needles:
            if needle in line:
                hits.append(f"{path.name}:{lineno}: {raw.rstrip()}")
                break
    return hits


def test_magickit_code_does_not_mention_the_consumer_vocabulary() -> None:
    """msg-072 §1 + revised DoD 7 (msg-072): the strings ``NEXT:`` and the
    literal ``"none"`` must not appear in executable Magickit code
    (adapter, MCP tool, or UI write handler). Reserving a vocabulary in
    this layer would break the ownership boundary the design turns on."""
    root = _repo_root()
    forbidden: tuple[str, ...] = ("NEXT:", '"none"', "'none'")
    hits: list[str] = []
    for rel in _MAGICKIT_ENFORCEMENT_FILES:
        hits.extend(_find_forbidden_literals(root / rel, forbidden))
    assert hits == [], (
        "consumer-routing vocabulary must not appear in Magickit "
        "enforcement code (T-handoff-target-structured-field msg-072 §1 / "
        f"DoD 7). Offending lines:\n" + "\n".join(hits)
    )


# ---- UI-write parity --------------------------------------------------


@pytest.fixture
def _configured_settings(settings: Settings):
    """Gates read module-level settings; the FastAPI app binds them at startup."""
    chatroom_tools.configure(settings)
    yield settings
    chatroom_tools._settings = None


async def _ui_post(path: str, data: dict[str, str]) -> httpx.Response:
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver",
    ) as client:
        return await client.post(path, data=data)


@pytest.mark.asyncio
async def test_ui_post_message_rejects_unknown_target(_configured_settings) -> None:
    """The browser write path claims the same enforcement (see the module
    comment in ``chatroom_writes``). If this passed while the MCP tool
    rejected, the browser would silently become the bypass."""
    adapter = AsyncMock()

    async def _lookup_returns_unregistered(*, identity_name: str, **_: Any) -> dict:
        return _UNREGISTERED

    prismind = AsyncMock()
    prismind.get_identity = AsyncMock(side_effect=_lookup_returns_unregistered)

    with (
        patch.object(chatroom_tools, "_adapter", return_value=adapter),
        patch.object(chatroom_tools, "_prismind_adapter", return_value=prismind),
    ):
        response = await _ui_post(
            f"/ui/projects/{PROJECT}/threads/{THREAD}/messages",
            {
                "type": "handoff", "author": "Bohr", "content": "c",
                "embodiment": "web_ai_chat", "next_participant": "ghost",
            },
        )

    assert "NextParticipantUnknownError" in response.text
    adapter.post_message.assert_not_called()


@pytest.mark.asyncio
async def test_ui_open_thread_rejects_unknown_target(_configured_settings) -> None:
    adapter = AsyncMock()
    prismind = AsyncMock()
    prismind.get_identity = AsyncMock(return_value=_UNREGISTERED)

    with (
        patch.object(chatroom_tools, "_adapter", return_value=adapter),
        patch.object(chatroom_tools, "_prismind_adapter", return_value=prismind),
    ):
        response = await _ui_post(
            f"/ui/projects/{PROJECT}/threads",
            {
                "thread_id": THREAD, "title": "t", "owner": "Bohr",
                "propose_content": "c", "next_participant": "ghost",
            },
        )

    assert "NextParticipantUnknownError" in response.text
    adapter.open_thread.assert_not_called()


@pytest.mark.asyncio
async def test_ui_post_message_forwards_registered_target(_configured_settings) -> None:
    """Happy path on the UI: a registered target is validated and forwarded
    to Conclair via the adapter."""
    adapter = AsyncMock()
    adapter.post_message = AsyncMock(
        return_value={"msg": {"msg_id": "msg-1", "type": "handoff"}}
    )

    prismind = AsyncMock()
    prismind.get_identity = AsyncMock(
        return_value=_identity_response(name="Heisenberg")
    )

    with (
        patch.object(chatroom_tools, "_adapter", return_value=adapter),
        patch.object(chatroom_tools, "_prismind_adapter", return_value=prismind),
    ):
        response = await _ui_post(
            f"/ui/projects/{PROJECT}/threads/{THREAD}/messages",
            {
                "type": "handoff", "author": "Bohr", "content": "c",
                "embodiment": "web_ai_chat", "next_participant": "Heisenberg",
            },
        )

    assert response.status_code == 200
    assert "NextParticipantUnknownError" not in response.text
    adapter.post_message.assert_awaited_once()
    assert (
        adapter.post_message.call_args.kwargs["next_participant"] == "Heisenberg"
    )
