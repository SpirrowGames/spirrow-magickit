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

# Optional caller-supplied fields on ``checkpoint`` that participate in the
# truthiness gate.  Listed explicitly so the D1 receipt can report each one
# as either forwarded (``fields_written``) or dropped (``fields_skipped``);
# the caller then no longer has to do a read-back to detect a silent drop.
# ``summary`` is not listed because it is always attempted; ``project`` /
# ``user`` / ``author`` are not listed because they route the write, not
# session state itself.  See chatroom T-checkpoint-silent-partial-write
# msg-262 §5 / msg-264 §5 for the specification.
_CHECKPOINT_OPTIONAL_FIELDS: tuple[str, ...] = (
    "blockers",
    "current_phase",
    "current_task",
    "next_action",
    "embodiment",
)


async def _begin_task_impl(
    project: str,
    task_description: str = "",
    max_tokens: int = 2000,
    user: str = "",
    author: str = "",
    embodiment: str = "",
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
            project=project, user=effective_user, author=author,
            embodiment=embodiment if embodiment else None,
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
        embodiment: str = "",
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
        return await _begin_task_impl(
            project, task_description, max_tokens, user, author, embodiment,
        )

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
        embodiment: str = "",
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
            embodiment: ADR-2026-05-29-12 self-declared runtime form
                (web_ai_chat / terminal_coding_agent / unknown). Forwarded to
                Prismind SessionState as meta. Empty skips the declaration.

        Returns:
            Dict containing:
            - success: Whether the checkpoint was saved.  ``True`` only when
              the downstream session store confirmed persistence (see
              ``persisted``).  Knowledge/decision failures do not flip this
              flag on their own; they surface via ``message``.
            - saved_to: List of storage locations that actually persisted.
              ``"session"`` appears only when ``persisted`` is ``True``;
              ``"knowledge"`` appears when at least one decision was saved.
            - knowledge_added: Number of knowledge entries created.
            - message: Status message; distinguishes an empty-answer from
              a no-answer response, and -- on every branch, the failure
              branches included -- reports any decisions this call did not
              save (msg-323 §2, R5).
            - fields_written: The optional session fields this call
              actually forwarded to Prismind (chatroom
              T-checkpoint-silent-partial-write msg-262 §2 / msg-264 §5).
            - fields_skipped: The optional session fields that were *not*
              forwarded.  Two different things land here and they cannot
              be told apart from inside this function: a field the caller
              passed as an explicit falsy value, and a field the caller
              never passed at all.  The MCP schema defaults (``""`` for
              the four string fields, ``None`` for ``blockers``) collapse
              that distinction at the call boundary, before this function is
              entered, so the server is not able to report which one
              happened -- the indistinguishability is the subject of this
              thread, not a gap in the receipt (msg-323 §3).  How to read
              it: if a field you meant to send is in ``fields_skipped``,
              it did not arrive.
            - persisted: ``True`` if the downstream response reported a
              non-empty ``saved_to``, ``False`` if it reported an empty
              ``saved_to``, ``None`` if the response did not include the
              key (or was not shaped as expected).  ``None`` means "no
              answer from the store", not "the store answered no".
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
        # R5(b) (msg-323 §2): decision-write failures are accumulated in
        # their own list, kept apart from the session-write failure (which
        # travels in ``save_error`` below).  One shared list would feed the
        # session failure into the decision postfix, where it would print a
        # second time and could push the decision failures out of the
        # truncation window.
        decision_errors: list[str] = []

        logger.info(
            "Creating checkpoint",
            summary_length=len(summary),
            decisions_count=len(decisions) if decisions else 0,
            user=effective_user,
            embodiment=embodiment or None,
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
        #
        # D1 receipt (chatroom T-checkpoint-silent-partial-write msg-262 §5 /
        # msg-264 §5): the truthiness gate below is *retained* under D1 —
        # inverting it belongs to D2 and requires the caller enumeration
        # (M2) plus the ``embodiment`` measurement (M1) to avoid turning
        # today's silent no-op into a silent destructive overwrite (msg-262
        # §1.1).  What D1 changes is the *report*: for every optional
        # session field the caller could have set, we say whether it was
        # forwarded or dropped, and we no longer claim that the write
        # persisted without the downstream store saying so.
        persisted: bool | None = None
        save_error: str | None = None
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
        if embodiment:
            save_args["embodiment"] = embodiment

        fields_written = [f for f in _CHECKPOINT_OPTIONAL_FIELDS if f in save_args]
        fields_skipped = [f for f in _CHECKPOINT_OPTIONAL_FIELDS if f not in save_args]

        try:
            save_result = await prismind.save_session(**save_args)
        except Exception as e:
            logger.error("Failed to save session", error=str(e))
            # The session failure travels in ``save_error`` alone; the
            # message branch below is its single point of report, so
            # accumulating it a second time would only duplicate it.
            save_error = str(e)
            save_result = None

        # R1/R2 (msg-264 §5): transcribe the downstream ``saved_to`` rather
        # than synthesising one from "no exception was raised".  R2 gives us
        # three states; keep all three so the message can tell the caller
        # whether the store answered "empty" or did not answer at all.
        if save_error is not None:
            persisted = None
        elif isinstance(save_result, dict):
            downstream_saved_to = save_result.get("saved_to")
            if isinstance(downstream_saved_to, list):
                # R6 (msg-323 §4 / msg-264 §2.3 L1 / msg-268): do NOT tighten
                # this to ``"session" in downstream_saved_to``.  The element
                # names inside ``saved_to`` are the downstream store's own
                # internal vocabulary, not our contract; narrowing by name
                # makes this check go quietly false the day that store
                # renames a target.  Non-empty means "something was
                # persisted", and that is the whole of what we may read
                # from it.
                persisted = len(downstream_saved_to) > 0
            else:
                persisted = None
        else:
            persisted = None

        if persisted is True:
            saved_to.append("session")
            logger.info("Session state saved")
        elif persisted is False:
            # Downstream answered, and it said "nothing was persisted".
            logger.warning("Session save returned an empty saved_to")
        elif save_error is None:
            # Downstream did not answer the persistence question at all.
            logger.warning("Session save response did not include saved_to")

        # Step 3: Save decisions as knowledge
        if decisions:
            if not project:
                # R5b (msg-323 §2): with no project the decisions are dropped
                # right here and never reach ``add_knowledge``.  Until this
                # line the drop was visible only in the server log -- the
                # caller saw success=True, knowledge_added=0 and "Checkpoint
                # saved successfully", which is this thread's defect wearing
                # a different hat.  Count them as unsaved so the message
                # below is obliged to say so.
                logger.warning("No project specified, decisions will not be saved")
                decision_errors.append(
                    f"{len(decisions)} decision(s) not saved: no project specified"
                )
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
                        decision_errors.append(
                            f"Decision save failed: {decision[:30]}..."
                        )

                if knowledge_added > 0:
                    saved_to.append("knowledge")
                    logger.info("Decisions saved as knowledge", count=knowledge_added)

        # R3 (msg-264 §5): ``success`` is fail-closed on persistence.  The
        # necessary condition for ``success=True`` is that the downstream
        # session store confirmed the write (``persisted is True``); an
        # empty answer or no answer is treated as "not saved".  Knowledge
        # failures continue to surface only via ``message`` — this
        # obligation is about session state, not about decision writes.
        success = persisted is True

        # R4 (msg-264 §5): the persisted-negative and persisted-null cases
        # must be spelled apart in the message.  A downstream store that
        # answered "I saved nothing" is different from a downstream store
        # that never told us either way, and conflating them re-opens the
        # very "answered without saying anything" defect this change closes.
        if save_error is not None:
            message = f"Checkpoint not saved: session write failed ({save_error})"
        elif persisted is True:
            message = "Checkpoint saved successfully"
        elif persisted is False:
            message = (
                "Checkpoint not saved: downstream session store answered "
                "with an empty saved_to (nothing was persisted)."
            )
        else:
            message = (
                "Checkpoint not saved: downstream session store did not "
                "confirm persistence (saved_to missing from response)."
            )

        # R5 (msg-323 §2): the fate of the decisions the caller handed us is
        # reported here, once, for every branch above.  The rule the message
        # has to satisfy is not "join the error list" but: if any decision
        # the caller passed was not saved, say so.  Doing the append in a
        # single place is the substance of the fix -- a copy of it inside
        # each branch is how the hole the PR gate found was opened, and it
        # would re-open the next time a branch is added.  ``success`` is not
        # touched here: INV-D1-SUCCESS (msg-267 §1) keeps it a statement
        # about session state alone.
        if decision_errors:
            message = (
                f"{message} [{len(decision_errors)} decision warning(s): "
                f"{'; '.join(decision_errors[:2])}]"
            )

        return {
            "success": success,
            "saved_to": saved_to,
            "knowledge_added": knowledge_added,
            "message": message,
            "fields_written": fields_written,
            "fields_skipped": fields_skipped,
            "persisted": persisted,
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
        embodiment: str = "",
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
            embodiment: ADR-2026-05-29-12 self-declared runtime form
                (web_ai_chat / terminal_coding_agent / unknown). Forwarded to
                Prismind SessionState. Empty skips the declaration.

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
            embodiment=embodiment or None,
        )

        # Delegate to internal implementation
        return await _begin_task_impl(
            project=project,
            task_description=task_description,
            max_tokens=max_tokens,
            user=user,
            author=author,
            embodiment=embodiment,
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
        # Pass authors through verbatim so the upstream-attached 'identity' field
        # (allowed_roles / default_role / display_name from the cross-project
        # identity record) reaches the caller without re-encoding.
        return {
            "success": result.get("success", True) if isinstance(result, dict) else True,
            "project": project,
            "authors": authors,
            "total_count": result.get("total_count", len(authors)) if isinstance(result, dict) else len(authors),
            "message": result.get("message", "") if isinstance(result, dict) else "",
        }

    @mcp.tool()
    async def upsert_identity(
        identity_name: str,
        independence_class: str,
        allowed_roles: list[str] | None = None,
        keep_allowed_roles: bool = False,
        persona_description: str = "",
        embodiment: str = "",
        user: str = "",
    ) -> dict[str, Any]:
        """Create or update a cross-project identity record (actor declaration).

        USE THIS WHEN: declaring or updating an AI role's stable identity
        (the same name used as the `author` argument on checkpoint / handoff /
        resume), so allowed_roles / independence_class / persona_description
        persist across projects without being redeclared on every save.

        Identity records live in a separate key space from session state
        (`prismind:identity:{user}:{identity_name}`), and the schema realizes
        ADR-2026-05-27-09 D-3's persona-continuity gate at the API level:

        - `independence_class` is "main-chain" / "independent" / "human" --
          required on every upsert ("書き忘れ不能" guarantee, msg-001 §C-4).
        - `allowed_roles` is the role enforcement list -- required unless
          `keep_allowed_roles=True`.
        - `persona_description` is optional human-readable note --
          preserve-on-empty.
        - `embodiment` is **DEPRECATED** by ADR-2026-05-29-12. Runtime form
          is now self-declared on the five APIs (checkpoint / resume /
          chatroom_*) rather than fixed on the identity record. Pass omit
          or empty; the field is preserved in the schema only for the
          staged migration window.

        Field semantics:
        - `independence_class`: required on every call. Invalid enum
          values are rejected upstream.
        - `allowed_roles=None` (omitted): you must also set
          `keep_allowed_roles=True`, or the call fails. Pass `[]` to
          explicitly declare "no allowed roles" -- this is a legal but
          unusual state, distinct from "preserve existing". Once role
          checks are live (P2), every role-bearing post from such an
          actor will be rejected.
        - `keep_allowed_roles=True`: only valid on update (existing record).
          Conflicts with passing `allowed_roles`.
        - `persona_description=""` (empty): preserve existing value. Pass a
          non-empty string to update.
        - `embodiment=""` (empty): DEPRECATED; treated as absent.

        Args:
            identity_name: Stable identity slug (e.g. "Heisenberg"). Same
                value used as `author` on checkpoint/handoff/resume.
            independence_class: "main-chain" / "independent" / "human".
                Required.
            allowed_roles: Roles this identity is allowed to assume (e.g.
                ["proposer", "reviewer"]). Required unless
                keep_allowed_roles=True. Magickit is the enforcement point;
                Prismind only persists.
            keep_allowed_roles: Preserve existing allowed_roles list. Only
                valid on update.
            persona_description: Optional human-readable persona note.
                Empty preserves existing.
            embodiment: DEPRECATED. Pass empty.
            user: Owning user (empty defers to upstream's configured user).

        Returns:
            Dict containing:
            - success: Whether the upsert succeeded
            - identity: The persisted identity record (or null on failure)
            - created: True if a new record was written, False on update
            - message: Status message
        """
        if _settings is None:
            raise RuntimeError("Settings not initialized")

        if not identity_name:
            return {
                "success": False,
                "identity": None,
                "created": False,
                "message": "identity_name is required",
            }

        prismind = PrismindAdapter(
            sse_url=_settings.prismind_url,
            timeout=_settings.prismind_timeout,
        )

        logger.info(
            "Upserting identity",
            identity_name=identity_name,
            independence_class=independence_class,
            allowed_roles=allowed_roles,
            keep_allowed_roles=keep_allowed_roles,
        )

        try:
            result = await prismind.upsert_identity(
                identity_name=identity_name,
                independence_class=independence_class,
                allowed_roles=allowed_roles,
                keep_allowed_roles=keep_allowed_roles,
                persona_description=persona_description if persona_description else None,
                embodiment=embodiment if embodiment else None,
                user=user,
            )
        except Exception as e:
            logger.error("Failed to upsert identity", error=str(e))
            return {
                "success": False,
                "identity": None,
                "created": False,
                "message": f"Failed to upsert identity: {e}",
            }

        if not isinstance(result, dict):
            return {
                "success": False,
                "identity": None,
                "created": False,
                "message": "Unexpected response from Prismind",
            }

        # Upstream MCP-level rejection (e.g. FastMCP schema enum violation):
        # `mcp_base.call_tool` now returns the structured envelope
        # `{"error_type": "...", "error": <text>, "details": {...}}`. Surface
        # both the human-readable text (as `message`) and the structured
        # `error_type` / `details` so callers can branch on the error class
        # without parsing the text. Envelope shape from msg-010 D-9.
        if "error_type" in result:
            return {
                "success": False,
                "identity": None,
                "created": False,
                "message": result.get("error", "Upstream MCP error"),
                "error_type": result["error_type"],
                "details": result.get("details", {}),
            }

        return {
            "success": result.get("success", False),
            "identity": result.get("identity"),
            "created": result.get("created", False),
            "message": result.get("message", ""),
        }

    @mcp.tool()
    async def get_identity(
        identity_name: str,
        user: str = "",
    ) -> dict[str, Any]:
        """Read one cross-project identity record. Read-only diagnostic.

        Returns the record exactly as Prismind sent it, and does NOT apply the
        gate's folding: a response the gate would reduce to "unusable" is
        reported here as `contract_violation` with the raw payload, so the
        tool can explain gate behaviour instead of reproducing its blind spot.

        Args:
            identity_name: Stable identity slug (e.g. "Heisenberg").
            user: Owning user. Empty resolves the same way the gate and
                `upsert_identity` do (upstream's configured user).

        Returns:
            `{status, identity_name, user_partition, ...}` where status is
            `found` / `not_found` / `lookup_failed` / `contract_violation`.
        """
        if _settings is None:
            raise RuntimeError("Settings not initialized")

        # Every envelope carries the partition, because "not_found" is
        # uninterpretable without knowing which partition was asked: an
        # unregistered identity and a partition mismatch are the same answer
        # otherwise. `resolved` is never invented -- when the caller defers to
        # upstream's configured user, Magickit does not know that value, and
        # the only honest place it can come from is the record itself.
        def _envelope(
            status: str, record: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            resolved: Any = user if user else None
            source = "argument" if user else "upstream_default"
            if not user and isinstance(record, dict) and "user" in record:
                resolved = record["user"]
                source = "record"
            return {
                "status": status,
                "identity_name": identity_name,
                "user_partition": {
                    "requested": user,
                    "resolved": resolved,
                    "source": source,
                },
            }

        if not identity_name:
            return {
                **_envelope("lookup_failed"),
                "error": "identity_name is required",
            }

        prismind = PrismindAdapter(
            sse_url=_settings.prismind_url,
            timeout=_settings.prismind_timeout,
        )

        logger.info("Reading identity", identity_name=identity_name, user=user or "")

        try:
            result = await prismind.get_identity(identity_name=identity_name, user=user)
        except Exception as e:
            logger.warning(
                "Identity read failed", identity_name=identity_name, error=str(e)
            )
            return {
                **_envelope("lookup_failed"),
                "error": f"{type(e).__name__}: {e}",
            }

        if not isinstance(result, dict):
            return {
                **_envelope("lookup_failed"),
                "error": "unexpected response from Prismind",
                "raw": result,
            }
        if "error_type" in result:
            return {
                **_envelope("lookup_failed"),
                "error": f"{result['error_type']}: {result.get('error', '')}",
                "raw": result,
            }
        if result.get("success") is not True:
            return {
                **_envelope("lookup_failed"),
                "error": result.get("message") or "lookup did not report success",
                "raw": result,
            }

        violations: list[str] = []

        # isinstance, not truthiness: JSON null / 0 / "" are all falsy and
        # would each be read as a confirmed "not registered". That default is
        # the reachable fail-open of msg-044 6.4, and a tool built to diagnose
        # it must not repeat it.
        found = result.get("found")
        if not isinstance(found, bool):
            violations.append(
                "found: expected bool, got "
                + ("<missing>" if "found" not in result else repr(found))
            )
            return {
                **_envelope("contract_violation"),
                "violations": violations,
                "raw": result,
            }

        if not found:
            return _envelope("not_found")

        identity = result.get("identity")
        if not isinstance(identity, dict):
            # No manufactured allowed_roles=[]: a record that never arrived
            # must not be reported as one that granted nothing.
            violations.append(
                "identity: expected dict on found=true, got "
                + ("<missing>" if "identity" not in result else repr(identity))
            )
            return {
                **_envelope("contract_violation"),
                "violations": violations,
                "raw": result,
            }

        # Reported, never coerced. tuple("naysayer") becomes ('n','a',...)
        # and tuple(True) raises; both are how the value the gates compare
        # against stops meaning what the record said. [] is a legal record
        # value ("no allowed roles"), not a violation.
        if "allowed_roles" in identity:
            allowed_roles = identity["allowed_roles"]
            if not isinstance(allowed_roles, list):
                violations.append(
                    f"identity.allowed_roles: expected list, got {allowed_roles!r}"
                )
            elif not all(isinstance(item, str) for item in allowed_roles):
                violations.append(
                    "identity.allowed_roles: expected list[str], got "
                    f"{allowed_roles!r}"
                )

        if violations:
            return {
                **_envelope("contract_violation", identity),
                "violations": violations,
                "raw": result,
            }

        # `identity` is passed through unchanged, and `present_keys` states
        # which keys were actually on it. The ADR-2026-05-29-12 (iii)
        # migration turns on telling an absent `embodiment` key apart from a
        # null one -- omitted means Prismind dropped the field, null means it
        # kept it and cleared it -- so the distinction is reported explicitly
        # and cannot be lost to a serializer that elides nulls.
        return {
            **_envelope("found", identity),
            "identity": identity,
            "present_keys": sorted(identity),
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
