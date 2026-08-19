"""arXiv e-print（LaTeX 源码）→ A 档真值 JSON。纯标准库（tarfile / gzip / re）。

输入可以是：目录、`.tar.gz` / `.tgz` / `.tar`、`.tex.gz` / 裸 `.gz`（arXiv 单文件 e-print）、
裸 `.tex`。流程：

1. **找主文件**：含 `\\documentclass` 且含 `\\begin{document}` 的那个（多个取最大）；
   退化时取含 `\\begin{document}` 的、再退化取最大的 `.tex`。跟进 `\\input` / `\\include` / `\\subfile`。
2. 去注释（未转义的 `%` 到行尾）、只取 `\\begin{document}`…`\\end{document}`；
   `\\title` / `\\author` 从导言区单独抽出（PDF 首页有这些字，engines 也会输出）。
3. **标题**：`\\chapter` / `\\section` / `\\subsection` / `\\subsubsection` / `\\paragraph`（含 `*` 变体）。
   层级映射（记在 `note.heading_level_map`）——`\\title` 恒为 1；
   文档**不含** `\\chapter` 时 section=2 / subsection=3 / subsubsection=4 / paragraph=5，
   含 `\\chapter` 时整体下移一级（chapter=2 … paragraph=6）。理由：转换器普遍把文档标题
   渲染成 `#`、把 `\\section` 渲染成 `##`，层级要和它们对齐。
4. **表格**：`tabular` / `tabular*` / `longtable` 的单元格抽成 tables（`\\\\` 切行、未转义 `&` 切列，
   `\\multicolumn`/`\\multirow` 取文本，`\\hline`/`booktabs` 规则行丢弃）。
   `figure` / `table` 浮动环境的其余内容丢弃，但 **`\\caption` 文本保留成段落**（PDF 里看得见）。
5. **公式统一变成一个空格**：`$…$` / `$$…$$` / `\\(…\\)` / `\\[…\\]` /
   equation·align·gather·multline·eqnarray… 环境。真值 `note.math_stripped = true`。
   ——取舍理由：被测引擎（含本项目）基本都丢公式，把公式字符算进 char_sim / cer
   会让所有引擎一起虚低、且噪声压过真实差异。**公式能力另开专项评测，不混在文本保真里。**
6. 参考文献（`\\bibliography` / `\\bibliographystyle` / `thebibliography` / `\\printbibliography`）整段丢弃。
"""
from __future__ import annotations

import gzip
import io
import re
import tarfile
import unicodedata
from pathlib import Path

from . import build_truth, norm_ws

MAX_INPUT_DEPTH = 8

MATH_ENVS = ("equation", "displaymath", "eqnarray", "align", "alignat", "flalign", "gather",
             "multline", "math", "dmath", "IEEEeqnarray", "subequations", "split", "cases")
TABULAR_ENVS = ("tabular*", "tabular", "longtable", "tabularx", "supertabular", "array")
# 浮动环境：正文丢弃、caption 保留。**不含 minipage**（它不是浮动体，常包着真正的正文）
FLOAT_ENVS = ("figure*", "figure", "table*", "table", "wrapfigure", "wraptable", "sidewaysfigure",
              "sidewaystable", "SCfigure", "subfigure")
#: 带强制参数的环境：删 \begin 时要把参数一起删掉，否则 {0.45\textwidth} 会漏成正文 "0.45"
ENV_WITH_ARGS = ("minipage", "multicols", "adjustbox", "resizebox", "wrapfigure", "tabularx")
DROP_ENVS = ("thebibliography", "tikzpicture", "picture", "comment", "filecontents",
             "algorithm", "algorithmic", "algorithm2e", "pgfpicture")

SECTION_LEVELS = ("chapter", "section", "subsection", "subsubsection", "paragraph", "subparagraph")

_TABLE_MARK = "@@BENCHTABLE{}@@"
_TABLE_MARK_RE = re.compile(r"@@BENCHTABLE(\d+)@@")

# ---------- 基础工具 ----------

_COMMENT = re.compile(r"(?<!\\)((?:\\\\)*)%.*")


def strip_comments(src: str) -> str:
    out = []
    for line in src.splitlines():
        out.append(_COMMENT.sub(r"\1", line))
    return "\n".join(out)


