"""Unit tests for smart_read and smart_analyze MCP tools."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from magickit.adapters.phanthand import (
    PhanthandConnectionError,
    PhanthandFileNotFoundError,
)
from magickit.mcp.tools.smart_read import _format_essence, _is_glob_pattern


# ── Helper utilities ──────────────────────────────────────────────


class TestIsGlobPattern:
    """Tests for _is_glob_pattern."""

    def test_absolute_path(self) -> None:
        assert _is_glob_pattern("/home/user/src/main.py") is False

    def test_star_pattern(self) -> None:
        assert _is_glob_pattern("src/*.py") is True

    def test_double_star_pattern(self) -> None:
        assert _is_glob_pattern("src/**/*.py") is True

    def test_question_mark_pattern(self) -> None:
        assert _is_glob_pattern("src/file?.py") is True

    def test_bracket_pattern(self) -> None:
        assert _is_glob_pattern("src/[abc].py") is True


class TestFormatEssence:
    """Tests for _format_essence."""

    def test_with_concepts(self) -> None:
        essence = {"concepts": ["auth", "middleware"]}
        result = _format_essence(essence)
        assert "Concepts" in result
        assert "auth" in result

    def test_with_dict_concepts(self) -> None:
        essence = {
            "concepts": [
                {"name": "Auth", "description": "Handles authentication"},
            ],
        }
        result = _format_essence(essence)
        assert "**Auth**" in result

    def test_with_relationships(self) -> None:
        essence = {
            "relationships": [
                {"from": "Auth", "to": "DB", "type": "depends_on"},
            ],
        }
        result = _format_essence(essence)
        assert "Auth" in result
        assert "DB" in result

    def test_empty_essence(self) -> None:
        result = _format_essence({})
        assert result == "{}"


# ── smart_read tool tests ─────────────────────────────────────────


def _make_settings() -> MagicMock:
    """Create a mock Settings object."""
    settings = MagicMock()
    settings.cognilens_mcp_url = "http://cognilens:8003"
    settings.lexora_mcp_url = "http://lexora:8001"
    settings.prismind_mcp_url = "http://prismind:8002"
    return settings


def _register_and_get_tools(settings: MagicMock) -> dict[str, Any]:
    """Register tools and capture the inner functions via a mock @mcp.tool().

    Avoids touching FastMCP's private `_tool_manager` (which has been
    renamed/removed across 2.x minor versions) by intercepting the
    decorator itself.
    """
    registered: dict[str, Any] = {}

    def fake_tool(*args, **kwargs):
        def decorator(fn):
            registered[fn.__name__] = fn
            return fn

        return decorator

    mock_mcp = MagicMock()
    mock_mcp.tool = fake_tool

    from magickit.mcp.tools.smart_read import register_tools
    register_tools(mock_mcp, settings)
    return registered


@pytest.fixture
def settings() -> MagicMock:
    return _make_settings()


@pytest.fixture
def tools(settings: MagicMock) -> dict[str, Any]:
    return _register_and_get_tools(settings)


# ── smart_read: parameter validation ─────────────────────────────


@pytest.mark.asyncio
async def test_smart_read_no_files(tools: dict) -> None:
    """Test smart_read with empty files list."""
    result = await tools["smart_read"](
        files=[],
        phanthand_url="http://localhost:7300",
        phanthand_api_key="key",
    )
    assert result["success"] is False
    assert "No files" in result["error"]


@pytest.mark.asyncio
async def test_smart_read_no_url(tools: dict) -> None:
    """Test smart_read without phanthand_url."""
    result = await tools["smart_read"](
        files=["src/main.py"],
        phanthand_url="",
        phanthand_api_key="key",
    )
    assert result["success"] is False
    assert "phanthand_url" in result["error"]


@pytest.mark.asyncio
async def test_smart_read_no_api_key(tools: dict) -> None:
    """Test smart_read without phanthand_api_key."""
    result = await tools["smart_read"](
        files=["src/main.py"],
        phanthand_url="http://localhost:7300",
        phanthand_api_key="",
    )
    assert result["success"] is False
    assert "phanthand_api_key" in result["error"]


@pytest.mark.asyncio
async def test_smart_read_invalid_mode(tools: dict) -> None:
    """Test smart_read with invalid mode."""
    result = await tools["smart_read"](
        files=["src/main.py"],
        phanthand_url="http://localhost:7300",
        phanthand_api_key="key",
        mode="invalid",
    )
    assert result["success"] is False
    assert "Invalid mode" in result["error"]


# ── smart_read: raw mode ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_smart_read_raw_mode(tools: dict) -> None:
    """Test smart_read in raw mode (no Cognilens processing)."""
    mock_phanthand = AsyncMock()
    mock_phanthand.read_file.return_value = {
        "path": "/src/main.py",
        "content": "print('hello')",
        "size": 14,
        "encoding": "utf-8",
    }

    with patch("magickit.mcp.tools.smart_read._get_phanthand", return_value=mock_phanthand):
        result = await tools["smart_read"](
            files=["/src/main.py"],
            phanthand_url="http://localhost:7300",
            phanthand_api_key="key",
            mode="raw",
        )

    assert result["success"] is True
    assert result["mode"] == "raw"
    assert len(result["results"]) == 1
    assert result["results"][0]["processed"] == "print('hello')"
    assert result["file_count"] == 1


# ── smart_read: summarize mode ───────────────────────────────────


@pytest.mark.asyncio
async def test_smart_read_summarize_mode(tools: dict) -> None:
    """Test smart_read in summarize mode."""
    mock_phanthand = AsyncMock()
    mock_phanthand.read_file.return_value = {
        "path": "/src/main.py",
        "content": "def main():\n    pass\n" * 100,
        "size": 2000,
        "encoding": "utf-8",
    }

    with patch("magickit.mcp.tools.smart_read._get_phanthand", return_value=mock_phanthand), \
         patch("magickit.mcp.tools.smart_read.CognilensAdapter") as MockCognilens:
        mock_cog = AsyncMock()
        mock_cog.summarize.return_value = "A main function that does nothing."
        MockCognilens.return_value = mock_cog

        result = await tools["smart_read"](
            files=["/src/main.py"],
            phanthand_url="http://localhost:7300",
            phanthand_api_key="key",
            mode="summarize",
        )

    assert result["success"] is True
    assert result["results"][0]["processed"] == "A main function that does nothing."
    mock_cog.summarize.assert_called_once()


# ── smart_read: essence mode ─────────────────────────────────────


@pytest.mark.asyncio
async def test_smart_read_essence_mode(tools: dict) -> None:
    """Test smart_read in essence mode."""
    mock_phanthand = AsyncMock()
    mock_phanthand.read_file.return_value = {
        "path": "/src/auth.py",
        "content": "class AuthService: ...",
        "size": 500,
        "encoding": "utf-8",
    }

    with patch("magickit.mcp.tools.smart_read._get_phanthand", return_value=mock_phanthand), \
         patch("magickit.mcp.tools.smart_read.CognilensAdapter") as MockCognilens:
        mock_cog = AsyncMock()
        mock_cog.extract_essence.return_value = {
            "concepts": [{"name": "AuthService", "description": "Auth handler"}],
        }
        MockCognilens.return_value = mock_cog

        result = await tools["smart_read"](
            files=["/src/auth.py"],
            phanthand_url="http://localhost:7300",
            phanthand_api_key="key",
            mode="essence",
            focus="authentication",
        )

    assert result["success"] is True
    assert "AuthService" in result["results"][0]["processed"]
    mock_cog.extract_essence.assert_called_once()


# ── smart_read: compress mode ────────────────────────────────────


@pytest.mark.asyncio
async def test_smart_read_compress_mode(tools: dict) -> None:
    """Test smart_read in compress mode."""
    mock_phanthand = AsyncMock()
    mock_phanthand.read_file.return_value = {
        "path": "/src/big.py",
        "content": "x = 1\n" * 1000,
        "size": 6000,
        "encoding": "utf-8",
    }

    with patch("magickit.mcp.tools.smart_read._get_phanthand", return_value=mock_phanthand), \
         patch("magickit.mcp.tools.smart_read.CognilensAdapter") as MockCognilens:
        mock_cog = AsyncMock()
        mock_cog.compress.return_value = "Variable assignment repeated 1000 times."
        MockCognilens.return_value = mock_cog

        result = await tools["smart_read"](
            files=["/src/big.py"],
            phanthand_url="http://localhost:7300",
            phanthand_api_key="key",
            mode="compress",
            focus="variables",
        )

    assert result["success"] is True
    assert result["results"][0]["processed"] == "Variable assignment repeated 1000 times."
    mock_cog.compress.assert_called_once()


# ── smart_read: multiple files ───────────────────────────────────


@pytest.mark.asyncio
async def test_smart_read_multiple_files(tools: dict) -> None:
    """Test smart_read with multiple files."""
    call_count = 0

    async def mock_read_file(url, key, path, encoding="utf-8"):
        nonlocal call_count
        call_count += 1
        return {
            "path": path,
            "content": f"content of {path}",
            "size": 100,
            "encoding": "utf-8",
        }

    mock_phanthand = AsyncMock()
    mock_phanthand.read_file = mock_read_file

    with patch("magickit.mcp.tools.smart_read._get_phanthand", return_value=mock_phanthand):
        result = await tools["smart_read"](
            files=["/src/a.py", "/src/b.py", "/src/c.py"],
            phanthand_url="http://localhost:7300",
            phanthand_api_key="key",
            mode="raw",
        )

    assert result["success"] is True
    assert result["file_count"] == 3
    assert len(result["results"]) == 3


# ── smart_read: error handling ───────────────────────────────────


@pytest.mark.asyncio
async def test_smart_read_connection_error_stops(tools: dict) -> None:
    """Test that connection error stops all processing."""
    mock_phanthand = AsyncMock()
    mock_phanthand.read_file.side_effect = PhanthandConnectionError("Cannot connect")

    with patch("magickit.mcp.tools.smart_read._get_phanthand", return_value=mock_phanthand):
        result = await tools["smart_read"](
            files=["/src/a.py", "/src/b.py"],
            phanthand_url="http://localhost:7300",
            phanthand_api_key="key",
            mode="raw",
        )

    assert result["success"] is False
    assert "Cannot connect" in result["error"]


@pytest.mark.asyncio
async def test_smart_read_per_file_error_continues(tools: dict) -> None:
    """Test that per-file errors don't stop other files."""
    call_count = 0

    async def mock_read_file(url, key, path, encoding="utf-8"):
        nonlocal call_count
        call_count += 1
        if "missing" in path:
            raise PhanthandFileNotFoundError(f"Not found: {path}")
        return {
            "path": path,
            "content": f"content of {path}",
            "size": 100,
            "encoding": "utf-8",
        }

    mock_phanthand = AsyncMock()
    mock_phanthand.read_file = mock_read_file

    with patch("magickit.mcp.tools.smart_read._get_phanthand", return_value=mock_phanthand):
        result = await tools["smart_read"](
            files=["/src/ok.py", "/src/missing.py", "/src/also_ok.py"],
            phanthand_url="http://localhost:7300",
            phanthand_api_key="key",
            mode="raw",
        )

    assert result["success"] is True
    assert result["file_count"] == 2
    assert len(result["errors"]) == 1
    assert "missing.py" in result["errors"][0]["file"]


