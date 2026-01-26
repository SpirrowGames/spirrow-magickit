"""MCP Server entry point for Magickit.

This module exposes Magickit's orchestration capabilities as an MCP server,
allowing Claude Code and other MCP clients to use multi-service workflows.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastmcp import FastMCP

from magickit.config import get_settings
from magickit.utils.logging import configure_logging, get_logger

# Import tool modules (will be registered via decorators)
from magickit.mcp.tools import health, research, orchestration, generation, session

logger = get_logger(__name__)


def create_mcp_server() -> FastMCP:
    """Create and configure the FastMCP server.

    Returns:
        Configured FastMCP server instance.
    """
    settings = get_settings()

    # Configure logging
    configure_logging(
        level=settings.log_level,
        format_type=settings.log_format,
    )

    # Create MCP server
    mcp = FastMCP(
        name="magickit",
        instructions="""Magickit is an orchestration layer for the Spirrow Platform.
It provides tools that combine multiple services (Cognilens, Prismind, Lexora)
into optimized workflows. Use these tools when you need multi-service operations
rather than calling individual services separately.""",
    )

    # Register tools from modules
    health.register_tools(mcp, settings)
    research.register_tools(mcp, settings)
    orchestration.register_tools(mcp, settings)
    generation.register_tools(mcp, settings)
    session.register_tools(mcp, settings)

    logger.info(
        "MCP server created",
        name="magickit",
        cognilens_url=settings.cognilens_url,
        prismind_url=settings.prismind_url,
        lexora_url=settings.lexora_url,
    )

    return mcp


# Global MCP instance
mcp = create_mcp_server()


def main() -> None:
    """Run the MCP server."""
    settings = get_settings()

    # Get MCP port from config (default 8114)
    mcp_port = getattr(settings, "mcp_port", 8114)

    logger.info(
        "Starting Magickit MCP server",
        host=settings.host,
        port=mcp_port,
    )

    # Run SSE server
    mcp.run(
        transport="sse",
        host=settings.host,
        port=mcp_port,
    )


if __name__ == "__main__":
    main()
