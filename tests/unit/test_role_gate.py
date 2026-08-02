"""Unit tests for the role x allowed_roles gate on the chatroom write paths.

Spec: T-magickit-identity-extension msg-002 §2.3 (placement / permissive
legacy semantics) and msg-017 §3 (invariants I-1..I-4 and the conditions
that falsify each). Each test below names the invariant it pins.

The gate lives at Magickit because Magickit is the only component that can
see both sides: Prismind holds the identity record (allowed_roles) and
Conclair holds the message (role). Neither validates -- msg-002 §2.2 (c).
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from magickit.config import Settings
from magickit.mcp.tools import chatroom as chatroom_tools


def _capture_tools(settings: Settings) -> dict[str, Any]:
    """Register chatroom tools and capture the wrapper fns by name."""
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


def _identity_response(allowed_roles: list[str], *, name: str = "Einstein") -> dict:
    """A successful get_identity response for a registered identity."""
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


_UNREGISTERED = {"success": True, "found": False, "identity": None, "message": "none"}


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
    adapter.get_identity = AsyncMock(return_value=_UNREGISTERED)
    return adapter


@pytest.fixture
def wired(settings: Settings, chatroom_adapter: MagicMock, prismind_adapter: MagicMock):
    """Tools with both the Conclair and Prismind adapters faked out."""
    tools = _capture_tools(settings)
    with (
        patch.object(chatroom_tools, "_adapter", return_value=chatroom_adapter),
        patch.object(
            chatroom_tools, "_prismind_adapter", return_value=prismind_adapter
        ),
    ):
        yield tools, chatroom_adapter, prismind_adapter


# ---- I-1: supply path -------------------------------------------------


@pytest.mark.asyncio
async def test_post_message_forwards_allowed_role_to_conclair(wired) -> None:
    """I-1: a validated role reaches Conclair (falsified if role is dropped)."""
    tools, chat, prismind = wired
    prismind.get_identity.return_value = _identity_response(
        ["proposer", "reviewer"], name="Bohr"
    )

    await tools["chatroom_post_message"](
        project="p", thread_id="T-1", msg_type="report", author="Bohr",
        content="c", role="proposer",
    )

    assert chat.post_message.call_args.kwargs["role"] == "proposer"


@pytest.mark.asyncio
async def test_open_thread_forwards_role_validated_against_owner(wired) -> None:
    """I-1: open_thread's propose msg is authored by ``owner``."""
    tools, chat, prismind = wired
    prismind.get_identity.return_value = _identity_response(["proposer"], name="Bohr")

    await tools["chatroom_open_thread"](
        project="p", thread_id="T-1", title="t", owner="Bohr",
        propose_content="hi", role="proposer",
    )

    assert prismind.get_identity.call_args.kwargs["identity_name"] == "Bohr"
    assert chat.open_thread.call_args.kwargs["role"] == "proposer"


@pytest.mark.asyncio
async def test_close_thread_forwards_role(wired) -> None:
    """I-1 + I-4: the emitted decide carries the validated role."""
    tools, chat, prismind = wired
    prismind.get_identity.return_value = _identity_response(
        ["implementer"], name="Heisenberg"
    )

    await tools["chatroom_close_thread"](
        project="p", thread_id="T-1", summary_content="done", author="Heisenberg",
        embodiment="terminal_coding_agent", role="implementer",
    )

    assert chat.close_thread.call_args.kwargs["role"] == "implementer"


# ---- I-2: the gate actually fires -------------------------------------


