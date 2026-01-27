"""Research tools for Magickit MCP server.

Combines Prismind knowledge search with Cognilens compression/summarization
for optimized knowledge retrieval within token budgets.
"""

from __future__ import annotations

import json
from typing import Any

from fastmcp import FastMCP

from magickit.adapters.cognilens import CognilensAdapter
from magickit.adapters.prismind import PrismindAdapter
from magickit.config import Settings
from magickit.utils.logging import get_logger

logger = get_logger(__name__)

# Module-level settings reference
_settings: Settings | None = None


def _parse_list_result(result: Any) -> list[dict[str, Any]]:
    """Parse MCP tool result to list of dicts.

    MCP tools return JSON strings, which need to be parsed.
    The result may be a list directly, or a dict containing a list.

    Args:
        result: Raw result from MCP tool call.

    Returns:
        List of dict entries.
    """
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
                for key in ["results", "items", "documents", "knowledge", "entries", "catalog"]:
                    if key in data and isinstance(data[key], list):
                        return data[key]
                # Single dict result
                return [data]
            return [{"result": data}]
        except json.JSONDecodeError:
            return [{"result": result}]
    if isinstance(result, dict):
        # Already a dict, check for list inside
        for key in ["results", "items", "documents", "knowledge", "entries", "catalog"]:
            if key in result and isinstance(result[key], list):
                return result[key]
        return [result]
    return [{"result": result}]


def _parse_dict_result(result: Any) -> dict[str, Any]:
    """Parse MCP tool result to dict.

    MCP tools return JSON strings, which need to be parsed.

    Args:
        result: Raw result from MCP tool call.

    Returns:
        Parsed dict.
    """
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