# ── smart_analyze tests ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_smart_analyze_no_question(tools: dict) -> None:
    """Test smart_analyze without a question."""
    result = await tools["smart_analyze"](
        files=["/src/main.py"],
        question="",
        phanthand_url="http://localhost:7300",
        phanthand_api_key="key",
    )
    assert result["success"] is False
    assert "question" in result["error"]


@pytest.mark.asyncio
async def test_smart_analyze_basic(tools: dict) -> None:
    """Test smart_analyze with direct file paths."""
    mock_phanthand = AsyncMock()
    mock_phanthand.read_file.return_value = {
        "path": "/src/api.py",
        "content": "class API: ...",
        "size": 200,
        "encoding": "utf-8",
    }

    with patch("magickit.mcp.tools.smart_read._get_phanthand", return_value=mock_phanthand), \
         patch("magickit.mcp.tools.smart_read.CognilensAdapter") as MockCognilens, \
         patch("magickit.mcp.tools.smart_read.LexoraAdapter") as MockLexora:
        mock_cog = AsyncMock()
        mock_cog.unify_summaries.return_value = "Unified summary of API code."
        MockCognilens.return_value = mock_cog

        mock_lex = AsyncMock()
        mock_lex.generate.return_value = "The API uses a class-based pattern."
        MockLexora.return_value = mock_lex

        result = await tools["smart_analyze"](
            files=["/src/api.py"],
            question="What pattern does the API use?",
            phanthand_url="http://localhost:7300",
            phanthand_api_key="key",
        )

    assert result["success"] is True
    assert result["answer"] == "The API uses a class-based pattern."
    assert result["file_count"] == 1
    assert "Unified summary" in result["summary"]


