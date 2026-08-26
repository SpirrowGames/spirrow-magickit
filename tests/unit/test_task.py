"""Tests for task management tools."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from magickit.adapters.cognilens import CognilensError
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


class TestAddTaskRace:
    """Verify the L1 lock prevents task_id duplication under concurrent add_task.

    Regression guard for the T173 incident: 21 occurrences of the same
    auto-assigned task_id across four phases when parallel add_task calls
    all read the same get_progress snapshot. The fix is the per-project
    asyncio.Lock in add_task_impl serializing get_progress -> add_task.
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        self.mock_settings = MagicMock()
        self.mock_settings.prismind_url = "http://localhost:8112"
        self.mock_settings.prismind_timeout = 30.0
        task._settings = self.mock_settings
        # Isolate from other tests that may have populated the lock dict.
        task._project_add_locks.clear()

    @pytest.mark.asyncio
    async def test_concurrent_add_task_no_duplicate_ids(self):
        """50 concurrent add_task calls produce 50 distinct task_ids."""
        added_tasks: list[dict] = []
        sheet_lock = asyncio.Lock()

        async def fake_get_progress(**kwargs):
            # A small await encourages the scheduler to interleave calls,
            # surfacing the race if the L1 lock were removed.
            await asyncio.sleep(0)
            return {
                "current_phase": "Phase 1",
                "phases": [{"phase": "Phase 1", "tasks": list(added_tasks)}],
            }

        async def fake_add_task(**kwargs):
            await asyncio.sleep(0)
            async with sheet_lock:
                added_tasks.append({"task_id": kwargs["task_id"]})
            return {"success": True, "message": "ok"}

        with patch.object(task, "PrismindAdapter") as mock_adapter_class:
            mock_adapter = AsyncMock()
            mock_adapter.get_progress = fake_get_progress
            mock_adapter.search_knowledge = AsyncMock(return_value=[])
            mock_adapter.add_task = fake_add_task
            mock_adapter.add_knowledge = AsyncMock(return_value={"success": True})
            mock_adapter_class.return_value = mock_adapter

            num = 50
            results = await asyncio.gather(*[
                task.add_task_impl(
                    settings=self.mock_settings,
                    name=f"Task {i}",
                    project="race_test",
                )
                for i in range(num)
            ])

        assigned_ids = [r["task_id"] for r in results if r.get("success")]
        assert len(assigned_ids) == num, (
            f"Expected {num} successful adds, got {len(assigned_ids)}: {results}"
        )
        assert len(set(assigned_ids)) == num, (
            f"Duplicate task_ids detected: {sorted(assigned_ids)}"
        )


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


class TestGetTask:
    """Tests for get_task tool."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Setup test fixtures."""
        self.mock_settings = MagicMock()
        self.mock_settings.prismind_url = "http://localhost:8112"
        self.mock_settings.prismind_timeout = 30.0
        task._settings = self.mock_settings

    @pytest.mark.asyncio
    async def test_get_task_success(self):
        """Test successful task retrieval."""
        with patch.object(task, "PrismindAdapter") as mock_adapter_class:
            mock_adapter = AsyncMock()
            mock_adapter.call = AsyncMock(return_value={
                "success": True,
                "task": {
                    "task_id": "T01",
                    "name": "Task 1",
                    "status": "in_progress",
                    "priority": "high",
                },
                "phase": "Phase 1",
                "project": "test-project",
                "message": "Task retrieved",
            })
            mock_adapter_class.return_value = mock_adapter

            result = await task.get_task_impl(
                settings=self.mock_settings,
                task_id="T01",
            )

            assert result["success"] is True
            assert result["task"]["task_id"] == "T01"
            assert result["phase"] == "Phase 1"

    @pytest.mark.asyncio
    async def test_get_task_with_knowledge(self):
        """Test get_task with related knowledge."""
        with patch.object(task, "PrismindAdapter") as mock_adapter_class:
            mock_adapter = AsyncMock()
            mock_adapter.call = AsyncMock(return_value={
                "success": True,
                "task": {
                    "task_id": "T01",
                    "name": "Task 1",
                    "notes": "Important notes",
                },
                "phase": "Phase 1",
                "project": "test-project",
            })
            mock_adapter.search_knowledge = AsyncMock(return_value=[
                {"content": "Related knowledge 1"},
            ])
            mock_adapter_class.return_value = mock_adapter

            result = await task.get_task_impl(
                settings=self.mock_settings,
                task_id="T01",
                include_related_knowledge=True,
            )

            assert result["success"] is True
            assert "related_knowledge" in result
            mock_adapter.search_knowledge.assert_called_once()


