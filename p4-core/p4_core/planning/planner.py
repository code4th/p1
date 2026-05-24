from __future__ import annotations

import re
import uuid
from typing import Any

from p4_core.schemas import FIRST_ACTION_CONTENT_MAX_LENGTH


PLANNING_STRATEGIES = (
    "direct_implementation",
    "task_decomposition",
    "state_space_search",
    "dynamic_programming",
    "constraint_satisfaction",
    "graph_shortest_path",
    "classical_planning",
)

PLAN_REVISION_FAILURE_TYPES = {
    "command_timeout",
    "repeated_command_failure",
    "blocked_action_ignored",
    "implementation_contract_nonreducing_edit_loop",
    "work_package_invalid",
    "plan_work_unit_embedded_edit",
    "plan_work_unit_too_large",
    "plan_work_unit_invalid_python",
    "plan_work_unit_placeholder",
    "plan_work_units_invalid",
    "plan_record_invalid",
    "plan_scope_incomplete",
    "planner_strategy_mismatch",
    "state_space_search_plan_missing_verifier",
    "state_space_search_plan_missing_execution_verifier",
    "dynamic_programming_plan_missing_verifier",
    "dynamic_programming_plan_missing_execution_verifier",
    "constraint_satisfaction_plan_missing_verifier",
    "constraint_satisfaction_plan_missing_execution_verifier",
    "plan_execution_validation_failed",
}

STATE_SPACE_REQUIRED_CONTRACT = (
    "state",
    "action",
    "goal",
    "legal_move_validator",
    "solvability_check",
    "heuristic_or_search_policy",
    "final_verifier",
)

DYNAMIC_PROGRAMMING_REQUIRED_CONTRACT = (
    "subproblem_state",
    "base_case_verifier",
    "recurrence_verifier",
    "evaluation_order_or_memoization",
    "sample_oracle",
)

CONSTRAINT_SATISFACTION_REQUIRED_CONTRACT = (
    "variables",
    "domains",
    "constraints",
    "constraint_checker",
    "solution_validator",
    "negative_case",
)

PLAN_FIRST_ACTION_CONTENT_BYTES = FIRST_ACTION_CONTENT_MAX_LENGTH

PLAN_RECORD_SHAPE_HINT = (
    'Expected create_plan shape: {"tool_name":"create_plan","tool_args":{"plan":{'
    '"plan_id":"plan-id","profile":{...},"strategy":"<same as profile.strategy for specialized strategies>",'
    '"work_units":[{"unit_id":"unit-1","goal":"concrete goal","depends_on":[],'
    '"work_type":"inspect|edit|run_test|search","first_action":{"tool":"list_files","args":{"path":"."}},'
    '"success_evidence":"observable success evidence","should_open_child_frame":true}],'
    '"verification_contract":["state","action","goal","legal_move_validator","solvability_check","heuristic_or_search_policy","final_verifier"],'
    '"status":"proposed","revision_count":0}}}. '
    "PlanRecord first_action must be one of list_files/read_file/search_code/run_command; do not embed write_file/append_file/replace_text in create_plan. "
    "For edit WorkUnits, first_action must inspect with list_files/read_file/search_code, not a no-op run_command such as echo. "
    "For state_space_search, include a run_test WorkUnit whose goal/success_evidence verifies a legal action path/replay reaches the goal; a vague 'tests pass' WorkUnit is not enough. "
    "State-space tests must be deterministic and bounded; prefer a near-goal fixture over random/shuffle/full-search fixtures. "
    "For dynamic_programming, include subproblem state, base-case verifier, recurrence verifier, evaluation order or memoization, sample oracle, and a run_test WorkUnit that verifies base cases plus recurrence/sample cases. "
    "For constraint_satisfaction, include variables, domains, constraints, constraint checker, solution validator, negative case, and a run_test WorkUnit that validates a good assignment and rejects an invalid assignment. "
    "run_test first_action must be non-interactive verification such as python3 -m unittest discover -s tests or python3 -c that calls the programmatic verifier; do not use direct python script.py. "
    "Do not put WorkUnit fields such as goal, work_type, success_evidence, should_open_child_frame, "
    "why_not_direct_action, context_summary, or done_when inside first_action. first_action may contain only tool and args. "
    f"create_plan is a concise plan, not a full implementation dump; any first_action text field must be <= {PLAN_FIRST_ACTION_CONTENT_BYTES} UTF-8 bytes. "
    "Implementation edits happen after the child frame opens in PLAN_EXECUTION."
)


