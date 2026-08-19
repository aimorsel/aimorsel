#!/usr/bin/env python3
"""
AImorsel —— 文档 → Markdown / JSON 提取工具（MCP Server 版，本地私用）

把转换能力暴露成 MCP 工具，供 Claude Code 等 Agent 直接调用：
    convert_pdf        转换文件/文件夹，返回产物清单
    read_pdf_markdown  转换并直接返回 Markdown 正文（小文档一次读完）
    extract_tables     提取所有表格，返回 CSV 文本
    get_chunks         返回 RAG 分块（JSONL）
    qa_check           质量自检，返回逐页统计与疑似问题页
    get_outline        标题大纲 + 每节 token 数（大文档第一步，渐进式披露）
    get_section        按标题取某一节正文（配合 get_outline，省上下文）
    search_documents   跨已转换文档检索，返回命中段落 + 页码 + 标题路径

纯标准库实现 MCP stdio 协议（newline-delimited JSON-RPC 2.0）。
所有转换共用 output/ 的断点续传清单——重复调用同一文件秒回（缓存命中）。

注册（本项目已附 .mcp.json）：
    { "command": "<py312 的 python>", "args": ["morsel_mcp.py"] }

注意：stdout 是协议通道，任何日志只能走 stderr。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from i18n import tr
from morsel import (
    DEFAULT_OUTPUT_DIR,
    ConvertOptions,
    _flatten_blocks,
    ensure_utf8_stdio,
    estimate_tokens,
    execute_batch,
    find_pdfs,
    load_manifest,
    is_supported_input,
)

SERVER_NAME = "morsel"
SERVER_VERSION = "1.0.0"
PROTOCOL_VERSION = "2024-11-05"
MAX_TEXT = 60_000  # 单次返回文本上限（字符），超出截断并附文件路径


def log(message: str) -> None:
    print(f"[morsel-mcp] {message}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- 工具实现


def _resolve_pdfs(path_str: str) -> list[Path]:
    path = Path(path_str).expanduser()
    if path.is_dir():
        return find_pdfs(path)
    if path.is_file() and is_supported_input(path):
        return [path]
    raise ValueError(tr("路径不存在或不是支持的格式（PDF/docx/xlsx/pptx/HTML/图片）：{path}", path=path))


def _convert(pdfs: list[Path], options: ConvertOptions, formats: list[str]):
    """统一转换入口：断点续传缓存命中时秒回。返回 BatchSummary。"""
    summary = execute_batch(
        pdfs, DEFAULT_OUTPUT_DIR, formats, options,
        jobs=1, resume=True, report=False, log=log,
    )
    return summary


def _dest_file(pdf: Path, suffix: str) -> Path:
    return DEFAULT_OUTPUT_DIR / pdf.stem / f"{pdf.stem}{suffix}"


def _degraded_note(r) -> str:
    """降级/空产出说明：本次转换看 result；缓存命中时查清单里的 needs_ocr（bench #5，Agent 必须看得到「产物无文字」）。"""
    if r.skipped:
        entry = load_manifest(DEFAULT_OUTPUT_DIR).get(str(r.pdf.resolve()))
        if isinstance(entry, dict) and entry.get("needs_ocr"):
            return tr("此前转换时产物无文字：图片未经 OCR 或 OCR 未产出文字")
        return ""
    return r.note if r.degraded else ""


def _clip(text: str, source: Path) -> str:
    if len(text) <= MAX_TEXT:
        return text
    return (text[:MAX_TEXT]
            + "\n\n" + tr("…（内容过长已截断，完整文件见 {source}，共 {n} 字符）", source=source, n=len(text)))


