import unittest

from p4_core.progress_block import ProgressBlockDecision


class ProgressBlockDecisionTests(unittest.TestCase):
    def test_event_details_and_messages_share_the_same_source(self) -> None:
        decision = ProgressBlockDecision.from_phase_block(
            tool_name="replace_text",
            tool_args={"path": "solver.py"},
            phase_block={
                "message": "old_text did not match",
                "reason_code": "implementation_task_replace_no_match",
                "allowed_next_actions": ["read_file solver.py once"],
                "suggested_fix": "read the current source",
                "current_source_excerpt": "def solve():\n    return None",
                "state": {"phase": "unittest_failed_needs_fix"},
            },
        )

        self.assertEqual(decision.reason_code, "implementation_task_replace_no_match")
        self.assertEqual(decision.path, "solver.py")
        self.assertIn("current_source_excerpt for the next exact old_text", decision.visible_message())
        self.assertEqual(decision.status_stream_text().count("current_source_excerpt"), 1)
        self.assertEqual(
            decision.event_details()["allowed_next_actions"],
            ["read_file solver.py once"],
        )
        self.assertEqual(
            decision.event_details()["current_source_excerpt"],
            "def solve():\n    return None",
        )

    def test_defaults_preserve_legacy_block_shape(self) -> None:
        decision = ProgressBlockDecision.from_phase_block(
            tool_name="write_file",
            tool_args={"path": "impl.py"},
            phase_block={"message": "not allowed yet"},
        )

        self.assertEqual(decision.reason_code, "implementation_task_phase_blocked")
        self.assertEqual(decision.path, "impl.py")
        self.assertEqual(decision.blocked_by, "implementation_task_progress_controller")
        self.assertEqual(decision.failure_type, "implementation_task_progress_blocked")
        self.assertEqual(decision.event_details()["allowed_next_actions"], [])


if __name__ == "__main__":
    unittest.main()
