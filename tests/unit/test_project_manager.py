"""Unit tests for project manager."""

import pytest
import pytest_asyncio

from magickit.api.models import ProjectStatus, UserRole
from magickit.core.project_manager import (
    ProjectAccessDeniedError,
    ProjectError,
    ProjectManager,
    ProjectNotFoundError,
)
from magickit.core.state_manager import StateManager
from magickit.core.workspace_manager import WorkspaceManager


@pytest_asyncio.fixture
async def state_manager(tmp_path):
    """Create a state manager with temporary database."""
    db_path = str(tmp_path / "test.db")
    manager = StateManager(db_path=db_path)
    await manager.initialize()

    # Run Phase 2 migrations
    from magickit.core.migrations import MigrationManager
    migration_manager = MigrationManager(db_path=db_path)
    await migration_manager.migrate()

    yield manager
    await manager.close()


@pytest_asyncio.fixture
async def workspace_manager(state_manager):
    """Create a workspace manager."""
    return WorkspaceManager(state_manager)


@pytest_asyncio.fixture
async def project_manager(state_manager, workspace_manager):
    """Create a project manager."""
    return ProjectManager(state_manager, workspace_manager)


@pytest_asyncio.fixture
async def test_user(state_manager):
    """Create a test user."""
    return await state_manager.create_user(
        user_id="user-1",
        email="test@example.com",
        name="Test User",
        password_hash="hashed",
    )


@pytest_asyncio.fixture
async def test_workspace(workspace_manager, test_user):
    """Create a test workspace."""
    return await workspace_manager.create_workspace(
        name="Test Workspace",
        owner_id=test_user.id,
    )


@pytest.mark.asyncio
async def test_create_project(project_manager, test_workspace, test_user):
    """Test creating a project."""
    project = await project_manager.create_project(
        workspace_id=test_workspace.id,
        name="Test Project",
        user_id=test_user.id,
        description="A test project",
    )

    assert project.name == "Test Project"
    assert project.description == "A test project"
    assert project.workspace_id == test_workspace.id
    assert project.status == ProjectStatus.ACTIVE


@pytest.mark.asyncio
async def test_create_project_with_settings(project_manager, test_workspace, test_user):
    """Test creating a project with custom settings."""
    settings = {"priority": "high", "visibility": "public"}
    project = await project_manager.create_project(
        workspace_id=test_workspace.id,
        name="Configured Project",
        user_id=test_user.id,
        settings=settings,
    )

    assert project.settings == settings


@pytest.mark.asyncio
async def test_get_project(project_manager, test_workspace, test_user):
    """Test getting a project by ID."""
    project = await project_manager.create_project(
        workspace_id=test_workspace.id,
        name="Test Project",
        user_id=test_user.id,
    )

    retrieved = await project_manager.get_project(project.id, test_user.id)
    assert retrieved.id == project.id
    assert retrieved.name == project.name


@pytest.mark.asyncio
async def test_get_project_not_found(project_manager, test_user):
    """Test getting a non-existent project."""
    with pytest.raises(ProjectNotFoundError):
        await project_manager.get_project("non-existent", test_user.id)


@pytest.mark.asyncio
async def test_get_project_access_denied(
    project_manager, workspace_manager, state_manager, test_workspace, test_user
):
    """Test accessing a project without workspace membership."""
    project = await project_manager.create_project(
        workspace_id=test_workspace.id,
        name="Private Project",
        user_id=test_user.id,
    )

    # Create another user not in the workspace
    other_user = await state_manager.create_user(
        user_id="user-2",
        email="other@example.com",
        name="Other User",
        password_hash="hashed",
    )

    with pytest.raises(ProjectAccessDeniedError):
        await project_manager.get_project(project.id, other_user.id)


@pytest.mark.asyncio
async def test_get_workspace_projects(project_manager, test_workspace, test_user):
    """Test getting all projects in a workspace."""
    # Create multiple projects
    await project_manager.create_project(
        workspace_id=test_workspace.id,
        name="Project 1",
        user_id=test_user.id,
    )
    await project_manager.create_project(
        workspace_id=test_workspace.id,
        name="Project 2",
        user_id=test_user.id,
    )

    projects = await project_manager.get_workspace_projects(
        test_workspace.id, test_user.id
    )
    assert len(projects) == 2


@pytest.mark.asyncio
async def test_update_project(project_manager, test_workspace, test_user):
    """Test updating a project."""
    project = await project_manager.create_project(
        workspace_id=test_workspace.id,
        name="Original",
        user_id=test_user.id,
    )

    updated = await project_manager.update_project(
        project_id=project.id,
        user_id=test_user.id,
        name="Updated",
        description="New description",
    )

    assert updated.name == "Updated"
    assert updated.description == "New description"


@pytest.mark.asyncio
async def test_delete_project(project_manager, test_workspace, test_user):
    """Test deleting (archiving) a project."""
    project = await project_manager.create_project(
        workspace_id=test_workspace.id,
        name="To Delete",
        user_id=test_user.id,
    )

    result = await project_manager.delete_project(project.id, test_user.id)
    assert result is True

    # Project should still exist but with deleted status
    retrieved = await project_manager.get_project(project.id, test_user.id)
    assert retrieved.status == ProjectStatus.DELETED


@pytest.mark.asyncio
async def test_cannot_delete_default_project(project_manager, state_manager, test_user):
    """Test that default project cannot be deleted."""
    # Migration creates default workspace/project, add test user as member
    await state_manager.add_workspace_member(
        workspace_id="default",
        user_id=test_user.id,
        role=UserRole.ADMIN,
    )

    with pytest.raises(ProjectError, match="Cannot delete the default project"):
        await project_manager.delete_project("default", test_user.id)


@pytest.mark.asyncio
async def test_archive_project(project_manager, test_workspace, test_user):
    """Test archiving a project."""
    project = await project_manager.create_project(
        workspace_id=test_workspace.id,
        name="To Archive",
        user_id=test_user.id,
    )

    archived = await project_manager.archive_project(project.id, test_user.id)
    assert archived.status == ProjectStatus.ARCHIVED


@pytest.mark.asyncio
async def test_restore_project(project_manager, test_workspace, test_user):
    """Test restoring an archived project."""
    project = await project_manager.create_project(
        workspace_id=test_workspace.id,
        name="To Restore",
        user_id=test_user.id,
    )

    # Archive then restore
    await project_manager.archive_project(project.id, test_user.id)
    restored = await project_manager.restore_project(project.id, test_user.id)

    assert restored.status == ProjectStatus.ACTIVE


@pytest.mark.asyncio
async def test_get_project_stats(project_manager, test_workspace, test_user):
    """Test getting project statistics."""
    project = await project_manager.create_project(
        workspace_id=test_workspace.id,
        name="Stats Project",
        user_id=test_user.id,
    )

    stats = await project_manager.get_project_stats(project.id, test_user.id)

    assert stats["project_id"] == project.id
    assert stats["total_tasks"] == 0
    assert "tasks_by_status" in stats
    assert "tasks_by_priority" in stats
