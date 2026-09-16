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


def _doc_link_entry(utid: str, doc_id: str, knowledge_id: str = "") -> dict[str, Any]:
    """A knowledge entry shaped the way add_task writes document links."""
    import json

    return {
        "id": knowledge_id or f"k-{doc_id}",
        "content": json.dumps({"type": "doc_link", "doc_id": doc_id, "utid": utid}),
        "category": "task_doc_link",
    }


class TestReadLinkedDocs:
    """get_task reads back what attach_docs wrote.

    Before this, the link was write-only: nothing queried task_doc_link,
    so a task could be linked to a design document with no way to ask
    what it was linked to.
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        self.settings = MagicMock()
        self.settings.prismind_url = "http://localhost:8112"
        self.settings.prismind_timeout = 30.0
        task._settings = self.settings

    def _adapter(self, entries: list[dict[str, Any]], *, project_uid: str = "folder-uid-1"):
        adapter = AsyncMock()
        adapter.call = AsyncMock(return_value={
            "success": True,
            "task": {"task_id": "T01", "name": "Design doc task"},
            "phase": "Phase 1",
            "project": "demo",
        })
        progress: dict[str, Any] = {"current_phase": "Phase 1", "phases": []}
        if project_uid:
            progress["root_folder_id"] = project_uid
        adapter.get_progress = AsyncMock(return_value=progress)
        adapter.search_knowledge = AsyncMock(return_value=entries)
        return adapter

    @pytest.mark.asyncio
    async def test_returns_linked_documents(self):
        utid = "folder-uid-1:phase1:T01"
        adapter = self._adapter([
            _doc_link_entry(utid, "doc-123"),
            _doc_link_entry(utid, "doc-456"),
        ])

        with patch.object(task, "PrismindAdapter", return_value=adapter):
            result = await task.get_task_impl(
                settings=self.settings, task_id="T01", phase="Phase 1", project="demo"
            )

        assert [d["doc_id"] for d in result["linked_docs"]] == ["doc-123", "doc-456"]
        assert result["linked_docs"][0]["knowledge_id"] == "k-doc-123"

        query = adapter.search_knowledge.await_args.kwargs
        assert query["category"] == "task_doc_link"
        assert query["query"] == f"utid:{utid}"

    @pytest.mark.asyncio
    async def test_ignores_entries_belonging_to_other_tasks(self):
        """The search is semantic; the recorded UTID decides membership."""
        utid = "folder-uid-1:phase1:T01"
        adapter = self._adapter([
            _doc_link_entry(utid, "doc-123"),
            _doc_link_entry("folder-uid-1:phase1:T99", "doc-999"),
        ])

        with patch.object(task, "PrismindAdapter", return_value=adapter):
            result = await task.get_task_impl(
                settings=self.settings, task_id="T01", phase="Phase 1", project="demo"
            )

        assert [d["doc_id"] for d in result["linked_docs"]] == ["doc-123"]

    @pytest.mark.asyncio
    async def test_collapses_duplicate_doc_ids(self):
        utid = "folder-uid-1:phase1:T01"
        adapter = self._adapter([
            _doc_link_entry(utid, "doc-123", "k-1"),
            _doc_link_entry(utid, "doc-123", "k-2"),
        ])

        with patch.object(task, "PrismindAdapter", return_value=adapter):
            result = await task.get_task_impl(
                settings=self.settings, task_id="T01", phase="Phase 1", project="demo"
            )

        assert len(result["linked_docs"]) == 1
        assert result["linked_docs"][0]["knowledge_id"] == "k-1"

    @pytest.mark.asyncio
    async def test_skips_unparseable_entries(self):
        utid = "folder-uid-1:phase1:T01"
        adapter = self._adapter([
            {"id": "k-bad", "content": "not json"},
            {"id": "k-none", "content": None},
            _doc_link_entry(utid, "doc-123"),
        ])

        with patch.object(task, "PrismindAdapter", return_value=adapter):
            result = await task.get_task_impl(
                settings=self.settings, task_id="T01", phase="Phase 1", project="demo"
            )

        assert [d["doc_id"] for d in result["linked_docs"]] == ["doc-123"]

    @pytest.mark.asyncio
    async def test_search_failure_does_not_fail_get_task(self):
        adapter = self._adapter([])
        adapter.search_knowledge = AsyncMock(side_effect=RuntimeError("prismind down"))

        with patch.object(task, "PrismindAdapter", return_value=adapter):
            result = await task.get_task_impl(
                settings=self.settings, task_id="T01", phase="Phase 1", project="demo"
            )

        assert result["success"] is True
        assert result["linked_docs"] == []

    @pytest.mark.asyncio
    async def test_no_project_id_yields_no_links(self):
        adapter = self._adapter([], project_uid="")

        with patch.object(task, "PrismindAdapter", return_value=adapter):
            result = await task.get_task_impl(
                settings=self.settings, task_id="T01", phase="Phase 1", project="demo"
            )

        assert result["linked_docs"] == []
        adapter.search_knowledge.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_can_be_turned_off(self):
        adapter = self._adapter([])

        with patch.object(task, "PrismindAdapter", return_value=adapter):
            result = await task.get_task_impl(
                settings=self.settings,
                task_id="T01",
                phase="Phase 1",
                project="demo",
                include_linked_docs=False,
            )

        assert "linked_docs" not in result
        adapter.get_progress.assert_not_awaited()


class TestStartTaskSurfacesLinks:
    """start_task hands the linked documents to whoever picks the task up."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.settings = MagicMock()
        self.settings.prismind_url = "http://localhost:8112"
        self.settings.prismind_timeout = 30.0
        self.settings.cognilens_url = "http://localhost:8111"
        self.settings.cognilens_timeout = 30.0
        task._settings = self.settings

    def _adapter(self, *, project_uid: str, entries: list[dict[str, Any]] | None = None):
        adapter = AsyncMock()
        progress: dict[str, Any] = {
            "current_phase": "Phase 1",
            "phases": [{
                "phase": "Phase 1",
                "tasks": [{"task_id": "T01", "name": "Design doc task", "status": "not_started"}],
            }],
        }
        if project_uid:
            progress["root_folder_id"] = project_uid
        adapter.get_progress = AsyncMock(return_value=progress)
        adapter.start_task = AsyncMock(return_value={"success": True, "message": "started"})
        adapter.search_knowledge = AsyncMock(return_value=entries or [])
        return adapter

    @pytest.mark.asyncio
    async def test_context_carries_linked_docs(self):
        utid = "folder-uid-1:phase1:T01"
        adapter = self._adapter(
            project_uid="folder-uid-1",
            entries=[_doc_link_entry(utid, "doc-123")],
        )

        with patch.object(task, "PrismindAdapter", return_value=adapter):
            result = await task.start_task_impl(
                settings=self.settings,
                task_id="T01",
                project="demo",
                refresh_attachments=False,
            )

        assert result["success"] is True
        assert [d["doc_id"] for d in result["context"]["linked_docs"]] == ["doc-123"]

    @pytest.mark.asyncio
    async def test_missing_project_id_warns_about_both_features(self):
        """refresh_attachments used to no-op here without a word."""
        adapter = self._adapter(project_uid="")

        with patch.object(task, "PrismindAdapter", return_value=adapter):
            result = await task.start_task_impl(
                settings=self.settings,
                task_id="T01",
                project="demo",
                refresh_attachments=True,
            )

        assert result["success"] is True
        assert result["attachment_status"] == {}
        warning = next(w for w in result["warnings"] if "project_uid" in w)
        assert "document links" in warning
        assert "attachment refresh" in warning

    @pytest.mark.asyncio
    async def test_missing_project_id_warns_about_links_alone(self):
        adapter = self._adapter(project_uid="")

        with patch.object(task, "PrismindAdapter", return_value=adapter):
            result = await task.start_task_impl(
                settings=self.settings,
                task_id="T01",
                project="demo",
                refresh_attachments=False,
            )

        warning = next(w for w in result["warnings"] if "project_uid" in w)
        assert "document links" in warning
        assert "attachment refresh" not in warning