def tool_convert_pdf(args: dict) -> str:
    pdfs = _resolve_pdfs(args["path"])
    formats = [f.strip() for f in (args.get("formats") or "markdown,json").split(",") if f.strip()]
    options = ConvertOptions(
        ocr_mode=args.get("ocr", "off"),
        pages=args.get("pages") or None,
        table_method="cluster" if args.get("better_tables") else None,
    )
    summary = _convert(pdfs, options, formats)
    if summary.degraded:
        lines = [tr("共 {total} 个：成功 {ok}（其中降级 {deg}），失败 {bad}，跳过(缓存命中) {skip}，耗时 {s:.1f}s",
                    total=summary.total, ok=summary.succeeded, deg=summary.degraded, bad=summary.failed,
                    skip=summary.skipped, s=summary.elapsed)]
    else:
        lines = [tr("共 {total} 个：成功 {ok}，失败 {bad}，跳过(缓存命中) {skip}，耗时 {s:.1f}s",
                    total=summary.total, ok=summary.succeeded, bad=summary.failed,
                    skip=summary.skipped, s=summary.elapsed)]
    for r in summary.results:
        if r.ok:
            produced = ", ".join(str(p) for p in r.produced[:6]) or tr("（沿用已有产物）")
            note = _degraded_note(r)
            mark = "△" if (r.degraded or note) else "✓"
            lines.append(f"{mark} {r.pdf.name} -> {produced}" + (f"（{note}）" if note else ""))
        else:
            lines.append(f"✗ {r.pdf.name}：{r.error}")
    return "\n".join(lines)


def tool_read_pdf_markdown(args: dict) -> str:
    pdfs = _resolve_pdfs(args["path"])
    options = ConvertOptions(ocr_mode=args.get("ocr", "off"), pages=args.get("pages") or None)
    summary = _convert(pdfs, options, ["markdown", "json"])
    parts: list[str] = []
    for r in summary.results:
        md = _dest_file(r.pdf, ".md")
        if r.ok and md.is_file():
            note = _degraded_note(r)
            head = "# " + tr("文件：{name}", name=r.pdf.name) + "\n\n"
            if note:
                head += f"> {note}\n\n"
            parts.append(head + md.read_text(encoding="utf-8"))
        else:
            parts.append("# " + tr("文件：{name}", name=r.pdf.name) + "\n\n" + tr("（转换失败：{err}）", err=r.error or tr("无 markdown 产物")))
    return _clip("\n\n---\n\n".join(parts), DEFAULT_OUTPUT_DIR)


def tool_extract_tables(args: dict) -> str:
    pdfs = _resolve_pdfs(args["path"])
    options = ConvertOptions(export_tables=True,
                             table_method="cluster" if args.get("better_tables") else None)
    summary = _convert(pdfs, options, ["json"])
    parts: list[str] = []
    for r in summary.results:
        tables_dir = DEFAULT_OUTPUT_DIR / r.pdf.stem / f"{r.pdf.stem}_tables"
        csvs = sorted(tables_dir.glob("*.csv")) if tables_dir.is_dir() else []
        if not csvs:
            parts.append(tr("{name}：未发现表格", name=r.pdf.name) + (f"（{r.error}）" if r.error else ""))
            continue
        for c in csvs:
            parts.append(f"## {r.pdf.name} / {c.name}\n{c.read_text(encoding='utf-8-sig')}")
    return _clip("\n\n".join(parts), DEFAULT_OUTPUT_DIR)


def tool_get_chunks(args: dict) -> str:
    pdfs = _resolve_pdfs(args["path"])
    options = ConvertOptions(rag_chunks=True,
                             chunk_tokens=max(50, int(args.get("chunk_size") or 400)))
    summary = _convert(pdfs, options, ["json"])
    parts: list[str] = []
    for r in summary.results:
        chunks = _dest_file(r.pdf, ".chunks.jsonl")
        if chunks.is_file():
            parts.append(chunks.read_text(encoding="utf-8").rstrip())
        else:
            parts.append(json.dumps({"source": r.pdf.name,
                                     "error": r.error or tr("无分块产物")}, ensure_ascii=False))
    return _clip("\n".join(parts), DEFAULT_OUTPUT_DIR)