def _match_brace(s: str, i: int) -> tuple[str, int]:
    """s[i] == '{'，返回 (内容, 右括号之后的下标)。容忍未闭合。"""
    assert s[i] == "{"
    depth, j = 0, i
    while j < len(s):
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return s[i + 1:j], j + 1
        j += 1
    return s[i + 1:], len(s)


def _skip_opt(s: str, i: int) -> int:
    """跳过 [..] 可选参数（可有多个）与空白。"""
    while i < len(s):
        while i < len(s) and s[i] in " \t\n":
            i += 1
        if i < len(s) and s[i] == "[":
            depth = 0
            while i < len(s):
                if s[i] == "[":
                    depth += 1
                elif s[i] == "]":
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break
                i += 1
        else:
            break
    return i


def _next_arg(s: str, i: int) -> tuple[str, int]:
    """取下一个参数：`{...}` 或紧跟的单个字符。"""
    i = _skip_opt(s, i)
    if i < len(s) and s[i] == "{":
        return _match_brace(s, i)
    if i < len(s):
        if s[i] == "\\" and i + 1 < len(s):
            return s[i:i + 2], i + 2
        return s[i], i + 1
    return "", i


def find_env(s: str, name: str, start: int = 0) -> tuple[int, int, int, int] | None:
    """找 `\\begin{name}`…`\\end{name}`（同名嵌套按深度配对）。

    返回 (begin_start, content_start, content_end, end_after)。
    """
    b = re.compile(r"\\begin\s*\{" + re.escape(name) + r"\}")
    e = re.compile(r"\\end\s*\{" + re.escape(name) + r"\}")
    m = b.search(s, start)
    if not m:
        return None
    depth, pos = 1, m.end()
    while depth:
        mb, me = b.search(s, pos), e.search(s, pos)
        if me is None:
            return m.start(), m.end(), len(s), len(s)
        if mb and mb.start() < me.start():
            depth += 1
            pos = mb.end()
        else:
            depth -= 1
            pos = me.end()
            if depth == 0:
                return m.start(), m.end(), me.start(), me.end()
    return None


# ---------- 命令 → 文本 ----------

SPECIALS = {
    "&": "&", "%": "%", "$": "$", "#": "#", "_": "_", "{": "{", "}": "}",
    "ss": "ß", "ae": "æ", "AE": "Æ", "oe": "œ", "OE": "Œ", "o": "ø", "O": "Ø",
    "aa": "å", "AA": "Å", "l": "ł", "L": "Ł", "i": "ı", "j": "ȷ", "dh": "ð", "DH": "Ð",
    "th": "þ", "TH": "Þ", "ldots": "…", "dots": "…", "textellipsis": "…",
    "textendash": "–", "textemdash": "—", "textbackslash": "\\", "textasciitilde": "~",
    "textquotedblleft": "“", "textquotedblright": "”", "textquoteleft": "‘",
    "textquoteright": "’", "textregistered": "®", "copyright": "©", "textcopyright": "©",
    "pounds": "£", "texteuro": "€", "euro": "€", "degree": "°", "textdegree": "°",
    "textbullet": "•", "S": "§", "P": "¶", "dag": "†", "ddag": "‡",
}
ACCENTS = {"`": "\u0300", "'": "\u0301", "^": "\u0302", '"': "\u0308", "~": "\u0303",
           "=": "\u0304", ".": "\u0307", "u": "\u0306", "v": "\u030c", "H": "\u030b",
           "c": "\u0327", "k": "\u0328", "b": "\u0331", "d": "\u0323", "r": "\u030a",
           "t": "\u0361"}
#: 保留第 n 个参数的文本（其余丢弃）
KEEP_ARG = {"textbf": 1, "textit": 1, "textsl": 1, "textsc": 1, "texttt": 1, "textrm": 1,
            "textsf": 1, "textnormal": 1, "emph": 1, "underline": 1, "uline": 1, "mbox": 1,
            "text": 1, "hbox": 1, "footnote": 1, "footnotetext": 1, "caption": 1,
            "textup": 1, "textmd": 1, "uppercase": 1, "lowercase": 1, "MakeUppercase": 1,
            "MakeLowercase": 1, "st": 1, "sout": 1, "enquote": 1, "textcolor": 2,
            "href": 2, "url": 1, "texorpdfstring": 1, "titlecap": 1, "acronym": 1,
            "textsuperscript": 1, "textsubscript": 1}
