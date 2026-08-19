"""A 档真值解析器单测（bench/truth/*.py + bench/make_truth.py）。

全部用内联 fixture（字符串写进临时目录），不碰网络、不碰 bench/corpus/ 里的真实语料。
断言重点：标题层级、表格单元格、公式被换成占位、噪声被剥掉、编码兜底、
以及「抓不到标题时省略 headings 键」这条与 metrics.score_document 的硬约定。
"""
from __future__ import annotations

import gzip
import io
import json
import tarfile
import zipfile

from bench import metrics as M
from bench.truth import from_html, from_latex, from_xml


# ============ HTML ============

def test_html_headings_tables_paragraphs():
    html = """<html><head><title>Doc</title></head><body>
    <h1>Chapter One</h1><p>First paragraph of text.</p>
    <h2>Sub A</h2><h3>Deeper</h3><p>Second paragraph.</p>
    <table><caption>Table cap</caption>
      <tr><th>Year</th><th>Value</th></tr>
      <tr><td>2024</td><td>7</td></tr></table>
    </body></html>"""
    t = from_html.parse_html(html)
    assert t["headings"] == ["Chapter One", "Sub A", "Deeper"]
    assert t["heading_levels"] == [1, 2, 3]
    assert t["tables"] == [[["Year", "Value"], ["2024", "7"]]]
    assert "First paragraph of text." in t["paragraphs"]
    assert "Table cap" in t["paragraphs"]          # caption 成段落，排在表格之前
    assert t["text"].index("Table cap") < t["text"].index("2024")
    # 真值本身必须能被指标消费（自己跟自己比 = 满分）
    assert M.char_sim(t["text"], t["text"]) == 1.0
    assert M.cjk_inner_spaces(t["text"]) == 0


def test_html_strips_noise_and_reference_sections():
    html = """<html><body>
    <nav><a href="/">Home</a> Navigation junk</nav>
    <aside>sidebar junk</aside>
    <script>var x = "script junk";</script><style>.a{color:red}</style>
    <noscript>noscript junk</noscript>
    <h1>Title <span class="mw-editsection">[edit]</span></h1>
    <p>Body sentence<sup class="reference">[1]</sup> continues [2].</p>
    <div hidden><p>hidden junk</p><div>nested inside hidden</div></div>
    <p>visible after hidden</p>
    <table class="infobox"><tr><td>Born</td><td>1900</td></tr></table>
    <h2>References</h2><p>reference junk entry</p>
    <h2>Kept Section</h2><p>tail sentence</p>
    <footer>footer junk</footer></body></html>"""
    t = from_html.parse_html(html)
    for junk in ("Navigation junk", "sidebar junk", "script junk", ".a{", "noscript junk",
                 "hidden junk", "nested inside hidden", "reference junk", "footer junk",
                 "[edit]", "[1]", "Born"):
        assert junk not in t["text"], junk
    assert t["headings"] == ["Title", "Kept Section"]
    assert "visible after hidden" in t["text"] and "tail sentence" in t["text"]
    assert t["tables"] == []                       # 信息框不算数据表格
    assert t["note"]["infobox_dropped"] == 1
    assert t["note"]["wikipedia_cleanup"] is True


def test_html_sec_style_without_semantic_tags_omits_headings():
    body = "".join(f"<div><font size=2>Item {i}. Some filing text about risk factors.</font></div>"
                   for i in range(60))
    html = f"<html><body>{body}<table><tr><td>Revenue</td><td>1,234</td></tr>" \
           "<tr><td>Cost</td><td>567</td></tr></table></body></html>"
    t = from_html.parse_html(html)
    # 没有 h1–h6：省略 headings 键（不是写 []），score_document 因此跳过 heading_f1
    assert "headings" not in t and "heading_levels" not in t
    assert "headings_unavailable" in t["note"]
    assert t["tables"] == [[["Revenue", "1,234"], ["Cost", "567"]]]
    scored = M.score_document("# Item 1\n\nRevenue", t)
    assert "heading_f1" not in scored and "cell_f1" in scored


