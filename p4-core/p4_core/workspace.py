from __future__ import annotations

import json
import shutil
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import timezone, datetime
from pathlib import Path
from typing import Any

from p4_core.config_defaults import DEFAULT_TOOL_CONTENT_CHUNK_BYTES


DEFAULT_CONFIG = {
    "ollama_base_url": "http://127.0.0.1:11434",
    "models": {
        "reasoning": "gemma4:26b",
        "fast": "glm-4.7-flash",
        "coding": "qwen3-coder",
        "terminal": "qwen3-coder",
    },
    "ollama_options": {
        "reasoning": {"temperature": 0.2, "num_predict": 1024},
        "fast": {"temperature": 0.1, "num_predict": 384},
        "coding": {"temperature": 0.1, "num_predict": 8192},
        "terminal": {"temperature": 0.1, "num_predict": 4096},
    },
    "runtime": {
        "max_steps_per_message": 12,
        "verified_implementation_max_steps": 32,
        "planned_implementation_max_steps": 48,
        "child_frame_max_steps": 15,
        "worker_poll_seconds": 2,
        "chat_timeout_seconds": 180,
        # json_retry_limit: parse_issue (length_truncated, json_parse_error,
        # schema_validation_failed, invalid_tool_envelope) に対する 1 回の修復試行を有効化。
        # 0 のままだと LLM が 1 回でも machine-control schema を外すと turn 全体が
        # 即失敗する (やり切らない設計)。recovery 経路と組合せて bounded best-effort を
        # 保証する。詳細: handoff/p4-followthrough-recovery-2026-05-03.md
        "json_retry_limit": 2,
        "thinking_only_repair_limit": 1,
        "execution_root": "",
        "dedicated_llm_workspace": True,
        "tool_content_chunk_bytes": DEFAULT_TOOL_CONTENT_CHUNK_BYTES,
        "max_machine_control_stream_chars": 24000,
        "implementation_task_machine_control_stream_chars": 10000,
        "machine_control_repetition_tail_chars": 160,
        "machine_control_repetition_min_repeats": 5,
        "machine_control_repetition_min_similar_lines": 14,
        "machine_control_repetition_min_chars": 3000,
        "observer_enabled": False,
        "observer_model": "",
        "validation_failure_consultant_enabled": True,
        "validation_failure_consultant_max_chars": 1200,
        "semantic_implementation_review_enabled": True,
        "semantic_implementation_review_max_chars": 1600,
        "initial_implementation_contract_repair_limit": 3,
        "legacy_domain_task_routes_enabled": False,
    },
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path, fallback: Any | None = None) -> Any:
    if not path.exists():
        if fallback is None:
            raise FileNotFoundError(path)
        return fallback
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp_path.replace(path)


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def read_jsonl(path: Path, *, limit: int | None = None) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    if limit is not None:
        lines: deque[str] = deque(maxlen=limit)
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    lines.append(line)
        return [json.loads(line) for line in lines]
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if limit is None:
        return rows
    return rows[-limit:]


@dataclass
class WorkspacePaths:
    root: Path

    def __post_init__(self) -> None:
        self.root = self.root.expanduser().resolve()

    @property
    def state_dir(self) -> Path:
        return self.root / "state"

    @property
    def runtime_dir(self) -> Path:
        return self.state_dir / "runtime"

    @property
    def workspaces_dir(self) -> Path:
        return self.root / "workspaces"

    @property
    def llm_runs_dir(self) -> Path:
        return self.workspaces_dir / "runs"

    @property
    def sessions_dir(self) -> Path:
        return self.state_dir / "sessions"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def config_path(self) -> Path:
        return self.root / "config.json"

    @property
    def goal_path(self) -> Path:
        return self.state_dir / "goal.json"

    @property
    def active_session_path(self) -> Path:
        return self.runtime_dir / "active_session.json"

    @property
    def queue_path(self) -> Path:
        return self.runtime_dir / "queue.json"

    @property
    def runtime_status_path(self) -> Path:
        return self.runtime_dir / "status.json"

    @property
    def worker_pid_path(self) -> Path:
        return self.runtime_dir / "worker.pid"

    @property
    def reflections_path(self) -> Path:
        return self.runtime_dir / "reflections.jsonl"

    @property
    def planning_path(self) -> Path:
        return self.runtime_dir / "planning.jsonl"

    @property
    def benchmark_status_path(self) -> Path:
        return self.runtime_dir / "benchmark.json"

    def session_dir(self, session_id: str) -> Path:
        return self.sessions_dir / session_id

    def session_meta_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "meta.json"

    def session_events_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "events.jsonl"

    def session_canonical_events_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "canonical-events.jsonl"

    def session_prompts_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "prompts.jsonl"


