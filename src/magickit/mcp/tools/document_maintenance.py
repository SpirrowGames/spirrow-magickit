"""Document maintenance tools for Magickit MCP server.

Provides tools for document and knowledge cleanup, orphan detection,
and consistency checking.
"""

from __future__ import annotations

import json
from typing import Any

from fastmcp import FastMCP

from magickit.adapters.prismind import PrismindAdapter
from magickit.config import Settings
from magickit.utils.logging import get_logger
from magickit.utils.user import get_current_user

logger = get_logger(__name__)

# Module-level settings reference
_settings: Settings | None = None


def _parse_result(result: Any) -> dict[str, Any]:
    """Parse MCP tool result to dict."""
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


def _parse_list_result(result: Any) -> list[dict[str, Any]]:
    """Parse MCP tool result to list."""
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
                for key in ["results", "items", "documents", "knowledge", "projects"]:
                    if key in data and isinstance(data[key], list):
                        return data[key]
                return [data]
            return [{"result": data}]
        except json.JSONDecodeError:
            return [{"result": result}]
    return [{"result": result}]


async def _smart_delete_document_impl(
    settings: Settings,
    doc_id: str,
    project: str = "",
    delete_related_knowledge: bool = True,
    delete_drive_file: bool = True,
    permanent: bool = False,
    dry_run: bool = False,
    user: str = "",
) -> dict[str, Any]:
    """Implementation of smart document deletion.

    Args:
        settings: Application settings.
        doc_id: Document ID to delete.
        project: Project identifier for filtering.
        delete_related_knowledge: Whether to delete related knowledge entries.
        delete_drive_file: Whether to delete the Google Drive file.
        permanent: True for permanent deletion, False for trash.
        dry_run: If True, only preview what would be deleted.
        user: User identifier for multi-user support.

    Returns:
        Dict containing deletion results and statistics.
    """
    effective_user = user or get_current_user()

    prismind = PrismindAdapter(
        sse_url=settings.prismind_url,
        timeout=settings.prismind_timeout,
    )

    result: dict[str, Any] = {
        "success": False,
        "doc_id": doc_id,
        "dry_run": dry_run,
        "document_deleted": False,
        "drive_file_deleted": False,
        "related_knowledge": [],
        "knowledge_deleted_count": 0,
        "message": "",
    }

    # Step 1: Verify document exists and get its info
    try:
        doc_result_raw = await prismind.get_document(doc_id=doc_id)
        doc_result = _parse_result(doc_result_raw)

        if not doc_result.get("found", False) and not doc_result.get("doc_id"):
            result["message"] = f"Document '{doc_id}' not found"
            return result

        doc_info = doc_result.get("document", doc_result)
        result["document_info"] = {
            "name": doc_info.get("name", ""),
            "doc_type": doc_info.get("doc_type", ""),
            "project": doc_info.get("project", project),
        }
        logger.info("Document found", doc_id=doc_id, name=doc_info.get("name", ""))
    except Exception as e:
        logger.warning("Failed to get document info", doc_id=doc_id, error=str(e))
        result["message"] = f"Failed to get document info: {e}"
        return result

    # Step 2: Find related knowledge entries
    related_knowledge: list[dict[str, Any]] = []
    if delete_related_knowledge:
        try:
            # Search for knowledge referencing this document
            search_result = await prismind.search_knowledge(
                query=f"doc:{doc_id}",
                limit=100,
                user=effective_user,
            )

            # Also search by source field
            for entry in search_result:
                source = entry.get("source", "")
                if doc_id in source or entry.get("doc_id") == doc_id:
                    related_knowledge.append({
                        "knowledge_id": entry.get("id", entry.get("knowledge_id", "")),
                        "category": entry.get("category", ""),
                        "source": source,
                        "content_preview": entry.get("content", "")[:100],
                    })

            result["related_knowledge"] = related_knowledge
            logger.info(
                "Found related knowledge entries",
                doc_id=doc_id,
                count=len(related_knowledge),
            )
        except Exception as e:
            logger.warning(
                "Failed to search related knowledge",
                doc_id=doc_id,
                error=str(e),
            )

    # Step 3: If dry_run, return preview
    if dry_run:
        result["success"] = True
        result["message"] = (
            f"Dry run: Would delete document '{doc_id}' "
            f"and {len(related_knowledge)} related knowledge entries"
        )
        result["would_delete"] = {
            "document": doc_id,
            "drive_file": delete_drive_file,
            "permanent": permanent,
            "knowledge_entries": [k["knowledge_id"] for k in related_knowledge],
        }
        return result

    # Step 4: Delete related knowledge entries
    deleted_knowledge_count = 0
    if delete_related_knowledge and related_knowledge:
        for entry in related_knowledge:
            knowledge_id = entry.get("knowledge_id")
            if not knowledge_id:
                continue
            try:
                await prismind.delete_knowledge(
                    knowledge_id=knowledge_id,
                    project=project,
                    user=effective_user,
                )
                deleted_knowledge_count += 1
            except Exception as e:
                logger.warning(
                    "Failed to delete knowledge entry",
                    knowledge_id=knowledge_id,
                    error=str(e),
                )
        result["knowledge_deleted_count"] = deleted_knowledge_count

    # Step 5: Delete the document
    try:
        delete_result_raw = await prismind.delete_document(
            doc_id=doc_id,
            project=project,
            delete_drive_file=delete_drive_file,
            permanent=permanent,
        )
        delete_result = _parse_result(delete_result_raw)

        if delete_result.get("success"):
            result["document_deleted"] = True
            result["drive_file_deleted"] = delete_result.get("drive_file_deleted", False)
            result["success"] = True
            result["message"] = (
                f"Document '{doc_id}' deleted"
                + (f", {deleted_knowledge_count} knowledge entries removed" if deleted_knowledge_count > 0 else "")
            )
        else:
            result["message"] = delete_result.get("message", "Document deletion failed")

    except Exception as e:
        logger.error("Document deletion failed", doc_id=doc_id, error=str(e))
        result["message"] = f"Document deletion failed: {e}"

    return result


