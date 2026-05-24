from __future__ import annotations

import ast
import re

from p4_core.implementation_contracts import python_call_name


def state_space_search_source_contract_issues(source: str) -> list[str]:
    """Return generic source issues for state-space-search PlanRecords."""

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
    solverish_tokens = ("solve", "solver", "search", "astar", "a_star", "bfs", "dfs", "plan", "path")
    verifierish_tokens = ("verify", "validator", "legal", "goal", "replay")
    replay_verifier_tokens = ("verify", "validate", "replay", "check_solution", "final_verifier")
    state_predicate_names = {"is_solved", "solved", "is_goal", "goal_reached", "is_goal_state"}
    has_solverish_callable = any(
        name not in state_predicate_names and any(token in name for token in solverish_tokens)
        for name in callable_names
    )
    has_verifierish_callable = any(any(token in name for token in verifierish_tokens) for name in callable_names)
    has_replay_verifier_callable = any(any(token in name for token in replay_verifier_tokens) for name in callable_names)
    has_manual_input_loop = bool(re.search(r"\binput\s*\(", text))
    nondeterministic_fixture_helpers = _state_space_nondeterministic_fixture_helpers(tree)

    issues: list[str] = []
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


def state_space_search_replay_verifier_repair_hints(
    *,
    source: str,
    issues: list[str],
) -> list[dict[str, str]]:
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
            "suggested_new_text": _state_space_search_replay_verifier_template(source),
        }
    ]


def state_space_search_test_contract_issues(
    test_sources: list[tuple[str, str]],
    *,
    plan_strategy: str,
) -> list[str]:
    """Return generic test-artifact issues for state-space-search plans."""

    if str(plan_strategy or "") != "state_space_search":
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
            calls = [python_call_name(call) for call in ast.walk(node) if isinstance(call, ast.Call)]
            has_solver_call = any(any(token in name for token in solverish_tokens) for name in calls if name)
            if not has_solver_call:
                continue
            solver_test_seen = True
            test_source = ast.get_source_segment(str(source or ""), node) or ""
            test_source_lower = test_source.lower()
            solver_result_names: set[str] = set()
            solver_execution_calls: list[ast.Call] = []
            for child in ast.walk(node):
                if isinstance(child, ast.Assign) and isinstance(child.value, ast.Call) and _state_space_is_solver_execution_call(child.value):
                    solver_execution_calls.append(child.value)
                    for target in child.targets:
                        if isinstance(target, ast.Name):
                            solver_result_names.add(target.id)
                elif isinstance(child, ast.Call) and _state_space_is_solver_execution_call(child):
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
            has_replay_or_verifier = any(any(token in name for token in verifierish_tokens) for name in calls if name)
            if has_replay_or_verifier:
                replaying_solver_test_seen = True
            no_solution_named_test = any(
                token in node.name.lower()
                for token in ("unsolvable", "no_solution", "no_path", "unreachable", "impossible")
            )
            no_solution_assert = any(
                isinstance(child, ast.Call)
                and _state_space_assertion_expects_none_from_solver(child, solver_result_names)
                for child in ast.walk(node)
            )
            if (no_solution_named_test or no_solution_assert) and solver_execution_calls:
                if not any(_state_space_solver_call_has_explicit_bound(call, test_source_lower) for call in solver_execution_calls):
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
                        for name in _state_space_target_names(target)
                    ]
                    if (
                        names
                        and any(any(token in name for token in action_fixture_name_tokens) for name in names)
                        and _state_space_is_handwritten_action_sequence_literal(child.value)
                    ):
                        size = _state_space_literal_container_len(child.value)
                        label = "/".join(names[:3])
                        handwritten_action_fixture_tests.append(
                            f"{path}:{node.name}:line {getattr(child, 'lineno', '?')}:{label}[{size}]"
                        )
                    if (
                        names
                        and any("near" in name and "goal" in name for name in names)
                        and isinstance(child.value, (ast.List, ast.Tuple))
                    ):
                        literal_near_goal_fixture_tests.append(f"{path}:{node.name}:line {getattr(child, 'lineno', '?')}")
                    if near_goal_claimed and names and _state_space_is_small_literal_action_item(child.value):
                        if any(any(token in name for token in start_fixture_name_tokens) for name in names):
                            literal_start_lines.append(getattr(child, "lineno", "?"))
                        if any(any(token in name for token in goal_fixture_name_tokens) for name in names):
                            literal_goal_lines.append(getattr(child, "lineno", "?"))
                elif isinstance(child, ast.Call):
                    name = python_call_name(child)
                    if not any(token in name for token in verifierish_tokens):
                        continue
                    for arg in child.args:
                        if _state_space_is_handwritten_action_sequence_literal(arg):
                            handwritten_action_fixture_tests.append(
                                f"{path}:{node.name}:line {getattr(child, 'lineno', '?')}:"
                                f"verifier_arg[{_state_space_literal_container_len(arg)}]"
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


def _state_space_nondeterministic_fixture_helpers(tree: ast.AST) -> list[str]:
    helpers: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        function_name = node.name.lower()
        if not any(token in function_name for token in ("fixture", "near_goal", "near", "scramble")):
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Import):
                if any(alias.name == "random" or alias.name.startswith("random.") for alias in child.names):
                    helpers.append(node.name)
                    break
            if isinstance(child, ast.ImportFrom) and child.module == "random":
                helpers.append(node.name)
                break
            if isinstance(child, ast.Call):
                func = child.func
                if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "random":
                    helpers.append(node.name)
                    break
                if isinstance(func, ast.Name) and func.id in {"choice", "sample", "shuffle", "randint", "randrange"}:
                    helpers.append(node.name)
                    break
    return helpers


