#!/usr/bin/env python3
"""把各采集脚本的 manifest 分片合并成 `corpus/manifest.jsonl`（跑批的唯一输入）。

    python -m bench.merge_manifest [--dry-run] [--docling-n 300]

做四件事：
1. **校验**：枚举字段取值合法、`path` 真的存在、单份 ≤ 8 MB 且 ≤ 30 页（PDF 页数为空时用
   pymupdf 补数）。不合格的不进 manifest，原因计数打在结尾。
2. **去重**：id 冲突后来者改名加后缀；sha256 相同的只留第一份（同一文件换个 URL 下两遍很常见）。
3. **分层抽样标 `docling`**：docling ~9 s/份是唯一瓶颈，全量跑不完，因此只跑
   「全部合成 + 真实里按 (format, lang) 轮转抽样」共 `--docling-n` 份，其余引擎跑全量。
   抽样用固定种子，可复现。跑批时 docling 用 `--filter docling=yes` 选出这个子集。
4. **原子写**：先写临时文件再 replace，跑批正在读 manifest 时也不会读到半截。

合成语料（source=synthetic）由 `make_synthetic.py` 直接写进 manifest.jsonl，本脚本会把它们
原样保留（它们不在 manifest.d/ 分片里）。
"""
from __future__ import annotations

import argparse
import collections
import json
import random
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parent
CORPUS = BENCH / "corpus"
SHARD_DIR = CORPUS / "manifest.d"
MANIFEST = CORPUS / "manifest.jsonl"

MAX_BYTES = 8 * 1024 * 1024
MAX_PAGES = 30
FORMATS = {"pdf", "scan-pdf", "docx", "xlsx", "pptx", "html", "png", "jpg", "jpeg", "tiff", "webp", "gif"}
LANGS = {"zh", "en", "es", "de", "fr", "ar", "ja", "ru", "it", "pt", "mixed"}
DOMAINS = {"it", "math", "law", "business", "edu", "gov", "med", "news"}
TRUTH_TYPES = {"A", "B", "C", "D"}


def page_count(p: Path) -> int | None:
    if p.suffix.lower() not in (".pdf",):
        return None
    try:
        import pymupdf  # noqa: PLC0415
        with pymupdf.open(p) as doc:
            return doc.page_count
    except Exception:  # noqa: BLE001
        return None


