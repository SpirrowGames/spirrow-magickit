"""Integration tests for session management MCP tools.

Tests the orchestration layer tools:
- begin_task / resume
- checkpoint (with new parameters)
- handoff (with new parameters)
- update_progress (new)

Run with: pytest tests/integration/test_session_tools.py -v
"""

import pytest

from magickit.config import Settings
from magickit.mcp.tools import session

TEST_PROJECT = "test-session-tools"
TEST_USER = "test-user"


@pytest.fixture
def settings():
    """Create test settings."""
    return Settings(
        prismind_url="http://localhost:8112/sse",
        cognilens_url="http://localhost:8113/sse",
        lexora_url="http://localhost:8111/sse",
    )


@pytest.fixture
def mock_mcp():
    """Create a mock MCP instance for tool registration."""
    class MockMCP:
        def __init__(self):
            self.tools = {}

        def tool(self):
            def decorator(func):
                self.tools[func.__name__] = func
                return func
            return decorator

    return MockMCP()


class TestSessionTools:
    """Integration tests for session MCP tools."""

    @pytest.mark.asyncio
    async def test_begin_task(self, settings, mock_mcp):
        """Test begin_task tool."""
        session.register_tools(mock_mcp, settings)

        result = await mock_mcp.tools["begin_task"](
            project=TEST_PROJECT,
            task_description="Test session tools",
            max_tokens=500,
            user=TEST_USER,
        )

        assert result is not None
        assert "project" in result
        assert result["project"] == TEST_PROJECT
        print(f"\nbegin_task result: {result}")

    @pytest.mark.asyncio
    async def test_checkpoint_with_new_params(self, settings, mock_mcp):
        """Test checkpoint tool with new parameters."""
        session.register_tools(mock_mcp, settings)

        # Start task first
        await mock_mcp.tools["begin_task"](
            project=TEST_PROJECT,
            user=TEST_USER,
        )

        # Checkpoint with new parameters
        result = await mock_mcp.tools["checkpoint"](
            summary="Implemented feature X",
            project=TEST_PROJECT,
            decisions=["Use pattern A"],
            blockers=[],
            current_phase="Phase 2",
            current_task="T01: Feature X",
            next_action="Test feature X",
            auto_extract=False,
            user=TEST_USER,
        )

        assert result is not None
        assert result.get("success") is True
        print(f"\ncheckpoint result: {result}")

    @pytest.mark.asyncio
    async def test_update_progress(self, settings, mock_mcp):
        """Test update_progress tool (new)."""
        session.register_tools(mock_mcp, settings)

        # Start task first
        await mock_mcp.tools["begin_task"](
            project=TEST_PROJECT,
            user=TEST_USER,
        )

        # Update progress
        result = await mock_mcp.tools["update_progress"](
            project=TEST_PROJECT,
            current_phase="Phase 2",
            current_task="T02: Next task",
            completed_task="T01: Feature X",
            blockers=[],
            user=TEST_USER,
        )

        assert result is not None
        assert result.get("success") is True
        print(f"\nupdate_progress result: {result}")

    @pytest.mark.asyncio
    async def test_handoff_with_new_params(self, settings, mock_mcp):
        """Test handoff tool with new parameters."""
        session.register_tools(mock_mcp, settings)

        # Start task first
        await mock_mcp.tools["begin_task"](
            project=TEST_PROJECT,
            user=TEST_USER,
        )

        # Handoff with new summary parameter
        result = await mock_mcp.tools["handoff"](
            next_action="Continue with feature Y",
            project=TEST_PROJECT,
            summary="Feature X completed successfully",
            notes="All tests passing",
            blockers=[],
            save_insights=False,
            user=TEST_USER,
        )

        assert result is not None
        assert result.get("success") is True
        print(f"\nhandoff result: {result}")

    @pytest.mark.asyncio
    async def test_resume_restores_handoff(self, settings, mock_mcp):
        """Test that resume restores handoff data."""
        session.register_tools(mock_mcp, settings)

        # First, do a handoff
        await mock_mcp.tools["begin_task"](
            project=TEST_PROJECT,
            user=TEST_USER,
        )

        await mock_mcp.tools["handoff"](
            next_action="Implement feature Z",
            project=TEST_PROJECT,
            summary="Features X and Y completed",
            notes="Ready for feature Z",
            save_insights=False,
            user=TEST_USER,
        )

        # Now resume
        result = await mock_mcp.tools["resume"](
            project=TEST_PROJECT,
            detail_level="standard",
            user=TEST_USER,
        )

        assert result is not None
        print(f"\nresume result: {result}")

        # Check if handoff data is restored
        if isinstance(result, dict):
            print(f"  last_summary: {result.get('last_summary', 'N/A')}")
            print(f"  next_action: {result.get('next_action', 'N/A')}")

    @pytest.mark.asyncio
    async def test_full_session_flow(self, settings, mock_mcp):
        """Test complete session flow with all tools."""
        session.register_tools(mock_mcp, settings)

        print("\n=== Full Session Flow Test ===")

        # 1. Begin task
        begin_result = await mock_mcp.tools["begin_task"](
            project=TEST_PROJECT,
            task_description="Full flow test",
            user=TEST_USER,
        )
        print(f"1. begin_task: success")

        # 2. Checkpoint
        checkpoint_result = await mock_mcp.tools["checkpoint"](
            summary="Started implementation",
            project=TEST_PROJECT,
            current_phase="Phase 1",
            current_task="T01: Setup",
            next_action="Implement core logic",
            auto_extract=False,
            user=TEST_USER,
        )
        print(f"2. checkpoint: {checkpoint_result.get('success')}")

        # 3. Update progress
        progress_result = await mock_mcp.tools["update_progress"](
            project=TEST_PROJECT,
            completed_task="T01: Setup",
            current_task="T02: Core logic",
            user=TEST_USER,
        )
        print(f"3. update_progress: {progress_result.get('success')}")

        # 4. Handoff
        handoff_result = await mock_mcp.tools["handoff"](
            next_action="Add tests for core logic",
            project=TEST_PROJECT,
            summary="Core logic implemented",
            notes="See src/core.py",
            save_insights=False,
            user=TEST_USER,
        )
        print(f"4. handoff: {handoff_result.get('success')}")

        # 5. Resume (new session)
        resume_result = await mock_mcp.tools["resume"](
            project=TEST_PROJECT,
            detail_level="standard",
            user=TEST_USER,
        )
        print(f"5. resume: success")
        print(f"   Restored last_summary: {resume_result.get('last_summary', 'N/A')}")
        print(f"   Restored next_action: {resume_result.get('next_action', 'N/A')}")

        assert all([
            begin_result,
            checkpoint_result.get("success"),
            progress_result.get("success"),
            handoff_result.get("success"),
            resume_result,
        ])


