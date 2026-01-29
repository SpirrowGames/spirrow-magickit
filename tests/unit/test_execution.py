"""Tests for execution tools."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from magickit.mcp.tools import execution


class TestDecomposeSpecification:
    """Tests for decompose_specification tool."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Setup test fixtures."""
        # Clear sessions before each test
        execution._execution_sessions.clear()

        # Create mock settings
        self.mock_settings = MagicMock()
        self.mock_settings.lexora_url = "http://localhost:8111"
        self.mock_settings.lexora_timeout = 60.0

        # Set module settings
        execution._settings = self.mock_settings

    @pytest.mark.asyncio
    async def test_generates_execution_id(self):
        """Test that a unique execution ID is generated."""
        with patch.object(execution, "LexoraAdapter") as mock_lexora_class:
            mock_lexora = AsyncMock()
            mock_lexora.chat = AsyncMock(
                return_value=json.dumps({
                    "tasks": [
                        {"id": "task-1", "name": "Test task", "description": "Do something"}
                    ]
                })
            )
            mock_lexora_class.return_value = mock_lexora

            result = await execution.decompose_specification(
                specification={"specification": {"title": "Test"}},
            )

            assert result["success"] is True
            assert result["execution_id"].startswith("exec-")

    @pytest.mark.asyncio
    async def test_returns_tasks_from_llm(self):
        """Test that LLM-generated tasks are returned."""
        with patch.object(execution, "LexoraAdapter") as mock_lexora_class:
            mock_lexora = AsyncMock()
            expected_tasks = [
                {
                    "id": "task-1",
                    "name": "Create cache module",
                    "description": "Create a new cache module",
                    "target_files": ["src/cache.py"],
                    "action_type": "create",
                    "dependencies": [],
                },
                {
                    "id": "task-2",
                    "name": "Add cache to API",
                    "description": "Integrate cache into API calls",
                    "target_files": ["src/api.py"],
                    "action_type": "modify",
                    "dependencies": ["task-1"],
                },
            ]
            mock_lexora.chat = AsyncMock(
                return_value=json.dumps({"tasks": expected_tasks})
            )
            mock_lexora_class.return_value = mock_lexora

            result = await execution.decompose_specification(
                specification={"specification": {"title": "Add caching"}},
            )

            assert result["success"] is True
            assert result["task_count"] == 2
            assert result["tasks"][0]["name"] == "Create cache module"
            assert result["tasks"][1]["dependencies"] == ["task-1"]

    @pytest.mark.asyncio
    async def test_stores_execution_session(self):
        """Test that execution session is stored."""
        with patch.object(execution, "LexoraAdapter") as mock_lexora_class:
            mock_lexora = AsyncMock()
            mock_lexora.chat = AsyncMock(
                return_value=json.dumps({"tasks": [{"id": "task-1", "name": "Test"}]})
            )
            mock_lexora_class.return_value = mock_lexora

            result = await execution.decompose_specification(
                specification={"specification": {"title": "Test"}},
            )

            execution_id = result["execution_id"]
            assert execution_id in execution._execution_sessions
            session = execution._execution_sessions[execution_id]
            assert session["status"] == "ready"
            assert len(session["tasks"]) == 1

    @pytest.mark.asyncio
    async def test_fallback_tasks_on_llm_error(self):
        """Test that fallback tasks are generated when LLM fails."""
        with patch.object(execution, "LexoraAdapter") as mock_lexora_class:
            mock_lexora = AsyncMock()
            mock_lexora.chat = AsyncMock(side_effect=Exception("LLM error"))
            mock_lexora_class.return_value = mock_lexora

            result = await execution.decompose_specification(
                specification={
                    "specification": {
                        "title": "Test",
                        "target_files": ["src/api.py", "src/utils.py"],
                    }
                },
            )

            assert result["success"] is True
            assert result["task_count"] >= 2  # At least one task per file

    @pytest.mark.asyncio
    async def test_adds_task_metadata(self):
        """Test that tasks get status and created_at metadata."""
        with patch.object(execution, "LexoraAdapter") as mock_lexora_class:
            mock_lexora = AsyncMock()
            mock_lexora.chat = AsyncMock(
                return_value=json.dumps({"tasks": [{"id": "task-1", "name": "Test"}]})
            )
            mock_lexora_class.return_value = mock_lexora

            result = await execution.decompose_specification(
                specification={"specification": {"title": "Test"}},
            )

            task = result["tasks"][0]
            assert task["status"] == "pending"
            assert "created_at" in task


