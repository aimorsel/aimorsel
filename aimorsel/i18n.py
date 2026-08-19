#!/usr/bin/env python3
"""轻量界面多语言：中文原文就是 key，英文在 `_EN` 字典里。

用法：
    from .i18n import tr
    print(tr("开始转换 {n} 个文件", n=3))

设计取舍（改代码前先读）：
- **中文原文即 key**：不发明几百个 message id，代码里中文照旧可读；
  英文缺条目时原样返回中文（兜底不崩），补翻译只改本文件。
- **占位符统一用 str.format 具名参数**：tr() 带 kwargs 时先查表再 format。
  代码里不要再写 f-string 拼用户可见文案，否则没法进查表流程。
- 语言解析优先级：环境变量 `MORSEL_LANG`（zh/en）> config.toml `[ui] lang`
  （由 morsel.load_config 调 set_lang 注入）> 系统 locale（zh* 认中文，其余英文）。
- 测试一律固定 `MORSEL_LANG=zh`（tests/conftest.py），断言不受 CI 机器 locale 影响。
- `tests/test_i18n.py` 会扫源码里全部 tr() 的中文 key，逐条断言 `_EN` 有翻译——
  新增 tr() 字符串忘了补翻译会直接红。
"""

from __future__ import annotations

import locale
import os

_lang: str | None = None          # None = 尚未解析
_config_lang: str | None = None   # 来自 config.toml 的值（次高优先级）


def set_lang(lang: str | None) -> None:
    """由配置加载方注入 config.toml 的语言设置（"zh"/"en"，其他值忽略）。"""
    global _config_lang, _lang
    if lang in ("zh", "en"):
        _config_lang = lang
        _lang = None  # 让下次 current_lang() 重新解析


def current_lang() -> str:
    global _lang
    if _lang is None:
        env = os.environ.get("MORSEL_LANG", "").strip().lower()
        if env in ("zh", "en"):
            _lang = env
        elif _config_lang:
            _lang = _config_lang
        else:
            try:
                loc = locale.getlocale()[0] or os.environ.get("LANG", "")
            except ValueError:
                loc = os.environ.get("LANG", "")
            _lang = "zh" if (loc or "").lower().startswith("zh") else "en"
    return _lang


def tr(text: str, **kwargs) -> str:
    """翻译并填充占位符。key 未收录时原文返回（中文兜底）。"""
    if current_lang() == "en":
        text = _EN.get(text, text)
    return text.format(**kwargs) if kwargs else text


def note_sep() -> str:
    """多条 note 拼接用的分隔符：中文全角「；」，英文「; 」。"""
    return "; " if current_lang() == "en" else "；"


# ---------------------------------------------------------------- 英文文案表
# 按来源文件分组维护；新增 tr() 后在这里补对应条目（test_i18n 会检查完整性）。

