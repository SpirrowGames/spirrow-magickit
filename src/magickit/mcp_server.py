"""MCP Server entry point for Magickit.

This module exposes Magickit's orchestration capabilities as an MCP server,
allowing Claude Code and other MCP clients to use multi-service workflows.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastmcp import FastMCP

from magickit.config import get_settings

# Import tool modules (will be registered via decorators)
from magickit.mcp.tools import (
    chatroom,
    document,
    document_maintenance,
    execution,
    generation,
    health,
    lifecycle,
    orchestration,
    progress,
    project,
    quality,
    reporting,
    research,
    session,
    smart_read,
    specification,
    task,
)
from magickit.utils.logging import configure_logging, get_logger

logger = get_logger(__name__)


def _build_auth_provider():
    """Build the FastMCP auth provider.

    Returns None when MAGICKIT_AUTH_DISABLED=1 so the server can boot before
    GCP OAuth credentials are provisioned.
    """
    if os.environ.get("MAGICKIT_AUTH_DISABLED") == "1":
        logger.warning("Auth disabled via MAGICKIT_AUTH_DISABLED=1")
        return None

    from pathlib import Path

    from fastmcp.server.auth.providers.google import GoogleProvider
    from key_value.aio.stores.disk import DiskStore

    # GoogleProvider's default storage uses platformdirs (~/.local/share/...),
    # which is read-only under systemd ProtectHome=read-only. Pin storage under
    # an explicit ReadWritePaths-allowed location instead.
    storage_dir = Path(
        os.environ.get(
            "MAGICKIT_OAUTH_STORAGE_DIR",
            "/home/sgadmin/services/spirrow/spirrow-magickit/data/oauth-storage",
        )
    )
    storage_dir.mkdir(parents=True, exist_ok=True)

    return GoogleProvider(
        client_id=os.environ["GOOGLE_OAUTH_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_OAUTH_CLIENT_SECRET"],
        base_url=os.environ["GOOGLE_OAUTH_BASE_URL"],
        required_scopes=[
            "openid",
            "https://www.googleapis.com/auth/userinfo.email",
        ],
        allowed_client_redirect_uris=[
            "https://claude.ai/api/mcp/auth_callback",
            "https://claude.com/api/mcp/auth_callback",
        ],
        jwt_signing_key=os.environ["JWT_SIGNING_KEY"],
        client_storage=DiskStore(directory=storage_dir),
    )


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
        auth=_build_auth_provider(),
    )

    # Register tools from modules
    health.register_tools(mcp, settings)
    research.register_tools(mcp, settings)
    orchestration.register_tools(mcp, settings)
    generation.register_tools(mcp, settings)
    session.register_tools(mcp, settings)
    project.register_tools(mcp, settings)
    document.register_tools(mcp, settings)
    document_maintenance.register_tools(mcp, settings)
    specification.register_tools(mcp, settings)
    execution.register_tools(mcp, settings)
    task.register_tools(mcp, settings)
    lifecycle.register_tools(mcp, settings)
    progress.register_tools(mcp, settings)
    quality.register_tools(mcp, settings)
    reporting.register_tools(mcp, settings)
    smart_read.register_tools(mcp, settings)
    chatroom.register_tools(mcp, settings)

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


def _run_dual(host: str, port: int) -> None:
    """Serve Streamable HTTP (/mcp) and legacy SSE (/sse, /messages/) in one process."""
    import uvicorn
    from starlette.applications import Starlette

    http_app = mcp.http_app(transport="http")  # exposes /mcp
    sse_app = mcp.http_app(transport="sse")    # exposes /sse and /messages/

    # http_app() calls auth.set_mcp_path() as a side effect; the sse call would
    # otherwise overwrite the auth's resource URL to /sse, causing OAuth clients
    # that request resource=<base>/mcp (e.g. claude.ai) to fail with invalid_target.
    if mcp.auth is not None:
        mcp.auth.set_mcp_path("/mcp")

    @asynccontextmanager
    async def lifespan(app):
        async with http_app.router.lifespan_context(app):
            async with sse_app.router.lifespan_context(app):
                yield

    app = Starlette(
        routes=list(http_app.routes) + list(sse_app.routes),
        lifespan=lifespan,
    )

    uvicorn.run(app, host=host, port=port, log_config=None)


def main() -> None:
    """Run the MCP server."""
    settings = get_settings()

    host = os.environ.get("MAGICKIT_HOST", settings.host)
    port = int(os.environ.get("MAGICKIT_MCP_PORT", getattr(settings, "mcp_port", 8114)))
    transport_mode = os.environ.get("MAGICKIT_TRANSPORT_MODE", "dual")

    logger.info(
        "Starting Magickit MCP server",
        host=host,
        port=port,
        transport=transport_mode,
        auth_disabled=os.environ.get("MAGICKIT_AUTH_DISABLED") == "1",
    )

    if transport_mode == "http":
        mcp.run(transport="http", host=host, port=port)
    elif transport_mode == "sse":
        mcp.run(transport="sse", host=host, port=port)
    elif transport_mode == "dual":
        _run_dual(host, port)
    else:
        raise ValueError(f"Unknown MAGICKIT_TRANSPORT_MODE: {transport_mode}")


if __name__ == "__main__":
    main()
