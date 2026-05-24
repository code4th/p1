from __future__ import annotations

import json
import os
import re
import signal
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from p3_core.models import ModelRouter
from p3_core.ollama_client import OllamaChatClient
from p3_core.tools import ToolExecutor
from p3_core.workspace import (
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


_UNSET = object()


class AgentRuntime:
    def __init__(self, root: Path, *, llm_backend: Any | None = None) -> None:
        self.root = Path(root).expanduser().resolve()
        self.paths = WorkspacePaths(self.root)
        self.config = read_json(self.paths.config_path, fallback={})
        self.router = ModelRouter(self.config.get("models", {}))
        self.llm_backend = llm_backend or OllamaChatClient(base_url=str(self.config.get("ollama_base_url") or "http://127.0.0.1:11434"))
        self.ollama_options = dict(self.config.get("ollama_options", {}))
        self.runtime_config = dict(self.config.get("runtime", {}))
        execution_root = str(self.runtime_config.get("execution_root") or self.root)
        self.base_execution_root = Path(execution_root).expanduser().resolve()
        self.execution_root = self.base_execution_root
        self.tools = ToolExecutor(self.execution_root)
        self._last_grounding_judge_trace: dict[str, Any] | None = None

    def _observer_enabled(self) -> bool:
        return bool(self.runtime_config.get("observer_enabled"))

    def send_message(self, content: str, *, session_id: str | None = None, run_immediately: bool = False) -> dict[str, Any]:
        payload = enqueue_message(self.root, content, session_id=session_id)
        if run_immediately:
            loop_result = self.run_until_idle()
            payload["run"] = loop_result
        return payload

    def simple_chat(self, content: str, *, session_id: str | None = None) -> dict[str, Any]:
        clean = str(content or "").strip()
        if not clean:
            raise ValueError("message content must not be empty")
        session_id = session_id or active_session_id(self.root)
        user_event = append_session_event(
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
        assistant_event = append_session_event(
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

    def run_terminal_agent(
        self,
        content: str,
        *,
        model: str,
        shell_name: str,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        session_id = session_id or active_session_id(self.root)
        payload = enqueue_message(self.root, content, session_id=session_id)
        terminal_model = self._resolve_terminal_model(model)
        run_result = self.run_until_idle(
            max_work_items=1,
            selection_override={
                "role": "terminal",
                "model": terminal_model,
                "reason": f"terminal agent mode via {shell_name}",
            },
            extra_prompt=(
                "You are in terminal agent mode. Prefer terminal commands when they are the most direct path. "
                "Use exactly one command per run_command step. Do not chain commands with &&, ||, ;, or newlines. "
                f"For run_command, prefer shell='{shell_name}'. "
                "The runtime assigns a dedicated LLM workspace for this turn. Use relative paths inside that workspace. "
                "If run_command reports that the requested shell is unavailable, stop immediately and explain that shell is unavailable. "
                "You may also use list_files, read_file, search_code, and write_file when they are simpler than shell."
            ),
        )
        payload["run"] = {
            **run_result,
            "shell": shell_name,
            "execution_root": str(self.base_execution_root),
            "model": terminal_model,
        }
        return payload

    def _dedicated_llm_workspace_enabled(self) -> bool:
        return bool(self.runtime_config.get("dedicated_llm_workspace", True))

    def _prepare_turn_workspace(self, *, turn_id: str) -> Path:
        if not self._dedicated_llm_workspace_enabled():
            self.execution_root = self.base_execution_root
            self.tools = ToolExecutor(self.execution_root)
            return self.execution_root
        workspace = (self.paths.llm_runs_dir / turn_id).resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        self.execution_root = workspace
        self.tools = ToolExecutor(self.execution_root)
        return workspace

    def _resolve_terminal_model(self, preferred_model: str) -> str:
        available: set[str] = set()
        if hasattr(self.llm_backend, "list_models"):
            try:
                payload = self.llm_backend.list_models()
                available = {
                    str(item.get("name") or "").strip()
                    for item in payload.get("models") or []
                    if str(item.get("name") or "").strip()
                }
            except Exception:
                available = set()
        for candidate in self.router.terminal_fallback_chain(preferred_model):
            if not available or candidate in available:
                return candidate
        return self.router.models.get("terminal") or preferred_model

    def _conversation_messages(self, session_id: str) -> list[dict[str, str]]:
        # Increased limit to 100 to capture enough history for complex loops
        events = read_jsonl(self.paths.session_events_path(session_id), limit=100)
        messages: list[dict[str, str]] = []
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
        from p3_core.workspace import update_goal

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
                    current_llm_workspace=None,
                )
                break
            processed += 1
            last_result = self._process_queue_item(
                item,
                selection_override=selection_override,
                extra_prompt=extra_prompt,
            )
        return {
            "ok": True,
            "processed": processed,
            "last_result": last_result,
            "pending_queue": len(queue_items(self.root)),
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

        self._write_runtime_status(status="stopped", worker_running=False)
        if self.paths.worker_pid_path.exists():
            self.paths.worker_pid_path.unlink()

    def status_snapshot(self) -> dict[str, Any]:
        session_id = active_session_id(self.root)
        goal = read_json(self.paths.goal_path, fallback={})
        meta = read_json(self.paths.session_meta_path(session_id), fallback={})
        runtime = read_json(self.paths.runtime_status_path, fallback={})
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
        }

    def _process_queue_item(
        self,
        item: dict[str, Any],
        *,
        selection_override: dict[str, str] | None = None,
        extra_prompt: str | None = None,
    ) -> dict[str, Any]:
        session_id = str(item.get("session_id") or active_session_id(self.root))
        max_steps = int(self.config.get("runtime", {}).get("max_steps_per_message") or 12)
        recent_user_message = str(item.get("content") or "")
        queue_id = str(item.get("queue_id") or "")
        turn_id = uuid.uuid4().hex
        turn_workspace = self._prepare_turn_workspace(turn_id=turn_id)
        steps: list[dict[str, Any]] = []
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
        append_session_event(
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
            recent_events = read_jsonl(self.paths.session_events_path(session_id), limit=30)
            goal_text = str(read_json(self.paths.goal_path, fallback={}).get("text") or "")
            current_phase = self._current_phase(user_message=recent_user_message, steps=steps, recent_events=recent_events)
            selection = selection_override or self.router.select_model(
                goal_text=goal_text,
                pending_message=recent_user_message,
                recent_events=recent_events,
                current_phase=current_phase,
            )
            controller_finish = self._controller_terminal_finish(
                selection=selection,
                goal_text=goal_text,
                user_message=recent_user_message,
                steps=steps,
            )
            if controller_finish is not None:
                append_session_event(
                    self.root,
                    session_id,
                    {
                        "type": "system_note",
                        "role": "system",
                        "content": "コントローラーによる自動完了: ターミナルの実行結果に基づき、根拠のある最終回答を合成しました",
                        "code": "controller_finish",
                        "reason_code": "grounded_terminal_evidence",
                        "turn_id": turn_id,
                        "queue_id": queue_id,
                        "step_index": step_index,
                        "llm_workspace": str(turn_workspace),
                    },
                )
                append_session_event(
                    self.root,
                    session_id,
                    {
                        "type": "finish",
                        "role": "assistant",
                        "content": controller_finish,
                        "model": selection["model"],
                        "model_reason": f"{selection['reason']} + controller-finish",
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
                    current_model_reason=f"{selection['reason']} + controller-finish",
                    current_tool="finish",
                    current_llm_workspace=None,
                    last_llm_workspace=str(turn_workspace),
                    last_error=None,
                    last_system_note=None,
                    current_started_at=None,
                    current_finished_at=now_iso(),
                    worker_running=self._worker_running(),
                )
                return {"ok": True, "session_id": session_id, "steps": steps, "final_answer": controller_finish}
            prompt = self._build_prompt(
                goal_text=goal_text,
                recent_events=recent_events,
                extra_prompt=extra_prompt,
                steps=steps,
                current_phase=current_phase,
                user_message=recent_user_message,
            )
            append_prompt_snapshot(
                self.root,
                session_id,
                {
                    "turn_id": turn_id,
                    "queue_id": queue_id,
                    "step_index": step_index,
                    "model": selection["model"],
                    "model_reason": selection["reason"],
                    "prompt": prompt,
                },
            )
            fast_envelope = self._fast_path_envelope(
                step_index=step_index,
                selection=selection,
                user_message=recent_user_message,
                extra_prompt=extra_prompt,
                recent_events=recent_events,
                steps=steps,
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
                last_error=None,
                last_system_note=None,
                current_started_at=now_iso(),
                current_finished_at=None,
                worker_running=self._worker_running(),
            )
            if fast_envelope is not None:
                telemetry = {"attempt_count": 0, "raw_text": "", "envelope": fast_envelope, "parse_issue": ""}
                envelope = fast_envelope
                self._write_runtime_status(
                    status="running",
                    current_role=selection["role"],
                    current_turn_id=turn_id,
                    current_queue_id=queue_id,
                    current_user_message=recent_user_message,
                    current_prompt_preview=prompt[:2000],
                    current_stream_text=f"高速パスが選択されました: {fast_envelope.get('tool_name')} {json.dumps(fast_envelope.get('tool_args') or {}, ensure_ascii=False)}",
                    current_plan=planning_note,
                    current_phase=current_phase,
                    current_model=selection["model"],
                    current_model_reason=f"{selection['reason']} + fast-path",
                    current_tool=None,
                    current_llm_workspace=str(turn_workspace),
                    last_llm_workspace=str(turn_workspace),
                    last_error=None,
                    last_system_note=None,
                    current_started_at=read_json(self.paths.runtime_status_path, fallback={}).get("current_started_at") or now_iso(),
                    current_finished_at=None,
                    worker_running=self._worker_running(),
                )
            else:
                telemetry = self._chat_with_repair(
                    role=str(selection["role"]),
                    model=str(selection["model"]),
                    prompt=prompt,
                    session_id=session_id,
                )
                envelope = telemetry["envelope"]
            assistant_message = str(envelope.get("assistant_message") or "").strip()
            if assistant_message:
                append_session_event(
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
                append_session_event(
                    self.root,
                    session_id,
                    {
                        "type": "system_note",
                        "role": "system",
                        "content": f"LLM応答がツール呼び出しJSONとして解釈できませんでした: {telemetry.get('parse_issue')}",
                        "code": "llm_output_issue",
                        "reason_code": str(telemetry.get("parse_issue") or "invalid_tool_envelope"),
                        "details": {
                            "raw_text": str(telemetry.get("raw_text") or "")[:4000],
                            "stream_metadata": telemetry.get("stream_metadata") or {},
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
                    assistant_message=assistant_message or str(telemetry.get("raw_text") or ""),
                    parse_issue=str(telemetry.get("parse_issue") or "invalid_tool_envelope"),
                    prompt_snapshot=prompt,
                    steps=steps,
                )
            tool_name = str(envelope.get("tool_name") or "").strip() or "finish"
            tool_args = envelope.get("tool_args") or {}
            if tool_name == "finish":
                missing_commands = self._missing_requested_commands(
                    user_message=recent_user_message,
                    steps=steps,
                )
                if missing_commands:
                    append_session_event(
                        self.root,
                        session_id,
                        {
                            "type": "system_note",
                            "role": "system",
                            "content": f"完了がブロックされました: 要求されたコマンドがまだ実行されていません: {', '.join(missing_commands)}",
                            "code": "finish_blocked",
                            "reason_code": "missing_required_commands",
                            "details": {"missing_commands": list(missing_commands)},
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
                    append_session_event(
                        self.root,
                        session_id,
                        {
                            "type": "system_note",
                            "role": "system",
                            "content": f"完了がブロックされました: 期待される成果物が見つかりません: {', '.join(missing_artifacts)}",
                            "code": "finish_blocked",
                            "reason_code": "missing_expected_artifacts",
                            "details": {"missing_artifacts": list(missing_artifacts)},
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
                        append_session_event(
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
                    append_session_event(
                        self.root,
                        session_id,
                        {
                            "type": "system_note",
                            "role": "system",
                            "content": f"完了がブロックされました: {issue_text}",
                            "code": "finish_blocked",
                            "reason_code": finish_reason_code,
                            "details": {"issues": list(grounding_issues), "judge": judge_trace},
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
                append_session_event(
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
                    current_llm_workspace=None,
                    last_llm_workspace=str(turn_workspace),
                    last_error=None,
                    last_system_note=None,
                    current_started_at=None,
                    current_finished_at=now_iso(),
                    worker_running=self._worker_running(),
                )
                return {"ok": True, "session_id": session_id, "steps": steps, "final_answer": final_answer}
            append_session_event(
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
                redundant_reason = self._redundant_command_reason(tool_args=tool_args, steps=steps)
                if redundant_reason:
                    previous_result = (steps[-1].get("tool_result") if steps else {}) or {}
                    previous_failed = not bool(previous_result.get("ok"))
                    append_session_event(
                        self.root,
                        session_id,
                        {
                            "type": "system_note",
                            "role": "system",
                            "content": redundant_reason,
                            "code": "command_blocked",
                            "reason_code": "repeated_command",
                            "details": {"command": str(tool_args.get("command") or "").strip()},
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
                            current_llm_workspace=None,
                            last_llm_workspace=str(turn_workspace),
                            last_error=redundant_reason,
                            last_system_note=redundant_reason,
                            current_started_at=None,
                            current_finished_at=now_iso(),
                            worker_running=self._worker_running(),
                        )
                        return {
                            "ok": False,
                            "session_id": session_id,
                            "steps": steps,
                            "error": redundant_reason,
                        }
                    continue
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
                        selection=selection,
                        turn_id=turn_id,
                        queue_id=queue_id,
                        recent_user_message=recent_user_message,
                        prompt=prompt,
                        tool_name=tool_name,
                    ) if tool_name == "run_command" else None),
                )
            except Exception as exc:
                tool_result = {"ok": False, "tool": tool_name, "error": str(exc)}
            append_session_event(
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
            if tool_name == "run_command":
                append_session_event(
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
                    append_session_event(
                        self.root,
                        session_id,
                        {
                            "type": "system_note",
                            "role": "system",
                            "content": self._failed_command_guardrail(tool_result=tool_result),
                            "code": "command_failed",
                            "reason_code": "recovery_guidance",
                            "turn_id": turn_id,
                            "queue_id": queue_id,
                            "step_index": step_index,
                            "llm_workspace": str(turn_workspace),
                        },
            )
            steps.append({"tool_name": tool_name, "tool_result": tool_result})
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
                current_phase=self._current_phase(user_message=recent_user_message, steps=steps),
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
                    append_session_event(
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
                    return {"ok": False, "session_id": session_id, "steps": steps, "final_answer": final_answer, "error": error_text}
        append_session_event(
            self.root,
            session_id,
            {
                "type": "system_note",
                "role": "system",
                "content": "step limit reached before finish",
                "turn_id": turn_id,
                "queue_id": queue_id,
                "step_index": max_steps,
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
            current_llm_workspace=None,
            last_llm_workspace=str(turn_workspace),
            last_error="step limit reached",
            last_system_note="step limit reached before finish",
            current_started_at=None,
            current_finished_at=now_iso(),
            worker_running=self._worker_running(),
        )
        return {"ok": False, "session_id": session_id, "steps": steps, "error": "step limit reached"}

    def _make_tool_stream_updater(
        self,
        *,
        selection: dict[str, str],
        turn_id: str,
        queue_id: str,
        recent_user_message: str,
        prompt: str,
        tool_name: str,
    ) -> Any:
        def _update(partial: dict[str, Any]) -> None:
            preview = json.dumps(partial, ensure_ascii=False)[-4000:]
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

    def _missing_requested_commands(self, *, user_message: str, steps: list[dict[str, Any]]) -> list[str]:
        requested = self._extract_requested_commands(user_message)
        if not requested:
            return []
        executed = " ; ".join(
            str(((step.get("tool_result") or {}).get("command") or ""))
            for step in steps
            if step.get("tool_name") == "run_command"
        ).lower()
        missing = [command for command in requested if command.lower() not in executed]
        return missing

    def _expected_artifacts(self, text: str) -> list[str]:
        raw = str(text or "")
        if not raw.strip():
            return []
        candidates: list[str] = []
        for match in re.findall(r"`([^`]+)`", raw):
            candidate = str(match).strip()
            if "/" in candidate or "." in Path(candidate).name:
                candidates.append(candidate)
        for match in re.findall(r"\b([A-Za-z0-9_.-]+\.[A-Za-z0-9_.-]+)\b", raw):
            candidates.append(str(match).strip())
        deduped: list[str] = []
        for candidate in candidates:
            normalized = candidate.lstrip("./")
            if normalized and normalized not in deduped:
                deduped.append(normalized)
        return deduped

    def _missing_expected_artifacts(self, *, user_message: str) -> list[str]:
        expected = self._expected_artifacts(user_message)
        if not expected:
            return []
        missing: list[str] = []
        for rel in expected:
            candidate = (self.execution_root / rel).resolve()
            try:
                if os.path.commonpath([str(self.execution_root), str(candidate)]) != str(self.execution_root):
                    continue
            except ValueError:
                continue
            if not candidate.exists():
                missing.append(rel)
        return missing

    def _current_phase(self, *, user_message: str, steps: list[dict[str, Any]], recent_events: list[dict[str, Any]] = None) -> str:
        if self._deliberation_reasons(user_message=user_message, steps=steps, recent_events=recent_events):
            return "DELIBERATE"

        # Phase 6: Planning for complex tasks
        is_complex = any(kw in user_message.lower() for kw in ["implement", "fix", "refactor", "create", "design", "実装", "修正", "構造", "作成", "新設", "設計"])
        if is_complex and not steps:
            return "PLANNING"

        requested = self._extract_requested_commands(user_message)
        if requested and not steps:
            return "DISCOVER_REQUIRED_COMMANDS"
        if self._missing_requested_commands(user_message=user_message, steps=steps):
            return "EXECUTE_MISSING_COMMANDS"
        if any(str(step.get("tool_name") or "") == "run_command" for step in steps):
            return "SYNTHESIZE_FROM_EVIDENCE"
        return "FINISH"

    def _deliberation_reasons(self, *, user_message: str, steps: list[dict[str, Any]], recent_events: list[dict[str, Any]] = None) -> list[str]:
        reasons = []
        # 1. Step count threshold
        if len(steps) >= 10: # Increased threshold as complex tasks need more steps
            reasons.append(f"ステップ数が上限（{len(steps)}/12）に近づいています")

        # 2. Consecutive finish blocks (checking recent events)
        if recent_events:
            consecutive_blocks = 0
            for event in reversed(recent_events):
                if event.get("type") == "system_note" and "完了がブロックされました" in str(event.get("content") or ""):
                    consecutive_blocks += 1
                elif event.get("type") == "assistant_message" or event.get("type") == "tool_result":
                    # Only count if no progress was made between blocks
                    continue
                elif event.get("type") == "operation" and event.get("status") == "running":
                    break
            if consecutive_blocks >= 2:
                reasons.append(f"完了が{consecutive_blocks}回連続でブロックされました")

        # 3. Overall command failures
        failed_count = sum(1 for step in steps if step.get("tool_name") == "run_command" and not bool((step.get("tool_result") or {}).get("ok")))
        if failed_count >= 3:
            reasons.append(f"コマンド実行が合計{failed_count}回失敗しています")
        elif len(steps) >= 2:
            last_results = [step.get("tool_result") or {} for step in steps[-2:]]
            if all(not bool(res.get("ok")) for res in last_results) and all(step.get("tool_name") == "run_command" for step in steps[-2:]):
                reasons.append("コマンド実行が連続して失敗しています")

        # 4. Stagnation: Repeated tool outputs without finding target
        if len(steps) >= 3:
            last_three = steps[-3:]
            tools = [s.get("tool_name") for s in last_three]
            results = [json.dumps(s.get("tool_result") or {}, sort_keys=True) for s in last_three]
            if len(set(tools)) == 1 and tools[0] in {"list_files", "search_code", "read_file"}:
                if len(set(results)) == 1:
                    reasons.append(f"ツール（{tools[0]}）で同じ結果を繰り返しています")

            # 5. Exact command duplication
            if len(set(tools)) == 1 and tools[0] == "run_command":
                commands = [str((s.get("tool_result") or {}).get("command") or "") for s in last_three]
                if len(set(commands)) == 1 and commands[0]:
                    reasons.append(f"まったく同じコマンド（{commands[0]}）を再実行しています")

        # 6. Missing commands persisting
        missing_commands = self._missing_requested_commands(user_message=user_message, steps=steps)
        if missing_commands and len(steps) >= 4:
            reasons.append(f"要求されたコマンド（{', '.join(missing_commands)}）が未実行のままステップを消費しています")

        return reasons

    def _fast_path_envelope(
        self,
        *,
        step_index: int,
        selection: dict[str, str],
        user_message: str,
        extra_prompt: str | None,
        recent_events: list[dict[str, Any]],
        steps: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        if str(selection.get("role") or "") != "terminal":
            return None
        if any(str(event.get("type") or "") == "finish" for event in recent_events[-8:]):
            return None
        missing = self._missing_requested_commands(user_message=user_message, steps=steps)
        if not missing:
            return None
        command = missing[0]
        shell_name = self._preferred_shell_from_extra_prompt(extra_prompt)
        blocked_reason = self._redundant_command_reason(
            tool_args={"command": command, "shell": shell_name},
            steps=steps,
        )
        if blocked_reason is not None:
            return None
        action_note = (
            "高速パス（起動）: 最初に要求されたコマンドを即座に実行します。"
            if step_index == 1
            else "高速パス（継続）: 次の未実行コマンドをモデルに渡さず続けて実行します。"
        )
        return {
            "analysis": action_note,
            "assistant_message": f"Executing '{command}' command...",
            "tool_name": "run_command",
            "tool_args": {"command": command, "shell": shell_name},
        }

    def _preferred_shell_from_extra_prompt(self, extra_prompt: str | None) -> str:
        text = str(extra_prompt or "")
        match = re.search(r"shell='([^']+)'", text)
        if match:
            return str(match.group(1) or "auto")
        return "auto"

    def _grounding_issues(self, *, user_message: str, final_answer: str, steps: list[dict[str, Any]]) -> list[str]:
        issues: list[str] = []
        executed_commands = [
            str(((step.get("tool_result") or {}).get("command") or "")).strip()
            for step in steps
            if step.get("tool_name") == "run_command"
        ]
        evidence_text = "\n".join(
            json.dumps(step.get("tool_result") or {}, ensure_ascii=False)
            for step in steps
        ).lower()
        for command in self._extract_requested_commands(final_answer):
            normalized = command.strip().rstrip(":;,")
            if not normalized:
                continue
            if normalized.lower() not in " ; ".join(executed_commands).lower():
                issues.append(f"最終回答に未実行のコマンドが含まれています: {normalized}")

        # Semantic Grounding Check (LLM-as-a-Judge)
        if final_answer and not self._semantic_grounding_check(final_answer=final_answer, evidence_text=evidence_text, user_message=user_message):
            issues.append("最終回答が事実に基づいていない、または証拠から逸脱しています。")

        return issues

    def _semantic_grounding_check(self, *, final_answer: str, evidence_text: str, user_message: str) -> bool:
        model = str(self.router.models.get("fast") or "fast")
        options = {"temperature": 0.1, "num_predict": 256}
        prompt = (
            "あなたは事実確認のエキスパートです。以下の証拠（Evidence）に基づき、提出された回答（Final Answer）が事実に基づいているか判定してください。\n\n"
            "【判定基準】\n"
            "- 回答内の具体的な数値、名称、引用内容が証拠に含まれているか、あるいは証拠から論理的に導き出せる場合は合格です。\n"
            "- ユーザーの依頼文（User Message）に元々含まれている内容も合格です。\n"
            "- 証拠に全く存在しない新しい事実を捏造している場合は不合格（NG）です。\n"
            "- 表現が多少異なっていても、事実関係が合っていれば合格です。\n"
            "- ただし、ユーザーの依頼が一般的な知識、挨拶、創作、または推論のみで完結するタスクであり、環境状態の調査を必要としない場合は、Evidenceが空であっても自身の知識に合致していれば例外的に合格としてください。\n\n"
            f"【User Message】: {user_message}\n\n"
            f"【Evidence】:\n{evidence_text[:8000]}\n\n"
            f"【Final Answer】:\n{final_answer}\n\n"
            "次のJSONだけを返してください。Markdown、説明文、前置きは禁止です。\n"
            '{"verdict":"ok または ng","reason_code":"supported|unsupported_claim|insufficient_evidence|general_knowledge_ok","unsupported_claims":["証拠にない主張"],"rationale":"短い理由"}'
        )
        self._last_grounding_judge_trace = {
            "model": model,
            "prompt": prompt,
            "user_message": user_message,
            "evidence_text": evidence_text[:8000],
            "final_answer": final_answer,
            "options": dict(options),
            "decision": "not_run",
        }
        try:
            response = self.llm_backend.chat(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                options=options,
                timeout_seconds=int(self.runtime_config.get("chat_timeout_seconds") or 180),
            )
            raw_content = str(response.get("content") or "")
            content_text = str(response.get("content_text") or "")
            thinking_text = str(response.get("thinking_text") or "")
            parse_source = content_text or raw_content
            parsed = self._parse_grounding_judge_payload(parse_source)
            verdict = str(parsed.get("verdict") or "").strip().lower() if parsed else ""
            if verdict == "ok":
                decision = "ok"
            elif verdict == "ng":
                decision = "ng"
            elif not parse_source.strip() and thinking_text.strip():
                decision = "empty_output"
            elif parsed is None:
                decision = "invalid_json"
            else:
                decision = "invalid_output"
            self._last_grounding_judge_trace = {
                **(self._last_grounding_judge_trace or {}),
                "response_model": response.get("model", model),
                "raw_response": raw_content,
                "content_text": content_text,
                "thinking_text": thinking_text,
                "decision": decision,
                "parsed": parsed,
                "verdict": verdict,
            }
            return decision == "ok"
        except Exception as exc:
            self._last_grounding_judge_trace = {
                **(self._last_grounding_judge_trace or {}),
                "decision": "error",
                "error": str(exc),
            }
            return False

    def _parse_grounding_judge_payload(self, text: str) -> dict[str, Any] | None:
        candidate = self._extract_json_object(str(text or ""))
        if candidate is None:
            return None
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict):
            return None
        return parsed

    def _maybe_record_observer_note(
        self,
        *,
        session_id: str,
        turn_id: str,
        queue_id: str,
        step_index: int,
        user_message: str,
        tool_name: str,
        tool_result: dict[str, Any],
        steps: list[dict[str, Any]],
        prompt_snapshot: str,
        assistant_message: str,
    ) -> None:
        if not self._observer_enabled():
            return
        model = str(self.runtime_config.get("observer_model") or self.router.models.get("fast") or "fast")
        options = dict(self.runtime_config.get("observer_options") or {"temperature": 0.2, "num_predict": 220})
        step_summary = {
            "tool_name": tool_name,
            "tool_result": tool_result,
            "step_count": len(steps),
            "latest_llm_message": assistant_message[-2000:],
            "context_excerpt": prompt_snapshot[-3000:],
        }
        prompt = (
            "あなたはAIエージェント研究開発のエキスパートであり、P3実験の実況解説者です。"
            "システムとLLMのやりとりを観察し、直近ステップで何が起きたのか、"
            "それがAIエージェントの設計・制御・失敗解析の観点でどう見えるのかを日本語で解説してください。"
            "特に、LLMが失敗しそうな出力をした場合は、なぜそうなったかを、渡されたコンテキスト、直近のLLM応答、"
            "ツール結果の不足、指示の衝突や混入の観点から点検してください。\n"
            "現段階では介入や指示はせず、観測と解説だけを行ってください。\n"
            "次の4点だけを簡潔に書いてください: 1. 起きたこと 2. 失敗要因の仮説 3. コンテキスト点検 4. 次に確認すべきこと。\n\n"
            f"ユーザー依頼: {user_message}\n\n"
            f"直近ステップ:\n{json.dumps(step_summary, ensure_ascii=False)[:6000]}"
        )
        try:
            response = self.llm_backend.chat(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                options=options,
                timeout_seconds=60,
            )
            content = str(response.get("content_text") or response.get("content") or "").strip()
            thinking_text = str(response.get("thinking_text") or "")
            if not self._usable_japanese_observer_text(content):
                content = self._deterministic_step_commentary(
                    user_message=user_message,
                    tool_name=tool_name,
                    tool_result=tool_result,
                    step_count=len(steps),
                    prompt_snapshot=prompt_snapshot,
                    assistant_message=assistant_message,
                    raw_observer_output=content or thinking_text,
                )
            append_session_event(
                self.root,
                session_id,
                {
                    "type": "observer_note",
                    "role": "observer",
                    "content": content,
                    "model": response.get("model", model),
                    "code": "live_commentator",
                    "reason_code": "step_commentary",
                    "details": {
                        "prompt": prompt,
                        "raw_response": str(response.get("content") or ""),
                        "content_text": str(response.get("content_text") or ""),
                        "thinking_text": thinking_text,
                    },
                    "turn_id": turn_id,
                    "queue_id": queue_id,
                    "step_index": step_index,
                },
            )
        except Exception as exc:
            append_session_event(
                self.root,
                session_id,
                {
                    "type": "observer_note",
                    "role": "observer",
                    "content": f"監視者の解説生成に失敗しました: {exc}",
                    "code": "live_commentator",
                    "reason_code": "observer_error",
                    "turn_id": turn_id,
                    "queue_id": queue_id,
                    "step_index": step_index,
                },
            )

    def _usable_japanese_observer_text(self, content: str) -> bool:
        text = str(content or "").strip()
        if not text:
            return False
        if any(
            marker in text
            for marker in (
                "Analyze the",
                "User Request",
                "R&D Insight",
                "**Role:**",
                "**Task:**",
                "Here's a thinking process",
                "Deconstruct the Log",
                "Input Log:",
                "Output Format:",
            )
        ):
            return False
        japanese_chars = sum(1 for char in text if "\u3040" <= char <= "\u30ff" or "\u4e00" <= char <= "\u9fff")
        return japanese_chars >= 8

    def _deterministic_step_commentary(
        self,
        *,
        user_message: str,
        tool_name: str,
        tool_result: dict[str, Any],
        step_count: int,
        prompt_snapshot: str = "",
        assistant_message: str = "",
        raw_observer_output: str = "",
    ) -> str:
        ok = bool(tool_result.get("ok"))
        result_summary = "成功" if ok else "失敗"
        target = str(tool_result.get("path") or tool_result.get("command") or tool_name)
        extra = ""
        if raw_observer_output:
            extra = " なお、解説LLMは日本語本文ではなく思考過程または英語分析を返したため、システム側で日本語解説に置き換えています。"
        context_risks = self._context_risk_summary(prompt_snapshot=prompt_snapshot, assistant_message=assistant_message)
        return (
            f"1. 起きたこと: ユーザー依頼「{user_message[:80]}」に対して、STEP{step_count}で `{tool_name}` を実行し、結果は{result_summary}でした。対象は `{target}` です。\n"
            f"2. 失敗要因の仮説: {context_risks} LLMの発話だけでなく、システムは tool_result を時系列証拠として記録しています。ここではツール結果が次の判断材料になります。{extra}\n"
            f"3. コンテキスト点検: 渡されたコンテキストには直近プロンプト、LLM応答、tool_result が含まれます。LLM応答が予定や未実行コマンドを完了のように扱っていないかを確認する必要があります。\n"
            "4. 次に確認すべきこと: このステップの結果だけで完了できるか、または次のツール実行やシステム判定が必要かを確認します。"
        )

    def _context_risk_summary(self, *, prompt_snapshot: str, assistant_message: str) -> str:
        prompt_text = str(prompt_snapshot or "")
        assistant_text = str(assistant_message or "")
        risks: list[str] = []
        if len(prompt_text) > 12000:
            risks.append("プロンプトが長く、重要な制約や証拠が埋もれた可能性があります。")
        if "次のJSONだけ" in prompt_text and "```" in assistant_text:
            risks.append("JSONのみ要求とMarkdownコードブロックが衝突し、形式違反を誘発した可能性があります。")
        if "未実行" in prompt_text or "完了がブロックされました" in prompt_text:
            risks.append("過去のブロック通知が文脈に残り、LLMがエラー説明に引っ張られた可能性があります。")
        if "run_command" in prompt_text and "python3" in assistant_text and "tool_name" not in assistant_text:
            risks.append("LLMが実行すべきコマンドを文章で述べ、ツール呼び出しへ変換できていない可能性があります。")
        if not risks:
            risks.append("明確なコンテキスト異常は見えませんが、証拠不足とタスク未完了を分けて見る必要があります。")
        return " ".join(risks)

    def _record_observer_judgement_note(
        self,
        *,
        session_id: str,
        turn_id: str,
        queue_id: str,
        step_index: int,
        user_message: str,
        assistant_message: str,
        system_decision: str,
        reason_code: str,
        prompt_snapshot: str = "",
        steps: list[dict[str, Any]] | None = None,
    ) -> None:
        if not self._observer_enabled():
            return
        context_risks = self._context_risk_summary(prompt_snapshot=prompt_snapshot, assistant_message=assistant_message)
        evidence_summary = json.dumps((steps or [])[-3:], ensure_ascii=False)[:2000]
        append_session_event(
            self.root,
            session_id,
            {
                "type": "observer_note",
                "role": "observer",
                "content": (
                    f"1. 入力とLLM応答: ユーザー依頼は「{user_message[:80]}」です。LLMはSTEP{step_index}で、完了または次の行動に進もうとする回答を出しました。\n"
                    f"2. システム判定: {system_decision}\n"
                    f"3. 失敗要因の仮説: {context_risks}\n"
                    f"4. コンテキスト点検: reason_code は `{reason_code}` です。直近証拠は「{evidence_summary}」です。未実行コマンドやjudge出力不正がある場合、完了を止める判断自体は妥当です。ただし judge がJSON判定を返せなかったケースは、根拠不足そのものではなく判定器出力の問題として分けて見るべきです。"
                ),
                "code": "live_commentator",
                "reason_code": "system_judgement_commentary",
                "details": {
                    "assistant_message": assistant_message[:4000],
                    "system_decision": system_decision,
                    "system_reason_code": reason_code,
                    "context_excerpt": prompt_snapshot[-4000:],
                    "recent_steps": (steps or [])[-3:],
                },
                "turn_id": turn_id,
                "queue_id": queue_id,
                "step_index": step_index,
            },
        )

    def _record_observer_llm_output_issue_note(
        self,
        *,
        session_id: str,
        turn_id: str,
        queue_id: str,
        step_index: int,
        user_message: str,
        assistant_message: str,
        parse_issue: str,
        prompt_snapshot: str,
        steps: list[dict[str, Any]],
    ) -> None:
        if not self._observer_enabled():
            return
        context_risks = self._context_risk_summary(prompt_snapshot=prompt_snapshot, assistant_message=assistant_message)
        append_session_event(
            self.root,
            session_id,
            {
                "type": "observer_note",
                "role": "observer",
                "content": (
                    f"1. 起きたこと: STEP{step_index}でLLM応答は返りましたが、システムが期待する tool_call JSON として解釈できませんでした。parse_issue は `{parse_issue}` です。\n"
                    f"2. 失敗要因の仮説: {context_risks}\n"
                    f"3. コンテキスト点検: ユーザー依頼は「{user_message[:100]}」です。直近の実行済みステップは {len(steps)} 件で、LLM応答は「{assistant_message[:300]}」から始まっています。文章で計画を述べるだけになっていないか、JSON-only 指示とMarkdown/長文コードが衝突していないかを確認します。\n"
                    "4. 次に確認すべきこと: この応答はツール実行に進んでいないため、次のシステム判定で未実行コマンドや証拠不足としてブロックされる可能性があります。"
                ),
                "code": "live_commentator",
                "reason_code": "llm_output_issue_commentary",
                "details": {
                    "parse_issue": parse_issue,
                    "assistant_message": assistant_message[:4000],
                    "context_excerpt": prompt_snapshot[-4000:],
                    "recent_steps": steps[-3:],
                },
                "turn_id": turn_id,
                "queue_id": queue_id,
                "step_index": step_index,
            },
        )

    def _normalize_run_command_evidence(self, steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for step in steps:
            if str(step.get("tool_name") or "") != "run_command":
                continue
            payload = step.get("tool_result") or {}
            if not isinstance(payload, dict):
                continue
            rows.append(
                {
                    "command": str(payload.get("command") or ""),
                    "ok": bool(payload.get("ok")),
                    "returncode": payload.get("returncode"),
                    "cwd": str(payload.get("cwd") or ""),
                    "stdout": str(payload.get("stdout") or "")[-1500:],
                    "stderr": str(payload.get("stderr") or "")[-800:],
                }
            )
        return rows

    def _output_preview(self, text: str, *, max_lines: int, max_chars: int) -> str:
        lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
        if not lines:
            return ""
        if len(lines) <= max_lines:
            selected = lines
        else:
            if max_lines <= 1:
                selected = [lines[0]]
            else:
                selected = []
                last_index = len(lines) - 1
                for offset in range(max_lines):
                    raw_index = round((last_index * offset) / (max_lines - 1))
                    item = lines[raw_index]
                    if not selected or selected[-1] != item:
                        selected.append(item)
        preview = " / ".join(selected)
        return preview[:max_chars].strip()

    def _controller_terminal_finish(
        self,
        *,
        selection: dict[str, str],
        goal_text: str,
        user_message: str,
        steps: list[dict[str, Any]],
    ) -> str | None:
        if str(selection.get("role") or "") != "terminal":
            return None
        if self._missing_requested_commands(user_message=user_message, steps=steps):
            return None
        if not any(str(step.get("tool_name") or "") == "run_command" for step in steps):
            return None
        final_answer = self._deterministic_terminal_final_answer(
            goal_text=goal_text,
            user_message=user_message,
            steps=steps,
        )
        if not final_answer:
            return None
        evidence_text = "\n".join(
            json.dumps(step.get("tool_result") or {}, ensure_ascii=False)
            for step in steps
        ).lower()
        if self._terminal_answer_is_direct_evidence(final_answer=final_answer, steps=steps):
            return final_answer
        if not self._semantic_grounding_check(final_answer=final_answer, evidence_text=evidence_text, user_message=user_message):
            return None
        return final_answer

    def _terminal_answer_is_direct_evidence(self, *, final_answer: str, steps: list[dict[str, Any]]) -> bool:
        if not final_answer:
            return False
        for step in steps:
            if str(step.get("tool_name") or "") != "run_command":
                continue
            payload = step.get("tool_result") or {}
            if not bool(payload.get("ok")):
                continue
            stdout = str(payload.get("stdout") or "").strip()
            command = str(payload.get("command") or "").strip()
            stdout_preview = self._output_preview(stdout, max_lines=12, max_chars=700)
            if stdout and stdout[:200] in final_answer:
                return True
            if stdout_preview and stdout_preview in final_answer:
                return True
            if command and command in final_answer and stdout:
                return True
        return False

    def _deterministic_terminal_final_answer(self, *, goal_text: str, user_message: str, steps: list[dict[str, Any]]) -> str | None:
        evidence = self._normalize_run_command_evidence(steps)
        if not evidence:
            return None
        goal = (goal_text or user_message).strip()
        parts: list[str] = []
        for row in evidence[-4:]:
            command = str(row.get("command") or "").strip()
            stdout_preview = self._output_preview(str(row.get("stdout") or ""), max_lines=12, max_chars=700)
            stderr_preview = self._output_preview(str(row.get("stderr") or ""), max_lines=2, max_chars=160)
            if bool(row.get("ok")):
                if stdout_preview:
                    parts.append(f"{command}: {stdout_preview}")
                else:
                    cwd = str(row.get("cwd") or "").strip()
                    suffix = f" cwd={cwd}" if cwd else "success"
                    parts.append(f"{command}: {suffix}".strip())
            else:
                failure_detail = stderr_preview or f"returncode={row.get('returncode')}"
                parts.append(f"{command}: failed ({failure_detail})")
        if not parts:
            return None
        if len(parts) == 1:
            first = parts[0]
            if ":" in first:
                _, detail = first.split(":", 1)
                return detail.strip()[:1200]
        if len(parts) == 2:
            return " / ".join(parts)[:1200]
        lead = f"{goal}: " if goal else ""
        return (lead + " / ".join(parts))[:1200]

    def _synthesize_terminal_final_answer(self, *, goal_text: str, user_message: str, steps: list[dict[str, Any]]) -> str | None:
        deterministic = self._deterministic_terminal_final_answer(
            goal_text=goal_text,
            user_message=user_message,
            steps=steps,
        )
        if deterministic:
            return deterministic
        evidence = self._normalize_run_command_evidence(steps)
        if not evidence:
            return None
        model = str(self.router.models.get("fast") or self.config.get("models", {}).get("fast") or "glm-4.7-flash:latest")
        options = dict(self.ollama_options.get("fast", {}))
        prompt = (
            "あなたはターミナル実行結果の要約を担当します。\n"
            "証拠（evidence）に基づいた短い回答をプレーンテキストで返してください。\n"
            "ユーザーの目標と以下の証拠のみを使用してください。\n"
            "証拠に存在しない事実は絶対に含めないでください。\n"
            "証拠が不足している場合は、その旨を簡潔に伝えてください。\n\n"
            f"ユーザーの目標:\n{(goal_text or user_message).strip()}\n\n"
            f"正規化された証拠:\n{json.dumps(evidence, ensure_ascii=False, indent=2)}\n"
        )
        try:
            reply = self.llm_backend.chat(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                options=options,
                timeout_seconds=int(self.runtime_config.get("chat_timeout_seconds") or 180),
            )
            text = str(reply.get("content") or "").strip()
            return text or self._deterministic_terminal_final_answer(
                goal_text=goal_text,
                user_message=user_message,
                steps=steps,
            )
        except Exception:
            return self._deterministic_terminal_final_answer(
                goal_text=goal_text,
                user_message=user_message,
                steps=steps,
            )

    def _failed_command_guardrail(self, *, tool_result: dict[str, Any]) -> str:
        command = str(tool_result.get("command") or "").strip()
        returncode = tool_result.get("returncode")
        stderr = str(tool_result.get("stderr") or tool_result.get("error") or "").strip()
        stderr_preview = stderr[:240] if stderr else "no stderr"
        return (
            f"リカバリモード：直前のコマンド `{command}` が失敗しました（終了コード={returncode}）。"
            "同じ引数での再試行は避け、エラー理由を分析した上でアプローチを変更してください。"
            f" エラー詳細：{stderr_preview}"
        )

    def _redundant_command_reason(self, *, tool_args: dict[str, Any], steps: list[dict[str, Any]]) -> str | None:
        command = str(tool_args.get("command") or "").strip()
        shell_name = str(tool_args.get("shell") or "")
        if not command or not steps:
            return None
        previous = steps[-1]
        if previous.get("tool_name") != "run_command":
            return None
        payload = previous.get("tool_result") or {}
        if not isinstance(payload, dict):
            return None
        previous_command = str(payload.get("command") or "").strip()
        previous_shell = str(payload.get("shell") or "")
        same_command = previous_command == command and (not shell_name or previous_shell == shell_name)
        if same_command and bool(payload.get("ok")):
            return f"重複コマンドの実行をブロックしました: {command}。既存の tool_result を使用するか、別のステップを選択してください。"
        if same_command and not bool(payload.get("ok")):
            return f"失敗した重複コマンドの実行をブロックしました: {command}。引数を変更するか、新しい証拠を収集してから再試行してください。"
        return None

    def _extract_requested_commands(self, text: str) -> list[str]:
        known_heads = {"pwd", "ls", "rg", "find", "cat", "sed", "head", "tail", "python", "python3", "pytest", "git", "$psversiontable.psversion.tostring()"}
        patterns = [
            r"([A-Za-z0-9_.:/$-]+(?:\s+[A-Za-z0-9_.$:/=-]+)*)\s*を実行",
            r"`([^`]+)`",
            r"'([^']+)'",
            r"\"([^\"]+)\"",
        ]
        commands: list[str] = []
        fragments = re.split(r"[、。\n]|その後|続けて|そして|then|and then", text)
        for fragment in fragments:
            clean = str(fragment).strip()
            if not clean:
                continue
            for pattern in patterns:
                for match in re.findall(pattern, clean):
                    candidate = str(match).strip()
                    if not candidate:
                        continue
                    head = candidate.split()[0].lower()
                    if head in known_heads:
                        commands.append(candidate)
            tokens = clean.split()
            for index, token in enumerate(tokens):
                head = token.lower()
                if head not in known_heads:
                    continue
                candidate = token
                if head == "git" and index + 1 < len(tokens):
                    candidate = f"{token} {tokens[index + 1]}"
                commands.append(candidate)
        deduped: list[str] = []
        for command in commands:
            if command not in deduped:
                deduped.append(command)
        return deduped

    def _system_prompt(self) -> str:
        return (
            "あなたは P3、ローカルエージェントランタイムです。"
            "あなたの仕事は、一度に一つのツールを選択し、ツールの実行結果を新しい証拠（evidence）として活用しながら、ユーザーの目標を達成することです。"
            "客観的な完了が確認されるまで停止しないでください。"
            "出力は必ず JSON 形式とし、キーは analysis, assistant_message, tool_name, tool_args としてください。"
            "tool_args は短く保ってください。長いコード全文を JSON に詰め込まず、新規ファイルは write_file で小さく開始して append_file で分割し、既存ファイルは replace_text で最小差分を適用してください。"
            "利用可能なツール:\n"
            f"{self.tools.describe_for_prompt()}\n"
            "finish はタスクが完了した際、またはこれ以上のツール実行が不要で最善の最終回答を出す際にのみ使用してください。"
            "tool_result に存在しない事実を主張しないでください。"
            "ユーザーが特定のコマンドの実行を要求した場合は、それらを一つずつ実行してから finish してください。"
            "コードベースを探索する際は、広範な読み取りを行う前に search_code による検索を優先してください。"
        )

    def _build_prompt(
        self,
        *,
        goal_text: str,
        recent_events: list[dict[str, Any]],
        extra_prompt: str | None = None,
        steps: list[dict[str, Any]] = None,
        current_phase: str = "FINISH",
        user_message: str = "",
    ) -> str:
        rendered_events = self._render_action_context_events(recent_events=recent_events, steps=steps or [], user_message=user_message)
        goal_part = goal_text.strip() or "(目標が設定されていません)"
        prompt = (
            f"現在の目標:\n{goal_part}\n\n"
            f"現在のユーザー依頼:\n{user_message.strip() or '(直近の依頼なし)'}\n\n"
            f"LLM作業ディレクトリ:\n{self.execution_root}\n"
            "このターンのファイル操作とコマンド実行は、この専用 workspace 配下で行われます。相対パスを使ってください。\n\n"
            "直近のセッションイベント:\n"
            + ("\n".join(rendered_events) if rendered_events else "(履歴なし)")
            + "\n\n直近のリフレクション (失敗からの教訓):\n"
            + self._reflection_prompt_block()
            + "\n\n編集方針:\n"
            "長いコード全文を1回の JSON tool_args に入れないでください。"
            "新規ファイルは小さな write_file で開始し、append_file で2000バイト以下の塊を追加してください。"
            "既存ファイルは read_file で確認し、replace_text で必要箇所だけ変更してください。"
        )
        if current_phase == "DELIBERATE":
            prompt += "\n\n" + self._build_deliberation_note(user_message=user_message, steps=steps or [], recent_events=recent_events)
        elif current_phase == "PLANNING":
            prompt += (
                "\n\n【計画フェーズ（PLANNING）】\n"
                "複雑なタスクが開始されました。最初のアクションとして、焦って修正や実行を行うのではなく、"
                "まずは関連するファイル構成の確認（list_files）や、コードの検索（search_code）を行い、"
                "現状の把握に努めてください。その後、段階的な実行計画を立ててください。"
            )

        prompt += "\n\n最適と思われる次の一手を決定してください。"
        if extra_prompt:
            prompt += f"\n\n実行の優先設定:\n{extra_prompt.strip()}"
        return prompt

    def _render_action_context_events(
        self,
        *,
        recent_events: list[dict[str, Any]],
        steps: list[dict[str, Any]],
        user_message: str,
    ) -> list[str]:
        current_user = str(user_message or "").strip()
        useful_types = {"user_message", "tool_call", "tool_result", "system_note", "planning_note"}
        useful_system_codes = {
            "",
            "llm_output_issue",
            "finish_blocked",
            "command_failed",
            "command_blocked",
            "controller_finish",
            "grounding_judge",
        }
        selected: list[dict[str, Any]] = []
        for event in recent_events:
            event_type = str(event.get("type") or "")
            if event_type not in useful_types:
                continue
            if event_type == "user_message" and current_user and str(event.get("content") or "").strip() != current_user:
                continue
            if event_type == "system_note" and str(event.get("code") or "") not in useful_system_codes:
                continue
            selected.append(event)
        selected = selected[-12:]
        rendered: list[str] = []
        for event in selected:
            event_type = str(event.get("type") or "")
            line = f"[{event_type}] "
            if event_type in {"user_message", "system_note", "planning_note"}:
                line += self._compact_context_text(str(event.get("content") or ""), limit=700)
            elif event_type == "tool_call":
                tool_args = dict(event.get("tool_args") or {})
                if "content" in tool_args:
                    tool_args["content"] = self._compact_context_text(str(tool_args.get("content") or ""), limit=220)
                if "new_text" in tool_args:
                    tool_args["new_text"] = self._compact_context_text(str(tool_args.get("new_text") or ""), limit=220)
                line += f"{event.get('tool_name')} args={json.dumps(tool_args, ensure_ascii=False)}"
            elif event_type == "tool_result":
                line += f"{event.get('tool_name')} -> {self._compact_context_text(str(event.get('content') or ''), limit=1000)}"
            rendered.append(line)
        if steps:
            rendered.append("[current_steps] " + self._compact_context_text(json.dumps(steps[-4:], ensure_ascii=False), limit=1400))
        return rendered

    def _compact_context_text(self, text: str, *, limit: int) -> str:
        clean = re.sub(r"\s+", " ", str(text or "")).strip()
        if len(clean) <= limit:
            return clean
        return f"{clean[:limit]}... [truncated {len(clean) - limit} chars]"

    def _build_planning_note(self, *, user_message: str, goal_text: str) -> str:
        requested_commands = self._extract_requested_commands(user_message)
        if requested_commands:
            return (
                "計画: 要求されたコマンドを一つずつ実行し、各結果を確認してから次へ進みます。"
                "すべての要求がカバーされるまで完了しません。"
            )
        haystack = f"{goal_text}\n{user_message}".lower()
        if any(token in haystack for token in {"code", "file", "search", "コード", "ファイル", "検索"}):
            return (
                "計画: まず関連領域を検索または一覧表示し、必要なファイルのみを読み取ります。"
                "完了前に1ステップにつき1つの最小限の変更または結論を出します。"
            )
        return "計画: 最新の証拠を確認し、具体的な次のアクションを一つ実行して結果を検証します。その上で完了が妥当かどうかを判断します。"

    def _build_deliberation_note(self, *, user_message: str, steps: list[dict[str, Any]], recent_events: list[dict[str, Any]]) -> str:
        reasons = self._deliberation_reasons(user_message=user_message, steps=steps, recent_events=recent_events)
        reason_text = "、".join(reasons)
        return (
            f"【熟考指示（DELIBERATE）】\n"
            f"重要：現在、プロセスの停滞が検知されています（理由：{reason_text}）。\n\n"
            "これまでの記録を冷静に分析し、なぜシステムに拒否されたのか、あるいはなぜ目的が未達成なのかを特定してください。"
            "単に同じアプローチを繰り返すのではなく、原因（例：余計なメタ情報の出力、ルートの誤りなど）を取り除いた新しい実行計画を立ててください。"
        )

    def _reflection_prompt_block(self) -> str:
        rows = read_jsonl(self.paths.reflections_path, limit=3)
        if not rows:
            return "(直近のリフレクションはありません)"
        lines = []
        for row in rows:
            reflection = str(row.get("reflection") or "").strip()
            if not reflection:
                continue
            failure_class = str(row.get("failure_class") or "").strip()
            prefix = f"[{failure_class}] " if failure_class else ""
            lines.append(prefix + reflection)
        return "\n".join(lines[-3:]) if lines else "(直近のリフレクションはありません)"

    def _classify_failure(
        self,
        *,
        reason: str,
        steps: list[dict[str, Any]],
    ) -> str:
        clean = str(reason or "").lower()
        if "shell unavailable" in clean or "pwsh" in clean:
            return "shell_unavailable"
        if "expected artifact" in clean:
            return "missing_expected_artifact"
        if "unsupported detail" in clean:
            return "unsupported_detail"
        if "requested commands not yet executed" in clean or "command coverage" in clean:
            return "missing_command_coverage"
        if "step limit reached" in clean or "before finish" in clean or "missing finish" in clean:
            return "missing_finish"
        if "repeated failed command blocked" in clean or "repeated command blocked" in clean:
            return "repeated_command"
        recent_tools = [str(step.get("tool_name") or "") for step in steps[-3:]]
        if recent_tools.count("run_command") >= 2:
            return "repeated_command"
        return "generic_failure"

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
        append_session_event(
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
        return payload

    def _chat_with_repair(self, *, role: str, model: str, prompt: str, session_id: str | None = None) -> dict[str, Any]:
        timeout_seconds = int(self.runtime_config.get("chat_timeout_seconds") or 180)
        retry_limit = int(self.runtime_config.get("json_retry_limit") or 0)
        options = dict(self.ollama_options.get(role, {}))
        started_at = time.time()
        started_iso = now_iso()
        last_raw = ""
        attempt_count = 0
        stream_metadata: dict[str, Any] = {}

        # Action/tool mode gets a curated prompt from _build_prompt. Replaying the
        # full chat history here reintroduces stale failures and unrelated tasks.
        del session_id
        messages = [
            {"role": "system", "content": self._system_prompt()},
            {"role": "user", "content": prompt},
        ]
        for attempt_index in range(retry_limit + 1):
            attempt_count = attempt_index + 1
            stream_metadata = {}
            if hasattr(self.llm_backend, "iter_chat_stream"):
                chunks = self.llm_backend.iter_chat_stream(
                    model=model,
                    messages=messages,
                    options=options,
                    timeout_seconds=timeout_seconds,
                )
                stream_parts: list[str] = []
                for chunk in chunks:
                    stream_metadata = self._extract_stream_metadata(chunk, previous=stream_metadata)
                    message = chunk.get("message") or {}
                    delta_content = str(message.get("content") or "")
                    delta_thinking = str(message.get("thinking") or "")
                    delta = f"{delta_thinking}{delta_content}"
                    if not delta:
                        continue
                    stream_parts.append(delta)
                    current_stream = "".join(stream_parts)
                    self._write_runtime_status(
                        status="running",
                        current_role=role,
                        current_model=model,
                        current_stream_text=current_stream[-4000:],
                        worker_running=self._worker_running(),
                    )
                last_raw = "".join(stream_parts)
            elif hasattr(self.llm_backend, "chat_stream"):
                # Some fake/test backends expose chat_stream as a buffered list.
                # Real Ollama streaming should use iter_chat_stream above so live
                # output is updated while tokens arrive.
                chunks = self.llm_backend.chat_stream(
                    model=model,
                    messages=messages,
                    options=options,
                    timeout_seconds=timeout_seconds,
                )
                stream_parts: list[str] = []
                for chunk in chunks:
                    stream_metadata = self._extract_stream_metadata(chunk, previous=stream_metadata)
                    message = chunk.get("message") or {}
                    delta_content = str(message.get("content") or "")
                    delta_thinking = str(message.get("thinking") or "")
                    delta = f"{delta_thinking}{delta_content}"
                    if not delta:
                        continue
                    stream_parts.append(delta)
                    current_stream = "".join(stream_parts)
                    self._write_runtime_status(
                        status="running",
                        current_role=role,
                        current_model=model,
                        current_stream_text=current_stream[-4000:],
                        worker_running=self._worker_running(),
                    )
                last_raw = "".join(stream_parts)
            else:
                response = self.llm_backend.chat(
                    model=model,
                    messages=messages,
                    options=options,
                    timeout_seconds=timeout_seconds,
                )
                last_raw = str(response.get("content") or "")
                stream_metadata = self._extract_stream_metadata(response.get("raw") or {}, previous={})
                self._write_runtime_status(
                    status="running",
                    current_role=role,
                    current_model=model,
                    current_stream_text=last_raw[-4000:],
                    worker_running=self._worker_running(),
                )
            envelope = self._parse_envelope(last_raw)
            if self._raw_contains_json_object(last_raw) and self._looks_like_structured_envelope(envelope):
                finished_at = time.time()
                finished_iso = now_iso()
                self._write_runtime_status(
                    status="running",
                    current_role=role,
                    current_model=model,
                    last_llm_started_at=started_iso,
                    last_llm_finished_at=finished_iso,
                    last_llm_duration_ms=int((finished_at - started_at) * 1000),
                    last_llm_attempt_count=attempt_count,
                    last_llm_raw_preview=last_raw[:500],
                    last_llm_parse_issue=None,
                    last_llm_stream_metadata=stream_metadata,
                    current_stream_text=last_raw[-4000:],
                )
                return {"envelope": envelope, "attempt_count": attempt_count, "raw_text": last_raw, "parse_issue": "", "stream_metadata": stream_metadata}
            messages = [
                {"role": "system", "content": self._system_prompt()},
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": last_raw},
                {
                    "role": "user",
                    "content": (
                        "Your previous response was not valid JSON for the required schema. "
                        "Return JSON only with keys analysis, assistant_message, tool_name, tool_args."
                    ),
                },
            ]
        finished_at = time.time()
        finished_iso = now_iso()
        fallback = self._parse_envelope(last_raw)
        parse_issue = self._classify_llm_parse_issue(
            raw_text=last_raw,
            envelope=fallback,
            stream_metadata=stream_metadata,
        )
        self._write_runtime_status(
            status="running",
            current_role=role,
            current_model=model,
            last_llm_started_at=started_iso,
            last_llm_finished_at=finished_iso,
            last_llm_duration_ms=int((finished_at - started_at) * 1000),
            last_llm_attempt_count=attempt_count,
            last_llm_raw_preview=last_raw[:500],
            last_llm_parse_issue=parse_issue,
            last_llm_stream_metadata=stream_metadata,
            current_stream_text=last_raw[-4000:],
            last_error=f"llm response did not follow json contract: {parse_issue}",
        )
        return {"envelope": fallback, "attempt_count": attempt_count, "raw_text": last_raw, "parse_issue": parse_issue, "stream_metadata": stream_metadata}

    def _extract_stream_metadata(self, chunk: dict[str, Any], *, previous: dict[str, Any]) -> dict[str, Any]:
        metadata = dict(previous or {})
        if not isinstance(chunk, dict):
            return metadata
        for key in (
            "done",
            "done_reason",
            "total_duration",
            "load_duration",
            "prompt_eval_count",
            "prompt_eval_duration",
            "eval_count",
            "eval_duration",
        ):
            if key in chunk:
                metadata[key] = chunk.get(key)
        return metadata

    def _classify_llm_parse_issue(
        self,
        *,
        raw_text: str,
        envelope: dict[str, Any],
        stream_metadata: dict[str, Any],
    ) -> str:
        raw = str(raw_text or "")
        if not raw.strip():
            return "empty_output"
        done_reason = str((stream_metadata or {}).get("done_reason") or "").lower()
        if done_reason in {"length", "max_tokens", "num_predict"}:
            return "length_truncated"
        if self._looks_like_truncated_json(raw):
            return "length_truncated"
        if "{" not in raw:
            return "missing_json_object"
        candidate = self._extract_json_object(raw.strip())
        if candidate is None:
            return "json_parse_error"
        try:
            json.loads(candidate)
        except json.JSONDecodeError:
            return "json_parse_error"
        if not self._looks_like_structured_envelope(envelope):
            return "invalid_tool_envelope"
        return "json_contract_not_confirmed"

    def _looks_like_truncated_json(self, raw_text: str) -> bool:
        raw = str(raw_text or "").strip()
        if not raw or "{" not in raw:
            return False
        if self._extract_json_object(raw) is not None:
            return False
        open_braces = raw.count("{") - raw.count("}")
        open_brackets = raw.count("[") - raw.count("]")
        quote_count = raw.count('"') - raw.count('\\"')
        if open_braces > 0 or open_brackets > 0:
            return True
        if quote_count % 2 == 1:
            return True
        return raw.endswith(("\\", ",", ":", "{", "["))

    def _looks_like_structured_envelope(self, envelope: dict[str, Any]) -> bool:
        return isinstance(envelope, dict) and "tool_name" in envelope and "tool_args" in envelope

    def _raw_contains_json_object(self, raw_text: str) -> bool:
        return self._extract_json_object(raw_text.strip()) is not None

    def _extract_json_object(self, text: str) -> str | None:
        start = text.find("{")
        if start < 0:
            return None
        depth = 0
        for index in range(start, len(text)):
            char = text[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start : index + 1]
        return None

    def _worker_running(self) -> bool:
        if not self.paths.worker_pid_path.exists():
            return False
        try:
            pid = int(self.paths.worker_pid_path.read_text(encoding="utf-8").strip())
        except ValueError:
            return False
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

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
        last_llm_parse_issue: Any = _UNSET,
        last_llm_stream_metadata: Any = _UNSET,
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
            "last_llm_parse_issue": current.get("last_llm_parse_issue") if last_llm_parse_issue is _UNSET else last_llm_parse_issue,
            "last_llm_stream_metadata": current.get("last_llm_stream_metadata") if last_llm_stream_metadata is _UNSET else last_llm_stream_metadata,
            "last_event_at": now_iso(),
            "worker_running": current.get("worker_running") if worker_running is None else worker_running,
        }
        write_json(self.paths.runtime_status_path, payload)


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
    return [sys.executable, "-u", "-m", "p3_core.cli", "worker"]
