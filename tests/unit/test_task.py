"""Tests for task management tools."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from magickit.mcp.tools import task


class TestHelperFunctions:
    """Tests for helper functions."""

    def test_extract_tasks_from_progress(self):
        """Test extracting flat task list from progress."""
        progress = {
            "phases": [
                {
                    "phase": "Phase 1",
                    "tasks": [
                        {"task_id": "T01", "name": "Task 1", "status": "completed"},
                        {"task_id": "T02", "name": "Task 2", "status": "in_progress"},
                    ],
                },
                {
                    "phase": "Phase 2",
                    "tasks": [
                        {"task_id": "T03", "name": "Task 3", "status": "not_started"},
                    ],
                },
            ]
        }

        tasks = task._extract_tasks_from_progress(progress)

        assert len(tasks) == 3
        assert tasks[0]["phase"] == "Phase 1"
        assert tasks[2]["phase"] == "Phase 2"

    def test_generate_next_task_id(self):
        """Test task ID generation."""
        tasks = [
            {"task_id": "T01"},
            {"task_id": "T02"},
            {"task_id": "T05"},
        ]

        next_id = task._generate_next_task_id(tasks)

        assert next_id == "T06"

    def test_generate_next_task_id_empty(self):
        """Test task ID generation with no existing tasks."""
        next_id = task._generate_next_task_id([])

        assert next_id == "T01"

    def test_smart_sort_tasks(self):
        """Test smart sorting of tasks."""
        tasks = [
            {"task_id": "T01", "status": "blocked", "priority": "high"},
            {"task_id": "T02", "status": "not_started", "priority": "low"},
            {"task_id": "T03", "status": "not_started", "priority": "high"},
            {"task_id": "T04", "status": "not_started", "priority": "medium", "blocked_by": ["T01"]},
        ]

        sorted_tasks = task._smart_sort_tasks(tasks)

        # High priority, not blocked, no blockers should be first
        assert sorted_tasks[0]["task_id"] == "T03"
        # Blocked task should be last
        assert sorted_tasks[-1]["task_id"] == "T01"

    def test_find_recommended_task(self):
        """Test finding recommended task."""
        tasks = [
            {"task_id": "T01", "status": "completed", "priority": "high"},
            {"task_id": "T02", "status": "not_started", "priority": "low"},
            {"task_id": "T03", "status": "not_started", "priority": "high", "blocked_by": ["T01"]},
            {"task_id": "T04", "status": "not_started", "priority": "medium", "blocked_by": ["T99"]},
        ]

        recommended = task._find_recommended_task(tasks)

        # T03 has high priority and T01 is completed
        assert recommended["task_id"] == "T03"

    def test_find_recommended_task_no_candidates(self):
        """Test when no task can be recommended."""
        tasks = [
            {"task_id": "T01", "status": "completed"},
            {"task_id": "T02", "status": "in_progress"},
        ]

        recommended = task._find_recommended_task(tasks)

        assert recommended is None

    def test_find_tasks_blocked_by(self):
        """Test finding tasks blocked by a specific task."""
        tasks = [
            {"task_id": "T01", "blocked_by": []},
            {"task_id": "T02", "blocked_by": ["T01"]},
            {"task_id": "T03", "blocked_by": ["T01", "T02"]},
            {"task_id": "T04", "blocked_by": ["T02"]},
        ]

        blocked = task._find_tasks_blocked_by(tasks, "T01")

        assert len(blocked) == 2
        blocked_ids = {t["task_id"] for t in blocked}
        assert blocked_ids == {"T02", "T03"}

    def test_calculate_stats(self):
        """Test task statistics calculation."""
        tasks = [
            {"task_id": "T01", "status": "completed"},
            {"task_id": "T02", "status": "completed"},
            {"task_id": "T03", "status": "in_progress"},
            {"task_id": "T04", "status": "blocked"},
            {"task_id": "T05", "status": "not_started"},
        ]

        stats = task._calculate_stats(tasks)

        assert stats["total"] == 5
        assert stats["completed"] == 2
        assert stats["in_progress"] == 1
        assert stats["blocked"] == 1
        assert stats["not_started"] == 1


class TestAddTask:
    """Tests for add_task tool."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Setup test fixtures."""
        self.mock_settings = MagicMock()
        self.mock_settings.prismind_url = "http://localhost:8112"
        self.mock_settings.prismind_timeout = 30.0
        task._settings = self.mock_settings

    @pytest.mark.asyncio
    async def test_add_task_success(self):
        """Test successful task addition."""
        with patch.object(task, "PrismindAdapter") as mock_adapter_class:
            mock_adapter = AsyncMock()
            mock_adapter.get_progress = AsyncMock(return_value={
                "current_phase": "Phase 1",
                "phases": [{"phase": "Phase 1", "tasks": []}],
            })
            mock_adapter.search_knowledge = AsyncMock(return_value=[])
            mock_adapter.add_task = AsyncMock(return_value={
                "success": True,
                "message": "Task added",
            })
            mock_adapter.add_knowledge = AsyncMock(return_value={"success": True})
            mock_adapter_class.return_value = mock_adapter

            result = await task.add_task_impl(
                settings=self.mock_settings,
                name="New Task",
                description="Description",
            )

            assert result["success"] is True
            assert result["task_id"] == "T01"
            assert result["phase"] == "Phase 1"
            mock_adapter.add_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_add_task_auto_id_generation(self):
        """Test automatic task ID generation."""
        with patch.object(task, "PrismindAdapter") as mock_adapter_class:
            mock_adapter = AsyncMock()
            mock_adapter.get_progress = AsyncMock(return_value={
                "current_phase": "Phase 1",
                "phases": [{
                    "phase": "Phase 1",
                    "tasks": [
                        {"task_id": "T01"},
                        {"task_id": "T02"},
                    ],
                }],
            })
            mock_adapter.search_knowledge = AsyncMock(return_value=[])
            mock_adapter.add_task = AsyncMock(return_value={"success": True})
            mock_adapter.add_knowledge = AsyncMock(return_value={"success": True})
            mock_adapter_class.return_value = mock_adapter

            result = await task.add_task_impl(
                settings=self.mock_settings,
                name="New Task",
            )

            assert result["task_id"] == "T03"

    @pytest.mark.asyncio
    async def test_add_task_duplicate_warning(self):
        """Test duplicate task detection."""
        with patch.object(task, "PrismindAdapter") as mock_adapter_class:
            mock_adapter = AsyncMock()
            mock_adapter.get_progress = AsyncMock(return_value={
                "current_phase": "Phase 1",
                "phases": [],
            })
            mock_adapter.search_knowledge = AsyncMock(return_value=[
                {"content": "Similar task", "score": 0.9}
            ])
            mock_adapter.add_task = AsyncMock(return_value={"success": True})
            mock_adapter.add_knowledge = AsyncMock(return_value={"success": True})
            mock_adapter_class.return_value = mock_adapter

            result = await task.add_task_impl(
                settings=self.mock_settings,
                name="Similar task",
            )

            assert result["success"] is True
            assert len(result["warnings"]) > 0
            assert "Similar task found" in result["warnings"][0]

    @pytest.mark.asyncio
    async def test_add_task_invalid_dependency(self):
        """Test validation of blocked_by task IDs."""
        with patch.object(task, "PrismindAdapter") as mock_adapter_class:
            mock_adapter = AsyncMock()
            mock_adapter.get_progress = AsyncMock(return_value={
                "current_phase": "Phase 1",
                "phases": [{
                    "phase": "Phase 1",
                    "tasks": [{"task_id": "T01"}],
                }],
            })
            mock_adapter_class.return_value = mock_adapter

            result = await task.add_task_impl(
                settings=self.mock_settings,
                name="New Task",
                blocked_by=["T99"],  # Non-existent
            )

            assert result["success"] is False
            assert "Invalid blocked_by" in result["error"]


