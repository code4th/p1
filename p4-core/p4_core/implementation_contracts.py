from __future__ import annotations

import ast
import re
from typing import Iterable


DYNAMIC_PROGRAMMING_SOLVERISH_TOKENS = (
    "solve",
    "compute",
    "count",
    "optimize",
    "optimal",
    "best",
    "dp",
    "memo",
    "tabulate",
)


DYNAMIC_PROGRAMMING_ORACLE_TOKENS = (
    "brute",
    "bruteforce",
    "exhaust",
    "reference",
    "oracle",
    "naive",
    "slow",
)


IGNORED_TEST_IMPORT_MODULES = {
    "collections",
    "dataclasses",
    "functools",
    "itertools",
    "math",
    "pathlib",
    "random",
    "typing",
    "unittest",
}


def python_call_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id.lower()
    if isinstance(func, ast.Attribute):
        return func.attr.lower()
    return ""


def dynamic_programming_callable_names(source: str, *, requested_names: Iterable[str] = ()) -> set[str]:
    """Return callable names that can satisfy a generic DP implementation contract.

    The runtime should not require domain functions to be named solve_* or
    compute_*. A function named weighted_interval_scheduling is a valid
    programmatic callable if its body exposes base cases and DP state updates.
    """

    text = str(source or "")
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()

    requested = {str(name).lower() for name in requested_names if str(name).strip()}
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        name = str(node.name).lower()
        if name in requested or any(token in name for token in DYNAMIC_PROGRAMMING_SOLVERISH_TOKENS):
            names.add(name)
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and function_has_dynamic_programming_structure(
            source=text,
            node=node,
        ):
            names.add(name)
    return names


