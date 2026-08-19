"""按 manifest 分片批量生成 A 档真值 JSON（bench/PLAN.md §2 的 B 阶段驱动）。

    python -m bench.make_truth [--shard wikipedia] [--force] [--limit N] [--dry-run]

扫描 ``bench/corpus/manifest.d/*.jsonl`` 的每一行（**只读，绝不改分片文件**）：
``truth_src`` 非空、且 ``truth`` 目标文件不存在（或给了 ``--force``）时，
按 ``truth_src`` 后缀分派到解析器，把真值写到 ``truth`` 指定的路径：

| truth_src | 解析器 |
|---|---|
| `.html` / `.htm` / `.xhtml`（含 `.gz`） | `bench.truth.from_html` |
| `.tex` / `.tar.gz` / `.tgz` / `.tar` / `.tex.gz` | `bench.truth.from_latex` |
| `.xml` / `.xml.gz` / `.xml.zip` / `.zip` | `bench.truth.from_xml` |
| 其他 | 嗅探文件头（`<?xml` / `<html` / `\\documentclass`），认不出记失败 |

单份失败只记录不中断；结尾打印「成功 / 跳过 / 失败 + 失败原因计数」。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from collections import Counter
from pathlib import Path

BENCH = Path(__file__).resolve().parent
sys.path.insert(0, str(BENCH.parent))

from bench.truth import from_html, from_latex, from_xml  # noqa: E402

HTML_SUFFIXES = (".html", ".htm", ".xhtml", ".html.gz", ".htm.gz", ".xhtml.gz")
LATEX_SUFFIXES = (".tex", ".tex.gz", ".tar.gz", ".tgz", ".tar", ".ltx", ".latex")
XML_SUFFIXES = (".xml", ".xml.gz", ".xml.zip", ".zip")


def echo(msg: str) -> None:
    print(msg, flush=True)


def _lower_name(p: Path) -> str:
    return p.name.lower()


def pick_parser(src: Path):
    """返回 (名字, parse 函数)；认不出来时抛 ValueError。"""
    name = _lower_name(src)
    if name.endswith(HTML_SUFFIXES):
        return "html", from_html.parse
    if name.endswith(LATEX_SUFFIXES):
        return "latex", from_latex.parse
    if name.endswith(XML_SUFFIXES):
        return "xml", from_xml.parse
    if src.is_dir():
        return "latex", from_latex.parse
    head = b""
    try:
        with src.open("rb") as fh:
            head = fh.read(4096)
    except OSError as exc:
        raise ValueError(f"读不到 {src}: {exc}") from exc
    if head[:2] == b"PK":
        return "xml", from_xml.parse
    if head[:2] == b"\x1f\x8b":  # gz：解开头部再嗅
        import gzip

        try:
            head = gzip.decompress(head + b"")[:4096]
        except Exception:
            return "latex", from_latex.parse  # arXiv 单文件 e-print 多是 tar.gz
    low = head.lstrip()[:200].lower()
    if low.startswith(b"<?xml") or low.startswith(b"<!doctype dokumente") or b"<law" in low:
        return "xml", from_xml.parse
    if low.startswith((b"<!doctype html", b"<html")) or b"<html" in low:
        return "html", from_html.parse
    if b"\\documentclass" in head or b"\\begin{document}" in head:
        return "latex", from_latex.parse
    raise ValueError(f"认不出真值源类型：{src.name}")


def resolve(rel: str, roots: list[Path]) -> Path:
    """manifest 里的相对路径可能相对 corpus/ 或分片目录，逐个候选试。"""
    p = Path(rel)
    if p.is_absolute():
        return p
    for root in roots:
        cand = root / p
        if cand.exists():
            return cand
    return roots[0] / p


def iter_shards(manifest_dir: Path, shard: str) -> list[Path]:
    if not manifest_dir.is_dir():
        return []
    files = sorted(manifest_dir.glob("*.jsonl"))
    if shard:
        want = {shard, shard + ".jsonl"}
        files = [f for f in files if f.name in want or f.stem == shard]
    return files


def read_rows(path: Path) -> list[dict]:
    rows = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            echo(f"  ! {path.name}:{i} JSON 解析失败，跳过：{exc}")
    return rows


def build_one(row: dict, corpus_dir: Path, shard_dir: Path, force: bool, dry: bool) -> str:
    """返回 "ok" / "skip" / 抛异常。"""
    src_rel = (row.get("truth_src") or "").strip()
    out_rel = (row.get("truth") or "").strip()
    if not src_rel:
        return "skip"
    if not out_rel:
        raise ValueError("行里有 truth_src 但没有 truth 目标路径")
    roots = [corpus_dir, shard_dir, BENCH, Path.cwd()]
    src = resolve(src_rel, roots)
    out = resolve(out_rel, [corpus_dir, shard_dir])
    if out.exists() and not force:
        return "skip"
    if not src.exists():
        raise FileNotFoundError(f"真值源不存在：{src_rel}")
    _name, fn = pick_parser(src)
    truth = fn(src)
    if not truth.get("text"):
        raise ValueError("解析出来是空文本")
    if dry:
        return "ok"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(truth, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(out)
    return "ok"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="按 manifest 分片生成 A 档真值 JSON")
    ap.add_argument("--manifest-dir", default=str(BENCH / "corpus" / "manifest.d"),
                    help="分片目录（默认 bench/corpus/manifest.d）")
    ap.add_argument("--shard", default="", help="只跑某个分片（文件名或不带后缀的名字）")
    ap.add_argument("--force", action="store_true", help="已存在的真值也重新生成")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 行（估时用）")
    ap.add_argument("--dry-run", action="store_true", help="解析但不写文件")
    a = ap.parse_args(argv)

    manifest_dir = Path(a.manifest_dir)
    corpus_dir = manifest_dir.parent if manifest_dir.name == "manifest.d" else manifest_dir
    shards = iter_shards(manifest_dir, a.shard)
    if not shards:
        echo(f"没有找到分片文件：{manifest_dir}/*.jsonl"
             + (f"（--shard {a.shard}）" if a.shard else ""))
        echo("语料采集还没写出分片就先等等——本脚本只读 manifest.d，不会自己造行。")
        return 1

    ok = skip = fail = 0
    reasons: Counter[str] = Counter()
    t0 = time.perf_counter()
    n_done = 0
    for shard in shards:
        rows = read_rows(shard)
        echo(f"== {shard.name}：{len(rows)} 行")
        for row in rows:
            if a.limit and n_done >= a.limit:
                break
            doc_id = row.get("id") or row.get("path") or "?"
            if not (row.get("truth_src") or "").strip():
                skip += 1
                continue
            n_done += 1
            t1 = time.perf_counter()
            try:
                res = build_one(row, corpus_dir, shard.parent, a.force, a.dry_run)
            except Exception as exc:  # 单份失败不中断
                fail += 1
                reasons[f"{type(exc).__name__}: {str(exc).splitlines()[0][:120]}"] += 1
                echo(f"  ✗ {doc_id}：{type(exc).__name__}: {str(exc).splitlines()[0][:200]}")
                if __debug__ and not isinstance(exc, (FileNotFoundError, ValueError)):
                    echo("    " + traceback.format_exc(limit=3).replace("\n", "\n    ").strip())
                continue
            if res == "skip":
                skip += 1
                continue
            ok += 1
            echo(f"  ✓ {doc_id}（{time.perf_counter() - t1:.2f}s）")
        if a.limit and n_done >= a.limit:
            break

    echo(f"\n完成：成功 {ok} / 跳过 {skip} / 失败 {fail}，共 {time.perf_counter() - t0:.1f}s")
    if reasons:
        echo("失败原因计数：")
        for reason, cnt in reasons.most_common():
            echo(f"  {cnt:>4}  {reason}")
    return 0 if fail == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
