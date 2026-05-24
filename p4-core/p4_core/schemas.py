from __future__ import annotations

from typing import Any


SCHEMA_VERSION = "p4-json-schema-v1"

NON_DECOMPOSE_ACTION_TOOLS = (
    "list_files",
    "read_file",
    "search_code",
    "write_file",
    "append_file",
    "replace_text",
    "run_command",
)

PLAN_FIRST_ACTION_TOOLS = (
    "list_files",
    "read_file",
    "search_code",
    "run_command",
)

TOOL_ACTION_NAMES = (
    *NON_DECOMPOSE_ACTION_TOOLS,
    "create_plan",
    "decompose_tasks",
    "open_child_frame",
    "return_to_parent",
    "finish",
    "final_answer",
)

WORK_TYPES = ("inspect", "edit", "run_test", "search")
FIRST_ACTION_CONTENT_MAX_LENGTH = 1200


def _string_schema(max_length: int) -> dict[str, Any]:
    return {"type": "string", "maxLength": max_length}


FIRST_ACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["tool", "args"],
    "additionalProperties": False,
    "properties": {
        "tool": {"type": "string", "enum": list(NON_DECOMPOSE_ACTION_TOOLS)},
        "args": {
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "path": _string_schema(500),
                "command": _string_schema(800),
                "content": _string_schema(FIRST_ACTION_CONTENT_MAX_LENGTH),
                "old_text": _string_schema(FIRST_ACTION_CONTENT_MAX_LENGTH),
                "new_text": _string_schema(FIRST_ACTION_CONTENT_MAX_LENGTH),
            },
        },
    },
}

PLAN_FIRST_ACTION_SCHEMA: dict[str, Any] = {
    **FIRST_ACTION_SCHEMA,
    "properties": {
        **FIRST_ACTION_SCHEMA["properties"],
        "tool": {"type": "string", "enum": list(PLAN_FIRST_ACTION_TOOLS)},
    },
}

WORK_PACKAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["goal", "work_type", "first_action", "success_evidence", "why_not_direct_action"],
    "additionalProperties": True,
    "properties": {
        "goal": _string_schema(800),
        "work_type": {"type": "string", "enum": list(WORK_TYPES)},
        "first_action": FIRST_ACTION_SCHEMA,
        "success_evidence": _string_schema(800),
        "why_not_direct_action": _string_schema(800),
        "context_summary": _string_schema(1600),
        "done_when": _string_schema(800),
        "task_id": _string_schema(120),
        "child_task_id": _string_schema(120),
        "status": _string_schema(120),
    },
}

TOOL_ACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["tool_name", "tool_args"],
    "additionalProperties": False,
    "properties": {
        "analysis": _string_schema(1200),
        "assistant_message": _string_schema(1600),
        "tool_name": {"type": "string", "enum": list(TOOL_ACTION_NAMES)},
        "tool_args": {"type": "object", "additionalProperties": True},
    },
}


