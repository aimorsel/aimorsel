"""OCR 一键安装的纯逻辑测试（不真下载几 GB 依赖、不起真服务）。"""

from __future__ import annotations

import os
import subprocess
import types
from pathlib import Path

import pytest

import ocr_setup


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """把所有落盘位置改到 tmp，避免碰用户的 ~/.aimorsel。

    **LEGACY_HOME 也必须隔离**：不隔离的话 legacy_home_hint() 会去读开发机真实的
    ~/.pdf2md，测试结果跟着机器走（本机装过就红、CI 上就绿）。
    """
    monkeypatch.setattr(ocr_setup, "OCR_HOME", tmp_path)
    monkeypatch.setattr(ocr_setup, "ENV_DIR", tmp_path / "ocr-env")
    monkeypatch.setattr(ocr_setup, "PID_FILE", tmp_path / "ocr-server.pid")
    monkeypatch.setattr(ocr_setup, "SERVER_LOG", tmp_path / "ocr-server.log")
    monkeypatch.setattr(ocr_setup, "LEGACY_HOME", tmp_path / "legacy-home")
    return tmp_path


def _fake_install(sandbox, *, script_shebang: str | None = "#!{python}") -> Path:
    """造一个「装好了」的 venv：bin/python + site-packages 里的包 + console script。"""
    python = ocr_setup._env_bin("python")
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("")
    pkg = ocr_setup.ENV_DIR / "lib" / "python3.12" / "site-packages" / "opendataloader_pdf"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "hybrid_server.py").write_text("")
    exe = ocr_setup._env_bin("opendataloader-pdf-hybrid")
    if script_shebang is not None:
        exe.write_text(script_shebang.format(python=python) + "\nsleep 60\n")
        exe.chmod(0o755)
    return exe


def _fake_exe(sandbox) -> Path:      # 旧名保留，几处老测试还在用
    return _fake_install(sandbox)


def _fake_legacy(sandbox) -> None:
    """造一个更名前的旧环境（只要 ocr-env 目录存在就算）。"""
    (ocr_setup.LEGACY_HOME / "ocr-env").mkdir(parents=True, exist_ok=True)


def test_env_bin_layout():
    path = ocr_setup._env_bin("python")
    if os.name == "nt":
        assert path.parent.name == "Scripts" and path.suffix == ".exe"
    else:
        assert path.parent.name == "bin" and path.suffix == ""


def test_is_installed(sandbox):
    assert not ocr_setup.is_installed()
    _fake_exe(sandbox)
    assert ocr_setup.is_installed()


def test_install_failure_raises(sandbox, monkeypatch):
    monkeypatch.setattr(ocr_setup.venv, "create", lambda *a, **k: _fake_install(sandbox))

    class FakeProc:
        stdout = iter(["ERROR: no matching distribution\n"])
        def wait(self):
            return 1
    monkeypatch.setattr(ocr_setup.subprocess, "Popen", lambda *a, **k: FakeProc())
    with pytest.raises(RuntimeError):
        ocr_setup.install(log=lambda m: None)


def test_start_server_writes_pid_and_polls(sandbox, monkeypatch):
    _fake_exe(sandbox)
    calls = {"n": 0}

    def fake_running(url=None):
        calls["n"] += 1
        return calls["n"] > 1  # 第一次探测离线，启动后在线
    monkeypatch.setattr(ocr_setup, "is_running", fake_running)

    launched = {}

    class FakeProc:
        pid = 4242
        def poll(self):
            return None
    def fake_popen(cmd, **kwargs):
        launched["cmd"] = [str(c) for c in cmd]
        return FakeProc()
    monkeypatch.setattr(ocr_setup.subprocess, "Popen", fake_popen)

    assert ocr_setup.start_server(log=lambda m: None, port=5099, lang="de,en", wait=10)
    assert ocr_setup.PID_FILE.read_text() == "4242"
    assert "--port" in launched["cmd"] and "5099" in launched["cmd"]
    assert "de,en" in launched["cmd"]


