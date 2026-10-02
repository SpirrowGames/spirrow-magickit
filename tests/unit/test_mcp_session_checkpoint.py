"""Behaviour tests for ``checkpoint``'s write receipt (D1) and its D2 target.

WHAT THIS FILE COVERS
---------------------
This file has two audiences, and both matter.

**The D1 tests** describe the write receipt that ``checkpoint`` now
returns and the fail-closed ``success`` rule that pairs with it.  They
answer the question chatroom ``T-checkpoint-silent-partial-write`` was
opened to answer: given a checkpoint call that returns success, can the
caller tell what actually got written?  Under D1 the answer is yes,
through three added keys — ``fields_written`` / ``fields_skipped`` /
``persisted`` — and through a ``success`` flag that is ``True`` only when
the downstream session store confirmed persistence (chatroom
T-checkpoint-silent-partial-write msg-262 §5 / msg-264 §5).

**The D2a tests** (msg-1063 §3 / msg-1065 §1) pin the contract that
closes msg-059 requirements 1 and 2 for ``next_action`` / ``blockers``:
both are required with no default, an explicit ``null`` / ``[]`` is a
clear that is forwarded and reported in ``fields_cleared``, and an
empty or whitespace-only value is rejected loudly before anything is
written.  The three remaining optional fields (``current_phase`` /
``current_task`` / ``embodiment``) keep "absent or empty keeps the
stored value" as their documented contract (D2a-3), and the tests that
pin their truthiness gate stay.

The ``embodiment`` test is the model the D2a adapter change imitates:
an explicit value survives the adapter instead of being dropped.

D1 IMPLEMENTATION NOTES (verified against source, 2026-09-07)
-------------------------------------------------------------
* ``mcp/tools/session.py`` — ``fields_written`` / ``fields_skipped`` /
  ``persisted`` are returned on every call; ``persisted`` transcribes
  the downstream ``saved_to`` (present-and-non-empty / present-and-empty
  / missing-or-malformed → ``True`` / ``False`` / ``None``).
* ``mcp/tools/session.py`` — ``saved_to.append("session")`` only fires
  when ``persisted is True``; the empty-answer and no-answer cases both
  suppress it.
* ``mcp/tools/session.py`` — ``success = persisted is True``.  A
  downstream store that answered ``saved_to: []``, a response that did
  not include ``saved_to``, and an exception during ``save_session`` all
  yield ``success: False`` and a message that spells the reason.
* ``adapters/prismind.py`` — ``save_session`` sends ``next_action=""``
  only on the keyword-only ``clear_next_action=True`` (msg-1105 §2; a JSON
  null would be dropped by Prismind, msg-1103 §2), and ``blockers``
  whenever it is a list, ``[]`` included (D2a).  The other scalars keep
  the gate.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError

from magickit.adapters.prismind import PrismindAdapter
from magickit.config import Settings
from magickit.mcp.tools import session as session_tools


def _capture_tools(settings: Settings) -> dict[str, Any]:
    """Register session tools and capture the wrappers by name.

    Same interception as tests/unit/test_mcp_session_identity.py: the tools
    build a PrismindAdapter per call rather than sharing a singleton, so the
    class is patched at the module that uses it.
    """
    registered: dict[str, Any] = {}

    def fake_tool(*args: Any, **kwargs: Any):
        def decorator(fn):
            registered[fn.__name__] = fn
            return fn

        return decorator

    mock_mcp = MagicMock()
    mock_mcp.tool = fake_tool
    session_tools.register_tools(mock_mcp, settings)
    return registered


@pytest.fixture
def settings() -> Settings:
    return Settings(prismind_url="http://localhost:8112", prismind_timeout=5.0)


@pytest.fixture
def tools(settings: Settings) -> dict[str, Any]:
    return _capture_tools(settings)


async def _checkpoint_kwargs(tools: dict[str, Any], **call: Any) -> dict[str, Any]:
    """Run ``checkpoint`` with a stubbed adapter that reports success.

    Returns the kwargs the tool passed to ``save_session``.  The stub
    returns a non-empty ``saved_to`` so ``persisted`` is ``True`` and the
    caller-visible ``success`` does not distract from what this helper is
    inspecting (which field-set actually got forwarded).
    """
    with patch.object(session_tools, "PrismindAdapter") as MockAdapter:
        inst = MockAdapter.return_value
        inst.save_session = AsyncMock(
            return_value={"success": True, "saved_to": ["MCP Memory Server"]}
        )

        call.setdefault("summary", "s")
        # D2a-1: both are required; tests not about them pass a plain value.
        call.setdefault("next_action", "n")
        call.setdefault("blockers", ["b"])
        call.setdefault("auto_extract", False)
        await tools["checkpoint"](**call)

        inst.save_session.assert_awaited_once()
        return inst.save_session.await_args.kwargs


async def _checkpoint_result(
    tools: dict[str, Any],
    save_return: Any,
    **call: Any,
) -> dict[str, Any]:
    """Run ``checkpoint`` with an explicit ``save_session`` return value.

    Used for the D1 receipt tests where the *response* is what matters.
    """
    with patch.object(session_tools, "PrismindAdapter") as MockAdapter:
        inst = MockAdapter.return_value
        inst.save_session = AsyncMock(return_value=save_return)

        call.setdefault("summary", "s")
        # D2a-1: both are required; tests not about them pass a plain value.
        call.setdefault("next_action", "n")
        call.setdefault("blockers", ["b"])
        call.setdefault("auto_extract", False)
        return await tools["checkpoint"](**call)


# ---------------------------------------------------------------------------
# D2a — next_action / blockers are required, clears are explicit, bad values
# are rejected loudly (msg-1063 §3 / msg-1065 §1).
# ---------------------------------------------------------------------------


class TestCheckpointRequiredFieldsAreForwardedOrCleared:
    """``next_action`` / ``blockers`` always leave the tool.

    The probe in msg-1055 showed the original symptom at the server
    boundary: ``next_action=""`` / ``blockers=[]`` returned ``success:true``
    and the read-back kept the old values, because the truthiness gate
    never forwarded them.  Under D2a a value is a write and ``null`` /
    ``[]`` is a clear; nothing in between is silent.
    """

    @pytest.mark.asyncio
    async def test_empty_blockers_list_is_forwarded_as_a_clear(self, tools):
        """``blockers=[]`` means "no blockers" and now reaches the store."""
        kwargs = await _checkpoint_kwargs(tools, blockers=[])

        assert kwargs["blockers"] == []

    @pytest.mark.asyncio
    async def test_a_nonempty_blockers_list_is_forwarded(self, tools):
        kwargs = await _checkpoint_kwargs(tools, blockers=["b"])

        assert kwargs["blockers"] == ["b"]

    @pytest.mark.asyncio
    async def test_null_next_action_is_forwarded_as_a_clear(self, tools):
        """``null`` is the only way to clear ``next_action`` (msg-1065 §1)."""
        kwargs = await _checkpoint_kwargs(tools, next_action=None)

        # msg-1105 §2: the clear travels as the keyword-only flag, never as
        # a None value the adapter could mistake for "omitted".
        assert kwargs["clear_next_action"] is True
        assert kwargs["next_action"] == ""

    @pytest.mark.asyncio
    async def test_a_next_action_value_does_not_set_the_clear_flag(self, tools):
        kwargs = await _checkpoint_kwargs(tools, next_action="do x")

        assert "clear_next_action" not in kwargs

    @pytest.mark.asyncio
    async def test_a_next_action_value_is_forwarded(self, tools):
        kwargs = await _checkpoint_kwargs(tools, next_action="do x")

        assert kwargs["next_action"] == "do x"

    @pytest.mark.asyncio
    async def test_optional_empty_scalars_are_still_not_forwarded(self, tools):
        """current_phase / current_task keep "empty keeps the stored value".

        D2a-3 (msg-1063 §3): that is their contract, not a gap.
        """
        kwargs = await _checkpoint_kwargs(tools, current_phase="", current_task="")

        assert "current_phase" not in kwargs
        assert "current_task" not in kwargs


class TestCheckpointRejectsBrokenRequiredValues:
    """D2a-2: the server-side forms of a malformed call fail loudly.

    Each rejection must happen before any write: a rejected call that
    still wrote part of its payload would be msg-059's partial write in a
    new shape.
    """

    async def _rejected(self, tools: dict[str, Any], **call: Any) -> Any:
        with patch.object(session_tools, "PrismindAdapter") as MockAdapter:
            inst = MockAdapter.return_value
            inst.save_session = AsyncMock(
                return_value={"success": True, "saved_to": ["MCP Memory Server"]}
            )
            inst.add_knowledge = AsyncMock()
            call.setdefault("summary", "s")
            call.setdefault("next_action", "n")
            call.setdefault("blockers", ["b"])
            call.setdefault("auto_extract", False)
            with pytest.raises(ToolError) as excinfo:
                await tools["checkpoint"](project="p", decisions=["d"], **call)
            inst.save_session.assert_not_awaited()
            inst.add_knowledge.assert_not_awaited()
            return excinfo.value

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", ["", " ", "\n\t "])
    async def test_empty_or_whitespace_next_action_is_rejected(self, tools, value):
        """probe arm-empty (msg-1055): this used to keep the old value."""
        err = await self._rejected(tools, next_action=value)

        assert "next_action" in str(err)
        assert "null" in str(err)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", [[""], ["ok", " "], ["ok", "\n"]])
    async def test_blockers_with_an_empty_element_are_rejected(self, tools, value):
        """The operator's extra variant (msg-1055): ``[""]`` used to overwrite."""
        err = await self._rejected(tools, blockers=value)

        assert "blockers" in str(err)
        assert "[]" in str(err)


