"""Adapter for Prismind RAG search service."""

from typing import Any

import httpx
from pydantic import BaseModel

from magickit.adapters.base import BaseAdapter
from magickit.utils.logging import get_logger

logger = get_logger(__name__)


class Document(BaseModel):
    """Document model from RAG search."""

    id: str
    content: str
    metadata: dict[str, Any] = {}
    score: float = 0.0


class PrismindAdapter(BaseAdapter):
    """Adapter for Prismind RAG service.

    Provides methods for semantic search and document retrieval.
    """

    async def health_check(self) -> bool:
        """Check if Prismind service is healthy.

        Returns:
            True if the service is healthy, False otherwise.
        """
        try:
            response = await self._get("/health")
            return response.status_code == 200
        except httpx.HTTPError as e:
            logger.warning("Prismind health check failed", error=str(e))
            return False

    async def search(
        self,
        query: str,
        n: int = 5,
        filter_metadata: dict[str, Any] | None = None,
    ) -> list[Document]:
        """Search for relevant documents.

        Args:
            query: Search query.
            n: Number of results to return.
            filter_metadata: Optional metadata filter.

        Returns:
            List of relevant documents.

        Raises:
            httpx.HTTPError: If the request fails.
        """
        payload: dict[str, Any] = {
            "query": query,
            "n": n,
        }
        if filter_metadata:
            payload["filter"] = filter_metadata

        logger.info("Searching documents", query_length=len(query), n=n)
        response = await self._post("/search", json=payload)
        result = response.json()

        documents = []
        for doc in result.get("documents", []):
            documents.append(
                Document(
                    id=doc.get("id", ""),
                    content=doc.get("content", ""),
                    metadata=doc.get("metadata", {}),
                    score=doc.get("score", 0.0),
                )
            )

        return documents

    async def index(
        self,
        documents: list[dict[str, Any]],
        collection: str = "default",
    ) -> dict[str, Any]:
        """Index documents for search.

        Args:
            documents: Documents to index. Each should have 'content' and optionally 'metadata'.
            collection: Collection name.

        Returns:
            Indexing result with count and status.

        Raises:
            httpx.HTTPError: If the request fails.
        """
        payload = {
            "documents": documents,
            "collection": collection,
        }

        logger.info(
            "Indexing documents",
            count=len(documents),
            collection=collection,
        )
        response = await self._post("/index", json=payload)

        return response.json()

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

        Raises:
            httpx.HTTPError: If the request fails.
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
        """Delete documents from the index.

        Args:
            document_ids: IDs of documents to delete.
            collection: Collection name.

        Returns:
            Deletion result.

        Raises:
            httpx.HTTPError: If the request fails.
        """
        payload = {
            "ids": document_ids,
            "collection": collection,
        }

        logger.info(
            "Deleting documents",
            count=len(document_ids),
            collection=collection,
        )
        response = await self._post("/delete", json=payload)

        return response.json()