class TestGetNextTask:
    """Tests for get_next_task tool."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Setup test fixtures."""
        execution._execution_sessions.clear()

        # Create a test execution session
        self.execution_id = "exec-test1234"
        execution._execution_sessions[self.execution_id] = {
            "specification": {},
            "tasks": [
                {
                    "id": "task-1",
                    "name": "First task",
                    "description": "Do first thing",
                    "status": "pending",
                    "dependencies": [],
                },
                {
                    "id": "task-2",
                    "name": "Second task",
                    "description": "Do second thing",
                    "status": "pending",
                    "dependencies": ["task-1"],
                },
            ],
            "current_task_index": 0,
            "completed_tasks": [],
            "failed_tasks": [],
            "status": "ready",
            "created_at": "2024-01-01T00:00:00",
        }

    @pytest.mark.asyncio
    async def test_returns_first_task_without_dependencies(self):
        """Test that task without dependencies is returned first."""
        result = await execution.get_next_task(self.execution_id)

        assert result["has_task"] is True
        assert result["task"]["id"] == "task-1"
        assert result["task"]["status"] == "in_progress"

    @pytest.mark.asyncio
    async def test_returns_error_for_unknown_session(self):
        """Test error for unknown execution ID."""
        result = await execution.get_next_task("exec-unknown")

        assert result["has_task"] is False
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_blocks_task_with_unmet_dependencies(self):
        """Test that task with unmet dependencies is not returned."""
        # Mark first task as in_progress
        session = execution._execution_sessions[self.execution_id]
        session["tasks"][0]["status"] = "in_progress"

        result = await execution.get_next_task(self.execution_id)

        # Should not return task-2 since task-1 is not completed
        assert result["has_task"] is False
        assert result["status"] == "waiting_for_dependencies"

    @pytest.mark.asyncio
    async def test_returns_task_after_dependency_completed(self):
        """Test that dependent task is available after dependency completes."""
        session = execution._execution_sessions[self.execution_id]
        # Complete task-1
        session["tasks"][0]["status"] = "completed"
        session["completed_tasks"].append(session["tasks"][0])

        result = await execution.get_next_task(self.execution_id)

        assert result["has_task"] is True
        assert result["task"]["id"] == "task-2"


class TestCompleteTask:
    """Tests for complete_task tool."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Setup test fixtures."""
        execution._execution_sessions.clear()

        self.execution_id = "exec-test1234"
        execution._execution_sessions[self.execution_id] = {
            "specification": {},
            "tasks": [
                {
                    "id": "task-1",
                    "name": "First task",
                    "status": "in_progress",
                    "dependencies": [],
                },
                {
                    "id": "task-2",
                    "name": "Second task",
                    "status": "pending",
                    "dependencies": ["task-1"],
                },
            ],
            "completed_tasks": [],
            "failed_tasks": [],
            "status": "in_progress",
            "created_at": "2024-01-01T00:00:00",
        }

    @pytest.mark.asyncio
    async def test_marks_task_as_completed(self):
        """Test that task is marked as completed."""
        result = await execution.complete_task(
            execution_id=self.execution_id,
            task_id="task-1",
            success=True,
            result="Done successfully",
        )

        assert result["success"] is True
        assert result["task_completed"] == "task-1"

        session = execution._execution_sessions[self.execution_id]
        assert len(session["completed_tasks"]) == 1
        assert session["completed_tasks"][0]["id"] == "task-1"

    @pytest.mark.asyncio
    async def test_marks_task_as_failed(self):
        """Test that failed task is recorded."""
        result = await execution.complete_task(
            execution_id=self.execution_id,
            task_id="task-1",
            success=False,
            error="Something went wrong",
        )

        assert result["success"] is True
        session = execution._execution_sessions[self.execution_id]
        assert len(session["failed_tasks"]) == 1

    @pytest.mark.asyncio
    async def test_returns_next_task_after_completion(self):
        """Test that next task is returned after completion."""
        result = await execution.complete_task(
            execution_id=self.execution_id,
            task_id="task-1",
            success=True,
        )

        assert result["has_next_task"] is True
        assert result["next_task"]["id"] == "task-2"

    @pytest.mark.asyncio
    async def test_reports_completion_when_all_done(self):
        """Test completion status when all tasks are done."""
        # Complete first task
        await execution.complete_task(
            execution_id=self.execution_id,
            task_id="task-1",
            success=True,
        )

        # Mark second task as in_progress and complete it
        session = execution._execution_sessions[self.execution_id]
        session["tasks"][1]["status"] = "in_progress"

        result = await execution.complete_task(
            execution_id=self.execution_id,
            task_id="task-2",
            success=True,
        )

        assert result["is_complete"] is True
        assert result["summary"]["completed"] == 2

    @pytest.mark.asyncio
    async def test_returns_error_for_unknown_task(self):
        """Test error for unknown task ID."""
        result = await execution.complete_task(
            execution_id=self.execution_id,
            task_id="task-unknown",
            success=True,
        )

        assert result["success"] is False
        assert "not found" in result["error"]


