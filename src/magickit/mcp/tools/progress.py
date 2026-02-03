"""Progress tracking and prediction tools for Magickit MCP server.

Provides tools for tracking project progress and predicting completion:
- Burndown chart data
- Completion predictions based on velocity
- Velocity tracking
- Risk indicators
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from fastmcp import FastMCP

from magickit.adapters.prismind import PrismindAdapter
from magickit.config import Settings
from magickit.utils.logging import get_logger
from magickit.utils.user import get_current_user

logger = get_logger(__name__)

# Module-level settings reference
_settings: Settings | None = None


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
                for key in ["results", "items", "documents", "knowledge", "entries"]:
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


def _calculate_task_stats(tasks: list[dict[str, Any]], phase: str = "") -> dict[str, int]:
    """Calculate task statistics, optionally for a specific phase."""
    if phase:
        tasks = [t for t in tasks if t.get("phase") == phase]

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


def register_tools(mcp: FastMCP, settings: Settings) -> None:
    """Register progress tracking tools with the MCP server.

    Args:
        mcp: FastMCP server instance.
        settings: Application settings.
    """
    global _settings
    _settings = settings

    @mcp.tool()
    async def get_burndown(
        project: str,
        phase: str = "",
        days: int = 14,
        user: str = "",
    ) -> dict[str, Any]:
        """Get burndown chart data for the project.

        USE THIS WHEN: You need to visualize project progress over time.
        This tool calculates historical and projected burndown data.

        Args:
            project: Project identifier.
            phase: Specific phase to analyze (empty for all).
            days: Number of days to include in the chart.
            user: User identifier for multi-user support.

        Returns:
            Dict containing:
            - data_points: List of daily data points with date and remaining tasks
            - total_tasks: Total number of tasks
            - completed_tasks: Number of completed tasks
            - remaining_tasks: Number of remaining tasks
            - ideal_burndown: Ideal burndown line data
            - current_velocity: Tasks completed per day (average)
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
            "Getting burndown data",
            project=project,
            phase=phase,
            days=days,
            user=effective_user,
        )

        # Get current progress
        try:
            progress = await prismind.get_progress(project=project, user=effective_user)
        except Exception as e:
            logger.error("Failed to get progress", error=str(e))
            return {
                "data_points": [],
                "total_tasks": 0,
                "completed_tasks": 0,
                "remaining_tasks": 0,
                "ideal_burndown": [],
                "current_velocity": 0.0,
                "message": f"Failed to get progress: {e}",
            }

        all_tasks = _extract_tasks_from_progress(progress)
        if phase:
            all_tasks = [t for t in all_tasks if t.get("phase") == phase]

        total_tasks = len(all_tasks)
        completed_tasks = sum(1 for t in all_tasks if t.get("status") == "completed")
        remaining_tasks = total_tasks - completed_tasks

        # Get velocity data from knowledge
        try:
            velocity_results = await prismind.search_knowledge(
                query="velocity tracking daily",
                category="velocity",
                project=project,
                limit=days,
                user=effective_user,
            )
        except Exception as e:
            logger.warning("Failed to get velocity data", error=str(e))
            velocity_results = []

        # Build data points from velocity records
        data_points: list[dict[str, Any]] = []
        velocity_records: list[dict[str, Any]] = []

        for result in velocity_results:
            content = result.get("content", "")
            try:
                data = json.loads(content)
                if data.get("type") == "velocity_record":
                    velocity_records.append(data)
            except (json.JSONDecodeError, TypeError):
                continue

        # Sort velocity records by date
        velocity_records.sort(key=lambda v: v.get("date", ""), reverse=True)

        # If we have historical data, use it
        if velocity_records:
            for record in velocity_records[:days]:
                data_points.append({
                    "date": record.get("date", ""),
                    "remaining": record.get("remaining_tasks", remaining_tasks),
                    "completed_today": record.get("completed_today", 0),
                })
        else:
            # Generate synthetic data point for today only
            today = datetime.now().date().isoformat()
            data_points.append({
                "date": today,
                "remaining": remaining_tasks,
                "completed_today": 0,
            })

        # Calculate velocity (average tasks per day)
        if velocity_records:
            total_completed = sum(v.get("completed_today", 0) for v in velocity_records)
            current_velocity = total_completed / len(velocity_records) if velocity_records else 0.0
        else:
            current_velocity = 0.0

        # Generate ideal burndown line
        ideal_burndown: list[dict[str, Any]] = []
        if total_tasks > 0 and days > 0:
            daily_ideal = total_tasks / days
            for i in range(days + 1):
                date = (datetime.now().date() + timedelta(days=i)).isoformat()
                ideal_burndown.append({
                    "date": date,
                    "remaining": max(0, total_tasks - (daily_ideal * i)),
                })

        return {
            "data_points": data_points,
            "total_tasks": total_tasks,
            "completed_tasks": completed_tasks,
            "remaining_tasks": remaining_tasks,
            "ideal_burndown": ideal_burndown,
            "current_velocity": round(current_velocity, 2),
            "message": f"{completed_tasks}/{total_tasks} tasks completed, {remaining_tasks} remaining",
        }

    @mcp.tool()
    async def estimate_completion(
        project: str,
        phase: str = "",
        user: str = "",
    ) -> dict[str, Any]:
        """Estimate project completion date based on velocity.

        USE THIS WHEN: You need to predict when the project or phase will be complete.
        Uses historical velocity to project completion.

        Args:
            project: Project identifier.
            phase: Specific phase to estimate (empty for all).
            user: User identifier for multi-user support.

        Returns:
            Dict containing:
            - estimated_date: Predicted completion date
            - days_remaining: Estimated days to completion
            - remaining_tasks: Number of tasks remaining
            - current_velocity: Current velocity (tasks/day)
            - confidence: Confidence level (low/medium/high)
            - factors: Factors affecting the estimate
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
            "Estimating completion",
            project=project,
            phase=phase,
            user=effective_user,
        )

        # Get current progress
        try:
            progress = await prismind.get_progress(project=project, user=effective_user)
        except Exception as e:
            logger.error("Failed to get progress", error=str(e))
            return {
                "estimated_date": "",
                "days_remaining": -1,
                "remaining_tasks": 0,
                "current_velocity": 0.0,
                "confidence": "low",
                "factors": [],
                "message": f"Failed to get progress: {e}",
            }

        all_tasks = _extract_tasks_from_progress(progress)
        if phase:
            all_tasks = [t for t in all_tasks if t.get("phase") == phase]

        total_tasks = len(all_tasks)
        completed_tasks = sum(1 for t in all_tasks if t.get("status") == "completed")
        remaining_tasks = total_tasks - completed_tasks
        blocked_tasks = sum(1 for t in all_tasks if t.get("status") == "blocked")

        if remaining_tasks == 0:
            return {
                "estimated_date": datetime.now().date().isoformat(),
                "days_remaining": 0,
                "remaining_tasks": 0,
                "current_velocity": 0.0,
                "confidence": "high",
                "factors": ["All tasks completed"],
                "message": "Project/phase is already complete",
            }

        # Get velocity data
        try:
            velocity_results = await prismind.search_knowledge(
                query="velocity tracking",
                category="velocity",
                project=project,
                limit=30,
                user=effective_user,
            )
        except Exception as e:
            logger.warning("Failed to get velocity data", error=str(e))
            velocity_results = []

        velocity_records: list[dict[str, Any]] = []
        for result in velocity_results:
            content = result.get("content", "")
            try:
                data = json.loads(content)
                if data.get("type") == "velocity_record":
                    velocity_records.append(data)
            except (json.JSONDecodeError, TypeError):
                continue

        factors: list[str] = []
        confidence = "medium"

        # Calculate velocity
        if velocity_records:
            # Use recent 7 days for more accurate velocity
            recent_records = sorted(
                velocity_records,
                key=lambda v: v.get("date", ""),
                reverse=True,
            )[:7]

            total_completed = sum(v.get("completed_today", 0) for v in recent_records)
            current_velocity = total_completed / len(recent_records) if recent_records else 0.0

            if len(recent_records) >= 7:
                confidence = "high"
                factors.append("Based on 7+ days of data")
            elif len(recent_records) >= 3:
                confidence = "medium"
                factors.append(f"Based on {len(recent_records)} days of data")
            else:
                confidence = "low"
                factors.append("Limited historical data")
        else:
            # Estimate velocity from completion ratio and time
            current_velocity = 0.5  # Default assumption: 0.5 tasks per day
            confidence = "low"
            factors.append("No historical velocity data, using default estimate")

        # Account for blocked tasks
        if blocked_tasks > 0:
            factors.append(f"{blocked_tasks} blocked task(s) may affect timeline")
            confidence = "low" if confidence == "high" else confidence

        # Calculate estimated completion
        if current_velocity > 0:
            days_remaining = remaining_tasks / current_velocity
            estimated_date = (datetime.now().date() + timedelta(days=int(days_remaining))).isoformat()
        else:
            days_remaining = -1
            estimated_date = ""
            factors.append("Velocity is zero - cannot estimate")
            confidence = "low"

        return {
            "estimated_date": estimated_date,
            "days_remaining": round(days_remaining, 1) if days_remaining >= 0 else -1,
            "remaining_tasks": remaining_tasks,
            "current_velocity": round(current_velocity, 2),
            "confidence": confidence,
            "factors": factors,
            "message": (
                f"Estimated completion: {estimated_date} ({round(days_remaining)} days)"
                if estimated_date
                else "Cannot estimate completion date"
            ),
        }

    @mcp.tool()
    async def track_velocity(
        project: str,
        completed_today: int = 0,
        notes: str = "",
        user: str = "",
    ) -> dict[str, Any]:
        """Track daily velocity for the project.

        USE THIS WHEN: Recording daily task completion for velocity tracking.
        Call this at the end of each work day to build accurate velocity data.

        Args:
            project: Project identifier.
            completed_today: Number of tasks completed today.
            notes: Optional notes about today's work.
            user: User identifier for multi-user support.

        Returns:
            Dict containing:
            - success: Whether the record was saved
            - date: Date of the record
            - completed_today: Tasks completed
            - rolling_average: 7-day rolling average velocity
            - message: Status message
        """
        if _settings is None:
            raise RuntimeError("Settings not initialized")

        effective_user = user or get_current_user()

        prismind = PrismindAdapter(
            sse_url=_settings.prismind_url,
            timeout=_settings.prismind_timeout,
        )

        today = datetime.now().date().isoformat()

        logger.info(
            "Tracking velocity",
            project=project,
            completed_today=completed_today,
            date=today,
            user=effective_user,
        )

        # Get current task stats
        try:
            progress = await prismind.get_progress(project=project, user=effective_user)
            all_tasks = _extract_tasks_from_progress(progress)
            remaining_tasks = sum(1 for t in all_tasks if t.get("status") != "completed")
            total_tasks = len(all_tasks)
        except Exception as e:
            logger.warning("Failed to get task stats", error=str(e))
            remaining_tasks = 0
            total_tasks = 0

        # Create velocity record
        velocity_record = json.dumps({
            "type": "velocity_record",
            "date": today,
            "completed_today": completed_today,
            "remaining_tasks": remaining_tasks,
            "total_tasks": total_tasks,
            "notes": notes,
            "recorded_at": datetime.now().isoformat(),
        })

        try:
            await prismind.add_knowledge(
                content=velocity_record,
                category="velocity",
                project=project,
                tags=["velocity", "daily", today],
                source=f"velocity:{today}",
                user=effective_user,
            )
        except Exception as e:
            logger.error("Failed to save velocity record", error=str(e))
            return {
                "success": False,
                "date": today,
                "completed_today": completed_today,
                "rolling_average": 0.0,
                "message": f"Failed to save velocity record: {e}",
            }

        # Calculate rolling average
        try:
            velocity_results = await prismind.search_knowledge(
                query="velocity tracking",
                category="velocity",
                project=project,
                limit=7,
                user=effective_user,
            )

            recent_completed = [completed_today]
            for result in velocity_results:
                content = result.get("content", "")
                try:
                    data = json.loads(content)
                    if data.get("type") == "velocity_record" and data.get("date") != today:
                        recent_completed.append(data.get("completed_today", 0))
                except (json.JSONDecodeError, TypeError):
                    continue

            rolling_average = sum(recent_completed) / len(recent_completed) if recent_completed else 0.0
        except Exception as e:
            logger.warning("Failed to calculate rolling average", error=str(e))
            rolling_average = float(completed_today)

        logger.info(
            "Velocity tracked",
            project=project,
            completed_today=completed_today,
            rolling_average=rolling_average,
        )

        return {
            "success": True,
            "date": today,
            "completed_today": completed_today,
            "rolling_average": round(rolling_average, 2),
            "remaining_tasks": remaining_tasks,
            "message": f"Recorded {completed_today} tasks completed. Rolling avg: {round(rolling_average, 2)}/day",
        }

    @mcp.tool()
    async def get_risk_indicators(
        project: str,
        phase: str = "",
        user: str = "",
    ) -> dict[str, Any]:
        """Get risk indicators for the project.

        USE THIS WHEN: You need to assess project health and identify potential issues.

        Risk indicators include:
        - Blocked task ratio
        - Velocity trend
        - Milestone delays
        - Phase completion health

        Args:
            project: Project identifier.
            phase: Specific phase to analyze (empty for all).
            user: User identifier for multi-user support.

        Returns:
            Dict containing:
            - overall_risk: Risk level (low/medium/high/critical)
            - risk_score: Numerical risk score (0-100)
            - indicators: List of risk indicators with details
            - recommendations: List of recommended actions
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
            "Getting risk indicators",
            project=project,
            phase=phase,
            user=effective_user,
        )

        indicators: list[dict[str, Any]] = []
        recommendations: list[str] = []
        risk_score = 0

        # Get current progress
        try:
            progress = await prismind.get_progress(project=project, user=effective_user)
            all_tasks = _extract_tasks_from_progress(progress)
            if phase:
                all_tasks = [t for t in all_tasks if t.get("phase") == phase]
        except Exception as e:
            logger.error("Failed to get progress", error=str(e))
            return {
                "overall_risk": "unknown",
                "risk_score": 0,
                "indicators": [],
                "recommendations": [],
                "message": f"Failed to get progress: {e}",
            }

        total_tasks = len(all_tasks)
        if total_tasks == 0:
            return {
                "overall_risk": "low",
                "risk_score": 0,
                "indicators": [{"type": "info", "message": "No tasks found", "severity": "info"}],
                "recommendations": ["Add tasks to the project"],
                "message": "No tasks to analyze",
            }

        # Calculate task stats
        stats = _calculate_task_stats(all_tasks, phase)

        # Risk indicator 1: Blocked task ratio
        blocked_ratio = stats["blocked"] / total_tasks if total_tasks > 0 else 0
        if blocked_ratio > 0.3:
            risk_score += 30
            indicators.append({
                "type": "blocked_tasks",
                "severity": "high",
                "value": f"{stats['blocked']}/{total_tasks}",
                "message": f"High blocked task ratio: {round(blocked_ratio * 100)}%",
            })
            recommendations.append("Review and resolve blocked tasks immediately")
        elif blocked_ratio > 0.1:
            risk_score += 15
            indicators.append({
                "type": "blocked_tasks",
                "severity": "medium",
                "value": f"{stats['blocked']}/{total_tasks}",
                "message": f"Moderate blocked task ratio: {round(blocked_ratio * 100)}%",
            })
            recommendations.append("Address blocked tasks before they become critical")
        else:
            indicators.append({
                "type": "blocked_tasks",
                "severity": "low",
                "value": f"{stats['blocked']}/{total_tasks}",
                "message": f"Blocked task ratio is healthy: {round(blocked_ratio * 100)}%",
            })

        # Risk indicator 2: Completion progress
        completion_ratio = stats["completed"] / total_tasks if total_tasks > 0 else 0
        not_started_ratio = stats["not_started"] / total_tasks if total_tasks > 0 else 0

        if not_started_ratio > 0.7 and stats["in_progress"] == 0:
            risk_score += 20
            indicators.append({
                "type": "stalled_progress",
                "severity": "high",
                "value": f"{round(not_started_ratio * 100)}%",
                "message": "Most tasks haven't started and none in progress",
            })
            recommendations.append("Start work on priority tasks")
        elif not_started_ratio > 0.5:
            risk_score += 10
            indicators.append({
                "type": "slow_start",
                "severity": "medium",
                "value": f"{round(not_started_ratio * 100)}%",
                "message": f"{round(not_started_ratio * 100)}% of tasks haven't started",
            })

        # Risk indicator 3: Velocity trend
        try:
            velocity_results = await prismind.search_knowledge(
                query="velocity tracking",
                category="velocity",
                project=project,
                limit=14,
                user=effective_user,
            )

            velocity_data: list[dict[str, Any]] = []
            for result in velocity_results:
                content = result.get("content", "")
                try:
                    data = json.loads(content)
                    if data.get("type") == "velocity_record":
                        velocity_data.append(data)
                except (json.JSONDecodeError, TypeError):
                    continue

            if len(velocity_data) >= 7:
                # Sort by date
                velocity_data.sort(key=lambda v: v.get("date", ""))

                # Compare recent week to previous week
                recent = velocity_data[-7:] if len(velocity_data) >= 7 else velocity_data
                previous = velocity_data[-14:-7] if len(velocity_data) >= 14 else []

                recent_avg = sum(v.get("completed_today", 0) for v in recent) / len(recent) if recent else 0
                previous_avg = sum(v.get("completed_today", 0) for v in previous) / len(previous) if previous else recent_avg

                if previous_avg > 0 and recent_avg < previous_avg * 0.5:
                    risk_score += 25
                    indicators.append({
                        "type": "velocity_decline",
                        "severity": "high",
                        "value": f"{round(recent_avg, 1)} vs {round(previous_avg, 1)}",
                        "message": "Velocity has dropped significantly",
                    })
                    recommendations.append("Investigate causes of velocity decline")
                elif previous_avg > 0 and recent_avg < previous_avg * 0.8:
                    risk_score += 10
                    indicators.append({
                        "type": "velocity_decline",
                        "severity": "medium",
                        "value": f"{round(recent_avg, 1)} vs {round(previous_avg, 1)}",
                        "message": "Velocity has decreased",
                    })
                else:
                    indicators.append({
                        "type": "velocity_stable",
                        "severity": "low",
                        "value": f"{round(recent_avg, 1)}/day",
                        "message": "Velocity is stable",
                    })
            else:
                indicators.append({
                    "type": "insufficient_data",
                    "severity": "info",
                    "value": f"{len(velocity_data)} days",
                    "message": "Not enough velocity data for trend analysis",
                })
                recommendations.append("Track velocity daily for better insights")

        except Exception as e:
            logger.warning("Failed to analyze velocity", error=str(e))

        # Risk indicator 4: Milestone delays
        try:
            milestone_results = await prismind.search_knowledge(
                query="milestone",
                category="milestone",
                project=project,
                limit=10,
                user=effective_user,
            )

            today = datetime.now().date()
            overdue_milestones = 0
            at_risk_milestones = 0

            for result in milestone_results:
                content = result.get("content", "")
                try:
                    data = json.loads(content)
                    if data.get("type") == "milestone" and data.get("status") != "completed":
                        target_str = data.get("target_date", "")
                        if target_str:
                            target_date = datetime.fromisoformat(target_str).date()
                            days_until = (target_date - today).days
                            if days_until < 0:
                                overdue_milestones += 1
                            elif days_until <= 7:
                                at_risk_milestones += 1
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue

            if overdue_milestones > 0:
                risk_score += 25
                indicators.append({
                    "type": "overdue_milestones",
                    "severity": "critical",
                    "value": str(overdue_milestones),
                    "message": f"{overdue_milestones} milestone(s) are overdue",
                })
                recommendations.append("Address overdue milestones immediately")
            elif at_risk_milestones > 0:
                risk_score += 15
                indicators.append({
                    "type": "at_risk_milestones",
                    "severity": "high",
                    "value": str(at_risk_milestones),
                    "message": f"{at_risk_milestones} milestone(s) at risk (due within 7 days)",
                })
                recommendations.append("Focus on upcoming milestone deliverables")

        except Exception as e:
            logger.warning("Failed to check milestones", error=str(e))

        # Determine overall risk level
        if risk_score >= 60:
            overall_risk = "critical"
        elif risk_score >= 40:
            overall_risk = "high"
        elif risk_score >= 20:
            overall_risk = "medium"
        else:
            overall_risk = "low"

        # Add positive indicators if risk is low
        if not recommendations:
            recommendations.append("Continue current progress")
            recommendations.append("Monitor velocity and milestone dates")

        return {
            "overall_risk": overall_risk,
            "risk_score": min(100, risk_score),
            "indicators": indicators,
            "recommendations": recommendations,
            "task_stats": stats,
            "message": f"Risk level: {overall_risk} (score: {min(100, risk_score)})",
        }
