"""format_adapters 单元测试：解析、渲染、图片包装。全程不需要 Java。"""

from __future__ import annotations

import format_adapters as fa


def test_docx_tree(sample_docx):
    tree = fa.parse_office(sample_docx)
    assert tree["number of pages"] == 1
    types = [k["type"] for k in tree["kids"]]
    assert types == ["heading", "paragraph", "heading", "list", "table", "paragraph"]
    headings = [k for k in tree["kids"] if k["type"] == "heading"]
    assert headings[0]["heading level"] == 1
    assert headings[1]["heading level"] == 2
    lst = next(k for k in tree["kids"] if k["type"] == "list")
    assert [i["content"] for i in lst["list items"]] == ["苹果", "香蕉"]


def test_docx_heading_level():
    assert fa._docx_heading_level("Heading 3") == 3
    assert fa._docx_heading_level("heading 2") == 2
    assert fa._docx_heading_level("Title") == 1
    assert fa._docx_heading_level("Heading") == 1
    assert fa._docx_heading_level("Normal") is None


def test_xlsx_tree(sample_xlsx):
    tree = fa.parse_office(sample_xlsx)
    assert tree["number of pages"] == 2
    tables = [k for k in tree["kids"] if k["type"] == "table"]
    assert len(tables) == 2
    grid_texts = [c["content"] for row in tables[0]["rows"] for c in row["cells"]]
    assert "42" in grid_texts          # 整数不带小数点
    assert "3.5" in grid_texts         # 浮点保留
    assert "2026-01-31" in grid_texts  # 日期 ISO 格式
    assert tables[0]["page number"] == 1
    assert tables[1]["page number"] == 2


def test_cell_str():
    import datetime

    assert fa._cell_str(None) == ""
    assert fa._cell_str(5.0) == "5"
    assert fa._cell_str(3.14) == "3.14"
    assert fa._cell_str(datetime.date(2026, 7, 25)) == "2026-07-25"
    assert fa._cell_str("文本") == "文本"


def test_pptx_tree(sample_pptx):
    tree = fa.parse_office(sample_pptx)
    assert tree["number of pages"] == 2
    contents = [k.get("content", "") for k in tree["kids"]]
    assert any("项目启动" in c for c in contents)
    assert any("演讲者备注" in c for c in contents)
    pages = {k["page number"] for k in tree["kids"]}
    assert pages == {1, 2}


def test_render_markdown_and_text(sample_docx):
    tree = fa.parse_office(sample_docx)
    md = fa.render_markdown(tree)
    assert "# 年度报告" in md
    assert "## 财务部分" in md
    assert "- 苹果" in md
    assert "| 项目 | 金额 |" in md
    txt = fa.render_text(tree)
    assert "年度报告" in txt and "#" not in txt.split("\n")[0]


def test_render_page_separator(sample_xlsx):
    tree = fa.parse_office(sample_xlsx)
    md = fa.render_markdown(tree, "\n\n=第 %page-number% 页=\n\n")
    assert "=第 2 页=" in md


def test_render_html_escapes(sample_docx):
    tree = fa.parse_office(sample_docx)
    tree["kids"].append({"type": "paragraph", "page number": 1, "content": "<b>不许注入</b>"})
    html = fa.render_html(tree)
    assert "&lt;b&gt;不许注入&lt;/b&gt;" in html
    assert "<h1>年度报告</h1>" in html


def test_image_to_pdf(sample_png, tmp_path):
    out = tmp_path / "wrapped.pdf"
    fa.image_to_pdf(sample_png, out)
    assert out.stat().st_size > 500
    import pdfplumber

    with pdfplumber.open(out) as doc:
        assert len(doc.pages) == 1


def test_image_to_pdf_multiframe(tmp_path):
    from PIL import Image

    gif = tmp_path / "anim.gif"
    frames = [Image.new("RGB", (50, 50), c) for c in ("red", "green", "blue")]
    frames[0].save(gif, save_all=True, append_images=frames[1:])
    out = tmp_path / "anim.pdf"
    fa.image_to_pdf(gif, out)
    import pdfplumber

    with pdfplumber.open(out) as doc:
        assert len(doc.pages) == 3


def test_parse_office_unknown_suffix(tmp_path):
    import pytest

    bad = tmp_path / "x.epub"
    bad.write_bytes(b"zz")
    with pytest.raises(fa.AdapterError):
        fa.parse_office(bad)


