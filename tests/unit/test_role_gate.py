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
async def test_unregistered_identity_posts_but_its_role_is_not_recorded(wired) -> None:
    """I-3 + the invariant, in the one place they pull against each other.

    An author with no identity record is a legacy actor, so the *message*
    must not be refused. But its role was never validated, so recording it
    would create the third state the invariant rules out -- and one reachable
    by simply choosing an unregistered author name, which would leave the
    gate binding only the identities that cooperate. Post: yes. Role: null.

    Falsified if the message is rejected, or if "anything" reaches Conclair.
    """
    tools, chat, prismind = wired
    prismind.get_identity.return_value = _UNREGISTERED

    result = await tools["chatroom_post_message"](
        project="p", thread_id="T-1", msg_type="report", author="claude-code",
        content="c", role="anything",
    )

    assert "error_type" not in result
    chat.post_message.assert_awaited_once()
    assert chat.post_message.call_args.kwargs["role"] is None


@pytest.mark.asyncio
async def test_unregistered_identity_role_is_not_recorded_on_open_thread(wired) -> None:
    """Same rule on the open path: the thread opens, the role does not stick."""
    tools, chat, prismind = wired
    prismind.get_identity.return_value = _UNREGISTERED

    result = await tools["chatroom_open_thread"](
        project="p", thread_id="T-1", title="t", owner="claude-code",
        propose_content="hi", role="proposer",
    )

    assert "error_type" not in result
    chat.open_thread.assert_awaited_once()
    assert chat.open_thread.call_args.kwargs["role"] is None


@pytest.mark.asyncio
async def test_unregistered_identity_role_is_not_recorded_on_close(wired) -> None:
    """Same rule on the close path -- otherwise close is the way around it."""
    tools, chat, prismind = wired
    prismind.get_identity.return_value = _UNREGISTERED

    result = await tools["chatroom_close_thread"](
        project="p", thread_id="T-1", summary_content="done", author="claude-code",
        embodiment="terminal_coding_agent", role="naysayer",
    )

    assert "error_type" not in result
    chat.close_thread.assert_awaited_once()
    assert chat.close_thread.call_args.kwargs["role"] is None


@pytest.mark.asyncio
async def test_partition_drift_shows_up_as_missing_roles_not_as_unverified_ones(
    wired,
) -> None:
    """The failure mode Bohr's post-deploy live check cannot catch.

    If Prismind is restarted resolving a different ``user_name``, every
    identity lookup answers found=false -- including for main-chain actors.
    Under a pass-through rule that degrades silently to "everything is
    allowed" while the data still looks verified. Under this rule the roles
    stop being written, so the drift is visible in the thread itself: it is
    exactly I-6's falsification condition (post-merge Bohr / Heisenberg /
    Einstein posts carry a non-null role) going red.
    """
    tools, chat, prismind = wired
    prismind.get_identity.return_value = _UNREGISTERED  # empty partition

    await tools["chatroom_post_message"](
        project="p", thread_id="T-1", msg_type="report", author="Einstein",
        content="c", role="naysayer",
    )

    chat.post_message.assert_awaited_once()
    assert chat.post_message.call_args.kwargs["role"] is None


@pytest.mark.asyncio
async def test_role_omitted_close_still_looks_the_identity_up(wired) -> None:
    """P3 changes this deliberately; recorded here rather than silently.

    Under P2 a close with no ``role`` consulted Prismind not at all. The
    second stage (I-7) does not read ``role``, so it has to look the record
    up regardless -- otherwise omitting ``role`` would be the bypass. The
    no-lookup property survives untouched on the post / open paths, which are
    not closes (``test_role_omitted_is_allowed_and_skips_lookup``).

    The recording half of I-3 is unchanged: no role supplied, no role stored.
    """
    tools, chat, prismind = wired
    prismind.get_identity.return_value = _UNREGISTERED

    await tools["chatroom_close_thread"](
        project="p", thread_id="T-1", summary_content="done", author="claude-code",
        embodiment="terminal_coding_agent",
    )

    prismind.get_identity.assert_awaited_once()
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


