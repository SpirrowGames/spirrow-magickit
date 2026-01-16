"""Dependency graph management using DAG."""

from collections import defaultdict
from typing import Any

from magickit.api.models import TaskResponse, TaskStatus
from magickit.utils.logging import get_logger

logger = get_logger(__name__)


class CycleDetectedError(Exception):
    """Raised when a cycle is detected in the dependency graph."""

    pass


class DependencyGraph:
    """Manages task dependencies using a Directed Acyclic Graph (DAG).

    Tracks dependencies between tasks and determines which tasks
    are ready to execute based on completed dependencies.
    """

    def __init__(self) -> None:
        """Initialize an empty dependency graph."""
        # task_id -> set of task_ids it depends on
        self._dependencies: dict[str, set[str]] = defaultdict(set)
        # task_id -> set of task_ids that depend on it
        self._dependents: dict[str, set[str]] = defaultdict(set)
        # task_id -> task data
        self._tasks: dict[str, TaskResponse] = {}
        # Completed task IDs
        self._completed: set[str] = set()

    def add_task(self, task: TaskResponse) -> None:
        """Add a task to the dependency graph.

        Args:
            task: Task to add.

        Raises:
            CycleDetectedError: If adding this task would create a cycle.
        """
        task_id = task.id
        deps = set(task.dependencies)

        # Check for self-dependency
        if task_id in deps:
            raise CycleDetectedError(f"Task {task_id} cannot depend on itself")

        # Store task
        self._tasks[task_id] = task
        self._dependencies[task_id] = deps

        # Update reverse mapping
        for dep_id in deps:
            self._dependents[dep_id].add(task_id)

        # Verify no cycles
        if self._has_cycle():
            # Rollback
            del self._tasks[task_id]
            del self._dependencies[task_id]
            for dep_id in deps:
                self._dependents[dep_id].discard(task_id)
            raise CycleDetectedError(f"Adding task {task_id} would create a cycle")

        logger.debug("Task added to graph", task_id=task_id, dependencies=list(deps))

    def remove_task(self, task_id: str) -> None:
        """Remove a task from the graph.

        Args:
            task_id: ID of task to remove.
        """
        if task_id not in self._tasks:
            return

        # Remove from dependencies of other tasks
        deps = self._dependencies.get(task_id, set())
        for dep_id in deps:
            self._dependents[dep_id].discard(task_id)

        # Remove tasks that depend on this one need to be updated
        dependents = self._dependents.get(task_id, set())
        for dependent_id in dependents:
            self._dependencies[dependent_id].discard(task_id)

        # Clean up
        self._dependencies.pop(task_id, None)
        self._dependents.pop(task_id, None)
        self._tasks.pop(task_id, None)
        self._completed.discard(task_id)

        logger.debug("Task removed from graph", task_id=task_id)

    def mark_complete(self, task_id: str) -> None:
        """Mark a task as completed.

        Args:
            task_id: ID of the completed task.
        """
        self._completed.add(task_id)
        logger.debug("Task marked complete", task_id=task_id)

    def is_complete(self, task_id: str) -> bool:
        """Check if a task is marked as complete.

        Args:
            task_id: Task ID.

        Returns:
            True if complete.
        """
        return task_id in self._completed

    def get_ready_tasks(self) -> list[TaskResponse]:
        """Get all tasks that are ready to execute.

        A task is ready when all its dependencies are completed.

        Returns:
            List of tasks ready to execute.
        """
        ready = []

        for task_id, task in self._tasks.items():
            # Skip already completed tasks
            if task_id in self._completed:
                continue

            # Skip non-pending tasks
            if task.status not in (TaskStatus.PENDING, TaskStatus.QUEUED):
                continue

            # Check if all dependencies are complete
            deps = self._dependencies.get(task_id, set())
            if deps.issubset(self._completed):
                ready.append(task)

        # Sort by priority (lower number = higher priority)
        ready.sort(key=lambda t: (t.priority, t.created_at))

        return ready

    def get_dependencies(self, task_id: str) -> set[str]:
        """Get direct dependencies of a task.

        Args:
            task_id: Task ID.

        Returns:
            Set of dependency task IDs.
        """
        return self._dependencies.get(task_id, set()).copy()

    def get_dependents(self, task_id: str) -> set[str]:
        """Get tasks that directly depend on this task.

        Args:
            task_id: Task ID.

        Returns:
            Set of dependent task IDs.
        """
        return self._dependents.get(task_id, set()).copy()

    def get_all_dependencies(self, task_id: str) -> set[str]:
        """Get all dependencies (transitive) of a task.

        Args:
            task_id: Task ID.

        Returns:
            Set of all dependency task IDs.
        """
        all_deps: set[str] = set()
        to_visit = list(self._dependencies.get(task_id, set()))

        while to_visit:
            dep_id = to_visit.pop()
            if dep_id not in all_deps:
                all_deps.add(dep_id)
                to_visit.extend(self._dependencies.get(dep_id, set()))

        return all_deps

    def topological_sort(self) -> list[str]:
        """Get tasks in topological order.

        Returns:
            List of task IDs in execution order.

        Raises:
            CycleDetectedError: If the graph contains a cycle.
        """
        # Kahn's algorithm
        in_degree: dict[str, int] = {task_id: 0 for task_id in self._tasks}

        for task_id in self._tasks:
            for dep_id in self._dependencies.get(task_id, set()):
                if dep_id in in_degree:
                    in_degree[task_id] += 1

        # Start with tasks that have no dependencies
        queue = [task_id for task_id, degree in in_degree.items() if degree == 0]
        result: list[str] = []

        while queue:
            # Sort by priority within the queue
            queue.sort(key=lambda tid: self._tasks[tid].priority if tid in self._tasks else 999)
            task_id = queue.pop(0)
            result.append(task_id)

            # Reduce in-degree for dependents
            for dependent_id in self._dependents.get(task_id, set()):
                if dependent_id in in_degree:
                    in_degree[dependent_id] -= 1
                    if in_degree[dependent_id] == 0:
                        queue.append(dependent_id)

        if len(result) != len(self._tasks):
            raise CycleDetectedError("Cycle detected in dependency graph")

        return result

    def _has_cycle(self) -> bool:
        """Check if the graph contains a cycle using DFS.

        Returns:
            True if a cycle exists.
        """
        WHITE, GRAY, BLACK = 0, 1, 2
        color: dict[str, int] = {task_id: WHITE for task_id in self._tasks}

        def dfs(task_id: str) -> bool:
            color[task_id] = GRAY

            for dep_id in self._dependencies.get(task_id, set()):
                if dep_id not in color:
                    continue
                if color[dep_id] == GRAY:
                    return True  # Back edge = cycle
                if color[dep_id] == WHITE and dfs(dep_id):
                    return True

            color[task_id] = BLACK
            return False

        for task_id in self._tasks:
            if color[task_id] == WHITE:
                if dfs(task_id):
                    return True

        return False

    def get_stats(self) -> dict[str, Any]:
        """Get graph statistics.

        Returns:
            Statistics dictionary.
        """
        return {
            "total_tasks": len(self._tasks),
            "completed_tasks": len(self._completed),
            "pending_tasks": len(self._tasks) - len(self._completed),
            "ready_tasks": len(self.get_ready_tasks()),
        }

    def clear(self) -> None:
        """Clear all tasks from the graph."""
        self._dependencies.clear()
        self._dependents.clear()
        self._tasks.clear()
        self._completed.clear()
