"""Event publisher for task lifecycle events.

Coordinates event logging, WebSocket broadcasts, and webhook notifications.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Any, Callable, Coroutine

from magickit.api.models import EventType, TaskEventResponse
from magickit.utils.logging import get_logger

if TYPE_CHECKING:
    from magickit.core.notification_manager import NotificationManager
    from magickit.core.state_manager import StateManager

logger = get_logger(__name__)

# Type alias for event handlers
EventHandler = Callable[[EventType, str, dict[str, Any]], Coroutine[Any, Any, None]]


class EventPublisher:
    """Central event publisher for task lifecycle events.

    Coordinates:
    - Audit logging to database
    - WebSocket broadcasts to connected clients
    - Webhook notifications to external services
    """

    def __init__(
        self,
        state_manager: StateManager,
        notification_manager: NotificationManager | None = None,
    ) -> None:
        """Initialize event publisher.

        Args:
            state_manager: State manager for event persistence.
            notification_manager: Optional notification manager for webhooks.
        """
        self._state = state_manager
        self._notifications = notification_manager
        self._handlers: list[EventHandler] = []
        self._ws_broadcast: Callable[[str, dict[str, Any]], Coroutine[Any, Any, None]] | None = None

    def register_handler(self, handler: EventHandler) -> None:
        """Register an event handler.

        Args:
            handler: Async function called for each event.
        """
        self._handlers.append(handler)

    def unregister_handler(self, handler: EventHandler) -> None:
        """Unregister an event handler.

        Args:
            handler: Handler to remove.
        """
        if handler in self._handlers:
            self._handlers.remove(handler)

    def set_ws_broadcast(
        self,
        broadcast_fn: Callable[[str, dict[str, Any]], Coroutine[Any, Any, None]],
    ) -> None:
        """Set the WebSocket broadcast function.

        Args:
            broadcast_fn: Async function to broadcast to WebSocket clients.
        """
        self._ws_broadcast = broadcast_fn

    async def publish(
        self,
        event_type: EventType,
        task_id: str,
        user_id: str | None = None,
        workspace_id: str | None = None,
        project_id: str | None = None,
        task_name: str | None = None,
        project_name: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> TaskEventResponse:
        """Publish an event.

        This method:
        1. Logs the event to the database
        2. Notifies all registered handlers
        3. Broadcasts to WebSocket clients
        4. Sends webhook notifications

        Args:
            event_type: Type of event.
            task_id: Task ID.
            user_id: Optional user who triggered the event.
            workspace_id: Optional workspace ID for webhook lookup.
            project_id: Optional project ID for WebSocket routing.
            task_name: Optional task name for notifications.
            project_name: Optional project name for notifications.
            details: Additional event details.

        Returns:
            Created event record.
        """
        event_id = str(uuid.uuid4())
        details = details or {}

        # 1. Log to database
        event = await self._state.create_task_event(
            event_id=event_id,
            task_id=task_id,
            event_type=event_type,
            user_id=user_id,
            details=details,
        )

        logger.info(
            "event_published",
            event_id=event_id,
            event_type=event_type.value,
            task_id=task_id,
            user_id=user_id,
        )

        # 2. Notify handlers (async, don't block)
        if self._handlers:
            asyncio.create_task(self._notify_handlers(event_type, task_id, details))

        # 3. Broadcast to WebSocket clients
        if self._ws_broadcast and project_id:
            asyncio.create_task(
                self._broadcast_ws(
                    project_id=project_id,
                    event_type=event_type,
                    task_id=task_id,
                    details=details,
                )
            )

        # 4. Send webhook notifications
        if self._notifications and workspace_id and task_name:
            asyncio.create_task(
                self._send_notifications(
                    workspace_id=workspace_id,
                    event_type=event_type,
                    task_id=task_id,
                    task_name=task_name,
                    project_name=project_name,
                    details=details,
                )
            )

        return event

    async def _notify_handlers(
        self,
        event_type: EventType,
        task_id: str,
        details: dict[str, Any],
    ) -> None:
        """Notify all registered handlers.

        Args:
            event_type: Event type.
            task_id: Task ID.
            details: Event details.
        """
        for handler in self._handlers:
            try:
                await handler(event_type, task_id, details)
            except Exception as e:
                logger.error(
                    "event_handler_error",
                    handler=handler.__name__,
                    error=str(e),
                )

    async def _broadcast_ws(
        self,
        project_id: str,
        event_type: EventType,
        task_id: str,
        details: dict[str, Any],
    ) -> None:
        """Broadcast event to WebSocket clients.

        Args:
            project_id: Project ID for routing.
            event_type: Event type.
            task_id: Task ID.
            details: Event details.
        """
        if self._ws_broadcast is None:
            return

        try:
            message = {
                "type": "task_event",
                "event_type": event_type.value,
                "task_id": task_id,
                "details": details,
            }
            await self._ws_broadcast(project_id, message)
        except Exception as e:
            logger.error(
                "ws_broadcast_error",
                project_id=project_id,
                error=str(e),
            )

    async def _send_notifications(
        self,
        workspace_id: str,
        event_type: EventType,
        task_id: str,
        task_name: str,
        project_name: str | None,
        details: dict[str, Any],
    ) -> None:
        """Send webhook notifications.

        Args:
            workspace_id: Workspace ID for webhook lookup.
            event_type: Event type.
            task_id: Task ID.
            task_name: Task name.
            project_name: Optional project name.
            details: Event details.
        """
        if self._notifications is None:
            return

        try:
            await self._notifications.notify(
                workspace_id=workspace_id,
                event_type=event_type,
                task_id=task_id,
                task_name=task_name,
                project_name=project_name,
                details=details,
                background=False,  # We're already in a background task
            )
        except Exception as e:
            logger.error(
                "notification_error",
                workspace_id=workspace_id,
                error=str(e),
            )

    # =========================================================================
    # Convenience Methods for Common Events
    # =========================================================================

    async def task_created(
        self,
        task_id: str,
        task_name: str,
        user_id: str | None = None,
        workspace_id: str | None = None,
        project_id: str | None = None,
        project_name: str | None = None,
    ) -> TaskEventResponse:
        """Publish task created event."""
        return await self.publish(
            event_type=EventType.CREATED,
            task_id=task_id,
            user_id=user_id,
            workspace_id=workspace_id,
            project_id=project_id,
            task_name=task_name,
            project_name=project_name,
        )

    async def task_started(
        self,
        task_id: str,
        task_name: str,
        user_id: str | None = None,
        workspace_id: str | None = None,
        project_id: str | None = None,
        project_name: str | None = None,
    ) -> TaskEventResponse:
        """Publish task started event."""
        return await self.publish(
            event_type=EventType.STARTED,
            task_id=task_id,
            user_id=user_id,
            workspace_id=workspace_id,
            project_id=project_id,
            task_name=task_name,
            project_name=project_name,
        )

    async def task_completed(
        self,
        task_id: str,
        task_name: str,
        result: dict[str, Any] | None = None,
        user_id: str | None = None,
        workspace_id: str | None = None,
        project_id: str | None = None,
        project_name: str | None = None,
    ) -> TaskEventResponse:
        """Publish task completed event."""
        return await self.publish(
            event_type=EventType.COMPLETED,
            task_id=task_id,
            user_id=user_id,
            workspace_id=workspace_id,
            project_id=project_id,
            task_name=task_name,
            project_name=project_name,
            details={"result": result} if result else None,
        )

    async def task_failed(
        self,
        task_id: str,
        task_name: str,
        error: str,
        user_id: str | None = None,
        workspace_id: str | None = None,
        project_id: str | None = None,
        project_name: str | None = None,
    ) -> TaskEventResponse:
        """Publish task failed event."""
        return await self.publish(
            event_type=EventType.FAILED,
            task_id=task_id,
            user_id=user_id,
            workspace_id=workspace_id,
            project_id=project_id,
            task_name=task_name,
            project_name=project_name,
            details={"error": error},
        )

    async def task_cancelled(
        self,
        task_id: str,
        task_name: str,
        user_id: str | None = None,
        workspace_id: str | None = None,
        project_id: str | None = None,
        project_name: str | None = None,
    ) -> TaskEventResponse:
        """Publish task cancelled event."""
        return await self.publish(
            event_type=EventType.CANCELLED,
            task_id=task_id,
            user_id=user_id,
            workspace_id=workspace_id,
            project_id=project_id,
            task_name=task_name,
            project_name=project_name,
        )
