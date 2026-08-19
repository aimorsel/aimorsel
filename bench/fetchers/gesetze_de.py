"""德国联邦法律（gesetze-im-internet.de）采集：PDF 被测件 + 官方 XML 真值源。

用法：``python -m bench.fetchers.gesetze_de`` 或 ``python bench/fetchers/gesetze_de.py``

## 站点结构（实测）

- 全站目录 ``https://www.gesetze-im-internet.de/gii-toc.xml``：6100+ 条
  ``<item><title>…</title><link>…/{slug}/xml.zip</link></item>``，由此拿到每部法律的 slug。
- **PDF 文件名不可推导**（``SigG.pdf`` / ``1._BImSchV.pdf`` / ``BJNR…pdf`` 混用，且已废止的
  法律整目录 404）。必须先取 ``/{slug}/index.html``，从里面的 ``href="….pdf"`` 读真实文件名。
- 官方 XML 在 ``/{slug}/xml.zip`` 里（DTD 见站内 ``dtd/1.0/``），解出唯一的 ``*.xml``
  作为真值源 → truth_type=A。

## 选材

BGB/HGB/StGB/GG 这类法典远超 30 页硬上限，池子里只放中小型法律；每部先看 PDF 页数与体积，
超限就换下一部。候选池顺序 ≈ 篇幅从短到长。
"""

from __future__ import annotations

import io
import re
import sys
import zipfile
from pathlib import Path
from urllib.parse import urljoin

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bench.fetchers.common import (  # noqa: E402
    CORPUS_DIR,
    MAX_BYTES,
    MAX_PAGES,
    Collector,
    FetchError,
    RateLimiter,
    cache_get,
    http_get,
    log,
    manifest_row,
    pdf_page_count,
    sha256_bytes,
    write_atomic,
)

SOURCE = "gesetze_de"
LICENSE = "公开（德国联邦司法部，允许转载）"
BASE = "https://www.gesetze-im-internet.de/"
TOC_URL = BASE + "gii-toc.xml"
TARGET = 15

# slug → 简称。全部已对着 gii-toc.xml 核过存在。顺序 ≈ 篇幅从短到长；取前 TARGET 部合格的。
CANDIDATES: list[tuple[str, str]] = [
    ("prodhaftg", "ProdHaftG 产品责任法"),
    ("kunsturhg", "KunstUrhG 艺术作品著作权法"),
    ("gewschg", "GewSchG 暴力保护法"),
    ("nachwg", "NachwG 劳动条件证明法"),
    ("burlg", "BUrlG 联邦休假法"),
    ("milog", "MiLoG 最低工资法"),
    ("ifg", "IFG 信息自由法"),
    ("netzdg", "NetzDG 社交网络执法改进法"),
    ("geschgehg", "GeschGehG 商业秘密保护法"),
    ("entgtranspg", "EntgTranspG 薪酬透明法"),
    ("agg", "AGG 一般平等待遇法"),
    ("kschg", "KSchG 解雇保护法"),
    ("arbzg", "ArbZG 工作时间法"),
    ("egovg", "EGovG 电子政务促进法"),
    ("tzbfg", "TzBfG 非全日制与定期劳动合同法"),
    ("ttdsg", "TTDSG 电信与数字服务数据保护法"),
    ("bgg", "BGG 残障人士平等法"),
    ("vig", "VIG 消费者健康信息法"),
    ("uwg_2004", "UWG 反不正当竞争法"),
    ("bimschv_1_2010", "1. BImSchV 联邦污染防治法第一号条例"),
    ("bdsg_2018", "BDSG 联邦数据保护法"),
    ("tierschg", "TierSchG 动物保护法"),
]

_ITEM_RE = re.compile(r"<item>\s*<title>(.*?)</title>\s*<link>(.*?)</link>\s*</item>", re.S)
_PDF_HREF_RE = re.compile(r'href="([^"]+\.pdf)"', re.I)


def load_toc(coll: Collector) -> dict[str, str]:
    """slug -> 官方长标题。"""
    raw = cache_get(coll.limiter, TOC_URL, "gii-toc.xml").decode("utf-8", "replace")
    out: dict[str, str] = {}
    for title, link in _ITEM_RE.findall(raw):
        m = re.search(r"gesetze-im-internet\.de/([^/]+)/xml\.zip", link)
        if m:
            out[m.group(1)] = re.sub(r"\s+", " ", title).strip()
    return out


def unescape_title(title: str) -> str:
    from html import unescape

    return unescape(title)


