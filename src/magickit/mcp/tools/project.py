"""Project management tools for Magickit MCP server.

Provides tools for managing projects across sessions, including
initialization, status tracking, archiving, and restoration.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from magickit.adapters.prismind import PrismindAdapter
from magickit.config import Settings
from magickit.utils.logging import get_logger
from magickit.utils.user import get_current_user

logger = get_logger(__name__)

# Module-level settings reference
_settings: Settings | None = None

# Project templates with predefined categories and phases
PROJECT_TEMPLATES = {
    "game": {
        "categories": ["design", "implementation", "asset", "bug", "decision"],
        "default_phases": ["pre-production", "production", "polish", "release"],
    },
    "mcp-server": {
        "categories": ["architecture", "tool", "adapter", "config", "decision"],
        "default_phases": ["design", "implementation", "testing", "deployment"],
    },
    "web-app": {
        "categories": ["frontend", "backend", "api", "design", "decision"],
        "default_phases": ["mvp", "iteration", "testing", "launch"],
    },
}


def register_tools(mcp: FastMCP, settings: Settings) -> None:
    """Register project management tools with the MCP server.

    Args:
        mcp: FastMCP server instance.
        settings: Application settings.
    """
    global _settings
    _settings = settings

    @mcp.tool()
    async def list_projects(include_archived: bool = False) -> dict[str, Any]:
        """List all projects with optional archived projects.

        USE THIS WHEN: You need to see available projects or check project status.

        Args:
            include_archived: If True, include archived projects in the list.

        Returns:
            Dict containing:
            - projects: List of project info dicts
            - total: Total number of projects
            - archived_count: Number of archived projects
        """
        if _settings is None:
            raise RuntimeError("Settings not initialized")

        prismind = PrismindAdapter(
            sse_url=_settings.prismind_url,
            timeout=_settings.prismind_timeout,
        )

        logger.info("Listing projects", include_archived=include_archived)

        try:
            result = await prismind.list_projects()
            projects = _parse_list_result(result)
        except Exception as e:
            logger.error("Failed to list projects", error=str(e))
            raise RuntimeError(f"Failed to list projects: {e}")

        # Filter out archived projects unless requested
        archived_count = sum(
            1 for p in projects if p.get("status") == "archived"
        )

        if not include_archived:
            projects = [p for p in projects if p.get("status") != "archived"]

        # Build response with basic info for each project
        project_list = []
        for p in projects:
            project_list.append({
                "name": p.get("name", p.get("project", "")),
                "status": p.get("status", "active"),
                "created_at": p.get("created_at", ""),
                "knowledge_count": p.get("knowledge_count", 0),
            })

        logger.info(
            "Projects listed",
            total=len(project_list),
            archived_count=archived_count,
        )

        return {
            "projects": project_list,
            "total": len(project_list) + (archived_count if not include_archived else 0),
            "archived_count": archived_count,
        }

    @mcp.tool()
    async def init_project(
        project: str,
        template: str = "game",
        name: str = "",
        description: str = "",
    ) -> dict[str, Any]:
        """Initialize a new project with optional template.

        USE THIS WHEN: Starting a new project with predefined categories and phases.

        Available templates:
        - game: For game development (design, implementation, asset, bug, decision)
        - mcp-server: For MCP server development (architecture, tool, adapter, config)
        - web-app: For web applications (frontend, backend, api, design)

        Args:
            project: Project identifier (e.g., "my-new-game").
            template: Template type ("game", "mcp-server", "web-app").
            name: Display name for the project (defaults to project identifier).
            description: Optional project description.

        Returns:
            Dict containing:
            - success: Whether initialization succeeded
            - project: Project name
            - name: Display name
            - template: Template used
            - categories: List of categories
            - phases: List of phases
            - message: Status message
        """
        if _settings is None:
            raise RuntimeError("Settings not initialized")

        prismind = PrismindAdapter(
            sse_url=_settings.prismind_url,
            timeout=_settings.prismind_timeout,
        )

        # Get template configuration
        template_config = PROJECT_TEMPLATES.get(template, PROJECT_TEMPLATES["game"])
        categories = template_config["categories"]
        phases = template_config["default_phases"]

        # Use project identifier as name if not provided
        display_name = name or project

        logger.info(
            "Initializing project",
            project=project,
            name=display_name,
            template=template,
        )

        try:
            # Step 1: Create project in Prismind
            setup_result = await prismind.setup_project(project=project, name=display_name)
            setup_parsed = _parse_result(setup_result)

            # Check for error in result
            result_str = str(setup_parsed.get("result", ""))
            has_error = (
                setup_parsed.get("error")
                or setup_parsed.get("success") is False
                or "error" in result_str.lower()
            )
            if has_error:
                error_msg = setup_parsed.get("error", setup_parsed.get("message", result_str or "Unknown error"))
                logger.error("Prismind setup_project failed", project=project, result=setup_parsed)
                raise RuntimeError(f"Failed to create project in Prismind: {error_msg}")

            logger.info("Prismind setup_project succeeded", project=project, result=setup_parsed)

            # Step 2: Update project with template metadata
            update_result = await prismind.update_project(
                project=project,
                name=display_name,
                categories=categories,
                phases=phases,
                description=description,
                template=template,
                status="active",
                created_at=datetime.now().isoformat(),
            )
            update_parsed = _parse_result(update_result)
            logger.info("Prismind update_project result", project=project, result=update_parsed)

            logger.info(
                "Project initialized",
                project=project,
                name=display_name,
                template=template,
                categories=categories,
            )

            return {
                "success": True,
                "project": project,
                "name": display_name,
                "template": template,
                "categories": categories,
                "phases": phases,
                "message": f"Project '{project}' initialized with '{template}' template",
            }

        except Exception as e:
            logger.error("Failed to initialize project", project=project, error=str(e))
            return {
                "success": False,
                "project": project,
                "name": display_name,
                "template": template,
                "categories": [],
                "phases": [],
                "message": f"Failed to initialize project: {e}",
            }

    @mcp.tool()
    async def get_project_status(project: str, user: str = "") -> dict[str, Any]:
        """Get comprehensive project status including sessions and knowledge stats.

        USE THIS WHEN: You need detailed status of a project including progress,
        knowledge statistics, and session history.

        Args:
            project: Project identifier.
            user: User identifier for multi-user support (auto-detected if empty).

        Returns:
            Dict containing:
            - project: Project name
            - status: Project status
            - current_phase: Current phase
            - progress: Progress information
            - knowledge_stats: Knowledge statistics by category
            - session_history: Recent session history
            - last_activity: Last activity timestamp
        """
        if _settings is None:
            raise RuntimeError("Settings not initialized")

        # Auto-detect user if not specified
        effective_user = user or get_current_user()

        prismind = PrismindAdapter(
            sse_url=_settings.prismind_url,
            timeout=_settings.prismind_timeout,
        )

        logger.info("Getting project status", project=project, user=effective_user)

        # Step 1: Get progress
        try:
            progress_result = await prismind.get_progress(project=project, user=effective_user)
            progress = _parse_result(progress_result)
        except Exception as e:
            logger.warning("Failed to get progress", project=project, error=str(e))
            progress = {}

        # Step 2: Get knowledge stats
        knowledge_stats = {"total": 0, "by_category": {}}
        try:
            knowledge_result = await prismind.search_knowledge(
                query="*",
                project=project,
                limit=100,
                user=effective_user,
            )
            knowledge_list = _parse_list_result(knowledge_result)
            knowledge_stats["total"] = len(knowledge_list)

            # Count by category
            by_category: dict[str, int] = {}
            for entry in knowledge_list:
                category = entry.get("category", "uncategorized")
                by_category[category] = by_category.get(category, 0) + 1
            knowledge_stats["by_category"] = by_category

        except Exception as e:
            logger.warning("Failed to get knowledge stats", error=str(e))

        # Step 3: Session history - not available (Prismind doesn't have list_sessions)
        session_history: list[dict[str, Any]] = []
        logger.debug("Session history not available (list_sessions not in Prismind)")

        # Build response
        last_activity = ""

        return {
            "project": project,
            "status": progress.get("status", "active"),
            "current_phase": progress.get("current_phase", ""),
            "progress": progress,
            "knowledge_stats": knowledge_stats,
            "session_history": session_history,
            "last_activity": last_activity,
        }

    @mcp.tool()
    async def clone_project(
        source_project: str,
        new_project: str,
        include_knowledge: bool = False,
        user: str = "",
    ) -> dict[str, Any]:
        """Clone an existing project as a template.

        USE THIS WHEN: You want to create a new project based on an existing one,
        optionally copying knowledge entries.

        Args:
            source_project: Source project to clone from.
            new_project: Name for the new project.
            include_knowledge: If True, copy knowledge entries to new project.
            user: User identifier for multi-user support (auto-detected if empty).

        Returns:
            Dict containing:
            - success: Whether clone succeeded
            - source_project: Source project name
            - new_project: New project name
            - knowledge_copied: Number of knowledge entries copied
            - message: Status message
        """
        if _settings is None:
            raise RuntimeError("Settings not initialized")

        # Auto-detect user if not specified
        effective_user = user or get_current_user()

        prismind = PrismindAdapter(
            sse_url=_settings.prismind_url,
            timeout=_settings.prismind_timeout,
        )

        logger.info(
            "Cloning project",
            source=source_project,
            target=new_project,
            include_knowledge=include_knowledge,
            user=effective_user,
        )

        knowledge_copied = 0

        try:
            # Step 1: Get source project info
            progress_result = await prismind.get_progress(project=source_project, user=effective_user)
            source_info = _parse_result(progress_result)

            if not source_info:
                raise RuntimeError(f"Source project '{source_project}' not found")

            # Step 2: Determine template from source
            template = source_info.get("template", "game")
            template_config = PROJECT_TEMPLATES.get(template, PROJECT_TEMPLATES["game"])

            # Step 3: Create new project
            # Use source project name with suffix, or just new_project ID
            clone_name = source_info.get("name", new_project)
            if clone_name == source_project:
                clone_name = new_project  # Avoid duplicate names

            setup_result = await prismind.setup_project(project=new_project, name=clone_name, force=True)
            setup_parsed = _parse_result(setup_result)

            # Check for error in result
            result_str = str(setup_parsed.get("result", ""))
            has_error = (
                setup_parsed.get("error")
                or setup_parsed.get("success") is False
                or "error" in result_str.lower()
            )
            if has_error:
                error_msg = setup_parsed.get("error", setup_parsed.get("message", result_str or "Unknown error"))
                logger.error("Prismind setup_project failed", project=new_project, result=setup_parsed)
                raise RuntimeError(f"Failed to create project in Prismind: {error_msg}")

            logger.info("Prismind setup_project succeeded", project=new_project, result=setup_parsed)

            await prismind.update_project(
                project=new_project,
                name=clone_name,
                categories=source_info.get("categories", template_config["categories"]),
                phases=source_info.get("phases", template_config["default_phases"]),
                template=template,
                status="active",
                created_at=datetime.now().isoformat(),
                cloned_from=source_project,
            )

            # Step 4: Copy knowledge if requested
            if include_knowledge:
                try:
                    knowledge_result = await prismind.search_knowledge(
                        query="*",
                        project=source_project,
                        limit=500,
                        user=effective_user,
                    )
                    knowledge_list = _parse_list_result(knowledge_result)

                    for entry in knowledge_list:
                        try:
                            await prismind.add_knowledge(
                                content=entry.get("content", ""),
                                project=new_project,
                                category=entry.get("category", ""),
                                tags=entry.get("tags", []),
                                user=effective_user,
                            )
                            knowledge_copied += 1
                        except Exception:
                            pass  # Continue on individual failures

                except Exception as e:
                    logger.warning("Failed to copy knowledge", error=str(e))

            logger.info(
                "Project cloned",
                source=source_project,
                target=new_project,
                knowledge_copied=knowledge_copied,
            )

            return {
                "success": True,
                "source_project": source_project,
                "new_project": new_project,
                "knowledge_copied": knowledge_copied,
                "message": f"Project cloned from '{source_project}' to '{new_project}'",
            }

        except Exception as e:
            logger.error("Failed to clone project", error=str(e))
            return {
                "success": False,
                "source_project": source_project,
                "new_project": new_project,
                "knowledge_copied": 0,
                "message": f"Failed to clone project: {e}",
            }

    @mcp.tool()
    async def delete_project(
        project: str,
        mode: str = "archive",
        confirm: bool = False,
        delete_drive_folder: bool = False,
        user: str = "",
    ) -> dict[str, Any]:
        """Delete a project with specified mode.

        USE THIS WHEN: You need to archive or delete a project.

        Modes:
        - archive: Set status to "archived" (data preserved, recoverable)
        - archive_and_delete: Export data then permanently delete
        - permanent: Immediately delete without backup (requires confirm=True)

        Args:
            project: Project identifier.
            mode: Deletion mode ("archive", "archive_and_delete", "permanent").
            confirm: Required for permanent deletion.
            delete_drive_folder: If True, also permanently delete Google Drive folder (irreversible).
            user: User identifier for multi-user support (auto-detected if empty).

        Returns:
            Dict containing:
            - success: Whether deletion succeeded
            - project: Project name
            - mode: Deletion mode used
            - export_path: Path to export (for archive_and_delete mode)
            - drive_folder_deleted: Whether Drive folder was deleted
            - message: Status message
        """
        if _settings is None:
            raise RuntimeError("Settings not initialized")

        # Auto-detect user if not specified
        effective_user = user or get_current_user()

        prismind = PrismindAdapter(
            sse_url=_settings.prismind_url,
            timeout=_settings.prismind_timeout,
        )

        logger.info(
            "Deleting project",
            project=project,
            mode=mode,
            user=effective_user,
        )

        export_path = None

        try:
            if mode == "archive":
                # Just mark as archived
                update_result = await prismind.update_project(
                    project=project,
                    status="archived",
                )
                update_parsed = _parse_result(update_result)

                # Check for error
                if update_parsed.get("success") is False:
                    error_msg = update_parsed.get("message", "Unknown error")
                    logger.error("Failed to archive project", project=project, error=error_msg)
                    return {
                        "success": False,
                        "project": project,
                        "mode": mode,
                        "export_path": None,
                        "drive_folder_deleted": False,
                        "message": f"Failed to archive project: {error_msg}",
                    }

                logger.info("Project archived", project=project, result=update_parsed)
                return {
                    "success": True,
                    "project": project,
                    "mode": mode,
                    "export_path": None,
                    "drive_folder_deleted": False,
                    "message": f"Project '{project}' archived (data preserved)",
                }

            elif mode == "archive_and_delete":
                # Export then delete
                export_path = await _export_project_impl(project, prismind, effective_user)
                delete_result = await prismind.delete_project(
                    project=project,
                    confirm=True,
                    delete_drive_folder=delete_drive_folder,
                )
                delete_parsed = _parse_result(delete_result)
                drive_folder_deleted = delete_parsed.get("drive_folder_deleted", False)

                msg = f"Project '{project}' exported to {export_path} and deleted"
                if drive_folder_deleted:
                    msg += " (Drive folder also deleted)"

                logger.info(
                    "Project archived and deleted",
                    project=project,
                    export_path=str(export_path),
                    drive_folder_deleted=drive_folder_deleted,
                )
                return {
                    "success": True,
                    "project": project,
                    "mode": mode,
                    "export_path": str(export_path),
                    "drive_folder_deleted": drive_folder_deleted,
                    "message": msg,
                }

            elif mode == "permanent":
                if not confirm:
                    return {
                        "success": False,
                        "project": project,
                        "mode": mode,
                        "export_path": None,
                        "drive_folder_deleted": False,
                        "message": "Permanent deletion requires confirm=True. This action cannot be undone.",
                    }

                delete_result = await prismind.delete_project(
                    project=project,
                    confirm=True,
                    delete_drive_folder=delete_drive_folder,
                )
                delete_parsed = _parse_result(delete_result)
                drive_folder_deleted = delete_parsed.get("drive_folder_deleted", False)

                msg = f"Project '{project}' permanently deleted"
                if drive_folder_deleted:
                    msg += " (Drive folder also deleted)"

                logger.info(
                    "Project permanently deleted",
                    project=project,
                    drive_folder_deleted=drive_folder_deleted,
                )
                return {
                    "success": True,
                    "project": project,
                    "mode": mode,
                    "export_path": None,
                    "drive_folder_deleted": drive_folder_deleted,
                    "message": msg,
                }

            else:
                return {
                    "success": False,
                    "project": project,
                    "mode": mode,
                    "export_path": None,
                    "drive_folder_deleted": False,
                    "message": f"Invalid mode: {mode}. Use 'archive', 'archive_and_delete', or 'permanent'.",
                }

        except Exception as e:
            logger.error("Failed to delete project", project=project, error=str(e))
            return {
                "success": False,
                "project": project,
                "mode": mode,
                "export_path": str(export_path) if export_path else None,
                "drive_folder_deleted": False,
                "message": f"Failed to delete project: {e}",
            }

    @mcp.tool()
    async def restore_project(
        project: str,
        from_export: str | None = None,
        user: str = "",
    ) -> dict[str, Any]:
        """Restore a project from archived status or export file.

        USE THIS WHEN: You need to restore an archived project or import
        from a previously exported backup.

        Args:
            project: Project identifier.
            from_export: Path to export directory (if restoring from export).
            user: User identifier for multi-user support (auto-detected if empty).

        Returns:
            Dict containing:
            - success: Whether restoration succeeded
            - project: Project name
            - restored_from: Source of restoration
            - knowledge_restored: Number of knowledge entries restored
            - message: Status message
        """
        if _settings is None:
            raise RuntimeError("Settings not initialized")

        # Auto-detect user if not specified
        effective_user = user or get_current_user()

        prismind = PrismindAdapter(
            sse_url=_settings.prismind_url,
            timeout=_settings.prismind_timeout,
        )

        logger.info(
            "Restoring project",
            project=project,
            from_export=from_export,
            user=effective_user,
        )

        knowledge_restored = 0

        try:
            if from_export:
                # Restore from export file
                export_path = Path(from_export)
                if not export_path.exists():
                    raise FileNotFoundError(f"Export path not found: {from_export}")

                result = await _import_project_impl(export_path, project, prismind, effective_user)
                knowledge_restored = result.get("knowledge_restored", 0)

                logger.info(
                    "Project restored from export",
                    project=project,
                    knowledge_restored=knowledge_restored,
                )
                return {
                    "success": True,
                    "project": project,
                    "restored_from": str(export_path),
                    "knowledge_restored": knowledge_restored,
                    "message": f"Project '{project}' restored from {export_path}",
                }

            else:
                # Restore from archived status
                await prismind.update_project(
                    project=project,
                    status="active",
                    restored_at=datetime.now().isoformat(),
                )

                logger.info("Project restored from archived status", project=project)
                return {
                    "success": True,
                    "project": project,
                    "restored_from": "archived_status",
                    "knowledge_restored": 0,
                    "message": f"Project '{project}' restored from archived status",
                }

        except FileNotFoundError as e:
            logger.error("Export file not found", error=str(e))
            raise
        except Exception as e:
            logger.error("Failed to restore project", project=project, error=str(e))
            return {
                "success": False,
                "project": project,
                "restored_from": from_export or "archived_status",
                "knowledge_restored": 0,
                "message": f"Failed to restore project: {e}",
            }


async def _export_project_impl(project: str, prismind: PrismindAdapter, user: str = "") -> Path:
    """Export project data to archive directory.

    Args:
        project: Project identifier.
        prismind: PrismindAdapter instance.
        user: User identifier for multi-user support.

    Returns:
        Path to the export directory.
    """
    if _settings is None:
        raise RuntimeError("Settings not initialized")

    archive_path = Path(_settings.archive_path)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    export_dir = archive_path / f"{project}_{timestamp}"
    export_dir.mkdir(parents=True, exist_ok=True)

    # Export project metadata
    try:
        progress_result = await prismind.get_progress(project=project, user=user)
        project_data = _parse_result(progress_result)
        (export_dir / "project.json").write_text(
            json.dumps(project_data, indent=2, default=str),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning("Failed to export project metadata", error=str(e))
        (export_dir / "project.json").write_text("{}", encoding="utf-8")

    # Export knowledge
    try:
        knowledge_result = await prismind.search_knowledge(
            query="*",
            project=project,
            limit=1000,
            user=user,
        )
        knowledge_list = _parse_list_result(knowledge_result)
        (export_dir / "knowledge.json").write_text(
            json.dumps(knowledge_list, indent=2, default=str),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning("Failed to export knowledge", error=str(e))
        (export_dir / "knowledge.json").write_text("[]", encoding="utf-8")

    # Note: sessions.json export skipped - list_sessions not available in Prismind
    logger.debug("Session export skipped (list_sessions not in Prismind)")

    logger.info("Project exported", project=project, export_dir=str(export_dir))
    return export_dir


async def _import_project_impl(
    export_path: Path,
    project: str,
    prismind: PrismindAdapter,
    user: str = "",
) -> dict[str, Any]:
    """Import project data from archive directory.

    Args:
        export_path: Path to the export directory.
        project: Target project name.
        prismind: PrismindAdapter instance.
        user: User identifier for multi-user support.

    Returns:
        Dict with import statistics.
    """
    knowledge_restored = 0
    knowledge_failed = 0

    # Read project metadata
    project_file = export_path / "project.json"
    if project_file.exists():
        project_data = json.loads(project_file.read_text(encoding="utf-8"))
    else:
        project_data = {}

    # Get display name from export data or use project ID
    display_name = project_data.get("name", project)

    # Create project and verify success (force=True to skip similar project check)
    setup_result = await prismind.setup_project(project=project, name=display_name, force=True)
    setup_parsed = _parse_result(setup_result)

    # Check for error in result (including validation errors)
    result_str = str(setup_parsed.get("result", ""))
    has_error = (
        setup_parsed.get("error")
        or setup_parsed.get("success") is False
        or "error" in result_str.lower()
    )
    if has_error:
        error_msg = setup_parsed.get("error", setup_parsed.get("message", result_str or "Unknown error"))
        logger.error("Failed to create project in Prismind", project=project, error=error_msg)
        raise RuntimeError(f"Failed to create project '{project}' in Prismind: {error_msg}")

    logger.info("Project created in Prismind", project=project, result=setup_parsed)

    # Update with metadata
    if project_data:
        update_result = await prismind.update_project(
            project=project,
            categories=project_data.get("categories", []),
            phases=project_data.get("phases", []),
            template=project_data.get("template", "game"),
            status="active",
            restored_at=datetime.now().isoformat(),
        )
        update_parsed = _parse_result(update_result)

        if update_parsed.get("error") or update_parsed.get("success") is False:
            error_msg = update_parsed.get("error", update_parsed.get("message", "Unknown error"))
            logger.warning("Failed to update project metadata", project=project, error=error_msg)
            # Continue - project was created, just metadata update failed

    # Restore knowledge
    knowledge_file = export_path / "knowledge.json"
    if knowledge_file.exists():
        knowledge_list = json.loads(knowledge_file.read_text(encoding="utf-8"))
        for entry in knowledge_list:
            try:
                add_result = await prismind.add_knowledge(
                    content=entry.get("content", ""),
                    project=project,
                    category=entry.get("category", ""),
                    tags=entry.get("tags", []),
                    user=user,
                )
                add_parsed = _parse_result(add_result)
                if add_parsed.get("error") or add_parsed.get("success") is False:
                    knowledge_failed += 1
                else:
                    knowledge_restored += 1
            except Exception as e:
                logger.debug("Failed to restore knowledge entry", error=str(e))
                knowledge_failed += 1

    logger.info(
        "Project imported",
        project=project,
        knowledge_restored=knowledge_restored,
        knowledge_failed=knowledge_failed,
    )

    return {
        "knowledge_restored": knowledge_restored,
        "knowledge_failed": knowledge_failed,
    }


def _parse_result(result: Any) -> dict[str, Any]:
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


def _parse_list_result(result: Any) -> list[dict[str, Any]]:
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
                for key in ["results", "items", "documents", "knowledge", "projects", "entries"]:
                    if key in data and isinstance(data[key], list):
                        return data[key]
                return [data]
            return [{"result": data}]
        except json.JSONDecodeError:
            return [{"result": result}]
    return [{"result": result}]
