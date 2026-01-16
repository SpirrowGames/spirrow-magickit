"""Notification manager for orchestrating webhook notifications."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from magickit.adapters.discord import DiscordAdapter
from magickit.adapters.slack import SlackAdapter
from magickit.api.models import EventType, WebhookService
from magickit.utils.logging import get_logger

if TYPE_CHECKING:
    from magickit.api.models import WebhookResponse
    from magickit.core.state_manager import StateManager

logger = get_logger(__name__)


class NotificationManager:
    """Manages sending notifications through various webhook services.

    Queries registered webhooks and dispatches notifications asynchronously.
    """

    def __init__(
        self,
        state_manager: StateManager,
        timeout: float = 10.0,
        max_retries: int = 3,
    ) -> None:
        """Initialize notification manager.

        Args:
            state_manager: State manager for retrieving webhooks.
            timeout: Webhook request timeout.
            max_retries: Maximum retry attempts.
        """
        self._state = state_manager
        self._timeout = timeout
        self._max_retries = max_retries

    async def notify(
        self,
        workspace_id: str,
        event_type: EventType,
        task_id: str,
        task_name: str,
        project_name: str | None = None,
        details: dict[str, Any] | None = None,
        background: bool = True,
    ) -> list[bool]:
        """Send notifications for an event to all registered webhooks.

        Args:
            workspace_id: Workspace ID for webhook lookup.
            event_type: Type of event.
            task_id: Task ID.
            task_name: Task name.
            project_name: Optional project name.
            details: Additional event details.
            background: If True, run notifications in background.

        Returns:
            List of success/failure for each webhook.
        """
        # Get active webhooks for this event type
        webhooks = await self._state.get_active_webhooks_for_event(
            workspace_id, event_type
        )

        if not webhooks:
            logger.debug(
                "no_webhooks_for_event",
                workspace_id=workspace_id,
                event_type=event_type.value,
            )
            return []

        logger.info(
            "sending_notifications",
            workspace_id=workspace_id,
            event_type=event_type.value,
            webhook_count=len(webhooks),
        )

        # Create notification tasks
        tasks = [
            self._send_to_webhook(
                webhook=webhook,
                event_type=event_type,
                task_id=task_id,
                task_name=task_name,
                project_name=project_name,
                details=details,
            )
            for webhook in webhooks
        ]

        if background:
            # Run in background - don't wait for results
            asyncio.create_task(self._run_background_notifications(tasks))
            return []
        else:
            # Wait for all notifications to complete
            results = await asyncio.gather(*tasks, return_exceptions=True)
            return [r is True for r in results]

    async def _run_background_notifications(
        self, tasks: list[asyncio.coroutine]
    ) -> None:
        """Run notification tasks in background.

        Args:
            tasks: List of notification coroutines.
        """
        results = await asyncio.gather(*tasks, return_exceptions=True)

        success_count = sum(1 for r in results if r is True)
        failure_count = len(results) - success_count

        if failure_count > 0:
            logger.warning(
                "background_notifications_partial_failure",
                success=success_count,
                failures=failure_count,
            )
        else:
            logger.debug(
                "background_notifications_complete",
                success=success_count,
            )

    async def _send_to_webhook(
        self,
        webhook: WebhookResponse,
        event_type: EventType,
        task_id: str,
        task_name: str,
        project_name: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> bool:
        """Send notification to a single webhook.

        Args:
            webhook: Webhook configuration.
            event_type: Event type.
            task_id: Task ID.
            task_name: Task name.
            project_name: Optional project name.
            details: Additional details.

        Returns:
            True if sent successfully.
        """
        try:
            adapter = self._create_adapter(webhook.service, webhook.url)

            result = await adapter.send_notification(
                event_type=event_type,
                task_id=task_id,
                task_name=task_name,
                project_name=project_name,
                details=details,
            )

            if result:
                logger.debug(
                    "webhook_notification_sent",
                    webhook_id=webhook.id,
                    service=webhook.service.value,
                )
            else:
                logger.warning(
                    "webhook_notification_failed",
                    webhook_id=webhook.id,
                    service=webhook.service.value,
                )

            return result

        except Exception as e:
            logger.error(
                "webhook_notification_error",
                webhook_id=webhook.id,
                service=webhook.service.value,
                error=str(e),
            )
            return False

    def _create_adapter(
        self, service: WebhookService, url: str
    ) -> SlackAdapter | DiscordAdapter:
        """Create an adapter for the specified service.

        Args:
            service: Webhook service type.
            url: Webhook URL.

        Returns:
            Adapter instance.
        """
        if service == WebhookService.SLACK:
            return SlackAdapter(
                webhook_url=url,
                timeout=self._timeout,
                max_retries=self._max_retries,
            )
        elif service == WebhookService.DISCORD:
            return DiscordAdapter(
                webhook_url=url,
                timeout=self._timeout,
                max_retries=self._max_retries,
            )
        else:
            raise ValueError(f"Unsupported webhook service: {service}")

    async def test_webhook(self, webhook: WebhookResponse) -> bool:
        """Test a webhook by sending a test notification.

        Args:
            webhook: Webhook to test.

        Returns:
            True if test succeeded.
        """
        adapter = self._create_adapter(webhook.service, webhook.url)

        return await adapter.send_notification(
            event_type=EventType.CREATED,
            task_id="test-task-id",
            task_name="Test Notification",
            project_name="Test Project",
            details={"message": "This is a test notification from Magickit"},
        )
