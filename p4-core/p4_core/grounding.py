from __future__ import annotations

import ast
import json
import re
import time
from typing import Any

from p4_core.output_contract import stdout_looks_like_user_visible_result, visible_result_sanity_issues
from p4_core.schema_validation import validate_json_schema
from p4_core.runtime_profile import is_runtime_identity_query, runtime_profile_evidence
from p4_core.schemas import FINISH_ACCEPTANCE_SCHEMA, JUDGE_VERDICT_SCHEMA
from p4_core.workspace import append_jsonl, append_session_event, now_iso, read_json, read_jsonl


def _grounding_issues(self, *, user_message: str, final_answer: str, steps: list[dict[str, Any]]) -> list[str]:
    issues: list[str] = []
    executed_commands = [
        str(((step.get("tool_result") or {}).get("command") or "")).strip()
        for step in steps
        if step.get("tool_name") == "run_command"
    ]
    evidence_package = _grounding_evidence_package(user_message=user_message, steps=steps)
    evidence_text = json.dumps(evidence_package, ensure_ascii=False).lower()
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


def _grounding_evidence_package(*, user_message: str, steps: list[dict[str, Any]]) -> dict[str, Any]:
    tool_facts: list[dict[str, Any]] = []
    external_facts: list[dict[str, Any]] = []
    for step in steps:
        tool_name = str(step.get("tool_name") or "")
        result = step.get("tool_result") or {}
        if not isinstance(result, dict):
            result = {}
        if tool_name in {"read_file", "list_files", "search_code", "run_command", "write_file", "append_file", "replace_text"}:
            tool_facts.append(
                {
                    "evidence_type": "tool_result",
                    "tool_name": tool_name,
                    "ok": bool(result.get("ok")),
                    "result": result,
                }
            )
        if tool_name in {"search_code", "read_file", "run_command"} and bool(result.get("ok")):
            external_facts.append(
                {
                    "evidence_type": "external_or_environment_fact",
                    "tool_name": tool_name,
                    "result": result,
                }
            )
    runtime_facts = [runtime_profile_evidence()] if is_runtime_identity_query(user_message) else []
    return {
        "runtime_facts": runtime_facts,
        "tool_facts": tool_facts,
        "external_facts": external_facts,
    }