async def _detect_orphan_documents_impl(
    settings: Settings,
    project: str = "",
    include_deleted_projects: bool = True,
    include_invalid_phase_task: bool = True,
    include_missing_doc_type: bool = True,
    limit: int = 100,
    user: str = "",
) -> dict[str, Any]:
    """Implementation of orphan document detection.

    Args:
        settings: Application settings.
        project: Filter by project (empty for all).
        include_deleted_projects: Check for docs in deleted projects.
        include_invalid_phase_task: Check for invalid phase_task references.
        include_missing_doc_type: Check for unregistered doc_types.
        limit: Maximum documents to return.
        user: User identifier for multi-user support.

    Returns:
        Dict containing detected orphan documents.
    """
    effective_user = user or get_current_user()

    prismind = PrismindAdapter(
        sse_url=settings.prismind_url,
        timeout=settings.prismind_timeout,
    )

    result: dict[str, Any] = {
        "success": False,
        "orphans": [],
        "deleted_project_docs": [],
        "invalid_phase_task_docs": [],
        "missing_doc_type_docs": [],
        "total_checked": 0,
        "total_orphans": 0,
        "message": "",
    }

    # Get all projects (including archived for reference)
    try:
        projects_result = await prismind.list_projects(include_archived=True)
        projects_data = _parse_result(projects_result)
        all_projects = projects_data.get("projects", [])
        active_projects = {
            p.get("project", p.get("name", ""))
            for p in all_projects
            if p.get("status") != "archived" and p.get("status") != "deleted"
        }
        archived_projects = {
            p.get("project", p.get("name", ""))
            for p in all_projects
            if p.get("status") == "archived" or p.get("status") == "deleted"
        }
    except Exception as e:
        logger.warning("Failed to list projects", error=str(e))
        active_projects = set()
        archived_projects = set()

    # Get registered document types
    registered_types: set[str] = set()
    if include_missing_doc_type:
        try:
            types_result = await prismind.list_document_types()
            types_data = _parse_result(types_result)
            for t in types_data.get("document_types", []):
                registered_types.add(t.get("type_id", ""))
        except Exception as e:
            logger.warning("Failed to list document types", error=str(e))

    # Get documents to check
    try:
        # Use search to get documents
        docs_result = await prismind.search_documents(
            query="*",
            project=project,
            limit=limit,
        )
        documents = _parse_list_result(docs_result)
        result["total_checked"] = len(documents)
    except Exception as e:
        logger.warning("Failed to search documents", error=str(e))
        result["message"] = f"Failed to search documents: {e}"
        return result

    # Check each document
    for doc in documents:
        doc_id = doc.get("doc_id", doc.get("id", ""))
        doc_project = doc.get("project", "")
        doc_type = doc.get("doc_type", "")
        phase_task = doc.get("phase_task", "")

        orphan_reasons = []

        # Check 1: Document belongs to deleted/archived project
        if include_deleted_projects and doc_project:
            if doc_project in archived_projects:
                orphan_reasons.append("deleted_project")
                result["deleted_project_docs"].append({
                    "doc_id": doc_id,
                    "project": doc_project,
                    "name": doc.get("name", ""),
                })

        # Check 2: Invalid phase_task
        if include_invalid_phase_task and phase_task:
            # Basic validation: phase_task should follow pattern like "phase1-task2"
            if doc_project and doc_project in active_projects:
                try:
                    progress_result = await prismind.get_progress(
                        project=doc_project,
                        user=effective_user,
                    )
                    progress_data = _parse_result(progress_result)
                    phases = progress_data.get("phases", [])

                    # Extract valid phase-task combinations
                    valid_phase_tasks: set[str] = set()
                    for phase in phases:
                        phase_name = phase.get("name", "")
                        for task in phase.get("tasks", []):
                            task_id = task.get("task_id", "")
                            # Various formats: "phase1-task1", "Phase 1-T01", etc.
                            valid_phase_tasks.add(f"{phase_name}-{task_id}".lower())

                    if phase_task.lower() not in valid_phase_tasks:
                        orphan_reasons.append("invalid_phase_task")
                        result["invalid_phase_task_docs"].append({
                            "doc_id": doc_id,
                            "phase_task": phase_task,
                            "project": doc_project,
                            "name": doc.get("name", ""),
                        })
                except Exception as e:
                    logger.debug(
                        "Could not validate phase_task",
                        phase_task=phase_task,
                        error=str(e),
                    )

        # Check 3: Unregistered doc_type
        if include_missing_doc_type and doc_type:
            if doc_type not in registered_types:
                orphan_reasons.append("missing_doc_type")
                result["missing_doc_type_docs"].append({
                    "doc_id": doc_id,
                    "doc_type": doc_type,
                    "project": doc_project,
                    "name": doc.get("name", ""),
                })

        if orphan_reasons:
            result["orphans"].append({
                "doc_id": doc_id,
                "name": doc.get("name", ""),
                "project": doc_project,
                "doc_type": doc_type,
                "reasons": orphan_reasons,
            })

    result["total_orphans"] = len(result["orphans"])
    result["success"] = True
    result["message"] = (
        f"Checked {result['total_checked']} documents, "
        f"found {result['total_orphans']} orphans"
    )

    return result


