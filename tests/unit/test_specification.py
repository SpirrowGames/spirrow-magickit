"""Tests for specification tools."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from magickit.mcp.tools import specification


class TestStartSpecification:
    """Tests for start_specification tool."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Setup test fixtures."""
        # Clear sessions before each test
        specification._sessions.clear()

        # Create mock settings
        self.mock_settings = MagicMock()
        self.mock_settings.prismind_url = "http://localhost:8112"
        self.mock_settings.prismind_timeout = 30.0
        self.mock_settings.lexora_url = "http://localhost:8111"
        self.mock_settings.lexora_timeout = 60.0

        # Set module settings
        specification._settings = self.mock_settings

    @pytest.mark.asyncio
    async def test_generates_session_id(self):
        """Test that a unique session ID is generated."""
        with patch.object(specification, "LexoraAdapter") as mock_lexora_class:
            mock_lexora = AsyncMock()
            mock_lexora.chat = AsyncMock(
                return_value=json.dumps(
                    {
                        "questions": [
                            {
                                "id": "q1",
                                "question": "Test question?",
                                "options": [{"label": "Option 1", "value": "opt1"}],
                                "allow_custom": False,
                            }
                        ]
                    }
                )
            )
            mock_lexora_class.return_value = mock_lexora

            result = await specification.start_specification(
                target="src/test.py",
                initial_request="Add caching",
            )

            assert "session_id" in result
            assert result["session_id"].startswith("spec-")
            assert len(result["session_id"]) == 13  # "spec-" + 8 hex chars

    @pytest.mark.asyncio
    async def test_returns_questions_from_llm(self):
        """Test that LLM-generated questions are returned."""
        with patch.object(specification, "LexoraAdapter") as mock_lexora_class:
            mock_lexora = AsyncMock()
            expected_questions = [
                {
                    "id": "cache_type",
                    "question": "What type of caching do you need?",
                    "options": [
                        {"label": "In-memory (recommended)", "value": "memory"},
                        {"label": "Redis", "value": "redis"},
                    ],
                    "allow_custom": True,
                }
            ]
            mock_lexora.chat = AsyncMock(
                return_value=json.dumps({"questions": expected_questions})
            )
            mock_lexora_class.return_value = mock_lexora

            result = await specification.start_specification(
                target="src/api.py",
                initial_request="Add caching to API responses",
            )

            assert result["status"] == "questions_ready"
            assert result["questions"] == expected_questions
            assert "next_action" in result

    @pytest.mark.asyncio
    async def test_stores_session(self):
        """Test that session is stored for later retrieval."""
        with patch.object(specification, "LexoraAdapter") as mock_lexora_class:
            mock_lexora = AsyncMock()
            mock_lexora.chat = AsyncMock(
                return_value=json.dumps({"questions": []})
            )
            mock_lexora_class.return_value = mock_lexora

            result = await specification.start_specification(
                target="src/test.py",
                initial_request="Add feature",
                feature_type="api",
            )

            session_id = result["session_id"]
            assert session_id in specification._sessions
            session = specification._sessions[session_id]
            assert session["target"] == "src/test.py"
            assert session["initial_request"] == "Add feature"
            assert session["feature_type"] == "api"
            assert session["status"] == "questions_ready"

    @pytest.mark.asyncio
    async def test_searches_template_when_feature_type_provided(self):
        """Test that Prismind is searched for templates when feature_type is given."""
        with (
            patch.object(specification, "LexoraAdapter") as mock_lexora_class,
            patch.object(specification, "PrismindAdapter") as mock_prismind_class,
        ):
            mock_lexora = AsyncMock()
            mock_lexora.chat = AsyncMock(
                return_value=json.dumps({"questions": []})
            )
            mock_lexora_class.return_value = mock_lexora

            mock_prismind = AsyncMock()
            mock_prismind.search_knowledge = AsyncMock(return_value=[])
            mock_prismind_class.return_value = mock_prismind

            await specification.start_specification(
                target="src/test.py",
                initial_request="Add caching",
                feature_type="cache",
            )

            mock_prismind.search_knowledge.assert_called_once_with(
                query="spec_template:cache",
                category="spec_template",
                limit=1,
            )

    @pytest.mark.asyncio
    async def test_fallback_questions_on_llm_error(self):
        """Test that fallback questions are used when LLM fails."""
        with patch.object(specification, "LexoraAdapter") as mock_lexora_class:
            mock_lexora = AsyncMock()
            mock_lexora.chat = AsyncMock(side_effect=Exception("LLM error"))
            mock_lexora_class.return_value = mock_lexora

            result = await specification.start_specification(
                target="src/test.py",
                initial_request="Add feature",
            )

            assert result["status"] == "questions_ready"
            assert len(result["questions"]) == 2  # Fallback has 2 questions
            assert result["questions"][0]["id"] == "scope"
            assert result["questions"][1]["id"] == "priority"

    @pytest.mark.asyncio
    async def test_raises_error_when_settings_not_initialized(self):
        """Test that error is raised when settings are not initialized."""
        specification._settings = None

        with pytest.raises(RuntimeError, match="Settings not initialized"):
            await specification.start_specification(
                target="src/test.py",
                initial_request="Add feature",
            )