def _finish_acceptance_evaluation(self, *, user_message: str, final_answer: str, steps: list[dict[str, Any]]) -> dict[str, Any]:
    evidence = _finish_acceptance_evidence(steps)
    contract = _finish_acceptance_contract(user_message)
    missing = [
        item
        for item in contract
        if not bool(evidence.get(item))
    ]
    evaluation: dict[str, Any] = {
        "status": "success" if not missing else "partial_success",
        "contract": contract,
        "missing": missing,
        "evidence": evidence,
        "semantic_status": "unchecked",
        "review": None,
        "limitations": [],
    }
    if missing:
        evaluation["limitations"].append("required observable evidence is missing")
        if "stdout_displayed" in missing:
            evaluation["allowed_next_actions"] = [
                "run_command <non-interactive demo or verifier command that prints the requested visible result to stdout>"
            ]
            evaluation["suggested_fix"] = (
                "ユーザーが実行結果の可視証拠を求めているため、unittestのstderr成功だけではfinishできません。"
                "成果物のprogrammatic APIまたはCLIを非対話で実行し、解答・到達状態・主要結果をstdoutに出してください。"
            )
            evaluation["next_required_action"] = "run the artifact or verifier and print the requested result to stdout"
        return evaluation
    sanity_issues = (
        visible_result_sanity_issues(
            user_message=user_message,
            stdout=str(evidence.get("last_success_stdout") or ""),
        )
        if "stdout_displayed" in contract
        else []
    )
    if sanity_issues:
        evaluation["status"] = "needs_revision"
        evaluation["semantic_status"] = "not_required"
        evaluation["missing"].append("visible_result_sanity_passed")
        evaluation["limitations"].extend(sanity_issues)
        evaluation["allowed_next_actions"] = [
            "run_command <non-interactive demo or verifier command that prints a meaningful visible result>"
        ]
        evaluation["suggested_fix"] = "表示されたstdoutが低情報量です。成果物の状態・結果・検証内容が分かるstdoutを出してください。"
        evaluation["next_required_action"] = "rerun the artifact or verifier with meaningful stdout"
        return evaluation
    if _final_answer_is_empty_tool_success(user_message=user_message, final_answer=final_answer, evidence=evidence):
        evaluation["status"] = "needs_revision"
        evaluation["semantic_status"] = "not_required"
        evaluation["missing"].append("meaningful_final_answer")
        evaluation["limitations"].append("successful command had no user-visible result; stdout/stderr/cwd success is evidence, not task completion")
        return evaluation
    if _final_answer_is_unrequested_environment_observation(self, user_message=user_message, final_answer=final_answer, evidence=evidence):
        evaluation["status"] = "needs_revision"
        evaluation["semantic_status"] = "not_required"
        evaluation["missing"].append("meaningful_final_answer")
        evaluation["limitations"].append("environment inspection output was not requested and does not satisfy the user goal")
        return evaluation
    if _final_answer_is_unsupported_by_environment_only_evidence(self, user_message=user_message, final_answer=final_answer, evidence=evidence):
        evaluation["status"] = "needs_revision"
        evaluation["semantic_status"] = "not_required"
        evaluation["missing"].append("meaningful_final_answer")
        evaluation["limitations"].append("only environment-inspection evidence is available; final answer must not claim task completion from that evidence")
        return evaluation
    if not _needs_finish_acceptance_review(user_message=user_message, steps=steps):
        evaluation["semantic_status"] = "not_required"
        return evaluation
    review = self._semantic_finish_acceptance_review(
        user_message=user_message,
        final_answer=final_answer,
        evidence_text=_finish_acceptance_evidence_text(steps),
    )
    evaluation["review"] = review
    review_status = str((review or {}).get("status") or "")
    if review_status == "success":
        evaluation["semantic_status"] = "reviewed"
        evaluation["status"] = "success"
    elif review_status == "needs_revision":
        evaluation["semantic_status"] = "needs_revision"
        evaluation["status"] = "needs_revision"
        evaluation["missing"].append("semantic_review_passed")
    elif review_status == "partial_success":
        evaluation["semantic_status"] = "partial_success"
        evaluation["status"] = "partial_success"
        evaluation["missing"].append("semantic_review_passed")
    else:
        fallback = _observation_acceptance_fallback(user_message=user_message, evidence=evidence, steps=steps)
        evaluation["fallback"] = fallback
        evaluation["semantic_status"] = "review_unavailable"
        if bool(fallback.get("ok")):
            # Acceptance override: judge verdict was not usable, but observable
            # contract was complete. Keep canonical status="success" and signal
            # the override via a dedicated field so the caller can emit a
            # decision event documenting why finish was accepted.
            evaluation["status"] = "success"
            evaluation["acceptance_override"] = {
                "reason_code": "judge_unavailable_observation_accepted",
                "evidence": fallback,
            }
            evaluation["limitations"].append("semantic review did not return a usable verdict; accepted by observable evidence fallback")
        else:
            evaluation["status"] = "partial_success"
            evaluation["missing"].append("semantic_review_passed")
            evaluation["limitations"].append("semantic review did not return a usable verdict")
    return evaluation


def _unittest_command_is_acceptance(command: str) -> bool:
    """Return whether a unittest command is the full acceptance suite.

    Narrow unittest targets are useful diagnostics after a timeout, but they
    must not satisfy the finish gate.
    """

    normalized = re.sub(r"\s+", " ", str(command or "").strip().lower())
    if " -m unittest " not in f" {normalized} ":
        return False
    if " discover" not in normalized:
        return False
    return bool(
        re.search(r"(?:^| )-s tests(?: |$)", normalized)
        or re.search(r"(?:^| )--start-directory(?:=| )tests(?: |$)", normalized)
    )


