#!/usr/bin/env python3
"""OCR 一键安装：在独立 venv 里装好 hybrid 依赖并启动/停止服务。

torch/docling 有几个 GB，是小白最大的坎。这里把「建环境 → 装依赖 → 起服务 →
等健康检查」做成一个可回调进度的函数，CLI（--setup-ocr）、GUI（按钮）、
Web（按钮）三个入口共用。

设计取舍：
- **独立 venv**（`~/.aimorsel/ocr-env`）：不污染用户环境、不依赖 conda；
  卸载 = 删目录。转换本身仍用主环境跑，venv 只为起 hybrid 服务。
- **服务托管**：Popen 脱离会话启动（关 GUI 不影响服务），pid 写
  `~/.aimorsel/ocr-server.pid`，服务日志落 `~/.aimorsel/ocr-server.log`。
- **进度全走 log 回调**（一行一条、已过 tr()），三个入口各自决定显示方式。
- 安装/启动幂等：已装只启动，已在线直接返回。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import venv
from pathlib import Path

from i18n import tr

OCR_HOME = Path.home() / ".aimorsel"
ENV_DIR = OCR_HOME / "ocr-env"
PID_FILE = OCR_HOME / "ocr-server.pid"
SERVER_LOG = OCR_HOME / "ocr-server.log"
LEGACY_HOME = Path.home() / ".pdf2md"   # 更名前的目录（v0.1.0 及更早）

HYBRID_PACKAGE = "opendataloader-pdf[hybrid]"
DEFAULT_PORT = 5002
DEFAULT_LANG = "ch_sim,en"
HEALTH_WAIT_SECONDS = 300  # 首次启动要加载模型，给足时间


def _env_bin(name: str) -> Path:
    sub = "Scripts" if os.name == "nt" else "bin"
    exe = name + (".exe" if os.name == "nt" else "")
    return ENV_DIR / sub / exe


def legacy_home_hint() -> bool:
    """更名前装过的人：环境还在 ~/.pdf2md，别让他们以为没装过又下几个 GB。"""
    return not is_installed() and (LEGACY_HOME / "ocr-env").exists()


def migration_message() -> str:
    """完整的迁移说明（多行）。只走日志和报错，**不要塞进状态标签**——
    GUI 的 OCR 状态是一个不换行的单行 Label，两行长文本会被整段挤出窗口。"""
    # 两句分开 tr()：隐式拼接的多行字符串，test_i18n 的扫描器只认得第一段
    return "\n".join([
        tr("检测到更名前的 OCR 环境 {old}，本版本改用 {new}。", old=LEGACY_HOME, new=OCR_HOME),
        tr("整个搬过去即可，不必重新下载：mv {old} {new}", old=LEGACY_HOME, new=OCR_HOME),
    ])


HYBRID_ENTRY = "opendataloader_pdf.hybrid_server"   # console script 背后的真正入口


def _site_packages() -> Path | None:
    for pattern in ("lib/python*/site-packages", "Lib/site-packages"):
        for path in sorted(ENV_DIR.glob(pattern)):
            return path
    return None


def is_installed() -> bool:
    """venv 里装好了 hybrid 后端。

    **故意不看 console script**：它的 shebang 是写死的绝对路径，目录一改名就变成
    「文件在、执行报 ENOENT」，而 is_installed 只判存在会误报「已安装」，
    接着 start_server 抛 FileNotFoundError，报错还点着一个 `ls` 得到的文件。
    venv 的 bin/python 是符号链接，搬家后照常可用，所以判据落在它 + 包本身。
    """
    site = _site_packages()
    return (_env_bin("python").exists() and site is not None
            and (site / "opendataloader_pdf" / "hybrid_server.py").exists())


def _shebang_ok(script: Path) -> bool:
    """console script 的 shebang 指向的解释器还在不在（目录被搬过就不在了）。"""
    try:
        first = script.open("rb").readline().decode("utf-8", "replace").strip()
    except OSError:
        return False
    return first.startswith("#!") and Path(first[2:].split()[0]).exists()


def server_command(port: int, lang: str) -> list[str]:
    """起服务的命令行。console script 可用就用它，shebang 断了就用 bin/python 直接调入口——
    后者让「把 ~/.pdf2md 整个搬成 ~/.aimorsel」真的能用，不必重装几个 GB。"""
    script = _env_bin("opendataloader-pdf-hybrid")
    if script.exists() and _shebang_ok(script):
        head = [str(script)]
    else:
        head = [str(_env_bin("python")), "-c",
                f"import sys; from {HYBRID_ENTRY} import main; sys.exit(main())"]
    return head + ["--port", str(port), "--ocr-lang", lang]


def is_running(url: str | None = None) -> bool:
    from morsel import check_ocr_server
    return check_ocr_server(url or f"http://127.0.0.1:{DEFAULT_PORT}")


def server_pid() -> int | None:
    """托管服务的 pid（进程已死则清掉 pid 文件并返回 None）。"""
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)  # 只探测存活，不发信号
        return pid
    except (OSError, ValueError):
        PID_FILE.unlink(missing_ok=True)
        return None


def install(log=print, package: str = HYBRID_PACKAGE) -> None:
    """建 venv 并安装 hybrid 依赖（几 GB，几分钟到几十分钟视网速）。幂等。"""
    if legacy_home_hint():
        # 只 log 一行没用：GUI/Web 是按钮一按就进后台线程，等用户读到那行，几个 GB 已经在下了。
        # 抛 RuntimeError 是有意的——CLI/GUI/Web 三个入口都已经在接它。
        raise RuntimeError(migration_message())
    OCR_HOME.mkdir(exist_ok=True)
    if not _env_bin("python").exists():
        log(tr("创建独立环境：{dir}", dir=ENV_DIR))
        venv.create(ENV_DIR, with_pip=True)
    env_python = _env_bin("python")
    log(tr("开始安装 {package}（几个 GB，视网速需要几分钟到几十分钟）…", package=package))
    proc = subprocess.Popen(
        [str(env_python), "-m", "pip", "install", "--upgrade", package],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        # pip 输出很密，只回显关键行（下载/安装/错误），别刷爆日志区
        if any(key in line for key in ("Downloading", "Installing collected",
                                       "Successfully installed", "ERROR", "error:")):
            log("  " + line)
    code = proc.wait()
    if code != 0:
        raise RuntimeError(tr("依赖安装失败（pip 退出码 {code}），完整输出见终端", code=code))
    # 完整性检查只对默认包有意义（测试会用小包替身走同一流程）
    if package == HYBRID_PACKAGE and not is_installed():
        raise RuntimeError(tr("安装完成但没有找到 opendataloader-pdf-hybrid，可能安装不完整"))
    log(tr("依赖安装完成。"))


def start_server(log=print, port: int = DEFAULT_PORT, lang: str = DEFAULT_LANG,
                 wait: int = HEALTH_WAIT_SECONDS) -> bool:
    """启动 hybrid 服务并等健康检查通过。已在线则直接返回 True。"""
    url = f"http://127.0.0.1:{port}"
    if is_running(url):
        log(tr("OCR 服务已在线：{url}", url=url))
        return True
    if not is_installed():
        raise RuntimeError(tr("尚未安装扫描件支持（{dir} 里没有装好的环境）", dir=ENV_DIR))
    cmd = server_command(port, lang)

    OCR_HOME.mkdir(exist_ok=True)
    log(tr("启动 OCR 服务（端口 {port}，识别语言 {lang}）…", port=port, lang=lang))
    log(tr("服务日志：{path}", path=SERVER_LOG))
    log_handle = SERVER_LOG.open("ab")
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000208  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True  # 脱离会话：关掉 GUI/终端服务照常跑
    try:
        proc = subprocess.Popen(cmd, stdout=log_handle, stderr=subprocess.STDOUT, **kwargs)
    except OSError as err:   # 别让裸 ENOENT 冒到用户面前（CLI 那层只接 RuntimeError）
        raise RuntimeError(tr("OCR 服务启动失败：{err}\n可尝试重新安装：morsel --setup-ocr",
                              err=err)) from err
    PID_FILE.write_text(str(proc.pid))

    deadline = time.time() + wait
    next_heartbeat = time.time() + 15
    while time.time() < deadline:
        if is_running(url):
            log(tr("OCR 服务已就绪：{url}（之后转换时选 auto/force 模式即可使用）", url=url))
            return True
        if proc.poll() is not None:
            raise RuntimeError(tr("OCR 服务启动失败（进程已退出），请查看日志：{path}", path=SERVER_LOG))
        if time.time() >= next_heartbeat:
            log(tr("仍在启动（首次需加载/下载模型，可能较慢）…"))
            next_heartbeat = time.time() + 30
        time.sleep(2)
    raise RuntimeError(tr("等待服务上线超时（{n}s），请查看日志：{path}", n=wait, path=SERVER_LOG))


def stop_server(log=print) -> bool:
    """停掉托管启动的服务（只管我们自己起的那个 pid）。"""
    pid = server_pid()
    if pid is None:
        log(tr("没有由本工具托管的 OCR 服务在运行。"))
        return False
    import signal
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
    PID_FILE.unlink(missing_ok=True)
    log(tr("OCR 服务已停止（pid {pid}）。", pid=pid))
    return True


def setup_and_start(log=print, port: int = DEFAULT_PORT, lang: str = DEFAULT_LANG) -> bool:
    """一键：装（如果没装）→ 启动 → 等就绪。三个入口都调这一个。"""
    if not is_installed():
        install(log)
    else:
        log(tr("扫描件支持已安装，直接启动服务。"))
    return start_server(log, port=port, lang=lang)


def status_text() -> str:
    """一行状态（GUI/Web 展示用）。"""
    if is_running():
        return tr("OCR 服务：在线")
    if is_installed():
        return tr("OCR 服务：已安装，未运行")
    if legacy_home_hint():   # 单行，完整的 mv 指引在日志里（见 migration_message）
        return tr("OCR 服务：未安装（检测到旧版目录，可直接搬过来，详见日志）")
    return tr("OCR 服务：未安装")
