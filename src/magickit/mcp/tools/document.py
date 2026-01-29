"""Document management tools for Magickit MCP server.

Provides smart document creation that automatically handles unknown document types
using RAG-based semantic matching and LLM-based metadata generation.
"""

from __future__ import annotations

import json
import re
from typing import Any

from fastmcp import FastMCP

from magickit.adapters.lexora import LexoraAdapter
from magickit.adapters.prismind import PrismindAdapter
from magickit.config import Settings
from magickit.utils.logging import get_logger
from magickit.utils.user import get_current_user

logger = get_logger(__name__)

# Module-level settings reference
_settings: Settings | None = None

# Default similarity threshold for semantic document type matching
# BGE-M3 embeddings typically return scores in 0.5-0.7 range for semantic matches
DEFAULT_SIMILARITY_THRESHOLD = 0.45


def _parse_result(result: Any) -> dict[str, Any]:
    """Parse MCP tool result to dict.

    Args:
        result: Result from MCP tool call (can be dict, JSON string, or other).

    Returns:
        Parsed dict, or empty dict if parsing fails.
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


async def _find_matching_document_type(
    prismind: PrismindAdapter,
    doc_type_name: str,
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
) -> dict[str, Any] | None:
    """Find a semantically similar document type using RAG-based search.

    Uses BGE-M3 embeddings for multilingual semantic matching.
    For example, "api仕様" can match "api_spec".

    Args:
        prismind: Prismind adapter instance.
        doc_type_name: The document type name to search for.
        threshold: Minimum similarity score (0.0-1.0).

    Returns:
        Dict with match info if found, None otherwise:
        - use_existing: True
        - matched_type_id: The matched type ID
        - matched_name: The matched type name
        - similarity: Similarity score
    """
    logger.info(
        "Searching for similar document type (RAG-based)",
        doc_type_name=doc_type_name,
        threshold=threshold,
    )

    try:
        result = await prismind.find_similar_document_type(
            type_query=doc_type_name,
            threshold=threshold,
        )

        if result.get("found"):
            logger.info(
                "RAG semantic match found",
                original=doc_type_name,
                matched_type_id=result.get("type_id"),
                similarity=result.get("similarity", 0.0),
            )
            return {
                "use_existing": True,
                "matched_type_id": result["type_id"],
                "matched_name": result.get("name", ""),
                "similarity": result.get("similarity", 0.0),
            }

        logger.debug(
            "No semantic match found for document type",
            doc_type_name=doc_type_name,
        )
        return None

    except Exception as e:
        logger.warning(
            "RAG semantic search failed, will proceed with new type creation",
            error=str(e),
        )
        return None


async def _generate_new_type_metadata(
    lexora: LexoraAdapter,
    doc_type_name: str,
    content_preview: str,
) -> dict[str, Any]:
    """Generate metadata for a new document type using LLM.

    This function is ONLY called when RAG semantic search finds no match.
    The LLM does NOT make matching decisions - it only generates metadata
    for the genuinely new document type.

    Args:
        lexora: Lexora adapter instance.
        doc_type_name: The document type name to classify.
        content_preview: Preview of the document content.

    Returns:
        Dict containing type_id, name, folder_name, description.
    """
    # Normalize the doc_type_name to a valid type_id format (ASCII only)
    normalized_type_id = doc_type_name.lower().replace("-", "_").replace(" ", "_")
    # Keep only ASCII alphanumeric characters and underscore
    normalized_type_id = "".join(
        c for c in normalized_type_id if c.isascii() and (c.isalnum() or c == "_")
    )

    # If normalized_type_id is empty (e.g., Japanese input), let LLM generate it
    has_valid_type_id = bool(normalized_type_id)

    if has_valid_type_id:
        prompt = f"""You are a document type metadata generator.

Generate metadata for a NEW document type named "{doc_type_name}".

【CRITICAL】
The type_id MUST be "{normalized_type_id}" exactly (already normalized).

【Document Content Preview (for context only)】
{content_preview[:300]}

【Requirements】
- type_id: Use "{normalized_type_id}" exactly
- name: Human-readable display name for "{doc_type_name}"
- folder_name: English only, PascalCase (e.g., "MeetingNotes", "APISpecs")
- description: Brief description (1 sentence)

