"""FastAPI application entry point for Magickit."""

from __future__ import annotations

from contextlib import asynccontextmanager
from html import escape
from pathlib import Path
from typing import AsyncGenerator

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from magickit import __version__
from magickit.api.models import TaskStatus
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
from magickit.mcp.tools import chatroom as chatroom_tools
from magickit.web import close_client as close_chatroom_ui_client
from magickit.web import dashboard_router as chatroom_dashboard_router
from magickit.web import deploys_router, ops_router
from magickit.web import router as chatroom_ui_router
from magickit.web import writes_router as chatroom_writes_router

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

    # Bind settings for the chatroom gates. The MCP process does this via
    # register_tools; this process serves the browser write path, which runs
    # the same gates and would otherwise find them unconfigured.
    chatroom_tools.configure(settings)

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
    await close_chatroom_ui_client()
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

    # Gated chatroom writes. Registered before the proxy so the three POST
    # routes are handled here -- with the role / naysayer / embodiment gates
    # -- instead of being forwarded to Conclair's own ungated form handlers.
    app.include_router(chatroom_writes_router)

    # Chatroom panel for the dashboard.
    app.include_router(chatroom_dashboard_router)

    # 稼働状況 (ops) view. Claims "/dashboard" itself -- it answers the
    # question a human opens the dashboard to ask ("is anything running?"),
    # which the queue view below cannot: that one reports Magickit's own
    # SQLite task table, not the autonomous loop. The queue view keeps its
    # panels and moves to /dashboard/system.
    app.include_router(ops_router)
    app.include_router(deploys_router)

    # Chatroom UI proxy. Registered BEFORE the /static mount on purpose:
    # Starlette matches routes in insertion order, and this router claims the
    # two Conclair assets (conclair.css / conclair.js) that would otherwise
    # fall into Magickit's own /static mount and 404 there.
    app.include_router(chatroom_ui_router)

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
    #
    # `/dashboard/system`, not `/dashboard`: this page reports Magickit's
    # own queue, locks and events. That is a useful view of the service,
    # but it is not the autonomous loop, and it held the URL a human
    # reaches for when asking whether anything is running. `web/ops.py`
    # answers that and now owns `/dashboard`.
    @app.get("/dashboard/system", response_class=HTMLResponse)
    async def dashboard_page(request: Request) -> HTMLResponse:
        """Render the Magickit-internals dashboard page."""
        if templates is None:
            return HTMLResponse("<h1>Templates not configured</h1>", status_code=500)
        return templates.TemplateResponse(
            "dashboard.html",
            {"request": request, "active_page": "system"},
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
    #
    # `_stats`, not `stats`: routes_v2 serves the JSON DashboardStats API at
    # /dashboard/stats, and it is registered first (include_router above runs
    # before these decorators), so Starlette matched it and this handler was
    # dead. The dashboard rendered a raw JSON dump where the stat cards
    # belong -- silently, because both handlers answer 200.
    #
    # The API route keeps the plain name: it is typed, authenticated and
    # covered by a test, so it is the contract. This one is a fragment for
    # one template. The sibling fragments below (events / locks / queue) do
    # not collide today and are left as they are; a duplicate-route test now
    # fails loudly if that ever changes.
    @app.get("/dashboard/_stats")
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

    # `_projects`, not `projects`: the same split as `_stats` above, for a
    # different cause. Nothing collided here -- there was simply no fragment
    # handler at all, so projects.html pointed its `#projects-list` at its
    # own page URL and HTMX swapped the whole page into the grid inside it.
    # Each copy brought another `#projects-list` with `hx-trigger="load"`,
    # so it fed itself: measured at 250 nested copies of the page and a
    # 137,870px document before it stopped, plus a burst of requests at the
    # server for each one.
    #
    # A page and a fragment of that page cannot share a URL: HTMX asks the
    # same path a full navigation asks, and only one of the two answers can
    # be right.
    @app.get("/dashboard/_projects")
    async def dashboard_projects_html(
        request: Request, workspace_id: str = "default"
    ) -> HTMLResponse:
        """Return project cards HTML for HTMX."""
        state_manager = request.app.state.state_manager

        try:
            projects = await state_manager.get_projects_in_workspace(workspace_id)
        except Exception:
            # Sibling fragments answer with their empty state rather than an
            # error; a 500 into an innerHTML swap shows as a blank panel.
            projects = []

        if not projects:
            return HTMLResponse('<p class="empty-state">No projects yet</p>')

        # escape(): these are user-supplied names and descriptions going into
        # an f-string. The sibling fragments interpolate their own values raw,
        # which is a pre-existing question for the ones carrying user input --
        # not a reason to add another.
        html = ""
        for project in projects:
            created = project.created_at.strftime("%Y-%m-%d")
            html += f"""
            <div class="project-card">
                <h3>{escape(project.name)}</h3>
                <p>{escape(project.description or "No description")}</p>
                <div class="project-meta">
                    <span class="status-badge {escape(project.status.value)}">
                        {escape(project.status.value)}
                    </span>
                    <span>Created {created}</span>
                </div>
            </div>
            """
        return HTMLResponse(html)

    # tasks.html had the same self-nesting as projects.html, in four places
    # -- both filter selects, the refresh button and the `#tasks-list` tbody
    # all pointed at /dashboard/tasks, the page. Measured at 121 nested
    # copies and a 64,284px document. This is the fragment they meant.
    @app.get("/dashboard/_tasks")
    async def dashboard_tasks_html(
        request: Request, project_id: str = "", status: str = ""
    ) -> HTMLResponse:
        """Return task table rows HTML for HTMX."""
        state_manager = request.app.state.state_manager

        # An unrecognised status filters nothing rather than 500ing into a
        # table body; the select cannot produce one, but the URL can.
        wanted: TaskStatus | None = None
        if status:
            try:
                wanted = TaskStatus(status)
            except ValueError:
                wanted = None

        try:
            if project_id:
                tasks = await state_manager.get_tasks_by_project(project_id, wanted)
            elif wanted is not None:
                tasks = await state_manager.get_tasks_by_status(wanted)
            else:
                tasks = await state_manager.get_all_tasks()
        except Exception:
            tasks = []

        if not tasks:
            return HTMLResponse(
                '<tr><td colspan="7" class="empty-state">No tasks</td></tr>'
            )

        # Bounded because this table is not paginated and the fragment
        # re-fetches every 5s. The cap is visible in the table rather than
        # silent -- a list that stops at a round number with no explanation
        # reads as data loss.
        capped = tasks[:100]

        html = ""
        for task in capped:
            html += f"""
            <tr>
                <td>{escape(task.id[:8])}...</td>
                <td>{escape(task.name)}</td>
                <td>{escape(task.service.value)}</td>
                <td>{task.priority}</td>
                <td><span class="status-badge {escape(task.status.value)}">{escape(task.status.value)}</span></td>
                <td>{task.created_at.strftime('%Y-%m-%d %H:%M')}</td>
                <td>
                    <button class="btn btn-sm" onclick="showTaskDetail('{escape(task.id)}')">
                        Details
                    </button>
                </td>
            </tr>
            """

        if len(tasks) > len(capped):
            html += (
                f'<tr><td colspan="7" class="empty-state">'
                f"showing {len(capped)} of {len(tasks)} tasks"
                f"</td></tr>"
            )
        return HTMLResponse(html)

    # `/dashboard/task-stats` never existed: tasks.html polled it every 10s
    # and took a 404 each time, so the four pills sat at their placeholder
    # "-" forever while the console filled up. Same data the stat cards use.
    @app.get("/dashboard/_task_stats")
    async def dashboard_task_stats_html(request: Request) -> HTMLResponse:
        """Return task stat pills HTML for HTMX."""
        state_manager = request.app.state.state_manager

        try:
            stats = await state_manager.get_dashboard_stats()
            by_status = stats.get("tasks_by_status", {})
        except Exception:
            by_status = {}

        pills = ""
        for key, label in (
            ("pending", "Pending"),
            ("running", "Running"),
            ("completed", "Completed"),
            ("failed", "Failed"),
        ):
            pills += f"""
            <div class="stat-pill {key}">
                <span class="count">{by_status.get(key, 0)}</span>
                <span class="label">{label}</span>
            </div>
            """
        return HTMLResponse(pills)

    # The Details button in each task row opened the modal and left it on
    # "Loading..." -- tasks.html asks for the task here and nothing served
    # it. Under `_tasks/` rather than `tasks/{id}` so the fragment prefix
    # stays the thing that marks a fragment: a real task *page* is a
    # plausible thing to add later, and it would want /dashboard/tasks/{id}.
    @app.get("/dashboard/_tasks/{task_id}")
    async def dashboard_task_detail_html(
        request: Request, task_id: str
    ) -> HTMLResponse:
        """Return one task's detail HTML for the modal."""
        state_manager = request.app.state.state_manager

        try:
            task = await state_manager.get_task(task_id)
        except Exception:
            task = None

        if task is None:
            return HTMLResponse(
                '<p class="empty-state">Task not found</p>', status_code=404
            )

        def row(label: str, value: str) -> str:
            return (
                f'<div class="detail-row"><span class="detail-label">{label}'
                f'</span><span class="detail-value">{value}</span></div>'
            )

        rows = [
            row("ID", escape(task.id)),
            row("Name", escape(task.name)),
            row("Description", escape(task.description or "—")),
            row("Service", escape(task.service.value)),
            row("Priority", str(task.priority)),
            row(
                "Status",
                f'<span class="status-badge {escape(task.status.value)}">'
                f"{escape(task.status.value)}</span>",
            ),
            row("Created", task.created_at.strftime("%Y-%m-%d %H:%M:%S")),
        ]
        if task.started_at:
            rows.append(row("Started", task.started_at.strftime("%Y-%m-%d %H:%M:%S")))
        if task.completed_at:
            rows.append(
                row("Completed", task.completed_at.strftime("%Y-%m-%d %H:%M:%S"))
            )
        if task.retry_count:
            rows.append(row("Retries", str(task.retry_count)))
        if task.dependencies:
            rows.append(
                row("Depends on", escape(", ".join(task.dependencies)))
            )
        if task.error:
            rows.append(row("Error", f'<pre class="detail-pre">{escape(task.error)}</pre>'))
        if task.result:
            rows.append(
                row("Result", f'<pre class="detail-pre">{escape(str(task.result))}</pre>')
            )

        return HTMLResponse("".join(rows))

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
                    <div class="event-title">Task {escape(event.event_type.value)}</div>
                    <div class="event-time">{escape(event.task_id[:8])}... - {event.created_at.strftime('%H:%M:%S')}</div>
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
                    <div class="lock-resource">{escape(lock.resource_type)}: {escape(lock.resource_id[:8])}...</div>
                    <div class="lock-holder">Held by: {escape(lock.holder_id[:8])}...</div>
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
                <td>{escape(task.name)}</td>
                <td>{escape(task.service.value)}</td>
                <td>{task.priority}</td>
                <td><span class="status-badge {escape(status_class)}">{escape(task.status.value)}</span></td>
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