@pytest.mark.asyncio
async def test_smart_analyze_glob_expansion(tools: dict) -> None:
    """Test smart_analyze with glob patterns."""
    mock_phanthand = AsyncMock()
    mock_phanthand.search.return_value = {
        "matches": ["/src/api/auth.py", "/src/api/users.py"],
        "count": 2,
        "truncated": False,
    }
    mock_phanthand.read_file.return_value = {
        "path": "/src/api/auth.py",
        "content": "code here",
        "size": 100,
        "encoding": "utf-8",
    }

    with patch("magickit.mcp.tools.smart_read._get_phanthand", return_value=mock_phanthand), \
         patch("magickit.mcp.tools.smart_read.CognilensAdapter") as MockCognilens, \
         patch("magickit.mcp.tools.smart_read.LexoraAdapter") as MockLexora:
        mock_cog = AsyncMock()
        mock_cog.unify_summaries.return_value = "Summary"
        MockCognilens.return_value = mock_cog

        mock_lex = AsyncMock()
        mock_lex.generate.return_value = "Analysis result"
        MockLexora.return_value = mock_lex

        result = await tools["smart_analyze"](
            files=["src/api/*.py"],
            question="What patterns?",
            phanthand_url="http://localhost:7300",
            phanthand_api_key="key",
            search_root="/src",
        )

    assert result["success"] is True
    assert result["file_count"] == 2
    mock_phanthand.search.assert_called_once()


