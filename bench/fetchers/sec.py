"""采集 SEC EDGAR 10-K/10-Q 主文档 HTML（20 家不同公司，表格密集）。

用法：`python -m bench.fetchers.sec`

要点 / 踩过的坑：
- **SEC 要求 UA 带联系方式**，且格式挑食：`AImorselBench/0.1 (+https://...; mail)`
  这种带 URL 括号的形式**直接 403**，改成「名字 + 邮箱」的朴素形式才通（实测）。
  官方限速上限 10 req/s，这里按 2 req/s 走（`min_interval=0.5`）。
- 10-K 主文档常常十几 MB（内联 XBRL），先 HEAD 问体积再决定下不下；
  超 8 MB 就退到同公司较新的 10-Q，全不合格才换公司（实测 JPM/BAC/DE 四份候选全超）。
- HEAD 请求**不带 gzip**，否则 Content-Length 是压缩后的值，会放过超大文件。
- ⚠️ **SEC 的 Archives 对大文档根本不返回 Content-Length**（分块传输，HEAD 和
  `Range: bytes=0-0` 都问不出体积），所以 HEAD 只是能省则省；真正的兜底是
  `http_get` 读到上限 +1 字节就放弃，并把超限 URL 记进 `_toobig.json`，重跑不再白下。
- HTML 自身即真值（truth_type=A，truth_src 指向自己）。
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from bench.fetchers import _common as C  # type: ignore
else:
    from . import _common as C

SOURCE = "sec"
TARGET = 20
LICENSE = "美国政府公开信息/公有领域"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

# 候选公司（按顺序尝试，收满 TARGET 即停）；覆盖多行业，便于表格样式多样化。
CANDIDATES = [
    "AAPL", "MSFT", "KO", "PEP", "JNJ", "PG", "WMT", "XOM", "CVX", "JPM",
    "BAC", "VZ", "NKE", "MCD", "DIS", "CAT", "DE", "UPS", "HD", "IBM",
    "ORCL", "CSCO", "INTC", "PFE", "MRK", "TGT", "COST", "SBUX", "GM", "F",
    "BA", "MMM", "HON", "LMT", "UNP", "T", "ABT", "LOW", "FDX", "GE",
]
# 每家公司最多试几份申报（10-K 优先，其次最近的 10-Q）
MAX_FILINGS_PER_COMPANY = 4


def load_cik_map(col: C.Collector) -> dict[str, tuple[str, str]]:
    """ticker -> (10 位 CIK, 公司名)。"""
    data = col.get_json(TICKERS_URL)
    out: dict[str, tuple[str, str]] = {}
    for row in data.values():
        out[str(row["ticker"]).upper()] = (f"{int(row['cik_str']):010d}", row["title"])
    return out


def candidate_filings(col: C.Collector, cik10: str) -> list[dict[str, str]]:
    """返回该公司的 10-K / 10-Q 主文档候选（10-K 在前，各自按时间倒序）。"""
    data = col.get_json(f"https://data.sec.gov/submissions/CIK{cik10}.json")
    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    rows: list[dict[str, str]] = []
    for i, form in enumerate(forms):
        if form not in ("10-K", "10-Q"):
            continue
        doc = recent["primaryDocument"][i]
        if not doc.lower().endswith((".htm", ".html")):
            continue
        acc = recent["accessionNumber"][i].replace("-", "")
        rows.append({
            "form": form,
            "date": recent["filingDate"][i],
            "url": (
                f"https://www.sec.gov/Archives/edgar/data/{int(cik10)}/{acc}/{doc}"
            ),
        })
    # 10-K 优先、组内时间倒序
    tenk = sorted([r for r in rows if r["form"] == "10-K"], key=lambda r: r["date"], reverse=True)
    tenq = sorted([r for r in rows if r["form"] == "10-Q"], key=lambda r: r["date"], reverse=True)
    return (tenk + tenq)[:MAX_FILINGS_PER_COMPANY]


def main() -> C.Collector:
    col = C.Collector(SOURCE, min_interval=0.5, ua_style="plain")
    try:
        cik_map = load_cik_map(col)
    except (C.FetchError, ValueError) as exc:
        C.echo(f"致命：拿不到 company_tickers.json：{exc}")
        col.skip("sec_tickers", TICKERS_URL, f"company_tickers.json 失败：{exc}")
        return col

    accepted = 0
    for ticker in CANDIDATES:
        if accepted >= TARGET:
            break
        info = cik_map.get(ticker)
        if not info:
            col.skip(f"sec_{ticker.lower()}", TICKERS_URL, "ticker 不在 company_tickers.json 里")
            continue
        cik10, company = info
        try:
            filings = candidate_filings(col, cik10)
        except (C.FetchError, ValueError, KeyError) as exc:
            col.skip(f"sec_{ticker.lower()}", f"https://data.sec.gov/submissions/CIK{cik10}.json",
                     f"submissions 解析失败：{exc}")
            continue
        if not filings:
            col.skip(f"sec_{ticker.lower()}", "", "近期无 10-K/10-Q HTML 主文档")
            continue

        got = None
        chosen: dict[str, str] = {}
        reasons: list[str] = []
        for f in filings:
            size = C.head_size(f["url"], col.limiter, col.ua_style)
            if size is not None and size > C.MAX_BYTES:
                reasons.append(f"{f['form']} {f['date']} {size // 1024}KB 超 8MB")
                continue
            item_id = f"sec_{ticker.lower()}_{f['form'].replace('-', '').lower()}_{f['date'].replace('-', '')}"
            try:
                got = col.get(f["url"], f"{item_id}.html", accept="text/html")
            except C.FetchError as exc:
                reasons.append(f"{f['form']} {f['date']}: {exc}")
                got = None
                continue
            chosen = {**f, "id": item_id}
            break

        if got is None or not chosen:
            col.skip(f"sec_{ticker.lower()}", filings[0]["url"], "；".join(reasons) or "全部候选失败")
            continue

        col.add({
            "id": chosen["id"],
            "path": got.rel,
            "format": "html",
            "lang": "en",
            "domain": "business",
            "layout": "table-heavy",
            "source": SOURCE,
            "license": LICENSE,
            "truth_type": "A",
            "truth": f"real/{SOURCE}/{chosen['id']}.truth.json",
            "truth_src": got.rel,
            "url": chosen["url"],
            "sha256": got.sha256,
            "size": got.size,
            "pages": None,
            "note": f"{company} {chosen['form']} {chosen['date']}",
        })
        accepted += 1
        C.echo(f"[{accepted}/{TARGET}] {ticker} {chosen['form']} {chosen['date']} "
               f"{got.size // 1024}KB -> {chosen['id']}")

    if accepted < TARGET:
        C.echo(f"警告：只收到 {accepted}/{TARGET} 份，候选公司已用尽")
    return col


if __name__ == "__main__":
    C.run_source(main)
