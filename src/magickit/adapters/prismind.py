"""Adapter for Prismind knowledge management MCP service."""

import json
from typing import Any

from pydantic import BaseModel

from magickit.adapters.mcp_base import MCPBaseAdapter
from magickit.utils.logging import get_logger

logger = get_logger(__name__)


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

    async def delete(
        self,
        document_ids: list[str],
        collection: str = "default",
    ) -> dict[str, Any]:
        """Delete knowledge entries.

        Note: Prismind uses update_knowledge for modifications.
        This is a placeholder for API compatibility.

        Args:
            document_ids: IDs of documents to delete.
            collection: Collection name.

        Returns:
            Deletion result.
        """
        logger.warning(
            "Delete operation not directly supported by Prismind MCP",
            document_ids=document_ids,
        )
        return {
            "deleted": 0,
            "message": "Delete not directly supported. Use update_knowledge to modify entries.",
        }

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
    ) -> dict[str, Any]:
        """Find a document type semantically similar to the query.

        Uses RAG-based semantic search (BGE-M3 embeddings) for multilingual
        matching. For example, "api仕様" can match "api_spec".

        Args:
            type_query: Search query (type name, ID, or description)
            threshold: Minimum similarity score (0.0-1.0)

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

        success, result = await self._call_tool_safe(
            "find_similar_document_type",
            {"type_query": type_query, "threshold": threshold},
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
        next_action: str = "",
        notes: str = "",
        blockers: list[str] | None = None,
        user: str = "",
    ) -> dict[str, Any]:
        """End the session and save state.

        Args:
            next_action: Recommended next action for next session
            notes: Notes to pass to next session
            blockers: List of blockers
            user: User identifier for multi-user support

        Returns:
            Dict with success status and session duration
        """
        arguments: dict[str, Any] = {}
        if next_action:
            arguments["next_action"] = next_action
        if notes:
            arguments["notes"] = notes
        if blockers:
            arguments["blockers"] = blockers
        if user:
            arguments["user"] = user

        logger.info("Ending session via MCP", user=user)

        success, result = await self._call_tool_safe("end_session", arguments)
        if not success:
            raise RuntimeError(f"end_session failed: {result}")

        return self._parse_json_result(result)

    async def save_session(
        self,
        summary: str = "",
        blockers: list[str] | None = None,
        user: str = "",
    ) -> dict[str, Any]:
        """Save session state without ending.

        Args:
            summary: Work summary
            blockers: List of blockers
            user: User identifier for multi-user support

        Returns:
            Dict with success status
        """
        arguments: dict[str, Any] = {}
        if summary:
            arguments["summary"] = summary
        if blockers:
            arguments["blockers"] = blockers
        if user:
            arguments["user"] = user

        logger.info("Saving session via MCP", user=user)

        success, result = await self._call_tool_safe("save_session", arguments)
        if not success:
            raise RuntimeError(f"save_session failed: {result}")

        return self._parse_json_result(result)