# ---- P3: closeable_roles, the second stage of the close check ---------
#
# Spec: msg-002 §3.1 / §3.2 (decided form), msg-037 §3 (I-7..I-11).
# Stage 1 in that numbering is the *owner* check and lives in Conclair; this
# stage runs in Magickit, before Conclair is contacted at all.


def test_closeable_roles_is_the_decided_set() -> None:
    """msg-003 D-3 / msg-005. Pinned as a set, not as behaviour, so a silent
    widening (e.g. adding "human" instead of exempting it) shows up here."""
    assert set(chatroom_tools.CLOSEABLE_ROLES) == {
        "implementer", "integrator", "proposer",
    }


@pytest.mark.asyncio
async def test_naysayer_only_identity_cannot_close(wired) -> None:
    """I-7. ``Einstein`` is allowed_roles=["naysayer"]; that intersects the
    closing roles nowhere, so the close is refused and no decide is written."""
    tools, chat, prismind = wired
    prismind.get_identity.return_value = _identity_response(["naysayer"])

    result = await tools["chatroom_close_thread"](
        project="p", thread_id="T-1", summary_content="done", author="Einstein",
        embodiment="web_ai_chat", role="naysayer",
    )

    assert result["error_type"] == "RoleNotAllowedToClose"
    assert result["details"]["allowed_roles"] == ["naysayer"]
    assert result["details"]["closeable_roles"] == [
        "implementer", "integrator", "proposer",
    ]
    chat.close_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_second_stage_fires_without_any_role_claim(wired) -> None:
    """I-7, the case that decides whether the stage is real.

    The decided form binds the identity's standing ``allowed_roles``, not the
    role claimed on this call. If it read the claim instead, omitting ``role``
    would walk straight past it -- and omitting ``role`` is the default.
    """
    tools, chat, prismind = wired
    prismind.get_identity.return_value = _identity_response(["naysayer"])

    result = await tools["chatroom_close_thread"](
        project="p", thread_id="T-1", summary_content="done", author="Einstein",
        embodiment="web_ai_chat",
    )

    assert result["error_type"] == "RoleNotAllowedToClose"
    chat.close_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_second_stage_rejection_is_distinguishable_from_the_owner_check(
    wired,
) -> None:
    """I-7 ordering invariant, stated as msg-037 requires it.

    ``Einstein`` here IS the thread owner, so Conclair's ``assert_owner_can_close``
    would let this through: any rejection is therefore stage 2's and only
    stage 2's. Stronger still, Conclair is never contacted -- not even the
    read that the close policies would perform -- so the owner check is not
    merely outvoted, it is unreachable.
    """
    tools, chat, prismind = wired
    prismind.get_identity.return_value = _identity_response(["naysayer"])
    chat.get_thread.return_value = {
        "thread": {"owner": "Einstein", "tags": []}, "messages": [], "mode": "full",
    }

    result = await tools["chatroom_close_thread"](
        project="p", thread_id="T-1", summary_content="done", author="Einstein",
        embodiment="web_ai_chat", role="naysayer",
    )

    assert result["error_type"] == "RoleNotAllowedToClose"
    chat.get_thread.assert_not_awaited()
    chat.close_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_closing_role_passes_the_second_stage(wired) -> None:
    """The other side of I-7: an identity that can integrate can close."""
    tools, chat, prismind = wired
    prismind.get_identity.return_value = _identity_response(
        ["proposer", "reviewer", "implementer", "integrator", "dogfooder"],
        name="Heisenberg",
    )

    result = await tools["chatroom_close_thread"](
        project="p", thread_id="T-1", summary_content="done", author="Heisenberg",
        embodiment="terminal_coding_agent", role="implementer",
    )

    assert "error_type" not in result
    chat.close_thread.assert_awaited_once()


@pytest.mark.asyncio
async def test_one_lookup_serves_both_stages(wired) -> None:
    """Two questions of one record, not two round-trips per close."""
    tools, chat, prismind = wired
    prismind.get_identity.return_value = _identity_response(
        ["implementer"], name="Heisenberg"
    )

    await tools["chatroom_close_thread"](
        project="p", thread_id="T-1", summary_content="done", author="Heisenberg",
        embodiment="terminal_coding_agent", role="implementer",
    )

    prismind.get_identity.assert_awaited_once()


