"""Unit tests for the upsert_identity / list_context_authors MCP tool wrappers.

The session tools instantiate PrismindAdapter() inside each call rather than
using a module-level singleton, so we patch the class itself.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from magickit.config import Settings
from magickit.mcp.tools import session as session_tools


def _capture_tools(settings: Settings) -> dict[str, Any]:
    """Register session tools and capture the wrappers by name.

    Intercepts @mcp.tool() with a mock; same approach as
    tests/unit/test_mcp_chatroom_tools.py to avoid coupling to FastMCP's
    versioned tool-lookup API.
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
    return Settings(
        prismind_url="http://localhost:8112",
        prismind_timeout=5.0,
    )


@pytest.fixture
def tools(settings: Settings) -> dict[str, Any]:
    return _capture_tools(settings)


class TestUpsertIdentityTool:
    """upsert_identity MCP wrapper forwards through to PrismindAdapter.

    Shape locked by msg-002 §1.1 / msg-005 D-5 (α): embodiment and
    independence_class are required, allowed_roles is required unless
    keep_allowed_roles=True.
    """

    @pytest.mark.asyncio
    async def test_forwards_fields_and_returns_payload(self, tools):
        upstream = {
            "success": True,
            "identity": {
                "identity_name": "Heisenberg",
                "user": "u",
                "allowed_roles": ["proposer", "reviewer"],
                "embodiment": "terminal_coding_agent",
                "independence_class": "main-chain",
                "persona_description": "Heisenberg",
            },
            "created": True,
            "message": "ok",
        }

        with patch.object(session_tools, "PrismindAdapter") as MockAdapter:
            inst = MockAdapter.return_value
            inst.upsert_identity = AsyncMock(return_value=upstream)

            result = await tools["upsert_identity"](
                identity_name="Heisenberg",
                embodiment="terminal_coding_agent",
                independence_class="main-chain",
                allowed_roles=["proposer", "reviewer"],
                persona_description="Heisenberg",
                user="u",
            )

            inst.upsert_identity.assert_awaited_once()
            kwargs = inst.upsert_identity.call_args.kwargs
            assert kwargs["identity_name"] == "Heisenberg"
            assert kwargs["embodiment"] == "terminal_coding_agent"
            assert kwargs["independence_class"] == "main-chain"
            assert kwargs["allowed_roles"] == ["proposer", "reviewer"]
            # Non-empty persona_description forwarded as-is
            assert kwargs["persona_description"] == "Heisenberg"
            assert kwargs["user"] == "u"
            # keep_allowed_roles default False is forwarded explicitly
            assert kwargs["keep_allowed_roles"] is False

        assert result["success"] is True
        assert result["created"] is True
        assert result["identity"]["allowed_roles"] == ["proposer", "reviewer"]
        assert result["identity"]["embodiment"] == "terminal_coding_agent"
        assert result["message"] == "ok"

    @pytest.mark.asyncio
    async def test_empty_persona_description_becomes_none(self, tools):
        """Empty persona_description default is forwarded as None so upstream preserves."""
        with patch.object(session_tools, "PrismindAdapter") as MockAdapter:
            inst = MockAdapter.return_value
            inst.upsert_identity = AsyncMock(
                return_value={"success": True, "created": False, "identity": {}}
            )

            await tools["upsert_identity"](
                identity_name="Heisenberg",
                embodiment="terminal_coding_agent",
                independence_class="main-chain",
                allowed_roles=["proposer"],
            )

            kwargs = inst.upsert_identity.call_args.kwargs
            # None lets Prismind preserve existing persona_description; ""
            # would overwrite it.
            assert kwargs["persona_description"] is None

    @pytest.mark.asyncio
    async def test_keep_allowed_roles_forwarded(self, tools):
        """keep_allowed_roles=True is forwarded to the adapter."""
        with patch.object(session_tools, "PrismindAdapter") as MockAdapter:
            inst = MockAdapter.return_value
            inst.upsert_identity = AsyncMock(
                return_value={"success": True, "created": False, "identity": {}}
            )

            await tools["upsert_identity"](
                identity_name="Heisenberg",
                embodiment="terminal_coding_agent",
                independence_class="main-chain",
                keep_allowed_roles=True,
            )

            kwargs = inst.upsert_identity.call_args.kwargs
            assert kwargs["keep_allowed_roles"] is True
            # allowed_roles omitted (None) -- upstream uses keep flag
            assert kwargs["allowed_roles"] is None

    @pytest.mark.asyncio
    async def test_empty_identity_name_fails_fast(self, tools):
        """Empty identity_name returns an error without hitting the adapter."""
        with patch.object(session_tools, "PrismindAdapter") as MockAdapter:
            inst = MockAdapter.return_value
            inst.upsert_identity = AsyncMock()

            result = await tools["upsert_identity"](
                identity_name="",
                embodiment="terminal_coding_agent",
                independence_class="main-chain",
                allowed_roles=["proposer"],
            )

            inst.upsert_identity.assert_not_called()
            assert result["success"] is False
            assert result["identity"] is None
            assert result["created"] is False

    @pytest.mark.asyncio
    async def test_adapter_exception_becomes_failure_dict(self, tools):
        """A raised exception is converted to {success: False, ...}."""
        with patch.object(session_tools, "PrismindAdapter") as MockAdapter:
            inst = MockAdapter.return_value
            inst.upsert_identity = AsyncMock(side_effect=RuntimeError("boom"))

            result = await tools["upsert_identity"](
                identity_name="Heisenberg",
                embodiment="terminal_coding_agent",
                independence_class="main-chain",
                allowed_roles=["proposer"],
            )

            assert result["success"] is False
            assert result["created"] is False
            assert "boom" in result["message"]

    @pytest.mark.asyncio
    async def test_upstream_envelope_surfaced_to_caller(self, tools):
        """When the adapter returns the upstream error_type envelope (D-7 i),
        the wrapper surfaces both the human-readable message and the
        structured error_type / details so callers can branch on the
        error class without parsing the text. Pinned by Einstein F-01 +
        msg-010 D-9."""
        envelope = {
            "error_type": "UpstreamValidationError",
            "error": "Input validation error: 'cli_robot' is not one of ['web_ai_chat', 'terminal_coding_agent']",
            "details": {"field": "embodiment", "value": "cli_robot"},
        }

        with patch.object(session_tools, "PrismindAdapter") as MockAdapter:
            inst = MockAdapter.return_value
            inst.upsert_identity = AsyncMock(return_value=envelope)

            result = await tools["upsert_identity"](
                identity_name="Heisenberg",
                embodiment="cli_robot",
                independence_class="main-chain",
                allowed_roles=["proposer"],
            )

        assert result["success"] is False
        assert result["identity"] is None
        assert result["created"] is False
        assert "cli_robot" in result["message"]
        # Structured fields propagated so callers can branch on error class
        assert result["error_type"] == "UpstreamValidationError"
        assert result["details"] == {"field": "embodiment", "value": "cli_robot"}


