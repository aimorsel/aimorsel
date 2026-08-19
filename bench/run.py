#!/usr/bin/env python3
"""评测跑批（bench/PLAN.md §5）。

    python -m bench.run --manifest bench/corpus/manifest.jsonl [--engines a,b] [--sample 20] [--jobs 6]

- 每个 (engine, doc) 一个子进程 + 超时；崩溃/超时记 status，不中断整批
- 可断点续跑：results/<engine>.jsonl 里已有 id 跳过（--force 重跑）
- 大内存引擎（engines.toml [serial]）单独串行队列，其余按 --jobs 并发
- 日志逐行 flush 到 logs/run-<tag>.log
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BENCH = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from bench import metrics  # noqa: E402

ALL_ENGINES = ["aimorsel", "pdfplumber_txt", "markitdown", "pymupdf4llm", "docling"]


def load_cfg() -> dict:
    p = BENCH / "engines.toml"
    if not p.exists():
        p = BENCH / "engines.example.toml"
    cfg = tomllib.loads(p.read_text(encoding="utf-8"))
    cfg.setdefault("python", {})
    cfg.setdefault("timeout_s", {"default": 300})
    cfg.setdefault("serial", {"engines": []})
    return cfg


def read_manifest(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            rows.append(json.loads(line))
    return rows


def load_done(results_file: Path) -> set[str]:
    if not results_file.exists():
        return set()
    return {json.loads(l)["id"] for l in results_file.read_text(encoding="utf-8").splitlines() if l.strip()}


def load_truth(doc: dict, corpus_dir: Path) -> dict | None:
    t = doc.get("truth")
    if not t:
        return None
    p = corpus_dir / t
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def run_one(engine: str, doc: dict, cfg: dict, corpus_dir: Path, out_root: Path, log) -> dict:
    src = corpus_dir / doc["path"]
    out_dir = out_root / engine / doc["id"]
    py = os.path.expanduser(cfg["python"].get(engine, sys.executable))
    timeout = cfg["timeout_s"].get(engine, cfg["timeout_s"].get("default", 300))
    row = {"id": doc["id"], "engine": engine, "format": doc.get("format"), "lang": doc.get("lang"),
           "domain": doc.get("domain"), "truth_type": doc.get("truth_type")}
    if not src.exists():
        row.update(status="missing", note=f"源文件不存在：{src}")
        log(f"{engine:14s} {doc['id']:28s} missing     （{src}）")  # 早返回也要留痕，否则排查时看不见
        return row
    t0 = time.perf_counter()
    try:
        proc = subprocess.run([py, "-m", f"bench.engines.{engine}", str(src), str(out_dir)],
                              cwd=ROOT, capture_output=True, text=True, timeout=timeout,
                              env={**os.environ, "MORSEL_LANG": "zh", "PYTHONUTF8": "1", "BENCH_DOC_LANG": str(doc.get("lang", "en"))})
        last = next((l for l in reversed(proc.stdout.splitlines()) if l.strip().startswith("{")), None)
        info = json.loads(last) if last else {"status": "fail", "note": f"无 JSON 输出 rc={proc.returncode}: {proc.stderr[-300:]}"}
    except subprocess.TimeoutExpired:
        info = {"status": "timeout", "note": f">{timeout}s", "wall_s": timeout}
    except Exception as e:  # noqa: BLE001
        info = {"status": "fail", "note": f"{type(e).__name__}: {e}"}
    info.setdefault("wall_s", round(time.perf_counter() - t0, 3))
    row.update({k: info.get(k) for k in ("status", "wall_s", "peak_rss_mb", "pages", "used_ocr", "note", "md_path")})
    # 指标
    md_path = info.get("md_path")
    if md_path and Path(md_path).exists():
        md = Path(md_path).read_text(encoding="utf-8", errors="replace")
        row["out_chars"] = len(md)
        truth = load_truth(doc, corpus_dir)
        row["compat_residual"] = metrics.compat_residual(md)
        row["rtl_visual_ratio"] = metrics.rtl_visual_ratio(md)
        if truth:
            row.update(metrics.score_document(md, truth))
        if row.get("pages"):
            row["s_per_page"] = round(row["wall_s"] / row["pages"], 3)
    log(f"{engine:14s} {doc['id']:28s} {row.get('status'):11s} {row.get('wall_s', 0):7.1f}s "
        f"sim={row.get('char_sim', '-')} cer={row.get('cer', '-')} {str(row.get('note') or '')[:80]}")
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default=str(BENCH / "corpus" / "manifest.jsonl"))
    ap.add_argument("--engines", default=",".join(ALL_ENGINES))
    ap.add_argument("--sample", type=int, default=0, help="只跑前 N 份（估时用）")
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 4) - 2))
    ap.add_argument("--force", action="store_true", help="忽略已有结果全部重跑")
    ap.add_argument("--filter", default="", help="只跑 manifest 字段匹配的，如 format=pdf,lang=zh；"
                                                "多值用 | 取或：format=png|jpg|tiff")
    ap.add_argument("--ids", default="", help="只跑这些 id（逗号分隔），配 --force 用来重跑失败件")
    ap.add_argument("--tag", default="run")
    a = ap.parse_args()

    cfg = load_cfg()
    manifest = Path(a.manifest)
    corpus_dir = manifest.parent
    docs = read_manifest(manifest)
    if a.filter:
        conds = {k: set(v.split("|")) for k, v in (kv.split("=", 1) for kv in a.filter.split(","))}
        docs = [d for d in docs if all(str(d.get(k)) in v for k, v in conds.items())]
    if a.ids:
        want = set(a.ids.split(","))
        docs = [d for d in docs if d["id"] in want]
    if a.sample:
        docs = docs[: a.sample]
    engines = [e for e in a.engines.split(",") if e]
    out_root = BENCH / "results" / "out"
    (BENCH / "logs").mkdir(exist_ok=True)
    logf = open(BENCH / "logs" / f"{a.tag}.log", "a", encoding="utf-8")

    def log(msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        logf.write(line + "\n")
        logf.flush()

    log(f"文档 {len(docs)} 份 × 引擎 {engines}，jobs={a.jobs}")
    tasks = []
    ids = {d["id"] for d in docs}
    for e in engines:
        rf = BENCH / "results" / f"{e}.jsonl"
        if a.force and rf.exists():  # 重跑：先删掉这些 id 的旧记录，避免重复计数
            keep = [l for l in rf.read_text(encoding="utf-8").splitlines() if l.strip() and json.loads(l)["id"] not in ids]
            rf.write_text("".join(x + "\n" for x in keep), encoding="utf-8")
        done = set() if a.force else load_done(rf)
        todo = [d for d in docs if d["id"] not in done]
        log(f"  {e}: 待跑 {len(todo)}（已跳过 {len(docs) - len(todo)}）")
        tasks += [(e, d) for d in todo]
    if not tasks:
        log("无待跑任务")
        return 0

    files = {e: open(BENCH / "results" / f"{e}.jsonl", "a", encoding="utf-8") for e in engines}
    t_start = time.perf_counter()
    n_done = 0
    lock = threading.Lock()

    def commit(row: dict) -> None:
        nonlocal n_done
        with lock:
            _commit(row)

    def _commit(row: dict) -> None:
        nonlocal n_done
        files[row["engine"]].write(json.dumps(row, ensure_ascii=False) + "\n")
        files[row["engine"]].flush()
        n_done += 1
        if n_done % 20 == 0:
            el = time.perf_counter() - t_start
            log(f"进度 {n_done}/{len(tasks)}，已用 {el/60:.1f} min，预计剩余 {(el/n_done)*(len(tasks)-n_done)/60:.1f} min")

    serial_engines = set(cfg["serial"].get("engines", []))
    par = [t for t in tasks if t[0] not in serial_engines]
    ser = [t for t in tasks if t[0] in serial_engines]
    with ThreadPoolExecutor(max_workers=a.jobs) as ex:
        futs = [ex.submit(run_one, e, d, cfg, corpus_dir, out_root, log) for e, d in par]
        # 串行队列跑在一个独立线程里，与并发池同时进行
        def serial_worker():
            for e, d in ser:
                commit(run_one(e, d, cfg, corpus_dir, out_root, log))
        sfut = ex.submit(serial_worker) if ser else None
        for f in as_completed(futs):
            commit(f.result())
        if sfut:
            sfut.result()
    for f in files.values():
        f.close()
    log(f"完成 {n_done} 任务，总耗时 {(time.perf_counter()-t_start)/60:.1f} min")
    return 0


if __name__ == "__main__":
    sys.exit(main())
