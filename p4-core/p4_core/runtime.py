from __future__ import annotations

import json
import os
import errno
import re
import signal
import subprocess
import sys
import time
import uuid
import ast
import hashlib
from pathlib import Path
from typing import Any, Iterable

from p4_core.frames import FrameManager
from p4_core.config_defaults import DEFAULT_TOOL_CONTENT_CHUNK_BYTES
from p4_core.models import ModelRouter
from p4_core.ollama_client import OllamaChatClient
from p4_core.output_contract import stdout_looks_like_user_visible_result
from p4_core.progress_block import ProgressBlockDecision
from p4_core.repo_map import build_repo_map, format_repo_map_for_prompt
from p4_core.schema_validation import validate_json_schema
from p4_core.planning import (
    CONSTRAINT_SATISFACTION_REQUIRED_CONTRACT,
    DYNAMIC_PROGRAMMING_REQUIRED_CONTRACT,
    PLAN_FIRST_ACTION_CONTENT_BYTES,
    PLAN_RECORD_SHAPE_HINT,
    plan_record_to_work_packages,
    plan_revision_reasons,
    planning_required_for_profile,
    profile_problem,
    plan_requires_revision,
    STATE_SPACE_REQUIRED_CONTRACT,
    validate_plan_record_contract,
)
from p4_core.schemas import NON_DECOMPOSE_ACTION_TOOLS, PLAN_ACCEPTANCE_SCHEMA, PLAN_RECORD_SCHEMA, TOOL_ACTION_NAMES, WORK_PACKAGE_SCHEMA, WORK_TYPES
from p4_core.runtime_profile import is_runtime_identity_query, runtime_identity_answer, runtime_profile_evidence
from p4_core.tools import ToolExecutor
from p4_core.workspace import (
    DEFAULT_CONFIG,
    WorkspacePaths,
    active_session_id,
    append_jsonl,
    append_prompt_snapshot,
    append_session_event,
    enqueue_message,
    now_iso,
    pop_next_queue_item,
    queue_items,
    read_json,
    read_jsonl,
    write_json,
)

def _merge_with_defaults(defaults: Any, override: Any) -> Any:
    if isinstance(defaults, dict) and isinstance(override, dict):
        merged = dict(defaults)
        for key, value in override.items():
            merged[key] = _merge_with_defaults(defaults.get(key), value) if key in defaults else value
        return merged
    return override if override is not None else defaults
from p4_core.grounding import (
    _artifact_path_is_python_implementation,
    _artifact_path_is_test,
    _finish_acceptance_evaluation,
    _finish_acceptance_contract,
    _finish_acceptance_evidence,
    _grounding_issues,
    _parse_grounding_judge_payload,
    _semantic_finish_acceptance_review,
    _semantic_grounding_check,
    _test_source_is_meaningful,
    _unittest_command_is_acceptance,
)
from p4_core.guards import (
    _classify_failure,
    _expected_artifacts,
    _extract_requested_commands,
    _failed_command_guardrail,
    _missing_expected_artifacts,
    _missing_requested_commands,
    _redundant_command_reason,
    _similar_command_warning,
)
from p4_core.llm_comm import (
    _chat_with_repair,
    _classify_llm_parse_issue,
    _extract_json_object,
    _extract_stream_metadata,
    _format_llm_stream_text,
    _json_repair_prompt,
    _looks_like_in_progress_write_file_content_stream,
    _looks_like_plan_record_embedded_edit_stream,
    _looks_like_repetitive_machine_control_output,
    _parse_issue_should_exit_repair_loop,
    _tail_stream_text,
    _thinking_only_repair_prompt,
    _looks_like_structured_envelope,
    _looks_like_truncated_json,
    _machine_control_stream_stop_reason,
    _raw_is_exact_json_object,
    _raw_contains_json_object,
)
from p4_core.observer import (
    _context_risk_summary,
    _deterministic_step_commentary,
    _maybe_record_observer_note,
    _observer_enabled,
    _record_observer_judgement_note,
    _record_observer_llm_output_issue_note,
    _usable_japanese_observer_text,
)
from p4_core.prompts import (
    _build_deliberation_note,
    _build_planning_note,
    _build_prompt,
    _compact_context_text,
    _current_phase,
    _deliberation_reasons,
    _output_budget_prompt,
    _reflection_relevant_to_user,
    _reflection_prompt_block,
    _file_context_preview,
    _run_command_traceback_summary,
    _render_action_context_events,
    _render_tool_result_context,
    _system_prompt,
    _judge_feedback_context_text,
)
from p4_core.terminal import (
    _command_stdout_can_answer_request,
    _controller_terminal_finish,
    _deterministic_terminal_final_answer,
    _empty_stdout_command_can_answer_request,
    _normalize_run_command_evidence,
    _output_preview,
    _preferred_shell_from_extra_prompt,
    _resolve_terminal_model,
    _stdout_result_block,
    _synthesize_terminal_final_answer,
    _terminal_answer_is_direct_evidence,
    run_terminal_agent,
)
from p4_core.unittest_repair import build_unittest_failed_allowed_actions


_UNSET = object()
WORK_PACKAGE_TYPES = set(WORK_TYPES)
NON_DECOMPOSE_ACTION_TOOL_SET = set(NON_DECOMPOSE_ACTION_TOOLS)
FRAME_OPERATION_TOOL_SET = {"create_plan", "decompose_tasks", "open_child_frame", "return_to_parent"}
IMPLEMENTATION_ROUTE_PROMPT_CONTEXT_ROUTES: set[str] = set()
IMPLEMENTATION_ROUTE_PROMPT_CONTEXT_PHASES = {
    "implementation_required",
    "implementation_contract_review",
    "tests_required",
    "test_artifact_semantic_repair",
    "traceback_targeted_repair",
    "unittest_required",
}
ROUTE_PROMPT_OMITTED_PAYLOAD_KEYS = {"content", "old_text", "new_text"}


class AgentRuntime:
    def __init__(self, root: Path, *, llm_backend: Any | None = None) -> None:
        self.root = Path(root).expanduser().resolve()
        self.paths = WorkspacePaths(self.root)
        loaded_config = read_json(self.paths.config_path, fallback={})
        self.config = _merge_with_defaults(DEFAULT_CONFIG, loaded_config)
        self.router = ModelRouter(self.config.get("models", {}))
        self.llm_backend = llm_backend or OllamaChatClient(base_url=str(self.config.get("ollama_base_url") or "http://127.0.0.1:11434"))
        self.ollama_options = dict(self.config.get("ollama_options", {}))
        self.runtime_config = dict(self.config.get("runtime", {}))
        execution_root = str(self.runtime_config.get("execution_root") or self.root)
        self.base_execution_root = Path(execution_root).expanduser().resolve()
        self.execution_root = self.base_execution_root
        self.tool_content_chunk_bytes = int(self.runtime_config.get("tool_content_chunk_bytes") or DEFAULT_TOOL_CONTENT_CHUNK_BYTES)
        self.tools = ToolExecutor(self.execution_root, content_chunk_max_bytes=self.tool_content_chunk_bytes)
        self.frame_manager = FrameManager(self.root)
        self._last_grounding_judge_trace: dict[str, Any] | None = None


    def _observed_model_names(self) -> tuple[list[str], bool]:
        if hasattr(self.llm_backend, "list_models"):
            try:
                payload = self.llm_backend.list_models()
                return [
                    str(item.get("name") or "").strip()
                    for item in payload.get("models") or []
                    if isinstance(item, dict) and str(item.get("name") or "").strip()
                ], True
            except Exception:
                return [], False
        return [], False

    def available_model_names(self) -> list[str]:
        observed, _observed_ok = self._observed_model_names()
        configured = [
            self.router._normalize_model_name(str(value or ""))
            for value in self.router.models.values()
            if str(value or "").strip()
        ]
        return list(dict.fromkeys([*observed, *configured]))

    def _operator_model_selection(self, model: str, *, model_role: str = "coding") -> dict[str, str]:
        role = str(model_role or "coding").strip()
        if role not in self.router.models:
            role = "coding"
        requested = str(model or "").strip()
        if not requested:
            raise ValueError("model must not be empty")
        normalized = self.router._normalize_model_name(requested)
        observed, observed_ok = self._observed_model_names()
        available = self.available_model_names()
        if observed_ok and observed and normalized not in observed and requested not in observed:
            available_text = ", ".join(observed)
            raise ValueError(f"model is not visible in ollama list: {normalized}. available_models={available_text}")
        selected = normalized if not available or normalized in available else requested
        return {
            "role": role,
            "model": selected,
            "reason": (
                "operator selected model from ollama list"
                if observed_ok
                else "operator selected model; ollama list unavailable in this process"
            ),
        }

    def _append_session_event(self, *args: Any) -> dict[str, Any]:
        if len(args) == 2:
            session_id, payload = args
        elif len(args) == 3:
            _root, session_id, payload = args
        else:
            raise TypeError("_append_session_event expects session_id,payload or root,session_id,payload")
        if "operation_id" not in payload:
            runtime_status = read_json(self.paths.runtime_status_path, fallback={})
            operation_id = str(runtime_status.get("current_operation_id") or "")
            if operation_id:
                payload = {**payload, "operation_id": operation_id}
        if payload.get("type") != "user_message":
            current_frame = self.frame_manager.current_frame()
            if current_frame is not None:
                if "frame_id" not in payload and "child_frame_id" not in payload:
                    payload = {**payload, "frame_id": current_frame.frame_id}
                if "frame_depth" not in payload and "depth" not in payload:
                    payload = {**payload, "frame_depth": current_frame.depth}
        event = append_session_event(self.root, session_id, payload)
        if payload.get("type") not in {"user_message"}:
            self.frame_manager.append_event(event)
        return event

    def _semantic_review_state_events(self, *, session_id: str) -> list[dict[str, Any]]:
        """Return review-relevant events without being skewed by stream volume.

        Runtime event streams can add thousands of rows between two control
        decisions. Semantic review dedupe and gating must use the canonical
        session log, filtered to the control facts that matter here: artifact
        tool results and semantic review notes.
        """
        path = self.paths.session_events_path(session_id)
        if not path.exists():
            return []
        events: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                event_type = str(event.get("type") or "")
                if event_type == "tool_result":
                    events.append(event)
                    continue
                if event_type == "system_note" and str(event.get("code") or "") in {"semantic_implementation_review", "edit_blocked", "implementation_task_progress_blocked"}:
                    events.append(event)
        return events

    def _append_runtime_event(
        self,
        session_id: str | None,
        *,
        event_name: str,
        content: str = "",
        details: dict[str, Any] | None = None,
        turn_id: str | None = None,
        queue_id: str | None = None,
        step_index: int | None = None,
        llm_workspace: str | None = None,
        phase: str | None = None,
    ) -> dict[str, Any] | None:
        if not session_id:
            return None
        payload: dict[str, Any] = {
            "type": "runtime_event",
            "role": "system",
            "event_name": event_name,
            "content": str(content or ""),
            "details": details or {},
        }
        if turn_id is not None:
            payload["turn_id"] = turn_id
        if queue_id is not None:
            payload["queue_id"] = queue_id
        if step_index is not None:
            payload["step_index"] = step_index
        if llm_workspace is not None:
            payload["llm_workspace"] = llm_workspace
        if phase is not None:
            payload["phase"] = phase
        current_frame = self.frame_manager.current_frame()
        if current_frame is not None:
            payload["frame_id"] = current_frame.frame_id
            payload["frame_depth"] = current_frame.depth
        return self._append_session_event(session_id, payload)

    def _start_turn_frame(self, *, user_message: str, resume_existing: bool = False) -> None:
        if resume_existing and self.frame_manager.current_frame() is not None:
            self.frame_manager.reset_active_stack_steps()
            self.frame_manager.append_event(
                {
                    "type": "user_message",
                    "role": "user",
                    "content": user_message,
                    "timestamp": now_iso(),
                }
            )
            return
        if self.frame_manager.frames:
            self.frame_manager.abandon_all()
        frame = self.frame_manager.create_root_frame(user_message)
        self.frame_manager.append_event(
            {
                "type": "user_message",
                "role": "user",
                "content": user_message,
                "timestamp": now_iso(),
            }
        )

    def _frame_snapshot(self) -> dict[str, Any]:
        return self.frame_manager.snapshot()

    def _normalize_child_tasks(self, raw_tasks: Any) -> list[dict[str, Any]]:
        tasks: list[dict[str, Any]] = []
        for index, item in enumerate(raw_tasks if isinstance(raw_tasks, list) else [], start=1):
            if isinstance(item, dict):
                goal = str(item.get("goal") or item.get("task") or "").strip()
                context_summary = str(item.get("context_summary") or item.get("context") or "").strip()
                done_when = str(item.get("done_when") or item.get("completion_criteria") or "").strip()
                work_type = str(item.get("work_type") or "").strip()
                first_action = item.get("first_action") or {}
                success_evidence = item.get("success_evidence") or item.get("done_when") or item.get("completion_criteria") or ""
                why_not_direct_action = str(item.get("why_not_direct_action") or "").strip()
            else:
                goal = str(item or "").strip()
                context_summary = ""
                done_when = ""
                work_type = ""
                first_action = {}
                success_evidence = ""
                why_not_direct_action = ""
            if not goal:
                continue
            tasks.append(
                {
                    "task_id": f"task-{index}",
                    "goal": goal,
                    "work_type": work_type,
                    "first_action": first_action,
                    "success_evidence": success_evidence,
                    "why_not_direct_action": why_not_direct_action,
                    "context_summary": context_summary,
                    "done_when": done_when,
                    "status": "pending",
                }
            )
        return tasks

    def _normalize_work_package(self, raw: Any) -> dict[str, Any]:
        data = dict(raw or {}) if isinstance(raw, dict) else {}
        first_action = data.get("first_action") or {}
        if not isinstance(first_action, dict):
            first_action = {}
        first_action = {
            "tool": str(first_action.get("tool") or "").strip(),
            "args": dict(first_action.get("args") or {}) if isinstance(first_action.get("args") or {}, dict) else {},
        }
        evidence = data.get("success_evidence") or data.get("done_when") or ""
        if isinstance(evidence, list):
            success_evidence = [str(item).strip() for item in evidence if str(item).strip()]
        else:
            success_evidence = str(evidence or "").strip()
        return {
            "goal": str(data.get("goal") or "").strip(),
            "work_type": str(data.get("work_type") or "").strip(),
            "first_action": first_action,
            "success_evidence": success_evidence,
            "why_not_direct_action": str(data.get("why_not_direct_action") or "").strip(),
            "context_summary": str(data.get("context_summary") or data.get("context") or "").strip(),
            "done_when": str(data.get("done_when") or "").strip(),
            "task_id": str(data.get("task_id") or data.get("child_task_id") or "").strip(),
            "status": str(data.get("status") or "pending").strip(),
        }

    def _work_package_issues(self, work_package: dict[str, Any]) -> list[str]:
        issues: list[str] = []
        schema_validation = validate_json_schema(work_package, WORK_PACKAGE_SCHEMA)
        issues.extend(schema_validation.errors)
        if not str(work_package.get("goal") or "").strip():
            issues.append("goal is required")
        work_type = str(work_package.get("work_type") or "").strip()
        if work_type not in WORK_PACKAGE_TYPES:
            issues.append(f"work_type must be one of {sorted(WORK_PACKAGE_TYPES)}")
        first_action = work_package.get("first_action") or {}
        if not isinstance(first_action, dict) or not str(first_action.get("tool") or "").strip():
            issues.append("first_action.tool is required")
        elif str(first_action.get("tool") or "").strip() not in NON_DECOMPOSE_ACTION_TOOL_SET:
            issues.append(f"first_action.tool must be a non-decomposition tool: {sorted(NON_DECOMPOSE_ACTION_TOOL_SET)}")
        if not isinstance(first_action.get("args") if isinstance(first_action, dict) else None, dict):
            issues.append("first_action.args must be an object")
        else:
            first_action_tool = str(first_action.get("tool") or "").strip()
            first_action_args = dict(first_action.get("args") or {})
            if first_action_tool in NON_DECOMPOSE_ACTION_TOOL_SET:
                for issue in self.tools.argument_issues(first_action_tool, first_action_args):
                    issues.append(f"first_action.args: {issue}")
            if work_type == "edit" and first_action_tool == "run_command":
                issues.append(
                    "edit WorkUnit first_action must not be a no-op run_command. "
                    "Use list_files/read_file/search_code to inspect, then perform the actual edit inside PLAN_EXECUTION."
                )
            if work_type == "run_test" and first_action_tool != "run_command":
                issues.append("run_test WorkUnit first_action must be run_command with the concrete verification command")
            if work_type == "run_test" and first_action_tool == "run_command":
                command = str(first_action_args.get("command") or "").strip()
                direct_python_script = bool(
                    re.fullmatch(r"(?:python|python3)(?:\s+[-\w]+)*\s+[\w./-]+\.py(?:\s+.*)?", command)
                )
                command_lower = command.lower()
                if "unittest" in command_lower and not (
                    "discover" in command_lower
                    and "-s tests" in command_lower
                ):
                    issues.append(
                        "run_test WorkUnit first_action must run unittest through `python3 -m unittest discover -s tests` "
                        "so verifier evidence is tied to the runtime test artifact contract."
                    )
                if direct_python_script and "unittest" not in command and "pytest" not in command and " -c " not in command:
                    issues.append(
                        "run_test WorkUnit first_action must be a non-interactive verification command. "
                        "Use `python3 -m unittest discover -s tests`, `pytest`, or a `python3 -c` verifier that calls the programmatic API; "
                        "do not use direct `python script.py` as the planned verification action."
                    )
            first_action_path = str(first_action_args.get("path") or "").strip()
            if first_action_tool in {"write_file", "append_file", "replace_text"}:
                first_action_content = (
                    str(first_action_args.get("new_text") or "")
                    if first_action_tool == "replace_text"
                    else str(first_action_args.get("content") or "")
                )
                normalized_path = first_action_path.replace("\\", "/")
                if normalized_path.endswith("tests/__init__.py"):
                    issues.append("first_action.path tests/__init__.py is only a package marker; write an actual test_*.py file for test evidence")
                elif _artifact_path_is_test(first_action_path) and not _test_source_is_meaningful(first_action_content):
                    issues.append("first_action.content for test file must contain meaningful unittest assertions, not pass-only tests")
                elif normalized_path.endswith(".py") and self._python_source_has_pass_only_callable(first_action_content):
                    issues.append("first_action.content for Python implementation must not contain pass-only functions or placeholder returns")
        evidence = work_package.get("success_evidence")
        if not evidence:
            issues.append("success_evidence is required")
        why_not_direct_action = str(work_package.get("why_not_direct_action") or "").strip()
        if not why_not_direct_action:
            issues.append("why_not_direct_action is required")
        elif self._why_not_direct_action_denies_delegation(why_not_direct_action):
            issues.append("why_not_direct_action must explain why delegation is necessary, not say the parent can directly execute first_action")
        return issues

    def _why_not_direct_action_denies_delegation(self, why_not_direct_action: str) -> bool:
        normalized = re.sub(r"\s+", "", str(why_not_direct_action or "").lower())
        denial_markers = [
            "直接実行可能",
            "直接実行できる",
            "直接実行すべき",
            "親が実行可能",
            "親が直接",
            "directlyexecutable",
            "directlyexecute",
            "parentcandirectly",
            "currentframecandirectly",
        ]
        return any(marker in normalized for marker in denial_markers)

    def _python_source_placeholder_markers(self, source: str) -> list[str]:
        text = str(source or "")
        incomplete_comment_markers = [
            "ここに実装",
            "ここに実際",
            "未実装",
            "簡単な実装",
            "テスト用の実装",
            "今回は簡単",
            "placeholder implementation",
            "for now",
            "for a full implementation",
            "real implementation",
            "actual implementation",
        ]
        if not any(
            marker in text
            for marker in [
                "pass",
                "TODO",
                "todo",
                "...",
                "NotImplemented",
                "return None",
                "return []",
                "placeholder",
                *incomplete_comment_markers,
            ]
        ):
            return []
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return []
        markers: list[str] = []
        lower_text = text.lower()
        if any(marker in text for marker in ["TODO", "todo", "ここに実装", "未実装"]):
            if any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) for node in ast.walk(tree)):
                markers.append("TODO")
        if any(marker.lower() in lower_text for marker in incomplete_comment_markers):
            if any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) for node in ast.walk(tree)):
                markers.append("incomplete-comment")
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = [
                stmt
                for stmt in node.body
                if not (
                    isinstance(stmt, ast.Expr)
                    and isinstance(getattr(stmt, "value", None), ast.Constant)
                    and isinstance(stmt.value.value, str)
                )
            ]
            if len(body) == 1 and isinstance(body[0], ast.Pass):
                markers.append(f"{node.name}: pass-only")
                continue
            if (
                len(body) == 1
                and isinstance(body[0], ast.Expr)
                and isinstance(getattr(body[0], "value", None), ast.Constant)
                and body[0].value.value is Ellipsis
            ):
                markers.append(f"{node.name}: ellipsis-only")
                continue
            if (
                len(body) == 1
                and isinstance(body[0], ast.Raise)
                and isinstance(getattr(body[0], "exc", None), ast.Call)
                and getattr(body[0].exc.func, "id", "") == "NotImplementedError"
            ):
                markers.append(f"{node.name}: NotImplementedError-only")
                continue
            if len(body) == 1 and isinstance(body[0], ast.Return):
                value = body[0].value
                if value is None:
                    markers.append(f"{node.name}: bare return-only")
                    continue
                if isinstance(value, ast.Constant) and value.value is None:
                    markers.append(f"{node.name}: return None-only")
                    continue
                if isinstance(value, ast.List) and not value.elts:
                    markers.append(f"{node.name}: return []-only")
                    continue
        return sorted(set(markers))

    def _python_source_placeholder_repair_hints(
        self,
        source: str,
        *,
        max_hints: int = 5,
    ) -> list[dict[str, Any]]:
        """Return exact local edit anchors for placeholder-only callables."""
        text = str(source or "")
        if not text.strip():
            return []
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return []
        lines = text.splitlines()
        hints: list[dict[str, Any]] = []

        def snippet(start_line: int, end_line: int) -> str:
            start = max(1, start_line)
            end = max(start, end_line)
            return "\n".join(lines[start - 1 : end])

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = [
                stmt
                for stmt in node.body
                if not (
                    isinstance(stmt, ast.Expr)
                    and isinstance(getattr(stmt, "value", None), ast.Constant)
                    and isinstance(stmt.value.value, str)
                )
            ]
            if len(body) != 1:
                continue
            only = body[0]
            reason = ""
            if isinstance(only, ast.Pass):
                reason = f"{node.name} is pass-only"
            elif (
                isinstance(only, ast.Expr)
                and isinstance(getattr(only, "value", None), ast.Constant)
                and only.value.value is Ellipsis
            ):
                reason = f"{node.name} is ellipsis-only"
            elif (
                isinstance(only, ast.Raise)
                and isinstance(getattr(only, "exc", None), ast.Call)
                and getattr(only.exc.func, "id", "") == "NotImplementedError"
            ):
                reason = f"{node.name} raises only NotImplementedError"
            elif isinstance(only, ast.Return):
                value = only.value
                if value is None:
                    reason = f"{node.name} has only bare return"
                elif isinstance(value, ast.Constant) and value.value is None:
                    reason = f"{node.name} returns only None"
                elif isinstance(value, ast.List) and not value.elts:
                    reason = f"{node.name} returns only []"
            if not reason:
                continue
            end_line = int(getattr(only, "end_lineno", None) or getattr(only, "lineno", 0) or 0)
            start_line = int(getattr(node, "lineno", None) or end_line or 0)
            old_text = snippet(start_line, end_line) if start_line and end_line else ""
            hints.append(
                {
                    "line": int(getattr(only, "lineno", None) or start_line or 0),
                    "current_text": old_text or reason,
                    "reason": reason
                    + "; replace this placeholder callable before editing unrelated functions or tests",
                    "suggested_action": "replace_placeholder_callable",
                    "suggested_old_text": old_text,
                    "suggested_strategy": (
                        "Use replace_text with this exact small function-local old_text, "
                        "or write_file a complete placeholder-free implementation if the local edit cannot express the fix."
                    ),
                }
            )
            if len(hints) >= max_hints:
                break
        return hints

    def _python_source_has_pass_only_callable(self, source: str) -> bool:
        return bool(self._python_source_placeholder_markers(source))

    def _python_source_incomplete_implementation_markers(self, source: str) -> list[str]:
        text = str(source or "")
        if "pass" not in text:
            return []
        lower_text = text.lower()
        incomplete_language_markers = [
            "placeholder",
            "next step",
            "rewrite",
            "refactor",
            "fix this",
            "fix below",
            "will fix",
            "will rewrite",
            "gets complex",
            "getting complex",
            "let's stick",
            "for now",
            "後で",
            "未完成",
            "未実装",
        ]
        if not any(marker in lower_text for marker in incomplete_language_markers):
            return []
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return []
        markers: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if any(isinstance(child, ast.Pass) for child in ast.walk(node)):
                markers.append(f"{node.name}: incomplete pass")
        return sorted(set(markers))

    def _python_source_has_test_callable(self, source: str) -> bool:
        try:
            tree = ast.parse(str(source or ""))
        except SyntaxError:
            return False
        return any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test")
            for node in ast.walk(tree)
        )

    def _python_source_narrows_requested_input_contract(self, *, user_message: str, source: str) -> str:
        """Return a source-contract issue when code narrows a user API shape.

        The runtime must not solve the domain problem, but it can protect
        explicit public API contracts such as identifier-preserving mapping inputs.
        """
        score = self._python_source_narrowed_input_contract_score(
            user_message=user_message,
            source=source,
        )
        if score <= 0:
            return ""
        text = str(source or "")
        parameter_pattern = self._source_public_parameter_pattern(text)
        if parameter_pattern and re.search(rf"\benumerate\s*\(\s*{parameter_pattern}\s*\)", text):
            return (
                "ユーザー要求はIDを保持するmapping入力です。"
                "実装がmappingをlist/enumerateの内部indexへ置き換えており、callerが渡したIDを壊します。"
                "mapping.items() で元IDを保持してください。"
            )
        sorted_public_input = bool(
            parameter_pattern and re.search(rf"\bsorted\s*\(\s*{parameter_pattern}\s*\)", text)
        )
        sorted_identifier_like = bool(re.search(r"\bsorted\s*\(\s*(?:[A-Za-z_][A-Za-z0-9_]*ids?|keys)\s*\)", text))
        if sorted_public_input or sorted_identifier_like:
            return (
                "ユーザー要求はIDを保持するmapping入力です。"
                "実装がID集合を sorted(...) しており、比較不能な任意IDで失敗します。"
                "IDには順序を要求せず、set/dictの反復だけで扱ってください。"
            )
        return (
            "ユーザー要求はIDを保持するmapping入力です。"
            "実装のpublic APIや解の型を int / str / sequence index などの特定ID型に固定しており、入力契約を狭めています。"
        )

    def _identifier_mapping_input_contract_requested(self, user_message: str) -> bool:
        user_text = str(user_message or "")
        lower_user_text = user_text.lower()
        id_mapping_requested = bool(
            re.search(
                r"\b[a-z][a-z0-9_]*_id\s*->\s*(?:set|list|tuple|dict)?\s*\(?\s*[a-z][a-z0-9_]*_id\b",
                lower_user_text,
            )
        )
        arbitrary_identifier_requested = (
            ("任意id" in lower_user_text or "任意 id" in lower_user_text or "arbitrary id" in lower_user_text)
            and ("dict" in lower_user_text or "mapping" in lower_user_text or "set(" in lower_user_text)
        )
        return id_mapping_requested or arbitrary_identifier_requested

    def _source_public_parameter_names(self, source: str) -> set[str]:
        try:
            tree = ast.parse(str(source or ""))
        except SyntaxError:
            return set()

        def parameter_names(function: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
            args = list(function.args.posonlyargs) + list(function.args.args) + list(function.args.kwonlyargs)
            if function.args.vararg is not None:
                args.append(function.args.vararg)
            if function.args.kwarg is not None:
                args.append(function.args.kwarg)
            return {arg.arg for arg in args if arg.arg not in {"self", "cls"}}

        names: set[str] = set()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.update(parameter_names(node))
            elif isinstance(node, ast.ClassDef):
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and not child.name.startswith("_"):
                        names.update(parameter_names(child))
        return names

    def _source_public_parameter_pattern(self, source: str) -> str:
        names = sorted(self._source_public_parameter_names(source), key=len, reverse=True)
        if not names:
            return ""
        return "(?:" + "|".join(re.escape(name) for name in names) + ")"

    def _python_source_narrowed_input_contract_score(self, *, user_message: str, source: str) -> int:
        """Count observable type/API narrowings for identifier-preserving inputs."""
        if not self._identifier_mapping_input_contract_requested(user_message):
            return 0
        text = str(source or "")
        parameter_pattern = self._source_public_parameter_pattern(text)
        primitive_fixed_patterns = [
            r"\bDict\s*\[\s*int\s*,\s*Set\s*\[\s*int\s*\]\s*\]",
            r"\bdict\s*\[\s*int\s*,\s*set\s*\[\s*int\s*\]\s*\]",
            r"\bDict\s*\[\s*str\s*,\s*Set\s*\[\s*str\s*\]\s*\]",
            r"\bdict\s*\[\s*str\s*,\s*set\s*\[\s*str\s*\]\s*\]",
            r"\bList\s*\[\s*int\s*\]",
            r"\blist\s*\[\s*int\s*\]",
            r"\bList\s*\[\s*str\s*\]",
            r"\blist\s*\[\s*str\s*\]",
            r"\bTuple\s*\[\s*int\b",
            r"\btuple\s*\[\s*int\b",
            r"\bTuple\s*\[\s*str\b",
            r"\btuple\s*\[\s*str\b",
            r"\bUnion\s*\[\s*int\s*,\s*str\s*\]",
            r"\bUnion\s*\[\s*str\s*,\s*int\s*\]",
            r"\bint\s*\|\s*str\b",
            r"\bstr\s*\|\s*int\b",
        ]
        score = sum(len(re.findall(pattern, text)) for pattern in primitive_fixed_patterns)
        if parameter_pattern and re.search(rf"\benumerate\s*\(\s*{parameter_pattern}\s*\)", text):
            score += 1
        sorted_public_input = bool(
            parameter_pattern and re.search(rf"\bsorted\s*\(\s*{parameter_pattern}\s*\)", text)
        )
        sorted_identifier_like = bool(re.search(r"\bsorted\s*\(\s*(?:[A-Za-z_][A-Za-z0-9_]*ids?|keys)\s*\)", text))
        if sorted_public_input or sorted_identifier_like:
            score += 1
        return score

    def _relaxed_identifier_mapping_line(self, line: str) -> str:
        relaxed = str(line or "")
        relaxed = re.sub(r"(?<=\[)\s*(?:int|str)\s*(?=[,\]])", "Any", relaxed)
        relaxed = re.sub(r"\b(?:int\s*\|\s*str|str\s*\|\s*int)\b", "Any", relaxed)
        relaxed = re.sub(r"\bUnion\s*\[\s*(?:int\s*,\s*str|str\s*,\s*int)\s*\]", "Any", relaxed)
        return relaxed

    def _python_source_input_contract_repair_hints(
        self,
        *,
        user_message: str,
        source: str,
        max_hints: int = 5,
    ) -> list[dict[str, Any]]:
        """Return source-local generic hints for identifier mapping repairs."""
        if not self._identifier_mapping_input_contract_requested(user_message):
            return []
        text = str(source or "")
        if not text:
            return []
        parameter_pattern = self._source_public_parameter_pattern(text)
        restricted_annotation_patterns = [
            r"\b(?:Dict|dict)\s*\[\s*(?:int|str)\s*,\s*(?:Set|set)\s*\[\s*(?:int|str)\s*\]\s*\]",
            r"\b(?:List|list)\s*\[\s*(?:int|str)\s*\]",
            r"\b(?:Tuple|tuple)\s*\[\s*(?:int|str)\b",
            r"\bUnion\s*\[\s*(?:int\s*,\s*str|str\s*,\s*int)\s*\]",
            r"\b(?:int\s*\|\s*str|str\s*\|\s*int)\b",
        ]
        hints: list[dict[str, Any]] = []
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if not stripped:
                continue
            reason = ""
            suggested = ""
            if any(re.search(pattern, line) for pattern in restricted_annotation_patterns):
                reason = "public API/result annotation narrows caller-supplied identifiers to primitive ID types"
                suggested = self._relaxed_identifier_mapping_line(line)
            elif parameter_pattern and re.search(rf"\benumerate\s*\(\s*{parameter_pattern}\s*\)", line):
                reason = "enumerate(...) converts caller IDs to positional indexes"
                suggested = re.sub(r"\benumerate\s*\(", "mapping.items() から元keyを保持する形に変更: enumerate(", line)
            elif re.search(r"\blist\s*\(\s*range\s*\(\s*len\s*\(", line):
                reason = "range(len(...)) returns positional indexes instead of caller IDs"
                suggested = re.sub(r"list\s*\(\s*range\s*\(\s*len\s*\(([^)]*)\)\s*\)\s*\)", r"list(\1.keys())", line)
            elif (
                (parameter_pattern and re.search(rf"\bsorted\s*\(\s*{parameter_pattern}\s*\)", line))
                or re.search(r"\bsorted\s*\(\s*(?:[A-Za-z_][A-Za-z0-9_]*ids?|keys)\s*\)", line)
            ):
                reason = "sorted(...) requires comparable IDs and changes the arbitrary-ID contract"
            if not reason:
                continue
            hint: dict[str, Any] = {
                "line": lineno,
                "current_text": stripped,
                "reason": reason,
            }
            if suggested and suggested != line:
                hint["suggested_new_text"] = suggested.strip()
            hints.append(hint)
            if len(hints) >= max_hints:
                break
        if hints and "Any" in json.dumps(hints, ensure_ascii=False) and "Any" not in text:
            hints.append(
                {
                    "line": 0,
                    "current_text": "typing imports",
                    "reason": "relaxed annotations need Any or an equivalent generic identifier type",
                    "suggested_new_text": "add Any to the typing import, or remove the narrowing annotation",
                }
            )
        return hints[:max_hints]

    def _python_source_mutates_requested_input_collections(self, *, user_message: str, source: str) -> str:
        user_text = str(user_message or "").lower()
        mapping_input_requested = bool(
            re.search(
                r"\b[a-z][a-z0-9_]*_id\s*->\s*(?:set|list|tuple|dict)?\s*\(?\s*[a-z][a-z0-9_]*_id\b",
                user_text,
            )
        ) or (
            ("dict" in user_text or "mapping" in user_text)
            and ("入力" in user_text or "input" in user_text)
            and ("set(" in user_text or "set[" in user_text or "集合" in user_text)
        )
        if not mapping_input_requested:
            return ""
        try:
            tree = ast.parse(str(source or ""))
        except SyntaxError:
            return ""
        mutating_methods = {
            "add",
            "append",
            "clear",
            "difference_update",
            "discard",
            "extend",
            "pop",
            "popitem",
            "remove",
            "setdefault",
            "update",
        }

        def parameter_names(function: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
            args = list(function.args.posonlyargs) + list(function.args.args) + list(function.args.kwonlyargs)
            if function.args.vararg is not None:
                args.append(function.args.vararg)
            if function.args.kwarg is not None:
                args.append(function.args.kwarg)
            return {arg.arg for arg in args}

        def walk_function_body(function: ast.FunctionDef | ast.AsyncFunctionDef) -> Iterable[ast.AST]:
            stack = list(reversed(function.body))
            while stack:
                node = stack.pop()
                yield node
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                    continue
                stack.extend(reversed(list(ast.iter_child_nodes(node))))

        def root_name(node: ast.AST) -> str:
            current = node
            while isinstance(current, (ast.Subscript, ast.Attribute)):
                current = current.value
            return current.id if isinstance(current, ast.Name) else ""

        def aliases_input_parameter(node: ast.AST, input_names: set[str]) -> bool:
            return isinstance(node, ast.Name) and node.id in input_names

        public_functions: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                public_functions.append(node)
            elif isinstance(node, ast.ClassDef):
                public_functions.extend(
                    child
                    for child in node.body
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and not child.name.startswith("_")
                )
        for function in public_functions:
            input_names = {
                name
                for name in parameter_names(function)
                if name not in {"self", "cls"}
            }
            if not input_names:
                continue
            for node in walk_function_body(function):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and aliases_input_parameter(node.value, input_names):
                            input_names.add(target.id)
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in mutating_methods
                    and root_name(node.func.value) in input_names
                ):
                    return (
                        "実装が caller-owned public API 入力、または入力内のmutable collectionを直接変更しています。"
                        "検証や複数回探索で入力が破壊されるため、caller-owned inputは不変として扱い、"
                        "必要なら local copy に別名で複製してから探索状態を更新してください。"
                    )
                if isinstance(node, (ast.Delete, ast.AugAssign, ast.Assign)):
                    targets: list[ast.AST] = []
                    if isinstance(node, ast.Delete):
                        targets = list(node.targets)
                    elif isinstance(node, ast.AugAssign):
                        targets = [node.target]
                    elif isinstance(node, ast.Assign):
                        targets = list(node.targets)
                    for target in targets:
                        if isinstance(target, ast.Subscript) and root_name(target.value) in input_names:
                            return (
                                "実装が caller-owned public API 入力の要素を直接代入/削除しています。"
                                "caller-owned inputは不変として扱い、必要なら local copy に別名で複製してから探索状態を更新してください。"
                            )
        return ""

    def _python_source_has_recursive_destructive_shared_state(self, source: str) -> str:
        try:
            tree = ast.parse(str(source or ""))
        except SyntaxError:
            return ""
        high_risk_methods = {
            "difference_update",
            "intersection_update",
            "popitem",
            "symmetric_difference_update",
        }
        state_name_markers = (
            "active",
            "available",
            "candidate",
            "closed",
            "frontier",
            "open",
            "pending",
            "remaining",
            "state",
            "visited",
        )

        def root_name(node: ast.AST) -> str:
            current = node
            while isinstance(current, (ast.Subscript, ast.Attribute)):
                current = current.value
            return current.id if isinstance(current, ast.Name) else ""

        def snapshot_source_root(node: ast.AST) -> str:
            if not isinstance(node, ast.Call):
                return ""
            if isinstance(node.func, ast.Name) and node.func.id in {"dict", "list", "set", "tuple"} and node.args:
                return root_name(node.args[0])
            if isinstance(node.func, ast.Attribute) and node.func.attr == "copy":
                return root_name(node.func.value)
            return ""

        def state_like(name: str) -> bool:
            lowered = name.lower()
            return any(marker in lowered for marker in state_name_markers)

        def parameter_names(function: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
            args = list(function.args.posonlyargs) + list(function.args.args) + list(function.args.kwonlyargs)
            if function.args.vararg is not None:
                args.append(function.args.vararg)
            if function.args.kwarg is not None:
                args.append(function.args.kwarg)
            return {arg.arg for arg in args}

        def bound_names(function: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
            names: set[str] = set(parameter_names(function))
            for node in ast.walk(function):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) and node is not function:
                    continue
                targets: list[ast.AST] = []
                if isinstance(node, ast.Assign):
                    targets = list(node.targets)
                elif isinstance(node, ast.AnnAssign):
                    targets = [node.target]
                elif isinstance(node, ast.For):
                    targets = [node.target]
                elif isinstance(node, ast.With):
                    targets = [item.optional_vars for item in node.items if item.optional_vars is not None]
                for target in targets:
                    for child in ast.walk(target):
                        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
                            names.add(child.id)
            return names

        for function in [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            has_recursive_call = False
            destructive_events: list[tuple[str, str]] = []
            recursive_same_arg_roots: set[str] = set()
            parameter_roots = parameter_names(function)
            local_names = bound_names(function)
            snapshot_alias_roots: dict[str, str] = {}
            restored_clear_roots: set[str] = set()
            for node in ast.walk(function):
                if isinstance(node, ast.Assign):
                    source_root = snapshot_source_root(node.value)
                    if source_root:
                        for target in node.targets:
                            if isinstance(target, ast.Name):
                                snapshot_alias_roots[target.id] = source_root
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "update"
                ):
                    target_root = root_name(node.func.value)
                    if target_root:
                        for arg in node.args:
                            alias_root = root_name(arg)
                            if snapshot_alias_roots.get(alias_root) == target_root:
                                restored_clear_roots.add(target_root)
            for node in ast.walk(function):
                if isinstance(node, ast.Call):
                    func = node.func
                    if (
                        (isinstance(func, ast.Name) and func.id == function.name)
                        or (isinstance(func, ast.Attribute) and func.attr == function.name)
                    ):
                        has_recursive_call = True
                        recursive_args = list(node.args) + [kw.value for kw in node.keywords if kw.value is not None]
                        for arg in recursive_args:
                            root = root_name(arg)
                            if root:
                                recursive_same_arg_roots.add(root)
                    if isinstance(func, ast.Attribute) and func.attr in high_risk_methods:
                        root = root_name(func.value)
                        if root and state_like(root):
                            destructive_events.append((root, f"{root}.{func.attr}"))
                    if isinstance(func, ast.Attribute) and func.attr == "clear":
                        root = root_name(func.value)
                        if root and state_like(root) and root not in restored_clear_roots:
                            destructive_events.append((root, f"{root}.clear"))
                if isinstance(node, ast.Delete):
                    for target in node.targets:
                        root = root_name(target)
                        if root and state_like(root):
                            destructive_events.append((root, f"del {root}[...]"))
            destructive_targets = {
                label
                for root, label in destructive_events
                if root not in local_names or (root in parameter_roots and root in recursive_same_arg_roots)
            }
            if has_recursive_call and destructive_targets:
                sample = ", ".join(sorted(destructive_targets)[:4])
                return (
                    "recursive/backtracking実装が共有探索状態を destructive に更新しています: "
                    f"{sample}。KeyError/無限再帰/復元漏れを起こしやすいため、"
                    "各再帰branchでは branch-local copy を渡すか、変更対象を完全に記録して対称に復元してください。"
                )
        return ""

    def _python_source_recursive_destructive_state_repair_hints(
        self,
        source: str,
        *,
        max_hints: int = 5,
    ) -> list[dict[str, Any]]:
        try:
            tree = ast.parse(str(source or ""))
        except SyntaxError:
            return []
        text = str(source or "")
        lines = text.splitlines()
        high_risk_methods = {
            "difference_update",
            "intersection_update",
            "popitem",
            "symmetric_difference_update",
        }
        state_name_markers = (
            "active",
            "available",
            "candidate",
            "closed",
            "frontier",
            "open",
            "pending",
            "remaining",
            "state",
            "visited",
        )

        def root_name(node: ast.AST) -> str:
            current = node
            while isinstance(current, (ast.Subscript, ast.Attribute)):
                current = current.value
            return current.id if isinstance(current, ast.Name) else ""

        def state_like(name: str) -> bool:
            lowered = name.lower()
            return any(marker in lowered for marker in state_name_markers)

        def line_text(node: ast.AST) -> str:
            lineno = int(getattr(node, "lineno", 0) or 0)
            if 1 <= lineno <= len(lines):
                return lines[lineno - 1].strip()
            segment = ast.get_source_segment(text, node)
            return str(segment or "").strip()

        hints: list[dict[str, Any]] = []
        for function in [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            recursive_arg_roots: set[str] = set()
            destructive_nodes: list[tuple[ast.AST, str, str]] = []
            for node in ast.walk(function):
                if isinstance(node, ast.Call):
                    func = node.func
                    is_recursive_call = (
                        (isinstance(func, ast.Name) and func.id == function.name)
                        or (isinstance(func, ast.Attribute) and func.attr == function.name)
                    )
                    if is_recursive_call:
                        recursive_args = list(node.args) + [kw.value for kw in node.keywords if kw.value is not None]
                        for arg in recursive_args:
                            root = root_name(arg)
                            if root and state_like(root):
                                recursive_arg_roots.add(root)
                    if isinstance(func, ast.Attribute) and func.attr in high_risk_methods:
                        root = root_name(func.value)
                        if root and state_like(root):
                            destructive_nodes.append((node, root, f"{root}.{func.attr}"))
                    if isinstance(func, ast.Attribute) and func.attr == "clear":
                        root = root_name(func.value)
                        if root and state_like(root):
                            destructive_nodes.append((node, root, f"{root}.clear"))
                elif isinstance(node, ast.Delete):
                    for target in node.targets:
                        root = root_name(target)
                        if root and state_like(root):
                            destructive_nodes.append((node, root, f"del {root}[...]"))
            for node, root, label in destructive_nodes:
                if recursive_arg_roots and root not in recursive_arg_roots:
                    continue
                hints.append(
                    {
                        "line": int(getattr(node, "lineno", 0) or 0),
                        "current_text": line_text(node),
                        "reason": (
                            f"recursive branch reuses mutable state `{root}` and then performs `{label}`; "
                            "this keeps the destructive update on shared search state"
                        ),
                        "suggested_action": "rewrite_recursive_branch_local_state",
                        "suggested_new_text": (
                            f"Build a branch-local `next_{root}` copy before this point, mutate only `next_{root}`, "
                            f"pass `next_{root}` into the recursive call, and remove destructive updates to `{root}`."
                        ),
                    }
                )
                if len(hints) >= max_hints:
                    return hints
        return hints

    def _python_source_has_unthreaded_branch_local_recursive_state(self, source: str) -> str:
        try:
            tree = ast.parse(str(source or ""))
        except SyntaxError:
            return ""
        state_name_markers = (
            "available",
            "candidate",
            "frontier",
            "open",
            "pending",
            "remaining",
            "state",
            "visited",
        )
        branch_state_prefixes = ("new_", "next_", "branch_")

        def root_name(node: ast.AST) -> str:
            current = node
            while isinstance(current, (ast.Subscript, ast.Attribute)):
                current = current.value
            return current.id if isinstance(current, ast.Name) else ""

        def state_like(name: str) -> bool:
            lowered = name.lower()
            return any(marker in lowered for marker in state_name_markers)

        def assigned_names(node: ast.AST) -> set[str]:
            names: set[str] = set()
            for child in ast.walk(node):
                if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
                    names.add(child.id)
            return names

        def loaded_names(node: ast.AST) -> set[str]:
            names: set[str] = set()
            for child in ast.walk(node):
                if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                    names.add(child.id)
            return names

        for function in [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            branch_state_names: set[str] = set()
            recursive_arg_names: set[str] = set()
            state_dependencies: dict[str, set[str]] = {}
            has_recursive_call = False
            for node in ast.walk(function):
                if isinstance(node, ast.Call):
                    func = node.func
                    if (
                        (isinstance(func, ast.Name) and func.id == function.name)
                        or (isinstance(func, ast.Attribute) and func.attr == function.name)
                    ):
                        has_recursive_call = True
                        recursive_args = list(node.args) + [kw.value for kw in node.keywords if kw.value is not None]
                        for arg in recursive_args:
                            root = root_name(arg)
                            if root:
                                recursive_arg_names.add(root)
                            recursive_arg_names.update(loaded_names(arg))
                    if isinstance(func, ast.Attribute):
                        container = root_name(func.value)
                        if container and func.attr in {"add", "append", "extend", "insert", "update", "setdefault"}:
                            dependency_names: set[str] = set()
                            for arg in node.args:
                                dependency_names.update(loaded_names(arg))
                            for keyword in node.keywords:
                                if keyword.value is not None:
                                    dependency_names.update(loaded_names(keyword.value))
                            if dependency_names:
                                state_dependencies.setdefault(container, set()).update(dependency_names)
                targets: list[ast.AST] = []
                value: ast.AST | None = None
                if isinstance(node, ast.Assign):
                    targets = list(node.targets)
                    value = node.value
                elif isinstance(node, ast.AnnAssign):
                    targets = [node.target]
                    value = node.value
                elif isinstance(node, ast.AugAssign):
                    targets = [node.target]
                    value = node.value
                if value is not None:
                    dependencies = loaded_names(value)
                    for target in targets:
                        target_root = root_name(target)
                        if target_root and dependencies:
                            state_dependencies.setdefault(target_root, set()).update(dependencies)
                for target in targets:
                    for name in assigned_names(target):
                        lowered = name.lower()
                        if lowered.startswith(branch_state_prefixes) and state_like(name):
                            branch_state_names.add(name)
            threaded_names = set(recursive_arg_names)
            changed = True
            while changed:
                changed = False
                for container, dependencies in state_dependencies.items():
                    if container not in threaded_names:
                        continue
                    for dependency in dependencies:
                        if dependency not in threaded_names:
                            threaded_names.add(dependency)
                            changed = True
            unthreaded = sorted(name for name in branch_state_names if name not in threaded_names)
            if has_recursive_call and unthreaded:
                sample = ", ".join(unthreaded[:4])
                return (
                    "recursive/backtracking実装が branch-local 探索状態を作っていますが、"
                    f"再帰呼び出しへ渡していません: {sample}。"
                    "copyを作るだけでは次branchの状態にならないため、作成した next_/branch_ state を"
                    "recursive call の引数へthreadしてください。"
                )
        return ""

    def _test_source_large_fixture_issue(self, *, user_message: str, source: str) -> dict[str, Any] | None:
        user_text = str(user_message or "").lower()
        asks_small_example = any(
            marker in user_text
            for marker in ["小さな", "小さい", "small", "known example", "既知例", "簡単な例"]
        )
        if not asks_small_example:
            return None
        source_text = str(source or "")
        max_chars = int(self.runtime_config.get("focused_test_max_source_chars") or 5000)
        if len(source_text) > max_chars:
            return {
                "reason_code": "test_artifact_fixture_too_large",
                "message": (
                    "ユーザー要求は小さな既知例によるunittestです。test file が大きすぎ、"
                    "fixtureの意味を人間とruntimeが確認しにくくなっています。"
                ),
                "allowed_next_actions": ["write_file tests/test_*.py", "replace_text tests/test_*.py"],
                "suggested_fix": (
                    "巨大fixtureを捨て、2-4個の小さい既知fixtureで、ユーザー要求から抽出した公開API、"
                    "成功例、複数結果が必要な場合の列挙例、失敗/不成立例を直接検証してください。"
                ),
            }
        try:
            tree = ast.parse(source_text)
        except SyntaxError:
            return None
        largest_literal = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                largest_literal = max(largest_literal, len(node.keys))
            elif isinstance(node, (ast.List, ast.Tuple, ast.Set)):
                largest_literal = max(largest_literal, len(node.elts))
        max_literal_items = int(self.runtime_config.get("focused_test_max_literal_items") or 24)
        if largest_literal > max_literal_items:
            return {
                "reason_code": "test_artifact_fixture_too_large",
                "message": (
                    "ユーザー要求は小さな既知例によるunittestですが、test file に巨大なliteral fixtureが含まれています。"
                    f"最大literal要素数は {largest_literal} で、review可能な小例の範囲を超えています。"
                ),
                "allowed_next_actions": ["write_file tests/test_*.py", "replace_text tests/test_*.py"],
                "suggested_fix": (
                    "大量行を列挙するfixtureではなく、各期待値を手で説明できる小さいfixtureへ置き換えてください。"
                    "必要なら成功例、全解列挙例、解なし例を別test methodに分けてください。"
                ),
            }
        return None






































    def _requested_top_level_function_names(self, user_message: str) -> list[str]:
        """Extract explicit public module-function API names from the user request."""

        text = str(user_message or "")
        lowered = text.lower()
        api_markers = ["api", "公開api", "公開 api", "公開関数", "public api", "public functions", "関数"]
        has_api_marker = any(marker in lowered for marker in api_markers)
        implementation_markers = [
            "実装",
            "作成",
            "追加",
            "検証",
            "テスト",
            "unittest",
            "python",
            "top-level",
            "top level",
        ]
        has_implementation_context = has_api_marker or any(marker in lowered for marker in implementation_markers)
        if not has_implementation_context:
            return []
        ignored = {
            "dict",
            "set",
            "list",
            "tuple",
            "str",
            "int",
            "float",
            "bool",
            "none",
            "print",
            "range",
            "enumerate",
            "sorted",
            "len",
            "python",
            "unittest",
            "nodes_visited",
            "backtracks",
            "implementation",
            "feasibility",
            "verifiability",
            "rationale",
            "reason",
            "before",
            "after",
            "delta",
            "append_file",
            "create_plan",
            "decompose_tasks",
            "finish",
            "list_files",
            "open_child_frame",
            "read_file",
            "replace_text",
            "return_to_parent",
            "run_command",
            "search_code",
            "write_file",
        }
        names: list[str] = []

        def add_name(candidate: str) -> None:
            name = str(candidate or "").strip()
            if not name or name.lower() in ignored or name.startswith("_"):
                return
            if name not in names:
                names.append(name)

        # Only treat call-shaped names as public API when the sentence itself
        # says it is describing an API. Formulae and examples often contain
        # helper calls that should remain implementation details.
        api_sentence_markers = (
            "api",
            "public function",
            "public functions",
            "公開api",
            "公開 api",
            "公開関数",
            "公開関数は",
        )
        for sentence in re.split(r"(?<=[。.!?])\s+|[。\n]", text):
            sentence_lower = sentence.lower()
            if not any(marker in sentence_lower for marker in api_sentence_markers):
                continue
            for match in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", sentence):
                add_name(match.group(1))

        explicit_call_api_pattern = (
            r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\([^)]*\)"
            r"\s*(?=(?:を|は|が|で)\s*(?:実装|作成|追加|検証|公開API|公開関数|public))"
        )
        for match in re.finditer(explicit_call_api_pattern, text):
            add_name(match.group(1))

        # Japanese task prompts often state the public API as prose rather than
        # as an explicit call expression. Treat snake_case names in that role as
        # requested module-level callables without knowing their domain.
        prose_api_pattern = (
            r"(?<![A-Za-z0-9_])"
            r"([a-z][a-z0-9]*_[a-z0-9_]*)"
            r"(?=\s*(?:"
            r"は[^。\n]{0,40}(?:実装|作成|追加|返|列挙|検証|存在|必要)|"
            r"が(?:存在|必要|返)|"
            r"を(?:実装|作成|追加|返|列挙|検証)|"
            r"で(?:検証|実装)"
            r"))"
        )
        for match in re.finditer(prose_api_pattern, text):
            add_name(match.group(1))

        for match in re.finditer(r"(?<![A-Za-z0-9_])([a-z][a-z0-9]*_[a-z0-9_]*)\s+as\s+function", lowered):
            add_name(match.group(1))
        return names[:12]

    def _public_api_wrapper_repair_hints(
        self,
        *,
        user_message: str,
        source: str,
        max_hints: int = 3,
    ) -> list[dict[str, Any]]:
        requested = self._requested_top_level_function_names(user_message)
        if not requested:
            return []
        try:
            tree = ast.parse(str(source or ""))
        except SyntaxError:
            return []
        top_level_functions = {
            node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        missing = [name for name in requested if name not in top_level_functions]
        if not missing:
            return []

        def simple_arg_names(args: ast.arguments, *, skip_self: bool) -> list[str]:
            names: list[str] = []
            positional = list(args.posonlyargs) + list(args.args)
            if skip_self and positional and positional[0].arg == "self":
                positional = positional[1:]
            for arg in positional:
                if arg.arg not in names:
                    names.append(arg.arg)
            for arg in args.kwonlyargs:
                if arg.arg not in names:
                    names.append(arg.arg)
            return names

        hints: list[dict[str, Any]] = []
        for class_node in [node for node in tree.body if isinstance(node, ast.ClassDef)]:
            methods = {
                node.name: node
                for node in class_node.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            missing_methods = [name for name in missing if name in methods]
            if not missing_methods:
                continue
            init_node = methods.get("__init__")
            init_args = (
                simple_arg_names(init_node.args, skip_self=True)
                if isinstance(init_node, (ast.FunctionDef, ast.AsyncFunctionDef))
                else []
            )
            constructor_args = ", ".join(init_args)
            wrappers: list[str] = []
            method_lines: list[str] = []
            for name in missing_methods:
                method_node = methods[name]
                method_args = simple_arg_names(method_node.args, skip_self=True)
                wrapper_args = [*init_args, *[arg for arg in method_args if arg not in init_args]]
                signature = ", ".join(wrapper_args)
                method_call = ", ".join(method_args)
                constructor = f"{class_node.name}({constructor_args})" if constructor_args else f"{class_node.name}()"
                call = f"{constructor}.{name}({method_call})" if method_call else f"{constructor}.{name}()"
                wrappers.append(f"def {name}({signature}):\n    return {call}")
                method_lines.append(f"{class_node.name}.{name}")
            hints.append(
                {
                    "line": int(class_node.lineno),
                    "current_text": "class methods only: " + ", ".join(method_lines),
                    "reason": "requested public API exists as class methods but not as module-level functions",
                    "suggested_action": "append_top_level_wrappers",
                    "suggested_new_text": "\n\n" + "\n\n".join(wrappers) + "\n",
                }
            )
            if len(hints) >= max_hints:
                break
        return hints

    def _requested_implementation_paths(self, user_message: str) -> list[str]:
        """Extract explicit non-test Python implementation paths from the request."""

        text = str(user_message or "")
        paths: list[str] = []
        for match in re.finditer(r"(?<![\w.-])([A-Za-z0-9_./-]+\.py)(?![\w.-])", text):
            path = match.group(1).strip("./")
            if not path or _artifact_path_is_test(path):
                continue
            if path not in paths:
                paths.append(path)
        return paths[:8]




    def _requested_top_level_api_test_issue(
        self,
        *,
        user_message: str,
        test_sources: list[tuple[str, str]],
    ) -> str:
        requested = self._requested_top_level_function_names(user_message)
        if not requested or not test_sources:
            return ""
        covered: set[str] = set()
        for _path, source in test_sources:
            try:
                tree = ast.parse(str(source or ""))
            except SyntaxError:
                continue
            module_aliases: set[str] = set()
            function_aliases: dict[str, str] = {}
            star_import = False
            for node in tree.body:
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        module_aliases.add(alias.asname or alias.name.split(".", 1)[0])
                elif isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        if alias.name == "*":
                            star_import = True
                        elif alias.name in requested:
                            function_aliases[alias.asname or alias.name] = alias.name
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if isinstance(func, ast.Name):
                    if func.id in function_aliases:
                        covered.add(function_aliases[func.id])
                    elif star_import and func.id in requested:
                        covered.add(func.id)
                elif (
                    isinstance(func, ast.Attribute)
                    and func.attr in requested
                    and isinstance(func.value, ast.Name)
                    and func.value.id in module_aliases
                ):
                    covered.add(func.attr)
        missing = [name for name in requested if name not in covered]
        if not missing:
            return ""
        return (
            "unittest がユーザー要求のtop-level public APIを直接検証していません: "
            + ", ".join(missing)
            + "。class method 経由だけでなく、実装moduleから公開関数をimportして呼び出すテストにしてください。"
        )

    def _implementation_source_contract_issues(self, *, user_message: str, source: str) -> list[str]:
        """Observable implementation-source issues for generic implementation tasks."""
        text = str(source or "")
        issues: list[str] = []
        plan_context = self._plan_record_execution_context()
        plan_strategy = str(plan_context.get("plan_strategy") or "")
        if plan_strategy == "state_space_search":
            issues.extend(self._state_space_search_source_contract_issues(text))
        elif plan_strategy == "dynamic_programming":
            issues.extend(self._dynamic_programming_source_contract_issues(text, user_message=user_message))
        elif plan_strategy == "constraint_satisfaction":
            issues.extend(self._constraint_satisfaction_source_contract_issues(text))
        requested_function_names = self._requested_top_level_function_names(user_message)
        if requested_function_names:
            try:
                tree = ast.parse(text)
            except SyntaxError:
                tree = None
            if tree is not None:
                top_level_functions = {
                    node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                }
                missing_functions = [name for name in requested_function_names if name not in top_level_functions]
                if missing_functions:
                    issues.append(
                        "ユーザー要求に明示された公開APIのtop-level functionがありません: "
                        + ", ".join(missing_functions)
                        + "。class内部のmethodだけではなく、同名のmodule-level defを定義してください。"
                    )
        input_contract_issue = self._python_source_narrows_requested_input_contract(
            user_message=user_message,
            source=text,
        )
        if input_contract_issue:
            issues.append(input_contract_issue)
        input_mutation_issue = self._python_source_mutates_requested_input_collections(
            user_message=user_message,
            source=text,
        )
        if input_mutation_issue:
            issues.append(input_mutation_issue)
        recursive_state_issue = self._python_source_has_recursive_destructive_shared_state(text)
        if recursive_state_issue:
            issues.append(recursive_state_issue)
        unthreaded_branch_state_issue = self._python_source_has_unthreaded_branch_local_recursive_state(text)
        if unthreaded_branch_state_issue:
            issues.append(unthreaded_branch_state_issue)
        if self._python_source_is_escaped_source_literal(text):
            issues.append(
                "Pythonファイル全体が、実行されるコードではなくエスケープされたソース文字列リテラルになっています。"
                "実装成果物としては、def/class/import がトップレベル構文として存在する必要があります。"
            )
        if self._python_source_has_pass_only_callable(text):
            markers = self._python_source_placeholder_markers(text)
            label = ", ".join(markers[:4])
            if len(markers) > 4:
                label += f", ... ({len(markers)} total)"
            issues.append(f"未完成のplaceholder callableが残っています: {label}。")
        incomplete_markers = self._python_source_incomplete_implementation_markers(text)
        if incomplete_markers:
            label = ", ".join(incomplete_markers[:4])
            if len(incomplete_markers) > 4:
                label += f", ... ({len(incomplete_markers)} total)"
            issues.append(f"未完成のpass文または次工程前提の実装が残っています: {label}。")
        return issues

    def _dynamic_programming_source_contract_issues(self, source: str, *, user_message: str = "") -> list[str]:
        """Return generic source issues for dynamic-programming PlanRecords."""

        text = str(source or "")
        if not text.strip():
            return []
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return []
        callable_names = [
            node.name.lower()
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ]
        requested_function_names = {
            name.lower()
            for name in self._requested_top_level_function_names(user_message)
        }
        solverish_tokens = (
            "solve",
            "compute",
            "count",
            "optimize",
            "optimal",
            "best",
            "dp",
            "memo",
            "tabulate",
        )
        has_requested_callable = bool(requested_function_names & set(callable_names))
        has_solverish_callable = has_requested_callable or any(
            any(token in name for token in solverish_tokens)
            for name in callable_names
        )
        lowered = text.lower()
        has_memo_or_table = any(
            token in lowered
            for token in ("memo", "cache", "lru_cache", "dp[", "table", "tabulat", "dynamic programming")
        )
        has_indexed_state_assignment = False
        has_recursive_call = False
        current_function = ""
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = []
                if isinstance(node, ast.Assign):
                    targets = list(node.targets)
                else:
                    targets = [node.target]
                if any(isinstance(target, ast.Subscript) for target in targets):
                    has_indexed_state_assignment = True
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                current_function = node.name
                for child in ast.walk(node):
                    if isinstance(child, ast.Call) and isinstance(child.func, ast.Name) and child.func.id == current_function:
                        has_recursive_call = True
                        break
        has_base_case = bool(
            re.search(r"\b(base|base_case|initial|seed)\b", lowered)
            or "基底" in text
            or "初期" in text
        )
        if not has_base_case:
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    if any(isinstance(target, ast.Subscript) for target in node.targets):
                        slice_text = ast.unparse(node.targets[0].slice) if hasattr(ast, "unparse") else ""
                        if re.search(r"\b0\b|\b1\b", slice_text):
                            has_base_case = True
                            break
                if isinstance(node, ast.If):
                    if any(isinstance(child, ast.Return) for child in ast.walk(node)):
                        condition_text = ast.unparse(node.test) if hasattr(ast, "unparse") else ""
                        if re.search(r"\b0\b|\b1\b|\bnot\b", condition_text):
                            has_base_case = True
                            break
        has_recurrence_or_transition = bool(
            re.search(r"\b(recurrence|transition|subproblem|state transition)\b", lowered)
            or "漸化" in text
            or "遷移" in text
            or has_recursive_call
            or has_memo_or_table
            or has_indexed_state_assignment
        )
        issues: list[str] = []
        if not has_solverish_callable:
            issues.append(
                "PlanRecord strategy=dynamic_programming ですが、subproblemを計算するprogrammatic callableが見つかりません。"
                "ユーザー指定のtop-level API、またはDP表やmemoizationを実行して結果を返すsolve/compute/count/optimize系callableを実装してください。"
            )
        if not has_base_case:
            issues.append(
                "dynamic_programming PlanRecordのbase_case_verifier義務に対し、base caseまたは初期状態を表す実装経路が観測できません。"
                "最小subproblemの戻り値、初期DP表、またはbase caseを明示してください。"
            )
        if not has_recurrence_or_transition:
            issues.append(
                "dynamic_programming PlanRecordのrecurrence_verifier義務に対し、recurrence/transition/memoization/table更新が観測できません。"
                "部分問題から次状態を導く漸化式またはDP表更新を実装してください。"
            )
        return issues

    def _constraint_satisfaction_source_contract_issues(self, source: str) -> list[str]:
        """Return generic source issues for constraint-satisfaction PlanRecords."""

        text = str(source or "")
        if not text.strip():
            return []
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return []
        callable_names = [
            node.name.lower()
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ]
        solverish_tokens = ("solve", "search", "assign", "backtrack", "satisfy", "csp")
        checker_tokens = ("constraint", "consistent", "valid", "validate", "check", "satisf")
        solution_tokens = ("solution", "assignment", "validate", "verify", "check")
        has_solverish_callable = any(any(token in name for token in solverish_tokens) for name in callable_names)
        has_constraint_checker = any(any(token in name for token in checker_tokens) for name in callable_names)
        has_solution_validator = any(any(token in name for token in solution_tokens) for name in callable_names)
        lowered = text.lower()
        has_constraint_model = any(
            token in lowered
            for token in ("variables", "domains", "constraints", "assignment", "constraint")
        ) or any(token in text for token in ("変数", "ドメイン", "制約", "割当"))
        issues: list[str] = []
        if not has_solverish_callable:
            issues.append(
                "PlanRecord strategy=constraint_satisfaction ですが、assignmentを探索するprogrammatic solver/search/backtrack callableが見つかりません。"
                "変数・ドメイン・制約から候補割当を生成するsolve/search/assign系callableを実装してください。"
            )
        if not has_constraint_model:
            issues.append(
                "constraint_satisfaction PlanRecordのvariables/domains/constraints義務に対し、制約モデルがsource上で観測できません。"
                "変数、ドメイン、制約を入力または内部モデルとして明示してください。"
            )
        if not has_constraint_checker:
            issues.append(
                "constraint_satisfaction PlanRecordのconstraint_checker義務に対し、各制約を検査するcheck/valid/constraint系callableが不足しています。"
                "候補割当が制約を満たすかを独立に判定する経路を実装してください。"
            )
        if not has_solution_validator:
            issues.append(
                "constraint_satisfaction PlanRecordのsolution_validator義務に対し、最終assignment全体を検証するvalidate/verify/check_solution系callableが不足しています。"
                "solverの戻り値を受け取り、完全性と全制約充足を確認する検証経路を実装してください。"
            )
        return issues

    def _state_space_search_source_contract_issues(self, source: str) -> list[str]:
        """Return generic source issues for state-space-search PlanRecords."""

        text = str(source or "")
        if not text.strip():
            return []
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return []
        callable_names: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                callable_names.append(node.name.lower())
        solverish_tokens = ("solve", "solver", "search", "astar", "a_star", "bfs", "dfs", "plan", "path")
        verifierish_tokens = ("verify", "validator", "legal", "goal", "replay")
        replay_verifier_tokens = ("verify", "validate", "replay", "check_solution", "final_verifier")
        state_predicate_names = {"is_solved", "solved", "is_goal", "goal_reached", "is_goal_state"}
        has_solverish_callable = any(
            name not in state_predicate_names and any(token in name for token in solverish_tokens)
            for name in callable_names
        )
        has_verifierish_callable = any(
            any(token in name for token in verifierish_tokens)
            for name in callable_names
        )
        has_replay_verifier_callable = any(
            any(token in name for token in replay_verifier_tokens)
            for name in callable_names
        )
        has_manual_input_loop = bool(re.search(r"\binput\s*\(", text))
        issues: list[str] = []
        nondeterministic_fixture_helpers: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            function_name = node.name.lower()
            if not any(token in function_name for token in ("fixture", "near_goal", "near", "scramble")):
                continue
            for child in ast.walk(node):
                if isinstance(child, ast.Import):
                    if any(alias.name == "random" or alias.name.startswith("random.") for alias in child.names):
                        nondeterministic_fixture_helpers.append(node.name)
                        break
                if isinstance(child, ast.ImportFrom) and child.module == "random":
                    nondeterministic_fixture_helpers.append(node.name)
                    break
                if isinstance(child, ast.Call):
                    func = child.func
                    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "random":
                        nondeterministic_fixture_helpers.append(node.name)
                        break
                    if isinstance(func, ast.Name) and func.id in {"choice", "sample", "shuffle", "randint", "randrange"}:
                        nondeterministic_fixture_helpers.append(node.name)
                        break
        if not has_solverish_callable:
            issues.append(
                "PlanRecord strategy=state_space_search ですが、action列を返すprogrammatic solver/search callableが見つかりません。"
                "手動UIやmove helperだけではなく、状態からゴールまでの探索結果を返すsolve/search/path系callableを実装してください。"
            )
        if has_manual_input_loop and not has_solverish_callable:
            issues.append(
                "state_space_search PlanRecordでは manual input loop alone は実装義務を満たしません。"
                "CLI入力は補助に留め、runtime/testから呼べるsolver/search callableを追加してください。"
            )
        if not has_verifierish_callable:
            issues.append(
                "state_space_search PlanRecordのfinal_verifier義務に対し、legal move validator / goal check / replay verifier を表すcallableが不足しています。"
                "返されたaction列を合法手としてreplayできる検証経路を実装してください。"
            )
        elif not has_replay_verifier_callable:
            issues.append(
                "state_space_search PlanRecordのfinal_verifier義務に対し、solver/searchが返したaction列を独立にreplay/verifyするcallableが不足しています。"
                "is_legal_move や is_goal_state だけではfinal_verifierになりません。solve/searchの戻り値を受け取り、合法手を順に適用してgoal到達を返す verify/replay/validate 系callableを追加してください。"
            )
        if nondeterministic_fixture_helpers:
            helpers = ", ".join(sorted(set(nondeterministic_fixture_helpers))[:4])
            issues.append(
                "state_space_search fixture helperが random/shuffle に依存しています: "
                + helpers
                + "。near-goalやfixture生成は決定的でなければなりません。"
                "既知の基準stateから固定順のlegal transitionを短く適用し、直前の逆操作だけを避けるなど、"
                "同じ入力から常に同じstart stateを作る実装にしてください。"
            )
        return issues

    def _state_space_search_replay_verifier_repair_hints(
        self,
        *,
        source: str,
        issues: list[str],
    ) -> list[dict[str, Any]]:
        issue_text = "\n".join(str(issue) for issue in issues)
        if "final_verifier" not in issue_text and "replay/verify" not in issue_text:
            return []
        return [
            {
                "suggested_action": "add_state_space_replay_verifier",
                "current_text": "solver/search action output has no independent replay verifier",
                "reason": (
                    "state_space_search の finish 証拠は、solver/search が返した action 列を "
                    "initial_state から合法性確認しながら適用し、goal 到達を boolean で返す callable です。"
                ),
                "suggested_new_text": self._state_space_search_replay_verifier_template(source),
            }
        ]

    @staticmethod
    def _state_space_search_preferred_symbol(names: set[str], preferred: tuple[str, ...], tokens: tuple[str, ...]) -> str:
        for name in preferred:
            if name in names:
                return name
        for name in sorted(names):
            lowered = name.lower()
            if tokens and any(token in lowered for token in tokens):
                return name
        return ""

    @staticmethod
    def _state_space_search_legal_check_lines(symbol: str, *, receiver: str = "", indent: str = "        ") -> str:
        lowered = str(symbol or "").lower()
        call = f"{receiver}{symbol}"
        if lowered.startswith("get_") or "moves" in lowered or "actions" in lowered:
            return (
                f"{indent}if action not in list({call}(current_state)):\n"
                f"{indent}    return False\n"
            )
        return (
            f"{indent}if not {call}(current_state, action):\n"
            f"{indent}    return False\n"
        )

    def _state_space_search_replay_verifier_template(self, source: str) -> str:
        """Return a generic replay-verifier repair template adapted to visible helper names."""

        try:
            tree = ast.parse(str(source or ""))
        except SyntaxError:
            tree = None
        if tree is not None:
            for class_node in [node for node in tree.body if isinstance(node, ast.ClassDef)]:
                method_names = {
                    node.name
                    for node in class_node.body
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                }
                legal_method = self._state_space_search_preferred_symbol(
                    method_names,
                    ("is_valid_move", "is_legal_move", "validate_move", "get_legal_moves", "legal_moves", "legal_actions"),
                    ("legal", "valid", "validate", "moves", "actions"),
                )
                transition_method = self._state_space_search_preferred_symbol(
                    method_names,
                    ("make_move", "apply_move", "apply_action", "transition", "next_state"),
                    ("apply", "transition", "next_state", "make_move"),
                )
                goal_attrs: set[str] = set()
                for node in ast.walk(class_node):
                    if (
                        isinstance(node, ast.Attribute)
                        and isinstance(node.value, ast.Name)
                        and node.value.id == "self"
                        and isinstance(getattr(node, "ctx", None), ast.Store)
                    ):
                        goal_attrs.add(node.attr)
                goal_attr = (
                    "goal_state"
                    if "goal_state" in goal_attrs or "goal_state" in str(source or "")
                    else sorted(goal_attrs)[0]
                    if goal_attrs
                    else "goal_state"
                )
                if legal_method and transition_method:
                    legal_check = self._state_space_search_legal_check_lines(
                        legal_method,
                        receiver="self.",
                        indent="            ",
                    )
                    return (
                        f"    def verify_solution(self, initial_state, actions):\n"
                        f"        if actions is None:\n"
                        f"            return False\n"
                        f"        current_state = initial_state\n"
                        f"        for action in actions:\n"
                        f"{legal_check}"
                        f"            current_state = self.{transition_method}(current_state, action)\n"
                        f"        return current_state == self.{goal_attr}\n"
                    )
            function_names = {
                node.name
                for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            legal_function = self._state_space_search_preferred_symbol(
                function_names,
                ("is_valid_move", "is_legal_move", "validate_move", "get_legal_moves", "legal_moves", "legal_actions"),
                ("legal", "valid", "validate", "moves", "actions"),
            )
            transition_function = self._state_space_search_preferred_symbol(
                function_names,
                ("make_move", "apply_move", "apply_action", "transition", "next_state"),
                ("apply", "transition", "next_state", "make_move"),
            )
            goal_function = self._state_space_search_preferred_symbol(
                function_names,
                ("is_goal_state", "is_goal", "goal_reached", "is_solved"),
                ("goal", "solved"),
            )
            if legal_function and transition_function:
                goal_line = (
                    f"    return {goal_function}(current_state, goal_state)\n"
                    if goal_function
                    else "    return current_state == goal_state\n"
                )
                legal_check = self._state_space_search_legal_check_lines(legal_function, indent="        ")
                return (
                    "def verify_solution(initial_state, goal_state, actions):\n"
                    "    if actions is None:\n"
                    "        return False\n"
                    "    current_state = initial_state\n"
                    "    for action in actions:\n"
                    f"{legal_check}"
                    f"        current_state = {transition_function}(current_state, action)\n"
                    f"{goal_line}"
                )
        return (
            "def verify_solution(initial_state, goal_state, actions, legal_move_validator, transition):\n"
            "    if actions is None:\n"
            "        return False\n"
            "    current_state = initial_state\n"
            "    for action in actions:\n"
            "        if not legal_move_validator(current_state, action):\n"
            "            return False\n"
            "        current_state = transition(current_state, action)\n"
            "    return current_state == goal_state\n"
        )

    def _state_space_search_test_contract_issues(self, test_sources: list[tuple[str, str]]) -> list[str]:
        """Return generic test-artifact issues for state-space-search plans."""

        if str(self._plan_record_execution_context().get("plan_strategy") or "") != "state_space_search":
            return []
        solverish_tokens = ("solve", "solver", "search", "astar", "a_star", "bfs", "dfs", "plan", "path")
        verifierish_tokens = (
            "verify",
            "verifier",
            "replay",
            "legal",
            "valid",
            "apply",
            "make_move",
            "goal",
            "solved",
            "is_solved",
        )

        def call_name(call: ast.Call) -> str:
            func = call.func
            if isinstance(func, ast.Name):
                return func.id.lower()
            if isinstance(func, ast.Attribute):
                return func.attr.lower()
            return ""

        def is_solver_execution_call(call: ast.Call) -> bool:
            name = call_name(call)
            if not name:
                return False
            if name in {"solver", "set_start_state", "set_goal_state", "is_solvable", "solvable"}:
                return False
            execution_tokens = (
                "solve",
                "search",
                "astar",
                "a_star",
                "bfs",
                "dfs",
                "find_path",
                "shortest_path",
                "plan_path",
            )
            return any(token in name for token in execution_tokens)

        def solver_call_has_explicit_bound(call: ast.Call, test_source_lower: str) -> bool:
            bound_tokens = (
                "max_depth",
                "max_steps",
                "max_nodes",
                "max_iterations",
                "depth_limit",
                "node_limit",
                "step_limit",
                "timeout",
                "time_limit",
                "budget",
                "cutoff",
                "bound",
                "limit",
            )
            for keyword in call.keywords:
                keyword_name = str(keyword.arg or "").lower()
                if keyword_name and any(token in keyword_name for token in bound_tokens):
                    return True
            return any(token in test_source_lower for token in bound_tokens)

        def assertion_expects_none_from_solver(call: ast.Call, solver_result_names: set[str]) -> bool:
            if call_name(call) not in {"assertisnone", "assert_is_none"}:
                return False
            if not call.args:
                return False
            target = call.args[0]
            if isinstance(target, ast.Name) and target.id in solver_result_names:
                return True
            return isinstance(target, ast.Call) and is_solver_execution_call(target)

        def target_names(target: ast.AST) -> list[str]:
            if isinstance(target, ast.Name):
                return [target.id]
            if isinstance(target, (ast.Tuple, ast.List)):
                names: list[str] = []
                for item in target.elts:
                    names.extend(target_names(item))
                return names
            return []

        def literal_container_len(node: ast.AST) -> int:
            if isinstance(node, (ast.List, ast.Tuple)):
                return len(node.elts)
            return 0

        def is_small_literal_action_item(node: ast.AST) -> bool:
            if isinstance(node, ast.Constant):
                return isinstance(node.value, (str, int, float, bool, type(None)))
            if isinstance(node, (ast.List, ast.Tuple)):
                return all(is_small_literal_action_item(item) for item in node.elts)
            if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
                return is_small_literal_action_item(node.operand)
            return False

        def is_handwritten_action_sequence_literal(node: ast.AST) -> bool:
            if not isinstance(node, (ast.List, ast.Tuple)) or len(node.elts) < 4:
                return False
            return all(is_small_literal_action_item(item) for item in node.elts)

        action_fixture_name_tokens = ("action", "actions", "move", "moves", "solution", "path", "route", "plan")
        start_fixture_name_tokens = ("start", "initial")
        goal_fixture_name_tokens = ("goal", "target")

        issues: list[str] = []
        solver_test_seen = False
        replaying_solver_test_seen = False
        solver_tests_with_pass: list[str] = []
        randomized_or_broad_solver_tests: list[str] = []
        unbounded_no_solution_solver_tests: list[str] = []
        handwritten_action_fixture_tests: list[str] = []
        literal_near_goal_fixture_tests: list[str] = []
        for path, source in test_sources:
            try:
                tree = ast.parse(str(source or ""))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if not node.name.startswith("test"):
                    continue
                calls = [call_name(call) for call in ast.walk(node) if isinstance(call, ast.Call)]
                has_solver_call = any(
                    any(token in name for token in solverish_tokens)
                    for name in calls
                    if name
                )
                if not has_solver_call:
                    continue
                solver_test_seen = True
                test_source = ast.get_source_segment(str(source or ""), node) or ""
                test_source_lower = test_source.lower()
                solver_result_names: set[str] = set()
                solver_execution_calls: list[ast.Call] = []
                for child in ast.walk(node):
                    if isinstance(child, ast.Assign) and isinstance(child.value, ast.Call) and is_solver_execution_call(child.value):
                        solver_execution_calls.append(child.value)
                        for target in child.targets:
                            if isinstance(target, ast.Name):
                                solver_result_names.add(target.id)
                    elif isinstance(child, ast.Call) and is_solver_execution_call(child):
                        solver_execution_calls.append(child)
                if (
                    "shuffle(" in test_source_lower
                    or "make_random_move(" in test_source_lower
                    or "random." in test_source_lower
                    or any(name in {"choice", "sample", "randint", "randrange"} for name in calls)
                ):
                    randomized_or_broad_solver_tests.append(f"{path}:{node.name}")
                if any(isinstance(stmt, ast.Pass) for stmt in ast.walk(node)):
                    solver_tests_with_pass.append(f"{path}:{node.name}")
                has_replay_or_verifier = any(
                    any(token in name for token in verifierish_tokens)
                    for name in calls
                    if name
                )
                if has_replay_or_verifier:
                    replaying_solver_test_seen = True
                no_solution_named_test = any(
                    token in node.name.lower()
                    for token in ("unsolvable", "no_solution", "no_path", "unreachable", "impossible")
                )
                no_solution_assert = any(
                    isinstance(child, ast.Call) and assertion_expects_none_from_solver(child, solver_result_names)
                    for child in ast.walk(node)
                )
                if (no_solution_named_test or no_solution_assert) and solver_execution_calls:
                    if not any(solver_call_has_explicit_bound(call, test_source_lower) for call in solver_execution_calls):
                        unbounded_no_solution_solver_tests.append(f"{path}:{node.name}")
                near_goal_claimed = any(
                    token in node.name.lower() or token in test_source_lower
                    for token in ("near_goal", "near-goal", "near goal")
                )
                literal_start_lines: list[int | str] = []
                literal_goal_lines: list[int | str] = []
                for child in ast.walk(node):
                    if isinstance(child, ast.Assign):
                        names = [
                            name.lower()
                            for target in child.targets
                            for name in target_names(target)
                        ]
                        if (
                            names
                            and any(any(token in name for token in action_fixture_name_tokens) for name in names)
                            and is_handwritten_action_sequence_literal(child.value)
                        ):
                            size = literal_container_len(child.value)
                            label = "/".join(names[:3])
                            handwritten_action_fixture_tests.append(
                                f"{path}:{node.name}:line {getattr(child, 'lineno', '?')}:{label}[{size}]"
                            )
                        if (
                            names
                            and any("near" in name and "goal" in name for name in names)
                            and isinstance(child.value, (ast.List, ast.Tuple))
                        ):
                            literal_near_goal_fixture_tests.append(
                                f"{path}:{node.name}:line {getattr(child, 'lineno', '?')}"
                            )
                        if near_goal_claimed and names and is_small_literal_action_item(child.value):
                            if any(any(token in name for token in start_fixture_name_tokens) for name in names):
                                literal_start_lines.append(getattr(child, "lineno", "?"))
                            if any(any(token in name for token in goal_fixture_name_tokens) for name in names):
                                literal_goal_lines.append(getattr(child, "lineno", "?"))
                    elif isinstance(child, ast.Call):
                        name = call_name(child)
                        if not any(token in name for token in verifierish_tokens):
                            continue
                        for arg in child.args:
                            if is_handwritten_action_sequence_literal(arg):
                                handwritten_action_fixture_tests.append(
                                    f"{path}:{node.name}:line {getattr(child, 'lineno', '?')}:verifier_arg[{literal_container_len(arg)}]"
                                )
                if near_goal_claimed and literal_start_lines and literal_goal_lines:
                    literal_near_goal_fixture_tests.append(
                        f"{path}:{node.name}:literal start/goal lines "
                        f"{literal_start_lines[0]}/{literal_goal_lines[0]}"
                    )
        if not solver_test_seen:
            issues.append(
                "state_space_search test artifact が solver/search callableを直接呼んでいません。"
                "PlanRecordの実行証拠として、探索結果のaction列を生成するテストを追加してください。"
            )
        elif not replaying_solver_test_seen:
            issues.append(
                "state_space_search test artifact が solver/searchの戻り値を legal move replay / final_verifier / goal assertion で検証していません。"
                "solution is not None だけでは不十分です。返されたaction列を合法手として再生し、goal到達をassertしてください。"
            )
        if solver_tests_with_pass:
            issues.append(
                "state_space_search solver test内に pass が残っています: "
                + ", ".join(solver_tests_with_pass[:4])
                + "。未検証ループで成功扱いにせず、各actionをreplayしてassertしてください。"
            )
        if randomized_or_broad_solver_tests:
            issues.append(
                "state_space_search solver testが random/shuffle に依存した広いfixtureで探索を実行しています: "
                + ", ".join(randomized_or_broad_solver_tests[:4])
                + "。unit testは決定的なnear-goal fixtureに縮小してください。"
                "goalまたは既知の基準stateから短い合法transitionで導出できるstart stateを明示し、solver/searchを1回だけ呼び、"
                "返ったaction列をfinal_verifier/legal replayへ渡してgoal到達をassertしてください。"
            )
        if unbounded_no_solution_solver_tests:
            issues.append(
                "state_space_search no-solution/unsolvable testが境界なしでsolver/searchを実行しています: "
                + ", ".join(unbounded_no_solution_solver_tests[:4])
                + "。no-solution系は状態空間全探索でtimeoutしやすいため、unit testでは"
                "solvability_checkを単体検証するか、max_depth/max_nodes/timeout等の明示境界付きでsolver/searchを呼んでください。"
                "全体unittestで広い不可解fixtureを無制限探索させてはいけません。"
            )
        if handwritten_action_fixture_tests:
            issues.append(
                "state_space_search solver testが長い手書きaction/path/solution fixtureを正解として埋め込んでいます: "
                + ", ".join(handwritten_action_fixture_tests[:4])
                + "。unit testは長い解列をテスト内で固定せず、solver/searchの戻り値をそのまま"
                "legal move replay / final_verifierへ渡してください。start fixtureは短い合法遷移で導出できる"
                "小さいnear-goalに縮小し、期待するのは具体的な経路列ではなくgoal到達です。"
            )
        if literal_near_goal_fixture_tests:
            issues.append(
                "state_space_search test artifact に literal near-goal fixture があります: "
                + ", ".join(literal_near_goal_fixture_tests[:4])
                + "。near-goalと名付けるfixtureは任意の値を直書きせず、goalや既知の基準stateから短いlegal move/transitionを"
                "適用して導出してください。runtimeはタスク専用oracleを持たないため、fixtureの由来を"
                "テストコード上で観測できる形にしてください。"
            )
        return issues

    def _dynamic_programming_test_contract_issues(
        self,
        test_sources: list[tuple[str, str]],
        *,
        user_message: str = "",
    ) -> list[str]:
        """Return generic test-artifact issues for dynamic-programming plans."""

        if str(self._plan_record_execution_context().get("plan_strategy") or "") != "dynamic_programming":
            return []

        def call_name(call: ast.Call) -> str:
            func = call.func
            if isinstance(func, ast.Name):
                return func.id.lower()
            if isinstance(func, ast.Attribute):
                return func.attr.lower()
            return ""

        solverish_tokens = (
            "solve",
            "compute",
            "count",
            "optimize",
            "optimal",
            "best",
            "dp",
            "memo",
            "tabulate",
        )
        requested_function_names = {
            name.lower()
            for name in self._requested_top_level_function_names(user_message)
        }
        assertion_names = {
            "assertequal",
            "assertnotequal",
            "asserttrue",
            "assertfalse",
            "assertisnone",
            "assertisnotnone",
            "assertgreater",
            "assertgreaterequal",
            "assertless",
            "assertlessequal",
        }
        solver_test_seen = False
        assertion_count = 0
        base_or_sample_seen = False
        recurrence_or_transition_seen = False
        for _path, source in test_sources:
            try:
                tree = ast.parse(str(source or ""))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if not node.name.startswith("test"):
                    continue
                test_source = ast.get_source_segment(str(source or ""), node) or ""
                lowered = test_source.lower()
                calls = [call_name(call) for call in ast.walk(node) if isinstance(call, ast.Call)]
                if any(name in requested_function_names for name in calls if name) or any(
                    any(token in name for token in solverish_tokens)
                    for name in calls
                    if name
                ):
                    solver_test_seen = True
                assertion_count += sum(1 for name in calls if name in assertion_names)
                if (
                    any(token in lowered for token in ("base", "base_case", "sample", "oracle", "initial"))
                    or any(token in test_source for token in ("基底", "初期", "期待値"))
                ):
                    base_or_sample_seen = True
                if (
                    any(token in lowered for token in ("recurrence", "transition", "subproblem", "memo", "table", "dp"))
                    or any(token in test_source for token in ("漸化", "遷移", "部分問題"))
                ):
                    recurrence_or_transition_seen = True
        issues: list[str] = []
        if not solver_test_seen:
            issues.append(
                "dynamic_programming test artifact が solver/compute callableを直接呼んでいません。"
                "PlanRecordの実行証拠として、DP本体を呼び出し期待値と比較するunittestを追加してください。"
            )
        if assertion_count < 2 or not base_or_sample_seen or not recurrence_or_transition_seen:
            issues.append(
                "dynamic_programming test artifact は base case と recurrence/sample oracle の両方を観測可能に検証していません。"
                "最小subproblemの期待値と、漸化式またはDP表更新で導かれるsampleケースを別々にassertしてください。"
            )
        return issues

    def _constraint_satisfaction_test_contract_issues(self, test_sources: list[tuple[str, str]]) -> list[str]:
        """Return generic test-artifact issues for constraint-satisfaction plans."""

        if str(self._plan_record_execution_context().get("plan_strategy") or "") != "constraint_satisfaction":
            return []

        def call_name(call: ast.Call) -> str:
            func = call.func
            if isinstance(func, ast.Name):
                return func.id.lower()
            if isinstance(func, ast.Attribute):
                return func.attr.lower()
            return ""

        solverish_tokens = ("solve", "search", "assign", "backtrack", "satisfy", "csp")
        validator_tokens = ("constraint", "consistent", "valid", "validate", "verify", "check", "satisf")
        solver_test_seen = False
        validator_test_seen = False
        negative_case_seen = False
        for _path, source in test_sources:
            try:
                tree = ast.parse(str(source or ""))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if not node.name.startswith("test"):
                    continue
                test_source = ast.get_source_segment(str(source or ""), node) or ""
                lowered = test_source.lower()
                calls = [call_name(call) for call in ast.walk(node) if isinstance(call, ast.Call)]
                if any(any(token in name for token in solverish_tokens) for name in calls if name):
                    solver_test_seen = True
                if any(any(token in name for token in validator_tokens) for name in calls if name):
                    validator_test_seen = True
                if (
                    any(token in lowered for token in ("negative", "invalid", "reject", "unsatisfied", "violate"))
                    or any(token in test_source for token in ("不正", "違反", "失敗", "拒否"))
                    or any(name in {"assertfalse", "assertraises", "assertisnone"} for name in calls)
                ):
                    negative_case_seen = True
        issues: list[str] = []
        if not solver_test_seen:
            issues.append(
                "constraint_satisfaction test artifact が solver/search/backtrack callableを直接呼んでいません。"
                "PlanRecordの実行証拠として、候補assignmentを生成するテストを追加してください。"
            )
        if not validator_test_seen:
            issues.append(
                "constraint_satisfaction test artifact が constraint checker / solution validator を直接検証していません。"
                "solverの戻り値をvalidate/checkし、全制約充足をassertしてください。"
            )
        if not negative_case_seen:
            issues.append(
                "constraint_satisfaction test artifact に invalid/negative assignment rejection の検証がありません。"
                "不正な割当がconstraint_checkerまたはsolution_validatorで拒否されることをassertしてください。"
            )
        return issues

    def _test_source_contract_issues(
        self,
        *,
        user_message: str,
        test_sources: list[tuple[str, str]],
    ) -> list[str]:
        """Observable test-source issues for generic implementation tasks."""

        issues: list[str] = []
        for issue in self._test_source_contradictory_predicate_expectation_issues(test_sources):
            if issue not in issues:
                issues.append(issue)
        for issue in self._state_space_search_test_contract_issues(test_sources):
            if issue not in issues:
                issues.append(issue)
        for issue in self._dynamic_programming_test_contract_issues(test_sources, user_message=user_message):
            if issue not in issues:
                issues.append(issue)
        for issue in self._constraint_satisfaction_test_contract_issues(test_sources):
            if issue not in issues:
                issues.append(issue)
        return issues

    def _test_source_contradictory_predicate_expectation_issues(
        self,
        test_sources: list[tuple[str, str]],
    ) -> list[str]:
        """Detect deterministic test fixtures that assert both truth values for the same predicate input."""

        def literal_signature(node: ast.AST, env: dict[str, str]) -> str:
            if isinstance(node, ast.Name) and node.id in env:
                return env[node.id]
            if isinstance(node, ast.Constant):
                return repr(node.value)
            if isinstance(node, ast.List):
                items = [literal_signature(item, env) for item in node.elts]
                if any(item == "" for item in items):
                    return ""
                return "list[" + ",".join(items) + "]"
            if isinstance(node, ast.Tuple):
                items = [literal_signature(item, env) for item in node.elts]
                if any(item == "" for item in items):
                    return ""
                return "tuple[" + ",".join(items) + "]"
            if isinstance(node, ast.Set):
                items = [literal_signature(item, env) for item in node.elts]
                if any(item == "" for item in items):
                    return ""
                return "set[" + ",".join(sorted(items)) + "]"
            if isinstance(node, ast.Dict):
                pairs: list[str] = []
                for key, value in zip(node.keys, node.values):
                    if key is None:
                        return ""
                    key_sig = literal_signature(key, env)
                    value_sig = literal_signature(value, env)
                    if not key_sig or not value_sig:
                        return ""
                    pairs.append(f"{key_sig}:{value_sig}")
                return "dict[" + ",".join(sorted(pairs)) + "]"
            return ""

        def predicate_name(call: ast.Call) -> str:
            func = call.func
            if isinstance(func, ast.Attribute):
                return func.attr
            if isinstance(func, ast.Name):
                return func.id
            return ""

        issues: list[str] = []
        for path, source in test_sources:
            text = str(source or "")
            try:
                tree = ast.parse(text)
            except SyntaxError:
                continue
            expectations: dict[str, dict[bool, list[str]]] = {}
            none_expectations: dict[str, dict[bool, list[str]]] = {}
            for function_node in ast.walk(tree):
                if not isinstance(function_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if not function_node.name.startswith("test"):
                    continue
                env: dict[str, str] = {}
                call_result_env: dict[str, str] = {}
                for node in ast.walk(function_node):
                    if isinstance(node, ast.Assign):
                        value_sig = literal_signature(node.value, env)
                        if value_sig:
                            for target in node.targets:
                                if isinstance(target, ast.Name):
                                    env[target.id] = value_sig
                        if isinstance(node.value, ast.Call):
                            call = node.value
                            name = predicate_name(call)
                            arg_sigs = [literal_signature(arg, env) for arg in call.args]
                            if name and arg_sigs and not any(not item for item in arg_sigs):
                                signature = name + "(" + ",".join(arg_sigs) + ")"
                                for target in node.targets:
                                    if isinstance(target, ast.Name):
                                        call_result_env[target.id] = signature
                    if not isinstance(node, ast.Call):
                        continue
                    func = node.func
                    if not isinstance(func, ast.Attribute):
                        continue
                    assert_name = func.attr
                    if assert_name in {"assertIsNone", "assertIsNotNone"} and node.args:
                        target = node.args[0]
                        signature = ""
                        if isinstance(target, ast.Name):
                            signature = call_result_env.get(target.id, "")
                        elif isinstance(target, ast.Call):
                            name = predicate_name(target)
                            arg_sigs = [literal_signature(arg, env) for arg in target.args]
                            if name and arg_sigs and not any(not item for item in arg_sigs):
                                signature = name + "(" + ",".join(arg_sigs) + ")"
                        if signature:
                            expects_none = assert_name == "assertIsNone"
                            location = f"{path}:{getattr(node, 'lineno', '?')} {function_node.name}"
                            by_expected = none_expectations.setdefault(signature, {True: [], False: []})
                            by_expected[expects_none].append(location)
                        continue
                    if assert_name not in {"assertTrue", "assertFalse"} or not node.args:
                        continue
                    predicate_call = node.args[0]
                    if not isinstance(predicate_call, ast.Call):
                        continue
                    name = predicate_name(predicate_call)
                    if not name:
                        continue
                    arg_sigs = [literal_signature(arg, env) for arg in predicate_call.args]
                    if not arg_sigs or any(not item for item in arg_sigs):
                        continue
                    signature = name + "(" + ",".join(arg_sigs) + ")"
                    expected = assert_name == "assertTrue"
                    location = f"{path}:{getattr(node, 'lineno', '?')} {function_node.name}"
                    by_expected = expectations.setdefault(signature, {True: [], False: []})
                    by_expected[expected].append(location)
            for signature, by_expected in expectations.items():
                if by_expected[True] and by_expected[False]:
                    issues.append(
                        "test artifact has contradictory predicate expectations for the same input: "
                        f"{signature} is asserted both True and False at "
                        + ", ".join([*by_expected[False][:2], *by_expected[True][:2]])
                        + "。同じ入力に対する同じpredicateの期待値を片方へ統一するか、fixtureを別状態へ分けてください。"
                    )
            for signature, by_expected in none_expectations.items():
                if by_expected[True] and by_expected[False]:
                    issues.append(
                        "test artifact has contradictory call-result expectations for the same input: "
                        f"{signature} is asserted both None and not None at "
                        + ", ".join([*by_expected[True][:2], *by_expected[False][:2]])
                        + "。同じ入力に対する同じcallableの戻り値期待を片方へ統一するか、"
                        "solvable/no-solution fixtureを別状態へ分けてください。"
                    )
        return issues










    def _python_source_is_escaped_source_literal(self, source: str) -> bool:
        """Return whether a Python artifact is only a quoted source blob.

        `py_compile` accepts a file that consists of one giant string literal,
        but such a file has no executable definitions and cannot satisfy an
        implementation contract. This checks for that narrow observable shape
        without judging algorithm correctness.
        """
        text = str(source or "").strip()
        if not text:
            return False
        try:
            module = ast.parse(text)
        except SyntaxError:
            return False
        if len(module.body) != 1:
            return False
        only = module.body[0]
        if not isinstance(only, ast.Expr) or not isinstance(only.value, ast.Constant) or not isinstance(only.value.value, str):
            return False
        literal = only.value.value
        return bool("\n" in literal and re.search(r"(^|\n)\s*(def|class|import|from)\s+", literal))

    def _trailing_whitespace_normalized(self, text: str) -> str:
        return "\n".join(line.rstrip() for line in str(text or "").splitlines()).rstrip("\n")

    def _effective_python_source_for_edit(self, *, tool_name: str, tool_args: dict[str, Any]) -> str | None:
        path = str(tool_args.get("path") or "").strip()
        if not path.replace("\\", "/").endswith(".py"):
            return None
        if tool_name == "write_file":
            return str(tool_args.get("content") or "")
        target = (self.execution_root / path).expanduser().resolve()
        try:
            target.relative_to(self.execution_root)
        except ValueError:
            return None
        if tool_name == "append_file":
            existing = target.read_text(encoding="utf-8") if target.exists() else ""
            return existing + str(tool_args.get("content") or "")
        if tool_name == "replace_text":
            if not target.exists():
                return None
            content = target.read_text(encoding="utf-8")
            old_text = str(tool_args.get("old_text") or "")
            new_text = str(tool_args.get("new_text") or "")
            if content.count(old_text) == 1:
                return content.replace(old_text, new_text, 1)
            if self._trailing_whitespace_normalized(old_text) == self._trailing_whitespace_normalized(content):
                return new_text
        return None



    def _python_artifact_contract_issue(
        self,
        *,
        user_message: str,
        tool_name: str,
        tool_args: dict[str, Any],
    ) -> dict[str, Any] | None:
        if tool_name not in {"write_file", "append_file", "replace_text"}:
            return None
        contract = _finish_acceptance_contract(user_message)
        if not {"python_artifact_written", "tests_written"}.intersection(contract):
            return None
        path = str(tool_args.get("path") or "").strip()
        normalized_path = path.replace("\\", "/")
        if not normalized_path.endswith(".py"):
            return None
        source = self._effective_python_source_for_edit(tool_name=tool_name, tool_args=tool_args)
        if source is None:
            return None
        if normalized_path.endswith("tests/__init__.py"):
            return {
                "reason_code": "test_artifact_contract_incomplete",
                "message": "tests/__init__.py はpackage markerであり、unittestの検証成果物ではありません。実際の test_*.py を作成してください。",
                "allowed_next_actions": ["write_file", "replace_text"],
                "suggested_fix": "tests/test_*.py に具体的な self.assert* または assert を含むunittestを書いてください。",
            }
        if _artifact_path_is_test(normalized_path):
            if self._python_source_has_test_callable(source) and not _test_source_is_meaningful(source):
                return {
                    "reason_code": "test_artifact_contract_incomplete",
                    "message": "test file がpass-onlyまたはassertなしです。unittest要求の検証証拠として受け付けません。",
                    "allowed_next_actions": ["write_file", "replace_text"],
                    "suggested_fix": "test_* メソッドに具体的な self.assert* または assert を追加してください。",
                }
            plan_test_issues = self._test_source_contract_issues(
                user_message=user_message,
                test_sources=[(normalized_path, source)],
            )
            if plan_test_issues:
                plan_strategy = str(self._plan_record_execution_context().get("plan_strategy") or "")
                if plan_strategy == "dynamic_programming":
                    suggested_fix = (
                        "base case と recurrence/sample oracle を別々にassertし、DP本体のcompute/solve callableを直接呼ぶunittestにしてください。"
                    )
                elif plan_strategy == "constraint_satisfaction":
                    suggested_fix = (
                        "solverの戻り値をconstraint checker / solution validatorへ渡し、valid assignment成功とinvalid/negative assignment拒否をassertするunittestにしてください。"
                    )
                else:
                    suggested_fix = (
                        "solver/searchの戻り値をlegal move replayまたはfinal_verifierへ渡し、goal到達をassertするunittestにしてください。"
                    )
                if "contradictory predicate expectations" in plan_test_issues[0]:
                    suggested_fix = (
                        "同じ入力fixtureを同じpredicateでTrue/False両方に期待しています。"
                        "片方の期待値を直すか、not-solved用fixtureとgoal用fixtureを別状態に分けてください。"
                    )
                elif "contradictory call-result expectations" in plan_test_issues[0]:
                    suggested_fix = (
                        "同じ入力fixtureを同じcallableでNone/not None両方に期待しています。"
                        "solvable fixtureとno-solution fixtureを別状態に分け、各期待を一貫させてください。"
                    )
                elif "no-solution/unsolvable" in plan_test_issues[0]:
                    suggested_fix = (
                        "no-solution系testはsolvability_check単体、またはmax_depth/max_nodes/timeout等の"
                        "明示境界付きsolver呼び出しに縮小してください。"
                    )
                return {
                    "reason_code": "test_artifact_contract_incomplete",
                    "message": plan_test_issues[0],
                    "allowed_next_actions": ["write_file", "replace_text"],
                    "suggested_fix": suggested_fix,
                }
            large_fixture_issue = self._test_source_large_fixture_issue(
                user_message=user_message,
                source=source,
            )
            if large_fixture_issue is not None:
                return large_fixture_issue
            return None
        if "python_artifact_written" not in contract or not _artifact_path_is_python_implementation(normalized_path):
            return None
        if self._python_source_is_escaped_source_literal(source):
            return {
                "reason_code": "python_artifact_escaped_source_literal",
                "message": (
                    "Pythonファイル全体が、実行されるコードではなくエスケープされたソース文字列リテラルになっています。"
                    "py_compileは通っても、importしても関数やクラスが定義されないため実装成果物として受け付けません。"
                ),
                "allowed_next_actions": ["write_file", "replace_text"],
                "suggested_fix": "コードを引用符で包まず、def/class/import をトップレベル構文として含む通常のPythonファイルを書いてください。",
            }
        placeholder_markers = self._python_source_placeholder_markers(source)
        if placeholder_markers:
            marker_signature = hashlib.sha1(
                "\n".join(sorted(placeholder_markers)).encode("utf-8", errors="replace")
            ).hexdigest()[:16]
            return {
                "reason_code": "python_artifact_contract_incomplete",
                "message": "Python実装にpass-only/TODO/ellipsis/NotImplementedError/placeholder-only return の関数またはメソッドが残っています。実装要求の成果物として受け付けません。",
                "allowed_next_actions": ["write_file", "replace_text"],
                "suggested_fix": (
                    "全callableからplaceholderを0個にし、骨組みではなく実行可能な本体を持つ完全なPython実装として"
                    "ファイル全体を構文的に有効な状態で再提出してください。"
                ),
                "recovery_class": "contract_reducing_full_implementation_required",
                "placeholder_markers": placeholder_markers,
                "block_signature": f"placeholder:{marker_signature}",
            }
        incomplete_markers = self._python_source_incomplete_implementation_markers(source)
        if incomplete_markers:
            return {
                "reason_code": "python_artifact_contract_incomplete",
                "message": (
                    "Python実装に、pass文や「次で直す」前提の未完成ブロックが残っています。"
                    "実装要求の成果物として受け付けません。"
                ),
                "allowed_next_actions": ["write_file", "replace_text"],
                "suggested_fix": (
                    "未完成ブロックを残さず、現在のファイル単体で実行できる完全なPython実装にしてください。"
                    f" 検出箇所: {', '.join(incomplete_markers[:4])}"
                ),
            }
        return None

    def _validation_failure_consultant_note(
        self,
        *,
        user_message: str,
        tool_result: dict[str, Any],
        steps: list[dict[str, Any]],
        turn_workspace: Path,
        current_model: str,
    ) -> dict[str, Any] | None:
        if not bool(self.runtime_config.get("validation_failure_consultant_enabled", True)):
            return None
        if bool(tool_result.get("ok")):
            return None
        command = str(tool_result.get("command") or "").strip()
        command_lower = command.lower()
        if not any(marker in command_lower for marker in ["unittest", "pytest"]):
            return None
        if "unittest_run" not in _finish_acceptance_contract(user_message):
            return None
        stderr = str(tool_result.get("stderr") or "")
        stdout = str(tool_result.get("stdout") or "")
        if self._command_failure_has_actionable_diagnostics(tool_result):
            return None
        evidence = (stderr or stdout)[-4000:]
        observed_context = self._validation_failure_observed_file_context(
            steps=steps,
            tool_result=tool_result,
            turn_workspace=turn_workspace,
        )
        current_context = self._validation_failure_current_file_context(
            steps=steps,
            tool_result=tool_result,
            turn_workspace=turn_workspace,
        )
        consultant = self.router.consultant_model(current_model=current_model, purpose="validation_failure")
        prompt = (
            "You are a second-opinion debugging reviewer for an AI coding agent. "
            "The actor LLM must still make and execute the next tool action. "
            "Do not provide a full replacement file. Give concise, actionable advice only.\n\n"
            "Return plain text, 3-6 short bullet points. Include:\n"
            "- the likely direct cause of the failing validation\n"
            "- whether the next edit target is implementation source, test source, or both\n"
            "- the concrete next edit strategy\n"
            "- what command should be rerun after the edit\n\n"
            "Use OBSERVED_RELEVANT_FILE_CONTENTS when present. These contents are actor-observed evidence from read_file events; "
            "also use RUNTIME_CURRENT_FILE_CONTENTS, which are the current artifacts the actor already wrote. "
            "Do not assume the failing test expectation is correct. Compare the traceback, user request, test source, and implementation source. "
            "If the test fixture or assertion contradicts the requested contract, say to edit the test. "
            "If implementation behavior contradicts the requested contract, say to edit the implementation. "
            "If both are wrong, list both targets and the order. "
            "If no relevant source is present, recommend one bounded read of the failing traceback file before choosing implementation vs test edits.\n\n"
            f"USER_REQUEST:\n{user_message}\n\n"
            f"FAILED_COMMAND:\n{command}\n\n"
            f"STDERR_OR_STDOUT:\n{evidence}\n"
            f"\nOBSERVED_RELEVANT_FILE_CONTENTS:\n{observed_context or '(none observed yet)'}\n"
            f"\nRUNTIME_CURRENT_FILE_CONTENTS:\n{current_context or '(none available)'}\n"
        )
        try:
            response = self.llm_backend.chat(
                model=str(consultant.get("model") or ""),
                messages=[{"role": "user", "content": prompt}],
                options={
                    **dict(self.ollama_options.get(str(consultant.get("role") or ""), {})),
                    "temperature": 0.1,
                    "num_predict": 512,
                },
                timeout_seconds=int(self.runtime_config.get("chat_timeout_seconds") or 180),
            )
            advice = str(response.get("content") or response.get("content_text") or "").strip()
        except Exception as exc:
            advice = f"相談役LLMを呼び出せませんでした: {exc}"
        max_chars = int(self.runtime_config.get("validation_failure_consultant_max_chars") or 1200)
        if not advice:
            advice = "テスト失敗のstderrを読み、失敗箇所を直接修正してから同じunittestを再実行してください。"
        return {
            "advice": advice[:max_chars],
            "consultant_model": consultant,
            "failed_command": command,
            "returncode": tool_result.get("returncode"),
            "stderr_preview": stderr[-1200:],
            "observed_file_context_preview": observed_context[:1200],
            "current_file_context_preview": current_context[:1200],
        }

    def _command_failure_has_actionable_diagnostics(self, tool_result: dict[str, Any]) -> bool:
        """Return whether command output already gives the actor a concrete repair anchor."""

        if bool(tool_result.get("ok")):
            return False
        output = "\n".join(
            str(tool_result.get(key) or "")
            for key in ("stderr", "stdout", "error")
            if str(tool_result.get(key) or "").strip()
        )
        if not output.strip():
            return False
        if re.search(r'File "([^"]+\.py)", line \d+', output):
            return True
        if re.search(
            r"\b(AssertionError|SyntaxError|ImportError|ModuleNotFoundError|NameError|TypeError|ValueError|"
            r"IndexError|KeyError|AttributeError|RuntimeError|Exception|Error):",
            output,
        ):
            return True
        return False

    def _review_file_excerpt(self, text: str, *, head_chars: int = 2400, tail_chars: int = 2600) -> str:
        source = str(text or "")
        if len(source) <= head_chars + tail_chars + 200:
            return source
        return (
            source[:head_chars]
            + "\n\n... [middle omitted for review budget] ...\n\n"
            + source[-tail_chars:]
        )

    def _replace_text_current_source_excerpt(
        self,
        *,
        current_source: str,
        old_text: str,
        max_lines: int = 36,
    ) -> str:
        source = str(current_source or "")
        if not source.strip():
            return ""
        old = str(old_text or "")
        lines = source.splitlines()
        start_index = 0
        name_match = re.search(r"(?m)^\s*(?:async\s+def|def|class)\s+([A-Za-z_][A-Za-z0-9_]*)\b", old)
        if name_match:
            name = re.escape(name_match.group(1))
            source_match = re.search(rf"(?m)^\s*(?:async\s+def|def|class)\s+{name}\b.*$", source)
            if source_match:
                start_index = source[: source_match.start()].count("\n")
        elif old.strip():
            for old_line in old.splitlines():
                needle = old_line.strip()
                if len(needle) < 8:
                    continue
                for index, source_line in enumerate(lines):
                    if needle in source_line:
                        start_index = max(0, index - 4)
                        break
                else:
                    continue
                break
        end_index = min(len(lines), start_index + max_lines)
        numbered = [
            f"{line_number:>4}: {line}"
            for line_number, line in enumerate(lines[start_index:end_index], start=start_index + 1)
        ]
        return "\n".join(numbered)

    def _validation_failure_current_file_context(
        self,
        *,
        steps: list[dict[str, Any]],
        tool_result: dict[str, Any],
        turn_workspace: Path,
    ) -> str:
        output = "\n".join(
            str(tool_result.get(key) or "")
            for key in ("stderr", "stdout", "error")
            if str(tool_result.get(key) or "")
        )
        failure_paths = self._extract_unittest_failure_paths(
            output=output,
            turn_workspace=turn_workspace,
        )
        latest_impl_path = ""
        latest_test_path = ""
        for step in steps:
            step_tool = str(step.get("tool_name") or "")
            step_args = step.get("tool_args") if isinstance(step.get("tool_args"), dict) else {}
            step_result = step.get("tool_result") if isinstance(step.get("tool_result"), dict) else {}
            if step_tool not in {"write_file", "append_file", "replace_text"} or not bool(step_result.get("ok")):
                continue
            path = str(step_result.get("path") or step_args.get("path") or "").replace("\\", "/")
            if _artifact_path_is_python_implementation(path):
                latest_impl_path = path
            elif _artifact_path_is_test(path):
                latest_test_path = path

        candidate_paths: list[str] = []
        for item in [*failure_paths, latest_test_path, latest_impl_path]:
            normalized = str(item or "").replace("\\", "/")
            if normalized and normalized not in candidate_paths:
                candidate_paths.append(normalized)

        workspace = Path(turn_workspace).resolve()
        observations: list[str] = []
        for candidate in candidate_paths:
            path = (workspace / candidate).resolve()
            try:
                path.relative_to(workspace)
            except ValueError:
                continue
            if not path.exists() or not path.is_file():
                continue
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if content:
                observations.append(
                    f"--- {candidate} (current artifact) ---\n"
                    + self._review_file_excerpt(content, head_chars=1800, tail_chars=1800)
                )
        return "\n\n".join(observations)[-9000:]

    def _validation_failure_observed_file_context(
        self,
        *,
        steps: list[dict[str, Any]],
        tool_result: dict[str, Any],
        turn_workspace: Path,
    ) -> str:
        output = "\n".join(
            str(tool_result.get(key) or "")
            for key in ("stderr", "stdout", "error")
            if str(tool_result.get(key) or "")
        )
        failure_paths = self._extract_unittest_failure_paths(
            output=output,
            turn_workspace=turn_workspace,
        )
        latest_impl_path = ""
        latest_test_path = ""
        for step in steps:
            step_tool = str(step.get("tool_name") or "")
            step_args = step.get("tool_args") if isinstance(step.get("tool_args"), dict) else {}
            step_result = step.get("tool_result") if isinstance(step.get("tool_result"), dict) else {}
            if step_tool not in {"write_file", "append_file", "replace_text"} or not bool(step_result.get("ok")):
                continue
            path = str(step_result.get("path") or step_args.get("path") or "").replace("\\", "/")
            if _artifact_path_is_python_implementation(path):
                latest_impl_path = path
            elif _artifact_path_is_test(path):
                latest_test_path = path

        candidate_paths: list[str] = []
        for item in [*failure_paths, latest_test_path, latest_impl_path]:
            normalized = str(item or "").replace("\\", "/")
            if normalized and normalized not in candidate_paths:
                candidate_paths.append(normalized)

        observations: list[str] = []
        for candidate in candidate_paths:
            for step in reversed(steps):
                if str(step.get("tool_name") or "") != "read_file":
                    continue
                step_args = step.get("tool_args") if isinstance(step.get("tool_args"), dict) else {}
                step_result = step.get("tool_result") if isinstance(step.get("tool_result"), dict) else {}
                if not bool(step_result.get("ok")):
                    continue
                path = str(step_result.get("path") or step_args.get("path") or "").replace("\\", "/")
                if path != candidate:
                    continue
                content = str(step_result.get("content") or "")
                if content:
                    observations.append(f"--- {candidate} (read_file observed) ---\n{content[-2200:]}")
                break
        return "\n\n".join(observations)[-7000:]

    def _semantic_review_is_usable(self, review: str) -> bool:
        """Return whether consultant text is usable as advice to the actor.

        The consultant is allowed to be wrong about the implementation, but it
        must not hand the actor a prompt echo or meta-instructions. Runtime
        owns that communication contract before turning the text into a
        system_note.
        """
        text = str(review or "").strip()
        if not text:
            return False
        lowered = text.lower()
        prompt_echo_markers = [
            "role: second-opinion",
            "goal: review",
            "constraints:",
            "checklist:",
            "user request:",
            "implementation_files",
            "test_files",
            "return plain text",
            "do not write code",
            "the actor llm must",
            "second-opinion semantic implementation reviewer",
            "review a rejected python implementation proposal",
            "identifier-preserving mapping",
            "suggest a concrete next implementation strategy",
            "check for placeholders",
        ]
        if sum(1 for marker in prompt_echo_markers if marker in lowered) >= 2:
            return False
        actionable_markers = [
            "不足",
            "満た",
            "修正",
            "確認",
            "テスト",
            "検証",
            "実装",
            "次",
            "missing",
            "should",
            "fix",
            "test",
            "validate",
        ]
        return any(marker in lowered for marker in actionable_markers)

    def _semantic_issue_targets_test_artifact(self, issue: str) -> bool:
        text = str(issue or "")
        lowered = text.lower()
        prefix = text.split(":", 1)[0].strip()
        if _artifact_path_is_test(prefix):
            return True
        if "test artifact" in lowered or "test file" in lowered or "tests/test_" in lowered:
            return True
        if "テスト成果物" in text or "テストfixture" in text or "fixture" in lowered:
            return True
        api_test_markers = [
            "top-level public api",
            "直接検証",
            "公開関数",
            "importして呼び出す",
            "meaningful_tests",
            "tests_written",
        ]
        test_context_markers = ["unittest", "テスト", "tests/"]
        return any(marker in lowered or marker in text for marker in api_test_markers) and any(
            marker in lowered or marker in text for marker in test_context_markers
        )

    def _semantic_issue_test_repair_paths(
        self,
        *,
        semantic_issues: list[str],
        test_paths: list[str],
        latest_test_path: str = "",
    ) -> list[str]:
        explicit_paths = sorted(
            {
                issue.split(":", 1)[0].strip().replace("\\", "/")
                for issue in semantic_issues
                if _artifact_path_is_test(issue.split(":", 1)[0].strip().replace("\\", "/"))
            }
        )
        if explicit_paths:
            return explicit_paths
        if not any(self._semantic_issue_targets_test_artifact(issue) for issue in semantic_issues):
            return []
        candidates: list[str] = []
        for item in [*test_paths, latest_test_path]:
            normalized = str(item or "").replace("\\", "/").strip()
            if normalized and normalized not in candidates:
                candidates.append(normalized)
        return candidates or ["tests/test_*.py"]

    def _semantic_implementation_contract_issues(
        self,
        *,
        user_message: str,
        implementation_sources: list[tuple[str, str]],
        test_sources: list[tuple[str, str]],
    ) -> list[str]:
        """Return runtime-owned generic semantic contract observations."""
        impl_text = "\n".join(text for _, text in implementation_sources)
        issues: list[str] = []
        requested_api_test_issue = self._requested_top_level_api_test_issue(
            user_message=user_message,
            test_sources=test_sources,
        )
        if requested_api_test_issue:
            issues.append(requested_api_test_issue)
        for issue in self._test_source_contract_issues(
            user_message=user_message,
            test_sources=test_sources,
        ):
            if issue not in issues:
                issues.append(issue)
        for issue in self._implementation_source_contract_issues(
            user_message=user_message,
            source=impl_text,
        ):
            if issue not in issues:
                issues.append(issue)
        incomplete_markers = self._python_source_placeholder_markers(impl_text)
        if incomplete_markers:
            issues.append(f"未完成または失敗を隠しやすい実装マーカーが残っています: {', '.join(incomplete_markers)}。意図した失敗表現か確認してください。")
        return issues

    def _semantic_review_fallback(
        self,
        *,
        user_message: str,
        implementation_sources: list[tuple[str, str]],
        test_sources: list[tuple[str, str]],
        failed_command: str,
        failed_output: str,
    ) -> str:
        """Build a short observation-based review when the consultant fails."""
        contract_issues = self._semantic_implementation_contract_issues(
            user_message=user_message,
            implementation_sources=implementation_sources,
            test_sources=test_sources,
        )
        bullets = list(contract_issues)
        if failed_command:
            preview = failed_output.strip().splitlines()[-1:] or ["出力なし"]
            bullets.append(f"{failed_command} が失敗しています。末尾の失敗理由: {preview[0][:180]}")
        if not bullets:
            bullets.append("runtime観測上の必須未達は見つかりません。次はunittestを実行し、実行結果を完了証拠として確認してください。")
        else:
            bullets.append("次は不足項目を修正し、unittestを再実行してからfinish可否を判断してください。")
        return "\n".join(f"- {line}" for line in bullets[:8])

    def _semantic_review_checklist(self, *, user_message: str, path: str = "") -> str:
        return (
            "- whether the API/input form from the user request is preserved\n"
            "- whether the implementation contains the requested behavior, not only scaffolding\n"
            "- whether tests are meaningful or merely fitted to the current implementation\n"
            "- whether required validation/error cases from the user request are exercised\n"
            "- whether placeholder-only functions remain (pass, TODO, ellipsis, NotImplementedError, or a callable whose only executable statement returns None/[])\n"
            "- do not treat a legitimate no-result branch as placeholder when the function contains real logic\n"
            "- the next concrete strategy the actor should take before claiming success"
        )

    def _semantic_implementation_review_note(
        self,
        *,
        user_message: str,
        steps: list[dict[str, Any]],
        turn_workspace: Path,
        current_model: str,
        session_id: str,
        trigger: str,
        failed_tool_result: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if not bool(self.runtime_config.get("semantic_implementation_review_enabled", True)):
            return None
        contract = _finish_acceptance_contract(user_message)
        if "python_artifact_written" not in contract or "unittest_run" not in contract:
            return None
        evidence = _finish_acceptance_evidence(steps)
        if not bool(evidence.get("python_artifact_written")) or not bool(evidence.get("meaningful_tests")):
            return None

        workspace = Path(turn_workspace).resolve()
        sources: list[tuple[str, str]] = []
        seen_paths: set[str] = set()
        for path in evidence.get("artifact_paths") or []:
            normalized = str(path or "").replace("\\", "/")
            if not normalized or normalized in seen_paths:
                continue
            if not (_artifact_path_is_python_implementation(normalized) or _artifact_path_is_test(normalized)):
                continue
            candidate = (workspace / normalized).resolve()
            try:
                candidate.relative_to(workspace)
            except ValueError:
                continue
            if not candidate.exists() or not candidate.is_file():
                continue
            text = candidate.read_text(encoding="utf-8", errors="replace")
            if _artifact_path_is_test(normalized) and not _test_source_is_meaningful(text):
                continue
            sources.append((normalized, text))
            seen_paths.add(normalized)
        implementation_sources = [(path, text) for path, text in sources if _artifact_path_is_python_implementation(path)]
        test_sources = [(path, text) for path, text in sources if _artifact_path_is_test(path)]
        if not implementation_sources or not test_sources:
            return None

        failed_command = ""
        failed_output = ""
        if failed_tool_result:
            command = str(failed_tool_result.get("command") or "").strip()
            if "unittest" in command.lower() or "pytest" in command.lower():
                failed_command = command
                failed_output = (str(failed_tool_result.get("stderr") or "") or str(failed_tool_result.get("stdout") or ""))[-3000:]
                if self._command_failure_has_actionable_diagnostics(failed_tool_result):
                    return None

        hasher = hashlib.sha256()
        hasher.update(str(user_message).encode("utf-8"))
        for path, text in sources:
            hasher.update(path.encode("utf-8"))
            hasher.update(text.encode("utf-8", errors="replace"))
        if failed_command:
            hasher.update(b"unittest_failed")
            hasher.update(failed_command.encode("utf-8"))
            hasher.update(failed_output.encode("utf-8", errors="replace"))
        fingerprint = hasher.hexdigest()[:20]
        for event in self._semantic_review_state_events(session_id=session_id):
            if (
                event.get("type") == "system_note"
                and event.get("code") == "semantic_implementation_review"
                and isinstance(event.get("details"), dict)
                and event["details"].get("fingerprint") == fingerprint
            ):
                return None

        contract_issues = self._semantic_implementation_contract_issues(
            user_message=user_message,
            implementation_sources=implementation_sources,
            test_sources=test_sources,
        )
        fixture_review_items: list[dict[str, Any]] = []
        if not contract_issues and not failed_command:
            return None
        consultant_enabled = bool(self.runtime_config.get("semantic_implementation_consultant_enabled", False))
        consultant = self.router.consultant_model(current_model=current_model, purpose="semantic_implementation_review")
        impl_block = "\n\n".join(
            f"FILE: {path}\n{self._review_file_excerpt(text, head_chars=2800, tail_chars=3200)}"
            for path, text in implementation_sources
        )
        test_block = "\n\n".join(
            f"FILE: {path}\n{self._review_file_excerpt(text, head_chars=2200, tail_chars=2400)}"
            for path, text in test_sources
        )
        failure_block = (
            f"\n\nFAILED_UNITTEST_COMMAND:\n{failed_command}\n\nFAILED_OUTPUT:\n{failed_output}"
            if failed_command
            else ""
        )
        review_checklist = self._semantic_review_checklist(user_message=user_message)
        prompt = (
            "You are a second-opinion semantic implementation reviewer for an AI coding agent. "
            "The actor LLM must still write code and run tools. Do not write code, patches, or full replacement files. "
            "Return plain text only, 4-8 short Japanese bullets.\n\n"
            "Review whether the implementation and tests satisfy the user's requested API and completion conditions. Check:\n"
            f"{review_checklist}\n\n"
            f"TRIGGER:\n{trigger}\n\n"
            f"USER_REQUEST:\n{user_message}\n\n"
            f"IMPLEMENTATION_FILES:\n{impl_block}\n\n"
            f"TEST_FILES:\n{test_block}"
            f"{failure_block}\n"
        )
        if consultant_enabled:
            try:
                response = self.llm_backend.chat(
                    model=str(consultant.get("model") or ""),
                    messages=[{"role": "user", "content": prompt}],
                    options={
                        **dict(self.ollama_options.get(str(consultant.get("role") or ""), {})),
                        "temperature": 0.1,
                        "num_predict": 768,
                    },
                    timeout_seconds=int(self.runtime_config.get("semantic_implementation_consultant_timeout_seconds") or 20),
                )
                review = str(response.get("content_text") or response.get("content") or "").strip()
            except Exception as exc:
                review = f"相談役LLMを呼び出せませんでした: {exc}"
            raw_review = review
            review_source = "consultant"
            if not self._semantic_review_is_usable(review):
                review_source = "runtime_fallback_after_invalid_consultant_output"
                review = self._semantic_review_fallback(
                    user_message=user_message,
                    implementation_sources=implementation_sources,
                    test_sources=test_sources,
                    failed_command=failed_command,
                    failed_output=failed_output,
                )
        else:
            raw_review = ""
            review_source = "runtime_contract_review"
            review = self._semantic_review_fallback(
                user_message=user_message,
                implementation_sources=implementation_sources,
                test_sources=test_sources,
                failed_command=failed_command,
                failed_output=failed_output,
            )
        # Consultant text is advisory material. Runtime blocking must be tied
        # to structured, runtime-owned contract issues so a cautious reviewer
        # cannot turn non-required suggestions into a finish gate.
        requires_revision = bool(contract_issues)
        max_chars = int(self.runtime_config.get("semantic_implementation_review_max_chars") or 1600)
        return {
            "review": review[:max_chars],
            "review_source": review_source,
            "consultant_review_usable": review_source == "consultant",
            "requires_revision": requires_revision,
            "semantic_issues": contract_issues,
            "fixture_review_items": fixture_review_items,
            "invalid_consultant_review_preview": "" if review_source == "consultant" else raw_review[:800],
            "consultant_model": consultant,
            "trigger": trigger,
            "fingerprint": fingerprint,
            "implementation_paths": [path for path, _ in implementation_sources],
            "test_paths": [path for path, _ in test_sources],
            "failed_command": failed_command,
        }

    def _append_semantic_implementation_review_if_needed(
        self,
        *,
        session_id: str,
        turn_id: str,
        queue_id: str,
        step_index: int,
        turn_workspace: Path,
        user_message: str,
        steps: list[dict[str, Any]],
        current_model: str,
        trigger: str,
        failed_tool_result: dict[str, Any] | None = None,
    ) -> bool:
        note = self._semantic_implementation_review_note(
            user_message=user_message,
            steps=steps,
            turn_workspace=turn_workspace,
            current_model=current_model,
            session_id=session_id,
            trigger=trigger,
            failed_tool_result=failed_tool_result,
        )
        if note is None:
            return False
        review_source = str(note.get("review_source") or "")
        if review_source == "consultant":
            review_label = "相談役LLMからの実装レビュー"
        elif review_source == "runtime_fallback_after_invalid_consultant_output":
            review_label = "相談役LLMのレビュー出力が無効だったため、runtime観測レビュー"
        else:
            review_label = "runtime観測レビュー"
        self._append_session_event(
            self.root,
            session_id,
            {
                "type": "system_note",
                "role": "system",
                "content": f"{review_label}: {note['review']}",
                "code": "semantic_implementation_review",
                "reason_code": "consultant_advice" if review_source == "consultant" else "runtime_semantic_review_requires_revision",
                "details": note,
                "turn_id": turn_id,
                "queue_id": queue_id,
                "step_index": step_index,
                "llm_workspace": str(turn_workspace),
            },
        )
        return True

    def _semantic_blocked_implementation_review_note(
        self,
        *,
        user_message: str,
        session_id: str,
        turn_workspace: Path,
        current_model: str,
        blocked_tool_name: str,
        blocked_tool_args: dict[str, Any],
        blocked_issue: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not bool(self.runtime_config.get("semantic_implementation_review_enabled", True)):
            return None
        contract = _finish_acceptance_contract(user_message)
        if "python_artifact_written" not in contract or "unittest_run" not in contract:
            return None
        reason_code = str(blocked_issue.get("reason_code") or "")
        if reason_code not in {"python_artifact_contract_incomplete", "python_artifact_input_contract_narrowed"}:
            return None
        path = str(blocked_tool_args.get("path") or "").replace("\\", "/")
        if not path or not _artifact_path_is_python_implementation(path):
            return None
        source = self._effective_python_source_for_edit(
            tool_name=blocked_tool_name,
            tool_args=dict(blocked_tool_args),
        )
        if source is None:
            return None

        same_blocks = 0
        for event in reversed(self._semantic_review_state_events(session_id=session_id)):
            if (
                event.get("type") == "system_note"
                and event.get("code") == "edit_blocked"
                and str(event.get("reason_code") or "") == reason_code
            ):
                details = event.get("details") if isinstance(event.get("details"), dict) else {}
                previous_path = str(details.get("path") or "").replace("\\", "/")
                if previous_path == path or _artifact_path_is_python_implementation(previous_path):
                    same_blocks += 1
                    continue
            if event.get("type") == "tool_result" and bool(event.get("ok")):
                break
        if same_blocks < 2:
            return None

        hasher = hashlib.sha256()
        hasher.update(b"blocked_implementation_proposal")
        hasher.update(str(user_message).encode("utf-8"))
        hasher.update(path.encode("utf-8"))
        hasher.update(reason_code.encode("utf-8"))
        hasher.update(source.encode("utf-8", errors="replace"))
        fingerprint = hasher.hexdigest()[:20]
        for event in self._semantic_review_state_events(session_id=session_id):
            if (
                event.get("type") == "system_note"
                and event.get("code") == "semantic_implementation_review"
                and isinstance(event.get("details"), dict)
                and event["details"].get("fingerprint") == fingerprint
            ):
                return None

        implementation_sources = [(path, source)]
        test_sources: list[tuple[str, str]] = []
        contract_issues = self._semantic_implementation_contract_issues(
            user_message=user_message,
            implementation_sources=implementation_sources,
            test_sources=test_sources,
        )
        consultant = self.router.consultant_model(current_model=current_model, purpose="semantic_implementation_review")
        review_checklist = self._semantic_review_checklist(user_message=user_message, path=path)
        prompt = (
            "You are a second-opinion semantic implementation reviewer for an AI coding agent. "
            "The actor LLM must still write code and run tools. Do not write code, patches, or full replacement files. "
            "Return plain text only, 4-8 short Japanese bullets.\n\n"
            "The runtime rejected the actor's proposed Python implementation before writing it. "
            "Review the rejected proposal against the user's requested API and completion conditions. Check:\n"
            f"{review_checklist}\n\n"
            "TRIGGER:\nblocked_implementation_proposal\n\n"
            f"USER_REQUEST:\n{user_message}\n\n"
            f"BLOCKED_TOOL:\n{blocked_tool_name} {path}\n\n"
            f"BLOCK_REASON:\n{blocked_issue.get('message') or reason_code}\n\n"
            f"REJECTED_IMPLEMENTATION_PROPOSAL:\nFILE: {path}\n{source[-5000:]}\n"
        )
        try:
            response = self.llm_backend.chat(
                model=str(consultant.get("model") or ""),
                messages=[{"role": "user", "content": prompt}],
                options={
                    **dict(self.ollama_options.get(str(consultant.get("role") or ""), {})),
                    "temperature": 0.1,
                    "num_predict": 768,
                },
                timeout_seconds=int(self.runtime_config.get("chat_timeout_seconds") or 180),
            )
            review = str(response.get("content_text") or response.get("content") or "").strip()
        except Exception as exc:
            review = f"相談役LLMを呼び出せませんでした: {exc}"
        raw_review = review
        review_source = "consultant"
        if not self._semantic_review_is_usable(review):
            review_source = "runtime_fallback_after_invalid_consultant_output"
            review = self._semantic_review_fallback(
                user_message=user_message,
                implementation_sources=implementation_sources,
                test_sources=test_sources,
                failed_command="",
                failed_output="",
            )
        max_chars = int(self.runtime_config.get("semantic_implementation_review_max_chars") or 1600)
        return {
            "review": review[:max_chars],
            "review_source": review_source,
            "consultant_review_usable": review_source == "consultant",
            "requires_revision": True,
            "semantic_issues": contract_issues,
            "invalid_consultant_review_preview": "" if review_source == "consultant" else raw_review[:800],
            "consultant_model": consultant,
            "trigger": "blocked_implementation_proposal",
            "fingerprint": fingerprint,
            "implementation_paths": [path],
            "test_paths": [],
            "failed_command": "",
            "blocked_tool_name": blocked_tool_name,
            "blocked_reason_code": reason_code,
            "blocked_path": path,
        }

    def _append_blocked_implementation_semantic_review_if_needed(
        self,
        *,
        session_id: str,
        turn_id: str,
        queue_id: str,
        step_index: int,
        turn_workspace: Path,
        user_message: str,
        current_model: str,
        blocked_tool_name: str,
        blocked_tool_args: dict[str, Any],
        blocked_issue: dict[str, Any],
    ) -> bool:
        note = self._semantic_blocked_implementation_review_note(
            user_message=user_message,
            session_id=session_id,
            turn_workspace=turn_workspace,
            current_model=current_model,
            blocked_tool_name=blocked_tool_name,
            blocked_tool_args=blocked_tool_args,
            blocked_issue=blocked_issue,
        )
        if note is None:
            return False
        review_label = (
            "相談役LLMからの実装レビュー"
            if note.get("review_source") == "consultant"
            else "相談役LLMのレビュー出力が無効だったため、runtime観測レビュー"
        )
        self._append_session_event(
            self.root,
            session_id,
            {
                "type": "system_note",
                "role": "system",
                "content": f"{review_label}: {note['review']}",
                "code": "semantic_implementation_review",
                "reason_code": "consultant_advice",
                "details": note,
                "turn_id": turn_id,
                "queue_id": queue_id,
                "step_index": step_index,
                "llm_workspace": str(turn_workspace),
            },
        )
        return True

    def _latest_semantic_review_requiring_revision_event(self, *, session_id: str) -> dict[str, Any] | None:
        """Return the current unresolved semantic review event, if any.

        A semantic review is current when it was emitted at or after the latest
        successful implementation/test artifact write. Artifact-less rejected
        implementation proposals also count: the review is still the current
        TaskState fact even though no source file was accepted.
        """
        latest_artifact_step = -1
        latest_review: dict[str, Any] | None = None
        for event in self._semantic_review_state_events(session_id=session_id):
            step_index = int(event.get("step_index") or 0)
            if (
                event.get("type") == "tool_result"
                and bool(event.get("ok"))
                and str(event.get("tool_name") or "") in {"write_file", "append_file", "replace_text"}
            ):
                try:
                    result = json.loads(str(event.get("content") or "{}"))
                except json.JSONDecodeError:
                    result = {}
                path = str(result.get("path") or "")
                if _artifact_path_is_python_implementation(path) or _artifact_path_is_test(path):
                    latest_artifact_step = max(latest_artifact_step, step_index)
            if event.get("type") == "system_note" and event.get("code") == "semantic_implementation_review":
                latest_review = event
        if latest_review is None:
            return None
        review_step = int(latest_review.get("step_index") or 0)
        if review_step < latest_artifact_step:
            return None
        details = latest_review.get("details") if isinstance(latest_review.get("details"), dict) else {}
        if not bool(details.get("requires_revision")):
            return None
        issues = details.get("semantic_issues")
        fixture_items = details.get("fixture_review_items")
        if not (
            isinstance(issues, list)
            and any(str(issue).strip() for issue in issues)
        ) and not (
            isinstance(fixture_items, list)
            and any(isinstance(item, dict) for item in fixture_items)
        ):
            return None
        return latest_review

    def _latest_semantic_review_requires_revision(self, *, session_id: str) -> bool:
        """Return whether current TaskState has an unresolved semantic review.

        If it requires revision, deterministic unittest recovery must not
        bypass the actor.
        """
        return self._latest_semantic_review_requiring_revision_event(session_id=session_id) is not None

    def _latest_repeated_semantic_issue(self, *, session_id: str) -> dict[str, Any] | None:
        reviews: list[dict[str, Any]] = []
        for event in self._semantic_review_state_events(session_id=session_id):
            if event.get("type") != "system_note" or event.get("code") != "semantic_implementation_review":
                continue
            details = event.get("details") if isinstance(event.get("details"), dict) else {}
            issues = details.get("semantic_issues")
            if not bool(details.get("requires_revision")) or not isinstance(issues, list) or not issues:
                continue
            normalized = sorted(str(issue).strip() for issue in issues if str(issue).strip())
            if not normalized:
                continue
            reviews.append({
                "step_index": int(event.get("step_index") or 0),
                "issues": normalized,
                "fixture_review_items": details.get("fixture_review_items") if isinstance(details.get("fixture_review_items"), list) else [],
            })
        if len(reviews) < 2:
            return None
        previous = reviews[-2]
        latest = reviews[-1]
        if previous["issues"] != latest["issues"]:
            return None
        return {
            "previous_step_index": previous["step_index"],
            "latest_step_index": latest["step_index"],
            "semantic_issues": latest["issues"],
            "fixture_review_items": latest.get("fixture_review_items") or [],
        }


    def _work_package_blocked_event(
        self,
        *,
        session_id: str,
        turn_id: str,
        queue_id: str,
        step_index: int,
        turn_workspace: Path,
        tool_name: str,
        issues: list[str],
        tasks: list[dict[str, Any]] | None = None,
        user_message: str = "",
    ) -> dict[str, Any]:
        issue_lines = [str(issue).strip() for issue in issues if str(issue).strip()]
        fingerprint = self._work_package_invalid_fingerprint(
            tool_name=tool_name,
            issues=issue_lines,
            tasks=list(tasks or []),
        )
        previous_count = self._work_package_invalid_repeat_count(
            session_id=session_id,
            fingerprint=fingerprint,
        )
        repeated_invalid = previous_count >= 1
        terminal_failure = previous_count >= 2 and not self._session_has_successful_tool_evidence(session_id=session_id)
        implementation_task = bool(
            re.search(r"(implement|implementation|unittest|test|solve|solver|実装|テスト|検証)", str(user_message or ""), re.I)
        )
        allowed_next_actions: list[dict[str, Any]]
        if repeated_invalid and implementation_task:
            allowed_next_actions = [
                {
                    "tool": "write_file",
                    "strategy": "write the smallest complete implementation artifact directly in the parent frame",
                },
                {
                    "tool": "write_file",
                    "strategy": "if implementation already exists, write a meaningful tests/test_*.py file instead",
                },
                {
                    "tool": "read_file",
                    "strategy": "observe one concrete artifact only if needed before the next write",
                },
            ]
        else:
            allowed_next_actions = [
                {
                    "tool": tool_name,
                    "strategy": "retry with a concrete work_package whose first_action is directly executable",
                },
                {
                    "tool": "read_file",
                    "strategy": "observe the relevant file directly if one read can advance the parent task",
                },
                {
                    "tool": "run_command",
                    "strategy": "run a concrete validation command if execution evidence is already available",
                },
            ]
        suggested_fix = (
            "Do not restate the parent goal. Provide the first concrete tool call, "
            "the evidence that will satisfy the child task, and why the parent "
            "cannot take that action directly."
            if not repeated_invalid
            else "Stop retrying the same invalid decomposition. Take the next concrete parent-frame action instead."
        )
        next_required_action = (
            "retry with a concrete work_package whose first_action is directly executable"
            if not repeated_invalid
            else "take one direct parent-frame action from allowed_next_actions"
        )
        return self._append_session_event(
            session_id,
            {
                "type": "system_note",
                "role": "system",
                "content": f"{tool_name} requires a concrete work_package: {'; '.join(issue_lines)}",
                "code": "work_package_invalid",
                "reason_code": "missing_work_package_contract",
                "details": {
                    "blocked_tool": tool_name,
                    "issues": issue_lines,
                    "fingerprint": fingerprint,
                    "repeat_count": previous_count + 1,
                    "decompose_temporarily_disabled": bool(repeated_invalid and implementation_task),
                    "terminal_failure": terminal_failure,
                    "failure_type": "repeated_work_package_invalid" if repeated_invalid else "work_package_invalid",
                    "blocked_by": "work_package_contract",
                    "required_contract": [
                        "goal",
                        "work_type",
                        "first_action.tool",
                        "first_action.args",
                        "success_evidence",
                        "why_not_direct_action",
                    ],
                    "allowed_next_actions": allowed_next_actions,
                    "suggested_fix": suggested_fix,
                    "next_required_action": next_required_action,
                },
                "turn_id": turn_id,
                "queue_id": queue_id,
                "step_index": step_index,
                "llm_workspace": str(turn_workspace),
            },
        )

    def _work_package_invalid_fingerprint(
        self,
        *,
        tool_name: str,
        issues: list[str],
        tasks: list[dict[str, Any]],
    ) -> str:
        normalized_tasks: list[dict[str, Any]] = []
        for raw_task in tasks:
            work_package = self._normalize_work_package(raw_task)
            first_action = work_package.get("first_action") if isinstance(work_package.get("first_action"), dict) else {}
            args = first_action.get("args") if isinstance(first_action.get("args"), dict) else {}
            normalized_tasks.append(
                {
                    "goal": re.sub(r"\s+", " ", str(work_package.get("goal") or "").lower()).strip()[:200],
                    "work_type": str(work_package.get("work_type") or "").strip(),
                    "first_action_tool": str(first_action.get("tool") or "").strip(),
                    "first_action_target": str(args.get("path") or args.get("command") or "").strip()[:200],
                    "issue_keys": sorted(str(issue).split(":", 1)[-1].strip() for issue in issues),
                }
            )
        payload = {
            "tool_name": tool_name,
            "tasks": normalized_tasks,
            "issues": sorted(str(issue).strip() for issue in issues),
        }
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:20]

    def _work_package_invalid_repeat_count(self, *, session_id: str, fingerprint: str) -> int:
        count = 0
        for event in read_jsonl(self.paths.session_events_path(session_id), limit=120):
            if event.get("type") != "system_note" or event.get("code") != "work_package_invalid":
                continue
            details = event.get("details") if isinstance(event.get("details"), dict) else {}
            if str(details.get("fingerprint") or "") == fingerprint:
                count += 1
        return count

    def _session_has_successful_tool_evidence(self, *, session_id: str) -> bool:
        for event in read_jsonl(self.paths.session_events_path(session_id), limit=200):
            if event.get("type") == "tool_result" and bool(event.get("ok")):
                return True
        return False

    def _plan_semantic_risk_issues(self, *, user_message: str, tasks: list[dict[str, Any]]) -> list[str]:
        """Return semantic-fit risks that require a second opinion.

        Invariant: work packages are not accepted only because they are
        executable. When the user asks for an abstract external outcome, a
        static message/document artifact is a semantic interpretation, not evidence of
        completion. That interpretation must be explicitly accepted or blocked
        before any file/tool side effect.
        """
        user_text = str(user_message or "").lower()
        abstract_markers = (
            "改善",
            "向上",
            "解決",
            "支援",
            "幸せ",
            "役に立",
            "impact",
            "outcome",
            "improve",
            "better",
            "help",
            "reduce",
            "increase",
            "optimize",
        )
        external_outcome_markers = (
            "社会",
            "人",
            "ユーザー",
            "顧客",
            "業務",
            "生活",
            "現実",
            "外部",
            "people",
            "person",
            "user",
            "customer",
            "business",
            "external",
            "impact",
            "outcome",
        )
        if not (
            any(marker in user_text for marker in abstract_markers)
            and any(marker in user_text for marker in external_outcome_markers)
        ):
            return []
        issues: list[str] = []
        for task in tasks:
            work_package = self._normalize_work_package(task)
            first_action = work_package.get("first_action") or {}
            first_tool = str(first_action.get("tool") or "").strip()
            first_args = dict(first_action.get("args") or {}) if isinstance(first_action.get("args") or {}, dict) else {}
            content = str(first_args.get("content") or "")
            semantic_text = "\n".join(
                [
                    str(work_package.get("goal") or ""),
                    str(work_package.get("success_evidence") or ""),
                    str(work_package.get("why_not_direct_action") or ""),
                    content[:1000],
                ]
            ).lower()
            static_artifact_markers = (
                "メッセージ",
                "表示する",
                "出力する",
                "計画",
                "アクションプラン",
                "問題を特定",
                "解決策を考える",
                "行動計画",
                "print(",
            )
            if first_tool in {"write_file", "append_file"} and any(marker in semantic_text for marker in static_artifact_markers):
                issues.append(
                    f"{work_package.get('task_id') or 'task'}: abstract user outcome was concretized as a static artifact instead of an executable outcome"
                )
        return issues

    def _plan_acceptance_blocked_event(
        self,
        *,
        session_id: str,
        turn_id: str,
        queue_id: str,
        step_index: int,
        turn_workspace: Path,
        tool_name: str,
        issues: list[str],
        review: dict[str, Any],
        tasks: list[dict[str, Any]],
    ) -> dict[str, Any]:
        reason_code = str(review.get("reason_code") or "plan_semantic_mismatch")
        rationale = str(review.get("rationale") or "; ".join(issues))
        suggested_fix = str(review.get("suggested_next_action") or "Clarify the intended concrete outcome before creating artifacts.")
        return self._append_session_event(
            session_id,
            {
                "type": "system_note",
                "role": "system",
                "content": f"{tool_name} was blocked by plan acceptance gate: {rationale}",
                "code": "plan_acceptance_blocked",
                "reason_code": reason_code,
                "details": {
                    "blocked_tool": tool_name,
                    "issues": issues,
                    "review": review,
                    "tasks": tasks,
                    "contract": "child task must advance the user's requested outcome, not merely be executable",
                    "failure_type": "plan_acceptance_blocked",
                    "blocked_by": "plan_acceptance_gate",
                    "allowed_next_actions": [
                        {
                            "tool": "finish",
                            "strategy": "ask the user to confirm the intended interpretation of the abstract request",
                        },
                        {
                            "tool": tool_name,
                            "strategy": "retry with tasks whose first_action and success_evidence directly support the user request",
                        },
                    ],
                    "suggested_fix": suggested_fix,
                    "next_required_action": suggested_fix,
                },
                "turn_id": turn_id,
                "queue_id": queue_id,
                "step_index": step_index,
                "llm_workspace": str(turn_workspace),
            },
        )

    def _consult_plan_acceptance(
        self,
        *,
        user_message: str,
        tasks: list[dict[str, Any]],
        current_model: str,
    ) -> dict[str, Any]:
        consultant = self.router.consultant_model(current_model=current_model, purpose="plan_acceptance")
        prompt = (
            "You are a skeptical runtime plan reviewer. Decide whether the proposed child tasks semantically advance "
            "the user's request. Do not judge whether the tasks are syntactically valid; judge whether the plan is a "
            "reasonable interpretation of the user's actual outcome.\n\n"
            "Return exact JSON only with this schema:\n"
            '{"verdict":"accept|reject","reason_code":"...","rationale":"...","suggested_next_action":"..."}\n\n'
            f"USER_REQUEST:\n{user_message}\n\n"
            f"PROPOSED_TASKS_JSON:\n{json.dumps(tasks, ensure_ascii=False, indent=2)}\n\n"
            "Reject if an abstract request is trivially converted into a static message, static document, or planning artifact without user confirmation."
        )
        try:
            response = self.llm_backend.chat(
                model=str(consultant.get("model") or ""),
                messages=[{"role": "user", "content": prompt}],
                options=dict(self.ollama_options.get(str(consultant.get("role") or ""), {})),
                timeout_seconds=int(self.runtime_config.get("chat_timeout_seconds") or 180),
            )
            raw_text = str(response.get("content") or response.get("content_text") or "")
            payload = json.loads(self._extract_json_object(raw_text) or raw_text)
            schema_validation = validate_json_schema(payload, PLAN_ACCEPTANCE_SCHEMA)
            if not schema_validation.ok:
                return {
                    "verdict": "reject",
                    "reason_code": "plan_acceptance_consultant_invalid_output",
                    "rationale": "; ".join(schema_validation.errors),
                    "suggested_next_action": "Ask the user to confirm the intended concrete outcome before executing the plan.",
                    "consultant_model": consultant,
                    "raw_text": raw_text[:2000],
                }
            return {**payload, "consultant_model": consultant, "raw_text": raw_text[:2000]}
        except Exception as exc:
            return {
                "verdict": "reject",
                "reason_code": "plan_acceptance_consultant_unavailable",
                "rationale": f"plan semantic risk could not be independently reviewed: {exc}",
                "suggested_next_action": "Ask the user to confirm the intended concrete outcome before executing the plan.",
                "consultant_model": consultant,
            }

    def _plan_acceptance_gate(
        self,
        *,
        session_id: str,
        turn_id: str,
        queue_id: str,
        step_index: int,
        turn_workspace: Path,
        tool_name: str,
        user_message: str,
        tasks: list[dict[str, Any]],
        current_model: str,
    ) -> dict[str, Any]:
        issues = self._plan_semantic_risk_issues(user_message=user_message, tasks=tasks)
        if not issues:
            return {"ok": True, "issues": []}
        review = self._consult_plan_acceptance(
            user_message=user_message,
            tasks=tasks,
            current_model=current_model,
        )
        if str(review.get("verdict") or "") == "accept":
            self._append_session_event(
                session_id,
                {
                    "type": "system_note",
                    "role": "system",
                    "content": "Plan acceptance consultant accepted a semantic-risk task plan.",
                    "code": "plan_acceptance_review",
                    "reason_code": str(review.get("reason_code") or "plan_semantic_review_accepted"),
                    "details": {"issues": issues, "review": review},
                    "turn_id": turn_id,
                    "queue_id": queue_id,
                    "step_index": step_index,
                    "llm_workspace": str(turn_workspace),
                },
            )
            return {"ok": True, "issues": issues, "review": review}
        blocked = self._plan_acceptance_blocked_event(
            session_id=session_id,
            turn_id=turn_id,
            queue_id=queue_id,
            step_index=step_index,
            turn_workspace=turn_workspace,
            tool_name=tool_name,
            issues=issues,
            review=review,
            tasks=tasks,
        )
        return {"ok": False, "event": blocked, "issues": issues, "review": review}

    def _planning_profile_for_message(self, user_message: str) -> dict[str, Any]:
        return profile_problem(user_message)

    def _planning_is_required(self, *, user_message: str, recent_events: list[dict[str, Any]], steps: list[dict[str, Any]]) -> bool:
        del steps
        profile = self._planning_profile_for_message(user_message)
        if not planning_required_for_profile(profile):
            return False
        if self._latest_plan_record_for_current_request(
            user_message=user_message,
            recent_events=recent_events,
        ) is not None:
            return False
        frame = self.frame_manager.current_frame()
        if frame is not None and (frame.working_memory.child_tasks or frame.working_memory.completed_child_tasks):
            return False
        return True

    def _latest_plan_record(self, *, recent_events: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
        if recent_events is None:
            events = read_jsonl(self.paths.session_events_path(active_session_id(self.root)))
        else:
            events = list(recent_events)
        for event in reversed(events):
            if str(event.get("type") or "") != "plan_record":
                continue
            plan = event.get("plan") if isinstance(event.get("plan"), dict) else event.get("details")
            if isinstance(plan, dict):
                return dict(plan)
        return None

    def _latest_plan_record_for_current_request(
        self,
        *,
        user_message: str,
        recent_events: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        """Return a PlanRecord only if it was accepted after the current user message.

        Plan records are execution contracts for a specific turn. A previous
        interrupted dashboard experiment must not make a new complex request
        skip PLANNING_REQUIRED and start writing implementation code.
        """

        events = list(recent_events or [])
        if not events:
            events = read_jsonl(self.paths.session_events_path(active_session_id(self.root)), limit=5000)

        latest_user_index = -1
        expected = str(user_message or "")
        for index, event in enumerate(events):
            if str(event.get("type") or "") != "user_message":
                continue
            if str(event.get("content") or "") == expected:
                latest_user_index = index

        if latest_user_index < 0 and recent_events:
            all_events = read_jsonl(self.paths.session_events_path(active_session_id(self.root)), limit=5000)
            for index, event in enumerate(all_events):
                if str(event.get("type") or "") != "user_message":
                    continue
                if str(event.get("content") or "") == expected:
                    latest_user_index = index
            if latest_user_index >= 0:
                return self._latest_plan_record(recent_events=all_events[latest_user_index + 1 :])

        search_events = events[latest_user_index + 1 :] if latest_user_index >= 0 else events
        return self._latest_plan_record(recent_events=search_events)

    def _plan_record_execution_context(self, *, recent_events: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Return prompt-visible obligations derived from the accepted PlanRecord."""

        plan = self._latest_plan_record(recent_events=recent_events)
        if not plan:
            return {}
        profile = plan.get("profile") if isinstance(plan.get("profile"), dict) else {}
        strategy = str(plan.get("strategy") or profile.get("strategy") or "").strip()
        raw_contract = plan.get("verification_contract")
        verification_contract: list[str] = []
        if isinstance(raw_contract, dict):
            verification_contract = [
                str(key)
                for key, value in raw_contract.items()
                if value is not None and str(key).strip()
            ]
        elif isinstance(raw_contract, list):
            verification_contract = [str(item) for item in raw_contract if str(item).strip()]
        work_units_summary: list[dict[str, str]] = []
        for unit in plan.get("work_units") or []:
            if not isinstance(unit, dict):
                continue
            work_units_summary.append(
                {
                    "unit_id": str(unit.get("unit_id") or ""),
                    "work_type": str(unit.get("work_type") or ""),
                    "goal": str(unit.get("goal") or ""),
                    "success_evidence": str(unit.get("success_evidence") or ""),
                }
            )
        obligation_keys = {item.lower() for item in verification_contract}
        obligations: list[str] = []
        if strategy == "state_space_search" or {"legal_move_validator", "final_verifier"} & obligation_keys:
            obligations.extend(
                [
                    "state_space_search implementation must expose a programmatic solver/search path that returns an action sequence; a manual input loop alone does not satisfy the plan.",
                    "final_verifier/tests must replay each returned action from the start state through a legal-move validator and assert the goal state.",
                    "tests should include at least one illegal-move or impossible-state rejection when those concepts are part of the requested state model.",
                ]
            )
        if strategy == "dynamic_programming" or {"base_case_verifier", "recurrence_verifier"} & obligation_keys:
            obligations.extend(
                [
                    "dynamic_programming implementation must expose a programmatic solve/compute callable that evaluates subproblem states.",
                    "tests must separately verify base cases and recurrence/sample oracle cases; a single happy-path assertion is not enough.",
                    "implementation evidence must show a base case plus recurrence, memoization, or table/state transition update.",
                ]
            )
        if strategy == "constraint_satisfaction" or {"constraint_checker", "solution_validator"} & obligation_keys:
            obligations.extend(
                [
                    "constraint_satisfaction implementation must model variables, domains, constraints, and assignments explicitly or through inputs.",
                    "implementation must expose a constraint checker and a solution validator for the solver result.",
                    "tests must validate a satisfying assignment and reject an invalid or negative assignment.",
                ]
            )
        return {
            "plan_id": str(plan.get("plan_id") or ""),
            "plan_strategy": strategy,
            "plan_verification_contract": verification_contract,
            "plan_work_units_summary": work_units_summary[:6],
            "plan_execution_obligations": obligations,
        }

    def _planner_action_blocked_event(
        self,
        *,
        session_id: str,
        turn_id: str,
        queue_id: str,
        step_index: int,
        turn_workspace: Path,
        tool_name: str,
        user_message: str,
        current_phase: str,
        recent_events: list[dict[str, Any]],
        steps: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        if current_phase not in {"PLANNING_REQUIRED", "PLAN_REVISION"}:
            return None
        if tool_name == "create_plan":
            return None
        profile = self._planning_profile_for_message(user_message)
        reason_code = "plan_revision_required" if current_phase == "PLAN_REVISION" else "planning_required_before_execution"
        if current_phase == "PLAN_REVISION" and not plan_requires_revision(recent_events, steps):
            reason_code = "planning_required_before_execution"
        revision_reasons = plan_revision_reasons(recent_events=recent_events, steps=steps) if current_phase == "PLAN_REVISION" else []
        if current_phase == "PLAN_REVISION":
            self._append_session_event(
                session_id,
                {
                    "type": "plan_revision",
                    "role": "system",
                    "content": "Plan revision is required before more execution.",
                    "profile": profile,
                    "details": {
                        "failure_type": reason_code,
                        "revision_reasons": revision_reasons,
                        "blocked_tool": tool_name,
                        "allowed_next_actions": ["create_plan"],
                        "next_required_action": "create_plan with a revised PlanRecord",
                    },
                    "turn_id": turn_id,
                    "queue_id": queue_id,
                    "step_index": step_index,
                    "llm_workspace": str(turn_workspace),
                },
            )
        message = (
            f"{tool_name} was blocked because the runtime requires a PlanRecord before execution."
            if current_phase == "PLANNING_REQUIRED"
            else f"{tool_name} was blocked because the previous plan requires revision before more execution."
        )
        return self._append_session_event(
            session_id,
            {
                "type": "system_note",
                "role": "system",
                "content": message,
                "code": "planner_action_blocked",
                "reason_code": reason_code,
                "details": {
                    "blocked_tool": tool_name,
                    "failure_type": reason_code,
                    "blocked_by": "planner_controller",
                    "phase": current_phase,
                    "problem_profile": profile,
                    "allowed_next_actions": ["create_plan"],
                    "suggested_fix": "Create a PlanRecord with profile, strategy, work_units, verification_contract, status, and revision_count before executing tools.",
                    "next_required_action": "create_plan with a valid PlanRecord",
                },
                "turn_id": turn_id,
                "queue_id": queue_id,
                "step_index": step_index,
                "llm_workspace": str(turn_workspace),
            },
        )

    def _plan_scope_issues(self, *, plan: dict[str, Any], user_message: str) -> list[str]:
        text = str(user_message or "").lower()
        work_units = [unit for unit in plan.get("work_units") or [] if isinstance(unit, dict)]
        combined_units = "\n".join(
            " ".join(
                str(unit.get(key) or "")
                for key in ("goal", "work_type", "success_evidence", "done_when", "context_summary")
            ).lower()
            for unit in work_units
        )
        implementation_markers = (
            "implement",
            "create",
            "write",
            "build",
            "program",
            "script",
            "code",
            "実装",
            "作って",
            "作る",
            "作成",
            "プログラム",
            "コード",
        )
        run_markers = (
            "run",
            "execute",
            "test",
            "verify",
            "unittest",
            "実行",
            "動か",
            "テスト",
            "検証",
            "確認",
        )
        issues: list[str] = []
        if any(marker in text for marker in implementation_markers):
            if not any(str(unit.get("work_type") or "") == "edit" for unit in work_units):
                issues.append(
                    "plan_scope_incomplete: implementation request requires at least one work_type=edit WorkUnit; "
                    "the PlanRecord may start that WorkUnit with list_files/read_file/search_code, but the edit itself happens in PLAN_EXECUTION"
                )
        if any(marker in text for marker in run_markers):
            has_run_unit = any(str(unit.get("work_type") or "") == "run_test" for unit in work_units)
            has_run_goal = any(marker in combined_units for marker in ("run", "execute", "test", "verify", "実行", "テスト", "検証", "確認"))
            if not has_run_unit and not has_run_goal:
                issues.append(
                    "plan_scope_incomplete: execution or verification request requires a run_test/verification WorkUnit "
                    "that depends on implementation evidence"
                )
        return issues

    def _plan_record_embedded_edit_issues(self, plan: dict[str, Any]) -> list[str]:
        issues: list[str] = []
        edit_tools = {"write_file", "append_file", "replace_text"}
        for index, raw_unit in enumerate(plan.get("work_units") or [], start=1):
            if not isinstance(raw_unit, dict):
                continue
            first_action = raw_unit.get("first_action") if isinstance(raw_unit.get("first_action"), dict) else {}
            tool = str(first_action.get("tool") or "").strip()
            if tool in edit_tools:
                args = first_action.get("args") if isinstance(first_action.get("args"), dict) else {}
                issues.append(
                    "plan_work_unit_embedded_edit: "
                    f"work_units[{index}].first_action.tool={tool} path={str(args.get('path') or '')!r}; "
                    "create_plan must not contain implementation edits or code bodies. "
                    "Use a small observational first_action such as list_files/read_file/search_code; "
                    "the actual write_file/append_file/replace_text must happen later inside PLAN_EXECUTION."
                )
        return issues

    def _minimal_state_space_implementation_work_units(self) -> list[dict[str, Any]]:
        return [
            {
                "unit_id": "inspect-workspace",
                "goal": "Inspect the workspace before choosing artifact paths",
                "depends_on": [],
                "work_type": "inspect",
                "first_action": {"tool": "list_files", "args": {"path": "."}},
                "success_evidence": "workspace files are listed",
                "should_open_child_frame": True,
            },
            {
                "unit_id": "implement-artifact",
                "goal": "Implement the requested Python artifact",
                "depends_on": ["inspect-workspace"],
                "work_type": "edit",
                "first_action": {"tool": "list_files", "args": {"path": "."}},
                "success_evidence": "implementation artifact is written in PLAN_EXECUTION",
                "should_open_child_frame": True,
            },
            {
                "unit_id": "verify-legal-replay",
                "goal": "Run final_verifier that replays each legal action from the start state to the goal",
                "depends_on": ["implement-artifact"],
                "work_type": "run_test",
                "first_action": {
                    "tool": "run_command",
                    "args": {"command": "python3 -m unittest discover -s tests"},
                },
                "success_evidence": "final_verifier replays only legal actions and reaches the goal; illegal moves are rejected",
                "should_open_child_frame": True,
            },
        ]

    def _minimal_plan_record_for_profile(self, *, user_message: str) -> dict[str, Any]:
        profile = self._planning_profile_for_message(user_message)
        strategy = str(profile.get("strategy") or "task_decomposition")
        if strategy == "state_space_search":
            work_units = self._minimal_state_space_implementation_work_units()
            verification_contract: list[str] = list(STATE_SPACE_REQUIRED_CONTRACT)
        elif strategy == "dynamic_programming":
            work_units = [
                {
                    "unit_id": "inspect-workspace",
                    "goal": "Inspect the workspace before choosing artifact paths",
                    "depends_on": [],
                    "work_type": "inspect",
                    "first_action": {"tool": "list_files", "args": {"path": "."}},
                    "success_evidence": "workspace files are listed",
                    "should_open_child_frame": True,
                },
                {
                    "unit_id": "implement-artifact",
                    "goal": "Implement the requested Python artifact with explicit subproblem state, base cases, and recurrence or memoization",
                    "depends_on": ["inspect-workspace"],
                    "work_type": "edit",
                    "first_action": {"tool": "list_files", "args": {"path": "."}},
                    "success_evidence": "implementation artifact is written in PLAN_EXECUTION and exposes a solve/compute callable",
                    "should_open_child_frame": True,
                },
                {
                    "unit_id": "verify-dynamic-programming-contract",
                    "goal": "Run tests that verify base case behavior and recurrence/sample oracle behavior",
                    "depends_on": ["implement-artifact"],
                    "work_type": "run_test",
                    "first_action": {
                        "tool": "run_command",
                        "args": {"command": "python3 -m unittest discover -s tests"},
                    },
                    "success_evidence": "base case assertions and recurrence/sample oracle assertions pass",
                    "should_open_child_frame": True,
                },
            ]
            verification_contract = list(DYNAMIC_PROGRAMMING_REQUIRED_CONTRACT)
        elif strategy == "constraint_satisfaction":
            work_units = [
                {
                    "unit_id": "inspect-workspace",
                    "goal": "Inspect the workspace before choosing artifact paths",
                    "depends_on": [],
                    "work_type": "inspect",
                    "first_action": {"tool": "list_files", "args": {"path": "."}},
                    "success_evidence": "workspace files are listed",
                    "should_open_child_frame": True,
                },
                {
                    "unit_id": "implement-artifact",
                    "goal": "Implement the requested Python artifact with variables, domains, constraints, assignment search, and validation",
                    "depends_on": ["inspect-workspace"],
                    "work_type": "edit",
                    "first_action": {"tool": "list_files", "args": {"path": "."}},
                    "success_evidence": "implementation artifact is written in PLAN_EXECUTION and exposes solver plus constraint checker",
                    "should_open_child_frame": True,
                },
                {
                    "unit_id": "verify-constraint-satisfaction-contract",
                    "goal": "Run tests that validate constraint satisfaction and reject invalid negative assignments",
                    "depends_on": ["implement-artifact"],
                    "work_type": "run_test",
                    "first_action": {
                        "tool": "run_command",
                        "args": {"command": "python3 -m unittest discover -s tests"},
                    },
                    "success_evidence": "constraint validator accepts a satisfying assignment and rejects an invalid negative assignment",
                    "should_open_child_frame": True,
                },
            ]
            verification_contract = list(CONSTRAINT_SATISFACTION_REQUIRED_CONTRACT)
        else:
            work_units = [
                {
                    "unit_id": "inspect-workspace",
                    "goal": "Inspect the workspace before choosing artifact paths",
                    "depends_on": [],
                    "work_type": "inspect",
                    "first_action": {"tool": "list_files", "args": {"path": "."}},
                    "success_evidence": "workspace files are listed",
                    "should_open_child_frame": True,
                },
                {
                    "unit_id": "implement-artifact",
                    "goal": "Implement the requested Python artifact",
                    "depends_on": ["inspect-workspace"],
                    "work_type": "edit",
                    "first_action": {"tool": "list_files", "args": {"path": "."}},
                    "success_evidence": "implementation artifact is written in PLAN_EXECUTION",
                    "should_open_child_frame": True,
                },
                {
                    "unit_id": "run-verification",
                    "goal": "Run the generated tests or verifier",
                    "depends_on": ["implement-artifact"],
                    "work_type": "run_test",
                    "first_action": {
                        "tool": "run_command",
                        "args": {"command": "python3 -m unittest discover -s tests"},
                    },
                    "success_evidence": "verification command exits successfully",
                    "should_open_child_frame": True,
                },
            ]
            verification_contract = ["implementation_artifact", "tests_or_verifier", "unittest_or_verification_passed"]
        return {
            "plan_id": f"runtime-repaired-plan-{uuid.uuid4().hex[:8]}",
            "profile": profile,
            "strategy": strategy,
            "work_units": work_units,
            "verification_contract": verification_contract,
            "status": "proposed",
            "revision_count": 0,
        }

    def _recent_planner_block_count(self, *, session_id: str, reason_codes: set[str], limit: int = 5000) -> int:
        events = read_jsonl(self.paths.session_events_path(session_id), limit=limit)
        count = 0
        for event in reversed(events):
            if str(event.get("type") or "") != "system_note":
                continue
            if str(event.get("code") or "") != "planner_action_blocked":
                continue
            if str(event.get("reason_code") or "") in reason_codes:
                count += 1
        return count

    def _recent_llm_output_issue_count(self, *, session_id: str, reason_codes: set[str], limit: int = 5000) -> int:
        events = read_jsonl(self.paths.session_events_path(session_id), limit=limit)
        count = 0
        for event in reversed(events):
            event_type = str(event.get("type") or "")
            if event_type == "plan_record":
                break
            if event_type != "system_note":
                continue
            if str(event.get("code") or "") != "llm_output_issue":
                continue
            if str(event.get("reason_code") or "") in reason_codes:
                count += 1
        return count

    def _auto_create_minimal_plan_after_repeated_stream_issue(
        self,
        *,
        session_id: str,
        turn_id: str,
        queue_id: str,
        step_index: int,
        turn_workspace: Path,
        user_message: str,
        current_phase: str,
        steps: list[dict[str, Any]],
        current_model: str,
    ) -> dict[str, Any] | None:
        if current_phase not in {"PLANNING_REQUIRED", "PLAN_REVISION"}:
            return None
        repeated_count = self._recent_llm_output_issue_count(
            session_id=session_id,
            reason_codes={"plan_record_embedded_edit_stream"},
        )
        if repeated_count < 3:
            return None
        repaired_plan = self._minimal_plan_record_for_profile(user_message=user_message)
        self._append_session_event(
            session_id,
            {
                "type": "system_note",
                "role": "system",
                "content": (
                    "PlanRecord stream failure repeated. Runtime is using a minimal generic PlanRecord "
                    "derived from the problem profile so execution can move to PLAN_EXECUTION without embedding code in create_plan."
                ),
                "code": "plan_record_autorepaired",
                "reason_code": "repeated_plan_record_embedded_edit_stream",
                "details": {
                    "blocked_by": "planner_stream_guard",
                    "repair_source": "problem_profile",
                    "repeated_issue_count": repeated_count,
                    "repaired_plan": repaired_plan,
                },
                "turn_id": turn_id,
                "queue_id": queue_id,
                "step_index": step_index,
                "llm_workspace": str(turn_workspace),
            },
        )
        return self._handle_create_plan(
            session_id=session_id,
            turn_id=turn_id,
            queue_id=queue_id,
            step_index=step_index,
            tool_args={"plan": repaired_plan},
            turn_workspace=turn_workspace,
            steps=steps,
            user_message=user_message,
            current_model=current_model,
        )

    def _plan_revision_signature(self, plan: dict[str, Any]) -> str:
        normalized_units: list[dict[str, Any]] = []
        for unit in plan.get("work_units") or []:
            if not isinstance(unit, dict):
                continue
            first_action = unit.get("first_action") if isinstance(unit.get("first_action"), dict) else {}
            normalized_units.append(
                {
                    "unit_id": str(unit.get("unit_id") or ""),
                    "work_type": str(unit.get("work_type") or ""),
                    "goal": str(unit.get("goal") or ""),
                    "success_evidence": str(unit.get("success_evidence") or ""),
                    "first_action": {
                        "tool": str(first_action.get("tool") or ""),
                        "args": first_action.get("args") if isinstance(first_action.get("args"), dict) else {},
                    },
                }
            )
        payload = {
            "strategy": str(plan.get("strategy") or ""),
            "work_units": normalized_units,
            "verification_contract": plan.get("verification_contract") or {},
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _latest_accepted_plan_record(self, session_id: str) -> dict[str, Any] | None:
        for event in reversed(read_jsonl(self.paths.session_events_path(session_id))):
            if event.get("type") != "plan_record":
                continue
            plan = event.get("plan")
            if isinstance(plan, dict):
                return dict(plan)
        return None

    def _plan_revision_contract_issues(
        self,
        *,
        session_id: str,
        plan: dict[str, Any],
        steps: list[dict[str, Any]] | None,
    ) -> list[str]:
        recent_events = read_jsonl(self.paths.session_events_path(session_id), limit=200)
        revision_reasons = plan_revision_reasons(recent_events=recent_events, steps=list(steps or []))
        if not revision_reasons:
            return []
        previous_plan = self._latest_accepted_plan_record(session_id)
        if not previous_plan:
            return []
        if self._plan_revision_signature(previous_plan) == self._plan_revision_signature(plan):
            return [
                "plan_revision_no_change: previous PlanRecord execution failed with "
                + ", ".join(revision_reasons[:4])
                + ", but the revised PlanRecord has the same strategy, work_units, and verification_contract. "
                "A revised plan must materially change the next edit/verification path before rerunning validation."
            ]
        return []

    def _return_plan_revision_to_root_frame(
        self,
        *,
        session_id: str,
        turn_id: str,
        queue_id: str,
        step_index: int,
        turn_workspace: Path,
    ) -> int:
        returned = 0
        while True:
            current = self.frame_manager.current_frame()
            if current is None or current.parent_frame_id is None:
                break
            result = self._handle_return_to_parent(
                session_id=session_id,
                turn_id=turn_id,
                queue_id=queue_id,
                step_index=step_index,
                tool_args={
                    "summary": "Plan revision superseded this WorkUnit; returning to root before installing the revised PlanRecord.",
                    "findings": [
                        "Active child WorkUnit was abandoned because the runtime entered PLAN_REVISION.",
                        "The next PlanRecord is root-scoped and replaces the old WorkUnit tree.",
                    ],
                },
                turn_workspace=turn_workspace,
            )
            if not bool(result.get("ok")):
                break
            returned += 1
        if returned:
            self._append_session_event(
                session_id,
                {
                    "type": "system_note",
                    "role": "system",
                    "content": f"Returned {returned} active child frame(s) before accepting a revised PlanRecord.",
                    "code": "plan_revision_returned_to_root",
                    "reason_code": "plan_record_is_root_scoped",
                    "details": {
                        "returned_frame_count": returned,
                        "blocked_by": "planner_controller",
                        "next_required_action": "install revised PlanRecord at the root frame",
                    },
                    "turn_id": turn_id,
                    "queue_id": queue_id,
                    "step_index": step_index,
                    "llm_workspace": str(turn_workspace),
                },
            )
        return returned

    def _handle_create_plan(
        self,
        *,
        session_id: str,
        turn_id: str,
        queue_id: str,
        step_index: int,
        tool_args: dict[str, Any],
        turn_workspace: Path,
        steps: list[dict[str, Any]] | None = None,
        user_message: str = "",
        current_model: str = "",
    ) -> dict[str, Any]:
        raw_plan = tool_args.get("plan") if isinstance(tool_args.get("plan"), dict) else tool_args
        plan = dict(raw_plan or {})
        expected_profile = self._planning_profile_for_message(user_message)
        if not isinstance(plan.get("profile"), dict):
            plan["profile"] = expected_profile
        if not str(plan.get("plan_id") or "").strip():
            plan["plan_id"] = f"plan-{uuid.uuid4().hex[:12]}"
        if not str(plan.get("strategy") or "").strip():
            plan["strategy"] = str(plan.get("profile", {}).get("strategy") or expected_profile.get("strategy") or "task_decomposition")
        if "revision_count" not in plan:
            plan["revision_count"] = 0
        if not str(plan.get("status") or "").strip():
            plan["status"] = "proposed"
        embedded_edit_issues = self._plan_record_embedded_edit_issues(plan)
        schema_validation = validate_json_schema(plan, PLAN_RECORD_SCHEMA)
        contract_issues = validate_plan_record_contract(plan)
        scope_issues = []
        if schema_validation.ok and not embedded_edit_issues and not contract_issues:
            scope_issues = self._plan_scope_issues(plan=plan, user_message=user_message)
        revision_issues = []
        if schema_validation.ok and not embedded_edit_issues and not contract_issues and not scope_issues:
            revision_issues = self._plan_revision_contract_issues(
                session_id=session_id,
                plan=plan,
                steps=steps,
            )
        repair_reason_codes = {
            "plan_scope_incomplete",
            "state_space_search_plan_missing_execution_verifier",
            "dynamic_programming_plan_missing_execution_verifier",
            "constraint_satisfaction_plan_missing_execution_verifier",
        }
        previous_repairable_planner_blocks = self._recent_planner_block_count(
            session_id=session_id,
            reason_codes=repair_reason_codes,
        )
        repairable_issues = [*contract_issues, *scope_issues, *revision_issues]
        if (
            schema_validation.ok
            and not embedded_edit_issues
            and previous_repairable_planner_blocks >= 1
            and any(
                "plan_scope_incomplete" in issue
                or "state_space_search_plan_missing_execution_verifier" in issue
                or "dynamic_programming_plan_missing_execution_verifier" in issue
                or "constraint_satisfaction_plan_missing_execution_verifier" in issue
                for issue in repairable_issues
            )
        ):
            repaired_plan = self._minimal_plan_record_for_profile(user_message=user_message)
            repaired_plan["plan_id"] = str(plan.get("plan_id") or repaired_plan.get("plan_id") or "")
            repaired_plan["revision_count"] = int(plan.get("revision_count") or 0)
            repaired_schema_validation = validate_json_schema(repaired_plan, PLAN_RECORD_SCHEMA)
            repaired_contract_issues = validate_plan_record_contract(repaired_plan)
            repaired_scope_issues = (
                self._plan_scope_issues(plan=repaired_plan, user_message=user_message)
                if repaired_schema_validation.ok and not repaired_contract_issues
                else []
            )
            if repaired_schema_validation.ok and not repaired_contract_issues and not repaired_scope_issues:
                plan = repaired_plan
                schema_validation = repaired_schema_validation
                contract_issues = []
                scope_issues = []
                self._append_session_event(
                    session_id,
                    {
                        "type": "system_note",
                        "role": "system",
                        "content": (
                            "PlanRecord was repaired by the runtime after repeated planner contract failures: "
                            "using minimal inspect -> edit -> run_test WorkUnits derived from the problem profile."
                        ),
                        "code": "plan_record_autorepaired",
                        "reason_code": "repeated_planner_contract_failure",
                        "details": {
                            "original_plan": raw_plan,
                            "repaired_plan": repaired_plan,
                            "blocked_by": "planner_contract",
                            "repair_source": "problem_profile",
                        },
                        "turn_id": turn_id,
                        "queue_id": queue_id,
                        "step_index": step_index,
                        "llm_workspace": str(turn_workspace),
                    },
                )
        if not schema_validation.ok or embedded_edit_issues or contract_issues or scope_issues or revision_issues:
            issues = [*schema_validation.errors, *embedded_edit_issues, *contract_issues, *scope_issues, *revision_issues]
            failure_type = (
                "state_space_search_plan_missing_verifier"
                if any("state_space_search_plan_missing_verifier" in issue for issue in issues)
                else "state_space_search_plan_missing_execution_verifier"
                if any("state_space_search_plan_missing_execution_verifier" in issue for issue in issues)
                else "dynamic_programming_plan_missing_verifier"
                if any("dynamic_programming_plan_missing_verifier" in issue for issue in issues)
                else "dynamic_programming_plan_missing_execution_verifier"
                if any("dynamic_programming_plan_missing_execution_verifier" in issue for issue in issues)
                else "constraint_satisfaction_plan_missing_verifier"
                if any("constraint_satisfaction_plan_missing_verifier" in issue for issue in issues)
                else "constraint_satisfaction_plan_missing_execution_verifier"
                if any("constraint_satisfaction_plan_missing_execution_verifier" in issue for issue in issues)
                else "plan_revision_no_change"
                if any("plan_revision_no_change" in issue for issue in issues)
                else "plan_work_unit_embedded_edit"
                if any("plan_work_unit_embedded_edit" in issue for issue in issues)
                else "plan_work_unit_too_large"
                if any("first_action" in issue and ("content" in issue or "new_text" in issue or "old_text" in issue) and ("too long" in issue or "max" in issue.lower()) for issue in issues)
                else "planner_strategy_mismatch"
                if any("planner_strategy_mismatch" in issue for issue in issues)
                else "plan_scope_incomplete"
                if any("plan_scope_incomplete" in issue for issue in issues)
                else "plan_record_invalid"
            )
            implementation_plan_repair_units = (
                "[{\"unit_id\":\"inspect-workspace\",\"goal\":\"Inspect the workspace before choosing artifact paths\","
                "\"depends_on\":[],\"work_type\":\"inspect\",\"first_action\":{\"tool\":\"list_files\",\"args\":{\"path\":\".\"}},"
                "\"success_evidence\":\"workspace files are listed\",\"should_open_child_frame\":true},"
                "{\"unit_id\":\"implement-artifact\",\"goal\":\"Implement the requested Python artifact\","
                "\"depends_on\":[\"inspect-workspace\"],\"work_type\":\"edit\","
                "\"first_action\":{\"tool\":\"list_files\",\"args\":{\"path\":\".\"}},"
                "\"success_evidence\":\"implementation artifact is written in PLAN_EXECUTION\","
                "\"should_open_child_frame\":true},"
                "{\"unit_id\":\"verify-legal-replay\",\"goal\":\"Run final_verifier that replays each legal action from the start state to the goal\","
                "\"depends_on\":[\"implement-artifact\"],\"work_type\":\"run_test\","
                "\"first_action\":{\"tool\":\"run_command\",\"args\":{\"command\":\"python3 -m unittest discover -s tests\"}},"
                "\"success_evidence\":\"final_verifier replays only legal actions and reaches the goal; illegal moves are rejected\","
                "\"should_open_child_frame\":true}]"
            )
            suggested_fix = (
                (
                    "Repair the PlanRecord scope. Implementation requests need work_type=edit WorkUnits; "
                    "execution/verification requests need run_test or verification WorkUnits. "
                    "Replace plan.work_units with this minimal executable sequence: "
                    f"{implementation_plan_repair_units}. "
                )
                if failure_type == "plan_scope_incomplete"
                else (
                    "State-space implementation plans must include both an edit WorkUnit and this executable verifier WorkUnit. "
                    "Replace plan.work_units with this minimal executable sequence: "
                    f"{implementation_plan_repair_units}. "
                    "Do not write vague success_evidence like 'tests pass' or 'works correctly'. "
                )
                if failure_type == "state_space_search_plan_missing_execution_verifier"
                else (
                    "Dynamic-programming implementation plans must include a run_test WorkUnit whose goal/success_evidence names base case verification and recurrence/sample oracle verification. "
                    "Do not write vague success_evidence like 'tests pass' or 'works correctly'. "
                )
                if failure_type == "dynamic_programming_plan_missing_execution_verifier"
                else (
                    "Constraint-satisfaction implementation plans must include a run_test WorkUnit whose goal/success_evidence names constraint validation and invalid/negative assignment rejection. "
                    "Do not write vague success_evidence like 'tests pass' or 'works correctly'. "
                )
                if failure_type == "constraint_satisfaction_plan_missing_execution_verifier"
                else (
                    "Do not resubmit the same PlanRecord after a timeout or repeated failure. "
                    "Change the WorkUnits so the next edit narrows the failing fixture or implementation scope before validation reruns. "
                )
                if failure_type == "plan_revision_no_change"
                else (
                    "Remove write_file/append_file/replace_text from PlanRecord.first_action. "
                    "Planning may choose only a small observation first_action such as "
                    "{\"tool\":\"list_files\",\"args\":{\"path\":\".\"}}. "
                    "Emit the actual implementation code later in PLAN_EXECUTION after the WorkUnit opens. "
                )
                if failure_type == "plan_work_unit_embedded_edit"
                else "Repair the PlanRecord schema exactly. "
            )
            suggested_fix += PLAN_RECORD_SHAPE_HINT
            content_prefix = (
                "create_plan was blocked: state_space_search needs an executable verifier WorkUnit. "
                + suggested_fix
                if failure_type == "state_space_search_plan_missing_execution_verifier"
                else "create_plan was blocked: dynamic_programming needs an executable verifier WorkUnit. "
                + suggested_fix
                if failure_type == "dynamic_programming_plan_missing_execution_verifier"
                else "create_plan was blocked: constraint_satisfaction needs an executable verifier WorkUnit. "
                + suggested_fix
                if failure_type == "constraint_satisfaction_plan_missing_execution_verifier"
                else "create_plan was blocked because PlanRecord is invalid: "
            )
            note = self._append_session_event(
                session_id,
                {
                    "type": "system_note",
                    "role": "system",
                    "content": (
                        content_prefix
                        + "; ".join(issues)
                        + " "
                        + PLAN_RECORD_SHAPE_HINT
                    ),
                    "code": "planner_action_blocked",
                    "reason_code": failure_type,
                    "details": {
                        "blocked_tool": "create_plan",
                        "failure_type": failure_type,
                        "blocked_by": "planner_contract",
                        "issues": issues,
                        "plan": plan,
                        "problem_profile": expected_profile,
                        "allowed_next_actions": ["create_plan"],
                        "expected_shape": PLAN_RECORD_SHAPE_HINT,
                        "suggested_fix": suggested_fix,
                        "next_required_action": "retry create_plan with a valid PlanRecord",
                    },
                    "turn_id": turn_id,
                    "queue_id": queue_id,
                    "step_index": step_index,
                    "llm_workspace": str(turn_workspace),
                },
            )
            return {"ok": False, "event": note, "error": "invalid PlanRecord"}
        accepted_plan = {**plan, "status": "accepted"}
        profile = accepted_plan.get("profile") if isinstance(accepted_plan.get("profile"), dict) else expected_profile
        work_packages = plan_record_to_work_packages(accepted_plan)
        work_package_issues: list[str] = []
        for task in work_packages:
            issues = self._work_package_issues(self._normalize_work_package(task))
            first_action = task.get("first_action") if isinstance(task.get("first_action"), dict) else {}
            first_action_args = first_action.get("args") if isinstance(first_action.get("args"), dict) else {}
            first_action_tool = str(first_action.get("tool") or "").strip()
            if first_action_tool in {"write_file", "append_file", "replace_text"}:
                content = (
                    str(first_action_args.get("new_text") or "")
                    if first_action_tool == "replace_text"
                    else str(first_action_args.get("content") or "")
                )
                content_bytes = len(content.encode("utf-8"))
                if content_bytes > PLAN_FIRST_ACTION_CONTENT_BYTES:
                    issues.append(
                        "first_action.content too large for PlanRecord "
                        f"({content_bytes} bytes > {PLAN_FIRST_ACTION_CONTENT_BYTES}); "
                        "create_plan must contain a concise first executable step, not a full implementation dump"
                    )
                if first_action_tool == "write_file" and str(first_action_args.get("path") or "").replace("\\", "/").endswith(".py"):
                    try:
                        ast.parse(content or "\n")
                    except SyntaxError as exc:
                        issues.append(
                            "first_action.content for Python write_file must be syntactically valid "
                            f"(line {exc.lineno}, offset {exc.offset}: {exc.msg})"
                        )
            if issues:
                work_package_issues.append(f"{task.get('task_id')}: {', '.join(issues)}")
        if work_package_issues:
            failure_type = (
                "plan_work_unit_too_large"
                if any("too large for PlanRecord" in issue for issue in work_package_issues)
                else "plan_work_unit_invalid_python"
                if any("syntactically valid" in issue for issue in work_package_issues)
                else "plan_work_unit_placeholder"
                if any("placeholder" in issue or "pass-only" in issue for issue in work_package_issues)
                else "plan_work_units_invalid"
            )
            suggested_fix = (
                "Repair WorkUnits so every first_action is executable and contains no pass/TODO/placeholder implementation. "
                f"{PLAN_RECORD_SHAPE_HINT}"
            )
            note = self._append_session_event(
                session_id,
                {
                    "type": "system_note",
                    "role": "system",
                    "content": "create_plan was blocked because PlanRecord WorkUnits are not executable: " + "; ".join(work_package_issues),
                    "code": "planner_action_blocked",
                    "reason_code": failure_type,
                    "details": {
                        "blocked_tool": "create_plan",
                        "failure_type": failure_type,
                        "blocked_by": "planner_work_unit_contract",
                        "issues": work_package_issues,
                        "plan": accepted_plan,
                        "problem_profile": expected_profile,
                        "allowed_next_actions": ["create_plan"],
                        "expected_shape": PLAN_RECORD_SHAPE_HINT,
                        "suggested_fix": suggested_fix,
                        "next_required_action": "retry create_plan with executable WorkUnits",
                    },
                    "turn_id": turn_id,
                    "queue_id": queue_id,
                    "step_index": step_index,
                    "llm_workspace": str(turn_workspace),
                },
            )
            return {"ok": False, "event": note, "error": "invalid PlanRecord WorkUnits"}
        acceptance = self._plan_acceptance_gate(
            session_id=session_id,
            turn_id=turn_id,
            queue_id=queue_id,
            step_index=step_index,
            turn_workspace=turn_workspace,
            tool_name="create_plan",
            user_message=user_message,
            tasks=work_packages,
            current_model=current_model,
        )
        if not bool(acceptance.get("ok")):
            return {"ok": False, "event": acceptance.get("event"), "error": "plan semantic mismatch"}
        self._append_session_event(
            session_id,
            {
                "type": "problem_profile",
                "role": "system",
                "content": f"Problem profile selected strategy={profile.get('strategy') or accepted_plan.get('strategy')}",
                "profile": profile,
                "turn_id": turn_id,
                "queue_id": queue_id,
                "step_index": step_index,
                "llm_workspace": str(turn_workspace),
            },
        )
        self._append_session_event(
            session_id,
            {
                "type": "planner_decision",
                "role": "system",
                "content": f"Planner selected strategy={accepted_plan.get('strategy')}",
                "strategy": str(accepted_plan.get("strategy") or ""),
                "profile": profile,
                "verification_contract": accepted_plan.get("verification_contract"),
                "turn_id": turn_id,
                "queue_id": queue_id,
                "step_index": step_index,
                "llm_workspace": str(turn_workspace),
            },
        )
        self._append_session_event(
            session_id,
            {
                "type": "plan_record",
                "role": "system",
                "content": f"Accepted PlanRecord {accepted_plan.get('plan_id')} with {len(accepted_plan.get('work_units') or [])} work units.",
                "plan": accepted_plan,
                "profile": profile,
                "strategy": str(accepted_plan.get("strategy") or ""),
                "work_units": list(accepted_plan.get("work_units") or []),
                "verification_contract": accepted_plan.get("verification_contract"),
                "turn_id": turn_id,
                "queue_id": queue_id,
                "step_index": step_index,
                "llm_workspace": str(turn_workspace),
            },
        )
        self._return_plan_revision_to_root_frame(
            session_id=session_id,
            turn_id=turn_id,
            queue_id=queue_id,
            step_index=step_index,
            turn_workspace=turn_workspace,
        )
        decompose_result = self._handle_decompose_tasks(
            session_id=session_id,
            turn_id=turn_id,
            queue_id=queue_id,
            step_index=step_index,
            tool_args={
                "tasks": work_packages,
                "rationale": f"PlanRecord {accepted_plan.get('plan_id')} strategy={accepted_plan.get('strategy')}",
            },
            turn_workspace=turn_workspace,
            steps=steps,
            user_message=user_message,
            current_model=current_model,
            skip_acceptance=True,
        )
        return {
            "ok": bool(decompose_result.get("ok")),
            "plan": accepted_plan,
            "event": decompose_result.get("event"),
            "tasks": work_packages,
            "auto_steps": list(decompose_result.get("auto_steps") or []),
            "terminal_failure": bool(decompose_result.get("terminal_failure")),
            "error": decompose_result.get("error"),
        }

    def _child_frame_has_tool_evidence(self) -> bool:
        current = self.frame_manager.current_frame()
        if current is None or current.parent_frame_id is None:
            return False
        for event in current.session_events:
            if str(event.get("type") or "") == "tool_result":
                return True
        return False

    def _current_child_is_plan_work_unit(self) -> bool:
        current = self.frame_manager.current_frame()
        if current is None or current.parent_frame_id is None:
            return False
        work_package = self.frame_manager.work_package_for(current) or {}
        context_summary = str(work_package.get("context_summary") or current.inherited_context.get("context_summary") or "")
        why_not_direct = str(work_package.get("why_not_direct_action") or "")
        return "planner_strategy=" in context_summary or "PlanRecord requires this WorkUnit" in why_not_direct

    def _child_frame_successful_tool_evidence(self) -> list[str]:
        current = self.frame_manager.current_frame()
        if current is None or current.parent_frame_id is None:
            return []
        findings: list[str] = []
        for event in current.session_events:
            if event.get("type") != "tool_result" or not bool(event.get("ok")):
                continue
            tool_name = str(event.get("tool_name") or "")
            content = str(event.get("content") or "")
            try:
                result = json.loads(content) if content else {}
            except json.JSONDecodeError:
                result = {}
            if tool_name in {"write_file", "append_file", "replace_text"}:
                path = str(result.get("path") or "").strip()
                findings.append(f"{tool_name} ok: {path}" if path else f"{tool_name} ok")
                continue
            if tool_name == "read_file":
                path = str(result.get("path") or "").strip()
                findings.append(f"read_file ok: {path}" if path else "read_file ok")
                continue
            if tool_name == "run_command":
                command = str(result.get("command") or "").strip()
                stdout = str(result.get("stdout") or "")
                preview = stdout[:400].replace("\n", "\\n")
                findings.append(
                    f"run_command ok: {command}; stdout_preview={preview}"
                    if preview
                    else f"run_command ok: {command}"
                )
                continue
            findings.append(f"{tool_name} ok")
        return findings

    @staticmethod
    def _work_unit_contract_text(work_package: dict[str, Any]) -> str:
        parts: list[str] = []
        for key in ("goal", "success_evidence", "done_when", "context_summary", "why_not_direct_action"):
            value = work_package.get(key)
            if isinstance(value, list):
                parts.extend(str(item) for item in value)
            elif value:
                parts.append(str(value))
        return "\n".join(parts)

    @staticmethod
    def _work_unit_edit_text(tool_name: str, tool_args: dict[str, Any]) -> str:
        if tool_name == "replace_text":
            return str(tool_args.get("new_text") or "")
        return str(tool_args.get("content") or "")

    @staticmethod
    def _work_unit_required_algorithm_markers(contract_text: str) -> list[str]:
        lowered = str(contract_text or "").lower()
        markers: list[str] = []
        if re.search(r"\ba\s*[-*]\s*search\b|\ba\s*[-*]\b|\bastar\b|\ba_star\b", lowered):
            markers.append("astar")
        for marker in ("dijkstra", "bfs", "dfs"):
            if re.search(rf"\b{re.escape(marker)}\b", lowered):
                markers.append(marker)
        if "動的計画" in lowered or "dynamic programming" in lowered:
            markers.append("dynamic_programming")
        return list(dict.fromkeys(markers))

    @staticmethod
    def _source_satisfies_algorithm_marker(source: str, marker: str) -> bool:
        text = str(source or "")
        lowered = text.lower()
        if marker == "astar":
            if re.search(r"\b(astar|a_star)\b", lowered):
                return True
            return "heapq" in lowered and any(token in lowered for token in ("heuristic", "f_score", "priority"))
        if marker == "dynamic_programming":
            return any(token in lowered for token in ("dynamic_programming", "dp", "memo", "cache")) or "漸化式" in lowered
        return bool(re.search(rf"\b{re.escape(marker)}\b", lowered))

    @staticmethod
    def _work_unit_mentions_tests(contract_text: str) -> bool:
        lowered = str(contract_text or "").lower()
        return any(
            marker in lowered
            for marker in (
                "test",
                "tests/",
                "unittest",
                "assert",
                "テスト",
                "検証テスト",
                "fixture",
            )
        )

    @staticmethod
    def _work_unit_mentions_implementation(contract_text: str) -> bool:
        lowered = str(contract_text or "").lower()
        return any(
            marker in lowered
            for marker in (
                "implement",
                "implementation",
                "algorithm",
                "solver",
                "search",
                "function",
                "class",
                "module",
                "code",
                "実装",
                "アルゴリズム",
                "ソルバー",
                "探索",
                "解法",
                "関数",
                "クラス",
                "コード",
                "プログラム",
            )
        )

    @staticmethod
    def _work_unit_output_literals(text: str) -> list[str]:
        literals: list[str] = []
        for match in re.finditer(r"'([^'\n]{1,80})'|\"([^\"\n]{1,80})\"", str(text or "")):
            literal = str(match.group(1) or match.group(2) or "").strip()
            if literal:
                literals.append(literal)
        return list(dict.fromkeys(literals))

    @staticmethod
    def _work_unit_declares_output_requirement(text: str) -> bool:
        lowered = str(text or "").lower()
        return any(
            marker in lowered
            for marker in (
                "stdout",
                "print",
                "prints",
                "output",
                "display",
                "show",
                "標準出力",
                "出力",
                "表示",
                "見せ",
            )
        )

    def _work_unit_success_evidence_issues(
        self,
        *,
        work_package: dict[str, Any],
        tool_name: str,
        tool_args: dict[str, Any],
        tool_result: dict[str, Any],
    ) -> list[str]:
        """Return generic mismatches between WorkUnit evidence and observed tool result."""

        if not bool(tool_result.get("ok")):
            return []
        work_type = str(work_package.get("work_type") or "").strip()
        contract_text = self._work_unit_contract_text(work_package)
        issues: list[str] = []
        if work_type == "edit" and tool_name in {"write_file", "append_file", "replace_text"}:
            path = str(tool_result.get("path") or tool_args.get("path") or "").strip()
            edit_text = self._work_unit_edit_text(tool_name, tool_args)
            mentions_tests = self._work_unit_mentions_tests(contract_text)
            mentions_impl = self._work_unit_mentions_implementation(contract_text)
            if _artifact_path_is_test(path) and mentions_impl and not mentions_tests:
                issues.append(
                    "WorkUnit goal/success_evidence asks for implementation evidence, "
                    f"but the successful edit wrote a test artifact: {path}"
                )
            if (
                tool_name in {"write_file", "append_file"}
                and edit_text.strip()
                and not _artifact_path_is_test(path)
            ):
                for marker in self._work_unit_required_algorithm_markers(contract_text):
                    if not self._source_satisfies_algorithm_marker(edit_text, marker):
                        issues.append(
                            f"WorkUnit requires {marker} evidence, but the edited implementation text does not contain an observable {marker} implementation marker"
                        )
            return issues
        if work_type == "run_test" and tool_name == "run_command":
            stdout = str(tool_result.get("stdout") or "")
            stderr = str(tool_result.get("stderr") or "")
            combined = stdout + "\n" + stderr
            if self._work_unit_declares_output_requirement(contract_text):
                literals = self._work_unit_output_literals(contract_text)
                missing_literals = [literal for literal in literals if literal not in combined]
                if literals and missing_literals:
                    issues.append(
                        "WorkUnit success_evidence requires output literal(s) that were not observed: "
                        + ", ".join(missing_literals)
                    )
                elif not stdout.strip():
                    issues.append(
                        "WorkUnit success_evidence requires user-visible stdout, but the successful command produced empty stdout"
                    )
            return issues
        return issues

    def _work_unit_success_evidence_block_event(
        self,
        *,
        session_id: str,
        turn_id: str,
        queue_id: str,
        step_index: int,
        turn_workspace: Path,
        tool_name: str,
        tool_args: dict[str, Any],
        tool_result: dict[str, Any],
        issues: list[str],
    ) -> dict[str, Any] | None:
        current = self.frame_manager.current_frame()
        if current is None or current.parent_frame_id is None:
            return None
        work_package = dict(self.frame_manager.work_package_for(current) or {})
        if not work_package:
            return None
        result_summary = {
            key: tool_result.get(key)
            for key in ("ok", "tool", "path", "command", "returncode", "stdout", "stderr", "error")
            if key in tool_result
        }
        if "stdout" in result_summary:
            result_summary["stdout"] = str(result_summary["stdout"] or "")[:800]
        if "stderr" in result_summary:
            result_summary["stderr"] = str(result_summary["stderr"] or "")[:800]
        message = (
            "WorkUnit の success_evidence がまだ観測されていないため、子フレーム完了をブロックしました。"
            " tool は成功しましたが、計画上の成功条件と実際の成果が一致していません。"
        )
        return self._append_session_event(
            session_id,
            {
                "type": "system_note",
                "role": "system",
                "content": message,
                "code": "work_unit_success_evidence_blocked",
                "reason_code": "work_unit_success_evidence_not_observed",
                "details": {
                    "active_frame_id": current.frame_id,
                    "active_frame_depth": current.depth,
                    "blocked_tool": "return_to_parent",
                    "failure_type": "work_unit_success_evidence_not_observed",
                    "blocked_by": "plan_execution_contract",
                    "work_package": work_package,
                    "tool_name": tool_name,
                    "tool_args": tool_args,
                    "tool_result_summary": result_summary,
                    "issues": issues,
                    "allowed_next_actions": [
                        {
                            "tool": "write_file|append_file|replace_text|run_command|read_file|search_code|list_files",
                            "strategy": "perform a direct action whose observed result satisfies this WorkUnit success_evidence",
                        },
                        {
                            "tool": "return_to_parent",
                            "strategy": "allowed only after the WorkUnit success_evidence is actually observed",
                        },
                    ],
                    "suggested_fix": (
                        "現在の WorkUnit goal と success_evidence を読み、成功済みtoolの種類ではなく、"
                        "観測されたpath/stdout/stderr/contentがその成功条件を満たすように直接編集または検証してください。"
                    ),
                    "next_required_action": "produce observed evidence matching the active WorkUnit success_evidence before returning to parent",
                },
                "turn_id": turn_id,
                "queue_id": queue_id,
                "step_index": step_index,
                "llm_workspace": str(turn_workspace),
            },
        )

    def _child_frame_has_unresolved_failure(self) -> bool:
        current = self.frame_manager.current_frame()
        if current is None or current.parent_frame_id is None:
            return False
        failure_codes = {"edit_blocked", "command_failed", "validation_failed", "work_unit_success_evidence_blocked"}
        for event in current.session_events:
            if event.get("type") == "tool_result" and not bool(event.get("ok")):
                return True
            if event.get("type") == "system_note" and str(event.get("code") or "") in failure_codes:
                return True
        return False

    def _child_finish_block_count(self) -> int:
        current = self.frame_manager.current_frame()
        if current is None or current.parent_frame_id is None:
            return 0
        count = 0
        for event in current.session_events:
            if (
                event.get("type") == "system_note"
                and event.get("code") == "finish_blocked"
                and event.get("reason_code") == "child_frame_must_return"
            ):
                count += 1
        return count

    def _child_first_action_blocked_event(
        self,
        *,
        session_id: str,
        turn_id: str,
        queue_id: str,
        step_index: int,
        turn_workspace: Path,
        requested_tool: str,
        requested_args: dict[str, Any],
    ) -> dict[str, Any] | None:
        current = self.frame_manager.current_frame()
        if current is None or current.parent_frame_id is None:
            return None
        if self._child_frame_has_tool_evidence():
            return None
        work_package = self.frame_manager.work_package_for(current) or {}
        first_action = work_package.get("first_action") or {}
        if not isinstance(first_action, dict):
            return None
        expected_tool = str(first_action.get("tool") or "").strip()
        expected_args = dict(first_action.get("args") or {}) if isinstance(first_action.get("args") or {}, dict) else {}
        if not expected_tool:
            return None
        if requested_tool == expected_tool and requested_args == expected_args:
            return None
        message = (
            "子フレームは最初の具体ツール結果を得る前に別の行動を選べません。"
            "現在の work_package.first_action をそのまま実行してください: "
            f"{json.dumps({'tool_name': expected_tool, 'tool_args': expected_args}, ensure_ascii=False)}"
        )
        return self._append_session_event(
            session_id,
            {
                "type": "system_note",
                "role": "system",
                "content": message,
                "code": "first_action_required",
                "reason_code": "child_contract_requires_first_action",
                "details": {
                    "active_frame_id": current.frame_id,
                    "active_frame_depth": current.depth,
                    "blocked_tool": requested_tool,
                    "failure_type": "frame_first_action_required",
                    "blocked_by": "frame_contract",
                    "requested_tool": requested_tool,
                    "requested_args": requested_args,
                    "expected_tool": expected_tool,
                    "expected_args": expected_args,
                    "work_package": work_package,
                    "allowed_next_actions": [
                        {"tool": expected_tool, "args": expected_args, "strategy": "execute work_package.first_action exactly"}
                    ],
                    "suggested_fix": "子フレームの最初の行動は work_package.first_action と完全一致させてください。",
                    "next_required_action": f"run {expected_tool} with expected_args",
                },
                "turn_id": turn_id,
                "queue_id": queue_id,
                "step_index": step_index,
                "llm_workspace": str(turn_workspace),
            },
        )

    def _child_contract_blocks_decomposition(
        self,
        *,
        session_id: str,
        turn_id: str,
        queue_id: str,
        step_index: int,
        turn_workspace: Path,
        tool_name: str,
    ) -> dict[str, Any] | None:
        current = self.frame_manager.current_frame()
        if current is None or current.parent_frame_id is None:
            return None
        is_plan_work_unit = self._current_child_is_plan_work_unit()
        has_tool_evidence = self._child_frame_has_tool_evidence()
        if not is_plan_work_unit and not has_tool_evidence:
            return None
        work_package = dict(self.frame_manager.work_package_for(current) or {})
        first_action = work_package.get("first_action") if isinstance(work_package.get("first_action"), dict) else {}
        if is_plan_work_unit:
            message = (
                "accepted PlanRecord の WorkUnit 子フレームでは再分解できません。"
                "PlanRecord自体が分解の正本なので、この子フレーム内で直接ツールを実行してください。"
            )
            reason_code = "plan_work_unit_requires_direct_action"
            failure_type = "plan_work_unit_decomposition_blocked"
            if not has_tool_evidence and first_action:
                allowed_next_actions = [
                    {
                        "tool": str(first_action.get("tool") or ""),
                        "args": first_action.get("args") or {},
                        "strategy": "execute work_package.first_action exactly",
                    }
                ]
                suggested_fix = "PlanRecord WorkUnit の最初の行動は work_package.first_action をそのまま実行してください。"
                next_required_action = f"run {first_action.get('tool')} with expected_args"
            else:
                allowed_next_actions = [
                    {
                        "tool": "read_file|search_code|run_command|write_file|append_file|replace_text|list_files",
                        "strategy": "perform one direct tool action that advances this WorkUnit",
                    },
                    {"tool": "return_to_parent", "strategy": "return only if the WorkUnit success_evidence is satisfied"},
                ]
                suggested_fix = (
                    "decompose_tasks/open_child_frame を使わず、現在の WorkUnit の未達を直接解消する "
                    "write_file/read_file/run_command などを1つ実行してください。"
                )
                next_required_action = "perform one direct child-frame tool action for this WorkUnit"
        else:
            message = (
                "子フレームは具体ツール結果を得た後に再分解できません。"
                "現在の work_package の証拠を return_to_parent で親へ返すか、同じ子フレーム内で直接ツールを実行してください。"
            )
            reason_code = "child_contract_requires_return"
            failure_type = "child_frame_decomposition_after_evidence"
            allowed_next_actions = [
                {"tool": "return_to_parent", "strategy": "return the child evidence to the parent frame"},
                {
                    "tool": "read_file|search_code|run_command|write_file|append_file|replace_text|list_files",
                    "strategy": "continue direct work inside this child frame only if more evidence is required",
                },
            ]
            suggested_fix = "子フレームで得た証拠を return_to_parent で親へ返すか、同じ子フレーム内で直接ツールを実行してください。"
            next_required_action = "return_to_parent with the child evidence, or perform one direct child-frame tool action"
        return self._append_session_event(
            session_id,
            {
                "type": "system_note",
                "role": "system",
                "content": message,
                "code": "decompose_tasks_blocked" if tool_name == "decompose_tasks" else "open_child_frame_blocked",
                "reason_code": reason_code,
                "details": {
                    "active_frame_id": current.frame_id,
                    "active_frame_depth": current.depth,
                    "blocked_tool": tool_name,
                    "failure_type": failure_type,
                    "blocked_by": "frame_contract",
                    "work_package": work_package,
                    "plan_work_unit": is_plan_work_unit,
                    "observations": list(current.working_memory.observations),
                    "allowed_next_actions": allowed_next_actions,
                    "suggested_fix": suggested_fix,
                    "next_required_action": next_required_action,
                },
                "turn_id": turn_id,
                "queue_id": queue_id,
                "step_index": step_index,
                "llm_workspace": str(turn_workspace),
            },
        )

    def _force_return_after_child_contract_block(
        self,
        *,
        session_id: str,
        turn_id: str,
        queue_id: str,
        step_index: int,
        turn_workspace: Path,
        blocked_tool: str,
    ) -> None:
        current = self.frame_manager.current_frame()
        if current is None or current.parent_frame_id is None:
            return
        findings = [
            *list(current.working_memory.observations),
            *self._child_frame_successful_tool_evidence(),
        ]
        self._handle_return_to_parent(
            session_id=session_id,
            turn_id=turn_id,
            queue_id=queue_id,
            step_index=step_index,
            tool_args={
                "summary": f"子フレームは {blocked_tool} を試みたため、観測済み証拠を親へ戻します。",
                "findings": findings,
            },
            turn_workspace=turn_workspace,
        )

    def _current_child_first_action_matches(self, *, tool_name: str, tool_args: dict[str, Any]) -> bool:
        current = self.frame_manager.current_frame()
        if current is None or current.parent_frame_id is None:
            return False
        if self._child_frame_has_tool_evidence():
            return False
        work_package = self.frame_manager.work_package_for(current) or {}
        first_action = work_package.get("first_action") or {}
        if not isinstance(first_action, dict):
            return False
        expected_tool = str(first_action.get("tool") or "").strip()
        expected_args = dict(first_action.get("args") or {}) if isinstance(first_action.get("args") or {}, dict) else {}
        return tool_name == expected_tool and dict(tool_args) == expected_args

    def _return_after_first_action_success(
        self,
        *,
        session_id: str,
        turn_id: str,
        queue_id: str,
        step_index: int,
        turn_workspace: Path,
        tool_name: str,
        tool_args: dict[str, Any],
        tool_result: dict[str, Any],
    ) -> bool:
        current = self.frame_manager.current_frame()
        if current is None or current.parent_frame_id is None:
            return False
        work_package = self.frame_manager.work_package_for(current) or {}
        issues = self._work_unit_success_evidence_issues(
            work_package=dict(work_package),
            tool_name=tool_name,
            tool_args=tool_args,
            tool_result=tool_result,
        )
        if issues:
            self._work_unit_success_evidence_block_event(
                session_id=session_id,
                turn_id=turn_id,
                queue_id=queue_id,
                step_index=step_index,
                turn_workspace=turn_workspace,
                tool_name=tool_name,
                tool_args=tool_args,
                tool_result=tool_result,
                issues=issues,
            )
            return False
        evidence = work_package.get("success_evidence") or work_package.get("done_when") or ""
        result_for_summary: dict[str, Any] = {}
        for key in (
            "ok",
            "tool",
            "path",
            "bytes_written",
            "size_policy",
            "returncode",
            "command",
            "duration_ms",
            "failure_type",
            "blocked_by",
            "error",
        ):
            if key in tool_result:
                result_for_summary[key] = tool_result.get(key)
        if "items" in tool_result and isinstance(tool_result.get("items"), list):
            result_for_summary["item_count"] = len(tool_result.get("items") or [])
        if "content" in tool_result:
            result_for_summary["content_summary"] = f"<omitted {len(str(tool_result.get('content') or ''))} chars>"
        if not result_for_summary:
            result_for_summary = {"ok": bool(tool_result.get("ok")), "tool": tool_name}
        result_summary = json.dumps(result_for_summary, ensure_ascii=False)[:700]
        self._handle_return_to_parent(
            session_id=session_id,
            turn_id=turn_id,
            queue_id=queue_id,
            step_index=step_index,
            tool_args={
                "summary": f"first_action succeeded: {tool_name}",
                "findings": [
                    f"declared_success_evidence: {evidence}",
                    f"tool_result: {result_summary}",
                    *list(current.working_memory.observations),
                ],
            },
            turn_workspace=turn_workspace,
        )
        return True

    def _current_child_should_return_after_tool_success(self, *, tool_name: str) -> bool:
        current = self.frame_manager.current_frame()
        if current is None or current.parent_frame_id is None:
            return False
        work_package = self.frame_manager.work_package_for(current) or {}
        work_type = str(work_package.get("work_type") or "").strip()
        if work_type == "edit":
            return tool_name in {"write_file", "append_file", "replace_text"}
        return work_type == "run_test" and tool_name == "run_command"

    def _step_limit_active_frame_block_event(
        self,
        *,
        session_id: str,
        turn_id: str,
        queue_id: str,
        step_index: int,
        turn_workspace: Path,
    ) -> dict[str, Any] | None:
        current = self.frame_manager.current_frame()
        if current is None or current.parent_frame_id is None:
            return None
        work_package = dict(self.frame_manager.work_package_for(current) or {})
        return self._append_session_event(
            self.root,
            session_id,
            {
                "type": "system_note",
                "role": "system",
                "content": (
                    "step limit final gate was blocked because a child frame still has unreturned work. "
                    "Return the child evidence to the parent frame before any final answer can be accepted."
                ),
                "code": "step_limit_plan_incomplete",
                "reason_code": "child_frame_active_at_step_limit",
                "details": {
                    "blocked_tool": "step_limit_final_gate",
                    "failure_type": "child_frame_active_at_step_limit",
                    "blocked_by": "plan_execution_contract",
                    "active_frame_id": current.frame_id,
                    "active_frame_depth": current.depth,
                    "work_package": work_package,
                    "successful_tool_evidence": self._child_frame_successful_tool_evidence(),
                    "allowed_next_actions": [
                        {
                            "tool": "return_to_parent",
                            "strategy": "return child-frame evidence before final acceptance",
                        }
                    ],
                    "suggested_fix": "子フレームの成果を return_to_parent で親へ返し、残りのPlanRecord WorkUnitを実行してください。",
                    "next_required_action": "return_to_parent with child frame evidence",
                },
                "turn_id": turn_id,
                "queue_id": queue_id,
                "step_index": step_index,
                "llm_workspace": str(turn_workspace),
            },
        )

    def _pending_plan_child_task(self) -> dict[str, Any] | None:
        """Return the next unfinished root-frame WorkUnit, if a plan is active.

        Invariant: accepted PlanRecord / task_plan units are executable
        obligations. A root frame must not synthesize finish while a planned
        child task remains pending.
        """
        current = self.frame_manager.current_frame()
        if current is None or current.parent_frame_id is not None:
            return None
        if not current.working_memory.child_tasks:
            return None
        return self.frame_manager.next_pending_child_task(current)

    def _plan_execution_pending_block_event(
        self,
        *,
        session_id: str,
        turn_id: str,
        queue_id: str,
        step_index: int,
        turn_workspace: Path,
        blocked_tool: str,
        pending_task: dict[str, Any],
    ) -> dict[str, Any]:
        task_id = str(pending_task.get("task_id") or "")
        goal = str(pending_task.get("goal") or "")
        return self._append_session_event(
            self.root,
            session_id,
            {
                "type": "system_note",
                "role": "system",
                "content": (
                    f"{blocked_tool} was blocked because the accepted plan still has a pending WorkUnit: "
                    f"{task_id} {goal}"
                ),
                "code": "finish_blocked" if blocked_tool == "finish" else "plan_execution_blocked",
                "reason_code": "plan_work_units_pending",
                "details": {
                    "blocked_tool": blocked_tool,
                    "failure_type": "plan_work_units_pending",
                    "blocked_by": "plan_execution_contract",
                    "pending_task": pending_task,
                    "allowed_next_actions": [
                        {
                            "tool": "open_child_frame",
                            "strategy": "open the pending planned WorkUnit before finish",
                        }
                    ],
                    "suggested_fix": "accepted PlanRecord の未完了WorkUnitを open_child_frame で実行してからfinishしてください。",
                    "next_required_action": "open_child_frame for the pending planned WorkUnit",
                },
                "turn_id": turn_id,
                "queue_id": queue_id,
                "step_index": step_index,
                "llm_workspace": str(turn_workspace),
            },
        )

    def _auto_open_pending_plan_child(
        self,
        *,
        session_id: str,
        turn_id: str,
        queue_id: str,
        step_index: int,
        turn_workspace: Path,
        user_message: str,
        current_model: str,
    ) -> dict[str, Any] | None:
        pending_task = self._pending_plan_child_task()
        if pending_task is None:
            return None
        self._append_session_event(
            self.root,
            session_id,
            {
                "type": "system_note",
                "role": "system",
                "content": f"PLAN_EXECUTION continues with pending WorkUnit: {pending_task.get('task_id') or ''}",
                "code": "plan_execution_continue",
                "reason_code": "pending_work_unit",
                "details": {
                    "pending_task": pending_task,
                    "blocked_by": "plan_execution_contract",
                    "next_required_action": "open_child_frame for the pending planned WorkUnit",
                },
                "turn_id": turn_id,
                "queue_id": queue_id,
                "step_index": step_index,
                "llm_workspace": str(turn_workspace),
            },
        )
        return self._handle_open_child_frame(
            session_id=session_id,
            turn_id=turn_id,
            queue_id=queue_id,
            step_index=step_index,
            tool_args={
                "work_package": pending_task,
                "child_task_id": str(pending_task.get("task_id") or ""),
            },
            turn_workspace=turn_workspace,
            user_message=user_message,
            current_model=current_model,
        )

    def _plan_run_test_should_wait_for_progress(self, *, pending_task: dict[str, Any], progress_state: dict[str, Any]) -> bool:
        """Return True when a planned verifier must wait for implementation progress."""

        if str(pending_task.get("work_type") or "") != "run_test":
            return False
        if not list(progress_state.get("test_paths") or []):
            return True
        if not self._implementation_task_progress_is_incomplete(progress_state):
            return False
        phase = str(progress_state.get("phase") or "")
        return phase not in {"unittest_not_run", "external_audit_required", "external_contract_satisfied"}

    def _first_action_result_allows_auto_return(self, *, work_package: dict[str, Any], tool_name: str) -> bool:
        """Return whether a successful first_action is itself enough child evidence.

        Tool success is not task success. Auto-return is reserved for work types
        whose first concrete observation is the intended evidence. Edit children
        that begin with read_file/search/list_files must continue in the child
        frame so the actual edit responsibility cannot be skipped.
        """
        work_type = str(work_package.get("work_type") or "").strip()
        if work_type in {"inspect", "search"}:
            return tool_name in {"read_file", "search_code", "list_files"}
        if work_type == "run_test":
            return tool_name == "run_command"
        return False

    def _execute_frame_first_action(
        self,
        *,
        session_id: str,
        turn_id: str,
        queue_id: str,
        step_index: int,
        turn_workspace: Path,
        work_package: dict[str, Any],
    ) -> list[dict[str, Any]]:
        first_action = work_package.get("first_action") or {}
        if not isinstance(first_action, dict):
            return []
        tool_name = str(first_action.get("tool") or "").strip()
        tool_args = dict(first_action.get("args") or {}) if isinstance(first_action.get("args") or {}, dict) else {}
        if not tool_name:
            return []
        self._append_session_event(
            self.root,
            session_id,
            {
                "type": "tool_call",
                "tool_name": tool_name,
                "tool_args": tool_args,
                "turn_id": turn_id,
                "queue_id": queue_id,
                "step_index": step_index,
                "llm_workspace": str(turn_workspace),
                "reason_code": "frame_first_action",
            },
        )
        self._append_runtime_event(
            session_id,
            event_name="tool_call_started",
            content=(
                f"Running command via {tool_args.get('shell') or 'auto'}:\n{tool_args.get('command')}"
                if tool_name == "run_command"
                else f"Running frame first_action: {tool_name}"
            ),
            details={"tool_name": tool_name, "tool_args": tool_args, "reason_code": "frame_first_action"},
            turn_id=turn_id,
            queue_id=queue_id,
            step_index=step_index,
            llm_workspace=str(turn_workspace),
            phase="FRAME_FIRST_ACTION",
        )
        try:
            tool_result = self.tools.execute(tool_name, tool_args)
        except Exception as exc:
            tool_result = {
                "ok": False,
                "tool": tool_name,
                "error": str(exc),
                "failure_type": "tool_execution_exception",
                "blocked_by": "runtime_tool_executor",
                "allowed_next_actions": [
                    {"tool": tool_name, "strategy": "retry only after correcting the arguments or choosing a safer equivalent action"}
                ],
                "suggested_fix": "tool_result.error を読み、同じ不正引数を繰り返さずに次の有効アクションへ進んでください。",
                "next_required_action": "correct the tool arguments or choose an allowed recovery action",
            }
        self._append_session_event(
            self.root,
            session_id,
            {
                "type": "tool_result",
                "tool_name": tool_name,
                "content": json.dumps(tool_result, ensure_ascii=False),
                "ok": bool(tool_result.get("ok")),
                "turn_id": turn_id,
                "queue_id": queue_id,
                "step_index": step_index,
                "llm_workspace": str(turn_workspace),
                "reason_code": "frame_first_action",
            },
        )
        self._append_runtime_event(
            session_id,
            event_name="tool_call_finished",
            content=json.dumps(tool_result, ensure_ascii=False),
            details={"tool_name": tool_name, "tool_args": tool_args, "tool_result": tool_result, "ok": bool(tool_result.get("ok")), "reason_code": "frame_first_action"},
            turn_id=turn_id,
            queue_id=queue_id,
            step_index=step_index,
            llm_workspace=str(turn_workspace),
            phase="FRAME_FIRST_ACTION",
        )
        self.frame_manager.update_from_tool_result(tool_name, tool_args, tool_result)
        if bool(tool_result.get("ok")) and self._first_action_result_allows_auto_return(
            work_package=work_package,
            tool_name=tool_name,
        ):
            self._return_after_first_action_success(
                session_id=session_id,
                turn_id=turn_id,
                queue_id=queue_id,
                step_index=step_index,
                turn_workspace=turn_workspace,
                tool_name=tool_name,
                tool_args=tool_args,
                tool_result=tool_result,
            )
        return [{"tool_name": tool_name, "tool_args": dict(tool_args), "tool_result": tool_result}]

    def _controller_finish_blocked_for_current_evidence(self, recent_events: list[dict[str, Any]]) -> bool:
        newer_tool_results: list[str] = []
        for event in reversed(recent_events):
            event_type = str(event.get("type") or "")
            if event_type == "tool_result":
                newer_tool_results.append(str(event.get("tool_name") or ""))
                continue
            if (
                event_type == "system_note"
                and str(event.get("code") or "") == "finish_blocked"
                and str(event.get("reason_code") or "") == "finish_acceptance_failed"
            ):
                details = event.get("details") if isinstance(event.get("details"), dict) else {}
                blocked_missing = {str(item) for item in details.get("missing") or []}
                if "visible_result_sanity_passed" in blocked_missing:
                    return not any(tool in {"write_file", "append_file", "replace_text"} for tool in newer_tool_results)
                return not newer_tool_results
        return False

    def _latest_successful_tool_name(self, steps: list[dict[str, Any]]) -> str:
        for step in reversed(steps):
            result = step.get("tool_result") or {}
            if bool(result.get("ok")):
                return str(step.get("tool_name") or "")
        return ""

    def _tool_action_already_succeeded(self, *, tool_name: str, tool_args: dict[str, Any], steps: list[dict[str, Any]]) -> bool:
        """Return whether an observational first_action is already satisfied."""
        tool = str(tool_name or "").strip()
        args = dict(tool_args or {})
        if tool not in {"list_files", "read_file", "search_code", "run_command"}:
            return False
        for step in steps:
            if str(step.get("tool_name") or "") != tool:
                continue
            result = step.get("tool_result") or {}
            if not bool(result.get("ok")):
                continue
            previous_args = dict(step.get("tool_args") or {})
            if tool == "list_files":
                if str(previous_args.get("path") or ".") == str(args.get("path") or "."):
                    return True
            elif tool == "read_file":
                if str(previous_args.get("path") or "") == str(args.get("path") or ""):
                    return True
            elif tool == "search_code":
                if json.dumps(previous_args, ensure_ascii=False, sort_keys=True) == json.dumps(args, ensure_ascii=False, sort_keys=True):
                    return True
            elif tool == "run_command":
                previous_command = str((step.get("tool_result") or {}).get("command") or previous_args.get("command") or "").strip()
                current_command = str(args.get("command") or "").strip()
                if previous_command and previous_command == current_command:
                    return True
        return False

    def _work_package_first_action_already_succeeded(self, work_package: dict[str, Any], steps: list[dict[str, Any]]) -> bool:
        if str(work_package.get("work_type") or "") not in {"inspect", "search"}:
            return False
        first_action = work_package.get("first_action") or {}
        if not isinstance(first_action, dict):
            return False
        return self._tool_action_already_succeeded(
            tool_name=str(first_action.get("tool") or ""),
            tool_args=dict(first_action.get("args") or {}) if isinstance(first_action.get("args") or {}, dict) else {},
            steps=steps,
        )

    def _environment_observation_dead_end_response(self, *, user_message: str, steps: list[dict[str, Any]]) -> str | None:
        """Stop repeated environment inspection when it cannot satisfy the goal."""
        if len(steps) < 2:
            return None
        text = str(user_message or "").lower()
        requested_commands = self._extract_requested_commands(user_message)
        if requested_commands:
            return None
        if any(marker in text for marker in ["作", "生成", "実行", "表示", "出力", "修正", "create", "run", "execute", "display", "show", "fix"]):
            return None
        if any(marker in text for marker in ["一覧", "リスト", "ls", "ファイル", "ディレクトリ", "構成", "中身", "確認"]):
            return None
        observed: list[str] = []
        for step in steps:
            tool_name = str(step.get("tool_name") or "")
            result = step.get("tool_result") or {}
            if tool_name == "list_files" and bool(result.get("ok")):
                observed.append(f"list_files:{json.dumps(result.get('items') or [], ensure_ascii=False, sort_keys=True)}")
                continue
            if tool_name == "run_command" and bool(result.get("ok")):
                command = str(result.get("command") or "").strip().lower()
                head = command.split()[0] if command.split() else ""
                if head not in {"ls", "find", "tree", "pwd"}:
                    return None
                observed.append(f"run_command:{head}:{str(result.get('stdout') or '').strip()[:240]}")
                continue
            return None
        if len(observed) < 2:
            return None
        if len(set(observed[-3:])) > 1 and not all(item.startswith(("list_files:[]", "run_command:ls:total 0")) for item in observed[-3:]):
            return None
        return (
            "この依頼は抽象的で、現在のワークスペース観測だけでは完了条件を定義できません。"
            "同じ環境確認を繰り返しても前進しないため、作成したい成果物、調査対象、改善したい範囲を具体的に指定してください。"
        )

    def _effective_max_steps_per_message(self, *, user_message: str, configured: int) -> int:
        """Return the execution budget for the current user contract.

        `max_steps_per_message` is a safety valve, not a completion contract.
        Requests that explicitly require implementation plus unittest evidence
        need a larger bounded budget so recovery steps do not consume all
        execution before tests can be written and run.
        """
        base = max(1, int(configured or 12))
        text = str(user_message or "").lower()
        implementation_markers = [
            "実装",
            "作成",
            "作り",
            "作って",
            "プログラム",
            "追加",
            "implement",
            "create",
            "write",
            "solver",
            "ソルバー",
        ]
        verification_markers = [
            "unittest",
            "tests/",
            "テスト",
            "検証",
            "実行",
            "動作確認",
            "クリア",
            "run",
            "execute",
            "test",
        ]
        profile = self._planning_profile_for_message(user_message)
        if planning_required_for_profile(profile):
            target = int(
                self.runtime_config.get("planned_implementation_max_steps")
                or self.runtime_config.get("verified_implementation_max_steps")
                or 48
            )
            return max(base, target)
        if any(marker in text for marker in implementation_markers) and any(marker in text for marker in verification_markers):
            target = int(self.runtime_config.get("verified_implementation_max_steps") or 32)
            return max(base, target)
        return base

    def _effective_frame_step_limit(self, *, current_frame: Any, turn_max_steps: int) -> int:
        """Return the frame-local budget without creating a second root truth.

        Root-frame progress is the current user turn, so it must use the same
        budget as the turn CompletionContract. Child frames keep a bounded
        local budget because they are delegated work packages, not the whole
        user request.
        """
        if current_frame is None:
            return max(1, int(turn_max_steps or 1))
        if getattr(current_frame, "parent_frame_id", None) is None:
            return max(1, int(turn_max_steps or 1))
        configured = int(self.runtime_config.get("child_frame_max_steps") or 15)
        return max(1, configured)

    def _route_read_consumed_after_latest_edit(self, *, steps: list[dict[str, Any]], path: str) -> bool:
        """Return whether the latest successful edit for ``path`` has been read."""

        normalized_path = str(path or "").replace("\\", "/").strip()
        if not normalized_path:
            return False
        latest_edit_index = -1
        read_after_latest_edit = False
        for index, step in enumerate(steps):
            step_tool = str(step.get("tool_name") or "")
            step_args = step.get("tool_args") if isinstance(step.get("tool_args"), dict) else {}
            step_result = step.get("tool_result") if isinstance(step.get("tool_result"), dict) else {}
            step_path = str(step_result.get("path") or step_args.get("path") or "").replace("\\", "/").strip()
            if step_path != normalized_path:
                continue
            if step_tool in {"write_file", "append_file", "replace_text"} and bool(step_result.get("ok")):
                latest_edit_index = index
                read_after_latest_edit = False
                continue
            if step_tool == "read_file" and bool(step_result.get("ok")) and latest_edit_index >= 0:
                read_after_latest_edit = True
        return read_after_latest_edit

    def _route_match_failure_after_consumed_read_requires_write(
        self,
        *,
        latest_edit_match_failure_recovery: dict[str, Any],
        route_read_consumed: bool,
    ) -> bool:
        """Return whether a route repair must switch from local replace to full write.

        A route grants one source observation after the latest successful edit.
        If a later replace_text misses, the controller must not create a second
        observation channel for the same file; the next repair has to be a
        contract-reducing complete write.
        """

        return bool(latest_edit_match_failure_recovery) and bool(route_read_consumed)

    def _read_consumption_after_latest_edit(
        self,
        *,
        steps: list[dict[str, Any]],
        paths: list[str],
    ) -> tuple[list[str], list[str]]:
        """Return (consumed, unread) paths for one-read repair contracts."""

        normalized_paths: list[str] = []
        for item in paths:
            path = str(item or "").replace("\\", "/").strip()
            if path and path not in normalized_paths:
                normalized_paths.append(path)
        consumed = [
            path
            for path in normalized_paths
            if self._route_read_consumed_after_latest_edit(steps=steps, path=path)
        ]
        unread = [path for path in normalized_paths if path not in set(consumed)]
        return consumed, unread

    def _step_limit_missing_requirements(self, *, user_message: str, steps: list[dict[str, Any]]) -> list[str]:
        contract = _finish_acceptance_contract(user_message)
        evidence = _finish_acceptance_evidence(steps)
        missing = [item for item in contract if not bool(evidence.get(item))]
        current = self.frame_manager.current_frame()
        if current is not None and current.parent_frame_id is not None:
            missing.append("child_frame_returned")
        if self._pending_plan_child_task() is not None:
            missing.append("plan_work_units_completed")
        return missing

    def _successful_unittest_run_count(self, steps: list[dict[str, Any]]) -> int:
        count = 0
        for step in steps:
            if str(step.get("tool_name") or "") != "run_command":
                continue
            result = step.get("tool_result") if isinstance(step.get("tool_result"), dict) else {}
            args = step.get("tool_args") if isinstance(step.get("tool_args"), dict) else {}
            command = str(result.get("command") or args.get("command") or "").lower()
            if _unittest_command_is_acceptance(command) and bool(result.get("ok")):
                count += 1
        return count

    def _steps_observed_python_verification(self, steps: list[dict[str, Any]]) -> bool:
        for step in steps:
            if str(step.get("tool_name") or "") != "run_command":
                continue
            result = step.get("tool_result") if isinstance(step.get("tool_result"), dict) else {}
            args = step.get("tool_args") if isinstance(step.get("tool_args"), dict) else {}
            command = str(result.get("command") or args.get("command") or "").lower()
            if "unittest" in command or "pytest" in command:
                return True
        return False

    def _unittest_failure_is_missing_tests_artifact(self, result: dict[str, Any]) -> bool:
        command = str(result.get("command") or "").lower()
        if "unittest" not in command and "pytest" not in command:
            return False
        output = "\n".join(
            str(result.get(key) or "")
            for key in ("stderr", "stdout", "error")
            if str(result.get(key) or "")
        )
        lowered = output.lower()
        return (
            "start directory is not importable" in lowered
            and "'tests'" in lowered
        ) or (
            "no such file or directory" in lowered
            and "tests" in lowered
        ) or bool(
            re.search(
                r"failed to import test module:\s*([a-zA-Z_][\w.]*)[\s\S]+no module named ['\"]\1['\"]",
                output,
                re.IGNORECASE,
            )
            and re.search(
                r"failed to import test module:\s*(?:[\w.]+\.)?test[_\w]*",
                output,
                re.IGNORECASE,
            )
        )

    def _unittest_failure_signature(self, result: dict[str, Any], *, turn_workspace: Path | None = None) -> str:
        """Stable signature for a failed unittest observation, independent of workspace path."""

        command = str(result.get("command") or "").strip()
        output = "\n".join(
            str(result.get(key) or "")
            for key in ("stderr", "stdout", "error")
            if str(result.get(key) or "")
        )
        if turn_workspace is not None:
            output = output.replace(str(Path(turn_workspace).resolve()), "<workspace>")
        output = re.sub(r'File "([^"]+)"', lambda match: f'File "{Path(match.group(1)).name}"', output)
        output = re.sub(r", line \d+", ", line <n>", output)
        output = re.sub(r"line \d+", "line <n>", output)
        payload = json.dumps(
            {"command": command, "output": output[-4000:]},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]

    def _repo_map_excerpt_for_workspace(self, turn_workspace: Path | None) -> str:
        if turn_workspace is None:
            return ""
        try:
            repo_map = build_repo_map(Path(turn_workspace), max_files=80, max_symbols=160)
        except Exception:
            return ""
        return format_repo_map_for_prompt(repo_map, max_chars=1600)

    def _implementation_progress_event_signature(self, state: dict[str, Any]) -> dict[str, Any]:
        implementation_repair_hints = list(state.get("implementation_source_repair_hints") or [])
        unittest_repair_hints = [str(item) for item in state.get("unittest_repair_hints") or []]
        return {
            "phase": str(state.get("phase") or ""),
            "contract_state": str(state.get("contract_state") or ""),
            "missing_requirements": [str(item) for item in state.get("missing_requirements") or []],
            "allowed_next_actions": [str(item) for item in state.get("allowed_next_actions") or []],
            "repeated_unittest_failure_signature": bool(state.get("repeated_unittest_failure_signature")),
            "latest_unittest_failure_signature": str(state.get("latest_unittest_failure_signature") or ""),
            "repair_hints": [*implementation_repair_hints, *unittest_repair_hints],
            "unittest_repair_hints": unittest_repair_hints,
            "implementation_generation_repetitive_output": bool(state.get("implementation_generation_repetitive_output")),
            "test_generation_repetitive_output": bool(state.get("test_generation_repetitive_output")),
            "repair_generation_repetitive_output": bool(state.get("repair_generation_repetitive_output")),
            "implementation_paths": [str(item) for item in state.get("implementation_paths") or []],
            "test_paths": [str(item) for item in state.get("test_paths") or []],
            "unittest_run": bool(state.get("unittest_run")),
            "unittest_passed": bool(state.get("unittest_passed")),
            "external_audit_passed": bool(state.get("external_audit_passed")),
        }

    def _append_implementation_progress_event(
        self,
        *,
        session_id: str,
        turn_id: str,
        queue_id: str,
        step_index: int,
        turn_workspace: Path,
        state: dict[str, Any],
        trigger: str,
    ) -> None:
        if not bool(state.get("applicable")):
            return
        signature = self._implementation_progress_event_signature(state)
        for event in reversed(read_jsonl(self.paths.session_events_path(session_id))):
            if event.get("type") != "system_note" or str(event.get("code") or "") != "implementation_task_progress":
                continue
            details = event.get("details") if isinstance(event.get("details"), dict) else {}
            if details.get("signature") == signature:
                return
            break
        phase = str(state.get("phase") or "")
        allowed = [str(item) for item in state.get("allowed_next_actions") or [] if str(item).strip()]
        missing = [str(item) for item in state.get("missing_requirements") or [] if str(item).strip()]
        implementation_repair_hints = list(state.get("implementation_source_repair_hints") or [])
        unittest_repair_hints = [str(item) for item in state.get("unittest_repair_hints") or []]
        self._append_session_event(
            self.root,
            session_id,
            {
                "type": "system_note",
                "role": "system",
                "content": f"実装タスク進行状態: {phase}",
                "code": "implementation_task_progress",
                "reason_code": "event_sourced_progress_state",
                "details": {
                    "trigger": trigger,
                    "phase": phase,
                    "contract_state": str(state.get("contract_state") or ""),
                    "missing_requirements": missing,
                    "allowed_next_actions": allowed,
                    "repair_hints": [*implementation_repair_hints, *unittest_repair_hints],
                    "unittest_repair_hints": unittest_repair_hints,
                    "implementation_generation_repetitive_output": bool(state.get("implementation_generation_repetitive_output")),
                    "test_generation_repetitive_output": bool(state.get("test_generation_repetitive_output")),
                    "repair_generation_repetitive_output": bool(state.get("repair_generation_repetitive_output")),
                    "signature": signature,
                },
                "turn_id": turn_id,
                "queue_id": queue_id,
                "step_index": step_index,
                "llm_workspace": str(turn_workspace),
            },
        )

    def _implementation_task_progress_state(
        self,
        *,
        user_message: str,
        steps: list[dict[str, Any]],
        session_id: str | None = None,
        turn_workspace: Path | None = None,
    ) -> dict[str, Any]:
        """Canonical progress state for generic implementation + unittest tasks."""
        contract = _finish_acceptance_contract(user_message)
        evidence = _finish_acceptance_evidence(steps)
        plan_context = self._plan_record_execution_context()
        plan_strategy = str(plan_context.get("plan_strategy") or "")
        plan_verification_contract = [str(item) for item in plan_context.get("plan_verification_contract") or []]
        plan_requires_python_verification = (
            plan_strategy in {
                "state_space_search",
                "dynamic_programming",
                "constraint_satisfaction",
                "graph_shortest_path",
                "classical_planning",
            }
            or any(item in {"final_verifier", "solution_validator", "path_validator", "plan_step_validator"} for item in plan_verification_contract)
        )
        if self._steps_observed_python_verification(steps) and bool(evidence.get("python_artifact_written")):
            for item in ["python_artifact_written", "tests_written", "meaningful_tests", "unittest_run", "unittest_passed"]:
                if item not in contract:
                    contract.append(item)
        if plan_requires_python_verification:
            for item in ["python_artifact_written", "tests_written", "meaningful_tests", "unittest_run", "unittest_passed"]:
                if item not in contract:
                    contract.append(item)
        if "python_artifact_written" not in contract or "unittest_run" not in contract:
            return {"applicable": False, "phase": "not_applicable"}
        artifact_paths = [str(path or "").replace("\\", "/") for path in evidence.get("artifact_paths") or []]
        implementation_paths = [path for path in artifact_paths if _artifact_path_is_python_implementation(path)]
        test_paths = [path for path in artifact_paths if _artifact_path_is_test(path)]
        latest_impl_path = implementation_paths[-1] if implementation_paths else ""
        latest_test_path = test_paths[-1] if test_paths else ""

        def source_for(path: str) -> str:
            if not path:
                return ""
            if turn_workspace is not None:
                candidate = (Path(turn_workspace) / path).resolve()
                try:
                    candidate.relative_to(Path(turn_workspace).resolve())
                except ValueError:
                    candidate = None  # type: ignore[assignment]
                if candidate is not None and candidate.exists() and candidate.is_file():
                    return candidate.read_text(encoding="utf-8", errors="replace")
            for step in reversed(steps):
                result = step.get("tool_result") if isinstance(step.get("tool_result"), dict) else {}
                args = step.get("tool_args") if isinstance(step.get("tool_args"), dict) else {}
                step_path = str(result.get("path") or args.get("path") or "").replace("\\", "/")
                if step_path != path:
                    continue
                if result.get("ok") is False:
                    continue
                tool_name = str(step.get("tool_name") or "")
                if tool_name in {"write_file", "append_file"}:
                    return str(args.get("content") or "")
                if tool_name == "replace_text":
                    return str(args.get("new_text") or "")
            return ""

        latest_impl_source = source_for(latest_impl_path)
        latest_test_sources = [(path, source_for(path)) for path in test_paths]
        latest_test_sources = [(path, source) for path, source in latest_test_sources if source]
        test_source_issues = self._test_source_contract_issues(
            user_message=user_message,
            test_sources=latest_test_sources,
        )
        repo_map_excerpt = self._repo_map_excerpt_for_workspace(turn_workspace)
        latest_llm_output_issue = ""
        if session_id:
            for event in reversed(read_jsonl(self.paths.session_events_path(session_id))):
                if event.get("type") == "system_note" and str(event.get("code") or "") == "llm_output_issue":
                    latest_llm_output_issue = str(event.get("content") or "")
                    break
        placeholder_present = bool(latest_impl_source) and self._python_source_has_pass_only_callable(latest_impl_source)
        implementation_source_issues = (
            self._implementation_source_contract_issues(user_message=user_message, source=latest_impl_source)
            if latest_impl_source
            else []
        )
        implementation_source_repair_hints = (
            [
                *self._python_source_placeholder_repair_hints(
                    latest_impl_source,
                ),
                *self._public_api_wrapper_repair_hints(
                    user_message=user_message,
                    source=latest_impl_source,
                ),
                *self._python_source_input_contract_repair_hints(
                    user_message=user_message,
                    source=latest_impl_source,
                ),
                *self._python_source_recursive_destructive_state_repair_hints(
                    latest_impl_source,
                ),
                *self._state_space_search_replay_verifier_repair_hints(
                    source=latest_impl_source,
                    issues=implementation_source_issues,
                ),
            ][:6]
            if latest_impl_source and implementation_source_issues
            else []
        )
        requested_impl_paths = self._requested_implementation_paths(user_message)
        if latest_impl_path and requested_impl_paths and latest_impl_path not in requested_impl_paths:
            implementation_source_issues.append(
                "ユーザー要求は実装ファイル "
                + " / ".join(requested_impl_paths)
                + f" を明示していますが、現在の実装成果物は {latest_impl_path} です。"
                "指定された実装ファイルへ公開APIを置いてください。"
            )

        latest_semantic_review = (
            self._latest_semantic_review_requiring_revision_event(session_id=str(session_id))
            if session_id
            else None
        )
        semantic_requires_revision = latest_semantic_review is not None
        latest_semantic_review_details = (
            latest_semantic_review.get("details")
            if latest_semantic_review is not None and isinstance(latest_semantic_review.get("details"), dict)
            else {}
        )
        latest_semantic_review_text = str(
            latest_semantic_review_details.get("review")
            or (latest_semantic_review.get("content") if latest_semantic_review is not None else "")
            or ""
        ).strip()
        latest_semantic_issues = [
            str(issue).strip()
            for issue in (latest_semantic_review_details.get("semantic_issues") or [])
            if str(issue).strip()
        ]
        semantic_repair_target = ""
        semantic_test_repair_paths = self._semantic_issue_test_repair_paths(
            semantic_issues=latest_semantic_issues,
            test_paths=test_paths,
            latest_test_path=latest_test_path,
        )
        semantic_test_repair_targets: list[str] = []
        if semantic_requires_revision:
            lower_issues = "\n".join(latest_semantic_issues).lower()
            if semantic_test_repair_paths or "tests " in lower_issues or "tests の" in lower_issues or "test " in lower_issues:
                semantic_repair_target = "test_artifact"
                semantic_test_repair_targets = semantic_test_repair_paths or [latest_test_path or "tests/test_*.py"]
            else:
                semantic_repair_target = "implementation_artifact"
        semantic_repair_read_consumed_paths, semantic_repair_unread_paths = self._read_consumption_after_latest_edit(
            steps=steps,
            paths=semantic_test_repair_targets,
        )

        latest_unittest_result: dict[str, Any] | None = None
        latest_any_unittest_result: dict[str, Any] | None = None
        successful_unittest_count = self._successful_unittest_run_count(steps)
        latest_failed_unittest_index = -1
        previous_same_unittest_failure_index = -1
        latest_unittest_failure_signature = ""
        latest_edit_after_failed_unittest = -1
        latest_read_paths_after_failed_unittest: set[str] = set()
        latest_failed_unittest_paths: list[str] = []
        failed_unittest_events: list[tuple[int, str]] = []
        same_signature_nonreducing_edit_paths: list[str] = []
        same_signature_read_paths: list[str] = []
        for step in reversed(steps):
            if str(step.get("tool_name") or "") != "run_command":
                continue
            result = step.get("tool_result") if isinstance(step.get("tool_result"), dict) else {}
            command_text = str(result.get("command") or "")
            if "unittest" in command_text.lower() and latest_any_unittest_result is None:
                latest_any_unittest_result = result
            if _unittest_command_is_acceptance(command_text):
                latest_unittest_result = result
                break
        if latest_unittest_result is None:
            latest_unittest_result = latest_any_unittest_result
        latest_unittest_triage_result = latest_unittest_result
        if latest_any_unittest_result is not None and not bool(latest_any_unittest_result.get("ok")):
            latest_unittest_triage_result = latest_any_unittest_result
        latest_unittest_output_for_triage = ""
        latest_unittest_missing_import = {}
        if latest_unittest_triage_result is not None and not bool(latest_unittest_triage_result.get("ok")):
            latest_unittest_output_for_triage = "\n".join(
                str(latest_unittest_triage_result.get(key) or "")
                for key in ("stderr", "stdout", "error")
                if str(latest_unittest_triage_result.get(key) or "")
            )
            latest_unittest_missing_import = self._unittest_missing_import_details(
                output=latest_unittest_output_for_triage,
            )
            latest_failed_unittest_paths = self._extract_unittest_failure_paths(
                output=latest_unittest_output_for_triage,
                turn_workspace=turn_workspace,
            )
        latest_unittest_missing_tests_artifact = (
            latest_unittest_result is not None
            and not bool(latest_unittest_result.get("ok"))
            and self._unittest_failure_is_missing_tests_artifact(latest_unittest_result)
        )
        latest_unittest_tool_failure_type = str((latest_unittest_triage_result or {}).get("failure_type") or "")
        latest_unittest_timed_out = (
            latest_unittest_tool_failure_type == "command_timeout"
            or "timed out" in str((latest_unittest_triage_result or {}).get("stderr") or "").lower()
            or "timeout" in str((latest_unittest_triage_result or {}).get("stderr") or "").lower()
            or "timed out" in str((latest_unittest_triage_result or {}).get("error") or "").lower()
            or "timeout" in str((latest_unittest_triage_result or {}).get("error") or "").lower()
        )
        for index, step in enumerate(steps):
            step_tool = str(step.get("tool_name") or "")
            step_args = step.get("tool_args") if isinstance(step.get("tool_args"), dict) else {}
            step_result = step.get("tool_result") if isinstance(step.get("tool_result"), dict) else {}
            command = str(step_result.get("command") or step_args.get("command") or "").lower()
            if step_tool == "run_command" and "unittest" in command and not bool(step_result.get("ok")):
                latest_failed_unittest_index = index
                latest_edit_after_failed_unittest = -1
                latest_read_paths_after_failed_unittest = set()
                failed_unittest_events.append((
                    index,
                    self._unittest_failure_signature(step_result, turn_workspace=turn_workspace),
                ))
                continue
            if latest_failed_unittest_index >= 0:
                if step_tool in {"write_file", "append_file", "replace_text"} and bool(step_result.get("ok")):
                    latest_edit_after_failed_unittest = index
                if step_tool == "read_file" and bool(step_result.get("ok")):
                    read_path = str(step_result.get("path") or step_args.get("path") or "").replace("\\", "/")
                    if read_path:
                        latest_read_paths_after_failed_unittest.add(read_path)

        repeated_unittest_failure_signature = False
        if latest_failed_unittest_index >= 0 and failed_unittest_events:
            latest_unittest_failure_signature = failed_unittest_events[-1][1]
            for index, signature in failed_unittest_events[:-1]:
                if signature == latest_unittest_failure_signature:
                    previous_same_unittest_failure_index = index
            if previous_same_unittest_failure_index >= 0:
                read_paths_between_same_failures: set[str] = set()
                edit_between_same_failures = False
                for index, step in enumerate(steps):
                    if index <= previous_same_unittest_failure_index or index >= latest_failed_unittest_index:
                        continue
                    step_tool = str(step.get("tool_name") or "")
                    step_args = step.get("tool_args") if isinstance(step.get("tool_args"), dict) else {}
                    step_result = step.get("tool_result") if isinstance(step.get("tool_result"), dict) else {}
                    if step_tool == "read_file" and bool(step_result.get("ok")):
                        read_path = str(step_result.get("path") or step_args.get("path") or "").replace("\\", "/")
                        if read_path:
                            read_paths_between_same_failures.add(read_path)
                    if step_tool in {"write_file", "append_file", "replace_text"} and bool(step_result.get("ok")):
                        edit_between_same_failures = True
                        edit_path = str(step_result.get("path") or step_args.get("path") or "").replace("\\", "/")
                        if edit_path and edit_path not in same_signature_nonreducing_edit_paths:
                            same_signature_nonreducing_edit_paths.append(edit_path)
                same_signature_read_paths = sorted(read_paths_between_same_failures)
                if edit_between_same_failures:
                    latest_read_paths_after_failed_unittest.update(read_paths_between_same_failures)
                    repeated_unittest_failure_signature = True

        latest_unittest_failed_paths_are_tests = bool(latest_failed_unittest_paths) and all(
            _artifact_path_is_test(str(item).replace("\\", "/"))
            for item in latest_failed_unittest_paths
        )
        state_space_test_impl_contract_suspected = False
        state_space_test_fixture_value_suspected = False
        if plan_strategy == "state_space_search" and latest_unittest_failed_paths_are_tests:
            fail_count = len(re.findall(r"(?m)^FAIL:", latest_unittest_output_for_triage))
            assertion_lines = "\n".join(
                match.group(1).strip()
                for match in re.finditer(
                    r"(?m)^\s+([^\n]*assert[A-Za-z0-9_]*[^\n]*)$",
                    latest_unittest_output_for_triage,
                )
            )
            failure_names = "\n".join(
                match.group(1)
                for match in re.finditer(r"(?m)^FAIL:\s+([^\s(]+)", latest_unittest_output_for_triage)
            )
            assertion_context = f"{assertion_lines}\n{failure_names}".lower()
            contract_assertion_tokens = (
                "assertraises",
                "assertisnotnone",
                "assertisnone",
                "asserttrue",
                "assertfalse",
                "solve",
                "search",
                "verify",
                "validate",
                "solvable",
                "legal",
                "valid",
                "goal",
            )
            state_space_test_impl_contract_suspected = (
                fail_count > 1
                or any(token in assertion_context for token in contract_assertion_tokens)
            )
            output_lower = latest_unittest_output_for_triage.lower()
            fixture_value_tokens = (
                "expected_state",
                "expected state",
                "expected =",
                "len(moves)",
                "len(actions)",
                "len(legal",
                "assertequal(len(",
                "assertequal(new_state",
                "lists differ",
            )
            state_space_test_fixture_value_suspected = (
                "assertionerror" in output_lower
                and any(token in output_lower for token in fixture_value_tokens)
            )
        state_space_test_fixture_repair_mode = (
            plan_strategy == "state_space_search"
            and latest_unittest_failed_paths_are_tests
            and not state_space_test_impl_contract_suspected
            and latest_test_path
            and "AssertionError" in latest_unittest_output_for_triage
        )
        failed_unittest_recovery_read_paths: list[str] = []
        if latest_unittest_triage_result is not None and not bool(latest_unittest_triage_result.get("ok")):
            import_module = str(latest_unittest_missing_import.get("module") or "")
            impl_module = Path(str(latest_impl_path)).stem if latest_impl_path else ""
            if state_space_test_fixture_repair_mode:
                recovery_read_candidates = [latest_test_path]
            elif (
                latest_unittest_missing_import
                and latest_impl_path
                and impl_module
                and import_module.split(".")[-1] == impl_module
            ):
                recovery_read_candidates = [latest_impl_path]
            else:
                recovery_read_candidates = [*latest_failed_unittest_paths, latest_test_path, latest_impl_path]
            for item in recovery_read_candidates:
                normalized_item = str(item or "").replace("\\", "/")
                if normalized_item and normalized_item not in failed_unittest_recovery_read_paths:
                    failed_unittest_recovery_read_paths.append(normalized_item)
        failed_unittest_unread_paths = [
            item for item in failed_unittest_recovery_read_paths if item not in latest_read_paths_after_failed_unittest
        ]
        failed_unittest_recovery_editable_paths = [
            item for item in failed_unittest_recovery_read_paths if item in latest_read_paths_after_failed_unittest
        ]
        failed_unittest_recovery_read_consumed = bool(failed_unittest_recovery_read_paths) and not failed_unittest_unread_paths
        same_signature_nonreducing_edit_path_set = (
            {
                str(item).replace("\\", "/")
                for item in same_signature_nonreducing_edit_paths
                if str(item).strip()
            }
            if repeated_unittest_failure_signature
            else set()
        )
        state_space_no_match_exact_replace_paths: list[str] = []
        state_space_fixture_value_impl_blocked_paths: list[str] = []
        blocked_failed_unittest_replace_paths = [
            item
            for item in self._blocked_failed_unittest_replace_paths(session_id=session_id)
            if item in failed_unittest_recovery_read_paths
        ]
        if (
            state_space_test_fixture_value_suspected
            and latest_impl_path
            and latest_test_path
            and latest_test_path in failed_unittest_recovery_editable_paths
        ):
            impl_recovery = self._latest_edit_match_failure_recovery(steps=steps, path=latest_impl_path)
            if (
                impl_recovery
                and int(impl_recovery.get("failure_index") or -1) > latest_failed_unittest_index
                and not impl_recovery.get("successful_edit_after_failure")
            ):
                state_space_fixture_value_impl_blocked_paths.append(latest_impl_path)
            if (
                latest_impl_path in blocked_failed_unittest_replace_paths
                and latest_impl_path not in state_space_fixture_value_impl_blocked_paths
            ):
                state_space_fixture_value_impl_blocked_paths.append(latest_impl_path)
        if (
            state_space_test_fixture_value_suspected
            and latest_impl_path
            and latest_impl_path in failed_unittest_recovery_editable_paths
            and latest_impl_path not in state_space_fixture_value_impl_blocked_paths
        ):
            state_space_no_match_exact_replace_paths.append(latest_impl_path)
        failed_unittest_no_match_write_only_paths: list[str] = []
        for item in blocked_failed_unittest_replace_paths:
            if (
                item in failed_unittest_recovery_editable_paths
                and item not in state_space_no_match_exact_replace_paths
                and item not in state_space_fixture_value_impl_blocked_paths
                and item not in same_signature_nonreducing_edit_path_set
                and item not in failed_unittest_no_match_write_only_paths
            ):
                failed_unittest_no_match_write_only_paths.append(item)
        if latest_failed_unittest_index >= 0:
            for item in failed_unittest_recovery_read_paths:
                recovery = self._latest_edit_match_failure_recovery(steps=steps, path=item)
                if not recovery:
                    continue
                if int(recovery.get("failure_index") or -1) <= latest_failed_unittest_index:
                    continue
                if recovery.get("successful_edit_after_failure"):
                    continue
                if item in latest_read_paths_after_failed_unittest or recovery.get("read_after_failure"):
                    if (
                        item not in state_space_no_match_exact_replace_paths
                        and item not in state_space_fixture_value_impl_blocked_paths
                        and item not in same_signature_nonreducing_edit_path_set
                        and item not in failed_unittest_no_match_write_only_paths
                    ):
                        failed_unittest_no_match_write_only_paths.append(item)
        if failed_unittest_recovery_read_consumed and latest_failed_unittest_index >= 0:
            no_match_evidence_paths: list[str] = []
            for item in failed_unittest_recovery_read_paths:
                recovery = self._latest_edit_match_failure_recovery(steps=steps, path=item)
                if not recovery:
                    continue
                if int(recovery.get("failure_index") or -1) <= latest_failed_unittest_index:
                    continue
                if recovery.get("successful_edit_after_failure"):
                    continue
                if item in latest_read_paths_after_failed_unittest or recovery.get("read_after_failure"):
                    no_match_evidence_paths.append(item)
            for item in blocked_failed_unittest_replace_paths:
                if item not in no_match_evidence_paths:
                    no_match_evidence_paths.append(item)
            if no_match_evidence_paths:
                for item in failed_unittest_recovery_editable_paths:
                    if (
                        item not in state_space_no_match_exact_replace_paths
                        and item not in state_space_fixture_value_impl_blocked_paths
                        and item not in same_signature_nonreducing_edit_path_set
                        and item not in failed_unittest_no_match_write_only_paths
                    ):
                        failed_unittest_no_match_write_only_paths.append(item)

        failed_unittest_repeated_noop_paths: list[str] = []
        if latest_failed_unittest_index >= 0:
            for item in failed_unittest_recovery_read_paths:
                recovery = self._latest_edit_match_failure_recovery(steps=steps, path=item)
                if not recovery:
                    continue
                if int(recovery.get("failure_index") or -1) <= latest_failed_unittest_index:
                    continue
                if recovery.get("successful_edit_after_failure"):
                    continue
                if str(recovery.get("failure_type") or "") != "no_op_edit":
                    continue
                if int(recovery.get("match_failures_since_success") or 0) < 2:
                    continue
                if item not in failed_unittest_repeated_noop_paths:
                    failed_unittest_repeated_noop_paths.append(item)
        failed_unittest_noop_alternate_paths: list[str] = []
        if failed_unittest_repeated_noop_paths:
            for item in failed_unittest_recovery_read_paths:
                if item not in failed_unittest_repeated_noop_paths and item not in failed_unittest_noop_alternate_paths:
                    failed_unittest_noop_alternate_paths.append(item)
            for item in [latest_impl_path, latest_test_path]:
                normalized_item = str(item or "").replace("\\", "/")
                if (
                    normalized_item
                    and normalized_item not in failed_unittest_repeated_noop_paths
                    and normalized_item not in failed_unittest_noop_alternate_paths
                ):
                    failed_unittest_noop_alternate_paths.append(normalized_item)
        failed_unittest_noop_blocked_paths = (
            list(failed_unittest_repeated_noop_paths) if failed_unittest_noop_alternate_paths else []
        )

        initial_implementation_actions = ["write_file <implementation>.py"]
        if latest_impl_path:
            initial_implementation_actions.append("replace_text <implementation>.py")
        implementation_read_consumed = (
            self._route_read_consumed_after_latest_edit(steps=steps, path=latest_impl_path)
            if latest_impl_path
            else False
        )
        test_contract_read_consumed = (
            self._route_read_consumed_after_latest_edit(steps=steps, path=latest_test_path)
            if latest_test_path
            else False
        )
        implementation_repair_strategy = self._implementation_contract_repair_strategy(
            user_message=user_message,
            current_issues=implementation_source_issues,
            candidate_issues=[],
        )
        implementation_targeted_edit_preferred = self._implementation_contract_prefers_targeted_edit(
            current_issues=implementation_source_issues,
        )
        phase = "implementation_missing"
        allowed_next_actions = list(initial_implementation_actions)
        missing_requirements = ["python_artifact_written"]
        if not bool(evidence.get("python_artifact_written")) and semantic_requires_revision:
            phase = "implementation_missing_needs_semantic_revision"
            missing_requirements = ["reviewed_implementation_strategy_not_applied"]
        elif bool(evidence.get("python_artifact_written")) and placeholder_present:
            phase = "implementation_present_but_placeholder"
            allowed_next_actions = ["write_file <implementation>.py", "replace_text <implementation>.py"]
            missing_requirements = ["complete_python_implementation_without_placeholder"]
        elif bool(evidence.get("python_artifact_written")) and implementation_source_issues:
            phase = "implementation_present_needs_semantic_review"
            latest_edit_match_failure_recovery = self._latest_edit_match_failure_recovery(steps=steps, path=latest_impl_path) if latest_impl_path else {}
            if latest_edit_match_failure_recovery and not latest_edit_match_failure_recovery.get("read_after_failure"):
                allowed_next_actions = [f"read_file {latest_impl_path} once"]
            elif latest_edit_match_failure_recovery and not latest_edit_match_failure_recovery.get("successful_edit_after_failure"):
                failed_old_chars = int(latest_edit_match_failure_recovery.get("failed_old_text_chars") or 0)
                failed_new_chars = int(latest_edit_match_failure_recovery.get("failed_new_text_chars") or 0)
                failed_replace_was_large = (
                    failed_old_chars >= 800
                    or failed_new_chars >= 800
                    or failed_old_chars + failed_new_chars >= 1200
                )
                allowed_next_actions = (
                    [f"write_file {latest_impl_path}"]
                    if failed_replace_was_large
                    else [f"replace_text {latest_impl_path}", f"write_file {latest_impl_path}"]
                )
            elif latest_impl_path and not implementation_read_consumed:
                allowed_next_actions = [f"read_file {latest_impl_path} once"]
            elif latest_impl_path and implementation_targeted_edit_preferred:
                has_wrapper_hint = any(
                    isinstance(item, dict) and item.get("suggested_action") == "append_top_level_wrappers"
                    for item in implementation_source_repair_hints
                )
                allowed_next_actions = (
                    [f"append_file {latest_impl_path}", f"replace_text {latest_impl_path}"]
                    if has_wrapper_hint
                    else [f"replace_text {latest_impl_path}"]
                )
            elif latest_impl_path:
                allowed_next_actions = [f"write_file {latest_impl_path}"]
            else:
                allowed_next_actions = ["write_file <implementation>.py", "replace_text <implementation>.py", "read_file <implementation>.py once"]
            missing_requirements = implementation_source_issues
        elif bool(evidence.get("python_artifact_written")) and latest_unittest_missing_tests_artifact and not bool(evidence.get("tests_written")):
            phase = "tests_missing"
            allowed_next_actions = ["write_file tests/test_*.py"]
            missing_requirements = [
                "tests_written",
                "meaningful_tests",
                "unittest_passed",
                "直近の unittest は tests directory が存在しないため失敗しています。実装再生成ではなく tests/test_*.py を作成してください。",
            ]
        elif (
            bool(evidence.get("python_artifact_written"))
            and bool(evidence.get("tests_written"))
            and latest_unittest_missing_tests_artifact
            and latest_edit_after_failed_unittest > latest_failed_unittest_index
        ):
            phase = "unittest_not_run"
            allowed_next_actions = ["run_command python3 -m unittest discover -s tests"]
            missing_requirements = [
                "unittest_run",
                "unittest_passed",
                "直近の unittest 失敗原因だった tests directory は作成済みです。次は同じunittestを再実行してください。",
            ]
        elif latest_unittest_result is not None and not bool(latest_unittest_result.get("ok")):
            phase = "unittest_failed_needs_fix"
            if latest_edit_after_failed_unittest > latest_failed_unittest_index:
                if latest_unittest_timed_out and repeated_unittest_failure_signature:
                    allowed_next_actions = [
                        "run_command with a narrower unittest target",
                    ]
                else:
                    allowed_next_actions = ["run_command python3 -m unittest discover -s tests"]
            else:
                allowed_next_actions = build_unittest_failed_allowed_actions(
                    failed_unittest_noop_blocked_paths=failed_unittest_noop_blocked_paths,
                    failed_unittest_noop_alternate_paths=failed_unittest_noop_alternate_paths,
                    latest_read_paths_after_failed_unittest=latest_read_paths_after_failed_unittest,
                    failed_unittest_no_match_write_only_paths=failed_unittest_no_match_write_only_paths,
                    failed_unittest_unread_paths=failed_unittest_unread_paths,
                    failed_unittest_recovery_editable_paths=failed_unittest_recovery_editable_paths,
                    failed_unittest_recovery_read_paths=failed_unittest_recovery_read_paths,
                    same_signature_nonreducing_edit_paths=same_signature_nonreducing_edit_paths,
                    repeated_unittest_failure_signature=repeated_unittest_failure_signature,
                    state_space_no_match_exact_replace_paths=state_space_no_match_exact_replace_paths,
                    state_space_fixture_value_impl_blocked_paths=state_space_fixture_value_impl_blocked_paths,
                    latest_test_path=latest_test_path or "",
                    latest_impl_path=latest_impl_path or "",
                )
            missing_requirements = ["unittest_passed"]
        elif bool(evidence.get("python_artifact_written")) and not bool(evidence.get("tests_written")):
            phase = "tests_missing"
            allowed_next_actions = ["write_file tests/test_*.py"]
            missing_requirements = ["tests_written", "meaningful_tests"]
        elif bool(evidence.get("tests_written")) and not bool(evidence.get("meaningful_tests")):
            phase = "tests_missing"
            allowed_next_actions = ["write_file tests/test_*.py", "replace_text tests/test_*.py"]
            missing_requirements = ["meaningful_tests"]
        elif bool(evidence.get("tests_written")) and test_source_issues:
            phase = "tests_present_needs_semantic_review"
            if latest_test_path and not test_contract_read_consumed:
                allowed_next_actions = [f"read_file {latest_test_path} once"]
            elif latest_test_path:
                allowed_next_actions = [f"replace_text {latest_test_path}", f"write_file {latest_test_path}"]
            else:
                allowed_next_actions = ["write_file tests/test_*.py", "replace_text tests/test_*.py"]
            missing_requirements = test_source_issues
        elif bool(evidence.get("tests_written")) and semantic_requires_revision:
            phase = "tests_present_needs_semantic_review"
            if semantic_repair_target == "test_artifact":
                targets = semantic_test_repair_targets or [latest_test_path or "tests/test_*.py"]
                allowed_next_actions = (
                    [f"read_file {target} once" for target in semantic_repair_unread_paths]
                    + [f"replace_text {target}" for target in targets]
                    + [f"write_file {target}" for target in targets]
                )
            else:
                allowed_next_actions = ["read_file <implementation>.py once", "replace_text <implementation>.py", "write_file <implementation>.py"]
            missing_requirements = ["semantic_review_revision"]
        elif bool(evidence.get("tests_written")) and not bool(evidence.get("unittest_run")):
            phase = "unittest_not_run"
            allowed_next_actions = ["run_command python3 -m unittest discover -s tests"]
            missing_requirements = ["unittest_run", "unittest_passed"]
        elif bool(evidence.get("unittest_passed")) and successful_unittest_count < 2:
            phase = "external_audit_required"
            allowed_next_actions = ["run_command python3 -m unittest discover -s tests"]
            missing_requirements = ["external_audit_passed"]
        elif bool(evidence.get("unittest_passed")):
            phase = "external_contract_satisfied"
            allowed_next_actions = ["finish"]
            missing_requirements = []
        elif bool(evidence.get("python_artifact_written")):
            phase = "implementation_present_needs_semantic_review"
            allowed_next_actions = ["write_file tests/test_*.py", "run_command python3 -m unittest discover -s tests"]
            missing_requirements = [item for item in contract if not bool(evidence.get(item))]

        unittest_repair_hints: list[str] = []
        latest_generation_too_large_or_repetitive = (
            "repetitive_output" in latest_llm_output_issue
            or "stream_char_limit" in latest_llm_output_issue
        )
        test_generation_repetitive_output = (
            phase == "tests_missing"
            and latest_generation_too_large_or_repetitive
        )
        implementation_generation_repetitive_output = (
            phase in {"implementation_missing", "implementation_missing_needs_semantic_revision"}
            and latest_generation_too_large_or_repetitive
        )
        repair_generation_repetitive_output = (
            phase in {"unittest_failed_needs_fix", "implementation_present_needs_semantic_review", "tests_present_needs_semantic_review"}
            and latest_generation_too_large_or_repetitive
        )
        if latest_unittest_result is not None and not bool(latest_unittest_result.get("ok")):
            if repeated_unittest_failure_signature:
                if latest_unittest_timed_out:
                    unittest_repair_hints.append(
                        "同一unittest command_timeout signatureが再発しています。stdout/stderrにtracebackがないため、"
                        "次は同じ全体unittestを繰り返さず、timeoutを起こす最小のtest method/moduleまたはfixture/solver呼び出しへ検証を狭めてください。"
                    )
                else:
                    unittest_repair_hints.append(
                        "同一unittest failure signatureが再発しています。直前の編集は失敗数/失敗箇所を減らしていないため、"
                        "次の編集前に『実装が契約違反なのか、test fixture/期待値が契約違反なのか』をtraceback行単位で切り分けてください。"
                    )
                if latest_failed_unittest_paths:
                    unittest_repair_hints.append(
                        "失敗traceback対象: "
                        + ", ".join(str(item) for item in latest_failed_unittest_paths[:6])
                    )
            if latest_unittest_missing_import:
                missing_name = str(latest_unittest_missing_import.get("name") or "")
                missing_module = str(latest_unittest_missing_import.get("module") or "")
                source_file = str(latest_unittest_missing_import.get("source_file") or "")
                source_line = str(latest_unittest_missing_import.get("source_line") or "")
                location = f"{source_file}:{source_line}" if source_file and source_line else source_file
                unittest_repair_hints.append(
                    "ImportErrorはtest/import側が要求するmodule-level export不足です: "
                    f"{missing_module}.{missing_name}。"
                    "実装内に同等の値/関数が別名である場合は、ファイル全体を書き直さず、"
                    "小さいreplace_textでalias追加または公開名へのrenameをしてください。"
                )
                if location:
                    unittest_repair_hints.append(
                        "ImportError発生箇所: "
                        f"{location}。stderrに不足symbol名が出ているため、"
                        "implementationをread済みなら同じreadを繰り返さず編集へ進んでください。"
                    )
            if latest_failed_unittest_paths and all(
                _artifact_path_is_test(str(item).replace("\\", "/"))
                for item in latest_failed_unittest_paths
            ):
                assertion_match = re.search(
                    r'File "([^"]+\.py)", line (\d+), in ([^\n]+)\n'
                    r"\s+([^\n]*assert[^\n]*)\n"
                    r"(?:\s+~[^\n]*\n)?"
                    r"AssertionError: ([^\n]+)",
                    latest_unittest_output_for_triage,
                )
                if assertion_match:
                    assertion_text = assertion_match.group(4).strip()
                    assertion_error = assertion_match.group(5).strip()
                    unittest_repair_hints.append(
                        "tracebackはtest artifact内のassert失敗です: "
                        f"{Path(assertion_match.group(1)).name}:{assertion_match.group(2)} "
                        f"{assertion_text} -> AssertionError: {assertion_error}。"
                        "これはtest fixture/期待値の誤りでも、実装が返却値・例外・入力検証契約を満たしていない場合でも発生します。"
                        "次は失敗行の期待値、ユーザー仕様、実装の公開APIを照合し、根拠がある側だけを小さく修正してください。"
                        "実装契約が明確でないまま期待値へ合わせる大きなrewriteは避けてください。"
                    )
            if plan_strategy == "state_space_search":
                unittest_repair_hints.append(
                    "state_space_searchのsolver/search戻り値は、PlanRecordのaction modelに沿ったaction列でなければなりません。"
                    "final_verifier / legal replayがactionとして消費できないstate列や表示用文字列を返すなら、solver出力とverifier入力の契約を揃えてください。"
                )
                if (
                    re.search(r"blank|sentinel|marker|空白|空マス", user_message, re.IGNORECASE)
                    and "tuple.index(x): x not in tuple" in latest_unittest_output_for_triage
                ):
                    unittest_repair_hints.append(
                        "state invariant violation: ユーザー仕様では state に必須のsentinel/blank markerが含まれる必要がありますが、"
                        "traceback は marker lookup が失敗しており、test fixture または goal/start が状態空間外の値です。"
                        "固定fixtureの値をruntimeが補完するのではなく、ユーザー仕様から導ける状態ドメインに従い、"
                        "必須markerを含む平坦なgoal/startへ修正してください。"
                    )
                if "'>' not supported between instances of 'tuple' and 'list'" in latest_unittest_output_for_triage:
                    unittest_repair_hints.append(
                        "state type invariant violation: solver/search/solvability_check が受け取る state は平坦な値列でなければなりません。"
                        "near-goal fixture/helper が (state, actions) のような複合tupleを返し、testがそれ全体を start として渡すと、"
                        "state内にtuple/listが混入して比較やheuristicが壊れます。"
                        "fixtureの公式戻り値をstateだけにするか、test側で state, _ = fixture(...) と分解してから solver に渡してください。"
                    )
                if "AssertionError: False is not true" in latest_unittest_output_for_triage and "final_verifier" in latest_unittest_output_for_triage:
                    unittest_repair_hints.append(
                        "final_verifier invariant: non-empty action列をgoal状態から開始すると、通常はgoalから離れるため goal到達assert True にはなりません。"
                        "valid replay testは、そのaction列を適用したときにgoalへ到達するstart stateから始めるか、"
                        "goalからの非空actionはFalse期待にしてください。"
                    )
                unittest_repair_hints.append(
                    "state_space_searchのtestで『valid action』をハードコードする場合、そのactionは同じ初期stateに対してlegal_move_validatorで合法でなければなりません。"
                    "合法手契約と矛盾する期待値なら、実装を歪めずtest fixtureを修正してください。"
                )
                if state_space_test_fixture_repair_mode:
                    unittest_repair_hints.append(
                        "state_space_searchのtracebackがtest artifact内のassertだけを指しています。"
                        "次はimplementationを大きく書き換えず、test fixtureのstart/action/expected/unsolvable_caseが"
                        "同じstate/action/goal契約から導けるかを優先して修正してください。"
                    )
                if state_space_fixture_value_impl_blocked_paths:
                    unittest_repair_hints.append(
                        "state_space_searchのfixture値疑いがあり、implementation側への直前編集は無変更または無一致でした。"
                        "分析でtest fixtureが矛盾していると判断した場合、次のtool targetもimplementationではなく"
                        "読了済みtests/test_*.pyにしてください。"
                    )
                elif state_space_test_impl_contract_suspected:
                    unittest_repair_hints.append(
                        "state_space_searchのtest assert失敗ですが、assertRaises / solve / search / verify / solvable / legal / goal "
                        "など公開API契約に関わる失敗、または複数FAILが含まれます。"
                        "test fixtureだけに閉じず、implementationのstate/action/goal/solvability/final_verifier契約も照合してください。"
                    )
                if latest_unittest_timed_out:
                    unittest_repair_hints.append(
                        "state_space_search unittest が command_timeout になっています。"
                        "random/shuffle/full-search fixtureを使うtestは広すぎます。"
                        "次の修復では deterministic near-goal fixture（短い合法遷移で導出できるstart state）に縮小し、"
                        "solver/searchを1回だけ呼び、返ったaction列をfinal_verifier/legal replayで検証してください。"
                    )
                    unittest_repair_hints.append(
                        "PLAN_REVISION中なら、同じrun_testだけのPlanRecordを再提出してはいけません。"
                        "まず tests/test_*.py または探索境界を修正するedit WorkUnitを入れ、"
                        "その後に python3 -m unittest discover -s tests を実行するPlanRecordへ改訂してください。"
                    )

        latest_edit_match_failure_recovery = self._latest_edit_match_failure_recovery(steps=steps, path=latest_impl_path) if latest_impl_path else {}
        if latest_impl_path and phase == "implementation_present_needs_semantic_review" and latest_edit_match_failure_recovery:
            if int(latest_edit_match_failure_recovery.get("match_failures_since_success") or 0) >= 2:
                allowed_next_actions = [f"write_file {latest_impl_path}"]
            elif not bool(latest_edit_match_failure_recovery.get("read_after_failure")):
                allowed_next_actions = [f"read_file {latest_impl_path} once"]
            elif not bool(latest_edit_match_failure_recovery.get("successful_edit_after_failure")):
                failed_old_chars = int(latest_edit_match_failure_recovery.get("failed_old_text_chars") or 0)
                failed_new_chars = int(latest_edit_match_failure_recovery.get("failed_new_text_chars") or 0)
                failed_replace_was_large = (
                    failed_old_chars >= 800
                    or failed_new_chars >= 800
                    or failed_old_chars + failed_new_chars >= 1200
                )
                allowed_next_actions = (
                    [f"write_file {latest_impl_path}"]
                    if failed_replace_was_large
                    else [f"replace_text {latest_impl_path}", f"write_file {latest_impl_path}"]
                )

        return {
            "applicable": True,
            "phase": phase,
            "route_name": "",
            "route_phase": "",
            "contract": contract,
            "contract_state": "satisfied" if phase == "external_contract_satisfied" else "incomplete",
            "missing_requirements": missing_requirements,
            "allowed_next_actions": allowed_next_actions,
            "route_allowed_next_actions": [],
            "route_repair_paths": [],
            "route_read_consumed": False,
            "route_read_recovery_pending": False,
            "superseded_by_route": [],
            "implementation_paths": implementation_paths,
            "test_paths": test_paths,
            "latest_implementation_path": latest_impl_path,
            "latest_test_path": latest_test_path,
            "placeholder_present": placeholder_present,
            "implementation_source_issues": implementation_source_issues,
            "implementation_source_repair_hints": implementation_source_repair_hints,
            "implementation_repair_strategy": implementation_repair_strategy,
            "implementation_targeted_edit_preferred": implementation_targeted_edit_preferred,
            "implementation_read_consumed": implementation_read_consumed,
            "repo_map_excerpt": repo_map_excerpt,
            "semantic_review_requires_revision": semantic_requires_revision,
            "semantic_repair_target": semantic_repair_target,
            "semantic_test_repair_paths": semantic_test_repair_paths,
            "semantic_repair_read_consumed_paths": semantic_repair_read_consumed_paths,
            "semantic_repair_unread_paths": semantic_repair_unread_paths,
            "semantic_review_issues": latest_semantic_issues,
            "semantic_review_excerpt": latest_semantic_review_text[:900],
            "semantic_review_source": str(latest_semantic_review_details.get("review_source") or ""),
            "test_source_issues": test_source_issues,
            "latest_llm_output_issue": latest_llm_output_issue,
            "implementation_generation_repetitive_output": implementation_generation_repetitive_output,
            "test_generation_repetitive_output": test_generation_repetitive_output,
            "repair_generation_repetitive_output": repair_generation_repetitive_output,
            "unittest_run": bool(evidence.get("unittest_run")),
            "unittest_passed": bool(evidence.get("unittest_passed")),
            "successful_unittest_run_count": successful_unittest_count,
            "external_audit_passed": successful_unittest_count >= 2,
            "latest_unittest_failed": latest_unittest_result is not None and not bool(latest_unittest_result.get("ok")),
            "latest_unittest_timed_out": latest_unittest_timed_out,
            "latest_unittest_missing_tests_artifact": latest_unittest_missing_tests_artifact,
            "latest_unittest_failure_type": (
                "missing_tests_artifact"
                if latest_unittest_missing_tests_artifact
                else (
                    "external_audit_failed_after_previous_success"
                    if latest_unittest_result is not None and not bool(latest_unittest_result.get("ok"))
                    and successful_unittest_count > 0
                    else (
                        (latest_unittest_tool_failure_type or "unittest_failed")
                        if latest_unittest_result is not None and not bool(latest_unittest_result.get("ok"))
                        else ""
                    )
                )
            ),
            "latest_unittest_stderr_excerpt": str((latest_unittest_result or {}).get("stderr") or "")[-1200:],
            "latest_unittest_stdout_excerpt": str((latest_unittest_result or {}).get("stdout") or "")[-1200:],
            "latest_unittest_output_excerpt": self._review_file_excerpt(
                (
                    str((latest_unittest_result or {}).get("stderr") or "")
                    or str((latest_unittest_result or {}).get("stdout") or "")
                ),
                head_chars=1200,
                tail_chars=1200,
            ),
            "latest_unittest_failed_paths": latest_failed_unittest_paths,
            "state_space_test_fixture_repair_mode": state_space_test_fixture_repair_mode,
            "state_space_test_fixture_value_suspected": state_space_test_fixture_value_suspected,
            "state_space_test_impl_contract_suspected": state_space_test_impl_contract_suspected,
            "repeated_unittest_failure_signature": repeated_unittest_failure_signature,
            "latest_unittest_failure_signature": latest_unittest_failure_signature,
            "unittest_repair_hints": unittest_repair_hints,
            "latest_unittest_missing_import": latest_unittest_missing_import,
            "latest_edit_blocked": self._latest_edit_blocked_details(session_id=session_id),
            "previous_same_unittest_failure_index": previous_same_unittest_failure_index,
            "latest_failed_unittest_index": latest_failed_unittest_index,
            "latest_edit_after_failed_unittest_index": latest_edit_after_failed_unittest,
            "successful_edit_after_failed_unittest": latest_edit_after_failed_unittest > latest_failed_unittest_index,
            "same_signature_nonreducing_edit_paths": same_signature_nonreducing_edit_paths,
            "same_signature_read_paths": same_signature_read_paths,
            "latest_edit_match_failure_recovery": latest_edit_match_failure_recovery,
            "failed_unittest_recovery_read_consumed": failed_unittest_recovery_read_consumed,
            "failed_unittest_recovery_read_paths": failed_unittest_recovery_read_paths,
            "failed_unittest_recovery_read_consumed_paths": sorted(latest_read_paths_after_failed_unittest),
            "failed_unittest_recovery_editable_paths": failed_unittest_recovery_editable_paths,
            "state_space_no_match_exact_replace_paths": state_space_no_match_exact_replace_paths,
            "state_space_fixture_value_impl_blocked_paths": state_space_fixture_value_impl_blocked_paths,
            "failed_unittest_no_match_write_only_paths": failed_unittest_no_match_write_only_paths,
            "failed_unittest_repeated_noop_paths": failed_unittest_repeated_noop_paths,
            "failed_unittest_noop_alternate_paths": failed_unittest_noop_alternate_paths,
            "failed_unittest_noop_blocked_paths": failed_unittest_noop_blocked_paths,
            **plan_context,
        }

    def _blocked_failed_unittest_replace_paths(self, *, session_id: str | None) -> list[str]:
        if not session_id:
            return []
        path = self.paths.session_events_path(session_id)
        if not path.exists():
            return []
        paths: list[str] = []
        for event in read_jsonl(path, limit=5000):
            if str(event.get("type") or "") != "system_note":
                continue
            if str(event.get("code") or "") != "implementation_task_progress_blocked":
                continue
            details = event.get("details") if isinstance(event.get("details"), dict) else {}
            reason_code = str(details.get("reason_code") or event.get("reason_code") or "")
            if reason_code not in {
                "implementation_task_failed_unittest_blocks_unmatched_replace_text",
                "implementation_task_failed_unittest_blocks_broad_replace_text",
                "implementation_task_failed_unittest_blocks_block_header_replace_text",
            }:
                continue
            if str(details.get("phase") or "") != "unittest_failed_needs_fix":
                continue
            blocked_path = str(details.get("path") or "").replace("\\", "/")
            if blocked_path and blocked_path not in paths:
                paths.append(blocked_path)
        return paths

    def _latest_edit_blocked_details(self, *, session_id: str | None) -> dict[str, Any]:
        if not session_id:
            return {}
        path = self.paths.session_events_path(session_id)
        if not path.exists():
            return {}
        latest: dict[str, Any] = {}
        same_reason_count = 0
        latest_key: tuple[str, str, str] | None = None
        events = read_jsonl(path, limit=5000)
        for event in events:
            if str(event.get("type") or "") != "system_note":
                continue
            if str(event.get("code") or "") != "edit_blocked":
                continue
            details = event.get("details") if isinstance(event.get("details"), dict) else {}
            reason_code = str(event.get("reason_code") or details.get("reason_code") or "")
            blocked_tool = str(details.get("blocked_tool") or "")
            blocked_path = str(details.get("path") or "").replace("\\", "/")
            key = (reason_code, blocked_tool, blocked_path)
            if latest_key is None or key != latest_key:
                latest_key = key
                same_reason_count = 1
            else:
                same_reason_count += 1
            latest = {
                "reason_code": reason_code,
                "blocked_tool": blocked_tool,
                "path": blocked_path,
                "message": str(event.get("content") or ""),
                "suggested_fix": str(details.get("suggested_fix") or ""),
                "next_required_action": str(details.get("next_required_action") or ""),
                "same_reason_count": same_reason_count,
            }
        return latest

    def _implementation_contract_final_answer(
        self,
        *,
        user_message: str,
        steps: list[dict[str, Any]],
        session_id: str,
        turn_workspace: Path,
    ) -> str | None:
        """Synthesize a final answer once implementation task evidence is complete."""
        state = self._implementation_task_progress_state(
            user_message=user_message,
            steps=steps,
            session_id=session_id,
            turn_workspace=turn_workspace,
        )
        if state.get("phase") != "external_contract_satisfied":
            return None
        latest_unittest: dict[str, Any] | None = None
        for step in reversed(steps):
            if str(step.get("tool_name") or "") != "run_command":
                continue
            result = step.get("tool_result") if isinstance(step.get("tool_result"), dict) else {}
            if _unittest_command_is_acceptance(str(result.get("command") or "")) and bool(result.get("ok")):
                latest_unittest = result
                break
        if latest_unittest is None:
            return None

        implementation_paths = [str(path) for path in state.get("implementation_paths") or [] if str(path).strip()]
        test_paths = [str(path) for path in state.get("test_paths") or [] if str(path).strip()]
        command = str(latest_unittest.get("command") or "python3 -m unittest discover -s tests").strip()
        output = str(latest_unittest.get("stderr") or latest_unittest.get("stdout") or "OK")
        output_preview = self._output_preview(output, max_lines=4, max_chars=400)
        lines = [
            "実装タスクは完了条件を満たしました。",
            "",
            "成果物:",
        ]
        lines.extend(f"- {path}" for path in implementation_paths)
        lines.extend(f"- {path}" for path in test_paths)
        lines.extend(["", "検証:", f"- {command}: 成功"])
        if output_preview:
            lines.append(f"- 結果: {output_preview}")
        lines.extend(
            [
                "",
                "契約状態:",
                "- 実装ファイル作成: satisfied",
                "- 意味のあるunittest作成: satisfied",
                "- unittest実行成功: satisfied",
                "- 外部audit成功: satisfied",
            ]
        )
        return "\n".join(lines)

    def _extract_unittest_failure_paths(self, *, output: str, turn_workspace: Path | None) -> list[str]:
        """Return workspace-relative Python file paths named by unittest tracebacks."""

        if not output.strip() or turn_workspace is None:
            return []
        workspace = Path(turn_workspace).resolve()
        paths: list[str] = []
        seen: set[str] = set()
        for match in re.finditer(r'File "([^"]+\.py)"', output):
            raw = match.group(1)
            candidate = Path(raw)
            if not candidate.is_absolute():
                candidate = workspace / candidate
            try:
                relative = candidate.resolve().relative_to(workspace)
            except (OSError, ValueError):
                continue
            rel_text = str(relative).replace("\\", "/")
            if rel_text not in seen:
                seen.add(rel_text)
                paths.append(rel_text)
        return paths

    def _unittest_missing_import_details(self, *, output: str) -> dict[str, str]:
        """Extract the actionable symbol/module from unittest import errors."""

        text = str(output or "")
        if not text.strip():
            return {}
        missing_match = re.search(
            r"ImportError:\s+cannot import name ['\"]([^'\"]+)['\"] from ['\"]([^'\"]+)['\"]",
            text,
        )
        if not missing_match:
            return {}
        source_file = ""
        source_line = ""
        file_matches = list(re.finditer(r'File "([^"]+\.py)", line (\d+), in ([^\n]+)', text))
        if file_matches:
            source_file = str(Path(file_matches[-1].group(1)).name)
            source_line = file_matches[-1].group(2)
        return {
            "name": missing_match.group(1),
            "module": missing_match.group(2),
            "source_file": source_file,
            "source_line": source_line,
        }

    def _implementation_task_effective_phase(self, *, fallback_phase: str, state: dict[str, Any]) -> str:
        if fallback_phase in {"PLANNING_REQUIRED", "PLAN_REVISION"}:
            if bool(state.get("applicable")) and str(state.get("contract_state") or "") != "satisfied":
                phase = str(state.get("phase") or "").strip()
                has_progress_material = bool(
                    state.get("implementation_paths")
                    or state.get("test_paths")
                    or state.get("unittest_run")
                )
                if has_progress_material and phase in {
                    "implementation_present_needs_semantic_review",
                    "tests_missing",
                    "tests_present_needs_semantic_review",
                    "unittest_not_run",
                    "unittest_failed_needs_fix",
                }:
                    return f"IMPLEMENTATION_TASK_PROGRESS:{phase}"
            return fallback_phase
        if fallback_phase == "PLAN_EXECUTION":
            current = self.frame_manager.current_frame()
            if current is not None and current.parent_frame_id is not None:
                return fallback_phase
        if not bool(state.get("applicable")):
            return fallback_phase
        if str(state.get("contract_state") or "") == "satisfied":
            return fallback_phase
        route_phase = str(state.get("route_phase") or "").strip()
        if route_phase and route_phase != "external_contract_satisfied":
            return f"IMPLEMENTATION_TASK_PROGRESS:{route_phase}"
        phase = str(state.get("phase") or "").strip()
        if phase and phase not in {"not_applicable", "external_contract_satisfied"}:
            return f"IMPLEMENTATION_TASK_PROGRESS:{phase}"
        return fallback_phase

    def _implementation_task_progress_is_incomplete(self, state: dict[str, Any]) -> bool:
        if not bool(state.get("applicable")):
            return False
        if str(state.get("contract_state") or "") == "satisfied":
            return False
        phase = str(state.get("phase") or "").strip()
        return bool(phase and phase not in {"not_applicable", "external_contract_satisfied"})

    def _implementation_task_progress_visible_allowed_actions(self, state: dict[str, Any]) -> list[str]:
        allowed = [str(item) for item in state.get("allowed_next_actions") or [] if str(item).strip()]
        route_name = str(state.get("route_name") or "")
        route_allowed = [str(item) for item in state.get("route_allowed_next_actions") or [] if str(item).strip()]
        return route_allowed if route_name and route_allowed else allowed




    def _implementation_task_prompt_steps(
        self,
        *,
        steps: list[dict[str, Any]],
        state: dict[str, Any],
    ) -> list[dict[str, Any]]:
        phase = str(state.get("phase") or "")
        if phase == "unittest_failed_needs_fix":
            latest_read_indexes: set[int] = set()
            seen_read_paths: set[str] = set()
            for index in range(len(steps) - 1, -1, -1):
                step = steps[index]
                if str(step.get("tool_name") or "") != "read_file":
                    continue
                result = step.get("tool_result") if isinstance(step.get("tool_result"), dict) else {}
                if not bool(result.get("ok")):
                    continue
                step_args = step.get("tool_args") if isinstance(step.get("tool_args"), dict) else {}
                path = str(result.get("path") or step_args.get("path") or "").replace("\\", "/")
                if not path or path in seen_read_paths:
                    continue
                latest_read_indexes.add(index)
                seen_read_paths.add(path)
                if len(latest_read_indexes) >= 2:
                    break
            if not latest_read_indexes:
                return []

            compacted_reads: list[dict[str, Any]] = []
            for index, step in enumerate(steps):
                if index not in latest_read_indexes:
                    continue
                item = {
                    "tool_name": step.get("tool_name"),
                    "tool_args": dict(step.get("tool_args") or {})
                    if isinstance(step.get("tool_args"), dict)
                    else step.get("tool_args"),
                }
                result = (
                    dict(step.get("tool_result") or {})
                    if isinstance(step.get("tool_result"), dict)
                    else step.get("tool_result")
                )
                if isinstance(result, dict):
                    result_content = str(result.get("content") or "")
                    if len(result_content) > 2500:
                        result["content"] = self._compact_context_text(result_content, limit=2500)
                item["tool_result"] = result
                compacted_reads.append(item)
            return compacted_reads

        latest_read_content_limit = 2500 if phase == "unittest_failed_needs_fix" else 6000
        latest_read_indexes: set[int] = set()
        seen_read_paths: set[str] = set()
        for index in range(len(steps) - 1, -1, -1):
            step = steps[index]
            if str(step.get("tool_name") or "") != "read_file":
                continue
            result = step.get("tool_result") if isinstance(step.get("tool_result"), dict) else {}
            if not bool(result.get("ok")):
                continue
            step_args = step.get("tool_args") if isinstance(step.get("tool_args"), dict) else {}
            path = str(result.get("path") or step_args.get("path") or "").replace("\\", "/")
            if not path or path in seen_read_paths:
                continue
            latest_read_indexes.add(index)
            seen_read_paths.add(path)
            if len(latest_read_indexes) >= 2:
                break

        compacted: list[dict[str, Any]] = []
        for index, step in enumerate(steps):
            item = {
                "tool_name": step.get("tool_name"),
                "tool_args": dict(step.get("tool_args") or {})
                if isinstance(step.get("tool_args"), dict)
                else step.get("tool_args"),
            }
            result = (
                dict(step.get("tool_result") or {})
                if isinstance(step.get("tool_result"), dict)
                else step.get("tool_result")
            )
            args = item.get("tool_args") if isinstance(item.get("tool_args"), dict) else {}
            if isinstance(args, dict) and "content" in args:
                content = str(args.get("content") or "")
                args["content"] = f"<omitted {len(content)} chars from prior {step.get('tool_name')} content>"
            if isinstance(result, dict):
                result_content = str(result.get("content") or "")
                if result_content and index not in latest_read_indexes:
                    result["content"] = f"<omitted {len(result_content)} chars from earlier read_file content>"
                elif len(result_content) > latest_read_content_limit:
                    result["content"] = self._compact_context_text(result_content, limit=latest_read_content_limit)
            item["tool_result"] = result
            compacted.append(item)
        return compacted

    def _implementation_task_prompt_events(
        self,
        *,
        recent_events: list[dict[str, Any]],
        state: dict[str, Any],
    ) -> list[dict[str, Any]]:
        phase = str(state.get("phase") or "")
        if phase == "unittest_failed_needs_fix":
            selected_indexes: set[int] = set()
            seen_read_paths: set[str] = set()
            latest_run_command_index: int | None = None
            for index in range(len(recent_events) - 1, -1, -1):
                event = recent_events[index]
                if str(event.get("type") or "") != "tool_result":
                    continue
                tool_name = str(event.get("tool_name") or "")
                if tool_name == "run_command" and latest_run_command_index is None:
                    latest_run_command_index = index
                    selected_indexes.add(index)
                    continue
                if tool_name != "read_file":
                    continue
                try:
                    payload = json.loads(str(event.get("content") or "{}"))
                except json.JSONDecodeError:
                    payload = {}
                path = str(payload.get("path") or "").replace("\\", "/")
                if not path or path in seen_read_paths:
                    continue
                seen_read_paths.add(path)
                selected_indexes.add(index)
                if len(seen_read_paths) >= 2 and latest_run_command_index is not None:
                    break
            return [event for index, event in enumerate(recent_events) if index in selected_indexes]
        return recent_events

    def _implementation_task_should_suppress_frame_operations(self, state: dict[str, Any]) -> bool:
        return self._implementation_task_progress_is_incomplete(state)

    def _implementation_task_schema_tool_names(self, state: dict[str, Any]) -> list[str] | None:
        if not self._implementation_task_progress_is_incomplete(state):
            return None
        route_allowed = [
            str(item).strip()
            for item in state.get("route_allowed_next_actions") or []
            if str(item).strip()
        ]
        generic_allowed = [
            str(item).strip()
            for item in state.get("allowed_next_actions") or []
            if str(item).strip()
        ]
        allowed_actions = route_allowed or generic_allowed
        tool_names: list[str] = []
        for item in allowed_actions:
            tool_name = item.split(maxsplit=1)[0]
            if tool_name == "auto_finish" or tool_name not in TOOL_ACTION_NAMES:
                continue
            if tool_name not in tool_names:
                tool_names.append(tool_name)
        if not tool_names:
            return None
        return tool_names

    def _implementation_task_frame_action_block(
        self,
        *,
        tool_name: str,
        tool_args: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any] | None:
        if tool_name == "create_plan":
            return None
        if tool_name == "return_to_parent" and self._child_frame_has_tool_evidence():
            return None
        if tool_name not in FRAME_OPERATION_TOOL_SET:
            return None
        if not self._implementation_task_progress_is_incomplete(state):
            return None
        allowed = self._implementation_task_progress_visible_allowed_actions(state)
        path = str(tool_args.get("path") or "").replace("\\", "/")
        phase = str(state.get("phase") or "")
        route_phase = str(state.get("route_phase") or "")
        return {
            "reason_code": "implementation_task_phase_requires_direct_action",
            "phase": phase,
            "route_phase": route_phase,
            "path": path,
            "message": (
                "implementation task progress が未完了です。現在の phase では frame操作ではなく、"
                "allowed_next_actions の直接ツール操作だけを許可します。"
            ),
            "allowed_next_actions": allowed,
            "suggested_fix": "decompose_tasks/open_child_frame/return_to_parent を使わず、allowed_next_actions の直接アクションを実行してください。",
            "state": state,
        }

    def _implementation_task_progress_prompt(self, state: dict[str, Any]) -> str:
        if not bool(state.get("applicable")):
            return ""
        parts = [
            "実装タスク進行契約:",
            f"- phase: {state.get('phase')}",
            f"- contract_state: {state.get('contract_state')}",
            "- missing_requirements: " + json.dumps(state.get("missing_requirements") or [], ensure_ascii=False),
            "- allowed_next_actions: " + json.dumps(state.get("allowed_next_actions") or [], ensure_ascii=False),
        ]
        implementation_paths = state.get("implementation_paths") or []
        test_paths = state.get("test_paths") or []
        if implementation_paths:
            parts.append("- implementation_paths: " + json.dumps(implementation_paths, ensure_ascii=False))
        if test_paths:
            parts.append("- test_paths: " + json.dumps(test_paths, ensure_ascii=False))
        plan_obligations = [str(item) for item in state.get("plan_execution_obligations") or [] if str(item).strip()]
        if state.get("plan_strategy") or plan_obligations:
            parts.append("PlanRecord実行契約:")
            if state.get("plan_id"):
                parts.append(f"- plan_id: {state.get('plan_id')}")
            if state.get("plan_strategy"):
                parts.append(f"- strategy: {state.get('plan_strategy')}")
            if state.get("plan_verification_contract"):
                parts.append(
                    "- verification_contract: "
                    + json.dumps(state.get("plan_verification_contract") or [], ensure_ascii=False)
                )
            work_units_summary = state.get("plan_work_units_summary") or []
            if work_units_summary:
                parts.append(
                    "- work_units: "
                    + self._compact_context_text(json.dumps(work_units_summary, ensure_ascii=False), limit=900)
                )
            if plan_obligations:
                parts.append("PlanRecord由来の未達にしてはいけない実行義務:")
                for obligation in plan_obligations[:6]:
                    parts.append(f"- {obligation}")
                parts.append(
                    "実装・テスト・修復はこのPlanRecord義務を満たしてください。"
                    "unittestが通っても、manual UIだけ、曖昧な'works correctly'だけ、"
                    "またはlegal action replayのないテストはfinish証拠として不足です。"
                )
        repo_map_excerpt = str(state.get("repo_map_excerpt") or "").strip()
        if repo_map_excerpt:
            parts.append("read_fileを減らすためのworkspace索引:")
            parts.append(repo_map_excerpt)
        if state.get("implementation_source_issues"):
            parts.append("実装未達:")
            for issue in list(state.get("implementation_source_issues") or [])[:6]:
                parts.append(f"- {issue}")
            repair_strategy = str(state.get("implementation_repair_strategy") or "").strip()
            if repair_strategy:
                parts.append("修復戦略:")
                parts.append(f"- {repair_strategy}")
            repair_hints = [
                item
                for item in state.get("implementation_source_repair_hints") or []
                if isinstance(item, dict)
            ]
            if repair_hints:
                parts.append("残っている具体修復箇所:")
                for hint in repair_hints[:5]:
                    line = int(hint.get("line") or 0)
                    current = str(hint.get("current_text") or "").strip()
                    reason = str(hint.get("reason") or "").strip()
                    suggested_action = str(hint.get("suggested_action") or "").strip()
                    suggested_old = str(hint.get("suggested_old_text") or "").strip()
                    suggested_strategy = str(hint.get("suggested_strategy") or "").strip()
                    suggested = str(hint.get("suggested_new_text") or "").strip()
                    prefix = f"line {line}" if line > 0 else "source context"
                    parts.append(f"- {prefix}: {current}")
                    if reason:
                        parts.append(f"  reason: {reason}")
                    if suggested_action:
                        parts.append(f"  suggested_action: {suggested_action}")
                    if suggested_old:
                        parts.append(f"  suggested_old_text: {suggested_old}")
                    if suggested_strategy:
                        parts.append(f"  suggested_strategy: {suggested_strategy}")
                    if suggested:
                        parts.append(f"  suggested_new_text: {suggested}")
                if any(hint.get("suggested_action") == "append_top_level_wrappers" for hint in repair_hints):
                    parts.append(
                        "top-level wrapper修復では、提示された suggested_new_text だけを append_file してください。"
                    )
                    parts.append(
                        "既存class全体を巨大 replace_text で再生成せず、public API未達を減らすwrapper追加に限定してください。"
                    )
                if any(hint.get("suggested_action") == "add_state_space_replay_verifier" for hint in repair_hints):
                    parts.append(
                        "state_space_search final_verifier修復では、既存solver/search全体の再生成ではなく、"
                        "action列を initial_state から1手ずつ legal check -> transition -> goal check する "
                        "verify/replay/validate callable の追加に限定してください。"
                    )
                    parts.append(
                        "提示された suggested_new_text を、既存の合法手関数・遷移関数・goal表現に合わせて追加し、"
                        "solver/searchの戻り値をそのcallableで検証できる形にしてください。"
                    )
        if state.get("semantic_review_issues"):
            parts.append("semantic review 未達:")
            for issue in list(state.get("semantic_review_issues") or [])[:6]:
                parts.append(f"- {issue}")
        if state.get("test_source_issues"):
            parts.append("test artifact 未達:")
            for issue in list(state.get("test_source_issues") or [])[:6]:
                parts.append(f"- {issue}")
        latest_edit_blocked = state.get("latest_edit_blocked") if isinstance(state.get("latest_edit_blocked"), dict) else {}
        if latest_edit_blocked and str(latest_edit_blocked.get("path") or "").startswith("tests/"):
            reason_code = str(latest_edit_blocked.get("reason_code") or "").strip()
            message = str(latest_edit_blocked.get("message") or "").strip()
            suggested_fix = str(latest_edit_blocked.get("suggested_fix") or "").strip()
            next_required_action = str(latest_edit_blocked.get("next_required_action") or "").strip()
            same_reason_count = int(latest_edit_blocked.get("same_reason_count") or 0)
            parts.append("直近のtest artifact生成はruntimeに拒否されました:")
            if reason_code:
                parts.append(f"- reason_code: {reason_code}")
            if message:
                parts.append(f"- message: {self._compact_context_text(message, limit=700)}")
            if suggested_fix:
                parts.append(f"- suggested_fix: {suggested_fix}")
            if next_required_action:
                parts.append(f"- next_required_action: {next_required_action}")
            if same_reason_count >= 2:
                parts.append(
                    "- 同じtest生成拒否が反復しています。前回候補は保存されていません。"
                    "次の write_file は同じcandidateを少し言い換えず、拒否理由を消した完全な tests/test_*.py にしてください。"
                )
            if reason_code == "test_artifact_contract_incomplete":
                parts.append(
                    "- 同じliteral input/fixtureに対して solve/search の None と not None を同時に期待してはいけません。"
                    "解けるfixtureとno-solution fixtureを別入力に分け、各assertの期待を一貫させてください。"
                )
        if state.get("latest_unittest_failed"):
            parts.append("unittest失敗後の修正契約:")
            if str(state.get("latest_unittest_failure_type") or "") == "external_audit_failed_after_previous_success":
                parts.append(
                    "- 以前のunittest成功後に最新の検証が失敗しています。これは外部audit/regression失敗として扱い、"
                    "成功済み自己申告ではなく最新失敗のtracebackを正にしてください。"
                )
                parts.append(
                    f"- previous_successful_unittest_run_count: {int(state.get('successful_unittest_run_count') or 0)}"
                )
            parts.append("- traceback対象test/implementationを各1回だけread_fileできます。")
            parts.append("- read後は同じreadを繰り返さず、対象ファイルを小さいtargeted replace_textまたはwrite_fileで修正してください。")
            parts.append("- replace_textは現在sourceに一意一致する数行のold_textだけ許可します。長い関数やファイル全体のreplace_textはstream浪費とno_match反復になりやすいため禁止です。")
            parts.append("- 成功編集後だけ unittest 再実行に進めます。")
            if state.get("successful_edit_after_failed_unittest"):
                parts.append("成功編集後の唯一の次アクション:")
                parts.append("- 直近の failed unittest 後に少なくとも1つの対象ファイル編集が成功しています。")
                parts.append("- 失敗signatureが減ったか変わったかを観測するまで、追加の read_file / replace_text / write_file は禁止です。")
                if (
                    str(state.get("latest_unittest_failure_type") or "") == "command_timeout"
                    and state.get("repeated_unittest_failure_signature")
                ):
                    parts.append(
                        "- 直近の失敗は同一command_timeoutの再発です。全体unittest discoverをそのまま再実行せず、"
                        "特定module/test methodなどの狭いunittest targetでtimeout箇所を切り分けてください。"
                    )
                else:
                    parts.append("- 次は必ず `run_command python3 -m unittest discover -s tests` だけを実行してください。")
            if state.get("repair_generation_repetitive_output"):
                parts.append("直近のrepair生成は repetitive_output/stream_char_limit でJSON未完了になりました。次の修復はさらに小さくしてください。")
                parts.append("- replace_textを使う場合、old_textは失敗行に関係する1つの関数または数行だけ、new_textも同じ範囲だけにしてください。")
                parts.append("- ファイル全体、class全体、長いhelper群、説明コメントをold_text/new_textに入れてはいけません。")
                parts.append("- 失敗tracebackがtest期待値の誤りを示す場合は、実装を歪めず該当test methodだけを小さく修正してください。")
                parts.append("- 迷ったら、すでにread済みのtraceback対象から1箇所だけ選び、失敗signatureを変える最小編集にしてください。")
            repeated_noop_paths = [
                str(item).strip()
                for item in state.get("failed_unittest_repeated_noop_paths") or []
                if str(item).strip()
            ]
            noop_blocked_paths = [
                str(item).strip()
                for item in state.get("failed_unittest_noop_blocked_paths") or []
                if str(item).strip()
            ]
            noop_alternates = [
                str(item).strip()
                for item in state.get("failed_unittest_noop_alternate_paths") or []
                if str(item).strip()
            ]
            if repeated_noop_paths:
                parts.append("no_op edit反復の修復契約:")
                parts.append("- 直近の修復で、同じ対象に実差分のないreplace_text/write_fileが連続しています。")
                parts.append("- 同じcontent、同じold_text/new_text、コメントだけ・空白だけの再提出は禁止です。")
                if noop_blocked_paths:
                    parts.append("- 一時的に編集禁止の対象: " + json.dumps(noop_blocked_paths, ensure_ascii=False))
                if noop_alternates:
                    parts.append("- 次に照合・修正すべき別対象: " + json.dumps(noop_alternates, ensure_ascii=False))
                parts.append("- allowed_next_actionsから、別対象のread_fileまたは実差分を含む編集を1つだけ選んでください。")
            output_excerpt = str(
                state.get("latest_unittest_output_excerpt")
                or state.get("latest_unittest_stderr_excerpt")
                or state.get("latest_unittest_stdout_excerpt")
                or ""
            ).strip()
            if output_excerpt:
                parts.append("直近unittest stdout/stderr excerpt:")
                parts.append(output_excerpt[-1200:])
            if state.get("repeated_unittest_failure_signature"):
                signature = str(state.get("latest_unittest_failure_signature") or "").strip()
                edit_paths = [
                    str(item).strip()
                    for item in state.get("same_signature_nonreducing_edit_paths") or []
                    if str(item).strip()
                ]
                read_paths = [
                    str(item).strip()
                    for item in state.get("same_signature_read_paths") or []
                    if str(item).strip()
                ]
                parts.append("同一unittest failure signatureの非進捗:")
                parts.append("- 前回編集後もunittest failure signatureが同一です。直前の編集は失敗を減らしていません。")
                if signature:
                    parts.append(f"- latest_unittest_failure_signature: {signature}")
                if edit_paths:
                    parts.append("- nonreducing_edit_paths: " + json.dumps(edit_paths, ensure_ascii=False))
                    parts.append(
                        "- 非進捗だった対象への次の全面write_fileは許可しません。"
                        "同じ失敗を減らす小さいreplace_text、または別のtraceback対象ファイルの修正に絞ってください。"
                    )
                if read_paths:
                    parts.append("- already_read_for_this_signature: " + json.dumps(read_paths, ensure_ascii=False))
                parts.append("- 同じ内容のwrite_fileや同じread_fileの反復は禁止です。")
                parts.append("- 次のwrite_fileはtracebackの具体行と読んだsourceに基づき、失敗signatureを変える修正だけにしてください。")
            unittest_repair_hints = [
                str(item).strip()
                for item in state.get("unittest_repair_hints") or []
                if str(item).strip()
            ]
            if unittest_repair_hints:
                parts.append("unittest失敗triage:")
                for hint in unittest_repair_hints[:6]:
                    parts.append(f"- {hint}")
        phase = str(state.get("phase") or "")
        if phase == "implementation_missing":
            parts.append("次は実行可能なPython実装を write_file してください。tests/unittest/finishへ先に進めません。")
            parts.append("初期実装の受理条件:")
            parts.append("- まず小さくても完全に動く単一の .py 実装を書いてください。未完成chunk、後でappendする前提の骨組みは禁止です。")
            parts.append("- ユーザーがpackage構成を要求していない限り、最初は最小の単一moduleを優先してください。")
            parts.append("- 要求された公開関数名がある場合、class helperだけでなく同じfileのmodule-level defとして初回から定義してください。")
            parts.append("- 高度な内部構造より、要求仕様から直接導ける最小の完結アルゴリズムを優先してください。")
            parts.append("- 実装本文の中で設計を考え直すコメント、TODO、pass、後で埋める前提の分岐を書いてはいけません。迷ったら書く前により単純な完結アルゴリズムへ縮小してください。")
            parts.append("- 4000 bytesを超えそうなら、大きい設計を途中まで書くのではなく、より小さい完全実装に縮小してください。")
            if state.get("implementation_generation_repetitive_output"):
                parts.append("直近の初回実装生成は repetitive_output/stream_char_limit でJSON未完了になりました。次の write_file はさらに小さいライブラリ実装だけに縮小してください。")
                parts.append("- content は目安2500 bytes以下。unittest、demo main、random scramble、長い説明コメント、網羅的helperは入れないでください。")
                parts.append("- state_space_searchの場合は、state表現、legal_moves/apply_move、goal判定、solve/search、verify/replayの最小callableだけを書いてください。")
                parts.append("- tests/test_*.py と実行デモは、実装artifactが受理された後の別ステップで作成・実行します。")
        elif phase == "implementation_missing_needs_semantic_revision":
            parts.append("次はreject済み候補と同型でない、placeholder-freeの完全なPython実装を write_file してください。")
            parts.append("受理条件:")
            parts.append("- pass/TODO/ellipsis/NotImplementedError/placeholder-only return が0個であること。")
            parts.append("- 要求された公開関数がある場合、class methodだけでなくmodule-level defとして定義すること。")
            parts.append("- 全callableが実行可能な制御/データ処理を持つこと。骨組み、stub、後で実装するコメントは禁止です。")
            parts.append("- 前回と同じplaceholder構造を少し言い換えるのではなく、成果物契約を満たす別実装に切り替えること。")
            if state.get("implementation_generation_repetitive_output"):
                parts.append("直近の修正版初回実装生成は repetitive_output/stream_char_limit でした。次は既存案を長く再出力せず、未達を満たす最小の完全実装だけに縮小してください。")
        elif phase == "implementation_present_but_placeholder":
            parts.append("placeholderを具体実装に置き換えてからtestsへ進んでください。")
        elif phase == "implementation_present_needs_semantic_review":
            allowed_actions = [str(item) for item in state.get("allowed_next_actions") or [] if str(item).strip()]
            recovery = (
                state.get("latest_edit_match_failure_recovery")
                if isinstance(state.get("latest_edit_match_failure_recovery"), dict)
                else {}
            )
            no_match_after_read = bool(recovery) and bool(recovery.get("read_after_failure")) and not bool(
                recovery.get("successful_edit_after_failure")
            )
            if no_match_after_read and not any(action.startswith("replace_text ") for action in allowed_actions):
                parts.append("replace_text no_match後の修復契約:")
                parts.append("- 直近の replace_text は old_text が一致せず失敗しています。")
                parts.append("- recovery read は完了済みです。同じold_textや巨大old_textでreplace_textを再試行してはいけません。")
                parts.append("- allowed_next_actions が write_file のため、次は現在の契約未達を減らす完全なPythonソースを write_file してください。")
            elif no_match_after_read:
                parts.append("replace_text no_match後の修復契約:")
                parts.append("- recovery read は完了済みです。replace_textを使うなら、数行の一意に一致する小さいold_textだけにしてください。")
                parts.append("- 一意な小さいold_textを作れない場合は write_file で完全な有効ソースを書いてください。")
            if state.get("implementation_targeted_edit_preferred") and any(
                action.startswith("replace_text ") for action in allowed_actions
            ):
                parts.append(
                    "次は全体再生成ではなく、missing_requirementsを直接減らす小さい replace_text を優先してください。"
                )
                parts.append(
                    "同じ未達が残るwrite_file/full rewriteは、進捗ではなく同型反復として扱われます。"
                )
            elif state.get("implementation_targeted_edit_preferred") and any(
                action.startswith("write_file ") for action in allowed_actions
            ):
                parts.append(
                    "allowed_next_actions は write_file です。replace_text no_matchを繰り返さず、missing_requirementsを直接減らす完全なファイル内容を書いてください。"
                )
                parts.append(
                    "同じ未達が残るfull rewriteは、進捗ではなく同型反復として扱われます。"
                )
        elif phase == "tests_missing":
            parts.append("次は意味のある tests/test_*.py を作成してください。assertなし/pass-only testは完了証拠になりません。")
            if str(state.get("plan_strategy") or "") == "state_space_search":
                parts.append("state_space_search test fixture契約:")
                parts.append("- solver/search統合testは、短い合法transitionで導出できるnear-goal start stateに縮小してください。")
                parts.append("- 手書きの長いsolution列、任意に作ったsolvable/unsolvable盤面、または由来を説明できないfixtureを使わないでください。")
                parts.append("- solve/searchの戻り値は verify/replay/final_verifier で合法手として再生し、goal到達をassertしてください。")
                parts.append("- no-solution系はsolver全探索に投げず、solvability_check単体または明示境界付きの小さい検証にしてください。")
            if state.get("test_generation_repetitive_output"):
                parts.append("直近のtest生成は repetitive_output/stream_char_limit でJSON未完了になりました。次のtest write_fileは包括テストではなく最小verification testに縮小してください。")
                parts.append("- 目安は1 test class / 2-3 test methods / 1200 bytes前後です。全近傍、全ケース、長い表、説明コメントを列挙しないでください。")
                parts.append("- まずsolver/searchを1回呼び、返ったaction列をfinal_verifierまたはlegal replayで検証するtestを1つ書いてください。")
                parts.append("- 追加するならillegal actionが拒否されるtestを1つだけにしてください。網羅テストはunittest成功後の改善対象です。")
            if state.get("latest_unittest_missing_tests_artifact"):
                parts.append(
                    "直近のunittest失敗は tests ディレクトリ未作成が原因です。"
                    "同じ実装ファイルの再生成では解消しないため、次は tests/test_*.py を作成してください。"
                )
        elif phase == "tests_present_needs_semantic_review":
            parts.append("次は semantic review 未達を減らす test artifact 修正を行ってください。")
            parts.append("テストファイル全体の巨大 replace_text は禁止です。全面的に書き直す必要がある場合は write_file を使い、小さいassert/fixture差し替えだけ replace_text できます。")
            parts.append(
                "このphaseでactionableな編集対象は tests/test_*.py だけです。"
                "semantic review excerpt内にimplementation側の指摘が含まれていても、"
                "implementation editは次phaseで許可されるまで実行しないでください。"
            )
            review_excerpt = str(state.get("semantic_review_excerpt") or "").strip()
            if review_excerpt:
                parts.append("semantic review excerpt:")
                parts.append(review_excerpt[:900])
        elif phase == "unittest_not_run":
            parts.append("次は python3 -m unittest discover -s tests を実行してください。")
        elif phase == "external_audit_required":
            parts.append("内部unittestは成功済みです。finish前にcontroller側の外部auditとして同じunittestを再実行してください。")
        elif phase == "external_contract_satisfied":
            parts.append("実装、意味のあるtests、unittest成功、外部audit成功が揃っています。次はfinishしてください。")
        return "\n".join(parts)

    def _latest_edit_match_failure_recovery(
        self,
        *,
        steps: list[dict[str, Any]],
        path: str,
    ) -> dict[str, Any]:
        """Return the latest replace_text match-failure recovery state for path.

        A failed local edit must not roll artifact progress back. After the
        single recovery read, the next action is governed by the implementation
        task phase, not by "try another huge replacement".
        """

        normalized_path = str(path or "").strip()
        if not normalized_path:
            return {}
        edit_tools = {"write_file", "append_file", "replace_text"}
        match_failure_types = {"replace_text_no_match", "replace_text_ambiguous_match", "no_op_edit"}
        latest_failure_index = -1
        read_after_failure_index = -1
        successful_edit_after_failure_index = -1
        match_failures_since_success = 0
        failure_type = ""
        failed_old_text = ""
        failed_new_text = ""
        for index, step in enumerate(steps):
            tool = str(step.get("tool_name") or "")
            args = step.get("tool_args") if isinstance(step.get("tool_args"), dict) else {}
            result = step.get("tool_result") if isinstance(step.get("tool_result"), dict) else {}
            step_path = str(result.get("path") or args.get("path") or "").strip()
            if step_path != normalized_path:
                continue
            if tool in edit_tools:
                if bool(result.get("ok")):
                    match_failures_since_success = 0
                    if latest_failure_index >= 0:
                        successful_edit_after_failure_index = index
                    continue
                candidate_failure_type = str(result.get("failure_type") or "")
                if candidate_failure_type in match_failure_types:
                    match_failures_since_success += 1
                    latest_failure_index = index
                    read_after_failure_index = -1
                    successful_edit_after_failure_index = -1
                    failure_type = candidate_failure_type
                    failed_old_text = str(args.get("old_text") or "")
                    failed_new_text = str(args.get("new_text") or "")
            elif tool == "read_file" and bool(result.get("ok")) and latest_failure_index >= 0:
                read_after_failure_index = index
        if latest_failure_index < 0:
            return {}
        return {
            "path": normalized_path,
            "failure_type": failure_type,
            "failure_index": latest_failure_index,
            "read_after_failure": read_after_failure_index > latest_failure_index,
            "read_after_failure_index": read_after_failure_index,
            "successful_edit_after_failure": successful_edit_after_failure_index > latest_failure_index,
            "successful_edit_after_failure_index": successful_edit_after_failure_index,
            "match_failures_since_success": match_failures_since_success,
            "failed_old_text": failed_old_text,
            "failed_new_text": failed_new_text,
            "failed_old_text_chars": len(failed_old_text),
            "failed_new_text_chars": len(failed_new_text),
        }

    def _current_artifact_source_for_path(self, path: str, *, turn_workspace: Path | None = None) -> str:
        workspace = Path(turn_workspace).resolve() if turn_workspace is not None else self.execution_root.resolve()
        target = (workspace / str(path or "")).expanduser().resolve()
        try:
            target.relative_to(workspace)
        except ValueError:
            return ""
        if not target.exists() or not target.is_file():
            return ""
        return target.read_text(encoding="utf-8", errors="replace")

    def _issue_preview(self, issues: list[str], *, limit: int = 3) -> str:
        normalized = [str(issue).strip() for issue in issues if str(issue).strip()]
        if not normalized:
            return "なし"
        preview = " / ".join(normalized[:limit])
        remaining = len(normalized) - limit
        if remaining > 0:
            preview += f" / ... 他{remaining}件"
        return preview

    def _implementation_edit_is_broad_rewrite(
        self,
        *,
        tool_name: str,
        tool_args: dict[str, Any],
        current_source: str,
    ) -> bool:
        source_len = max(len(current_source or ""), 1)
        if tool_name == "write_file":
            content = str(tool_args.get("content") or "")
            return bool(current_source) and len(content) >= max(1200, int(source_len * 0.55))
        if tool_name == "replace_text":
            old_text = str(tool_args.get("old_text") or "")
            new_text = str(tool_args.get("new_text") or "")
            if self._replace_text_uses_block_header_only(old_text=old_text, new_text=new_text):
                return True
            return (
                len(old_text) >= max(1200, int(source_len * 0.45))
                or len(new_text) >= max(1200, int(source_len * 0.45))
                or (len(old_text) + len(new_text)) >= max(1800, int(source_len * 0.80))
            )
        return False

    def _replace_text_uses_block_header_only(self, *, old_text: str, new_text: str) -> bool:
        """Detect replacements that would insert a whole block after only its header."""
        old_lines = [line for line in str(old_text or "").replace("\r\n", "\n").split("\n") if line.strip()]
        new_lines = [line for line in str(new_text or "").replace("\r\n", "\n").split("\n") if line.strip()]
        if len(old_lines) != 1 or len(new_lines) <= 1:
            return False
        header = old_lines[0].strip()
        return bool(
            re.match(
                r"^(?:async\s+def|def|class|if|elif|else|for|async\s+for|while|try|except|finally|with|async\s+with|match|case)\b.*:\s*(?:#.*)?$",
                header,
            )
        )

    def _normalized_source_for_write_compare(self, source: str) -> str:
        return str(source or "").replace("\r\n", "\n").rstrip()

    def _python_ast_signature_for_write_compare(self, source: str) -> str:
        try:
            tree = ast.parse(str(source or ""))
        except SyntaxError:
            return ""
        return ast.dump(tree, include_attributes=False)

    def _write_file_nonreducing_reason(
        self,
        *,
        candidate_source: str,
        current_source: str,
        previous_nonreducing_sources: list[str] | None = None,
    ) -> str:
        normalized_candidate = self._normalized_source_for_write_compare(candidate_source)
        normalized_current = self._normalized_source_for_write_compare(current_source)
        if normalized_current and normalized_candidate == normalized_current:
            return "identical_current_source"
        candidate_ast = self._python_ast_signature_for_write_compare(candidate_source)
        current_ast = self._python_ast_signature_for_write_compare(current_source)
        if candidate_ast and current_ast and candidate_ast == current_ast:
            return "semantic_noop_ast"
        for previous_source in previous_nonreducing_sources or []:
            normalized_previous = self._normalized_source_for_write_compare(previous_source)
            if normalized_previous and normalized_candidate == normalized_previous:
                return "repeats_nonreducing_edit"
            previous_ast = self._python_ast_signature_for_write_compare(previous_source)
            if candidate_ast and previous_ast and candidate_ast == previous_ast:
                return "semantic_repeats_nonreducing_edit"
        return ""

    def _implementation_unittest_repair_target_paths(self, state: dict[str, Any]) -> list[str]:
        targets: list[str] = []
        for item in [
            *(state.get("failed_unittest_recovery_read_paths") or []),
            *(state.get("latest_unittest_failed_paths") or []),
            state.get("latest_test_path") or "",
            state.get("latest_implementation_path") or "",
        ]:
            normalized = str(item or "").replace("\\", "/").strip()
            if normalized and normalized not in targets:
                targets.append(normalized)
        return targets

    def _candidate_source_from_implementation_edit(
        self,
        *,
        tool_name: str,
        tool_args: dict[str, Any],
        current_source: str,
    ) -> str:
        if tool_name == "write_file":
            return str(tool_args.get("content") or "")
        if tool_name == "append_file":
            return str(current_source or "") + str(tool_args.get("content") or "")
        if tool_name != "replace_text":
            return ""
        old_text = str(tool_args.get("old_text") or "")
        new_text = str(tool_args.get("new_text") or "")
        if current_source and old_text and current_source.count(old_text) == 1:
            return current_source.replace(old_text, new_text, 1)
        if (
            new_text
            and len(new_text) >= max(1200, int(max(len(current_source or ""), 1) * 0.55))
            and any(marker in new_text for marker in ("class ", "def ", "import "))
        ):
            return new_text
        return ""








    def _candidate_implementation_issue_set(
        self,
        *,
        user_message: str,
        candidate_source: str,
        current_issue_set: set[str],
        implementation_path: str = "",
        turn_workspace: Path | None = None,
    ) -> set[str]:
        return {
            str(issue).strip()
            for issue in self._implementation_source_contract_issues(
                user_message=user_message,
                source=candidate_source,
            )
            if str(issue).strip()
        }

    def _implementation_source_contract_progress_metrics(
        self,
        *,
        user_message: str,
        source: str,
    ) -> dict[str, int]:
        """Return coarse generic violation counts for partial repair progress.

        Some issue strings intentionally group many equivalent observations
        into one human-facing contract violation. Blocking only on the issue
        string would reject useful targeted edits that reduce the concrete
        occurrences but do not eliminate the whole class yet.
        """

        text = str(source or "")
        metrics: dict[str, int] = {}

        requested_function_names = self._requested_top_level_function_names(user_message)
        if requested_function_names:
            try:
                tree = ast.parse(text)
            except SyntaxError:
                tree = None
            if tree is not None:
                top_level_functions = {
                    node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                }
                missing_count = sum(1 for name in requested_function_names if name not in top_level_functions)
                if missing_count:
                    metrics["missing_top_level_public_api"] = missing_count

        narrowing_count = self._python_source_narrowed_input_contract_score(
            user_message=user_message,
            source=text,
        )
        if narrowing_count:
            metrics["identifier_mapping_input_narrowing"] = narrowing_count

        if self._python_source_mutates_requested_input_collections(user_message=user_message, source=text):
            metrics["caller_input_mutation"] = 1
        if self._python_source_has_recursive_destructive_shared_state(text):
            metrics["recursive_destructive_state"] = 1
        if self._python_source_has_unthreaded_branch_local_recursive_state(text):
            metrics["unthreaded_branch_state"] = 1
        if self._python_source_is_escaped_source_literal(text):
            metrics["escaped_source_literal"] = 1
        return metrics

    def _implementation_contract_progress_reduced(
        self,
        *,
        user_message: str,
        current_source: str,
        candidate_source: str,
        current_issue_set: set[str],
        candidate_issue_set: set[str],
    ) -> bool:
        if candidate_issue_set < current_issue_set:
            return True
        if candidate_issue_set != current_issue_set:
            return False
        current_metrics = self._implementation_source_contract_progress_metrics(
            user_message=user_message,
            source=current_source,
        )
        candidate_metrics = self._implementation_source_contract_progress_metrics(
            user_message=user_message,
            source=candidate_source,
        )
        if not current_metrics:
            return False
        current_total = sum(current_metrics.values())
        candidate_total = sum(candidate_metrics.get(key, 0) for key in current_metrics)
        return candidate_total < current_total



    def _implementation_contract_block_signature(self, *, reason_code: str, issue_set: set[str]) -> str:
        normalized = sorted(str(issue).strip() for issue in issue_set if str(issue).strip())
        if not normalized:
            return ""
        payload = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
        digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]
        return f"{reason_code}:{digest}"

    def _implementation_contract_repair_strategy(
        self,
        *,
        user_message: str,
        current_issues: list[str],
        candidate_issues: list[str],
    ) -> str:
        issue_text = "\n".join(current_issues + candidate_issues)
        suggestions: list[str] = []
        if "recursive/backtracking実装が共有探索状態を destructive に更新" in issue_text:
            suggestions.append(
                "recursive/backtrackingの共有探索状態は、各branchで新しいnext/branch-local stateを渡すか、"
                "変更対象と復元対象が静的に追える対称なcover/uncoverにしてください。"
                "clear/difference_update/deleteなど広域破壊で復元根拠が曖昧な実装は未達のままです。"
            )
        if "branch-local 探索状態を作っていますが、再帰呼び出しへ渡していません" in issue_text:
            suggestions.append(
                "next_/branch_ stateを作るだけでは不十分です。作成した次状態を"
                "recursive callの引数としてthreadしてください。"
            )
        if "IDを保持するmapping入力" in issue_text:
            suggestions.append(
                "mapping入力のIDはcallerが渡した任意のhashable値として保持してください。"
                "public APIやsolution型をint/str/list indexへ固定せず、型注釈はAny/Hashable相当または未注釈にし、"
                "mapping.items()のkeyをそのまま返す小さいtargeted editで直してください。"
            )
        if "top-level functionがありません" in issue_text:
            suggestions.append(
                "要求APIがclass methodにだけ存在する場合は、全体再生成ではなく、"
                "既存classを呼ぶmodule-level wrapperを小さいappend/replaceで追加してください。"
                "wrapper追加だけでpublic API未達を減らせる場合、巨大replaceは無駄な反復です。"
            )
        return " ".join(suggestions)

    def _implementation_contract_prefers_targeted_edit(self, *, current_issues: list[str]) -> bool:
        """Return whether a source-contract issue should be repaired locally.

        Some generic contract failures are structural and usually need a new
        implementation strategy. Others, such as over-specific public type
        annotations, are local contract violations; allowing repeated full-file
        rewrites wastes model budget and tends to preserve the same mistake.
        """

        issue_text = "\n".join(str(issue) for issue in current_issues)
        targeted_markers = [
            "IDを保持するmapping入力",
            "top-level functionがありません",
        ]
        return any(marker in issue_text for marker in targeted_markers)

    def _implementation_edit_reduces_source_contract_issues(
        self,
        *,
        user_message: str,
        tool_name: str,
        tool_args: dict[str, Any],
        current_source: str,
        current_issues: list[str],
        implementation_path: str = "",
        turn_workspace: Path | None = None,
    ) -> bool:
        """Return whether a broad recovery edit observably reduces generic contract issues."""
        current_issue_set = {str(issue).strip() for issue in current_issues if str(issue).strip()}
        if not current_issue_set:
            return False
        candidate_source = self._candidate_source_from_implementation_edit(
            tool_name=tool_name,
            tool_args=tool_args,
            current_source=current_source,
        )
        if not candidate_source.strip():
            return False
        try:
            ast.parse(candidate_source)
        except SyntaxError:
            return False
        candidate_issue_set = self._candidate_implementation_issue_set(
            user_message=user_message,
            candidate_source=candidate_source,
            current_issue_set=current_issue_set,
            implementation_path=implementation_path,
            turn_workspace=turn_workspace,
        )
        return self._implementation_contract_progress_reduced(
            user_message=user_message,
            current_source=current_source,
            candidate_source=candidate_source,
            current_issue_set=current_issue_set,
            candidate_issue_set=candidate_issue_set,
        )

    def _implementation_rewrite_recovery_block(
        self,
        *,
        user_message: str,
        tool_name: str,
        tool_args: dict[str, Any],
        steps: list[dict[str, Any]],
        state: dict[str, Any],
        session_id: str,
        turn_workspace: Path | None = None,
    ) -> dict[str, Any] | None:
        path = str(tool_args.get("path") or "").replace("\\", "/")
        if tool_name not in {"write_file", "append_file", "replace_text"}:
            return None
        if not _artifact_path_is_python_implementation(path):
            return None
        recovery = self._latest_edit_match_failure_recovery(steps=steps, path=path)
        if not recovery or not recovery.get("read_after_failure") or recovery.get("successful_edit_after_failure"):
            return None
        phase = str(state.get("phase") or "")
        current_source = self._current_artifact_source_for_path(path, turn_workspace=turn_workspace)
        broad_rewrite = self._implementation_edit_is_broad_rewrite(
            tool_name=tool_name,
            tool_args=tool_args,
            current_source=current_source,
        )
        semantic_issues = [str(item) for item in state.get("semantic_review_issues") or [] if str(item).strip()]
        test_issue_paths = self._semantic_issue_test_repair_paths(
            semantic_issues=semantic_issues,
            test_paths=[str(item) for item in state.get("test_paths") or [] if str(item).strip()],
            latest_test_path=str(state.get("latest_test_path") or ""),
        )
        current_issues: list[str] = []
        candidate_issues: list[str] = []
        repair_strategy = ""
        if test_issue_paths:
            allowed = (
                [f"read_file {target} once" for target in test_issue_paths]
                + [f"replace_text {target}" for target in test_issue_paths]
                + [f"write_file {target}" for target in test_issue_paths]
            )
            message = (
                "replace_text old_text 不一致後の recovery read は完了しています。"
                "semantic review はテスト成果物の未達を指しているため、implementation rewrite では解消できません。"
                "対象の tests/test_*.py を修正してください。"
            )
            reason_code = "implementation_rewrite_recovery_blocked_test_semantic"
        elif phase == "tests_missing":
            allowed = list(state.get("allowed_next_actions") or ["write_file tests/test_*.py"])
            message = (
                "replace_text old_text 不一致後の recovery read は完了しています。"
                "old_text 不一致は実装を書き直せという意味ではありません。"
                "現在の未達条件は tests_written / meaningful_tests なので、tests/test_*.py 作成に進んでください。"
            )
            reason_code = "implementation_rewrite_recovery_blocked_tests_missing"
        elif phase == "unittest_not_run":
            allowed = list(state.get("allowed_next_actions") or ["run_command python3 -m unittest discover -s tests"])
            message = (
                "replace_text old_text 不一致後の recovery read は完了しています。"
                "実装とテストは揃っているため、同じ実装の巨大修正ではなく unittest 実行に進んでください。"
            )
            reason_code = "implementation_rewrite_recovery_blocked_unittest_not_run"
        elif phase == "unittest_failed_needs_fix":
            repair_targets = self._implementation_unittest_repair_target_paths(state)
            if repair_targets and path not in repair_targets:
                allowed = list(state.get("allowed_next_actions") or [])
                message = (
                    "replace_text old_text 不一致後の recovery read は完了しています。"
                    "このファイルは直近のunittest失敗から導出された修正対象ではありません。"
                    "traceback対象testまたは関連implementationだけを修正してください。"
                )
                reason_code = "implementation_rewrite_recovery_blocked_unittest_target"
            elif tool_name == "write_file":
                return None
            elif not broad_rewrite:
                return None
            else:
                allowed = list(state.get("allowed_next_actions") or [])
                message = (
                    "replace_text old_text 不一致後の recovery read は完了しています。"
                    "unittest失敗の修正は可能ですが、同じ巨大 replace_text を繰り返さず、"
                    "tracebackや失敗箇所に基づく小さい targeted edit、または一意に置換できない場合は"
                    "allowed_next_actions の write_file に進んでください。"
                )
                reason_code = "implementation_rewrite_recovery_blocked_broad_unittest_fix"
        elif phase == "implementation_present_needs_semantic_review":
            if not broad_rewrite:
                return None
            current_issues = [str(issue) for issue in state.get("implementation_source_issues") or [] if str(issue).strip()]
            if self._implementation_edit_reduces_source_contract_issues(
                user_message=user_message,
                tool_name=tool_name,
                tool_args=tool_args,
                current_source=current_source,
                current_issues=current_issues,
                implementation_path=path,
                turn_workspace=turn_workspace,
            ):
                return None
            candidate_issues: list[str] = []
            repair_strategy = ""
            current_issue_set = {str(issue).strip() for issue in current_issues if str(issue).strip()}
            candidate_source = self._candidate_source_from_implementation_edit(
                tool_name=tool_name,
                tool_args=tool_args,
                current_source=current_source,
            )
            if candidate_source.strip() and current_issue_set:
                try:
                    ast.parse(candidate_source)
                except SyntaxError as exc:
                    candidate_issues = [f"candidate_source_syntax_error: {exc.msg}"]
                else:
                    candidate_issues = sorted(
                        self._candidate_implementation_issue_set(
                            user_message=user_message,
                            candidate_source=candidate_source,
                            current_issue_set=current_issue_set,
                            implementation_path=path,
                            turn_workspace=turn_workspace,
                        )
                    )
                repair_strategy = self._implementation_contract_repair_strategy(
                    user_message=user_message,
                    current_issues=current_issues,
                    candidate_issues=candidate_issues,
                )
            prefers_targeted = self._implementation_contract_prefers_targeted_edit(current_issues=current_issues)
            allowed = list(state.get("allowed_next_actions") or [])
            if not allowed:
                allowed = ["replace_text <implementation>.py with small targeted old_text/new_text"]
            if tool_name == "replace_text" and not any(str(action).startswith("replace_text ") for action in allowed):
                message = (
                    "replace_text old_text 不一致後の recovery read は完了しています。"
                    "allowed_next_actions は write_file です。"
                    "同じ巨大 replace_text を再試行せず、missing_requirements を減らす完全な実装を書いてください。"
                )
                reason_code = "implementation_task_phase_blocks_repeated_or_large_replace_after_no_match"
            elif not any(str(action).startswith("replace_text ") for action in allowed) and any(
                str(action).startswith("write_file ") for action in allowed
            ):
                message = (
                    "replace_text old_text 不一致後の recovery read は完了しています。"
                    "allowed_next_actions は write_file です。"
                    "同じ巨大 replace_text / full rewrite を反復せず、missing_requirements を減らす完全な実装を書いてください。"
                )
                reason_code = "implementation_rewrite_recovery_blocked_broad_semantic_fix"
            else:
                allowed = (
                    [f"replace_text {path} with <=200 chars exact old_text"]
                    if prefers_targeted
                    else [f"write_file {path} only if it removes listed missing_requirements"]
                )
                message = (
                    "replace_text old_text 不一致後の recovery read は完了しています。"
                    "実装契約の未達修正は可能ですが、同じ巨大 replace_text / full rewrite を繰り返さず、"
                    "missing_requirements に対応する小さい targeted edit にしてください。"
                )
                reason_code = "implementation_rewrite_recovery_blocked_broad_semantic_fix"
        else:
            if not broad_rewrite:
                return None
            allowed = list(state.get("allowed_next_actions") or [])
            message = (
                "replace_text old_text 不一致後の recovery read は完了しています。"
                f"現在の progress phase は {phase} です。同じ巨大 replace_text / full rewrite を再試行せず、"
                "phase の allowed_next_actions に従ってください。"
            )
            reason_code = "implementation_rewrite_recovery_blocked"
        recent = self._recent_same_blocked_action_count(
            session_id=session_id,
            code="implementation_task_progress_blocked",
            reason_code=reason_code,
            blocked_tool=tool_name,
            path=path,
        )
        return {
            "reason_code": reason_code,
            "phase": phase,
            "path": path,
            "message": message,
            "allowed_next_actions": allowed,
            "suggested_fix": (
                "現在の progress phase と allowed_next_actions を優先してください。"
                "old_text 不一致後に同じ巨大 implementation rewrite を繰り返さないでください。"
                "提案後も残る未達と修復ヒントを読み、未達が減る編集だけを返してください。"
                + (f" {repair_strategy}" if repair_strategy else "")
            ),
            "state": state,
            "latest_edit_match_failure_recovery": recovery,
            "repair_hints": list(state.get("implementation_source_repair_hints") or []),
            "missing_requirements": current_issues if phase == "implementation_present_needs_semantic_review" else [],
            "candidate_missing_requirements": candidate_issues if phase == "implementation_present_needs_semantic_review" else [],
            "broad_rewrite": broad_rewrite,
            "terminal_failure": recent >= 2,
            "failure_type": "implementation_rewrite_recovery_loop" if recent >= 2 else "",
        }





    def _implementation_task_phase_action_block(
        self,
        *,
        user_message: str,
        tool_name: str,
        tool_args: dict[str, Any],
        steps: list[dict[str, Any]],
        session_id: str,
        turn_workspace: Path,
    ) -> dict[str, Any] | None:
        state = self._implementation_task_progress_state(
            user_message=user_message,
            steps=steps,
            session_id=session_id,
            turn_workspace=turn_workspace,
        )
        if not bool(state.get("applicable")):
            return None
        path = str(tool_args.get("path") or "").replace("\\", "/")
        phase = str(state.get("phase") or "")
        rewrite_recovery_block = self._implementation_rewrite_recovery_block(
            user_message=user_message,
            tool_name=tool_name,
            tool_args=tool_args,
            steps=steps,
            state=state,
            session_id=session_id,
            turn_workspace=turn_workspace,
        )
        if rewrite_recovery_block is not None:
            return rewrite_recovery_block
        repeated_semantic_issue = self._latest_repeated_semantic_issue(session_id=session_id)
        if repeated_semantic_issue is not None:
            return {
                "reason_code": "semantic_review_issue_repeated_after_revision",
                "phase": phase,
                "path": path,
                "message": (
                    "semantic implementation review の同じ未達項目が、修正試行後も再発しています。"
                    "このまま同じ制御経路を続けると同型失敗ループになるためrunを停止します。"
                ),
                "allowed_next_actions": ["start_new_run_with_revised_implementation_strategy"],
                "suggested_fix": "同じ設計を微修正せず、semantic_issuesを消す別実装戦略へ切り替えてください。",
                "state": state,
                "terminal_failure": True,
                "failure_type": "semantic_review_ignored",
                "repeated_semantic_issue": repeated_semantic_issue,
            }
        frame_action_block = self._implementation_task_frame_action_block(
            tool_name=tool_name,
            tool_args=tool_args,
            state=state,
        )
        if frame_action_block is not None:
            return frame_action_block
        if phase in {"implementation_missing", "implementation_missing_needs_semantic_revision"}:
            if tool_name == "write_file" and _artifact_path_is_python_implementation(path):
                return None
            existing_python_artifact = False
            if tool_name in {"append_file", "replace_text"} and _artifact_path_is_python_implementation(path):
                workspace = Path(turn_workspace).resolve() if turn_workspace is not None else self.execution_root.resolve()
                candidate = (workspace / path).resolve()
                try:
                    candidate.relative_to(workspace)
                except ValueError:
                    existing_python_artifact = False
                else:
                    existing_python_artifact = candidate.exists() and candidate.is_file()
            if existing_python_artifact:
                return None
            reason_code = "implementation_task_phase_requires_initial_implementation"
            message = (
                "実装artifactがまだ存在しません。実装+unittest契約では、コマンド実行、テスト作成、"
                "read_file、finishへ進む前に、まず実行可能なPython実装を write_file で作成してください。"
            )
            suggested_fix = "最初の未達条件である write_file <implementation>.py を実行し、placeholderではない実装artifactを作成してください。"
            if phase == "implementation_missing_needs_semantic_revision":
                reason_code = "implementation_task_phase_requires_reviewed_implementation"
                message = (
                    "直前の実装提案は semantic review で未達と判定され、実装artifactもまだ存在しません。"
                    "次はレビュー内容を反映した完全なPython実装を write_file してください。"
                )
                suggested_fix = "同じplaceholderや同じ失敗戦略を繰り返さず、レビュー本文の不足項目を解消する実装を作成してください。"
            return {
                "reason_code": reason_code,
                "phase": phase,
                "path": path,
                "message": message,
                "allowed_next_actions": list(state.get("allowed_next_actions") or []),
                "suggested_fix": suggested_fix,
                "state": state,
            }
        if phase == "implementation_present_but_placeholder":
            if tool_name in {"write_file", "append_file", "replace_text"} and _artifact_path_is_python_implementation(path):
                if tool_name == "replace_text":
                    current_source = self._current_artifact_source_for_path(path, turn_workspace=turn_workspace)
                    if self._implementation_edit_is_broad_rewrite(
                        tool_name=tool_name,
                        tool_args=tool_args,
                        current_source=current_source,
                    ):
                        old_text = str(tool_args.get("old_text") or "")
                        new_text = str(tool_args.get("new_text") or "")
                        return {
                            "reason_code": "implementation_task_placeholder_blocks_broad_replace_text",
                            "phase": phase,
                            "path": path,
                            "message": (
                                "placeholder実装の修復でも、巨大replace_textで既存ファイルを丸ごと置換することは許可しません。"
                                f" proposed_old_text_chars={len(old_text)}, proposed_new_text_chars={len(new_text)}。"
                                "完全実装へ置き換える場合は write_file でファイル全体を提出してください。"
                            ),
                            "allowed_next_actions": [f"write_file {path}", f"replace_text {path} with a small unique old_text"],
                            "suggested_fix": "新しい完全実装はwrite_fileで出してください。replace_textはplaceholder行など小さい一意なold_textだけに限定してください。",
                            "blocked_by": "implementation_task_progress_controller",
                            "next_required_action": "write_file the complete placeholder-free implementation",
                            "broad_rewrite": True,
                            "state": state,
                        }
                return None
            if tool_name == "read_file" and _artifact_path_is_python_implementation(path):
                return None
            return {
                "reason_code": "implementation_task_phase_requires_placeholder_fix",
                "phase": phase,
                "path": path,
                "message": (
                    "実装成果物に pass/TODO/return None/return [] などのplaceholderが残っています。"
                    "テスト作成やfinishへ進む前に、完全な実装へ修正してください。"
                ),
                "allowed_next_actions": list(state.get("allowed_next_actions") or []),
                "suggested_fix": "placeholder contract を解消する write_file/replace_text を行ってください。",
                "state": state,
            }
        if phase == "implementation_present_needs_semantic_review":
            recovery = state.get("latest_edit_match_failure_recovery") if isinstance(state.get("latest_edit_match_failure_recovery"), dict) else {}
            latest_impl = str(state.get("latest_implementation_path") or "").replace("\\", "/")
            repeated_match_failures = int(recovery.get("match_failures_since_success") or 0) >= 2 if recovery else False
            if repeated_match_failures and _artifact_path_is_python_implementation(path):
                current_source = self._current_artifact_source_for_path(path, turn_workspace=turn_workspace)
                current_issues = [str(issue) for issue in state.get("implementation_source_issues") or [] if str(issue).strip()]
                current_issue_set = {str(issue).strip() for issue in current_issues if str(issue).strip()}
                if tool_name == "write_file" and path == latest_impl and self._implementation_edit_reduces_source_contract_issues(
                    user_message=user_message,
                    tool_name=tool_name,
                    tool_args=tool_args,
                    current_source=current_source,
                    current_issues=current_issues,
                    implementation_path=path,
                    turn_workspace=turn_workspace,
                ):
                    return None
                candidate_issues: list[str] = []
                block_signature = ""
                repair_strategy = ""
                if tool_name == "write_file" and path == latest_impl:
                    candidate_source = self._candidate_source_from_implementation_edit(
                        tool_name=tool_name,
                        tool_args=tool_args,
                        current_source=current_source,
                    )
                    candidate_issue_set: set[str] = set()
                    if candidate_source.strip() and current_issue_set:
                        try:
                            ast.parse(candidate_source)
                        except SyntaxError as exc:
                            candidate_issue_set.add(f"candidate_source_syntax_error: {exc.msg}")
                        else:
                            candidate_issue_set = self._candidate_implementation_issue_set(
                                user_message=user_message,
                                candidate_source=candidate_source,
                                current_issue_set=current_issue_set,
                                implementation_path=path,
                                turn_workspace=turn_workspace,
                            )
                    candidate_issues = sorted(candidate_issue_set)
                    block_signature = self._implementation_contract_block_signature(
                        reason_code="implementation_task_phase_requires_contract_reducing_write_after_repeated_no_match",
                        issue_set=candidate_issue_set or current_issue_set,
                    )
                    repair_strategy = self._implementation_contract_repair_strategy(
                        user_message=user_message,
                        current_issues=current_issues,
                        candidate_issues=candidate_issues,
                    )
                return {
                    "reason_code": "implementation_task_phase_requires_contract_reducing_write_after_repeated_no_match",
                    "phase": phase,
                    "path": path,
                    "message": (
                        "replace_text old_text 不一致が同じ実装ファイルで複数回続いています。"
                        "再読や曖昧なreplace_textではなく、missing_requirementsを減らす完全な write_file だけ許可します。"
                        f" 現在未達: {self._issue_preview(current_issues)}。"
                        f" 提案後も残る未達: {self._issue_preview(candidate_issues)}。"
                    ),
                    "allowed_next_actions": [f"write_file {latest_impl or path}"],
                    "suggested_fix": (
                        "現在未達と提案後未達の差分を見て、残っている public API / source contract を直接消す"
                        "完全なPythonソースをwrite_fileしてください。契約未達が減らないrewriteは拒否されます。"
                        + (f" {repair_strategy}" if repair_strategy else "")
                    ),
                    "repair_strategy": repair_strategy,
                    "repair_hints": list(state.get("implementation_source_repair_hints") or []),
                    "missing_requirements": current_issues,
                    "candidate_missing_requirements": candidate_issues,
                    "block_signature": block_signature,
                    "state": state,
                }
            if (
                tool_name == "read_file"
                and _artifact_path_is_python_implementation(path)
                and latest_impl
                and path == latest_impl
                and recovery
                and recovery.get("read_after_failure")
                and not recovery.get("successful_edit_after_failure")
            ):
                return {
                    "reason_code": "implementation_task_phase_requires_targeted_replace_after_no_match",
                    "phase": phase,
                    "path": path,
                    "message": (
                        "replace_text old_text 不一致後の recovery read はすでに完了しています。"
                        "同じ実装ファイルを再読せず、現在読めている内容から一意に一致する小さい old_text の replace_text、"
                        "または完全な write_file に進んでください。"
                    ),
                    "allowed_next_actions": list(state.get("allowed_next_actions") or [f"replace_text {latest_impl}", f"write_file {latest_impl}"]),
                    "suggested_fix": "read_fileを繰り返さず、missing_requirementsを直接減らすtargeted replace_textまたはcomplete write_fileに進んでください。",
                    "repair_hints": list(state.get("implementation_source_repair_hints") or []),
                    "state": state,
                }
            if (
                tool_name == "replace_text"
                and _artifact_path_is_python_implementation(path)
                and latest_impl
                and path == latest_impl
                and recovery
                and recovery.get("read_after_failure")
                and not recovery.get("successful_edit_after_failure")
            ):
                old_text = str(tool_args.get("old_text") or "")
                new_text = str(tool_args.get("new_text") or "")
                failed_old_text = str(recovery.get("failed_old_text") or "")

                def normalized_replace_text(value: str) -> str:
                    return str(value or "").replace("\r\n", "\n").rstrip()

                repeats_failed_old_text = bool(failed_old_text) and (
                    normalized_replace_text(old_text) == normalized_replace_text(failed_old_text)
                )
                retry_is_large = len(old_text) >= 800 or len(new_text) >= 800 or len(old_text) + len(new_text) >= 1200
                allowed_actions = [str(item) for item in state.get("allowed_next_actions") or [] if str(item).strip()]
                replace_allowed_by_state = any(action.startswith("replace_text ") for action in allowed_actions)
                if repeats_failed_old_text or retry_is_large or not replace_allowed_by_state:
                    allowed = (
                        [f"write_file {latest_impl}"]
                        if not replace_allowed_by_state
                        else [f"replace_text {latest_impl} with a different small old_text", f"write_file {latest_impl}"]
                    )
                    return {
                        "reason_code": "implementation_task_phase_blocks_repeated_or_large_replace_after_no_match",
                        "phase": phase,
                        "path": path,
                        "message": (
                            "replace_text old_text 不一致後の recovery read は完了済みです。"
                            "同じold_textの再試行、または巨大old_text/new_textでの再試行は許可しません。"
                            f" proposed_old_text_chars={len(old_text)}, proposed_new_text_chars={len(new_text)}。"
                        ),
                        "allowed_next_actions": allowed,
                        "suggested_fix": (
                            "replace_textを使うなら、現在sourceから数行だけを正確に抜いた一意のold_textに変更してください。"
                            "大きな関数単位の修正が必要なら、完全な有効ソースをwrite_fileしてください。"
                        ),
                        "blocked_by": "implementation_task_progress_controller",
                        "next_required_action": "use a different small targeted replace_text, or write_file the complete corrected implementation",
                        "latest_edit_match_failure_recovery": recovery,
                        "repair_hints": list(state.get("implementation_source_repair_hints") or []),
                        "state": state,
                    }
            if tool_name in {"write_file", "append_file", "replace_text"} and _artifact_path_is_python_implementation(path):
                current_issues = [str(issue) for issue in state.get("implementation_source_issues") or [] if str(issue).strip()]
                current_issue_set = {str(issue).strip() for issue in current_issues if str(issue).strip()}
                if latest_impl and path == latest_impl and current_issue_set:
                    current_source = self._current_artifact_source_for_path(path, turn_workspace=turn_workspace)
                    broad_replace = tool_name == "replace_text" and self._implementation_edit_is_broad_rewrite(
                        tool_name=tool_name,
                        tool_args=tool_args,
                        current_source=current_source,
                    )
                    if broad_replace:
                        targeted = self._implementation_contract_prefers_targeted_edit(
                            current_issues=current_issues,
                        )
                        allowed = [f"replace_text {latest_impl}"] if targeted else [f"write_file {latest_impl}"]
                        return {
                            "reason_code": "implementation_task_phase_blocks_broad_replace_text",
                            "phase": phase,
                            "path": path,
                            "message": (
                                "既存実装の巨大 replace_text は禁止です。"
                                "局所修正なら一意に一致する小さい old_text/new_text を使い、"
                                "構造的な大幅修正なら完全な write_file を使ってください。"
                            ),
                            "allowed_next_actions": allowed,
                            "suggested_fix": (
                                "replace_text は数行のtargeted editだけにしてください。"
                                "関数全体やファイル大半を置換する場合はwrite_fileで完全なファイル内容を出してください。"
                            ),
                            "repair_hints": list(state.get("implementation_source_repair_hints") or []),
                            "missing_requirements": current_issues,
                            "state": state,
                        }
                    candidate_source = self._candidate_source_from_implementation_edit(
                        tool_name=tool_name,
                        tool_args=tool_args,
                        current_source=current_source,
                    )
                    if candidate_source.strip():
                        candidate_issue_set: set[str] = set()
                        try:
                            ast.parse(candidate_source)
                        except SyntaxError:
                            candidate_issue_set = set(current_issue_set)
                        else:
                            candidate_issue_set = self._candidate_implementation_issue_set(
                                user_message=user_message,
                                candidate_source=candidate_source,
                                current_issue_set=current_issue_set,
                                implementation_path=path,
                                turn_workspace=turn_workspace,
                            )
                        progress_reduced = self._implementation_contract_progress_reduced(
                            user_message=user_message,
                            current_source=current_source,
                            candidate_source=candidate_source,
                            current_issue_set=current_issue_set,
                            candidate_issue_set=candidate_issue_set,
                        )
                        if not progress_reduced:
                            candidate_issues = sorted(candidate_issue_set)
                            current_progress_metrics = self._implementation_source_contract_progress_metrics(
                                user_message=user_message,
                                source=current_source,
                            )
                            candidate_progress_metrics = self._implementation_source_contract_progress_metrics(
                                user_message=user_message,
                                source=candidate_source,
                            )
                            repair_strategy = self._implementation_contract_repair_strategy(
                                user_message=user_message,
                                current_issues=current_issues,
                                candidate_issues=candidate_issues,
                            )
                            targeted = self._implementation_contract_prefers_targeted_edit(
                                current_issues=current_issues,
                            )
                            reason_code = "implementation_task_phase_requires_contract_reducing_edit"
                            recent = self._recent_same_blocked_action_count(
                                session_id=session_id,
                                code="implementation_task_progress_blocked",
                                reason_code=reason_code,
                                blocked_tool=tool_name,
                                path=path,
                            )
                            state_allowed = [str(item) for item in state.get("allowed_next_actions") or [] if str(item).strip()]
                            state_allows_replace = any(action.startswith("replace_text ") for action in state_allowed)
                            state_allows_write = any(action.startswith("write_file ") for action in state_allowed)
                            allowed = [f"replace_text {latest_impl}"] if targeted and state_allows_replace else state_allowed
                            if targeted and state_allows_replace:
                                suggested_fix = (
                                    "missing_requirementsを減らす編集だけが受理されます。"
                                    "全体再生成ではなく、現在のsourceから一意に一致する小さいold_text/new_textで修正してください。"
                                )
                            elif state_allows_write:
                                suggested_fix = (
                                    "missing_requirementsを減らす編集だけが受理されます。"
                                    "allowed_next_actions は write_file です。現在の未達を解消した完全なファイル内容を書いてください。"
                                )
                            else:
                                suggested_fix = (
                                    "missing_requirementsを減らす編集だけが受理されます。"
                                    "allowed_next_actions に従い、現在の未達を直接減らしてください。"
                                )
                            return {
                                "reason_code": reason_code,
                                "phase": phase,
                                "path": path,
                                "message": (
                                    "実装契約の未達が残っているため、同じ未達を残す編集は進捗として受理しません。"
                                    f" 現在未達: {self._issue_preview(current_issues)}。"
                                    f" 提案後も残る未達: {self._issue_preview(candidate_issues)}。"
                                ),
                                "allowed_next_actions": allowed,
                                "suggested_fix": suggested_fix + (f" {repair_strategy}" if repair_strategy else ""),
                                "repair_strategy": repair_strategy,
                                "repair_hints": list(state.get("implementation_source_repair_hints") or []),
                                "missing_requirements": current_issues,
                                "candidate_missing_requirements": candidate_issues,
                                "current_progress_metrics": current_progress_metrics,
                                "candidate_progress_metrics": candidate_progress_metrics,
                                "block_signature": self._implementation_contract_block_signature(
                                    reason_code=reason_code,
                                    issue_set=candidate_issue_set or current_issue_set,
                                ),
                                "state": state,
                                "terminal_failure": recent >= 2,
                                "failure_type": "implementation_contract_nonreducing_edit_loop" if recent >= 2 else "",
                            }
                return None
            if tool_name == "read_file" and _artifact_path_is_python_implementation(path):
                return None
            return {
                "reason_code": "implementation_task_phase_requires_implementation_revision",
                "phase": phase,
                "path": path,
                "message": (
                    "実装成果物は存在していますが、ユーザー要求の実装契約を満たしていない観測があります。"
                    "テスト作成やunittestへ進む前に、未達条件を解消する実装修正を行ってください。"
                ),
                "allowed_next_actions": list(state.get("allowed_next_actions") or []),
                "suggested_fix": "missing_requirements に列挙された実装契約違反を解消してから、tests/test_*.py 作成へ進んでください。",
                "repair_hints": list(state.get("implementation_source_repair_hints") or []),
                "state": state,
            }
        if phase == "tests_missing":
            if tool_name in {"write_file", "append_file", "replace_text"} and _artifact_path_is_test(path):
                return None
            if tool_name in {"write_file", "append_file", "replace_text"} and _artifact_path_is_python_implementation(path):
                return {
                    "reason_code": "implementation_task_phase_requires_tests",
                    "phase": phase,
                    "path": path,
                    "message": (
                        "実装成果物は存在しています。実装+unittest契約では、同じ実装ファイルを編集し続けず、"
                        "次の未達条件である意味のある tests/test_*.py 作成に進んでください。"
                    ),
                    "allowed_next_actions": list(state.get("allowed_next_actions") or []),
                    "suggested_fix": "tests/test_*.py にユーザー要求の公開APIと期待挙動を検証するassertを書いてください。",
                    "state": state,
                }
            if tool_name in {"run_command", "finish", "read_file"}:
                return {
                    "reason_code": "implementation_task_phase_requires_tests",
                    "phase": phase,
                    "path": path,
                    "message": "意味のあるunittest成果物がまだありません。先に tests/test_*.py を作成してください。",
                    "allowed_next_actions": list(state.get("allowed_next_actions") or []),
                    "suggested_fix": "pass-onlyやassertなしではなく、具体的な期待値を持つunittestを書いてください。",
                    "state": state,
                }
        if phase == "tests_present_needs_semantic_review":
            semantic_repair_target = str(state.get("semantic_repair_target") or "")
            test_issue_paths = [str(item).replace("\\", "/") for item in state.get("semantic_test_repair_paths") or [] if str(item).strip()]
            latest_test = str(state.get("latest_test_path") or "").replace("\\", "/")
            if not test_issue_paths and (
                semantic_repair_target == "test_artifact" or state.get("test_source_issues")
            ):
                if latest_test:
                    test_issue_paths = [latest_test]
            consumed_test_reads = {str(item).replace("\\", "/") for item in state.get("semantic_repair_read_consumed_paths") or [] if str(item).strip()}
            if test_issue_paths:
                if tool_name in {"write_file", "append_file", "replace_text"} and _artifact_path_is_test(path):
                    if tool_name == "replace_text":
                        current_source = self._current_artifact_source_for_path(path, turn_workspace=turn_workspace)
                        if self._implementation_edit_is_broad_rewrite(
                            tool_name=tool_name,
                            tool_args=tool_args,
                            current_source=current_source,
                        ):
                            return {
                                "reason_code": "implementation_task_test_semantic_blocks_broad_replace_text",
                                "phase": phase,
                                "path": path,
                                "message": (
                                    "semantic review はテスト成果物の未達を示していますが、既存テストファイルの巨大 replace_text は禁止です。"
                                    "全面的なテスト修正が必要なら write_file、小さいassert/fixture修正なら targeted replace_text にしてください。"
                                ),
                                "allowed_next_actions": [
                                    f"replace_text {path} with a small unique old_text",
                                    f"write_file {path}",
                                ],
                                "suggested_fix": "テスト全体を書き直す場合は完全な tests/test_*.py を write_file してください。replace_text は数行の一意なold_textだけにしてください。",
                                "blocked_by": "implementation_task_progress_controller",
                                "next_required_action": "write_file the complete corrected test file, or retry replace_text with a small unique old_text",
                                "state": state,
                                "broad_rewrite": True,
                            }
                    return None
                if tool_name == "read_file" and path in test_issue_paths:
                    if path in consumed_test_reads:
                        return {
                            "reason_code": "implementation_task_test_semantic_read_already_consumed",
                            "phase": phase,
                            "path": path,
                            "message": (
                                f"{path} は tests_present_needs_semantic_review ですでに read_file 済みです。"
                                "同じ観測を繰り返さず、semantic_issues を減らす replace_text/write_file に進んでください。"
                            ),
                            "allowed_next_actions": list(state.get("allowed_next_actions") or []),
                            "suggested_fix": "読んだtest内容に基づき、対象テストの期待値やassertだけを修正してください。",
                            "state": state,
                        }
                    return None
            if semantic_repair_target == "implementation_artifact":
                if tool_name in {"write_file", "append_file", "replace_text"} and _artifact_path_is_python_implementation(path):
                    return None
                if tool_name == "read_file" and _artifact_path_is_python_implementation(path):
                    return None
            return {
                "reason_code": "implementation_task_phase_requires_semantic_revision",
                "phase": phase,
                "path": path,
                "message": "semantic review が未達を示しています。unittest再実行やfinishの前に対象ファイルを修正してください。",
                "allowed_next_actions": list(state.get("allowed_next_actions") or []),
                "suggested_fix": "semantic_issues が示す対象をread/editし、未達を減らしてください。",
                "state": state,
            }
        if phase == "unittest_not_run":
            command = str(tool_args.get("command") or "")
            if tool_name == "run_command" and "unittest" in command.lower():
                return None
            return {
                "reason_code": "implementation_task_phase_requires_unittest",
                "phase": phase,
                "path": path,
                "message": "実装と意味のあるテストが揃っています。次は編集や観測ではなく unittest を実行して検証証拠を取得してください。",
                "allowed_next_actions": list(state.get("allowed_next_actions") or []),
                "suggested_fix": "python3 -m unittest discover -s tests を実行してください。",
                "state": state,
            }
        if phase == "external_audit_required":
            command = str(tool_args.get("command") or "")
            if tool_name == "run_command" and "unittest" in command.lower():
                return None
            return {
                "reason_code": "implementation_task_phase_requires_external_audit",
                "phase": phase,
                "path": path,
                "message": "内部unittestは成功済みです。finish前にcontroller側の外部audit runを取得してください。",
                "allowed_next_actions": list(state.get("allowed_next_actions") or []),
                "suggested_fix": "python3 -m unittest discover -s tests を再実行し、独立した検証証拠を追加してください。",
                "state": state,
            }
        if phase == "unittest_failed_needs_fix":
            command = str(tool_args.get("command") or "")
            failed_paths = [str(item).replace("\\", "/") for item in state.get("failed_unittest_recovery_read_paths") or [] if str(item).strip()]
            consumed_paths = {str(item).replace("\\", "/") for item in state.get("failed_unittest_recovery_read_consumed_paths") or [] if str(item).strip()}
            unread_paths = [item for item in failed_paths if item not in consumed_paths]
            editable_after_read_paths = [
                str(item).replace("\\", "/")
                for item in state.get("failed_unittest_recovery_editable_paths") or []
                if str(item).strip()
            ]
            write_only_paths = [
                str(item).replace("\\", "/")
                for item in state.get("failed_unittest_no_match_write_only_paths") or []
                if str(item).strip()
            ]
            noop_blocked_paths = {
                str(item).replace("\\", "/")
                for item in state.get("failed_unittest_noop_blocked_paths") or []
                if str(item).strip()
            }
            state_space_exact_replace_paths = {
                str(item).replace("\\", "/")
                for item in state.get("state_space_no_match_exact_replace_paths") or []
                if str(item).strip()
            }
            state_space_fixture_value_impl_blocked_paths = {
                str(item).replace("\\", "/")
                for item in state.get("state_space_fixture_value_impl_blocked_paths") or []
                if str(item).strip()
            }
            repeated_nonreducing_paths_for_phase = (
                {
                    str(item).replace("\\", "/")
                    for item in state.get("same_signature_nonreducing_edit_paths") or []
                    if str(item).strip()
                }
                if bool(state.get("repeated_unittest_failure_signature"))
                else set()
            )
            if state.get("successful_edit_after_failed_unittest"):
                repeated_timeout = (
                    str(state.get("latest_unittest_failure_type") or "") == "command_timeout"
                    and bool(state.get("repeated_unittest_failure_signature"))
                )
                full_unittest_command = "python3 -m unittest discover -s tests"
                normalized_command = " ".join(command.split()).lower()
                if repeated_timeout:
                    if (
                        tool_name == "run_command"
                        and "unittest" in command.lower()
                        and normalized_command != full_unittest_command
                    ):
                        return None
                    return {
                        "reason_code": "implementation_task_failed_unittest_requires_narrow_rerun_after_timeout",
                        "phase": phase,
                        "path": path,
                        "message": (
                            "unittest失敗後に対象ファイルの成功編集がありますが、直近の失敗は同一command_timeoutの再発です。"
                            "全体unittest discoverを同じまま繰り返してもtimeout箇所を特定できません。"
                        ),
                        "allowed_next_actions": list(state.get("allowed_next_actions") or []),
                        "suggested_fix": (
                            "python3 -m unittest tests.<module>.<Class>.<test_method> など、"
                            "失敗しそうな最小test targetへ狭めたrun_commandを実行してください。"
                        ),
                        "blocked_by": "implementation_task_progress_controller",
                        "next_required_action": "run_command with a narrower unittest target",
                        "state": state,
                    }
                if tool_name == "run_command" and "unittest" in command.lower():
                    return None
                return {
                    "reason_code": "implementation_task_failed_unittest_requires_rerun_after_edit",
                    "phase": phase,
                    "path": path,
                    "message": (
                        "unittest失敗後に対象ファイルの成功編集があります。"
                        "同じread_fileや追加編集を続けず、同じunittestを再実行して"
                        "失敗signatureが解消または変化したかを確認してください。"
                    ),
                    "allowed_next_actions": list(state.get("allowed_next_actions") or []),
                    "suggested_fix": "python3 -m unittest discover -s tests を再実行してください。",
                    "blocked_by": "implementation_task_progress_controller",
                    "next_required_action": "run_command python3 -m unittest discover -s tests",
                    "state": state,
                }
            if unread_paths:
                if tool_name == "read_file" and path in unread_paths:
                    return None
                if tool_name == "read_file" and path in consumed_paths and not write_only_paths:
                    return {
                        "reason_code": "implementation_task_failed_unittest_read_already_consumed",
                        "phase": phase,
                        "path": path,
                        "message": f"{path} はunittest失敗後にすでにread_file済みです。同じ観測を繰り返さず、未読対象を読むか読了済み対象の修正へ進んでください。",
                        "allowed_next_actions": list(state.get("allowed_next_actions") or []),
                        "suggested_fix": "allowed_next_actions に出ている未読対象のread_fileを1つ実行してください。",
                        "state": state,
                    }
                return {
                    "reason_code": "implementation_task_failed_unittest_requires_recovery_read",
                    "phase": phase,
                    "path": path,
                    "message": (
                        "unittest失敗後は修正前にtraceback対象test/implementationをread_fileできます。"
                        "未読対象が残っている間は編集を混ぜず、観測を揃えてからtargeted editへ進んでください。"
                    ),
                    "allowed_next_actions": list(state.get("allowed_next_actions") or []),
                    "suggested_fix": "allowed_next_actions の read_file を実行し、未読対象の具体ソースを確認してください。",
                    "state": state,
                }
            if path in noop_blocked_paths and tool_name in {"read_file", "append_file", "replace_text", "write_file"}:
                return {
                    "reason_code": "implementation_task_failed_unittest_blocks_repeated_noop_target",
                    "phase": phase,
                    "path": path,
                    "message": (
                        "unittest失敗後、この対象には実差分のない編集が連続しています。"
                        "同じ内容を再提出しても失敗signatureは変わりません。"
                    ),
                    "allowed_next_actions": list(state.get("allowed_next_actions") or []),
                    "suggested_fix": (
                        "allowed_next_actionsに出ている別のtraceback関連対象を読み直すか、"
                        "別対象へ実差分を含む修正を出してください。"
                    ),
                    "blocked_by": "implementation_task_progress_controller",
                    "next_required_action": "choose a different allowed target; do not repeat the no-op file",
                    "state": state,
                }
            if path in state_space_fixture_value_impl_blocked_paths and tool_name in {"append_file", "replace_text", "write_file"}:
                return {
                    "reason_code": "implementation_task_failed_unittest_prioritizes_fixture_repair_after_impl_noop",
                    "phase": phase,
                    "path": path,
                    "message": (
                        "state_space_searchのunittest失敗はtest artifact内のfixture期待値を強く示しており、"
                        "implementation側への直前編集は無変更または無一致でした。"
                        "同じimplementation targetを再試行せず、読了済みtest artifactのfixture/action/expected値を"
                        "state/action/goal契約から導ける形へ修正してください。"
                    ),
                    "allowed_next_actions": list(state.get("allowed_next_actions") or []),
                    "suggested_fix": (
                        "allowed_next_actionsに出ているtest artifactを修正してください。"
                        "分析でtest fixtureが矛盾していると判断した場合、tool targetもtests/test_*.pyにしてください。"
                    ),
                    "blocked_by": "implementation_task_progress_controller",
                    "next_required_action": "repair the read test artifact fixture instead of repeating implementation no-op edit",
                    "state": state,
                }
            if tool_name == "write_file" and path in state_space_exact_replace_paths:
                current_source = self._current_artifact_source_for_path(path, turn_workspace=turn_workspace)
                current_source_excerpt = self._replace_text_current_source_excerpt(
                    current_source=current_source,
                    old_text="",
                )
                return {
                    "reason_code": "implementation_task_failed_unittest_requires_exact_replace_after_fixture_no_match",
                    "phase": phase,
                    "path": path,
                    "message": (
                        "state_space_searchのunittest失敗はtest artifact内のhardcoded fixture期待値を指しており、"
                        "このimplementation pathでは直前のreplace_text old_textが現在sourceに一致していません。"
                        "実装全体を再生成せず、現行sourceに一意一致する小さいold_textでtargeted replace_textしてください。"
                    ),
                    "allowed_next_actions": [
                        f"replace_text {target} with a small unique old_text"
                        for target in sorted(state_space_exact_replace_paths)
                    ]
                    + [f"write_file {target}" for target in write_only_paths],
                    "suggested_fix": (
                        "current_source_excerptがある場合はそこからold_textを正確にコピーしてください。"
                        "test fixtureが契約違反ならtest artifactを修正し、implementationを直す場合も小さいreplace_textに限定してください。"
                    ),
                    "blocked_by": "implementation_task_progress_controller",
                    "next_required_action": f"replace_text {path} with a small unique old_text",
                    "current_source_excerpt": current_source_excerpt,
                    "state": state,
                }
            if write_only_paths:
                if tool_name == "write_file" and path in write_only_paths:
                    return None
                if not (
                    tool_name in {"replace_text", "write_file"}
                    and path in repeated_nonreducing_paths_for_phase
                ):
                    allowed_after_no_match = [
                        *[
                            f"replace_text {target} with a small unique old_text"
                            for target in repeated_nonreducing_paths_for_phase
                            if target in editable_after_read_paths
                        ],
                        *[f"write_file {target}" for target in write_only_paths],
                    ]
                    return {
                        "reason_code": "implementation_task_failed_unittest_requires_write_after_no_match",
                        "phase": phase,
                        "path": path,
                        "message": (
                            "unittest失敗の修復対象はすでにread_file済みで、その後のreplace_textが無一致または無変更でした。"
                            "同じread_fileや曖昧なreplace_textを繰り返さず、"
                            "非改善pathは小さいexact replace_text、その他の既読対象はwrite_fileで修正してください。"
                        ),
                        "allowed_next_actions": allowed_after_no_match,
                        "suggested_fix": "直近に読んだ実装/テスト内容とtracebackに基づき、非改善pathなら現在sourceに一意一致する小さいold_textでreplace_textし、別対象なら完全なwrite_fileで失敗signatureを変えてください。",
                        "state": state,
                    }
            if tool_name == "read_file" and path in consumed_paths:
                return {
                    "reason_code": "implementation_task_failed_unittest_read_already_consumed",
                    "phase": phase,
                    "path": path,
                    "message": f"{path} はunittest失敗後にすでにread_file済みです。同じ観測を繰り返さず、未読対象を読むか読了済み対象の修正へ進んでください。",
                    "allowed_next_actions": list(state.get("allowed_next_actions") or []),
                    "suggested_fix": "allowed_next_actions から、未読対象のread_file、または読了済み対象のreplace_text/write_fileを1つ選んでください。",
                    "state": state,
                }
            if tool_name in {"append_file", "replace_text"} and (_artifact_path_is_test(path) or _artifact_path_is_python_implementation(path)):
                repair_targets = self._implementation_unittest_repair_target_paths(state)
                if not repair_targets or path in repair_targets:
                    repeated_nonreducing_paths = (
                        {
                            str(item).replace("\\", "/")
                            for item in state.get("same_signature_nonreducing_edit_paths") or []
                            if str(item).strip()
                        }
                        if bool(state.get("repeated_unittest_failure_signature"))
                        else set()
                    )
                    target_actions = [
                        *[
                            f"replace_text {target} with a small unique old_text"
                            for target in repair_targets
                            if _artifact_path_is_test(target) or _artifact_path_is_python_implementation(target)
                        ],
                        *[
                            f"write_file {target}"
                            for target in repair_targets
                            if (
                                (_artifact_path_is_test(target) or _artifact_path_is_python_implementation(target))
                                and target not in repeated_nonreducing_paths
                            )
                        ],
                    ] or list(state.get("allowed_next_actions") or [])
                    if tool_name == "replace_text":
                        current_source = self._current_artifact_source_for_path(path, turn_workspace=turn_workspace)
                        old_text = str(tool_args.get("old_text") or "")
                        new_text = str(tool_args.get("new_text") or "")
                        block_header_only_replace = self._replace_text_uses_block_header_only(
                            old_text=old_text,
                            new_text=new_text,
                        )
                        broad_replace = self._implementation_edit_is_broad_rewrite(
                            tool_name=tool_name,
                            tool_args=tool_args,
                            current_source=current_source,
                        )
                        exact_match_count = current_source.count(old_text) if current_source and old_text else 0
                        if not block_header_only_replace and not broad_replace and exact_match_count == 1:
                            return None
                        current_source_excerpt = self._replace_text_current_source_excerpt(
                            current_source=current_source,
                            old_text=old_text,
                        )
                        if block_header_only_replace:
                            reason_code = "implementation_task_failed_unittest_blocks_block_header_replace_text"
                        elif broad_replace:
                            reason_code = "implementation_task_failed_unittest_blocks_broad_replace_text"
                        else:
                            reason_code = "implementation_task_failed_unittest_blocks_unmatched_replace_text"
                        if block_header_only_replace:
                            allowed_actions = (
                                [f"replace_text {path} with a small unique old_text"]
                                if path in repeated_nonreducing_paths
                                else [f"write_file {path}"]
                            )
                        else:
                            allowed_actions: list[str] = []
                            for action in [f"replace_text {path} with a small unique old_text", *target_actions]:
                                if action not in allowed_actions:
                                    allowed_actions.append(action)
                        message = (
                            "unittest失敗後の必要ファイルはread_file済みです。"
                            "小さいtargeted replace_textは許可しますが、現在sourceに一意一致しないold_text、"
                            "長い関数/ファイル全体を置換するreplace_text、"
                            "またはPythonブロックヘッダ1行だけをold_textにした複数行replace_textは許可しません。"
                            f" proposed_old_text_chars={len(old_text)}, proposed_new_text_chars={len(new_text)}, "
                            f"exact_old_text_matches={exact_match_count}, "
                            f"block_header_only_replace={block_header_only_replace}。"
                        )
                        return {
                            "reason_code": reason_code,
                            "phase": phase,
                            "path": path,
                            "message": message,
                            "allowed_next_actions": allowed_actions,
                            "suggested_fix": (
                                "replace_textを使うなら、read_file済みの現在sourceから数行だけを正確にコピーした"
                                "一意なold_textにしてください。関数やclassを置換する場合はヘッダだけではなく現在のブロック全体を含めるか、"
                                "大きい修正なら完全なwrite_fileで出してください。"
                                " current_source_excerptがある場合は、そこからold_textを作ってください。"
                            ),
                            "blocked_by": "implementation_task_progress_controller",
                            "next_required_action": (
                                "write_file the complete corrected target file"
                                if block_header_only_replace
                                else "retry with a small exact replace_text, or write_file the complete corrected target file"
                            ),
                            "broad_rewrite": broad_replace,
                            "block_header_only_replace": block_header_only_replace,
                            "exact_old_text_matches": exact_match_count,
                            "current_source_excerpt": current_source_excerpt,
                            "state": state,
                        }
                    return {
                        "reason_code": "implementation_task_failed_unittest_requires_write_repair",
                        "phase": phase,
                        "path": path,
                        "message": (
                            "unittest失敗後の必要ファイルはread_file済みです。"
                            "old_textに大きな範囲を入れるreplace_textや追記ではなく、"
                            "tracebackと読んだ内容に基づく完全なwrite_fileで修正してください。"
                        ),
                        "allowed_next_actions": target_actions,
                        "suggested_fix": "失敗対象のtestまたはimplementationを、修正後の完全なファイル内容としてwrite_fileしてください。",
                        "state": state,
                    }
            if tool_name == "write_file" and (_artifact_path_is_test(path) or _artifact_path_is_python_implementation(path)):
                repair_targets = self._implementation_unittest_repair_target_paths(state)
                if not repair_targets or path in repair_targets:
                    candidate_source = str(tool_args.get("content") or "")
                    current_source = self._current_artifact_source_for_path(path, turn_workspace=turn_workspace)
                    missing_import = (
                        state.get("latest_unittest_missing_import")
                        if isinstance(state.get("latest_unittest_missing_import"), dict)
                        else {}
                    )
                    latest_impl_path = str(state.get("latest_implementation_path") or "").replace("\\", "/")
                    repeated_nonreducing_paths = {
                        str(item).replace("\\", "/")
                        for item in state.get("same_signature_nonreducing_edit_paths") or []
                        if str(item).strip()
                    }
                    if (
                        missing_import
                        and latest_impl_path
                        and path == latest_impl_path
                        and self._implementation_edit_is_broad_rewrite(
                            tool_name="write_file",
                            tool_args=tool_args,
                            current_source=current_source,
                        )
                    ):
                        missing_name = str(missing_import.get("name") or "")
                        missing_module = str(missing_import.get("module") or "")
                        return {
                            "reason_code": "implementation_task_failed_unittest_blocks_broad_write_for_import_error",
                            "phase": phase,
                            "path": path,
                            "message": (
                                "直近unittestは単一のImportErrorです。"
                                f"{missing_module}.{missing_name} のmodule-level export不足を直すために、"
                                "既存実装全体のwrite_fileは広すぎます。"
                            ),
                            "allowed_next_actions": [f"replace_text {path} with a small unique old_text"],
                            "suggested_fix": (
                                "read済みsourceの数行だけをold_textにして、欠けているexport名のalias追加またはrenameだけを行ってください。"
                                "同等の小文字/camelCase名が既にある場合は、その定義行の直後に互換aliasを追加する小さいreplace_textにしてください。"
                            ),
                            "latest_unittest_missing_import": missing_import,
                            "latest_unittest_output_excerpt": str(state.get("latest_unittest_output_excerpt") or "")[-1200:],
                            "blocked_by": "implementation_task_progress_controller",
                            "next_required_action": f"replace_text {path} with a small unique old_text",
                            "state": state,
                        }
                    if not bool(state.get("repeated_unittest_failure_signature")):
                        nonreducing_reason = self._write_file_nonreducing_reason(
                            candidate_source=candidate_source,
                            current_source=current_source,
                        )
                    else:
                        nonreducing_reason = ""
                    if nonreducing_reason:
                        target_actions = [
                            *[
                                f"replace_text {target} with a small unique old_text"
                                for target in repair_targets
                                if _artifact_path_is_test(target) or _artifact_path_is_python_implementation(target)
                            ],
                            *[
                                f"write_file {target}"
                                for target in repair_targets
                                if (
                                    (_artifact_path_is_test(target) or _artifact_path_is_python_implementation(target))
                                    and (
                                        not bool(state.get("repeated_unittest_failure_signature"))
                                        or target not in repeated_nonreducing_paths
                                    )
                                )
                            ],
                        ] or list(state.get("allowed_next_actions") or [])
                        output_excerpt = str(state.get("latest_unittest_output_excerpt") or "").strip()
                        return {
                            "reason_code": "implementation_task_failed_unittest_blocks_nonreducing_write",
                            "phase": phase,
                            "path": path,
                            "message": (
                                "unittest失敗後のwrite_fileが現在sourceと同一、またはコメント/空白だけのsemantic no-opです。"
                                "この編集は失敗signatureを変える可能性が低いため受理しません。"
                            ),
                            "allowed_next_actions": target_actions,
                            "suggested_fix": (
                                "tracebackの具体行とread済みsourceに基づき、実行されるコードまたはテスト期待値を変更してください。"
                                "コメントだけ、空白だけ、同一内容のwrite_fileは拒否します。"
                            ),
                            "latest_unittest_failure_signature": str(
                                state.get("latest_unittest_failure_signature") or ""
                            ),
                            "latest_unittest_output_excerpt": output_excerpt[-1200:],
                            "nonreducing_reason": nonreducing_reason,
                            "blocked_by": "implementation_task_progress_controller",
                            "next_required_action": "write_file a semantically changed repair for one traceback-related target",
                            "state": state,
                        }
                    if bool(state.get("repeated_unittest_failure_signature")) and path in repeated_nonreducing_paths:
                        target_actions = [
                            *[
                                f"replace_text {target} with a small unique old_text"
                                for target in repair_targets
                                if _artifact_path_is_test(target) or _artifact_path_is_python_implementation(target)
                            ],
                            *[
                                f"write_file {target}"
                                for target in repair_targets
                                if (
                                    (_artifact_path_is_test(target) or _artifact_path_is_python_implementation(target))
                                    and target not in repeated_nonreducing_paths
                                )
                            ],
                        ] or list(state.get("allowed_next_actions") or [])
                        output_excerpt = str(state.get("latest_unittest_output_excerpt") or "").strip()
                        return {
                            "reason_code": "implementation_task_failed_unittest_blocks_repeated_full_write_after_nonreducing_signature",
                            "phase": phase,
                            "path": path,
                            "message": (
                                "前回編集後もunittest failure signatureが同一で、このpathへの全文writeは失敗を減らしていません。"
                                "同じ対象への次の全面write_fileは、同型反復を避けるため許可しません。"
                            ),
                            "allowed_next_actions": target_actions,
                            "suggested_fix": (
                                "read済みsourceの具体行に対する小さいreplace_text、"
                                "または別のtraceback対象ファイルのwrite_fileで失敗signatureを変えてください。"
                            ),
                            "latest_unittest_failure_signature": str(
                                state.get("latest_unittest_failure_signature") or ""
                            ),
                            "latest_unittest_output_excerpt": output_excerpt[-1200:],
                            "nonreducing_edit_paths": sorted(repeated_nonreducing_paths),
                            "blocked_by": "implementation_task_progress_controller",
                            "next_required_action": (
                                "use a small targeted replace_text on the nonreducing path, "
                                "or write_file a different traceback-related target"
                            ),
                            "state": state,
                        }
                    if bool(state.get("repeated_unittest_failure_signature")):
                        recent_nonreducing_sources: list[str] = []
                        previous_same_index = int(state.get("previous_same_unittest_failure_index") or -1)
                        latest_failed_index = int(state.get("latest_failed_unittest_index") or -1)
                        for index, step in enumerate(steps):
                            if (
                                previous_same_index < 0
                                or latest_failed_index < 0
                                or index <= previous_same_index
                                or index >= latest_failed_index
                            ):
                                continue
                            step_tool = str(step.get("tool_name") or "")
                            if step_tool not in {"write_file", "append_file", "replace_text"}:
                                continue
                            step_args = step.get("tool_args") if isinstance(step.get("tool_args"), dict) else {}
                            step_result = step.get("tool_result") if isinstance(step.get("tool_result"), dict) else {}
                            if not bool(step_result.get("ok")):
                                continue
                            step_path = str(step_result.get("path") or step_args.get("path") or "").replace("\\", "/")
                            if step_path != path:
                                continue
                            if step_tool == "write_file":
                                previous_candidate = str(step_args.get("content") or "")
                            elif step_tool == "replace_text":
                                previous_candidate = str(
                                    step_args.get("new_text") or step_args.get("content") or ""
                                )
                            else:
                                previous_candidate = str(step_args.get("content") or "")
                            if previous_candidate:
                                recent_nonreducing_sources.append(previous_candidate)

                        repeated_nonreducing_reason = self._write_file_nonreducing_reason(
                            candidate_source=candidate_source,
                            current_source=current_source,
                            previous_nonreducing_sources=recent_nonreducing_sources,
                        )
                        if repeated_nonreducing_reason:
                            target_actions = [
                                *[
                                    f"replace_text {target} with a small unique old_text"
                                    for target in repair_targets
                                    if _artifact_path_is_test(target) or _artifact_path_is_python_implementation(target)
                                ],
                                *[
                                f"write_file {target}"
                                for target in repair_targets
                                if (
                                    (_artifact_path_is_test(target) or _artifact_path_is_python_implementation(target))
                                    and target not in repeated_nonreducing_paths
                                )
                            ],
                        ] or list(state.get("allowed_next_actions") or [])
                            output_excerpt = str(state.get("latest_unittest_output_excerpt") or "").strip()
                            return {
                                "reason_code": "implementation_task_failed_unittest_blocks_noop_write",
                                "phase": phase,
                                "path": path,
                                "message": (
                                    "前回編集後もunittest failure signatureが同一です。"
                                    "今回のwrite_fileは現在sourceまたは直前の非進捗編集と同じ内容で、失敗を減らせません。"
                                ),
                                "allowed_next_actions": target_actions,
                                "suggested_fix": (
                                    "同じ内容を書き直さず、tracebackの具体行と読んだtest/implementationに基づいて"
                                    "失敗signatureを変える完全なファイル内容をwrite_fileしてください。"
                                ),
                                "latest_unittest_failure_signature": str(
                                    state.get("latest_unittest_failure_signature") or ""
                                ),
                                "latest_unittest_output_excerpt": output_excerpt[-1200:],
                                "nonreducing_edit_paths": list(
                                    state.get("same_signature_nonreducing_edit_paths") or []
                                ),
                                "nonreducing_reason": repeated_nonreducing_reason,
                                "blocked_by": "implementation_task_progress_controller",
                                "next_required_action": (
                                    "write_file a changed repair for one traceback-related target; "
                                    "do not repeat identical content"
                                ),
                                "state": state,
                            }
                    return None
                return {
                    "reason_code": "implementation_task_failed_unittest_requires_targeted_fix",
                    "phase": phase,
                    "path": path,
                    "message": "unittest が失敗しています。このファイルは直近の失敗から導出された修正対象ではありません。",
                    "allowed_next_actions": list(state.get("allowed_next_actions") or []),
                    "suggested_fix": "traceback対象testまたは関連implementationをreplace_text/write_fileで修正してください。",
                    "state": state,
                }
            if tool_name == "run_command" and "unittest" in command.lower():
                latest_edit_recovery = state.get("latest_edit_match_failure_recovery") if isinstance(state.get("latest_edit_match_failure_recovery"), dict) else {}
                if (
                    state.get("failed_unittest_recovery_read_consumed")
                    and not state.get("successful_edit_after_failed_unittest")
                    and not latest_edit_recovery
                ):
                    return {
                        "reason_code": "implementation_task_failed_unittest_requires_edit_before_rerun",
                        "phase": phase,
                        "path": path,
                        "message": "unittest失敗後、対象ファイルを読んだだけでまだ成功編集がありません。再実行前に失敗原因を修正してください。",
                        "allowed_next_actions": list(state.get("allowed_next_actions") or []),
                        "suggested_fix": "read内容とtracebackに基づき、replace_text/write_fileで修正してからunittestを再実行してください。",
                        "state": state,
                    }
                return None
            return {
                "reason_code": "implementation_task_failed_unittest_requires_targeted_fix",
                "phase": phase,
                "path": path,
                "message": "unittest が失敗しています。finishや無関係な操作ではなく、失敗対象を修正してください。",
                "allowed_next_actions": list(state.get("allowed_next_actions") or []),
                "suggested_fix": "traceback対象testまたはimplementationをreplace_text/write_fileで修正してください。",
                "state": state,
            }
        if phase == "external_contract_satisfied" and tool_name != "finish":
            return {
                "reason_code": "implementation_task_phase_requires_finish",
                "phase": phase,
                "path": path,
                "message": "実装、意味のあるテスト、unittest成功の外部契約が揃っています。次は追加作業ではなくfinishしてください。",
                "allowed_next_actions": list(state.get("allowed_next_actions") or ["finish"]),
                "suggested_fix": "成果物と検証結果をfinal_answerに含めてfinishしてください。",
                "state": state,
            }
        return None

    def _completion_contract_recovery_action(
        self,
        *,
        session_id: str,
        user_message: str,
        steps: list[dict[str, Any]],
        step_index: int,
        max_steps: int,
        turn_workspace: Path | None = None,
    ) -> dict[str, Any] | None:
        """Return a deterministic next action when generic contract evidence is missing."""
        contract = _finish_acceptance_contract(user_message)
        evidence = _finish_acceptance_evidence(steps)
        progress_state: dict[str, Any] | None = None

        def unittest_recovery_is_currently_allowed() -> bool:
            nonlocal progress_state
            if progress_state is None:
                progress_state = self._implementation_task_progress_state(
                    user_message=user_message,
                    steps=steps,
                    session_id=session_id,
                    turn_workspace=turn_workspace,
                )
            return "run_command python3 -m unittest discover -s tests" in [
                str(item) for item in progress_state.get("allowed_next_actions") or []
            ]

        if (
            "unittest_run" in contract
            and bool(evidence.get("python_artifact_written"))
            and bool(evidence.get("tests_written"))
            and bool(evidence.get("meaningful_tests"))
            and not bool(evidence.get("unittest_run"))
        ):
            if self._latest_semantic_review_requires_revision(session_id=session_id):
                return None
            if not unittest_recovery_is_currently_allowed():
                return None
            return {
                "tool_name": "run_command",
                "tool_args": {"command": "python3 -m unittest discover -s tests", "shell": "auto"},
                "reason_code": "completion_contract_unittest_recovery",
                "system_decision": "CompletionContract recovery: 実装と意味のあるunittestが揃ったため、unittestを実行して検証証拠を取得します。",
            }
        if (
            "unittest_run" in contract
            and bool(evidence.get("python_artifact_written"))
            and bool(evidence.get("tests_written"))
            and bool(evidence.get("meaningful_tests"))
            and bool(evidence.get("unittest_passed"))
            and self._successful_unittest_run_count(steps) < 2
        ):
            if self._latest_semantic_review_requires_revision(session_id=session_id):
                return None
            if not unittest_recovery_is_currently_allowed():
                return None
            return {
                "tool_name": "run_command",
                "tool_args": {"command": "python3 -m unittest discover -s tests", "shell": "auto"},
                "reason_code": "completion_contract_external_audit_recovery",
                "system_decision": "CompletionContract recovery: finish前の外部auditとしてunittestを再実行します。",
            }
        text = str(user_message or "").lower()
        if not any(marker in text for marker in ["実行", "run", "execute", "起動"]):
            return None
        if step_index < max(1, int(max_steps or 1)) - 1:
            return None
        missing_commands = _missing_requested_commands(self, user_message=user_message, steps=steps)
        if not missing_commands:
            return None
        command = missing_commands[0]
        return {
            "tool_name": "run_command",
            "tool_args": {"command": command, "shell": "auto"},
            "reason_code": "completion_contract_requested_command_recovery",
            "system_decision": "CompletionContract recovery: requested command has not been executed yet.",
        }

    def _edit_observation_required_after_validation_failure(
        self,
        *,
        tool_name: str,
        tool_args: dict[str, Any],
        steps: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        edit_tools = {"write_file", "append_file", "replace_text"}
        recoverable_failure_types = {"validation_failed", "replace_text_no_match", "replace_text_ambiguous_match"}
        if tool_name not in edit_tools:
            return None
        path = str(tool_args.get("path") or "").strip()
        if not path:
            return None
        read_after_latest_failure = False
        match_failure_types = {"replace_text_no_match", "replace_text_ambiguous_match"}
        repeated_match_failures = 0
        for step in reversed(steps):
            previous_tool = str(step.get("tool_name") or "")
            previous_args = step.get("tool_args") if isinstance(step.get("tool_args"), dict) else {}
            previous_result = step.get("tool_result") if isinstance(step.get("tool_result"), dict) else {}
            previous_path = str(previous_result.get("path") or previous_args.get("path") or "").strip()
            if previous_path != path:
                continue
            if previous_tool in edit_tools and bool(previous_result.get("ok")):
                break
            if previous_tool in edit_tools and str(previous_result.get("failure_type") or "") in match_failure_types:
                repeated_match_failures += 1
        for step in reversed(steps):
            previous_tool = str(step.get("tool_name") or "")
            previous_args = step.get("tool_args") if isinstance(step.get("tool_args"), dict) else {}
            previous_result = step.get("tool_result") if isinstance(step.get("tool_result"), dict) else {}
            previous_path = str(previous_result.get("path") or previous_args.get("path") or "").strip()
            if previous_path != path:
                continue
            if previous_tool == "read_file" and bool(previous_result.get("ok")):
                read_after_latest_failure = True
                continue
            if previous_tool in edit_tools:
                failure_type = str(previous_result.get("failure_type") or "")
                if not bool(previous_result.get("ok")) and failure_type in recoverable_failure_types:
                    if failure_type == "validation_failed" and previous_result.get("file_exists") is False:
                        if tool_name == "write_file":
                            return None
                        blocked = dict(previous_result)
                        blocked["_recovery_reason_code"] = "write_file_required_after_failed_create_validation"
                        blocked["_allowed_next_actions"] = ["write_file"]
                        blocked["_suggested_fix"] = (
                            f"{path} は構文検証失敗によりまだ作成されていません。"
                            "read_file や append_file ではなく、ファイル全体を完全な有効Pythonとして write_file で再提出してください。"
                        )
                        return blocked
                    if failure_type == "validation_failed" and tool_name == "write_file":
                        return None
                    if failure_type in match_failure_types and tool_name == "write_file":
                        return None
                    if read_after_latest_failure and failure_type in match_failure_types and repeated_match_failures >= 2 and tool_name != "write_file":
                        blocked = dict(previous_result)
                        blocked["_recovery_reason_code"] = "write_file_required_after_repeated_edit_match_failure"
                        blocked["_allowed_next_actions"] = ["write_file"]
                        blocked["_suggested_fix"] = (
                            f"{path} への局所置換は同じ対象一致失敗を繰り返しています。"
                            "次は replace_text ではなく、ファイル全体を完全な有効Pythonとして write_file で書き直してください。"
                        )
                        return blocked
                    if read_after_latest_failure and tool_name == "append_file":
                        blocked = dict(previous_result)
                        blocked["_recovery_reason_code"] = "replace_or_write_required_after_validation_failed" if failure_type == "validation_failed" else "replace_or_write_required_after_edit_failed"
                        blocked["_allowed_next_actions"] = ["replace_text", "write_file"]
                        blocked["_suggested_fix"] = (
                            f"read_file 済みです。{path} は既存の有効なPythonファイルなので、"
                            "append_file で断片を足さず、replace_text で一意な範囲を置換するか、"
                            "write_file で完全な有効Pythonとして書き直してください。"
                        )
                        return blocked
                    if read_after_latest_failure:
                        return None
                    blocked = dict(previous_result)
                    blocked["_recovery_reason_code"] = "read_file_required_after_validation_failed" if failure_type == "validation_failed" else "read_file_required_after_edit_failed"
                    blocked["_allowed_next_actions"] = ["read_file"]
                    blocked["_suggested_fix"] = f"read_file で {path} の現在内容を確認し、現在のファイルから exact old_text をコピーしてから修正してください。"
                    return blocked
                if bool(previous_result.get("ok")):
                    return None
        return None

    def _repeated_recovery_observation_block(
        self,
        *,
        tool_name: str,
        tool_args: dict[str, Any],
        steps: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        if tool_name != "read_file":
            return None
        path = str(tool_args.get("path") or "").strip()
        if not path:
            return None
        reads_since_failed_command = 0
        saw_failed_command = False
        for step in reversed(steps):
            previous_tool = str(step.get("tool_name") or "")
            previous_args = step.get("tool_args") if isinstance(step.get("tool_args"), dict) else {}
            previous_result = step.get("tool_result") if isinstance(step.get("tool_result"), dict) else {}
            if previous_tool in {"write_file", "append_file", "replace_text"} and bool(previous_result.get("ok")):
                return None
            if previous_tool == "run_command":
                if bool(previous_result.get("ok")):
                    return None
                saw_failed_command = True
                break
            previous_path = str(previous_result.get("path") or previous_args.get("path") or "").strip()
            if previous_tool == "read_file" and previous_path == path and bool(previous_result.get("ok")):
                reads_since_failed_command += 1
        if not saw_failed_command or reads_since_failed_command < 1:
            return None
        return {
            "reason_code": "repeated_recovery_observation",
            "path": path,
            "allowed_next_actions": ["replace_text", "write_file", "read_file"],
            "suggested_fix": (
                f"{path} は検証失敗後にすでに観測済みです。"
                "同じファイルを再読せず、失敗箇所を replace_text で修正するか、"
                "ファイル全体を write_file で書き直してください。別ファイルの確認が必要な場合だけ別pathをread_fileしてください。"
            ),
        }

    def _read_file_recovers_latest_edit_match_failure(
        self,
        *,
        tool_name: str,
        tool_args: dict[str, Any],
        steps: list[dict[str, Any]],
    ) -> bool:
        """Whether this read is the single recovery observation after a failed local edit."""

        if tool_name != "read_file":
            return False
        path = str(tool_args.get("path") or "").strip()
        if not path:
            return False
        edit_tools = {"write_file", "append_file", "replace_text"}
        match_failure_types = {"replace_text_no_match", "replace_text_ambiguous_match"}
        for step in reversed(steps):
            previous_tool = str(step.get("tool_name") or "")
            previous_args = step.get("tool_args") if isinstance(step.get("tool_args"), dict) else {}
            previous_result = step.get("tool_result") if isinstance(step.get("tool_result"), dict) else {}
            previous_path = str(previous_result.get("path") or previous_args.get("path") or "").strip()
            if previous_path != path:
                continue
            if previous_tool == "read_file" and bool(previous_result.get("ok")):
                return False
            if previous_tool in edit_tools:
                failure_type = str(previous_result.get("failure_type") or "")
                return (not bool(previous_result.get("ok"))) and failure_type in match_failure_types
        return False

    def _post_implementation_test_progress_observation_block(
        self,
        *,
        user_message: str,
        tool_name: str,
        tool_args: dict[str, Any],
        steps: list[dict[str, Any]],
        progress_state: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if tool_name != "read_file":
            return None
        if "unittest_run" not in _finish_acceptance_contract(user_message):
            return None
        path = str(tool_args.get("path") or "").strip()
        if not _artifact_path_is_python_implementation(path):
            return None
        if self._read_file_recovers_latest_edit_match_failure(
            tool_name=tool_name,
            tool_args=tool_args,
            steps=steps,
        ):
            return None
        latest_impl_index = -1
        for index, step in enumerate(steps):
            previous_tool = str(step.get("tool_name") or "")
            previous_args = step.get("tool_args") if isinstance(step.get("tool_args"), dict) else {}
            previous_result = step.get("tool_result") if isinstance(step.get("tool_result"), dict) else {}
            previous_path = str(previous_result.get("path") or previous_args.get("path") or "").strip()
            if (
                previous_tool in {"write_file", "append_file", "replace_text"}
                and bool(previous_result.get("ok"))
                and _artifact_path_is_python_implementation(previous_path)
            ):
                latest_impl_index = index
        if latest_impl_index < 0:
            return None
        reads_after_impl = 0
        for step in steps[latest_impl_index + 1:]:
            previous_tool = str(step.get("tool_name") or "")
            previous_args = step.get("tool_args") if isinstance(step.get("tool_args"), dict) else {}
            previous_result = step.get("tool_result") if isinstance(step.get("tool_result"), dict) else {}
            previous_path = str(previous_result.get("path") or previous_args.get("path") or "").strip()
            if previous_tool in {"write_file", "append_file", "replace_text"} and bool(previous_result.get("ok")) and _artifact_path_is_test(previous_path):
                return None
            if previous_tool == "run_command":
                return None
            if previous_tool == "read_file" and previous_path == path and bool(previous_result.get("ok")):
                reads_after_impl += 1
        if reads_after_impl < 1:
            return None
        if self._progress_allows_semantic_implementation_repair(
            progress_state=progress_state,
            tool_name="replace_text",
            path=path,
        ):
            missing = [
                str(item).strip()
                for item in (progress_state or {}).get("missing_requirements") or []
                if str(item).strip()
            ]
            return {
                "reason_code": "post_implementation_observation_requires_revision",
                "path": path,
                "allowed_next_actions": [f"replace_text {path}", f"write_file {path}"],
                "suggested_fix": (
                    f"{path} は実装成果物として作成済みで、すでに一度 read_file で確認済みです。"
                    "同じ実装ファイルの観測を続けず、missing_requirements を解消する実装修正に進んでください。"
                ),
                "missing_requirements": missing,
            }
        current_source = ""
        target = (self.execution_root / path).expanduser().resolve()
        try:
            target.relative_to(self.execution_root)
        except ValueError:
            target = None  # type: ignore[assignment]
        if target is not None and target.exists() and target.is_file():
            current_source = target.read_text(encoding="utf-8", errors="replace")
        source_issues = (
            self._implementation_source_contract_issues(
                user_message=user_message,
                source=current_source,
            )
            if current_source
            else []
        )
        if source_issues:
            return {
                "reason_code": "post_implementation_observation_requires_revision",
                "path": path,
                "allowed_next_actions": ["replace_text <implementation>.py", "write_file <implementation>.py"],
                "suggested_fix": (
                    f"{path} は実装成果物として作成済みで、すでに一度 read_file で確認済みです。"
                    "同じ実装ファイルの観測を続けず、missing_requirements を解消する実装修正に進んでください。"
                ),
                "missing_requirements": source_issues,
            }
        return {
            "reason_code": "post_implementation_observation_loop",
            "path": path,
            "allowed_next_actions": ["write_file tests/test_*.py"],
            "suggested_fix": (
                f"{path} は実装成果物として作成済みで、すでに一度 read_file で確認済みです。"
                "実装+unittest契約では、同じ実装ファイルの観測を続けず、"
                "次の未達条件である意味のある tests/test_*.py 作成に進んでください。"
            ),
        }

    def _recent_same_blocked_action_count(
        self,
        *,
        session_id: str,
        code: str,
        reason_code: str,
        blocked_tool: str,
        path: str = "",
        block_signature: str = "",
        lookback: int = 80,
    ) -> int:
        count = 0
        normalized_path = str(path or "").strip()
        normalized_signature = str(block_signature or "").strip()
        inspected_control_events = 0
        for event in reversed(read_jsonl(self.paths.session_events_path(session_id))):
            if str(event.get("type") or "") == "runtime_event":
                continue
            inspected_control_events += 1
            if inspected_control_events > lookback:
                break
            if event.get("type") == "tool_result" and bool(event.get("ok")):
                break
            if event.get("type") != "system_note" or str(event.get("code") or "") != code:
                continue
            details = event.get("details") if isinstance(event.get("details"), dict) else {}
            event_path = str(details.get("path") or "").strip()
            if (
                str(event.get("reason_code") or "") == reason_code
                and str(details.get("blocked_tool") or "") == blocked_tool
                and (not normalized_path or event_path == normalized_path)
                and (not normalized_signature or str(details.get("block_signature") or "").strip() == normalized_signature)
            ):
                count += 1
                continue
        return count


    def _implementation_initial_repair_context(
        self,
        *,
        user_message: str,
        steps: list[dict[str, Any]],
        session_id: str,
        turn_workspace: Path,
        reason_code: str,
        blocked_tool: str,
        path: str = "",
        block_signature: str = "",
    ) -> dict[str, Any]:
        progress_state = self._implementation_task_progress_state(
            user_message=user_message,
            steps=steps,
            session_id=session_id,
            turn_workspace=turn_workspace,
        )
        phase = str(progress_state.get("phase") or "")
        if (
            not bool(progress_state.get("applicable"))
            or phase not in {"implementation_missing", "implementation_missing_needs_semantic_revision"}
            or list(progress_state.get("implementation_paths") or [])
            or blocked_tool not in {"write_file", "replace_text"}
            or not _artifact_path_is_python_implementation(path)
        ):
            return {
                "active": False,
                "phase": phase,
            }
        same_block_limit = int(self.runtime_config.get("initial_implementation_contract_repair_limit") or 3)
        same_block_count = self._recent_same_blocked_action_count(
            session_id=session_id,
            code="edit_blocked",
            reason_code=reason_code,
            blocked_tool=blocked_tool,
            path=path,
            block_signature=block_signature,
        )
        return {
            "active": True,
            "phase": phase,
            "same_block_count": same_block_count,
            "same_block_limit": same_block_limit,
        }

    def _implementation_rewrite_loop_block(
        self,
        *,
        user_message: str,
        tool_name: str,
        tool_args: dict[str, Any],
        steps: list[dict[str, Any]],
        progress_state: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if tool_name not in {"write_file", "append_file", "replace_text"}:
            return None
        if "unittest_run" not in _finish_acceptance_contract(user_message):
            return None
        path = str(tool_args.get("path") or "").strip()
        if not _artifact_path_is_python_implementation(path):
            return None
        if self._progress_allows_semantic_implementation_repair(
            progress_state=progress_state,
            tool_name=tool_name,
            path=path,
        ):
            return None
        current_source = ""
        target = (self.execution_root / path).expanduser().resolve()
        try:
            target.relative_to(self.execution_root)
        except ValueError:
            target = None  # type: ignore[assignment]
        if target is not None and target.exists() and target.is_file():
            current_source = target.read_text(encoding="utf-8", errors="replace")
        if current_source and self._implementation_source_contract_issues(
            user_message=user_message,
            source=current_source,
        ):
            return None
        successful_same_artifact_edits = 0
        for step in reversed(steps):
            previous_tool = str(step.get("tool_name") or "")
            previous_args = step.get("tool_args") if isinstance(step.get("tool_args"), dict) else {}
            previous_result = step.get("tool_result") if isinstance(step.get("tool_result"), dict) else {}
            previous_path = str(previous_result.get("path") or previous_args.get("path") or "").strip()
            if previous_tool == "run_command":
                return None
            if previous_tool in {"write_file", "append_file", "replace_text"} and _artifact_path_is_test(previous_path):
                return None
            if previous_tool in {"write_file", "append_file", "replace_text"} and previous_path == path and bool(previous_result.get("ok")):
                successful_same_artifact_edits += 1
        if successful_same_artifact_edits < 1:
            return None
        return {
            "reason_code": "implementation_artifact_rewrite_loop",
            "path": path,
            "allowed_next_actions": ["write_file tests/test_*.py", "run_command python3 -m unittest discover -s tests"],
            "suggested_fix": (
                f"{path} はすでに構文有効なPython成果物として書き込まれています。"
                "同じ実装ファイルを書き直し続けず、次の未達条件である意味のあるunittest作成、"
                "またはunittest実行による検証証拠の取得に進んでください。"
            ),
        }


    def _progress_allows_semantic_implementation_repair(
        self,
        *,
        progress_state: dict[str, Any] | None,
        tool_name: str,
        path: str,
    ) -> bool:
        if not isinstance(progress_state, dict):
            return False
        if str(progress_state.get("phase") or "") != "implementation_present_needs_semantic_review":
            return False
        if tool_name not in {"write_file", "replace_text"}:
            return False
        normalized_path = str(path or "").replace("\\", "/")
        latest_impl = str(progress_state.get("latest_implementation_path") or "").replace("\\", "/")
        if latest_impl and normalized_path != latest_impl:
            return False
        issue_keys = ("implementation_source_issues",)
        if any(progress_state.get(key) for key in issue_keys):
            return True
        return bool(progress_state.get("missing_requirements"))

    def _implementation_progress_allows_repeated_command(
        self,
        *,
        user_message: str,
        tool_args: dict[str, Any],
        steps: list[dict[str, Any]],
        session_id: str,
        turn_workspace: Path,
    ) -> bool:
        command = str(tool_args.get("command") or "").strip().lower()
        if "unittest" not in command:
            return False
        state = self._implementation_task_progress_state(
            user_message=user_message,
            steps=steps,
            session_id=session_id,
            turn_workspace=turn_workspace,
        )
        if state.get("phase") != "external_audit_required":
            return False
        return any(
            "run_command" in str(action).lower() and "unittest" in str(action).lower()
            for action in state.get("allowed_next_actions") or []
        )

    def _finish_status_is_accepted(self, status: Any) -> bool:
        return str(status or "") == "success"

    def _finish_acceptance_reason_code(self, acceptance: dict[str, Any]) -> str:
        """Effective reason_code for the finish_acceptance decision event.

        When semantic review was unavailable but the runtime accepted via the
        observation-based override, surface that as a single canonical reason
        code so the canonical decision event remains "1 event = 1 decision".
        """
        override = (acceptance or {}).get("acceptance_override") or {}
        override_reason = str(override.get("reason_code") or "")
        if override_reason:
            return override_reason
        return str((acceptance or {}).get("semantic_status") or "")

    def _finish_acceptance_block_text(self, acceptance: dict[str, Any]) -> str:
        missing_text = ", ".join(str(item) for item in acceptance.get("missing") or []) or str(acceptance.get("semantic_status") or "unknown")
        parts = [f"完了がブロックされました: success acceptance を満たしていません: {missing_text}"]
        limitations = [str(item) for item in acceptance.get("limitations") or [] if str(item).strip()]
        if limitations:
            parts.append("limitations: " + "; ".join(limitations))
        return " ".join(parts)

    _FINISH_BLOCK_REASONS = ("finish_acceptance_failed", "judge_invalid_output", "judge_error", "grounding_issues")

    def _consecutive_finish_block_count(self, recent_events: list[dict[str, Any]]) -> int:
        count, _ = self._consecutive_finish_block_summary(recent_events)
        return count

    def _consecutive_finish_block_summary(self, recent_events: list[dict[str, Any]]) -> tuple[int, str]:
        """Return (count, dominant_trigger_reason).

        dominant_trigger_reason は連続ブロック群の中で最後 (=最直近) の reason_code。
        fallback ラベルの asymmetry 解消のため、何が引き金だったかを失わないようにする。
        """
        count = 0
        latest_reason = ""
        for event in reversed(recent_events):
            event_type = str(event.get("type") or "")
            if event_type == "tool_result":
                break
            if event_type != "system_note":
                continue
            code = str(event.get("code") or "")
            reason = str(event.get("reason_code") or "")
            if code == "finish_blocked" and reason in self._FINISH_BLOCK_REASONS:
                count += 1
                if not latest_reason:
                        latest_reason = reason
        return count, latest_reason

    def _recent_events_for_action_context(
        self,
        *,
        session_id: str,
        current_frame: Any,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        """Return recent events plus the latest critical judge/block context.

        Invariant: prompt compaction may drop ordinary history, but it must not
        drop the latest event that explains why completion or grounding was
        blocked. Otherwise the actor repeats the same action with no corrective
        signal.
        """
        source = (
            list(current_frame.session_events)
            if current_frame is not None and getattr(current_frame, "session_events", None)
            else read_jsonl(self.paths.session_events_path(session_id), limit=max(limit, 1000))
        )
        tail = source[-limit:]
        critical_codes = {
            "llm_output_issue",
            "command_failed",
            "command_blocked",
            "edit_blocked",
            "observation_blocked",
            "implementation_task_progress_blocked",
            "implementation_task_initial_placeholder_loop",
            "implementation_task_progress",
            "semantic_implementation_review",
            "grounding_judge",
            "finish_blocked",
            "finish_acceptance",
            "first_action_required",
            "plan_acceptance_blocked",
            "plan_record_autorepaired",
            "plan_revision_returned_to_root",
            "plan_execution_paused_for_progress",
            "completion_contract_recovery",
            "contract_incomplete",
            "step_limit_reached",
            "step_limit_final_gate",
            "blocked_action_ignored",
            "command_similarity_warning",
            "validation_failure_consultant",
            "work_package_invalid",
        }
        critical: list[dict[str, Any]] = []
        latest_judge_context: dict[str, Any] | None = None
        for event in reversed(source):
            if str(event.get("type") or "") != "system_note":
                continue
            code = str(event.get("code") or "")
            details = event.get("details") if isinstance(event.get("details"), dict) else {}
            if latest_judge_context is None and (
                code == "grounding_judge"
                or (code == "finish_blocked" and isinstance(details.get("judge"), dict))
            ):
                latest_judge_context = event
            if code in critical_codes:
                critical.append(event)
            if len(critical) >= 4 and latest_judge_context is not None:
                break
        keep_keys = {str(event.get("event_id") or id(event)) for event in tail}
        keep_keys.update(str(event.get("event_id") or id(event)) for event in critical)
        if latest_judge_context is not None:
            keep_keys.add(str(latest_judge_context.get("event_id") or id(latest_judge_context)))
        # Preserve chronological source order. Reordering critical events would
        # corrupt guards that reason about whether a tool result happened before
        # or after a block.
        return [
            event
            for event in source
            if str(event.get("event_id") or id(event)) in keep_keys
        ]

    def _handle_decompose_tasks(
        self,
        *,
        session_id: str,
        turn_id: str,
        queue_id: str,
        step_index: int,
        tool_args: dict[str, Any],
        turn_workspace: Path,
        steps: list[dict[str, Any]] | None = None,
        user_message: str = "",
        current_model: str = "",
        skip_acceptance: bool = False,
    ) -> dict[str, Any]:
        parent = self.frame_manager.current_frame()
        blocked = self._child_contract_blocks_decomposition(
            session_id=session_id,
            turn_id=turn_id,
            queue_id=queue_id,
            step_index=step_index,
            turn_workspace=turn_workspace,
            tool_name="decompose_tasks",
        )
        if blocked is not None:
            return {"ok": False, "event": blocked, "error": "child contract blocks decomposition"}
        tasks = self._normalize_child_tasks(tool_args.get("tasks") or [])
        if not tasks:
            note = self._append_session_event(
                session_id,
                {
                    "type": "system_note",
                    "role": "system",
                    "content": "decompose_tasks requires at least one task with a goal",
                    "code": "decompose_tasks_blocked",
                    "reason_code": "empty_task_plan",
                    "details": {
                        "blocked_tool": "decompose_tasks",
                        "failure_type": "empty_task_plan",
                        "blocked_by": "work_package_contract",
                        "allowed_next_actions": [
                            {"tool": "decompose_tasks", "strategy": "retry with at least one concrete task and executable first_action"},
                            {"tool": "read_file|search_code|run_command|write_file|append_file|replace_text|list_files", "strategy": "take one direct action if decomposition is unnecessary"},
                        ],
                        "suggested_fix": "少なくとも1件の具体taskを含め、各taskに実行可能なfirst_actionを入れてください。",
                        "next_required_action": "retry decompose_tasks with concrete tasks, or take one direct parent-frame action",
                    },
                    "turn_id": turn_id,
                    "queue_id": queue_id,
                    "step_index": step_index,
                    "llm_workspace": str(turn_workspace),
                },
            )
            return {"ok": False, "event": note, "error": "empty task plan"}
        invalid: list[str] = []
        for task in tasks:
            issues = self._work_package_issues(self._normalize_work_package(task))
            if issues:
                invalid.append(f"{task.get('task_id')}: {', '.join(issues)}")
        if invalid:
            note = self._work_package_blocked_event(
                session_id=session_id,
                turn_id=turn_id,
                queue_id=queue_id,
                step_index=step_index,
                turn_workspace=turn_workspace,
                tool_name="decompose_tasks",
                issues=invalid,
                tasks=tasks,
                user_message=user_message,
            )
            details = note.get("details") if isinstance(note.get("details"), dict) else {}
            return {
                "ok": False,
                "event": note,
                "error": "invalid work package",
                "terminal_failure": bool(details.get("terminal_failure")),
            }
        if not skip_acceptance:
            acceptance = self._plan_acceptance_gate(
                session_id=session_id,
                turn_id=turn_id,
                queue_id=queue_id,
                step_index=step_index,
                turn_workspace=turn_workspace,
                tool_name="decompose_tasks",
                user_message=user_message,
                tasks=tasks,
                current_model=current_model,
            )
            if not bool(acceptance.get("ok")):
                return {"ok": False, "event": acceptance.get("event"), "error": "plan semantic mismatch"}
        active_tasks: list[dict[str, Any]] = []
        skipped_tasks: list[dict[str, Any]] = []
        for task in tasks:
            normalized = self._normalize_work_package(task)
            if self._work_package_first_action_already_succeeded(normalized, list(steps or [])):
                skipped_tasks.append({**task, "status": "complete", "completion_reason": "first_action_already_satisfied"})
            else:
                active_tasks.append(task)
        if skipped_tasks:
            self._append_session_event(
                session_id,
                {
                    "type": "system_note",
                    "role": "system",
                    "content": f"既に満たされた観測タスクを {len(skipped_tasks)} 件スキップしました。",
                    "code": "decompose_tasks_skipped_satisfied",
                    "reason_code": "first_action_already_satisfied",
                    "details": {"skipped_tasks": skipped_tasks},
                    "turn_id": turn_id,
                    "queue_id": queue_id,
                    "step_index": step_index,
                    "llm_workspace": str(turn_workspace),
                },
            )
        if not active_tasks:
            note = self._append_session_event(
                session_id,
                {
                    "type": "system_note",
                    "role": "system",
                    "content": "decompose_tasks was blocked because every child first_action was already satisfied by existing evidence.",
                    "code": "decompose_tasks_blocked",
                    "reason_code": "all_child_first_actions_already_satisfied",
                    "details": {
                        "blocked_tool": "decompose_tasks",
                        "failure_type": "all_child_first_actions_already_satisfied",
                        "blocked_by": "work_package_contract",
                        "skipped_tasks": skipped_tasks,
                        "allowed_next_actions": [
                            {"tool": "finish", "strategy": "finish if existing evidence satisfies the parent goal"},
                            {"tool": "read_file|search_code|run_command|write_file|append_file|replace_text|list_files", "strategy": "take a new direct action for a still-missing requirement"},
                        ],
                        "suggested_fix": "既存証拠で満たされた子タスクを再計画せず、未達があれば直接アクションへ進んでください。",
                        "next_required_action": "finish with existing evidence, or take one direct action for the remaining gap",
                    },
                    "turn_id": turn_id,
                    "queue_id": queue_id,
                    "step_index": step_index,
                    "llm_workspace": str(turn_workspace),
                },
            )
            return {"ok": False, "event": note, "error": "all child first actions already satisfied"}
        tasks = active_tasks
        self.frame_manager.set_child_tasks(tasks)
        plan_event = self._append_session_event(
            session_id,
            {
                "type": "task_plan",
                "role": "system",
                "frame_id": parent.frame_id if parent else None,
                "tasks": tasks,
                "rationale": str(tool_args.get("rationale") or ""),
                "content": f"Planned {len(tasks)} child tasks.",
                "turn_id": turn_id,
                "queue_id": queue_id,
                "step_index": step_index,
                "llm_workspace": str(turn_workspace),
            },
        )
        first_task = tasks[0]
        open_result = self._handle_open_child_frame(
            session_id=session_id,
            turn_id=turn_id,
            queue_id=queue_id,
            step_index=step_index,
            tool_args={
                "work_package": first_task,
                "child_task_id": first_task["task_id"],
            },
            turn_workspace=turn_workspace,
            user_message=user_message,
            current_model=current_model,
        )
        return {"ok": True, "event": plan_event, "tasks": tasks, "auto_steps": list(open_result.get("auto_steps") or [])}

    def _handle_open_child_frame(
        self,
        *,
        session_id: str,
        turn_id: str,
        queue_id: str,
        step_index: int,
        tool_args: dict[str, Any],
        turn_workspace: Path,
        user_message: str = "",
        current_model: str = "",
    ) -> dict[str, Any]:
        parent = self.frame_manager.current_frame()
        blocked = self._child_contract_blocks_decomposition(
            session_id=session_id,
            turn_id=turn_id,
            queue_id=queue_id,
            step_index=step_index,
            turn_workspace=turn_workspace,
            tool_name="open_child_frame",
        )
        if blocked is not None:
            return {"ok": False, "event": blocked, "error": "child contract blocks decomposition"}
        parent_id = parent.frame_id if parent else None
        planned_task = None
        requested_task_id = str(tool_args.get("child_task_id") or "").strip()
        if parent is not None and parent.working_memory.child_tasks:
            if requested_task_id:
                planned_task = next(
                    (task for task in parent.working_memory.child_tasks if str(task.get("task_id") or "") == requested_task_id),
                    None,
                )
            else:
                planned_task = self.frame_manager.next_pending_child_task(parent)
        raw_work_package = tool_args.get("work_package") or planned_task
        if not raw_work_package and any(key in tool_args for key in ("goal", "work_type", "first_action", "success_evidence", "why_not_direct_action")):
            raw_work_package = tool_args
        work_package = self._normalize_work_package(raw_work_package)
        if not work_package["goal"]:
            work_package["goal"] = str(tool_args.get("goal") or (planned_task or {}).get("goal") or "").strip()
        if not work_package["task_id"]:
            work_package["task_id"] = str(tool_args.get("child_task_id") or (planned_task or {}).get("task_id") or "")
        issues = self._work_package_issues(work_package)
        if issues:
            note = self._work_package_blocked_event(
                session_id=session_id,
                turn_id=turn_id,
                queue_id=queue_id,
                step_index=step_index,
                turn_workspace=turn_workspace,
                tool_name="open_child_frame",
                issues=issues,
                tasks=[work_package],
                user_message=user_message,
            )
            details = note.get("details") if isinstance(note.get("details"), dict) else {}
            return {
                "ok": False,
                "event": note,
                "error": "invalid work package",
                "terminal_failure": bool(details.get("terminal_failure")),
            }
        if not planned_task:
            acceptance = self._plan_acceptance_gate(
                session_id=session_id,
                turn_id=turn_id,
                queue_id=queue_id,
                step_index=step_index,
                turn_workspace=turn_workspace,
                tool_name="open_child_frame",
                user_message=user_message,
                tasks=[work_package],
                current_model=current_model,
            )
            if not bool(acceptance.get("ok")):
                return {"ok": False, "event": acceptance.get("event"), "error": "plan semantic mismatch"}
        recent_events = read_jsonl(self.paths.session_events_path(session_id), limit=30)
        first_action_tool = str((work_package.get("first_action") or {}).get("tool") or "")
        if first_action_tool == "run_command" and self._controller_finish_blocked_for_current_evidence(recent_events):
            message = (
                "子フレーム開始がブロックされました: 直近の未達条件を解消する corrective edit がまだありません。"
                " 同じ成果物の再実行では visible_result_sanity_passed を満たせません。"
            )
            note = self._append_session_event(
                self.root,
                session_id,
                {
                    "type": "system_note",
                    "role": "system",
                    "content": message,
                    "code": "open_child_frame_blocked",
                    "reason_code": "corrective_action_required",
                    "details": {
                        "blocked_tool": "open_child_frame",
                        "blocked_first_action": "run_command",
                        "failure_type": "corrective_action_required",
                        "blocked_by": "frame_contract",
                        "allowed_next_actions": ["replace_text", "write_file", "append_file"],
                        "suggested_fix": "問題のある成果物を修正し、その後に再実行して表示結果を更新してください。",
                        "next_required_action": "perform a corrective edit before opening a run_command child frame",
                    },
                    "turn_id": turn_id,
                    "queue_id": queue_id,
                    "step_index": step_index,
                    "llm_workspace": str(turn_workspace),
                },
            )
            return {"ok": False, "event": note, "error": "corrective edit required before rerun"}
        if parent is not None:
            work_package = self.frame_manager.register_child_task(parent=parent, task=work_package)
        goal = str(work_package.get("goal") or "child frame")
        context_summary = str(work_package.get("context_summary") or "")
        inherited_context = {
            "parent_frame_id": parent_id,
            "parent_goal": parent.goal if parent else "",
            "context_summary": context_summary,
            "done_when": str(work_package.get("done_when") or ""),
            "child_task_id": str(work_package.get("task_id") or ""),
        }
        try:
            child = self.frame_manager.open_child_frame(
                goal=goal,
                inherited_context=inherited_context,
            )
        except ValueError as exc:
            note = self._append_session_event(
                session_id,
                {
                    "type": "system_note",
                    "role": "system",
                    "content": str(exc),
                    "code": "frame_open_blocked",
                    "reason_code": "depth_limit",
                    "details": {
                        "blocked_tool": "open_child_frame",
                        "failure_type": "frame_depth_limit",
                        "blocked_by": "frame_contract",
                        "allowed_next_actions": [
                            {"tool": "return_to_parent", "strategy": "return current findings if inside a child frame"},
                            {"tool": "read_file|search_code|run_command|write_file|append_file|replace_text|list_files", "strategy": "take a direct action instead of opening another frame"},
                        ],
                        "suggested_fix": "これ以上子フレームを深くせず、現在のフレームで直接行動するか親へ戻ってください。",
                        "next_required_action": "return_to_parent or perform one direct non-frame tool action",
                    },
                    "turn_id": turn_id,
                    "queue_id": queue_id,
                    "step_index": step_index,
                    "llm_workspace": str(turn_workspace),
                },
            )
            return {"ok": False, "event": note, "error": str(exc)}
        event = self._append_session_event(
            session_id,
            {
                "type": "frame_opened",
                "role": "system",
                "frame_id": child.frame_id,
                "parent_frame_id": parent_id,
                "goal": child.goal,
                "depth": child.depth,
                "content": f"Opened child frame: {child.goal}",
                "turn_id": turn_id,
                "queue_id": queue_id,
                "step_index": step_index,
                "llm_workspace": str(turn_workspace),
            },
        )
        self.frame_manager.append_event(
            {
                "type": "planning_note",
                "role": "system",
                "content": f"子フレーム開始: {child.goal}",
                "turn_id": turn_id,
                "queue_id": queue_id,
                "step_index": step_index,
            }
        )
        if self._observer_enabled():
            self._append_session_event(
                session_id,
                {
                    "type": "observer_note",
                    "role": "observer",
                    "content": f"フレーム遷移: 親の問題を局所目的「{child.goal}」に分解しました。戻る条件は、親が次を判断できる発見を得ることです。",
                    "reason_code": "frame_opened",
                    "turn_id": turn_id,
                    "queue_id": queue_id,
                    "step_index": step_index,
                    "llm_workspace": str(turn_workspace),
                },
            )
        auto_steps = self._execute_frame_first_action(
            session_id=session_id,
            turn_id=turn_id,
            queue_id=queue_id,
            step_index=step_index,
            turn_workspace=turn_workspace,
            work_package=work_package,
        )
        return {"ok": True, "event": event, "frame": child.to_dict(), "auto_steps": auto_steps}

    def _handle_return_to_parent(
        self,
        *,
        session_id: str,
        turn_id: str,
        queue_id: str,
        step_index: int,
        tool_args: dict[str, Any],
        turn_workspace: Path,
    ) -> dict[str, Any]:
        current = self.frame_manager.current_frame()
        payload = {
            "summary": str(tool_args.get("summary") or ""),
            "findings": list(tool_args.get("findings") or []),
        }
        try:
            parent = self.frame_manager.return_to_parent(payload)
        except ValueError as exc:
            note = self._append_session_event(
                session_id,
                {
                    "type": "system_note",
                    "role": "system",
                    "content": str(exc),
                    "code": "frame_return_blocked",
                    "reason_code": "root_frame",
                    "details": {
                        "blocked_tool": "return_to_parent",
                        "failure_type": "root_frame_cannot_return",
                        "blocked_by": "frame_contract",
                        "allowed_next_actions": [
                            {"tool": "finish", "strategy": "finish if the root goal is satisfied"},
                            {"tool": "read_file|search_code|run_command|write_file|append_file|replace_text|list_files", "strategy": "take a direct root-frame action for remaining work"},
                        ],
                        "suggested_fix": "root frameではreturn_to_parentできません。完了条件が揃っていればfinish、未達があれば直接ツールを実行してください。",
                        "next_required_action": "finish at root if satisfied, or take one direct action",
                    },
                    "turn_id": turn_id,
                    "queue_id": queue_id,
                    "step_index": step_index,
                    "llm_workspace": str(turn_workspace),
                },
            )
            return {"ok": False, "event": note, "error": str(exc)}
        child_id = current.frame_id if current else None
        self.frame_manager.mark_child_task_completed(parent=parent, child=current, return_payload=payload)
        next_task = self.frame_manager.next_pending_child_task(parent)
        frame_event = self._append_session_event(
            session_id,
            {
                "type": "frame_returned",
                "role": "system",
                "frame_id": child_id,
                "parent_frame_id": parent.frame_id,
                "depth": current.depth if current is not None else None,
                "return_payload": payload,
                "content": f"Returned to parent frame: {payload['summary']}",
                "turn_id": turn_id,
                "queue_id": queue_id,
                "step_index": step_index,
                "llm_workspace": str(turn_workspace),
            },
        )
        self._append_session_event(
            session_id,
            {
                "type": "child_return",
                "role": "system",
                "child_frame_id": child_id,
                "return_payload": payload,
                "next_child_task": next_task,
                "content": payload["summary"],
                "turn_id": turn_id,
                "queue_id": queue_id,
                "step_index": step_index,
                "llm_workspace": str(turn_workspace),
            },
        )
        if self._observer_enabled():
            findings = " / ".join(str(item) for item in payload.get("findings") or [])
            self._append_session_event(
                session_id,
                {
                    "type": "observer_note",
                    "role": "observer",
                    "content": f"フレーム帰還: 子フレームは「{payload['summary']}」を持ち帰りました。親はこの発見を使って次の判断に進めます。主要発見: {findings}",
                    "reason_code": "frame_returned",
                    "turn_id": turn_id,
                    "queue_id": queue_id,
                    "step_index": step_index,
                    "llm_workspace": str(turn_workspace),
                },
            )
        return {"ok": True, "event": frame_event, "parent": parent.to_dict()}


    def send_message(
        self,
        content: str,
        *,
        session_id: str | None = None,
        run_immediately: bool = False,
        model: str | None = None,
        model_role: str = "coding",
    ) -> dict[str, Any]:
        session_id = session_id or active_session_id(self.root)
        if run_immediately and is_runtime_identity_query(content):
            return self._answer_runtime_identity_query(content, session_id=session_id)
        model_selection = self._operator_model_selection(model, model_role=model_role) if model else None
        payload = enqueue_message(
            self.root,
            content,
            session_id=session_id,
            model_selection=model_selection,
        )
        if run_immediately:
            loop_result = self.run_until_idle()
            payload["run"] = loop_result
        return payload

    def _answer_runtime_identity_query(self, content: str, *, session_id: str) -> dict[str, Any]:
        clean = str(content or "").strip()
        if not clean:
            raise ValueError("message content must not be empty")
        started_at = now_iso()
        answer = runtime_identity_answer()
        evidence = runtime_profile_evidence()
        user_event = self._append_session_event(
            session_id,
            {"type": "user_message", "role": "user", "content": clean},
        )
        self._write_runtime_status(
            status="running",
            current_role="runtime_profile",
            current_turn_id=user_event["event_id"],
            current_queue_id=None,
            current_user_message=clean,
            current_prompt_preview=None,
            current_stream_text="",
            current_model=None,
            current_model_reason="runtime profile deterministic answer",
            current_tool=None,
            last_error=None,
            last_system_note=None,
            current_started_at=started_at,
            current_finished_at=None,
            worker_running=self._worker_running(),
        )
        self._append_runtime_event(
            session_id,
            event_name="runtime_profile_answered",
            content=answer,
            details={
                "route": "runtime_identity",
                "schema_required": False,
                "streaming": False,
                "evidence": evidence,
            },
            turn_id=str(user_event["event_id"]),
            step_index=1,
            phase="RUNTIME_PROFILE",
        )
        assistant_event = self._append_session_event(
            session_id,
            {
                "type": "assistant_message",
                "role": "assistant",
                "content": answer,
                "reason_code": "runtime_profile_identity",
                "details": {"evidence": evidence},
                "turn_id": str(user_event["event_id"]),
                "step_index": 1,
            },
        )
        self._write_runtime_status(
            status="idle",
            current_role="runtime_profile",
            current_turn_id=None,
            current_queue_id=None,
            current_user_message=None,
            current_prompt_preview=None,
            current_stream_text=answer,
            current_phase="FINISH",
            current_model=None,
            current_model_reason="runtime profile deterministic answer",
            current_tool=None,
            last_error=None,
            last_system_note=None,
            last_llm_attempt_count=0,
            last_llm_raw_preview=None,
            last_llm_thinking_preview=None,
            last_llm_parse_issue=None,
            last_llm_schema_validation=None,
            raw_output_is_machine_json=None,
            schema_validation_ok=None,
            last_llm_stream_metadata=None,
            current_started_at=None,
            current_finished_at=now_iso(),
            worker_running=self._worker_running(),
        )
        return {
            "ok": True,
            "route": "runtime_identity",
            "session_id": session_id,
            "user_event_id": user_event["event_id"],
            "assistant_event_id": assistant_event["event_id"],
            "answer": answer,
            "evidence": evidence,
        }

    def simple_chat(self, content: str, *, session_id: str | None = None) -> dict[str, Any]:
        clean = str(content or "").strip()
        if not clean:
            raise ValueError("message content must not be empty")
        session_id = session_id or active_session_id(self.root)
        user_event = self._append_session_event(
            self.root,
            session_id,
            {"type": "user_message", "role": "user", "content": clean},
        )
        conversation = self._conversation_messages(session_id)
        model = str(self.router.models.get("reasoning") or self.config.get("models", {}).get("reasoning") or "gemma4:26b")
        timeout_seconds = int(self.runtime_config.get("chat_timeout_seconds") or 180)
        options = dict(self.ollama_options.get("reasoning", {}))
        started_at = now_iso()
        self._write_runtime_status(
            status="running",
            current_role="chat",
            current_turn_id=user_event["event_id"],
            current_user_message=clean,
            current_prompt_preview=clean,
            current_stream_text="",
            current_model=model,
            current_model_reason="plain chat mode",
            current_tool=None,
            last_error=None,
            worker_running=self._worker_running(),
        )
        self._append_runtime_event(
            session_id,
            event_name="llm_call_started",
            content=f"Plain chat LLM call started: {model}",
            details={"role": "chat", "model": model, "timeout_seconds": timeout_seconds},
            turn_id=str(user_event["event_id"]),
            step_index=1,
            phase="CHAT",
        )

        chunks = self.llm_backend.chat_stream(
            model=model,
            messages=conversation,
            options=options,
            timeout_seconds=timeout_seconds,
        )
        thinking_started = False
        content_started = False
        stream_parts: list[str] = []
        thinking_parts: list[str] = []
        content_parts: list[str] = []
        for chunk in chunks:
            message = chunk.get("message") or {}
            delta_thinking = str(message.get("thinking") or "")
            delta_content = str(message.get("content") or "")
            if delta_thinking:
                if not thinking_started:
                    stream_parts.append("Thinking...\n\n")
                    thinking_started = True
                stream_parts.append(delta_thinking)
                thinking_parts.append(delta_thinking)
            if delta_content:
                if thinking_started and not content_started:
                    stream_parts.append("\n\n")
                content_started = True
                stream_parts.append(delta_content)
                content_parts.append(delta_content)
            current_stream = "".join(stream_parts)
            delta_stream = self._format_llm_stream_text(
                thinking_text=delta_thinking,
                content_text=delta_content,
            )
            self._append_runtime_event(
                session_id,
                event_name="llm_stream_chunk",
                content=delta_stream,
                details={
                    "role": "chat",
                    "model": model,
                    "delta_content": delta_content,
                    "delta_thinking": delta_thinking,
                    "content_text": delta_content,
                    "thinking_text": delta_thinking,
                    "accumulated_content_chars": len("".join(content_parts)),
                    "accumulated_thinking_chars": len("".join(thinking_parts)),
                },
                turn_id=str(user_event["event_id"]),
                step_index=1,
                phase="CHAT",
            )
            self._write_runtime_status(
                status="running",
                current_role="chat",
                current_turn_id=user_event["event_id"],
                current_user_message=clean,
                current_prompt_preview=clean,
                current_stream_text=current_stream[-12000:],
                current_model=model,
                current_model_reason="plain chat mode",
                worker_running=self._worker_running(),
            )

        final_stream = "".join(stream_parts)
        self._append_runtime_event(
            session_id,
            event_name="llm_call_finished",
            content=final_stream,
            details={
                "role": "chat",
                "model": model,
                "content_text": "".join(content_parts),
                "thinking_text": "".join(thinking_parts),
            },
            turn_id=str(user_event["event_id"]),
            step_index=1,
            phase="CHAT",
        )
        assistant_event = self._append_session_event(
            self.root,
            session_id,
            {
                "type": "assistant_message",
                "role": "assistant",
                "content": final_stream,
                "model": model,
                "thinking_text": "".join(thinking_parts),
                "content_text": "".join(content_parts),
            },
        )
        self._write_runtime_status(
            status="idle",
            current_role="chat",
            current_turn_id=None,
            current_queue_id=None,
            current_user_message=clean,
            current_prompt_preview=clean,
            current_stream_text=final_stream[-12000:],
            current_model=model,
            current_model_reason="plain chat mode",
            current_tool=None,
            last_error=None,
            last_llm_started_at=started_at,
            last_llm_finished_at=now_iso(),
            last_llm_duration_ms=None,
            last_llm_attempt_count=1,
            last_llm_raw_preview=final_stream[:1000],
            worker_running=self._worker_running(),
        )
        return {"ok": True, "session_id": session_id, "user_event_id": user_event["event_id"], "assistant_event_id": assistant_event["event_id"]}


    def _dedicated_llm_workspace_enabled(self) -> bool:
        return bool(self.runtime_config.get("dedicated_llm_workspace", True))

    @staticmethod
    def _message_requests_runtime_continuation(message: str) -> bool:
        text = str(message or "").lower()
        markers = (
            "続け",
            "続きを",
            "再開",
            "前回",
            "未完",
            "同じworkspace",
            "同じ work",
            "workspace",
            "events",
            "resume",
            "continue",
            "continuation",
            "previous",
            "last workspace",
        )
        return any(marker in text for marker in markers)

    @staticmethod
    def _runtime_status_allows_workspace_resume(status: dict[str, Any]) -> bool:
        if not status:
            return False
        status_text = "\n".join(
            str(status.get(key) or "")
            for key in ("status", "current_phase", "last_error", "last_system_note", "current_stream_text")
        ).lower()
        return (
            "step limit" in status_text
            or "interrupted_by_operator" in status_text
            or "operator_interrupt" in status_text
            or "operator interrupt" in status_text
        )

    def _resume_workspace_for_message(self, message: str) -> Path | None:
        if not self._dedicated_llm_workspace_enabled():
            return None
        if not self._message_requests_runtime_continuation(message):
            return None
        status = read_json(self.paths.runtime_status_path, fallback={})
        if not self._runtime_status_allows_workspace_resume(status):
            return None
        candidate_text = str(status.get("current_llm_workspace") or status.get("last_llm_workspace") or "").strip()
        if not candidate_text:
            return None
        candidate = Path(candidate_text).expanduser().resolve()
        runs_dir = self.paths.llm_runs_dir.resolve()
        try:
            candidate.relative_to(runs_dir)
        except ValueError:
            return None
        if not candidate.exists() or not candidate.is_dir():
            return None
        return candidate

    def _event_sourced_steps_for_workspace(self, *, session_id: str, workspace: Path) -> list[dict[str, Any]]:
        target = str(Path(workspace).resolve())
        events = read_jsonl(self.paths.session_events_path(session_id))
        tool_calls: dict[tuple[str, int, str], dict[str, Any]] = {}
        steps: list[dict[str, Any]] = []
        for event in events:
            if str(event.get("llm_workspace") or "") != target:
                continue
            event_type = str(event.get("type") or "")
            tool_name = str(event.get("tool_name") or "")
            if not tool_name:
                continue
            turn = str(event.get("turn_id") or "")
            try:
                index = int(event.get("step_index") or 0)
            except (TypeError, ValueError):
                index = 0
            key = (turn, index, tool_name)
            if event_type == "tool_call":
                args = event.get("tool_args") if isinstance(event.get("tool_args"), dict) else {}
                tool_calls[key] = dict(args)
                continue
            if event_type != "tool_result":
                continue
            args = dict(tool_calls.get(key) or {})
            raw_result = event.get("content")
            result: dict[str, Any]
            if isinstance(raw_result, str) and raw_result.strip():
                try:
                    parsed = json.loads(raw_result)
                except json.JSONDecodeError:
                    parsed = {}
                result = dict(parsed) if isinstance(parsed, dict) else {}
            else:
                result = {}
            if "ok" not in result:
                result["ok"] = bool(event.get("ok"))
            steps.append({"tool_name": tool_name, "tool_args": args, "tool_result": result})
        return steps

    def _prepare_turn_workspace(self, *, turn_id: str, resume_workspace: Path | None = None) -> Path:
        if not self._dedicated_llm_workspace_enabled():
            self.execution_root = self.base_execution_root
            self.tools = ToolExecutor(self.execution_root, content_chunk_max_bytes=self.tool_content_chunk_bytes)
            return self.execution_root
        workspace = Path(resume_workspace).resolve() if resume_workspace is not None else (self.paths.llm_runs_dir / turn_id).resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        self.execution_root = workspace
        self.tools = ToolExecutor(self.execution_root, content_chunk_max_bytes=self.tool_content_chunk_bytes)
        return workspace


    def _conversation_messages(self, session_id: str) -> list[dict[str, str]]:
        # Increased limit to 100 to capture enough history for complex loops
        current_frame = self.frame_manager.current_frame()
        if current_frame is not None and current_frame.session_events:
            events = current_frame.session_events[-100:]
        else:
            events = read_jsonl(self.paths.session_events_path(session_id), limit=100)
        messages: list[dict[str, str]] = []
        if current_frame is not None and current_frame.inherited_context:
            inherited_context = dict(current_frame.inherited_context)
            work_package = self.frame_manager.work_package_for(current_frame)
            parent_working_memory = self.frame_manager.parent_working_memory_for(current_frame)
            if work_package is not None:
                inherited_context["work_package"] = work_package
            if parent_working_memory is not None:
                inherited_context["parent_working_memory"] = {
                    "observations": list(parent_working_memory.observations),
                    "current_focus": str(parent_working_memory.current_focus or ""),
                    "unresolved_questions": list(parent_working_memory.unresolved_questions),
                    "avoid_repeating": list(parent_working_memory.avoid_repeating),
                    "child_tasks": list(parent_working_memory.child_tasks),
                    "completed_child_tasks": list(parent_working_memory.completed_child_tasks),
                }
            messages.append(
                {
                    "role": "user",
                    "content": "[Inherited Frame Context] "
                    + json.dumps(inherited_context, ensure_ascii=False),
                }
            )
        for event in events:
            event_type = str(event.get("type") or "")
            role = str(event.get("role") or "")
            if event_type == "user_message" and role == "user":
                messages.append({"role": "user", "content": str(event.get("content") or "")})
            elif event_type == "assistant_message" and role == "assistant":
                content = str(event.get("content_text") or event.get("content") or "")
                messages.append({"role": "assistant", "content": content})
            elif event_type == "system_note":
                content = str(event.get("content") or "")
                # We map system_note to 'user' role with a clear prefix so the LLM treats it as feedback
                messages.append({"role": "user", "content": f"[System Note] {content}"})
            elif event_type == "tool_call":
                tool_name = str(event.get("tool_name") or "")
                tool_args = event.get("tool_args") or {}
                # Represent tool calls in context
                messages.append({"role": "assistant", "content": f"Tool Call: {tool_name} {json.dumps(tool_args, ensure_ascii=False)}"})
            elif event_type == "tool_result":
                tool_name = str(event.get("tool_name") or "")
                content = str(event.get("content") or "")
                # Represent tool results in context
                messages.append({"role": "user", "content": f"Tool Result ({tool_name}): {content}"})
        return messages

    def set_goal(self, text: str) -> dict[str, Any]:
        from p4_core.workspace import update_goal

        return update_goal(self.root, text)

    def run_until_idle(
        self,
        *,
        max_work_items: int | None = None,
        selection_override: dict[str, str] | None = None,
        extra_prompt: str | None = None,
    ) -> dict[str, Any]:
        processed = 0
        last_result: dict[str, Any] | None = None
        while True:
            if max_work_items is not None and processed >= max_work_items:
                break
            item = pop_next_queue_item(self.root)
            if item is None:
                current_status = read_json(self.paths.runtime_status_path, fallback={})
                if str(current_status.get("status") or "") != "interrupted_by_operator":
                    self._write_runtime_status(
                        status="idle",
                        current_turn_id=None,
                        current_queue_id=None,
                        current_user_message=None,
                        current_prompt_preview=None,
                        current_stream_text="",
                        current_plan=None,
                        current_phase=None,
                        current_started_at=None,
                        current_tool=None,
                        current_operation_id=None,
                        current_llm_workspace=None,
                    )
                break
            processed += 1
            item_selection_override = selection_override
            if item_selection_override is None:
                raw_selection = item.get("model_selection")
                if isinstance(raw_selection, dict):
                    item_selection_override = {
                        "role": str(raw_selection.get("role") or "coding"),
                        "model": str(raw_selection.get("model") or ""),
                        "reason": str(raw_selection.get("reason") or "operator selected model from queued message"),
                    }
            last_result = self._process_queue_item(
                item,
                selection_override=item_selection_override,
                extra_prompt=extra_prompt,
            )
        return {
            "ok": True,
            "processed": processed,
            "last_result": last_result,
            "pending_queue": len(queue_items(self.root)),
        }

    def _append_operator_interrupt_event(self, *, operator_reason: str = "operator requested worker stop") -> dict[str, Any]:
        session_id = active_session_id(self.root)
        runtime_status = read_json(self.paths.runtime_status_path, fallback={})
        details = {
            "status": "interrupted",
            "reason_code": "interrupted_by_operator",
            "operator_reason": operator_reason,
            "stopped_at_step": runtime_status.get("current_step") or runtime_status.get("current_phase"),
            "current_phase": runtime_status.get("current_phase"),
            "current_tool": runtime_status.get("current_tool"),
            "current_model": runtime_status.get("current_model"),
            "contract_state": runtime_status.get("contract_state"),
            "missing_requirements": runtime_status.get("missing_requirements") or [],
            "latest_llm_workspace": runtime_status.get("last_llm_workspace") or runtime_status.get("current_llm_workspace"),
        }
        event = self._append_session_event(
            session_id,
            {
                "type": "system_note",
                "role": "system",
                "content": f"Run interrupted by operator: {operator_reason}",
                "code": "operator_interrupt",
                "reason_code": "interrupted_by_operator",
                "details": details,
            },
        )
        operation_id = str(runtime_status.get("current_operation_id") or "")
        if operation_id:
            self._append_session_event(
                session_id,
                {
                    "type": "operation",
                    "role": "system",
                    "operation_id": operation_id,
                    "title": "Runtime queue item",
                    "detail": str(runtime_status.get("current_user_message") or ""),
                    "status": "interrupted_by_operator",
                    "started_at": runtime_status.get("current_started_at"),
                    "finished_at": now_iso(),
                    "output_preview": f"interrupted_by_operator: {operator_reason}",
                    "reason_code": "interrupted_by_operator",
                },
            )
        return event

    def record_operator_interrupt(self, *, operator_reason: str = "operator requested runtime stop") -> dict[str, Any]:
        event = self._append_operator_interrupt_event(operator_reason=operator_reason)
        self._write_runtime_status(
            status="interrupted_by_operator",
            current_phase="OPERATOR_INTERRUPT",
            current_finished_at=now_iso(),
            current_process_pid=None,
            worker_running=False,
            last_system_note=operator_reason,
        )
        return {
            "ok": False,
            "status": "interrupted_by_operator",
            "reason_code": "interrupted_by_operator",
            "operator_reason": operator_reason,
            "event_id": event.get("event_id"),
        }

    def worker_loop(self) -> None:
        poll_seconds = int(self.config.get("runtime", {}).get("worker_poll_seconds") or 2)
        self.paths.worker_pid_path.write_text(str(os.getpid()), encoding="utf-8")
        self._write_runtime_status(status="worker_idle", worker_running=True)
        stop_requested = False

        def _handle_stop(signum: int, frame: Any) -> None:
            del signum
            del frame
            nonlocal stop_requested
            stop_requested = True

        signal.signal(signal.SIGTERM, _handle_stop)
        signal.signal(signal.SIGINT, _handle_stop)

        while not stop_requested:
            self.run_until_idle(max_work_items=1)
            time.sleep(poll_seconds)

        self._append_operator_interrupt_event(operator_reason="worker received stop signal")
        self._write_runtime_status(
            status="interrupted_by_operator",
            current_phase="OPERATOR_INTERRUPT",
            last_system_note="worker received stop signal",
            current_finished_at=now_iso(),
            worker_running=False,
        )
        if self.paths.worker_pid_path.exists():
            self.paths.worker_pid_path.unlink()

    def status_snapshot(self) -> dict[str, Any]:
        session_id = active_session_id(self.root)
        self._repair_stale_operator_interrupt()
        goal = read_json(self.paths.goal_path, fallback={})
        meta = read_json(self.paths.session_meta_path(session_id), fallback={})
        runtime = read_json(self.paths.runtime_status_path, fallback={})
        live_worker_running = self._worker_running()
        if runtime.get("worker_running") != live_worker_running:
            runtime = {**runtime, "worker_running": live_worker_running}
            write_json(self.paths.runtime_status_path, runtime)
        events = read_jsonl(self.paths.session_events_path(session_id), limit=20)
        prompts = read_jsonl(self.paths.session_prompts_path(session_id), limit=5)
        transcript = [event for event in events if event.get("type") in {"user_message", "assistant_message", "finish"}]
        last_reply = None
        for event in reversed(transcript):
            if event.get("type") in {"assistant_message", "finish"}:
                last_reply = event
                break
        return {
            "goal": goal,
            "runtime": runtime,
            "session": meta,
            "pending_queue": len(queue_items(self.root)),
            "recent_events": events,
            "recent_prompts": prompts,
            "recent_transcript": transcript,
            "last_reply": last_reply,
            "frames": self._frame_snapshot(),
        }

    def _process_queue_item(
        self,
        item: dict[str, Any],
        *,
        selection_override: dict[str, str] | None = None,
        extra_prompt: str | None = None,
    ) -> dict[str, Any]:
        session_id = str(item.get("session_id") or active_session_id(self.root))
        recent_user_message = str(item.get("content") or "")
        configured_max_steps = int(self.config.get("runtime", {}).get("max_steps_per_message") or 12)
        max_steps = self._effective_max_steps_per_message(user_message=recent_user_message, configured=configured_max_steps)
        queue_id = str(item.get("queue_id") or "")
        operation_id = str(item.get("operation_id") or queue_id or uuid.uuid4().hex)
        turn_id = uuid.uuid4().hex
        resume_workspace = self._resume_workspace_for_message(recent_user_message)
        resume_existing_frame = resume_workspace is not None and self.frame_manager.current_frame() is not None
        turn_workspace = self._prepare_turn_workspace(turn_id=turn_id, resume_workspace=resume_workspace)
        resumed_steps = (
            self._event_sourced_steps_for_workspace(session_id=session_id, workspace=turn_workspace)
            if resume_workspace is not None
            else []
        )
        operation_started_at = now_iso()

        def finish_operation(status: str, *, output_preview: str = "") -> None:
            self._append_session_event(
                self.root,
                session_id,
                {
                    "type": "operation",
                    "role": "system",
                    "operation_id": operation_id,
                    "title": "Runtime queue item",
                    "detail": recent_user_message,
                    "status": status,
                    "started_at": operation_started_at,
                    "finished_at": now_iso(),
                    "output_preview": str(output_preview or "")[-4000:],
                    "turn_id": turn_id,
                    "queue_id": queue_id,
                    "step_index": max_steps,
                    "llm_workspace": str(turn_workspace),
                },
            )

        self._write_runtime_status(
            status="running",
            current_turn_id=turn_id,
            current_queue_id=queue_id,
            current_user_message=recent_user_message,
            current_stream_text="",
            current_operation_id=operation_id,
            current_started_at=operation_started_at,
            current_finished_at=None,
            current_llm_workspace=str(turn_workspace),
            last_llm_workspace=str(turn_workspace),
            current_process_pid=os.getpid(),
            worker_running=self._worker_running(),
        )
        self._append_session_event(
            self.root,
            session_id,
            {
                "type": "operation",
                "role": "system",
                "operation_id": operation_id,
                "title": "Runtime queue item",
                "detail": recent_user_message,
                "status": "running",
                "started_at": operation_started_at,
                "turn_id": turn_id,
                "queue_id": queue_id,
                "step_index": 0,
                "llm_workspace": str(turn_workspace),
            },
        )
        self._start_turn_frame(user_message=recent_user_message, resume_existing=resume_existing_frame)
        if resume_workspace is not None:
            self._append_session_event(
                self.root,
                session_id,
                {
                    "type": "system_note",
                    "role": "system",
                    "content": "未完runの継続要求として前回workspaceとevent由来tool履歴を再利用します。",
                    "code": "workspace_resume",
                    "reason_code": "resume_incomplete_run_workspace",
                    "details": {
                        "resumed_workspace": str(turn_workspace),
                        "resumed_step_count": len(resumed_steps),
                        "resume_existing_frame": bool(resume_existing_frame),
                        "contract_state": "continuation",
                        "blocked_by": "runtime_workspace_controller",
                        "next_required_action": "continue from event-sourced progress in the resumed workspace",
                    },
                    "turn_id": turn_id,
                    "queue_id": queue_id,
                    "step_index": 0,
                    "llm_workspace": str(turn_workspace),
                },
            )
        steps: list[dict[str, Any]] = list(resumed_steps)
        planning_note = self._build_planning_note(user_message=recent_user_message, goal_text=str(read_json(self.paths.goal_path, fallback={}).get("text") or ""))
        append_jsonl(
            self.paths.planning_path,
            {
                "timestamp": now_iso(),
                "turn_id": turn_id,
                "queue_id": queue_id,
                "session_id": session_id,
                "note": planning_note,
                "llm_workspace": str(turn_workspace),
            },
        )
        self._append_session_event(
            self.root,
            session_id,
            {
                "type": "planning_note",
                "role": "system",
                "content": planning_note,
                "turn_id": turn_id,
                "queue_id": queue_id,
                "step_index": 0,
                "llm_workspace": str(turn_workspace),
            },
        )
        for step_index in range(1, max_steps + 1):
            current_frame = self.frame_manager.current_frame()
            if current_frame is not None:
                frame_step_count = self.frame_manager.increment_step()
                frame_step_limit = self._effective_frame_step_limit(
                    current_frame=current_frame,
                    turn_max_steps=max_steps,
                )
                if frame_step_count > frame_step_limit:
                    if current_frame.parent_frame_id is not None:
                        self._handle_return_to_parent(
                            session_id=session_id,
                            turn_id=turn_id,
                            queue_id=queue_id,
                            step_index=step_index,
                            tool_args={
                                "summary": "ステップ上限に達したため親フレームに戻ります。",
                                "findings": list(current_frame.working_memory.observations),
                            },
                            turn_workspace=turn_workspace,
                        )
                        continue
                    final_answer = "ステップ上限に達しました。現時点の結果を報告します。"
                    self._append_session_event(
                        session_id,
                        {
                            "type": "finish",
                            "role": "assistant",
                            "content": final_answer,
                            "turn_id": turn_id,
                            "queue_id": queue_id,
                            "step_index": step_index,
                            "llm_workspace": str(turn_workspace),
                            "details": {
                                "frame_step_count": frame_step_count,
                                "frame_step_limit": frame_step_limit,
                                "effective_turn_max_steps": max_steps,
                            },
                        },
                    )
                    finish_operation("failed", output_preview=final_answer)
                    return {"ok": False, "session_id": session_id, "steps": steps, "final_answer": final_answer, "error": "frame step limit reached"}
                recent_events = self._recent_events_for_action_context(
                    session_id=session_id,
                    current_frame=current_frame,
                    limit=30,
                )
            else:
                recent_events = self._recent_events_for_action_context(
                    session_id=session_id,
                    current_frame=None,
                    limit=30,
                )
            goal_text = str(read_json(self.paths.goal_path, fallback={}).get("text") or "")
            current_phase = self._current_phase(user_message=recent_user_message, steps=steps, recent_events=recent_events)
            selection = selection_override or self.router.select_model(
                goal_text=goal_text,
                pending_message=recent_user_message,
                recent_events=recent_events,
                current_phase=current_phase,
            )
            pending_task_for_auto_open = self._pending_plan_child_task()
            pending_plan_open = None
            if pending_task_for_auto_open is not None:
                progress_gate_state = self._implementation_task_progress_state(
                    user_message=recent_user_message,
                    steps=steps,
                    session_id=session_id,
                    turn_workspace=turn_workspace,
                )
                if self._plan_run_test_should_wait_for_progress(
                    pending_task=pending_task_for_auto_open,
                    progress_state=progress_gate_state,
                ):
                    self._append_session_event(
                        self.root,
                        session_id,
                        {
                            "type": "system_note",
                            "role": "system",
                            "content": (
                                "PLAN_EXECUTION paused before verifier WorkUnit because implementation progress is incomplete. "
                                "Satisfy implementation/tests progress before opening the planned run_test WorkUnit."
                            ),
                            "code": "plan_execution_paused_for_progress",
                            "reason_code": "implementation_progress_before_plan_verifier",
                            "details": {
                                "pending_task": pending_task_for_auto_open,
                                "progress_phase": progress_gate_state.get("phase"),
                                "missing_requirements": list(progress_gate_state.get("missing_requirements") or []),
                                "allowed_next_actions": list(progress_gate_state.get("allowed_next_actions") or []),
                                "blocked_by": "plan_execution_contract",
                                "next_required_action": "satisfy implementation_task_progress before run_test WorkUnit",
                            },
                            "turn_id": turn_id,
                            "queue_id": queue_id,
                            "step_index": step_index,
                            "llm_workspace": str(turn_workspace),
                        },
                    )
                else:
                    pending_plan_open = self._auto_open_pending_plan_child(
                        session_id=session_id,
                        turn_id=turn_id,
                        queue_id=queue_id,
                        step_index=step_index,
                        turn_workspace=turn_workspace,
                        user_message=recent_user_message,
                        current_model=str(selection.get("model") or ""),
                    )
            if pending_plan_open is not None:
                steps.extend(list(pending_plan_open.get("auto_steps") or []))
                open_message = (
                    f"Opened pending planned WorkUnit ({(pending_plan_open.get('event') or {}).get('content') or 'PLAN_EXECUTION'})."
                    if bool(pending_plan_open.get("ok"))
                    else str((pending_plan_open.get("event") or {}).get("content") or pending_plan_open.get("error") or "pending planned WorkUnit blocked")
                )
                self._write_runtime_status(
                    status="running",
                    current_role=selection["role"],
                    current_turn_id=turn_id,
                    current_queue_id=queue_id,
                    current_user_message=recent_user_message,
                    current_prompt_preview=None,
                    current_stream_text=open_message,
                    current_plan=planning_note,
                    current_phase="PLAN_EXECUTION",
                    current_model=selection["model"],
                    current_model_reason=selection["reason"],
                    current_tool="open_child_frame",
                    current_llm_workspace=str(turn_workspace),
                    last_llm_workspace=str(turn_workspace),
                    last_error=None if bool(pending_plan_open.get("ok")) else open_message,
                    current_finished_at=now_iso(),
                    worker_running=self._worker_running(),
                )
                if bool(pending_plan_open.get("terminal_failure")):
                    finish_operation("failed", output_preview=open_message)
                    return {
                        "ok": False,
                        "session_id": session_id,
                        "steps": steps,
                        "final_answer": open_message,
                        "error": open_message,
                    }
                continue
            block_count, trigger_reason = self._consecutive_finish_block_summary(recent_events)
            if steps and block_count >= 3:
                fallback_answer = self._synthesize_terminal_final_answer(
                    goal_text=goal_text,
                    user_message=recent_user_message,
                    steps=steps,
                ) or "judge が継続して失敗したため、観測済み証拠に基づいて終了します。"
                acceptance = self._finish_acceptance_evaluation(
                    user_message=recent_user_message,
                    final_answer=fallback_answer,
                    steps=steps,
                )
                accepted = self._finish_status_is_accepted(acceptance.get("status"))
                override = acceptance.get("acceptance_override") or {}
                override_reason = str(override.get("reason_code") or "")
                # trigger_reason ("judge_invalid_output" / "judge_error" / ...) を fallback ラベルに継承する。
                # 旧実装は accepted=False のとき一律 "judge_unavailable" に丸めていたが、
                # judge は到達しているケース (例: invalid_output) も同じラベルになり症状=原因の対応が壊れていた
                # (p4-coding-invariants Invariant 4 — variation と asymmetry の混同)。
                trigger_label = trigger_reason or "judge_unavailable"
                if accepted and override_reason:
                    finish_reason_code = override_reason
                elif accepted:
                    finish_reason_code = f"{trigger_label}_observation_accepted"
                else:
                    finish_reason_code = f"{trigger_label}_observation_rejected"
                # acceptance details に trigger を埋める (dashboard / 監査が原因を再構成可能に)
                acceptance = {**acceptance, "trigger_reason_code": trigger_label}
                self._append_session_event(
                    self.root,
                    session_id,
                    {
                        "type": "system_note",
                        "role": "system",
                        "content": (
                            f"judge が連続して完了をブロックしたため、観測ベースで受理しました ({finish_reason_code})。"
                            if accepted
                            else f"judge が利用できず観測フォールバックも満たさないため終了します ({finish_reason_code})。"
                        ),
                        "code": "judge_fallback_finish",
                        "reason_code": finish_reason_code,
                        "details": acceptance,
                        "turn_id": turn_id,
                        "queue_id": queue_id,
                        "step_index": step_index,
                        "llm_workspace": str(turn_workspace),
                    },
                )
                if accepted:
                    self._append_session_event(
                        self.root,
                        session_id,
                        {
                            "type": "finish",
                            "role": "assistant",
                            "content": fallback_answer,
                            "model": selection["model"],
                            "model_reason": f"{selection['reason']} + judge-fallback",
                            "llm_attempt_count": 0,
                            "turn_id": turn_id,
                            "queue_id": queue_id,
                            "step_index": step_index,
                            "llm_workspace": str(turn_workspace),
                        },
                    )
                self._write_runtime_status(
                    status="idle",
                    current_role=selection["role"],
                    current_turn_id=None,
                    current_queue_id=None,
                    current_user_message=None,
                    current_prompt_preview=None,
                    current_stream_text="",
                    current_plan=None,
                    current_phase="FINISH" if accepted else f"FAILED_DUE_TO_{trigger_label.upper()}",
                    current_model=selection["model"],
                    current_model_reason=selection["reason"],
                    current_tool="finish" if accepted else None,
                    current_operation_id=None,
                    current_llm_workspace=None,
                    last_llm_workspace=str(turn_workspace),
                    last_error=None if accepted else f"failed due to {trigger_label}",
                    last_system_note=None if accepted else f"連続 finish_blocked ({trigger_label}) と観測フォールバック失敗により終了。",
                    current_started_at=None,
                    current_finished_at=now_iso(),
                    worker_running=self._worker_running(),
                )
                finish_operation("finished" if accepted else "failed", output_preview=fallback_answer)
                return {
                    "ok": accepted,
                    "session_id": session_id,
                    "steps": steps,
                    "final_answer": fallback_answer,
                    "acceptance": acceptance,
                    "error": None if accepted else f"failed_due_to_{trigger_label}",
                }
            controller_finish = self._controller_terminal_finish(
                selection=selection,
                goal_text=goal_text,
                user_message=recent_user_message,
                steps=steps,
            )
            controller_finish_reason_code = "grounded_terminal_evidence"
            controller_finish_note = "コントローラーによる自動完了: ターミナルの実行結果に基づき、根拠のある最終回答を合成しました"
            controller_finish_model_reason = f"{selection['reason']} + controller-finish"
            environment_dead_end = self._environment_observation_dead_end_response(
                user_message=recent_user_message,
                steps=steps,
            )
            if environment_dead_end is not None:
                self._append_session_event(
                    self.root,
                    session_id,
                    {
                        "type": "system_note",
                        "role": "system",
                        "content": "環境観測だけでは完了条件を満たせないため、具体化を要求して停止します。",
                        "code": "contract_incomplete",
                        "reason_code": "environment_observation_dead_end",
                        "details": {
                            "contract_state": "incomplete",
                            "missing_requirements": ["concrete_user_goal"],
                            "successful_tools": [str(step.get("tool_name") or "") for step in steps],
                            "failure_type": "contract_incomplete",
                            "blocked_by": "finish_contract_environment_observation",
                            "allowed_next_actions": ["final_answer ask user for a concrete goal"],
                            "suggested_fix": "環境観測だけで判断できる完了条件がないため、ユーザーに具体的な目的を確認してください。",
                            "next_required_action": "ask the user to provide a concrete actionable goal",
                        },
                        "turn_id": turn_id,
                        "queue_id": queue_id,
                        "step_index": step_index,
                        "llm_workspace": str(turn_workspace),
                    },
                )
                self._append_session_event(
                    self.root,
                    session_id,
                    {
                        "type": "finish",
                        "role": "assistant",
                        "content": environment_dead_end,
                        "model": selection["model"],
                        "model_reason": f"{selection['reason']} + contract-incomplete",
                        "llm_attempt_count": 0,
                        "turn_id": turn_id,
                        "queue_id": queue_id,
                        "step_index": step_index,
                        "llm_workspace": str(turn_workspace),
                    },
                )
                self._write_runtime_status(
                    status="idle",
                    current_role=selection["role"],
                    current_turn_id=None,
                    current_queue_id=None,
                    current_user_message=None,
                    current_prompt_preview=None,
                    current_stream_text="",
                    current_plan=None,
                    current_phase="CONTRACT_INCOMPLETE",
                    current_model=selection["model"],
                    current_model_reason=f"{selection['reason']} + contract-incomplete",
                    current_tool="finish",
                    current_operation_id=None,
                    current_llm_workspace=None,
                    last_llm_workspace=str(turn_workspace),
                    last_error="environment observation dead end",
                    last_system_note="環境観測だけでは完了条件を満たせません。",
                    current_started_at=None,
                    current_finished_at=now_iso(),
                    worker_running=self._worker_running(),
                )
                finish_operation("blocked", output_preview=environment_dead_end)
                return {
                    "ok": False,
                    "session_id": session_id,
                    "steps": steps,
                    "final_answer": environment_dead_end,
                    "error": "environment_observation_dead_end",
                }
            current_frame = self.frame_manager.current_frame()
            implementation_finish = None
            if current_frame is None or current_frame.parent_frame_id is None:
                implementation_finish = self._implementation_contract_final_answer(
                    user_message=recent_user_message,
                    steps=steps,
                    session_id=session_id,
                    turn_workspace=turn_workspace,
                )
            if implementation_finish is not None:
                controller_finish = implementation_finish
                controller_finish_reason_code = "implementation_contract_satisfied"
                controller_finish_note = "コントローラーによる自動完了: 実装タスクのCompletionContractが満たされたため、検証証拠に基づく最終回答を合成しました"
                controller_finish_model_reason = f"{selection['reason']} + implementation-contract-finish"
            if controller_finish is not None and current_frame is not None and current_frame.parent_frame_id is not None:
                controller_finish = None
            if controller_finish is not None and self._latest_successful_tool_name(steps) != "run_command":
                controller_finish = None
            if controller_finish is not None and self._controller_finish_blocked_for_current_evidence(recent_events):
                controller_finish = None
            if controller_finish is not None:
                acceptance = self._finish_acceptance_evaluation(
                    user_message=recent_user_message,
                    final_answer=controller_finish,
                    steps=steps,
                )
                self._append_session_event(
                    self.root,
                    session_id,
                    {
                        "type": "system_note",
                        "role": "system",
                        "content": f"完了受理判定: {acceptance.get('status')}",
                        "code": "finish_acceptance",
                        "reason_code": self._finish_acceptance_reason_code(acceptance),
                        "details": acceptance,
                        "turn_id": turn_id,
                        "queue_id": queue_id,
                        "step_index": step_index,
                        "llm_workspace": str(turn_workspace),
                    },
                )
                if not self._finish_status_is_accepted(acceptance.get("status")):
                    block_text = self._finish_acceptance_block_text(acceptance)
                    self._append_session_event(
                        self.root,
                        session_id,
                        {
                            "type": "system_note",
                            "role": "system",
                            "content": block_text,
                            "code": "finish_blocked",
                            "reason_code": "finish_acceptance_failed",
                            "details": {
                                **acceptance,
                                "failure_type": "finish_acceptance_failed",
                                "blocked_by": "finish_acceptance_gate",
                                "allowed_next_actions": list(acceptance.get("allowed_next_actions") or ["revise evidence or final_answer before retrying finish"]),
                                "suggested_fix": str(acceptance.get("suggested_fix") or acceptance.get("reason") or block_text),
                                "next_required_action": str(acceptance.get("next_required_action") or acceptance.get("suggested_fix") or "revise the blocked finish evidence before retrying"),
                            },
                            "turn_id": turn_id,
                            "queue_id": queue_id,
                            "step_index": step_index,
                            "llm_workspace": str(turn_workspace),
                        },
                    )
                    self._write_runtime_status(
                        status="running",
                        current_role=selection["role"],
                        current_turn_id=turn_id,
                        current_queue_id=queue_id,
                        current_user_message=recent_user_message,
                        current_prompt_preview=None,
                        current_stream_text=f"Finish blocked. {block_text}",
                        current_plan=None,
                        current_phase="REVISE_FROM_ACCEPTANCE",
                        current_model=selection["model"],
                        current_model_reason=selection["reason"],
                        current_tool=None,
                        current_llm_workspace=str(turn_workspace),
                        last_llm_workspace=str(turn_workspace),
                        last_error="finish blocked by acceptance check",
                        last_system_note=block_text,
                        worker_running=self._worker_running(),
                    )
                    continue
                self._append_session_event(
                    self.root,
                    session_id,
                    {
                        "type": "system_note",
                        "role": "system",
                        "content": controller_finish_note,
                        "code": "controller_finish",
                        "reason_code": controller_finish_reason_code,
                        "turn_id": turn_id,
                        "queue_id": queue_id,
                        "step_index": step_index,
                        "llm_workspace": str(turn_workspace),
                    },
                )
                self._append_session_event(
                    self.root,
                    session_id,
                    {
                        "type": "finish",
                        "role": "assistant",
                        "content": controller_finish,
                        "model": selection["model"],
                        "model_reason": controller_finish_model_reason,
                        "llm_attempt_count": 0,
                        "turn_id": turn_id,
                        "queue_id": queue_id,
                        "step_index": step_index,
                        "llm_workspace": str(turn_workspace),
                    },
                )
                self._write_runtime_status(
                    status="idle",
                    current_role=selection["role"],
                    current_turn_id=None,
                    current_queue_id=None,
                    current_user_message=None,
                    current_prompt_preview=None,
                    current_stream_text="",
                    current_plan=None,
                    current_phase="FINISH",
                    current_model=selection["model"],
                    current_model_reason=controller_finish_model_reason,
                    current_tool="finish",
                    current_operation_id=None,
                    current_llm_workspace=None,
                    last_llm_workspace=str(turn_workspace),
                    last_error=None,
                    last_system_note=None,
                    current_started_at=None,
                    current_finished_at=now_iso(),
                    worker_running=self._worker_running(),
                )
                finish_operation("finished", output_preview=controller_finish)
                return {"ok": True, "session_id": session_id, "steps": steps, "final_answer": controller_finish}
            if self._append_semantic_implementation_review_if_needed(
                session_id=session_id,
                turn_id=turn_id,
                queue_id=queue_id,
                step_index=step_index,
                turn_workspace=turn_workspace,
                user_message=recent_user_message,
                steps=steps,
                current_model=str(selection.get("model") or ""),
                trigger="before_unittest",
            ):
                message = (
                    "unittest 自動実行前に、semantic implementation review をP4へ渡しました。"
                    " レビューで未達があれば修正し、満たしていればunittestを実行してください。"
                )
                self._write_runtime_status(
                    status="running",
                    current_role=selection["role"],
                    current_turn_id=turn_id,
                    current_queue_id=queue_id,
                    current_user_message=recent_user_message,
                    current_prompt_preview=None,
                    current_stream_text=message,
                    current_plan=planning_note,
                    current_phase="SEMANTIC_REVIEW",
                    current_model=selection["model"],
                    current_model_reason=selection["reason"],
                    current_tool=None,
                    current_llm_workspace=str(turn_workspace),
                    last_llm_workspace=str(turn_workspace),
                    last_system_note=message,
                    worker_running=self._worker_running(),
                )
                continue
            recovery_action = self._completion_contract_recovery_action(
                session_id=session_id,
                user_message=recent_user_message,
                steps=steps,
                step_index=step_index,
                max_steps=max_steps,
                turn_workspace=turn_workspace,
            )
            if recovery_action is not None:
                recovery_tool_name = str(recovery_action.get("tool_name") or "")
                recovery_tool_args = dict(recovery_action.get("tool_args") or {})
                recovery_reason = str(recovery_action.get("reason_code") or "completion_contract_recovery")
                recovery_decision = str(recovery_action.get("system_decision") or "")
                self._append_session_event(
                    self.root,
                    session_id,
                    {
                        "type": "system_note",
                        "role": "system",
                        "content": recovery_decision,
                        "code": "completion_contract_recovery",
                        "reason_code": recovery_reason,
                        "details": {
                            "tool_name": recovery_tool_name,
                            "tool_args": recovery_tool_args,
                        },
                        "turn_id": turn_id,
                        "queue_id": queue_id,
                        "step_index": step_index,
                        "llm_workspace": str(turn_workspace),
                    },
                )
                self._append_session_event(
                    self.root,
                    session_id,
                    {
                        "type": "tool_call",
                        "tool_name": recovery_tool_name,
                        "tool_args": recovery_tool_args,
                        "turn_id": turn_id,
                        "queue_id": queue_id,
                        "step_index": step_index,
                        "llm_workspace": str(turn_workspace),
                        "reason_code": recovery_reason,
                    },
                )
                self._append_runtime_event(
                    session_id,
                    event_name="tool_call_started",
                    content=(
                        f"Running command via {recovery_tool_args.get('shell') or 'auto'}:\n{recovery_tool_args.get('command')}"
                        if recovery_tool_name == "run_command"
                        else f"Running tool: {recovery_tool_name}"
                    ),
                    details={
                        "tool_name": recovery_tool_name,
                        "tool_args": recovery_tool_args,
                        "reason_code": recovery_reason,
                    },
                    turn_id=turn_id,
                    queue_id=queue_id,
                    step_index=step_index,
                    llm_workspace=str(turn_workspace),
                    phase="COMPLETION_CONTRACT_RECOVERY",
                )
                self._write_runtime_status(
                    status="running_tool",
                    current_role=selection["role"],
                    current_turn_id=turn_id,
                    current_queue_id=queue_id,
                    current_user_message=recent_user_message,
                    current_prompt_preview=None,
                    current_stream_text=(
                        f"Running command via {recovery_tool_args.get('shell') or 'auto'}:\n{recovery_tool_args.get('command')}"
                        if recovery_tool_name == "run_command"
                        else ""
                    ),
                    current_plan=planning_note,
                    current_phase="COMPLETION_CONTRACT_RECOVERY",
                    current_model=selection["model"],
                    current_model_reason=selection["reason"],
                    current_tool=recovery_tool_name,
                    current_llm_workspace=str(turn_workspace),
                    last_llm_workspace=str(turn_workspace),
                    current_started_at=read_json(self.paths.runtime_status_path, fallback={}).get("current_started_at") or now_iso(),
                    current_finished_at=None,
                    worker_running=self._worker_running(),
                )
                try:
                    recovery_result = self.tools.execute(
                        recovery_tool_name,
                        recovery_tool_args,
                        on_update=(self._make_tool_stream_updater(
                            session_id=session_id,
                            selection=selection,
                            turn_id=turn_id,
                            queue_id=queue_id,
                            step_index=step_index,
                            recent_user_message=recent_user_message,
                            prompt=recovery_decision,
                            tool_name=recovery_tool_name,
                            llm_workspace=str(turn_workspace),
                            current_phase="COMPLETION_CONTRACT_RECOVERY",
                        ) if recovery_tool_name == "run_command" else None),
                    )
                except Exception as exc:
                    recovery_result = {"ok": False, "tool": recovery_tool_name, "error": str(exc)}
                self._append_session_event(
                    self.root,
                    session_id,
                    {
                        "type": "tool_result",
                        "tool_name": recovery_tool_name,
                        "content": json.dumps(recovery_result, ensure_ascii=False),
                        "ok": bool(recovery_result.get("ok")),
                        "turn_id": turn_id,
                        "queue_id": queue_id,
                        "step_index": step_index,
                        "model_reason": selection["reason"],
                        "llm_workspace": str(turn_workspace),
                        "reason_code": recovery_reason,
                    },
                )
                self._append_runtime_event(
                    session_id,
                    event_name="tool_call_finished",
                    content=json.dumps(recovery_result, ensure_ascii=False),
                    details={
                        "tool_name": recovery_tool_name,
                        "tool_args": recovery_tool_args,
                        "tool_result": recovery_result,
                        "ok": bool(recovery_result.get("ok")),
                        "reason_code": recovery_reason,
                    },
                    turn_id=turn_id,
                    queue_id=queue_id,
                    step_index=step_index,
                    llm_workspace=str(turn_workspace),
                    phase="COMPLETION_CONTRACT_RECOVERY",
                )
                if recovery_tool_name == "run_command" and not bool(recovery_result.get("ok")):
                    consultant_note = self._validation_failure_consultant_note(
                        user_message=recent_user_message,
                        tool_result=recovery_result,
                        steps=steps,
                        turn_workspace=turn_workspace,
                        current_model=str(selection.get("model") or ""),
                    )
                    if consultant_note is not None:
                        self._append_session_event(
                            self.root,
                            session_id,
                            {
                                "type": "system_note",
                                "role": "system",
                                "content": f"相談役LLMからの検証失敗レビュー: {consultant_note['advice']}",
                                "code": "validation_failure_consultant",
                                "reason_code": "consultant_advice",
                                "details": consultant_note,
                                "turn_id": turn_id,
                                "queue_id": queue_id,
                                "step_index": step_index,
                                "llm_workspace": str(turn_workspace),
                            },
                        )
                self.frame_manager.update_from_tool_result(recovery_tool_name, recovery_tool_args, recovery_result)
                steps.append({"tool_name": recovery_tool_name, "tool_args": recovery_tool_args, "tool_result": recovery_result})
                recovery_finish = self._controller_terminal_finish(
                    selection=selection,
                    goal_text=goal_text,
                    user_message=recent_user_message,
                    steps=steps,
                )
                if recovery_finish is not None:
                    acceptance = self._finish_acceptance_evaluation(
                        user_message=recent_user_message,
                        final_answer=recovery_finish,
                        steps=steps,
                    )
                    self._append_session_event(
                        self.root,
                        session_id,
                        {
                            "type": "system_note",
                            "role": "system",
                            "content": f"完了受理判定: {acceptance.get('status')}",
                            "code": "finish_acceptance",
                            "reason_code": self._finish_acceptance_reason_code(acceptance),
                            "details": acceptance,
                            "turn_id": turn_id,
                            "queue_id": queue_id,
                            "step_index": step_index,
                            "llm_workspace": str(turn_workspace),
                        },
                    )
                    if self._finish_status_is_accepted(acceptance.get("status")):
                        self._append_session_event(
                            self.root,
                            session_id,
                            {
                                "type": "finish",
                                "role": "assistant",
                                "content": recovery_finish,
                                "model": selection["model"],
                                "model_reason": f"{selection['reason']} + completion-contract-recovery",
                                "llm_attempt_count": 0,
                                "turn_id": turn_id,
                                "queue_id": queue_id,
                                "step_index": step_index,
                                "llm_workspace": str(turn_workspace),
                            },
                        )
                        self._write_runtime_status(
                            status="idle",
                            current_role=selection["role"],
                            current_turn_id=None,
                            current_queue_id=None,
                            current_user_message=None,
                            current_prompt_preview=None,
                            current_stream_text="",
                            current_plan=None,
                            current_phase="FINISH",
                            current_model=selection["model"],
                            current_model_reason=f"{selection['reason']} + completion-contract-recovery",
                            current_tool="finish",
                            current_operation_id=None,
                            current_llm_workspace=None,
                            last_llm_workspace=str(turn_workspace),
                            last_error=None,
                            last_system_note=None,
                            current_started_at=None,
                            current_finished_at=now_iso(),
                            worker_running=self._worker_running(),
                        )
                        finish_operation("finished", output_preview=recovery_finish)
                        return {"ok": True, "session_id": session_id, "steps": steps, "final_answer": recovery_finish}
                continue
            progress_state = self._implementation_task_progress_state(
                user_message=recent_user_message,
                steps=steps,
                session_id=session_id,
                turn_workspace=turn_workspace,
            )
            self._append_implementation_progress_event(
                session_id=session_id,
                turn_id=turn_id,
                queue_id=queue_id,
                step_index=step_index,
                turn_workspace=turn_workspace,
                state=progress_state,
                trigger="before_model",
            )
            if (
                bool(progress_state.get("applicable"))
                and str(progress_state.get("phase") or "") == "external_contract_satisfied"
            ):
                final_answer = (
                    "auto_finish: implementation artifact, meaningful tests, unittest success, "
                    "and external audit success are present."
                )
                self._append_session_event(
                    self.root,
                    session_id,
                    {
                        "type": "finish",
                        "role": "assistant",
                        "content": final_answer,
                        "model": selection["model"],
                        "model_reason": f"{selection['reason']} + implementation-contract-auto-finish",
                        "llm_attempt_count": 0,
                        "turn_id": turn_id,
                        "queue_id": queue_id,
                        "step_index": step_index,
                        "llm_workspace": str(turn_workspace),
                    },
                )
                self._write_runtime_status(
                    status="idle",
                    current_role=selection["role"],
                    current_turn_id=None,
                    current_queue_id=None,
                    current_user_message=None,
                    current_prompt_preview=None,
                    current_stream_text=final_answer,
                    current_plan=planning_note,
                    current_phase="FINISH",
                    current_model=selection["model"],
                    current_model_reason=f"{selection['reason']} + implementation-contract-auto-finish",
                    current_tool="finish",
                    current_operation_id=None,
                    current_llm_workspace=None,
                    last_llm_workspace=str(turn_workspace),
                    last_error=None,
                    last_system_note=final_answer,
                    current_started_at=None,
                    current_finished_at=now_iso(),
                    worker_running=self._worker_running(),
                )
                finish_operation("finished", output_preview=final_answer)
                return {"ok": True, "session_id": session_id, "steps": steps, "final_answer": final_answer}
            current_phase = self._implementation_task_effective_phase(
                fallback_phase=current_phase,
                state=progress_state,
            )
            progress_prompt = self._implementation_task_progress_prompt(progress_state)
            if current_phase in {"PLANNING_REQUIRED", "PLAN_REVISION"}:
                suppress_frame_operations = False
                schema_tool_names = ["create_plan"]
            elif current_phase == "PLAN_EXECUTION":
                suppress_frame_operations = self._implementation_task_should_suppress_frame_operations(progress_state)
                schema_tool_names = self._implementation_task_schema_tool_names(progress_state)
            else:
                suppress_frame_operations = self._implementation_task_should_suppress_frame_operations(progress_state)
                schema_tool_names = self._implementation_task_schema_tool_names(progress_state)
            prompt_recent_events = self._implementation_task_prompt_events(
                recent_events=recent_events,
                state=progress_state,
            )
            prompt_steps = self._implementation_task_prompt_steps(
                steps=steps,
                state=progress_state,
            )
            prompt_extra = "\n\n".join(
                part.strip()
                for part in [extra_prompt or "", progress_prompt]
                if str(part or "").strip()
            ) or None
            prompt = self._build_prompt(
                goal_text=goal_text,
                recent_events=prompt_recent_events,
                extra_prompt=prompt_extra,
                steps=prompt_steps,
                current_phase=current_phase,
                user_message=recent_user_message,
                suppress_frame_operations=suppress_frame_operations,
            )
            append_prompt_snapshot(
                self.root,
                session_id,
                {
                    "turn_id": turn_id,
                    "queue_id": queue_id,
                    "step_index": step_index,
                    "role": selection["role"],
                    "model": selection["model"],
                    "model_reason": selection["reason"],
                    "prompt": prompt,
                },
            )
            self._write_runtime_status(
                status="running",
                current_role=selection["role"],
                current_turn_id=turn_id,
                current_queue_id=queue_id,
                current_user_message=recent_user_message,
                current_prompt_preview=prompt[:2000],
                current_stream_text="Waiting for model response...",
                current_plan=planning_note,
                current_phase=current_phase,
                current_model=selection["model"],
                current_model_reason=selection["reason"],
                current_tool=None,
                current_llm_workspace=str(turn_workspace),
                last_llm_workspace=str(turn_workspace),
                last_llm_stream_metadata={},
                last_llm_parse_issue=None,
                last_error=None,
                last_system_note=None,
                current_started_at=now_iso(),
                current_finished_at=None,
                worker_running=self._worker_running(),
            )
            telemetry = self._chat_with_repair(
                role=str(selection["role"]),
                model=str(selection["model"]),
                prompt=prompt,
                session_id=session_id,
                turn_id=turn_id,
                queue_id=queue_id,
                step_index=step_index,
                llm_workspace=str(turn_workspace),
                current_phase=current_phase,
                suppress_frame_operations=suppress_frame_operations,
                allowed_tool_names=schema_tool_names,
            )
            envelope = telemetry["envelope"]
            assistant_message = str(envelope.get("assistant_message") or "").strip()
            if assistant_message and not telemetry.get("parse_issue"):
                self._append_session_event(
                    self.root,
                    session_id,
                    {
                        "type": "assistant_message",
                        "role": "assistant",
                        "content": assistant_message,
                        "model": selection["model"],
                        "model_reason": selection["reason"],
                        "llm_attempt_count": telemetry["attempt_count"],
                        "turn_id": turn_id,
                        "queue_id": queue_id,
                        "step_index": step_index,
                        "llm_workspace": str(turn_workspace),
                    },
                )
            if telemetry.get("parse_issue"):
                issue = str(telemetry.get("parse_issue") or "invalid_tool_envelope")
                stream_abort_reason = str((telemetry.get("stream_metadata") or {}).get("client_abort_reason") or "")
                if stream_abort_reason in {
                    "plan_record_embedded_edit_stream",
                    "stream_char_limit",
                    "repetitive_output",
                    "json_envelope_followed_by_extra_text",
                }:
                    issue = stream_abort_reason
                transport_error = "; ".join(
                    str(item)
                    for item in (telemetry.get("schema_validation") or {}).get("errors") or []
                    if str(item).strip()
                )
                if issue == "llm_transport_error" and (
                    "OperatorInterrupt" in transport_error or "received signal" in transport_error
                ):
                    progress_state = self._implementation_task_progress_state(
                        user_message=recent_user_message,
                        steps=steps,
                        session_id=session_id,
                        turn_workspace=turn_workspace,
                    )
                    message = f"Runtime interrupted by operator: {transport_error or 'operator interrupted runtime'}"
                    self._append_session_event(
                        self.root,
                        session_id,
                        {
                            "type": "system_note",
                            "role": "system",
                            "content": message,
                            "code": "operator_interrupt",
                            "reason_code": "interrupted_by_operator",
                            "details": {
                                "operator_reason": transport_error or "operator interrupted runtime",
                                "stopped_at_step": step_index,
                                "current_phase": current_phase,
                                "current_tool": None,
                                "current_model": selection["model"],
                                "contract_state": progress_state.get("contract_state"),
                                "missing_requirements": progress_state.get("missing_requirements") or [],
                                "failure_type": "operator_interrupt",
                                "blocked_by": "operator",
                                "allowed_next_actions": ["resume the run manually if appropriate"],
                                "suggested_fix": "operator interrupt により停止しました。続ける場合は同じworkspaceの状態を確認して再開してください。",
                                "next_required_action": "wait for operator decision or resume from current workspace state",
                                "latest_llm_workspace": str(turn_workspace),
                            },
                            "turn_id": turn_id,
                            "queue_id": queue_id,
                            "step_index": step_index,
                            "llm_workspace": str(turn_workspace),
                        },
                    )
                    self._write_runtime_status(
                        status="interrupted_by_operator",
                        current_role=selection["role"],
                        current_turn_id=None,
                        current_queue_id=None,
                        current_user_message=None,
                        current_prompt_preview=None,
                        current_stream_text=message,
                        current_plan=planning_note,
                        current_phase="OPERATOR_INTERRUPT",
                        current_model=selection["model"],
                        current_model_reason=selection["reason"],
                        current_tool=None,
                        current_operation_id=None,
                        current_llm_workspace=None,
                        last_llm_workspace=str(turn_workspace),
                        last_error=message,
                        last_system_note=message,
                        current_started_at=None,
                        current_finished_at=now_iso(),
                        worker_running=False,
                        current_process_pid=None,
                    )
                    finish_operation("interrupted", output_preview=message)
                    return {
                        "ok": False,
                        "session_id": session_id,
                        "steps": steps,
                        "error": message,
                        "parse_issue": "interrupted_by_operator",
                        "schema_validation": telemetry.get("schema_validation") or {},
                        "raw_output_is_machine_json": bool(telemetry.get("raw_output_is_machine_json")),
                        "schema_validation_ok": bool(telemetry.get("schema_validation_ok")),
                    }
                issue_progress_state = self._implementation_task_progress_state(
                    user_message=recent_user_message,
                    steps=steps,
                    session_id=session_id,
                    turn_workspace=turn_workspace,
                )
                issue_allowed_actions = list(schema_tool_names or [])
                if bool(issue_progress_state.get("applicable")) and issue_progress_state.get("allowed_next_actions"):
                    issue_allowed_actions = [
                        str(item)
                        for item in issue_progress_state.get("allowed_next_actions") or []
                        if str(item).strip()
                    ] or issue_allowed_actions
                issue_missing_requirements = (
                    list(issue_progress_state.get("missing_requirements") or [])
                    if bool(issue_progress_state.get("applicable"))
                    else []
                )
                if issue == "plan_record_embedded_edit_stream":
                    issue_blocked_by = "runtime_stream_guard"
                    issue_suggested_fix = (
                        "create_plan のstream中に PlanRecord.first_action が write_file/append_file/replace_text を含むことを検出したため停止しました。"
                        "PlanRecordは小さい計画契約だけにし、first_actionは list_files/read_file/search_code/run_command のどれかにしてください。"
                        "edit WorkUnit の first_action は {\"tool\":\"list_files\",\"args\":{\"path\":\".\"}} のような観測専用actionにしてください。"
                        "実装コード本文は、該当WorkUnitの子フレームが開いた後のPLAN_EXECUTIONで write_file として返してください。"
                    )
                    issue_next_required_action = (
                        "retry create_plan with a concise PlanRecord; use list_files/read_file/search_code/run_command first_action only"
                    )
                elif issue in {"stream_char_limit", "repetitive_output"}:
                    issue_blocked_by = "runtime_stream_guard"
                    issue_suggested_fix = (
                        "直前のLLM出力は長大または反復的で、完全なtool JSONとして閉じる前に停止しました。"
                        "同じ長い設計や途中出力を続けず、次の許可actionでより小さい完全な成果物または小さいtargeted editを返してください。"
                    )
                    issue_next_required_action = (
                        "return one valid tool JSON for the next allowed action; if writing an implementation, "
                        "choose a smaller complete reference implementation instead of repeating the same large architecture"
                    )
                elif issue == "schema_validation_failed":
                    issue_blocked_by = "runtime_tool_schema"
                    issue_suggested_fix = "schema_validation_errorsを満たすtool_name/tool_argsだけで、正確に1つのJSON objectを返してください。"
                    issue_next_required_action = "return exactly one valid tool call JSON object matching the current schema"
                else:
                    issue_blocked_by = "runtime_machine_control_parser"
                    issue_suggested_fix = "proseやMarkdownを混ぜず、現在phaseの許可toolだけを使う1つのtool JSON objectを返してください。"
                    issue_next_required_action = "return exactly one valid tool call JSON object matching the current schema"
                raw_text_for_issue = str(telemetry.get("raw_text") or "")
                combined_text_for_issue = str(telemetry.get("combined_text") or "")
                self._append_session_event(
                    self.root,
                    session_id,
                    {
                        "type": "system_note",
                        "role": "system",
                        "content": f"LLM応答がツール呼び出しJSONとして解釈できませんでした: {telemetry.get('parse_issue')}",
                        "code": "llm_output_issue",
                        "reason_code": str(telemetry.get("parse_issue") or "invalid_tool_envelope"),
                        "details": {
                            "parse_target": "content",
                            "failure_type": issue,
                            "blocked_by": issue_blocked_by,
                            "raw_text": raw_text_for_issue[:4000],
                            "raw_text_tail": raw_text_for_issue[-4000:],
                            "thinking_text": str(telemetry.get("thinking_text") or "")[:4000],
                            "combined_text": combined_text_for_issue[:4000],
                            "combined_text_tail": combined_text_for_issue[-4000:],
                            "stream_metadata": telemetry.get("stream_metadata") or {},
                            "raw_output_is_machine_json": bool(telemetry.get("raw_output_is_machine_json")),
                            "schema_validation_ok": bool(telemetry.get("schema_validation_ok")),
                            "schema_validation": telemetry.get("schema_validation") or {},
                            "current_phase": current_phase,
                            "missing_requirements": issue_missing_requirements,
                            "allowed_tool_names": list(schema_tool_names or []),
                            "allowed_next_actions": issue_allowed_actions,
                            "suggested_fix": issue_suggested_fix,
                            "next_required_action": issue_next_required_action,
                        },
                        "turn_id": turn_id,
                        "queue_id": queue_id,
                        "step_index": step_index,
                        "llm_workspace": str(turn_workspace),
                    },
                )
                self._record_observer_llm_output_issue_note(
                    session_id=session_id,
                    turn_id=turn_id,
                    queue_id=queue_id,
                    step_index=step_index,
                    user_message=recent_user_message,
                    assistant_message=assistant_message
                    or str(telemetry.get("combined_text") or telemetry.get("raw_text") or ""),
                    parse_issue=str(telemetry.get("parse_issue") or "invalid_tool_envelope"),
                    prompt_snapshot=prompt,
                    steps=steps,
                )
                self._record_reflection(
                    session_id=session_id,
                    turn_id=turn_id,
                    queue_id=queue_id,
                    user_message=recent_user_message,
                    reason=f"llm output did not satisfy machine-control schema: {issue}",
                    steps=steps,
                )
                if issue == "plan_record_embedded_edit_stream":
                    autorepair_result = self._auto_create_minimal_plan_after_repeated_stream_issue(
                        session_id=session_id,
                        turn_id=turn_id,
                        queue_id=queue_id,
                        step_index=step_index,
                        turn_workspace=turn_workspace,
                        user_message=recent_user_message,
                        current_phase=current_phase,
                        steps=steps,
                        current_model=str(selection["model"]),
                    )
                    if autorepair_result and bool(autorepair_result.get("ok")):
                        message = "PlanRecord was auto-repaired after repeated embedded edit streams; continuing with PLAN_EXECUTION."
                        self._write_runtime_status(
                            status="running",
                            current_role=selection["role"],
                            current_turn_id=turn_id,
                            current_queue_id=queue_id,
                            current_user_message=recent_user_message,
                            current_prompt_preview=prompt[:2000],
                            current_stream_text=message,
                            current_plan=planning_note,
                            current_phase="PLAN_EXECUTION",
                            current_model=selection["model"],
                            current_model_reason=selection["reason"],
                            current_tool=None,
                            current_operation_id=operation_id,
                            current_llm_workspace=str(turn_workspace),
                            last_llm_workspace=str(turn_workspace),
                            last_error=None,
                            last_system_note=message,
                            current_started_at=read_json(self.paths.runtime_status_path, fallback={}).get("current_started_at") or now_iso(),
                            current_finished_at=None,
                            worker_running=self._worker_running(),
                        )
                        continue
                message = f"LLM output did not satisfy machine-control schema: {issue}"
                if issue == "llm_transport_error":
                    message = f"LLM transport failed during machine-control generation: {transport_error or 'unknown transport error'}"
                if issue != "llm_transport_error" and step_index < max_steps:
                    recovery_message = (
                        f"{message}. これは直前までの成果物失敗ではなく、次アクション生成の通信失敗です。"
                        " 成功済みの観測とtool結果を保持したまま、次ステップで別の具体アクションを選んでください。"
                    )
                    self._write_runtime_status(
                        status="running",
                        current_role=selection["role"],
                        current_turn_id=turn_id,
                        current_queue_id=queue_id,
                        current_user_message=recent_user_message,
                        current_prompt_preview=prompt[:2000],
                        current_stream_text=recovery_message,
                        current_plan=planning_note,
                        current_phase="RECOVER_FROM_LLM_OUTPUT_ISSUE",
                        current_model=selection["model"],
                        current_model_reason=selection["reason"],
                        current_tool=None,
                        current_operation_id=operation_id,
                        current_llm_workspace=str(turn_workspace),
                        last_llm_workspace=str(turn_workspace),
                        last_error=recovery_message,
                        last_system_note=recovery_message,
                        current_started_at=read_json(self.paths.runtime_status_path, fallback={}).get("current_started_at") or now_iso(),
                        current_finished_at=None,
                        worker_running=self._worker_running(),
                    )
                    continue
                self._write_runtime_status(
                    status="idle",
                    current_role=selection["role"],
                    current_turn_id=None,
                    current_queue_id=None,
                    current_user_message=None,
                    current_prompt_preview=None,
                    current_stream_text="",
                    current_plan=None,
                    current_phase="FINISH",
                    current_model=selection["model"],
                    current_model_reason=selection["reason"],
                    current_tool=None,
                    current_operation_id=None,
                    current_llm_workspace=None,
                    last_llm_workspace=str(turn_workspace),
                    last_error=message,
                    last_system_note=message,
                    current_started_at=None,
                    current_finished_at=now_iso(),
                    worker_running=self._worker_running(),
                )
                finish_operation("failed", output_preview=message)
                return {
                    "ok": False,
                    "session_id": session_id,
                    "steps": steps,
                    "error": message,
                    "parse_issue": issue,
                    "schema_validation": telemetry.get("schema_validation") or {},
                    "raw_output_is_machine_json": bool(telemetry.get("raw_output_is_machine_json")),
                    "schema_validation_ok": bool(telemetry.get("schema_validation_ok")),
                }
            tool_name = str(envelope.get("tool_name") or "").strip() or "finish"
            tool_args = envelope.get("tool_args") or {}
            if not isinstance(tool_args, dict):
                tool_args = {}
            first_action_block = self._child_first_action_blocked_event(
                session_id=session_id,
                turn_id=turn_id,
                queue_id=queue_id,
                step_index=step_index,
                turn_workspace=turn_workspace,
                requested_tool=tool_name,
                requested_args=dict(tool_args),
            )
            if first_action_block is not None:
                message = str(first_action_block.get("content") or "")
                self._write_runtime_status(
                    status="running",
                    current_role=selection["role"],
                    current_turn_id=turn_id,
                    current_queue_id=queue_id,
                    current_user_message=recent_user_message,
                    current_prompt_preview=prompt[:2000],
                    current_stream_text=message,
                    current_plan=planning_note,
                    current_phase="FIRST_ACTION_REQUIRED",
                    current_model=selection["model"],
                    current_model_reason=selection["reason"],
                    current_tool=None,
                    current_llm_workspace=str(turn_workspace),
                    last_llm_workspace=str(turn_workspace),
                    last_error=message,
                    last_system_note=message,
                    worker_running=self._worker_running(),
                )
                continue
            planner_block = self._planner_action_blocked_event(
                session_id=session_id,
                turn_id=turn_id,
                queue_id=queue_id,
                step_index=step_index,
                turn_workspace=turn_workspace,
                tool_name=tool_name,
                user_message=recent_user_message,
                current_phase=current_phase,
                recent_events=recent_events,
                steps=steps,
            )
            if planner_block is not None:
                message = str(planner_block.get("content") or "")
                self._write_runtime_status(
                    status="running",
                    current_role=selection["role"],
                    current_turn_id=turn_id,
                    current_queue_id=queue_id,
                    current_user_message=recent_user_message,
                    current_prompt_preview=prompt[:2000],
                    current_stream_text=message,
                    current_plan=planning_note,
                    current_phase=current_phase,
                    current_model=selection["model"],
                    current_model_reason=selection["reason"],
                    current_tool=None,
                    current_llm_workspace=str(turn_workspace),
                    last_llm_workspace=str(turn_workspace),
                    last_error=message,
                    last_system_note=message,
                    worker_running=self._worker_running(),
                )
                continue
            early_route_block = self._implementation_task_frame_action_block(
                    tool_name=tool_name,
                    tool_args=dict(tool_args),
                    state=progress_state,
                )
            if early_route_block is not None:
                message = str(early_route_block.get("message") or "")
                reason_code = str(early_route_block.get("reason_code") or "implementation_task_phase_blocked")
                blocked_path = str(early_route_block.get("path") or tool_args.get("path") or "")
                block_signature = str(early_route_block.get("block_signature") or "")
                suggested_fix = str(early_route_block.get("suggested_fix") or "").strip()
                allowed_next_actions = list(early_route_block.get("allowed_next_actions") or [])
                block_content = f"{tool_name} がブロックされました: {message}"
                if suggested_fix:
                    block_content += f" suggested_fix: {suggested_fix}"
                if allowed_next_actions:
                    block_content += " allowed_next_actions: " + ", ".join(
                        str(action) for action in allowed_next_actions
                    )
                repeated_block_count = self._recent_same_blocked_action_count(
                    session_id=session_id,
                    code="implementation_task_progress_blocked",
                    reason_code=reason_code,
                    blocked_tool=tool_name,
                    path=blocked_path,
                    block_signature=block_signature,
                )
                self._append_session_event(
                    self.root,
                    session_id,
                    {
                        "type": "system_note",
                        "role": "system",
                        "content": block_content,
                        "code": "implementation_task_progress_blocked",
                        "reason_code": reason_code,
                        "details": {
                            "reason_code": reason_code,
                            "blocked_tool": tool_name,
                            "path": blocked_path,
                            "phase": str(early_route_block.get("phase") or ""),
                            "route_phase": str(early_route_block.get("route_phase") or ""),
                            "blocked_by": str(early_route_block.get("blocked_by") or "implementation_task_progress_controller"),
                            "failure_type": str(early_route_block.get("failure_type") or "implementation_task_progress_blocked"),
                            "allowed_next_actions": allowed_next_actions,
                            "suggested_fix": suggested_fix,
                            "next_required_action": str(early_route_block.get("next_required_action") or suggested_fix or ""),
                            "block_signature": block_signature,
                            "state": early_route_block.get("state") or {},
                        },
                        "turn_id": turn_id,
                        "queue_id": queue_id,
                        "step_index": step_index,
                        "llm_workspace": str(turn_workspace),
                    },
                )
                if repeated_block_count >= 2:
                    failure_message = (
                        "同じ implementation task progress block が繰り返されました。runtimeは現在許可される"
                        "次アクションを提示しましたが、同じ不許可アクションが再提案されたため、このrunを"
                        "同型失敗サンプルとして停止します。"
                    )
                    self._append_session_event(
                        self.root,
                        session_id,
                        {
                            "type": "system_note",
                            "role": "system",
                            "content": failure_message,
                            "code": "blocked_action_ignored",
                            "reason_code": "blocked_action_ignored_repeatedly",
                            "details": {
                                "blocked_event_code": "implementation_task_progress_blocked",
                                "blocked_reason_code": reason_code,
                                "blocked_tool": tool_name,
                                "path": blocked_path,
                                "block_signature": block_signature,
                                "failure_type": "blocked_action_ignored",
                                "blocked_by": "implementation_task_progress_controller",
                                "repeated_block_count": repeated_block_count + 1,
                                "allowed_next_actions": list(early_route_block.get("allowed_next_actions") or []),
                                "suggested_fix": str(early_route_block.get("suggested_fix") or "許可された次アクションだけを実行してください。"),
                                "next_required_action": str(early_route_block.get("next_required_action") or early_route_block.get("suggested_fix") or "choose one allowed_next_actions item"),
                            },
                            "turn_id": turn_id,
                            "queue_id": queue_id,
                            "step_index": step_index,
                            "llm_workspace": str(turn_workspace),
                        },
                    )
                    self._write_runtime_status(
                        status="idle",
                        current_role=selection["role"],
                        current_turn_id=None,
                        current_queue_id=None,
                        current_user_message=None,
                        current_prompt_preview=prompt[:2000],
                        current_stream_text=failure_message,
                        current_plan=planning_note,
                        current_phase="IMPLEMENTATION_TASK_PROGRESS",
                        current_model=selection["model"],
                        current_model_reason=selection["reason"],
                        current_tool=None,
                        current_operation_id=None,
                        current_llm_workspace=None,
                        last_llm_workspace=str(turn_workspace),
                        last_error=failure_message,
                        last_system_note=failure_message,
                        current_started_at=None,
                        current_finished_at=now_iso(),
                        worker_running=self._worker_running(),
                    )
                    finish_operation("failed", output_preview=failure_message)
                    return {
                        "ok": False,
                        "session_id": session_id,
                        "steps": steps,
                        "error": failure_message,
                    }
                self._write_runtime_status(
                    status="running",
                    current_role=selection["role"],
                    current_turn_id=turn_id,
                    current_queue_id=queue_id,
                    current_user_message=recent_user_message,
                    current_prompt_preview=prompt[:2000],
                    current_stream_text=f"{tool_name} blocked by implementation progress phase. {message}",
                    current_plan=planning_note,
                    current_phase="IMPLEMENTATION_TASK_PROGRESS",
                    current_model=selection["model"],
                    current_model_reason=selection["reason"],
                    current_tool=None,
                    current_llm_workspace=str(turn_workspace),
                    last_llm_workspace=str(turn_workspace),
                    last_error=message,
                    last_system_note=message,
                    worker_running=self._worker_running(),
                )
                continue
            if tool_name == "create_plan":
                create_plan_result = self._handle_create_plan(
                    session_id=session_id,
                    turn_id=turn_id,
                    queue_id=queue_id,
                    step_index=step_index,
                    tool_args=tool_args,
                    turn_workspace=turn_workspace,
                    steps=steps,
                    user_message=recent_user_message,
                    current_model=str(selection["model"]),
                )
                create_plan_ok = bool(create_plan_result.get("ok"))
                if create_plan_result.get("auto_steps"):
                    steps.extend(list(create_plan_result.get("auto_steps") or []))
                create_plan_message = (
                    f"Accepted PlanRecord and opened first WorkUnit ({len(create_plan_result.get('tasks') or [])} work units)."
                    if create_plan_ok
                    else str((create_plan_result.get("event") or {}).get("content") or create_plan_result.get("error") or "create_plan blocked")
                )
                self._write_runtime_status(
                    status="running",
                    current_role=selection["role"],
                    current_turn_id=turn_id,
                    current_queue_id=queue_id,
                    current_user_message=recent_user_message,
                    current_prompt_preview=prompt[:2000],
                    current_stream_text=create_plan_message,
                    current_plan=planning_note,
                    current_phase="PLAN_EXECUTION" if create_plan_ok else "PLANNING_REQUIRED",
                    current_model=selection["model"],
                    current_model_reason=selection["reason"],
                    current_tool="create_plan",
                    current_llm_workspace=str(turn_workspace),
                    last_llm_workspace=str(turn_workspace),
                    last_error=None if create_plan_ok else create_plan_message,
                    last_system_note=None if create_plan_ok else create_plan_message,
                    current_finished_at=now_iso(),
                    worker_running=self._worker_running(),
                )
                if bool(create_plan_result.get("terminal_failure")):
                    finish_operation("failed", output_preview=create_plan_message)
                    return {
                        "ok": False,
                        "session_id": session_id,
                        "steps": steps,
                        "error": create_plan_message,
                    }
                continue
            if tool_name == "decompose_tasks":
                decompose_result = self._handle_decompose_tasks(
                    session_id=session_id,
                    turn_id=turn_id,
                    queue_id=queue_id,
                    step_index=step_index,
                    tool_args=tool_args,
                    turn_workspace=turn_workspace,
                    steps=steps,
                    user_message=recent_user_message,
                    current_model=str(selection["model"]),
                )
                decompose_ok = bool(decompose_result.get("ok"))
                if decompose_result.get("auto_steps"):
                    steps.extend(list(decompose_result.get("auto_steps") or []))
                decompose_message = (
                    "Planned child tasks and opened the first child frame."
                    if decompose_ok
                    else str((decompose_result.get("event") or {}).get("content") or decompose_result.get("error") or "decompose_tasks blocked")
                )
                self._write_runtime_status(
                    status="running",
                    current_role=selection["role"],
                    current_turn_id=turn_id,
                    current_queue_id=queue_id,
                    current_user_message=recent_user_message,
                    current_prompt_preview=prompt[:2000],
                    current_stream_text=decompose_message,
                    current_plan=planning_note,
                    current_phase="TASK_DECOMPOSED" if decompose_ok else "DECOMPOSE_BLOCKED",
                    current_model=selection["model"],
                    current_model_reason=selection["reason"],
                    current_tool="decompose_tasks",
                    current_llm_workspace=str(turn_workspace),
                    last_llm_workspace=str(turn_workspace),
                    last_error=None if decompose_ok else decompose_message,
                    last_system_note=None if decompose_ok else decompose_message,
                    current_finished_at=now_iso(),
                    worker_running=self._worker_running(),
                )
                if bool(decompose_result.get("terminal_failure")):
                    self._write_runtime_status(
                        status="idle",
                        current_role=selection["role"],
                        current_turn_id=None,
                        current_queue_id=None,
                        current_user_message=None,
                        current_prompt_preview=prompt[:2000],
                        current_stream_text=decompose_message,
                        current_plan=planning_note,
                        current_phase="WORK_PACKAGE_INVALID_REPEATED",
                        current_model=selection["model"],
                        current_model_reason=selection["reason"],
                        current_tool=None,
                        current_operation_id=None,
                        current_llm_workspace=None,
                        last_llm_workspace=str(turn_workspace),
                        last_error=decompose_message,
                        last_system_note=decompose_message,
                        current_started_at=None,
                        current_finished_at=now_iso(),
                        worker_running=self._worker_running(),
                    )
                    finish_operation("failed", output_preview=decompose_message)
                    return {
                        "ok": False,
                        "session_id": session_id,
                        "steps": steps,
                        "error": decompose_message,
                    }
                continue
            if tool_name == "open_child_frame":
                open_result = self._handle_open_child_frame(
                    session_id=session_id,
                    turn_id=turn_id,
                    queue_id=queue_id,
                    step_index=step_index,
                    tool_args=tool_args,
                    turn_workspace=turn_workspace,
                    user_message=recent_user_message,
                    current_model=str(selection["model"]),
                )
                open_ok = bool(open_result.get("ok"))
                if open_result.get("auto_steps"):
                    steps.extend(list(open_result.get("auto_steps") or []))
                open_message = (
                    "Opened child frame."
                    if open_ok
                    else str((open_result.get("event") or {}).get("content") or open_result.get("error") or "open_child_frame blocked")
                )
                self._write_runtime_status(
                    status="running",
                    current_role=selection["role"],
                    current_turn_id=turn_id,
                    current_queue_id=queue_id,
                    current_user_message=recent_user_message,
                    current_prompt_preview=prompt[:2000],
                    current_stream_text=open_message,
                    current_plan=planning_note,
                    current_phase="FRAME_OPENED" if open_ok else "FRAME_OPEN_BLOCKED",
                    current_model=selection["model"],
                    current_model_reason=selection["reason"],
                    current_tool="open_child_frame",
                    current_llm_workspace=str(turn_workspace),
                    last_llm_workspace=str(turn_workspace),
                    last_error=None if open_ok else open_message,
                    last_system_note=None if open_ok else open_message,
                    current_finished_at=now_iso(),
                    worker_running=self._worker_running(),
                )
                if bool(open_result.get("terminal_failure")):
                    self._write_runtime_status(
                        status="idle",
                        current_role=selection["role"],
                        current_turn_id=None,
                        current_queue_id=None,
                        current_user_message=None,
                        current_prompt_preview=prompt[:2000],
                        current_stream_text=open_message,
                        current_plan=planning_note,
                        current_phase="WORK_PACKAGE_INVALID_REPEATED",
                        current_model=selection["model"],
                        current_model_reason=selection["reason"],
                        current_tool=None,
                        current_operation_id=None,
                        current_llm_workspace=None,
                        last_llm_workspace=str(turn_workspace),
                        last_error=open_message,
                        last_system_note=open_message,
                        current_started_at=None,
                        current_finished_at=now_iso(),
                        worker_running=self._worker_running(),
                    )
                    finish_operation("failed", output_preview=open_message)
                    return {
                        "ok": False,
                        "session_id": session_id,
                        "steps": steps,
                        "error": open_message,
                    }
                continue
            if tool_name == "return_to_parent":
                self._handle_return_to_parent(
                    session_id=session_id,
                    turn_id=turn_id,
                    queue_id=queue_id,
                    step_index=step_index,
                    tool_args=tool_args,
                    turn_workspace=turn_workspace,
                )
                self._write_runtime_status(
                    status="running",
                    current_role=selection["role"],
                    current_turn_id=turn_id,
                    current_queue_id=queue_id,
                    current_user_message=recent_user_message,
                    current_prompt_preview=prompt[:2000],
                    current_stream_text="Returned to parent frame.",
                    current_plan=planning_note,
                    current_phase="FRAME_RETURNED",
                    current_model=selection["model"],
                    current_model_reason=selection["reason"],
                    current_tool="return_to_parent",
                    current_llm_workspace=str(turn_workspace),
                    last_llm_workspace=str(turn_workspace),
                    current_finished_at=now_iso(),
                    worker_running=self._worker_running(),
                )
                continue
            if tool_name == "finish":
                active_frame = self.frame_manager.current_frame()
                if active_frame is not None and active_frame.parent_frame_id is not None:
                    message = "子フレームでは finish ではなく return_to_parent を使用してください。"
                    prior_child_finish_blocks = self._child_finish_block_count()
                    evidence = self._child_frame_successful_tool_evidence()
                    has_unresolved_failure = self._child_frame_has_unresolved_failure()
                    self._append_session_event(
                        session_id,
                        {
                            "type": "system_note",
                            "role": "system",
                            "content": message,
                            "code": "finish_blocked",
                            "reason_code": "child_frame_must_return",
                            "details": {
                                "active_frame_id": active_frame.frame_id,
                                "active_frame_depth": active_frame.depth,
                                "blocked_tool": "finish",
                                "failure_type": "child_frame_must_return",
                                "blocked_by": "frame_contract",
                                "allowed_next_actions": [
                                    {
                                        "tool": "return_to_parent",
                                        "strategy": "return the child evidence to the parent frame before finalizing",
                                    }
                                ],
                                "successful_tool_evidence": evidence,
                                "auto_return_if_repeated": bool(evidence and not has_unresolved_failure),
                                "unresolved_failure": has_unresolved_failure,
                                "repeat_count": prior_child_finish_blocks + 1,
                                "suggested_fix": "子フレームの成果を finish ではなく return_to_parent で親フレームへ返してください。",
                                "next_required_action": "return_to_parent with the child frame evidence",
                            },
                            "turn_id": turn_id,
                            "queue_id": queue_id,
                            "step_index": step_index,
                            "llm_workspace": str(turn_workspace),
                        },
                    )
                    if prior_child_finish_blocks >= 1 and evidence and not has_unresolved_failure:
                        self._force_return_after_child_contract_block(
                            session_id=session_id,
                            turn_id=turn_id,
                            queue_id=queue_id,
                            step_index=step_index,
                            turn_workspace=turn_workspace,
                            blocked_tool="finish",
                        )
                        self._write_runtime_status(
                            status="running",
                            current_role=selection["role"],
                            current_turn_id=turn_id,
                            current_queue_id=queue_id,
                            current_user_message=recent_user_message,
                            current_prompt_preview=prompt[:2000],
                            current_stream_text="Repeated child finish was converted to return_to_parent with existing tool evidence.",
                            current_plan=planning_note,
                            current_phase="FRAME_RETURNED",
                            current_model=selection["model"],
                            current_model_reason=selection["reason"],
                            current_tool="return_to_parent",
                            current_llm_workspace=str(turn_workspace),
                            last_llm_workspace=str(turn_workspace),
                            last_error=None,
                            last_system_note=message,
                            current_finished_at=now_iso(),
                            worker_running=self._worker_running(),
                        )
                        continue
                    self._write_runtime_status(
                        status="running",
                        current_role=selection["role"],
                        current_turn_id=turn_id,
                        current_queue_id=queue_id,
                        current_user_message=recent_user_message,
                        current_prompt_preview=prompt[:2000],
                        current_stream_text=message,
                        current_plan=planning_note,
                        current_phase="RETURN_TO_PARENT_REQUIRED",
                        current_model=selection["model"],
                        current_model_reason=selection["reason"],
                        current_tool=None,
                        current_llm_workspace=str(turn_workspace),
                        last_llm_workspace=str(turn_workspace),
                        last_error=message,
                        last_system_note=message,
                        worker_running=self._worker_running(),
                    )
                    continue
                pending_task = self._pending_plan_child_task()
                if pending_task is not None:
                    event = self._plan_execution_pending_block_event(
                        session_id=session_id,
                        turn_id=turn_id,
                        queue_id=queue_id,
                        step_index=step_index,
                        turn_workspace=turn_workspace,
                        blocked_tool="finish",
                        pending_task=pending_task,
                    )
                    message = str(event.get("content") or "finish blocked by pending planned WorkUnit")
                    self._write_runtime_status(
                        status="running",
                        current_role=selection["role"],
                        current_turn_id=turn_id,
                        current_queue_id=queue_id,
                        current_user_message=recent_user_message,
                        current_prompt_preview=prompt[:2000],
                        current_stream_text=message,
                        current_plan=planning_note,
                        current_phase="PLAN_EXECUTION",
                        current_model=selection["model"],
                        current_model_reason=selection["reason"],
                        current_tool=None,
                        current_llm_workspace=str(turn_workspace),
                        last_llm_workspace=str(turn_workspace),
                        last_error=message,
                        last_system_note=message,
                        worker_running=self._worker_running(),
                    )
                    continue
                if self._controller_finish_blocked_for_current_evidence(recent_events):
                    message = (
                        "完了がブロックされました: 直近の未達条件を解消する corrective edit がまだありません。"
                        " read_file などの観測だけでは完了候補に戻せません。"
                    )
                    self._append_session_event(
                        self.root,
                        session_id,
                        {
                            "type": "system_note",
                            "role": "system",
                            "content": message,
                            "code": "finish_blocked",
                            "reason_code": "corrective_action_required",
                            "details": {
                                "blocked_tool": "finish",
                                "failure_type": "corrective_action_required",
                                "blocked_by": "finish_contract_corrective_action_gate",
                                "allowed_next_actions": ["replace_text", "write_file", "append_file"],
                                "suggested_fix": "問題のある成果物を修正し、その後に再実行して表示結果を更新してください。",
                                "next_required_action": "perform a corrective edit before proposing finish again",
                            },
                            "turn_id": turn_id,
                            "queue_id": queue_id,
                            "step_index": step_index,
                            "llm_workspace": str(turn_workspace),
                        },
                    )
                    self._write_runtime_status(
                        status="running",
                        current_role=selection["role"],
                        current_turn_id=turn_id,
                        current_queue_id=queue_id,
                        current_user_message=recent_user_message,
                        current_prompt_preview=prompt[:2000],
                        current_stream_text=f"Finish blocked. {message}",
                        current_plan=planning_note,
                        current_phase="REVISE_FROM_ACCEPTANCE",
                        current_model=selection["model"],
                        current_model_reason=selection["reason"],
                        current_tool=None,
                        current_llm_workspace=str(turn_workspace),
                        last_llm_workspace=str(turn_workspace),
                        last_error="finish blocked until corrective action",
                        last_system_note=message,
                        worker_running=self._worker_running(),
                    )
                    continue
                missing_commands = self._missing_requested_commands(
                    user_message=recent_user_message,
                    steps=steps,
                )
                if missing_commands:
                    self._append_session_event(
                        self.root,
                        session_id,
                        {
                            "type": "system_note",
                            "role": "system",
                            "content": f"完了がブロックされました: 要求されたコマンドがまだ実行されていません: {', '.join(missing_commands)}",
                            "code": "finish_blocked",
                            "reason_code": "missing_required_commands",
                            "details": {
                                "missing_commands": list(missing_commands),
                                "missing_requirements": [f"run_command {command}" for command in missing_commands],
                                "failure_type": "missing_required_commands",
                                "blocked_by": "finish_contract_command_coverage",
                                "allowed_next_actions": [f"run_command {command}" for command in missing_commands],
                                "suggested_fix": "要求された検証コマンドを実行し、そのstdout/stderr/returncodeを完了証拠にしてください。",
                                "next_required_action": f"run the missing command: {missing_commands[0]}",
                            },
                            "turn_id": turn_id,
                            "queue_id": queue_id,
                            "step_index": step_index,
                            "llm_workspace": str(turn_workspace),
                        },
                    )
                    self._record_observer_judgement_note(
                        session_id=session_id,
                        turn_id=turn_id,
                        queue_id=queue_id,
	                        step_index=step_index,
	                        user_message=recent_user_message,
	                        assistant_message=assistant_message,
	                        system_decision=f"完了をブロックしました: 要求されたコマンドがまだ実行されていません: {', '.join(missing_commands)}",
	                        reason_code="missing_required_commands",
                            prompt_snapshot=prompt,
                            steps=steps,
	                    )
                    self._write_runtime_status(
                        status="running",
                        current_role=selection["role"],
                        current_turn_id=turn_id,
                        current_queue_id=queue_id,
                        current_user_message=recent_user_message,
                        current_prompt_preview=prompt[:2000],
                        current_stream_text=f"Finish blocked. Missing commands: {', '.join(missing_commands)}",
                        current_plan=planning_note,
                        current_phase="EXECUTE_MISSING_COMMANDS",
                        current_model=selection["model"],
                        current_model_reason=selection["reason"],
                        current_tool=None,
                        current_llm_workspace=str(turn_workspace),
                        last_llm_workspace=str(turn_workspace),
                        last_error="finish blocked by command coverage check",
                        last_system_note=f"完了がブロックされました: 要求されたコマンドがまだ実行されていません: {', '.join(missing_commands)}",
                        worker_running=self._worker_running(),
                    )
                    continue
                missing_artifacts = self._missing_expected_artifacts(user_message=recent_user_message)
                if missing_artifacts:
                    self._append_session_event(
                        self.root,
                        session_id,
                        {
                            "type": "system_note",
                            "role": "system",
                            "content": f"完了がブロックされました: 期待される成果物が見つかりません: {', '.join(missing_artifacts)}",
                            "code": "finish_blocked",
                            "reason_code": "missing_expected_artifacts",
                            "details": {
                                "missing_artifacts": list(missing_artifacts),
                                "missing_requirements": [f"write_file {artifact}" for artifact in missing_artifacts],
                                "failure_type": "missing_expected_artifacts",
                                "blocked_by": "finish_contract_expected_artifacts",
                                "allowed_next_actions": [f"write_file {artifact}" for artifact in missing_artifacts],
                                "suggested_fix": "期待される成果物を作成し、必要な検証を実行してからfinishしてください。",
                                "next_required_action": f"create the missing artifact: {missing_artifacts[0]}",
                            },
                            "turn_id": turn_id,
                            "queue_id": queue_id,
                            "step_index": step_index,
                            "llm_workspace": str(turn_workspace),
                        },
                    )
                    self._record_observer_judgement_note(
                        session_id=session_id,
                        turn_id=turn_id,
                        queue_id=queue_id,
                        step_index=step_index,
                        user_message=recent_user_message,
                        assistant_message=assistant_message,
                        system_decision=f"完了をブロックしました: 期待される成果物が見つかりません: {', '.join(missing_artifacts)}",
                        reason_code="missing_expected_artifacts",
                        prompt_snapshot=prompt,
                        steps=steps,
                    )
                    self._write_runtime_status(
                        status="running",
                        current_role=selection["role"],
                        current_turn_id=turn_id,
                        current_queue_id=queue_id,
                        current_user_message=recent_user_message,
                        current_prompt_preview=prompt[:2000],
                        current_stream_text=f"Finish blocked. Missing artifacts: {', '.join(missing_artifacts)}",
                        current_plan=planning_note,
                        current_phase="EXECUTE_MISSING_COMMANDS",
                        current_model=selection["model"],
                        current_model_reason=selection["reason"],
                        current_tool=None,
                        current_llm_workspace=str(turn_workspace),
                        last_llm_workspace=str(turn_workspace),
                        last_error="finish blocked by expected artifact check",
                        last_system_note=f"完了がブロックされました: 期待される成果物が見つかりません: {', '.join(missing_artifacts)}",
                        worker_running=self._worker_running(),
                    )
                    continue
                final_answer = str(tool_args.get("final_answer") or assistant_message or "Task finished.")
                if str(selection.get("role") or "") == "terminal":
                    synthesized_answer = self._synthesize_terminal_final_answer(
                        goal_text=goal_text,
                        user_message=recent_user_message,
                        steps=steps,
                    )
                    if synthesized_answer:
                        final_answer = synthesized_answer
                grounding_issues = self._grounding_issues(
                    user_message=recent_user_message,
                    final_answer=final_answer,
                    steps=steps,
                )
                if grounding_issues:
                    issue_text = "; ".join(grounding_issues)
                    judge_trace = self._last_grounding_judge_trace
                    judge_decision = str((judge_trace or {}).get("decision") or "unknown")
                    if judge_decision == "ng":
                        finish_reason_code = "grounding_issues"
                    elif judge_decision == "error":
                        finish_reason_code = "judge_error"
                    elif judge_decision in {"invalid_output", "invalid_json", "empty_output"}:
                        finish_reason_code = "judge_invalid_output"
                    else:
                        finish_reason_code = "grounding_issues"
                    if judge_trace:
                        if judge_decision == "ng":
                            judge_note = "根拠判定: NG。judge は最終回答が証拠から逸脱していると判定しました。"
                        elif judge_decision == "error":
                            judge_note = "根拠判定: ERROR。judge 実行中にエラーが発生したため、完了判定に失敗しました。"
                        else:
                            judge_note = "根拠判定: INVALID_OUTPUT。judge が有効な判定JSONを返さなかったため、完了判定に失敗しました。"
                        self._append_session_event(
                            self.root,
                            session_id,
                            {
                                "type": "system_note",
                                "role": "system",
                                "content": judge_note,
                                "code": "grounding_judge",
                                "reason_code": judge_decision,
                                "details": judge_trace,
                                "turn_id": turn_id,
                                "queue_id": queue_id,
                                "step_index": step_index,
                                "llm_workspace": str(turn_workspace),
                            },
                        )
                    self._append_session_event(
                        self.root,
                        session_id,
                        {
                            "type": "system_note",
                            "role": "system",
                            "content": f"完了がブロックされました: {issue_text}",
                            "code": "finish_blocked",
                            "reason_code": finish_reason_code,
                            "details": {
                                "issues": list(grounding_issues),
                                "missing_requirements": list(grounding_issues),
                                "judge": judge_trace,
                                "failure_type": finish_reason_code,
                                "blocked_by": "finish_contract_grounding",
                                "allowed_next_actions": [
                                    "read_file or run_command to gather missing evidence",
                                    "finish with a final_answer grounded only in observed evidence",
                                ],
                                "suggested_fix": (
                                    "最終回答を直近のtool_result/system_noteで観測済みの事実だけに合わせるか、"
                                    "不足証拠をread_file/run_commandで取得してください。"
                                ),
                                "next_required_action": "ground the final answer in observed evidence, or gather the missing evidence first",
                            },
                            "turn_id": turn_id,
                            "queue_id": queue_id,
                            "step_index": step_index,
                            "llm_workspace": str(turn_workspace),
                        },
                    )
                    self._record_observer_judgement_note(
                        session_id=session_id,
                        turn_id=turn_id,
                        queue_id=queue_id,
                        step_index=step_index,
                        user_message=recent_user_message,
                        assistant_message=assistant_message,
                        system_decision=f"完了をブロックしました: {issue_text}",
                        reason_code=finish_reason_code,
                        prompt_snapshot=prompt,
                        steps=steps,
                    )
                    self._write_runtime_status(
                        status="running",
                        current_role=selection["role"],
                        current_turn_id=turn_id,
                        current_queue_id=queue_id,
                        current_user_message=recent_user_message,
                        current_prompt_preview=prompt[:2000],
                        current_stream_text=f"Finish blocked. {issue_text}",
                        current_plan=planning_note,
                        current_phase="SYNTHESIZE_FROM_EVIDENCE",
                        current_model=selection["model"],
                        current_model_reason=selection["reason"],
                        current_tool=None,
                        current_llm_workspace=str(turn_workspace),
                        last_llm_workspace=str(turn_workspace),
                        last_error="finish blocked by grounding check",
                        last_system_note=f"完了がブロックされました: {issue_text}",
                        worker_running=self._worker_running(),
                    )
                    continue
                acceptance = self._finish_acceptance_evaluation(
                    user_message=recent_user_message,
                    final_answer=final_answer,
                    steps=steps,
                )
                self._append_session_event(
                    self.root,
                    session_id,
                    {
                        "type": "system_note",
                        "role": "system",
                        "content": f"完了受理判定: {acceptance.get('status')}",
                        "code": "finish_acceptance",
                        "reason_code": self._finish_acceptance_reason_code(acceptance),
                        "details": acceptance,
                        "turn_id": turn_id,
                        "queue_id": queue_id,
                        "step_index": step_index,
                        "llm_workspace": str(turn_workspace),
                    },
                )
                if not self._finish_status_is_accepted(acceptance.get("status")):
                    block_text = self._finish_acceptance_block_text(acceptance)
                    self._append_session_event(
                        self.root,
                        session_id,
                        {
                            "type": "system_note",
                            "role": "system",
                            "content": block_text,
                            "code": "finish_blocked",
                            "reason_code": "finish_acceptance_failed",
                            "details": {
                                **acceptance,
                                "failure_type": "finish_acceptance_failed",
                                "blocked_by": "finish_acceptance_gate",
                                "allowed_next_actions": list(acceptance.get("allowed_next_actions") or ["revise evidence or final_answer before retrying finish"]),
                                "suggested_fix": str(acceptance.get("suggested_fix") or acceptance.get("reason") or block_text),
                                "next_required_action": str(acceptance.get("next_required_action") or acceptance.get("suggested_fix") or "revise the blocked finish evidence before retrying"),
                            },
                            "turn_id": turn_id,
                            "queue_id": queue_id,
                            "step_index": step_index,
                            "llm_workspace": str(turn_workspace),
                        },
                    )
                    self._write_runtime_status(
                        status="running",
                        current_role=selection["role"],
                        current_turn_id=turn_id,
                        current_queue_id=queue_id,
                        current_user_message=recent_user_message,
                        current_prompt_preview=prompt[:2000],
                        current_stream_text=f"Finish blocked. {block_text}",
                        current_plan=planning_note,
                        current_phase="REVISE_FROM_ACCEPTANCE",
                        current_model=selection["model"],
                        current_model_reason=selection["reason"],
                        current_tool=None,
                        current_llm_workspace=str(turn_workspace),
                        last_llm_workspace=str(turn_workspace),
                        last_error="finish blocked by acceptance check",
                        last_system_note=block_text,
                        worker_running=self._worker_running(),
                    )
                    continue
                self._append_session_event(
                    self.root,
                    session_id,
                    {
                        "type": "finish",
                        "role": "assistant",
                        "content": final_answer,
                        "model": selection["model"],
                        "model_reason": selection["reason"],
                        "llm_attempt_count": telemetry["attempt_count"],
                        "turn_id": turn_id,
                        "queue_id": queue_id,
                        "step_index": step_index,
                        "llm_workspace": str(turn_workspace),
                    },
                )
                self._write_runtime_status(
                    status="idle",
                    current_role=selection["role"],
                    current_turn_id=None,
                    current_queue_id=None,
                    current_user_message=None,
                    current_prompt_preview=None,
                    current_stream_text="",
                    current_plan=None,
                    current_phase="FINISH",
                    current_model=selection["model"],
                    current_model_reason=selection["reason"],
                    current_tool="finish",
                    current_operation_id=None,
                    current_llm_workspace=None,
                    last_llm_workspace=str(turn_workspace),
                    last_error=None,
                    last_system_note=None,
                    current_started_at=None,
                    current_finished_at=now_iso(),
                    worker_running=self._worker_running(),
                )
                finish_operation("finished", output_preview=final_answer)
                return {"ok": True, "session_id": session_id, "steps": steps, "final_answer": final_answer}
            if tool_name == "run_command" and self._controller_finish_blocked_for_current_evidence(recent_events):
                command_text = str(tool_args.get("command") or "").strip()
                message = (
                    "run_command がブロックされました: 直近の未達条件を解消する corrective edit がまだありません。"
                    " 同じ成果物の再実行では visible_result_sanity_passed を満たせません。"
                )
                self._append_session_event(
                    self.root,
                    session_id,
                    {
                        "type": "system_note",
                        "role": "system",
                        "content": message,
                        "code": "command_blocked",
                        "reason_code": "corrective_action_required",
                        "details": {
                            "blocked_tool": "run_command",
                            "command": command_text,
                            "failure_type": "corrective_action_required",
                            "blocked_by": "runtime_command_gate",
                            "allowed_next_actions": ["replace_text", "write_file", "append_file"],
                            "suggested_fix": "問題のある成果物を修正し、その後に再実行して表示結果を更新してください。",
                            "next_required_action": "perform a corrective edit before rerunning the command",
                        },
                        "turn_id": turn_id,
                        "queue_id": queue_id,
                        "step_index": step_index,
                        "llm_workspace": str(turn_workspace),
                    },
                )
                self._write_runtime_status(
                    status="running",
                    current_role=selection["role"],
                    current_turn_id=turn_id,
                    current_queue_id=queue_id,
                    current_user_message=recent_user_message,
                    current_prompt_preview=prompt[:2000],
                    current_stream_text=f"Command blocked. {message}",
                    current_plan=planning_note,
                    current_phase="REVISE_FROM_ACCEPTANCE",
                    current_model=selection["model"],
                    current_model_reason=selection["reason"],
                    current_tool=None,
                    current_llm_workspace=str(turn_workspace),
                    last_llm_workspace=str(turn_workspace),
                    last_error="run_command blocked until corrective action",
                    last_system_note=message,
                    worker_running=self._worker_running(),
                )
                continue
            self._append_session_event(
                self.root,
                session_id,
                {
                    "type": "tool_call",
                    "tool_name": tool_name,
                    "tool_args": tool_args,
                    "model": selection["model"],
                    "model_reason": selection["reason"],
                    "llm_attempt_count": telemetry["attempt_count"],
                    "turn_id": turn_id,
                    "queue_id": queue_id,
                    "step_index": step_index,
                    "llm_workspace": str(turn_workspace),
                },
            )
            if tool_name == "run_command":
                repeated_command_allowed = self._implementation_progress_allows_repeated_command(
                    user_message=recent_user_message,
                    tool_args=dict(tool_args),
                    steps=steps,
                    session_id=session_id,
                    turn_workspace=turn_workspace,
                )
                redundant_reason = (
                    ""
                    if repeated_command_allowed
                    else self._redundant_command_reason(tool_args=tool_args, steps=steps)
                )
                if redundant_reason:
                    previous_result = (steps[-1].get("tool_result") if steps else {}) or {}
                    previous_failed = not bool(previous_result.get("ok"))
                    self._append_session_event(
                        self.root,
                        session_id,
                        {
                            "type": "system_note",
                            "role": "system",
                            "content": redundant_reason,
                            "code": "command_blocked",
                            "reason_code": "repeated_command",
                            "details": {
                                "command": str(tool_args.get("command") or "").strip(),
                                "blocked_tool": "run_command",
                                "failure_type": "repeated_command",
                                "blocked_by": "runtime_command_gate",
                                "allowed_next_actions": ["read_file", "replace_text", "write_file", "append_file"],
                                "suggested_fix": "同じコマンドを再実行せず、直前結果を使うか、失敗原因を修正する観測/編集へ進んでください。",
                                "next_required_action": "use existing evidence, or inspect/edit before rerunning",
                            },
                            "turn_id": turn_id,
                            "queue_id": queue_id,
                            "step_index": step_index,
                            "llm_workspace": str(turn_workspace),
                        },
                    )
                    self._write_runtime_status(
                        status="running",
                        current_role=selection["role"],
                        current_turn_id=turn_id,
                        current_queue_id=queue_id,
                        current_user_message=recent_user_message,
                        current_prompt_preview=prompt[:2000],
                        current_stream_text=redundant_reason,
                        current_plan=planning_note,
                        current_model=selection["model"],
                        current_model_reason=selection["reason"],
                        current_tool=None,
                        current_llm_workspace=str(turn_workspace),
                        last_llm_workspace=str(turn_workspace),
                        last_error=redundant_reason,
                        last_system_note=redundant_reason,
                        worker_running=self._worker_running(),
                    )
                    if previous_failed:
                        self._record_reflection(
                            session_id=session_id,
                            turn_id=turn_id,
                            queue_id=queue_id,
                            user_message=recent_user_message,
                            reason=redundant_reason,
                            steps=steps,
                        )
                        self._write_runtime_status(
                            status="idle",
                            current_role=selection["role"],
                            current_turn_id=None,
                            current_queue_id=None,
                            current_user_message=None,
                            current_prompt_preview=None,
                            current_stream_text=redundant_reason,
                            current_plan=None,
                            current_phase="FINISH",
                            current_model=selection["model"],
                            current_model_reason=selection["reason"],
                            current_tool=None,
                            current_operation_id=None,
                            current_llm_workspace=None,
                            last_llm_workspace=str(turn_workspace),
                            last_error=redundant_reason,
                            last_system_note=redundant_reason,
                            current_started_at=None,
                            current_finished_at=now_iso(),
                            worker_running=self._worker_running(),
                        )
                        finish_operation("failed", output_preview=redundant_reason)
                        return {
                            "ok": False,
                            "session_id": session_id,
                            "steps": steps,
                            "error": redundant_reason,
                        }
                    continue
                similar_warning = "" if repeated_command_allowed else self._similar_command_warning(tool_args=tool_args, steps=steps)
                if similar_warning:
                    self._append_session_event(
                        self.root,
                        session_id,
                        {
                            "type": "system_note",
                            "role": "system",
                            "content": similar_warning,
                            "code": "command_similarity_warning",
                            "reason_code": "similar_recent_command",
                            "details": {"command": str(tool_args.get("command") or "").strip()},
                            "turn_id": turn_id,
                            "queue_id": queue_id,
                            "step_index": step_index,
                            "llm_workspace": str(turn_workspace),
                        },
                    )
            implementation_phase_block = self._implementation_task_phase_action_block(
                user_message=recent_user_message,
                tool_name=tool_name,
                tool_args=dict(tool_args),
                steps=steps,
                session_id=session_id,
                turn_workspace=turn_workspace,
            )
            if implementation_phase_block is not None:
                progress_block = ProgressBlockDecision.from_phase_block(
                    tool_name=tool_name,
                    tool_args=tool_args,
                    phase_block=implementation_phase_block,
                )
                message = progress_block.message
                reason_code = progress_block.reason_code
                blocked_path = progress_block.path
                block_signature = progress_block.block_signature
                repeated_block_count = self._recent_same_blocked_action_count(
                    session_id=session_id,
                    code="implementation_task_progress_blocked",
                    reason_code=reason_code,
                    blocked_tool=tool_name,
                    path=blocked_path,
                    block_signature=block_signature,
                )
                self._append_session_event(
                    self.root,
                    session_id,
                    {
                        "type": "system_note",
                        "role": "system",
                        "content": progress_block.visible_message(),
                        "code": "implementation_task_progress_blocked",
                        "reason_code": reason_code,
                        "details": progress_block.event_details(),
                        "turn_id": turn_id,
                        "queue_id": queue_id,
                        "step_index": step_index,
                        "llm_workspace": str(turn_workspace),
                    },
                )
                if repeated_block_count >= 2 and not progress_block.terminal_failure:
                    failure_message = (
                        "同じ implementation task progress block が繰り返されました。runtimeは現在許可される"
                        "次アクションを提示しましたが、同じ不許可アクションが再提案されたため、このrunを"
                        "同型失敗サンプルとして停止します。"
                    )
                    self._append_session_event(
                        self.root,
                        session_id,
                        {
                            "type": "system_note",
                            "role": "system",
                            "content": failure_message,
                            "code": "blocked_action_ignored",
                            "reason_code": "blocked_action_ignored_repeatedly",
                            "details": {
                                "blocked_event_code": "implementation_task_progress_blocked",
                                "blocked_reason_code": reason_code,
                                "blocked_tool": tool_name,
                                "path": blocked_path,
                                "block_signature": block_signature,
                                "failure_type": "blocked_action_ignored",
                                "blocked_by": "implementation_task_progress_controller",
                                "repeated_block_count": repeated_block_count + 1,
                                "allowed_next_actions": list(progress_block.allowed_next_actions),
                                "suggested_fix": progress_block.suggested_fix or "許可された次アクションだけを実行してください。",
                                "next_required_action": progress_block.next_required_action or "choose one allowed_next_actions item",
                            },
                            "turn_id": turn_id,
                            "queue_id": queue_id,
                            "step_index": step_index,
                            "llm_workspace": str(turn_workspace),
                        },
                    )
                    self._write_runtime_status(
                        status="idle",
                        current_role=selection["role"],
                        current_turn_id=None,
                        current_queue_id=None,
                        current_user_message=None,
                        current_prompt_preview=prompt[:2000],
                        current_stream_text=failure_message,
                        current_plan=planning_note,
                        current_phase="IMPLEMENTATION_TASK_PROGRESS",
                        current_model=selection["model"],
                        current_model_reason=selection["reason"],
                        current_tool=None,
                        current_operation_id=None,
                        current_llm_workspace=None,
                        last_llm_workspace=str(turn_workspace),
                        last_error=failure_message,
                        last_system_note=failure_message,
                        current_started_at=None,
                        current_finished_at=now_iso(),
                        worker_running=self._worker_running(),
                    )
                    finish_operation("failed", output_preview=failure_message)
                    return {
                        "ok": False,
                        "session_id": session_id,
                        "steps": steps,
                        "error": failure_message,
                    }
                self._write_runtime_status(
                    status="running",
                    current_role=selection["role"],
                    current_turn_id=turn_id,
                    current_queue_id=queue_id,
                    current_user_message=recent_user_message,
                    current_prompt_preview=prompt[:2000],
                    current_stream_text=progress_block.status_stream_text(),
                    current_plan=planning_note,
                    current_phase="IMPLEMENTATION_TASK_PROGRESS",
                    current_model=selection["model"],
                    current_model_reason=selection["reason"],
                    current_tool=None,
                    current_llm_workspace=str(turn_workspace),
                    last_llm_workspace=str(turn_workspace),
                    last_error=message,
                    last_system_note=message,
                    worker_running=self._worker_running(),
                )
                if progress_block.terminal_failure:
                    failure_message = message
                    self._append_session_event(
                        self.root,
                        session_id,
                        {
                            "type": "system_note",
                            "role": "system",
                            "content": failure_message,
                            "code": "implementation_task_semantic_review_ignored",
                            "reason_code": str(implementation_phase_block.get("reason_code") or "semantic_review_ignored"),
                            "details": {
                                "blocked_tool": tool_name,
                                "path": str(implementation_phase_block.get("path") or tool_args.get("path") or ""),
                                "failure_type": str(implementation_phase_block.get("failure_type") or "semantic_review_ignored"),
                                "blocked_by": "implementation_task_progress_controller",
                                "repeated_semantic_issue": implementation_phase_block.get("repeated_semantic_issue") or {},
                                "allowed_next_actions": list(implementation_phase_block.get("allowed_next_actions") or []),
                                "suggested_fix": str(implementation_phase_block.get("suggested_fix") or ""),
                                "next_required_action": str(implementation_phase_block.get("next_required_action") or implementation_phase_block.get("suggested_fix") or "start a revised implementation strategy"),
                            },
                            "turn_id": turn_id,
                            "queue_id": queue_id,
                            "step_index": step_index,
                            "llm_workspace": str(turn_workspace),
                        },
                    )
                    self._write_runtime_status(
                        status="idle",
                        current_role=selection["role"],
                        current_turn_id=None,
                        current_queue_id=None,
                        current_user_message=None,
                        current_prompt_preview=prompt[:2000],
                        current_stream_text=failure_message,
                        current_plan=planning_note,
                        current_phase="IMPLEMENTATION_TASK_PROGRESS",
                        current_model=selection["model"],
                        current_model_reason=selection["reason"],
                        current_tool=None,
                        current_operation_id=None,
                        current_llm_workspace=None,
                        last_llm_workspace=str(turn_workspace),
                        last_error=failure_message,
                        last_system_note=failure_message,
                        current_started_at=None,
                        current_finished_at=now_iso(),
                        worker_running=self._worker_running(),
                    )
                    finish_operation("failed", output_preview=failure_message)
                    return {
                        "ok": False,
                        "session_id": session_id,
                        "steps": steps,
                        "error": failure_message,
                    }
                continue
            edit_validation_failure = self._edit_observation_required_after_validation_failure(
                tool_name=tool_name,
                tool_args=dict(tool_args),
                steps=steps,
            )
            repeated_recovery_observation = self._repeated_recovery_observation_block(
                tool_name=tool_name,
                tool_args=dict(tool_args),
                steps=steps,
            )
            post_implementation_observation = self._post_implementation_test_progress_observation_block(
                user_message=recent_user_message,
                tool_name=tool_name,
                tool_args=dict(tool_args),
                steps=steps,
                progress_state=self._implementation_task_progress_state(
                    user_message=recent_user_message,
                    steps=steps,
                    session_id=session_id,
                    turn_workspace=turn_workspace,
                ),
            )
            if post_implementation_observation is not None:
                path = str(post_implementation_observation.get("path") or tool_args.get("path") or "")
                reason_code = str(post_implementation_observation.get("reason_code") or "post_implementation_observation_loop")
                suggested_fix = str(post_implementation_observation.get("suggested_fix") or "")
                message = f"{tool_name} がブロックされました: {suggested_fix}"
                repeated_block_count = self._recent_same_blocked_action_count(
                    session_id=session_id,
                    code="observation_blocked",
                    reason_code=reason_code,
                    blocked_tool=tool_name,
                    path=path,
                )
                self._append_session_event(
                    self.root,
                    session_id,
                    {
                        "type": "system_note",
                        "role": "system",
                        "content": message,
                        "code": "observation_blocked",
                        "reason_code": reason_code,
                        "details": {
                            "blocked_tool": tool_name,
                            "path": path,
                            "failure_type": "observation_blocked",
                            "blocked_by": "runtime_observation_gate",
                            "allowed_next_actions": list(post_implementation_observation.get("allowed_next_actions") or []),
                            "suggested_fix": suggested_fix,
                            "next_required_action": str(post_implementation_observation.get("next_required_action") or suggested_fix or "choose one allowed_next_actions item"),
                            "missing_requirements": list(post_implementation_observation.get("missing_requirements") or []),
                        },
                        "turn_id": turn_id,
                        "queue_id": queue_id,
                        "step_index": step_index,
                        "llm_workspace": str(turn_workspace),
                    },
                )
                if repeated_block_count >= 2:
                    failure_message = (
                        "同じblocked actionが繰り返されました。runtimeはすでに許可される次アクションを提示しましたが、"
                        "同じ不許可アクションが再提案されたため、このrunを同型失敗サンプルとして停止します。"
                    )
                    self._append_session_event(
                        self.root,
                        session_id,
                        {
                            "type": "system_note",
                            "role": "system",
                            "content": failure_message,
                            "code": "blocked_action_ignored",
                            "reason_code": "blocked_action_ignored_repeatedly",
                            "details": {
                                "blocked_event_code": "observation_blocked",
                                "blocked_reason_code": reason_code,
                                "blocked_tool": tool_name,
                                "path": path,
                                "failure_type": "blocked_action_ignored",
                                "blocked_by": "runtime_observation_gate",
                                "repeated_block_count": repeated_block_count + 1,
                                "allowed_next_actions": list(post_implementation_observation.get("allowed_next_actions") or []),
                                "missing_requirements": list(post_implementation_observation.get("missing_requirements") or []),
                                "suggested_fix": str(post_implementation_observation.get("suggested_fix") or "許可された次アクションだけを実行してください。"),
                                "next_required_action": str(post_implementation_observation.get("next_required_action") or post_implementation_observation.get("suggested_fix") or "choose one allowed_next_actions item"),
                            },
                            "turn_id": turn_id,
                            "queue_id": queue_id,
                            "step_index": step_index,
                            "llm_workspace": str(turn_workspace),
                        },
                    )
                    self._write_runtime_status(
                        status="idle",
                        current_role=selection["role"],
                        current_turn_id=None,
                        current_queue_id=None,
                        current_user_message=None,
                        current_prompt_preview=prompt[:2000],
                        current_stream_text=failure_message,
                        current_plan=planning_note,
                        current_phase="IMPLEMENTATION_TASK_PROGRESS",
                        current_model=selection["model"],
                        current_model_reason=selection["reason"],
                        current_tool=None,
                        current_operation_id=None,
                        current_llm_workspace=None,
                        last_llm_workspace=str(turn_workspace),
                        last_error=failure_message,
                        last_system_note=failure_message,
                        current_started_at=None,
                        current_finished_at=now_iso(),
                        worker_running=self._worker_running(),
                    )
                    finish_operation("failed", output_preview=failure_message)
                    return {
                        "ok": False,
                        "session_id": session_id,
                        "steps": steps,
                        "error": failure_message,
                    }
                self._write_runtime_status(
                    status="running",
                    current_role=selection["role"],
                    current_turn_id=turn_id,
                    current_queue_id=queue_id,
                    current_user_message=recent_user_message,
                    current_prompt_preview=prompt[:2000],
                    current_stream_text=message,
                    current_plan=planning_note,
                    current_phase=current_phase,
                    current_model=selection["model"],
                    current_model_reason=selection["reason"],
                    current_tool=None,
                    current_llm_workspace=str(turn_workspace),
                    last_llm_workspace=str(turn_workspace),
                    last_error=message,
                    last_system_note=message,
                    worker_running=self._worker_running(),
                )
                continue
            if repeated_recovery_observation is not None:
                path = str(repeated_recovery_observation.get("path") or tool_args.get("path") or "")
                message = (
                    f"{tool_name} がブロックされました: {path} は検証失敗後にすでに観測済みです。"
                    " 同じ観測を繰り返さず、修正または別の必要な観測に進んでください。"
                )
                self._append_session_event(
                    self.root,
                    session_id,
                    {
                        "type": "system_note",
                        "role": "system",
                        "content": message,
                        "code": "observation_blocked",
                        "reason_code": str(repeated_recovery_observation.get("reason_code") or "repeated_recovery_observation"),
                        "details": {
                            "blocked_tool": tool_name,
                            "path": path,
                            "failure_type": "repeated_recovery_observation",
                            "blocked_by": "runtime_observation_gate",
                            "allowed_next_actions": list(repeated_recovery_observation.get("allowed_next_actions") or ["replace_text", "write_file"]),
                            "suggested_fix": str(repeated_recovery_observation.get("suggested_fix") or ""),
                            "next_required_action": str(repeated_recovery_observation.get("next_required_action") or repeated_recovery_observation.get("suggested_fix") or "edit the already-observed failing target"),
                        },
                        "turn_id": turn_id,
                        "queue_id": queue_id,
                        "step_index": step_index,
                        "llm_workspace": str(turn_workspace),
                    },
                )
                self._write_runtime_status(
                    status="running",
                    current_role=selection["role"],
                    current_turn_id=turn_id,
                    current_queue_id=queue_id,
                    current_user_message=recent_user_message,
                    current_prompt_preview=prompt[:2000],
                    current_stream_text=message,
                    current_plan=planning_note,
                    current_phase=current_phase,
                    current_model=selection["model"],
                    current_model_reason=selection["reason"],
                    current_tool=None,
                    current_llm_workspace=str(turn_workspace),
                    last_llm_workspace=str(turn_workspace),
                    last_error=message,
                    last_system_note=message,
                    worker_running=self._worker_running(),
                )
                continue
            if edit_validation_failure is not None:
                path = str(tool_args.get("path") or edit_validation_failure.get("path") or "")
                syntax_error = str(edit_validation_failure.get("syntax_error") or "")
                reason_code = str(edit_validation_failure.get("_recovery_reason_code") or "read_file_required_after_validation_failed")
                allowed_next_actions = list(edit_validation_failure.get("_allowed_next_actions") or ["read_file"])
                suggested_fix = str(edit_validation_failure.get("_suggested_fix") or f"read_file で {path} の現在内容を確認し、構文が通る完全なPythonファイルとして修正してください。")
                route_progress_state: dict[str, Any] = {}
                if reason_code in {"read_file_required_after_validation_failed", "read_file_required_after_edit_failed"} and path:
                    route_progress_state = self._implementation_task_progress_state(
                        user_message=recent_user_message,
                        steps=steps,
                        session_id=session_id,
                        turn_workspace=turn_workspace,
                    )
                    route_name_for_recovery = str(route_progress_state.get("route_name") or "")
                    route_allowed_for_recovery = [
                        str(item)
                        for item in route_progress_state.get("route_allowed_next_actions") or []
                        if str(item).strip()
                    ]
                    route_read_allowed = (
                        f"read_file {path}" in route_allowed_for_recovery
                        or f"read_file {path} once" in route_allowed_for_recovery
                    )
                    if route_name_for_recovery and not route_read_allowed:
                        reason_code = "route_recovery_read_not_allowed"
                        allowed_next_actions = route_allowed_for_recovery
                        suggested_fix = (
                            f"{route_name_for_recovery} では controller recovery の read_file {path} は許可外です。"
                            "route_allowed_next_actions に従い、再読せずに実装契約未達を減らす編集へ進んでください。"
                        )
                if reason_code == "replace_or_write_required_after_validation_failed":
                    message = (
                        f"{tool_name} がブロックされました: {path} は直前の構文検証失敗後に read_file 済みです。"
                        " 同じファイルへの append_file 再試行は失敗の反復になりやすいため、replace_text または write_file で修正してください。"
                    )
                elif reason_code == "route_recovery_read_not_allowed":
                    message = (
                        f"{tool_name} がブロックされました: {path} の controller recovery read はroute契約上許可されていません。"
                        " 同じファイルを再読せず、route_allowed_next_actions に従って修正してください。"
                    )
                elif reason_code == "read_file_required_after_edit_failed":
                    message = (
                        f"{tool_name} がブロックされました: {path} は直前の編集で対象一致に失敗しています。"
                        " 現在のファイル内容を read_file で観測してから、正確な old_text で replace_text するか write_file で修正してください。"
                    )
                elif reason_code == "replace_or_write_required_after_edit_failed":
                    message = (
                        f"{tool_name} がブロックされました: {path} は直前の編集失敗後に read_file 済みです。"
                        " append_file で断片を足さず、replace_text または write_file で修正してください。"
                    )
                elif reason_code == "write_file_required_after_repeated_edit_match_failure":
                    message = (
                        f"{tool_name} がブロックされました: {path} への局所置換が繰り返し対象一致に失敗しています。"
                        " 同じ replace_text 戦略を続けず、write_file で完全な有効Pythonとして書き直してください。"
                    )
                else:
                    message = (
                        f"{tool_name} がブロックされました: {path} は直前の編集で構文検証に失敗しています。"
                        " 現在のファイル内容を read_file で観測してから、replace_text または write_file で修正してください。"
                    )
                previous_failure_type = str(edit_validation_failure.get("failure_type") or "")
                self._append_session_event(
                    self.root,
                    session_id,
                    {
                        "type": "system_note",
                        "role": "system",
                        "content": message,
                        "code": "edit_blocked",
                        "reason_code": reason_code,
                        "details": {
                            "blocked_tool": tool_name,
                            "path": path,
                            "previous_failure_type": previous_failure_type,
                            "failure_type": previous_failure_type or "edit_recovery_blocked",
                            "blocked_by": "runtime_edit_validation",
                            "syntax_error": syntax_error,
                            "allowed_next_actions": allowed_next_actions,
                            "suggested_fix": suggested_fix,
                            "next_required_action": str(edit_validation_failure.get("next_required_action") or suggested_fix or "choose one allowed_next_actions item"),
                        },
                        "turn_id": turn_id,
                        "queue_id": queue_id,
                        "step_index": step_index,
                        "llm_workspace": str(turn_workspace),
                    },
                )
                self._write_runtime_status(
                    status="running",
                    current_role=selection["role"],
                    current_turn_id=turn_id,
                    current_queue_id=queue_id,
                    current_user_message=recent_user_message,
                    current_prompt_preview=prompt[:2000],
                    current_stream_text=message,
                    current_plan=planning_note,
                    current_phase=current_phase,
                    current_model=selection["model"],
                    current_model_reason=selection["reason"],
                    current_tool=None,
                    current_llm_workspace=str(turn_workspace),
                    last_llm_workspace=str(turn_workspace),
                    last_error=message,
                    last_system_note=message,
                    worker_running=self._worker_running(),
                )
                if reason_code in {"read_file_required_after_validation_failed", "read_file_required_after_edit_failed"} and path:
                    recovery_tool = "read_file"
                    recovery_args = {"path": path}
                    self._append_runtime_event(
                        session_id,
                        event_name="tool_call_started",
                        content=f"Controller recovery: read_file {path}",
                        details={
                            "tool_name": recovery_tool,
                            "tool_args": recovery_args,
                            "reason_code": "controller_recovery_read_file",
                            "blocked_tool": tool_name,
                        },
                        turn_id=turn_id,
                        queue_id=queue_id,
                        step_index=step_index,
                        llm_workspace=str(turn_workspace),
                        phase=current_phase,
                    )
                    try:
                        recovery_result = self.tools.execute(recovery_tool, recovery_args)
                    except Exception as exc:
                        recovery_result = {"ok": False, "tool": recovery_tool, "path": path, "error": str(exc)}
                    self._append_session_event(
                        self.root,
                        session_id,
                        {
                            "type": "tool_result",
                            "tool_name": recovery_tool,
                            "content": json.dumps(recovery_result, ensure_ascii=False),
                            "ok": bool(recovery_result.get("ok")),
                            "turn_id": turn_id,
                            "queue_id": queue_id,
                            "step_index": step_index,
                            "model_reason": selection["reason"],
                            "llm_workspace": str(turn_workspace),
                            "reason_code": "controller_recovery_read_file",
                        },
                    )
                    self._append_runtime_event(
                        session_id,
                        event_name="tool_call_finished",
                        content=json.dumps(recovery_result, ensure_ascii=False),
                        details={
                            "tool_name": recovery_tool,
                            "tool_args": recovery_args,
                            "tool_result": recovery_result,
                            "ok": bool(recovery_result.get("ok")),
                            "reason_code": "controller_recovery_read_file",
                        },
                        turn_id=turn_id,
                        queue_id=queue_id,
                        step_index=step_index,
                        llm_workspace=str(turn_workspace),
                        phase=current_phase,
                    )
                    self.frame_manager.update_from_tool_result(recovery_tool, recovery_args, recovery_result)
                    steps.append({"tool_name": recovery_tool, "tool_args": recovery_args, "tool_result": recovery_result})
                continue
            python_contract_issue = self._python_artifact_contract_issue(
                user_message=recent_user_message,
                tool_name=tool_name,
                tool_args=dict(tool_args),
            )
            if python_contract_issue is not None:
                path = str(tool_args.get("path") or "")
                reason_code = str(python_contract_issue.get("reason_code") or "python_artifact_contract_incomplete")
                semantic_review_already_requires_revision = self._latest_semantic_review_requires_revision(session_id=session_id)
                message = f"{tool_name} がブロックされました: {python_contract_issue['message']}"
                blocked_details = {
                    "blocked_tool": tool_name,
                    "path": path,
                    "failure_type": "implementation_contract_failed",
                    "blocked_by": "runtime_python_artifact_contract",
                    "allowed_next_actions": list(python_contract_issue.get("allowed_next_actions") or ["write_file", "replace_text"]),
                    "suggested_fix": str(python_contract_issue.get("suggested_fix") or ""),
                    "next_required_action": str(python_contract_issue.get("next_required_action") or python_contract_issue.get("suggested_fix") or "submit a contract-satisfying Python artifact"),
                }
                for detail_key in [
                    "route_name",
                    "route_phase",
                    "recovery_class",
                    "route_repair_invariant",
                    "placeholder_markers",
                    "block_signature",
                ]:
                    if detail_key in python_contract_issue:
                        blocked_details[detail_key] = python_contract_issue.get(detail_key)
                self._append_session_event(
                    self.root,
                    session_id,
                    {
                        "type": "system_note",
                        "role": "system",
                        "content": message,
                        "code": "edit_blocked",
                        "reason_code": reason_code,
                        "details": blocked_details,
                        "turn_id": turn_id,
                        "queue_id": queue_id,
                        "step_index": step_index,
                        "llm_workspace": str(turn_workspace),
                    },
                )
                route_initial_repair_context = {"active": False}
                route_initial_repair_has_grace = False
                initial_repair_context = self._implementation_initial_repair_context(
                    user_message=recent_user_message,
                    steps=steps,
                    session_id=session_id,
                    turn_workspace=turn_workspace,
                    reason_code=reason_code,
                    blocked_tool=tool_name,
                    path=path,
                    block_signature=str(python_contract_issue.get("block_signature") or ""),
                )
                initial_repair_has_grace = (
                    reason_code == "python_artifact_contract_incomplete"
                    and
                    bool(initial_repair_context.get("active"))
                    and int(initial_repair_context.get("same_block_count") or 0)
                    < int(initial_repair_context.get("same_block_limit") or 0)
                )
                if semantic_review_already_requires_revision and reason_code in {
                    "python_artifact_contract_incomplete",
                    "python_artifact_input_contract_narrowed",
                } and not route_initial_repair_has_grace and not initial_repair_has_grace:
                    failure_code = "implementation_task_semantic_review_ignored"
                    failure_reason_code = "semantic_review_ignored_repeated_implementation_contract"
                    failure_type = "semantic_review_ignored"
                    failure_message = (
                        "semantic implementation review が未達を指摘した後、実装契約違反が再提案されました。"
                        "このrunは同型失敗ループとして停止します。次回はレビュー内容を反映した完全実装戦略へ切り替えてください。"
                    )
                    if reason_code == "python_artifact_contract_incomplete" and bool(initial_repair_context.get("active")):
                        failure_code = "implementation_task_initial_placeholder_loop"
                        failure_reason_code = "initial_implementation_placeholder_loop"
                        failure_type = "initial_implementation_placeholder_loop"
                        failure_message = (
                            "初期実装がplaceholder成果物のまま繰り返されました。"
                            "runtimeは骨組み/stubを実装成果物として受け付けないため、このrunを停止します。"
                            "次回は全callableに実行可能な本体を持つ完全実装戦略へ切り替えてください。"
                        )
                    self._append_session_event(
                        self.root,
                        session_id,
                        {
                            "type": "system_note",
                            "role": "system",
                            "content": failure_message,
                            "code": failure_code,
                            "reason_code": failure_reason_code,
                            "details": {
                                "blocked_tool": tool_name,
                                "path": path,
                                "blocked_reason_code": reason_code,
                                "failure_type": failure_type,
                                "allowed_next_actions": ["start_new_run_with_revised_implementation_strategy"],
                                "route_initial_repair_context": route_initial_repair_context,
                                "initial_repair_context": initial_repair_context,
                            },
                            "turn_id": turn_id,
                            "queue_id": queue_id,
                            "step_index": step_index,
                            "llm_workspace": str(turn_workspace),
                        },
                    )
                    self._write_runtime_status(
                        status="idle",
                        current_role=selection["role"],
                        current_turn_id=None,
                        current_queue_id=None,
                        current_user_message=None,
                        current_prompt_preview=prompt[:2000],
                        current_stream_text=failure_message,
                        current_plan=planning_note,
                        current_phase="IMPLEMENTATION_TASK_PROGRESS",
                        current_model=selection["model"],
                        current_model_reason=selection["reason"],
                        current_tool=None,
                        current_operation_id=None,
                        current_llm_workspace=None,
                        last_llm_workspace=str(turn_workspace),
                        last_error=failure_message,
                        last_system_note=failure_message,
                        current_started_at=None,
                        current_finished_at=now_iso(),
                        worker_running=self._worker_running(),
                    )
                    finish_operation("failed", output_preview=failure_message)
                    return {
                        "ok": False,
                        "session_id": session_id,
                        "steps": steps,
                        "error": failure_message,
                    }
                self._append_blocked_implementation_semantic_review_if_needed(
                    session_id=session_id,
                    turn_id=turn_id,
                    queue_id=queue_id,
                    step_index=step_index,
                    turn_workspace=turn_workspace,
                    user_message=recent_user_message,
                    current_model=str(selection.get("model") or ""),
                    blocked_tool_name=tool_name,
                    blocked_tool_args=dict(tool_args),
                    blocked_issue=python_contract_issue,
                )
                self._write_runtime_status(
                    status="running",
                    current_role=selection["role"],
                    current_turn_id=turn_id,
                    current_queue_id=queue_id,
                    current_user_message=recent_user_message,
                    current_prompt_preview=prompt[:2000],
                    current_stream_text=message,
                    current_plan=planning_note,
                    current_phase=current_phase,
                    current_model=selection["model"],
                    current_model_reason=selection["reason"],
                    current_tool=None,
                    current_llm_workspace=str(turn_workspace),
                    last_llm_workspace=str(turn_workspace),
                    last_error=message,
                    last_system_note=message,
                    worker_running=self._worker_running(),
                )
                continue
            progress_state_for_rewrite = self._implementation_task_progress_state(
                user_message=recent_user_message,
                steps=steps,
                session_id=session_id,
                turn_workspace=turn_workspace,
            )
            implementation_rewrite_loop = self._implementation_rewrite_loop_block(
                user_message=recent_user_message,
                tool_name=tool_name,
                tool_args=dict(tool_args),
                steps=steps,
                progress_state=progress_state_for_rewrite,
            )
            if implementation_rewrite_loop is not None:
                path = str(implementation_rewrite_loop.get("path") or tool_args.get("path") or "")
                message = (
                    f"{tool_name} がブロックされました: {path} はすでに実装成果物として書き込まれています。"
                    " 同じ実装ファイルを書き直し続けず、テスト作成または検証実行に進んでください。"
                )
                self._append_session_event(
                    self.root,
                    session_id,
                    {
                        "type": "system_note",
                        "role": "system",
                        "content": message,
                        "code": "edit_blocked",
                        "reason_code": str(implementation_rewrite_loop.get("reason_code") or "implementation_artifact_rewrite_loop"),
                        "details": {
                            "blocked_tool": tool_name,
                            "path": path,
                            "failure_type": "implementation_artifact_rewrite_loop",
                            "blocked_by": "implementation_task_progress_controller",
                            "allowed_next_actions": list(implementation_rewrite_loop.get("allowed_next_actions") or []),
                            "suggested_fix": str(implementation_rewrite_loop.get("suggested_fix") or ""),
                            "next_required_action": str(implementation_rewrite_loop.get("next_required_action") or implementation_rewrite_loop.get("suggested_fix") or "move to tests or validation instead of rewriting the same implementation"),
                        },
                        "turn_id": turn_id,
                        "queue_id": queue_id,
                        "step_index": step_index,
                        "llm_workspace": str(turn_workspace),
                    },
                )
                self._write_runtime_status(
                    status="running",
                    current_role=selection["role"],
                    current_turn_id=turn_id,
                    current_queue_id=queue_id,
                    current_user_message=recent_user_message,
                    current_prompt_preview=prompt[:2000],
                    current_stream_text=message,
                    current_plan=planning_note,
                    current_phase=current_phase,
                    current_model=selection["model"],
                    current_model_reason=selection["reason"],
                    current_tool=None,
                    current_llm_workspace=str(turn_workspace),
                    last_llm_workspace=str(turn_workspace),
                    last_error=message,
                    last_system_note=message,
                    worker_running=self._worker_running(),
                )
                continue
            command_text = str(tool_args.get("command") or "").strip()
            if (
                tool_name == "run_command"
                and ("unittest" in command_text.lower() or "pytest" in command_text.lower())
                and self._append_semantic_implementation_review_if_needed(
                    session_id=session_id,
                    turn_id=turn_id,
                    queue_id=queue_id,
                    step_index=step_index,
                    turn_workspace=turn_workspace,
                    user_message=recent_user_message,
                    steps=steps,
                    current_model=str(selection.get("model") or ""),
                    trigger="before_unittest",
                )
            ):
                message = (
                    "unittest 実行前に、相談役LLMからの実装レビューをP4へ渡しました。"
                    " レビュー内容を踏まえて、必要なら修正し、問題なければ同じunittestを実行してください。"
                )
                self._write_runtime_status(
                    status="running",
                    current_role=selection["role"],
                    current_turn_id=turn_id,
                    current_queue_id=queue_id,
                    current_user_message=recent_user_message,
                    current_prompt_preview=prompt[:2000],
                    current_stream_text=message,
                    current_plan=planning_note,
                    current_phase=current_phase,
                    current_model=selection["model"],
                    current_model_reason=selection["reason"],
                    current_tool=None,
                    current_llm_workspace=str(turn_workspace),
                    last_llm_workspace=str(turn_workspace),
                    last_system_note=message,
                    worker_running=self._worker_running(),
                )
                continue
            auto_return_after_first_action = self._current_child_first_action_matches(
                tool_name=tool_name,
                tool_args=dict(tool_args),
            )
            self._append_runtime_event(
                session_id,
                event_name="tool_call_started",
                content=(
                    f"Running command via {tool_args.get('shell') or 'auto'}:\n{tool_args.get('command')}"
                    if tool_name == "run_command"
                    else f"Running tool: {tool_name}"
                ),
                details={
                    "tool_name": tool_name,
                    "tool_args": tool_args,
                },
                turn_id=turn_id,
                queue_id=queue_id,
                step_index=step_index,
                llm_workspace=str(turn_workspace),
                phase=current_phase,
            )
            self._write_runtime_status(
                status="running_tool",
                current_role=selection["role"],
                current_turn_id=turn_id,
                current_queue_id=queue_id,
                current_user_message=recent_user_message,
                current_prompt_preview=prompt[:2000],
                current_stream_text=(
                    f"Running command via {tool_args.get('shell') or 'auto'}:\n{tool_args.get('command')}"
                    if tool_name == "run_command"
                    else ""
                ),
                current_plan=planning_note,
                current_phase=current_phase,
                current_model=selection["model"],
                current_model_reason=selection["reason"],
                current_tool=tool_name,
                current_llm_workspace=str(turn_workspace),
                last_llm_workspace=str(turn_workspace),
                current_started_at=read_json(self.paths.runtime_status_path, fallback={}).get("current_started_at") or now_iso(),
                current_finished_at=None,
                worker_running=self._worker_running(),
            )
            try:
                tool_result = self.tools.execute(
                    tool_name,
                    dict(tool_args),
                    on_update=(self._make_tool_stream_updater(
                        session_id=session_id,
                        selection=selection,
                        turn_id=turn_id,
                        queue_id=queue_id,
                        step_index=step_index,
                        recent_user_message=recent_user_message,
                        prompt=prompt,
                        tool_name=tool_name,
                        llm_workspace=str(turn_workspace),
                        current_phase=current_phase,
                    ) if tool_name == "run_command" else None),
                )
            except Exception as exc:
                tool_result = {
                    "ok": False,
                    "tool": tool_name,
                    "error": str(exc),
                    "failure_type": "tool_execution_exception",
                    "blocked_by": "runtime_tool_executor",
                    "allowed_next_actions": [
                        {"tool": tool_name, "strategy": "retry only after correcting the arguments or choosing a safer equivalent action"}
                    ],
                    "suggested_fix": "tool_result.error を読み、同じ不正引数を繰り返さずに次の有効アクションへ進んでください。",
                    "next_required_action": "correct the tool arguments or choose an allowed recovery action",
                }
            self._append_session_event(
                self.root,
                session_id,
                {
                    "type": "tool_result",
                    "tool_name": tool_name,
                    "content": json.dumps(tool_result, ensure_ascii=False),
                    "ok": bool(tool_result.get("ok")),
                    "turn_id": turn_id,
                    "queue_id": queue_id,
                    "step_index": step_index,
                    "model_reason": selection["reason"],
                    "llm_workspace": str(turn_workspace),
                },
            )
            self._append_runtime_event(
                session_id,
                event_name="tool_call_finished",
                content=json.dumps(tool_result, ensure_ascii=False),
                details={
                    "tool_name": tool_name,
                    "tool_args": tool_args,
                    "tool_result": tool_result,
                    "ok": bool(tool_result.get("ok")),
                },
                turn_id=turn_id,
                queue_id=queue_id,
                step_index=step_index,
                llm_workspace=str(turn_workspace),
                phase=current_phase,
            )
            if tool_name == "run_command":
                self._append_session_event(
                    self.root,
                    session_id,
                    {
                        "type": "system_note",
                        "role": "system",
                        "content": (
                            "run_command の実行結果を受信しました"
                            if bool(tool_result.get("ok"))
                            else f"run_command が失敗しました: {tool_result.get('error') or tool_result.get('stderr') or '不明なエラー'}"
                        ),
                        "turn_id": turn_id,
                        "queue_id": queue_id,
                        "step_index": step_index,
                        "llm_workspace": str(turn_workspace),
                    },
                )
                if not bool(tool_result.get("ok")):
                    command_output = "\n".join(
                        str(tool_result.get(key) or "")
                        for key in ("stderr", "stdout", "error")
                        if str(tool_result.get(key) or "")
                    )
                    command_details = {
                        "command": str(tool_result.get("command") or tool_args.get("command") or ""),
                        "returncode": tool_result.get("returncode"),
                        "failure_type": str(tool_result.get("failure_type") or "command_failed"),
                        "blocked_by": str(tool_result.get("blocked_by") or "runtime_command_result"),
                        "traceback_summary": _run_command_traceback_summary(command_output),
                        "stderr_tail": str(tool_result.get("stderr") or "")[-2600:],
                        "stdout_tail": str(tool_result.get("stdout") or "")[-1800:],
                        "error": str(tool_result.get("error") or ""),
                        "allowed_next_actions": list(
                            tool_result.get("allowed_next_actions")
                            or [
                                "read_file the traceback target if not already read",
                                "replace_text or write_file the failing implementation/test target",
                                "rerun validation only after a successful corrective edit",
                            ]
                        ),
                        "suggested_fix": str(
                            tool_result.get("suggested_fix")
                            or tool_result.get("suggested_split_strategy")
                            or "stdout/stderrの具体行を根拠に、失敗対象を修正してから検証を再実行してください。"
                        ),
                        "next_required_action": str(
                            tool_result.get("next_required_action")
                            or "inspect or edit the failing target before rerunning the command"
                        ),
                    }
                    self._append_session_event(
                        self.root,
                        session_id,
                        {
                            "type": "system_note",
                            "role": "system",
                            "content": self._failed_command_guardrail(tool_result=tool_result),
                            "code": "command_failed",
                            "reason_code": "recovery_guidance",
                            "details": command_details,
                            "turn_id": turn_id,
                            "queue_id": queue_id,
                            "step_index": step_index,
                            "llm_workspace": str(turn_workspace),
                        },
                    )
                    consultant_note = self._validation_failure_consultant_note(
                        user_message=recent_user_message,
                        tool_result=tool_result,
                        steps=steps,
                        turn_workspace=turn_workspace,
                        current_model=str(selection.get("model") or ""),
                    )
                    if consultant_note is not None:
                        self._append_session_event(
                            self.root,
                            session_id,
                            {
                                "type": "system_note",
                                "role": "system",
                                "content": f"相談役LLMからの検証失敗レビュー: {consultant_note['advice']}",
                                "code": "validation_failure_consultant",
                                "reason_code": "consultant_advice",
                                "details": consultant_note,
                                "turn_id": turn_id,
                                "queue_id": queue_id,
                                "step_index": step_index,
                                "llm_workspace": str(turn_workspace),
                            },
                        )
            completed_step = {"tool_name": tool_name, "tool_args": dict(tool_args), "tool_result": tool_result}
            preview_steps = steps + [completed_step]
            if (
                bool(tool_result.get("ok"))
                and tool_name in {"write_file", "append_file", "replace_text"}
                and _artifact_path_is_test(str(tool_result.get("path") or tool_args.get("path") or ""))
            ):
                self._append_semantic_implementation_review_if_needed(
                    session_id=session_id,
                    turn_id=turn_id,
                    queue_id=queue_id,
                    step_index=step_index,
                    turn_workspace=turn_workspace,
                    user_message=recent_user_message,
                    steps=preview_steps,
                    current_model=str(selection.get("model") or ""),
                    trigger="artifacts_ready",
                )
            elif (
                tool_name == "run_command"
                and not bool(tool_result.get("ok"))
                and ("unittest" in str(tool_result.get("command") or tool_args.get("command") or "").lower()
                     or "pytest" in str(tool_result.get("command") or tool_args.get("command") or "").lower())
            ):
                self._append_semantic_implementation_review_if_needed(
                    session_id=session_id,
                    turn_id=turn_id,
                    queue_id=queue_id,
                    step_index=step_index,
                    turn_workspace=turn_workspace,
                    user_message=recent_user_message,
                    steps=preview_steps,
                    current_model=str(selection.get("model") or ""),
                    trigger="unittest_failed",
                    failed_tool_result=tool_result,
                )
            self.frame_manager.update_from_tool_result(tool_name, dict(tool_args), tool_result)
            steps.append(completed_step)
            if auto_return_after_first_action and bool(tool_result.get("ok")):
                self._return_after_first_action_success(
                    session_id=session_id,
                    turn_id=turn_id,
                    queue_id=queue_id,
                    step_index=step_index,
                    turn_workspace=turn_workspace,
                    tool_name=tool_name,
                    tool_args=tool_args,
                    tool_result=tool_result,
                )
            elif bool(tool_result.get("ok")) and self._current_child_should_return_after_tool_success(tool_name=tool_name):
                self._return_after_first_action_success(
                    session_id=session_id,
                    turn_id=turn_id,
                    queue_id=queue_id,
                    step_index=step_index,
                    turn_workspace=turn_workspace,
                    tool_name=tool_name,
                    tool_args=tool_args,
                    tool_result=tool_result,
                )
            if int(telemetry.get("attempt_count") or 0) > 0:
                self._maybe_record_observer_note(
                    session_id=session_id,
                    turn_id=turn_id,
                    queue_id=queue_id,
                    step_index=step_index,
                    user_message=recent_user_message,
                    tool_name=tool_name,
                    tool_result=tool_result,
                    steps=steps,
                    prompt_snapshot=prompt,
                    assistant_message=assistant_message,
                )
            next_current_phase = self._current_phase(user_message=recent_user_message, steps=steps)
            next_progress_state = self._implementation_task_progress_state(
                user_message=recent_user_message,
                steps=steps,
                session_id=session_id,
                turn_workspace=turn_workspace,
            )
            self._append_implementation_progress_event(
                session_id=session_id,
                turn_id=turn_id,
                queue_id=queue_id,
                step_index=step_index,
                turn_workspace=turn_workspace,
                state=next_progress_state,
                trigger="after_tool_result",
            )
            next_current_phase = self._implementation_task_effective_phase(
                fallback_phase=next_current_phase,
                state=next_progress_state,
            )
            self._write_runtime_status(
                status="running",
                current_role=selection["role"],
                current_turn_id=turn_id,
                current_queue_id=queue_id,
                current_user_message=recent_user_message,
                current_prompt_preview=prompt[:2000],
                current_stream_text=(
                    f"Received result from {tool_name}\n{json.dumps(tool_result, ensure_ascii=False)}"
                )[-4000:],
                current_plan=planning_note,
                current_phase=next_current_phase,
                current_model=selection["model"],
                current_model_reason=selection["reason"],
                current_tool=tool_name,
                current_llm_workspace=str(turn_workspace),
                last_llm_workspace=str(turn_workspace),
                last_error=None,
                current_finished_at=now_iso(),
                worker_running=self._worker_running(),
            )
            if tool_name == "run_command" and not bool(tool_result.get("ok")):
                error_text = str(tool_result.get("error") or tool_result.get("stderr") or "")
                if "PowerShell requested but 'pwsh' is not installed" in error_text:
                    final_answer = "PowerShell is not available in this environment because 'pwsh' is not installed."
                    self._append_session_event(
                        self.root,
                        session_id,
                        {
                            "type": "finish",
                            "role": "assistant",
                            "content": final_answer,
                            "model": selection["model"],
                            "model_reason": selection["reason"],
                            "llm_attempt_count": telemetry["attempt_count"],
                            "turn_id": turn_id,
                            "queue_id": queue_id,
                            "step_index": step_index,
                        },
                    )
                    self._write_runtime_status(
                        status="idle",
                        current_role=selection["role"],
                        current_turn_id=None,
                        current_queue_id=None,
                        current_user_message=None,
                        current_prompt_preview=None,
                        current_stream_text=error_text,
                        current_plan=None,
                        current_phase="FINISH",
                        current_model=selection["model"],
                        current_model_reason=selection["reason"],
                        current_tool="finish",
                        current_operation_id=None,
                        current_llm_workspace=None,
                        last_llm_workspace=str(turn_workspace),
                        last_error=error_text,
                        last_system_note=error_text,
                        current_started_at=None,
                        current_finished_at=now_iso(),
                        worker_running=self._worker_running(),
                    )
                    self._record_reflection(
                        session_id=session_id,
                        turn_id=turn_id,
                        queue_id=queue_id,
                        user_message=recent_user_message,
                        reason="shell unavailable",
                        steps=steps,
                    )
                    finish_operation("failed", output_preview=error_text)
                    return {"ok": False, "session_id": session_id, "steps": steps, "final_answer": final_answer, "error": error_text}
        goal_text = str(read_json(self.paths.goal_path, fallback={}).get("text") or "")
        final_selection = selection_override or self.router.select_model(
            goal_text=goal_text,
            pending_message=recent_user_message,
            recent_events=self._recent_events_for_action_context(
                session_id=session_id,
                current_frame=self.frame_manager.current_frame(),
                limit=30,
            ),
            current_phase="STEP_LIMIT_FINAL_GATE",
        )
        active_frame = self.frame_manager.current_frame()
        plan_incomplete_at_step_limit = False
        if active_frame is not None and active_frame.parent_frame_id is not None:
            if self._child_frame_successful_tool_evidence() and not self._child_frame_has_unresolved_failure():
                self._force_return_after_child_contract_block(
                    session_id=session_id,
                    turn_id=turn_id,
                    queue_id=queue_id,
                    step_index=max_steps,
                    turn_workspace=turn_workspace,
                    blocked_tool="step_limit_final_gate",
                )
                active_frame = self.frame_manager.current_frame()
            if active_frame is not None and active_frame.parent_frame_id is not None:
                self._step_limit_active_frame_block_event(
                    session_id=session_id,
                    turn_id=turn_id,
                    queue_id=queue_id,
                    step_index=max_steps,
                    turn_workspace=turn_workspace,
                )
                plan_incomplete_at_step_limit = True
        pending_plan_task = self._pending_plan_child_task()
        if pending_plan_task is not None:
            self._plan_execution_pending_block_event(
                session_id=session_id,
                turn_id=turn_id,
                queue_id=queue_id,
                step_index=max_steps,
                turn_workspace=turn_workspace,
                blocked_tool="step_limit_final_gate",
                pending_task=pending_plan_task,
            )
            plan_incomplete_at_step_limit = True
        step_limit_finish = None
        if not plan_incomplete_at_step_limit:
            step_limit_finish = self._controller_terminal_finish(
                selection=final_selection,
                goal_text=goal_text,
                user_message=recent_user_message,
                steps=steps,
            )
        if step_limit_finish is not None and self._latest_successful_tool_name(steps) == "run_command":
            acceptance = self._finish_acceptance_evaluation(
                user_message=recent_user_message,
                final_answer=step_limit_finish,
                steps=steps,
            )
            self._append_session_event(
                self.root,
                session_id,
                {
                    "type": "system_note",
                    "role": "system",
                    "content": f"step limit final gate: 完了受理判定: {acceptance.get('status')}",
                    "code": "finish_acceptance",
                    "reason_code": self._finish_acceptance_reason_code(acceptance),
                    "details": acceptance,
                    "turn_id": turn_id,
                    "queue_id": queue_id,
                    "step_index": max_steps,
                    "llm_workspace": str(turn_workspace),
                },
            )
            if self._finish_status_is_accepted(acceptance.get("status")):
                self._append_session_event(
                    self.root,
                    session_id,
                    {
                        "type": "system_note",
                        "role": "system",
                        "content": "step limit 到達前の最新ツール結果が完了契約を満たしていたため、final として受理しました。",
                        "code": "step_limit_final_gate",
                        "reason_code": "contract_satisfied_at_step_limit",
                        "details": {"acceptance": acceptance},
                        "turn_id": turn_id,
                        "queue_id": queue_id,
                        "step_index": max_steps,
                        "llm_workspace": str(turn_workspace),
                    },
                )
                self._append_session_event(
                    self.root,
                    session_id,
                    {
                        "type": "finish",
                        "role": "assistant",
                        "content": step_limit_finish,
                        "model": final_selection["model"],
                        "model_reason": f"{final_selection['reason']} + step-limit-final-gate",
                        "llm_attempt_count": 0,
                        "turn_id": turn_id,
                        "queue_id": queue_id,
                        "step_index": max_steps,
                        "llm_workspace": str(turn_workspace),
                    },
                )
                self._write_runtime_status(
                    status="idle",
                    current_role=final_selection["role"],
                    current_turn_id=None,
                    current_queue_id=None,
                    current_user_message=None,
                    current_prompt_preview=None,
                    current_stream_text="",
                    current_plan=None,
                    current_phase="FINISH",
                    current_model=final_selection["model"],
                    current_model_reason=f"{final_selection['reason']} + step-limit-final-gate",
                    current_tool="finish",
                    current_operation_id=None,
                    current_llm_workspace=None,
                    last_llm_workspace=str(turn_workspace),
                    last_error=None,
                    last_system_note="step limit final gate accepted latest evidence",
                    current_started_at=None,
                    current_finished_at=now_iso(),
                    worker_running=self._worker_running(),
                )
                finish_operation("finished", output_preview=step_limit_finish)
                return {"ok": True, "session_id": session_id, "steps": steps, "final_answer": step_limit_finish, "acceptance": acceptance}
        self._append_session_event(
            self.root,
            session_id,
            {
                "type": "system_note",
                "role": "system",
                "content": "step limit reached before finish",
                "code": "step_limit_reached",
                "reason_code": "contract_incomplete_at_step_limit",
                "details": {
                    "contract_state": "incomplete",
                    "missing_requirements": self._step_limit_missing_requirements(user_message=recent_user_message, steps=steps),
                    "configured_max_steps": configured_max_steps,
                    "effective_max_steps": max_steps,
                    "successful_tools": [str(step.get("tool_name") or "") for step in steps if bool((step.get("tool_result") or {}).get("ok"))],
                },
                "turn_id": turn_id,
                "queue_id": queue_id,
                "step_index": max_steps,
                "llm_workspace": str(turn_workspace),
            },
        )
        self._record_reflection(
            session_id=session_id,
            turn_id=turn_id,
            queue_id=queue_id,
            user_message=recent_user_message,
            reason="step limit reached before finish",
            steps=steps,
        )
        self._write_runtime_status(
            status="idle",
            current_turn_id=None,
            current_queue_id=None,
            current_user_message=None,
            current_prompt_preview=None,
            current_stream_text="",
            current_plan=None,
            current_phase="FINISH",
            current_tool=None,
            current_operation_id=None,
            current_llm_workspace=None,
            last_llm_workspace=str(turn_workspace),
            last_error="step limit reached",
            last_system_note="step limit reached before finish",
            current_started_at=None,
            current_finished_at=now_iso(),
            worker_running=self._worker_running(),
        )
        finish_operation("failed", output_preview="step limit reached before finish")
        return {"ok": False, "session_id": session_id, "steps": steps, "error": "step limit reached"}

    def _make_tool_stream_updater(
        self,
        *,
        session_id: str,
        selection: dict[str, str],
        turn_id: str,
        queue_id: str,
        step_index: int,
        recent_user_message: str,
        prompt: str,
        tool_name: str,
        llm_workspace: str,
        current_phase: str,
    ) -> Any:
        def _update(partial: dict[str, Any]) -> None:
            preview = json.dumps(partial, ensure_ascii=False)[-4000:]
            self._append_runtime_event(
                session_id,
                event_name="tool_stream",
                content=preview,
                details={
                    "tool_name": tool_name,
                    "partial": partial,
                    "stream": partial.get("active_stream"),
                },
                turn_id=turn_id,
                queue_id=queue_id,
                step_index=step_index,
                llm_workspace=llm_workspace,
                phase=current_phase,
            )
            self._write_runtime_status(
                status="running_tool",
                current_role=selection["role"],
                current_turn_id=turn_id,
                current_queue_id=queue_id,
                current_user_message=recent_user_message,
                current_prompt_preview=prompt[:2000],
                current_stream_text=preview,
                current_model=selection["model"],
                current_model_reason=selection["reason"],
                current_tool=tool_name,
                worker_running=self._worker_running(),
            )
        return _update




































    def _record_reflection(
        self,
        *,
        session_id: str,
        turn_id: str,
        queue_id: str,
        user_message: str,
        reason: str,
        steps: list[dict[str, Any]],
    ) -> None:
        recent_tools = [
            str(step.get("tool_name") or "")
            for step in steps[-3:]
            if str(step.get("tool_name") or "")
        ]
        failure_class = self._classify_failure(reason=reason, steps=steps)
        reflection = (
            f"失敗パターン ({failure_class}): {reason}。 "
            f"ユーザーの依頼: {user_message[:120]}。 "
            f"直近のツール: {', '.join(recent_tools) or 'なし'}。 "
            "次ターンではスコープを絞り込み、事実（tool_result）の実績を重視し、安易な完了を避けるべきです。"
        )
        append_jsonl(
            self.paths.reflections_path,
            {
                "timestamp": now_iso(),
                "session_id": session_id,
                "turn_id": turn_id,
                "queue_id": queue_id,
                "reason": reason,
                "failure_class": failure_class,
                "reflection": reflection,
            },
        )
        self._append_session_event(
            self.root,
            session_id,
            {
                "type": "system_note",
                "role": "system",
                "content": f"reflection: {reflection}",
                "turn_id": turn_id,
                "queue_id": queue_id,
            },
        )
        self._write_runtime_status(last_reflection=reflection, worker_running=self._worker_running(), status=read_json(self.paths.runtime_status_path, fallback={}).get("status") or "idle")


    def _parse_envelope(self, raw_text: str) -> dict[str, Any]:
        raw_text = raw_text.strip()
        if not raw_text:
            return {"tool_name": "finish", "tool_args": {"final_answer": "No model output was produced."}}
        candidate = self._extract_json_object(raw_text)
        if candidate is None:
            return {"assistant_message": raw_text, "tool_name": "finish", "tool_args": {"final_answer": raw_text}}
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            return {"assistant_message": raw_text, "tool_name": "finish", "tool_args": {"final_answer": raw_text}}
        if not isinstance(payload, dict):
            return {"assistant_message": raw_text, "tool_name": "finish", "tool_args": {"final_answer": raw_text}}
        action = str(payload.get("action") or "").strip()
        tool_name = str(payload.get("tool_name") or action or "").strip()
        if tool_name == "final_answer":
            tool_args = payload.get("tool_args") if isinstance(payload.get("tool_args"), dict) else {}
            answer = str(tool_args.get("answer") or tool_args.get("final_answer") or payload.get("answer") or payload.get("assistant_message") or "")
            payload["tool_name"] = "finish"
            payload["tool_args"] = {"final_answer": answer}
            payload["assistant_message"] = str(payload.get("assistant_message") or answer)
        return payload








    def _worker_running(self) -> bool:
        if not self.paths.worker_pid_path.exists():
            return False
        try:
            pid = int(self.paths.worker_pid_path.read_text(encoding="utf-8").strip())
        except ValueError:
            try:
                self.paths.worker_pid_path.unlink()
            except OSError:
                pass
            return False
        try:
            os.kill(pid, 0)
        except OSError as exc:
            if getattr(exc, "errno", None) == errno.EPERM:
                return True
            try:
                self.paths.worker_pid_path.unlink()
            except OSError:
                pass
            return False
        return True

    def _process_is_alive(self, pid: Any) -> bool:
        try:
            value = int(pid)
        except (TypeError, ValueError):
            return False
        if value <= 0:
            return False
        try:
            os.kill(value, 0)
        except OSError as exc:
            if getattr(exc, "errno", None) == errno.EPERM:
                return True
            return False
        return True

    def _repair_stale_operator_interrupt(self) -> None:
        runtime_status = read_json(self.paths.runtime_status_path, fallback={})
        if runtime_status.get("status") != "running":
            return
        pid = runtime_status.get("current_process_pid")
        if not pid or self._process_is_alive(pid):
            return
        session_id = active_session_id(self.root)
        operation_id = str(runtime_status.get("current_operation_id") or "")
        existing_events = read_jsonl(self.paths.session_events_path(session_id), limit=50)
        already_recorded = any(
            row.get("code") == "operator_interrupt"
            and row.get("reason_code") == "interrupted_by_operator"
            and (not operation_id or row.get("operation_id") == operation_id)
            for row in existing_events
        )
        reason = f"runtime process pid {pid} is no longer alive"
        if not already_recorded:
            self._append_operator_interrupt_event(operator_reason=reason)
        self._write_runtime_status(
            status="interrupted_by_operator",
            current_phase="OPERATOR_INTERRUPT",
            current_finished_at=now_iso(),
            current_process_pid=None,
            worker_running=False,
            last_system_note=reason,
        )

    def _write_runtime_status(
        self,
        *,
        status: str,
        current_role: Any = _UNSET,
        current_turn_id: Any = _UNSET,
        current_queue_id: Any = _UNSET,
        current_user_message: Any = _UNSET,
        current_prompt_preview: Any = _UNSET,
        current_stream_text: Any = _UNSET,
        current_plan: Any = _UNSET,
        current_phase: Any = _UNSET,
        current_started_at: Any = _UNSET,
        current_finished_at: Any = _UNSET,
        current_model: Any = _UNSET,
        current_model_reason: Any = _UNSET,
        current_operation_id: Any = _UNSET,
        current_tool: Any = _UNSET,
        current_llm_workspace: Any = _UNSET,
        last_llm_workspace: Any = _UNSET,
        last_error: Any = _UNSET,
        last_system_note: Any = _UNSET,
        last_reflection: Any = _UNSET,
        last_llm_started_at: Any = _UNSET,
        last_llm_finished_at: Any = _UNSET,
        last_llm_duration_ms: Any = _UNSET,
        last_llm_attempt_count: Any = _UNSET,
        last_llm_raw_preview: Any = _UNSET,
        last_llm_thinking_preview: Any = _UNSET,
        last_llm_parse_issue: Any = _UNSET,
        last_llm_schema_validation: Any = _UNSET,
        raw_output_is_machine_json: Any = _UNSET,
        schema_validation_ok: Any = _UNSET,
        last_llm_stream_metadata: Any = _UNSET,
        current_process_pid: Any = _UNSET,
        worker_running: bool | None = None,
    ) -> None:
        current = read_json(self.paths.runtime_status_path, fallback={})
        payload = {
            **current,
            "status": status,
            "current_role": current.get("current_role") if current_role is _UNSET else current_role,
            "current_turn_id": current.get("current_turn_id") if current_turn_id is _UNSET else current_turn_id,
            "current_queue_id": current.get("current_queue_id") if current_queue_id is _UNSET else current_queue_id,
            "current_user_message": current.get("current_user_message") if current_user_message is _UNSET else current_user_message,
            "current_prompt_preview": current.get("current_prompt_preview") if current_prompt_preview is _UNSET else current_prompt_preview,
            "current_stream_text": current.get("current_stream_text") if current_stream_text is _UNSET else current_stream_text,
            "current_plan": current.get("current_plan") if current_plan is _UNSET else current_plan,
            "current_phase": current.get("current_phase") if current_phase is _UNSET else current_phase,
            "current_started_at": current.get("current_started_at") if current_started_at is _UNSET else current_started_at,
            "current_finished_at": current.get("current_finished_at") if current_finished_at is _UNSET else current_finished_at,
            "current_model": current.get("current_model") if current_model is _UNSET else current_model,
            "current_model_reason": current.get("current_model_reason") if current_model_reason is _UNSET else current_model_reason,
            "current_operation_id": current.get("current_operation_id") if current_operation_id is _UNSET else current_operation_id,
            "current_tool": current.get("current_tool") if current_tool is _UNSET else current_tool,
            "current_llm_workspace": current.get("current_llm_workspace") if current_llm_workspace is _UNSET else current_llm_workspace,
            "last_llm_workspace": current.get("last_llm_workspace") if last_llm_workspace is _UNSET else last_llm_workspace,
            "last_error": current.get("last_error") if last_error is _UNSET else last_error,
            "last_system_note": current.get("last_system_note") if last_system_note is _UNSET else last_system_note,
            "last_reflection": current.get("last_reflection") if last_reflection is _UNSET else last_reflection,
            "last_llm_started_at": current.get("last_llm_started_at") if last_llm_started_at is _UNSET else last_llm_started_at,
            "last_llm_finished_at": current.get("last_llm_finished_at") if last_llm_finished_at is _UNSET else last_llm_finished_at,
            "last_llm_duration_ms": current.get("last_llm_duration_ms") if last_llm_duration_ms is _UNSET else last_llm_duration_ms,
            "last_llm_attempt_count": current.get("last_llm_attempt_count", 0) if last_llm_attempt_count is _UNSET else last_llm_attempt_count,
            "last_llm_raw_preview": current.get("last_llm_raw_preview") if last_llm_raw_preview is _UNSET else last_llm_raw_preview,
            "last_llm_thinking_preview": current.get("last_llm_thinking_preview") if last_llm_thinking_preview is _UNSET else last_llm_thinking_preview,
            "last_llm_parse_issue": current.get("last_llm_parse_issue") if last_llm_parse_issue is _UNSET else last_llm_parse_issue,
            "last_llm_schema_validation": current.get("last_llm_schema_validation") if last_llm_schema_validation is _UNSET else last_llm_schema_validation,
            "raw_output_is_machine_json": current.get("raw_output_is_machine_json") if raw_output_is_machine_json is _UNSET else raw_output_is_machine_json,
            "schema_validation_ok": current.get("schema_validation_ok") if schema_validation_ok is _UNSET else schema_validation_ok,
            "last_llm_stream_metadata": current.get("last_llm_stream_metadata") if last_llm_stream_metadata is _UNSET else last_llm_stream_metadata,
            "current_process_pid": current.get("current_process_pid") if current_process_pid is _UNSET else current_process_pid,
            "last_event_at": now_iso(),
            "worker_running": current.get("worker_running") if worker_running is None else worker_running,
        }
        write_json(self.paths.runtime_status_path, payload)

# Helper methods are kept as AgentRuntime attributes so existing tests and
# monkeypatch extension points remain source-compatible after the split.
AgentRuntime._observer_enabled = _observer_enabled
AgentRuntime._maybe_record_observer_note = _maybe_record_observer_note
AgentRuntime._usable_japanese_observer_text = _usable_japanese_observer_text
AgentRuntime._deterministic_step_commentary = _deterministic_step_commentary
AgentRuntime._context_risk_summary = _context_risk_summary
AgentRuntime._record_observer_judgement_note = _record_observer_judgement_note
AgentRuntime._record_observer_llm_output_issue_note = _record_observer_llm_output_issue_note
AgentRuntime._grounding_issues = _grounding_issues
AgentRuntime._finish_acceptance_evaluation = _finish_acceptance_evaluation
AgentRuntime._semantic_grounding_check = _semantic_grounding_check
AgentRuntime._semantic_finish_acceptance_review = _semantic_finish_acceptance_review
AgentRuntime._parse_grounding_judge_payload = _parse_grounding_judge_payload
AgentRuntime.run_terminal_agent = run_terminal_agent
AgentRuntime._resolve_terminal_model = _resolve_terminal_model
AgentRuntime._preferred_shell_from_extra_prompt = _preferred_shell_from_extra_prompt
AgentRuntime._controller_terminal_finish = _controller_terminal_finish
AgentRuntime._terminal_answer_is_direct_evidence = _terminal_answer_is_direct_evidence
AgentRuntime._deterministic_terminal_final_answer = _deterministic_terminal_final_answer
AgentRuntime._empty_stdout_command_can_answer_request = _empty_stdout_command_can_answer_request
AgentRuntime._command_stdout_can_answer_request = _command_stdout_can_answer_request
AgentRuntime._synthesize_terminal_final_answer = _synthesize_terminal_final_answer
AgentRuntime._normalize_run_command_evidence = _normalize_run_command_evidence
AgentRuntime._output_preview = _output_preview
AgentRuntime._stdout_result_block = _stdout_result_block
AgentRuntime._current_phase = _current_phase
AgentRuntime._deliberation_reasons = _deliberation_reasons
AgentRuntime._system_prompt = _system_prompt
AgentRuntime._output_budget_prompt = _output_budget_prompt
AgentRuntime._build_prompt = _build_prompt
AgentRuntime._render_action_context_events = _render_action_context_events
AgentRuntime._compact_context_text = _compact_context_text
AgentRuntime._render_tool_result_context = _render_tool_result_context
AgentRuntime._file_context_preview = _file_context_preview
AgentRuntime._judge_feedback_context_text = _judge_feedback_context_text
AgentRuntime._build_planning_note = _build_planning_note
AgentRuntime._build_deliberation_note = _build_deliberation_note
AgentRuntime._reflection_prompt_block = _reflection_prompt_block
AgentRuntime._reflection_relevant_to_user = _reflection_relevant_to_user
AgentRuntime._chat_with_repair = _chat_with_repair
AgentRuntime._extract_stream_metadata = _extract_stream_metadata
AgentRuntime._machine_control_stream_stop_reason = _machine_control_stream_stop_reason
AgentRuntime._looks_like_in_progress_write_file_content_stream = _looks_like_in_progress_write_file_content_stream
AgentRuntime._looks_like_plan_record_embedded_edit_stream = _looks_like_plan_record_embedded_edit_stream
AgentRuntime._looks_like_repetitive_machine_control_output = _looks_like_repetitive_machine_control_output
AgentRuntime._format_llm_stream_text = _format_llm_stream_text
AgentRuntime._tail_stream_text = _tail_stream_text
AgentRuntime._json_repair_prompt = _json_repair_prompt
AgentRuntime._thinking_only_repair_prompt = _thinking_only_repair_prompt
AgentRuntime._parse_issue_should_exit_repair_loop = _parse_issue_should_exit_repair_loop
AgentRuntime._classify_llm_parse_issue = _classify_llm_parse_issue
AgentRuntime._looks_like_truncated_json = _looks_like_truncated_json
AgentRuntime._looks_like_structured_envelope = _looks_like_structured_envelope
AgentRuntime._raw_is_exact_json_object = _raw_is_exact_json_object
AgentRuntime._raw_contains_json_object = _raw_contains_json_object
AgentRuntime._extract_json_object = _extract_json_object
AgentRuntime._missing_requested_commands = _missing_requested_commands
AgentRuntime._expected_artifacts = _expected_artifacts
AgentRuntime._missing_expected_artifacts = _missing_expected_artifacts
AgentRuntime._failed_command_guardrail = _failed_command_guardrail
AgentRuntime._redundant_command_reason = _redundant_command_reason
AgentRuntime._similar_command_warning = _similar_command_warning
AgentRuntime._extract_requested_commands = _extract_requested_commands
AgentRuntime._classify_failure = _classify_failure
AgentRuntime._is_runtime_identity_query = staticmethod(is_runtime_identity_query)


def stop_worker(root: Path) -> dict[str, Any]:
    paths = WorkspacePaths(root)
    if not paths.worker_pid_path.exists():
        write_json(paths.runtime_status_path, {**read_json(paths.runtime_status_path, fallback={}), "worker_running": False})
        return {"ok": True, "stopped": False}
    try:
        pid = int(paths.worker_pid_path.read_text(encoding="utf-8").strip())
    except ValueError:
        paths.worker_pid_path.unlink(missing_ok=True)
        return {"ok": True, "stopped": False}
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
    return {"ok": True, "stopped": True, "pid": pid}


def worker_command() -> list[str]:
    return [sys.executable, "-u", "-m", "p4_core.cli", "worker"]
