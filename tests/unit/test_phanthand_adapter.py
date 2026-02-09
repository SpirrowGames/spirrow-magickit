"""Unit tests for PhanthandAdapter."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio

from magickit.adapters.phanthand import (
    PhanthandAdapter,
    PhanthandAuthError,
    PhanthandConnectionError,
    PhanthandFileNotFoundError,
    PhanthandPathNotAllowedError,
    PhanthandRequestError,
    PhanthandTimeoutError,
)

TEST_URL = "http://192.168.1.10:7300"
TEST_API_KEY = "test-secret-key"


@pytest.fixture
def adapter() -> PhanthandAdapter:
    """Create a PhanthandAdapter instance."""
    return PhanthandAdapter(timeout=5.0)


def _mock_response(
    status_code: int = 200,
    data: dict | None = None,
    success: bool = True,
    error: str | None = None,
) -> httpx.Response:
    """Create a mock httpx.Response with Phanthand ApiResponse format."""
    body = {"success": success, "data": data, "error": error}
    return httpx.Response(
        status_code=status_code,
        json=body,
        request=httpx.Request("POST", TEST_URL),
    )


# ── health_check ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_check_success(adapter: PhanthandAdapter) -> None:
    """Test successful health check."""
    health_data = {
        "status": "ok",
        "version": "0.1.0",
        "hostname": "dev-pc",
        "uptime_seconds": 123.4,
    }
    mock_resp = httpx.Response(
        status_code=200,
        json={"success": True, "data": health_data, "error": None},
        request=httpx.Request("GET", f"{TEST_URL}/health"),
    )

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
        result = await adapter.health_check(TEST_URL)

    assert result["status"] == "ok"
    assert result["version"] == "0.1.0"


@pytest.mark.asyncio
async def test_health_check_connection_error(adapter: PhanthandAdapter) -> None:
    """Test health check when Phanthand is unreachable."""
    with patch(
        "httpx.AsyncClient.get",
        new_callable=AsyncMock,
        side_effect=httpx.ConnectError("Connection refused"),
    ):
        with pytest.raises(PhanthandConnectionError):
            await adapter.health_check(TEST_URL)


# ── read_file ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_read_file_success(adapter: PhanthandAdapter) -> None:
    """Test successful file read."""
    file_data = {
        "path": "/home/user/src/main.py",
        "content": "print('hello')",
        "size": 14,
        "encoding": "utf-8",
    }
    mock_resp = _mock_response(data=file_data)

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
        result = await adapter.read_file(TEST_URL, TEST_API_KEY, "/home/user/src/main.py")

    assert result["content"] == "print('hello')"
    assert result["size"] == 14


@pytest.mark.asyncio
async def test_read_file_not_found(adapter: PhanthandAdapter) -> None:
    """Test reading a nonexistent file."""
    mock_resp = _mock_response(
        status_code=404,
        success=False,
        error="File not found: /no/such/file.py",
    )

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
        with pytest.raises(PhanthandFileNotFoundError):
            await adapter.read_file(TEST_URL, TEST_API_KEY, "/no/such/file.py")


@pytest.mark.asyncio
async def test_read_file_path_not_allowed(adapter: PhanthandAdapter) -> None:
    """Test reading a file outside allowed paths."""
    mock_resp = _mock_response(
        status_code=403,
        success=False,
        error="Path not allowed: /etc/passwd",
    )

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
        with pytest.raises(PhanthandPathNotAllowedError):
            await adapter.read_file(TEST_URL, TEST_API_KEY, "/etc/passwd")


@pytest.mark.asyncio
async def test_read_file_auth_error(adapter: PhanthandAdapter) -> None:
    """Test reading with invalid API key."""
    mock_resp = _mock_response(
        status_code=401,
        success=False,
        error="Invalid API key",
    )

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
        with pytest.raises(PhanthandAuthError):
            await adapter.read_file(TEST_URL, "wrong-key", "/home/user/src/main.py")


@pytest.mark.asyncio
async def test_read_file_connection_error(adapter: PhanthandAdapter) -> None:
    """Test connection failure during file read."""
    with patch(
        "httpx.AsyncClient.post",
        new_callable=AsyncMock,
        side_effect=httpx.ConnectError("Connection refused"),
    ):
        with pytest.raises(PhanthandConnectionError):
            await adapter.read_file(TEST_URL, TEST_API_KEY, "/home/user/src/main.py")


@pytest.mark.asyncio
async def test_read_file_timeout(adapter: PhanthandAdapter) -> None:
    """Test timeout during file read."""
    with patch(
        "httpx.AsyncClient.post",
        new_callable=AsyncMock,
        side_effect=httpx.ReadTimeout("Read timed out"),
    ):
        with pytest.raises(PhanthandTimeoutError):
            await adapter.read_file(TEST_URL, TEST_API_KEY, "/home/user/big-file.py")


@pytest.mark.asyncio
async def test_read_file_server_error(adapter: PhanthandAdapter) -> None:
    """Test file size exceeded or other server error."""
    mock_resp = _mock_response(
        status_code=422,
        success=False,
        error="File size 20000000 bytes exceeds limit of 10 MB",
    )

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
        with pytest.raises(PhanthandRequestError, match="exceeds limit"):
            await adapter.read_file(TEST_URL, TEST_API_KEY, "/home/user/huge-file.bin")


# ── list_directory ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_directory_success(adapter: PhanthandAdapter) -> None:
    """Test successful directory listing."""
    list_data = {
        "path": "/home/user/src",
        "entries": [
            {"name": "main.py", "path": "/home/user/src/main.py", "is_dir": False, "size": 100},
            {"name": "utils", "path": "/home/user/src/utils", "is_dir": True},
        ],
        "count": 2,
    }
    mock_resp = _mock_response(data=list_data)

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
        result = await adapter.list_directory(TEST_URL, TEST_API_KEY, "/home/user/src")

    assert result["count"] == 2
    assert len(result["entries"]) == 2


# ── file_exists ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_file_exists_true(adapter: PhanthandAdapter) -> None:
    """Test exists check for existing file."""
    mock_resp = _mock_response(data={
        "path": "/home/user/src/main.py",
        "exists": True,
        "is_file": True,
        "is_dir": False,
    })

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
        result = await adapter.file_exists(TEST_URL, TEST_API_KEY, "/home/user/src/main.py")

    assert result["exists"] is True
    assert result["is_file"] is True


@pytest.mark.asyncio
async def test_file_exists_false(adapter: PhanthandAdapter) -> None:
    """Test exists check for missing file."""
    mock_resp = _mock_response(data={
        "path": "/home/user/no-file.py",
        "exists": False,
        "is_file": False,
        "is_dir": False,
    })

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
        result = await adapter.file_exists(TEST_URL, TEST_API_KEY, "/home/user/no-file.py")

    assert result["exists"] is False


# ── file_info ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_file_info_success(adapter: PhanthandAdapter) -> None:
    """Test getting file metadata."""
    mock_resp = _mock_response(data={
        "path": "/home/user/src/main.py",
        "name": "main.py",
        "size": 1024,
        "created": "2024-01-01T00:00:00Z",
        "modified": "2024-06-15T12:00:00Z",
        "is_file": True,
        "is_dir": False,
        "readonly": False,
    })

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
        result = await adapter.file_info(TEST_URL, TEST_API_KEY, "/home/user/src/main.py")

    assert result["name"] == "main.py"
    assert result["size"] == 1024


# ── tree ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tree_success(adapter: PhanthandAdapter) -> None:
    """Test directory tree."""
    mock_resp = _mock_response(data={
        "path": "/home/user/src",
        "tree": {
            "name": "src",
            "path": "/home/user/src",
            "is_dir": True,
            "children": [
                {"name": "main.py", "path": "/home/user/src/main.py", "is_dir": False, "children": None},
            ],
        },
    })

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
        result = await adapter.tree(TEST_URL, TEST_API_KEY, "/home/user/src", max_depth=2)

    assert result["tree"]["name"] == "src"
    assert len(result["tree"]["children"]) == 1


# ── search ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_success(adapter: PhanthandAdapter) -> None:
    """Test file search."""
    mock_resp = _mock_response(data={
        "path": "/home/user/src",
        "pattern": "*.py",
        "matches": ["/home/user/src/main.py", "/home/user/src/utils.py"],
        "count": 2,
        "truncated": False,
    })

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
        result = await adapter.search(TEST_URL, TEST_API_KEY, "/home/user/src", "*.py")

    assert result["count"] == 2
    assert result["truncated"] is False


@pytest.mark.asyncio
async def test_search_truncated(adapter: PhanthandAdapter) -> None:
    """Test search with truncated results."""
    mock_resp = _mock_response(data={
        "path": "/home/user",
        "pattern": "**/*.py",
        "matches": [f"/home/user/file{i}.py" for i in range(5)],
        "count": 5,
        "truncated": True,
    })

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
        result = await adapter.search(
            TEST_URL, TEST_API_KEY, "/home/user", "**/*.py", max_results=5,
        )

    assert result["truncated"] is True


# ── _request error handling ───────────────────────────────────────


@pytest.mark.asyncio
async def test_request_api_response_failure(adapter: PhanthandAdapter) -> None:
    """Test handling of success=false in Phanthand ApiResponse."""
    mock_resp = _mock_response(
        status_code=200,
        success=False,
        error="Internal processing error",
    )

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
        with pytest.raises(PhanthandRequestError, match="Internal processing error"):
            await adapter.read_file(TEST_URL, TEST_API_KEY, "/home/user/file.py")


@pytest.mark.asyncio
async def test_url_trailing_slash_handling(adapter: PhanthandAdapter) -> None:
    """Test that trailing slashes in URL are handled correctly."""
    mock_resp = _mock_response(data={
        "path": "/home/user/file.py",
        "exists": True,
        "is_file": True,
        "is_dir": False,
    })

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp) as mock_post:
        await adapter.file_exists(f"{TEST_URL}/", TEST_API_KEY, "/home/user/file.py")

        # Verify the URL doesn't have double slashes
        called_url = mock_post.call_args[0][0]
        assert "//" not in called_url.replace("http://", "")
