# Spirrow-Magickit

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

**Orchestration layer for the Spirrow Platform** - A conductor that coordinates multiple MCP servers with intelligent routing and optimization.

[日本語版 README](README.ja.md)

## Overview

Magickit is the orchestration hub of the Spirrow Platform. It integrates multiple specialized services (Lexora, Prismind, Cognilens) into unified workflows, handling:

- **Task Management** - Queue management with priority and dependency resolution
- **Context Optimization** - Intelligent context compression and RAG-enhanced retrieval
- **Project Lifecycle** - Full lifecycle support from setup to archive

**Philosophy: "The Conductor - Never plays, only directs."** Magickit delegates to specialized services rather than implementing functionality itself.

## Architecture

```
Claude Code / MCP Client
        │
        ▼
    Magickit (:8114 MCP Streamable HTTP / :8113 FastAPI)
        │
   ┌────┼────┬────┐
   ▼    ▼    ▼    ▼
Lexora Cognilens Prismind UnrealWise
(:8110)  (:8111)  (:8112)   (:8115)
```

## Quick Start

### Prerequisites

- Python 3.11+
- Running instances of [Lexora](https://github.com/spirrowgames/spirrow-lexora), [Prismind](https://github.com/spirrowgames/spirrow-prismind), [Cognilens](https://github.com/spirrowgames/spirrow-cognilens)

### Installation

```bash
# Clone the repository
git clone https://github.com/spirrowgames/spirrow-magickit.git
cd spirrow-magickit

# Create virtual environment
python -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -e ".[dev]"

# Configure services (optional - defaults work for local setup)
export MAGICKIT_LEXORA_URL=http://localhost:8110
export MAGICKIT_COGNILENS_URL=http://localhost:8111
export MAGICKIT_PRISMIND_URL=http://localhost:8112
```

### Running

```bash
# As MCP server (recommended)
python -m magickit.mcp_server

# As REST API server
uvicorn magickit.main:app --port 8113
```

### Claude Code Integration

Add to your `~/.claude/mcp.json`:

```json
{
  "mcpServers": {
    "spirrow-magickit": {
      "type": "http",
      "url": "http://localhost:8114/mcp"
    }
  }
}
```

## Features

### MCP Tools

Magickit exposes high-level MCP tools that combine multiple services:

| Tool | Description |
|------|-------------|
| `service_health` | Check health of all services (incl. github-mcp) |
| `research_and_summarize` | Prismind search + Cognilens compression |
| `analyze_documents` | Document search + essence extraction |
| `generate_with_context` | RAG-enhanced content generation |
| `intelligent_route` | Task analysis and service recommendation |
| `orchestrate_workflow` | Multi-service workflow execution |
| `github` / `github_operations` | Passthrough dispatcher for github-mcp (identity split + merge-to-main guard; see below) |

### GitHub Integration (identity split + merge guard)

`github` / `github_operations` proxy the github-mcp container (`127.0.0.1:8116`,
toolsets `repos,issues,pull_requests`), collapsing its 35 tools into **two** so the
context cost stays low under connectors that freeze the tool list at connect time.

- **Identity routing**: review-submit operations (`pull_request_review_write`,
  `add_comment_to_pending_review`) are forwarded with the *reviewer* PAT
  (`GITHUB_MCP_PAT_REVIEWER`, Contents read-only); everything else (commit, push,
  PR creation, merge, reads) uses the *implementer* PAT (`GITHUB_MCP_PAT_IMPLEMENTER`).
  Both fall back to the legacy single `GITHUB_MCP_PAT`, so single-PAT setups are
  unchanged. This keeps a review event off the account that opened the PR (avoiding
  the self-review 422). Both PATs share one process env — an operation-level split,
  not process/file isolation.
- **Merge guard (merge to main = human GO)**: `merge_pull_request` carries only the
  PR number, so before forwarding, the dispatcher looks up the PR's base branch via
  `pull_request_read(get)` and refuses if it targets a protected branch (default
  `main`; `GITHUB_PROTECTED_BASE_BRANCHES`, comma-separated). Merges into other
  branches (e.g. develop) pass; an undeterminable base fails closed. Needed because
  this plan has no branch protection and per-tool permissions can't gate by operation.
- Secret placement (`/etc/spirrow-magickit/github.env`) is covered in CLAUDE.md.

### Session Management

| Tool | Description |
|------|-------------|
| `begin_task` / `resume` | Restore session context (optional `author` for role-partitioned context) |
| `checkpoint` | Save intermediate progress (optional `author`) |
| `handoff` | End session with handoff notes (optional `author`) |
| `list_context_authors` | List context authors/roles saved for a project |

### Project Lifecycle

| Tool | Description |
|------|-------------|
| `init_project` | Initialize project from template |
| `advance_phase` | Phase transitions with quality gates |
| `add_milestone` | Milestone tracking |
| `get_burndown` / `estimate_completion` | Progress tracking |
| `generate_status_report` | Stakeholder reports |

### Task Management

| Tool | Description |
|------|-------------|
| `add_task` | Add task with auto-ID generation, duplicate detection |
| `list_tasks` | List tasks with smart sorting and recommendations |
| `get_task` | Get single task with optional related knowledge |
| `update_task` | Update any task field, supports phase move |
| `delete_task` | Delete task with dependency cleanup |
| `start_task` | Start task with dependency validation |
| `complete_task` | Complete task with learnings recording |
| `block_task` | Block task with impact analysis |
| `move_task_to_phase` | Shortcut for phase move |
| `set_task_priority` | Shortcut for priority update |
| `set_task_blockers` | Shortcut for dependency update |

### Smart Document Creation

Automatically matches document types using RAG-based semantic search (BGE-M3 embeddings) with multilingual support:

```python
smart_create_document(
    name="2024-01-15 Sprint Planning",
    doc_type="議事録",  # Japanese → matches "meeting_minutes"
    content="...",
    phase_task="phase1-task2"
)
```

### Document Maintenance

Tools for document and knowledge cleanup, orphan detection, and consistency checking:

| Tool | Description |
|------|-------------|
| `smart_delete_document` | Delete document with related knowledge cleanup |
| `detect_orphan_documents` | Find orphan documents (deleted projects, invalid refs) |
| `detect_orphan_knowledge` | Find orphan knowledge entries |
| `detect_unused_document_types` | Find unused/duplicate document types |
| `check_document_consistency` | Comprehensive health check |
| `cleanup_documents` | Batch cleanup with dry_run/confirm safety |

```python
# Preview what would be deleted
check_document_consistency(project="my-project")
# -> {"summary": {"orphan_documents": 3, "orphan_knowledge": 5, ...}}

# Clean up with safety checks
cleanup_documents(
    cleanup_orphan_documents=True,
    dry_run=True  # Preview first
)
```

### Orchestration Workflow

Execute multi-step workflows with dependency management:

```python
steps = [
    {"service": "prismind", "action": "search",
     "params": {"query": "AI best practices"}, "output_key": "results"},
    {"service": "cognilens", "action": "compress",
     "params": {"text": "${results}", "max_tokens": 500},
     "depends_on": [0], "output_key": "compressed"},
    {"service": "lexora", "action": "generate",
     "params": {"prompt": "Summarize: ${compressed}"},
     "depends_on": [1]}
]
orchestrate_workflow(steps=steps)
```

## Multi-User Support

Magickit supports multiple concurrent users with automatic identification:

1. `SPIRROW_USER` environment variable (highest priority)
2. `git config user.email`
3. OS username (fallback)

All tools accept an optional `user` parameter for explicit identification.

## Configuration

Environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `MAGICKIT_LEXORA_URL` | `http://localhost:8110` | Lexora service URL |
| `MAGICKIT_COGNILENS_URL` | `http://localhost:8111` | Cognilens service URL |
| `MAGICKIT_PRISMIND_URL` | `http://localhost:8112` | Prismind service URL |
| `MAGICKIT_MCP_PORT` | `8114` | MCP Streamable HTTP server port |
| `MAGICKIT_PORT` | `8113` | FastAPI HTTP API server port |
| `MAGICKIT_TRANSPORT_MODE` | `http` | MCP transport: `http` (Streamable HTTP) or `sse` (legacy) |
| `MAGICKIT_AUTH_DISABLED` | `0` | Set to `1` to bypass Google OAuth (local-only deployments) |

Or use `config/magickit_config.yaml` for file-based configuration.

## Project Structure

```
src/magickit/
├── main.py              # FastAPI app
├── mcp_server.py        # MCP server entry point
├── config.py            # Pydantic Settings
├── mcp/tools/           # MCP tool implementations
│   ├── health.py        # Health checks
│   ├── research.py      # Knowledge search
│   ├── orchestration.py # Workflow execution
│   ├── session.py       # Session management
│   ├── project.py       # Project lifecycle
│   └── ...
├── adapters/            # Service adapters
│   ├── lexora.py        # LLM calls
│   ├── cognilens.py     # Text compression
│   └── prismind.py      # RAG search
└── core/                # Core components
    ├── task_queue.py
    └── dependency_graph.py
```

## Related Services

| Service | Port | Description |
|---------|------|-------------|
| [Lexora](https://github.com/spirrowgames/spirrow-lexora) | 8110 | Local LLM gateway (Qwen2.5, etc.) |
| [Prismind](https://github.com/spirrowgames/spirrow-prismind) | 8112 | Knowledge management & RAG search |
| [Cognilens](https://github.com/spirrowgames/spirrow-cognilens) | 8111 | Text compression & summarization |

## Tech Stack

- **Python 3.11+**
- **FastMCP** - MCP server framework
- **FastAPI** - REST API
- **httpx** - Async HTTP client
- **Pydantic v2** - Settings & validation

## Testing

```bash
# Run all tests
pytest tests/

# With coverage
pytest tests/ --cov=magickit --cov-report=html
```

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## License

[MIT License](LICENSE)

## Acknowledgments

Part of the **Spirrow Platform** - an AI-powered development toolkit.