@pytest.mark.asyncio
async def test_stage_one_rejection_wins_over_stage_two(wired) -> None:
    """Claim-then-capability: a role the identity may not assume is reported
    as such, even when the identity also could not have closed."""
    tools, chat, prismind = wired
    prismind.get_identity.return_value = _identity_response(["naysayer"])

    result = await tools["chatroom_close_thread"](
        project="p", thread_id="T-1", summary_content="done", author="Einstein",
        embodiment="web_ai_chat", role="implementer",
    )

    assert result["error_type"] == "RoleNotAllowed"
    chat.close_thread.assert_not_awaited()


# ---- I-8: the human exemption -----------------------------------------


@pytest.mark.asyncio
async def test_human_is_exempt_from_the_second_stage(wired) -> None:
    """I-8, and the reason msg-037 §2 put it in front of the implementation.

    The human record is ``allowed_roles=["human"]`` (verified live). Applied
    literally, the decided set would intersect that nowhere and lock the human
    out of closing anything.
    """
    tools, chat, prismind = wired
    prismind.get_identity.return_value = _identity_response(["human"], name="human")

    result = await tools["chatroom_close_thread"](
        project="p", thread_id="T-1", summary_content="done", author="human",
        role="human",
    )

    assert "error_type" not in result
    chat.close_thread.assert_awaited_once()


@pytest.mark.asyncio
async def test_human_force_close_of_a_non_owned_thread_survives_p3(wired) -> None:
    """I-8's real target: the shipped Tier-C force-close (ADR-2026-06-04-19 D-5).

    A human closing someone else's thread is the feature the naive form would
    have killed -- it is reached only after both role stages pass.
    """
    tools, chat, prismind = wired
    prismind.get_identity.return_value = _identity_response(["human"], name="human")
    chat.get_thread.return_value = {
        "thread": {"owner": "Bohr", "tags": []}, "messages": [], "mode": "full",
    }

    result = await tools["chatroom_close_thread"](
        project="p", thread_id="T-1", summary_content="done", author="human",
        owner_override_reason="Tier-C: superseded by the new design",
    )

    assert "error_type" not in result
    chat.close_thread.assert_awaited_once()
    assert chat.close_thread.call_args.kwargs["owner_override"] is True


@pytest.mark.asyncio
async def test_human_close_does_not_depend_on_prismind(wired) -> None:
    """The exemption is applied before the lookup, not after it.

    If the human path fetched the record first and then discarded the verdict,
    a Prismind outage would still block the above-loop approval layer. It must
    not even ask.
    """
    tools, chat, prismind = wired
    prismind.get_identity = AsyncMock(side_effect=RuntimeError("down"))

    result = await tools["chatroom_close_thread"](
        project="p", thread_id="T-1", summary_content="done", author="human",
    )

    assert "error_type" not in result
    prismind.get_identity.assert_not_awaited()
    chat.close_thread.assert_awaited_once()


@pytest.mark.asyncio
async def test_human_close_does_not_depend_on_prismind_even_with_a_role(wired) -> None:
    """The exemption has to hold for the close, not for one spelling of it.

    msg-041 Q6: if the human supplies ``role`` the first stage fetches the
    record, so an outage would block the above-loop Tier-C force-close over an
    optional string argument. The claim is unrecordable while the identity
    service is down, so it degrades to the value the system already means by
    "unverified" (null) instead of refusing the close.
    """
    tools, chat, prismind = wired
    prismind.get_identity = AsyncMock(side_effect=RuntimeError("down"))

    result = await tools["chatroom_close_thread"](
        project="p", thread_id="T-1", summary_content="done", author="human",
        role="human",
    )

    assert "error_type" not in result
    chat.close_thread.assert_awaited_once()
    assert chat.close_thread.call_args.kwargs["role"] is None


