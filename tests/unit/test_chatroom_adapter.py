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


# ---- role forwarding (msg-017 I-1) ------------------------------------


@pytest.mark.asyncio
async def test_write_paths_forward_role_when_supplied(
    adapter: ChatroomAdapter,
) -> None:
    """All three write endpoints put `role` on the wire.

    Conclair accepts `role` on OpenThreadRequest / PostMessageRequest /
    CloseThreadRequest (PR#5). If the adapter dropped it on any one of
    them, that path would silently record role=NULL after passing the gate.
    """
    for method, kwargs, path in (
        (
            "open_thread",
            dict(project="p", thread_id="T-1", title="t", owner="a",
                 propose_content="hi", role="proposer"),
            "/v1/projects/p/threads",
        ),
        (
            "post_message",
            dict(project="p", thread_id="T-1", type="report", author="a",
                 content="c", role="implementer"),
            "/v1/projects/p/threads/T-1/messages",
        ),
        (
            "close_thread",
            dict(project="p", thread_id="T-1", summary_content="done",
                 author="a", role="proposer"),
            "/v1/projects/p/threads/T-1/close",
        ),
    ):
        request_mock = _patch_request(adapter, _resp(201, {}))
        await getattr(adapter, method)(**kwargs)
        call = request_mock.call_args
        assert call.args[1] == path
        assert call.kwargs["json"]["role"] == kwargs["role"], method


@pytest.mark.asyncio
async def test_write_paths_omit_role_when_none(adapter: ChatroomAdapter) -> None:
    """role=None must be absent from the body, not sent as JSON null --
    same thin-wrapper convention the other optional fields follow."""
    for method, kwargs in (
        ("open_thread", dict(project="p", thread_id="T-1", title="t",
                             owner="a", propose_content="hi")),
        ("post_message", dict(project="p", thread_id="T-1", type="report",
                              author="a", content="c")),
        ("close_thread", dict(project="p", thread_id="T-1",
                              summary_content="done", author="a")),
    ):
        request_mock = _patch_request(adapter, _resp(201, {}))
        await getattr(adapter, method)(**kwargs)
        assert "role" not in request_mock.call_args.kwargs["json"], method


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


# ---- mark_read --------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_read_with_explicit_msg_id(adapter: ChatroomAdapter) -> None:
    request_mock = _patch_request(
        adapter,
        _resp(200, {
            "project": "p", "identity_name": "Bohr", "thread_id": "T-1",
            "last_read_msg_id": "msg-005", "updated_at": "2026-05-01T00:00:00Z",
            "advanced": True,
        }),
    )
    await adapter.mark_read(
        project="p", thread_id="T-1",
        identity_name="Bohr", up_to_msg_id="msg-005",
    )
    call = request_mock.call_args
    assert call.args[0] == "POST"
    assert call.args[1] == "/v1/projects/p/threads/T-1/read"
    assert call.kwargs["json"] == {
        "identity_name": "Bohr",
        "up_to_msg_id": "msg-005",
    }


@pytest.mark.asyncio
async def test_mark_read_none_omits_up_to_msg_id(adapter: ChatroomAdapter) -> None:
    """up_to_msg_id=None means "catch up to latest" -- the field is
    omitted from the JSON body (kept off the wire) so the catch-up
    payload is the minimal ``{"identity_name": ...}`` form."""
    request_mock = _patch_request(
        adapter,
        _resp(200, {
            "project": "p", "identity_name": "Bohr", "thread_id": "T-1",
            "last_read_msg_id": "msg-001", "updated_at": "2026-05-01T00:00:00Z",
            "advanced": True,
        }),
    )
    await adapter.mark_read(
        project="p", thread_id="T-1", identity_name="Bohr",
    )
    body = request_mock.call_args.kwargs["json"]
    assert body == {"identity_name": "Bohr"}
    assert "up_to_msg_id" not in body