def register_tools(mcp: FastMCP, settings: Settings) -> None:
    """Register research tools with the MCP server.

    Args:
        mcp: FastMCP server instance.
        settings: Application settings.
    """
    global _settings
    _settings = settings

    @mcp.tool()
    async def research_and_summarize(
        query: str,
        max_tokens: int = 1000,
        category: str = "",
        project: str = "",
        tags: list[str] | None = None,
        search_limit: int = 10,
        summary_style: str = "concise",
    ) -> dict[str, Any]:
        """Search knowledge base and compress results in a single optimized operation.

        USE THIS WHEN: you need to find relevant knowledge AND fit it within a token budget.
        Unlike calling Prismind search_knowledge then Cognilens compress separately, this:
        - Automatically handles pagination for large result sets
        - Deduplicates overlapping knowledge entries
        - Optimizes compression based on query context

        DO NOT USE WHEN:
        - You just need raw search results → use Prismind search_knowledge directly
        - You have existing text to compress → use Cognilens compress_context directly
        - You need full document content without compression → use analyze_documents

        Args:
            query: Search query for knowledge retrieval.
            max_tokens: Target token budget for the final output.
            category: Optional category filter for Prismind search.
            project: Optional project filter for Prismind search.
            tags: Optional tag filters for Prismind search.
            search_limit: Maximum number of search results to retrieve.
            summary_style: Style for summarization ("concise", "detailed", "bullet").

        Returns:
            Dict containing:
            - summary: Compressed/summarized knowledge within token budget
            - source_count: Number of knowledge entries used
            - sources: List of source references with IDs and scores
            - original_tokens: Estimated tokens before compression
            - final_tokens: Estimated tokens after compression
        """
        if _settings is None:
            raise RuntimeError("Settings not initialized")

        # Step 1: Search knowledge via Prismind
        prismind = PrismindAdapter(
            sse_url=_settings.prismind_url,
            timeout=_settings.prismind_timeout,
        )

        logger.info(
            "Searching knowledge",
            query=query[:50],
            limit=search_limit,
            category=category,
        )

        try:
            # Build search params, excluding None values to avoid validation errors
            search_params: dict[str, Any] = {
                "query": query,
                "limit": search_limit,
            }
            if category:
                search_params["category"] = category
            if project:
                search_params["project"] = project
            if tags:
                search_params["tags"] = tags

            raw_results = await prismind.search_knowledge(**search_params)

            logger.info(
                "Prismind search raw result",
                result_type=type(raw_results).__name__,
                result_preview=str(raw_results)[:200] if raw_results else "None",
            )
        except Exception as e:
            logger.error("Prismind search failed", error=str(e))
            raw_results = None

        # Parse JSON string result to list
        search_results = _parse_list_result(raw_results)

        logger.info(
            "Parsed search results",
            result_count=len(search_results),
        )

        if not search_results:
            return {
                "summary": "No relevant knowledge found for the query.",
                "source_count": 0,
                "sources": [],
                "original_tokens": 0,
                "final_tokens": 10,
            }

        # Step 2: Deduplicate and combine content
        seen_content = set()
        unique_entries = []
        sources = []

        for entry in search_results:
            content = entry.get("content", "")
            content_hash = hash(content[:200])  # Hash first 200 chars for dedup

            if content_hash not in seen_content and content:
                seen_content.add(content_hash)
                unique_entries.append(content)
                sources.append({
                    "id": entry.get("id", entry.get("knowledge_id", "")),
                    "score": entry.get("score", entry.get("similarity", 0.0)),
                    "category": entry.get("category", ""),
                })

        combined_text = "\n\n---\n\n".join(unique_entries)
        original_tokens = len(combined_text) // 4  # Rough estimate

        # Step 3: Compress if needed via Cognilens
        if original_tokens <= max_tokens:
            # No compression needed
            return {
                "summary": combined_text,
                "source_count": len(unique_entries),
                "sources": sources,
                "original_tokens": original_tokens,
                "final_tokens": original_tokens,
            }

        cognilens = CognilensAdapter(
            sse_url=_settings.cognilens_url,
            timeout=_settings.cognilens_timeout,
        )

        logger.info(
            "Compressing results",
            original_tokens=original_tokens,
            target_tokens=max_tokens,
        )

        # Use optimize_context for task-aware compression
        compressed = await cognilens.optimize_context(
            context=combined_text,
            task_description=f"Summarize knowledge relevant to: {query}. Style: {summary_style}",
            target_tokens=max_tokens,
        )

        final_tokens = len(compressed) // 4

        return {
            "summary": compressed,
            "source_count": len(unique_entries),
            "sources": sources,
            "original_tokens": original_tokens,
            "final_tokens": final_tokens,
        }

    @mcp.tool()
    async def analyze_documents(
        query: str,
        doc_type: str = "",
        token_budget: int = 2000,
        focus_areas: list[str] | None = None,
        include_essence: bool = True,
    ) -> dict[str, Any]:
        """Retrieve and analyze documents with intelligent essence extraction.

        USE THIS WHEN: you need to understand document content, extract key concepts,
        or prepare document summaries for further processing. This tool:
        - Searches the document catalog for relevant documents
        - Extracts essential information with configurable focus areas
        - Manages token budget to fit context windows

        DO NOT USE WHEN:
        - You need keyword-level knowledge search → use research_and_summarize
        - You want to add new documents → use Prismind add_knowledge directly
        - You already have document IDs → use Prismind get_document directly

        Args:
            query: Search query to find relevant documents.
            doc_type: Optional document type filter (e.g., "api", "design", "spec").
            token_budget: Maximum tokens for the combined output.
            focus_areas: Optional areas to focus on during essence extraction.
            include_essence: Whether to extract essence (key concepts) from documents.

        Returns:
            Dict containing:
            - documents: List of document summaries
            - total_documents: Number of documents found
            - essence: Extracted key concepts (if include_essence=True)
            - token_usage: Estimated tokens in the response
        """
        if _settings is None:
            raise RuntimeError("Settings not initialized")

        prismind = PrismindAdapter(
            sse_url=_settings.prismind_url,
            timeout=_settings.prismind_timeout,
        )

        # Search catalog
        logger.info(
            "Searching document catalog",
            query=query[:50],
            doc_type=doc_type,
        )

        # Build catalog search params, excluding empty values
        catalog_params: dict[str, Any] = {
            "query": query,
            "limit": 10,
        }
        if doc_type:
            catalog_params["doc_type"] = doc_type

        raw_catalog = await prismind.search_catalog(**catalog_params)

        # Parse JSON string result to list
        catalog_results = _parse_list_result(raw_catalog)

        if not catalog_results:
            return {
                "documents": [],
                "total_documents": 0,
                "essence": None,
                "token_usage": 10,
            }

        # Collect document content
        documents = []
        combined_content = []

        for entry in catalog_results[:5]:  # Limit to 5 documents
            doc_id = entry.get("doc_id", entry.get("id", ""))

            try:
                raw_doc = await prismind.get_document(doc_id=doc_id)
                # Parse JSON string result to dict
                doc = _parse_dict_result(raw_doc)
                content = doc.get("content", "")

                documents.append({
                    "id": doc_id,
                    "type": entry.get("doc_type", doc_type),
                    "title": entry.get("title", doc_id),
                    "preview": content[:500] + "..." if len(content) > 500 else content,
                })
                combined_content.append(content)
            except Exception as e:
                logger.warning("Failed to get document", doc_id=doc_id, error=str(e))

        # Extract essence if requested
        essence = None
        if include_essence and combined_content:
            cognilens = CognilensAdapter(
                sse_url=_settings.cognilens_url,
                timeout=_settings.cognilens_timeout,
            )

            full_text = "\n\n".join(combined_content)

            # Check if we need to compress first
            if len(full_text) // 4 > token_budget:
                full_text = await cognilens.optimize_context(
                    context=full_text,
                    task_description=f"Extract key information about: {query}",
                    target_tokens=token_budget,
                )

            try:
                # Build essence params, excluding None values
                essence_params: dict[str, Any] = {"document": full_text}
                if focus_areas:
                    essence_params["focus_areas"] = focus_areas

                essence = await cognilens.extract_essence(**essence_params)
            except Exception as e:
                logger.warning("Essence extraction failed", error=str(e))
                essence = {"error": str(e)}

        # Calculate token usage
        total_preview_length = sum(len(d["preview"]) for d in documents)
        token_usage = total_preview_length // 4

        return {
            "documents": documents,
            "total_documents": len(catalog_results),
            "essence": essence,
            "token_usage": token_usage,
        }
