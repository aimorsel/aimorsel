"""引擎包装的公共协议。

每个 ``bench/engines/<name>.py`` 实现 ``run(path: Path, out_dir: Path) -> dict``，返回：
``{"md_path": str|None, "json_path": str|None, "pages": int|None, "note": str}``。
run.py 以子进程方式调用 ``python -m bench.engines.<name> <path> <out_dir>``，
本模块的 ``main()`` 负责计时、峰值内存、异常兜底，最后一行 stdout 打印 JSON。
引擎自己的日志请走 stderr。
"""
from __future__ import annotations

import json
import resource
import sys
import time
import traceback
from pathlib import Path


def peak_rss_mb() -> float:
    """自身 + 已结束子进程（Java/JVM）的峰值 RSS，MB。macOS 单位是字节，Linux 是 KB。"""
    own = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    kids = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    v = max(own, kids)
    return round(v / (1024 * 1024) if sys.platform == "darwin" else v / 1024, 1)


def unsupported(path: Path, exts: set[str]) -> dict | None:
    if path.suffix.lower() not in exts:
        return {"md_path": None, "json_path": None, "pages": None,
                "status": "unsupported", "note": f"不支持 {path.suffix}"}
    return None


def main(run) -> None:
    src, out_dir = Path(sys.argv[1]), Path(sys.argv[2])
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    try:
        info = run(src, out_dir) or {}
        info.setdefault("status", "ok" if info.get("md_path") else "fail")
    except Exception as e:  # noqa: BLE001
        info = {"md_path": None, "json_path": None, "pages": None, "status": "fail",
                "note": f"{type(e).__name__}: {str(e)[:300]}"}
        traceback.print_exc(file=sys.stderr)
    info["wall_s"] = round(time.perf_counter() - t0, 3)
    info["peak_rss_mb"] = peak_rss_mb()
    sys.stdout.write("\n" + json.dumps(info, ensure_ascii=False) + "\n")
    sys.stdout.flush()