class TestRequiredFieldsAtTheMcpBoundary:
    """D2a-1 through the real FastMCP schema, not the captured function.

    The captured-function tests above cannot see the framework's argument
    validation.  These register ``checkpoint`` on a real ``FastMCP`` and
    call it the way a client does, so "absent is rejected" and "null is
    accepted" are tested where they are enforced.
    """

    async def _call(self, settings: Settings, arguments: dict[str, Any]) -> Any:
        from fastmcp import Client, FastMCP

        server = FastMCP("t")
        session_tools.register_tools(server, settings)
        with patch.object(session_tools, "PrismindAdapter") as MockAdapter:
            inst = MockAdapter.return_value
            inst.save_session = AsyncMock(
                return_value={"success": True, "saved_to": ["MCP Memory Server"]}
            )
            async with Client(server) as client:
                result = await client.call_tool(
                    "checkpoint", arguments, raise_on_error=False
                )
            return result, inst.save_session

    @pytest.mark.asyncio
    @pytest.mark.parametrize("missing", ["next_action", "blockers"])
    async def test_an_absent_required_field_is_an_error(self, settings, missing):
        """probe arm-omit (msg-1055): this used to return success:true."""
        arguments = {
            "summary": "s",
            "next_action": "n",
            "blockers": ["b"],
            "auto_extract": False,
        }
        del arguments[missing]

        result, save_session = await self._call(settings, arguments)

        assert result.is_error is True
        save_session.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_explicit_null_next_action_is_accepted(self, settings):
        result, save_session = await self._call(
            settings,
            {
                "summary": "s",
                "next_action": None,
                "blockers": [],
                "auto_extract": False,
            },
        )

        # t4 (msg-1105 §2): caller null -> adapter clear flag, and the
        # receipt reports the clear.
        assert result.is_error is False
        kwargs = save_session.await_args.kwargs
        assert kwargs["clear_next_action"] is True
        assert kwargs["next_action"] == ""
        assert kwargs["blockers"] == []
        assert "next_action" in result.data["fields_cleared"]
        assert "blockers" in result.data["fields_cleared"]

    @pytest.mark.asyncio
    async def test_a_broken_value_is_an_error_result(self, settings):
        result, save_session = await self._call(
            settings,
            {
                "summary": "s",
                "next_action": "",
                "blockers": ["b"],
                "auto_extract": False,
            },
        )

        assert result.is_error is True
        save_session.assert_not_awaited()


