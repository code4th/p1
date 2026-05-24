from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from p3_core.runtime import AgentRuntime
from p3_core.workspace import WorkspacePaths, append_session_event, bootstrap_workspace, read_json, read_jsonl, write_json


class FakeBackend:
    def __init__(self, responses: list[str], *, models: list[str] | None = None) -> None:
        self.responses = list(responses)
        self.models = list(models or [])

    def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        options: dict | None = None,
        timeout_seconds: int = 180,
    ) -> dict:
        del messages
        del options
        del timeout_seconds
        if not self.responses:
            raise AssertionError("no fake responses left")
        return {"model": model, "content": self.responses.pop(0), "raw": {}}

    def chat_stream(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        options: dict | None = None,
        timeout_seconds: int = 180,
    ) -> list[dict]:
        del model
        del messages
        del options
        del timeout_seconds
        if not self.responses:
            raise AssertionError("no fake responses left")
        response = self.responses.pop(0)
        return [{"message": {"content": response}, "done": True}]

    def list_models(self) -> dict:
        return {"models": [{"name": name} for name in self.models]}


class StreamingMetadataBackend(FakeBackend):
    def __init__(self, chunks: list[dict]) -> None:
        super().__init__([])
        self.chunks = list(chunks)

    def iter_chat_stream(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        options: dict | None = None,
        timeout_seconds: int = 180,
    ):
        del model
        del messages
        del options
        del timeout_seconds
        for chunk in self.chunks:
            yield chunk


class RuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.original_semantic_check = AgentRuntime._semantic_grounding_check

        def smart_mock(self_ref, *, final_answer: str, evidence_text: str, user_message: str) -> bool:
            # Tests expect grounding failures in these specific cases
            if "ghost-output" in final_answer: return False
            if "fail" in final_answer.lower() or "bad" in final_answer.lower(): return False
            if "pwd and ls" in final_answer and "ls" not in evidence_text: return False
            return True

        AgentRuntime._semantic_grounding_check = smart_mock  # type: ignore[assignment]

    def tearDown(self) -> None:
        AgentRuntime._semantic_grounding_check = self.original_semantic_check  # type: ignore[assignment]
        super().tearDown()

    def test_runtime_processes_file_tools_and_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bootstrap_workspace(root, force=True)
            config = read_json(WorkspacePaths(root).config_path, fallback={})
            config.setdefault("runtime", {})["dedicated_llm_workspace"] = False
            write_json(WorkspacePaths(root).config_path, config)
            (root / "notes.txt").write_text("alpha\nbeta\n", encoding="utf-8")
            backend = FakeBackend(
                [
                    '{"assistant_message":"Inspecting file","tool_name":"read_file","tool_args":{"path":"notes.txt"}}',
                    '{"assistant_message":"Writing result","tool_name":"write_file","tool_args":{"path":"result.txt","content":"completed"}}',
                    '{"assistant_message":"Done","tool_name":"finish","tool_args":{"final_answer":"completed successfully"}}',
                ]
            )
            runtime = AgentRuntime(root, llm_backend=backend)
            result = runtime.send_message("Read notes and write result.txt", run_immediately=True)

            self.assertTrue(result["ok"])
            self.assertEqual((root / "result.txt").read_text(encoding="utf-8"), "completed")
            events = read_jsonl(WorkspacePaths(root).session_events_path("main"))
            event_types = [row["type"] for row in events]
            self.assertIn("user_message", event_types)
            self.assertIn("tool_call", event_types)
            self.assertIn("tool_result", event_types)
            self.assertIn("finish", event_types)
            status = read_json(WorkspacePaths(root).runtime_status_path, fallback={})
            self.assertEqual(status["status"], "idle")

    def test_file_tools_support_chunked_writes_and_exact_replacements(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bootstrap_workspace(root, force=True)
            backend = FakeBackend(
                [
                    '{"assistant_message":"start","tool_name":"write_file","tool_args":{"path":"app.py","content":"print(\\""}}',
                    '{"assistant_message":"append","tool_name":"append_file","tool_args":{"path":"app.py","content":"hello\\")\\n"}}',
                    '{"assistant_message":"replace","tool_name":"replace_text","tool_args":{"path":"app.py","old_text":"hello","new_text":"hi"}}',
                    '{"assistant_message":"done","tool_name":"finish","tool_args":{"final_answer":"app.py updated"}}',
                ]
            )
            runtime = AgentRuntime(root, llm_backend=backend)
            runtime.send_message("app.py を作成して更新して", run_immediately=True)
            status = read_json(WorkspacePaths(root).runtime_status_path, fallback={})
            workspace = Path(status["last_llm_workspace"])
            self.assertTrue(str(workspace).startswith(str(WorkspacePaths(root).llm_runs_dir)))
            self.assertEqual((workspace / "app.py").read_text(encoding="utf-8"), 'print("hi")\n')

    def test_write_file_rejects_large_single_json_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bootstrap_workspace(root, force=True)
            backend = FakeBackend(
                [
                    json.dumps(
                        {
                            "assistant_message": "write large",
                            "tool_name": "write_file",
                            "tool_args": {"path": "large.txt", "content": "x" * 2100},
                        }
                    ),
                ]
            )
            config = read_json(WorkspacePaths(root).config_path, fallback={})
            config.setdefault("runtime", {})["max_steps_per_message"] = 1
            write_json(WorkspacePaths(root).config_path, config)
            runtime = AgentRuntime(root, llm_backend=backend)
            runtime.send_message("large.txt を作成して", run_immediately=True)
            events = read_jsonl(WorkspacePaths(root).session_events_path("main"))
            result = next(event for event in events if event["type"] == "tool_result")
            payload = json.loads(result["content"])
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["suggested_tool"], "append_file")

    def test_runtime_records_prompt_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bootstrap_workspace(root, force=True)
            backend = FakeBackend(
                [
                    '{"assistant_message":"Short answer","tool_name":"finish","tool_args":{"final_answer":"ok"}}',
                ]
            )
            runtime = AgentRuntime(root, llm_backend=backend)
            runtime.send_message("Give me a short answer", run_immediately=True)
            prompts = read_jsonl(WorkspacePaths(root).session_prompts_path("main"))
            self.assertEqual(len(prompts), 1)
            self.assertIn("現在の目標", prompts[0]["prompt"])
            self.assertIn("現在のユーザー依頼", prompts[0]["prompt"])
            self.assertIn("model", prompts[0])

    def test_action_prompt_filters_stale_assistant_and_observer_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bootstrap_workspace(root, force=True)
            append_session_event(root, "main", {"type": "user_message", "role": "user", "content": "古い別タスク"})
            append_session_event(root, "main", {"type": "assistant_message", "role": "assistant", "content": "BROKEN_LONG_CODE"})
            append_session_event(root, "main", {"type": "observer_note", "role": "observer", "content": "実況解説の長文"})
            backend = FakeBackend(
                [
                    '{"assistant_message":"ok","tool_name":"finish","tool_args":{"final_answer":"ok"}}',
                ]
            )
            runtime = AgentRuntime(root, llm_backend=backend)
            runtime.send_message("現在のタスクだけ扱って", run_immediately=True)
            prompts = read_jsonl(WorkspacePaths(root).session_prompts_path("main"))
            prompt = prompts[-1]["prompt"]
            self.assertIn("現在のタスクだけ扱って", prompt)
            self.assertNotIn("BROKEN_LONG_CODE", prompt)
            self.assertNotIn("実況解説の長文", prompt)
            self.assertNotIn("古い別タスク", prompt)

    def test_status_snapshot_exposes_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bootstrap_workspace(root, force=True)
            backend = FakeBackend(
                [
                    '{"assistant_message":"Working on it","tool_name":"finish","tool_args":{"final_answer":"done"}}',
                ]
            )
            runtime = AgentRuntime(root, llm_backend=backend)
            runtime.send_message("hello", run_immediately=True)
            snapshot = runtime.status_snapshot()
            self.assertIn("recent_transcript", snapshot)
            self.assertGreaterEqual(len(snapshot["recent_transcript"]), 2)
            self.assertEqual(snapshot["last_reply"]["type"], "finish")

    def test_runtime_persists_turn_metadata_and_tool_observability(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bootstrap_workspace(root, force=True)
            backend = FakeBackend(
                [
                    '{"assistant_message":"Running command","tool_name":"run_command","tool_args":{"command":"printf hello"}}',
                    '{"assistant_message":"done","tool_name":"finish","tool_args":{"final_answer":"ok"}}',
                ]
            )
            runtime = AgentRuntime(root, llm_backend=backend)
            runtime.send_message("run a command", run_immediately=True)
            events = read_jsonl(WorkspacePaths(root).session_events_path("main"))
            tool_call = next(event for event in events if event["type"] == "tool_call")
            tool_result = next(event for event in events if event["type"] == "tool_result")
            self.assertTrue(tool_call["turn_id"])
            self.assertEqual(tool_call["turn_id"], tool_result["turn_id"])
            payload = json.loads(tool_result["content"])
            self.assertEqual(payload["tool"], "run_command")
            self.assertIn("duration_ms", payload)
            self.assertIn("cwd", payload)
            self.assertIn("shell", payload)
            meta = read_json(WorkspacePaths(root).session_meta_path("main"), fallback={})
            self.assertIn("last_tool_result", meta)
            self.assertIn("last_event_summary", meta)

    def test_runtime_retries_invalid_json_once_and_records_telemetry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bootstrap_workspace(root, force=True)
            backend = FakeBackend(
                [
                    "not json yet",
                    '{"assistant_message":"ok","tool_name":"finish","tool_args":{"final_answer":"done"}}',
                ]
            )
            runtime = AgentRuntime(root, llm_backend=backend)
            runtime.send_message("say hi", run_immediately=True)
            status = read_json(WorkspacePaths(root).runtime_status_path, fallback={})
            self.assertEqual(status["last_llm_attempt_count"], 2)
            self.assertIsNotNone(status["last_llm_duration_ms"])
            self.assertIn("last_llm_raw_preview", status)

    def test_runtime_tracks_current_stream_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bootstrap_workspace(root, force=True)
            backend = FakeBackend(
                [
                    '{"assistant_message":"hello","tool_name":"finish","tool_args":{"final_answer":"hello"}}',
                ]
            )
            runtime = AgentRuntime(root, llm_backend=backend)
            runtime.send_message("say hello", run_immediately=True)
            status = read_json(WorkspacePaths(root).runtime_status_path, fallback={})
            self.assertIn("current_stream_text", status)
            self.assertIn("last_llm_raw_preview", status)
            self.assertIn("current_phase", status)
            self.assertIsNone(status["current_started_at"])
            self.assertIsNotNone(status["current_finished_at"])

    def test_runtime_blocks_finish_when_requested_commands_not_executed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bootstrap_workspace(root, force=True)
            backend = FakeBackend(
                [
                    '{"assistant_message":"run pwd","tool_name":"run_command","tool_args":{"command":"pwd","shell":"bash"}}',
                    '{"assistant_message":"done","tool_name":"finish","tool_args":{"final_answer":"listed files"}}',
                    '{"assistant_message":"run ls","tool_name":"run_command","tool_args":{"command":"ls","shell":"bash"}}',
                    '{"assistant_message":"done","tool_name":"finish","tool_args":{"final_answer":"pwd and ls completed"}}',
                ]
            )
            runtime = AgentRuntime(root, llm_backend=backend)
            runtime._fast_path_envelope = lambda **_: None  # type: ignore[method-assign]
            runtime.send_message("pwd を実行して、その後 ls を実行して要約して", run_immediately=True)
            events = read_jsonl(WorkspacePaths(root).session_events_path("main"))
            system_notes = [event for event in events if event["type"] == "system_note"]
            self.assertTrue(any(note.get("code") == "finish_blocked" for note in system_notes))
            self.assertTrue(any(note.get("reason_code") == "missing_required_commands" for note in system_notes))
            finish = next(event for event in reversed(events) if event["type"] == "finish")
            self.assertIn("pwd:", finish["content"])
            self.assertIn("pwd", finish["content"])
            self.assertIn("ls", finish["content"])

    def test_run_command_rejects_chained_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bootstrap_workspace(root, force=True)
            config = read_json(WorkspacePaths(root).config_path, fallback={})
            config.setdefault("runtime", {})["max_steps_per_message"] = 1
            write_json(WorkspacePaths(root).config_path, config)
            backend = FakeBackend(
                [
                    '{"assistant_message":"run both","tool_name":"run_command","tool_args":{"command":"pwd && ls","shell":"bash"}}',
                ]
            )
            runtime = AgentRuntime(root, llm_backend=backend)
            runtime._fast_path_envelope = lambda **_: None  # type: ignore[method-assign]
            result = runtime.send_message("pwd を実行して、その後 ls を実行して", run_immediately=True)
            events = read_jsonl(WorkspacePaths(root).session_events_path("main"))
            tool_results = [event for event in events if event["type"] == "tool_result"]
            payloads = [json.loads(row["content"]) for row in tool_results]
            denied = next(payload for payload in payloads if not payload["ok"])
            self.assertIn("exactly one command per step", denied["error"])
            self.assertFalse(result["run"]["last_result"]["ok"])

    def test_runtime_records_reflection_after_failed_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bootstrap_workspace(root, force=True)
            config = read_json(WorkspacePaths(root).config_path, fallback={})
            config.setdefault("runtime", {})["max_steps_per_message"] = 1
            write_json(WorkspacePaths(root).config_path, config)
            backend = FakeBackend(
                [
                    '{"assistant_message":"done","tool_name":"finish","tool_args":{"final_answer":"`ghost-output` was seen"}}',
                ]
            )
            runtime = AgentRuntime(root, llm_backend=backend)
            result = runtime.send_message("結果を確認して", run_immediately=True)
            self.assertFalse(result["run"]["last_result"]["ok"])
            reflections = read_jsonl(WorkspacePaths(root).reflections_path)
            self.assertEqual(len(reflections), 1)
            self.assertIn("失敗パターン", reflections[0]["reflection"])
            self.assertIn("failure_class", reflections[0])
            status = read_json(WorkspacePaths(root).runtime_status_path, fallback={})
            self.assertIn("失敗パターン", str(status.get("last_reflection") or ""))

    def test_runtime_records_grounding_judge_trace_when_finish_is_blocked(self) -> None:
        original = AgentRuntime._semantic_grounding_check
        AgentRuntime._semantic_grounding_check = self.original_semantic_check  # type: ignore[assignment]
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                bootstrap_workspace(root, force=True)
                config = read_json(WorkspacePaths(root).config_path, fallback={})
                config.setdefault("runtime", {})["max_steps_per_message"] = 1
                write_json(WorkspacePaths(root).config_path, config)
                backend = FakeBackend(
                    [
                        '{"assistant_message":"done","tool_name":"finish","tool_args":{"final_answer":"ghost-output"}}',
                        "1. Analyze first, but never emit OK",
                    ]
                )
                runtime = AgentRuntime(root, llm_backend=backend)
                result = runtime.send_message("結果を確認して", run_immediately=True)
                self.assertFalse(result["run"]["last_result"]["ok"])
                events = read_jsonl(WorkspacePaths(root).session_events_path("main"))
                judge = next(event for event in events if event.get("code") == "grounding_judge")
                self.assertEqual(judge.get("reason_code"), "invalid_json")
                self.assertIn("INVALID_OUTPUT", judge["content"])
                self.assertEqual(judge["details"]["raw_response"], "1. Analyze first, but never emit OK")
                blocked = next(event for event in events if event.get("code") == "finish_blocked")
                self.assertEqual(blocked["reason_code"], "judge_invalid_output")
                self.assertEqual(blocked["details"]["judge"]["decision"], "invalid_json")
        finally:
            AgentRuntime._semantic_grounding_check = original  # type: ignore[assignment]

    def test_grounding_judge_accepts_structured_json_verdict(self) -> None:
        original = AgentRuntime._semantic_grounding_check
        AgentRuntime._semantic_grounding_check = self.original_semantic_check  # type: ignore[assignment]
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                bootstrap_workspace(root, force=True)
                runtime = AgentRuntime(
                    root,
                    llm_backend=FakeBackend([
                        '{"verdict":"ok","reason_code":"supported","unsupported_claims":[],"rationale":"stdoutと一致"}'
                    ]),
                )
                ok = runtime._semantic_grounding_check(
                    final_answer="cwd is /tmp",
                    evidence_text='{"stdout":"/tmp"}',
                    user_message="pwdを確認して",
                )
                self.assertTrue(ok)
                self.assertEqual(runtime._last_grounding_judge_trace["decision"], "ok")
                self.assertEqual(runtime._last_grounding_judge_trace["parsed"]["verdict"], "ok")
        finally:
            AgentRuntime._semantic_grounding_check = original  # type: ignore[assignment]

    def test_passive_observer_records_step_commentary_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bootstrap_workspace(root, force=True)
            config = read_json(WorkspacePaths(root).config_path, fallback={})
            config.setdefault("runtime", {})["max_steps_per_message"] = 1
            config.setdefault("runtime", {})["observer_enabled"] = True
            write_json(WorkspacePaths(root).config_path, config)
            backend = FakeBackend(
                [
                    '{"assistant_message":"run pwd","tool_name":"run_command","tool_args":{"command":"pwd","shell":"bash"}}',
                    "1. 起きたこと: pwdを実行しました。\n2. 気になる点: まだ完了していません。\n3. 次に確認すべきこと: 出力を回答に反映すること。",
                ]
            )
            runtime = AgentRuntime(root, llm_backend=backend)
            runtime._fast_path_envelope = lambda **_: None  # type: ignore[method-assign]
            runtime.send_message("pwdを実行して", run_immediately=True)
            events = read_jsonl(WorkspacePaths(root).session_events_path("main"))
            observer = next(event for event in events if event.get("type") == "observer_note")
            self.assertEqual(observer.get("code"), "live_commentator")
            self.assertIn("起きたこと", observer["content"])

    def test_observer_records_llm_output_issue_before_tool_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bootstrap_workspace(root, force=True)
            config = read_json(WorkspacePaths(root).config_path, fallback={})
            config.setdefault("runtime", {})["max_steps_per_message"] = 1
            config.setdefault("runtime", {})["observer_enabled"] = True
            config.setdefault("runtime", {})["json_retry_limit"] = 0
            write_json(WorkspacePaths(root).config_path, config)
            backend = FakeBackend(["I will create the maze script and run it."])
            runtime = AgentRuntime(root, llm_backend=backend)
            runtime.send_message("サンプルプログラムを作成して実行して表示して", run_immediately=True)
            events = read_jsonl(WorkspacePaths(root).session_events_path("main"))
            self.assertTrue(any(event.get("code") == "llm_output_issue" for event in events))
            observer = next(
                event
                for event in events
                if event.get("type") == "observer_note"
                and event.get("reason_code") == "llm_output_issue_commentary"
            )
            self.assertIn("tool_call JSON", observer["content"])

    def test_llm_output_issue_classifies_length_truncated_stream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bootstrap_workspace(root, force=True)
            config = read_json(WorkspacePaths(root).config_path, fallback={})
            config.setdefault("runtime", {})["max_steps_per_message"] = 1
            config.setdefault("runtime", {})["json_retry_limit"] = 0
            write_json(WorkspacePaths(root).config_path, config)
            backend = StreamingMetadataBackend(
                [
                    {"message": {"content": '{"assistant_message":"writing","tool_name":"write_file","tool_args":{"path":"maze_gen.py","content":"import random\\n'}},
                    {"done": True, "done_reason": "length", "eval_count": 384},
                ]
            )
            runtime = AgentRuntime(root, llm_backend=backend)
            runtime.send_message("maze_gen.py を作成して", run_immediately=True)
            status = read_json(WorkspacePaths(root).runtime_status_path, fallback={})
            self.assertEqual(status["last_llm_parse_issue"], "length_truncated")
            self.assertEqual(status["last_llm_stream_metadata"]["done_reason"], "length")
            events = read_jsonl(WorkspacePaths(root).session_events_path("main"))
            issue = next(event for event in events if event.get("code") == "llm_output_issue")
            self.assertEqual(issue["reason_code"], "length_truncated")

    def test_extract_requested_commands_handles_japanese_connectors_and_git(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bootstrap_workspace(root, force=True)
            runtime = AgentRuntime(root, llm_backend=FakeBackend([]))
            commands = runtime._extract_requested_commands("pwd を実行して、その後 git status を実行して、続けて ls を実行して")
            self.assertEqual(commands, ["pwd", "git status", "ls"])

    def test_terminal_model_falls_back_to_available_terminal_chain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bootstrap_workspace(root, force=True)
            backend = FakeBackend([], models=["qwen3-coder:latest", "gemma4:26b"])
            runtime = AgentRuntime(root, llm_backend=backend)
            selected = runtime._resolve_terminal_model("devstral:latest")
            self.assertEqual(selected, "qwen3-coder:latest")

    def test_runtime_blocks_repeated_successful_command_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bootstrap_workspace(root, force=True)
            backend = FakeBackend(
                [
                    '{"assistant_message":"run pwd","tool_name":"run_command","tool_args":{"command":"pwd","shell":"bash"}}',
                    '{"assistant_message":"run pwd again","tool_name":"run_command","tool_args":{"command":"pwd","shell":"bash"}}',
                    '{"assistant_message":"done","tool_name":"finish","tool_args":{"final_answer":"/tmp/path"}}',
                ]
            )
            runtime = AgentRuntime(root, llm_backend=backend)
            runtime._controller_terminal_finish = lambda **_: None  # type: ignore[method-assign]
            runtime.send_message("pwd を実行して、結果だけ返して", run_immediately=True)
            events = read_jsonl(WorkspacePaths(root).session_events_path("main"))
            system_notes = [event for event in events if event["type"] == "system_note"]
            self.assertTrue(any(note.get("code") == "command_blocked" for note in system_notes))
            self.assertTrue(any(note.get("reason_code") == "repeated_command" for note in system_notes))

    def test_runtime_blocks_repeated_failed_command_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bootstrap_workspace(root, force=True)
            backend = FakeBackend(
                [
                    '{"assistant_message":"run bad","tool_name":"run_command","tool_args":{"command":"false","shell":"bash"}}',
                    '{"assistant_message":"retry bad","tool_name":"run_command","tool_args":{"command":"false","shell":"bash"}}',
                    '{"assistant_message":"done","tool_name":"finish","tool_args":{"final_answer":"stopped after failure"}}',
                ]
            )
            runtime = AgentRuntime(root, llm_backend=backend)
            runtime._controller_terminal_finish = lambda **_: None  # type: ignore[method-assign]
            runtime.send_message("失敗するコマンドを実行して確認して", run_immediately=True)
            events = read_jsonl(WorkspacePaths(root).session_events_path("main"))
            system_notes = [event for event in events if event["type"] == "system_note"]
            self.assertTrue(any(note.get("code") == "command_blocked" for note in system_notes))
            self.assertTrue(any(note.get("reason_code") == "repeated_command" for note in system_notes))
            self.assertTrue(any(note.get("code") == "command_failed" for note in system_notes))
            self.assertTrue(any(note.get("reason_code") == "recovery_guidance" for note in system_notes))

    def test_terminal_fast_path_executes_first_requested_command_without_initial_llm_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bootstrap_workspace(root, force=True)
            backend = FakeBackend(
                [
                    '{"assistant_message":"done","tool_name":"finish","tool_args":{"final_answer":"ok"}}',
                ]
            )
            runtime = AgentRuntime(root, llm_backend=backend)
            runtime.run_terminal_agent("pwd を実行して、結果だけ返して", model="devstral:latest", shell_name="bash")
            events = read_jsonl(WorkspacePaths(root).session_events_path("main"))
            tool_call = next(event for event in events if event["type"] == "tool_call")
            self.assertEqual(tool_call["tool_name"], "run_command")
            self.assertEqual(tool_call["tool_args"]["command"], "pwd")

    def test_terminal_agent_uses_llm_for_maze_requests_instead_of_fixed_scaffold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bootstrap_workspace(root, force=True)
            backend = FakeBackend(
                [
                    '{"assistant_message":"writing","tool_name":"write_file","tool_args":{"path":"maze_gen.py","content":"print(\\"MODEL_MAZE\\")\\n"}}',
                    '{"assistant_message":"running","tool_name":"run_command","tool_args":{"command":"python3 maze_gen.py","shell":"bash"}}',
                ]
            )
            runtime = AgentRuntime(root, llm_backend=backend)
            result = runtime.run_terminal_agent("迷路を実装して実行し表示して結果を見せて", model="devstral:latest", shell_name="bash")
            self.assertTrue(result["run"]["last_result"]["ok"])
            status = read_json(WorkspacePaths(root).runtime_status_path, fallback={})
            workspace = Path(status["last_llm_workspace"])
            self.assertTrue((workspace / "maze_gen.py").exists())
            self.assertEqual((workspace / "maze_gen.py").read_text(), 'print("MODEL_MAZE")\n')
            events = read_jsonl(WorkspacePaths(root).session_events_path("main"))
            tool_calls = [event for event in events if event["type"] == "tool_call"]
            self.assertEqual([event["tool_name"] for event in tool_calls], ["write_file", "run_command"])
            self.assertTrue(any(event["type"] == "assistant_message" and "writing" in event["content"] for event in events))
            run_result = next(
                json.loads(event["content"])
                for event in events
                if event["type"] == "tool_result" and event["tool_name"] == "run_command"
            )
            self.assertTrue(run_result["ok"])
            self.assertIn("MODEL_MAZE", run_result["stdout"])

    def test_terminal_fast_path_continues_with_next_missing_command_before_llm_finish(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bootstrap_workspace(root, force=True)
            backend = FakeBackend(
                [
                    '{"assistant_message":"done","tool_name":"finish","tool_args":{"final_answer":"pwd and ls completed"}}',
                ]
            )
            runtime = AgentRuntime(root, llm_backend=backend)
            runtime.run_terminal_agent("pwd を実行して、その後 ls を実行して要約して", model="devstral:latest", shell_name="bash")
            events = read_jsonl(WorkspacePaths(root).session_events_path("main"))
            tool_calls = [event for event in events if event["type"] == "tool_call" and event["tool_name"] == "run_command"]
            self.assertEqual([event["tool_args"]["command"] for event in tool_calls], ["pwd", "ls"])
            finish = next(event for event in reversed(events) if event["type"] == "finish")
            self.assertIn("pwd:", finish["content"])
            self.assertIn("pwd", finish["content"])
            self.assertIn("ls", finish["content"])

    def test_terminal_controller_finish_uses_grounded_evidence_without_second_llm_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bootstrap_workspace(root, force=True)
            backend = FakeBackend(
                [
                    '{"assistant_message":"done","tool_name":"finish","tool_args":{"final_answer":"ignored"}}',
                ]
            )
            runtime = AgentRuntime(root, llm_backend=backend)
            runtime.run_terminal_agent("pwd を実行して、結果だけ短く返して", model="devstral:latest", shell_name="bash")
            events = read_jsonl(WorkspacePaths(root).session_events_path("main"))
            finish = next(event for event in reversed(events) if event["type"] == "finish")
            self.assertIn(str(root), finish["content"])
            system_notes = [event for event in events if event["type"] == "system_note"]
            self.assertTrue(any(note.get("code") == "controller_finish" for note in system_notes))
            self.assertEqual(len(backend.responses), 1)

    def test_terminal_controller_finish_handles_multi_command_grounding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bootstrap_workspace(root, force=True)
            backend = FakeBackend(
                [
                    '{"assistant_message":"done","tool_name":"finish","tool_args":{"final_answer":"ignored"}}',
                ]
            )
            runtime = AgentRuntime(root, llm_backend=backend)
            runtime.run_terminal_agent("pwd を実行して、その後 ls を実行して要約して", model="devstral:latest", shell_name="bash")
            events = read_jsonl(WorkspacePaths(root).session_events_path("main"))
            finish = next(event for event in reversed(events) if event["type"] == "finish")
            self.assertIn("pwd:", finish["content"])
            self.assertIn("ls:", finish["content"])

    def test_controller_finish_handles_git_status_then_pwd_without_false_unexecuted_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bootstrap_workspace(root, force=True)
            runtime = AgentRuntime(root, llm_backend=FakeBackend([]))
            steps = [
                {
                    "tool_name": "run_command",
                    "tool_result": {
                        "ok": True,
                        "tool": "run_command",
                        "command": "git status",
                        "shell": "bash",
                        "cwd": "/Users/satojunichi/Documents/openclaw",
                        "returncode": 0,
                        "stdout": "On branch main\nChanges not staged for commit:\n  modified: p3-core/p3_core/runtime.py\n",
                        "stderr": "",
                    },
                },
                {
                    "tool_name": "run_command",
                    "tool_result": {
                        "ok": True,
                        "tool": "run_command",
                        "command": "pwd",
                        "shell": "bash",
                        "cwd": "/Users/satojunichi/Documents/openclaw",
                        "returncode": 0,
                        "stdout": "/Users/satojunichi/Documents/openclaw\n",
                        "stderr": "",
                    },
                },
            ]
            answer = runtime._controller_terminal_finish(
                selection={"role": "terminal"},
                goal_text="",
                user_message="git status を実行して、その後 pwd を実行して、どのディレクトリで status を見たか短く返して",
                steps=steps,
            )
            self.assertIsNotNone(answer)
            self.assertIn("/Users/satojunichi/Documents/openclaw", str(answer))

    def test_finish_blocked_when_expected_artifact_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bootstrap_workspace(root, force=True)
            config = read_json(WorkspacePaths(root).config_path, fallback={})
            config.setdefault("runtime", {})["max_steps_per_message"] = 1
            write_json(WorkspacePaths(root).config_path, config)
            backend = FakeBackend(
                [
                    '{"assistant_message":"done","tool_name":"finish","tool_args":{"final_answer":"created maze_gen.py"}}',
                ]
            )
            runtime = AgentRuntime(root, llm_backend=backend)
            result = runtime.send_message("maze_gen.py を作成して", run_immediately=True)
            self.assertFalse(result["run"]["last_result"]["ok"])
            events = read_jsonl(WorkspacePaths(root).session_events_path("main"))
            system_notes = [event for event in events if event["type"] == "system_note"]
            self.assertTrue(any(note.get("code") == "finish_blocked" for note in system_notes))
            self.assertTrue(any(note.get("reason_code") == "missing_expected_artifacts" for note in system_notes))

    def test_successful_turn_clears_stale_runtime_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bootstrap_workspace(root, force=True)
            paths = WorkspacePaths(root)
            write_json(
                paths.runtime_status_path,
                {
                    **read_json(paths.runtime_status_path, fallback={}),
                    "status": "idle",
                    "last_error": "old error",
                    "last_system_note": "old note",
                },
            )
            backend = FakeBackend(
                [
                    '{"assistant_message":"done","tool_name":"finish","tool_args":{"final_answer":"ok"}}',
                ]
            )
            runtime = AgentRuntime(root, llm_backend=backend)
            runtime.send_message("short answer", run_immediately=True)
            status = read_json(paths.runtime_status_path, fallback={})
            self.assertIsNone(status.get("last_error"))
            self.assertIsNone(status.get("last_system_note"))

    def test_run_until_idle_clears_stale_current_fields_when_queue_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bootstrap_workspace(root, force=True)
            paths = WorkspacePaths(root)
            write_json(
                paths.runtime_status_path,
                {
                    **read_json(paths.runtime_status_path, fallback={}),
                    "status": "running",
                    "current_turn_id": "stale-turn",
                    "current_queue_id": "stale-queue",
                    "current_user_message": "stale message",
                    "current_prompt_preview": "stale prompt",
                    "current_stream_text": "stale stream",
                    "current_plan": "stale plan",
                    "current_phase": "DISCOVER_REQUIRED_COMMANDS",
                    "current_started_at": "2026-04-18T00:00:00Z",
                },
            )
            runtime = AgentRuntime(root, llm_backend=FakeBackend([]))
            result = runtime.run_until_idle()
            self.assertEqual(result["processed"], 0)
            status = read_json(paths.runtime_status_path, fallback={})
            self.assertEqual(status["status"], "idle")
            self.assertIsNone(status.get("current_turn_id"))
            self.assertIsNone(status.get("current_queue_id"))
            self.assertIsNone(status.get("current_user_message"))
            self.assertIsNone(status.get("current_prompt_preview"))
            self.assertEqual(status.get("current_stream_text"), "")
            self.assertIsNone(status.get("current_plan"))
            self.assertIsNone(status.get("current_phase"))
            self.assertIsNone(status.get("current_started_at"))


if __name__ == "__main__":
    unittest.main()