def tool_qa_check(args: dict) -> str:
    pdfs = _resolve_pdfs(args["path"])
    summary = _convert(pdfs, ConvertOptions(qa=True), ["json"])
    parts: list[str] = []
    for r in summary.results:
        qa = _dest_file(r.pdf, ".qa.csv")
        note = r.note or r.error
        parts.append(f"## {r.pdf.name}" + (f"（{note}）" if note else ""))
        if qa.is_file():
            parts.append(qa.read_text(encoding="utf-8-sig").rstrip())
            annotated = _dest_file(r.pdf, "_annotated.pdf")
            if annotated.is_file():
                parts.append(tr("标注版 PDF：{path}", path=annotated))
    return _clip("\n".join(parts), DEFAULT_OUTPUT_DIR)


# ---------------------------------------------------------------- 渐进式披露（P0-2）
# 省上下文的分层取用：get_outline 看结构（几百 token）→ get_section 按需取某节
# → search_documents 跨文档定位。数据源都是 JSON 结构树。

# 结构树拍平结果的进程内缓存：{json 路径: (mtime, blocks)}。server 常驻，重复查询免重读。
_DOC_CACHE: dict[str, tuple[float, list[dict]]] = {}


def _doc_blocks(json_path: Path) -> list[dict]:
    """读 JSON 结构树并按阅读顺序拍平成块，每块带所在的标题路径（含标题块自身）。"""
    key = str(json_path)
    mtime = json_path.stat().st_mtime
    cached = _DOC_CACHE.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    doc = json.loads(json_path.read_text(encoding="utf-8"))
    raw: list[dict] = []
    _flatten_blocks(doc.get("kids") or [], raw)
    stack: list[tuple[int, str]] = []
    blocks: list[dict] = []
    for b in raw:
        if b["kind"] == "heading":
            level = b.get("level") or 1
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, b["text"]))
        blocks.append({**b, "path": [t for _, t in stack]})
    _DOC_CACHE[key] = (mtime, blocks)
    return blocks


def _section_end(blocks: list[dict], start: int) -> int:
    """从标题块 start 起，本节的结束位置（下一个同级或更高级标题，或文末）。"""
    level = blocks[start].get("level") or 1
    for j in range(start + 1, len(blocks)):
        if blocks[j]["kind"] == "heading" and (blocks[j].get("level") or 1) <= level:
            return j
    return len(blocks)


def _block_tokens(blocks: list[dict]) -> list[int]:
    return [estimate_tokens(b["text"]) for b in blocks]


def _render_blocks_md(blocks: list[dict]) -> str:
    parts = []
    for b in blocks:
        if b["kind"] == "heading":
            parts.append("#" * (b.get("level") or 1) + " " + b["text"])
        else:
            parts.append(b["text"])
    return "\n\n".join(parts)


def tool_get_outline(args: dict) -> str:
    pdfs = _resolve_pdfs(args["path"])
    # 与 read_pdf_markdown 相同的选项和格式 -> 共享同一份转换缓存
    summary = _convert(pdfs, ConvertOptions(ocr_mode=args.get("ocr", "off")), ["markdown", "json"])
    parts: list[str] = []
    for r in summary.results:
        json_path = _dest_file(r.pdf, ".json")
        if not (r.ok and json_path.is_file()):
            parts.append("# " + tr("文件：{name}", name=r.pdf.name) + "\n" + tr("（转换失败：{err}）", err=r.error or tr("无 JSON 产物")))
            continue
        blocks = _doc_blocks(json_path)
        tokens = _block_tokens(blocks)
        total = sum(tokens)
        pages = r.pages or ""
        header = ("# " + tr("文件：{name}", name=r.pdf.name)
                  + (tr("（{pages} 页，", pages=pages) if pages else "（")
                  + tr("全文约 {total} token）", total=total))
        lines = [header]
        heads = [i for i, b in enumerate(blocks) if b["kind"] == "heading"]
        if not heads:
            lines.append(tr("（未识别出标题；可用 get_chunks 分块或 read_pdf_markdown 读全文）"))
        else:
            if heads[0] > 0:  # 第一个标题之前的内容
                lines.append(tr("- （前言，p{page}，约 {n} token）", page=blocks[0].get('page') or '?', n=sum(tokens[:heads[0]])))
            min_level = min(blocks[i].get("level") or 1 for i in heads)
            for i in heads:
                level = blocks[i].get("level") or 1
                sect_tokens = sum(tokens[i:_section_end(blocks, i)])
                indent = "  " * (level - min_level)
                page = blocks[i].get("page")
                lines.append(indent + "- " + blocks[i]["text"] + tr("  [p{page}，约 {n} token]", page=page if page is not None else "?", n=sect_tokens))
        lines.append(tr("（用 get_section 按标题取正文，不必读全文）"))
        parts.append("\n".join(lines))
    return _clip("\n\n".join(parts), DEFAULT_OUTPUT_DIR)


