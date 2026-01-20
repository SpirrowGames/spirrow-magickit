"""Database migration system for Magickit.

Handles versioned database schema changes with forward migration support.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Coroutine
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from magickit.utils.logging import get_logger

logger = get_logger(__name__)


class MigrationError(Exception):
    """Raised when a migration fails."""

    pass


MigrationFunc = Callable[[aiosqlite.Connection], Coroutine[None, None, None]]


class Migration:
    """Represents a single database migration."""

    def __init__(
        self,
        version: int,
        name: str,
        up: MigrationFunc,
        description: str = "",
    ) -> None:
        """Initialize migration.

        Args:
            version: Sequential version number.
            name: Short name for the migration.
            up: Async function to apply migration.
            description: Human-readable description.
        """
        self.version = version
        self.name = name
        self.up = up
        self.description = description


class MigrationManager:
    """Manages database migrations."""

    def __init__(self, db_path: str) -> None:
        """Initialize migration manager.

        Args:
            db_path: Path to SQLite database file.
        """
        self.db_path = db_path
        self._migrations: list[Migration] = []
        self._register_migrations()

    def _register_migrations(self) -> None:
        """Register all migrations in order."""
        # Migration 1: Phase 2 schema additions
        self._migrations.append(
            Migration(
                version=1,
                name="phase2_schema",
                up=self._migration_001_phase2_schema,
                description="Add Phase 2 tables: workspaces, projects, users, locks, webhooks",
            )
        )

    async def _ensure_migrations_table(self, conn: aiosqlite.Connection) -> None:
        """Create migrations tracking table if not exists."""
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS _migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL,
                description TEXT DEFAULT ''
            )
            """
        )
        await conn.commit()

    async def _get_current_version(self, conn: aiosqlite.Connection) -> int:
        """Get the current database schema version."""
        cursor = await conn.execute(
            "SELECT MAX(version) FROM _migrations"
        )
        row = await cursor.fetchone()
        return row[0] if row and row[0] is not None else 0

    async def _record_migration(
        self, conn: aiosqlite.Connection, migration: Migration
    ) -> None:
        """Record a migration as applied."""
        await conn.execute(
            """
            INSERT INTO _migrations (version, name, applied_at, description)
            VALUES (?, ?, ?, ?)
            """,
            (
                migration.version,
                migration.name,
                datetime.now(timezone.utc).isoformat(),
                migration.description,
            ),
        )

    async def migrate(self) -> list[str]:
        """Run all pending migrations.

        Returns:
            List of applied migration names.
        """
        applied: list[str] = []

        async with aiosqlite.connect(self.db_path) as conn:
            await self._ensure_migrations_table(conn)
            current_version = await self._get_current_version(conn)

            logger.info(
                "migration_check",
                current_version=current_version,
                available_migrations=len(self._migrations),
            )

            for migration in self._migrations:
                if migration.version > current_version:
                    logger.info(
                        "applying_migration",
                        version=migration.version,
                        name=migration.name,
                    )
                    try:
                        await migration.up(conn)
                        await self._record_migration(conn, migration)
                        await conn.commit()
                        applied.append(migration.name)
                        logger.info(
                            "migration_applied",
                            version=migration.version,
                            name=migration.name,
                        )
                    except Exception as e:
                        await conn.rollback()
                        logger.error(
                            "migration_failed",
                            version=migration.version,
                            name=migration.name,
                            error=str(e),
                        )
                        raise MigrationError(
                            f"Migration {migration.version} ({migration.name}) failed: {e}"
                        ) from e

        return applied

    async def get_status(self) -> dict[str, list[dict]]:
        """Get migration status.

        Returns:
            Dict with 'applied' and 'pending' migrations.
        """
        async with aiosqlite.connect(self.db_path) as conn:
            await self._ensure_migrations_table(conn)
            current_version = await self._get_current_version(conn)

            cursor = await conn.execute(
                "SELECT version, name, applied_at, description FROM _migrations ORDER BY version"
            )
            rows = await cursor.fetchall()

            applied = [
                {
                    "version": row[0],
                    "name": row[1],
                    "applied_at": row[2],
                    "description": row[3],
                }
                for row in rows
            ]

            pending = [
                {
                    "version": m.version,
                    "name": m.name,
                    "description": m.description,
                }
                for m in self._migrations
                if m.version > current_version
            ]

            return {"applied": applied, "pending": pending}

    # =========================================================================
    # Migration Functions
    # =========================================================================

    async def _migration_001_phase2_schema(self, conn: aiosqlite.Connection) -> None:
        """Phase 2 schema: workspaces, projects, users, locks, webhooks, events."""
        # Workspaces table
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS workspaces (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                owner_id TEXT,
                settings TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT
            )
            """
        )

        # Projects table
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                status TEXT DEFAULT 'active',
                settings TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT,
                FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE
            )
            """
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_projects_workspace ON projects(workspace_id)"
        )

        # Users table
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT DEFAULT 'member',
                created_at TEXT NOT NULL,
                last_login TEXT
            )
            """
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)"
        )

        # Workspace members table
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS workspace_members (
                workspace_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                role TEXT DEFAULT 'member',
                joined_at TEXT NOT NULL,
                PRIMARY KEY (workspace_id, user_id),
                FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )

        # Project members table
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS project_members (
                project_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                role TEXT DEFAULT 'member',
                permissions TEXT DEFAULT '[]',
                joined_at TEXT NOT NULL,
                PRIMARY KEY (project_id, user_id),
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )

        # Add columns to existing tasks table (SQLite ALTER TABLE limitations)
        # We need to check if columns exist first
        cursor = await conn.execute("PRAGMA table_info(tasks)")
        columns = {row[1] for row in await cursor.fetchall()}

        if "project_id" not in columns:
            await conn.execute("ALTER TABLE tasks ADD COLUMN project_id TEXT")
        if "created_by" not in columns:
            await conn.execute("ALTER TABLE tasks ADD COLUMN created_by TEXT")
        if "version" not in columns:
            await conn.execute("ALTER TABLE tasks ADD COLUMN version INTEGER DEFAULT 1")

        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_created_by ON tasks(created_by)"
        )

        # Locks table
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS locks (
                id TEXT PRIMARY KEY,
                resource_type TEXT NOT NULL,
                resource_id TEXT NOT NULL,
                holder_id TEXT NOT NULL,
                acquired_at TEXT NOT NULL,
                expires_at TEXT,
                UNIQUE(resource_type, resource_id)
            )
            """
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_locks_resource ON locks(resource_type, resource_id)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_locks_holder ON locks(holder_id)"
        )

        # Task events (audit log) table
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS task_events (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                user_id TEXT,
                details TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
            )
            """
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events(task_id)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_task_events_type ON task_events(event_type)"
        )

        # Webhooks table
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS webhooks (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                service TEXT NOT NULL,
                url TEXT NOT NULL,
                events TEXT DEFAULT '[]',
                active INTEGER DEFAULT 1,
                created_at TEXT NOT NULL,
                FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE
            )
            """
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_webhooks_workspace ON webhooks(workspace_id)"
        )

        # Create default workspace and project for backward compatibility
        default_workspace_id = "default"
        default_project_id = "default"
        now = datetime.now(timezone.utc).isoformat()

        await conn.execute(
            """
            INSERT OR IGNORE INTO workspaces (id, name, settings, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (default_workspace_id, "Default Workspace", "{}", now),
        )

        await conn.execute(
            """
            INSERT OR IGNORE INTO projects (id, workspace_id, name, description, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                default_project_id,
                default_workspace_id,
                "Default Project",
                "Default project for backward compatibility",
                now,
            ),
        )

        # Assign existing tasks to default project
        await conn.execute(
            "UPDATE tasks SET project_id = ? WHERE project_id IS NULL",
            (default_project_id,),
        )