@pytest.mark.asyncio
async def test_human_close_still_records_a_role_the_lookup_confirms(wired) -> None:
    """The degradation above is scoped to the outage, not to being human.

    Falsifies "the human exemption was widened into never validating the
    human's claim": with a reachable identity service the claim is checked and
    recorded like anyone else's.
    """
    tools, chat, prismind = wired
    prismind.get_identity.return_value = _identity_response(["human"], name="human")

    result = await tools["chatroom_close_thread"](
        project="p", thread_id="T-1", summary_content="done", author="human",
        role="human",
    )

    assert "error_type" not in result
    assert chat.close_thread.call_args.kwargs["role"] == "human"


@pytest.mark.asyncio
async def test_a_human_role_the_record_denies_is_still_rejected_on_a_close(
    wired,
) -> None:
    """The exemption is from the *capability* stage, not from the claim gate.

    An unassumable role is a verdict, not an outage: it stays a rejection, so
    the exemption cannot be read as "the human may claim anything".
    """
    tools, chat, prismind = wired
    prismind.get_identity.return_value = _identity_response(["human"], name="human")

    result = await tools["chatroom_close_thread"](
        project="p", thread_id="T-1", summary_content="done", author="human",
        role="implementer",
    )

    assert result["error_type"] == "RoleNotAllowed"
    chat.close_thread.assert_not_awaited()


# ---- I-9: unregistered authors are not bound --------------------------


@pytest.mark.asyncio
async def test_unregistered_author_skips_the_second_stage(wired) -> None:
    """I-9 / msg-002 §3.2. Not theoretical, but not for the reason first
    written here (msg-041 Q4): the naysayer driver never closes anything --
    spirrow-mindwire@4ed9eb4 has no call site for it, and the orchestrator's
    PR-review threads are closed by ``human``. What makes the skip
    load-bearing is that unregistered identities close threads themselves:
    ``claude-code`` has no identity record (verified live 2026-08-02) and
    closed ``T-T183-plan-scope`` in spirrow-voxelworld."""
    tools, chat, prismind = wired
    prismind.get_identity.return_value = _UNREGISTERED

    result = await tools["chatroom_close_thread"](
        project="p", thread_id="T-1", summary_content="done", author="orchestrator",
        embodiment="terminal_coding_agent",
    )

    assert "error_type" not in result
    chat.close_thread.assert_awaited_once()


