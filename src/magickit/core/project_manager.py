"""Project management for organizing tasks within workspaces."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from magickit.api.models import (
    ProjectResponse,
    ProjectStatus,
    TaskResponse,
    TaskStatus,
)
from magickit.utils.logging import get_logger

if TYPE_CHECKING:
    from magickit.core.state_manager import StateManager
    from magickit.core.workspace_manager import WorkspaceManager

logger = get_logger(__name__)


class ProjectError(Exception):
    """Base exception for project errors."""

    pass


class ProjectNotFoundError(ProjectError):
    """Raised when project is not found."""

    pass


class ProjectAccessDeniedError(ProjectError):
    """Raised when user doesn't have access to project."""

    pass


class ProjectManager:
    """Manages project operations within workspaces.

    Projects organize tasks within a workspace and provide
    scoping for task queues and collaboration.
    """

    def __init__(
        self,
        state_manager: StateManager,
        workspace_manager: WorkspaceManager,
    ) -> None:
        """Initialize project manager.

        Args:
            state_manager: State manager for persistence.
            workspace_manager: Workspace manager for access control.
        """
        self._state = state_manager
        self._workspace = workspace_manager

    async def create_project(
        self,
        workspace_id: str,
        name: str,
        user_id: str,
        description: str = "",
        settings: dict[str, Any] | None = None,
    ) -> ProjectResponse:
        """Create a new project in a workspace.

        Args:
            workspace_id: Workspace ID.
            name: Project name.
            user_id: User creating the project.
            description: Project description.
            settings: Optional project settings.

        Returns:
            Created project.

        Raises:
            WorkspaceAccessDeniedError: If user can't access workspace.
        """
        # Verify workspace access
        await self._workspace.get_workspace(workspace_id, user_id)

        project_id = str(uuid.uuid4())

        project = await self._state.create_project(
            project_id=project_id,
            workspace_id=workspace_id,
            name=name,
            description=description,
            settings=settings,
        )

        logger.info(
            "project_created",
            project_id=project_id,
            workspace_id=workspace_id,
            name=name,
            created_by=user_id,
        )

        return project

    async def get_project(
        self,
        project_id: str,
        user_id: str | None = None,
    ) -> ProjectResponse:
        """Get a project by ID.

        Args:
            project_id: Project ID.
            user_id: Optional user ID to check access.

        Returns:
            Project response.

        Raises:
            ProjectNotFoundError: If project not found.
            ProjectAccessDeniedError: If user doesn't have access.
        """
        project = await self._state.get_project(project_id)

        if project is None:
            raise ProjectNotFoundError(f"Project {project_id} not found")

        # Check access through workspace membership
        if user_id:
            try:
                await self._workspace.get_workspace(project.workspace_id, user_id)
            except Exception:
                raise ProjectAccessDeniedError(
                    f"User {user_id} does not have access to project {project_id}"
                )

        return project

    async def get_workspace_projects(
        self,
        workspace_id: str,
        user_id: str,
    ) -> list[ProjectResponse]:
        """Get all projects in a workspace.

        Args:
            workspace_id: Workspace ID.
            user_id: User ID for access check.

        Returns:
            List of projects.

        Raises:
            WorkspaceAccessDeniedError: If user can't access workspace.
        """
        # Verify workspace access
        await self._workspace.get_workspace(workspace_id, user_id)

        return await self._state.get_projects_in_workspace(workspace_id)

    async def update_project(
        self,
        project_id: str,
        user_id: str,
        name: str | None = None,
        description: str | None = None,
        status: ProjectStatus | None = None,
        settings: dict[str, Any] | None = None,
    ) -> ProjectResponse:
        """Update a project.

        Args:
            project_id: Project ID.
            user_id: User ID performing update.
            name: New name.
            description: New description.
            status: New status.
            settings: New settings.

        Returns:
            Updated project.

        Raises:
            ProjectNotFoundError: If project not found.
            ProjectAccessDeniedError: If user can't update project.
        """
        # Verify access
        project = await self.get_project(project_id, user_id)

        updated = await self._state.update_project(
            project_id=project_id,
            name=name,
            description=description,
            status=status,
            settings=settings,
        )

        if updated is None:
            raise ProjectNotFoundError(f"Project {project_id} not found")

        logger.info(
            "project_updated",
            project_id=project_id,
            updated_by=user_id,
        )

        return updated

    async def delete_project(self, project_id: str, user_id: str) -> bool:
        """Delete a project (soft delete).

        Args:
            project_id: Project ID.
            user_id: User ID performing delete.

        Returns:
            True if deleted.

        Raises:
            ProjectNotFoundError: If project not found.
            ProjectAccessDeniedError: If user can't delete project.
            ProjectError: If trying to delete the default project.
        """
        # Verify access
        project = await self.get_project(project_id, user_id)

        # Prevent deleting default project
        if project_id == "default":
            raise ProjectError("Cannot delete the default project")

        result = await self._state.delete_project(project_id)

        if result:
            logger.info(
                "project_deleted",
                project_id=project_id,
                deleted_by=user_id,
            )

        return result

    async def archive_project(self, project_id: str, user_id: str) -> ProjectResponse:
        """Archive a project.

        Args:
            project_id: Project ID.
            user_id: User ID performing archive.

        Returns:
            Archived project.
        """
        return await self.update_project(
            project_id=project_id,
            user_id=user_id,
            status=ProjectStatus.ARCHIVED,
        )

    async def restore_project(self, project_id: str, user_id: str) -> ProjectResponse:
        """Restore an archived project.

        Args:
            project_id: Project ID.
            user_id: User ID performing restore.

        Returns:
            Restored project.
        """
        return await self.update_project(
            project_id=project_id,
            user_id=user_id,
            status=ProjectStatus.ACTIVE,
        )

    # =========================================================================
    # Task Operations (Project-Scoped)
    # =========================================================================

    async def get_project_tasks(
        self,
        project_id: str,
        user_id: str,
        status: TaskStatus | None = None,
    ) -> list[TaskResponse]:
        """Get all tasks in a project.

        Args:
            project_id: Project ID.
            user_id: User ID for access check.
            status: Optional status filter.

        Returns:
            List of tasks.

        Raises:
            ProjectAccessDeniedError: If user can't access project.
        """
        # Verify access
        await self.get_project(project_id, user_id)

        return await self._state.get_tasks_by_project(project_id, status)

    async def get_project_stats(
        self, project_id: str, user_id: str
    ) -> dict[str, Any]:
        """Get statistics for a project.

        Args:
            project_id: Project ID.
            user_id: User ID for access check.

        Returns:
            Project statistics.

        Raises:
            ProjectAccessDeniedError: If user can't access project.
        """
        # Verify access
        await self.get_project(project_id, user_id)

        tasks = await self._state.get_tasks_by_project(project_id)

        # Calculate stats
        total = len(tasks)
        by_status: dict[str, int] = {}
        by_priority: dict[int, int] = {}

        for task in tasks:
            status_key = task.status.value
            by_status[status_key] = by_status.get(status_key, 0) + 1
            by_priority[task.priority] = by_priority.get(task.priority, 0) + 1

        return {
            "project_id": project_id,
            "total_tasks": total,
            "tasks_by_status": by_status,
            "tasks_by_priority": by_priority,
            "pending": by_status.get(TaskStatus.PENDING.value, 0),
            "running": by_status.get(TaskStatus.RUNNING.value, 0),
            "completed": by_status.get(TaskStatus.COMPLETED.value, 0),
            "failed": by_status.get(TaskStatus.FAILED.value, 0),
        }
