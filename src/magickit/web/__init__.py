"""Human-facing web surface served by Magickit.

Magickit is the front door for the browser (see ``chatroom_proxy`` for
why). Conclair stays a loopback-only leaf service.
"""

from __future__ import annotations

from magickit.web.board import router as board_router
from magickit.web.chatroom_dashboard import router as dashboard_router
from magickit.web.chatroom_digest import router as digest_router
from magickit.web.chatroom_proxy import close_client, router
from magickit.web.chatroom_writes import router as writes_router
from magickit.web.decisions import router as decisions_router
from magickit.web.deploys import router as deploys_router
from magickit.web.ops import router as ops_router

__all__ = [
    "router",
    "writes_router",
    "dashboard_router",
    "digest_router",
    "decisions_router",
    "deploys_router",
    "ops_router",
    "board_router",
    "close_client",
]