#: 连同参数一起丢弃
DROP_CMD = {"label": 1, "cite": 1, "citep": 1, "citet": 1, "citeay": 1, "citealp": 1,
            "citealt": 1, "citeauthor": 1, "citeyear": 1, "nocite": 1, "ref": 1, "eqref": 1,
            "pageref": 1, "autoref": 1, "cref": 1, "Cref": 1, "index": 1, "vspace": 1,
            "hspace": 1, "setlength": 2, "addtolength": 2, "bibliographystyle": 1,
            "bibliography": 1, "usepackage": 1, "documentclass": 1, "newcommand": 2,
            "renewcommand": 2, "providecommand": 2, "def": 1, "input": 1, "include": 1,
            "includegraphics": 1, "graphicspath": 1, "thanks": 1, "affiliation": 1,
            "institute": 1, "email": 1, "keywords": 0, "pagestyle": 1, "thispagestyle": 1,
            "hypersetup": 1, "geometry": 1, "captionsetup": 1, "linespread": 1,
            "footnotemark": 0, "maketitle": 0, "tableofcontents": 0, "listoffigures": 0,
            "listoftables": 0, "printbibliography": 0, "clearpage": 0, "newpage": 0,
            "noindent": 0, "centering": 0, "raggedright": 0, "hline": 0, "toprule": 0,
            "midrule": 0, "bottomrule": 0, "cmidrule": 1, "cline": 1, "small": 0,
            "footnotesize": 0, "scriptsize": 0, "tiny": 0, "large": 0, "Large": 0,
            "LARGE": 0, "huge": 0, "Huge": 0, "normalsize": 0, "bfseries": 0, "itshape": 0,
            "rmfamily": 0, "sffamily": 0, "ttfamily": 0, "par": 0, "protect": 0,
            "linebreak": 0, "nolinebreak": 0, "sloppy": 0, "appendix": 0, "and": 0}


def latex_to_text(s: str) -> str:
    """展开常见命令、去掉其余标记，得到可读文本（假定公式已被替换掉）。"""
    out: list[str] = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            if i + 1 >= n:
                break
            m = re.match(r"[A-Za-z@]+\*?", s[i + 1:])
            if not m:
                nxt = s[i + 1]
                if nxt == "\\":                     # 换行
                    out.append(" ")
                    i = _skip_opt(s, i + 2)
                    continue
                if nxt in ACCENTS:                  # \'e \"u
                    arg, j = _next_arg(s, i + 2)
                    out.append(unicodedata.normalize("NFC", latex_to_text(arg) + ACCENTS[nxt]))
                    i = j
                    continue
                if nxt in SPECIALS:
                    out.append(SPECIALS[nxt])
                    i += 2
                    continue
                if nxt in " ,;:!/-":                # \  \, \; 等间距
                    out.append(" " if nxt != "-" else "")
                    i += 2
                    continue
                out.append(" ")
                i += 2
                continue
            name = m.group(0)
            i += 1 + len(name)
            base = name.rstrip("*")
            if base in SPECIALS:
                out.append(SPECIALS[base])
                continue
            if base in ACCENTS and len(base) == 1:
                arg, j = _next_arg(s, i)
                out.append(unicodedata.normalize("NFC", latex_to_text(arg) + ACCENTS[base]))
                i = j
                continue
            if base in KEEP_ARG:
                k = KEEP_ARG[base]
                arg = ""
                for idx in range(1, k + 1):
                    a, i = _next_arg(s, i)
                    if idx == k:
                        arg = a
                out.append(" " + latex_to_text(arg) + " ")
                continue
            if base in DROP_CMD:
                for _ in range(DROP_CMD[base]):
                    _, i = _next_arg(s, i)
                out.append(" ")
                continue
            # 未知命令：命令名丢掉，参数里的文本留下（\textcolor{red}{x} 之类的残留可接受）
            i = _skip_opt(s, i)
            out.append(" ")
            continue
        if c in "{}":
            i += 1
            continue
        if c == "~":
            out.append(" ")
            i += 1
            continue
        if c == "&":
            out.append(" ")
            i += 1
            continue
        if s.startswith("---", i):
            out.append("—")
            i += 3
            continue
        if s.startswith("--", i):
            out.append("–")
            i += 2
            continue
        if s.startswith("``", i):
            out.append("“")
            i += 2
            continue
        if s.startswith("''", i):
            out.append("”")
            i += 2
            continue
        out.append(c)
        i += 1
    text = norm_ws("".join(out))
    # 命令展开会在参数两侧塞空格，收一下标点前的空隙（"a link ." → "a link."）
    return re.sub(r" +([,.;:!?%)\]}»”’])", r"\1", text)


