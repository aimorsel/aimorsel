"""i18n 机制 + 翻译完整性。

完整性检查扫三类 key：
① 源码里所有 tr("字面量") ；② _ask_yes_no("…") 的提问（函数内部才包 tr）；
③ FORMAT_PRESETS / FORMAT_CHOICES 的显示名（展示处 tr(label) 间接调用）。
新增用户可见文案而忘了进英文表，会在这里直接红。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import i18n

PROJECT = Path(__file__).resolve().parent.parent
SOURCES = ["morsel.py", "morsel_gui.py", "morsel_web.py", "morsel_mcp.py",
           "format_adapters.py", "ocr_setup.py"]

_TR_PAT = re.compile(r"""tr\(\s*(("(?:[^"\\]|\\.)+")|('(?:[^'\\]|\\.)+'))""")
_ASK_PAT = re.compile(r"""_ask_yes_no\(\s*"((?:[^"\\]|\\.)+)"

""".strip())


def _all_keys() -> set[str]:
    keys: set[str] = set()
    for name in SOURCES:
        text = (PROJECT / name).read_text(encoding="utf-8")
        for m in _TR_PAT.finditer(text):
            keys.add(eval(m.group(1)))  # noqa: S307 —— 还原字符串转义
        for m in _ASK_PAT.finditer(text):
            keys.add(m.group(1))
    import morsel
    import morsel_gui
    keys.update(label for label, _ in morsel.FORMAT_PRESETS.values())
    keys.update(label for label, _, _ in morsel_gui.FORMAT_CHOICES)
    return {k for k in keys if re.search(r"[一-鿿]", k)}  # 只检查含中文的 key


def test_catalog_complete():
    missing = sorted(k for k in _all_keys() if k not in i18n._EN)
    assert not missing, f"缺 {len(missing)} 条英文翻译：\n" + "\n".join(missing[:20])


def test_tr_zh_passthrough(monkeypatch):
    monkeypatch.setenv("MORSEL_LANG", "zh")
    monkeypatch.setattr(i18n, "_lang", None)
    assert i18n.tr("成功") == "成功"
    assert i18n.tr("RAG 分块 {n} 块", n=3) == "RAG 分块 3 块"


def test_tr_en(monkeypatch):
    monkeypatch.setenv("MORSEL_LANG", "en")
    monkeypatch.setattr(i18n, "_lang", None)
    assert i18n.tr("成功") == "OK"
    assert i18n.tr("RAG 分块 {n} 块", n=3) == "3 RAG chunk(s)"
    # 未收录 key：原文兜底不崩
    assert i18n.tr("这条不存在的文案") == "这条不存在的文案"


def test_tr_format_spec(monkeypatch):
    monkeypatch.setenv("MORSEL_LANG", "en")
    monkeypatch.setattr(i18n, "_lang", None)
    out = i18n.tr("，耗时 {s:.1f}s", s=1.234)
    assert out == ", 1.2s"


def test_en_placeholders_match():
    """英文翻译里的占位符必须与中文 key 完全一致，否则 format 时 KeyError。"""
    pat = re.compile(r"\{([a-z_]+)(?:[:!][^}]*)?\}")
    for zh, en in i18n._EN.items():
        assert set(pat.findall(zh)) == set(pat.findall(en)), f"占位符不一致：{zh!r}"


def test_lang_resolution(monkeypatch):
    monkeypatch.delenv("MORSEL_LANG", raising=False)
    monkeypatch.setattr(i18n, "_lang", None)
    monkeypatch.setattr(i18n, "_config_lang", None)
    i18n.set_lang("en")
    assert i18n.current_lang() == "en"
    # 环境变量优先于 config
    monkeypatch.setenv("MORSEL_LANG", "zh")
    monkeypatch.setattr(i18n, "_lang", None)
    assert i18n.current_lang() == "zh"