def _finish_acceptance_evidence(steps: list[dict[str, Any]]) -> dict[str, Any]:
    successful_edits = [
        step
        for step in steps
        if str(step.get("tool_name") or "") in {"write_file", "append_file", "replace_text"}
        and bool((step.get("tool_result") or {}).get("ok"))
    ]
    artifact_written = any(
        True
        for step in successful_edits
    )
    last_artifact_write_index = -1
    for index, step in enumerate(steps):
        if (
            str(step.get("tool_name") or "") in {"write_file", "append_file", "replace_text"}
            and bool((step.get("tool_result") or {}).get("ok"))
        ):
            last_artifact_write_index = index
    command_results = [
        step.get("tool_result") or {}
        for index, step in enumerate(steps)
        if str(step.get("tool_name") or "") == "run_command"
        and index > last_artifact_write_index
    ]
    total_command_count = sum(1 for step in steps if str(step.get("tool_name") or "") == "run_command")
    command_executed = any(bool(row.get("ok")) for row in command_results)
    stdout_displayed = any(
        stdout_looks_like_user_visible_result(str(row.get("stdout") or ""))
        for row in command_results
        if bool(row.get("ok"))
    )
    stderr_only = any(str(row.get("stderr") or "").strip() for row in command_results if bool(row.get("ok"))) and not stdout_displayed
    artifact_paths = [
        str((step.get("tool_result") or {}).get("path") or "")
        for step in successful_edits
    ]
    test_steps = [
        step
        for step in successful_edits
        if _artifact_path_is_test(str((step.get("tool_result") or {}).get("path") or ""))
    ]
    unittest_results = [
        row
        for row in command_results
        if "unittest" in str(row.get("command") or "").lower()
    ]
    acceptance_unittest_results = [
        row
        for row in unittest_results
        if _unittest_command_is_acceptance(str(row.get("command") or ""))
    ]
    return {
        "artifact_written": artifact_written,
        "python_artifact_written": any(_artifact_path_is_python_implementation(path) for path in artifact_paths),
        "tests_written": bool(test_steps),
        "meaningful_tests": any(_test_source_is_meaningful(_step_source_text(step)) for step in test_steps),
        "unittest_run": bool(acceptance_unittest_results),
        "unittest_passed": any(bool(row.get("ok")) for row in acceptance_unittest_results),
        "diagnostic_unittest_run": bool(unittest_results),
        "diagnostic_unittest_passed": any(bool(row.get("ok")) for row in unittest_results),
        "command_executed": command_executed,
        "stdout_displayed": stdout_displayed,
        "stderr_only": stderr_only,
        "artifact_paths": artifact_paths,
        "successful_commands": [
            str(row.get("command") or "")
            for row in command_results
            if bool(row.get("ok"))
        ],
        "last_success_stdout": next(
            (
                str(row.get("stdout") or "")
                for row in reversed(command_results)
                if bool(row.get("ok")) and str(row.get("stdout") or "").strip()
            ),
            "",
        ),
        "tool_successes": [
            str(step.get("tool_name") or "")
            for step in steps
            if bool((step.get("tool_result") or {}).get("ok"))
        ],
        "command_count": len(command_results),
        "total_command_count": total_command_count,
    }


def _finish_acceptance_contract(user_message: str) -> list[str]:
    text = str(user_message or "").lower()
    contract: list[str] = []
    execution_requested = any(marker in text for marker in ["実行", "run", "execute", "起動"])
    if any(marker in text for marker in ["作", "生成", "create", "write", "file", "ファイル", "コード"]):
        contract.append("artifact_written")
    if any(marker in text for marker in ["実装", "implement", "python", "solver", "ソルバー", "コード"]):
        contract.append("python_artifact_written")
    if any(marker in text for marker in ["unittest", "tests/", "テスト", "検証", "test"]):
        contract.extend(["tests_written", "meaningful_tests", "unittest_run", "unittest_passed"])
    if execution_requested:
        contract.append("command_executed")
    self_run_markers = ["自分で", "自ら", "クリア", "解く", "solve", "complete"]
    if any(marker in text for marker in ["標準出力", "stdout", "表示", "見せ", "display", "show"]) or (
        execution_requested and any(marker in text for marker in self_run_markers)
    ):
        contract.append("stdout_displayed")
    return list(dict.fromkeys(contract))


