"""Execution tools for task decomposition and queue management.

Provides tools for breaking down specifications into executable tasks,
managing task queues, and tracking execution progress.
"""

from __future__ import annotations

import json
import uuid
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

# In-memory execution session storage
_execution_sessions: dict[str, dict[str, Any]] = {}


async def decompose_specification(
    specification: dict[str, Any],
    session_id: str = "",
    granularity: str = "medium",
    user: str = "",
) -> dict[str, Any]:
    """Decompose a specification into executable tasks with dependencies.

    USE THIS WHEN: You have a generated specification and want to break it
    down into a sequence of executable steps for automated implementation.

    This tool uses LLM to analyze the specification and generate a task list
    with proper ordering and dependencies.

    Args:
        specification: The specification dict from generate_specification.
        session_id: Optional session ID for tracking (generates new if empty).
        granularity: Task granularity level:
            - "fine": Many small tasks (good for complex changes)
            - "medium": Balanced task size (default)
            - "coarse": Fewer larger tasks (good for simple changes)
        user: User identifier for multi-user support (empty for default user).

    Returns:
        Dict containing:
        - success: Whether decomposition succeeded
        - execution_id: ID for this execution session
        - tasks: List of tasks with id, name, description, dependencies
        - task_count: Number of tasks generated
        - estimated_steps: Rough estimate of implementation steps
        - next_action: Instructions for starting execution
    """
    if _settings is None:
        raise RuntimeError("Settings not initialized")

    effective_user = user or get_current_user()
    execution_id = session_id or f"exec-{uuid.uuid4().hex[:8]}"

    logger.info(
        "Decomposing specification",
        execution_id=execution_id,
        granularity=granularity,
        user=effective_user,
    )

    # Extract specification data
    spec_data = specification.get("specification", specification)
    title = spec_data.get("title", "Untitled")
    purpose = spec_data.get("purpose", "")
    target_files = spec_data.get("target_files", [])
    requirements = spec_data.get("requirements", [])
    constraints = spec_data.get("constraints", [])
    test_points = spec_data.get("test_points", [])

    # Generate tasks using LLM
    lexora = LexoraAdapter(
        base_url=_settings.lexora_url,
        timeout=_settings.lexora_timeout,
    )

    granularity_hints = {
        "fine": "各ファイルの各関数レベルで細かくタスクを分割してください。",
        "medium": "論理的なまとまりでタスクを分割してください。1タスク = 1つの明確な変更。",
        "coarse": "大きなまとまりでタスクを分割してください。1タスク = 1つの機能追加/変更。",
    }

    system_prompt = f"""あなたは実装計画のスペシャリストです。
仕様書を分析し、実行可能なタスクリストに分解します。

ルール:
1. 各タスクは独立して実行可能であること
2. 依存関係がある場合は明示すること
3. タスクの順序は依存関係を考慮すること
4. {granularity_hints.get(granularity, granularity_hints["medium"])}

出力形式（JSON）:
{{
  "tasks": [
    {{
      "id": "task-1",
      "name": "タスク名（簡潔に）",
      "description": "何をするか（具体的に）",
      "target_files": ["file1.py"],
      "action_type": "create|modify|delete|test",
      "dependencies": [],
      "priority": 1
    }},
    {{
      "id": "task-2",
      "name": "次のタスク",
      "description": "詳細",
      "target_files": ["file2.py"],
      "action_type": "modify",
      "dependencies": ["task-1"],
      "priority": 2
    }}
  ]
}}

action_type:
- create: 新規ファイル/関数の作成
- modify: 既存コードの変更
- delete: 不要コードの削除
- test: テストの実行/追加"""

    user_prompt = f"""以下の仕様書をタスクに分解してください。

【仕様書】
タイトル: {title}
目的: {purpose}

対象ファイル:
{chr(10).join(f"- {f}" for f in target_files) if target_files else "- 未指定"}

要件:
{chr(10).join(f"- {r}" for r in requirements) if requirements else "- 未指定"}

制約:
{chr(10).join(f"- {c}" for c in constraints) if constraints else "- なし"}

テスト観点:
{chr(10).join(f"- {t}" for t in test_points) if test_points else "- 未指定"}

JSON形式で出力してください。"""

    try:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        response = await lexora.chat(
            messages=messages,
            max_tokens=2000,
            temperature=0.2,
        )

        # Parse LLM response
        tasks = _parse_tasks_response(response)

        if not tasks:
            # Fallback: Generate basic tasks from specification
            tasks = _generate_fallback_tasks(spec_data)

    except Exception as e:
        logger.error("Task decomposition failed", error=str(e))
        # Fallback to basic task generation
        tasks = _generate_fallback_tasks(spec_data)

    # Add execution metadata to each task
    for i, task in enumerate(tasks):
        task["status"] = "pending"
        task["created_at"] = datetime.utcnow().isoformat()
        if "priority" not in task:
            task["priority"] = i + 1

    # Store execution session
    _execution_sessions[execution_id] = {
        "specification": specification,
        "tasks": tasks,
        "current_task_index": 0,
        "completed_tasks": [],
        "failed_tasks": [],
        "status": "ready",
        "created_at": datetime.utcnow().isoformat(),
    }

    return {
        "success": True,
        "execution_id": execution_id,
        "tasks": tasks,
        "task_count": len(tasks),
        "estimated_steps": len(tasks),
        "next_action": {
            "instruction": (
                "Use get_next_task to retrieve the first task, then execute it. "
                "After completing each task, use complete_execution_task to mark it done"
                "and get the next one."
            ),
            "first_task": tasks[0] if tasks else None,
        },
    }


