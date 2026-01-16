"""Priority-based task queue with dependency awareness."""

import asyncio
import uuid
from datetime import datetime
from typing import Any

from magickit.api.models import (
    ServiceType,
    TaskCreate,
    TaskResponse,
    TaskStatus,
)
from magickit.core.dependency_graph import CycleDetectedError, DependencyGraph
from magickit.core.state_manager import StateManager
from magickit.utils.logging import get_logger

logger = get_logger(__name__)


class TaskQueue:
    """Priority-based task queue with dependency management.

    Manages task registration, execution ordering, and state transitions.
    Uses DependencyGraph for dependency resolution and StateManager for persistence.
    """

    def __init__(
        self,
        state_manager: StateManager,
        max_concurrent: int = 5,
        default_priority: int = 5,
        max_retries: int = 3,
    ) -> None:
        """Initialize the task queue.

        Args:
            state_manager: State manager for persistence.
            max_concurrent: Maximum concurrent tasks.
            default_priority: Default task priority (1-9).
            max_retries: Maximum retry attempts for failed tasks.
        """
        self.state_manager = state_manager
        self.max_concurrent = max_concurrent
        self.default_priority = default_priority
        self.max_retries = max_retries

        self._graph = DependencyGraph()
        self._running_count = 0
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Initialize the queue from persisted state."""
        # Load tasks from database
        tasks = await self.state_manager.get_all_tasks()

        for task in tasks:
            if task.status in (TaskStatus.PENDING, TaskStatus.QUEUED, TaskStatus.RUNNING):
                try:
                    self._graph.add_task(task)
                except CycleDetectedError:
                    logger.error("Cycle detected loading task", task_id=task.id)
                    continue

                # Reset running tasks to queued
                if task.status == TaskStatus.RUNNING:
                    await self.state_manager.update_task_status(
                        task.id,
                        TaskStatus.QUEUED,
                    )

            elif task.status == TaskStatus.COMPLETED:
                self._graph.mark_complete(task.id)

        logger.info(
            "Task queue initialized",
            task_count=len(tasks),
            graph_stats=self._graph.get_stats(),
        )

    async def register(self, tasks: list[TaskCreate]) -> list[str]:
        """Register one or more tasks.

        Args:
            tasks: Tasks to register.

        Returns:
            List of created task IDs.

        Raises:
            CycleDetectedError: If tasks would create a dependency cycle.
        """
        async with self._lock:
            task_ids: list[str] = []

            for task_create in tasks:
                task_id = str(uuid.uuid4())
                now = datetime.utcnow()

                task = TaskResponse(
                    id=task_id,
                    name=task_create.name,
                    description=task_create.description,
                    service=task_create.service,
                    payload=task_create.payload,
                    priority=task_create.priority,
                    status=TaskStatus.PENDING,
                    dependencies=task_create.dependencies,
                    metadata=task_create.metadata,
                    created_at=now,
                )

                # Add to dependency graph (validates no cycles)
                self._graph.add_task(task)

                # Persist
                await self.state_manager.save_task(task)

                task_ids.append(task_id)
                logger.info(
                    "Task registered",
                    task_id=task_id,
                    name=task_create.name,
                    service=task_create.service,
                )

            return task_ids

    async def get_next(self) -> TaskResponse | None:
        """Get the next task ready for execution.

        Returns:
            Next task to execute, or None if no tasks are ready.
        """
        async with self._lock:
            if self._running_count >= self.max_concurrent:
                return None

            ready_tasks = self._graph.get_ready_tasks()
            if not ready_tasks:
                return None

            # Get highest priority ready task
            task = ready_tasks[0]

            # Mark as running
            self._running_count += 1
            updated_task = await self.state_manager.update_task_status(
                task.id,
                TaskStatus.RUNNING,
            )

            logger.info(
                "Task dequeued",
                task_id=task.id,
                name=task.name,
                running_count=self._running_count,
            )

            return updated_task

    async def complete(
        self,
        task_id: str,
        result: dict[str, Any] | None = None,
    ) -> TaskResponse | None:
        """Mark a task as completed.

        Args:
            task_id: Task ID.
            result: Task result.

        Returns:
            Updated task, or None if not found.
        """
        async with self._lock:
            task = await self.state_manager.get_task(task_id)
            if task is None:
                logger.warning("Task not found for completion", task_id=task_id)
                return None

            if task.status != TaskStatus.RUNNING:
                logger.warning(
                    "Task not in running state",
                    task_id=task_id,
                    status=task.status,
                )

            # Update state
            updated_task = await self.state_manager.update_task_status(
                task_id,
                TaskStatus.COMPLETED,
                result=result,
            )

            # Update graph
            self._graph.mark_complete(task_id)
            self._running_count = max(0, self._running_count - 1)

            logger.info(
                "Task completed",
                task_id=task_id,
                running_count=self._running_count,
            )

            return updated_task

    async def fail(
        self,
        task_id: str,
        error: str,
        retry: bool = True,
    ) -> TaskResponse | None:
        """Mark a task as failed.

        Args:
            task_id: Task ID.
            error: Error message.
            retry: Whether to retry the task.

        Returns:
            Updated task, or None if not found.
        """
        async with self._lock:
            task = await self.state_manager.get_task(task_id)
            if task is None:
                logger.warning("Task not found for failure", task_id=task_id)
                return None

            self._running_count = max(0, self._running_count - 1)

            # Check retry
            if retry and task.retry_count < self.max_retries:
                # Re-queue for retry
                task.retry_count += 1
                task.status = TaskStatus.QUEUED
                await self.state_manager.save_task(task)

                logger.info(
                    "Task queued for retry",
                    task_id=task_id,
                    retry_count=task.retry_count,
                    max_retries=self.max_retries,
                )

                return task

            # Mark as failed
            updated_task = await self.state_manager.update_task_status(
                task_id,
                TaskStatus.FAILED,
                error=error,
            )

            logger.error(
                "Task failed",
                task_id=task_id,
                error=error,
            )

            return updated_task

    async def cancel(self, task_id: str) -> TaskResponse | None:
        """Cancel a pending or queued task.

        Args:
            task_id: Task ID.

        Returns:
            Updated task, or None if not found.
        """
        async with self._lock:
            task = await self.state_manager.get_task(task_id)
            if task is None:
                return None

            if task.status not in (TaskStatus.PENDING, TaskStatus.QUEUED):
                logger.warning(
                    "Cannot cancel task not in pending/queued state",
                    task_id=task_id,
                    status=task.status,
                )
                return task

            updated_task = await self.state_manager.update_task_status(
                task_id,
                TaskStatus.CANCELLED,
            )

            self._graph.remove_task(task_id)

            logger.info("Task cancelled", task_id=task_id)

            return updated_task

    async def get_task(self, task_id: str) -> TaskResponse | None:
        """Get a task by ID.

        Args:
            task_id: Task ID.

        Returns:
            Task if found, None otherwise.
        """
        return await self.state_manager.get_task(task_id)

    async def get_all_tasks(self) -> list[TaskResponse]:
        """Get all tasks.

        Returns:
            List of all tasks.
        """
        return await self.state_manager.get_all_tasks()

    async def get_execution_order(self) -> list[str]:
        """Get the planned execution order of pending tasks.

        Returns:
            List of task IDs in execution order.
        """
        return self._graph.topological_sort()

    def get_queue_depth(self) -> int:
        """Get the number of tasks waiting to be executed.

        Returns:
            Number of pending/queued tasks.
        """
        return self._graph.get_stats()["pending_tasks"]

    def get_running_count(self) -> int:
        """Get the number of currently running tasks.

        Returns:
            Number of running tasks.
        """
        return self._running_count

    async def get_stats(self) -> dict[str, Any]:
        """Get queue statistics.

        Returns:
            Statistics dictionary.
        """
        db_stats = await self.state_manager.get_stats()
        graph_stats = self._graph.get_stats()

        return {
            **db_stats,
            "queue_depth": graph_stats["pending_tasks"],
            "active_tasks": self._running_count,
            "max_concurrent": self.max_concurrent,
            "ready_tasks": graph_stats["ready_tasks"],
        }
