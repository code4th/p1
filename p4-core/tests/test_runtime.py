from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from p4_core.runtime import AgentRuntime
from p4_core.schemas import FIRST_ACTION_CONTENT_MAX_LENGTH, PLAN_RECORD_SCHEMA, tool_action_schema
from p4_core.workspace import bootstrap_workspace, enqueue_message, read_jsonl


class FakeBackend:
    def __init__(self, responses: list[str], *, models: list[str] | None = None) -> None:
        self.responses = list(responses)
        self.models = list(models or ["test-model"])
        self.messages_seen: list[list[dict[str, str]]] = []

    def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        options: dict | None = None,
        timeout_seconds: int = 180,
    ) -> dict:
        self.messages_seen.append(messages)
        del model, options, timeout_seconds
        if not self.responses:
            raise AssertionError("no fake responses left")
        return {"content": self.responses.pop(0), "raw": {}}

    def chat_stream(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        options: dict | None = None,
        timeout_seconds: int = 180,
    ) -> list[dict]:
        self.messages_seen.append(messages)
        del model, options, timeout_seconds
        if not self.responses:
            raise AssertionError("no fake responses left")
        return [{"message": {"content": self.responses.pop(0)}, "done": True}]

    def list_models(self) -> dict:
        return {"models": [{"name": name} for name in self.models]}


class StreamingFakeBackend(FakeBackend):
    def __init__(self, chunks: list[str], *, models: list[str] | None = None) -> None:
        super().__init__([], models=models)
        self.chunks = list(chunks)

    def iter_chat_stream(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        options: dict | None = None,
        timeout_seconds: int = 180,
    ):
        self.messages_seen.append(messages)
        del model, options, timeout_seconds
        for chunk in self.chunks:
            yield {"message": {"content": chunk}, "done": False}
        yield {"message": {"content": ""}, "done": True}


def tool_step(tool_name: str, path: str = "", *, content: str = "", ok: bool = True, **result: object) -> dict:
    args: dict[str, object] = {}
    if path:
        args["path"] = path
    if content:
        args["content"] = content
    tool_result: dict[str, object] = {"ok": ok}
    if path:
        tool_result["path"] = path
    tool_result.update(result)
    return {"tool_name": tool_name, "tool_args": args, "tool_result": tool_result}


def run_step(
    command: str,
    *,
    ok: bool,
    stderr: str = "",
    stdout: str = "",
    failure_type: str = "",
    returncode: int | None = None,
) -> dict:
    tool_result: dict[str, object] = {
        "ok": ok,
        "command": command,
        "returncode": 0 if ok else (1 if returncode is None else returncode),
        "stdout": stdout,
        "stderr": stderr,
    }
    if failure_type:
        tool_result["failure_type"] = failure_type
    return {
        "tool_name": "run_command",
        "tool_args": {"command": command},
        "tool_result": tool_result,
    }


class GenericRuntimeContractTests(unittest.TestCase):
    def runtime(self, root: Path | None = None, responses: list[str] | None = None) -> AgentRuntime:
        if root is None:
            root = Path(tempfile.mkdtemp())
        bootstrap_workspace(root)
        return AgentRuntime(root, llm_backend=FakeBackend(responses or []))

    def state_space_plan(self, runtime: AgentRuntime, user_message: str | None = None) -> dict:
        profile = runtime._planning_profile_for_message(
            user_message
            or "4x4 sliding puzzle の状態、合法手、ゴール、探索方針を使って解く実装を作ってください。"
        )
        return {
            "plan_id": "plan-state-space",
            "profile": profile,
            "strategy": "state_space_search",
            "work_units": [
                {
                    "unit_id": "inspect-workspace",
                    "goal": "Inspect the current workspace before implementing the generic state-space solver.",
                    "depends_on": [],
                    "work_type": "edit",
                    "first_action": {"tool": "list_files", "args": {"path": "."}},
                    "success_evidence": "Workspace files are listed so implementation targets can be chosen.",
                    "should_open_child_frame": True,
                },
                {
                    "unit_id": "verify-state-space-solution",
                    "goal": "Run a final verifier that replays the legal action path and confirms it reaches the goal.",
                    "depends_on": ["inspect-workspace"],
                    "work_type": "run_test",
                    "first_action": {"tool": "run_command", "args": {"command": "python3 -m unittest discover -s tests"}},
                    "success_evidence": "final_verifier replays legal actions from the start state and reaches the goal.",
                    "should_open_child_frame": True,
                }
            ],
            "verification_contract": {
                "state": "state representation is explicit and serializable",
                "action": "moves are explicit actions",
                "goal": "goal predicate is checked",
                "legal_move_validator": "illegal moves are rejected",
                "solvability_check": "unsolvable inputs are rejected or reported",
                "heuristic_or_search_policy": "search policy is explicit",
                "final_verifier": "returned move sequence is replayed and reaches the goal",
            },
            "status": "proposed",
            "revision_count": 0,
        }

    def dynamic_programming_plan(self, runtime: AgentRuntime, user_message: str | None = None) -> dict:
        profile = runtime._planning_profile_for_message(
            user_message
            or "動的計画で部分問題、基底ケース、漸化式を使うPython実装とunittestを作ってください。"
        )
        return {
            "plan_id": "plan-dynamic-programming",
            "profile": profile,
            "strategy": "dynamic_programming",
            "work_units": [
                {
                    "unit_id": "inspect-workspace",
                    "goal": "Inspect the current workspace before implementing the generic dynamic-programming solver.",
                    "depends_on": [],
                    "work_type": "edit",
                    "first_action": {"tool": "list_files", "args": {"path": "."}},
                    "success_evidence": "Workspace files are listed so implementation targets can be chosen.",
                    "should_open_child_frame": True,
                },
                {
                    "unit_id": "verify-dp-contract",
                    "goal": "Run tests that verify base case behavior and recurrence/sample oracle behavior.",
                    "depends_on": ["inspect-workspace"],
                    "work_type": "run_test",
                    "first_action": {"tool": "run_command", "args": {"command": "python3 -m unittest discover -s tests"}},
                    "success_evidence": "base case assertions and recurrence/sample oracle assertions pass.",
                    "should_open_child_frame": True,
                },
            ],
            "verification_contract": {
                "subproblem_state": "subproblem state is explicit",
                "base_case_verifier": "base cases are verified",
                "recurrence_verifier": "recurrence or transition is verified",
                "evaluation_order_or_memoization": "iteration order or memoization is explicit",
                "sample_oracle": "sample oracle cases are asserted",
            },
            "status": "proposed",
            "revision_count": 0,
        }

    def constraint_satisfaction_plan(self, runtime: AgentRuntime, user_message: str | None = None) -> dict:
        profile = runtime._planning_profile_for_message(
            user_message
            or "制約、変数、ドメイン、割当を使うPython実装とunittestを作ってください。"
        )
        return {
            "plan_id": "plan-constraint-satisfaction",
            "profile": profile,
            "strategy": "constraint_satisfaction",
            "work_units": [
                {
                    "unit_id": "inspect-workspace",
                    "goal": "Inspect the current workspace before implementing the generic constraint solver.",
                    "depends_on": [],
                    "work_type": "edit",
                    "first_action": {"tool": "list_files", "args": {"path": "."}},
                    "success_evidence": "Workspace files are listed so implementation targets can be chosen.",
                    "should_open_child_frame": True,
                },
                {
                    "unit_id": "verify-constraint-contract",
                    "goal": "Run tests that validate constraint satisfaction and reject invalid negative assignments.",
                    "depends_on": ["inspect-workspace"],
                    "work_type": "run_test",
                    "first_action": {"tool": "run_command", "args": {"command": "python3 -m unittest discover -s tests"}},
                    "success_evidence": "constraint validator accepts a satisfying assignment and rejects an invalid negative assignment.",
                    "should_open_child_frame": True,
                },
            ],
            "verification_contract": {
                "variables": "variables are explicit",
                "domains": "domains are explicit",
                "constraints": "constraints are explicit",
                "constraint_checker": "individual constraints can be checked",
                "solution_validator": "complete assignments can be validated",
                "negative_case": "invalid assignments are rejected",
            },
            "status": "proposed",
            "revision_count": 0,
        }

    def test_runtime_contains_no_benchmark_task_specializations(self) -> None:
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted(Path("p4_core").rglob("*.py"))
        )
        forbidden = [
            "ExactCover",
            "Sudoku",
            "Pentomino",
            "WorldBetter",
            "Exact Cover",
            "exact_cover",
            "sudoku",
            "pentomino",
            "world_better",
            "world_improvement",
            "FILNPTUVWXYZ",
            "generate_candidates",
            "score_candidate",
            "select_task",
            "before_after_evaluation",
            "row_id",
            "column_id",
            "row_id -> set",
            "solve_one",
            "solve_all",
            "validate_solution",
            "make the world better",
            "improve the world",
            "better world",
            "15-puzzle",
            "puzzle15",
            "sliding puzzle",
            "tuple(range(1, 16))",
            "tuple(range(1, 17))",
            "knapsack",
            "edit distance",
        ]
        for marker in forbidden:
            self.assertNotIn(marker, source)

    def test_all_runtime_system_note_codes_are_prompt_visible(self) -> None:
        import re

        runtime_source = Path("p4_core/runtime.py").read_text(encoding="utf-8")
        prompt_source = Path("p4_core/prompts.py").read_text(encoding="utf-8")
        runtime_codes = set(re.findall(r'"code": "([^"]+)"', runtime_source))
        match = re.search(r"useful_system_codes = \{(.*?)\n    \}", prompt_source, re.S)
        self.assertIsNotNone(match)
        visible_codes = {item for item in re.findall(r'"([^"]*)"', match.group(1)) if item}

        self.assertEqual(sorted(runtime_codes - visible_codes), [])

    def test_failure_system_notes_include_actionable_recovery_contract(self) -> None:
        import ast

        runtime_source = Path("p4_core/runtime.py").read_text(encoding="utf-8")
        tree = ast.parse(runtime_source)
        failure_markers = ("blocked", "failed", "invalid", "required", "incomplete", "ignored", "interrupt")
        required_detail_keys = {
            "failure_type",
            "blocked_by",
            "allowed_next_actions",
            "suggested_fix",
            "next_required_action",
        }
        missing: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            pairs: dict[str, ast.AST] = {}
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    pairs[key.value] = value
            event_type = pairs.get("type")
            code = pairs.get("code")
            if not (
                isinstance(event_type, ast.Constant)
                and event_type.value == "system_note"
                and isinstance(code, ast.Constant)
                and isinstance(code.value, str)
                and any(marker in code.value for marker in failure_markers)
            ):
                continue
            details = pairs.get("details")
            detail_keys: set[str] = set()
            if isinstance(details, ast.Dict):
                for detail_key in details.keys:
                    if isinstance(detail_key, ast.Constant) and isinstance(detail_key.value, str):
                        detail_keys.add(detail_key.value)
            elif details is not None:
                detail_keys.add("<dynamic>")
            missing_keys = set() if "<dynamic>" in detail_keys else required_detail_keys - detail_keys
            if details is None or missing_keys:
                missing.append(f"line {node.lineno}: {code.value}: {sorted(missing_keys or required_detail_keys)}")

        self.assertEqual(missing, [])

    def test_plan_record_schema_limits_first_action_content_for_llm(self) -> None:
        args_schema = PLAN_RECORD_SCHEMA["properties"]["work_units"]["items"]["properties"]["first_action"]["properties"]["args"]
        tool_schema = PLAN_RECORD_SCHEMA["properties"]["work_units"]["items"]["properties"]["first_action"]["properties"]["tool"]

        self.assertEqual(args_schema["properties"]["content"]["maxLength"], FIRST_ACTION_CONTENT_MAX_LENGTH)
        self.assertEqual(args_schema["properties"]["new_text"]["maxLength"], FIRST_ACTION_CONTENT_MAX_LENGTH)
        self.assertEqual(args_schema["properties"]["old_text"]["maxLength"], FIRST_ACTION_CONTENT_MAX_LENGTH)
        self.assertNotIn("write_file", tool_schema["enum"])
        self.assertIn("list_files", tool_schema["enum"])

    def test_create_plan_tool_schema_exposes_nested_plan_record_contract(self) -> None:
        schema = tool_action_schema(allowed_tool_names=["create_plan"])
        tool_args = schema["properties"]["tool_args"]
        first_action_tool = (
            tool_args["properties"]["plan"]["properties"]["work_units"]["items"]["properties"]["first_action"]["properties"]["tool"]
        )

        self.assertEqual(schema["properties"]["tool_name"]["enum"], ["create_plan"])
        self.assertEqual(tool_args["required"], ["plan"])
        self.assertNotIn("write_file", first_action_tool["enum"])
        self.assertIn("list_files", first_action_tool["enum"])

    def test_planning_stream_guard_aborts_embedded_edit_before_large_code_dump(self) -> None:
        runtime = self.runtime()
        partial_plan = (
            '{"tool_name":"create_plan","tool_args":{"plan":{"plan_id":"p",'
            '"profile":{"strategy":"state_space_search"},"strategy":"state_space_search",'
            '"work_units":[{"unit_id":"impl","goal":"write implementation","depends_on":[],'
            '"work_type":"edit","first_action":{"tool":"write_file","args":{"path":"solver.py","content":"'
        )

        reason = runtime._machine_control_stream_stop_reason(
            content_text=partial_plan,
            thinking_text="",
            max_stream_chars=24000,
            schema=PLAN_RECORD_SCHEMA,
            current_phase="PLANNING_REQUIRED",
        )

        self.assertEqual(reason, "plan_record_embedded_edit_stream")

    def test_planning_stream_guard_does_not_abort_state_space_signal_prefix(self) -> None:
        runtime = self.runtime()
        partial_plan = (
            '{\n'
            '  "tool_name": "create_plan",\n'
            '  "tool_args": {"plan": {"plan_id": "p", "profile": {"complexity": "complex", "signals": [\n'
            '    "state_space:状態", "state_space:ゴール", "state_space:合法手", "state_space:探索", "state_space'
        )

        reason = runtime._machine_control_stream_stop_reason(
            content_text=partial_plan,
            thinking_text="",
            max_stream_chars=24000,
            schema=PLAN_RECORD_SCHEMA,
            current_phase="PLANNING_REQUIRED",
        )

        self.assertEqual(reason, "")

    def test_plan_record_embedded_edit_stream_feedback_is_specific_not_length_repair(self) -> None:
        runtime = self.runtime()
        metadata = {"client_abort_reason": "plan_record_embedded_edit_stream"}

        issue = runtime._classify_llm_parse_issue(
            raw_text='{"tool_name":"create_plan","tool_args":{"plan":{"work_units":[{"first_action":{"tool":"write_file",',
            thinking_text="",
            envelope={},
            stream_metadata=metadata,
            schema=PLAN_RECORD_SCHEMA,
        )
        prompt = runtime._json_repair_prompt(
            parse_target_text="",
            stream_metadata=metadata,
            schema_errors=["$.tool_name: value 'finish' is not in enum ['create_plan']"],
            allowed_tool_names=["create_plan"],
        )

        self.assertEqual(issue, "plan_record_embedded_edit_stream")
        self.assertTrue(
            runtime._parse_issue_should_exit_repair_loop(
                parse_issue=issue,
                stream_metadata=metadata,
                current_phase="PLANNING_REQUIRED",
            )
        )
        self.assertIn("not a length problem", prompt)
        self.assertIn("Do not put implementation edits inside create_plan", prompt)
        self.assertIn("list_files, read_file, search_code, or run_command", prompt)

    def test_repeated_plan_record_embedded_stream_autorepairs_minimal_plan(self) -> None:
        runtime = self.runtime()
        user_message = "4x4 sliding puzzle を状態、合法手、ゴール、探索方針で解く実装を作ってください。"
        for _ in range(3):
            runtime._append_session_event(
                "main",
                {
                    "type": "system_note",
                    "role": "system",
                    "content": "LLM応答がツール呼び出しJSONとして解釈できませんでした: plan_record_embedded_edit_stream",
                    "code": "llm_output_issue",
                    "reason_code": "plan_record_embedded_edit_stream",
                    "details": {"failure_type": "plan_record_embedded_edit_stream"},
                },
            )

        result = runtime._auto_create_minimal_plan_after_repeated_stream_issue(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=3,
            turn_workspace=runtime.execution_root,
            user_message=user_message,
            current_phase="PLANNING_REQUIRED",
            steps=[],
            current_model="fake",
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertTrue(result["ok"], result.get("error"))
        events = read_jsonl(runtime.paths.session_events_path("main"))
        self.assertTrue(any(event.get("code") == "plan_record_autorepaired" for event in events))
        plan_events = [event for event in events if event.get("type") == "plan_record"]
        self.assertTrue(plan_events)
        plan = plan_events[-1]["plan"]
        self.assertEqual(plan["strategy"], "state_space_search")
        edit_units = [unit for unit in plan["work_units"] if unit["work_type"] == "edit"]
        self.assertTrue(edit_units)
        self.assertEqual(edit_units[0]["first_action"]["tool"], "list_files")
        self.assertTrue(any(event.get("type") == "task_plan" for event in events))

    def test_implementation_missing_requires_python_write_first(self) -> None:
        runtime = self.runtime()
        message = "Pythonで未知の文字列整形ツールを実装し、tests/ にunittestを追加して検証してください。"
        state = runtime._implementation_task_progress_state(user_message=message, steps=[])
        self.assertEqual(state["phase"], "implementation_missing")
        self.assertEqual(state["allowed_next_actions"], ["write_file <implementation>.py"])
        prompt = runtime._implementation_task_progress_prompt(state)
        self.assertIn("小さくても完全に動く単一", prompt)
        self.assertIn("module-level def", prompt)
        self.assertIn("未完成chunk", prompt)

        blocked = runtime._implementation_task_phase_action_block(
            user_message=message,
            tool_name="write_file",
            tool_args={"path": "tests/test_tool.py", "content": "import unittest\n"},
            steps=[],
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertEqual(blocked["reason_code"], "implementation_task_phase_requires_initial_implementation")

    def test_placeholder_implementation_cannot_advance_to_tests_or_finish(self) -> None:
        runtime = self.runtime()
        message = "Pythonで normalize_name(text) を実装し、tests/ にunittestを追加して検証してください。"
        impl = "def normalize_name(text):\n    pass\n"
        steps = [tool_step("write_file", "name_tools.py", content=impl)]

        state = runtime._implementation_task_progress_state(user_message=message, steps=steps)
        self.assertEqual(state["phase"], "implementation_present_but_placeholder")

        blocked = runtime._implementation_task_phase_action_block(
            user_message=message,
            tool_name="write_file",
            tool_args={"path": "tests/test_name_tools.py", "content": "import unittest\n"},
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertEqual(blocked["reason_code"], "implementation_task_phase_requires_placeholder_fix")

    def test_placeholder_phase_prompt_carries_exact_local_repair_anchor(self) -> None:
        runtime = self.runtime()
        message = "Pythonで未知の探索ツールを実装し、tests/ にunittestを追加して検証してください。"
        impl = (
            "class Solver:\n"
            "    def __init__(self):\n"
            "        pass\n"
            "\n"
            "    def solve(self, state):\n"
            "        return [state]\n"
        )
        steps = [tool_step("write_file", "solver.py", content=impl)]

        state = runtime._implementation_task_progress_state(user_message=message, steps=steps)
        self.assertEqual(state["phase"], "implementation_present_but_placeholder")
        hints = state["implementation_source_repair_hints"]
        self.assertEqual(hints[0]["suggested_action"], "replace_placeholder_callable")
        self.assertIn("def __init__(self):", hints[0]["suggested_old_text"])
        self.assertIn("pass", hints[0]["suggested_old_text"])

        prompt = runtime._implementation_task_progress_prompt(state)
        self.assertIn("replace_placeholder_callable", prompt)
        self.assertIn("suggested_old_text", prompt)
        self.assertIn("before editing unrelated functions or tests", prompt)

    def test_implementation_prompt_steps_omit_prior_large_write_content(self) -> None:
        runtime = self.runtime()
        large_source = "def solve():\n" + "    return 1\n" * 500
        steps = [
            tool_step("write_file", "solver.py", content=large_source),
            tool_step("read_file", "old.py"),
            tool_step("read_file", "solver.py"),
        ]
        steps[1]["tool_result"]["content"] = "old content"
        steps[2]["tool_result"]["content"] = "current content"

        compacted = runtime._implementation_task_prompt_steps(steps=steps, state={})

        self.assertIn("<omitted", compacted[0]["tool_args"]["content"])
        self.assertIn("prior write_file content", compacted[0]["tool_args"]["content"])
        self.assertEqual(compacted[1]["tool_result"]["content"], "old content")
        self.assertEqual(compacted[2]["tool_result"]["content"], "current content")

    def test_failed_unittest_prompt_steps_compact_latest_large_reads(self) -> None:
        runtime = self.runtime()
        large_read = "\n".join(f"line {index}: x = {index}" for index in range(180))
        steps = [
            tool_step("read_file", "tests/test_solver.py"),
            tool_step("read_file", "solver.py"),
        ]
        steps[0]["tool_result"]["content"] = large_read
        steps[1]["tool_result"]["content"] = large_read

        default_compacted = runtime._implementation_task_prompt_steps(
            steps=steps,
            state={"phase": "tests_missing"},
        )
        failed_compacted = runtime._implementation_task_prompt_steps(
            steps=steps,
            state={"phase": "unittest_failed_needs_fix"},
        )

        self.assertEqual(default_compacted[0]["tool_result"]["content"], large_read)
        self.assertEqual(default_compacted[1]["tool_result"]["content"], large_read)
        self.assertLess(len(failed_compacted[0]["tool_result"]["content"]), len(large_read))
        self.assertLess(len(failed_compacted[1]["tool_result"]["content"]), len(large_read))
        self.assertIn("truncated", failed_compacted[0]["tool_result"]["content"])
        self.assertIn("truncated", failed_compacted[1]["tool_result"]["content"])

    def test_failed_unittest_prompt_makes_progress_gate_authoritative_inside_plan_child(self) -> None:
        runtime = self.runtime()
        user_message = "状態、合法手、ゴール、探索方針で解く小さなパズル実装とunittestを作ってください。"
        root = runtime.frame_manager.create_root_frame(user_message)
        runtime.frame_manager.register_child_task(
            parent=root,
            task={
                "task_id": "task-run-test",
                "goal": "Run final verifier",
                "work_type": "run_test",
                "first_action": {
                    "tool": "run_command",
                    "args": {"command": "python3 -m unittest discover -s tests"},
                },
                "success_evidence": "final verifier passes",
                "context_summary": "planner_strategy=state_space_search; profile_strategy=state_space_search",
                "why_not_direct_action": "PlanRecord requires this WorkUnit to run under the existing child-frame contract.",
            },
        )
        runtime.frame_manager.open_child_frame(
            "Run final verifier",
            {
                "child_task_id": "task-run-test",
                "context_summary": "planner_strategy=state_space_search; profile_strategy=state_space_search",
            },
        )

        prompt = runtime._build_prompt(
            goal_text="",
            recent_events=[],
            steps=[],
            current_phase="IMPLEMENTATION_TASK_PROGRESS:unittest_failed_needs_fix",
            user_message=user_message,
            suppress_frame_operations=True,
        )

        self.assertIn("implementation task progress gate が未完了", prompt)
        self.assertIn("allowed_next_actions", prompt)
        self.assertIn("read_file once / targeted edit / unittest再実行", prompt)
        self.assertNotIn("run_test なら run_command", prompt)

    def test_failed_unittest_prompt_context_keeps_only_failure_and_latest_reads(self) -> None:
        runtime = self.runtime()
        state = {"phase": "unittest_failed_needs_fix"}
        events = [
            {"type": "planning_note", "content": "large plan text"},
            {
                "type": "tool_result",
                "tool_name": "write_file",
                "content": json.dumps({"ok": True, "path": "solver.py", "content": "X" * 5000}),
            },
            {
                "type": "tool_result",
                "tool_name": "run_command",
                "content": json.dumps(
                    {
                        "ok": False,
                        "command": "python3 -m unittest discover -s tests",
                        "stderr": "FAILED (failures=1)",
                    }
                ),
            },
            {
                "type": "tool_result",
                "tool_name": "read_file",
                "content": json.dumps({"ok": True, "path": "solver.py", "content": "def solve():\n    return 1\n"}),
            },
        ]

        compact_events = runtime._implementation_task_prompt_events(recent_events=events, state=state)
        compact_steps_before_read = runtime._implementation_task_prompt_steps(
            steps=[run_step("python3 -m unittest discover -s tests", ok=False, stderr="FAILED")],
            state=state,
        )
        compact_steps_after_read = runtime._implementation_task_prompt_steps(
            steps=[
                run_step("python3 -m unittest discover -s tests", ok=False, stderr="FAILED"),
                tool_step("read_file", "solver.py", ok=True, content="def solve():\n    return 1\n"),
            ],
            state=state,
        )

        self.assertEqual([event["tool_name"] for event in compact_events], ["run_command", "read_file"])
        self.assertEqual(compact_steps_before_read, [])
        self.assertEqual([step["tool_name"] for step in compact_steps_after_read], ["read_file"])

    def test_progress_gate_overrides_plan_revision_after_unittest_material_exists(self) -> None:
        runtime = self.runtime()
        state = {
            "applicable": True,
            "contract_state": "incomplete",
            "phase": "unittest_failed_needs_fix",
            "implementation_paths": ["solver.py"],
            "test_paths": ["tests/test_solver.py"],
            "unittest_run": True,
        }

        self.assertEqual(
            runtime._implementation_task_effective_phase(fallback_phase="PLAN_REVISION", state=state),
            "IMPLEMENTATION_TASK_PROGRESS:unittest_failed_needs_fix",
        )
        self.assertEqual(
            runtime._implementation_task_effective_phase(fallback_phase="PLANNING_REQUIRED", state=state),
            "IMPLEMENTATION_TASK_PROGRESS:unittest_failed_needs_fix",
        )
        self.assertEqual(
            runtime._implementation_task_effective_phase(
                fallback_phase="PLANNING_REQUIRED",
                state={
                    "applicable": True,
                    "contract_state": "incomplete",
                    "phase": "implementation_missing",
                    "implementation_paths": [],
                    "test_paths": [],
                    "unittest_run": False,
                },
            ),
            "PLANNING_REQUIRED",
        )

    def test_placeholder_phase_blocks_huge_replace_text_and_allows_write_file(self) -> None:
        runtime = self.runtime()
        message = "Pythonで normalize_name(text) を実装し、tests/ にunittestを追加して検証してください。"
        impl = "def normalize_name(text):\n    pass\n"
        fixed = (
            "def normalize_name(text):\n"
            "    return ' '.join(str(text).strip().split()).title()\n"
            + "\n".join(f"# filler {index}" for index in range(180))
            + "\n"
        )
        steps = [tool_step("write_file", "name_tools.py", content=impl)]
        (runtime.execution_root / "name_tools.py").write_text(impl, encoding="utf-8")

        blocked = runtime._implementation_task_phase_action_block(
            user_message=message,
            tool_name="replace_text",
            tool_args={"path": "name_tools.py", "old_text": impl, "new_text": fixed},
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertEqual(blocked["reason_code"], "implementation_task_placeholder_blocks_broad_replace_text")
        self.assertEqual(blocked["allowed_next_actions"][0], "write_file name_tools.py")
        self.assertTrue(blocked["broad_rewrite"])

        allowed_write = runtime._implementation_task_phase_action_block(
            user_message=message,
            tool_name="write_file",
            tool_args={"path": "name_tools.py", "content": fixed},
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertIsNone(allowed_write)

    def test_placeholder_phase_allows_small_targeted_replace_text(self) -> None:
        runtime = self.runtime()
        message = "Pythonで normalize_name(text) を実装し、tests/ にunittestを追加して検証してください。"
        impl = "def normalize_name(text):\n    pass\n"
        steps = [tool_step("write_file", "name_tools.py", content=impl)]
        (runtime.execution_root / "name_tools.py").write_text(impl, encoding="utf-8")

        allowed = runtime._implementation_task_phase_action_block(
            user_message=message,
            tool_name="replace_text",
            tool_args={
                "path": "name_tools.py",
                "old_text": "    pass\n",
                "new_text": "    return str(text).strip().title()\n",
            },
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertIsNone(allowed)

    def test_placeholder_reject_carries_recovery_contract_signature(self) -> None:
        runtime = self.runtime()
        issue = runtime._python_artifact_contract_issue(
            user_message="Pythonで normalize_name(text) を実装し、tests/ にunittestを追加して検証してください。",
            tool_name="write_file",
            tool_args={"path": "name_tools.py", "content": "def normalize_name(text):\n    pass\n"},
        )
        self.assertIsNotNone(issue)
        assert issue is not None
        self.assertEqual(issue["reason_code"], "python_artifact_contract_incomplete")
        self.assertEqual(issue["recovery_class"], "contract_reducing_full_implementation_required")
        self.assertTrue(str(issue["block_signature"]).startswith("placeholder:"))
        self.assertIn("normalize_name", "\n".join(issue["placeholder_markers"]))
        self.assertIn("全callable", issue["suggested_fix"])

    def test_completion_recovery_does_not_run_unittest_while_placeholder_blocks_progress(self) -> None:
        runtime = self.runtime()
        message = "Pythonで normalize_name(value) を実装し、tests/ にunittestを追加して検証してください。"
        impl = "def normalize_name(value):\n    pass\n"
        test = (
            "import unittest\nfrom name_tools import normalize_name\n\n"
            "class TestNameTools(unittest.TestCase):\n"
            "    def test_normalize_name(self):\n"
            "        self.assertEqual(normalize_name(' Alice '), 'alice')\n"
        )
        steps = [
            tool_step("write_file", "name_tools.py", content=impl),
            tool_step("write_file", "tests/test_name_tools.py", content=test),
        ]

        state = runtime._implementation_task_progress_state(
            user_message=message,
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertEqual(state["phase"], "implementation_present_but_placeholder")

        recovery = runtime._completion_contract_recovery_action(
            session_id="main",
            user_message=message,
            steps=steps,
            step_index=2,
            max_steps=4,
            turn_workspace=runtime.execution_root,
        )

        self.assertIsNone(recovery)

    def test_completion_recovery_runs_unittest_only_when_progress_surface_allows_it(self) -> None:
        runtime = self.runtime()
        message = "Pythonで normalize_name(value) を実装し、tests/ にunittestを追加して検証してください。"
        impl = "def normalize_name(value):\n    return str(value).strip().lower()\n"
        test = (
            "import unittest\nfrom name_tools import normalize_name\n\n"
            "class TestNameTools(unittest.TestCase):\n"
            "    def test_normalize_name(self):\n"
            "        self.assertEqual(normalize_name(' Alice '), 'alice')\n"
        )
        steps = [
            tool_step("write_file", "name_tools.py", content=impl),
            tool_step("write_file", "tests/test_name_tools.py", content=test),
        ]

        state = runtime._implementation_task_progress_state(
            user_message=message,
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertEqual(state["phase"], "unittest_not_run")

        recovery = runtime._completion_contract_recovery_action(
            session_id="main",
            user_message=message,
            steps=steps,
            step_index=2,
            max_steps=4,
            turn_workspace=runtime.execution_root,
        )

        self.assertIsNotNone(recovery)
        assert recovery is not None
        self.assertEqual(recovery["reason_code"], "completion_contract_unittest_recovery")

    def test_initial_semantic_revision_prompt_requires_full_non_stub_implementation(self) -> None:
        runtime = self.runtime()
        prompt = runtime._implementation_task_progress_prompt(
            {
                "applicable": True,
                "phase": "implementation_missing_needs_semantic_revision",
                "contract_state": "incomplete",
                "missing_requirements": ["reviewed_implementation_strategy_not_applied"],
                "allowed_next_actions": ["write_file <implementation>.py"],
                "implementation_paths": [],
                "test_paths": [],
                "implementation_source_issues": [],
                "semantic_review_issues": ["公開関数がありません。"],
            }
        )
        self.assertIn("placeholder-free", prompt)
        self.assertIn("pass/TODO/ellipsis/NotImplementedError", prompt)
        self.assertIn("module-level def", prompt)
        self.assertIn("骨組み", prompt)

    def test_japanese_named_public_functions_require_module_level_defs(self) -> None:
        runtime = self.runtime()
        message = (
            "Pythonで汎用選択ツールを実装してください。"
            "pick_one は候補を1つ返してください。"
            "pick_all は全候補を返してください。"
            "validate_choice は候補が有効か検証してください。"
            "stats_report は top-level の counters を含む統計を返してください。"
            "入力は item_id -> set(feature_id) の辞書形式です。"
            "tests/ にunittestを追加して検証してください。"
        )
        impl = (
            "class Picker:\n"
            "    def pick_one(self, rows):\n"
            "        return next(iter(rows), None)\n"
            "    def pick_all(self, rows):\n"
            "        return list(rows)\n"
            "    def validate_choice(self, rows, choice):\n"
            "        return choice in rows\n"
            "    def stats_report(self, rows):\n"
            "        return {'counters': len(rows)}\n"
        )
        steps = [tool_step("write_file", "picker.py", content=impl)]

        requested = runtime._requested_top_level_function_names(message)
        self.assertEqual(requested, ["pick_one", "pick_all", "validate_choice", "stats_report"])
        state = runtime._implementation_task_progress_state(user_message=message, steps=steps)
        self.assertEqual(state["phase"], "implementation_present_needs_semantic_review")
        issue_text = "\n".join(state["implementation_source_issues"])
        self.assertIn("pick_one", issue_text)
        self.assertIn("pick_all", issue_text)
        self.assertIn("validate_choice", issue_text)
        self.assertIn("stats_report", issue_text)
        self.assertNotIn("item_id", issue_text)
        self.assertNotIn("feature_id", issue_text)

    def test_requested_public_functions_ignore_runtime_tool_names(self) -> None:
        runtime = self.runtime()
        message = (
            "Pythonで15パズルを実装してください。read_file は必要最小限にし、"
            "次は replace_text または write_file で修正し、run_command でunittestを実行してください。"
            "solve(start, goal) と create_near_goal_fixture(goal, moves) は公開APIです。"
        )

        requested = runtime._requested_top_level_function_names(message)

        self.assertIn("solve", requested)
        self.assertIn("create_near_goal_fixture", requested)
        self.assertNotIn("read_file", requested)
        self.assertNotIn("replace_text", requested)
        self.assertNotIn("write_file", requested)
        self.assertNotIn("run_command", requested)

    def test_requested_public_functions_ignore_formula_helper_calls(self) -> None:
        runtime = self.runtime()
        message = (
            "Pythonで汎用状態探索ライブラリを作成してください。"
            "公開APIは solve(start, goal), final_verifier(start, goal, actions), "
            "solvability_check(start, goal) です。"
            "重要: solvability_checkは ((inversions(start) + blank_row_from_bottom(start)) % 2) "
            "== ((inversions(goal) + blank_row_from_bottom(goal)) % 2) で判定してください。"
            "blank_row_from_bottomは下から1始まりです。"
        )

        requested = runtime._requested_top_level_function_names(message)

        self.assertEqual(requested, ["solve", "final_verifier", "solvability_check"])
        self.assertNotIn("inversions", requested)
        self.assertNotIn("blank_row_from_bottom", requested)

    def test_mutating_caller_owned_input_contract_is_parameter_name_generic(self) -> None:
        runtime = self.runtime()
        message = (
            "Pythonで choose_options(data) を実装してください。"
            "入力は dict[str, set[str]] で、caller-owned input を壊さないでください。"
        )
        source = (
            "def choose_options(data):\n"
            "    alias = data\n"
            "    alias.pop('seen', None)\n"
            "    return []\n"
        )

        issue = runtime._python_source_mutates_requested_input_collections(
            user_message=message,
            source=source,
        )

        self.assertIn("caller-owned public API 入力", issue)
        self.assertNotIn("rows", issue)

    def test_requested_public_functions_must_be_exercised_directly_by_tests(self) -> None:
        runtime = self.runtime()
        message = (
            "Pythonで汎用選択ツールを実装してください。"
            "pick_one は候補を1つ返してください。"
            "pick_all は全候補を返してください。"
            "validate_choice は候補が有効か検証してください。"
            "tests/ にunittestを追加して検証してください。"
        )
        impl = (
            "def pick_one(rows):\n"
            "    return next(iter(rows), None)\n"
            "def pick_all(rows):\n"
            "    return list(rows)\n"
            "def validate_choice(rows, choice):\n"
            "    return choice in rows\n"
        )
        test = (
            "import unittest\n\n"
            "class Picker:\n"
            "    def pick_one(self, rows):\n"
            "        return None\n"
            "    def pick_all(self, rows):\n"
            "        return []\n"
            "    def validate_choice(self, rows, choice):\n"
            "        return False\n\n"
            "class TestPicker(unittest.TestCase):\n"
            "    def test_picker_class(self):\n"
            "        picker = Picker()\n"
            "        self.assertIsNone(picker.pick_one({}))\n"
            "        self.assertEqual(picker.pick_all({}), [])\n"
            "        self.assertFalse(picker.validate_choice({}, 'x'))\n"
        )
        issue = runtime._requested_top_level_api_test_issue(
            user_message=message,
            test_sources=[("tests/test_picker.py", test)],
        )
        self.assertIn("pick_one", issue)
        self.assertIn("pick_all", issue)
        self.assertIn("validate_choice", issue)
        clean_issue = runtime._requested_top_level_api_test_issue(
            user_message=message,
            test_sources=[
                (
                    "tests/test_picker.py",
                    (
                        "import unittest\nfrom picker import pick_one, pick_all, validate_choice\n\n"
                        "class TestPicker(unittest.TestCase):\n"
                        "    def test_public_api(self):\n"
                        "        rows = {'a': {1}}\n"
                        "        self.assertEqual(pick_one(rows), 'a')\n"
                        "        self.assertEqual(pick_all(rows), ['a'])\n"
                        "        self.assertTrue(validate_choice(rows, 'a'))\n"
                    ),
                )
            ],
        )
        self.assertEqual(clean_issue, "")
        contract_issues = runtime._semantic_implementation_contract_issues(
            user_message=message,
            implementation_sources=[("picker.py", impl)],
            test_sources=[("tests/test_picker.py", test)],
        )
        self.assertTrue(any("top-level public API" in item for item in contract_issues))

    def test_identifier_mapping_contract_is_generic_not_task_named(self) -> None:
        runtime = self.runtime()
        message = (
            "Pythonで汎用mapping処理を実装してください。"
            "入力は item_id -> set(feature_id) の辞書形式です。"
            "select_item はIDを壊さず候補を返してください。"
            "tests/ にunittestを追加して検証してください。"
        )
        source = (
            "from typing import Dict, Set, List\n\n"
            "def select_item(rows: Dict[int, Set[int]]) -> List[int]:\n"
            "    return list(range(len(rows)))\n"
        )
        issue = runtime._python_source_narrows_requested_input_contract(
            user_message=message,
            source=source,
        )
        self.assertIn("IDを保持するmapping入力", issue)
        self.assertNotIn("item_id", issue)
        self.assertNotIn("feature_id", issue)

    def test_identifier_mapping_semantic_repair_is_read_once_then_targeted_edit(self) -> None:
        runtime = self.runtime()
        message = (
            "Pythonで汎用mapping処理を実装してください。"
            "入力は item_id -> set(feature_id) の辞書形式です。"
            "select_item はIDを壊さず候補を返してください。"
            "tests/ にunittestを追加して検証してください。"
        )
        source = (
            "from typing import Dict, Set, List\n\n"
            "def select_item(rows: Dict[int, Set[int]]) -> List[int]:\n"
            "    return list(range(len(rows)))\n"
        )
        steps = [tool_step("write_file", "picker.py", content=source)]

        state = runtime._implementation_task_progress_state(user_message=message, steps=steps)
        self.assertEqual(state["phase"], "implementation_present_needs_semantic_review")
        self.assertEqual(state["allowed_next_actions"], ["read_file picker.py once"])
        self.assertFalse(state["implementation_read_consumed"])

        steps.append(tool_step("read_file", "picker.py", content=source))
        state = runtime._implementation_task_progress_state(user_message=message, steps=steps)
        self.assertEqual(state["allowed_next_actions"], ["replace_text picker.py"])
        self.assertTrue(state["implementation_read_consumed"])
        prompt = runtime._implementation_task_progress_prompt(state)
        self.assertIn("mapping入力のID", prompt)
        self.assertIn("小さい replace_text", prompt)
        self.assertIn("残っている具体修復箇所", prompt)
        self.assertIn("def select_item(rows: Dict[int, Set[int]]) -> List[int]:", prompt)
        self.assertIn("suggested_new_text", prompt)

        (runtime.execution_root / "picker.py").write_text(source, encoding="utf-8")
        blocked = runtime._implementation_task_phase_action_block(
            user_message=message,
            tool_name="write_file",
            tool_args={"path": "picker.py", "content": source},
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertEqual(blocked["reason_code"], "implementation_task_phase_requires_contract_reducing_edit")
        self.assertEqual(blocked["allowed_next_actions"], ["replace_text picker.py"])
        self.assertIn("提案後も残る未達", blocked["message"])
        self.assertIn("def select_item(rows: Dict[int, Set[int]]) -> List[int]:", blocked["repair_hints"][0]["current_text"])

        partial_reduction = runtime._implementation_task_phase_action_block(
            user_message=message,
            tool_name="replace_text",
            tool_args={
                "path": "picker.py",
                "old_text": "def select_item(rows: Dict[int, Set[int]]) -> List[int]:",
                "new_text": "def select_item(rows: Dict[Any, Set[Any]]) -> List[int]:",
            },
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertIsNone(partial_reduction)

    def test_identifier_mapping_semantic_prompt_updates_remaining_repair_hint(self) -> None:
        runtime = self.runtime()
        message = (
            "Pythonで汎用mapping処理を実装してください。"
            "入力は item_id -> set(feature_id) の辞書形式です。"
            "pick_one はIDを壊さず候補を1つ返してください。"
            "pick_all はIDを壊さず全候補を返してください。"
            "tests/ にunittestを追加して検証してください。"
        )
        source = (
            "from typing import Any, Dict, Set, List\n\n"
            "def pick_one(rows: Dict[Any, Set[Any]]) -> Any:\n"
            "    return next(iter(rows), None)\n\n"
            "def pick_all(rows: Dict[str, Set[str]]) -> List[List[str]]:\n"
            "    return [list(rows)]\n"
        )
        steps = [
            tool_step("write_file", "picker.py", content=source),
            tool_step("read_file", "picker.py", content=source),
        ]

        state = runtime._implementation_task_progress_state(user_message=message, steps=steps)
        hints = state["implementation_source_repair_hints"]
        self.assertEqual(len(hints), 1)
        self.assertIn("def pick_all(rows: Dict[str, Set[str]]) -> List[List[str]]:", hints[0]["current_text"])
        self.assertIn("Dict[Any, Set[Any]]", hints[0]["suggested_new_text"])
        self.assertIn("List[List[Any]]", hints[0]["suggested_new_text"])

        prompt = runtime._implementation_task_progress_prompt(state)
        self.assertIn("def pick_all(rows: Dict[str, Set[str]]) -> List[List[str]]:", prompt)
        self.assertNotIn("def pick_one(rows: Dict[Any, Set[Any]]) -> Any:", prompt)
        self.assertNotIn("line 3: def pick_one", prompt)

        reducing_source = (
            "def pick_one(rows):\n"
            "    return next(iter(rows), None)\n\n"
            "def pick_all(rows):\n"
            "    return list(rows)\n"
        )
        self.assertIsNone(
            runtime._implementation_task_phase_action_block(
                user_message=message,
                tool_name="write_file",
                tool_args={"path": "picker.py", "content": reducing_source},
                steps=steps,
                session_id="main",
                turn_workspace=runtime.execution_root,
            )
        )

    def test_class_method_public_api_repair_prefers_wrapper_append(self) -> None:
        runtime = self.runtime()
        message = (
            "Pythonで汎用集計ツールを実装してください。"
            "summarize_values は集計を返してください。"
            "validate_summary は結果を検証してください。"
            "tests/ にunittestを追加して検証してください。"
        )
        source = (
            "class Summarizer:\n"
            "    def __init__(self, values):\n"
            "        self.values = list(values)\n"
            "    def summarize_values(self):\n"
            "        return {'count': len(self.values)}\n"
            "    def validate_summary(self, summary):\n"
            "        return summary.get('count') == len(self.values)\n"
        )
        steps = [
            tool_step("write_file", "summaries.py", content=source),
            tool_step("read_file", "summaries.py", content=source),
        ]

        state = runtime._implementation_task_progress_state(user_message=message, steps=steps)
        self.assertEqual(state["phase"], "implementation_present_needs_semantic_review")
        self.assertEqual(state["allowed_next_actions"], ["append_file summaries.py", "replace_text summaries.py"])
        hints = state["implementation_source_repair_hints"]
        self.assertEqual(hints[0]["suggested_action"], "append_top_level_wrappers")
        self.assertIn("def summarize_values(values):", hints[0]["suggested_new_text"])
        self.assertIn("return Summarizer(values).summarize_values()", hints[0]["suggested_new_text"])
        self.assertIn("def validate_summary(values, summary):", hints[0]["suggested_new_text"])

        prompt = runtime._implementation_task_progress_prompt(state)
        self.assertIn("top-level wrapper修復", prompt)
        self.assertIn("append_file", prompt)
        self.assertIn("巨大 replace_text", prompt)

        wrapper_block = hints[0]["suggested_new_text"]
        self.assertIsNone(
            runtime._implementation_task_phase_action_block(
                user_message=message,
                tool_name="append_file",
                tool_args={"path": "summaries.py", "content": wrapper_block},
                steps=steps,
                session_id="main",
                turn_workspace=runtime.execution_root,
            )
        )

    def test_tests_missing_requires_meaningful_test_artifact(self) -> None:
        runtime = self.runtime()
        message = "Pythonで normalize_name(text) を実装し、tests/ にunittestを追加して検証してください。"
        impl = "def normalize_name(text):\n    return ' '.join(str(text).split()).title()\n"
        steps = [tool_step("write_file", "name_tools.py", content=impl)]

        state = runtime._implementation_task_progress_state(user_message=message, steps=steps)
        self.assertEqual(state["phase"], "tests_missing")
        self.assertEqual(state["allowed_next_actions"], ["write_file tests/test_*.py"])

        blocked = runtime._implementation_task_phase_action_block(
            user_message=message,
            tool_name="run_command",
            tool_args={"command": "python3 -m unittest discover -s tests"},
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertEqual(blocked["reason_code"], "implementation_task_phase_requires_tests")

    def test_observed_unittest_missing_tests_artifact_becomes_generic_tests_missing(self) -> None:
        runtime = self.runtime()
        message = "１５パズルのプログラムを作り、それを実行して自分でクリアしてみて"
        impl = (
            "import random\n\n"
            "class Puzzle15:\n"
            "    def solve(self):\n"
            "        while True:\n"
            "            input('Press Enter')\n"
        )
        stderr = (
            "Traceback (most recent call last):\n"
            "  File \"<frozen runpy>\", line 198, in _run_module_as_main\n"
            "  File \"/opt/homebrew/lib/python3.14/unittest/loader.py\", line 334, in discover\n"
            "    raise ImportError('Start directory is not importable: %r' % start_dir)\n"
            "ImportError: Start directory is not importable: 'tests'\n"
        )
        steps = [
            tool_step("write_file", "src/puzzle15.py", content=impl),
            run_step("python3 -m unittest discover -s tests", ok=False, stderr=stderr),
            tool_step("read_file", "src/puzzle15.py", content=impl),
        ]

        state = runtime._implementation_task_progress_state(
            user_message=message,
            steps=steps,
            turn_workspace=runtime.execution_root,
        )

        self.assertTrue(state["applicable"])
        self.assertEqual(state["phase"], "tests_missing")
        self.assertEqual(state["latest_unittest_failure_type"], "missing_tests_artifact")
        self.assertEqual(state["allowed_next_actions"], ["write_file tests/test_*.py"])
        prompt = runtime._implementation_task_progress_prompt(state)
        self.assertIn("tests ディレクトリ未作成", prompt)
        self.assertIn("tests/test_*.py", prompt)

        blocked = runtime._implementation_task_phase_action_block(
            user_message=message,
            tool_name="write_file",
            tool_args={"path": "src/puzzle15.py", "content": impl},
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )

        self.assertEqual(blocked["reason_code"], "implementation_task_phase_requires_tests")
        self.assertEqual(blocked["allowed_next_actions"], ["write_file tests/test_*.py"])

    def test_root_level_test_file_does_not_satisfy_discover_contract(self) -> None:
        runtime = self.runtime()
        message = "Pythonで add_one(value) を実装し、unittestも作ってください。"
        impl = "def add_one(value):\n    return value + 1\n"
        test = (
            "import unittest\n"
            "from math_tools import add_one\n\n"
            "class TestMathTools(unittest.TestCase):\n"
            "    def test_add_one(self):\n"
            "        self.assertEqual(add_one(1), 2)\n"
        )
        steps = [
            tool_step("write_file", "math_tools.py", content=impl),
            tool_step("write_file", "test_math_tools.py", content=test),
        ]

        state = runtime._implementation_task_progress_state(
            user_message=message,
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )

        self.assertEqual(state["phase"], "tests_missing")
        self.assertEqual(state["test_paths"], [])
        self.assertEqual(state["allowed_next_actions"], ["write_file tests/test_*.py"])

        blocked = runtime._implementation_task_phase_action_block(
            user_message=message,
            tool_name="run_command",
            tool_args={"command": "python3 -m unittest test_math_tools -v"},
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertEqual(blocked["reason_code"], "implementation_task_phase_requires_tests")
        self.assertEqual(blocked["allowed_next_actions"], ["write_file tests/test_*.py"])

    def test_unittest_missing_root_test_module_returns_to_tests_missing(self) -> None:
        runtime = self.runtime()
        message = "Pythonで add_one(value) を実装し、unittestも作ってください。"
        impl = "def add_one(value):\n    return value + 1\n"
        stderr = (
            "test_math_tools (unittest.loader._FailedTest.test_math_tools) ... ERROR\n\n"
            "======================================================================\n"
            "ERROR: test_math_tools (unittest.loader._FailedTest.test_math_tools)\n"
            "----------------------------------------------------------------------\n"
            "ImportError: Failed to import test module: test_math_tools\n"
            "Traceback (most recent call last):\n"
            "  File \"/opt/homebrew/lib/python3.14/unittest/loader.py\", line 137, in loadTestsFromName\n"
            "    module = __import__(module_name)\n"
            "ModuleNotFoundError: No module named 'test_math_tools'\n"
        )
        steps = [
            tool_step("write_file", "math_tools.py", content=impl),
            run_step("python3 -m unittest test_math_tools -v", ok=False, stderr=stderr),
        ]

        state = runtime._implementation_task_progress_state(
            user_message=message,
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )

        self.assertEqual(state["phase"], "tests_missing")
        self.assertEqual(state["latest_unittest_failure_type"], "missing_tests_artifact")
        self.assertEqual(state["allowed_next_actions"], ["write_file tests/test_*.py"])

    def test_missing_tests_artifact_after_test_write_allows_unittest_rerun(self) -> None:
        runtime = self.runtime()
        message = "１５パズルのプログラムを作り、それを実行して自分でクリアしてみて"
        impl = "class Puzzle15:\n    pass\n"
        tests = (
            "import unittest\n"
            "from puzzle import Puzzle15\n\n"
            "class TestPuzzle15(unittest.TestCase):\n"
            "    def test_constructs(self):\n"
            "        self.assertIsInstance(Puzzle15(), Puzzle15)\n"
        )
        stderr = (
            "Traceback (most recent call last):\n"
            "  File \"/opt/homebrew/lib/python3.14/unittest/loader.py\", line 334, in discover\n"
            "    raise ImportError('Start directory is not importable: %r' % start_dir)\n"
            "ImportError: Start directory is not importable: 'tests'\n"
        )
        steps = [
            tool_step("write_file", "puzzle.py", content=impl),
            run_step("python3 -m unittest discover -s tests", ok=False, stderr=stderr),
            tool_step("write_file", "tests/test_puzzle.py", content=tests),
        ]

        state = runtime._implementation_task_progress_state(
            user_message=message,
            steps=steps,
            turn_workspace=runtime.execution_root,
        )

        self.assertEqual(state["phase"], "unittest_not_run")
        self.assertEqual(state["latest_unittest_failure_type"], "missing_tests_artifact")
        self.assertEqual(state["allowed_next_actions"], ["run_command python3 -m unittest discover -s tests"])

    def test_unittest_not_run_blocks_finish_and_allows_unittest(self) -> None:
        runtime = self.runtime()
        message = "Pythonで normalize_name(text) を実装し、tests/ にunittestを追加して検証してください。"
        impl = "def normalize_name(text):\n    return ' '.join(str(text).split()).title()\n"
        test = (
            "import unittest\nfrom name_tools import normalize_name\n\n"
            "class TestNameTools(unittest.TestCase):\n"
            "    def test_normalize(self):\n"
            "        self.assertEqual(normalize_name(' ada   lovelace '), 'Ada Lovelace')\n"
        )
        steps = [
            tool_step("write_file", "name_tools.py", content=impl),
            tool_step("write_file", "tests/test_name_tools.py", content=test),
        ]

        state = runtime._implementation_task_progress_state(user_message=message, steps=steps)
        self.assertEqual(state["phase"], "unittest_not_run")
        blocked = runtime._implementation_task_phase_action_block(
            user_message=message,
            tool_name="finish",
            tool_args={"final_answer": "done"},
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertEqual(blocked["reason_code"], "implementation_task_phase_requires_unittest")

    def test_replace_text_no_match_gets_one_recovery_read_then_edit(self) -> None:
        runtime = self.runtime()
        message = "Pythonで required_api(text) を実装し、tests/ にunittestを追加して検証してください。"
        impl = "def other_api(text):\n    return text\n"
        steps = [
            tool_step("write_file", "tool.py", content=impl),
            {
                "tool_name": "replace_text",
                "tool_args": {"path": "tool.py", "old_text": "missing", "new_text": impl},
                "tool_result": {"ok": False, "path": "tool.py", "failure_type": "replace_text_no_match"},
            },
        ]
        state = runtime._implementation_task_progress_state(user_message=message, steps=steps)
        self.assertEqual(state["phase"], "implementation_present_needs_semantic_review")
        self.assertEqual(state["allowed_next_actions"], ["read_file tool.py once"])

        read_steps = [*steps, tool_step("read_file", "tool.py", content=impl)]
        blocked = runtime._implementation_task_phase_action_block(
            user_message=message,
            tool_name="read_file",
            tool_args={"path": "tool.py"},
            steps=read_steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertEqual(blocked["reason_code"], "implementation_task_phase_requires_targeted_replace_after_no_match")
        self.assertEqual(
            blocked["allowed_next_actions"],
            ["replace_text tool.py", "write_file tool.py"],
        )

    def test_large_replace_text_no_match_recovery_requires_write_file_without_prompt_conflict(self) -> None:
        runtime = self.runtime()
        message = (
            "Pythonで汎用mapping処理を実装してください。"
            "入力は item_id -> set(feature_id) の辞書形式です。"
            "select_item はIDを壊さず候補を返してください。"
            "tests/ にunittestを追加して検証してください。"
        )
        impl = (
            "from typing import Dict, Set, List\n\n"
            "def select_item(rows: Dict[int, Set[int]]) -> List[int]:\n"
            "    return list(range(len(rows)))\n"
        )
        large_old = "missing line\n" * 90
        large_new = "def select_item(rows):\n    return list(rows)\n" + ("# rewritten\n" * 90)
        steps = [
            tool_step("write_file", "picker.py", content=impl),
            {
                "tool_name": "replace_text",
                "tool_args": {"path": "picker.py", "old_text": large_old, "new_text": large_new},
                "tool_result": {"ok": False, "path": "picker.py", "failure_type": "replace_text_no_match"},
            },
            tool_step("read_file", "picker.py", content=impl),
        ]

        state = runtime._implementation_task_progress_state(user_message=message, steps=steps)
        self.assertEqual(state["phase"], "implementation_present_needs_semantic_review")
        self.assertEqual(state["allowed_next_actions"], ["write_file picker.py"])
        prompt = runtime._implementation_task_progress_prompt(state)
        self.assertIn("replace_text no_match後の修復契約", prompt)
        self.assertIn("allowed_next_actions は write_file", prompt)
        self.assertNotIn("小さい replace_text を優先", prompt)

        blocked = runtime._implementation_task_phase_action_block(
            user_message=message,
            tool_name="replace_text",
            tool_args={"path": "picker.py", "old_text": large_old, "new_text": large_new},
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertEqual(
            blocked["reason_code"],
            "implementation_task_phase_blocks_repeated_or_large_replace_after_no_match",
        )
        self.assertEqual(blocked["allowed_next_actions"], ["write_file picker.py"])
        self.assertIn("old_text 不一致", blocked["message"])

    def test_recursive_backtracking_symmetric_state_is_not_source_contract_issue(self) -> None:
        runtime = self.runtime()
        source = (
            "def choose_items(rows):\n"
            "    remaining = set(rows)\n"
            "    path = []\n"
            "    def search(remaining, path):\n"
            "        if not remaining:\n"
            "            return list(path)\n"
            "        item = next(iter(remaining))\n"
            "        path.append(item)\n"
            "        remaining.remove(item)\n"
            "        result = search(remaining, path)\n"
            "        if result is not None:\n"
            "            return result\n"
            "        path.pop()\n"
            "        remaining.add(item)\n"
            "        return None\n"
            "    return search(remaining, path)\n"
        )

        self.assertEqual(runtime._python_source_has_recursive_destructive_shared_state(source), "")
        state = runtime._implementation_task_progress_state(
            user_message="Pythonで choose_items(rows) を実装し、tests/ にunittestを追加して検証してください。",
            steps=[tool_step("write_file", "chooser.py", content=source)],
        )
        self.assertEqual(state["phase"], "tests_missing")

    def test_recursive_backtracking_snapshot_restore_is_not_source_contract_issue(self) -> None:
        runtime = self.runtime()
        source = (
            "def choose_items(rows):\n"
            "    remaining = set(rows)\n"
            "    def search(remaining):\n"
            "        if not remaining:\n"
            "            return []\n"
            "        item = next(iter(remaining))\n"
            "        old_remaining = set(remaining)\n"
            "        remaining.remove(item)\n"
            "        result = search(remaining)\n"
            "        remaining.clear()\n"
            "        remaining.update(old_remaining)\n"
            "        return [item] + result\n"
            "    return search(remaining)\n"
        )

        self.assertEqual(runtime._python_source_has_recursive_destructive_shared_state(source), "")

    def test_recursive_backtracking_high_risk_destructive_state_is_source_contract_issue(self) -> None:
        runtime = self.runtime()
        source = (
            "def choose_items(rows):\n"
            "    remaining = set(rows)\n"
            "    def search(remaining):\n"
            "        if not remaining:\n"
            "            return []\n"
            "        remaining.difference_update({next(iter(remaining))})\n"
            "        return search(remaining)\n"
            "    return search(remaining)\n"
        )

        issue = runtime._python_source_has_recursive_destructive_shared_state(source)
        self.assertIn("recursive/backtracking実装", issue)
        self.assertIn("remaining.difference_update", issue)

    def test_structural_semantic_issue_gives_line_hints_and_blocks_broad_replace(self) -> None:
        runtime = self.runtime()
        message = "Pythonで choose_items(rows) を実装し、tests/ にunittestを追加して検証してください。"
        source = (
            "def choose_items(rows):\n"
            "    remaining = {row: {row} for row in rows}\n"
            "    solution = []\n"
            "    def search(remaining, solution):\n"
            "        if not any(remaining[row] for row in remaining):\n"
            "            return True\n"
            "        chosen = next(iter(remaining))\n"
            "        solution.append(chosen)\n"
            "        del remaining[chosen]\n"
            "        if search(remaining, solution):\n"
            "            return True\n"
            "        solution.pop()\n"
            "        remaining[chosen] = {chosen}\n"
            "        return False\n"
            "    return solution if search(remaining, solution) else None\n"
        )
        large_source = source + "\n" + "\n".join(f"# filler {index}" for index in range(160)) + "\n"
        steps = [
            tool_step("write_file", "chooser.py", content=large_source),
            tool_step("read_file", "chooser.py", content=large_source),
        ]

        state = runtime._implementation_task_progress_state(user_message=message, steps=steps)
        self.assertEqual(state["phase"], "implementation_present_needs_semantic_review")
        self.assertEqual(state["allowed_next_actions"], ["write_file chooser.py"])
        hints = state["implementation_source_repair_hints"]
        self.assertTrue(any(hint.get("suggested_action") == "rewrite_recursive_branch_local_state" for hint in hints))
        self.assertTrue(any("del remaining[chosen]" in hint.get("current_text", "") for hint in hints))

        (runtime.execution_root / "chooser.py").write_text(large_source, encoding="utf-8")
        blocked = runtime._implementation_task_phase_action_block(
            user_message=message,
            tool_name="replace_text",
            tool_args={
                "path": "chooser.py",
                "old_text": large_source,
                "new_text": large_source.replace("del remaining[chosen]", "next_remaining = dict(remaining)", 1),
            },
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertEqual(blocked["reason_code"], "implementation_task_phase_blocks_broad_replace_text")
        self.assertEqual(blocked["allowed_next_actions"], ["write_file chooser.py"])

    def test_nonreducing_write_block_uses_write_guidance_when_write_is_allowed(self) -> None:
        runtime = self.runtime()
        message = "Pythonで choose_items(rows) を実装し、tests/ にunittestを追加して検証してください。"
        source = (
            "def choose_items(rows):\n"
            "    remaining = {row: {row} for row in rows}\n"
            "    solution = []\n"
            "    def search(remaining, solution):\n"
            "        if not any(remaining[row] for row in remaining):\n"
            "            return True\n"
            "        chosen = next(iter(remaining))\n"
            "        solution.append(chosen)\n"
            "        del remaining[chosen]\n"
            "        if search(remaining, solution):\n"
            "            return True\n"
            "        solution.pop()\n"
            "        remaining[chosen] = {chosen}\n"
            "        return False\n"
            "    return solution if search(remaining, solution) else None\n"
        )
        steps = [
            tool_step("write_file", "chooser.py", content=source),
            tool_step("read_file", "chooser.py", content=source),
        ]
        (runtime.execution_root / "chooser.py").write_text(source, encoding="utf-8")

        blocked = runtime._implementation_task_phase_action_block(
            user_message=message,
            tool_name="write_file",
            tool_args={"path": "chooser.py", "content": source + "\n# still same defect\n"},
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )

        self.assertEqual(blocked["reason_code"], "implementation_task_phase_requires_contract_reducing_edit")
        self.assertEqual(blocked["allowed_next_actions"], ["write_file chooser.py"])
        self.assertIn("allowed_next_actions は write_file", blocked["suggested_fix"])
        self.assertNotIn("old_text/new_text", blocked["suggested_fix"])

    def test_failed_unittest_reads_test_and_implementation_once_then_requires_edit(self) -> None:
        runtime = self.runtime()
        message = "Pythonで add_one(value) を実装し、tests/ にunittestを追加して検証してください。"
        impl = "def add_one(value):\n    return value\n"
        test = (
            "import unittest\nfrom math_tools import add_one\n\n"
            "class TestMathTools(unittest.TestCase):\n"
            "    def test_add_one(self):\n"
            "        self.assertEqual(add_one(1), 2)\n"
        )
        stderr = (
            "FAIL: test_add_one (test_math_tools.TestMathTools.test_add_one)\n"
            f"  File \"{runtime.execution_root / 'tests' / 'test_math_tools.py'}\", line 5, in test_add_one\n"
            "    self.assertEqual(add_one(1), 2)\n"
            "    ~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^\n"
            "AssertionError: 1 != 2\n"
        )
        steps = [
            tool_step("write_file", "math_tools.py", content=impl),
            tool_step("write_file", "tests/test_math_tools.py", content=test),
            run_step("python3 -m unittest discover -s tests", ok=False, stderr=stderr),
        ]
        state = runtime._implementation_task_progress_state(
            user_message=message,
            steps=steps,
            turn_workspace=runtime.execution_root,
        )
        self.assertEqual(state["phase"], "unittest_failed_needs_fix")
        self.assertIn("tests/test_math_tools.py", state["failed_unittest_recovery_read_paths"])
        self.assertIn("math_tools.py", state["failed_unittest_recovery_read_paths"])
        self.assertEqual(
            state["allowed_next_actions"],
            ["read_file tests/test_math_tools.py once", "read_file math_tools.py once"],
        )
        self.assertTrue(
            any("tracebackはtest artifact内のassert失敗" in hint for hint in state["unittest_repair_hints"])
        )

        steps_after_reads = [
            *steps,
            tool_step("read_file", "tests/test_math_tools.py", content=test),
            tool_step("read_file", "math_tools.py", content=impl),
        ]
        state_after_reads = runtime._implementation_task_progress_state(
            user_message=message,
            steps=steps_after_reads,
            turn_workspace=runtime.execution_root,
        )
        self.assertEqual(
            state_after_reads["allowed_next_actions"],
            [
                "replace_text tests/test_math_tools.py with a small unique old_text",
                "replace_text math_tools.py with a small unique old_text",
                "write_file tests/test_math_tools.py",
                "write_file math_tools.py",
            ],
        )
        blocked = runtime._implementation_task_phase_action_block(
            user_message=message,
            tool_name="read_file",
            tool_args={"path": "math_tools.py"},
            steps=steps_after_reads,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertEqual(blocked["reason_code"], "implementation_task_failed_unittest_read_already_consumed")

        fixed = "def add_one(value):\n    return value + 1\n"
        steps_after_edit = [*steps_after_reads, tool_step("write_file", "math_tools.py", content=fixed)]
        state_after_edit = runtime._implementation_task_progress_state(
            user_message=message,
            steps=steps_after_edit,
            turn_workspace=runtime.execution_root,
        )
        self.assertEqual(state_after_edit["allowed_next_actions"], ["run_command python3 -m unittest discover -s tests"])

    def test_unittest_return_shape_hint_detects_scalar_container_assertion(self) -> None:
        runtime = self.runtime()
        output = (
            "FAIL: test_sample (test_solver.TestSolver.test_sample)\n"
            "AssertionError: 0 != []\n"
            "AssertionError: 17 != [(2, 5, 6), (5, 8, 11)]\n"
        )

        hint = runtime._unittest_output_return_shape_hint(output)

        self.assertIn("返却shape/API契約不一致", hint)
        self.assertIn("docstring", hint)
        self.assertIn("unpack", hint)
        self.assertIn("oracle", hint)

    def test_unittest_return_shape_hint_reaches_repair_prompt(self) -> None:
        runtime = self.runtime()
        message = "Pythonで最適化アルゴリズムを実装し、unittestで検証してください。"
        impl = (
            "def solve(items):\n"
            "    \"\"\"Return (score, selected_items).\"\"\"\n"
            "    return 0, []\n"
        )
        test = (
            "import unittest\n"
            "from solver import solve\n\n"
            "class TestSolver(unittest.TestCase):\n"
            "    def test_sample(self):\n"
            "        selected, score = solve([])\n"
            "        self.assertEqual(selected, [])\n"
            "        self.assertEqual(score, 0)\n"
        )
        stderr = (
            "FAIL: test_sample (test_solver.TestSolver.test_sample)\n"
            "----------------------------------------------------------------------\n"
            "Traceback (most recent call last):\n"
            f"  File \"{runtime.execution_root / 'tests' / 'test_solver.py'}\", line 7, in test_sample\n"
            "    self.assertEqual(selected, [])\n"
            "AssertionError: 0 != []\n"
        )
        steps = [
            tool_step("write_file", "solver.py", content=impl),
            tool_step("write_file", "tests/test_solver.py", content=test),
            run_step("python3 -m unittest discover -s tests", ok=False, stderr=stderr),
            tool_step("read_file", "tests/test_solver.py", content=test),
            tool_step("read_file", "solver.py", content=impl),
        ]

        state = runtime._implementation_task_progress_state(
            user_message=message,
            steps=steps,
            turn_workspace=runtime.execution_root,
        )
        self.assertTrue(state["return_shape_contract_mismatch"])
        self.assertNotIn("write_file solver.py", state["allowed_next_actions"])
        self.assertIn("replace_text solver.py with a small unique old_text", state["allowed_next_actions"])
        self.assertIn("write_file tests/test_solver.py", state["allowed_next_actions"])
        hints = "\n".join(state["unittest_repair_hints"])
        self.assertIn("返却shape/API契約不一致", hints)
        self.assertIn("reference/brute_force/oracle", hints)

        prompt = runtime._implementation_task_progress_prompt(state)
        self.assertIn("返却shape/API契約不一致", prompt)
        self.assertIn("公開関数のdocstring/仕様", prompt)
        self.assertIn("返却shape/API修復契約", prompt)
        self.assertIn("アルゴリズム全面再実装", prompt)

        blocked = runtime._implementation_task_phase_action_block(
            user_message=message,
            tool_name="replace_text",
            tool_args={
                "path": "solver.py",
                "old_text": impl + ("# stale whole-file filler\n" * 80),
                "new_text": impl.replace("return 0, []", "return [], 0") + ("# stale whole-file filler\n" * 80),
            },
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertIsNotNone(blocked)
        self.assertEqual(blocked["reason_code"], "implementation_task_failed_unittest_blocks_broad_replace_text")
        self.assertIn("返却shape/API契約の修復", blocked["suggested_fix"])
        self.assertNotIn("write_file solver.py", blocked["allowed_next_actions"])
        self.assertIn("write_file tests/test_solver.py", blocked["allowed_next_actions"])

    def test_return_shape_impl_broad_replace_removes_impl_from_next_actions(self) -> None:
        runtime = self.runtime()
        message = "Pythonで最適化アルゴリズムを実装し、unittestで検証してください。"
        impl = (
            "def solve(items):\n"
            "    \"\"\"Return (score, selected_items).\"\"\"\n"
            "    return 0, []\n"
        )
        test = (
            "import unittest\n"
            "from solver import solve\n\n"
            "def brute_force_solve(items):\n"
            "    return [], 0\n\n"
            "class TestSolver(unittest.TestCase):\n"
            "    def test_sample(self):\n"
            "        selected, score = solve([])\n"
            "        expected_selected, expected_score = brute_force_solve([])\n"
            "        self.assertEqual(selected, expected_selected)\n"
            "        self.assertEqual(score, expected_score)\n"
        )
        stderr = (
            "FAIL: test_sample (test_solver.TestSolver.test_sample)\n"
            "----------------------------------------------------------------------\n"
            "Traceback (most recent call last):\n"
            f"  File \"{runtime.execution_root / 'tests' / 'test_solver.py'}\", line 10, in test_sample\n"
            "    self.assertEqual(selected, expected_selected)\n"
            "AssertionError: 0 != []\n"
        )
        steps = [
            tool_step("write_file", "solver.py", content=impl),
            tool_step("write_file", "tests/test_solver.py", content=test),
            run_step("python3 -m unittest discover -s tests", ok=False, stderr=stderr),
            tool_step("read_file", "tests/test_solver.py", content=test),
            tool_step("read_file", "solver.py", content=impl),
        ]
        runtime._append_session_event(
            "main",
            {
                "type": "system_note",
                "role": "system",
                "code": "implementation_task_progress_blocked",
                "reason_code": "implementation_task_failed_unittest_blocks_broad_replace_text",
                "details": {
                    "reason_code": "implementation_task_failed_unittest_blocks_broad_replace_text",
                    "phase": "unittest_failed_needs_fix",
                    "path": "solver.py",
                },
            },
        )

        state = runtime._implementation_task_progress_state(
            user_message=message,
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )

        self.assertTrue(state["return_shape_contract_mismatch"])
        self.assertEqual(state["return_shape_impl_blocked_paths"], ["solver.py"])
        self.assertNotIn("replace_text solver.py with a small unique old_text", state["allowed_next_actions"])
        self.assertNotIn("write_file solver.py", state["allowed_next_actions"])
        self.assertEqual(
            state["allowed_next_actions"],
            [
                "replace_text tests/test_solver.py with a small unique old_text",
                "write_file tests/test_solver.py",
            ],
        )

        blocked = runtime._implementation_task_phase_action_block(
            user_message=message,
            tool_name="replace_text",
            tool_args={
                "path": "solver.py",
                "old_text": impl,
                "new_text": impl.replace("return 0, []", "return [], 0"),
            },
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertIsNotNone(blocked)
        self.assertEqual(
            blocked["reason_code"],
            "implementation_task_failed_unittest_prioritizes_return_shape_surface",
        )
        self.assertIn("test/oracle", blocked["suggested_fix"])
        blocked_actions = "\n".join(blocked["allowed_next_actions"])
        self.assertNotIn("write_file solver.py", blocked_actions)
        self.assertNotIn("replace_text solver.py ", blocked_actions)

    def test_failed_unittest_after_one_recovery_read_requires_remaining_traceback_read(self) -> None:
        runtime = self.runtime()
        message = "Pythonで add_one(value) を実装し、tests/ にunittestを追加して検証してください。"
        impl = "def add_one(value):\n    return value\n"
        test = (
            "import unittest\nfrom math_tools import add_one\n\n"
            "class TestMathTools(unittest.TestCase):\n"
            "    def test_add_one(self):\n"
            "        self.assertEqual(add_one(1), 2)\n"
        )
        stderr = (
            "FAIL: test_add_one (test_math_tools.TestMathTools.test_add_one)\n"
            f"  File \"{runtime.execution_root / 'tests' / 'test_math_tools.py'}\", line 5, in test_add_one\n"
            "AssertionError: 1 != 2\n"
        )
        steps = [
            tool_step("write_file", "math_tools.py", content=impl),
            tool_step("write_file", "tests/test_math_tools.py", content=test),
            run_step("python3 -m unittest discover -s tests", ok=False, stderr=stderr),
            tool_step("read_file", "math_tools.py", content=impl),
        ]

        state = runtime._implementation_task_progress_state(
            user_message=message,
            steps=steps,
            turn_workspace=runtime.execution_root,
        )

        self.assertEqual(state["phase"], "unittest_failed_needs_fix")
        self.assertEqual(state["failed_unittest_recovery_editable_paths"], ["math_tools.py"])
        self.assertEqual(state["allowed_next_actions"], ["read_file tests/test_math_tools.py once"])

        blocked_repeat_read = runtime._implementation_task_phase_action_block(
            user_message=message,
            tool_name="read_file",
            tool_args={"path": "math_tools.py"},
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertEqual(
            blocked_repeat_read["reason_code"],
            "implementation_task_failed_unittest_read_already_consumed",
        )
        self.assertEqual(blocked_repeat_read["allowed_next_actions"], ["read_file tests/test_math_tools.py once"])

        blocked_write = runtime._implementation_task_phase_action_block(
            user_message=message,
            tool_name="write_file",
            tool_args={"path": "math_tools.py", "content": "def add_one(value):\n    return value + 1\n"},
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertEqual(
            blocked_write["reason_code"],
            "implementation_task_failed_unittest_requires_recovery_read",
        )

    def test_unittest_output_excerpt_preserves_first_error_and_tail_failure(self) -> None:
        runtime = self.runtime()
        message = "Pythonで Solver を実装し、tests/ にunittestを追加して検証してください。"
        impl = "class Solver:\n    pass\n"
        test = "import unittest\nclass TestSolver(unittest.TestCase):\n    pass\n"
        stderr = (
            "ERROR: test_missing_api (tests.TestSolver.test_missing_api)\n"
            "Traceback (most recent call last):\n"
            f"  File \"{runtime.execution_root / 'tests' / 'test_solver.py'}\", line 8, in test_missing_api\n"
            "    self.assertTrue(self.solver.is_goal_state([]))\n"
            "AttributeError: 'Solver' object has no attribute 'is_goal_state'\n"
            + ("filler line\n" * 300)
            + "FAIL: test_expected_value (tests.TestSolver.test_expected_value)\n"
            "Traceback (most recent call last):\n"
            f"  File \"{runtime.execution_root / 'tests' / 'test_solver.py'}\", line 30, in test_expected_value\n"
            "    self.assertEqual(value, 3)\n"
            "AssertionError: 0 != 3\n"
        )
        steps = [
            tool_step("write_file", "solver.py", content=impl),
            tool_step("write_file", "tests/test_solver.py", content=test),
            run_step("python3 -m unittest discover -s tests", ok=False, stderr=stderr),
        ]

        state = runtime._implementation_task_progress_state(
            user_message=message,
            steps=steps,
            turn_workspace=runtime.execution_root,
        )

        excerpt = state["latest_unittest_output_excerpt"]
        self.assertIn("AttributeError", excerpt)
        self.assertIn("AssertionError: 0 != 3", excerpt)

    def test_repeated_same_unittest_failure_after_edit_requires_edit_not_reread(self) -> None:
        runtime = self.runtime()
        message = "Pythonで add_one(value) を実装し、tests/ にunittestを追加して検証してください。"
        impl = "def add_one(value):\n    return value\n"
        test = (
            "import unittest\nfrom math_tools import add_one\n\n"
            "class TestMathTools(unittest.TestCase):\n"
            "    def test_add_one(self):\n"
            "        self.assertEqual(add_one(1), 2)\n"
        )
        stderr = (
            "FAIL: test_add_one (test_math_tools.TestMathTools.test_add_one)\n"
            f"  File \"{runtime.execution_root / 'tests' / 'test_math_tools.py'}\", line 5, in test_add_one\n"
            "AssertionError: 1 != 2\n"
        )
        steps = [
            tool_step("write_file", "math_tools.py", content=impl),
            tool_step("write_file", "tests/test_math_tools.py", content=test),
            run_step("python3 -m unittest discover -s tests", ok=False, stderr=stderr),
            tool_step("read_file", "tests/test_math_tools.py", content=test),
            tool_step("read_file", "math_tools.py", content=impl),
            tool_step("replace_text", "math_tools.py", content=impl),
            run_step("python3 -m unittest discover -s tests", ok=False, stderr=stderr),
        ]

        state = runtime._implementation_task_progress_state(
            user_message=message,
            steps=steps,
            turn_workspace=runtime.execution_root,
        )
        self.assertTrue(state["repeated_unittest_failure_signature"])
        self.assertEqual(state["same_signature_nonreducing_edit_paths"], ["math_tools.py"])
        self.assertEqual(
            state["same_signature_read_paths"],
            ["math_tools.py", "tests/test_math_tools.py"],
        )
        self.assertRegex(state["latest_unittest_failure_signature"], r"^[0-9a-f]{16}$")
        self.assertEqual(
            state["allowed_next_actions"],
            [
                "replace_text tests/test_math_tools.py with a small unique old_text",
                "replace_text math_tools.py with a small unique old_text",
                "write_file tests/test_math_tools.py",
            ],
        )

        blocked_read = runtime._implementation_task_phase_action_block(
            user_message=message,
            tool_name="read_file",
            tool_args={"path": "math_tools.py"},
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertEqual(blocked_read["reason_code"], "implementation_task_failed_unittest_read_already_consumed")

        prompt = runtime._implementation_task_progress_prompt(state)
        self.assertIn("同一unittest failure signatureの非進捗", prompt)
        self.assertIn("直前の編集は失敗を減らしていません", prompt)
        self.assertIn("latest_unittest_failure_signature", prompt)
        self.assertIn("math_tools.py", prompt)

    def test_repeated_unittest_signature_ignores_line_number_drift(self) -> None:
        runtime = self.runtime()
        message = "Pythonで board solver を実装し、tests/ にunittestを追加して検証してください。"
        impl = "class Board:\n    def __init__(self, board):\n        self.board = board\n"
        test = "import unittest\nfrom board_solver import Board\n"
        stderr_first = (
            "ERROR: test_board (test_board_solver.TestBoard.test_board)\n"
            f"  File \"{runtime.execution_root / 'tests' / 'test_board_solver.py'}\", line 8, in test_board\n"
            f"  File \"{runtime.execution_root / 'board_solver.py'}\", line 19, in _find_blank\n"
            "TypeError: 'int' object is not subscriptable\n"
        )
        stderr_second = (
            "ERROR: test_board (test_board_solver.TestBoard.test_board)\n"
            f"  File \"{runtime.execution_root / 'tests' / 'test_board_solver.py'}\", line 9, in test_board\n"
            f"  File \"{runtime.execution_root / 'board_solver.py'}\", line 21, in _find_blank\n"
            "TypeError: 'int' object is not subscriptable\n"
        )
        steps = [
            tool_step("write_file", "board_solver.py", content=impl),
            tool_step("write_file", "tests/test_board_solver.py", content=test),
            run_step("python3 -m unittest discover -s tests", ok=False, stderr=stderr_first),
            tool_step("read_file", "tests/test_board_solver.py", content=test),
            tool_step("read_file", "board_solver.py", content=impl),
            tool_step("write_file", "board_solver.py", content=impl + "\n"),
            run_step("python3 -m unittest discover -s tests", ok=False, stderr=stderr_second),
        ]

        state = runtime._implementation_task_progress_state(
            user_message=message,
            steps=steps,
            turn_workspace=runtime.execution_root,
        )

        self.assertTrue(state["repeated_unittest_failure_signature"])
        self.assertEqual(state["same_signature_nonreducing_edit_paths"], ["board_solver.py"])

    def test_repeated_same_unittest_failure_blocks_noop_write(self) -> None:
        runtime = self.runtime()
        message = "Pythonで add_one(value) を実装し、tests/ にunittestを追加して検証してください。"
        impl = "def add_one(value):\n    return value\n"
        test = (
            "import unittest\nfrom math_tools import add_one\n\n"
            "class TestMathTools(unittest.TestCase):\n"
            "    def test_add_one(self):\n"
            "        self.assertEqual(add_one(1), 2)\n"
        )
        stderr = (
            "FAIL: test_add_one (test_math_tools.TestMathTools.test_add_one)\n"
            f"  File \"{runtime.execution_root / 'tests' / 'test_math_tools.py'}\", line 5, in test_add_one\n"
            "AssertionError: 1 != 2\n"
        )
        steps = [
            tool_step("write_file", "math_tools.py", content=impl),
            tool_step("write_file", "tests/test_math_tools.py", content=test),
            run_step("python3 -m unittest discover -s tests", ok=False, stderr=stderr),
            tool_step("read_file", "tests/test_math_tools.py", content=test),
            tool_step("read_file", "math_tools.py", content=impl),
            tool_step("replace_text", "math_tools.py", content=impl),
            run_step("python3 -m unittest discover -s tests", ok=False, stderr=stderr),
        ]

        blocked = runtime._implementation_task_phase_action_block(
            user_message=message,
            tool_name="write_file",
            tool_args={"path": "math_tools.py", "content": impl},
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )

        self.assertEqual(
            blocked["reason_code"],
            "implementation_task_failed_unittest_blocks_repeated_full_write_after_nonreducing_signature",
        )
        self.assertIn("failure signature", blocked["message"])
        self.assertRegex(blocked["latest_unittest_failure_signature"], r"^[0-9a-f]{16}$")
        self.assertEqual(blocked["nonreducing_edit_paths"], ["math_tools.py"])
        self.assertEqual(
            blocked["allowed_next_actions"],
            [
                "replace_text tests/test_math_tools.py with a small unique old_text",
                "replace_text math_tools.py with a small unique old_text",
                "write_file tests/test_math_tools.py",
            ],
        )

        changed = runtime._implementation_task_phase_action_block(
            user_message=message,
            tool_name="write_file",
            tool_args={"path": "math_tools.py", "content": "def add_one(value):\n    return value + 1\n"},
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertEqual(
            changed["reason_code"],
            "implementation_task_failed_unittest_blocks_repeated_full_write_after_nonreducing_signature",
        )
        self.assertEqual(changed["nonreducing_edit_paths"], ["math_tools.py"])

    def test_failed_unittest_blocks_initial_noop_write_after_reads(self) -> None:
        runtime = self.runtime()
        message = "Pythonで add_one(value) を実装し、tests/ にunittestを追加して検証してください。"
        impl = "def add_one(value):\n    return value\n"
        test = (
            "import unittest\nfrom math_tools import add_one\n\n"
            "class TestMathTools(unittest.TestCase):\n"
            "    def test_add_one(self):\n"
            "        self.assertEqual(add_one(1), 2)\n"
        )
        stderr = (
            "FAIL: test_add_one (test_math_tools.TestMathTools.test_add_one)\n"
            f"  File \"{runtime.execution_root / 'tests' / 'test_math_tools.py'}\", line 5, in test_add_one\n"
            "AssertionError: 1 != 2\n"
        )
        steps = [
            tool_step("write_file", "math_tools.py", content=impl),
            tool_step("write_file", "tests/test_math_tools.py", content=test),
            run_step("python3 -m unittest discover -s tests", ok=False, stderr=stderr),
            tool_step("read_file", "tests/test_math_tools.py", content=test),
            tool_step("read_file", "math_tools.py", content=impl),
        ]
        (runtime.execution_root / "math_tools.py").write_text(impl, encoding="utf-8")

        blocked = runtime._implementation_task_phase_action_block(
            user_message=message,
            tool_name="write_file",
            tool_args={"path": "math_tools.py", "content": impl},
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )

        self.assertEqual(blocked["reason_code"], "implementation_task_failed_unittest_blocks_nonreducing_write")
        self.assertEqual(blocked["nonreducing_reason"], "identical_current_source")
        self.assertIn("失敗signatureを変える可能性が低い", blocked["message"])

    def test_repeated_unittest_blocks_comment_only_write(self) -> None:
        runtime = self.runtime()
        message = "Pythonで add_one(value) を実装し、tests/ にunittestを追加して検証してください。"
        impl = "def add_one(value):\n    return value\n"
        test = (
            "import unittest\nfrom math_tools import add_one\n\n"
            "class TestMathTools(unittest.TestCase):\n"
            "    def test_add_one(self):\n"
            "        self.assertEqual(add_one(1), 2)\n"
        )
        stderr = (
            "FAIL: test_add_one (test_math_tools.TestMathTools.test_add_one)\n"
            f"  File \"{runtime.execution_root / 'tests' / 'test_math_tools.py'}\", line 5, in test_add_one\n"
            "AssertionError: 1 != 2\n"
        )
        steps = [
            tool_step("write_file", "math_tools.py", content=impl),
            tool_step("write_file", "tests/test_math_tools.py", content=test),
            run_step("python3 -m unittest discover -s tests", ok=False, stderr=stderr),
            tool_step("read_file", "tests/test_math_tools.py", content=test),
            tool_step("read_file", "math_tools.py", content=impl),
            tool_step("write_file", "math_tools.py", content=impl),
            run_step("python3 -m unittest discover -s tests", ok=False, stderr=stderr),
        ]
        (runtime.execution_root / "math_tools.py").write_text(impl, encoding="utf-8")

        blocked = runtime._implementation_task_phase_action_block(
            user_message=message,
            tool_name="write_file",
            tool_args={"path": "math_tools.py", "content": impl + "# comment only\n"},
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )

        self.assertEqual(
            blocked["reason_code"],
            "implementation_task_failed_unittest_blocks_repeated_full_write_after_nonreducing_signature",
        )
        self.assertEqual(blocked["nonreducing_edit_paths"], ["math_tools.py"])

    def test_failed_unittest_after_reads_allows_small_targeted_replace_text(self) -> None:
        runtime = self.runtime()
        message = "Pythonで add_one(value) を実装し、tests/ にunittestを追加して検証してください。"
        impl = "def add_one(value):\n    return value\n"
        test = (
            "import unittest\nfrom math_tools import add_one\n\n"
            "class TestMathTools(unittest.TestCase):\n"
            "    def test_add_one(self):\n"
            "        self.assertEqual(add_one(1), 2)\n"
        )
        stderr = (
            "FAIL: test_add_one (test_math_tools.TestMathTools.test_add_one)\n"
            f"  File \"{runtime.execution_root / 'tests' / 'test_math_tools.py'}\", line 5, in test_add_one\n"
            "AssertionError: 1 != 2\n"
        )
        steps = [
            tool_step("write_file", "math_tools.py", content=impl),
            tool_step("write_file", "tests/test_math_tools.py", content=test),
            run_step("python3 -m unittest discover -s tests", ok=False, stderr=stderr),
            tool_step("read_file", "tests/test_math_tools.py", content=test),
            tool_step("read_file", "math_tools.py", content=impl),
        ]
        (runtime.execution_root / "math_tools.py").write_text(impl, encoding="utf-8")

        allowed = runtime._implementation_task_phase_action_block(
            user_message=message,
            tool_name="replace_text",
            tool_args={
                "path": "math_tools.py",
                "old_text": "    return value\n",
                "new_text": "    return value + 1\n",
            },
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertIsNone(allowed)

    def test_successful_edit_after_failed_unittest_allows_unittest_rerun(self) -> None:
        runtime = self.runtime()
        message = "Pythonで add_one(value) を実装し、tests/ にunittestを追加して検証してください。"
        impl = "def add_one(value):\n    return value\n"
        fixed_impl = "def add_one(value):\n    return value + 1\n"
        test = (
            "import unittest\nfrom math_tools import add_one\n\n"
            "class TestMathTools(unittest.TestCase):\n"
            "    def test_add_one(self):\n"
            "        self.assertEqual(add_one(1), 2)\n"
        )
        stderr = (
            "FAIL: test_add_one (test_math_tools.TestMathTools.test_add_one)\n"
            f"  File \"{runtime.execution_root / 'tests' / 'test_math_tools.py'}\", line 5, in test_add_one\n"
            f"  File \"{runtime.execution_root / 'math_tools.py'}\", line 2, in add_one\n"
            "AssertionError: 1 != 2\n"
        )
        steps = [
            tool_step("write_file", "math_tools.py", content=impl),
            tool_step("write_file", "tests/test_math_tools.py", content=test),
            run_step("python3 -m unittest discover -s tests", ok=False, stderr=stderr),
            tool_step("read_file", "tests/test_math_tools.py", content=test),
            tool_step("read_file", "math_tools.py", content=impl),
            tool_step("replace_text", "math_tools.py", content=fixed_impl),
        ]

        state = runtime._implementation_task_progress_state(
            user_message=message,
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertTrue(state["successful_edit_after_failed_unittest"])
        self.assertEqual(state["allowed_next_actions"], ["run_command python3 -m unittest discover -s tests"])

        allowed = runtime._implementation_task_phase_action_block(
            user_message=message,
            tool_name="run_command",
            tool_args={"command": "python3 -m unittest discover -s tests"},
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertIsNone(allowed)

        blocked_read = runtime._implementation_task_phase_action_block(
            user_message=message,
            tool_name="read_file",
            tool_args={"path": "math_tools.py"},
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertIsNotNone(blocked_read)
        self.assertEqual(
            blocked_read["reason_code"],
            "implementation_task_failed_unittest_requires_rerun_after_edit",
        )
        self.assertEqual(
            blocked_read["allowed_next_actions"],
            ["run_command python3 -m unittest discover -s tests"],
        )
        self.assertIn("再実行", blocked_read["message"])

    def test_successful_edit_after_failed_unittest_overrides_remaining_unread_paths(self) -> None:
        runtime = self.runtime()
        message = "Pythonで add_one(value) を実装し、tests/ にunittestを追加して検証してください。"
        impl = "def add_one(value):\n    return value\n"
        test = (
            "import unittest\nfrom math_tools import add_one\n\n"
            "class TestMathTools(unittest.TestCase):\n"
            "    def test_add_one(self):\n"
            "        self.assertEqual(add_one(1), 2)\n"
        )
        fixed_test = test.replace("add_one(1), 2", "add_one(1), 1")
        stderr = (
            "FAIL: test_add_one (test_math_tools.TestMathTools.test_add_one)\n"
            f"  File \"{runtime.execution_root / 'tests' / 'test_math_tools.py'}\", line 5, in test_add_one\n"
            f"  File \"{runtime.execution_root / 'math_tools.py'}\", line 2, in add_one\n"
            "AssertionError: 1 != 2\n"
        )
        steps = [
            tool_step("write_file", "math_tools.py", content=impl),
            tool_step("write_file", "tests/test_math_tools.py", content=test),
            run_step("python3 -m unittest discover -s tests", ok=False, stderr=stderr),
            tool_step("read_file", "tests/test_math_tools.py", content=test),
            tool_step("replace_text", "tests/test_math_tools.py", content=fixed_test),
        ]

        state = runtime._implementation_task_progress_state(
            user_message=message,
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        prompt = runtime._implementation_task_progress_prompt(state)

        self.assertTrue(state["successful_edit_after_failed_unittest"])
        self.assertIn("math_tools.py", state["failed_unittest_recovery_read_paths"])
        self.assertEqual(state["allowed_next_actions"], ["run_command python3 -m unittest discover -s tests"])
        self.assertEqual(runtime._implementation_task_schema_tool_names(state), ["run_command"])
        self.assertIn("成功編集後の唯一の次アクション", prompt)
        self.assertIn("run_command python3 -m unittest discover -s tests", prompt)

    def test_external_audit_required_allows_repeated_unittest_command(self) -> None:
        runtime = self.runtime()
        message = "Pythonで add_one(value) を実装し、tests/ にunittestを追加して検証してください。"
        impl = "def add_one(value):\n    return value + 1\n"
        test = (
            "import unittest\nfrom math_tools import add_one\n\n"
            "class TestMathTools(unittest.TestCase):\n"
            "    def test_add_one(self):\n"
            "        self.assertEqual(add_one(1), 2)\n"
        )
        command = "python3 -m unittest discover -s tests"
        steps = [
            tool_step("write_file", "math_tools.py", content=impl),
            tool_step("write_file", "tests/test_math_tools.py", content=test),
            run_step(command, ok=True, stderr=".\n----------------------------------------------------------------------\nRan 1 test in 0.000s\n\nOK\n"),
        ]
        tool_args = {"command": command}

        state = runtime._implementation_task_progress_state(
            user_message=message,
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertEqual(state["phase"], "external_audit_required")
        self.assertIsNotNone(runtime._redundant_command_reason(tool_args=tool_args, steps=steps))
        self.assertTrue(
            runtime._implementation_progress_allows_repeated_command(
                user_message=message,
                tool_args=tool_args,
                steps=steps,
                session_id="main",
                turn_workspace=runtime.execution_root,
            )
        )

    def test_no_op_replace_after_failed_unittest_forces_write_file_repair(self) -> None:
        runtime = self.runtime()
        message = "Pythonで add_one(value) を実装し、tests/ にunittestを追加して検証してください。"
        impl = "def add_one(value):\n    return value\n"
        test = (
            "import unittest\nfrom math_tools import add_one\n\n"
            "class TestMathTools(unittest.TestCase):\n"
            "    def test_add_one(self):\n"
            "        self.assertEqual(add_one(1), 2)\n"
        )
        stderr = (
            "FAIL: test_add_one (test_math_tools.TestMathTools.test_add_one)\n"
            f"  File \"{runtime.execution_root / 'tests' / 'test_math_tools.py'}\", line 5, in test_add_one\n"
            f"  File \"{runtime.execution_root / 'math_tools.py'}\", line 2, in add_one\n"
            "AssertionError: 1 != 2\n"
        )
        steps = [
            tool_step("write_file", "math_tools.py", content=impl),
            tool_step("write_file", "tests/test_math_tools.py", content=test),
            run_step("python3 -m unittest discover -s tests", ok=False, stderr=stderr),
            tool_step("read_file", "tests/test_math_tools.py", content=test),
            tool_step("read_file", "math_tools.py", content=impl),
            tool_step("replace_text", "math_tools.py", ok=False, failure_type="no_op_edit"),
        ]

        state = runtime._implementation_task_progress_state(
            user_message=message,
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertEqual(
            set(state["failed_unittest_no_match_write_only_paths"]),
            {"tests/test_math_tools.py", "math_tools.py"},
        )

        blocked = runtime._implementation_task_phase_action_block(
            user_message=message,
            tool_name="replace_text",
            tool_args={"path": "math_tools.py", "old_text": "return value", "new_text": "return value"},
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertEqual(blocked["reason_code"], "implementation_task_failed_unittest_requires_write_after_no_match")
        self.assertEqual(
            set(blocked["allowed_next_actions"]),
            {"write_file tests/test_math_tools.py", "write_file math_tools.py"},
        )

        allowed = runtime._implementation_task_phase_action_block(
            user_message=message,
            tool_name="write_file",
            tool_args={"path": "math_tools.py", "content": "def add_one(value):\n    return value + 1\n"},
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertIsNone(allowed)

    def test_failed_unittest_after_reads_blocks_unmatched_replace_text(self) -> None:
        runtime = self.runtime()
        message = "Pythonで add_one(value) を実装し、tests/ にunittestを追加して検証してください。"
        impl = "def add_one(value):\n    return value\n"
        test = (
            "import unittest\nfrom math_tools import add_one\n\n"
            "class TestMathTools(unittest.TestCase):\n"
            "    def test_add_one(self):\n"
            "        self.assertEqual(add_one(1), 2)\n"
        )
        stderr = (
            "FAIL: test_add_one (test_math_tools.TestMathTools.test_add_one)\n"
            f"  File \"{runtime.execution_root / 'tests' / 'test_math_tools.py'}\", line 5, in test_add_one\n"
            "AssertionError: 1 != 2\n"
        )
        steps = [
            tool_step("write_file", "math_tools.py", content=impl),
            tool_step("write_file", "tests/test_math_tools.py", content=test),
            run_step("python3 -m unittest discover -s tests", ok=False, stderr=stderr),
            tool_step("read_file", "tests/test_math_tools.py", content=test),
            tool_step("read_file", "math_tools.py", content=impl),
        ]
        (runtime.execution_root / "math_tools.py").write_text(impl, encoding="utf-8")

        blocked = runtime._implementation_task_phase_action_block(
            user_message=message,
            tool_name="replace_text",
            tool_args={
                "path": "math_tools.py",
                "old_text": "def add_one(value): return value",
                "new_text": "def add_one(value):\n    return value + 1\n",
            },
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertEqual(blocked["reason_code"], "implementation_task_failed_unittest_blocks_unmatched_replace_text")
        self.assertIn("current_source_excerpt", blocked)
        self.assertIn("def add_one(value):", blocked["current_source_excerpt"])
        self.assertIn("return value", blocked["current_source_excerpt"])
        visible_message = (
            f"replace_text がブロックされました: {blocked['message']}"
            "\ncurrent_source_excerpt for the next exact old_text:\n"
            + blocked["current_source_excerpt"]
        )
        self.assertIn("current_source_excerpt for the next exact old_text", visible_message)
        self.assertEqual(
            blocked["allowed_next_actions"],
            [
                "replace_text math_tools.py with a small unique old_text",
                "replace_text tests/test_math_tools.py with a small unique old_text",
                "write_file tests/test_math_tools.py",
                "write_file math_tools.py",
            ],
        )

    def test_failed_unittest_blocks_block_header_only_replace_text(self) -> None:
        runtime = self.runtime()
        message = "Pythonで add_one(value) を実装し、tests/ にunittestを追加して検証してください。"
        impl = "def add_one(value):\n    return value\n"
        test = (
            "import unittest\nfrom math_tools import add_one\n\n"
            "class TestMathTools(unittest.TestCase):\n"
            "    def test_add_one(self):\n"
            "        self.assertEqual(add_one(1), 2)\n"
        )
        stderr = (
            "FAIL: test_add_one (test_math_tools.TestMathTools.test_add_one)\n"
            f"  File \"{runtime.execution_root / 'tests' / 'test_math_tools.py'}\", line 5, in test_add_one\n"
            "AssertionError: 1 != 2\n"
        )
        steps = [
            tool_step("write_file", "math_tools.py", content=impl),
            tool_step("write_file", "tests/test_math_tools.py", content=test),
            run_step("python3 -m unittest discover -s tests", ok=False, stderr=stderr),
            tool_step("read_file", "tests/test_math_tools.py", content=test),
            tool_step("read_file", "math_tools.py", content=impl),
        ]
        (runtime.execution_root / "math_tools.py").write_text(impl, encoding="utf-8")

        blocked = runtime._implementation_task_phase_action_block(
            user_message=message,
            tool_name="replace_text",
            tool_args={
                "path": "math_tools.py",
                "old_text": "def add_one(value):",
                "new_text": "def add_one(value):\n    return value + 1\n",
            },
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )

        self.assertEqual(blocked["reason_code"], "implementation_task_failed_unittest_blocks_block_header_replace_text")
        self.assertEqual(blocked["exact_old_text_matches"], 1)
        self.assertTrue(blocked["block_header_only_replace"])
        self.assertEqual(blocked["allowed_next_actions"], ["write_file math_tools.py"])
        self.assertIn("ブロックヘッダ1行だけ", blocked["message"])

    def test_failed_unittest_after_block_header_replace_allows_write_only_next(self) -> None:
        runtime = self.runtime()
        message = "Pythonで add_one(value) を実装し、tests/ にunittestを追加して検証してください。"
        impl = "def add_one(value):\n    return value\n"
        test = (
            "import unittest\nfrom math_tools import add_one\n\n"
            "class TestMathTools(unittest.TestCase):\n"
            "    def test_add_one(self):\n"
            "        self.assertEqual(add_one(1), 2)\n"
        )
        stderr = (
            "FAIL: test_add_one (test_math_tools.TestMathTools.test_add_one)\n"
            f"  File \"{runtime.execution_root / 'tests' / 'test_math_tools.py'}\", line 5, in test_add_one\n"
            "AssertionError: 1 != 2\n"
        )
        steps = [
            tool_step("write_file", "math_tools.py", content=impl),
            tool_step("write_file", "tests/test_math_tools.py", content=test),
            run_step("python3 -m unittest discover -s tests", ok=False, stderr=stderr),
            tool_step("read_file", "math_tools.py", content=impl),
        ]
        runtime._append_session_event(
            "main",
            {
                "type": "system_note",
                "role": "system",
                "code": "implementation_task_progress_blocked",
                "reason_code": "implementation_task_failed_unittest_blocks_block_header_replace_text",
                "details": {
                    "reason_code": "implementation_task_failed_unittest_blocks_block_header_replace_text",
                    "phase": "unittest_failed_needs_fix",
                    "path": "math_tools.py",
                },
            },
        )

        state = runtime._implementation_task_progress_state(
            user_message=message,
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )

        self.assertEqual(state["phase"], "unittest_failed_needs_fix")
        self.assertEqual(state["failed_unittest_no_match_write_only_paths"], ["math_tools.py"])
        self.assertEqual(
            state["allowed_next_actions"],
            ["read_file tests/test_math_tools.py once"],
        )

    def test_failed_unittest_import_error_targets_missing_export_repair(self) -> None:
        runtime = self.runtime()
        message = "Pythonでパズルsolverを実装し、tests/ にunittestを追加して検証してください。"
        impl = "goal_state = [1, 2, 3, 0]\n\ndef solve():\n    return goal_state\n"
        test = (
            "import unittest\n"
            "from puzzle import GOAL_STATE, solve\n\n"
            "class TestPuzzle(unittest.TestCase):\n"
            "    def test_solve(self):\n"
            "        self.assertEqual(solve(), GOAL_STATE)\n"
        )
        test_path = runtime.execution_root / "tests" / "test_puzzle.py"
        stderr = (
            "E\n"
            "======================================================================\n"
            "ERROR: test_puzzle (unittest.loader._FailedTest.test_puzzle)\n"
            "----------------------------------------------------------------------\n"
            "ImportError: Failed to import test module: test_puzzle\n"
            "Traceback (most recent call last):\n"
            f"  File \"{test_path}\", line 2, in <module>\n"
            "    from puzzle import GOAL_STATE, solve\n"
            "ImportError: cannot import name 'GOAL_STATE' from 'puzzle' "
            f"({runtime.execution_root / 'puzzle.py'})\n"
        )
        steps = [
            tool_step("write_file", "puzzle.py", content=impl),
            tool_step("write_file", "tests/test_puzzle.py", content=test),
            run_step("python3 -m unittest discover -s tests", ok=False, stderr=stderr),
            tool_step("read_file", "puzzle.py", content=impl),
        ]
        (runtime.execution_root / "puzzle.py").write_text(impl, encoding="utf-8")

        state = runtime._implementation_task_progress_state(
            user_message=message,
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )

        self.assertEqual(state["failed_unittest_recovery_read_paths"], ["puzzle.py"])
        self.assertEqual(state["failed_unittest_recovery_read_consumed_paths"], ["puzzle.py"])
        self.assertEqual(
            state["allowed_next_actions"],
            ["replace_text puzzle.py with a small unique old_text", "write_file puzzle.py"],
        )
        self.assertEqual(
            state["latest_unittest_missing_import"],
            {"name": "GOAL_STATE", "module": "puzzle", "source_file": "test_puzzle.py", "source_line": "2"},
        )
        self.assertTrue(
            any("GOAL_STATE" in hint and "module-level export" in hint for hint in state["unittest_repair_hints"])
        )

        blocked_read = runtime._implementation_task_phase_action_block(
            user_message=message,
            tool_name="read_file",
            tool_args={"path": "puzzle.py"},
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertEqual(blocked_read["reason_code"], "implementation_task_failed_unittest_read_already_consumed")
        self.assertNotIn("read_file puzzle.py once", blocked_read["allowed_next_actions"])

        broad_write = runtime._implementation_task_phase_action_block(
            user_message=message,
            tool_name="write_file",
            tool_args={"path": "puzzle.py", "content": impl + ("\n# filler\n" * 200)},
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertEqual(
            broad_write["reason_code"],
            "implementation_task_failed_unittest_blocks_broad_write_for_import_error",
        )
        self.assertEqual(broad_write["allowed_next_actions"], ["replace_text puzzle.py with a small unique old_text"])

        targeted_replace = runtime._implementation_task_phase_action_block(
            user_message=message,
            tool_name="replace_text",
            tool_args={
                "path": "puzzle.py",
                "old_text": "goal_state = [1, 2, 3, 0]",
                "new_text": "GOAL_STATE = [1, 2, 3, 0]\ngoal_state = GOAL_STATE",
            },
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertIsNone(targeted_replace)

    def test_failed_unittest_after_reads_blocks_broad_replace_text(self) -> None:
        runtime = self.runtime()
        message = "Pythonで add_one(value) を実装し、tests/ にunittestを追加して検証してください。"
        impl = (
            "def add_one(value):\n"
            "    return value\n"
            + "\n".join(f"# filler {index}" for index in range(180))
            + "\n"
        )
        test = (
            "import unittest\nfrom math_tools import add_one\n\n"
            "class TestMathTools(unittest.TestCase):\n"
            "    def test_add_one(self):\n"
            "        self.assertEqual(add_one(1), 2)\n"
        )
        stderr = (
            "FAIL: test_add_one (test_math_tools.TestMathTools.test_add_one)\n"
            f"  File \"{runtime.execution_root / 'tests' / 'test_math_tools.py'}\", line 5, in test_add_one\n"
            "AssertionError: 1 != 2\n"
        )
        steps = [
            tool_step("write_file", "math_tools.py", content=impl),
            tool_step("write_file", "tests/test_math_tools.py", content=test),
            run_step("python3 -m unittest discover -s tests", ok=False, stderr=stderr),
            tool_step("read_file", "tests/test_math_tools.py", content=test),
            tool_step("read_file", "math_tools.py", content=impl),
        ]
        (runtime.execution_root / "math_tools.py").write_text(impl, encoding="utf-8")

        blocked = runtime._implementation_task_phase_action_block(
            user_message=message,
            tool_name="replace_text",
            tool_args={
                "path": "math_tools.py",
                "old_text": impl,
                "new_text": impl.replace("    return value\n", "    return value + 1\n", 1),
            },
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertEqual(blocked["reason_code"], "implementation_task_failed_unittest_blocks_broad_replace_text")
        self.assertTrue(blocked["broad_rewrite"])

    def test_run_command_prompt_preserves_traceback_line_and_exception_tail(self) -> None:
        runtime = self.runtime()
        stderr = (
            "FAIL: test_big (tests.test_big.TestBig.test_big)\n"
            + "\n".join(f"noise line {index}" for index in range(120))
            + f"\n  File \"{runtime.execution_root / 'tests' / 'test_big.py'}\", line 999, in test_big\n"
            "    self.assertEqual(actual, expected)\n"
            "AssertionError: {'actual': 1} != {'expected': 2}\n"
        )
        event = {
            "type": "tool_result",
            "tool_name": "run_command",
            "content": json.dumps(
                {
                    "ok": False,
                    "tool": "run_command",
                    "command": "python3 -m unittest discover -s tests",
                    "returncode": 1,
                    "cwd": str(runtime.execution_root),
                    "stdout": "",
                    "stderr": stderr,
                },
                ensure_ascii=False,
            ),
        }

        rendered = runtime._render_tool_result_context(event)

        self.assertIn("returncode=1", rendered)
        self.assertIn("line 999", rendered)
        self.assertIn("AssertionError", rendered)
        self.assertIn("stderr_tail", rendered)

    def test_failed_unittest_with_traceback_does_not_block_on_consultant(self) -> None:
        runtime = self.runtime(responses=[])
        message = "Pythonで add_one(value) を実装し、tests/ にunittestを追加して検証してください。"
        stderr = (
            "FAIL: test_add_one (test_math_tools.TestMathTools.test_add_one)\n"
            f"  File \"{runtime.execution_root / 'tests' / 'test_math_tools.py'}\", line 5, in test_add_one\n"
            "AssertionError: 1 != 2\n"
        )
        note = runtime._validation_failure_consultant_note(
            user_message=message,
            tool_result={
                "ok": False,
                "command": "python3 -m unittest discover -s tests",
                "returncode": 1,
                "stderr": stderr,
                "stdout": "",
            },
            steps=[],
            turn_workspace=runtime.execution_root,
            current_model="test-model",
        )

        self.assertIsNone(note)
        self.assertEqual(runtime.llm_backend.messages_seen, [])

    def test_unittest_failed_semantic_review_does_not_delay_concrete_traceback_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = self.runtime(root, responses=[])
            message = "Pythonで add_one(value) を実装し、tests/ にunittestを追加して検証してください。"
            impl = "def add_one(value):\n    return value\n"
            test = (
                "import unittest\nfrom math_tools import add_one\n\n"
                "class TestMathTools(unittest.TestCase):\n"
                "    def test_add_one(self):\n"
                "        self.assertEqual(add_one(1), 2)\n"
            )
            (runtime.execution_root / "math_tools.py").write_text(impl, encoding="utf-8")
            (runtime.execution_root / "tests").mkdir(parents=True, exist_ok=True)
            (runtime.execution_root / "tests" / "test_math_tools.py").write_text(test, encoding="utf-8")
            steps = [
                tool_step("write_file", "math_tools.py", content=impl),
                tool_step("write_file", "tests/test_math_tools.py", content=test),
            ]
            failed_result = {
                "ok": False,
                "command": "python3 -m unittest discover -s tests",
                "returncode": 1,
                "stderr": (
                    "FAIL: test_add_one (test_math_tools.TestMathTools.test_add_one)\n"
                    f"  File \"{runtime.execution_root / 'tests' / 'test_math_tools.py'}\", line 5, in test_add_one\n"
                    "AssertionError: 1 != 2\n"
                ),
                "stdout": "",
            }

            appended = runtime._append_semantic_implementation_review_if_needed(
                session_id="main",
                turn_id="turn",
                queue_id="queue",
                step_index=3,
                turn_workspace=runtime.execution_root,
                user_message=message,
                steps=[*steps, {"tool_name": "run_command", "tool_args": {"command": failed_result["command"]}, "tool_result": failed_result}],
                current_model="test-model",
                trigger="unittest_failed",
                failed_tool_result=failed_result,
            )

            self.assertFalse(appended)
            self.assertEqual(runtime.llm_backend.messages_seen, [])

    def test_artifacts_ready_semantic_review_skips_consultant_when_no_runtime_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = self.runtime(root, responses=[])
            message = "Pythonで add_one(value) を実装し、tests/ にunittestを追加して検証してください。"
            impl = "def add_one(value):\n    return value + 1\n"
            test = (
                "import unittest\nfrom math_tools import add_one\n\n"
                "class TestMathTools(unittest.TestCase):\n"
                "    def test_add_one(self):\n"
                "        self.assertEqual(add_one(1), 2)\n"
            )
            (runtime.execution_root / "math_tools.py").write_text(impl, encoding="utf-8")
            (runtime.execution_root / "tests").mkdir(parents=True, exist_ok=True)
            (runtime.execution_root / "tests" / "test_math_tools.py").write_text(test, encoding="utf-8")
            steps = [
                tool_step("write_file", "math_tools.py", content=impl),
                tool_step("write_file", "tests/test_math_tools.py", content=test),
            ]

            appended = runtime._append_semantic_implementation_review_if_needed(
                session_id="main",
                turn_id="turn",
                queue_id="queue",
                step_index=2,
                turn_workspace=runtime.execution_root,
                user_message=message,
                steps=steps,
                current_model="test-model",
                trigger="artifacts_ready",
            )

            self.assertFalse(appended)
            self.assertEqual(runtime.llm_backend.messages_seen, [])

    def test_artifacts_ready_semantic_review_uses_runtime_observation_without_consultant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = self.runtime(root, responses=[])
            message = "Pythonで add_one(value) を実装し、tests/ にunittestを追加して検証してください。"
            impl = "def add_one(value):\n    return value + 1\n"
            test = (
                "import unittest\n\n"
                "class TestMathTools(unittest.TestCase):\n"
                "    def test_add_one_indirect(self):\n"
                "        self.assertEqual(2, 2)\n"
            )
            (runtime.execution_root / "math_tools.py").write_text(impl, encoding="utf-8")
            (runtime.execution_root / "tests").mkdir(parents=True, exist_ok=True)
            (runtime.execution_root / "tests" / "test_math_tools.py").write_text(test, encoding="utf-8")
            steps = [
                tool_step("write_file", "math_tools.py", content=impl),
                tool_step("write_file", "tests/test_math_tools.py", content=test),
            ]

            appended = runtime._append_semantic_implementation_review_if_needed(
                session_id="main",
                turn_id="turn",
                queue_id="queue",
                step_index=2,
                turn_workspace=runtime.execution_root,
                user_message=message,
                steps=steps,
                current_model="test-model",
                trigger="artifacts_ready",
            )
            events = read_jsonl(root / "state" / "sessions" / "main" / "events.jsonl")

            self.assertTrue(appended)
            self.assertEqual(runtime.llm_backend.messages_seen, [])
            self.assertEqual(events[-1]["code"], "semantic_implementation_review")
            self.assertEqual(events[-1]["reason_code"], "runtime_semantic_review_requires_revision")
            self.assertIn("runtime観測レビュー", events[-1]["content"])

    def test_non_command_tool_failure_prompt_preserves_recovery_contract(self) -> None:
        runtime = self.runtime()
        event = {
            "type": "tool_result",
            "tool_name": "replace_text",
            "content": json.dumps(
                {
                    "ok": False,
                    "tool": "replace_text",
                    "path": "math_tools.py",
                    "error": "old_text must match exactly once; matched 0 times",
                    "failure_type": "replace_text_no_match",
                    "blocked_by": "runtime_edit_validation",
                    "allowed_next_actions": [
                        {"tool": "read_file", "strategy": "inspect the current file"},
                        {"tool": "write_file", "strategy": "rewrite complete valid source"},
                    ],
                    "next_required_action": "read current source before retrying",
                },
                ensure_ascii=False,
            ),
        }

        rendered = runtime._render_tool_result_context(event)

        self.assertIn("failure_type=replace_text_no_match", rendered)
        self.assertIn("old_text must match exactly once", rendered)
        self.assertIn("allowed_next_actions", rendered)
        self.assertIn("next_required_action=read current source", rendered)

    def test_tool_validation_failures_include_block_owner_and_next_action(self) -> None:
        runtime = self.runtime()

        syntax_result = runtime.tools.execute(
            "write_file",
            {"path": "broken.py", "content": "def broken(:\n    pass\n"},
        )
        self.assertFalse(syntax_result["ok"])
        self.assertEqual(syntax_result["failure_type"], "validation_failed")
        self.assertEqual(syntax_result["blocked_by"], "runtime_python_syntax_validation")
        self.assertIn("next_required_action", syntax_result)

        large_result = runtime.tools.execute(
            "write_file",
            {"path": "notes.txt", "content": "x" * (runtime.tools.content_chunk_max_bytes * 2 + 1)},
        )
        self.assertFalse(large_result["ok"])
        self.assertEqual(large_result["failure_type"], "content_too_large")
        self.assertEqual(large_result["blocked_by"], "runtime_content_size_policy")
        self.assertIn("next_required_action", large_result)

        ok = runtime.tools.execute("write_file", {"path": "math_tools.py", "content": "def add_one(value):\n    return value\n"})
        self.assertTrue(ok["ok"])
        before = (runtime.tools.root / "math_tools.py").read_text(encoding="utf-8")
        no_op_write_result = runtime.tools.execute(
            "write_file",
            {"path": "math_tools.py", "content": before},
        )
        self.assertFalse(no_op_write_result["ok"])
        self.assertEqual(no_op_write_result["failure_type"], "no_op_edit")
        self.assertEqual(no_op_write_result["blocked_by"], "runtime_edit_validation")
        self.assertIn("next_required_action", no_op_write_result)
        self.assertEqual((runtime.tools.root / "math_tools.py").read_text(encoding="utf-8"), before)

        no_op_result = runtime.tools.execute(
            "replace_text",
            {"path": "math_tools.py", "old_text": "return value", "new_text": "return value"},
        )
        self.assertFalse(no_op_result["ok"])
        self.assertEqual(no_op_result["failure_type"], "no_op_edit")
        self.assertEqual(no_op_result["blocked_by"], "runtime_edit_validation")
        self.assertIn("next_required_action", no_op_result)
        self.assertEqual((runtime.tools.root / "math_tools.py").read_text(encoding="utf-8"), before)

        replace_result = runtime.tools.execute(
            "replace_text",
            {"path": "math_tools.py", "old_text": "return missing", "new_text": "return value + 1"},
        )
        self.assertFalse(replace_result["ok"])
        self.assertEqual(replace_result["failure_type"], "replace_text_no_match")
        self.assertEqual(replace_result["blocked_by"], "runtime_edit_validation")
        self.assertIn("next_required_action", replace_result)

    def test_progress_block_prompt_preserves_candidate_failure_evidence(self) -> None:
        runtime = self.runtime()
        event = {
            "type": "system_note",
            "role": "system",
            "content": "write_file がブロックされました",
            "code": "implementation_task_progress_blocked",
            "reason_code": "implementation_task_phase_requires_contract_reducing_edit",
            "details": {
                "reason_code": "implementation_task_phase_requires_contract_reducing_edit",
                "failure_type": "implementation_contract_nonreducing_edit_loop",
                "blocked_tool": "write_file",
                "path": "math_tools.py",
                "phase": "implementation_present_needs_semantic_review",
                "blocked_by": "implementation_task_progress_controller",
                "missing_requirements": ["required_api is missing"],
                "candidate_missing_requirements": ["required_api is still missing"],
                "repair_hints": [
                    {
                        "line": 3,
                        "current_text": "def other_api(value):",
                        "reason": "public API name does not match",
                        "suggested_new_text": "def required_api(value):",
                    }
                ],
                "allowed_next_actions": ["replace_text math_tools.py"],
                "suggested_fix": "Rename the API with a small exact replacement.",
            },
        }

        rendered = "\n".join(
            runtime._render_action_context_events(
                recent_events=[event],
                steps=[],
                user_message="Pythonで required_api を実装してください。",
            )
        )

        self.assertIn("reason_code: implementation_task_phase_requires_contract_reducing_edit", rendered)
        self.assertIn("failure_type: implementation_contract_nonreducing_edit_loop", rendered)
        self.assertIn("提案後も残る未達", rendered)
        self.assertIn("required_api is still missing", rendered)
        self.assertIn("修復ヒント", rendered)
        self.assertIn("def required_api(value):", rendered)

    def test_edit_block_prompt_preserves_machine_readable_reason(self) -> None:
        runtime = self.runtime()
        event = {
            "type": "system_note",
            "role": "system",
            "content": "replace_text がブロックされました",
            "code": "edit_blocked",
            "reason_code": "read_file_required_after_edit_failed",
            "details": {
                "reason_code": "read_file_required_after_edit_failed",
                "previous_failure_type": "replace_text_no_match",
                "blocked_tool": "replace_text",
                "path": "math_tools.py",
                "blocked_by": "runtime_edit_validation",
                "allowed_next_actions": ["read_file math_tools.py once"],
                "next_required_action": "read the current source once before retrying",
            },
        }

        rendered = "\n".join(
            runtime._render_action_context_events(
                recent_events=[event],
                steps=[],
                user_message="Pythonで required_api を実装してください。",
            )
        )

        self.assertIn("reason_code: read_file_required_after_edit_failed", rendered)
        self.assertIn("failure_type: replace_text_no_match", rendered)
        self.assertIn("blocked_tool: replace_text", rendered)
        self.assertIn("path: math_tools.py", rendered)
        self.assertIn("next_required_action: read the current source once before retrying", rendered)

    def test_generic_system_note_prompt_fallback_preserves_recovery_fields(self) -> None:
        runtime = self.runtime()
        event = {
            "type": "system_note",
            "role": "system",
            "content": "generic block",
            "code": "command_similarity_warning",
            "reason_code": "similar_recent_command",
            "details": {
                "failure_type": "same_signature_retry",
                "blocked_by": "runtime_generic_gate",
                "blocked_tool": "run_command",
                "path": "tests/test_math_tools.py",
                "missing_requirements": ["previous failure signature did not change"],
                "allowed_next_actions": ["replace_text tests/test_math_tools.py"],
                "suggested_fix": "Change the failing source before retrying.",
                "next_required_action": "edit before rerun",
            },
        }

        rendered = "\n".join(
            runtime._render_action_context_events(
                recent_events=[event],
                steps=[],
                user_message="Pythonで required_api を実装してください。",
            )
        )

        self.assertIn("reason_code: similar_recent_command", rendered)
        self.assertIn("failure_type: same_signature_retry", rendered)
        self.assertIn("blocked_by: runtime_generic_gate", rendered)
        self.assertIn("previous failure signature did not change", rendered)
        self.assertIn("next_required_action: edit before rerun", rendered)

    def test_no_match_recovery_nonreducing_write_keeps_actionable_retry_context(self) -> None:
        runtime = self.runtime()
        message = "Pythonで required_api(text) を実装し、tests/ にunittestを追加して検証してください。"
        impl = "def other_api(text):\n    return text\n"
        steps = [
            tool_step("write_file", "tool.py", content=impl),
            {
                "tool_name": "replace_text",
                "tool_args": {"path": "tool.py", "old_text": "missing", "new_text": impl},
                "tool_result": {"ok": False, "path": "tool.py", "failure_type": "replace_text_no_match"},
            },
            tool_step("read_file", "tool.py", content=impl),
        ]

        blocked = runtime._implementation_task_phase_action_block(
            user_message=message,
            tool_name="write_file",
            tool_args={"path": "tool.py", "content": impl},
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )

        self.assertIsNotNone(blocked)
        assert blocked is not None
        self.assertEqual(blocked["reason_code"], "implementation_task_phase_requires_contract_reducing_edit")
        self.assertFalse(blocked.get("terminal_failure"))
        self.assertTrue(blocked["missing_requirements"])
        self.assertTrue(blocked["candidate_missing_requirements"])
        self.assertIn("missing_requirementsを減らす編集", blocked["suggested_fix"])

    def test_unittest_failure_progress_prompt_uses_stdout_when_stderr_is_empty(self) -> None:
        runtime = self.runtime()
        message = "Pythonで add_one(value) を実装し、tests/ にunittestを追加して検証してください。"
        impl = "def add_one(value):\n    return value\n"
        test = "import unittest\nfrom math_tools import add_one\n"
        stdout = (
            "FAIL: test_add_one\n"
            "  File \"tests/test_math_tools.py\", line 5, in test_add_one\n"
            "AssertionError: 1 != 2\n"
        )
        steps = [
            tool_step("write_file", "math_tools.py", content=impl),
            tool_step("write_file", "tests/test_math_tools.py", content=test),
            run_step("python3 -m unittest discover -s tests", ok=False, stdout=stdout),
        ]

        state = runtime._implementation_task_progress_state(
            user_message=message,
            steps=steps,
            turn_workspace=runtime.execution_root,
        )
        prompt = runtime._implementation_task_progress_prompt(state)

        self.assertEqual(state["phase"], "unittest_failed_needs_fix")
        self.assertIn("tests/test_math_tools.py", state["failed_unittest_recovery_read_paths"])
        self.assertIn("math_tools.py", state["failed_unittest_recovery_read_paths"])
        self.assertIn("stdout/stderr excerpt", prompt)
        self.assertIn("AssertionError: 1 != 2", prompt)

    def test_llm_output_issue_prompt_preserves_schema_validation_errors(self) -> None:
        runtime = self.runtime()
        event = {
            "type": "system_note",
            "role": "system",
            "content": "LLM応答がツール呼び出しJSONとして解釈できませんでした: schema_validation_failed",
            "code": "llm_output_issue",
            "reason_code": "schema_validation_failed",
            "details": {
                "current_phase": "IMPLEMENTATION_TASK_PROGRESS:unittest_failed_needs_fix",
                "failure_type": "schema_validation_failed",
                "blocked_by": "runtime_tool_schema",
                "raw_output_is_machine_json": True,
                "schema_validation_ok": False,
                "schema_validation": {
                    "errors": [
                        "tool_name must be one of ['write_file', 'read_file']",
                        "tool_args.path is required",
                    ]
                },
                "allowed_tool_names": ["write_file", "read_file"],
                "allowed_next_actions": ["read_file math_tools.py once", "write_file math_tools.py"],
                "missing_requirements": ["valid_tool_json"],
                "combined_text": "{\"tool_name\":\"finish\"}",
                "suggested_fix": "schema_validation_errorsを満たすtool_name/tool_argsだけで返してください。",
                "next_required_action": "return a valid tool call",
            },
        }

        prompt = runtime._build_prompt(
            goal_text="",
            recent_events=[event],
            steps=[],
            user_message="Pythonで add_one(value) を実装してください。",
        )

        self.assertIn("parse_issue: schema_validation_failed", prompt)
        self.assertIn("schema_validation_errors", prompt)
        self.assertIn("tool_args.path is required", prompt)
        self.assertIn("allowed_tool_names", prompt)
        self.assertIn("allowed_next_actions", prompt)
        self.assertIn("blocked_by: runtime_tool_schema", prompt)
        self.assertIn("next_required_action: return a valid tool call", prompt)
        self.assertIn("raw_output_preview", prompt)

    def test_timeout_run_command_prompt_preserves_timeout_recovery_contract(self) -> None:
        runtime = self.runtime()
        event = {
            "type": "tool_result",
            "tool_name": "run_command",
            "content": json.dumps(
                {
                    "ok": False,
                    "tool": "run_command",
                    "command": "python3 -m unittest discover -s tests",
                    "returncode": None,
                    "stdout": "",
                    "stderr": "Timed out after 5s",
                    "failure_type": "command_timeout",
                    "blocked_by": "runtime_command_timeout",
                    "timeout_seconds": 5,
                    "allowed_next_actions": ["run_command with a narrower command"],
                    "suggested_fix": "Narrow the command before retrying.",
                    "next_required_action": "narrow the command or edit the target before rerun",
                },
                ensure_ascii=False,
            ),
        }

        rendered = "\n".join(
            runtime._render_action_context_events(
                recent_events=[event],
                steps=[],
                user_message="Pythonで add_one(value) を実装してください。",
            )
        )

        self.assertIn("failure_type=command_timeout", rendered)
        self.assertIn("blocked_by=runtime_command_timeout", rendered)
        self.assertIn("timeout_seconds=5", rendered)
        self.assertIn("next_required_action=narrow the command", rendered)

    def test_command_failed_system_note_preserves_stdout_stderr_and_line_evidence(self) -> None:
        runtime = self.runtime()
        event = {
            "type": "system_note",
            "role": "system",
            "content": "リカバリモード：直前のコマンド `python3 -m unittest discover -s tests` が失敗しました",
            "code": "command_failed",
            "reason_code": "recovery_guidance",
            "details": {
                "command": "python3 -m unittest discover -s tests",
                "returncode": 1,
                "failure_type": "command_failed",
                "blocked_by": "runtime_command_result",
                "traceback_summary": "traceback_file_lines=tests/test_math_tools.py:5 in test_add_one | last_exception_line=AssertionError: 1 != 2",
                "stdout_tail": "FAIL: test_add_one\n  File \"tests/test_math_tools.py\", line 5, in test_add_one\nAssertionError: 1 != 2\n",
                "stderr_tail": "",
                "allowed_next_actions": ["read_file tests/test_math_tools.py once", "write_file math_tools.py"],
                "suggested_fix": "stdout/stderrの具体行を根拠に修正してください。",
                "next_required_action": "inspect or edit the failing target before rerunning the command",
            },
        }

        prompt = runtime._build_prompt(
            goal_text="",
            recent_events=[event],
            steps=[],
            user_message="Pythonで add_one(value) を実装し、tests/ にunittestを追加して検証してください。",
        )

        self.assertIn("command: python3 -m unittest discover -s tests", prompt)
        self.assertIn("returncode: 1", prompt)
        self.assertIn("tests/test_math_tools.py:5", prompt)
        self.assertIn("stdout_tail", prompt)
        self.assertIn("AssertionError: 1 != 2", prompt)
        self.assertIn("next_required_action: inspect or edit", prompt)

    def test_repetitive_in_progress_write_stream_is_stopped_with_actionable_context(self) -> None:
        prefix = (
            '{"analysis":"","assistant_message":"","tool_name":"write_file",'
            '"tool_args":{"path":"tests/test_generated.py","content":"'
        )
        repeated_chunks = [
            f"    # repeated exploratory fixture {index % 3}: this line should not continue forever\\n"
            for index in range(60)
        ]
        backend = StreamingFakeBackend([prefix, *repeated_chunks])
        root = Path(tempfile.mkdtemp())
        bootstrap_workspace(root)
        runtime = AgentRuntime(root, llm_backend=backend)
        runtime.runtime_config["json_retry_limit"] = 0
        runtime.runtime_config["machine_control_repetition_min_chars"] = 1000
        runtime.runtime_config["machine_control_repetition_min_similar_lines"] = 6

        telemetry = runtime._chat_with_repair(
            role="coding",
            model="test-model",
            prompt="Pythonで add_one(value) を実装し、tests/ にunittestを追加して検証してください。",
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            llm_workspace=str(runtime.execution_root),
            current_phase="IMPLEMENTATION_TASK_PROGRESS:tests_missing",
            suppress_frame_operations=True,
            allowed_tool_names=["write_file"],
        )

        self.assertEqual(telemetry["parse_issue"], "repetitive_output")
        metadata = telemetry["stream_metadata"]
        self.assertEqual(metadata["client_abort_reason"], "repetitive_output")
        self.assertGreater(metadata["accumulated_content_chars"], 400)

        issue_event = {
            "type": "system_note",
            "role": "system",
            "content": "LLM応答がツール呼び出しJSONとして解釈できませんでした: repetitive_output",
            "code": "llm_output_issue",
            "reason_code": "repetitive_output",
            "details": {
                "current_phase": "IMPLEMENTATION_TASK_PROGRESS:tests_missing",
                "combined_text": telemetry["combined_text"],
                "stream_metadata": metadata,
                "allowed_tool_names": ["write_file"],
                "schema_validation": telemetry["schema_validation"],
                "schema_validation_ok": telemetry["schema_validation_ok"],
            },
        }
        prompt = runtime._build_prompt(
            goal_text="",
            recent_events=[issue_event],
            steps=[],
            user_message="Pythonで add_one(value) を実装し、tests/ にunittestを追加して検証してください。",
        )
        self.assertIn("parse_issue: repetitive_output", prompt)
        self.assertIn("stream_abort_reason: repetitive_output", prompt)
        self.assertIn("accumulated_content_chars", prompt)
        self.assertIn("allowed_tool_names", prompt)

    def test_stream_char_limit_exits_same_call_repair_loop(self) -> None:
        prefix = (
            '{"analysis":"","assistant_message":"","tool_name":"write_file",'
            '"tool_args":{"path":"generated.py","content":"'
        )
        chunks = [prefix, *[f"def generated_{index}():\\n    return {index}\\n" for index in range(80)]]
        backend = StreamingFakeBackend(chunks)
        root = Path(tempfile.mkdtemp())
        bootstrap_workspace(root)
        runtime = AgentRuntime(root, llm_backend=backend)
        runtime.runtime_config["json_retry_limit"] = 2
        runtime.runtime_config["max_machine_control_stream_chars"] = 600
        runtime.runtime_config["implementation_task_machine_control_stream_chars"] = 600

        telemetry = runtime._chat_with_repair(
            role="coding",
            model="test-model",
            prompt="Pythonで add_one(value) を実装し、tests/ にunittestを追加して検証してください。",
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            llm_workspace=str(runtime.execution_root),
            current_phase="IMPLEMENTATION_TASK_PROGRESS:implementation_missing",
            suppress_frame_operations=True,
            allowed_tool_names=["write_file"],
        )

        self.assertEqual(telemetry["parse_issue"], "stream_char_limit")
        self.assertEqual(len(backend.messages_seen), 1)
        self.assertEqual(telemetry["stream_metadata"]["client_abort_reason"], "stream_char_limit")

    def test_stream_char_limit_records_actionable_llm_output_issue_without_crash(self) -> None:
        prefix = (
            '{"analysis":"","assistant_message":"","tool_name":"write_file",'
            '"tool_args":{"path":"generated.py","content":"'
        )
        chunks = [prefix, *[f"def generated_{index}():\\n    return {index}\\n" for index in range(80)]]
        backend = StreamingFakeBackend(chunks)
        root = Path(tempfile.mkdtemp())
        bootstrap_workspace(root)
        runtime = AgentRuntime(root, llm_backend=backend)
        runtime.runtime_config["json_retry_limit"] = 2
        runtime.runtime_config["max_machine_control_stream_chars"] = 600
        runtime.runtime_config["implementation_task_machine_control_stream_chars"] = 600
        runtime.config.setdefault("runtime", {})["max_steps_per_message"] = 1
        runtime.config.setdefault("runtime", {})["verified_implementation_max_steps"] = 1
        enqueue_message(root, "Pythonで add_one(value) を実装し、tests/ にunittestを追加して検証してください。")

        result = runtime.run_until_idle(
            max_work_items=1,
            selection_override={"role": "coding", "model": "test-model", "reason": "test"},
        )

        self.assertFalse(result["last_result"]["ok"])
        events = read_jsonl(runtime.paths.session_events_path("main"))
        issue_events = [
            event
            for event in events
            if event.get("type") == "system_note" and event.get("code") == "llm_output_issue"
        ]
        self.assertTrue(issue_events)
        details = issue_events[-1]["details"]
        self.assertEqual(details["failure_type"], "stream_char_limit")
        self.assertEqual(details["blocked_by"], "runtime_stream_guard")
        self.assertIn("smaller complete reference implementation", details["next_required_action"])

    def test_finish_block_prompt_preserves_missing_action_contract(self) -> None:
        runtime = self.runtime()
        event = {
            "type": "system_note",
            "role": "system",
            "content": "完了がブロックされました: 期待される成果物が見つかりません: math_tools.py",
            "code": "finish_blocked",
            "reason_code": "missing_expected_artifacts",
            "details": {
                "missing_artifacts": ["math_tools.py"],
                "missing_requirements": ["write_file math_tools.py"],
                "blocked_by": "finish_contract_expected_artifacts",
                "allowed_next_actions": ["write_file math_tools.py"],
                "suggested_fix": "期待される成果物を作成してください。",
                "next_required_action": "create the missing artifact: math_tools.py",
            },
        }

        prompt = runtime._build_prompt(
            goal_text="",
            recent_events=[event],
            steps=[],
            user_message="Pythonで add_one(value) を実装してください。",
        )

        self.assertIn("blocked_by: finish_contract_expected_artifacts", prompt)
        self.assertIn("write_file math_tools.py", prompt)
        self.assertIn("next_required_action: create the missing artifact", prompt)

    def test_recent_context_preserves_critical_llm_output_issue_outside_tail(self) -> None:
        runtime = self.runtime()
        runtime._append_session_event(
            "main",
            {
                "type": "system_note",
                "role": "system",
                "content": "LLM output invalid",
                "code": "llm_output_issue",
                "reason_code": "schema_validation_failed",
                "details": {"schema_validation": {"errors": ["bad enum"]}},
            },
        )
        for index in range(20):
            runtime._append_session_event(
                "main",
                {
                    "type": "planning_note",
                    "role": "system",
                    "content": f"ordinary note {index}",
                },
            )

        recent = runtime._recent_events_for_action_context(
            session_id="main",
            current_frame=None,
            limit=3,
        )

        self.assertTrue(any(event.get("code") == "llm_output_issue" for event in recent))

    def test_command_blocked_prompt_shows_allowed_action_and_block_owner(self) -> None:
        runtime = self.runtime()
        event = {
            "type": "system_note",
            "role": "system",
            "content": "run_command がブロックされました",
            "code": "command_blocked",
            "details": {
                "command": "python3 -m unittest discover -s tests",
                "blocked_tool": "run_command",
                "blocked_by": "runtime_command_gate",
                "allowed_next_actions": ["write_file math_tools.py"],
                "suggested_fix": "失敗原因を修正してから再実行してください。",
                "next_required_action": "write_file corrected implementation",
            },
        }

        prompt = runtime._build_prompt(
            goal_text="",
            recent_events=[event],
            steps=[],
            user_message="Pythonで add_one(value) を実装してください。",
        )

        self.assertIn("blocked_by: runtime_command_gate", prompt)
        self.assertIn("write_file math_tools.py", prompt)
        self.assertIn("write_file corrected implementation", prompt)

    def test_planner_action_blocked_prompt_preserves_workunit_shape_hint(self) -> None:
        runtime = self.runtime()
        event = {
            "type": "system_note",
            "role": "system",
            "content": "create_plan was blocked because PlanRecord is invalid",
            "code": "planner_action_blocked",
            "reason_code": "plan_record_invalid",
            "details": {
                "blocked_tool": "create_plan",
                "failure_type": "plan_record_invalid",
                "blocked_by": "planner_contract",
                "allowed_next_actions": ["create_plan"],
                "expected_shape": (
                    "Expected create_plan shape. Do not put WorkUnit fields such as goal, work_type, "
                    "success_evidence, should_open_child_frame, why_not_direct_action, context_summary, "
                    "or done_when inside first_action. first_action may contain only tool and args."
                ),
                "suggested_fix": "Repair the PlanRecord schema exactly.",
                "next_required_action": "retry create_plan with a valid PlanRecord",
            },
        }

        prompt = runtime._build_prompt(
            goal_text="",
            recent_events=[event],
            steps=[],
            current_phase="PLAN_REVISION",
            user_message="4x4 sliding puzzle を状態空間探索で解く実装を作ってください。",
        )

        self.assertIn("PlanRecord正本", prompt)
        self.assertIn("Do not put WorkUnit fields", prompt)
        self.assertIn("first_action may contain only tool and args", prompt)

    def test_first_action_required_prompt_preserves_expected_tool_call(self) -> None:
        runtime = self.runtime()
        event = {
            "type": "system_note",
            "role": "system",
            "content": "子フレームは最初の具体ツール結果を得る前に別の行動を選べません。",
            "code": "first_action_required",
            "details": {
                "requested_tool": "finish",
                "expected_tool": "read_file",
                "expected_args": {"path": "math_tools.py"},
            },
        }

        prompt = runtime._build_prompt(
            goal_text="",
            recent_events=[event],
            steps=[],
            user_message="Pythonで add_one(value) を実装してください。",
        )

        self.assertIn("blocked_tool: finish", prompt)
        self.assertIn("read_file", prompt)
        self.assertIn("math_tools.py", prompt)

    def test_plan_acceptance_block_prompt_preserves_reviewer_reason_and_actions(self) -> None:
        runtime = self.runtime()
        event = {
            "type": "system_note",
            "role": "system",
            "content": "decompose_tasks was blocked by plan acceptance gate",
            "code": "plan_acceptance_blocked",
            "details": {
                "issues": ["child task does not advance the requested implementation"],
                "review": {"rationale": "The plan creates a static note instead of editing code."},
                "allowed_next_actions": [
                    {"tool": "decompose_tasks", "strategy": "retry with code-producing tasks"},
                    {"tool": "finish", "strategy": "ask user to clarify"},
                ],
                "suggested_fix": "Create child tasks whose first_action edits or verifies the requested code.",
            },
        }

        prompt = runtime._build_prompt(
            goal_text="",
            recent_events=[event],
            steps=[],
            user_message="Pythonで add_one(value) を実装してください。",
        )

        self.assertIn("does not advance", prompt)
        self.assertIn("static note", prompt)
        self.assertIn("retry with code-producing tasks", prompt)
        self.assertIn("first_action edits", prompt)

    def test_frame_and_plan_block_events_include_standard_recovery_contract(self) -> None:
        runtime = self.runtime()

        work_note = runtime._work_package_blocked_event(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            turn_workspace=runtime.execution_root,
            tool_name="decompose_tasks",
            issues=["task-1: first_action.tool is required"],
            tasks=[],
            user_message="Pythonで add_one(value) を実装してください。",
        )
        work_details = work_note["details"]
        self.assertEqual(work_details["blocked_by"], "work_package_contract")
        self.assertIn("failure_type", work_details)
        self.assertIn("allowed_next_actions", work_details)
        self.assertIn("suggested_fix", work_details)
        self.assertIn("next_required_action", work_details)

        plan_note = runtime._plan_acceptance_blocked_event(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=2,
            turn_workspace=runtime.execution_root,
            tool_name="decompose_tasks",
            issues=["plan does not advance the requested implementation"],
            review={
                "reason_code": "plan_semantic_mismatch",
                "rationale": "The plan creates notes instead of code.",
                "suggested_next_action": "retry with code-producing first_action",
            },
            tasks=[],
        )
        plan_details = plan_note["details"]
        self.assertEqual(plan_details["blocked_by"], "plan_acceptance_gate")
        self.assertEqual(plan_details["failure_type"], "plan_acceptance_blocked")
        self.assertIn("allowed_next_actions", plan_details)
        self.assertIn("suggested_fix", plan_details)
        self.assertIn("next_required_action", plan_details)

        empty_plan = runtime._handle_decompose_tasks(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=3,
            tool_args={"tasks": []},
            turn_workspace=runtime.execution_root,
        )
        empty_details = empty_plan["event"]["details"]
        self.assertEqual(empty_details["blocked_by"], "work_package_contract")
        self.assertEqual(empty_details["failure_type"], "empty_task_plan")
        self.assertIn("next_required_action", empty_details)

        root_return = runtime._handle_return_to_parent(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=4,
            tool_args={"summary": "done"},
            turn_workspace=runtime.execution_root,
        )
        return_details = root_return["event"]["details"]
        self.assertEqual(return_details["blocked_by"], "frame_contract")
        self.assertEqual(return_details["failure_type"], "root_frame_cannot_return")
        self.assertIn("allowed_next_actions", return_details)
        self.assertIn("next_required_action", return_details)

    def test_problem_profile_selects_state_space_search_for_puzzle_like_request(self) -> None:
        runtime = self.runtime()
        user_message = "4x4 sliding puzzle を、状態、合法手、ゴール、探索方針で解くPython実装を作ってください。"

        profile = runtime._planning_profile_for_message(user_message)
        phase = runtime._current_phase(user_message=user_message, steps=[], recent_events=[])

        self.assertEqual(profile["strategy"], "state_space_search")
        self.assertEqual(phase, "PLANNING_REQUIRED")

    def test_problem_profile_prefers_explicit_dynamic_programming_over_weak_scheduling_word(self) -> None:
        runtime = self.runtime()
        user_message = (
            "Pythonで weighted interval scheduling を解く実装とunittestを作ってください。"
            "動的計画法で、部分問題、基底ケース、漸化式を検証してください。"
        )

        profile = runtime._planning_profile_for_message(user_message)

        self.assertEqual(profile["strategy"], "dynamic_programming")
        self.assertIn("dynamic_programming:動的計画", profile["signals"])
        self.assertIn("constraint_weak:scheduling", profile["signals"])

    def test_problem_profile_does_not_treat_display_or_generic_graph_as_dp(self) -> None:
        runtime = self.runtime()
        user_message = "Pythonでトポロジカルソートを実装し、unittestで検証し、サンプルグラフで実行して並び順を表示して"

        profile = runtime._planning_profile_for_message(user_message)

        self.assertEqual(profile["strategy"], "direct_implementation")
        self.assertIn("graph:グラフ", profile["signals"])
        self.assertNotIn("dynamic_programming:表", profile["signals"])

    def test_problem_profile_selects_graph_shortest_path_only_for_shortest_path_requests(self) -> None:
        runtime = self.runtime()
        user_message = "Pythonでグラフの最短経路をDijkstraで解く実装と検証を作ってください。"

        profile = runtime._planning_profile_for_message(user_message)

        self.assertEqual(profile["strategy"], "graph_shortest_path")
        self.assertIn("graph:グラフ", profile["signals"])
        self.assertTrue(
            any(str(item).startswith("graph_shortest_path:") for item in profile["signals"])
        )

    def test_stale_plan_record_before_latest_user_message_does_not_bypass_planning_required(self) -> None:
        runtime = self.runtime()
        old_message = "古い状態空間探索タスクを作ってください。"
        new_message = "4x4 sliding puzzle を、状態、合法手、ゴール、探索方針で解くPython実装を作ってください。"
        runtime._append_session_event(
            "main",
            {"type": "user_message", "role": "user", "content": old_message},
        )
        runtime._append_session_event(
            "main",
            {
                "type": "plan_record",
                "role": "system",
                "plan": self.state_space_plan(runtime, old_message),
            },
        )
        runtime._append_session_event(
            "main",
            {"type": "user_message", "role": "user", "content": new_message},
        )
        recent_events = read_jsonl(runtime.paths.session_events_path("main"))

        phase = runtime._current_phase(
            user_message=new_message,
            steps=[],
            recent_events=recent_events,
        )

        self.assertEqual(phase, "PLANNING_REQUIRED")
        blocked = runtime._planner_action_blocked_event(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            turn_workspace=runtime.execution_root,
            tool_name="write_file",
            user_message=new_message,
            current_phase=phase,
            recent_events=recent_events,
            steps=[],
        )
        self.assertIsNotNone(blocked)
        assert blocked is not None
        self.assertEqual(blocked["reason_code"], "planning_required_before_execution")

    def test_create_plan_rejects_edit_work_unit_noop_run_command_first_action(self) -> None:
        runtime = self.runtime()
        user_message = "4x4 sliding puzzle を、状態、合法手、ゴール、探索方針で解くPython実装を作ってください。"
        plan = self.state_space_plan(runtime, user_message)
        plan["work_units"][0]["first_action"] = {
            "tool": "run_command",
            "args": {"command": "echo 'Implementing solver'"},
        }

        result = runtime._handle_create_plan(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            tool_args={"plan": plan},
            turn_workspace=runtime.execution_root,
            user_message=user_message,
            current_model="test-model",
        )

        self.assertFalse(result["ok"])
        self.assertIn(
            "edit WorkUnit first_action must not be a no-op run_command",
            result["event"]["content"],
        )
        events = read_jsonl(runtime.paths.session_events_path("main"))
        self.assertFalse(any(event.get("type") == "plan_record" for event in events))

    def test_planning_execution_request_uses_verified_implementation_step_budget(self) -> None:
        runtime = self.runtime()
        runtime.runtime_config["verified_implementation_max_steps"] = 32
        runtime.runtime_config["planned_implementation_max_steps"] = 48

        budget = runtime._effective_max_steps_per_message(
            user_message="１５パズルのプログラムを作り、それを実行して自分でクリアしてみて",
            configured=12,
        )

        self.assertEqual(budget, 48)

    def test_non_planning_implementation_request_uses_verified_budget(self) -> None:
        runtime = self.runtime()
        runtime.runtime_config["verified_implementation_max_steps"] = 32
        runtime.runtime_config["planned_implementation_max_steps"] = 48

        budget = runtime._effective_max_steps_per_message(
            user_message="Pythonで add_one(value) を実装し、unittestで検証してください。",
            configured=12,
        )

        self.assertEqual(budget, 32)

    def test_simple_one_file_implementation_does_not_require_planning_record(self) -> None:
        runtime = self.runtime()

        phase = runtime._current_phase(
            user_message="Pythonで add_one(value) を実装してください。",
            steps=[],
            recent_events=[],
        )

        self.assertNotEqual(phase, "PLANNING_REQUIRED")

    def test_state_space_search_plan_requires_verification_contract(self) -> None:
        runtime = self.runtime()
        user_message = "4x4 sliding puzzle を状態空間探索で解く実装を作ってください。"
        plan = self.state_space_plan(runtime, user_message)
        del plan["verification_contract"]["final_verifier"]

        result = runtime._handle_create_plan(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            tool_args={"plan": plan},
            turn_workspace=runtime.execution_root,
            user_message=user_message,
            current_model="test-model",
        )

        self.assertFalse(result["ok"])
        event = result["event"]
        self.assertEqual(event["code"], "planner_action_blocked")
        self.assertEqual(event["details"]["failure_type"], "state_space_search_plan_missing_verifier")
        self.assertIn("final_verifier", "\n".join(event["details"]["issues"]))
        self.assertEqual(event["details"]["allowed_next_actions"], ["create_plan"])
        self.assertIn("Expected create_plan shape", event["details"]["suggested_fix"])
        self.assertIn("Do not put WorkUnit fields", event["content"])

    def test_state_space_plan_strategy_mismatch_is_rejected_before_plan_record(self) -> None:
        runtime = self.runtime()
        user_message = "4x4 sliding puzzle を状態空間探索で解く実装を作ってください。"
        plan = self.state_space_plan(runtime, user_message)
        plan["strategy"] = "task_decomposition"

        result = runtime._handle_create_plan(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            tool_args={"plan": plan},
            turn_workspace=runtime.execution_root,
            user_message=user_message,
            current_model="test-model",
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["event"]["details"]["failure_type"], "planner_strategy_mismatch")
        events = read_jsonl(runtime.paths.session_events_path("main"))
        self.assertFalse(any(event.get("type") == "plan_record" for event in events))

    def test_state_space_plan_requires_execution_verifier_work_unit(self) -> None:
        runtime = self.runtime()
        user_message = "4x4 sliding puzzle を状態空間探索で解く実装を作ってください。"
        plan = self.state_space_plan(runtime, user_message)
        plan["work_units"][1]["goal"] = "Verify the implementation works correctly."
        plan["work_units"][1]["success_evidence"] = "tests pass"

        result = runtime._handle_create_plan(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            tool_args={"plan": plan},
            turn_workspace=runtime.execution_root,
            user_message=user_message,
            current_model="test-model",
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["event"]["details"]["failure_type"], "state_space_search_plan_missing_execution_verifier")
        self.assertIn("legal action replay", "\n".join(result["event"]["details"]["issues"]))
        self.assertIn("run_test WorkUnit", result["event"]["details"]["suggested_fix"])
        self.assertIn("implement-artifact", result["event"]["details"]["suggested_fix"])
        self.assertIn("verify-legal-replay", result["event"]["details"]["suggested_fix"])

    def test_dynamic_programming_plan_requires_verification_contract(self) -> None:
        runtime = self.runtime()
        user_message = "動的計画で部分問題、基底ケース、漸化式を使うPython実装を作ってください。"
        profile = runtime._planning_profile_for_message(user_message)
        self.assertEqual(profile["strategy"], "dynamic_programming")
        self.assertEqual(runtime._current_phase(user_message=user_message, steps=[], recent_events=[]), "PLANNING_REQUIRED")
        plan = self.dynamic_programming_plan(runtime, user_message)
        del plan["verification_contract"]["recurrence_verifier"]

        result = runtime._handle_create_plan(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            tool_args={"plan": plan},
            turn_workspace=runtime.execution_root,
            user_message=user_message,
            current_model="test-model",
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["event"]["details"]["failure_type"], "dynamic_programming_plan_missing_verifier")
        self.assertIn("recurrence_verifier", "\n".join(result["event"]["details"]["issues"]))
        self.assertIn("For dynamic_programming", result["event"]["details"]["suggested_fix"])

    def test_constraint_satisfaction_plan_requires_negative_validator_work_unit(self) -> None:
        runtime = self.runtime()
        user_message = "制約、変数、ドメイン、割当を使うPython実装を作ってください。"
        profile = runtime._planning_profile_for_message(user_message)
        self.assertEqual(profile["strategy"], "constraint_satisfaction")
        plan = self.constraint_satisfaction_plan(runtime, user_message)
        plan["work_units"][1]["goal"] = "Run tests for the implementation."
        plan["work_units"][1]["success_evidence"] = "tests pass"

        result = runtime._handle_create_plan(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            tool_args={"plan": plan},
            turn_workspace=runtime.execution_root,
            user_message=user_message,
            current_model="test-model",
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["event"]["details"]["failure_type"], "constraint_satisfaction_plan_missing_execution_verifier")
        issues = "\n".join(result["event"]["details"]["issues"])
        self.assertIn("invalid/negative assignment", issues)
        self.assertIn("For constraint_satisfaction", result["event"]["details"]["suggested_fix"])

    def test_create_plan_rejects_direct_python_script_as_run_test_first_action(self) -> None:
        runtime = self.runtime()
        user_message = "状態、合法手、ゴール、探索方針で解く小さなパズル実装とunittestを作ってください。"
        plan = self.state_space_plan(runtime, user_message)
        plan["work_units"][1]["first_action"] = {
            "tool": "run_command",
            "args": {"command": "python puzzle_solver.py"},
        }

        result = runtime._handle_create_plan(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            tool_args={"plan": plan},
            turn_workspace=runtime.execution_root,
            user_message=user_message,
            current_model="test-model",
        )

        self.assertFalse(result["ok"])
        issues = "\n".join(result["event"]["details"]["issues"])
        self.assertIn("non-interactive verification command", issues)
        self.assertIn("do not use direct `python script.py`", issues)

    def test_create_plan_rejects_unittest_module_run_as_run_test_first_action(self) -> None:
        runtime = self.runtime()
        user_message = "状態、合法手、ゴール、探索方針で解く小さなパズル実装とunittestを作ってください。"
        plan = self.state_space_plan(runtime, user_message)
        plan["work_units"][1]["first_action"] = {
            "tool": "run_command",
            "args": {"command": "python3 -m unittest test_puzzle15 -v"},
        }

        result = runtime._handle_create_plan(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            tool_args={"plan": plan},
            turn_workspace=runtime.execution_root,
            user_message=user_message,
            current_model="test-model",
        )

        self.assertFalse(result["ok"])
        issues = "\n".join(result["event"]["details"]["issues"])
        self.assertIn("python3 -m unittest discover -s tests", issues)

    def test_create_plan_rejects_embedded_edit_first_actions_before_plan_record(self) -> None:
        runtime = self.runtime()
        user_message = "4x4 sliding puzzle を状態空間探索で解く実装を作ってください。"
        plan = self.state_space_plan(runtime, user_message)
        plan["work_units"][0] = {
            "unit_id": "implementation",
            "goal": "Write the puzzle implementation.",
            "depends_on": [],
            "work_type": "edit",
            "first_action": {
                "tool": "write_file",
                "args": {"path": "fifteen_puzzle.py", "content": "class FifteenPuzzle:\n    pass\n"},
            },
            "success_evidence": "Implementation file exists.",
            "should_open_child_frame": True,
        }

        result = runtime._handle_create_plan(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            tool_args={"plan": plan},
            turn_workspace=runtime.execution_root,
            user_message=user_message,
            current_model="test-model",
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["event"]["details"]["failure_type"], "plan_work_unit_embedded_edit")
        self.assertIn("write_file", "\n".join(result["event"]["details"]["issues"]))
        self.assertIn("PLAN_EXECUTION", result["event"]["details"]["suggested_fix"])
        events = read_jsonl(runtime.paths.session_events_path("main"))
        self.assertFalse(any(event.get("type") == "plan_record_normalized" for event in events))
        self.assertFalse(any(event.get("type") == "plan_record" for event in events))

    def test_create_plan_rejects_inspect_only_plan_for_implementation_request(self) -> None:
        runtime = self.runtime()
        user_message = "15パズルのプログラムを作って実行してください。"
        plan = self.state_space_plan(runtime, user_message)
        plan["work_units"][0]["work_type"] = "inspect"
        plan["work_units"][0]["goal"] = "Inspect the puzzle state space before doing implementation."
        plan["work_units"][0]["success_evidence"] = "State-space notes are available."

        result = runtime._handle_create_plan(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            tool_args={"plan": plan},
            turn_workspace=runtime.execution_root,
            user_message=user_message,
            current_model="test-model",
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["event"]["details"]["failure_type"], "plan_scope_incomplete")
        self.assertIn("work_type=edit", "\n".join(result["event"]["details"]["issues"]))
        self.assertIn("implement-artifact", result["event"]["details"]["suggested_fix"])
        self.assertIn("verify-legal-replay", result["event"]["details"]["suggested_fix"])
        events = read_jsonl(runtime.paths.session_events_path("main"))
        self.assertFalse(any(event.get("type") == "plan_record" for event in events))

    def test_repeated_repairable_planner_failure_autorepairs_minimal_state_space_plan(self) -> None:
        runtime = self.runtime()
        user_message = "15パズルのプログラムを作って実行してください。"
        runtime._append_session_event(
            "main",
            {
                "type": "system_note",
                "role": "system",
                "content": "previous planner block",
                "code": "planner_action_blocked",
                "reason_code": "state_space_search_plan_missing_execution_verifier",
                "details": {"failure_type": "state_space_search_plan_missing_execution_verifier"},
            },
        )
        for index in range(400):
            runtime._append_session_event(
                "main",
                {
                    "type": "runtime_event",
                    "role": "system",
                    "content": f"stream chunk {index}",
                    "phase": "PLAN_REVISION",
                },
            )
        plan = self.state_space_plan(runtime, user_message)
        plan["work_units"] = [
            {
                "unit_id": "inspect-only",
                "goal": "Inspect the problem",
                "depends_on": [],
                "work_type": "inspect",
                "first_action": {"tool": "list_files", "args": {"path": "."}},
                "success_evidence": "workspace inspected",
                "should_open_child_frame": True,
            },
            {
                "unit_id": "verify-solution",
                "goal": "Verify that it works correctly",
                "depends_on": ["inspect-only"],
                "work_type": "run_test",
                "first_action": {"tool": "run_command", "args": {"command": "python 15_puzzle.py"}},
                "success_evidence": "program works correctly",
                "should_open_child_frame": True,
            },
        ]

        result = runtime._handle_create_plan(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            tool_args={"plan": plan},
            turn_workspace=runtime.execution_root,
            user_message=user_message,
            current_model="test-model",
        )

        self.assertTrue(result["ok"], result.get("error"))
        unit_ids = [task["task_id"] for task in result["tasks"]]
        self.assertEqual(unit_ids, ["inspect-workspace", "implement-artifact", "verify-legal-replay"])
        events = read_jsonl(runtime.paths.session_events_path("main"))
        self.assertTrue(any(event.get("code") == "plan_record_autorepaired" for event in events))
        plan_events = [event for event in events if event.get("type") == "plan_record"]
        self.assertTrue(plan_events)
        accepted_units = plan_events[-1]["plan"]["work_units"]
        self.assertEqual(accepted_units[-1]["first_action"]["args"]["command"], "python3 -m unittest discover -s tests")

    def test_repeated_dynamic_programming_planner_failure_autorepairs_minimal_dp_plan(self) -> None:
        runtime = self.runtime()
        user_message = (
            "Pythonで weighted interval scheduling を解く実装とunittestを作ってください。"
            "動的計画法で、部分問題、基底ケース、漸化式を検証してください。"
        )
        runtime._append_session_event(
            "main",
            {
                "type": "system_note",
                "role": "system",
                "content": "previous planner block",
                "code": "planner_action_blocked",
                "reason_code": "dynamic_programming_plan_missing_execution_verifier",
                "details": {"failure_type": "dynamic_programming_plan_missing_execution_verifier"},
            },
        )
        plan = self.dynamic_programming_plan(runtime, user_message)
        plan["work_units"] = [
            {
                "unit_id": "inspect-only",
                "goal": "Inspect the problem",
                "depends_on": [],
                "work_type": "inspect",
                "first_action": {"tool": "list_files", "args": {"path": "."}},
                "success_evidence": "workspace inspected",
                "should_open_child_frame": True,
            },
            {
                "unit_id": "verify-vague",
                "goal": "Verify that it works correctly",
                "depends_on": ["inspect-only"],
                "work_type": "run_test",
                "first_action": {"tool": "run_command", "args": {"command": "python3 -m unittest discover -s tests"}},
                "success_evidence": "tests pass",
                "should_open_child_frame": True,
            },
        ]

        result = runtime._handle_create_plan(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            tool_args={"plan": plan},
            turn_workspace=runtime.execution_root,
            user_message=user_message,
            current_model="test-model",
        )

        self.assertTrue(result["ok"], result.get("error"))
        events = read_jsonl(runtime.paths.session_events_path("main"))
        self.assertTrue(any(event.get("code") == "plan_record_autorepaired" for event in events))
        accepted_plan = [event for event in events if event.get("type") == "plan_record"][-1]["plan"]
        self.assertEqual(accepted_plan["strategy"], "dynamic_programming")
        unit_ids = [unit["unit_id"] for unit in accepted_plan["work_units"]]
        self.assertEqual(unit_ids, ["inspect-workspace", "implement-artifact", "verify-dynamic-programming-contract"])
        self.assertEqual(accepted_plan["verification_contract"], [
            "subproblem_state",
            "base_case_verifier",
            "recurrence_verifier",
            "evaluation_order_or_memoization",
            "sample_oracle",
        ])

    def test_planning_required_blocks_direct_write_and_allows_create_plan(self) -> None:
        runtime = self.runtime()
        user_message = "4x4 sliding puzzle を状態、合法手、ゴール、探索方針で解く実装を作ってください。"
        phase = runtime._current_phase(user_message=user_message, steps=[], recent_events=[])

        blocked = runtime._planner_action_blocked_event(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            turn_workspace=runtime.execution_root,
            tool_name="write_file",
            user_message=user_message,
            current_phase=phase,
            recent_events=[],
            steps=[],
        )
        allowed = runtime._planner_action_blocked_event(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=2,
            turn_workspace=runtime.execution_root,
            tool_name="create_plan",
            user_message=user_message,
            current_phase=phase,
            recent_events=[],
            steps=[],
        )

        self.assertIsNotNone(blocked)
        assert blocked is not None
        self.assertEqual(blocked["details"]["blocked_by"], "planner_controller")
        self.assertEqual(blocked["details"]["failure_type"], "planning_required_before_execution")
        self.assertEqual(blocked["details"]["allowed_next_actions"], ["create_plan"])
        self.assertIsNone(allowed)

    def test_planning_required_phase_is_not_overwritten_by_implementation_progress(self) -> None:
        runtime = self.runtime()
        user_message = "4x4 sliding puzzle を状態、合法手、ゴール、探索方針で解く実装とunittestを作ってください。"
        state = runtime._implementation_task_progress_state(
            user_message=user_message,
            steps=[],
            session_id="main",
            turn_workspace=runtime.execution_root,
        )

        phase = runtime._implementation_task_effective_phase(
            fallback_phase="PLANNING_REQUIRED",
            state=state,
        )

        self.assertEqual(phase, "PLANNING_REQUIRED")

    def test_accepted_create_plan_records_plan_and_task_plan_events(self) -> None:
        runtime = self.runtime()
        user_message = "4x4 sliding puzzle を状態、合法手、ゴール、探索方針で解く実装を作ってください。"
        plan = self.state_space_plan(runtime, user_message)

        result = runtime._handle_create_plan(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            tool_args={"plan": plan},
            turn_workspace=runtime.execution_root,
            user_message=user_message,
            current_model="test-model",
        )

        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["tasks"][0]["first_action"], {"tool": "list_files", "args": {"path": "."}})
        events = read_jsonl(runtime.paths.session_events_path("main"))
        event_types = [event.get("type") for event in events]
        self.assertIn("problem_profile", event_types)
        self.assertIn("planner_decision", event_types)
        self.assertIn("plan_record", event_types)
        self.assertIn("task_plan", event_types)
        self.assertIn("frame_opened", event_types)
        self.assertTrue(any(event.get("type") == "tool_result" and event.get("tool_name") == "list_files" for event in events))

    def test_create_plan_rejects_unchanged_revised_plan_after_timeout(self) -> None:
        runtime = self.runtime()
        user_message = "4x4 sliding puzzle を状態、合法手、ゴール、探索方針で解く実装とunittestを作ってください。"
        plan = self.state_space_plan(runtime, user_message)

        first = runtime._handle_create_plan(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            tool_args={"plan": plan},
            turn_workspace=runtime.execution_root,
            user_message=user_message,
            current_model="test-model",
        )
        self.assertTrue(first["ok"], first.get("error"))
        steps = [
            run_step(
                "python3 -m unittest discover -s tests",
                ok=False,
                stderr="Timed out after 60s",
                failure_type="command_timeout",
                returncode=None,
            )
        ]

        second = runtime._handle_create_plan(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=2,
            tool_args={"plan": plan},
            turn_workspace=runtime.execution_root,
            steps=steps,
            user_message=user_message,
            current_model="test-model",
        )

        self.assertFalse(second["ok"])
        event = second["event"]
        self.assertEqual(event["reason_code"], "plan_revision_no_change")
        self.assertIn("Do not resubmit the same PlanRecord", event["details"]["suggested_fix"])

    def test_create_plan_rejects_revision_that_only_changes_dependencies_after_timeout(self) -> None:
        runtime = self.runtime()
        user_message = "4x4 sliding puzzle を状態、合法手、ゴール、探索方針で解く実装とunittestを作ってください。"
        plan = self.state_space_plan(runtime, user_message)

        first = runtime._handle_create_plan(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            tool_args={"plan": plan},
            turn_workspace=runtime.execution_root,
            user_message=user_message,
            current_model="test-model",
        )
        self.assertTrue(first["ok"], first.get("error"))
        revised_plan = json.loads(json.dumps(plan))
        revised_plan["revision_count"] = 1
        revised_plan["work_units"][1]["depends_on"] = []
        revised_plan["work_units"][1]["should_open_child_frame"] = False
        steps = [
            run_step(
                "python3 -m unittest discover -s tests",
                ok=False,
                stderr="Timed out after 60s",
                failure_type="command_timeout",
                returncode=None,
            )
        ]

        second = runtime._handle_create_plan(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=2,
            tool_args={"plan": revised_plan},
            turn_workspace=runtime.execution_root,
            steps=steps,
            user_message=user_message,
            current_model="test-model",
        )

        self.assertFalse(second["ok"])
        self.assertEqual(second["event"]["reason_code"], "plan_revision_no_change")

    def test_revised_create_plan_from_child_frame_returns_to_root_before_decompose(self) -> None:
        runtime = self.runtime()
        user_message = "4x4 sliding puzzle を状態、合法手、ゴール、探索方針で解く実装とunittestを作ってください。"
        plan = self.state_space_plan(runtime, user_message)

        first = runtime._handle_create_plan(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            tool_args={"plan": plan},
            turn_workspace=runtime.execution_root,
            user_message=user_message,
            current_model="test-model",
        )
        self.assertTrue(first["ok"], first.get("error"))
        self.assertIsNotNone(runtime.frame_manager.current_frame())
        self.assertEqual(runtime.frame_manager.current_frame().depth, 1)

        revised_plan = json.loads(json.dumps(plan))
        revised_plan["revision_count"] = 1
        revised_plan["work_units"][0]["goal"] = "Narrow timed-out state-space tests to deterministic near-goal fixtures before rerunning validation."
        revised_plan["work_units"][0]["success_evidence"] = "Timed-out tests are replaced with deterministic near-goal replay checks."
        steps = [
            run_step(
                "python3 -m unittest discover -s tests",
                ok=False,
                stderr="Timed out after 60s",
                failure_type="command_timeout",
                returncode=None,
            )
        ]

        second = runtime._handle_create_plan(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=2,
            tool_args={"plan": revised_plan},
            turn_workspace=runtime.execution_root,
            steps=steps,
            user_message=user_message,
            current_model="test-model",
        )

        self.assertTrue(second["ok"], second.get("error"))
        events = read_jsonl(runtime.paths.session_events_path("main"))
        self.assertTrue(any(event.get("code") == "plan_revision_returned_to_root" for event in events))
        self.assertFalse(any(event.get("code") == "decompose_tasks_blocked" for event in events))

    def test_plan_record_obligations_are_visible_to_implementation_progress_prompt(self) -> None:
        runtime = self.runtime()
        user_message = "4x4 sliding puzzle を状態、合法手、ゴール、探索方針で解く実装とunittestを作ってください。"
        plan = self.state_space_plan(runtime, user_message)

        result = runtime._handle_create_plan(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            tool_args={"plan": plan},
            turn_workspace=runtime.execution_root,
            user_message=user_message,
            current_model="test-model",
        )
        self.assertTrue(result["ok"], result.get("error"))
        for index in range(400):
            runtime._append_session_event(
                "main",
                {
                    "type": "runtime_event",
                    "role": "system",
                    "content": f"stream chunk {index}",
                    "phase": "PLAN_EXECUTION",
                },
            )

        steps = [
            tool_step(
                "write_file",
                "puzzle_solver.py",
                content="def solve(start, goal):\n    return []\n",
            )
        ]
        state = runtime._implementation_task_progress_state(
            user_message=user_message,
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        prompt = runtime._implementation_task_progress_prompt(state)

        self.assertEqual(state["plan_strategy"], "state_space_search")
        self.assertIn("programmatic solver/search path", prompt)
        self.assertIn("manual input loop alone does not satisfy", prompt)
        self.assertIn("final_verifier/tests must replay", prompt)
        self.assertIn("legal-move validator", prompt)

    def test_plan_record_context_survives_large_event_stream_history(self) -> None:
        runtime = self.runtime()
        user_message = "4x4 sliding puzzle を状態、合法手、ゴール、探索方針で解く実装とunittestを作ってください。"
        plan = self.state_space_plan(runtime, user_message)

        result = runtime._handle_create_plan(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            tool_args={"plan": plan},
            turn_workspace=runtime.execution_root,
            user_message=user_message,
            current_model="test-model",
        )
        self.assertTrue(result["ok"], result.get("error"))
        for index in range(5100):
            runtime._append_session_event(
                "main",
                {
                    "type": "runtime_event",
                    "role": "system",
                    "event_name": "llm_stream_chunk",
                    "content": f"stream chunk {index}",
                    "phase": "PLAN_EXECUTION",
                },
            )

        state = runtime._implementation_task_progress_state(
            user_message=user_message,
            steps=[tool_step("write_file", "puzzle_solver.py", content="def solve(start, goal):\n    return []\n")],
            session_id="main",
            turn_workspace=runtime.execution_root,
        )

        self.assertEqual(state["plan_strategy"], "state_space_search")
        self.assertEqual(runtime._plan_record_execution_context()["plan_strategy"], "state_space_search")
        self.assertNotEqual(state["phase"], "not_applicable")

    def test_state_space_plan_blocks_manual_ui_only_implementation_before_tests(self) -> None:
        runtime = self.runtime()
        user_message = "4x4 sliding puzzle を状態、合法手、ゴール、探索方針で解く実装とunittestを作ってください。"
        plan = self.state_space_plan(runtime, user_message)

        result = runtime._handle_create_plan(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            tool_args={"plan": plan},
            turn_workspace=runtime.execution_root,
            user_message=user_message,
            current_model="test-model",
        )
        self.assertTrue(result["ok"], result.get("error"))

        manual_ui_source = (
            "class Puzzle:\n"
            "    def get_possible_moves(self):\n"
            "        return [1]\n"
            "    def make_move(self, move):\n"
            "        return True\n"
            "    def is_solved(self):\n"
            "        return False\n"
            "\n"
            "if __name__ == '__main__':\n"
            "    move = input('move: ')\n"
            "    print(move)\n"
        )
        steps = [tool_step("write_file", "puzzle.py", content=manual_ui_source)]
        state = runtime._implementation_task_progress_state(
            user_message=user_message,
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )

        self.assertEqual(state["phase"], "implementation_present_needs_semantic_review")
        issues = "\n".join(state["implementation_source_issues"])
        self.assertIn("state_space_search", issues)
        self.assertIn("programmatic solver/search callable", issues)
        self.assertIn("manual input loop alone", issues)

        self.assertTrue(
            runtime._plan_run_test_should_wait_for_progress(
                pending_task={"work_type": "run_test"},
                progress_state=state,
            )
        )
        ready_without_test_artifact = {**state, "phase": "unittest_not_run", "missing_requirements": ["unittest_run"]}
        self.assertTrue(
            runtime._plan_run_test_should_wait_for_progress(
                pending_task={"work_type": "run_test"},
                progress_state=ready_without_test_artifact,
            )
        )
        ready_state = {
            **state,
            "phase": "unittest_not_run",
            "missing_requirements": ["unittest_run"],
            "test_paths": ["tests/test_puzzle.py"],
        }
        self.assertFalse(
            runtime._plan_run_test_should_wait_for_progress(
                pending_task={"work_type": "run_test"},
                progress_state=ready_state,
            )
        )

    def test_state_space_plan_requires_tests_to_replay_solver_result(self) -> None:
        runtime = self.runtime()
        user_message = "状態、合法手、ゴール、探索方針で解く小さなパズル実装とunittestを作ってください。"
        plan = self.state_space_plan(runtime, user_message)
        result = runtime._handle_create_plan(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            tool_args={"plan": plan},
            turn_workspace=runtime.execution_root,
            user_message=user_message,
            current_model="test-model",
        )
        self.assertTrue(result["ok"], result.get("error"))

        implementation_source = (
            "from collections import deque\n\n"
            "def solve_state_space(start, goal, legal_moves):\n"
            "    if start == goal:\n"
            "        return []\n"
            "    queue = deque([(start, [])])\n"
            "    seen = {start}\n"
            "    while queue:\n"
            "        state, path = queue.popleft()\n"
            "        for action, next_state in legal_moves(state):\n"
            "            if next_state in seen:\n"
            "                continue\n"
            "            next_path = path + [action]\n"
            "            if next_state == goal:\n"
            "                return next_path\n"
            "            seen.add(next_state)\n"
            "            queue.append((next_state, next_path))\n"
            "    return None\n"
            "\n"
            "def replay(start, moves, legal_moves):\n"
            "    state = start\n"
            "    for move in moves:\n"
            "        state = legal_moves(state, move)\n"
            "    return state\n"
        )
        weak_test_source = (
            "import unittest\n"
            "from puzzle_solver import solve_state_space\n"
            "\n"
            "class TestPuzzleSolver(unittest.TestCase):\n"
            "    def test_solve_returns_path(self):\n"
            "        solution = solve_state_space((1, 2, 0), (1, 2, 0), lambda state: [])\n"
            "        self.assertIsNotNone(solution)\n"
            "        for move in solution:\n"
            "            pass\n"
        )

        artifact_issue = runtime._python_artifact_contract_issue(
            user_message=user_message,
            tool_name="write_file",
            tool_args={"path": "tests/test_puzzle_solver.py", "content": weak_test_source},
        )
        self.assertIsNotNone(artifact_issue)
        self.assertEqual(artifact_issue["reason_code"], "test_artifact_contract_incomplete")
        self.assertIn("state_space_search", artifact_issue["message"])
        self.assertIn("replay", artifact_issue["message"])

        steps = [
            tool_step("write_file", "puzzle_solver.py", content=implementation_source),
            tool_step("write_file", "tests/test_puzzle_solver.py", content=weak_test_source),
        ]
        state = runtime._implementation_task_progress_state(
            user_message=user_message,
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertEqual(state["phase"], "tests_present_needs_semantic_review")
        self.assertEqual(state["allowed_next_actions"], ["read_file tests/test_puzzle_solver.py once"])
        prompt = runtime._implementation_task_progress_prompt(state)
        self.assertIn("test artifact 未達", prompt)
        self.assertIn("goal到達をassert", prompt)
        block = runtime._implementation_task_phase_action_block(
            user_message=user_message,
            tool_name="read_file",
            tool_args={"path": "tests/test_puzzle_solver.py"},
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertIsNone(block)

    def test_state_space_implementation_requires_replay_verifier_not_only_legal_and_goal_helpers(self) -> None:
        runtime = self.runtime()
        user_message = "状態、合法手、ゴール、探索方針で解く小さなパズル実装とunittestを作ってください。"
        plan = self.state_space_plan(runtime, user_message)
        result = runtime._handle_create_plan(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            tool_args={"plan": plan},
            turn_workspace=runtime.execution_root,
            user_message=user_message,
            current_model="test-model",
        )
        self.assertTrue(result["ok"], result.get("error"))

        source_without_replay = (
            "def solve_state_space(start, goal):\n"
            "    return ['advance'] if start != goal else []\n\n"
            "def is_legal_move(state, action):\n"
            "    return action == 'advance'\n\n"
            "def make_move(state, action):\n"
            "    return state + 1\n\n"
            "def is_goal_state(state, goal):\n"
            "    return state == goal\n"
        )
        steps = [tool_step("write_file", "puzzle_solver.py", content=source_without_replay)]
        state = runtime._implementation_task_progress_state(
            user_message=user_message,
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )

        self.assertEqual(state["phase"], "implementation_present_needs_semantic_review")
        issues = "\n".join(state["implementation_source_issues"])
        self.assertIn("action列を独立にreplay/verifyするcallable", issues)
        self.assertIn("is_legal_move や is_goal_state だけではfinal_verifierになりません", issues)
        hints = state["implementation_source_repair_hints"]
        self.assertTrue(
            any(
                isinstance(item, dict) and item.get("suggested_action") == "add_state_space_replay_verifier"
                for item in hints
            ),
            hints,
        )
        suggested = "\n".join(str(item.get("suggested_new_text") or "") for item in hints if isinstance(item, dict))
        self.assertIn("def verify_solution", suggested)
        self.assertIn("is_legal_move", suggested)
        self.assertIn("make_move", suggested)
        self.assertIn("\n        if not is_legal_move(current_state, action):", suggested)
        self.assertNotIn("\n            if not is_legal_move(current_state, action):", suggested)
        prompt = runtime._implementation_task_progress_prompt(state)
        self.assertIn("state_space_search final_verifier修復", prompt)
        self.assertIn("legal check -> transition -> goal check", prompt)

    def test_state_space_replay_hint_does_not_treat_transition_as_legal_validator(self) -> None:
        runtime = self.runtime()
        user_message = "状態、合法手、ゴール、探索方針で解く小さなパズル実装とunittestを作ってください。"
        plan = self.state_space_plan(runtime, user_message)
        result = runtime._handle_create_plan(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            tool_args={"plan": plan},
            turn_workspace=runtime.execution_root,
            user_message=user_message,
            current_model="test-model",
        )
        self.assertTrue(result["ok"], result.get("error"))

        source_without_legal_validator = (
            "class Puzzle:\n"
            "    def __init__(self):\n"
            "        self.goal_state = 2\n\n"
            "    def solve(self, start):\n"
            "        return ['advance']\n\n"
            "    def make_move(self, state, action):\n"
            "        return state + 1\n\n"
            "    def is_solved(self):\n"
            "        return False\n"
        )
        steps = [tool_step("write_file", "puzzle_solver.py", content=source_without_legal_validator)]
        state = runtime._implementation_task_progress_state(
            user_message=user_message,
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )

        suggested = "\n".join(
            str(item.get("suggested_new_text") or "")
            for item in state["implementation_source_repair_hints"]
            if isinstance(item, dict)
        )
        self.assertIn("legal_move_validator", suggested)
        self.assertNotIn("not self.make_move", suggested)

    def test_state_space_replay_hint_handles_legal_moves_generator(self) -> None:
        runtime = self.runtime()
        user_message = "状態、合法手、ゴール、探索方針で解く小さなパズル実装とunittestを作ってください。"
        plan = self.state_space_plan(runtime, user_message)
        result = runtime._handle_create_plan(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            tool_args={"plan": plan},
            turn_workspace=runtime.execution_root,
            user_message=user_message,
            current_model="test-model",
        )
        self.assertTrue(result["ok"], result.get("error"))

        source_without_replay = (
            "def get_legal_moves(state):\n"
            "    return [('advance', state + 1)]\n\n"
            "def apply_move(state, action):\n"
            "    return action[1]\n\n"
            "def solve_state_space(start, goal):\n"
            "    return [('advance', goal)]\n"
        )
        steps = [tool_step("write_file", "puzzle_solver.py", content=source_without_replay)]
        state = runtime._implementation_task_progress_state(
            user_message=user_message,
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )

        suggested = "\n".join(
            str(item.get("suggested_new_text") or "")
            for item in state["implementation_source_repair_hints"]
            if isinstance(item, dict)
        )
        self.assertIn("action not in list(get_legal_moves(current_state))", suggested)
        self.assertIn("\n        if action not in list(get_legal_moves(current_state)):", suggested)
        self.assertNotIn("\n            if action not in list(get_legal_moves(current_state)):", suggested)
        self.assertNotIn("get_legal_moves(current_state, action)", suggested)
        self.assertIn("apply_move(current_state, action)", suggested)

    def test_state_space_repeated_unittest_failure_prompts_action_contract_triage(self) -> None:
        runtime = self.runtime()
        user_message = "状態、合法手、ゴール、探索方針で解く小さなパズル実装とunittestを作ってください。"
        plan = self.state_space_plan(runtime, user_message)
        result = runtime._handle_create_plan(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            tool_args={"plan": plan},
            turn_workspace=runtime.execution_root,
            user_message=user_message,
            current_model="test-model",
        )
        self.assertTrue(result["ok"], result.get("error"))

        implementation_source = (
            "def legal_moves(state):\n"
            "    return [('advance', state + 1)] if state < 2 else []\n\n"
            "def solve_state_space(start, goal):\n"
            "    return []\n\n"
            "def replay(start, actions):\n"
            "    state = start\n"
            "    for action in actions:\n"
            "        for legal_action, next_state in legal_moves(state):\n"
            "            if action == legal_action:\n"
            "                state = next_state\n"
            "                break\n"
            "        else:\n"
            "            return None\n"
            "    return state\n\n"
            "def verify_solution(start, goal, actions):\n"
            "    return replay(start, actions) == goal\n"
        )
        revised_implementation_source = implementation_source.replace(
            "return []",
            "return ['wait']",
            1,
        )
        test_source = (
            "import unittest\n"
            "from puzzle_solver import solve_state_space, verify_solution\n\n"
            "class TestPuzzleSolver(unittest.TestCase):\n"
            "    def test_solver_reaches_goal(self):\n"
            "        actions = solve_state_space(0, 2)\n"
            "        self.assertTrue(verify_solution(0, 2, actions))\n"
        )
        stderr = (
            "FAIL: test_solver_reaches_goal (test_puzzle_solver.TestPuzzleSolver.test_solver_reaches_goal)\n"
            f"  File \"{runtime.execution_root / 'tests' / 'test_puzzle_solver.py'}\", line 7, in test_solver_reaches_goal\n"
            "AssertionError: False is not true\n"
        )
        steps = [
            tool_step("write_file", "puzzle_solver.py", content=implementation_source),
            tool_step("write_file", "tests/test_puzzle_solver.py", content=test_source),
            run_step("python3 -m unittest discover -s tests", ok=False, stderr=stderr),
            tool_step("read_file", "tests/test_puzzle_solver.py", content=test_source),
            tool_step("read_file", "puzzle_solver.py", content=implementation_source),
            tool_step("write_file", "puzzle_solver.py", content=revised_implementation_source),
            run_step("python3 -m unittest discover -s tests", ok=False, stderr=stderr),
        ]

        state = runtime._implementation_task_progress_state(
            user_message=user_message,
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertTrue(state["repeated_unittest_failure_signature"])
        self.assertTrue(state["unittest_repair_hints"])

        prompt = runtime._implementation_task_progress_prompt(state)
        self.assertIn("unittest失敗triage", prompt)
        self.assertIn("solver/search戻り値", prompt)
        self.assertIn("action列", prompt)
        self.assertIn("legal_move_validator", prompt)

        signature = runtime._implementation_progress_event_signature(state)
        self.assertIn("unittest_repair_hints", signature)
        self.assertTrue(signature["repair_hints"])

    def test_state_space_test_contract_rejects_random_shuffle_solver_fixture(self) -> None:
        runtime = self.runtime()
        user_message = "状態、合法手、ゴール、探索方針で解く小さなパズル実装とunittestを作ってください。"
        plan = self.state_space_plan(runtime, user_message)
        result = runtime._handle_create_plan(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            tool_args={"plan": plan},
            turn_workspace=runtime.execution_root,
            user_message=user_message,
            current_model="test-model",
        )
        self.assertTrue(result["ok"], result.get("error"))

        implementation_source = (
            "class Puzzle:\n"
            "    def __init__(self):\n"
            "        self.goal_state = [1, 2, 3, 0]\n"
            "        self.current_state = list(self.goal_state)\n"
            "    def shuffle(self):\n"
            "        self.current_state = [1, 2, 0, 3]\n"
            "    def solve(self):\n"
            "        return ['right']\n"
            "    def verify_solution(self, start, goal, actions):\n"
            "        return bool(actions) and goal == self.goal_state\n"
        )
        test_source = (
            "import unittest\n"
            "from puzzle_solver import Puzzle\n\n"
            "class TestPuzzle(unittest.TestCase):\n"
            "    def test_solver_reaches_goal(self):\n"
            "        puzzle = Puzzle()\n"
            "        puzzle.shuffle()\n"
            "        actions = puzzle.solve()\n"
            "        self.assertTrue(puzzle.verify_solution(puzzle.current_state, puzzle.goal_state, actions))\n"
        )
        steps = [
            tool_step("write_file", "puzzle_solver.py", content=implementation_source),
            tool_step("write_file", "tests/test_puzzle_solver.py", content=test_source),
        ]

        state = runtime._implementation_task_progress_state(
            user_message=user_message,
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )

        self.assertEqual(state["phase"], "tests_present_needs_semantic_review")
        issues = "\n".join(state["test_source_issues"])
        self.assertIn("random/shuffle", issues)
        self.assertIn("near-goal", issues)
        prompt = runtime._implementation_task_progress_prompt(state)
        self.assertIn("決定的なnear-goal fixture", prompt)
        self.assertIn("near-goal", prompt)

    def test_state_space_source_contract_rejects_random_fixture_helper(self) -> None:
        runtime = self.runtime()
        user_message = "状態、合法手、ゴール、探索方針で解く小さなパズル実装とunittestを作ってください。"
        plan = self.state_space_plan(runtime, user_message)
        result = runtime._handle_create_plan(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            tool_args={"plan": plan},
            turn_workspace=runtime.execution_root,
            user_message=user_message,
            current_model="test-model",
        )
        self.assertTrue(result["ok"], result.get("error"))

        implementation_source = (
            "def solve_path(start, goal):\n"
            "    return ['R']\n\n"
            "def verify_solution(start, goal, actions):\n"
            "    return bool(actions)\n\n"
            "def create_near_goal_fixture(goal, moves):\n"
            "    import random\n"
            "    action = random.choice(['L', 'U'])\n"
            "    return (goal, action, moves)\n"
        )
        steps = [tool_step("write_file", "puzzle_solver.py", content=implementation_source)]

        state = runtime._implementation_task_progress_state(
            user_message=user_message,
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )

        self.assertEqual(state["phase"], "implementation_present_needs_semantic_review")
        issues = "\n".join(state["implementation_source_issues"])
        self.assertIn("fixture helperが random/shuffle", issues)
        self.assertIn("create_near_goal_fixture", issues)
        self.assertIn("決定的", issues)

    def test_dynamic_programming_source_contract_rejects_formula_without_base_or_recurrence(self) -> None:
        runtime = self.runtime()
        user_message = "動的計画で部分問題、基底ケース、漸化式を使うPython実装とunittestを作ってください。"
        plan = self.dynamic_programming_plan(runtime, user_message)
        result = runtime._handle_create_plan(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            tool_args={"plan": plan},
            turn_workspace=runtime.execution_root,
            user_message=user_message,
            current_model="test-model",
        )
        self.assertTrue(result["ok"], result.get("error"))

        implementation_source = (
            "def compute_score(value):\n"
            "    return value * 2\n"
        )
        steps = [tool_step("write_file", "dp_solver.py", content=implementation_source)]

        state = runtime._implementation_task_progress_state(
            user_message=user_message,
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )

        self.assertEqual(state["phase"], "implementation_present_needs_semantic_review")
        issues = "\n".join(state["implementation_source_issues"])
        self.assertIn("dynamic_programming", issues)
        self.assertIn("base_case_verifier", issues)
        self.assertIn("recurrence_verifier", issues)

    def test_dynamic_programming_source_contract_accepts_requested_api_name_with_dp_structure(self) -> None:
        runtime = self.runtime()
        user_message = (
            "Pythonで weighted interval scheduling を解く実装を作ってください。"
            "APIは weighted_interval_scheduling(intervals) としてください。動的計画法で検証してください。"
        )
        plan = self.dynamic_programming_plan(runtime, user_message)
        result = runtime._handle_create_plan(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            tool_args={"plan": plan},
            turn_workspace=runtime.execution_root,
            user_message=user_message,
            current_model="test-model",
        )
        self.assertTrue(result["ok"], result.get("error"))

        implementation_source = (
            "def weighted_interval_scheduling(intervals):\n"
            "    if not intervals:\n"
            "        return {'max_weight': 0, 'selected_jobs': []}\n"
            "    sorted_items = sorted(intervals, key=lambda item: item[1])\n"
            "    dp = [0] * len(sorted_items)\n"
            "    dp[0] = sorted_items[0][2]\n"
            "    for index in range(1, len(sorted_items)):\n"
            "        include_weight = sorted_items[index][2]\n"
            "        dp[index] = max(dp[index - 1], include_weight)\n"
            "    return {'max_weight': dp[-1], 'selected_jobs': []}\n"
        )

        issues = runtime._implementation_source_contract_issues(
            user_message=user_message,
            source=implementation_source,
        )

        self.assertNotIn("programmatic callableが見つかりません", "\n".join(issues))

    def test_dynamic_programming_test_contract_accepts_requested_api_name_calls(self) -> None:
        runtime = self.runtime()
        user_message = (
            "Pythonで weighted interval scheduling を解く実装とunittestを作ってください。"
            "APIは weighted_interval_scheduling(intervals) としてください。動的計画法で検証してください。"
        )
        plan = self.dynamic_programming_plan(runtime, user_message)
        result = runtime._handle_create_plan(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            tool_args={"plan": plan},
            turn_workspace=runtime.execution_root,
            user_message=user_message,
            current_model="test-model",
        )
        self.assertTrue(result["ok"], result.get("error"))
        test_source = (
            "import unittest\n"
            "from weighted_interval_scheduling import weighted_interval_scheduling\n\n"
            "def brute_force_weighted_interval_scheduling(intervals):\n"
            "    total = 0\n"
            "    for item in intervals:\n"
            "        total += item[2]\n"
            "    return total\n\n"
            "class TestDP(unittest.TestCase):\n"
            "    def test_base_case(self):\n"
            "        result = weighted_interval_scheduling([])\n"
            "        self.assertEqual(result['max_weight'], 0)\n\n"
            "    def test_recurrence_sample_oracle(self):\n"
            "        intervals = [(1, 2, 5, 'a'), (3, 4, 7, 'b')]\n"
            "        result = weighted_interval_scheduling(intervals)\n"
            "        self.assertEqual(result['max_weight'], brute_force_weighted_interval_scheduling(intervals))\n"
        )

        issues = runtime._test_source_contract_issues(
            user_message=user_message,
            test_sources=[("tests/test_weighted_interval_scheduling.py", test_source)],
        )

        self.assertNotIn("solver/compute callableを直接呼んでいません", "\n".join(issues))

    def test_dynamic_programming_source_contract_accepts_structural_callable_without_explicit_api(self) -> None:
        runtime = self.runtime()
        user_message = "weighted interval scheduling を解く Python プログラムを作成して実行して表示して。"
        plan = self.dynamic_programming_plan(runtime, user_message)
        result = runtime._handle_create_plan(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            tool_args={"plan": plan},
            turn_workspace=runtime.execution_root,
            user_message=user_message,
            current_model="test-model",
        )
        self.assertTrue(result["ok"], result.get("error"))

        implementation_source = (
            "def weighted_interval_scheduling(intervals):\n"
            "    if not intervals:\n"
            "        return 0, []\n"
            "    intervals = sorted(intervals, key=lambda item: item[1])\n"
            "    dp = [0] * len(intervals)\n"
            "    dp[0] = intervals[0][2]\n"
            "    for index in range(1, len(intervals)):\n"
            "        include_weight = intervals[index][2]\n"
            "        dp[index] = max(dp[index - 1], include_weight)\n"
            "    return dp[-1], []\n"
        )

        issues = runtime._implementation_source_contract_issues(
            user_message=user_message,
            source=implementation_source,
        )

        self.assertNotIn("programmatic callableが見つかりません", "\n".join(issues))

    def test_dynamic_programming_test_contract_accepts_imported_domain_callable_without_solver_name(self) -> None:
        runtime = self.runtime()
        user_message = "weighted interval scheduling を解く Python プログラムを作成して実行して表示して。"
        plan = self.dynamic_programming_plan(runtime, user_message)
        result = runtime._handle_create_plan(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            tool_args={"plan": plan},
            turn_workspace=runtime.execution_root,
            user_message=user_message,
            current_model="test-model",
        )
        self.assertTrue(result["ok"], result.get("error"))
        test_source = (
            "import unittest\n"
            "from weighted_interval_scheduling import weighted_interval_scheduling\n\n"
            "def reference_weighted_interval_scheduling(intervals):\n"
            "    total = 0\n"
            "    for item in intervals:\n"
            "        total += item[2]\n"
            "    return total\n\n"
            "class TestWeightedIntervalScheduling(unittest.TestCase):\n"
            "    def test_empty_intervals_base_case(self):\n"
            "        result = weighted_interval_scheduling([])\n"
            "        self.assertEqual(result, (0, []))\n\n"
            "    def test_sample_oracle_two_non_overlapping_intervals(self):\n"
            "        intervals = [(1, 2, 5), (3, 4, 10)]\n"
            "        result = weighted_interval_scheduling(intervals)\n"
            "        self.assertEqual(result[0], reference_weighted_interval_scheduling(intervals))\n"
        )

        issues = runtime._test_source_contract_issues(
            user_message=user_message,
            test_sources=[("tests/test_weighted_interval_scheduling.py", test_source)],
        )

        self.assertNotIn("solver/compute callableを直接呼んでいません", "\n".join(issues))
        self.assertNotIn("base case と recurrence/sample oracle", "\n".join(issues))

    def test_dynamic_programming_test_contract_treats_oracle_call_as_sample_evidence(self) -> None:
        runtime = self.runtime()
        user_message = "weighted interval scheduling を解く Python プログラムを作成して実行して表示して。"
        plan = self.dynamic_programming_plan(runtime, user_message)
        result = runtime._handle_create_plan(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            tool_args={"plan": plan},
            turn_workspace=runtime.execution_root,
            user_message=user_message,
            current_model="test-model",
        )
        self.assertTrue(result["ok"], result.get("error"))
        test_source = (
            "import unittest\n"
            "from weighted_interval_scheduling import weighted_interval_scheduling\n\n"
            "def brute_force_weighted_interval_scheduling(intervals):\n"
            "    best = 0\n"
            "    for mask in range(1 << len(intervals)):\n"
            "        total = 0\n"
            "        for index, item in enumerate(intervals):\n"
            "            if mask & (1 << index):\n"
            "                total += item[2]\n"
            "        best = max(best, total)\n"
            "    return best\n\n"
            "class TestWeightedIntervalScheduling(unittest.TestCase):\n"
            "    def test_base_case(self):\n"
            "        result = weighted_interval_scheduling([])\n"
            "        self.assertEqual(result[0], 0)\n\n"
            "    def test_small_case(self):\n"
            "        intervals = [(1, 2, 5), (3, 4, 7)]\n"
            "        result = weighted_interval_scheduling(intervals)\n"
            "        expected = brute_force_weighted_interval_scheduling(intervals)\n"
            "        self.assertEqual(result[0], expected)\n"
        )

        issues = runtime._test_source_contract_issues(
            user_message=user_message,
            test_sources=[("tests/test_weighted_interval_scheduling.py", test_source)],
        )

        self.assertNotIn("base case と recurrence/sample oracle", "\n".join(issues))
        self.assertNotIn("独立reference/brute-force oracle", "\n".join(issues))

    def test_dynamic_programming_test_contract_rejects_hardcoded_expected_without_independent_oracle(self) -> None:
        runtime = self.runtime()
        user_message = "動的計画法で最適化問題を解くPython実装とunittestを作ってください。"
        plan = self.dynamic_programming_plan(runtime, user_message)
        result = runtime._handle_create_plan(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            tool_args={"plan": plan},
            turn_workspace=runtime.execution_root,
            user_message=user_message,
            current_model="test-model",
        )
        self.assertTrue(result["ok"], result.get("error"))
        test_source = (
            "import unittest\n"
            "from optimizer import compute_optimum\n\n"
            "class TestDP(unittest.TestCase):\n"
            "    def test_base_case(self):\n"
            "        self.assertEqual(compute_optimum([]), 0)\n\n"
            "    def test_sample_oracle_case(self):\n"
            "        values = [5, 6, 4]\n"
            "        self.assertEqual(compute_optimum(values), 10)\n"
        )

        issues = runtime._test_source_contract_issues(
            user_message=user_message,
            test_sources=[("tests/test_optimizer.py", test_source)],
        )

        self.assertIn("独立reference/brute-force oracle", "\n".join(issues))

    def test_dynamic_programming_oracle_block_gives_matching_repair_guidance(self) -> None:
        runtime = self.runtime()
        user_message = "動的計画法で最適化問題を解くPython実装とunittestを作ってください。"
        plan = self.dynamic_programming_plan(runtime, user_message)
        result = runtime._handle_create_plan(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            tool_args={"plan": plan},
            turn_workspace=runtime.execution_root,
            user_message=user_message,
            current_model="test-model",
        )
        self.assertTrue(result["ok"], result.get("error"))
        test_source = (
            "import unittest\n"
            "from optimizer import compute_optimum\n\n"
            "class TestDP(unittest.TestCase):\n"
            "    def test_base_case(self):\n"
            "        self.assertEqual(compute_optimum([]), 0)\n\n"
            "    def test_sample_oracle_case(self):\n"
            "        values = [5, 6, 4]\n"
            "        self.assertEqual(compute_optimum(values), 10)\n"
        )

        issue = runtime._python_artifact_contract_issue(
            user_message=user_message,
            tool_name="write_file",
            tool_args={"path": "tests/test_optimizer.py", "content": test_source},
        )

        self.assertIsNotNone(issue)
        assert issue is not None
        self.assertIn("独立reference/brute-force oracle", issue["message"])
        self.assertIn("brute_force/reference/oracle helper", issue["suggested_fix"])
        self.assertIn("independent brute_force/reference/oracle helper", issue["next_required_action"])

        runtime._append_session_event(
            runtime.root,
            "main",
            {
                "type": "system_note",
                "role": "system",
                "content": f"write_file がブロックされました: {issue['message']}",
                "code": "edit_blocked",
                "reason_code": issue["reason_code"],
                "details": {
                    "blocked_tool": "write_file",
                    "path": "tests/test_optimizer.py",
                    "suggested_fix": issue["suggested_fix"],
                    "next_required_action": issue["next_required_action"],
                },
            },
        )
        state = runtime._implementation_task_progress_state(user_message=user_message, steps=[], session_id="main")
        prompt = runtime._implementation_task_progress_prompt(state)

        self.assertIn("brute_force/reference/oracle helper", prompt)
        self.assertIn("hardcoded expected", prompt)
        self.assertNotIn("None と not None", prompt)

    def test_unittest_failure_signature_ignores_stdout_demo_noise(self) -> None:
        runtime = self.runtime()
        stderr = (
            "FF\n"
            "======================================================================\n"
            "FAIL: test_sample (test_solver.TestSolver.test_sample)\n"
            "----------------------------------------------------------------------\n"
            "Traceback (most recent call last):\n"
            "  File \"tests/test_solver.py\", line 10, in test_sample\n"
            "    self.assertEqual(result[0], 13)\n"
            "AssertionError: 18 != 13\n"
        )
        first = run_step(
            "python3 -m unittest discover -s tests",
            ok=False,
            stderr=stderr,
            stdout="Selected intervals: [(1, 4, 5)]\nMaximum weight: 15\n",
        )["tool_result"]
        second = run_step(
            "python3 -m unittest discover -s tests",
            ok=False,
            stderr=stderr,
            stdout="",
        )["tool_result"]

        self.assertEqual(
            runtime._unittest_failure_signature(first),
            runtime._unittest_failure_signature(second),
        )

    def test_dynamic_programming_repeated_self_test_assertion_prioritizes_test_fixture(self) -> None:
        runtime = self.runtime()
        user_message = "weighted interval scheduling を解く Python プログラムを作成し、サンプルデータで実行して、選ばれた区間と最大重みを表示して"
        plan = self.dynamic_programming_plan(runtime, user_message)
        result = runtime._handle_create_plan(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            tool_args={"plan": plan},
            turn_workspace=runtime.execution_root,
            user_message=user_message,
            current_model="test-model",
        )
        self.assertTrue(result["ok"], result.get("error"))
        implementation_source = (
            "def weighted_interval_scheduling(intervals):\n"
            "    if not intervals:\n"
            "        return 0, []\n"
            "    intervals = sorted(intervals, key=lambda item: item[1])\n"
            "    dp = [0] * len(intervals)\n"
            "    dp[0] = intervals[0][2]\n"
            "    for i in range(1, len(intervals)):\n"
            "        include_weight = intervals[i][2]\n"
            "        for j in range(i - 1, -1, -1):\n"
            "            if intervals[j][1] <= intervals[i][0]:\n"
            "                include_weight += dp[j]\n"
            "                break\n"
            "        dp[i] = max(dp[i - 1], include_weight)\n"
            "    return dp[-1], []\n"
        )
        changed_implementation_source = implementation_source + "\n"
        test_source = (
            "import unittest\n"
            "from weighted_interval_scheduling import weighted_interval_scheduling\n\n"
            "class TestWeightedIntervalScheduling(unittest.TestCase):\n"
            "    def test_base_case_empty(self):\n"
            "        self.assertEqual(weighted_interval_scheduling([])[0], 0)\n\n"
            "    def test_sample_oracle_case(self):\n"
            "        intervals = [(1, 2, 5), (3, 5, 6), (6, 7, 3), (8, 9, 4)]\n"
            "        result = weighted_interval_scheduling(intervals)\n"
            "        self.assertEqual(result[0], 13)\n"
        )
        stderr = (
            "F\n"
            "======================================================================\n"
            "FAIL: test_sample_oracle_case (test_weighted_interval_scheduling.TestWeightedIntervalScheduling.test_sample_oracle_case)\n"
            "----------------------------------------------------------------------\n"
            "Traceback (most recent call last):\n"
            "  File \"tests/test_weighted_interval_scheduling.py\", line 11, in test_sample_oracle_case\n"
            "    self.assertEqual(result[0], 13)\n"
            "AssertionError: 18 != 13\n"
        )
        steps = [
            tool_step("write_file", "weighted_interval_scheduling.py", content=implementation_source),
            tool_step("write_file", "tests/test_weighted_interval_scheduling.py", content=test_source),
            run_step("python3 -m unittest discover -s tests", ok=False, stderr=stderr, stdout="demo output\n"),
            tool_step("read_file", "tests/test_weighted_interval_scheduling.py", content=test_source),
            tool_step("read_file", "weighted_interval_scheduling.py", content=implementation_source),
            tool_step("write_file", "weighted_interval_scheduling.py", content=changed_implementation_source),
            run_step("python3 -m unittest discover -s tests", ok=False, stderr=stderr, stdout=""),
        ]

        state = runtime._implementation_task_progress_state(
            user_message=user_message,
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )

        self.assertTrue(state["repeated_unittest_failure_signature"])
        self.assertTrue(state["algorithmic_test_fixture_value_suspected"])
        self.assertIn("weighted_interval_scheduling.py", state["test_fixture_value_impl_blocked_paths"])
        self.assertEqual(
            state["allowed_next_actions"],
            [
                "replace_text tests/test_weighted_interval_scheduling.py with a small unique old_text",
                "write_file tests/test_weighted_interval_scheduling.py",
            ],
        )

        after_test_read = runtime._implementation_task_progress_state(
            user_message=user_message,
            steps=[*steps, tool_step("read_file", "tests/test_weighted_interval_scheduling.py", content=test_source)],
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertEqual(
            after_test_read["allowed_next_actions"],
            [
                "replace_text tests/test_weighted_interval_scheduling.py with a small unique old_text",
                "write_file tests/test_weighted_interval_scheduling.py",
            ],
        )
        self.assertTrue(any("自己生成test fixture" in hint for hint in after_test_read["unittest_repair_hints"]))

        block = runtime._implementation_task_phase_action_block(
            user_message=user_message,
            steps=[*steps, tool_step("read_file", "tests/test_weighted_interval_scheduling.py", content=test_source)],
            session_id="main",
            turn_workspace=runtime.execution_root,
            tool_name="write_file",
            tool_args={"path": "weighted_interval_scheduling.py", "content": changed_implementation_source},
        )
        self.assertIsNotNone(block)
        self.assertEqual(block["reason_code"], "implementation_task_failed_unittest_prioritizes_test_fixture_oracle")

    def test_dynamic_programming_noop_impl_edit_prioritizes_test_fixture_oracle(self) -> None:
        runtime = self.runtime()
        user_message = "weighted interval scheduling を解く Python プログラムを作成し、サンプルデータで実行して、選ばれた区間と最大重みを表示して"
        plan = self.dynamic_programming_plan(runtime, user_message)
        result = runtime._handle_create_plan(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            tool_args={"plan": plan},
            turn_workspace=runtime.execution_root,
            user_message=user_message,
            current_model="test-model",
        )
        self.assertTrue(result["ok"], result.get("error"))
        implementation_source = (
            "def weighted_interval_scheduling(intervals):\n"
            "    if not intervals:\n"
            "        return 0, []\n"
            "    intervals = sorted(intervals, key=lambda item: item[1])\n"
            "    dp = [0] * len(intervals)\n"
            "    dp[0] = intervals[0][2]\n"
            "    for i in range(1, len(intervals)):\n"
            "        include_weight = intervals[i][2]\n"
            "        for j in range(i - 1, -1, -1):\n"
            "            if intervals[j][1] <= intervals[i][0]:\n"
            "                include_weight += dp[j]\n"
            "                break\n"
            "        dp[i] = max(dp[i - 1], include_weight)\n"
            "    return dp[-1], []\n"
        )
        api_aligned_source = implementation_source.replace("return 0, []", "return [], 0").replace(
            "return dp[-1], []",
            "return [], dp[-1]",
        )
        test_source = (
            "import unittest\n"
            "from weighted_interval_scheduling import weighted_interval_scheduling\n\n"
            "class TestWeightedIntervalScheduling(unittest.TestCase):\n"
            "    def test_base_case_empty(self):\n"
            "        self.assertEqual(weighted_interval_scheduling([]), ([], 0))\n\n"
            "    def test_recurrence_case(self):\n"
            "        intervals = [(1, 2, 5), (3, 5, 6), (6, 7, 3), (8, 9, 4)]\n"
            "        result = weighted_interval_scheduling(intervals)\n"
            "        self.assertEqual(result[1], 13)\n"
        )
        initial_stderr = (
            "FF\n"
            "======================================================================\n"
            "FAIL: test_base_case_empty (test_weighted_interval_scheduling.TestWeightedIntervalScheduling.test_base_case_empty)\n"
            "----------------------------------------------------------------------\n"
            "Traceback (most recent call last):\n"
            "  File \"tests/test_weighted_interval_scheduling.py\", line 6, in test_base_case_empty\n"
            "    self.assertEqual(weighted_interval_scheduling([]), ([], 0))\n"
            "AssertionError: Tuples differ: (0, []) != ([], 0)\n"
            "======================================================================\n"
            "FAIL: test_recurrence_case (test_weighted_interval_scheduling.TestWeightedIntervalScheduling.test_recurrence_case)\n"
            "----------------------------------------------------------------------\n"
            "Traceback (most recent call last):\n"
            "  File \"tests/test_weighted_interval_scheduling.py\", line 11, in test_recurrence_case\n"
            "    self.assertEqual(result[1], 13)\n"
            "AssertionError: [] != 13\n"
        )
        latest_stderr = (
            "F\n"
            "======================================================================\n"
            "FAIL: test_recurrence_case (test_weighted_interval_scheduling.TestWeightedIntervalScheduling.test_recurrence_case)\n"
            "----------------------------------------------------------------------\n"
            "Traceback (most recent call last):\n"
            "  File \"tests/test_weighted_interval_scheduling.py\", line 11, in test_recurrence_case\n"
            "    self.assertEqual(result[1], 13)\n"
            "AssertionError: 18 != 13\n"
        )
        steps = [
            tool_step("write_file", "weighted_interval_scheduling.py", content=implementation_source),
            tool_step("write_file", "tests/test_weighted_interval_scheduling.py", content=test_source),
            run_step("python3 -m unittest discover -s tests", ok=False, stderr=initial_stderr),
            tool_step("read_file", "tests/test_weighted_interval_scheduling.py", content=test_source),
            tool_step("read_file", "weighted_interval_scheduling.py", content=implementation_source),
            tool_step("write_file", "weighted_interval_scheduling.py", content=api_aligned_source),
            run_step("python3 -m unittest discover -s tests", ok=False, stderr=latest_stderr),
            tool_step("read_file", "weighted_interval_scheduling.py", content=api_aligned_source),
            tool_step("read_file", "tests/test_weighted_interval_scheduling.py", content=test_source),
            tool_step(
                "write_file",
                "weighted_interval_scheduling.py",
                content=api_aligned_source,
                ok=False,
                failure_type="no_op_edit",
            ),
        ]

        state = runtime._implementation_task_progress_state(
            user_message=user_message,
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )

        self.assertFalse(state["repeated_unittest_failure_signature"])
        self.assertTrue(state["implementation_noop_after_failed_unittest"])
        self.assertTrue(state["algorithmic_test_fixture_value_suspected"])
        self.assertEqual(
            state["allowed_next_actions"],
            [
                "replace_text tests/test_weighted_interval_scheduling.py with a small unique old_text",
                "write_file tests/test_weighted_interval_scheduling.py",
            ],
        )

        blocked = runtime._implementation_task_phase_action_block(
            user_message=user_message,
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
            tool_name="write_file",
            tool_args={"path": "weighted_interval_scheduling.py", "content": api_aligned_source},
        )
        self.assertIsNotNone(blocked)
        self.assertEqual(blocked["reason_code"], "implementation_task_failed_unittest_prioritizes_test_fixture_oracle")

    def test_dynamic_programming_broad_impl_replace_block_prioritizes_test_fixture_oracle(self) -> None:
        runtime = self.runtime()
        user_message = "weighted interval scheduling を解く Python プログラムを作成し、サンプルデータで実行して、選ばれた区間と最大重みを表示して"
        plan = self.dynamic_programming_plan(runtime, user_message)
        result = runtime._handle_create_plan(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            tool_args={"plan": plan},
            turn_workspace=runtime.execution_root,
            user_message=user_message,
            current_model="test-model",
        )
        self.assertTrue(result["ok"], result.get("error"))
        implementation_source = (
            "def weighted_interval_scheduling(intervals):\n"
            "    if not intervals:\n"
            "        return 0, []\n"
            "    intervals = sorted(intervals, key=lambda item: item[1])\n"
            "    dp = [0] * len(intervals)\n"
            "    dp[0] = intervals[0][2]\n"
            "    for i in range(1, len(intervals)):\n"
            "        include_weight = intervals[i][2]\n"
            "        for j in range(i - 1, -1, -1):\n"
            "            if intervals[j][1] <= intervals[i][0]:\n"
            "                include_weight += dp[j]\n"
            "                break\n"
            "        dp[i] = max(dp[i - 1], include_weight)\n"
            "    return dp[-1], []\n"
        )
        test_source = (
            "import unittest\n"
            "from weighted_interval_scheduling import weighted_interval_scheduling\n\n"
            "class TestWeightedIntervalScheduling(unittest.TestCase):\n"
            "    def test_recurrence_case(self):\n"
            "        intervals = [(1, 2, 5), (3, 5, 6), (6, 7, 5)]\n"
            "        result = weighted_interval_scheduling(intervals)\n"
            "        self.assertEqual(result[0], 10)\n"
        )
        stderr = (
            "F\n"
            "======================================================================\n"
            "FAIL: test_recurrence_case (test_weighted_interval_scheduling.TestWeightedIntervalScheduling.test_recurrence_case)\n"
            "----------------------------------------------------------------------\n"
            "Traceback (most recent call last):\n"
            "  File \"tests/test_weighted_interval_scheduling.py\", line 8, in test_recurrence_case\n"
            "    self.assertEqual(result[0], 10)\n"
            "AssertionError: 16 != 10\n"
        )
        steps = [
            tool_step("write_file", "weighted_interval_scheduling.py", content=implementation_source),
            tool_step("write_file", "tests/test_weighted_interval_scheduling.py", content=test_source),
            run_step("python3 -m unittest discover -s tests", ok=False, stderr=stderr),
            tool_step("read_file", "tests/test_weighted_interval_scheduling.py", content=test_source),
            tool_step("read_file", "weighted_interval_scheduling.py", content=implementation_source),
        ]
        runtime._append_session_event(
            "main",
            {
                "type": "system_note",
                "role": "system",
                "code": "implementation_task_progress_blocked",
                "reason_code": "implementation_task_failed_unittest_blocks_broad_replace_text",
                "details": {
                    "reason_code": "implementation_task_failed_unittest_blocks_broad_replace_text",
                    "phase": "unittest_failed_needs_fix",
                    "path": "weighted_interval_scheduling.py",
                },
            },
        )

        state = runtime._implementation_task_progress_state(
            user_message=user_message,
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )

        self.assertTrue(state["implementation_replace_blocked_after_failed_unittest"])
        self.assertTrue(state["algorithmic_test_fixture_value_suspected"])
        self.assertNotIn("write_file weighted_interval_scheduling.py", state["allowed_next_actions"])
        self.assertEqual(
            state["allowed_next_actions"],
            [
                "replace_text tests/test_weighted_interval_scheduling.py with a small unique old_text",
                "write_file tests/test_weighted_interval_scheduling.py",
            ],
        )

        blocked = runtime._implementation_task_phase_action_block(
            user_message=user_message,
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
            tool_name="write_file",
            tool_args={"path": "weighted_interval_scheduling.py", "content": implementation_source},
        )
        self.assertIsNotNone(blocked)
        self.assertEqual(blocked["reason_code"], "implementation_task_failed_unittest_prioritizes_test_fixture_oracle")

    def test_dynamic_programming_independent_oracle_failure_allows_implementation_repair(self) -> None:
        runtime = self.runtime()
        user_message = "weighted interval scheduling を解く Python プログラムを作成し、サンプルデータで実行して、選ばれた区間と最大重みを表示して"
        plan = self.dynamic_programming_plan(runtime, user_message)
        result = runtime._handle_create_plan(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            tool_args={"plan": plan},
            turn_workspace=runtime.execution_root,
            user_message=user_message,
            current_model="test-model",
        )
        self.assertTrue(result["ok"], result.get("error"))
        implementation_source = (
            "def weighted_interval_scheduling(intervals):\n"
            "    if not intervals:\n"
            "        return 0, []\n"
            "    intervals = sorted(intervals, key=lambda item: item[1])\n"
            "    dp = [0] * len(intervals)\n"
            "    dp[0] = intervals[0][2]\n"
            "    for index in range(1, len(intervals)):\n"
            "        include_weight = intervals[index][2]\n"
            "        dp[index] = max(dp[index - 1], include_weight)\n"
            "    return dp[-1], list(intervals)\n"
        )
        repaired_source = implementation_source.replace("return dp[-1], list(intervals)", "return dp[-1], [intervals[-1]]")
        test_source = (
            "import unittest\n"
            "from weighted_interval_scheduling import weighted_interval_scheduling\n\n"
            "def brute_force_weighted_interval_scheduling(intervals):\n"
            "    best_weight = 0\n"
            "    best_items = []\n"
            "    for item in intervals:\n"
            "        if item[2] > best_weight:\n"
            "            best_weight = item[2]\n"
            "            best_items = [item]\n"
            "    return best_weight, best_items\n\n"
            "class TestWeightedIntervalScheduling(unittest.TestCase):\n"
            "    def test_base_case_empty(self):\n"
            "        self.assertEqual(weighted_interval_scheduling([]), (0, []))\n\n"
            "    def test_sample_oracle_case(self):\n"
            "        intervals = [(1, 3, 5), (2, 5, 6)]\n"
            "        actual_weight, actual_items = weighted_interval_scheduling(intervals)\n"
            "        expected_weight, expected_items = brute_force_weighted_interval_scheduling(intervals)\n"
            "        self.assertEqual(actual_weight, expected_weight)\n"
            "        self.assertEqual(len(actual_items), len(expected_items))\n"
        )
        stderr = (
            "F\n"
            "======================================================================\n"
            "FAIL: test_sample_oracle_case (test_weighted_interval_scheduling.TestWeightedIntervalScheduling.test_sample_oracle_case)\n"
            "----------------------------------------------------------------------\n"
            "Traceback (most recent call last):\n"
            "  File \"tests/test_weighted_interval_scheduling.py\", line 20, in test_sample_oracle_case\n"
            "    self.assertEqual(len(actual_items), len(expected_items))\n"
            "AssertionError: 2 != 1\n"
        )
        steps = [
            tool_step("write_file", "weighted_interval_scheduling.py", content=implementation_source),
            tool_step("write_file", "tests/test_weighted_interval_scheduling.py", content=test_source),
            run_step("python3 -m unittest discover -s tests", ok=False, stderr=stderr),
            tool_step("read_file", "tests/test_weighted_interval_scheduling.py", content=test_source),
            tool_step("read_file", "weighted_interval_scheduling.py", content=implementation_source),
        ]
        runtime._append_session_event(
            "main",
            {
                "type": "system_note",
                "role": "system",
                "code": "implementation_task_progress_blocked",
                "reason_code": "implementation_task_failed_unittest_blocks_broad_replace_text",
                "details": {
                    "reason_code": "implementation_task_failed_unittest_blocks_broad_replace_text",
                    "phase": "unittest_failed_needs_fix",
                    "path": "weighted_interval_scheduling.py",
                },
            },
        )

        state = runtime._implementation_task_progress_state(
            user_message=user_message,
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )

        self.assertTrue(state["algorithmic_independent_oracle_seen"])
        self.assertFalse(state["algorithmic_test_fixture_value_suspected"])
        self.assertIn("write_file weighted_interval_scheduling.py", state["allowed_next_actions"])

        allowed = runtime._implementation_task_phase_action_block(
            user_message=user_message,
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
            tool_name="write_file",
            tool_args={"path": "weighted_interval_scheduling.py", "content": repaired_source},
        )
        self.assertIsNone(allowed)

    def test_constraint_satisfaction_tests_require_validator_and_negative_case(self) -> None:
        runtime = self.runtime()
        user_message = "制約、変数、ドメイン、割当を使うPython実装とunittestを作ってください。"
        plan = self.constraint_satisfaction_plan(runtime, user_message)
        result = runtime._handle_create_plan(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            tool_args={"plan": plan},
            turn_workspace=runtime.execution_root,
            user_message=user_message,
            current_model="test-model",
        )
        self.assertTrue(result["ok"], result.get("error"))

        implementation_source = (
            "def check_constraints(variables, domains, constraints, assignment):\n"
            "    return all(constraint(assignment) for constraint in constraints)\n\n"
            "def validate_solution(variables, domains, constraints, assignment):\n"
            "    return set(variables) <= set(assignment) and check_constraints(variables, domains, constraints, assignment)\n\n"
            "def solve_assignment(variables, domains, constraints):\n"
            "    return {variable: domains[variable][0] for variable in variables}\n"
        )
        test_source = (
            "import unittest\n"
            "from csp_solver import solve_assignment\n\n"
            "class TestConstraintSolver(unittest.TestCase):\n"
            "    def test_solver_returns_assignment(self):\n"
            "        result = solve_assignment(['x'], {'x': [1]}, [])\n"
            "        self.assertIsNotNone(result)\n"
        )
        steps = [
            tool_step("write_file", "csp_solver.py", content=implementation_source),
            tool_step("write_file", "tests/test_csp_solver.py", content=test_source),
        ]

        state = runtime._implementation_task_progress_state(
            user_message=user_message,
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )

        self.assertEqual(state["phase"], "tests_present_needs_semantic_review")
        issues = "\n".join(state["test_source_issues"])
        self.assertIn("constraint_satisfaction", issues)
        self.assertIn("solution validator", issues)
        self.assertIn("invalid/negative", issues)

    def test_state_space_test_contract_rejects_handwritten_long_solution_fixture(self) -> None:
        runtime = self.runtime()
        user_message = "状態、合法手、ゴール、探索方針で解く小さなパズル実装とunittestを作ってください。"
        plan = self.state_space_plan(runtime, user_message)
        result = runtime._handle_create_plan(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            tool_args={"plan": plan},
            turn_workspace=runtime.execution_root,
            user_message=user_message,
            current_model="test-model",
        )
        self.assertTrue(result["ok"], result.get("error"))

        implementation_source = (
            "def solve_path(start, goal):\n"
            "    return ['right']\n\n"
            "def verify_solution(start, goal, actions):\n"
            "    return actions is not None and goal is not None\n"
        )
        test_source = (
            "import unittest\n"
            "from puzzle_solver import solve_path, verify_solution\n\n"
            "class TestPuzzleSolver(unittest.TestCase):\n"
            "    def test_solver_reaches_goal(self):\n"
            "        solution = solve_path('start', 'goal')\n"
            "        actions = ['left', 'up', 'right', 'down', 'left']\n"
            "        self.assertIsNotNone(solution)\n"
            "        self.assertTrue(verify_solution('start', 'goal', actions))\n"
        )
        steps = [
            tool_step("write_file", "puzzle_solver.py", content=implementation_source),
            tool_step("write_file", "tests/test_puzzle_solver.py", content=test_source),
        ]

        state = runtime._implementation_task_progress_state(
            user_message=user_message,
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )

        self.assertEqual(state["phase"], "tests_present_needs_semantic_review")
        issues = "\n".join(state["test_source_issues"])
        self.assertIn("長い手書きaction/path/solution fixture", issues)
        self.assertIn("solver/searchの戻り値", issues)
        self.assertIn("短い合法遷移", issues)

    def test_state_space_test_contract_rejects_literal_near_goal_fixture_not_derived(self) -> None:
        runtime = self.runtime()
        user_message = "状態、合法手、ゴール、探索方針で解く小さなパズル実装とunittestを作ってください。"
        plan = self.state_space_plan(runtime, user_message)
        result = runtime._handle_create_plan(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            tool_args={"plan": plan},
            turn_workspace=runtime.execution_root,
            user_message=user_message,
            current_model="test-model",
        )
        self.assertTrue(result["ok"], result.get("error"))

        implementation_source = (
            "GOAL = [1, 2, 0]\n\n"
            "def solve_path(start, goal):\n"
            "    return ['right']\n\n"
            "def verify_solution(start, goal, actions):\n"
            "    return actions is not None and start != goal\n"
        )
        test_source = (
            "import unittest\n"
            "from puzzle_solver import GOAL, solve_path, verify_solution\n\n"
            "class TestPuzzleSolver(unittest.TestCase):\n"
            "    def test_solver_reaches_goal_from_near_goal(self):\n"
            "        near_goal = [1, 0, 2]\n"
            "        solution = solve_path(near_goal, GOAL)\n"
            "        self.assertTrue(verify_solution(near_goal, GOAL, solution))\n"
        )
        steps = [
            tool_step("write_file", "puzzle_solver.py", content=implementation_source),
            tool_step("write_file", "tests/test_puzzle_solver.py", content=test_source),
        ]

        state = runtime._implementation_task_progress_state(
            user_message=user_message,
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )

        self.assertEqual(state["phase"], "tests_present_needs_semantic_review")
        issues = "\n".join(state["test_source_issues"])
        self.assertIn("literal near-goal fixture", issues)
        self.assertIn("基準stateから短いlegal move/transition", issues)
        self.assertIn("テストコード上で観測", issues)

    def test_state_space_test_contract_rejects_claimed_near_goal_literal_start_goal(self) -> None:
        runtime = self.runtime()
        user_message = "状態、合法手、ゴール、探索方針で解く小さなパズル実装とunittestを作ってください。"
        plan = self.state_space_plan(runtime, user_message)
        result = runtime._handle_create_plan(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            tool_args={"plan": plan},
            turn_workspace=runtime.execution_root,
            user_message=user_message,
            current_model="test-model",
        )
        self.assertTrue(result["ok"], result.get("error"))

        implementation_source = (
            "class StateSpaceSolver:\n"
            "    def __init__(self, initial_state, goal_state, legal_moves_func):\n"
            "        self.initial_state = initial_state\n"
            "        self.goal_state = goal_state\n"
            "        self.legal_moves_func = legal_moves_func\n"
            "    def solve(self):\n"
            "        return [1, 2, 3]\n"
            "    def verify_solution(self, actions):\n"
            "        return actions[-1] == self.goal_state\n"
        )
        test_source = (
            "import unittest\n"
            "from state_space_solver import StateSpaceSolver\n\n"
            "class TestStateSpaceSolver(unittest.TestCase):\n"
            "    def test_solve_with_near_goal(self):\n"
            "        initial_state = 0\n"
            "        goal_state = 3\n"
            "        def legal_moves_func(state):\n"
            "            return [state + 1] if state < 3 else []\n"
            "        solver = StateSpaceSolver(initial_state, goal_state, legal_moves_func)\n"
            "        actions = solver.solve()\n"
            "        self.assertTrue(solver.verify_solution(actions))\n"
        )
        steps = [
            tool_step("write_file", "state_space_solver.py", content=implementation_source),
            tool_step("write_file", "tests/test_state_space_solver.py", content=test_source),
        ]

        state = runtime._implementation_task_progress_state(
            user_message=user_message,
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )

        self.assertEqual(state["phase"], "tests_present_needs_semantic_review")
        issues = "\n".join(state["test_source_issues"])
        self.assertIn("literal near-goal fixture", issues)
        self.assertIn("literal start/goal", issues)
        self.assertIn("任意の値を直書きせず", issues)

    def test_state_space_test_contract_rejects_unbounded_no_solution_solver_fixture(self) -> None:
        runtime = self.runtime()
        user_message = "状態、合法手、ゴール、探索方針で解く小さなパズル実装とunittestを作ってください。"
        plan = self.state_space_plan(runtime, user_message)
        result = runtime._handle_create_plan(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            tool_args={"plan": plan},
            turn_workspace=runtime.execution_root,
            user_message=user_message,
            current_model="test-model",
        )
        self.assertTrue(result["ok"], result.get("error"))

        implementation_source = (
            "class Puzzle:\n"
            "    def __init__(self):\n"
            "        self.start = (1, 2, 0)\n"
            "        self.goal = (1, 2, 3)\n"
            "    def solve(self, max_depth=None):\n"
            "        if self.start == self.goal:\n"
            "            return []\n"
            "        if max_depth == 0:\n"
            "            return []\n"
            "        return None\n"
            "    def verify_solution(self, start, goal, actions):\n"
            "        return actions is not None and start == goal\n"
        )
        test_source = (
            "import unittest\n"
            "from puzzle_solver import Puzzle\n\n"
            "class TestPuzzle(unittest.TestCase):\n"
            "    def test_solve_unsolvable(self):\n"
            "        puzzle = Puzzle()\n"
            "        solution = puzzle.solve()\n"
            "        self.assertIsNone(solution)\n"
        )
        steps = [
            tool_step("write_file", "puzzle_solver.py", content=implementation_source),
            tool_step("write_file", "tests/test_puzzle_solver.py", content=test_source),
        ]

        state = runtime._implementation_task_progress_state(
            user_message=user_message,
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )

        self.assertEqual(state["phase"], "tests_present_needs_semantic_review")
        issues = "\n".join(state["test_source_issues"])
        self.assertIn("no-solution/unsolvable", issues)
        self.assertIn("max_depth", issues)
        self.assertIn("無制限探索", issues)

    def test_state_space_unittest_timeout_prompts_near_goal_revision(self) -> None:
        runtime = self.runtime()
        user_message = "状態、合法手、ゴール、探索方針で解く小さなパズル実装とunittestを作ってください。"
        plan = self.state_space_plan(runtime, user_message)
        result = runtime._handle_create_plan(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            tool_args={"plan": plan},
            turn_workspace=runtime.execution_root,
            user_message=user_message,
            current_model="test-model",
        )
        self.assertTrue(result["ok"], result.get("error"))

        implementation_source = (
            "def solve_state_space(start, goal):\n"
            "    while True:\n"
            "        pass\n\n"
            "def verify_solution(start, goal, actions):\n"
            "    return False\n"
        )
        test_source = (
            "import unittest\n"
            "from puzzle_solver import solve_state_space, verify_solution\n\n"
            "class TestPuzzleSolver(unittest.TestCase):\n"
            "    def test_solver_reaches_goal(self):\n"
            "        actions = solve_state_space((1, 2, 0), (1, 2, 0))\n"
            "        self.assertTrue(verify_solution((1, 2, 0), (1, 2, 0), actions))\n"
        )
        steps = [
            tool_step("write_file", "puzzle_solver.py", content=implementation_source),
            tool_step("write_file", "tests/test_puzzle_solver.py", content=test_source),
            run_step(
                "python3 -m unittest discover -s tests",
                ok=False,
                stderr="F\nTimed out after 60s",
                failure_type="command_timeout",
                returncode=None,
            ),
        ]

        state = runtime._implementation_task_progress_state(
            user_message=user_message,
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )

        self.assertEqual(state["latest_unittest_failure_type"], "command_timeout")
        prompt = runtime._implementation_task_progress_prompt(state)
        self.assertIn("command_timeout", prompt)
        self.assertIn("deterministic near-goal fixture", prompt)
        self.assertIn("同じrun_testだけのPlanRecordを再提出してはいけません", prompt)

    def test_repeated_unittest_timeout_after_edit_requires_narrow_rerun(self) -> None:
        runtime = self.runtime()
        user_message = "状態、合法手、ゴール、探索方針で解く小さなパズル実装とunittestを作ってください。"
        implementation_source = (
            "def solve_state_space(start, goal):\n"
            "    while True:\n"
            "        pass\n\n"
            "def verify_solution(start, goal, actions):\n"
            "    return False\n"
        )
        test_source = (
            "import unittest\n"
            "from puzzle_solver import solve_state_space, verify_solution\n\n"
            "class TestPuzzleSolver(unittest.TestCase):\n"
            "    def test_solver_reaches_goal(self):\n"
            "        actions = solve_state_space((1, 2, 0), (1, 2, 0))\n"
            "        self.assertTrue(verify_solution((1, 2, 0), (1, 2, 0), actions))\n"
        )
        revised_test_source = test_source.replace("test_solver_reaches_goal", "test_solver_timeout_scope")
        steps = [
            tool_step("write_file", "puzzle_solver.py", content=implementation_source),
            tool_step("write_file", "tests/test_puzzle_solver.py", content=test_source),
            run_step(
                "python3 -m unittest discover -s tests",
                ok=False,
                stderr="\nTimed out after 60s",
                failure_type="command_timeout",
                returncode=None,
            ),
            tool_step("read_file", "tests/test_puzzle_solver.py", content=test_source),
            tool_step("read_file", "puzzle_solver.py", content=implementation_source),
            tool_step("write_file", "puzzle_solver.py", content=implementation_source + "\n"),
            run_step(
                "python3 -m unittest discover -s tests",
                ok=False,
                stderr="\nTimed out after 60s",
                failure_type="command_timeout",
                returncode=None,
            ),
            tool_step("write_file", "tests/test_puzzle_solver.py", content=revised_test_source),
        ]

        state = runtime._implementation_task_progress_state(
            user_message=user_message,
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )

        self.assertTrue(state["successful_edit_after_failed_unittest"])
        self.assertTrue(state["repeated_unittest_failure_signature"])
        self.assertTrue(state["latest_unittest_timed_out"])
        self.assertEqual(state["allowed_next_actions"], ["run_command with a narrower unittest target"])
        prompt = runtime._implementation_task_progress_prompt(state)
        self.assertIn("全体unittest discoverをそのまま再実行せず", prompt)
        self.assertIn("stdout/stderrにtracebackがない", prompt)

        recovery = runtime._completion_contract_recovery_action(
            user_message=user_message,
            steps=steps,
            session_id="main",
            step_index=len(steps) + 1,
            max_steps=20,
            turn_workspace=runtime.execution_root,
        )
        self.assertIsNone(recovery)

        blocked_full = runtime._implementation_task_phase_action_block(
            user_message=user_message,
            tool_name="run_command",
            tool_args={"command": "python3 -m unittest discover -s tests"},
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertEqual(
            blocked_full["reason_code"],
            "implementation_task_failed_unittest_requires_narrow_rerun_after_timeout",
        )

        allowed_narrow = runtime._implementation_task_phase_action_block(
            user_message=user_message,
            tool_name="run_command",
            tool_args={
                "command": "python3 -m unittest tests.test_puzzle_solver.TestPuzzleSolver.test_solver_timeout_scope"
            },
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertIsNone(allowed_narrow)

    def test_narrow_unittest_success_after_timeout_is_diagnostic_not_acceptance(self) -> None:
        runtime = self.runtime()
        user_message = "状態、合法手、ゴール、探索方針で解く小さなパズル実装とunittestを作ってください。"
        implementation_source = (
            "def solve_state_space(start, goal):\n"
            "    while True:\n"
            "        pass\n\n"
            "def verify_solution(start, goal, actions):\n"
            "    return start == goal\n"
        )
        test_source = (
            "import unittest\n"
            "from puzzle_solver import solve_state_space, verify_solution\n\n"
            "class TestPuzzleSolver(unittest.TestCase):\n"
            "    def test_solver_timeout_scope(self):\n"
            "        actions = solve_state_space((1, 0, 2), (1, 2, 0))\n"
            "        self.assertTrue(verify_solution((1, 0, 2), (1, 2, 0), actions))\n\n"
            "    def test_find_empty_index(self):\n"
            "        self.assertEqual((1, 2, 0).index(0), 2)\n"
        )
        revised_test_source = test_source + "\n"
        steps = [
            tool_step("write_file", "puzzle_solver.py", content=implementation_source),
            tool_step("write_file", "tests/test_puzzle_solver.py", content=test_source),
            run_step(
                "python3 -m unittest discover -s tests",
                ok=False,
                stderr="F...FFF\nTimed out after 60s",
                failure_type="command_timeout",
                returncode=None,
            ),
            tool_step("read_file", "tests/test_puzzle_solver.py", content=test_source),
            tool_step("read_file", "puzzle_solver.py", content=implementation_source),
            tool_step("write_file", "tests/test_puzzle_solver.py", content=revised_test_source),
            run_step(
                "python3 -m unittest discover -s tests",
                ok=False,
                stderr="F...FFF\nTimed out after 60s",
                failure_type="command_timeout",
                returncode=None,
            ),
            tool_step("write_file", "tests/test_puzzle_solver.py", content=revised_test_source + "\n"),
            run_step(
                "python3 -m unittest tests.test_puzzle_solver.TestPuzzleSolver.test_find_empty_index -v",
                ok=True,
                stderr=(
                    "test_find_empty_index "
                    "(tests.test_puzzle_solver.TestPuzzleSolver.test_find_empty_index) ... ok\n\n"
                    "----------------------------------------------------------------------\n"
                    "Ran 1 test in 0.000s\n\nOK\n"
                ),
            ),
        ]

        state = runtime._implementation_task_progress_state(
            user_message=user_message,
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )

        self.assertEqual(state["phase"], "unittest_failed_needs_fix")
        self.assertFalse(state["unittest_passed"])
        self.assertEqual(state["successful_unittest_run_count"], 0)
        self.assertEqual(state["latest_unittest_failure_type"], "command_timeout")
        self.assertEqual(state["allowed_next_actions"], ["run_command with a narrower unittest target"])

        recovery = runtime._completion_contract_recovery_action(
            user_message=user_message,
            steps=steps,
            session_id="main",
            step_index=len(steps) + 1,
            max_steps=20,
            turn_workspace=runtime.execution_root,
        )
        self.assertIsNone(recovery)

    def test_narrow_unittest_failure_after_timeout_drives_repair_hints_not_acceptance(self) -> None:
        runtime = self.runtime()
        user_message = "状態、合法手、ゴール、探索方針で解く小さなパズル実装とunittestを作ってください。"
        implementation_source = (
            "goal_state = (1, 2, 0)\n\n"
            "def solve_state_space(start):\n"
            "    frontier = [(start, [])]\n"
            "    seen = {start}\n"
            "    while frontier:\n"
            "        state, path = frontier.pop(0)\n"
            "        if state == goal_state:\n"
            "            return path\n"
            "        for action in ('left', 'right'):\n"
            "            candidate = tuple(reversed(state)) if action == 'left' else state\n"
            "            if candidate not in seen:\n"
            "                seen.add(candidate)\n"
            "                frontier.append((candidate, path + [action]))\n"
            "    return None\n"
        )
        test_source = (
            "import unittest\n"
            "from puzzle_solver import GOAL_STATE, solve_state_space\n\n"
            "class TestPuzzleSolver(unittest.TestCase):\n"
            "    def test_solver_timeout_scope(self):\n"
            "        self.assertEqual(solve_state_space((1, 0, 2)), [])\n"
        )
        diagnostic_stderr = (
            "test_puzzle_solver (unittest.loader._FailedTest.test_puzzle_solver) ... ERROR\n\n"
            "======================================================================\n"
            "ERROR: test_puzzle_solver (unittest.loader._FailedTest.test_puzzle_solver)\n"
            "----------------------------------------------------------------------\n"
            "ImportError: Failed to import test module: test_puzzle_solver\n"
            "Traceback (most recent call last):\n"
            "  File \"/tmp/work/tests/test_puzzle_solver.py\", line 2, in <module>\n"
            "    from puzzle_solver import GOAL_STATE, solve_state_space\n"
            "ImportError: cannot import name 'GOAL_STATE' from 'puzzle_solver' (/tmp/work/puzzle_solver.py)\n"
        )
        steps = [
            tool_step("write_file", "puzzle_solver.py", content=implementation_source),
            tool_step("write_file", "tests/test_puzzle_solver.py", content=test_source),
            run_step(
                "python3 -m unittest discover -s tests",
                ok=False,
                stderr="F...FFF\nTimed out after 60s",
                failure_type="command_timeout",
                returncode=None,
            ),
            run_step(
                "python3 -m unittest tests.test_puzzle_solver.TestPuzzleSolver.test_solver_timeout_scope -v",
                ok=False,
                stderr=diagnostic_stderr,
            ),
        ]

        state = runtime._implementation_task_progress_state(
            user_message=user_message,
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )

        self.assertEqual(state["phase"], "unittest_failed_needs_fix")
        self.assertFalse(state["unittest_passed"])
        self.assertEqual(state["successful_unittest_run_count"], 0)
        self.assertEqual(state["latest_unittest_missing_import"]["name"], "GOAL_STATE")
        self.assertEqual(state["latest_unittest_missing_import"]["module"], "puzzle_solver")
        self.assertEqual(state["allowed_next_actions"], ["read_file puzzle_solver.py once"])
        self.assertTrue(any("GOAL_STATE" in item for item in state["unittest_repair_hints"]))

    def test_tests_missing_after_repetitive_output_prompts_minimal_verification_test(self) -> None:
        runtime = self.runtime()
        user_message = "状態、合法手、ゴール、探索方針で解く小さなパズル実装とunittestを作ってください。"
        plan = self.state_space_plan(runtime, user_message)
        result = runtime._handle_create_plan(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            tool_args={"plan": plan},
            turn_workspace=runtime.execution_root,
            user_message=user_message,
            current_model="test-model",
        )
        self.assertTrue(result["ok"], result.get("error"))
        runtime._append_session_event(
            "main",
            {
                "type": "system_note",
                "role": "system",
                "content": "LLM応答がツール呼び出しJSONとして解釈できませんでした: repetitive_output",
                "code": "llm_output_issue",
                "reason_code": "schema_validation_failed",
            },
        )
        implementation_source = (
            "def legal_moves(state):\n"
            "    return [('advance', state + 1)] if state < 1 else []\n\n"
            "def solve_state_space(start, goal):\n"
            "    return ['advance'] if start != goal else []\n\n"
            "def replay(start, actions):\n"
            "    state = start\n"
            "    for action in actions:\n"
            "        if action != 'advance':\n"
            "            return None\n"
            "        state += 1\n"
            "    return state\n\n"
            "def verify_solution(start, goal, actions):\n"
            "    return replay(start, actions) == goal\n"
        )
        steps = [tool_step("write_file", "puzzle_solver.py", content=implementation_source)]

        state = runtime._implementation_task_progress_state(
            user_message=user_message,
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertEqual(state["phase"], "tests_missing")
        self.assertTrue(state["test_generation_repetitive_output"])

        prompt = runtime._implementation_task_progress_prompt(state)
        self.assertIn("repetitive_output", prompt)
        self.assertIn("最小verification test", prompt)
        self.assertIn("2-3 test methods", prompt)
        self.assertIn("solver/searchを1回呼び", prompt)

    def test_implementation_missing_after_repetitive_output_prompts_small_library_implementation(self) -> None:
        runtime = self.runtime()
        user_message = "状態、合法手、ゴール、探索方針で解く小さなパズル実装とunittestを作ってください。"
        plan = self.state_space_plan(runtime, user_message)
        result = runtime._handle_create_plan(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            tool_args={"plan": plan},
            turn_workspace=runtime.execution_root,
            user_message=user_message,
            current_model="test-model",
        )
        self.assertTrue(result["ok"], result.get("error"))
        runtime._append_session_event(
            "main",
            {
                "type": "system_note",
                "role": "system",
                "content": "LLM応答がツール呼び出しJSONとして解釈できませんでした: repetitive_output",
                "code": "llm_output_issue",
                "reason_code": "repetitive_output",
            },
        )

        state = runtime._implementation_task_progress_state(
            user_message=user_message,
            steps=[],
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertEqual(state["phase"], "implementation_missing")
        self.assertTrue(state["implementation_generation_repetitive_output"])

        prompt = runtime._implementation_task_progress_prompt(state)
        self.assertIn("初回実装生成は repetitive_output/stream_char_limit", prompt)
        self.assertIn("2500 bytes以下", prompt)
        self.assertIn("tests/test_*.py と実行デモ", prompt)

    def test_implementation_missing_after_stream_limit_prompts_small_library_implementation(self) -> None:
        runtime = self.runtime()
        user_message = "状態、合法手、ゴール、探索方針で解く小さなパズル実装とunittestを作ってください。"
        plan = self.state_space_plan(runtime, user_message)
        result = runtime._handle_create_plan(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            tool_args={"plan": plan},
            turn_workspace=runtime.execution_root,
            user_message=user_message,
            current_model="test-model",
        )
        self.assertTrue(result["ok"], result.get("error"))
        runtime._append_session_event(
            "main",
            {
                "type": "system_note",
                "role": "system",
                "content": "LLM応答がツール呼び出しJSONとして解釈できませんでした: stream_char_limit",
                "code": "llm_output_issue",
                "reason_code": "stream_char_limit",
            },
        )

        state = runtime._implementation_task_progress_state(
            user_message=user_message,
            steps=[],
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertEqual(state["phase"], "implementation_missing")
        self.assertTrue(state["implementation_generation_repetitive_output"])

        prompt = runtime._implementation_task_progress_prompt(state)
        self.assertIn("stream_char_limit", prompt)
        self.assertIn("小さいライブラリ実装だけ", prompt)
        self.assertIn("2500 bytes以下", prompt)

    def test_unittest_repair_after_repetitive_output_prompts_minimal_targeted_edit(self) -> None:
        runtime = self.runtime()
        user_message = "状態、合法手、ゴール、探索方針で解く小さなパズル実装とunittestを作ってください。"
        plan = self.state_space_plan(runtime, user_message)
        result = runtime._handle_create_plan(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            tool_args={"plan": plan},
            turn_workspace=runtime.execution_root,
            user_message=user_message,
            current_model="test-model",
        )
        self.assertTrue(result["ok"], result.get("error"))
        runtime._append_session_event(
            "main",
            {
                "type": "system_note",
                "role": "system",
                "content": "LLM応答がツール呼び出しJSONとして解釈できませんでした: repetitive_output",
                "code": "llm_output_issue",
                "reason_code": "schema_validation_failed",
            },
        )
        implementation_source = (
            "def solve_state_space(start, goal):\n"
            "    return ['advance']\n\n"
            "def is_legal_move(state, action):\n"
            "    return action == 'advance'\n\n"
            "def make_move(state, action):\n"
            "    return state + 1\n\n"
            "def verify_solution(start, goal, actions):\n"
            "    state = start\n"
            "    for action in actions:\n"
            "        if not is_legal_move(state, action):\n"
            "            return False\n"
            "        state = make_move(state, action)\n"
            "    return state == goal\n"
        )
        test_source = (
            "import unittest\n"
            "from puzzle_solver import verify_solution\n\n"
            "class TestPuzzleSolver(unittest.TestCase):\n"
            "    def test_invalid_action_raises(self):\n"
            "        with self.assertRaises(ValueError):\n"
            "            verify_solution(0, 1, ['bad'])\n"
        )
        stderr = (
            "F\n======================================================================\n"
            "FAIL: test_invalid_action_raises (test_puzzle_solver.TestPuzzleSolver.test_invalid_action_raises)\n"
            "----------------------------------------------------------------------\n"
            "Traceback (most recent call last):\n"
            "  File \"tests/test_puzzle_solver.py\", line 6, in test_invalid_action_raises\n"
            "    with self.assertRaises(ValueError):\n"
            "AssertionError: ValueError not raised\n"
        )
        steps = [
            tool_step("write_file", "puzzle_solver.py", content=implementation_source),
            tool_step("write_file", "tests/test_puzzle_solver.py", content=test_source),
            run_step("python3 -m unittest discover -s tests", ok=False, stderr=stderr),
            tool_step("read_file", "tests/test_puzzle_solver.py", content=test_source),
            tool_step("read_file", "puzzle_solver.py", content=implementation_source),
        ]

        state = runtime._implementation_task_progress_state(
            user_message=user_message,
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertEqual(state["phase"], "unittest_failed_needs_fix")
        self.assertTrue(state["repair_generation_repetitive_output"])
        allowed_actions = "\n".join(state["allowed_next_actions"])
        self.assertIn("replace_text tests/test_puzzle_solver.py", allowed_actions)
        self.assertIn("replace_text puzzle_solver.py", allowed_actions)
        self.assertNotIn("write_file tests/test_puzzle_solver.py", allowed_actions)
        hints = "\n".join(state["unittest_repair_hints"])
        self.assertIn("返却値・例外・入力検証契約", hints)
        self.assertIn("根拠がある側だけを小さく修正", hints)

        prompt = runtime._implementation_task_progress_prompt(state)
        self.assertIn("直近のrepair生成は repetitive_output", prompt)
        self.assertIn("全文再生成失敗", prompt)
        self.assertIn("write_fileでファイル全体を再出力せず", prompt)
        self.assertIn("old_textは失敗行に関係する1つの関数または数行だけ", prompt)
        self.assertIn("class全体", prompt)
        self.assertIn("失敗signatureを変える最小編集", prompt)

    def test_state_space_test_only_assertion_repair_prioritizes_fixture(self) -> None:
        runtime = self.runtime()
        user_message = "状態、合法手、ゴール、探索方針で解く小さなパズル実装とunittestを作ってください。"
        plan = self.state_space_plan(runtime, user_message)
        result = runtime._handle_create_plan(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            tool_args={"plan": plan},
            turn_workspace=runtime.execution_root,
            user_message=user_message,
            current_model="test-model",
        )
        self.assertTrue(result["ok"], result.get("error"))
        implementation_source = (
            "GOAL = [1, 2, 0]\n\n"
            "def apply_move(state, action):\n"
            "    new_state = list(state)\n"
            "    new_state[1], new_state[2] = new_state[2], new_state[1]\n"
            "    return new_state\n\n"
            "def verify_solution(start, goal, actions):\n"
            "    state = list(start)\n"
            "    for action in actions:\n"
            "        state = apply_move(state, action)\n"
            "    return state == goal\n"
            "\n"
            "def solve_path(start, goal):\n"
            "    return ['right']\n"
        )
        test_source = (
            "import unittest\n"
            "from puzzle_solver import apply_move\n\n"
            "class TestPuzzleSolver(unittest.TestCase):\n"
            "    def test_apply_move_expected_fixture(self):\n"
            "        self.assertEqual(apply_move([1, 0, 2], 'right'), [0, 1, 2])\n"
        )
        stderr = (
            "F\n======================================================================\n"
            "FAIL: test_apply_move_expected_fixture (test_puzzle_solver.TestPuzzleSolver.test_apply_move_expected_fixture)\n"
            "----------------------------------------------------------------------\n"
            "Traceback (most recent call last):\n"
            "  File \"tests/test_puzzle_solver.py\", line 6, in test_apply_move_expected_fixture\n"
            "    self.assertEqual(apply_move([1, 0, 2], 'right'), [0, 1, 2])\n"
            "AssertionError: Lists differ: [1, 2, 0] != [0, 1, 2]\n"
        )
        steps = [
            tool_step("write_file", "puzzle_solver.py", content=implementation_source),
            tool_step("write_file", "tests/test_puzzle_solver.py", content=test_source),
            run_step("python3 -m unittest discover -s tests", ok=False, stderr=stderr),
        ]

        state = runtime._implementation_task_progress_state(
            user_message=user_message,
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )

        self.assertTrue(state["state_space_test_fixture_repair_mode"])
        self.assertEqual(state["allowed_next_actions"], ["read_file tests/test_puzzle_solver.py once"])
        self.assertIn("tests/test_puzzle_solver.py", state["failed_unittest_recovery_read_paths"])
        self.assertNotIn("puzzle_solver.py", state["failed_unittest_recovery_read_paths"])

        state_after_test_read = runtime._implementation_task_progress_state(
            user_message=user_message,
            steps=[*steps, tool_step("read_file", "tests/test_puzzle_solver.py", content=test_source)],
            session_id="main",
            turn_workspace=runtime.execution_root,
        )

        self.assertEqual(
            state_after_test_read["allowed_next_actions"],
            [
                "replace_text tests/test_puzzle_solver.py with a small unique old_text",
                "write_file tests/test_puzzle_solver.py",
            ],
        )
        self.assertTrue(
            any("test fixture" in hint for hint in state_after_test_read["unittest_repair_hints"])
        )

    def test_state_space_test_contract_assertion_keeps_implementation_recovery_path(self) -> None:
        runtime = self.runtime()
        user_message = "状態、合法手、ゴール、探索方針で解く小さなパズル実装とunittestを作ってください。"
        plan = self.state_space_plan(runtime, user_message)
        result = runtime._handle_create_plan(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            tool_args={"plan": plan},
            turn_workspace=runtime.execution_root,
            user_message=user_message,
            current_model="test-model",
        )
        self.assertTrue(result["ok"], result.get("error"))
        implementation_source = (
            "GOAL = [1, 2, 0]\n\n"
            "def is_solvable(state):\n"
            "    return False\n\n"
            "def get_legal_moves(state):\n"
            "    return [(1, 2)]\n\n"
            "def apply_move(state, action):\n"
            "    new_state = list(state)\n"
            "    src, dst = action\n"
            "    new_state[src], new_state[dst] = new_state[dst], new_state[src]\n"
            "    return new_state\n\n"
            "def verify_solution(start, goal, actions):\n"
            "    return False\n\n"
            "def solve_path(start, goal):\n"
            "    return ['not-a-legal-action']\n"
        )
        test_source = (
            "import unittest\n"
            "from puzzle_solver import apply_move, is_solvable, solve_path, verify_solution\n\n"
            "class TestPuzzleSolver(unittest.TestCase):\n"
            "    def test_illegal_move_rejection(self):\n"
            "        with self.assertRaises(Exception):\n"
            "            apply_move([1, 2, 0], (0, 1))\n\n"
            "    def test_is_solvable(self):\n"
            "        self.assertTrue(is_solvable([1, 0, 2]))\n\n"
            "    def test_solver_reaches_goal(self):\n"
            "        solution = solve_path([1, 0, 2], [1, 2, 0])\n"
            "        self.assertIsNotNone(solution)\n"
            "        self.assertTrue(verify_solution([1, 0, 2], [1, 2, 0], solution))\n"
        )
        stderr = (
            "FFF\n"
            "======================================================================\n"
            "FAIL: test_illegal_move_rejection (test_puzzle_solver.TestPuzzleSolver.test_illegal_move_rejection)\n"
            "----------------------------------------------------------------------\n"
            "Traceback (most recent call last):\n"
            "  File \"tests/test_puzzle_solver.py\", line 6, in test_illegal_move_rejection\n"
            "    with self.assertRaises(Exception):\n"
            "AssertionError: Exception not raised\n"
            "\n"
            "======================================================================\n"
            "FAIL: test_is_solvable (test_puzzle_solver.TestPuzzleSolver.test_is_solvable)\n"
            "----------------------------------------------------------------------\n"
            "Traceback (most recent call last):\n"
            "  File \"tests/test_puzzle_solver.py\", line 10, in test_is_solvable\n"
            "    self.assertTrue(is_solvable([1, 0, 2]))\n"
            "AssertionError: False is not true\n"
            "\n"
            "======================================================================\n"
            "FAIL: test_solver_reaches_goal (test_puzzle_solver.TestPuzzleSolver.test_solver_reaches_goal)\n"
            "----------------------------------------------------------------------\n"
            "Traceback (most recent call last):\n"
            "  File \"tests/test_puzzle_solver.py\", line 14, in test_solver_reaches_goal\n"
            "    self.assertIsNotNone(solution)\n"
            "AssertionError: unexpectedly None\n"
        )
        steps = [
            tool_step("write_file", "puzzle_solver.py", content=implementation_source),
            tool_step("write_file", "tests/test_puzzle_solver.py", content=test_source),
            run_step("python3 -m unittest discover -s tests", ok=False, stderr=stderr),
            tool_step("read_file", "tests/test_puzzle_solver.py", content=test_source),
        ]

        state = runtime._implementation_task_progress_state(
            user_message=user_message,
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )

        self.assertFalse(state["state_space_test_fixture_repair_mode"])
        self.assertTrue(state["state_space_test_impl_contract_suspected"])
        self.assertIn("tests/test_puzzle_solver.py", state["failed_unittest_recovery_read_paths"])
        self.assertIn("puzzle_solver.py", state["failed_unittest_recovery_read_paths"])
        self.assertEqual(state["allowed_next_actions"], ["read_file puzzle_solver.py once"])
        self.assertTrue(
            any("公開API契約" in hint for hint in state["unittest_repair_hints"])
        )

    def test_state_space_no_match_fixture_value_blocks_implementation_full_write(self) -> None:
        runtime = self.runtime()
        user_message = "状態、合法手、ゴール、探索方針で解く小さなパズル実装とunittestを作ってください。"
        plan = self.state_space_plan(runtime, user_message)
        result = runtime._handle_create_plan(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            tool_args={"plan": plan},
            turn_workspace=runtime.execution_root,
            user_message=user_message,
            current_model="test-model",
        )
        self.assertTrue(result["ok"], result.get("error"))
        implementation_source = (
            "GOAL = [1, 2, 0]\n\n"
            "def get_legal_moves(state):\n"
            "    return [(1, 2), (2, 1)]\n\n"
            "def apply_move(state, action):\n"
            "    new_state = list(state)\n"
            "    src, dst = action\n"
            "    new_state[src], new_state[dst] = new_state[dst], new_state[src]\n"
            "    return new_state\n\n"
            "def verify_solution(start, goal, actions):\n"
            "    state = list(start)\n"
            "    for action in actions:\n"
            "        state = apply_move(state, action)\n"
            "    return state == goal\n\n"
            "def solve_path(start, goal):\n"
            "    return [(1, 2)]\n"
        )
        test_source = (
            "import unittest\n"
            "from puzzle_solver import apply_move, get_legal_moves, solve_path, verify_solution\n\n"
            "class TestPuzzleSolver(unittest.TestCase):\n"
            "    def test_apply_move_expected_state(self):\n"
            "        state = [1, 0, 2]\n"
            "        move = (1, 2)\n"
            "        new_state = apply_move(state, move)\n"
            "        expected_state = [0, 1, 2]\n"
            "        self.assertEqual(new_state, expected_state)\n\n"
            "    def test_legal_move_count(self):\n"
            "        moves = list(get_legal_moves([1, 0, 2]))\n"
            "        self.assertEqual(len(moves), 1)\n\n"
            "    def test_solver_reaches_goal(self):\n"
            "        solution = solve_path([1, 0, 2], [1, 2, 0])\n"
            "        self.assertTrue(verify_solution([1, 0, 2], [1, 2, 0], solution))\n"
        )
        stderr = (
            "FF.\n"
            "======================================================================\n"
            "FAIL: test_apply_move_expected_state (test_puzzle_solver.TestPuzzleSolver.test_apply_move_expected_state)\n"
            "----------------------------------------------------------------------\n"
            "Traceback (most recent call last):\n"
            "  File \"tests/test_puzzle_solver.py\", line 10, in test_apply_move_expected_state\n"
            "    self.assertEqual(new_state, expected_state)\n"
            "AssertionError: Lists differ: [1, 2, 0] != [0, 1, 2]\n"
            "\n"
            "======================================================================\n"
            "FAIL: test_legal_move_count (test_puzzle_solver.TestPuzzleSolver.test_legal_move_count)\n"
            "----------------------------------------------------------------------\n"
            "Traceback (most recent call last):\n"
            "  File \"tests/test_puzzle_solver.py\", line 14, in test_legal_move_count\n"
            "    self.assertEqual(len(moves), 1)\n"
            "AssertionError: 2 != 1\n"
        )
        steps = [
            tool_step("write_file", "puzzle_solver.py", content=implementation_source),
            tool_step("write_file", "tests/test_puzzle_solver.py", content=test_source),
            run_step("python3 -m unittest discover -s tests", ok=False, stderr=stderr),
            tool_step("read_file", "tests/test_puzzle_solver.py", content=test_source),
            tool_step("read_file", "puzzle_solver.py", content=implementation_source),
        ]
        (runtime.execution_root / "puzzle_solver.py").write_text(implementation_source, encoding="utf-8")
        runtime._append_session_event(
            "main",
            {
                "type": "system_note",
                "code": "implementation_task_progress_blocked",
                "reason_code": "implementation_task_failed_unittest_blocks_unmatched_replace_text",
                "content": "replace_text was blocked after no exact match",
                "details": {
                    "reason_code": "implementation_task_failed_unittest_blocks_unmatched_replace_text",
                    "phase": "unittest_failed_needs_fix",
                    "path": "puzzle_solver.py",
                    "blocked_tool": "replace_text",
                    "exact_old_text_matches": 0,
                },
            },
        )

        state = runtime._implementation_task_progress_state(
            user_message=user_message,
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )

        self.assertTrue(state["state_space_test_fixture_value_suspected"])
        self.assertIn("puzzle_solver.py", state["state_space_fixture_value_impl_blocked_paths"])
        self.assertNotIn("puzzle_solver.py", state["state_space_no_match_exact_replace_paths"])
        self.assertNotIn("puzzle_solver.py", state["failed_unittest_no_match_write_only_paths"])
        self.assertIn("write_file tests/test_puzzle_solver.py", state["allowed_next_actions"])
        self.assertNotIn("replace_text puzzle_solver.py with a small unique old_text", state["allowed_next_actions"])
        self.assertNotIn("write_file puzzle_solver.py", state["allowed_next_actions"])

        blocked = runtime._implementation_task_phase_action_block(
            user_message=user_message,
            tool_name="write_file",
            tool_args={"path": "puzzle_solver.py", "content": implementation_source},
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertEqual(
            blocked["reason_code"],
            "implementation_task_failed_unittest_prioritizes_fixture_repair_after_impl_noop",
        )
        self.assertIn("test artifact", blocked["next_required_action"])
        self.assertNotIn("write_file puzzle_solver.py", blocked["allowed_next_actions"])

    def test_state_space_fixture_value_after_impl_noop_prioritizes_test_artifact(self) -> None:
        runtime = self.runtime()
        user_message = "状態、合法手、ゴール、探索方針で解く小さなパズル実装とunittestを作ってください。"
        plan = self.state_space_plan(runtime, user_message)
        result = runtime._handle_create_plan(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            tool_args={"plan": plan},
            turn_workspace=runtime.execution_root,
            user_message=user_message,
            current_model="test-model",
        )
        self.assertTrue(result["ok"], result.get("error"))
        implementation_source = (
            "class Puzzle:\n"
            "    def __init__(self):\n"
            "        self.goal = [1, 2, 3]\n\n"
            "    def get_legal_moves(self, state):\n"
            "        try:\n"
            "            empty = state.index(0)\n"
            "        except ValueError:\n"
            "            return []\n"
            "        moves = []\n"
            "        if empty > 0:\n"
            "            moves.append('left')\n"
            "        if empty < len(state) - 1:\n"
            "            moves.append('right')\n"
            "        return moves\n"
            "\n"
            "    def apply_move(self, state, action):\n"
            "        new_state = list(state)\n"
            "        empty = new_state.index(0)\n"
            "        delta = -1 if action == 'left' else 1\n"
            "        target = empty + delta\n"
            "        new_state[empty], new_state[target] = new_state[target], new_state[empty]\n"
            "        return new_state\n"
            "\n"
            "    def solve(self, start=None):\n"
            "        return ['right']\n"
            "\n"
            "    def verify_solution(self, initial_state, actions):\n"
            "        current = list(initial_state)\n"
            "        for action in actions:\n"
            "            if action not in self.get_legal_moves(current):\n"
            "                return False\n"
            "            current = self.apply_move(current, action)\n"
            "        return current == self.goal\n"
        )
        test_source = (
            "import unittest\n"
            "from puzzle_solver import Puzzle\n\n"
            "class TestPuzzle(unittest.TestCase):\n"
            "    def test_get_legal_moves(self):\n"
            "        self.assertEqual(Puzzle().get_legal_moves([1, 2, 3]), ['left', 'right'])\n"
        )
        stderr = (
            "F\n"
            "======================================================================\n"
            "FAIL: test_get_legal_moves (test_puzzle_solver.TestPuzzle.test_get_legal_moves)\n"
            "----------------------------------------------------------------------\n"
            "Traceback (most recent call last):\n"
            "  File \"tests/test_puzzle_solver.py\", line 6, in test_get_legal_moves\n"
            "    self.assertEqual(Puzzle().get_legal_moves([1, 2, 3]), ['left', 'right'])\n"
            "AssertionError: Lists differ: [] != ['left', 'right']\n"
        )
        steps = [
            tool_step("write_file", "puzzle_solver.py", content=implementation_source),
            tool_step("write_file", "tests/test_puzzle_solver.py", content=test_source),
            run_step("python3 -m unittest discover -s tests", ok=False, stderr=stderr),
            tool_step("read_file", "tests/test_puzzle_solver.py", content=test_source),
            tool_step("read_file", "puzzle_solver.py", content=implementation_source),
            {
                "tool_name": "replace_text",
                "tool_args": {
                    "path": "puzzle_solver.py",
                    "old_text": (
                        "    def get_legal_moves(self, state):\n"
                        "        try:\n"
                        "            empty = state.index(0)\n"
                        "        except ValueError:\n"
                        "            return []\n"
                    ),
                    "new_text": (
                        "    def get_legal_moves(self, state):\n"
                        "        try:\n"
                        "            empty = state.index(0)\n"
                        "        except ValueError:\n"
                        "            return []\n"
                    ),
                },
                "tool_result": {"ok": False, "path": "puzzle_solver.py", "failure_type": "no_op_edit"},
            },
        ]

        state = runtime._implementation_task_progress_state(
            user_message=user_message,
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )

        self.assertTrue(state["state_space_test_fixture_value_suspected"])
        self.assertEqual(state["state_space_fixture_value_impl_blocked_paths"], ["puzzle_solver.py"])
        self.assertEqual(state["failed_unittest_no_match_write_only_paths"], ["tests/test_puzzle_solver.py"])
        self.assertEqual(state["allowed_next_actions"], ["write_file tests/test_puzzle_solver.py"])
        self.assertNotIn("replace_text puzzle_solver.py with a small unique old_text", state["allowed_next_actions"])
        self.assertTrue(any("tool target" in hint for hint in state["unittest_repair_hints"]))

        blocked = runtime._implementation_task_phase_action_block(
            user_message=user_message,
            tool_name="replace_text",
            tool_args={
                "path": "puzzle_solver.py",
                "old_text": "return []",
                "new_text": "return []",
            },
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertEqual(
            blocked["reason_code"],
            "implementation_task_failed_unittest_prioritizes_fixture_repair_after_impl_noop",
        )
        self.assertEqual(blocked["allowed_next_actions"], ["write_file tests/test_puzzle_solver.py"])

    def test_test_contract_rejects_contradictory_predicate_expectations(self) -> None:
        runtime = self.runtime()
        source = (
            "import unittest\n"
            "from puzzle import Puzzle\n\n"
            "class TestPuzzle(unittest.TestCase):\n"
            "    def setUp(self):\n"
            "        self.puzzle = Puzzle()\n\n"
            "    def test_initial_state_is_not_solved(self):\n"
            "        initial_state = [1, 2, 3, 0]\n"
            "        self.assertFalse(self.puzzle.is_solved(initial_state))\n\n"
            "    def test_goal_state_is_solved(self):\n"
            "        goal_state = [1, 2, 3, 0]\n"
            "        self.assertTrue(self.puzzle.is_solved(goal_state))\n"
        )

        issues = runtime._test_source_contract_issues(
            user_message="Pythonでパズルを実装してunittestで検証してください。",
            test_sources=[("tests/test_puzzle.py", source)],
        )

        issue_text = "\n".join(issues)
        self.assertIn("contradictory predicate expectations", issue_text)
        self.assertIn("is_solved", issue_text)
        self.assertIn("tests/test_puzzle.py", issue_text)

        issue = runtime._python_artifact_contract_issue(
            user_message="Pythonでパズルを実装してunittestで検証してください。",
            tool_name="write_file",
            tool_args={
                "path": "tests/test_puzzle.py",
                "content": source,
            },
        )

        self.assertIsNotNone(issue)
        assert issue is not None
        self.assertEqual(issue["reason_code"], "test_artifact_contract_incomplete")
        self.assertIn("contradictory predicate expectations", issue["message"])

    def test_test_contract_allows_distinct_predicate_fixtures(self) -> None:
        runtime = self.runtime()
        source = (
            "import unittest\n"
            "from puzzle import Puzzle\n\n"
            "class TestPuzzle(unittest.TestCase):\n"
            "    def setUp(self):\n"
            "        self.puzzle = Puzzle()\n\n"
            "    def test_initial_state_is_not_solved(self):\n"
            "        initial_state = [1, 2, 0, 3]\n"
            "        self.assertFalse(self.puzzle.is_solved(initial_state))\n\n"
            "    def test_goal_state_is_solved(self):\n"
            "        goal_state = [1, 2, 3, 0]\n"
            "        self.assertTrue(self.puzzle.is_solved(goal_state))\n"
        )

        issues = runtime._test_source_contract_issues(
            user_message="Pythonでパズルを実装してunittestで検証してください。",
            test_sources=[("tests/test_puzzle.py", source)],
        )

        self.assertNotIn(
            "contradictory predicate expectations",
            "\n".join(issues),
        )

    def test_test_contract_rejects_contradictory_call_result_expectations(self) -> None:
        runtime = self.runtime()
        source = (
            "import unittest\n"
            "from puzzle import Puzzle\n\n"
            "class TestPuzzle(unittest.TestCase):\n"
            "    def setUp(self):\n"
            "        self.puzzle = Puzzle()\n\n"
            "    def test_solve_returns_solution(self):\n"
            "        initial_state = [1, 2, 3, 0]\n"
            "        solution = self.puzzle.solve(initial_state)\n"
            "        self.assertIsNotNone(solution)\n\n"
            "    def test_solve_returns_none_for_impossible_state(self):\n"
            "        impossible_state = [1, 2, 3, 0]\n"
            "        solution = self.puzzle.solve(impossible_state)\n"
            "        self.assertIsNone(solution)\n"
        )

        issues = runtime._test_source_contract_issues(
            user_message="Pythonでパズルを実装してunittestで検証してください。",
            test_sources=[("tests/test_puzzle.py", source)],
        )

        issue_text = "\n".join(issues)
        self.assertIn("contradictory call-result expectations", issue_text)
        self.assertIn("solve", issue_text)
        self.assertIn("None and not None", issue_text)

        issue = runtime._python_artifact_contract_issue(
            user_message="Pythonでパズルを実装してunittestで検証してください。",
            tool_name="write_file",
            tool_args={
                "path": "tests/test_puzzle.py",
                "content": source,
            },
        )

        self.assertIsNotNone(issue)
        assert issue is not None
        self.assertEqual(issue["reason_code"], "test_artifact_contract_incomplete")
        self.assertIn("solvable fixtureとno-solution fixture", issue["suggested_fix"])

    def test_japanese_incomplete_algorithm_comment_counts_as_placeholder(self) -> None:
        runtime = self.runtime()
        source = (
            "def solve(initial_state):\n"
            "    # ここに実際の解法アルゴリズムを実装する\n"
            "    # 今回は簡単なテスト用の実装\n"
            "    return None\n"
        )

        markers = runtime._python_source_placeholder_markers(source)

        self.assertIn("incomplete-comment", markers)

    def test_state_space_plan_enables_progress_gate_even_without_unittest_word(self) -> None:
        runtime = self.runtime()
        user_message = "１５パズルのプログラムを作り、それを実行して自分でクリアしてみて"
        plan = self.state_space_plan(runtime, user_message)
        result = runtime._handle_create_plan(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            tool_args={"plan": plan},
            turn_workspace=runtime.execution_root,
            user_message=user_message,
            current_model="test-model",
        )
        self.assertTrue(result["ok"], result.get("error"))

        manual_ui_source = (
            "class FifteenPuzzle:\n"
            "    def __init__(self):\n"
            "        self.board = list(range(1, 16)) + [None]\n"
            "        self.empty_index = 15\n"
            "    def get_neighbors(self, index):\n"
            "        return [index - 1] if index > 0 else [index + 1]\n"
            "    def move(self, number):\n"
            "        return number in self.board\n"
            "    def is_solved(self):\n"
            "        return self.board == list(range(1, 16)) + [None]\n"
            "    def play(self):\n"
            "        while not self.is_solved():\n"
            "            input('move: ')\n"
        )
        steps = [tool_step("write_file", "fifteen_puzzle.py", content=manual_ui_source)]
        state = runtime._implementation_task_progress_state(
            user_message=user_message,
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )

        self.assertTrue(state["applicable"])
        self.assertIn("unittest_run", state["contract"])
        self.assertEqual(state["phase"], "implementation_present_needs_semantic_review")
        self.assertTrue(
            runtime._plan_run_test_should_wait_for_progress(
                pending_task={"work_type": "run_test"},
                progress_state=state,
            )
        )

    def test_plan_work_unit_blocks_nested_decomposition_without_returning_child(self) -> None:
        runtime = self.runtime()
        user_message = "4x4 sliding puzzle を状態、合法手、ゴール、探索方針で解く実装を作ってください。"
        runtime.frame_manager.create_root_frame(user_message)
        plan = self.state_space_plan(runtime, user_message)
        result = runtime._handle_create_plan(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=1,
            tool_args={"plan": plan},
            turn_workspace=runtime.execution_root,
            user_message=user_message,
            current_model="test-model",
        )
        self.assertTrue(result["ok"], result.get("error"))
        active_before = runtime.frame_manager.current_frame()
        self.assertIsNotNone(active_before)
        assert active_before is not None
        self.assertEqual(active_before.depth, 1)

        blocked = runtime._handle_decompose_tasks(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=2,
            tool_args={
                "tasks": [
                    {
                        "goal": "rewrite this WorkUnit into another implementation task",
                        "work_type": "edit",
                        "first_action": {"tool": "write_file", "args": {"path": "puzzle.py", "content": "print('bad')\n"}},
                        "success_evidence": "nested task completed",
                        "why_not_direct_action": "nested split",
                    }
                ]
            },
            turn_workspace=runtime.execution_root,
            user_message=user_message,
            current_model="test-model",
        )

        self.assertFalse(blocked["ok"])
        event = blocked["event"]
        self.assertEqual(event["details"]["failure_type"], "plan_work_unit_decomposition_blocked")
        self.assertEqual(event["details"]["blocked_by"], "frame_contract")
        self.assertTrue(event["details"]["plan_work_unit"])
        self.assertIn("直接", event["details"]["suggested_fix"])
        active_after = runtime.frame_manager.current_frame()
        self.assertIsNotNone(active_after)
        assert active_after is not None
        self.assertEqual(active_after.frame_id, active_before.frame_id)
        parent = runtime.frame_manager.parent_of(active_after)
        self.assertIsNotNone(parent)
        assert parent is not None
        self.assertEqual(parent.working_memory.completed_child_tasks, [])
        events = read_jsonl(runtime.paths.session_events_path("main"))
        self.assertFalse(any(event.get("type") == "frame_returned" for event in events))

    def test_planning_required_prompt_exposes_only_create_plan_surface(self) -> None:
        runtime = self.runtime()
        user_message = "4x4 sliding puzzle を状態、合法手、ゴール、探索方針で解く実装を作ってください。"
        runtime.frame_manager.create_root_frame(user_message)

        system_prompt = runtime._system_prompt(
            suppress_frame_operations=False,
            allowed_tool_names=["create_plan"],
        )
        action_prompt = runtime._build_prompt(
            goal_text="",
            recent_events=[],
            steps=[],
            current_phase="PLANNING_REQUIRED",
            user_message=user_message,
            suppress_frame_operations=False,
        )

        self.assertIn("- create_plan:", system_prompt)
        self.assertNotIn("- write_file:", system_prompt)
        self.assertNotIn("- decompose_tasks:", system_prompt)
        self.assertNotIn("フレーム操作:", system_prompt)
        self.assertNotIn("利用可能なフレーム操作", action_prompt)
        self.assertNotIn("write_file と append_file の tool_args.content", action_prompt)
        self.assertIn("PlanRecord には実装コード本文", action_prompt)
        self.assertIn("first_action は list_files か read_file", action_prompt)

    def test_plan_execution_prompt_uses_progress_action_surface(self) -> None:
        runtime = self.runtime()
        state = {
            "applicable": True,
            "contract_state": "incomplete",
            "phase": "implementation_missing",
            "allowed_next_actions": ["write_file puzzle_solver.py"],
        }

        allowed_tool_names = runtime._implementation_task_schema_tool_names(state)
        system_prompt = runtime._system_prompt(
            suppress_frame_operations=runtime._implementation_task_should_suppress_frame_operations(state),
            allowed_tool_names=allowed_tool_names,
        )

        self.assertEqual(allowed_tool_names, ["write_file"])
        self.assertIn("- write_file:", system_prompt)
        self.assertNotIn("- create_plan:", system_prompt)
        self.assertNotIn("- decompose_tasks:", system_prompt)

    def test_state_space_request_can_plan_then_reach_unittest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            user_message = "15パズルのような状態、合法手、ゴール、探索方針を持つ未知タスクをPython実装し、tests/にunittestを追加して検証してください。"
            impl = (
                "from collections import deque\n\n"
                "def solve_state_space(start, goal, legal_moves, *, max_nodes=10000):\n"
                "    if start == goal:\n"
                "        return []\n"
                "    queue = deque([(start, [])])\n"
                "    seen = {start}\n"
                "    while queue and len(seen) <= max_nodes:\n"
                "        state, path = queue.popleft()\n"
                "        for action, next_state in legal_moves(state):\n"
                "            if next_state in seen:\n"
                "                continue\n"
                "            next_path = path + [action]\n"
                "            if next_state == goal:\n"
                "                return next_path\n"
                "            seen.add(next_state)\n"
                "            queue.append((next_state, next_path))\n"
                "    return None\n\n"
                "def replay(start, moves, transition):\n"
                "    state = start\n"
                "    for move in moves:\n"
                "        state = transition(state, move)\n"
                "    return state\n"
            )
            test = (
                "import unittest\nfrom puzzle_solver import replay, solve_state_space\n\n"
                "SWAPS = {0: {'R': 1, 'D': 2}, 1: {'L': 0, 'D': 3}, 2: {'U': 0, 'R': 3}, 3: {'U': 1, 'L': 2}}\n\n"
                "def transition(state, move):\n"
                "    blank = state.index(0)\n"
                "    target = SWAPS[blank][move]\n"
                "    data = list(state)\n"
                "    data[blank], data[target] = data[target], data[blank]\n"
                "    return tuple(data)\n\n"
                "def legal_moves(state):\n"
                "    blank = state.index(0)\n"
                "    for move in sorted(SWAPS[blank]):\n"
                "        yield move, transition(state, move)\n\n"
                "class TestPuzzleSolver(unittest.TestCase):\n"
                "    def test_finds_and_replays_solution(self):\n"
                "        start = (1, 0, 3, 2)\n"
                "        goal = (1, 2, 3, 0)\n"
                "        moves = solve_state_space(start, goal, legal_moves)\n"
                "        self.assertIsNotNone(moves)\n"
                "        self.assertEqual(replay(start, moves, transition), goal)\n\n"
                "    def test_reports_none_when_bounded_search_exhausts(self):\n"
                "        self.assertIsNone(solve_state_space((1, 0, 3, 2), (1, 2, 3, 0), legal_moves, max_nodes=0))\n"
            )
            bootstrap_workspace(root)
            runtime = AgentRuntime(root, llm_backend=FakeBackend([]))
            plan = self.state_space_plan(runtime, user_message)
            plan["work_units"] = [
                {
                    "unit_id": "write-generic-state-space-solver",
                    "goal": "Write the generic state-space search implementation.",
                    "depends_on": [],
                    "work_type": "edit",
                    "first_action": {"tool": "list_files", "args": {"path": "."}},
                    "success_evidence": "puzzle_solver.py contains solve_state_space and replay.",
                    "should_open_child_frame": True,
                },
                {
                    "unit_id": "write-state-space-tests",
                    "goal": "Write tests that call the final verifier by replaying each legal action to the goal.",
                    "depends_on": ["write-generic-state-space-solver"],
                    "work_type": "edit",
                    "first_action": {"tool": "list_files", "args": {"path": "."}},
                    "success_evidence": "tests/test_puzzle_solver.py asserts that legal action replay reaches the goal.",
                    "should_open_child_frame": True,
                },
                {
                    "unit_id": "run-state-space-tests",
                    "goal": "Run tests that verify legal action replay reaches the goal.",
                    "depends_on": ["write-state-space-tests"],
                    "work_type": "run_test",
                    "first_action": {"tool": "run_command", "args": {"command": "python3 -m unittest discover -s tests"}},
                    "success_evidence": "final_verifier replay validates legal actions and unittest exits successfully.",
                    "should_open_child_frame": True,
                },
            ]
            responses = [
                json.dumps({
                    "assistant_message": "create generic state-space PlanRecord",
                    "tool_name": "create_plan",
                    "tool_args": {"plan": plan},
                }),
                json.dumps({
                    "assistant_message": "write implementation after plan first observation",
                    "tool_name": "write_file",
                    "tool_args": {"path": "puzzle_solver.py", "content": impl},
                }),
                json.dumps({
                    "assistant_message": "write state-space tests",
                    "tool_name": "write_file",
                    "tool_args": {"path": "tests/test_puzzle_solver.py", "content": test},
                }),
                json.dumps({
                    "assistant_message": "finish after planned unittest evidence",
                    "tool_name": "finish",
                    "tool_args": {"final_answer": "implementation, tests, and verifier run completed"},
                }),
            ]
            runtime = AgentRuntime(root, llm_backend=FakeBackend(responses))
            runtime.config.setdefault("runtime", {})["max_steps_per_message"] = 4

            result = runtime.send_message(user_message, run_immediately=True)

            self.assertTrue(result["ok"], result.get("error"))
            events = read_jsonl(root / "state" / "sessions" / "main" / "events.jsonl")
            self.assertTrue(any(event.get("type") == "plan_record" for event in events))
            self.assertTrue(any(event.get("type") == "task_plan" for event in events))
            self.assertTrue(any(event.get("type") == "tool_result" and event.get("tool_name") == "run_command" for event in events))
            self.assertTrue(any(event.get("type") == "finish" for event in events))
            self.assertTrue(any((root / "workspaces").glob("**/puzzle_solver.py")))
            self.assertTrue(any((root / "workspaces").glob("**/tests/test_puzzle_solver.py")))

    def test_edit_work_unit_returns_after_successful_edit_and_continues_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            user_message = "状態、合法手、ゴール、探索方針を持つ未知タスクをPython実装してください。"
            bootstrap_workspace(root)
            seed_runtime = AgentRuntime(root, llm_backend=FakeBackend([]))
            plan = self.state_space_plan(seed_runtime, user_message)
            plan["work_units"] = [
                {
                    "unit_id": "write-state-tool",
                    "goal": "Write the state-space tool implementation.",
                    "depends_on": [],
                    "work_type": "edit",
                    "first_action": {"tool": "list_files", "args": {"path": "."}},
                    "success_evidence": "puzzle.py contains executable state-space code.",
                    "should_open_child_frame": True,
                },
                {
                    "unit_id": "verify-replay",
                    "goal": "Run final_verifier that replays each legal action and confirms it reaches the goal.",
                    "depends_on": ["write-state-tool"],
                    "work_type": "run_test",
                    "first_action": {"tool": "run_command", "args": {"command": "python3 -m unittest discover -s tests"}},
                    "success_evidence": "final_verifier replays only legal actions from the start state and reaches the goal.",
                    "should_open_child_frame": True,
                },
            ]
            responses = [
                json.dumps({"tool_name": "create_plan", "tool_args": {"plan": plan}}),
                json.dumps({
                    "tool_name": "write_file",
                    "tool_args": {
                        "path": "puzzle.py",
                        "content": "print('final_verifier replays legal actions and reaches the goal')\n",
                    },
                }),
                json.dumps({
                    "tool_name": "read_file",
                    "tool_args": {"path": "puzzle.py"},
                }),
            ]
            runtime = AgentRuntime(root, llm_backend=FakeBackend(responses))
            runtime.config.setdefault("runtime", {})["max_steps_per_message"] = 3
            runtime.config.setdefault("runtime", {})["verified_implementation_max_steps"] = 3
            runtime.config.setdefault("runtime", {})["planned_implementation_max_steps"] = 3
            runtime.runtime_config["verified_implementation_max_steps"] = 3
            runtime.runtime_config["planned_implementation_max_steps"] = 3

            result = runtime.send_message(user_message, run_immediately=True)

            self.assertEqual(result["run"]["last_result"].get("error"), "step limit reached")
            events = read_jsonl(root / "state" / "sessions" / "main" / "events.jsonl")
            returns = [event for event in events if event.get("type") == "frame_returned"]
            self.assertGreaterEqual(len(returns), 1)
            self.assertTrue(any("write_file" in str(event.get("return_payload") or {}) for event in returns))
            self.assertTrue(any(event.get("code") == "plan_execution_paused_for_progress" for event in events))
            self.assertFalse(any(event.get("type") == "tool_result" and event.get("tool_name") == "run_command" for event in events))

    def test_step_limit_final_gate_does_not_accept_with_pending_plan_work_units(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            user_message = "状態、合法手、ゴール、探索方針を持つ未知タスクをPython実装してください。"
            bootstrap_workspace(root)
            seed_runtime = AgentRuntime(root, llm_backend=FakeBackend([]))
            plan = self.state_space_plan(seed_runtime, user_message)
            plan["work_units"] = [
                {
                    "unit_id": "write-state-tool",
                    "goal": "Write the state-space tool implementation.",
                    "depends_on": [],
                    "work_type": "edit",
                    "first_action": {"tool": "list_files", "args": {"path": "."}},
                    "success_evidence": "puzzle.py contains executable state-space code.",
                    "should_open_child_frame": True,
                },
                {
                    "unit_id": "verify-replay-once",
                    "goal": "Run final_verifier that replays each legal action and confirms it reaches the goal.",
                    "depends_on": ["write-state-tool"],
                    "work_type": "run_test",
                    "first_action": {"tool": "run_command", "args": {"command": "python3 -m unittest discover -s tests"}},
                    "success_evidence": "final_verifier replays only legal actions from the start state and reaches the goal.",
                    "should_open_child_frame": True,
                },
                {
                    "unit_id": "verify-replay-again",
                    "goal": "Run an independent final verifier that replays legal actions to the goal.",
                    "depends_on": ["verify-replay-once"],
                    "work_type": "run_test",
                    "first_action": {"tool": "run_command", "args": {"command": "python3 -m unittest discover -s tests"}},
                    "success_evidence": "independent final_verifier replay validates legal actions and reaches the goal.",
                    "should_open_child_frame": True,
                },
            ]
            responses = [
                json.dumps({"tool_name": "create_plan", "tool_args": {"plan": plan}}),
                json.dumps({
                    "tool_name": "write_file",
                    "tool_args": {
                        "path": "puzzle.py",
                        "content": "print('final_verifier replays legal actions and reaches the goal')\n",
                    },
                }),
                json.dumps({
                    "tool_name": "read_file",
                    "tool_args": {"path": "puzzle.py"},
                }),
            ]
            runtime = AgentRuntime(root, llm_backend=FakeBackend(responses))
            runtime.config.setdefault("runtime", {})["max_steps_per_message"] = 3
            runtime.config.setdefault("runtime", {})["verified_implementation_max_steps"] = 3
            runtime.config.setdefault("runtime", {})["planned_implementation_max_steps"] = 3
            runtime.runtime_config["verified_implementation_max_steps"] = 3
            runtime.runtime_config["planned_implementation_max_steps"] = 3

            result = runtime.send_message(user_message, run_immediately=True)

            last_result = result["run"]["last_result"]
            self.assertFalse(last_result["ok"])
            self.assertEqual(last_result["error"], "step limit reached")
            events = read_jsonl(root / "state" / "sessions" / "main" / "events.jsonl")
            self.assertTrue(any(event.get("code") == "plan_execution_paused_for_progress" for event in events))
            self.assertTrue(any(event.get("code") == "step_limit_reached" for event in events))
            self.assertFalse(any(event.get("code") == "step_limit_final_gate" for event in events))
            self.assertFalse(any(event.get("type") == "finish" and event.get("role") == "assistant" for event in events))

    def test_continue_after_step_limit_reuses_workspace_and_event_sourced_steps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bootstrap_workspace(root)
            backend = FakeBackend([
                json.dumps({
                    "tool_name": "write_file",
                    "tool_args": {
                        "path": "calc.py",
                        "content": "def add(a, b):\n    return a + b\n",
                    },
                }),
            ])
            runtime = AgentRuntime(root, llm_backend=backend)
            runtime.config.setdefault("runtime", {})["max_steps_per_message"] = 1
            runtime.runtime_config["max_steps_per_message"] = 1
            runtime.config.setdefault("runtime", {})["verified_implementation_max_steps"] = 1
            runtime.runtime_config["verified_implementation_max_steps"] = 1
            first = runtime.send_message(
                "Pythonでadd関数を実装してunittestで検証してください。",
                run_immediately=True,
            )
            self.assertEqual(first["run"]["last_result"]["error"], "step limit reached")
            first_workspace = next((root / "workspaces" / "runs").iterdir())
            self.assertTrue((first_workspace / "calc.py").exists())

            backend.responses.append(json.dumps({
                "tool_name": "write_file",
                "tool_args": {
                    "path": "tests/test_calc.py",
                    "content": (
                        "import unittest\n"
                        "from calc import add\n\n"
                        "class TestCalc(unittest.TestCase):\n"
                        "    def test_add(self):\n"
                        "        self.assertEqual(add(2, 3), 5)\n\n"
                        "if __name__ == '__main__':\n"
                        "    unittest.main()\n"
                    ),
                },
            }))
            second = runtime.send_message("続けてください。前回workspaceとeventsを正として未完タスクを進めてください。", run_immediately=True)

            self.assertEqual(second["run"]["last_result"]["error"], "step limit reached")
            workspaces = sorted((root / "workspaces" / "runs").iterdir())
            self.assertEqual(workspaces, [first_workspace])
            self.assertTrue((first_workspace / "tests" / "test_calc.py").exists())
            events = read_jsonl(root / "state" / "sessions" / "main" / "events.jsonl")
            resume_events = [event for event in events if event.get("code") == "workspace_resume"]
            self.assertTrue(resume_events)
            self.assertGreaterEqual(resume_events[-1]["details"]["resumed_step_count"], 1)
            prompts = read_jsonl(root / "state" / "sessions" / "main" / "prompts.jsonl")
            self.assertIn("tests/test_*.py", prompts[-1]["prompt"])
            self.assertIn("実装タスク進行状態: tests_missing", prompts[-1]["prompt"])
            self.assertIn("[current_steps]", prompts[-1]["prompt"])

    def test_new_task_after_step_limit_starts_new_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bootstrap_workspace(root)
            backend = FakeBackend([
                json.dumps({
                    "tool_name": "write_file",
                    "tool_args": {
                        "path": "first.py",
                        "content": "def first():\n    return 1\n",
                    },
                }),
                json.dumps({
                    "tool_name": "write_file",
                    "tool_args": {
                        "path": "second.py",
                        "content": "def second():\n    return 2\n",
                    },
                }),
            ])
            runtime = AgentRuntime(root, llm_backend=backend)
            runtime.config.setdefault("runtime", {})["max_steps_per_message"] = 1
            runtime.runtime_config["max_steps_per_message"] = 1
            runtime.config.setdefault("runtime", {})["verified_implementation_max_steps"] = 1
            runtime.runtime_config["verified_implementation_max_steps"] = 1

            runtime.send_message("Pythonでfirst関数を実装してunittestで検証してください。", run_immediately=True)
            runtime.send_message("別のPythonタスクとしてsecond関数を実装してunittestで検証してください。", run_immediately=True)

            workspaces = sorted((root / "workspaces" / "runs").iterdir())
            self.assertEqual(len(workspaces), 2)
            self.assertTrue(any((workspace / "first.py").exists() for workspace in workspaces))
            self.assertTrue(any((workspace / "second.py").exists() for workspace in workspaces))
            events = read_jsonl(root / "state" / "sessions" / "main" / "events.jsonl")
            self.assertFalse(any(event.get("code") == "workspace_resume" for event in events))

    def test_unittest_timeout_triggers_plan_revision_phase(self) -> None:
        runtime = self.runtime()
        step = tool_step(
            "run_command",
            ok=False,
            command="python3 -m unittest discover -s tests",
            error="Timed out after 60 seconds",
            failure_type="command_timeout",
        )

        phase = runtime._current_phase(
            user_message="4x4 sliding puzzle を状態、合法手、ゴール、探索方針で解く実装を作ってください。",
            steps=[step],
            recent_events=[],
        )

        self.assertEqual(phase, "PLAN_REVISION")

        blocked = runtime._planner_action_blocked_event(
            session_id="main",
            turn_id="turn",
            queue_id="queue",
            step_index=2,
            turn_workspace=runtime.execution_root,
            tool_name="write_file",
            user_message="4x4 sliding puzzle を状態、合法手、ゴール、探索方針で解く実装を作ってください。",
            current_phase=phase,
            recent_events=[],
            steps=[step],
        )
        self.assertIsNotNone(blocked)
        events = read_jsonl(runtime.paths.session_events_path("main"))
        revision_events = [event for event in events if event.get("type") == "plan_revision"]
        self.assertTrue(revision_events)
        self.assertIn("command_timeout", revision_events[-1]["details"]["revision_reasons"])

    def test_invalid_work_package_after_plan_attempt_triggers_plan_revision_phase(self) -> None:
        runtime = self.runtime()
        event = {
            "type": "system_note",
            "role": "system",
            "content": "decompose_tasks requires a concrete work_package",
            "code": "work_package_invalid",
            "reason_code": "missing_work_package_contract",
            "details": {"failure_type": "work_package_invalid"},
        }

        phase = runtime._current_phase(
            user_message="4x4 sliding puzzle を状態、合法手、ゴール、探索方針で解く実装を作ってください。",
            steps=[],
            recent_events=[event],
        )

        self.assertEqual(phase, "PLAN_REVISION")

    def test_finish_acceptance_prompt_is_not_filtered_out(self) -> None:
        runtime = self.runtime()
        event = {
            "type": "system_note",
            "role": "system",
            "content": "完了受理判定: blocked",
            "code": "finish_acceptance",
            "details": {
                "status": "blocked",
                "reason": "unittest evidence is missing",
                "missing": ["unittest_run"],
                "suggested_fix": "run python3 -m unittest discover -s tests",
            },
        }

        prompt = runtime._build_prompt(
            goal_text="",
            recent_events=[event],
            steps=[],
            user_message="Pythonで add_one(value) を実装してください。",
        )

        self.assertIn("完了受理判定: blocked", prompt)
        self.assertIn("unittest evidence is missing", prompt)

    def test_run_command_multi_command_denial_is_structured_for_recovery(self) -> None:
        runtime = self.runtime()

        result = runtime.tools.execute("run_command", {"command": "echo one && echo two"})

        self.assertFalse(result["ok"])
        self.assertEqual(result["failure_type"], "multi_command_denied")
        self.assertEqual(result["blocked_by"], "tool_safety_policy")
        self.assertIn("allowed_next_actions", result)
        self.assertIn("next_required_action", result)

    def test_run_command_allows_separators_inside_quoted_process_argument(self) -> None:
        runtime = self.runtime()

        result = runtime.tools.execute("run_command", {"command": "python3 -c \"print('one'); print('two')\""})

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["returncode"], 0)
        self.assertIn("one", result["stdout"])
        self.assertIn("two", result["stdout"])

    def test_run_command_blocks_separator_outside_quotes(self) -> None:
        runtime = self.runtime()

        result = runtime.tools.execute("run_command", {"command": "python3 -c \"print('one')\" ; python3 -c \"print('two')\""})

        self.assertFalse(result["ok"])
        self.assertEqual(result["failure_type"], "multi_command_denied")
        self.assertIn("outside quotes", result["error"])

    def test_test_semantic_review_blocks_broad_replace_and_allows_write_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = self.runtime(root)
            message = "Pythonで add_one(value) を実装し、tests/ にunittestを追加して検証してください。"
            impl = "def add_one(value):\n    return value + 1\n"
            test = (
                "import unittest\nfrom math_tools import MathTools\n\n"
                "class TestMathTools(unittest.TestCase):\n"
                "    def test_class_only(self):\n"
                "        self.assertEqual(MathTools().add_one(1), 2)\n"
            )
            large_test = test + "\n" + "\n".join(f"# filler {index}" for index in range(180)) + "\n"
            (runtime.execution_root / "tests").mkdir(parents=True, exist_ok=True)
            (runtime.execution_root / "tests" / "test_math_tools.py").write_text(large_test, encoding="utf-8")
            steps = [
                tool_step("write_file", "math_tools.py", content=impl),
                tool_step("write_file", "tests/test_math_tools.py", content=large_test),
                tool_step("read_file", "tests/test_math_tools.py", content=large_test),
            ]
            runtime._append_session_event(
                "main",
                {
                    "type": "system_note",
                    "role": "system",
                    "content": "tests do not directly exercise requested top-level public API add_one",
                    "code": "semantic_implementation_review",
                    "reason_code": "runtime_semantic_review_requires_revision",
                    "details": {
                        "review": "tests do not directly exercise requested top-level public API add_one",
                        "review_source": "runtime",
                        "requires_revision": True,
                        "semantic_issues": ["tests do not directly exercise requested top-level public API add_one"],
                        "test_paths": ["tests/test_math_tools.py"],
                        "fingerprint": "test-api-coverage",
                    },
                    "step_index": 2,
                },
            )

            state = runtime._implementation_task_progress_state(
                user_message=message,
                steps=steps,
                session_id="main",
                turn_workspace=runtime.execution_root,
            )
            self.assertEqual(state["phase"], "tests_present_needs_semantic_review")
            prompt = runtime._implementation_task_progress_prompt(state)
            self.assertIn("テストファイル全体の巨大 replace_text は禁止", prompt)
            self.assertIn("このphaseでactionableな編集対象は tests/test_*.py だけです", prompt)
            self.assertIn("implementation editは次phaseで許可されるまで実行しない", prompt)

            blocked = runtime._implementation_task_phase_action_block(
                user_message=message,
                tool_name="replace_text",
                tool_args={
                    "path": "tests/test_math_tools.py",
                    "old_text": large_test,
                    "new_text": large_test.replace("MathTools().add_one(1)", "add_one(1)", 1),
                },
                steps=steps,
                session_id="main",
                turn_workspace=runtime.execution_root,
            )
            self.assertEqual(blocked["reason_code"], "implementation_task_test_semantic_blocks_broad_replace_text")
            self.assertEqual(
                blocked["allowed_next_actions"],
                [
                    "replace_text tests/test_math_tools.py with a small unique old_text",
                    "write_file tests/test_math_tools.py",
                ],
            )
            self.assertEqual(blocked["blocked_by"], "implementation_task_progress_controller")

    def test_failed_unittest_no_match_after_reads_requires_write_file(self) -> None:
        runtime = self.runtime()
        message = "Pythonで add_one(value) を実装し、tests/ にunittestを追加して検証してください。"
        impl = "def add_one(value):\n    return value\n"
        test = (
            "import unittest\nfrom math_tools import add_one\n\n"
            "class TestMathTools(unittest.TestCase):\n"
            "    def test_add_one(self):\n"
            "        self.assertEqual(add_one(1), 2)\n"
        )
        stderr = (
            "FAIL: test_add_one (test_math_tools.TestMathTools.test_add_one)\n"
            f"  File \"{runtime.execution_root / 'tests' / 'test_math_tools.py'}\", line 5, in test_add_one\n"
            "AssertionError: 1 != 2\n"
        )
        steps = [
            tool_step("write_file", "math_tools.py", content=impl),
            tool_step("write_file", "tests/test_math_tools.py", content=test),
            run_step("python3 -m unittest discover -s tests", ok=False, stderr=stderr),
            tool_step("read_file", "tests/test_math_tools.py", content=test),
            tool_step("read_file", "math_tools.py", content=impl),
            {
                "tool_name": "replace_text",
                "tool_args": {"path": "math_tools.py", "old_text": "def add_one(value): return value", "new_text": "def add_one(value):\n    return value + 1\n"},
                "tool_result": {"ok": False, "path": "math_tools.py", "failure_type": "replace_text_no_match"},
            },
        ]

        state = runtime._implementation_task_progress_state(
            user_message=message,
            steps=steps,
            turn_workspace=runtime.execution_root,
        )
        self.assertEqual(state["phase"], "unittest_failed_needs_fix")
        self.assertEqual(
            set(state["allowed_next_actions"]),
            {"write_file tests/test_math_tools.py", "write_file math_tools.py"},
        )
        self.assertEqual(
            set(state["failed_unittest_no_match_write_only_paths"]),
            {"tests/test_math_tools.py", "math_tools.py"},
        )

        blocked = runtime._implementation_task_phase_action_block(
            user_message=message,
            tool_name="replace_text",
            tool_args={
                "path": "math_tools.py",
                "old_text": "    return value",
                "new_text": "    return value + 1",
            },
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertEqual(blocked["reason_code"], "implementation_task_failed_unittest_requires_write_after_no_match")

        blocked_read = runtime._implementation_task_phase_action_block(
            user_message=message,
            tool_name="read_file",
            tool_args={"path": "math_tools.py"},
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertEqual(blocked_read["reason_code"], "implementation_task_failed_unittest_requires_write_after_no_match")

        broad_write = runtime._implementation_task_phase_action_block(
            user_message=message,
            tool_name="write_file",
            tool_args={
                "path": "math_tools.py",
                "content": "def add_one(value):\n    return value + 1\n",
            },
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertIsNone(broad_write)

        test_write = runtime._implementation_task_phase_action_block(
            user_message=message,
            tool_name="write_file",
            tool_args={
                "path": "tests/test_math_tools.py",
                "content": test.replace("self.assertEqual(add_one(1), 2)", "self.assertEqual(add_one(1), 1)"),
            },
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertIsNone(test_write)

    def test_failed_unittest_repeated_noop_switches_to_alternate_target(self) -> None:
        runtime = self.runtime()
        message = "Pythonで add_one(value) を実装し、tests/ にunittestを追加して検証してください。"
        impl = "def add_one(value):\n    return value\n"
        test = (
            "import unittest\nfrom math_tools import add_one\n\n"
            "class TestMathTools(unittest.TestCase):\n"
            "    def test_add_one(self):\n"
            "        self.assertEqual(add_one(1), 2)\n"
        )
        stderr = (
            "FAIL: test_add_one (test_math_tools.TestMathTools.test_add_one)\n"
            f"  File \"{runtime.execution_root / 'tests' / 'test_math_tools.py'}\", line 5, in test_add_one\n"
            f"  File \"{runtime.execution_root / 'math_tools.py'}\", line 2, in add_one\n"
            "AssertionError: 1 != 2\n"
        )
        steps = [
            tool_step("write_file", "math_tools.py", content=impl),
            tool_step("write_file", "tests/test_math_tools.py", content=test),
            run_step("python3 -m unittest discover -s tests", ok=False, stderr=stderr),
            tool_step("read_file", "tests/test_math_tools.py", content=test),
            tool_step("read_file", "math_tools.py", content=impl),
            {
                "tool_name": "replace_text",
                "tool_args": {
                    "path": "tests/test_math_tools.py",
                    "old_text": "        self.assertEqual(add_one(1), 2)",
                    "new_text": "        self.assertEqual(add_one(1), 2)",
                },
                "tool_result": {
                    "ok": False,
                    "path": "tests/test_math_tools.py",
                    "failure_type": "no_op_edit",
                },
            },
            {
                "tool_name": "write_file",
                "tool_args": {"path": "tests/test_math_tools.py", "content": test},
                "tool_result": {
                    "ok": False,
                    "path": "tests/test_math_tools.py",
                    "failure_type": "no_op_edit",
                },
            },
        ]

        state = runtime._implementation_task_progress_state(
            user_message=message,
            steps=steps,
            turn_workspace=runtime.execution_root,
        )

        self.assertEqual(state["phase"], "unittest_failed_needs_fix")
        self.assertEqual(state["failed_unittest_repeated_noop_paths"], ["tests/test_math_tools.py"])
        self.assertEqual(state["failed_unittest_noop_blocked_paths"], ["tests/test_math_tools.py"])
        self.assertIn("math_tools.py", state["failed_unittest_noop_alternate_paths"])
        self.assertIn("write_file math_tools.py", state["allowed_next_actions"])
        self.assertNotIn("write_file tests/test_math_tools.py", state["allowed_next_actions"])
        prompt = runtime._implementation_task_progress_prompt(state)
        self.assertIn("no_op edit反復の修復契約", prompt)
        self.assertIn("次に照合・修正すべき別対象", prompt)

        blocked = runtime._implementation_task_phase_action_block(
            user_message=message,
            tool_name="write_file",
            tool_args={"path": "tests/test_math_tools.py", "content": test},
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertEqual(blocked["reason_code"], "implementation_task_failed_unittest_blocks_repeated_noop_target")

        allowed_impl = runtime._implementation_task_phase_action_block(
            user_message=message,
            tool_name="write_file",
            tool_args={"path": "math_tools.py", "content": "def add_one(value):\n    return value + 1\n"},
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertIsNone(allowed_impl)

    def test_failed_unittest_implementation_exception_does_not_switch_to_test_repair(self) -> None:
        runtime = self.runtime()
        message = "Pythonでグラフ処理を実装し、unittestで検証し、サンプルで実行して表示してください。"
        impl = (
            "def order_graph(graph):\n"
            "    queue = [node for node in graph if not graph[node]]\n"
            "    result = []\n"
            "    while queue:\n"
            "        node = queue.pop(0)\n"
            "        result.append(node)\n"
            "    if len(result) != len(graph):\n"
            "        raise ValueError('graph cycle')\n"
            "    return result\n"
        )
        fixed_impl = (
            "def order_graph(graph):\n"
            "    result = []\n"
            "    temporary = set()\n"
            "    permanent = set()\n"
            "\n"
            "    def visit(node):\n"
            "        if node in permanent:\n"
            "            return\n"
            "        if node in temporary:\n"
            "            raise ValueError('graph cycle')\n"
            "        temporary.add(node)\n"
            "        for dependency in graph.get(node, []):\n"
            "            visit(dependency)\n"
            "        temporary.remove(node)\n"
            "        permanent.add(node)\n"
            "        result.append(node)\n"
            "\n"
            "    for node in graph:\n"
            "        visit(node)\n"
            "    return result\n"
        )
        test = (
            "import unittest\n"
            "from graph_tools import order_graph\n\n"
            "class TestGraphTools(unittest.TestCase):\n"
            "    def test_dependency_order(self):\n"
            "        graph = {'a': ['b'], 'b': ['c'], 'c': []}\n"
            "        self.assertEqual(order_graph(graph), ['c', 'b', 'a'])\n"
        )
        stderr = (
            "E\n"
            "======================================================================\n"
            "ERROR: test_dependency_order (test_graph_tools.TestGraphTools.test_dependency_order)\n"
            "----------------------------------------------------------------------\n"
            "Traceback (most recent call last):\n"
            f"  File \"{runtime.execution_root / 'tests' / 'test_graph_tools.py'}\", line 7, in test_dependency_order\n"
            "    self.assertEqual(order_graph(graph), ['c', 'b', 'a'])\n"
            f"  File \"{runtime.execution_root / 'graph_tools.py'}\", line 7, in order_graph\n"
            "    raise ValueError('graph cycle')\n"
            "ValueError: graph cycle\n"
        )
        steps = [
            tool_step("write_file", "graph_tools.py", content=impl),
            tool_step("write_file", "tests/test_graph_tools.py", content=test),
            run_step("python3 -m unittest discover -s tests", ok=False, stderr=stderr),
            tool_step("read_file", "tests/test_graph_tools.py", content=test),
            tool_step("read_file", "graph_tools.py", content=impl),
            tool_step("write_file", "graph_tools.py", content=impl, ok=False, failure_type="no_op_edit"),
            tool_step("write_file", "graph_tools.py", content=impl, ok=False, failure_type="no_op_edit"),
        ]

        state = runtime._implementation_task_progress_state(
            user_message=message,
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )

        self.assertTrue(state["latest_unittest_points_to_implementation_exception"])
        self.assertEqual(state["failed_unittest_repair_target_paths"], ["graph_tools.py"])
        self.assertEqual(state["failed_unittest_repeated_noop_paths"], ["graph_tools.py"])
        self.assertEqual(state["failed_unittest_noop_blocked_paths"], [])
        self.assertEqual(state["allowed_next_actions"], ["write_file graph_tools.py"])
        self.assertNotIn("write_file tests/test_graph_tools.py", state["allowed_next_actions"])
        prompt = runtime._implementation_task_progress_prompt(state)
        self.assertIn("implementation traceback修復契約", prompt)

        blocked_test_rewrite = runtime._implementation_task_phase_action_block(
            user_message=message,
            tool_name="write_file",
            tool_args={
                "path": "tests/test_graph_tools.py",
                "content": test.replace("['c', 'b', 'a']", "['a']"),
            },
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertEqual(
            blocked_test_rewrite["reason_code"],
            "implementation_task_failed_unittest_requires_write_after_no_match",
        )
        self.assertEqual(blocked_test_rewrite["allowed_next_actions"], ["write_file graph_tools.py"])

        allowed_impl_rewrite = runtime._implementation_task_phase_action_block(
            user_message=message,
            tool_name="write_file",
            tool_args={"path": "graph_tools.py", "content": fixed_impl},
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertIsNone(allowed_impl_rewrite)

    def test_failed_unittest_no_match_still_allows_unread_traceback_file_read(self) -> None:
        runtime = self.runtime()
        message = "Pythonで add_one(value) を実装し、tests/ にunittestを追加して検証してください。"
        impl = "def add_one(value):\n    return value\n"
        test = (
            "import unittest\nfrom math_tools import add_one\n\n"
            "class TestMathTools(unittest.TestCase):\n"
            "    def test_add_one(self):\n"
            "        self.assertEqual(add_one(1), 2)\n"
        )
        stderr = (
            "FAIL: test_add_one (test_math_tools.TestMathTools.test_add_one)\n"
            f"  File \"{runtime.execution_root / 'tests' / 'test_math_tools.py'}\", line 5, in test_add_one\n"
            f"  File \"{runtime.execution_root / 'math_tools.py'}\", line 2, in add_one\n"
            "AssertionError: 1 != 2\n"
        )
        steps = [
            tool_step("write_file", "math_tools.py", content=impl),
            tool_step("write_file", "tests/test_math_tools.py", content=test),
            run_step("python3 -m unittest discover -s tests", ok=False, stderr=stderr),
            tool_step("read_file", "math_tools.py", content=impl),
            {
                "tool_name": "replace_text",
                "tool_args": {
                    "path": "math_tools.py",
                    "old_text": "def add_one(value): return value",
                    "new_text": "def add_one(value):\n    return value + 1\n",
                },
                "tool_result": {"ok": False, "path": "math_tools.py", "failure_type": "replace_text_no_match"},
            },
        ]

        state = runtime._implementation_task_progress_state(
            user_message=message,
            steps=steps,
            turn_workspace=runtime.execution_root,
        )
        self.assertEqual(
            state["allowed_next_actions"],
            ["read_file tests/test_math_tools.py once"],
        )

        allowed_read = runtime._implementation_task_phase_action_block(
            user_message=message,
            tool_name="read_file",
            tool_args={"path": "tests/test_math_tools.py"},
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertIsNone(allowed_read)

        blocked_replace = runtime._implementation_task_phase_action_block(
            user_message=message,
            tool_name="replace_text",
            tool_args={
                "path": "math_tools.py",
                "old_text": "    return value",
                "new_text": "    return value + 1",
            },
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertEqual(
            blocked_replace["reason_code"],
            "implementation_task_failed_unittest_requires_recovery_read",
        )

    def test_failed_unittest_blocked_unmatched_replace_forces_write_file_next(self) -> None:
        runtime = self.runtime()
        message = "Pythonで add_one(value) を実装し、tests/ にunittestを追加して検証してください。"
        impl = "def add_one(value):\n    return value\n"
        test = (
            "import unittest\nfrom math_tools import add_one\n\n"
            "class TestMathTools(unittest.TestCase):\n"
            "    def test_add_one(self):\n"
            "        self.assertEqual(add_one(1), 2)\n"
        )
        stderr = (
            "FAIL: test_add_one (test_math_tools.TestMathTools.test_add_one)\n"
            f"  File \"{runtime.execution_root / 'tests' / 'test_math_tools.py'}\", line 5, in test_add_one\n"
            "AssertionError: 1 != 2\n"
        )
        steps = [
            tool_step("write_file", "math_tools.py", content=impl),
            tool_step("write_file", "tests/test_math_tools.py", content=test),
            run_step("python3 -m unittest discover -s tests", ok=False, stderr=stderr),
            tool_step("read_file", "tests/test_math_tools.py", content=test),
            tool_step("read_file", "math_tools.py", content=impl),
        ]
        runtime._append_session_event(
            "main",
            {
                "type": "system_note",
                "code": "implementation_task_progress_blocked",
                "reason_code": "implementation_task_failed_unittest_blocks_unmatched_replace_text",
                "content": "replace_text was blocked after no exact match",
                "details": {
                    "reason_code": "implementation_task_failed_unittest_blocks_unmatched_replace_text",
                    "phase": "unittest_failed_needs_fix",
                    "path": "math_tools.py",
                    "blocked_tool": "replace_text",
                    "exact_old_text_matches": 0,
                },
            },
        )
        for index in range(300):
            runtime._append_session_event(
                "main",
                {
                    "type": "runtime_event",
                    "event_name": "llm_stream_chunk",
                    "content": f"chunk-{index}",
                },
            )

        state = runtime._implementation_task_progress_state(
            user_message=message,
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )

        self.assertEqual(
            set(state["failed_unittest_no_match_write_only_paths"]),
            {"tests/test_math_tools.py", "math_tools.py"},
        )
        self.assertEqual(
            set(state["allowed_next_actions"]),
            {"write_file tests/test_math_tools.py", "write_file math_tools.py"},
        )

    def test_repeated_unittest_no_match_does_not_reoffer_nonreducing_full_write(self) -> None:
        runtime = self.runtime()
        message = "Pythonで add_one(value) を実装し、tests/ にunittestを追加して検証してください。"
        impl = "def add_one(value):\n    return value\n"
        changed_but_wrong_impl = "def add_one(value):\n    return value - 1\n"
        test = (
            "import unittest\nfrom math_tools import add_one\n\n"
            "class TestMathTools(unittest.TestCase):\n"
            "    def test_add_one(self):\n"
            "        self.assertEqual(add_one(1), 2)\n"
        )
        stderr = (
            "FAIL: test_add_one (test_math_tools.TestMathTools.test_add_one)\n"
            f"  File \"{runtime.execution_root / 'tests' / 'test_math_tools.py'}\", line 5, in test_add_one\n"
            "AssertionError: 1 != 2\n"
        )
        steps = [
            tool_step("write_file", "math_tools.py", content=impl),
            tool_step("write_file", "tests/test_math_tools.py", content=test),
            run_step("python3 -m unittest discover -s tests", ok=False, stderr=stderr),
            tool_step("read_file", "tests/test_math_tools.py", content=test),
            tool_step("read_file", "math_tools.py", content=impl),
            tool_step("write_file", "math_tools.py", content=changed_but_wrong_impl),
            run_step("python3 -m unittest discover -s tests", ok=False, stderr=stderr),
        ]
        runtime._append_session_event(
            "main",
            {
                "type": "system_note",
                "code": "implementation_task_progress_blocked",
                "reason_code": "implementation_task_failed_unittest_blocks_unmatched_replace_text",
                "content": "replace_text was blocked after no exact match",
                "details": {
                    "reason_code": "implementation_task_failed_unittest_blocks_unmatched_replace_text",
                    "phase": "unittest_failed_needs_fix",
                    "path": "math_tools.py",
                    "blocked_tool": "replace_text",
                    "exact_old_text_matches": 0,
                },
            },
        )

        state = runtime._implementation_task_progress_state(
            user_message=message,
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )

        self.assertTrue(state["repeated_unittest_failure_signature"])
        self.assertEqual(state["same_signature_nonreducing_edit_paths"], ["math_tools.py"])
        self.assertNotIn("math_tools.py", state["failed_unittest_no_match_write_only_paths"])
        self.assertIn("replace_text math_tools.py with a small unique old_text", state["allowed_next_actions"])
        self.assertNotIn("write_file math_tools.py", state["allowed_next_actions"])

        blocked = runtime._implementation_task_phase_action_block(
            user_message=message,
            tool_name="write_file",
            tool_args={"path": "math_tools.py", "content": "def add_one(value):\n    return value + 1\n"},
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertEqual(
            blocked["reason_code"],
            "implementation_task_failed_unittest_blocks_repeated_full_write_after_nonreducing_signature",
        )
        self.assertNotIn("write_file math_tools.py", blocked["allowed_next_actions"])

    def test_failed_unittest_blocks_unrelated_implementation_edit(self) -> None:
        runtime = self.runtime()
        message = "Pythonで add_one(value) を実装し、tests/ にunittestを追加して検証してください。"
        impl = "def add_one(value):\n    return value\n"
        test = (
            "import unittest\nfrom math_tools import add_one\n\n"
            "class TestMathTools(unittest.TestCase):\n"
            "    def test_add_one(self):\n"
            "        self.assertEqual(add_one(1), 2)\n"
        )
        stderr = (
            "FAIL: test_add_one (test_math_tools.TestMathTools.test_add_one)\n"
            f"  File \"{runtime.execution_root / 'tests' / 'test_math_tools.py'}\", line 5, in test_add_one\n"
            "AssertionError: 1 != 2\n"
        )
        steps = [
            tool_step("write_file", "math_tools.py", content=impl),
            tool_step("write_file", "tests/test_math_tools.py", content=test),
            run_step("python3 -m unittest discover -s tests", ok=False, stderr=stderr),
            tool_step("read_file", "tests/test_math_tools.py", content=test),
            tool_step("read_file", "math_tools.py", content=impl),
        ]

        blocked = runtime._implementation_task_phase_action_block(
            user_message=message,
            tool_name="replace_text",
            tool_args={
                "path": "other_tools.py",
                "old_text": "return value",
                "new_text": "return value + 1",
            },
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertEqual(blocked["reason_code"], "implementation_task_failed_unittest_requires_targeted_fix")

    def test_unittest_success_reaches_finish_phase(self) -> None:
        runtime = self.runtime()
        message = "Pythonで add_one(value) を実装し、tests/ にunittestを追加して検証してください。"
        impl = "def add_one(value):\n    return value + 1\n"
        test = (
            "import unittest\nfrom math_tools import add_one\n\n"
            "class TestMathTools(unittest.TestCase):\n"
            "    def test_add_one(self):\n"
            "        self.assertEqual(add_one(1), 2)\n"
        )
        steps = [
            tool_step("write_file", "math_tools.py", content=impl),
            tool_step("write_file", "tests/test_math_tools.py", content=test),
            run_step("python3 -m unittest discover -s tests", ok=True, stderr=".\nOK\n"),
        ]
        state = runtime._implementation_task_progress_state(user_message=message, steps=steps)
        self.assertEqual(state["phase"], "external_audit_required")
        self.assertEqual(state["allowed_next_actions"], ["run_command python3 -m unittest discover -s tests"])

        audited_steps = [
            *steps,
            run_step("python3 -m unittest discover -s tests", ok=True, stderr=".\nOK\n"),
        ]
        state = runtime._implementation_task_progress_state(user_message=message, steps=audited_steps)
        self.assertEqual(state["phase"], "external_contract_satisfied")
        self.assertEqual(state["allowed_next_actions"], ["finish"])

    def test_unittest_success_with_display_request_requires_stdout_result(self) -> None:
        runtime = self.runtime()
        message = "Pythonで add_one(value) を実装し、tests/ にunittestを追加して検証し、実行して表示してください。"
        impl = (
            "def add_one(value):\n"
            "    return value + 1\n\n"
            "if __name__ == '__main__':\n"
            "    print(add_one(1))\n"
        )
        test = (
            "import unittest\nfrom math_tools import add_one\n\n"
            "class TestMathTools(unittest.TestCase):\n"
            "    def test_add_one(self):\n"
            "        self.assertEqual(add_one(1), 2)\n"
        )
        audited_steps = [
            tool_step("write_file", "math_tools.py", content=impl),
            tool_step("write_file", "tests/test_math_tools.py", content=test),
            tool_step("write_file", "tests/test_math_tools.py", content=test),
            run_step("python3 -m unittest discover -s tests", ok=True, stderr=".\nOK\n"),
            run_step("python3 -m unittest discover -s tests", ok=True, stderr=".\nOK\n"),
        ]

        state = runtime._implementation_task_progress_state(user_message=message, steps=audited_steps)

        self.assertEqual(state["phase"], "result_display_required")
        self.assertEqual(state["contract_state"], "incomplete")
        self.assertEqual(state["missing_requirements"], ["stdout_displayed"])
        self.assertEqual(
            state["allowed_next_actions"],
            [
                "run_command <non-interactive demo or verifier command that prints the requested visible result to stdout>"
            ],
        )

        recovery = runtime._completion_contract_recovery_action(
            session_id="main",
            user_message=message,
            steps=audited_steps,
            step_index=4,
            max_steps=20,
            turn_workspace=runtime.execution_root,
        )
        self.assertIsNotNone(recovery)
        self.assertEqual(recovery["reason_code"], "completion_contract_stdout_recovery")
        self.assertEqual(recovery["tool_args"]["command"], "python3 math_tools.py")

        displayed_steps = [
            *audited_steps,
            run_step("python3 math_tools.py", ok=True, stdout="2\n"),
        ]
        state = runtime._implementation_task_progress_state(user_message=message, steps=displayed_steps)
        self.assertEqual(state["phase"], "external_contract_satisfied")
        final_answer = runtime._implementation_contract_final_answer(
            user_message=message,
            steps=displayed_steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertIsNotNone(final_answer)
        assert final_answer is not None
        self.assertIn("表示結果:", final_answer)
        self.assertIn("2", final_answer)
        self.assertIn("ユーザー向け表示結果: satisfied", final_answer)
        self.assertEqual(final_answer.count("- tests/test_math_tools.py"), 1)

    def test_result_display_required_blocks_repeating_empty_stdout_command(self) -> None:
        runtime = self.runtime()
        message = "Pythonで add_one(value) を実装し、unittestで検証し、サンプルで実行して表示してください。"
        impl = "def add_one(value):\n    return value + 1\n"
        test = (
            "import unittest\nfrom math_tools import add_one\n\n"
            "class TestMathTools(unittest.TestCase):\n"
            "    def test_add_one(self):\n"
            "        self.assertEqual(add_one(1), 2)\n"
        )
        steps = [
            tool_step("write_file", "math_tools.py", content=impl),
            tool_step("write_file", "tests/test_math_tools.py", content=test),
            run_step("python3 -m unittest discover -s tests", ok=True, stderr=".\nOK\n"),
            run_step("python3 -m unittest discover -s tests", ok=True, stderr=".\nOK\n"),
            run_step("python3 math_tools.py", ok=True, stdout=""),
        ]

        state = runtime._implementation_task_progress_state(
            user_message=message,
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )

        self.assertEqual(state["phase"], "result_display_required")
        self.assertEqual(state["successful_empty_stdout_commands"], ["python3 math_tools.py"])
        self.assertEqual(state["missing_requirements"], ["stdout_displayed"])
        self.assertIn(
            "run_command <non-interactive python -c/import command that prints a concrete sample result to stdout>",
            state["allowed_next_actions"],
        )
        self.assertIn(
            "replace_text math_tools.py with a small unique old_text to add __main__ demo printing sample result",
            state["allowed_next_actions"],
        )
        self.assertIn("write_file math_tools.py", state["allowed_next_actions"])
        self.assertIn("空stdout実行の反復禁止", runtime._implementation_task_progress_prompt(state))

        blocked_repeat = runtime._implementation_task_phase_action_block(
            user_message=message,
            tool_name="run_command",
            tool_args={"command": "python3 math_tools.py"},
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertEqual(
            blocked_repeat["reason_code"],
            "implementation_task_result_display_blocks_repeated_empty_stdout_command",
        )

        blocked_unittest = runtime._implementation_task_phase_action_block(
            user_message=message,
            tool_name="run_command",
            tool_args={"command": "python3 -m unittest discover -s tests"},
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertEqual(
            blocked_unittest["reason_code"],
            "implementation_task_result_display_blocks_unittest_rerun",
        )

        allowed_demo_command = runtime._implementation_task_phase_action_block(
            user_message=message,
            tool_name="run_command",
            tool_args={"command": "python3 -c \"from math_tools import add_one; print(add_one(2))\""},
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertIsNone(allowed_demo_command)

        allowed_demo_edit = runtime._implementation_task_phase_action_block(
            user_message=message,
            tool_name="write_file",
            tool_args={
                "path": "math_tools.py",
                "content": impl + "\nif __name__ == '__main__':\n    print(add_one(2))\n",
            },
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertIsNone(allowed_demo_edit)

        blocked_finish = runtime._implementation_task_phase_action_block(
            user_message=message,
            tool_name="finish",
            tool_args={},
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertEqual(blocked_finish["reason_code"], "implementation_task_phase_requires_result_display")

    def test_external_audit_required_blocks_finish_until_second_unittest(self) -> None:
        runtime = self.runtime()
        message = "Pythonで add_one(value) を実装し、tests/ にunittestを追加して検証してください。"
        steps = [
            tool_step("write_file", "math_tools.py", content="def add_one(value):\n    return value + 1\n"),
            tool_step(
                "write_file",
                "tests/test_math_tools.py",
                content=(
                    "import unittest\nfrom math_tools import add_one\n\n"
                    "class TestMathTools(unittest.TestCase):\n"
                    "    def test_add_one(self):\n"
                    "        self.assertEqual(add_one(1), 2)\n"
                ),
            ),
            run_step("python3 -m unittest discover -s tests", ok=True, stderr=".\nOK\n"),
        ]
        blocked = runtime._implementation_task_phase_action_block(
            user_message=message,
            tool_name="finish",
            tool_args={"final_answer": "done"},
            steps=steps,
            session_id="main",
            turn_workspace=runtime.execution_root,
        )
        self.assertEqual(blocked["reason_code"], "implementation_task_phase_requires_external_audit")

    def test_unittest_failure_after_previous_success_is_rendered_as_external_audit_failure(self) -> None:
        runtime = self.runtime()
        message = "Pythonで add_one(value) を実装し、tests/ にunittestを追加して検証してください。"
        impl = "def add_one(value):\n    return value + 1\n"
        test = (
            "import unittest\nfrom math_tools import add_one\n\n"
            "class TestMathTools(unittest.TestCase):\n"
            "    def test_add_one(self):\n"
            "        self.assertEqual(add_one(1), 2)\n"
        )
        stderr = (
            "FAIL: test_add_one (test_math_tools.TestMathTools.test_add_one)\n"
            "  File \"tests/test_math_tools.py\", line 5, in test_add_one\n"
            "AssertionError: 1 != 2\n"
        )
        steps = [
            tool_step("write_file", "math_tools.py", content=impl),
            tool_step("write_file", "tests/test_math_tools.py", content=test),
            run_step("python3 -m unittest discover -s tests", ok=True, stderr=".\nOK\n"),
            run_step("python3 -m unittest discover -s tests", ok=False, stderr=stderr),
        ]

        state = runtime._implementation_task_progress_state(
            user_message=message,
            steps=steps,
            turn_workspace=runtime.execution_root,
        )
        prompt = runtime._implementation_task_progress_prompt(state)

        self.assertEqual(state["phase"], "unittest_failed_needs_fix")
        self.assertEqual(state["latest_unittest_failure_type"], "external_audit_failed_after_previous_success")
        self.assertIn("外部audit/regression失敗", prompt)
        self.assertIn("previous_successful_unittest_run_count: 1", prompt)

    def test_consultant_advice_without_structured_issue_does_not_block_unittest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = self.runtime(root)
            message = "Pythonで add_one(value) を実装し、tests/ にunittestを追加して検証してください。"
            impl = "def add_one(value):\n    return value + 1\n"
            test = (
                "import unittest\nfrom math_tools import add_one\n\n"
                "class TestMathTools(unittest.TestCase):\n"
                "    def test_add_one(self):\n"
                "        self.assertEqual(add_one(1), 2)\n"
            )
            steps = [
                tool_step("write_file", "math_tools.py", content=impl),
                tool_step("write_file", "tests/test_math_tools.py", content=test),
            ]
            runtime._append_session_event(
                "main",
                {
                    "type": "system_note",
                    "role": "system",
                    "content": "相談役LLMからの実装レビュー: additional cases could be useful, but requested behavior is covered",
                    "code": "semantic_implementation_review",
                    "reason_code": "consultant_advice",
                    "details": {
                        "review": "additional cases could be useful, but requested behavior is covered",
                        "review_source": "consultant",
                        "requires_revision": True,
                        "semantic_issues": [],
                        "fingerprint": "advisory-only",
                    },
                    "step_index": 2,
                },
            )
            state = runtime._implementation_task_progress_state(
                user_message=message,
                steps=steps,
                session_id="main",
                turn_workspace=runtime.execution_root,
            )
            self.assertEqual(state["phase"], "unittest_not_run")

    def test_unknown_python_task_runs_through_generic_contract_to_controller_finish(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            impl = "def slugify(text):\n    return '-'.join(str(text).strip().lower().split())\n"
            test = (
                "import unittest\nfrom text_tools import slugify\n\n"
                "class TestTextTools(unittest.TestCase):\n"
                "    def test_slugify(self):\n"
                "        self.assertEqual(slugify(' Hello  P4 Runtime '), 'hello-p4-runtime')\n"
            )
            responses = [
                json.dumps({
                    "assistant_message": "write implementation",
                    "tool_name": "write_file",
                    "tool_args": {"path": "text_tools.py", "content": impl},
                }),
                json.dumps({
                    "assistant_message": "write tests",
                    "tool_name": "write_file",
                    "tool_args": {"path": "tests/test_text_tools.py", "content": test},
                }),
            ]
            runtime = self.runtime(root, responses)
            result = runtime.send_message(
                "Pythonで未知のslugify(text)を text_tools.py に実装し、tests/ にunittestを追加して検証してください。",
                run_immediately=True,
            )
            self.assertTrue(result["ok"])
            self.assertTrue((root / "workspaces").exists())
            events = read_jsonl(root / "state" / "sessions" / "main" / "events.jsonl")
            self.assertTrue(any(event.get("code") == "controller_finish" for event in events))
            self.assertTrue(any(event.get("type") == "finish" for event in events))
            unittest_results = [
                event
                for event in events
                if event.get("type") == "tool_result"
                and event.get("tool_name") == "run_command"
                and "unittest" in str(event.get("content") or "")
            ]
            self.assertEqual(len(unittest_results), 2)
            self.assertTrue(any(event.get("code") == "implementation_task_progress" for event in events))

    def test_repo_map_summarizes_workspace_symbols_for_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bootstrap_workspace(root)
            (root / "name_tools.py").write_text(
                "import re\n\nclass Normalizer:\n    def clean(self, text):\n        return re.sub(r'\\s+', ' ', text).strip()\n",
                encoding="utf-8",
            )
            runtime = AgentRuntime(root, llm_backend=FakeBackend([]))
            state = runtime._implementation_task_progress_state(
                user_message="Pythonで normalize_name(text) を実装し、tests/ にunittestを追加して検証してください。",
                steps=[tool_step("write_file", "name_tools.py", content="def normalize_name(text):\n    return text\n")],
                turn_workspace=root,
            )
            self.assertIn("repo_map:", state["repo_map_excerpt"])
            self.assertIn("Normalizer.clean", state["repo_map_excerpt"])
            tool_result = runtime.tools.execute("repo_map", {"path": "."})
            self.assertTrue(tool_result["ok"])
            self.assertTrue(any(item.get("name") == "Normalizer.clean" for item in tool_result["repo_map"]["symbols"]))


if __name__ == "__main__":
    unittest.main()