async def get_next_task(
    execution_id: str,
    user: str = "",
) -> dict[str, Any]:
    """Get the next task ready for execution.

    USE THIS WHEN: You're in the middle of an automated execution session
    and need to know what task to work on next.

    Args:
        execution_id: Execution session ID from decompose_specification.
        user: User identifier for multi-user support (empty for default user).

    Returns:
        Dict containing:
        - has_task: Whether there's a task available
        - task: The next task to execute (if available)
        - progress: Current progress (completed/total)
        - remaining: Number of remaining tasks
    """
    effective_user = user or get_current_user()

    if execution_id not in _execution_sessions:
        return {
            "has_task": False,
            "error": f"Execution session not found: {execution_id}",
            "task": None,
            "progress": "0/0",
            "remaining": 0,
        }

    session = _execution_sessions[execution_id]
    tasks = session["tasks"]
    completed = session["completed_tasks"]
    failed = session["failed_tasks"]

    # Find next pending task whose dependencies are met
    for task in tasks:
        if task["status"] != "pending":
            continue

        # Check dependencies
        deps = task.get("dependencies", [])
        deps_met = all(
            any(ct["id"] == dep for ct in completed)
            for dep in deps
        )

        if deps_met:
            # Mark as in_progress
            task["status"] = "in_progress"
            task["started_at"] = datetime.utcnow().isoformat()

            logger.info(
                "Task retrieved",
                execution_id=execution_id,
                task_id=task["id"],
                task_name=task["name"],
            )

            return {
                "has_task": True,
                "task": task,
                "progress": f"{len(completed)}/{len(tasks)}",
                "remaining": len(tasks) - len(completed) - len(failed),
            }

    # No tasks available
    all_done = len(completed) + len(failed) >= len(tasks)
    session["status"] = "completed" if all_done else "blocked"

    return {
        "has_task": False,
        "task": None,
        "progress": f"{len(completed)}/{len(tasks)}",
        "remaining": len(tasks) - len(completed) - len(failed),
        "status": "completed" if all_done else "waiting_for_dependencies",
    }


