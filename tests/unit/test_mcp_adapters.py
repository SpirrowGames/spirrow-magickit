"""Tests for MCP adapter base class and Prismind adapter."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from magickit.adapters.mcp_base import MCPBaseAdapter
from magickit.adapters.prismind import Document, PrismindAdapter


class ConcreteMCPAdapter(MCPBaseAdapter):
    """Concrete implementation for testing abstract base class."""

    async def health_check(self) -> bool:
        return True


class TestMCPBaseAdapter:
    """Tests for MCPBaseAdapter."""

    def test_init_adds_sse_suffix(self):
        """Test that SSE suffix is added if missing."""
        adapter = ConcreteMCPAdapter(sse_url="http://localhost:8112")
        assert adapter.sse_url == "http://localhost:8112/sse"

    def test_init_keeps_sse_suffix(self):
        """Test that SSE suffix is not duplicated."""
        adapter = ConcreteMCPAdapter(sse_url="http://localhost:8112/sse")
        assert adapter.sse_url == "http://localhost:8112/sse"

    def test_init_strips_trailing_slash(self):
        """Test that trailing slash is handled correctly."""
        adapter = ConcreteMCPAdapter(sse_url="http://localhost:8112/")
        assert adapter.sse_url == "http://localhost:8112/sse"

    @pytest.mark.asyncio
    async def test_call_delegates_to_call_tool(self):
        """Test that call() delegates to call_tool() with kwargs as dict."""
        adapter = ConcreteMCPAdapter(sse_url="http://localhost:8112")
        adapter.call_tool = AsyncMock(return_value={"success": True})

        result = await adapter.call("test_tool", arg1="value1", arg2=123)

        adapter.call_tool.assert_called_once_with(
            "test_tool", {"arg1": "value1", "arg2": 123}
        )
        assert result == {"success": True}

    @pytest.mark.asyncio
    async def test_call_with_no_args(self):
        """Test call() with no arguments."""
        adapter = ConcreteMCPAdapter(sse_url="http://localhost:8112")
        adapter.call_tool = AsyncMock(return_value={"projects": []})

        result = await adapter.call("list_projects")

        adapter.call_tool.assert_called_once_with("list_projects", {})
        assert result == {"projects": []}

    @pytest.mark.asyncio
    async def test_call_tool_returns_envelope_on_upstream_iserror(self):
        """Upstream isError=True surfaces as an error_type envelope.

        Pinned by T-magickit-identity-extension D-7 (i) / D-9: the upstream
        rejection text was previously dropped on the floor (returned as if
        it were a normal text result), which became an empty `message` in
        downstream wrappers (Einstein F-01).
        """
        adapter = ConcreteMCPAdapter(sse_url="http://localhost:8112")

        text_content = MagicMock()
        text_content.text = "Input validation error: 'cli_robot' is not one of [...]"
        mock_result = MagicMock()
        mock_result.isError = True
        mock_result.content = [text_content]

        mock_session = AsyncMock()
        mock_session.call_tool = AsyncMock(return_value=mock_result)
        mock_session.initialize = AsyncMock()

        with patch.object(adapter, "_get_session") as mock_get_session:
            mock_context = AsyncMock()
            mock_context.__aenter__ = AsyncMock(return_value=mock_session)
            mock_context.__aexit__ = AsyncMock()
            mock_get_session.return_value = mock_context

            result = await adapter.call_tool("upsert_identity", {})

        assert isinstance(result, dict)
        assert result["error_type"] == "UpstreamValidationError"
        assert "cli_robot" in result["error"]
        assert result["details"] == {}

    @pytest.mark.asyncio
    async def test_call_tool_returns_envelope_with_empty_text_on_iserror(self):
        """isError with no text content still yields the envelope."""
        adapter = ConcreteMCPAdapter(sse_url="http://localhost:8112")

        mock_result = MagicMock()
        mock_result.isError = True
        mock_result.content = []

        mock_session = AsyncMock()
        mock_session.call_tool = AsyncMock(return_value=mock_result)
        mock_session.initialize = AsyncMock()

        with patch.object(adapter, "_get_session") as mock_get_session:
            mock_context = AsyncMock()
            mock_context.__aenter__ = AsyncMock(return_value=mock_session)
            mock_context.__aexit__ = AsyncMock()
            mock_get_session.return_value = mock_context

            result = await adapter.call_tool("upsert_identity", {})

        assert result["error_type"] == "UpstreamValidationError"
        assert result["error"] == ""

    @pytest.mark.asyncio
    async def test_call_tool_normal_text_unchanged_on_success(self):
        """isError=False returns text content as-is (no envelope)."""
        adapter = ConcreteMCPAdapter(sse_url="http://localhost:8112")

        text_content = MagicMock()
        text_content.text = '{"success": true, "value": 42}'
        mock_result = MagicMock()
        mock_result.isError = False
        mock_result.content = [text_content]

        mock_session = AsyncMock()
        mock_session.call_tool = AsyncMock(return_value=mock_result)
        mock_session.initialize = AsyncMock()

        with patch.object(adapter, "_get_session") as mock_get_session:
            mock_context = AsyncMock()
            mock_context.__aenter__ = AsyncMock(return_value=mock_session)
            mock_context.__aexit__ = AsyncMock()
            mock_get_session.return_value = mock_context

            result = await adapter.call_tool("some_tool", {})

        assert result == '{"success": true, "value": 42}'

    @pytest.mark.asyncio
    async def test_get_tool_schemas(self):
        """Test get_tool_schemas() returns proper schema format."""
        adapter = ConcreteMCPAdapter(sse_url="http://localhost:8112")

        mock_tool = MagicMock()
        mock_tool.name = "test_tool"
        mock_tool.description = "A test tool"
        mock_tool.inputSchema = {"type": "object", "properties": {}}

        mock_result = MagicMock()
        mock_result.tools = [mock_tool]

        mock_session = AsyncMock()
        mock_session.list_tools = AsyncMock(return_value=mock_result)
        mock_session.initialize = AsyncMock()

        with patch.object(adapter, "_get_session") as mock_get_session:
            mock_context = AsyncMock()
            mock_context.__aenter__ = AsyncMock(return_value=mock_session)
            mock_context.__aexit__ = AsyncMock()
            mock_get_session.return_value = mock_context

            schemas = await adapter.get_tool_schemas()

        assert len(schemas) == 1
        assert schemas[0]["name"] == "test_tool"
        assert schemas[0]["description"] == "A test tool"
        assert schemas[0]["inputSchema"] == {"type": "object", "properties": {}}

    @pytest.mark.asyncio
    async def test_batch_call_parallel(self):
        """Test batch_call() executes calls in parallel."""
        adapter = ConcreteMCPAdapter(sse_url="http://localhost:8112")

        call_order = []

        async def mock_call_tool(name, args):
            call_order.append(name)
            await asyncio.sleep(0.01)
            return {name: "result"}

        adapter.call_tool = mock_call_tool

        operations = [
            ("tool1", {"arg": 1}),
            ("tool2", {"arg": 2}),
            ("tool3", {"arg": 3}),
        ]

        results = await adapter.batch_call(operations, parallel=True)

        assert len(results) == 3
        assert results[0] == {"tool1": "result"}
        assert results[1] == {"tool2": "result"}
        assert results[2] == {"tool3": "result"}

    @pytest.mark.asyncio
    async def test_batch_call_sequential(self):
        """Test batch_call() executes calls sequentially when parallel=False."""
        adapter = ConcreteMCPAdapter(sse_url="http://localhost:8112")

        call_order = []

        async def mock_call_tool(name, args):
            call_order.append(name)
            return {name: "result"}

        adapter.call_tool = mock_call_tool

        operations = [
            ("tool1", {}),
            ("tool2", {}),
            ("tool3", {}),
        ]

        results = await adapter.batch_call(operations, parallel=False)

        assert call_order == ["tool1", "tool2", "tool3"]
        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_batch_call_parallel_with_exception(self):
        """Test batch_call() handles exceptions in parallel mode."""
        adapter = ConcreteMCPAdapter(sse_url="http://localhost:8112")

        async def mock_call_tool(name, args):
            if name == "tool2":
                raise ValueError("Tool 2 failed")
            return {name: "result"}

        adapter.call_tool = mock_call_tool

        operations = [
            ("tool1", {}),
            ("tool2", {}),
            ("tool3", {}),
        ]

        results = await adapter.batch_call(operations, parallel=True)

        assert results[0] == {"tool1": "result"}
        assert isinstance(results[1], ValueError)
        assert results[2] == {"tool3": "result"}

    @pytest.mark.asyncio
    async def test_dynamic_method_dispatch(self):
        """Test __getattr__ allows calling tools as methods."""
        adapter = ConcreteMCPAdapter(sse_url="http://localhost:8112")
        adapter.call_tool = AsyncMock(return_value={"success": True})

        # Call tool via __getattr__ (dynamic method)
        result = await adapter.some_tool(arg1="value1", arg2=123)

        adapter.call_tool.assert_called_once_with(
            "some_tool", {"arg1": "value1", "arg2": 123}
        )
        assert result == {"success": True}

    @pytest.mark.asyncio
    async def test_dynamic_method_dispatch_no_args(self):
        """Test __getattr__ works with no arguments."""
        adapter = ConcreteMCPAdapter(sse_url="http://localhost:8112")
        adapter.call_tool = AsyncMock(return_value={"projects": []})

        # Call tool via __getattr__ with no arguments
        result = await adapter.list_projects()

        adapter.call_tool.assert_called_once_with("list_projects", {})
        assert result == {"projects": []}

    def test_private_attr_raises_attribute_error(self):
        """Test that accessing private attributes raises AttributeError."""
        adapter = ConcreteMCPAdapter(sse_url="http://localhost:8112")

        with pytest.raises(AttributeError) as exc_info:
            _ = adapter._private_thing

        assert "'ConcreteMCPAdapter' object has no attribute '_private_thing'" in str(
            exc_info.value
        )

    def test_dunder_attr_raises_attribute_error(self):
        """Test that accessing dunder attributes raises AttributeError."""
        adapter = ConcreteMCPAdapter(sse_url="http://localhost:8112")

        with pytest.raises(AttributeError):
            _ = adapter.__nonexistent__

    @pytest.mark.asyncio
    async def test_dynamic_method_dispatch_multiple_calls(self):
        """Test multiple dynamic method calls work correctly."""
        adapter = ConcreteMCPAdapter(sse_url="http://localhost:8112")
        adapter.call_tool = AsyncMock(side_effect=[{"a": 1}, {"b": 2}, {"c": 3}])

        r1 = await adapter.tool_a()
        r2 = await adapter.tool_b(x="y")
        r3 = await adapter.tool_c(num=42)

        assert r1 == {"a": 1}
        assert r2 == {"b": 2}
        assert r3 == {"c": 3}
        assert adapter.call_tool.call_count == 3


class TestPrismindAdapter:
    """Tests for PrismindAdapter."""

    @pytest.mark.asyncio
    async def test_health_check_success(self):
        """Test health_check returns True when expected tools exist."""
        adapter = PrismindAdapter(sse_url="http://localhost:8112")
        adapter.list_tools = AsyncMock(
            return_value=["search_knowledge", "add_knowledge", "list_projects", "extra"]
        )

        result = await adapter.health_check()

        assert result is True

    @pytest.mark.asyncio
    async def test_health_check_missing_tools(self):
        """Test health_check returns False when tools are missing."""
        adapter = PrismindAdapter(sse_url="http://localhost:8112")
        adapter.list_tools = AsyncMock(return_value=["search_knowledge"])

        result = await adapter.health_check()

        assert result is False

    @pytest.mark.asyncio
    async def test_health_check_exception(self):
        """Test health_check returns False on exception."""
        adapter = PrismindAdapter(sse_url="http://localhost:8112")
        adapter.list_tools = AsyncMock(side_effect=Exception("Connection failed"))

        result = await adapter.health_check()

        assert result is False

    @pytest.mark.asyncio
    async def test_search_returns_documents(self):
        """Test search() returns Document objects."""
        adapter = PrismindAdapter(sse_url="http://localhost:8112")
        adapter._call_tool_safe = AsyncMock(
            return_value=(
                True,
                [
                    {
                        "id": "doc1",
                        "content": "Test content",
                        "category": "test",
                        "tags": ["tag1"],
                        "score": 0.95,
                    }
                ],
            )
        )

        results = await adapter.search("test query", n=5)

        assert len(results) == 1
        assert isinstance(results[0], Document)
        assert results[0].id == "doc1"
        assert results[0].content == "Test content"
        assert results[0].score == 0.95

    @pytest.mark.asyncio
    async def test_get_context_concatenates_documents(self):
        """Test get_context() concatenates document contents."""
        adapter = PrismindAdapter(sse_url="http://localhost:8112")
        adapter.search = AsyncMock(
            return_value=[
                Document(id="1", content="First doc", metadata={}, score=0.9),
                Document(id="2", content="Second doc", metadata={}, score=0.8),
            ]
        )

        result = await adapter.get_context("query", max_tokens=1000)

        assert "First doc" in result
        assert "Second doc" in result
        assert "---" in result  # separator

    @pytest.mark.asyncio
    async def test_generic_call_inherited(self):
        """Test that PrismindAdapter inherits call() from base."""
        adapter = PrismindAdapter(sse_url="http://localhost:8112")
        adapter.call_tool = AsyncMock(return_value='{"projects": []}')

        result = await adapter.call("list_projects")

        adapter.call_tool.assert_called_once_with("list_projects", {})

    @pytest.mark.asyncio
    async def test_upsert_identity_forwards_required_and_optional_fields(self):
        """upsert_identity passes embodiment / independence_class / allowed_roles
        through, plus optional persona_description / user when provided."""
        adapter = PrismindAdapter(sse_url="http://localhost:8112")
        adapter._call_tool_safe = AsyncMock(
            return_value=(
                True,
                {
                    "success": True,
                    "identity": {
                        "identity_name": "Heisenberg",
                        "user": "u",
                        "allowed_roles": ["proposer", "reviewer"],
                        "embodiment": "terminal_coding_agent",
                        "independence_class": "main-chain",
                    },
                    "created": True,
                    "message": "ok",
                },
            )
        )

        result = await adapter.upsert_identity(
            identity_name="Heisenberg",
            embodiment="terminal_coding_agent",
            independence_class="main-chain",
            allowed_roles=["proposer", "reviewer"],
            persona_description="Heisenberg",
            user="u",
        )

        adapter._call_tool_safe.assert_called_once()
        tool_name, args = adapter._call_tool_safe.call_args[0]
        assert tool_name == "upsert_identity"
        assert args["identity_name"] == "Heisenberg"
        assert args["embodiment"] == "terminal_coding_agent"
        assert args["independence_class"] == "main-chain"
        assert args["allowed_roles"] == ["proposer", "reviewer"]
        assert args["persona_description"] == "Heisenberg"
        assert args["user"] == "u"
        # keep_allowed_roles=False default is omitted from the wire
        assert "keep_allowed_roles" not in args
        assert result["success"] is True
        assert result["created"] is True

    @pytest.mark.asyncio
    async def test_upsert_identity_omits_optional_when_none(self):
        """None for allowed_roles and persona_description is omitted (preserve)."""
        adapter = PrismindAdapter(sse_url="http://localhost:8112")
        adapter._call_tool_safe = AsyncMock(
            return_value=(True, {"success": True, "created": False})
        )

        await adapter.upsert_identity(
            identity_name="Heisenberg",
            embodiment="terminal_coding_agent",
            independence_class="main-chain",
            keep_allowed_roles=True,
        )

        _, args = adapter._call_tool_safe.call_args[0]
        # Required fields present, optional fields absent
        assert args == {
            "identity_name": "Heisenberg",
            "embodiment": "terminal_coding_agent",
            "independence_class": "main-chain",
            "keep_allowed_roles": True,
        }

    @pytest.mark.asyncio
    async def test_upsert_identity_empty_list_clears(self):
        """allowed_roles=[] is forwarded as an empty list (explicit clear)."""
        adapter = PrismindAdapter(sse_url="http://localhost:8112")
        adapter._call_tool_safe = AsyncMock(
            return_value=(True, {"success": True})
        )

        await adapter.upsert_identity(
            identity_name="silent-actor",
            embodiment="web_ai_chat",
            independence_class="independent",
            allowed_roles=[],
        )

        _, args = adapter._call_tool_safe.call_args[0]
        assert args["allowed_roles"] == []

    @pytest.mark.asyncio
    async def test_upsert_identity_requires_name(self):
        """Empty identity_name raises ValueError before any tool call."""
        adapter = PrismindAdapter(sse_url="http://localhost:8112")
        adapter._call_tool_safe = AsyncMock()

        with pytest.raises(ValueError):
            await adapter.upsert_identity(
                identity_name="",
                embodiment="web_ai_chat",
                independence_class="main-chain",
                allowed_roles=["proposer"],
            )

        adapter._call_tool_safe.assert_not_called()

    @pytest.mark.asyncio
    async def test_upsert_identity_no_embodiment_raise(self):
        """ADR-12 partial-rollback of F-03: omitting embodiment is now OK
        (the field is deprecated and optional). identity_name and
        independence_class still raise symmetrically."""
        adapter = PrismindAdapter(sse_url="http://localhost:8112")
        adapter._call_tool_safe = AsyncMock(
            return_value=(True, {"success": True, "identity": {}, "created": True})
        )

        # No raise -- the call proceeds through to the wire with embodiment
        # absent from the arguments (the adapter forwards None as omit).
        await adapter.upsert_identity(
            identity_name="Heisenberg",
            independence_class="main-chain",
            allowed_roles=["proposer"],
        )

        adapter._call_tool_safe.assert_called_once()
        _, args = adapter._call_tool_safe.call_args[0]
        assert "embodiment" not in args

    @pytest.mark.asyncio
    async def test_upsert_identity_requires_independence_class(self):
        """Empty independence_class raises ValueError before any tool call (F-03)."""
        adapter = PrismindAdapter(sse_url="http://localhost:8112")
        adapter._call_tool_safe = AsyncMock()

        with pytest.raises(ValueError, match="independence_class"):
            await adapter.upsert_identity(
                identity_name="Heisenberg",
                embodiment="web_ai_chat",
                independence_class="",
                allowed_roles=["proposer"],
            )

        adapter._call_tool_safe.assert_not_called()
