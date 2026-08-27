"""Smart file reading and analysis tools for Magickit MCP server.

Combines Phanthand (development PC file access) with Cognilens (compression/
summarization) and Lexora (LLM analysis) for context-efficient file operations.
Files are read via Phanthand and processed by Cognilens before returning to
Claude, so only compressed/summarized results consume the context window.
"""

from __future__ import annotations

import re
from typing import Any

from fastmcp import FastMCP

from magickit.adapters.cognilens import CognilensAdapter, CognilensError
from magickit.adapters.lexora import LexoraAdapter
from magickit.adapters.phanthand import (
    PhanthandAdapter,
    PhanthandConnectionError,
    PhanthandError,
)
from magickit.adapters.prismind import PrismindAdapter
from magickit.config import Settings
from magickit.utils.logging import get_logger
from magickit.utils.user import get_current_user

logger = get_logger(__name__)

# Module-level references
_settings: Settings | None = None
_phanthand: PhanthandAdapter | None = None

VALID_MODES = {"raw", "summarize", "essence", "compress"}

_GLOB_CHARS = re.compile(r"[*?\[]")


def _is_glob_pattern(path: str) -> bool:
    """Check if a path contains glob pattern characters.

    Args:
        path: File path string.

    Returns:
        True if the path contains *, ?, or [.
    """
    return bool(_GLOB_CHARS.search(path))


def _get_phanthand() -> PhanthandAdapter:
    """Get or create the PhanthandAdapter singleton.

    Returns:
        PhanthandAdapter instance.
    """
    global _phanthand
    if _phanthand is None:
        _phanthand = PhanthandAdapter()
    return _phanthand


