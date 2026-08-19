"""采集来源 `govcn`：中国政府网及各部委站点的公开 PDF 公文。

为什么要这一档：中文公文 PDF 的排版链（方正 / WPS / Quartz / InDesign）经常写出错的
ToUnicode 表，抽出来的汉字落在「康熙部首」等兼容码位区——肉眼一样但 grep/分词全失效。
这是 AImorsel `normalize_compat_chars()` 的正面测点，所以本档**优先挑 producer 含
Quartz / Founder / 方正 的文件**（判断不到时也收，producer 一律写进 note）。

用法：

    python -m bench.fetchers.govcn --target 30

    # 或直接跑脚本
    python bench/fetchers/govcn.py --target 30 --pool 60

策略：两级爬取（栏目页 → 正文页 → .pdf 附件直链）。政府站的 PDF 几乎都挂在正文页上，
栏目页本身通常没有直链。抓不到就换下一个种子，单点失败不影响整批。

许可：中国政府网公开信息。只留 URL + 校验和入库，语料本身不再分发
（`bench/corpus/` 已 gitignore）。
"""

from __future__ import annotations

import argparse
import json
import re
import urllib.parse
from pathlib import Path
from typing import Any

try:
    from . import _common as C
except ImportError:                                       # 直接当脚本跑
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from bench.fetchers import _common as C               # type: ignore

SOURCE = "govcn"

# 栏目页种子。挑的都是「公文 / 公报 / 报告」类栏目，正文页大概率挂 PDF 附件。
# 某个站改版或 412/504 时脚本会跳过它继续跑，加新种子不影响已入库的条目。
SEEDS: list[str] = [
    # 中国政府网
    "https://www.gov.cn/zhengce/index.htm",
    "https://www.gov.cn/zhengce/jiedu/index.htm",
    # 审计署（审计工作报告，PDF 附件最多）
    "https://www.audit.gov.cn/n5/n26/index.html",
    "https://www.audit.gov.cn/n5/n25/index.html",
    "https://www.audit.gov.cn/n5/n24/index.html",
    "https://www.audit.gov.cn/n6/index.html",
    # 中国人民银行（公告、统计报告）
    "https://www.pbc.gov.cn/goutongjiaoliu/113456/113469/index.html",
    "https://www.pbc.gov.cn/zhengcehuobisi/125207/125213/125440/index.html",
    "https://www.pbc.gov.cn/diaochatongjisi/116219/116225/index.html",
    "https://www.pbc.gov.cn/tiaofasi/144941/144959/index.html",
    # 财政部
    "https://www.mof.gov.cn/gkml/",
    "https://www.mof.gov.cn/zhengwuxinxi/caizhengxinwen/",
    # 发改委
    "https://www.ndrc.gov.cn/xxgk/zcfb/ghwb/",
    "https://www.ndrc.gov.cn/xxgk/zcfb/tz/",
    "https://www.ndrc.gov.cn/xxgk/zcfb/ghxwj/",
    "https://www.ndrc.gov.cn/xxgk/zcfb/gg/",
    # 科技部
    "https://www.most.gov.cn/xxgk/xinxifenlei/fdzdgknr/",
    # 生态环境部
    "https://www.mee.gov.cn/hjzl/sthjzk/zghjzkgb/",
    "https://www.mee.gov.cn/xxgk2018/xxgk/xxgk02/",
    # 农业农村部
    "https://www.moa.gov.cn/gk/tzgg_1/tfw/",
    "https://www.moa.gov.cn/govpublic/",
    # 国家统计局
    "https://www.stats.gov.cn/sj/zxfb/",
    "https://www.stats.gov.cn/sj/tjgb/ndtjgb/",
    # 教育部 / 工信部 / 自然资源部 / 应急管理部（能通就抓）
    "https://www.moe.gov.cn/jyb_xxgk/moe_1777/moe_1778/",
    "https://www.miit.gov.cn/jgsj/index.html",
    "https://www.mnr.gov.cn/gk/tzgg/",
    "https://www.mem.gov.cn/gk/tzgg/",
    # 国家卫健委（med 领域补充）
    "https://www.nhc.gov.cn/wjw/gfxwj/wenjian.shtml",
    # 最高人民法院（law 领域补充）
    "https://www.court.gov.cn/fabu.html",
]

