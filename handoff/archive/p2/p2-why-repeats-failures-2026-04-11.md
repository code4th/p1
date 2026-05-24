# P2 が失敗を繰り返す理由

日付: 2026-04-11

## 結論

P2 が失敗を繰り返す主因は、モデルの賢さ不足ではなく runtime の形が一般的な OSS agent と違うためである。

OSS の多くは、

- 要求
- 会話履歴
- tool 定義
- tool 実行結果

を同じ session に戻しながら、モデルが次の行動を決める。

一方 P2 は、

- kernel が phase を固定
- 各 phase ごとに大きな prompt を新規構築
- LLM には JSON を返させる
- validation は外部で固定実行

という形であり、同一 session 内での「行動 -> 結果 -> 次の修正」の連続性が弱い。

## 失敗を繰り返す直接原因

### 1. 失敗が session の中で処理されていない

OSS agent は tool 実行結果をそのまま履歴へ戻すため、

- 自分が何をしたか
- 直後に何が起きたか
- 次に何を直すか

が同じ流れの中に残る。

P2 は `recent_attempt_bundle` や `meta_diagnosis` のような再構成済みコンテキストを多用しており、raw な因果列が弱い。

### 2. full-file rewrite が壊れ方を大きくする

P2 v0 は `revised_file_content` に全文を書かせる。
この方式では、少し逸脱しただけで Python ファイル全体を JSON や説明文で上書きしてしまう。

実際のログでは、

- `reasoning_summary` 相当の内容がコードに混入
- JSON 風の文字列で Python を破壊
- `SyntaxError`

が繰り返し起きている。

### 3. 再帰フレームが行動原理ではなく説明に寄っている

再帰フレーム自体は実装されているが、P2 の支配的な loop は依然として phase-driven である。

そのため、問題が難しい時に自然に

- 子フレームへ降りる
- 局所問題を解く
- 親へ戻る

という動作が主経路になっていない。

ログ上でも `continue_here` と `flat_frame_streak` が長く続いている。

### 4. LLM が first-class tool を直接使っていない

OSS agent では LLM が

- 読む
- grep する
- 実行する
- 検証する

を tool call として選べる。

P2 では kernel が手順を決めており、LLM は各 phase で JSON を返すだけなので、「自分で確認して直す」主体になり切れていない。

### 5. 因果の粒度が粗い

P2 に必要だったのは「どう考えるか」の誘導より前に、

- 何を書き換えたか
- その直後にどの validation がどう失敗したか
- その failure がどの編集断片と結びつくか

という raw 事実列だった。

ここが粗いまま summary を厚くしても、同じ種類の失敗を再導入しやすい。

## OSS との比較

### claw-code / opencode / OpenHands 的な形

- session が主
- tool/result/history loop が主
- LLM はその場で次の action を選ぶ

### P2 の現状

- candidate / validation / promotion pipeline が主
- phase prompt が主
- 再帰フレームやスキルは上乗せ

この違いにより、P2 は「失敗を知る」ことはできても、「同じ session 内で失敗を処理して修復する」ことが弱い。

## 重要な解釈

再帰階層コンテキストの発想自体は間違っていない。
問題は、それを runtime kernel の中心ではなく prompt 上の規律として足したことにある。

つまり失敗の本質は、

- 再帰が不要だった

ではなく、

- 再帰を支える内側 kernel が session/tool-result 型になっていなかった

である。

## 一番大きい原因

一言で言うと、

P2 は「失敗を直す agent runtime」ではなく「失敗を説明する phase pipeline」に寄っていた。

このため、

- 同じ壊れ方を繰り返す
- 階層を知っていても使い切れない
- 自分で確認してその場で直す連続性が弱い

という現象が起きた。
