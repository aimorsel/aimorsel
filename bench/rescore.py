#!/usr/bin/env python3
"""用已经落盘的产物重算指标，不重跑引擎。

    python -m bench.rescore [--dry-run] [--engines a,b]

什么时候用：**指标本身改了**（修 bug、加指标）。跑一次整批要几小时，但产物都在
`results/out/<engine>/<id>/`，重算只要几分钟。`run.py` 写结果时存了 `md_path`，
这里照着它重新读产物、重新 `score_document`，其余字段（status/wall_s/peak_rss_mb…）原样保留。

会打印每个引擎 char_sim 的平均变化量，用来确认这次改动的影响面。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parent
sys.path.insert(0, str(BENCH.parents[0]))
from bench import metrics  # noqa: E402

CORPUS = BENCH / "corpus"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--engines", default="")
    a = ap.parse_args()

    truths: dict[str, dict] = {}
    docs = {}
    for line in (CORPUS / "manifest.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            d = json.loads(line)
            docs[d["id"]] = d
    for i, d in docs.items():
        if d.get("truth") and (CORPUS / d["truth"]).exists():
            try:
                truths[i] = json.loads((CORPUS / d["truth"]).read_text(encoding="utf-8"))
            except Exception as e:  # noqa: BLE001
                print(f"  真值读不了 {i}: {type(e).__name__}")

    for f in sorted((BENCH / "results").glob("*.jsonl")):
        if a.engines and f.stem not in a.engines.split(","):
            continue
        rows = [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
        n_re = 0
        deltas = []
        for r in rows:
            mp = r.get("md_path")
            if not mp or not Path(mp).exists():
                continue
            md = Path(mp).read_text(encoding="utf-8", errors="replace")
            before = r.get("char_sim")
            r["out_chars"] = len(md)
            r["compat_residual"] = metrics.compat_residual(md)
            r["rtl_visual_ratio"] = metrics.rtl_visual_ratio(md)
            t = truths.get(r["id"])
            if t:
                # 旧字段先清掉：真值缺某项时 score_document 不会返回它，留着就是陈旧值
                for k in ("char_sim", "cer", "length_ratio", "heading_f1", "cell_f1",
                          "table_count_diff", "order_tau", "metrics_truncated",
                          "digit_f1", "digit_precision", "digit_recall", "digit_n_truth", "digit_n_pred"):
                    r.pop(k, None)
                r.update(metrics.score_document(md, t))
            n_re += 1
            if before is not None and r.get("char_sim") is not None:
                deltas.append(r["char_sim"] - before)
        avg = sum(deltas) / len(deltas) if deltas else 0.0
        worst = max(deltas, default=0.0)
        print(f"  {f.stem:15s} 重算 {n_re:4d}/{len(rows):4d} 行，char_sim 平均 {avg:+.4f}，单份最大 {worst:+.4f}")
        if not a.dry_run:
            tmp = f.with_suffix(".jsonl.tmp")
            tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
            tmp.replace(f)
    if a.dry_run:
        print("(dry-run，未写回)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
