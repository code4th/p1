from __future__ import annotations

import json
import re
import time
from typing import Any

from p4_core.config_defaults import DEFAULT_TOOL_CONTENT_CHUNK_BYTES
from p4_core.schema_validation import validate_json_schema
from p4_core.schemas import TOOL_ACTION_SCHEMA, tool_action_schema
from p4_core.workspace import append_jsonl, append_session_event, now_iso, read_json, read_jsonl


def _chat_with_repair(
    self,
    *,
    role: str,
    model: str,
    prompt: str,
    session_id: str | None = None,
    turn_id: str | None = None,
    queue_id: str | None = None,
    step_index: int | None = None,
    llm_workspace: str | None = None,
    current_phase: str | None = None,
    suppress_frame_operations: bool = False,
    allowed_tool_names: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    timeout_seconds = int(self.runtime_config.get("chat_timeout_seconds") or 180)
    retry_limit = int(self.runtime_config.get("json_retry_limit") or 0)
    thinking_only_repair_limit = int(self.runtime_config.get("thinking_only_repair_limit") if self.runtime_config.get("thinking_only_repair_limit") is not None else 1)
    max_stream_chars = int(self.runtime_config.get("max_machine_control_stream_chars") or 24000)
    phase_name = str(current_phase or "")
    if phase_name.startswith("IMPLEMENTATION_TASK_PROGRESS") or phase_name == "PLAN_EXECUTION":
        implementation_stream_chars = int(
            self.runtime_config.get("implementation_task_machine_control_stream_chars") or 10000
        )
        if implementation_stream_chars > 0:
            max_stream_chars = (
                min(max_stream_chars, implementation_stream_chars)
                if max_stream_chars > 0
                else implementation_stream_chars
            )
    options = dict(self.ollama_options.get(role, {}))
    active_tool_action_schema = tool_action_schema(
        include_frame_operations=not suppress_frame_operations,
        allowed_tool_names=allowed_tool_names,
    )
    if role == "coding":
        options["num_predict"] = max(int(options.get("num_predict") or 0), 8192)
    elif role == "terminal":
        options["num_predict"] = max(int(options.get("num_predict") or 0), 4096)
    options.setdefault("format", active_tool_action_schema)
    options.setdefault("think", False)
    started_at = time.time()
    started_iso = now_iso()
    last_content = ""
    last_thinking = ""
    last_display = ""
    attempt_count = 0
    stream_metadata: dict[str, Any] = {}

    messages = [
        {
            "role": "system",
            "content": self._system_prompt(
                suppress_frame_operations=suppress_frame_operations,
                allowed_tool_names=allowed_tool_names,
            ),
        },
        {"role": "user", "content": prompt},
    ]
    normal_repairs_used = 0
    thinking_repairs_used = 0
    while True:
        attempt_count += 1
        stream_metadata = {}
        supports_stream = callable(getattr(self.llm_backend, "iter_chat_stream", None))
        transport = "chat_stream" if supports_stream else "chat_nonstream"
        self._append_runtime_event(
            session_id,
            event_name="llm_call_started",
            content=f"LLM call started: {model}",
            details={
                "role": role,
                "model": model,
                "attempt_count": attempt_count,
                "timeout_seconds": timeout_seconds,
                "transport": transport,
                "schema_required": True,
            },
            turn_id=turn_id,
            queue_id=queue_id,
            step_index=step_index,
            llm_workspace=llm_workspace,
            phase=current_phase,
        )
        try:
            if supports_stream:
                content_parts: list[str] = []
                thinking_parts: list[str] = []
                for chunk in self.llm_backend.iter_chat_stream(
                    model=model,
                    messages=messages,
                    options=options,
                    timeout_seconds=timeout_seconds,
                ):
                    stream_metadata = self._extract_stream_metadata(chunk, previous=stream_metadata)
                    message = chunk.get("message") if isinstance(chunk, dict) else {}
                    message = message if isinstance(message, dict) else {}
                    delta_content = str(message.get("content") or "")
                    delta_thinking = str(message.get("thinking") or "")
                    if delta_content:
                        content_parts.append(delta_content)
                    if delta_thinking:
                        thinking_parts.append(delta_thinking)
                    last_content = "".join(content_parts)
                    last_thinking = "".join(thinking_parts)
                    last_display = self._format_llm_stream_text(thinking_text=last_thinking, content_text=last_content)
                    delta_display = self._format_llm_stream_text(thinking_text=delta_thinking, content_text=delta_content)
                    self._append_runtime_event(
                        session_id,
                        event_name="llm_stream_chunk",
                        content=delta_display,
                        details={
                            "role": role,
                            "model": model,
                            "attempt_count": attempt_count,
                            "delta_content": delta_content,
                            "delta_thinking": delta_thinking,
                            "content_text": delta_content,
                            "thinking_text": delta_thinking,
                            "accumulated_content_chars": len(last_content),
                            "accumulated_thinking_chars": len(last_thinking),
                            "stream_metadata": stream_metadata,
                            "transport": transport,
                            "schema_required": True,
                        },
                        turn_id=turn_id,
                        queue_id=queue_id,
                        step_index=step_index,
                        llm_workspace=llm_workspace,
                        phase=current_phase,
                    )
                    self._write_runtime_status(
                        status="running",
                        current_role=role,
                        current_model=model,
                        current_stream_text=self._tail_stream_text(last_display, limit=4000),
                        last_llm_stream_metadata=stream_metadata,
                        worker_running=self._worker_running(),
                    )
                    stop_reason = self._machine_control_stream_stop_reason(
                        content_text=last_content,
                        thinking_text=last_thinking,
                        max_stream_chars=max_stream_chars,
                        schema=active_tool_action_schema,
                        current_phase=current_phase,
                    )
                    if stop_reason:
                        stream_metadata["client_abort_reason"] = stop_reason
                        stream_metadata["accumulated_content_chars"] = len(last_content)
                        stream_metadata["accumulated_thinking_chars"] = len(last_thinking)
                        self._append_runtime_event(
                            session_id,
                            event_name="llm_stream_aborted",
                            content=f"LLM stream stopped by runtime: {stop_reason}",
                            details={
                                "role": role,
                                "model": model,
                                "attempt_count": attempt_count,
                                "client_abort_reason": stop_reason,
                                "accumulated_content_chars": len(last_content),
                                "accumulated_thinking_chars": len(last_thinking),
                                "stream_metadata": stream_metadata,
                                "transport": transport,
                                "schema_required": True,
                            },
                            turn_id=turn_id,
                            queue_id=queue_id,
                            step_index=step_index,
                            llm_workspace=llm_workspace,
                            phase=current_phase,
                        )
                        break
            else:
                response = self.llm_backend.chat(
                    model=model,
                    messages=messages,
                    options=options,
                    timeout_seconds=timeout_seconds,
                )
                last_content = str(response.get("content_text") if "content_text" in response else response.get("content") or "")
                last_thinking = str(response.get("thinking_text") or response.get("thinking") or "")
                last_display = self._format_llm_stream_text(thinking_text=last_thinking, content_text=last_content)
                stream_metadata = self._extract_stream_metadata(response.get("raw") or {}, previous={})
        except Exception as exc:
            error_text = f"{type(exc).__name__}: {exc}"
            stream_metadata = {**stream_metadata, "transport_error": error_text}
            last_display = self._format_llm_stream_text(thinking_text=last_thinking, content_text=last_content)
            self._append_runtime_event(
                session_id,
                event_name="llm_call_failed",
                content=f"LLM call failed: {error_text}",
                details={
                    "role": role,
                    "model": model,
                    "attempt_count": attempt_count,
                    "transport": transport,
                    "schema_required": True,
                    "error": error_text,
                    "content_text": last_content,
                    "thinking_text": last_thinking,
                    "stream_metadata": stream_metadata,
                },
                turn_id=turn_id,
                queue_id=queue_id,
                step_index=step_index,
                llm_workspace=llm_workspace,
                phase=current_phase,
            )
            self._write_runtime_status(
                status="running",
                current_role=role,
                current_model=model,
                current_stream_text=f"LLM call failed: {error_text}",
                last_llm_stream_metadata=stream_metadata,
                last_error=f"LLM call failed: {error_text}",
                worker_running=self._worker_running(),
            )
            return {
                "envelope": {},
                "attempt_count": attempt_count,
                "raw_text": last_content,
                "thinking_text": last_thinking,
                "combined_text": last_display,
                "parse_issue": "llm_transport_error",
                "schema_validation": {"ok": False, "errors": [error_text]},
                "raw_output_is_machine_json": False,
                "schema_validation_ok": False,
                "stream_metadata": stream_metadata,
            }
        self._append_runtime_event(
            session_id,
            event_name="llm_response_received",
            content=last_display,
            details={
                "role": role,
                "model": model,
                "attempt_count": attempt_count,
                "content_text": last_content,
                "thinking_text": last_thinking,
                "stream_metadata": stream_metadata,
                "transport": transport,
                "schema_required": True,
            },
            turn_id=turn_id,
            queue_id=queue_id,
            step_index=step_index,
            llm_workspace=llm_workspace,
            phase=current_phase,
        )
        self._write_runtime_status(
            status="running",
            current_role=role,
            current_model=model,
            current_stream_text=self._tail_stream_text(last_display, limit=4000),
            worker_running=self._worker_running(),
        )
        envelope = self._parse_envelope(last_content)
        schema_validation = validate_json_schema(envelope, active_tool_action_schema)
        raw_output_is_machine_json = self._raw_is_exact_json_object(last_content)
        # Recovery path (やり切る invariant): envelope が valid envelope + schema 適合なら、
        # raw に余計テキストがあっても採用する。tool_name と tool_args は
        # _extract_json_object が決定論的に最長 valid object を選ぶため確定的。
        # 失敗を全捨てするのではなく、警告付きで前進する (graceful degradation)。
        envelope_is_recoverable = (
            self._looks_like_structured_envelope(envelope)
            and schema_validation.ok
        )
        if (
            raw_output_is_machine_json
            and self._looks_like_structured_envelope(envelope)
            and schema_validation.ok
        ):
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
                last_llm_raw_preview=last_content[:500],
                last_llm_thinking_preview=last_thinking[:500],
                last_llm_parse_issue=None,
                last_llm_schema_validation={"ok": True, "errors": []},
                raw_output_is_machine_json=True,
                schema_validation_ok=True,
                last_llm_stream_metadata=stream_metadata,
                current_stream_text=self._tail_stream_text(last_display, limit=4000),
            )
            self._append_runtime_event(
                session_id,
                event_name="llm_call_finished",
                content=last_display,
                details={
                    "role": role,
                    "model": model,
                    "attempt_count": attempt_count,
                    "duration_ms": int((finished_at - started_at) * 1000),
                    "content_text": last_content,
                    "thinking_text": last_thinking,
                    "stream_metadata": stream_metadata,
                    "parse_issue": "",
                    "schema_validation": {"ok": True, "errors": []},
                    "raw_output_is_machine_json": True,
                    "schema_validation_ok": True,
                    "transport": transport,
                },
                turn_id=turn_id,
                queue_id=queue_id,
                step_index=step_index,
                llm_workspace=llm_workspace,
                phase=current_phase,
            )
            return {
                "envelope": envelope,
                "attempt_count": attempt_count,
                "raw_text": last_content,
                "thinking_text": last_thinking,
                "combined_text": last_display,
                "parse_issue": "",
                "schema_validation": {"ok": True, "errors": []},
                "raw_output_is_machine_json": True,
                "schema_validation_ok": True,
                "stream_metadata": stream_metadata,
            }
        fallback = self._parse_envelope(last_content)
        parse_issue = self._classify_llm_parse_issue(
            raw_text=last_content,
            thinking_text=last_thinking,
            envelope=fallback,
            stream_metadata=stream_metadata,
            schema=active_tool_action_schema,
        )
        # やり切る recovery: parse_issue == json_extraneous_text かつ envelope は valid + schema-ok
        # の場合、警告付きで envelope を採用する。LLM が合法な envelope を出した直後に
        # 続けて余計な JSON や prose を出すケース (=複数手を一括予測しようとする) を
        # 1ステップ単位に切り戻して前進する。
        # 詳細: handoff/p4-followthrough-recovery-2026-05-03.md
        if (
            parse_issue == "json_extraneous_text"
            and envelope_is_recoverable
        ):
            finished_at = time.time()
            finished_iso = now_iso()
            warning = "extraneous text after structured envelope; using first valid envelope only"
            self._write_runtime_status(
                status="running",
                current_role=role,
                current_model=model,
                last_llm_started_at=started_iso,
                last_llm_finished_at=finished_iso,
                last_llm_duration_ms=int((finished_at - started_at) * 1000),
                last_llm_attempt_count=attempt_count,
                last_llm_raw_preview=last_content[:500],
                last_llm_thinking_preview=last_thinking[:500],
                last_llm_parse_issue="json_extraneous_text_recovered",
                last_llm_schema_validation={"ok": True, "errors": []},
                raw_output_is_machine_json=False,
                schema_validation_ok=True,
                last_llm_stream_metadata=stream_metadata,
                current_stream_text=self._tail_stream_text(last_display, limit=4000),
            )
            self._append_runtime_event(
                session_id,
                event_name="llm_call_finished",
                content=last_display,
                details={
                    "role": role,
                    "model": model,
                    "attempt_count": attempt_count,
                    "duration_ms": int((finished_at - started_at) * 1000),
                    "content_text": last_content,
                    "thinking_text": last_thinking,
                    "stream_metadata": stream_metadata,
                    "parse_issue": "json_extraneous_text_recovered",
                    "schema_validation": {"ok": True, "errors": []},
                    "raw_output_is_machine_json": False,
                    "schema_validation_ok": True,
                    "transport": transport,
                    "recovery_warning": warning,
                },
                turn_id=turn_id,
                queue_id=queue_id,
                step_index=step_index,
                llm_workspace=llm_workspace,
                phase=current_phase,
            )
            # システムノートとして「envelope を採用、余計テキストは破棄」を可視化
            if session_id is not None:
                append_session_event(
                    self.root,
                    session_id,
                    {
                        "type": "system_note",
                        "role": "system",
                        "content": f"LLM出力の余計テキストを除去し、最初の有効なツールエンベロープを採用しました ({envelope.get('tool_name','?')})。",
                        "code": "llm_output_recovered",
                        "reason_code": "json_extraneous_text_recovered",
                        "details": {
                            "envelope_tool_name": str(envelope.get("tool_name") or ""),
                            "raw_length": len(last_content),
                            "warning": warning,
                        },
                        "turn_id": turn_id,
                        "queue_id": queue_id,
                        "step_index": step_index,
                        "llm_workspace": llm_workspace,
                    },
                )
            return {
                "envelope": envelope,
                "attempt_count": attempt_count,
                "raw_text": last_content,
                "thinking_text": last_thinking,
                "combined_text": last_display,
                "parse_issue": "",  # 後段の "turn 失敗" 経路に流さない
                "schema_validation": {"ok": True, "errors": []},
                "raw_output_is_machine_json": False,
                "schema_validation_ok": True,
                "stream_metadata": stream_metadata,
                "recovery_warning": warning,
            }
        if parse_issue == "thinking_only_output" and thinking_repairs_used < thinking_only_repair_limit:
            self._append_runtime_event(
                session_id,
                event_name="llm_repair_requested",
                content=f"Repair requested: {parse_issue}",
                details={
                    "role": role,
                    "model": model,
                    "attempt_count": attempt_count,
                    "parse_issue": parse_issue,
                    "schema_validation": {"ok": schema_validation.ok, "errors": list(schema_validation.errors)},
                    "raw_output_is_machine_json": raw_output_is_machine_json,
                    "schema_validation_ok": bool(schema_validation.ok),
                    "content_text": last_content,
                    "thinking_text": last_thinking,
                    "stream_metadata": stream_metadata,
                },
                turn_id=turn_id,
                queue_id=queue_id,
                step_index=step_index,
                llm_workspace=llm_workspace,
                phase=current_phase,
            )
            thinking_repairs_used += 1
            messages = [
                {
                    "role": "system",
                    "content": self._system_prompt(
                        suppress_frame_operations=suppress_frame_operations,
                        allowed_tool_names=allowed_tool_names,
                    ),
                },
                {"role": "user", "content": prompt},
                {
                    "role": "user",
                    "content": self._thinking_only_repair_prompt(thinking_text=last_thinking, stream_metadata=stream_metadata),
                },
            ]
            continue
        if self._parse_issue_should_exit_repair_loop(
            parse_issue=parse_issue,
            stream_metadata=stream_metadata,
            current_phase=current_phase,
        ):
            break
        if normal_repairs_used >= retry_limit:
            break
        self._append_runtime_event(
            session_id,
            event_name="llm_repair_requested",
            content=f"Repair requested: {parse_issue}",
            details={
                "role": role,
                "model": model,
                "attempt_count": attempt_count,
                "parse_issue": parse_issue,
                "schema_validation": {"ok": schema_validation.ok, "errors": list(schema_validation.errors)},
                "raw_output_is_machine_json": raw_output_is_machine_json,
                "schema_validation_ok": bool(schema_validation.ok),
                "content_text": last_content,
                "thinking_text": last_thinking,
                "stream_metadata": stream_metadata,
            },
            turn_id=turn_id,
            queue_id=queue_id,
            step_index=step_index,
            llm_workspace=llm_workspace,
            phase=current_phase,
        )
        normal_repairs_used += 1
        messages = [
            {
                "role": "system",
                "content": self._system_prompt(
                    suppress_frame_operations=suppress_frame_operations,
                    allowed_tool_names=allowed_tool_names,
                ),
            },
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": last_content},
            {
                "role": "user",
                "content": self._json_repair_prompt(
                    parse_target_text=last_content,
                    stream_metadata=stream_metadata,
                    schema_errors=list(schema_validation.errors),
                    allowed_tool_names=list(allowed_tool_names or []),
                ),
            },
        ]
    finished_at = time.time()
    finished_iso = now_iso()
    fallback = self._parse_envelope(last_content)
    parse_issue = self._classify_llm_parse_issue(
        raw_text=last_content,
        thinking_text=last_thinking,
        envelope=fallback,
        stream_metadata=stream_metadata,
        schema=active_tool_action_schema,
    )
    schema_validation = validate_json_schema(fallback, active_tool_action_schema)
    raw_output_is_machine_json = self._raw_is_exact_json_object(last_content)
    self._write_runtime_status(
        status="running",
        current_role=role,
        current_model=model,
        last_llm_started_at=started_iso,
        last_llm_finished_at=finished_iso,
        last_llm_duration_ms=int((finished_at - started_at) * 1000),
        last_llm_attempt_count=attempt_count,
        last_llm_raw_preview=last_content[:500],
        last_llm_thinking_preview=last_thinking[:500],
        last_llm_parse_issue=parse_issue,
        last_llm_schema_validation={"ok": schema_validation.ok, "errors": list(schema_validation.errors)},
        raw_output_is_machine_json=raw_output_is_machine_json,
        schema_validation_ok=bool(schema_validation.ok),
        last_llm_stream_metadata=stream_metadata,
        current_stream_text=self._tail_stream_text(last_display, limit=4000),
        last_error=f"llm response did not follow json contract: {parse_issue}",
    )
    self._append_runtime_event(
        session_id,
        event_name="llm_call_finished",
        content=last_display,
        details={
            "role": role,
            "model": model,
            "attempt_count": attempt_count,
            "duration_ms": int((finished_at - started_at) * 1000),
            "content_text": last_content,
            "thinking_text": last_thinking,
            "stream_metadata": stream_metadata,
            "parse_issue": parse_issue,
            "schema_validation": {"ok": schema_validation.ok, "errors": list(schema_validation.errors)},
            "raw_output_is_machine_json": raw_output_is_machine_json,
            "schema_validation_ok": bool(schema_validation.ok),
            "transport": transport,
        },
        turn_id=turn_id,
        queue_id=queue_id,
        step_index=step_index,
        llm_workspace=llm_workspace,
        phase=current_phase,
    )
    return {
        "envelope": fallback,
        "attempt_count": attempt_count,
        "raw_text": last_content,
        "thinking_text": last_thinking,
        "combined_text": last_display,
        "parse_issue": parse_issue,
        "schema_validation": {"ok": schema_validation.ok, "errors": list(schema_validation.errors)},
        "raw_output_is_machine_json": raw_output_is_machine_json,
        "schema_validation_ok": bool(schema_validation.ok),
        "stream_metadata": stream_metadata,
    }


