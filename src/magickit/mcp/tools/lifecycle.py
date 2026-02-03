"""Lifecycle management tools for Magickit MCP server.

Provides tools for managing game development project phases and milestones:
- Phase transitions (advance_phase, set_phase, get_phase_status)
- Milestone management (add, update, list, check status)
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from fastmcp import FastMCP

from magickit.adapters.prismind import PrismindAdapter
from magickit.config import Settings
from magickit.utils.logging import get_logger
from magickit.utils.user import get_current_user

logger = get_logger(__name__)

# Module-level settings reference
_settings: Settings | None = None

# Default phase order for game development
DEFAULT_PHASE_ORDER = ["pre-production", "production", "polish", "release"]


def _parse_result(result: Any) -> dict[str, Any]:
    """Parse tool result to dict."""
    if result is None:
        return {}
    if isinstance(result, dict):
        return result
    if isinstance(result, str):
        try:
            data = json.loads(result)
            if isinstance(data, dict):
                return data
            return {"result": data}
        except json.JSONDecodeError:
            return {"result": result}
    return {"result": result}


def _parse_list_result(result: Any) -> list[dict[str, Any]]:
    """Parse tool result to list."""
    if result is None:
        return []
    if isinstance(result, list):
        return result
    if isinstance(result, str):
        try:
            data = json.loads(result)
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                for key in ["results", "items", "documents", "knowledge", "milestones"]:
                    if key in data and isinstance(data[key], list):
                        return data[key]
                return [data]
            return [{"result": data}]
        except json.JSONDecodeError:
            return [{"result": result}]
    return [{"result": result}]


def _extract_tasks_from_progress(progress: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract flat task list from progress response."""
    tasks = []
    phases = progress.get("phases", [])

    for phase_data in phases:
        phase_name = phase_data.get("phase", "")
        for task in phase_data.get("tasks", []):
            task_with_phase = {**task, "phase": phase_name}
            tasks.append(task_with_phase)

    return tasks


def _calculate_phase_completion(
    tasks: list[dict[str, Any]],
    phase: str,
) -> dict[str, Any]:
    """Calculate completion stats for a phase."""
    phase_tasks = [t for t in tasks if t.get("phase") == phase]
    total = len(phase_tasks)

    if total == 0:
        return {
            "total": 0,
            "completed": 0,
            "in_progress": 0,
            "blocked": 0,
            "not_started": 0,
            "completion_percent": 100.0,  # Empty phase is considered complete
        }

    stats = {
        "total": total,
        "completed": 0,
        "in_progress": 0,
        "blocked": 0,
        "not_started": 0,
    }

    for task in phase_tasks:
        status = task.get("status", "not_started")
        if status in stats:
            stats[status] += 1
        else:
            stats["not_started"] += 1

    stats["completion_percent"] = round((stats["completed"] / total) * 100, 1)

    return stats