@pytest.mark.asyncio
async def test_disallowed_role_is_rejected_and_nothing_is_written(wired) -> None:
    """I-2, verbatim falsification case from msg-017.

    ``Einstein`` is registered with ``allowed_roles=["naysayer"]``. Posting
    as ``implementer`` must return the ``RoleNotAllowed`` envelope AND must
    not create a message -- msg-017 names both halves ("reject ... and do
    not write to Conclair"); a reject that still wrote would be RED.
    """
    tools, chat, prismind = wired
    prismind.get_identity.return_value = _identity_response(["naysayer"])

    result = await tools["chatroom_post_message"](
        project="p", thread_id="T-1", msg_type="report", author="Einstein",
        content="c", role="implementer",
    )

    assert result["error_type"] == "RoleNotAllowed"
    assert result["details"]["author"] == "Einstein"
    assert result["details"]["role"] == "implementer"
    assert result["details"]["allowed_roles"] == ["naysayer"]
    chat.post_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_disallowed_role_blocks_close(wired) -> None:
    """I-4: close must not be a bypass of the gate."""
    tools, chat, prismind = wired
    prismind.get_identity.return_value = _identity_response(["naysayer"])

    result = await tools["chatroom_close_thread"](
        project="p", thread_id="T-1", summary_content="done", author="Einstein",
        embodiment="terminal_coding_agent", role="implementer",
    )

    assert result["error_type"] == "RoleNotAllowed"
    chat.close_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_disallowed_role_blocks_open_thread(wired) -> None:
    tools, chat, prismind = wired
    prismind.get_identity.return_value = _identity_response(["naysayer"])

    result = await tools["chatroom_open_thread"](
        project="p", thread_id="T-1", title="t", owner="Einstein",
        propose_content="hi", role="proposer",
    )

    assert result["error_type"] == "RoleNotAllowed"
    chat.open_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_allowed_roles_rejects_every_role(wired) -> None:
    """`allowed_roles=[]` is a legal declaration of "no roles" (documented on
    upsert_identity) and must behave as such, not as "unset -> allow"."""
    tools, chat, prismind = wired
    prismind.get_identity.return_value = _identity_response([])

    result = await tools["chatroom_post_message"](
        project="p", thread_id="T-1", msg_type="report", author="Einstein",
        content="c", role="naysayer",
    )

    assert result["error_type"] == "RoleNotAllowed"
    chat.post_message.assert_not_awaited()


# ---- I-3: legacy compatibility ----------------------------------------


@pytest.mark.asyncio
async def test_role_omitted_is_allowed_and_skips_lookup(wired) -> None:
    """I-3: omitting role must not reject (that would break every existing
    caller), and must not even consult Prismind -- an unrelated post should
    not depend on the identity service being up."""
    tools, chat, prismind = wired

    await tools["chatroom_post_message"](
        project="p", thread_id="T-1", msg_type="report", author="Einstein",
        content="c",
    )

    prismind.get_identity.assert_not_awaited()
    chat.post_message.assert_awaited_once()
    assert chat.post_message.call_args.kwargs["role"] is None


@pytest.mark.asyncio
async def test_unregistered_identity_is_allowed_any_role(wired) -> None:
    """I-3: an author with no identity record is a legacy actor -> skip."""
    tools, chat, prismind = wired
    prismind.get_identity.return_value = _UNREGISTERED

    await tools["chatroom_post_message"](
        project="p", thread_id="T-1", msg_type="report", author="claude-code",
        content="c", role="anything",
    )

    chat.post_message.assert_awaited_once()
    assert chat.post_message.call_args.kwargs["role"] == "anything"


@pytest.mark.asyncio
async def test_role_omitted_close_thread_skips_lookup(wired) -> None:
    tools, chat, prismind = wired

    await tools["chatroom_close_thread"](
        project="p", thread_id="T-1", summary_content="done", author="Einstein",
        embodiment="terminal_coding_agent",
    )

    prismind.get_identity.assert_not_awaited()
    chat.close_thread.assert_awaited_once()
    assert chat.close_thread.call_args.kwargs["role"] is None


# ---- fail-closed on an unusable verdict -------------------------------


