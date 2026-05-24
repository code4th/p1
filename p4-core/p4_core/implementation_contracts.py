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


def _assignment_targets(node: ast.Assign | ast.AnnAssign | ast.AugAssign) -> list[ast.expr]:
    if isinstance(node, ast.Assign):
        return list(node.targets)
    return [node.target]


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
