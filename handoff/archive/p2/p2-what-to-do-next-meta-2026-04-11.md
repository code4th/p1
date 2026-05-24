# P2 次に何をするべきか

日付: 2026-04-11

## 結論

次にやるべきことは、P2 を「もっと賢く反省させる」ことではない。

やるべきことは、P2 の内側 runtime を

- 小さな行為
- raw な結果
- 連続した session

で回る形に切り直すことである。

その上に、

- 自己改善
- 再帰階層コンテキスト
- skill
- memo

を載せる。

## メタ原則

### 1. 思考を教えるのではなく、気づける世界を与える

P2 に必要なのは「こう考えろ」という複雑な誘導ではない。

必要なのは、

- 何をしたか
- 何が起きたか
- それがどこに結びつくか

が自然に見える世界である。

## 2. summary より event

次の判断材料の主は summary ではなく event にする。

必要なのは、

- action
- observation
- validation result
- diff

である。

summary は補助でよい。

## 3. 再帰は prompt 規律ではなく実行プリミティブにする

再帰フレームは残す。
ただし「使うとよい」と説明するだけでは弱い。

再帰は、

- session を引き継いだ子 task を開く
- 局所問題だけに集中する
- 終わったら親へ戻る

という first-class な runtime 操作にする。

## P2 vNext の構造

### 内側 loop

P2 の内側には、次の session loop を置く。

1. goal を持つ
2. 現在 session の messages / events を持つ
3. LLM が次の action を選ぶ
4. action を tool として実行する
5. raw result を session に戻す
6. 続けるか、子フレームへ降りるか、終えるかを決める

### 外側 loop

その外側に自己改善 loop を置く。

1. candidate を分離
2. 内側 loop で自己改良を試す
3. validation
4. promote / reject
5. high-level goal に戻る

## 必須の実装変更

### A. 編集を全文生成から patch/action に変える

`revised_file_content` 全文置換を中核から外す。

最低限必要な action は次で十分である。

- `read_file`
- `search_code`
- `apply_patch`
- `run_validation`
- `open_child_frame`
- `return_to_parent`
- `finish`

### B. session を event-sourced にする

P2 が保持すべき主記録は summary JSON ではなく event log である。

各 event は最低限、

- `session_id`
- `frame_id`
- `candidate_id`
- `action`
- `action_input`
- `result`
- `exit_code`
- `changed_files`
- `timestamp`

を持つ。

### C. 因果を直接参照できるようにする

P2 が失敗から学ぶには、次の参照が必要である。

- 直前の edit event
- その直後の validation result
- 失敗したファイル断片
- 失敗種別

この 4 点を summary ではなく raw 参照として渡す。

### D. チャネルを分離する

少なくとも次の 4 チャネルを分ける。

- goal
- action
- observation
- narrative

コードを書き換えられるのは action だけにする。

### E. 再帰発火条件を observation から判断させる

再帰するかどうかは system が強制判定するのではなく、LLM が決めてよい。

ただし LLM が気づけるよう、最低限次の raw signal を渡す。

- 同一 failure の反復回数
- 同一 file の反復編集回数
- 変更なし回数
- validation failure 種別

## skill と memo の位置づけ

skill と memo は残してよい。
ただし主役にしない。

- skill
  - どういう手があるかを思い出すための参照
- memo
  - 将来また使う価値のある発見を短く残すための永続記憶

これらは session/action/result loop の上に載る補助機能である。

## 一番重要な設計変更

一言で言うと、

「P2 にどう考えさせるか」から、
「P2 が自分の行為と結果を見ながら考えられる runtime をどう与えるか」

へ軸を移すべきである。

## 実装順

### Phase 1

- action/tool schema を定義
- session event log を作る
- `read_file / apply_patch / run_validation / finish` だけで回す

### Phase 2

- `open_child_frame / return_to_parent` を session loop に統合
- frame ごとに event を継承

### Phase 3

- memo と skill の再接続
- stall 時のモデル切替や review action を追加

## 捨てるべき誤り

- prompt を厚くすれば解けるという発想
- summary を増やせば失敗を避けられるという発想
- 再帰を説明だけで使わせようとする発想

## 保持すべき本質

- P2 は自分を改良する AI エージェントである
- 問題は再帰階層コンテキストで分解する
- ただしその前提を支える内側 kernel は session/action/result loop でなければならない
