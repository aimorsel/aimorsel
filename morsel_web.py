#!/usr/bin/env python3
"""
AImorsel —— 文档 → Markdown / JSON 提取工具（Web 常驻服务版）

把监听模式和一个本机 Web 界面合在一起：
  - 后台线程持续监听输入目录，新增/修改的 PDF 自动转换（断点续传去重）
  - 浏览器里上传 PDF（存入监听目录，自动被转换）、看实时日志、下载产物

零额外依赖（纯标准库 http.server）。转换选项从 config.toml 读取；
服务本身的参数用命令行传：

    python morsel_web.py                          # http://127.0.0.1:8008
    python morsel_web.py --port 9000 --input ~/Dropbox/收件
"""

from __future__ import annotations

import argparse
import contextlib
import html
import json
import re
import signal
import threading
import time
import urllib.parse
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from i18n import tr
from morsel import (
    DEFAULT_CHUNK_TOKENS,
    DEFAULT_HYBRID_URL,
    DEFAULT_INPUT_DIR,
    DEFAULT_OUTPUT_DIR,
    ConvertOptions,
    describe_options,
    ensure_utf8_stdio,
    execute_batch,
    find_pdfs,
    human_size,
    is_supported_input,
    load_config,
    load_manifest,
    options_signature,
    ocr_redo_available,
    should_skip,
)


def options_from_config(cfg: dict) -> ConvertOptions:
    """把 load_config() 的覆盖字典翻译成 ConvertOptions（Web 服务没有逐项界面）。"""
    return ConvertOptions(
        image_output=cfg.get("images"),
        pages=cfg.get("pages") or None,
        page_markers=cfg.get("page_markers", False),
        table_method="cluster" if cfg.get("better_tables") else None,
        sanitize=cfg.get("sanitize", False),
        threads=cfg.get("threads") or None,
        include_header_footer=cfg.get("header_footer", False),
        ocr_mode=cfg.get("ocr", "auto"),
        hybrid_url=cfg.get("ocr_url", DEFAULT_HYBRID_URL),
        rag_chunks=cfg.get("rag_chunks", False),
        chunk_tokens=cfg.get("chunk_size", DEFAULT_CHUNK_TOKENS),
        export_tables=cfg.get("export_tables", False),
        qa=cfg.get("qa", False),
        keep_all_content=cfg.get("keep_all_content", False),
        # 反义开关：config 里写 deskew=false → 覆盖字典里是 no_deskew=True
        deskew=not cfg.get("no_deskew", False),
        tidy=not cfg.get("no_tidy", False),
    )


