"""Tests for document maintenance tools."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from magickit.mcp.tools import document_maintenance


class TestHelperFunctions:
    """Tests for helper functions."""

    def test_parse_result_dict(self):
        """Test parsing dict result."""
        result = {"success": True, "data": "test"}
        parsed = document_maintenance._parse_result(result)
        assert parsed == result

    def test_parse_result_json_string(self):
        """Test parsing JSON string result."""
        result = '{"success": true, "data": "test"}'
        parsed = document_maintenance._parse_result(result)
        assert parsed["success"] is True
        assert parsed["data"] == "test"

    def test_parse_result_none(self):
        """Test parsing None result."""
        parsed = document_maintenance._parse_result(None)
        assert parsed == {}

    def test_parse_list_result_list(self):
        """Test parsing list result."""
        result = [{"id": "1"}, {"id": "2"}]
        parsed = document_maintenance._parse_list_result(result)
        assert parsed == result

    def test_parse_list_result_json_string(self):
        """Test parsing JSON list string."""
        result = '[{"id": "1"}, {"id": "2"}]'
        parsed = document_maintenance._parse_list_result(result)
        assert len(parsed) == 2


class TestSmartDeleteDocument:
    """Tests for smart_delete_document tool."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Setup test fixtures."""
        self.mock_settings = MagicMock()
        self.mock_settings.prismind_url = "http://localhost:8112"
        self.mock_settings.prismind_timeout = 30.0
        document_maintenance._settings = self.mock_settings

    @pytest.mark.asyncio
    async def test_delete_document_dry_run(self):
        """Test dry run deletion preview."""
        with patch.object(
            document_maintenance, "PrismindAdapter"
        ) as mock_adapter_class:
            mock_adapter = AsyncMock()
            mock_adapter.get_document = AsyncMock(
                return_value={
                    "found": True,
                    "doc_id": "doc-123",
                    "document": {
                        "name": "Test Doc",
                        "doc_type": "design",
                        "project": "test-project",
                    },
                }
            )
            mock_adapter.search_knowledge = AsyncMock(return_value=[])
            mock_adapter_class.return_value = mock_adapter

            result = await document_maintenance._smart_delete_document_impl(
                settings=self.mock_settings,
                doc_id="doc-123",
                dry_run=True,
            )

            assert result["success"] is True
            assert result["dry_run"] is True
            assert "would_delete" in result
            assert result["would_delete"]["document"] == "doc-123"

    @pytest.mark.asyncio
    async def test_delete_document_not_found(self):
        """Test deletion of non-existent document."""
        with patch.object(
            document_maintenance, "PrismindAdapter"
        ) as mock_adapter_class:
            mock_adapter = AsyncMock()
            mock_adapter.get_document = AsyncMock(return_value={"found": False})
            mock_adapter_class.return_value = mock_adapter

            result = await document_maintenance._smart_delete_document_impl(
                settings=self.mock_settings,
                doc_id="nonexistent",
            )

            assert result["success"] is False
            assert "not found" in result["message"]

    @pytest.mark.asyncio
    async def test_delete_document_with_knowledge(self):
        """Test deletion with related knowledge."""
        with patch.object(
            document_maintenance, "PrismindAdapter"
        ) as mock_adapter_class:
            mock_adapter = AsyncMock()
            mock_adapter.get_document = AsyncMock(
                return_value={
                    "found": True,
                    "doc_id": "doc-123",
                    "document": {"name": "Test Doc", "doc_type": "design"},
                }
            )
            mock_adapter.search_knowledge = AsyncMock(
                return_value=[
                    {
                        "id": "k1",
                        "source": "doc:doc-123",
                        "content": "Related content",
                    }
                ]
            )
            mock_adapter.delete_knowledge = AsyncMock(return_value={"success": True})
            mock_adapter.delete_document = AsyncMock(
                return_value={"success": True, "drive_file_deleted": True}
            )
            mock_adapter_class.return_value = mock_adapter

            result = await document_maintenance._smart_delete_document_impl(
                settings=self.mock_settings,
                doc_id="doc-123",
                delete_related_knowledge=True,
                dry_run=False,
            )

            assert result["success"] is True
            assert result["knowledge_deleted_count"] == 1