def _contains_any(text: str, markers: tuple[str, ...]) -> list[str]:
    return [marker for marker in markers if marker in text]


def _has_word(text: str, marker: str) -> bool:
    return bool(re.search(rf"(?<![a-z0-9_]){re.escape(marker)}(?![a-z0-9_])", text))


def _state_space_profile_match(text: str) -> tuple[bool, list[str], list[str]]:
    """Classify state-space planning without letting generic search terms dominate.

    A word like "探索" or "search" is too weak by itself: binary search,
    code search, and other named algorithms are not state-space planning
    problems. State-space contracts are justified only when the request names
    a state/action/goal model or a puzzle-like domain.
    """

    explicit_markers = (
        "状態空間",
        "状態探索",
        "state space",
        "state-space",
        "合法手",
        "legal move",
        "パズル",
        "puzzle",
    )
    model_markers = (
        "状態",
        "ゴール",
        "手順",
        "手数",
        "手を",
        "moves",
        "move sequence",
        "state",
        "goal",
    )
    search_markers = (
        "探索方針",
        "探索",
        "経路",
        "最短",
        "path",
        "shortest",
        "search",
        "bfs",
        "a*",
        "ida*",
    )

    explicit = _contains_any(text, explicit_markers)
    model = _contains_any(text, model_markers)
    search = _contains_any(text, search_markers)
    strong_enough = bool(explicit) or (len(model) >= 2 and bool(search))
    signals = [f"state_space:{item}" for item in (explicit + model + search)[:8]]
    weak_signals = [f"search_weak:{item}" for item in search[:4]]
    return strong_enough, signals, weak_signals