class TestDeleteTask:
    """Tests for delete_task tool."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Setup test fixtures."""
        self.mock_settings = MagicMock()
        self.mock_settings.prismind_url = "http://localhost:8112"
        self.mock_settings.prismind_timeout = 30.0
        task._settings = self.mock_settings

    @pytest.mark.asyncio
    async def test_delete_task_success(self):
        """Test successful task deletion."""
        with patch.object(task, "PrismindAdapter") as mock_adapter_class:
            mock_adapter = AsyncMock()
            mock_adapter.get_progress = AsyncMock(return_value={
                "phases": [{
                    "phase": "Phase 1",
                    "tasks": [
                        {"task_id": "T01", "name": "Task 1"},
                    ],
                }],
            })
            mock_adapter.call = AsyncMock(return_value={
                "success": True,
                "task_id": "T01",
                "phase": "Phase 1",
                "project": "test-project",
                "dependent_tasks_updated": [],
                "message": "Task deleted",
            })
            mock_adapter_class.return_value = mock_adapter

            result = await task.delete_task_impl(
                settings=self.mock_settings,
                task_id="T01",
            )

            assert result["success"] is True
            assert result["task_id"] == "T01"

    @pytest.mark.asyncio
    async def test_delete_task_with_dependencies_no_cascade(self):
        """Test delete_task fails with dependencies when cascade is disabled."""
        with patch.object(task, "PrismindAdapter") as mock_adapter_class:
            mock_adapter = AsyncMock()
            mock_adapter.get_progress = AsyncMock(return_value={
                "phases": [{
                    "phase": "Phase 1",
                    "tasks": [
                        {"task_id": "T01", "name": "Task 1"},
                        {"task_id": "T02", "blocked_by": ["T01"]},
                    ],
                }],
            })
            mock_adapter_class.return_value = mock_adapter

            result = await task.delete_task_impl(
                settings=self.mock_settings,
                task_id="T01",
                cascade_unblock=False,
            )

            assert result["success"] is False
            assert "impacted_tasks" in result