class TestDetectOrphanDocuments:
    """Tests for detect_orphan_documents tool."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Setup test fixtures."""
        self.mock_settings = MagicMock()
        self.mock_settings.prismind_url = "http://localhost:8112"
        self.mock_settings.prismind_timeout = 30.0
        document_maintenance._settings = self.mock_settings

    @pytest.mark.asyncio
    async def test_detect_orphan_documents_deleted_project(self):
        """Test detection of docs in deleted projects."""
        with patch.object(
            document_maintenance, "PrismindAdapter"
        ) as mock_adapter_class:
            mock_adapter = AsyncMock()
            mock_adapter.list_projects = AsyncMock(
                return_value={
                    "projects": [
                        {"project": "active-project", "status": "active"},
                        {"project": "archived-project", "status": "archived"},
                    ]
                }
            )
            mock_adapter.list_document_types = AsyncMock(
                return_value={"document_types": [{"type_id": "design"}]}
            )
            mock_adapter.search_documents = AsyncMock(
                return_value=[
                    {
                        "doc_id": "doc-1",
                        "project": "archived-project",
                        "doc_type": "design",
                    }
                ]
            )
            mock_adapter_class.return_value = mock_adapter

            result = await document_maintenance._detect_orphan_documents_impl(
                settings=self.mock_settings,
            )

            assert result["success"] is True
            assert result["total_orphans"] == 1
            assert len(result["deleted_project_docs"]) == 1

    @pytest.mark.asyncio
    async def test_detect_orphan_documents_missing_doc_type(self):
        """Test detection of docs with unregistered doc_type."""
        with patch.object(
            document_maintenance, "PrismindAdapter"
        ) as mock_adapter_class:
            mock_adapter = AsyncMock()
            mock_adapter.list_projects = AsyncMock(
                return_value={"projects": [{"project": "test", "status": "active"}]}
            )
            mock_adapter.list_document_types = AsyncMock(
                return_value={"document_types": [{"type_id": "design"}]}
            )
            mock_adapter.search_documents = AsyncMock(
                return_value=[
                    {
                        "doc_id": "doc-1",
                        "project": "test",
                        "doc_type": "unknown_type",  # Not registered
                    }
                ]
            )
            mock_adapter_class.return_value = mock_adapter

            result = await document_maintenance._detect_orphan_documents_impl(
                settings=self.mock_settings,
            )

            assert result["success"] is True
            assert result["total_orphans"] == 1
            assert len(result["missing_doc_type_docs"]) == 1


class TestDetectOrphanKnowledge:
    """Tests for detect_orphan_knowledge tool."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Setup test fixtures."""
        self.mock_settings = MagicMock()
        self.mock_settings.prismind_url = "http://localhost:8112"
        self.mock_settings.prismind_timeout = 30.0
        document_maintenance._settings = self.mock_settings

    @pytest.mark.asyncio
    async def test_detect_orphan_knowledge_invalid_doc_ref(self):
        """Test detection of knowledge with invalid document references."""
        with patch.object(
            document_maintenance, "PrismindAdapter"
        ) as mock_adapter_class:
            mock_adapter = AsyncMock()
            mock_adapter.search_knowledge = AsyncMock(
                return_value=[
                    {
                        "id": "k1",
                        "source": "doc:nonexistent-doc",
                        "category": "design",
                    }
                ]
            )
            mock_adapter.get_document = AsyncMock(return_value={"found": False})
            mock_adapter_class.return_value = mock_adapter

            result = await document_maintenance._detect_orphan_knowledge_impl(
                settings=self.mock_settings,
            )

            assert result["success"] is True
            assert result["total_orphans"] == 1
            assert len(result["invalid_document_refs"]) == 1


