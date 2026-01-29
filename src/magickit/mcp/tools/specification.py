"""Specification tools for AI-driven requirement gathering.

Provides tools for guiding users through specification creation by dynamically
generating questions and producing structured specifications from answers.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from fastmcp import FastMCP

from magickit.adapters.lexora import LexoraAdapter
from magickit.adapters.prismind import PrismindAdapter
from magickit.config import Settings
from magickit.utils.logging import get_logger
from magickit.utils.user import get_current_user

logger = get_logger(__name__)

# Module-level settings reference
_settings: Settings | None = None

# In-memory session storage (TODO: move to Prismind for persistence)
_sessions: dict[str, dict[str, Any]] = {}


async def start_specification(
    target: str,
    initial_request: str,
    feature_type: str = "",
    user: str = "",
) -> dict[str, Any]:
    """Start AI-driven specification process by generating clarifying questions.

    USE THIS WHEN: User makes a feature request that needs clarification.
    This tool analyzes the request and generates targeted questions to
    gather requirements before implementation.

    DO NOT USE WHEN:
    - Requirements are already fully specified
    - Just exploring or researching (use research_and_summarize instead)

    Args:
        target: Target file, function, or component to modify.
        initial_request: User's original feature request (can be vague).
        feature_type: Optional hint about the type (cache, api, refactor, etc.).
        user: User identifier for multi-user support (empty for default user).

    Returns:
        Dict containing:
        - session_id: ID for this specification session
        - status: "questions_ready"
        - questions: List of questions for the user
        - next_action: Instructions for Claude to present questions
    """
    if _settings is None:
        raise RuntimeError("Settings not initialized")

    effective_user = user or get_current_user()
    session_id = f"spec-{uuid.uuid4().hex[:8]}"

    logger.info(
        "Starting specification",
        session_id=session_id,
        target=target,
        initial_request=initial_request[:50],
        user=effective_user,
    )

    # Step 1: Check for existing template in Prismind
    template = None
    if feature_type:
        prismind = PrismindAdapter(
            sse_url=_settings.prismind_url,
            timeout=_settings.prismind_timeout,
        )
        try:
            results = await prismind.search_knowledge(
                query=f"spec_template:{feature_type}",
                category="spec_template",
                limit=1,
                user=effective_user,
            )
            if results and isinstance(results, list) and len(results) > 0:
                template = results[0]
                logger.info("Found template", feature_type=feature_type)
        except Exception as e:
            logger.warning("Template search failed", error=str(e))

    # Step 2: Generate questions using LLM
    lexora = LexoraAdapter(
        sse_url=_settings.lexora_url,
        timeout=_settings.lexora_timeout,
    )

    system_prompt = """あなたは仕様策定のスペシャリストです。
ユーザーの曖昧な要望から、実装に必要な具体的な仕様を引き出すための質問を生成します。

ルール:
1. 質問は3-5個に絞る（多すぎると負担）
2. 各質問には選択肢を2-4個用意する（ユーザーの負担軽減）
3. 選択肢には推奨マークをつける
4. 必要に応じてカスタム入力を許可する

出力形式（JSON）:
{
  "questions": [
    {
      "id": "q1",
      "question": "質問文",
      "options": [
        {"label": "選択肢1 (推奨)", "value": "value1"},
        {"label": "選択肢2", "value": "value2"}
      ],
      "allow_custom": true
    }
  ]
}"""

    template_hint = ""
    if template and isinstance(template, dict):
        template_hint = f"\n\n参考テンプレート:\n{json.dumps(template, ensure_ascii=False)}"

    user_prompt = f"""対象: {target}
要望: {initial_request}
機能タイプ: {feature_type or "未指定"}{template_hint}