def bootstrap_workspace(root: Path, *, force: bool = False) -> dict[str, Any]:
    paths = WorkspacePaths(root)
    if paths.root.exists() and any(paths.root.iterdir()) and not force:
        raise FileExistsError(f"workspace is not empty: {paths.root}")
    if paths.root.exists() and force:
        shutil.rmtree(paths.root)
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    paths.runtime_dir.mkdir(parents=True, exist_ok=True)
    paths.llm_runs_dir.mkdir(parents=True, exist_ok=True)
    paths.sessions_dir.mkdir(parents=True, exist_ok=True)
    paths.logs_dir.mkdir(parents=True, exist_ok=True)

    write_json(paths.config_path, DEFAULT_CONFIG)
    write_json(
        paths.goal_path,
        {"text": "", "status": "active", "updated_at": now_iso()},
    )
    write_json(
        paths.active_session_path,
        {"session_id": "main", "updated_at": now_iso()},
    )
    write_json(paths.queue_path, {"items": []})
    write_json(
        paths.runtime_status_path,
        {
            "status": "idle",
            "current_role": None,
            "current_turn_id": None,
            "current_queue_id": None,
            "current_user_message": None,
            "current_prompt_preview": None,
            "current_stream_text": "",
            "current_plan": None,
            "current_phase": None,
            "current_model": None,
            "current_model_reason": None,
            "current_operation_id": None,
            "current_tool": None,
            "current_llm_workspace": None,
            "last_llm_workspace": None,
            "current_started_at": None,
            "current_finished_at": None,
            "last_error": None,
            "last_system_note": None,
            "last_reflection": None,
            "last_llm_started_at": None,
            "last_llm_finished_at": None,
            "last_llm_duration_ms": None,
            "last_llm_attempt_count": 0,
            "last_llm_raw_preview": None,
            "last_llm_thinking_preview": None,
            "last_llm_parse_issue": None,
            "last_llm_schema_validation": None,
            "raw_output_is_machine_json": None,
            "schema_validation_ok": None,
            "last_llm_stream_metadata": None,
            "last_event_at": now_iso(),
            "worker_running": False,
        },
    )
    write_json(
        paths.benchmark_status_path,
        {
            "status": "idle",
            "started_at": None,
            "finished_at": None,
            "current_model": None,
            "current_case": None,
            "completed_models": 0,
            "completed_cases": 0,
            "results": [],
            "ranking": [],
            "recommended_next_target": None,
            "last_error": None,
        },
    )
    write_json(
        paths.session_meta_path("main"),
        {
            "session_id": "main",
            "title": "Main Session",
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "message_count": 0,
            "tool_call_count": 0,
            "last_assistant_message": None,
            "last_finish_message": None,
        },
    )
    return {"ok": True, "root": str(paths.root), "session_id": "main"}


def active_session_id(root: Path) -> str:
    return str(read_json(WorkspacePaths(root).active_session_path, fallback={}).get("session_id") or "main")


def update_goal(root: Path, text: str) -> dict[str, Any]:
    clean_text = str(text or "").strip()
    if not clean_text:
        raise ValueError("goal text must not be empty")
    payload = {"text": clean_text, "status": "active", "updated_at": now_iso()}
    write_json(WorkspacePaths(root).goal_path, payload)
    return {"ok": True, "goal": payload}


