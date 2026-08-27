"""Unit tests for CognilensAdapter (MCP tool-result extraction).

The bug these tests pin: the adapter used to look for a ``result`` / ``text``
key that no Cognilens tool has ever returned, so every method fell through to
``str(data)`` and handed callers the Python repr of the whole payload -- e.g.
``"{'summary': '...', 'original_tokens': 3521}"`` -- as if it were prose. The
same path stringified ``MCPBaseAdapter.call_tool``'s upstream-rejection
envelope, so a rejection became content.

Patching note: patch ``call_tool`` **on the class**. ``MCPBaseAdapter.__getattr__``
fabricates an MCP tool call for any unknown attribute, so patching a misspelled
attribute on an *instance* silently succeeds and the test passes for the wrong
reason.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from magickit.adapters.cognilens import CognilensAdapter, CognilensError

SSE_URL = "http://localhost:8111"


@pytest.fixture
def adapter() -> CognilensAdapter:
    return CognilensAdapter(sse_url=SSE_URL, timeout=5.0)


def _tool_result(payload: dict[str, Any]) -> str:
    """What MCPBaseAdapter.call_tool returns on success: the text content."""
    return json.dumps(payload, ensure_ascii=False)


def _patch_call_tool(return_value: Any) -> Any:
    return patch.object(
        CognilensAdapter, "call_tool", new=AsyncMock(return_value=return_value)
    )


# Every method, the tool it calls, and the key that tool actually answers
# under. Mirrors `_RESULT_KEYS`; kept spelled out here so a rename has to be
# made twice, deliberately.
_METHOD_CASES = [
    ("summarize", {"text": "x" * 50}, "summary"),
    ("compress", {"text": "x" * 50}, "compressed_context"),
    ("optimize_context", {"context": "x" * 50, "task_description": "t"}, "compressed_context"),
    ("unify_summaries", {"documents": [{"title": "a", "content": "b"}]}, "unified_summary"),
    ("summarize_diff", {"before": "a", "after": "b"}, "diff_summary"),
    ("progressive_compress", {"text": "x" * 50}, "final_text"),
]


# ---- the key each tool actually uses ----------------------------------


@pytest.mark.parametrize("method,kwargs,key", _METHOD_CASES)
async def test_each_method_extracts_its_own_key(
    adapter: CognilensAdapter, method: str, kwargs: dict[str, Any], key: str
) -> None:
    payload = {key: "本当の要約テキスト", "original_tokens": 100, "compressed_tokens": 20}
    with _patch_call_tool(_tool_result(payload)):
        out = await getattr(adapter, method)(**kwargs)
    assert out == "本当の要約テキスト"


@pytest.mark.parametrize("method,kwargs,key", _METHOD_CASES)
async def test_no_method_ever_returns_a_stringified_dict(
    adapter: CognilensAdapter, method: str, kwargs: dict[str, Any], key: str
) -> None:
    """The tell of the original bug, stated as an invariant.

    `str({...})` starts with `{'`. If any method ever produces that again,
    this fails regardless of which key moved.
    """
    payload = {key: "要約", "original_tokens": 100, "quality_score": 0.8}
    with _patch_call_tool(_tool_result(payload)):
        out = await getattr(adapter, method)(**kwargs)
    assert not out.startswith("{'")
    assert "original_tokens" not in out


@pytest.mark.parametrize("method,kwargs,key", _METHOD_CASES)
async def test_a_payload_without_the_key_raises_instead_of_stringifying(
    adapter: CognilensAdapter, method: str, kwargs: dict[str, Any], key: str
) -> None:
    with _patch_call_tool(_tool_result({"unexpected": "shape", "original_tokens": 1})):
        with pytest.raises(CognilensError) as exc:
            await getattr(adapter, method)(**kwargs)
    assert exc.value.error_type == "CognilensShapeError"
    # The diagnostics name what *was* there, so the next reader can see the
    # rename rather than guess at it.
    assert exc.value.details["key"] == key
    assert "unexpected" in exc.value.details["keys"]


# ---- upstream rejection is a failure, not content ----------------------


async def test_upstream_rejection_envelope_raises(adapter: CognilensAdapter) -> None:
    envelope = {
        "error_type": "UpstreamValidationError",
        "error": "Input validation error: 'stages' is not of type 'array'",
        "details": {},
    }
    with _patch_call_tool(envelope):
        with pytest.raises(CognilensError) as exc:
            await adapter.summarize("x" * 50)
    assert exc.value.error_type == "UpstreamValidationError"
    assert exc.value.tool == "summarize"
    assert "not of type" in str(exc.value)
    # The rejection text must not arrive wrapped in a dict repr.
    assert not str(exc.value).startswith("{'")


async def test_transport_failure_raises(adapter: CognilensAdapter) -> None:
    """_call_tool_safe swallows transport errors into (False, str). Not content."""
    with patch.object(
        CognilensAdapter, "call_tool", new=AsyncMock(side_effect=RuntimeError("connect refused"))
    ):
        with pytest.raises(CognilensError) as exc:
            await adapter.summarize("x" * 50)
    assert exc.value.error_type == "CognilensTransportError"
    assert "connect refused" in str(exc.value)


async def test_empty_content_raises(adapter: CognilensAdapter) -> None:
    with _patch_call_tool(None):
        with pytest.raises(CognilensError) as exc:
            await adapter.summarize("x" * 50)
    assert exc.value.error_type == "CognilensEmptyResponse"


# ---- summarize / summarize_payload ------------------------------------


async def test_summarize_payload_returns_provenance(adapter: CognilensAdapter) -> None:
    payload = {
        "summary": "要約",
        "original_tokens": 3521,
        "compressed_tokens": 400,
        "compression_ratio": 0.11,
        "savings_percent": 88.6,
        "quality_score": 0.72,
    }
    with _patch_call_tool(_tool_result(payload)):
        out = await adapter.summarize_payload("x" * 50, style="concise", max_tokens=400)
    assert out == payload


async def test_summarize_is_a_wrapper_over_summarize_payload(
    adapter: CognilensAdapter,
) -> None:
    payload = {"summary": "要約", "original_tokens": 10, "compressed_tokens": 2}
    with _patch_call_tool(_tool_result(payload)) as mock:
        assert await adapter.summarize("x" * 50) == "要約"
    assert mock.await_count == 1


async def test_summarize_forwards_style_and_max_tokens(adapter: CognilensAdapter) -> None:
    with _patch_call_tool(_tool_result({"summary": "s"})) as mock:
        await adapter.summarize("body", style="bullet", max_tokens=400)
    name, arguments = mock.await_args.args
    assert name == "summarize"
    assert arguments == {"text": "body", "style": "bullet", "max_tokens": 400}


async def test_summarize_forwards_preserve_when_given(adapter: CognilensAdapter) -> None:
    """`preserve` is a real tool parameter; the adapter used to drop it."""
    with _patch_call_tool(_tool_result({"summary": "s"})) as mock:
        await adapter.summarize("body", preserve=["msg-012"])
    assert mock.await_args.args[1]["preserve"] == ["msg-012"]


async def test_summarize_omits_preserve_when_empty(adapter: CognilensAdapter) -> None:
    with _patch_call_tool(_tool_result({"summary": "s"})) as mock:
        await adapter.summarize("body")
    assert "preserve" not in mock.await_args.args[1]


# ---- extract_essence keeps its dict contract --------------------------


async def test_extract_essence_returns_the_payload_dict(adapter: CognilensAdapter) -> None:
    payload = {"essence": "本質", "original_tokens": 100, "focus_areas": ["api"]}
    with _patch_call_tool(_tool_result(payload)):
        out = await adapter.extract_essence("doc", focus_areas=["api"])
    assert out == payload


async def test_extract_essence_rejects_an_envelope(adapter: CognilensAdapter) -> None:
    with _patch_call_tool({"error_type": "UpstreamValidationError", "error": "bad"}):
        with pytest.raises(CognilensError):
            await adapter.extract_essence("doc")


# ---- progressive_compress wire shape ----------------------------------


async def test_progressive_compress_sends_stage_configs_not_an_int(
    adapter: CognilensAdapter,
) -> None:
    """The MCP tool's parameter is `list[dict]`; the adapter defaulted to `3`.

    Every call was therefore rejected by FastMCP before reaching a model.
    """
    with _patch_call_tool(_tool_result({"final_text": "done"})) as mock:
        await adapter.progressive_compress("x" * 50, stages=3)
    stages = mock.await_args.args[1]["stages"]
    assert isinstance(stages, list)
    assert len(stages) == 3
    assert all(isinstance(s, dict) and "target_ratio" in s for s in stages)


async def test_progressive_compress_passes_explicit_stage_configs_through(
    adapter: CognilensAdapter,
) -> None:
    explicit = [{"target_ratio": 0.5}, {"target_ratio": 0.5, "preserve": ["api"]}]
    with _patch_call_tool(_tool_result({"final_text": "done"})) as mock:
        await adapter.progressive_compress("x" * 50, stages=explicit)
    assert mock.await_args.args[1]["stages"] == explicit


def test_stage_configs_compound_to_the_intended_ratio() -> None:
    """Ratios are relative to the previous stage's output, so they multiply."""
    for count in (1, 2, 3, 5):
        stages = CognilensAdapter._stage_configs(count)
        compound = 1.0
        for stage in stages:
            compound *= float(stage["target_ratio"])
        assert compound == pytest.approx(0.3, abs=0.01)


