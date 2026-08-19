"""评测语料采集脚本的公共工具（纯标准库 + 可选 pymupdf）。

设计约束（三个 fetcher 共用，改这里要同时想到三处）：

- **幂等可续跑**：产物文件已存在、且 sha256 与上一轮 manifest 分片里记的一致，就跳过下载；
  manifest 分片每处理完一份就整体重写一次，中途被杀不丢已完成的部分。
- **限速**：按域名节流，默认 ≤ 2 req/s（``RateLimiter``）。
- **重试**：单个 URL 失败重试 3 次（指数退避）；HTTP 202（反爬/异步生成）单独按更长间隔重试。
  重试用尽记入跳过清单 ``<source>.skipped.jsonl``，**不中断整批**。
- **联系邮箱**：User-Agent 里的邮箱取环境变量 ``BENCH_CONTACT_EMAIL``，默认 ``bench@example.com``。
  仓库将公开，**脚本里不得出现任何真人邮箱或本机绝对路径**——所有路径基于 ``__file__`` 解析。
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

# ---------------------------------------------------------------- 路径（全部基于 __file__）

FETCHERS_DIR = Path(__file__).resolve().parent
BENCH_DIR = FETCHERS_DIR.parent
CORPUS_DIR = BENCH_DIR / "corpus"
REAL_DIR = CORPUS_DIR / "real"
MANIFEST_D = CORPUS_DIR / "manifest.d"
CACHE_DIR = BENCH_DIR / "corpus" / ".fetch_cache"

# ---------------------------------------------------------------- 常量

CONTACT_EMAIL = os.environ.get("BENCH_CONTACT_EMAIL", "bench@example.com")
BOT_UA = f"AImorselBench/0.1 (+{CONTACT_EMAIL})"
# 少数站点（EUR-Lex/publications.europa.eu）对非浏览器 UA 更容易触发挑战，这些地方用浏览器 UA。
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

MAX_BYTES = 8 * 1024 * 1024          # 单份 ≤ 8 MB
MAX_PAGES = 30                       # 单份 ≤ 30 页
BUDGET_BYTES = 1024 * 1024 * 1024    # 本轮采集总量上限 1 GB


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------- 限速

class RateLimiter:
    """按域名节流：同一域名两次请求间隔至少 1/rate 秒。"""

    def __init__(self, rate_per_sec: float = 2.0) -> None:
        self.min_gap = 1.0 / rate_per_sec
        self._last: dict[str, float] = {}

    def wait(self, url: str) -> None:
        host = urllib.parse.urlsplit(url).netloc.lower()
        now = time.monotonic()
        last = self._last.get(host)
        if last is not None:
            gap = self.min_gap - (now - last)
            if gap > 0:
                time.sleep(gap)
        self._last[host] = time.monotonic()


# ---------------------------------------------------------------- HTTP

class FetchError(RuntimeError):
    pass


@dataclass
class Response:
    status: int
    content_type: str
    body: bytes
    url: str


def http_get(
    url: str,
    limiter: RateLimiter,
    headers: dict[str, str] | None = None,
    retries: int = 3,
    timeout: int = 120,
    retry_202: int = 5,
    retry_202_sleep: float = 6.0,
) -> Response:
    """GET 一个 URL。

    失败重试 ``retries`` 次（指数退避 + 抖动）；HTTP 202 另按 ``retry_202`` 次 /
    ``retry_202_sleep`` 秒重试（EUR-Lex 的 legal-content 路径会先回 202 再生成内容）。
    全部用尽抛 ``FetchError``，由调用方记入跳过清单。
    """
    hdrs = {"User-Agent": BOT_UA, "Accept": "*/*"}
    if headers:
        hdrs.update(headers)

    last_err = ""
    attempts_202 = 0
    attempt = 0
    while attempt < retries:
        limiter.wait(url)
        req = urllib.request.Request(url, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = getattr(resp, "status", resp.getcode())
                body = resp.read()
                if status == 202:
                    attempts_202 += 1
                    last_err = "HTTP 202（内容尚未生成 / 反爬挑战）"
                    if attempts_202 >= retry_202:
                        break
                    time.sleep(retry_202_sleep)
                    continue  # 202 不消耗普通重试额度
                return Response(status, resp.headers.get("Content-Type", ""), body, resp.geturl())
        except urllib.error.HTTPError as exc:
            last_err = f"HTTP {exc.code}"
            if exc.code in (400, 401, 403, 404, 406, 410):
                break  # 确定性失败，重试无意义
        except Exception as exc:  # noqa: BLE001 —— 网络层什么都可能抛
            last_err = f"{type(exc).__name__}: {exc}"
        attempt += 1
        if attempt < retries:
            time.sleep(min(2 ** attempt, 8) + random.random())
    raise FetchError(f"{url} -> {last_err}")


# ---------------------------------------------------------------- 文件

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(path)


def pdf_page_count(data: bytes) -> int | None:
    """用 pymupdf 数页；没装或打不开返回 None（调用方据此判断是否放行）。"""
    try:
        import pymupdf  # type: ignore
    except ImportError:  # pragma: no cover
        try:
            import fitz as pymupdf  # type: ignore
        except ImportError:
            return None
    try:
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            return doc.page_count
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- manifest 分片 / 跳过清单

@dataclass
class Collector:
    """收集 manifest 行 + 跳过项，负责幂等判断与落盘。"""

    source: str
    limiter: RateLimiter = field(default_factory=lambda: RateLimiter(2.0))
    rows: dict[str, dict[str, Any]] = field(default_factory=dict)
    skipped: list[dict[str, str]] = field(default_factory=list)
    downloaded_bytes: int = 0
    reused: int = 0
    _prev: dict[str, dict[str, Any]] = field(default_factory=dict)

    # --- 生命周期

    def __post_init__(self) -> None:
        MANIFEST_D.mkdir(parents=True, exist_ok=True)
        (REAL_DIR / self.source).mkdir(parents=True, exist_ok=True)
        prev_path = self.manifest_path
        if prev_path.exists():
            for line in prev_path.read_text("utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._prev[row.get("id", "")] = row

    @property
    def manifest_path(self) -> Path:
        return MANIFEST_D / f"{self.source}.jsonl"

    @property
    def skipped_path(self) -> Path:
        return MANIFEST_D / f"{self.source}.skipped.jsonl"

    @property
    def out_dir(self) -> Path:
        return REAL_DIR / self.source

    # --- 幂等

    def cached(self, doc_id: str, rel_path: str) -> dict[str, Any] | None:
        """上一轮已下好且 sha256 对得上 → 返回旧行，调用方直接复用。"""
        prev = self._prev.get(doc_id)
        if not prev or prev.get("path") != rel_path:
            return None
        target = CORPUS_DIR / rel_path
        if not target.exists():
            return None
        recorded = prev.get("sha256") or ""
        if not recorded or sha256_file(target) != recorded:
            return None
        return dict(prev)

    def cached_aux(self, rel_path: str) -> bool:
        """真值源等附属文件：存在且非空即认为已下好（sha256 不入 manifest，无从校验）。"""
        p = CORPUS_DIR / rel_path
        return p.exists() and p.stat().st_size > 0

    # --- 预算

    def budget_left(self) -> int:
        return BUDGET_BYTES - self.downloaded_bytes

    def note_download(self, nbytes: int) -> None:
        self.downloaded_bytes += nbytes

    # --- 记录

    def add(self, row: dict[str, Any]) -> None:
        self.rows[row["id"]] = row
        self.flush()

    def skip(self, doc_id: str, url: str, reason: str) -> None:
        log(f"  跳过 {doc_id}: {reason}")
        self.skipped.append({"id": doc_id, "url": url, "reason": reason})
        self.flush()

    def flush(self) -> None:
        lines = [json.dumps(self.rows[k], ensure_ascii=False) for k in sorted(self.rows)]
        tmp = self.manifest_path.with_suffix(".jsonl.part")
        tmp.write_text("\n".join(lines) + ("\n" if lines else ""), "utf-8")
        tmp.replace(self.manifest_path)
        if self.skipped:
            slines = [json.dumps(s, ensure_ascii=False) for s in self.skipped]
            stmp = self.skipped_path.with_suffix(".jsonl.part")
            stmp.write_text("\n".join(slines) + "\n", "utf-8")
            stmp.replace(self.skipped_path)

    def summary(self) -> str:
        fmt: dict[str, int] = {}
        lang: dict[str, int] = {}
        for row in self.rows.values():
            fmt[row["format"]] = fmt.get(row["format"], 0) + 1
            lang[row["lang"]] = lang.get(row["lang"], 0) + 1
        return (
            f"[{self.source}] 入 manifest {len(self.rows)} 份"
            f"（复用 {self.reused}，本轮下载 {self.downloaded_bytes / 1e6:.1f} MB）"
            f"，跳过 {len(self.skipped)} 项；format={fmt} lang={lang}"
        )


def manifest_row(
    *,
    doc_id: str,
    rel_path: str,
    fmt: str,
    lang: str,
    domain: str,
    layout: str,
    source: str,
    license_: str,
    truth_type: str,
    truth: str,
    truth_src: str,
    url: str,
    sha256: str,
    size: int,
    pages: int | None,
    note: str = "",
) -> dict[str, Any]:
    """字段顺序固定，方便 diff。"""
    return {
        "id": doc_id,
        "path": rel_path,
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


def cache_get(limiter: RateLimiter, url: str, name: str, **kwargs: Any) -> bytes:
    """把索引类的大文件（法令一览、gii-toc）缓存到 corpus/.fetch_cache，续跑不重下。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / name
    if path.exists() and path.stat().st_size > 0:
        return path.read_bytes()
    resp = http_get(url, limiter, **kwargs)
    write_atomic(path, resp.body)
    return resp.body