def test_html_encoding_bom_meta_and_loose():
    assert from_html.decode_html_bytes("<p>ü</p>".encode("utf-8-sig")) == "<p>ü</p>"
    latin = '<html><head><meta charset="iso-8859-1"></head><body><p>Grün</p></body></html>'
    t = from_html.parse_html(latin.encode("iso-8859-1"))
    assert "Grün" in t["text"]
    # 声明 utf-8 但字节是 latin-1 → 宽松解码，不抛异常
    broken = b'<html><head><meta charset="utf-8"></head><body><p>Gr\xfcn</p></body></html>'
    assert "Gr" in from_html.parse_html(broken)["text"]


def test_html_nested_table_and_math_placeholder():
    html = """<body><p>text</p>
    <table><tr><th>h1</th><th>h2</th></tr>
    <tr><td>outer</td><td><table><tr><td>in</td><td>ner</td></tr></table></td></tr></table>
    <p>formula <math><mi>x</mi><mo>=</mo><mn>1</mn></math> here</p></body>"""
    t = from_html.parse_html(html)
    assert t["tables"] == [[["h1", "h2"], ["outer", "in ner"]]]   # 表中表拍平进外层单元格
    assert "mi" not in t["text"] and "x=1" not in t["text"]
    assert t["note"]["math_stripped"] is True
    assert "formula here" in t["text"]


def test_html_layout_tables_demoted_to_paragraphs():
    # EUR-Lex 的 recital：每条 "(n) 正文" 都包在一个单行两列 <table> 里 —— 不是数据表格
    rows = "".join(
        f'<table><tr><td>({i})</td><td>Recital number {i} with a reasonably long sentence '
        f'of legal text that goes on and on.</td></tr></table>' for i in range(1, 4))
    t = from_html.parse_html(f"<body><h1>Directive</h1>{rows}"
                             "<table><tr><th>Year</th><th>Value</th></tr>"
                             "<tr><td>2024</td><td>7</td></tr></table></body>")
    assert t["tables"] == [[["Year", "Value"], ["2024", "7"]]]   # 只留真表格
    assert t["note"]["layout_tables_demoted"] == 3
    assert "(1) Recital number 1" in t["text"]                   # 正文一字不少
    assert any(p.startswith("(2) Recital number 2") for p in t["paragraphs"])
    # 关掉开关就原样保留（给需要区分 HTML/PDF 两种口径的场合留后门）
    keep = from_html.parse_html(f"<body>{rows}</body>", demote_layout_tables=False)
    assert len(keep["tables"]) == 3


def test_html_layout_table_keeps_text_as_paragraph():
    # SEC 文件里满是单单元格「版面表格」：不算表格，但里面的正文必须留下
    t = from_html.parse_html("<div><table><tr><td><p>Item 1.</p><p>Business overview.</p>"
                             "</td></tr></table></div>")
    assert t["tables"] == []
    assert "Item 1. Business overview." in t["text"]


# ============ LaTeX ============

LATEX_DOC = r"""
\documentclass{article}
\usepackage{amsmath}
\title{On Sparse Matrices}
\author{A. Muster\thanks{footnote} \and B. K\"onig}
\begin{document}
\maketitle
\begin{abstract}
We study sparse matrices $A \in \mathbb{R}^{n}$. % comment must vanish
\end{abstract}
\section{Introduction}
\label{sec:intro}
Prior work~\cite{smith2020} shows that
\begin{equation}
  E = mc^2
\end{equation}
holds for 50\% of cases.
\subsection{Method}
\begin{itemize}
  \item First item
  \item Second item
\end{itemize}
\begin{table}[t]
\caption{Results overview}
\begin{tabular}{lrr}
\toprule
\multicolumn{1}{c}{Metric} & Ours & Base \\
\midrule
Recall & 0.91 & 0.80 \\
\bottomrule
\end{tabular}
\end{table}
\begin{figure}[h]
\includegraphics[width=0.5\textwidth]{plot.png}
\caption{A figure caption}
\end{figure}
\section{Conclusion}
Sch\"on und na\"iv, \ss{}.
\begin{thebibliography}{9}
\bibitem{smith2020} Smith, J. Some Paper Title. 2020.
\end{thebibliography}
\end{document}
"""