def tool_action_schema(
    *,
    include_frame_operations: bool = True,
    allowed_tool_names: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Return the machine-control schema for the currently visible action set."""

    if include_frame_operations:
        visible_names = TOOL_ACTION_NAMES
    else:
        visible_names = (*NON_DECOMPOSE_ACTION_TOOLS, "finish", "final_answer")
    if allowed_tool_names:
        allowed = [name for name in allowed_tool_names if name in visible_names]
        if allowed:
            visible_names = tuple(dict.fromkeys(allowed))
    properties = dict(TOOL_ACTION_SCHEMA["properties"])
    properties["tool_name"] = {"type": "string", "enum": list(visible_names)}
    if tuple(visible_names) == ("create_plan",):
        properties["tool_args"] = {
            "type": "object",
            "required": ["plan"],
            "additionalProperties": False,
            "properties": {
                "plan": PLAN_RECORD_SCHEMA,
            },
        }
    return {**TOOL_ACTION_SCHEMA, "properties": properties}

# --- Judge schemas: verdict-first design ---
#
# 設計意図:
#   judge の完了制御の正本は verdict (ok/ng) / status (success/...) のみ。
#   reason_code, rationale, unsupported_claims, observed_mismatch は説明用 annotation で
#   あり、決定権を持たない (p4-coding-invariants Invariant 5 — 正本を増やさない)。
#
#   旧 schema は annotation を required + enum 拘束していたため、annotation 側の
#   文字列ミスマッチ (例: LLM が "supported_claim" を返す) が verdict の決定を
#   逆流的に棄却してしまい、judge の決定の正本性が破られていた。
#
#   詳細: handoff/p4-judge-verdict-first-2026-05-03.md
#
# 不変条件:
#   - verdict / status の enum 不一致は **decision の正本違反** であり棄却対象。
#   - reason_code 等 annotation の表現揺れは **decision を棄却しない**。
JUDGE_VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["verdict"],
    "additionalProperties": False,
    "properties": {
        "verdict": {"type": "string", "enum": ["ok", "ng"]},
        "reason_code": _string_schema(200),
        "unsupported_claims": {
            "type": "array",
            "maxItems": 8,
            "items": _string_schema(500),
        },
        "rationale": _string_schema(800),
    },
}

FINISH_ACCEPTANCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["status", "reason_code", "rationale", "observed_mismatch"],
    "additionalProperties": False,
    "properties": {
        "status": {"type": "string", "enum": ["success", "partial_success", "needs_revision"]},
        "reason_code": _string_schema(200),
        "rationale": _string_schema(800),
        "observed_mismatch": _string_schema(800),
    },
}

PLAN_ACCEPTANCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["verdict", "reason_code", "rationale"],
    "additionalProperties": False,
    "properties": {
        "verdict": {"type": "string", "enum": ["accept", "reject"]},
        "reason_code": _string_schema(200),
        "rationale": _string_schema(1000),
        "suggested_next_action": _string_schema(800),
    },
}

PLAN_RECORD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["plan_id", "profile", "strategy", "work_units", "verification_contract", "status", "revision_count"],
    "additionalProperties": True,
    "properties": {
        "plan_id": _string_schema(120),
        "profile": {
            "type": "object",
            "required": ["complexity", "signals", "strategy", "state_model", "constraints", "verification_requirements"],
            "additionalProperties": True,
            "properties": {
                "complexity": _string_schema(80),
                "signals": {"type": "array", "maxItems": 32, "items": _string_schema(160)},
                "strategy": {"type": "string", "enum": [
                    "direct_implementation",
                    "task_decomposition",
                    "state_space_search",
                    "dynamic_programming",
                    "constraint_satisfaction",
                    "graph_shortest_path",
                    "classical_planning",
                ]},
                "state_model": _string_schema(800),
                "constraints": {"type": "array", "maxItems": 24, "items": _string_schema(300)},
                "verification_requirements": {"type": "array", "maxItems": 24, "items": _string_schema(300)},
            },
        },
        "strategy": {
            "type": "string",
            "enum": [
                "direct_implementation",
                "task_decomposition",
                "state_space_search",
                "dynamic_programming",
                "constraint_satisfaction",
                "graph_shortest_path",
                "classical_planning",
            ],
        },
        "work_units": {
            "type": "array",
            "minItems": 1,
            "maxItems": 12,
            "items": {
                "type": "object",
                "required": ["unit_id", "goal", "depends_on", "work_type", "first_action", "success_evidence", "should_open_child_frame"],
                "additionalProperties": True,
                "properties": {
                    "unit_id": _string_schema(120),
                    "goal": _string_schema(800),
                    "depends_on": {"type": "array", "maxItems": 12, "items": _string_schema(120)},
                    "work_type": {"type": "string", "enum": list(WORK_TYPES)},
                    "first_action": PLAN_FIRST_ACTION_SCHEMA,
                    "success_evidence": _string_schema(800),
                    "should_open_child_frame": {"type": "boolean"},
                    "why_not_direct_action": _string_schema(800),
                    "context_summary": _string_schema(1600),
                    "done_when": _string_schema(800),
                },
            },
        },
        "verification_contract": {
            "oneOf": [
                {"type": "array", "maxItems": 32, "items": _string_schema(200)},
                {"type": "object", "additionalProperties": True},
            ]
        },
        "status": {"type": "string", "enum": ["proposed", "accepted", "revision_required"]},
        "revision_count": {"type": "integer", "minimum": 0, "maximum": 99},
    },
}
