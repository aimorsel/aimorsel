"""RTL 视觉序 → 逻辑序还原（rtl_text.py + morsel 接线），纯逻辑，免 Java。"""

from __future__ import annotations

import json
from pathlib import Path

import morsel
import rtl_text as R

# 逻辑序样本与它的视觉序（逐字符反转 RTL run，拉丁/数字 run 保持内部顺序）
LOGICAL_LINE = "قرار اتخذته الجمعية العامة في 7 تشرين الأول/أكتوبر 2022"
VISUAL_LINE = "2022 ربوتكأ/لوألا نيرشت 7 يف ةماعلا ةيعمجلا هتذختا رارق"


def test_visual_to_logical_pure_and_mixed():
    assert R.visual_to_logical(VISUAL_LINE) == LOGICAL_LINE
    # 拉丁 run 内部顺序不动，整体位置随基础方向翻转
    assert R.visual_to_logical("A/RES/77/1 ةدحتملا ممألا") == "الأمم المتحدة A/RES/77/1"
    # 无 RTL 字符：原样返回（含空白）
    assert R.visual_to_logical("17 October 2022  ") == "17 October 2022  "
    # 首尾空白保留
    assert R.visual_to_logical("  ةدحتملا \n") == "  المتحدة \n"


def test_visual_to_logical_ltr_dominant_line_only_flips_rtl_runs():
    assert R.visual_to_logical("the word ةدحتملا means united") == "the word المتحدة means united"


def test_visual_to_logical_brackets():
    # 生成器写逻辑码位：")أ(" 反过来正好是 "(أ)"
    assert R.visual_to_logical("لامعألا لودج نم )أ( 69 دنبلا") == "البند 69 (أ) من جدول الأعمال"
    # 紧贴拉丁 run 的括号跟着拉丁走（Word 把 "(A/77/L.3)" 整体当 LTR run）
    got = R.visual_to_logical("](A/77/L.3) ةيسيئر ةنجل ىلإ ةلاحإلا نود[")
    assert got == "[دون الإحالة إلى لجنة رئيسية (A/77/L.3)]"
    # 脚注引用 "2030(1)،"
    assert R.visual_to_logical("،)1(2030 ماعل") == "لعام 2030(1)،"
    # mirror=True：生成器写的是字形码位时整篇镜像
    assert R.visual_to_logical("(أ) 69 دنبلا", mirror=True) == "البند 69 (أ)"


def test_visual_to_logical_is_involution_on_logical_input_shape():
    # 逻辑序文本再过一遍会被弄乱——这就是为什么必须先探测（下面的测试）再动手
    assert R.visual_to_logical(LOGICAL_LINE) != LOGICAL_LINE


def test_looks_visual_rtl_detection():
    visual_doc = "\n".join([VISUAL_LINE, "،ثراوكلا تالاح يف ةيثوغ ةدناسم نمو", "ئراوطلا تالاح يف ةدحتملا ممألا اهمدقت"])
    logical_doc = "\n".join([LOGICAL_LINE, "ومن مساندة غوثية في حالات الكوارث،", "تقدمها الأمم المتحدة في حالات الطوارئ"])
    assert R.looks_visual_rtl(visual_doc)
    assert not R.looks_visual_rtl(logical_doc)
    # 希伯来文：尾形字母出现在词首 = 视觉序
    assert R.looks_visual_rtl("םולש םלוע םירבד םינש\nםיבר םישנא")
    assert not R.looks_visual_rtl("שלום עולם דברים שנים\nאנשים רבים")
    # 证据不足（不到 3 票）保守判否；纯拉丁文本判否
    assert not R.looks_visual_rtl("ةدحتملا")
    assert not R.looks_visual_rtl("hello world\nplain text")


def test_restore_rtl_text_markdown_keeps_syntax():
    src = "\n".join([
        "# ةماعلا ةيعمجلا",
        "",
        "- ةدحتملا ممألا",
        "1. ةدحتملا ممألا",
        "",
        "|ةلوؤسملا ةهجلا|ئمزلا راطإلا|",
        "|---|---|",
        "|أ ةرقف<br><br>ب ةرقف|x|",
        "",
        "```",
        "ةدحتملا ممألا",
        "```",
        "17 October 2022",
    ])
    out, n = R.restore_rtl_text(src, "md")
    lines = out.split("\n")
    assert lines[0] == "# الجمعية العامة"
    assert lines[2] == "- الأمم المتحدة"
    assert lines[3] == "1. الأمم المتحدة"
    assert lines[5] == "|الجهة المسؤولة|الإطار الزمئ|"
    assert lines[6] == "|---|---|"
    # 单元格里的 <br> 是分段边界：段各自还原、段序不变
    assert lines[7] == "|فقرة أ<br><br>فقرة ب|x|"
    assert lines[10] == "ةدحتملا ممألا"  # 围栏代码块不动
    assert lines[12] == "17 October 2022"
    assert n == 5


