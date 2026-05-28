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
    """upsert_identity MCP wrapper forwards through to PrismindAdapter."""

    @pytest.mark.asyncio
    async def test_forwards_fields_and_returns_payload(self, tools):
        upstream = {
            "success": True,
            "identity": {
                "identity_name": "ident-1",
                "user": "u",
                "allowed_roles": ["proposer", "reviewer"],
                "default_role": "proposer",
                "display_name": "Disp",
            },
            "created": True,
            "message": "ok",
        }

        with patch.object(session_tools, "PrismindAdapter") as MockAdapter:
            inst = MockAdapter.return_value
            inst.upsert_identity = AsyncMock(return_value=upstream)

            result = await tools["upsert_identity"](
                identity_name="ident-1",
                allowed_roles=["proposer", "reviewer"],
                default_role="proposer",
                display_name="Disp",
                notes="hi",
                user="u",
            )

            inst.upsert_identity.assert_awaited_once()
            kwargs = inst.upsert_identity.call_args.kwargs
            assert kwargs["identity_name"] == "ident-1"
            assert kwargs["allowed_roles"] == ["proposer", "reviewer"]
            # default_role/display_name/notes are non-empty strings in this
            # case, so the wrapper forwards them as-is. Empty defaults are
            # mapped to None to let upstream preserve existing values.
            assert kwargs["default_role"] == "proposer"
            assert kwargs["display_name"] == "Disp"
            assert kwargs["notes"] == "hi"
            assert kwargs["user"] == "u"

        assert result["success"] is True
        assert result["created"] is True
        assert result["identity"]["allowed_roles"] == ["proposer", "reviewer"]
        assert result["message"] == "ok"

    @pytest.mark.asyncio
    async def test_empty_string_defaults_become_none(self, tools):
        """Empty string defaults are forwarded as None so upstream preserves them."""
        with patch.object(session_tools, "PrismindAdapter") as MockAdapter:
            inst = MockAdapter.return_value
            inst.upsert_identity = AsyncMock(
                return_value={"success": True, "created": False, "identity": {}}
            )

            await tools["upsert_identity"](
                identity_name="ident-1",
                allowed_roles=["proposer"],
            )

            kwargs = inst.upsert_identity.call_args.kwargs
            # None lets Prismind preserve existing values; "" would overwrite.
            assert kwargs["default_role"] is None
            assert kwargs["display_name"] is None
            assert kwargs["notes"] is None

    @pytest.mark.asyncio
    async def test_empty_identity_name_fails_fast(self, tools):
        """Empty identity_name returns an error without hitting the adapter."""
        with patch.object(session_tools, "PrismindAdapter") as MockAdapter:
            inst = MockAdapter.return_value
            inst.upsert_identity = AsyncMock()

            result = await tools["upsert_identity"](identity_name="")

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

            result = await tools["upsert_identity"](identity_name="ident-1")

            assert result["success"] is False
            assert result["created"] is False
            assert "boom" in result["message"]


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
                    "default_role": "proposer",
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
