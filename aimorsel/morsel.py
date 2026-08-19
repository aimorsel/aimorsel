#!/usr/bin/env python3
"""
AImorsel（文粒）—— 文档 → Markdown / JSON 提取工具（命令行版 + 核心转换逻辑）

基于 opendataloader-pdf (https://github.com/opendataloader-project/opendataloader-pdf)

用法:
    交互模式:  morsel            （源码运行: python -m aimorsel）
    直接转换:  morsel a.pdf b.pdf
               morsel raw/ -f markdown -o output
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import csv
import hashlib
import io
import json
import math
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib  # Python 3.11+；旧版本没有则忽略 config.toml
except ModuleNotFoundError:
    tomllib = None

try:
    import opendataloader_pdf
except ImportError:
    sys.exit("缺少依赖，请先运行:  pip install opendataloader-pdf\n（另需 Java 11+）")

try:
    import pdfplumber  # 兜底网 + 快速密度探测；缺失时自动退回 JVM 路径，功能不受影响
except ImportError:
    pdfplumber = None

try:
    import pikepdf  # 结构损坏（xref 坏 / 截断）PDF 的修复层（qpdf，MPL-2.0）；缺失时跳过修复
except ImportError:
    pikepdf = None

from . import format_adapters  # 多格式输入路由（docx/xlsx/pptx/HTML/图片），项目内模块
from . import i18n
from . import rtl_text  # RTL 视觉序 → 逻辑序还原（bench issue #0），项目内模块
from .i18n import tr

def _project_dir() -> Path:
    """raw/ output/ config.toml 的落脚点：
    - PyInstaller 打包版：可执行文件旁边（__file__ 在 _internal 里）
    - 源码仓库运行（含 `pip install -e .`）：仓库根目录（包目录的上一级，有 pyproject.toml）
    - pip 装进 site-packages：当前工作目录——绝不能写进 site-packages
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    root = Path(__file__).resolve().parent.parent
    if (root / "pyproject.toml").is_file() and (root / "aimorsel").is_dir():
        return root
    return Path.cwd()


PROJECT_DIR = _project_dir()
DEFAULT_INPUT_DIR = PROJECT_DIR / "raw"
DEFAULT_OUTPUT_DIR = PROJECT_DIR / "output"
CONFIG_PATH = PROJECT_DIR / "config.toml"

# 预设的格式组合：编号 -> (显示名, 传给 opendataloader 的格式列表)。显示名过 tr()。
FORMAT_PRESETS = {
    "1": ("Markdown + JSON（默认）", ["markdown", "json"]),
    "2": ("仅 Markdown", ["markdown"]),
    "3": ("仅 JSON", ["json"]),
    "4": ("全部（Markdown + JSON + HTML + 纯文本）", ["markdown", "json", "html", "text"]),
}


# 开启分页标记时插入的分隔符模板（%page-number% 由底层替换成页码）。
# 做成函数：模板文字跟随界面语言（注意——它计入转换签名，换语言会触发重转，合理）。
def markdown_page_separator() -> str:
    return "\n\n---\n\n" + tr("**— 第 %page-number% 页 —**") + "\n\n"


def text_page_separator() -> str:
    return "\n\n===== " + tr("第 %page-number% 页") + " =====\n\n"

# OCR（hybrid 后端）相关默认值
DEFAULT_HYBRID_URL = "http://127.0.0.1:5002"  # 别写 localhost：配了系统代理的机器上 urllib 会把回环地址也送去代理（bench #4）
DEFAULT_HYBRID_BACKEND = "docling-fast"
# 每页平均字符数低于此阈值，判定为疑似扫描件/图片型 PDF（正常文档每页通常几百上千字符）
SCANNED_CHARS_PER_PAGE = 50

# RAG 分块的默认目标块大小（估算 token 数）
DEFAULT_CHUNK_TOKENS = 400

# 多文档合并的输出文件名，写在输出目录下
MERGED_NAME = "merged.md"


@dataclass
class ConvertOptions:
    """一次转换用到的全部可调参数。CLI、交互模式、GUI 共用这一份。"""

    password: str | None = None
    image_output: str | None = None       # None=底层默认(external) / off / embedded / external
    pages: str | None = None              # 如 "1,3,5-7"，None 表示全部页
    page_markers: bool = False            # 是否在 Markdown / 文本里插入分页标记
    table_method: str | None = None       # None=默认(border) / cluster(无边框表格更好)
    sanitize: bool = False                # 脱敏：邮箱/电话/身份证等替换成占位符
    threads: int | None = None            # 每页并行的线程数，None=串行(稳定)
    include_header_footer: bool = False   # 是否保留页眉页脚
    ocr_mode: str = "auto"                # off / auto(默认，检测扫描件才用) / force(全部用)
    hybrid_url: str = DEFAULT_HYBRID_URL  # OCR 服务地址
    hybrid_backend: str = DEFAULT_HYBRID_BACKEND  # OCR 后端类型
    rag_chunks: bool = False              # 转换后是否输出 RAG 分块（<名>.chunks.jsonl）
    chunk_tokens: int = DEFAULT_CHUNK_TOKENS  # 每块的目标 token 数（估算值）
    export_tables: bool = False           # 转换后是否把所有表格导出为 CSV（<名>_tables/）
    qa: bool = False                      # 质量自检：标注版 PDF + 逐页统计（<名>.qa.csv）
    keep_all_content: bool = False        # 关闭底层内容安全过滤（隐藏/页外/微小文字也保留）
    deskew: bool = True                   # 图片输入：进引擎前把轻微倾斜的页面转正（bench #8）
    tidy: bool = True                     # 清理结构噪声：碎片标题降级、编号段落还原成段落（bench #9/#10）

    def to_convert_kwargs(self, formats: list[str], use_ocr: bool = False) -> dict:
        """把选项翻译成 opendataloader_pdf.convert() 的关键字参数。

        use_ocr 由运行时决定（auto 模式检测后 / force 模式），为 True 时注入 hybrid 参数。
        """
        kwargs: dict = {}
        if self.password:
            kwargs["password"] = self.password
        if self.image_output:
            kwargs["image_output"] = self.image_output
        if self.pages:
            kwargs["pages"] = self.pages
        if self.page_markers:
            if "markdown" in formats:
                kwargs["markdown_page_separator"] = markdown_page_separator()
            if "text" in formats:
                kwargs["text_page_separator"] = text_page_separator()
        if self.table_method:
            kwargs["table_method"] = self.table_method
        if self.sanitize:
            kwargs["sanitize"] = True
        if self.threads and self.threads > 1:
            kwargs["threads"] = str(self.threads)
        if self.include_header_footer:
            kwargs["include_header_footer"] = True
        if self.keep_all_content:
            kwargs["content_safety_off"] = "all"
        if use_ocr:
            kwargs["hybrid"] = self.hybrid_backend
            # 我们的 decide_ocr 已做过文件级判断，这里必须 full：底层默认的 auto 分诊
            # 会把纯图片页也路由给 Java（实测），导致 OCR 后端根本收不到请求
            kwargs["hybrid_mode"] = "full"
            kwargs["hybrid_url"] = self.hybrid_url
            kwargs["hybrid_fallback"] = True  # OCR 出错时退回 Java 引擎，不让整批失败
        return kwargs


@dataclass
class ConvertResult:
    """单个 PDF 的转换结果。convert_one 返回它，日志、汇总、CSV 报告都从这取数。"""

    pdf: Path
    ok: bool = False
    skipped: bool = False                 # 断点续传跳过（此前已完成且源文件未变化）
    produced: list[Path] = field(default_factory=list)
    error: str = ""
    note: str = ""                        # 附加说明（OCR 决策等）
    duration: float = 0.0                 # 转换耗时（秒）
    pages: int | None = None              # 页数（输出含 json 时才能拿到，否则 None）
    used_ocr: bool = False
    degraded: bool = False                # 兜底/退化产出：pdfplumber 纯文本，或图片未经 OCR 的无文字版面
    needs_ocr: bool = False               # 图片输入未走 OCR、产物无文字：OCR 服务上线后应自动重转（bench #5）


@dataclass
class BatchSummary:
    """一整批转换的汇总，execute_batch 返回它。"""

    total: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    degraded: int = 0                     # 成功里有多少是降级/空产出（含在 succeeded 里）
    elapsed: float = 0.0
    results: list[ConvertResult] = field(default_factory=list)
    report_path: Path | None = None
    merged_path: Path | None = None


# 全部支持的输入格式：PDF 走 Java 引擎，办公文档/HTML 走适配器，图片走 OCR 通道
INPUT_EXTENSIONS = (
    {".pdf"} | format_adapters.ADAPTER_EXTENSIONS | format_adapters.IMAGE_EXTENSIONS
)


def is_supported_input(path: Path) -> bool:
    """单个文件是否是支持的输入：后缀在列，且不是隐藏文件/Office 临时锁文件（~$xxx.docx）。

    所有入口（CLI/GUI/Web/MCP）的单文件判断都走这里，别再各自查后缀。
    """
    return (
        path.suffix.lower() in INPUT_EXTENSIONS
        and not path.name.startswith((".", "~$"))
    )


def find_inputs(root: Path) -> list[Path]:
    """递归查找目录下所有支持的输入文件，按路径排序。"""
    return sorted(p for p in root.rglob("*") if p.is_file() and is_supported_input(p))


find_pdfs = find_inputs  # 旧名兼容（GUI 等处仍按此名 import）


def ensure_utf8_stdio() -> None:
    """Windows 的 stdout/stderr 重定向时默认用本地编码（GBK 等），打 ✓/中文会
    UnicodeEncodeError。各入口 main() 开头调用一次；errors=replace 兜底不崩。"""
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


def parse_dropped_path(raw: str) -> Path | None:
    """解析用户拖拽或粘贴进终端的路径（可能带引号或反斜杠转义）。

    Windows 上不能用 POSIX 模式的 shlex——反斜杠路径分隔符会被当成转义符吃掉，
    改为只剥外层引号。
    """
    raw = raw.strip()
    if not raw:
        return None
    if sys.platform == "win32":
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
            raw = raw[1:-1]
        return Path(raw).expanduser()
    try:
        parts = shlex.split(raw)
    except ValueError:
        parts = [raw]
    return Path(parts[0]).expanduser() if parts else None


