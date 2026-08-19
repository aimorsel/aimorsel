"""采集来源 `cninfo`：巨潮资讯网（深交所指定信息披露平台）的定期报告 PDF。

为什么要这一档：中文表格评测主力。上市公司定期报告里满是三线表、跨行跨列表头、
千分位数字和括号负数，是 `cell_f1` / `table_detect_r` 最难的一档。

**只收短件**：年报动辄几百页几十 MB，硬约束（≤ 8 MB / ≤ 30 页）会全部挡掉，所以查询
接口只问「第一季度报告 / 第三季度报告 / 半年度报告摘要」这类小体量文件。

公开查询接口（POST，2026-08 实测可用）：

    http://www.cninfo.com.cn/new/hisAnnouncement/query
    pageNum=1&pageSize=30&column=szse&tabName=fulltext&category=category_yjdbg_szsh&isHLtitle=true

返回 JSON 的 ``announcements[]`` 里 ``adjunctUrl`` 是相对路径，实际下载地址是
``http://static.cninfo.com.cn/`` + adjunctUrl；``adjunctSize`` 单位是 KB，可先按体积粗筛，
省掉一次无用下载。

用法：

    python -m bench.fetchers.cninfo --target 20

许可：公开披露信息（仅评测使用，不再分发）。`bench/corpus/` 已 gitignore。
"""

from __future__ import annotations

import argparse
import urllib.parse
from typing import Any

try:
    from . import _common as C
except ImportError:                                       # 直接当脚本跑
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from bench.fetchers import _common as C               # type: ignore

SOURCE = "cninfo"
QUERY_URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
STATIC_BASE = "http://static.cninfo.com.cn/"

# 只问短件类目。category 取值来自巨潮的公开筛选参数。
CATEGORIES = [
    ("category_yjdbg_szsh", "一季度报告"),
    ("category_sjdbg_szsh", "三季度报告"),
    ("category_bndbg_szsh", "半年度报告"),
]
PAGE_SIZE = 30
MAX_PAGES_PER_CATEGORY = 8      # 每个类目最多翻几页查询结果
MAX_PER_COMPANY = 2             # 同一家公司最多收几份，保证行业/排版多样性
MAX_KB = C.MAX_BYTES // 1024
MIN_CJK_RATIO = 0.15            # 巨潮同时挂英文版报告，本档要中文表格，低于此比例丢弃


def query(client: C.Client, category: str, page_num: int) -> list[dict[str, Any]]:
    form = {
        "pageNum": str(page_num),
        "pageSize": str(PAGE_SIZE),
        "column": "szse",
        "tabName": "fulltext",
        "plate": "",
        "stock": "",
        "searchkey": "",
        "secid": "",
        "category": category,
        "trade": "",
        "seDate": "",
        "sortName": "",
        "sortType": "",
        "isHLtitle": "true",
    }
    body = urllib.parse.urlencode(form).encode("ascii")
    payload = client.get_json(
        QUERY_URL,
        data=body,
        headers={
            "Referer": "http://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice",
            "X-Requested-With": "XMLHttpRequest",
        },
        timeout=30,
    )
    return payload.get("announcements") or []


def build_row(ann: dict[str, Any], url: str, path: Any, info: dict[str, Any]) -> dict[str, Any]:
    code = C.slug(str(ann.get("secCode") or "0"), 8)
    ident = f"{SOURCE}_{code}_{C.slug(str(ann.get('announcementId') or C.url_hash(url)), 14)}"
    note_bits = [
        f"sec={ann.get('secCode')}",
        f"producer={(info.get('producer') or info.get('creator') or '-')}".replace(" ", "_"),
        f"cpp={info['chars_per_page']}",
    ]
    title = (ann.get("announcementTitle") or "").replace("\t", " ").strip()
    if title:
        note_bits.append(f"title={title[:40]}")
    flag = C.producer_flag(info)
    if flag:
        note_bits.append(f"compat_risk={flag}")
    return C.manifest_row(
        ident=ident,
        path=C.rel_to_corpus(path),
        fmt=C.pdf_format(info),
        lang="zh",
        domain="business",
        layout="table-heavy",
        source=SOURCE,
        license_="公开披露信息（仅评测使用，不再分发）",
        truth_type="C",
        truth="",
        truth_src="",
        url=url,
        sha256=info["sha256"],
        size=info["size"],
        pages=info["pages"],
        note="; ".join(note_bits),
    )