def function_has_dynamic_programming_structure(*, source: str, node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    segment = ast.get_source_segment(source, node) or ""
    lowered = segment.lower()
    has_return = any(isinstance(child, ast.Return) for child in ast.walk(node))
    has_loop = any(isinstance(child, (ast.For, ast.While)) for child in ast.walk(node))
    has_recursive_call = any(
        isinstance(child, ast.Call)
        and isinstance(child.func, ast.Name)
        and child.func.id == node.name
        for child in ast.walk(node)
    )
    has_indexed_state_assignment = any(
        isinstance(child, (ast.Assign, ast.AnnAssign, ast.AugAssign))
        and any(isinstance(target, ast.Subscript) for target in _assignment_targets(child))
        for child in ast.walk(node)
    )
    has_memo_or_table = any(
        token in lowered
        for token in ("memo", "cache", "lru_cache", "dp[", "table", "tabulat", "dynamic programming")
    )
    has_base_case = bool(
        re.search(r"\b(base|base_case|initial|seed)\b", lowered)
        or any(_if_node_looks_like_base_case(child) for child in ast.walk(node) if isinstance(child, ast.If))
    )
    has_transition = bool(
        re.search(r"\b(recurrence|transition|subproblem|state transition)\b", lowered)
        or has_recursive_call
        or (has_loop and has_indexed_state_assignment)
        or has_memo_or_table
    )
    return has_return and has_base_case and has_transition


def test_calls_implementation_callable(
    test_sources: list[tuple[str, str]],
    *,
    requested_names: Iterable[str] = (),
) -> bool:
    """Return whether tests directly call a likely production callable.

    This intentionally recognizes imported module callables such as
    `from weighted_interval_scheduling import weighted_interval_scheduling`.
    That is stronger evidence than a name heuristic and avoids requiring
    wrapper functions purely to satisfy runtime naming preferences.
    """

    requested = {str(name).lower() for name in requested_names if str(name).strip()}
    for _path, source in test_sources:
        text = str(source or "")
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        imported_callables = _imported_implementation_callables(tree)
        candidates = set(requested) | imported_callables
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or not node.name.startswith("test"):
                continue
            calls = {python_call_name(call) for call in ast.walk(node) if isinstance(call, ast.Call)}
            calls.discard("")
            if calls & candidates:
                return True
            if any(any(token in name for token in DYNAMIC_PROGRAMMING_SOLVERISH_TOKENS) for name in calls):
                return True
    return False


def dynamic_programming_independent_oracle_seen(
    test_sources: list[tuple[str, str]],
    *,
    requested_names: Iterable[str] = (),
) -> bool:
    """Return whether tests contain a small independent DP oracle/reference.

    A hardcoded expected value is useful as a fixture, but it is not an
    independent oracle. The runtime cannot know every algorithm's truth table,
    so DP tests should include a simple reference/brute-force helper for small
    inputs. The helper must not call the implementation under test.
    """

    requested = {str(name).lower() for name in requested_names if str(name).strip()}
    for _path, source in test_sources:
        text = str(source or "")
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        implementation_callables = requested | _imported_implementation_callables(tree)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            name = str(node.name).lower()
            if name.startswith("test"):
                continue
            if not any(token in name for token in DYNAMIC_PROGRAMMING_ORACLE_TOKENS):
                continue
            calls = {python_call_name(call) for call in ast.walk(node) if isinstance(call, ast.Call)}
            calls.discard("")
            if calls & implementation_callables:
                continue
            has_return = any(isinstance(child, ast.Return) for child in ast.walk(node))
            has_control_flow = any(isinstance(child, (ast.For, ast.While, ast.If)) for child in ast.walk(node))
            if has_return and has_control_flow:
                return True
    return False


def dynamic_programming_source_contract_issues(
    source: str,
    *,
    requested_names: Iterable[str] = (),
) -> list[str]:
    """Return generic source issues for dynamic-programming PlanRecords."""

    text = str(source or "")
    if not text.strip():
        return []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []

    requested = {str(name).lower() for name in requested_names if str(name).strip()}
    has_solverish_callable = bool(dynamic_programming_callable_names(text, requested_names=requested))
    lowered = text.lower()
    has_memo_or_table = any(
        token in lowered
        for token in ("memo", "cache", "lru_cache", "dp[", "table", "tabulat", "dynamic programming")
    )
    has_indexed_state_assignment = any(
        isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign))
        and any(isinstance(target, ast.Subscript) for target in _assignment_targets(node))
        for node in ast.walk(tree)
    )
    has_recursive_call = False
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name) and child.func.id == node.name:
                has_recursive_call = True
                break
        if has_recursive_call:
            break

    has_base_case = bool(
        re.search(r"\b(base|base_case|initial|seed)\b", lowered)
        or "基底" in text
        or "初期" in text
        or any(_assignment_initializes_indexed_base_case(node) for node in ast.walk(tree) if isinstance(node, ast.Assign))
        or any(_if_node_looks_like_base_case(node) for node in ast.walk(tree) if isinstance(node, ast.If))
    )
    has_recurrence_or_transition = bool(
        re.search(r"\b(recurrence|transition|subproblem|state transition)\b", lowered)
        or "漸化" in text
        or "遷移" in text
        or has_recursive_call
        or has_memo_or_table
        or has_indexed_state_assignment
    )

    issues: list[str] = []
    if not has_solverish_callable:
        issues.append(
            "PlanRecord strategy=dynamic_programming ですが、subproblemを計算するprogrammatic callableが見つかりません。"
            "ユーザー指定のtop-level API、またはDP表やmemoizationを実行して結果を返すsolve/compute/count/optimize系callableを実装してください。"
        )
    if not has_base_case:
        issues.append(
            "dynamic_programming PlanRecordのbase_case_verifier義務に対し、base caseまたは初期状態を表す実装経路が観測できません。"
            "最小subproblemの戻り値、初期DP表、またはbase caseを明示してください。"
        )
    if not has_recurrence_or_transition:
        issues.append(
            "dynamic_programming PlanRecordのrecurrence_verifier義務に対し、recurrence/transition/memoization/table更新が観測できません。"
            "部分問題から次状態を導く漸化式またはDP表更新を実装してください。"
        )
    return issues


