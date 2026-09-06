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

**The D2 characterization tests** are still red-in-spirit even where
they pass.  They pin the *residual* truthiness gate — the one D1 does
not touch and D2 will invert once the measurements M1/M2/M3 are
recorded (msg-264 §1).  They exist so that "the tool layer still drops
``blockers=[]``" and "the adapter still drops empty scalars a second
time" show up as *pinned* facts a future change can see and reverse,
rather than as unstated assumptions.  Do not fix them here; the danger
is that a naive gate inversion turns today's silent no-op into a silent
destructive overwrite of the caller's other fields (msg-262 §1.1), and
that is exactly what M1/M2/M3 are being run first to prevent.

The ``embodiment`` test is the same shape but positive: one field
already behaves the way D2 wants the others to behave, and it is the
model D2 will imitate.

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
* ``adapters/prismind.py`` — the second truthiness gate is unchanged.
  D2's task, not D1's.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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
        call.setdefault("auto_extract", False)
        return await tools["checkpoint"](**call)


# ---------------------------------------------------------------------------
# D2 characterization pins — the residual truthiness gate D1 does not touch.
# ---------------------------------------------------------------------------


class TestCheckpointStillDropsFalsyOptionalFields:
    """The tool-layer truthiness gate is intact under D1.

    D2 (msg-264 §1) will flip these once M1/M2/M3 are recorded, at which
    point ``blockers=[]`` will mean "clear the blocker" instead of "no
    change".  Until that measurement lands, the gate has to stay — an
    inversion applied while the schema default of the scalars is still
    ``""`` would silently overwrite the caller's stored values with
    empties whenever the field was omitted (msg-262 §1.1).
    """

    @pytest.mark.asyncio
    async def test_empty_blockers_list_is_not_forwarded(self, tools):
        """``blockers=[]`` is still dropped, so a stale blocker cannot be cleared.

        Operationally this is the sharpest observable of the D2 defect:
        every turn of the trilateral loop wants to say "nothing is
        blocking me now" and today that call is a silent no-op.  D1's
        receipt makes the drop *visible* (see the D1 tests below); D2
        will make the drop *stop*.
        """
        kwargs = await _checkpoint_kwargs(tools, blockers=[])

        assert "blockers" not in kwargs

    @pytest.mark.asyncio
    async def test_a_nonempty_blockers_list_is_forwarded(self, tools):
        """Control: the drop is about falsiness, not about the field."""
        kwargs = await _checkpoint_kwargs(tools, blockers=["b"])

        assert kwargs["blockers"] == ["b"]

    @pytest.mark.asyncio
    async def test_empty_next_action_is_not_forwarded(self, tools):
        kwargs = await _checkpoint_kwargs(tools, next_action="")

        assert "next_action" not in kwargs

    @pytest.mark.asyncio
    async def test_empty_scalars_are_not_forwarded(self, tools):
        """current_phase / current_task drop on the same gate."""
        kwargs = await _checkpoint_kwargs(tools, current_phase="", current_task="")

        assert "current_phase" not in kwargs
        assert "current_task" not in kwargs


