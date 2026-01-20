"""Generation tools for Magickit MCP server.

Provides RAG-enhanced content generation combining all services.
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from magickit.adapters.cognilens import CognilensAdapter
from magickit.adapters.prismind import PrismindAdapter
from magickit.adapters.lexora import LexoraAdapter
from magickit.config import Settings
from magickit.utils.logging import get_logger

logger = get_logger(__name__)

# Module-level settings reference
_settings: Settings | None = None


def register_tools(mcp: FastMCP, settings: Settings) -> None:
    """Register generation tools with the MCP server.

    Args:
        mcp: FastMCP server instance.
        settings: Application settings.
    """
    global _settings
    _settings = settings

    @mcp.tool()
    async def generate_with_context(
        task: str,
        context_query: str = "",
        max_context_tokens: int = 1500,
        max_output_tokens: int = 1000,
        temperature: float = 0.7,
        category: str = "",
        project: str = "",
        system_prompt: str = "",
        compress_context: bool = True,
    ) -> dict[str, Any]:
        """Generate content using RAG-enhanced context from the knowledge base.

        USE THIS WHEN: you need to generate content (text, code, explanations)
        that should be grounded in existing knowledge. This tool:
        - Searches relevant knowledge from Prismind
        - Optionally compresses context via Cognilens to fit token budgets
        - Generates output via Lexora with the enriched context

        DO NOT USE WHEN:
        - You have all context already → call Lexora generate directly
        - You just need knowledge search → use research_and_summarize
        - The task doesn't benefit from knowledge context

        Args:
            task: The generation task or prompt (what to generate).
            context_query: Query to search for relevant context.
                          If empty, uses the task itself as the query.
            max_context_tokens: Maximum tokens for retrieved context.
            max_output_tokens: Maximum tokens for generated output.
            temperature: Generation temperature (0.0-1.0).
            category: Optional category filter for knowledge search.
            project: Optional project filter for knowledge search.
            system_prompt: Optional system prompt for generation.
            compress_context: Whether to compress context if too large.

        Returns:
            Dict containing:
            - generated: The generated content
            - context_used: Summary of context that was used
            - sources: List of knowledge sources referenced
            - tokens: Token usage breakdown
        """
        if _settings is None:
            raise RuntimeError("Settings not initialized")

        # Use task as context query if not provided
        if not context_query:
            context_query = task

        # Step 1: Search for relevant context via Prismind
        prismind = PrismindAdapter(
            sse_url=_settings.prismind_url,
            timeout=_settings.prismind_timeout,
        )

        logger.info(
            "Searching for context",
            query=context_query[:50],
            category=category,
        )

        search_results = await prismind.search_knowledge(
            query=context_query,
            category=category,
            project=project,
            limit=10,
        )

        # Collect and dedupe context
        seen_content = set()
        context_parts = []
        sources = []

        for entry in search_results:
            content = entry.get("content", "")
            content_hash = hash(content[:200])

            if content_hash not in seen_content and content:
                seen_content.add(content_hash)
                context_parts.append(content)
                sources.append({
                    "id": entry.get("id", entry.get("knowledge_id", "")),
                    "score": entry.get("score", entry.get("similarity", 0.0)),
                })

        combined_context = "\n\n---\n\n".join(context_parts)
        original_context_tokens = len(combined_context) // 4

        # Step 2: Compress context if needed
        final_context = combined_context
        context_compressed = False

        if compress_context and original_context_tokens > max_context_tokens:
            cognilens = CognilensAdapter(
                sse_url=_settings.cognilens_url,
                timeout=_settings.cognilens_timeout,
            )

            logger.info(
                "Compressing context",
                original_tokens=original_context_tokens,
                target_tokens=max_context_tokens,
            )

            final_context = await cognilens.optimize_context(
                context=combined_context,
                task_description=f"Compress context relevant to: {task}",
                target_tokens=max_context_tokens,
            )
            context_compressed = True

        final_context_tokens = len(final_context) // 4

        # Step 3: Build prompt and generate via Lexora
        lexora = LexoraAdapter(
            base_url=_settings.lexora_url,
            timeout=_settings.lexora_timeout,
        )

        # Build the full prompt
        if final_context:
            prompt = f"""Based on the following context:

{final_context}

---

Task: {task}"""
        else:
            prompt = task

        # Add system prompt if provided
        if system_prompt:
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ]
            logger.info(
                "Generating with chat",
                prompt_length=len(prompt),
                max_tokens=max_output_tokens,
            )
            generated = await lexora.chat(
                messages=messages,
                max_tokens=max_output_tokens,
                temperature=temperature,
            )
        else:
            logger.info(
                "Generating content",
                prompt_length=len(prompt),
                max_tokens=max_output_tokens,
            )
            generated = await lexora.generate(
                prompt=prompt,
                max_tokens=max_output_tokens,
                temperature=temperature,
            )

        output_tokens = len(generated) // 4

        return {
            "generated": generated,
            "context_used": final_context[:500] + "..." if len(final_context) > 500 else final_context,
            "context_compressed": context_compressed,
            "sources": sources,
            "tokens": {
                "context_original": original_context_tokens,
                "context_final": final_context_tokens,
                "output": output_tokens,
                "total": final_context_tokens + output_tokens,
            },
        }
