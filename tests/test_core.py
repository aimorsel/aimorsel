"""纯逻辑单元测试：不碰 Java、不碰文件系统重活。"""

from __future__ import annotations

from pathlib import Path

import morsel


def test_parse_pages_spec():
    assert morsel._parse_pages_spec("1,3,5-7") == {1, 3, 5, 6, 7}
    assert morsel._parse_pages_spec(" 2 , 4 ") == {2, 4}
    assert morsel._parse_pages_spec("abc") is None
    assert morsel._parse_pages_spec("") is None


def test_parse_selection():
    assert morsel.parse_selection("all", 3) == [0, 1, 2]
    assert morsel.parse_selection("1,3", 3) == [0, 2]
    assert morsel.parse_selection("1-3", 5) == [0, 1, 2]


def test_estimate_tokens():
    assert morsel.estimate_tokens("abcdefgh") == 2          # 4 ASCII 字符 1 token
    assert morsel.estimate_tokens("中文四个字") == 5          # CJK 每字 1 token
    assert morsel.estimate_tokens("") == 0


def test_human_size():
    assert morsel.human_size(500) == "500B"
    assert "KB" in morsel.human_size(2048)
    assert "MB" in morsel.human_size(5 * 1024 * 1024)


def test_is_supported_input():
    assert morsel.is_supported_input(Path("a.pdf"))
    assert morsel.is_supported_input(Path("b.DOCX"))
    assert morsel.is_supported_input(Path("c.png"))
    assert not morsel.is_supported_input(Path("~$lock.docx"))
    assert not morsel.is_supported_input(Path(".hidden.pdf"))
    assert not morsel.is_supported_input(Path("notes.txt"))


def test_find_inputs(tmp_path):
    (tmp_path / "a.pdf").write_bytes(b"x")
    (tmp_path / "b.docx").write_bytes(b"x")
    (tmp_path / "~$b.docx").write_bytes(b"x")
    (tmp_path / "c.txt").write_bytes(b"x")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "d.xlsx").write_bytes(b"x")
    names = [p.name for p in morsel.find_inputs(tmp_path)]
    assert names == ["a.pdf", "b.docx", "d.xlsx"]


def test_options_signature_changes():
    formats = ["markdown", "json"]
    base = morsel.options_signature(formats, morsel.ConvertOptions())
    assert base == morsel.options_signature(formats, morsel.ConvertOptions())
    # 影响输出的选项变了 -> 签名变
    assert base != morsel.options_signature(formats, morsel.ConvertOptions(sanitize=True))
    assert base != morsel.options_signature(["json"], morsel.ConvertOptions())
    assert base != morsel.options_signature(formats, morsel.ConvertOptions(rag_chunks=True))
    # 分块关着时块大小不影响签名
    assert base == morsel.options_signature(formats, morsel.ConvertOptions(chunk_tokens=999))


def test_table_to_grid_span_anchor():
    node = {
        "type": "table", "number of rows": 2, "number of columns": 2,
        "rows": [
            {"cells": [{"row number": 1, "column number": 1, "content": "跨行",
                        "row span": 2, "column span": 1},
                       {"row number": 1, "column number": 2, "content": "B1"}]},
            {"cells": [{"row number": 2, "column number": 2, "content": "B2"}]},
        ],
    }
    assert morsel._table_to_grid(node) == [["跨行", "B1"], ["", "B2"]]


SYNTH_TREE = {
    "file name": "t.pdf", "number of pages": 2,
    "kids": [
        {"type": "heading", "heading level": 1, "page number": 1, "content": "第一章"},
        {"type": "paragraph", "page number": 1, "content": "正文内容" * 10},
        {"type": "list", "page number": 1,
         "list items": [{"type": "list item", "page number": 1, "content": "条目甲"}]},
        {"type": "heading", "heading level": 2, "page number": 2, "content": "小节"},
        {"type": "table", "page number": 2, "number of rows": 1, "number of columns": 1,
         "rows": [{"cells": [{"row number": 1, "column number": 1, "content": "唯一格"}]}]},
    ],
}


