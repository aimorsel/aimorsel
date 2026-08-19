"""XFUND 多语扫描表单采集器（bench/PLAN.md §1.2）——**本基准唯一的真实多语扫描件**。

数据源：<https://github.com/doc-analysis/XFUND/releases/tag/v1.0>，每语言一对
`{lang}.val.zip`（扫描图）+ `{lang}.val.json`（人工标注）。取 zh/ja/de/fr/es 各 8 份 = 40 份。

## 为什么用 HTTP Range 而不是整包下载

五个语言的 zip 合计 **283 MB**（zh 69 / ja 32 / de 70 / fr 29 / es 83），而我们每语言只要
**8 张图**（约 1.4 MB/张，合计 ~56 MB）。GitHub 的 release 资产（302 到
`release-assets.githubusercontent.com`）**支持 `Range` 且 urllib 跟随重定向时会带上该头**，
实测回 `206 + Content-Range`。所以这里用 ``HttpRangeFile`` 把远端 zip 当可 seek 的文件喂给
`zipfile`：先读尾部的中央目录，再只取要的那 8 个成员的字节区间，省掉 ~230 MB 下载。

Range 若在某个资产上失效（回 200 而非 206），``HttpRangeFile`` 会抛错，该语言记进跳过清单，
**不会退化成静默把整包拉下来**（那样会炸预算）。

## 真值

做法与 `funsd.py` 完全一致（同一批标注规范）：`documents[].document[]` 每条带
`text` + `box=[x0,y0,x1,y1]`，按「行带 + 行内左起」拼成阅读顺序。
**`headings` / `tables` 两个键都省略**——表单没有标题层级，也没有表格真值，
按 `bench/truth/__init__.py` 的约定写 `[]` 等于惩罚引擎抓到了真实字段。

行带高度按图高自适应（`img.height // 120`）：XFUND 是 2480×3508 的高分辨率扫描件，
坐标空间比 FUNSD 大三倍多，沿用 FUNSD 的 8px 会把同一行切碎。
"""

from __future__ import annotations

import io
import json
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

if __package__ in (None, ""):  # 允许直接 python bench/fetchers/xfund.py
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bench.fetchers.common import (  # noqa: E402
    BOT_UA,
    CORPUS_DIR,
    MAX_BYTES,
    Collector,
    FetchError,
    RateLimiter,
    cache_get,
    log,
    manifest_row,
    sha256_bytes,
    write_atomic,
)
from bench.truth import build_truth  # noqa: E402

SOURCE = "xfund"
BASE = "https://github.com/doc-analysis/XFUND/releases/download/v1.0"
LICENSE = "XFUND 研究许可（不再分发，仅本机评测）"
LANGS = ["zh", "ja", "de", "fr", "es"]
PER_LANG = 8

# 图片后缀 -> manifest 的 format。XFUND 实际是 jpg，留个映射以防个别语言给的是 png。
EXT_FMT = {".jpg": "jpg", ".jpeg": "jpg", ".png": "png", ".tif": "tiff", ".tiff": "tiff"}


# ---------------------------------------------------------------- 远端 zip 的按需读取