def sha256_of(p: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def validate(row: dict, reasons: collections.Counter, verify_sha: bool = False) -> dict | None:
    for field, allowed in (("format", FORMATS), ("lang", LANGS), ("domain", DOMAINS),
                           ("truth_type", TRUTH_TYPES)):
        if row.get(field) not in allowed:
            reasons[f"{field} 取值非法: {row.get(field)!r}"] += 1
            return None
    src = CORPUS / str(row.get("path", ""))
    if not src.exists():
        reasons["源文件不存在"] += 1
        return None
    size = src.stat().st_size
    if size > MAX_BYTES:
        reasons[f"> {MAX_BYTES // 1024 // 1024} MB"] += 1
        return None
    # 采集脚本还在下载时合并会把半截文件收进来（实测撞到过：截断的 arXiv PDF 一跑就 fail）。
    # sha256 是采集脚本在下载成功后写的，对不上就是没下完。
    if verify_sha and row.get("sha256") and sha256_of(src) != row["sha256"]:
        reasons["sha256 不匹配（文件没下完或被改动）"] += 1
        return None
    row["size"] = size
    if row.get("pages") is None:
        row["pages"] = page_count(src)
    if isinstance(row.get("pages"), int) and row["pages"] > MAX_PAGES:
        reasons[f"> {MAX_PAGES} 页"] += 1
        return None
    # truth 文件没生成也不算错（run.py 会跳过对应指标），但要提示
    if row.get("truth") and not (CORPUS / row["truth"]).exists():
        reasons["真值 JSON 还没生成（跑 make_truth 后重新合并）"] += 1
        row["truth"] = ""
    return row


def mark_docling(rows: list[dict], n: int, seed: int = 20260817) -> None:
    """全部合成 + 真实里按 (format, lang) 轮转抽样，共 n 份标 docling=yes。"""
    for r in rows:
        r["docling"] = "no"
    # 已经跑过 docling 的先标上：语料是分批到位的，每次重新抽样都会换一批「yes」，
    # 那样旧结果白跑、docling 总量还会无上限膨胀（实测语料从 639 涨到 729 时就会发生）。
    ran = set()
    rf = BENCH / "results" / "docling.jsonl"
    if rf.exists():
        ran = {json.loads(l)["id"] for l in rf.read_text(encoding="utf-8").splitlines() if l.strip()}
    syn = [r for r in rows if r.get("source") == "synthetic" or r["id"] in ran]
    for r in syn:
        r["docling"] = "yes"
    budget = max(0, n - len(syn))
    buckets: dict[tuple, list[dict]] = collections.defaultdict(list)
    for r in rows:
        if r["docling"] != "yes":
            buckets[(r["format"], r["lang"])].append(r)
    rng = random.Random(seed)
    for b in buckets.values():
        rng.shuffle(b)
    keys = sorted(buckets)
    picked = 0
    while picked < budget and any(buckets[k] for k in keys):
        for k in keys:
            if picked >= budget:
                break
            if buckets[k]:
                buckets[k].pop()["docling"] = "yes"
                picked += 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--docling-n", type=int, default=300)
    ap.add_argument("--verify-sha", action="store_true", help="重算 sha256，剔掉没下完的文件（慢一点，收尾必做）")
    a = ap.parse_args()

    kept_syn = []
    if MANIFEST.exists():
        for line in MANIFEST.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                if r.get("source") == "synthetic":
                    kept_syn.append(r)
    print(f"保留合成语料 {len(kept_syn)} 份", flush=True)

    reasons: collections.Counter = collections.Counter()
    rows = list(kept_syn)
    seen_ids = {r["id"] for r in rows}
    seen_sha: dict[str, str] = {}
    shards = sorted(p for p in SHARD_DIR.glob("*.jsonl") if not p.name.endswith(".skipped.jsonl")) \
        if SHARD_DIR.exists() else []
    for shard in shards:
        n_in = n_ok = 0
        for line in shard.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            n_in += 1
            try:  # 采集脚本可能正在往分片追加，容忍读到半截行
                parsed = json.loads(line)
            except json.JSONDecodeError:
                reasons["行不是完整 JSON（采集可能正在写，重跑合并即可）"] += 1
                continue
            row = validate(parsed, reasons, a.verify_sha)
            if row is None:
                continue
            sha = row.get("sha256") or ""
            if sha and sha in seen_sha:
                reasons[f"内容重复（同 sha256，已有 {seen_sha[sha]}）"] += 1
                continue
            if row["id"] in seen_ids:
                base, i = row["id"], 2
                while f"{base}_{i}" in seen_ids:
                    i += 1
                row["id"] = f"{base}_{i}"
                reasons["id 冲突已改名"] += 1
            seen_ids.add(row["id"])
            if sha:
                seen_sha[sha] = row["id"]
            rows.append(row)
            n_ok += 1
        print(f"  {shard.name:26s} {n_ok}/{n_in} 通过", flush=True)

    mark_docling(rows, a.docling_n)

    def dist(field: str) -> str:
        c = collections.Counter(r.get(field) for r in rows)
        return " ".join(f"{k}:{v}" for k, v in sorted(c.items(), key=lambda kv: -kv[1]))

    print(f"\n合计 {len(rows)} 份（真实 {len(rows) - len(kept_syn)}）")
    for f in ("format", "lang", "domain", "truth_type", "source", "docling"):
        print(f"  {f:11s} {dist(f)}")
    if reasons:
        print("\n剔除/修正原因：")
        for k, v in reasons.most_common():
            print(f"  {v:4d}  {k}")
    if a.dry_run:
        print("\n(dry-run，未写入)")
        return 0
    tmp = MANIFEST.with_suffix(".jsonl.tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    tmp.replace(MANIFEST)
    print(f"\n已写入 {MANIFEST}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