def parse_selection(answer: str, total: int) -> list[int]:
    """把 "1,3-5" 这类输入解析成 0-based 索引列表；"all" 表示全选。"""
    if answer.lower() in ("all", "a", "*"):
        return list(range(total))

    picked: list[int] = []
    for chunk in answer.replace("，", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            start, _, end = chunk.partition("-")
            try:
                lo, hi = int(start), int(end)
            except ValueError:
                raise ValueError(tr("无法识别的范围: {chunk}", chunk=chunk))
            if lo > hi:
                lo, hi = hi, lo
            candidates = range(lo, hi + 1)
        else:
            try:
                candidates = [int(chunk)]
            except ValueError:
                raise ValueError(tr("无法识别的编号: {chunk}", chunk=chunk))
        for n in candidates:
            if not 1 <= n <= total:
                raise ValueError(tr("编号 {n} 超出范围 (1-{total})", n=n, total=total))
            if n - 1 not in picked:
                picked.append(n - 1)
    return picked


def human_size(num_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024:
            return f"{num_bytes:.0f}{unit}" if unit == "B" else f"{num_bytes:.1f}{unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f}TB"


def choose_pdfs() -> list[Path]:
    """交互式选择待转换的文件（PDF/docx/xlsx/pptx/HTML/图片）。"""
    pdfs = find_inputs(DEFAULT_INPUT_DIR) if DEFAULT_INPUT_DIR.is_dir() else []

    if pdfs:
        print(tr("\n在 {dir} 找到 {n} 个可转换文件：\n", dir=DEFAULT_INPUT_DIR, n=len(pdfs)))
        for i, p in enumerate(pdfs, 1):
            rel = p.relative_to(DEFAULT_INPUT_DIR)
            print(f"  [{i:>2}] {rel}  ({human_size(p.stat().st_size)})")
        print(tr("\n请输入编号选择（如 1 / 1,3 / 1-3 / all），"))
        print(tr("或直接把文件、文件夹拖进来后回车："))
    else:
        print(tr("\n{dir} 里没有找到可转换的文件（支持 PDF/docx/xlsx/pptx/HTML/图片）。", dir=DEFAULT_INPUT_DIR))
        print(tr("请把文件或文件夹拖进终端后回车（也可直接粘贴路径）："))

    while True:
        answer = input("> ").strip()
        if not answer:
            print(tr("没有输入，请重试（Ctrl+C 退出）。"))
            continue

        # 优先按编号解析；解析不了再当成路径处理
        if pdfs and (answer.lower() in ("all", "a", "*") or answer[0].isdigit()):
            try:
                return [pdfs[i] for i in parse_selection(answer, len(pdfs))]
            except ValueError as err:
                print(tr("输入有误：{err}，请重试。", err=err))
                continue

        path = parse_dropped_path(answer)
        if path is None or not path.exists():
            print(tr("路径不存在：{path}，请重试。", path=path))
            continue
        if path.is_dir():
            found = find_inputs(path)
            if not found:
                print(tr("{path} 里没有可转换的文件，请重试。", path=path))
                continue
            print(tr("该文件夹下找到 {n} 个可转换文件，将全部转换。", n=len(found)))
            return found
        if not is_supported_input(path):
            print(tr("{name} 不是支持的格式（PDF/docx/xlsx/pptx/HTML/图片），请重试。", name=path.name))
            continue
        return [path]


def choose_formats() -> list[str]:
    """交互式选择输出格式。"""
    print(tr("\n选择输出格式："))
    for key, (label, _) in FORMAT_PRESETS.items():
        print(f"  [{key}] {tr(label)}")
    answer = input(tr("> 直接回车用默认 [1]: ")).strip() or "1"
    if answer not in FORMAT_PRESETS:
        print(tr("无法识别 '{answer}'，改用默认 Markdown + JSON。", answer=answer))
        answer = "1"
    label, formats = FORMAT_PRESETS[answer]
    print(tr("已选择：{label}", label=tr(label)))
    return formats


def choose_output_dir() -> Path:
    """交互式选择输出目录，默认 output/。"""
    print(tr("\n输出目录（回车用默认 {dir}）：", dir=DEFAULT_OUTPUT_DIR))
    answer = input("> ").strip()
    if not answer:
        return DEFAULT_OUTPUT_DIR
    path = parse_dropped_path(answer)
    return path if path else DEFAULT_OUTPUT_DIR


def _ask_yes_no(prompt: str, default: bool = False) -> bool:
    """问一个是非题，回车用默认值。"""
    suffix = "[Y/n]" if default else "[y/N]"
    answer = input(f"> {tr(prompt)} {suffix}: ").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes", "是", "1")


def choose_options() -> ConvertOptions:
    """交互式询问高级选项。全部回车即用默认（等价于最基础的转换）。"""
    print(tr("\n高级选项（想跳过就一路回车）："))
    options = ConvertOptions()

    pages = input(tr("> 只转指定页码？如 1,3,5-7（回车=全部）: ")).strip()
    options.pages = pages or None

    if _ask_yes_no("提取图片存为独立文件？", default=False):
        options.image_output = "external"
    if _ask_yes_no("在 Markdown/文本里插入分页标记？", default=False):
        options.page_markers = True
    if _ask_yes_no("增强表格识别（无边框表格更准，稍慢）？", default=False):
        options.table_method = "cluster"
    if _ask_yes_no("对邮箱/电话/身份证等做脱敏？", default=False):
        options.sanitize = True
    # OCR 默认开启 auto；这里让用户可以关掉
    if _ask_yes_no("自动检测扫描件并用 OCR？（默认开启，需先启动 hybrid 服务）", default=True):
        options.ocr_mode = "auto"
    else:
        options.ocr_mode = "off"
    if _ask_yes_no("输出 RAG 分块（chunks.jsonl，供大模型/知识库用）？", default=False):
        options.rag_chunks = True
    if _ask_yes_no("把识别出的表格导出为 CSV？", default=False):
        options.export_tables = True
    if _ask_yes_no("质量自检（标注版 PDF + 逐页统计）？", default=False):
        options.qa = True

    return options


def choose_jobs() -> int:
    """交互式询问并发进程数，回车=1（串行）。"""
    answer = input(tr("> 并发转换进程数？多文件时可加速（回车=1 串行）: ")).strip()
    if not answer:
        return 1
    try:
        return max(1, int(answer))
    except ValueError:
        print(tr("无法识别，按串行处理。"))
        return 1


def clean_error(err: Exception, captured: str) -> str:
    """从底层 CLI 的输出里提取一行可读的错误原因，丢掉冗长的 java 命令行。"""
    if isinstance(err, subprocess.CalledProcessError):
        blob = "\n".join(filter(None, [err.stdout, err.stderr, captured]))
        for line in blob.splitlines():
            line = line.strip()
            # 底层错误形如 "Error: 'x.pdf' is not a valid PDF file (...)"
            if line.startswith("Error:") and "opendataloader-pdf CLI" not in line:
                return line[len("Error:"):].strip()
        return tr("底层转换失败（退出码 {code}）", code=err.returncode)
    return str(err)


def check_ocr_server(url: str, timeout: float = 2.0) -> bool:
    """探测 OCR(hybrid) 服务是否在线：GET {url}/health 返回 200 即为可用。"""
    health = url.rstrip("/") + "/health"
    # 显式空 ProxyHandler：本地服务不该走系统代理。曾实测 localhost:5002 经代理拿到 502 而被判离线，
    # OCR 静默不启用、整批图片输出为空（bench #4）。
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(health, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001 —— 端口上是非 HTTP 程序时会抛 http.client.BadStatusLine（不是 OSError）；
        return False       # 探测函数被监听线程每轮调用，任何异常都只能当「离线」，绝不能把线程带死


def ocr_server_hint(url: str) -> str:
    """OCR 服务不在线时的友好提示，告诉用户怎么启动。"""
    return (
        tr("OCR 服务未启动（{url}），扫描件将按普通模式转换（可能为空）。", url=url) + "\n"
        + tr("  启动办法：") + 'pip install "opendataloader-pdf[hybrid]"\n'
        + '           opendataloader-pdf-hybrid --port 5002 --ocr-lang "ch_sim,en"'
    )


def _parse_pages_spec(spec: str) -> set[int] | None:
    """把 "1,3,5-7" 解析成页码集合 {1,3,5,6,7}；解析失败返回 None（视为全部页）。"""
    pages: set[int] = set()
    try:
        for part in spec.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                lo, hi = part.split("-", 1)
                pages.update(range(int(lo), int(hi) + 1))
            else:
                pages.add(int(part))
    except ValueError:
        return None
    return pages or None


def _pdfplumber_page_texts(pdf: Path, options: ConvertOptions) -> list[tuple[int, str]] | None:
    """用 pdfplumber 逐页提取纯文本，返回 [(页码, 文本), ...]。

    尊重 options 的页码范围和密码。pdfplumber 未安装、文件打不开或提取出错时
    返回 None，调用方自行退回 JVM 路径或保留原失败结果。
    """
    if pdfplumber is None:
        return None
    wanted = _parse_pages_spec(options.pages) if options.pages else None
    try:
        with pdfplumber.open(pdf, password=options.password or "") as doc:
            texts: list[tuple[int, str]] = []
            for number, page in enumerate(doc.pages, start=1):
                if wanted is not None and number not in wanted:
                    continue
                texts.append((number, page.extract_text() or ""))
            return texts
    except Exception:
        return None


def probe_text_density(pdf: Path, options: ConvertOptions | None = None) -> float | None:
    """轻量探测 PDF 每页平均字符数，用于判断是否为扫描件。

    优先走 pdfplumber（毫秒级、不起 JVM）；不可用或打不开时退回底层引擎探测。
    返回每页平均字符数；探测失败返回 None（无法判断）。
    """
    options = options or ConvertOptions()
    texts = _pdfplumber_page_texts(pdf, options)
    if texts:
        total = sum(len(text.strip()) for _, text in texts)
        return total / len(texts)
    return _probe_text_density_java(pdf, options)


def _probe_text_density_java(pdf: Path, options: ConvertOptions) -> float | None:
    """密度探测的 JVM 退路：跑一次 json 转换到临时目录，统计 content 长度 / 页数。

    注意：不能用 to_stdout=True（quiet 模式下 runner 直写 sys.stdout.buffer，
    redirect 捕获不到），必须落盘临时目录。
    """
    import tempfile

    kwargs = {}
    if options.password:
        kwargs["password"] = options.password
    if options.pages:
        kwargs["pages"] = options.pages

    sink = io.StringIO()
    with tempfile.TemporaryDirectory() as tmp:
        try:
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                opendataloader_pdf.convert(
                    input_path=str(pdf),
                    output_dir=tmp,
                    format="json",
                    quiet=True,
                    **kwargs,
                )
        except Exception:
            return None

        json_files = list(Path(tmp).rglob("*.json"))
        if not json_files:
            return None
        try:
            doc = json.loads(json_files[0].read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    pages = doc.get("number of pages") or 1
    total_chars = _count_content_chars(doc)
    return total_chars / max(pages, 1)


def _count_content_chars(node) -> int:
    """递归统计 JSON 结构树里所有 content 字段的字符总数。"""
    total = 0
    if isinstance(node, dict):
        content = node.get("content")
        if isinstance(content, str):
            total += len(content.strip())
        for value in node.values():
            total += _count_content_chars(value)
    elif isinstance(node, list):
        for item in node:
            total += _count_content_chars(item)
    return total


def decide_ocr(pdf: Path, options: ConvertOptions, server_ok: bool) -> tuple[bool, str]:
    """根据输入类型和 OCR 模式决定本文件是否走 OCR。返回 (是否用OCR, 说明)。"""
    suffix = pdf.suffix.lower()
    if suffix in format_adapters.ADAPTER_EXTENSIONS:
        return False, ""  # 办公文档/HTML 走适配器，OCR 用不上，也别去探测密度
    if suffix in format_adapters.IMAGE_EXTENSIONS:
        # 图片必然没有文字层，不用探测：服务在线就 OCR，离线只提取版面
        if options.ocr_mode == "off":
            return False, ""
        if not server_ok:
            return False, tr("图片输入：OCR 服务未启动，仅提取版面（无文字）")
        return True, tr("图片输入，走 OCR")
    if options.ocr_mode == "off":
        return False, ""
    if not server_ok:
        # auto 是默认模式，服务未启动很常见：批开头已提示过一次，这里不再逐文件刷屏
        if options.ocr_mode == "force":
            return False, tr("OCR 服务未启动，按普通模式转换")
        return False, ""
    if options.ocr_mode == "force":
        return True, tr("强制 OCR")
    # auto：探测文字密度，过低则判定为扫描件
    density = probe_text_density(pdf, options)
    if density is None:
        return False, tr("无法探测文字密度，按普通模式转换")
    if density < SCANNED_CHARS_PER_PAGE:
        return True, tr("疑似扫描件（每页约 {density:.0f} 字符），启用 OCR", density=density)
    return False, ""


def _repair_pdf(pdf: Path, options: ConvertOptions) -> Path | None:
    """用 pikepdf（qpdf）重写一份结构完好的副本，放在临时目录里、文件名不变。

    针对 xref 表损坏 / 文件被截断这类「主引擎与 pdfminer 都拒收、qpdf 能重建对象表」的
    文件（bench issue #3）。pikepdf 未安装、密码不对或 qpdf 也救不回来时返回 None。
    调用方用完负责删掉临时目录（`shutil.rmtree(path.parent)`）。
    """
    if pikepdf is None:
        return None
    import tempfile

    import warnings

    tmp_dir = Path(tempfile.mkdtemp(prefix="morsel-repair-"))
    target = tmp_dir / pdf.name
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # pikepdf 对「给了密码但文件没加密」会打 UserWarning
            with pikepdf.open(pdf, password=options.password or "") as doc:
                # 原件加密的话副本保持加密（encryption=True），别把明文副本写进临时目录
                doc.save(target, encryption=bool(doc.is_encrypted))
        return target
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return None


def _run_engine(src: Path, dest: Path, conv_formats: list[str], options: ConvertOptions, use_ocr: bool) -> str | None:
    """跑一次底层 Java 引擎；成功返回 None，失败返回清洗后的错误信息（保证非空——异常本身就是失败信号）。"""
    # 底层库会把 JAR 的日志和完整命令行直接打到 stdout/stderr，这里拦下来自己处理
    sink = io.StringIO()
    try:
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            opendataloader_pdf.convert(
                input_path=str(src),
                output_dir=str(dest),
                format=conv_formats,
                quiet=True,
                **options.to_convert_kwargs(conv_formats, use_ocr=use_ocr),
            )
    except Exception as err:  # 单个文件失败不应中断整批
        return clean_error(err, sink.getvalue()) or type(err).__name__
    return None


# 引擎报错里长这样的才像「文件结构坏了」，值得花一次 qpdf 修复 + 再起一次 JVM；
# 缺 Java / OOM / 超时 / 密码错之类的失败修了也白修（且会给整批每个文件都多跑一遍）
_STRUCTURAL_ERROR_RE = re.compile(
    r"not a valid PDF|corrupt|truncat|xref|startxref|trailer|%%EOF|Unexpected EOF|Root object|"
    r"damaged|malformed|missing %PDF|invalid PDF|Expected .* object|end of file",
    re.I,
)


def looks_structural_error(message: str) -> bool:
    return bool(message) and bool(_STRUCTURAL_ERROR_RE.search(message))


def _fallback_convert(
    pdf: Path,
    dest: Path,
    formats: list[str],
    options: ConvertOptions,
    result: ConvertResult,
) -> bool:
    """兜底网：Java 引擎彻底失败时用 pdfplumber 降级提取纯文本。

    成功时就地改写 result（ok/degraded/produced/pages，原错误挪进 note）并返回 True；
    pdfplumber 不可用、文件打不开或全文无文字（如扫描件）时返回 False，保留原失败结果。
    只产出 Markdown / 纯文本（版面结构拿不到），JSON 相关后处理不适用。
    """
    texts = _pdfplumber_page_texts(pdf, options)
    if not texts or not any(text.strip() for _, text in texts):
        return False

    def body(sep_template: str | None) -> str:
        parts: list[str] = []
        for index, (number, text) in enumerate(texts):
            if sep_template and index > 0:
                parts.append(sep_template.replace("%page-number%", str(number)).strip("\n"))
            if text.strip():
                parts.append(text.strip())
        return "\n\n".join(parts) + "\n"

    produced: list[Path] = []
    try:
        if "markdown" in formats:
            path = dest / f"{pdf.stem}.md"
            path.write_text(body(markdown_page_separator() if options.page_markers else None), encoding="utf-8")
            produced.append(path)
        if "text" in formats or not produced:  # 没选 markdown/text 时也兜出一份 .txt
            path = dest / f"{pdf.stem}.txt"
            path.write_text(body(text_page_separator() if options.page_markers else None), encoding="utf-8")
            produced.append(path)
    except OSError:
        return False

    notes = [tr("降级转换（pdfplumber 纯文本）：{error}", error=result.error)]
    if options.rag_chunks or options.export_tables or options.qa:
        notes.append(tr("分块/表格/QA 需结构树，降级输出不适用"))
    fixed = normalize_produced_files(produced)
    if fixed:
        notes.append(tr("修正 {n} 处兼容码位", n=fixed))
    rtl_lines = restore_rtl_products(pdf, produced, formats, options, use_ocr=False, second_pass=False)
    if rtl_lines:
        notes.append(tr("还原 {n} 行 RTL 文本的逻辑序", n=rtl_lines))
    result.ok = True
    result.degraded = True
    result.error = ""
    result.produced = produced
    result.pages = len(texts)
    result.note = i18n.note_sep().join(x for x in (result.note, *notes) if x)
    return True


def convert_one(
    pdf: Path,
    out_root: Path,
    formats: list[str],
    options: ConvertOptions | None = None,
    use_ocr: bool = False,
) -> ConvertResult:
    """转换单个输入文件——格式路由入口。产物放进 output/<文件名>/ 子目录，避免同名覆盖。

    PDF 走底层 Java 引擎；docx/xlsx/pptx/HTML 走 format_adapters；图片包装成单页 PDF
    走 OCR 通道。批量引擎/断点续传/报告在 convert_one 之上，感知不到格式差异。
    """
    options = options or ConvertOptions()
    suffix = pdf.suffix.lower()
    if suffix in format_adapters.ADAPTER_EXTENSIONS:
        return _convert_office(pdf, out_root, formats, options)
    if suffix in format_adapters.IMAGE_EXTENSIONS:
        return _convert_image(pdf, out_root, formats, options, use_ocr)
    return _convert_pdf(pdf, out_root, formats, options, use_ocr)


def _convert_pdf(
    pdf: Path,
    out_root: Path,
    formats: list[str],
    options: ConvertOptions,
    use_ocr: bool = False,
) -> ConvertResult:
    """PDF 主路径：底层 Java 引擎 → （拒收时）qpdf 修复结构后重喂引擎 → pdfplumber 纯文本兜底。"""
    dest = out_root / pdf.stem
    dest.mkdir(parents=True, exist_ok=True)
    result = ConvertResult(pdf=pdf, used_ocr=use_ocr)
    tick = time.time()

    conv_formats = list(formats)
    if (options.rag_chunks or options.export_tables or options.qa) and "json" not in conv_formats:
        conv_formats.append("json")  # 分块/表格导出/质量自检都基于 JSON 结构树，缺了就自动补上
    if options.qa and "pdf" not in conv_formats:
        conv_formats.append("pdf")  # 质量自检需要标注版 PDF 供人工比对

    src = pdf  # 实际喂给引擎/兜底/RTL 第二遍的文件；结构修复后换成临时副本（文件名不变，产物名不受影响）
    repaired_dir: Path | None = None
    notes: list[str] = []
    try:
        error = _run_engine(pdf, dest, conv_formats, options, use_ocr)
        if error is not None and looks_structural_error(error):
            # 引擎拒收且像结构损坏：先试 qpdf 修复再喂一次（保住完整结构树），修不好才落到 pdfplumber 纯文本
            repaired = _repair_pdf(pdf, options)
            if repaired is not None:
                repaired_dir = repaired.parent
                if _run_engine(repaired, dest, conv_formats, options, use_ocr) is None:
                    fresh = [p for p in dest.rglob("*") if p.is_file() and _mtime_at_least(p, math.floor(tick))]
                    if extracted_text_chars(fresh) == 0:
                        # qpdf 只保证对象表合法，不保证内容流可解码（加密字典随 trailer 一起截掉时副本是密文）：
                        # 「修复成功」但产物无字不算成功，扔掉这次产物继续走兜底/失败
                        for p in fresh:
                            p.unlink(missing_ok=True)
                    else:
                        src = repaired
                        notes.append(tr("PDF 结构损坏，已修复后转换：{error}", error=error))
                        error = None
        if error is not None:
            result.error = error
            # 修复副本（若有）比原件更可能被 pdfminer 接受；副本兜不住再试原件
            if not _fallback_convert(src if repaired_dir is None else repaired, dest, conv_formats, options, result) \
                    and (repaired_dir is None or not _fallback_convert(pdf, dest, conv_formats, options, result)):
                prune_if_empty(dest)
            result.duration = time.time() - tick
            return result

        produced = sorted(p for p in dest.rglob("*") if p.is_file())
        if not produced:
            result.error = tr("转换未产生任何输出文件")
            result.note = i18n.note_sep().join(notes)  # 修复过的话把这条留给用户（_fallback_convert 在其上拼接）
            if not _fallback_convert(src, dest, conv_formats, options, result):
                prune_if_empty(dest)
            result.duration = time.time() - tick
            return result
        result.duration = time.time() - tick
        result.ok = True
        result.produced = produced
        result.pages = _pages_from_json(produced)
        fixed = normalize_produced_files(produced)  # 必须在 _post_process 之前：分块/表格要吃修正后的 JSON
        if fixed:
            notes.append(tr("修正 {n} 处兼容码位", n=fixed))
        rtl_lines = restore_rtl_products(src, produced, conv_formats, options, use_ocr)
        if rtl_lines:
            notes.append(tr("还原 {n} 行 RTL 文本的逻辑序", n=rtl_lines))
        tidied = tidy_products(produced) if options.tidy else 0   # 同样要在 _post_process 之前
        if tidied:
            notes.append(tr("整理 {n} 处结构噪声", n=tidied))
    finally:
        if repaired_dir is not None:
            shutil.rmtree(repaired_dir, ignore_errors=True)
    result.note = i18n.note_sep().join(notes)
    _post_process(result, options)
    return result


def _post_process(result: ConvertResult, options: ConvertOptions) -> None:
    """转换成功后的场景加工（RAG 分块 / 表格导出 / QA），PDF 与适配器路径共用。

    失败不推翻已成功的转换，只在 note 里标出来。就地修改 result。
    """
    notes: list[str] = []
    extra_files: set[Path] = set()
    json_files = [p for p in result.produced if p.suffix.lower() == ".json"]
    if options.rag_chunks:
        try:
            if not json_files:
                raise ValueError(tr("没有 JSON 产物可供分块"))
            chunk_path, n_chunks = write_chunks(json_files[0], result.pdf.name, options.chunk_tokens)
            extra_files.add(chunk_path)
            notes.append(tr("RAG 分块 {n} 块", n=n_chunks))
        except Exception as err:
            notes.append(tr("RAG 分块失败：{err}", err=err))
    if options.export_tables:
        try:
            if not json_files:
                raise ValueError(tr("没有 JSON 产物可供提取表格"))
            table_paths = write_tables(json_files[0])
            extra_files.update(table_paths)
            notes.append(tr("导出 {n} 个表格", n=len(table_paths)) if table_paths else tr("未发现表格"))
        except Exception as err:
            notes.append(tr("表格导出失败：{err}", err=err))
    if options.qa:
        try:
            if not json_files:
                raise ValueError(tr("没有 JSON 产物可供质检"))
            qa_path, flagged, total = write_qa(json_files[0])
            extra_files.add(qa_path)
            notes.append(tr("QA：{flagged}/{total} 页疑似需复核", flagged=flagged, total=total) if flagged else tr("QA：{total} 页均正常", total=total))
        except Exception as err:
            notes.append(tr("质量自检失败：{err}", err=err))
    if extra_files:
        result.produced = sorted(set(result.produced) | extra_files)
    result.note = i18n.note_sep().join(x for x in (result.note, *notes) if x)


def _convert_office(
    pdf: Path,
    out_root: Path,
    formats: list[str],
    options: ConvertOptions,
) -> ConvertResult:
    """docx/xlsx/pptx/HTML 适配器路径：解析成结构树后按需渲染，产物布局与 PDF 路径一致。

    树 schema 与底层引擎对齐，RAG 分块/表格导出/QA 直接复用；
    QA 没有标注版 PDF（适配器不渲染版面），只有逐页统计。
    """
    dest = out_root / pdf.stem
    dest.mkdir(parents=True, exist_ok=True)
    result = ConvertResult(pdf=pdf)
    tick = time.time()

    conv_formats = list(formats)
    if (options.rag_chunks or options.export_tables or options.qa) and "json" not in conv_formats:
        conv_formats.append("json")

    try:
        tree = format_adapters.parse_office(pdf)
        wanted = _parse_pages_spec(options.pages) if options.pages else None
        if wanted is not None:
            tree["kids"] = [k for k in tree["kids"] if k.get("page number") in wanted]
        md_sep = markdown_page_separator() if options.page_markers else None
        txt_sep = text_page_separator() if options.page_markers else None
        if "json" in conv_formats:
            (dest / f"{pdf.stem}.json").write_text(
                json.dumps(tree, ensure_ascii=False, indent=1), encoding="utf-8")
        if "markdown" in conv_formats:
            (dest / f"{pdf.stem}.md").write_text(
                format_adapters.render_markdown(tree, md_sep), encoding="utf-8")
        if "html" in conv_formats:
            (dest / f"{pdf.stem}.html").write_text(
                format_adapters.render_html(tree), encoding="utf-8")
        if "text" in conv_formats:
            (dest / f"{pdf.stem}.txt").write_text(
                format_adapters.render_text(tree, txt_sep), encoding="utf-8")
    except Exception as err:
        prune_if_empty(dest)
        result.duration = time.time() - tick
        result.error = str(err)
        return result

    result.duration = time.time() - tick
    produced = sorted(p for p in dest.rglob("*") if p.is_file())
    if not produced:
        prune_if_empty(dest)
        result.error = tr("转换未产生任何输出文件")
        return result
    result.ok = True
    result.produced = produced
    result.pages = tree.get("number of pages")
    # HTML 常由 PDF 转来，会以数字实体（&#64257; = ﬁ）继承排版连字，解码后原样进产物（bench #2）；
    # 与 PDF 路径同一套归一化，且必须在 _post_process 之前（分块/表格吃修正后的 JSON）
    fixed = normalize_produced_files(produced)
    notes = [tr("修正 {n} 处兼容码位", n=fixed)] if fixed else []
    # 假标题（EUR-Lex 的 <title> 其实是源 XML 文件名，被当首标题补进来）也出在这条路径上，
    # 与 PDF 路径一样要在 _post_process 之前清理（bench #9/#10）
    tidied = tidy_products(produced) if options.tidy else 0
    if tidied:
        notes.append(tr("整理 {n} 处结构噪声", n=tidied))
    result.note = _join_notes(*notes)
    _post_process(result, options)
    return result


def _convert_image(
    pdf: Path,
    out_root: Path,
    formats: list[str],
    options: ConvertOptions,
    use_ocr: bool = False,
) -> ConvertResult:
    """图片路径：包装成同名单页 PDF（多帧图每帧一页），走已有的 PDF/OCR 管线。

    result.pdf 保持指向原图片（断点续传清单、报告都按源文件记）。
    """
    import tempfile

    tick = time.time()
    turned = 0.0
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_pdf = Path(tmp) / f"{pdf.stem}.pdf"
            turned = format_adapters.image_to_pdf(pdf, tmp_pdf, deskew=options.deskew)
            _timing(pdf, "image_to_pdf", tick)
            engine_tick = time.time()
            result = _convert_pdf(tmp_pdf, out_root, formats, options, use_ocr)
            _timing(pdf, "engine+ocr", engine_tick)
    except format_adapters.AdapterError as err:
        return ConvertResult(pdf=pdf, error=str(err), duration=time.time() - tick)
    result.pdf = pdf
    result.duration = time.time() - tick
    if turned:
        # 版面分析按倾斜的行切块会把内容切碎，所以进引擎前先转正（bench #8）
        result.note = _join_notes(result.note, tr("已校正 {angle} 度倾斜", angle=f"{turned:.1f}"))
    # produced 是 output/<名>/ 目录里现存全部文件（含上一轮换格式留下的旧产物），判空只能看本次写出的：
    # 按 mtime 过滤（取整，容忍 1 秒粒度的文件系统）
    fresh = [p for p in result.produced if _mtime_at_least(p, math.floor(tick))]
    if result.ok and not result.degraded and extracted_text_chars(fresh) == 0:
        # 图片没有文字层，未经 OCR 时引擎只能给出「一张图」的版面；这不是成功，是空产出（bench #5）。
        # 状态标 degraded 让报告能区分；needs_ocr 让清单知道 OCR 服务在线时要重转。
        result.degraded = True
        result.needs_ocr = True  # 关着 OCR 也记：只有 OCR 未关且服务在线时才会触发补转
        if use_ocr:
            # 走了 OCR 仍无字：可能真是空白图，也可能后端出错被 hybrid_fallback 静默退回 Java（Python 层看不出来），
            # 所以也算「等 OCR」，服务正常时再试一次；清单里 ocr_attempts 计数防止空白图每轮重转
            result.note = _join_notes(result.note, tr("OCR 未产出文字（后端失败或未识别），产物无文字内容；服务正常后重跑会再试一次"))
        else:
            if options.ocr_mode == "off":
                result.note = _join_notes(result.note, tr("产物无文字内容：图片未经 OCR（OCR 已关闭）"))
            else:
                result.note = _join_notes(result.note, tr("产物无文字内容：图片未经 OCR，OCR 服务上线后重跑会自动重转"))
    return result


def _join_notes(*parts: str) -> str:
    return i18n.note_sep().join(x for x in parts if x)


def _timing(source: Path, phase: str, since: float) -> None:
    """`MORSEL_DEBUG_TIMING=1` 时把各阶段耗时打到 stderr（排查单文件异常慢用，bench #14）。"""
    if os.environ.get("MORSEL_DEBUG_TIMING"):
        print(f"[timing] {source.name} {phase} {time.time() - since:.2f}s", file=sys.stderr, flush=True)


def _mtime_at_least(path: Path, stamp: float) -> bool:
    try:
        return path.stat().st_mtime >= stamp
    except OSError:
        return False


_MD_NON_TEXT_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)|<img\b[^>]*>|<!--.*?-->", re.S)


def _page_separator_re() -> re.Pattern:
    """匹配 md/txt 分页标记（中英两种文案都认，产物可能是另一语言下转出来的），统计文字量时剔除。"""
    parts = []
    for template in ("**— 第 %page-number% 页 —**", "第 %page-number% 页"):
        for text in {template, i18n._EN.get(template, template)}:
            parts.append(re.escape(text).replace(re.escape("%page-number%"), r"\d+"))
    return re.compile("|".join(parts))


def extracted_text_chars(produced: list[Path]) -> int:
    """统计文本类产物里真正的文字量（字母/数字/汉字个数），用来判断「转出来是空的」。

    优先读 JSON 结构树（只数 content 字段，不受图片文件名/版面标记干扰），没有 JSON 再读
    md/txt/html 并剔除图片引用与分页标记。读不到任何文本产物时返回 -1（无法判断，不当空处理）。
    调用方要传**本次写出的**产物——output/<名>/ 里上一轮换格式留下的旧文件会误导判断。
    """
    by_suffix = {p.suffix.lower(): p for p in produced}
    try:
        if ".json" in by_suffix:
            tree = json.loads(by_suffix[".json"].read_text(encoding="utf-8"))
            return sum(1 for text in _iter_json_content(tree) for c in text if c.isalnum())
        for suffix in (".txt", ".md", ".html", ".htm"):
            if suffix in by_suffix:
                text = by_suffix[suffix].read_text(encoding="utf-8")
                if suffix in (".html", ".htm"):
                    text = re.sub(r"<(head|style|script)\b.*?</\1\s*>", " ", text, flags=re.S | re.I)  # <title> 里是文件名
                    text = re.sub(r"<[^>]+>", " ", text)
                text = _MD_NON_TEXT_RE.sub(" ", text)
                text = _page_separator_re().sub(" ", text)
                return sum(1 for c in text if c.isalnum())
    except (OSError, UnicodeDecodeError, ValueError):
        return -1
    return -1


def _iter_json_content(node):
    """深度遍历结构树，产出所有 content 字符串。"""
    if isinstance(node, dict):
        content = node.get("content")
        if isinstance(content, str):
            yield content
        for value in node.values():
            if isinstance(value, (dict, list)):
                yield from _iter_json_content(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_json_content(item)



# 兼容码位归一化 ─────────────────────────────────────────────────────────
# macOS Quartz 生成的 PDF（PingFang/Hiragino 的 Type 3 子集字体）ToUnicode 表常把常用汉字
# 映射到「康熙部首」(U+2F00-2FD5) / 「CJK 部首补充」(U+2E80-2EF3) / 「CJK 兼容表意」
# (U+F900-FAFF, U+2F800-2FA1F) 区，外加 Office 常见的 ﬁ/ﬂ 连字 (U+FB00-FB06)。
# 显示上一模一样，但 grep/正则/分词/字频统计全部失效（实测一份 3.5 万字转录稿有 2,834 处）。
# 只对这几个区间做 NFKC，不做全文 NFKC——后者会把全角标点/上标等一并改掉，属过度修正。
_COMPAT_RANGES = ((0x2E80, 0x2EF3), (0x2F00, 0x2FD5), (0xF900, 0xFAFF), (0xFB00, 0xFB06), (0x2F800, 0x2FA1F))
_COMPAT_MAP: dict[int, str] = {}
for _lo, _hi in _COMPAT_RANGES:
    for _cp in range(_lo, _hi + 1):
        _norm = unicodedata.normalize("NFKC", chr(_cp))
        if _norm and _norm != chr(_cp):
            _COMPAT_MAP[_cp] = _norm
_COMPAT_RE = re.compile("[" + "".join(re.escape(chr(cp)) for cp in _COMPAT_MAP) + "]")
NORMALIZE_TEXT_SUFFIXES = {".md", ".txt", ".json", ".html", ".htm"}


def normalize_compat_chars(text: str) -> tuple[str, int]:
    """把兼容区码位归一到常规码位，返回 (新文本, 修正字符数)。无命中时零开销返回原串。"""
    if not _COMPAT_RE.search(text):
        return text, 0
    return _COMPAT_RE.subn(lambda m: _COMPAT_MAP[ord(m.group())], text)


def normalize_produced_files(produced: list[Path]) -> int:
    """就地修正文本类产物（md/txt/json/html）中的兼容码位，返回修正总数。单文件失败静默跳过。"""
    total = 0
    for path in produced:
        if path.suffix.lower() not in NORMALIZE_TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        fixed, n = normalize_compat_chars(text)
        if n:
            path.write_text(fixed, encoding="utf-8")
            total += n
    return total


# 结构噪声清理 ──────────────────────────────────────────────────────────
# 版面分析会把两类东西判错，两类都直接污染 get_outline 与 RAG 分块（bench issue #9 / #10）：
# ① 图注面板上的单个字母、RFC 里的 MUST/SHOULD、源文件内部名（L_2016157EN.01000101.xml）
#    被提升成标题；② 法条里 "(1) …" 开头的**条款段落**被整段渲染成列表项。
# 这里只清理**判据明确**的那几类，宁可漏也不误伤：长标题是否吞了正文首句没有可靠信号
# （"§ 2 Begriffsbestimmungen Im Sinne dieses Gesetzes ist" 连句末标点都没有），故不处理。
_RFC2119_WORDS = {"MUST", "MUST NOT", "SHALL", "SHALL NOT", "SHOULD", "SHOULD NOT",
                  "MAY", "REQUIRED", "RECOMMENDED", "NOT RECOMMENDED", "OPTIONAL"}
_FILENAME_HEADING = re.compile(r"^\S+\.(?:xml|pdf|html?|docx?|xlsx?|pptx?|txt|json|csv)$", re.I)
_NUMBERED_PARAGRAPH = re.compile(r"^\(\s*\d{1,3}\s*\)\s")
# 列表项里残留的项目符号字形：底层引擎把 "• First point" 整串当作 list item 的内容，
# 渲染时再加一层 "- "，产物就成了 "- • First point"（bench 冒烟第 ③ 条）。JSON 的
# list item content 同样带着它，RAG 分块与 MCP get_section 一并受影响。
# 只剥「字形 + 空白 + 还有内容」，不碰 - / * （可能是负号或强调），不碰编号（有序列表的序号有意义）。
# U+F0B7 是 Word 用 Symbol 字体做项目符号时落进私有区的码位，实际文档里很常见。
_LIST_BULLET_GLYPHS = "\u2022\u2023\u25aa\u25ab\u25cf\u25cb\u25e6\u2043\u2219\u00b7\u25b8\u25b9\u2756\uf0b7"
_LEADING_BULLET = re.compile(f"^[{_LIST_BULLET_GLYPHS}]+[ \t\u00a0]+(?=\\S)")
TIDY_LIST_PARAGRAPH_CHARS = 80   # "(1) …" 后面还有这么多字，就是条款段落不是列表项
_MD_HEADING_LINE = re.compile(r"^(#{1,6})[ \t]+(.*\S)[ \t]*$")
_MD_LIST_LINE = re.compile(r"^([ \t]*)- (.*)$")
_HTML_HEADING = re.compile(r"<h([1-6])(\s[^>]*)?>(.*?)</h\1>", re.S | re.I)
_HTML_LIST_ITEM = re.compile(r"<li(?:\s[^>]*)?>(.*?)</li>", re.S | re.I)
# <li> 开头的项目符号字形（允许中间隔着 <p>/<span> 之类的起始标签）
_HTML_LI_BULLET = re.compile(
    r"(<li(?:\s[^>]*)?>)((?:\s*<[^/!][^>]*>)*\s*)"
    f"[{_LIST_BULLET_GLYPHS}]+[ \t\u00a0]+(?=\\S)", re.I)
# **最内层**列表（内部不再嵌套 ul/ol）。不能用朴素的 `<(ul|ol).*?</\1>`：非贪婪匹配碰到嵌套列表
# 会从外层的 <ul> 一路配到内层的 </ul>，把两层的 <li> 混成一组（实测德语法条的条款列表正好是内层）。
# 只改最内层，外层原样保留——条款列表本来就在最内层，够用，且绝不会产出结构错乱的 HTML
_HTML_INNER_LIST = re.compile(r"<(ul|ol)(\s[^>]*)?>((?:(?!<(?:ul|ol)\b)[\s\S])*?)</\1>", re.I)
_TIDY_TEXT_SUFFIXES = {".md", ".markdown", ".json", ".html", ".htm"}


def _is_cjk_char(ch: str) -> bool:
    return "\u3400" <= ch <= "\u9fff" or "\u3040" <= ch <= "\u30ff" or "\uac00" <= ch <= "\ud7af"


def heading_is_noise(text: str) -> bool:
    """这行明显不是标题：单个字母/符号碎片、RFC 2119 关键词、源文件内部名。"""
    flat = " ".join((text or "").split())
    if not flat:
        return False
    # 单字符：图注面板的 a/b/c、公式碎片、阿语单字母。CJK 单字（「記」「序」）是正经小标题，放过
    if len(flat) == 1 and not _is_cjk_char(flat):
        return True
    return flat.upper() in _RFC2119_WORDS or bool(_FILENAME_HEADING.match(flat))


def strip_list_bullet(text: str) -> tuple[str, bool]:
    """剥掉列表项开头残留的项目符号字形，返回 (文本, 是否改动)。幂等。"""
    if not text:
        return text, False
    fixed = _LEADING_BULLET.sub("", text, count=1)
    return fixed, fixed != text


def list_paragraph_flags(contents: list[str]) -> list[bool]:
    """一个列表里哪些项其实是「(1) …」开头的条款段落（法条里成片出现），不是列表项。

    **按整个列表判**，不逐项判：同一条法条里 "(1) 短句。" 和 "(2) 长段落…" 并排出现，
    逐项判会把同一组条款一半留在列表里一半变成段落。判据是「有编号前缀的项 ≥2 个，
    且其中至少一个长到段落级」——单独一个 "(1) 甲" 更可能是真的列表项，放过。
    """
    flat = [(c or "").strip() for c in contents]
    numbered = [bool(_NUMBERED_PARAGRAPH.match(c)) for c in flat]
    if sum(numbered) < 2 or not any(n and len(c) > TIDY_LIST_PARAGRAPH_CHARS
                                    for n, c in zip(numbered, flat)):
        return [False] * len(flat)
    return numbered


def _strip_tags(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html)


def _tidy_json_nodes(nodes: list) -> tuple[list, int]:
    """结构树里：噪声标题降成段落；假列表按「连续真项 / 连续假项」切开，假项各成一个段落。"""
    out: list = []
    changed = 0
    for node in nodes:
        if not isinstance(node, dict):
            out.append(node)
            continue
        kids = node.get("kids")
        if isinstance(kids, list):
            node["kids"], n = _tidy_json_nodes(kids)
            changed += n
        kind = node.get("type")
        if kind == "heading" and heading_is_noise(node.get("content") or ""):
            node = {k: v for k, v in node.items() if k != "heading level"}
            node["type"] = "paragraph"
            changed += 1
        elif kind == "list":
            items = node.get("list items") or []
            for item in items:      # 先剥残留的项目符号，再判条款段落（"• (1) …" 两条规则会叠）
                if isinstance(item, dict):
                    stripped, hit = strip_list_bullet(item.get("content") or "")
                    if hit:
                        item["content"] = stripped
                        changed += 1
            flags = list_paragraph_flags([i.get("content") if isinstance(i, dict) else None
                                          for i in items])
            if any(flags):
                keep: list = []   # 攒着的真列表项，遇到假项或走完时收成一个列表节点

                def flush() -> None:
                    if keep:
                        kept = {k: v for k, v in node.items()
                                if k not in ("list items", "number of list items")}
                        kept["number of list items"] = len(keep)
                        kept["list items"] = list(keep)
                        out.append(kept)
                        keep.clear()

                for item, is_para in zip(items, flags):
                    if is_para:
                        flush()
                        # 整份搬过去：list item 常常不是叶子，kids 里挂着整段嵌套列表/段落，
                        # 只抄 content 会把那些正文从 JSON 里直接删掉（实测一份德语法条丢 1143 字）
                        para = dict(item)
                        para["type"] = "paragraph"
                        if para.get("page number") is None:
                            para["page number"] = node.get("page number")
                        out.append(para)
                        changed += 1
                    else:
                        keep.append(item)
                flush()
                continue
        out.append(node)
    return out, changed


def _tidy_markdown(text: str) -> tuple[str, int]:
    src = text.split("\n")
    out: list[str] = []
    changed = 0
    # 按「连续且同缩进的 - 行」分块（缩进变了就是嵌套子列表，另算一块），整块判定后逐行改写
    i = 0
    while i < len(src):
        head = _MD_HEADING_LINE.match(src[i])
        if head and heading_is_noise(head.group(2)):
            out.append(head.group(2))
            changed += 1
            i += 1
            continue
        first = _MD_LIST_LINE.match(src[i])
        if not first:
            out.append(src[i])
            i += 1
            continue
        indent = first.group(1)
        block = [first]
        j = i + 1
        while j < len(src):
            m = _MD_LIST_LINE.match(src[j])
            if not m or m.group(1) != indent:
                break
            block.append(m)
            j += 1
        texts, hits = [], []
        for m in block:
            stripped, hit = strip_list_bullet(m.group(2))
            changed += hit
            texts.append(stripped)
            hits.append(hit)
        flags = list_paragraph_flags(texts)
        for m, body, hit, is_para in zip(block, texts, hits, flags):
            if not is_para:
                out.append(f"{indent}- {body}" if hit else m.group(0))
                continue
            # 脱掉列表符号后必须自成一段：紧贴着上一条列表项会被 Markdown 当成它的续行
            if out and out[-1].strip():
                out.append("")
            out.append(indent + body)
            out.append("")
            changed += 1
        while len(out) > 1 and out[-1] == "" and (j >= len(src) or src[j].strip() == ""):
            out.pop()   # 块尾多补的空行，后面本来就有空行时去掉
            break
        i = j
    return "\n".join(out), changed


def _tidy_html(text: str) -> tuple[str, int]:
    changed = 0

    def head(m: re.Match) -> str:
        nonlocal changed
        if not heading_is_noise(_strip_tags(m.group(3))):
            return m.group(0)
        changed += 1
        return f"<p>{m.group(3)}</p>"

    def block(m: re.Match) -> str:
        """一个列表整块一起判：命中的 <li> 拆成 <p> 并把列表就地切开，剩下的留在列表里。"""
        nonlocal changed
        tag, attrs, body = m.group(1), m.group(2) or "", m.group(3)
        items = _HTML_LIST_ITEM.findall(body)
        flags = list_paragraph_flags([_strip_tags(x) for x in items])
        if not any(flags):
            return m.group(0)
        parts, pending = [], []

        def flush() -> None:
            if pending:
                parts.append(f"<{tag}{attrs}>" + "".join(pending) + f"</{tag}>")
                pending.clear()

        for raw, inner, is_para in zip(_HTML_LIST_ITEM.finditer(body), items, flags):
            if is_para:
                flush()
                stripped = inner.strip()
                parts.append(stripped if stripped.startswith("<p") else f"<p>{stripped}</p>")
                changed += 1
            else:
                pending.append(raw.group(0))
        flush()
        return "".join(parts)

    def bullet(m: re.Match) -> str:
        nonlocal changed
        changed += 1
        return m.group(1) + m.group(2)

    text = _HTML_LI_BULLET.sub(bullet, text)
    return _HTML_INNER_LIST.sub(block, _HTML_HEADING.sub(head, text)), changed


def tidy_products(produced: list[Path]) -> int:
    """就地清理产物里的结构噪声，返回改动处数。单文件失败静默跳过（不能推翻已成功的转换）。"""
    total = 0
    for path in produced:
        suffix = path.suffix.lower()
        if suffix not in _TIDY_TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            if suffix == ".json":
                tree = json.loads(text)
                kids = tree.get("kids") if isinstance(tree, dict) else None
                if not isinstance(kids, list):
                    continue
                tree["kids"], n = _tidy_json_nodes(kids)
                fixed = json.dumps(tree, ensure_ascii=False, indent=2)
            elif suffix in (".html", ".htm"):
                fixed, n = _tidy_html(text)
            else:
                fixed, n = _tidy_markdown(text)
        except (ValueError, TypeError, AttributeError):
            continue
        if n:
            try:
                path.write_text(fixed, encoding="utf-8")
            except OSError:
                continue
            total += n
    return total


# RTL 视觉序还原 ─────────────────────────────────────────────────────────
# 阿拉伯文/希伯来文 PDF 常按视觉顺序写内容流，抽出来每个 run 逐字符反转（bench issue #0）。
# 算法在 rtl_text.py；这里只管接线：文档级探测 → 第二遍 keep_line_breaks 转换拿物理行 →
# 逐行还原并按第一遍的行结构拼回。第二遍失败就退回整行还原（多行段落的行序会倒，但字序对）。
_RTL_TEXT_FORMATS = {".md": "markdown", ".txt": "text", ".json": "json", ".html": "html", ".htm": "html"}


def restore_rtl_products(
    pdf: Path,
    produced: list[Path],
    formats: list[str],
    options: ConvertOptions,
    use_ocr: bool = False,
    second_pass: bool = True,
) -> int:
    """就地把视觉序 RTL 产物还原成逻辑序，返回改动行数。非 RTL 文档只多读一次文件，零改动。"""
    text_files = [p for p in produced if p.suffix.lower() in _RTL_TEXT_FORMATS]
    if not text_files:
        return 0
    order = {".md": 0, ".txt": 1, ".json": 2, ".html": 3, ".htm": 3}
    text_files.sort(key=lambda p: order.get(p.suffix.lower(), 9))
    contents: dict[Path, str] = {}
    for path in text_files:
        try:
            contents[path] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    if not contents:
        return 0
    sample_path = next(iter(contents))
    if not rtl_text.looks_visual_rtl(contents[sample_path]):
        return 0

    references: dict[str, str] = {}
    if second_pass and opendataloader_pdf is not None:
        references = _rtl_second_pass(pdf, sorted({_RTL_TEXT_FORMATS[p.suffix.lower()] for p in contents}), options, use_ocr)

    sample_kind = rtl_text.kind_of_suffix(sample_path.suffix)
    mirror = rtl_text.decide_mirror(references.get(sample_path.name, contents[sample_path]), sample_kind)

    total = 0
    for path, text in contents.items():
        kind = rtl_text.kind_of_suffix(path.suffix)
        ref = references.get(path.name)
        if ref is not None:
            fixed, n = rtl_text.restore_rtl_text_with_reference(text, ref, kind, mirror)
        else:
            fixed, n = rtl_text.restore_rtl_text(text, kind, mirror)
        if n and fixed != text:
            try:
                path.write_text(fixed, encoding="utf-8")
                total += n
            except OSError:
                continue
    return total


def _rtl_second_pass(pdf: Path, formats: list[str], options: ConvertOptions, use_ocr: bool) -> dict[str, str]:
    """用 keep_line_breaks=True 再转一遍到临时目录，返回 {文件名: 文本}。失败返回空字典。"""
    import tempfile

    refs: dict[str, str] = {}
    sink = io.StringIO()
    try:
        with tempfile.TemporaryDirectory(prefix="morsel-rtl-") as tmp:
            kwargs = options.to_convert_kwargs(formats, use_ocr=use_ocr)
            kwargs["keep_line_breaks"] = True
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                opendataloader_pdf.convert(
                    input_path=str(pdf), output_dir=tmp, format=formats, quiet=True, **kwargs
                )
            for path in Path(tmp).rglob("*"):
                if path.is_file() and path.suffix.lower() in _RTL_TEXT_FORMATS:
                    try:
                        refs[path.name] = path.read_text(encoding="utf-8")
                    except (OSError, UnicodeDecodeError):
                        continue
    except Exception:  # 第二遍只是锦上添花，任何失败都退回整行还原
        return {}
    return refs


def _pages_from_json(produced: list[Path]) -> int | None:
    """从产物里的 JSON 读出页数（输出不含 json 时拿不到，返回 None）。"""
    for path in produced:
        if path.suffix.lower() != ".json":
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        pages = doc.get("number of pages")
        if isinstance(pages, int):
            return pages
    return None


def _convert_task(
    pdf: Path,
    out_root: Path,
    formats: list[str],
    options: ConvertOptions,
    server_ok: bool,
) -> ConvertResult:
    """单文件完整任务：OCR 决策 + 转换。模块级函数，进程池（pickle）要求。"""
    use_ocr, note = decide_ocr(pdf, options, server_ok)
    result = convert_one(pdf, out_root, formats, options, use_ocr=use_ocr)
    # convert_one 可能已写入 RAG 分块说明，OCR 决策说明放前面拼接
    result.note = i18n.note_sep().join(x for x in (note, result.note) if x)
    return result


def prune_if_empty(path: Path) -> None:
    """转换失败时删掉刚建的空目录，避免 output 里堆积垃圾。"""
    with contextlib.suppress(OSError):
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()


def describe_options(options: ConvertOptions) -> list[str]:
    """把启用的非默认选项列成人类可读的短句，用于日志展示。"""
    notes: list[str] = []
    if options.pages:
        notes.append(tr("页码范围 {pages}", pages=options.pages))
    if options.image_output == "off":
        notes.append(tr("不提取图片"))
    elif options.image_output == "embedded":
        notes.append(tr("图片内嵌 Markdown"))
    elif options.image_output == "external":
        notes.append(tr("图片存为独立文件"))
    if options.page_markers:
        notes.append(tr("插入分页标记"))
    if options.table_method == "cluster":
        notes.append(tr("增强表格识别"))
    if options.sanitize:
        notes.append(tr("敏感信息脱敏"))
    if options.threads and options.threads > 1:
        notes.append(tr("{n} 线程并行", n=options.threads))
    if options.include_header_footer:
        notes.append(tr("保留页眉页脚"))
    if options.keep_all_content:
        notes.append(tr("关闭内容安全过滤"))
    if not options.deskew:
        notes.append(tr("不做倾斜校正"))
    if not options.tidy:
        notes.append(tr("不清理结构噪声"))
    # auto 已是默认模式，不在这里刷屏（批处理开头会打印 OCR 服务状态）
    if options.ocr_mode == "force":
        notes.append(tr("强制 OCR"))
    elif options.ocr_mode == "off":
        notes.append(tr("OCR 已关闭"))
    if options.rag_chunks:
        notes.append(tr("RAG 分块（约 {n} token/块）", n=options.chunk_tokens))
    if options.export_tables:
        notes.append(tr("表格导出 CSV"))
    if options.qa:
        notes.append(tr("质量自检"))
    return notes


# ---------------------------------------------------------------- 第三阶段：批量工程化

# 断点续传清单：记录已成功转换的文件（源文件指纹 + 选项签名），放在输出目录下
DONE_MANIFEST_NAME = ".done.json"
# 每批转换的 CSV 报告文件名，写在输出目录下
REPORT_NAME = "report.csv"


# 输出行为变了但选项没变时，手动 +1 让旧产物失效（否则断点续传永远跳过、用户看不到修复）。
# 变更史：2 = 兼容码位归一化 / RTL 逻辑序还原 / 图片倾斜校正 / 结构噪声清理（bench #0–#12）
#         3 = 列表项残留项目符号剥离（"- • x" → "- x"，冒烟第 ③ 条）
PIPELINE_VERSION = 3


def options_signature(formats: list[str], options: ConvertOptions) -> str:
    """转换配置的指纹。格式或影响输出的选项变了，断点续传就该重新转换。

    只存哈希不存原文，避免把密码等敏感项写进清单。
    """
    payload = {
        "formats": sorted(formats),
        "kwargs": options.to_convert_kwargs(formats, use_ocr=False),
        "ocr_mode": options.ocr_mode,
        # RAG 分块开关/块大小变了也该重转（关着的时候块大小无关紧要）
        "rag": options.chunk_tokens if options.rag_chunks else None,
        "tables": options.export_tables,
        "qa": options.qa,
        # 只影响产物、不进底层 kwargs 的后处理开关
        "deskew": options.deskew,
        "tidy": options.tidy,
        "pipeline": PIPELINE_VERSION,
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.md5(blob.encode("utf-8")).hexdigest()


def load_manifest(out_root: Path) -> dict:
    """读断点续传清单，返回 {源文件绝对路径: 条目} 字典；没有或损坏时返回空。"""
    try:
        data = json.loads((out_root / DONE_MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    entries = data.get("entries")
    return entries if isinstance(entries, dict) else {}


def save_manifest(out_root: Path, entries: dict) -> None:
    """原子写回清单：先写临时文件再替换，转换中途被杀也不会留下半截 JSON。"""
    path = out_root / DONE_MANIFEST_NAME
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(
        json.dumps({"version": 1, "entries": entries}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    tmp.replace(path)


MAX_OCR_ATTEMPTS = 2  # 图片无文字时最多自动重试 OCR 的次数（防空白图在监听模式下每轮重转）


def should_skip(pdf: Path, entries: dict, signature: str, server_ok: bool = False) -> bool:
    """此前已成功转换、源文件（mtime+大小）和选项都没变时才跳过——兼顾增量转换。

    server_ok=True 时，此前「产物无文字」的图片（清单里 needs_ocr）不再跳过，OCR 服务一上线重跑就
    自动补转（bench #5）；已经带着 OCR 试过 MAX_OCR_ATTEMPTS 次仍无字的（ocr_attempts）视为真空白图，照常跳过。
    """
    entry = entries.get(str(pdf.resolve()))
    if not isinstance(entry, dict) or entry.get("signature") != signature:
        return False
    if server_ok and entry.get("needs_ocr") and int(entry.get("ocr_attempts", 0)) < MAX_OCR_ATTEMPTS:
        return False
    try:
        st = pdf.stat()
    except OSError:
        return False
    return entry.get("mtime") == st.st_mtime and entry.get("size") == st.st_size


def record_done(entries: dict, pdf: Path, signature: str, needs_ocr: bool = False, used_ocr: bool = False) -> None:
    """把一次成功转换记进清单（就地修改 entries）。

    needs_ocr 标记「产物无文字，OCR 服务在线时重转」；used_ocr=True 时累计 ocr_attempts
    （同一签名下连续几次带 OCR 仍无字 → 不再自动重试）。正常有字的记录会清掉这两项。
    """
    st = pdf.stat()
    key = str(pdf.resolve())
    old = entries.get(key) if isinstance(entries.get(key), dict) else {}
    entry = {
        "mtime": st.st_mtime,
        "size": st.st_size,
        "signature": signature,
        "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if needs_ocr:
        entry["needs_ocr"] = True
        attempts = int(old.get("ocr_attempts", 0)) if old.get("signature") == signature else 0
        if used_ocr:
            attempts += 1
        if attempts:
            entry["ocr_attempts"] = attempts
    entries[key] = entry


def ocr_redo_available(entries: dict, options: ConvertOptions) -> bool:
    """监听模式每轮调用：清单里有等 OCR 的图片且 OCR 未关闭时才探测服务，在线返回 True。

    只在有待补转的条目时才发请求，平时不产生额外流量。
    """
    if options.ocr_mode == "off":
        return False
    if not any(isinstance(e, dict) and e.get("needs_ocr") for e in entries.values()):
        return False
    return check_ocr_server(options.hybrid_url)


def write_report(out_root: Path, results: list[ConvertResult]) -> Path:
    """把整批结果写成 CSV 报告。utf-8-sig 编码，Excel 直接打开中文不乱码。"""
    path = out_root / REPORT_NAME
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow([tr("文件"), tr("状态"), tr("页数"), tr("产物数"), tr("耗时(秒)"), "OCR", tr("说明"), tr("源路径")])
        for r in results:
            if r.skipped:
                status = tr("跳过")
            elif r.degraded:
                status = tr("降级转换")
            else:
                status = tr("成功") if r.ok else tr("失败")
            writer.writerow([
                r.pdf.name,
                status,
                r.pages if r.pages is not None else "",
                len(r.produced),
                f"{r.duration:.1f}",
                tr("是") if r.used_ocr else "",
                r.error or r.note,
                str(r.pdf),
            ])
    return path


def execute_batch(
    pdfs: list[Path],
    out_root: Path,
    formats: list[str],
    options: ConvertOptions | None = None,
    *,
    jobs: int = 1,
    resume: bool = True,
    report: bool = True,
    merge: bool = False,
    log=print,
    progress=None,
) -> BatchSummary:
    """批量转换的统一引擎：断点续传 + 并发 + CSV 报告 + 可选合并。CLI 和 GUI 都走这里。

    log 回调收一行文本；progress 回调收 (已完成数, 总数)。
    resume=False 时不跳过任何文件，但成功记录仍会写进清单供下次续传。
    """
    options = options or ConvertOptions()
    out_root.mkdir(parents=True, exist_ok=True)

    log(tr("开始转换 {n} 个文件 -> {out}", n=len(pdfs), out=out_root))
    log(tr("输出格式：{formats}", formats=', '.join(formats)))
    extras = describe_options(options)
    if extras:
        log(tr("已启用：{extras}", extras=(', ' if i18n.current_lang() == 'en' else '、').join(extras)))
    if jobs > 1:
        log(tr("并发转换：{jobs} 个进程", jobs=jobs))

    # OCR 模式下先探测服务是否在线，不在线就明确提示（每批只查一次）
    server_ok = False
    if options.ocr_mode != "off":
        server_ok = check_ocr_server(options.hybrid_url)
        if server_ok:
            log(tr("OCR 服务在线：{url}", url=options.hybrid_url))
        else:
            log(ocr_server_hint(options.hybrid_url))

    signature = options_signature(formats, options)
    entries = load_manifest(out_root)

    results: list[ConvertResult] = []
    todo: list[Path] = []
    for pdf in pdfs:
        if resume and should_skip(pdf, entries, signature, server_ok=server_ok):
            results.append(ConvertResult(pdf=pdf, ok=True, skipped=True, note=tr("此前已完成且未变化")))
        else:
            todo.append(pdf)
    if results:
        log(tr("断点续传：跳过 {n} 个此前已完成且未变化的文件", n=len(results)))

    total = len(pdfs)
    done_count = len(results)
    if progress:
        progress(done_count, total)
    started = time.time()

    def handle(result: ConvertResult) -> None:
        """消费一个转换结果：记日志、更新清单、报进度。只在主进程里调用。"""
        nonlocal done_count
        done_count += 1
        results.append(result)
        prefix = f"[{result.note}] " if result.note else ""
        if result.ok:
            record_done(entries, result.pdf, signature, needs_ocr=result.needs_ocr, used_ocr=result.used_ocr)
            save_manifest(out_root, entries)  # 每成功一个就落盘，中断后能续传
            names = ", ".join(p.name for p in result.produced[:4])
            more = tr(" 等 {n} 个文件", n=len(result.produced)) if len(result.produced) > 4 else ""
            mark = "△" if result.degraded else "✓"  # △ = 降级/空产出，报告里状态列也是「降级转换」
            log(f"[{done_count}/{total}] {prefix}{mark} {result.pdf.name} ({result.duration:.1f}s) -> {names}{more}")
        else:
            log(tr("[{i}/{total}] {prefix}✗ {name} 失败：{error}", i=done_count, total=total, prefix=prefix, name=result.pdf.name, error=result.error))
        if progress:
            progress(done_count, total)

    if jobs <= 1 or len(todo) <= 1:
        for pdf in todo:
            handle(_convert_task(pdf, out_root, formats, options, server_ok))
    else:
        with concurrent.futures.ProcessPoolExecutor(max_workers=jobs) as pool:
            future_map = {
                pool.submit(_convert_task, pdf, out_root, formats, options, server_ok): pdf
                for pdf in todo
            }
            for future in concurrent.futures.as_completed(future_map):
                try:
                    result = future.result()
                except Exception as err:  # 工作进程崩溃等极端情况，不拖垮整批
                    result = ConvertResult(pdf=future_map[future], error=tr("工作进程异常：{err}", err=err))
                handle(result)

    elapsed = time.time() - started
    # 报告和汇总按原始输入顺序排，而不是并发完成顺序
    order = {pdf: i for i, pdf in enumerate(pdfs)}
    results.sort(key=lambda r: order.get(r.pdf, len(order)))

    report_path = None
    if report:
        try:
            report_path = write_report(out_root, results)
            log(tr("转换报告：{path}", path=report_path))
        except OSError as err:
            log(tr("转换报告写入失败：{err}", err=err))

    merged_path = None
    if merge:
        try:
            merged_path, n_merged = write_merged(out_root, results)
            if merged_path:
                log(tr("已合并 {n} 份文档 -> {path}", n=n_merged, path=merged_path))
            else:
                log(tr("没有可合并的 Markdown（合并需要 markdown 格式且至少一个成功文件）"))
        except OSError as err:
            log(tr("合并失败：{err}", err=err))

    return BatchSummary(
        total=total,
        succeeded=sum(1 for r in results if r.ok and not r.skipped),
        failed=sum(1 for r in results if not r.ok),
        degraded=sum(1 for r in results if r.ok and not r.skipped and r.degraded),
        skipped=sum(1 for r in results if r.skipped),
        elapsed=elapsed,
        results=results,
        report_path=report_path,
        merged_path=merged_path,
    )


def run_batch(
    pdfs: list[Path],
    out_root: Path,
    formats: list[str],
    options: ConvertOptions | None = None,
    *,
    jobs: int = 1,
    resume: bool = True,
    report: bool = True,
    merge: bool = False,
) -> int:
    """CLI 入口：调统一引擎并打印结果汇总。返回失败数量。"""
    print()
    summary = execute_batch(
        pdfs, out_root, formats, options,
        jobs=jobs, resume=resume, report=report, merge=merge,
    )
    print(f"\n{'=' * 60}")
    line = tr("共 {total} 个，成功 {ok} 个，失败 {bad} 个", total=summary.total, ok=summary.succeeded, bad=summary.failed)
    if summary.degraded:
        line += tr("（其中 {n} 个降级/无文字，见报告「说明」列）", n=summary.degraded)
    if summary.skipped:
        line += tr("，跳过 {n} 个", n=summary.skipped)
    print(line + tr("，耗时 {s:.1f}s", s=summary.elapsed))
    print(tr("输出目录：{out}", out=out_root))
    failures = [r for r in summary.results if not r.ok]
    if failures:
        print(tr("\n失败列表："))
        for r in failures:
            print(f"  - {r.pdf.name}: {r.error}")
    return summary.failed


def watch_loop(
    watch_dir: Path,
    out_root: Path,
    formats: list[str],
    options: ConvertOptions | None = None,
    *,
    jobs: int = 1,
    interval: float = 5.0,
    report: bool = True,
) -> int:
    """监听模式：轮询 watch_dir，新增/修改的 PDF 自动转换，Ctrl+C 退出。

    不依赖 watchdog 库：轮询 + 「连续两轮 mtime/size 不变」的稳定性检测，
    避免转到只拷贝了一半的文件；已完成的靠断点续传清单跳过，不重复劳动。
    """
    options = options or ConvertOptions()

    def echo(*parts, **kw):  # 长驻进程输出常被重定向，必须逐行 flush 才实时可见
        print(*parts, flush=True, **kw)

    # 后台启动（shell 的 & ）会把 SIGINT 继承为忽略，这里显式装回；SIGTERM 也走干净退出
    def _stop(signum, frame):
        raise KeyboardInterrupt
    with contextlib.suppress(ValueError):  # 非主线程装不了信号处理器，忽略即可
        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)

    echo(tr("监听中：{dir}（每 {interval:g}s 扫描一次，Ctrl+C 退出）", dir=watch_dir, interval=interval))
    echo(tr("输出到：{out}；格式：{formats}", out=out_root, formats=', '.join(formats)))
    extras = describe_options(options)
    if extras:
        echo(tr("已启用：{extras}", extras=(', ' if i18n.current_lang() == 'en' else '、').join(extras)))

    signature = options_signature(formats, options)
    last_seen: dict[Path, tuple[float, int]] = {}  # 上一轮扫描到的 (mtime, size)
    rounds = 0
    try:
        while True:
            entries = load_manifest(out_root)
            redo_ok = ocr_redo_available(entries, options)  # 有等 OCR 的图片且服务已上线 → 本轮补转
            ready: list[Path] = []
            for pdf in find_pdfs(watch_dir):
                try:
                    st = pdf.stat()
                except OSError:
                    continue
                stamp = (st.st_mtime, st.st_size)
                if should_skip(pdf, entries, signature, server_ok=redo_ok):
                    last_seen[pdf] = stamp
                    continue
                if last_seen.get(pdf) == stamp:
                    ready.append(pdf)  # 两轮未变，认为已写完
                else:
                    last_seen[pdf] = stamp
            if ready:
                echo("\n[" + time.strftime("%H:%M:%S") + "] " + tr("发现 {n} 个待转换文件", n=len(ready)))
                execute_batch(ready, out_root, formats, options,
                              jobs=jobs, resume=True, report=report, log=echo)
                echo("[" + time.strftime("%H:%M:%S") + "] " + tr("继续监听…（Ctrl+C 退出）"))
            rounds += 1
            time.sleep(interval)
    except KeyboardInterrupt:
        echo(tr("\n监听结束（共扫描 {n} 轮）。", n=rounds))
        return 0


# ---------------------------------------------------------------- 第四阶段：RAG 分块


def estimate_tokens(text: str) -> int:
    """粗估 token 数：CJK 每字约 1 token，其余约 4 字符 1 token。不追求精确，够切块用。"""
    cjk = sum(1 for c in text if "一" <= c <= "鿿" or "　" <= c <= "〿")
    other = len(text) - cjk
    return cjk + (other + 3) // 4


def _gather_text(node) -> str:
    """收集子树里所有 content 文本（用于表格单元格等嵌套结构）。"""
    parts: list[str] = []
    if isinstance(node, dict):
        content = node.get("content")
        if isinstance(content, str) and content.strip():
            parts.append(content.strip())
        for value in node.values():
            if isinstance(value, (dict, list)):
                parts.append(_gather_text(value))
    elif isinstance(node, list):
        for item in node:
            parts.append(_gather_text(item))
    return " ".join(p for p in parts if p)


def _table_to_markdown(node: dict) -> str:
    """把 JSON 里的 table 节点渲染成 Markdown 表格（首行当表头）。"""
    lines: list[str] = []
    for i, row in enumerate(node.get("rows") or []):
        cells = [_gather_text(cell).replace("|", "\\|") for cell in row.get("cells") or []]
        lines.append("| " + " | ".join(cells) + " |")
        if i == 0:
            lines.append("|" + " --- |" * len(cells))
    return "\n".join(lines)


def _flatten_blocks(node, blocks: list[dict]) -> None:
    """把 JSON 结构树按阅读顺序拍平成块列表：{kind, text, page, level}。"""
    if isinstance(node, list):
        for item in node:
            _flatten_blocks(item, blocks)
        return
    if not isinstance(node, dict):
        return

    node_type = node.get("type")
    page = node.get("page number")
    if node_type == "heading":
        text = (node.get("content") or "").strip()
        if text:
            level = node.get("heading level")
            blocks.append({
                "kind": "heading",
                "level": level if isinstance(level, int) and level >= 1 else 1,
                "text": text,
                "page": page,
            })
        return
    if node_type == "table":
        text = _table_to_markdown(node)
        if text:
            blocks.append({"kind": "table", "text": text, "page": page})
        return
    if node_type == "list":
        for item in node.get("list items") or []:
            text = (item.get("content") or "").strip()
            if text:
                blocks.append({"kind": "text", "text": "- " + text,
                               "page": item.get("page number") or page})
        return

    content = node.get("content")
    if isinstance(content, str) and content.strip():
        blocks.append({"kind": "text", "text": content.strip(), "page": page})
    for kid in node.get("kids") or []:
        _flatten_blocks(kid, blocks)


def _split_long_text(text: str, max_tokens: int) -> list[str]:
    """超过预算的单块按 行 → 句 → 硬切 逐级劈小。"""
    if estimate_tokens(text) <= max_tokens:
        return [text]
    segments = re.split(r"(?<=\n)|(?<=[。！？；])|(?<=[.!?] )", text)
    pieces: list[str] = []
    current = ""
    for seg in segments:
        if current and estimate_tokens(current + seg) > max_tokens:
            pieces.append(current)
            current = ""
        while estimate_tokens(seg) > max_tokens:  # 单句仍超长（CJK 最坏 1 字 1 token），硬切
            pieces.append(seg[:max_tokens])
            seg = seg[max_tokens:]
        current += seg
    if current:
        pieces.append(current)
    return [p for p in (piece.strip() for piece in pieces) if p]


def chunk_blocks(blocks: list[dict], source: str, max_tokens: int) -> list[dict]:
    """按标题层级 + token 预算切块。小节太小就与后文合并，超预算就开新块。"""
    min_tokens = max(50, max_tokens // 5)  # 块起码要这么大才值得在标题处切开
    chunks: list[dict] = []
    stack: list[tuple[int, str]] = []      # (heading level, 标题文本)
    cur_texts: list[str] = []
    cur_pages: list[int] = []
    cur_path: list[str] = []
    cur_tokens = 0

    def flush() -> None:
        nonlocal cur_texts, cur_pages, cur_tokens
        if not cur_texts:
            return
        pages = [p for p in cur_pages if isinstance(p, int)]
        chunks.append({
            "chunk": len(chunks) + 1,
            "source": source,
            "pages": [min(pages), max(pages)] if pages else None,
            "heading_path": list(cur_path),
            "tokens": cur_tokens,
            "content": "\n\n".join(cur_texts),
        })
        cur_texts, cur_pages, cur_tokens = [], [], 0

    def append_piece(text: str, page) -> None:
        nonlocal cur_tokens
        if not cur_texts:
            cur_path[:] = [title for _, title in stack]
        cur_texts.append(text)
        if isinstance(page, int):
            cur_pages.append(page)
        cur_tokens += estimate_tokens(text)

    for block in blocks:
        if block["kind"] == "heading":
            if cur_tokens >= min_tokens:  # 已积累的小节够大，在标题处开新块
                flush()
            level = block["level"]
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, block["text"]))
            append_piece("#" * min(level, 6) + " " + block["text"], block["page"])
            continue
        for piece in _split_long_text(block["text"], max_tokens):
            piece_tokens = estimate_tokens(piece)
            if cur_tokens and cur_tokens + piece_tokens > max_tokens:
                flush()
            append_piece(piece, block["page"])
    flush()
    return chunks


def write_chunks(json_path: Path, source: str, max_tokens: int) -> tuple[Path, int]:
    """从 JSON 结构树生成 RAG 分块，写成 <名>.chunks.jsonl（一行一块）。"""
    doc = json.loads(json_path.read_text(encoding="utf-8"))
    blocks: list[dict] = []
    _flatten_blocks(doc, blocks)
    chunks = chunk_blocks(blocks, source, max_tokens)
    out_path = json_path.with_name(json_path.stem + ".chunks.jsonl")
    with out_path.open("w", encoding="utf-8") as fh:
        for chunk in chunks:
            fh.write(json.dumps(chunk, ensure_ascii=False) + "\n")
    return out_path, len(chunks)


def _table_to_grid(node: dict) -> list[list[str]]:
    """把 table 节点还原成二维格子。跨行/跨列的单元格只填在锚点位置，其余留空。"""
    n_rows = node.get("number of rows") or 0
    n_cols = node.get("number of columns") or 0
    rows = node.get("rows") or []
    if not n_rows or not n_cols:  # 兜底：字段缺失时按实际内容推
        n_rows = len(rows)
        n_cols = max((len(r.get("cells") or []) for r in rows), default=0)
    grid = [["" for _ in range(n_cols)] for _ in range(n_rows)]
    for row in rows:
        for cell in row.get("cells") or []:
            r = (cell.get("row number") or 1) - 1
            c = (cell.get("column number") or 1) - 1
            if 0 <= r < n_rows and 0 <= c < n_cols:
                grid[r][c] = _gather_text(cell)
    return grid


def write_tables(json_path: Path) -> list[Path]:
    """把 JSON 树里的所有表格逐个导出为 CSV（utf-8-sig），存进 <名>_tables/ 目录。"""
    doc = json.loads(json_path.read_text(encoding="utf-8"))
    tables: list[dict] = []

    def collect(node) -> None:
        if isinstance(node, dict):
            if node.get("type") == "table":
                tables.append(node)
                return  # 不递归进表格内部（嵌套表格并入外层）
            for value in node.values():
                if isinstance(value, (dict, list)):
                    collect(value)
        elif isinstance(node, list):
            for item in node:
                collect(item)

    collect(doc)
    if not tables:
        return []
    out_dir = json_path.parent / f"{json_path.stem}_tables"
    out_dir.mkdir(exist_ok=True)
    paths: list[Path] = []
    for i, node in enumerate(tables, 1):
        page = node.get("page number")
        name = f"table_{i}" + (f"_p{page}" if isinstance(page, int) else "") + ".csv"
        path = out_dir / name
        with path.open("w", newline="", encoding="utf-8-sig") as fh:
            csv.writer(fh).writerows(_table_to_grid(node))
        paths.append(path)
    return paths


# 页字符数低于全文档中位数的这个比例时，标记为「低密度」疑似漏识别
QA_LOW_DENSITY_RATIO = 0.2


def _page_stats(doc) -> dict[int, dict]:
    """逐页统计识别出的元素数和字符数（嵌套元素也计入元素数）。"""
    stats: dict[int, dict] = {}

    def walk(node) -> None:
        if isinstance(node, dict):
            page = node.get("page number")
            if isinstance(page, int) and node.get("type"):
                entry = stats.setdefault(page, {"elements": 0, "chars": 0})
                entry["elements"] += 1
                content = node.get("content")
                if isinstance(content, str):
                    entry["chars"] += len(content.strip())
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(doc)
    return stats


def write_qa(json_path: Path) -> tuple[Path, int, int]:
    """质量自检：逐页统计并标记疑似漏识别页，写 <名>.qa.csv。

    返回 (csv 路径, 疑似页数, 总页数)。判定规则：
    无任何元素 = 空白页；有元素无文字 = 仅图片（疑似扫描页）；
    字符数低于全文档中位数 20% = 低密度。配合同批生成的标注版 PDF 人工复核。
    """
    doc = json.loads(json_path.read_text(encoding="utf-8"))
    stats = _page_stats(doc)
    total_pages = doc.get("number of pages") or max(stats, default=0)
    char_counts = [stats.get(p, {}).get("chars", 0) for p in range(1, total_pages + 1)]
    nonzero = sorted(c for c in char_counts if c > 0)
    median = nonzero[len(nonzero) // 2] if nonzero else 0

    flagged = 0
    rows: list[list] = []
    for page in range(1, total_pages + 1):
        entry = stats.get(page, {"elements": 0, "chars": 0})
        chars = entry["chars"]
        if entry["elements"] == 0:
            status = tr("空白页：无任何识别结果，若原页有内容则为漏识别")
        elif chars == 0:
            status = tr("仅图片无文字：疑似扫描页，可试 --ocr auto")
        elif median and chars < median * QA_LOW_DENSITY_RATIO:
            status = tr("低密度：仅为本文档中位数（{median} 字符/页）的 {pct}%", median=median, pct=chars * 100 // median)
        else:
            status = tr("正常")
        if status != tr("正常"):
            flagged += 1
        rows.append([page, entry["elements"], chars, status])

    path = json_path.with_name(json_path.stem + ".qa.csv")
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow([tr("页码"), tr("元素数"), tr("字符数"), tr("状态")])
        writer.writerows(rows)
    return path, flagged, total_pages


def write_merged(out_root: Path, results: list[ConvertResult]) -> tuple[Path | None, int]:
    """把本批（成功 + 断点跳过的）文档的 Markdown 按输入顺序合并成带目录的 merged.md。

    每份文档占一个一级标题，原有标题整体降一级；目录用显式 <a id> 锚点（对中文标题也可靠）。
    """
    sections: list[tuple[str, str]] = []
    for r in results:
        if not r.ok:
            continue
        dest = out_root / r.pdf.stem
        md_path = dest / f"{r.pdf.stem}.md"
        if not md_path.is_file():
            candidates = sorted(dest.glob("*.md"))
            if not candidates:
                continue  # 该文档没转 markdown，合并时跳过
            md_path = candidates[0]
        try:
            body = md_path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        body = re.sub(r"^(#{1,5}) ", r"#\1 ", body, flags=re.MULTILINE)
        sections.append((r.pdf.stem, body))
    if not sections:
        return None, 0

    lines: list[str] = ["# " + tr("合并文档"), "", tr("共 {n} 份。", n=len(sections)), "", "## " + tr("目录"), ""]
    for i, (title, _) in enumerate(sections, 1):
        lines.append(f"{i}. [{title}](#doc-{i})")
    for i, (title, body) in enumerate(sections, 1):
        lines += ["", "---", "", f'<a id="doc-{i}"></a>', "", f"# {title}", "", body]
    path = out_root / MERGED_NAME
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path, len(sections)


# ---------------------------------------------------------------- config.toml

# (小节, 键) -> (argparse 的 dest, 类型, 允许值)。resume/report 是反义开关，加载时翻转。
CONFIG_SCHEMA = {
    ("convert", "format"): ("format", str, None),
    ("convert", "output"): ("output", str, None),
    ("convert", "pages"): ("pages", str, None),
    ("convert", "images"): ("images", str, ("off", "embedded", "external")),
    ("convert", "page_markers"): ("page_markers", bool, None),
    ("convert", "better_tables"): ("better_tables", bool, None),
    ("convert", "sanitize"): ("sanitize", bool, None),
    ("convert", "header_footer"): ("header_footer", bool, None),
    ("convert", "keep_all_content"): ("keep_all_content", bool, None),
    ("convert", "deskew"): ("no_deskew", bool, None),   # 反义开关，加载时取反
    ("convert", "tidy"): ("no_tidy", bool, None),       # 同上
    ("convert", "threads"): ("threads", int, None),
    ("ocr", "mode"): ("ocr", str, ("off", "auto", "force")),
    ("ocr", "url"): ("ocr_url", str, None),
    ("batch", "jobs"): ("jobs", int, None),
    ("batch", "resume"): ("force", bool, None),      # resume=false 等价于 --force
    ("batch", "report"): ("no_report", bool, None),  # report=false 等价于 --no-report
    ("rag", "enabled"): ("rag_chunks", bool, None),
    ("rag", "chunk_size"): ("chunk_size", int, None),
    ("tables", "enabled"): ("export_tables", bool, None),
    ("merge", "enabled"): ("merge", bool, None),
    ("qa", "enabled"): ("qa", bool, None),
}
_INVERTED_CONFIG_KEYS = {("batch", "resume"), ("batch", "report"),
                         ("convert", "deskew"), ("convert", "tidy")}


def load_config(path: Path | None = None) -> tuple[dict, list[str]]:
    """读 config.toml，翻译成 argparse 默认值的覆盖字典。返回 (覆盖, 警告列表)。

    配置只当默认值用：命令行显式传参永远优先。文件不存在 = 空配置，不算错。
    """
    path = path or CONFIG_PATH
    warnings: list[str] = []
    if not path.is_file():
        return {}, warnings
    if tomllib is None:
        return {}, [tr("当前 Python 版本读不了 {name}（需要 3.11+），已忽略配置文件", name=path.name)]
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as err:
        return {}, [tr("{name} 解析失败，已忽略：{err}", name=path.name, err=err)]

    # [ui] lang 是给 i18n 的，不进 argparse；在其余小节解析（可能产生警告文案）之前生效
    ui = data.pop("ui", None)
    if isinstance(ui, dict) and "lang" in ui:
        if ui["lang"] in ("zh", "en"):
            i18n.set_lang(ui["lang"])
        else:
            warnings.append(tr("{name}：[ui] lang 只能是 zh/en，已忽略", name=path.name))

    overrides: dict = {}
    for section, table in data.items():
        if not isinstance(table, dict):
            warnings.append(tr("{name}：顶层键 {section!r} 不是配置小节，已忽略", name=path.name, section=section))
            continue
        for key, value in table.items():
            spec = CONFIG_SCHEMA.get((section, key))
            if spec is None:
                warnings.append(tr("{name}：未知配置 [{section}] {key}，已忽略", name=path.name, section=section, key=key))
                continue
            dest, expect, choices = spec
            # bool 是 int 的子类，类型检查要先挡掉 bool 混进 int
            type_ok = isinstance(value, bool) if expect is bool else (
                not isinstance(value, bool) and isinstance(value, expect)
            )
            if not type_ok:
                warnings.append(tr("{name}：[{section}] {key} 应为 {type}，已忽略", name=path.name, section=section, key=key, type=expect.__name__))
                continue
            if choices and value not in choices:
                warnings.append(tr("{name}：[{section}] {key} 只能是 {choices}，已忽略", name=path.name, section=section, key=key, choices='/'.join(choices)))
                continue
            overrides[dest] = (not value) if (section, key) in _INVERTED_CONFIG_KEYS else value
    return overrides, warnings


def build_parser() -> argparse.ArgumentParser:
    from . import __version__

    parser = argparse.ArgumentParser(
        prog="morsel",   # `python -m aimorsel` 时默认会显示 __main__.py
        description=tr("把文档（PDF/docx/xlsx/pptx/HTML/图片）转成 Markdown / JSON（基于 opendataloader-pdf）"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n\n".join([tr("不带参数运行则进入交互模式。"), subcommand_help_text()]),
    )
    parser.add_argument("--version", action="version", version=f"morsel {__version__}")
    parser.add_argument("inputs", nargs="*", help=tr("要转换的文件或文件夹"))
    parser.add_argument("-o", "--output", default=None, help=tr("输出目录（默认 {dir}）", dir=DEFAULT_OUTPUT_DIR))
    parser.add_argument(
        "-f",
        "--format",
        default="markdown,json",
        help=tr("输出格式，逗号分隔：markdown, json, html, text, pdf（默认 markdown,json）"),
    )
    parser.add_argument("-p", "--password", default=None, help=tr("加密 PDF 的密码"))

    adv = parser.add_argument_group(tr("高级选项"))
    adv.add_argument("--pages", default=None, help=tr('只转指定页码，如 "1,3,5-7"（默认全部）'))
    adv.add_argument(
        "--images",
        choices=["off", "embedded", "external"],
        default=None,
        help=tr("图片处理：off 不提取 / embedded 内嵌 Markdown / external 存独立文件（底层默认 external）"),
    )
    adv.add_argument("--page-markers", action="store_true", help=tr("在 Markdown/文本里插入分页标记"))
    adv.add_argument("--better-tables", action="store_true", help=tr("增强表格识别（cluster 模式，无边框表格更准）"))
    adv.add_argument("--sanitize", action="store_true", help=tr("脱敏：邮箱/电话/身份证/信用卡/IP 替换成占位符"))
    adv.add_argument("--header-footer", action="store_true", help=tr("保留页眉页脚（默认丢弃）"))
    adv.add_argument(
        "--keep-all-content", action="store_true",
        help=tr("关闭底层内容安全过滤：被判为隐藏/页外/微小/隐藏图层的文字也保留（怀疑内容缺失时用）"),
    )
    adv.add_argument(
        "--no-deskew", action="store_true",
        help=tr("图片输入不做倾斜校正（默认会把 0.5–5 度的轻微倾斜转正，扫描件版面才切得准）"),
    )
    adv.add_argument(
        "--no-tidy", action="store_true",
        help=tr("不清理结构噪声（默认会把单字母/关键词/文件名这类假标题降为正文、把「(1) …」条款段落从假列表还原）"),
    )
    adv.add_argument("--threads", type=int, default=None, metavar="N", help=tr("每页并行线程数，加速大文件（实验特性）"))

    ocr = parser.add_argument_group(tr("OCR 选项（处理扫描件）"))
    ocr.add_argument(
        "--ocr",
        choices=["off", "auto", "force"],
        default="auto",
        help=tr("OCR 模式：auto 自动检测扫描件才用（默认）/ off 不用 / force 全部走 OCR（auto/force 需先启动 hybrid 服务，未启动时自动降级）"),
    )
    ocr.add_argument("--ocr-url", default=DEFAULT_HYBRID_URL, help=tr("OCR 服务地址（默认 {url}）", url=DEFAULT_HYBRID_URL))
    ocr.add_argument("--setup-ocr", action="store_true",
                     help=tr("一键安装扫描件支持并启动 OCR 服务（独立环境，几个 GB，装完即用）"))
    ocr.add_argument("--setup-ocr-lang", default=None, metavar="LANG",
                     help=tr('OCR 识别语言，配合 --setup-ocr（默认 "ch_sim,en"，须匹配文档语言）'))
    ocr.add_argument("--stop-ocr", action="store_true", help=tr("停止由本工具托管的 OCR 服务"))

    batch = parser.add_argument_group(tr("批量选项"))
    batch.add_argument(
        "--jobs", type=int, default=1, metavar="N",
        help=tr("并发转换的进程数（默认 1 串行；多文件时建议设 2-4，别超过 CPU 核数）"),
    )
    batch.add_argument(
        "--force", action="store_true",
        help=tr("忽略断点续传记录，所有文件都重新转换（默认跳过已完成且未变化的文件）"),
    )
    batch.add_argument("--no-report", action="store_true", help=tr("不生成 {name} 转换报告", name=REPORT_NAME))
    batch.add_argument(
        "--watch", action="store_true",
        help=tr("监听模式：持续监视输入文件夹（默认 {dir}），新增/修改的文件自动转换，Ctrl+C 退出", dir=DEFAULT_INPUT_DIR),
    )
    batch.add_argument(
        "--watch-interval", type=float, default=5.0, metavar="SEC",
        help=tr("监听模式的扫描间隔（默认 5 秒）"),
    )

    scene = parser.add_argument_group(tr("面向场景加工"))
    scene.add_argument(
        "--rag-chunks", action="store_true",
        help=tr("按标题层级+token 预算切块，输出 <名>.chunks.jsonl（每块带页码和标题路径；会自动附带 json 格式）"),
    )
    scene.add_argument(
        "--chunk-size", type=int, default=DEFAULT_CHUNK_TOKENS, metavar="N",
        help=tr("每块的目标 token 数（估算值，默认 {n}）", n=DEFAULT_CHUNK_TOKENS),
    )
    scene.add_argument(
        "--export-tables", action="store_true",
        help=tr("把识别出的所有表格逐个导出为 CSV，存进 <名>_tables/（会自动附带 json 格式）"),
    )
    scene.add_argument(
        "--merge", action="store_true",
        help=tr("转换后把本批文档的 Markdown 按顺序合并成带目录的 {name}", name=MERGED_NAME),
    )
    scene.add_argument(
        "--qa", action="store_true",
        help=tr("质量自检：生成标注版 PDF（识别区域画彩框）+ 逐页统计 <名>.qa.csv，标记空白/低密度页（会自动附带 pdf 和 json 格式）"),
    )
    return parser


def options_from_args(args: argparse.Namespace) -> ConvertOptions:
    """把命令行参数组装成 ConvertOptions。"""
    return ConvertOptions(
        password=args.password,
        image_output=args.images,
        pages=args.pages,
        page_markers=args.page_markers,
        table_method="cluster" if args.better_tables else None,
        sanitize=args.sanitize,
        threads=args.threads,
        include_header_footer=args.header_footer,
        keep_all_content=args.keep_all_content,
        deskew=not args.no_deskew,
        tidy=not args.no_tidy,
        ocr_mode=args.ocr,
        hybrid_url=args.ocr_url,
        rag_chunks=args.rag_chunks,
        chunk_tokens=max(50, args.chunk_size),
        export_tables=args.export_tables,
        qa=args.qa,
    )


# 三个副入口用子命令暴露（官网文档写的就是 `morsel mcp`）。做成保留字而不是 argparse
# 子解析器：主命令的位置参数是「要转换的文件/目录」，加子解析器会让 `morsel a.pdf` 变成
# 非法调用（帮助的 epilog 里列了这三个子命令）。**同名的真实文件或目录优先**（`web/`、`gui/` 是很常见的目录名，不能让
# `morsel web/` 变成起服务），逃生舱是各自的独立可执行 morsel-gui / morsel-web / morsel-mcp。
SUBCOMMANDS = {"gui": "aimorsel.morsel_gui", "web": "aimorsel.morsel_web", "mcp": "aimorsel.morsel_mcp"}
SUBCOMMAND_HELP = {
    "gui": "图形界面：拖文件进窗口、勾选项、点转换",
    "web": "Web 常驻服务：浏览器上传/下载 + 监听文件夹",
    "mcp": "MCP Server：供 Claude Code 等 Agent 调用（stdio 协议，日志走 stderr）",
}


def subcommand_help_text() -> str:
    """主命令 --help 末尾列出的子命令。文案拆成短 key——tr() 的扫描器只认单个字面量，
    隐式拼接的多行字符串会被判成漏翻译。"""
    lines = [tr("子命令（当前目录下有同名文件或目录时按输入路径处理，改用 morsel-gui / morsel-web / morsel-mcp）：")]
    lines += [f"  morsel {name:<6}{tr(text)}" for name, text in SUBCOMMAND_HELP.items()]
    return "\n".join(lines)


def _dispatch_subcommand(argv: list[str]) -> int | None:
    """第一个参数是 gui/web/mcp 且没有同名的真实路径 → 转给对应入口。"""
    if not argv or argv[0] not in SUBCOMMANDS:
        return None
    name = argv[0]
    if Path(name).exists():
        # 让位给真实路径（用户大概率是想转换它），但要说清楚，否则「服务没起来」很难查
        print(tr("提示：当前目录下有同名的 {name}，按输入路径处理；要启动该功能请改用 morsel-{name}。",
                 name=name), file=sys.stderr)
        return None
    import importlib
    import inspect

    module = importlib.import_module(SUBCOMMANDS[name])   # import 在前：hiddenimports 缺了会当场报错
    rest = argv[1:]
    # 按签名分发，别拿 TypeError 当判据——那也可能是 main() 内部抛的，会被误当成「不收参数」
    if inspect.signature(module.main).parameters:
        return module.main(rest)
    if rest in (["-h"], ["--help"]):      # 不收参数的入口也得能查帮助，别报错退 2
        print(f"usage: morsel {name}\n\n{tr(SUBCOMMAND_HELP[name])}")
        return 0
    if rest:                              # gui/mcp 不收参数，别把用户给的参数悄悄吞掉
        print(tr("{cmd} 不接受额外参数：{extra}", cmd=name, extra=" ".join(rest)),
              file=sys.stderr)
        return 2
    return module.main()


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_stdio()
    sub = _dispatch_subcommand(list(sys.argv[1:] if argv is None else argv))
    if sub is not None:
        return sub
    parser = build_parser()
    config_overrides, config_warnings = load_config()
    if config_overrides:
        parser.set_defaults(**config_overrides)  # 配置只改默认值，命令行显式传参仍优先
    args = parser.parse_args(argv)

    print("=" * 60)
    print(tr("  AImorsel 文粒 · 文档 -> Markdown / JSON"))
    print("  powered by opendataloader-pdf")
    print("=" * 60)
    for warning in config_warnings:
        print(f"[config] {warning}")
    if config_overrides:
        print(tr("已加载 config.toml（{n} 项默认值）", n=len(config_overrides)))

    if args.setup_ocr or args.stop_ocr:
        from . import ocr_setup  # 函数级导入：ocr_setup 反向依赖本模块的 check_ocr_server

        try:
            if args.stop_ocr:
                ocr_setup.stop_server(print)
            else:
                ocr_setup.setup_and_start(
                    print, lang=args.setup_ocr_lang or ocr_setup.DEFAULT_LANG)
            return 0
        except RuntimeError as err:
            print(str(err))
            return 1

    if args.watch:
        # 监听模式：输入是一个文件夹（默认 raw/），持续转换新增/修改的 PDF
        watch_dir = DEFAULT_INPUT_DIR
        if args.inputs:
            candidate = Path(args.inputs[0]).expanduser()
            if not candidate.is_dir():
                print(tr("--watch 需要一个已存在的文件夹作为输入：{path}", path=candidate))
                return 1
            watch_dir = candidate
        else:
            watch_dir.mkdir(parents=True, exist_ok=True)
        if args.merge:
            print(tr("提示：监听模式按增量逐批转换，--merge 已忽略"))
        formats = [f.strip() for f in args.format.split(",") if f.strip()]
        out_root = Path(args.output).expanduser() if args.output else DEFAULT_OUTPUT_DIR
        return watch_loop(
            watch_dir, out_root, formats, options_from_args(args),
            jobs=max(1, args.jobs or 1),
            interval=max(1.0, args.watch_interval),
            report=not args.no_report,
        )

    if args.inputs:
        # 非交互模式
        pdfs: list[Path] = []
        for item in args.inputs:
            path = Path(item).expanduser()
            if not path.exists():
                print(tr("跳过不存在的路径：{path}", path=path))
                continue
            if path.is_dir():
                pdfs.extend(find_inputs(path))
            elif is_supported_input(path):
                pdfs.append(path)
            else:
                print(tr("跳过不支持的格式：{path}（支持 PDF/docx/xlsx/pptx/HTML/图片）", path=path))
        if not pdfs:
            print(tr("没有找到可转换的文件。"))
            return 1
        # 去重并保持顺序
        pdfs = list(dict.fromkeys(pdfs))
        formats = [f.strip() for f in args.format.split(",") if f.strip()]
        out_root = Path(args.output).expanduser() if args.output else DEFAULT_OUTPUT_DIR
        options = options_from_args(args)
        jobs = max(1, args.jobs or 1)
        resume = not args.force
        report = not args.no_report
        merge = args.merge
    else:
        # 交互模式
        try:
            pdfs = choose_pdfs()
            formats = choose_formats()
            out_root = choose_output_dir()
            options = choose_options()
            jobs = choose_jobs()
            merge = _ask_yes_no("转换后把本批文档合并成一份带目录的 Markdown？", default=False)
        except (KeyboardInterrupt, EOFError):
            print(tr("\n已取消。"))
            return 130
        resume = True
        report = True

    try:
        failures = run_batch(pdfs, out_root, formats, options,
                             jobs=jobs, resume=resume, report=report, merge=merge)
    except KeyboardInterrupt:
        print(tr("\n已中断。"))
        return 130
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