def tool_get_section(args: dict) -> str:
    query = (args.get("heading") or "").strip()
    if not query:
        raise ValueError(tr("缺少 heading 参数（要提取的标题，可用 get_outline 先查看结构）"))
    pdfs = _resolve_pdfs(args["path"])
    summary = _convert(pdfs, ConvertOptions(ocr_mode=args.get("ocr", "off")), ["markdown", "json"])
    matches: list[tuple[Path, list[dict], int]] = []  # (源文件, blocks, 标题块下标)
    lowered = query.lower()
    for r in summary.results:
        json_path = _dest_file(r.pdf, ".json")
        if not (r.ok and json_path.is_file()):
            continue
        blocks = _doc_blocks(json_path)
        exact = [i for i, b in enumerate(blocks)
                 if b["kind"] == "heading" and b["text"].strip().lower() == lowered]
        fuzzy = [i for i, b in enumerate(blocks)
                 if b["kind"] == "heading" and lowered in b["text"].lower()]
        for i in (exact or fuzzy):
            matches.append((r.pdf, blocks, i))
    if not matches:
        return tr("没有找到标题「{query}」。先用 get_outline 查看文档结构，再用完整标题取节。", query=query)
    if len(matches) > 3:
        lines = [tr("标题「{query}」命中 {n} 处，请用更完整的标题精确定位：", query=query, n=len(matches))]
        for pdf, blocks, i in matches[:20]:
            path = " > ".join(blocks[i]["path"]) or blocks[i]["text"]
            lines.append(f"- {pdf.name} p{blocks[i].get('page')}：{path}")
        return "\n".join(lines)
    parts: list[str] = []
    for pdf, blocks, i in matches:
        section = blocks[i:_section_end(blocks, i)]
        first_page, last_page = section[0].get("page"), section[-1].get("page")
        span = f"p{first_page}" + (f"-{last_page}" if last_page not in (None, first_page) else "")
        label = " > ".join(blocks[i]["path"]) or blocks[i]["text"]
        parts.append(tr("（{name}，{span}，约 {n} token）路径：{label}", name=pdf.name, span=span, n=sum(_block_tokens(section)), label=label) + "\n\n"
                     + _render_blocks_md(section))
    return _clip("\n\n---\n\n".join(parts), DEFAULT_OUTPUT_DIR)


