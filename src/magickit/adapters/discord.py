"""Discord webhook adapter for notifications."""

from __future__ import annotations

from typing import Any

import httpx

from magickit.adapters.base import BaseAdapter
from magickit.api.models import EventType
from magickit.utils.logging import get_logger

logger = get_logger(__name__)


class DiscordAdapter(BaseAdapter):
    """Adapter for sending notifications to Discord via webhooks.

    Formats task events into Discord-compatible embeds.
    """

    def __init__(
        self,
        webhook_url: str,
        timeout: float = 10.0,
        max_retries: int = 3,
        bot_name: str = "Magickit",
        avatar_url: str | None = None,
    ) -> None:
        """Initialize Discord adapter.

        Args:
            webhook_url: Discord webhook URL.
            timeout: Request timeout in seconds.
            max_retries: Maximum retry attempts.
            bot_name: Bot username for messages.
            avatar_url: Optional avatar URL for the bot.
        """
        super().__init__(base_url="", timeout=timeout)
        self.webhook_url = webhook_url
        self.max_retries = max_retries
        self.bot_name = bot_name
        self.avatar_url = avatar_url

    async def health_check(self) -> bool:
        """Check if webhook URL is valid.

        Returns:
            True if URL is configured.
        """
        return bool(self.webhook_url)

    async def send_notification(
        self,
        event_type: EventType,
        task_id: str,
        task_name: str,
        project_name: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> bool:
        """Send a task notification to Discord.

        Args:
            event_type: Type of event.
            task_id: Task ID.
            task_name: Task name.
            project_name: Optional project name.
            details: Additional event details.

        Returns:
            True if sent successfully.
        """
        message = self._format_message(
            event_type=event_type,
            task_id=task_id,
            task_name=task_name,
            project_name=project_name,
            details=details,
        )

        return await self._send_webhook(message)

    async def _send_webhook(self, payload: dict[str, Any]) -> bool:
        """Send a message to the Discord webhook.

        Args:
            payload: Discord message payload.

        Returns:
            True if sent successfully.
        """
        for attempt in range(self.max_retries):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.post(
                        self.webhook_url,
                        json=payload,
                    )
                    response.raise_for_status()

                    logger.debug(
                        "discord_notification_sent",
                        status_code=response.status_code,
                    )
                    return True

            except httpx.HTTPError as e:
                logger.warning(
                    "discord_notification_failed",
                    attempt=attempt + 1,
                    max_retries=self.max_retries,
                    error=str(e),
                )
                if attempt == self.max_retries - 1:
                    logger.error(
                        "discord_notification_exhausted",
                        error=str(e),
                    )
                    return False

        return False

    def _format_message(
        self,
        event_type: EventType,
        task_id: str,
        task_name: str,
        project_name: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Format a Discord message with embeds.

        Args:
            event_type: Event type.
            task_id: Task ID.
            task_name: Task name.
            project_name: Optional project name.
            details: Additional details.

        Returns:
            Discord message payload.
        """
        emoji = self._get_event_emoji(event_type)
        color = self._get_event_color(event_type)
        status_text = event_type.value.capitalize()

        # Build embed fields
        fields: list[dict[str, Any]] = [
            {
                "name": "Task",
                "value": task_name,
                "inline": True,
            },
            {
                "name": "ID",
                "value": f"`{task_id[:8]}...`",
                "inline": True,
            },
        ]

        if project_name:
            fields.append({
                "name": "Project",
                "value": project_name,
                "inline": True,
            })

        # Add details
        if details:
            if "error" in details:
                fields.append({
                    "name": "Error",
                    "value": f"```{details['error'][:200]}```",
                    "inline": False,
                })
            if "result" in details:
                result_preview = str(details["result"])[:200]
                fields.append({
                    "name": "Result",
                    "value": result_preview,
                    "inline": False,
                })
            if "user" in details:
                fields.append({
                    "name": "By",
                    "value": details["user"],
                    "inline": True,
                })

        embed: dict[str, Any] = {
            "title": f"{emoji} Task {status_text}",
            "color": color,
            "fields": fields,
            "footer": {
                "text": "Magickit Task Orchestrator",
            },
        }

        payload: dict[str, Any] = {
            "username": self.bot_name,
            "embeds": [embed],
        }

        if self.avatar_url:
            payload["avatar_url"] = self.avatar_url

        return payload

    def _get_event_emoji(self, event_type: EventType) -> str:
        """Get emoji for event type.

        Args:
            event_type: Event type.

        Returns:
            Emoji string.
        """
        emoji_map = {
            EventType.CREATED: "\U0001F195",  # NEW
            EventType.STARTED: "\u25B6\uFE0F",  # Play
            EventType.COMPLETED: "\u2705",  # Check
            EventType.FAILED: "\u274C",  # X
            EventType.CANCELLED: "\U0001F6AB",  # No entry
            EventType.UPDATED: "\u270F\uFE0F",  # Pencil
            EventType.ASSIGNED: "\U0001F464",  # Person
            EventType.COMMENT: "\U0001F4AC",  # Speech bubble
        }
        return emoji_map.get(event_type, "\U0001F514")  # Bell

    def _get_event_color(self, event_type: EventType) -> int:
        """Get color for event type (Discord embed color as int).

        Args:
            event_type: Event type.

        Returns:
            Color as integer.
        """
        color_map = {
            EventType.CREATED: 0x36A64F,  # Green
            EventType.STARTED: 0x2196F3,  # Blue
            EventType.COMPLETED: 0x4CAF50,  # Green
            EventType.FAILED: 0xF44336,  # Red
            EventType.CANCELLED: 0x9E9E9E,  # Gray
            EventType.UPDATED: 0xFF9800,  # Orange
            EventType.ASSIGNED: 0x9C27B0,  # Purple
            EventType.COMMENT: 0x00BCD4,  # Cyan
        }
        return color_map.get(event_type, 0x607D8B)
