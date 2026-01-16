"""FastAPI application entry point for Magickit."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from magickit import __version__
from magickit.api.routes import router, set_dependencies
from magickit.api.routes_v2 import router as router_v2, set_v2_dependencies
from magickit.api.websocket import router as ws_router, broadcast_to_project
from magickit.auth.jwt import JWTHandler
from magickit.auth.middleware import AuthMiddleware
from magickit.config import get_settings
from magickit.core.event_publisher import EventPublisher
from magickit.core.lock_manager import LockManager
from magickit.core.migrations import MigrationManager
from magickit.core.notification_manager import NotificationManager
from magickit.core.project_manager import ProjectManager
from magickit.core.state_manager import StateManager
from magickit.core.task_queue import TaskQueue
from magickit.core.workspace_manager import WorkspaceManager
from magickit.utils.logging import configure_logging, get_logger

logger = get_logger(__name__)

# Template directory path
TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

# Templates instance (will be set during startup)
templates: Jinja2Templates | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan manager.

    Handles startup and shutdown events.
    """
    global templates

    settings = get_settings()

    # Configure logging
    configure_logging(
        level=settings.log_level,
        format_type=settings.log_format,
    )

    logger.info(
        "Starting Magickit",
        version=__version__,
        host=settings.host,
        port=settings.port,
    )

    # Initialize state manager
    state_manager = StateManager(db_path=settings.db_path)
    await state_manager.initialize()

    # Run migrations
    logger.info("Running database migrations...")
    migration_manager = MigrationManager(db_path=settings.db_path)
    applied = await migration_manager.migrate()
    if applied:
        logger.info("Migrations applied", migrations=applied)
    else:
        logger.info("No new migrations to apply")

    # Initialize task queue
    task_queue = TaskQueue(
        state_manager=state_manager,
        max_concurrent=settings.task_max_concurrent,
        default_priority=settings.task_default_priority,
        max_retries=settings.task_max_retries,
    )
    await task_queue.initialize()

    # Phase 2: Initialize JWT handler
    jwt_handler = JWTHandler(
        secret_key=settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
        access_token_expire_minutes=settings.jwt_expire_minutes,
        refresh_token_expire_days=settings.jwt_refresh_expire_days,
    )

    # Phase 2: Initialize managers
    workspace_manager = WorkspaceManager(state_manager)
    project_manager = ProjectManager(state_manager, workspace_manager)
    lock_manager = LockManager(state_manager)

    # Phase 2: Initialize notification manager
    notification_manager = NotificationManager(
        state_manager=state_manager,
        timeout=settings.webhook_timeout,
        max_retries=settings.webhook_max_retries,
    )

    # Phase 2: Initialize event publisher
    event_publisher = EventPublisher(
        state_manager=state_manager,
        notification_manager=notification_manager,
    )
    # Connect WebSocket broadcasting
    event_publisher.set_ws_broadcast(broadcast_to_project)

    # Set router dependencies
    set_dependencies(task_queue, settings)

    # Set Phase 2 router dependencies
    set_v2_dependencies(
        state_manager=state_manager,
        jwt_handler=jwt_handler,
        workspace_manager=workspace_manager,
        project_manager=project_manager,
        lock_manager=lock_manager,
    )

    # Store instances on app.state for access from routes
    app.state.state_manager = state_manager
    app.state.task_queue = task_queue
    app.state.jwt_handler = jwt_handler
    app.state.workspace_manager = workspace_manager
    app.state.project_manager = project_manager
    app.state.lock_manager = lock_manager
    app.state.notification_manager = notification_manager
    app.state.event_publisher = event_publisher
    app.state.auth_enabled = settings.auth_enabled

    # Initialize templates
    if TEMPLATES_DIR.exists():
        templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    logger.info("Magickit initialized successfully")

    yield

    # Shutdown
    logger.info("Shutting down Magickit")
    await state_manager.close()


