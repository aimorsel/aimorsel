"""World Bank 多语文档采集器（bench/PLAN.md §1.2）——CC BY，**可再分发**。

## 为什么用 WDS 而不是 OKR

- ❌ **OKR（openknowledge.worldbank.org，DSpace 7 REST）**：API 通得很顺
  （`discover/search/objects` + `f.supportedlanguage=<lang>,equals`，再走
  `items/<uuid>/bundles` → ORIGINAL → `bitstreams/<uuid>/content` 拿语言分册 PDF），
  但**多语池太浅**：`supportedlanguage` facet 全库只有 ar 18 / ru 12 / zh 4 条，
  而且基本都是整本书——实测抓到的 `Business_Ready_Arabic.pdf` 是 56 页，直接顶穿 30 页上限。
  阿语/俄语各要 4+ 份的话，这个池子凑不出来。
- ✅ **WDS（search.worldbank.org/api/v3/wds，Documents & Reports）**：同一家机构的另一个库，
  `lang_exact=<语言英文名>` 的量级完全不同——**ar 2963 / ru 2196 / zh 1946 / es 10245 / fr 16395**，
  且每条记录直接带 `pdfurl` 直链，无需二次跳转。短文档（Brief / 执行摘要 / 项目文件）很多，
  容易挑出 ≤30 页的。

## 两个实测坑

1. **`documents.worldbank.org` 的 PDF 路径不支持 HEAD**（回 404，GET 同一 URL 回 200），
   所以**没法先探 Content-Length 再决定下不下**。改成 ``fetch_capped()``：流式读，
   一超过 8 MB 立刻断开——单份最多浪费 8 MB，不会被某个 200 MB 的大报告拖垮预算。
2. **元数据的 `lang` 字段不等于 PDF 正文语言**：有记录标着 Arabic 但挂的是英文 PDF。
   所以对 ar / ru / zh 三种**字形上可判**的语言加了 ``script_ratio()`` 硬校验
   （正文里目标文字的占比要够），不达标就跳过。es / fr 是拉丁字母，跟英文无法用字形区分，
   只能信元数据——这个不对称是有意的，宁可放过几份也不要把英文文档冒充成阿语语料。

真值 `truth_type="C"`（无精确源，`truth` / `truth_src` 留空）：这些 PDF 自带文字层，
用于版面 / 阅读顺序类指标，不是 OCR 真值。**要求有文字层**，纯扫描件跳过
（没有真值的扫描件在本基准里评不出东西）。
"""

from __future__ import annotations

import json
import random
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

if __package__ in (None, ""):  # 允许直接 python bench/fetchers/worldbank.py
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bench.fetchers.common import (  # noqa: E402
    BROWSER_UA,
    CORPUS_DIR,
    MAX_BYTES,
    MAX_PAGES,
    Collector,
    FetchError,
    RateLimiter,
    http_get,
    log,
    manifest_row,
    pdf_page_count,
    sha256_bytes,
    write_atomic,
)

SOURCE = "worldbank"
WDS = "https://search.worldbank.org/api/v3/wds"
LICENSE = "CC BY 3.0 IGO"

# (manifest 的 lang, WDS 的 lang_exact 值, 要几份)
# ar / ru 给到 5 份：这两种是整个基准最缺的，其它来源基本补不上。
TARGETS = [
    ("ar", "Arabic", 5),
    ("ru", "Russian", 5),
    ("es", "Spanish", 4),
    ("fr", "French", 3),
    ("zh", "Chinese", 3),
]

FIELDS = "docna,pdfurl,lang,docty,majdocty,guid,repnb,owner"
PAGE_ROWS = 40          # 每页拉 40 条候选（大多会因页数/体积被筛掉）
MAX_CANDIDATES = 240    # 每语言最多看这么多条，避免无限翻页

# 字形占比校验：只对字形上可判的语言启用（见模块文档）
SCRIPT_RANGES: dict[str, tuple[tuple[int, int], ...]] = {
    "ar": ((0x0600, 0x06FF), (0x0750, 0x077F), (0xFB50, 0xFDFF), (0xFE70, 0xFEFF)),
    "ru": ((0x0400, 0x04FF),),
    "zh": ((0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF)),
}
MIN_SCRIPT_RATIO = 0.15  # 目标文字 / 全部字母数字字符，够低以容忍大量英文表头和数字