async def _detect_orphan_knowledge_impl(
    settings: Settings,
    project: str = "",
    check_document_refs: bool = True,
    check_task_refs: bool = True,
    limit: int = 500,
    user: str = "",
) -> dict[str, Any]:
    """Implementation of orphan knowledge detection.

    Args:
        settings: Application settings.
        project: Filter by project (empty for all).
        check_document_refs: Check for invalid document references.
        check_task_refs: Check for invalid task references.
        limit: Maximum knowledge entries to check.
        user: User identifier for multi-user support.

    Returns:
        Dict containing detected orphan knowledge entries.
    """
    effective_user = user or get_current_user()

    prismind = PrismindAdapter(
        sse_url=settings.prismind_url,
        timeout=settings.prismind_timeout,
    )

    result: dict[str, Any] = {
        "success": False,
        "orphans": [],
        "invalid_document_refs": [],
        "invalid_task_refs": [],
        "total_checked": 0,
        "total_orphans": 0,
        "message": "",
    }

    # Get knowledge entries
    try:
        search_result = await prismind.search_knowledge(
            query="*",
            project=project,
            limit=limit,
            user=effective_user,
        )
        knowledge_entries = _parse_list_result(search_result)
        result["total_checked"] = len(knowledge_entries)
    except Exception as e:
        logger.warning("Failed to search knowledge", error=str(e))
        result["message"] = f"Failed to search knowledge: {e}"
        return result

    # Cache for document existence checks
    doc_exists_cache: dict[str, bool] = {}

    # Check each knowledge entry
    for entry in knowledge_entries:
        knowledge_id = entry.get("id", entry.get("knowledge_id", ""))
        source = entry.get("source", "")
        category = entry.get("category", "")
        entry_project = entry.get("project", "")

        orphan_reasons = []

        # Check 1: Invalid document reference
        if check_document_refs and source:
            # Extract doc_id from source (format: "doc:xxx" or just document ID)
            doc_id = ""
            if source.startswith("doc:"):
                doc_id = source[4:]
            elif source.startswith("document:"):
                doc_id = source[9:]

            if doc_id:
                # Check cache first
                if doc_id not in doc_exists_cache:
                    try:
                        doc_result = await prismind.get_document(doc_id=doc_id)
                        doc_data = _parse_result(doc_result)
                        doc_exists_cache[doc_id] = doc_data.get("found", False) or bool(doc_data.get("doc_id"))
                    except Exception:
                        doc_exists_cache[doc_id] = False

                if not doc_exists_cache[doc_id]:
                    orphan_reasons.append("invalid_document_ref")
                    result["invalid_document_refs"].append({
                        "knowledge_id": knowledge_id,
                        "source": source,
                        "category": category,
                        "project": entry_project,
                    })

        # Check 2: Invalid task reference
        if check_task_refs and category:
            # Check if category references a task (e.g., "task:T01", "タスク完了:T01")
            task_ref = None
            if "task:" in category.lower() or "タスク" in category:
                # Extract task ID
                import re
                task_match = re.search(r'[Tt](\d+)', category)
                if task_match:
                    task_ref = f"T{task_match.group(1).zfill(2)}"

            if task_ref and entry_project:
                try:
                    progress_result = await prismind.get_progress(
                        project=entry_project,
                        user=effective_user,
                    )
                    progress_data = _parse_result(progress_result)

                    # Collect all task IDs
                    all_task_ids: set[str] = set()
                    for phase in progress_data.get("phases", []):
                        for task in phase.get("tasks", []):
                            all_task_ids.add(task.get("task_id", "").upper())

                    if task_ref.upper() not in all_task_ids:
                        orphan_reasons.append("invalid_task_ref")
                        result["invalid_task_refs"].append({
                            "knowledge_id": knowledge_id,
                            "task_ref": task_ref,
                            "category": category,
                            "project": entry_project,
                        })
                except Exception as e:
                    logger.debug(
                        "Could not validate task reference",
                        task_ref=task_ref,
                        error=str(e),
                    )

        if orphan_reasons:
            result["orphans"].append({
                "knowledge_id": knowledge_id,
                "source": source,
                "category": category,
                "project": entry_project,
                "content_preview": entry.get("content", "")[:100],
                "reasons": orphan_reasons,
            })

    result["total_orphans"] = len(result["orphans"])
    result["success"] = True
    result["message"] = (
        f"Checked {result['total_checked']} knowledge entries, "
        f"found {result['total_orphans']} orphans"
    )

    return result