class TestAdapterForwardsTheD2aFields:
    """The second gate, in adapters/prismind.py.

    D2a has to change both layers: a fix applied only to the tool would be
    swallowed here with no signal (msg-262 §1.3).
    """

    async def _save_session_arguments(self, **call: Any) -> dict[str, Any]:
        adapter = PrismindAdapter(sse_url="http://localhost:8112")
        adapter._call_tool_safe = AsyncMock(return_value=(True, {"success": True}))

        await adapter.save_session(**call)

        name, arguments = adapter._call_tool_safe.await_args.args
        assert name == "save_session"
        return arguments

    @pytest.mark.asyncio
    async def test_empty_blockers_list_is_forwarded(self):
        arguments = await self._save_session_arguments(summary="s", blockers=[])

        assert arguments["blockers"] == []

    @pytest.mark.asyncio
    async def test_blockers_none_is_not_forwarded(self):
        """``None`` stays "not provided" for any other adapter caller."""
        arguments = await self._save_session_arguments(summary="s", blockers=None)

        assert "blockers" not in arguments

    @pytest.mark.asyncio
    async def test_t1_no_arguments_sends_no_next_action_key(self):
        """t1 (msg-1105 §2): omission never clears."""
        arguments = await self._save_session_arguments()

        assert "next_action" not in arguments

    @pytest.mark.asyncio
    async def test_t2_clear_flag_sends_empty_string(self):
        """t2: the clear is sent as "" -- Prismind 3fbde90 drops a null.

        server.py:1865 reads ``args.get("next_action")``, which cannot tell
        null from a missing key, so only "" reaches the store as a clear
        (msg-1103 §2).
        """
        arguments = await self._save_session_arguments(clear_next_action=True)

        assert arguments["next_action"] == ""

    @pytest.mark.asyncio
    async def test_t3_clear_flag_with_a_value_is_rejected(self):
        """t3: contradictory input is loud and nothing is sent."""
        adapter = PrismindAdapter(sse_url="http://localhost:8112")
        adapter._call_tool_safe = AsyncMock(return_value=(True, {"success": True}))

        with pytest.raises(ValueError):
            await adapter.save_session(next_action="x", clear_next_action=True)

        adapter._call_tool_safe.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_clear_flag_is_keyword_only(self):
        adapter = PrismindAdapter(sse_url="http://localhost:8112")
        adapter._call_tool_safe = AsyncMock(return_value=(True, {"success": True}))

        with pytest.raises(TypeError):
            await adapter.save_session(
                "s", "", None, "", "", "", "", "", "", None, True
            )

    @pytest.mark.asyncio
    async def test_empty_scalars_are_still_dropped(self):
        """``""`` stays "not provided" in the adapter; the tool rejects it upstream."""
        arguments = await self._save_session_arguments(
            summary="s", next_action="", current_phase="", current_task=""
        )

        assert "next_action" not in arguments
        assert "current_phase" not in arguments
        assert "current_task" not in arguments

    @pytest.mark.asyncio
    async def test_even_summary_is_dropped_when_empty(self):
        """``summary`` is unconditional only in the tool, not in the adapter.

        msg-247 §3(a): unchanged by D2a, which is scoped to next_action and
        blockers.
        """
        arguments = await self._save_session_arguments(summary="")

        assert "summary" not in arguments

    @pytest.mark.asyncio
    async def test_embodiment_uses_the_none_sentinel(self):
        """``embodiment`` is gated on ``is not None``: the model D2a follows."""
        arguments = await self._save_session_arguments(summary="s", embodiment="")

        assert arguments["embodiment"] == ""