class WebService:
    """服务状态：监听线程 + 日志环形缓冲 + 累计统计。Handler 通过它读写一切。"""

    def __init__(self, watch_dir: Path, out_root: Path, formats: list[str],
                 options: ConvertOptions, jobs: int, interval: float, merge: bool) -> None:
        self.watch_dir = watch_dir
        self.out_root = out_root
        self.formats = formats
        self.options = options
        self.jobs = jobs
        self.interval = interval
        self.merge = merge
        self.started_at = time.time()
        self.lock = threading.Lock()
        self.log_lines: deque[str] = deque(maxlen=400)
        self.recent: deque[dict] = deque(maxlen=50)
        self.stats = {"succeeded": 0, "failed": 0, "skipped": 0, "degraded": 0}
        self.converting = False
        self.stop_event = threading.Event()

    def log(self, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        with self.lock:
            for line in str(message).splitlines():
                self.log_lines.append(f"[{stamp}] {line}")

    # ------------------------------------------------------------ 监听线程

    def watcher(self) -> None:
        """和 morsel.watch_loop 相同的扫描策略，跑在 daemon 线程里。"""
        signature = options_signature(self.formats, self.options)
        last_seen: dict[Path, tuple[float, int]] = {}
        while not self.stop_event.is_set():
            entries = load_manifest(self.out_root)
            redo_ok = ocr_redo_available(entries, self.options)  # 等 OCR 的图片在服务上线后自动补转
            ready: list[Path] = []
            for pdf in find_pdfs(self.watch_dir):
                try:
                    st = pdf.stat()
                except OSError:
                    continue
                stamp = (st.st_mtime, st.st_size)
                if should_skip(pdf, entries, signature, server_ok=redo_ok):
                    last_seen[pdf] = stamp
                    continue
                if last_seen.get(pdf) == stamp:
                    ready.append(pdf)  # 连续两轮未变化，认为已写完
                else:
                    last_seen[pdf] = stamp
            if ready:
                self.log(tr("发现 {n} 个待转换文件", n=len(ready)))
                self.converting = True
                try:
                    summary = execute_batch(
                        ready, self.out_root, self.formats, self.options,
                        jobs=self.jobs, resume=True, merge=self.merge, log=self.log,
                    )
                    with self.lock:
                        self.stats["succeeded"] += summary.succeeded
                        self.stats["failed"] += summary.failed
                        self.stats["skipped"] += summary.skipped
                        self.stats["degraded"] += summary.degraded
                        for r in summary.results:
                            self.recent.appendleft({
                                "name": r.pdf.name,
                                "ok": r.ok,
                                "degraded": r.degraded,
                                "skipped": r.skipped,
                                "duration": round(r.duration, 1),
                                "pages": r.pages,
                                "note": r.error or r.note,
                                "dir": r.pdf.stem,
                            })
                except Exception as err:  # 监听线程绝不能死
                    self.log(tr("转换过程异常：{err}", err=err))
                finally:
                    self.converting = False
            self.stop_event.wait(self.interval)

    # ------------------------------------------------------------ 数据快照

    def status_payload(self) -> dict:
        with self.lock:
            log_tail = list(self.log_lines)
            recent = list(self.recent)
            stats = dict(self.stats)
        outputs = []
        if self.out_root.is_dir():
            for item in sorted(self.out_root.iterdir()):
                if item.name.startswith("."):
                    continue
                if item.is_file():
                    outputs.append({"dir": "", "name": item.name,
                                    "size": human_size(item.stat().st_size)})
                elif item.is_dir():
                    for f in sorted(item.rglob("*")):
                        if f.is_file():
                            outputs.append({"dir": item.name,
                                            "name": str(f.relative_to(item)),
                                            "size": human_size(f.stat().st_size)})
        return {
            "watch_dir": str(self.watch_dir),
            "out_root": str(self.out_root),
            "formats": self.formats,
            "options": describe_options(self.options) or [tr("默认设置")],
            "jobs": self.jobs,
            "interval": self.interval,
            "uptime": int(time.time() - self.started_at),
            "converting": self.converting,
            "stats": stats,
            "recent": recent,
            "log": log_tail[-120:],
            "outputs": outputs[:500],
            "ocr": _ocr_status_text(),
        }


_ocr_setup_running = False


def _ocr_status_text() -> str:
    try:
        import ocr_setup
        if _ocr_setup_running:
            return tr("OCR 服务：安装/启动中…（进度见实时日志）")
        return ocr_setup.status_text()
    except Exception:
        return ""


def start_ocr_setup_thread(service: "WebService") -> bool:
    """后台线程跑一键安装，进度进服务日志。已在跑则拒绝重复启动。"""
    global _ocr_setup_running
    if _ocr_setup_running:
        return False

    def worker() -> None:
        global _ocr_setup_running
        import ocr_setup
        try:
            ocr_setup.setup_and_start(service.log)
        except Exception as err:
            service.log(str(err))
        finally:
            _ocr_setup_running = False

    _ocr_setup_running = True
    threading.Thread(target=worker, daemon=True).start()
    return True


def parse_multipart_files(body: bytes, content_type: str) -> list[tuple[str, bytes]]:
    """极简 multipart/form-data 解析，只提取带 filename 的文件字段。"""
    match = re.search(r"boundary=([^;]+)", content_type or "")
    if not match:
        return []
    boundary = match.group(1).strip().strip('"').encode()
    files: list[tuple[str, bytes]] = []
    for part in body.split(b"--" + boundary):
        if b"\r\n\r\n" not in part:
            continue
        header_blob, _, payload = part.partition(b"\r\n\r\n")
        name_match = re.search(r'filename="([^"]*)"', header_blob.decode("utf-8", "replace"))
        if not name_match or not name_match.group(1):
            continue
        files.append((name_match.group(1), payload.rstrip(b"\r\n")))
    return files


PAGE_TEMPLATE = """<!doctype html>
<html lang="{t_lang}">
<head>
<meta charset="utf-8">
<title>{t_title}</title>
<style>
  body {{ font-family: -apple-system, "PingFang SC", sans-serif; margin: 0; background: #f5f6f8; color: #222; }}
  header {{ background: #2a5db0; color: #fff; padding: 14px 22px; }}
  header h1 {{ margin: 0; font-size: 20px; }}
  main {{ max-width: 1000px; margin: 18px auto; padding: 0 16px; }}
  .card {{ background: #fff; border-radius: 8px; padding: 14px 18px; margin-bottom: 14px;
           box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
  .meta span {{ display: inline-block; margin-right: 18px; color: #555; font-size: 13px; }}
  .stats b {{ font-size: 18px; }}
  pre {{ background: #14171c; color: #cde3c8; font-size: 12px; padding: 10px;
         border-radius: 6px; max-height: 280px; overflow: auto; white-space: pre-wrap; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
  th, td {{ text-align: left; padding: 5px 8px; border-bottom: 1px solid #eee; }}
  a {{ color: #2a5db0; text-decoration: none; }}
  .ok {{ color: #1a7f37; }} .bad {{ color: #c0392b; }} .skip {{ color: #888; }} .deg {{ color: #b7791f; }}
  .busy {{ color: #b8860b; font-weight: 600; }}
  input[type=file] {{ margin-right: 10px; }}
  button {{ background: #2a5db0; color: #fff; border: 0; padding: 6px 16px;
            border-radius: 5px; cursor: pointer; }}
</style>
</head>
<body>
<header><h1>{t_header}</h1></header>
<main>
  <div class="card meta" id="meta">{t_loading}</div>
  <div class="card">
    <b>{t_upload_head}</b>{t_upload_hint}
    <form id="up" style="margin-top:8px">
      <input type="file" name="files" multiple
             accept=".pdf,.docx,.xlsx,.pptx,.html,.htm,.png,.jpg,.jpeg,.tif,.tiff,.bmp,.webp,.gif">
      <button type="submit">{t_upload_btn}</button>
      <span id="upmsg" style="margin-left:10px;color:#555"></span>
    </form>
    <div style="margin-top:10px;color:#555;font-size:13px">
      <span id="ocrstatus"></span>
      <button type="button" id="ocrbtn" style="margin-left:10px">{t_ocr_btn}</button>
    </div>
  </div>
  <div class="card"><b>{t_recent}</b><table id="recent"></table></div>
  <div class="card"><b>{t_log}</b><pre id="log"></pre></div>
  <div class="card"><b>{t_outputs}</b><table id="outputs"></table></div>
</main>
<script>
async function refresh() {{
  try {{
    const s = await (await fetch('/api/status')).json();
    const st = s.stats;
    document.getElementById('meta').innerHTML =
      `<span>{t_watching}<b>${{s.watch_dir}}</b>（${{s.interval}}s）</span>` +
      `<span>{t_out}<b>${{s.out_root}}</b></span>` +
      `<span>{t_formats}${{s.formats.join(', ')}}</span>` +
      `<span>{t_options}${{s.options.join('{t_join}')}}</span>` +
      `<span>{t_jobs}${{s.jobs}}</span>` +
      `<span class="stats">{t_ok} <b class="ok">${{st.succeeded}}</b> ·
        {t_bad} <b class="bad">${{st.failed}}</b> · {t_skip} <b class="skip">${{st.skipped}}</b>` +
      (st.degraded ? ` · {t_deg} <b class="deg">${{st.degraded}}</b>` : '') + `</span>` +
      (s.converting ? '<span class="busy">{t_busy}</span>' : '<span class="ok">{t_idle}</span>');
    document.getElementById('log').textContent = s.log.join('\\n');
    document.getElementById('ocrstatus').textContent = s.ocr;
    document.getElementById('recent').innerHTML =
      '<tr><th>{t_file}</th><th>{t_status}</th><th>{t_pages}</th><th>{t_dur}</th><th>{t_note}</th></tr>' +
      s.recent.map(r => `<tr><td>${{r.name}}</td>` +
        `<td class="${{r.skipped ? 'skip' : (r.ok ? (r.degraded ? 'deg' : 'ok') : 'bad')}}">` +
        `${{r.skipped ? '{t_skip}' : (r.ok ? (r.degraded ? '△ {t_deg}' : '{t_ok}') : '{t_bad}')}}</td>` +
        `<td>${{r.pages ?? ''}}</td><td>${{r.duration}}s</td><td>${{r.note || ''}}</td></tr>`).join('');
    document.getElementById('outputs').innerHTML =
      '<tr><th>{t_dir}</th><th>{t_file}</th><th>{t_size}</th></tr>' +
      s.outputs.map(o => {{
        const rel = o.dir ? o.dir + '/' + o.name : o.name;
        return `<tr><td>${{o.dir}}</td>` +
          `<td><a href="/files?p=${{encodeURIComponent(rel)}}">${{o.name}}</a></td>` +
          `<td>${{o.size}}</td></tr>`;
      }}).join('');
  }} catch (e) {{ /* 服务重启间隙忽略 */ }}
}}
document.getElementById('ocrbtn').addEventListener('click', async () => {{
  const btn = document.getElementById('ocrbtn');
  btn.disabled = true;
  await fetch('/ocr/setup', {{ method: 'POST' }});
  setTimeout(() => {{ btn.disabled = false; }}, 5000);
}});
document.getElementById('up').addEventListener('submit', async ev => {{
  ev.preventDefault();
  const fd = new FormData(ev.target);
  const msg = document.getElementById('upmsg');
  msg.textContent = '{t_uploading}';
  const resp = await fetch('/upload', {{ method: 'POST', body: fd }});
  msg.textContent = await resp.text();
  ev.target.reset();
  refresh();
}});
refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>
"""


def render_page() -> str:
    """把翻译文案填进模板。模板里 {{ }} 是 format 转义，占位符 {t_*} 单大括号。"""
    import i18n as _i18n

    return PAGE_TEMPLATE.format(
        t_lang=("en" if _i18n.current_lang() == "en" else "zh"),
        t_title=tr("AImorsel 文粒 — 文档转换服务"),
        t_header=tr("AImorsel 文粒 · 文档 → Markdown / JSON"),
        t_loading=tr("加载中…"),
        t_upload_head=tr("上传文件"),
        t_upload_hint=tr("（PDF / docx / xlsx / pptx / HTML / 图片，保存到监听目录后自动转换）"),
        t_upload_btn=tr("上传"),
        t_recent=tr("最近转换"),
        t_log=tr("实时日志"),
        t_outputs=tr("输出文件"),
        t_watching=tr("监听："), t_out=tr("输出："), t_formats=tr("格式："),
        t_options=tr("选项："), t_jobs=tr("并发："),
        t_join=tr("、"),
        t_ok=tr("成功"), t_bad=tr("失败"), t_skip=tr("跳过"), t_deg=tr("降级"),
        t_busy=tr("转换中…"), t_idle=tr("空闲"),
        t_file=tr("文件"), t_status=tr("状态"), t_pages=tr("页数"),
        t_dur=tr("耗时"), t_note=tr("说明"), t_dir=tr("目录"), t_size=tr("大小"),
        t_uploading=tr("上传中…"),
        t_ocr_btn=tr("启用扫描件支持"),
    )


def make_handler(service: WebService):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:  # 静音默认的逐请求日志
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/":
                # 模板里 {{ }} 是 str.format 转义，必须 .format() 还原成单大括号
                self._send(200, render_page().encode("utf-8"), "text/html; charset=utf-8")
            elif parsed.path == "/api/status":
                body = json.dumps(service.status_payload(), ensure_ascii=False).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
            elif parsed.path == "/files":
                self._serve_file(parsed)
            else:
                self._send(404, b"not found", "text/plain")

        def _serve_file(self, parsed) -> None:
            rel = urllib.parse.parse_qs(parsed.query).get("p", [""])[0]
            target = (service.out_root / rel).resolve()
            root = service.out_root.resolve()
            # 防路径穿越：只允许输出目录内的文件
            if not target.is_file() or not target.is_relative_to(root):
                self._send(404, b"not found", "text/plain")
                return
            ctype = {
                ".md": "text/markdown; charset=utf-8",
                ".json": "application/json; charset=utf-8",
                ".jsonl": "application/json; charset=utf-8",
                ".csv": "text/csv; charset=utf-8",
                ".html": "text/html; charset=utf-8",
                ".txt": "text/plain; charset=utf-8",
                ".pdf": "application/pdf",
                ".png": "image/png",
            }.get(target.suffix.lower(), "application/octet-stream")
            self._send(200, target.read_bytes(), ctype)

        def do_POST(self) -> None:
            if self.path == "/ocr/setup":
                started = start_ocr_setup_thread(service)
                message = tr("已开始安装/启动，进度见实时日志") if started else tr("安装已在进行中")
                self._send(200, message.encode("utf-8"), "text/plain; charset=utf-8")
                return
            if urllib.parse.urlparse(self.path).path != "/upload":
                self._send(404, b"not found", "text/plain")
                return
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= 500 * 1024 * 1024:
                self._send(400, tr("文件过大或为空").encode("utf-8"), "text/plain; charset=utf-8")
                return
            body = self.rfile.read(length)
            files = parse_multipart_files(body, self.headers.get("Content-Type", ""))
            saved = 0
            for raw_name, payload in files:
                name = Path(raw_name).name  # 去掉客户端路径
                if not is_supported_input(Path(name)) or not payload:
                    continue
                (service.watch_dir / name).write_bytes(payload)
                saved += 1
            if saved:
                service.log(tr("网页上传 {n} 个文件 -> {dir}", n=saved, dir=service.watch_dir))
                message = tr("已上传 {n} 个文件，稍候自动转换", n=saved)
            else:
                message = tr("没有可接收的文件（支持 PDF/docx/xlsx/pptx/HTML/图片）")
            self._send(200, message.encode("utf-8"), "text/plain; charset=utf-8")

    return Handler


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_stdio()
    # prog 写死成子命令形式：默认取 sys.argv[0]，走 `morsel web` 进来时会打成 "morsel.py"，
    # 照着 usage 敲 `morsel --port 9000` 是非法的
    parser = argparse.ArgumentParser(
        prog="morsel web", description=tr("文档转换常驻服务（Web 界面 + 文件夹监听）"))
    parser.add_argument("--host", default="127.0.0.1", help=tr("监听地址（默认仅本机 127.0.0.1）"))
    parser.add_argument("--port", type=int, default=8008, help=tr("端口（默认 8008）"))
    parser.add_argument("--input", default=None, help=tr("被监听的文件夹（默认 {dir}）", dir=DEFAULT_INPUT_DIR))
    parser.add_argument("-o", "--output", default=None, help=tr("输出目录（默认 {dir}）", dir=DEFAULT_OUTPUT_DIR))
    parser.add_argument("--interval", type=float, default=5.0, help=tr("扫描间隔秒数（默认 5）"))
    args = parser.parse_args(argv)

    cfg, warnings = load_config()
    watch_dir = Path(args.input).expanduser() if args.input else DEFAULT_INPUT_DIR
    out_root = Path(args.output or cfg.get("output") or DEFAULT_OUTPUT_DIR).expanduser()
    watch_dir.mkdir(parents=True, exist_ok=True)
    out_root.mkdir(parents=True, exist_ok=True)
    formats = [f.strip() for f in (cfg.get("format") or "markdown,json").split(",") if f.strip()]

    service = WebService(
        watch_dir, out_root, formats,
        options_from_config(cfg),
        jobs=max(1, cfg.get("jobs", 1)),
        interval=max(1.0, args.interval),
        merge=bool(cfg.get("merge", False)),
    )
    for warning in warnings:
        service.log(f"[config] {warning}")
    if cfg:
        service.log(tr("已加载 config.toml（{n} 项）", n=len(cfg)))
    service.log(tr("服务启动"))

    threading.Thread(target=service.watcher, daemon=True).start()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(service))

    def _stop(signum, frame):
        raise KeyboardInterrupt
    with contextlib.suppress(ValueError):
        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)

    print(tr("文档转换服务已启动：{url}", url=f"http://{args.host}:{args.port}"), flush=True)
    print(tr("监听目录：{dir}（新文件自动转换，Ctrl+C 退出）", dir=watch_dir), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        service.stop_event.set()
        server.server_close()
        print(tr("服务已停止。"), flush=True)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
