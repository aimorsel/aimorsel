"""日本 e-Gov 法令（laws.e-gov.go.jp）采集：docx + HTML 被测件 + 官方 XML 真值源。

用法：``python -m bench.fetchers.egov_jp`` 或 ``python bench/fetchers/egov_jp.py``

## API 实测结论（与预想不同，以实测为准）

- v1 ``/api/1/lawlists/1`` 一次拿全部现行法令的 ``LawId``/``LawName``（约 9000 条，2.8 MB），
  用来把法令名解析成 lawId。
- v2 ``/api/2/law_file/{file_type}/{lawId}`` 才是取正文的接口，``file_type`` 实测只接受
  **xml / json / html / docx / rtf**；``pdf`` 与 ``csv`` 一律 400
  ``{"code":"400042","message":"ファイル種別（file_type）が誤っています。"}``。
  → **e-Gov 不提供法令 PDF**，因此被测件取 **docx**（官方渲染）与 **html** 两种，
  真值源取同一部法令的官方 **XML**（truth_type=A）。
- 网页 ``https://laws.e-gov.go.jp/law/{lawId}`` 只是 SPA 外壳（约 800 字节，正文靠 JS 拉），
  **不能当 HTML 语料**，所以 HTML 也走 law_file 接口。

## 选材

docx/html 无物理页概念，用**官方 XML 去标签后的正文字符数**当篇幅代理：
日文法令排版约 1.5 千字/页，故上限取 ``MAX_JA_CHARS``（≈ 30 页）。超限就换池子里下一部
（個人情報保護法、著作権法、電気通信事業法这类大法会被这条挡掉）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bench.fetchers.common import (  # noqa: E402
    CORPUS_DIR,
    MAX_BYTES,
    Collector,
    FetchError,
    RateLimiter,
    cache_get,
    http_get,
    log,
    manifest_row,
    sha256_bytes,
    write_atomic,
)

SOURCE = "egov_jp"
LICENSE = "公開（日本国 e-Gov、標準利用規約）"
LAWLISTS_URL = "https://laws.e-gov.go.jp/api/1/lawlists/1"
FILE_URL = "https://laws.e-gov.go.jp/api/2/law_file/{ft}/{law_id}"
TARGET = 15
MAX_JA_CHARS = 45_000  # ≈ 30 页日文法令正文

# 候选池：中小型法令优先，顺序 ≈ 篇幅从短到长。取前 TARGET 部合格的。
CANDIDATES: list[tuple[str, str]] = [
    ("製造物責任法", "PL 法（产品责任法）"),
    ("労働契約法", "劳动契约法"),
    ("消費者契約法", "消费者契约法"),
    ("官民データ活用推進基本法", "官民数据利用推进基本法"),
    ("デジタル社会形成基本法", "数字社会形成基本法"),
    ("サイバーセキュリティ基本法", "网络安全基本法"),
    ("電子署名及び認証業務に関する法律", "电子签名与认证业务法"),
    ("不正アクセス行為の禁止等に関する法律", "禁止非法访问法"),
    ("特定電子メールの送信の適正化等に関する法律", "特定电子邮件法（反垃圾邮件）"),
    ("公文書等の管理に関する法律", "公文书管理法"),
    ("公益通報者保護法", "公益举报者保护法"),
    ("消費者基本法", "消费者基本法"),
    ("最低賃金法", "最低工资法"),
    ("行政手続法", "行政程序法"),
    ("行政機関の保有する情報の公開に関する法律", "行政机关信息公开法"),
    ("独立行政法人等の保有する情報の公開に関する法律", "独立行政法人信息公开法"),
    ("特定デジタルプラットフォームの透明性及び公正性の向上に関する法律", "特定数字平台透明化法"),
    ("裁判外紛争解決手続の利用の促進に関する法律", "ADR 促进法"),
    ("障害を理由とする差別の解消の推進に関する法律", "残障歧视消除推进法"),
    ("不当景品類及び不当表示防止法", "景品表示法"),
    ("不正競争防止法", "反不正当竞争法"),
    ("行政不服審査法", "行政复议法"),
]

_LAW_RE = re.compile(r"<LawNameListInfo>\s*<LawId>(.*?)</LawId>\s*<LawName>(.*?)</LawName>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def load_law_index(coll: Collector) -> dict[str, str]:
    raw = cache_get(coll.limiter, LAWLISTS_URL, "egov_lawlists_1.xml").decode("utf-8", "replace")
    index: dict[str, str] = {}
    for law_id, name in _LAW_RE.findall(raw):
        index.setdefault(name.strip(), law_id.strip())
    return index


def body_chars(xml_bytes: bytes) -> int:
    text = _TAG_RE.sub("", xml_bytes.decode("utf-8", "replace"))
    return len(re.sub(r"\s+", "", text))


def slugify(law_id: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", law_id.lower()).strip("_")


def fetch_law(coll: Collector, law_id: str, name: str, label: str) -> int:
    """返回本部法令实际入 manifest 的份数（0 = 整部跳过）。"""
    slug = slugify(law_id)
    xml_rel = f"real/{SOURCE}/{SOURCE}_{slug}.xml"
    plans = [
        ("docx", "docx", f"real/{SOURCE}/{SOURCE}_{slug}.docx", f"{SOURCE}_{slug}_docx", 1),
        ("html", "html", f"real/{SOURCE}/{SOURCE}_{slug}.html", f"{SOURCE}_{slug}_html", None),
    ]

    all_cached = coll.cached_aux(xml_rel) and all(
        coll.cached(doc_id, rel) for _ft, _fmt, rel, doc_id, _pg in plans
    )
    if all_cached:
        for _ft, _fmt, rel, doc_id, _pg in plans:
            row = coll.cached(doc_id, rel)
            assert row is not None
            coll.add(row)
            coll.reused += 1
        log(f"  ✓ {name} 复用 {len(plans)} 份")
        return len(plans)

    # ---- 1. 官方 XML（真值源 + 篇幅代理）
    xml_url = FILE_URL.format(ft="xml", law_id=law_id)
    try:
        xres = http_get(xml_url, coll.limiter)
    except FetchError as exc:
        coll.skip(f"{SOURCE}_{slug}", xml_url, f"XML 取不到（无真值源，整部不收）：{exc}")
        return 0
    xml_bytes = xres.body
    if b"<" not in xml_bytes[:200]:
        coll.skip(f"{SOURCE}_{slug}", xml_url, "XML 返回内容不像 XML")
        return 0
    n_chars = body_chars(xml_bytes)
    if n_chars > MAX_JA_CHARS:
        coll.skip(f"{SOURCE}_{slug}", xml_url,
                  f"正文 {n_chars} 字（≈{n_chars // 1500} 页）超 30 页上限")
        return 0

    staged: list[tuple[Path, bytes]] = [(CORPUS_DIR / xml_rel, xml_bytes)]
    rows: list[dict] = []

    # ---- 2. 被测件 docx / html
    for ft, fmt, rel, doc_id, pages in plans:
        url = FILE_URL.format(ft=ft, law_id=law_id)
        cached = coll.cached(doc_id, rel)
        if cached:
            rows.append(cached)
            coll.reused += 1
            continue
        try:
            res = http_get(url, coll.limiter)
        except FetchError as exc:
            coll.skip(doc_id, url, f"{ft} 取不到：{exc}")
            return 0
        data = res.body
        if len(data) > MAX_BYTES:
            coll.skip(doc_id, url, f"{ft} {len(data)/1e6:.1f}MB 超 8MB 上限")
            return 0
        if ft == "docx" and data[:2] != b"PK":
            coll.skip(doc_id, url, "docx 返回内容不是 zip 容器")
            return 0
        if ft == "html" and b"<" not in data[:200]:
            coll.skip(doc_id, url, "html 返回内容不像 HTML")
            return 0
        staged.append((CORPUS_DIR / rel, data))
        rows.append(manifest_row(
            doc_id=doc_id, rel_path=rel, fmt=fmt, lang="ja", domain="law",
            layout="single-column", source=SOURCE, license_=LICENSE, truth_type="A",
            truth=f"real/{SOURCE}/{doc_id}.truth.json", truth_src=xml_rel,
            url=url, sha256=sha256_bytes(data), size=len(data), pages=pages,
            note=f"{label}；{name}（lawId {law_id}）；正文约 {n_chars} 字；"
                 f"真值源为官方 XML（{xml_url}）；e-Gov 不提供法令 PDF（file_type=pdf 返回 400）",
        ))

    total = 0
    for path, data in staged:
        write_atomic(path, data)
        total += len(data)
    coll.note_download(total)
    for row in rows:
        coll.add(row)
    log(f"  ✓ {name} {len(rows)} 份入库（正文 {n_chars} 字，XML {len(xml_bytes)/1e3:.0f}KB）")
    return len(rows)


def main() -> int:
    coll = Collector(SOURCE, RateLimiter(2.0))
    index = load_law_index(coll)
    log(f"[{SOURCE}] 法令一览收录 {len(index)} 部")

    done = 0
    for name, label in CANDIDATES:
        if done >= TARGET:
            break
        if coll.budget_left() <= 0:
            log("下载预算用尽，停止")
            break
        law_id = index.get(name)
        if not law_id:
            coll.skip(f"{SOURCE}_{name}", LAWLISTS_URL, "法令一览里没有这个法令名")
            continue
        log(f"[{SOURCE}] {name}（{label}）")
        if fetch_law(coll, law_id, name, label):
            done += 1

    coll.flush()
    log(coll.summary() + f"，覆盖 {done} 部法令")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
