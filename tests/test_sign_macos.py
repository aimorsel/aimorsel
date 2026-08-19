"""macOS 签名脚本的纯逻辑测试（不需要证书，不真的签名）。

重点锁两件事：
① Mach-O 分类正确——分错一个文件（可执行当成库签、或漏掉）公证就会被拒；
② 没有证书时给出可操作的中文引导，而不是抛栈。
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
PACKAGING = PROJECT / "packaging"

pytestmark = pytest.mark.skipif(sys.platform != "darwin",
                                reason="签名脚本只在 macOS 上有意义")


@pytest.fixture(scope="module")
def sign_mod():
    """按路径加载 packaging/sign_macos.py（它不在 import path 上）。"""
    spec = importlib.util.spec_from_file_location(
        "sign_macos", PACKAGING / "sign_macos.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_platform_label_shared_with_build(sign_mod):
    """平台标签必须与 build.py 同源，否则 x86_64 runner 会产出名字骗人的包。"""
    label = sign_mod.platform_label()
    assert label.startswith("macos-")
    assert label.split("-", 1)[1] in {"arm64", "x86_64"}


def test_entitlements_has_required_keys():
    """三项授权缺一：JVM 崩（前两项）或 PyInstaller 加载不了 .so（第三项）。"""
    import plistlib

    data = plistlib.loads((PACKAGING / "entitlements.plist").read_bytes())
    for key in ("com.apple.security.cs.allow-jit",
                "com.apple.security.cs.allow-unsigned-executable-memory",
                "com.apple.security.cs.disable-library-validation"):
        assert data.get(key) is True, f"缺少授权项 {key}"


def test_classify_machos(sign_mod, tmp_path):
    """可执行 / 库 / 非 Mach-O / 符号链接 四类都要归对。"""
    import shutil as _shutil

    # 真可执行
    _shutil.copy("/bin/ls", tmp_path / "tool")
    # 真库：Python 自带的 C 扩展（CI 上也一定有）
    import _ssl
    lib = Path(_ssl.__file__)
    _shutil.copy(lib, tmp_path / lib.name)
    # 非 Mach-O，且刻意用 Java class 的魔数——它和 fat Mach-O 撞车，
    # 只靠魔数判断会误收，必须靠 file(1) 排掉
    (tmp_path / "fake.class").write_bytes(b"\xca\xfe\xba\xbe" + b"\x00" * 32)
    (tmp_path / "readme.txt").write_text("not a binary")
    # 符号链接必须跳过（jre 里有 52 个），否则会重复签同一份内容
    (tmp_path / "link").symlink_to(tmp_path / "tool")

    libs, execs = sign_mod.classify_machos(tmp_path)
    assert [p.name for p in execs] == ["tool"]
    assert [p.name for p in libs] == [lib.name]


def test_no_identity_gives_actionable_error(sign_mod, monkeypatch):
    """没证书时应退出并指向手册，不能抛栈。"""
    monkeypatch.setattr(sign_mod, "run", lambda *a, **k: subprocess.CompletedProcess(
        args=[], returncode=1, stdout="0 valid identities found\n", stderr=""))
    with pytest.raises(SystemExit) as exc:
        sign_mod.find_identity(None)
    assert "SIGNING.md" in str(exc.value)


def test_explicit_identity_skips_lookup(sign_mod):
    assert sign_mod.find_identity("Developer ID Application: X (T)") == \
        "Developer ID Application: X (T)"


def test_zip_target_is_not_stapled(sign_mod, tmp_path, capsys):
    """zip 无法钉票据是 Apple 的格式限制，必须提示用户而不是静默跳过。"""
    target = tmp_path / "x.zip"
    target.write_bytes(b"")
    sign_mod.staple(target)
    assert "联网" in capsys.readouterr().out


def test_classify_machos_multiline_file_output(sign_mod, tmp_path, monkeypatch):
    """macOS 15 的 file(1) 对 universal 二进制输出多行（每架构一行）——
    不能按行 zip 对齐，必须按路径前缀归并（CI 实测踩过，三个 mac 任务全错位）。"""
    (tmp_path / "tool").write_bytes(b"\xca\xfe\xba\xbe" + b"\x00" * 60)
    (tmp_path / "lib.dylib").write_bytes(b"\xcf\xfa\xed\xfe" + b"\x00" * 60)
    (tmp_path / "fake.class").write_bytes(b"\xca\xfe\xba\xbe" + b"\x00" * 32)

    def fake_run(cmd, capture=False, check=True):
        out = (
            f"{tmp_path}/fake.class: compiled Java class data, version 52.0\n"
            f"{tmp_path}/lib.dylib: Mach-O 64-bit dynamically linked shared library arm64\n"
            f"{tmp_path}/tool: Mach-O universal binary with 2 architectures: [x86_64] [arm64e]\n"
            f"- Mach-O 64-bit executable x86_64\n"
            f"- Mach-O 64-bit executable arm64e\n"
        )
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=out, stderr="")

    monkeypatch.setattr(sign_mod, "run", fake_run)
    libs, execs = sign_mod.classify_machos(tmp_path)
    assert [p.name for p in execs] == ["tool"]
    assert [p.name for p in libs] == ["lib.dylib"]
