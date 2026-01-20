"""Base adapter class for MCP-based services."""

from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator

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