def test_parse_office_corrupt_docx(tmp_path):
    import pytest

    bad = tmp_path / "broken.docx"
    bad.write_bytes(b"not a real docx")
    with pytest.raises(fa.AdapterError) as ei:
        fa.parse_office(bad)
    assert "broken.docx" in str(ei.value)


# ---------------------------------------------------------------- HTML

_MESSY_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>页面标题</title>
<style>p{color:red}</style><script>var x="<p>no</p>";</script></head>
<body>
<h1>报告&amp;总结</h1>
<p>第一段
跨行 <b>加粗</b> 文本。<br>第二行</p>
<div>裸文本在 div 里
<p>嵌套段落<p>未闭合段落
<h2>列表</h2>
<ul><li>苹果<li>香蕉<ul><li>小蕉</li></ul></li><li><p>橙子</p></li></ul>
<table><caption>表 1</caption><tr><th>A</th><th>B</th></tr><tr><td>1<td>2</tr>
<tr><td><p>三</p><p>四</p></td><td>&nbsp;</td></tr></table>
<pre>code line 1
   indented 2</pre>
<template><p>hidden</p></template><p hidden>hid</p><p>after hidden</p>
<ol><li>one</li><li>two</li></ol>
</body></html>"""


def test_html_tree(tmp_path):
    path = tmp_path / "page.html"
    path.write_text(_MESSY_HTML, encoding="utf-8")
    tree = fa.parse_office(path)
    assert tree["number of pages"] == 1
    kids = tree["kids"]
    types = [k["type"] for k in kids]
    assert types == ["heading", "paragraph", "paragraph", "paragraph", "paragraph",
                     "heading", "list", "paragraph", "table", "paragraph", "paragraph", "list"]
    assert kids[0] == {"type": "heading", "heading level": 1, "page number": 1,
                       "content": "报告&总结"}          # 实体解码；有 h1 时不补 <title>
    assert kids[1]["content"] == "第一段 跨行 加粗 文本。\n第二行"  # 源码换行折叠，<br> 保留
    assert [k["content"] for k in kids[2:5]] == ["裸文本在 div 里", "嵌套段落", "未闭合段落"]
    lst = kids[6]
    assert [i["content"] for i in lst["list items"]] == ["苹果", "香蕉", "小蕉", "橙子"]  # 嵌套拍平
    assert kids[7]["content"] == "表 1"                 # caption 成独立段落，位于表格之前
    table = kids[8]
    grid = [[c["content"] for c in row["cells"]] for row in table["rows"]]
    assert grid == [["A", "B"], ["1", "2"], ["三 四", ""]]  # 未闭合 td、&nbsp; 空单元格
    assert kids[9]["content"] == "code line 1\n   indented 2"  # pre 保留缩进
    assert kids[10]["content"] == "after hidden"        # template/hidden 被丢弃且不吞后文
    assert [i["content"] for i in kids[11]["list items"]] == ["one", "two"]


def test_html_title_fallback_and_encoding(tmp_path):
    # 没有 h1 时用 <title> 补首个一级标题；GBK 声明编码能正确读出
    path = tmp_path / "gbk.htm"
    body = "<html><head><meta charset='gbk'><title>标题 甲</title></head><body><p>正文</p></body></html>"
    path.write_bytes(body.encode("gbk"))
    tree = fa.parse_office(path)
    assert [k["content"] for k in tree["kids"]] == ["标题 甲", "正文"]
    assert tree["kids"][0]["heading level"] == 1


def test_html_extensions_routed():
    import morsel
    from pathlib import Path

    for ext in (".html", ".htm", ".xhtml"):
        assert ext in fa.ADAPTER_EXTENSIONS
        assert morsel.is_supported_input(Path(f"a{ext}"))
    assert morsel.decide_ocr(Path("a.html"), morsel.ConvertOptions(), server_ok=True) == (False, "")


_MATH_HTML = """<html><body>
<p>需要知道 <span class="mwe-math-element"><span style="display:none"><math xmlns="http://www.w3.org/1998/Math/MathML" alttext="{\\displaystyle n-1}">
  <semantics><mrow><mi>n</mi><mo>&#x2212;<!-- − --></mo><mn>1</mn></mrow>
  <annotation encoding="application/x-tex">{\\displaystyle n-1}</annotation></semantics></math></span>