def _format_llm_stream_text(self, *, thinking_text: str, content_text: str) -> str:
    thinking = str(thinking_text or "")
    content = str(content_text or "")
    if thinking and content:
        return f"[thinking]\n{thinking}\n\n[content]\n{content}"
    if thinking:
        return f"[thinking]\n{thinking}"
    return content


def _tail_stream_text(self, text: str, *, limit: int) -> str:
    value = str(text or "")
    if len(value) <= limit:
        return value
    tail = value[-limit:]
    if value.startswith("[thinking]") and not tail.startswith("[thinking]"):
        return "[thinking]\n... [live output truncated]\n" + tail
    if value.startswith("[content]") and not tail.startswith("[content]"):
        return "[content]\n... [live output truncated]\n" + tail
    return tail


def _json_repair_prompt(
    self,
    *,
    parse_target_text: str,
    stream_metadata: dict[str, Any],
    schema_errors: list[str] | tuple[str, ...] | None = None,
    allowed_tool_names: list[str] | tuple[str, ...] | None = None,
) -> str:
    del parse_target_text
    chunk_bytes = int(getattr(self, "tool_content_chunk_bytes", DEFAULT_TOOL_CONTENT_CHUNK_BYTES) or DEFAULT_TOOL_CONTENT_CHUNK_BYTES)
    hard_chunk_bytes = chunk_bytes * 2
    done_reason = str((stream_metadata or {}).get("done_reason") or "")
    client_abort_reason = str((stream_metadata or {}).get("client_abort_reason") or "")
    length_note = (
        "The previous response hit the generation length limit. Do not continue the previous text. "
        if done_reason.lower() in {"length", "max_tokens", "num_predict"}
        else ""
    )
    if client_abort_reason == "stream_char_limit":
        length_note = (
            "The previous response exceeded P4's machine-control stream limit. "
            "Do not continue the previous text and do not repeat the same large design. "
            "Restart with one smaller complete tool JSON object. For implementation artifacts, prefer a minimal "
            "complete reference implementation over optional architecture or optimization. "
        )
    elif client_abort_reason == "repetitive_output":
        length_note = (
            "The previous response repeated similar content and was stopped by P4's stream guard. "
            "Do not continue that repeated text. Return one smaller complete tool JSON object that directly performs "
            "the next allowed action, preferably a minimal meaningful artifact or targeted edit. "
        )
    elif client_abort_reason == "plan_record_embedded_edit_stream":
        length_note = (
            "The previous create_plan response was stopped because a PlanRecord WorkUnit first_action tried to use "
            "write_file, append_file, or replace_text. This is not a length problem. Do not put implementation edits "
            "inside create_plan. Return a concise PlanRecord only, and make every WorkUnit first_action use one of "
            "list_files, read_file, search_code, or run_command. Implementation code must be emitted later during "
            "PLAN_EXECUTION after the child frame opens. For any edit WorkUnit, use exactly "
            '{"first_action":{"tool":"list_files","args":{"path":"."}}} or another inspect-only first_action; '
            "never use write_file as an edit WorkUnit first_action. "
        )
    schema_note = ""
    clean_errors = [str(item).strip() for item in schema_errors or [] if str(item).strip()]
    clean_tools = [str(item).strip() for item in allowed_tool_names or [] if str(item).strip()]
    if clean_errors:
        schema_note += "Schema errors to fix: " + "; ".join(clean_errors[:4]) + ". "
    if clean_tools:
        schema_note += (
            "The allowed tool_name enum for this phase is exactly "
            f"{json.dumps(clean_tools, ensure_ascii=False)}. "
            "Do not use any other tool_name. "
        )
        if "read_file" not in clean_tools:
            schema_note += (
                "If you were about to use read_file again, that observation has already been consumed; "
                "choose an allowed edit tool such as replace_text or write_file instead. "
            )
    return (
        "Your previous response was not valid JSON for the required schema. "
        f"{length_note}"
        f"{schema_note}"
        "Return exactly one JSON object in assistant content only, with keys "
        "analysis, assistant_message, tool_name, tool_args. Do not put prose, Markdown, "
        "code fences, or hidden reasoning in the visible content. "
        "If you are writing a file, return only one tool call that can complete within this response. "
        f"write_file and append_file tool_args.content should usually stay within {chunk_bytes} UTF-8 bytes per step. "
        f"If the JSON can close and the source syntax remains valid, P4 may accept up to {hard_chunk_bytes} UTF-8 bytes with a warning. "
        f"If the content is more than {hard_chunk_bytes} UTF-8 bytes, do not emit it in one response: include only the next line-boundary chunk now and continue with append_file in a later step. "
        "For existing files, use replace_text with an exact unique old_text copied from read_file output. "
        "Always close the JSON object."
    )


