"""Orchestration tools for Magickit MCP server.

Provides intelligent routing and multi-step workflow execution across services.
"""

from __future__ import annotations

import asyncio
from enum import Enum
from typing import Any

from fastmcp import FastMCP

from magickit.adapters.cognilens import CognilensAdapter
from magickit.adapters.prismind import PrismindAdapter
from magickit.adapters.lexora import LexoraAdapter
from magickit.config import Settings
from magickit.utils.logging import get_logger

logger = get_logger(__name__)

# Module-level settings reference
_settings: Settings | None = None


class ServiceType(str, Enum):
    """Available service types."""

    PRISMIND = "prismind"
    COGNILENS = "cognilens"
    LEXORA = "lexora"
    MAGICKIT = "magickit"


class ActionType(str, Enum):
    """Available action types."""

    SEARCH = "search"
    COMPRESS = "compress"
    SUMMARIZE = "summarize"
    GENERATE = "generate"
    ANALYZE = "analyze"
    STORE = "store"
    ROUTE = "route"


def register_tools(mcp: FastMCP, settings: Settings) -> None:
    """Register orchestration tools with the MCP server.

    Args:
        mcp: FastMCP server instance.
        settings: Application settings.
    """
    global _settings
    _settings = settings

    @mcp.tool()
    async def intelligent_route(
        request: str,
        context: str = "",
        available_services: list[str] | None = None,
    ) -> dict[str, Any]:
        """Analyze a request and recommend the optimal service(s) and approach.

        USE THIS WHEN: you're uncertain which Spirrow service to use, or need
        guidance on the best approach for a complex task. This tool:
        - Analyzes the request intent
        - Considers available services and their capabilities
        - Recommends a routing strategy with rationale

        DO NOT USE WHEN:
        - You already know which service to use
        - The task is straightforward (e.g., simple search → just use search)
        - You need to execute the task → use the recommended service directly

        Args:
            request: The user's request or task description.
            context: Optional additional context about the situation.
            available_services: Optional list of services to consider
                               (default: all services).

        Returns:
            Dict containing:
            - recommended_service: Primary service to use
            - recommended_action: Suggested action type
            - workflow: For complex tasks, a suggested multi-step workflow
            - rationale: Explanation of the recommendation
            - alternatives: Other viable approaches
        """
        if _settings is None:
            raise RuntimeError("Settings not initialized")

        # Default to all services
        if available_services is None:
            available_services = ["prismind", "cognilens", "lexora", "magickit"]

        # Analyze request keywords and patterns
        request_lower = request.lower()

        # Routing logic based on keywords and patterns
        recommendations = _analyze_request(request_lower, context, available_services)

        logger.info(
            "Routing analysis complete",
            request_preview=request[:50],
            recommended=recommendations["recommended_service"],
        )

        return recommendations

    @mcp.tool()
    async def orchestrate_workflow(
        steps: list[dict[str, Any]],
        parallel_groups: list[list[int]] | None = None,
        stop_on_error: bool = True,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute a multi-step workflow across Spirrow services with dependency management.

        USE THIS WHEN: you need to execute a sequence of operations that depend on
        each other's outputs, or run multiple independent operations in parallel.
        This tool:
        - Manages step dependencies automatically
        - Supports parallel execution of independent steps
        - Passes outputs between steps as context
        - Handles errors gracefully

        DO NOT USE WHEN:
        - You have a single operation → call the service directly
        - You can manually chain 2-3 simple operations
        - Steps don't have dependencies → just call them in parallel yourself

        Args:
            steps: List of workflow steps. Each step should contain:
                - service: "prismind", "cognilens", "lexora", or "magickit"
                - action: Action to perform (e.g., "search", "compress", "generate")
                - params: Parameters for the action
                - depends_on: Optional list of step indices this step depends on
                - output_key: Optional key to store this step's output for later steps

            parallel_groups: Optional grouping for parallel execution.
                            Each inner list contains step indices to run in parallel.

            stop_on_error: Whether to stop workflow on first error (default: True).

            context: Optional initial context available to all steps.
                    Use ${output_key} in params to reference previous step outputs.

        Returns:
            Dict containing:
            - status: "completed", "partial", or "failed"
            - results: Results from each step
            - outputs: Named outputs (from output_key) for use in subsequent processing
            - errors: Any errors encountered
            - execution_time_ms: Total execution time

        Example:
            steps = [
                {"service": "prismind", "action": "search",
                 "params": {"query": "AI best practices"}, "output_key": "search_results"},
                {"service": "cognilens", "action": "compress",
                 "params": {"text": "${search_results}", "max_tokens": 500},
                 "depends_on": [0], "output_key": "compressed"},
                {"service": "lexora", "action": "generate",
                 "params": {"prompt": "Based on: ${compressed}\n\nWrite a summary."},
                 "depends_on": [1]}
            ]
        """
        if _settings is None:
            raise RuntimeError("Settings not initialized")

        start_time = asyncio.get_event_loop().time()

        # Initialize tracking
        results: list[dict[str, Any]] = [{"status": "pending"} for _ in steps]
        outputs: dict[str, Any] = context.copy() if context else {}
        errors: list[dict[str, Any]] = []

        # Build dependency graph
        execution_order = _build_execution_order(steps, parallel_groups)

        logger.info(
            "Starting workflow",
            total_steps=len(steps),
            execution_order=execution_order,
        )

        # Execute steps
        for batch in execution_order:
            if len(batch) == 1:
                # Single step
                idx = batch[0]
                result = await _execute_step(
                    steps[idx], idx, outputs, _settings
                )
                results[idx] = result

                if result["status"] == "error":
                    errors.append({"step": idx, "error": result.get("error", "Unknown error")})
                    if stop_on_error:
                        break
                elif result.get("output_key"):
                    outputs[result["output_key"]] = result.get("output")
            else:
                # Parallel batch
                tasks = [
                    _execute_step(steps[idx], idx, outputs, _settings)
                    for idx in batch
                ]
                batch_results = await asyncio.gather(*tasks, return_exceptions=True)

                for idx, result in zip(batch, batch_results):
                    if isinstance(result, Exception):
                        results[idx] = {"status": "error", "error": str(result)}
                        errors.append({"step": idx, "error": str(result)})
                        if stop_on_error:
                            break
                    else:
                        results[idx] = result
                        if result["status"] == "error":
                            errors.append({"step": idx, "error": result.get("error")})
                            if stop_on_error:
                                break
                        elif result.get("output_key"):
                            outputs[result["output_key"]] = result.get("output")

                if stop_on_error and errors:
                    break

        # Determine overall status
        completed_count = sum(1 for r in results if r["status"] == "completed")
        if completed_count == len(steps):
            status = "completed"
        elif completed_count > 0:
            status = "partial"
        else:
            status = "failed"

        elapsed = asyncio.get_event_loop().time() - start_time

        return {
            "status": status,
            "results": results,
            "outputs": outputs,
            "errors": errors,
            "execution_time_ms": round(elapsed * 1000, 2),
        }


def _analyze_request(
    request: str,
    context: str,
    available_services: list[str],
) -> dict[str, Any]:
    """Analyze a request and determine routing recommendations."""

    # Keyword patterns for each service/action
    search_keywords = ["search", "find", "look for", "query", "retrieve", "knowledge"]
    compress_keywords = ["compress", "shorten", "reduce", "condense", "fit", "token"]
    summarize_keywords = ["summarize", "summary", "tldr", "brief", "overview"]
    generate_keywords = ["generate", "create", "write", "compose", "draft"]
    analyze_keywords = ["analyze", "extract", "understand", "parse", "essence"]
    store_keywords = ["store", "save", "add", "index", "remember"]

    # Calculate keyword matches
    scores = {
        "prismind": sum(1 for k in search_keywords + store_keywords if k in request),
        "cognilens": sum(1 for k in compress_keywords + summarize_keywords + analyze_keywords if k in request),
        "lexora": sum(1 for k in generate_keywords if k in request),
    }

    # Filter by available services
    scores = {k: v for k, v in scores.items() if k in available_services}

    # Determine action type
    action = ActionType.SEARCH
    if any(k in request for k in compress_keywords):
        action = ActionType.COMPRESS
    elif any(k in request for k in summarize_keywords):
        action = ActionType.SUMMARIZE
    elif any(k in request for k in generate_keywords):
        action = ActionType.GENERATE
    elif any(k in request for k in analyze_keywords):
        action = ActionType.ANALYZE
    elif any(k in request for k in store_keywords):
        action = ActionType.STORE

    # Determine recommended service
    if scores:
        recommended = max(scores, key=scores.get)
    else:
        recommended = "prismind"  # Default to knowledge search

    # Build workflow for complex requests
    workflow = None
    if scores.get("prismind", 0) > 0 and scores.get("cognilens", 0) > 0:
        # Combined search + compression
        workflow = [
            {"service": "prismind", "action": "search", "description": "Search knowledge base"},
            {"service": "cognilens", "action": "compress", "description": "Compress results"},
        ]
        recommended = "magickit"
        action = ActionType.ROUTE

    if scores.get("prismind", 0) > 0 and scores.get("lexora", 0) > 0:
        # RAG pattern
        workflow = [
            {"service": "prismind", "action": "search", "description": "Retrieve context"},
            {"service": "lexora", "action": "generate", "description": "Generate with context"},
        ]
        recommended = "magickit"
        action = ActionType.ROUTE

    # Build alternatives
    alternatives = [
        {"service": s, "score": score}
        for s, score in sorted(scores.items(), key=lambda x: -x[1])
        if s != recommended
    ][:2]

    # Build rationale
    if workflow:
        rationale = f"Complex request requiring multiple services. Recommended workflow: {' → '.join(s['service'] for s in workflow)}"
    else:
        rationale = f"Request matches {recommended} capabilities with action '{action.value}'"

    return {
        "recommended_service": recommended,
        "recommended_action": action.value,
        "workflow": workflow,
        "rationale": rationale,
        "alternatives": alternatives,
        "confidence": min(max(scores.values()) / 3, 1.0) if scores else 0.5,
    }


def _build_execution_order(
    steps: list[dict[str, Any]],
    parallel_groups: list[list[int]] | None,
) -> list[list[int]]:
    """Build execution order from steps and parallel groups."""

    if parallel_groups:
        return parallel_groups

    # Build from dependencies
    n = len(steps)
    executed = [False] * n
    order = []

    while not all(executed):
        batch = []
        for i, step in enumerate(steps):
            if executed[i]:
                continue

            deps = step.get("depends_on", [])
            if all(executed[d] for d in deps):
                batch.append(i)

        if not batch:
            # Remaining steps have unresolved dependencies
            remaining = [i for i in range(n) if not executed[i]]
            order.append(remaining)
            break

        order.append(batch)
        for i in batch:
            executed[i] = True

    return order


async def _execute_step(
    step: dict[str, Any],
    step_idx: int,
    outputs: dict[str, Any],
    settings: Settings,
) -> dict[str, Any]:
    """Execute a single workflow step."""

    service = step.get("service", "")
    action = step.get("action", "")
    params = step.get("params", {}).copy()
    output_key = step.get("output_key")

    # Substitute output references in params
    for key, value in params.items():
        if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
            ref_key = value[2:-1]
            if ref_key in outputs:
                params[key] = outputs[ref_key]

    logger.debug(
        "Executing step",
        step=step_idx,
        service=service,
        action=action,
    )

    try:
        result = await _call_service(service, action, params, settings)
        return {
            "status": "completed",
            "output": result,
            "output_key": output_key,
        }
    except Exception as e:
        logger.error("Step execution failed", step=step_idx, error=str(e))
        return {
            "status": "error",
            "error": str(e),
            "output_key": output_key,
        }


async def _call_service(
    service: str,
    action: str,
    params: dict[str, Any],
    settings: Settings,
) -> Any:
    """Call a specific service action."""

    if service == "prismind":
        adapter = PrismindAdapter(
            sse_url=settings.prismind_url,
            timeout=settings.prismind_timeout,
        )

        if action == "search":
            results = await adapter.search_knowledge(
                query=params.get("query", ""),
                category=params.get("category", ""),
                project=params.get("project", ""),
                tags=params.get("tags"),
                limit=params.get("limit", 10),
            )
            return "\n\n".join(r.get("content", "") for r in results)

        elif action == "add" or action == "store":
            return await adapter.add_knowledge(
                content=params.get("content", ""),
                category=params.get("category", ""),
                project=params.get("project", ""),
                tags=params.get("tags"),
                source=params.get("source", ""),
            )

        elif action == "get_document":
            return await adapter.get_document(
                query=params.get("query", ""),
                doc_id=params.get("doc_id", ""),
                doc_type=params.get("doc_type", ""),
            )

    elif service == "cognilens":
        adapter = CognilensAdapter(
            sse_url=settings.cognilens_url,
            timeout=settings.cognilens_timeout,
        )

        if action == "compress":
            return await adapter.compress(
                text=params.get("text", ""),
                ratio=params.get("ratio", 0.5),
                preserve=params.get("preserve"),
            )

        elif action == "summarize":
            return await adapter.summarize(
                text=params.get("text", ""),
                style=params.get("style", "concise"),
                max_tokens=params.get("max_tokens", 500),
            )

        elif action == "extract_essence":
            return await adapter.extract_essence(
                document=params.get("document", ""),
                focus_areas=params.get("focus_areas"),
            )

        elif action == "optimize":
            return await adapter.optimize_context(
                context=params.get("context", ""),
                task_description=params.get("task_description", ""),
                target_tokens=params.get("target_tokens", 500),
            )

    elif service == "lexora":
        adapter = LexoraAdapter(
            base_url=settings.lexora_url,
            timeout=settings.lexora_timeout,
        )

        if action == "generate":
            return await adapter.generate(
                prompt=params.get("prompt", ""),
                max_tokens=params.get("max_tokens", 1000),
                temperature=params.get("temperature", 0.7),
            )

        elif action == "chat":
            return await adapter.chat(
                messages=params.get("messages", []),
                max_tokens=params.get("max_tokens", 1000),
                temperature=params.get("temperature", 0.7),
            )

    raise ValueError(f"Unknown service/action: {service}/{action}")