<img src="x.svg" class="mwe-math-fallback-image-inline" aria-hidden="true" alt="{\\displaystyle n-1}"/></span> 的質因數。</p>
<p>只有 alttext：<math alttext="\\alpha+\\beta"><mi>α</mi></math>；只有词元：<math><mi>x</mi><mo>=</mo><mn>2</mn>
<annotation-xml encoding="MathML-Content"><ci>ignored</ci></annotation-xml></math>；只有回退图：<img class="mwe-math-fallback-image-inline" alt="{\\textstyle e^{i\\pi}}"/></p>
<p>独立公式<math display="block"><annotation encoding="application/x-tex">\\sum_{k} k</annotation></math>之后</p>
<table><tr><td><math><mi>y</mi></math></td><td>ok</td></tr></table>
</body></html>"""


def test_html_math_kept_as_latex(tmp_path):
    """bench issue #1：MathML 公式不再整块丢弃，以 $...$ 文本形式保留，且不重复输出回退图片的 alt。"""
    path = tmp_path / "math.html"
    path.write_text(_MATH_HTML, encoding="utf-8")
    kids = fa.parse_office(path)["kids"]
    assert kids[0]["content"] == "需要知道 $n-1$ 的質因數。"
    assert kids[1]["content"] == "只有 alttext：$\\alpha+\\beta$；只有词元：$x=2$；只有回退图：$e^{i\\pi}$"
    assert kids[2]["content"] == "独立公式\n$$\\sum_{k} k$$\n之后"
    grid = [[c["content"] for c in row["cells"]] for row in kids[3]["rows"]]
    assert grid == [["$y$", "ok"]]
    assert fa._clean_tex("{\\displaystyle \\mathbb {N} }") == "\\mathbb {N}"
    assert fa._clean_tex("  a  b ") == "a b"


def test_html_math_edge_cases(tmp_path):
    """审查逼出来的边界：回退图去重只针对紧跟公式的那张；aria-hidden 视觉副本不重复；标题/列表/单元格里
    独立公式降为行内；未闭合 <math> 不吞后文；_clean_tex 不剥并列壳。"""
    def kids(html):
        path = tmp_path / "e.html"
        path.write_text(html, encoding="utf-8")
        return fa.parse_office(path)["kids"]

    img = '<img class="mwe-math-fallback-image-inline" alt="{\\displaystyle n}">'
    assert kids(f"<p>Let {img} be prime. Then {img} divides.</p>")[0]["content"] == "Let $n$ be prime. Then $n$ divides."
    katex = ('<p>Value <span class="katex"><span class="katex-mathml"><math><semantics><mrow><mi>x</mi></mrow>'
             '<annotation encoding="application/x-tex">x^2</annotation></semantics></math></span>'
             '<span class="katex-html" aria-hidden="true"><span>x</span><span>2</span></span></span> end</p>')
    assert kids(katex)[0]["content"] == "Value $x^2$ end"
    mathjax = '<p>A <nobr aria-hidden="true">x2</nobr><span class="MJX_Assistive_MathML"><math><mi>x</mi><mn>2</mn></math></span> B</p>'
    assert kids(mathjax)[0]["content"] == "A $x2$ B"
    ks = kids('<h2>Head <math display="block"><mi>c</mi></math> more</h2><ul><li>item <math display="block"><mi>a</mi></math></li></ul>'
              '<table><tr><td>x<math display="block"><mi>d</mi></math></td></tr></table>')
    assert ks[0]["content"] == "Head $c$ more"
    assert ks[1]["list items"][0]["content"] == "item $a$"
    assert ks[2]["rows"][0]["cells"][0]["content"] == "x$d$"
    ks = kids("<p>before <math><mi>x</mi></p><p>next</p><h1>H</h1>")
    assert [k["content"] for k in ks] == ["before $x$", "next", "H"]
    assert fa._clean_tex("{\\displaystyle a}{\\displaystyle b}") == "{\\displaystyle a}{\\displaystyle b}"
    assert fa._clean_tex("{\\displaystyle a} + {\\displaystyle b}") == "{\\displaystyle a} + {\\displaystyle b}"
    assert fa._clean_tex("{\\displaystyle\\alpha }") == "\\alpha"
    assert fa._clean_tex("{\\displaystyle {a}{b}}") == "{a}{b}"