def _thinking_only_repair_prompt(self, *, thinking_text: str, stream_metadata: dict[str, Any]) -> str:
    del stream_metadata
    thinking_preview = str(thinking_text or "")[:1200]
    return (
        "Your previous response put useful text only in hidden thinking/reasoning, and assistant content was empty. "
        "P4 cannot treat hidden thinking as an action or final answer. "
        "Return exactly one JSON object in visible assistant content only, with keys "
        "analysis, assistant_message, tool_name, tool_args. "
        "Do not include Markdown, code fences, prose outside JSON, or hidden reasoning in content. "
        "If the user only needs a conversational answer and no tool is needed, use "
        "{\"assistant_message\":\"...\",\"tool_name\":\"final_answer\",\"tool_args\":{\"answer\":\"...\"}}. "
        "If a tool is needed, choose exactly one concrete tool call. "
        f"Previous hidden-thinking preview for diagnosis only, not to copy verbatim: {thinking_preview}"
    )


def _parse_issue_should_exit_repair_loop(
    self,
    *,
    parse_issue: str,
    stream_metadata: dict[str, Any],
    current_phase: str | None,
) -> bool:
    del current_phase
    issue = str(parse_issue or "")
    client_abort_reason = str((stream_metadata or {}).get("client_abort_reason") or "")
    if issue in {"stream_char_limit", "repetitive_output", "plan_record_embedded_edit_stream"}:
        return True
    if client_abort_reason in {"stream_char_limit", "repetitive_output", "plan_record_embedded_edit_stream"}:
        return True
    return False


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