class TestDetectUnusedDocumentTypes:
    """Tests for detect_unused_document_types tool."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Setup test fixtures."""
        self.mock_settings = MagicMock()
        self.mock_settings.prismind_url = "http://localhost:8112"
        self.mock_settings.prismind_timeout = 30.0
        document_maintenance._settings = self.mock_settings

    @pytest.mark.asyncio
    async def test_detect_unused_types(self):
        """Test detection of unused document types."""
        with patch.object(
            document_maintenance, "PrismindAdapter"
        ) as mock_adapter_class:
            mock_adapter = AsyncMock()
            mock_adapter.list_document_types = AsyncMock(
                return_value={
                    "document_types": [
                        {"type_id": "used_type", "name": "Used Type"},
                        {"type_id": "unused_type", "name": "Unused Type"},
                    ]
                }
            )
            mock_adapter.search_documents = AsyncMock(
                side_effect=[
                    [{"doc_id": "doc-1"}],  # used_type has docs
                    [],  # unused_type has no docs
                ]
            )
            mock_adapter.find_similar_document_type = AsyncMock(
                return_value={"found": False}
            )
            mock_adapter_class.return_value = mock_adapter

            result = await document_maintenance._detect_unused_document_types_impl(
                settings=self.mock_settings,
                include_semantic_duplicates=False,
            )

            assert result["success"] is True
            assert result["total_unused"] == 1
            assert result["unused_types"][0]["type_id"] == "unused_type"


class TestCheckDocumentConsistency:
    """Tests for check_document_consistency tool."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Setup test fixtures."""
        self.mock_settings = MagicMock()
        self.mock_settings.prismind_url = "http://localhost:8112"
        self.mock_settings.prismind_timeout = 30.0
        document_maintenance._settings = self.mock_settings

    @pytest.mark.asyncio
    async def test_consistency_check_runs_all_detectors(self):
        """Test that consistency check runs all detection tools."""
        with patch.object(
            document_maintenance, "_detect_orphan_documents_impl"
        ) as mock_orphan_docs, patch.object(
            document_maintenance, "_detect_orphan_knowledge_impl"
        ) as mock_orphan_knowledge, patch.object(
            document_maintenance, "_detect_unused_document_types_impl"
        ) as mock_unused_types:
            mock_orphan_docs.return_value = {
                "total_orphans": 2,
                "orphans": [{"doc_id": "d1"}, {"doc_id": "d2"}],
            }
            mock_orphan_knowledge.return_value = {
                "total_orphans": 1,
                "orphans": [{"knowledge_id": "k1"}],
            }
            mock_unused_types.return_value = {
                "total_unused": 1,
                "unused_types": [{"type_id": "t1"}],
                "total_duplicates": 0,
                "semantic_duplicates": [],
            }

            result = await document_maintenance._check_document_consistency_impl(
                settings=self.mock_settings,
            )

            assert result["success"] is True
            assert result["summary"]["orphan_documents"] == 2
            assert result["summary"]["orphan_knowledge"] == 1
            assert result["summary"]["unused_document_types"] == 1
            assert result["summary"]["total_issues"] == 4


class TestCleanupDocuments:
    """Tests for cleanup_documents tool."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Setup test fixtures."""
        self.mock_settings = MagicMock()
        self.mock_settings.prismind_url = "http://localhost:8112"
        self.mock_settings.prismind_timeout = 30.0
        document_maintenance._settings = self.mock_settings

    @pytest.mark.asyncio
    async def test_cleanup_requires_confirm(self):
        """Test that cleanup requires confirm for non-dry-run."""
        result = await document_maintenance._cleanup_documents_impl(
            settings=self.mock_settings,
            cleanup_orphan_documents=True,
            dry_run=False,
            confirm=False,  # Not confirmed
        )

        assert result["success"] is False
        assert "confirm=True" in result["message"]

    @pytest.mark.asyncio
    async def test_cleanup_dry_run_preview(self):
        """Test cleanup dry run shows what would be deleted."""
        with patch.object(
            document_maintenance, "_detect_orphan_documents_impl"
        ) as mock_detect:
            mock_detect.return_value = {
                "orphans": [
                    {"doc_id": "doc-1", "name": "Doc 1", "reasons": ["deleted_project"]}
                ]
            }

            result = await document_maintenance._cleanup_documents_impl(
                settings=self.mock_settings,
                cleanup_orphan_documents=True,
                dry_run=True,
            )

            assert result["success"] is True
            assert result["dry_run"] is True
            assert len(result["deleted"]["documents"]) == 1
            assert result["deleted"]["documents"][0]["would_delete"] is True


class TestRegisterTools:
    """Tests for tool registration."""

    def test_register_tools(self):
        """Test that tools are registered correctly."""
        mock_mcp = MagicMock()
        mock_settings = MagicMock()

        document_maintenance.register_tools(mock_mcp, mock_settings)

        # Should register 6 tools
        assert mock_mcp.tool.call_count == 6