def test_restore_rtl_text_json_and_html():
    js = '{\n  "kids" : [ {\n    "type" : "paragraph",\n    "content" : "ةدحتملا ممألا"\n  }, {\n    "content" : "plain"\n  } ]\n}\n'
    out, n = R.restore_rtl_text(js, "json")
    assert n == 1
    assert '    "content" : "الأمم المتحدة"\n' in out
    assert json.loads(out)["kids"][0]["content"] == "الأمم المتحدة"
    assert out.endswith("\n")
    html = "<h1>ةماعلا ةيعمجلا</h1>\n<p>ةدحتملا ممألا <b>x</b></p>\n"
    out, n = R.restore_rtl_text(html, "html")
    assert out == "<h1>الجمعية العامة</h1>\n<p>الأمم المتحدة <b>x</b></p>\n"
    assert n == 2


def test_restore_with_reference_keeps_physical_line_order():
    # 引擎第一遍把两个物理行拼成一行；整行反转会把行序倒过来，参照第二遍（保留物理行）能还原
    l1_vis, l2_vis = "1/70 رارقلا )1(", "313/69 رارقلا )2("
    text1 = f"{l1_vis} {l2_vis}\n\nplain\n"
    text2 = f"{l1_vis}\n{l2_vis}\n\nplain\n"
    out, n = R.restore_rtl_text_with_reference(text1, text2, "md")
    assert out == "(1) القرار 1/70 (2) القرار 313/69\n\nplain\n"
    assert n == 1
    # 没有参照：行序倒置（这是退化行为，仍然字序正确）
    out2, _ = R.restore_rtl_text(text1, "md")
    assert out2.startswith("(2) القرار 313/69 (1) القرار 1/70")
    # 参照对不上（块数不同）→ 退回逐行还原，不抛异常
    out3, _ = R.restore_rtl_text_with_reference(text1, "something else entirely", "md")
    assert out3 == out2


def test_restore_with_reference_json_and_table():
    j1 = '{\n  "content" : "1/70 رارقلا )1( 313/69 رارقلا )2("\n}\n'
    j2 = '{\n  "content" : "1/70 رارقلا )1(\\n313/69 رارقلا )2("\n}\n'
    out, n = R.restore_rtl_text_with_reference(j1, j2, "json")
    assert json.loads(out)["content"] == "(1) القرار 1/70 (2) القرار 313/69"
    assert n == 1
    # 表格：第二遍把单元格内物理换行写成单个 <br>，段落边界是 <br><br>
    t1 = "|أ رطس ب رطس|x|\n|---|---|\n"
    t2 = "|أ رطس<br>ب رطس|x|\n|---|---|\n"
    out, n = R.restore_rtl_text_with_reference(t1, t2, "md")
    assert out == "|سطر أ سطر ب|x|\n|---|---|\n"


def test_decide_mirror_votes_by_bracket_balance():
    logical_codes = "\n".join(["لامعألا لودج نم )أ( 69 دنبلا"] * 3)
    shape_codes = "\n".join(["لامعألا لودج نم (أ) 69 دنبلا"] * 3)
    assert not R.decide_mirror(logical_codes, "txt")
    assert R.decide_mirror(shape_codes, "txt")


def test_restore_rtl_products_wiring(tmp_path: Path, monkeypatch):
    md = tmp_path / "doc.md"
    js = tmp_path / "doc.json"
    png = tmp_path / "doc.png"
    visual_doc = "\n\n".join([VISUAL_LINE, "،ثراوكلا تالاح يف ةيثوغ ةدناسم نمو", "ئراوطلا تالاح يف ةدحتملا ممألا اهمدقت"])
    md.write_text(visual_doc + "\n", encoding="utf-8")
    js.write_text('{\n  "content" : "%s"\n}\n' % VISUAL_LINE, encoding="utf-8")
    png.write_bytes(b"\x89PNG")
    calls: list[list[str]] = []

    def fake_second_pass(pdf, formats, options, use_ocr):
        calls.append(formats)
        return {"doc.md": md.read_text(encoding="utf-8")}  # 与第一遍相同（无多行段落），json 无参照走逐行

    monkeypatch.setattr(morsel, "_rtl_second_pass", fake_second_pass)
    n = morsel.restore_rtl_products(Path("doc.pdf"), [md, js, png], ["markdown", "json"], morsel.ConvertOptions())
    assert n == 4  # md 3 行 + json 1 行
    assert calls == [["json", "markdown"]]
    assert md.read_text(encoding="utf-8").splitlines()[0] == LOGICAL_LINE
    assert json.loads(js.read_text(encoding="utf-8"))["content"] == LOGICAL_LINE
    assert png.read_bytes() == b"\x89PNG"
    # 逻辑序文档：探测不通过，零改动、不发起第二遍
    calls.clear()
    md.write_text("\n\n".join([LOGICAL_LINE] * 3), encoding="utf-8")
    assert morsel.restore_rtl_products(Path("doc.pdf"), [md], ["markdown"], morsel.ConvertOptions()) == 0
    assert calls == []
