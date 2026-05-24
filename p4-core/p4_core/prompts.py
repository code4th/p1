from __future__ import annotations

import json
import re
import time
from typing import Any

from p4_core.config_defaults import DEFAULT_TOOL_CONTENT_CHUNK_BYTES
from p4_core.planning import PLAN_RECORD_SHAPE_HINT, plan_requires_revision, planning_required_for_profile, profile_problem
from p4_core.schemas import PLAN_RECORD_SCHEMA, tool_action_schema
from p4_core.runtime_profile import runtime_identity_answer

from p4_core.workspace import append_jsonl, append_session_event, now_iso, read_json, read_jsonl


def _current_phase(self, *, user_message: str, steps: list[dict[str, Any]], recent_events: list[dict[str, Any]] = None) -> str:
    edit_tools = {"write_file", "append_file", "replace_text"}
    for step in reversed(steps):
        tool_name = str(step.get("tool_name") or "")
        result = step.get("tool_result") if isinstance(step.get("tool_result"), dict) else {}
        if tool_name == "read_file" and bool(result.get("ok")):
            continue
        if not bool(result.get("ok")) and plan_requires_revision(list(recent_events or []), [step]):
            return "PLAN_REVISION"
        if tool_name in edit_tools and not bool(result.get("ok")):
            return "RECOVER_FROM_TOOL_FAILURE"
        if tool_name == "run_command" and not bool(result.get("ok")):
            return "RECOVER_FROM_TOOL_FAILURE"
        break
    if self._deliberation_reasons(user_message=user_message, steps=steps, recent_events=recent_events):
        return "DELIBERATE"
    recent = list(recent_events or [])
    if plan_requires_revision(recent, steps):
        return "PLAN_REVISION"
    current_frame = self.frame_manager.current_frame()
    if current_frame is not None and (
        current_frame.parent_frame_id is not None
        or current_frame.working_memory.child_tasks
        or current_frame.working_memory.completed_child_tasks
    ):
        return "PLAN_EXECUTION"
    profile = profile_problem(user_message)
    has_plan_record = (
        self._latest_plan_record_for_current_request(
            user_message=user_message,
            recent_events=recent,
        )
        is not None
    )
    if planning_required_for_profile(profile) and not has_plan_record:
        return "PLANNING_REQUIRED"
    if recent_events:
        for event in reversed(recent_events):
            event_type = str(event.get("type") or "")
            if event_type in {"assistant_message", "tool_call"}:
                continue
            if event_type == "system_note" and str(event.get("code") or "") == "edit_blocked":
                return "RECOVER_FROM_TOOL_FAILURE"
            break

    # Phase 6: Planning for complex tasks
    is_complex = any(kw in user_message.lower() for kw in ["implement", "fix", "refactor", "create", "design", "実装", "修正", "構造", "作成", "新設", "設計"])
    if is_complex and not steps:
        return "PLANNING"

    requested = self._extract_requested_commands(user_message)
    if requested and not steps:
        return "DISCOVER_REQUIRED_COMMANDS"
    if self._missing_requested_commands(user_message=user_message, steps=steps):
        return "EXECUTE_MISSING_COMMANDS"
    if any(str(step.get("tool_name") or "") == "run_command" for step in steps):
        return "SYNTHESIZE_FROM_EVIDENCE"
    return "FINISH"


def _deliberation_reasons(self, *, user_message: str, steps: list[dict[str, Any]], recent_events: list[dict[str, Any]] = None) -> list[str]:
    reasons = []
    # 1. Step count threshold
    if len(steps) >= 10: # Increased threshold as complex tasks need more steps
        reasons.append(f"ステップ数が上限（{len(steps)}/12）に近づいています")

    # 2. Consecutive finish blocks (checking recent events)
    if recent_events:
        consecutive_blocks = 0
        for event in reversed(recent_events):
            if event.get("type") == "system_note" and "完了がブロックされました" in str(event.get("content") or ""):
                consecutive_blocks += 1
            elif event.get("type") == "assistant_message" or event.get("type") == "tool_result":
                # Only count if no progress was made between blocks
                continue
            elif event.get("type") == "operation" and event.get("status") == "running":
                break
        if consecutive_blocks >= 2:
            reasons.append(f"完了が{consecutive_blocks}回連続でブロックされました")

        edit_blocks: list[dict[str, Any]] = [
            event
            for event in recent_events
            if event.get("type") == "system_note" and str(event.get("code") or "") == "edit_blocked"
        ]
        if len(edit_blocks) >= 2:
            latest = edit_blocks[-1]
            latest_details = latest.get("details") if isinstance(latest.get("details"), dict) else {}
            latest_reason = str(latest.get("reason_code") or "")
            latest_path = str(latest_details.get("path") or "")
            same_blocks = [
                event
                for event in edit_blocks[-3:]
                if str(event.get("reason_code") or "") == latest_reason
                and (
                    str((event.get("details") if isinstance(event.get("details"), dict) else {}).get("path") or "") == latest_path
                    or (
                        latest_reason == "python_artifact_contract_incomplete"
                        and str((event.get("details") if isinstance(event.get("details"), dict) else {}).get("path") or "").replace("\\", "/").endswith(".py")
                    )
                )
            ]
            if len(same_blocks) >= 2:
                reasons.append(f"{latest_path or '同じファイル'} への編集提案が {latest_reason or '同じ理由'} で連続拒否されています")

    # 3. Overall command failures
    failed_count = sum(1 for step in steps if step.get("tool_name") == "run_command" and not bool((step.get("tool_result") or {}).get("ok")))
    if failed_count >= 3:
        reasons.append(f"コマンド実行が合計{failed_count}回失敗しています")
    elif len(steps) >= 2:
        last_results = [step.get("tool_result") or {} for step in steps[-2:]]
        if all(not bool(res.get("ok")) for res in last_results) and all(step.get("tool_name") == "run_command" for step in steps[-2:]):
            reasons.append("コマンド実行が連続して失敗しています")

    # 4. Stagnation: Repeated tool outputs without finding target
    if len(steps) >= 3:
        last_three = steps[-3:]
        tools = [s.get("tool_name") for s in last_three]
        results = [json.dumps(s.get("tool_result") or {}, sort_keys=True) for s in last_three]
        if len(set(tools)) == 1 and tools[0] in {"list_files", "search_code", "read_file"}:
            if len(set(results)) == 1:
                reasons.append(f"ツール（{tools[0]}）で同じ結果を繰り返しています")

        # 5. Exact command duplication
        if len(set(tools)) == 1 and tools[0] == "run_command":
            commands = [str((s.get("tool_result") or {}).get("command") or "") for s in last_three]
            if len(set(commands)) == 1 and commands[0]:
                reasons.append(f"まったく同じコマンド（{commands[0]}）を再実行しています")

    # 6. Missing commands persisting
    missing_commands = self._missing_requested_commands(user_message=user_message, steps=steps)
    if missing_commands and len(steps) >= 4:
        reasons.append(f"要求されたコマンド（{', '.join(missing_commands)}）が未実行のままステップを消費しています")

    return reasons