def register_tools(mcp: FastMCP, settings: Settings) -> None:
    """Register lifecycle management tools with the MCP server.

    Args:
        mcp: FastMCP server instance.
        settings: Application settings.
    """
    global _settings
    _settings = settings

    @mcp.tool()
    async def advance_phase(
        project: str,
        force: bool = False,
        completion_threshold: float = 80.0,
        user: str = "",
    ) -> dict[str, Any]:
        """Advance project to the next phase with completion checks.

        USE THIS WHEN: Project is ready to move to the next development phase.
        This tool:
        - Checks current phase completion (task completion %)
        - Validates phase order
        - Records phase transition in knowledge

        Args:
            project: Project identifier.
            force: Skip completion check and advance anyway.
            completion_threshold: Minimum completion % required (default 80%).
            user: User identifier for multi-user support.

        Returns:
            Dict containing:
            - success: Whether the phase was advanced
            - previous_phase: Phase before transition
            - current_phase: New current phase
            - completion_stats: Stats from previous phase
            - warnings: Any warnings about incomplete tasks
            - message: Status message
        """
        if _settings is None:
            raise RuntimeError("Settings not initialized")

        effective_user = user or get_current_user()

        prismind = PrismindAdapter(
            sse_url=_settings.prismind_url,
            timeout=_settings.prismind_timeout,
        )

        logger.info(
            "Advancing phase",
            project=project,
            force=force,
            threshold=completion_threshold,
            user=effective_user,
        )

        # Get current progress
        try:
            progress = await prismind.get_progress(project=project, user=effective_user)
        except Exception as e:
            logger.error("Failed to get progress", error=str(e))
            return {
                "success": False,
                "previous_phase": "",
                "current_phase": "",
                "completion_stats": {},
                "warnings": [],
                "message": f"Failed to get project progress: {e}",
            }

        current_phase = progress.get("current_phase", "")
        phases = progress.get("phases", [])
        phase_names = [p.get("phase", "") for p in phases] or DEFAULT_PHASE_ORDER

        # Find next phase
        if current_phase not in phase_names:
            return {
                "success": False,
                "previous_phase": current_phase,
                "current_phase": current_phase,
                "completion_stats": {},
                "warnings": [],
                "message": f"Current phase '{current_phase}' not found in project phases: {phase_names}",
            }

        current_index = phase_names.index(current_phase)
        if current_index >= len(phase_names) - 1:
            return {
                "success": False,
                "previous_phase": current_phase,
                "current_phase": current_phase,
                "completion_stats": {},
                "warnings": [],
                "message": f"Already at final phase '{current_phase}'. Cannot advance further.",
            }

        next_phase = phase_names[current_index + 1]

        # Calculate completion stats for current phase
        all_tasks = _extract_tasks_from_progress(progress)
        completion_stats = _calculate_phase_completion(all_tasks, current_phase)

        warnings: list[str] = []

        # Check completion threshold
        if completion_stats["completion_percent"] < completion_threshold:
            if not force:
                return {
                    "success": False,
                    "previous_phase": current_phase,
                    "current_phase": current_phase,
                    "completion_stats": completion_stats,
                    "warnings": [],
                    "message": (
                        f"Phase '{current_phase}' is only {completion_stats['completion_percent']}% complete. "
                        f"Required: {completion_threshold}%. Use force=True to advance anyway."
                    ),
                }
            warnings.append(
                f"Advancing with only {completion_stats['completion_percent']}% completion "
                f"(threshold: {completion_threshold}%)"
            )

        # Check for blocked tasks
        if completion_stats["blocked"] > 0:
            warnings.append(f"{completion_stats['blocked']} task(s) are still blocked")

        # Check for in-progress tasks
        if completion_stats["in_progress"] > 0:
            warnings.append(f"{completion_stats['in_progress']} task(s) still in progress")

        # Advance the phase
        try:
            await prismind.update_progress(
                current_phase=next_phase,
                project=project,
                user=effective_user,
            )
        except Exception as e:
            logger.error("Failed to update phase", error=str(e))
            return {
                "success": False,
                "previous_phase": current_phase,
                "current_phase": current_phase,
                "completion_stats": completion_stats,
                "warnings": warnings,
                "message": f"Failed to advance phase: {e}",
            }

        # Record phase transition as knowledge
        try:
            transition_content = (
                f"Phase transition: {current_phase} → {next_phase}\n"
                f"Completion: {completion_stats['completion_percent']}%\n"
                f"Tasks: {completion_stats['completed']}/{completion_stats['total']} completed\n"
                f"Date: {datetime.now().isoformat()}"
            )
            if warnings:
                transition_content += f"\nWarnings: {'; '.join(warnings)}"

            await prismind.add_knowledge(
                content=transition_content,
                category="phase_transition",
                project=project,
                tags=[current_phase, next_phase, "lifecycle"],
                source=f"phase:{current_phase}:{next_phase}",
                user=effective_user,
            )
        except Exception as e:
            logger.warning("Failed to record phase transition", error=str(e))
            warnings.append("Phase transition not recorded in knowledge")

        logger.info(
            "Phase advanced",
            project=project,
            from_phase=current_phase,
            to_phase=next_phase,
        )

        return {
            "success": True,
            "previous_phase": current_phase,
            "current_phase": next_phase,
            "completion_stats": completion_stats,
            "warnings": warnings,
            "message": f"Advanced from '{current_phase}' to '{next_phase}'",
        }

    @mcp.tool()
    async def set_phase(
        project: str,
        phase: str,
        user: str = "",
    ) -> dict[str, Any]:
        """Manually set the current project phase.

        USE THIS WHEN: You need to set a specific phase without sequential advancement.
        Use this for corrections or jumping between phases.

        Args:
            project: Project identifier.
            phase: Target phase name.
            user: User identifier for multi-user support.

        Returns:
            Dict containing:
            - success: Whether the phase was set
            - previous_phase: Previous phase
            - current_phase: New current phase
            - message: Status message
        """
        if _settings is None:
            raise RuntimeError("Settings not initialized")

        effective_user = user or get_current_user()

        prismind = PrismindAdapter(
            sse_url=_settings.prismind_url,
            timeout=_settings.prismind_timeout,
        )

        logger.info(
            "Setting phase",
            project=project,
            phase=phase,
            user=effective_user,
        )

        # Get current phase
        try:
            progress = await prismind.get_progress(project=project, user=effective_user)
            previous_phase = progress.get("current_phase", "")
        except Exception as e:
            logger.warning("Failed to get current phase", error=str(e))
            previous_phase = ""

        # Validate phase exists in project
        phases = progress.get("phases", [])
        phase_names = [p.get("phase", "") for p in phases]
        if phase_names and phase not in phase_names:
            return {
                "success": False,
                "previous_phase": previous_phase,
                "current_phase": previous_phase,
                "message": f"Phase '{phase}' not found. Available phases: {phase_names}",
            }

        # Set the phase
        try:
            await prismind.update_progress(
                current_phase=phase,
                project=project,
                user=effective_user,
            )
        except Exception as e:
            logger.error("Failed to set phase", error=str(e))
            return {
                "success": False,
                "previous_phase": previous_phase,
                "current_phase": previous_phase,
                "message": f"Failed to set phase: {e}",
            }

        logger.info(
            "Phase set",
            project=project,
            from_phase=previous_phase,
            to_phase=phase,
        )

        return {
            "success": True,
            "previous_phase": previous_phase,
            "current_phase": phase,
            "message": f"Phase set to '{phase}'" + (f" (was '{previous_phase}')" if previous_phase else ""),
        }

    @mcp.tool()
    async def get_phase_status(
        project: str,
        phase: str = "",
        user: str = "",
    ) -> dict[str, Any]:
        """Get detailed status for a project phase.

        USE THIS WHEN: You need detailed information about a specific phase
        including task breakdown and completion metrics.

        Args:
            project: Project identifier.
            phase: Phase to get status for (empty for current phase).
            user: User identifier for multi-user support.

        Returns:
            Dict containing:
            - phase: Phase name
            - is_current: Whether this is the current phase
            - stats: Task completion statistics
            - tasks: List of tasks in this phase
            - blockers: List of blocked tasks
            - in_progress: List of in-progress tasks
            - message: Status message
        """
        if _settings is None:
            raise RuntimeError("Settings not initialized")

        effective_user = user or get_current_user()

        prismind = PrismindAdapter(
            sse_url=_settings.prismind_url,
            timeout=_settings.prismind_timeout,
        )

        logger.info(
            "Getting phase status",
            project=project,
            phase=phase,
            user=effective_user,
        )

        # Get progress
        try:
            progress = await prismind.get_progress(project=project, user=effective_user)
        except Exception as e:
            logger.error("Failed to get progress", error=str(e))
            return {
                "phase": phase,
                "is_current": False,
                "stats": {},
                "tasks": [],
                "blockers": [],
                "in_progress": [],
                "message": f"Failed to get project progress: {e}",
            }

        current_phase = progress.get("current_phase", "")
        target_phase = phase or current_phase

        all_tasks = _extract_tasks_from_progress(progress)
        phase_tasks = [t for t in all_tasks if t.get("phase") == target_phase]

        # Calculate stats
        stats = _calculate_phase_completion(all_tasks, target_phase)

        # Categorize tasks
        blockers = [t for t in phase_tasks if t.get("status") == "blocked"]
        in_progress_tasks = [t for t in phase_tasks if t.get("status") == "in_progress"]

        return {
            "phase": target_phase,
            "is_current": target_phase == current_phase,
            "stats": stats,
            "tasks": phase_tasks,
            "blockers": blockers,
            "in_progress": in_progress_tasks,
            "message": f"Phase '{target_phase}': {stats['completion_percent']}% complete",
        }

    @mcp.tool()
    async def add_milestone(
        project: str,
        name: str,
        target_date: str,
        phase: str = "",
        description: str = "",
        user: str = "",
    ) -> dict[str, Any]:
        """Add a milestone to the project.

        USE THIS WHEN: Setting up project milestones (Alpha, Beta, Release, etc.).

        Args:
            project: Project identifier.
            name: Milestone name (e.g., "Alpha", "Beta", "Release").
            target_date: Target date in ISO format (YYYY-MM-DD).
            phase: Associated phase (optional).
            description: Milestone description.
            user: User identifier for multi-user support.

        Returns:
            Dict containing:
            - success: Whether the milestone was added
            - milestone_id: ID of the created milestone
            - name: Milestone name
            - target_date: Target date
            - message: Status message
        """
        if _settings is None:
            raise RuntimeError("Settings not initialized")

        effective_user = user or get_current_user()

        prismind = PrismindAdapter(
            sse_url=_settings.prismind_url,
            timeout=_settings.prismind_timeout,
        )

        logger.info(
            "Adding milestone",
            project=project,
            name=name,
            target_date=target_date,
            phase=phase,
            user=effective_user,
        )

        # Validate date format
        try:
            datetime.fromisoformat(target_date)
        except ValueError:
            return {
                "success": False,
                "milestone_id": "",
                "name": name,
                "target_date": target_date,
                "message": f"Invalid date format: {target_date}. Use YYYY-MM-DD.",
            }

        # Create milestone as knowledge entry
        milestone_id = f"milestone:{name.lower().replace(' ', '_')}"
        milestone_content = json.dumps({
            "type": "milestone",
            "name": name,
            "target_date": target_date,
            "phase": phase,
            "description": description,
            "status": "pending",
            "created_at": datetime.now().isoformat(),
        })

        try:
            await prismind.add_knowledge(
                content=milestone_content,
                category="milestone",
                project=project,
                tags=["milestone", name.lower(), phase] if phase else ["milestone", name.lower()],
                source=milestone_id,
                user=effective_user,
            )
        except Exception as e:
            logger.error("Failed to add milestone", error=str(e))
            return {
                "success": False,
                "milestone_id": "",
                "name": name,
                "target_date": target_date,
                "message": f"Failed to add milestone: {e}",
            }

        logger.info(
            "Milestone added",
            project=project,
            name=name,
            target_date=target_date,
        )

        return {
            "success": True,
            "milestone_id": milestone_id,
            "name": name,
            "target_date": target_date,
            "phase": phase,
            "message": f"Milestone '{name}' added with target date {target_date}",
        }

    @mcp.tool()
    async def update_milestone(
        project: str,
        name: str,
        target_date: str = "",
        status: str = "",
        actual_date: str = "",
        notes: str = "",
        user: str = "",
    ) -> dict[str, Any]:
        """Update an existing milestone.

        USE THIS WHEN: Updating milestone dates, status, or notes.

        Args:
            project: Project identifier.
            name: Milestone name to update.
            target_date: New target date (YYYY-MM-DD).
            status: New status (pending/in_progress/completed/delayed).
            actual_date: Actual completion date (YYYY-MM-DD).
            notes: Additional notes.
            user: User identifier for multi-user support.

        Returns:
            Dict containing:
            - success: Whether the milestone was updated
            - name: Milestone name
            - status: New status
            - message: Status message
        """
        if _settings is None:
            raise RuntimeError("Settings not initialized")

        effective_user = user or get_current_user()

        prismind = PrismindAdapter(
            sse_url=_settings.prismind_url,
            timeout=_settings.prismind_timeout,
        )

        logger.info(
            "Updating milestone",
            project=project,
            name=name,
            status=status,
            user=effective_user,
        )

        # Search for existing milestone
        try:
            results = await prismind.search_knowledge(
                query=f"milestone {name}",
                category="milestone",
                project=project,
                limit=5,
                user=effective_user,
            )
        except Exception as e:
            logger.error("Failed to search milestones", error=str(e))
            return {
                "success": False,
                "name": name,
                "status": status,
                "message": f"Failed to find milestone: {e}",
            }

        # Find matching milestone
        milestone_data = None
        for result in results:
            content = result.get("content", "")
            try:
                data = json.loads(content)
                if data.get("type") == "milestone" and data.get("name", "").lower() == name.lower():
                    milestone_data = data
                    break
            except (json.JSONDecodeError, TypeError):
                continue

        if not milestone_data:
            return {
                "success": False,
                "name": name,
                "status": status,
                "message": f"Milestone '{name}' not found in project '{project}'",
            }

        # Update milestone data
        if target_date:
            try:
                datetime.fromisoformat(target_date)
                milestone_data["target_date"] = target_date
            except ValueError:
                return {
                    "success": False,
                    "name": name,
                    "status": status,
                    "message": f"Invalid target_date format: {target_date}",
                }

        if actual_date:
            try:
                datetime.fromisoformat(actual_date)
                milestone_data["actual_date"] = actual_date
            except ValueError:
                return {
                    "success": False,
                    "name": name,
                    "status": status,
                    "message": f"Invalid actual_date format: {actual_date}",
                }

        if status:
            valid_statuses = ["pending", "in_progress", "completed", "delayed"]
            if status not in valid_statuses:
                return {
                    "success": False,
                    "name": name,
                    "status": status,
                    "message": f"Invalid status: {status}. Valid: {valid_statuses}",
                }
            milestone_data["status"] = status

        if notes:
            milestone_data["notes"] = notes

        milestone_data["updated_at"] = datetime.now().isoformat()

        # Save updated milestone
        milestone_id = f"milestone:{name.lower().replace(' ', '_')}"
        try:
            await prismind.add_knowledge(
                content=json.dumps(milestone_data),
                category="milestone",
                project=project,
                tags=["milestone", name.lower(), milestone_data.get("phase", "")],
                source=milestone_id,
                user=effective_user,
            )
        except Exception as e:
            logger.error("Failed to update milestone", error=str(e))
            return {
                "success": False,
                "name": name,
                "status": milestone_data.get("status", ""),
                "message": f"Failed to update milestone: {e}",
            }

        logger.info(
            "Milestone updated",
            project=project,
            name=name,
            status=milestone_data.get("status"),
        )

        return {
            "success": True,
            "name": name,
            "status": milestone_data.get("status", ""),
            "target_date": milestone_data.get("target_date", ""),
            "actual_date": milestone_data.get("actual_date", ""),
            "message": f"Milestone '{name}' updated",
        }

    @mcp.tool()
    async def list_milestones(
        project: str,
        user: str = "",
    ) -> dict[str, Any]:
        """List all milestones for a project.

        USE THIS WHEN: You need to see all project milestones and their status.

        Args:
            project: Project identifier.
            user: User identifier for multi-user support.

        Returns:
            Dict containing:
            - milestones: List of milestone objects
            - total: Total number of milestones
            - pending: Count of pending milestones
            - completed: Count of completed milestones
            - delayed: Count of delayed milestones
            - message: Status message
        """
        if _settings is None:
            raise RuntimeError("Settings not initialized")

        effective_user = user or get_current_user()

        prismind = PrismindAdapter(
            sse_url=_settings.prismind_url,
            timeout=_settings.prismind_timeout,
        )

        logger.info(
            "Listing milestones",
            project=project,
            user=effective_user,
        )

        try:
            results = await prismind.search_knowledge(
                query="milestone",
                category="milestone",
                project=project,
                limit=50,
                user=effective_user,
            )
        except Exception as e:
            logger.error("Failed to list milestones", error=str(e))
            return {
                "milestones": [],
                "total": 0,
                "pending": 0,
                "completed": 0,
                "delayed": 0,
                "message": f"Failed to list milestones: {e}",
            }

        milestones: list[dict[str, Any]] = []
        for result in results:
            content = result.get("content", "")
            try:
                data = json.loads(content)
                if data.get("type") == "milestone":
                    milestones.append(data)
            except (json.JSONDecodeError, TypeError):
                continue

        # Sort by target date
        milestones.sort(key=lambda m: m.get("target_date", "9999-12-31"))

        # Count by status
        pending = sum(1 for m in milestones if m.get("status") == "pending")
        in_progress = sum(1 for m in milestones if m.get("status") == "in_progress")
        completed = sum(1 for m in milestones if m.get("status") == "completed")
        delayed = sum(1 for m in milestones if m.get("status") == "delayed")

        return {
            "milestones": milestones,
            "total": len(milestones),
            "pending": pending,
            "in_progress": in_progress,
            "completed": completed,
            "delayed": delayed,
            "message": f"Found {len(milestones)} milestone(s)",
        }

    @mcp.tool()
    async def check_milestone_status(
        project: str,
        name: str = "",
        user: str = "",
    ) -> dict[str, Any]:
        """Check milestone achievement status and detect delays.

        USE THIS WHEN: You need to check if milestones are on track,
        delayed, or at risk.

        Args:
            project: Project identifier.
            name: Specific milestone to check (empty for all).
            user: User identifier for multi-user support.

        Returns:
            Dict containing:
            - milestones: List of milestone status reports
            - at_risk: List of milestones at risk of delay
            - overdue: List of overdue milestones
            - on_track: Number of milestones on track
            - message: Status message
        """
        if _settings is None:
            raise RuntimeError("Settings not initialized")

        effective_user = user or get_current_user()

        prismind = PrismindAdapter(
            sse_url=_settings.prismind_url,
            timeout=_settings.prismind_timeout,
        )

        logger.info(
            "Checking milestone status",
            project=project,
            name=name,
            user=effective_user,
        )

        # Get milestones
        try:
            query = f"milestone {name}" if name else "milestone"
            results = await prismind.search_knowledge(
                query=query,
                category="milestone",
                project=project,
                limit=50,
                user=effective_user,
            )
        except Exception as e:
            logger.error("Failed to get milestones", error=str(e))
            return {
                "milestones": [],
                "at_risk": [],
                "overdue": [],
                "on_track": 0,
                "message": f"Failed to check milestones: {e}",
            }

        today = datetime.now().date()
        milestones_status: list[dict[str, Any]] = []
        at_risk: list[dict[str, Any]] = []
        overdue: list[dict[str, Any]] = []
        on_track = 0

        for result in results:
            content = result.get("content", "")
            try:
                data = json.loads(content)
                if data.get("type") != "milestone":
                    continue

                # Filter by name if specified
                if name and data.get("name", "").lower() != name.lower():
                    continue

                target_str = data.get("target_date", "")
                if not target_str:
                    continue

                target_date = datetime.fromisoformat(target_str).date()
                status = data.get("status", "pending")
                days_until = (target_date - today).days

                milestone_report = {
                    "name": data.get("name", ""),
                    "target_date": target_str,
                    "status": status,
                    "days_until": days_until,
                    "phase": data.get("phase", ""),
                    "is_overdue": days_until < 0 and status not in ["completed"],
                    "is_at_risk": 0 <= days_until <= 7 and status not in ["completed"],
                }

                milestones_status.append(milestone_report)

                if milestone_report["is_overdue"]:
                    overdue.append(milestone_report)
                elif milestone_report["is_at_risk"]:
                    at_risk.append(milestone_report)
                elif status == "completed" or days_until > 7:
                    on_track += 1

            except (json.JSONDecodeError, TypeError, ValueError):
                continue

        # Sort by target date
        milestones_status.sort(key=lambda m: m.get("target_date", ""))

        message_parts = []
        if overdue:
            message_parts.append(f"{len(overdue)} overdue")
        if at_risk:
            message_parts.append(f"{len(at_risk)} at risk")
        if on_track:
            message_parts.append(f"{on_track} on track")

        return {
            "milestones": milestones_status,
            "at_risk": at_risk,
            "overdue": overdue,
            "on_track": on_track,
            "message": ", ".join(message_parts) if message_parts else "No milestones found",
        }