def append_session_event(root: Path, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    paths = WorkspacePaths(root)
    event = {"event_id": uuid.uuid4().hex, "timestamp": now_iso(), **payload}
    append_jsonl(paths.session_events_path(session_id), event)
    canonical = _canonical_event_from_session_event(event)
    if canonical is not None:
        _assert_canonical_event_shape(canonical)
        append_jsonl(paths.session_canonical_events_path(session_id), canonical)
    meta = read_json(paths.session_meta_path(session_id), fallback={})
    meta["updated_at"] = now_iso()
    if payload.get("type") == "user_message":
        meta["message_count"] = int(meta.get("message_count") or 0) + 1
    if payload.get("type") == "tool_call":
        meta["tool_call_count"] = int(meta.get("tool_call_count") or 0) + 1
    if payload.get("type") == "assistant_message":
        meta["last_assistant_message"] = payload.get("content")
    if payload.get("type") == "finish":
        meta["last_finish_message"] = payload.get("content")
    summary_source = payload.get("content") or payload.get("tool_name") or payload.get("type")
    meta["last_event_summary"] = str(summary_source or "")
    if payload.get("type") == "tool_result":
        meta["last_tool_result"] = payload.get("content")
    write_json(paths.session_meta_path(session_id), meta)
    return event


def append_canonical_event(root: Path, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    paths = WorkspacePaths(root)
    event = {
        "event_id": str(payload.get("event_id") or uuid.uuid4().hex),
        "timestamp": str(payload.get("timestamp") or now_iso()),
        **{key: value for key, value in payload.items() if key not in {"event_id", "timestamp"}},
    }
    _assert_canonical_event_shape(event)
    append_jsonl(paths.session_canonical_events_path(session_id), event)
    return event


# Canonical event schema guard (per p4-event-contract-audit-2026-04-24).
# Any non-canonical kind/status indicates a design-philosophy drift (e.g. an
# ad-hoc status like "accepted_with_warning" being added at the wrong layer).
# Surface the drift at write time so it cannot silently propagate.
_CANONICAL_KIND_STATUS: dict[str, set[str]] = {
    "operation": {"started", "finished", "failed", "blocked"},
    "llm": {"started", "stream", "finished", "invalid_output", "failed"},
    "tool": {"started", "stream", "finished", "failed"},
    "frame": {"opened", "returned", "blocked"},
    "decision": {"accepted", "blocked", "failed"},
    "observation": {"started", "finished", "stream"},
}


def _assert_canonical_event_shape(event: dict[str, Any]) -> None:
    kind = str(event.get("kind") or "")
    status = str(event.get("status") or "")
    allowed = _CANONICAL_KIND_STATUS.get(kind)
    if allowed is None:
        raise AssertionError(
            f"canonical event kind '{kind}' is not in the canonical schema; "
            f"see handoff/p4-event-contract-audit-2026-04-24.md for the allowed kinds"
        )
    if status not in allowed:
        raise AssertionError(
            f"canonical event status '{status}' is not allowed for kind '{kind}'; "
            f"allowed = {sorted(allowed)}. Lift the asymmetric variant into a "
            f"reason_code on a canonical status instead of inventing a new one."
        )


def _canonical_operation_status(status: str) -> str:
    """Normalize legacy operation statuses to the canonical set.

    Per p4-event-contract-audit-2026-04-24, canonical operation status is
    {started, finished, failed, blocked}. Existing emitters use legacy
    synonyms ("running", "success") which we normalize at the translation
    boundary so the canonical event log stays clean. The original value is
    preserved in payload.raw_status for forensic visibility.
    """
    legacy_map = {
        "running": "started",
        "success": "finished",
        "completed": "finished",
        "ok": "finished",
        "error": "failed",
        "abandoned": "blocked",
    }
    if status in {"started", "finished", "failed", "blocked"}:
        return status
    return legacy_map.get(status, "started")


def _decision_status_from_code(code: str, reason_code: str) -> str:
    """Map legacy decision codes to canonical decision status.

    Canonical decision status (per p4-event-contract-audit):
        "accepted" | "blocked" | "failed"
    """
    blocked_codes = {
        "finish_blocked",
        "command_blocked",
        "work_package_invalid",
        "decompose_tasks_blocked",
        "frame_open_blocked",
        "frame_return_blocked",
        "grounding_judge",
    }
    failed_codes = {"command_failed"}
    accepted_codes = {"controller_finish"}
    if code in blocked_codes:
        return "blocked"
    if code in failed_codes:
        return "failed"
    if code in accepted_codes:
        return "accepted"
    if code == "judge_fallback_finish":
        # accepted iff the runtime decided to override and complete the turn.
        # reason_code は trigger ("judge_unavailable", "judge_invalid_output",
        # "judge_error", "finish_acceptance_failed", "grounding_issues") に
        # "_observation_accepted" / "_observation_rejected" を付与した形を取る。
        if reason_code.endswith("_observation_accepted"):
            return "accepted"
        return "failed"
    if code == "finish_acceptance":
        # reason_code carries the effective acceptance outcome. Accept on
        # positive verdicts and on any observation-based override variant.
        if reason_code in {"reviewed", "not_required"} or reason_code.endswith("_observation_accepted"):
            return "accepted"
        return "blocked"
    if reason_code in {"invalid_output", "invalid_json", "empty_output"}:
        return "blocked"
    return "blocked"


def _canonical_event_from_session_event(event: dict[str, Any]) -> dict[str, Any] | None:
    row_type = str(event.get("type") or "")
    operation_id = str(event.get("operation_id") or "")
    turn_id = event.get("turn_id")
    step_index = event.get("step_index")
    frame_id = str(event.get("frame_id") or "")
    frame_depth = event.get("frame_depth")

    common = {
        "event_id": str(event.get("event_id") or uuid.uuid4().hex),
        "timestamp": str(event.get("timestamp") or now_iso()),
        "operation_id": operation_id,
        "turn_id": turn_id,
        "step_index": step_index,
        "parent_event_id": event.get("parent_event_id"),
    }

    if row_type == "operation":
        return {
            **common,
            "kind": "operation",
            "status": _canonical_operation_status(str(event.get("status") or "")),
            "payload": {
                "title": str(event.get("title") or ""),
                "detail": str(event.get("detail") or ""),
                "started_at": event.get("started_at"),
                "finished_at": event.get("finished_at"),
                "duration_ms": event.get("duration_ms"),
                "output_preview": str(event.get("output_preview") or ""),
                "raw_status": str(event.get("status") or ""),
            },
        }

    if row_type == "runtime_event":
        event_name = str(event.get("event_name") or "")
        details = dict(event.get("details") or {})
        base_payload = {
            **details,
            "content": str(event.get("content") or ""),
            "frame_id": frame_id,
            "frame_depth": frame_depth,
            "llm_workspace": event.get("llm_workspace"),
            "phase": event.get("phase"),
        }
        if event_name.startswith("llm_"):
            llm_status_map = {
                "llm_call_started": "started",
                "llm_stream_chunk": "stream",
                "llm_response_received": "finished",
                "llm_call_finished": "finished",
                "llm_call_failed": "failed",
                "llm_repair_requested": "invalid_output",
            }
            return {
                **common,
                "kind": "llm",
                "status": llm_status_map.get(event_name, "finished"),
                "payload": {
                    "event_name": event_name,
                    **base_payload,
                },
            }
        if event_name.startswith("tool_"):
            tool_status = "stream"
            if event_name == "tool_call_started":
                tool_status = "started"
            elif event_name == "tool_call_finished":
                tool_status = "finished" if bool(details.get("ok")) else "failed"
            return {
                **common,
                "kind": "tool",
                "status": tool_status,
                "payload": {
                    "event_name": event_name,
                    **base_payload,
                },
            }
        return {
            **common,
            "kind": "observation",
            "status": "finished",
            "payload": {
                "source": "runtime_event",
                "event_name": event_name,
                **base_payload,
            },
        }

    if row_type == "system_note":
        code = str(event.get("code") or "")
        reason_code = str(event.get("reason_code") or "")
        details = dict(event.get("details") or {})
        if code == "llm_output_issue":
            return {
                **common,
                "kind": "llm",
                "status": "invalid_output",
                "payload": {
                    "event_name": code,
                    "summary": str(event.get("content") or ""),
                    "parse_issue": reason_code,
                    **details,
                    "frame_id": frame_id,
                    "frame_depth": frame_depth,
                },
            }
        if code:
            return {
                **common,
                "kind": "decision",
                "status": _decision_status_from_code(code, reason_code),
                "payload": {
                    "decision_type": code,
                    "reason_code": reason_code,
                    "message": str(event.get("content") or ""),
                    "details": details,
                    "frame_id": frame_id,
                    "frame_depth": frame_depth,
                },
            }
        return {
            **common,
            "kind": "observation",
            "status": "finished",
            "payload": {
                "source": "system_note",
                "summary": str(event.get("content") or ""),
                "frame_id": frame_id,
                "frame_depth": frame_depth,
            },
        }

    if row_type == "observer_note":
        return {
            **common,
            "kind": "observation",
            "status": "finished",
            "payload": {
                "source": "observer",
                "summary": str(event.get("content") or ""),
                "code": str(event.get("code") or ""),
                "reason_code": str(event.get("reason_code") or ""),
                "details": dict(event.get("details") or {}),
                "frame_id": frame_id,
                "frame_depth": frame_depth,
            },
        }

    if row_type in {"activity_update", "planning_note"}:
        raw_status = str(event.get("status") or "")
        return {
            **common,
            "kind": "observation",
            "status": "finished",
            "payload": {
                "source": row_type,
                "summary": str(event.get("content") or ""),
                "raw_status": raw_status,
                "frame_id": frame_id,
                "frame_depth": frame_depth,
            },
        }

    if row_type == "task_plan":
        return {
            **common,
            "kind": "decision",
            "status": "accepted",
            "payload": {
                "decision_type": "task_plan",
                "message": str(event.get("content") or ""),
                "tasks": list(event.get("tasks") or []),
                "rationale": str(event.get("rationale") or ""),
                "frame_id": frame_id or str(event.get("frame_id") or ""),
                "frame_depth": frame_depth,
            },
        }

    if row_type in {"problem_profile", "planner_decision", "plan_record", "plan_revision"}:
        plan = event.get("plan") if isinstance(event.get("plan"), dict) else {}
        profile = event.get("profile") if isinstance(event.get("profile"), dict) else {}
        details = event.get("details") if isinstance(event.get("details"), dict) else {}
        return {
            **common,
            "kind": "decision",
            "status": "accepted" if row_type != "plan_revision" else "blocked",
            "payload": {
                "decision_type": row_type,
                "message": str(event.get("content") or ""),
                "strategy": str(event.get("strategy") or plan.get("strategy") or profile.get("strategy") or ""),
                "profile": profile,
                "plan": plan,
                "work_units": list(event.get("work_units") or plan.get("work_units") or []),
                "verification_contract": event.get("verification_contract") or plan.get("verification_contract") or {},
                "details": details,
                "frame_id": frame_id,
                "frame_depth": frame_depth,
            },
        }

    if row_type == "frame_opened":
        return {
            **common,
            "kind": "frame",
            "status": "opened",
            "payload": {
                "frame_id": str(event.get("frame_id") or ""),
                "parent_frame_id": str(event.get("parent_frame_id") or ""),
                "goal": str(event.get("goal") or ""),
                "depth": event.get("depth") if event.get("depth") is not None else frame_depth,
                "message": str(event.get("content") or ""),
            },
        }

    if row_type == "frame_returned":
        return {
            **common,
            "kind": "frame",
            "status": "returned",
            "payload": {
                "frame_id": str(event.get("frame_id") or ""),
                "parent_frame_id": str(event.get("parent_frame_id") or ""),
                "depth": event.get("depth") if event.get("depth") is not None else frame_depth,
                "return_payload": dict(event.get("return_payload") or {}),
                "message": str(event.get("content") or ""),
            },
        }

    if row_type == "finish":
        return {
            **common,
            "kind": "decision",
            "status": "accepted",
            "payload": {
                "decision_type": "finish",
                "message": str(event.get("content") or ""),
                "model": str(event.get("model") or ""),
                "model_reason": str(event.get("model_reason") or ""),
                "frame_id": frame_id,
                "frame_depth": frame_depth,
            },
        }

    return None


def append_prompt_snapshot(root: Path, session_id: str, payload: dict[str, Any]) -> None:
    append_jsonl(WorkspacePaths(root).session_prompts_path(session_id), {"timestamp": now_iso(), **payload})


def enqueue_message(
    root: Path,
    content: str,
    *,
    session_id: str | None = None,
    model_selection: dict[str, str] | None = None,
) -> dict[str, Any]:
    clean_content = str(content or "").strip()
    if not clean_content:
        raise ValueError("message content must not be empty")
    session_id = session_id or active_session_id(root)
    operation_id = uuid.uuid4().hex
    append_session_event(
        root,
        session_id,
        {"type": "user_message", "role": "user", "content": clean_content, "operation_id": operation_id, "step_index": 0},
    )
    paths = WorkspacePaths(root)
    queue = read_json(paths.queue_path, fallback={"items": []})
    items = list(queue.get("items") or [])
    item = {
        "queue_id": uuid.uuid4().hex,
        "operation_id": operation_id,
        "session_id": session_id,
        "content": clean_content,
        "enqueued_at": now_iso(),
    }
    if model_selection:
        item["model_selection"] = dict(model_selection)
    items.append(item)
    write_json(paths.queue_path, {"items": items})
    return {"ok": True, "queued": item}


def pop_next_queue_item(root: Path) -> dict[str, Any] | None:
    paths = WorkspacePaths(root)
    queue = read_json(paths.queue_path, fallback={"items": []})
    items = list(queue.get("items") or [])
    if not items:
        return None
    item = items.pop(0)
    write_json(paths.queue_path, {"items": items})
    return item


def queue_items(root: Path) -> list[dict[str, Any]]:
    return list(read_json(WorkspacePaths(root).queue_path, fallback={"items": []}).get("items") or [])