class TestListTasks:
    """Tests for list_tasks tool."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Setup test fixtures."""
        self.mock_settings = MagicMock()
        self.mock_settings.prismind_url = "http://localhost:8112"
        self.mock_settings.prismind_timeout = 30.0
        task._settings = self.mock_settings

    @pytest.mark.asyncio
    async def test_list_tasks_success(self):
        """Test successful task listing."""
        with patch.object(task, "PrismindAdapter") as mock_adapter_class:
            mock_adapter = AsyncMock()
            mock_adapter.get_progress = AsyncMock(return_value={
                "current_phase": "Phase 1",
                "project": "test-project",
                "phases": [{
                    "phase": "Phase 1",
                    "tasks": [
                        {"task_id": "T01", "status": "completed", "priority": "high"},
                        {"task_id": "T02", "status": "not_started", "priority": "high"},
                    ],
                }],
            })
            mock_adapter_class.return_value = mock_adapter

            result = await task.list_tasks_impl(settings=self.mock_settings)

            assert result["success"] is True
            assert len(result["tasks"]) == 2
            assert result["stats"]["total"] == 2
            assert result["recommended"] is not None

    @pytest.mark.asyncio
    async def test_list_tasks_with_filter(self):
        """Test task listing with status filter."""
        with patch.object(task, "PrismindAdapter") as mock_adapter_class:
            mock_adapter = AsyncMock()
            mock_adapter.get_progress = AsyncMock(return_value={
                "current_phase": "Phase 1",
                "phases": [{
                    "phase": "Phase 1",
                    "tasks": [
                        {"task_id": "T01", "status": "completed"},
                        {"task_id": "T02", "status": "not_started"},
                    ],
                }],
            })
            mock_adapter_class.return_value = mock_adapter

            result = await task.list_tasks_impl(
                settings=self.mock_settings,
                status="not_started",
            )

            assert result["success"] is True
            assert len(result["tasks"]) == 1
            assert result["tasks"][0]["task_id"] == "T02"


