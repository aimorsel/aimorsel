"""官方法规 XML → A 档真值 JSON（标准库 `xml.etree.ElementTree`）。

支持两种方言，靠**实际元素名嗅探**（不靠文件名），认不出来时走通用兜底，绝不崩：

1. **德国 gesetze-im-internet**（`xml.zip` 里的 `BJNR….xml`，DTD `gii-norm`）：
   `<dokumente>` → 多个 `<norm>`；`<metadaten>` 里 `<jurabk>/<langue>/<kurzue>`（文档标题）、
   `<gliederungseinheit>`（`<gliederungskennzahl>/<gliederungsbez>/<gliederungstitel>`，章节）、
   `<enbez>`（"§ 1"）+ `<titel>`（条标题）；正文在 `<textdaten><text><Content>` 里的
   `<P>` / `<DL><DT><DD><LA>` / `<table>`（CALS `tgroup/row/entry`，也兼容 `tr/td`）。
   层级：文档标题 1；Gliederung 按 `gliederungskennzahl` 长度算（每 3 位一层，2 起）；
   `§` 条 = 所在 Gliederung 层级 + 1，标题文本 = `enbez + " " + titel`。
   `<fussnoten>` / `<standangabe>` 这类元数据不进正文。

2. **日本 e-Gov 法令 XML**（`<Law><LawBody>…`，也兼容外层包着 `DataRoot/ApplData/LawFullText`）：
   `<LawTitle>` = 1 级标题；`Part/Chapter/Section/Subsection/Division/Article` 的
   `*Title`（`Article` 再拼上 `ArticleCaption`）按**结构嵌套深度**给层级（1 + 祖先结构层数，上限 6）；
   正文取 `ParagraphSentence/ItemSentence/Subitem*Sentence` 下的 `<Sentence>`，
   条号 `ParagraphNum`/`ItemTitle` 拼在句首；表格取 `TableStruct/Table/TableRow/TableColumn`。
   `<TOC>`（目次）丢弃，记在 `note.toc_dropped`。

3. **通用兜底**：局部名含 title/caption/heading/titel/bez 的元素当标题（按深度给层级），
   `table`/`Table`/`TableStruct` 当表格，其余块级元素文本当段落。

缺字段、缺整节都不抛异常（真实语料里 `<titel>`、`<gliederungstitel>` 经常缺）。
DOCTYPE 无法解析的实体（`&nbsp;` 之类）在解析前替换掉，避免 ET 因找不到 DTD 报错。
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from html.entities import html5
from pathlib import Path

from . import build_truth, norm_ws

ROW_TAGS = {"row", "tr", "tablerow"}
CELL_TAGS = {"entry", "td", "th", "tablecolumn", "tableheadercolumn"}
GII_SKIP = {"fussnoten", "standangabe", "standkommentar", "standtyp", "footnotes"}
JP_SKIP = {"toc", "tocpreamblelabel", "tocpart", "tocchapter", "tocsection", "tocarticle",
           "tocsupplprovision", "tocappdxtablelabel", "tocdivision", "tocsubsection"}
JP_STRUCT = ("part", "chapter", "section", "subsection", "division", "article")


def _ln(tag) -> str:
    """局部名（去命名空间），小写留给比较用的场合另取。"""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


# ---------- 宽容解析 ----------

_DOCTYPE = re.compile(r"<!DOCTYPE[^>[]*(\[[^\]]*\])?[^>]*>", re.S)
_ENT = re.compile(r"&([A-Za-z][A-Za-z0-9]{1,31});")
_KEEP_ENT = {"amp", "lt", "gt", "quot", "apos"}


def parse_xml_bytes(data: bytes) -> ET.Element:
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        pass
    text = data.decode("utf-8", errors="replace")
    if text[:1] != "<":
        text = text[text.find("<"):] if "<" in text else text
    text = _DOCTYPE.sub("", text)

    def ent(m):
        name = m.group(1)
        if name in _KEEP_ENT:
            return m.group(0)
        ch = html5.get(name + ";") or html5.get(name)
        return ch if ch else " "

    text = _ENT.sub(ent, text)
    text = re.sub(r"^<\?xml[^>]*\?>", "", text).strip()
    return ET.fromstring(text)


# ---------- 通用小工具 ----------

def _text(elem: ET.Element, skip: set[str] = frozenset()) -> str:
    """子树文本（跳过 skip 里的局部名子树）。"""
    parts: list[str] = []

    def rec(e: ET.Element):
        if _ln(e.tag).lower() in skip:
            return
        if e.text:
            parts.append(e.text)
        for c in e:
            rec(c)
            if c.tail:
                parts.append(c.tail)

    rec(elem)
    return norm_ws(" ".join(parts))


def _find_child(elem: ET.Element, name: str) -> ET.Element | None:
    for c in elem:
        if _ln(c.tag).lower() == name:
            return c
    return None


def _child_text(elem: ET.Element | None, name: str) -> str:
    if elem is None:
        return ""
    c = _find_child(elem, name)
    return _text(c) if c is not None else ""


def extract_table(elem: ET.Element) -> list[list[str]]:
    """CALS（row/entry）/ HTML（tr/td|th）/ e-Gov（TableRow/TableColumn）通吃。"""
    rows: list[list[str]] = []
    for r in elem.iter():
        if _ln(r.tag).lower() not in ROW_TAGS:
            continue
        cells = [_text(c) for c in r if _ln(c.tag).lower() in CELL_TAGS]
        if not cells:
            cells = [_text(c) for c in r]
        if any(cells):
            rows.append(cells)
    return rows


def _is_table(elem: ET.Element) -> bool:
    return _ln(elem.tag).lower() in ("table", "tablestruct", "informaltable")


# ---------- 德国 gesetze-im-internet ----------

def parse_gii(root: ET.Element) -> dict:
    blocks: list[tuple[str, object]] = []
    doc_title = ""
    gl_level = 1
    for norm in root.iter():
        if _ln(norm.tag).lower() != "norm":
            continue
        meta = _find_child(norm, "metadaten")
        if not doc_title and meta is not None:
            doc_title = (_child_text(meta, "langue") or _child_text(meta, "kurzue")
                         or _child_text(meta, "amtabk") or _child_text(meta, "jurabk"))
            if doc_title:
                blocks.append(("heading", (doc_title, 1)))
        if meta is not None:
            gl = _find_child(meta, "gliederungseinheit")
            if gl is not None:
                kz = _child_text(gl, "gliederungskennzahl")
                gl_level = min(5, 2 + max(0, len(kz) // 3 - 1))
                head = " ".join(x for x in (_child_text(gl, "gliederungsbez"),
                                            _child_text(gl, "gliederungstitel")) if x)
                if head:
                    blocks.append(("heading", (head, gl_level)))
            enbez = _child_text(meta, "enbez")
            titel = _child_text(meta, "titel")
            head = " ".join(x for x in (enbez, titel) if x)
            if head:
                blocks.append(("heading", (head, min(6, gl_level + 1))))
        td = _find_child(norm, "textdaten")
        text_el = _find_child(td, "text") if td is not None else None
        content = _find_child(text_el, "content") if text_el is not None else None
        if content is not None:
            blocks.extend(_gii_content(content))
    return build_truth(blocks, note={"parser": "from_xml", "dialect": "gii-norm"})


_LIST_TAGS = ("dl", "ul", "ol")


def _gii_list(elem: ET.Element, out: list[tuple[str, object]]) -> None:
    """`<DL><DT>1.</DT><DD><LA>…</LA></DD>` → 每项一段（DT 序号并进 DD 文本，与印刷版一致）。"""
    pending = ""
    for c in elem:
        name = _ln(c.tag).lower()
        if name == "dt":
            pending = _text(c)
            continue
        if name in ("dd", "li", "la", "p"):
            nested = [g for g in c if _ln(g.tag).lower() in _LIST_TAGS]
            t = _text(c, skip=set(_LIST_TAGS))
            merged = norm_ws(f"{pending} {t}") if pending else t
            pending = ""
            if merged:
                out.append(("para", merged))
            for g in nested:
                _gii_list(g, out)
            continue
        if name in _LIST_TAGS:
            _gii_list(c, out)
            continue
        if _is_table(c):
            rows = extract_table(c)
            if rows:
                out.append(("table", rows))
            continue
        t = _text(c)
        if t:
            out.append(("para", norm_ws(f"{pending} {t}") if pending else t))
            pending = ""
    if pending:
        out.append(("para", pending))


def _gii_content(content: ET.Element) -> list[tuple[str, object]]:
    out: list[tuple[str, object]] = []
    for child in content:
        name = _ln(child.tag).lower()
        if name in GII_SKIP:
            continue
        if _is_table(child):
            rows = extract_table(child)
            if rows:
                out.append(("table", rows))
            continue
        if name in ("dl", "ul", "ol"):
            _gii_list(child, out)
            continue
        t = _text(child)
        if t:
            out.append(("para", t))
    return out


# ---------- 日本 e-Gov ----------

LINE_PARENTS = {"paragraph", "item", "class", "amendprovision", "sublist1", "sublist2",
                "sublist3"} | {f"subitem{i}" for i in range(1, 11)}
#: 由父容器统一消费的行内元素（单独出现时不再成段，避免重复计入）
INLINE_SUFFIX = ("sentence", "num")


def parse_jp(root: ET.Element) -> dict:
    """e-Gov 法令 XML。结构不全时逐层退让，不抛异常。"""
    law = next((e for e in root.iter() if _ln(e.tag).lower() == "law"), root)
    body = next((e for e in law.iter() if _ln(e.tag).lower() == "lawbody"), None)
    scope = body if body is not None else law

    blocks: list[tuple[str, object]] = []
    state = {"toc": False}

    title = next((_text(e) for e in scope.iter() if _ln(e.tag).lower() == "lawtitle"), "")
    if title:
        blocks.append(("heading", (title, 1)))
    law_num = next((_text(e) for e in law.iter() if _ln(e.tag).lower() == "lawnum"), "")
    if law_num:
        blocks.append(("para", law_num))

    def emit_table(elem: ET.Element) -> None:
        rows = extract_table(elem)
        if rows:
            blocks.append(("table", rows))

    def walk(elem: ET.Element, depth: int, skip: frozenset = frozenset()) -> None:
        for child in elem:
            if id(child) in skip:
                continue
            name = _ln(child.tag).lower()
            if name in JP_SKIP:
                state["toc"] = state["toc"] or name == "toc"
                continue
            if name in ("lawtitle", "lawnum"):
                continue
            if name == "tablestruct":
                for t in child:
                    if _ln(t.tag).lower().endswith(("title", "caption")):
                        tt = _text(t)
                        if tt:
                            blocks.append(("para", tt))
                emit_table(child)
                continue
            if _is_table(child):
                emit_table(child)
                continue
            if name in JP_STRUCT:                      # 編/章/節/款/目/条
                cap = next((_text(t) for t in child
                            if _ln(t.tag).lower() == name + "caption"), "")
                head_el = next((t for t in child if _ln(t.tag).lower() == name + "title"), None)
                consumed = set()
                if head_el is not None:
                    head = " ".join(x for x in (_text(head_el), cap) if x)
                    if head:
                        blocks.append(("heading", (head, min(6, depth + 1))))
                    consumed.add(id(head_el))
                elif cap:
                    blocks.append(("heading", (cap, min(6, depth + 1))))
                consumed.update(id(t) for t in child
                                if _ln(t.tag).lower() == name + "caption")
                walk(child, depth + 1, frozenset(consumed))
                continue
            if name in LINE_PARENTS:                   # 項/号/細分：号数 + 句子拼一段
                parts, consumed = [], set()
                for c in child:
                    cn = _ln(c.tag).lower()
                    if cn.endswith(INLINE_SUFFIX) or cn == "column" or cn in (name + "title",):
                        t = _text(c)
                        if t:
                            parts.append(t)
                        consumed.add(id(c))
                if parts:
                    blocks.append(("para", norm_ws(" ".join(parts))))
                walk(child, depth + 1, frozenset(consumed))
                continue
            if name.endswith(INLINE_SUFFIX) or name == "column":
                t = _text(child)
                if t:
                    blocks.append(("para", t))
                continue
            if name.endswith(("title", "caption", "label")) and len(child) == 0:
                t = _text(child)                        # AppdxTableTitle / SupplProvisionLabel …
                if t:
                    blocks.append(("heading", (t, min(6, depth + 1))))
                continue
            if len(child) == 0:
                t = _text(child)
                if t:
                    blocks.append(("para", t))
                continue
            # MainProvision / SupplProvision / AppdxTable 这类包装容器**不占层级**，
            # 否则 Part 会从 3 起（编 = 2 才对得上印刷版）
            walk(child, depth)

    walk(scope, 1)
    note = {"parser": "from_xml", "dialect": "egov-jp"}
    if state["toc"]:
        note["toc_dropped"] = True
    return build_truth(blocks, note=note)


# ---------- 通用兜底 ----------

_TITLEISH = re.compile(r"(title|caption|heading|titel|bez|ueberschrift)$", re.I)


def parse_generic(root: ET.Element) -> dict:
    blocks: list[tuple[str, object]] = []

    def walk(elem: ET.Element, depth: int) -> None:
        for child in elem:
            name = _ln(child.tag)
            low = name.lower()
            if low in GII_SKIP or low in JP_SKIP or low in ("script", "style"):
                continue
            if _is_table(child):
                rows = extract_table(child)
                if rows:
                    blocks.append(("table", rows))
                continue
            if _TITLEISH.search(name) and len(child) == 0:
                t = _text(child)
                if t:
                    blocks.append(("heading", (t, min(6, depth))))
                continue
            if len(child) == 0:
                t = _text(child)
                if t:
                    blocks.append(("para", t))
                continue
            walk(child, depth + 1)

    walk(root, 1)
    return build_truth(blocks, note={"parser": "from_xml", "dialect": "generic"})


# ---------- 入口 ----------

def detect_dialect(root: ET.Element) -> str:
    names = {_ln(e.tag).lower() for e in root.iter()}
    if _ln(root.tag).lower() in ("dokumente", "norm") or "norm" in names and "metadaten" in names:
        return "gii"
    if "law" in names or "lawbody" in names or _ln(root.tag).lower() in ("law", "lawbody"):
        return "jp"
    return "generic"


def parse_xml(data: bytes | str) -> dict:
    root = parse_xml_bytes(data.encode("utf-8") if isinstance(data, str) else data)
    dialect = detect_dialect(root)
    truth = {"gii": parse_gii, "jp": parse_jp}.get(dialect, parse_generic)(root)
    if not norm_ws(truth.get("text", "")) and dialect != "generic":
        # 结构与预期不符（官方 schema 变了 / 拿到的是别的封装）→ 兜底再来一遍
        alt = parse_generic(root)
        if len(alt.get("text", "")) > len(truth.get("text", "")):
            alt.setdefault("note", {})["fallback_from"] = dialect
            truth = alt
    if not truth.get("headings"):
        # 一个标题都没抓到 = 这份文档不评 heading_f1（**省略键**，写 [] 会把所有引擎判成 0 分）
        truth.pop("headings", None)
        truth.pop("heading_levels", None)
        truth.setdefault("note", {})["headings_unavailable"] = "XML 结构里没抓到标题"
    return truth


def parse(path: str | Path) -> dict:
    """支持 .xml / .xml.gz / .zip（取最大的 .xml 成员，gesetze-im-internet 的 xml.zip）。"""
    path = Path(path)
    raw = path.read_bytes()
    member = ""
    if raw[:2] == b"PK":
        import zipfile

        with zipfile.ZipFile(path) as zf:
            xmls = [i for i in zf.infolist()
                    if i.filename.lower().endswith(".xml") and not i.is_dir()]
            if not xmls:
                raise ValueError(f"{path.name} 里没有 .xml 成员")
            best = max(xmls, key=lambda i: i.file_size)
            member = best.filename
            raw = zf.read(best)
    elif raw[:2] == b"\x1f\x8b":
        import gzip

        raw = gzip.decompress(raw)
    truth = parse_xml(raw)
    note = truth.setdefault("note", {})
    note["truth_src"] = path.name
    if member:
        note["zip_member"] = member
    return truth
