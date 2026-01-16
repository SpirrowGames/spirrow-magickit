"""Slack webhook adapter for notifications."""

from __future__ import annotations

from typing import Any

import httpx

from magickit.adapters.base import BaseAdapter
from magickit.api.models import EventType, TaskStatus
from magickit.utils.logging import get_logger

logger = get_logger(__name__)


class SlackAdapter(BaseAdapter):
    """Adapter for sending notifications to Slack via webhooks.

    Formats task events into Slack-compatible message blocks.
    """

    def __init__(
        self,
        webhook_url: str,
        timeout: float = 10.0,
        max_retries: int = 3,
    ) -> None:
        """Initialize Slack adapter.

        Args:
            webhook_url: Slack incoming webhook URL.
            timeout: Request timeout in seconds.
            max_retries: Maximum retry attempts.
        """
        # BaseAdapter expects base_url, but webhooks use full URL
        super().__init__(base_url="", timeout=timeout)
        self.webhook_url = webhook_url
        self.max_retries = max_retries

    async def health_check(self) -> bool:
        """Check if webhook URL is valid (doesn't actually call Slack).

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
        """Send a task notification to Slack.

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
        """Send a message to the Slack webhook.

        Args:
            payload: Slack message payload.

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
                        "slack_notification_sent",
                        status_code=response.status_code,
                    )
                    return True

            except httpx.HTTPError as e:
                logger.warning(
                    "slack_notification_failed",
                    attempt=attempt + 1,
                    max_retries=self.max_retries,
                    error=str(e),
                )
                if attempt == self.max_retries - 1:
                    logger.error(
                        "slack_notification_exhausted",
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
        """Format a Slack message with blocks.

        Args:
            event_type: Event type.
            task_id: Task ID.
            task_name: Task name.
            project_name: Optional project name.
            details: Additional details.

        Returns:
            Slack message payload.
        """
        emoji = self._get_event_emoji(event_type)
        color = self._get_event_color(event_type)
        status_text = event_type.value.capitalize()

        # Build location text
        location = f"in *{project_name}*" if project_name else ""

        blocks: list[dict[str, Any]] = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"{emoji} *Task {status_text}* {location}",
                },
            },
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": f"*Task:*\n{task_name}",
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*ID:*\n`{task_id[:8]}...`",
                    },
                ],
            },
        ]

        # Add details if present
        if details:
            detail_items = []
            if "error" in details:
                detail_items.append(f"*Error:* {details['error']}")
            if "result" in details:
                result_preview = str(details["result"])[:100]
                detail_items.append(f"*Result:* {result_preview}")
            if "user" in details:
                detail_items.append(f"*By:* {details['user']}")

            if detail_items:
                blocks.append({
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "\n".join(detail_items),
                    },
                })

        return {
            "attachments": [
                {
                    "color": color,
                    "blocks": blocks,
                }
            ]
        }

    def _get_event_emoji(self, event_type: EventType) -> str:
        """Get emoji for event type.

        Args:
            event_type: Event type.

        Returns:
            Emoji string.
        """
        emoji_map = {
            EventType.CREATED: ":new:",
            EventType.STARTED: ":arrow_forward:",
            EventType.COMPLETED: ":white_check_mark:",
            EventType.FAILED: ":x:",
            EventType.CANCELLED: ":no_entry_sign:",
            EventType.UPDATED: ":pencil:",
            EventType.ASSIGNED: ":bust_in_silhouette:",
            EventType.COMMENT: ":speech_balloon:",
        }
        return emoji_map.get(event_type, ":bell:")

    def _get_event_color(self, event_type: EventType) -> str:
        """Get color for event type (Slack attachment color).

        Args:
            event_type: Event type.

        Returns:
            Hex color string.
        """
        color_map = {
            EventType.CREATED: "#36a64f",  # Green
            EventType.STARTED: "#2196F3",  # Blue
            EventType.COMPLETED: "#4CAF50",  # Green
            EventType.FAILED: "#f44336",  # Red
            EventType.CANCELLED: "#9E9E9E",  # Gray
            EventType.UPDATED: "#FF9800",  # Orange
            EventType.ASSIGNED: "#9C27B0",  # Purple
            EventType.COMMENT: "#00BCD4",  # Cyan
        }
        return color_map.get(event_type, "#607D8B")