async def complete_execution_task(
    execution_id: str,
    task_id: str,
    success: bool = True,
    result: str = "",
    error: str = "",
    user: str = "",
) -> dict[str, Any]:
    """Mark an execution task as completed or failed.

    USE THIS WHEN: You've finished working on a task in an execution session
    (from decompose_specification) and want to record the result and get the next task.

    Note: This is different from complete_task in task.py which is for
    Prismind-backed project task management.

    Args:
        execution_id: Execution session ID from decompose_specification.
        task_id: ID of the completed task.
        success: Whether the task succeeded.
        result: Result or summary of what was done.
        error: Error message if the task failed.
        user: User identifier for multi-user support (empty for default user).

    Returns:
        Dict containing:
        - success: Whether the update succeeded
        - next_task: The next task to execute (if available)
        - progress: Current progress
        - is_complete: Whether all tasks are done
    """
    effective_user = user or get_current_user()

    if execution_id not in _execution_sessions:
        return {
            "success": False,
            "error": f"Execution session not found: {execution_id}",
            "next_task": None,
            "progress": "0/0",
            "is_complete": False,
        }

    session = _execution_sessions[execution_id]
    tasks = session["tasks"]

    # Find the task
    task = None
    for t in tasks:
        if t["id"] == task_id:
            task = t
            break

    if not task:
        return {
            "success": False,
            "error": f"Task not found: {task_id}",
            "next_task": None,
            "progress": f"{len(session['completed_tasks'])}/{len(tasks)}",
            "is_complete": False,
        }

    # Update task status
    task["status"] = "completed" if success else "failed"
    task["completed_at"] = datetime.utcnow().isoformat()
    task["result"] = result
    if error:
        task["error"] = error

    # Add to appropriate list
    if success:
        session["completed_tasks"].append(task)
    else:
        session["failed_tasks"].append(task)

    logger.info(
        "Task completed",
        execution_id=execution_id,
        task_id=task_id,
        success=success,
    )

    # Get next task
    next_task_result = await get_next_task(execution_id)

    completed_count = len(session["completed_tasks"])
    failed_count = len(session["failed_tasks"])
    total_count = len(tasks)
    is_complete = completed_count + failed_count >= total_count

    if is_complete:
        session["status"] = "completed"

    return {
        "success": True,
        "task_completed": task_id,
        "next_task": next_task_result.get("task"),
        "has_next_task": next_task_result.get("has_task", False),
        "progress": f"{completed_count}/{total_count}",
        "is_complete": is_complete,
        "summary": {
            "completed": completed_count,
            "failed": failed_count,
            "total": total_count,
        },
    }


async def get_execution_status(
    execution_id: str,
    user: str = "",
) -> dict[str, Any]:
    """Get the current status of an execution session.

    USE THIS WHEN: You want to check the overall progress of an
    automated execution session.

    Args:
        execution_id: Execution session ID.
        user: User identifier for multi-user support (empty for default user).

    Returns:
        Dict containing:
        - found: Whether the session exists
        - status: Overall status (ready, in_progress, completed, blocked)
        - progress: Progress details
        - tasks: All tasks with their current status
    """
    effective_user = user or get_current_user()

    if execution_id not in _execution_sessions:
        return {
            "found": False,
            "error": f"Execution session not found: {execution_id}",
        }

    session = _execution_sessions[execution_id]
    tasks = session["tasks"]
    completed = len(session["completed_tasks"])
    failed = len(session["failed_tasks"])
    total = len(tasks)

    # Count in-progress tasks
    in_progress = sum(1 for t in tasks if t["status"] == "in_progress")
    pending = sum(1 for t in tasks if t["status"] == "pending")

    return {
        "found": True,
        "execution_id": execution_id,
        "status": session["status"],
        "progress": {
            "completed": completed,
            "failed": failed,
            "in_progress": in_progress,
            "pending": pending,
            "total": total,
            "percent": round(completed / total * 100, 1) if total > 0 else 0,
        },
        "tasks": [
            {
                "id": t["id"],
                "name": t["name"],
                "status": t["status"],
                "dependencies": t.get("dependencies", []),
            }
            for t in tasks
        ],
        "created_at": session["created_at"],
    }