async def _detect_unused_document_types_impl(
    settings: Settings,
    scope: str = "all",
    project: str = "",
    include_semantic_duplicates: bool = True,
    duplicate_threshold: float = 0.75,
    user: str = "",
) -> dict[str, Any]:
    """Implementation of unused document type detection.

    Args:
        settings: Application settings.
        scope: Scope to check ("all", "global", "project").
        project: Project to check (for "project" scope).
        include_semantic_duplicates: Check for semantic duplicates using RAG.
        duplicate_threshold: Similarity threshold for duplicates.
        user: User identifier for multi-user support.

    Returns:
        Dict containing unused and duplicate document types.
    """
    effective_user = user or get_current_user()

    prismind = PrismindAdapter(
        sse_url=settings.prismind_url,
        timeout=settings.prismind_timeout,
    )

    result: dict[str, Any] = {
        "success": False,
        "unused_types": [],
        "semantic_duplicates": [],
        "total_types_checked": 0,
        "total_unused": 0,
        "total_duplicates": 0,
        "message": "",
    }

    # Get document types
    try:
        types_result = await prismind.list_document_types()
        types_data = _parse_result(types_result)
        document_types = types_data.get("document_types", [])
    except Exception as e:
        logger.warning("Failed to list document types", error=str(e))
        result["message"] = f"Failed to list document types: {e}"
        return result

    # Filter by scope
    filtered_types = []
    for t in document_types:
        t_scope = t.get("scope", "global")
        t_project = t.get("project", "")

        if scope == "all":
            filtered_types.append(t)
        elif scope == "global" and t_scope == "global":
            filtered_types.append(t)
        elif scope == "project" and t_scope == "project":
            if not project or t_project == project:
                filtered_types.append(t)

    result["total_types_checked"] = len(filtered_types)

    # Check usage of each type
    for doc_type in filtered_types:
        type_id = doc_type.get("type_id", "")
        if not type_id:
            continue

        # Search for documents using this type
        try:
            docs_result = await prismind.search_documents(
                query=f"doc_type:{type_id}",
                limit=1,
            )
            docs = _parse_list_result(docs_result)

            if not docs:
                result["unused_types"].append({
                    "type_id": type_id,
                    "name": doc_type.get("name", ""),
                    "scope": doc_type.get("scope", "global"),
                    "project": doc_type.get("project", ""),
                    "folder_name": doc_type.get("folder_name", ""),
                })
        except Exception as e:
            logger.debug(
                "Could not check document type usage",
                type_id=type_id,
                error=str(e),
            )

    result["total_unused"] = len(result["unused_types"])

    # Check for semantic duplicates
    if include_semantic_duplicates and len(filtered_types) > 1:
        checked_pairs: set[tuple[str, str]] = set()

        for i, type_a in enumerate(filtered_types):
            type_id_a = type_a.get("type_id", "")
            name_a = type_a.get("name", "")

            for type_b in filtered_types[i + 1:]:
                type_id_b = type_b.get("type_id", "")
                name_b = type_b.get("name", "")

                # Skip if already checked
                pair_key = tuple(sorted([type_id_a, type_id_b]))
                if pair_key in checked_pairs:
                    continue
                checked_pairs.add(pair_key)

                # Use RAG to check similarity
                try:
                    match_result = await prismind.find_similar_document_type(
                        type_query=name_a,
                        threshold=duplicate_threshold,
                    )

                    if match_result.get("found"):
                        matched_id = match_result.get("type_id", "")
                        similarity = match_result.get("similarity", 0.0)

                        if matched_id == type_id_b and similarity >= duplicate_threshold:
                            result["semantic_duplicates"].append({
                                "type_a": {
                                    "type_id": type_id_a,
                                    "name": name_a,
                                },
                                "type_b": {
                                    "type_id": type_id_b,
                                    "name": name_b,
                                },
                                "similarity": similarity,
                            })
                except Exception as e:
                    logger.debug(
                        "Could not check semantic similarity",
                        type_a=type_id_a,
                        type_b=type_id_b,
                        error=str(e),
                    )

    result["total_duplicates"] = len(result["semantic_duplicates"])
    result["success"] = True
    result["message"] = (
        f"Checked {result['total_types_checked']} document types: "
        f"{result['total_unused']} unused, {result['total_duplicates']} potential duplicates"
    )

    return result