def test_latex_headings_levels_and_math_placeholder():
    t = from_latex.parse_latex(LATEX_DOC)
    assert t["headings"] == ["On Sparse Matrices", "Introduction", "Method", "Conclusion"]
    assert t["heading_levels"] == [1, 2, 3, 2]      # \title=1, section=2, subsection=3
    assert t["note"]["math_stripped"] is True and t["note"]["math_spans"] >= 2
    for gone in ("mc^2", "$", "mathbb", "\\in", "E ="):
        assert gone not in t["text"], gone
    assert "holds for 50% of cases." in t["text"]   # \% → %，公式处只留空格
    assert "comment must vanish" not in t["text"]


def test_latex_tables_captions_and_bibliography():
    t = from_latex.parse_latex(LATEX_DOC)
    assert t["tables"] == [[["Metric", "Ours", "Base"], ["Recall", "0.91", "0.80"]]]
    assert "Results overview" in t["paragraphs"]    # \caption 保留
    assert "A figure caption" in t["paragraphs"]
    assert "plot.png" not in t["text"]              # 浮动体其余内容丢弃
    assert "Some Paper Title" not in t["text"]      # thebibliography 整段丢弃
    assert "Schön und naïv, ß." in t["text"]        # 重音命令展开
    assert "First item" in t["paragraphs"] and "Second item" in t["paragraphs"]


def test_latex_chapter_shifts_levels():
    src = r"""\documentclass{book}\title{Bk}\begin{document}
    \chapter{Ch}\section{Sec}\subsection{Sub}Body text here.\end{document}"""
    t = from_latex.parse_latex(src)
    assert t["headings"] == ["Bk", "Ch", "Sec", "Sub"]
    assert t["heading_levels"] == [1, 2, 3, 4]
    assert t["note"]["heading_level_map"]["chapter"] == 2


def test_latex_main_file_detection_and_inputs(tmp_path):
    main = r"""\documentclass{article}\title{Multi File}\begin{document}
    \input{sections/intro}
    \include{sections/second}
    \end{document}"""
    intro = "\\section{Intro}\nIntro body sentence.\n"
    second = "\\section{Second}\nSecond body sentence.\n"
    tgz = tmp_path / "eprint.tar.gz"
    with tarfile.open(tgz, "w:gz") as tf:
        for name, content in (("main.tex", main), ("sections/intro.tex", intro),
                              ("sections/second.tex", second), ("junk.bib", "@article{x}")):
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    t = from_latex.parse(tgz)
    assert t["note"]["main_tex"] == "main.tex"
    assert t["headings"] == ["Multi File", "Intro", "Second"]
    assert "Intro body sentence." in t["text"] and "Second body sentence." in t["text"]

    # 裸 .tex.gz 单文件 e-print 也要能吃
    gz = tmp_path / "solo.tex.gz"
    gz.write_bytes(gzip.compress(br"""\documentclass{article}\begin{document}
    \section{Solo}Solo body sentence.\end{document}"""))
    assert from_latex.parse(gz)["headings"] == ["Solo"]


# ============ XML ============

GII_XML = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE dokumente SYSTEM "http://www.gesetze-im-internet.de/dtd/1.01/gii-norm.dtd">
<dokumente builddate="2024">
 <norm>
  <metadaten><jurabk>TestG</jurabk><langue>Testgesetz &uuml;ber Dinge</langue></metadaten>
  <textdaten><text format="XML"><Content><P>Vorspann Text.</P></Content></text></textdaten>
 </norm>
 <norm>
  <metadaten><gliederungseinheit><gliederungskennzahl>010</gliederungskennzahl>
   <gliederungsbez>Kapitel 1</gliederungsbez>
   <gliederungstitel>Allgemeines</gliederungstitel></gliederungseinheit></metadaten>
  <textdaten><text/></textdaten>
 </norm>
 <norm>
  <metadaten><enbez>&#167; 1</enbez><titel>Zweck</titel></metadaten>
  <textdaten><text format="XML"><Content>
   <P>Dieses Gesetz regelt Dinge.</P>
   <DL Type="arabic"><DT>1.</DT><DD><LA>erster Punkt</LA></DD>
    <DT>2.</DT><DD><LA>zweiter Punkt</LA></DD></DL>
   <table><tgroup cols="2"><tbody>
     <row><entry>Jahr</entry><entry>Wert</entry></row>
     <row><entry>2024</entry><entry>7</entry></row></tbody></tgroup></table>
  </Content></text>
  <fussnoten><Content><P>Fussnote gehoert nicht in den Text</P></Content></fussnoten></textdaten>
 </norm>
