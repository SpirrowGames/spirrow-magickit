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
from magickit.utils.user import get_current_user

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
    user: str = "",
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
        user: User identifier for multi-user support

    Returns:
        Dict with task info and warnings
    """
    # Auto-detect user if not specified
    effective_user = user or get_current_user()

    prismind = PrismindAdapter(
        sse_url=settings.prismind_url,
        timeout=settings.prismind_timeout,
    )

    warnings: list[str] = []

    # Step 1: Get current progress for task_id generation and phase detection
    try:
        progress = await prismind.get_progress(project=project, user=effective_user)
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
            user=effective_user,
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
            user=effective_user,
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
            user=effective_user,
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
    user: str = "",
) -> dict[str, Any]:
    """List tasks with smart sorting and recommendations.

    Args:
        settings: Application settings
        phase: Filter by phase
        status: Filter by status
        priority: Filter by priority
        project: Project ID
        include_blocked: Include blocked tasks
        user: User identifier for multi-user support

    Returns:
        Dict with sorted tasks, recommended task, and stats
    """
    # Auto-detect user if not specified
    effective_user = user or get_current_user()

    prismind = PrismindAdapter(
        sse_url=settings.prismind_url,
        timeout=settings.prismind_timeout,
    )

    try:
        progress = await prismind.get_progress(project=project, phase=phase, user=effective_user)
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
    phase: str = "",
    project: str = "",
    force: bool = False,
    user: str = "",
) -> dict[str, Any]:
    """Start a task with dependency check and context retrieval.

    Args:
        settings: Application settings
        task_id: Task ID to start
        phase: Phase name (specify if task_id exists in multiple phases)
        project: Project ID
        force: Start even if dependencies not met
        user: User identifier for multi-user support

    Returns:
        Dict with task info and related context
    """
    # Auto-detect user if not specified
    effective_user = user or get_current_user()

    prismind = PrismindAdapter(
        sse_url=settings.prismind_url,
        timeout=settings.prismind_timeout,
    )

    # Get current progress
    try:
        progress = await prismind.get_progress(project=project, user=effective_user)
        all_tasks = _extract_tasks_from_progress(progress)
    except Exception as e:
        logger.error("Failed to get progress", error=str(e))
        return {
            "success": False,
            "error": f"Failed to get task info: {e}",
        }

    # Find target task (consider phase if specified)
    target_task = None
    matching_tasks = []
    for task in all_tasks:
        if task.get("task_id") == task_id:
            if phase and task.get("phase") != phase:
                continue
            matching_tasks.append(task)

    if not matching_tasks:
        return {
            "success": False,
            "error": f"Task {task_id} not found" + (f" in phase {phase}" if phase else ""),
        }

    if len(matching_tasks) > 1:
        phases = [t.get("phase") for t in matching_tasks]
        return {
            "success": False,
            "error": f"Task {task_id} is ambiguous (exists in phases: {phases}). Specify phase parameter.",
        }

    target_task = matching_tasks[0]

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

    # Start the task (pass phase to avoid ambiguity)
    task_phase = target_task.get("phase", "")
    try:
        result = await prismind.start_task(
            task_id=task_id,
            phase=task_phase,
            project=project,
            user=effective_user,
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
            user=effective_user,
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
    phase: str = "",
    notes: str = "",
    learnings: str = "",
    project: str = "",
    user: str = "",
) -> dict[str, Any]:
    """Complete a task with knowledge recording.

    Args:
        settings: Application settings
        task_id: Task ID to complete
        phase: Phase name (specify if task_id exists in multiple phases)
        notes: Completion notes
        learnings: Learnings to record as knowledge
        project: Project ID
        user: User identifier for multi-user support

    Returns:
        Dict with completion info and unblocked tasks
    """
    # Auto-detect user if not specified
    effective_user = user or get_current_user()

    prismind = PrismindAdapter(
        sse_url=settings.prismind_url,
        timeout=settings.prismind_timeout,
    )

    # Get current progress
    try:
        progress = await prismind.get_progress(project=project, user=effective_user)
        all_tasks = _extract_tasks_from_progress(progress)
    except Exception as e:
        logger.error("Failed to get progress", error=str(e))
        return {
            "success": False,
            "error": f"Failed to get task info: {e}",
        }

    # Find target task (consider phase if specified)
    target_task = None
    matching_tasks = []
    for task in all_tasks:
        if task.get("task_id") == task_id:
            if phase and task.get("phase") != phase:
                continue
            matching_tasks.append(task)

    if not matching_tasks:
        return {
            "success": False,
            "error": f"Task {task_id} not found" + (f" in phase {phase}" if phase else ""),
        }

    if len(matching_tasks) > 1:
        phases = [t.get("phase") for t in matching_tasks]
        return {
            "success": False,
            "error": f"Task {task_id} is ambiguous (exists in phases: {phases}). Specify phase parameter.",
        }

    target_task = matching_tasks[0]

    # Complete the task (pass phase to avoid ambiguity)
    task_phase = target_task.get("phase", "")
    try:
        result = await prismind.complete_task(
            task_id=task_id,
            phase=task_phase,
            project=project,
            notes=notes,
            user=effective_user,
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
                user=effective_user,
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
    phase: str = "",
    blocked_by: list[str] | None = None,
    project: str = "",
    user: str = "",
) -> dict[str, Any]:
    """Block a task with reason recording.

    Args:
        settings: Application settings
        task_id: Task ID to block
        reason: Reason for blocking
        phase: Phase name (specify if task_id exists in multiple phases)
        blocked_by: Task IDs causing the block
        project: Project ID
        user: User identifier for multi-user support

    Returns:
        Dict with block info and impact analysis
    """
    # Auto-detect user if not specified
    effective_user = user or get_current_user()

    prismind = PrismindAdapter(
        sse_url=settings.prismind_url,
        timeout=settings.prismind_timeout,
    )

    # Get current progress
    try:
        progress = await prismind.get_progress(project=project, user=effective_user)
        all_tasks = _extract_tasks_from_progress(progress)
    except Exception as e:
        logger.error("Failed to get progress", error=str(e))
        return {
            "success": False,
            "error": f"Failed to get task info: {e}",
        }

    # Find target task (consider phase if specified)
    target_task = None
    matching_tasks = []
    for task in all_tasks:
        if task.get("task_id") == task_id:
            if phase and task.get("phase") != phase:
                continue
            matching_tasks.append(task)

    if not matching_tasks:
        return {
            "success": False,
            "error": f"Task {task_id} not found" + (f" in phase {phase}" if phase else ""),
        }

    if len(matching_tasks) > 1:
        phases = [t.get("phase") for t in matching_tasks]
        return {
            "success": False,
            "error": f"Task {task_id} is ambiguous (exists in phases: {phases}). Specify phase parameter.",
        }

    target_task = matching_tasks[0]

    # Block the task (pass phase to avoid ambiguity)
    task_phase = target_task.get("phase", "")
    try:
        result = await prismind.block_task(
            task_id=task_id,
            reason=reason,
            phase=task_phase,
            project=project,
            user=effective_user,
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
            user=effective_user,
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


async def get_task_impl(
    settings: Settings,
    task_id: str,
    phase: str = "",
    project: str = "",
    include_related_knowledge: bool = False,
    user: str = "",
) -> dict[str, Any]:
    """Get a single task by ID with optional related knowledge.

    Args:
        settings: Application settings
        task_id: Task ID to retrieve
        phase: Phase name (specify if task_id exists in multiple phases)
        project: Project ID
        include_related_knowledge: Include related knowledge entries
        user: User identifier for multi-user support

    Returns:
        Dict with task details and optional related knowledge
    """
    effective_user = user or get_current_user()

    prismind = PrismindAdapter(
        sse_url=settings.prismind_url,
        timeout=settings.prismind_timeout,
    )

    # Get task from Prismind
    try:
        result = await prismind.call(
            "get_task",
            task_id=task_id,
            phase=phase,
            project=project,
            user=effective_user,
        )
    except Exception as e:
        logger.error("Failed to get task", error=str(e))
        return {
            "success": False,
            "error": f"Failed to get task: {e}",
        }

    if not result.get("success"):
        return result

    response = {
        "success": True,
        "task": result.get("task"),
        "phase": result.get("phase"),
        "project": result.get("project"),
        "message": result.get("message"),
    }

    # Get related knowledge if requested
    if include_related_knowledge:
        try:
            task = result.get("task", {})
            task_name = task.get("name", "")
            task_notes = task.get("notes", "")
            search_query = f"{task_name} {task_notes}"[:200]

            related = await prismind.search_knowledge(
                query=search_query,
                project=project,
                limit=5,
                user=effective_user,
            )
            response["related_knowledge"] = related
        except Exception as e:
            logger.warning("Failed to get related knowledge", error=str(e))
            response["related_knowledge"] = []

    return response


async def delete_task_impl(
    settings: Settings,
    task_id: str,
    phase: str = "",
    project: str = "",
    check_dependencies: bool = True,
    cascade_unblock: bool = True,
    user: str = "",
) -> dict[str, Any]:
    """Delete a task with dependency impact analysis.

    Args:
        settings: Application settings
        task_id: Task ID to delete
        phase: Phase name (specify if task_id exists in multiple phases)
        project: Project ID
        check_dependencies: Check and warn about dependent tasks
        cascade_unblock: Automatically remove from blocked_by lists
        user: User identifier for multi-user support

    Returns:
        Dict with deletion result and impact info
    """
    effective_user = user or get_current_user()

    prismind = PrismindAdapter(
        sse_url=settings.prismind_url,
        timeout=settings.prismind_timeout,
    )

    # Get current progress for impact analysis
    if check_dependencies:
        try:
            progress = await prismind.get_progress(project=project, user=effective_user)
            all_tasks = _extract_tasks_from_progress(progress)
            impacted_tasks = _find_tasks_blocked_by(all_tasks, task_id)

            if impacted_tasks and not cascade_unblock:
                return {
                    "success": False,
                    "error": f"Task {task_id} has dependent tasks",
                    "impacted_tasks": impacted_tasks,
                    "hint": "Use cascade_unblock=True to automatically update dependent tasks",
                }
        except Exception as e:
            logger.warning("Failed to check dependencies", error=str(e))

    # Delete task via Prismind
    try:
        result = await prismind.call(
            "delete_task",
            task_id=task_id,
            phase=phase,
            project=project,
            user=effective_user,
        )
    except Exception as e:
        logger.error("Failed to delete task", error=str(e))
        return {
            "success": False,
            "error": f"Failed to delete task: {e}",
        }

    return {
        "success": result.get("success", False),
        "task_id": result.get("task_id"),
        "phase": result.get("phase"),
        "project": result.get("project"),
        "dependent_tasks_updated": result.get("dependent_tasks_updated", []),
        "message": result.get("message"),
    }


async def update_task_impl(
    settings: Settings,
    task_id: str,
    phase: str = "",
    name: str | None = None,
    description: str | None = None,
    status: str | None = None,
    priority: str | None = None,
    category: str | None = None,
    blocked_by: list[str] | None = None,
    blockers: list[str] | None = None,
    new_phase: str | None = None,
    project: str = "",
    user: str = "",
) -> dict[str, Any]:
    """Update a task with any combination of fields.

    Args:
        settings: Application settings
        task_id: Task ID to update
        phase: Current phase name (specify if task_id exists in multiple phases)
        name: New task name
        description: New description (stored in notes)
        status: New status (not_started/in_progress/completed/blocked)
        priority: New priority (high/medium/low)
        category: New category
        blocked_by: New blocked_by list
        blockers: New blockers list
        new_phase: Target phase for moving the task
        project: Project ID
        user: User identifier for multi-user support

    Returns:
        Dict with update result including phase move info
    """
    effective_user = user or get_current_user()

    prismind = PrismindAdapter(
        sse_url=settings.prismind_url,
        timeout=settings.prismind_timeout,
    )

    # Validate blocked_by if provided
    if blocked_by:
        try:
            progress = await prismind.get_progress(project=project, user=effective_user)
            all_tasks = _extract_tasks_from_progress(progress)
            existing_ids = {t.get("task_id") for t in all_tasks}

            invalid_deps = [dep for dep in blocked_by if dep not in existing_ids]
            if invalid_deps:
                return {
                    "success": False,
                    "error": f"Invalid blocked_by task IDs: {invalid_deps}",
                    "existing_task_ids": list(existing_ids),
                }
        except Exception as e:
            logger.warning("Failed to validate dependencies", error=str(e))

    # Update task via Prismind
    try:
        result = await prismind.call(
            "update_task",
            task_id=task_id,
            phase=phase,
            name=name,
            description=description,
            status=status,
            priority=priority,
            category=category,
            blocked_by=blocked_by,
            blockers=blockers,
            new_phase=new_phase,
            project=project,
            user=effective_user,
        )
    except Exception as e:
        logger.error("Failed to update task", error=str(e))
        return {
            "success": False,
            "error": f"Failed to update task: {e}",
        }

    return {
        "success": result.get("success", False),
        "task_id": result.get("task_id"),
        "project": result.get("project"),
        "updated_fields": result.get("updated_fields", []),
        "phase_moved": result.get("phase_moved", False),
        "old_phase": result.get("old_phase", ""),
        "new_phase": result.get("new_phase", ""),
        "message": result.get("message"),
    }


# === Shortcut/Convenience Functions ===


async def move_task_to_phase_impl(
    settings: Settings,
    task_id: str,
    from_phase: str,
    to_phase: str,
    project: str = "",
    user: str = "",
) -> dict[str, Any]:
    """Move a task from one phase to another.

    Shortcut for update_task with new_phase.

    Args:
        settings: Application settings
        task_id: Task ID to move
        from_phase: Current phase name
        to_phase: Target phase name
        project: Project ID
        user: User identifier

    Returns:
        Dict with move result
    """
    return await update_task_impl(
        settings=settings,
        task_id=task_id,
        phase=from_phase,
        new_phase=to_phase,
        project=project,
        user=user,
    )


async def set_task_priority_impl(
    settings: Settings,
    task_id: str,
    priority: str,
    phase: str = "",
    project: str = "",
    user: str = "",
) -> dict[str, Any]:
    """Set task priority.

    Shortcut for update_task with priority.

    Args:
        settings: Application settings
        task_id: Task ID
        priority: New priority (high/medium/low)
        phase: Phase name (if needed)
        project: Project ID
        user: User identifier

    Returns:
        Dict with update result
    """
    valid_priorities = ["high", "medium", "low"]
    if priority not in valid_priorities:
        return {
            "success": False,
            "error": f"Invalid priority. Valid values: {valid_priorities}",
        }

    return await update_task_impl(
        settings=settings,
        task_id=task_id,
        phase=phase,
        priority=priority,
        project=project,
        user=user,
    )


async def set_task_blockers_impl(
    settings: Settings,
    task_id: str,
    blocked_by: list[str],
    phase: str = "",
    project: str = "",
    validate: bool = True,
    user: str = "",
) -> dict[str, Any]:
    """Set task dependencies (blocked_by).

    Shortcut for update_task with blocked_by.

    Args:
        settings: Application settings
        task_id: Task ID
        blocked_by: List of task IDs this depends on
        phase: Phase name (if needed)
        project: Project ID
        validate: Validate that blocked_by IDs exist
        user: User identifier

    Returns:
        Dict with update result
    """
    effective_user = user or get_current_user()

    prismind = PrismindAdapter(
        sse_url=settings.prismind_url,
        timeout=settings.prismind_timeout,
    )

    # Validate dependencies if requested
    if validate and blocked_by:
        try:
            progress = await prismind.get_progress(project=project, user=effective_user)
            all_tasks = _extract_tasks_from_progress(progress)
            existing_ids = {t.get("task_id") for t in all_tasks}

            invalid_deps = [dep for dep in blocked_by if dep not in existing_ids]
            if invalid_deps:
                return {
                    "success": False,
                    "error": f"Invalid blocked_by task IDs: {invalid_deps}",
                    "existing_task_ids": list(existing_ids),
                }
        except Exception as e:
            logger.warning("Failed to validate dependencies", error=str(e))

    return await update_task_impl(
        settings=settings,
        task_id=task_id,
        phase=phase,
        blocked_by=blocked_by,
        project=project,
        user=user,
    )


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
        user: str = "",
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
            user: User identifier for multi-user support (auto-detected if empty)

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
            user=user,
        )

    @mcp.tool()
    async def list_tasks(
        phase: str = "",
        status: str = "",
        priority: str = "",
        project: str = "",
        include_blocked: bool = True,
        user: str = "",
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
            user: User identifier for multi-user support (auto-detected if empty)

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
            user=user,
        )

    @mcp.tool()
    async def start_task(
        task_id: str,
        phase: str = "",
        project: str = "",
        force: bool = False,
        user: str = "",
    ) -> dict[str, Any]:
        """Start a task with dependency validation and context retrieval.

        USE THIS WHEN: Beginning work on a task.
        This tool:
        - Checks if dependencies are completed
        - Retrieves related knowledge and context
        - Gets completion notes from dependency tasks

        Args:
            task_id: Task ID to start (required)
            phase: Phase name (required if same task_id exists in multiple phases)
            project: Project ID (empty for current)
            force: Start even if dependencies incomplete
            user: User identifier for multi-user support (auto-detected if empty)

        Returns:
            Dict with task info, related context, and warnings
        """
        if _settings is None:
            raise RuntimeError("Settings not initialized")

        return await start_task_impl(
            settings=_settings,
            task_id=task_id,
            phase=phase,
            project=project,
            force=force,
            user=user,
        )

    @mcp.tool()
    async def complete_task(
        task_id: str,
        phase: str = "",
        notes: str = "",
        learnings: str = "",
        project: str = "",
        user: str = "",
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
            phase: Phase name (required if same task_id exists in multiple phases)
            notes: Completion notes
            learnings: Key learnings to record as knowledge
            project: Project ID (empty for current)
            user: User identifier for multi-user support (auto-detected if empty)

        Returns:
            Dict with completion status, unblocked tasks, and next recommendation
        """
        if _settings is None:
            raise RuntimeError("Settings not initialized")

        return await complete_task_impl(
            settings=_settings,
            task_id=task_id,
            phase=phase,
            notes=notes,
            learnings=learnings,
            project=project,
            user=user,
        )

    @mcp.tool()
    async def block_task(
        task_id: str,
        reason: str,
        phase: str = "",
        blocked_by: list[str] | None = None,
        project: str = "",
        user: str = "",
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
            phase: Phase name (required if same task_id exists in multiple phases)
            blocked_by: Task IDs causing the block
            project: Project ID (empty for current)
            user: User identifier for multi-user support (auto-detected if empty)

        Returns:
            Dict with block status, impacted tasks, and cascade analysis
        """
        if _settings is None:
            raise RuntimeError("Settings not initialized")

        return await block_task_impl(
            settings=_settings,
            task_id=task_id,
            reason=reason,
            phase=phase,
            blocked_by=blocked_by,
            project=project,
            user=user,
        )

    @mcp.tool()
    async def get_task(
        task_id: str,
        phase: str = "",
        project: str = "",
        include_related_knowledge: bool = False,
        user: str = "",
    ) -> dict[str, Any]:
        """Get a single task by ID with full details.

        USE THIS WHEN: You need detailed information about a specific task.
        This tool:
        - Retrieves task with all fields (name, status, priority, etc.)
        - Optionally includes related knowledge entries

        Args:
            task_id: Task ID to retrieve (required)
            phase: Phase name (required if same task_id exists in multiple phases)
            project: Project ID (empty for current)
            include_related_knowledge: Include related knowledge entries
            user: User identifier for multi-user support (auto-detected if empty)

        Returns:
            Dict with task details and optional related knowledge
        """
        if _settings is None:
            raise RuntimeError("Settings not initialized")

        return await get_task_impl(
            settings=_settings,
            task_id=task_id,
            phase=phase,
            project=project,
            include_related_knowledge=include_related_knowledge,
            user=user,
        )

    @mcp.tool()
    async def delete_task(
        task_id: str,
        phase: str = "",
        project: str = "",
        check_dependencies: bool = True,
        cascade_unblock: bool = True,
        user: str = "",
    ) -> dict[str, Any]:
        """Delete a task with dependency handling.

        USE THIS WHEN: Removing a task that is no longer needed.
        This tool:
        - Checks for dependent tasks (tasks blocked by this one)
        - Automatically updates blocked_by references if cascade_unblock=True
        - Records deletion impact

        Args:
            task_id: Task ID to delete (required)
            phase: Phase name (required if same task_id exists in multiple phases)
            project: Project ID (empty for current)
            check_dependencies: Check and warn about dependent tasks
            cascade_unblock: Automatically remove from blocked_by lists
            user: User identifier for multi-user support (auto-detected if empty)

        Returns:
            Dict with deletion status and updated dependent tasks
        """
        if _settings is None:
            raise RuntimeError("Settings not initialized")

        return await delete_task_impl(
            settings=_settings,
            task_id=task_id,
            phase=phase,
            project=project,
            check_dependencies=check_dependencies,
            cascade_unblock=cascade_unblock,
            user=user,
        )

    @mcp.tool()
    async def update_task(
        task_id: str,
        phase: str = "",
        name: str | None = None,
        description: str | None = None,
        status: str | None = None,
        priority: str | None = None,
        category: str | None = None,
        blocked_by: list[str] | None = None,
        blockers: list[str] | None = None,
        new_phase: str | None = None,
        project: str = "",
        user: str = "",
    ) -> dict[str, Any]:
        """Update a task with any combination of fields.

        USE THIS WHEN: Modifying task details, changing status, or moving phases.
        This tool:
        - Updates any combination of task fields
        - Supports moving task to a different phase
        - Validates dependencies if blocked_by is provided

        Args:
            task_id: Task ID to update (required)
            phase: Current phase name (required if same task_id exists in multiple phases)
            name: New task name
            description: New description (stored in notes)
            status: New status (not_started/in_progress/completed/blocked)
            priority: New priority (high/medium/low)
            category: New category
            blocked_by: New blocked_by list (task dependencies)
            blockers: New blockers list (obstacles)
            new_phase: Target phase for moving the task
            project: Project ID (empty for current)
            user: User identifier for multi-user support (auto-detected if empty)

        Returns:
            Dict with updated fields and phase move info
        """
        if _settings is None:
            raise RuntimeError("Settings not initialized")

        return await update_task_impl(
            settings=_settings,
            task_id=task_id,
            phase=phase,
            name=name,
            description=description,
            status=status,
            priority=priority,
            category=category,
            blocked_by=blocked_by,
            blockers=blockers,
            new_phase=new_phase,
            project=project,
            user=user,
        )

    @mcp.tool()
    async def move_task_to_phase(
        task_id: str,
        from_phase: str,
        to_phase: str,
        project: str = "",
        user: str = "",
    ) -> dict[str, Any]:
        """Move a task from one phase to another.

        USE THIS WHEN: Reorganizing tasks between phases.
        Shortcut for update_task with new_phase.

        Args:
            task_id: Task ID to move (required)
            from_phase: Current phase name (required)
            to_phase: Target phase name (required)
            project: Project ID (empty for current)
            user: User identifier for multi-user support (auto-detected if empty)

        Returns:
            Dict with move result
        """
        if _settings is None:
            raise RuntimeError("Settings not initialized")

        return await move_task_to_phase_impl(
            settings=_settings,
            task_id=task_id,
            from_phase=from_phase,
            to_phase=to_phase,
            project=project,
            user=user,
        )

    @mcp.tool()
    async def set_task_priority(
        task_id: str,
        priority: str,
        phase: str = "",
        project: str = "",
        user: str = "",
    ) -> dict[str, Any]:
        """Set task priority.

        USE THIS WHEN: Changing task priority level.
        Shortcut for update_task with priority.

        Args:
            task_id: Task ID (required)
            priority: New priority (high/medium/low) (required)
            phase: Phase name (required if same task_id exists in multiple phases)
            project: Project ID (empty for current)
            user: User identifier for multi-user support (auto-detected if empty)

        Returns:
            Dict with update result
        """
        if _settings is None:
            raise RuntimeError("Settings not initialized")

        return await set_task_priority_impl(
            settings=_settings,
            task_id=task_id,
            priority=priority,
            phase=phase,
            project=project,
            user=user,
        )

    @mcp.tool()
    async def set_task_blockers(
        task_id: str,
        blocked_by: list[str],
        phase: str = "",
        project: str = "",
        validate: bool = True,
        user: str = "",
    ) -> dict[str, Any]:
        """Set task dependencies (blocked_by).

        USE THIS WHEN: Updating task dependencies.
        Shortcut for update_task with blocked_by.

        Args:
            task_id: Task ID (required)
            blocked_by: List of task IDs this depends on (required)
            phase: Phase name (required if same task_id exists in multiple phases)
            project: Project ID (empty for current)
            validate: Validate that blocked_by IDs exist
            user: User identifier for multi-user support (auto-detected if empty)

        Returns:
            Dict with update result
        """
        if _settings is None:
            raise RuntimeError("Settings not initialized")

        return await set_task_blockers_impl(
            settings=_settings,
            task_id=task_id,
            blocked_by=blocked_by,
            phase=phase,
            project=project,
            validate=validate,
            user=user,
        )
