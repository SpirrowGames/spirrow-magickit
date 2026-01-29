"""Task management tools for Magickit MCP server.

Provides orchestrated task management with smart features:
- Automatic task ID generation
- Duplicate detection via knowledge search
- Dependency validation
- Context retrieval on task start
- Knowledge recording on task completion
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from magickit.adapters.prismind import PrismindAdapter
from magickit.config import Settings
from magickit.utils.logging import get_logger

logger = get_logger(__name__)

# Module-level settings reference
_settings: Settings | None = None


def _extract_tasks_from_progress(progress: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract flat task list from progress response.

    Args:
        progress: Progress response from Prismind

    Returns:
        Flat list of tasks with phase info
    """
    tasks = []
    phases = progress.get("phases", [])

    for phase_data in phases:
        phase_name = phase_data.get("phase", "")
        for task in phase_data.get("tasks", []):
            task_with_phase = {**task, "phase": phase_name}
            tasks.append(task_with_phase)

    return tasks


def _generate_next_task_id(tasks: list[dict[str, Any]]) -> str:
    """Generate next task ID based on existing tasks.

    Args:
        tasks: List of existing tasks

    Returns:
        Next task ID (e.g., "T05")
    """
    max_num = 0
    for task in tasks:
        task_id = task.get("task_id", "")
        if task_id.startswith("T") and task_id[1:].isdigit():
            num = int(task_id[1:])
            if num > max_num:
                max_num = num

    return f"T{max_num + 1:02d}"