class TestStartTask:
    """Tests for start_task tool."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Setup test fixtures."""
        self.mock_settings = MagicMock()
        self.mock_settings.prismind_url = "http://localhost:8112"
        self.mock_settings.prismind_timeout = 30.0
        task._settings = self.mock_settings

    @pytest.mark.asyncio
    async def test_start_task_success(self):
        """Test successful task start."""
        with patch.object(task, "PrismindAdapter") as mock_adapter_class:
            mock_adapter = AsyncMock()
            mock_adapter.get_progress = AsyncMock(return_value={
                "phases": [{
                    "phase": "Phase 1",
                    "tasks": [
                        {"task_id": "T01", "name": "Task 1", "status": "not_started"},
                    ],
                }],
            })
            mock_adapter.start_task = AsyncMock(return_value={
                "success": True,
                "message": "Task started",
            })
            mock_adapter.search_knowledge = AsyncMock(return_value=[])
            mock_adapter_class.return_value = mock_adapter

            result = await task.start_task_impl(
                settings=self.mock_settings,
                task_id="T01",
            )

            assert result["success"] is True
            mock_adapter.start_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_start_task_dependency_not_met(self):
        """Test starting task with incomplete dependencies."""
        with patch.object(task, "PrismindAdapter") as mock_adapter_class:
            mock_adapter = AsyncMock()
            mock_adapter.get_progress = AsyncMock(return_value={
                "phases": [{
                    "phase": "Phase 1",
                    "tasks": [
                        {"task_id": "T01", "status": "not_started"},
                        {"task_id": "T02", "status": "not_started", "blocked_by": ["T01"]},
                    ],
                }],
            })
            mock_adapter_class.return_value = mock_adapter

            result = await task.start_task_impl(
                settings=self.mock_settings,
                task_id="T02",
            )

            assert result["success"] is False
            assert "Dependencies not completed" in result["error"]

    @pytest.mark.asyncio
    async def test_start_task_force_with_incomplete_deps(self):
        """Test force starting task with incomplete dependencies."""
        with patch.object(task, "PrismindAdapter") as mock_adapter_class:
            mock_adapter = AsyncMock()
            mock_adapter.get_progress = AsyncMock(return_value={
                "phases": [{
                    "phase": "Phase 1",
                    "tasks": [
                        {"task_id": "T01", "name": "Task 1", "status": "not_started"},
                        {"task_id": "T02", "name": "Task 2", "status": "not_started", "blocked_by": ["T01"]},
                    ],
                }],
            })
            mock_adapter.start_task = AsyncMock(return_value={"success": True})
            mock_adapter.search_knowledge = AsyncMock(return_value=[])
            mock_adapter_class.return_value = mock_adapter

            result = await task.start_task_impl(
                settings=self.mock_settings,
                task_id="T02",
                force=True,
            )

            assert result["success"] is True
            assert len(result["warnings"]) > 0


