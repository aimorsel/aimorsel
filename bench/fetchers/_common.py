"""评测语料采集的公共工具。

设计约定（所有 fetcher 共用）：

- **幂等**：manifest 分片里已有的条目，若磁盘文件存在且 sha256 一致 → 直接跳过，不重下。
- **限速**：按主机名限速，默认 ≤ 2 req/s（arXiv 等要求更慢的自己传 ``min_interval``）。
- **重试**：单个 URL 失败重试 3 次（指数退避，尊重 ``Retry-After``），仍失败则记入
  跳过清单 ``corpus/real/<source>/_skipped.tsv`` 并继续跑，绝不中断整批。
- **UA**：联系邮箱取环境变量 ``BENCH_CONTACT_EMAIL``，默认 ``bench@example.com``。
  仓库里不出现真人邮箱。
- **路径**：全部基于 ``__file__`` 解析，不出现本机绝对路径。
- **硬约束**：单份 ≤ 8 MB 且 ≤ 30 页；整轮下载量有上限（``--budget-mb``）。

manifest 分片一行一条 JSON，字段见 ``manifest_row()``。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

# ---------------------------------------------------------------- 路径与常量

BENCH_DIR = Path(__file__).resolve().parent.parent
CORPUS_DIR = BENCH_DIR / "corpus"
REAL_DIR = CORPUS_DIR / "real"
MANIFEST_D = CORPUS_DIR / "manifest.d"

MAX_BYTES = 8 * 1024 * 1024          # 单份体积上限
MAX_PAGES = 30                        # 单份页数上限
SCANNED_CHARS_PER_PAGE = 50           # 低于此值判为 scan-pdf（与 morsel 的阈值一致）
DEFAULT_BUDGET_MB = 2048              # 整轮下载量上限

FORMATS = {"pdf", "scan-pdf", "docx", "xlsx", "pptx", "html", "png", "jpg", "tiff"}
LANGS = {"zh", "en", "es", "de", "fr", "ar", "ja", "ru", "mixed"}
DOMAINS = {"it", "math", "law", "business", "edu", "gov", "med", "news"}


def contact_email() -> str:
    return os.environ.get("BENCH_CONTACT_EMAIL", "bench@example.com").strip() or "bench@example.com"


def user_agent() -> str:
    return f"Mozilla/5.0 (compatible; aimorsel-bench/0.1; +mailto:{contact_email()})"


def log(msg: str) -> None:
    """逐行 flush —— 重定向到文件时块缓冲会把日志全憋住。"""
    print(msg, flush=True)


# ---------------------------------------------------------------- HTTP 客户端


class BudgetExceeded(RuntimeError):
    pass


class FetchError(RuntimeError):
    pass


_SSL_CTX = ssl.create_default_context()
# 不少政府站点证书链不全 / 中间证书缺失，这里只下公开文件，放宽校验但仍走 https。
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


@dataclass
class Client:
    """带限速 / 重试 / 下载量预算的极简 HTTP 客户端（纯 stdlib）。"""

    min_interval: float = 0.5          # 单主机最小请求间隔，0.5s = 2 req/s
    timeout: float = 60.0
    retries: int = 3
    budget_bytes: int = DEFAULT_BUDGET_MB * 1024 * 1024
    downloaded: int = 0
    requests: int = 0
    _last: dict[str, float] = field(default_factory=dict)

    # -- 限速 --------------------------------------------------------------
    def _wait(self, url: str) -> None:
        host = urllib.parse.urlparse(url).netloc.lower()
        last = self._last.get(host)
        if last is not None:
            gap = self.min_interval - (time.monotonic() - last)
            if gap > 0:
                time.sleep(gap)
        self._last[host] = time.monotonic()

    def _spend(self, n: int) -> None:
        self.downloaded += n
        if self.downloaded > self.budget_bytes:
            raise BudgetExceeded(
                f"下载量已达上限 {self.budget_bytes / 1024 / 1024:.0f} MB，停止采集"
            )

    # -- 请求 --------------------------------------------------------------
    def request(
        self,
        url: str,
        *,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        max_bytes: int | None = None,
    ) -> bytes:
        """发一次请求，返回响应体。失败按 retries 重试，最终失败抛 FetchError。"""
        hdrs = {
            "User-Agent": user_agent(),
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        if data is not None:
            hdrs["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        if headers:
            hdrs.update(headers)

        url = normalize_url(url)
        last_err: Exception | None = None
        for attempt in range(1, self.retries + 1):
            self._wait(url)
            self.requests += 1
            try:
                req = urllib.request.Request(url, data=data, headers=hdrs)
                with urllib.request.urlopen(req, timeout=timeout or self.timeout, context=_SSL_CTX) as resp:
                    declared = resp.headers.get("Content-Length")
                    if max_bytes and declared and int(declared) > max_bytes:
                        raise FetchError(f"Content-Length {declared} 超过上限 {max_bytes}")
                    if max_bytes:
                        body = resp.read(max_bytes + 1)
                        if len(body) > max_bytes:
                            raise FetchError(f"响应体超过上限 {max_bytes}")
                    else:
                        body = resp.read()
                self._spend(len(body))
                return body
            except BudgetExceeded:
                raise
            except FetchError as exc:
                # 体积超限属于确定性失败，重试无意义。
                raise exc
            except urllib.error.HTTPError as exc:
                last_err = exc
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                if exc.code in (429, 503) and retry_after:
                    try:
                        delay = min(float(retry_after), 300.0)
                    except ValueError:
                        delay = 30.0
                    log(f"    [{exc.code}] 服务端要求等待 {delay:.0f}s（Retry-After）")
                    time.sleep(delay)
                elif exc.code in (400, 401, 403, 404, 410):
                    break  # 确定性失败，别浪费重试
                else:
                    time.sleep(2.0 * attempt)
            except Exception as exc:                        # noqa: BLE001 —— 网络层什么都可能抛
                last_err = exc
                time.sleep(2.0 * attempt)
        raise FetchError(f"{url} 失败（{self.retries} 次）：{last_err}")

    def get_text(self, url: str, **kw: Any) -> str:
        return decode_html(self.request(url, **kw))

    def get_json(self, url: str, **kw: Any) -> Any:
        return json.loads(self.request(url, **kw).decode("utf-8", "replace"))


def normalize_url(url: str) -> str:
    """把非 ASCII 路径 / 查询串百分号编码。政府站大量中文文件名，不编码 urllib 直接炸。"""
    parts = urllib.parse.urlsplit(url)
    path = urllib.parse.quote(parts.path, safe="/%:@&=+$,~*!'()")
    query = urllib.parse.quote(parts.query, safe="/?:@&=+$,;%~*!'()")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, query, ""))


def decode_html(raw: bytes) -> str:
    """BOM > <meta charset> > utf-8 > gb18030 宽松。中文政府站大量 gb2312。"""
    if raw[:3] == b"\xef\xbb\xbf":
        return raw[3:].decode("utf-8", "replace")
    head = raw[:4096].decode("ascii", "ignore").lower()
    m = re.search(r'charset=["\']?([\w\-]+)', head)
    if m:
        enc = m.group(1)
        if enc in ("gb2312", "gbk"):
            enc = "gb18030"
        try:
            return raw.decode(enc, "replace")
        except LookupError:
            pass
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("gb18030", "replace")


# ---------------------------------------------------------------- 文件与哈希


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def write_atomic(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_bytes(raw)
    tmp.replace(path)


def slug(text: str, maxlen: int = 40) -> str:
    """归一成 id 可用的小写下划线片段。"""
    out = re.sub(r"[^0-9a-zA-Z]+", "_", text.lower()).strip("_")
    return out[:maxlen] or "x"


def url_hash(url: str, n: int = 8) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:n]


# ---------------------------------------------------------------- PDF 探测


_CJK_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿]")


def cjk_ratio(text: str) -> float:
    """汉字占非空白字符的比例。用来把「英文版年报」这类混进来的文件挑出去。"""
    body = re.sub(r"\s", "", text)
    if not body:
        return 0.0
    return len(_CJK_RE.findall(body)) / len(body)


def pdf_info(path: Path) -> dict[str, Any] | None:
    """返回 {pages, chars_per_page, producer, creator, columns}；打不开返回 None。"""
    try:
        import pymupdf                                   # type: ignore
    except ImportError:                                  # pragma: no cover
        try:
            import fitz as pymupdf                       # type: ignore
        except ImportError:
            return None
    try:
        doc = pymupdf.open(path)
    except Exception:                                    # noqa: BLE001
        return None
    try:
        pages = doc.page_count
        if pages <= 0:
            return None
        chars = 0
        sample: list[str] = []
        for idx, page in enumerate(doc):
            try:
                text = page.get_text()
            except Exception:                            # noqa: BLE001
                continue
            chars += len(text)
            if idx < 4:
                sample.append(text)
        meta = doc.metadata or {}
        info = {
            "pages": pages,
            "chars_per_page": chars // pages,
            "cjk_ratio": cjk_ratio("".join(sample)),
            "producer": (meta.get("producer") or "").strip(),
            "creator": (meta.get("creator") or "").strip(),
            "columns": _guess_columns(doc),
        }
        return info
    finally:
        doc.close()


def _guess_columns(doc: Any) -> int:
    """粗判栏数：看正文页文本块左边界是否明显聚成两簇。"""
    lefts: list[float] = []
    width = 0.0
    for idx in range(min(4, doc.page_count)):
        try:
            page = doc[idx]
            width = max(width, float(page.rect.width) or 0.0)
            for block in page.get_text("blocks"):
                x0, y0, x1, y1 = block[:4]
                if (x1 - x0) > 30 and (y1 - y0) > 10:
                    lefts.append(float(x0))
        except Exception:                                # noqa: BLE001
            continue
    if not lefts or width <= 0:
        return 1
    mid = width / 2
    right_side = [x for x in lefts if x > mid]
    left_side = [x for x in lefts if x <= mid]
    if len(right_side) >= 4 and len(right_side) >= 0.25 * len(lefts) and left_side:
        return 2
    return 1


def pdf_format(info: dict[str, Any]) -> str:
    """整份平均每页字符数 < 50 判为扫描/图片化 PDF。"""
    return "scan-pdf" if info["chars_per_page"] < SCANNED_CHARS_PER_PAGE else "pdf"


def pdf_layout(info: dict[str, Any]) -> str:
    return "two-column" if info.get("columns") == 2 else "single-column"


def producer_flag(info: dict[str, Any]) -> str:
    """Quartz / 方正 / Founder 是「兼容码位」缺陷的高发排版链，标出来供优先挑选。"""
    blob = f"{info.get('producer', '')} {info.get('creator', '')}"
    low = blob.lower()
    if "quartz" in low:
        return "quartz"
    if "founder" in low or "apabi" in low or "方正" in blob:
        return "founder"
    return ""


# ---------------------------------------------------------------- 产出写入


def manifest_row(
    *,
    ident: str,
    path: str,
    fmt: str,
    lang: str,
    domain: str,
    layout: str,
    source: str,
    license_: str,
    truth_type: str,
    truth: str = "",
    truth_src: str = "",
    url: str = "",
    sha256: str = "",
    size: int = 0,
    pages: int | None = None,
    note: str = "",
) -> dict[str, Any]:
    assert fmt in FORMATS, f"未知 format: {fmt}"
    assert lang in LANGS, f"未知 lang: {lang}"
    assert domain in DOMAINS, f"未知 domain: {domain}"
    assert truth_type in ("A", "B", "C"), f"未知 truth_type: {truth_type}"
    return {
        "id": ident,
        "path": path,
        "format": fmt,
        "lang": lang,
        "domain": domain,
        "layout": layout,
        "source": source,
        "license": license_,
        "truth_type": truth_type,
        "truth": truth,
        "truth_src": truth_src,
        "url": url,
        "sha256": sha256,
        "size": size,
        "pages": pages,
        "note": note,
    }


class Shard:
    """一个来源对应的 manifest 分片 + 跳过清单，支持续跑。"""

    def __init__(self, source: str) -> None:
        self.source = source
        self.dir = REAL_DIR / source
        self.dir.mkdir(parents=True, exist_ok=True)
        MANIFEST_D.mkdir(parents=True, exist_ok=True)
        self.path = MANIFEST_D / f"{source}.jsonl"
        self.skip_path = self.dir / "_skipped.tsv"
        self.rows: dict[str, dict[str, Any]] = {}
        self.seen_urls: set[str] = set()
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self.rows[row["id"]] = row
                if row.get("url"):
                    self.seen_urls.add(row["url"])
        self.skipped: list[tuple[str, str]] = []
        if self.skip_path.exists():
            for line in self.skip_path.read_text(encoding="utf-8").splitlines():
                if "\t" in line:
                    a, b = line.split("\t", 1)
                    self.skipped.append((a, b))

    # -- 幂等判断 ----------------------------------------------------------
    def is_done(self, ident: str) -> bool:
        """已有条目 + 磁盘文件存在 + sha256 一致 → 视为完成。"""
        row = self.rows.get(ident)
        if not row:
            return False
        target = CORPUS_DIR / row["path"]
        if not target.exists():
            return False
        if row.get("sha256") and sha256_file(target) != row["sha256"]:
            return False
        if row.get("truth_src"):
            if not (CORPUS_DIR / row["truth_src"]).exists():
                return False
        return True

    def add(self, row: dict[str, Any]) -> None:
        self.rows[row["id"]] = row
        if row.get("url"):
            self.seen_urls.add(row["url"])

    def skip(self, key: str, reason: str) -> None:
        self.skipped.append((key, reason))

    def save(self) -> None:
        lines = [json.dumps(self.rows[k], ensure_ascii=False) for k in sorted(self.rows)]
        tmp = self.path.with_suffix(".jsonl.part")
        tmp.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        tmp.replace(self.path)
        if self.skipped:
            seen: set[tuple[str, str]] = set()
            out: list[str] = []
            for key, reason in self.skipped:
                if (key, reason) in seen:
                    continue
                seen.add((key, reason))
                out.append(f"{key}\t{reason}")
            self.skip_path.write_text("\n".join(out) + "\n", encoding="utf-8")

    def __len__(self) -> int:
        return len(self.rows)


def rel_to_corpus(path: Path) -> str:
    return path.resolve().relative_to(CORPUS_DIR.resolve()).as_posix()


# ---------------------------------------------------------------- 下载 + 校验


def fetch_pdf_candidate(
    client: Client,
    url: str,
    dest: Path,
    *,
    max_bytes: int = MAX_BYTES,
    max_pages: int = MAX_PAGES,
) -> tuple[dict[str, Any] | None, str]:
    """下载一个 PDF 并做硬约束校验。

    返回 ``(info, reason)``：info 为 None 时 reason 是中文跳过原因；
    成功时文件已落到 dest，reason 为 ""。
    """
    try:
        raw = client.request(url, max_bytes=max_bytes)
    except BudgetExceeded:
        raise
    except FetchError as exc:
        return None, f"下载失败：{exc}"
    if not raw[:5].startswith(b"%PDF"):
        return None, "不是 PDF（响应体缺 %PDF 头）"
    if len(raw) > max_bytes:
        return None, f"超过体积上限（{len(raw)} > {max_bytes}）"
    write_atomic(dest, raw)
    info = pdf_info(dest)
    if info is None:
        dest.unlink(missing_ok=True)
        return None, "PDF 打不开（pymupdf 解析失败）"
    if info["pages"] > max_pages:
        dest.unlink(missing_ok=True)
        return None, f"超过页数上限（{info['pages']} > {max_pages}）"
    info["size"] = len(raw)
    info["sha256"] = sha256_bytes(raw)
    return info, ""


def summarize(shard: Shard) -> None:
    """按 format / lang / domain 打分布，收尾用。"""
    from collections import Counter

    rows = list(shard.rows.values())
    log(f"\n=== {shard.source}：入 manifest {len(rows)} 份，跳过 {len(shard.skipped)} 项")
    for key in ("format", "lang", "domain", "layout"):
        counts = Counter(r.get(key) for r in rows)
        log(f"  {key:7s} " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])))
    flags = Counter(
        re.search(r"producer=([^\s;]+)", r.get("note", "")).group(1)
        if re.search(r"producer=([^\s;]+)", r.get("note", ""))
        else "-"
        for r in rows
    )
    if len(flags) > 1:
        log("  producer " + "  ".join(f"{k}={v}" for k, v in flags.most_common(8)))
    log(f"  分片：{shard.path.relative_to(BENCH_DIR)}")


def run_guarded(fn: Callable[[], None]) -> int:
    """统一的顶层收尾：预算超限 / Ctrl-C 都算「正常收尾」，已下的照样入 manifest。"""
    try:
        fn()
    except BudgetExceeded as exc:
        log(f"\n[停止] {exc}")
    except KeyboardInterrupt:
        log("\n[中断] 收到 Ctrl-C，已保存进度")
    return 0


def iter_unique(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(items))


def ensure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", line_buffering=True)  # type: ignore[attr-defined]
        except Exception:                                              # noqa: BLE001
            pass