def _artifact_path_is_test(path: str) -> bool:
    normalized = str(path or "").replace("\\", "/")
    name = normalized.rsplit("/", 1)[-1]
    if name == "__init__.py":
        return False
    return normalized.startswith("tests/") and (name.startswith("test_") or name.endswith("_test.py"))


def _artifact_path_is_python_implementation(path: str) -> bool:
    normalized = str(path or "").replace("\\", "/")
    name = normalized.rsplit("/", 1)[-1]
    return (
        normalized.endswith(".py")
        and not _artifact_path_is_test(normalized)
        and not name.startswith("test_")
        and not name.endswith("_test.py")
        and name != "__init__.py"
    )


def _step_source_text(step: dict[str, Any]) -> str:
    tool_name = str(step.get("tool_name") or "")
    tool_args = step.get("tool_args") if isinstance(step.get("tool_args"), dict) else {}
    if tool_name in {"write_file", "append_file"}:
        return str(tool_args.get("content") or "")
    if tool_name == "replace_text":
        return str(tool_args.get("new_text") or "")
    return ""


def _test_source_is_meaningful(source: str) -> bool:
    """Return whether a unittest source contains executable assertions.

    This is a contract check, not a correctness proof. It prevents pass-only
    tests from satisfying "tests/unittest" requirements while keeping the
    runtime independent from task-specific domains.
    """
    text = str(source or "")
    if not text.strip():
        return False
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test"):
            continue
        body = [
            stmt
            for stmt in node.body
            if not (
                isinstance(stmt, ast.Pass)
                or (
                    isinstance(stmt, ast.Expr)
                    and isinstance(getattr(stmt, "value", None), ast.Constant)
                    and isinstance(stmt.value.value, str)
                )
            )
        ]
        if not body:
            continue
        for stmt in ast.walk(node):
            if isinstance(stmt, ast.Assert):
                return True
            if isinstance(stmt, ast.Call):
                func = stmt.func
                if isinstance(func, ast.Attribute) and func.attr.startswith("assert"):
                    return True
    return False


def _final_answer_is_empty_tool_success(*, user_message: str, final_answer: str, evidence: dict[str, Any]) -> bool:
    text = str(final_answer or "").strip().lower()
    if not text:
        return True
    if not bool(evidence.get("command_executed")):
        return False
    if bool(evidence.get("stdout_displayed")):
        return False
    empty_success_answers = {"success", "ok", "done", "完了", "成功"}
    if text in empty_success_answers:
        return True
    if text.startswith("cwd="):
        user_text = str(user_message or "").lower()
        if any(marker in user_text for marker in ["cwd", "pwd", "現在のディレクトリ", "作業ディレクトリ", "どのディレクトリ"]):
            return False
        return True
    if text.startswith("returncode=0") or text == "0":
        return True
    return False


def _final_answer_is_unrequested_environment_observation(self, *, user_message: str, final_answer: str, evidence: dict[str, Any]) -> bool:
    if not bool(evidence.get("command_executed")):
        return False
    user_text = str(user_message or "").lower()
    if any(marker in user_text for marker in ["cwd", "pwd", "現在のディレクトリ", "作業ディレクトリ", "どのディレクトリ"]):
        return False
    requested_commands = self._extract_requested_commands(user_message)
    if requested_commands:
        return False
    commands = [str(item or "").strip().lower() for item in evidence.get("successful_commands") or []]
    if not commands:
        return False
    environment_heads = {"ls", "find", "tree", "pwd"}
    if not all((command.split()[0] if command.split() else "") in environment_heads for command in commands):
        return False
    text = str(final_answer or "").strip()
    lower = text.lower()
    if lower.startswith("cwd="):
        return True
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if lines and lines[0].startswith("total "):
        return True
    if any(line.startswith(("drwx", "-rw", "lrwx")) for line in lines):
        return True
    return False