@pytest.mark.asyncio
async def test_second_stage_fails_closed_when_the_lookup_is_unusable(wired) -> None:
    """A stage that opens when the identity service is unreachable binds only
    the callers who cannot reach it. Distinct error_type from the stage-1
    envelope because that one's remedy ("post without role") is inapplicable:
    this stage never reads role."""
    tools, chat, prismind = wired
    prismind.get_identity = AsyncMock(side_effect=RuntimeError("connection refused"))

    result = await tools["chatroom_close_thread"](
        project="p", thread_id="T-1", summary_content="done", author="Einstein",
        embodiment="web_ai_chat",
    )

    assert result["error_type"] == "CloseRoleValidationUnavailableError"
    assert "connection refused" in result["details"]["reason"]
    chat.close_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_close_never_hands_back_a_remedy_it_will_refuse(wired) -> None:
    """msg-041 Q3, the blocking objection: the two stages must not disagree
    about what an outage means on a close.

    The stage-1 envelope names a remedy -- "retry, or post without `role`" --
    that is genuine on a post. On a close it is not: stage 2 does not read
    ``role``, so the retry it invites is refused deterministically one round
    trip later. Whichever way the caller spells the call, the close answers
    with the terminal stage-2 error and no instruction to drop ``role``.

    Falsified by: the with-role arm returning RoleValidationUnavailableError,
    or the two arms disagreeing.
    """
    tools, chat, prismind = wired
    prismind.get_identity = AsyncMock(side_effect=RuntimeError("connection refused"))

    with_role = await tools["chatroom_close_thread"](
        project="p", thread_id="T-1", summary_content="done", author="Einstein",
        embodiment="web_ai_chat", role="naysayer",
    )
    without_role = await tools["chatroom_close_thread"](
        project="p", thread_id="T-1", summary_content="done", author="Einstein",
        embodiment="web_ai_chat",
    )

    assert with_role["error_type"] == "CloseRoleValidationUnavailableError"
    assert with_role["error_type"] == without_role["error_type"]
    assert "without `role`" not in with_role["error"]
    chat.close_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_closing_decide_path_answers_an_outage_the_same_way(wired) -> None:
    """The trap must not survive on the other close entrance.

    ``post_message(decide, closes_thread=...)`` is a close, so it owes the
    caller the same answer; otherwise the misleading remedy is still reachable
    through the path msg-038 §3(b) brought under the stage.
    """
    tools, chat, prismind = wired
    prismind.get_identity = AsyncMock(side_effect=RuntimeError("connection refused"))

    result = await tools["chatroom_post_message"](
        project="p", thread_id="T-1", msg_type="decide", author="Einstein",
        content="c", closes_thread="T-1", embodiment="web_ai_chat",
        role="naysayer",
    )

    assert result["error_type"] == "CloseRoleValidationUnavailableError"
    chat.post_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_ordinary_post_still_offers_the_remedy(wired) -> None:
    """The stage-1 envelope is not wrong, it was in the wrong place.

    On a post the advice holds -- dropping ``role`` records null and the write
    proceeds -- so the fix must not blunt the error the non-close paths give.
    """
    tools, chat, prismind = wired
    prismind.get_identity = AsyncMock(side_effect=RuntimeError("connection refused"))

    refused = await tools["chatroom_post_message"](
        project="p", thread_id="T-1", msg_type="report", author="Einstein",
        content="c", role="naysayer",
    )
    retried = await tools["chatroom_post_message"](
        project="p", thread_id="T-1", msg_type="report", author="Einstein",
        content="c",
    )

    assert refused["error_type"] == "RoleValidationUnavailableError"
    assert "without `role`" in refused["error"]
    assert "error_type" not in retried
    assert chat.post_message.call_args.kwargs["role"] is None


# ---- the lookup contract: only a conforming answer is a verdict -------
#
# msg-044 §6.4 / msg-045 §3 (i). ``get_identity`` is documented to answer
# ``{"success": bool, "found": bool, "identity": dict|None, "message": str}``
# and "not registered" is carried by ``found=False`` alone. The skip that
# answer triggers exists for I-3 / I-9 legacy compatibility, so it is only
# ever correct when the service actually *said* "no such identity". A 200 OK
# that does not carry the field says nothing -- and reading "no" out of its
# absence turns the close gate off for the identity it exists to bind.


