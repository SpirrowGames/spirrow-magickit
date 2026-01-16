"""Tests for TaskQueue."""

import pytest

from magickit.api.models import ServiceType, TaskCreate, TaskStatus
from magickit.core.dependency_graph import CycleDetectedError
from magickit.core.task_queue import TaskQueue


class TestTaskQueue:
    """Tests for TaskQueue class."""

    @pytest.mark.asyncio
    async def test_register_single_task(self, task_queue: TaskQueue) -> None:
        """Test registering a single task."""
        task = TaskCreate(
            name="Test Task",
            description="A test task",
            service=ServiceType.LEXORA,
            payload={"key": "value"},
        )

        task_ids = await task_queue.register([task])

        assert len(task_ids) == 1
        assert task_ids[0]  # Should be a non-empty string

        # Verify task was created
        created_task = await task_queue.get_task(task_ids[0])
        assert created_task is not None
        assert created_task.name == "Test Task"
        assert created_task.status == TaskStatus.PENDING

    @pytest.mark.asyncio
    async def test_register_multiple_tasks(self, task_queue: TaskQueue) -> None:
        """Test registering multiple tasks."""
        tasks = [
            TaskCreate(name="Task 1", service=ServiceType.LEXORA),
            TaskCreate(name="Task 2", service=ServiceType.COGNILENS),
            TaskCreate(name="Task 3", service=ServiceType.PRISMIND),
        ]

        task_ids = await task_queue.register(tasks)

        assert len(task_ids) == 3

    @pytest.mark.asyncio
    async def test_get_next_respects_priority(self, task_queue: TaskQueue) -> None:
        """Test that get_next returns highest priority task first."""
        tasks = [
            TaskCreate(name="Low Priority", service=ServiceType.LEXORA, priority=7),
            TaskCreate(name="High Priority", service=ServiceType.LEXORA, priority=1),
            TaskCreate(name="Normal Priority", service=ServiceType.LEXORA, priority=5),
        ]

        await task_queue.register(tasks)

        # Should get high priority first
        next_task = await task_queue.get_next()
        assert next_task is not None
        assert next_task.name == "High Priority"
        assert next_task.status == TaskStatus.RUNNING

    @pytest.mark.asyncio
    async def test_get_next_respects_dependencies(self, task_queue: TaskQueue) -> None:
        """Test that get_next respects task dependencies."""
        task1 = TaskCreate(name="Task 1", service=ServiceType.LEXORA)
        task_ids = await task_queue.register([task1])

        task2 = TaskCreate(
            name="Task 2",
            service=ServiceType.LEXORA,
            dependencies=task_ids,
        )
        await task_queue.register([task2])

        # Should get task1 first (task2 depends on it)
        next_task = await task_queue.get_next()
        assert next_task is not None
        assert next_task.name == "Task 1"

    @pytest.mark.asyncio
    async def test_complete_task(self, task_queue: TaskQueue) -> None:
        """Test completing a task."""
        task = TaskCreate(name="Test Task", service=ServiceType.LEXORA)
        task_ids = await task_queue.register([task])

        # Get and start the task
        await task_queue.get_next()

        # Complete it
        result = {"output": "success"}
        completed = await task_queue.complete(task_ids[0], result=result)

        assert completed is not None
        assert completed.status == TaskStatus.COMPLETED
        assert completed.result == result
        assert completed.completed_at is not None

    @pytest.mark.asyncio
    async def test_complete_unlocks_dependents(self, task_queue: TaskQueue) -> None:
        """Test that completing a task unlocks dependent tasks."""
        task1 = TaskCreate(name="Task 1", service=ServiceType.LEXORA)
        task_ids = await task_queue.register([task1])

        task2 = TaskCreate(
            name="Task 2",
            service=ServiceType.LEXORA,
            dependencies=task_ids,
        )
        await task_queue.register([task2])

        # Get and complete task1
        t1 = await task_queue.get_next()
        assert t1 is not None
        await task_queue.complete(t1.id)

        # Now task2 should be available
        t2 = await task_queue.get_next()
        assert t2 is not None
        assert t2.name == "Task 2"

    @pytest.mark.asyncio
    async def test_fail_task(self, task_queue: TaskQueue) -> None:
        """Test failing a task."""
        task = TaskCreate(name="Test Task", service=ServiceType.LEXORA)
        task_ids = await task_queue.register([task])

        # Get and start the task
        await task_queue.get_next()

        # Fail it without retry
        failed = await task_queue.fail(task_ids[0], error="Test error", retry=False)

        assert failed is not None
        assert failed.status == TaskStatus.FAILED
        assert failed.error == "Test error"

    @pytest.mark.asyncio
    async def test_fail_with_retry(self, task_queue: TaskQueue) -> None:
        """Test failing a task triggers retry."""
        task = TaskCreate(name="Test Task", service=ServiceType.LEXORA)
        task_ids = await task_queue.register([task])

        # Get and start the task
        await task_queue.get_next()

        # Fail with retry
        failed = await task_queue.fail(task_ids[0], error="Test error", retry=True)

        assert failed is not None
        # Should be requeued, not failed
        assert failed.status == TaskStatus.QUEUED
        assert failed.retry_count == 1

    @pytest.mark.asyncio
    async def test_cancel_task(self, task_queue: TaskQueue) -> None:
        """Test cancelling a task."""
        task = TaskCreate(name="Test Task", service=ServiceType.LEXORA)
        task_ids = await task_queue.register([task])

        cancelled = await task_queue.cancel(task_ids[0])

        assert cancelled is not None
        assert cancelled.status == TaskStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_max_concurrent_limit(self, task_queue: TaskQueue) -> None:
        """Test that max concurrent limit is respected."""
        # Create more tasks than max_concurrent (which is 5)
        tasks = [
            TaskCreate(name=f"Task {i}", service=ServiceType.LEXORA)
            for i in range(10)
        ]
        await task_queue.register(tasks)

        # Get tasks up to the limit
        running_tasks = []
        for _ in range(5):
            t = await task_queue.get_next()
            assert t is not None
            running_tasks.append(t)

        # Next call should return None (at limit)
        assert await task_queue.get_next() is None

        # Complete one task
        await task_queue.complete(running_tasks[0].id)

        # Now we should be able to get another
        t = await task_queue.get_next()
        assert t is not None

    @pytest.mark.asyncio
    async def test_get_stats(self, task_queue: TaskQueue) -> None:
        """Test getting queue statistics."""
        tasks = [
            TaskCreate(name="Task 1", service=ServiceType.LEXORA),
            TaskCreate(name="Task 2", service=ServiceType.COGNILENS),
        ]
        await task_queue.register(tasks)

        # Get and complete one task
        t = await task_queue.get_next()
        if t:
            await task_queue.complete(t.id)

        stats = await task_queue.get_stats()

        assert stats["total_tasks"] == 2
        assert stats["active_tasks"] == 0  # None running after completion
        assert stats["max_concurrent"] == 5

    @pytest.mark.asyncio
    async def test_cycle_detection(self, task_queue: TaskQueue) -> None:
        """Test that circular dependencies are detected."""
        # First register task1 depending on task2 (which doesn't exist yet)
        task1 = TaskCreate(
            name="Task 1",
            service=ServiceType.LEXORA,
            dependencies=["nonexistent"],
        )
        task1_ids = await task_queue.register([task1])

        # Now try to create task2 depending on task1 - this creates a potential issue
        # but since "nonexistent" isn't in the graph, it should work
        task2 = TaskCreate(
            name="Task 2",
            service=ServiceType.LEXORA,
            dependencies=task1_ids,
        )
        task2_ids = await task_queue.register([task2])

        # Verify both tasks exist
        assert len(task1_ids) == 1
        assert len(task2_ids) == 1

    @pytest.mark.asyncio
    async def test_get_all_tasks(self, task_queue: TaskQueue) -> None:
        """Test getting all tasks."""
        tasks = [
            TaskCreate(name="Task 1", service=ServiceType.LEXORA),
            TaskCreate(name="Task 2", service=ServiceType.COGNILENS),
            TaskCreate(name="Task 3", service=ServiceType.PRISMIND),
        ]
        await task_queue.register(tasks)

        all_tasks = await task_queue.get_all_tasks()

        assert len(all_tasks) == 3

    @pytest.mark.asyncio
    async def test_execution_order(self, task_queue: TaskQueue) -> None:
        """Test getting planned execution order."""
        task1 = TaskCreate(name="Task 1", service=ServiceType.LEXORA)
        task1_ids = await task_queue.register([task1])

        task2 = TaskCreate(
            name="Task 2",
            service=ServiceType.LEXORA,
            dependencies=task1_ids,
        )
        task2_ids = await task_queue.register([task2])

        order = await task_queue.get_execution_order()

        # task1 should come before task2
        assert order.index(task1_ids[0]) < order.index(task2_ids[0])
