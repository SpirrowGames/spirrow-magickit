"""Adapter for Cognilens compression/summarization MCP service."""

import json
from typing import Any

from magickit.adapters.mcp_base import MCPBaseAdapter
from magickit.utils.logging import get_logger

logger = get_logger(__name__)

# Which key each Cognilens tool puts its prose under. Cognilens returns a
# dict per tool and every tool names its payload differently -- there is no
# common `result` / `text` key, and never has been.
#
# This table exists because the previous implementation guessed
# (`data.get("result", data.get("text", str(data)))`) and, since neither key
# is ever present, fell through to `str(data)` on *every* call. Callers were
# therefore storing the Python repr of the whole dict -- e.g.
# ``"{'summary': '...', 'original_tokens': 3521}"`` -- as if it were the
# summary, into Prismind session context (mcp/tools/session.py) and into
# Drive documents (mcp/tools/task.py).
#
# Keep this table next to the methods that use it: a Cognilens release that
# renames a key must break loudly here rather than silently downstream.
_RESULT_KEYS = {
    "summarize": "summary",
    "compress_context": "compressed_context",
    "unify_summaries": "unified_summary",
    "summarize_diff": "diff_summary",
    "progressive_compress": "final_text",
    "extract_essence": "essence",
}

# Compound target for `progressive_compress` when the caller passes a stage
# *count* instead of stage configs. Each stage's ratio is relative to the
# previous stage's output, so the per-stage ratio is the Nth root of this.
# 0.3 matches Cognilens's own `compression.default_ratio`.
_PROGRESSIVE_COMPOUND_RATIO = 0.3


