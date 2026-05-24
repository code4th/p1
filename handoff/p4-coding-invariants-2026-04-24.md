# P4 Coding Invariants 2026-04-24

## Purpose

この文書は、P4 を実装・修正する際の恒久的な一般原則だけを固定する。

案件固有の論点や一時的な設計判断はここに入れない。

## Invariants

1. 設計意図を最初に確認する
   曖昧な箇所は勝手に補完せず、既存コード、状態、イベント契約、設計文書から意図を読む。

2. 付け焼き刃の局所対応で塞がない
   問題が起きたら、その状態がなぜ発生できたのかを上位レイヤーで確認する。局所 if や表示補正だけで終わらせない。

3. 局所整合で閉じる設計を優先する
   他のコードの暗黙前提を読まないと成立しない実装を避ける。必要な責務と不変条件は、その場で読める形にする。

4. variation と asymmetry を区別する
   理由や値だけが違うものは variation として同じ構造の中に閉じ込める。
   特定ケースだけ別の状態、別の表示、別の終了条件を持つ asymmetry は、まず対称化できないかを検討する。
   非対称が避けられない場合は、実装前に設計判断として扱う。

5. 正本を増やさない
   同じ事実を複数の場所で別形式で持たせない。状態、イベント、表示の責務を分離し、表示側に推測を押しつけない。

6. 到達しないはずの経路はアサート対象
   想定外の分岐は握り潰さず、設計意図との衝突として扱う。アサートが出たら、その場しのぎで潰さず意図を読み直す。

7. 証拠に基づいて進める
   tool result、構造化イベント、状態ファイルなどの観測にない事実を finish や summary に混ぜない。

8. 分解は実行責務に着地させる
   first action と success evidence を具体化できない分解は抽象の言い換えと見なす。親が直接できる一手なら、まず親が実行する。

9. judge / 評価器の正本は decision フィールド単独
   judge スキーマで完了制御の決定権を持つのは verdict / status のみ。reason_code, rationale 等の annotation を required + enum 拘束して decision の正本性に逆流させない。annotation の表現揺れで decision を棄却する設計は Invariant 5 違反。
   詳細: `p4-judge-verdict-first-2026-05-03.md`

10. fallback ラベルは trigger reason を保持する
    リトライ上限や連続失敗で fallback 経路に入るとき、最終ラベルは trigger となった reason_code を継承する (例: `judge_invalid_output_observation_rejected`)。複数の異なる原因を一つの label に丸めない。Invariant 4 (variation / asymmetry) の具体適用。

11. prompt と schema は同じ source of truth で整合させる
    LLM 向け prompt の例示文字列と JSON schema の enum 値が別ファイルで二重管理されると、片方のドリフトが LLM 応答の構造的棄却を引き起こす。enum 拘束は decision フィールドのみに限定し、annotation は自由記述で受ける。

12. ユーザ request の「やり切る」を最上位に置く
    LLM 出力の完璧性を理由に turn 全体を放棄しない。valid な envelope が抽出できる場合は graceful degradation で前進し、警告は `system_note` (例: `code: llm_output_recovered`) として可視化する。「LLM 出力が完璧 / 全部失敗」の二値出口を「perfect / warning-accepted / total failure」の三値に拡張する。
    詳細: `p4-followthrough-recovery-2026-05-03.md`

## Usage

この文書は恒久原則だけを保持する。

- 今回固有の設計判断
- 一時的な移行方針
- 個別の issue / branch / event contract の論点

は別文書に分離する。
