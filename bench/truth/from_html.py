"""HTML → A 档真值 JSON（纯标准库 `html.parser`）。

覆盖三类语料：
1. **一般语义 HTML**（EUR-Lex / OpenStax / RFC / W3C）：h1–h6 → headings，`<table>` → tables，
   块级元素边界切 paragraphs。
2. **Wikipedia REST/Parsoid HTML**：额外剥掉编辑链接（`.mw-editsection`、`[edit]`）、
   `[1]` 式参考文献上标（`sup.reference` / `.mw-ref`）、目录（`#toc`/`.toc`）、
   导航框（`.navbox` / `.vertical-navbox` / `.sidebar`）、维护模板（`.ambox`/`.metadata`/`.noprint`），
   并从「参考文献 / 外部链接 / 延伸阅读」这类标题起整节丢弃（到同级或更高级标题为止）。
   **信息框（`table.infobox`）默认丢弃**——它是版面表格而非数据表格，各引擎要么摊平成正文、
   要么整块丢掉，算进 cell_f1 只会让所有引擎一起虚低；取舍记在 `note.infobox_dropped`。
3. **SEC EDGAR 10-K/10-Q**：全是 `<font>`/`<div>`，没有 h1–h6。这时**省略 `headings` 键**
   （不是写空数组），run.py/score_document 就跳过 heading_f1；`text` 与 `tables` 照产。
   判据：没抓到任何标题，或标题数 < 3 而段落数 > 50（少量孤立 h 标签不足以当全文标题真值）。

数学公式（`<math>` / `.mwe-math-element`）统一换成一个空格，与 `from_latex.py` 保持一致，
并在 `note.math_stripped` 记录。

**不 import 项目根的 `format_adapters.py`**（真值必须与被测代码解耦）。
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

from . import build_truth, norm_ws

# ---------- 常量 ----------

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
        "param", "source", "track", "wbr"}

#: 整段丢弃的标签（含其子树）。**不含 head**——`<title>` 在里面，要留着当兜底标题
DROP_TAGS = {"script", "style", "template", "svg", "iframe", "noscript",
             "nav", "aside", "footer", "button", "select", "textarea",
             "object", "canvas", "audio", "video", "map"}

#: 公式标签：整段丢弃，但换成一个空格占位并记 math_stripped
MATH_TAGS = {"math", "annotation", "semantics"}

#: 块级元素：进出都切段（`<br>` 不在内——它只算段内换行，切段会把段落打得太碎）
BLOCK_TAGS = {"p", "div", "section", "article", "main", "header", "blockquote", "pre",
              "li", "dd", "dt", "dl", "ul", "ol", "figure", "figcaption", "address",
              "details", "summary", "fieldset", "hgroup", "hr", "center"}

HEADINGS = {f"h{i}": i for i in range(1, 7)}

#: class / id 命中即丢弃子树（Wikipedia 噪声 + 通用导航噪声）
DROP_CLASS = {
    "mw-editsection", "mw-editsection-bracket", "mw-editsection-divider",
    "mw-jump-link", "mw-empty-elt", "mw-cite-backlink", "mw-references-wrap",
    "mw-reflink-text", "mw-headline-anchor", "mw-indicators",
    "reference", "references", "reflist", "refbegin", "reference-text-hidden",
    "navbox", "navbox-inner", "vertical-navbox", "sidebar", "sistersitebox",
    "toc", "toctitle", "tocnumber", "catlinks", "printfooter", "noprint",
    "ambox", "metadata", "hatnote", "shortdescription", "mbox-small",
    "navigation", "breadcrumb", "sidenav", "site-header", "site-footer",
    "cookie-banner", "skip-link", "screen-reader-text", "sr-only", "visually-hidden",
    "mwe-math-fallback-image-inline", "mwe-math-fallback-image-display",
}
# 注意：不要把 "References"/"Notes" 之类**标题锚点 id** 放进来——那会只丢掉标题本身、
# 留下整节正文，反而绕过了 _drop_noise_sections 的整节丢弃
DROP_ID = {"toc", "mw-navigation", "mw-panel", "siteNotice", "footer", "catlinks",
           "siteSub", "jump-to-nav", "mw-hidden-catlinks"}
DROP_ROLE = {"navigation", "banner", "contentinfo", "complementary", "search", "menu"}

#: 表格 class 命中即整表丢弃（版面表格 / 信息框 / 导航框）
DROP_TABLE_CLASS = {"infobox", "infobox_v2", "infobox_v3", "navbox", "vertical-navbox",
                    "sidebar", "ambox", "metadata", "toccolours-nav", "nowraplinks",
                    "mbox-small", "succession-box"}

#: 标题命中即整节丢弃（到同级或更高级标题为止）
DROP_SECTIONS = {
    "references", "reference", "notes", "notes and references", "footnotes", "citations",
    "external links", "further reading", "bibliography", "sources", "see also",
    "works cited", "literature", "external link",
    "einzelnachweise", "weblinks", "literatur", "anmerkungen", "siehe auch", "fußnoten",
    "参考文献", "参考资料", "外部链接", "外部連結", "延伸阅读", "参见", "另见", "脚注", "註釋", "注释",
    "関連項目", "外部リンク", "参照", "脚註",
    "notas", "referencias", "enlaces externos", "véase también",
    "notes et références", "liens externes", "voir aussi", "bibliographie",
}

_EDIT_MARK = re.compile(r"\[\s*(?:edit|edit\s+source|编辑|編集|編輯|bearbeiten|modifier|editar)\s*\]",
                        re.I)
#: 只在 Wikipedia 模式下剥的参考文献上标残留
_REF_MARK = re.compile(r"\[\s*(?:\d{1,3}|[a-z]|note\s*\d*|nb\s*\d*|注\s*\d*|citation needed|来源请求)\s*\]", re.I)


# ---------- 编码 ----------

_META_CHARSET = re.compile(rb"""<meta[^>]*?charset\s*=\s*["']?\s*([\w:.+-]+)""", re.I)