MAX_PER_HOST = 10         # 单站最多入选份数，避免 30 份全来自一个站
DETAIL_PER_SEED = 20      # 每个栏目页最多深入几个正文页

_PDF_RE = re.compile(r"""["'(]([^"'()\s]{1,300}?\.pdf)["')]""", re.I)
_HREF_RE = re.compile(r'href\s*=\s*["\']([^"\']+)["\']', re.I)


def _host_label(url: str) -> str:
    netloc = urllib.parse.urlparse(url).netloc.lower()
    netloc = re.sub(r"^www\.", "", netloc)
    netloc = re.sub(r"\.gov\.cn$|\.org\.cn$|\.com\.cn$", "", netloc)
    return C.slug(netloc, 20)


def _pdf_links(page_url: str, html: str) -> list[str]:
    out: list[str] = []
    for raw in _PDF_RE.findall(html):
        # 有的站用 /zxfile/reader?file=https://...pdf 包一层预览器
        if "http" in raw[1:]:
            raw = raw[raw.rindex("http"):]
        out.append(urllib.parse.urljoin(page_url, raw.replace("\\", "/")))
    return C.iter_unique(out)


def _detail_links(page_url: str, html: str) -> list[str]:
    host = urllib.parse.urlparse(page_url).netloc.lower()
    out: list[str] = []
    for raw in _HREF_RE.findall(html):
        if raw.startswith(("#", "javascript:", "mailto:")):
            continue
        full = urllib.parse.urljoin(page_url, raw)
        parts = urllib.parse.urlparse(full)
        if parts.netloc.lower() != host:
            continue
        if not parts.path.endswith((".htm", ".html", ".shtml")):
            continue
        # 正文页的典型形态：content_123456.htm / art_xx.html / c10758168/ / /202608/
        if not re.search(r"(content_?\d{3,}|art_|/c\d{5,}|/\d{6}/|t2\d{7})", parts.path):
            continue
        out.append(full)
    return C.iter_unique(out)


def harvest(client: C.Client, seeds: list[str]) -> list[str]:
    """两级爬取，返回候选 PDF 直链（保持发现顺序，按站点交错以利多样性）。"""
    per_host: dict[str, list[str]] = {}
    for seed in seeds:
        try:
            html = client.get_text(seed, timeout=25)
        except C.FetchError as exc:
            C.log(f"  [种子跳过] {seed} —— {exc}")
            continue
        found = _pdf_links(seed, html)
        details = _detail_links(seed, html)[:DETAIL_PER_SEED]
        for detail in details:
            try:
                dhtml = client.get_text(detail, timeout=25)
            except C.FetchError:
                continue
            found.extend(_pdf_links(detail, dhtml))
        found = C.iter_unique(found)
        C.log(f"  {seed} → 正文页 {len(details)}，PDF 候选 {len(found)}")
        for url in found:
            per_host.setdefault(_host_label(url), []).append(url)

    # 按站点轮转交错，保证前若干个候选覆盖多个来源
    ordered: list[str] = []
    lists = [C.iter_unique(v) for v in per_host.values()]
    idx = 0
    while any(idx < len(lst) for lst in lists):
        for lst in lists:
            if idx < len(lst):
                ordered.append(lst[idx])
        idx += 1
    return C.iter_unique(ordered)


