"""Unit tests for notification system."""

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from magickit.adapters.discord import DiscordAdapter
from magickit.adapters.slack import SlackAdapter
from magickit.api.models import EventType, WebhookService
from magickit.core.notification_manager import NotificationManager
from magickit.core.state_manager import StateManager


@pytest_asyncio.fixture
async def state_manager(tmp_path):
    """Create a state manager with temporary database."""
    db_path = str(tmp_path / "test.db")
    manager = StateManager(db_path=db_path)
    await manager.initialize()

    # Run Phase 2 migrations
    from magickit.core.migrations import MigrationManager
    migration_manager = MigrationManager(db_path=db_path)
    await migration_manager.migrate()

    yield manager
    await manager.close()


@pytest_asyncio.fixture
async def notification_manager(state_manager):
    """Create a notification manager."""
    return NotificationManager(state_manager)


class TestSlackAdapter:
    """Tests for Slack adapter."""

    def test_init(self):
        """Test Slack adapter initialization."""
        adapter = SlackAdapter(
            webhook_url="https://hooks.slack.com/services/xxx",
            timeout=10.0,
            max_retries=3,
        )
        assert adapter.webhook_url == "https://hooks.slack.com/services/xxx"
        assert adapter.max_retries == 3

    @pytest.mark.asyncio
    async def test_health_check(self):
        """Test Slack adapter health check."""
        adapter = SlackAdapter(webhook_url="https://hooks.slack.com/services/xxx")
        assert await adapter.health_check() is True

        empty_adapter = SlackAdapter(webhook_url="")
        assert await empty_adapter.health_check() is False

    def test_format_message(self):
        """Test Slack message formatting."""
        adapter = SlackAdapter(webhook_url="https://example.com")
        message = adapter._format_message(
            event_type=EventType.COMPLETED,
            task_id="task-123",
            task_name="Test Task",
            project_name="Test Project",
            details={"result": "success"},
        )

        assert "attachments" in message
        assert len(message["attachments"]) == 1
        assert "blocks" in message["attachments"][0]

    def test_get_event_emoji(self):
        """Test event emoji mapping."""
        adapter = SlackAdapter(webhook_url="https://example.com")

        assert adapter._get_event_emoji(EventType.COMPLETED) == ":white_check_mark:"
        assert adapter._get_event_emoji(EventType.FAILED) == ":x:"
        assert adapter._get_event_emoji(EventType.STARTED) == ":arrow_forward:"

    def test_get_event_color(self):
        """Test event color mapping."""
        adapter = SlackAdapter(webhook_url="https://example.com")

        assert adapter._get_event_color(EventType.COMPLETED) == "#4CAF50"  # Green
        assert adapter._get_event_color(EventType.FAILED) == "#f44336"  # Red


class TestDiscordAdapter:
    """Tests for Discord adapter."""

    def test_init(self):
        """Test Discord adapter initialization."""
        adapter = DiscordAdapter(
            webhook_url="https://discord.com/api/webhooks/xxx",
            timeout=10.0,
            max_retries=3,
            bot_name="TestBot",
        )
        assert adapter.webhook_url == "https://discord.com/api/webhooks/xxx"
        assert adapter.bot_name == "TestBot"

    @pytest.mark.asyncio
    async def test_health_check(self):
        """Test Discord adapter health check."""
        adapter = DiscordAdapter(webhook_url="https://discord.com/api/webhooks/xxx")
        assert await adapter.health_check() is True

    def test_format_message(self):
        """Test Discord message formatting."""
        adapter = DiscordAdapter(
            webhook_url="https://example.com",
            bot_name="Magickit",
        )
        message = adapter._format_message(
            event_type=EventType.FAILED,
            task_id="task-456",
            task_name="Failed Task",
            project_name="Test Project",
            details={"error": "Something went wrong"},
        )

        assert "username" in message
        assert message["username"] == "Magickit"
        assert "embeds" in message
        assert len(message["embeds"]) == 1
        assert "fields" in message["embeds"][0]

    def test_get_event_color_int(self):
        """Test event color is returned as integer for Discord."""
        adapter = DiscordAdapter(webhook_url="https://example.com")

        color = adapter._get_event_color(EventType.COMPLETED)
        assert isinstance(color, int)
        assert color == 0x4CAF50


