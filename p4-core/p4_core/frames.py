from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from p4_core.workspace import append_jsonl, now_iso, read_jsonl


@dataclass
class WorkingMemory:
    observations: list[str] = field(default_factory=list)
    current_focus: str = ""
    unresolved_questions: list[str] = field(default_factory=list)
    avoid_repeating: list[str] = field(default_factory=list)
    child_tasks: list[dict[str, Any]] = field(default_factory=list)
    completed_child_tasks: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Frame:
    frame_id: str
    parent_frame_id: str | None
    depth: int
    goal: str
    inherited_context: dict[str, Any]
    working_memory: WorkingMemory = field(default_factory=WorkingMemory)
    session_events: list[dict[str, Any]] = field(default_factory=list)
    return_payload: dict[str, Any] | None = None
    status: str = "active"
    created_at: str = field(default_factory=now_iso)
    returned_at: str | None = None
    step_count: int = 0

    def to_dict(self, *, include_session_events: bool = False) -> dict[str, Any]:
        payload = asdict(self)
        if not include_session_events:
            payload.pop("session_events", None)
        payload["working_memory"] = asdict(self.working_memory)
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Frame":
        data = dict(payload)
        data["working_memory"] = WorkingMemory(**dict(data.get("working_memory") or {}))
        return cls(**data)


