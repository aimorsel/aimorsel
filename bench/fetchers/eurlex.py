"""EUR-Lex 平行语料采集：同一部法规的 EN/DE/FR/ES × (PDF + HTML)。

用法：``python -m bench.fetchers.eurlex`` 或 ``python bench/fetchers/eurlex.py``

## 为什么不用 eur-lex.europa.eu 的直链

``https://eur-lex.europa.eu/legal-content/{LANG}/TXT/PDF/?uri=CELEX:{celex}`` 实测**恒回
HTTP 202**，正文是 AWS WAF 的 JS 挑战页（``window.gokuProps`` / ``awsWafCookieDomainList``），
带浏览器 UA、Accept-Language、Referer、cookie jar 反复重试都拿不到内容——它要求执行
JS/WASM 才发 challenge cookie，纯 HTTP 客户端过不去。

改走**出版局 CELLAR 的内容协商接口**（官方机器可读入口，无 WAF）：

    GET https://publications.europa.eu/resource/celex/{CELEX}
        Accept: application/pdf;type=pdfa1a   （拿不到时退回 application/pdf）
        Accept: application/xhtml+xml         （HTML 版）
        Accept-Language: eng|deu|fra|spa

同一份 CELEX 的 PDF 与 HTML 内容一致，因此**同语言的 HTML 就是 PDF 的真值源**（truth_type=A）。
HTML 那一份自己入 manifest 时，真值源指向自身——考的是 HTML→结构树，参考解析由真值脚本另写。

## 选材

法规必须四种语言的 PDF 都 ≤ 30 页 / ≤ 8 MB，且四种语言的 HTML 都存在，才整组入库；
任一项不满足则整组跳过、换池子里下一部。候选池按篇幅从短到长排，取前 ``TARGET_ACTS`` 部。
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):  # 允许直接 python bench/fetchers/eurlex.py
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

SOURCE = "eurlex"
LICENSE = "© European Union, 可再使用（EUR-Lex 再利用政策）"
CELLAR = "https://publications.europa.eu/resource/celex/"
EURLEX_DIRECT = "https://eur-lex.europa.eu/legal-content/{lang2}/TXT/{kind}/?uri=CELEX:{celex}"

# (三字母语言码, manifest 的 lang, EUR-Lex 页面用的两字母码)
LANGS = [("eng", "en", "EN"), ("deu", "de", "DE"), ("fra", "fr", "FR"), ("spa", "es", "ES")]

# 候选池：按英文版页数从短到长。都是中等篇幅的单栏法律文本，含大量编号条款、脚注、
# 少量表格；篇幅长的名法（GDPR 88 页 / DSA 102 页 / AI Act 144 页 / MiCA / DMA / NIS2）
# 全部超 30 页硬上限，故不在池中。
CANDIDATES = [
    ("32011R0182", "委员会执行权力监督条例（Comitology）"),
    ("32006L0114", "误导性与比较广告指令"),
    ("32018R1807", "非个人数据自由流动条例"),
    ("32013R0524", "消费者在线争议解决条例（ODR）"),
    ("32015L1535", "技术法规信息程序指令"),
    ("32018R0302", "反不合理地域封锁条例"),
    ("32016L0943", "商业秘密保护指令"),
    ("32019R1150", "平台对企业公平性条例（P2B）"),
    # 以下为备选，前面有整组不合格时顶上
    ("32019L0771", "商品销售合同指令"),
    ("32011L0083", "消费者权利指令"),
    ("32017R2394", "消费者保护合作条例（CPC）"),
    ("32014L0026", "著作权集体管理指令"),
]

TARGET_ACTS = 8
PDF_ACCEPTS = ["application/pdf;type=pdfa1a", "application/pdf"]
HTML_ACCEPT = "application/xhtml+xml"


def _cellar_get(limiter: RateLimiter, celex: str, accept: str, lang3: str):
    return http_get(
        CELLAR + celex,
        limiter,
        headers={"Accept": accept, "Accept-Language": lang3, "User-Agent": BROWSER_UA},
        retries=3,
        retry_202=3,
    )


def probe_direct_eurlex(limiter: RateLimiter) -> bool:
    """探一次 eur-lex.europa.eu 直链是否可用（记录 WAF 结论，不用于批量下载）。"""
    url = EURLEX_DIRECT.format(lang2="EN", kind="PDF", celex="32011R0182")
    try:
        resp = http_get(
            url,
            limiter,
            headers={
                "User-Agent": BROWSER_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-GB,en;q=0.9",
                "Referer": "https://eur-lex.europa.eu/homepage.html",
            },
            retries=2,
            retry_202=5,
            retry_202_sleep=5.0,
        )
    except FetchError as exc:
        log(f"eur-lex 直链不可用（{exc}）→ 走 CELLAR 内容协商")
        return False
    if resp.body[:5] == b"%PDF-":
        log("eur-lex 直链可用（返回真 PDF）")
        return True
    log("eur-lex 直链返回非 PDF（AWS WAF 挑战页）→ 走 CELLAR 内容协商")
    return False


def fetch_act(coll: Collector, celex: str, title: str) -> list[dict] | None:
    """取一部法规的 4 语言 × (PDF+HTML)。全部合格才返回 8 行，否则 None。"""
    low = celex.lower()
    staged: list[tuple[Path, bytes]] = []
    rows: list[dict] = []

    for lang3, lang2, _page_code in LANGS:
        html_rel = f"real/{SOURCE}/{SOURCE}_{low}_{lang2}.html"
        pdf_rel = f"real/{SOURCE}/{SOURCE}_{low}_{lang2}.pdf"
        pdf_id = f"{SOURCE}_{low}_{lang2}_pdf"
        html_id = f"{SOURCE}_{low}_{lang2}_html"

        # ---- HTML（既是被测件，也是同语言 PDF 的真值源）
        cached = coll.cached(html_id, html_rel)
        if cached:
            html_bytes = (CORPUS_DIR / html_rel).read_bytes()
            html_sha, html_size = cached["sha256"], cached["size"]
            coll.reused += 1
        else:
            try:
                resp = _cellar_get(coll.limiter, celex, HTML_ACCEPT, lang3)
            except FetchError as exc:
                coll.skip(html_id, CELLAR + celex, f"HTML 取不到：{exc}")
                return None
            html_bytes = resp.body
            if len(html_bytes) > MAX_BYTES:
                coll.skip(html_id, CELLAR + celex, f"HTML {len(html_bytes)/1e6:.1f}MB 超 8MB 上限")
                return None
            html_sha, html_size = sha256_bytes(html_bytes), len(html_bytes)
            staged.append((CORPUS_DIR / html_rel, html_bytes))
            coll.note_download(html_size)

        rows.append(manifest_row(
            doc_id=html_id, rel_path=html_rel, fmt="html", lang=lang2, domain="law",
            layout="single-column", source=SOURCE, license_=LICENSE, truth_type="A",
            truth=f"real/{SOURCE}/{html_id}.truth.json", truth_src=html_rel,
            url=CELLAR + celex, sha256=html_sha, size=html_size, pages=None,
            note=f"{title}；CELLAR 内容协商 Accept={HTML_ACCEPT} Accept-Language={lang3}；"
                 f"真值源即本文件（考 HTML→结构树，参考解析另写）",
        ))

        # ---- PDF
        cached = coll.cached(pdf_id, pdf_rel)
        if cached:
            pdf_sha, pdf_size, pages = cached["sha256"], cached["size"], cached["pages"]
            used_accept = "（复用上一轮）"
            coll.reused += 1
        else:
            pdf_bytes = b""
            used_accept = ""
            last = ""
            for accept in PDF_ACCEPTS:
                try:
                    resp = _cellar_get(coll.limiter, celex, accept, lang3)
                except FetchError as exc:
                    last = str(exc)
                    continue
                if resp.body[:5] == b"%PDF-":
                    pdf_bytes, used_accept = resp.body, accept
                    break
                last = f"返回非 PDF（{resp.content_type}）"
            if not pdf_bytes:
                coll.skip(pdf_id, CELLAR + celex, f"PDF 取不到：{last}")
                return None
            if len(pdf_bytes) > MAX_BYTES:
                coll.skip(pdf_id, CELLAR + celex, f"PDF {len(pdf_bytes)/1e6:.1f}MB 超 8MB 上限")
                return None
            pages = pdf_page_count(pdf_bytes)
            if pages is None:
                coll.skip(pdf_id, CELLAR + celex, "PDF 页数数不出来（pymupdf 打不开）")
                return None
            if pages > MAX_PAGES:
                coll.skip(pdf_id, CELLAR + celex, f"PDF {pages} 页超 {MAX_PAGES} 页上限")
                return None
            pdf_sha, pdf_size = sha256_bytes(pdf_bytes), len(pdf_bytes)
            staged.append((CORPUS_DIR / pdf_rel, pdf_bytes))
            coll.note_download(pdf_size)

        rows.append(manifest_row(
            doc_id=pdf_id, rel_path=pdf_rel, fmt="pdf", lang=lang2, domain="law",
            layout="single-column", source=SOURCE, license_=LICENSE, truth_type="A",
            truth=f"real/{SOURCE}/{pdf_id}.truth.json", truth_src=html_rel,
            url=CELLAR + celex, sha256=pdf_sha, size=pdf_size, pages=pages,
            note=f"{title}；CELLAR 内容协商 Accept={used_accept} Accept-Language={lang3}；"
                 f"真值源为同语言 HTML 版",
        ))

    for path, data in staged:
        write_atomic(path, data)
    return rows


def main() -> int:
    coll = Collector(SOURCE, RateLimiter(2.0))
    probe_direct_eurlex(coll.limiter)

    done = 0
    for celex, title in CANDIDATES:
        if done >= TARGET_ACTS:
            break
        if coll.budget_left() <= 0:
            log("下载预算用尽，停止")
            break
        log(f"[{SOURCE}] {celex} {title}")
        rows = fetch_act(coll, celex, title)
        if rows is None:
            log(f"  {celex} 整组不合格，换下一部")
            continue
        for row in rows:
            coll.add(row)
        done += 1
        log(f"  ✓ {celex} 8 份入库（累计 {done}/{TARGET_ACTS} 部）")

    coll.flush()
    log(coll.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
