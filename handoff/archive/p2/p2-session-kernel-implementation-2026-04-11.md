# P2 session/action/result kernel 実装メモ

日付: 2026-04-11

## 実装したこと

- `legacy_phase_loop_v1` を残したまま、`session_action_loop_v1` を追加
- 新 kernel は `action -> result -> 次の action` を同一 session event 列で回す
- LLM が選べる最小 action:
  - `read_file`
  - `search_code`
  - `apply_patch`
  - `run_validation`
  - `open_child_frame`
  - `return_to_parent`
  - `finish`
- 各 action/result は `state/attempts/<candidate>.events.jsonl` に保存
- `show-attempt` と snapshot で session events を参照可能にした
- 各フレームに first-class な `frame context` を持たせた
  - `inherited_context`
  - `local_working_memory`
  - `local_tool_results`
  - `child_return_payloads`
  - `return_payload`
- 子フレーム生成時に、親の上位文脈と tool result を `inherited_context` として継承するようにした
- `return_to_parent` と `finish` で `return_payload` を明示できるようにした
- `return_payload` と `continue_or_return / transition_decision` を分離した
- 子フレームが返した `return_payload` を親へ merge し、親フレームがそのまま action loop を継続できるようにした
- dashboard で階層ごとの
  - 継承フレーム列
  - 継承知見
  - ローカル知見
  - 子フレーム返却要約
  - このフレームが返す要約
  を見られるようにした

## 重要な設計判断

- system は最小化し、phase ごとの要約 prompt を主役にしない
- summary より raw event を主材料にする
- `revised_file_content` の全文生成ではなく、`apply_patch` の小さな編集を主にする
- 再帰フレームは残し、`open_child_frame` / `return_to_parent` を action として使えるようにした
- 子フレームは「逃げ道」ではなく、親の判断材料を作る局所作業フレームとして扱う
- `return_to_parent` は失敗と同義ではない
  - 局所 goal を閉じて親へ材料を返す neutral な遷移として扱う
- 親へ返す内容と、戻るかどうかの判断は別に持つ

## 互換性

- 既存の default はまだ `legacy_phase_loop_v1`
- 既存 CLI / dashboard / tests は壊していない
- 新 kernel は `state/self_model.json` の `runtime_kernel` を `session_action_loop_v1` にすると使える

## 追加 state

- `state/attempts/<candidate>.events.jsonl`
- `state/attempts/<candidate>.prompts.jsonl`
- attempt report に `runtime_kernel`
- attempt report に `session_events_path`
- attempt report に `prompt_snapshots_path`
- snapshot に `latest_session_events`
- attempt report / task frame に `context.inherited_context`
- attempt report / task frame に `context.local_working_memory`
- attempt report / task frame に `context.local_tool_results`
- attempt report / task frame に `context.child_return_payloads`
- attempt report / task frame に `context.return_payload`

## 今回の確認結果

- `python3 -m unittest discover -s tests -v`
  - `36/36 OK`
- `python3 -m unittest tests/test_loop.py -v`
  - `25/25 OK`
- `python3 -m unittest tests/test_dashboard.py -v`
  - `4/4 OK`

今回、明示的に確認した点:

1. 子フレームは親の観測結果を継承できる
   - child prompt に親の `goal_logic.py を確認した` という知見が入るテストを追加
2. 子フレームは `return_payload` を選んで親へ返せる
   - child frame の `continue_or_return` と `context.return_payload` を別に検証
3. 親フレームは子の返却後に終了せず続行できる
   - child が `return_to_parent` で戻った後、親が `finish` して promotion するテストを追加
4. 各 LLM 呼び出しの再現材料が残る
   - `system_prompt / user_prompt / model / phase / step / prompt_context` を `prompts.jsonl` に保存し、`show-attempt` で取り出せる

## 既知の限界

- provider の first-class tool calling ではなく、JSON action protocol で実装している
- action はまだ少ない
- skill / memo を action loop に深く接続してはいない
- live workspace はまだ自動では新 kernel に切り替えていない
- 2026-04-11 の live 観測では、`hard rule` を外した後の `session_action_loop_v1` が `read_file` を連続選択し、観測から編集へ移る決断が弱いケースを確認した
- したがって次のボトルネックは「戻る/下るの強制」ではなく、「十分読んだ後に局所編集または分解へ移る work-unit commitment をどう自然に出すか」である

## 次の順番

1. live workspace を `session_action_loop_v1` に切り替える
2. dashboard に session events と current action を見やすく出す
3. `frame_state` を見たうえで、観測 loop から局所編集または分解へ移る判断を強める
4. skill / memo を `read_file` 相当の参照対象として action loop へ接続する
