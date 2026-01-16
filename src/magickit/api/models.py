"""Request and Response models for Magickit API."""

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# === Enums ===


class TaskStatus(str, Enum):
    """Task execution status."""

    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskPriority(int, Enum):
    """Task priority levels."""

    CRITICAL = 1
    HIGH = 3
    NORMAL = 5
    LOW = 7
    BACKGROUND = 9


class ServiceType(str, Enum):
    """Available service types for routing."""

    LEXORA = "lexora"
    COGNILENS = "cognilens"
    PRISMIND = "prismind"
    UNREALWISE = "unrealwise"


# === Task Models ===


class TaskCreate(BaseModel):
    """Request model for creating a new task."""

    name: str = Field(..., description="Task name")
    description: str = Field(default="", description="Task description")
    service: ServiceType = Field(..., description="Target service")
    payload: dict[str, Any] = Field(default_factory=dict, description="Task payload")
    priority: int = Field(default=TaskPriority.NORMAL, ge=1, le=9, description="Priority (1-9)")
    dependencies: list[str] = Field(default_factory=list, description="Dependent task IDs")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Additional metadata")


class TaskResponse(BaseModel):
    """Response model for task information."""

    id: str = Field(..., description="Unique task ID")
    name: str
    description: str
    service: ServiceType
    payload: dict[str, Any]
    priority: int
    status: TaskStatus
    dependencies: list[str]
    metadata: dict[str, Any]
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    retry_count: int = 0


class TaskListResponse(BaseModel):
    """Response model for listing tasks."""

    tasks: list[TaskResponse]
    total: int
    pending: int
    running: int
    completed: int


class TaskCompleteRequest(BaseModel):
    """Request model for completing a task."""

    result: dict[str, Any] = Field(default_factory=dict, description="Task result")


class TaskFailRequest(BaseModel):
    """Request model for failing a task."""

    error: str = Field(..., description="Error message")


# === Orchestration Models ===


class OrchestrateRequest(BaseModel):
    """Request model for orchestration."""

    query: str = Field(..., description="User query or instruction")
    context: str = Field(default="", description="Additional context")
    max_tokens: int = Field(default=2000, description="Maximum tokens for context")
    enable_rag: bool = Field(default=True, description="Enable RAG enhancement")


class OrchestrateResponse(BaseModel):
    """Response model for orchestration."""

    task_ids: list[str] = Field(..., description="Created task IDs")
    plan: list[dict[str, Any]] = Field(..., description="Execution plan")
    estimated_services: list[ServiceType] = Field(..., description="Services to be used")


# === Routing Models ===


class RouteRequest(BaseModel):
    """Request model for routing decision."""

    query: str = Field(..., description="Query to route")
    context: str = Field(default="", description="Additional context")


class RouteResponse(BaseModel):
    """Response model for routing decision."""

    service: ServiceType = Field(..., description="Recommended service")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Confidence score")
    reasoning: str = Field(..., description="Routing reasoning")
    alternatives: list[dict[str, Any]] = Field(
        default_factory=list, description="Alternative services with scores"
    )


# === Health Check Models ===


class ServiceHealth(BaseModel):
    """Health status for a single service."""

    name: str
    status: str = Field(..., description="healthy, unhealthy, or unknown")
    latency_ms: float | None = None
    error: str | None = None


class HealthResponse(BaseModel):
    """Response model for health check."""

    status: str = Field(..., description="Overall status: healthy or degraded")
    version: str
    uptime_seconds: float
    services: list[ServiceHealth]


# === Statistics Models ===


class StatsResponse(BaseModel):
    """Response model for system statistics."""

    total_tasks: int
    tasks_by_status: dict[str, int]
    tasks_by_service: dict[str, int]
    avg_completion_time_ms: float
    queue_depth: int
    active_tasks: int


# === Phase 2: Enums ===


class UserRole(str, Enum):
    """User role levels."""

    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"


class ProjectStatus(str, Enum):
    """Project status."""

    ACTIVE = "active"
    ARCHIVED = "archived"
    DELETED = "deleted"


class EventType(str, Enum):
    """Task event types for audit logging."""

    CREATED = "created"
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UPDATED = "updated"
    ASSIGNED = "assigned"
    COMMENT = "comment"


class WebhookService(str, Enum):
    """Supported webhook services."""

    SLACK = "slack"
    DISCORD = "discord"


# === Phase 2: Authentication Models ===


class UserCreate(BaseModel):
    """Request model for user registration."""

    email: str = Field(..., description="User email")
    name: str = Field(..., description="User display name")
    password: str = Field(..., min_length=8, description="Password (min 8 chars)")


class UserLogin(BaseModel):
    """Request model for user login."""

    email: str = Field(..., description="User email")
    password: str = Field(..., description="User password")


class UserResponse(BaseModel):
    """Response model for user information."""

    id: str
    email: str
    name: str
    role: UserRole
    created_at: datetime
    last_login: datetime | None = None