def script_ratio(text: str, lang: str) -> float:
    """目标文字字符占「有意义字符」的比例。无法判定的语言返回 1.0（视为通过）。"""
    ranges = SCRIPT_RANGES.get(lang)
    if not ranges:
        return 1.0
    hit = 0
    total = 0
    for ch in text:
        if not ch.isalnum():
            continue
        total += 1
        cp = ord(ch)
        if any(lo <= cp <= hi for lo, hi in ranges):
            hit += 1
    return (hit / total) if total else 0.0


class TooLarge(Exception):
    """流式下载中途发现超过体积上限——不是错误，不该重试。"""


def fetch_capped(url: str, limiter: RateLimiter, cap: int, retries: int = 3) -> bytes:
    """流式下载，超过 ``cap`` 字节立刻放弃（抛 ``TooLarge``）。

    `documents.worldbank.org` 不支持 HEAD，只能边下边判；这样单份最多浪费 cap 字节。
    这个站点相当容易在传输中途断流（实测大量 ``IncompleteRead``），所以**必须重试**——
    否则一批里能白丢七八份。``TooLarge`` 不参与重试（重试也还是那么大）。
    """
    last_err = ""
    for attempt in range(retries):
        limiter.wait(url)
        req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA, "Accept": "*/*"})
        buf = bytearray()
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                while True:
                    block = resp.read(1 << 18)
                    if not block:
                        break
                    buf += block
                    if len(buf) > cap:
                        raise TooLarge
            return bytes(buf)
        except TooLarge:
            raise
        except Exception as exc:  # noqa: BLE001 —— 网络层什么都可能抛
            last_err = f"{type(exc).__name__}: {exc}"
            if attempt + 1 < retries:
                time.sleep(min(2 ** (attempt + 1), 8) + random.random())
    raise FetchError(f"{url} -> {last_err}")


def search(limiter: RateLimiter, lang_exact: str, offset: int) -> list[dict]:
    """WDS 检索一页。返回记录 dict 列表（已摊平 documents 字典）。"""
    qs = urllib.parse.urlencode(
        {"format": "json", "lang_exact": lang_exact, "rows": PAGE_ROWS, "os": offset, "fl": FIELDS}
    )
    resp = http_get(f"{WDS}?{qs}", limiter, headers={"User-Agent": BROWSER_UA})
    data = json.loads(resp.body.decode("utf-8", "replace"))
    docs = data.get("documents") or {}
    out = []
    for key, rec in docs.items():
        if not isinstance(rec, dict) or not rec.get("pdfurl"):
            continue
        rec = dict(rec)
        rec["_key"] = key
        out.append(rec)
    return out


def doc_title(rec: dict) -> str:
    """docna 是 {"0": {"docna": "标题"}} 这种嵌套形状。"""
    docna = rec.get("docna")
    if isinstance(docna, dict):
        for v in docna.values():
            if isinstance(v, dict) and v.get("docna"):
                return str(v["docna"])
    if isinstance(docna, str):
        return docna
    return ""


def pdf_text_head(data: bytes, pages: int = 4) -> str:
    """取前几页文字用于字形校验；没装 pymupdf 或打不开返回空串。"""
    try:
        import pymupdf  # type: ignore
    except ImportError:  # pragma: no cover
        try:
            import fitz as pymupdf  # type: ignore
        except ImportError:
            return ""
    try:
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            return "".join(doc[i].get_text() for i in range(min(pages, doc.page_count)))
    except Exception:  # noqa: BLE001
        return ""


def readd_previous(col: Collector, lang: str, want: int) -> int:
    """先把上一轮已选中且文件还在的同语言文档重新占住配额。

    不做这一步的话续跑会「换人」：某个上轮因断流失败、但在候选列表里更靠前的文档
    这轮成功了，就会顶掉一个已经下好的靠后文档——manifest 里份数没变，
    磁盘上却多出一份不被引用的孤儿文件。先复用旧选择，配额分配才在多轮之间稳定。
    """
    got = 0
    for doc_id, prev in sorted(col._prev.items()):  # noqa: SLF001 —— 同仓库内部结构，故意复用
        if got >= want or prev.get("lang") != lang:
            continue
        hit = col.cached(doc_id, prev.get("path", ""))
        if hit:
            col.reused += 1
            col.add(hit)
            got += 1
    if got:
        log(f"[{SOURCE}] {lang}：复用上轮 {got} 份")
    return got