# ---- shape edge cases -------------------------------------------------


async def test_bare_prose_response_is_passed_through(adapter: CognilensAdapter) -> None:
    """Undocumented, but it *is* text -- do not fail a call that produced content."""
    with _patch_call_tool("just some prose, not JSON"):
        assert await adapter.summarize("x" * 50) == "just some prose, not JSON"


async def test_structured_content_wrapper_is_unwrapped_one_level(
    adapter: CognilensAdapter,
) -> None:
    with _patch_call_tool(_tool_result({"result": {"summary": "要約"}})):
        assert await adapter.summarize("x" * 50) == "要約"


async def test_a_genuine_top_level_key_beats_the_wrapper(adapter: CognilensAdapter) -> None:
    with _patch_call_tool(_tool_result({"summary": "本物", "result": {"summary": "偽"}})):
        assert await adapter.summarize("x" * 50) == "本物"


async def test_a_non_text_value_raises(adapter: CognilensAdapter) -> None:
    with _patch_call_tool(_tool_result({"summary": {"nested": "dict"}})):
        with pytest.raises(CognilensError) as exc:
            await adapter.summarize("x" * 50)
    assert exc.value.error_type == "CognilensShapeError"


# ---- health_check -----------------------------------------------------


async def test_health_check_requires_the_expected_tools(adapter: CognilensAdapter) -> None:
    with patch.object(
        CognilensAdapter,
        "list_tools",
        new=AsyncMock(return_value=["summarize", "compress_context", "extract_essence"]),
    ):
        assert await adapter.health_check() is True

    with patch.object(
        CognilensAdapter, "list_tools", new=AsyncMock(return_value=["summarize"])
    ):
        assert await adapter.health_check() is False

    with patch.object(
        CognilensAdapter, "list_tools", new=AsyncMock(side_effect=RuntimeError("down"))
    ):
        assert await adapter.health_check() is False