def _final_answer_is_unsupported_by_environment_only_evidence(self, *, user_message: str, final_answer: str, evidence: dict[str, Any]) -> bool:
    if bool(evidence.get("artifact_written")):
        return False
    user_text = str(user_message or "").lower()
    if any(marker in user_text for marker in ["cwd", "pwd", "現在のディレクトリ", "作業ディレクトリ", "どのディレクトリ"]):
        return False
    commands = [str(item or "").strip().lower() for item in evidence.get("successful_commands") or []]
    if not commands:
        return False
    environment_heads = {"ls", "find", "tree", "pwd"}
    if not all((command.split()[0] if command.split() else "") in environment_heads for command in commands):
        return False
    if self._extract_requested_commands(user_message):
        return False
    text = str(final_answer or "").strip().lower()
    if _final_answer_is_clarification_request(text):
        return False
    return True


def _final_answer_is_clarification_request(text: str) -> bool:
    if not text:
        return False
    clarification_markers = [
        "教えてください",
        "指定してください",
        "選んでください",
        "確認させてください",
        "どのよう",
        "何を",
        "具体的",
        "clarify",
        "please specify",
        "what would you like",
    ]
    return any(marker in text for marker in clarification_markers)


def _needs_finish_acceptance_review(*, user_message: str, steps: list[dict[str, Any]]) -> bool:
    text = str(user_message or "").lower()
    observable_result_markers = ["表示", "見せ", "出力", "display", "show", "画面"]
    if not any(marker in text for marker in observable_result_markers):
        return False
    if not any(str(step.get("tool_name") or "") in {"write_file", "append_file", "replace_text"} for step in steps):
        return False
    return any(str(step.get("tool_name") or "") == "run_command" for step in steps)


def _observation_acceptance_fallback(*, user_message: str, evidence: dict[str, Any], steps: list[dict[str, Any]]) -> dict[str, Any]:
    if not bool(evidence.get("artifact_written")):
        return {"ok": False, "reason": "missing_artifact_write"}
    if not bool(evidence.get("command_executed")):
        return {"ok": False, "reason": "missing_successful_command"}
    if not bool(evidence.get("stdout_displayed")):
        return {"ok": False, "reason": "missing_stdout"}
    return {"ok": True, "reason": "observable_evidence_complete"}


def _finish_acceptance_evidence_text(steps: list[dict[str, Any]]) -> str:
    compact: list[dict[str, Any]] = []
    for step in steps[-8:]:
        tool_name = str(step.get("tool_name") or "")
        tool_args = step.get("tool_args") if isinstance(step.get("tool_args"), dict) else {}
        result = step.get("tool_result") or {}
        if not isinstance(result, dict):
            result = {}
        source_preview = ""
        if tool_name in {"write_file", "append_file"}:
            source_preview = str(tool_args.get("content") or result.get("content") or "")[-3000:]
        elif tool_name == "replace_text":
            source_preview = str(tool_args.get("new_text") or result.get("new_text") or "")[-3000:]
        compact.append(
            {
                "tool_name": tool_name,
                "ok": bool(result.get("ok")),
                "path": result.get("path"),
                "command": result.get("command"),
                "returncode": result.get("returncode"),
                "source_preview": source_preview,
                "stdout": str(result.get("stdout") or "")[-3000:],
                "stderr": str(result.get("stderr") or "")[-1000:],
            }
        )
    return json.dumps(compact, ensure_ascii=False, indent=2)


