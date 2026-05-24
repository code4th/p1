from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from p4_core.config_defaults import DEFAULT_TOOL_CONTENT_CHUNK_BYTES
from p4_core.repo_map import build_repo_map


DANGEROUS_COMMAND_PATTERNS = [
    r"\brm\s+-rf\s+/",
    r"\bgit\s+reset\s+--hard\b",
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bmkfs\b",
]

def has_unquoted_command_separator(command: str) -> bool:
    """Return True when a shell command chains processes outside quotes."""

    quote: str | None = None
    escaped = False
    i = 0
    while i < len(command):
        char = command[i]
        if escaped:
            escaped = False
            i += 1
            continue
        if char == "\\" and quote != "'":
            escaped = True
            i += 1
            continue
        if quote:
            if char == quote:
                quote = None
            i += 1
            continue
        if char in {"'", '"'}:
            quote = char
            i += 1
            continue
        if char in {";", "\n", "|", "&"}:
            return True
        i += 1
    return False


class ToolExecutor:
    def __init__(self, root: Path, *, content_chunk_max_bytes: int = DEFAULT_TOOL_CONTENT_CHUNK_BYTES) -> None:
        self.root = root.expanduser().resolve()
        self.content_chunk_max_bytes = int(content_chunk_max_bytes or DEFAULT_TOOL_CONTENT_CHUNK_BYTES)

    def specs(self) -> list[dict[str, Any]]:
        return [
            {"name": "list_files", "args": {"path": "relative path, optional"}, "description": "List files under the workspace."},
            {"name": "repo_map", "args": {"path": "relative path, optional"}, "description": "Return a concise file/symbol/import map so the agent can choose targeted reads and edits."},
            {"name": "read_file", "args": {"path": "relative file path"}, "description": "Read a UTF-8 text file."},
            {"name": "search_code", "args": {"query": "text or regex", "path": "relative path, optional"}, "description": "Search files with ripgrep."},
            {"name": "write_file", "args": {"path": "relative file path", "content": "new file content"}, "description": f"Create or overwrite a UTF-8 file. Prefer chunks up to {self.content_chunk_max_bytes} bytes; complete source up to {self._hard_content_max_bytes()} bytes may be accepted when syntax is valid. Larger content must be split across write_file and append_file."},
            {"name": "append_file", "args": {"path": "relative file path", "content": "content chunk"}, "description": f"Append one UTF-8 chunk. Prefer chunks up to {self.content_chunk_max_bytes} bytes; complete source chunks up to {self._hard_content_max_bytes()} bytes may be accepted when syntax is valid. Larger content must be split on a line boundary."},
            {"name": "replace_text", "args": {"path": "relative file path", "old_text": "exact text to replace", "new_text": "replacement text"}, "description": "Replace one exact, unique text block in an existing file after reading it."},
            {
                "name": "run_command",
                "args": {"command": "shell command", "timeout_seconds": "optional int", "shell": "optional: auto|zsh|bash|sh|powershell"},
                "description": "Run a shell command inside the workspace.",
            },
            {
                "name": "create_plan",
                "args": {
                    "plan": {
                        "plan_id": "stable plan id",
                        "profile": "ProblemProfile derived from the request",
                        "strategy": "direct_implementation|task_decomposition|state_space_search|dynamic_programming|constraint_satisfaction|graph_shortest_path|classical_planning",
                        "work_units": "ordered WorkUnit list with first_action and success_evidence",
                        "verification_contract": "strategy-specific verification contract",
                        "status": "proposed",
                        "revision_count": 0,
                    }
                },
                "description": "Create a structured PlanRecord before executing a complex task; accepted plans are converted into child-frame work packages.",
            },
            {
                "name": "decompose_tasks",
                "args": {
                    "tasks": [
                        {
                            "goal": "focused child-frame goal",
                            "work_type": "inspect|edit|run_test|search",
                            "first_action": {"tool": "read_file|search_code|run_command|write_file|append_file|replace_text|list_files", "args": {}},
                            "success_evidence": "observable evidence required before returning",
                            "why_not_direct_action": "why the current frame should delegate instead of calling first_action now",
                            "context_summary": "parent context needed by that child",
                            "done_when": "evidence or finding required before returning",
                        }
                    ],
                    "rationale": "why these are the right task boundaries",
                },
                "description": "Plan multiple ordered child tasks for the current frame and immediately open the first pending child task.",
            },
            {
                "name": "open_child_frame",
                "args": {
                    "work_package": {
                        "goal": "local child-frame goal",
                        "work_type": "inspect|edit|run_test|search",
                        "first_action": {"tool": "read_file|search_code|run_command|write_file|append_file|replace_text|list_files", "args": {}},
                        "success_evidence": "observable evidence required before returning",
                        "why_not_direct_action": "why the current frame should delegate instead of calling first_action now",
                        "context_summary": "brief parent context to inherit",
                    },
                    "child_task_id": "optional planned task id",
                },
                "description": "Open one focused child frame with a concrete work package contract.",
            },
            {
                "name": "return_to_parent",
                "args": {"summary": "what this frame learned", "findings": "list of key findings"},
                "description": "Return from a child frame to its parent with findings.",
            },
            {"name": "final_answer", "args": {"answer": "direct conversational answer for the user"}, "description": "Alias for finish when no tool execution is needed."},
            {"name": "finish", "args": {"final_answer": "final answer for the user"}, "description": "Mark the task as complete."},
        ]

    def describe_for_prompt(self) -> str:
        lines = []
        for spec in self.specs():
            lines.append(f"- {spec['name']}: {spec['description']} args={spec['args']}")
        return "\n".join(lines)

    def execute(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        *,
        on_update: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        arg_issues = self.argument_issues(tool_name, tool_args)
        if arg_issues:
            return self._invalid_tool_args_result(tool_name=tool_name, issues=arg_issues)
        if tool_name == "list_files":
            return self._list_files(path=str(tool_args.get("path") or "."))
        if tool_name == "repo_map":
            return self._repo_map(path=str(tool_args.get("path") or "."))
        if tool_name == "read_file":
            return self._read_file(path=str(tool_args.get("path") or ""))
        if tool_name == "search_code":
            return self._search_code(
                query=str(tool_args.get("query") or ""),
                path=str(tool_args.get("path") or "."),
            )
        if tool_name == "write_file":
            return self._write_file(
                path=str(tool_args.get("path") or ""),
                content=str(tool_args.get("content") or ""),
            )
        if tool_name == "append_file":
            return self._append_file(
                path=str(tool_args.get("path") or ""),
                content=str(tool_args.get("content") or ""),
            )
        if tool_name == "replace_text":
            return self._replace_text(
                path=str(tool_args.get("path") or ""),
                old_text=str(tool_args.get("old_text") or ""),
                new_text=str(tool_args.get("new_text") or ""),
            )
        if tool_name == "run_command":
            return self._run_command(
                command=str(tool_args.get("command") or ""),
                timeout_seconds=int(tool_args.get("timeout_seconds") or 60),
                shell_name=str(tool_args.get("shell") or "auto"),
                on_update=on_update,
            )
        raise ValueError(f"unsupported tool: {tool_name}")

    def argument_issues(self, tool_name: str, tool_args: dict[str, Any]) -> list[str]:
        """Validate action arguments that must be meaningful before execution.

        Invariant: tool success must mean the requested action had enough
        concrete input to produce observable work. Missing values are not
        normalized to empty strings here, because that turns a contract breach
        into a successful no-op artifact.
        """
        tool = str(tool_name or "").strip()
        args = tool_args if isinstance(tool_args, dict) else {}
        issues: list[str] = []

        def has_text(key: str) -> bool:
            return key in args and bool(str(args.get(key) or "").strip())

        if tool in {"read_file", "write_file", "append_file", "replace_text"} and not has_text("path"):
            issues.append("path is required")
        if tool == "search_code" and not has_text("query"):
            issues.append("query is required")
        if tool == "run_command" and not has_text("command"):
            issues.append("command is required")
        if tool in {"write_file", "append_file"}:
            if "content" not in args:
                issues.append("content is required")
            elif not str(args.get("content") or "").strip():
                issues.append("content must not be empty")
        if tool == "replace_text":
            if "old_text" not in args or not str(args.get("old_text") or ""):
                issues.append("old_text is required")
            if "new_text" not in args:
                issues.append("new_text is required")
        return issues

    def _invalid_tool_args_result(self, *, tool_name: str, issues: list[str]) -> dict[str, Any]:
        return {
            "ok": False,
            "tool": str(tool_name or ""),
            "error": "invalid tool arguments: " + "; ".join(issues),
            "failure_type": "invalid_tool_args",
            "blocked_by": "tool_schema",
            "missing_requirements": list(issues),
            "allowed_next_actions": [
                {
                    "tool": str(tool_name or ""),
                    "strategy": "retry with all required arguments populated with concrete values",
                }
            ],
            "suggested_fix": "必須引数を具体値で埋めて同じtoolを再提案してください。",
            "next_required_action": "retry the same tool with complete concrete arguments",
        }

    def _resolve_path(self, path: str) -> Path:
        clean = str(path or "").strip()
        if not clean:
            raise ValueError("path is required")
        candidate = (self.root / clean).resolve() if not Path(clean).is_absolute() else Path(clean).resolve()
        if os.path.commonpath([str(self.root), str(candidate)]) != str(self.root):
            raise ValueError("path escapes workspace root")
        return candidate

    def _list_files(self, *, path: str) -> dict[str, Any]:
        target = self._resolve_path(path if path != "." else str(self.root))
        if target.is_file():
            rel = target.relative_to(self.root)
            return {"ok": True, "tool": "list_files", "items": [str(rel)]}
        try:
            proc = subprocess.run(
                ["rg", "--files", str(target)],
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.returncode == 0:
                items = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
                items = [str(Path(item).resolve().relative_to(self.root)) for item in items if Path(item).exists()]
                return {"ok": True, "tool": "list_files", "items": items[:200]}
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        items = []
        for file_path in sorted(target.rglob("*")):
            if file_path.is_file():
                items.append(str(file_path.relative_to(self.root)))
                if len(items) >= 200:
                    break
        return {"ok": True, "tool": "list_files", "items": items}

    def _repo_map(self, *, path: str) -> dict[str, Any]:
        target = self._resolve_path(path if path != "." else str(self.root))
        if target.is_file():
            target = target.parent
        return {"ok": True, "tool": "repo_map", "path": str(target.relative_to(self.root)) if target != self.root else ".", "repo_map": build_repo_map(target)}

    def _read_file(self, *, path: str) -> dict[str, Any]:
        target = self._resolve_path(path)
        if not target.exists():
            raise ValueError(f"file does not exist: {path}")
        return {
            "ok": True,
            "tool": "read_file",
            "path": str(target.relative_to(self.root)),
            "content": target.read_text(encoding="utf-8"),
        }

    def _search_code(self, *, query: str, path: str) -> dict[str, Any]:
        if not query.strip():
            raise ValueError("query is required")
        target = self._resolve_path(path if path != "." else str(self.root))
        try:
            proc = subprocess.run(
                ["rg", "-n", "--hidden", "--glob", "!.git", query, str(target)],
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=30,
            )
            lines = (proc.stdout or "").splitlines()[:200]
            return {"ok": True, "tool": "search_code", "matches": lines}
        except FileNotFoundError as exc:
            raise RuntimeError("rg is required for search_code") from exc

    def _write_file(self, *, path: str, content: str) -> dict[str, Any]:
        target = self._resolve_path(path)
        content_bytes = len(content.encode("utf-8"))
        max_bytes = self.content_chunk_max_bytes
        hard_max_bytes = self._hard_content_max_bytes()
        rel_path = str(target.relative_to(self.root))
        if target.exists() and target.read_text(encoding="utf-8") == content:
            return {
                "ok": False,
                "tool": "write_file",
                "path": rel_path,
                "error": "write_file content is identical to the existing file; edit would not change the file",
                "failure_type": "no_op_edit",
                "blocked_by": "runtime_edit_validation",
                "allowed_next_actions": [
                    {"tool": "replace_text", "strategy": "change only the minimal text needed to reduce the failing signature"},
                    {"tool": "write_file", "strategy": "rewrite the complete target file with real changes"},
                ],
                "suggested_fix": "contentが現行ファイルと同一です。失敗signatureを減らす具体的な差分を含む内容を返してください。",
                "next_required_action": "submit changed file content that targets the reported failure",
            }
        size_guidance = self._content_size_guidance(tool_name="write_file", path=rel_path, content=content)
        if size_guidance is not None and not bool(size_guidance.get("ok")):
            return size_guidance
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        result = {
            "ok": True,
            "tool": "write_file",
            "path": rel_path,
            "bytes_written": content_bytes,
        }
        if content_bytes > max_bytes:
            result.update(
                self._accepted_size_policy_fields(
                    content_bytes=content_bytes,
                    soft_max_bytes=max_bytes,
                    hard_max_bytes=hard_max_bytes,
                    accepted_valid_source=bool((size_guidance or {}).get("accepted_valid_source")),
                )
            )
        return result

    def _syntax_validation_failed_result(self, *, tool_name: str, path: str, content_bytes: int, syntax_error: str) -> dict[str, Any]:
        return {
            "ok": False,
            "tool": tool_name,
            "path": path,
            "error": "python source failed syntax validation; fix or split on a syntactically valid boundary before writing",
            "failure_type": "validation_failed",
            "blocked_by": "runtime_python_syntax_validation",
            "bytes_requested": content_bytes,
            "recommended_next_chunk_bytes": self.content_chunk_max_bytes,
            "hard_max_bytes": self._hard_content_max_bytes(),
            "syntax_error": syntax_error,
            "allowed_next_actions": [
                {"tool": "read_file", "strategy": "inspect the current file before retrying"},
                {"tool": tool_name, "strategy": "retry with syntactically valid Python source"},
            ],
            "suggested_split_strategy": "For Python files, write a complete syntactically valid module or use smaller edits that keep the whole file parseable.",
            "next_required_action": "inspect the current file if it exists, then submit syntactically valid Python with replace_text or write_file",
        }

    def _append_file(self, *, path: str, content: str) -> dict[str, Any]:
        target = self._resolve_path(path)
        content_bytes = len(content.encode("utf-8"))
        max_bytes = self.content_chunk_max_bytes
        hard_max_bytes = self._hard_content_max_bytes()
        existing_content = target.read_text(encoding="utf-8") if target.exists() else ""
        size_guidance = self._content_size_guidance(
            tool_name="append_file",
            path=str(target.relative_to(self.root)),
            content=content,
            syntax_content=existing_content + content,
        )
        if size_guidance is not None and not bool(size_guidance.get("ok")):
            return size_guidance
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(content)
        result = {
            "ok": True,
            "tool": "append_file",
            "path": str(target.relative_to(self.root)),
            "bytes_appended": content_bytes,
            "total_bytes": target.stat().st_size,
        }
        if content_bytes > max_bytes:
            result.update(
                self._accepted_size_policy_fields(
                    content_bytes=content_bytes,
                    soft_max_bytes=max_bytes,
                    hard_max_bytes=hard_max_bytes,
                    accepted_valid_source=bool((size_guidance or {}).get("accepted_valid_source")),
                )
            )
        return result

    def _hard_content_max_bytes(self) -> int:
        return self.content_chunk_max_bytes * 2

    def _accepted_size_policy_fields(
        self,
        *,
        content_bytes: int,
        soft_max_bytes: int,
        hard_max_bytes: int,
        accepted_valid_source: bool,
    ) -> dict[str, Any]:
        if accepted_valid_source:
            return {
                "size_policy": "accepted_valid_source_over_hard_limit",
                "warning": (
                    "content exceeded hard chunk size, but the visible JSON closed and "
                    "the resulting Python source passed syntax validation; accepted so "
                    "the task can proceed to semantic validation"
                ),
                "bytes_requested": content_bytes,
                "recommended_next_chunk_bytes": soft_max_bytes,
                "hard_max_bytes": hard_max_bytes,
            }
        return {
            "size_policy": "accepted_over_soft_limit",
            "warning": f"content exceeded recommended chunk size ({content_bytes}/{soft_max_bytes} bytes) but stayed within hard limit ({hard_max_bytes} bytes) and syntax validation did not fail",
            "recommended_next_chunk_bytes": soft_max_bytes,
            "hard_max_bytes": hard_max_bytes,
        }

    def _content_size_guidance(self, *, tool_name: str, path: str, content: str, syntax_content: str | None = None) -> dict[str, Any] | None:
        content_bytes = len(content.encode("utf-8"))
        soft_max_bytes = self.content_chunk_max_bytes
        hard_max_bytes = self._hard_content_max_bytes()
        syntax_error = self._source_syntax_error(path=path, content=content if syntax_content is None else syntax_content)
        if syntax_error:
            try:
                file_exists = self._resolve_path(path).exists()
            except Exception:
                file_exists = False
            if file_exists:
                allowed_next_actions = [
                    {"tool": "read_file", "strategy": "inspect the current file before retrying"},
                    {"tool": "replace_text", "strategy": "after read_file, replace one exact block with complete valid Python"},
                    {"tool": "write_file", "strategy": "rewrite the complete valid Python file when replacement is not unique or the file does not exist"},
                ]
                suggested_split_strategy = "Keep the whole Python file syntactically valid after each successful edit. After a Python validation failure, inspect the current file and use replace_text or write_file; do not retry append_file on the same path."
            else:
                allowed_next_actions = [
                    {"tool": "write_file", "strategy": "rewrite the complete valid Python file; read_file is invalid because the file does not exist"},
                ]
                suggested_split_strategy = "No file was written because Python syntax validation failed. Return one complete syntactically valid Python module with write_file; do not read the missing file."
            return {
                "ok": False,
                "tool": tool_name,
                "path": path,
                "error": "python source failed syntax validation; fix or split the source before writing",
                "failure_type": "validation_failed",
                "blocked_by": "runtime_python_syntax_validation",
                "bytes_requested": content_bytes,
                "recommended_next_chunk_bytes": soft_max_bytes,
                "hard_max_bytes": hard_max_bytes,
                "syntax_error": syntax_error,
                "file_exists": file_exists,
                "allowed_next_actions": allowed_next_actions,
                "suggested_split_strategy": suggested_split_strategy,
                "next_required_action": (
                    "read the existing file before retrying, then use replace_text or write_file with complete valid Python"
                    if file_exists
                    else "retry write_file with one complete syntactically valid Python module"
                ),
            }
        if content_bytes <= soft_max_bytes:
            return None
        if content_bytes <= hard_max_bytes:
            return {"ok": True}
        if path.endswith(".py"):
            return {
                "ok": True,
                "accepted_valid_source": True,
                "recommended_next_chunk_bytes": soft_max_bytes,
                "hard_max_bytes": hard_max_bytes,
            }
        return {
            "ok": False,
            "tool": tool_name,
            "path": path,
            "error": f"{tool_name} content is more than double the recommended chunk size; split it before retrying",
            "failure_type": "content_too_large",
            "blocked_by": "runtime_content_size_policy",
            "bytes_requested": content_bytes,
            "recommended_next_chunk_bytes": soft_max_bytes,
            "hard_max_bytes": hard_max_bytes,
            "allowed_next_actions": [
                {"tool": "write_file", "strategy": "write only the first line-boundary chunk"},
                {"tool": "append_file", "strategy": "append the next line-boundary chunk after the first chunk exists"},
            ],
            "suggested_split_strategy": "Use chunks no larger than recommended_next_chunk_bytes; if needed, one syntactically valid chunk may exceed that up to hard_max_bytes, but content above hard_max_bytes must be split.",
            "next_required_action": "split the content into a smaller complete chunk before retrying",
        }

    def _source_syntax_error(self, *, path: str, content: str) -> str:
        if not path.endswith(".py"):
            return ""
        try:
            compile(content, path, "exec")
        except SyntaxError as exc:
            location = f"line {exc.lineno}" if exc.lineno else "unknown location"
            return f"{exc.msg} ({location})"
        try:
            tree = ast.parse(content, filename=path)
        except SyntaxError as exc:
            location = f"line {exc.lineno}" if exc.lineno else "unknown location"
            return f"{exc.msg} ({location})"
        return self._duplicate_python_definition_error(tree)

    def _duplicate_python_definition_error(self, tree: ast.AST) -> str:
        def check_body(body: list[ast.stmt], scope: str) -> str:
            seen: dict[str, int] = {}
            for node in body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    previous = seen.get(node.name)
                    if previous is not None:
                        return f"duplicate definition `{node.name}` in {scope} (lines {previous} and {node.lineno})"
                    seen[node.name] = int(node.lineno)
                    if isinstance(node, ast.ClassDef):
                        nested = check_body(list(node.body), f"class {node.name}")
                        if nested:
                            return nested
                nested_body = getattr(node, "body", None)
                if isinstance(nested_body, list) and not isinstance(node, ast.ClassDef):
                    nested = check_body(nested_body, scope)
                    if nested:
                        return nested
            return ""

        return check_body(list(getattr(tree, "body", [])), "module")

    def _trailing_whitespace_normalized(self, text: str) -> str:
        return "\n".join(line.rstrip() for line in text.splitlines()).rstrip("\n")

    def _replace_text(self, *, path: str, old_text: str, new_text: str) -> dict[str, Any]:
        if not old_text:
            raise ValueError("old_text is required")
        target = self._resolve_path(path)
        if not target.exists():
            raise ValueError(f"file does not exist: {path}")
        if old_text == new_text:
            rel_path = str(target.relative_to(self.root))
            return {
                "ok": False,
                "tool": "replace_text",
                "path": rel_path,
                "error": "replace_text old_text and new_text are identical; edit would not change the file",
                "failure_type": "no_op_edit",
                "blocked_by": "runtime_edit_validation",
                "allowed_next_actions": [
                    {"tool": "replace_text", "strategy": "change only the minimal text needed to reduce the failing signature"},
                    {"tool": "write_file", "strategy": "rewrite the complete valid file only if a targeted replacement cannot express the fix"},
                ],
                "suggested_fix": "old_textとnew_textが同一です。失敗signatureを減らす具体的な差分だけを返してください。",
                "next_required_action": "submit a non-empty edit that changes the file and targets the reported failure",
            }
        content = target.read_text(encoding="utf-8")
        count = content.count(old_text)
        normalized_whole_file_match = (
            count == 0
            and self._trailing_whitespace_normalized(old_text) == self._trailing_whitespace_normalized(content)
        )
        if count != 1:
            if normalized_whole_file_match:
                rel_path = str(target.relative_to(self.root))
                syntax_error = self._source_syntax_error(path=rel_path, content=new_text)
                if syntax_error:
                    return self._syntax_validation_failed_result(
                        tool_name="replace_text",
                        path=rel_path,
                        content_bytes=len(new_text.encode("utf-8")),
                        syntax_error=syntax_error,
                    )
                target.write_text(new_text, encoding="utf-8")
                return {
                    "ok": True,
                    "tool": "replace_text",
                    "path": rel_path,
                    "bytes_written": len(new_text.encode("utf-8")),
                    "match_strategy": "whole_file_trailing_whitespace_normalized",
                }
            return {
                "ok": False,
                "tool": "replace_text",
                "path": str(target.relative_to(self.root)),
                "error": f"old_text must match exactly once; matched {count} times",
                "failure_type": "replace_text_no_match" if count == 0 else "replace_text_ambiguous_match",
                "blocked_by": "runtime_edit_validation",
                "matches": count,
                "allowed_next_actions": [
                    {"tool": "read_file", "strategy": "inspect the current file and copy an exact old_text before retrying"},
                    {"tool": "write_file", "strategy": "rewrite the complete valid file when a unique replacement target cannot be identified"},
                ],
                "suggested_fix": "read_fileで現在内容を確認し、一意一致する小さいold_textにするか、完全な有効ファイルをwrite_fileしてください。",
                "next_required_action": "read the current file, then retry with exact unique old_text or write_file the complete file",
            }
        updated = content.replace(old_text, new_text, 1)
        rel_path = str(target.relative_to(self.root))
        if updated == content:
            return {
                "ok": False,
                "tool": "replace_text",
                "path": rel_path,
                "error": "replace_text produced no file change",
                "failure_type": "no_op_edit",
                "blocked_by": "runtime_edit_validation",
                "allowed_next_actions": [
                    {"tool": "replace_text", "strategy": "change only the minimal text needed to reduce the failing signature"},
                    {"tool": "write_file", "strategy": "rewrite the complete valid file only if a targeted replacement cannot express the fix"},
                ],
                "suggested_fix": "replace_textが無変更になっています。失敗signatureを減らす具体的な差分だけを返してください。",
                "next_required_action": "submit a non-empty edit that changes the file and targets the reported failure",
            }
        syntax_error = self._source_syntax_error(path=rel_path, content=updated)
        if syntax_error:
            return self._syntax_validation_failed_result(
                tool_name="replace_text",
                path=rel_path,
                content_bytes=len(updated.encode("utf-8")),
                syntax_error=syntax_error,
            )
        target.write_text(updated, encoding="utf-8")
        return {
            "ok": True,
            "tool": "replace_text",
            "path": rel_path,
            "bytes_written": len(updated.encode("utf-8")),
        }

    def _run_command(
        self,
        *,
        command: str,
        timeout_seconds: int,
        shell_name: str,
        on_update: Callable[[dict[str, Any]], None] | None,
    ) -> dict[str, Any]:
        clean = command.strip()
        if not clean:
            return {
                "ok": False,
                "tool": "run_command",
                "command": command,
                "error": "command is required",
                "failure_type": "invalid_command",
                "blocked_by": "tool_schema",
                "allowed_next_actions": [{"tool": "run_command", "strategy": "provide one concrete command"}],
                "suggested_fix": "run_command.command に実行する単一コマンドを入れてください。",
                "next_required_action": "retry run_command with a non-empty command",
            }
        if has_unquoted_command_separator(clean):
            return {
                "ok": False,
                "tool": "run_command",
                "command": clean,
                "error": "run_command accepts exactly one shell command per step; chaining commands with separators outside quotes is not allowed",
                "failure_type": "multi_command_denied",
                "blocked_by": "tool_safety_policy",
                "allowed_next_actions": [{"tool": "run_command", "strategy": "run one process only; separators such as ;, &&, ||, |, or newline are allowed only inside a quoted argument"}],
                "suggested_fix": "シェル連結は分解し、次に必要な1プロセスだけを実行してください。python3 -c などの引用符内コードに含まれるセミコロンは許可されます。",
                "next_required_action": "retry run_command with a single command",
            }
        for pattern in DANGEROUS_COMMAND_PATTERNS:
            if re.search(pattern, clean):
                return {
                    "ok": False,
                    "tool": "run_command",
                    "command": clean,
                    "error": f"command denied by safety policy: {clean}",
                    "failure_type": "dangerous_command_denied",
                    "blocked_by": "tool_safety_policy",
                    "allowed_next_actions": [{"tool": "run_command", "strategy": "choose a non-destructive inspection or test command"}],
                    "suggested_fix": "破壊的操作ではなく、状態確認・テスト・非破壊の編集検証コマンドに切り替えてください。",
                    "next_required_action": "choose a safe non-destructive command",
                }
        normalized_from = ""
        if re.match(r"^python(\s+.+)?$", clean) and shutil.which("python") is None and shutil.which("python3") is not None:
            normalized_from = clean
            clean = re.sub(r"^python(\s+|$)", "python3\\1", clean, count=1)
        try:
            argv, resolved_shell = self._command_argv(clean, shell_name)
        except ValueError as exc:
            return {
                "ok": False,
                "tool": "run_command",
                "command": clean,
                "error": str(exc),
                "failure_type": "unsupported_shell",
                "blocked_by": "tool_schema",
                "allowed_next_actions": [{"tool": "run_command", "strategy": "retry with shell auto, zsh, bash, or sh"}],
                "suggested_fix": "shell を auto/zsh/bash/sh のいずれかにして同じ目的の単一コマンドを実行してください。",
                "next_required_action": "retry run_command with a supported shell",
            }
        started_at = time.time()
        started_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started_at))
        proc = subprocess.Popen(
            argv,
            cwd=self.root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []

        def _pump(handle: Any, sink: list[str], stream_name: str) -> None:
            if handle is None:
                return
            for chunk in iter(handle.readline, ""):
                sink.append(chunk)
                if on_update is not None:
                    on_update(
                        {
                            "ok": None,
                            "tool": "run_command",
                            "command": clean,
                            "shell": resolved_shell,
                            "cwd": str(self.root),
                            "stdout": "".join(stdout_chunks)[-4000:],
                            "stderr": "".join(stderr_chunks)[-4000:],
                            "active_stream": stream_name,
                        }
                    )
            handle.close()

        stdout_thread = threading.Thread(target=_pump, args=(proc.stdout, stdout_chunks, "stdout"), daemon=True)
        stderr_thread = threading.Thread(target=_pump, args=(proc.stderr, stderr_chunks, "stderr"), daemon=True)
        stdout_thread.start()
        stderr_thread.start()
        try:
            returncode = proc.wait(timeout=timeout_seconds)
            stdout_thread.join(timeout=1)
            stderr_thread.join(timeout=1)
            finished_at = time.time()
            finished_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(finished_at))
            duration_ms = int((finished_at - started_at) * 1000)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout_thread.join(timeout=1)
            stderr_thread.join(timeout=1)
            finished_at = time.time()
            finished_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(finished_at))
            duration_ms = int((finished_at - started_at) * 1000)
            return {
                "ok": False,
                "tool": "run_command",
                "command": clean,
                "normalized_from": normalized_from,
                "shell": resolved_shell,
                "cwd": str(self.root),
                "started_at": started_iso,
                "finished_at": finished_iso,
                "duration_ms": duration_ms,
                "returncode": None,
                "stdout": "".join(stdout_chunks),
                "stderr": "".join(stderr_chunks) + f"\nTimed out after {timeout_seconds}s",
                "failure_type": "command_timeout",
                "blocked_by": "runtime_command_timeout",
                "timeout_seconds": timeout_seconds,
                "allowed_next_actions": [
                    "run_command with a narrower command",
                    "read_file to inspect the failing target before retrying",
                    "write_file or replace_text to reduce the failing scope before rerun",
                ],
                "suggested_fix": (
                    "The command exceeded the runtime timeout. Inspect the target or narrow the command "
                    "before retrying; do not repeat the same long-running command unchanged."
                ),
                "next_required_action": "narrow the command or edit the target before rerunning validation",
            }
        return {
            "ok": returncode == 0,
            "tool": "run_command",
            "command": clean,
            "normalized_from": normalized_from,
            "shell": resolved_shell,
            "cwd": str(self.root),
            "started_at": started_iso,
            "finished_at": finished_iso,
            "duration_ms": duration_ms,
            "returncode": returncode,
            "stdout": "".join(stdout_chunks),
            "stderr": "".join(stderr_chunks),
        }

    def _command_argv(self, command: str, shell_name: str) -> tuple[list[str], str]:
        requested = str(shell_name or "auto").strip().lower()
        if requested in {"", "auto"}:
            env_shell = Path(os.environ.get("SHELL") or "").name.lower()
            requested = env_shell if env_shell in {"zsh", "bash", "sh"} else "bash"
        if requested in {"powershell", "pwsh"}:
            if shutil.which("pwsh") is None:
                raise ValueError("PowerShell requested but 'pwsh' is not installed in this environment")
            return ["pwsh", "-NoLogo", "-NoProfile", "-Command", command], "powershell"
        if requested not in {"zsh", "bash", "sh"}:
            raise ValueError(f"unsupported shell: {shell_name}")
        return [requested, "-lc", command], requested