class TestUpdateTask:
    """Tests for update_task tool."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Setup test fixtures."""
        self.mock_settings = MagicMock()
        self.mock_settings.prismind_url = "http://localhost:8112"
        self.mock_settings.prismind_timeout = 30.0
        task._settings = self.mock_settings

    @pytest.mark.asyncio
    async def test_update_task_success(self):
        """Test successful task update."""
        with patch.object(task, "PrismindAdapter") as mock_adapter_class:
            mock_adapter = AsyncMock()
            mock_adapter.call = AsyncMock(return_value={
                "success": True,
                "task_id": "T01",
                "project": "test-project",
                "updated_fields": ["name", "priority"],
                "phase_moved": False,
                "old_phase": "Phase 1",
                "new_phase": "Phase 1",
                "message": "Task updated",
            })
            mock_adapter_class.return_value = mock_adapter

            result = await task.update_task_impl(
                settings=self.mock_settings,
                task_id="T01",
                name="New Name",
                priority="high",
            )

            assert result["success"] is True
            assert "name" in result["updated_fields"]
            assert "priority" in result["updated_fields"]

    @pytest.mark.asyncio
    async def test_update_task_phase_move(self):
        """Test task phase move."""
        with patch.object(task, "PrismindAdapter") as mock_adapter_class:
            mock_adapter = AsyncMock()
            mock_adapter.call = AsyncMock(return_value={
                "success": True,
                "task_id": "T01",
                "project": "test-project",
                "updated_fields": ["phase"],
                "phase_moved": True,
                "old_phase": "Phase 1",
                "new_phase": "Phase 2",
                "message": "Task moved",
            })
            mock_adapter_class.return_value = mock_adapter

            result = await task.update_task_impl(
                settings=self.mock_settings,
                task_id="T01",
                new_phase="Phase 2",
            )

            assert result["success"] is True
            assert result["phase_moved"] is True
            assert result["new_phase"] == "Phase 2"

    @pytest.mark.asyncio
    async def test_update_task_invalid_dependency(self):
        """Test update_task fails with invalid dependency."""
        with patch.object(task, "PrismindAdapter") as mock_adapter_class:
            mock_adapter = AsyncMock()
            mock_adapter.get_progress = AsyncMock(return_value={
                "phases": [{
                    "phase": "Phase 1",
                    "tasks": [
                        {"task_id": "T01", "name": "Task 1"},
                    ],
                }],
            })
            mock_adapter_class.return_value = mock_adapter

            result = await task.update_task_impl(
                settings=self.mock_settings,
                task_id="T01",
                blocked_by=["T99"],  # Non-existent task
            )

            assert result["success"] is False
            assert "Invalid blocked_by task IDs" in result["error"]


class TestShortcutTools:
    """Tests for shortcut/convenience tools."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Setup test fixtures."""
        self.mock_settings = MagicMock()
        self.mock_settings.prismind_url = "http://localhost:8112"
        self.mock_settings.prismind_timeout = 30.0
        task._settings = self.mock_settings

    @pytest.mark.asyncio
    async def test_move_task_to_phase(self):
        """Test move_task_to_phase shortcut."""
        with patch.object(task, "PrismindAdapter") as mock_adapter_class:
            mock_adapter = AsyncMock()
            mock_adapter.call = AsyncMock(return_value={
                "success": True,
                "task_id": "T01",
                "phase_moved": True,
                "old_phase": "Phase 1",
                "new_phase": "Phase 2",
            })
            mock_adapter_class.return_value = mock_adapter

            result = await task.move_task_to_phase_impl(
                settings=self.mock_settings,
                task_id="T01",
                from_phase="Phase 1",
                to_phase="Phase 2",
            )

            assert result["success"] is True
            assert result["phase_moved"] is True

    @pytest.mark.asyncio
    async def test_set_task_priority(self):
        """Test set_task_priority shortcut."""
        with patch.object(task, "PrismindAdapter") as mock_adapter_class:
            mock_adapter = AsyncMock()
            mock_adapter.call = AsyncMock(return_value={
                "success": True,
                "task_id": "T01",
                "updated_fields": ["priority"],
            })
            mock_adapter_class.return_value = mock_adapter

            result = await task.set_task_priority_impl(
                settings=self.mock_settings,
                task_id="T01",
                priority="high",
            )

            assert result["success"] is True
            assert "priority" in result["updated_fields"]

    @pytest.mark.asyncio
    async def test_set_task_priority_invalid(self):
        """Test set_task_priority with invalid priority."""
        result = await task.set_task_priority_impl(
            settings=self.mock_settings,
            task_id="T01",
            priority="invalid",
        )

        assert result["success"] is False
        assert "Invalid priority" in result["error"]

    @pytest.mark.asyncio
    async def test_set_task_blockers(self):
        """Test set_task_blockers shortcut."""
        with patch.object(task, "PrismindAdapter") as mock_adapter_class:
            mock_adapter = AsyncMock()
            mock_adapter.get_progress = AsyncMock(return_value={
                "phases": [{
                    "phase": "Phase 1",
                    "tasks": [
                        {"task_id": "T01"},
                        {"task_id": "T02"},
                    ],
                }],
            })
            mock_adapter.call = AsyncMock(return_value={
                "success": True,
                "task_id": "T02",
                "updated_fields": ["blocked_by"],
            })
            mock_adapter_class.return_value = mock_adapter

            result = await task.set_task_blockers_impl(
                settings=self.mock_settings,
                task_id="T02",
                blocked_by=["T01"],
            )

            assert result["success"] is True
            assert "blocked_by" in result["updated_fields"]


