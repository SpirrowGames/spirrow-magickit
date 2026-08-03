"""Human-facing web surface served by Magickit.

Magickit is the front door for the browser (see ``chatroom_proxy`` for
why). Conclair stays a loopback-only leaf service.
"""

from __future__ import annotations

from magickit.web.chatroom_proxy import close_client, router

__all__ = ["router", "close_client"]
