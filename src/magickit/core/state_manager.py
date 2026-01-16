"""State management with SQLite persistence."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from magickit.api.models import (
    EventType,
    LockResponse,
    ProjectResponse,
    ProjectStatus,
    TaskEventResponse,
    TaskResponse,
    TaskStatus,
    UserResponse,
    UserRole,
    WebhookResponse,
    WebhookService,
    WorkspaceMemberResponse,
    WorkspaceResponse,
)
from magickit.utils.logging import get_logger

logger = get_logger(__name__)


class StateManager:
    """Manages persistent state using SQLite.

    Handles task storage, retrieval, and state transitions.
    """

    def __init__(self, db_path: str = "data/magickit.db") -> None:
        """Initialize the state manager.

        Args:
            db_path: Path to the SQLite database file.
        """
        self.db_path = db_path
        self._connection: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        """Initialize the database and create tables."""
        # Ensure directory exists
        db_dir = Path(self.db_path).parent
        db_dir.mkdir(parents=True, exist_ok=True)

        self._connection = await aiosqlite.connect(self.db_path)
        self._connection.row_factory = aiosqlite.Row

        await self._create_tables()
        logger.info("State manager initialized", db_path=self.db_path)

    async def _create_tables(self) -> None:
        """Create database tables if they don't exist."""
        assert self._connection is not None

        await self._connection.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                service TEXT NOT NULL,
                payload TEXT DEFAULT '{}',
                priority INTEGER DEFAULT 5,
                status TEXT DEFAULT 'pending',
                dependencies TEXT DEFAULT '[]',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                result TEXT,
                error TEXT,
                retry_count INTEGER DEFAULT 0
            )
        """)

        await self._connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)
        """)

        await self._connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_tasks_priority ON tasks(priority)
        """)

        await self._connection.commit()

    async def close(self) -> None:
        """Close the database connection."""
        if self._connection:
            await self._connection.close()
            self._connection = None

    async def save_task(self, task: TaskResponse) -> None:
        """Save or update a task in the database.

        Args:
            task: Task to save.
        """
        assert self._connection is not None

        await self._connection.execute(
            """
            INSERT OR REPLACE INTO tasks (
                id, name, description, service, payload, priority, status,
                dependencies, metadata, created_at, started_at, completed_at,
                result, error, retry_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task.id,
                task.name,
                task.description,
                task.service.value,
                json.dumps(task.payload),
                task.priority,
                task.status.value,
                json.dumps(task.dependencies),
                json.dumps(task.metadata),
                task.created_at.isoformat(),
                task.started_at.isoformat() if task.started_at else None,
                task.completed_at.isoformat() if task.completed_at else None,
                json.dumps(task.result) if task.result else None,
                task.error,
                task.retry_count,
            ),
        )
        await self._connection.commit()

    async def get_task(self, task_id: str) -> TaskResponse | None:
        """Get a task by ID.

        Args:
            task_id: Task ID.

        Returns:
            Task if found, None otherwise.
        """
        assert self._connection is not None

        cursor = await self._connection.execute(
            "SELECT * FROM tasks WHERE id = ?",
            (task_id,),
        )
        row = await cursor.fetchone()

        if row is None:
            return None

        return self._row_to_task(row)

    async def get_tasks_by_status(self, status: TaskStatus) -> list[TaskResponse]:
        """Get all tasks with a specific status.

        Args:
            status: Task status to filter by.

        Returns:
            List of tasks.
        """
        assert self._connection is not None

        cursor = await self._connection.execute(
            "SELECT * FROM tasks WHERE status = ? ORDER BY priority, created_at",
            (status.value,),
        )
        rows = await cursor.fetchall()

        return [self._row_to_task(row) for row in rows]

    async def get_all_tasks(self) -> list[TaskResponse]:
        """Get all tasks.

        Returns:
            List of all tasks.
        """
        assert self._connection is not None

        cursor = await self._connection.execute(
            "SELECT * FROM tasks ORDER BY created_at DESC"
        )
        rows = await cursor.fetchall()

        return [self._row_to_task(row) for row in rows]

    async def update_task_status(
        self,
        task_id: str,
        status: TaskStatus,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> TaskResponse | None:
        """Update task status and optionally result/error.

        Args:
            task_id: Task ID.
            status: New status.
            result: Task result (for completed tasks).
            error: Error message (for failed tasks).

        Returns:
            Updated task if found, None otherwise.
        """
        assert self._connection is not None

        task = await self.get_task(task_id)
        if task is None:
            return None

        # Update timestamps based on status
        started_at = task.started_at
        completed_at = task.completed_at
        now = datetime.utcnow()

        if status == TaskStatus.RUNNING and task.started_at is None:
            started_at = now
        elif status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
            completed_at = now

        await self._connection.execute(
            """
            UPDATE tasks SET
                status = ?,
                started_at = ?,
                completed_at = ?,
                result = ?,
                error = ?
            WHERE id = ?
            """,
            (
                status.value,
                started_at.isoformat() if started_at else None,
                completed_at.isoformat() if completed_at else None,
                json.dumps(result) if result else None,
                error,
                task_id,
            ),
        )
        await self._connection.commit()

        return await self.get_task(task_id)

    async def delete_task(self, task_id: str) -> bool:
        """Delete a task.

        Args:
            task_id: Task ID.

        Returns:
            True if deleted, False if not found.
        """
        assert self._connection is not None

        cursor = await self._connection.execute(
            "DELETE FROM tasks WHERE id = ?",
            (task_id,),
        )
        await self._connection.commit()

        return cursor.rowcount > 0

    async def get_stats(self) -> dict[str, Any]:
        """Get task statistics.

        Returns:
            Statistics dictionary.
        """
        assert self._connection is not None

        # Count by status
        cursor = await self._connection.execute(
            "SELECT status, COUNT(*) as count FROM tasks GROUP BY status"
        )
        status_rows = await cursor.fetchall()
        tasks_by_status = {row["status"]: row["count"] for row in status_rows}

        # Count by service
        cursor = await self._connection.execute(
            "SELECT service, COUNT(*) as count FROM tasks GROUP BY service"
        )
        service_rows = await cursor.fetchall()
        tasks_by_service = {row["service"]: row["count"] for row in service_rows}

        # Total count
        cursor = await self._connection.execute("SELECT COUNT(*) as total FROM tasks")
        total_row = await cursor.fetchone()
        total = total_row["total"] if total_row else 0

        # Average completion time (for completed tasks)
        cursor = await self._connection.execute("""
            SELECT AVG(
                (julianday(completed_at) - julianday(started_at)) * 86400000
            ) as avg_ms
            FROM tasks
            WHERE status = 'completed' AND started_at IS NOT NULL AND completed_at IS NOT NULL
        """)
        avg_row = await cursor.fetchone()
        avg_completion_time = avg_row["avg_ms"] if avg_row and avg_row["avg_ms"] else 0.0

        return {
            "total_tasks": total,
            "tasks_by_status": tasks_by_status,
            "tasks_by_service": tasks_by_service,
            "avg_completion_time_ms": avg_completion_time,
        }

    def _row_to_task(self, row: aiosqlite.Row) -> TaskResponse:
        """Convert a database row to a TaskResponse.

        Args:
            row: Database row.

        Returns:
            TaskResponse instance.
        """
        from magickit.api.models import ServiceType

        return TaskResponse(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            service=ServiceType(row["service"]),
            payload=json.loads(row["payload"]),
            priority=row["priority"],
            status=TaskStatus(row["status"]),
            dependencies=json.loads(row["dependencies"]),
            metadata=json.loads(row["metadata"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] else None,
            completed_at=datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None,
            result=json.loads(row["result"]) if row["result"] else None,
            error=row["error"],
            retry_count=row["retry_count"],
        )

    # =========================================================================
    # Phase 2: User Management
    # =========================================================================

    async def create_user(
        self,
        user_id: str,
        email: str,
        name: str,
        password_hash: str,
        role: UserRole = UserRole.MEMBER,
    ) -> UserResponse:
        """Create a new user.

        Args:
            user_id: Unique user ID.
            email: User email.
            name: User display name.
            password_hash: Hashed password.
            role: User role.

        Returns:
            Created user.
        """
        assert self._connection is not None
        now = datetime.now(timezone.utc).isoformat()

        await self._connection.execute(
            """
            INSERT INTO users (id, email, name, password_hash, role, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, email, name, password_hash, role.value, now),
        )
        await self._connection.commit()

        return UserResponse(
            id=user_id,
            email=email,
            name=name,
            role=role,
            created_at=datetime.fromisoformat(now),
        )

    async def get_user(self, user_id: str) -> UserResponse | None:
        """Get a user by ID."""
        assert self._connection is not None

        cursor = await self._connection.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        )
        row = await cursor.fetchone()

        if row is None:
            return None

        return self._row_to_user(row)

    async def get_user_by_email(self, email: str) -> tuple[UserResponse, str] | None:
        """Get a user by email, including password hash.

        Returns:
            Tuple of (UserResponse, password_hash) or None.
        """
        assert self._connection is not None

        cursor = await self._connection.execute(
            "SELECT * FROM users WHERE email = ?", (email,)
        )
        row = await cursor.fetchone()

        if row is None:
            return None

        return self._row_to_user(row), row["password_hash"]

    async def update_user_last_login(self, user_id: str) -> None:
        """Update user's last login timestamp."""
        assert self._connection is not None

        now = datetime.now(timezone.utc).isoformat()
        await self._connection.execute(
            "UPDATE users SET last_login = ? WHERE id = ?",
            (now, user_id),
        )
        await self._connection.commit()

    def _row_to_user(self, row: aiosqlite.Row) -> UserResponse:
        """Convert a database row to a UserResponse."""
        return UserResponse(
            id=row["id"],
            email=row["email"],
            name=row["name"],
            role=UserRole(row["role"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            last_login=datetime.fromisoformat(row["last_login"]) if row["last_login"] else None,
        )

    # =========================================================================
    # Phase 2: Workspace Management
    # =========================================================================

    async def create_workspace(
        self,
        workspace_id: str,
        name: str,
        owner_id: str | None = None,
        settings: dict[str, Any] | None = None,
    ) -> WorkspaceResponse:
        """Create a new workspace."""
        assert self._connection is not None
        now = datetime.now(timezone.utc).isoformat()
        settings_json = json.dumps(settings or {})

        await self._connection.execute(
            """
            INSERT INTO workspaces (id, name, owner_id, settings, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (workspace_id, name, owner_id, settings_json, now),
        )
        await self._connection.commit()

        return WorkspaceResponse(
            id=workspace_id,
            name=name,
            owner_id=owner_id,
            settings=settings or {},
            created_at=datetime.fromisoformat(now),
        )

    async def get_workspace(self, workspace_id: str) -> WorkspaceResponse | None:
        """Get a workspace by ID."""
        assert self._connection is not None

        cursor = await self._connection.execute(
            "SELECT * FROM workspaces WHERE id = ?", (workspace_id,)
        )
        row = await cursor.fetchone()

        if row is None:
            return None

        return self._row_to_workspace(row)

    async def get_workspaces_for_user(self, user_id: str) -> list[WorkspaceResponse]:
        """Get all workspaces the user is a member of."""
        assert self._connection is not None

        cursor = await self._connection.execute(
            """
            SELECT w.* FROM workspaces w
            JOIN workspace_members wm ON w.id = wm.workspace_id
            WHERE wm.user_id = ?
            ORDER BY w.name
            """,
            (user_id,),
        )
        rows = await cursor.fetchall()

        return [self._row_to_workspace(row) for row in rows]

    async def update_workspace(
        self,
        workspace_id: str,
        name: str | None = None,
        settings: dict[str, Any] | None = None,
    ) -> WorkspaceResponse | None:
        """Update a workspace."""
        assert self._connection is not None

        workspace = await self.get_workspace(workspace_id)
        if workspace is None:
            return None

        new_name = name if name is not None else workspace.name
        new_settings = settings if settings is not None else workspace.settings
        now = datetime.now(timezone.utc).isoformat()

        await self._connection.execute(
            """
            UPDATE workspaces SET name = ?, settings = ?, updated_at = ?
            WHERE id = ?
            """,
            (new_name, json.dumps(new_settings), now, workspace_id),
        )
        await self._connection.commit()

        return await self.get_workspace(workspace_id)

    async def delete_workspace(self, workspace_id: str) -> bool:
        """Delete a workspace."""
        assert self._connection is not None

        cursor = await self._connection.execute(
            "DELETE FROM workspaces WHERE id = ?", (workspace_id,)
        )
        await self._connection.commit()

        return cursor.rowcount > 0

    async def add_workspace_member(
        self, workspace_id: str, user_id: str, role: UserRole = UserRole.MEMBER
    ) -> None:
        """Add a member to a workspace."""
        assert self._connection is not None
        now = datetime.now(timezone.utc).isoformat()

        await self._connection.execute(
            """
            INSERT OR REPLACE INTO workspace_members (workspace_id, user_id, role, joined_at)
            VALUES (?, ?, ?, ?)
            """,
            (workspace_id, user_id, role.value, now),
        )
        await self._connection.commit()

    async def remove_workspace_member(self, workspace_id: str, user_id: str) -> bool:
        """Remove a member from a workspace."""
        assert self._connection is not None

        cursor = await self._connection.execute(
            "DELETE FROM workspace_members WHERE workspace_id = ? AND user_id = ?",
            (workspace_id, user_id),
        )
        await self._connection.commit()

        return cursor.rowcount > 0

    async def get_workspace_members(
        self, workspace_id: str
    ) -> list[WorkspaceMemberResponse]:
        """Get all members of a workspace."""
        assert self._connection is not None

        cursor = await self._connection.execute(
            """
            SELECT wm.*, u.name as user_name, u.email as user_email
            FROM workspace_members wm
            JOIN users u ON wm.user_id = u.id
            WHERE wm.workspace_id = ?
            ORDER BY wm.joined_at
            """,
            (workspace_id,),
        )
        rows = await cursor.fetchall()

        return [
            WorkspaceMemberResponse(
                user_id=row["user_id"],
                user_name=row["user_name"],
                user_email=row["user_email"],
                role=UserRole(row["role"]),
                joined_at=datetime.fromisoformat(row["joined_at"]),
            )
            for row in rows
        ]

    async def is_workspace_member(self, workspace_id: str, user_id: str) -> bool:
        """Check if a user is a member of a workspace."""
        assert self._connection is not None

        cursor = await self._connection.execute(
            "SELECT 1 FROM workspace_members WHERE workspace_id = ? AND user_id = ?",
            (workspace_id, user_id),
        )
        return await cursor.fetchone() is not None

    def _row_to_workspace(self, row: aiosqlite.Row) -> WorkspaceResponse:
        """Convert a database row to a WorkspaceResponse."""
        return WorkspaceResponse(
            id=row["id"],
            name=row["name"],
            owner_id=row["owner_id"],
            settings=json.loads(row["settings"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]) if row["updated_at"] else None,
        )

    # =========================================================================
    # Phase 2: Project Management
    # =========================================================================

    async def create_project(
        self,
        project_id: str,
        workspace_id: str,
        name: str,
        description: str = "",
        settings: dict[str, Any] | None = None,
    ) -> ProjectResponse:
        """Create a new project."""
        assert self._connection is not None
        now = datetime.now(timezone.utc).isoformat()
        settings_json = json.dumps(settings or {})

        await self._connection.execute(
            """
            INSERT INTO projects (id, workspace_id, name, description, settings, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (project_id, workspace_id, name, description, settings_json, now),
        )
        await self._connection.commit()

        return ProjectResponse(
            id=project_id,
            workspace_id=workspace_id,
            name=name,
            description=description,
            status=ProjectStatus.ACTIVE,
            settings=settings or {},
            created_at=datetime.fromisoformat(now),
        )

    async def get_project(self, project_id: str) -> ProjectResponse | None:
        """Get a project by ID."""
        assert self._connection is not None

        cursor = await self._connection.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        )
        row = await cursor.fetchone()

        if row is None:
            return None

        return self._row_to_project(row)

    async def get_projects_in_workspace(
        self, workspace_id: str
    ) -> list[ProjectResponse]:
        """Get all projects in a workspace."""
        assert self._connection is not None

        cursor = await self._connection.execute(
            """
            SELECT * FROM projects
            WHERE workspace_id = ? AND status != 'deleted'
            ORDER BY name
            """,
            (workspace_id,),
        )
        rows = await cursor.fetchall()

        return [self._row_to_project(row) for row in rows]

    async def update_project(
        self,
        project_id: str,
        name: str | None = None,
        description: str | None = None,
        status: ProjectStatus | None = None,
        settings: dict[str, Any] | None = None,
    ) -> ProjectResponse | None:
        """Update a project."""
        assert self._connection is not None

        project = await self.get_project(project_id)
        if project is None:
            return None

        new_name = name if name is not None else project.name
        new_description = description if description is not None else project.description
        new_status = status if status is not None else project.status
        new_settings = settings if settings is not None else project.settings
        now = datetime.now(timezone.utc).isoformat()

        await self._connection.execute(
            """
            UPDATE projects SET name = ?, description = ?, status = ?, settings = ?, updated_at = ?
            WHERE id = ?
            """,
            (new_name, new_description, new_status.value, json.dumps(new_settings), now, project_id),
        )
        await self._connection.commit()

        return await self.get_project(project_id)

    async def delete_project(self, project_id: str) -> bool:
        """Soft delete a project by setting status to deleted."""
        assert self._connection is not None

        cursor = await self._connection.execute(
            "UPDATE projects SET status = 'deleted' WHERE id = ?", (project_id,)
        )
        await self._connection.commit()

        return cursor.rowcount > 0

    def _row_to_project(self, row: aiosqlite.Row) -> ProjectResponse:
        """Convert a database row to a ProjectResponse."""
        return ProjectResponse(
            id=row["id"],
            workspace_id=row["workspace_id"],
            name=row["name"],
            description=row["description"],
            status=ProjectStatus(row["status"]),
            settings=json.loads(row["settings"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]) if row["updated_at"] else None,
        )

    # =========================================================================
    # Phase 2: Task Extensions (Project Scope)
    # =========================================================================

    async def get_tasks_by_project(
        self, project_id: str, status: TaskStatus | None = None
    ) -> list[TaskResponse]:
        """Get all tasks for a project."""
        assert self._connection is not None

        if status:
            cursor = await self._connection.execute(
                """
                SELECT * FROM tasks
                WHERE project_id = ? AND status = ?
                ORDER BY priority, created_at
                """,
                (project_id, status.value),
            )
        else:
            cursor = await self._connection.execute(
                """
                SELECT * FROM tasks WHERE project_id = ?
                ORDER BY priority, created_at
                """,
                (project_id,),
            )

        rows = await cursor.fetchall()
        return [self._row_to_task(row) for row in rows]

    async def update_task_version(self, task_id: str) -> int:
        """Increment task version for optimistic locking.

        Returns:
            New version number.
        """
        assert self._connection is not None

        await self._connection.execute(
            "UPDATE tasks SET version = version + 1 WHERE id = ?",
            (task_id,),
        )
        await self._connection.commit()

        cursor = await self._connection.execute(
            "SELECT version FROM tasks WHERE id = ?", (task_id,)
        )
        row = await cursor.fetchone()
        return row["version"] if row else 0

    # =========================================================================
    # Phase 2: Lock Management
    # =========================================================================

    async def acquire_lock(
        self,
        lock_id: str,
        resource_type: str,
        resource_id: str,
        holder_id: str,
        expires_at: datetime | None = None,
    ) -> LockResponse | None:
        """Try to acquire a lock on a resource.

        Returns:
            Lock response if acquired, None if resource is already locked.
        """
        assert self._connection is not None
        now = datetime.now(timezone.utc)

        # Clean up expired locks first
        await self._connection.execute(
            "DELETE FROM locks WHERE expires_at IS NOT NULL AND expires_at < ?",
            (now.isoformat(),),
        )

        # Check if already locked
        cursor = await self._connection.execute(
            """
            SELECT * FROM locks
            WHERE resource_type = ? AND resource_id = ?
            """,
            (resource_type, resource_id),
        )
        existing = await cursor.fetchone()

        if existing is not None:
            return None  # Already locked

        # Acquire lock
        await self._connection.execute(
            """
            INSERT INTO locks (id, resource_type, resource_id, holder_id, acquired_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                lock_id,
                resource_type,
                resource_id,
                holder_id,
                now.isoformat(),
                expires_at.isoformat() if expires_at else None,
            ),
        )
        await self._connection.commit()

        return LockResponse(
            id=lock_id,
            resource_type=resource_type,
            resource_id=resource_id,
            holder_id=holder_id,
            acquired_at=now,
            expires_at=expires_at,
        )

    async def release_lock(self, lock_id: str, holder_id: str) -> bool:
        """Release a lock. Only the holder can release it.

        Returns:
            True if released, False otherwise.
        """
        assert self._connection is not None

        cursor = await self._connection.execute(
            "DELETE FROM locks WHERE id = ? AND holder_id = ?",
            (lock_id, holder_id),
        )
        await self._connection.commit()

        return cursor.rowcount > 0

    async def get_lock(
        self, resource_type: str, resource_id: str
    ) -> LockResponse | None:
        """Get the current lock on a resource."""
        assert self._connection is not None
        now = datetime.now(timezone.utc)

        # Clean up expired locks
        await self._connection.execute(
            "DELETE FROM locks WHERE expires_at IS NOT NULL AND expires_at < ?",
            (now.isoformat(),),
        )

        cursor = await self._connection.execute(
            """
            SELECT * FROM locks
            WHERE resource_type = ? AND resource_id = ?
            """,
            (resource_type, resource_id),
        )
        row = await cursor.fetchone()

        if row is None:
            return None

        return self._row_to_lock(row)

    async def get_active_locks(self, holder_id: str | None = None) -> list[LockResponse]:
        """Get all active locks, optionally filtered by holder."""
        assert self._connection is not None
        now = datetime.now(timezone.utc)

        # Clean up expired locks
        await self._connection.execute(
            "DELETE FROM locks WHERE expires_at IS NOT NULL AND expires_at < ?",
            (now.isoformat(),),
        )

        if holder_id:
            cursor = await self._connection.execute(
                "SELECT * FROM locks WHERE holder_id = ?",
                (holder_id,),
            )
        else:
            cursor = await self._connection.execute("SELECT * FROM locks")

        rows = await cursor.fetchall()
        return [self._row_to_lock(row) for row in rows]

    def _row_to_lock(self, row: aiosqlite.Row) -> LockResponse:
        """Convert a database row to a LockResponse."""
        return LockResponse(
            id=row["id"],
            resource_type=row["resource_type"],
            resource_id=row["resource_id"],
            holder_id=row["holder_id"],
            acquired_at=datetime.fromisoformat(row["acquired_at"]),
            expires_at=datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None,
        )

    # =========================================================================
    # Phase 2: Task Events (Audit Log)
    # =========================================================================

    async def create_task_event(
        self,
        event_id: str,
        task_id: str,
        event_type: EventType,
        user_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> TaskEventResponse:
        """Create a task event for audit logging."""
        assert self._connection is not None
        now = datetime.now(timezone.utc).isoformat()
        details_json = json.dumps(details or {})

        await self._connection.execute(
            """
            INSERT INTO task_events (id, task_id, event_type, user_id, details, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (event_id, task_id, event_type.value, user_id, details_json, now),
        )
        await self._connection.commit()

        return TaskEventResponse(
            id=event_id,
            task_id=task_id,
            event_type=event_type,
            user_id=user_id,
            details=details or {},
            created_at=datetime.fromisoformat(now),
        )

    async def get_task_events(
        self, task_id: str, limit: int = 100
    ) -> list[TaskEventResponse]:
        """Get events for a task."""
        assert self._connection is not None

        cursor = await self._connection.execute(
            """
            SELECT * FROM task_events
            WHERE task_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (task_id, limit),
        )
        rows = await cursor.fetchall()

        return [self._row_to_task_event(row) for row in rows]

    async def get_recent_events(self, limit: int = 50) -> list[TaskEventResponse]:
        """Get recent task events across all tasks."""
        assert self._connection is not None

        cursor = await self._connection.execute(
            """
            SELECT * FROM task_events
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = await cursor.fetchall()

        return [self._row_to_task_event(row) for row in rows]

    def _row_to_task_event(self, row: aiosqlite.Row) -> TaskEventResponse:
        """Convert a database row to a TaskEventResponse."""
        return TaskEventResponse(
            id=row["id"],
            task_id=row["task_id"],
            event_type=EventType(row["event_type"]),
            user_id=row["user_id"],
            details=json.loads(row["details"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    # =========================================================================
    # Phase 2: Webhook Management
    # =========================================================================

    async def create_webhook(
        self,
        webhook_id: str,
        workspace_id: str,
        service: WebhookService,
        url: str,
        events: list[EventType] | None = None,
    ) -> WebhookResponse:
        """Create a new webhook."""
        assert self._connection is not None
        now = datetime.now(timezone.utc).isoformat()
        events_list = events or list(EventType)
        events_json = json.dumps([e.value for e in events_list])

        await self._connection.execute(
            """
            INSERT INTO webhooks (id, workspace_id, service, url, events, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (webhook_id, workspace_id, service.value, url, events_json, now),
        )
        await self._connection.commit()

        return WebhookResponse(
            id=webhook_id,
            workspace_id=workspace_id,
            service=service,
            url=url,
            events=events_list,
            active=True,
            created_at=datetime.fromisoformat(now),
        )

    async def get_webhook(self, webhook_id: str) -> WebhookResponse | None:
        """Get a webhook by ID."""
        assert self._connection is not None

        cursor = await self._connection.execute(
            "SELECT * FROM webhooks WHERE id = ?", (webhook_id,)
        )
        row = await cursor.fetchone()

        if row is None:
            return None

        return self._row_to_webhook(row)

    async def get_webhooks_for_workspace(
        self, workspace_id: str
    ) -> list[WebhookResponse]:
        """Get all webhooks for a workspace."""
        assert self._connection is not None

        cursor = await self._connection.execute(
            "SELECT * FROM webhooks WHERE workspace_id = ? ORDER BY created_at",
            (workspace_id,),
        )
        rows = await cursor.fetchall()

        return [self._row_to_webhook(row) for row in rows]

    async def get_active_webhooks_for_event(
        self, workspace_id: str, event_type: EventType
    ) -> list[WebhookResponse]:
        """Get active webhooks that subscribe to a specific event type."""
        assert self._connection is not None

        cursor = await self._connection.execute(
            """
            SELECT * FROM webhooks
            WHERE workspace_id = ? AND active = 1
            """,
            (workspace_id,),
        )
        rows = await cursor.fetchall()

        # Filter by event type in Python (SQLite JSON support is limited)
        webhooks = [self._row_to_webhook(row) for row in rows]
        return [w for w in webhooks if event_type in w.events]

    async def update_webhook(
        self,
        webhook_id: str,
        url: str | None = None,
        events: list[EventType] | None = None,
        active: bool | None = None,
    ) -> WebhookResponse | None:
        """Update a webhook."""
        assert self._connection is not None

        webhook = await self.get_webhook(webhook_id)
        if webhook is None:
            return None

        new_url = url if url is not None else webhook.url
        new_events = events if events is not None else webhook.events
        new_active = active if active is not None else webhook.active

        await self._connection.execute(
            """
            UPDATE webhooks SET url = ?, events = ?, active = ?
            WHERE id = ?
            """,
            (new_url, json.dumps([e.value for e in new_events]), int(new_active), webhook_id),
        )
        await self._connection.commit()

        return await self.get_webhook(webhook_id)

    async def delete_webhook(self, webhook_id: str) -> bool:
        """Delete a webhook."""
        assert self._connection is not None

        cursor = await self._connection.execute(
            "DELETE FROM webhooks WHERE id = ?", (webhook_id,)
        )
        await self._connection.commit()

        return cursor.rowcount > 0

    def _row_to_webhook(self, row: aiosqlite.Row) -> WebhookResponse:
        """Convert a database row to a WebhookResponse."""
        events_raw = json.loads(row["events"])
        events = [EventType(e) for e in events_raw]

        return WebhookResponse(
            id=row["id"],
            workspace_id=row["workspace_id"],
            service=WebhookService(row["service"]),
            url=row["url"],
            events=events,
            active=bool(row["active"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    # =========================================================================
    # Phase 2: Extended Statistics
    # =========================================================================

    async def get_dashboard_stats(self) -> dict[str, Any]:
        """Get dashboard statistics."""
        assert self._connection is not None

        # Count workspaces
        cursor = await self._connection.execute("SELECT COUNT(*) as count FROM workspaces")
        row = await cursor.fetchone()
        total_workspaces = row["count"] if row else 0

        # Count projects
        cursor = await self._connection.execute(
            "SELECT COUNT(*) as count FROM projects WHERE status != 'deleted'"
        )
        row = await cursor.fetchone()
        total_projects = row["count"] if row else 0

        # Count users
        cursor = await self._connection.execute("SELECT COUNT(*) as count FROM users")
        row = await cursor.fetchone()
        total_users = row["count"] if row else 0

        # Count active locks
        now = datetime.now(timezone.utc).isoformat()
        cursor = await self._connection.execute(
            """
            SELECT COUNT(*) as count FROM locks
            WHERE expires_at IS NULL OR expires_at > ?
            """,
            (now,),
        )
        row = await cursor.fetchone()
        active_locks = row["count"] if row else 0

        # Get task stats (reuse existing method)
        task_stats = await self.get_stats()

        return {
            "total_workspaces": total_workspaces,
            "total_projects": total_projects,
            "total_users": total_users,
            "active_locks": active_locks,
            **task_stats,
        }
