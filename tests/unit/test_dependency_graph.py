"""Tests for DependencyGraph."""

from datetime import datetime

import pytest

from magickit.api.models import ServiceType, TaskResponse, TaskStatus
from magickit.core.dependency_graph import CycleDetectedError, DependencyGraph


def make_task(
    task_id: str,
    dependencies: list[str] | None = None,
    priority: int = 5,
) -> TaskResponse:
    """Create a test task."""
    return TaskResponse(
        id=task_id,
        name=f"Task {task_id}",
        description="Test task",
        service=ServiceType.LEXORA,
        payload={},
        priority=priority,
        status=TaskStatus.PENDING,
        dependencies=dependencies or [],
        metadata={},
        created_at=datetime.utcnow(),
    )


class TestDependencyGraph:
    """Tests for DependencyGraph class."""

    def test_add_task_no_dependencies(self) -> None:
        """Test adding a task with no dependencies."""
        graph = DependencyGraph()
        task = make_task("task1")

        graph.add_task(task)

        assert "task1" in graph._tasks
        assert graph.get_dependencies("task1") == set()

    def test_add_task_with_dependencies(self) -> None:
        """Test adding a task with dependencies."""
        graph = DependencyGraph()
        task1 = make_task("task1")
        task2 = make_task("task2", dependencies=["task1"])

        graph.add_task(task1)
        graph.add_task(task2)

        assert graph.get_dependencies("task2") == {"task1"}
        assert graph.get_dependents("task1") == {"task2"}

    def test_self_dependency_raises_error(self) -> None:
        """Test that self-dependency raises an error."""
        graph = DependencyGraph()
        task = make_task("task1", dependencies=["task1"])

        with pytest.raises(CycleDetectedError):
            graph.add_task(task)

    def test_cycle_detection(self) -> None:
        """Test that cycles are detected."""
        graph = DependencyGraph()
        task1 = make_task("task1", dependencies=["task2"])
        task2 = make_task("task2", dependencies=["task1"])

        graph.add_task(task1)

        with pytest.raises(CycleDetectedError):
            graph.add_task(task2)

    def test_get_ready_tasks_no_dependencies(self) -> None:
        """Test getting ready tasks when tasks have no dependencies."""
        graph = DependencyGraph()
        task1 = make_task("task1", priority=3)
        task2 = make_task("task2", priority=5)

        graph.add_task(task1)
        graph.add_task(task2)

        ready = graph.get_ready_tasks()

        assert len(ready) == 2
        # Should be sorted by priority
        assert ready[0].id == "task1"
        assert ready[1].id == "task2"

    def test_get_ready_tasks_with_dependencies(self) -> None:
        """Test getting ready tasks respects dependencies."""
        graph = DependencyGraph()
        task1 = make_task("task1")
        task2 = make_task("task2", dependencies=["task1"])

        graph.add_task(task1)
        graph.add_task(task2)

        ready = graph.get_ready_tasks()

        # Only task1 should be ready (task2 depends on task1)
        assert len(ready) == 1
        assert ready[0].id == "task1"

    def test_mark_complete_unlocks_dependents(self) -> None:
        """Test that completing a task unlocks its dependents."""
        graph = DependencyGraph()
        task1 = make_task("task1")
        task2 = make_task("task2", dependencies=["task1"])

        graph.add_task(task1)
        graph.add_task(task2)

        # Initially only task1 is ready
        assert len(graph.get_ready_tasks()) == 1

        # Mark task1 complete
        graph.mark_complete("task1")

        # Now task2 should be ready
        ready = graph.get_ready_tasks()
        assert len(ready) == 1
        assert ready[0].id == "task2"

    def test_topological_sort(self) -> None:
        """Test topological sort ordering."""
        graph = DependencyGraph()
        task1 = make_task("task1")
        task2 = make_task("task2", dependencies=["task1"])
        task3 = make_task("task3", dependencies=["task1", "task2"])

        graph.add_task(task1)
        graph.add_task(task2)
        graph.add_task(task3)

        order = graph.topological_sort()

        # task1 must come before task2 and task3
        # task2 must come before task3
        assert order.index("task1") < order.index("task2")
        assert order.index("task1") < order.index("task3")
        assert order.index("task2") < order.index("task3")

    def test_remove_task(self) -> None:
        """Test removing a task."""
        graph = DependencyGraph()
        task1 = make_task("task1")
        task2 = make_task("task2", dependencies=["task1"])

        graph.add_task(task1)
        graph.add_task(task2)

        graph.remove_task("task1")

        assert "task1" not in graph._tasks
        # task2's dependency should be cleaned up
        assert "task1" not in graph.get_dependencies("task2")

    def test_get_all_dependencies(self) -> None:
        """Test getting transitive dependencies."""
        graph = DependencyGraph()
        task1 = make_task("task1")
        task2 = make_task("task2", dependencies=["task1"])
        task3 = make_task("task3", dependencies=["task2"])

        graph.add_task(task1)
        graph.add_task(task2)
        graph.add_task(task3)

        all_deps = graph.get_all_dependencies("task3")

        assert all_deps == {"task1", "task2"}

    def test_clear(self) -> None:
        """Test clearing the graph."""
        graph = DependencyGraph()
        task1 = make_task("task1")
        task2 = make_task("task2")

        graph.add_task(task1)
        graph.add_task(task2)

        graph.clear()

        assert len(graph._tasks) == 0
        assert len(graph._dependencies) == 0
        assert len(graph._completed) == 0

    def test_stats(self) -> None:
        """Test getting graph statistics."""
        graph = DependencyGraph()
        task1 = make_task("task1")
        task2 = make_task("task2", dependencies=["task1"])

        graph.add_task(task1)
        graph.add_task(task2)
        graph.mark_complete("task1")

        stats = graph.get_stats()

        assert stats["total_tasks"] == 2
        assert stats["completed_tasks"] == 1
        assert stats["pending_tasks"] == 1
        assert stats["ready_tasks"] == 1