# ---------------------------------------------------------------------------
# D1 receipt — the write is inspectable and success is fail-closed.
# ---------------------------------------------------------------------------


class TestCheckpointReceiptEnumeratesFieldFate:
    """``fields_written`` / ``fields_cleared`` / ``fields_skipped`` account
    for every session field (msg-262 §5 / msg-264 §5 / msg-1065 §1).
    """

    @pytest.mark.asyncio
    async def test_only_optional_fields_can_be_skipped(self, tools):
        """With only the required fields, the three optional ones are skipped.

        For the optional fields, "passed empty" and "not passed" collapse at
        the call boundary (msg-323 §3); both mean "keep" (D2a-3).
        """
        result = await _checkpoint_result(
            tools,
            {"success": True, "saved_to": ["MCP Memory Server"]},
            summary="s",
            next_action="n",
            blockers=["b"],
        )

        assert result["fields_written"] == ["blockers", "next_action"]
        assert result["fields_cleared"] == []
        # Order is stable and defined by _CHECKPOINT_OPTIONAL_FIELDS.
        assert result["fields_skipped"] == [
            "current_phase",
            "current_task",
            "embodiment",
        ]

    @pytest.mark.asyncio
    async def test_a_full_payload_reports_them_all_as_written(self, tools):
        result = await _checkpoint_result(
            tools,
            {"success": True, "saved_to": ["MCP Memory Server"]},
            summary="s",
            blockers=["b"],
            current_phase="p",
            current_task="t",
            next_action="n",
            embodiment="terminal_coding_agent",
        )

        assert result["fields_written"] == [
            "blockers",
            "current_phase",
            "current_task",
            "next_action",
            "embodiment",
        ]
        assert result["fields_cleared"] == []
        assert result["fields_skipped"] == []

    @pytest.mark.asyncio
    async def test_clears_are_reported_in_fields_cleared(self, tools):
        """A clear is never silent (msg-1065 §1 / msg-262 §1.1)."""
        result = await _checkpoint_result(
            tools,
            {"success": True, "saved_to": ["MCP Memory Server"]},
            summary="s",
            blockers=[],
            next_action=None,
            current_phase="p",
        )

        assert result["fields_cleared"] == ["blockers", "next_action"]
        assert result["fields_written"] == ["current_phase"]
        assert "blockers" not in result["fields_skipped"]
        assert "next_action" not in result["fields_skipped"]


