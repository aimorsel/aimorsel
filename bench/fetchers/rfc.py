"""采集 IETF RFC 语料（同一内容三形态：html + pdf + txt）。

用法：`python -m bench.fetchers.rfc`（或 `python bench/fetchers/rfc.py`）

要点 / 踩过的坑：
- **PDF 只有 v3 时代（约 RFC 8650 起，2019-11 之后）的 RFC 才有**：
  `rfc8259.pdf` 是 404，`rfc9110.pdf` 才有。所以候选池只放 ≥ 8650 的号。
- 硬上限 30 页把 HTTP 核心那批（9110/9111/9112 等上百页）全挡在外面，
  所以候选池是「较新 + 中等长度」的规范，实际页数以 PDF 为准（pymupdf 数页），
  超限就换下一个候选，直到收满 TARGET 份。
- **txt 形态不入 manifest**（format 取值里没有 txt），只下载留作真值备用；
  真值统一用 html 那份：pdf/html 两份的 `truth_src` 都指向 html 文件。
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):  # 支持直接 python bench/fetchers/rfc.py
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from bench.fetchers import _common as C  # type: ignore
else:
    from . import _common as C

SOURCE = "rfc"
TARGET = 20
BASE = "https://www.rfc-editor.org/rfc"
LICENSE = "IETF Trust / 公开可再分发"

# 候选池：v3 时代（有 PDF）且预计中等长度的 RFC。按顺序尝试，收满 TARGET 即停。
CANDIDATES = [
    9309,  # robots.txt
    9457,  # Problem Details for HTTP APIs
    9205,  # Building Protocols with HTTP
    9116,  # security.txt
    9211,  # Cache-Status header
    9213,  # Target Cache-Control
    9297,  # HTTP Datagrams
    9298,  # Proxying UDP in HTTP
    9412,  # ORIGIN extension in HTTP/3
    9440,  # Client-Cert HTTP header
    8890,  # The Internet is for End Users
    8874,  # Working Group GitHub Usage Guidance
    8875,  # Working Group GitHub Administration
    8996,  # Deprecating TLS 1.0 and TLS 1.1
    8998,  # ARIA in TLS
    9155,  # Deprecating MD5/SHA-1 in TLS
    9106,  # Argon2
    9364,  # DNSSEC
    9210,  # DNS over TCP
    8932,  # Recommendations for DNS Privacy Service Operators
    9199,  # Considerations for Large Authoritative DNS Servers
    9218,  # Extensible Prioritization Scheme for HTTP
    9250,  # DNS over Dedicated QUIC Connections
    9209,  # Proxy-Status HTTP field
    9207,  # OAuth 2.0 Mix-Up Mitigation
    9068,  # JWT Profile for OAuth 2.0 Access Tokens
    9163,  # Expect-CT
    9019,  # SUIT Architecture
    9530,  # Digest Fields
    9518,  # Centralization, Decentralization, and Internet Standards
    9325,  # Recommendations for TLS/DTLS
    8942,  # HTTP Client Hints
    9126,  # OAuth 2.0 Pushed Authorization Requests
    9278,  # JWK Thumbprint URI
    9449,  # OAuth 2.0 DPoP
    9421,  # HTTP Message Signatures
]


def main() -> C.Collector:
    col = C.Collector(SOURCE)
    accepted = 0
    for num in CANDIDATES:
        if accepted >= TARGET:
            break
        rid = f"rfc{num}"
        pdf_url = f"{BASE}/{rid}.pdf"
        html_url = f"{BASE}/{rid}.html"
        txt_url = f"{BASE}/{rid}.txt"

        # 1) PDF 先下：页数是最硬的门槛
        try:
            pdf = col.get(pdf_url, f"{rid}.pdf", accept="application/pdf")
        except C.FetchError as exc:
            col.skip(f"{rid}_pdf", pdf_url, f"PDF 下载失败：{exc}")
            continue
        pages = C.pdf_pages(pdf.path)
        if pages is None:
            col.skip(f"{rid}_pdf", pdf_url, "PDF 无法解析页数")
            continue
        if pages > C.MAX_PAGES:
            col.skip(f"{rid}_pdf", pdf_url, f"{pages} 页 > 上限 {C.MAX_PAGES}，换下一个 RFC")
            pdf.path.unlink(missing_ok=True)
            col.mark_bad(pdf_url, f"{pages} 页 > 上限 {C.MAX_PAGES}")  # 重跑时直接跳过
            continue

        # 2) HTML（真值源）
        try:
            html = col.get(html_url, f"{rid}.html", accept="text/html")
        except C.FetchError as exc:
            col.skip(f"{rid}_html", html_url, f"HTML 下载失败：{exc}")
            continue

        # 3) txt：只下载留作真值备用，不入 manifest
        try:
            col.get(txt_url, f"{rid}.txt", accept="text/plain")
        except C.FetchError as exc:
            col.skip(f"{rid}_txt", txt_url, f"txt 下载失败（不影响入库）：{exc}")

        common = {
            "lang": "en",
            "domain": "it",
            "layout": "spec",
            "source": SOURCE,
            "license": LICENSE,
            "truth_type": "A",
            "truth_src": html.rel,
            "note": "",
        }
        col.add({
            "id": f"{rid}_html",
            "path": html.rel,
            "format": "html",
            "truth": f"real/{SOURCE}/{rid}_html.truth.json",
            "url": html_url,
            "sha256": html.sha256,
            "size": html.size,
            "pages": None,
            **common,
        })
        col.add({
            "id": f"{rid}_pdf",
            "path": pdf.rel,
            "format": "pdf",
            "truth": f"real/{SOURCE}/{rid}_pdf.truth.json",
            "url": pdf_url,
            "sha256": pdf.sha256,
            "size": pdf.size,
            "pages": pages,
            **common,
        })
        accepted += 1
        C.echo(f"[{accepted}/{TARGET}] {rid}: pdf {pages}p {pdf.size // 1024}KB, html {html.size // 1024}KB")

    if accepted < TARGET:
        C.echo(f"警告：只收到 {accepted}/{TARGET} 份 RFC，候选池已用尽")
    return col


if __name__ == "__main__":
    C.run_source(main)