async def _check_document_consistency_impl(
    settings: Settings,
    project: str = "",
    fix_issues: bool = False,
    dry_run: bool = True,
    user: str = "",
) -> dict[str, Any]:
    """Implementation of document consistency checking.

    Args:
        settings: Application settings.
        project: Project to check (empty for all).
        fix_issues: Whether to attempt automatic fixes.
        dry_run: If True, only report what would be fixed.
        user: User identifier for multi-user support.

    Returns:
        Dict containing consistency check results.
    """
    effective_user = user or get_current_user()

    result: dict[str, Any] = {
        "success": False,
        "project": project,
        "dry_run": dry_run,
        "checks_performed": [],
        "issues_found": [],
        "fixes_applied": [],
        "summary": {},
        "message": "",
    }

    # Run orphan document detection
    orphan_docs_result = await _detect_orphan_documents_impl(
        settings=settings,
        project=project,
        user=effective_user,
    )
    result["checks_performed"].append("orphan_documents")
    if orphan_docs_result.get("total_orphans", 0) > 0:
        result["issues_found"].append({
            "type": "orphan_documents",
            "count": orphan_docs_result["total_orphans"],
            "details": orphan_docs_result["orphans"][:10],  # Limit details
        })

    # Run orphan knowledge detection
    orphan_knowledge_result = await _detect_orphan_knowledge_impl(
        settings=settings,
        project=project,
        user=effective_user,
    )
    result["checks_performed"].append("orphan_knowledge")
    if orphan_knowledge_result.get("total_orphans", 0) > 0:
        result["issues_found"].append({
            "type": "orphan_knowledge",
            "count": orphan_knowledge_result["total_orphans"],
            "details": orphan_knowledge_result["orphans"][:10],
        })

    # Run unused document types detection
    unused_types_result = await _detect_unused_document_types_impl(
        settings=settings,
        project=project,
        user=effective_user,
    )
    result["checks_performed"].append("unused_document_types")
    if unused_types_result.get("total_unused", 0) > 0:
        result["issues_found"].append({
            "type": "unused_document_types",
            "count": unused_types_result["total_unused"],
            "details": unused_types_result["unused_types"][:10],
        })
    if unused_types_result.get("total_duplicates", 0) > 0:
        result["issues_found"].append({
            "type": "semantic_duplicate_types",
            "count": unused_types_result["total_duplicates"],
            "details": unused_types_result["semantic_duplicates"][:10],
        })

    # Summary
    result["summary"] = {
        "orphan_documents": orphan_docs_result.get("total_orphans", 0),
        "orphan_knowledge": orphan_knowledge_result.get("total_orphans", 0),
        "unused_document_types": unused_types_result.get("total_unused", 0),
        "semantic_duplicate_types": unused_types_result.get("total_duplicates", 0),
        "total_issues": sum(
            issue["count"] for issue in result["issues_found"]
        ),
    }

    # Apply fixes if requested and not dry_run
    if fix_issues and not dry_run:
        # Currently, automatic fixes are limited to prevent accidental data loss
        # Users should use cleanup_documents for explicit cleanup
        result["fixes_applied"].append({
            "note": "Automatic fixes not implemented. Use cleanup_documents for explicit cleanup.",
        })

    result["success"] = True
    result["message"] = (
        f"Consistency check complete: {result['summary']['total_issues']} issues found"
    )

    return result