def _parse_tasks_response(response: str) -> list[dict[str, Any]]:
    """Parse LLM response to extract tasks."""
    try:
        # Try to find JSON in response
        start = response.find("{")
        end = response.rfind("}") + 1
        if start >= 0 and end > start:
            data = json.loads(response[start:end])
            if "tasks" in data:
                return data["tasks"]
    except json.JSONDecodeError:
        pass

    logger.warning("Failed to parse tasks response, using fallback")
    return []


def _generate_fallback_tasks(spec_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Generate basic tasks from specification when LLM fails."""
    tasks = []
    target_files = spec_data.get("target_files", [])
    requirements = spec_data.get("requirements", [])
    test_points = spec_data.get("test_points", [])

    task_id = 1

    # Create tasks for each file
    for file_path in target_files:
        tasks.append({
            "id": f"task-{task_id}",
            "name": f"Modify {file_path}",
            "description": f"Implement changes in {file_path}",
            "target_files": [file_path],
            "action_type": "modify",
            "dependencies": [f"task-{task_id - 1}"] if task_id > 1 else [],
            "priority": task_id,
        })
        task_id += 1

    # Add test task if test points exist
    if test_points:
        tasks.append({
            "id": f"task-{task_id}",
            "name": "Run tests",
            "description": f"Verify: {', '.join(test_points[:3])}",
            "target_files": [],
            "action_type": "test",
            "dependencies": [f"task-{task_id - 1}"] if task_id > 1 else [],
            "priority": task_id,
        })

    # If no files specified, create a generic task from requirements
    if not tasks and requirements:
        tasks.append({
            "id": "task-1",
            "name": "Implement requirements",
            "description": "\n".join(requirements[:5]),
            "target_files": [],
            "action_type": "modify",
            "dependencies": [],
            "priority": 1,
        })

    return tasks


async def finalize_execution(
    execution_id: str,
    project: str = "",
    save_to_knowledge: bool = True,
    user: str = "",
) -> dict[str, Any]:
    """Finalize an execution session and record results.

    USE THIS WHEN: All tasks in an execution session are complete and you
    want to save the results, generate a summary, and prepare for handoff.

    This tool:
    - Generates an execution summary
    - Optionally saves results to Prismind as knowledge
    - Prepares handoff information for the next session

    Args:
        execution_id: Execution session ID.
        project: Project name for saving to Prismind.
        save_to_knowledge: Whether to save results as knowledge entries.
        user: User identifier for multi-user support (empty for default user).

    Returns:
        Dict containing:
        - success: Whether finalization succeeded
        - summary: Execution summary
        - knowledge_saved: Number of knowledge entries created
        - handoff: Information for next session
    """
    if _settings is None:
        raise RuntimeError("Settings not initialized")

    effective_user = user or get_current_user()

    if execution_id not in _execution_sessions:
        return {
            "success": False,
            "error": f"Execution session not found: {execution_id}",
        }

    session = _execution_sessions[execution_id]
    tasks = session["tasks"]
    completed_tasks = session["completed_tasks"]
    failed_tasks = session["failed_tasks"]
    specification = session.get("specification", {})

    logger.info(
        "Finalizing execution",
        execution_id=execution_id,
        completed=len(completed_tasks),
        failed=len(failed_tasks),
        user=effective_user,
    )

    # Generate summary
    spec_data = specification.get("specification", specification)
    title = spec_data.get("title", "Untitled Implementation")

    summary_parts = [
        f"# {title} - 実行結果",
        "",
        f"## 概要",
        f"- 完了タスク: {len(completed_tasks)}/{len(tasks)}",
        f"- 失敗タスク: {len(failed_tasks)}",
        "",
    ]

    if completed_tasks:
        summary_parts.append("## 完了したタスク")
        for task in completed_tasks:
            result = task.get("result", "")
            summary_parts.append(f"- **{task['name']}**: {result[:100] if result else '完了'}")
        summary_parts.append("")

    if failed_tasks:
        summary_parts.append("## 失敗したタスク")
        for task in failed_tasks:
            error = task.get("error", "不明なエラー")
            summary_parts.append(f"- **{task['name']}**: {error}")
        summary_parts.append("")

    # Extract learnings and decisions
    learnings = []
    for task in completed_tasks:
        if task.get("result"):
            learnings.append(f"- {task['name']}: {task['result'][:200]}")

    if learnings:
        summary_parts.append("## 実装メモ")
        summary_parts.extend(learnings[:5])
        summary_parts.append("")

    summary = "\n".join(summary_parts)

    # Save to Prismind if requested
    knowledge_saved = 0
    if save_to_knowledge and project:
        try:
            prismind = PrismindAdapter(
                sse_url=_settings.prismind_url,
                timeout=_settings.prismind_timeout,
            )

            # Save execution summary
            await prismind.add_knowledge(
                content=summary,
                category="実装記録",
                project=project,
                tags=["execution", "implementation", title[:30]],
                source=f"execution:{execution_id}",
                user=effective_user,
            )
            knowledge_saved += 1

            # Save individual task results as knowledge (for significant tasks)
            for task in completed_tasks:
                if task.get("result") and len(task.get("result", "")) > 50:
                    await prismind.add_knowledge(
                        content=f"# {task['name']}\n\n{task.get('result', '')}",
                        category="実装詳細",
                        project=project,
                        tags=["task-result", task.get("action_type", "modify")],
                        source=f"task:{task['id']}",
                        user=effective_user,
                    )
                    knowledge_saved += 1

            logger.info("Knowledge saved", count=knowledge_saved)

        except Exception as e:
            logger.warning("Failed to save knowledge", error=str(e))

    # Update session status
    session["status"] = "finalized"
    session["finalized_at"] = datetime.utcnow().isoformat()
    session["summary"] = summary

    # Prepare handoff
    handoff = {
        "execution_id": execution_id,
        "title": title,
        "status": "success" if not failed_tasks else "partial",
        "completed_count": len(completed_tasks),
        "failed_count": len(failed_tasks),
        "next_steps": [],
    }

    if failed_tasks:
        handoff["next_steps"].append(f"Retry failed tasks: {', '.join(t['name'] for t in failed_tasks[:3])}")

    # Suggest next actions based on specification
    test_points = spec_data.get("test_points", [])
    if test_points and not any(t.get("action_type") == "test" for t in completed_tasks):
        handoff["next_steps"].append("Run tests to verify implementation")

    return {
        "success": True,
        "execution_id": execution_id,
        "summary": summary,
        "knowledge_saved": knowledge_saved,
        "handoff": handoff,
        "statistics": {
            "total_tasks": len(tasks),
            "completed": len(completed_tasks),
            "failed": len(failed_tasks),
            "success_rate": round(len(completed_tasks) / len(tasks) * 100, 1) if tasks else 0,
        },
    }


async def generate_execution_report(
    execution_id: str,
    format: str = "markdown",
    include_details: bool = True,
    user: str = "",
) -> dict[str, Any]:
    """Generate a detailed execution report.

    USE THIS WHEN: You want a formatted report of the execution session
    for documentation or review purposes.

    Args:
        execution_id: Execution session ID.
        format: Output format ("markdown", "changelog", "brief").
        include_details: Whether to include detailed task information.
        user: User identifier for multi-user support (empty for default user).

    Returns:
        Dict containing:
        - success: Whether generation succeeded
        - report: The formatted report
        - format: The format used
    """
    effective_user = user or get_current_user()

    if execution_id not in _execution_sessions:
        return {
            "success": False,
            "error": f"Execution session not found: {execution_id}",
        }

    session = _execution_sessions[execution_id]
    tasks = session["tasks"]
    completed_tasks = session["completed_tasks"]
    failed_tasks = session["failed_tasks"]
    specification = session.get("specification", {})
    spec_data = specification.get("specification", specification)
    title = spec_data.get("title", "Implementation")

    if format == "changelog":
        # CHANGELOG format
        lines = [
            f"## [{title}] - {datetime.utcnow().strftime('%Y-%m-%d')}",
            "",
        ]

        # Group by action type
        added = [t for t in completed_tasks if t.get("action_type") == "create"]
        changed = [t for t in completed_tasks if t.get("action_type") == "modify"]
        removed = [t for t in completed_tasks if t.get("action_type") == "delete"]

        if added:
            lines.append("### Added")
            for t in added:
                lines.append(f"- {t['name']}")
            lines.append("")

        if changed:
            lines.append("### Changed")
            for t in changed:
                lines.append(f"- {t['name']}")
            lines.append("")

        if removed:
            lines.append("### Removed")
            for t in removed:
                lines.append(f"- {t['name']}")
            lines.append("")

        report = "\n".join(lines)

    elif format == "brief":
        # Brief summary
        status = "✅ Success" if not failed_tasks else f"⚠️ Partial ({len(failed_tasks)} failed)"
        report = f"{title}: {status} - {len(completed_tasks)}/{len(tasks)} tasks completed"

    else:
        # Markdown format (default)
        lines = [
            f"# Execution Report: {title}",
            "",
            f"**Execution ID:** `{execution_id}`",
            f"**Date:** {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC",
            f"**Status:** {'Completed' if not failed_tasks else 'Partial'}",
            "",
            "## Summary",
            "",
            f"| Metric | Value |",
            f"|--------|-------|",
            f"| Total Tasks | {len(tasks)} |",
            f"| Completed | {len(completed_tasks)} |",
            f"| Failed | {len(failed_tasks)} |",
            f"| Success Rate | {round(len(completed_tasks) / len(tasks) * 100, 1) if tasks else 0}% |",
            "",
        ]

        if include_details:
            if completed_tasks:
                lines.append("## Completed Tasks")
                lines.append("")
                for task in completed_tasks:
                    lines.append(f"### {task['name']}")
                    if task.get("description"):
                        lines.append(f"> {task['description']}")
                    if task.get("target_files"):
                        lines.append(f"**Files:** {', '.join(task['target_files'])}")
                    if task.get("result"):
                        lines.append(f"**Result:** {task['result']}")
                    lines.append("")

            if failed_tasks:
                lines.append("## Failed Tasks")
                lines.append("")
                for task in failed_tasks:
                    lines.append(f"### ❌ {task['name']}")
                    if task.get("error"):
                        lines.append(f"**Error:** {task['error']}")
                    lines.append("")

        report = "\n".join(lines)

    return {
        "success": True,
        "execution_id": execution_id,
        "report": report,
        "format": format,
    }


async def run_full_workflow(
    target: str,
    request: str,
    project: str = "",
    feature_type: str = "",
    auto_approve: bool = False,
    user: str = "",
) -> dict[str, Any]:
    """Run the complete specification-to-execution workflow.

    USE THIS WHEN: You want to run the entire workflow from initial request
    to execution planning in one step. This is a convenience tool that
    orchestrates multiple specification and execution tools.

    Note: This does NOT execute the actual implementation - it prepares
    everything for execution. Claude still needs to perform the actual
    code changes using the generated task list.

    Args:
        target: Target file, function, or component.
        request: User's feature request.
        project: Project name for context.
        feature_type: Optional hint about feature type.
        auto_approve: If True, skip question phase (use defaults).
        user: User identifier for multi-user support (empty for default user).

    Returns:
        Dict containing:
        - success: Whether workflow preparation succeeded
        - workflow_id: Combined ID for tracking
        - specification: Generated specification
        - execution_plan: Task list with dependencies
        - permissions: Required permissions
        - next_action: Instructions to start execution
    """
    if _settings is None:
        raise RuntimeError("Settings not initialized")

    effective_user = user or get_current_user()
    workflow_id = f"workflow-{uuid.uuid4().hex[:8]}"

    logger.info(
        "Starting full workflow",
        workflow_id=workflow_id,
        target=target,
        request=request[:50],
        user=effective_user,
    )

    # Import specification tools
    from magickit.mcp.tools import specification

    try:
        # Step 1: Generate specification (skip questions if auto_approve)
        if auto_approve:
            # Generate basic specification directly
            lexora = LexoraAdapter(
                base_url=_settings.lexora_url,
                timeout=_settings.lexora_timeout,
            )

            system_prompt = """仕様書を生成してください。JSON形式で出力。
{
  "specification": {
    "title": "機能名",
    "purpose": "目的",
    "target_files": ["file.py"],
    "requirements": ["要件"],
    "constraints": [],
    "test_points": ["テスト"]
  },
  "required_permissions": {"edit": [], "bash": []}
}"""

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"対象: {target}\n要望: {request}"},
            ]
            response = await lexora.chat(messages=messages, max_tokens=1500, temperature=0.2)
            spec_result = specification._parse_specification_response(response)
            spec_result["success"] = True
        else:
            # Start specification process
            start_result = await specification.start_specification(
                target=target,
                initial_request=request,
                feature_type=feature_type,
                user=effective_user,
            )

            return {
                "success": True,
                "workflow_id": workflow_id,
                "status": "questions_pending",
                "session_id": start_result["session_id"],
                "questions": start_result["questions"],
                "next_action": {
                    "instruction": (
                        "Answer the questions using AskUserQuestion, then call "
                        "generate_specification with the answers, followed by "
                        "decompose_specification to create the task list."
                    ),
                    "questions": start_result["questions"],
                },
            }

        # Step 2: Prepare execution permissions
        exec_prep = await specification.prepare_execution(spec_result, user=effective_user)

        # Step 3: Decompose into tasks
        decompose_result = await decompose_specification(
            specification=spec_result,
            session_id=workflow_id,
            user=effective_user,
        )

        # Store workflow info
        if decompose_result["execution_id"] in _execution_sessions:
            _execution_sessions[decompose_result["execution_id"]]["workflow_id"] = workflow_id
            _execution_sessions[decompose_result["execution_id"]]["project"] = project

        return {
            "success": True,
            "workflow_id": workflow_id,
            "status": "ready_to_execute",
            "specification": spec_result.get("specification", {}),
            "execution_id": decompose_result["execution_id"],
            "execution_plan": {
                "tasks": decompose_result["tasks"],
                "task_count": decompose_result["task_count"],
            },
            "permissions": exec_prep["allowed_prompts"],
            "next_action": {
                "instruction": (
                    "1. Use ExitPlanMode with allowedPrompts to get permission approval\n"
                    "2. Use get_next_task to start executing tasks\n"
                    "3. After each task, use complete_execution_task to record results\n"
                    "4. When done, use finalize_execution to save results"
                ),
                "first_task": decompose_result["tasks"][0] if decompose_result["tasks"] else None,
                "allowed_prompts": exec_prep["allowed_prompts"],
            },
        }

    except Exception as e:
        logger.error("Workflow failed", error=str(e))
        return {
            "success": False,
            "workflow_id": workflow_id,
            "error": str(e),
        }


def register_tools(mcp: FastMCP, settings: Settings) -> None:
    """Register execution tools with the MCP server.

    Args:
        mcp: FastMCP server instance.
        settings: Application settings.
    """
    global _settings
    _settings = settings

    # Register the module-level functions as MCP tools
    mcp.tool()(decompose_specification)
    mcp.tool()(get_next_task)
    mcp.tool()(complete_execution_task)
    mcp.tool()(get_execution_status)
    mcp.tool()(finalize_execution)
    mcp.tool()(generate_execution_report)
    mcp.tool()(run_full_workflow)