def tool_search_documents(args: dict) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        raise ValueError(tr("缺少 query 参数（检索词，空格分隔多个词为 AND）"))
    limit = max(1, min(int(args.get("limit") or 10), 50))
    terms = [t.lower() for t in query.split() if t]

    if args.get("path"):  # 指定范围：需要时先转换
        pdfs = _resolve_pdfs(args["path"])
        summary = _convert(pdfs, ConvertOptions(), ["markdown", "json"])
        json_paths = [(_dest_file(r.pdf, ".json"), r.pdf.name)
                      for r in summary.results if r.ok]
    else:  # 默认搜全部已转换文档，不触发新转换
        json_paths = [(d / f"{d.name}.json", d.name)
                      for d in sorted(DEFAULT_OUTPUT_DIR.iterdir())
                      if d.is_dir() and (d / f"{d.name}.json").is_file()]
    if not json_paths:
        return tr("还没有已转换的文档（output/ 里没有 JSON 产物）。先用 convert_pdf 转换，或在 path 参数里给出要搜的文件。")

    hits: list[tuple[int, str, dict]] = []  # (得分, 文档名, 块)
    for json_path, doc_name in json_paths:
        try:
            blocks = _doc_blocks(json_path)
        except (OSError, json.JSONDecodeError):
            continue
        for b in blocks:
            text_lower = b["text"].lower()
            if all(t in text_lower for t in terms):
                hits.append((sum(text_lower.count(t) for t in terms), doc_name, b))
    if not hits:
        return tr("在 {n} 份已转换文档中没有命中「{query}」。", n=len(json_paths), query=query)
    hits.sort(key=lambda h: -h[0])
    lines = [tr("共命中 {hits} 块（按相关度显示前 {shown}；共搜索 {docs} 份文档）：", hits=len(hits), shown=min(limit, len(hits)), docs=len(json_paths))]
    for rank, (score, doc_name, b) in enumerate(hits[:limit], 1):
        snippet = " ".join(b["text"].split())
        pos = snippet.lower().find(terms[0])
        start = max(0, pos - 80)
        clip = ("…" if start else "") + snippet[start:start + 240] + ("…" if start + 240 < len(snippet) else "")
        path = " > ".join(b["path"])
        lines.append(f"{rank}. {doc_name}  p{b.get('page')}" + (f"  ｜ {path}" if path else ""))
        lines.append(f"   {clip}")
    lines.append(tr("（用 get_section 取完整小节，或 read_pdf_markdown 读整篇）"))
    return _clip("\n".join(lines), DEFAULT_OUTPUT_DIR)


_PATH_PROP = {"type": "string",
              "description": tr("文件（PDF/docx/xlsx/pptx/HTML/图片）或包含这些文件的文件夹的绝对路径")}
_OCR_PROP = {"type": "string", "enum": ["off", "auto", "force"],
             "description": tr("OCR 模式（默认 off；扫描件需先启动 hybrid 服务）")}

