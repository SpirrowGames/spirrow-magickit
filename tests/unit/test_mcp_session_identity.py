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
