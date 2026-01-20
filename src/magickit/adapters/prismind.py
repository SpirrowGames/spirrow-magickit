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

    # === Prismind-specific methods ===

    async def add_knowledge(
        self,
        content: str,
        category: str = "",
        project: str = "",
        tags: list[str] | None = None,
        source: str = "",
    ) -> dict[str, Any]:
        """Add knowledge entry.

        Args:
            content: Knowledge content.
            category: Category for the knowledge.
            project: Project to associate with.
            tags: Tags for the knowledge.
            source: Source of the knowledge.

        Returns:
            Result with knowledge ID.
        """
        arguments: dict[str, Any] = {
            "content": content,
        }
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
            raise RuntimeError(f"Add knowledge failed: {result}")

        return self._parse_json_result(result)

    async def search_knowledge(
        self,
        query: str,
        category: str = "",
        project: str = "",
        tags: list[str] | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Search knowledge with full options.

        Args:
            query: Search query.
            category: Filter by category.
            project: Filter by project.
            tags: Filter by tags.
            limit: Maximum results.

        Returns:
            List of knowledge entries.
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

        logger.info("Searching knowledge via MCP", query=query, limit=limit)

        success, result = await self._call_tool_safe("search_knowledge", arguments)
        if not success:
            raise RuntimeError(f"Search knowledge failed: {result}")

        return self._parse_list_result(result)

    async def list_projects(self) -> list[dict[str, Any]]:
        """List all projects.

        Returns:
            List of project information.
        """
        success, result = await self._call_tool_safe("list_projects", {})
        if not success:
            raise RuntimeError(f"List projects failed: {result}")

        return self._parse_list_result(result)

    async def get_setup_status(self) -> dict[str, Any]:
        """Get setup status.

        Returns:
            Setup status information.
        """
        success, result = await self._call_tool_safe("get_setup_status", {})
        if not success:
            raise RuntimeError(f"Get setup status failed: {result}")

        return self._parse_json_result(result)

    async def check_services_status(self, detailed: bool = False) -> dict[str, Any]:
        """Check services status.

        Args:
            detailed: Include detailed information.

        Returns:
            Services status.
        """
        arguments = {"detailed": detailed}

        success, result = await self._call_tool_safe("check_services_status", arguments)
        if not success:
            raise RuntimeError(f"Check services status failed: {result}")

        return self._parse_json_result(result)

    async def get_document(
        self,
        query: str = "",
        doc_id: str = "",
        doc_type: str = "",
    ) -> dict[str, Any]:
        """Get a document.

        Args:
            query: Search query for the document.
            doc_id: Document ID.
            doc_type: Document type.

        Returns:
            Document content and metadata.
        """
        arguments: dict[str, Any] = {}
        if query:
            arguments["query"] = query
        if doc_id:
            arguments["doc_id"] = doc_id
        if doc_type:
            arguments["doc_type"] = doc_type

        success, result = await self._call_tool_safe("get_document", arguments)
        if not success:
            raise RuntimeError(f"Get document failed: {result}")

        return self._parse_json_result(result)

    async def search_catalog(
        self,
        query: str,
        doc_type: str = "",
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Search the document catalog.

        Args:
            query: Search query.
            doc_type: Filter by document type.
            limit: Maximum results.

        Returns:
            List of catalog entries.
        """
        arguments: dict[str, Any] = {
            "query": query,
            "limit": limit,
        }
        if doc_type:
            arguments["doc_type"] = doc_type

        success, result = await self._call_tool_safe("search_catalog", arguments)
        if not success:
            raise RuntimeError(f"Search catalog failed: {result}")

        return self._parse_list_result(result)

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