TOOLS = {
    "convert_pdf": {
        "handler": tool_convert_pdf,
        "description": tr("把文档（PDF/docx/xlsx/pptx/HTML/图片）转换成 Markdown/JSON/HTML/文本，返回产物文件清单。已转换过且未变化的文件自动跳过（缓存）。"),
        "schema": {"type": "object", "required": ["path"], "properties": {
            "path": _PATH_PROP,
            "formats": {"type": "string", "description": tr("输出格式，逗号分隔：markdown,json,html,text（默认 markdown,json）")},
            "pages": {"type": "string", "description": tr('只转指定页码，如 "1,3,5-7"')},
            "ocr": _OCR_PROP,
            "better_tables": {"type": "boolean", "description": tr("增强表格识别（无边框表格更准）")},
        }},
    },
    "read_pdf_markdown": {
        "handler": tool_read_pdf_markdown,
        "description": tr("转换文档（PDF/docx/xlsx/pptx/HTML/图片）并直接返回 Markdown 正文内容（小文档一次读完合适；大文档建议先 get_outline 再 get_section，省上下文。过长会截断并给出完整文件路径）。"),
        "schema": {"type": "object", "required": ["path"], "properties": {
            "path": _PATH_PROP,
            "pages": {"type": "string", "description": tr('只读指定页码，如 "1-5"')},
            "ocr": _OCR_PROP,
        }},
    },
    "extract_tables": {
        "handler": tool_extract_tables,
        "description": tr("提取文档（PDF/docx/xlsx/pptx/HTML）里的所有表格，以 CSV 文本返回（含页码）。"),
        "schema": {"type": "object", "required": ["path"], "properties": {
            "path": _PATH_PROP,
            "better_tables": {"type": "boolean", "description": tr("增强表格识别")},
        }},
    },
    "get_chunks": {
        "handler": tool_get_chunks,
        "description": tr("把文档（PDF/docx/xlsx/pptx/HTML）切成 RAG 分块并返回 JSONL：每块带页码范围、标题路径、token 估算。"),
        "schema": {"type": "object", "required": ["path"], "properties": {
            "path": _PATH_PROP,
            "chunk_size": {"type": "integer", "description": tr("每块目标 token 数（默认 400）")},
        }},
    },
    "qa_check": {
        "handler": tool_qa_check,
        "description": tr("对文档转换结果做质量自检：逐页元素/字符统计，标记空白页、疑似扫描页、低密度页。"),
        "schema": {"type": "object", "required": ["path"], "properties": {"path": _PATH_PROP}},
    },
    "get_outline": {
        "handler": tool_get_outline,
        "description": tr("返回文档的标题大纲（层级树 + 每节页码和 token 数，只花几百 token）。读大文档的第一步：先看结构，再用 get_section 按需取内容，别直接读全文。"),
        "schema": {"type": "object", "required": ["path"], "properties": {
            "path": _PATH_PROP,
            "ocr": _OCR_PROP,
        }},
    },
    "get_section": {
        "handler": tool_get_section,
        "description": tr("按标题提取文档某一节的正文（含子节，Markdown 格式）。标题支持模糊匹配；配合 get_outline 使用，只取需要的部分、省上下文。"),
        "schema": {"type": "object", "required": ["path", "heading"], "properties": {
            "path": _PATH_PROP,
            "heading": {"type": "string", "description": tr("要提取的标题（可只写一部分，大小写不敏感）")},
            "ocr": _OCR_PROP,
        }},
    },
    "search_documents": {
        "handler": tool_search_documents,
        "description": tr("跨已转换文档全文检索，返回命中段落 + 页码 + 标题路径（按相关度排序）。默认搜 output/ 里全部已转换文档；给 path 参数可限定范围（会自动转换）。"),
        "schema": {"type": "object", "required": ["query"], "properties": {
            "query": {"type": "string", "description": tr("检索词，空格分隔多个词表示同时出现（AND）")},
            "path": _PATH_PROP,
            "limit": {"type": "integer", "description": tr("最多返回的命中数（默认 10，上限 50）")},
        }},
    },
}


# ---------------------------------------------------------------- MCP stdio 协议


def _respond(msg_id, result=None, error=None) -> None:
    payload: dict = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def handle(message: dict) -> None:
    method = message.get("method")
    msg_id = message.get("id")
    if method == "initialize":
        _respond(msg_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
    elif method == "notifications/initialized":
        pass  # 通知无需回复
    elif method == "tools/list":
        _respond(msg_id, {"tools": [
            {"name": name, "description": spec["description"], "inputSchema": spec["schema"]}
            for name, spec in TOOLS.items()
        ]})
    elif method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        spec = TOOLS.get(name)
        if spec is None:
            _respond(msg_id, error={"code": -32602, "message": f"未知工具：{name}"})
            return
        try:
            text = spec["handler"](params.get("arguments") or {})
            _respond(msg_id, {"content": [{"type": "text", "text": text}], "isError": False})
        except Exception as err:  # 工具错误按 MCP 规范放 result 里，不作协议错误
            _respond(msg_id, {"content": [{"type": "text", "text": f"出错：{err}"}], "isError": True})
    elif method == "ping":
        _respond(msg_id, {})
    elif msg_id is not None:  # 未知请求按协议报错；未知通知直接忽略
        _respond(msg_id, error={"code": -32601, "message": f"不支持的方法：{method}"})


def main() -> int:
    ensure_utf8_stdio()  # MCP 协议按 UTF-8 走 stdout，Windows 默认编码会坏
    log(f"启动（输出目录 {DEFAULT_OUTPUT_DIR}）")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            log(f"收到非 JSON 行，忽略：{line[:120]}")
            continue
        try:
            handle(message)
        except Exception as err:  # 协议层兜底，服务不能因单条消息崩溃
            log(f"处理消息异常：{err}")
            if message.get("id") is not None:
                _respond(message["id"], error={"code": -32603, "message": str(err)})
    log("stdin 关闭，退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
