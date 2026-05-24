from __future__ import annotations
import json
import uuid
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from p4_core.frames import FrameManager
from p4_core.grounding import _finish_acceptance_contract
from p4_core.ollama_client import OllamaChatClient
from p4_core.workspace import WorkspacePaths, active_session_id, read_json, read_jsonl, now_iso

_BENCHMARK_CASE_FALLBACKS: dict[str, dict[str, str]] = {
    "terminal_pwd_short": {"label": "単発パス確認", "phase": "basic-grounding"},
    "terminal_pwd_ls_summary": {"label": "複数コマンド要約", "phase": "multi-step-grounding"},
    "terminal_find_head_agents": {"label": "探索とプレビュー要約", "phase": "file-evidence-synthesis"},
    "terminal_git_status_then_pwd": {"label": "git 文脈確認", "phase": "multi-step-grounding"},
}
_BENCHMARK_PHASE_ORDER: dict[str, int] = {
    "basic-grounding": 0,
    "multi-step-grounding": 1,
    "file-evidence-synthesis": 2,
}

def _benchmark_phase_rank(phase: str) -> int:
    return _BENCHMARK_PHASE_ORDER.get(str(phase or ""), 99)

def _reasoning_model(root: Path) -> str:
    config = read_json(WorkspacePaths(root).config_path, fallback={})
    models = config.get("models", {}) if isinstance(config, dict) else {}
    return str(models.get("reasoning") or "gemma4:26b")

def _configured_model_names(root: Path) -> list[str]:
    config = read_json(WorkspacePaths(root).config_path, fallback={})
    configured = config.get("models", {}) if isinstance(config, dict) else {}
    models = [str(value).strip() for value in configured.values() if str(value or "").strip()]
    return list(dict.fromkeys(models))

def _ollama_model_names(root: Path, *, timeout_seconds: float = 0.5) -> list[str]:
    config = read_json(WorkspacePaths(root).config_path, fallback={})
    base_url = str(config.get("ollama_base_url") or "http://127.0.0.1:11434") if isinstance(config, dict) else "http://127.0.0.1:11434"
    payload = OllamaChatClient(base_url=base_url).list_models(timeout_seconds=timeout_seconds)
    models = payload.get("models") if isinstance(payload, dict) else []
    names = [
        str(item.get("name") or "").strip()
        for item in models
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    ]
    return list(dict.fromkeys(names))

def _available_models(root: Path) -> list[str]:
    try:
        observed = _ollama_model_names(root)
    except Exception:
        observed = []
    if observed:
        return observed
    return _configured_model_names(root) or [_reasoning_model(root)]

def _duration_ms(started_at: str, finished_at: str) -> int | None:
    try:
        start = datetime.fromisoformat(started_at)
        finish = datetime.fromisoformat(finished_at)
    except ValueError:
        return None
    return max(0, int((finish - start).total_seconds() * 1000))

def _parse_timestamp(value: str | None) -> datetime | None:
    if not value: return None
    try: return datetime.fromisoformat(value)
    except ValueError: return None

def _runtime_activity_age_seconds(runtime: dict[str, Any]) -> float:
    runtime_last_at = _parse_timestamp(str(runtime.get("last_event_at") or ""))
    now_dt = _parse_timestamp(now_iso())
    if runtime_last_at is None or now_dt is None:
        return 9999.0
    return (now_dt - runtime_last_at).total_seconds()

def _runtime_live_is_fresh(runtime: dict[str, Any]) -> bool:
    return bool(runtime.get("worker_running", False)) or _runtime_activity_age_seconds(runtime) < 120