def dynamic_programming_test_contract_issues(
    test_sources: list[tuple[str, str]],
    *,
    plan_strategy: str,
    requested_names: Iterable[str] = (),
) -> list[str]:
    """Return generic test-artifact issues for dynamic-programming plans."""

    if str(plan_strategy or "") != "dynamic_programming":
        return []

    requested = {str(name).lower() for name in requested_names if str(name).strip()}
    assertion_names = {
        "assertequal",
        "assertnotequal",
        "asserttrue",
        "assertfalse",
        "assertisnone",
        "assertisnotnone",
        "assertgreater",
        "assertgreaterequal",
        "assertless",
        "assertlessequal",
    }
    solver_test_seen = test_calls_implementation_callable(test_sources, requested_names=requested)
    independent_oracle_seen = dynamic_programming_independent_oracle_seen(test_sources, requested_names=requested)
    assertion_count = 0
    base_seen = False
    recurrence_or_sample_seen = False
    for _path, source in test_sources:
        try:
            tree = ast.parse(str(source or ""))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test"):
                continue
            test_source = ast.get_source_segment(str(source or ""), node) or ""
            lowered = test_source.lower()
            calls = [python_call_name(call) for call in ast.walk(node) if isinstance(call, ast.Call)]
            assertion_count += sum(1 for name in calls if name in assertion_names)
            if (
                any(token in lowered for token in ("base", "base_case", "initial"))
                or any(token in test_source for token in ("基底", "初期", "期待値"))
            ):
                base_seen = True
            if (
                any(token in lowered for token in ("recurrence", "transition", "subproblem", "memo", "table", "dp", "sample", "oracle"))
                or any(token in test_source for token in ("漸化", "遷移", "部分問題"))
                or any(
                    any(token in name for token in DYNAMIC_PROGRAMMING_ORACLE_TOKENS)
                    for name in calls
                    if name
                )
            ):
                recurrence_or_sample_seen = True

    issues: list[str] = []
    if not solver_test_seen:
        issues.append(
            "dynamic_programming test artifact が solver/compute callableを直接呼んでいません。"
            "PlanRecordの実行証拠として、DP本体を呼び出し期待値と比較するunittestを追加してください。"
        )
    if assertion_count < 2 or not base_seen or not recurrence_or_sample_seen:
        issues.append(
            "dynamic_programming test artifact は base case と recurrence/sample oracle の両方を観測可能に検証していません。"
            "最小subproblemの期待値と、漸化式またはDP表更新で導かれるsampleケースを別々にassertしてください。"
        )
    if not independent_oracle_seen:
        issues.append(
            "dynamic_programming test artifact が小さい入力用の独立reference/brute-force oracleを持っていません。"
            "runtimeは自己生成hardcoded expectedを正本扱いしないため、実装本体を呼ばない"
            "brute_force/reference/oracle helperで期待値を導出し、その結果とDP本体を比較してください。"
        )
    return issues


def test_artifact_contract_guidance(*, issue_text: str, plan_strategy: str) -> dict[str, str]:
    """Return prompt/action guidance derived from the same test issue."""

    issue = str(issue_text or "")
    strategy = str(plan_strategy or "")
    if strategy == "dynamic_programming" and "独立reference/brute-force oracle" in issue:
        guidance = (
            "tests/test_*.py に、実装本体を呼ばない brute_force/reference/oracle helper を追加してください。"
            "小さい入力だけを対象に全列挙または単純な基準実装で期待値を導出し、"
            "その結果とDP本体の戻り値を比較するunittestにしてください。"
        )
        return {
            "suggested_fix": guidance,
            "next_required_action": (
                "rewrite the test artifact with an independent brute_force/reference/oracle helper "
                "that does not call the implementation under test"
            ),
        }
    if strategy == "dynamic_programming":
        guidance = (
            "base case と recurrence/sample oracle を別々にassertし、"
            "DP本体のprogrammatic callableを直接呼ぶunittestにしてください。"
        )
        return {
            "suggested_fix": guidance,
            "next_required_action": "rewrite the test artifact so it directly validates the DP callable contract",
        }
    if strategy == "constraint_satisfaction":
        guidance = (
            "solverの戻り値をconstraint checker / solution validatorへ渡し、"
            "valid assignment成功とinvalid/negative assignment拒否をassertするunittestにしてください。"
        )
        return {
            "suggested_fix": guidance,
            "next_required_action": "rewrite the test artifact around a constraint validator and negative fixture",
        }
    guidance = (
        "solver/searchの戻り値をlegal move replayまたはfinal_verifierへ渡し、goal到達をassertするunittestにしてください。"
    )
    return {
        "suggested_fix": guidance,
        "next_required_action": "rewrite the test artifact around legal replay or final_verifier evidence",
    }


