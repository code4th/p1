from __future__ import annotations

from dataclasses import dataclass


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
        ]
        nonreducing_paths = (
            set(self.same_signature_nonreducing_edit_paths)
            if self.repeated_unittest_failure_signature
            else set()
        )
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
        latest_test_path=str(latest_test_path or ""),
        latest_impl_path=str(latest_impl_path or ""),
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
        latest_test_path=latest_test_path,
        latest_impl_path=latest_impl_path,
    ).legacy_allowed_next_actions()