def test_flatten_and_chunk():
    blocks: list = []
    morsel._flatten_blocks(SYNTH_TREE["kids"], blocks)
    kinds = [b["kind"] for b in blocks]
    assert kinds == ["heading", "text", "text", "heading", "table"]
    assert blocks[2]["text"] == "- 条目甲"

    chunks = morsel.chunk_blocks(blocks, "t.pdf", max_tokens=400)
    assert chunks, "应至少产出一块"
    assert chunks[0]["heading_path"] == ["第一章"]
    assert chunks[0]["pages"][0] == 1
    assert all(c["source"] == "t.pdf" for c in chunks)


def test_split_long_text():
    text = "句子一。句子二。" * 100
    pieces = morsel._split_long_text(text, max_tokens=50)
    assert len(pieces) > 1
    assert "".join(pieces).replace("\n", "") == text.replace("\n", "")


def test_clean_error_plain():
    assert morsel.clean_error(ValueError("boom"), "") == "boom"


def test_normalize_compat_chars_kangxi_and_ligature():
    # macOS Quartz PDF 常见产物：康熙部首区的「⼀⼤⼈」+ Office 的 ﬁ 连字，显示相同但码位不同
    text, n = morsel.normalize_compat_chars("⼀个⼤⼈ ﬁle ⺟")
    assert text == "一个大人 file 母"
    assert n == 5
    assert "一" in text and "⼀" not in text


def test_normalize_compat_chars_leaves_normal_text_alone():
    # 全角标点、正常汉字、上标等不动——不是全文 NFKC
    src = "全角，标点。正常汉字 x² ½ abc"
    text, n = morsel.normalize_compat_chars(src)
    assert (text, n) == (src, 0)


def test_normalize_produced_files(tmp_path: Path):
    md = tmp_path / "a.md"
    js = tmp_path / "a.json"
    png = tmp_path / "a.png"
    md.write_text("⼀个", encoding="utf-8")
    js.write_text('{"content": "⼤"}', encoding="utf-8")
    png.write_bytes(b"\x89PNG\xff")
    assert morsel.normalize_produced_files([md, js, png]) == 2
    assert md.read_text(encoding="utf-8") == "一个"
    assert js.read_text(encoding="utf-8") == '{"content": "大"}'
    assert png.read_bytes() == b"\x89PNG\xff"  # 非文本后缀不碰


def test_check_ocr_server_bypasses_proxy(monkeypatch):
    """bench #4：健康检查不能走系统代理（本地回环经代理会拿到 502 而误判离线）。"""
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")  # 一个必然连不上的代理
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200 if self.path == "/health" else 404)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        assert morsel.check_ocr_server(url)
        assert not morsel.check_ocr_server(f"http://127.0.0.1:{srv.server_address[1] + 1 if srv.server_address[1] < 65000 else 1}")
    finally:
        srv.shutdown()
    assert morsel.DEFAULT_HYBRID_URL.startswith("http://127.0.0.1")


def test_heading_is_noise():
    """明显不是标题的那几类（bench issue #9）。"""
    assert morsel.heading_is_noise("a")                    # 图注面板字母
    assert morsel.heading_is_noise("√")
    assert morsel.heading_is_noise("MUST")                 # RFC 2119 关键词
    assert morsel.heading_is_noise("should not")
    assert morsel.heading_is_noise("L_2016157EN.01000101.xml")
    assert not morsel.heading_is_noise("記")               # CJK 单字是正经小标题
    assert not morsel.heading_is_noise("背景")
    assert not morsel.heading_is_noise("DIRECTIVES")       # 全大写但不是关键词
    assert not morsel.heading_is_noise("§ 1 Grundsatz")
    assert not morsel.heading_is_noise("")