class TestRegisterTools:
    """Tests for tool registration."""

    def test_register_tools(self):
        """Test that tools are registered correctly."""
        mock_mcp = MagicMock()
        mock_settings = MagicMock()

        task.register_tools(mock_mcp, mock_settings)

        # Should register 12 tools (5 original + 3 new + 3 shortcuts + detect_task_duplicates)
        assert mock_mcp.tool.call_count == 12

    def test_lookup_tools_require_phase(self):
        """Lookup tools must declare phase as a required parameter (no default).

        Catches regression where phase becomes optional and silent task_id
        ambiguity reappears (the T173 race fallout).
        """
        import inspect

        registered: dict[str, callable] = {}

        # Capture the decorated function under each `@mcp.tool()` call.
        def fake_tool_factory():
            def decorator(fn):
                registered[fn.__name__] = fn
                return fn

            return decorator

        mock_mcp = MagicMock()
        mock_mcp.tool.side_effect = fake_tool_factory
        mock_settings = MagicMock()

        task.register_tools(mock_mcp, mock_settings)

        lookup_tools = [
            "start_task",
            "complete_task",
            "block_task",
            "get_task",
            "delete_task",
            "update_task",
            "set_task_priority",
            "set_task_blockers",
        ]
        for name in lookup_tools:
            assert name in registered, f"{name} was not registered"
            sig = inspect.signature(registered[name])
            assert "phase" in sig.parameters, f"{name} missing phase parameter"
            phase_param = sig.parameters["phase"]
            assert phase_param.default is inspect.Parameter.empty, (
                f"{name}.phase must be required (no default), got default={phase_param.default!r}"
            )


class TestAttachmentSummary:
    """Tests for _attachment_summary (Cognilens fallback for attachments).

    CognilensAdapter raises rather than returning a rejection as if it were
    prose, so this helper is what decides whether an attachment survives a
    summarizer outage.
    """

    async def test_returns_the_summary_on_success(self):
        cognilens = AsyncMock()
        cognilens.summarize.return_value = "3 行の要約"

        out = await task._attachment_summary(cognilens, "body" * 500)

        assert out == "3 行の要約"
        cognilens.summarize.assert_awaited_once()

    async def test_falls_back_to_a_labelled_excerpt(self):
        """The attachment must survive, and the reader must know it is not a summary."""
        cognilens = AsyncMock()
        cognilens.summarize.side_effect = CognilensError(
            "summarize returned no 'summary'", tool="summarize"
        )
        content = "line\n" * 1000

        out = await task._attachment_summary(cognilens, content)

        assert out.startswith("(要約失敗")
        assert "以下略" in out
        assert content[:100] in out
        # The excerpt is bounded: the stored document must not change size
        # class just because the summarizer was down.
        assert len(out) < len(content)

    async def test_short_content_is_not_marked_elided(self):
        cognilens = AsyncMock()
        cognilens.summarize.side_effect = CognilensError("down", tool="summarize")

        out = await task._attachment_summary(cognilens, "short body")

        assert "short body" in out
        assert "以下略" not in out

    async def test_a_non_cognilens_error_is_not_swallowed(self):
        """Only Cognilens failures degrade. A bug here must still surface."""
        cognilens = AsyncMock()
        cognilens.summarize.side_effect = TypeError("unexpected keyword argument")

        with pytest.raises(TypeError):
            await task._attachment_summary(cognilens, "body")
