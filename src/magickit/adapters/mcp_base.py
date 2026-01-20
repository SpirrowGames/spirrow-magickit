"""Base adapter class for MCP-based services."""

import asyncio
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Callable, Coroutine

from mcp import ClientSession
from mcp.client.sse import sse_client

from magickit.utils.logging import get_logger

logger = get_logger(__name__)


class MCPBaseAdapter(ABC):
    """Abstract base class for MCP service adapters.

    Provides common functionality for connecting to MCP servers
    via SSE transport and calling tools.
    """

    def __init__(self, sse_url: str, timeout: float = 30.0) -> None:
        """Initialize the MCP adapter.

        Args:
            sse_url: SSE endpoint URL of the MCP server.
            timeout: Request timeout in seconds.
        """
        self.sse_url = sse_url.rstrip("/")
        if not self.sse_url.endswith("/sse"):
            self.sse_url = f"{self.sse_url}/sse"
        self.timeout = timeout

    @asynccontextmanager
    async def _get_session(self) -> AsyncGenerator[ClientSession, None]:
        """Create a new MCP session within a context manager.

        Yields:
            Connected MCP ClientSession.
        """
        logger.debug("Connecting to MCP server", url=self.sse_url)
        async with sse_client(self.sse_url) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                logger.debug("MCP session initialized", url=self.sse_url)
                yield session

    async def __aenter__(self) -> "MCPBaseAdapter":
        """Async context manager entry."""
        return self

    async def __aexit__(self, *args: Any) -> None:
        """Async context manager exit."""
        pass

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Call an MCP tool.

        Args:
            name: Tool name.
            arguments: Tool arguments.

        Returns:
            Tool result content.

        Raises:
            Exception: If tool call fails.
        """
        async with self._get_session() as session:
            logger.debug("Calling MCP tool", tool=name, arguments=arguments)
            result = await session.call_tool(name, arguments)

            # Extract text content from result
            if result.content:
                for content in result.content:
                    if hasattr(content, "text"):
                        return content.text
                return result.content[0] if result.content else None

            return None

    async def list_tools(self) -> list[str]:
        """List available tools.

        Returns:
            List of tool names.
        """
        async with self._get_session() as session:
            result = await session.list_tools()
            return [tool.name for tool in result.tools]

    async def call(self, tool_name: str, **kwargs: Any) -> Any:
        """Generic tool call with keyword arguments.

        Allows calling any MCP tool without explicit wrapper.
        Example: await adapter.call("start_session", project="my-project")

        Args:
            tool_name: Name of the tool to call.
            **kwargs: Tool arguments as keyword arguments.

        Returns:
            Tool result content.
        """
        return await self.call_tool(tool_name, kwargs)

    def __getattr__(self, name: str) -> Callable[..., Coroutine[Any, Any, Any]]:
        """Dynamic method dispatch for MCP tools.

        Allows calling any MCP tool as a method:
            await adapter.start_session(project="foo")
        Instead of:
            await adapter.call("start_session", project="foo")

        Args:
            name: Tool name (method name).

        Returns:
            Async callable that invokes the tool.

        Raises:
            AttributeError: If name starts with underscore (private attribute).
        """
        # Avoid infinite recursion for special attributes
        if name.startswith("_"):
            raise AttributeError(
                f"'{type(self).__name__}' object has no attribute '{name}'"
            )

        async def method(**kwargs: Any) -> Any:
            return await self.call(name, **kwargs)

        return method

    async def get_tool_schemas(self) -> list[dict[str, Any]]:
        """Get tool schemas for discovery.

        Returns list of {name, description, inputSchema} for each tool.
        Useful for Claude and other LLMs to understand available tools.

        Returns:
            List of tool schema dictionaries.
        """
        async with self._get_session() as session:
            result = await session.list_tools()
            return [
                {
                    "name": t.name,
                    "description": t.description,
                    "inputSchema": t.inputSchema,
                }
                for t in result.tools
            ]

    async def batch_call(
        self,
        operations: list[tuple[str, dict[str, Any]]],
        parallel: bool = True,
    ) -> list[Any]:
        """Execute multiple tool calls, optionally in parallel.

        Args:
            operations: List of (tool_name, arguments) tuples.
            parallel: If True, execute all calls concurrently.

        Returns:
            List of results in the same order as operations.
            If parallel=True and a call fails, the result will be an Exception.
        """
        if parallel:
            tasks = [self.call_tool(name, args) for name, args in operations]
            return await asyncio.gather(*tasks, return_exceptions=True)
        else:
            results = []
            for name, args in operations:
                results.append(await self.call_tool(name, args))
            return results

    @abstractmethod
    async def health_check(self) -> bool:
        """Check if the service is healthy.

        Returns:
            True if the service is healthy, False otherwise.
        """
        pass

    async def _call_tool_safe(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> tuple[bool, Any]:
        """Call a tool with error handling.

        Args:
            name: Tool name.
            arguments: Tool arguments.

        Returns:
            Tuple of (success, result or error message).
        """
        try:
            result = await self.call_tool(name, arguments)
            return True, result
        except Exception as e:
            logger.error("MCP tool call failed", tool=name, error=str(e))
            return False, str(e)
