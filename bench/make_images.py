#!/usr/bin/env python3
"""从已下载的真实 PDF 渲染出「图片集 + 扫描件集」，补齐 PLAN §1.1 的图片 / scan-pdf 配额。

    python -m bench.make_images [--dry-run] [--limit N] [--force] [--report]

**为什么要自造**：真实语料全是 html/pdf，图片与扫描件是 0，而 PLAN 要 80 份图片 +
60 份扫描/图片化 PDF。这里把已有真实 PDF 的**正文页**渲染成 png/jpg/tiff 与纯图片 PDF，
真值直接取源 PDF 的**文字层**（A 档），因此 OCR 有精确参照。

产出
----
* 文件：``bench/corpus/real/rendered/<id>.{png,jpg,tiff,pdf}``
* 真值：``bench/corpus/real/rendered/<id>.truth.json``（schema 同 ``bench.truth.build_truth``；
  只给 ``text`` + ``paragraphs`` + ``note``，**故意省略 headings / tables**——渲染页里
  标题层级不可靠，写空数组会让 ``metrics.score_document`` 惩罚抓对标题的引擎）
* manifest 分片：``bench/corpus/manifest.d/rendered.jsonl``

配额（``build_specs()``）
-----------------------
* 图片 60：png 20（150 dpi）/ jpg 20（300 dpi q85）/ tiff 20（200 dpi deflate，其中 5 份 3 帧）
  每种里 7/20 带轻微退化：4 份 ±3° 旋转（白底填充）、3 份轻高斯噪声（σ=5）
* scan-pdf 40：页图打进纯图片 PDF（JPEG 流，**无文字层**，生成后断言过），每份 2–5 页

约束与幂等
---------
* 单份 ≤ 8 MB（``merge_manifest`` 的硬门槛）：超了自动降 dpi 重渲染
* **计划持久化**在 ``real/rendered/.plan.json``：别的采集脚本继续往 manifest.d 里加行时，
  已有产物的 id / 页码 / 退化档不会漂移；重跑只补缺的、不重复生成（``--force`` 才重做）
* 语言摊开：按 lang 轮转挑源 PDF（每个源最多出 2 份产物，第二份取文档后半部分的页）

真值里的兼容码位
---------------
源 PDF 的文字层本身可能是康熙部首等兼容码位（macOS Quartz / 部分 zh PDF 常见），
不处理会污染真值。只对四段做 NFKC（康熙部首 / CJK 部首补充 / CJK 兼容表意 / ﬁﬂ 连字），
**不做全文 NFKC**（会误改全角标点与上标）——与 `morsel.normalize_compat_chars()` 同一套判据。
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import io
import json
import os
import random
import re
import sys
import unicodedata
import zlib
from pathlib import Path

BENCH = Path(__file__).resolve().parent
sys.path.insert(0, str(BENCH.parent))

from bench.truth import build_truth, norm_ws  # noqa: E402

CORPUS = BENCH / "corpus"
SHARD_DIR = CORPUS / "manifest.d"
OUT_DIR = CORPUS / "real" / "rendered"
PLAN_FILE = OUT_DIR / ".plan.json"
SHARD = SHARD_DIR / "rendered.jsonl"

MAX_BYTES = 8 * 1024 * 1024
MIN_PAGE_CHARS = 200          # 正文页判据：提取字符数下限（封面/目录/空白页因此落选）
NOISE_SIGMA = 5.0             # 轻高斯噪声
LICENSE = "派生自源语料，同源许可"

# ---------- 兼容码位归一（只这四段） ----------

_COMPAT_RANGES = ((0x2F00, 0x2FD5), (0x2E80, 0x2EF3), (0xF900, 0xFAFF), (0xFB00, 0xFB04))
_COMPAT_MAP: dict[str, str] = {}
for _lo, _hi in _COMPAT_RANGES:
    for _cp in range(_lo, _hi + 1):
        _ch = chr(_cp)
        _n = unicodedata.normalize("NFKC", _ch)
        if _n != _ch:
            _COMPAT_MAP[_ch] = _n


def normalize_compat(s: str) -> tuple[str, int]:
    """返回 (归一后文本, 被替换的字符数)。"""
    if not s:
        return s, 0
    hits = 0
    out = []
    for ch in s:
        rep = _COMPAT_MAP.get(ch)
        if rep is None:
            out.append(ch)
        else:
            out.append(rep)
            hits += 1
    return "".join(out), hits


# ---------- 配额 / 计划 ----------

def _deg_pattern() -> list[str]:
    """20 份里 4 rotated + 3 noisy（≈1/3 退化），位置固定以便幂等。"""
    pat = ["none"] * 20
    for i in (1, 6, 11, 16):
        pat[i] = "rotated"
    for i in (3, 9, 15):
        pat[i] = "noisy"
    return pat


def build_specs() -> list[dict]:
    specs: list[dict] = []
    deg = _deg_pattern()
    for kind, dpi in (("png", 150), ("jpg", 300), ("tiff", 200)):
        for i in range(20):
            frames = 3 if (kind == "tiff" and i % 4 == 0) else 1
            specs.append({"kind": kind, "dpi": dpi, "frames": frames, "deg": deg[i]})
    for i in range(40):
        specs.append({"kind": "scan", "dpi": 150, "frames": 2 + (i % 4), "deg": "none"})
    return specs


def load_pdf_rows() -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    for shard in sorted(SHARD_DIR.glob("*.jsonl")):
        if ".skipped." in shard.name or shard.name == SHARD.name:
            continue
        for line in shard.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("format") != "pdf" or r.get("source") == "rendered":
                continue
            rid = r.get("id")
            if not rid or rid in seen:
                continue
            if not (CORPUS / str(r.get("path", ""))).exists():
                continue
            seen.add(rid)
            rows.append(r)
    rows.sort(key=lambda r: (r.get("lang", ""), r.get("source", ""), r["id"]))
    return rows


def build_pool(rows: list[dict]) -> list[tuple[dict, int]]:
    """按 lang 轮转排出源 PDF 序列；每个源最多出现两次（occ=0 / occ=1，取不同页段）。"""
    lanes: dict[str, list[tuple[dict, int]]] = collections.defaultdict(list)
    by_lang: dict[str, list[dict]] = collections.defaultdict(list)
    for r in rows:
        by_lang[r.get("lang", "?")].append(r)
    for lang, items in by_lang.items():
        lanes[lang] = [(r, 0) for r in items] + [(r, 1) for r in items]
    order = sorted(lanes, key=lambda l: (-len(lanes[l]), l))
    pool: list[tuple[dict, int]] = []
    while any(lanes[l] for l in order):
        for lang in order:
            if lanes[lang]:
                pool.append(lanes[lang].pop(0))
    return pool


# ---------- 页面选择 ----------

_DOTS = re.compile(r"\.{5,}")


def eligible_pages(doc) -> list[int]:
    """正文页（0-based）：字符数够、非首页（多页文档的封面）、非目录页。"""
    out: list[int] = []
    for i in range(doc.page_count):
        try:
            t = doc[i].get_text().strip()
        except Exception:  # noqa: BLE001 - 坏页跳过
            continue
        if len(t) < MIN_PAGE_CHARS:
            continue
        if doc.page_count >= 3 and i == 0:
            continue
        if len(_DOTS.findall(t)) >= 3:      # 目录（点线）
            continue
        out.append(i)
    return out


def pick_window(elig: list[int], n: int, occ: int) -> list[int] | None:
    if len(elig) < n:
        return None
    start = 0 if occ == 0 else len(elig) // 2
    for s in list(range(start, len(elig) - n + 1)) + list(range(0, len(elig) - n + 1)):
        if elig[s + n - 1] - elig[s] == n - 1:   # 连续页优先
            return elig[s:s + n]
    idx = max(0, min(start, len(elig) - n))
    return elig[idx:idx + n]


def make_id(src_id: str, pages: list[int], kind: str) -> str:
    tag = f"p{pages[0] + 1}" if len(pages) == 1 else f"p{pages[0] + 1}-{pages[-1] + 1}"
    return f"rendered_{src_id}_{tag}_{'scan' if kind == 'scan' else kind}"


def plan_items(specs: list[dict], pool: list[tuple[dict, int]], echo) -> list[dict]:
    """给每条 spec 配一个源 PDF + 具体页码，返回计划条目（不渲染）。"""
    import pymupdf  # noqa: PLC0415

    items: list[dict] = []
    used_ids: set[str] = set()
    cache: dict[str, list[int]] = {}
    pi = 0
    for spec in specs:
        placed = False
        while pi < len(pool):
            row, occ = pool[pi]
            pi += 1
            src = CORPUS / row["path"]
            key = row["id"]
            if key not in cache:
                try:
                    with pymupdf.open(src) as doc:
                        cache[key] = eligible_pages(doc)
                except Exception as exc:  # noqa: BLE001
                    echo(f"  ! 打不开 {row['path']}：{exc}")
                    cache[key] = []
            pages = pick_window(cache[key], spec["frames"], occ)
            if not pages:
                continue
            doc_id = make_id(row["id"], pages, spec["kind"])
            if doc_id in used_ids:
                continue
            used_ids.add(doc_id)
            items.append({
                "id": doc_id,
                "src_id": row["id"],
                "src_path": row["path"],
                "lang": row.get("lang", "en"),
                "domain": row.get("domain", "gov"),
                "pages": pages,
                "kind": spec["kind"],
                "dpi": spec["dpi"],
                "deg": spec["deg"],
            })
            placed = True
            break
        if not placed:
            echo("  ! 源 PDF 用尽，配额未满")
            break
    return items


# ---------- 渲染 ----------

def render_pages(doc, pages: list[int], dpi: int):
    import pymupdf  # noqa: PLC0415
    from PIL import Image  # noqa: PLC0415

    imgs = []
    for p in pages:
        pix = doc[p].get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB)
        imgs.append(Image.frombytes("RGB", (pix.width, pix.height), pix.samples))
    return imgs


def degrade(imgs, deg: str, seed: int):
    from PIL import Image  # noqa: PLC0415

    if deg == "none":
        return imgs
    rng = random.Random(seed)
    if deg == "rotated":
        out = []
        for img in imgs:
            angle = round(rng.uniform(2.0, 3.0), 2) * rng.choice((1, -1))
            out.append(img.rotate(angle, resample=Image.BICUBIC, expand=True,
                                  fillcolor=(255, 255, 255)))
        return out
    if deg == "noisy":
        import numpy as np  # noqa: PLC0415

        gen = np.random.default_rng(seed)
        out = []
        for img in imgs:
            arr = np.asarray(img).astype(np.float32)
            arr += gen.normal(0.0, NOISE_SIGMA, arr.shape)
            out.append(Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB"))
        return out
    raise ValueError(f"未知退化档 {deg!r}")


def save_images(imgs, kind: str, dpi: int, path: Path) -> None:
    first, rest = imgs[0], imgs[1:]
    if kind == "png":
        first.save(path, "PNG", optimize=True, dpi=(dpi, dpi))
    elif kind == "jpg":
        first.save(path, "JPEG", quality=85, optimize=True, dpi=(dpi, dpi))
    elif kind == "tiff":
        first.save(path, "TIFF", compression="tiff_deflate", dpi=(dpi, dpi),
                   save_all=True, append_images=rest)
    else:
        raise ValueError(kind)


def save_scan_pdf(imgs, dpi: int, path: Path, quality: int = 80) -> None:
    """页图打成纯图片 PDF（JPEG 流，无文字层）。"""
    import pymupdf  # noqa: PLC0415

    out = pymupdf.open()
    try:
        for img in imgs:
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=quality, optimize=True)
            w = img.width * 72.0 / dpi
            h = img.height * 72.0 / dpi
            page = out.new_page(width=w, height=h)
            page.insert_image(pymupdf.Rect(0, 0, w, h), stream=buf.getvalue())
        out.save(path, deflate=True, garbage=3)
    finally:
        out.close()
    with pymupdf.open(path) as chk:                    # 断言无文字层
        leaked = [i for i in range(chk.page_count) if chk[i].get_text().strip()]
    if leaked:
        raise RuntimeError(f"{path.name} 竟然有文字层（页 {leaked}），不合格")


def produce_file(doc, item: dict, path: Path) -> tuple[int, int]:
    """渲染 + 退化 + 落盘，超 8 MB 自动降 dpi 重来。返回 (实际 dpi, 帧数)。"""
    seed = zlib.crc32(item["id"].encode("utf-8"))
    base = item["dpi"]
    for dpi in (base, int(base * 0.75), int(base * 0.6), int(base * 0.45)):
        imgs = degrade(render_pages(doc, item["pages"], dpi), item["deg"], seed)
        tmp = path.with_suffix(path.suffix + ".part")
        if item["kind"] == "scan":
            save_scan_pdf(imgs, dpi, tmp)
        else:
            save_images(imgs, item["kind"], dpi, tmp)
        if tmp.stat().st_size <= MAX_BYTES:
            os.replace(tmp, path)
            return dpi, len(imgs)
        tmp.unlink(missing_ok=True)
    raise RuntimeError(f"{path.name} 降到最低 dpi 仍 > 8 MB")


# ---------- 真值 ----------

def page_paragraphs(page) -> tuple[list[str], int]:
    """按阅读顺序取文本块作为段落；返回 (段落, 兼容码位替换数)。"""
    paras: list[str] = []
    hits = 0
    try:
        blocks = page.get_text("blocks", sort=True)
    except Exception:  # noqa: BLE001
        blocks = []
    if not blocks:
        raw = page.get_text()
        chunks = [c for c in re.split(r"\n\s*\n", raw) if c.strip()]
    else:
        chunks = [b[4] for b in blocks if len(b) > 6 and b[6] == 0 and str(b[4]).strip()]
    for c in chunks:
        fixed, n = normalize_compat(str(c))
        hits += n
        t = norm_ws(fixed)
        if t:
            paras.append(t)
    return paras, hits


def build_item_truth(doc, item: dict, dpi: int) -> dict:
    paras: list[str] = []
    hits = 0
    for p in item["pages"]:
        pp, h = page_paragraphs(doc[p])
        paras.extend(pp)
        hits += h
    note = {
        "rendered_from": item["src_id"],
        "pages": [p + 1 for p in item["pages"]],
        "dpi": dpi,
        "degradation": item["deg"],
        "compat_normalized": hits,
    }
    return build_truth([("para", t) for t in paras], note=note,
                       with_headings=False, with_tables=False)


# ---------- manifest 行 ----------

def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ext_of(kind: str) -> str:
    return {"png": ".png", "jpg": ".jpg", "tiff": ".tiff", "scan": ".pdf"}[kind]


def manifest_row(item: dict, path: Path, truth_rel: str, dpi: int, frames: int,
                 compat: int) -> dict:
    fmt = "scan-pdf" if item["kind"] == "scan" else item["kind"]
    return {
        "id": item["id"],
        "path": str(path.relative_to(CORPUS)),
        "format": fmt,
        "lang": item["lang"],
        "domain": item["domain"],
        "layout": "rendered-scan" if item["kind"] == "scan" else "rendered-image",
        "source": "rendered",
        "license": LICENSE,
        "truth_type": "A",
        "truth": truth_rel,
        "truth_src": "",
        "url": "",
        "sha256": sha256_of(path),
        "size": path.stat().st_size,
        "pages": frames,
        "note": (f"rendered_from={item['src_id']}; src_pages="
                 f"{','.join(str(p + 1) for p in item['pages'])}; dpi={dpi}; "
                 f"degradation={item['deg']}; compat_normalized={compat}"),
    }


def write_atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ---------- 主流程 ----------

def echo_factory():
    def echo(msg: str) -> None:
        print(msg, flush=True)
    return echo


def summarize(rows: list[dict], echo) -> None:
    fmt_c = collections.Counter(r["format"] for r in rows)
    lang_c = collections.Counter(r["lang"] for r in rows)
    deg_c = collections.Counter(re.search(r"degradation=(\w+)", r["note"]).group(1) for r in rows)
    frames_multi = sum(1 for r in rows if r["format"] == "tiff" and r["pages"] > 1)
    total = sum(r["size"] for r in rows)
    echo(f"\n共 {len(rows)} 份 / {total / 1024 / 1024:.1f} MB")
    echo(f"  format: {dict(sorted(fmt_c.items()))}")
    echo(f"  lang:   {dict(sorted(lang_c.items()))}")
    echo(f"  退化:   {dict(sorted(deg_c.items()))}；多帧 TIFF {frames_multi} 份")
    echo(f"  scan 页数合计: {sum(r['pages'] for r in rows if r['format'] == 'scan-pdf')}")


def main() -> int:
    ap = argparse.ArgumentParser(description="从真实 PDF 渲染图片集 / 扫描件集")
    ap.add_argument("--dry-run", action="store_true", help="只排计划，不渲染")
    ap.add_argument("--limit", type=int, default=0, help="只做前 N 份（调试）")
    ap.add_argument("--force", action="store_true", help="已存在的产物也重做")
    ap.add_argument("--replan", action="store_true", help="丢掉旧计划重排（会产生新 id）")
    ap.add_argument("--report", action="store_true", help="只统计现有产物")
    a = ap.parse_args()
    echo = echo_factory()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    if a.report:
        rows = [json.loads(l) for l in SHARD.read_text(encoding="utf-8").splitlines() if l.strip()] \
            if SHARD.exists() else []
        summarize(rows, echo)
        return 0

    if PLAN_FILE.exists() and not a.replan:
        items = json.loads(PLAN_FILE.read_text(encoding="utf-8"))
        echo(f"沿用已有计划 {len(items)} 条（--replan 可重排）")
    else:
        rows = load_pdf_rows()
        echo(f"源 PDF {len(rows)} 份："
             f"{dict(sorted(collections.Counter(r['lang'] for r in rows).items()))}")
        items = plan_items(build_specs(), build_pool(rows), echo)
        echo(f"排出计划 {len(items)} 条")
        if not a.dry_run:
            write_atomic(PLAN_FILE, json.dumps(items, ensure_ascii=False, indent=1))

    if a.limit:
        items = items[:a.limit]
    if a.dry_run:
        kinds = collections.Counter((i["kind"], i["deg"]) for i in items)
        echo(f"  {dict(sorted(kinds.items()))}")
        echo(f"  lang: {dict(sorted(collections.Counter(i['lang'] for i in items).items()))}")
        return 0

    import pymupdf  # noqa: PLC0415

    rows_out: list[dict] = []
    made = skipped = failed = 0
    for n, item in enumerate(items, 1):
        path = OUT_DIR / (item["id"] + ext_of(item["kind"]))
        truth_path = OUT_DIR / (item["id"] + ".truth.json")
        truth_rel = str(truth_path.relative_to(CORPUS))
        if path.exists() and truth_path.exists() and not a.force:
            try:
                note = json.loads(truth_path.read_text(encoding="utf-8")).get("note", {})
                rows_out.append(manifest_row(item, path, truth_rel, note.get("dpi", item["dpi"]),
                                             note.get("pages") and len(note["pages"]) or 1,
                                             note.get("compat_normalized", 0)))
                skipped += 1
                continue
            except Exception as exc:  # noqa: BLE001 - 真值坏了就重做
                echo(f"[{n}/{len(items)}] ! 旧真值不可读，重做：{exc}")
        try:
            with pymupdf.open(CORPUS / item["src_path"]) as doc:
                dpi, frames = produce_file(doc, item, path)
                truth = build_item_truth(doc, item, dpi)
            if not truth.get("text"):
                raise RuntimeError("真值文本为空")
            write_atomic(truth_path, json.dumps(truth, ensure_ascii=False, indent=1))
            compat = truth["note"]["compat_normalized"]
            rows_out.append(manifest_row(item, path, truth_rel, dpi, frames, compat))
            made += 1
            echo(f"[{n}/{len(items)}] ✓ {item['id']} "
                 f"{path.stat().st_size / 1024:.0f} KB dpi={dpi} {item['deg']}"
                 + (f" compat={compat}" if compat else ""))
        except Exception as exc:  # noqa: BLE001 - 单份失败不拖垮整批
            failed += 1
            path.unlink(missing_ok=True)
            echo(f"[{n}/{len(items)}] ✗ {item['id']}：{exc}")

    write_atomic(SHARD, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows_out))
    echo(f"\n新生成 {made} · 已存在跳过 {skipped} · 失败 {failed}")
    echo(f"manifest 分片：{SHARD}")
    summarize(rows_out, echo)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