# ---------- 公式 ----------

def strip_math(s: str) -> tuple[str, int]:
    """把所有数学替换成一个空格，返回 (文本, 被替换的段数)。"""
    count = 0
    for env in MATH_ENVS:
        for name in (env + "*", env):
            while True:
                f = find_env(s, name)
                if not f:
                    break
                s = s[:f[0]] + " " + s[f[3]:]
                count += 1
    def sub(pat, text):
        nonlocal count
        def r(m):
            nonlocal count
            count += 1
            return " "
        return re.sub(pat, r, text, flags=re.S)

    s = sub(r"\$\$.*?\$\$", s)
    s = sub(r"\\\[.*?\\\]", s)
    s = sub(r"\\\(.*?\\\)", s)
    s = sub(r"(?<!\\)\$(?:\\.|[^$\\])*?\$", s)
    return s, count


# ---------- 表格 ----------

_ROW_SPLIT = re.compile(r"\\\\|\\tabularnewline|\\newline")
_RULE_LINE = re.compile(r"^\s*(?:\\(?:hline|toprule|midrule|bottomrule|cline\s*\{[^}]*\}|"
                        r"cmidrule(?:\s*\([^)]*\))?\s*\{[^}]*\}|noalign\s*\{[^}]*\}|"
                        r"rowcolor\s*\{[^}]*\}|arrayrulecolor\s*\{[^}]*\})\s*)+$")


def _split_cells(row: str) -> list[str]:
    cells, buf, i, depth = [], [], 0, 0
    while i < len(row):
        c = row[i]
        if c == "\\" and i + 1 < len(row):
            buf.append(row[i:i + 2])
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        if c == "&" and depth == 0:
            cells.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    cells.append("".join(buf))
    return cells


def _cell_text(cell: str) -> str:
    cell = re.sub(r"\\multicolumn\s*\{[^}]*\}\s*\{[^}]*\}\s*", "", cell)
    cell = re.sub(r"\\multirow\s*\{[^}]*\}\s*\{[^}]*\}\s*", "", cell)
    return latex_to_text(cell)


def parse_tabular(content: str) -> list[list[str]]:
    """tabular 环境正文（已去掉列格式参数）→ 二维数组。"""
    rows = []
    for raw in _ROW_SPLIT.split(content):
        raw = re.sub(r"^\s*\[[^\]]*\]", "", raw)  # `\\[2pt]` 的行距参数
        if not raw.strip() or _RULE_LINE.match(raw.strip()):
            continue
        cells = [_cell_text(c) for c in _split_cells(raw)]
        if any(cells):
            rows.append(cells)
    return rows


def extract_tables(body: str) -> tuple[str, list[list[list[str]]]]:
    """把所有 tabular 类环境换成 `@@BENCHTABLEn@@` 标记，返回 (新正文, 表格列表)。"""
    tables: list[list[list[str]]] = []
    for env in TABULAR_ENVS:
        while True:
            f = find_env(body, env)
            if not f:
                break
            bs, cs, ce, ea = f
            content = body[cs:ce]
            # 去掉 [pos] 与 {列格式}（tabular* 还多一个宽度参数）
            j = _skip_opt(content, 0)
            if env == "tabular*" and j < len(content) and content[j] == "{":
                _, j = _match_brace(content, j)
                j = _skip_opt(content, j)
            if j < len(content) and content[j] == "{":
                _, j = _match_brace(content, j)
            rows = parse_tabular(content[j:])
            if rows and env != "array":
                mark = _TABLE_MARK.format(len(tables))
                tables.append(rows)
                body = body[:bs] + f"\n\n{mark}\n\n" + body[ea:]
            else:  # array（数学用）或空表：整段丢掉
                body = body[:bs] + " " + body[ea:]
    return body, tables


# ---------- 浮动环境 / 参考文献 ----------