class TestGetExecutionStatus:
    """Tests for get_execution_status tool."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Setup test fixtures."""
        execution._execution_sessions.clear()

        self.execution_id = "exec-test1234"
        execution._execution_sessions[self.execution_id] = {
            "specification": {},
            "tasks": [
                {"id": "task-1", "name": "Task 1", "status": "completed", "dependencies": []},
                {"id": "task-2", "name": "Task 2", "status": "in_progress", "dependencies": []},
                {"id": "task-3", "name": "Task 3", "status": "pending", "dependencies": []},
            ],
            "completed_tasks": [{"id": "task-1"}],
            "failed_tasks": [],
            "status": "in_progress",
            "created_at": "2024-01-01T00:00:00",
        }

    @pytest.mark.asyncio
    async def test_returns_status_summary(self):
        """Test that status summary is returned."""
        result = await execution.get_execution_status(self.execution_id)

        assert result["found"] is True
        assert result["status"] == "in_progress"
        assert result["progress"]["completed"] == 1
        assert result["progress"]["in_progress"] == 1
        assert result["progress"]["pending"] == 1
        assert result["progress"]["total"] == 3

    @pytest.mark.asyncio
    async def test_returns_error_for_unknown_session(self):
        """Test error for unknown execution ID."""
        result = await execution.get_execution_status("exec-unknown")

        assert result["found"] is False
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_calculates_progress_percent(self):
        """Test that progress percentage is calculated."""
        result = await execution.get_execution_status(self.execution_id)

        # 1 completed out of 3 = 33.3%
        assert result["progress"]["percent"] == 33.3


class TestHelperFunctions:
    """Tests for helper functions."""

    def test_parse_tasks_response_valid_json(self):
        """Test parsing valid JSON response."""
        response = '{"tasks": [{"id": "task-1", "name": "Test"}]}'
        result = execution._parse_tasks_response(response)
        assert len(result) == 1
        assert result[0]["id"] == "task-1"

    def test_parse_tasks_response_with_surrounding_text(self):
        """Test parsing JSON with surrounding text."""
        response = 'Here are the tasks:\n{"tasks": [{"id": "task-1"}]}\n\nDone.'
        result = execution._parse_tasks_response(response)
        assert len(result) == 1

    def test_parse_tasks_response_invalid_json(self):
        """Test parsing invalid JSON returns empty list."""
        response = "This is not JSON"
        result = execution._parse_tasks_response(response)
        assert result == []

    def test_generate_fallback_tasks_from_files(self):
        """Test fallback task generation from target files."""
        spec_data = {
            "target_files": ["src/api.py", "src/utils.py"],
        }
        result = execution._generate_fallback_tasks(spec_data)

        assert len(result) >= 2
        assert any("api.py" in t.get("name", "") for t in result)

    def test_generate_fallback_tasks_includes_test(self):
        """Test that fallback includes test task when test_points exist."""
        spec_data = {
            "target_files": ["src/api.py"],
            "test_points": ["Verify cache hit", "Check TTL"],
        }
        result = execution._generate_fallback_tasks(spec_data)

        test_tasks = [t for t in result if t.get("action_type") == "test"]
        assert len(test_tasks) == 1

    def test_generate_fallback_tasks_from_requirements(self):
        """Test fallback with only requirements."""
        spec_data = {
            "requirements": ["Add logging", "Handle errors"],
        }
        result = execution._generate_fallback_tasks(spec_data)

        assert len(result) >= 1


