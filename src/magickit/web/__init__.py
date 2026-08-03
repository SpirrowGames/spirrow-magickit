"""Human-facing web surface served by Magickit.

Magickit is the front door for the browser (see ``chatroom_proxy`` for
why). Conclair stays a loopback-only leaf service.
"""

from __future__ import annotations

from magickit.web.chatroom_dashboard import router as dashboard_router
from magickit.web.chatroom_proxy import close_client, router
from magickit.web.chatroom_writes import router as writes_router

__all__ = ["router", "writes_router", "dashboard_router", "close_client"]