async def _require_author_capable_prismind(mock_mcp):
    """Skip if the live Prismind has not been redeployed with author support.

    The context-author feature needs the updated spirrow-prismind service; an
    older running service does not expose list_context_authors. These
    integration tests auto-activate once Prismind is redeployed.
    """
    probe = await mock_mcp.tools["list_context_authors"](
        project="test-context-author-capability-probe",
    )
    if not probe.get("success"):
        pytest.skip(
            "live spirrow-prismind lacks list_context_authors "
            f"(redeploy required): {probe.get('message')}"
        )


class TestContextAuthorPartition:
    """Integration tests for context-author partitioned contexts."""

    @pytest.mark.asyncio
    async def test_author_isolation_and_listing(self, settings, mock_mcp):
        """Two authors keep isolated contexts; list_context_authors sees both."""
        session.register_tools(mock_mcp, settings)
        await _require_author_capable_prismind(mock_mcp)
        project = "test-context-author"

        await mock_mcp.tools["checkpoint"](
            summary="architect context",
            project=project,
            current_task="T-arch",
            auto_extract=False,
            user=TEST_USER,
            author="claude.ai",
        )
        await mock_mcp.tools["checkpoint"](
            summary="implementer context",
            project=project,
            current_task="T-impl",
            auto_extract=False,
            user=TEST_USER,
            author="claude-code",
        )

        # Each author resumes its own context
        arch = await mock_mcp.tools["resume"](
            project=project, user=TEST_USER, author="claude.ai",
        )
        impl = await mock_mcp.tools["resume"](
            project=project, user=TEST_USER, author="claude-code",
        )
        assert arch.get("author") == "claude.ai"
        assert impl.get("author") == "claude-code"

        # list_context_authors surfaces both
        listed = await mock_mcp.tools["list_context_authors"](
            project=project, user=TEST_USER,
        )
        assert listed["success"] is True
        authors = {a.get("author") for a in listed["authors"]}
        assert {"claude.ai", "claude-code"}.issubset(authors)
        print(f"\nlist_context_authors: {listed}")

    @pytest.mark.asyncio
    async def test_list_context_authors_empty_project(self, settings, mock_mcp):
        """list_context_authors returns success with no authors for unknown project."""
        session.register_tools(mock_mcp, settings)
        await _require_author_capable_prismind(mock_mcp)
        result = await mock_mcp.tools["list_context_authors"](
            project="test-context-author-nonexistent-xyz",
            user=TEST_USER,
        )
        assert result["success"] is True
        assert result["total_count"] == 0