class TestListContextAuthorsTool:
    """list_context_authors must pass the joined 'identity' field through."""

    @pytest.mark.asyncio
    async def test_identity_field_passed_through(self, tools):
        upstream_authors = [
            {
                "author": "ident-1",
                "user": "u",
                "current_phase": "P1",
                "current_task": "T1",
                "updated_at": "2026-05-28T00:00:00",
                "identity": {
                    "identity_name": "ident-1",
                    "allowed_roles": ["proposer", "reviewer"],
                    "embodiment": "web_ai_chat",
                    "independence_class": "main-chain",
                },
            },
            {
                "author": "legacy-author",
                "user": "u",
                "updated_at": "2026-05-27T00:00:00",
                "identity": None,
            },
        ]
        upstream = {
            "success": True,
            "project": "p1",
            "authors": upstream_authors,
            "total_count": 2,
            "message": "ok",
        }

        with patch.object(session_tools, "PrismindAdapter") as MockAdapter:
            inst = MockAdapter.return_value
            inst.list_context_authors = AsyncMock(return_value=upstream)

            result = await tools["list_context_authors"](project="p1")

        # Authors list is forwarded verbatim, identity field intact.
        assert result["success"] is True
        assert result["total_count"] == 2
        assert result["authors"][0]["identity"] is not None
        assert result["authors"][0]["identity"]["allowed_roles"] == [
            "proposer", "reviewer",
        ]
        assert result["authors"][1]["identity"] is None


