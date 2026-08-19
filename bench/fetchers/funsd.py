"""FUNSD 表单数据集采集器（bench/PLAN.md §1.2）——**带 OCR 真值的真实扫描件**。

数据源：<https://guillaumejaume.github.io/FUNSD/dataset.zip>（约 16 MB）。
取 `testing_data/` 的 25 份表单扫描图（png）+ 对应人工标注 JSON。

为什么值钱：本基准里绝大多数 PDF 都自带文字层，`scan-pdf` 只能靠合成语料造。
FUNSD 是**真实扫描件 + 人工逐字标注**，是评 OCR 通道唯一的真值来源。

## 真值怎么来

这份数据集的标注**本身就是真值**，不需要走 `bench/make_truth.py`：
标注 JSON 的 `form[]` 每条带 `text` 与 `box=[x0,y0,x1,y1]`，按阅读顺序拼起来即可。
schema 照 `bench/truth/build_truth()`，两个刻意的取舍：

- **`headings` 整个省略该键**：表单没有标题层级。按 `bench/truth/__init__.py` 的约定，
  「有这个键」= 要算这项指标，写 `[]` 会把所有引擎的 heading_f1 判成 0，
  等于惩罚它们抓到了真实的表单字段——省略键才是「这份文档不评标题」。
- **`tables` 同样省略**：FUNSD 的 key-value 版面没有表格真值，而引擎把成对字段
  渲染成表格并不算错，同理不该评。

阅读顺序用 **行带 + 行内左起**（`(y0 // BAND, x0)`）而不是裸 `(y0, x0)`：
同一行的字段 y0 常差几个像素，裸排序会把左右两个字段颠倒。BAND=8px 是按
FUNSD 扫描件行高（约 15-20px）取的。
"""

from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path

if __package__ in (None, ""):  # 允许直接 python bench/fetchers/funsd.py
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bench.fetchers.common import (  # noqa: E402
    CORPUS_DIR,
    Collector,
    FetchError,
    RateLimiter,
    cache_get,
    log,
    manifest_row,
    sha256_bytes,
    write_atomic,
)
from bench.truth import build_truth  # noqa: E402

SOURCE = "funsd"
DATASET_URL = "https://guillaumejaume.github.io/FUNSD/dataset.zip"
LICENSE = "FUNSD 研究许可（不再分发，仅本机评测）"
WANT = 25
BAND = 8  # 行带高度（px）：同一行内的字段按 x 排序，不按 y 的像素抖动排序

IMG_PREFIX = "dataset/testing_data/images/"
ANN_PREFIX = "dataset/testing_data/annotations/"


def reading_order(entries: list[dict]) -> list[dict]:
    """按「行带 + 行内左起」排序。box = [x0, y0, x1, y1]。"""

    def key(e: dict) -> tuple[int, int]:
        box = e.get("box") or [0, 0, 0, 0]
        x0, y0 = int(box[0]), int(box[1])
        return (y0 // BAND, x0)

    return sorted(entries, key=key)


def make_truth(ann: dict) -> dict:
    """标注 JSON -> 真值 dict（headings / tables 均省略，见模块文档）。"""
    entries = [e for e in ann.get("form", []) if isinstance(e, dict)]
    blocks: list[tuple[str, object]] = []
    for e in reading_order(entries):
        text = (e.get("text") or "").strip()
        if not text:
            continue  # FUNSD 有一批空 box（纯版面框），不构成文本
        blocks.append(("para", text))
    return build_truth(
        blocks,
        with_headings=False,   # 表单无标题层级：省略键 = 不评这项
        with_tables=False,     # 无表格真值：引擎把 key-value 渲染成表不算错
        note={"reading_order": f"band={BAND}px then x0", "entries": len(entries)},
    )


def main() -> int:
    col = Collector(SOURCE, RateLimiter(2.0))
    log(f"[{SOURCE}] 目标 {WANT} 份真实扫描表单（png + 人工标注真值）")

    # 整包只有 16 MB，一次性取到内存（不落盘，避免留一份 zip 占地）。
    try:
        blob = cache_get(col.limiter, DATASET_URL, "funsd_dataset.zip")
    except FetchError as exc:
        col.skip(SOURCE, DATASET_URL, f"数据集下载失败：{exc}")
        log(col.summary())
        return 1

    col.note_download(len(blob))
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as exc:
        col.skip(SOURCE, DATASET_URL, f"zip 损坏：{exc}")
        log(col.summary())
        return 1

    # __MACOSX/._* 是 macOS 打包残留（AppleDouble），不是真数据，必须排除
    imgs = {
        Path(n).stem: n
        for n in zf.namelist()
        if n.startswith(IMG_PREFIX) and n.lower().endswith(".png")
    }
    anns = {
        Path(n).stem: n
        for n in zf.namelist()
        if n.startswith(ANN_PREFIX) and n.lower().endswith(".json")
    }
    stems = sorted(set(imgs) & set(anns))
    log(f"[{SOURCE}] testing_data 可用 {len(stems)} 份，取前 {WANT} 份")

    for stem in stems[:WANT]:
        doc_id = f"funsd_{stem}"
        rel_img = f"real/{SOURCE}/{stem}.png"
        rel_truth = f"real/{SOURCE}/{stem}.truth.json"

        hit = col.cached(doc_id, rel_img)
        if hit and col.cached_aux(rel_truth):
            col.reused += 1
            col.add(hit)
            continue

        try:
            img = zf.read(imgs[stem])
            ann = json.loads(zf.read(anns[stem]).decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            col.skip(doc_id, DATASET_URL, f"解包失败：{type(exc).__name__}: {exc}")
            continue

        truth = make_truth(ann)
        if not truth.get("text"):
            col.skip(doc_id, DATASET_URL, "标注里没有任何文本")
            continue

        write_atomic(CORPUS_DIR / rel_img, img)
        write_atomic(
            CORPUS_DIR / rel_truth,
            json.dumps(truth, ensure_ascii=False, indent=1).encode("utf-8"),
        )
        col.add(
            manifest_row(
                doc_id=doc_id,
                rel_path=rel_img,
                fmt="png",
                lang="en",
                domain="business",
                layout="form",
                source=SOURCE,
                license_=LICENSE,
                truth_type="A",
                truth=rel_truth,
                truth_src=f"{ANN_PREFIX}{stem}.json",
                url=DATASET_URL,
                sha256=sha256_bytes(img),
                size=len(img),
                pages=1,
                note=f"真实扫描表单；标注 {len(ann.get('form', []))} 字段；无标题/表格真值（省略键）",
            )
        )
        log(f"  ✓ {doc_id}（{len(img) / 1024:.0f} KB，{len(truth['paragraphs'])} 段）")

    log(col.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