class CognilensError(RuntimeError):
    """Cognilens rejected a tool call, or answered in an unusable shape.

    Raised instead of returning something string-shaped. The whole class of
    bug this adapter used to have was "a dict silently became prose", so
    there is deliberately no path here that stringifies a payload and hands
    it back as content.

    Attributes:
        error_type: The upstream envelope's ``error_type`` when the failure
            came from Cognilens rejecting the call, else a ``Cognilens*``
            label. Matches the Spirrow Platform error envelope convention.
        tool: The MCP tool that was called.
        details: Whatever context the envelope carried, or diagnostics about
            the shape we got (e.g. the keys that *were* present).
    """

    def __init__(
        self,
        message: str,
        *,
        tool: str,
        error_type: str = "CognilensError",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.tool = tool
        self.details = details or {}


class CognilensAdapter(MCPBaseAdapter):
    """Adapter for Cognilens MCP service.

    Provides methods for text compression, summarization, and context
    optimization via MCP tool calls.

    Note:
        ``MCPBaseAdapter.__getattr__`` turns any unknown attribute into an MCP
        tool call, so ``await adapter.close()`` does not close anything -- it
        fires a bogus ``close`` tool over the wire. This adapter opens a
        session per call and needs no teardown.
    """

    async def health_check(self) -> bool:
        """Check if Cognilens service is healthy.

        Returns:
            True if the service is healthy, False otherwise.
        """
        try:
            tools = await self.list_tools()
            # Check if expected tools are available
            expected = {"summarize", "compress_context", "extract_essence"}
            return expected.issubset(set(tools))
        except Exception as e:
            logger.warning("Cognilens health check failed", error=str(e))
            return False

    async def compress(
        self,
        text: str,
        ratio: float = 0.5,
        preserve: list[str] | None = None,
    ) -> str:
        """Compress text while preserving key information.

        Uses compress_context tool with calculated target tokens.

        Args:
            text: Text to compress.
            ratio: Target compression ratio (0.0-1.0).
            preserve: Keywords or concepts to preserve (used in task_description).

        Returns:
            Compressed text.

        Raises:
            CognilensError: If Cognilens rejected the call or answered without
                a ``compressed_context``.
        """
        # Estimate tokens (rough: 4 chars per token)
        estimated_tokens = len(text) // 4
        target_tokens = int(estimated_tokens * ratio)

        task_desc = "Compress text while preserving key information"
        if preserve:
            task_desc += f". Preserve: {', '.join(preserve)}"

        arguments = {
            "full_context": text,
            "task_description": task_desc,
            "target_tokens": target_tokens,
        }

        logger.info(
            "Compressing text via MCP",
            input_length=len(text),
            target_ratio=ratio,
        )

        return await self._call_for_text("compress_context", arguments)

    async def summarize(
        self,
        text: str,
        style: str = "concise",
        max_tokens: int = 500,
        preserve: list[str] | None = None,
    ) -> str:
        """Summarize text with specified style.

        Args:
            text: Text to summarize.
            style: Summary style ('concise', 'detailed', 'bullet').
            max_tokens: Maximum tokens for summary.
            preserve: Elements the summary must keep.

        Returns:
            Summarized text.

        Raises:
            CognilensError: If Cognilens rejected the call or answered without
                a ``summary``.
        """
        payload = await self.summarize_payload(
            text, style=style, max_tokens=max_tokens, preserve=preserve
        )
        return self._require_text(payload, "summary", tool="summarize")

    async def summarize_payload(
        self,
        text: str,
        *,
        style: str = "concise",
        max_tokens: int = 500,
        preserve: list[str] | None = None,
    ) -> dict[str, Any]:
        """Summarize text and return the whole Cognilens payload.

        ``summarize()`` is a thin wrapper over this. The extra fields --
        ``original_tokens`` / ``compressed_tokens`` / ``compression_ratio`` /
        ``savings_percent`` / ``quality_score`` -- are provenance that callers
        record alongside the summary; forcing them through the ``-> str`` API
        would mean either summarizing twice or re-parsing the same response.

        Note:
            ``quality_score`` is not a quality measure. Cognilens computes it
            as ``preservation_ratio * 0.6 + ratio_score * 0.4``, and with an
            empty ``preserve`` the preservation term is unconditionally 1.0 --
            so it reduces to a function of output length alone. Record it;
            never gate on it.

        Args:
            text: Text to summarize.
            style: Summary style ('concise', 'detailed', 'bullet').
            max_tokens: Maximum tokens for summary.
            preserve: Elements the summary must keep.

        Returns:
            The tool's payload, guaranteed to yield a ``summary``.

        Raises:
            CognilensError: If Cognilens rejected the call or answered without
                a ``summary``.
        """
        arguments: dict[str, Any] = {
            "text": text,
            "style": style,
            "max_tokens": max_tokens,
        }
        if preserve:
            arguments["preserve"] = preserve

        logger.info(
            "Summarizing text via MCP",
            input_length=len(text),
            style=style,
            max_tokens=max_tokens,
        )

        # Validate the key here so a caller that only wants the payload still
        # fails at the call, not later where the missing key is anonymous.
        payload = await self._call_for_payload("summarize", arguments)
        return self._require_payload(payload, "summary", tool="summarize")

    async def extract_essence(
        self,
        document: str,
        focus_areas: list[str] | None = None,
    ) -> dict[str, Any]:
        """Extract essential information from a document.

        Args:
            document: Document to analyze.
            focus_areas: Areas to focus on (e.g., ['API changes', 'breaking changes']).

        Returns:
            The tool's payload, guaranteed to yield an ``essence`` (alongside
            ``original_tokens`` / ``compressed_tokens`` / ``focus_areas``).

        Raises:
            CognilensError: If Cognilens rejected the call or answered without
                an ``essence``.
        """
        arguments: dict[str, Any] = {
            "document": document,
        }
        if focus_areas:
            arguments["focus_areas"] = focus_areas

        logger.info(
            "Extracting essence via MCP",
            document_length=len(document),
            focus_areas=focus_areas,
        )

        payload = await self._call_for_payload("extract_essence", arguments)
        return self._require_payload(payload, "essence", tool="extract_essence")

    async def optimize_context(
        self,
        context: str,
        task_description: str,
        target_tokens: int = 500,
    ) -> str:
        """Optimize context for a specific task.

        Args:
            context: Full context to optimize.
            task_description: Description of the task.
            target_tokens: Target token count.

        Returns:
            Optimized context.

        Raises:
            CognilensError: If Cognilens rejected the call or answered without
                a ``compressed_context``.
        """
        arguments = {
            "full_context": context,
            "task_description": task_description,
            "target_tokens": target_tokens,
        }

        logger.info(
            "Optimizing context via MCP",
            context_length=len(context),
            target_tokens=target_tokens,
        )

        return await self._call_for_text("compress_context", arguments)

    async def unify_summaries(
        self,
        documents: list[dict[str, str]],
        purpose: str = "",
    ) -> str:
        """Unify multiple documents into a single coherent summary.

        Args:
            documents: List of document dicts with "title" and "content" keys.
            purpose: Purpose of the unified summary.

        Returns:
            Unified summary.

        Raises:
            CognilensError: If Cognilens rejected the call or answered without
                a ``unified_summary``.
        """
        arguments: dict[str, Any] = {
            "documents": documents,
        }
        if purpose:
            arguments["purpose"] = purpose

        logger.info(
            "Unifying summaries via MCP",
            document_count=len(documents),
        )

        return await self._call_for_text("unify_summaries", arguments)

    async def summarize_diff(
        self,
        before: str,
        after: str,
        focus: str = "",
    ) -> str:
        """Summarize differences between two versions of text.

        Args:
            before: Original text.
            after: Modified text.
            focus: What to focus on in the diff.

        Returns:
            Summary of differences.

        Raises:
            CognilensError: If Cognilens rejected the call or answered without
                a ``diff_summary``.
        """
        arguments: dict[str, Any] = {
            "before": before,
            "after": after,
        }
        if focus:
            arguments["focus"] = focus

        logger.info(
            "Summarizing diff via MCP",
            before_length=len(before),
            after_length=len(after),
        )

        return await self._call_for_text("summarize_diff", arguments)

    async def progressive_compress(
        self,
        text: str,
        stages: int | list[dict[str, Any]] = 3,
    ) -> str:
        """Apply progressive compression through multiple stages.

        Args:
            text: Text to compress.
            stages: Either a stage count, or explicit stage configs
                (``{"target_ratio": float, "preserve": list[str]}``).

        Returns:
            Progressively compressed text.

        Raises:
            CognilensError: If Cognilens rejected the call or answered without
                a ``final_text``.
        """
        stage_configs = self._stage_configs(stages)
        arguments: dict[str, Any] = {
            "text": text,
            "stages": stage_configs,
        }

        logger.info(
            "Progressive compression via MCP",
            input_length=len(text),
            stages=len(stage_configs),
        )

        return await self._call_for_text("progressive_compress", arguments)

    @staticmethod
    def _stage_configs(stages: int | list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Normalise a stage count into the stage configs the tool wants.

        The MCP tool's parameter is ``stages: list[dict]`` (each
        ``{"target_ratio": float, "preserve": list[str]}``), but this adapter's
        signature has always defaulted it to the int ``3`` and sent that
        through -- so every call was rejected by FastMCP's validation before
        reaching any model. Accept both shapes rather than break the existing
        signature.

        Ratios compound (each stage compresses the previous stage's output),
        so the per-stage ratio is the Nth root of the intended overall ratio.
        """
        if isinstance(stages, int):
            count = max(1, stages)
            per_stage = _PROGRESSIVE_COMPOUND_RATIO ** (1 / count)
            return [{"target_ratio": round(per_stage, 4)} for _ in range(count)]
        return stages

    async def _call_for_text(self, tool: str, arguments: dict[str, Any]) -> str:
        """Call a tool and return the prose it is contracted to produce."""
        payload = await self._call_for_payload(tool, arguments)
        return self._require_text(payload, _RESULT_KEYS[tool], tool=tool)

    async def _call_for_payload(self, tool: str, arguments: dict[str, Any]) -> Any:
        """Call a tool and decode its response, raising on any failure.

        Raises:
            CognilensError: On transport failure, on an upstream rejection
                envelope, or on an undecodable response.
        """
        success, result = await self._call_tool_safe(tool, arguments)
        if not success:
            # _call_tool_safe swallowed a transport-level exception and gave
            # us its string. That is a failure, not content.
            raise CognilensError(
                f"{tool} call failed: {result}",
                tool=tool,
                error_type="CognilensTransportError",
            )
        return self._decode(result, tool=tool)

    def _decode(self, result: Any, *, tool: str) -> Any:
        """Decode one raw tool result into a payload.

        Three shapes reach here:

        * ``dict`` -- only ever ``MCPBaseAdapter.call_tool``'s
          ``UpstreamValidationError`` envelope. A successful call returns text,
          never a dict, so a dict here always means rejection.
        * ``str`` -- the tool's JSON payload, or (unexpectedly) bare prose.
        * ``None`` -- empty content.

        Raises:
            CognilensError: On the upstream envelope or on empty content.
        """
        if result is None:
            raise CognilensError(
                f"{tool} returned no content",
                tool=tool,
                error_type="CognilensEmptyResponse",
            )

        if isinstance(result, dict):
            if "error_type" in result:
                raise CognilensError(
                    str(result.get("error") or f"{tool} was rejected upstream"),
                    tool=tool,
                    error_type=str(result["error_type"]),
                    details=dict(result.get("details") or {}),
                )
            return result

        if isinstance(result, str):
            try:
                return json.loads(result)
            except json.JSONDecodeError:
                # Bare prose. Not the documented shape, but it *is* text, so
                # pass it on rather than fail a call that produced content.
                return result

        return result

    def _require_payload(self, payload: Any, key: str, *, tool: str) -> dict[str, Any]:
        """Validate ``key`` and hand back the payload as a dict.

        For the methods whose contract is "the whole payload" rather than "the
        prose". A bare-prose response (see ``_decode``) is normalised to
        ``{key: text}`` so those callers can keep a ``dict`` signature instead
        of branching on a shape they never asked about.

        Raises:
            CognilensError: If the payload has no usable ``key``.
        """
        text = self._require_text(payload, key, tool=tool)
        if isinstance(payload, dict):
            return payload
        return {key: text}

    def _require_text(self, payload: Any, key: str, *, tool: str) -> str:
        """Pull ``key`` out of a decoded payload, or raise.

        Never falls back to ``str(payload)``: a dict that becomes prose is the
        exact bug this method exists to make impossible.

        Raises:
            CognilensError: If the payload has no usable ``key``.
        """
        if isinstance(payload, str):
            # A bare-prose response (see _decode). The whole body is the text.
            return payload

        if isinstance(payload, dict):
            if key in payload:
                value = payload[key]
                if isinstance(value, str):
                    return value
                if value is not None and not isinstance(value, (dict, list)):
                    return str(value)
                raise CognilensError(
                    f"{tool} returned a non-text {key!r}",
                    tool=tool,
                    error_type="CognilensShapeError",
                    details={"key": key, "type": type(value).__name__},
                )
            # Some FastMCP versions wrap a non-ToolResult return in structured
            # content. Look exactly one level down, and only after a genuine
            # top-level `key` has had its chance to win.
            inner = payload.get("result")
            if isinstance(inner, dict) and key in inner:
                return self._require_text(inner, key, tool=tool)
            raise CognilensError(
                f"{tool} returned no {key!r}",
                tool=tool,
                error_type="CognilensShapeError",
                details={"key": key, "keys": sorted(str(k) for k in payload)},
            )

        raise CognilensError(
            f"{tool} returned {type(payload).__name__}, expected a payload with {key!r}",
            tool=tool,
            error_type="CognilensShapeError",
            details={"key": key, "type": type(payload).__name__},
        )