def register_tools(mcp: FastMCP, settings: Settings) -> None:
    """Register smart read/analyze tools with the MCP server.

    Args:
        mcp: FastMCP server instance.
        settings: Application settings.
    """
    global _settings
    _settings = settings

    @mcp.tool()
    async def smart_read(
        files: list[str],
        phanthand_url: str,
        phanthand_api_key: str,
        mode: str = "summarize",
        focus: str = "",
        project: str = "",
        user: str = "",
    ) -> dict[str, Any]:
        """Read files from a development PC via Phanthand and process with Cognilens.

        Files are read remotely and processed server-side. Only the processed
        results are returned, saving Claude's context window.

        USE THIS WHEN: You need to read files from the development PC with
        AI-powered summarization, compression, or essence extraction.

        DO NOT USE WHEN:
        - Files are on the same machine as Magickit → use Read tool directly
        - You need raw file content and the file is small → consider Read tool

        Args:
            files: List of absolute file paths on the development PC.
            phanthand_url: Phanthand server URL (e.g., "http://192.168.1.10:7300").
            phanthand_api_key: Phanthand API key for authentication.
            mode: Processing mode:
                - "raw": Return file content as-is (no Cognilens processing)
                - "summarize": Summarize file content (default)
                - "essence": Extract design patterns, API structures, key concepts
                - "compress": Compress content to save context tokens
            focus: Focus area for essence/compress modes
                (e.g., "authentication flow", "API endpoints").
            project: Optional project identifier.
            user: User identifier for multi-user support (auto-detected if empty).

        Returns:
            Dict containing:
            - success: Whether the operation succeeded
            - mode: Processing mode used
            - results: List of per-file results with file, size, processed, mode
            - file_count: Number of files processed
            - errors: List of per-file errors (file continues on individual failure)
        """
        effective_user = user or get_current_user()

        # Validate parameters
        if not files:
            return {"success": False, "error": "No files specified"}
        if not phanthand_url:
            return {"success": False, "error": "phanthand_url is required"}
        if not phanthand_api_key:
            return {"success": False, "error": "phanthand_api_key is required"}
        if mode not in VALID_MODES:
            return {
                "success": False,
                "error": f"Invalid mode: {mode}. Must be one of: {', '.join(VALID_MODES)}",
            }

        phanthand = _get_phanthand()
        cognilens = CognilensAdapter(
            sse_url=settings.cognilens_url,
            timeout=settings.cognilens_timeout,
        )

        results: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []

        logger.info(
            "smart_read started",
            file_count=len(files),
            mode=mode,
            user=effective_user,
        )

        for file_path in files:
            try:
                # Read file via Phanthand
                file_data = await phanthand.read_file(
                    phanthand_url, phanthand_api_key, file_path,
                )
                content = file_data["content"]
                size = file_data["size"]

                # Process with Cognilens based on mode.
                #
                # Guarded separately from the Phanthand read above: by this
                # point the file has been read successfully, so a Cognilens
                # failure should degrade this file to raw content rather than
                # drop it into `errors` and lose the read. CognilensAdapter
                # raises on an upstream rejection instead of returning the
                # rejection text as if it were the summary, so this branch is
                # where that rejection becomes visible.
                effective_mode = mode
                processing_error = ""
                try:
                    if mode == "raw":
                        processed = content
                    elif mode == "summarize":
                        processed = await cognilens.summarize(content)
                    elif mode == "essence":
                        focus_areas = [focus] if focus else None
                        essence = await cognilens.extract_essence(
                            content, focus_areas=focus_areas,
                        )
                        # extract_essence returns dict, convert to readable string
                        if isinstance(essence, dict):
                            processed = _format_essence(essence)
                        else:
                            processed = str(essence)
                    elif mode == "compress":
                        preserve = [focus] if focus else None
                        processed = await cognilens.compress(
                            content, preserve=preserve,
                        )
                    else:
                        processed = content
                except CognilensError as e:
                    processed = content
                    effective_mode = "raw"
                    processing_error = str(e)
                    logger.warning(
                        "smart_read falling back to raw content",
                        file=file_path,
                        requested_mode=mode,
                        error=str(e),
                        error_type=e.error_type,
                    )

                entry: dict[str, Any] = {
                    "file": file_path,
                    "size": size,
                    "processed": processed,
                    "mode": effective_mode,
                }
                if processing_error:
                    # Name the requested mode too: "mode: raw" alone would
                    # read as a choice rather than as a fallback.
                    entry["requested_mode"] = mode
                    entry["processing_error"] = processing_error
                results.append(entry)

            except PhanthandConnectionError:
                # Connection error affects all files, fail immediately
                return {
                    "success": False,
                    "error": f"Cannot connect to Phanthand at {phanthand_url}",
                    "results": results,
                    "errors": errors,
                }
            except PhanthandError as e:
                # Per-file Phanthand error, continue with next file
                errors.append({"file": file_path, "error": str(e)})
                logger.warning(
                    "smart_read file error",
                    file=file_path,
                    error=str(e),
                )
            except Exception as e:
                # Cognilens or other error, log and continue
                errors.append({"file": file_path, "error": str(e)})
                logger.warning(
                    "smart_read processing error",
                    file=file_path,
                    error=str(e),
                )

        return {
            "success": len(results) > 0,
            "mode": mode,
            "results": results,
            "file_count": len(results),
            "errors": errors,
        }

    @mcp.tool()
    async def smart_analyze(
        files: list[str],
        question: str,
        phanthand_url: str,
        phanthand_api_key: str,
        search_root: str = "",
        max_files: int = 20,
        project: str = "",
        save_to_knowledge: bool = False,
        user: str = "",
    ) -> dict[str, Any]:
        """Analyze multiple files from a development PC and answer a question.

        Reads files via Phanthand, unifies summaries with Cognilens, and
        generates an answer using Lexora. Supports glob patterns for file
        discovery.

        USE THIS WHEN: You need cross-file analysis or want to understand
        patterns across multiple source files.

        DO NOT USE WHEN:
        - You only need to read a single file → use smart_read
        - You need raw file content → use smart_read with mode="raw"

        Args:
            files: List of absolute file paths or glob patterns
                (e.g., ["src/api/*.py", "src/auth.py"]).
            question: The analysis question to answer
                (e.g., "What error handling patterns are used?").
            phanthand_url: Phanthand server URL.
            phanthand_api_key: Phanthand API key.
            search_root: Root directory for glob pattern expansion
                (required if files contain glob patterns).
            max_files: Maximum number of files to process (default 20,
                controls cost/load).
            project: Optional project identifier.
            save_to_knowledge: If True, save analysis result to Prismind
                as searchable knowledge.
            user: User identifier for multi-user support (auto-detected if empty).

        Returns:
            Dict containing:
            - success: Whether the analysis succeeded
            - question: The original question
            - answer: LLM-generated analysis answer
            - files_analyzed: List of file paths that were analyzed
            - file_count: Number of files analyzed
            - summary: Unified summary from Cognilens
            - knowledge_saved: Whether result was saved to Prismind
            - errors: List of per-file errors
        """
        effective_user = user or get_current_user()

        # Validate parameters
        if not files:
            return {"success": False, "error": "No files specified"}
        if not question:
            return {"success": False, "error": "question is required"}
        if not phanthand_url:
            return {"success": False, "error": "phanthand_url is required"}
        if not phanthand_api_key:
            return {"success": False, "error": "phanthand_api_key is required"}

        phanthand = _get_phanthand()
        cognilens = CognilensAdapter(
            sse_url=settings.cognilens_url,
            timeout=settings.cognilens_timeout,
        )
        lexora = LexoraAdapter(
            base_url=settings.lexora_url,
            timeout=settings.lexora_timeout,
        )

        logger.info(
            "smart_analyze started",
            file_patterns=len(files),
            question=question[:100],
            user=effective_user,
        )

        # Step 1: Resolve file list (expand glob patterns)
        resolved_files: list[str] = []
        errors: list[dict[str, str]] = []

        for file_entry in files:
            if _is_glob_pattern(file_entry):
                if not search_root:
                    errors.append({
                        "file": file_entry,
                        "error": "search_root is required for glob patterns",
                    })
                    continue
                try:
                    search_result = await phanthand.search(
                        phanthand_url, phanthand_api_key,
                        search_root, file_entry,
                        max_results=max_files,
                    )
                    resolved_files.extend(search_result.get("matches", []))
                except PhanthandError as e:
                    errors.append({"file": file_entry, "error": str(e)})
            else:
                resolved_files.append(file_entry)

        # Enforce max_files limit
        truncated = False
        if len(resolved_files) > max_files:
            resolved_files = resolved_files[:max_files]
            truncated = True

        if not resolved_files:
            return {
                "success": False,
                "error": "No files resolved after glob expansion",
                "errors": errors,
            }

        # Step 2: Read all files
        file_contents: list[dict[str, str]] = []
        files_analyzed: list[str] = []

        for file_path in resolved_files:
            try:
                file_data = await phanthand.read_file(
                    phanthand_url, phanthand_api_key, file_path,
                )
                file_contents.append({
                    "title": file_path,
                    "content": file_data["content"],
                })
                files_analyzed.append(file_path)
            except PhanthandConnectionError:
                return {
                    "success": False,
                    "error": f"Cannot connect to Phanthand at {phanthand_url}",
                    "errors": errors,
                }
            except PhanthandError as e:
                errors.append({"file": file_path, "error": str(e)})
                logger.warning(
                    "smart_analyze file read error",
                    file=file_path,
                    error=str(e),
                )

        if not file_contents:
            return {
                "success": False,
                "error": "No files could be read",
                "errors": errors,
            }

        # Step 3: Unify summaries via Cognilens
        try:
            unified_summary = await cognilens.unify_summaries(
                file_contents,
                purpose=question,
            )
        except Exception as e:
            logger.error("Cognilens unify_summaries failed", error=str(e))
            return {
                "success": False,
                "error": f"Failed to unify summaries: {e}",
                "files_analyzed": files_analyzed,
                "errors": errors,
            }

        # Step 4: Generate answer via Lexora
        try:
            prompt = (
                f"Based on the following analysis of source code files, "
                f"answer this question: {question}\n\n"
                f"Source analysis:\n{unified_summary}"
            )
            answer = await lexora.generate(prompt)
        except Exception as e:
            logger.error("Lexora generation failed", error=str(e))
            # Return summary even if Lexora fails
            return {
                "success": True,
                "question": question,
                "answer": f"[LLM analysis unavailable: {e}]",
                "files_analyzed": files_analyzed,
                "file_count": len(files_analyzed),
                "summary": unified_summary,
                "knowledge_saved": False,
                "truncated": truncated,
                "errors": errors,
            }

        # Step 5: Optionally save to Prismind
        knowledge_saved = False
        if save_to_knowledge and project:
            try:
                prismind = PrismindAdapter(
                    sse_url=settings.prismind_url,
                    timeout=settings.prismind_timeout,
                )
                await prismind.add_knowledge(
                    content=f"Analysis: {question}\n\nAnswer: {answer}",
                    category="analysis",
                    project=project,
                    tags=["smart_analyze", "cross-file"],
                    source=f"smart_analyze:{','.join(files_analyzed[:5])}",
                    user=effective_user,
                )
                knowledge_saved = True
            except Exception as e:
                logger.warning(
                    "Failed to save analysis to Prismind",
                    error=str(e),
                )

        return {
            "success": True,
            "question": question,
            "answer": answer,
            "files_analyzed": files_analyzed,
            "file_count": len(files_analyzed),
            "summary": unified_summary,
            "knowledge_saved": knowledge_saved,
            "truncated": truncated,
            "errors": errors,
        }