def test_list_paragraph_flags():
    """「(1) …」条款段落整块判定（bench issue #10）。"""
    long_a, long_b = "(1) " + "x" * 90, "(2) " + "y" * 90
    assert morsel.list_paragraph_flags([long_a, long_b]) == [True, True]
    # 同一块里的短条款跟着一起还原，不留半截在列表里
    assert morsel.list_paragraph_flags(["(1) 短。", long_b, "真列表项"]) == [True, True, False]
    # 只有一个编号项：更可能是真的列表，放过
    assert morsel.list_paragraph_flags([long_a, "真列表项"]) == [False, False]
    # 都是短的：没有段落级证据，放过
    assert morsel.list_paragraph_flags(["(1) 甲", "(2) 乙"]) == [False, False]


def test_tidy_products(tmp_path: Path):
    """产物就地清理：md / json / html 三种格式的改写一致。"""
    import json as _json

    long_a, long_b = "(1) " + "x" * 90, "(2) " + "y" * 90
    md = tmp_path / "d.md"
    md.write_text(f"# T\n\n#### a\n\n##### MUST\n\n- {long_a}\n- {long_b}\n- echt\n\n## 背景\n",
                  encoding="utf-8")
    html = tmp_path / "d.html"
    html.write_text(f"<h1>T</h1><h4>a</h4><ul><li><p>{long_a}</p></li><li><p>{long_b}</p></li>"
                    f"<li><p>echt</p></li></ul>", encoding="utf-8")
    tree = {"file name": "d.pdf", "number of pages": 1, "kids": [
        {"type": "heading", "heading level": 4, "page number": 1, "content": "a"},
        {"type": "heading", "heading level": 2, "page number": 1, "content": "背景"},
        {"type": "list", "page number": 1, "number of list items": 3, "list items": [
            {"type": "list item", "page number": 1, "content": long_a},
            {"type": "list item", "page number": 1, "content": long_b},
            {"type": "list item", "page number": 1, "content": "echt"}]},
    ]}
    js = tmp_path / "d.json"
    js.write_text(_json.dumps(tree, ensure_ascii=False), encoding="utf-8")
    txt = tmp_path / "d.txt"
    txt.write_text("- (1) 纯文本没有结构标记，别动\n", encoding="utf-8")

    assert morsel.tidy_products([md, html, js, txt]) > 0
    lines = md.read_text(encoding="utf-8").splitlines()
    assert "#### a" not in lines and "a" in lines            # 降级成正文，内容不丢
    assert "##### MUST" not in lines and "MUST" in lines
    assert long_a in lines and f"- {long_b}" not in lines    # 编号段落脱掉列表符号
    assert "- echt" in lines and "## 背景" in lines          # 真列表项与正经标题不动
    out_html = html.read_text(encoding="utf-8")
    assert "<h4>" not in out_html and "<p>a</p>" in out_html
    assert f"<p>{long_a}</p>" in out_html and "<li><p>echt</p></li>" in out_html
    kids = _json.loads(js.read_text(encoding="utf-8"))["kids"]
    assert [k["type"] for k in kids] == ["paragraph", "heading", "paragraph", "paragraph", "list"]
    assert kids[0]["content"] == "a" and "heading level" not in kids[0]
    assert [i["content"] for i in kids[4]["list items"]] == ["echt"]
    assert txt.read_text(encoding="utf-8") == "- (1) 纯文本没有结构标记，别动\n"


