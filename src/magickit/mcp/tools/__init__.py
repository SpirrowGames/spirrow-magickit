"""MCP Tools for Magickit.

This package contains tool implementations for the Magickit MCP server:
- health: Service health monitoring
- research: Knowledge search and summarization (Prismind + Cognilens)
- orchestration: Intelligent routing and workflow orchestration
- generation: RAG-enhanced content generation (all services)
- session: Session management for cross-session context persistence
- project: Project management (init, status, clone, archive, restore)
- document: Smart document creation with automatic type handling
- document_maintenance: Document/knowledge cleanup and consistency checking
- task: Task management with dependencies and recommendations
- specification: AI-driven specification with dynamic questions
- execution: Task decomposition and execution pipeline
- lifecycle: Phase and milestone management for project lifecycles
- progress: Progress tracking, burndown charts, and velocity
- quality: Quality gate definitions and checking
- reporting: Status reports, release notes, and performance analysis
"""

from magickit.mcp.tools import (
    health,
    research,
    orchestration,
    generation,
    session,
    project,
    document,
    document_maintenance,
    task,
    specification,
    execution,
    lifecycle,
    progress,
    quality,
    reporting,
)

__all__ = [
    "health",
    "research",
    "orchestration",
    "generation",
    "session",
    "project",
    "document",
    "document_maintenance",
    "task",
    "specification",
    "execution",
    "lifecycle",
    "progress",
    "quality",
    "reporting",
]
