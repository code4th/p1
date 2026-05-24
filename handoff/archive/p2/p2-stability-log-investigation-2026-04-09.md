# 自己改善AIを長時間動かして分かったこと

P2 は、自分のコードを読み、直す案を 1 つ作り、テストし、通ったものだけを新しい自分として採用する AI です。今回見たかったのは、この仕組みが長時間でも前に進めるかどうかでした。

結論は単純です。前半はかなり前に進みましたが、後半は止まりました。しかも止まった原因は AI 本体だけではなく、周りの監視の仕組みにもありました。

## まず何が起きたか

P2 は合計 220 回動き、42 回採用され、115 回失敗し、63 回は途中で終わったままになっていました。見た目では `v0044` まで進んでいますが、最後に本当に採用されたのは 2026年4月9日 06:54 です。その後は長い停滞に入り、`c0156` を最後に `c0220` まで 64 回連続で新しい採用が起きていませんでした。

実際、最後の方の履歴はこうなっていました。

```text
{"generation": 44, "step": "attempt_started", "candidate_id": "c0218"}
{"generation": 44, "step": "attempt_started", "candidate_id": "c0219"}
{"generation": 44, "step": "attempt_started", "candidate_id": "c0220"}
{"generation": 44, "step": "candidate_generated", "candidate_id": "c0220"}
{"generation": 44, "step": "validation", "outcome": "failed", "candidate_id": "c0220"}
```

generation は 44 のままです。候補は作られていますが、新しい版としては採用されていません。つまり後半は「動いているが伸びていない」状態でした。

## 前半は何を良くできていたか

前半の改善は素直でした。最初の採用では、`self_check()` が成功か失敗かを返すだけの状態から、失敗理由を文字で返す方向に進んでいました。

```json
{
  "candidate_id": "c0001",
  "status": "promoted",
  "summary": "self_check() 関数を変更し、失敗理由を文字列で返すようにした。これにより、診断の可観測性が向上する。"
}
```

この時点では、P2 は「何を良くしたいのか」をかなりはっきり持てていました。自分の状態を説明しやすくし、あとから直しやすい形を少しずつ作れていました。

## どこで止まったのか

後半に増えた失敗は、ほとんど変わっていない、変えたけれど壊れた、変えたけれど価値が小さすぎた、の 3 つでした。中でも多かったのは「対象ファイルが実質変わっていない」という失敗で、47 回ありました。前に進める差分を作れなくなっていた可能性があります。

ここで効いていたのが監視です。朝 6:39 以降、monitor ログには次の記録が何度も出ていました。

```text
[monitor] 2026-04-09T06:39:17+0900 worker probe failed 3x; restarting loop worker
[monitor] 2026-04-09T06:41:28+0900 worker probe failed 3x; restarting loop worker
[monitor] 2026-04-09T06:45:55+0900 worker probe failed 3x; restarting loop worker
...
[monitor] 2026-04-09T09:15:08+0900 worker probe failed 3x; restarting loop worker
```

この再起動は 63 回ありました。途中で終わったままの試行も 63 回なので、長く考えている途中の処理を監視側が「止まった」と誤認して落としていた可能性が高いです。ここが今回いちばん大きな発見でした。

## 見た目は成長していても、中身はまだ浅い

`v0044` は最初の版よりかなり複雑です。ただし、中身を見ると危ういところもありました。たとえば、診断の履歴を持っているように見えて、実際には毎回リセットされています。

```python
def __init__(self, checks: list[callable]):
    self.checks = checks
    self.history = []  # 診断履歴を保持

def self_check() -> int | str:
    payload = describe_agent()
    runner = DiagnosisRunner(CHECKS)
    return runner.run(payload)
```

`self.history` はありますが、`self_check()` のたびに `DiagnosisRunner(CHECKS)` を作り直しています。実行結果も毎回こうでした。

```text
=== 診断結果: 正常 ===
=== 診断統計情報 ===
総診断回数: 1
成功回数: 1
失敗回数: 0
```

さらに、今のテストでは見つからないタイプミスも残っていました。

```python
if len(payload["operator_guidance"]) < 2:
    return False, f"... {len(payload["operator_guidANCE"])} 個 ..."
```

`operator_guidance` と `operator_guidANCE` が混ざっています。つまり、自己改善ループが回ることと、安全に良くなり続けることは別です。

## 今回の実験で言えること

P2 は自己改善を始められます。方向も悪くありません。ただし、長時間動かすと AI 本体より先に監視や記録の設計が成長を止めることがあります。だから次にやるべきことは、監視の誤停止を減らすこと、途中終了を明示すること、そして P2 自身に長期停滞を見せることです。

今回の run で分かったのは、自己改善 AI の難しさは「自分を書き換えられるかどうか」だけではない、ということでした。本当に大事なのは、自分が今どうなっているかを見られること、止まったときに止まったと分かること、そして長い試行を最後まで走らせられることです。