def test_start_server_already_online(sandbox, monkeypatch):
    monkeypatch.setattr(ocr_setup, "is_running", lambda url=None: True)
    assert ocr_setup.start_server(log=lambda m: None)  # 不需要可执行文件存在


def test_start_server_missing_install(sandbox, monkeypatch):
    monkeypatch.setattr(ocr_setup, "is_running", lambda url=None: False)
    with pytest.raises(RuntimeError):
        ocr_setup.start_server(log=lambda m: None)


def test_stop_server_no_pid(sandbox):
    assert ocr_setup.stop_server(log=lambda m: None) is False


def test_status_text_branches(sandbox, monkeypatch):
    monkeypatch.setattr(ocr_setup, "is_running", lambda url=None: False)
    assert "未安装" in ocr_setup.status_text()
    _fake_exe(sandbox)
    assert "未运行" in ocr_setup.status_text()
    monkeypatch.setattr(ocr_setup, "is_running", lambda url=None: True)
    assert "在线" in ocr_setup.status_text()


def test_legacy_home_migration(sandbox, monkeypatch):
    """更名前装过的人：提示要出现，install 要**中断**而不是照样下载几个 GB。"""
    assert ocr_setup.legacy_home_hint() is False        # 没有旧目录时不打扰
    _fake_legacy(sandbox)
    assert ocr_setup.legacy_home_hint() is True
    monkeypatch.setattr(ocr_setup, "is_running", lambda url=None: False)
    status = ocr_setup.status_text()
    assert "未安装" in status and "\n" not in status    # 状态标签必须是单行
    assert "mv " in ocr_setup.migration_message()       # 完整指引走日志/报错

    started = []
    monkeypatch.setattr(ocr_setup.venv, "create", lambda *a, **k: started.append("venv"))
    monkeypatch.setattr(ocr_setup.subprocess, "Popen", lambda *a, **k: started.append("pip"))
    with pytest.raises(RuntimeError, match="mv "):
        ocr_setup.install(log=lambda m: None)
    assert started == []                                # 一个字节都不能下

    _fake_install(sandbox)                              # 搬过来之后
    assert ocr_setup.legacy_home_hint() is False        # 提示消失


def test_server_command_survives_moved_venv(sandbox):
    """venv 被整个搬过来后 console script 的绝对 shebang 会断，此时必须回退到 bin/python。"""
    _fake_install(sandbox)
    assert ocr_setup.is_installed()                     # 判据不看 console script
    cmd = ocr_setup.server_command(5099, "de,en")
    assert cmd[0].endswith("opendataloader-pdf-hybrid")  # shebang 完好时照旧用它

    exe = ocr_setup._env_bin("opendataloader-pdf-hybrid")
    exe.write_text("#!/nonexistent/old-home/ocr-env/bin/python\n")   # 模拟搬家后的断链
    assert not ocr_setup._shebang_ok(exe)
    cmd = ocr_setup.server_command(5099, "de,en")
    assert cmd[0].endswith("python") and cmd[1] == "-c"
    assert ocr_setup.HYBRID_ENTRY in cmd[2]
    assert cmd[-4:] == ["--port", "5099", "--ocr-lang", "de,en"]


def test_start_server_wraps_oserror(sandbox, monkeypatch):
    """起进程失败要包成 RuntimeError——CLI 那层只接 RuntimeError，裸 ENOENT 会冒成 traceback。"""
    _fake_install(sandbox)
    monkeypatch.setattr(ocr_setup, "is_running", lambda url=None: False)

    def boom(*a, **k):
        raise OSError(2, "No such file or directory")
    monkeypatch.setattr(ocr_setup.subprocess, "Popen", boom)
    with pytest.raises(RuntimeError):
        ocr_setup.start_server(log=lambda m: None, wait=1)
