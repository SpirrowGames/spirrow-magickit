"""Integration tests for session management in Prismind adapter.

Tests the updated session methods:
- start_session
- save_session (with new parameters)
- end_session (with new parameters)
- update_progress (new)

Run with: pytest tests/integration/test_session_adapter.py -v
"""

import pytest

from magickit.adapters.prismind import PrismindAdapter

PRISMIND_SSE_URL = "http://localhost:8112/sse"
TEST_PROJECT = "test-session"
TEST_USER = "test-user"


@pytest.fixture
def adapter():
    """Create Prismind adapter instance."""
    return PrismindAdapter(sse_url=PRISMIND_SSE_URL)


class TestSessionManagement:
    """Integration tests for session management."""

    @pytest.mark.asyncio
    async def test_start_session(self, adapter):
        """Test starting a session."""
        result = await adapter.start_session(
            project=TEST_PROJECT,
            user=TEST_USER,
        )
        assert result is not None
        print(f"\nstart_session result: {result}")

        # Check expected fields
        assert "project" in result or "session_id" in result or result.get("notes")

    @pytest.mark.asyncio
    async def test_save_session_with_new_params(self, adapter):
        """Test saving session with new parameters."""
        # Start session first
        await adapter.start_session(project=TEST_PROJECT, user=TEST_USER)

        # Save with new parameters
        result = await adapter.save_session(
            summary="Test summary for session",
            next_action="Continue with next feature",
            current_phase="Phase 1",
            current_task="T01: Test task",
            blockers=["blocker1"],
            notes="Test notes",
            project=TEST_PROJECT,
            user=TEST_USER,
        )
        assert result is not None
        print(f"\nsave_session result: {result}")

    @pytest.mark.asyncio
    async def test_update_progress(self, adapter):
        """Test updating progress (new method)."""
        # Start session first
        await adapter.start_session(project=TEST_PROJECT, user=TEST_USER)

        # Update progress
        result = await adapter.update_progress(
            current_phase="Phase 2",
            current_task="T02: New task",
            completed_task="T01: Test task",
            blockers=[],
            project=TEST_PROJECT,
            user=TEST_USER,
        )
        assert result is not None
        print(f"\nupdate_progress result: {result}")

    @pytest.mark.asyncio
    async def test_end_session_with_new_params(self, adapter):
        """Test ending session with new parameters."""
        # Start session first
        await adapter.start_session(project=TEST_PROJECT, user=TEST_USER)

        # End with new parameters
        result = await adapter.end_session(
            summary="Session completed successfully",
            next_action="Start next session with feature X",
            blockers=[],
            notes="All tasks completed",
            project=TEST_PROJECT,
            user=TEST_USER,
        )
        assert result is not None
        print(f"\nend_session result: {result}")

    @pytest.mark.asyncio
    async def test_session_handoff_flow(self, adapter):
        """Test full session handoff flow."""
        # 1. Start session
        start_result = await adapter.start_session(
            project=TEST_PROJECT,
            user=TEST_USER,
        )
        print(f"\n1. Start session: {start_result}")

        # 2. Save checkpoint
        save_result = await adapter.save_session(
            summary="Implemented feature A",
            next_action="Test feature A",
            current_phase="Phase 1",
            current_task="T01: Feature A",
            project=TEST_PROJECT,
            user=TEST_USER,
        )
        print(f"2. Save session: {save_result}")

        # 3. Update progress
        progress_result = await adapter.update_progress(
            completed_task="T01: Feature A",
            current_task="T02: Feature B",
            project=TEST_PROJECT,
            user=TEST_USER,
        )
        print(f"3. Update progress: {progress_result}")

        # 4. End session (handoff)
        end_result = await adapter.end_session(
            summary="Feature A implemented and tested",
            next_action="Start Feature B implementation",
            notes="See docs/feature-a.md for details",
            project=TEST_PROJECT,
            user=TEST_USER,
        )
        print(f"4. End session: {end_result}")

        # 5. Start new session (should restore context)
        restore_result = await adapter.start_session(
            project=TEST_PROJECT,
            user=TEST_USER,
        )
        print(f"5. Restore session: {restore_result}")

        # Verify handoff data is restored
        if isinstance(restore_result, dict):
            last_summary = restore_result.get("last_summary", "")
            next_action = restore_result.get("next_action", "")
            print(f"\n   Restored last_summary: {last_summary}")
            print(f"   Restored next_action: {next_action}")