class TestGenerateSpecification:
    """Tests for generate_specification tool."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Setup test fixtures."""
        specification._sessions.clear()

        self.mock_settings = MagicMock()
        self.mock_settings.lexora_url = "http://localhost:8111"
        self.mock_settings.lexora_timeout = 60.0
        specification._settings = self.mock_settings

        # Create a test session
        self.session_id = "spec-test1234"
        specification._sessions[self.session_id] = {
            "target": "src/api.py",
            "initial_request": "Add caching",
            "feature_type": "cache",
            "questions": [
                {
                    "id": "cache_type",
                    "question": "What type of caching?",
                    "options": [{"label": "Memory", "value": "memory"}],
                    "allow_custom": False,
                }
            ],
            "answers": {},
            "status": "questions_ready",
        }

    @pytest.mark.asyncio
    async def test_generates_specification_from_answers(self):
        """Test that specification is generated from user answers."""
        with patch.object(specification, "LexoraAdapter") as mock_lexora_class:
            mock_lexora = AsyncMock()
            expected_spec = {
                "specification": {
                    "title": "API Response Caching",
                    "purpose": "Improve response times",
                    "target_files": ["src/api.py"],
                    "requirements": ["Use in-memory cache"],
                    "constraints": ["Cache TTL: 5 minutes"],
                    "test_points": ["Verify cache hit/miss"],
                },
                "required_permissions": {
                    "edit": ["src/api.py"],
                    "bash": ["pytest:*"],
                },
            }
            mock_lexora.chat = AsyncMock(return_value=json.dumps(expected_spec))
            mock_lexora_class.return_value = mock_lexora

            result = await specification.generate_specification(
                session_id=self.session_id,
                answers={"cache_type": "memory"},
            )

            assert result["success"] is True
            assert result["specification"] == expected_spec["specification"]
            assert result["required_permissions"] == expected_spec["required_permissions"]
            assert result["estimated_files"] == ["src/api.py"]

    @pytest.mark.asyncio
    async def test_returns_error_for_unknown_session(self):
        """Test that error is returned for unknown session ID."""
        result = await specification.generate_specification(
            session_id="spec-unknown",
            answers={},
        )

        assert result["success"] is False
        assert "Session not found" in result["error"]

    @pytest.mark.asyncio
    async def test_updates_session_status(self):
        """Test that session status is updated during generation."""
        with patch.object(specification, "LexoraAdapter") as mock_lexora_class:
            mock_lexora = AsyncMock()
            mock_lexora.chat = AsyncMock(
                return_value=json.dumps(
                    {"specification": {}, "required_permissions": {}}
                )
            )
            mock_lexora_class.return_value = mock_lexora

            await specification.generate_specification(
                session_id=self.session_id,
                answers={"cache_type": "memory"},
            )

            session = specification._sessions[self.session_id]
            assert session["status"] == "completed"
            assert session["answers"] == {"cache_type": "memory"}

    @pytest.mark.asyncio
    async def test_handles_llm_error(self):
        """Test that LLM errors are handled gracefully."""
        with patch.object(specification, "LexoraAdapter") as mock_lexora_class:
            mock_lexora = AsyncMock()
            mock_lexora.chat = AsyncMock(side_effect=Exception("LLM error"))
            mock_lexora_class.return_value = mock_lexora

            result = await specification.generate_specification(
                session_id=self.session_id,
                answers={},
            )

            assert result["success"] is False
            assert "LLM error" in result["error"]
            assert specification._sessions[self.session_id]["status"] == "failed"

    @pytest.mark.asyncio
    async def test_raises_error_when_settings_not_initialized(self):
        """Test that error is raised when settings are not initialized."""
        specification._settings = None

        with pytest.raises(RuntimeError, match="Settings not initialized"):
            await specification.generate_specification(
                session_id=self.session_id,
                answers={},
            )


class TestParseHelpers:
    """Tests for parsing helper functions."""

    def test_parse_questions_response_valid_json(self):
        """Test parsing valid JSON response."""
        response = '{"questions": [{"id": "q1", "question": "Test?"}]}'
        result = specification._parse_questions_response(response)
        assert len(result) == 1
        assert result[0]["id"] == "q1"

    def test_parse_questions_response_with_surrounding_text(self):
        """Test parsing JSON with surrounding text."""
        response = 'Here is the output:\n{"questions": [{"id": "q1"}]}\n\nDone.'
        result = specification._parse_questions_response(response)
        assert len(result) == 1
        assert result[0]["id"] == "q1"

    def test_parse_questions_response_invalid_json(self):
        """Test parsing invalid JSON returns empty list."""
        response = "This is not JSON"
        result = specification._parse_questions_response(response)
        assert result == []

    def test_parse_specification_response_valid_json(self):
        """Test parsing valid specification JSON."""
        response = '{"specification": {"title": "Test"}, "required_permissions": {}}'
        result = specification._parse_specification_response(response)
        assert result["specification"]["title"] == "Test"

    def test_parse_specification_response_invalid_json(self):
        """Test parsing invalid JSON returns empty structure."""
        response = "Invalid"
        result = specification._parse_specification_response(response)
        assert result == {"specification": {}, "required_permissions": {}}


