"""Characterization tests for ``checkpoint``'s silent field-drop.

WHAT THESE TESTS ARE
--------------------
These pin behaviour that is **defective**, not behaviour that is wanted.
They exist so the mechanism established in chatroom
``T-checkpoint-silent-partial-write`` msg-231 is reproducible inside the
repo instead of only in prose, and so that the fix (D1 receipt / D2
``None`` sentinel) shows up as a deliberate inversion of a named
expectation rather than as an unexplained red test.

**If you are implementing D1/D2, you are supposed to break these.** Invert
the assertion and move the docstring's "current" wording to "was".  Do not
"repair" a failure here by restoring the truthiness gate.

THE MECHANISM (verified against source, 2026-09-06)
---------------------------------------------------
``checkpoint`` forwards an optional field to Prismind only when the value
is *truthy*, and the adapter underneath applies the same test a second
time:

* ``mcp/tools/session.py``   -- ``if blockers: save_args["blockers"] = ...``
* ``adapters/prismind.py``   -- ``if blockers: arguments["blockers"] = ...``

Falsy values (``""``, ``None``, ``[]``) are therefore dropped twice and
never reach the wire.  Prismind treats an absent key as "leave unchanged"
(``x if x is not None else existing``), so a dropped field silently keeps
its old value while the call reports success.

Consequence worth stating plainly: **there is no way to clear a blocker
through this API.** ``blockers=[]`` is indistinguishable from omitting it.

Both layers are pinned on purpose.  Fixing only the tool layer leaves the
adapter's gate to swallow the value a second time, which would look like
the fix had failed for no visible reason.
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
    """Run ``checkpoint`` and return the kwargs it passed to save_session."""
    with patch.object(session_tools, "PrismindAdapter") as MockAdapter:
        inst = MockAdapter.return_value
        inst.save_session = AsyncMock(return_value={"success": True, "saved_to": ["MCP Memory Server"]})

        call.setdefault("summary", "s")
        call.setdefault("auto_extract", False)
        await tools["checkpoint"](**call)

        inst.save_session.assert_awaited_once()
        return inst.save_session.await_args.kwargs


class TestCheckpointDropsFalsyFields:
    """msg-231 Step 1, pins 1 and 2: falsy optional fields never leave the tool."""

    @pytest.mark.asyncio
    async def test_empty_blockers_list_is_not_forwarded(self, tools):
        """``blockers=[]`` is dropped, so a stale blocker cannot be cleared.

        This is the pin that matters most operationally: every turn of the
        trilateral loop ends with a checkpoint, and passing an empty list to
        say "nothing is blocking me now" is silently a no-op.
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


class TestCheckpointReportsNothingAboutWhatItWrote:
    """msg-231 Step 1, pin 3, and the D1 target: success carries no receipt."""

    @pytest.mark.asyncio
    async def test_dropping_every_optional_field_still_reports_success(self, tools):
        """The caller cannot tell a full write from a summary-only write."""
        with patch.object(session_tools, "PrismindAdapter") as MockAdapter:
            inst = MockAdapter.return_value
            inst.save_session = AsyncMock(return_value={"success": True, "saved_to": []})

            result = await tools["checkpoint"](
                summary="s",
                blockers=[],
                current_phase="",
                current_task="",
                next_action="",
                auto_extract=False,
            )

        assert result["success"] is True
        # D1 will add these two keys; today there is no way to learn that
        # four of the five fields were discarded.
        assert "fields_written" not in result
        assert "fields_skipped" not in result

    @pytest.mark.asyncio
    async def test_saved_to_ignores_whether_prismind_actually_persisted(self, tools):
        """``saved_to`` records "we called", not "it stored".

        Prismind's own ``save_session`` returns ``success=True`` with an
        empty ``saved_to`` when the Memory Server write fails.  checkpoint
        appends "session" on the strength of no exception being raised and
        never inspects the payload, so a non-persisting backend is reported
        to the caller as a clean save.
        """
        with patch.object(session_tools, "PrismindAdapter") as MockAdapter:
            inst = MockAdapter.return_value
            inst.save_session = AsyncMock(
                return_value={
                    "success": True,
                    "saved_to": [],
                    "message": "not persisted - check the Memory Server",
                }
            )

            result = await tools["checkpoint"](summary="s", auto_extract=False)

        assert result["saved_to"] == ["session"]
        assert result["success"] is True


class TestAdapterAppliesTheSameGateASecondTime:
    """The second gate, in adapters/prismind.py.

    Pinned separately because D2 has to change both. A fix applied only to
    the tool layer would be swallowed here with no signal.
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
        """
        arguments = await self._save_session_arguments(summary="")

        assert "summary" not in arguments

    @pytest.mark.asyncio
    async def test_embodiment_already_uses_the_none_sentinel(self):
        """One field is already correct -- it is the model for D2.

        ``embodiment`` is gated on ``is not None``, so an explicit empty
        string survives the call. The D2 change is to make the other five
        fields behave the way this one already does.
        """
        arguments = await self._save_session_arguments(summary="s", embodiment="")

        assert arguments["embodiment"] == ""