この要望を実装するために必要な仕様を引き出す質問を生成してください。
JSON形式で出力してください。"""

    try:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        response = await lexora.chat(
            messages=messages,
            max_tokens=1000,
            temperature=0.3,
        )

        # Parse LLM response
        questions = _parse_questions_response(response)

    except Exception as e:
        logger.error("Question generation failed", error=str(e))
        # Fallback to generic questions
        questions = [
            {
                "id": "scope",
                "question": "変更のスコープは？",
                "options": [
                    {"label": "単一ファイル", "value": "single"},
                    {"label": "複数ファイル", "value": "multiple"},
                ],
                "allow_custom": False,
            },
            {
                "id": "priority",
                "question": "優先度は？",
                "options": [
                    {"label": "高（すぐに必要）", "value": "high"},
                    {"label": "中（今週中）", "value": "medium"},
                    {"label": "低（いつでも）", "value": "low"},
                ],
                "allow_custom": False,
            },
        ]

    # Store session
    _sessions[session_id] = {
        "target": target,
        "initial_request": initial_request,
        "feature_type": feature_type,
        "questions": questions,
        "answers": {},
        "status": "questions_ready",
    }

    return {
        "session_id": session_id,
        "status": "questions_ready",
        "questions": questions,
        "next_action": {
            "instruction": "AskUserQuestionツールを使って、以下の質問を順番にユーザーに提示してください。",
            "questions_for_user": questions,
        },
    }


async def generate_specification(
    session_id: str,
    answers: dict[str, Any],
    user: str = "",
) -> dict[str, Any]:
    """Generate a specification document from user answers.

    USE THIS WHEN: User has answered all questions from start_specification.
    This tool compiles answers into a structured specification.

    Args:
        session_id: Session ID from start_specification.
        answers: Dict mapping question IDs to user answers.
        user: User identifier for multi-user support (empty for default user).

    Returns:
        Dict containing:
        - success: Whether generation succeeded
        - specification: Generated specification document
        - required_permissions: List of permissions needed for implementation
        - estimated_files: List of files that will be modified
    """
    if _settings is None:
        raise RuntimeError("Settings not initialized")

    effective_user = user or get_current_user()

    if session_id not in _sessions:
        return {
            "success": False,
            "error": f"Session not found: {session_id}",
            "specification": None,
            "required_permissions": [],
            "estimated_files": [],
        }

    session = _sessions[session_id]
    session["answers"] = answers
    session["status"] = "generating"

    logger.info(
        "Generating specification",
        session_id=session_id,
        answers_count=len(answers),
        user=effective_user,
    )

    # Generate specification using LLM
    lexora = LexoraAdapter(
        sse_url=_settings.lexora_url,
        timeout=_settings.lexora_timeout,
    )

    system_prompt = """あなたは仕様書作成のスペシャリストです。
ユーザーの要望と回答から、実装可能な仕様書を生成します。

仕様書に含めるべき内容:
1. 目的: 何を達成するか
2. 対象: どのファイル/コンポーネントを変更するか
3. 要件: 具体的な実装要件（箇条書き）
4. 制約: 守るべき制約や注意点
5. テスト観点: 動作確認のポイント

出力形式（JSON）:
{
  "specification": {
    "title": "機能名",
    "purpose": "目的",
    "target_files": ["file1.py", "file2.py"],
    "requirements": ["要件1", "要件2"],
    "constraints": ["制約1"],
    "test_points": ["テスト1"]
  },
  "required_permissions": {
    "edit": ["path/to/file.py"],
    "bash": ["pytest:*"]
  }
}"""

    questions_and_answers = []
    for q in session["questions"]:
        qid = q["id"]
        answer = answers.get(qid, "未回答")
        questions_and_answers.append(f"Q: {q['question']}\nA: {answer}")

    user_prompt = f"""対象: {session['target']}
元の要望: {session['initial_request']}

質問と回答:
{chr(10).join(questions_and_answers)}

