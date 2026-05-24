# P4 デザイン仕様書 (改訂版)

日付: 2026-04-21

## アーキテクチャ概要

P4 は P3 の session loop を 2 層に拡張する。大きな構成は変えず、安定して動作する P3 をそのまま土台とし、その上にフレーム階層を追加する。

```
┌─────────────────────────────────────────────┐
│  Frame Manager                              │
│  フレーム階層の管理、コンテキスト継承        │
│  open_child_frame / return_to_parent        │
├─────────────────────────────────────────────┤
│  Session Loop (P3 のもの)                    │
│  queue → model select → tool → result → ... │
├─────────────────────────────────────────────┤
│  Tool Executor / LLM Backend / Workspace    │
│  P3 のインフラ層（変更なし）                │
└─────────────────────────────────────────────┘
```

## フェーズ 0: P4 プロジェクト作成 ＋ リファクタリング

### 目的

P3 をそのままコピーして P4 を作成し、`runtime.py` の肥大化した責務をモジュールごとに分離することが目的である。重要なのは **責務の境界を明確にし、元の挙動を一切変えないこと** であり、ファイルの行数を目標にすることではない。リファクタリングはあくまで可読性と保守性の向上のためである。P3 は完成品として凍結し、変更しない。

### 分割計画

現在の `runtime.py` を以下のように分離する。

```
p4_core/
├── runtime.py         ← 核: session loop + queue処理
├── observer.py        ← passive commentator (7メソッド)
├── grounding.py       ← finish時の証拠判定 (4メソッド)
├── terminal.py        ← terminal agent固有ロジック (5メソッド)
├── prompts.py         ← prompt構築 (7メソッド)
├── llm_comm.py        ← LLM通信・JSON修復 (8メソッド)
├── guards.py          ← finish blocking・コマンド検証 (7メソッド)
├── tools.py           ← (既存) tool executor
├── models.py          ← (既存) model router
├── workspace.py       ← (既存) workspace管理
├── ollama_client.py   ← (既存) Ollama クライアント
├── cli.py             ← (既存) CLI
├── benchmark.py       ← (既存) ベンチマーク
└── dashboard/         ← (既存) ダッシュボード
```

### プロジェクト作成手順

1. P3 (`p3-core/`) をコピーして `p4-core/` を作成する（P3 は凍結、変更しない）
2. package 名を `p3_core` → `p4_core` に変更する
3. テスト全件がパスすることを確認する（機能差分が無いことを保証）
4. 上記分割計画に従いリファクタリングを実施する
5. リファクタリング後に再度テストを実行し、 **挙動に差分がないこと** を確認する
6. 行数は副産物として自然に減るはずだが、行数削減は成果指標に含めない

### 分割の原則

1. **runtime.py に残すもの**: session loop 本体のみ（`__init__`, `run_until_idle`, `_process_queue_item`, `worker_loop`, `send_message`, `status_snapshot` 等）。ここにビジネスロジックを持ち込まない。
2. **分離されたモジュールは runtime のインスタンスを受け取る関数群とする**（クラスにしない）。過度な抽象化は避ける。
3. **テストは全件パスを維持する**: リファクタリング前後で 35/35 のテストが同じ結果になることを保証する。

---

## フェーズ 1: フレーム基盤

フレームは問題を階層的に分割するためのコンテナであり、親子関係を持つ。P4 ではフレーム遷移を **LLM からは一般的なツールとして呼び出せるが、runtime 内部では特別な制御アクションとして扱う** ことで、session loop の安定性を維持する。

### Frame データ構造

```python
@dataclass
class Frame:
    frame_id: str               # UUID
    parent_frame_id: str | None
    depth: int                  # 0 = root
    goal: str                   # このフレームの局所目的
    inherited_context: dict     # 親から引き継いだ文脈
    working_memory: dict        # このフレームの理解状態
    session_events: list        # このフレームの行動履歴（LLMとの対話、tool_call等）
    return_payload: dict | None # 親に返す結果（完了時）
    status: str                 # active / returned / abandoned
    created_at: str
    returned_at: str | None
```

### Frame Manager

