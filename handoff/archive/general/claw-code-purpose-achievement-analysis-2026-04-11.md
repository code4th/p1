# claw-code 実コード調査メモ: ユーザー要求から目的達成までの経路

作成日: 2026-04-11
作業ディレクトリ: `/Users/satojunichi/Documents/openclaw`
参照 clone: `/tmp/claw-code-inspect`
参照元: `https://github.com/ultraworkers/claw-code`

## 1. このメモの目的

`claw-code` が、ユーザー要求を受けて実際にどうやって目的達成へ進むのかを、実コードベースで確認したメモである。

ここで見たいポイントは次の 3 つ。

- ユーザー要求が何と一緒にモデルへ渡るのか
- 目的達成がどのような loop で実現されているのか
- `goal manager` のような明示的な仕組みなのか、それとも会話反復なのか

## 2. 結論

`claw-code` の目的達成は、明示的な `goal` オブジェクトや専用 planner によるものではない。

実体は次の反復 loop である。

1. ユーザー要求を `Session.messages` に追加する
2. `system_prompt + session.messages` をモデルへ送る
3. モデルがテキストか tool call を返す
4. tool call があれば permission check の後に実行する
5. tool result を `Session.messages` に戻す
6. tool call がなくなるまで繰り返す
7. tool call がなくなったらその turn を終了する

つまり、`claw-code` は「目的を状態機械で管理する」のではなく、「会話履歴と tool result を次ターンへ戻し続ける」ことで目的達成を進めている。

## 3. system prompt は何を含むか

`load_system_prompt()` が prompt を事前に構築する。

主要構成要素は次の通り。

- 静的な行動指示
- environment context
- project context
- instruction files
- runtime config

### 3.1 environment context

最低でも次が入る。

- working directory
- date
- platform
- model family

参照:

- `/tmp/claw-code-inspect/rust/crates/runtime/src/prompt.rs`
- `SystemPromptBuilder::build()`
- `SystemPromptBuilder::environment_section()`

### 3.2 project context

`ProjectContext::discover_with_git()` で次を収集する。

- `cwd`
- `current_date`
- `git status`
- `git diff`
- `git context`
- instruction files

参照:

- `/tmp/claw-code-inspect/rust/crates/runtime/src/prompt.rs`
- `ProjectContext::discover_with_git()`
- `render_project_context()`

### 3.3 instruction files

祖先ディレクトリを辿って以下を読む。

- `CLAUDE.md`
- `CLAUDE.local.md`
- `.claw/CLAUDE.md`
- `.claw/instructions.md`

同一内容は dedupe され、総量にも上限がある。

参照:

- `/tmp/claw-code-inspect/rust/crates/runtime/src/prompt.rs`
- `discover_instruction_files()`
- `render_instruction_files()`

### 3.4 runtime config

`ConfigLoader` で読み込んだ config も prompt に入る。

参照:

- `/tmp/claw-code-inspect/rust/crates/runtime/src/prompt.rs`
- `render_config_section()`
- `load_system_prompt()`

## 4. ユーザー要求はどうモデルへ渡るか

実コード上の流れはかなり単純である。

### 4.1 runtime 構築

CLI 側で `system_prompt` を用意し、`ConversationRuntime` を作る。

参照:

- `/tmp/claw-code-inspect/rust/crates/rusty-claude-cli/src/main.rs`
- `build_runtime_with_plugin_state()`

### 4.2 user input の取り込み

`run_turn()` 冒頭で user input は `Session.messages` に `user text` として push される。

参照:

- `/tmp/claw-code-inspect/rust/crates/runtime/src/conversation.rs`
- `ConversationRuntime::run_turn()`
- `/tmp/claw-code-inspect/rust/crates/runtime/src/session.rs`
- `Session::push_user_text()`

### 4.3 モデルへ渡る payload

その直後に作られる request は次だけである。

```rust
ApiRequest {
    system_prompt: self.system_prompt.clone(),
    messages: self.session.messages.clone(),
}
```

つまり、モデルは毎回、

- 現在の system prompt 全体
- session に積まれた会話履歴全体

を受け取る。

参照:

- `/tmp/claw-code-inspect/rust/crates/runtime/src/conversation.rs`
- `ConversationRuntime::run_turn()`

## 5. session に何が入るか

`Session.messages` は構造化メッセージ列である。

- `System`
- `User`
- `Assistant`
- `Tool`

各メッセージには `ContentBlock` が入る。

- `Text`
- `ToolUse`
- `ToolResult`

重要なのは、tool 実行結果が単なるログではなく、次ターンでモデルに再送される会話履歴の一部であること。

参照:

- `/tmp/claw-code-inspect/rust/crates/runtime/src/session.rs`
- `MessageRole`
- `ContentBlock`
- `ConversationMessage`

### 5.1 session に入らないもの

以下は保存用であり、モデル request にそのままは入らない。