def constraint_satisfaction_source_contract_issues(source: str) -> list[str]:
    """Return generic source issues for constraint-satisfaction PlanRecords."""

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
    solverish_tokens = ("solve", "search", "assign", "backtrack", "satisfy", "csp")
    checker_tokens = ("constraint", "consistent", "valid", "validate", "check", "satisf")
    solution_tokens = ("solution", "assignment", "validate", "verify", "check")
    has_solverish_callable = any(any(token in name for token in solverish_tokens) for name in callable_names)
    has_constraint_checker = any(any(token in name for token in checker_tokens) for name in callable_names)
    has_solution_validator = any(any(token in name for token in solution_tokens) for name in callable_names)
    lowered = text.lower()
    has_constraint_model = any(
        token in lowered
        for token in ("variables", "domains", "constraints", "assignment", "constraint")
    ) or any(token in text for token in ("変数", "ドメイン", "制約", "割当"))

    issues: list[str] = []
    if not has_solverish_callable:
        issues.append(
            "PlanRecord strategy=constraint_satisfaction ですが、assignmentを探索するprogrammatic solver/search/backtrack callableが見つかりません。"
            "変数・ドメイン・制約から候補割当を生成するsolve/search/assign系callableを実装してください。"
        )
    if not has_constraint_model:
        issues.append(
            "constraint_satisfaction PlanRecordのvariables/domains/constraints義務に対し、制約モデルがsource上で観測できません。"
            "変数、ドメイン、制約を入力または内部モデルとして明示してください。"
        )
    if not has_constraint_checker:
        issues.append(
            "constraint_satisfaction PlanRecordのconstraint_checker義務に対し、各制約を検査するcheck/valid/constraint系callableが不足しています。"
            "候補割当が制約を満たすかを独立に判定する経路を実装してください。"
        )
    if not has_solution_validator:
        issues.append(
            "constraint_satisfaction PlanRecordのsolution_validator義務に対し、最終assignment全体を検証するvalidate/verify/check_solution系callableが不足しています。"
            "solverの戻り値を受け取り、完全性と全制約充足を確認する検証経路を実装してください。"
        )
    return issues


def constraint_satisfaction_test_contract_issues(
    test_sources: list[tuple[str, str]],
    *,
    plan_strategy: str,
) -> list[str]:
    """Return generic test-artifact issues for constraint-satisfaction plans."""

    if str(plan_strategy or "") != "constraint_satisfaction":
        return []

    solverish_tokens = ("solve", "search", "assign", "backtrack", "satisfy", "csp")
    validator_tokens = ("constraint", "consistent", "valid", "validate", "verify", "check", "satisf")
    solver_test_seen = False
    validator_test_seen = False
    negative_case_seen = False
    for _path, source in test_sources:
        try:
            tree = ast.parse(str(source or ""))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test"):
                continue
            test_source = ast.get_source_segment(str(source or ""), node) or ""
            lowered = test_source.lower()
            calls = [python_call_name(call) for call in ast.walk(node) if isinstance(call, ast.Call)]
            if any(any(token in name for token in solverish_tokens) for name in calls if name):
                solver_test_seen = True
            if any(any(token in name for token in validator_tokens) for name in calls if name):
                validator_test_seen = True
            if (
                any(token in lowered for token in ("negative", "invalid", "reject", "unsatisfied", "violate"))
                or any(token in test_source for token in ("不正", "違反", "失敗", "拒否"))
                or any(name in {"assertfalse", "assertraises", "assertisnone"} for name in calls)
            ):
                negative_case_seen = True

    issues: list[str] = []
    if not solver_test_seen:
        issues.append(
            "constraint_satisfaction test artifact が solver/search/backtrack callableを直接呼んでいません。"
            "PlanRecordの実行証拠として、候補assignmentを生成するテストを追加してください。"
        )
    if not validator_test_seen:
        issues.append(
            "constraint_satisfaction test artifact が constraint checker / solution validator を直接呼んでいません。"
            "solverの戻り値を検証経路へ渡し、全制約充足をassertしてください。"
        )
    if not negative_case_seen:
        issues.append(
            "constraint_satisfaction test artifact が invalid/negative case を検証していません。"
            "制約違反のassignmentをrejectできることをassertしてください。"
        )
    return issues


def _assignment_targets(node: ast.Assign | ast.AnnAssign | ast.AugAssign) -> list[ast.expr]:
    if isinstance(node, ast.Assign):
        return list(node.targets)
    return [node.target]


def _assignment_initializes_indexed_base_case(node: ast.Assign) -> bool:
    if not any(isinstance(target, ast.Subscript) for target in node.targets):
        return False
    slice_text = ast.unparse(node.targets[0].slice) if hasattr(ast, "unparse") else ""
    return bool(re.search(r"\b0\b|\b1\b", slice_text))


def _if_node_looks_like_base_case(node: ast.If) -> bool:
    if not any(isinstance(child, ast.Return) for child in ast.walk(node)):
        return False
    condition_text = ast.unparse(node.test) if hasattr(ast, "unparse") else ""
    lowered = condition_text.lower()
    return bool(re.search(r"\b0\b|\b1\b|\bnot\b", lowered) or "len(" in lowered)


def _imported_implementation_callables(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        module_root = str(node.module or "").split(".", 1)[0].lower()
        if not module_root or module_root in IGNORED_TEST_IMPORT_MODULES or module_root.startswith("test"):
            continue
        for alias in node.names:
            if alias.name == "*":
                continue
            exposed = alias.asname or alias.name
            if exposed and not exposed.startswith("_"):
                names.add(exposed.lower())
    return names
