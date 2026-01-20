"""Adapter for Cognilens compression/summarization MCP service."""

import json
from typing import Any

from magickit.adapters.mcp_base import MCPBaseAdapter
from magickit.utils.logging import get_logger

logger = get_logger(__name__)


class CognilensAdapter(MCPBaseAdapter):
    """Adapter for Cognilens MCP service.

    Provides methods for text compression, summarization, and context optimization
    via MCP tool calls.
    """

    async def health_check(self) -> bool:
        """Check if Cognilens service is healthy.

        Returns:
            True if the service is healthy, False otherwise.
        """
        try:
            tools = await self.list_tools()
            # Check if expected tools are available
            expected = {"summarize", "compress_context", "extract_essence"}
            return expected.issubset(set(tools))
        except Exception as e:
            logger.warning("Cognilens health check failed", error=str(e))
            return False

    async def compress(
        self,
        text: str,
        ratio: float = 0.5,
        preserve: list[str] | None = None,
    ) -> str:
        """Compress text while preserving key information.

        Uses compress_context tool with calculated target tokens.

        Args:
            text: Text to compress.
            ratio: Target compression ratio (0.0-1.0).
            preserve: Keywords or concepts to preserve (used in task_description).

        Returns:
            Compressed text.
        """
        # Estimate tokens (rough: 4 chars per token)
        estimated_tokens = len(text) // 4
        target_tokens = int(estimated_tokens * ratio)

        task_desc = "Compress text while preserving key information"
        if preserve:
            task_desc += f". Preserve: {', '.join(preserve)}"

        arguments = {
            "full_context": text,
            "task_description": task_desc,
            "target_tokens": target_tokens,
        }

        logger.info(
            "Compressing text via MCP",
            input_length=len(text),
            target_ratio=ratio,
        )

        success, result = await self._call_tool_safe("compress_context", arguments)
        if not success:
            raise RuntimeError(f"Compression failed: {result}")

        return self._parse_result(result)

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
        """
        arguments = {
            "text": text,
            "style": style,
            "max_tokens": max_tokens,
        }

        logger.info(
            "Summarizing text via MCP",
            input_length=len(text),
            style=style,
            max_tokens=max_tokens,
        )

        success, result = await self._call_tool_safe("summarize", arguments)
        if not success:
            raise RuntimeError(f"Summarization failed: {result}")

        return self._parse_result(result)

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
        """
        arguments: dict[str, Any] = {
            "document": document,
        }
        if focus_areas:
            arguments["focus_areas"] = focus_areas

        logger.info(
            "Extracting essence via MCP",
            document_length=len(document),
            focus_areas=focus_areas,
        )

        success, result = await self._call_tool_safe("extract_essence", arguments)
        if not success:
            raise RuntimeError(f"Essence extraction failed: {result}")

        return self._parse_json_result(result)

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
        """
        arguments = {
            "full_context": context,
            "task_description": task_description,
            "target_tokens": target_tokens,
        }

        logger.info(
            "Optimizing context via MCP",
            context_length=len(context),
            target_tokens=target_tokens,
        )

        success, result = await self._call_tool_safe("compress_context", arguments)
        if not success:
            raise RuntimeError(f"Context optimization failed: {result}")

        return self._parse_result(result)

    async def unify_summaries(
        self,
        documents: list[str],
        purpose: str = "",
    ) -> str:
        """Unify multiple documents into a single coherent summary.

        Args:
            documents: List of documents to unify.
            purpose: Purpose of the unified summary.

        Returns:
            Unified summary.
        """
        arguments: dict[str, Any] = {
            "documents": documents,
        }
        if purpose:
            arguments["purpose"] = purpose

        logger.info(
            "Unifying summaries via MCP",
            document_count=len(documents),
        )

        success, result = await self._call_tool_safe("unify_summaries", arguments)
        if not success:
            raise RuntimeError(f"Summary unification failed: {result}")

        return self._parse_result(result)

    async def summarize_diff(
        self,
        before: str,
        after: str,
        focus: str = "",
    ) -> str:
        """Summarize differences between two versions of text.

        Args:
            before: Original text.
            after: Modified text.
            focus: What to focus on in the diff.

        Returns:
            Summary of differences.
        """
        arguments: dict[str, Any] = {
            "before": before,
            "after": after,
        }
        if focus:
            arguments["focus"] = focus

        logger.info(
            "Summarizing diff via MCP",
            before_length=len(before),
            after_length=len(after),
        )

        success, result = await self._call_tool_safe("summarize_diff", arguments)
        if not success:
            raise RuntimeError(f"Diff summarization failed: {result}")

        return self._parse_result(result)

    async def progressive_compress(
        self,
        text: str,
        stages: int = 3,
    ) -> str:
        """Apply progressive compression through multiple stages.

        Args:
            text: Text to compress.
            stages: Number of compression stages.

        Returns:
            Progressively compressed text.
        """
        arguments = {
            "text": text,
            "stages": stages,
        }

        logger.info(
            "Progressive compression via MCP",
            input_length=len(text),
            stages=stages,
        )

        success, result = await self._call_tool_safe("progressive_compress", arguments)
        if not success:
            raise RuntimeError(f"Progressive compression failed: {result}")

        return self._parse_result(result)

    def _parse_result(self, result: Any) -> str:
        """Parse tool result to string."""
        if result is None:
            return ""
        if isinstance(result, str):
            # Try to parse as JSON and extract text
            try:
                data = json.loads(result)
                if isinstance(data, dict):
                    return data.get("result", data.get("text", str(data)))
                return str(data)
            except json.JSONDecodeError:
                return result
        return str(result)

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