class TestCompleteTask:
    """Tests for complete_task tool."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Setup test fixtures."""
        self.mock_settings = MagicMock()
        self.mock_settings.prismind_url = "http://localhost:8112"
        self.mock_settings.prismind_timeout = 30.0
        task._settings = self.mock_settings

    @pytest.mark.asyncio
    async def test_complete_task_success(self):
        """Test successful task completion."""
        with patch.object(task, "PrismindAdapter") as mock_adapter_class:
            mock_adapter = AsyncMock()
            mock_adapter.get_progress = AsyncMock(return_value={
                "phases": [{
                    "phase": "Phase 1",
                    "tasks": [
                        {"task_id": "T01", "name": "Task 1", "status": "in_progress"},
                        {"task_id": "T02", "status": "not_started", "blocked_by": ["T01"]},
                    ],
                }],
            })
            mock_adapter.complete_task = AsyncMock(return_value={
                "success": True,
                "message": "Task completed",
            })
            mock_adapter.add_knowledge = AsyncMock(return_value={"success": True})
            mock_adapter_class.return_value = mock_adapter

            result = await task.complete_task_impl(
                settings=self.mock_settings,
                task_id="T01",
                notes="Done",
                learnings="Learned something",
            )

            assert result["success"] is True
            assert len(result["newly_unblocked"]) == 1
            assert result["newly_unblocked"][0]["task_id"] == "T02"

    @pytest.mark.asyncio
    async def test_complete_task_records_learnings(self):
        """Test that learnings are recorded as knowledge."""
        with patch.object(task, "PrismindAdapter") as mock_adapter_class:
            mock_adapter = AsyncMock()
            mock_adapter.get_progress = AsyncMock(return_value={
                "phases": [{
                    "phase": "Phase 1",
                    "tasks": [
                        {"task_id": "T01", "name": "Task 1", "status": "in_progress"},
                    ],
                }],
            })
            mock_adapter.complete_task = AsyncMock(return_value={"success": True})
            mock_adapter.add_knowledge = AsyncMock(return_value={"success": True})
            mock_adapter_class.return_value = mock_adapter

            await task.complete_task_impl(
                settings=self.mock_settings,
                task_id="T01",
                learnings="Important learning",
            )

            mock_adapter.add_knowledge.assert_called_once()
            call_kwargs = mock_adapter.add_knowledge.call_args.kwargs
            assert "Important learning" in call_kwargs["content"]
            assert call_kwargs["category"] == "task_completion"


class TestBlockTask:
    """Tests for block_task tool."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Setup test fixtures."""
        self.mock_settings = MagicMock()
        self.mock_settings.prismind_url = "http://localhost:8112"
        self.mock_settings.prismind_timeout = 30.0
        task._settings = self.mock_settings

    @pytest.mark.asyncio
    async def test_block_task_success(self):
        """Test successful task blocking."""
        with patch.object(task, "PrismindAdapter") as mock_adapter_class:
            mock_adapter = AsyncMock()
            mock_adapter.get_progress = AsyncMock(return_value={
                "phases": [{
                    "phase": "Phase 1",
                    "tasks": [
                        {"task_id": "T01", "name": "Task 1", "status": "in_progress"},
                    ],
                }],
            })
            mock_adapter.block_task = AsyncMock(return_value={
                "success": True,
                "message": "Task blocked",
            })
            mock_adapter.add_knowledge = AsyncMock(return_value={"success": True})
            mock_adapter_class.return_value = mock_adapter

            result = await task.block_task_impl(
                settings=self.mock_settings,
                task_id="T01",
                reason="Waiting for API",
            )

            assert result["success"] is True
            assert result["reason"] == "Waiting for API"

    @pytest.mark.asyncio
    async def test_block_task_impact_analysis(self):
        """Test impact analysis when blocking a task."""
        with patch.object(task, "PrismindAdapter") as mock_adapter_class:
            mock_adapter = AsyncMock()
            mock_adapter.get_progress = AsyncMock(return_value={
                "phases": [{
                    "phase": "Phase 1",
                    "tasks": [
                        {"task_id": "T01", "name": "Task 1", "status": "in_progress"},
                        {"task_id": "T02", "blocked_by": ["T01"]},
                        {"task_id": "T03", "blocked_by": ["T02"]},
                    ],
                }],
            })
            mock_adapter.block_task = AsyncMock(return_value={"success": True})
            mock_adapter.add_knowledge = AsyncMock(return_value={"success": True})
            mock_adapter_class.return_value = mock_adapter

            result = await task.block_task_impl(
                settings=self.mock_settings,
                task_id="T01",
                reason="Issue found",
            )

            assert result["success"] is True
            assert len(result["directly_impacted"]) == 1
            assert len(result["cascade_impact"]) == 1
            assert result["total_impacted"] == 2


class TestRegisterTools:
    """Tests for tool registration."""

    def test_register_tools(self):
        """Test that tools are registered correctly."""
        mock_mcp = MagicMock()
        mock_settings = MagicMock()

        task.register_tools(mock_mcp, mock_settings)

        # Should register 5 tools
        assert mock_mcp.tool.call_count == 5