```python
class FrameManager:
    """フレーム階層の管理。runtime.py から呼ばれる。"""

    def __init__(self, root: Path):
        self.root = root
        self.frames: dict[str, Frame] = {}
        self.active_frame_id: str | None = None

    def create_root_frame(self, goal: str) -> Frame: ...
    def open_child_frame(self, goal: str, inherited_context: dict) -> Frame: ...
    def return_to_parent(self, return_payload: dict) -> Frame: ...
    def current_frame(self) -> Frame | None: ...
    def frame_stack(self) -> list[Frame]: ...  # root → 現在のフレームまでのパス
    def update_working_memory(self, updates: dict) -> None: ...
```

### フレーム遷移アクション

フレーム遷移は **kernel control actions** である。LLM からは `open_child_frame` や `return_to_parent` という tool のように呼び出すが、runtime 内部ではツール executor を通さず、特別なパスで処理する。このとき:

* ログの `type` を `frame_opened` / `frame_returned` とし、一般的な `tool_call` / `tool_result` では扱わない。
* `open_child_frame` の引数には子の `goal` と `context_summary`（親の文脈の要約）が含まれる。
* `return_to_parent` の引数には `summary` と `findings`（子フレームで得られた主要な発見）が含まれる。
* フレーム遷移により `active_frame_id` が切り替わり、`session_events` の追加対象も変わる。

### 新しいツール（LLM 側のアクション）

既存の tool (read_file, write_file, run_command, etc.) に加えて、以下のツールを **LLM から選択できるアクション** として定義する。ツールとして見せることで、LLM の action vocabulary は一貫する。しかし実装では kernel control action として処理する。

* `open_child_frame`: 子フレームを開く
  * 引数: `goal` (str), `context_summary` (str)
  * 効果: 新しいフレームを作成し、`active_frame_id` を子フレームに切り替える。親フレームの `working_memory` を保存し、子フレームの `session_events` に計画ノートを記録する。
* `return_to_parent`: 親フレームに戻る
  * 引数: `summary` (str), `findings` (list[str])
  * 効果: 子フレームの `return_payload` を記録し、`active_frame_id` を親に切り替える。親フレームの `session_events` に `child_return` イベントを追加する。

### session loop の変更

`_process_queue_item` の tool 実行部分を拡張し、フレーム遷移アクションを特別扱いする:

```python
if tool_name == "open_child_frame":
    # 1. 現在のフレームの working memory を保存
    # 2. 子フレームを作成（inherited_context に親の goal と直近の findings を含める）
    # 3. active_frame を子に切り替え
    # 4. 子フレームの session event として planning_note を追記
    # 5. ログとして frame_opened イベントを記録
    # 6. loop を継続（子フレーム内で次の step が始まる）

elif tool_name == "return_to_parent":
    # 1. return_payload を記録
    # 2. active_frame を親に切り替え
    # 3. 親の session event に child_return イベントを追記
    # 4. ログとして frame_returned イベントを記録
    # 5. loop を継続（親フレーム内で次の step が始まる）

else:
    # 既存の tool 実行
    ...
```

### フレーム間のコンテキスト分離

重要な設計判断として、**子フレームの session events は親に混入しない**。子フレームが開かれると、LLM に渡す `_conversation_messages` はその子フレームの events のみとなる。親フレームに戻る際には、子の `return_payload` だけが `child_return` イベントとして親の events に入る。これにより、子フレーム内の試行錯誤やエラーが親のコンテキストを汚染しない。

### 子フレームでの finish 処理

子フレームで LLM が `finish` を呼んだ場合、**ブロックして理由を返す**。「子フレームでは return_to_parent を使用してください」というメッセージを返す。リダイレクト（暗黙的に return_to_parent に変換）はしない。root フレームでのみ `finish` が許可される。

### フレームリセット（新規ユーザーメッセージ）

子フレームで作業中にユーザーから新しいメッセージが送信された場合、現在のフレーム階層をすべて abandon（status: abandoned）し、root フレームに戻って新しいメッセージを処理する。abandon されたフレームの session_events はログとして残るが、新しい turn のコンテキストには含めない。

これにより、1 queue item = 1 ユーザーメッセージ = 1 turn の関係が明確になる。フレーム遷移は turn 内のステップとして発生する。

### フレーム safety valve

1 フレームあたりの最大ステップ数を 15 に制限する。超過した場合、runtime が自動的に `return_to_parent` を発行し、「ステップ上限に達したため親フレームに戻ります」というメッセージとともに現時点の observations を return_payload として返す。root フレームでステップ上限に達した場合は finish を強制する。



---

## フェーズ 2: Working Memory

### 最小構成

