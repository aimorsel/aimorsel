#!/usr/bin/env python3
"""重跑结果里状态不是 ok/degraded/unsupported 的记录（fail / timeout / missing）。

    python -m bench.retry_failed [--engines a,b] [--dry-run] [--max 200]

跑批时的偶发失败（模型下载被网络挤断、临时内存不足）不重跑就会污染失败率这个指标，
所以收尾必须过一遍。`unsupported` 是引擎能力结论、`degraded` 是兜底成功，都不重跑。
"""
from __future__ import annotations

import argparse
import collections
import json
import subprocess
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parent
ROOT = BENCH.parents[0]
KEEP = {"ok", "degraded", "unsupported"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engines", default="")
    ap.add_argument("--tag", default="retry")
    ap.add_argument("--max", type=int, default=400)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    todo: dict[str, list[str]] = collections.defaultdict(list)
    why: collections.Counter = collections.Counter()
    for f in sorted((BENCH / "results").glob("*.jsonl")):
        engine = f.stem
        if a.engines and engine not in a.engines.split(","):
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            if r.get("status") not in KEEP:
                todo[engine].append(r["id"])
                why[f"{engine}/{r.get('status')}: {str(r.get('note') or '')[:60]}"] += 1
    if not todo:
        print("没有需要重跑的失败件")
        return 0
    for k, v in why.most_common(20):
        print(f"  {v:4d}  {k}")
    for engine, ids in todo.items():
        ids = ids[: a.max]
        print(f"\n== {engine}: 重跑 {len(ids)} 份", flush=True)
        if a.dry_run:
            continue
        subprocess.run([sys.executable, "-m", "bench.run", "--engines", engine, "--force",
                        "--tag", a.tag, "--ids", ",".join(ids)], cwd=ROOT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
