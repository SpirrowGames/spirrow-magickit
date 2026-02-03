"""Reporting and analysis tools for Magickit MCP server.

Provides tools for generating project reports and analysis:
- Status reports for stakeholders
- Release notes generation
- Project performance analysis (retrospectives)
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from fastmcp import FastMCP

from magickit.adapters.lexora import LexoraAdapter
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
    """Register reporting tools with the MCP server.

    Args:
        mcp: FastMCP server instance.
        settings: Application settings.
    """
    global _settings
    _settings = settings

    @mcp.tool()
    async def generate_status_report(
        project: str,
        format: str = "markdown",
        include_tasks: bool = True,
        include_milestones: bool = True,
        include_risks: bool = True,
        user: str = "",
    ) -> dict[str, Any]:
        """Generate a comprehensive status report for stakeholders.

        USE THIS WHEN: You need to create a status update for team members
        or stakeholders. Generates a formatted report with project health,
        progress, and key metrics.

        Args:
            project: Project identifier.
            format: Output format ("markdown", "text", "json").
            include_tasks: Include task breakdown in report.
            include_milestones: Include milestone status.
            include_risks: Include risk indicators.
            user: User identifier for multi-user support.

        Returns:
            Dict containing:
            - report: Formatted report content
            - format: Report format used
            - generated_at: Timestamp of generation
            - metrics: Key metrics included in the report
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
            "Generating status report",
            project=project,
            format=format,
            user=effective_user,
        )

        # Gather data
        try:
            progress = await prismind.get_progress(project=project, user=effective_user)
        except Exception as e:
            logger.error("Failed to get progress", error=str(e))
            return {
                "report": "",
                "format": format,
                "generated_at": datetime.now().isoformat(),
                "metrics": {},
                "message": f"Failed to get project data: {e}",
            }

        all_tasks = _extract_tasks_from_progress(progress)
        current_phase = progress.get("current_phase", "Unknown")

        # Calculate overall stats
        stats = _calculate_task_stats(all_tasks)
        completion_percent = (stats["completed"] / stats["total"] * 100) if stats["total"] > 0 else 0

        # Get milestones
        milestones: list[dict[str, Any]] = []
        if include_milestones:
            try:
                milestone_results = await prismind.search_knowledge(
                    query="milestone",
                    category="milestone",
                    project=project,
                    limit=10,
                    user=effective_user,
                )
                for result in milestone_results:
                    content = result.get("content", "")
                    try:
                        data = json.loads(content)
                        if data.get("type") == "milestone":
                            milestones.append(data)
                    except (json.JSONDecodeError, TypeError):
                        continue
                milestones.sort(key=lambda m: m.get("target_date", ""))
            except Exception as e:
                logger.warning("Failed to get milestones", error=str(e))

        # Get phase-specific stats
        phases = progress.get("phases", [])
        phase_stats: list[dict[str, Any]] = []
        for phase_data in phases:
            phase_name = phase_data.get("phase", "")
            p_stats = _calculate_task_stats(all_tasks, phase_name)
            p_completion = (p_stats["completed"] / p_stats["total"] * 100) if p_stats["total"] > 0 else 0
            phase_stats.append({
                "phase": phase_name,
                "is_current": phase_name == current_phase,
                **p_stats,
                "completion_percent": round(p_completion, 1),
            })

        # Get risk indicators
        risk_data: dict[str, Any] = {}
        if include_risks:
            blocked_ratio = (stats["blocked"] / stats["total"]) if stats["total"] > 0 else 0
            if blocked_ratio > 0.2:
                risk_level = "high"
            elif blocked_ratio > 0.1:
                risk_level = "medium"
            else:
                risk_level = "low"

            risk_data = {
                "level": risk_level,
                "blocked_tasks": stats["blocked"],
                "blocked_ratio": round(blocked_ratio * 100, 1),
            }

        # Build metrics
        metrics = {
            "total_tasks": stats["total"],
            "completed_tasks": stats["completed"],
            "in_progress_tasks": stats["in_progress"],
            "blocked_tasks": stats["blocked"],
            "completion_percent": round(completion_percent, 1),
            "current_phase": current_phase,
        }

        # Generate report content
        generated_at = datetime.now().isoformat()

        if format == "json":
            report = json.dumps({
                "project": project,
                "generated_at": generated_at,
                "current_phase": current_phase,
                "overall": metrics,
                "phases": phase_stats,
                "milestones": milestones,
                "risks": risk_data,
            }, indent=2)
        elif format == "text":
            lines = [
                f"PROJECT STATUS REPORT: {project}",
                f"Generated: {generated_at}",
                "",
                f"Current Phase: {current_phase}",
                f"Overall Progress: {round(completion_percent, 1)}%",
                f"Tasks: {stats['completed']}/{stats['total']} completed",
                "",
            ]

            if stats["blocked"] > 0:
                lines.append(f"BLOCKED: {stats['blocked']} task(s)")
            if stats["in_progress"] > 0:
                lines.append(f"In Progress: {stats['in_progress']} task(s)")

            if include_milestones and milestones:
                lines.append("")
                lines.append("MILESTONES:")
                for m in milestones:
                    status = m.get("status", "pending")
                    target = m.get("target_date", "TBD")
                    lines.append(f"  - {m.get('name', 'Unknown')}: {status} (target: {target})")

            if include_risks and risk_data:
                lines.append("")
                lines.append(f"RISK LEVEL: {risk_data.get('level', 'unknown').upper()}")

            report = "\n".join(lines)
        else:  # markdown
            lines = [
                f"# Project Status Report: {project}",
                "",
                f"*Generated: {generated_at}*",
                "",
                "## Overview",
                "",
                f"- **Current Phase:** {current_phase}",
                f"- **Overall Progress:** {round(completion_percent, 1)}%",
                f"- **Tasks:** {stats['completed']}/{stats['total']} completed",
                "",
            ]

            if include_tasks and phase_stats:
                lines.append("## Phase Progress")
                lines.append("")
                lines.append("| Phase | Progress | Completed | In Progress | Blocked |")
                lines.append("|-------|----------|-----------|-------------|---------|")
                for ps in phase_stats:
                    current_marker = " *" if ps.get("is_current") else ""
                    lines.append(
                        f"| {ps['phase']}{current_marker} | {ps['completion_percent']}% | "
                        f"{ps['completed']} | {ps['in_progress']} | {ps['blocked']} |"
                    )
                lines.append("")

            if include_milestones and milestones:
                lines.append("## Milestones")
                lines.append("")
                for m in milestones:
                    status = m.get("status", "pending")
                    target = m.get("target_date", "TBD")
                    icon = "✅" if status == "completed" else "⏳" if status == "in_progress" else "📅"
                    lines.append(f"- {icon} **{m.get('name', 'Unknown')}**: {status} (target: {target})")
                lines.append("")

            if include_risks and risk_data:
                risk_icon = "🔴" if risk_data.get("level") == "high" else "🟡" if risk_data.get("level") == "medium" else "🟢"
                lines.append("## Risk Assessment")
                lines.append("")
                lines.append(f"**Risk Level:** {risk_icon} {risk_data.get('level', 'unknown').upper()}")
                if stats["blocked"] > 0:
                    lines.append(f"- {stats['blocked']} blocked task(s) ({risk_data.get('blocked_ratio', 0)}%)")
                lines.append("")

            report = "\n".join(lines)

        return {
            "report": report,
            "format": format,
            "generated_at": generated_at,
            "metrics": metrics,
            "message": f"Status report generated for project '{project}'",
        }

    @mcp.tool()
    async def generate_release_notes(
        project: str,
        from_phase: str = "",
        version: str = "",
        include_tasks: bool = True,
        user: str = "",
    ) -> dict[str, Any]:
        """Generate release notes from completed tasks.

        USE THIS WHEN: Preparing release notes for a version or phase completion.
        Uses completed tasks and their categories to generate structured notes.

        Args:
            project: Project identifier.
            from_phase: Generate notes from this phase onward (empty for all).
            version: Version label for the release (e.g., "v1.0.0").
            include_tasks: Include individual task details.
            user: User identifier for multi-user support.

        Returns:
            Dict containing:
            - release_notes: Generated release notes in markdown
            - version: Version label
            - features_count: Number of features
            - fixes_count: Number of bug fixes
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
            "Generating release notes",
            project=project,
            from_phase=from_phase,
            version=version,
            user=effective_user,
        )

        # Get progress
        try:
            progress = await prismind.get_progress(project=project, user=effective_user)
        except Exception as e:
            logger.error("Failed to get progress", error=str(e))
            return {
                "release_notes": "",
                "version": version,
                "features_count": 0,
                "fixes_count": 0,
                "message": f"Failed to get project data: {e}",
            }

        all_tasks = _extract_tasks_from_progress(progress)

        # Filter by phase if specified
        if from_phase:
            phases = [p.get("phase", "") for p in progress.get("phases", [])]
            if from_phase in phases:
                start_index = phases.index(from_phase)
                included_phases = phases[start_index:]
                all_tasks = [t for t in all_tasks if t.get("phase") in included_phases]

        # Filter to completed tasks only
        completed_tasks = [t for t in all_tasks if t.get("status") == "completed"]

        # Categorize tasks
        features: list[dict[str, Any]] = []
        bug_fixes: list[dict[str, Any]] = []
        improvements: list[dict[str, Any]] = []
        other: list[dict[str, Any]] = []

        for task in completed_tasks:
            category = task.get("category", "").lower()
            if category in ["feature", "implementation"]:
                features.append(task)
            elif category == "bug":
                bug_fixes.append(task)
            elif category in ["refactor", "improvement", "polish"]:
                improvements.append(task)
            else:
                other.append(task)

        # Generate release notes
        version_str = version or datetime.now().strftime("%Y.%m.%d")
        lines = [
            f"# Release Notes - {version_str}",
            "",
            f"*Project: {project}*",
            f"*Date: {datetime.now().strftime('%Y-%m-%d')}*",
            "",
        ]

        if features:
            lines.append("## New Features")
            lines.append("")
            for task in features:
                name = task.get("name", "Unnamed")
                lines.append(f"- **{name}**")
                if include_tasks and task.get("notes"):
                    lines.append(f"  - {task.get('notes')}")
            lines.append("")

        if bug_fixes:
            lines.append("## Bug Fixes")
            lines.append("")
            for task in bug_fixes:
                name = task.get("name", "Unnamed")
                lines.append(f"- {name}")
            lines.append("")

        if improvements:
            lines.append("## Improvements")
            lines.append("")
            for task in improvements:
                name = task.get("name", "Unnamed")
                lines.append(f"- {name}")
            lines.append("")

        if other and include_tasks:
            lines.append("## Other Changes")
            lines.append("")
            for task in other:
                name = task.get("name", "Unnamed")
                lines.append(f"- {name}")
            lines.append("")

        # Summary
        lines.append("---")
        lines.append("")
        lines.append(f"*{len(completed_tasks)} task(s) completed in this release.*")

        release_notes = "\n".join(lines)

        return {
            "release_notes": release_notes,
            "version": version_str,
            "features_count": len(features),
            "fixes_count": len(bug_fixes),
            "improvements_count": len(improvements),
            "total_completed": len(completed_tasks),
            "message": f"Release notes generated with {len(features)} features, {len(bug_fixes)} fixes",
        }

    @mcp.tool()
    async def analyze_project_performance(
        project: str,
        use_llm: bool = True,
        user: str = "",
    ) -> dict[str, Any]:
        """Analyze project performance and generate retrospective insights.

        USE THIS WHEN: Running a project retrospective or post-mortem analysis.
        Analyzes velocity trends, blockers, and phase transitions to generate
        actionable insights.

        Args:
            project: Project identifier.
            use_llm: Use LLM to generate narrative insights.
            user: User identifier for multi-user support.

        Returns:
            Dict containing:
            - insights: List of key insights
            - metrics: Performance metrics
            - recommendations: Suggested improvements
            - narrative: LLM-generated narrative analysis (if use_llm=True)
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
            "Analyzing project performance",
            project=project,
            use_llm=use_llm,
            user=effective_user,
        )

        insights: list[str] = []
        recommendations: list[str] = []

        # Get progress
        try:
            progress = await prismind.get_progress(project=project, user=effective_user)
        except Exception as e:
            logger.error("Failed to get progress", error=str(e))
            return {
                "insights": [],
                "metrics": {},
                "recommendations": [],
                "narrative": "",
                "message": f"Failed to get project data: {e}",
            }

        all_tasks = _extract_tasks_from_progress(progress)
        stats = _calculate_task_stats(all_tasks)

        # Analyze velocity
        try:
            velocity_results = await prismind.search_knowledge(
                query="velocity tracking",
                category="velocity",
                project=project,
                limit=30,
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

            if velocity_data:
                velocity_data.sort(key=lambda v: v.get("date", ""))
                total_completed_velocity = sum(v.get("completed_today", 0) for v in velocity_data)
                avg_velocity = total_completed_velocity / len(velocity_data) if velocity_data else 0

                # Analyze trend
                if len(velocity_data) >= 7:
                    first_half = velocity_data[:len(velocity_data)//2]
                    second_half = velocity_data[len(velocity_data)//2:]

                    first_avg = sum(v.get("completed_today", 0) for v in first_half) / len(first_half)
                    second_avg = sum(v.get("completed_today", 0) for v in second_half) / len(second_half)

                    if second_avg > first_avg * 1.2:
                        insights.append("Velocity improved significantly over time")
                    elif second_avg < first_avg * 0.8:
                        insights.append("Velocity declined over the project")
                        recommendations.append("Investigate factors causing velocity decline")
                    else:
                        insights.append("Velocity remained relatively stable")
        except Exception as e:
            logger.warning("Failed to analyze velocity", error=str(e))
            avg_velocity = 0

        # Analyze blockers
        try:
            blocker_results = await prismind.search_knowledge(
                query="blocked blocker",
                category="blocker",
                project=project,
                limit=20,
                user=effective_user,
            )

            if blocker_results:
                insights.append(f"{len(blocker_results)} blockers were recorded during the project")
                if len(blocker_results) > 5:
                    recommendations.append("Consider implementing better blocker prevention")
        except Exception as e:
            logger.warning("Failed to analyze blockers", error=str(e))

        # Analyze phase transitions
        try:
            transition_results = await prismind.search_knowledge(
                query="phase transition",
                category="phase_transition",
                project=project,
                limit=10,
                user=effective_user,
            )

            phase_count = len(transition_results)
            if phase_count > 0:
                insights.append(f"{phase_count} phase transition(s) occurred")
        except Exception as e:
            logger.warning("Failed to analyze phase transitions", error=str(e))

        # Analyze task categories
        category_breakdown: dict[str, int] = {}
        for task in all_tasks:
            category = task.get("category", "uncategorized")
            category_breakdown[category] = category_breakdown.get(category, 0) + 1

        if category_breakdown:
            top_category = max(category_breakdown, key=category_breakdown.get)  # type: ignore
            insights.append(f"Most common task category: {top_category} ({category_breakdown[top_category]} tasks)")

        # Calculate metrics
        completion_rate = (stats["completed"] / stats["total"] * 100) if stats["total"] > 0 else 0
        blocked_rate = (stats["blocked"] / stats["total"] * 100) if stats["total"] > 0 else 0

        metrics = {
            "total_tasks": stats["total"],
            "completed_tasks": stats["completed"],
            "completion_rate": round(completion_rate, 1),
            "blocked_rate": round(blocked_rate, 1),
            "average_velocity": round(avg_velocity, 2) if avg_velocity else 0,
            "category_breakdown": category_breakdown,
        }

        # Generate recommendations based on analysis
        if blocked_rate > 20:
            recommendations.append("High blocker rate suggests dependency management issues")

        if stats["not_started"] > stats["completed"]:
            recommendations.append("Many tasks never started - review task prioritization")

        if completion_rate >= 90:
            insights.append("Excellent completion rate achieved")
        elif completion_rate >= 70:
            insights.append("Good completion rate, but room for improvement")
        else:
            recommendations.append("Improve task completion through better planning")

        # Generate narrative using LLM if requested
        narrative = ""
        if use_llm and insights:
            try:
                lexora = LexoraAdapter(
                    base_url=_settings.lexora_url,
                    timeout=_settings.lexora_timeout,
                )

                prompt = f"""Analyze this project performance data and write a brief retrospective summary:

Project: {project}
Completion Rate: {completion_rate}%
Total Tasks: {stats['total']}, Completed: {stats['completed']}, Blocked: {stats['blocked']}
Average Velocity: {round(avg_velocity, 2)} tasks/day
Category Breakdown: {category_breakdown}

Key Insights:
{chr(10).join('- ' + i for i in insights)}

Write 2-3 paragraphs summarizing the project's performance and key learnings.
Focus on actionable insights for future projects."""

                narrative = await lexora.generate(
                    prompt=prompt,
                    max_tokens=500,
                    temperature=0.7,
                )
            except Exception as e:
                logger.warning("Failed to generate narrative", error=str(e))
                narrative = "LLM narrative generation unavailable."

        return {
            "insights": insights,
            "metrics": metrics,
            "recommendations": recommendations,
            "narrative": narrative,
            "message": f"Analysis complete: {len(insights)} insights, {len(recommendations)} recommendations",
        }