def _malformed_success(**overrides: Any) -> dict:
    """A 200 OK from the identity service that breaks the documented shape."""
    base = {
        "success": True,
        "identity": {"identity_name": "Einstein", "allowed_roles": ["naysayer"]},
        "message": "ok",
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_a_success_without_found_is_an_absent_verdict_not_a_negative_one(
    wired,
) -> None:
    """The post path, where the same coercion is visible without a close.

    ``not result.get("found", False)`` cannot tell "the record does not
    exist" from "the answer never contained the field". Only the first is a
    verdict; the second is the same nothing an outage returns, and it must
    take the same branch.
    """
    tools, chat, prismind = wired
    prismind.get_identity.return_value = _malformed_success()

    result = await tools["chatroom_post_message"](
        project="p", thread_id="T-1", msg_type="report", author="Einstein",
        content="c", role="naysayer",
    )

    assert result["error_type"] == "RoleValidationUnavailableError"
    chat.post_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_malformed_success_does_not_open_the_close_gate(wired) -> None:
    """★ The reachable fail-open (msg-044 §6.4, measured: ``await_count=1``).

    ``Einstein`` has ``allowed_roles=["naysayer"]``, which intersects
    ``CLOSEABLE_ROLES`` nowhere -- the exact identity the second stage was
    built to stop. A ``200 OK`` missing ``found`` classified it as an
    unregistered legacy actor, so I-9's exemption carried it straight through
    to ``close_thread``.

    Falsified by: ``close_thread`` being awaited at all, or the call
    answering with anything other than the terminal close envelope.
    """
    tools, chat, prismind = wired
    prismind.get_identity.return_value = _malformed_success()

    result = await tools["chatroom_close_thread"](
        project="p", thread_id="T-1", summary_content="done", author="Einstein",
        embodiment="web_ai_chat", role="naysayer",
    )

    assert result["error_type"] == "CloseRoleValidationUnavailableError"
    chat.close_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_malformed_success_does_not_open_the_close_gate_without_a_role(
    wired,
) -> None:
    """Omitting ``role`` must not be the way around it either.

    Stage 2 never reads ``role``, so the malformed answer has to be terminal
    on the no-claim spelling as well -- otherwise the bypass survives the fix
    and is simply spelled differently (the shape of msg-038 §3(b)).
    """
    tools, chat, prismind = wired
    prismind.get_identity.return_value = _malformed_success()

    result = await tools["chatroom_close_thread"](
        project="p", thread_id="T-1", summary_content="done", author="Einstein",
        embodiment="web_ai_chat",
    )

    assert result["error_type"] == "CloseRoleValidationUnavailableError"
    chat.close_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_closing_decide_path_rejects_a_malformed_success_too(wired) -> None:
    """The other close entrance owes the same answer (``closes_thread``)."""
    tools, chat, prismind = wired
    prismind.get_identity.return_value = _malformed_success()

    result = await tools["chatroom_post_message"](
        project="p", thread_id="T-1", msg_type="decide", author="Einstein",
        content="c", closes_thread="T-1", embodiment="web_ai_chat",
        role="naysayer",
    )

    assert result["error_type"] == "CloseRoleValidationUnavailableError"
    chat.post_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("found", [None, 0, "", []])
async def test_only_a_boolean_found_is_read_as_an_answer(wired, found: Any) -> None:
    """The hinge is the field's *type*, not its truthiness.

    Every value here is falsy, so ``.get("found", False)`` collapses each of
    them onto the same "not registered" branch as a genuine ``False``. A JSON
    ``null`` in particular is the shape a partially-populated response takes,
    and it is the one that most looks like an answer without being one.
    """
    tools, chat, prismind = wired
    prismind.get_identity.return_value = _malformed_success(found=found)

    result = await tools["chatroom_close_thread"](
        project="p", thread_id="T-1", summary_content="done", author="Einstein",
        embodiment="web_ai_chat", role="naysayer",
    )

    assert result["error_type"] == "CloseRoleValidationUnavailableError"
    chat.close_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_found_true_without_a_record_is_not_an_empty_allowed_roles(
    wired,
) -> None:
    """The mirror case, on the fail-closed side: do not invent a verdict.

    ``found=True`` with no ``identity`` object left ``allowed_roles`` to
    default to ``()``, which then produced ``RoleNotAllowed`` naming
    ``allowed_roles=[]`` -- a statement about a record the response never
    carried. It refuses, so it is not a hole; it is the same coercion of a
    contract violation into a verdict, and it lies to the one audience that
    reads the message. Both directions get the one rule.
    """
    tools, chat, prismind = wired
    prismind.get_identity.return_value = _malformed_success(found=True, identity=None)

    result = await tools["chatroom_post_message"](
        project="p", thread_id="T-1", msg_type="report", author="Einstein",
        content="c", role="naysayer",
    )

    assert result["error_type"] == "RoleValidationUnavailableError"
    chat.post_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_confirmed_negative_still_skips_and_only_it_does(wired) -> None:
    """I-3 / I-9 non-regression, stated as the distinction itself.

    Same author, same close, two responses. The well-formed ``found=False``
    is a verdict and must keep working -- ``claude-code`` closes threads today
    (msg-042 §4) and binding it would change traffic that exists. The
    malformed one is not a verdict and must not. If the fix cost the first
    arm, it traded a fail-open for a regression, so both are pinned in one
    place where they cannot drift apart.
    """
    tools, chat, prismind = wired

    prismind.get_identity.return_value = _UNREGISTERED
    confirmed = await tools["chatroom_close_thread"](
        project="p", thread_id="T-1", summary_content="done", author="claude-code",
        embodiment="terminal_coding_agent",
    )
    assert "error_type" not in confirmed
    chat.close_thread.assert_awaited_once()

    prismind.get_identity.return_value = _malformed_success()
    undetermined = await tools["chatroom_close_thread"](
        project="p", thread_id="T-2", summary_content="done", author="claude-code",
        embodiment="terminal_coding_agent",
    )
    assert undetermined["error_type"] == "CloseRoleValidationUnavailableError"
    chat.close_thread.assert_awaited_once()  # still the first one


@pytest.mark.asyncio
async def test_a_malformed_success_leaves_the_post_remedy_intact(wired) -> None:
    """The stage-1 remedy must stay true for the case that now produces it.

    "post without `role`" works here for the same reason it works on an
    outage: ``_check_role_allowed`` returns before it looks anything up, so
    the retry the envelope invites does not re-enter the broken lookup. This
    is the Q3 trap's falsification condition applied to the new branch.
    """
    tools, chat, prismind = wired
    prismind.get_identity.return_value = _malformed_success()

    refused = await tools["chatroom_post_message"](
        project="p", thread_id="T-1", msg_type="report", author="Einstein",
        content="c", role="naysayer",
    )
    retried = await tools["chatroom_post_message"](
        project="p", thread_id="T-1", msg_type="report", author="Einstein",
        content="c",
    )

    assert "without `role`" in refused["error"]
    assert "error_type" not in retried
    assert chat.post_message.call_args.kwargs["role"] is None


# ---- the closes_thread bypass -----------------------------------------


@pytest.mark.asyncio
async def test_decide_that_closes_a_thread_takes_the_second_stage(wired) -> None:
    """``post_message(msg_type="decide", closes_thread=...)`` resolves a thread,
    so it is a close. Leaving it on the per-message gate alone would document
    the way around I-7 in the tool's own docstring."""
    tools, chat, prismind = wired
    prismind.get_identity.return_value = _identity_response(["naysayer"])

    result = await tools["chatroom_post_message"](
        project="p", thread_id="T-1", msg_type="decide", author="Einstein",
        content="c", closes_thread="T-1", embodiment="web_ai_chat",
        role="naysayer",
    )

    assert result["error_type"] == "RoleNotAllowedToClose"
    chat.get_thread.assert_not_awaited()
    chat.post_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_decide_that_does_not_close_is_not_a_close(wired) -> None:
    """Scope: the second stage attaches to resolving a thread, not to the
    ``decide`` msg_type. A non-closing decide is an ordinary post."""
    tools, chat, prismind = wired
    prismind.get_identity.return_value = _identity_response(["naysayer"])

    result = await tools["chatroom_post_message"](
        project="p", thread_id="T-1", msg_type="decide", author="Einstein",
        content="c", embodiment="web_ai_chat", role="naysayer",
    )

    assert "error_type" not in result
    chat.post_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_ordinary_posts_are_untouched_by_the_second_stage(wired) -> None:
    """The naysayer must keep reviewing; only closing is restricted."""
    tools, chat, prismind = wired
    prismind.get_identity.return_value = _identity_response(["naysayer"])

    result = await tools["chatroom_post_message"](
        project="p", thread_id="T-1", msg_type="report", author="Einstein",
        content="c", role="naysayer",
    )

    assert "error_type" not in result
    assert chat.post_message.call_args.kwargs["role"] == "naysayer"


@pytest.mark.asyncio
async def test_open_thread_is_untouched_by_the_second_stage(wired) -> None:
    """Opening is not closing."""
    tools, chat, prismind = wired
    prismind.get_identity.return_value = _identity_response(["naysayer"])

    result = await tools["chatroom_open_thread"](
        project="p", thread_id="T-1", title="t", owner="Einstein",
        propose_content="hi", role="naysayer",
    )

    assert "error_type" not in result
    chat.open_thread.assert_awaited_once()


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
