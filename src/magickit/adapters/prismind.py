"""Adapter for Prismind knowledge management MCP service."""

import json
from typing import Any, TypedDict

from pydantic import BaseModel

from magickit.adapters.mcp_base import MCPBaseAdapter
from magickit.utils.logging import get_logger

logger = get_logger(__name__)


# === Type Definitions ===


class SetupProjectResult(TypedDict, total=False):
    """Result from setup_project operation."""

    success: bool
    project_id: str
    name: str
    root_folder_id: str  # Google Drive folder ID (unique project identifier)
    spreadsheet_id: str
    message: str


class DocumentTypeInfo(TypedDict, total=False):
    """Document type information."""

    type_id: str
    name: str
    folder_name: str
    scope: str  # "global" or "project"
    description: str


class Document(BaseModel):
    """Document model from knowledge search."""

    id: str
    content: str
    metadata: dict[str, Any] = {}
    score: float = 0.0


class PrismindAdapter(MCPBaseAdapter):
    """Adapter for Prismind MCP service.

    Provides methods for knowledge management, document operations,
    and project management via MCP tool calls.
    """

    async def health_check(self) -> bool:
        """Check if Prismind service is healthy.

        Returns:
            True if the service is healthy, False otherwise.
        """
        try:
            tools = await self.list_tools()
            # Check if expected tools are available
            expected = {"search_knowledge", "add_knowledge", "list_projects"}
            return expected.issubset(set(tools))
        except Exception as e:
            logger.warning("Prismind health check failed", error=str(e))
            return False

    async def search(
        self,
        query: str,
        n: int = 5,
        filter_metadata: dict[str, Any] | None = None,
    ) -> list[Document]:
        """Search for relevant knowledge.

        Args:
            query: Search query.
            n: Number of results to return.
            filter_metadata: Optional metadata filter (category, tags, etc.).

        Returns:
            List of relevant documents.
        """
        arguments: dict[str, Any] = {
            "query": query,
            "limit": n,
        }
        if filter_metadata:
            if "category" in filter_metadata:
                arguments["category"] = filter_metadata["category"]
            if "tags" in filter_metadata:
                arguments["tags"] = filter_metadata["tags"]
            if "project" in filter_metadata:
                arguments["project"] = filter_metadata["project"]

        logger.info("Searching knowledge via MCP", query_length=len(query), n=n)

        success, result = await self._call_tool_safe("search_knowledge", arguments)
        if not success:
            raise RuntimeError(f"Search failed: {result}")

        return self._parse_documents(result)

    async def index(
        self,
        documents: list[dict[str, Any]],
        collection: str = "default",
    ) -> dict[str, Any]:
        """Index documents (add knowledge).

        Args:
            documents: Documents to index. Each should have 'content' and optionally 'metadata'.
            collection: Collection/category name.

        Returns:
            Indexing result with count and status.
        """
        results = []
        for doc in documents:
            arguments: dict[str, Any] = {
                "content": doc.get("content", ""),
                "category": collection,
            }
            if "metadata" in doc:
                if "tags" in doc["metadata"]:
                    arguments["tags"] = doc["metadata"]["tags"]
                if "source" in doc["metadata"]:
                    arguments["source"] = doc["metadata"]["source"]

            success, result = await self._call_tool_safe("add_knowledge", arguments)
            results.append({"success": success, "result": result})

        logger.info(
            "Indexed documents via MCP",
            count=len(documents),
            collection=collection,
        )

        return {
            "indexed": len([r for r in results if r["success"]]),
            "failed": len([r for r in results if not r["success"]]),
            "details": results,
        }

    async def get_context(
        self,
        query: str,
        max_tokens: int = 2000,
    ) -> str:
        """Get relevant context for a query.

        Args:
            query: Query to get context for.
            max_tokens: Maximum tokens to return.

        Returns:
            Concatenated relevant context.
        """
        documents = await self.search(query, n=5)

        # Concatenate document contents
        context_parts = []
        for doc in documents:
            context_parts.append(doc.content)

        context = "\n\n---\n\n".join(context_parts)

        # Truncate if too long (rough estimate: 4 chars per token)
        max_chars = max_tokens * 4
        if len(context) > max_chars:
            context = context[:max_chars] + "..."

        return context

    async def delete_knowledge(
        self,
        knowledge_id: str,
        project: str = "",
        user: str = "",
    ) -> dict[str, Any]:
        """Delete a knowledge entry.

        Args:
            knowledge_id: ID of the knowledge entry to delete.
            project: Project ID for validation (optional).
            user: User identifier for multi-user support.

        Returns:
            Dict with success status and message.
        """
        arguments: dict[str, Any] = {"knowledge_id": knowledge_id}
        if project:
            arguments["project"] = project
        if user:
            arguments["user"] = user

        logger.info(
            "Deleting knowledge via MCP",
            knowledge_id=knowledge_id,
            project=project,
        )

        success, result = await self._call_tool_safe("delete_knowledge", arguments)
        if not success:
            raise RuntimeError(f"delete_knowledge failed: {result}")

        return self._parse_json_result(result)

    # === Helper methods ===

    def _parse_documents(self, result: Any) -> list[Document]:
        """Parse search result to Document list."""
        if result is None:
            return []

        data = self._parse_list_result(result)
        documents = []

        for item in data:
            if isinstance(item, dict):
                documents.append(
                    Document(
                        id=item.get("id", item.get("knowledge_id", "")),
                        content=item.get("content", ""),
                        metadata={
                            "category": item.get("category", ""),
                            "tags": item.get("tags", []),
                            "source": item.get("source", ""),
                        },
                        score=item.get("score", item.get("similarity", 0.0)),
                    )
                )

        return documents

    def _parse_json_result(self, result: Any) -> dict[str, Any]:
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

    def _parse_list_result(self, result: Any) -> list[dict[str, Any]]:
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
                    # Try common list keys
                    for key in ["results", "items", "documents", "knowledge", "projects"]:
                        if key in data and isinstance(data[key], list):
                            return data[key]
                    return [data]
                return [{"result": data}]
            except json.JSONDecodeError:
                return [{"result": result}]
        return [{"result": result}]

    # === Document Type Methods ===

    async def find_similar_document_type(
        self,
        type_query: str,
        threshold: float = 0.75,
        user: str = "",
    ) -> dict[str, Any]:
        """Find a document type semantically similar to the query.

        Uses RAG-based semantic search (BGE-M3 embeddings) for multilingual
        matching. For example, "api仕様" can match "api_spec".

        Args:
            type_query: Search query (type name, ID, or description)
            threshold: Minimum similarity score (0.0-1.0)
            user: User identifier for multi-user support

        Returns:
            Dict containing:
            - found: Whether a match was found
            - type_id: Matched type ID (if found)
            - name: Matched type name (if found)
            - folder_name: Matched type folder name (if found)
            - similarity: Similarity score (if found)
            - message: Status message
        """
        logger.info(
            "Finding similar document type",
            type_query=type_query,
            threshold=threshold,
        )

        arguments: dict[str, Any] = {"type_query": type_query, "threshold": threshold}
        if user:
            arguments["user"] = user

        success, result = await self._call_tool_safe(
            "find_similar_document_type",
            arguments,
        )

        if not success:
            logger.warning(
                "find_similar_document_type failed, returning not found",
                error=result,
            )
            return {
                "found": False,
                "type_id": "",
                "name": "",
                "folder_name": "",
                "similarity": 0.0,
                "message": f"Search failed: {result}",
            }

        return self._parse_json_result(result)

    # === Task Management Methods ===

    async def get_progress(
        self,
        project: str = "",
        phase: str = "",
        user: str = "",
    ) -> dict[str, Any]:
        """Get project progress with tasks.

        Args:
            project: Project ID (empty for current project)
            phase: Filter by specific phase (empty for all)
            user: User identifier for multi-user support

        Returns:
            Dict containing phases and tasks
        """
        arguments: dict[str, Any] = {}
        if user:
            arguments["user"] = user
        if project:
            arguments["project"] = project
        if phase:
            arguments["phase"] = phase

        logger.info("Getting progress via MCP", project=project, phase=phase)

        success, result = await self._call_tool_safe("get_progress", arguments)
        if not success:
            raise RuntimeError(f"get_progress failed: {result}")

        return self._parse_json_result(result)

    async def add_task(
        self,
        phase: str,
        task_id: str,
        name: str,
        description: str = "",
        project: str = "",
        priority: str = "medium",
        category: str = "",
        blocked_by: list[str] | None = None,
        user: str = "",
    ) -> dict[str, Any]:
        """Add a new task.

        Args:
            phase: Phase name (e.g., "Phase 2")
            task_id: Task ID (e.g., "T01")
            user: User identifier for multi-user support
            name: Task name
            description: Task description
            project: Project ID (empty for current)
            priority: Priority level (high/medium/low)
            category: Task category (bug/feature/refactor/design/test)
            blocked_by: List of task IDs this task depends on

        Returns:
            Dict with success status and message
        """
        arguments: dict[str, Any] = {
            "phase": phase,
            "task_id": task_id,
            "name": name,
        }
        if description:
            arguments["description"] = description
        if project:
            arguments["project"] = project
        if priority and priority != "medium":
            arguments["priority"] = priority
        if category:
            arguments["category"] = category
        if blocked_by:
            arguments["blocked_by"] = blocked_by
        if user:
            arguments["user"] = user

        logger.info(
            "Adding task via MCP",
            task_id=task_id,
            name=name,
            phase=phase,
            user=user,
        )

        success, result = await self._call_tool_safe("add_task", arguments)
        if not success:
            raise RuntimeError(f"add_task failed: {result}")

        return self._parse_json_result(result)

    async def start_task(
        self,
        task_id: str,
        phase: str = "",
        project: str = "",
        notes: str = "",
        user: str = "",
    ) -> dict[str, Any]:
        """Start a task (set status to in_progress).

        Args:
            task_id: Task ID
            phase: Phase name (required if task_id is ambiguous across phases)
            project: Project ID (empty for current)
            notes: Optional notes
            user: User identifier for multi-user support

        Returns:
            Dict with success status and message
        """
        arguments: dict[str, Any] = {"task_id": task_id}
        if phase:
            arguments["phase"] = phase
        if project:
            arguments["project"] = project
        if notes:
            arguments["notes"] = notes
        if user:
            arguments["user"] = user

        logger.info("Starting task via MCP", task_id=task_id)

        success, result = await self._call_tool_safe("start_task", arguments)
        if not success:
            raise RuntimeError(f"start_task failed: {result}")

        return self._parse_json_result(result)

    async def complete_task(
        self,
        task_id: str,
        phase: str = "",
        project: str = "",
        notes: str = "",
        user: str = "",
    ) -> dict[str, Any]:
        """Complete a task (set status to completed).

        Args:
            task_id: Task ID
            phase: Phase name (required if task_id is ambiguous across phases)
            project: Project ID (empty for current)
            notes: Completion notes
            user: User identifier for multi-user support

        Returns:
            Dict with success status and message
        """
        arguments: dict[str, Any] = {"task_id": task_id}
        if phase:
            arguments["phase"] = phase
        if project:
            arguments["project"] = project
        if notes:
            arguments["notes"] = notes
        if user:
            arguments["user"] = user

        logger.info("Completing task via MCP", task_id=task_id)

        success, result = await self._call_tool_safe("complete_task", arguments)
        if not success:
            raise RuntimeError(f"complete_task failed: {result}")

        return self._parse_json_result(result)

    async def block_task(
        self,
        task_id: str,
        reason: str,
        phase: str = "",
        project: str = "",
        user: str = "",
    ) -> dict[str, Any]:
        """Block a task with a reason.

        Args:
            task_id: Task ID
            reason: Reason for blocking
            phase: Phase name (required if task_id is ambiguous across phases)
            project: Project ID (empty for current)
            user: User identifier for multi-user support

        Returns:
            Dict with success status and message
        """
        arguments: dict[str, Any] = {
            "task_id": task_id,
            "reason": reason,
        }
        if phase:
            arguments["phase"] = phase
        if project:
            arguments["project"] = project
        if user:
            arguments["user"] = user

        logger.info("Blocking task via MCP", task_id=task_id, reason=reason)

        success, result = await self._call_tool_safe("block_task", arguments)
        if not success:
            raise RuntimeError(f"block_task failed: {result}")

        return self._parse_json_result(result)

    async def update_task_status(
        self,
        task_id: str,
        status: str,
        phase: str = "",
        project: str = "",
        notes: str = "",
        user: str = "",
    ) -> dict[str, Any]:
        """Update task status.

        Args:
            task_id: Task ID
            status: New status (not_started/in_progress/completed/blocked)
            phase: Phase name (required if task_id is ambiguous across phases)
            project: Project ID (empty for current)
            notes: Optional notes
            user: User identifier for multi-user support

        Returns:
            Dict with success status and message
        """
        arguments: dict[str, Any] = {
            "task_id": task_id,
            "status": status,
        }
        if phase:
            arguments["phase"] = phase
        if project:
            arguments["project"] = project
        if notes:
            arguments["notes"] = notes
        if user:
            arguments["user"] = user

        logger.info(
            "Updating task status via MCP",
            task_id=task_id,
            status=status,
        )

        success, result = await self._call_tool_safe("update_task_status", arguments)
        if not success:
            raise RuntimeError(f"update_task_status failed: {result}")

        return self._parse_json_result(result)

    async def search_knowledge(
        self,
        query: str,
        category: str = "",
        project: str = "",
        tags: list[str] | None = None,
        limit: int = 10,
        user: str = "",
    ) -> list[dict[str, Any]]:
        """Search knowledge base.

        Args:
            query: Search query
            category: Filter by category
            project: Filter by project
            tags: Filter by tags
            limit: Maximum results
            user: User identifier for multi-user support

        Returns:
            List of matching knowledge entries
        """
        arguments: dict[str, Any] = {
            "query": query,
            "limit": limit,
        }
        if category:
            arguments["category"] = category
        if project:
            arguments["project"] = project
        if tags:
            arguments["tags"] = tags
        if user:
            arguments["user"] = user

        logger.info("Searching knowledge via MCP", query=query[:50], user=user)

        success, result = await self._call_tool_safe("search_knowledge", arguments)
        if not success:
            raise RuntimeError(f"search_knowledge failed: {result}")

        return self._parse_list_result(result)

    async def add_knowledge(
        self,
        content: str,
        category: str = "",
        project: str = "",
        tags: list[str] | None = None,
        source: str = "",
        user: str = "",
    ) -> dict[str, Any]:
        """Add knowledge entry.

        Args:
            content: Knowledge content
            category: Category
            project: Project ID
            tags: Tags
            source: Source reference
            user: User identifier for multi-user support

        Returns:
            Dict with success status and knowledge_id
        """
        arguments: dict[str, Any] = {"content": content}
        if user:
            arguments["user"] = user
        if category:
            arguments["category"] = category
        if project:
            arguments["project"] = project
        if tags:
            arguments["tags"] = tags
        if source:
            arguments["source"] = source

        logger.info("Adding knowledge via MCP", content_length=len(content))

        success, result = await self._call_tool_safe("add_knowledge", arguments)
        if not success:
            raise RuntimeError(f"add_knowledge failed: {result}")

        return self._parse_json_result(result)

    # === Session management methods ===

    async def start_session(
        self,
        project: str = "",
        user: str = "",
    ) -> dict[str, Any]:
        """Start a session and load saved state.

        Args:
            project: Project ID (empty for current)
            user: User identifier for multi-user support

        Returns:
            Dict with session context (project, current_phase, current_task, etc.)
        """
        arguments: dict[str, Any] = {}
        if project:
            arguments["project"] = project
        if user:
            arguments["user"] = user

        logger.info("Starting session via MCP", project=project, user=user)

        success, result = await self._call_tool_safe("start_session", arguments)
        if not success:
            raise RuntimeError(f"start_session failed: {result}")

        return self._parse_json_result(result)

    async def end_session(
        self,
        summary: str = "",
        next_action: str = "",
        blockers: list[str] | None = None,
        notes: str = "",
        project: str = "",
        user: str = "",
    ) -> dict[str, Any]:
        """End the session and save state.

        Args:
            summary: Work summary for this session
            next_action: Recommended next action for next session
            blockers: List of blockers
            notes: Notes to pass to next session
            project: Project ID (uses current if empty)
            user: User identifier for multi-user support

        Returns:
            Dict with success status and session duration
        """
        arguments: dict[str, Any] = {}
        if summary:
            arguments["summary"] = summary
        if next_action:
            arguments["next_action"] = next_action
        if blockers:
            arguments["blockers"] = blockers
        if notes:
            arguments["notes"] = notes
        if project:
            arguments["project"] = project
        if user:
            arguments["user"] = user

        logger.info("Ending session via MCP", project=project, user=user)

        success, result = await self._call_tool_safe("end_session", arguments)
        if not success:
            raise RuntimeError(f"end_session failed: {result}")

        return self._parse_json_result(result)

    async def save_session(
        self,
        summary: str = "",
        next_action: str = "",
        blockers: list[str] | None = None,
        notes: str = "",
        current_phase: str = "",
        current_task: str = "",
        project: str = "",
        user: str = "",
    ) -> dict[str, Any]:
        """Save session state without ending.

        Args:
            summary: Work summary
            next_action: What to do next
            blockers: List of blockers
            notes: Notes
            current_phase: Update current phase
            current_task: Update current task
            project: Project ID (uses current if empty)
            user: User identifier for multi-user support

        Returns:
            Dict with success status
        """
        arguments: dict[str, Any] = {}
        if summary:
            arguments["summary"] = summary
        if next_action:
            arguments["next_action"] = next_action
        if blockers:
            arguments["blockers"] = blockers
        if notes:
            arguments["notes"] = notes
        if current_phase:
            arguments["current_phase"] = current_phase
        if current_task:
            arguments["current_task"] = current_task
        if project:
            arguments["project"] = project
        if user:
            arguments["user"] = user

        logger.info("Saving session via MCP", project=project, user=user)

        success, result = await self._call_tool_safe("save_session", arguments)
        if not success:
            raise RuntimeError(f"save_session failed: {result}")

        return self._parse_json_result(result)

    async def update_progress(
        self,
        current_phase: str = "",
        current_task: str = "",
        completed_task: str = "",
        blockers: list[str] | None = None,
        project: str = "",
        user: str = "",
    ) -> dict[str, Any]:
        """Update progress in the session.

        Args:
            current_phase: New current phase
            current_task: New current task
            completed_task: Task that was just completed
            blockers: Updated blockers
            project: Project ID (uses current if empty)
            user: User identifier for multi-user support

        Returns:
            Dict with success status
        """
        arguments: dict[str, Any] = {}
        if current_phase:
            arguments["current_phase"] = current_phase
        if current_task:
            arguments["current_task"] = current_task
        if completed_task:
            arguments["completed_task"] = completed_task
        if blockers is not None:
            arguments["blockers"] = blockers
        if project:
            arguments["project"] = project
        if user:
            arguments["user"] = user

        logger.info(
            "Updating progress via MCP",
            project=project,
            current_phase=current_phase,
            current_task=current_task,
        )

        success, result = await self._call_tool_safe("update_progress", arguments)
        if not success:
            raise RuntimeError(f"update_progress failed: {result}")

        return self._parse_json_result(result)

    # === Project Management Methods ===

    async def setup_project(
        self,
        project: str,
        name: str,
        force: bool = False,
        user: str = "",
    ) -> SetupProjectResult:
        """Setup a new project in Prismind.

        Args:
            project: Project identifier.
            name: Display name for the project.
            force: If True, skip similar project check.
            user: User identifier for multi-user support.

        Returns:
            SetupProjectResult containing project info including root_folder_id.
        """
        arguments: dict[str, Any] = {
            "project": project,
            "name": name,
        }
        if force:
            arguments["force"] = force
        if user:
            arguments["user"] = user

        logger.info(
            "Setting up project via MCP",
            project=project,
            name=name,
        )

        success, result = await self._call_tool_safe("setup_project", arguments)
        if not success:
            raise RuntimeError(f"setup_project failed: {result}")

        return self._parse_json_result(result)  # type: ignore[return-value]

    async def update_project(
        self,
        project: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Update project metadata.

        Args:
            project: Project identifier.
            **kwargs: Fields to update (name, categories, phases, template,
                     status, description, project_uid, etc.)

        Returns:
            Dict with success status and message.
        """
        arguments: dict[str, Any] = {"project": project}
        arguments.update(kwargs)

        logger.info(
            "Updating project via MCP",
            project=project,
            fields=list(kwargs.keys()),
        )

        success, result = await self._call_tool_safe("update_project", arguments)
        if not success:
            raise RuntimeError(f"update_project failed: {result}")

        return self._parse_json_result(result)

    async def delete_project(
        self,
        project: str,
        confirm: bool = False,
        delete_drive_folder: bool = False,
        user: str = "",
    ) -> dict[str, Any]:
        """Delete a project.

        Args:
            project: Project identifier.
            confirm: Must be True for permanent deletion.
            delete_drive_folder: If True, also delete Google Drive folder.
            user: User identifier for multi-user support.

        Returns:
            Dict with success status and deletion details.
        """
        arguments: dict[str, Any] = {
            "project": project,
            "confirm": confirm,
        }
        if delete_drive_folder:
            arguments["delete_drive_folder"] = delete_drive_folder
        if user:
            arguments["user"] = user

        logger.info(
            "Deleting project via MCP",
            project=project,
            confirm=confirm,
        )

        success, result = await self._call_tool_safe("delete_project", arguments)
        if not success:
            raise RuntimeError(f"delete_project failed: {result}")

        return self._parse_json_result(result)

    async def list_projects(
        self,
        include_archived: bool = False,
        user: str = "",
    ) -> list[dict[str, Any]]:
        """List all projects.

        Args:
            include_archived: If True, include archived projects.
            user: User identifier for multi-user support.

        Returns:
            List of project info dicts.
        """
        arguments: dict[str, Any] = {}
        if include_archived:
            arguments["include_archived"] = include_archived
        if user:
            arguments["user"] = user

        logger.info(
            "Listing projects via MCP",
            include_archived=include_archived,
        )

        success, result = await self._call_tool_safe("list_projects", arguments)
        if not success:
            raise RuntimeError(f"list_projects failed: {result}")

        return self._parse_list_result(result)

    # === Document Management Methods ===

    async def create_document(
        self,
        doc_type: str,
        name: str,
        content: str,
        phase_task: str,
        project: str = "",
        feature: str = "",
        keywords: list[str] | None = None,
        auto_register_type: bool = False,
        user: str = "",
    ) -> dict[str, Any]:
        """Create a document in Prismind.

        Args:
            doc_type: Document type (e.g., "design", "api_spec").
            name: Document name.
            content: Document content.
            phase_task: Phase-task identifier (e.g., "phase1-task2").
            project: Project identifier.
            feature: Feature name.
            keywords: Search keywords.
            auto_register_type: If True, auto-register unknown type.
            user: User identifier for multi-user support.

        Returns:
            Dict with doc_id, doc_url, and status.
        """
        arguments: dict[str, Any] = {
            "doc_type": doc_type,
            "name": name,
            "content": content,
            "phase_task": phase_task,
        }
        if project:
            arguments["project"] = project
        if feature:
            arguments["feature"] = feature
        if keywords:
            arguments["keywords"] = keywords
        if auto_register_type:
            arguments["auto_register_type"] = auto_register_type
        if user:
            arguments["user"] = user

        logger.info(
            "Creating document via MCP",
            doc_type=doc_type,
            name=name,
            project=project,
        )

        success, result = await self._call_tool_safe("create_document", arguments)
        if not success:
            raise RuntimeError(f"create_document failed: {result}")

        return self._parse_json_result(result)

    async def update_document(
        self,
        doc_id: str,
        content: str | None = None,
        append: bool = False,
        doc_type: str | None = None,
        phase_task: str | None = None,
        feature: str | None = None,
        project: str = "",
        user: str = "",
    ) -> dict[str, Any]:
        """Update a document in Prismind.

        Args:
            doc_id: Document ID.
            content: New content (None to keep existing).
            append: If True, append content. If False, replace.
            doc_type: New document type (moves to corresponding folder).
            phase_task: New phase-task value.
            feature: New feature value.
            project: Project identifier.
            user: User identifier for multi-user support.

        Returns:
            Dict with success, doc_id, updated_fields, message.
        """
        arguments: dict[str, Any] = {"doc_id": doc_id}
        if content is not None:
            arguments["content"] = content
        if append:
            arguments["append"] = append
        if doc_type:
            arguments["doc_type"] = doc_type
        if phase_task:
            arguments["phase_task"] = phase_task
        if feature:
            arguments["feature"] = feature
        if project:
            arguments["project"] = project
        if user:
            arguments["user"] = user

        logger.info(
            "Updating document via MCP",
            doc_id=doc_id,
            project=project,
        )

        success, result = await self._call_tool_safe("update_document", arguments)
        if not success:
            raise RuntimeError(f"update_document failed: {result}")

        return self._parse_json_result(result)

    async def get_document(
        self,
        doc_id: str | None = None,
        query: str | None = None,
        doc_type: str | None = None,
        project: str = "",
        user: str = "",
    ) -> dict[str, Any] | None:
        """Get a document by ID or query.

        Args:
            doc_id: Document ID (preferred).
            query: Search query (if doc_id not provided).
            doc_type: Filter by document type.
            project: Project identifier.
            user: User identifier for multi-user support.

        Returns:
            Document dict or None if not found.
        """
        arguments: dict[str, Any] = {}
        if doc_id:
            arguments["doc_id"] = doc_id
        if query:
            arguments["query"] = query
        if doc_type:
            arguments["doc_type"] = doc_type
        if project:
            arguments["project"] = project
        if user:
            arguments["user"] = user

        logger.info(
            "Getting document via MCP",
            doc_id=doc_id,
            query=query,
        )

        success, result = await self._call_tool_safe("get_document", arguments)
        if not success:
            return None

        parsed = self._parse_json_result(result)
        if parsed.get("success") is False:
            return None
        return parsed

    async def search_documents(
        self,
        query: str | None = None,
        doc_type: str | None = None,
        project: str = "",
        phase_task: str = "",
        limit: int = 10,
        user: str = "",
    ) -> list[dict[str, Any]]:
        """Search documents.

        Args:
            query: Search query.
            doc_type: Filter by document type.
            project: Project identifier.
            phase_task: Filter by phase-task ID.
            limit: Maximum results.
            user: User identifier for multi-user support.

        Returns:
            List of matching documents.
        """
        arguments: dict[str, Any] = {"limit": limit}
        if query:
            arguments["query"] = query
        if doc_type:
            arguments["doc_type"] = doc_type
        if project:
            arguments["project"] = project
        if phase_task:
            arguments["phase_task"] = phase_task
        if user:
            arguments["user"] = user

        logger.info(
            "Searching documents via MCP",
            query=query[:50] if query else None,
            doc_type=doc_type,
        )

        success, result = await self._call_tool_safe("search_documents", arguments)
        if not success:
            raise RuntimeError(f"search_documents failed: {result}")

        return self._parse_list_result(result)

    async def delete_document(
        self,
        doc_id: str,
        project: str = "",
        delete_drive_file: bool = False,
        permanent: bool = False,
        user: str = "",
    ) -> dict[str, Any]:
        """Delete a document.

        Args:
            doc_id: Document ID.
            project: Project identifier.
            delete_drive_file: If True, also delete from Google Drive.
            permanent: If True, permanent delete (no trash).
            user: User identifier for multi-user support.

        Returns:
            Dict with success status.
        """
        arguments: dict[str, Any] = {"doc_id": doc_id}
        if project:
            arguments["project"] = project
        if delete_drive_file:
            arguments["delete_drive_file"] = delete_drive_file
        if permanent:
            arguments["permanent"] = permanent
        if user:
            arguments["user"] = user

        logger.info(
            "Deleting document via MCP",
            doc_id=doc_id,
            permanent=permanent,
        )

        success, result = await self._call_tool_safe("delete_document", arguments)
        if not success:
            raise RuntimeError(f"delete_document failed: {result}")

        return self._parse_json_result(result)

    # === Document Type Management Methods ===

    async def list_document_types(
        self,
        user: str = "",
    ) -> list[dict[str, Any]]:
        """List all document types (global + project).

        Args:
            user: User identifier for multi-user support.

        Returns:
            List of document type info dicts.
        """
        arguments: dict[str, Any] = {}
        if user:
            arguments["user"] = user

        logger.info("Listing document types via MCP")

        success, result = await self._call_tool_safe("list_document_types", arguments)
        if not success:
            raise RuntimeError(f"list_document_types failed: {result}")

        parsed = self._parse_json_result(result)
        return parsed.get("document_types", [])

    async def register_document_type(
        self,
        type_id: str,
        name: str,
        folder_name: str,
        scope: str = "global",
        description: str = "",
        create_folder: bool = True,
        user: str = "",
    ) -> dict[str, Any]:
        """Register a new document type.

        Args:
            type_id: Type identifier (e.g., "api_spec").
            name: Display name (e.g., "API Specification").
            folder_name: Folder name in Google Drive (English, PascalCase).
            scope: "global" for all projects or "project" for current only.
            description: Type description.
            create_folder: If True, create folder in Drive.
            user: User identifier for multi-user support.

        Returns:
            Dict with success status and type info.
        """
        arguments: dict[str, Any] = {
            "type_id": type_id,
            "name": name,
            "folder_name": folder_name,
            "scope": scope,
        }
        if description:
            arguments["description"] = description
        if not create_folder:
            arguments["create_folder"] = create_folder
        if user:
            arguments["user"] = user

        logger.info(
            "Registering document type via MCP",
            type_id=type_id,
            scope=scope,
        )

        success, result = await self._call_tool_safe(
            "register_document_type", arguments
        )
        if not success:
            raise RuntimeError(f"register_document_type failed: {result}")

        return self._parse_json_result(result)

    async def delete_document_type(
        self,
        type_id: str,
        scope: str = "global",
        user: str = "",
    ) -> dict[str, Any]:
        """Delete a document type.

        Args:
            type_id: Type identifier.
            scope: "global" or "project".
            user: User identifier for multi-user support.

        Returns:
            Dict with success status.
        """
        arguments: dict[str, Any] = {
            "type_id": type_id,
            "scope": scope,
        }
        if user:
            arguments["user"] = user

        logger.info(
            "Deleting document type via MCP",
            type_id=type_id,
            scope=scope,
        )

        success, result = await self._call_tool_safe(
            "delete_document_type", arguments
        )
        if not success:
            raise RuntimeError(f"delete_document_type failed: {result}")

        return self._parse_json_result(result)

    # === Catalog Search Methods ===

    async def search_catalog(
        self,
        query: str,
        doc_type: str | None = None,
        project: str = "",
        limit: int = 10,
        user: str = "",
    ) -> list[dict[str, Any]]:
        """Search the document catalog.

        Args:
            query: Search query.
            doc_type: Filter by document type.
            project: Project identifier.
            limit: Maximum results.
            user: User identifier for multi-user support.

        Returns:
            List of matching catalog entries.
        """
        arguments: dict[str, Any] = {
            "query": query,
            "limit": limit,
        }
        if doc_type:
            arguments["doc_type"] = doc_type
        if project:
            arguments["project"] = project
        if user:
            arguments["user"] = user

        logger.info(
            "Searching catalog via MCP",
            query=query[:50],
        )

        success, result = await self._call_tool_safe("search_catalog", arguments)
        if not success:
            raise RuntimeError(f"search_catalog failed: {result}")

        return self._parse_list_result(result)
