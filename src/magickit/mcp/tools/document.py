"""Document management tools for Magickit MCP server.

Provides smart document creation that automatically handles unknown document types
by classifying with Lexora and registering with Prismind.
"""

from __future__ import annotations

import json
from typing import Any

from fastmcp import FastMCP

from magickit.adapters.lexora import LexoraAdapter
from magickit.adapters.prismind import PrismindAdapter
from magickit.config import Settings
from magickit.utils.logging import get_logger

logger = get_logger(__name__)

# Module-level settings reference
_settings: Settings | None = None


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


async def _classify_document_type(
    lexora: LexoraAdapter,
    doc_type_name: str,
    content_preview: str,
    existing_types: list[dict[str, Any]],
) -> dict[str, Any]:
    """Classify a document type using Lexora LLM.

    Args:
        lexora: Lexora adapter instance.
        doc_type_name: The document type name to classify.
        content_preview: Preview of the document content.
        existing_types: List of existing document types from Prismind.

    Returns:
        Dict containing type_id, name, folder_name, description.
    """
    # Format existing types for the prompt
    existing_types_json = json.dumps(
        [
            {
                "type_id": t.get("type_id", ""),
                "name": t.get("name", ""),
                "folder_name": t.get("folder_name", ""),
                "description": t.get("description", ""),
            }
            for t in existing_types
        ],
        ensure_ascii=False,
        indent=2,
    )

    prompt = f"""あなたはドキュメント分類の専門家です。

新しいドキュメントタイプを登録する必要があります。
以下の情報から、適切なtype_id、表示名、フォルダパスを決定してください。

【登録したいドキュメントタイプ名】
{doc_type_name}

【ドキュメント内容の一部】
{content_preview[:500]}

【既存のドキュメントタイプ】
{existing_types_json}

【ルール】
- type_idは英数字とアンダースコアのみ（例: meeting_notes）
- folder_nameはネストパスが使用可能（例: "議事録/定例", "設計/詳細設計"）
- 既存タイプと重複しないこと
- 日本語のフォルダ名を推奨

【出力形式】JSON形式のみで出力。余計な説明は不要。
{{
    "type_id": "meeting_notes",
    "name": "議事録",
    "folder_name": "議事録",
    "description": "会議の議事録を保存"
}}"""

    logger.info(
        "Classifying document type with Lexora",
        doc_type_name=doc_type_name,
    )

    try:
        response = await lexora.generate(
            prompt=prompt,
            max_tokens=300,
            temperature=0.3,
        )

        # Parse JSON from response
        # Try to extract JSON from the response
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
                    # Try to fix common issues: unquoted values, trailing commas
                    # Remove any control characters
                    import re
                    json_str_clean = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', json_str)
                    result = json.loads(json_str_clean)

                # Validate required fields
                if not all(k in result for k in ["type_id", "name", "folder_name"]):
                    raise ValueError(f"Missing required fields in classification result: {result}")

                return result

        raise ValueError(f"No valid JSON found in Lexora response: {response[:200]}")

    except json.JSONDecodeError as e:
        logger.error("Failed to parse Lexora response as JSON", error=str(e))
        raise
    except Exception as e:
        logger.error("Document type classification failed", error=str(e))
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
) -> dict[str, Any]:
    """Smart document creation that handles unknown document types.

    This is the shared implementation used by both the MCP tool and
    orchestrate_workflow's create_document action.

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

    Returns:
        Dict containing:
        - success: Whether creation succeeded
        - doc_id: Document ID
        - doc_url: Document URL
        - doc_type: Used document type
        - type_registered: Whether a new type was registered
        - registered_type: Details of registered type (if any)
        - message: Status message
    """
    prismind = PrismindAdapter(
        sse_url=settings.prismind_url,
        timeout=settings.prismind_timeout,
    )

    type_registered = False
    registered_type = None

    logger.info(
        "Smart document creation",
        name=name,
        doc_type=doc_type,
        project=project,
    )

    # Step 1: Get existing document types
    try:
        types_result = await prismind.list_document_types()
        if isinstance(types_result, dict):
            existing_types = types_result.get("document_types", [])
        else:
            existing_types = []
    except Exception as e:
        logger.warning("Failed to list document types, assuming none", error=str(e))
        existing_types = []

    # Step 2: Check if doc_type exists
    existing_type_ids = [t.get("type_id", "") for t in existing_types]
    type_exists = doc_type in existing_type_ids

    logger.debug(
        "Document type check",
        doc_type=doc_type,
        type_exists=type_exists,
        existing_count=len(existing_types),
    )

    # Step 3: If type doesn't exist and auto_register is enabled, classify and register
    if not type_exists and auto_register_type:
        lexora = LexoraAdapter(
            base_url=settings.lexora_url,
            timeout=settings.lexora_timeout,
        )

        try:
            # Classify the document type
            classification = await _classify_document_type(
                lexora=lexora,
                doc_type_name=doc_type,
                content_preview=content,
                existing_types=existing_types,
            )

            logger.info(
                "Document type classified",
                type_id=classification.get("type_id"),
                folder_name=classification.get("folder_name"),
            )

            # Register the new document type
            register_result_raw = await prismind.register_document_type(
                type_id=classification["type_id"],
                name=classification["name"],
                folder_name=classification["folder_name"],
                description=classification.get("description", ""),
                create_folder=True,
            )
            register_result = _parse_result(register_result_raw)

            if register_result.get("success"):
                type_registered = True
                registered_type = {
                    "type_id": classification["type_id"],
                    "name": classification["name"],
                    "folder_name": classification["folder_name"],
                    "description": classification.get("description", ""),
                }
                # Use the classified type_id for document creation
                doc_type = classification["type_id"]

                logger.info(
                    "Document type registered",
                    type_id=doc_type,
                    folder_name=classification["folder_name"],
                )
            else:
                logger.warning(
                    "Document type registration returned non-success",
                    result=register_result,
                )

        except Exception as e:
            logger.error("Failed to classify/register document type", error=str(e))
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
    ) -> dict[str, Any]:
        """Create a document with automatic document type handling.

        USE THIS WHEN: Creating documents where the document type may not exist.
        This tool:
        - Checks if the document type is registered in Prismind
        - If unknown, uses Lexora to classify and suggest type_id/folder structure
        - Automatically registers the new document type in Prismind
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

        Returns:
            Dict containing:
            - success: Whether creation succeeded
            - doc_id: Created document ID
            - doc_url: Document URL (Google Drive)
            - doc_type: The document type used (may differ if classified)
            - type_registered: Whether a new type was registered
            - registered_type: Details of registered type (type_id, name, folder_name, description)
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
        )