def collect(target: int, budget_mb: int) -> None:
    shard = C.Shard(SOURCE)
    client = C.Client(min_interval=0.5, budget_bytes=budget_mb * 1024 * 1024)

    per_company: dict[str, int] = {}
    for row in shard.rows.values():
        code = row["id"].split("_")[1]
        per_company[code] = per_company.get(code, 0) + 1
    C.log(f"[cninfo] 已有 manifest {len(shard)} 份")

    for category, label in CATEGORIES:
        if len(shard) >= target:
            break
        for page_num in range(1, MAX_PAGES_PER_CATEGORY + 1):
            if len(shard) >= target:
                break
            try:
                anns = query(client, category, page_num)
            except C.FetchError as exc:
                C.log(f"  [查询失败] {label} 第 {page_num} 页 —— {exc}")
                shard.skip(f"{category}#p{page_num}", f"查询接口失败：{exc}")
                break
            if not anns:
                C.log(f"  [{label}] 第 {page_num} 页无结果，换类目")
                break
            C.log(f"  [{label}] 第 {page_num} 页 {len(anns)} 条")
            for ann in anns:
                if len(shard) >= target:
                    break
                if (ann.get("adjunctType") or "").upper() != "PDF":
                    continue
                adj = ann.get("adjunctUrl") or ""
                if not adj:
                    continue
                url = STATIC_BASE + adj.lstrip("/")
                code = C.slug(str(ann.get("secCode") or "0"), 8)
                ident = f"{SOURCE}_{code}_{C.slug(str(ann.get('announcementId') or C.url_hash(url)), 14)}"
                if shard.is_done(ident):
                    continue
                if per_company.get(code, 0) >= MAX_PER_COMPANY:
                    continue
                size_kb = ann.get("adjunctSize") or 0
                if isinstance(size_kb, (int, float)) and size_kb > MAX_KB:
                    shard.skip(url, f"体积 {int(size_kb)}KB 超过上限 {MAX_KB}KB（未下载）")
                    continue
                dest = shard.dir / f"{ident}.pdf"
                info, reason = C.fetch_pdf_candidate(client, url, dest)
                if info is None:
                    shard.skip(url, reason)
                    C.log(f"    ✗ {reason}  {ann.get('announcementTitle', '')[:28]}")
                    continue
                if info["cjk_ratio"] < MIN_CJK_RATIO:
                    dest.unlink(missing_ok=True)
                    shard.skip(url, f"汉字占比 {info['cjk_ratio']:.2f} 过低（英文版报告），本档只要中文")
                    C.log(f"    ✗ 英文版（cjk={info['cjk_ratio']:.2f}）  {(ann.get('announcementTitle') or '')[:26]}")
                    continue
                shard.add(build_row(ann, url, dest, info))
                per_company[code] = per_company.get(code, 0) + 1
                C.log(
                    f"    ✓ [{len(shard)}/{target}] {ann.get('secCode')} "
                    f"{info['pages']}p {info['size']//1024}KB "
                    f"{(ann.get('announcementTitle') or '')[:26]}"
                )
                shard.save()

    shard.save()
    C.summarize(shard)
    C.log(f"  本轮请求 {client.requests} 次，下载 {client.downloaded/1024/1024:.1f} MB")


def main(argv: list[str] | None = None) -> int:
    C.ensure_utf8_stdio()
    ap = argparse.ArgumentParser(description="采集巨潮资讯网定期报告 PDF 作评测语料")
    ap.add_argument("--target", type=int, default=20, help="入 manifest 的目标份数")
    ap.add_argument("--budget-mb", type=int, default=300, help="本轮下载量上限（MB）")
    args = ap.parse_args(argv)
    return C.run_guarded(lambda: collect(args.target, args.budget_mb))


if __name__ == "__main__":
    raise SystemExit(main())
