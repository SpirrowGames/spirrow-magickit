"""WebSocket endpoints for real-time updates."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from magickit.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter()


class ConnectionManager:
    """Manages WebSocket connections organized by project.

    Allows broadcasting messages to all clients subscribed to a project.
    """

    def __init__(self) -> None:
        """Initialize connection manager."""
        # project_id -> set of WebSocket connections
        self._connections: dict[str, set[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket, project_id: str) -> None:
        """Accept a WebSocket connection and register it.

        Args:
            websocket: WebSocket connection.
            project_id: Project ID to subscribe to.
        """
        await websocket.accept()

        async with self._lock:
            if project_id not in self._connections:
                self._connections[project_id] = set()
            self._connections[project_id].add(websocket)

        logger.info(
            "websocket_connected",
            project_id=project_id,
            total_connections=len(self._connections.get(project_id, set())),
        )

    async def disconnect(self, websocket: WebSocket, project_id: str) -> None:
        """Remove a WebSocket connection.

        Args:
            websocket: WebSocket connection.
            project_id: Project ID the connection was subscribed to.
        """
        async with self._lock:
            if project_id in self._connections:
                self._connections[project_id].discard(websocket)
                if not self._connections[project_id]:
                    del self._connections[project_id]

        logger.info(
            "websocket_disconnected",
            project_id=project_id,
            remaining_connections=len(self._connections.get(project_id, set())),
        )

    async def broadcast(self, project_id: str, message: dict[str, Any]) -> None:
        """Broadcast a message to all connections for a project.

        Args:
            project_id: Project ID.
            message: Message to broadcast.
        """
        async with self._lock:
            connections = self._connections.get(project_id, set()).copy()

        if not connections:
            return

        # Add timestamp to message
        message["timestamp"] = datetime.now(timezone.utc).isoformat()

        # Serialize once for efficiency
        message_json = json.dumps(message)

        # Send to all connections, removing failed ones
        failed: list[WebSocket] = []

        for websocket in connections:
            try:
                await websocket.send_text(message_json)
            except Exception as e:
                logger.warning(
                    "websocket_send_failed",
                    project_id=project_id,
                    error=str(e),
                )
                failed.append(websocket)

        # Clean up failed connections
        if failed:
            async with self._lock:
                for ws in failed:
                    if project_id in self._connections:
                        self._connections[project_id].discard(ws)

        logger.debug(
            "websocket_broadcast_complete",
            project_id=project_id,
            sent_count=len(connections) - len(failed),
            failed_count=len(failed),
        )

    async def broadcast_all(self, message: dict[str, Any]) -> None:
        """Broadcast a message to all connected clients.

        Args:
            message: Message to broadcast.
        """
        async with self._lock:
            all_project_ids = list(self._connections.keys())

        for project_id in all_project_ids:
            await self.broadcast(project_id, message)

    def get_connection_count(self, project_id: str | None = None) -> int:
        """Get the number of active connections.

        Args:
            project_id: Optional project ID to filter by.

        Returns:
            Number of connections.
        """
        if project_id:
            return len(self._connections.get(project_id, set()))
        return sum(len(conns) for conns in self._connections.values())

    def get_project_ids(self) -> list[str]:
        """Get all project IDs with active connections.

        Returns:
            List of project IDs.
        """
        return list(self._connections.keys())


# Global connection manager instance
manager = ConnectionManager()


@router.websocket("/ws/projects/{project_id}")
async def websocket_endpoint(websocket: WebSocket, project_id: str) -> None:
    """WebSocket endpoint for project updates.

    Clients connect to receive real-time task updates for a specific project.

    Message format (outgoing):
    ```json
    {
        "type": "task_event",
        "event_type": "completed",
        "task_id": "...",
        "details": {...},
        "timestamp": "..."
    }
    ```

    Clients can send ping messages to keep connection alive:
    ```json
    {"type": "ping"}
    ```

    Server responds with:
    ```json
    {"type": "pong", "timestamp": "..."}
    ```

    Args:
        websocket: WebSocket connection.
        project_id: Project ID to subscribe to.
    """
    await manager.connect(websocket, project_id)

    try:
        # Send welcome message
        await websocket.send_json({
            "type": "connected",
            "project_id": project_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        # Handle incoming messages (mainly ping/pong for keepalive)
        while True:
            try:
                data = await websocket.receive_text()
                message = json.loads(data)

                if message.get("type") == "ping":
                    await websocket.send_json({
                        "type": "pong",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })
                elif message.get("type") == "subscribe":
                    # Allow subscribing to additional projects
                    new_project_id = message.get("project_id")
                    if new_project_id:
                        await manager.connect(websocket, new_project_id)
                        await websocket.send_json({
                            "type": "subscribed",
                            "project_id": new_project_id,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        })

            except json.JSONDecodeError:
                await websocket.send_json({
                    "type": "error",
                    "message": "Invalid JSON",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })

    except WebSocketDisconnect:
        await manager.disconnect(websocket, project_id)
    except Exception as e:
        logger.error(
            "websocket_error",
            project_id=project_id,
            error=str(e),
        )
        await manager.disconnect(websocket, project_id)


# Function to be used by EventPublisher for broadcasting
async def broadcast_to_project(project_id: str, message: dict[str, Any]) -> None:
    """Broadcast a message to all WebSocket clients for a project.

    This function is meant to be registered with EventPublisher.

    Args:
        project_id: Project ID.
        message: Message to broadcast.
    """
    await manager.broadcast(project_id, message)


def get_manager() -> ConnectionManager:
    """Get the global connection manager.

    Returns:
        Connection manager instance.
    """
    return manager