@pytest.mark.asyncio
async def test_mark_read_empty_string_forwards_as_empty(
    adapter: ChatroomAdapter,
) -> None:
    """Empty string is forwarded as-is. The server treats empty == null
    (both advance to latest); the MCP wrapper normalizes "" -> None
    before calling the adapter, so this adapter-level test pins that
    empty-string is still a legal payload value when callers hand-write
    it (e.g. integration smoke scripts)."""
    request_mock = _patch_request(
        adapter,
        _resp(200, {
            "project": "p", "identity_name": "Bohr", "thread_id": "T-1",
            "last_read_msg_id": "msg-003", "updated_at": "2026-05-01T00:00:00Z",
            "advanced": True,
        }),
    )
    await adapter.mark_read(
        project="p", thread_id="T-1",
        identity_name="Bohr", up_to_msg_id="",
    )
    body = request_mock.call_args.kwargs["json"]
    assert body["up_to_msg_id"] == ""


@pytest.mark.asyncio
async def test_mark_read_forwards_error_envelope(
    adapter: ChatroomAdapter,
) -> None:
    err = {
        "error_type": "ChatroomIntegrityError",
        "error": "msg_id 'msg-999' is not in thread 'T-1'",
        "details": {"project": "p", "thread_id": "T-1", "msg_id": "msg-999"},
    }
    _patch_request(adapter, _resp(409, err))
    result = await adapter.mark_read(
        project="p", thread_id="T-1",
        identity_name="Bohr", up_to_msg_id="msg-999",
    )
    assert result == err


# ---- list_unread ------------------------------------------------------


@pytest.mark.asyncio
async def test_list_unread_query_params(adapter: ChatroomAdapter) -> None:
    request_mock = _patch_request(
        adapter,
        _resp(200, {"items": [], "total": 0, "limit": 50, "offset": 10}),
    )
    await adapter.list_unread(
        project="p",
        identity_name="Heisenberg",
        include_resolved=True,
        limit=50,
        offset=10,
    )
    call = request_mock.call_args
    assert call.args[0] == "GET"
    assert call.args[1] == "/v1/projects/p/unread"
    params = dict(call.kwargs["params"])
    assert params["identity_name"] == "Heisenberg"
    # FastAPI parses ?include_resolved=true/false as the bool -- str
    # serialization here matches how the adapter forwards it.
    assert params["include_resolved"] == "true"
    assert params["limit"] == 50
    assert params["offset"] == 10


@pytest.mark.asyncio
async def test_list_unread_default_include_resolved_is_false(
    adapter: ChatroomAdapter,
) -> None:
    request_mock = _patch_request(
        adapter,
        _resp(200, {"items": [], "total": 0, "limit": 100, "offset": 0}),
    )
    await adapter.list_unread(project="p", identity_name="Bohr")
    params = dict(request_mock.call_args.kwargs["params"])
    assert params["include_resolved"] == "false"


# ---- thread digests ----------------------------------------------------


@pytest.mark.asyncio
async def test_put_thread_digest_sends_the_required_fields(
    adapter: ChatroomAdapter,
) -> None:
    request_mock = _patch_request(adapter, _resp(200, {"present": True}))

    await adapter.put_thread_digest(
        project="p",
        thread_id="T-1",
        digest="要約",
        source_last_msg_id="msg-042",
        source_msg_count=18,
        producer="magickit-digest-sweeper",
    )

    method, path = request_mock.call_args.args
    assert method == "PUT"
    assert path == "/v1/projects/p/threads/T-1/digest"
    assert request_mock.call_args.kwargs["json"] == {
        "digest": "要約",
        "source_last_msg_id": "msg-042",
        "source_msg_count": 18,
        "producer": "magickit-digest-sweeper",
        "scope": "thread",
    }