def _system_prompt(
    self,
    *,
    suppress_frame_operations: bool = False,
    allowed_tool_names: list[str] | tuple[str, ...] | None = None,
) -> str:
    allowed_tool_set = {str(name) for name in (allowed_tool_names or [])}
    planning_contract_only = allowed_tool_set == {"create_plan"}
    if planning_contract_only:
        suppress_frame_operations = True
    output_budget = (
        "現在は計画契約フェーズです。create_plan 以外の tool は選べません。"
        "PlanRecord には実装コード本文、テストコード本文、write_file/append_file/replace_text を入れないでください。"
        "WorkUnit.first_action は list_files/read_file/search_code/run_command のいずれかだけにしてください。"
        if planning_contract_only
        else self._output_budget_prompt(suppress_frame_operations=suppress_frame_operations)
    )
    tool_action_schema_text = json.dumps(
        tool_action_schema(
            include_frame_operations=not suppress_frame_operations,
            allowed_tool_names=allowed_tool_names,
        ),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    tool_descriptions = self.tools.describe_for_prompt()
    if allowed_tool_set:
        tool_descriptions = "\n".join(
            line for line in tool_descriptions.splitlines() if any(line.startswith(f"- {name}:") for name in allowed_tool_set)
        )
    if suppress_frame_operations:
        hidden_prefixes = ("- decompose_tasks:", "- open_child_frame:", "- return_to_parent:")
        tool_descriptions = "\n".join(
            line for line in tool_descriptions.splitlines() if not line.startswith(hidden_prefixes)
        )
    frame_operations_prompt = ""
    if not suppress_frame_operations:
        frame_operations_prompt = (
            "フレーム操作: 問題が複数の局所目的に分かれる場合は decompose_tasks で良い粒度の子タスク計画を作ってください。"
            "decompose_tasks と open_child_frame は、goal だけでは使えません。各子には work_type, first_action, success_evidence, why_not_direct_action を含む work_package が必要です。"
            "first_action は read_file/search_code/run_command/write_file/append_file/replace_text/list_files の具体的な1 tool callとして書いてください。"
            "first_action.args には、その tool の必須引数をすべて具体値で含めてください。write_file/append_file は path と非空 content が必須です。run_command は非空 command が必須です。"
            "decompose_tasks は最初の子フレームを開きます。子が戻ったら親は未完了の子タスクを順に open_child_frame で処理してください。"
            "子フレームでは、最初の具体ツール結果を得るまでは work_package.first_action をそのまま実行し、decompose_tasks/open_child_frame/finish を選ばないでください。"
            "子フレーム内でさらに分解してよいのは、first_action のツール結果を得た後、それでも複数の独立責務が残る場合だけです。"
            "1つだけ局所目的を切り出せば十分な場合だけ open_child_frame を直接使ってください。親が first_action を直接実行できるなら、分解せず直接実行してください。"
            "子フレームで必要な結果または判断材料が揃ったら finish ではなく return_to_parent を使ってください。"
        )
    return (
        f"あなたは {runtime_identity_answer()} "
        "最重要: assistant の可視 content には、必ず JSON オブジェクトを1個だけ出力してください。"
        "Markdown、コードフェンス、説明文、箇条書き、JSONの前後の文章を content に出してはいけません。"
        "内部の thinking / reasoning は content とは別に保ち、content へ混入させないでください。"
        f"content は次の JSON Schema に厳密に従ってください: {tool_action_schema_text}"
        "あなたの仕事は、一度に一つのツールを選択し、ツールの実行結果を新しい証拠（evidence）として活用しながら、ユーザーの目標を達成することです。"
        "客観的な完了が確認されるまで停止しないでください。"
        "出力は必ず上記 JSON 形式とし、tool_name と tool_args は必須です。"
        "analysis と assistant_message は人間向け表示用の任意項目として、必要な場合だけ短く付けてください。"
        f"{output_budget}"
        "実装不変条件:\n"
        "1. まず設計意図を守ってください。曖昧なら勝手に補完せず、既存の証拠・状態・契約に合わせて行動してください。\n"
        "2. 付け焼き刃の局所対応で塞がず、なぜその状態が発生できたのかを上位レイヤーから見直してください。\n"
        "3. 他のコードの暗黙前提に依存せず、局所整合で閉じる形を優先してください。\n"
        "4. tool_result に存在しない事実を主張しないでください。\n"
        "5. 到達しないはずの経路や、設計意図に反する状態は正当化せず、より単純で対称な構造へ戻せないかを優先して考えてください。\n"
        "利用可能なツール:\n"
        f"{tool_descriptions}\n"
        f"{frame_operations_prompt}"
        "finish はタスクが完了した際、またはこれ以上のツール実行が不要で最善の最終回答を出す際にのみ使用してください。"
        "会話への直接回答だけでツールが不要な場合は tool_name=final_answer, tool_args={\"answer\": string} を使用できます。これは runtime が finish として扱います。"
        "tool_result に存在しない事実を主張しないでください。"
        "ユーザーが特定のコマンドの実行を要求した場合は、それらを一つずつ実行してから finish してください。"
        "コードベースを探索する際は、広範な読み取りを行う前に search_code による検索を優先してください。"
    )


def _output_budget_prompt(self, *, suppress_frame_operations: bool = False) -> str:
    chunk_bytes = int(getattr(self, "tool_content_chunk_bytes", DEFAULT_TOOL_CONTENT_CHUNK_BYTES) or DEFAULT_TOOL_CONTENT_CHUNK_BYTES)
    hard_chunk_bytes = chunk_bytes * 2
    frame_budget = (
        ""
        if suppress_frame_operations
        else "タスクが複数の局所問題に分かれる場合は decompose_tasks で順序付き子タスクに分け、各子フレームは必要な発見を return_to_parent してください。"
    )
    return (
        "この応答には生成上限があります。必ず JSON を閉じてください。"
        f"write_file と append_file の tool_args.content は1ステップあたり {chunk_bytes} UTF-8 bytes 以下を推奨します。"
        f"ただし JSON が完全に閉じ、source code の構文が壊れていない場合は最大 {hard_chunk_bytes} UTF-8 bytes まで runtime が警告付きで採用できます。"
        f"{hard_chunk_bytes} bytes を超える内容は1回で返さず、先頭または次に続く1 chunkだけを返し、"
        "次ステップで append_file を続けてください。source code の chunk は原則として行境界で終えてください。"
        "ただし Python ファイルは各ツール成功後にファイル全体が構文的に有効でなければ拒否されます。"
        "Python の chunk は未完成の関数・クラス・文字列で終えてはいけません。"
        "一度で完全実装を書けない場合は、まず小さいが動く完全な実装を write_file し、次ステップで replace_text により構文を保ったまま拡張してください。"
        "構文エラー後は append_file で断片を足して修復しようとせず、read_file 後に replace_text または complete write_file を使ってください。"
        "JSON を閉じられない長さになりそうなら、コード全文や説明を出さず、現在の chunk だけを返してください。"
        "既存ファイルは必ず read_file で対象を確認し、原則として replace_text で一意に一致する old_text と new_text だけを返してください。"
        "ただし tool_result や system_note の allowed_next_actions が write_file を要求または許可した場合は、runtime契約を優先し、ファイル全体を完全な有効ソースとして write_file で書き直してください。"
        "unittest を要求された場合、pass だけの test_* メソッドや assert のないテストは完了証拠になりません。"
        "編集が大きい場合は1回で終えようとせず、1ステップにつき1つの最小編集だけを返し、次ステップで続けてください。"
        f"{frame_budget}"
    )


def _build_prompt(
    self,
    *,
    goal_text: str,
    recent_events: list[dict[str, Any]],
    extra_prompt: str | None = None,
    steps: list[dict[str, Any]] = None,
    current_phase: str = "FINISH",
    user_message: str = "",
    suppress_frame_operations: bool = False,
) -> str:
    planning_contract_phase = current_phase in {"PLANNING_REQUIRED", "PLAN_REVISION"}
    if planning_contract_phase:
        suppress_frame_operations = True
    rendered_events = self._render_action_context_events(recent_events=recent_events, steps=steps or [], user_message=user_message)
    goal_part = goal_text.strip() or "(目標が設定されていません)"
    frame = self.frame_manager.current_frame()
    frame_block = ""
    if frame is not None:
        wm = frame.working_memory
        has_child_return = any(str(event.get("type") or "") == "child_return" for event in frame.session_events[-5:])
        frame_block = (
            "\n\n現在のフレーム状態:\n"
            f"- 目的: {frame.goal}\n"
            f"- 深さ: {frame.depth} / 最大4\n"
            f"- 観測された事実: {json.dumps(wm.observations, ensure_ascii=False)}\n"
            f"- 現在の焦点: {wm.current_focus or '(なし)'}\n"
            f"- 未解決の問い: {json.dumps(wm.unresolved_questions, ensure_ascii=False)}\n"
            f"- 繰り返すべきでない操作: {json.dumps(wm.avoid_repeating, ensure_ascii=False)}\n"
            f"- 子タスク計画: {json.dumps(wm.child_tasks, ensure_ascii=False)}\n"
            f"- 完了した子タスク: {json.dumps(wm.completed_child_tasks, ensure_ascii=False)}\n"
        )
        if not suppress_frame_operations:
            frame_block += (
                "\n利用可能なフレーム操作:\n"
                "- decompose_tasks: 複数の局所タスクを work_package として順序付きに計画し、最初の子フレームを開く\n"
                "- open_child_frame: work_package を持つ局所目的だけを子フレームとして開く\n"
                "- return_to_parent: 子フレームで結果または判断材料が揃ったら親フレームに戻る\n"
            )
        next_task = self.frame_manager.next_pending_child_task(frame)
        if next_task and not suppress_frame_operations:
            frame_block += (
                "\n重要: 未完了の子タスク計画があります。"
                f"次に扱う候補は {json.dumps(next_task, ensure_ascii=False)} です。"
                "親フレームでは、この子タスクを open_child_frame で開くか、全子タスクが不要になった根拠を示して finish してください。\n"
            )
        work_package = self.frame_manager.work_package_for(frame)
        has_tool_evidence = any(str(event.get("type") or "") == "tool_result" for event in frame.session_events)
        if frame.depth > 0 and work_package and not has_tool_evidence:
            first_action = work_package.get("first_action") or {}
            frame_block += (
                "\n重要: この子フレームはまだ具体ツール結果を得ていません。"
                "次は work_package.first_action をそのまま実行してください。"
                f"first_action={json.dumps(first_action, ensure_ascii=False)}\n"
                "この状態で decompose_tasks/open_child_frame/finish を選ぶと runtime がブロックします。\n"
            )
        if frame.depth > 0 and work_package and self._current_child_is_plan_work_unit():
            if str(current_phase or "").startswith("IMPLEMENTATION_TASK_PROGRESS:"):
                frame_block += (
                    "\n重要: この子フレームは accepted PlanRecord の WorkUnit ですが、"
                    "現在は implementation task progress gate が未完了です。"
                    "この場合の正本は WorkUnit の work_type ではなく、後続の "
                    "実装タスク進行契約 allowed_next_actions です。"
                    "特に unittest 失敗後は、同じ run_command を繰り返さず、"
                    "read_file once / targeted edit / unittest再実行の順に従ってください。\n"
                )
            else:
                frame_block += (
                    "\n重要: この子フレームは accepted PlanRecord の WorkUnit です。"
                    "PlanRecord が分解の正本なので、この子フレーム内で decompose_tasks/open_child_frame は使えません。"
                    "work_type と success_evidence に対応する直接ツールを1つ実行してください。"
                    "edit なら write_file/replace_text、run_test なら run_command、inspect なら list_files/read_file/search_code を優先してください。"
                    "WorkUnit の success_evidence が満たされた時だけ return_to_parent してください。\n"
                )
        if frame.depth > 0 and has_child_return:
            frame_block += (
                "\n重要: このフレームは直近で child_return を受け取り済みです。"
                "新しい局所問題を開かず、親が判断できる summary/findings をまとめて return_to_parent してください。\n"
            )
    prompt = (
        f"現在の目標:\n{goal_part}\n\n"
        f"現在のユーザー依頼:\n{user_message.strip() or '(直近の依頼なし)'}\n\n"
        f"LLM作業ディレクトリ:\n{self.execution_root}\n"
        "このターンのファイル操作とコマンド実行は、この専用 workspace 配下で行われます。相対パスを使ってください。\n\n"
        + frame_block
        + "\n"
        "直近のセッションイベント:\n"
        + ("\n".join(rendered_events) if rendered_events else "(履歴なし)")
        + "\n\n直近のリフレクション (失敗からの教訓):\n"
        + self._reflection_prompt_block(user_message=user_message)
        + "\n\n編集方針:\n"
        + (
            "現在は計画契約フェーズです。create_plan 以外の tool は選ばないでください。"
            "PlanRecord には実装コード本文、テストコード本文、write_file/append_file/replace_text を入れないでください。"
            "WorkUnit.first_action は list_files/read_file/search_code/run_command のいずれかだけにしてください。"
            if planning_contract_phase
            else self._output_budget_prompt(suppress_frame_operations=suppress_frame_operations)
        )
    )
    if current_phase == "DELIBERATE":
        prompt += "\n\n" + self._build_deliberation_note(user_message=user_message, steps=steps or [], recent_events=recent_events)
    elif current_phase in {"PLANNING_REQUIRED", "PLAN_REVISION"}:
        profile = profile_problem(user_message)
        prompt += (
            f"\n\n【計画契約フェーズ（{current_phase}）】\n"
            "このタスクは実行前に PlanRecord が必要です。write_file/run_command/finish へ進まず、"
            "tool_name=create_plan を返してください。\n"
            "このフェーズの出力は小さい計画契約だけです。実装・テスト・長いcontent・コード断片は一切出力しないでください。"
            "edit WorkUnit でも first_action は list_files か read_file にし、実際の write_file は PLAN_EXECUTION の子フレームで返してください。"
            "run_test WorkUnit の first_action は run_command にしてください。"
            "最小形は work_units を inspect/edit/run_test の3件程度にし、各 first_action は tool と args だけにしてください。\n"
            f"ProblemProfile: {json.dumps(profile, ensure_ascii=False)}\n"
            f"PlanRecord JSON Schema: {json.dumps(PLAN_RECORD_SCHEMA, ensure_ascii=False, separators=(',', ':'))}\n"
            "PlanRecord は profile, strategy, work_units, verification_contract, status, revision_count を含めてください。"
            "profile.strategy が state_space_search 等の専門戦略なら plan.strategy も同じ値にしてください。"
            "state_space_search を選ぶ場合、verification_contract に state/action/goal/legal_move_validator/"
            "solvability_check/heuristic_or_search_policy/final_verifier を必ず含めてください。"
            "WorkUnit の first_action は実行可能な具体アクションにし、pass/TODO/暫定return/未実装コメントを含む実装やテストを入れないでください。"
            f"{PLAN_RECORD_SHAPE_HINT}"
        )
    elif current_phase == "PLAN_EXECUTION":
        prompt += (
            "\n\n【計画実行フェーズ（PLAN_EXECUTION）】\n"
            "accepted PlanRecord の WorkUnit を既存の child frame 契約として順に実行してください。"
            "子フレーム内では first_action を優先し、PlanRecord WorkUnit の中で再度 decompose_tasks/open_child_frame を使わないでください。"
            "証拠が揃ったら return_to_parent してください。"
        )
    elif current_phase == "PLANNING":
        prompt += (
            "\n\n【計画フェーズ（PLANNING）】\n"
            "複雑なタスクが開始されました。最初のアクションとして、焦って修正や実行を行うのではなく、"
            "まずは関連するファイル構成の確認（list_files）や、コードの検索（search_code）を行い、"
            "現状の把握に努めてください。その後、段階的な実行計画を立ててください。"
        )
    elif current_phase == "RECOVER_FROM_TOOL_FAILURE":
        prompt += (
            "\n\n【失敗回復フェーズ（RECOVER_FROM_TOOL_FAILURE）】\n"
            "直前の tool_result / system_note の failure_type, allowed_next_actions, suggested_fix を最優先してください。"
            "同じ tool_args や同じ失敗する局所編集を繰り返してはいけません。"
            "allowed_next_actions が write_file のみ、または write_file を明示している場合は、"
            "replace_text に固執せず、現在ファイルを踏まえた完全な有効ソースを write_file してください。"
            "回復後は unittest や requested command を実行して、契約未達を検証してください。"
        )

    prompt += "\n\n最適と思われる次の一手を決定してください。"
    if extra_prompt:
        prompt += f"\n\n実行の優先設定:\n{extra_prompt.strip()}"
    return prompt


def _render_action_context_events(
    self,
    *,
    recent_events: list[dict[str, Any]],
    steps: list[dict[str, Any]],
    user_message: str,
) -> list[str]:
    current_user = str(user_message or "").strip()
    useful_types = {"user_message", "tool_call", "tool_result", "system_note", "planning_note", "task_plan", "child_return"}
    useful_system_codes = {
        "",
        "llm_output_issue",
        "finish_blocked",
        "command_failed",
        "command_blocked",
        "edit_blocked",
        "implementation_task_progress_blocked",
        "implementation_task_initial_placeholder_loop",
        "observation_blocked",
        "work_package_invalid",
        "decompose_tasks_blocked",
        "open_child_frame_blocked",
        "frame_open_blocked",
        "frame_return_blocked",
        "work_unit_success_evidence_blocked",
        "controller_finish",
        "grounding_judge",
        "validation_failure_consultant",
        "semantic_implementation_review",
        "step_limit_reached",
        "finish_acceptance",
        "first_action_required",
        "plan_acceptance_blocked",
        "plan_acceptance_review",
        "planner_action_blocked",
        "plan_record_autorepaired",
        "plan_revision_returned_to_root",
        "plan_execution_continue",
        "plan_execution_paused_for_progress",
        "workspace_resume",
        "completion_contract_recovery",
        "contract_incomplete",
        "judge_fallback_finish",
        "implementation_task_semantic_review_ignored",
        "blocked_action_ignored",
        "command_similarity_warning",
        "step_limit_final_gate",
        "step_limit_plan_incomplete",
        "decompose_tasks_skipped_satisfied",
        "implementation_task_progress",
        "operator_interrupt",
    }
    selected: list[dict[str, Any]] = []
    for event in recent_events:
        event_type = str(event.get("type") or "")
        if event_type not in useful_types:
            continue
        if event_type == "user_message" and current_user and str(event.get("content") or "").strip() != current_user:
            continue
        if event_type == "system_note" and str(event.get("code") or "") not in useful_system_codes:
            continue
        selected.append(event)
    latest_judge_feedback = ""
    for event in reversed(selected):
        if str(event.get("type") or "") != "system_note":
            continue
        details = event.get("details") if isinstance(event.get("details"), dict) else {}
        feedback = ""
        if str(event.get("code") or "") == "grounding_judge":
            feedback = self._judge_feedback_context_text(details)
        elif str(event.get("code") or "") == "finish_blocked":
            judge = details.get("judge") if isinstance(details.get("judge"), dict) else {}
            feedback = self._judge_feedback_context_text(judge)
        elif str(event.get("code") or "") == "finish_acceptance":
            feedback = self._judge_feedback_context_text(details.get("review") if isinstance(details.get("review"), dict) else {})
        if feedback:
            latest_judge_feedback = feedback
            break
    selected = selected[-12:]
    rendered: list[str] = []
    selected_has_judge_feedback = False
    for event in selected:
        event_type = str(event.get("type") or "")
        line = f"[{event_type}] "
        if event_type in {"user_message", "system_note", "planning_note"}:
            line += self._compact_context_text(str(event.get("content") or ""), limit=700)
            if event_type == "system_note" and str(event.get("code") or "") == "work_package_invalid":
                details = event.get("details") if isinstance(event.get("details"), dict) else {}
                issue_text = "; ".join(str(issue) for issue in details.get("issues") or [] if str(issue).strip())
                suggested_fix = str(details.get("suggested_fix") or "")
                if issue_text:
                    line += f" | 具体的な不足: {self._compact_context_text(issue_text, limit=500)}"
                if suggested_fix:
                    line += f" | 次はこの不足を解消: {self._compact_context_text(suggested_fix, limit=500)}"
            if event_type == "system_note" and str(event.get("code") or "") == "llm_output_issue":
                details = event.get("details") if isinstance(event.get("details"), dict) else {}
                parse_issue = str(event.get("reason_code") or details.get("parse_issue") or "").strip()
                phase = str(details.get("current_phase") or "").strip()
                failure_type = str(details.get("failure_type") or "").strip()
                blocked_by = str(details.get("blocked_by") or "").strip()
                raw_is_json = details.get("raw_output_is_machine_json")
                schema_ok = details.get("schema_validation_ok")
                schema_validation = details.get("schema_validation") if isinstance(details.get("schema_validation"), dict) else {}
                schema_errors = schema_validation.get("errors") if isinstance(schema_validation.get("errors"), list) else []
                allowed_tool_names = details.get("allowed_tool_names") if isinstance(details.get("allowed_tool_names"), list) else []
                allowed_next_actions = details.get("allowed_next_actions") if isinstance(details.get("allowed_next_actions"), list) else []
                missing_requirements = details.get("missing_requirements") if isinstance(details.get("missing_requirements"), list) else []
                suggested_fix = str(details.get("suggested_fix") or "").strip()
                next_required_action = str(details.get("next_required_action") or "").strip()
                stream_metadata = details.get("stream_metadata") if isinstance(details.get("stream_metadata"), dict) else {}
                client_abort_reason = str(stream_metadata.get("client_abort_reason") or "").strip()
                accumulated_content_chars = stream_metadata.get("accumulated_content_chars")
                raw_preview = str(details.get("combined_text") or details.get("raw_text") or "").strip()
                raw_tail = str(details.get("combined_text_tail") or details.get("raw_text_tail") or "").strip()
                if parse_issue:
                    line += f" | parse_issue: {self._compact_context_text(parse_issue, limit=180)}"
                if failure_type:
                    line += f" | failure_type: {self._compact_context_text(failure_type, limit=180)}"
                if blocked_by:
                    line += f" | blocked_by: {self._compact_context_text(blocked_by, limit=180)}"
                if phase:
                    line += f" | current_phase: {self._compact_context_text(phase, limit=160)}"
                if client_abort_reason:
                    line += f" | stream_abort_reason: {self._compact_context_text(client_abort_reason, limit=180)}"
                if accumulated_content_chars is not None:
                    line += f" | accumulated_content_chars: {accumulated_content_chars}"
                if missing_requirements:
                    line += (
                        " | missing_requirements: "
                        + self._compact_context_text(json.dumps(missing_requirements[:8], ensure_ascii=False), limit=500)
                    )
                if raw_is_json is not None:
                    line += f" | raw_output_is_machine_json: {bool(raw_is_json)}"
                if schema_ok is not None:
                    line += f" | schema_validation_ok: {bool(schema_ok)}"
                if schema_errors:
                    line += (
                        " | schema_validation_errors: "
                        + self._compact_context_text(json.dumps(schema_errors[:6], ensure_ascii=False), limit=900)
                    )
                if allowed_tool_names:
                    line += (
                        " | allowed_tool_names: "
                        + self._compact_context_text(json.dumps(allowed_tool_names, ensure_ascii=False), limit=500)
                    )
                if allowed_next_actions:
                    line += (
                        " | allowed_next_actions: "
                        + self._compact_context_text(json.dumps(allowed_next_actions, ensure_ascii=False), limit=700)
                    )
                if suggested_fix:
                    line += f" | suggested_fix: {self._compact_context_text(suggested_fix, limit=700)}"
                if next_required_action:
                    line += f" | next_required_action: {self._compact_context_text(next_required_action, limit=700)}"
                if raw_preview:
                    line += f" | raw_output_preview: {self._compact_context_text(raw_preview, limit=500)}"
                if raw_tail and raw_tail != raw_preview:
                    line += f" | raw_output_tail: {self._compact_context_text(raw_tail, limit=500)}"
            if event_type == "system_note" and str(event.get("code") or "") == "command_failed":
                details = event.get("details") if isinstance(event.get("details"), dict) else {}
                command = str(details.get("command") or "")
                returncode = details.get("returncode")
                traceback_summary = str(details.get("traceback_summary") or "")
                stderr_tail = str(details.get("stderr_tail") or "")
                stdout_tail = str(details.get("stdout_tail") or "")
                error = str(details.get("error") or "")
                if command:
                    line += f" | command: {self._compact_context_text(command, limit=260)}"
                if returncode is not None:
                    line += f" | returncode: {returncode}"
                if traceback_summary:
                    line += f" | traceback: {self._compact_context_text(traceback_summary, limit=500)}"
                if stderr_tail:
                    line += f" | stderr_tail: {self._compact_context_text(stderr_tail, limit=900)}"
                if stdout_tail:
                    line += f" | stdout_tail: {self._compact_context_text(stdout_tail, limit=900)}"
                if error:
                    line += f" | error: {self._compact_context_text(error, limit=500)}"
            if event_type == "system_note" and str(event.get("code") or "") == "edit_blocked":
                details = event.get("details") if isinstance(event.get("details"), dict) else {}
                reason_code = str(event.get("reason_code") or details.get("reason_code") or "")
                failure_type = str(details.get("failure_type") or details.get("previous_failure_type") or "")
                blocked_tool = str(details.get("blocked_tool") or "")
                path = str(details.get("path") or "")
                allowed_next_actions = ", ".join(str(item) for item in details.get("allowed_next_actions") or [] if str(item).strip())
                suggested_fix = str(details.get("suggested_fix") or "")
                syntax_error = str(details.get("syntax_error") or "")
                blocked_by = str(details.get("blocked_by") or "runtime_edit_validation")
                next_required_action = str(details.get("next_required_action") or suggested_fix or "")
                if reason_code:
                    line += f" | reason_code: {self._compact_context_text(reason_code, limit=160)}"
                if failure_type:
                    line += f" | failure_type: {self._compact_context_text(failure_type, limit=160)}"
                line += f" | blocked_by: {blocked_by}"
                if blocked_tool:
                    line += f" | blocked_tool: {self._compact_context_text(blocked_tool, limit=120)}"
                if path:
                    line += f" | path: {self._compact_context_text(path, limit=180)}"
                if syntax_error:
                    line += f" | 前回の構文エラー: {self._compact_context_text(syntax_error, limit=240)}"
                if allowed_next_actions:
                    line += f" | 許可される次アクション: {self._compact_context_text(allowed_next_actions, limit=240)}"
                if suggested_fix:
                    line += f" | 次に必要: {self._compact_context_text(suggested_fix, limit=500)}"
                if next_required_action and next_required_action != suggested_fix:
                    line += f" | next_required_action: {self._compact_context_text(next_required_action, limit=500)}"
            if event_type == "system_note" and str(event.get("code") or "") == "observation_blocked":
                details = event.get("details") if isinstance(event.get("details"), dict) else {}
                missing = "; ".join(str(item) for item in details.get("missing_requirements") or [] if str(item).strip())
                allowed_next_actions = ", ".join(str(item) for item in details.get("allowed_next_actions") or [] if str(item).strip())
                suggested_fix = str(details.get("suggested_fix") or "")
                blocked_by = str(details.get("blocked_by") or "runtime_observation_gate")
                next_required_action = str(details.get("next_required_action") or suggested_fix or "")
                line += f" | blocked_by: {blocked_by}"
                if missing:
                    line += f" | 未達条件: {self._compact_context_text(missing, limit=320)}"
                if allowed_next_actions:
                    line += f" | 許可される次アクション: {self._compact_context_text(allowed_next_actions, limit=240)}"
                if suggested_fix:
                    line += f" | 次に必要: {self._compact_context_text(suggested_fix, limit=500)}"
                if next_required_action and next_required_action != suggested_fix:
                    line += f" | next_required_action: {self._compact_context_text(next_required_action, limit=500)}"
            if event_type == "system_note" and str(event.get("code") or "") == "command_blocked":
                details = event.get("details") if isinstance(event.get("details"), dict) else {}
                command = str(details.get("command") or "")
                allowed_next_actions = ", ".join(str(item) for item in details.get("allowed_next_actions") or [] if str(item).strip())
                suggested_fix = str(details.get("suggested_fix") or "")
                blocked_by = str(details.get("blocked_by") or "runtime_command_gate")
                next_required_action = str(details.get("next_required_action") or suggested_fix or "")
                line += f" | blocked_by: {blocked_by}"
                if command:
                    line += f" | blocked_command: {self._compact_context_text(command, limit=260)}"
                if allowed_next_actions:
                    line += f" | 許可される次アクション: {self._compact_context_text(allowed_next_actions, limit=320)}"
                if suggested_fix:
                    line += f" | 次に必要: {self._compact_context_text(suggested_fix, limit=600)}"
                if next_required_action and next_required_action != suggested_fix:
                    line += f" | next_required_action: {self._compact_context_text(next_required_action, limit=600)}"
            if event_type == "system_note" and str(event.get("code") or "") == "implementation_task_progress_blocked":
                details = event.get("details") if isinstance(event.get("details"), dict) else {}
                state = details.get("state") if isinstance(details.get("state"), dict) else {}
                reason_code = str(event.get("reason_code") or details.get("reason_code") or "")
                failure_type = str(details.get("failure_type") or "")
                blocked_tool = str(details.get("blocked_tool") or "")
                path = str(details.get("path") or "")
                phase = str(state.get("phase") or details.get("phase") or "")
                missing_items = list(details.get("missing_requirements") or state.get("missing_requirements") or [])
                missing = "; ".join(str(item) for item in missing_items if str(item).strip())
                candidate_missing = "; ".join(
                    str(item)
                    for item in details.get("candidate_missing_requirements") or []
                    if str(item).strip()
                )
                repair_hints = details.get("repair_hints") if isinstance(details.get("repair_hints"), list) else []
                repair_hint_texts: list[str] = []
                for hint in repair_hints[:3]:
                    if not isinstance(hint, dict):
                        repair_hint_texts.append(str(hint))
                        continue
                    line_no = str(hint.get("line") or hint.get("line_number") or "").strip()
                    current_text = str(hint.get("current_text") or hint.get("text") or "").strip()
                    reason = str(hint.get("reason") or "").strip()
                    suggested = str(hint.get("suggested_new_text") or hint.get("suggested_fix") or "").strip()
                    parts = []
                    if line_no:
                        parts.append(f"line {line_no}")
                    if current_text:
                        parts.append(f"current={current_text}")
                    if reason:
                        parts.append(f"reason={reason}")
                    if suggested:
                        parts.append(f"suggested={suggested}")
                    if parts:
                        repair_hint_texts.append(" / ".join(parts))
                allowed_next_actions = ", ".join(str(item) for item in details.get("allowed_next_actions") or state.get("allowed_next_actions") or [] if str(item).strip())
                suggested_fix = str(details.get("suggested_fix") or "")
                blocked_by = str(details.get("blocked_by") or "implementation_task_progress_controller")
                next_required_action = str(details.get("next_required_action") or suggested_fix or "")
                fixture_items = details.get("fixture_review_items") if isinstance(details.get("fixture_review_items"), list) else []
                if reason_code:
                    line += f" | reason_code: {self._compact_context_text(reason_code, limit=180)}"
                if failure_type:
                    line += f" | failure_type: {self._compact_context_text(failure_type, limit=180)}"
                line += f" | blocked_by: {blocked_by}"
                if blocked_tool:
                    line += f" | blocked_tool: {self._compact_context_text(blocked_tool, limit=120)}"
                if path:
                    line += f" | path: {self._compact_context_text(path, limit=180)}"
                if phase:
                    line += f" | 実装タスク進行phase: {phase}"
                if missing:
                    line += f" | 未達条件: {self._compact_context_text(missing, limit=320)}"
                if candidate_missing:
                    line += f" | 提案後も残る未達: {self._compact_context_text(candidate_missing, limit=420)}"
                if repair_hint_texts:
                    line += f" | 修復ヒント: {self._compact_context_text('; '.join(repair_hint_texts), limit=900)}"
                if allowed_next_actions:
                    line += f" | 許可される次アクション: {self._compact_context_text(allowed_next_actions, limit=320)}"
                if suggested_fix:
                    line += f" | 次に必要: {self._compact_context_text(suggested_fix, limit=600)}"
                if next_required_action and next_required_action != suggested_fix:
                    line += f" | next_required_action: {self._compact_context_text(next_required_action, limit=600)}"
                if fixture_items:
                    line += (
                        " | テストfixtureレビュー: 実装ファイルを編集しないで、対象test fixtureの期待値を修正してください。"
                        f" 正しい期待値: {self._compact_context_text(json.dumps(fixture_items[:3], ensure_ascii=False), limit=900)}"
                    )
            if event_type == "system_note" and str(event.get("code") or "") == "step_limit_reached":
                details = event.get("details") if isinstance(event.get("details"), dict) else {}
                missing = "; ".join(str(item) for item in details.get("missing_requirements") or [] if str(item).strip())
                if missing:
                    line += f" | 未達条件: {self._compact_context_text(missing, limit=500)}"
                line += " | 次に必要: 未達条件を満たす具体ツール実行を優先してください。"
            if event_type == "system_note" and str(event.get("code") or "") == "finish_blocked":
                details = event.get("details") if isinstance(event.get("details"), dict) else {}
                missing_items = details.get("missing") or details.get("missing_requirements") or []
                missing = "; ".join(str(item) for item in missing_items if str(item).strip())
                limitations = "; ".join(str(item) for item in details.get("limitations") or [] if str(item).strip())
                suggested_fix = str(details.get("suggested_fix") or "")
                allowed_next_actions = ", ".join(str(item) for item in details.get("allowed_next_actions") or [] if str(item).strip())
                next_required_action = str(details.get("next_required_action") or "")
                blocked_by = str(details.get("blocked_by") or "")
                judge_feedback = self._judge_feedback_context_text(details.get("judge") if isinstance(details.get("judge"), dict) else details.get("review"))
                if blocked_by:
                    line += f" | blocked_by: {self._compact_context_text(blocked_by, limit=180)}"
                if missing:
                    line += f" | 未達条件: {self._compact_context_text(missing, limit=400)}"
                if limitations:
                    line += f" | ブロック理由: {self._compact_context_text(limitations, limit=500)}"
                if judge_feedback:
                    selected_has_judge_feedback = True
                    line += f" | judge LLMからの回答: {self._compact_context_text(judge_feedback, limit=900)}"
                if suggested_fix:
                    line += f" | 次に必要: {self._compact_context_text(suggested_fix, limit=500)}"
                if allowed_next_actions:
                    line += f" | 許可される次アクション: {self._compact_context_text(allowed_next_actions, limit=300)}"
                if next_required_action:
                    line += f" | next_required_action: {self._compact_context_text(next_required_action, limit=500)}"
            if event_type == "system_note" and str(event.get("code") or "") in {"grounding_judge", "finish_acceptance"}:
                details = event.get("details") if isinstance(event.get("details"), dict) else {}
                judge_feedback = self._judge_feedback_context_text(details)
                if judge_feedback:
                    selected_has_judge_feedback = True
                    line += f" | judge LLMからの回答: {self._compact_context_text(judge_feedback, limit=900)}"
            if event_type == "system_note" and str(event.get("code") or "") == "first_action_required":
                details = event.get("details") if isinstance(event.get("details"), dict) else {}
                expected_tool = str(details.get("expected_tool") or "")
                expected_args = details.get("expected_args") if isinstance(details.get("expected_args"), dict) else {}
                requested_tool = str(details.get("requested_tool") or "")
                if requested_tool:
                    line += f" | blocked_tool: {self._compact_context_text(requested_tool, limit=120)}"
                if expected_tool:
                    line += (
                        " | 次に必要なfirst_action: "
                        + self._compact_context_text(
                            json.dumps({"tool_name": expected_tool, "tool_args": expected_args}, ensure_ascii=False),
                            limit=700,
                        )
                    )
            if event_type == "system_note" and str(event.get("code") or "") == "plan_acceptance_blocked":
                details = event.get("details") if isinstance(event.get("details"), dict) else {}
                issue_text = "; ".join(str(issue) for issue in details.get("issues") or [] if str(issue).strip())
                suggested_fix = str(details.get("suggested_fix") or "")
                allowed_next_actions = self._compact_context_text(
                    json.dumps(details.get("allowed_next_actions") or [], ensure_ascii=False),
                    limit=700,
                )
                review = details.get("review") if isinstance(details.get("review"), dict) else {}
                rationale = str(review.get("rationale") or "")
                if issue_text:
                    line += f" | 具体的な不足: {self._compact_context_text(issue_text, limit=500)}"
                if rationale:
                    line += f" | reviewer根拠: {self._compact_context_text(rationale, limit=500)}"
                if allowed_next_actions != "[]":
                    line += f" | 許可される次アクション: {allowed_next_actions}"
                if suggested_fix:
                    line += f" | 次に必要: {self._compact_context_text(suggested_fix, limit=500)}"
            if event_type == "system_note" and str(event.get("code") or "") == "completion_contract_recovery":
                details = event.get("details") if isinstance(event.get("details"), dict) else {}
                tool = str(details.get("tool_name") or "")
                args = details.get("tool_args") if isinstance(details.get("tool_args"), dict) else {}
                if tool:
                    line += (
                        " | controller実行: "
                        + self._compact_context_text(json.dumps({"tool_name": tool, "tool_args": args}, ensure_ascii=False), limit=700)
                    )
            if event_type == "system_note" and str(event.get("code") or "") in {"contract_incomplete", "step_limit_final_gate"}:
                details = event.get("details") if isinstance(event.get("details"), dict) else {}
                missing = "; ".join(str(item) for item in details.get("missing_requirements") or [] if str(item).strip())
                if missing:
                    line += f" | 未達条件: {self._compact_context_text(missing, limit=500)}"
            if event_type == "system_note" and str(event.get("code") or "") in {
                "implementation_task_semantic_review_ignored",
                "blocked_action_ignored",
            }:
                details = event.get("details") if isinstance(event.get("details"), dict) else {}
                blocked_reason = str(details.get("blocked_reason_code") or details.get("reason_code") or "")
                blocked_tool = str(details.get("blocked_tool") or "")
                allowed_next_actions = ", ".join(str(item) for item in details.get("allowed_next_actions") or [] if str(item).strip())
                suggested_fix = str(details.get("suggested_fix") or "")
                if blocked_tool:
                    line += f" | blocked_tool: {self._compact_context_text(blocked_tool, limit=120)}"
                if blocked_reason:
                    line += f" | blocked_reason: {self._compact_context_text(blocked_reason, limit=240)}"
                if allowed_next_actions:
                    line += f" | 許可される次アクション: {self._compact_context_text(allowed_next_actions, limit=320)}"
                if suggested_fix:
                    line += f" | 次に必要: {self._compact_context_text(suggested_fix, limit=500)}"
            if event_type == "system_note" and str(event.get("code") or "") == "command_similarity_warning":
                details = event.get("details") if isinstance(event.get("details"), dict) else {}
                command = str(details.get("command") or "")
                if command:
                    line += f" | 類似コマンド: {self._compact_context_text(command, limit=260)}"
            if event_type == "system_note" and str(event.get("code") or "") == "implementation_task_progress":
                details = event.get("details") if isinstance(event.get("details"), dict) else {}
                phase = str(details.get("phase") or "")
                missing = "; ".join(str(item) for item in details.get("missing_requirements") or [] if str(item).strip())
                allowed_next_actions = ", ".join(str(item) for item in details.get("allowed_next_actions") or [] if str(item).strip())
                if phase:
                    line += f" | phase: {self._compact_context_text(phase, limit=160)}"
                if missing:
                    line += f" | 未達条件: {self._compact_context_text(missing, limit=320)}"
                if allowed_next_actions:
                    line += f" | 許可される次アクション: {self._compact_context_text(allowed_next_actions, limit=320)}"
            if event_type == "system_note" and str(event.get("code") or "") == "decompose_tasks_skipped_satisfied":
                details = event.get("details") if isinstance(event.get("details"), dict) else {}
                skipped = details.get("skipped_tasks") if isinstance(details.get("skipped_tasks"), list) else []
                if skipped:
                    line += " | skipped_tasks: " + self._compact_context_text(json.dumps(skipped[:3], ensure_ascii=False), limit=700)
            if event_type == "system_note" and str(event.get("code") or "") == "operator_interrupt":
                details = event.get("details") if isinstance(event.get("details"), dict) else {}
                reason = str(details.get("operator_reason") or "")
                phase = str(details.get("current_phase") or "")
                tool = str(details.get("current_tool") or "")
                if reason:
                    line += f" | operator_reason: {self._compact_context_text(reason, limit=300)}"
                if phase:
                    line += f" | stopped_phase: {self._compact_context_text(phase, limit=160)}"
                if tool:
                    line += f" | stopped_tool: {self._compact_context_text(tool, limit=120)}"
            if event_type == "system_note" and str(event.get("code") or "") == "validation_failure_consultant":
                details = event.get("details") if isinstance(event.get("details"), dict) else {}
                advice = str(details.get("advice") or event.get("content") or "")
                failed_command = str(details.get("failed_command") or "")
                if failed_command:
                    line += f" | 失敗した検証: {self._compact_context_text(failed_command, limit=240)}"
                if advice:
                    line += f" | 相談役LLMの平文レビュー: {self._compact_context_text(advice, limit=1800)}"
            if event_type == "system_note" and str(event.get("code") or "") == "semantic_implementation_review":
                details = event.get("details") if isinstance(event.get("details"), dict) else {}
                review = str(details.get("review") or event.get("content") or "")
                implementation_paths = ", ".join(str(item) for item in details.get("implementation_paths") or [] if str(item).strip())
                test_paths = ", ".join(str(item) for item in details.get("test_paths") or [] if str(item).strip())
                fixture_items = details.get("fixture_review_items") if isinstance(details.get("fixture_review_items"), list) else []
                if implementation_paths:
                    line += f" | 実装対象: {self._compact_context_text(implementation_paths, limit=240)}"
                if test_paths:
                    line += f" | テスト対象: {self._compact_context_text(test_paths, limit=240)}"
                if fixture_items:
                    line += (
                        " | 相談役LLM/Runtime oracle からのテストfixtureレビュー: "
                        "実装ファイルを編集しないで、対象test fixtureの期待値を修正してください。"
                        f" 正しい期待値: {self._compact_context_text(json.dumps(fixture_items[:4], ensure_ascii=False), limit=1200)}"
                    )
                if review:
                    review_source = str(details.get("review_source") or "consultant")
                    review_label = "相談役LLMからの実装レビュー" if review_source == "consultant" else "runtime観測レビュー"
                    line += f" | {review_label}: {self._compact_context_text(review, limit=1100)}"
            if event_type == "system_note":
                details = event.get("details") if isinstance(event.get("details"), dict) else {}
                reason_code = str(event.get("reason_code") or details.get("reason_code") or "")
                failure_type = str(details.get("failure_type") or details.get("previous_failure_type") or "")
                blocked_by = str(details.get("blocked_by") or "")
                blocked_tool = str(details.get("blocked_tool") or "")
                path = str(details.get("path") or "")
                expected_shape = str(details.get("expected_shape") or "")
                missing = "; ".join(str(item) for item in details.get("missing_requirements") or [] if str(item).strip())
                allowed_next_actions = ", ".join(str(item) for item in details.get("allowed_next_actions") or [] if str(item).strip())
                suggested_fix = str(details.get("suggested_fix") or "")
                next_required_action = str(details.get("next_required_action") or "")
                if reason_code and "reason_code:" not in line:
                    line += f" | reason_code: {self._compact_context_text(reason_code, limit=180)}"
                if failure_type and "failure_type:" not in line:
                    line += f" | failure_type: {self._compact_context_text(failure_type, limit=180)}"
                if blocked_by and "blocked_by:" not in line:
                    line += f" | blocked_by: {self._compact_context_text(blocked_by, limit=180)}"
                if blocked_tool and "blocked_tool:" not in line:
                    line += f" | blocked_tool: {self._compact_context_text(blocked_tool, limit=120)}"
                if path and "path:" not in line:
                    line += f" | path: {self._compact_context_text(path, limit=180)}"
                if missing and "未達条件:" not in line:
                    line += f" | 未達条件: {self._compact_context_text(missing, limit=420)}"
                if allowed_next_actions and "許可される次アクション:" not in line:
                    line += f" | 許可される次アクション: {self._compact_context_text(allowed_next_actions, limit=360)}"
                if suggested_fix and "次に必要:" not in line:
                    line += f" | 次に必要: {self._compact_context_text(suggested_fix, limit=600)}"
                if str(event.get("code") or "") == "planner_action_blocked" and expected_shape and "PlanRecord正本:" not in line:
                    line += f" | PlanRecord正本: {self._compact_context_text(expected_shape, limit=1500)}"
                if next_required_action and "next_required_action:" not in line:
                    line += f" | next_required_action: {self._compact_context_text(next_required_action, limit=600)}"
        elif event_type == "tool_call":
            tool_args = dict(event.get("tool_args") or {})
            if "content" in tool_args:
                tool_args["content"] = self._compact_context_text(str(tool_args.get("content") or ""), limit=220)
            if "new_text" in tool_args:
                tool_args["new_text"] = self._compact_context_text(str(tool_args.get("new_text") or ""), limit=220)
            line += f"{event.get('tool_name')} args={json.dumps(tool_args, ensure_ascii=False)}"
        elif event_type == "tool_result":
            line += self._render_tool_result_context(event)
        elif event_type == "task_plan":
            line += self._compact_context_text(
                json.dumps(
                    {
                        "tasks": event.get("tasks") or [],
                        "rationale": event.get("rationale") or "",
                    },
                    ensure_ascii=False,
                ),
                limit=1000,
            )
        elif event_type == "child_return":
            line += self._compact_context_text(
                json.dumps(
                    {
                        "summary": event.get("content") or "",
                        "return_payload": event.get("return_payload") or {},
                        "next_child_task": event.get("next_child_task") or None,
                    },
                    ensure_ascii=False,
                ),
                limit=1000,
            )
        rendered.append(line)
    if latest_judge_feedback and not selected_has_judge_feedback:
        rendered.append(
            "[judge_feedback] judge LLMからの直近回答: "
            + self._compact_context_text(latest_judge_feedback, limit=900)
        )
    if steps:
        rendered.append("[current_steps] " + self._compact_context_text(json.dumps(steps[-4:], ensure_ascii=False), limit=1400))
    return rendered


def _compact_context_text(self, text: str, *, limit: int) -> str:
    clean = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(clean) <= limit:
        return clean
    return f"{clean[:limit]}... [truncated {len(clean) - limit} chars]"


def _tail_text(text: str, *, limit: int) -> str:
    source = str(text or "")
    if len(source) <= limit:
        return source
    return f"... [truncated head {len(source) - limit} chars]\n{source[-limit:]}"


def _run_command_traceback_summary(output: str, *, max_file_lines: int = 8) -> str:
    text = str(output or "")
    lines = text.splitlines()
    file_lines: list[str] = []
    error_lines: list[str] = []
    file_pattern = re.compile(r'File "([^"]+)", line (\d+)(?:, in ([^\s]+))?')
    error_pattern = re.compile(
        r"(^|\b)(AssertionError|SyntaxError|ImportError|ModuleNotFoundError|NameError|TypeError|ValueError|"
        r"IndexError|KeyError|AttributeError|RuntimeError|Exception|Error):"
    )
    for line in lines:
        match = file_pattern.search(line)
        if match:
            path = match.group(1)
            line_no = match.group(2)
            func = match.group(3) or ""
            file_lines.append(f"{path}:{line_no}" + (f" in {func}" if func else ""))
        stripped = line.strip()
        if stripped and error_pattern.search(stripped):
            error_lines.append(stripped)
    parts: list[str] = []
    if file_lines:
        deduped_file_lines: list[str] = []
        for item in file_lines:
            if item not in deduped_file_lines:
                deduped_file_lines.append(item)
        parts.append("traceback_file_lines=" + "; ".join(deduped_file_lines[-max_file_lines:]))
    if error_lines:
        parts.append("last_exception_line=" + error_lines[-1])
    return " | ".join(parts)


def _render_run_command_result_context(self, payload: dict[str, Any]) -> str:
    command = str(payload.get("command") or "").strip()
    returncode = payload.get("returncode")
    cwd = str(payload.get("cwd") or "").strip()
    stdout = str(payload.get("stdout") or "")
    stderr = str(payload.get("stderr") or "")
    error = str(payload.get("error") or "").strip()
    failure_type = str(payload.get("failure_type") or "").strip()
    blocked_by = str(payload.get("blocked_by") or "").strip()
    timeout_seconds = payload.get("timeout_seconds")
    allowed_next_actions = payload.get("allowed_next_actions") or []
    suggested_fix = str(payload.get("suggested_fix") or "").strip()
    next_required_action = str(payload.get("next_required_action") or "").strip()
    output = "\n".join(part for part in [stderr, stdout, error] if part)
    fields = [
        f"run_command command={json.dumps(command, ensure_ascii=False)}",
        f"ok={bool(payload.get('ok'))}",
        f"returncode={returncode}",
    ]
    if cwd:
        fields.append(f"cwd={cwd}")
    if failure_type:
        fields.append(f"failure_type={failure_type}")
    if blocked_by:
        fields.append(f"blocked_by={blocked_by}")
    if timeout_seconds is not None:
        fields.append(f"timeout_seconds={timeout_seconds}")
    summary = _run_command_traceback_summary(output)
    if summary:
        fields.append(summary)
    if allowed_next_actions:
        fields.append(
            "allowed_next_actions="
            + self._compact_context_text(json.dumps(allowed_next_actions, ensure_ascii=False), limit=700)
        )
    if suggested_fix:
        fields.append("suggested_fix=" + self._compact_context_text(suggested_fix, limit=700))
    if next_required_action:
        fields.append("next_required_action=" + self._compact_context_text(next_required_action, limit=500))
    rendered = " | ".join(fields)
    if stderr:
        rendered += "\nstderr_tail:\n" + _tail_text(stderr, limit=2600)
    if stdout:
        rendered += "\nstdout_tail:\n" + _tail_text(stdout, limit=1800)
    if error and not stderr:
        rendered += "\nerror:\n" + _tail_text(error, limit=1200)
    return rendered


def _render_tool_result_context(self, event: dict[str, Any]) -> str:
    tool_name = str(event.get("tool_name") or "")
    content = str(event.get("content") or "")
    if tool_name == "read_file":
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            return f"{tool_name} -> {self._compact_context_text(content, limit=1000)}"
        file_content = str(payload.get("content") or "")
        path = str(payload.get("path") or "")
        return (
            f"read_file {path} ->\n"
            f"{self._file_context_preview(file_content, limit=4000)}"
        )
    if tool_name == "run_command":
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            return f"{tool_name} -> {self._compact_context_text(content, limit=1600)}"
        if isinstance(payload, dict):
            return _render_run_command_result_context(self, payload)
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict) and not bool(payload.get("ok", True)):
        fields = [
            f"{tool_name} ok=False",
        ]
        path = str(payload.get("path") or "").strip()
        error = str(payload.get("error") or "").strip()
        failure_type = str(payload.get("failure_type") or "").strip()
        blocked_by = str(payload.get("blocked_by") or "").strip()
        syntax_error = str(payload.get("syntax_error") or "").strip()
        allowed_next_actions = payload.get("allowed_next_actions") or []
        suggested_fix = str(payload.get("suggested_fix") or payload.get("suggested_split_strategy") or "").strip()
        next_required_action = str(payload.get("next_required_action") or "").strip()
        if path:
            fields.append(f"path={path}")
        if failure_type:
            fields.append(f"failure_type={failure_type}")
        if blocked_by:
            fields.append(f"blocked_by={blocked_by}")
        if error:
            fields.append("error=" + self._compact_context_text(error, limit=500))
        if syntax_error:
            fields.append("syntax_error=" + self._compact_context_text(syntax_error, limit=300))
        if allowed_next_actions:
            fields.append(
                "allowed_next_actions="
                + self._compact_context_text(json.dumps(allowed_next_actions, ensure_ascii=False), limit=700)
            )
        if suggested_fix:
            fields.append("suggested_fix=" + self._compact_context_text(suggested_fix, limit=700))
        if next_required_action:
            fields.append("next_required_action=" + self._compact_context_text(next_required_action, limit=500))
        return " | ".join(fields)
    return f"{tool_name} -> {self._compact_context_text(content, limit=1000)}"