class TestFinalizeExecution:
    """Tests for finalize_execution tool."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Setup test fixtures."""
        execution._execution_sessions.clear()

        self.mock_settings = MagicMock()
        self.mock_settings.prismind_url = "http://localhost:8112"
        self.mock_settings.prismind_timeout = 30.0
        execution._settings = self.mock_settings

        # Create a completed execution session
        self.execution_id = "exec-final123"
        execution._execution_sessions[self.execution_id] = {
            "specification": {
                "specification": {
                    "title": "Add Caching Feature",
                    "test_points": ["Verify cache hit"],
                }
            },
            "tasks": [
                {"id": "task-1", "name": "Create cache", "status": "completed", "result": "Created cache.py"},
                {"id": "task-2", "name": "Integrate", "status": "completed", "result": "Updated api.py"},
            ],
            "completed_tasks": [
                {"id": "task-1", "name": "Create cache", "result": "Created cache.py", "action_type": "create"},
                {"id": "task-2", "name": "Integrate", "result": "Updated api.py", "action_type": "modify"},
            ],
            "failed_tasks": [],
            "status": "completed",
            "created_at": "2024-01-01T00:00:00",
        }

    @pytest.mark.asyncio
    async def test_generates_summary(self):
        """Test that execution summary is generated."""
        result = await execution.finalize_execution(
            execution_id=self.execution_id,
            save_to_knowledge=False,
        )

        assert result["success"] is True
        assert "Add Caching Feature" in result["summary"]
        assert "完了タスク" in result["summary"]

    @pytest.mark.asyncio
    async def test_returns_statistics(self):
        """Test that statistics are returned."""
        result = await execution.finalize_execution(
            execution_id=self.execution_id,
            save_to_knowledge=False,
        )

        assert result["statistics"]["total_tasks"] == 2
        assert result["statistics"]["completed"] == 2
        assert result["statistics"]["success_rate"] == 100.0

    @pytest.mark.asyncio
    async def test_returns_handoff_info(self):
        """Test that handoff information is returned."""
        result = await execution.finalize_execution(
            execution_id=self.execution_id,
            save_to_knowledge=False,
        )

        assert "handoff" in result
        assert result["handoff"]["status"] == "success"
        assert result["handoff"]["completed_count"] == 2

    @pytest.mark.asyncio
    async def test_saves_to_knowledge_when_enabled(self):
        """Test that results are saved to Prismind when enabled."""
        with patch.object(execution, "PrismindAdapter") as mock_prismind_class:
            mock_prismind = AsyncMock()
            mock_prismind.add_knowledge = AsyncMock(return_value={"success": True})
            mock_prismind_class.return_value = mock_prismind

            result = await execution.finalize_execution(
                execution_id=self.execution_id,
                project="test-project",
                save_to_knowledge=True,
            )

            assert result["success"] is True
            assert result["knowledge_saved"] >= 1
            mock_prismind.add_knowledge.assert_called()

    @pytest.mark.asyncio
    async def test_returns_error_for_unknown_session(self):
        """Test error for unknown execution ID."""
        result = await execution.finalize_execution(
            execution_id="exec-unknown",
            save_to_knowledge=False,
        )

        assert result["success"] is False
        assert "not found" in result["error"]


