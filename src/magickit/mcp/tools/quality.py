"""Quality gate management tools for Magickit MCP server.

Provides tools for defining and checking phase completion conditions:
- Define quality gates with specific criteria
- Check if conditions are met for phase advancement
- List all defined quality gates
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

# Default quality gate templates
DEFAULT_QUALITY_GATES = {
    "pre-production": {
        "name": "Pre-production Gate",
        "criteria": [
            {"type": "task_completion", "threshold": 80, "description": "80% of design tasks completed"},
            {"type": "no_critical_blockers", "description": "No critical blockers"},
        ],
    },
    "production": {
        "name": "Production Gate",
        "criteria": [
            {"type": "task_completion", "threshold": 90, "description": "90% of implementation tasks completed"},
            {"type": "no_blockers", "description": "No blocked tasks"},
        ],
    },
    "polish": {
        "name": "Polish Gate",
        "criteria": [
            {"type": "task_completion", "threshold": 95, "description": "95% of polish tasks completed"},
            {"type": "no_blockers", "description": "No blocked tasks"},
            {"type": "all_bugs_resolved", "description": "All known bugs resolved"},
        ],
    },
    "release": {
        "name": "Release Gate",
        "criteria": [
            {"type": "task_completion", "threshold": 100, "description": "All tasks completed"},
            {"type": "no_blockers", "description": "No blocked tasks"},
            {"type": "milestone_achieved", "milestone": "Release", "description": "Release milestone achieved"},
        ],
    },
}


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
                for key in ["results", "items", "documents", "knowledge", "gates"]:
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


def register_tools(mcp: FastMCP, settings: Settings) -> None:
    """Register quality gate management tools with the MCP server.

    Args:
        mcp: FastMCP server instance.
        settings: Application settings.
    """
    global _settings
    _settings = settings

    @mcp.tool()
    async def define_quality_gate(
        project: str,
        phase: str,
        criteria: list[dict[str, Any]],
        name: str = "",
        description: str = "",
        user: str = "",
    ) -> dict[str, Any]:
        """Define a quality gate for a project phase.

        USE THIS WHEN: Setting up phase completion requirements.

        Available criteria types:
        - task_completion: {"type": "task_completion", "threshold": 80}
        - no_blockers: {"type": "no_blockers"}
        - no_critical_blockers: {"type": "no_critical_blockers"}
        - all_bugs_resolved: {"type": "all_bugs_resolved"}
        - milestone_achieved: {"type": "milestone_achieved", "milestone": "Alpha"}
        - custom: {"type": "custom", "description": "Manual check required"}

        Args:
            project: Project identifier.
            phase: Phase this gate applies to.
            criteria: List of criteria objects defining the gate requirements.
            name: Optional gate name (defaults to "Phase Gate").
            description: Optional description of the gate.
            user: User identifier for multi-user support.

        Returns:
            Dict containing:
            - success: Whether the gate was defined
            - gate_id: ID of the created gate
            - phase: Phase the gate applies to
            - criteria_count: Number of criteria
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
            "Defining quality gate",
            project=project,
            phase=phase,
            criteria_count=len(criteria),
            user=effective_user,
        )

        # Validate criteria
        valid_types = [
            "task_completion",
            "no_blockers",
            "no_critical_blockers",
            "all_bugs_resolved",
            "milestone_achieved",
            "custom",
        ]

        for criterion in criteria:
            if criterion.get("type") not in valid_types:
                return {
                    "success": False,
                    "gate_id": "",
                    "phase": phase,
                    "criteria_count": 0,
                    "message": f"Invalid criterion type: {criterion.get('type')}. Valid types: {valid_types}",
                }

        gate_id = f"quality_gate:{phase.lower().replace(' ', '_')}"
        gate_name = name or f"{phase} Gate"

        gate_data = json.dumps({
            "type": "quality_gate",
            "gate_id": gate_id,
            "name": gate_name,
            "phase": phase,
            "description": description,
            "criteria": criteria,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
        })

        try:
            await prismind.add_knowledge(
                content=gate_data,
                category="quality_gate",
                project=project,
                tags=["quality_gate", phase.lower(), "lifecycle"],
                source=gate_id,
                user=effective_user,
            )
        except Exception as e:
            logger.error("Failed to define quality gate", error=str(e))
            return {
                "success": False,
                "gate_id": "",
                "phase": phase,
                "criteria_count": 0,
                "message": f"Failed to define quality gate: {e}",
            }

        logger.info(
            "Quality gate defined",
            project=project,
            phase=phase,
            gate_id=gate_id,
        )

        return {
            "success": True,
            "gate_id": gate_id,
            "name": gate_name,
            "phase": phase,
            "criteria_count": len(criteria),
            "message": f"Quality gate '{gate_name}' defined for phase '{phase}' with {len(criteria)} criteria",
        }

    @mcp.tool()
    async def check_quality_gate(
        project: str,
        phase: str = "",
        user: str = "",
    ) -> dict[str, Any]:
        """Check if quality gate conditions are met for a phase.

        USE THIS WHEN: You need to verify if a phase is ready for advancement.
        This tool evaluates all defined criteria and reports which pass/fail.

        Args:
            project: Project identifier.
            phase: Phase to check (empty for current phase).
            user: User identifier for multi-user support.

        Returns:
            Dict containing:
            - passed: Whether all criteria are met
            - phase: Phase being checked
            - results: List of criterion results with pass/fail status
            - passed_count: Number of criteria that passed
            - failed_count: Number of criteria that failed
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
            "Checking quality gate",
            project=project,
            phase=phase,
            user=effective_user,
        )

        # Get current progress
        try:
            progress = await prismind.get_progress(project=project, user=effective_user)
            current_phase = progress.get("current_phase", "")
            target_phase = phase or current_phase
        except Exception as e:
            logger.error("Failed to get progress", error=str(e))
            return {
                "passed": False,
                "phase": phase,
                "results": [],
                "passed_count": 0,
                "failed_count": 0,
                "message": f"Failed to get project progress: {e}",
            }

        # Get quality gate for the phase
        try:
            gate_results = await prismind.search_knowledge(
                query=f"quality_gate {target_phase}",
                category="quality_gate",
                project=project,
                limit=5,
                user=effective_user,
            )
        except Exception as e:
            logger.warning("Failed to search quality gates", error=str(e))
            gate_results = []

        # Find matching gate
        gate_data = None
        for result in gate_results:
            content = result.get("content", "")
            try:
                data = json.loads(content)
                if data.get("type") == "quality_gate" and data.get("phase", "").lower() == target_phase.lower():
                    gate_data = data
                    break
            except (json.JSONDecodeError, TypeError):
                continue

        # Use default gate if none defined
        if not gate_data:
            default = DEFAULT_QUALITY_GATES.get(target_phase.lower(), {})
            if default:
                gate_data = {
                    "name": default.get("name", f"{target_phase} Gate"),
                    "phase": target_phase,
                    "criteria": default.get("criteria", []),
                }
            else:
                # Generic default
                gate_data = {
                    "name": f"{target_phase} Gate",
                    "phase": target_phase,
                    "criteria": [
                        {"type": "task_completion", "threshold": 80, "description": "80% tasks completed"},
                    ],
                }

        # Get tasks for evaluation
        all_tasks = _extract_tasks_from_progress(progress)
        phase_tasks = [t for t in all_tasks if t.get("phase", "").lower() == target_phase.lower()]

        total_tasks = len(phase_tasks)
        completed_tasks = sum(1 for t in phase_tasks if t.get("status") == "completed")
        blocked_tasks = sum(1 for t in phase_tasks if t.get("status") == "blocked")
        bug_tasks = sum(1 for t in phase_tasks if t.get("category") == "bug" and t.get("status") != "completed")

        completion_percent = (completed_tasks / total_tasks * 100) if total_tasks > 0 else 100

        # Evaluate each criterion
        results: list[dict[str, Any]] = []
        for criterion in gate_data.get("criteria", []):
            criterion_type = criterion.get("type", "")
            passed = False
            details = ""

            if criterion_type == "task_completion":
                threshold = criterion.get("threshold", 80)
                passed = completion_percent >= threshold
                details = f"{round(completion_percent, 1)}% complete (threshold: {threshold}%)"

            elif criterion_type == "no_blockers":
                passed = blocked_tasks == 0
                details = f"{blocked_tasks} blocked task(s)"

            elif criterion_type == "no_critical_blockers":
                # Check for high-priority blocked tasks
                critical_blocked = sum(
                    1 for t in phase_tasks
                    if t.get("status") == "blocked" and t.get("priority") == "high"
                )
                passed = critical_blocked == 0
                details = f"{critical_blocked} critical blocked task(s)"

            elif criterion_type == "all_bugs_resolved":
                passed = bug_tasks == 0
                details = f"{bug_tasks} unresolved bug(s)"

            elif criterion_type == "milestone_achieved":
                milestone_name = criterion.get("milestone", "")
                try:
                    milestone_results = await prismind.search_knowledge(
                        query=f"milestone {milestone_name}",
                        category="milestone",
                        project=project,
                        limit=1,
                        user=effective_user,
                    )
                    for m_result in milestone_results:
                        content = m_result.get("content", "")
                        try:
                            m_data = json.loads(content)
                            if m_data.get("type") == "milestone" and m_data.get("name", "").lower() == milestone_name.lower():
                                passed = m_data.get("status") == "completed"
                                details = f"Milestone status: {m_data.get('status', 'unknown')}"
                                break
                        except (json.JSONDecodeError, TypeError):
                            continue
                    if not details:
                        details = f"Milestone '{milestone_name}' not found"
                except Exception as e:
                    details = f"Failed to check milestone: {e}"

            elif criterion_type == "custom":
                # Custom criteria require manual verification
                passed = False
                details = f"Manual check required: {criterion.get('description', 'No description')}"

            results.append({
                "type": criterion_type,
                "description": criterion.get("description", criterion_type),
                "passed": passed,
                "details": details,
            })

        passed_count = sum(1 for r in results if r["passed"])
        failed_count = len(results) - passed_count
        all_passed = failed_count == 0

        return {
            "passed": all_passed,
            "phase": target_phase,
            "gate_name": gate_data.get("name", ""),
            "results": results,
            "passed_count": passed_count,
            "failed_count": failed_count,
            "completion_percent": round(completion_percent, 1),
            "message": (
                f"Quality gate PASSED ({passed_count}/{len(results)} criteria met)"
                if all_passed
                else f"Quality gate FAILED ({failed_count} criteria not met)"
            ),
        }

    @mcp.tool()
    async def list_quality_gates(
        project: str,
        user: str = "",
    ) -> dict[str, Any]:
        """List all quality gates defined for a project.

        USE THIS WHEN: You need to see all defined quality gates and their criteria.

        Args:
            project: Project identifier.
            user: User identifier for multi-user support.

        Returns:
            Dict containing:
            - gates: List of quality gate definitions
            - total: Number of gates defined
            - phases_with_gates: List of phases that have gates
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
            "Listing quality gates",
            project=project,
            user=effective_user,
        )

        try:
            results = await prismind.search_knowledge(
                query="quality_gate",
                category="quality_gate",
                project=project,
                limit=20,
                user=effective_user,
            )
        except Exception as e:
            logger.error("Failed to list quality gates", error=str(e))
            return {
                "gates": [],
                "total": 0,
                "phases_with_gates": [],
                "message": f"Failed to list quality gates: {e}",
            }

        gates: list[dict[str, Any]] = []
        phases_with_gates: list[str] = []

        for result in results:
            content = result.get("content", "")
            try:
                data = json.loads(content)
                if data.get("type") == "quality_gate":
                    gates.append({
                        "gate_id": data.get("gate_id", ""),
                        "name": data.get("name", ""),
                        "phase": data.get("phase", ""),
                        "description": data.get("description", ""),
                        "criteria_count": len(data.get("criteria", [])),
                        "criteria": data.get("criteria", []),
                        "created_at": data.get("created_at", ""),
                    })
                    phase = data.get("phase", "")
                    if phase and phase not in phases_with_gates:
                        phases_with_gates.append(phase)
            except (json.JSONDecodeError, TypeError):
                continue

        # Sort by phase
        gates.sort(key=lambda g: g.get("phase", ""))

        return {
            "gates": gates,
            "total": len(gates),
            "phases_with_gates": phases_with_gates,
            "message": f"Found {len(gates)} quality gate(s) for {len(phases_with_gates)} phase(s)",
        }