def _state_space_search_preferred_symbol(
    names: set[str],
    preferred: tuple[str, ...],
    tokens: tuple[str, ...],
) -> str:
    for name in preferred:
        if name in names:
            return name
    for name in sorted(names):
        lowered = name.lower()
        if tokens and any(token in lowered for token in tokens):
            return name
    return ""


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


def _state_space_search_replay_verifier_template(source: str) -> str:
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
            legal_method = _state_space_search_preferred_symbol(
                method_names,
                ("is_valid_move", "is_legal_move", "validate_move", "get_legal_moves", "legal_moves", "legal_actions"),
                ("legal", "valid", "validate", "moves", "actions"),
            )
            transition_method = _state_space_search_preferred_symbol(
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
                legal_check = _state_space_search_legal_check_lines(
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
        legal_function = _state_space_search_preferred_symbol(
            function_names,
            ("is_valid_move", "is_legal_move", "validate_move", "get_legal_moves", "legal_moves", "legal_actions"),
            ("legal", "valid", "validate", "moves", "actions"),
        )
        transition_function = _state_space_search_preferred_symbol(
            function_names,
            ("make_move", "apply_move", "apply_action", "transition", "next_state"),
            ("apply", "transition", "next_state", "make_move"),
        )
        goal_function = _state_space_search_preferred_symbol(
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
            legal_check = _state_space_search_legal_check_lines(legal_function, indent="        ")
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


def _state_space_is_solver_execution_call(call: ast.Call) -> bool:
    name = python_call_name(call)
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


def _state_space_solver_call_has_explicit_bound(call: ast.Call, test_source_lower: str) -> bool:
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


def _state_space_assertion_expects_none_from_solver(call: ast.Call, solver_result_names: set[str]) -> bool:
    if python_call_name(call) not in {"assertisnone", "assert_is_none"}:
        return False
    if not call.args:
        return False
    target = call.args[0]
    if isinstance(target, ast.Name) and target.id in solver_result_names:
        return True
    return isinstance(target, ast.Call) and _state_space_is_solver_execution_call(target)


def _state_space_target_names(target: ast.AST) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        names: list[str] = []
        for item in target.elts:
            names.extend(_state_space_target_names(item))
        return names
    return []


def _state_space_literal_container_len(node: ast.AST) -> int:
    if isinstance(node, (ast.List, ast.Tuple)):
        return len(node.elts)
    return 0


def _state_space_is_small_literal_action_item(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, (str, int, float, bool, type(None)))
    if isinstance(node, (ast.List, ast.Tuple)):
        return all(_state_space_is_small_literal_action_item(item) for item in node.elts)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        return _state_space_is_small_literal_action_item(node.operand)
    return False


def _state_space_is_handwritten_action_sequence_literal(node: ast.AST) -> bool:
    if not isinstance(node, (ast.List, ast.Tuple)) or len(node.elts) < 4:
        return False
    return all(_state_space_is_small_literal_action_item(item) for item in node.elts)