Working Memory はフレームごとに保持される理解状態である。P4 初期では、P2 で採用していた複雑な分類を一気に導入せず、以下の 4 項目で始める。

* `observations` — LLM や tool の実行結果など、確認済みの事実を蓄積する。
* `current_focus` — 今このフレームで注目している対象。
* `unresolved_questions` — まだ答えが出ていない問い。
* `avoid_repeating` — 実行して失敗したコマンドや、繰り返しを避けるべき操作を記録する。

推論や仮説、意思決定は working memory には格納せず、session events に自然に残す。これは P2 で summary 汚染が発生したことへの教訓であり、フレーム化が安定した後で段階的に分類を拡張する。

### 自動更新ルール

tool 実行後に working memory を自動更新するロジックは以下のように定義する。

- `read_file` → `observations` にファイル名を追加
- `search_code` → `observations` に検索したシンボルを追加
- `run_command` (成功) → `observations` に結果の要約を追加
- `run_command` (失敗) → `observations` に失敗事実を追加し、`avoid_repeating` に同じコマンドを追加

### prompt への注入

working memory は LLM への prompt に以下のように注入する。ここでは分類を絞り、LLM がフレームの状態を把握しやすくする。

```
## 現在のフレーム状態
- 目的: {frame.goal}
- 深さ: {frame.depth} / 最大4
- 観測された事実: {working_memory.observations}
- 現在の焦点: {working_memory.current_focus}
- 未解決の問い: {working_memory.unresolved_questions}
- 繰り返すべきでない操作: {working_memory.avoid_repeating}

## 利用可能なフレーム操作
- open_child_frame: 問題が大きすぎる場合、局所目的に分解して子フレームを開く
- return_to_parent: 結果または判断材料が揃ったら親フレームに戻る
```

---

## フェーズ 3: Dashboard 拡張

### フレーム階層の表示

Dashboard snapshot にフレーム階層を表示し、現在フレームとその working memory を参照できるようにする。また `frame_opened` / `frame_returned` イベントに応じて履歴が追えるようにする。表示内容は運用者が「なぜ今この階層にいるのか」「戻る条件は満たされたか」「次にすべきことは何か」を判断する手助けとなるよう設計する。

### observer の拡張

observer はフレーム遷移時にその理由を解説し、child→parent の帰還では何を持って戻ったか、親が次に検討すべきことは何かを説明する。フィードバックの位置付けは、LLM の学習ではなく運用者の理解を助けることにある。

---

## フェーズ 4: 安定化・検証

### 検証シナリオ

P4 の実装を検証するため、以下のシナリオを用意する。

1. **単純タスク**: フレームを使わない `pwd → finish` が引き続き動く。
2. **2階層タスク**: 「README を読んで要約して」→ root が `read_file` → finish。
3. **分解が必要なタスク**: 「p1-core のバグを見つけて報告して」→ root が child(ファイル調査) → child(テスト実行) → return → finish。
4. **深い再帰**: 3–4階層の分解と帰還が成功する。

### 成功基準

再帰フレームが正常に動作することを以下の不変条件で評価する。

* 子フレームの session events が親フレームに混入しない。
* `return_to_parent` 後に親フレームが次の action を継続できる（スタックが正しく復元される）。
* 子フレームの `return_payload` が親フレームの events に `child_return` として記録される。
* 深さ制限（4階層）を超えるとエラーが返る。
* 再帰フレームを使わないタスクは P3 と同じ結果になる。

帰還率は運用指標として計測するが、受け入れ条件ではなく観察項目とする。

---

## P2 の教訓の反映

| P2 の失敗 | P4 での対策 |
|---|---|
| 再帰が prompt 規律だった | `open_child_frame` / `return_to_parent` を first-class のツール（LLM から見たアクション）として定義し、内部では kernel control action として処理する |
| 因果が summary で間接的だった | 子フレーム内は raw event のみを保持し、summary は return_payload のみとする |
| 全文書き換えで壊れた | P3 の `patch_file` / `replace_text` をそのまま使用し、変更部分のみを適用する |
| コードが 12,700 行に肥大化した | フェーズ0 で責務を分割し、 runtime.py の役割を核のみに絞る |
| session loop が不安定だった | P3 の安定した loop をそのまま使用し、遷移アクションは kernel control path で処理する |
| 新 kernel が切り替えられなかった | P3 を直接拡張し、別 kernel を作成せずにフレームを統合する |
