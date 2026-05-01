"""Unit tests for ChatroomAdapter (httpx wrapper)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from magickit.adapters.chatroom import ChatroomAdapter

BASE_URL = "http://localhost:8115"


def _resp(status: int, body: dict, *, url: str = BASE_URL) -> httpx.Response:
    """Build an httpx.Response with a JSON body and an attached request.

    httpx.Response.raise_for_status() requires `_request` to be set, so
    even mocks must come pre-attached.
    """
    return httpx.Response(
        status,
        json=body,
        request=httpx.Request("GET", url),
    )


@pytest.fixture
def adapter() -> ChatroomAdapter:
    return ChatroomAdapter(base_url=BASE_URL, timeout=5.0)


def _patch_request(adapter: ChatroomAdapter, response: httpx.Response) -> AsyncMock:
    """Replace adapter.client.request with a mock that returns `response`."""
    fake_client = MagicMock()
    fake_client.is_closed = False
    fake_client.request = AsyncMock(return_value=response)
    adapter._client = fake_client
    return fake_client.request


# ---- _isoformat -------------------------------------------------------


def test_isoformat_none_returns_none() -> None:
    assert ChatroomAdapter._isoformat(None) is None


def test_isoformat_str_passthrough() -> None:
    assert ChatroomAdapter._isoformat("2026-05-01T00:00:00Z") == "2026-05-01T00:00:00Z"


def test_isoformat_aware_datetime_uses_isoformat() -> None:
    dt = datetime(2026, 5, 1, 0, 0, 0, tzinfo=timezone.utc)
    assert ChatroomAdapter._isoformat(dt) == "2026-05-01T00:00:00+00:00"


def test_isoformat_naive_datetime_appends_z() -> None:
    dt = datetime(2026, 5, 1, 0, 0, 0)
    assert ChatroomAdapter._isoformat(dt) == "2026-05-01T00:00:00Z"


# ---- health_check -----------------------------------------------------


@pytest.mark.asyncio
async def test_health_check_true_on_200(adapter: ChatroomAdapter) -> None:
    _patch_request(adapter, _resp(200, {"status": "healthy"}))
    assert await adapter.health_check() is True


@pytest.mark.asyncio
async def test_health_check_false_on_503(adapter: ChatroomAdapter) -> None:
    _patch_request(adapter, _resp(503, {"status": "degraded"}))
    assert await adapter.health_check() is False


@pytest.mark.asyncio
async def test_health_check_false_on_http_error(adapter: ChatroomAdapter) -> None:
    fake_client = MagicMock()
    fake_client.is_closed = False
    fake_client.request = AsyncMock(side_effect=httpx.ConnectError("boom"))
    adapter._client = fake_client
    assert await adapter.health_check() is False


# ---- open_thread ------------------------------------------------------


@pytest.mark.asyncio
async def test_open_thread_posts_correct_payload(adapter: ChatroomAdapter) -> None:
    request_mock = _patch_request(
        adapter, _resp(201, {"thread": {}, "msg": {}})
    )
    await adapter.open_thread(
        project="p",
        thread_id="T-1",
        title="t",
        owner="alice",
        propose_content="hi",
        tags=["x"],
        commit_ref="abc",
    )
    call = request_mock.call_args
    assert call.args[0] == "POST"
    assert call.args[1] == "/v1/projects/p/threads"
    assert call.kwargs["json"] == {
        "thread_id": "T-1",
        "title": "t",
        "owner": "alice",
        "propose_content": "hi",
        "tags": ["x"],
        "commit_ref": "abc",
    }


@pytest.mark.asyncio
async def test_open_thread_omits_optional_none(adapter: ChatroomAdapter) -> None:
    request_mock = _patch_request(adapter, _resp(201, {}))
    await adapter.open_thread(
        project="p", thread_id="T-1", title="t", owner="a", propose_content="hi"
    )
    body = request_mock.call_args.kwargs["json"]
    # only required fields present
    assert set(body.keys()) == {"thread_id", "title", "owner", "propose_content"}


# ---- post_message -----------------------------------------------------


@pytest.mark.asyncio
async def test_post_message_full_payload(adapter: ChatroomAdapter) -> None:
    request_mock = _patch_request(adapter, _resp(201, {"msg": {}}))
    ts = datetime(2026, 5, 1, 0, 0, 0, tzinfo=timezone.utc)
    await adapter.post_message(
        project="p",
        thread_id="T-1",
        type="answer",
        author="bob",
        content="ok",
        reply_to="msg-002",
        references_threads=["T-x"],
        related_tasks=["TASK-1"],
        closes_thread=None,
        tags=["t1"],
        commit_ref="def",
        timestamp=ts,
    )
    call = request_mock.call_args
    assert call.args[1] == "/v1/projects/p/threads/T-1/messages"
    body = call.kwargs["json"]
    assert body["type"] == "answer"
    assert body["author"] == "bob"
    assert body["content"] == "ok"
    assert body["reply_to"] == "msg-002"
    assert body["references_threads"] == ["T-x"]
    assert body["related_tasks"] == ["TASK-1"]
    assert body["tags"] == ["t1"]
    assert body["commit_ref"] == "def"
    assert body["timestamp"] == "2026-05-01T00:00:00+00:00"
    # closes_thread=None must be omitted
    assert "closes_thread" not in body


# ---- close_thread -----------------------------------------------------


@pytest.mark.asyncio
async def test_close_thread_payload(adapter: ChatroomAdapter) -> None:
    request_mock = _patch_request(adapter, _resp(201, {"thread": {}, "decide_msg": {}}))
    await adapter.close_thread(
        project="p",
        thread_id="T-1",
        summary_content="done",
        author="alice",
        affects_threads=["T-other"],
    )
    call = request_mock.call_args
    assert call.args[1] == "/v1/projects/p/threads/T-1/close"
    body = call.kwargs["json"]
    assert body["summary_content"] == "done"
    assert body["author"] == "alice"
    assert body["affects_threads"] == ["T-other"]


# ---- list_threads -----------------------------------------------------


@pytest.mark.asyncio
async def test_list_threads_query_params(adapter: ChatroomAdapter) -> None:
    request_mock = _patch_request(
        adapter, _resp(200, {"items": [], "total": 0, "limit": 100, "offset": 0})
    )
    await adapter.list_threads(
        project="p",
        status_filter=["active", "awaiting_reply"],
        owner="alice",
        limit=50,
        offset=10,
    )
    call = request_mock.call_args
    assert call.args[1] == "/v1/projects/p/threads"
    params = call.kwargs["params"]
    # repeatable status filter encoded as list[tuple]
    assert ("status", "active") in params
    assert ("status", "awaiting_reply") in params
    assert ("owner", "alice") in params
    assert ("limit", 50) in params
    assert ("offset", 10) in params


# ---- get_thread -------------------------------------------------------


@pytest.mark.asyncio
async def test_get_thread_summary_mode(adapter: ChatroomAdapter) -> None:
    request_mock = _patch_request(
        adapter, _resp(200, {"thread": {}, "messages": [], "mode": "summary"})
    )
    await adapter.get_thread(project="p", thread_id="T-1", mode="summary")
    call = request_mock.call_args
    assert call.args[0] == "GET"
    assert call.args[1] == "/v1/projects/p/threads/T-1"
    assert call.kwargs["params"] == {"mode": "summary"}


# ---- list_events ------------------------------------------------------


@pytest.mark.asyncio
async def test_list_events_filters(adapter: ChatroomAdapter) -> None:
    request_mock = _patch_request(adapter, _resp(200, {"items": [], "total": 0}))
    since = datetime(2026, 5, 1, tzinfo=timezone.utc)
    await adapter.list_events(
        project="p",
        thread_id="T-1",
        action="status_transition",
        since=since,
        limit=200,
    )
    params = dict(request_mock.call_args.kwargs["params"])
    assert params["thread_id"] == "T-1"
    assert params["action"] == "status_transition"
    assert params["since"] == "2026-05-01T00:00:00+00:00"
    assert params["limit"] == 200


# ---- check_integrity --------------------------------------------------


@pytest.mark.asyncio
async def test_check_integrity_route(adapter: ChatroomAdapter) -> None:
    request_mock = _patch_request(
        adapter, _resp(200, {"issues": [], "issue_count": 0, "checked_at": "x"})
    )
    await adapter.check_integrity(project="p")
    call = request_mock.call_args
    assert call.args[0] == "GET"
    assert call.args[1] == "/v1/projects/p/integrity"


# ---- error envelope passthrough ---------------------------------------


@pytest.mark.asyncio
async def test_error_envelope_passes_through(adapter: ChatroomAdapter) -> None:
    """Conclair 4xx body is returned as-is, not raised."""
    err_body = {
        "error_type": "ChatroomNotFoundError",
        "error": "Thread 'T-x' not found",
        "details": {"thread_id": "T-x"},
    }
    _patch_request(adapter, _resp(404, err_body))
    result = await adapter.get_thread(project="p", thread_id="T-x")
    assert result == err_body


@pytest.mark.asyncio
async def test_non_json_error_response_synthesizes_envelope(
    adapter: ChatroomAdapter,
) -> None:
    response = httpx.Response(
        502,
        text="<html>bad gateway</html>",
        request=httpx.Request("GET", BASE_URL),
    )
    _patch_request(adapter, response)
    result = await adapter.get_thread(project="p", thread_id="T-x")
    assert result["error_type"] == "ConclairUpstreamError"
    assert "502" in result["error"]
    assert "bad gateway" in result["details"]["text"]