def _load_pool(shard: C.Shard) -> dict[str, Any]:
    path = shard.dir / "_pool.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _save_pool(shard: C.Shard, pool: dict[str, Any]) -> None:
    (shard.dir / "_pool.json").write_text(
        json.dumps(pool, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def build_row(url: str, path: Path, info: dict[str, Any]) -> dict[str, Any]:
    ident = f"{SOURCE}_{_host_label(url)}_{C.url_hash(url)}"
    flag = C.producer_flag(info)
    note_bits = [
        f"producer={info.get('producer') or info.get('creator') or '-'}".replace(" ", "_"),
        f"cpp={info['chars_per_page']}",
    ]
    if flag:
        note_bits.append(f"compat_risk={flag}")
    return C.manifest_row(
        ident=ident,
        path=C.rel_to_corpus(path),
        fmt=C.pdf_format(info),
        lang="zh",
        domain="gov",
        layout=C.pdf_layout(info),
        source=SOURCE,
        license_="中国政府网公开信息",
        truth_type="C",
        truth="",
        truth_src="",
        url=url,
        sha256=info["sha256"],
        size=info["size"],
        pages=info["pages"],
        note="; ".join(note_bits),
    )


def collect(target: int, pool_size: int, budget_mb: int) -> None:
    shard = C.Shard(SOURCE)
    client = C.Client(min_interval=0.5, budget_bytes=budget_mb * 1024 * 1024)

    pool = _load_pool(shard)
    accepted = {u: v for u, v in pool.items() if v.get("ok")}
    C.log(f"[govcn] 已有 manifest {len(shard)} 份，候选池已接受 {len(accepted)} 份")

    if len(accepted) < pool_size:
        C.log("[govcn] 爬取候选 PDF 直链……")
        candidates = harvest(client, SEEDS)
        C.log(f"[govcn] 候选 {len(candidates)} 个，开始下载校验（目标池 {pool_size}）")
        for url in candidates:
            if len(accepted) >= pool_size:
                break
            if url in pool:
                continue
            ident = f"{SOURCE}_{_host_label(url)}_{C.url_hash(url)}"
            dest = shard.dir / f"{ident}.pdf"
            if dest.exists() and shard.is_done(ident):
                pool[url] = {"ok": True, "file": dest.name, "info": shard.rows[ident]}
                accepted[url] = pool[url]
                continue
            info, reason = C.fetch_pdf_candidate(client, url, dest)
            if info is None:
                pool[url] = {"ok": False, "reason": reason}
                shard.skip(url, reason)
                C.log(f"    ✗ {reason}  {url[:110]}")
            else:
                pool[url] = {"ok": True, "file": dest.name, "info": info}
                accepted[url] = pool[url]
                C.log(
                    f"    ✓ {info['pages']}p {info['size']//1024}KB "
                    f"producer={info.get('producer') or info.get('creator') or '-'}  {url[:90]}"
                )
            _save_pool(shard, pool)
    _save_pool(shard, pool)

    # ---- 选片：优先 Quartz/方正，其次单站配额均衡 ----------------------------
    def score(item: tuple[str, dict[str, Any]]) -> tuple[int, int]:
        url, rec = item
        info = rec["info"]
        flag = C.producer_flag(info) if "chars_per_page" in info else ""
        return (0 if flag else 1, 0 if info.get("pages", 0) >= 3 else 1)

    ranked = sorted(accepted.items(), key=score)
    per_host: dict[str, int] = {}
    chosen: list[tuple[str, dict[str, Any]]] = []
    for url, rec in ranked:
        if len(chosen) >= target:
            break
        host = _host_label(url)
        if per_host.get(host, 0) >= MAX_PER_HOST:
            continue
        per_host[host] = per_host.get(host, 0) + 1
        chosen.append((url, rec))
    # 配额挡下来的补位（宁可多来自一个站，也别少于目标份数）
    if len(chosen) < target:
        picked = {u for u, _ in chosen}
        for url, rec in ranked:
            if len(chosen) >= target:
                break
            if url not in picked:
                chosen.append((url, rec))

    for url, rec in chosen:
        path = shard.dir / rec["file"]
        if not path.exists():
            shard.skip(url, "选片时文件已不在磁盘上")
            continue
        info = rec["info"]
        if "chars_per_page" not in info:      # 复用了 manifest 里的旧行
            shard.add(info)
            continue
        shard.add(build_row(url, path, info))

    shard.save()
    C.summarize(shard)
    C.log(f"  本轮请求 {client.requests} 次，下载 {client.downloaded/1024/1024:.1f} MB")


def main(argv: list[str] | None = None) -> int:
    C.ensure_utf8_stdio()
    ap = argparse.ArgumentParser(description="采集中国政府网 / 部委公开 PDF 作评测语料")
    ap.add_argument("--target", type=int, default=30, help="入 manifest 的目标份数")
    ap.add_argument("--pool", type=int, default=60, help="候选池大小（多下一些好挑 Quartz/方正）")
    ap.add_argument("--budget-mb", type=int, default=400, help="本轮下载量上限（MB）")
    args = ap.parse_args(argv)
    return C.run_guarded(lambda: collect(args.target, args.pool, args.budget_mb))


if __name__ == "__main__":
    raise SystemExit(main())
