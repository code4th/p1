from __future__ import annotations

import unittest

from p4_core.unittest_repair import (
    AllowedAction,
    UnittestRepairActionPolicy,
    build_unittest_failed_allowed_actions,
)


def policy(**overrides: object) -> UnittestRepairActionPolicy:
    values = {
        "failed_unittest_noop_blocked_paths": (),
        "failed_unittest_noop_alternate_paths": (),
        "latest_read_paths_after_failed_unittest": frozenset(),
        "failed_unittest_no_match_write_only_paths": (),
        "failed_unittest_unread_paths": (),
        "failed_unittest_recovery_editable_paths": (),
        "failed_unittest_recovery_read_paths": (),
        "same_signature_nonreducing_edit_paths": (),
        "repeated_unittest_failure_signature": False,
        "state_space_no_match_exact_replace_paths": (),
        "state_space_fixture_value_impl_blocked_paths": (),
        "latest_test_path": "",
        "latest_impl_path": "",
    }
    values.update(overrides)
    return UnittestRepairActionPolicy(**values)


class UnittestRepairActionPolicyTests(unittest.TestCase):
    def test_allowed_action_legacy_text(self) -> None:
        self.assertEqual(AllowedAction("read_file", "tests/test_app.py").legacy_text(), "read_file tests/test_app.py once")
        self.assertEqual(
            AllowedAction("replace_text", "app.py", "with a small unique old_text").legacy_text(),
            "replace_text app.py with a small unique old_text",
        )
        self.assertEqual(AllowedAction("write_file", "app.py").legacy_text(), "write_file app.py")

    def test_unread_paths_take_priority(self) -> None:
        result = policy(
            failed_unittest_unread_paths=("tests/test_app.py", "app.py"),
            failed_unittest_recovery_read_paths=("tests/test_app.py", "app.py"),
        ).legacy_allowed_next_actions()

        self.assertEqual(result, ["read_file tests/test_app.py once", "read_file app.py once"])

    def test_state_space_fixture_impl_block_removes_impl_target(self) -> None:
        result = policy(
            failed_unittest_recovery_read_paths=("tests/test_app.py", "app.py"),
            state_space_fixture_value_impl_blocked_paths=("app.py",),
        ).legacy_allowed_next_actions()

        self.assertEqual(
            result,
            [
                "replace_text tests/test_app.py with a small unique old_text",
                "write_file tests/test_app.py",
            ],
        )

    def test_legacy_wrapper_preserves_action_strings(self) -> None:
        result = build_unittest_failed_allowed_actions(
            failed_unittest_noop_blocked_paths=[],
            failed_unittest_noop_alternate_paths=[],
            latest_read_paths_after_failed_unittest=set(),
            failed_unittest_no_match_write_only_paths=["tests/test_app.py"],
            failed_unittest_unread_paths=[],
            failed_unittest_recovery_editable_paths=["tests/test_app.py", "app.py"],
            failed_unittest_recovery_read_paths=["tests/test_app.py", "app.py"],
            same_signature_nonreducing_edit_paths=["app.py"],
            repeated_unittest_failure_signature=True,
            state_space_no_match_exact_replace_paths=["app.py"],
            state_space_fixture_value_impl_blocked_paths=[],
            latest_test_path="tests/test_app.py",
            latest_impl_path="app.py",
        )

        self.assertEqual(
            result,
            [
                "replace_text app.py with a small unique old_text",
                "write_file tests/test_app.py",
            ],
        )


if __name__ == "__main__":
    unittest.main()