def profile_problem(user_message: str) -> dict[str, Any]:
    """Derive a generic planning profile from the user's request.

    This is intentionally heuristic. The runtime owns the gate and evidence;
    the LLM still owns the actual plan content.
    """

    original = str(user_message or "")
    text = original.lower()
    signals: list[str] = []

    dp_markers = (
        "動的計画",
        "dp",
        "漸化式",
        "部分問題",
        "最適値",
        "dp表",
        "テーブル",
        "最適部分構造",
        "memoization",
        "subproblem",
        "recurrence",
        "state transition",
        "transition table",
        "weighted interval scheduling",
    )
    strong_constraint_markers = (
        "制約",
        "割当",
        "充足",
        "constraint",
        "constraint satisfaction",
        "assignment",
        "csp",
    )
    weak_constraint_markers = (
        "scheduling",
        "schedule",
    )
    graph_shortest_path_markers = (
        "最短経路",
        "shortest path",
        "dijkstra",
        "astar",
        "a*",
    )
    graph_generic_markers = (
        "グラフ",
        "network",
        "graph",
        "トポロジカル",
        "topological sort",
        "toposort",
    )
    classical_markers = (
        "pddl",
        "古典計画",
        "前提条件",
        "効果",
        "operator",
        "precondition",
        "effect",
    )
    decomposition_markers = (
        "分解",
        "複数ステップ",
        "長期",
        "計画",
        "plan",
        "multi-step",
        "decompose",
        "refactor",
        "設計",
    )

    strategy = "direct_implementation"
    state_space_matched, state_space_signals, weak_search_signals = _state_space_profile_match(text)
    if state_space_matched:
        strategy = "state_space_search"
        signals.extend(state_space_signals)
    else:
        signals.extend(weak_search_signals)
    matched = _contains_any(text, graph_generic_markers)
    if matched:
        signals.extend(f"graph:{item}" for item in matched[:6])
    matched = _contains_any(text, graph_shortest_path_markers)
    if matched:
        strategy = "graph_shortest_path"
        signals.extend(f"graph_shortest_path:{item}" for item in matched[:6])
    matched = _contains_any(text, dp_markers)
    if matched:
        strategy = "dynamic_programming"
        signals.extend(f"dynamic_programming:{item}" for item in matched[:6])
    matched = _contains_any(text, strong_constraint_markers)
    if matched:
        strategy = "constraint_satisfaction"
        signals.extend(f"constraint:{item}" for item in matched[:6])
    weak_matched = _contains_any(text, weak_constraint_markers)
    if weak_matched:
        signals.extend(f"constraint_weak:{item}" for item in weak_matched[:4])
        if strategy in {"direct_implementation", "task_decomposition"}:
            strategy = "constraint_satisfaction"
    matched = _contains_any(text, classical_markers)
    if matched:
        strategy = "classical_planning"
        signals.extend(f"classical_planning:{item}" for item in matched[:6])
    matched = _contains_any(text, decomposition_markers)
    if matched and strategy == "direct_implementation":
        strategy = "task_decomposition"
        signals.extend(f"task_decomposition:{item}" for item in matched[:6])

    implementation_markers = ("実装", "作って", "作成", "implement", "create", "build")
    if any(marker in text for marker in implementation_markers):
        signals.append("implementation_request")

    simple_one_file = (
        strategy == "direct_implementation"
        and len(original) < 180
        and not any(marker in text for marker in ("テスト", "unittest", "複数", "algorithm", "アルゴリズム"))
    )
    complexity = "simple" if simple_one_file else "complex" if strategy != "direct_implementation" or len(original) >= 240 else "moderate"

    state_model = ""
    constraints: list[str] = []
    verification_requirements: list[str] = []
    if strategy == "state_space_search":
        state_model = "explicit states, legal actions, goal test, transition function"
        constraints = ["state must be serializable", "actions must be legal from the current state", "search must terminate or be bounded"]
        verification_requirements = list(STATE_SPACE_REQUIRED_CONTRACT)
    elif strategy == "dynamic_programming":
        state_model = "subproblem state and recurrence"
        constraints = ["base cases", "recurrence", "iteration or memoization order"]
        verification_requirements = list(DYNAMIC_PROGRAMMING_REQUIRED_CONTRACT)
    elif strategy == "constraint_satisfaction":
        state_model = "variables, domains, constraints, assignment"
        constraints = ["all constraints must be checked independently", "invalid assignments must be rejected"]
        verification_requirements = list(CONSTRAINT_SATISFACTION_REQUIRED_CONTRACT)
    elif strategy == "graph_shortest_path":
        state_model = "nodes, edges, weights or uniform transitions"
        constraints = ["edge validity", "path cost", "start and goal existence"]
        verification_requirements = ["path_validator", "cost_or_reachability_check"]
    elif strategy == "classical_planning":
        state_model = "objects, predicates, actions, preconditions, effects, goal"
        constraints = ["preconditions", "effects", "goal satisfaction"]
        verification_requirements = ["plan_step_validator", "goal_state_verifier"]
    elif strategy == "task_decomposition":
        state_model = "ordered work units with observable evidence"
        verification_requirements = ["work_unit_success_evidence", "final_integration_check"]

    return {
        "complexity": complexity,
        "signals": list(dict.fromkeys(signals)),
        "strategy": strategy,
        "state_model": state_model,
        "constraints": constraints,
        "verification_requirements": verification_requirements,
    }


def planning_required_for_profile(profile: dict[str, Any]) -> bool:
    strategy = str((profile or {}).get("strategy") or "")
    complexity = str((profile or {}).get("complexity") or "")
    return complexity == "complex" and strategy != "direct_implementation"


def _contract_values(contract: Any) -> set[str]:
    if isinstance(contract, dict):
        values = set()
        for key, value in contract.items():
            if bool(value):
                values.add(str(key))
        return values
    if isinstance(contract, list):
        return {str(item) for item in contract}
    return set()