- `prompt_history`
- `workspace_root`
- `model`
- `fork`
- `session_meta`

この点は重要で、`claw-code` の実際の推論入力は「保存メタデータ全部」ではなく `system_prompt + messages` に絞られている。

## 6. provider への最終変換

runtime client は `ApiRequest` を provider 用 `MessageRequest` に変換する。

ここで重要なのは 2 点。

### 6.1 system prompt は joined text として入る

```rust
system: (!request.system_prompt.is_empty()).then(|| request.system_prompt.join("\n\n"))
```

### 6.2 tools は構造化で別フィールドに入る

```rust
tools: self.enable_tools.then(|| filter_tool_specs(...))
tool_choice: self.enable_tools.then_some(ToolChoice::Auto)
```

つまり、ツール一覧は prompt の文章だけに埋め込まれているのではなく、provider request の構造化フィールドとしても渡される。

この点は、元の最小レポートより実装の方が強い。

参照:

- `/tmp/claw-code-inspect/rust/crates/rusty-claude-cli/src/main.rs`
- `impl ApiClient for AnthropicRuntimeClient`

## 7. assistant 応答はどう解釈されるか

stream からは `AssistantEvent` が返る。

- `TextDelta`
- `ToolUse`
- `Usage`
- `PromptCache`
- `MessageStop`

それを `build_assistant_message()` で 1 つの `ConversationMessage` に畳み込む。

ここで、

- text は `ContentBlock::Text`
- tool call は `ContentBlock::ToolUse`

として分離される。

参照:

- `/tmp/claw-code-inspect/rust/crates/runtime/src/conversation.rs`
- `AssistantEvent`
- `build_assistant_message()`

## 8. tool 実行と目的達成の実体

`run_turn()` は assistant message から `pending_tool_uses` を抽出する。

### 8.1 tool call がある場合

- pre-hook
- permission check
- tool execute
- post-hook
- `ConversationMessage::tool_result(...)` を session に push

これが終わると loop 継続で、再度モデルに問い合わせる。

### 8.2 tool call がない場合

turn を終了する。

ここで重要なのは、runtime が「goal satisfied」を判定しているわけではないこと。

停止条件は、

- assistant message に tool call が残っていない

ただそれだけである。

参照:

- `/tmp/claw-code-inspect/rust/crates/runtime/src/conversation.rs`
- `ConversationRuntime::run_turn()`
- `/tmp/claw-code-inspect/rust/crates/runtime/src/permissions.rs`
- `PermissionPolicy`

## 9. compaction の意味

会話が長くなると、古い履歴は compaction で要約される。

このとき古いメッセージ群は、そのまま全部送られるのではなく、先頭付近の synthetic system message に要約される。

したがって `claw-code` の実際の文脈保持は、

- 全履歴そのまま
- もしくは compaction 済み要約 + recent messages

のどちらかである。

参照:

- `/tmp/claw-code-inspect/rust/crates/runtime/src/conversation.rs`
- `maybe_auto_compact()`
- `/tmp/claw-code-inspect/rust/crates/runtime/src/compact.rs`

## 10. 実コードから見た本質

`claw-code` の本質は次の 4 点に要約できる。

1. system prompt を環境依存で毎回しっかり組み立てる
2. user / assistant / tool result を構造化 session として持つ
3. tool result を必ず session に戻す
4. tool call がなくなるまで model loop を回す

要するに、「目的達成の仕組み」は planner ではなく session loop である。

## 11. P2 への示唆

P2 に引き写すときの重要点は次の通り。

### 11.1 強い goal state より強い session loop

`claw-code` は goal object を中心にしていない。
それでも動くのは、session と tool result loop が強いからである。

### 11.2 raw result を次ターンへ戻すことが本体

P2 で必要なのは、反省文を増やすことよりも、

- 何をしたか
- 実行して何が起きたか
- どの tool / change に対応するか

を raw に近い形で次ターンへ戻すことである。

### 11.3 編集出力と観測出力は分離すべき

`claw-code` は `ToolUse / ToolResult / Text` を構造的に分けている。
P2 の「コードに思考文を混ぜて壊す」問題は、この分離不足の問題として理解できる。

### 11.4 階層より先に turn loop を強くする必要がある

P2 が不安定なのは、まず「1 turn の観測と実行の接続」が弱いからである。
`claw-code` 的に言えば、先に強化すべきは hierarchy ではなく `Session + ToolResult + Runtime loop` である。

## 12. 短いまとめ

`claw-code` の目的達成は、

- `goal manager`
- 特別な planner
- 複雑な orchestration

ではなく、

- `system prompt`
- `session`
- `tool result`
- `conversation runtime`

の反復で成立している。

この観点から見ると、P2 の見直しポイントは「自己改善ループをどう賢くするか」より先に、「1 turn の構造をどう壊れにくくするか」である。