def test_tidy_keeps_list_item_subtree(tmp_path: Path):
    """降级的列表项要整份搬走：kids 里常挂着整段嵌套内容，只抄 content 会把正文删掉。"""
    import json as _json

    long_a, long_b = "(1) " + "x" * 90, "(2) " + "y" * 90
    tree = {"file name": "x.pdf", "number of pages": 1, "kids": [
        {"type": "list", "page number": 1, "pdfua_tag": "L", "number of list items": 3,
         "list items": [
             {"type": "list item", "page number": 1, "content": long_a,
              "kids": [{"type": "paragraph", "page number": 1, "content": "嵌套正文"}]},
             {"type": "list item", "page number": 1, "content": long_b},
             {"type": "list item", "page number": 1, "content": "真列表项"}]}]}
    js = tmp_path / "t.json"
    js.write_text(_json.dumps(tree, ensure_ascii=False), encoding="utf-8")
    morsel.tidy_products([js])
    kids = _json.loads(js.read_text(encoding="utf-8"))["kids"]
    assert [k["type"] for k in kids] == ["paragraph", "paragraph", "list"]
    assert kids[0]["kids"][0]["content"] == "嵌套正文"      # 子树不能丢
    assert kids[2]["pdfua_tag"] == "L"                      # 列表节点自身字段保留
    assert kids[2]["number of list items"] == 1


def test_tidy_markdown_keeps_blocks_separate(tmp_path: Path):
    """脱掉列表符号的条款段落要自成一段，否则会被 Markdown 当成上一条列表项的续行。"""
    long_a, long_b = "(1) " + "x" * 90, "(2) " + "y" * 90
    md = tmp_path / "m.md"
    md.write_text(f"- 真列表\n- {long_a}\n- {long_b}\n- 真列表2\n", encoding="utf-8")
    morsel.tidy_products([md])
    lines = md.read_text(encoding="utf-8").split("\n")
    for clause in (long_a, long_b):
        i = lines.index(clause)
        assert lines[i - 1] == "" and lines[i + 1] == ""
    assert "- 真列表" in lines and "- 真列表2" in lines


def test_tidy_html_nested_list(tmp_path: Path):
    """嵌套列表只动最内层：朴素的非贪婪正则会把外层开头配到内层结尾，两层 <li> 混成一组。"""
    long_a = "(1) " + "a" * 90
    html = tmp_path / "h.html"
    html.write_text(f"<ul><li><p>§ 2</p><ul><li><p>{long_a}</p></li></ul></li>"
                    f"<li><p>普通项</p></li></ul>", encoding="utf-8")
    morsel.tidy_products([html])
    out = html.read_text(encoding="utf-8")
    assert f"<p>{long_a}</p>" in out and "<li><p>普通项</p></li>" in out
    assert out.startswith("<ul><li><p>§ 2</p>")     # 外层列表结构不动


def test_strip_list_bullet():
    """列表项里残留的项目符号字形要剥掉，但别碰负号/编号/单独一个符号（冒烟第 ③ 条）。"""
    assert morsel.strip_list_bullet("• First point") == ("First point", True)
    assert morsel.strip_list_bullet("▪ 第一条") == ("第一条", True)
    assert morsel.strip_list_bullet("\uf0b7 Word 私有区符号") == ("Word 私有区符号", True)
    assert morsel.strip_list_bullet("- minus") == ("- minus", False)      # 可能是负号或强调
    assert morsel.strip_list_bullet("3. plain") == ("3. plain", False)    # 有序编号有意义
    assert morsel.strip_list_bullet("•x") == ("•x", False)                # 后面没空白，不是符号
    assert morsel.strip_list_bullet("•") == ("•", False)                  # 剥完就空了
    assert morsel.strip_list_bullet("") == ("", False)