def _file_context_preview(self, content: str, *, limit: int) -> str:
    text = str(content or "")
    if len(text) <= limit:
        return text
    marker = f"\n... [middle truncated {len(text) - limit} chars; file tail follows] ...\n"
    head_limit = max(0, (limit - len(marker)) // 2)
    tail_limit = max(0, limit - len(marker) - head_limit)
    return text[:head_limit] + marker + text[-tail_limit:]


def _judge_feedback_context_text(self, trace: Any) -> str:
    """Convert judge trace into actor-readable feedback.

    Runtime keeps the structured judge result for decisions. The actor LLM
    should receive the judge as a second-opinion message, not as a schema it
    must repair or reinterpret.
    """
    if not isinstance(trace, dict) or not trace:
        return ""
    parsed = trace.get("parsed") if isinstance(trace.get("parsed"), dict) else {}
    parts: list[str] = []
    decision = str(trace.get("decision") or trace.get("status") or "").strip()
    if decision:
        parts.append(f"decision={decision}")
    reason = str(trace.get("reason") or trace.get("reason_code") or "").strip()
    if reason:
        parts.append(f"reason={reason}")
    missing = trace.get("missing") or trace.get("missing_requirements")
    if isinstance(missing, list) and missing:
        parts.append("missing=" + "; ".join(str(item) for item in missing if str(item).strip()))
    limitations = trace.get("limitations")
    if isinstance(limitations, list) and limitations:
        parts.append("limitations=" + "; ".join(str(item) for item in limitations if str(item).strip()))
    suggested_fix = str(trace.get("suggested_fix") or "").strip()
    if suggested_fix:
        parts.append(f"suggested_fix={suggested_fix}")
    if parsed:
        verdict = str(parsed.get("verdict") or parsed.get("status") or "").strip()
        reason_code = str(parsed.get("reason_code") or "").strip()
        rationale = str(parsed.get("rationale") or "").strip()
        mismatch = str(parsed.get("observed_mismatch") or "").strip()
        unsupported = parsed.get("unsupported_claims")
        if verdict:
            parts.append(f"verdict={verdict}")
        if reason_code:
            parts.append(f"reason={reason_code}")
        if rationale:
            parts.append(f"rationale={rationale}")
        if mismatch:
            parts.append(f"mismatch={mismatch}")
        if isinstance(unsupported, list) and unsupported:
            parts.append("unsupported_claims=" + "; ".join(str(item) for item in unsupported if str(item).strip()))
    else:
        raw = str(trace.get("content_text") or trace.get("raw_response") or trace.get("error") or "").strip()
        if raw:
            parts.append(f"raw_answer={raw}")
    return " / ".join(parts)


def _build_planning_note(self, *, user_message: str, goal_text: str) -> str:
    requested_commands = self._extract_requested_commands(user_message)
    if requested_commands:
        return (
            "計画: 要求されたコマンドを一つずつ実行し、各結果を確認してから次へ進みます。"
            "すべての要求がカバーされるまで完了しません。"
        )
    haystack = f"{goal_text}\n{user_message}".lower()
    if any(token in haystack for token in {"code", "file", "search", "コード", "ファイル", "検索"}):
        return (
            "計画: まず関連領域を検索または一覧表示し、必要なファイルのみを読み取ります。"
            "完了前に1ステップにつき1つの最小限の変更または結論を出します。"
        )
    return "計画: 最新の証拠を確認し、具体的な次のアクションを一つ実行して結果を検証します。その上で完了が妥当かどうかを判断します。"


def _build_deliberation_note(self, *, user_message: str, steps: list[dict[str, Any]], recent_events: list[dict[str, Any]]) -> str:
    reasons = self._deliberation_reasons(user_message=user_message, steps=steps, recent_events=recent_events)
    reason_text = "、".join(reasons)
    return (
        f"【熟考指示（DELIBERATE）】\n"
        f"重要：現在、プロセスの停滞が検知されています（理由：{reason_text}）。\n\n"
        "これまでの記録を冷静に分析し、なぜシステムに拒否されたのか、あるいはなぜ目的が未達成なのかを特定してください。"
        "単に同じアプローチを繰り返すのではなく、原因（例：余計なメタ情報の出力、ルートの誤り、JSON生成上限、巨大なtool_args.contentなど）を取り除いた新しい実行計画を立ててください。"
        "実装要求で pass/TODO/ellipsis/NotImplementedError/placeholder-only return を含む提案が拒否された場合は、同じ骨組みを再提出せず、"
        "意味のある tests/test_*.py で要求APIと完了条件を固定するか、passなしで動く最小完全実装だけを提出してください。"
        "長い出力が原因の場合は、前回出力の続きを文章で出さず、次の1 tool callで完了できる最小の write_file / append_file / replace_text だけを返してください。"
        "目的が大きすぎる場合は decompose_tasks で順序付きの局所目的に分け、子フレームの発見を return_to_parent して親フレームで次の一手を判断してください。"
    )


def _reflection_prompt_block(self, *, user_message: str = "") -> str:
    rows = read_jsonl(self.paths.reflections_path, limit=3)
    if not rows:
        return "(直近のリフレクションはありません)"
    lines = []
    for row in rows:
        reflection = str(row.get("reflection") or "").strip()
        if not reflection:
            continue
        if not self._reflection_relevant_to_user(reflection=reflection, user_message=user_message):
            continue
        failure_class = str(row.get("failure_class") or "").strip()
        prefix = f"[{failure_class}] " if failure_class else ""
        lines.append(prefix + reflection)
    return "\n".join(lines[-3:]) if lines else "(直近のリフレクションはありません)"


def _reflection_relevant_to_user(self, *, reflection: str, user_message: str) -> bool:
    current = re.sub(r"\s+", "", str(user_message or "")).lower()
    if not current:
        return True
    haystack = re.sub(r"\s+", "", str(reflection or "")).lower()
    if current in haystack:
        return True
    if len(current) <= 8:
        return False
    tokens = set(re.findall(r"[a-z0-9_]{3,}|[ぁ-んァ-ヶー一-龥]{2,}", current))
    for index in range(max(0, len(current) - 1)):
        pair = current[index : index + 2]
        if re.search(r"[ぁ-んァ-ヶー一-龥]", pair):
            tokens.add(pair)
    hits = sum(1 for token in tokens if token in haystack)
    return hits >= 2
