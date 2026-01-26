"""MCP Tools for Magickit.

This package contains tool implementations for the Magickit MCP server:
- health: Service health monitoring
- research: Knowledge search and summarization (Prismind + Cognilens)
- orchestration: Intelligent routing and workflow orchestration
- generation: RAG-enhanced content generation (all services)
- session: Session management for cross-session context persistence
"""

from magickit.mcp.tools import health, research, orchestration, generation, session

__all__ = ["health", "research", "orchestration", "generation", "session"]
