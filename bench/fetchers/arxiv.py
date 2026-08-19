"""采集来源 `arxiv`：arXiv 上 **CC BY 4.0** 的论文 PDF + LaTeX 源码（A 档真值源）。

为什么要这一档：LaTeX 源码是天然真值——标题层级、段落、表格、公式都在源码里写着，
解析源码即得 ground truth，所以这一档是唯一能算全套结构指标的真实语料（truth_type=A）。
另外它是数学/公式和双栏版面的重点测点。

许可纪律：**只取 `<license>` 含 `creativecommons.org/licenses/by/4.0` 的记录**。
许可信息只在 OAI-PMH 的 `arXiv` metadataPrefix 里有（arXiv API 的 Atom 输出没有），
所以走 OAI：

    https://oaipmh.arxiv.org/oai?verb=ListRecords&metadataPrefix=arXiv&set=math&from=..&until=..

⚠️ 两个实测坑（2026-08）：
1. 老地址 `export.arxiv.org/oai2` 已 **301 到 `oaipmh.arxiv.org/oai`**，且 301 响应体为空
   ——不跟随重定向会静默拿到 0 字节。
2. OAI 首字节要等 **90 秒上下**，Fastly 会先回一个 `503 first byte timeout`；必须给长超时
   并按 `Retry-After` 退避重试（第二次通常命中缓存，秒回）。

限速：arXiv 要求慢——本模块对 arXiv 各主机固定 **≥ 3 秒/请求**。

真值 JSON 不由本脚本生成（另有 LaTeX 解析器负责），这里只把 e-print 源码下下来并填
`truth_src`；`truth` 字段填约定路径 `real/arxiv/<id>.truth.json`（文件不存在时跑批会
优雅跳过打分）。

用法：

    python -m bench.fetchers.arxiv --target 40
"""

from __future__ import annotations

import argparse
import gzip
import io
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterator

try:
    from . import _common as C
except ImportError:                                       # 直接当脚本跑
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from bench.fetchers import _common as C               # type: ignore

SOURCE = "arxiv"
OAI_URL = "https://oaipmh.arxiv.org/oai"
PDF_URL = "https://arxiv.org/pdf/{id}"
EPRINT_URL = "https://arxiv.org/e-print/{id}"
ABS_URL = "https://arxiv.org/abs/{id}"

CC_BY_4 = "creativecommons.org/licenses/by/4.0"
MIN_INTERVAL = 3.0                # arXiv 要求的最小请求间隔
OAI_TIMEOUT = 300.0               # 首字节可能等 90s+
MAX_EPRINT_BYTES = 8 * 1024 * 1024

NS = {
    "oai": "http://www.openarchives.org/OAI/2.0/",
    "arx": "http://arxiv.org/OAI/arXiv/",
}

# 覆盖 math / cs / physics 三个领域，各取一个日期窗口（一天的量就有几百条记录，
# CC-BY 占三成上下，足够挑十几篇）。窗口写死是为了可复现。
WINDOWS: list[tuple[str, str, str]] = [
    ("math", "2025-01-06", "2025-01-07"),
    ("cs", "2025-01-06", "2025-01-07"),
    ("physics", "2025-01-06", "2025-01-07"),
    # 备用窗口：上面挑不够时继续往后取
    ("math", "2025-02-10", "2025-02-11"),
    ("cs", "2025-02-10", "2025-02-11"),
    ("physics", "2025-02-10", "2025-02-11"),
]

DOMAIN_BY_SET = {"cs": "it", "math": "math", "physics": "math"}


def list_records(client: C.Client, set_spec: str, date_from: str, date_until: str) -> Iterator[dict[str, Any]]:
    """遍历一个 set + 日期窗口的全部记录（自动跟 resumptionToken）。"""
    token: str | None = None
    while True:
        if token:
            url = f"{OAI_URL}?verb=ListRecords&resumptionToken={token}"
        else:
            url = (
                f"{OAI_URL}?verb=ListRecords&metadataPrefix=arXiv"
                f"&set={set_spec}&from={date_from}&until={date_until}"
            )
        try:
            raw = client.request(url, timeout=OAI_TIMEOUT)
        except C.FetchError as exc:
            C.log(f"  [OAI 失败] set={set_spec} {date_from} —— {exc}")
            return
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            C.log(f"  [OAI 响应不是 XML] set={set_spec} —— {exc}")
            return
        err = root.find("oai:error", NS)
        if err is not None:
            C.log(f"  [OAI error] {err.get('code')}: {(err.text or '').strip()[:100]}")
            return
        listing = root.find("oai:ListRecords", NS)
        if listing is None:
            return
        for record in listing.findall("oai:record", NS):
            meta = record.find("oai:metadata/arx:arXiv", NS)
            if meta is None:
                continue
            def text(tag: str) -> str:
                node = meta.find(f"arx:{tag}", NS)
                return (node.text or "").strip() if node is not None and node.text else ""
            yield {
                "id": text("id"),
                "title": " ".join(text("title").split()),
                "categories": text("categories"),
                "license": text("license"),
                "set": set_spec,
            }
        tok = listing.find("oai:resumptionToken", NS)
        token = (tok.text or "").strip() if tok is not None and tok.text else ""
        if not token:
            return