def _machine_control_stream_stop_reason(
    self,
    *,
    content_text: str,
    thinking_text: str,
    max_stream_chars: int,
    schema: dict[str, Any] | None = None,
    current_phase: str | None = None,
) -> str:
    del thinking_text
    content = str(content_text or "")
    if not content:
        return ""
    if self._looks_like_plan_record_embedded_edit_stream(content, current_phase=current_phase):
        return "plan_record_embedded_edit_stream"
    candidate = self._extract_json_object(content.strip())
    if candidate is not None:
        envelope = self._parse_envelope(content)
        schema_validation = validate_json_schema(envelope, schema or TOOL_ACTION_SCHEMA)
        if (
            self._looks_like_structured_envelope(envelope)
            and schema_validation.ok
            and content.strip() != candidate
        ):
            return "json_envelope_followed_by_extra_text"
    if max_stream_chars > 0 and len(content) >= max_stream_chars:
        return "stream_char_limit"
    if self._looks_like_repetitive_machine_control_output(content):
        return "repetitive_output"
    if self._looks_like_in_progress_write_file_content_stream(content):
        return ""
    return ""


def _looks_like_plan_record_embedded_edit_stream(self, text: str, *, current_phase: str | None = None) -> bool:
    phase = str(current_phase or "")
    if phase not in {"PLANNING_REQUIRED", "PLAN_REVISION"}:
        return False
    content = str(text or "")
    if '"create_plan"' not in content or '"work_units"' not in content or '"first_action"' not in content:
        return False
    first_action_index = content.rfind('"first_action"')
    if first_action_index < 0:
        return False
    tail = content[first_action_index:first_action_index + 1600]
    return bool(re.search(r'"tool"\s*:\s*"(write_file|append_file|replace_text)"', tail))