def _semantic_finish_acceptance_review(self, *, user_message: str, final_answer: str, evidence_text: str) -> dict[str, Any]:
    model = str(self.router.models.get("fast") or "")
    if not model:
        raise RuntimeError("router.models['fast'] is not configured; cannot run finish acceptance judge")
    options = {"temperature": 0.1, "num_predict": 160}
    prompt = (
        "あなたは作業完了判定のレビュアーです。プログラムの完全な正当性証明は不要です。\n"
        "観測された evidence と user request を比較し、実行結果が要求物として成立しているかを判定してください。\n"
        "実行できたが成果物が要求と明らかに違う場合は needs_revision です。\n"
        "stdout があるだけでは success にしないでください。stdout の可視内容そのものを見て、要求された成果物に見えるか確認してください。\n"
        "コード生成タスクでは source_preview と実行stdoutの両方を見て、明らかな実装漏れや表示結果の不成立があれば needs_revision です。\n"
        "迷路・図・表などの可視成果物では、単なる反復パターンや未完成な形で、要求物として読めない場合は needs_revision です。\n"
        "判断不能だが観測上は作成・実行・表示されている場合は partial_success です。\n\n"
        f"User request:\n{user_message}\n\n"
        f"Evidence:\n{evidence_text[:8000]}\n\n"
        f"Final answer:\n{final_answer[:2000]}\n\n"
        "次のJSONだけを返してください。Markdownは禁止です。\n"
        "必須キーは status, reason_code, rationale, observed_mismatch です。\n"
        "success の場合も rationale に、なぜ要求物として成立していると判断したかを書いてください。\n"
        "observed_mismatch は不一致がなければ空文字、少しでも明確な不一致があればその内容を書いてください。\n"
        '例: {"status":"success","reason_code":"supported","rationale":"短い理由","observed_mismatch":""}'
    )
    trace = _chat_for_judge_with_repair(
        self,
        model=model,
        prompt=prompt,
        options=options,
        required_keys={"status"},
        response_schema=FINISH_ACCEPTANCE_SCHEMA,
        attempts=2,
    )
    parsed = trace.get("parsed") if isinstance(trace.get("parsed"), dict) else None
    status = str((parsed or {}).get("status") or "").strip()
    observed_mismatch = str((parsed or {}).get("observed_mismatch") or "").strip()
    if status == "success" and observed_mismatch:
        status = "needs_revision"
    if status not in {"success", "partial_success", "needs_revision"}:
        status = str(trace.get("decision") or "invalid_output")
    return {**trace, "decision": status, "status": status}


def _looks_like_evidence_required_task(user_message: str) -> bool:
    text = str(user_message or "").lower()
    evidence_required_markers = [
        "確認", "調べ", "見て", "読ん", "探し", "検索", "実行", "起動", "作成",
        "修正", "変更", "編集", "削除", "保存", "ファイル", "コード", "テスト",
        "dashboard", "ダッシュボード", "run", "execute", "create", "edit",
        "delete", "file", "code", "test", "check", "inspect", "search",
    ]
    return any(marker in text for marker in evidence_required_markers)


def _can_accept_general_knowledge_without_judge(*, user_message: str, evidence_text: str, final_answer: str) -> bool:
    if str(evidence_text or "").strip():
        return False
    answer = str(final_answer or "").strip()
    if not answer or answer.lower() in {"no model output was produced.", "task finished."}:
        return False
    return not _looks_like_evidence_required_task(user_message)


