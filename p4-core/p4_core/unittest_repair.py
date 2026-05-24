from __future__ import annotations

import re
from dataclasses import dataclass


def _looks_like_test_path(path: str) -> bool:
    normalized = str(path or "").replace("\\", "/")
    name = normalized.rsplit("/", 1)[-1]
    return normalized.startswith("tests/") or "/tests/" in normalized or name.startswith("test_")


def unittest_failure_signature_body(output: str) -> str:
    """Extract the semantic failure body and ignore stdout/demo noise."""

    interesting: list[str] = []
    for raw_line in str(output or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith(("FAIL:", "ERROR:")):
            interesting.append(line)
            continue
        if line.startswith("File "):
            interesting.append(line)
            continue
        if re.search(r"\bself\.assert[A-Za-z0-9_]*\b|\bassert[A-Z][A-Za-z0-9_]*\b", line):
            interesting.append(line)
            continue
        if re.match(
            r"^(AssertionError|SyntaxError|ImportError|ModuleNotFoundError|NameError|TypeError|ValueError|"
            r"IndexError|KeyError|AttributeError|RuntimeError|Exception|Error):",
            line,
        ):
            interesting.append(line)
    return "\n".join(interesting) if interesting else str(output or "")


def unittest_output_looks_like_test_value_assertion(output: str) -> bool:
    text = str(output or "")
    if "AssertionError" not in text:
        return False
    value_failure_markers = (
        " != ",
        "Lists differ:",
        "Tuples differ:",
        "Dictionaries differ:",
        "not equal",
        "False is not true",
        "True is not false",
        "unexpectedly None",
        "not raised",
    )
    return any(marker in text for marker in value_failure_markers)


def unittest_output_return_shape_hint(output: str) -> str:
    text = str(output or "")
    if "AssertionError" not in text or " != " not in text:
        return ""

    examples: list[str] = []
    for match in re.finditer(r"AssertionError:\s*([^\n]+?)\s+!=\s+([^\n]+)", text):
        left = match.group(1).strip()
        right = match.group(2).strip()
        shapes = {_unittest_value_shape(left), _unittest_value_shape(right)}
        if shapes == {"scalar", "container"}:
            examples.append(f"{left} != {right}")
        if len(examples) >= 3:
            break
    if not examples:
        return ""

    return (
        "返却shape/API契約不一致の疑いがあります。unittestがscalar値とsequence/containerを比較しています"
        f"（例: {'; '.join(examples)}）。"
        "アルゴリズム本体を書き換える前に、公開関数のdocstring/仕様、test側のunpack順序、"
        "reference/brute_force/oracle helperの戻り値shapeを同一契約に揃えてください。"
        "関数がtupleを返す場合は、実装・test・oracleのすべてで戻り値の順序を一致させてください。"
    )


def unittest_output_missing_name_details(output: str) -> dict[str, str]:
    """Extract a NameError symbol and traceback location from unittest output."""

    text = str(output or "")
    if not text.strip():
        return {}
    name_match = re.search(r"NameError:\s+name ['\"]([^'\"]+)['\"] is not defined", text)
    if not name_match:
        return {}

    source_file = ""
    source_line = ""
    source_scope = ""
    file_matches = list(re.finditer(r'File "([^"]+\.py)", line (\d+), in ([^\n]+)', text))
    if file_matches:
        latest = file_matches[-1]
        source_file = latest.group(1).replace("\\", "/").rsplit("/", 1)[-1]
        source_line = latest.group(2)
        source_scope = latest.group(3).strip()
    return {
        "name": name_match.group(1),
        "source_file": source_file,
        "source_line": source_line,
        "source_scope": source_scope,
    }


def _unittest_value_shape(value: str) -> str:
    stripped = value.strip()
    if stripped.startswith(("[", "(", "{")):
        return "container"
    if re.match(r"^[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?$", stripped):
        return "scalar"
    if stripped in {"True", "False", "None"}:
        return "scalar"
    if len(stripped) >= 2 and stripped[0] in {"'", '"'} and stripped[-1] == stripped[0]:
        return "scalar"
    return "unknown"


@dataclass(frozen=True)
class AllowedAction:
    tool: str
    path: str
    mode: str = ""

    def legacy_text(self) -> str:
        if self.tool == "read_file":
            return f"read_file {self.path} once"
        if self.tool == "replace_text" and self.mode:
            return f"replace_text {self.path} {self.mode}"
        return f"{self.tool} {self.path}"


@dataclass(frozen=True)
class UnittestRepairActionPolicy:
    failed_unittest_noop_blocked_paths: tuple[str, ...]
    failed_unittest_noop_alternate_paths: tuple[str, ...]
    latest_read_paths_after_failed_unittest: frozenset[str]
    failed_unittest_no_match_write_only_paths: tuple[str, ...]
    failed_unittest_unread_paths: tuple[str, ...]
    failed_unittest_recovery_editable_paths: tuple[str, ...]
    failed_unittest_recovery_read_paths: tuple[str, ...]
    same_signature_nonreducing_edit_paths: tuple[str, ...]
    repeated_unittest_failure_signature: bool
    state_space_no_match_exact_replace_paths: tuple[str, ...]
    state_space_fixture_value_impl_blocked_paths: tuple[str, ...]
    latest_test_path: str
    latest_impl_path: str
    test_fixture_value_impl_blocked_paths: tuple[str, ...] = ()
    return_shape_contract_mismatch: bool = False

    def allowed_actions(self) -> list[AllowedAction]:
        if self.failed_unittest_noop_blocked_paths:
            return self._noop_recovery_actions()
        if self.failed_unittest_unread_paths:
            return [
                AllowedAction("read_file", target)
                for target in self.failed_unittest_unread_paths
            ]
        if self.failed_unittest_no_match_write_only_paths:
            return self._no_match_recovery_actions()
        return self._default_recovery_actions()

    def legacy_allowed_next_actions(self) -> list[str]:
        return [action.legacy_text() for action in self.allowed_actions()]

    def _noop_recovery_actions(self) -> list[AllowedAction]:
        noop_unread_paths = [
            item
            for item in self.failed_unittest_noop_alternate_paths
            if item not in self.latest_read_paths_after_failed_unittest
        ]
        if noop_unread_paths:
            return [AllowedAction("read_file", target) for target in noop_unread_paths]
        noop_editable_paths = [
            item
            for item in self.failed_unittest_noop_alternate_paths
            if item in self.latest_read_paths_after_failed_unittest
        ]
        actions: list[AllowedAction] = []
        for target in noop_editable_paths or self.failed_unittest_noop_alternate_paths:
            if target in self.failed_unittest_no_match_write_only_paths:
                actions.append(AllowedAction("write_file", target))
            else:
                actions.append(AllowedAction("replace_text", target, "with a small unique old_text"))
                actions.append(AllowedAction("write_file", target))
        return actions

    def _no_match_recovery_actions(self) -> list[AllowedAction]:
        same_signature_nonreducing_edit_path_set = (
            set(self.same_signature_nonreducing_edit_paths)
            if self.repeated_unittest_failure_signature
            else set()
        )
        blocked_paths = set(self.state_space_fixture_value_impl_blocked_paths) | set(
            self.test_fixture_value_impl_blocked_paths
        )
        if self.return_shape_contract_mismatch:
            actions = [
                AllowedAction("replace_text", target, "with a small unique old_text")
                for target in self.failed_unittest_recovery_editable_paths
                if target not in blocked_paths
            ]
            actions.extend(
                AllowedAction("write_file", target)
                for target in self.failed_unittest_no_match_write_only_paths
                if _looks_like_test_path(target) and target not in blocked_paths
            )
            return actions
        actions = [
            AllowedAction("replace_text", target, "with a small unique old_text")
            for target in self.failed_unittest_recovery_editable_paths
            if target in same_signature_nonreducing_edit_path_set
            or target in self.state_space_no_match_exact_replace_paths
        ]
        actions.extend(
            AllowedAction("write_file", target)
            for target in self.failed_unittest_no_match_write_only_paths
        )
        return actions

    def _default_recovery_actions(self) -> list[AllowedAction]:
        targets = [
            target
            for target in (
                list(self.failed_unittest_recovery_read_paths)
                or [self.latest_test_path or "tests/test_*.py", self.latest_impl_path or "<implementation>.py"]
            )
            if target not in self.state_space_fixture_value_impl_blocked_paths
            and target not in self.test_fixture_value_impl_blocked_paths
        ]
        nonreducing_paths = (
            set(self.same_signature_nonreducing_edit_paths)
            if self.repeated_unittest_failure_signature
            else set()
        )
        if self.return_shape_contract_mismatch:
            actions = [
                AllowedAction("replace_text", target, "with a small unique old_text")
                for target in targets
            ]
            actions.extend(
                AllowedAction("write_file", target)
                for target in targets
                if _looks_like_test_path(target) and target not in nonreducing_paths
            )
            return actions
        actions = [
            AllowedAction("replace_text", target, "with a small unique old_text")
            for target in targets
        ]
        actions.extend(
            AllowedAction("write_file", target)
            for target in targets
            if target not in nonreducing_paths
        )
        return actions


def _policy_from_legacy_args(
    *,
    failed_unittest_noop_blocked_paths: list[str],
    failed_unittest_noop_alternate_paths: list[str],
    latest_read_paths_after_failed_unittest: set[str],
    failed_unittest_no_match_write_only_paths: list[str],
    failed_unittest_unread_paths: list[str],
    failed_unittest_recovery_editable_paths: list[str],
    failed_unittest_recovery_read_paths: list[str],
    same_signature_nonreducing_edit_paths: list[str],
    repeated_unittest_failure_signature: bool,
    state_space_no_match_exact_replace_paths: list[str],
    state_space_fixture_value_impl_blocked_paths: list[str],
    latest_test_path: str,
    latest_impl_path: str,
    test_fixture_value_impl_blocked_paths: list[str] | None = None,
    return_shape_contract_mismatch: bool = False,
) -> UnittestRepairActionPolicy:
    return UnittestRepairActionPolicy(
        failed_unittest_noop_blocked_paths=tuple(failed_unittest_noop_blocked_paths),
        failed_unittest_noop_alternate_paths=tuple(failed_unittest_noop_alternate_paths),
        latest_read_paths_after_failed_unittest=frozenset(latest_read_paths_after_failed_unittest),
        failed_unittest_no_match_write_only_paths=tuple(failed_unittest_no_match_write_only_paths),
        failed_unittest_unread_paths=tuple(failed_unittest_unread_paths),
        failed_unittest_recovery_editable_paths=tuple(failed_unittest_recovery_editable_paths),
        failed_unittest_recovery_read_paths=tuple(failed_unittest_recovery_read_paths),
        same_signature_nonreducing_edit_paths=tuple(same_signature_nonreducing_edit_paths),
        repeated_unittest_failure_signature=bool(repeated_unittest_failure_signature),
        state_space_no_match_exact_replace_paths=tuple(state_space_no_match_exact_replace_paths),
        state_space_fixture_value_impl_blocked_paths=tuple(state_space_fixture_value_impl_blocked_paths),
        test_fixture_value_impl_blocked_paths=tuple(test_fixture_value_impl_blocked_paths or []),
        latest_test_path=str(latest_test_path or ""),
        latest_impl_path=str(latest_impl_path or ""),
        return_shape_contract_mismatch=bool(return_shape_contract_mismatch),
    )


def build_unittest_failed_allowed_actions(
    *,
    failed_unittest_noop_blocked_paths: list[str],
    failed_unittest_noop_alternate_paths: list[str],
    latest_read_paths_after_failed_unittest: set[str],
    failed_unittest_no_match_write_only_paths: list[str],
    failed_unittest_unread_paths: list[str],
    failed_unittest_recovery_editable_paths: list[str],
    failed_unittest_recovery_read_paths: list[str],
    same_signature_nonreducing_edit_paths: list[str],
    repeated_unittest_failure_signature: bool,
    state_space_no_match_exact_replace_paths: list[str],
    state_space_fixture_value_impl_blocked_paths: list[str],
    latest_test_path: str,
    latest_impl_path: str,
    test_fixture_value_impl_blocked_paths: list[str] | None = None,
    return_shape_contract_mismatch: bool = False,
) -> list[str]:
    """Return the canonical action surface for unittest_failed_needs_fix."""
    return _policy_from_legacy_args(
        failed_unittest_noop_blocked_paths=failed_unittest_noop_blocked_paths,
        failed_unittest_noop_alternate_paths=failed_unittest_noop_alternate_paths,
        latest_read_paths_after_failed_unittest=latest_read_paths_after_failed_unittest,
        failed_unittest_no_match_write_only_paths=failed_unittest_no_match_write_only_paths,
        failed_unittest_unread_paths=failed_unittest_unread_paths,
        failed_unittest_recovery_editable_paths=failed_unittest_recovery_editable_paths,
        failed_unittest_recovery_read_paths=failed_unittest_recovery_read_paths,
        same_signature_nonreducing_edit_paths=same_signature_nonreducing_edit_paths,
        repeated_unittest_failure_signature=repeated_unittest_failure_signature,
        state_space_no_match_exact_replace_paths=state_space_no_match_exact_replace_paths,
        state_space_fixture_value_impl_blocked_paths=state_space_fixture_value_impl_blocked_paths,
        test_fixture_value_impl_blocked_paths=test_fixture_value_impl_blocked_paths,
        latest_test_path=latest_test_path,
        latest_impl_path=latest_impl_path,
        return_shape_contract_mismatch=return_shape_contract_mismatch,
    ).legacy_allowed_next_actions()