@pytest.mark.asyncio
async def test_lookup_transport_failure_blocks_the_write(wired) -> None:
    """A role that cannot be validated must not be recorded as if it were.

    Falling through on error would put unverified values in
    ``messages.role`` that are indistinguishable from verified ones --
    the "shipped but never armed" failure msg-017 §2 describes.
    """
    tools, chat, prismind = wired
    prismind.get_identity = AsyncMock(side_effect=RuntimeError("connection refused"))

    result = await tools["chatroom_post_message"](
        project="p", thread_id="T-1", msg_type="report", author="Einstein",
        content="c", role="naysayer",
    )

    assert result["error_type"] == "RoleValidationUnavailableError"
    assert "connection refused" in result["details"]["reason"]
    chat.post_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_lookup_error_envelope_blocks_the_write(wired) -> None:
    """An upstream MCP rejection (e.g. Prismind too old to have the tool)
    is a failed lookup, not a permissive verdict."""
    tools, chat, prismind = wired
    prismind.get_identity.return_value = {
        "error_type": "UpstreamValidationError",
        "error": "Unknown tool: get_identity",
        "details": {},
    }

    result = await tools["chatroom_post_message"](
        project="p", thread_id="T-1", msg_type="report", author="Einstein",
        content="c", role="naysayer",
    )

    assert result["error_type"] == "RoleValidationUnavailableError"
    assert "Unknown tool" in result["details"]["reason"]
    chat.post_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_lookup_success_false_blocks_the_write(wired) -> None:
    """success=False means "could not answer" and must fail closed;
    "registered? no" is carried by found=False, which does not."""
    tools, chat, prismind = wired
    prismind.get_identity.return_value = {
        "success": False, "found": False, "identity": None, "message": "boom",
    }

    result = await tools["chatroom_post_message"](
        project="p", thread_id="T-1", msg_type="report", author="Einstein",
        content="c", role="naysayer",
    )

    assert result["error_type"] == "RoleValidationUnavailableError"
    chat.post_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_lookup_failure_does_not_block_when_role_omitted(wired) -> None:
    """The escape hatch: a caller that cannot reach Prismind can still post
    by omitting role, which records role=null (honestly unverified)."""
    tools, chat, prismind = wired
    prismind.get_identity = AsyncMock(side_effect=RuntimeError("down"))

    await tools["chatroom_post_message"](
        project="p", thread_id="T-1", msg_type="report", author="Einstein",
        content="c",
    )

    chat.post_message.assert_awaited_once()


# ---- ordering ---------------------------------------------------------


@pytest.mark.asyncio
async def test_embodiment_check_precedes_role_lookup(wired) -> None:
    """The free parameter check runs before the network round-trip."""
    tools, chat, prismind = wired
    prismind.get_identity.return_value = _identity_response(["naysayer"])

    result = await tools["chatroom_post_message"](
        project="p", thread_id="T-1", msg_type="handoff", author="Einstein",
        content="c", role="implementer",
    )

    assert result["error_type"] == "EmbodimentRequiredError"
    prismind.get_identity.assert_not_awaited()


@pytest.mark.asyncio
async def test_role_gate_precedes_the_naysayer_gate_read(wired) -> None:
    """A rejected role costs no Conclair read: the gate short-circuits
    before the close-policy path fetches the thread."""
    tools, chat, prismind = wired
    prismind.get_identity.return_value = _identity_response(["naysayer"])

    result = await tools["chatroom_post_message"](
        project="p", thread_id="T-1", msg_type="decide", author="Einstein",
        content="c", closes_thread="T-1", embodiment="web_ai_chat",
        role="implementer",
    )

    assert result["error_type"] == "RoleNotAllowed"
    chat.get_thread.assert_not_awaited()
    chat.post_message.assert_not_awaited()


# ---- human identity ---------------------------------------------------


@pytest.mark.asyncio
async def test_human_is_not_exempt_from_the_role_gate(wired) -> None:
    """Unlike the embodiment rule, role has no human exemption.

    ``human`` is registered with ``allowed_roles=["human"]``, so it is
    checked like anyone else. msg-002 §2.3 / msg-017 grant an exemption to
    *unregistered* identities only, and human is registered (msg-015).
    """
    tools, chat, prismind = wired
    prismind.get_identity.return_value = _identity_response(["human"], name="human")

    result = await tools["chatroom_post_message"](
        project="p", thread_id="T-1", msg_type="report", author="human",
        content="c", role="proposer",
    )

    assert result["error_type"] == "RoleNotAllowed"
    chat.post_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_human_own_role_passes(wired) -> None:
    tools, chat, prismind = wired
    prismind.get_identity.return_value = _identity_response(["human"], name="human")

    await tools["chatroom_post_message"](
        project="p", thread_id="T-1", msg_type="report", author="human",
        content="c", role="human",
    )

    assert chat.post_message.call_args.kwargs["role"] == "human"


# ---- P3 boundary ------------------------------------------------------