class TestNotificationManager:
    """Tests for notification manager."""

    @pytest.mark.asyncio
    async def test_notify_no_webhooks(self, notification_manager):
        """Test notify when no webhooks are registered."""
        results = await notification_manager.notify(
            workspace_id="workspace-1",
            event_type=EventType.COMPLETED,
            task_id="task-1",
            task_name="Test Task",
            background=False,
        )

        # Should return empty list when no webhooks
        assert results == []

    @pytest.mark.asyncio
    async def test_notify_with_webhook(self, state_manager, notification_manager):
        """Test notify with a registered webhook."""
        # Create a workspace first
        await state_manager.create_workspace(
            workspace_id="workspace-1",
            name="Test Workspace",
        )

        # Register a webhook
        await state_manager.create_webhook(
            webhook_id="webhook-1",
            workspace_id="workspace-1",
            service=WebhookService.SLACK,
            url="https://hooks.slack.com/services/test",
            events=[EventType.COMPLETED],
        )

        # Mock the HTTP client
        with patch.object(SlackAdapter, "_send_webhook", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = True

            results = await notification_manager.notify(
                workspace_id="workspace-1",
                event_type=EventType.COMPLETED,
                task_id="task-1",
                task_name="Test Task",
                background=False,
            )

            assert len(results) == 1
            assert results[0] is True

    @pytest.mark.asyncio
    async def test_create_adapter_slack(self, notification_manager):
        """Test creating Slack adapter."""
        adapter = notification_manager._create_adapter(
            WebhookService.SLACK,
            "https://hooks.slack.com/services/xxx",
        )
        assert isinstance(adapter, SlackAdapter)

    @pytest.mark.asyncio
    async def test_create_adapter_discord(self, notification_manager):
        """Test creating Discord adapter."""
        adapter = notification_manager._create_adapter(
            WebhookService.DISCORD,
            "https://discord.com/api/webhooks/xxx",
        )
        assert isinstance(adapter, DiscordAdapter)

    @pytest.mark.asyncio
    async def test_test_webhook(self, notification_manager):
        """Test webhook testing functionality."""
        from magickit.api.models import WebhookResponse
        from datetime import datetime

        webhook = WebhookResponse(
            id="webhook-1",
            workspace_id="workspace-1",
            service=WebhookService.SLACK,
            url="https://hooks.slack.com/services/test",
            events=[EventType.CREATED],
            active=True,
            created_at=datetime.now(),
        )

        with patch.object(SlackAdapter, "send_notification", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = True

            result = await notification_manager.test_webhook(webhook)
            assert result is True
            mock_send.assert_called_once()

    @pytest.mark.asyncio
    async def test_webhook_event_filtering(self, state_manager, notification_manager):
        """Test that webhooks only receive events they subscribed to."""
        # Create workspace and webhook
        await state_manager.create_workspace(
            workspace_id="workspace-2",
            name="Test Workspace 2",
        )

        # Only subscribe to COMPLETED events
        await state_manager.create_webhook(
            webhook_id="webhook-2",
            workspace_id="workspace-2",
            service=WebhookService.DISCORD,
            url="https://discord.com/api/webhooks/test",
            events=[EventType.COMPLETED],
        )

        # Send a STARTED event - should not trigger webhook
        with patch.object(DiscordAdapter, "_send_webhook", new_callable=AsyncMock) as mock_send:
            results = await notification_manager.notify(
                workspace_id="workspace-2",
                event_type=EventType.STARTED,  # Not subscribed
                task_id="task-1",
                task_name="Test Task",
                background=False,
            )

            # No webhooks should be triggered
            assert results == []
            mock_send.assert_not_called()