async def _cleanup_documents_impl(
    settings: Settings,
    cleanup_orphan_documents: bool = False,
    cleanup_orphan_knowledge: bool = False,
    cleanup_unused_types: bool = False,
    project: str = "",
    confirm: bool = False,
    dry_run: bool = True,
    user: str = "",
) -> dict[str, Any]:
    """Implementation of document cleanup.

    Args:
        settings: Application settings.
        cleanup_orphan_documents: Whether to delete orphan documents.
        cleanup_orphan_knowledge: Whether to delete orphan knowledge.
        cleanup_unused_types: Whether to delete unused document types.
        project: Project to clean up (empty for all).
        confirm: Safety confirmation required for non-dry-run.
        dry_run: If True, only preview what would be deleted.
        user: User identifier for multi-user support.

    Returns:
        Dict containing cleanup results.
    """
    effective_user = user or get_current_user()

    result: dict[str, Any] = {
        "success": False,
        "dry_run": dry_run,
        "confirm": confirm,
        "deleted": {
            "documents": [],
            "knowledge": [],
            "document_types": [],
        },
        "counts": {
            "documents_deleted": 0,
            "knowledge_deleted": 0,
            "types_deleted": 0,
        },
        "errors": [],
        "message": "",
    }

    # Safety check
    if not dry_run and not confirm:
        result["message"] = "Cleanup requires confirm=True for non-dry-run execution"
        return result

    prismind = PrismindAdapter(
        sse_url=settings.prismind_url,
        timeout=settings.prismind_timeout,
    )

    # Cleanup orphan documents
    if cleanup_orphan_documents:
        orphan_docs_result = await _detect_orphan_documents_impl(
            settings=settings,
            project=project,
            user=effective_user,
        )

        for orphan in orphan_docs_result.get("orphans", []):
            doc_id = orphan.get("doc_id", "")
            if not doc_id:
                continue

            if dry_run:
                result["deleted"]["documents"].append({
                    "doc_id": doc_id,
                    "name": orphan.get("name", ""),
                    "reasons": orphan.get("reasons", []),
                    "would_delete": True,
                })
            else:
                try:
                    delete_result = await _smart_delete_document_impl(
                        settings=settings,
                        doc_id=doc_id,
                        project=project,
                        delete_related_knowledge=True,
                        permanent=False,  # Use trash by default
                        user=effective_user,
                    )
                    if delete_result.get("success"):
                        result["deleted"]["documents"].append({
                            "doc_id": doc_id,
                            "name": orphan.get("name", ""),
                            "deleted": True,
                        })
                        result["counts"]["documents_deleted"] += 1
                except Exception as e:
                    result["errors"].append({
                        "type": "document_deletion",
                        "doc_id": doc_id,
                        "error": str(e),
                    })

    # Cleanup orphan knowledge
    if cleanup_orphan_knowledge:
        orphan_knowledge_result = await _detect_orphan_knowledge_impl(
            settings=settings,
            project=project,
            user=effective_user,
        )

        for orphan in orphan_knowledge_result.get("orphans", []):
            knowledge_id = orphan.get("knowledge_id", "")
            if not knowledge_id:
                continue

            if dry_run:
                result["deleted"]["knowledge"].append({
                    "knowledge_id": knowledge_id,
                    "reasons": orphan.get("reasons", []),
                    "would_delete": True,
                })
            else:
                try:
                    await prismind.delete_knowledge(
                        knowledge_id=knowledge_id,
                        project=project,
                        user=effective_user,
                    )
                    result["deleted"]["knowledge"].append({
                        "knowledge_id": knowledge_id,
                        "deleted": True,
                    })
                    result["counts"]["knowledge_deleted"] += 1
                except Exception as e:
                    result["errors"].append({
                        "type": "knowledge_deletion",
                        "knowledge_id": knowledge_id,
                        "error": str(e),
                    })

    # Cleanup unused document types
    if cleanup_unused_types:
        unused_types_result = await _detect_unused_document_types_impl(
            settings=settings,
            project=project,
            user=effective_user,
        )

        for unused_type in unused_types_result.get("unused_types", []):
            type_id = unused_type.get("type_id", "")
            if not type_id:
                continue

            if dry_run:
                result["deleted"]["document_types"].append({
                    "type_id": type_id,
                    "name": unused_type.get("name", ""),
                    "scope": unused_type.get("scope", ""),
                    "would_delete": True,
                })
            else:
                try:
                    await prismind.delete_document_type(
                        type_id=type_id,
                        scope=unused_type.get("scope", "global"),
                    )
                    result["deleted"]["document_types"].append({
                        "type_id": type_id,
                        "deleted": True,
                    })
                    result["counts"]["types_deleted"] += 1
                except Exception as e:
                    result["errors"].append({
                        "type": "document_type_deletion",
                        "type_id": type_id,
                        "error": str(e),
                    })

    result["success"] = True
    if dry_run:
        result["message"] = (
            f"Dry run: Would delete {len(result['deleted']['documents'])} documents, "
            f"{len(result['deleted']['knowledge'])} knowledge entries, "
            f"{len(result['deleted']['document_types'])} document types"
        )
    else:
        result["message"] = (
            f"Cleanup complete: Deleted {result['counts']['documents_deleted']} documents, "
            f"{result['counts']['knowledge_deleted']} knowledge entries, "
            f"{result['counts']['types_deleted']} document types"
        )

    return result