def extract_xml(zip_bytes: bytes) -> bytes | None:
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".xml")]
            if not names:
                return None
            return zf.read(sorted(names)[0])
    except Exception:  # noqa: BLE001
        return None


def fetch_law(coll: Collector, slug: str, label: str, title: str) -> bool:
    doc_id = f"{SOURCE}_{slug}_pdf"
    pdf_rel = f"real/{SOURCE}/{SOURCE}_{slug}.pdf"
    xml_rel = f"real/{SOURCE}/{SOURCE}_{slug}.xml"
    index_url = f"{BASE}{slug}/index.html"

    cached = coll.cached(doc_id, pdf_rel)
    if cached and coll.cached_aux(xml_rel):
        coll.reused += 1
        coll.add(cached)
        log(f"  ✓ {slug} 复用（{cached['pages']} 页）")
        return True

    # ---- 1. 目录页拿真实 PDF 文件名
    try:
        idx = http_get(index_url, coll.limiter)
    except FetchError as exc:
        coll.skip(doc_id, index_url, f"目录页取不到（可能已废止）：{exc}")
        return False
    html = idx.body.decode("iso-8859-1", "replace")
    hrefs = _PDF_HREF_RE.findall(html)
    if not hrefs:
        coll.skip(doc_id, index_url, "目录页里没有 PDF 链接")
        return False
    pdf_url = urljoin(index_url, hrefs[0])

    # ---- 2. PDF 被测件
    try:
        resp = http_get(pdf_url, coll.limiter)
    except FetchError as exc:
        coll.skip(doc_id, pdf_url, f"PDF 取不到：{exc}")
        return False
    pdf_bytes = resp.body
    if pdf_bytes[:5] != b"%PDF-":
        coll.skip(doc_id, pdf_url, f"返回不是 PDF（{resp.content_type}）")
        return False
    if len(pdf_bytes) > MAX_BYTES:
        coll.skip(doc_id, pdf_url, f"PDF {len(pdf_bytes)/1e6:.1f}MB 超 8MB 上限")
        return False
    pages = pdf_page_count(pdf_bytes)
    if pages is None:
        coll.skip(doc_id, pdf_url, "PDF 页数数不出来（pymupdf 打不开）")
        return False
    if pages > MAX_PAGES:
        coll.skip(doc_id, pdf_url, f"PDF {pages} 页超 {MAX_PAGES} 页上限")
        return False

    # ---- 3. 官方 XML 真值源
    zip_url = f"{BASE}{slug}/xml.zip"
    try:
        zres = http_get(zip_url, coll.limiter)
    except FetchError as exc:
        coll.skip(doc_id, zip_url, f"xml.zip 取不到（无真值源，整份不收）：{exc}")
        return False
    xml_bytes = extract_xml(zres.body)
    if not xml_bytes:
        coll.skip(doc_id, zip_url, "xml.zip 里没有 XML（无真值源，整份不收）")
        return False

    write_atomic(CORPUS_DIR / pdf_rel, pdf_bytes)
    write_atomic(CORPUS_DIR / xml_rel, xml_bytes)
    coll.note_download(len(pdf_bytes) + len(zres.body))

    coll.add(manifest_row(
        doc_id=doc_id, rel_path=pdf_rel, fmt="pdf", lang="de", domain="law",
        layout="single-column", source=SOURCE, license_=LICENSE, truth_type="A",
        truth=f"real/{SOURCE}/{doc_id}.truth.json", truth_src=xml_rel,
        url=pdf_url, sha256=sha256_bytes(pdf_bytes), size=len(pdf_bytes), pages=pages,
        note=f"{label}；{title}；真值源为官方 XML（{zip_url} 解包）",
    ))
    log(f"  ✓ {slug} {pages} 页 {len(pdf_bytes)/1e6:.2f}MB，XML {len(xml_bytes)/1e3:.0f}KB")
    return True


def main() -> int:
    coll = Collector(SOURCE, RateLimiter(2.0))
    toc = load_toc(coll)
    log(f"[{SOURCE}] gii-toc 收录 {len(toc)} 部")

    done = 0
    for slug, label in CANDIDATES:
        if done >= TARGET:
            break
        if coll.budget_left() <= 0:
            log("下载预算用尽，停止")
            break
        title = unescape_title(toc.get(slug, ""))
        if not title:
            coll.skip(f"{SOURCE}_{slug}_pdf", BASE + slug, "gii-toc 里没有这个 slug")
            continue
        log(f"[{SOURCE}] {slug} {label}")
        if fetch_law(coll, slug, label, title):
            done += 1

    coll.flush()
    log(coll.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