def decode_html_bytes(data: bytes) -> str:
    """BOM > `<meta charset>` > utf-8 > utf-8 宽松（errors="replace"）。返回值不带 BOM。"""
    for bom, enc in ((b"\xef\xbb\xbf", "utf-8-sig"), (b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16")):
        if data.startswith(bom):
            try:
                return data.decode(enc).lstrip("﻿")
            except Exception:
                break
    m = _META_CHARSET.search(data[:8192])
    if m:
        enc = m.group(1).decode("ascii", "ignore").strip().lower()
        if enc not in ("utf-8", "utf8"):
            try:
                return data.decode(enc)
            except Exception:
                pass
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    return data.decode("utf-8", errors="replace")


# ---------- 解析器 ----------

class _TruthHtmlParser(HTMLParser):
    """事件驱动，容忍未闭合标签。产出阅读顺序块序列 self.blocks。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[tuple[str, object]] = []
        self.buf: list[str] = []
        # 丢弃子树：记「触发标签名 + 同名嵌套深度」（不能按「任意 DROP 标签」计数，
        # 否则 <div hidden> 里嵌一个 <div> 就会把后文全吞掉）
        self._skip_tag: str | None = None
        self._skip_depth = 0
        self._h_level: int | None = None
        self._tables: list[dict] = []          # 表格栈（支持表中表）
        self._in_caption = False
        self._title_parts: list[str] = []
        self._in_title = False
        self.is_wiki = False
        self.math_stripped = False
        self.infobox_dropped = 0
        self._pre_depth = 0

    # -- 工具 --

    @property
    def _skipping(self) -> bool:
        return self._skip_tag is not None

    def _cur_cell(self) -> list[str] | None:
        if self._tables and self._tables[-1]["cell"] is not None:
            return self._tables[-1]["cell"]
        return None

    def _flush_para(self) -> None:
        cell = self._cur_cell()
        if cell is not None:  # 单元格里的块级边界只当空格，不切段
            cell.append(" ")
            return
        if self._h_level is not None:
            return
        text = norm_ws("".join(self.buf))
        self.buf = []
        if text:
            self.blocks.append(("para", text))

    def _emit(self, kind: str, payload: object) -> None:
        self.blocks.append((kind, payload))

    @staticmethod
    def _classes(attrs: dict) -> set[str]:
        return {c.strip().lower() for c in (attrs.get("class") or "").split() if c.strip()}

    def _should_drop(self, tag: str, attrs: dict) -> bool:
        if "hidden" in attrs and (attrs.get("hidden") in (None, "", "hidden", "true")):
            return True
        if (attrs.get("aria-hidden") or "").lower() == "true":
            return True
        style = (attrs.get("style") or "").replace(" ", "").lower()
        if "display:none" in style or "visibility:hidden" in style:
            return True
        if (attrs.get("role") or "").lower() in DROP_ROLE:
            return True
        if (attrs.get("id") or "") in DROP_ID:
            return True
        cls = self._classes(attrs)
        if cls & DROP_CLASS:
            return True
        if tag == "table" and cls & DROP_TABLE_CLASS:
            self.infobox_dropped += 1
            return True
        return False

    # -- 事件 --

    def handle_starttag(self, tag: str, attrs_list) -> None:  # noqa: C901
        tag = tag.lower()
        attrs = {k.lower(): v for k, v in attrs_list}
        if any(c.startswith("mw-") for c in self._classes(attrs)):
            self.is_wiki = True
        if tag == "br" and not self._skipping:
            (self._cur_cell() if self._cur_cell() is not None else self.buf).append(" ")
            return
        if self._skipping:
            if tag == self._skip_tag and tag not in VOID:
                self._skip_depth += 1
            return
        if tag in MATH_TAGS:
            self.math_stripped = True
            self._skip_tag, self._skip_depth = tag, 1
            (self._cur_cell() if self._cur_cell() is not None else self.buf).append(" ")
            return
        if tag in DROP_TAGS or self._should_drop(tag, attrs):
            if any(c.startswith("mwe-math") for c in self._classes(attrs)):
                self.math_stripped = True
            if tag in VOID:
                return
            self._skip_tag, self._skip_depth = tag, 1
            return
        if tag == "title":
            self._in_title = True
            return
        if tag == "table":
            self._flush_para()
            self._tables.append({"rows": [], "row": None, "cell": None})
            return
        if self._tables:
            if tag == "caption":
                self._flush_para()
                self._in_caption = True
                return
            if tag == "tr":
                self._close_cell()
                self._close_row()
                self._tables[-1]["row"] = []
                return
            if tag in ("td", "th"):
                self._close_cell()
                if self._tables[-1]["row"] is None:
                    self._tables[-1]["row"] = []
                self._tables[-1]["cell"] = []
                return
        if tag in HEADINGS:
            self._flush_para()
            self._h_level = HEADINGS[tag]
            self.buf = []
            return
        if tag == "pre":
            self._pre_depth += 1
        if tag in BLOCK_TAGS:
            self._flush_para()
            return

    def handle_startendtag(self, tag, attrs) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:  # noqa: C901
        tag = tag.lower()
        if self._skipping:
            if tag == self._skip_tag:
                self._skip_depth -= 1
                if self._skip_depth <= 0:
                    self._skip_tag, self._skip_depth = None, 0
            return
        if tag == "title":
            self._in_title = False
            return
        if tag == "caption" and self._in_caption:
            self._in_caption = False
            self._flush_para()
            return
        if tag == "table" and self._tables:
            self._close_cell()
            self._close_row()
            t = self._tables.pop()
            rows = [r for r in t["rows"] if any(norm_ws(c) for c in r)]
            if not rows:
                return
            if self._tables:  # 表中表：拍平成文本塞进外层单元格
                flat = " ".join(norm_ws(c) for r in rows for c in r if norm_ws(c))
                target = self._cur_cell()
                (target if target is not None else self.buf).append(" " + flat + " ")
            else:
                self._emit("table", rows)
            return
        if self._tables:
            if tag in ("td", "th"):
                self._close_cell()
                return
            if tag == "tr":
                self._close_cell()
                self._close_row()
                return
        if tag in HEADINGS:
            if self._h_level is not None:
                text = norm_ws("".join(self.buf))
                self.buf = []
                if text:
                    self._emit("heading", (text, self._h_level))
                self._h_level = None
            return
        if tag == "pre":
            self._pre_depth = max(0, self._pre_depth - 1)
        if tag in BLOCK_TAGS:
            self._flush_para()

    def handle_data(self, data: str) -> None:
        if self._skipping or not data:
            return
        if self._in_title:
            self._title_parts.append(data)
            return
        cell = self._cur_cell()
        if cell is not None:
            cell.append(data)
        else:
            self.buf.append(data)

    # -- 表格辅助 --

    def _close_cell(self) -> None:
        if not self._tables:
            return
        t = self._tables[-1]
        if t["cell"] is not None:
            if t["row"] is None:
                t["row"] = []
            t["row"].append(norm_ws("".join(t["cell"])))
            t["cell"] = None

    def _close_row(self) -> None:
        if not self._tables:
            return
        t = self._tables[-1]
        if t["row"] is not None:
            if any(c for c in t["row"]):
                t["rows"].append(t["row"])
            t["row"] = None

    def close(self) -> None:  # type: ignore[override]
        super().close()
        while self._tables:  # 未闭合的 </table>
            self._close_cell()
            self._close_row()
            t = self._tables.pop()
            rows = [r for r in t["rows"] if any(norm_ws(c) for c in r)]
            if rows and not self._tables:
                self._emit("table", rows)
        self._h_level = None
        self._flush_para()

    @property
    def title(self) -> str:
        return norm_ws("".join(self._title_parts))


# ---------- 后处理 ----------

def _drop_noise_sections(blocks: list[tuple[str, object]]) -> list[tuple[str, object]]:
    """从「参考文献 / 外部链接 / …」标题起丢到同级或更高级标题。"""
    out: list[tuple[str, object]] = []
    dropping_level: int | None = None
    for kind, payload in blocks:
        if kind == "heading":
            _t, lvl = payload  # type: ignore[misc]
            text = norm_ws(_t).lower().strip(" :：.")
            if dropping_level is not None and lvl <= dropping_level:
                dropping_level = None
            if dropping_level is None and text in DROP_SECTIONS:
                dropping_level = lvl
                continue
        if dropping_level is not None:
            continue
        out.append((kind, payload))
    return out


def _strip_marks(blocks, wiki: bool):
    def fix(s: str) -> str:
        s = _EDIT_MARK.sub(" ", s)
        if wiki:
            s = _REF_MARK.sub(" ", s)
        return norm_ws(s)

    out = []
    for kind, payload in blocks:
        if kind == "heading":
            t, lvl = payload
            out.append(("heading", (fix(t), lvl)))
        elif kind == "para":
            out.append(("para", fix(payload)))
        else:
            out.append(("table", [[fix(c) for c in r] for r in payload]))
    return out


def _min_table(rows: list[list[str]]) -> bool:
    """至少 2 个非空单元格才算表格（1×1 的 <table> 是版面容器）。"""
    return sum(1 for r in rows for c in r if c) >= 2


#: 编号单元格（EUR-Lex 的 "(1)" / "a)" / 罗马数字 / 项目符号）
_NUM_CELL = re.compile(r"^[\(\[]?\s*(?:\d{1,3}|[a-zA-Z]{1,2}|[ivxlcIVXLC]{1,5}|[-–—•·*])\s*[\)\].°]?$")


def _is_layout_table(rows: list[list[str]]) -> bool:
    """版面表格判定：单行表 = 报头/版面行；「短编号 + 长正文」两列表 = 编号列表。

    这类东西在 EUR-Lex（recital 逐条编号）和 SEC（页眉/勾选框）里成百上千。
    留在真值 tables 里会让**从 PDF 转换的引擎**（那边根本看不到 table 标签）
    cell_f1 全灭；降级成段落后，正文一字不少，各引擎站在同一基线上。
    """
    if len(rows) == 1:
        return True
    body = [r for r in rows if any(r)]
    if len(body) >= 1 and all(len(r) == 2 for r in body):
        marks = sum(1 for r in body if _NUM_CELL.match(r[0].strip()))
        long_text = sum(len(r[1]) for r in body) / max(1, len(body))
        if marks == len(body) and long_text > 40:
            return True
    return False


# ---------- 入口 ----------

def parse_html(data: bytes | str, *, headings_mode: str = "auto",
               demote_layout_tables: bool = True) -> dict:
    """HTML 字节/文本 → 真值 dict。

    headings_mode: ``auto``（默认，标题不可靠时省略 headings 键）/ ``keep`` / ``drop``。
    demote_layout_tables: 版面表格降级成段落（见 `_is_layout_table`），默认开。
    """
    text = decode_html_bytes(data) if isinstance(data, bytes) else data
    p = _TruthHtmlParser()
    p.feed(text)
    p.close()

    blocks = _drop_noise_sections(p.blocks)
    blocks = _strip_marks(blocks, p.is_wiki)
    kept: list[tuple[str, object]] = []
    demoted = 0
    for k, v in blocks:
        if k == "table":
            if _min_table(v) and not (demote_layout_tables and _is_layout_table(v)):  # type: ignore[arg-type]
                kept.append((k, v))
            elif _min_table(v):  # type: ignore[arg-type]
                demoted += 1
                for r in v:  # type: ignore[union-attr]
                    line = " ".join(c for c in r if c)
                    if line:
                        kept.append(("para", line))
            else:
                # 1 个单元格的 `<table>` 是版面容器（SEC 文件里成千上万）：
                # 表格丢掉，但里面的文字必须留成段落，不能连正文一起蒸发
                text_ = " ".join(c for r in v for c in r if c)  # type: ignore[union-attr]
                if text_:
                    kept.append(("para", text_))
            continue
        if (v[0] if k == "heading" else v):  # type: ignore[index]
            kept.append((k, v))
    blocks = kept

    n_head = sum(1 for k, _ in blocks if k == "heading")
    n_para = sum(1 for k, _ in blocks if k == "para")
    if n_head == 0 and p.title:  # 正文无 h1–h6 时用 <title> 补一个
        blocks.insert(0, ("heading", (p.title, 1)))
        n_head = 1

    with_headings = True
    reason = ""
    if headings_mode == "drop":
        with_headings = False
        reason = "headings_mode=drop"
    elif headings_mode == "auto":
        if n_head == 0:
            with_headings, reason = False, "文档无 h1–h6"
        elif n_head < 3 and n_para > 50:
            with_headings, reason = False, "标题数 < 3 而段落数 > 50（疑似 SEC 式无语义标签）"

    note: dict = {"parser": "from_html"}
    if p.math_stripped:
        note["math_stripped"] = True
    if p.is_wiki:
        note["wikipedia_cleanup"] = True
    if p.infobox_dropped:
        note["infobox_dropped"] = p.infobox_dropped
    if demoted:
        note["layout_tables_demoted"] = demoted
    if not with_headings:
        note["headings_unavailable"] = reason
    return build_truth(blocks, note=note, with_headings=with_headings)


def parse(path: str | Path, **kw) -> dict:
    """从文件路径解析（支持 .gz）。"""
    path = Path(path)
    raw = path.read_bytes()
    if path.suffix == ".gz" or raw[:2] == b"\x1f\x8b":
        import gzip
        raw = gzip.decompress(raw)
    truth = parse_html(raw, **kw)
    truth.setdefault("note", {})["truth_src"] = path.name
    return truth
