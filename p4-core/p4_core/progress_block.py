"""Typed rendering for implementation progress blocks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class ProgressBlockDecision:
    """A prompt-visible progress block with one event/status source of truth."""

    tool_name: str
    message: str
    reason_code: str
    path: str
    block_signature: str
    current_source_excerpt: str
    phase: str
    route_phase: str
    blocked_by: str
    failure_type: str
    allowed_next_actions: tuple[str, ...]
    suggested_fix: str
    next_required_action: str
    missing_requirements: tuple[Any, ...]
    candidate_missing_requirements: tuple[Any, ...]
    repair_hints: tuple[Any, ...]
    state: Any
    latest_edit_match_failure_recovery: Any
    broad_rewrite: bool
    block_header_only_replace: bool
    exact_old_text_matches: Any
    fixture_repair_mode: bool
    fixture_review_items: Any
    repeated_semantic_issue: Any
    terminal_failure: bool

    @classmethod
    def from_phase_block(
        cls,
        *,
        tool_name: str,
        tool_args: Mapping[str, Any],
        phase_block: Mapping[str, Any],
    ) -> "ProgressBlockDecision":
        message = str(phase_block.get("message") or "")
        suggested_fix = str(phase_block.get("suggested_fix") or "")
        return cls(
            tool_name=str(tool_name),
            message=message,
            reason_code=str(phase_block.get("reason_code") or "implementation_task_phase_blocked"),
            path=str(phase_block.get("path") or tool_args.get("path") or ""),
            block_signature=str(phase_block.get("block_signature") or ""),
            current_source_excerpt=str(phase_block.get("current_source_excerpt") or "").strip(),
            phase=str(phase_block.get("phase") or ""),
            route_phase=str(phase_block.get("route_phase") or ""),
            blocked_by=str(phase_block.get("blocked_by") or "implementation_task_progress_controller"),
            failure_type=str(phase_block.get("failure_type") or "implementation_task_progress_blocked"),
            allowed_next_actions=tuple(str(action) for action in (phase_block.get("allowed_next_actions") or [])),
            suggested_fix=suggested_fix,
            next_required_action=str(phase_block.get("next_required_action") or suggested_fix or ""),
            missing_requirements=tuple(phase_block.get("missing_requirements") or []),
            candidate_missing_requirements=tuple(phase_block.get("candidate_missing_requirements") or []),
            repair_hints=tuple(phase_block.get("repair_hints") or []),
            state=phase_block.get("state") or {},
            latest_edit_match_failure_recovery=phase_block.get("latest_edit_match_failure_recovery") or {},
            broad_rewrite=bool(phase_block.get("broad_rewrite")),
            block_header_only_replace=bool(phase_block.get("block_header_only_replace")),
            exact_old_text_matches=phase_block.get("exact_old_text_matches"),
            fixture_repair_mode=bool(phase_block.get("fixture_repair_mode")),
            fixture_review_items=phase_block.get("fixture_review_items") or [],
            repeated_semantic_issue=phase_block.get("repeated_semantic_issue") or {},
            terminal_failure=bool(phase_block.get("terminal_failure")),
        )

    def visible_message(self) -> str:
        message = f"{self.tool_name} がブロックされました: {self.message}"
        if self.current_source_excerpt:
            message += "\ncurrent_source_excerpt for the next exact old_text:\n" + self.current_source_excerpt
        return message

    def status_stream_text(self) -> str:
        message = f"{self.tool_name} blocked by implementation task progress phase. {self.message}"
        if self.current_source_excerpt:
            message += "\ncurrent_source_excerpt for the next exact old_text:\n" + self.current_source_excerpt
        return message

    def event_details(self) -> dict[str, Any]:
        return {
            "reason_code": self.reason_code,
            "blocked_tool": self.tool_name,
            "path": self.path,
            "phase": self.phase,
            "route_phase": self.route_phase,
            "blocked_by": self.blocked_by,
            "failure_type": self.failure_type,
            "allowed_next_actions": list(self.allowed_next_actions),
            "suggested_fix": self.suggested_fix,
            "next_required_action": self.next_required_action,
            "missing_requirements": list(self.missing_requirements),
            "candidate_missing_requirements": list(self.candidate_missing_requirements),
            "repair_hints": list(self.repair_hints),
            "block_signature": self.block_signature,
            "state": self.state,
            "latest_edit_match_failure_recovery": self.latest_edit_match_failure_recovery,
            "broad_rewrite": self.broad_rewrite,
            "block_header_only_replace": self.block_header_only_replace,
            "exact_old_text_matches": self.exact_old_text_matches,
            "current_source_excerpt": self.current_source_excerpt,
            "fixture_repair_mode": self.fixture_repair_mode,
            "fixture_review_items": self.fixture_review_items,
            "repeated_semantic_issue": self.repeated_semantic_issue,
        }