class TestCheckpointReceiptRecordsPersistence:
    """``persisted`` transcribes the downstream store's answer verbatim.

    R2 (msg-264 §5): three cases must remain distinguishable — the store
    said "I saved this", the store said "I saved nothing", and the store
    did not answer the question.  Collapsing the last two into one
    boolean would re-open the "answered without saying anything" defect
    this change closes (msg-264 §2.2).

    Einstein's structural objection in msg-265 argued for a two-value
    ``persisted``; the tri-state is retained here per the same message's
    non-blocking clearance ("The design is safe to build").  The
    tri-state's *operational* consequence — the ``success`` value — is
    fail-closed either way (see the ``TestCheckpointSuccessIsFailClosed``
    class), so Einstein's correctness concern is satisfied by R3, and
    the shape below is the value-preserving report of *why*.
    """

    @pytest.mark.asyncio
    async def test_non_empty_saved_to_reports_persisted_true(self, tools):
        result = await _checkpoint_result(
            tools,
            {"success": True, "saved_to": ["MCP Memory Server"]},
            summary="s",
        )

        assert result["persisted"] is True
        assert result["saved_to"] == ["session"]

    @pytest.mark.asyncio
    async def test_empty_saved_to_reports_persisted_false(self, tools):
        """The store answered, and said it saved nothing.

        The tool must not add ``"session"`` to its own ``saved_to`` in
        this case — R1 (msg-264 §5).
        """
        result = await _checkpoint_result(
            tools,
            {
                "success": True,
                "saved_to": [],
                "message": "not persisted - check the Memory Server",
            },
            summary="s",
        )

        assert result["persisted"] is False
        assert "session" not in result["saved_to"]

    @pytest.mark.asyncio
    async def test_missing_saved_to_key_reports_persisted_null(self, tools):
        """The store did not answer the persistence question.

        Preserved as ``None`` (not coerced to ``False``) so the message
        can distinguish "answered empty" from "did not answer".  Doing
        otherwise would blame the downstream store for a write failure
        we did not observe (msg-264 §2.2).
        """
        result = await _checkpoint_result(
            tools,
            {"success": True, "message": "response without saved_to"},
            summary="s",
        )

        assert result["persisted"] is None
        assert "session" not in result["saved_to"]

    @pytest.mark.asyncio
    async def test_missing_saved_to_and_empty_saved_to_have_distinct_messages(
        self, tools
    ):
        """R4 (msg-264 §5): the reason must be spellable from the message.

        The distinction is what lets a caller decide whether to file a
        Prismind bug (empty saved_to case) or a shape-of-response bug
        (missing key case).  Collapsing them costs the reader the
        diagnosis (mirrors the OBL-SPEC-PIN reason-code rule for pins).
        """
        empty = await _checkpoint_result(
            tools, {"success": True, "saved_to": []}, summary="s"
        )
        missing = await _checkpoint_result(
            tools, {"success": True}, summary="s"
        )

        assert "empty" in empty["message"].lower()
        assert "missing" in missing["message"].lower() or "did not" in missing["message"].lower()
        assert empty["message"] != missing["message"]

    @pytest.mark.asyncio
    async def test_malformed_saved_to_reports_persisted_null(self, tools):
        """A ``saved_to`` that is not a list is not a valid answer.

        Preserved as ``None`` for the same reason as the missing case:
        the store did not answer the question in the shape the contract
        requires (msg-264 §2.3 L1).
        """
        result = await _checkpoint_result(
            tools,
            {"success": True, "saved_to": "MCP Memory Server"},  # str, not list
            summary="s",
        )

        assert result["persisted"] is None