def test_html_hidden_metadata_dropped(tmp_path):
    """XBRL 隐藏事实与行内 display:none 不进正文（bench issue #11）。"""
    path = tmp_path / "x.html"
    path.write_text(
        "<html><body>"
        '<div style="display:none"><ix:header><ix:hidden>'
        '<ix:nonNumeric name="dei:EntityCentralIndexKey">0000732712</ix:nonNumeric>'
        "</ix:hidden></ix:header></div>"
        '<p>可见段落 <span style="display: none;">排序键</span>尾巴</p>'
        '<p><span style="visibility:hidden">隐形</span>后半</p>'
        '<div style="display:none"><div>嵌套也丢</div></div>'
        "<p>结尾</p></body></html>", encoding="utf-8")
    kids = fa.parse_office(path)["kids"]
    assert [k["content"] for k in kids] == ["可见段落 尾巴", "后半", "结尾"]


def test_html_hidden_void_tag_does_not_swallow_rest(tmp_path):
    """空元素没有结束标签：隐藏的 <img>/<input>/<hr> 不能把后文全吞掉。

    写成 `<img ...>`（无自闭合斜杠）时 html.parser 不会补 end 事件，
    若按普通元素起 skip 就永远关不掉——追踪像素一出现，整篇正文归零。
    """
    for void in ('<img src="px.gif" style="display:none">',
                 '<input type="hidden" style="display:none">',
                 '<hr style="visibility:hidden">',
                 '<br style="display:none">',
                 "<meta name=x hidden>"):
        path = tmp_path / "v.html"
        path.write_text(f"<html><body><p>前</p>{void}<p>后</p></body></html>", encoding="utf-8")
        assert [k["content"] for k in fa.parse_office(path)["kids"]] == ["前", "后"], void


def test_promote_numbered_headings(tmp_path):
    """零标题文档里的编号短行提升成标题（bench issue #12）。"""
    def kids(body: str):
        path = tmp_path / "law.html"
        path.write_text(f"<html><body>{body}</body></html>", encoding="utf-8")
        return fa.parse_office(path)["kids"]

    body = ("<p>第一章　総則</p><p>（目的）</p>"
            "<p>第一条　この法律は、賃金の最低額を保障することを目的とする。</p>"
            "<p>第二章　最低賃金</p><p>第一節　総則</p>"
            "<p>第三条　最低賃金額は、時間によつて定めるものとする。</p>")
    ks = kids(body)
    got = [(k["type"], k.get("heading level"), k["content"]) for k in ks]
    assert got[0] == ("heading", 2, "第一章　総則")
    assert got[1][0] == "paragraph"                     # （目的）不带编号，不提升
    assert got[2][0] == "paragraph"                     # 条文正文太长且有句末标点，不提升
    assert got[3] == ("heading", 2, "第二章　最低賃金")
    assert got[4] == ("heading", 3, "第一節　総則")

    # 拉丁编号；level 与法条层次一致
    ks = kids("<p>CHAPTER I</p><p>Article 1</p><p>Article 2</p><p>§ 5 Begriffe</p>")
    assert [(k["type"], k.get("heading level")) for k in ks] == [
        ("heading", 2), ("heading", 4), ("heading", 4), ("heading", 4)]

    # 少于 3 处命中 → 一律不动
    assert all(k["type"] == "paragraph" for k in kids("<p>第一条　甲</p><p>Article 2</p><p>别的</p>"))
    # 文档已有真实标题层级 → 不碰
    ks = kids("<h2>已有标题</h2><p>第一条　甲</p><p>第二条　乙</p><p>第三条　丙</p>")
    assert [k["type"] for k in ks] == ["heading", "paragraph", "paragraph", "paragraph"]
    # 反复出现的是页眉，不是标题：光秃编号出现两次即判页眉，带标题文字的允许「目次 + 正文」两次
    assert all(k["type"] == "paragraph" for k in
               kids("<p>PART I</p><p>x</p><p>PART I</p><p>y</p><p>PART II</p><p>z</p>"))
    ks = kids("<p>第二章　最低賃金</p><p>x</p><p>第二章　最低賃金</p><p>第三章　罰則</p><p>第四章　附則</p>")
    assert [k["type"] for k in ks] == ["heading", "paragraph", "heading", "heading", "heading"]
    # 多语言编号（EUR-Lex 的 de/es/fr 版）与法语的序数词第一条
    ks = kids("<p>KAPITEL I</p><p>Artikel 1</p><p>Artículo 2</p><p>Article premier</p><p>Sección 3</p>")
    assert [k["type"] for k in ks] == ["heading"] * 5
    # 正文里引用条号（后接句子）不提升
    assert all(k["type"] == "paragraph" for k in
               kids("<p>第五条 の規定は適用しない。</p><p>Article 3 shall apply.</p><p>§ 9 gilt nicht.</p>"))