@pytest.mark.asyncio
async def test_smart_analyze_glob_without_search_root(tools: dict) -> None:
    """Test smart_analyze with glob but no search_root."""
    with patch("magickit.mcp.tools.smart_read._get_phanthand", return_value=AsyncMock()), \
         patch("magickit.mcp.tools.smart_read.CognilensAdapter"), \
         patch("magickit.mcp.tools.smart_read.LexoraAdapter"):
        result = await tools["smart_analyze"](
            files=["src/*.py"],
            question="What patterns?",
            phanthand_url="http://localhost:7300",
            phanthand_api_key="key",
            search_root="",
        )

    assert result["success"] is False
    assert "No files resolved" in result["error"]


@pytest.mark.asyncio
async def test_smart_analyze_max_files_limit(tools: dict) -> None:
    """Test smart_analyze truncates at max_files."""
    mock_phanthand = AsyncMock()
    mock_phanthand.search.return_value = {
        "matches": [f"/src/file{i}.py" for i in range(10)],
        "count": 10,
        "truncated": False,
    }
    mock_phanthand.read_file.return_value = {
        "path": "/src/file.py",
        "content": "code",
        "size": 10,
        "encoding": "utf-8",
    }

    with patch("magickit.mcp.tools.smart_read._get_phanthand", return_value=mock_phanthand), \
         patch("magickit.mcp.tools.smart_read.CognilensAdapter") as MockCognilens, \
         patch("magickit.mcp.tools.smart_read.LexoraAdapter") as MockLexora:
        mock_cog = AsyncMock()
        mock_cog.unify_summaries.return_value = "Summary"
        MockCognilens.return_value = mock_cog

        mock_lex = AsyncMock()
        mock_lex.generate.return_value = "Answer"
        MockLexora.return_value = mock_lex

        result = await tools["smart_analyze"](
            files=["*.py"],
            question="Q?",
            phanthand_url="http://localhost:7300",
            phanthand_api_key="key",
            search_root="/src",
            max_files=3,
        )

    assert result["success"] is True
    assert result["file_count"] == 3
    assert result["truncated"] is True


@pytest.mark.asyncio
async def test_smart_analyze_lexora_failure_returns_summary(tools: dict) -> None:
    """Test smart_analyze still returns summary if Lexora fails."""
    mock_phanthand = AsyncMock()
    mock_phanthand.read_file.return_value = {
        "path": "/src/main.py",
        "content": "code",
        "size": 10,
        "encoding": "utf-8",
    }

    with patch("magickit.mcp.tools.smart_read._get_phanthand", return_value=mock_phanthand), \
         patch("magickit.mcp.tools.smart_read.CognilensAdapter") as MockCognilens, \
         patch("magickit.mcp.tools.smart_read.LexoraAdapter") as MockLexora:
        mock_cog = AsyncMock()
        mock_cog.unify_summaries.return_value = "Good summary here"
        MockCognilens.return_value = mock_cog

        mock_lex = AsyncMock()
        mock_lex.generate.side_effect = RuntimeError("LLM down")
        MockLexora.return_value = mock_lex

        result = await tools["smart_analyze"](
            files=["/src/main.py"],
            question="Q?",
            phanthand_url="http://localhost:7300",
            phanthand_api_key="key",
        )

    assert result["success"] is True
    assert "LLM analysis unavailable" in result["answer"]
    assert result["summary"] == "Good summary here"
