"""Adapter for Cognilens compression/summarization service."""

from typing import Any

import httpx

from magickit.adapters.base import BaseAdapter
from magickit.utils.logging import get_logger

logger = get_logger(__name__)


class CognilensAdapter(BaseAdapter):
    """Adapter for Cognilens service.

    Provides methods for text compression, summarization, and context optimization.
    """

    async def health_check(self) -> bool:
        """Check if Cognilens service is healthy.

        Returns:
            True if the service is healthy, False otherwise.
        """
        try:
            response = await self._get("/health")
            return response.status_code == 200
        except httpx.HTTPError as e:
            logger.warning("Cognilens health check failed", error=str(e))
            return False

    async def compress(
        self,
        text: str,
        ratio: float = 0.5,
        preserve: list[str] | None = None,
    ) -> str:
        """Compress text while preserving key information.

        Args:
            text: Text to compress.
            ratio: Target compression ratio (0.0-1.0).
            preserve: Keywords or concepts to preserve.

        Returns:
            Compressed text.

        Raises:
            httpx.HTTPError: If the request fails.
        """
        payload: dict[str, Any] = {
            "text": text,
            "ratio": ratio,
        }
        if preserve:
            payload["preserve"] = preserve

        logger.info(
            "Compressing text",
            input_length=len(text),
            target_ratio=ratio,
        )
        response = await self._post("/compress", json=payload)
        result = response.json()

        return result.get("compressed", "")

    async def summarize(
        self,
        text: str,
        style: str = "concise",
        max_tokens: int = 500,
    ) -> str:
        """Summarize text with specified style.

        Args:
            text: Text to summarize.
            style: Summary style ('concise', 'detailed', 'bullet').
            max_tokens: Maximum tokens for summary.

        Returns:
            Summarized text.

        Raises:
            httpx.HTTPError: If the request fails.
        """
        payload = {
            "text": text,
            "style": style,
            "max_tokens": max_tokens,
        }

        logger.info(
            "Summarizing text",
            input_length=len(text),
            style=style,
            max_tokens=max_tokens,
        )
        response = await self._post("/summarize", json=payload)
        result = response.json()

        return result.get("summary", "")

    async def extract_essence(
        self,
        document: str,
        focus_areas: list[str] | None = None,
    ) -> dict[str, Any]:
        """Extract essential information from a document.

        Args:
            document: Document to analyze.
            focus_areas: Areas to focus on (e.g., ['API changes', 'breaking changes']).

        Returns:
            Extracted essence with concepts, relationships, and specifications.

        Raises:
            httpx.HTTPError: If the request fails.
        """
        payload: dict[str, Any] = {
            "document": document,
        }
        if focus_areas:
            payload["focus_areas"] = focus_areas

        logger.info(
            "Extracting essence",
            document_length=len(document),
            focus_areas=focus_areas,
        )
        response = await self._post("/extract", json=payload)

        return response.json()

    async def optimize_context(
        self,
        context: str,
        task_description: str,
        target_tokens: int = 500,
    ) -> str:
        """Optimize context for a specific task.

        Args:
            context: Full context to optimize.
            task_description: Description of the task.
            target_tokens: Target token count.

        Returns:
            Optimized context.

        Raises:
            httpx.HTTPError: If the request fails.
        """
        payload = {
            "full_context": context,
            "task_description": task_description,
            "target_tokens": target_tokens,
        }

        logger.info(
            "Optimizing context",
            context_length=len(context),
            target_tokens=target_tokens,
        )
        response = await self._post("/compress_context", json=payload)
        result = response.json()

        return result.get("compressed", "")
