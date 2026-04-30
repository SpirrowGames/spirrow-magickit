"""Tests for smart document tools (smart_create_document, smart_update_document)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from magickit.mcp.tools import document


class TestSmartUpdateDocument:
    """Tests for smart_update_document_impl."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.mock_settings = MagicMock()
        self.mock_settings.prismind_url = "http://localhost:8112"
        self.mock_settings.prismind_timeout = 30.0
        self.mock_settings.lexora_url = "http://localhost:8110"
        self.mock_settings.lexora_timeout = 30.0
        document._settings = self.mock_settings

    @pytest.mark.asyncio
    async def test_update_content_only(self):
        """Update content with no doc_type change should bypass type resolution."""
        with patch.object(document, "PrismindAdapter") as mock_adapter_class:
            mock_adapter = AsyncMock()
            mock_adapter.update_document = AsyncMock(
                return_value={
                    "success": True,
                    "doc_id": "doc-123",
                    "updated_fields": ["content"],
                    "message": "ok",
                }
            )
            mock_adapter_class.return_value = mock_adapter

            result = await document.smart_update_document_impl(
                settings=self.mock_settings,
                doc_id="doc-123",
                content="new content",
            )

            assert result["success"] is True
            assert result["doc_id"] == "doc-123"
            assert result["updated_fields"] == ["content"]
            assert result["matched_existing"] is False
            assert result["type_registered"] is False
            mock_adapter.list_document_types.assert_not_called()
            mock_adapter.update_document.assert_called_once()
            call_kwargs = mock_adapter.update_document.call_args.kwargs
            assert call_kwargs["doc_id"] == "doc-123"
            assert call_kwargs["content"] == "new content"
            assert "doc_type" not in call_kwargs

    @pytest.mark.asyncio
    async def test_update_with_known_doc_type(self):
        """Known doc_type should pass through without semantic match."""
        with patch.object(document, "PrismindAdapter") as mock_adapter_class:
            mock_adapter = AsyncMock()
            mock_adapter.list_document_types = AsyncMock(
                return_value=[{"type_id": "design"}, {"type_id": "api_spec"}]
            )
            mock_adapter.update_document = AsyncMock(
                return_value={
                    "success": True,
                    "doc_id": "doc-123",
                    "updated_fields": ["doc_type"],
                    "message": "moved",
                }
            )
            mock_adapter_class.return_value = mock_adapter

            result = await document.smart_update_document_impl(
                settings=self.mock_settings,
                doc_id="doc-123",
                doc_type="design",
            )

            assert result["success"] is True
            assert result["doc_type"] == "design"
            assert result["matched_existing"] is False
            assert result["type_registered"] is False
            # find_similar_document_type should not be called for exact match
            mock_adapter.find_similar_document_type.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_with_semantic_match(self):
        """Unknown doc_type should resolve via RAG semantic match."""
        with patch.object(document, "PrismindAdapter") as mock_adapter_class:
            mock_adapter = AsyncMock()
            mock_adapter.list_document_types = AsyncMock(
                return_value=[{"type_id": "api_spec"}]
            )
            mock_adapter.find_similar_document_type = AsyncMock(
                return_value={
                    "found": True,
                    "type_id": "api_spec",
                    "name": "API Spec",
                    "similarity": 0.82,
                }
            )
            mock_adapter.update_document = AsyncMock(
                return_value={
                    "success": True,
                    "doc_id": "doc-123",
                    "updated_fields": ["doc_type"],
                    "message": "ok",
                }
            )
            mock_adapter_class.return_value = mock_adapter

            result = await document.smart_update_document_impl(
                settings=self.mock_settings,
                doc_id="doc-123",
                doc_type="api仕様",
            )

            assert result["success"] is True
            assert result["matched_existing"] is True
            assert result["doc_type"] == "api_spec"
            assert result["type_registered"] is False
            # update_document was called with the resolved type, not the original
            call_kwargs = mock_adapter.update_document.call_args.kwargs
            assert call_kwargs["doc_type"] == "api_spec"

    @pytest.mark.asyncio
    async def test_update_registers_new_type_when_no_match(self):
        """Unknown doc_type with no semantic match should register a new type."""
        with patch.object(document, "PrismindAdapter") as mock_adapter_class, \
             patch.object(document, "LexoraAdapter") as mock_lexora_class, \
             patch.object(
                 document, "_generate_new_type_metadata", new_callable=AsyncMock
             ) as mock_gen:
            mock_adapter = AsyncMock()
            mock_adapter.list_document_types = AsyncMock(return_value=[])
            mock_adapter.find_similar_document_type = AsyncMock(
                return_value={"found": False}
            )
            mock_adapter.register_document_type = AsyncMock(
                return_value={"success": True}
            )
            mock_adapter.update_document = AsyncMock(
                return_value={
                    "success": True,
                    "doc_id": "doc-123",
                    "updated_fields": ["doc_type"],
                    "message": "ok",
                }
            )
            mock_adapter_class.return_value = mock_adapter
            mock_lexora_class.return_value = AsyncMock()
            mock_gen.return_value = {
                "type_id": "meeting_notes",
                "name": "Meeting Notes",
                "folder_name": "MeetingNotes",
                "description": "Meeting records",
            }

            result = await document.smart_update_document_impl(
                settings=self.mock_settings,
                doc_id="doc-123",
                doc_type="議事録",
            )

            assert result["success"] is True
            assert result["type_registered"] is True
            assert result["doc_type"] == "meeting_notes"
            assert result["registered_type"]["folder_name"] == "MeetingNotes"
            mock_adapter.register_document_type.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_propagates_failure_message(self):
        """update_document failure should be reflected in the response."""
        with patch.object(document, "PrismindAdapter") as mock_adapter_class:
            mock_adapter = AsyncMock()
            mock_adapter.update_document = AsyncMock(
                side_effect=RuntimeError("prismind boom")
            )
            mock_adapter_class.return_value = mock_adapter

            result = await document.smart_update_document_impl(
                settings=self.mock_settings,
                doc_id="doc-123",
                content="x",
            )

            assert result["success"] is False
            assert "prismind boom" in result["message"]

    @pytest.mark.asyncio
    async def test_update_skips_type_resolution_when_disabled(self):
        """auto_register_type=False should skip RAG/registration entirely."""
        with patch.object(document, "PrismindAdapter") as mock_adapter_class:
            mock_adapter = AsyncMock()
            mock_adapter.update_document = AsyncMock(
                return_value={
                    "success": True,
                    "doc_id": "doc-123",
                    "updated_fields": ["doc_type"],
                    "message": "ok",
                }
            )
            mock_adapter_class.return_value = mock_adapter

            result = await document.smart_update_document_impl(
                settings=self.mock_settings,
                doc_id="doc-123",
                doc_type="some_unknown_type",
                auto_register_type=False,
            )

            assert result["success"] is True
            mock_adapter.list_document_types.assert_not_called()
            mock_adapter.find_similar_document_type.assert_not_called()
            # doc_type passes through verbatim
            call_kwargs = mock_adapter.update_document.call_args.kwargs
            assert call_kwargs["doc_type"] == "some_unknown_type"

    @pytest.mark.asyncio
    async def test_update_append_mode(self):
        """append=True should be forwarded to the adapter."""
        with patch.object(document, "PrismindAdapter") as mock_adapter_class:
            mock_adapter = AsyncMock()
            mock_adapter.update_document = AsyncMock(
                return_value={
                    "success": True,
                    "doc_id": "doc-123",
                    "updated_fields": ["content"],
                    "message": "appended",
                }
            )
            mock_adapter_class.return_value = mock_adapter

            await document.smart_update_document_impl(
                settings=self.mock_settings,
                doc_id="doc-123",
                content="extra",
                append=True,
            )

            call_kwargs = mock_adapter.update_document.call_args.kwargs
            assert call_kwargs["append"] is True
            assert call_kwargs["content"] == "extra"