def _semantic_grounding_check(self, *, final_answer: str, evidence_text: str, user_message: str) -> bool:
    model = str(self.router.models.get("fast") or "")
    if not model:
        raise RuntimeError("router.models['fast'] is not configured; cannot run grounding judge")
    options = {"temperature": 0.1, "num_predict": 256}
    prompt = (
        "あなたは事実確認のエキスパートです。以下の証拠（Evidence）に基づき、提出された回答（Final Answer）が事実に基づいているか判定してください。\n\n"
        "【判定基準】\n"
        "- Evidence は runtime_facts / tool_facts / external_facts に分かれています。\n"
        "- runtime_facts は P4 自身の正本事実です。名前、役割、status など runtime 自身の事実はここで判定してください。\n"
        "- tool_facts は P4 が実際に実行・観測した結果です。ファイル、コマンド、出力はここで判定してください。\n"
        "- external_facts は tool_facts から得た外部環境やファイル内容の事実です。外部事実は runtime_facts で代用してはいけません。\n"
        "- 回答内の具体的な数値、名称、引用内容が証拠に含まれているか、あるいは証拠から論理的に導き出せる場合は合格です。\n"
        "- ユーザーの依頼文（User Message）に元々含まれている内容も合格です。\n"
        "- 証拠に全く存在しない新しい事実を捏造している場合は不合格（NG）です。\n"
        "- 表現が多少異なっていても、事実関係が合っていれば合格です。\n"
        "- ただし、ユーザーの依頼が一般的な知識、挨拶、創作、または推論のみで完結するタスクであり、環境状態の調査を必要としない場合は、Evidenceが空であっても自身の知識に合致していれば例外的に合格としてください。\n\n"
        f"【User Message】: {user_message}\n\n"
        f"【Evidence】:\n{evidence_text[:8000]}\n\n"
        f"【Final Answer】:\n{final_answer}\n\n"
        "次のJSONだけを返してください。Markdown、説明文、前置きは禁止です。\n"
        "必須キーは verdict (ok | ng のいずれか) のみです。\n"
        "reason_code, unsupported_claims, rationale は説明用で省略可、自由記述で構いません。\n"
        '例: {"verdict":"ok","reason_code":"supported","unsupported_claims":[],"rationale":"短い理由"}'
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
        trace = _chat_for_judge_with_repair(
            self,
            model=model,
            prompt=prompt,
            options=options,
            required_keys={"verdict"},
            response_schema=JUDGE_VERDICT_SCHEMA,
            attempts=2,
        )
        raw_content = str(trace.get("raw_response") or "")
        content_text = str(trace.get("content_text") or "")
        thinking_text = str(trace.get("thinking_text") or "")
        parse_source = content_text or raw_content
        parsed = trace.get("parsed") if isinstance(trace.get("parsed"), dict) else None
        verdict = str(parsed.get("verdict") or "").strip().lower() if parsed else ""
        if verdict == "ok":
            decision = "ok"
        elif verdict == "ng":
            decision = "ng"
        elif not parse_source.strip() and thinking_text.strip():
            decision = "empty_output"
        elif parsed is None:
            decision = str(trace.get("decision") or "invalid_json")
        else:
            decision = "invalid_output"
        accepted_by_fallback = (
            decision in {"invalid_output", "invalid_json", "empty_output"}
            and _can_accept_general_knowledge_without_judge(
                user_message=user_message,
                evidence_text=evidence_text,
                final_answer=final_answer,
            )
        )
        if accepted_by_fallback:
            decision = "general_knowledge_fallback"
        self._last_grounding_judge_trace = {
            **(self._last_grounding_judge_trace or {}),
            "response_model": trace.get("response_model", model),
            "raw_response": raw_content,
            "content_text": content_text,
            "thinking_text": thinking_text,
            "decision": decision,
            "parsed": parsed,
            "verdict": verdict,
            "judge_retry_count": int(trace.get("retry_count") or 0),
            "attempts": trace.get("attempts") or [],
            "fallback_reason": "evidence-free general knowledge/chat task" if accepted_by_fallback else "",
        }
        return decision in {"ok", "general_knowledge_fallback"}
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


def _chat_for_judge_with_repair(
    self,
    *,
    model: str,
    prompt: str,
    options: dict[str, Any],
    required_keys: set[str],
    response_schema: dict[str, Any],
    attempts: int = 2,
) -> dict[str, Any]:
    base_options = dict(options or {})
    base_options.setdefault("format", response_schema)
    base_options.setdefault("think", False)
    timeout_seconds = int(self.runtime_config.get("chat_timeout_seconds") or 180)
    attempt_rows: list[dict[str, Any]] = []
    last: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "options": dict(base_options),
        "decision": "not_run",
        "parsed": None,
    }
    for attempt_index in range(1, max(1, attempts) + 1):
        attempt_options = dict(base_options)
        attempt_prompt = prompt
        if attempt_index > 1:
            attempt_options["temperature"] = 0.0
            attempt_options["format"] = response_schema
            attempt_prompt = (
                f"{prompt}\n\n"
                "再試行です。JSONオブジェクトだけを返してください。"
                "前置き、説明、Markdown、thinking、コードブロックは禁止です。"
            )
        try:
            response = self.llm_backend.chat(
                model=model,
                messages=[{"role": "user", "content": attempt_prompt}],
                options=attempt_options,
                timeout_seconds=timeout_seconds,
            )
            raw_content = str(response.get("content") or "")
            content_text = str(response.get("content_text") or "")
            thinking_text = str(response.get("thinking_text") or "")
            parse_source = content_text or raw_content
            candidate = self._extract_json_object(parse_source)
            parsed: dict[str, Any] | None = None
            schema_validation = None
            raw_output_is_machine_json = candidate is not None and parse_source.strip() == candidate
            if candidate is not None:
                loaded = json.loads(candidate)
                schema_validation = validate_json_schema(loaded, response_schema) if isinstance(loaded, dict) else None
                if (
                    raw_output_is_machine_json
                    and
                    isinstance(loaded, dict)
                    and required_keys.issubset(set(loaded.keys()))
                    and schema_validation is not None
                    and schema_validation.ok
                ):
                    parsed = loaded
            validation_errors = list(schema_validation.errors) if schema_validation is not None else []
            decision = "ok" if parsed is not None else ("empty_output" if not parse_source.strip() else "invalid_json")
            if schema_validation is not None and not schema_validation.ok:
                decision = "invalid_output"
            row = {
                "attempt": attempt_index,
                "options": dict(attempt_options),
                "decision": decision,
                "schema_validation": {
                    "ok": bool(schema_validation.ok) if schema_validation is not None else False,
                    "errors": validation_errors,
                },
                "raw_output_is_machine_json": raw_output_is_machine_json,
                "schema_validation_ok": bool(schema_validation.ok) if schema_validation is not None else False,
                "raw_response": raw_content,
                "content_text": content_text,
                "thinking_text": thinking_text,
                "parsed": parsed,
                "response_model": response.get("model", model),
            }
            attempt_rows.append(row)
            last = {
                "model": model,
                "prompt": attempt_prompt,
                "options": dict(attempt_options),
                "decision": decision if parsed is None else "ok",
                "status": decision if parsed is None else "ok",
                "raw_response": raw_content,
                "content_text": content_text,
                "thinking_text": thinking_text,
                "parsed": parsed,
                "schema_validation": {
                    "ok": bool(schema_validation.ok) if schema_validation is not None else False,
                    "errors": validation_errors,
                },
                "raw_output_is_machine_json": raw_output_is_machine_json,
                "schema_validation_ok": bool(schema_validation.ok) if schema_validation is not None else False,
                "response_model": response.get("model", model),
                "attempts": list(attempt_rows),
                "retry_count": attempt_index - 1,
            }
            if parsed is not None:
                return last
        except Exception as exc:
            row = {
                "attempt": attempt_index,
                "options": dict(attempt_options),
                "decision": "error",
                "error": str(exc),
            }
            attempt_rows.append(row)
            if last.get("decision") in {"invalid_json", "empty_output", "invalid_output"}:
                last["attempts"] = list(attempt_rows)
                last["retry_count"] = attempt_index - 1
                return last
            last = {
                "model": model,
                "prompt": attempt_prompt,
                "options": dict(attempt_options),
                "decision": "error",
                "status": "error",
                "error": str(exc),
                "parsed": None,
                "attempts": list(attempt_rows),
                "retry_count": attempt_index - 1,
            }
    return last