</dokumente>"""

JP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Law Era="Heisei" Lang="ja" LawType="Act" Num="86" Year="17">
<LawNum>平成十七年法律第八十六号</LawNum>
<LawBody>
<LawTitle>会社法</LawTitle>
<TOC><TOCLabel>目次</TOCLabel><TOCChapter><ChapterTitle>目次の章題</ChapterTitle></TOCChapter></TOC>
<MainProvision>
 <Part Num="1"><PartTitle>第一編 総則</PartTitle>
  <Chapter Num="1"><ChapterTitle>第一章 通則</ChapterTitle>
   <Article Num="1"><ArticleCaption>（趣旨）</ArticleCaption><ArticleTitle>第一条</ArticleTitle>
    <Paragraph Num="1"><ParagraphNum/>
     <ParagraphSentence><Sentence>会社の設立について定める。</Sentence></ParagraphSentence>
     <Item Num="1"><ItemTitle>一</ItemTitle>
      <ItemSentence><Column Num="1"><Sentence>株式会社</Sentence></Column></ItemSentence></Item>
    </Paragraph>
    <Paragraph Num="2"><ParagraphNum>2</ParagraphNum>
     <ParagraphSentence><Sentence>前項の規定は適用しない。</Sentence></ParagraphSentence></Paragraph>
   </Article>
  </Chapter>
 </Part>
</MainProvision>
<AppdxTable><AppdxTableTitle>別表第一</AppdxTableTitle>
 <TableStruct><Table>
  <TableRow><TableColumn><Sentence>区分</Sentence></TableColumn>
   <TableColumn><Sentence>金額</Sentence></TableColumn></TableRow>
  <TableRow><TableColumn><Sentence>甲</Sentence></TableColumn>
   <TableColumn><Sentence>千円</Sentence></TableColumn></TableRow></Table></TableStruct>
</AppdxTable>
</LawBody></Law>"""


def test_xml_gii_structure():
    t = from_xml.parse_xml(GII_XML)
    assert t["note"]["dialect"] == "gii-norm"
    assert t["headings"] == ["Testgesetz über Dinge", "Kapitel 1 Allgemeines", "§ 1 Zweck"]
    assert t["heading_levels"] == [1, 2, 3]
    assert t["tables"] == [[["Jahr", "Wert"], ["2024", "7"]]]
    assert "1. erster Punkt" in t["paragraphs"] and "2. zweiter Punkt" in t["paragraphs"]
    assert t["paragraphs"].count("erster Punkt") == 0        # DT/DD 只出一次，不重复
    assert "Fussnote" not in t["text"]                       # 脚注不进正文
    assert "Vorspann Text." in t["text"]


def test_xml_egov_jp_structure():
    t = from_xml.parse_xml(JP_XML)
    assert t["note"]["dialect"] == "egov-jp"
    assert t["note"]["toc_dropped"] is True
    assert "目次の章題" not in t["text"]
    assert t["headings"][:4] == ["会社法", "第一編 総則", "第一章 通則", "第一条 （趣旨）"]
    assert t["heading_levels"][:4] == [1, 2, 3, 4]
    assert "別表第一" in t["headings"]
    assert t["tables"] == [[["区分", "金額"], ["甲", "千円"]]]
    assert "一 株式会社" in t["paragraphs"]
    assert "2 前項の規定は適用しない。" in t["paragraphs"]
    assert M.cjk_inner_spaces("".join(t["paragraphs"][1:2])) == 0


def test_xml_generic_fallback_and_missing_headings():
    t = from_xml.parse_xml("<root><doc><body>Some text here</body>"
                           "<table><tr><td>a</td><td>b</td></tr></table></doc></root>")
    assert t["note"]["dialect"] == "generic"
    assert "headings" not in t                       # 抓不到标题 → 省略键
    assert "headings_unavailable" in t["note"]
    assert t["tables"] == [[["a", "b"]]]
    # 结构残缺（没有 metadaten/textdaten）也不能崩
    broken = from_xml.parse_xml("<dokumente><norm><metadaten/></norm>"
                                "<norm><textdaten><text><Content><P>nur Text</P>"
                                "</Content></text></textdaten></norm></dokumente>")
    assert "nur Text" in broken["text"]


