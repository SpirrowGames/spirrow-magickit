"""API routes for Magickit."""

import time
from typing import Any

from fastapi import APIRouter, HTTPException, status

from magickit.adapters import CognilensAdapter, LexoraAdapter, PrismindAdapter
from magickit.api.models import (
    HealthResponse,
    OrchestrateRequest,
    OrchestrateResponse,
    RouteRequest,
    RouteResponse,
    ServiceHealth,
    ServiceType,
    StatsResponse,
    TaskCompleteRequest,
    TaskCreate,
    TaskFailRequest,
    TaskListResponse,
    TaskResponse,
    TaskStatus,
)
from magickit.config import Settings
from magickit.core.task_queue import TaskQueue
from magickit.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter()

# These will be set by main.py during app initialization
_task_queue: TaskQueue | None = None
_settings: Settings | None = None
_start_time: float = time.time()


def set_dependencies(task_queue: TaskQueue, settings: Settings) -> None:
    """Set router dependencies.

    Args:
        task_queue: Task queue instance.
        settings: Application settings.
    """
    global _task_queue, _settings
    _task_queue = task_queue
    _settings = settings


def get_task_queue() -> TaskQueue:
    """Get the task queue instance."""
    if _task_queue is None:
        raise RuntimeError("Task queue not initialized")
    return _task_queue


def get_settings() -> Settings:
    """Get the settings instance."""
    if _settings is None:
        raise RuntimeError("Settings not initialized")
    return _settings


# === Task Endpoints ===


@router.post("/tasks", response_model=list[str], status_code=status.HTTP_201_CREATED)
async def create_tasks(tasks: list[TaskCreate]) -> list[str]:
    """Register one or more tasks.

    Args:
        tasks: List of tasks to create.

    Returns:
        List of created task IDs.
    """
    queue = get_task_queue()

    try:
        task_ids = await queue.register(tasks)
        logger.info("Tasks created", count=len(task_ids))
        return task_ids
    except Exception as e:
        logger.error("Failed to create tasks", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@router.get("/tasks", response_model=TaskListResponse)
async def list_tasks() -> TaskListResponse:
    """List all tasks."""
    queue = get_task_queue()
    tasks = await queue.get_all_tasks()

    # Count by status
    pending = sum(1 for t in tasks if t.status == TaskStatus.PENDING)
    running = sum(1 for t in tasks if t.status == TaskStatus.RUNNING)
    completed = sum(1 for t in tasks if t.status == TaskStatus.COMPLETED)

    return TaskListResponse(
        tasks=tasks,
        total=len(tasks),
        pending=pending,
        running=running,
        completed=completed,
    )


@router.get("/tasks/next", response_model=TaskResponse | None)
async def get_next_task() -> TaskResponse | None:
    """Get the next task ready for execution.

    Returns:
        Next task or null if no tasks are ready.
    """
    queue = get_task_queue()
    return await queue.get_next()


@router.get("/tasks/{task_id}", response_model=TaskResponse)
async def get_task(task_id: str) -> TaskResponse:
    """Get a task by ID.

    Args:
        task_id: Task ID.

    Returns:
        Task details.
    """
    queue = get_task_queue()
    task = await queue.get_task(task_id)

    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task {task_id} not found",
        )

    return task


@router.post("/tasks/{task_id}/complete", response_model=TaskResponse)
async def complete_task(task_id: str, request: TaskCompleteRequest) -> TaskResponse:
    """Mark a task as completed.

    Args:
        task_id: Task ID.
        request: Completion request with result.

    Returns:
        Updated task.
    """
    queue = get_task_queue()
    task = await queue.complete(task_id, result=request.result)

    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task {task_id} not found",
        )

    return task


@router.post("/tasks/{task_id}/fail", response_model=TaskResponse)
async def fail_task(task_id: str, request: TaskFailRequest) -> TaskResponse:
    """Mark a task as failed.

    Args:
        task_id: Task ID.
        request: Failure request with error message.

    Returns:
        Updated task.
    """
    queue = get_task_queue()
    task = await queue.fail(task_id, error=request.error)

    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task {task_id} not found",
        )

    return task


@router.delete("/tasks/{task_id}", response_model=TaskResponse)
async def cancel_task(task_id: str) -> TaskResponse:
    """Cancel a pending or queued task.

    Args:
        task_id: Task ID.

    Returns:
        Updated task.
    """
    queue = get_task_queue()
    task = await queue.cancel(task_id)

    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task {task_id} not found",
        )

    return task


# === Orchestration Endpoints ===