def register_tools(mcp: FastMCP, settings: Settings) -> None:
    """Register document maintenance tools with the MCP server.

    Args:
        mcp: FastMCP server instance.
        settings: Application settings.
    """
    global _settings
    _settings = settings

    @mcp.tool()
    async def smart_delete_document(
        doc_id: str,
        project: str = "",
        delete_related_knowledge: bool = True,
        delete_drive_file: bool = True,
        permanent: bool = False,
        dry_run: bool = False,
        user: str = "",
    ) -> dict[str, Any]:
        """Delete a document with related knowledge cleanup.

        USE THIS WHEN: You need to delete a document and its associated data.
        This tool:
        - Verifies the document exists
        - Finds and optionally deletes related knowledge entries
        - Deletes the document from Prismind and optionally from Google Drive
        - Supports dry_run mode to preview what would be deleted

        DO NOT USE WHEN:
        - You only want to delete the document without knowledge cleanup -> use Prismind delete_document
        - You want to bulk delete -> use cleanup_documents instead

        Args:
            doc_id: Document ID to delete.
            project: Project identifier for filtering related knowledge.
            delete_related_knowledge: If True, delete knowledge entries referencing this document.
            delete_drive_file: If True, delete the file from Google Drive.
            permanent: If True, permanently delete; if False, move to trash.
            dry_run: If True, only preview what would be deleted without making changes.
            user: User identifier for multi-user support (auto-detected if empty).

        Returns:
            Dict containing:
            - success: Whether deletion succeeded
            - doc_id: The document ID
            - dry_run: Whether this was a dry run
            - document_deleted: Whether the document was deleted
            - drive_file_deleted: Whether the Drive file was deleted
            - related_knowledge: List of related knowledge entries found
            - knowledge_deleted_count: Number of knowledge entries deleted
            - would_delete: (dry_run only) Preview of what would be deleted
            - message: Status message
        """
        if _settings is None:
            raise RuntimeError("Settings not initialized")

        return await _smart_delete_document_impl(
            settings=_settings,
            doc_id=doc_id,
            project=project,
            delete_related_knowledge=delete_related_knowledge,
            delete_drive_file=delete_drive_file,
            permanent=permanent,
            dry_run=dry_run,
            user=user,
        )

    @mcp.tool()
    async def detect_orphan_documents(
        project: str = "",
        include_deleted_projects: bool = True,
        include_invalid_phase_task: bool = True,
        include_missing_doc_type: bool = True,
        limit: int = 100,
        user: str = "",
    ) -> dict[str, Any]:
        """Detect orphan documents that may need cleanup.

        USE THIS WHEN: You want to find documents that:
        - Belong to deleted/archived projects
        - Have invalid phase_task references
        - Use unregistered document types

        This is a detection-only tool. Use cleanup_documents to remove detected orphans.

        Args:
            project: Filter by project (empty for all projects).
            include_deleted_projects: Check for docs in deleted/archived projects.
            include_invalid_phase_task: Check for invalid phase_task references.
            include_missing_doc_type: Check for unregistered document types.
            limit: Maximum documents to check.
            user: User identifier for multi-user support (auto-detected if empty).

        Returns:
            Dict containing:
            - success: Whether detection completed
            - orphans: List of orphan documents with reasons
            - deleted_project_docs: Docs in deleted projects
            - invalid_phase_task_docs: Docs with invalid phase_task
            - missing_doc_type_docs: Docs with unregistered types
            - total_checked: Number of documents checked
            - total_orphans: Total orphans found
            - message: Status message
        """
        if _settings is None:
            raise RuntimeError("Settings not initialized")

        return await _detect_orphan_documents_impl(
            settings=_settings,
            project=project,
            include_deleted_projects=include_deleted_projects,
            include_invalid_phase_task=include_invalid_phase_task,
            include_missing_doc_type=include_missing_doc_type,
            limit=limit,
            user=user,
        )

    @mcp.tool()
    async def detect_orphan_knowledge(
        project: str = "",
        check_document_refs: bool = True,
        check_task_refs: bool = True,
        limit: int = 500,
        user: str = "",
    ) -> dict[str, Any]:
        """Detect orphan knowledge entries that may need cleanup.

        USE THIS WHEN: You want to find knowledge entries that:
        - Reference non-existent documents
        - Reference non-existent tasks

        This is a detection-only tool. Use cleanup_documents to remove detected orphans.

        Args:
            project: Filter by project (empty for all projects).
            check_document_refs: Check for invalid document references in source field.
            check_task_refs: Check for invalid task references in category field.
            limit: Maximum knowledge entries to check.
            user: User identifier for multi-user support (auto-detected if empty).

        Returns:
            Dict containing:
            - success: Whether detection completed
            - orphans: List of orphan knowledge entries with reasons
            - invalid_document_refs: Entries with invalid document references
            - invalid_task_refs: Entries with invalid task references
            - total_checked: Number of entries checked
            - total_orphans: Total orphans found
            - message: Status message
        """
        if _settings is None:
            raise RuntimeError("Settings not initialized")

        return await _detect_orphan_knowledge_impl(
            settings=_settings,
            project=project,
            check_document_refs=check_document_refs,
            check_task_refs=check_task_refs,
            limit=limit,
            user=user,
        )

    @mcp.tool()
    async def detect_unused_document_types(
        scope: str = "all",
        project: str = "",
        include_semantic_duplicates: bool = True,
        duplicate_threshold: float = 0.75,
        user: str = "",
    ) -> dict[str, Any]:
        """Detect unused and duplicate document types.

        USE THIS WHEN: You want to find:
        - Document types with no documents using them
        - Semantically similar document types that may be duplicates

        This is a detection-only tool. Use cleanup_documents to remove detected unused types.

        Args:
            scope: Scope to check ("all", "global", "project").
            project: Project to check (for "project" scope).
            include_semantic_duplicates: Check for semantic duplicates using RAG matching.
            duplicate_threshold: Similarity threshold for duplicate detection (0.0-1.0).
            user: User identifier for multi-user support (auto-detected if empty).

        Returns:
            Dict containing:
            - success: Whether detection completed
            - unused_types: List of unused document types
            - semantic_duplicates: List of potential duplicate type pairs
            - total_types_checked: Number of types checked
            - total_unused: Count of unused types
            - total_duplicates: Count of duplicate pairs
            - message: Status message
        """
        if _settings is None:
            raise RuntimeError("Settings not initialized")

        return await _detect_unused_document_types_impl(
            settings=_settings,
            scope=scope,
            project=project,
            include_semantic_duplicates=include_semantic_duplicates,
            duplicate_threshold=duplicate_threshold,
            user=user,
        )

    @mcp.tool()
    async def check_document_consistency(
        project: str = "",
        fix_issues: bool = False,
        dry_run: bool = True,
        user: str = "",
    ) -> dict[str, Any]:
        """Run comprehensive document consistency checks.

        USE THIS WHEN: You want a complete health check of your documents and knowledge.
        This tool runs all detection tools and provides a consolidated report.

        Checks performed:
        - Orphan documents (deleted projects, invalid phase_task, missing doc_type)
        - Orphan knowledge (invalid document refs, invalid task refs)
        - Unused document types
        - Semantic duplicate document types

        Args:
            project: Project to check (empty for all projects).
            fix_issues: If True, attempt automatic fixes (currently limited).
            dry_run: If True with fix_issues, only report what would be fixed.
            user: User identifier for multi-user support (auto-detected if empty).

        Returns:
            Dict containing:
            - success: Whether checks completed
            - project: Project checked
            - dry_run: Whether this was a dry run
            - checks_performed: List of checks that were run
            - issues_found: List of issues with type, count, and details
            - fixes_applied: List of fixes applied (if any)
            - summary: Summary counts of all issues
            - message: Status message
        """
        if _settings is None:
            raise RuntimeError("Settings not initialized")

        return await _check_document_consistency_impl(
            settings=_settings,
            project=project,
            fix_issues=fix_issues,
            dry_run=dry_run,
            user=user,
        )

    @mcp.tool()
    async def cleanup_documents(
        cleanup_orphan_documents: bool = False,
        cleanup_orphan_knowledge: bool = False,
        cleanup_unused_types: bool = False,
        project: str = "",
        confirm: bool = False,
        dry_run: bool = True,
        user: str = "",
    ) -> dict[str, Any]:
        """Batch cleanup of orphan documents, knowledge, and unused types.

        USE THIS WHEN: You want to clean up detected issues from consistency checks.
        CAUTION: This tool deletes data. Always run with dry_run=True first.

        Safety requirements:
        - dry_run defaults to True (preview mode)
        - confirm=True required for actual deletion
        - Documents are moved to trash by default, not permanently deleted

        Args:
            cleanup_orphan_documents: If True, delete orphan documents.
            cleanup_orphan_knowledge: If True, delete orphan knowledge entries.
            cleanup_unused_types: If True, delete unused document types.
            project: Project to clean up (empty for all projects).
            confirm: REQUIRED for non-dry-run execution. Safety confirmation.
            dry_run: If True, only preview what would be deleted.
            user: User identifier for multi-user support (auto-detected if empty).

        Returns:
            Dict containing:
            - success: Whether cleanup completed
            - dry_run: Whether this was a dry run
            - confirm: Whether confirmation was provided
            - deleted: Lists of deleted items by type
            - counts: Counts of deleted items
            - errors: Any errors encountered
            - message: Status message
        """
        if _settings is None:
            raise RuntimeError("Settings not initialized")

        return await _cleanup_documents_impl(
            settings=_settings,
            cleanup_orphan_documents=cleanup_orphan_documents,
            cleanup_orphan_knowledge=cleanup_orphan_knowledge,
            cleanup_unused_types=cleanup_unused_types,
            project=project,
            confirm=confirm,
            dry_run=dry_run,
            user=user,
        )