class TestGetIdentityTool:
    """get_identity is a diagnostic, so it must not fold the way the gate does.

    Spec: T-adr12-step3-embodiment-column-removal msg-282 2-2..2-5. The gate
    (``chatroom._lookup_identity``) collapses every unreadable answer into one
    "unusable" verdict; a tool built to explain the gate has to keep those
    cases apart, or it is blind to exactly what it exists to show.
    """

    @staticmethod
    def _ok(identity: dict[str, Any]) -> dict[str, Any]:
        return {"success": True, "found": True, "identity": identity, "message": "ok"}

    @staticmethod
    async def _call(tools, upstream=None, exc=None, **kwargs):
        with patch.object(session_tools, "PrismindAdapter") as MockAdapter:
            inst = MockAdapter.return_value
            inst.get_identity = AsyncMock(return_value=upstream, side_effect=exc)
            return await tools["get_identity"](**kwargs)

    # -- 1. found: raw passthrough, and embodiment absent != null (2-4) ----

    @pytest.mark.asyncio
    async def test_found_passes_record_through_unchanged(self, tools):
        record = {
            "identity_name": "Heisenberg",
            "user": "u",
            "allowed_roles": ["implementer"],
            "independence_class": "main-chain",
            "persona_description": "note",
            "embodiment": "terminal_coding_agent",
        }
        result = await self._call(
            tools, upstream=self._ok(record), identity_name="Heisenberg"
        )

        assert result["status"] == "found"
        assert result["identity"] == record
        assert result["identity"]["persona_description"] == "note"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("record_extra", "expect_key", "expect_value"),
        [
            ({"embodiment": "terminal_coding_agent"}, True, "terminal_coding_agent"),
            ({"embodiment": None}, True, None),
            ({}, False, None),
        ],
        ids=["embodiment-present", "embodiment-null", "embodiment-absent"],
    )
    async def test_embodiment_absent_is_distinguishable_from_null(
        self, tools, record_extra, expect_key, expect_value
    ):
        """The (a)/(b) branch of msg-277 4 turns on exactly this distinction.

        After re-upserting without ``embodiment``, "key gone" and "value null"
        are branch (a) while "old value still there" is branch (b). A
        diagnostic that folds absent into null makes that call impossible.
        """
        record = {"identity_name": "Bohr", "allowed_roles": ["proposer"]}
        record.update(record_extra)

        result = await self._call(
            tools, upstream=self._ok(record), identity_name="Bohr"
        )

        assert result["status"] == "found"
        assert ("embodiment" in result["identity"]) is expect_key
        assert ("embodiment" in result["present_keys"]) is expect_key
        if expect_key:
            assert result["identity"]["embodiment"] == expect_value

    # -- 2. found=false is an answer, not a failure --------------------------

    @pytest.mark.asyncio
    async def test_found_false_is_not_found_not_lookup_failed(self, tools):
        result = await self._call(
            tools,
            upstream={"success": True, "found": False, "identity": None, "message": ""},
            identity_name="nobody",
        )

        assert result["status"] == "not_found"
        assert "identity" not in result

    # -- 3. a missing `found` is a violation, never the permissive branch ----

    @pytest.mark.asyncio
    async def test_missing_found_key_is_contract_violation(self, tools):
        """``.get("found", False)`` is the msg-044 6.4 fail-open itself."""
        upstream = {"success": True, "identity": {"allowed_roles": ["naysayer"]}}
        result = await self._call(tools, upstream=upstream, identity_name="Einstein")

        assert result["status"] == "contract_violation"
        assert result["raw"] == upstream
        assert any("found" in v for v in result["violations"])

    @pytest.mark.asyncio
    async def test_non_bool_found_is_contract_violation(self, tools):
        result = await self._call(
            tools,
            upstream={"success": True, "found": "yes", "identity": {}},
            identity_name="Einstein",
        )

        assert result["status"] == "contract_violation"

    # -- 4. found=true without a record must not manufacture one ------------

    @pytest.mark.asyncio
    async def test_found_true_without_identity_is_violation_not_empty_roles(
        self, tools
    ):
        result = await self._call(
            tools,
            upstream={"success": True, "found": True, "message": "ok"},
            identity_name="Bohr",
        )

        assert result["status"] == "contract_violation"
        assert "identity" not in result
        assert any("identity" in v for v in result["violations"])

    # -- 5/6. allowed_roles is reported, never coerced -----------------------

    @pytest.mark.asyncio
    async def test_bool_allowed_roles_is_violation_and_survives_raw(self, tools):
        """``tuple(True)`` raises; the value has to come back as it was."""
        upstream = self._ok({"identity_name": "X", "allowed_roles": True})
        result = await self._call(tools, upstream=upstream, identity_name="X")

        assert result["status"] == "contract_violation"
        assert result["raw"]["identity"]["allowed_roles"] is True

    @pytest.mark.asyncio
    async def test_string_allowed_roles_is_not_split_into_characters(self, tools):
        """``tuple("naysayer")`` is the bypass that recorded the role "n"."""
        upstream = self._ok({"identity_name": "Einstein", "allowed_roles": "naysayer"})
        result = await self._call(tools, upstream=upstream, identity_name="Einstein")

        assert result["status"] == "contract_violation"
        reported = result["raw"]["identity"]["allowed_roles"]
        assert isinstance(reported, str)
        assert reported == "naysayer"

    @pytest.mark.asyncio
    async def test_non_string_element_is_a_violation(self, tools):
        """Beyond 2-2's literal list: the gate rejects these too.

        ``_lookup_identity`` checks element types, so a record carrying them
        makes the gate answer "unusable". Reporting it as ``found`` would
        leave the diagnostic unable to explain that outcome.
        """
        upstream = self._ok({"identity_name": "X", "allowed_roles": ["proposer", 7]})
        result = await self._call(tools, upstream=upstream, identity_name="X")

        assert result["status"] == "contract_violation"
        assert result["raw"]["identity"]["allowed_roles"] == ["proposer", 7]

    # -- 7. [] is a legal record value --------------------------------------

    @pytest.mark.asyncio
    async def test_empty_allowed_roles_is_found(self, tools):
        upstream = self._ok({"identity_name": "human", "allowed_roles": []})
        result = await self._call(tools, upstream=upstream, identity_name="human")

        assert result["status"] == "found"
        assert result["identity"]["allowed_roles"] == []

    # -- 8. transport failure is its own status ------------------------------

    @pytest.mark.asyncio
    async def test_adapter_exception_is_lookup_failed(self, tools):
        result = await self._call(
            tools, exc=RuntimeError("connection refused"), identity_name="Bohr"
        )

        assert result["status"] == "lookup_failed"
        assert "connection refused" in result["error"]

    @pytest.mark.asyncio
    async def test_unsuccessful_response_is_lookup_failed(self, tools):
        result = await self._call(
            tools,
            upstream={"success": False, "message": "prismind down"},
            identity_name="Bohr",
        )

        assert result["status"] == "lookup_failed"
        assert result["error"] == "prismind down"

    @pytest.mark.asyncio
    async def test_error_type_envelope_is_lookup_failed(self, tools):
        result = await self._call(
            tools,
            upstream={"error_type": "ToolNotFound", "error": "no such tool"},
            identity_name="Bohr",
        )

        assert result["status"] == "lookup_failed"
        assert "ToolNotFound" in result["error"]

    # -- 9. every envelope names the partition it read -----------------------

    @pytest.mark.asyncio
    async def test_partition_is_reported_on_every_status(self, tools):
        cases = [
            ({"success": True, "found": True, "identity": {"user": "u"}}, None),
            ({"success": True, "found": False}, None),
            ({"success": True, "identity": {}}, None),
            ({"success": False, "message": "down"}, None),
            (None, RuntimeError("boom")),
        ]
        for upstream, exc in cases:
            result = await self._call(
                tools, upstream=upstream, exc=exc, identity_name="Bohr"
            )
            assert result["identity_name"] == "Bohr"
            assert "user_partition" in result
            assert "resolved" in result["user_partition"]

    @pytest.mark.asyncio
    async def test_explicit_user_is_forwarded_and_echoed_as_resolved(self, tools):
        with patch.object(session_tools, "PrismindAdapter") as MockAdapter:
            inst = MockAdapter.return_value
            inst.get_identity = AsyncMock(
                return_value=self._ok({"identity_name": "Bohr", "user": "sgadmin"})
            )
            result = await tools["get_identity"](identity_name="Bohr", user="sgadmin")

            inst.get_identity.assert_awaited_once_with(
                identity_name="Bohr", user="sgadmin"
            )

        assert result["user_partition"]["requested"] == "sgadmin"
        assert result["user_partition"]["resolved"] == "sgadmin"
        assert result["user_partition"]["source"] == "argument"

    @pytest.mark.asyncio
    async def test_deferred_partition_is_named_from_the_record_not_invented(
        self, tools
    ):
        """An unregistered identity and a partition mismatch read alike.

        With ``user=""`` Magickit defers to upstream's configured user and
        does not know that value, so ``resolved`` may only be filled in from
        the record that came back -- never guessed.
        """
        found = await self._call(
            tools,
            upstream=self._ok({"identity_name": "Bohr", "user": "sgadmin"}),
            identity_name="Bohr",
        )
        assert found["user_partition"]["resolved"] == "sgadmin"
        assert found["user_partition"]["source"] == "record"

        missing = await self._call(
            tools,
            upstream={"success": True, "found": False},
            identity_name="ghost",
        )
        assert missing["user_partition"]["resolved"] is None
        assert missing["user_partition"]["source"] == "upstream_default"

    # -- read-only: no write path is reachable from here ---------------------

    @pytest.mark.asyncio
    async def test_never_calls_upsert(self, tools):
        with patch.object(session_tools, "PrismindAdapter") as MockAdapter:
            inst = MockAdapter.return_value
            inst.get_identity = AsyncMock(return_value=self._ok({"user": "u"}))
            inst.upsert_identity = AsyncMock()

            await tools["get_identity"](identity_name="Bohr")

            inst.upsert_identity.assert_not_awaited()