def _looks_like_in_progress_write_file_content_stream(self, text: str) -> bool:
    content = str(text or "")
    if '"write_file"' not in content or '"tool_args"' not in content:
        return False
    if self._extract_json_object(content.strip()) is not None:
        return False
    content_match = re.search(r'"content"\s*:\s*"', content)
    if not content_match:
        return False
    escaped = False
    for char in content[content_match.end() :]:
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            return False
    return True


def _looks_like_repetitive_machine_control_output(self, text: str) -> bool:
    content = str(text or "")
    min_chars = int(self.runtime_config.get("machine_control_repetition_min_chars") or 3000)
    if len(content) < min_chars and content.lstrip().startswith("{"):
        return False
    tail_chars = int(self.runtime_config.get("machine_control_repetition_tail_chars") or 160)
    min_repeats = int(self.runtime_config.get("machine_control_repetition_min_repeats") or 5)
    if tail_chars <= 0 or min_repeats <= 1:
        return False
    min_unit = max(16, min(24, tail_chars // 4))
    if len(content) < min_unit * min_repeats:
        return False
    max_unit = min(tail_chars, len(content) // min_repeats)
    candidate_lengths = {min_unit, max_unit, tail_chars // 2, tail_chars}
    candidate_lengths.update(range(min_unit, max_unit + 1))
    for unit_len in sorted(candidate_lengths):
        if unit_len <= 0 or len(content) < unit_len * min_repeats:
            continue
        unit = content[-unit_len:]
        if len(unit.strip()) < min_unit:
            continue
        repeated_region = content[-unit_len * min_repeats :]
        if repeated_region == unit * min_repeats:
            return True
        if repeated_region.count(unit) >= min_repeats - 1:
            return True
    lookback = content[-max(tail_chars * min_repeats, 800) :]
    for phrase_len in range(max(40, min_unit), min(tail_chars, len(lookback) // 2) + 1):
        phrase = lookback[-phrase_len:]
        if len(phrase.strip()) < 40:
            continue
        if lookback.count(phrase) >= min_repeats:
            return True
    if len(content) < min_chars:
        return False
    min_similar_lines = int(self.runtime_config.get("machine_control_repetition_min_similar_lines") or 14)
    if min_similar_lines <= 1:
        return False
    line_lookback = content[-5000:]
    normalized_counts: dict[str, int] = {}
    for raw_line in line_lookback.splitlines():
        line = raw_line.strip()
        if len(line) < 28:
            continue
        if line in {"{", "}", "[", "]"}:
            continue
        normalized = re.sub(r"\d+", "N", line)
        normalized = re.sub(r"(['\"])[^'\"]{1,80}\1", r"\1S\1", normalized)
        normalized = re.sub(r"\s+", " ", normalized).strip()[:180]
        if len(normalized) < 28:
            continue
        normalized_counts[normalized] = normalized_counts.get(normalized, 0) + 1
        if normalized_counts[normalized] >= min_similar_lines:
            return True
    return False


def _classify_llm_parse_issue(
    self,
    *,
    raw_text: str,
    thinking_text: str = "",
    envelope: dict[str, Any],
    stream_metadata: dict[str, Any],
    schema: dict[str, Any] | None = None,
) -> str:
    raw = str(raw_text or "")
    client_abort_reason = str((stream_metadata or {}).get("client_abort_reason") or "")
    if client_abort_reason in {"repetitive_output", "stream_char_limit", "plan_record_embedded_edit_stream"}:
        return client_abort_reason
    if not raw.strip():
        if str(thinking_text or "").strip():
            return "thinking_only_output"
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
    if raw.strip() != candidate:
        return "json_extraneous_text"
    try:
        json.loads(candidate)
    except json.JSONDecodeError:
        return "json_parse_error"
    if not self._looks_like_structured_envelope(envelope):
        return "invalid_tool_envelope"
    schema_validation = validate_json_schema(envelope, schema or TOOL_ACTION_SCHEMA)
    if not schema_validation.ok:
        return "schema_validation_failed"
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


def _raw_is_exact_json_object(self, raw_text: str) -> bool:
    raw = str(raw_text or "").strip()
    candidate = self._extract_json_object(raw)
    return candidate is not None and raw == candidate


def _extract_json_object(self, text: str) -> str | None:
    raw = str(text or "")
    if "{" not in raw:
        return None
    decoder = json.JSONDecoder()
    best: tuple[int, str] | None = None
    for index, char in enumerate(raw):
        if char != "{":
            continue
        prefix = raw[:index].rstrip()
        if prefix and prefix[-1] in {":", "[", ","}:
            continue
        try:
            payload, end = decoder.raw_decode(raw[index:])
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        candidate = raw[index : index + end]
        candidate_len = len(candidate)
        if best is None or candidate_len > best[0]:
            best = (candidate_len, candidate)
    return best[1] if best is not None else None