_EN: dict[str, str] = {
    # ---- 常量/分隔符 ----
    "**— 第 %page-number% 页 —**": "**— Page %page-number% —**",
    "第 %page-number% 页": "Page %page-number%",
    # ---- 格式预设（FORMAT_PRESETS/FORMAT_CHOICES 标签，经 tr(label) 间接调用）----
    "Markdown + JSON（默认）": "Markdown + JSON (default)",
    "仅 Markdown": "Markdown only",
    "仅 JSON": "JSON only",
    "全部（Markdown + JSON + HTML + 纯文本）": "All (Markdown + JSON + HTML + plain text)",
    "纯文本": "Plain text",
    # ---- CLI 交互 ----
    "无法识别的范围: {chunk}": "Invalid range: {chunk}",
    "无法识别的编号: {chunk}": "Invalid number: {chunk}",
    "编号 {n} 超出范围 (1-{total})": "Number {n} out of range (1-{total})",
    "\n在 {dir} 找到 {n} 个可转换文件：\n": "\nFound {n} convertible file(s) in {dir}:\n",
    "\n请输入编号选择（如 1 / 1,3 / 1-3 / all），": "\nPick by number (e.g. 1 / 1,3 / 1-3 / all),",
    "或直接把文件、文件夹拖进来后回车：": "or drag files/folders here and press Enter:",
    "\n{dir} 里没有找到可转换的文件（支持 PDF/docx/xlsx/pptx/HTML/图片）。": "\nNo convertible files in {dir} (supported: PDF/docx/xlsx/pptx/HTML/images).",
    "请把文件或文件夹拖进终端后回车（也可直接粘贴路径）：": "Drag a file or folder into the terminal and press Enter (or paste a path):",
    "没有输入，请重试（Ctrl+C 退出）。": "Nothing entered — try again (Ctrl+C to quit).",
    "输入有误：{err}，请重试。": "Invalid input: {err} — try again.",
    "路径不存在：{path}，请重试。": "Path does not exist: {path} — try again.",
    "{path} 里没有可转换的文件，请重试。": "No convertible files in {path} — try again.",
    "该文件夹下找到 {n} 个可转换文件，将全部转换。": "Found {n} convertible file(s) in that folder — converting all.",
    "{name} 不是支持的格式（PDF/docx/xlsx/pptx/HTML/图片），请重试。": "{name} is not a supported format (PDF/docx/xlsx/pptx/HTML/images) — try again.",
    "\n选择输出格式：": "\nChoose output formats:",
    "> 直接回车用默认 [1]: ": "> Press Enter for default [1]: ",
    "无法识别 '{answer}'，改用默认 Markdown + JSON。": "Unrecognized '{answer}' — using default Markdown + JSON.",
    "已选择：{label}": "Selected: {label}",
    "\n输出目录（回车用默认 {dir}）：": "\nOutput directory (Enter for default {dir}):",
    "\n高级选项（想跳过就一路回车）：": "\nAdvanced options (press Enter to skip any):",
    "> 只转指定页码？如 1,3,5-7（回车=全部）: ": "> Convert specific pages only? e.g. 1,3,5-7 (Enter = all): ",
    "> 并发转换进程数？多文件时可加速（回车=1 串行）: ": "> Parallel worker processes? Speeds up big batches (Enter = 1): ",
    "无法识别，按串行处理。": "Unrecognized — running sequentially.",
    # ---- 是非题（经 _ask_yes_no 间接调用）----
    "提取图片存为独立文件？": "Extract images as separate files?",
    "在 Markdown/文本里插入分页标记？": "Insert page markers into Markdown/text?",
    "增强表格识别（无边框表格更准，稍慢）？": "Enhanced table detection (better for borderless tables, slower)?",
    "对邮箱/电话/身份证等做脱敏？": "Redact emails/phones/IDs?",
    "自动检测扫描件并用 OCR？（默认开启，需先启动 hybrid 服务）": "Auto-detect scanned files and use OCR? (default on; requires the hybrid service)",
    "输出 RAG 分块（chunks.jsonl，供大模型/知识库用）？": "Output RAG chunks (chunks.jsonl, for LLMs/knowledge bases)?",
    "把识别出的表格导出为 CSV？": "Export detected tables as CSV?",
    "转换后把本批文档合并成一份带目录的 Markdown？": "Merge this batch into one Markdown with a table of contents?",
    "质量自检（标注版 PDF + 逐页统计）？": "Quality check (annotated PDF + per-page stats)?",
    # ---- 错误/OCR 决策 ----
    "底层转换失败（退出码 {code}）": "Engine conversion failed (exit code {code})",
    "OCR 服务未启动（{url}），扫描件将按普通模式转换（可能为空）。": "OCR service is not running ({url}); scanned files will convert without OCR (possibly empty).",
    "  启动办法：": "  To start it: ",
    "图片输入：OCR 服务未启动，仅提取版面（无文字）": "Image input: OCR service not running — layout only, no text",
    "产物无文字内容：图片未经 OCR，OCR 服务上线后重跑会自动重转": "Output has no text: image was not OCR'd — rerun after starting the OCR service and it will be reconverted automatically",
    "产物无文字内容：图片未经 OCR（OCR 已关闭）": "Output has no text: image was not OCR'd (OCR is off)",
    "OCR 未产出文字（后端失败或未识别），产物无文字内容；服务正常后重跑会再试一次": "OCR produced no text (backend failure or nothing recognized) — output has no text; rerun once the service is healthy and it will be retried",
    "此前转换时产物无文字：图片未经 OCR 或 OCR 未产出文字": "previous conversion produced no text: image was not OCR'd or OCR found nothing",
    "降级": "degraded",
    "共 {total} 个：成功 {ok}（其中降级 {deg}），失败 {bad}，跳过(缓存命中) {skip}，耗时 {s:.1f}s": "{total} file(s): {ok} succeeded ({deg} degraded), {bad} failed, {skip} skipped (cache hits), {s:.1f}s",
    "图片输入，走 OCR": "Image input — using OCR",
    "OCR 服务未启动，按普通模式转换": "OCR service not running — converting without OCR",
    "强制 OCR": "OCR forced",
    "无法探测文字密度，按普通模式转换": "Could not probe text density — converting without OCR",
    "疑似扫描件（每页约 {density:.0f} 字符），启用 OCR": "Looks scanned (~{density:.0f} chars/page) — using OCR",
    "降级转换（pdfplumber 纯文本）：{error}": "Degraded conversion (pdfplumber plain text): {error}",
    "PDF 结构损坏，已修复后转换：{error}": "PDF structure was damaged; repaired (qpdf) and converted: {error}",
    "分块/表格/QA 需结构树，降级输出不适用": "Chunks/tables/QA need the structure tree — unavailable for degraded output",
    "转换未产生任何输出文件": "Conversion produced no output files",
    # ---- 后处理 ----
    "没有 JSON 产物可供分块": "No JSON output available for chunking",
    "修正 {n} 处兼容码位": "Normalized {n} compatibility code points",
    "还原 {n} 行 RTL 文本的逻辑序": "Restored logical order of {n} RTL text lines",
    "RAG 分块 {n} 块": "{n} RAG chunk(s)",
    "RAG 分块失败：{err}": "RAG chunking failed: {err}",
    "没有 JSON 产物可供提取表格": "No JSON output available for table export",
    "导出 {n} 个表格": "Exported {n} table(s)",
    "未发现表格": "No tables found",
    "表格导出失败：{err}": "Table export failed: {err}",
    "没有 JSON 产物可供质检": "No JSON output available for QA",
    "QA：{flagged}/{total} 页疑似需复核": "QA: {flagged}/{total} page(s) may need review",
    "QA：{total} 页均正常": "QA: all {total} page(s) look fine",
    "质量自检失败：{err}": "Quality check failed: {err}",
    # ---- describe_options ----
    "页码范围 {pages}": "pages {pages}",
    "不提取图片": "no images",
    "图片内嵌 Markdown": "images embedded in Markdown",
    "图片存为独立文件": "images as separate files",
    "插入分页标记": "page markers",
    "增强表格识别": "enhanced tables",
    "敏感信息脱敏": "sanitize",
    "{n} 线程并行": "{n} threads",
    "保留页眉页脚": "keep headers/footers",
    "关闭内容安全过滤": "content filtering off",
    "不做倾斜校正": "no deskew",
    "不清理结构噪声": "no structure tidying",
    "整理 {n} 处结构噪声": "Tidied {n} structure artifact(s)",
    "不清理结构噪声（默认会把单字母/关键词/文件名这类假标题降为正文、把「(1) …」条款段落从假列表还原）":
        "Skip structure tidying (by default bogus headings such as single letters, keywords and file names are demoted to body text, and \"(1) ...\" clause paragraphs are restored from fake lists)",
    "已校正 {angle} 度倾斜": "deskewed by {angle} degrees",
    "图片输入不做倾斜校正（默认会把 0.5–5 度的轻微倾斜转正，扫描件版面才切得准）":
        "Skip deskewing image input (by default a 0.5-5 degree tilt is corrected, which the layout analysis needs)",
    "OCR 已关闭": "OCR off",
    "RAG 分块（约 {n} token/块）": "RAG chunks (~{n} tokens each)",
    "表格导出 CSV": "table export CSV",
    "质量自检": "quality check",
    # ---- 报告 ----
    "文件": "File",
    "状态": "Status",
    "页数": "Pages",
    "产物数": "Outputs",
    "耗时(秒)": "Duration (s)",
    "说明": "Note",
    "源路径": "Source path",
    "跳过": "Skipped",
    "降级转换": "Degraded",
    "成功": "OK",
    "失败": "Failed",
    "是": "yes",
    # ---- 批量引擎 ----
    "开始转换 {n} 个文件 -> {out}": "Converting {n} file(s) -> {out}",
    "输出格式：{formats}": "Output formats: {formats}",
    "已启用：{extras}": "Enabled: {extras}",
    "并发转换：{jobs} 个进程": "Parallel workers: {jobs}",
    "OCR 服务在线：{url}": "OCR service online: {url}",
    "此前已完成且未变化": "already converted, unchanged",
    "断点续传：跳过 {n} 个此前已完成且未变化的文件": "Resume: skipping {n} file(s) already converted and unchanged",
    " 等 {n} 个文件": " and {n} file(s) total",
    "[{i}/{total}] {prefix}✗ {name} 失败：{error}": "[{i}/{total}] {prefix}✗ {name} failed: {error}",
    "工作进程异常：{err}": "Worker process error: {err}",
    "转换报告：{path}": "Report: {path}",
    "转换报告写入失败：{err}": "Failed to write report: {err}",
    "已合并 {n} 份文档 -> {path}": "Merged {n} document(s) -> {path}",
    "没有可合并的 Markdown（合并需要 markdown 格式且至少一个成功文件）": "Nothing to merge (needs markdown format and at least one successful file)",
    "合并失败：{err}": "Merge failed: {err}",
    "共 {total} 个，成功 {ok} 个，失败 {bad} 个": "{total} file(s): {ok} succeeded, {bad} failed",
    "（其中 {n} 个降级/无文字，见报告「说明」列）": " ({n} degraded/no-text — see the Note column of the report)",
    "，跳过 {n} 个": ", {n} skipped",
    "，耗时 {s:.1f}s": ", {s:.1f}s",
    "输出目录：{out}": "Output directory: {out}",
    "\n失败列表：": "\nFailures:",
    # ---- watch ----
    "监听中：{dir}（每 {interval:g}s 扫描一次，Ctrl+C 退出）": "Watching {dir} (every {interval:g}s, Ctrl+C to quit)",
    "输出到：{out}；格式：{formats}": "Output: {out}; formats: {formats}",
    "发现 {n} 个待转换文件": "Found {n} file(s) to convert",
    "继续监听…（Ctrl+C 退出）": "Watching… (Ctrl+C to quit)",
    "\n监听结束（共扫描 {n} 轮）。": "\nStopped watching (scanned {n} round(s)).",
    # ---- QA ----
    "空白页：无任何识别结果，若原页有内容则为漏识别": "Blank: nothing detected — if the page has content, it was missed",
    "仅图片无文字：疑似扫描页，可试 --ocr auto": "Image only, no text: likely scanned — try --ocr auto",
    "低密度：仅为本文档中位数（{median} 字符/页）的 {pct}%": "Low density: only {pct}% of this document's median ({median} chars/page)",
    "正常": "OK",
    "页码": "Page",
    "元素数": "Elements",
    "字符数": "Characters",
    # ---- 合并 ----
    "合并文档": "Merged Documents",
    "共 {n} 份。": "{n} document(s).",
    "目录": "Contents",
    # ---- config ----
    "当前 Python 版本读不了 {name}（需要 3.11+），已忽略配置文件": "This Python cannot read {name} (needs 3.11+) — config ignored",
    "{name} 解析失败，已忽略：{err}": "Failed to parse {name} — ignored: {err}",
    "{name}：[ui] lang 只能是 zh/en，已忽略": "{name}: [ui] lang must be zh/en — ignored",
    "{name}：顶层键 {section!r} 不是配置小节，已忽略": "{name}: top-level key {section!r} is not a section — ignored",
    "{name}：未知配置 [{section}] {key}，已忽略": "{name}: unknown option [{section}] {key} — ignored",
    "{name}：[{section}] {key} 应为 {type}，已忽略": "{name}: [{section}] {key} should be {type} — ignored",
    "{name}：[{section}] {key} 只能是 {choices}，已忽略": "{name}: [{section}] {key} must be one of {choices} — ignored",
    # ---- argparse ----
    "把文档（PDF/docx/xlsx/pptx/HTML/图片）转成 Markdown / JSON（基于 opendataloader-pdf）": "Convert documents (PDF/docx/xlsx/pptx/HTML/images) to Markdown / JSON (powered by opendataloader-pdf)",
    "不带参数运行则进入交互模式。": "Run without arguments for interactive mode.",
    "要转换的文件或文件夹": "files or folders to convert",
    "输出目录（默认 {dir}）": "output directory (default {dir})",
    "输出格式，逗号分隔：markdown, json, html, text, pdf（默认 markdown,json）": "output formats, comma-separated: markdown, json, html, text, pdf (default markdown,json)",
    "加密 PDF 的密码": "password for encrypted PDFs",
    "高级选项": "Advanced options",
    "只转指定页码，如 \"1,3,5-7\"（默认全部）": "convert only these pages, e.g. \"1,3,5-7\" (default: all)",
    "图片处理：off 不提取 / embedded 内嵌 Markdown / external 存独立文件（底层默认 external）": "image handling: off / embedded in Markdown / external files (engine default: external)",
    "在 Markdown/文本里插入分页标记": "insert page markers into Markdown/text",
    "增强表格识别（cluster 模式，无边框表格更准）": "enhanced table detection (cluster mode, better for borderless tables)",
    "脱敏：邮箱/电话/身份证/信用卡/IP 替换成占位符": "redact emails/phones/IDs/credit cards/IPs with placeholders",
    "保留页眉页脚（默认丢弃）": "keep headers and footers (dropped by default)",
    "关闭底层内容安全过滤：被判为隐藏/页外/微小/隐藏图层的文字也保留（怀疑内容缺失时用）": "disable content filtering: keep text judged hidden/off-page/tiny (use when content seems missing)",
    "每页并行线程数，加速大文件（实验特性）": "per-page parallel threads for large files (experimental)",
    "OCR 选项（处理扫描件）": "OCR options (for scanned documents)",
    "OCR 模式：auto 自动检测扫描件才用（默认）/ off 不用 / force 全部走 OCR（auto/force 需先启动 hybrid 服务，未启动时自动降级）": "OCR mode: auto = detect scanned files (default) / off / force (auto/force need the hybrid service; degrades gracefully when absent)",
    "OCR 服务地址（默认 {url}）": "OCR service URL (default {url})",
    "批量选项": "Batch options",
    "并发转换的进程数（默认 1 串行；多文件时建议设 2-4，别超过 CPU 核数）": "parallel worker processes (default 1; try 2-4 for many files, at most your CPU cores)",
    "忽略断点续传记录，所有文件都重新转换（默认跳过已完成且未变化的文件）": "ignore resume records and reconvert everything (default skips unchanged completed files)",
    "不生成 {name} 转换报告": "do not write the {name} report",
    "监听模式：持续监视输入文件夹（默认 {dir}），新增/修改的文件自动转换，Ctrl+C 退出": "watch mode: monitor the input folder (default {dir}), convert new/changed files, Ctrl+C to quit",
    "监听模式的扫描间隔（默认 5 秒）": "watch polling interval (default 5s)",
    "面向场景加工": "AI-oriented processing",
    "按标题层级+token 预算切块，输出 <名>.chunks.jsonl（每块带页码和标题路径；会自动附带 json 格式）": "split by heading hierarchy + token budget into <name>.chunks.jsonl (each chunk has pages and heading path; json format auto-added)",
    "每块的目标 token 数（估算值，默认 {n}）": "target tokens per chunk (estimated, default {n})",
    "把识别出的所有表格逐个导出为 CSV，存进 <名>_tables/（会自动附带 json 格式）": "export every detected table as CSV into <name>_tables/ (json format auto-added)",
    "转换后把本批文档的 Markdown 按顺序合并成带目录的 {name}": "merge the batch's Markdown into {name} with a table of contents",
    "质量自检：生成标注版 PDF（识别区域画彩框）+ 逐页统计 <名>.qa.csv，标记空白/低密度页（会自动附带 pdf 和 json 格式）": "quality check: annotated PDF + per-page stats in <name>.qa.csv, flags blank/low-density pages (pdf and json formats auto-added)",
    "  AImorsel 文粒 · 文档 -> Markdown / JSON": "  AImorsel · Document -> Markdown / JSON",
    "已加载 config.toml（{n} 项默认值）": "Loaded config.toml ({n} default(s))",
    "--watch 需要一个已存在的文件夹作为输入：{path}": "--watch needs an existing folder as input: {path}",
    "提示：监听模式按增量逐批转换，--merge 已忽略": "Note: watch mode converts incrementally — --merge ignored",
    "跳过不存在的路径：{path}": "Skipping nonexistent path: {path}",
    "跳过不支持的格式：{path}（支持 PDF/docx/xlsx/pptx/HTML/图片）": "Skipping unsupported format: {path} (supported: PDF/docx/xlsx/pptx/HTML/images)",
    "没有找到可转换的文件。": "No convertible files found.",
    "\n已取消。": "\nCancelled.",
    "\n已中断。": "\nInterrupted.",
    # ---- GUI ----
    "文档 → Markdown / JSON 转换工具": "Document → Markdown / JSON Converter",
    "文档 → Markdown / JSON": "Document → Markdown / JSON",
    "把文件或文件夹拖到这里": "Drop files or folders here",
    "当前环境不支持拖拽（pip install tkinterdnd2 可启用）": "Drag & drop unavailable (pip install tkinterdnd2 to enable)",
    "或点击下方「添加文件」按钮": "or use the \"Add files\" button below",
    "待转换文件": "Files to convert",
    "添加文件": "Add files",
    "添加文件夹": "Add folder",
    "移除选中": "Remove selected",
    "清空": "Clear",
    "共 {n} 个文件": "{n} file(s)",
    "输出设置": "Output settings",
    "格式：": "Formats: ",
    "输出到：": "Output to: ",
    "更改…": "Change…",
    "页码范围：": "Pages: ",
    "留空=全部，如 1,3,5-7": "empty = all, e.g. 1,3,5-7",
    "图片：": "Images: ",
    "存独立文件": "Separate files",
    "内嵌 Markdown": "Embedded",
    "不提取": "Off",
    "关闭内容过滤": "Content filter off",
    "关闭": "Off",
    "自动检测扫描件": "Auto-detect scans",
    "全部强制": "Force all",
    "需先启动 hybrid 服务": "requires the hybrid service",
    "并发进程：": "Workers: ",
    "跳过已转换过且未变化的文件（断点续传）": "Skip unchanged already-converted files (resume)",
    "RAG 分块（输出 chunks.jsonl）": "RAG chunks (chunks.jsonl)",
    "块大小(约 token)：": "Chunk size (~tokens): ",
    "合并为单个 Markdown（带目录）": "Merge into one Markdown (with TOC)",
    "开始转换": "Convert",
    "打开输出文件夹": "Open output folder",
    "转换日志": "Log",
    "{name}（文件夹内没有可转换的文件）": "{name} (no convertible files inside)",
    "{name}（不支持的格式）": "{name} (unsupported format)",
    "已添加 {n} 个文件": "Added {n} file(s)",
    "已跳过 {item}": "Skipped {item}",
    "选择要转换的文件": "Choose files to convert",
    "支持的文件": "Supported files",
    "PDF 文件": "PDF files",
    "Office 文档": "Office documents",
    "网页": "Web pages",
    "图片": "Images",
    "所有文件": "All files",
    "选择要转换的文件夹": "Choose a folder to convert",
    "选择输出目录": "Choose output directory",
    "提示": "Note",
    "输出目录还不存在，先转换一次吧。": "The output directory doesn't exist yet — run a conversion first.",
    "没有文件": "No files",
    "请先拖入或添加要转换的文件。": "Add or drop some files to convert first.",
    "没有选择格式": "No format selected",
    "请至少勾选一种输出格式。": "Tick at least one output format.",
    "转换中…": "Converting…",
    "转换过程异常：{err}": "Conversion error: {err}",
    "转换完成（有失败）": "Done (with failures)",
    "详情见日志区。": "See the log for details.",
    "转换完成": "Done",
    # ---- Web ----
    "默认设置": "defaults",
    "文档转换服务": "Document Conversion Service",
    "文档 → Markdown / JSON 转换服务": "Document → Markdown / JSON Service",
    "加载中…": "Loading…",
    "上传文件": "Upload files",
    "（PDF / docx / xlsx / pptx / HTML / 图片，保存到监听目录后自动转换）": " (PDF / docx / xlsx / pptx / HTML / images — saved to the watched folder and converted automatically)",
    "上传": "Upload",
    "最近转换": "Recent conversions",
    "实时日志": "Live log",
    "输出文件": "Output files",
    "监听：": "Watching: ",
    "输出：": "Output: ",
    "选项：": "Options: ",
    "并发：": "Workers: ",
    "、": ", ",
    "空闲": "idle",
    "耗时": "Duration",
    "大小": "Size",
    "上传中…": "Uploading…",
    "文件过大或为空": "File too large or empty",
    "网页上传 {n} 个文件 -> {dir}": "Web upload: {n} file(s) -> {dir}",
    "已上传 {n} 个文件，稍候自动转换": "Uploaded {n} file(s) — conversion starts shortly",
    "没有可接收的文件（支持 PDF/docx/xlsx/pptx/HTML/图片）": "No accepted files (supported: PDF/docx/xlsx/pptx/HTML/images)",
    "文档转换常驻服务（Web 界面 + 文件夹监听）": "Document conversion service (web UI + folder watching)",
    "监听地址（默认仅本机 127.0.0.1）": "bind address (default 127.0.0.1, local only)",
    "端口（默认 8008）": "port (default 8008)",
    "被监听的文件夹（默认 {dir}）": "watched folder (default {dir})",
    "扫描间隔秒数（默认 5）": "polling interval in seconds (default 5)",
    "已加载 config.toml（{n} 项）": "Loaded config.toml ({n} option(s))",
    "服务启动": "Service started",
    "文档转换服务已启动：{url}": "Document conversion service running at {url}",
    "监听目录：{dir}（新文件自动转换，Ctrl+C 退出）": "Watching {dir} (new files convert automatically, Ctrl+C to quit)",
    "服务已停止。": "Service stopped.",
    # ---- MCP ----
    "路径不存在或不是支持的格式（PDF/docx/xlsx/pptx/HTML/图片）：{path}": "Path does not exist or unsupported format (PDF/docx/xlsx/pptx/HTML/images): {path}",
    "…（内容过长已截断，完整文件见 {source}，共 {n} 字符）": "… (truncated; full file at {source}, {n} characters total)",
    "共 {total} 个：成功 {ok}，失败 {bad}，跳过(缓存命中) {skip}，耗时 {s:.1f}s": "{total} file(s): {ok} succeeded, {bad} failed, {skip} skipped (cache hits), {s:.1f}s",
    "（沿用已有产物）": "(reusing existing outputs)",
    "文件：{name}": "File: {name}",
    "（转换失败：{err}）": "(conversion failed: {err})",
    "无 markdown 产物": "no markdown output",
    "{name}：未发现表格": "{name}: no tables found",
    "无分块产物": "no chunks output",
    "标注版 PDF：{path}": "Annotated PDF: {path}",
    "无 JSON 产物": "no JSON output",
    "（{pages} 页，": "({pages} pages, ",
    "全文约 {total} token）": "~{total} tokens total)",
    "（未识别出标题；可用 get_chunks 分块或 read_pdf_markdown 读全文）": "(no headings detected; use get_chunks or read_pdf_markdown instead)",
    "- （前言，p{page}，约 {n} token）": "- (preamble, p{page}, ~{n} tokens)",
    "  [p{page}，约 {n} token]": "  [p{page}, ~{n} tokens]",
    "（用 get_section 按标题取正文，不必读全文）": "(use get_section to fetch a section by heading instead of reading everything)",
    "缺少 heading 参数（要提取的标题，可用 get_outline 先查看结构）": "Missing 'heading' argument (the heading to extract; run get_outline first to see the structure)",
    "没有找到标题「{query}」。先用 get_outline 查看文档结构，再用完整标题取节。": "Heading \"{query}\" not found. Run get_outline to see the structure, then use the full heading.",
    "标题「{query}」命中 {n} 处，请用更完整的标题精确定位：": "Heading \"{query}\" matched {n} places — use a more specific heading:",
    "（{name}，{span}，约 {n} token）路径：{label}": "({name}, {span}, ~{n} tokens) path: {label}",
    "缺少 query 参数（检索词，空格分隔多个词为 AND）": "Missing 'query' argument (search terms; space-separated terms are ANDed)",
    "还没有已转换的文档（output/ 里没有 JSON 产物）。先用 convert_pdf 转换，或在 path 参数里给出要搜的文件。": "No converted documents yet (no JSON outputs in output/). Convert with convert_pdf first, or pass 'path' to search specific files.",
    "在 {n} 份已转换文档中没有命中「{query}」。": "No hits for \"{query}\" across {n} converted document(s).",
    "共命中 {hits} 块（按相关度显示前 {shown}；共搜索 {docs} 份文档）：": "{hits} hit(s) (showing top {shown} by relevance; searched {docs} document(s)):",
    "（用 get_section 取完整小节，或 read_pdf_markdown 读整篇）": "(use get_section for a full section, or read_pdf_markdown for the whole document)",
    "文件（PDF/docx/xlsx/pptx/HTML/图片）或包含这些文件的文件夹的绝对路径": "Absolute path to a file (PDF/docx/xlsx/pptx/HTML/image) or a folder containing them",
    "OCR 模式（默认 off；扫描件需先启动 hybrid 服务）": "OCR mode (default off; scanned files need the hybrid service running)",
    "把文档（PDF/docx/xlsx/pptx/HTML/图片）转换成 Markdown/JSON/HTML/文本，返回产物文件清单。已转换过且未变化的文件自动跳过（缓存）。": "Convert documents (PDF/docx/xlsx/pptx/HTML/images) to Markdown/JSON/HTML/text and list the outputs. Unchanged already-converted files are skipped (cached).",
    "输出格式，逗号分隔：markdown,json,html,text（默认 markdown,json）": "Output formats, comma-separated: markdown,json,html,text (default markdown,json)",
    "只转指定页码，如 \"1,3,5-7\"": "Convert only these pages, e.g. \"1,3,5-7\"",
    "增强表格识别（无边框表格更准）": "Enhanced table detection (better for borderless tables)",
    "转换文档（PDF/docx/xlsx/pptx/HTML/图片）并直接返回 Markdown 正文内容（小文档一次读完合适；大文档建议先 get_outline 再 get_section，省上下文。过长会截断并给出完整文件路径）。": "Convert a document (PDF/docx/xlsx/pptx/HTML/image) and return its Markdown body. Fine for small documents; for large ones prefer get_outline then get_section to save context. Long output is truncated with the full file path.",
    "只读指定页码，如 \"1-5\"": "Read only these pages, e.g. \"1-5\"",
    "提取文档（PDF/docx/xlsx/pptx/HTML）里的所有表格，以 CSV 文本返回（含页码）。": "Extract every table from a document (PDF/docx/xlsx/pptx/HTML) and return them as CSV text with page numbers.",
    "把文档（PDF/docx/xlsx/pptx/HTML）切成 RAG 分块并返回 JSONL：每块带页码范围、标题路径、token 估算。": "Split a document (PDF/docx/xlsx/pptx/HTML) into RAG chunks and return JSONL: each chunk has page range, heading path and token estimate.",
    "每块目标 token 数（默认 400）": "Target tokens per chunk (default 400)",
    "对文档转换结果做质量自检：逐页元素/字符统计，标记空白页、疑似扫描页、低密度页。": "Quality-check a conversion: per-page element/character stats, flags blank, likely-scanned and low-density pages.",
    "返回文档的标题大纲（层级树 + 每节页码和 token 数，只花几百 token）。读大文档的第一步：先看结构，再用 get_section 按需取内容，别直接读全文。": "Return the document's heading outline (tree with per-section pages and token counts — only a few hundred tokens). First step for large documents: inspect the structure, then fetch sections with get_section instead of reading everything.",
    "按标题提取文档某一节的正文（含子节，Markdown 格式）。标题支持模糊匹配；配合 get_outline 使用，只取需要的部分、省上下文。": "Extract one section (including subsections) by heading, as Markdown. Headings are fuzzy-matched; pair with get_outline to fetch only what you need and save context.",
    "要提取的标题（可只写一部分，大小写不敏感）": "The heading to extract (partial match OK, case-insensitive)",
    "跨已转换文档全文检索，返回命中段落 + 页码 + 标题路径（按相关度排序）。默认搜 output/ 里全部已转换文档；给 path 参数可限定范围（会自动转换）。": "Full-text search across converted documents; hits include the passage, page number and heading path, sorted by relevance. Searches everything in output/ by default; pass 'path' to scope (converts as needed).",
    "检索词，空格分隔多个词表示同时出现（AND）": "Search terms; space-separated terms must all appear (AND)",
    "最多返回的命中数（默认 10，上限 50）": "Maximum hits to return (default 10, cap 50)",
    # ---- OCR 一键安装 ----
    "一键安装扫描件支持并启动 OCR 服务（独立环境，几个 GB，装完即用）": "one-click install of scanned-document support and start the OCR service (isolated env, several GB)",
    'OCR 识别语言，配合 --setup-ocr（默认 "ch_sim,en"，须匹配文档语言）': 'OCR languages for --setup-ocr (default "ch_sim,en"; must match your documents)',
    "停止由本工具托管的 OCR 服务": "stop the OCR service managed by this tool",
    "启用扫描件支持": "Enable scanned-document support",
    "启用扫描件支持…": "Enable scanned-document support…",
    "OCR 服务：安装/启动中…（进度见实时日志）": "OCR service: installing/starting… (see live log)",
    "已开始安装/启动，进度见实时日志": "Installation started — progress in the live log",
    "安装已在进行中": "Installation already in progress",
    "创建独立环境：{dir}": "Creating isolated environment: {dir}",
    "开始安装 {package}（几个 GB，视网速需要几分钟到几十分钟）…": "Installing {package} (several GB — minutes to tens of minutes depending on bandwidth)…",
    "依赖安装失败（pip 退出码 {code}），完整输出见终端": "Dependency installation failed (pip exit code {code}); see terminal for full output",
    "安装完成但没有找到 opendataloader-pdf-hybrid，可能安装不完整": "Install finished but opendataloader-pdf-hybrid not found — installation may be incomplete",
    "依赖安装完成。": "Dependencies installed.",
    "OCR 服务已在线：{url}": "OCR service already online: {url}",
    "尚未安装扫描件支持（找不到 {exe}）": "Scanned-document support not installed ({exe} not found)",
    "启动 OCR 服务（端口 {port}，识别语言 {lang}）…": "Starting OCR service (port {port}, languages {lang})…",
    "服务日志：{path}": "Service log: {path}",
    "OCR 服务已就绪：{url}（之后转换时选 auto/force 模式即可使用）": "OCR service ready: {url} (use auto/force OCR mode in conversions)",
    "OCR 服务启动失败（进程已退出），请查看日志：{path}": "OCR service failed to start (process exited); see log: {path}",
    "仍在启动（首次需加载/下载模型，可能较慢）…": "Still starting (first run loads/downloads models, can be slow)…",
    "等待服务上线超时（{n}s），请查看日志：{path}": "Timed out waiting for the service ({n}s); see log: {path}",
    "没有由本工具托管的 OCR 服务在运行。": "No OCR service managed by this tool is running.",
    "OCR 服务已停止（pid {pid}）。": "OCR service stopped (pid {pid}).",
    "扫描件支持已安装，直接启动服务。": "Scanned-document support already installed — starting the service.",
    "OCR 服务：在线": "OCR service: online",
    "OCR 服务：已安装，未运行": "OCR service: installed, not running",
    "OCR 服务：未安装": "OCR service: not installed",
    # ---- 子命令 ----（SUBCOMMAND_HELP 的值经 tr(变量) 调用，扫描器抓不到，手工维护）
    "子命令（当前目录下有同名文件或目录时按输入路径处理，改用 morsel-gui / morsel-web / morsel-mcp）：":
        "Subcommands (a file or directory of the same name in the current directory wins; "
        "use morsel-gui / morsel-web / morsel-mcp instead):",
    "提示：当前目录下有同名的 {name}，按输入路径处理；要启动该功能请改用 morsel-{name}。":
        "Note: a {name} exists in the current directory, so it is treated as input; "
        "run morsel-{name} to start that instead.",
    "图形界面：拖文件进窗口、勾选项、点转换": "Graphical interface: drag files in, tick options, convert",
    "Web 常驻服务：浏览器上传/下载 + 监听文件夹":
        "Resident web service: upload/download in the browser + folder watching",
    "MCP Server：供 Claude Code 等 Agent 调用（stdio 协议，日志走 stderr）":
        "MCP server: for Claude Code and other agents (stdio protocol, logs on stderr)",
    # ---- 品牌 ----（英文界面不带中文名）
    "AImorsel 文粒 — 文档 → Markdown / JSON": "AImorsel — Document to Markdown / JSON",
    "AImorsel 文粒 — 文档转换服务": "AImorsel — Document conversion service",
    "AImorsel 文粒 · 文档 → Markdown / JSON": "AImorsel · Document → Markdown / JSON",
    "检测到更名前的 OCR 环境 {old}，本版本改用 {new}。":
        "Found an OCR environment from before the rename at {old}; this version uses {new}.",
    "整个搬过去即可，不必重新下载：mv {old} {new}":
        "Move the whole directory over instead of downloading again: mv {old} {new}",
    "OCR 服务：未安装（检测到旧版目录，可直接搬过来，详见日志）":
        "OCR service: not installed (found a pre-rename directory — it can be moved over; see log)",
    "尚未安装扫描件支持（{dir} 里没有装好的环境）":
        "Scanned-document support is not installed (no environment in {dir})",
    "OCR 服务启动失败：{err}\n可尝试重新安装：morsel --setup-ocr":
        "Failed to start the OCR service: {err}\nTry reinstalling: morsel --setup-ocr",
    "{cmd} 不接受额外参数：{extra}": "{cmd} takes no extra arguments: {extra}",
    # ---- 适配器 ----
    "缺少依赖 {package}，请运行: pip install {package}": "Missing dependency {package} — run: pip install {package}",
    "解析 {name} 失败：{err}": "Failed to parse {name}: {err}",
    "不支持的格式：{suffix}": "Unsupported format: {suffix}",
    "（演讲者备注）": "(Speaker notes) ",
    "读取图片 {name} 失败：{err}": "Failed to read image {name}: {err}",
}
