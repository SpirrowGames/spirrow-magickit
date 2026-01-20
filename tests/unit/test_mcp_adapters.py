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
