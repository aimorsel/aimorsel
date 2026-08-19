"""主角：AImorsel 默认参数（ocr auto，服务在线时图片/扫描件走 OCR）。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from aimorsel import morsel  # noqa: E402
from bench.engines._base import main  # noqa: E402

_SERVER_OK: bool | None = None


def run(path: Path, out_dir: Path) -> dict:
    global _SERVER_OK
    if not morsel.is_supported_input(path):
        return {"md_path": None, "json_path": None, "pages": None, "status": "unsupported",
                "note": f"不支持 {path.suffix}"}
    opts = morsel.ConvertOptions()
    # 图片/扫描件按语言分批评测时，run_ocr_batches.py 会给每种语言起一个独立端口的 OCR 服务
    if os.environ.get("BENCH_OCR_URL"):
        opts.hybrid_url = os.environ["BENCH_OCR_URL"]
    if _SERVER_OK is None:
        _SERVER_OK = morsel.check_ocr_server(opts.hybrid_url)
    res = morsel._convert_task(path, out_dir, ["markdown", "json"], opts, _SERVER_OK)
    md = next((p for p in res.produced if p.suffix == ".md"), None)
    js = next((p for p in res.produced if p.suffix == ".json"), None)
    status = "ok" if res.ok and md else "fail"
    if res.degraded:
        status = "degraded"
    return {"md_path": str(md) if md else None, "json_path": str(js) if js else None,
            "pages": res.pages, "status": status, "used_ocr": res.used_ocr,
            "note": "；".join(x for x in (res.note, res.error) if x)}


if __name__ == "__main__":
    main(run)
