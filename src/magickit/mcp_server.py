"""MCP Server entry point for Magickit.

This module exposes Magickit's orchestration capabilities as an MCP server,
allowing Claude Code and other MCP clients to use multi-service workflows.
"""

from __future__ import annotations

import os

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


def _mount_github_proxy(mcp: FastMCP) -> None:
    """Mount the local github-mcp container as a proxied sub-server.

    No-op unless GITHUB_MCP_PAT is set, so the no-auth tailnet instance and
    the test suite are unaffected. The github-mcp container runs in HTTP mode
    and expects the GitHub token in the per-request Authorization header; we
    inject it here so the public OAuth-gated Magickit endpoint is the only
    way in. The container is started with --dynamic-toolsets, so only a small
    set of meta-tools is exposed until a toolset is enabled on demand.

    Args:
        mcp: The Magickit FastMCP server to mount the proxy onto.
    """
    pat = os.environ.get("GITHUB_MCP_PAT")
    if not pat:
        logger.info("github-mcp proxy disabled (GITHUB_MCP_PAT unset)")
        return

    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    url = os.environ.get("GITHUB_MCP_URL", "http://127.0.0.1:8116/mcp")
    github_proxy = FastMCP.as_proxy(
        Client(
            StreamableHttpTransport(
                url,
                headers={"Authorization": f"Bearer {pat}"},
            )
        )
    )
    mcp.mount(github_proxy, prefix="github")
    logger.info("github-mcp proxy mounted", url=url, prefix="github")


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

    # Mount the local github-mcp container (no-op unless GITHUB_MCP_PAT set)
    _mount_github_proxy(mcp)

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

    host = os.environ.get("MAGICKIT_HOST", settings.host)
    port = int(os.environ.get("MAGICKIT_MCP_PORT", getattr(settings, "mcp_port", 8114)))
    transport_mode = os.environ.get("MAGICKIT_TRANSPORT_MODE", "http")

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
    else:
        raise ValueError(f"Unknown MAGICKIT_TRANSPORT_MODE: {transport_mode}")


if __name__ == "__main__":
    main()