class HttpRangeFile(io.RawIOBase):
    """把支持 `Range` 的远端文件包装成可 seek 的类文件对象，供 `zipfile` 按需读取。

    内部维护一个 ``chunk`` 大小的滑动窗口：``read()`` 落在窗口内直接切片，
    落在窗口外才发一次 Range 请求。`zipfile` 的访问模式是「尾部中央目录 → 若干成员」，
    窗口取 2 MB 时每张图约 1-2 次请求。
    """

    def __init__(self, url: str, limiter: RateLimiter, chunk: int = 2 << 20) -> None:
        self.url = url
        self.limiter = limiter
        self.chunk = chunk
        self._pos = 0
        self._buf = b""
        self._buf_start = -1
        self.fetched = 0  # 实际下载字节数，用于预算统计
        self.size = self._probe_size()

    # --- HTTP

    def _range_get(self, start: int, end: int) -> bytes:
        """取 [start, end] 闭区间。必须回 206，否则说明服务端不支持 Range。"""
        end = min(end, (self.size - 1) if self.size else end)
        if end < start:
            return b""
        last_err = ""
        for attempt in range(3):
            self.limiter.wait(self.url)
            req = urllib.request.Request(
                self.url,
                headers={"User-Agent": BOT_UA, "Accept": "*/*", "Range": f"bytes={start}-{end}"},
            )
            try:
                with urllib.request.urlopen(req, timeout=180) as resp:
                    status = getattr(resp, "status", resp.getcode())
                    body = resp.read()
                    if status != 206:
                        raise FetchError(
                            f"{self.url} 不支持 Range（回 HTTP {status}），"
                            "拒绝退化成整包下载以免超预算"
                        )
                    self.fetched += len(body)
                    return body
            except FetchError:
                raise
            except Exception as exc:  # noqa: BLE001
                last_err = f"{type(exc).__name__}: {exc}"
        raise FetchError(f"{self.url} Range {start}-{end} 失败：{last_err}")

    def _probe_size(self) -> int:
        """用 `Range: bytes=0-0` 从 `Content-Range` 尾部拿总长度（比 HEAD 更可靠）。"""
        self.limiter.wait(self.url)
        req = urllib.request.Request(
            self.url, headers={"User-Agent": BOT_UA, "Accept": "*/*", "Range": "bytes=0-0"}
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                status = getattr(resp, "status", resp.getcode())
                cr = resp.headers.get("Content-Range", "")
                resp.read()
        except Exception as exc:  # noqa: BLE001
            raise FetchError(f"{self.url} 探测大小失败：{type(exc).__name__}: {exc}") from exc
        if status != 206 or "/" not in cr:
            raise FetchError(f"{self.url} 不支持 Range（HTTP {status}, Content-Range={cr!r}）")
        return int(cr.rsplit("/", 1)[1])

    # --- 类文件接口（zipfile 只用到这几个）

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            self._pos = offset
        elif whence == io.SEEK_CUR:
            self._pos += offset
        else:  # SEEK_END
            self._pos = self.size + offset
        self._pos = max(0, min(self._pos, self.size))
        return self._pos

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            n = self.size - self._pos
        n = min(n, self.size - self._pos)
        if n <= 0:
            return b""
        out = bytearray()
        while n > 0:
            if self._buf_start < 0 or not (
                self._buf_start <= self._pos < self._buf_start + len(self._buf)
            ):
                start = self._pos
                self._buf = self._range_get(start, start + self.chunk - 1)
                self._buf_start = start
                if not self._buf:
                    break
            off = self._pos - self._buf_start
            take = self._buf[off : off + n]
            out += take
            self._pos += len(take)
            n -= len(take)
        return bytes(out)


# ---------------------------------------------------------------- 真值


def reading_order(entries: list[dict], band: int) -> list[dict]:
    def key(e: dict) -> tuple[int, int]:
        box = e.get("box") or [0, 0, 0, 0]
        return (int(box[1]) // band, int(box[0]))

    return sorted(entries, key=key)


def make_truth(doc: dict, band: int) -> dict:
    entries = [e for e in doc.get("document", []) if isinstance(e, dict)]
    blocks: list[tuple[str, object]] = []
    for e in reading_order(entries, band):
        text = (e.get("text") or "").strip()
        if text:
            blocks.append(("para", text))
    return build_truth(
        blocks,
        with_headings=False,  # 表单无标题层级：省略键 = 不评这项
        with_tables=False,    # 无表格真值
        note={"reading_order": f"band={band}px then x0", "entries": len(entries)},
    )


# ---------------------------------------------------------------- 主流程


def fetch_lang(col: Collector, lang: str) -> None:
    ann_url = f"{BASE}/{lang}.val.json"
    zip_url = f"{BASE}/{lang}.val.zip"

    # 标注 JSON 只有 1-2 MB，整份缓存下来（真值源，续跑不重下）
    try:
        ann_blob = cache_get(col.limiter, ann_url, f"xfund_{lang}.val.json")
    except FetchError as exc:
        col.skip(f"xfund_{lang}", ann_url, f"标注下载失败：{exc}")
        return
    try:
        ann = json.loads(ann_blob.decode("utf-8"))
        docs = [d for d in ann.get("documents", []) if isinstance(d, dict)]
    except Exception as exc:  # noqa: BLE001
        col.skip(f"xfund_{lang}", ann_url, f"标注解析失败：{type(exc).__name__}: {exc}")
        return
    if not docs:
        col.skip(f"xfund_{lang}", ann_url, "标注里没有 documents")
        return

    wanted = docs[:PER_LANG]
    by_fname = {(d.get("img") or {}).get("fname", ""): d for d in wanted}

    # 先看这批要的图是不是都已经落好了——全命中就完全不碰网络（连 zip 尾部都不读）
    pending = []
    for d in wanted:
        fname = (d.get("img") or {}).get("fname", "")
        stem = Path(fname).stem or d.get("id", "")
        rel_img = f"real/{SOURCE}/{stem}{Path(fname).suffix.lower()}"
        rel_truth = f"real/{SOURCE}/{stem}.truth.json"
        hit = col.cached(f"xfund_{stem}", rel_img)
        if hit and col.cached_aux(rel_truth):
            col.reused += 1
            col.add(hit)
        else:
            pending.append(fname)
    if not pending:
        log(f"[{SOURCE}] {lang}：{len(wanted)} 份全部复用，跳过网络访问")
        return

    # 远端 zip 按需读取
    try:
        rf = HttpRangeFile(zip_url, col.limiter)
        zf = zipfile.ZipFile(rf)
    except (FetchError, zipfile.BadZipFile, urllib.error.URLError) as exc:
        col.skip(f"xfund_{lang}", zip_url, f"Range 读取 zip 失败：{exc}")
        return

    entries = {Path(n).name: n for n in zf.namelist() if not Path(n).name.startswith("._")}
    log(f"[{SOURCE}] {lang}：远端 zip {rf.size / 1e6:.0f} MB，{len(entries)} 个成员，取 {len(pending)} 份")

    for fname in pending:
        d = by_fname[fname]
        stem = Path(fname).stem or str(d.get("id", ""))
        suffix = Path(fname).suffix.lower()
        doc_id = f"xfund_{stem}"
        member = entries.get(fname)
        if not member:
            col.skip(doc_id, zip_url, f"zip 里找不到 {fname}")
            continue
        info = zf.getinfo(member)
        if info.file_size > MAX_BYTES:
            col.skip(doc_id, zip_url, f"图片 {info.file_size / 1e6:.1f} MB 超过 8 MB 上限")
            continue
        if col.budget_left() <= 0:
            col.skip(doc_id, zip_url, "已达本轮下载预算上限")
            break

        before = rf.fetched
        try:
            img = zf.read(member)
        except Exception as exc:  # noqa: BLE001 —— Range 层/解压层什么都可能抛
            col.skip(doc_id, zip_url, f"解出 {fname} 失败：{type(exc).__name__}: {exc}")
            continue
        col.note_download(rf.fetched - before)  # 逐份计入，预算判断才不滞后

        meta = d.get("img") or {}
        band = max(4, int(meta.get("height") or 3508) // 120)
        truth = make_truth(d, band)
        if not truth.get("text"):
            col.skip(doc_id, zip_url, "标注里没有任何文本")
            continue

        rel_img = f"real/{SOURCE}/{stem}{suffix}"
        rel_truth = f"real/{SOURCE}/{stem}.truth.json"
        write_atomic(CORPUS_DIR / rel_img, img)
        write_atomic(
            CORPUS_DIR / rel_truth,
            json.dumps(truth, ensure_ascii=False, indent=1).encode("utf-8"),
        )
        col.add(
            manifest_row(
                doc_id=doc_id,
                rel_path=rel_img,
                fmt=EXT_FMT.get(suffix, "png"),
                lang=lang,
                domain="business",
                layout="form",
                source=SOURCE,
                license_=LICENSE,
                truth_type="A",
                truth=rel_truth,
                truth_src=f".fetch_cache/xfund_{lang}.val.json#{d.get('id', '')}",
                url=zip_url,
                sha256=sha256_bytes(img),
                size=len(img),
                pages=1,
                note=(
                    f"真实多语扫描表单；{meta.get('width')}x{meta.get('height')}；"
                    f"标注 {len(d.get('document', []))} 字段；无标题/表格真值（省略键）"
                ),
            )
        )
        log(f"  ✓ {doc_id}（{len(img) / 1024:.0f} KB，{len(truth['paragraphs'])} 段）")

    log(f"[{SOURCE}] {lang}：Range 实取 {rf.fetched / 1e6:.1f} MB（整包 {rf.size / 1e6:.0f} MB）")
    zf.close()


def main() -> int:
    col = Collector(SOURCE, RateLimiter(2.0))
    log(f"[{SOURCE}] 目标 {len(LANGS)} 语言 × {PER_LANG} 份 = {len(LANGS) * PER_LANG} 份真实多语扫描表单")
    for lang in LANGS:
        fetch_lang(col, lang)
    log(col.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