【Output Format】JSON only, no explanation.
{{
    "type_id": "{normalized_type_id}",
    "name": "Meeting Notes",
    "folder_name": "MeetingNotes",
    "description": "Records of meeting discussions and decisions"
}}"""
    else:
        # Non-ASCII input (e.g., Japanese) - LLM must generate English type_id
        prompt = f"""You are a document type metadata generator.

Generate metadata for a NEW document type named "{doc_type_name}" (translate to English).

【CRITICAL】
Generate an appropriate English type_id based on the meaning of "{doc_type_name}".
type_id MUST be lowercase ASCII English with underscores only.

【Document Content Preview (for context only)】
{content_preview[:300]}

【Requirements】
- type_id: Lowercase English with underscores (e.g., meeting_notes, api_spec)
- name: Human-readable display name (can be Japanese)
- folder_name: English only, PascalCase (e.g., "MeetingNotes", "APISpecs")
- description: Brief description (1 sentence)

【Examples】
- "議事録" → type_id: "meeting_notes"
- "設計書" → type_id: "design"
- "仕様書" → type_id: "specification"

【Output Format】JSON only, no explanation.
{{
    "type_id": "meeting_notes",
    "name": "議事録",
    "folder_name": "MeetingNotes",
    "description": "Records of meeting discussions and decisions"
}}"""

    logger.info(
        "Generating new document type metadata with Lexora",
        doc_type_name=doc_type_name,
    )

    try:
        response = await lexora.generate(
            prompt=prompt,
            max_tokens=200,
            temperature=0.3,
        )

        # Parse JSON from response
        response = response.strip()
        if response.startswith("```"):
            # Remove markdown code blocks
            lines = response.split("\n")
            json_lines = []
            in_json = False
            for line in lines:
                if line.startswith("```"):
                    in_json = not in_json
                    continue
                if in_json or (not line.startswith("```") and "{" in line):
                    json_lines.append(line)
            response = "\n".join(json_lines)

        # Find first complete JSON object in response
        start_idx = response.find("{")
        if start_idx >= 0:
            # Find matching closing brace for the first opening brace
            depth = 0
            end_idx = start_idx
            for i, char in enumerate(response[start_idx:], start_idx):
                if char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth == 0:
                        end_idx = i + 1
                        break

            if end_idx > start_idx:
                json_str = response[start_idx:end_idx]
                logger.debug("Extracted JSON string", json_str=json_str[:200])

                try:
                    result = json.loads(json_str)
                except json.JSONDecodeError:
                    # Try to fix common issues
                    json_str_clean = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', json_str)
                    result = json.loads(json_str_clean)

                # Validate required fields
                if not all(k in result for k in ["type_id", "name", "folder_name"]):
                    raise ValueError(f"Missing required fields in metadata: {result}")

                return result

        raise ValueError(f"No valid JSON found in Lexora response: {response[:200]}")

    except json.JSONDecodeError as e:
        logger.error("Failed to parse Lexora response as JSON", error=str(e))
        raise
    except Exception as e:
        logger.error("Document type metadata generation failed", error=str(e))
        raise


async def smart_create_document_impl(
    settings: Settings,
    name: str,
    doc_type: str,
    content: str,
    phase_task: str,
    project: str = "",
    feature: str = "",
    keywords: list[str] | None = None,
    auto_register_type: bool = True,
    user: str = "",
) -> dict[str, Any]:
    """Smart document creation that handles unknown document types.

    This is the shared implementation used by both the MCP tool and
    orchestrate_workflow's create_document action.

    When an unknown doc_type is provided, this function:
    1. Uses RAG semantic search (BGE-M3) to find similar existing types
    2. If a match is found (e.g., "api仕様" ≈ "api_spec"), uses existing type
    3. If no match, uses LLM to generate metadata for a new type

    Args:
        settings: Application settings.
        name: Document name.
        doc_type: Document type (can be unregistered).
        content: Document content.
        phase_task: Phase-task ID.
        project: Project identifier.
        feature: Feature name.
        keywords: Search keywords.
        auto_register_type: Whether to auto-register unknown types.
        user: User identifier for multi-user support.

    Returns:
        Dict containing:
        - success: Whether creation succeeded
        - doc_id: Document ID
        - doc_url: Document URL
        - doc_type: Used document type (may differ from input if matched existing)
        - type_registered: Whether a new type was registered
        - registered_type: Details of registered type (if any)
        - matched_existing: Whether an existing type was matched semantically
        - message: Status message
    """
    # Auto-detect user if not specified
    effective_user = user or get_current_user()

    prismind = PrismindAdapter(
        sse_url=settings.prismind_url,
        timeout=settings.prismind_timeout,
    )

    type_registered = False
    registered_type = None
    matched_existing = False  # True if semantic match found existing type

    logger.info(
        "Smart document creation",
        name=name,
        doc_type=doc_type,
        project=project,
    )

    # Step 1: Get existing document types to check for exact match
    try:
        types_result_raw = await prismind.list_document_types()
        types_result = _parse_result(types_result_raw)
        existing_types = types_result.get("document_types", [])
    except Exception as e:
        logger.warning("Failed to list document types, assuming none", error=str(e))
        existing_types = []

    # Step 2: Check if doc_type exists (exact match)
    existing_type_ids = [t.get("type_id", "") for t in existing_types]
    type_exists = doc_type in existing_type_ids

    logger.debug(
        "Document type check",
        doc_type=doc_type,
        type_exists=type_exists,
        existing_count=len(existing_types),
    )

    # Step 3: If type doesn't exist and auto_register is enabled, try semantic match
    if not type_exists and auto_register_type:
        try:
            # Step 3a: Try RAG-based semantic matching first
            semantic_match = await _find_matching_document_type(
                prismind=prismind,
                doc_type_name=doc_type,
                threshold=DEFAULT_SIMILARITY_THRESHOLD,
            )

            if semantic_match:
                # Found a semantic match - use existing type
                matched_type_id = semantic_match["matched_type_id"]
                original_type = doc_type
                doc_type = matched_type_id
                matched_existing = True
                logger.info(
                    "Using existing document type (RAG semantic match)",
                    original_type=original_type,
                    matched_type_id=matched_type_id,
                    similarity=semantic_match.get("similarity", 0.0),
                )
                # No registration needed, type already exists
            else:
                # Step 3b: No semantic match - generate metadata for new type
                lexora = LexoraAdapter(
                    base_url=settings.lexora_url,
                    timeout=settings.lexora_timeout,
                )

                new_type_metadata = await _generate_new_type_metadata(
                    lexora=lexora,
                    doc_type_name=doc_type,
                    content_preview=content,
                )

                # Check if generated type_id already exists
                if new_type_metadata.get("type_id") in existing_type_ids:
                    # LLM generated same ID as existing - use existing type
                    matched_type_id = new_type_metadata["type_id"]
                    original_type = doc_type
                    doc_type = matched_type_id
                    matched_existing = True
                    logger.info(
                        "Using existing document type (generated type_id match)",
                        original_type=original_type,
                        matched_type_id=matched_type_id,
                    )
                else:
                    # Register the new document type
                    logger.info(
                        "Registering new document type",
                        type_id=new_type_metadata.get("type_id"),
                        folder_name=new_type_metadata.get("folder_name"),
                    )

                    register_result_raw = await prismind.register_document_type(
                        type_id=new_type_metadata["type_id"],
                        name=new_type_metadata["name"],
                        folder_name=new_type_metadata["folder_name"],
                        scope="global",  # Register as global type for cross-project use
                        description=new_type_metadata.get("description", ""),
                        create_folder=True,
                    )
                    register_result = _parse_result(register_result_raw)

                    if register_result.get("success"):
                        type_registered = True
                        registered_type = {
                            "type_id": new_type_metadata["type_id"],
                            "name": new_type_metadata["name"],
                            "folder_name": new_type_metadata["folder_name"],
                            "description": new_type_metadata.get("description", ""),
                        }
                        # Use the new type_id for document creation
                        doc_type = new_type_metadata["type_id"]

                        logger.info(
                            "Document type registered",
                            type_id=doc_type,
                            folder_name=new_type_metadata["folder_name"],
                        )
                    else:
                        logger.warning(
                            "Document type registration returned non-success",
                            result=register_result,
                        )

        except Exception as e:
            logger.error("Failed to match/register document type", error=str(e))
            if not auto_register_type:
                return {
                    "success": False,
                    "doc_id": "",
                    "doc_url": "",
                    "doc_type": doc_type,
                    "type_registered": False,
                    "registered_type": None,
                    "message": f"Document type '{doc_type}' not found and auto-registration failed: {e}",
                }
            # Continue with original doc_type, Prismind may handle it

    # Step 4: Create the document
    try:
        create_kwargs: dict[str, Any] = {
            "doc_type": doc_type,
            "name": name,
            "content": content,
            "phase_task": phase_task,
        }
        if project:
            create_kwargs["project"] = project
        if feature:
            create_kwargs["feature"] = feature
        if keywords is not None:
            create_kwargs["keywords"] = keywords

        create_result_raw = await prismind.create_document(**create_kwargs)
        create_result = _parse_result(create_result_raw)

        success = create_result.get("success", False)
        doc_id = create_result.get("doc_id", "")
        doc_url = create_result.get("doc_url", "")
        message = create_result.get("message", "Document created")

        # Check if Prismind flagged unknown doc type
        if create_result.get("unknown_doc_type") and not type_registered:
            message += " (Warning: document type may not be registered)"

        logger.info(
            "Document created",
            doc_id=doc_id,
            type_registered=type_registered,
        )

        return {
            "success": success,
            "doc_id": doc_id,
            "doc_url": doc_url,
            "doc_type": doc_type,
            "type_registered": type_registered,
            "registered_type": registered_type,
            "matched_existing": matched_existing,
            "message": message,
        }

    except Exception as e:
        logger.error("Document creation failed", error=str(e))
        return {
            "success": False,
            "doc_id": "",
            "doc_url": "",
            "doc_type": doc_type,
            "type_registered": type_registered,
            "registered_type": registered_type,
            "matched_existing": matched_existing,
            "message": f"Document creation failed: {e}",
        }


def register_tools(mcp: FastMCP, settings: Settings) -> None:
    """Register document management tools with the MCP server.

    Args:
        mcp: FastMCP server instance.
        settings: Application settings.
    """
    global _settings
    _settings = settings

    @mcp.tool()
    async def smart_create_document(
        name: str,
        doc_type: str,
        content: str,
        phase_task: str,
        project: str = "",
        feature: str = "",
        keywords: list[str] | None = None,
        auto_register_type: bool = True,
        user: str = "",
    ) -> dict[str, Any]:
        """Create a document with automatic document type handling.

        USE THIS WHEN: Creating documents where the document type may not exist.
        This tool:
        - Checks if the document type is registered in Prismind
        - If unknown, uses RAG semantic search (BGE-M3) to find similar types
          (e.g., "api仕様" matches "api_spec" across languages)
        - If match found, uses existing type; otherwise generates new type metadata
        - Folder names are always in English to prevent notation variations
        - Creates the document in the appropriate folder

        DO NOT USE WHEN:
        - You know the document type exists -> use Prismind create_document directly
        - You want to register a type without creating a document -> use register_document_type

        Args:
            name: Document name (e.g., "2024-01-15 Sprint Planning").
            doc_type: Document type - can be unregistered (e.g., "meeting_notes").
            content: Document content.
            phase_task: Phase-task ID (e.g., "phase1-task2").
            project: Optional project identifier.
            feature: Optional feature name.
            keywords: Optional search keywords.
            auto_register_type: If True, auto-register unknown types (default: True).
            user: User identifier for multi-user support (auto-detected if empty).

        Returns:
            Dict containing:
            - success: Whether creation succeeded
            - doc_id: Created document ID
            - doc_url: Document URL (Google Drive)
            - doc_type: The document type used (may differ if matched existing)
            - type_registered: Whether a new type was registered
            - registered_type: Details of registered type (type_id, name, folder_name, description)
            - matched_existing: Whether an existing type was matched semantically
            - message: Status message
        """
        if _settings is None:
            raise RuntimeError("Settings not initialized")

        return await smart_create_document_impl(
            settings=_settings,
            name=name,
            doc_type=doc_type,
            content=content,
            phase_task=phase_task,
            project=project,
            feature=feature,
            keywords=keywords,
            auto_register_type=auto_register_type,
            user=user,
        )