def test_xml_zip_and_gz_inputs(tmp_path):
    z = tmp_path / "law.xml.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("BJNR0001.xml", GII_XML)
        zf.writestr("tiny.xml", "<dokumente/>")
    t = from_xml.parse(z)
    assert t["note"]["zip_member"] == "BJNR0001.xml"     # 取最大的 xml 成员
    assert "Zweck" in t["text"]
    g = tmp_path / "law.xml.gz"
    g.write_bytes(gzip.compress(JP_XML.encode("utf-8")))
    assert from_xml.parse(g)["note"]["dialect"] == "egov-jp"


# ============ make_truth 驱动 ============

def _shard(tmp_path, rows) -> tuple:
    corpus = tmp_path / "corpus"
    (corpus / "manifest.d").mkdir(parents=True)
    (corpus / "real").mkdir(parents=True)
    (corpus / "manifest.d" / "s1.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    return corpus, corpus / "manifest.d"


def test_make_truth_dispatch_skip_and_force(tmp_path, capsys):
    from bench import make_truth

    rows = [
        {"id": "h1", "truth_src": "real/a.html", "truth": "real/h1.truth.json"},
        {"id": "l1", "truth_src": "real/b.tex", "truth": "real/l1.truth.json"},
        {"id": "x1", "truth_src": "real/c.xml", "truth": "real/x1.truth.json"},
        {"id": "no_src", "truth_src": "", "truth": ""},
        {"id": "missing", "truth_src": "real/nope.html", "truth": "real/missing.truth.json"},
    ]
    corpus, mdir = _shard(tmp_path, rows)
    (corpus / "real" / "a.html").write_text("<h1>H</h1><p>Body text of the page.</p>",
                                            encoding="utf-8")
    (corpus / "real" / "b.tex").write_text(
        r"\documentclass{article}\begin{document}\section{S}Body $x$ here.\end{document}",
        encoding="utf-8")
    (corpus / "real" / "c.xml").write_text(JP_XML, encoding="utf-8")

    rc = make_truth.main(["--manifest-dir", str(mdir)])
    out = capsys.readouterr().out
    assert rc == 2 and "成功 3" in out and "跳过 1" in out and "失败 1" in out
    assert "FileNotFoundError" in out
    h = json.loads((corpus / "real" / "h1.truth.json").read_text(encoding="utf-8"))
    assert h["headings"] == ["H"] and "Body text of the page." in h["text"]
    x = json.loads((corpus / "real" / "x1.truth.json").read_text(encoding="utf-8"))
    assert x["note"]["dialect"] == "egov-jp"
    stamp = (corpus / "real" / "h1.truth.json").stat().st_mtime_ns

    # 第二轮：已有真值全部跳过
    make_truth.main(["--manifest-dir", str(mdir)])
    assert "成功 0" in capsys.readouterr().out
    assert (corpus / "real" / "h1.truth.json").stat().st_mtime_ns == stamp

    # --force 重新生成
    make_truth.main(["--manifest-dir", str(mdir), "--force"])
    assert "成功 3" in capsys.readouterr().out
    # 分片文件本身绝不能被改写
    assert len((mdir / "s1.jsonl").read_text(encoding="utf-8").strip().splitlines()) == 5


def test_make_truth_missing_manifest_dir_and_sniffing(tmp_path, capsys):
    from bench import make_truth

    assert make_truth.main(["--manifest-dir", str(tmp_path / "nope")]) == 1
    assert "没有找到分片文件" in capsys.readouterr().out

    # 后缀认不出来时按文件头嗅探
    p = tmp_path / "weird.bin"
    p.write_text("<!DOCTYPE html><html><body><h1>Sniffed</h1><p>text body</p></body></html>",
                 encoding="utf-8")
    assert make_truth.pick_parser(p)[0] == "html"
    q = tmp_path / "weird2.bin"
    q.write_text(r"\documentclass{book}\begin{document}x\end{document}", encoding="utf-8")
    assert make_truth.pick_parser(q)[0] == "latex"
    r = tmp_path / "weird3.bin"
    r.write_text('<?xml version="1.0"?><Law><LawBody><LawTitle>T</LawTitle></LawBody></Law>',
                 encoding="utf-8")
    assert make_truth.pick_parser(r)[0] == "xml"
