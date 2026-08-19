#!/usr/bin/env python3
"""按语言分批跑「需要 OCR 的输入」（图片 / 扫描件）。

OCR 服务（`opendataloader-pdf-hybrid`）的识别语言是**启动时固定**的，一批多语言语料
必须按语言分批：本脚本对每种语言起一个独立端口的服务 → 只跑该语言的图片/扫描件 →
关掉服务再换下一种语言。`BENCH_OCR_URL` 让 `bench/engines/aimorsel.py` 连到这个临时服务，
因此**不动用户常驻的 5002 服务**。

    python -m bench.run_ocr_batches --tag full [--langs zh,en] [--formats png,jpg,tiff]

服务解释器默认取 `--setup-ocr` 装出来的 `~/.aimorsel/ocr-env`，可用 BENCH_OCR_BIN 覆盖。
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BENCH = Path(__file__).resolve().parent
ROOT = BENCH.parents[0]
DEFAULT_BIN = os.path.expanduser(
    os.environ.get("BENCH_OCR_BIN", "~/.aimorsel/ocr-env/bin/opendataloader-pdf-hybrid"))
PORT = int(os.environ.get("BENCH_OCR_PORT", "5011"))

# manifest 的 lang → OCR 服务的 --ocr-lang。EasyOCR 要求同批语言的字符集兼容，
# 因此只做「目标语 + en」的组合，不把多种非拉丁文字混在一起。
OCR_LANG = {
    "zh": "ch_sim,en", "en": "en", "es": "es,en", "de": "de,en", "fr": "fr,en",
    "ja": "ja,en", "ru": "ru,en", "ar": "ar,en", "it": "it,en", "pt": "pt,en",
    "mixed": "ch_sim,en",
}


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] ocr-batch {msg}", flush=True)


def wait_health(url: str, timeout_s: int) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        try:
            with urllib.request.urlopen(f"{url}/health", timeout=3) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError, TimeoutError):
            time.sleep(3)
    return False


def start_server(ocr_lang: str, log_path: Path) -> subprocess.Popen | None:
    if not Path(DEFAULT_BIN).exists():
        log(f"找不到 OCR 服务可执行文件 {DEFAULT_BIN}")
        return None
    lf = open(log_path, "a", encoding="utf-8", buffering=1)
    p = subprocess.Popen([DEFAULT_BIN, "--port", str(PORT), "--ocr-lang", ocr_lang],
                         stdout=lf, stderr=subprocess.STDOUT, start_new_session=True)
    return p


def stop_server(p: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        p.wait(timeout=30)
    except Exception:  # noqa: BLE001
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default=str(BENCH / "corpus" / "manifest.jsonl"))
    ap.add_argument("--formats", default="png,jpg,jpeg,tiff,webp,gif,scan-pdf")
    ap.add_argument("--langs", default="")
    ap.add_argument("--tag", default="ocr")
    ap.add_argument("--engines", default="aimorsel")
    ap.add_argument("--force", action="store_true", help="忽略已有结果全部重跑")
    ap.add_argument("--boot-timeout", type=int, default=600, help="首次跑某语言要下载模型，给足时间")
    a = ap.parse_args()

    formats = set(a.formats.split(","))
    rows = [json.loads(l) for l in Path(a.manifest).read_text(encoding="utf-8").splitlines() if l.strip()]
    todo = [r for r in rows if r.get("format") in formats]
    langs = a.langs.split(",") if a.langs else sorted({r.get("lang", "en") for r in todo})
    log(f"图片/扫描件 {len(todo)} 份，语言批次 {langs}")

    (BENCH / "logs").mkdir(exist_ok=True)
    url = f"http://127.0.0.1:{PORT}"
    for lang in langs:
        n = sum(1 for r in todo if r.get("lang") == lang)
        if not n:
            continue
        ocr_lang = OCR_LANG.get(lang, f"{lang},en")
        log(f"=== {lang}（{n} 份）→ 起服务 --ocr-lang {ocr_lang}")
        proc = start_server(ocr_lang, BENCH / "logs" / f"ocr-server-{lang}.log")
        if proc is None:
            continue
        if not wait_health(url, a.boot_timeout):
            log(f"{lang}: 服务 {a.boot_timeout}s 内未就绪，跳过（见 logs/ocr-server-{lang}.log）")
            stop_server(proc)
            continue
        log(f"{lang}: 服务就绪，开跑")
        fmt_expr = "|".join(sorted(formats))
        rc = subprocess.run(
            [sys.executable, "-m", "bench.run", "--manifest", a.manifest,
             # jobs=1：OCR 服务是 CPU-bound 的单实例，并发只是让两份文档互相抢核——
             # 实测 4 份图片 jobs=1 与 jobs=2 墙钟时间相同（约 65 s），但单份耗时从 13 s 涨到 33 s，
             # 夜跑里那些 160 s+ 的离群值就是这么来的（bench issue #14）
             "--engines", a.engines, "--jobs", "1", "--tag", a.tag,
             "--filter", f"format={fmt_expr},lang={lang}"]
            + (["--force"] if a.force else []),
            cwd=ROOT, env={**os.environ, "BENCH_OCR_URL": url}).returncode
        log(f"{lang}: 跑完 rc={rc}，关服务")
        stop_server(proc)
        time.sleep(3)
    log("全部语言批次结束")
    return 0


if __name__ == "__main__":
    sys.exit(main())
