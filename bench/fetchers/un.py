"""联合国文件采集器（bench/PLAN.md §1.2）——**阿拉伯语与俄语的主力来源**。

同一份决议有六种官方语言（ar/zh/en/fr/ru/es）的官方译本，页面版式也基本平行，
所以一个符号（symbol）就能一次拿到六语平行样本。这是本基准里 **ar / ru 唯一的真实来源**
（合成语料造不出阿语的 RTL 版面与俄语西里尔字形）。

## 入口是怎么找到的（别再走弯路）

- ❌ `digitallibrary.un.org/search?...&of=xm`：**持续回 HTTP 202**（Invenio 前面挂了反爬挑战），
  换浏览器 UA、多次重试都一样，拿不到 MARC XML。
- ❌ `documents.un.org/`（ODS）：JS 单页应用，没有可解析的 HTML。
- ✅ **`undocs.org/<lang>/<symbol>`** 是官方短链：301 到 `docs.un.org`，返回一个 4 KB 的
  查看器页面，里面的 `<iframe src>` 直指真正的 PDF 接口——

      https://documents.un.org/api/symbol/access?s=<symbol>&l=<lang>&t=pdf

  这个接口直接回 `application/pdf`，无需 cookie / referer，六语言都通。**脚本直接打它**，
  不再解析中间页（少一跳，也少一个会改版的依赖）。

## 取舍

决议（`A/RES/...`）通常只有 1-12 页、200-300 KB，天然满足「≤ 30 页 ≤ 8 MB」。
候选池按顺序试，**只有六个语言全部拿到的符号才计入目标数**——半套的平行样本
对「同一内容跨语言对比」没用（但已下好的仍留在 manifest 里，不浪费）。

真值：`truth_type="C"`（无精确源，`truth` / `truth_src` 留空）。这些 PDF 自带文字层，
是给版面/顺序类指标用的，不是 OCR 真值。
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):  # 允许直接 python bench/fetchers/un.py
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

SOURCE = "un"
PDF_API = "https://documents.un.org/api/symbol/access?s={symbol}&l={lang}&t=pdf"
LICENSE = "联合国公开文件"
LANGS = ["ar", "zh", "en", "fr", "ru", "es"]  # 六种官方语言
TARGET_SYMBOLS = 5  # 5 符号 × 6 语言 = 30 份

# 候选池：大会决议，按预计篇幅从短到长。多备几个，个别符号缺某语言时能顺延。
CANDIDATES = [
    "A/RES/77/1",
    "A/RES/77/2",
    "A/RES/77/3",
    "A/RES/78/2",
    "A/RES/76/1",
    "A/RES/75/1",
    "A/RES/78/1",
    "A/RES/77/300",
    "A/RES/76/2",
    "A/RES/75/2",
    "A/RES/74/1",
    "A/RES/73/1",
]


def symbol_slug(symbol: str) -> str:
    """A/RES/77/1 -> a_res_77_1（能当文件名，且可读）。"""
    return symbol.lower().replace("/", "_").replace(".", "_")


def fetch_one(col: Collector, symbol: str, lang: str) -> bool:
    slug = symbol_slug(symbol)
    doc_id = f"un_{slug}_{lang}"
    rel = f"real/{SOURCE}/{slug}_{lang}.pdf"
    url = PDF_API.format(symbol=symbol, lang=lang)

    hit = col.cached(doc_id, rel)
    if hit:
        col.reused += 1
        col.add(hit)
        return True

    if col.budget_left() <= 0:
        col.skip(doc_id, url, "已达本轮下载预算上限")
        return False

    try:
        # 这个接口对 bot UA 也放行，但用浏览器 UA 更稳（同 eurlex 的处理）
        resp = http_get(url, col.limiter, headers={"User-Agent": BROWSER_UA})
    except FetchError as exc:
        col.skip(doc_id, url, f"下载失败：{exc}")
        return False

    body = resp.body
    if not body.startswith(b"%PDF"):
        col.skip(doc_id, url, f"返回的不是 PDF（Content-Type={resp.content_type!r}, {len(body)} B）")
        return False
    if len(body) > MAX_BYTES:
        col.skip(doc_id, url, f"{len(body) / 1e6:.1f} MB 超过 8 MB 上限")
        return False

    pages = pdf_page_count(body)
    if pages is None:
        col.skip(doc_id, url, "pymupdf 打不开，无法核对页数")
        return False
    if pages > MAX_PAGES:
        col.skip(doc_id, url, f"{pages} 页超过 {MAX_PAGES} 页上限（换更短的决议）")
        return False

    write_atomic(CORPUS_DIR / rel, body)
    col.note_download(len(body))
    col.add(
        manifest_row(
            doc_id=doc_id,
            rel_path=rel,
            fmt="pdf",
            lang=lang,
            domain="gov",
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
            note=f"联大决议 {symbol} 官方 {lang} 译本；六语平行样本",
        )
    )
    log(f"  ✓ {doc_id}（{pages} 页，{len(body) / 1024:.0f} KB）")
    return True


def main() -> int:
    col = Collector(SOURCE, RateLimiter(2.0))
    log(f"[{SOURCE}] 目标 {TARGET_SYMBOLS} 个符号 × {len(LANGS)} 语言 = {TARGET_SYMBOLS * len(LANGS)} 份")

    complete = 0
    for symbol in CANDIDATES:
        if complete >= TARGET_SYMBOLS:
            break
        log(f"[{SOURCE}] {symbol}")
        got = sum(1 for lang in LANGS if fetch_one(col, symbol, lang))
        if got == len(LANGS):
            complete += 1
            log(f"[{SOURCE}] {symbol}：六语齐全（{complete}/{TARGET_SYMBOLS}）")
        else:
            log(f"[{SOURCE}] {symbol}：只拿到 {got}/{len(LANGS)} 语，不计入目标（已下的仍保留）")

    if complete < TARGET_SYMBOLS:
        log(f"[{SOURCE}] ⚠️ 候选池用尽，只凑到 {complete} 个六语齐全的符号")
    log(col.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