class TestCheckpointSuccessIsFailClosed:
    """R3 (msg-264 §5, Einstein-endorsed in msg-265): ``success`` needs a positive confirmation.

    The cost asymmetry (msg-264 §1.2) is what decides this: a false
    ``False`` costs an idempotent retry, a false ``True`` costs the next
    role running on stale state.  The latter is the very defect the
    thread was opened to address, so the direction to fail closed is the
    only one available.
    """

    @pytest.mark.asyncio
    async def test_success_true_only_when_persisted_true(self, tools):
        result = await _checkpoint_result(
            tools,
            {"success": True, "saved_to": ["MCP Memory Server"]},
            summary="s",
        )

        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_success_false_when_downstream_saved_to_is_empty(self, tools):
        """The store's ``success=True`` no longer suffices — R3."""
        result = await _checkpoint_result(
            tools,
            {"success": True, "saved_to": []},
            summary="s",
        )

        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_success_false_when_downstream_did_not_report_saved_to(self, tools):
        """No answer to the persistence question is not a positive answer."""
        result = await _checkpoint_result(
            tools,
            {"success": True, "message": "opaque"},
            summary="s",
        )

        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_success_false_when_save_session_raises(self, tools):
        """An exception was already the loud path; keep it loud in the receipt."""
        with patch.object(session_tools, "PrismindAdapter") as MockAdapter:
            inst = MockAdapter.return_value
            inst.save_session = AsyncMock(side_effect=RuntimeError("boom"))

            result = await tools["checkpoint"](
                summary="s", next_action="n", blockers=["b"], auto_extract=False
            )

        assert result["success"] is False
        assert result["persisted"] is None
        assert "boom" in result["message"]

    @pytest.mark.asyncio
    async def test_a_full_write_with_a_decision_failure_stays_successful(
        self, tools, monkeypatch
    ):
        """Knowledge failures do not flip ``success`` on their own.

        The obligation this file implements is about *session state*.  A
        decision-write failure surfaces via ``message`` (mirrors the
        existing behaviour), and the session-persisted receipt remains
        the truth of what happened to the checkpoint itself.
        """
        with patch.object(session_tools, "PrismindAdapter") as MockAdapter:
            inst = MockAdapter.return_value
            inst.save_session = AsyncMock(
                return_value={"success": True, "saved_to": ["MCP Memory Server"]}
            )
            inst.add_knowledge = AsyncMock(side_effect=RuntimeError("knowledge down"))

            result = await tools["checkpoint"](
                summary="s",
                next_action="n",
                blockers=["b"],
                project="p",
                decisions=["d1"],
                auto_extract=False,
            )

        assert result["success"] is True
        assert result["persisted"] is True
        # INV-D1-SUCCESS is unchanged: the direction of ``success`` is not
        # what this assert is about.  R5 (msg-323 §2) adds the requirement
        # that the message actually name the decision that did not land.
        assert "warning" in result["message"].lower()
        assert "decision" in result["message"].lower()


