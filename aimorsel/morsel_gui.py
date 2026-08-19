#!/usr/bin/env python3
"""
AImorsel —— 文档 → Markdown / JSON 提取工具（图形界面版）

支持把 PDF 文件或文件夹直接拖进窗口，选择输出格式和输出目录后一键转换。

用法:
    python morsel_gui.py
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .i18n import tr
from .morsel import (
    DEFAULT_CHUNK_TOKENS,
    DEFAULT_HYBRID_URL,
    DEFAULT_INPUT_DIR,
    DEFAULT_OUTPUT_DIR,
    INPUT_EXTENSIONS,
    BatchSummary,
    ConvertOptions,
    ensure_utf8_stdio,
    execute_batch,
    find_inputs,
    human_size,
    is_supported_input,
    load_config,
)

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD

    DND_AVAILABLE = True
except ImportError:
    DND_AVAILABLE = False

# 界面上可勾选的输出格式
FORMAT_CHOICES = [
    ("Markdown", "markdown", True),
    ("JSON", "json", True),
    ("HTML", "html", False),
    ("纯文本", "text", False),
]


class ConverterApp:
    def __init__(self, root: tk.Misc) -> None:
        self.root = root
        self.pdfs: list[Path] = []
        self.output_dir = DEFAULT_OUTPUT_DIR
        self.events: queue.Queue = queue.Queue()
        self.running = False

        root.title(tr("AImorsel 文粒 — 文档 → Markdown / JSON"))
        # 高度随屏幕自适应：内容完整展开约需 960px，小屏上放不下时
        # 底部锚定的「开始转换」行仍然可见（见 _build_ui 的 pack 顺序）
        height = min(max(720, root.winfo_screenheight() - 120), 980)
        root.geometry(f"760x{height}")
        root.minsize(660, 620)

        # config.toml 只提供控件初始值，之后界面上的改动优先
        self.config, self.config_warnings = load_config()

        self._build_ui()
        self._apply_config()
        self._poll_events()

    # ---------------------------------------------------------------- UI 构建

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=14)
        outer.pack(fill="both", expand=True)

        ttk.Label(
            outer,
            text=tr("AImorsel 文粒 · 文档 → Markdown / JSON"),
            font=("Helvetica", 18, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            outer,
            text="powered by opendataloader-pdf",
            foreground="#888",
        ).pack(anchor="w", pady=(0, 10))

        # 日志和操作行从底部锚定（先 pack 的贴最底），保证窗口不够高时
        # 「开始转换」按钮永远可见，被压缩的是中间的文件列表区
        self._build_log(outer)
        self._build_actions(outer)
        self._build_drop_zone(outer)
        self._build_file_list(outer)
        self._build_options(outer)

    def _build_drop_zone(self, parent: ttk.Frame) -> None:
        zone = tk.Frame(
            parent,
            height=96,
            bg="#f2f4f7",
            highlightbackground="#b9c0cc",
            highlightthickness=2,
        )
        zone.pack(fill="x", pady=(0, 10))
        zone.pack_propagate(False)

        hint = (
            tr("把文件或文件夹拖到这里")
            if DND_AVAILABLE
            else tr("当前环境不支持拖拽（pip install tkinterdnd2 可启用）")
        )
        self.drop_label = tk.Label(
            zone,
            text=hint + "\n" + tr("或点击下方「添加文件」按钮"),
            bg="#f2f4f7",
            fg="#5a6472",
            justify="center",
        )
        self.drop_label.pack(expand=True)

        if DND_AVAILABLE:
            for widget in (zone, self.drop_label):
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", self._on_drop)

    def _build_file_list(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text=tr("待转换文件"), padding=8)
        frame.pack(fill="both", expand=True, pady=(0, 10))

        row = ttk.Frame(frame)
        row.pack(fill="both", expand=True)

        self.listbox = tk.Listbox(row, selectmode="extended", height=7, activestyle="none")
        self.listbox.pack(side="left", fill="both", expand=True)

        scroll = ttk.Scrollbar(row, orient="vertical", command=self.listbox.yview)
        scroll.pack(side="right", fill="y")
        self.listbox.config(yscrollcommand=scroll.set)

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(buttons, text=tr("添加文件"), command=self._add_files).pack(side="left")
        ttk.Button(buttons, text=tr("添加文件夹"), command=self._add_folder).pack(side="left", padx=6)
        ttk.Button(buttons, text=tr("移除选中"), command=self._remove_selected).pack(side="left")
        ttk.Button(buttons, text=tr("清空"), command=self._clear_files).pack(side="left", padx=6)

        self.count_label = ttk.Label(buttons, text=tr("共 {n} 个文件", n=0), foreground="#666")
        self.count_label.pack(side="right")

    def _build_options(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text=tr("输出设置"), padding=8)
        frame.pack(fill="x", pady=(0, 10))

        fmt_row = ttk.Frame(frame)
        fmt_row.pack(fill="x")
        ttk.Label(fmt_row, text=tr("格式：")).pack(side="left")

        self.format_vars: dict[str, tk.BooleanVar] = {}
        for label, value, default_on in FORMAT_CHOICES:
            var = tk.BooleanVar(value=default_on)
            self.format_vars[value] = var
            ttk.Checkbutton(fmt_row, text=tr(label), variable=var).pack(side="left", padx=(0, 12))

        dir_row = ttk.Frame(frame)
        dir_row.pack(fill="x", pady=(10, 0))
        ttk.Label(dir_row, text=tr("输出到：")).pack(side="left")
        self.output_label = ttk.Label(dir_row, text=str(self.output_dir), foreground="#2a5db0")
        self.output_label.pack(side="left", padx=(0, 8))
        ttk.Button(dir_row, text=tr("更改…"), command=self._choose_output).pack(side="right")

        self._build_advanced(frame)

    def _build_advanced(self, parent: ttk.Frame) -> None:
        """高级选项区：对应第一阶段放出来的底层能力。"""
        ttk.Separator(parent, orient="horizontal").pack(fill="x", pady=10)

        # 页码范围
        pages_row = ttk.Frame(parent)
        pages_row.pack(fill="x")
        ttk.Label(pages_row, text=tr("页码范围：")).pack(side="left")
        self.pages_var = tk.StringVar()
        ttk.Entry(pages_row, textvariable=self.pages_var, width=18).pack(side="left")
        ttk.Label(pages_row, text=tr('留空=全部，如 1,3,5-7'), foreground="#888").pack(side="left", padx=(8, 0))

        # 图片处理
        img_row = ttk.Frame(parent)
        img_row.pack(fill="x", pady=(8, 0))
        ttk.Label(img_row, text=tr("图片：")).pack(side="left")
        self.image_var = tk.StringVar(value="external")
        for label, value in [(tr("存独立文件"), "external"), (tr("内嵌 Markdown"), "embedded"), (tr("不提取"), "off")]:
            ttk.Radiobutton(img_row, text=label, value=value, variable=self.image_var).pack(side="left", padx=(0, 10))

        # 开关类选项
        toggles = ttk.Frame(parent)
        toggles.pack(fill="x", pady=(8, 0))
        self.page_markers_var = tk.BooleanVar(value=False)
        self.better_tables_var = tk.BooleanVar(value=False)
        self.sanitize_var = tk.BooleanVar(value=False)
        self.header_footer_var = tk.BooleanVar(value=False)
        self.keep_all_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(toggles, text=tr("插入分页标记"), variable=self.page_markers_var).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(toggles, text=tr("增强表格识别"), variable=self.better_tables_var).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(toggles, text=tr("敏感信息脱敏"), variable=self.sanitize_var).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(toggles, text=tr("保留页眉页脚"), variable=self.header_footer_var).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(toggles, text=tr("关闭内容过滤"), variable=self.keep_all_var).pack(side="left", padx=(0, 12))

        # OCR（扫描件）
        ocr_row = ttk.Frame(parent)
        ocr_row.pack(fill="x", pady=(8, 0))
        ttk.Label(ocr_row, text="OCR：").pack(side="left")
        self.ocr_var = tk.StringVar(value="auto")
        for label, value in [(tr("关闭"), "off"), (tr("自动检测扫描件"), "auto"), (tr("全部强制"), "force")]:
            ttk.Radiobutton(ocr_row, text=label, value=value, variable=self.ocr_var).pack(side="left", padx=(0, 10))
        self.ocr_setup_btn = ttk.Button(ocr_row, text=tr("启用扫描件支持…"),
                                        command=self._setup_ocr)
        self.ocr_setup_btn.pack(side="left", padx=(8, 0))
        self.ocr_status_label = ttk.Label(ocr_row, text="", foreground="#888")
        self.ocr_status_label.pack(side="left", padx=(8, 0))
        self._refresh_ocr_status()

        # 批量工程化（第三阶段）：并发 + 断点续传
        batch_row = ttk.Frame(parent)
        batch_row.pack(fill="x", pady=(8, 0))
        ttk.Label(batch_row, text=tr("并发进程：")).pack(side="left")
        self.jobs_var = tk.IntVar(value=1)
        ttk.Spinbox(batch_row, from_=1, to=8, width=4, textvariable=self.jobs_var).pack(side="left")
        self.resume_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            batch_row,
            text=tr("跳过已转换过且未变化的文件（断点续传）"),
            variable=self.resume_var,
        ).pack(side="left", padx=(16, 0))

        # 面向场景加工（第四阶段）
        rag_row = ttk.Frame(parent)
        rag_row.pack(fill="x", pady=(8, 0))
        self.rag_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(rag_row, text=tr("RAG 分块（输出 chunks.jsonl）"), variable=self.rag_var).pack(side="left")
        ttk.Label(rag_row, text=tr("块大小(约 token)：")).pack(side="left", padx=(16, 0))
        self.chunk_var = tk.StringVar(value=str(DEFAULT_CHUNK_TOKENS))
        ttk.Entry(rag_row, textvariable=self.chunk_var, width=6).pack(side="left")

        scene_row = ttk.Frame(parent)
        scene_row.pack(fill="x", pady=(8, 0))
        self.tables_var = tk.BooleanVar(value=False)
        self.merge_var = tk.BooleanVar(value=False)
        self.qa_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(scene_row, text=tr("表格导出 CSV"), variable=self.tables_var).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(scene_row, text=tr("合并为单个 Markdown（带目录）"), variable=self.merge_var).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(scene_row, text=tr("质量自检"), variable=self.qa_var).pack(side="left")

    def _apply_config(self) -> None:
        """把 config.toml 的值套到控件初始状态上。"""
        for warning in self.config_warnings:
            self._log(f"[config] {warning}")
        cfg = self.config
        if not cfg:
            return
        if "format" in cfg:
            wanted = {f.strip() for f in cfg["format"].split(",") if f.strip()}
            for value, var in self.format_vars.items():
                var.set(value in wanted)
        if "output" in cfg:
            self.output_dir = Path(cfg["output"]).expanduser()
            self.output_label.config(text=str(self.output_dir))
        if "pages" in cfg:
            self.pages_var.set(cfg["pages"])
        if "images" in cfg:
            self.image_var.set(cfg["images"])
        for dest, var in [
            ("page_markers", self.page_markers_var),
            ("better_tables", self.better_tables_var),
            ("sanitize", self.sanitize_var),
            ("header_footer", self.header_footer_var),
            ("keep_all_content", self.keep_all_var),
            ("rag_chunks", self.rag_var),
            ("export_tables", self.tables_var),
            ("merge", self.merge_var),
            ("qa", self.qa_var),
        ]:
            if dest in cfg:
                var.set(cfg[dest])
        if "ocr" in cfg:
            self.ocr_var.set(cfg["ocr"])
        if "jobs" in cfg:
            self.jobs_var.set(max(1, cfg["jobs"]))
        if "force" in cfg:
            self.resume_var.set(not cfg["force"])
        if "chunk_size" in cfg:
            self.chunk_var.set(str(cfg["chunk_size"]))
        self._log(tr("已加载 config.toml（{n} 项默认值）", n=len(cfg)))

    def _collect_options(self) -> ConvertOptions:
        """从界面控件读出当前的转换选项。"""
        try:
            chunk_tokens = max(50, int(self.chunk_var.get().strip() or DEFAULT_CHUNK_TOKENS))
        except ValueError:
            chunk_tokens = DEFAULT_CHUNK_TOKENS
        return ConvertOptions(
            image_output=self.image_var.get(),
            pages=self.pages_var.get().strip() or None,
            page_markers=self.page_markers_var.get(),
            table_method="cluster" if self.better_tables_var.get() else None,
            sanitize=self.sanitize_var.get(),
            threads=self.config.get("threads") or None,  # GUI 没有线程控件，只从 config 取
            include_header_footer=self.header_footer_var.get(),
            keep_all_content=self.keep_all_var.get(),
            ocr_mode=self.ocr_var.get(),
            hybrid_url=self.config.get("ocr_url", DEFAULT_HYBRID_URL),
            rag_chunks=self.rag_var.get(),
            chunk_tokens=chunk_tokens,
            export_tables=self.tables_var.get(),
            qa=self.qa_var.get(),
        )

    def _build_actions(self, parent: ttk.Frame) -> None:
        frame = ttk.Frame(parent)
        frame.pack(side="bottom", fill="x", pady=(0, 10))

        self.convert_btn = ttk.Button(frame, text=tr("开始转换"), command=self._start_conversion)
        self.convert_btn.pack(side="left")
        self.open_btn = ttk.Button(frame, text=tr("打开输出文件夹"), command=self._open_output)
        self.open_btn.pack(side="left", padx=8)

        self.progress = ttk.Progressbar(frame, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=(12, 0))

    def _build_log(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text=tr("转换日志"), padding=8)
        frame.pack(side="bottom", fill="both", expand=True)

        self.log_text = tk.Text(frame, height=8, wrap="word", state="disabled", font=("Menlo", 11))
        self.log_text.pack(side="left", fill="both", expand=True)

        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.log_text.yview)
        scroll.pack(side="right", fill="y")
        self.log_text.config(yscrollcommand=scroll.set)

    # ------------------------------------------------------------ 文件列表操作

    def _add_paths(self, paths: list[Path]) -> None:
        """把文件/文件夹展开成待转换列表并去重加入。"""
        added = 0
        skipped: list[str] = []
        for path in paths:
            if path.is_dir():
                found = find_inputs(path)
                if not found:
                    skipped.append(tr("{name}（文件夹内没有可转换的文件）", name=path.name))
                for pdf in found:
                    if pdf not in self.pdfs:
                        self.pdfs.append(pdf)
                        added += 1
            elif is_supported_input(path):
                if path not in self.pdfs:
                    self.pdfs.append(path)
                    added += 1
            else:
                skipped.append(tr("{name}（不支持的格式）", name=path.name))

        self._refresh_list()
        if added:
            self._log(tr("已添加 {n} 个文件", n=added))
        for item in skipped:
            self._log(tr("已跳过 {item}", item=item))

    def _refresh_list(self) -> None:
        self.listbox.delete(0, "end")
        for pdf in self.pdfs:
            try:
                size = human_size(pdf.stat().st_size)
            except OSError:
                size = "?"
            self.listbox.insert("end", f"{pdf.name}   ({size})   —   {pdf.parent}")
        self.count_label.config(text=tr("共 {n} 个文件", n=len(self.pdfs)))

    def _on_drop(self, event) -> None:
        # tkinterdnd2 传回的是 Tcl 列表字符串，带空格的路径用 {} 包裹
        paths = [Path(p).expanduser() for p in self.root.tk.splitlist(event.data)]
        self._add_paths([p for p in paths if p.exists()])

    def _add_files(self) -> None:
        initial = DEFAULT_INPUT_DIR if DEFAULT_INPUT_DIR.is_dir() else Path.home()
        selected = filedialog.askopenfilenames(
            title=tr("选择要转换的文件"),
            initialdir=str(initial),
            filetypes=[
                (tr("支持的文件"), " ".join(f"*{ext}" for ext in sorted(INPUT_EXTENSIONS))),
                (tr("PDF 文件"), "*.pdf"),
                (tr("Office 文档"), "*.docx *.xlsx *.pptx"),
                (tr("网页"), "*.html *.htm *.xhtml"),
                (tr("图片"), "*.png *.jpg *.jpeg *.tif *.tiff *.bmp *.webp *.gif"),
                (tr("所有文件"), "*.*"),
            ],
        )
        if selected:
            self._add_paths([Path(p) for p in selected])

    def _add_folder(self) -> None:
        initial = DEFAULT_INPUT_DIR if DEFAULT_INPUT_DIR.is_dir() else Path.home()
        folder = filedialog.askdirectory(title=tr("选择要转换的文件夹"), initialdir=str(initial))
        if folder:
            self._add_paths([Path(folder)])

    def _remove_selected(self) -> None:
        for index in sorted(self.listbox.curselection(), reverse=True):
            del self.pdfs[index]
        self._refresh_list()

    def _clear_files(self) -> None:
        self.pdfs.clear()
        self._refresh_list()

    # ---------------------------------------------------------------- 输出目录

    def _choose_output(self) -> None:
        folder = filedialog.askdirectory(title=tr("选择输出目录"), initialdir=str(self.output_dir))
        if folder:
            self.output_dir = Path(folder)
            self.output_label.config(text=str(self.output_dir))

    def _open_output(self) -> None:
        if not self.output_dir.exists():
            messagebox.showinfo(tr("提示"), tr("输出目录还不存在，先转换一次吧。"))
            return
        opener = {"darwin": "open", "win32": "explorer"}.get(sys.platform, "xdg-open")
        subprocess.Popen([opener, str(self.output_dir)])

    # ------------------------------------------------------------------ 转换

    def _start_conversion(self) -> None:
        if self.running:
            return
        if not self.pdfs:
            messagebox.showwarning(tr("没有文件"), tr("请先拖入或添加要转换的文件。"))
            return

        formats = [value for value, var in self.format_vars.items() if var.get()]
        if not formats:
            messagebox.showwarning(tr("没有选择格式"), tr("请至少勾选一种输出格式。"))
            return

        options = self._collect_options()
        try:
            jobs = max(1, int(self.jobs_var.get()))
        except (tk.TclError, ValueError):
            jobs = 1
        resume = self.resume_var.get()

        self.running = True
        self.convert_btn.config(state="disabled", text=tr("转换中…"))
        self.progress.config(maximum=len(self.pdfs), value=0)

        # 快照一份列表，避免转换过程中界面改动影响后台线程
        worker = threading.Thread(
            target=self._worker,
            args=(list(self.pdfs), self.output_dir, formats, options, jobs, resume, self.merge_var.get()),
            daemon=True,
        )
        worker.start()

    def _worker(
        self,
        pdfs: list[Path],
        out_root: Path,
        formats: list[str],
        options: ConvertOptions,
        jobs: int,
        resume: bool,
        merge: bool,
    ) -> None:
        """后台线程执行转换（统一走 execute_batch），通过队列把进度回传给界面。"""
        try:
            summary = execute_batch(
                pdfs, out_root, formats, options,
                jobs=jobs, resume=resume, merge=merge,
                report=not self.config.get("no_report", False),
                log=lambda msg: self.events.put(("log", msg)),
                progress=lambda done, total: self.events.put(("progress", done)),
            )
        except Exception as err:  # 引擎层意外异常也要让界面复位，不能卡在"转换中"
            self.events.put(("log", tr("转换过程异常：{err}", err=err)))
            summary = BatchSummary(total=len(pdfs), failed=len(pdfs))
        self.events.put(("done", summary))

    def _poll_events(self) -> None:
        """在主线程里消费后台线程的消息（tkinter 不允许跨线程更新界面）。"""
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "progress":
                    self.progress.config(value=payload)
                elif kind == "done":
                    self._on_done(payload)
                elif kind == "ocr_done":
                    self.ocr_setup_btn.config(state="normal")
                    self._refresh_ocr_status()
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)

    # ---------------------------------------------------------------- OCR 一键安装

    def _refresh_ocr_status(self) -> None:
        from . import ocr_setup

        try:
            self.ocr_status_label.config(text=ocr_setup.status_text())
        except Exception:
            self.ocr_status_label.config(text="")

    def _setup_ocr(self) -> None:
        """后台线程跑「安装 + 启动」，进度打进日志区。转换与它互不阻塞。"""
        from . import ocr_setup

        self.ocr_setup_btn.config(state="disabled")
        self._log(ocr_setup.status_text())

        def worker() -> None:
            try:
                ocr_setup.setup_and_start(lambda m: self.events.put(("log", m)))
            except Exception as err:
                self.events.put(("log", str(err)))
            finally:
                self.events.put(("ocr_done", None))

        threading.Thread(target=worker, daemon=True).start()

    def _on_done(self, summary: BatchSummary) -> None:
        self.running = False
        self.convert_btn.config(state="normal", text=tr("开始转换"))
        line = tr("共 {total} 个，成功 {ok} 个，失败 {bad} 个", total=summary.total, ok=summary.succeeded, bad=summary.failed)
        if summary.degraded:
            line += tr("（其中 {n} 个降级/无文字，见报告「说明」列）", n=summary.degraded)
        if summary.skipped:
            line += tr("，跳过 {n} 个", n=summary.skipped)
        line += tr("，耗时 {s:.1f}s", s=summary.elapsed)
        self._log(line)
        self._log("—" * 30)
        if summary.failed:
            messagebox.showwarning(tr("转换完成（有失败）"), line + "\n\n" + tr("详情见日志区。"))
        else:
            messagebox.showinfo(tr("转换完成"), line + "\n\n" + tr("输出目录：{out}", out=self.output_dir))

    def _log(self, message: str) -> None:
        self.log_text.config(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")


def main() -> int:
    ensure_utf8_stdio()
    root = TkinterDnD.Tk() if DND_AVAILABLE else tk.Tk()
    ConverterApp(root)
    if os.environ.get("MORSEL_GUI_SMOKE"):  # 打包冒烟测试用：起窗后自动关闭
        root.after(800, root.destroy)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