def fetch_lang(col: Collector, lang: str, lang_exact: str, want: int) -> int:
    log(f"[{SOURCE}] {lang}（{lang_exact}）目标 {want} 份")
    got = readd_previous(col, lang, want)
    seen = 0
    offset = 0

    while got < want and seen < MAX_CANDIDATES:
        try:
            recs = search(col.limiter, lang_exact, offset)
        except (FetchError, json.JSONDecodeError) as exc:
            col.skip(f"{SOURCE}_{lang}", f"{WDS}?lang_exact={lang_exact}&os={offset}",
                     f"检索失败：{exc}")
            break
        if not recs:
            log(f"[{SOURCE}] {lang}：offset={offset} 没有更多结果")
            break
        offset += PAGE_ROWS

        for rec in recs:
            if got >= want:
                break
            seen += 1
            guid = str(rec.get("guid") or rec.get("_key") or "").strip()
            if not guid:
                continue
            doc_id = f"wb_{lang}_{guid}"
            rel = f"real/{SOURCE}/{lang}_{guid}.pdf"
            url = str(rec["pdfurl"])

            if doc_id in col.rows:
                continue  # readd_previous 已经占住这个配额，别重复计数

            hit = col.cached(doc_id, rel)
            if hit:
                col.reused += 1
                col.add(hit)
                got += 1
                continue

            if col.budget_left() <= 0:
                col.skip(doc_id, url, "已达本轮下载预算上限")
                return got

            try:
                body = fetch_capped(url, col.limiter, MAX_BYTES)
            except TooLarge:
                col.skip(doc_id, url, "超过 8 MB 上限（下载中止）")
                continue
            except Exception as exc:  # noqa: BLE001 —— 网络层什么都可能抛
                col.skip(doc_id, url, f"下载失败（重试 3 次仍失败）：{type(exc).__name__}: {exc}")
                continue
            col.note_download(len(body))
            if not body.startswith(b"%PDF"):
                col.skip(doc_id, url, "返回的不是 PDF")
                continue

            pages = pdf_page_count(body)
            if pages is None:
                col.skip(doc_id, url, "pymupdf 打不开，无法核对页数")
                continue
            if pages > MAX_PAGES:
                col.skip(doc_id, url, f"{pages} 页超过 {MAX_PAGES} 页上限")
                continue

            head = pdf_text_head(body)
            if len(head.strip()) < 200:
                col.skip(doc_id, url, "几乎没有文字层（疑似纯扫描件，无真值不收）")
                continue
            ratio = script_ratio(head, lang)
            if ratio < MIN_SCRIPT_RATIO:
                col.skip(doc_id, url, f"正文 {lang} 文字占比仅 {ratio:.0%}，元数据语言与实际不符")
                continue

            majdocty = str(rec.get("majdocty") or "")
            domain = "gov" if "Project Documents" in majdocty else "business"
            write_atomic(CORPUS_DIR / rel, body)
            col.add(
                manifest_row(
                    doc_id=doc_id,
                    rel_path=rel,
                    fmt="pdf",
                    lang=lang,
                    domain=domain,
                    layout="single-column",
                    source=SOURCE,
                    license_=LICENSE,
                    truth_type="C",
                    truth="",
                    truth_src="",
                    url=url,
                    sha256=sha256_bytes(body),
                    size=len(body),
                    pages=pages,
                    note=(
                        f"World Bank D&R；{rec.get('docty') or majdocty}；"
                        f"{lang} 文字占比 {ratio:.0%}；{doc_title(rec)[:70]}"
                    ),
                )
            )
            got += 1
            log(f"  ✓ {doc_id}（{pages} 页，{len(body) / 1024:.0f} KB，{lang} 占比 {ratio:.0%}）")

    if got < want:
        log(f"[{SOURCE}] ⚠️ {lang} 只凑到 {got}/{want} 份（看过 {seen} 条候选）")
    return got


def main() -> int:
    col = Collector(SOURCE, RateLimiter(2.0))
    total = sum(w for _, _, w in TARGETS)
    log(f"[{SOURCE}] 目标 {total} 份（{', '.join(f'{l}×{w}' for l, _, w in TARGETS)}）")
    for lang, lang_exact, want in TARGETS:
        fetch_lang(col, lang, lang_exact, want)
    log(col.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