class TokenResponse(BaseModel):
    """Response model for authentication tokens."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(..., description="Token expiry in seconds")


class TokenRefreshRequest(BaseModel):
    """Request model for token refresh."""

    refresh_token: str


# === Phase 2: Workspace Models ===


class WorkspaceCreate(BaseModel):
    """Request model for creating a workspace."""

    name: str = Field(..., min_length=1, max_length=100, description="Workspace name")
    settings: dict[str, Any] = Field(default_factory=dict, description="Workspace settings")


class WorkspaceUpdate(BaseModel):
    """Request model for updating a workspace."""

    name: str | None = Field(None, min_length=1, max_length=100)
    settings: dict[str, Any] | None = None


class WorkspaceResponse(BaseModel):
    """Response model for workspace information."""

    id: str
    name: str
    owner_id: str | None = None
    settings: dict[str, Any]
    created_at: datetime
    updated_at: datetime | None = None


class WorkspaceMemberAdd(BaseModel):
    """Request model for adding a workspace member."""

    user_id: str = Field(..., description="User ID to add")
    role: UserRole = Field(default=UserRole.MEMBER, description="Member role")


class WorkspaceMemberResponse(BaseModel):
    """Response model for workspace member."""

    user_id: str
    user_name: str
    user_email: str
    role: UserRole
    joined_at: datetime


# === Phase 2: Project Models ===


class ProjectCreate(BaseModel):
    """Request model for creating a project."""

    name: str = Field(..., min_length=1, max_length=100, description="Project name")
    description: str = Field(default="", description="Project description")
    settings: dict[str, Any] = Field(default_factory=dict, description="Project settings")


class ProjectUpdate(BaseModel):
    """Request model for updating a project."""

    name: str | None = Field(None, min_length=1, max_length=100)
    description: str | None = None
    status: ProjectStatus | None = None
    settings: dict[str, Any] | None = None


class ProjectResponse(BaseModel):
    """Response model for project information."""

    id: str
    workspace_id: str
    name: str
    description: str
    status: ProjectStatus
    settings: dict[str, Any]
    created_at: datetime
    updated_at: datetime | None = None


class ProjectMemberAdd(BaseModel):
    """Request model for adding a project member."""

    user_id: str = Field(..., description="User ID to add")
    role: UserRole = Field(default=UserRole.MEMBER, description="Member role")
    permissions: list[str] = Field(default_factory=list, description="Specific permissions")


class ProjectMemberResponse(BaseModel):
    """Response model for project member."""

    user_id: str
    user_name: str
    user_email: str
    role: UserRole
    permissions: list[str]
    joined_at: datetime


# === Phase 2: Lock Models ===


class LockAcquire(BaseModel):
    """Request model for acquiring a lock."""

    resource_type: str = Field(..., description="Type of resource (e.g., 'task', 'project')")
    resource_id: str = Field(..., description="ID of the resource to lock")
    ttl_seconds: int | None = Field(default=300, ge=1, le=3600, description="Lock TTL in seconds")


class LockResponse(BaseModel):
    """Response model for lock information."""

    id: str
    resource_type: str
    resource_id: str
    holder_id: str
    acquired_at: datetime
    expires_at: datetime | None = None


# === Phase 2: Task Event Models ===


class TaskEventResponse(BaseModel):
    """Response model for task event."""

    id: str
    task_id: str
    event_type: EventType
    user_id: str | None = None
    details: dict[str, Any]
    created_at: datetime


# === Phase 2: Webhook Models ===


class WebhookCreate(BaseModel):
    """Request model for creating a webhook."""

    service: WebhookService = Field(..., description="Webhook service type")
    url: str = Field(..., description="Webhook URL")
    events: list[EventType] = Field(
        default_factory=lambda: list(EventType),
        description="Events to trigger webhook",
    )


class WebhookUpdate(BaseModel):
    """Request model for updating a webhook."""

    url: str | None = None
    events: list[EventType] | None = None
    active: bool | None = None


class WebhookResponse(BaseModel):
    """Response model for webhook information."""

    id: str
    workspace_id: str
    service: WebhookService
    url: str
    events: list[EventType]
    active: bool
    created_at: datetime


# === Phase 2: Extended Task Models ===


class TaskCreateWithProject(TaskCreate):
    """Extended task creation with project context."""

    project_id: str | None = Field(None, description="Project ID (defaults to 'default')")


class TaskResponseWithProject(TaskResponse):
    """Extended task response with project context."""

    project_id: str | None = None
    created_by: str | None = None
    version: int = 1


# === Phase 2: WebSocket Models ===


class WebSocketMessage(BaseModel):
    """WebSocket message format."""

    type: str = Field(..., description="Message type")
    payload: dict[str, Any] = Field(default_factory=dict, description="Message payload")
    timestamp: datetime = Field(default_factory=lambda: datetime.now())


class TaskUpdateMessage(BaseModel):
    """WebSocket message for task updates."""

    task_id: str
    status: TaskStatus
    project_id: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None


# === Phase 2: Dashboard Models ===


class DashboardStats(BaseModel):
    """Dashboard statistics response."""

    total_workspaces: int
    total_projects: int
    total_users: int
    total_tasks: int
    tasks_by_status: dict[str, int]
    tasks_by_service: dict[str, int]
    recent_events: list[TaskEventResponse]
    active_locks: int