@pytest.mark.asyncio
async def test_closeable_roles_second_stage_is_not_implemented(wired) -> None:
    """Scope pin (msg-017 I-4): P2 is role x allowed_roles ONLY.

    ``closeable_roles = {implementer, integrator, proposer}`` is P3. A
    naysayer-only identity closing under its own allowed role must still
    pass here -- if a future change starts rejecting this, that is P3
    landing, and it should land deliberately rather than by drift.
    """
    tools, chat, prismind = wired
    prismind.get_identity.return_value = _identity_response(["naysayer"])

    result = await tools["chatroom_close_thread"](
        project="p", thread_id="T-1", summary_content="done", author="Einstein",
        embodiment="web_ai_chat", role="naysayer",
    )

    assert "error_type" not in result
    chat.close_thread.assert_awaited_once()


# ---- adapter lifetimes (T-pr-review-11 msg-020 rebuttal) --------------
#
# The naysayer read the missing `await prismind.close()` in
# `_check_role_allowed` as a connection leak, because every write path in
# chatroom.py wraps its *Conclair* adapter in `try/finally: await
# adapter.close()`. The asymmetry is intentional: the two adapters have
# different base classes and different lifetimes. These tests pin the
# distinction so the "fix" cannot be reintroduced as a regression.


def test_http_adapter_owns_a_client_and_must_be_closed() -> None:
    """ChatroomAdapter holds an httpx client across calls -> close() is real."""
    from magickit.adapters.base import BaseAdapter
    from magickit.adapters.chatroom import ChatroomAdapter

    adapter = ChatroomAdapter(base_url="http://localhost:8115", timeout=5.0)

    # A resource slot exists on the instance, and close() is a real method
    # defined in the MRO (not synthesised by attribute lookup).
    assert "_client" in adapter.__dict__
    assert any("close" in klass.__dict__ for klass in BaseAdapter.__mro__)
    assert inspect.iscoroutinefunction(type(adapter).close)


def test_mcp_adapter_holds_no_connection_to_leak() -> None:
    """PrismindAdapter construction opens nothing: two scalars, no client."""
    from magickit.adapters.prismind import PrismindAdapter

    adapter = PrismindAdapter(sse_url="http://localhost:8002", timeout=5.0)

    assert set(adapter.__dict__) == {"sse_url", "timeout"}
    assert isinstance(adapter.sse_url, str)
    assert isinstance(adapter.timeout, float)


def test_mcp_adapter_has_no_close_to_call() -> None:
    """`await prismind.close()` would be an MCP tool call, not a cleanup.

    MCPBaseAdapter defines no close() anywhere in its MRO and routes unknown
    attributes through __getattr__ to dynamic tool dispatch. Following the
    Conclair pattern here would therefore issue `call_tool("close", {})` --
    a whole extra SSE connect + initialize + Unknown-tool round trip per
    validated post. That is the regression this test exists to block.
    """
    from magickit.adapters.mcp_base import MCPBaseAdapter
    from magickit.adapters.prismind import PrismindAdapter

    assert not any("close" in klass.__dict__ for klass in MCPBaseAdapter.__mro__)

    adapter = PrismindAdapter(sse_url="http://localhost:8002", timeout=5.0)
    assert not inspect.ismethod(adapter.close)  # synthesised, not a real method

    calls: list[tuple[str, dict]] = []

    async def spy(name: str, arguments: dict):
        calls.append((name, arguments))

    adapter.call_tool = spy  # type: ignore[method-assign]
    asyncio.run(adapter.close())

    assert calls == [("close", {})]


@pytest.mark.asyncio
async def test_role_gate_does_not_close_the_prismind_adapter(wired) -> None:
    """The gate performs exactly one lookup and no lifecycle call.

    `prismind_adapter` is a bare MagicMock, so `await adapter.close()` in
    production would raise (`MagicMock` is not awaitable) -- the assertion
    below makes the intent explicit rather than relying on that side effect.
    """
    tools, chat, prismind = wired
    prismind.get_identity.return_value = _identity_response(["proposer"], name="Bohr")

    await tools["chatroom_post_message"](
        project="p", thread_id="T-1", msg_type="report", author="Bohr",
        content="c", role="proposer",
    )

    prismind.get_identity.assert_awaited_once()
    assert "close" not in {name for name, _, _ in prismind.mock_calls}
    # The Conclair adapter, by contrast, IS closed on every write path.
    chat.close.assert_awaited_once()
