"""Session management tools for Magickit MCP server.

Provides tools for maintaining context across Claude sessions by combining
Prismind (session/knowledge management) with Cognilens (compression/summarization).
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from magickit.adapters.cognilens import CognilensAdapter
from magickit.adapters.prismind import PrismindAdapter
from magickit.config import Settings
from magickit.utils.logging import get_logger

logger = get_logger(__name__)

# Module-level settings reference
_settings: Settings | None = None

# Detail level to token budget mapping
DETAIL_LEVEL_TOKENS = {
    "minimal": 500,
    "standard": 2000,
    "full": 4000,
}


async def _begin_task_impl(
    project: str,
    task_description: str = "",
    max_tokens: int = 2000,
) -> dict[str, Any]:
    """Internal implementation for begin_task logic.

    This function contains the actual implementation that both begin_task
    and resume tools delegate to.
    """
    if _settings is None:
        raise RuntimeError("Settings not initialized")

    prismind = PrismindAdapter(
        sse_url=_settings.prismind_url,
        timeout=_settings.prismind_timeout,
    )

    logger.info(
        "Starting task session",
        project=project,
        task_description=task_description[:50] if task_description else "",
    )

    # Step 1: Start session in Prismind
    try:
        session_result = await prismind.start_session(project=project)
        session_data = _parse_result(session_result)
    except Exception as e:
        logger.error("Failed to start session", project=project, error=str(e))
        raise RuntimeError(f"Failed to start session for project {project}: {e}")

    # Step 2: Search for relevant knowledge
    query = task_description or f"project {project} context decisions blockers"
    try:
        knowledge_results = await prismind.search_knowledge(
            query=query,
            project=project,
            limit=10,
        )
        knowledge_list = _parse_list_result(knowledge_results)
    except Exception as e:
        logger.warning("Failed to search knowledge", error=str(e))
        knowledge_list = []

    # Step 3: Build context string
    context_parts = []

    # Add session state
    if isinstance(session_data, dict):
        if session_data.get("current_phase"):
            context_parts.append(f"Current Phase: {session_data.get('current_phase')}")
        if session_data.get("current_task"):
            context_parts.append(f"Current Task: {session_data.get('current_task')}")
        if session_data.get("last_completed"):
            context_parts.append(f"Last Completed: {session_data.get('last_completed')}")
        if session_data.get("blockers"):
            blockers = session_data.get("blockers", [])
            if blockers:
                context_parts.append(f"Blockers: {', '.join(blockers)}")
        if session_data.get("notes"):
            context_parts.append(f"Notes: {session_data.get('notes')}")

    # Add knowledge entries
    if knowledge_list:
        context_parts.append("\n--- Relevant Knowledge ---")
        for entry in knowledge_list:
            if isinstance(entry, dict):
                content = entry.get("content", "")
                category = entry.get("category", "")
                if content:
                    prefix = f"[{category}] " if category else ""
                    context_parts.append(f"{prefix}{content}")

    combined_context = "\n\n".join(context_parts)
    estimated_tokens = len(combined_context) // 4

    # Step 4: Compress if needed
    if estimated_tokens > max_tokens and combined_context:
        cognilens = CognilensAdapter(
            sse_url=_settings.cognilens_url,
            timeout=_settings.cognilens_timeout,
        )

        logger.info(
            "Compressing context",
            original_tokens=estimated_tokens,
            target_tokens=max_tokens,
        )

        try:
            combined_context = await cognilens.optimize_context(
                context=combined_context,
                task_description=f"Restore context for: {task_description or project}",
                target_tokens=max_tokens,
            )
        except Exception as e:
            logger.warning("Context compression failed", error=str(e))
            # Truncate as fallback
            max_chars = max_tokens * 4
            combined_context = combined_context[:max_chars] + "..."

    # Build response
    response: dict[str, Any] = {
        "project": project,
        "session_id": session_data.get("session_id", "") if isinstance(session_data, dict) else "",
        "current_phase": session_data.get("current_phase", "") if isinstance(session_data, dict) else "",
        "current_task": session_data.get("current_task", "") if isinstance(session_data, dict) else "",
        "last_completed": session_data.get("last_completed", "") if isinstance(session_data, dict) else "",
        "blockers": session_data.get("blockers", []) if isinstance(session_data, dict) else [],
        "context": combined_context,
        "recommended_docs": session_data.get("recommended_docs", []) if isinstance(session_data, dict) else [],
        "knowledge_count": len(knowledge_list),
        "notes": session_data.get("notes", "") if isinstance(session_data, dict) else "",
    }

    logger.info(
        "Task session started",
        project=project,
        knowledge_count=len(knowledge_list),
    )

    return response


def register_tools(mcp: FastMCP, settings: Settings) -> None:
    """Register session management tools with the MCP server.

    Args:
        mcp: FastMCP server instance.
        settings: Application settings.
    """
    global _settings
    _settings = settings

    @mcp.tool()
    async def begin_task(
        project: str,
        task_description: str = "",
        max_tokens: int = 2000,
    ) -> dict[str, Any]:
        """Start a task session and restore relevant context from previous sessions.

        USE THIS WHEN: Beginning work on a project to restore prior context,
        decisions, and knowledge. This tool:
        - Starts a new session in Prismind for the project
        - Retrieves relevant knowledge and prior session state
        - Compresses context to fit within token budget

        DO NOT USE WHEN:
        - Continuing within the same session → state is already loaded
        - Just searching for knowledge → use research_and_summarize

        Args:
            project: Project identifier (e.g., "trapxtrap").
            task_description: Optional description of the current task for context retrieval.
            max_tokens: Maximum tokens for the restored context.

        Returns:
            Dict containing:
            - project: Project identifier
            - session_id: New session ID
            - current_phase: Current project phase
            - current_task: Current active task
            - last_completed: Last completed task
            - blockers: List of known blockers
            - context: Compressed relevant context
            - recommended_docs: Related documents to review
            - knowledge_count: Number of relevant knowledge entries found
            - notes: Session notes from prior work
        """
        return await _begin_task_impl(project, task_description, max_tokens)

    @mcp.tool()
    async def checkpoint(
        summary: str,
        decisions: list[str] | None = None,
        blockers: list[str] | None = None,
        auto_extract: bool = True,
    ) -> dict[str, Any]:
        """Save intermediate progress during a session.

        USE THIS WHEN: You want to save progress mid-session, record important
        decisions, or note blockers. This tool:
        - Saves session state to Prismind
        - Optionally extracts and saves key decisions as knowledge
        - Uses Cognilens to extract essence if summary is long

        DO NOT USE WHEN:
        - Ending a session → use handoff instead
        - Just searching/reading → no state to save

        Args:
            summary: Summary of work done since last checkpoint.
            decisions: List of decisions made (will be saved as knowledge).
            blockers: List of current blockers or issues.
            auto_extract: If True, use Cognilens to extract essence from long summaries.

        Returns:
            Dict containing:
            - success: Whether the checkpoint was saved
            - saved_to: List of storage locations used
            - knowledge_added: Number of knowledge entries created
            - message: Status message
        """
        if _settings is None:
            raise RuntimeError("Settings not initialized")

        prismind = PrismindAdapter(
            sse_url=_settings.prismind_url,
            timeout=_settings.prismind_timeout,
        )

        saved_to = []
        knowledge_added = 0
        errors = []

        logger.info(
            "Creating checkpoint",
            summary_length=len(summary),
            decisions_count=len(decisions) if decisions else 0,
        )

        # Step 1: Extract essence if summary is long
        processed_summary = summary
        if auto_extract and len(summary) > 500:
            cognilens = CognilensAdapter(
                sse_url=_settings.cognilens_url,
                timeout=_settings.cognilens_timeout,
            )

            try:
                essence_result = await cognilens.extract_essence(
                    document=summary,
                    focus_areas=["key accomplishments", "decisions", "blockers"],
                )
                if isinstance(essence_result, dict):
                    # Use extracted essence for the summary
                    key_points = essence_result.get("key_concepts", [])
                    if key_points:
                        processed_summary = "; ".join(key_points)
                logger.info("Extracted essence from summary")
            except Exception as e:
                logger.warning("Essence extraction failed", error=str(e))
                # Continue with original summary

        # Step 2: Save session state
        try:
            save_args: dict[str, Any] = {"summary": processed_summary}
            if blockers:
                save_args["blockers"] = blockers

            await prismind.save_session(**save_args)
            saved_to.append("session")
            logger.info("Session state saved")
        except Exception as e:
            logger.error("Failed to save session", error=str(e))
            errors.append(f"Session save failed: {e}")

        # Step 3: Save decisions as knowledge
        if decisions:
            for decision in decisions:
                try:
                    await prismind.add_knowledge(
                        content=decision,
                        category="decision",
                    )
                    knowledge_added += 1
                except Exception as e:
                    logger.warning("Failed to save decision", decision=decision[:50], error=str(e))
                    errors.append(f"Decision save failed: {decision[:30]}...")

            if knowledge_added > 0:
                saved_to.append("knowledge")
                logger.info("Decisions saved as knowledge", count=knowledge_added)

        success = len(errors) == 0 or "session" in saved_to
        message = "Checkpoint saved successfully"
        if errors:
            message = f"Checkpoint saved with {len(errors)} warning(s): {'; '.join(errors[:2])}"

        return {
            "success": success,
            "saved_to": saved_to,
            "knowledge_added": knowledge_added,
            "message": message,
        }

    @mcp.tool()
    async def handoff(
        next_action: str,
        notes: str = "",
        blockers: list[str] | None = None,
        save_insights: bool = True,
    ) -> dict[str, Any]:
        """End a session and prepare handoff for the next session.

        USE THIS WHEN: Ending a work session and want to preserve context
        for the next Claude session. This tool:
        - Summarizes notes if they're long
        - Ends the session in Prismind with handoff information
        - Optionally extracts and saves session insights as knowledge

        DO NOT USE WHEN:
        - Just taking a break within the same session → use checkpoint
        - Abandoning work without wanting to save → no tool needed

        Args:
            next_action: The recommended next step for the following session.
            notes: Additional notes or context to pass to the next session.
            blockers: List of blockers that need resolution.
            save_insights: If True, extract and save session insights as knowledge.

        Returns:
            Dict containing:
            - success: Whether the handoff was completed
            - session_duration: Duration of the session (if available)
            - summary: Final session summary
            - saved_to: List of storage locations used
            - insights_saved: Number of insight entries created
            - message: Status message
        """
        if _settings is None:
            raise RuntimeError("Settings not initialized")

        prismind = PrismindAdapter(
            sse_url=_settings.prismind_url,
            timeout=_settings.prismind_timeout,
        )

        saved_to = []
        insights_saved = 0
        processed_notes = notes

        logger.info(
            "Performing handoff",
            next_action=next_action[:50],
            notes_length=len(notes),
        )

        # Step 1: Summarize notes if long
        if len(notes) > 500:
            cognilens = CognilensAdapter(
                sse_url=_settings.cognilens_url,
                timeout=_settings.cognilens_timeout,
            )

            try:
                processed_notes = await cognilens.summarize(
                    text=notes,
                    style="concise",
                    max_tokens=200,
                )
                logger.info("Notes summarized", original_length=len(notes))
            except Exception as e:
                logger.warning("Notes summarization failed", error=str(e))
                # Truncate as fallback
                processed_notes = notes[:500] + "..."

        # Step 2: End session in Prismind
        try:
            end_args: dict[str, Any] = {
                "next_action": next_action,
                "notes": processed_notes,
            }
            if blockers:
                end_args["blockers"] = blockers

            session_result = await prismind.end_session(**end_args)
            session_data = _parse_result(session_result)
            saved_to.append("session")
            logger.info("Session ended")
        except Exception as e:
            logger.error("Failed to end session", error=str(e))
            return {
                "success": False,
                "session_duration": "",
                "summary": "",
                "saved_to": saved_to,
                "insights_saved": 0,
                "message": f"Failed to end session: {e}",
            }

        # Step 3: Extract and save insights if requested
        if save_insights and notes:
            cognilens = CognilensAdapter(
                sse_url=_settings.cognilens_url,
                timeout=_settings.cognilens_timeout,
            )

            try:
                essence_result = await cognilens.extract_essence(
                    document=notes,
                    focus_areas=["learnings", "patterns", "recommendations"],
                )

                if isinstance(essence_result, dict):
                    # Save key concepts as session insights
                    key_concepts = essence_result.get("key_concepts", [])
                    for concept in key_concepts[:5]:  # Limit to 5 insights
                        try:
                            await prismind.add_knowledge(
                                content=concept,
                                category="session_insight",
                            )
                            insights_saved += 1
                        except Exception as e:
                            logger.warning("Failed to save insight", error=str(e))

                if insights_saved > 0:
                    saved_to.append("knowledge")
                    logger.info("Session insights saved", count=insights_saved)

            except Exception as e:
                logger.warning("Insight extraction failed", error=str(e))

        # Build response
        session_duration = ""
        summary = ""
        if isinstance(session_data, dict):
            session_duration = session_data.get("duration", "")
            summary = session_data.get("summary", f"Next: {next_action}")

        return {
            "success": True,
            "session_duration": session_duration,
            "summary": summary or f"Session ended. Next action: {next_action}",
            "saved_to": saved_to,
            "insights_saved": insights_saved,
            "message": "Handoff completed successfully",
        }

    @mcp.tool()
    async def resume(
        project: str,
        detail_level: str = "standard",
        task_description: str = "",
    ) -> dict[str, Any]:
        """Resume work on a project with preset detail levels.

        This is a convenience wrapper around begin_task with preset token budgets:
        - minimal: 500 tokens (quick overview)
        - standard: 2000 tokens (balanced context)
        - full: 4000 tokens (comprehensive context)

        USE THIS WHEN: Quickly resuming work without specifying exact token limits.

        Args:
            project: Project identifier (e.g., "trapxtrap").
            detail_level: Amount of context to restore ("minimal", "standard", "full").
            task_description: Optional description of the task to focus context retrieval.

        Returns:
            Same structure as begin_task.
        """
        max_tokens = DETAIL_LEVEL_TOKENS.get(detail_level, DETAIL_LEVEL_TOKENS["standard"])

        logger.info(
            "Resuming project",
            project=project,
            detail_level=detail_level,
            max_tokens=max_tokens,
        )

        # Delegate to internal implementation
        return await _begin_task_impl(
            project=project,
            task_description=task_description,
            max_tokens=max_tokens,
        )


def _parse_result(result: Any) -> dict[str, Any]:
    """Parse tool result to dict."""
    import json

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
    import json

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
                # Try common list keys
                for key in ["results", "items", "documents", "knowledge", "entries"]:
                    if key in data and isinstance(data[key], list):
                        return data[key]
                return [data]
            return [{"result": data}]
        except json.JSONDecodeError:
            return [{"result": result}]
    return [{"result": result}]