def _smart_sort_tasks(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort tasks by priority and blocked status.

    Sort order:
    1. Not blocked before blocked
    2. High priority before medium before low
    3. Dependencies resolved before unresolved

    Args:
        tasks: List of tasks

    Returns:
        Sorted task list
    """
    priority_order = {"high": 0, "medium": 1, "low": 2, "": 1}

    def sort_key(task: dict[str, Any]) -> tuple:
        is_blocked = task.get("status") == "blocked"
        priority = priority_order.get(task.get("priority", "medium"), 1)
        # Tasks with no blockers come first
        has_blockers = bool(task.get("blocked_by", []))
        return (is_blocked, has_blockers, priority, task.get("task_id", ""))

    return sorted(tasks, key=sort_key)


def _find_recommended_task(tasks: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Find the recommended next task to work on.

    Criteria:
    - Status is not_started
    - All blocked_by tasks are completed
    - Highest priority

    Args:
        tasks: List of all tasks

    Returns:
        Recommended task or None
    """
    # Build completed task set
    completed_ids = {
        t.get("task_id")
        for t in tasks
        if t.get("status") == "completed"
    }

    candidates = []
    for task in tasks:
        if task.get("status") != "not_started":
            continue

        # Check if all dependencies are completed
        blocked_by = task.get("blocked_by", [])
        if blocked_by:
            if not all(dep in completed_ids for dep in blocked_by):
                continue

        candidates.append(task)

    if not candidates:
        return None

    # Sort by priority and return first
    priority_order = {"high": 0, "medium": 1, "low": 2, "": 1}
    candidates.sort(key=lambda t: priority_order.get(t.get("priority", "medium"), 1))

    return candidates[0]


def _find_tasks_blocked_by(
    tasks: list[dict[str, Any]],
    task_id: str,
) -> list[dict[str, Any]]:
    """Find tasks that are blocked by the given task.

    Args:
        tasks: List of all tasks
        task_id: Task ID to check

    Returns:
        List of tasks blocked by this task
    """
    blocked_tasks = []
    for task in tasks:
        blocked_by = task.get("blocked_by", [])
        if task_id in blocked_by:
            blocked_tasks.append(task)
    return blocked_tasks


def _calculate_stats(tasks: list[dict[str, Any]]) -> dict[str, int]:
    """Calculate task statistics.

    Args:
        tasks: List of tasks

    Returns:
        Dict with counts by status
    """
    stats = {
        "total": len(tasks),
        "completed": 0,
        "in_progress": 0,
        "blocked": 0,
        "not_started": 0,
    }

    for task in tasks:
        status = task.get("status", "not_started")
        if status in stats:
            stats[status] += 1
        else:
            stats["not_started"] += 1

    return stats


async def add_task_impl(
    settings: Settings,
    name: str,
    description: str = "",
    phase: str = "",
    priority: str = "medium",
    category: str = "",
    blocked_by: list[str] | None = None,
    project: str = "",
) -> dict[str, Any]:
    """Add a new task with orchestration.

    Orchestration:
    1. Auto-generate task_id
    2. Check for duplicate tasks via knowledge search
    3. Validate blocked_by task IDs exist
    4. Add task via Prismind
    5. Register task info as knowledge

    Args:
        settings: Application settings
        name: Task name
        description: Task description
        phase: Phase name (empty for current phase)
        priority: Priority (high/medium/low)
        category: Category (bug/feature/refactor/design/test)
        blocked_by: List of task IDs this depends on
        project: Project ID

    Returns:
        Dict with task info and warnings
    """
    prismind = PrismindAdapter(
        sse_url=settings.prismind_url,
        timeout=settings.prismind_timeout,
    )

    warnings: list[str] = []

    # Step 1: Get current progress for task_id generation and phase detection
    try:
        progress = await prismind.get_progress(project=project)
        all_tasks = _extract_tasks_from_progress(progress)
    except Exception as e:
        logger.warning("Failed to get progress, using T01", error=str(e))
        all_tasks = []

    # Auto-generate task_id
    task_id = _generate_next_task_id(all_tasks)

    # Determine phase
    if not phase:
        phase = progress.get("current_phase", "Phase 1") if progress else "Phase 1"

    # Step 2: Check for duplicate tasks
    try:
        search_query = f"{name} {description}"[:200]
        similar = await prismind.search_knowledge(
            query=search_query,
            category="task",
            project=project,
            limit=3,
        )
        if similar:
            for item in similar:
                score = item.get("score", item.get("similarity", 0))
                if score > 0.8:
                    warnings.append(
                        f"Similar task found: {item.get('content', '')[:100]}... "
                        f"(similarity: {score:.2f})"
                    )
    except Exception as e:
        logger.warning("Duplicate check failed", error=str(e))

    # Step 3: Validate blocked_by
    if blocked_by:
        existing_ids = {t.get("task_id") for t in all_tasks}
        invalid_deps = [dep for dep in blocked_by if dep not in existing_ids]
        if invalid_deps:
            return {
                "success": False,
                "error": f"Invalid blocked_by task IDs: {invalid_deps}",
                "existing_task_ids": list(existing_ids),
            }

    # Step 4: Add task via Prismind
    try:
        result = await prismind.add_task(
            phase=phase,
            task_id=task_id,
            name=name,
            description=description,
            project=project,
            priority=priority,
            category=category,
            blocked_by=blocked_by,
        )
    except Exception as e:
        logger.error("Failed to add task", error=str(e))
        return {
            "success": False,
            "error": f"Failed to add task: {e}",
        }

    # Step 5: Register as knowledge for searchability
    try:
        knowledge_content = f"Task {task_id}: {name}\n{description}"
        tags = [phase, task_id]
        if category:
            tags.append(category)
        if priority != "medium":
            tags.append(f"priority:{priority}")

        await prismind.add_knowledge(
            content=knowledge_content,
            category="task",
            project=project,
            tags=tags,
            source=f"task:{task_id}",
        )
    except Exception as e:
        logger.warning("Failed to register task as knowledge", error=str(e))
        warnings.append("Task added but knowledge registration failed")

    return {
        "success": True,
        "task_id": task_id,
        "phase": phase,
        "name": name,
        "priority": priority,
        "category": category,
        "blocked_by": blocked_by or [],
        "warnings": warnings,
        "message": result.get("message", f"Task {task_id} added successfully"),
    }


async def list_tasks_impl(
    settings: Settings,
    phase: str = "",
    status: str = "",
    priority: str = "",
    project: str = "",
    include_blocked: bool = True,
) -> dict[str, Any]:
    """List tasks with smart sorting and recommendations.

    Args:
        settings: Application settings
        phase: Filter by phase
        status: Filter by status
        priority: Filter by priority
        project: Project ID
        include_blocked: Include blocked tasks

    Returns:
        Dict with sorted tasks, recommended task, and stats
    """
    prismind = PrismindAdapter(
        sse_url=settings.prismind_url,
        timeout=settings.prismind_timeout,
    )

    try:
        progress = await prismind.get_progress(project=project, phase=phase)
    except Exception as e:
        logger.error("Failed to get progress", error=str(e))
        return {
            "success": False,
            "error": f"Failed to get tasks: {e}",
        }

    all_tasks = _extract_tasks_from_progress(progress)

    # Apply filters
    filtered_tasks = all_tasks
    if status:
        filtered_tasks = [t for t in filtered_tasks if t.get("status") == status]
    if priority:
        filtered_tasks = [t for t in filtered_tasks if t.get("priority") == priority]
    if not include_blocked:
        filtered_tasks = [t for t in filtered_tasks if t.get("status") != "blocked"]

    # Smart sort
    sorted_tasks = _smart_sort_tasks(filtered_tasks)

    # Find recommended task
    recommended = _find_recommended_task(all_tasks)

    # Mark recommended in sorted list
    if recommended:
        for task in sorted_tasks:
            if task.get("task_id") == recommended.get("task_id"):
                task["recommended"] = True
                break

    # Calculate stats
    stats = _calculate_stats(all_tasks)

    return {
        "success": True,
        "tasks": sorted_tasks,
        "recommended": recommended,
        "stats": stats,
        "current_phase": progress.get("current_phase", ""),
        "project": progress.get("project", project),
    }


async def start_task_impl(
    settings: Settings,
    task_id: str,
    project: str = "",
    force: bool = False,
) -> dict[str, Any]:
    """Start a task with dependency check and context retrieval.

    Args:
        settings: Application settings
        task_id: Task ID to start
        project: Project ID
        force: Start even if dependencies not met

    Returns:
        Dict with task info and related context
    """
    prismind = PrismindAdapter(
        sse_url=settings.prismind_url,
        timeout=settings.prismind_timeout,
    )

    # Get current progress
    try:
        progress = await prismind.get_progress(project=project)
        all_tasks = _extract_tasks_from_progress(progress)
    except Exception as e:
        logger.error("Failed to get progress", error=str(e))
        return {
            "success": False,
            "error": f"Failed to get task info: {e}",
        }

    # Find target task
    target_task = None
    for task in all_tasks:
        if task.get("task_id") == task_id:
            target_task = task
            break

    if not target_task:
        return {
            "success": False,
            "error": f"Task {task_id} not found",
        }

    # Check dependencies
    warnings: list[str] = []
    blocked_by = target_task.get("blocked_by", [])
    if blocked_by:
        completed_ids = {
            t.get("task_id")
            for t in all_tasks
            if t.get("status") == "completed"
        }
        incomplete_deps = [dep for dep in blocked_by if dep not in completed_ids]
        if incomplete_deps:
            if not force:
                return {
                    "success": False,
                    "error": f"Dependencies not completed: {incomplete_deps}",
                    "incomplete_dependencies": incomplete_deps,
                    "hint": "Use force=True to start anyway",
                }
            warnings.append(f"Starting with incomplete dependencies: {incomplete_deps}")

    # Start the task
    try:
        result = await prismind.start_task(
            task_id=task_id,
            project=project,
        )
    except Exception as e:
        logger.error("Failed to start task", error=str(e))
        return {
            "success": False,
            "error": f"Failed to start task: {e}",
        }

    # Get related context
    context: dict[str, Any] = {}
    try:
        task_name = target_task.get("name", "")
        task_desc = target_task.get("notes", target_task.get("description", ""))
        search_query = f"{task_name} {task_desc}"[:200]

        related_knowledge = await prismind.search_knowledge(
            query=search_query,
            project=project,
            limit=5,
        )
        context["related_knowledge"] = related_knowledge

        # Get dependency completion notes
        if blocked_by:
            dep_notes = []
            for task in all_tasks:
                if task.get("task_id") in blocked_by:
                    if task.get("notes"):
                        dep_notes.append({
                            "task_id": task.get("task_id"),
                            "name": task.get("name"),
                            "notes": task.get("notes"),
                        })
            context["dependency_notes"] = dep_notes

    except Exception as e:
        logger.warning("Failed to get related context", error=str(e))

    return {
        "success": True,
        "task_id": task_id,
        "task": target_task,
        "context": context,
        "warnings": warnings,
        "message": result.get("message", f"Task {task_id} started"),
    }


async def complete_task_impl(
    settings: Settings,
    task_id: str,
    notes: str = "",
    learnings: str = "",
    project: str = "",
) -> dict[str, Any]:
    """Complete a task with knowledge recording.

    Args:
        settings: Application settings
        task_id: Task ID to complete
        notes: Completion notes
        learnings: Learnings to record as knowledge
        project: Project ID

    Returns:
        Dict with completion info and unblocked tasks
    """
    prismind = PrismindAdapter(
        sse_url=settings.prismind_url,
        timeout=settings.prismind_timeout,
    )

    # Get current progress
    try:
        progress = await prismind.get_progress(project=project)
        all_tasks = _extract_tasks_from_progress(progress)
    except Exception as e:
        logger.error("Failed to get progress", error=str(e))
        return {
            "success": False,
            "error": f"Failed to get task info: {e}",
        }

    # Find target task
    target_task = None
    for task in all_tasks:
        if task.get("task_id") == task_id:
            target_task = task
            break

    if not target_task:
        return {
            "success": False,
            "error": f"Task {task_id} not found",
        }

    # Complete the task
    try:
        result = await prismind.complete_task(
            task_id=task_id,
            project=project,
            notes=notes,
        )
    except Exception as e:
        logger.error("Failed to complete task", error=str(e))
        return {
            "success": False,
            "error": f"Failed to complete task: {e}",
        }

    # Record learnings as knowledge
    if learnings:
        try:
            task_name = target_task.get("name", "")
            phase = target_task.get("phase", "")

            knowledge_content = (
                f"Task {task_id} ({task_name}) completed.\n\n"
                f"Learnings:\n{learnings}"
            )
            if notes:
                knowledge_content += f"\n\nNotes:\n{notes}"

            await prismind.add_knowledge(
                content=knowledge_content,
                category="task_completion",
                project=project,
                tags=[task_id, phase, "completed"],
                source=f"task:{task_id}:completion",
            )
        except Exception as e:
            logger.warning("Failed to record learnings", error=str(e))

    # Find unblocked tasks
    unblocked_tasks = _find_tasks_blocked_by(all_tasks, task_id)

    # Check which are now fully unblocked
    completed_ids = {
        t.get("task_id")
        for t in all_tasks
        if t.get("status") == "completed"
    }
    completed_ids.add(task_id)  # Include just-completed task

    newly_unblocked = []
    for task in unblocked_tasks:
        blocked_by = task.get("blocked_by", [])
        if all(dep in completed_ids for dep in blocked_by):
            newly_unblocked.append(task)

    # Find next recommended task
    # Update all_tasks with new completion status
    for task in all_tasks:
        if task.get("task_id") == task_id:
            task["status"] = "completed"
            break

    recommended = _find_recommended_task(all_tasks)

    return {
        "success": True,
        "task_id": task_id,
        "task": target_task,
        "newly_unblocked": newly_unblocked,
        "recommended_next": recommended,
        "message": result.get("message", f"Task {task_id} completed"),
    }


async def block_task_impl(
    settings: Settings,
    task_id: str,
    reason: str,
    blocked_by: list[str] | None = None,
    project: str = "",
) -> dict[str, Any]:
    """Block a task with reason recording.

    Args:
        settings: Application settings
        task_id: Task ID to block
        reason: Reason for blocking
        blocked_by: Task IDs causing the block
        project: Project ID

    Returns:
        Dict with block info and impact analysis
    """
    prismind = PrismindAdapter(
        sse_url=settings.prismind_url,
        timeout=settings.prismind_timeout,
    )

    # Get current progress
    try:
        progress = await prismind.get_progress(project=project)
        all_tasks = _extract_tasks_from_progress(progress)
    except Exception as e:
        logger.error("Failed to get progress", error=str(e))
        return {
            "success": False,
            "error": f"Failed to get task info: {e}",
        }

    # Find target task
    target_task = None
    for task in all_tasks:
        if task.get("task_id") == task_id:
            target_task = task
            break

    if not target_task:
        return {
            "success": False,
            "error": f"Task {task_id} not found",
        }

    # Block the task
    try:
        result = await prismind.block_task(
            task_id=task_id,
            reason=reason,
            project=project,
        )
    except Exception as e:
        logger.error("Failed to block task", error=str(e))
        return {
            "success": False,
            "error": f"Failed to block task: {e}",
        }

    # Record blocker as knowledge
    try:
        task_name = target_task.get("name", "")
        phase = target_task.get("phase", "")

        knowledge_content = (
            f"Task {task_id} ({task_name}) blocked.\n\n"
            f"Reason: {reason}"
        )
        if blocked_by:
            knowledge_content += f"\n\nBlocked by: {', '.join(blocked_by)}"

        await prismind.add_knowledge(
            content=knowledge_content,
            category="blocker",
            project=project,
            tags=[task_id, phase, "blocked"],
            source=f"task:{task_id}:blocked",
        )
    except Exception as e:
        logger.warning("Failed to record blocker", error=str(e))

    # Analyze impact - find tasks that depend on this blocked task
    impacted_tasks = _find_tasks_blocked_by(all_tasks, task_id)

    # Find cascade impact (tasks blocked by impacted tasks)
    cascade_impact: list[dict[str, Any]] = []
    checked = {task_id}
    to_check = [t.get("task_id") for t in impacted_tasks]

    while to_check:
        check_id = to_check.pop(0)
        if check_id in checked:
            continue
        checked.add(check_id)

        downstream = _find_tasks_blocked_by(all_tasks, check_id)
        for task in downstream:
            if task.get("task_id") not in checked:
                cascade_impact.append(task)
                to_check.append(task.get("task_id"))

    return {
        "success": True,
        "task_id": task_id,
        "task": target_task,
        "reason": reason,
        "directly_impacted": impacted_tasks,
        "cascade_impact": cascade_impact,
        "total_impacted": len(impacted_tasks) + len(cascade_impact),
        "message": result.get("message", f"Task {task_id} blocked"),
    }


def register_tools(mcp: FastMCP, settings: Settings) -> None:
    """Register task management tools with the MCP server.

    Args:
        mcp: FastMCP server instance
        settings: Application settings
    """
    global _settings
    _settings = settings

    @mcp.tool()
    async def add_task(
        name: str,
        description: str = "",
        phase: str = "",
        priority: str = "medium",
        category: str = "",
        blocked_by: list[str] | None = None,
        project: str = "",
    ) -> dict[str, Any]:
        """Add a new task with automatic ID generation and validation.

        USE THIS WHEN: Adding a new task to the project backlog.
        This tool:
        - Auto-generates task ID (T01, T02, etc.)
        - Checks for duplicate/similar tasks
        - Validates dependency task IDs
        - Records task info as searchable knowledge

        Args:
            name: Task name (required)
            description: Detailed description
            phase: Phase name (empty for current phase)
            priority: Priority level (high/medium/low)
            category: Category (bug/feature/refactor/design/test)
            blocked_by: Task IDs this depends on
            project: Project ID (empty for current)

        Returns:
            Dict with task_id, warnings, and status
        """
        if _settings is None:
            raise RuntimeError("Settings not initialized")

        return await add_task_impl(
            settings=_settings,
            name=name,
            description=description,
            phase=phase,
            priority=priority,
            category=category,
            blocked_by=blocked_by,
            project=project,
        )

    @mcp.tool()
    async def list_tasks(
        phase: str = "",
        status: str = "",
        priority: str = "",
        project: str = "",
        include_blocked: bool = True,
    ) -> dict[str, Any]:
        """List tasks with smart sorting and recommendations.

        USE THIS WHEN: Reviewing project tasks or deciding what to work on next.
        This tool:
        - Sorts by priority and blocked status
        - Recommends next task to work on
        - Provides task statistics

        Args:
            phase: Filter by phase
            status: Filter by status (not_started/in_progress/completed/blocked)
            priority: Filter by priority (high/medium/low)
            project: Project ID (empty for current)
            include_blocked: Include blocked tasks in results

        Returns:
            Dict with sorted tasks, recommended task, and stats
        """
        if _settings is None:
            raise RuntimeError("Settings not initialized")

        return await list_tasks_impl(
            settings=_settings,
            phase=phase,
            status=status,
            priority=priority,
            project=project,
            include_blocked=include_blocked,
        )

    @mcp.tool()
    async def start_task(
        task_id: str,
        project: str = "",
        force: bool = False,
    ) -> dict[str, Any]:
        """Start a task with dependency validation and context retrieval.

        USE THIS WHEN: Beginning work on a task.
        This tool:
        - Checks if dependencies are completed
        - Retrieves related knowledge and context
        - Gets completion notes from dependency tasks

        Args:
            task_id: Task ID to start (required)
            project: Project ID (empty for current)
            force: Start even if dependencies incomplete

        Returns:
            Dict with task info, related context, and warnings
        """
        if _settings is None:
            raise RuntimeError("Settings not initialized")

        return await start_task_impl(
            settings=_settings,
            task_id=task_id,
            project=project,
            force=force,
        )

    @mcp.tool()
    async def complete_task(
        task_id: str,
        notes: str = "",
        learnings: str = "",
        project: str = "",
    ) -> dict[str, Any]:
        """Complete a task with knowledge recording.

        USE THIS WHEN: Finishing work on a task.
        This tool:
        - Marks task as completed
        - Records learnings as searchable knowledge
        - Identifies newly unblocked tasks
        - Recommends next task to work on

        Args:
            task_id: Task ID to complete (required)
            notes: Completion notes
            learnings: Key learnings to record as knowledge
            project: Project ID (empty for current)

        Returns:
            Dict with completion status, unblocked tasks, and next recommendation
        """
        if _settings is None:
            raise RuntimeError("Settings not initialized")

        return await complete_task_impl(
            settings=_settings,
            task_id=task_id,
            notes=notes,
            learnings=learnings,
            project=project,
        )

    @mcp.tool()
    async def block_task(
        task_id: str,
        reason: str,
        blocked_by: list[str] | None = None,
        project: str = "",
    ) -> dict[str, Any]:
        """Block a task with reason and impact analysis.

        USE THIS WHEN: A task cannot proceed due to blockers.
        This tool:
        - Marks task as blocked with reason
        - Records blocker as searchable knowledge
        - Analyzes impact on dependent tasks
        - Shows cascade effect

        Args:
            task_id: Task ID to block (required)
            reason: Reason for blocking (required)
            blocked_by: Task IDs causing the block
            project: Project ID (empty for current)

        Returns:
            Dict with block status, impacted tasks, and cascade analysis
        """
        if _settings is None:
            raise RuntimeError("Settings not initialized")

        return await block_task_impl(
            settings=_settings,
            task_id=task_id,
            reason=reason,
            blocked_by=blocked_by,
            project=project,
        )