def _format_essence(essence: dict[str, Any]) -> str:
    """Format extract_essence result dict into readable text.

    Args:
        essence: Dict from Cognilens extract_essence.

    Returns:
        Formatted string representation.
    """
    parts: list[str] = []

    if "concepts" in essence:
        parts.append("## Concepts")
        for concept in essence["concepts"]:
            if isinstance(concept, str):
                parts.append(f"- {concept}")
            elif isinstance(concept, dict):
                name = concept.get("name", concept.get("concept", ""))
                desc = concept.get("description", "")
                parts.append(f"- **{name}**: {desc}" if desc else f"- {name}")

    if "relationships" in essence:
        parts.append("\n## Relationships")
        for rel in essence["relationships"]:
            if isinstance(rel, str):
                parts.append(f"- {rel}")
            elif isinstance(rel, dict):
                parts.append(
                    f"- {rel.get('from', '?')} → {rel.get('to', '?')}: "
                    f"{rel.get('type', rel.get('relationship', ''))}"
                )

    if "specifications" in essence:
        parts.append("\n## Specifications")
        for spec in essence["specifications"]:
            if isinstance(spec, str):
                parts.append(f"- {spec}")
            elif isinstance(spec, dict):
                parts.append(f"- {spec.get('name', '')}: {spec.get('value', '')}")

    # Fallback: dump any remaining keys
    shown_keys = {"concepts", "relationships", "specifications"}
    for key, value in essence.items():
        if key not in shown_keys:
            parts.append(f"\n## {key.title()}")
            if isinstance(value, list):
                for item in value:
                    parts.append(f"- {item}")
            else:
                parts.append(str(value))

    return "\n".join(parts) if parts else str(essence)