@pytest.mark.asyncio
async def test_put_thread_digest_omits_unset_optionals(
    adapter: ChatroomAdapter,
) -> None:
    """Omitted rather than null, so Conclair's own defaults apply.

    Same convention as mark_read's up_to_msg_id.
    """
    request_mock = _patch_request(adapter, _resp(200, {"present": True}))

    await adapter.put_thread_digest(
        project="p", thread_id="T-1", digest="要約",
        source_last_msg_id="msg-042", source_msg_count=18, producer="x",
    )

    body = request_mock.call_args.kwargs["json"]
    for field in ("style", "truncated", "model", "tier", "target_msg_id",
                  "source_chars", "input_tokens", "output_tokens", "duration_ms"):
        assert field not in body


@pytest.mark.asyncio
async def test_put_thread_digest_carries_provenance_when_given(
    adapter: ChatroomAdapter,
) -> None:
    request_mock = _patch_request(adapter, _resp(200, {"present": True}))

    await adapter.put_thread_digest(
        project="p", thread_id="T-1", digest="要約",
        source_last_msg_id="msg-042", source_msg_count=18, producer="x",
        style="concise", truncated=True, model="Qwen3-32B", tier="light",
        source_chars=21000, input_tokens=6000, output_tokens=380,
        duration_ms=18400,
    )

    body = request_mock.call_args.kwargs["json"]
    assert body["style"] == "concise"
    assert body["truncated"] is True
    assert body["model"] == "Qwen3-32B"
    assert body["tier"] == "light"
    assert body["source_chars"] == 21000
    assert body["duration_ms"] == 18400


@pytest.mark.asyncio
async def test_put_thread_digest_keeps_truncated_false_on_the_wire(
    adapter: ChatroomAdapter,
) -> None:
    """`False` is a claim ("nothing was elided"), not an unset value."""
    request_mock = _patch_request(adapter, _resp(200, {"present": True}))

    await adapter.put_thread_digest(
        project="p", thread_id="T-1", digest="要約",
        source_last_msg_id="msg-042", source_msg_count=18, producer="x",
        truncated=False,
    )

    assert request_mock.call_args.kwargs["json"]["truncated"] is False


@pytest.mark.asyncio
async def test_get_thread_digest_defaults_to_the_thread_scope(
    adapter: ChatroomAdapter,
) -> None:
    request_mock = _patch_request(
        adapter, _resp(200, {"present": False, "digest": None})
    )

    await adapter.get_thread_digest(project="p", thread_id="T-1")

    method, path = request_mock.call_args.args
    assert method == "GET"
    assert path == "/v1/projects/p/threads/T-1/digest"
    assert request_mock.call_args.kwargs["params"] == {"scope": "thread"}


@pytest.mark.asyncio
async def test_get_thread_digest_absence_is_not_an_error(
    adapter: ChatroomAdapter,
) -> None:
    """Callers branch on `present`, not on `error_type`.

    Reading "not digested yet" as an outage means never digesting anything.
    """
    _patch_request(adapter, _resp(200, {"present": False, "digest": None}))

    result = await adapter.get_thread_digest(project="p", thread_id="T-1")

    assert "error_type" not in result
    assert result["present"] is False


@pytest.mark.asyncio
async def test_get_thread_digest_forwards_the_error_envelope(
    adapter: ChatroomAdapter,
) -> None:
    err = {"error_type": "ChatroomNotFoundError", "error": "no thread"}
    _patch_request(adapter, _resp(404, err))

    assert await adapter.get_thread_digest(project="p", thread_id="T-x") == err


@pytest.mark.asyncio
async def test_get_thread_only_asks_for_the_digest_when_told(
    adapter: ChatroomAdapter,
) -> None:
    """Nothing changes for the callers that do not read it."""
    request_mock = _patch_request(adapter, _resp(200, {"thread": {}, "messages": []}))

    await adapter.get_thread(project="p", thread_id="T-1")
    assert request_mock.call_args.kwargs["params"] == {"mode": "full"}

    await adapter.get_thread(project="p", thread_id="T-1", include_digest=True)
    assert request_mock.call_args.kwargs["params"] == {
        "mode": "full",
        "include_digest": "true",
    }
