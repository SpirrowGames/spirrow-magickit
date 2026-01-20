"""Health monitoring tools for Magickit MCP server.

Provides unified health status for all Spirrow Platform services.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from fastmcp import FastMCP

from magickit.adapters.cognilens import CognilensAdapter
from magickit.adapters.prismind import PrismindAdapter
from magickit.adapters.lexora import LexoraAdapter
from magickit.config import Settings
from magickit.utils.logging import get_logger

logger = get_logger(__name__)

# Module-level settings reference
_settings: Settings | None = None


def register_tools(mcp: FastMCP, settings: Settings) -> None:
    """Register health tools with the MCP server.

    Args:
        mcp: FastMCP server instance.
        settings: Application settings.
    """
    global _settings
    _settings = settings

    @mcp.tool()
    async def service_health() -> dict[str, Any]:
        """Check health status of all Spirrow Platform services in a single call.

        USE THIS WHEN: you need to verify service availability before operations,
        diagnose connectivity issues, or get a quick overview of system status.

        DO NOT USE WHEN:
        - You only need to check one specific service → just call that service directly
        - You're in the middle of an operation that already confirmed connectivity

        Returns:
            Health status for each service including:
            - status: "healthy", "degraded", or "unhealthy"
            - services: Individual service statuses with response times
            - timestamp: When the check was performed
        """
        if _settings is None:
            raise RuntimeError("Settings not initialized")

        results: dict[str, Any] = {
            "timestamp": datetime.utcnow().isoformat(),
            "services": {},
            "status": "healthy",
        }

        # Check all services concurrently
        checks = await asyncio.gather(
            _check_cognilens(_settings),
            _check_prismind(_settings),
            _check_lexora(_settings),
            return_exceptions=True,
        )

        service_names = ["cognilens", "prismind", "lexora"]
        healthy_count = 0

        for name, result in zip(service_names, checks):
            if isinstance(result, Exception):
                results["services"][name] = {
                    "status": "error",
                    "error": str(result),
                }
            else:
                results["services"][name] = result
                if result.get("status") == "healthy":
                    healthy_count += 1

        # Determine overall status
        if healthy_count == len(service_names):
            results["status"] = "healthy"
        elif healthy_count > 0:
            results["status"] = "degraded"
        else:
            results["status"] = "unhealthy"

        logger.info(
            "Health check completed",
            status=results["status"],
            healthy_count=healthy_count,
        )

        return results


async def _check_cognilens(settings: Settings) -> dict[str, Any]:
    """Check Cognilens service health."""
    start = asyncio.get_event_loop().time()
    try:
        adapter = CognilensAdapter(
            sse_url=settings.cognilens_url,
            timeout=settings.cognilens_timeout,
        )
        healthy = await adapter.health_check()
        elapsed = asyncio.get_event_loop().time() - start

        if healthy:
            tools = await adapter.list_tools()
            return {
                "status": "healthy",
                "response_time_ms": round(elapsed * 1000, 2),
                "url": settings.cognilens_url,
                "available_tools": tools,
            }
        else:
            return {
                "status": "unhealthy",
                "response_time_ms": round(elapsed * 1000, 2),
                "url": settings.cognilens_url,
                "error": "Health check returned false",
            }
    except Exception as e:
        elapsed = asyncio.get_event_loop().time() - start
        return {
            "status": "error",
            "response_time_ms": round(elapsed * 1000, 2),
            "url": settings.cognilens_url,
            "error": str(e),
        }


async def _check_prismind(settings: Settings) -> dict[str, Any]:
    """Check Prismind service health."""
    start = asyncio.get_event_loop().time()
    try:
        adapter = PrismindAdapter(
            sse_url=settings.prismind_url,
            timeout=settings.prismind_timeout,
        )
        healthy = await adapter.health_check()
        elapsed = asyncio.get_event_loop().time() - start

        if healthy:
            tools = await adapter.list_tools()
            return {
                "status": "healthy",
                "response_time_ms": round(elapsed * 1000, 2),
                "url": settings.prismind_url,
                "available_tools": tools,
            }
        else:
            return {
                "status": "unhealthy",
                "response_time_ms": round(elapsed * 1000, 2),
                "url": settings.prismind_url,
                "error": "Health check returned false",
            }
    except Exception as e:
        elapsed = asyncio.get_event_loop().time() - start
        return {
            "status": "error",
            "response_time_ms": round(elapsed * 1000, 2),
            "url": settings.prismind_url,
            "error": str(e),
        }


async def _check_lexora(settings: Settings) -> dict[str, Any]:
    """Check Lexora service health."""
    start = asyncio.get_event_loop().time()
    try:
        adapter = LexoraAdapter(
            base_url=settings.lexora_url,
            timeout=settings.lexora_timeout,
        )
        healthy = await adapter.health_check()
        elapsed = asyncio.get_event_loop().time() - start

        return {
            "status": "healthy" if healthy else "unhealthy",
            "response_time_ms": round(elapsed * 1000, 2),
            "url": settings.lexora_url,
        }
    except Exception as e:
        elapsed = asyncio.get_event_loop().time() - start
        return {
            "status": "error",
            "response_time_ms": round(elapsed * 1000, 2),
            "url": settings.lexora_url,
            "error": str(e),
        }