この情報を元に仕様書を生成してください。
JSON形式で出力してください。"""

    try:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        response = await lexora.chat(
            messages=messages,
            max_tokens=1500,
            temperature=0.2,
        )

        result = _parse_specification_response(response)
        session["status"] = "completed"
        session["specification"] = result.get("specification", {})

        return {
            "success": True,
            "specification": result.get("specification", {}),
            "required_permissions": result.get("required_permissions", {}),
            "estimated_files": result.get("specification", {}).get("target_files", []),
        }

    except Exception as e:
        logger.error("Specification generation failed", error=str(e))
        session["status"] = "failed"

        return {
            "success": False,
            "error": str(e),
            "specification": None,
            "required_permissions": {},
            "estimated_files": [],
        }


def _parse_questions_response(response: str) -> list[dict[str, Any]]:
    """Parse LLM response to extract questions."""
    try:
        # Try to find JSON in response
        start = response.find("{")
        end = response.rfind("}") + 1
        if start >= 0 and end > start:
            data = json.loads(response[start:end])
            if "questions" in data:
                return data["questions"]
    except json.JSONDecodeError:
        pass

    logger.warning("Failed to parse questions response, using fallback")
    return []


def _parse_specification_response(response: str) -> dict[str, Any]:
    """Parse LLM response to extract specification."""
    try:
        # Try to find JSON in response
        start = response.find("{")
        end = response.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(response[start:end])
    except json.JSONDecodeError:
        pass

    logger.warning("Failed to parse specification response")
    return {"specification": {}, "required_permissions": {}}


async def prepare_execution(
    specification: dict[str, Any],
    session_id: str = "",
    user: str = "",
) -> dict[str, Any]:
    """Analyze specification and prepare permissions for automated execution.

    USE THIS WHEN: You have a generated specification and want to prepare
    for automated execution by identifying required permissions.

    This tool converts the specification's required_permissions into
    Claude Code's allowedPrompts format for use with ExitPlanMode.

    Args:
        specification: The specification dict from generate_specification,
                      containing target_files, requirements, and required_permissions.
        session_id: Optional session ID for tracking.
        user: User identifier for multi-user support (empty for default user).

    Returns:
        Dict containing:
        - success: Whether preparation succeeded
        - allowed_prompts: List of permissions in Claude Code allowedPrompts format
        - summary: Human-readable summary of required permissions
        - next_action: Instructions for applying permissions
    """
    effective_user = user or get_current_user()

    logger.info(
        "Preparing execution",
        session_id=session_id or "none",
        has_specification=bool(specification),
        user=effective_user,
    )

    # Extract permissions from specification
    spec_data = specification.get("specification", specification)
    required_permissions = specification.get("required_permissions", {})
    target_files = spec_data.get("target_files", [])

    # Convert to Claude Code allowedPrompts format
    allowed_prompts: list[dict[str, str]] = []

    # Handle edit permissions
    edit_files = required_permissions.get("edit", [])
    if edit_files or target_files:
        files_to_edit = edit_files or target_files
        # Create semantic prompts for file editing
        for file_path in files_to_edit:
            allowed_prompts.append({
                "tool": "Bash",
                "prompt": f"edit {file_path}",
            })

    # Handle bash/command permissions
    bash_commands = required_permissions.get("bash", [])
    for cmd_pattern in bash_commands:
        # Convert patterns like "pytest:*" to semantic prompts
        if "pytest" in cmd_pattern.lower():
            allowed_prompts.append({
                "tool": "Bash",
                "prompt": "run tests",
            })
        elif "npm" in cmd_pattern.lower():
            allowed_prompts.append({
                "tool": "Bash",
                "prompt": "run npm commands",
            })
        elif "pip" in cmd_pattern.lower():
            allowed_prompts.append({
                "tool": "Bash",
                "prompt": "install dependencies",
            })
        else:
            # Generic command permission
            allowed_prompts.append({
                "tool": "Bash",
                "prompt": cmd_pattern.replace(":", " ").replace("*", "commands"),
            })

    # Handle read permissions if specified
    read_files = required_permissions.get("read", [])
    for file_path in read_files:
        allowed_prompts.append({
            "tool": "Bash",
            "prompt": f"read {file_path}",
        })

    # Deduplicate prompts
    seen = set()
    unique_prompts = []
    for prompt in allowed_prompts:
        key = (prompt["tool"], prompt["prompt"])
        if key not in seen:
            seen.add(key)
            unique_prompts.append(prompt)

    # Generate summary
    summary_parts = []
    if edit_files or target_files:
        files = edit_files or target_files
        summary_parts.append(f"Edit {len(files)} file(s): {', '.join(files[:3])}" +
                           ("..." if len(files) > 3 else ""))
    if bash_commands:
        summary_parts.append(f"Run commands: {', '.join(bash_commands[:3])}" +
                           ("..." if len(bash_commands) > 3 else ""))

    return {
        "success": True,
        "allowed_prompts": unique_prompts,
        "summary": "; ".join(summary_parts) if summary_parts else "No special permissions required",
        "permission_count": len(unique_prompts),
        "next_action": {
            "instruction": (
                "Use ExitPlanMode with allowedPrompts parameter to request "
                "these permissions from the user. Once approved, execution "
                "can proceed without further permission prompts."
            ),
            "example": {
                "tool": "ExitPlanMode",
                "allowedPrompts": unique_prompts,
            },
        },
    }


async def apply_permissions(
    allowed_prompts: list[dict[str, str]],
    scope: str = "session",
    project_path: str = "",
    user: str = "",
) -> dict[str, Any]:
    """Generate settings configuration for Claude Code permissions.

    USE THIS WHEN: You need to apply permissions to Claude Code settings.
    This tool generates the configuration that should be added to
    settings.local.json or passed to ExitPlanMode.

    Note: This tool does not directly modify settings files. It returns
    the configuration that Claude should apply using the appropriate method.

    Args:
        allowed_prompts: List of permissions in allowedPrompts format.
        scope: Permission scope - "session" (temporary) or "project" (persistent).
        project_path: Optional project path for project-scoped permissions.
        user: User identifier for multi-user support (empty for default user).

    Returns:
        Dict containing:
        - success: Whether generation succeeded
        - config: The configuration to apply
        - apply_method: Recommended method to apply permissions
        - instructions: Step-by-step instructions for applying
    """
    effective_user = user or get_current_user()

    logger.info(
        "Generating permission configuration",
        prompt_count=len(allowed_prompts),
        scope=scope,
        user=effective_user,
    )

    if not allowed_prompts:
        return {
            "success": True,
            "config": {},
            "apply_method": "none",
            "instructions": "No permissions to apply.",
        }

    # Generate configuration based on scope
    if scope == "session":
        # For session scope, use ExitPlanMode
        return {
            "success": True,
            "config": {
                "allowedPrompts": allowed_prompts,
            },
            "apply_method": "exit_plan_mode",
            "instructions": (
                "Pass the allowedPrompts to ExitPlanMode tool. "
                "These permissions will be active for the current plan execution."
            ),
            "example_usage": {
                "tool": "ExitPlanMode",
                "params": {
                    "allowedPrompts": allowed_prompts,
                },
            },
        }
    else:
        # For project scope, generate settings.local.json content
        settings_content = {
            "permissions": {
                "allow": allowed_prompts,
            },
        }

        return {
            "success": True,
            "config": settings_content,
            "apply_method": "settings_file",
            "file_path": f"{project_path}/.claude/settings.local.json" if project_path else ".claude/settings.local.json",
            "instructions": (
                "Add the following to your project's .claude/settings.local.json file. "
                "These permissions will persist across sessions for this project."
            ),
            "example_content": json.dumps(settings_content, indent=2, ensure_ascii=False),
        }


def register_tools(mcp: FastMCP, settings: Settings) -> None:
    """Register specification tools with the MCP server.

    Args:
        mcp: FastMCP server instance.
        settings: Application settings.
    """
    global _settings
    _settings = settings

    # Register the module-level functions as MCP tools
    mcp.tool()(start_specification)
    mcp.tool()(generate_specification)
    mcp.tool()(prepare_execution)
    mcp.tool()(apply_permissions)