def eprint_suffix(raw: bytes) -> str:
    """e-print 的后缀。

    返回 ``""`` 表示**不是 LaTeX 源码**——arXiv 允许「PDF only」投稿，e-print 端点会直接回一份
    PDF，那种没有源码可解析，必须降为 C 档（否则 `make_truth.py` 会在「认不出真值源类型」上报错）。
    后缀必须落在 `make_truth.py` 的 ``LATEX_SUFFIXES`` 里，别自创。
    """
    if raw[:5].startswith(b"%PDF") or raw[:2] != b"\x1f\x8b":
        return ""
    try:
        head = gzip.GzipFile(fileobj=io.BytesIO(raw)).read(1024)
    except OSError:
        return ""
    return ".src.tar.gz" if head[257:262] == b"ustar" else ".src.tex.gz"


def build_row(
    rec: dict[str, Any],
    pdf_url: str,
    pdf_path: Path,
    info: dict[str, Any],
    src_path: Path | None,
) -> dict[str, Any]:
    ident = f"{SOURCE}_{C.slug(rec['id'], 20)}"
    primary = (rec["categories"].split() or ["?"])[0]
    note_bits = [
        f"cat={primary}",
        f"cpp={info['chars_per_page']}",
        f"abs={ABS_URL.format(id=rec['id'])}",
    ]
    if rec["title"]:
        note_bits.append(f"title={rec['title'][:60]}")
    if src_path is None:
        note_bits.append("无 e-print 源码（真值需另寻）")
    return C.manifest_row(
        ident=ident,
        path=C.rel_to_corpus(pdf_path),
        fmt=C.pdf_format(info),
        lang="en",
        domain=DOMAIN_BY_SET.get(rec["set"], "math"),
        layout=C.pdf_layout(info),
        source=SOURCE,
        license_="CC BY 4.0",
        truth_type="A" if src_path is not None else "C",
        truth=f"real/{SOURCE}/{ident}.truth.json" if src_path is not None else "",
        truth_src=C.rel_to_corpus(src_path) if src_path is not None else "",
        url=pdf_url,
        sha256=info["sha256"],
        size=info["size"],
        pages=info["pages"],
        note="; ".join(note_bits),
    )


def collect(target: int, budget_mb: int) -> None:
    shard = C.Shard(SOURCE)
    client = C.Client(min_interval=MIN_INTERVAL, budget_bytes=budget_mb * 1024 * 1024, timeout=120.0)
    C.log(f"[arxiv] 已有 manifest {len(shard)} 份，目标 {target}")

    per_set: dict[str, int] = {}
    for row in shard.rows.values():
        cat = re.search(r"cat=([\w.\-]+)", row.get("note", ""))
        key = (cat.group(1).split(".")[0] if cat else "?")
        per_set[key] = per_set.get(key, 0) + 1

    per_window = max(1, target // max(1, len(set(w[0] for w in WINDOWS))))

    for set_spec, date_from, date_until in WINDOWS:
        if len(shard) >= target:
            break
        C.log(f"[arxiv] OAI set={set_spec} {date_from}..{date_until}（首字节可能等 90s）")
        taken = 0
        for rec in list_records(client, set_spec, date_from, date_until):
            if len(shard) >= target or taken >= per_window:
                break
            if CC_BY_4 not in rec["license"]:
                continue
            if not re.fullmatch(r"[\w.\-/]{5,20}", rec["id"] or ""):
                continue
            ident = f"{SOURCE}_{C.slug(rec['id'], 20)}"
            if shard.is_done(ident):
                taken += 1
                continue

            pdf_url = PDF_URL.format(id=rec["id"])
            pdf_path = shard.dir / f"{ident}.pdf"
            info, reason = C.fetch_pdf_candidate(client, pdf_url, pdf_path)
            if info is None:
                shard.skip(pdf_url, reason)
                C.log(f"    ✗ {rec['id']} {reason}")
                continue

            # 真值源：e-print LaTeX 源码
            src_path: Path | None = None
            src_url = EPRINT_URL.format(id=rec["id"])
            try:
                raw = client.request(src_url, max_bytes=MAX_EPRINT_BYTES, timeout=120.0)
                suffix = eprint_suffix(raw)
                if suffix:
                    src_path = shard.dir / f"{ident}{suffix}"
                    C.write_atomic(src_path, raw)
                else:
                    shard.skip(src_url, "PDF only 投稿，e-print 里没有 LaTeX 源码（降为 C 档）")
                    C.log(f"    ! {rec['id']} PDF only 投稿，无源码（降为 C 档）")
            except C.BudgetExceeded:
                raise
            except C.FetchError as exc:
                shard.skip(src_url, f"e-print 源码下载失败：{exc}")
                C.log(f"    ! {rec['id']} 源码缺失（降为 C 档）：{exc}")
                src_path = None

            shard.add(build_row(rec, pdf_url, pdf_path, info, src_path))
            taken += 1
            per_set[set_spec] = per_set.get(set_spec, 0) + 1
            C.log(
                f"    ✓ [{len(shard)}/{target}] {rec['id']} {info['pages']}p "
                f"{info['size']//1024}KB {C.pdf_layout(info)} {rec['title'][:44]}"
            )
            shard.save()

    shard.save()
    C.summarize(shard)
    C.log(f"  本轮请求 {client.requests} 次，下载 {client.downloaded/1024/1024:.1f} MB")


def main(argv: list[str] | None = None) -> int:
    C.ensure_utf8_stdio()
    ap = argparse.ArgumentParser(description="采集 arXiv CC-BY 论文 PDF + LaTeX 源码作评测语料")
    ap.add_argument("--target", type=int, default=40, help="入 manifest 的目标份数")
    ap.add_argument("--budget-mb", type=int, default=600, help="本轮下载量上限（MB）")
    args = ap.parse_args(argv)
    return C.run_guarded(lambda: collect(args.target, args.budget_mb))


if __name__ == "__main__":
    raise SystemExit(main())