class FrameManager:
    def __init__(self, root: Path, *, max_depth: int = 4) -> None:
        self.root = Path(root).expanduser().resolve()
        self.max_depth = max_depth
        self.frames: dict[str, Frame] = {}
        self.active_frame_id: str | None = None
        self.event_counts: dict[str, int] = {"frame_opened": 0, "frame_returned": 0, "child_return": 0}
        self.events_path = self.root / "state" / "frames" / "frames.jsonl"
        self._load_latest()

    def create_root_frame(self, goal: str) -> Frame:
        frame = Frame(
            frame_id=uuid.uuid4().hex,
            parent_frame_id=None,
            depth=0,
            goal=str(goal or "root turn"),
            inherited_context={},
        )
        self.frames = {frame.frame_id: frame}
        self.active_frame_id = frame.frame_id
        self._persist("root_created")
        return frame

    # Forbidden keys in inherited_context. These represent parent state and
    # must be dereferenced via parent_frame_id/child_task_id at read time
    # rather than copied at open time. Keeping them out preserves the
    # single-source-of-truth invariant (see p4-symmetry-audit-2026-04-26).
    _FORBIDDEN_INHERITED_KEYS = frozenset({"work_package", "parent_working_memory"})

    def open_child_frame(self, goal: str, inherited_context: dict[str, Any]) -> Frame:
        parent = self.current_frame()
        if parent is None:
            parent = self.create_root_frame("root turn")
        if parent.depth + 1 > self.max_depth:
            raise ValueError(f"frame depth limit exceeded: max depth is {self.max_depth}")
        ctx = dict(inherited_context or {})
        forbidden_present = self._FORBIDDEN_INHERITED_KEYS.intersection(ctx.keys())
        if forbidden_present:
            raise AssertionError(
                f"inherited_context must not copy parent state: {sorted(forbidden_present)}. "
                "Use parent_frame_id + child_task_id and call "
                "FrameManager.work_package_for / parent_working_memory_for at read time."
            )
        child = Frame(
            frame_id=uuid.uuid4().hex,
            parent_frame_id=parent.frame_id,
            depth=parent.depth + 1,
            goal=str(goal or "child frame"),
            inherited_context=ctx,
        )
        self.frames[child.frame_id] = child
        self.active_frame_id = child.frame_id
        self._persist("child_opened")
        return child

    def return_to_parent(self, return_payload: dict[str, Any]) -> Frame:
        child = self.current_frame()
        if child is None or child.parent_frame_id is None:
            raise ValueError("cannot return to parent from root frame")
        child.return_payload = dict(return_payload or {})
        child.status = "returned"
        child.returned_at = now_iso()
        parent = self.frames[child.parent_frame_id]
        self.active_frame_id = parent.frame_id
        self._persist("child_returned")
        return parent

    def current_frame(self) -> Frame | None:
        if self.active_frame_id is None:
            return None
        return self.frames.get(self.active_frame_id)

    def frame_stack(self) -> list[Frame]:
        current = self.current_frame()
        if current is None:
            return []
        stack = [current]
        while stack[-1].parent_frame_id:
            parent = self.frames.get(str(stack[-1].parent_frame_id))
            if parent is None:
                break
            stack.append(parent)
        return list(reversed(stack))

    # ------------------------------------------------------------------
    # Inherited context dereferencing.
    #
    # Per the symmetry constitution (single source of truth), child frames
    # MUST NOT carry copies of parent state in `inherited_context`. The
    # parent_frame_id and child_task_id are enough to derive the live view.
    # These helpers replace the old `inherited_context["work_package"]` and
    # `inherited_context["parent_working_memory"]` reads.
    # ------------------------------------------------------------------
    def parent_of(self, frame: Frame) -> Frame | None:
        parent_id = str(frame.parent_frame_id or "")
        if not parent_id:
            return None
        return self.frames.get(parent_id)

    def parent_working_memory_for(self, frame: Frame) -> WorkingMemory | None:
        parent = self.parent_of(frame)
        return parent.working_memory if parent is not None else None

    def work_package_for(self, frame: Frame) -> dict[str, Any] | None:
        parent = self.parent_of(frame)
        if parent is None:
            return None
        task_id = str(frame.inherited_context.get("child_task_id") or "")
        if not task_id:
            return None
        for task in parent.working_memory.child_tasks:
            if str(task.get("task_id") or "") == task_id:
                return dict(task)
        for task in parent.working_memory.completed_child_tasks:
            if str(task.get("task_id") or "") == task_id:
                return dict(task)
        return None

    def update_working_memory(self, updates: dict[str, Any]) -> None:
        frame = self.current_frame()
        if frame is None:
            return
        wm = frame.working_memory
        for key in ("observations", "unresolved_questions", "avoid_repeating"):
            values = updates.get(key)
            if not values:
                continue
            target = getattr(wm, key)
            for value in values if isinstance(values, list) else [values]:
                clean = str(value or "").strip()
                if clean and clean not in target:
                    target.append(clean)
        if "current_focus" in updates:
            wm.current_focus = str(updates.get("current_focus") or "")
        self._persist("working_memory_updated")

    def update_from_tool_result(self, tool_name: str, tool_args: dict[str, Any], tool_result: dict[str, Any]) -> None:
        if self.current_frame() is None:
            return
        ok = bool(tool_result.get("ok"))
        updates: dict[str, Any] = {}
        if tool_name == "read_file":
            path = str(tool_result.get("path") or tool_args.get("path") or "")
            updates["observations"] = [f"read_file: {path}"] if path else []
            updates["current_focus"] = path
        elif tool_name == "search_code":
            query = str(tool_args.get("query") or "")
            updates["observations"] = [f"search_code: {query}"] if query else []
            updates["current_focus"] = query
        elif tool_name == "run_command":
            command = str(tool_result.get("command") or tool_args.get("command") or "")
            status = "succeeded" if ok else "failed"
            updates["observations"] = [f"run_command {status}: {command}"] if command else []
            if not ok and command:
                updates["avoid_repeating"] = [command]
        elif tool_name in {"write_file", "append_file", "replace_text"}:
            path = str(tool_result.get("path") or tool_args.get("path") or "")
            status = "succeeded" if ok else "failed"
            updates["observations"] = [f"{tool_name} {status}: {path}"] if path else []
            updates["current_focus"] = path
        self.update_working_memory(updates)

    def set_child_tasks(self, tasks: list[dict[str, Any]]) -> None:
        frame = self.current_frame()
        if frame is None:
            return
        frame.working_memory.child_tasks = [dict(task) for task in tasks]
        frame.working_memory.completed_child_tasks = []
        self._persist("child_tasks_planned")

    def register_child_task(self, *, parent: Frame, task: dict[str, Any]) -> dict[str, Any]:
        registered = dict(task or {})
        task_id = str(registered.get("task_id") or registered.get("child_task_id") or "").strip()
        if not task_id:
            task_id = f"adhoc-{uuid.uuid4().hex}"
            registered["task_id"] = task_id
        for index, existing in enumerate(parent.working_memory.child_tasks):
            if str(existing.get("task_id") or "") == task_id:
                parent.working_memory.child_tasks[index] = registered
                self._persist("child_task_registered")
                return dict(registered)
        parent.working_memory.child_tasks.append(registered)
        self._persist("child_task_registered")
        return dict(registered)

    def next_pending_child_task(self, frame: Frame | None = None) -> dict[str, Any] | None:
        target = frame or self.current_frame()
        if target is None:
            return None
        completed_ids = {
            str(item.get("task_id") or "")
            for item in target.working_memory.completed_child_tasks
            if item.get("task_id")
        }
        for task in target.working_memory.child_tasks:
            task_id = str(task.get("task_id") or "")
            if task_id and task_id not in completed_ids:
                return dict(task)
        return None

    def mark_child_task_completed(self, *, parent: Frame, child: Frame | None, return_payload: dict[str, Any]) -> None:
        if child is None:
            return
        task_id = str(child.inherited_context.get("child_task_id") or "")
        if not task_id:
            return
        already_done = {
            str(item.get("task_id") or "")
            for item in parent.working_memory.completed_child_tasks
            if item.get("task_id")
        }
        if task_id in already_done:
            return
        parent.working_memory.completed_child_tasks.append(
            {
                "task_id": task_id,
                "goal": child.goal,
                "summary": str(return_payload.get("summary") or ""),
                "findings": list(return_payload.get("findings") or []),
            }
        )
        self._persist("child_task_completed")

    def append_event(self, event: dict[str, Any], *, frame_id: str | None = None) -> None:
        if self._is_frame_irrelevant_event(event):
            return
        frame = self.frames.get(frame_id or str(self.active_frame_id or ""))
        if frame is None:
            return
        frame.session_events.append(dict(event))
        event_type = str(event.get("type") or "")
        if event_type in self.event_counts:
            self.event_counts[event_type] += 1
        self._persist("event_appended")

    def _is_frame_irrelevant_event(self, event: dict[str, Any]) -> bool:
        event_type = str(event.get("type") or "")
        event_name = str(event.get("event_name") or "")
        return event_type == "runtime_event" and event_name == "llm_stream_chunk"

    def increment_step(self) -> int:
        frame = self.current_frame()
        if frame is None:
            return 0
        frame.step_count += 1
        self._persist("step_incremented")
        return frame.step_count

    def reset_active_stack_steps(self) -> None:
        """Start a continuation turn with a fresh per-turn frame budget."""

        stack = self.frame_stack()
        if not stack:
            return
        for frame in stack:
            frame.step_count = 0
        self._persist("continuation_steps_reset")

    def abandon_all(self) -> None:
        for frame in self.frames.values():
            if frame.status == "active":
                frame.status = "abandoned"
                frame.returned_at = now_iso()
        self.active_frame_id = None
        self._persist("abandoned")

    def snapshot(self) -> dict[str, Any]:
        return {
            "active_frame_id": self.active_frame_id,
            "frame_stack": [frame.to_dict() for frame in self.frame_stack()],
            "frames": [frame.to_dict() for frame in sorted(self.frames.values(), key=lambda item: item.created_at)],
            "metrics": {
                "frame_opened": self.event_counts["frame_opened"],
                "frame_returned": self.event_counts["frame_returned"],
                "child_return": self.event_counts["child_return"],
                "active_depth": self.current_frame().depth if self.current_frame() else 0,
            },
        }

    def _persist(self, event_type: str) -> None:
        append_jsonl(
            self.events_path,
            {
                "type": event_type,
                "timestamp": now_iso(),
                "snapshot": {
                    "active_frame_id": self.active_frame_id,
                    "event_counts": dict(self.event_counts),
                    "frames": [frame.to_dict() for frame in self.frames.values()],
                },
            },
        )

    def _load_latest(self) -> None:
        rows = read_jsonl(self.events_path, limit=1)
        if not rows:
            return
        snapshot = rows[-1].get("snapshot") or {}
        self.frames = {
            str(item.get("frame_id")): Frame.from_dict(item)
            for item in snapshot.get("frames") or []
            if item.get("frame_id")
        }
        self.active_frame_id = snapshot.get("active_frame_id")
        loaded_counts = dict(snapshot.get("event_counts") or {})
        if loaded_counts:
            self.event_counts = {
                key: int(loaded_counts.get(key) or 0)
                for key in self.event_counts
            }
        else:
            all_events = [event for frame in self.frames.values() for event in frame.session_events]
            self.event_counts = {
                key: sum(1 for event in all_events if event.get("type") == key)
                for key in self.event_counts
            }
