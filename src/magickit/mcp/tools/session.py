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
from magickit.utils.user import get_current_user

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
    user: str = "",
    author: str = "",
) -> dict[str, Any]:
    """Internal implementation for begin_task logic.

    This function contains the actual implementation that both begin_task
    and resume tools delegate to.

    Args:
        project: Project identifier
        task_description: Description of current task
        max_tokens: Maximum tokens for context
        user: User identifier for multi-user support
        author: Context author/role partition (empty for default context)
    """
    if _settings is None:
        raise RuntimeError("Settings not initialized")

    # Auto-detect user if not specified
    effective_user = user or get_current_user()

    prismind = PrismindAdapter(
        sse_url=_settings.prismind_url,
        timeout=_settings.prismind_timeout,
    )

    logger.info(
        "Starting task session",
        project=project,
        task_description=task_description[:50] if task_description else "",
        user=effective_user,
    )

    # Step 1: Start session in Prismind
    try:
        session_result = await prismind.start_session(
            project=project, user=effective_user, author=author
        )
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
            user=effective_user,
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
        if session_data.get("last_summary"):
            context_parts.append(f"Last Summary: {session_data.get('last_summary')}")
        if session_data.get("next_action"):
            context_parts.append(f"Next Action: {session_data.get('next_action')}")
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
        "user": effective_user,
        "author": session_data.get("author", author) if isinstance(session_data, dict) else author,
        "session_id": session_data.get("session_id", "") if isinstance(session_data, dict) else "",
        "current_phase": session_data.get("current_phase", "") if isinstance(session_data, dict) else "",
        "current_task": session_data.get("current_task", "") if isinstance(session_data, dict) else "",
        "last_completed": session_data.get("last_completed", "") if isinstance(session_data, dict) else "",
        "blockers": session_data.get("blockers", []) if isinstance(session_data, dict) else [],
        "last_summary": session_data.get("last_summary", "") if isinstance(session_data, dict) else "",
        "next_action": session_data.get("next_action", "") if isinstance(session_data, dict) else "",
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
        user: str = "",
        author: str = "",
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
            user: User identifier for multi-user support (empty for default user).
            author: Context author/role partition to restore. Use this when
                multiple roles (e.g. "claude.ai", "claude-code") keep separate
                contexts for the same project. Empty restores the default
                context. Call list_context_authors first to see which authors
                already have saved context and avoid naming-variation duplicates.

        Returns:
            Dict containing:
            - project: Project identifier
            - author: Context author/role this context belongs to
            - session_id: New session ID
            - current_phase: Current project phase
            - current_task: Current active task
            - last_completed: Last completed task
            - blockers: List of known blockers
            - last_summary: Summary from the last session
            - next_action: Recommended next action from handoff
            - context: Compressed relevant context
            - recommended_docs: Related documents to review
            - knowledge_count: Number of relevant knowledge entries found
            - notes: Session notes from prior work
            - user: User identifier
        """
        return await _begin_task_impl(project, task_description, max_tokens, user, author)

    @mcp.tool()
    async def checkpoint(
        summary: str,
        project: str = "",
        decisions: list[str] | None = None,
        blockers: list[str] | None = None,
        current_phase: str = "",
        current_task: str = "",
        next_action: str = "",
        auto_extract: bool = True,
        user: str = "",
        author: str = "",
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
            project: Project identifier for saving decisions.
            decisions: List of decisions made (will be saved as knowledge).
            blockers: List of current blockers or issues.
            current_phase: Update the current phase (e.g., "Phase 2").
            current_task: Update the current task (e.g., "T01: Implement feature").
            next_action: What to do next (saved for session continuity).
            auto_extract: If True, use Cognilens to extract essence from long summaries.
            user: User identifier for multi-user support (empty for default user).
            author: Context author/role partition to save under. Use the same
                author you intend to resume() with. Empty saves to the default
                context. Call list_context_authors to reuse an existing author
                name instead of introducing a naming-variation duplicate.

        Returns:
            Dict containing:
            - success: Whether the checkpoint was saved
            - saved_to: List of storage locations used
            - knowledge_added: Number of knowledge entries created
            - message: Status message
        """
        if _settings is None:
            raise RuntimeError("Settings not initialized")

        # Auto-detect user if not specified
        effective_user = user or get_current_user()

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
            user=effective_user,
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
            if current_phase:
                save_args["current_phase"] = current_phase
            if current_task:
                save_args["current_task"] = current_task
            if next_action:
                save_args["next_action"] = next_action
            if project:
                save_args["project"] = project
            save_args["user"] = effective_user
            if author:
                save_args["author"] = author

            await prismind.save_session(**save_args)
            saved_to.append("session")
            logger.info("Session state saved")
        except Exception as e:
            logger.error("Failed to save session", error=str(e))
            errors.append(f"Session save failed: {e}")

        # Step 3: Save decisions as knowledge
        if decisions:
            if not project:
                logger.warning("No project specified, decisions will not be saved")
            else:
                decision_tags = ["checkpoint", "decision"]
                if author:
                    decision_tags.append(f"author:{author}")
                for decision in decisions:
                    try:
                        await prismind.add_knowledge(
                            content=decision,
                            category="decision",
                            project=project,
                            tags=decision_tags,
                            user=effective_user,
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
        project: str = "",
        summary: str = "",
        notes: str = "",
        blockers: list[str] | None = None,
        save_insights: bool = True,
        user: str = "",
        author: str = "",
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
            project: Project identifier for saving insights and session state.
            summary: Summary of work done in this session.
            notes: Additional notes or context to pass to the next session.
            blockers: List of blockers that need resolution.
            save_insights: If True, extract and save session insights as knowledge.
            user: User identifier for multi-user support (empty for default user).
            author: Context author/role partition to hand off. The next session
                restores it via resume(author=...). Empty uses the default
                context.

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

        # Auto-detect user if not specified
        effective_user = user or get_current_user()

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
            user=effective_user,
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
            if summary:
                end_args["summary"] = summary
            if blockers:
                end_args["blockers"] = blockers
            if project:
                end_args["project"] = project
            end_args["user"] = effective_user
            if author:
                end_args["author"] = author

            session_result = await prismind.end_session(**end_args)
            session_data = _parse_result(session_result)
            saved_to.append("session")
            logger.info("Session ended", project=project)
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
            if not project:
                logger.warning("No project specified, insights will not be saved")
            else:
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
                        insight_tags = ["handoff", "insight"]
                        if author:
                            insight_tags.append(f"author:{author}")
                        for concept in key_concepts[:5]:  # Limit to 5 insights
                            try:
                                await prismind.add_knowledge(
                                    content=concept,
                                    category="session_insight",
                                    project=project,
                                    tags=insight_tags,
                                    user=effective_user,
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
        user: str = "",
        author: str = "",
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
            user: User identifier for multi-user support (empty for default user).
            author: Context author/role partition to resume. Use the same author
                the context was checkpoint()/handoff()'d under. Empty resumes
                the default context. Call list_context_authors to see which
                authors have saved context for this project.

        Returns:
            Same structure as begin_task.
        """
        max_tokens = DETAIL_LEVEL_TOKENS.get(detail_level, DETAIL_LEVEL_TOKENS["standard"])

        logger.info(
            "Resuming project",
            project=project,
            detail_level=detail_level,
            max_tokens=max_tokens,
            user=user or "default",
            author=author or "default",
        )

        # Delegate to internal implementation
        return await _begin_task_impl(
            project=project,
            task_description=task_description,
            max_tokens=max_tokens,
            user=user,
            author=author,
        )

    @mcp.tool()
    async def update_progress(
        project: str = "",
        current_phase: str = "",
        current_task: str = "",
        completed_task: str = "",
        blockers: list[str] | None = None,
        user: str = "",
        author: str = "",
    ) -> dict[str, Any]:
        """Update progress in the current session.

        USE THIS WHEN: You want to update the current phase/task without saving
        a full checkpoint. Use this for lightweight progress tracking.

        DO NOT USE WHEN:
        - You want to save a summary or notes → use checkpoint
        - You're ending the session → use handoff

        Args:
            project: Project identifier (uses current if empty).
            current_phase: New current phase (e.g., "Phase 2").
            current_task: New current task (e.g., "T01: Implement feature").
            completed_task: Task that was just completed.
            blockers: Updated list of blockers.
            user: User identifier for multi-user support (empty for default user).
            author: Context author/role partition to update (empty for the
                default context).

        Returns:
            Dict containing:
            - success: Whether the update was saved
            - saved_to: List of storage locations used
            - message: Status message
        """
        if _settings is None:
            raise RuntimeError("Settings not initialized")

        # Auto-detect user if not specified
        effective_user = user or get_current_user()

        prismind = PrismindAdapter(
            sse_url=_settings.prismind_url,
            timeout=_settings.prismind_timeout,
        )

        logger.info(
            "Updating progress",
            project=project,
            current_phase=current_phase,
            current_task=current_task,
            completed_task=completed_task,
            user=effective_user,
        )

        try:
            result = await prismind.update_progress(
                current_phase=current_phase,
                current_task=current_task,
                completed_task=completed_task,
                blockers=blockers,
                project=project,
                user=effective_user,
                author=author,
            )

            saved_to = result.get("saved_to", [])
            message = result.get("message", "Progress updated successfully")

            return {
                "success": True,
                "saved_to": saved_to,
                "message": message,
            }
        except Exception as e:
            logger.error("Failed to update progress", error=str(e))
            return {
                "success": False,
                "saved_to": [],
                "message": f"Failed to update progress: {e}",
            }

    @mcp.tool()
    async def list_context_authors(
        project: str,
        user: str = "",
    ) -> dict[str, Any]:
        """List the context authors/roles that have saved context for a project.

        USE THIS WHEN: Before checkpoint/handoff/resume with an `author`, to:
        - Reuse an existing author name instead of creating a near-duplicate
          from a naming variation (e.g. "claude-code" vs "claude_code").
        - Check whether your own role's context has already been saved.

        Each project+user can hold multiple independent contexts, one per
        author. An empty author ("") is the default/legacy context.

        Args:
            project: Project identifier to inspect.
            user: Optional user filter (empty = all users on the project).

        Returns:
            Dict containing:
            - success: Whether the lookup succeeded
            - project: Project identifier
            - authors: List of {author, user, current_phase, current_task,
              updated_at}, most-recently-updated first
            - total_count: Number of distinct authors
            - message: Status message
        """
        if _settings is None:
            raise RuntimeError("Settings not initialized")

        prismind = PrismindAdapter(
            sse_url=_settings.prismind_url,
            timeout=_settings.prismind_timeout,
        )

        logger.info("Listing context authors", project=project, user=user or "all")

        try:
            result = await prismind.list_context_authors(project=project, user=user)
        except Exception as e:
            logger.error("Failed to list context authors", error=str(e))
            return {
                "success": False,
                "project": project,
                "authors": [],
                "total_count": 0,
                "message": f"Failed to list context authors: {e}",
            }

        authors = result.get("authors", []) if isinstance(result, dict) else []
        return {
            "success": result.get("success", True) if isinstance(result, dict) else True,
            "project": project,
            "authors": authors,
            "total_count": result.get("total_count", len(authors)) if isinstance(result, dict) else len(authors),
            "message": result.get("message", "") if isinstance(result, dict) else "",
        }


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