class TestGenerateExecutionReport:
    """Tests for generate_execution_report tool."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Setup test fixtures."""
        execution._execution_sessions.clear()

        self.execution_id = "exec-report123"
        execution._execution_sessions[self.execution_id] = {
            "specification": {"specification": {"title": "Test Feature"}},
            "tasks": [
                {"id": "task-1", "name": "Task A", "status": "completed", "action_type": "create", "description": "Create something"},
                {"id": "task-2", "name": "Task B", "status": "completed", "action_type": "modify", "description": "Modify something"},
            ],
            "completed_tasks": [
                {"id": "task-1", "name": "Task A", "action_type": "create", "result": "Done", "description": "Create something"},
                {"id": "task-2", "name": "Task B", "action_type": "modify", "result": "Done", "description": "Modify something"},
            ],
            "failed_tasks": [],
            "status": "completed",
            "created_at": "2024-01-01T00:00:00",
        }

    @pytest.mark.asyncio
    async def test_generates_markdown_report(self):
        """Test markdown format report generation."""
        result = await execution.generate_execution_report(
            execution_id=self.execution_id,
            format="markdown",
        )

        assert result["success"] is True
        assert result["format"] == "markdown"
        assert "# Execution Report" in result["report"]
        assert "Test Feature" in result["report"]

    @pytest.mark.asyncio
    async def test_generates_changelog_report(self):
        """Test changelog format report generation."""
        result = await execution.generate_execution_report(
            execution_id=self.execution_id,
            format="changelog",
        )

        assert result["success"] is True
        assert result["format"] == "changelog"
        assert "### Added" in result["report"]
        assert "### Changed" in result["report"]

    @pytest.mark.asyncio
    async def test_generates_brief_report(self):
        """Test brief format report generation."""
        result = await execution.generate_execution_report(
            execution_id=self.execution_id,
            format="brief",
        )

        assert result["success"] is True
        assert result["format"] == "brief"
        assert "✅ Success" in result["report"]

    @pytest.mark.asyncio
    async def test_returns_error_for_unknown_session(self):
        """Test error for unknown execution ID."""
        result = await execution.generate_execution_report(
            execution_id="exec-unknown",
        )

        assert result["success"] is False
        assert "not found" in result["error"]


class TestRunFullWorkflow:
    """Tests for run_full_workflow tool."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Setup test fixtures."""
        execution._execution_sessions.clear()

        self.mock_settings = MagicMock()
        self.mock_settings.lexora_url = "http://localhost:8111"
        self.mock_settings.lexora_timeout = 60.0
        self.mock_settings.prismind_url = "http://localhost:8112"
        self.mock_settings.prismind_timeout = 30.0
        execution._settings = self.mock_settings

    @pytest.mark.asyncio
    async def test_returns_questions_when_not_auto_approve(self):
        """Test that questions are returned when auto_approve is False."""
        with patch("magickit.mcp.tools.specification.start_specification") as mock_start:
            mock_start.return_value = {
                "session_id": "spec-123",
                "questions": [{"id": "q1", "question": "Test?"}],
                "status": "questions_ready",
            }

            result = await execution.run_full_workflow(
                target="src/api.py",
                request="Add caching",
                auto_approve=False,
            )

            assert result["success"] is True
            assert result["status"] == "questions_pending"
            assert "questions" in result

    @pytest.mark.asyncio
    async def test_generates_plan_when_auto_approve(self):
        """Test that execution plan is generated when auto_approve is True."""
        with patch.object(execution, "LexoraAdapter") as mock_lexora_class:
            mock_lexora = AsyncMock()
            mock_lexora.chat = AsyncMock(
                return_value=json.dumps({
                    "specification": {
                        "title": "Add Caching",
                        "target_files": ["src/api.py"],
                        "requirements": ["Add cache"],
                    },
                    "required_permissions": {"edit": ["src/api.py"]},
                })
            )
            mock_lexora_class.return_value = mock_lexora

            result = await execution.run_full_workflow(
                target="src/api.py",
                request="Add caching",
                auto_approve=True,
            )

            assert result["success"] is True
            assert result["status"] == "ready_to_execute"
            assert "execution_plan" in result
            assert "permissions" in result

    @pytest.mark.asyncio
    async def test_raises_error_when_settings_not_initialized(self):
        """Test that error is raised when settings are not initialized."""
        execution._settings = None

        with pytest.raises(RuntimeError, match="Settings not initialized"):
            await execution.run_full_workflow(
                target="src/api.py",
                request="Add caching",
            )