class TestCheckpointReceiptShapeIsStable:
    """Every call returns the three new keys, populated with defined values.

    The whole point of the receipt is that a caller can trust it exists;
    a receipt that appears only on some code paths would push the
    read-back workaround back onto the caller for the paths where it
    does not (which is msg-231 requirement 3 written backwards).
    """

    @pytest.mark.asyncio
    async def test_all_three_receipt_keys_are_always_present(self, tools):
        result = await _checkpoint_result(
            tools,
            {"success": True, "saved_to": ["MCP Memory Server"]},
            summary="s",
        )

        assert "fields_written" in result
        assert "fields_cleared" in result
        assert "fields_skipped" in result
        assert "persisted" in result

    @pytest.mark.asyncio
    async def test_receipt_keys_present_even_when_save_raises(self, tools):
        with patch.object(session_tools, "PrismindAdapter") as MockAdapter:
            inst = MockAdapter.return_value
            inst.save_session = AsyncMock(side_effect=RuntimeError("boom"))

            result = await tools["checkpoint"](
                summary="s", next_action="n", blockers=["b"], auto_extract=False
            )

        assert "fields_written" in result
        assert "fields_cleared" in result
        assert "fields_skipped" in result
        assert result["persisted"] is None


class TestMessageAlwaysReportsUnsavedDecisions:
    """R5 (msg-323 §2): every branch of ``message`` accounts for the decisions.

    The PR gate on #49/#50 found that the message branches consulted the
    error list only when ``persisted is True``.  A decision that failed
    while the session write *also* failed was therefore reported nowhere:
    the return value carries no ``errors`` key, so ``message`` is the only
    channel the docstring contracts for it ("Knowledge/decision failures
    do not flip this flag on their own; they surface via ``message``").

    The predicate these tests hold the code to is not "the error list is
    joined into the string" but the one written in msg-323 §2: *if any
    decision the caller passed was not saved, the message says so -- on
    every branch*.  ``project``-less calls count as unsaved (R5b), because
    from the caller's side those decisions vanish just as completely.

    What is deliberately NOT tested here is ``success`` moving.  It does
    not move: INV-D1-SUCCESS (msg-267 §1) scopes ``success`` to session
    state, because widening it would turn a knowledge outage into a retry
    loop against an append-only store (msg-268 endorsed exactly this).
    """

    @pytest.mark.asyncio
    async def test_a_session_exception_and_a_decision_failure_are_both_reported(
        self, tools
    ):
        """The branch the gate objected to: both failures, session one once.

        Reported once matters as much as reported at all.  The session
        failure already has its own branch text; re-joining it from a
        shared accumulator would print it twice and could evict the
        decision failure from the truncation window (msg-323 §2(b)).
        """
        with patch.object(session_tools, "PrismindAdapter") as MockAdapter:
            inst = MockAdapter.return_value
            inst.save_session = AsyncMock(side_effect=RuntimeError("boom"))
            inst.add_knowledge = AsyncMock(side_effect=RuntimeError("knowledge down"))

            result = await tools["checkpoint"](
                summary="s",
                next_action="n",
                blockers=["b"],
                project="p",
                decisions=["d1"],
                auto_extract=False,
            )

        assert result["success"] is False
        assert result["persisted"] is None
        assert "boom" in result["message"]
        assert result["message"].count("boom") == 1
        assert "decision" in result["message"].lower()
        assert "d1" in result["message"]

    @pytest.mark.asyncio
    async def test_an_empty_saved_to_still_reports_the_decision_failure(self, tools):
        """``persisted is False`` keeps its own reason *and* gains the decision."""
        with patch.object(session_tools, "PrismindAdapter") as MockAdapter:
            inst = MockAdapter.return_value
            inst.save_session = AsyncMock(
                return_value={"success": True, "saved_to": []}
            )
            inst.add_knowledge = AsyncMock(side_effect=RuntimeError("knowledge down"))

            result = await tools["checkpoint"](
                summary="s",
                next_action="n",
                blockers=["b"],
                project="p",
                decisions=["d1"],
                auto_extract=False,
            )

        assert result["success"] is False
        assert result["persisted"] is False
        assert "empty" in result["message"].lower()
        assert "decision" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_a_missing_saved_to_still_reports_the_decision_failure(self, tools):
        """``persisted is None`` likewise: the no-answer text is not a swallow."""
        with patch.object(session_tools, "PrismindAdapter") as MockAdapter:
            inst = MockAdapter.return_value
            inst.save_session = AsyncMock(return_value={"success": True})
            inst.add_knowledge = AsyncMock(side_effect=RuntimeError("knowledge down"))

            result = await tools["checkpoint"](
                summary="s",
                next_action="n",
                blockers=["b"],
                project="p",
                decisions=["d1"],
                auto_extract=False,
            )

        assert result["success"] is False
        assert result["persisted"] is None
        assert "confirm persistence" in result["message"]
        assert "decision" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_decisions_without_a_project_are_reported_as_unsaved(self, tools):
        """R5b: the drop the PR gate did not find, on the *success* path.

        No exception is raised anywhere in this call.  The decisions are
        discarded before ``add_knowledge`` is ever reached, so nothing
        lands in the error path at all -- the caller used to get
        ``success: True``, ``knowledge_added: 0`` and "Checkpoint saved
        successfully", with the loss recorded only in a server-side log.
        That is this thread's defect in its purest form, so it is one of
        the cases R5 has to cover.
        """
        with patch.object(session_tools, "PrismindAdapter") as MockAdapter:
            inst = MockAdapter.return_value
            inst.save_session = AsyncMock(
                return_value={"success": True, "saved_to": ["MCP Memory Server"]}
            )
            inst.add_knowledge = AsyncMock()

            result = await tools["checkpoint"](
                summary="s",
                next_action="n",
                blockers=["b"],
                decisions=["d1"],
                auto_extract=False,
            )

        inst.add_knowledge.assert_not_awaited()
        assert result["knowledge_added"] == 0
        assert "not saved" in result["message"].lower()
        assert "project" in result["message"].lower()
        # INV-D1-SUCCESS (msg-267 §1): the session write did land, so
        # ``success`` stays True.  R5 changes what the message must say,
        # never what ``success`` means.
        assert result["success"] is True
        assert result["persisted"] is True
