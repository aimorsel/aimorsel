#!/usr/bin/env python3
"""跨平台一键构建打包版：jlink 精简 JRE → PyInstaller → 按平台命名的 zip。

用法（macOS / Windows 同一个脚本，CI 的 release.yml 也调它）：
    python packaging/build.py                  # 构建 + 打 zip
    python packaging/build.py --version v1.0.0 # zip 名里带版本号
    python packaging/build.py --no-zip         # 只构建不打包

前置：构建环境装有 requirements.txt 依赖 + pyinstaller；JDK 21+（jlink 用，
通过 JAVA_HOME / PATH / macOS java_home 自动探测）。
产物：dist/morsel/（四个可执行 + 捆绑 JRE）和 dist/morsel-<版本>-<平台>.zip。
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"
JRE_DIR = PACKAGING / "jre"
DIST = ROOT / "dist" / "morsel"

# 模块集 = jdeps 分析底层 JAR 的结果 + 保险的 jdk.unsupported / jdk.crypto.ec
JLINK_MODULES = (
    "java.base,java.compiler,java.desktop,java.management,java.sql,"
    "jdk.unsupported,jdk.crypto.ec"
)


if sys.platform == "win32":
    # Windows 控制台默认 cp1252/GBK，打中文进度会 UnicodeEncodeError（CI 实测炸过）
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


def run(cmd, **kwargs) -> None:
    print("==>", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], check=True, **kwargs)


def find_jdk_tool(name: str) -> Path:
    """按 JAVA_HOME → PATH → macOS java_home 的顺序找 JDK 工具（jlink 等）。"""
    exe = f"{name}.exe" if os.name == "nt" else name
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        candidate = Path(java_home) / "bin" / exe
        if candidate.exists():
            return candidate
    found = shutil.which(name)
    if found:
        return Path(found)
    if sys.platform == "darwin":
        try:
            home = subprocess.run(["/usr/libexec/java_home"], capture_output=True,
                                  text=True, check=True).stdout.strip()
            candidate = Path(home) / "bin" / exe
            if candidate.exists():
                return candidate
        except (OSError, subprocess.CalledProcessError):
            pass
    sys.exit(f"找不到 {name}：请安装 JDK 21+，并保证 JAVA_HOME 或 PATH 可见")


def build_jre() -> None:
    if JRE_DIR.is_dir():
        print(f"已有 {JRE_DIR}，跳过 jlink（删掉该目录可强制重建）")
        return
    jlink = find_jdk_tool("jlink")
    run([jlink, "--add-modules", JLINK_MODULES,
         "--strip-debug", "--no-header-files", "--no-man-pages",
         "--compress", "zip-6", "--output", JRE_DIR])


def build_dist() -> None:
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", PACKAGING / "morsel.spec"],
        cwd=ROOT)
    shutil.copy(ROOT / "config.toml", DIST / "config.toml")


def platform_label() -> str:
    os_name = {"darwin": "macos", "win32": "windows"}.get(sys.platform, sys.platform)
    machine = platform.machine().lower()
    arch = {"amd64": "x64", "x86_64": "x86_64", "arm64": "arm64",
            "aarch64": "arm64"}.get(machine, machine)
    return f"{os_name}-{arch}"


def make_zip(version: str) -> Path:
    tag = f"{version}-" if version else ""
    out = ROOT / "dist" / f"morsel-{tag}{platform_label()}.zip"
    if out.exists():
        out.unlink()
    if sys.platform == "darwin":
        # ditto 保留可执行权限和符号链接——Python zipfile 会丢，包解开后跑不起来
        run(["ditto", "-c", "-k", "--keepParent", DIST, out])
    else:
        # Windows 无权限位问题；Linux 非发布目标（有需要应改用 tar.gz 保权限）
        shutil.make_archive(str(out.with_suffix("")), "zip",
                            root_dir=DIST.parent, base_dir=DIST.name)
    print(f"打包完成：{out}（{out.stat().st_size / 1024 / 1024:.0f}MB）")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="构建三平台发行包（当前平台）")
    parser.add_argument("--version", default=os.environ.get("MORSEL_VERSION", ""),
                        help="写进 zip 文件名的版本号（CI 里传 tag 名）")
    parser.add_argument("--no-zip", action="store_true", help="只构建 dist/，不打 zip")
    args = parser.parse_args()

    build_jre()
    build_dist()
    if not args.no_zip:
        make_zip(args.version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