@router.post("/orchestrate", response_model=OrchestrateResponse)
async def orchestrate(request: OrchestrateRequest) -> OrchestrateResponse:
    """Orchestrate a complex task.

    Analyzes the query, creates a plan, and registers tasks.

    Args:
        request: Orchestration request.

    Returns:
        Orchestration response with created tasks and plan.
    """
    queue = get_task_queue()
    settings = get_settings()

    # For now, create a simple plan based on the query
    # In a full implementation, this would use Lexora to analyze the query
    # and create a sophisticated execution plan

    plan: list[dict[str, Any]] = []
    tasks_to_create: list[TaskCreate] = []
    services_needed: list[ServiceType] = []

    # Basic heuristic routing
    query_lower = request.query.lower()

    if request.enable_rag and ("search" in query_lower or "find" in query_lower):
        services_needed.append(ServiceType.PRISMIND)
        plan.append({
            "step": 1,
            "action": "Search for relevant context",
            "service": ServiceType.PRISMIND,
        })
        tasks_to_create.append(
            TaskCreate(
                name="RAG Search",
                description=f"Search for: {request.query}",
                service=ServiceType.PRISMIND,
                payload={"query": request.query, "n": 5},
                priority=3,
            )
        )

    if "compress" in query_lower or "summarize" in query_lower or len(request.context) > 1000:
        services_needed.append(ServiceType.COGNILENS)
        plan.append({
            "step": len(plan) + 1,
            "action": "Compress/summarize context",
            "service": ServiceType.COGNILENS,
        })
        tasks_to_create.append(
            TaskCreate(
                name="Context Compression",
                description="Compress context for processing",
                service=ServiceType.COGNILENS,
                payload={
                    "text": request.context,
                    "max_tokens": request.max_tokens,
                },
                priority=3,
            )
        )

    # Always add a final LLM processing step
    services_needed.append(ServiceType.LEXORA)
    plan.append({
        "step": len(plan) + 1,
        "action": "Generate response",
        "service": ServiceType.LEXORA,
    })

    # Add dependencies based on plan order
    dep_ids: list[str] = []
    for task in tasks_to_create:
        task.dependencies = dep_ids.copy()

    tasks_to_create.append(
        TaskCreate(
            name="LLM Processing",
            description=f"Process query: {request.query}",
            service=ServiceType.LEXORA,
            payload={
                "prompt": request.query,
                "context": request.context,
                "max_tokens": request.max_tokens,
            },
            priority=5,
            dependencies=dep_ids,
        )
    )

    # Register tasks
    task_ids = await queue.register(tasks_to_create)

    logger.info(
        "Orchestration complete",
        task_count=len(task_ids),
        services=services_needed,
    )

    return OrchestrateResponse(
        task_ids=task_ids,
        plan=plan,
        estimated_services=list(set(services_needed)),
    )


@router.post("/route", response_model=RouteResponse)
async def route(request: RouteRequest) -> RouteResponse:
    """Determine the best service for a query.

    Args:
        request: Routing request.

    Returns:
        Routing decision with confidence.
    """
    # Simple rule-based routing
    # In a full implementation, this would use Lexora for intent classification
    query_lower = request.query.lower()

    alternatives: list[dict[str, Any]] = []

    # Heuristics for service selection
    if any(kw in query_lower for kw in ["search", "find", "lookup", "retrieve"]):
        service = ServiceType.PRISMIND
        confidence = 0.85
        reasoning = "Query appears to be a search/retrieval task"
        alternatives = [
            {"service": ServiceType.LEXORA, "score": 0.15},
        ]
    elif any(kw in query_lower for kw in ["compress", "summarize", "shorten", "condense"]):
        service = ServiceType.COGNILENS
        confidence = 0.90
        reasoning = "Query requests text compression or summarization"
        alternatives = [
            {"service": ServiceType.LEXORA, "score": 0.10},
        ]
    elif any(kw in query_lower for kw in ["unreal", "blueprint", "actor", "component"]):
        service = ServiceType.UNREALWISE
        confidence = 0.85
        reasoning = "Query relates to Unreal Engine operations"
        alternatives = [
            {"service": ServiceType.LEXORA, "score": 0.15},
        ]
    else:
        service = ServiceType.LEXORA
        confidence = 0.70
        reasoning = "Default routing to LLM for general queries"
        alternatives = [
            {"service": ServiceType.PRISMIND, "score": 0.20},
            {"service": ServiceType.COGNILENS, "score": 0.10},
        ]

    logger.info(
        "Routing decision",
        service=service,
        confidence=confidence,
    )

    return RouteResponse(
        service=service,
        confidence=confidence,
        reasoning=reasoning,
        alternatives=alternatives,
    )


# === Health & Stats Endpoints ===


@router.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    """Check health of all services."""
    settings = get_settings()
    services: list[ServiceHealth] = []
    overall_healthy = True

    # Check each service
    service_configs = [
        ("lexora", settings.lexora_url, settings.lexora_timeout, LexoraAdapter),
        ("cognilens", settings.cognilens_url, settings.cognilens_timeout, CognilensAdapter),
        ("prismind", settings.prismind_url, settings.prismind_timeout, PrismindAdapter),
    ]

    for name, url, timeout, adapter_class in service_configs:
        try:
            start = time.time()
            async with adapter_class(url, timeout) as adapter:
                healthy = await adapter.health_check()
            latency = (time.time() - start) * 1000

            services.append(
                ServiceHealth(
                    name=name,
                    status="healthy" if healthy else "unhealthy",
                    latency_ms=latency,
                )
            )

            if not healthy:
                overall_healthy = False

        except Exception as e:
            services.append(
                ServiceHealth(
                    name=name,
                    status="unhealthy",
                    error=str(e),
                )
            )
            overall_healthy = False

    uptime = time.time() - _start_time

    return HealthResponse(
        status="healthy" if overall_healthy else "degraded",
        version="0.1.0",
        uptime_seconds=uptime,
        services=services,
    )


@router.get("/stats", response_model=StatsResponse)
async def get_stats() -> StatsResponse:
    """Get system statistics."""
    queue = get_task_queue()
    stats = await queue.get_stats()

    return StatsResponse(
        total_tasks=stats["total_tasks"],
        tasks_by_status=stats["tasks_by_status"],
        tasks_by_service=stats["tasks_by_service"],
        avg_completion_time_ms=stats["avg_completion_time_ms"],
        queue_depth=stats["queue_depth"],
        active_tasks=stats["active_tasks"],
    )