def create_app() -> FastAPI:
    """Create and configure the FastAPI application.

    Returns:
        Configured FastAPI app.
    """
    settings = get_settings()

    app = FastAPI(
        title="Spirrow-Magickit",
        description="Orchestration layer for Spirrow Platform",
        version=__version__,
        lifespan=lifespan,
    )

    # CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Phase 2: Add auth middleware (will be configured in lifespan)
    # Note: We need to defer JWT handler creation to lifespan
    # For now, middleware will check app.state for configuration

    # Mount static files if directory exists
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # Include Phase 1 routes
    app.include_router(router)

    # Include Phase 2 routes
    app.include_router(router_v2)

    # Include WebSocket routes
    app.include_router(ws_router)

    # Dashboard HTML routes
    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard_page(request: Request) -> HTMLResponse:
        """Render dashboard page."""
        if templates is None:
            return HTMLResponse("<h1>Templates not configured</h1>", status_code=500)
        return templates.TemplateResponse(
            "dashboard.html",
            {"request": request, "active_page": "dashboard"},
        )

    @app.get("/dashboard/projects", response_class=HTMLResponse)
    async def projects_page(request: Request) -> HTMLResponse:
        """Render projects page."""
        if templates is None:
            return HTMLResponse("<h1>Templates not configured</h1>", status_code=500)

        # Get workspaces for selector
        state_manager = request.app.state.state_manager
        workspaces = []
        selected_workspace = "default"

        # Try to get user's workspaces (if authenticated)
        # For now, just return default workspace
        try:
            workspace = await state_manager.get_workspace("default")
            if workspace:
                workspaces = [workspace]
        except Exception:
            pass

        return templates.TemplateResponse(
            "projects.html",
            {
                "request": request,
                "active_page": "projects",
                "workspaces": workspaces,
                "selected_workspace": selected_workspace,
            },
        )

    @app.get("/dashboard/tasks", response_class=HTMLResponse)
    async def tasks_page(request: Request) -> HTMLResponse:
        """Render tasks page."""
        if templates is None:
            return HTMLResponse("<h1>Templates not configured</h1>", status_code=500)

        # Get projects for selector
        state_manager = request.app.state.state_manager
        projects = []
        selected_project = None

        try:
            projects = await state_manager.get_projects_in_workspace("default")
        except Exception:
            pass

        return templates.TemplateResponse(
            "tasks.html",
            {
                "request": request,
                "active_page": "tasks",
                "projects": projects,
                "selected_project": selected_project,
            },
        )

    # Dashboard API endpoints for HTMX
    @app.get("/dashboard/stats")
    async def dashboard_stats_html(request: Request) -> HTMLResponse:
        """Return stats cards HTML for HTMX."""
        state_manager = request.app.state.state_manager
        stats = await state_manager.get_dashboard_stats()

        html = f"""
        <div class="stat-card">
            <div class="stat-value">{stats['total_workspaces']}</div>
            <div class="stat-label">Workspaces</div>
        </div>
        <div class="stat-card">
            <div class="stat-value">{stats['total_projects']}</div>
            <div class="stat-label">Projects</div>
        </div>
        <div class="stat-card primary">
            <div class="stat-value">{stats['total_tasks']}</div>
            <div class="stat-label">Total Tasks</div>
        </div>
        <div class="stat-card success">
            <div class="stat-value">{stats['tasks_by_status'].get('completed', 0)}</div>
            <div class="stat-label">Completed</div>
        </div>
        <div class="stat-card warning">
            <div class="stat-value">{stats['tasks_by_status'].get('running', 0)}</div>
            <div class="stat-label">Running</div>
        </div>
        <div class="stat-card danger">
            <div class="stat-value">{stats['tasks_by_status'].get('failed', 0)}</div>
            <div class="stat-label">Failed</div>
        </div>
        """
        return HTMLResponse(html)

    @app.get("/dashboard/events")
    async def dashboard_events_html(request: Request) -> HTMLResponse:
        """Return events list HTML for HTMX."""
        state_manager = request.app.state.state_manager
        events = await state_manager.get_recent_events(limit=10)

        if not events:
            return HTMLResponse('<p class="empty-state">No recent events</p>')

        html = ""
        for event in events:
            event_class = event.event_type.value
            html += f"""
            <div class="event-item">
                <div class="event-icon {event_class}">
                    {_get_event_icon(event.event_type.value)}
                </div>
                <div class="event-content">
                    <div class="event-title">Task {event.event_type.value}</div>
                    <div class="event-time">{event.task_id[:8]}... - {event.created_at.strftime('%H:%M:%S')}</div>
                </div>
            </div>
            """
        return HTMLResponse(html)

    @app.get("/dashboard/locks")
    async def dashboard_locks_html(request: Request) -> HTMLResponse:
        """Return locks list HTML for HTMX."""
        state_manager = request.app.state.state_manager
        locks = await state_manager.get_active_locks()

        if not locks:
            return HTMLResponse('<p class="empty-state">No active locks</p>')

        html = ""
        for lock in locks:
            html += f"""
            <div class="lock-item">
                <div class="lock-info">
                    <div class="lock-resource">{lock.resource_type}: {lock.resource_id[:8]}...</div>
                    <div class="lock-holder">Held by: {lock.holder_id[:8]}...</div>
                </div>
            </div>
            """
        return HTMLResponse(html)

    @app.get("/dashboard/queue")
    async def dashboard_queue_html(request: Request) -> HTMLResponse:
        """Return task queue HTML for HTMX."""
        task_queue = request.app.state.task_queue
        tasks = await task_queue.get_all_tasks()

        # Get pending/queued tasks
        pending_tasks = [t for t in tasks if t.status.value in ('pending', 'queued', 'running')][:10]

        if not pending_tasks:
            return HTMLResponse('<p class="empty-state">Queue is empty</p>')

        html = '<table class="table"><thead><tr><th>Name</th><th>Service</th><th>Priority</th><th>Status</th></tr></thead><tbody>'
        for task in pending_tasks:
            status_class = task.status.value
            html += f"""
            <tr>
                <td>{task.name}</td>
                <td>{task.service.value}</td>
                <td>{task.priority}</td>
                <td><span class="status-badge {status_class}">{task.status.value}</span></td>
            </tr>
            """
        html += '</tbody></table>'
        return HTMLResponse(html)

    return app


def _get_event_icon(event_type: str) -> str:
    """Get icon character for event type."""
    icons = {
        "created": "+",
        "started": ">",
        "completed": "V",
        "failed": "X",
        "cancelled": "-",
        "updated": "*",
    }
    return icons.get(event_type, "?")


# Create app instance
app = create_app()


def main() -> None:
    """Run the application."""
    settings = get_settings()

    uvicorn.run(
        "magickit.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
    )


if __name__ == "__main__":
    main()
