"""采集 Wikipedia 渲染后 HTML（8 语言 × 8 领域 = 64 份）。

用法：`python -m bench.fetchers.wikipedia`

要点：
- 取 REST API 的**渲染后 HTML**（`/api/rest_v1/page/html/<title>`）——正文结构完整、
  带 infobox 表格与脚注，HTML 自身即真值（truth_type=A，truth_src 指向自己）。
- 各语言标题用**英文条目的 langlinks** 解析（不硬编码各语标题，避免猜错），
  某语缺条目就换该领域的备选条目。
- **阿拉伯语是 RTL 重点**，note 里标 `RTL`。
- 单份 > 8 MB 或缺条目 → 换备选，仍不行则记跳过清单。
"""

from __future__ import annotations

import sys
import urllib.parse
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from bench.fetchers import _common as C  # type: ignore
else:
    from . import _common as C

SOURCE = "wikipedia"
LICENSE = "CC BY-SA 4.0"
LANGS = ["zh", "en", "es", "de", "fr", "ar", "ja", "ru"]

# 每个领域：英文主条目 + 备选（主条目某语言缺失或体积超限时用）
TOPICS: list[tuple[str, str, list[str]]] = [
    # (domain, 主条目, 备选)
    ("med", "Diabetes", ["Insulin", "Tuberculosis"]),
    ("law", "Copyright", ["Trademark", "Human rights"]),
    ("math", "Prime number", ["Pythagorean theorem", "Logarithm"]),
    ("it", "Hypertext Transfer Protocol", ["Domain Name System", "Operating system"]),
    ("news", "Newspaper", ["Journalism", "News agency"]),
    ("edu", "University", ["Primary education", "Library"]),
    ("business", "Stock market", ["Inflation", "Accounting"]),
    ("gov", "United Nations", ["Passport", "Parliament"]),
]


def slug(title: str) -> str:
    out = []
    for ch in title.lower():
        out.append(ch if ch.isalnum() else "_")
    s = "".join(out)
    while "__" in s:
        s = s.replace("__", "_")
    return s.strip("_")


def langlinks(col: C.Collector, title: str) -> dict[str, str]:
    """英文条目 -> {语言代码: 该语标题}，含 en 自身。"""
    url = (
        "https://en.wikipedia.org/w/api.php?action=query&format=json&prop=langlinks"
        "&lllimit=500&redirects=1&titles=" + urllib.parse.quote(title)
    )
    data = col.get_json(url)
    pages = data.get("query", {}).get("pages", {})
    out: dict[str, str] = {}
    for page in pages.values():
        if "missing" in page:
            continue
        out["en"] = page.get("title", title)
        for link in page.get("langlinks", []) or []:
            out[link["lang"]] = link["*"]
    return out


def rest_html_url(lang: str, title: str) -> str:
    return (
        f"https://{lang}.wikipedia.org/api/rest_v1/page/html/"
        + urllib.parse.quote(title.replace(" ", "_"), safe="")
    )


def main() -> C.Collector:
    col = C.Collector(SOURCE)
    for domain, primary, alternates in TOPICS:
        titles_by_lang: dict[str, list[tuple[str, str]]] = {lang: [] for lang in LANGS}
        for seed in [primary] + alternates:
            try:
                links = langlinks(col, seed)
            except (C.FetchError, ValueError) as exc:
                C.echo(f"  [warn] langlinks 失败 {seed}: {exc}")
                continue
            for lang in LANGS:
                if lang in links:
                    titles_by_lang[lang].append((seed, links[lang]))

        for lang in LANGS:
            item_id = f"wiki_{lang}_{slug(primary)}"
            attempts = titles_by_lang[lang]
            if not attempts:
                col.skip(item_id, "", f"{lang} 维基缺该领域全部候选条目（{primary} 及备选）")
                continue
            last_reason = ""
            for seed, title in attempts:
                url = rest_html_url(lang, title)
                fname = f"wiki_{lang}_{slug(seed)}.html"
                try:
                    got = col.get(url, fname, accept="text/html")
                except C.FetchError as exc:
                    last_reason = f"{title}: {exc}"
                    continue
                entry_id = f"wiki_{lang}_{slug(seed)}"
                col.add({
                    "id": entry_id,
                    "path": got.rel,
                    "format": "html",
                    "lang": lang,
                    "domain": domain,
                    "layout": "single-column",
                    "source": SOURCE,
                    "license": LICENSE,
                    "truth_type": "A",
                    "truth": f"real/{SOURCE}/{entry_id}.truth.json",
                    "truth_src": got.rel,
                    "url": url,
                    "sha256": got.sha256,
                    "size": got.size,
                    "pages": None,
                    "note": "RTL" if lang == "ar" else "",
                })
                C.echo(f"  [{lang}/{domain}] {title} {got.size // 1024}KB -> {entry_id}")
                break
            else:
                col.skip(item_id, "", f"全部候选条目下载失败/超限：{last_reason}")
    return col


if __name__ == "__main__":
    C.run_source(main)