class TestPrepareExecution:
    """Tests for prepare_execution tool."""

    @pytest.mark.asyncio
    async def test_converts_edit_permissions(self):
        """Test conversion of edit permissions to allowedPrompts format."""
        spec = {
            "specification": {
                "target_files": ["src/api.py", "src/utils.py"],
            },
            "required_permissions": {
                "edit": ["src/api.py", "src/utils.py"],
            },
        }

        result = await specification.prepare_execution(spec)

        assert result["success"] is True
        assert len(result["allowed_prompts"]) >= 2
        prompts = {p["prompt"] for p in result["allowed_prompts"]}
        assert "edit src/api.py" in prompts
        assert "edit src/utils.py" in prompts

    @pytest.mark.asyncio
    async def test_converts_bash_permissions(self):
        """Test conversion of bash permissions to semantic prompts."""
        spec = {
            "specification": {},
            "required_permissions": {
                "bash": ["pytest:*", "npm:install"],
            },
        }

        result = await specification.prepare_execution(spec)

        assert result["success"] is True
        prompts = {p["prompt"] for p in result["allowed_prompts"]}
        assert "run tests" in prompts

    @pytest.mark.asyncio
    async def test_uses_target_files_when_no_edit_permissions(self):
        """Test that target_files are used when edit permissions are not specified."""
        spec = {
            "specification": {
                "target_files": ["src/main.py"],
            },
            "required_permissions": {},
        }

        result = await specification.prepare_execution(spec)

        assert result["success"] is True
        prompts = {p["prompt"] for p in result["allowed_prompts"]}
        assert "edit src/main.py" in prompts

    @pytest.mark.asyncio
    async def test_deduplicates_prompts(self):
        """Test that duplicate prompts are removed."""
        spec = {
            "specification": {
                "target_files": ["src/api.py"],
            },
            "required_permissions": {
                "edit": ["src/api.py"],  # Same file
            },
        }

        result = await specification.prepare_execution(spec)

        # Should only have one prompt for the file
        edit_prompts = [p for p in result["allowed_prompts"] if "api.py" in p["prompt"]]
        assert len(edit_prompts) == 1

    @pytest.mark.asyncio
    async def test_generates_summary(self):
        """Test that human-readable summary is generated."""
        spec = {
            "specification": {
                "target_files": ["src/api.py"],
            },
            "required_permissions": {
                "edit": ["src/api.py"],
                "bash": ["pytest:*"],
            },
        }

        result = await specification.prepare_execution(spec)

        assert result["summary"]
        assert "Edit" in result["summary"] or "Run" in result["summary"]

    @pytest.mark.asyncio
    async def test_includes_next_action(self):
        """Test that next_action instructions are included."""
        spec = {
            "specification": {},
            "required_permissions": {"edit": ["test.py"]},
        }

        result = await specification.prepare_execution(spec)

        assert "next_action" in result
        assert "instruction" in result["next_action"]
        assert "ExitPlanMode" in result["next_action"]["instruction"]


class TestApplyPermissions:
    """Tests for apply_permissions tool."""

    @pytest.mark.asyncio
    async def test_session_scope_returns_exit_plan_mode_config(self):
        """Test that session scope returns ExitPlanMode configuration."""
        prompts = [
            {"tool": "Bash", "prompt": "run tests"},
            {"tool": "Bash", "prompt": "edit src/api.py"},
        ]

        result = await specification.apply_permissions(prompts, scope="session")

        assert result["success"] is True
        assert result["apply_method"] == "exit_plan_mode"
        assert result["config"]["allowedPrompts"] == prompts

    @pytest.mark.asyncio
    async def test_project_scope_returns_settings_file_config(self):
        """Test that project scope returns settings file configuration."""
        prompts = [
            {"tool": "Bash", "prompt": "run tests"},
        ]

        result = await specification.apply_permissions(
            prompts, scope="project", project_path="/home/user/myproject"
        )

        assert result["success"] is True
        assert result["apply_method"] == "settings_file"
        assert "permissions" in result["config"]
        assert "/home/user/myproject/.claude/settings.local.json" in result["file_path"]

    @pytest.mark.asyncio
    async def test_empty_prompts_returns_no_action(self):
        """Test that empty prompts returns no action needed."""
        result = await specification.apply_permissions([], scope="session")

        assert result["success"] is True
        assert result["apply_method"] == "none"
        assert "No permissions" in result["instructions"]

    @pytest.mark.asyncio
    async def test_includes_example_usage(self):
        """Test that example usage is included for session scope."""
        prompts = [{"tool": "Bash", "prompt": "run tests"}]

        result = await specification.apply_permissions(prompts, scope="session")

        assert "example_usage" in result
        assert result["example_usage"]["tool"] == "ExitPlanMode"
