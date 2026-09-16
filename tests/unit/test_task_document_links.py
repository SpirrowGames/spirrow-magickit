"""Tests for task <-> document linking and the project id it depends on.

The UTID that ties a task to its documents is derived from the project's
identifier, which Magickit can only learn from Prismind's get_progress
payload. Two separate defects broke that chain:

  * init_project wrote the id back under the key ``project_uid``, which
    Prismind's update_project does not accept, so it was dropped.
  * add_task skipped its attach_docs block whenever the id was missing --
    without saying so, returning ``linked_docs: []`` on a successful call.

These tests pin both.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from magickit.config import Settings
from magickit.mcp.tools import project as project_tools
from magickit.mcp.tools import task


def _prismind_stub() -> AsyncMock:
    adapter = AsyncMock()
    adapter.search_knowledge = AsyncMock(return_value=[])
    adapter.add_task = AsyncMock(return_value={"success": True, "message": "Task added"})
    adapter.add_knowledge = AsyncMock(return_value={"success": True})
    return adapter


class TestAttachDocs:
    """add_task's attach_docs path."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.settings = MagicMock()
        self.settings.prismind_url = "http://localhost:8112"
        self.settings.prismind_timeout = 30.0
        task._settings = self.settings

    @pytest.mark.asyncio
    async def test_links_documents_when_project_id_is_present(self):
        """A project id in the progress payload is enough to link docs."""
        adapter = _prismind_stub()
        adapter.get_progress = AsyncMock(return_value={
            "current_phase": "Phase 1",
            "phases": [{"phase": "Phase 1", "tasks": []}],
            "root_folder_id": "folder-uid-1",
        })

        with patch.object(task, "PrismindAdapter", return_value=adapter):
            result = await task.add_task_impl(
                settings=self.settings,
                name="Design doc task",
                attach_docs=["doc-123", "doc-456"],
            )

        assert result["success"] is True
        assert result["utid"] == "folder-uid-1:phase1:T01"
        assert result["linked_docs"] == ["doc-123", "doc-456"]
        assert result["warnings"] == []

        link_calls = [
            call for call in adapter.add_knowledge.await_args_list
            if call.kwargs.get("category") == "task_doc_link"
        ]
        assert len(link_calls) == 2
        assert link_calls[0].kwargs["source"] == "doc_link:folder-uid-1:phase1:T01:doc-123"

    @pytest.mark.asyncio
    async def test_legacy_project_uid_key_still_accepted(self):
        """Older payloads spelled the id project_uid; keep reading those."""
        adapter = _prismind_stub()
        adapter.get_progress = AsyncMock(return_value={
            "current_phase": "Phase 1",
            "phases": [{"phase": "Phase 1", "tasks": []}],
            "project_uid": "legacy-uid",
        })

        with patch.object(task, "PrismindAdapter", return_value=adapter):
            result = await task.add_task_impl(
                settings=self.settings,
                name="Design doc task",
                attach_docs=["doc-123"],
            )

        assert result["linked_docs"] == ["doc-123"]

    @pytest.mark.asyncio
    async def test_missing_project_id_warns_instead_of_failing_silently(self):
        """No id means no link -- but the caller has to be told.

        This is the regression that produced ``linked_docs: []`` with an
        empty warnings list, which reads as "linked to nothing" rather
        than "skipped".
        """
        adapter = _prismind_stub()
        adapter.get_progress = AsyncMock(return_value={
            "current_phase": "Phase 1",
            "phases": [{"phase": "Phase 1", "tasks": []}],
        })

        with patch.object(task, "PrismindAdapter", return_value=adapter):
            result = await task.add_task_impl(
                settings=self.settings,
                name="Design doc task",
                attach_docs=["doc-123"],
            )

        assert result["success"] is True
        assert result["linked_docs"] == []
        assert any("attach_docs" in w for w in result["warnings"])

        assert not [
            call for call in adapter.add_knowledge.await_args_list
            if call.kwargs.get("category") == "task_doc_link"
        ]

    @pytest.mark.asyncio
    async def test_no_warning_when_no_docs_were_requested(self):
        """The warning is about a skipped request, not about a missing id."""
        adapter = _prismind_stub()
        adapter.get_progress = AsyncMock(return_value={
            "current_phase": "Phase 1",
            "phases": [{"phase": "Phase 1", "tasks": []}],
        })

        with patch.object(task, "PrismindAdapter", return_value=adapter):
            result = await task.add_task_impl(settings=self.settings, name="Plain task")

        assert result["warnings"] == []


def _init_project_tool(settings: Settings):
    """Pull init_project out of register_tools.

    FastMCP's tool-introspection API has moved between minor versions, so
    capture the undecorated function instead of depending on it.
    """
    registered: dict[str, Any] = {}

    def fake_tool(*args: Any, **kwargs: Any):
        def decorator(fn):
            registered[fn.__name__] = fn
            return fn

        return decorator

    mock_mcp = MagicMock()
    mock_mcp.tool = fake_tool
    project_tools.register_tools(mock_mcp, settings)
    return registered["init_project"]


class TestInitProjectWritesTheId:
    """init_project has to store the id under a key Prismind accepts."""

    @pytest.fixture
    def settings(self) -> Settings:
        return Settings(prismind_url="http://localhost:8112", prismind_timeout=30.0)

    @pytest.mark.asyncio
    async def test_sends_root_folder_id_not_project_uid(self, settings):
        adapter = AsyncMock()
        adapter.setup_project = AsyncMock(return_value={
            "success": True,
            "root_folder_id": "folder-uid-1",
        })
        adapter.update_project = AsyncMock(return_value={"success": True})

        init_project = _init_project_tool(settings)
        with patch.object(project_tools, "PrismindAdapter", return_value=adapter):
            result = await init_project(project="demo", template="mcp-server")

        assert result["success"] is True
        assert result["project_uid"] == "folder-uid-1"

        sent = adapter.update_project.await_args.kwargs
        assert sent["root_folder_id"] == "folder-uid-1"
        assert "project_uid" not in sent

    @pytest.mark.asyncio
    async def test_omits_the_key_when_setup_returned_no_id(self, settings):
        """Sending "" would blank an id Prismind already holds."""
        adapter = AsyncMock()
        adapter.setup_project = AsyncMock(return_value={"success": True})
        adapter.update_project = AsyncMock(return_value={"success": True})

        init_project = _init_project_tool(settings)
        with patch.object(project_tools, "PrismindAdapter", return_value=adapter):
            await init_project(project="demo")

        assert "root_folder_id" not in adapter.update_project.await_args.kwargs