class TestAdapterAppliesTheSameGateASecondTime:
    """The second gate, in adapters/prismind.py.

    Pinned separately because D2 has to change both.  A fix applied only
    to the tool layer would be swallowed here with no signal, which is
    exactly the "looks like it failed for no visible reason" mode
    msg-262 §1.3 warns against.
    """

    async def _save_session_arguments(self, **call: Any) -> dict[str, Any]:
        adapter = PrismindAdapter(sse_url="http://localhost:8112")
        adapter._call_tool_safe = AsyncMock(return_value=(True, {"success": True}))

        await adapter.save_session(**call)

        name, arguments = adapter._call_tool_safe.await_args.args
        assert name == "save_session"
        return arguments

    @pytest.mark.asyncio
    async def test_empty_blockers_list_is_dropped_again(self):
        arguments = await self._save_session_arguments(summary="s", blockers=[])

        assert "blockers" not in arguments

    @pytest.mark.asyncio
    async def test_empty_scalars_are_dropped_again(self):
        arguments = await self._save_session_arguments(
            summary="s", next_action="", current_phase="", current_task=""
        )

        assert "next_action" not in arguments
        assert "current_phase" not in arguments
        assert "current_task" not in arguments

    @pytest.mark.asyncio
    async def test_even_summary_is_dropped_when_empty(self):
        """``summary`` is unconditional only in the tool, not in the adapter.

        msg-231 describes summary as "the one field always forwarded"; that
        holds at session.py but not here, so an empty summary reaches
        Prismind as an absent key and leaves ``last_summary`` untouched.
        msg-247 §3(a) corrected this: D2 must not exempt ``summary`` from
        the inversion.
        """
        arguments = await self._save_session_arguments(summary="")

        assert "summary" not in arguments

    @pytest.mark.asyncio
    async def test_embodiment_already_uses_the_none_sentinel(self):
        """One field is already correct — it is the model for D2.

        ``embodiment`` is gated on ``is not None``, so an explicit empty
        string survives the call.  D2 will make the other fields behave
        the way this one already does.  (M1 in msg-262 §1.2 still has to
        check that the *tool* layer forwards this unchanged; if it applies
        a truthiness gate upstream, ``embodiment`` is a false model and
        this pin turns into evidence rather than into a template.)
        """
        arguments = await self._save_session_arguments(summary="s", embodiment="")

        assert arguments["embodiment"] == ""


# ---------------------------------------------------------------------------
# D1 receipt — the write is now inspectable and success is fail-closed.
# ---------------------------------------------------------------------------


class TestCheckpointReceiptEnumeratesFieldFate:
    """``fields_written`` / ``fields_skipped`` account for every optional field.

    The read-back the chatroom was doing by hand — "did the field I sent
    actually get through?" — is now on the wire.  msg-231 opened the
    thread on this; msg-262 §5 fixed the receipt's shape; msg-264 §5
    confirmed it.
    """

    @pytest.mark.asyncio
    async def test_omitting_every_optional_field_reports_them_all_as_skipped(
        self, tools
    ):
        result = await _checkpoint_result(
            tools,
            {"success": True, "saved_to": ["MCP Memory Server"]},
            summary="s",
        )

        assert result["fields_written"] == []
        # Order is stable and defined by _CHECKPOINT_OPTIONAL_FIELDS, so the
        # caller can rely on it when diffing turns.
        assert result["fields_skipped"] == [
            "blockers",
            "current_phase",
            "current_task",
            "next_action",
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
        assert result["fields_skipped"] == []

    @pytest.mark.asyncio
    async def test_falsy_values_are_reported_as_skipped_not_written(self, tools):
        """The receipt describes what actually left the tool, not intent.

        The caller passed ``blockers=[]`` and ``next_action=""``; both were
        dropped by the truthiness gate above.  Under D1 the caller does
        not have to resume to find that out — the receipt says it.
        """
        result = await _checkpoint_result(
            tools,
            {"success": True, "saved_to": ["MCP Memory Server"]},
            summary="s",
            blockers=[],
            next_action="",
            current_phase="p",
        )

        assert result["fields_written"] == ["current_phase"]
        assert "blockers" in result["fields_skipped"]
        assert "next_action" in result["fields_skipped"]


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

            result = await tools["checkpoint"](summary="s", auto_extract=False)

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
                project="p",
                decisions=["d1"],
                auto_extract=False,
            )

        assert result["success"] is True
        assert result["persisted"] is True
        assert "warning" in result["message"].lower()


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
        assert "fields_skipped" in result
        assert "persisted" in result

    @pytest.mark.asyncio
    async def test_receipt_keys_present_even_when_save_raises(self, tools):
        with patch.object(session_tools, "PrismindAdapter") as MockAdapter:
            inst = MockAdapter.return_value
            inst.save_session = AsyncMock(side_effect=RuntimeError("boom"))

            result = await tools["checkpoint"](summary="s", auto_extract=False)

        assert "fields_written" in result
        assert "fields_skipped" in result
        assert result["persisted"] is None