def test_tidy_strips_bullets_in_all_formats(tmp_path: Path):
    """md / json / html 三条路径同口径剥离，且幂等。"""
    import json as _json

    md = tmp_path / "b.md"
    md.write_text("## 小标题\n\n- • First point\n- ▪ Second point\n- plain\n", encoding="utf-8")
    html = tmp_path / "b.html"
    html.write_text("<ul><li>• First</li><li><p>▪ Second</p></li><li>plain</li></ul>",
                    encoding="utf-8")
    js = tmp_path / "b.json"
    js.write_text(_json.dumps({"file name": "b.pdf", "number of pages": 1, "kids": [
        {"type": "list", "page number": 1, "number of list items": 2, "list items": [
            {"type": "list item", "page number": 1, "content": "• First"},
            {"type": "list item", "page number": 1, "content": "plain"}]}]},
        ensure_ascii=False), encoding="utf-8")

    assert morsel.tidy_products([md, html, js]) == 5
    assert md.read_text(encoding="utf-8") == "## 小标题\n\n- First point\n- Second point\n- plain\n"
    assert html.read_text(encoding="utf-8") == \
        "<ul><li>First</li><li><p>Second</p></li><li>plain</li></ul>"
    items = _json.loads(js.read_text(encoding="utf-8"))["kids"][0]["list items"]
    assert [i["content"] for i in items] == ["First", "plain"]

    assert morsel.tidy_products([md, html, js]) == 0      # 幂等


def test_tidy_bullet_and_numbered_paragraph_stack(tmp_path: Path):
    """"• (1) 长条款…" 要先剥符号再判条款段落，两条规则不能互相挡住。"""
    import json as _json

    long_a, long_b = "• (1) " + "x" * 90, "• (2) " + "y" * 90
    js = tmp_path / "c.json"
    js.write_text(_json.dumps({"file name": "c.pdf", "number of pages": 1, "kids": [
        {"type": "list", "page number": 1, "number of list items": 2, "list items": [
            {"type": "list item", "page number": 1, "content": long_a},
            {"type": "list item", "page number": 1, "content": long_b}]}]},
        ensure_ascii=False), encoding="utf-8")
    morsel.tidy_products([js])
    kids = _json.loads(js.read_text(encoding="utf-8"))["kids"]
    assert [k["type"] for k in kids] == ["paragraph", "paragraph"]
    assert kids[0]["content"].startswith("(1) ")


def test_dispatch_subcommand(monkeypatch, tmp_path: Path):
    """`morsel gui|web|mcp` 子命令分发：按签名传参、不吞多余参数、同名文件让位。"""
    import sys
    import types

    seen: list = []
    fake_web = types.ModuleType("morsel_web")
    fake_web.main = lambda argv: seen.append(argv) or 7
    fake_gui = types.ModuleType("morsel_gui")
    fake_gui.main = lambda: 5
    monkeypatch.setitem(sys.modules, "morsel_web", fake_web)
    monkeypatch.setitem(sys.modules, "morsel_gui", fake_gui)

    assert morsel._dispatch_subcommand([]) is None            # 没参数 = 交互模式
    assert morsel._dispatch_subcommand(["a.pdf"]) is None     # 普通输入
    assert morsel._dispatch_subcommand(["web", "--port", "9"]) == 7
    assert seen == [["--port", "9"]]                          # 收 argv 的入口拿到剩余参数
    assert morsel._dispatch_subcommand(["gui"]) == 5          # 不收参数的入口直接调
    assert morsel._dispatch_subcommand(["gui", "x"]) == 2     # 多余参数要报错，不能悄悄吞掉

    assert morsel._dispatch_subcommand(["gui", "--help"]) == 0   # 不收参数的入口也要能查帮助

    monkeypatch.chdir(tmp_path)
    (tmp_path / "web").write_text("x", encoding="utf-8")
    assert morsel._dispatch_subcommand(["web"]) is None       # 真有同名文件时子命令让位
    (tmp_path / "gui").mkdir()                                # 目录同理（web/ gui/ 是常见目录名）
    assert morsel._dispatch_subcommand(["gui"]) is None


def test_subcommand_help_text():
    """主命令 --help 必须列出三个子命令，否则装了安装包的用户只能从官网发现它们。"""
    text = morsel.subcommand_help_text()
    for name in morsel.SUBCOMMANDS:
        assert f"morsel {name}" in text
        assert f"morsel-{name}" in text or name in text       # 逃生舱也要提到