def _operation_rows(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered_ids: list[str] = []
    operations: dict[str, dict[str, Any]] = {}
    current_scan_op_id = None

    for row in events:
        event_type = row.get("type")
        if event_type == "operation":
            operation_id = str(row.get("operation_id") or row.get("event_id") or uuid.uuid4().hex)
            if operation_id not in operations:
                ordered_ids.append(operation_id)
                operations[operation_id] = {
                    "operation_id": operation_id,
                    "title": "",
                    "detail": "",
                    "status": "running",
                    "started_at": None,
                    "finished_at": None,
                    "duration_ms": None,
                    "output_preview": "",
                    "trace_preview": "",
                    "flow_preview": "",
                    "flow_steps": [],
                    "timestamp": row.get("timestamp"),
                    "last_event_at": row.get("timestamp"),
                    "max_step_index": 0,
                }
            current = operations[operation_id]
            for key in ("title", "detail", "status", "started_at", "finished_at", "duration_ms", "output_preview", "trace_preview", "flow_preview", "flow_steps"):
                value = row.get(key)
                if value is not None and value != "":
                    current[key] = value
            current["timestamp"] = row.get("timestamp") or current.get("timestamp")
            current["last_event_at"] = row.get("timestamp") or current.get("last_event_at")
            if current.get("status") in {"started", "running"}:
                current_scan_op_id = operation_id
            else:
                current_scan_op_id = None
        elif current_scan_op_id:
            current = operations[current_scan_op_id]
            current["last_event_at"] = row.get("timestamp") or current.get("last_event_at")
            if row.get("turn_id") and not current.get("turn_id"):
                current["turn_id"] = str(row.get("turn_id") or "")
            if row.get("queue_id") and not current.get("queue_id"):
                current["queue_id"] = str(row.get("queue_id") or "")
            step_idx = row.get("step_index")
            if step_idx is not None:
                current["max_step_index"] = max(current.get("max_step_index", 0), int(step_idx))

    rows = [operations[op_id] for op_id in ordered_ids][-20:]
    rows.reverse()
    return rows

def _normalize_operation_rows(runtime: dict[str, Any], operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    runtime_status = str(runtime.get("status") or "")
    worker_running = bool(runtime.get("worker_running", False))
    now = now_iso()
    chrono = list(reversed(operations))
    for i, current in enumerate(chrono):
        if str(current.get("status") or "") not in {"started", "running"}:
            continue
        is_active_in_runtime = False
        current_op_id_in_runtime = str(runtime.get("current_operation_id") or "")
        if runtime_status.startswith("running"):
            current_turn_id = str(runtime.get("current_turn_id") or "")
            current_started_at = str(runtime.get("current_started_at") or "")
            if current_op_id_in_runtime and str(current.get("operation_id") or "") == current_op_id_in_runtime:
                is_active_in_runtime = True
            elif current_turn_id and str(current.get("turn_id") or "") == current_turn_id:
                is_active_in_runtime = True
            elif current_started_at and str(current.get("started_at") or "") == current_started_at:
                is_active_in_runtime = True
        started_at_str = str(current.get("started_at") or "")
        last_event_at_str = str(current.get("last_event_at") or "")
        age_seconds = 9999
        if started_at_str:
            try:
                started_dt = _parse_timestamp(started_at_str)
                now_dt = _parse_timestamp(now)
                age_seconds = (now_dt - started_dt).total_seconds()
            except Exception: pass
        activity_age_seconds = 9999
        if last_event_at_str:
            try:
                last_dt = _parse_timestamp(last_event_at_str)
                now_dt = _parse_timestamp(now)
                activity_age_seconds = (now_dt - last_dt).total_seconds()
            except Exception: pass
        if is_active_in_runtime and runtime.get("last_event_at"):
            try:
                runtime_last_dt = _parse_timestamp(str(runtime.get("last_event_at") or ""))
                now_dt = _parse_timestamp(now)
                activity_age_seconds = (now_dt - runtime_last_dt).total_seconds()
            except Exception:
                pass
        if is_active_in_runtime and (worker_running or activity_age_seconds < 120):
            continue
        if age_seconds < 60: continue
        if activity_age_seconds < 120 and worker_running: continue
        if current_op_id_in_runtime and current_op_id_in_runtime != str(current.get("operation_id")): pass
        elif worker_running and activity_age_seconds < 300: continue
        current["status"] = "failed"
        if is_active_in_runtime and runtime.get("current_stream_text") and not current.get("output_preview"):
            current["output_preview"] = str(runtime.get("current_stream_text") or "")
        next_op_start = None
        if i + 1 < len(chrono):
            next_op_start = chrono[i+1].get("started_at")
        runtime_hint = str(runtime.get("last_event_at") or "")
        if not current.get("finished_at"):
            current["finished_at"] = next_op_start or runtime_hint or now
        if not current.get("duration_ms") and current.get("started_at") and current.get("finished_at"):
            current["duration_ms"] = _duration_ms(str(current.get("started_at") or ""), str(current.get("finished_at") or ""))
        detail = str(current.get("detail") or "")
        if "normalized from stale" not in detail:
            if is_active_in_runtime:
                reason = "runtime current operation has no active worker and no recent activity"
            elif runtime_status.startswith("running"):
                reason = "another run is active"
            else:
                reason = "runtime is idle and last activity was too long ago"
            current["detail"] = (detail + f"\n\n[normalized from stale running operation because {reason}]").strip()
    return list(reversed(chrono))

def _trace_preview_for_operation(operation: dict[str, Any], events: list[dict[str, Any]]) -> str:
    started_at = _parse_timestamp(str(operation.get("started_at") or ""))
    finished_at = _parse_timestamp(str(operation.get("finished_at") or ""))
    if started_at is None: return ""
    trace_lines: list[str] = []
    for row in events:
        row_time = _parse_timestamp(str(row.get("timestamp") or ""))
        if row_time is None or row_time < started_at: continue
        if finished_at is not None and row_time > finished_at + timedelta(seconds=2): continue
        row_type = str(row.get("type") or "")
        if row_type not in {"user_message", "assistant_message", "tool_call", "tool_result", "finish", "system_note", "planning_note", "task_plan", "problem_profile", "planner_decision", "plan_record", "plan_revision", "observer_note", "activity_update", "runtime_event", "frame_opened", "frame_returned", "child_return"}: continue
        if row_type == "user_message":
            trace_lines.append(f"[user_message] {row.get('content') or ''}")
        elif row_type == "assistant_message":
            trace_lines.append(f"[assistant_message] {row.get('content') or ''}")
        elif row_type == "activity_update":
            trace_lines.append(f"[activity_update] {row.get('content') or ''}")
        elif row_type == "tool_call":
            trace_lines.append(f"[tool_call] {row.get('tool_name') or ''} args={json.dumps(row.get('tool_args') or {}, ensure_ascii=False)}")
        elif row_type == "tool_result":
            trace_lines.append(f"[tool_result] {row.get('tool_name') or ''} -> {row.get('content') or ''}")
        elif row_type == "runtime_event":
            trace_lines.append(f"[runtime_event:{row.get('event_name') or ''}] {row.get('content') or ''}")
        elif row_type == "task_plan":
            trace_lines.append(f"[task_plan] {row.get('content') or ''} tasks={json.dumps(row.get('tasks') or [], ensure_ascii=False)}")
        elif row_type in {"problem_profile", "planner_decision", "plan_record", "plan_revision"}:
            trace_lines.append(f"[{row_type}] {row.get('content') or ''} details={json.dumps(row.get('plan') or row.get('profile') or row.get('details') or {}, ensure_ascii=False)}")
        elif row_type == "finish":
            trace_lines.append(f"[finish] {row.get('content') or ''}")
    return "\n\n".join(trace_lines)[-16000:]

def _event_in_operation_window(row: dict[str, Any], operation: dict[str, Any]) -> bool:
    op_id = str(operation.get("operation_id") or "")
    row_op_id = str(row.get("operation_id") or "")
    if op_id and row_op_id:
        return op_id == row_op_id
    op_turn_id = str(operation.get("turn_id") or "")
    row_turn_id = str(row.get("turn_id") or "")
    if op_turn_id and row_turn_id and op_turn_id == row_turn_id: return True
    row_time = _parse_timestamp(str(row.get("timestamp") or ""))
    started_at = _parse_timestamp(str(operation.get("started_at") or ""))
    finished_at = _parse_timestamp(str(operation.get("finished_at") or ""))
    if row_time is None or started_at is None or row_time < started_at: return False
    if finished_at is not None and row_time > finished_at + timedelta(seconds=2): return False
    return True

def _flow_steps_for_operation(operation: dict[str, Any], events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Import locally to avoid circular dependency
    from p4_core.dashboard.templates import _phase_for_flow_step
    grouped: dict[int, dict[str, Any]] = {}
    order: list[int] = []
    frame_depth_by_id: dict[str, int] = {}
    frame_stack: list[str] = []
    for row in events:
        row_type = str(row.get("type") or "")
        if row_type not in {"user_message", "assistant_message", "tool_call", "tool_result", "finish", "system_note", "planning_note", "task_plan", "problem_profile", "planner_decision", "plan_record", "plan_revision", "observer_note", "activity_update", "runtime_event", "frame_opened", "frame_returned", "child_return"}: continue
        if not _event_in_operation_window(row, operation): continue
        if row_type == "runtime_event" and str(row.get("event_name") or "") in {"llm_stream_chunk", "tool_stream"}:
            continue
        item_frame_id = frame_stack[-1] if frame_stack else ""
        item_frame_depth = len(frame_stack)
        step_index = int(row.get("step_index") or 0)
        if step_index not in grouped:
            order.append(step_index)
            grouped[step_index] = {
                "step_index": step_index,
                "title": "Input" if step_index == 0 else f"Step {step_index}",
                "items": [],
            }
        if row_type == "tool_call":
            item = {
                "label": row_type,
                "tool_name": str(row.get("tool_name") or ""),
                "content": f"{row.get('tool_name') or ''} args={json.dumps(row.get('tool_args') or {}, ensure_ascii=False)}",
                "tool_args": row.get("tool_args") or {},
            }
        elif row_type == "tool_result":
            parsed_payload: dict[str, Any] | None = None
            try:
                loaded = json.loads(str(row.get("content") or ""))
                if isinstance(loaded, dict): parsed_payload = loaded
            except json.JSONDecodeError: pass
            item = {
                "label": row_type,
                "tool_name": str(row.get("tool_name") or ""),
                "content": str(row.get("content") or ""),
                "parsed_payload": parsed_payload,
            }
        elif row_type in {"system_note", "planning_note", "observer_note", "activity_update"}:
            item = {
                "label": row_type,
                "content": str(row.get("content") or ""),
                "code": str(row.get("code") or ""),
                "reason_code": str(row.get("reason_code") or ""),
                "details": row.get("details") or {},
            }
        elif row_type == "task_plan":
            item = {
                "label": row_type,
                "content": str(row.get("content") or ""),
                "rationale": str(row.get("rationale") or ""),
                "tasks": row.get("tasks") or [],
                "frame_id": str(row.get("frame_id") or ""),
            }
        elif row_type in {"problem_profile", "planner_decision", "plan_record", "plan_revision"}:
            item = {
                "label": row_type,
                "content": str(row.get("content") or ""),
                "profile": row.get("profile") or {},
                "strategy": str(row.get("strategy") or ""),
                "plan": row.get("plan") or {},
                "work_units": row.get("work_units") or [],
                "verification_contract": row.get("verification_contract") or {},
                "details": row.get("details") or {},
            }
        elif row_type == "runtime_event":
            item = {
                "label": row_type,
                "content": str(row.get("content") or ""),
                "event_name": str(row.get("event_name") or ""),
                "details": row.get("details") or {},
            }
        elif row_type in {"frame_opened", "frame_returned", "child_return"}:
            item = {
                "label": row_type,
                "content": str(row.get("content") or ""),
                "frame_id": str(row.get("frame_id") or ""),
                "parent_frame_id": str(row.get("parent_frame_id") or ""),
                "child_frame_id": str(row.get("child_frame_id") or ""),
                "goal": str(row.get("goal") or ""),
                "return_payload": row.get("return_payload") or {},
            }
        else:
            item = {"label": row_type, "content": str(row.get("content") or "")}
        if row_type == "frame_opened":
            parent_id = str(row.get("parent_frame_id") or "")
            frame_id = str(row.get("frame_id") or "")
            parent_depth = frame_depth_by_id.get(parent_id, 0) if parent_id else 0
            item_frame_id = frame_id
            item_frame_depth = parent_depth + 1
            if frame_id:
                frame_depth_by_id[frame_id] = item_frame_depth
                frame_stack.append(frame_id)
        elif row_type == "frame_returned":
            returned_id = str(row.get("frame_id") or "")
            item_frame_id = returned_id or item_frame_id
            item_frame_depth = frame_depth_by_id.get(returned_id, item_frame_depth)
        elif row_type == "child_return":
            returned_id = str(row.get("child_frame_id") or "")
            item_frame_id = returned_id or item_frame_id
            item_frame_depth = frame_depth_by_id.get(returned_id, item_frame_depth)
            if returned_id:
                while frame_stack and frame_stack[-1] != returned_id:
                    frame_stack.pop()
                if frame_stack and frame_stack[-1] == returned_id:
                    frame_stack.pop()
        item["frame_id_for_display"] = item_frame_id
        item["frame_depth"] = item_frame_depth
        if row_type == "observer_note":
            grouped[step_index]["items"] = [
                existing
                for existing in grouped[step_index]["items"]
                if str(existing.get("label") or "") != "observer_note"
            ]
        grouped[step_index]["items"].append(item)
    rows = [grouped[index] for index in order]
    for row in rows:
        row["phase"] = _phase_for_flow_step(row)
    return rows


def _append_live_stream_step(operation: dict[str, Any], runtime: dict[str, Any]) -> None:
    if str(operation.get("status") or "") != "running":
        return
    live_text = str(operation.get("output_preview") or runtime.get("current_stream_text") or "")
    if not live_text:
        return
    steps = operation.setdefault("flow_steps", [])
    if not isinstance(steps, list):
        return
    last_step_index = 0
    for step in steps:
        try:
            last_step_index = max(last_step_index, int(step.get("step_index") or 0))
        except Exception:
            pass
    live_step = {
        "step_index": last_step_index + 1,
        "title": "Live",
        "phase": "LLM_STREAMING",
        "items": [
            {
                "label": "live_stream",
                "content": live_text,
                "code": "llm_live_stream",
                "frame_depth": 0,
                "frame_id_for_display": "",
            }
        ],
    }
    steps.append(live_step)

def _latest_blocked_reason(flow_steps: list[dict[str, Any]]) -> str | None:
    latest_block: str | None = None
    latest_finish_seen = False
    for step in flow_steps:
        for item in step.get("items") or []:
            label = str(item.get("label") or "")
            content = str(item.get("content") or "")
            code = str(item.get("code") or "")
            if label == "finish":
                latest_finish_seen = True
            elif label == "system_note" and (code == "finish_blocked" or "完了がブロックされました" in content):
                latest_block = content
                latest_finish_seen = False
    if latest_block and not latest_finish_seen:
        return latest_block
    return None

def _commentator_notes(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    notes: list[dict[str, Any]] = []
    for row in reversed(events):
        if str(row.get("type") or "") != "observer_note":
            continue
        notes.append(
            {
                "timestamp": str(row.get("timestamp") or ""),
                "content": str(row.get("content") or ""),
                "step_index": row.get("step_index"),
                "code": str(row.get("code") or ""),
                "reason_code": str(row.get("reason_code") or ""),
                "model": str(row.get("model") or ""),
            }
        )
        if len(notes) >= 5:
            break
    return notes


def _canonical_operation_rows(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered_ids: list[str] = []
    operations: dict[str, dict[str, Any]] = {}
    for row in events:
        operation_id = str(row.get("operation_id") or "")
        if not operation_id:
            continue
        kind = str(row.get("kind") or "")
        if kind == "operation" and operation_id not in operations:
            ordered_ids.append(operation_id)
            operations[operation_id] = {
                "operation_id": operation_id,
                "title": "",
                "detail": "",
                "status": "running",
                "started_at": None,
                "finished_at": None,
                "duration_ms": None,
                "output_preview": "",
                "timestamp": row.get("timestamp"),
                "last_event_at": row.get("timestamp"),
                "flow_steps": [],
            }
        if operation_id not in operations:
            continue
        current = operations[operation_id]
        payload = dict(row.get("payload") or {})
        if kind == "operation":
            current["status"] = str(row.get("status") or current.get("status") or "running")
        current["timestamp"] = str(current.get("timestamp") or row.get("timestamp") or "")
        current["last_event_at"] = str(row.get("timestamp") or current.get("last_event_at") or "")
        if kind != "operation":
            continue
        if payload.get("title") is not None:
            current["title"] = str(payload.get("title") or "")
        if payload.get("detail") is not None:
            current["detail"] = str(payload.get("detail") or "")
        if payload.get("started_at") is not None:
            current["started_at"] = payload.get("started_at")
        if payload.get("finished_at") is not None:
            current["finished_at"] = payload.get("finished_at")
        if payload.get("duration_ms") is not None:
            current["duration_ms"] = payload.get("duration_ms")
        if payload.get("output_preview") is not None:
            current["output_preview"] = str(payload.get("output_preview") or "")
    for current in operations.values():
        current.pop("live_output_preview", None)
    rows = [operations[op_id] for op_id in ordered_ids][-20:]
    rows.reverse()
    return rows


def _canonical_flow_item(row: dict[str, Any]) -> dict[str, Any]:
    kind = str(row.get("kind") or "")
    status = str(row.get("status") or "")
    payload = dict(row.get("payload") or {})
    frame_depth = 0
    if kind == "frame":
        frame_depth = int(payload.get("depth") or 0)
    else:
        frame_depth = int(payload.get("frame_depth") or 0)
    content = ""
    if kind == "llm":
        content = str(payload.get("summary") or payload.get("content_text") or payload.get("content") or "")
    elif kind == "tool":
        content = str(payload.get("content") or "")
    elif kind == "decision":
        content = str(payload.get("message") or "")
    elif kind == "observation":
        content = str(payload.get("summary") or "")
    elif kind == "frame":
        content = str(payload.get("message") or "")
    item: dict[str, Any] = {
        "label": kind,
        "status": status,
        "content": content,
        "details": payload,
        "frame_depth": frame_depth,
    }
    if kind == "llm":
        item["event_name"] = str(payload.get("event_name") or "")
    if kind == "tool":
        item["tool_name"] = str(payload.get("tool_name") or "")
        item["tool_args"] = payload.get("tool_args") or payload.get("args") or {}
        tool_result = payload.get("tool_result") or payload.get("result")
        if isinstance(tool_result, dict):
            item["parsed_payload"] = tool_result
    if kind == "decision":
        item["code"] = str(payload.get("decision_type") or "")
        item["reason_code"] = str(payload.get("reason_code") or "")
        item["human_title"] = str(payload.get("human_title") or "")
        item["human_desc"] = str(payload.get("human_desc") or "")
    if kind == "observation":
        item["code"] = str(payload.get("source") or "")
    if kind == "frame":
        item["frame_id"] = str(payload.get("frame_id") or "")
        item["parent_frame_id"] = str(payload.get("parent_frame_id") or "")
        item["goal"] = str(payload.get("goal") or "")
        item["return_payload"] = payload.get("return_payload") or {}
    return item


def _canonical_phase(step: dict[str, Any]) -> str:
    items = step.get("items") or []
    labels = {str(item.get("label") or "") for item in items if isinstance(item, dict)}
    if "decision" in labels:
        decision_items = [item for item in items if isinstance(item, dict) and str(item.get("label") or "") == "decision"]
        if any(str(item.get("code") or "") == "finish" and str(item.get("status") or "") == "accepted" for item in decision_items):
            return "FINISH"
        return "DECISION"
    if "frame" in labels:
        return "FRAME"
    if "tool" in labels:
        tool_names = {str(item.get("tool_name") or "") for item in items if isinstance(item, dict)}
        if "run_command" in tool_names:
            return "EXECUTE_MISSING_COMMANDS"
        return "TOOL"
    if "llm" in labels:
        return "LLM"
    if "observation" in labels:
        return "OBSERVATION"
    return "DISCOVER_REQUIRED_COMMANDS"


def _coalesce_canonical_flow_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    terminal_by_key: set[tuple[str, str]] = set()
    for item in items:
        label = str(item.get("label") or "")
        status = str(item.get("status") or "")
        if label not in {"llm", "tool"}:
            continue
        if status not in {"finished", "failed", "invalid_output"}:
            continue
        key = (label, str(item.get("tool_name") or "llm"))
        terminal_by_key.add(key)
    coalesced: list[dict[str, Any]] = []
    for item in items:
        label = str(item.get("label") or "")
        status = str(item.get("status") or "")
        key = (label, str(item.get("tool_name") or "llm"))
        if label in {"llm", "tool"} and status == "started" and key in terminal_by_key:
            continue
        coalesced.append(item)
    return coalesced



def _prompt_snapshot_index(rows: list[dict[str, Any]]) -> dict[tuple[str, int], dict[str, Any]]:
    index: dict[tuple[str, int], dict[str, Any]] = {}
    for row in rows:
        turn_id = str(row.get("turn_id") or "")
        if not turn_id:
            continue
        try:
            step_index = int(row.get("step_index") or 0)
        except Exception:
            continue
        index[(turn_id, step_index)] = row
    return index


def _canonical_flow_steps_for_operation(
    operation: dict[str, Any],
    events: list[dict[str, Any]],
    prompt_snapshots: dict[tuple[str, int], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    operation_id = str(operation.get("operation_id") or "")
    operation_finished = str(operation.get("status") or "") == "finished"
    grouped: dict[int, dict[str, Any]] = {}
    order: list[int] = []
    
    # Pre-process failure translations
    _FAILURE_TRANSLATIONS = {
        "missing_work_package_contract": {"title": "計画分解の契約不足", "desc": "子タスク化に必要な work_package が具体的な実行責務に着地していない"},
        "json_extraneous_text": {"title": "machine-control JSON形式違反", "desc": "JSONの外側にMarkdownや前置きがある"},
        "schema_validation_failed": {"title": "schema不一致", "desc": "JSON自体は読めるが、必要項目・enum・型がschemaと合わない"},
        "contract_incomplete": {"title": "完了条件未達", "desc": "TaskState が CompletionContract を満たしていない"},
        "judge_invalid_output": {"title": "judge出力形式エラー", "desc": "acceptance judge から verdict/status (ok|ng / success|partial_success|needs_revision) を抽出できなかった"},
        "json_extraneous_text_recovered": {"title": "✓ recovery: 余計テキスト除去", "desc": "LLM出力に余計テキストが含まれたが、最初の有効ツールエンベロープを採用して前進した (やり切る recovery)"},
        "judge_error": {"title": "judge実行エラー", "desc": "完了判定用のjudge呼び出し自体が失敗した"},
        "grounding_judge_error": {"title": "grounding judge実行エラー", "desc": "根拠判定judgeの呼び出しまたは応答取得に失敗した"},
        "grounding_issues": {"title": "根拠判定NG", "desc": "最終回答が収集済みEvidenceで支えられていない"},
        "tool_failed": {"title": "ツール実行失敗", "desc": "toolが returncode 非0または ok=false を返した"},
        "child_task_incomplete": {"title": "子タスク未完了", "desc": "first_action は成功したが success_evidence 未達"},
        "work_unit_success_evidence_not_observed": {"title": "WorkUnit成功条件未達", "desc": "toolは成功したが、PlanRecordのsuccess_evidenceに対応する観測証拠がまだない"},
    }

    for row in events:
        if str(row.get("operation_id") or "") != operation_id:
            continue
        kind = str(row.get("kind") or "")
        if kind == "operation":
            continue
            
        step_index = int(row.get("step_index") or 0)
        if step_index not in grouped:
            order.append(step_index)
            grouped[step_index] = {
                "step_index": step_index,
                "title": "Input" if step_index == 0 else f"Step {step_index}",
                "items": [],
                "raw_events": [],
            }
            
        payload = dict(row.get("payload") or {})
        
        # Translate failure blocks
        code = str(payload.get("code") or payload.get("decision_type") or "")
        reason_code = str(payload.get("reason_code") or "")
        parse_issue = str(payload.get("details", {}).get("parse_issue") or payload.get("parse_issue") or "")
        
        target_code = code
        if code == "llm_output_issue": target_code = parse_issue
        if code in {"work_package_invalid", "decompose_tasks_blocked", "open_child_frame_blocked", "frame_open_blocked", "work_unit_success_evidence_blocked"} and reason_code:
            target_code = reason_code
        if code == "grounding_judge" and reason_code == "error":
            target_code = "grounding_judge_error"
        if not target_code: target_code = reason_code
        
        if target_code in _FAILURE_TRANSLATIONS:
            payload["human_title"] = _FAILURE_TRANSLATIONS[target_code]["title"]
            payload["human_desc"] = _FAILURE_TRANSLATIONS[target_code]["desc"]
            row["payload"] = payload
        elif "LLM output did not satisfy machine-control schema" in str(row.get("content") or ""):
            if "json_extraneous_text" in str(row.get("content") or ""):
                payload["human_title"] = _FAILURE_TRANSLATIONS["json_extraneous_text"]["title"]
                payload["human_desc"] = _FAILURE_TRANSLATIONS["json_extraneous_text"]["desc"]
                row["payload"] = payload
                
        grouped[step_index]["raw_events"].append(row)

    rows = []
    
    # Sub-task grouping variables
    child_tasks = []
    current_child_task = None
    
    def _close_child_task(final: bool = False):
        nonlocal current_child_task
        if current_child_task:
            status = str(current_child_task.get("status") or "")
            if not operation_finished:
                if final:
                    if status in {"blocked", "failed"}:
                        current_child_task["status"] = "running"
                elif status in {"blocked", "failed"} or current_child_task.get("has_warnings"):
                    current_child_task["status"] = "finished_with_warnings"
                elif status == "running":
                    current_child_task["status"] = "finished"
            current_child_task.pop("has_warnings", None)
            child_tasks.append(current_child_task)
            current_child_task = None

    def _open_child_task(title):
        nonlocal current_child_task
        _close_child_task()
        current_child_task = {
            "is_child_task": True,
            "title": title,
            "steps": [],
            "status": "running"
        }

    for index in order:
        step_group = grouped[index]
        raw_events = step_group.get("raw_events", [])
        
        # Aggregate raw events into consolidated cards
        cards_map = {}
        ordered_cards = []
        
        for row in raw_events:
            kind = str(row.get("kind") or "")
            status = str(row.get("status") or "")
            payload = dict(row.get("payload") or {})
            
            action_id = str(row.get("action_id") or row.get("tool_call_id") or payload.get("tool_call_id") or payload.get("action_id") or "")
            
            if kind == "llm":
                action_id = "llm_card"
            elif kind == "tool":
                action_id = action_id or "tool_card"
            elif kind == "decision" and str(payload.get("decision_type", "")) in ("finish_acceptance", "controller_finish", "grounding_judge", "finish_blocked"):
                action_id = "finish_card"
            elif kind == "system_note" and str(payload.get("code", "")) == "finish_blocked":
                action_id = "finish_card"
            else:
                action_id = action_id or f"item_{len(ordered_cards)}"
                
            if action_id not in cards_map:
                ordered_cards.append(action_id)
                cards_map[action_id] = {
                    "label": "consolidated_card",
                    "card_type": action_id, # 'llm_card', 'tool_card', 'finish_card', etc.
                    "status": "running",
                    "events": [],
                }
                
            cards_map[action_id]["events"].append(row)
            
            # Update status of card based on terminal events
            if status in {"finished", "failed", "invalid_output", "blocked"}:
                cards_map[action_id]["status"] = status
                
        # Synthesize each card
        consolidated_items = []
        for action_id in ordered_cards:
            card = cards_map[action_id]
            card_type = card["card_type"]
            events = card["events"]
            
            synth = {
                "label": "consolidated_card",
                "card_type": "generic",
                "status": card["status"],
                "content": "",
                "details": {},
                "raw_events": events,
                "frame_depth": int(events[0].get("payload", {}).get("frame_depth") or 0) if events else 0
            }
            
            if card_type == "llm_card":
                synth["card_type"] = "llm"
                last_response_text = ""
                for ev in events:
                    p = ev.get("payload", {})
                    event_name = str(p.get("event_name") or "")
                    if event_name == "llm_response_received":
                        last_response_text = str(p.get("content_text") or "")
                        synth["details"].setdefault("rejected_attempts", []).append(
                            {
                                "title": "LLM → P4 Output (rejected)",
                                "content_text": last_response_text,
                                "model": p.get("model"),
                                "event_name": event_name,
                            }
                        )
                    if ev.get("status") == "started":
                        prompt_row = (prompt_snapshots or {}).get((str(ev.get("turn_id") or ""), int(ev.get("step_index") or 0))) or {}
                        prompt_text = str(prompt_row.get("prompt") or "")
                        synth["details"]["role"] = p.get("role")
                        synth["details"]["transport"] = p.get("transport")
                        synth["details"]["schema_required"] = p.get("schema_required")
                        synth["details"]["attempt_count"] = p.get("attempt_count")
                        synth["details"]["prompt"] = prompt_text
                        synth["details"]["prompt_preview"] = prompt_text[:2000]
                        synth["details"]["model_reason"] = prompt_row.get("model_reason")
                    elif ev.get("status") == "stream":
                        synth["details"]["streaming_text"] = p.get("content_text") or p.get("thinking_text")
                    elif event_name == "llm_stream_aborted":
                        synth["details"].setdefault("causal_notes", []).append(
                            {
                                "title": "前回LLM出力を中断",
                                "summary": p.get("content") or "LLM stream was stopped by runtime.",
                                "failure_type": p.get("parse_issue") or p.get("reason_code") or "stream_aborted",
                            }
                        )
                    elif ev.get("status") == "invalid_output" or event_name == "llm_repair_requested":
                        parse_issue = p.get("parse_issue") or (p.get("details") or {}).get("parse_issue") or "invalid_output"
                        synth["details"].setdefault("causal_notes", []).append(
                            {
                                "title": "P4 runtime判定: rejected",
                                "summary": p.get("content") or f"Repair requested: {parse_issue}",
                                "failure_type": parse_issue,
                                "rejected_output_preview": last_response_text[:600] if last_response_text else "",
                                "next": "同じStep内で、P4はJSON契約に合う応答をLLMへ再要求しました。",
                            }
                        )
                    elif ev.get("status") == "finished":
                        synth["details"]["final_text"] = p.get("content_text") or ev.get("content")
                        synth["details"]["thinking_text"] = p.get("thinking_text")
                        synth["details"]["model"] = p.get("model")
                        
                        import json
                        try:
                            parsed = json.loads(synth["details"]["final_text"])
                            if isinstance(parsed, dict):
                                synth["details"]["analysis"] = parsed.get("analysis")
                                synth["details"]["assistant_message"] = parsed.get("assistant_message")
                                synth["details"]["tool_name"] = parsed.get("tool_name")
                                synth["details"]["tool_args"] = parsed.get("tool_args")
                        except Exception:
                            pass
                if synth["details"].get("rejected_attempts") and synth["details"].get("final_text"):
                    final_text = str(synth["details"].get("final_text") or "")
                    synth["details"]["rejected_attempts"] = [
                        attempt
                        for attempt in synth["details"].get("rejected_attempts", [])
                        if str(attempt.get("content_text") or "") != final_text
                    ]
            elif "tool" in card_type:
                synth["card_type"] = "tool"
                for ev in events:
                    p = ev.get("payload", {})
                    if ev.get("kind") == "tool":
                        synth["details"]["tool_name"] = p.get("tool_name")
                        if "tool_args" in p: synth["details"]["tool_args"] = p.get("tool_args")
                        if "tool_result" in p: synth["details"]["tool_result"] = p.get("tool_result")
                        if ev.get("status") == "finished":
                            synth["details"]["status"] = "finished"
            elif card_type == "finish_card":
                synth["card_type"] = "finish"
                for ev in events:
                    p = ev.get("payload", {})
                    if p.get("decision_type") == "finish_acceptance":
                        synth["details"]["acceptance"] = p
                    elif p.get("decision_type") == "controller_finish":
                        synth["details"]["controller_finish"] = p
                    elif p.get("decision_type") == "grounding_judge":
                        synth["details"]["grounding_judge"] = p
                    elif p.get("decision_type") == "finish_blocked":
                        synth["details"]["blocked"] = p
                    elif ev.get("kind") == "system_note" and p.get("code") == "finish_blocked":
                        synth["details"]["blocked"] = p
            else:
                synth["card_type"] = "generic"
                # fallback to just canonical item
                synth = _canonical_flow_item(events[0])
                
            consolidated_items.append(synth)
        for item_index, item in enumerate(consolidated_items):
            if item.get("card_type") != "llm":
                continue
            details = item.get("details") if isinstance(item.get("details"), dict) else {}
            proposed_tool = str(details.get("tool_name") or "")
            if not proposed_tool:
                continue
            for candidate in consolidated_items[item_index + 1:]:
                if candidate.get("card_type") != "tool":
                    continue
                candidate_details = candidate.get("details") if isinstance(candidate.get("details"), dict) else {}
                executed_tool = str(candidate_details.get("tool_name") or "")
                if executed_tool and executed_tool == proposed_tool:
                    details["executed_tool"] = candidate_details
                    item["details"] = details
                    candidate["hidden"] = True
                    break
            
        step_group["items"] = consolidated_items
        step_group["phase"] = _canonical_phase(step_group)
        
        # Sub-task grouping logic based on items
        has_decompose = any(i.get("details", {}).get("tool_name") == "decompose_tasks" for i in consolidated_items if i.get("card_type") == "tool")
        has_file_ops = any(i.get("details", {}).get("tool_name") in ("write_file", "append_file", "read_file") for i in consolidated_items if i.get("card_type") == "tool")
        has_run = any(i.get("details", {}).get("tool_name") == "run_command" for i in consolidated_items if i.get("card_type") == "tool")
        has_finish = any(i.get("card_type") == "finish" for i in consolidated_items)
        
        if not current_child_task:
            if has_decompose:
                _open_child_task("計画分解")
            elif has_file_ops:
                _open_child_task("ファイル作成・修正")
            elif has_run:
                _open_child_task("実行して表示")
            elif has_finish:
                _open_child_task("完了判定")
            else:
                _open_child_task("処理中")
                
        # Adjust grouping if phase shifts
        if current_child_task["title"] == "計画分解" and not has_decompose and (has_file_ops or has_run):
            if has_file_ops: _open_child_task("ファイル作成・修正")
            elif has_run: _open_child_task("実行して表示")
            
        if current_child_task["title"] == "ファイル作成・修正" and has_run and not has_file_ops:
            _open_child_task("実行して表示")
            
        if has_finish and current_child_task["title"] != "完了判定":
            _open_child_task("完了判定")
            
        current_child_task["steps"].append(step_group)
        
        # Set task status
        if any(i.get("status") == "blocked" for i in consolidated_items):
            current_child_task["has_warnings"] = True
            current_child_task["status"] = "finished_with_warnings" if operation_finished else "running"
        elif any(i.get("status") == "failed" for i in consolidated_items):
            current_child_task["has_warnings"] = True
            current_child_task["status"] = "finished_with_warnings" if operation_finished else "running"
        elif operation_finished and current_child_task["status"] == "running":
            current_child_task["status"] = "finished" # optimistic finish for now

    _close_child_task(final=True)
    if operation_finished and str(operation.get("status") or "") == "finished":
        for task in child_tasks:
            if str(task.get("status") or "") in {"running", "blocked", "finished_with_warnings"}:
                task["status"] = "finished"
            if str(task.get("title") or "") == "処理中":
                task["title"] = "実行フロー"
    
    return child_tasks


def _canonical_commentator_notes(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    notes: list[dict[str, Any]] = []
    for row in reversed(events):
        if str(row.get("kind") or "") != "observation":
            continue
        payload = dict(row.get("payload") or {})
        if str(payload.get("source") or "") != "observer":
            continue
        notes.append(
            {
                "timestamp": str(row.get("timestamp") or ""),
                "content": str(payload.get("summary") or ""),
                "step_index": row.get("step_index"),
                "code": str(payload.get("code") or ""),
                "reason_code": str(payload.get("reason_code") or ""),
                "model": str(payload.get("model") or ""),
            }
        )
        if len(notes) >= 5:
            break
    return notes

def _judge_metrics(events: list[dict[str, Any]]) -> dict[str, Any]:
    consecutive_finish_blocks = 0
    last_judge_decision = ""
    judge_retry_count = 0
    fallback_used = False
    for row in reversed(events):
        row_type = str(row.get("type") or "")
        if row_type == "tool_result" and consecutive_finish_blocks:
            break
        if row_type != "system_note":
            continue
        code = str(row.get("code") or "")
        reason = str(row.get("reason_code") or "")
        details = row.get("details") if isinstance(row.get("details"), dict) else {}
        if code == "finish_blocked" and reason in {"finish_acceptance_failed", "judge_invalid_output", "judge_error", "grounding_issues"}:
            consecutive_finish_blocks += 1
        if not last_judge_decision and code in {"finish_acceptance", "grounding_judge", "finish_blocked"}:
            last_judge_decision = reason or str((details or {}).get("semantic_status") or (details or {}).get("status") or "")
        review = (details or {}).get("review") if isinstance((details or {}).get("review"), dict) else {}
        judge = (details or {}).get("judge") if isinstance((details or {}).get("judge"), dict) else {}
        judge_retry_count = max(
            judge_retry_count,
            int((review or {}).get("retry_count") or 0),
            int((judge or {}).get("judge_retry_count") or 0),
        )
        status = str((details or {}).get("status") or "")
        semantic_status = str((details or {}).get("semantic_status") or "")
        if code == "judge_fallback_finish" or status == "accepted_with_warning" or semantic_status == "review_unavailable_observation_accepted":
            fallback_used = True
    return {
        "consecutive_finish_blocks": consecutive_finish_blocks,
        "last_judge_decision": last_judge_decision or "-",
        "judge_retry_count": judge_retry_count,
        "fallback_used": fallback_used,
    }

def _format_live_text(*, thinking_text: str, content_text: str) -> str:
    thinking = str(thinking_text or "")
    content = str(content_text or "")
    if thinking and content:
        return f"[thinking]\n{thinking}\n\n[content]\n{content}"
    if thinking:
        return f"[thinking]\n{thinking}"
    return content

def _canonical_display_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row
        for row in events
        if not (
            str(row.get("kind") or "") in {"llm", "tool"}
            and str(row.get("status") or "") == "stream"
        )
    ]

def _read_canonical_snapshot_events(path: Path, *, display_limit: int = 2000, stream_tail_limit: int = 2000) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    display_lines: deque[str] = deque(maxlen=display_limit)
    stream_lines: deque[str] = deque(maxlen=stream_tail_limit)
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                str(row.get("kind") or "") in {"llm", "tool"}
                and str(row.get("status") or "") == "stream"
            ):
                stream_lines.append(line)
            else:
                display_lines.append(line)
    rows = [json.loads(line) for line in display_lines]
    rows.extend(json.loads(line) for line in stream_lines)
    rows.sort(key=lambda row: (str(row.get("timestamp") or ""), str(row.get("event_id") or "")))
    return rows

def build_snapshot(root: Path) -> dict[str, Any]:
    paths = WorkspacePaths(root)
    session_id = active_session_id(root)
    runtime = dict(read_json(paths.runtime_status_path, fallback={}) or {})
    if str(runtime.get("status") or "").startswith("running") and not _runtime_live_is_fresh(runtime):
        runtime["status"] = "idle"
        runtime["current_stream_text"] = ""
        runtime["worker_running"] = False
    benchmark = read_json(paths.benchmark_status_path, fallback={})
    session = read_json(paths.session_meta_path(session_id), fallback={})
    events = read_jsonl(paths.session_events_path(session_id), limit=1000)
    canonical_events = _read_canonical_snapshot_events(paths.session_canonical_events_path(session_id), display_limit=2000, stream_tail_limit=2000)
    prompt_snapshots = _prompt_snapshot_index(read_jsonl(paths.session_prompts_path(session_id), limit=500))
    transcript = [row for row in events if row.get("type") in {"user_message", "assistant_message"}][-40:]
    transcript.reverse()
    if canonical_events:
        canonical_display_events = _canonical_display_events(canonical_events)
        operations = _canonical_operation_rows(canonical_display_events)
        runtime_live_is_fresh = _runtime_live_is_fresh(runtime)
        for operation in operations:
            if (
                str(operation.get("operation_id") or "") == str(runtime.get("current_operation_id") or "")
                and str(operation.get("status") or "") in {"started", "running"}
                and runtime_live_is_fresh
            ):
                operation["output_preview"] = str(runtime.get("current_stream_text") or "")
            operation["flow_steps"] = _canonical_flow_steps_for_operation(operation, canonical_display_events, prompt_snapshots)
            operation_assistant = next(
                (
                    row
                    for row in events
                    if row.get("type") in {"assistant_message", "finish"}
                    and str(row.get("operation_id") or "") == str(operation.get("operation_id") or "")
                ),
                None,
            )
            if operation_assistant is not None:
                operation["transcript_preview"] = str(operation_assistant.get("content") or "")
    else:
        operations = _normalize_operation_rows(runtime, _operation_rows(events))
        if operations:
            for index, operation in enumerate(operations):
                if index == 0 and str(operation.get("status") or "") in {"started", "running"} and _runtime_live_is_fresh(runtime):
                    operation["output_preview"] = str(runtime.get("current_stream_text") or operation.get("output_preview") or "")
                trace_preview = _trace_preview_for_operation(operation, events)
                if trace_preview:
                    operation["trace_preview"] = trace_preview
                    operation["flow_preview"] = trace_preview
                operation["flow_steps"] = _flow_steps_for_operation(operation, events)
                _append_live_stream_step(operation, runtime)
                blocked_reason = _latest_blocked_reason(operation["flow_steps"])
                if blocked_reason:
                    operation["blocked_reason"] = blocked_reason
                    if str(operation.get("status") or "") not in {"started", "running"} or not bool(runtime.get("worker_running", False)):
                        operation["status"] = "blocked"
                    if not operation.get("output_preview") or str(operation.get("status") or "") == "blocked":
                        operation["output_preview"] = blocked_reason
                operation_assistant = next((row for row in events if row.get("type") in {"assistant_message", "finish"} and _event_in_operation_window(row, operation)), None)
                if operation_assistant is not None:
                    operation["transcript_preview"] = str(operation_assistant.get("content") or "")
                if str(operation.get("status") or "") != "running":
                    latest_tool_result = next((row for row in reversed(events) if row.get("type") == "tool_result" and _event_in_operation_window(row, operation)), None)
                    if latest_tool_result is not None and str(operation.get("status") or "") != "blocked":
                        operation["output_preview"] = str(latest_tool_result.get("content") or operation.get("output_preview") or "")
    contract_progress = _compute_contract_progress(canonical_display_events) if canonical_events else _compute_contract_progress(_legacy_events_for_contract_progress(events))
    if not contract_progress.get("required_contract"):
        current_user_message = str(runtime.get("current_user_message") or "").strip()
        if current_user_message:
            contract_progress["required_contract"] = _finish_acceptance_contract(current_user_message)
            if contract_progress["required_contract"] and contract_progress.get("contract_state") == "unknown":
                contract_progress["contract_state"] = "running"
    return {
        "root": str(Path(root).expanduser().resolve()),
        "model": _reasoning_model(root),
        "available_models": _available_models(root),
        "runtime": runtime,
        "judge_metrics": _judge_metrics(events),
        "benchmark": benchmark,
        "session": session,
        "recent_transcript": transcript,
        "recent_updates": [],
        "recent_operations": operations,
        "commentator_notes": _canonical_commentator_notes(canonical_display_events) if canonical_events else _commentator_notes(events),
        "frames": FrameManager(root).snapshot(),
        "latest_result": _latest_result_from_canonical(canonical_events) if canonical_events else _latest_result_from_legacy(session),
        "contract_progress": contract_progress,
    }


def _latest_result_from_canonical(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Derive the latest result panel content from canonical events only.

    Per p4-event-contract-decisions Decision 3, the dashboard reads canonical
    `kind=tool` events instead of the legacy `session.last_tool_result` cache.
    """
    summary = ""
    body = ""
    source = "none"
    latest_operation_id = ""
    for row in reversed(events):
        kind = str(row.get("kind") or "")
        status = str(row.get("status") or "")
        payload = row.get("payload") or {}
        if kind == "operation" and status in {"finished", "failed", "blocked"}:
            latest_operation_id = str(row.get("operation_id") or payload.get("operation_id") or "")
            output_preview = str(payload.get("output_preview") or "")
            if output_preview:
                summary = output_preview
            break
    if not latest_operation_id:
        return {"summary": summary, "body": "", "source": source}
    for row in reversed(events):
        if str(row.get("operation_id") or "") != latest_operation_id:
            continue
        kind = str(row.get("kind") or "")
        status = str(row.get("status") or "")
        payload = row.get("payload") or {}
        if kind == "tool" and status == "finished":
            tool_result = payload.get("tool_result") or {}
            if isinstance(tool_result, dict):
                for key in ("stdout", "stderr", "error"):
                    value = tool_result.get(key)
                    if value:
                        body = str(value)
                        source = "tool"
                        break
        if body:
            break
        if kind == "decision" and status == "finished":
            for key in ("final_answer", "message", "content", "summary"):
                value = payload.get(key)
                if value:
                    body = str(value)
                    source = "decision"
                    break
        if body:
            break
    if not body and summary:
        body = summary
        source = "operation"
    return {"summary": summary, "body": body, "source": source}


def _latest_result_from_legacy(session: dict[str, Any]) -> dict[str, Any]:
    """Legacy fallback when canonical events are absent (migration only).

    Pre-canonical sessions still rely on `session.last_tool_result`. This path
    is kept so old sessions render, but new sessions go through the canonical
    derivation above.
    """
    if not isinstance(session, dict):
        return {"summary": "", "body": "", "source": "none"}
    summary = str(session.get("last_assistant_message") or "")
    body = ""
    raw_tool = session.get("last_tool_result")
    if raw_tool:
        try:
            payload = json.loads(str(raw_tool))
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            for key in ("stdout", "stderr", "error"):
                value = payload.get(key)
                if value:
                    body = str(value)
                    break
    if not body:
        body = str(session.get("last_finish_message") or "")
    return {"summary": summary, "body": body, "source": "legacy"}


def _compute_contract_progress(events: list[dict[str, Any]]) -> dict[str, Any]:
    state = {
        "contract_state": "unknown",
        "required_contract": [],
        "artifact_written": "no",
        "command_executed": "no",
        "stdout_displayed": "no",
        "result_selected_for_user": "no"
    }
    
    # Defaults based on tool events (optimistic view)
    for row in events:
        kind = str(row.get("kind") or "")
        payload = row.get("payload") or {}
        if kind == "tool":
            tname = str(payload.get("tool_name") or "")
            ok = bool(payload.get("tool_result", {}).get("ok"))
            if tname in ("write_file", "append_file", "replace_text") and ok:
                state["artifact_written"] = "yes"
            if tname == "run_command" and ok:
                state["command_executed"] = "yes"
                if str(payload.get("tool_result", {}).get("stdout") or "").strip():
                    state["stdout_displayed"] = "yes"
                    
        # Override with finish_acceptance decision if present
        if kind == "decision" and payload.get("decision_type") == "finish_acceptance":
            details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
            state["contract_state"] = details.get("status") or payload.get("status", "unknown")
            contract = details.get("contract") if isinstance(details.get("contract"), list) else payload.get("contract", [])
            if isinstance(contract, list):
                state["required_contract"] = [str(item) for item in contract if str(item or "").strip()]
            evidence = details.get("evidence") if isinstance(details.get("evidence"), dict) else payload.get("evidence", {})
            if "artifact_written" in evidence:
                state["artifact_written"] = "yes" if evidence["artifact_written"] else "no"
            if "command_executed" in evidence:
                state["command_executed"] = "yes" if evidence["command_executed"] else "no"
            if "stdout_displayed" in evidence:
                state["stdout_displayed"] = "yes" if evidence["stdout_displayed"] else "no"
            if state["contract_state"] == "success":
                state["result_selected_for_user"] = "yes"
                
    return state


def _legacy_events_for_contract_progress(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in events:
        event_type = str(row.get("type") or "")
        if event_type == "tool_result":
            tool_result = {}
            raw_content = row.get("content")
            if isinstance(raw_content, str) and raw_content.strip():
                try:
                    parsed = json.loads(raw_content)
                    if isinstance(parsed, dict):
                        tool_result = parsed
                except json.JSONDecodeError:
                    tool_result = {}
            if "ok" not in tool_result:
                tool_result["ok"] = bool(row.get("ok"))
            rows.append(
                {
                    "kind": "tool",
                    "payload": {
                        "tool_name": str(row.get("tool_name") or tool_result.get("tool") or ""),
                        "tool_result": tool_result,
                    },
                }
            )
        if event_type == "system_note" and str(row.get("code") or "") == "finish_acceptance":
            rows.append(
                {
                    "kind": "decision",
                    "payload": {
                        "decision_type": "finish_acceptance",
                        "details": row.get("details") if isinstance(row.get("details"), dict) else {},
                    },
                }
            )
    return rows