def handle_floats(body: str) -> str:
    """figure/table 等浮动环境：只留 `\\caption` 文本与已抽出的表格标记，其余丢弃。"""
    for env in FLOAT_ENVS:
        guard = 0
        while guard < 500:
            f = find_env(body, env)
            if not f:
                break
            guard += 1
            bs, cs, ce, ea = f
            inner = body[cs:ce]
            keep: list[str] = []
            for m in re.finditer(r"\\caption\*?\s*", inner):
                k = _skip_opt(inner, m.end())
                if k < len(inner) and inner[k] == "{":
                    cap, _ = _match_brace(inner, k)
                    keep.append(latex_to_text(cap))
            keep.extend(m.group(0) for m in _TABLE_MARK_RE.finditer(inner))
            body = body[:bs] + "\n\n" + "\n\n".join(x for x in keep if x) + "\n\n" + body[ea:]
    return body


def drop_envs(body: str, names=DROP_ENVS) -> str:
    for env in names:
        guard = 0
        while guard < 500:
            f = find_env(body, env)
            if not f:
                break
            guard += 1
            body = body[:f[0]] + "\n\n" + body[f[3]:]
    return body


# ---------- 源文件收集 ----------

def _decode(raw: bytes) -> str:
    for enc in ("utf-8", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def collect_sources(path: str | Path) -> dict[str, str]:
    """把输入展开成 {相对名: 文本}（只收 .tex/.ltx/.txt 类文本）。"""
    path = Path(path)
    files: dict[str, str] = {}

    def want(name: str) -> bool:
        return Path(name).suffix.lower() in (".tex", ".ltx", ".latex", "") or name.endswith(".tex")

    if path.is_dir():
        for p in sorted(path.rglob("*")):
            if p.is_file() and p.suffix.lower() in (".tex", ".ltx", ".latex"):
                files[str(p.relative_to(path))] = _decode(p.read_bytes())
        return files

    raw = path.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except Exception:
            pass
    # 先当 tar 试（arXiv 的 e-print 常常是 .gz 里包 tar）
    try:
        with tarfile.open(fileobj=io.BytesIO(raw)) as tf:
            for m in tf.getmembers():
                if not m.isfile() or not want(m.name):
                    continue
                fh = tf.extractfile(m)
                if fh is None:
                    continue
                data = fh.read()
                if b"\\documentclass" in data or b"\\begin{document}" in data or m.name.endswith(".tex"):
                    files[m.name] = _decode(data)
        if files:
            return files
    except tarfile.TarError:
        pass
    return {path.name: _decode(raw)}


def find_main_tex(files: dict[str, str]) -> str:
    if not files:
        raise ValueError("没有找到任何 .tex 源文件")
    best = [n for n, s in files.items() if "\\documentclass" in s and "\\begin{document}" in s]
    if not best:
        best = [n for n, s in files.items() if "\\begin{document}" in s]
    if not best:
        best = list(files)
    return max(best, key=lambda n: len(files[n]))


def _resolve(name: str, files: dict[str, str]) -> str | None:
    cands = [name, name + ".tex", name.lstrip("./"), name.lstrip("./") + ".tex"]
    for c in cands:
        if c in files:
            return c
    base = Path(name).name
    for c in (base, base + ".tex"):
        for key in files:
            if Path(key).name == c:
                return key
    return None


def expand_inputs(src: str, files: dict[str, str], depth: int = 0, seen: set[str] | None = None) -> str:
    if depth >= MAX_INPUT_DEPTH:
        return src
    seen = seen or set()
    pat = re.compile(r"\\(?:input|include|subfile)\s*\{([^}]*)\}")

    def rep(m):
        key = _resolve(m.group(1).strip(), files)
        if not key or key in seen:
            return "\n\n"
        seen.add(key)
        return "\n\n" + expand_inputs(strip_comments(files[key]), files, depth + 1, seen) + "\n\n"

    return pat.sub(rep, src)


# ---------- 主流程 ----------

def _preamble_field(src: str, cmd: str) -> str:
    m = re.search(r"\\" + cmd + r"\s*(?=[\[{])", src)
    if not m:
        return ""
    i = _skip_opt(src, m.end())
    if i < len(src) and src[i] == "{":
        content, _ = _match_brace(src, i)
        content, _ = strip_math(content)
        return latex_to_text(content)
    return ""


def parse_latex(src: str, files: dict[str, str] | None = None) -> dict:
    """LaTeX 主文件源码（可给出同批文件用于 `\\input`）→ 真值 dict。"""
    files = files or {}
    src = strip_comments(src)
    src = expand_inputs(src, {k: v for k, v in files.items()}, 0, set())

    title = _preamble_field(src, "title")
    author = _preamble_field(src, "author")

    f = find_env(src, "document")
    body = src[f[1]:f[2]] if f else src

    body = drop_envs(body)                     # thebibliography / tikzpicture / …
    body, tables = extract_tables(body)        # tabular → 标记
    body = handle_floats(body)                 # figure/table：留 caption + 表格标记
    body, n_math = strip_math(body)
    body = re.sub(r"\\(?:bibliography|bibliographystyle|printbibliography|nobibliography)\s*(?:\{[^}]*\})?",
                  " ", body)
    has_chapter = re.search(r"\\chapter\*?\s*[\[{]", body) is not None
    if has_chapter:  # 书/学位论文：\title=1, chapter=2, section=3 …
        level_map = {"chapter": 2, "section": 3, "subsection": 4, "subsubsection": 5,
                     "paragraph": 6, "subparagraph": 6}
    else:            # 论文：\title=1, section=2, subsection=3 …（与转换器的 #/## 对齐）
        level_map = {"chapter": 2, "section": 2, "subsection": 3, "subsubsection": 4,
                     "paragraph": 5, "subparagraph": 6}

    # 其余环境标记（itemize/abstract/quote/…）与 \item 一律当段落边界
    for env in ENV_WITH_ARGS:
        body = re.sub(r"\\begin\s*\{" + re.escape(env) + r"\}\s*(?:\[[^\]]*\])?\s*(?:\{[^{}]*\})+",
                      "\n\n", body)
    body = re.sub(r"\\(?:begin|end)\s*\{[^}]*\}(?:\s*\[[^\]]*\])?", "\n\n", body)
    body = re.sub(r"\\item\b(?:\s*\[[^\]]*\])?", "\n\n", body)

    blocks: list[tuple[str, object]] = []
    if title:
        blocks.append(("heading", (title, 1)))
    if author:
        blocks.append(("para", author))

    used_tables: set[int] = set()

    def add_text(chunk: str) -> None:
        # split 带捕获组：偶数位是正文、奇数位是表格编号
        for idx, piece in enumerate(_TABLE_MARK_RE.split(chunk)):
            if idx % 2 == 1:
                n = int(piece)
                if n < len(tables):
                    blocks.append(("table", tables[n]))
                    used_tables.add(n)
                continue
            for para in re.split(r"\n\s*\n", piece):
                t = latex_to_text(para)
                if t:
                    blocks.append(("para", t))

    sec_re = re.compile(r"\\(" + "|".join(SECTION_LEVELS) + r")\*?\s*(?=[\[{])")
    pos = 0
    for m in sec_re.finditer(body):
        add_text(body[pos:m.start()])
        i = _skip_opt(body, m.end())
        if i < len(body) and body[i] == "{":
            head, pos = _match_brace(body, i)
        else:
            head, pos = "", m.end()
        head = latex_to_text(strip_math(head)[0])
        if head:
            blocks.append(("heading", (head, level_map.get(m.group(1), 2))))
    add_text(body[pos:])

    for n, t in enumerate(tables):  # 标记被吃掉的表格（少见）也要进真值
        if n not in used_tables:
            blocks.append(("table", t))

    note = {"parser": "from_latex", "math_stripped": True, "math_spans": n_math,
            "heading_level_map": dict(level_map),
            "bibliography_dropped": True, "float_content_dropped_captions_kept": True}
    truth = build_truth(blocks, note=note)
    if not truth.get("headings"):
        # 一个标题都没抓到，多半是自定义分节宏（\mysection）而不是「文档真没标题」→
        # **省略 headings 键**，让 score_document 跳过 heading_f1，而不是判所有引擎 0 分
        truth.pop("headings", None)
        truth.pop("heading_levels", None)
        note["headings_unavailable"] = "源码里没抓到 \\section 类命令"
    return truth


def parse(path: str | Path) -> dict:
    files = collect_sources(path)
    main = find_main_tex(files)
    truth = parse_latex(files[main], files)
    truth.setdefault("note", {}).update({"truth_src": Path(path).name, "main_tex": main})
    return truth
