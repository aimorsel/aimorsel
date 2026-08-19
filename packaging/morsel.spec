# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec：一个 dist 目录出三个可执行（CLI / GUI / Web），共享精简 JRE 和依赖。

构建：packaging/build.sh（或手动 pyinstaller --noconfirm packaging/morsel.spec）
产物：dist/morsel/（morsel、morsel-gui、morsel-web、morsel-mcp + _internal/，raw/、output/ 会在首次运行时建在旁边）
"""

import os

from PyInstaller.utils.hooks import collect_all, collect_data_files

PROJECT = os.path.abspath(os.path.join(SPECPATH, ".."))

# 底层 JAR 等包内数据 + 捆绑 JRE
common_datas = collect_data_files("opendataloader_pdf")
common_datas += [(os.path.join(SPECPATH, "jre"), "jre")]

# tkinterdnd2 带二进制 tkdnd 库，必须整包收集
dnd_datas, dnd_binaries, dnd_hidden = collect_all("tkinterdnd2")

# 可选依赖（兜底网 + 多格式适配器）走 try/函数内 import，显式点名确保收进包；
# pyinstaller-hooks-contrib 的对应 hook 会把 docx/pptx 的模板数据一起带上。
# 前提：构建环境装有这些库（见 requirements.txt），否则静默不进包、对应功能在打包版缺失。
optional_hidden = ["pdfplumber", "pikepdf", "docx", "openpyxl", "pptx", "PIL"]

common_kwargs = dict(
    pathex=[PROJECT, SPECPATH],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

# `morsel gui|web|mcp` 子命令用 importlib 动态加载，PyInstaller 静态发现不了——
# 不点名的话打包版一敲子命令就 ModuleNotFoundError。dnd_hidden 跟着 aimorsel.morsel_gui 一起进。
# 源码已收进 aimorsel/ 包（2026-08-19 为 PyPI 发布重构），pathex 含仓库根即可找到。
subcommand_hidden = ["aimorsel.morsel_gui", "aimorsel.morsel_web", "aimorsel.morsel_mcp"]

a_cli = Analysis([os.path.join(SPECPATH, "entry_cli.py")],
                 datas=common_datas + dnd_datas, binaries=dnd_binaries,
                 hiddenimports=subcommand_hidden + dnd_hidden + optional_hidden,
                 **common_kwargs)
a_gui = Analysis(
    [os.path.join(SPECPATH, "entry_gui.py")],
    datas=common_datas + dnd_datas,
    binaries=dnd_binaries,
    hiddenimports=dnd_hidden + optional_hidden,
    **common_kwargs,
)
a_web = Analysis([os.path.join(SPECPATH, "entry_web.py")], datas=common_datas,
                 hiddenimports=optional_hidden, **common_kwargs)
a_mcp = Analysis([os.path.join(SPECPATH, "entry_mcp.py")], datas=common_datas,
                 hiddenimports=optional_hidden, **common_kwargs)

exe_cli = EXE(
    PYZ(a_cli.pure), a_cli.scripts, [],
    exclude_binaries=True, name="morsel", console=True,
)
exe_gui = EXE(
    PYZ(a_gui.pure), a_gui.scripts, [],
    exclude_binaries=True, name="morsel-gui", console=False,
)
exe_web = EXE(
    PYZ(a_web.pure), a_web.scripts, [],
    exclude_binaries=True, name="morsel-web", console=True,
)
# 独立的 morsel-mcp：`morsel mcp` 是常规用法，这个是工作目录下正好有 mcp 文件/目录时的逃生舱
exe_mcp = EXE(
    PYZ(a_mcp.pure), a_mcp.scripts, [],
    exclude_binaries=True, name="morsel-mcp", console=True,
)

COLLECT(
    exe_cli, a_cli.binaries, a_cli.datas,
    exe_gui, a_gui.binaries, a_gui.datas,
    exe_web, a_web.binaries, a_web.datas,
    exe_mcp, a_mcp.binaries, a_mcp.datas,
    name="morsel",
)