def validate_plan_record_contract(plan: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    strategy = str((plan or {}).get("strategy") or "").strip()
    profile = plan.get("profile") if isinstance(plan.get("profile"), dict) else {}
    profile_strategy = str(profile.get("strategy") or "").strip()
    if strategy not in PLANNING_STRATEGIES:
        issues.append(f"strategy must be one of {list(PLANNING_STRATEGIES)}")
    specialized_profile = profile_strategy not in {"", "direct_implementation", "task_decomposition"}
    if specialized_profile and strategy != profile_strategy:
        issues.append(
            f"planner_strategy_mismatch: plan.strategy must match profile.strategy {profile_strategy!r}, got {strategy!r}"
        )
    work_units = plan.get("work_units")
    if not isinstance(work_units, list) or not work_units:
        issues.append("work_units must contain at least one WorkUnit")
    else:
        seen: set[str] = set()
        for index, unit in enumerate(work_units, start=1):
            if not isinstance(unit, dict):
                issues.append(f"work_units[{index}] must be an object")
                continue
            unit_id = str(unit.get("unit_id") or "").strip()
            if not unit_id:
                issues.append(f"work_units[{index}].unit_id is required")
            elif unit_id in seen:
                issues.append(f"work_units[{index}].unit_id must be unique")
            seen.add(unit_id)
            first_action = unit.get("first_action") if isinstance(unit.get("first_action"), dict) else {}
            if not str(first_action.get("tool") or "").strip():
                issues.append(f"work_units[{index}].first_action.tool is required")
            if not isinstance(first_action.get("args"), dict):
                issues.append(f"work_units[{index}].first_action.args must be an object")
            if not str(unit.get("success_evidence") or "").strip():
                issues.append(f"work_units[{index}].success_evidence is required")
    present = _contract_values(plan.get("verification_contract"))
    effective_strategy = strategy or profile_strategy
    if effective_strategy == "state_space_search" or profile_strategy == "state_space_search":
        missing = [item for item in STATE_SPACE_REQUIRED_CONTRACT if item not in present]
        if missing:
            issues.append("state_space_search_plan_missing_verifier: missing " + ", ".join(missing))
        if not _has_strategy_execution_verifier(
            work_units,
            required_groups=(
                (
                    "final_verifier",
                    "verifier",
                    "verify",
                    "validated",
                    "検証",
                    "確認",
                    "replay",
                    "再生",
                ),
                (
                    "legal",
                    "合法",
                    "valid move",
                    "action",
                    "actions",
                    "move sequence",
                    "moves",
                    "transition",
                    "手順",
                    "手",
                ),
                ("goal", "ゴール", "final state", "到達", "解"),
            ),
        ):
            issues.append(
                "state_space_search_plan_missing_execution_verifier: "
                "state_space_search requires a run_test WorkUnit whose goal/success_evidence verifies legal action replay to the goal"
            )
    if effective_strategy == "dynamic_programming" or profile_strategy == "dynamic_programming":
        missing = [item for item in DYNAMIC_PROGRAMMING_REQUIRED_CONTRACT if item not in present]
        if missing:
            issues.append("dynamic_programming_plan_missing_verifier: missing " + ", ".join(missing))
        if not _has_strategy_execution_verifier(
            work_units,
            required_groups=(
                ("base", "base case", "base_case", "基底", "初期"),
                ("recurrence", "漸化", "transition", "遷移", "sample", "oracle", "期待値"),
            ),
        ):
            issues.append(
                "dynamic_programming_plan_missing_execution_verifier: "
                "dynamic_programming requires a run_test WorkUnit whose goal/success_evidence verifies base cases and recurrence/sample oracle cases"
            )
    if effective_strategy == "constraint_satisfaction" or profile_strategy == "constraint_satisfaction":
        missing = [item for item in CONSTRAINT_SATISFACTION_REQUIRED_CONTRACT if item not in present]
        if missing:
            issues.append("constraint_satisfaction_plan_missing_verifier: missing " + ", ".join(missing))
        if not _has_strategy_execution_verifier(
            work_units,
            required_groups=(
                ("constraint", "constraints", "制約"),
                ("validator", "validate", "check", "検証", "確認"),
                ("negative", "invalid", "reject", "不正", "失敗"),
            ),
        ):
            issues.append(
                "constraint_satisfaction_plan_missing_execution_verifier: "
                "constraint_satisfaction requires a run_test WorkUnit whose goal/success_evidence validates a satisfying assignment and rejects an invalid/negative assignment"
            )
    return issues


def _has_strategy_execution_verifier(
    work_units: Any,
    *,
    required_groups: tuple[tuple[str, ...], ...],
) -> bool:
    for unit in work_units if isinstance(work_units, list) else []:
        if not isinstance(unit, dict):
            continue
        if str(unit.get("work_type") or "").strip() != "run_test":
            continue
        text = " ".join(
            str(unit.get(key) or "")
            for key in ("goal", "success_evidence", "done_when", "context_summary")
        ).lower()
        if all(any(marker in text for marker in group) for group in required_groups):
            return True
    return False


def plan_record_to_work_packages(plan: dict[str, Any]) -> list[dict[str, Any]]:
    strategy = str(plan.get("strategy") or "")
    profile = plan.get("profile") if isinstance(plan.get("profile"), dict) else {}
    packages: list[dict[str, Any]] = []
    for index, unit in enumerate(plan.get("work_units") or [], start=1):
        if not isinstance(unit, dict):
            continue
        unit_id = str(unit.get("unit_id") or "").strip() or f"unit-{index}"
        first_action = unit.get("first_action") if isinstance(unit.get("first_action"), dict) else {}
        packages.append(
            {
                "task_id": unit_id,
                "goal": str(unit.get("goal") or "").strip(),
                "work_type": str(unit.get("work_type") or "inspect").strip(),
                "first_action": {
                    "tool": str(first_action.get("tool") or "").strip(),
                    "args": dict(first_action.get("args") or {}) if isinstance(first_action.get("args"), dict) else {},
                },
                "success_evidence": str(unit.get("success_evidence") or "").strip(),
                "why_not_direct_action": (
                    str(unit.get("why_not_direct_action") or "").strip()
                    or "PlanRecord requires this WorkUnit to run under the existing child-frame contract."
                ),
                "context_summary": (
                    str(unit.get("context_summary") or "").strip()
                    or f"planner_strategy={strategy}; profile_strategy={profile.get('strategy') or ''}"
                ),
                "done_when": str(unit.get("done_when") or unit.get("success_evidence") or "").strip(),
                "planner_strategy": strategy,
                "depends_on": list(unit.get("depends_on") or []) if isinstance(unit.get("depends_on"), list) else [],
                "should_open_child_frame": bool(unit.get("should_open_child_frame", True)),
            }
        )
    return packages


def plan_requires_revision(recent_events: list[dict[str, Any]], steps: list[dict[str, Any]]) -> bool:
    return bool(plan_revision_reasons(recent_events=recent_events, steps=steps))


def plan_revision_reasons(recent_events: list[dict[str, Any]], steps: list[dict[str, Any]]) -> list[str]:
    reasons: list[str] = []
    for step in reversed(steps[-4:]):
        result = step.get("tool_result") if isinstance(step.get("tool_result"), dict) else {}
        if str(result.get("failure_type") or "") in PLAN_REVISION_FAILURE_TYPES:
            reasons.append(str(result.get("failure_type") or ""))
        stderr = str(result.get("stderr") or result.get("error") or "").lower()
        if "timed out" in stderr or "timeout" in stderr:
            reasons.append("command_timeout")
    for event in reversed(recent_events[-12:]):
        details = event.get("details") if isinstance(event.get("details"), dict) else {}
        reason = str(event.get("reason_code") or details.get("reason_code") or "")
        failure_type = str(details.get("failure_type") or "")
        if reason in PLAN_REVISION_FAILURE_TYPES or failure_type in PLAN_REVISION_FAILURE_TYPES:
            reasons.append(failure_type or reason)
    return list(dict.fromkeys(reason for reason in reasons if reason))


def new_plan_id() -> str:
    return f"plan-{uuid.uuid4().hex[:12]}"