def test_html_hidden_element_with_omitted_end_tag(tmp_path):
    """HTML 允许省略 </p></li></td> 等结束标签：隐藏元素不能因此把后文全吞掉。"""
    def kids(body: str):
        path = tmp_path / "o.html"
        path.write_text(f"<html><body>{body}</body></html>", encoding="utf-8")
        return fa.parse_office(path)["kids"]

    assert [k["content"] for k in kids('<p style="display:none">隐<p>甲</p><p>乙</p>')] == ["甲", "乙"]
    assert [k["content"] for k in kids("<p hidden>隐<p>甲</p><p>乙</p>")] == ["甲", "乙"]
    ks = kids('<ul><li style="display:none">隐<li>甲<li>乙</ul><p>尾</p>')
    assert [i["content"] for i in ks[0]["list items"]] == ["甲", "乙"] and ks[1]["content"] == "尾"
    ks = kids('<table><tr><td style="display:none">隐<td>甲</table><p>尾</p>')
    assert [[c["content"] for c in r["cells"]] for r in ks[0]["rows"]] == [["甲"]]
    assert ks[1]["content"] == "尾"
    # 正常闭合与同名嵌套照旧全丢；内部元素没闭合也不能把后文带走
    assert [k["content"] for k in kids('<div style="display:none"><div>隐</div>还是隐</div><p>甲</p>')] == ["甲"]
    assert [k["content"] for k in kids('<div style="display:none"><span>隐</div><p>甲</p>')] == ["甲"]
    assert [k["content"] for k in kids('<ul><li style="display:none"><span>隐</ul><p>甲</p>')] == ["甲"]
    # display:none !important 也算隐藏
    assert [k["content"] for k in kids('<p>甲<span style="display:none !important">隐</span>乙</p>')] == ["甲乙"]


def _bars_image(angle: float = 0.0):
    """造一张「横线条纹」测试图（模拟文字行）；angle 非 0 时转成倾斜的。"""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (600, 800), "white")
    draw = ImageDraw.Draw(img)
    for y in range(60, 740, 40):
        draw.rectangle([80, y, 520, y + 14], fill="black")
    if angle:
        img = img.rotate(angle, resample=Image.BICUBIC, expand=True, fillcolor=(255, 255, 255))
    return img


def test_detect_skew(tmp_path):
    """倾斜校正：转正角度要能估准，正的图不能被乱转（bench issue #8）。"""
    assert abs(fa.detect_skew(_bars_image())) < fa.DESKEW_MIN_DEG
    for applied in (-3.0, -2.0, 2.5, 4.0):
        # 图被转了 applied 度，要摆正就得反着转回去
        got = fa.detect_skew(_bars_image(applied))
        assert abs(got + applied) < 0.3, (applied, got)
    # 超出 ±5 度不认（页面本身横放，不是扫描歪了）
    assert fa.detect_skew(_bars_image(30)) == 0.0 or abs(fa.detect_skew(_bars_image(30))) <= fa.DESKEW_MAX_DEG


def test_image_to_pdf_deskew(tmp_path):
    from PIL import Image

    straight, tilted = tmp_path / "s.png", tmp_path / "t.png"
    _bars_image().save(straight)
    _bars_image(3.0).save(tilted)
    out = tmp_path / "o.pdf"

    assert fa.image_to_pdf(straight, out) == 0.0          # 正的图零改动
    before = Image.open(tilted).size
    turned = fa.image_to_pdf(tilted, out)
    assert 2.7 <= turned <= 3.3
    assert fa.image_to_pdf(tilted, out, deskew=False) == 0.0
    assert Image.open(tilted).size == before               # 只改临时 PDF，不动源图


def test_image_to_pdf_deskew_degenerate(tmp_path):
    """极细长/1×1 图片不能因为倾斜校正而把「能转」变成「转失败」。"""
    from PIL import Image

    out = tmp_path / "o.pdf"
    for size in ((1, 1), (1000, 2), (2, 1000)):
        path = tmp_path / f"s{size[0]}x{size[1]}.png"
        Image.new("RGB", size, "white").save(path)
        assert fa.image_to_pdf(path, out) == 0.0, size
