"""Integration tests for Prismind adapter with real MCP server.

Requires Prismind MCP server running on localhost:8112.
Run with: pytest tests/integration/test_prismind_adapter.py -v
"""

import pytest

from magickit.adapters.prismind import PrismindAdapter

PRISMIND_SSE_URL = "http://localhost:8112/sse"


@pytest.fixture
def adapter():
    """Create Prismind adapter instance."""
    return PrismindAdapter(sse_url=PRISMIND_SSE_URL)


class TestPrismindIntegration:
    """Integration tests for Prismind adapter."""

    @pytest.mark.asyncio
    async def test_health_check(self, adapter):
        """Test health check against real server."""
        result = await adapter.health_check()
        assert result is True, "Prismind server should be healthy"

    @pytest.mark.asyncio
    async def test_list_tools(self, adapter):
        """Test listing available tools."""
        tools = await adapter.list_tools()
        assert isinstance(tools, list)
        assert len(tools) > 0
        # Check expected tools exist
        assert "search_knowledge" in tools
        assert "add_knowledge" in tools
        assert "list_projects" in tools
        print(f"\nAvailable tools ({len(tools)}): {tools}")

    @pytest.mark.asyncio
    async def test_get_tool_schemas(self, adapter):
        """Test getting tool schemas."""
        schemas = await adapter.get_tool_schemas()
        assert isinstance(schemas, list)
        assert len(schemas) > 0

        # Verify schema structure
        for schema in schemas:
            assert "name" in schema
            assert "description" in schema
            assert "inputSchema" in schema

        # Print schema summary
        print(f"\nTool schemas ({len(schemas)}):")
        for s in schemas:
            print(f"  - {s['name']}: {s['description'][:60]}...")

    @pytest.mark.asyncio
    async def test_generic_call_list_projects(self, adapter):
        """Test generic call() method with list_projects."""
        result = await adapter.call("list_projects")
        assert result is not None
        print(f"\nlist_projects result: {result}")

    @pytest.mark.asyncio
    async def test_generic_call_get_setup_status(self, adapter):
        """Test generic call() method with get_setup_status."""
        result = await adapter.call("get_setup_status")
        assert result is not None
        print(f"\nget_setup_status result: {result}")

    @pytest.mark.asyncio
    async def test_batch_call_parallel(self, adapter):
        """Test batch_call() with parallel execution."""
        operations = [
            ("list_projects", {}),
            ("get_setup_status", {}),
        ]

        results = await adapter.batch_call(operations, parallel=True)

        assert len(results) == 2
        assert results[0] is not None  # list_projects result
        assert results[1] is not None  # get_setup_status result
        print(f"\nBatch call results: {results}")

    @pytest.mark.asyncio
    async def test_batch_call_sequential(self, adapter):
        """Test batch_call() with sequential execution."""
        operations = [
            ("list_projects", {}),
            ("get_setup_status", {}),
        ]

        results = await adapter.batch_call(operations, parallel=False)

        assert len(results) == 2
        assert results[0] is not None
        assert results[1] is not None

    @pytest.mark.asyncio
    async def test_search_knowledge(self, adapter):
        """Test search() method."""
        # This may return empty if no knowledge exists
        results = await adapter.search("test", n=3)
        assert isinstance(results, list)
        print(f"\nSearch results: {len(results)} documents")

    @pytest.mark.asyncio
    async def test_generic_call_search_knowledge(self, adapter):
        """Test generic call for search_knowledge."""
        result = await adapter.call(
            "search_knowledge",
            query="test",
            limit=3,
        )
        assert result is not None
        print(f"\nGeneric search result: {result}")

    @pytest.mark.asyncio
    async def test_generic_call_check_services_status(self, adapter):
        """Test calling check_services_status via generic call."""
        result = await adapter.call("check_services_status", detailed=True)
        assert result is not None
        print(f"\nServices status: {result}")

    # Phase 2: Dynamic method dispatch tests (__getattr__)

    @pytest.mark.asyncio
    async def test_dynamic_call_list_projects(self, adapter):
        """Test calling list_projects via __getattr__ (dynamic method)."""
        result = await adapter.list_projects()
        assert result is not None
        print(f"\nDynamic list_projects result: {result}")

    @pytest.mark.asyncio
    async def test_dynamic_call_get_setup_status(self, adapter):
        """Test calling get_setup_status via __getattr__ (dynamic method)."""
        result = await adapter.get_setup_status()
        assert result is not None
        print(f"\nDynamic get_setup_status result: {result}")

    @pytest.mark.asyncio
    async def test_dynamic_call_search_knowledge(self, adapter):
        """Test calling search_knowledge via __getattr__ with arguments."""
        result = await adapter.search_knowledge(query="test", limit=3)
        assert result is not None
        print(f"\nDynamic search_knowledge result: {result}")

    @pytest.mark.asyncio
    async def test_dynamic_call_check_services_status(self, adapter):
        """Test calling check_services_status via __getattr__."""
        result = await adapter.check_services_status(detailed=True)
        assert result is not None
        print(f"\nDynamic services status: {result}")
