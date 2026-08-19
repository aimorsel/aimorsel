"""RTL（阿拉伯文 / 希伯来文等）文本的「视觉序 → 逻辑序」还原。

背景：不少 PDF 的内容流按**视觉顺序**（页面从左到右）写字，右到左的文字每个 run 被
逐字符反转（"الجمهورية" 变成 "ةيروهمجلا"）。字一个不缺、渲染出来肉眼一样，但 grep /
分词 / 向量化 / 复制粘贴全废。Java 引擎和 pdfplumber 抽出来都是这副样子，
docling / pymupdf4llm 做了重排，我们没做——这是 bench issue #0。

做法：把抽出的一行当作 UAX#9 的**结果**反推回**输入**。Unicode 双向算法的重排步骤（L2：
按层级由高到低逐段反转）在层级已定时是自反的，所以对视觉行**再算一遍层级、再反转一遍**
就回到逻辑序（弱类型/中性字符的归属在少数边界情况下会有偏差，属已知精度边界）。
这里实现的是无显式嵌入控制符（PDF 抽出的文本不含 RLE/LRE/PDF/isolate）的简化版：
只有基础层级 + 一层反向嵌入，覆盖 W1–W7 / N1–N2 / I1–I2 / L1–L2。

三条设计决策（改之前想清楚）：
1. **先探测、再动手**：`looks_visual_rtl()` 用词形统计（ة/ى 出现在词首、"ال" 冠词
   反过来变成词尾"لا"、希伯来尾形字母出现在词首、标点粘在词首）判断一份文档是不是视觉序。
   逻辑序的 RTL 文档（HTML/Office 输入、部分 PDF 生成器）**绝不能碰**，否则等于把它反着弄坏。
   判断按**文档级**做，不按行——同一份文档不会一半视觉一半逻辑。
2. **括号镜像按文档投票**：有的生成器在视觉流里写的是逻辑码位（")أ(" 反过来正好是 "(أ)"），
   有的写的是字形码位（反过来得到 ")أ("）。还原后数一遍括号配对，配错的多就整篇镜像一次。
3. **格式感知**：Markdown 只动正文（保留标题井号/列表前缀/表格竖线，表格逐格处理），
   HTML 只动标签之间的文本，JSON 只动 `"content"` 行的字符串值（保持 Jackson 的缩进格式原样），
   txt 整行处理。带围栏的代码块不动。

已知边界（写进 issue，不在这层解决）：
- lam-alef 连字（لا/لأ/لإ/لآ）在视觉流里以**逻辑序**成对出现（是一个字形映射成两个字符），
  整行反转后会变成 "ال"；与冠词 "ال" 反转的结果不可区分，无词典无法修，与 pymupdf4llm 同等水平。
- 谐音符号（harakat, NSM）在视觉流里的位置取决于生成器，还原后可能落到基字符前面。
"""

from __future__ import annotations

import json
import re
import unicodedata

# ── 字符分类 ────────────────────────────────────────────────────────────

_RTL_TYPES = {"R", "AL"}
_STRONG_TYPES = {"L", "R", "AL"}
_NEUTRAL_TYPES = {"B", "S", "WS", "ON"}
# UAX#9 的显式控制符 / 隔离符在 PDF 抽出的文本里几乎不会出现，统一按 BN（忽略）处理
_IGNORED_TYPES = {"BN", "LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI"}

# Bidi_Mirrored 常见配对（unicodedata 只给 mirrored() 布尔值，没有配对表；括号类够用）
_MIRROR_PAIRS = {
    "(": ")", ")": "(", "[": "]", "]": "[", "{": "}", "}": "{", "<": ">", ">": "<",
    "«": "»", "»": "«", "‹": "›", "›": "‹", "⟨": "⟩", "⟩": "⟨", "⟦": "⟧", "⟧": "⟦",
    "⁅": "⁆", "⁆": "⁅", "❨": "❩", "❩": "❨", "❪": "❫", "❫": "❪", "❬": "❭", "❭": "❬",
    "≤": "≥", "≥": "≤", "⊂": "⊃", "⊃": "⊂", "⊆": "⊇", "⊇": "⊆",
    "﴾": "﴿", "﴿": "﴾",
}
_OPENERS = "([{<«‹⟨⟦⁅❨❪❬﴾"
_CLOSERS = ")]}>»›⟩⟧⁆❩❫❭﴿"
_CLOSE_OF = dict(zip(_OPENERS, _CLOSERS))


def _bidi_type(ch: str) -> str:
    t = unicodedata.bidirectional(ch)
    if not t:  # 未分配码位
        return "L"
    if t in _IGNORED_TYPES:
        return "BN"
    return t


# RTL 字符所在的 Unicode 区块（希伯来/阿拉伯/叙利亚/它拿/N'Ko/撒玛利亚等 + 阿拉伯表现形式），
# 先用 C 速度的正则粗筛，绝大多数文档一次 search 就出局，不必逐字符查 bidirectional()
_RTL_BLOCK_RE = re.compile("[\u0590-\u08ff\ufb1d-\ufdff\ufe70-\ufeff\U00010800-\U00010fff\U0001e800-\U0001efff]")


def _has_rtl(text: str) -> bool:
    if not _RTL_BLOCK_RE.search(text):
        return False
    return any(_bidi_type(ch) in _RTL_TYPES for ch in text)


def _count_strong(text: str) -> tuple[int, int]:
    rtl = ltr = 0
    for ch in text:
        t = _bidi_type(ch)
        if t in _RTL_TYPES:
            rtl += 1
        elif t == "L":
            ltr += 1
    return rtl, ltr


# ── UAX#9 简化实现 ──────────────────────────────────────────────────────


def _resolve_levels(text: str, base: int) -> list[int]:
    """给一行文本算出每个字符的嵌入层级（只有 base / base+1 / base+2 三档）。"""
    n = len(text)
    types = [_bidi_type(ch) for ch in text]
    sor = "R" if base % 2 else "L"

    # BN 不参与解析：先记下位置，用相邻字符的层级填回去
    idx = [i for i in range(n) if types[i] != "BN"]
    seq = [types[i] for i in idx]
    m = len(seq)

    # W1：NSM 取前一个字符的类型（行首取 sor）
    for i in range(m):
        if seq[i] == "NSM":
            seq[i] = sor if i == 0 else seq[i - 1]
    # W2：EN 前面最近的强类型是 AL → AN
    last_strong = sor
    for i in range(m):
        if seq[i] in _STRONG_TYPES:
            last_strong = seq[i]
        elif seq[i] == "EN" and last_strong == "AL":
            seq[i] = "AN"
    # W3：AL → R
    seq = ["R" if t == "AL" else t for t in seq]
    # W4：单个 ES/CS 夹在两个 EN 之间 → EN；单个 CS 夹在两个 AN 之间 → AN
    for i in range(1, m - 1):
        if seq[i] == "ES" and seq[i - 1] == "EN" and seq[i + 1] == "EN":
            seq[i] = "EN"
        elif seq[i] == "CS" and seq[i - 1] == seq[i + 1] and seq[i - 1] in ("EN", "AN"):
            seq[i] = seq[i - 1]
    # W5：与 EN 相邻的一串 ET → EN
    i = 0
    while i < m:
        if seq[i] == "ET":
            j = i
            while j < m and seq[j] == "ET":
                j += 1
            if (i > 0 and seq[i - 1] == "EN") or (j < m and seq[j] == "EN"):
                for k in range(i, j):
                    seq[k] = "EN"
            i = j
        else:
            i += 1
    # W6：剩下的分隔符/终结符 → ON
    seq = ["ON" if t in ("ES", "ET", "CS") else t for t in seq]
    # W7：EN 前面最近的强类型是 L → L
    last_strong = sor
    for i in range(m):
        if seq[i] in ("L", "R"):
            last_strong = seq[i]
        elif seq[i] == "EN" and last_strong == "L":
            seq[i] = "L"
    # 括号规则（对 UAX#9 的一处有意偏离）：视觉流里紧贴在拉丁 run 左侧的开括号 / 右侧的闭括号
    # 一定属于那段拉丁文本——不管生成器写的是逻辑码位还是字形码位（"(A/77/L.3)"），
    # 让它跟着 L run 走就能还原成 "(A/77/L.3)"；按标准解析成嵌入方向反转后会得到 ")A/77/L.3("。
    chars = [text[i] for i in idx]
    for i in range(m):
        if seq[i] != "ON":
            continue
        if chars[i] in _OPENERS and i + 1 < m and seq[i + 1] == "L":
            seq[i] = "L"
        elif chars[i] in _CLOSERS and i > 0 and seq[i - 1] == "L":
            seq[i] = "L"
    # N1/N2：中性字符两侧同向则取该方向（EN/AN 视为 R），否则取嵌入方向
    i = 0
    while i < m:
        if seq[i] in _NEUTRAL_TYPES:
            j = i
            while j < m and seq[j] in _NEUTRAL_TYPES:
                j += 1
            before = sor if i == 0 else seq[i - 1]
            after = sor if j >= m else seq[j]
            before = "R" if before in ("EN", "AN") else before
            after = "R" if after in ("EN", "AN") else after
            fill = before if before == after else sor
            for k in range(i, j):
                seq[k] = fill
            i = j
        else:
            i += 1
    # I1/I2：按基础层级奇偶分配
    lv = []
    for t in seq:
        if base % 2 == 0:
            lv.append(base + 1 if t == "R" else base + 2 if t in ("AN", "EN") else base)
        else:
            lv.append(base + 1 if t in ("L", "EN", "AN") else base)

    levels = [base] * n
    for pos, level in zip(idx, lv):
        levels[pos] = level
    # BN 跟随前一个字符的层级（行首跟随后一个）
    for i in range(n):
        if types[i] == "BN":
            levels[i] = levels[i - 1] if i > 0 else (levels[1] if n > 1 else base)
    # L1：行尾空白回到基础层级（视觉行的两端空白已由调用方剥掉，这里只是保险）
    for i in range(n - 1, -1, -1):
        if types[i] in ("WS", "S", "B", "BN"):
            levels[i] = base
        else:
            break
    return levels


def _reorder(chars: list[str], levels: list[int]) -> list[str]:
    """L2：从最高层级到最低的奇数层级，逐层反转所有 ≥ 该层级的连续段。"""
    if not chars:
        return chars
    chars = list(chars)
    levels = list(levels)
    highest = max(levels)
    lowest_odd = min((lv for lv in levels if lv % 2), default=None)
    if lowest_odd is None:
        return chars
    for level in range(highest, lowest_odd - 1, -1):
        i = 0
        n = len(chars)
        while i < n:
            if levels[i] >= level:
                j = i
                while j < n and levels[j] >= level:
                    j += 1
                chars[i:j] = chars[i:j][::-1]
                levels[i:j] = levels[i:j][::-1]
                i = j
            else:
                i += 1
    return chars


def visual_to_logical(line: str, mirror: bool = False) -> str:
    """把一行视觉序文本还原成逻辑序。没有 RTL 字符时原样返回。

    基础方向按强字符多数决定（视觉行的首个强字符可能是逻辑上的最后一个，UAX#9 的 P2 不适用）。
    mirror=True 时把落在 RTL 层级上的括号类字符换成镜像配对（针对写字形码位的生成器）。
    """
    if not _has_rtl(line):
        return line
    stripped = line.strip()
    if not stripped:
        return line
    lead = line[: len(line) - len(line.lstrip())]
    trail = line[len(line.rstrip()):]
    rtl, ltr = _count_strong(stripped)
    base = 1 if rtl >= ltr else 0
    levels = _resolve_levels(stripped, base)
    chars = list(stripped)
    if mirror:
        chars = [
            _MIRROR_PAIRS.get(ch, ch) if (lv % 2 and ch in _MIRROR_PAIRS) else ch
            for ch, lv in zip(chars, levels)
        ]
    return lead + "".join(_reorder(chars, levels)) + trail


# ── 视觉序探测 ──────────────────────────────────────────────────────────

_TOKEN_RE = re.compile(r"\S+")
_AR_FINAL_ONLY = "ةى"  # 只出现在词尾的阿拉伯字母（teh marbuta / alef maqsura）
_HE_FINAL_ONLY = "ךםןףץ"  # 希伯来尾形字母
_RTL_PUNCT = "،؛,.:!?؟"
_WORD_EDGE_STRIP = "\"'“”‘’()[]{}«»*_"


def visual_rtl_votes(text: str) -> tuple[int, int]:
    """统计一段文本里「像视觉序」与「像逻辑序」的证据数，返回 (visual, logical)。"""
    visual = logical = 0
    for line in text.splitlines():
        if not _has_rtl(line):
            continue
        for raw in _TOKEN_RE.findall(line):
            tok = raw.strip(_WORD_EDGE_STRIP)
            if len(tok) < 2 or not _has_rtl(tok):
                continue
            first, last = tok[0], tok[-1]
            # 标点：逻辑序粘在词尾，视觉序粘在词首
            if first in _RTL_PUNCT and last not in _RTL_PUNCT:
                visual += 1
                tok = tok.lstrip(_RTL_PUNCT)
            elif last in _RTL_PUNCT and first not in _RTL_PUNCT:
                logical += 1
                tok = tok.rstrip(_RTL_PUNCT)
            if len(tok) < 2:
                continue
            first, last = tok[0], tok[-1]
            # 只能出现在词尾的字母
            if first in _AR_FINAL_ONLY or first in _HE_FINAL_ONLY:
                visual += 1
            if last in _AR_FINAL_ONLY or last in _HE_FINAL_ONLY:
                logical += 1
            # 冠词 ال：逻辑序在词首，视觉序反转后成了词尾的 لا（连字词会误投，权重同为 1）
            if len(tok) > 3:
                if tok.startswith("ال"):
                    logical += 1
                if tok.endswith("لا"):
                    visual += 1
    return visual, logical


def looks_visual_rtl(text: str, min_votes: int = 3) -> bool:
    """文档级判断：RTL 内容是否按视觉序存放。证据不足（不到 min_votes）时保守返回 False。"""
    visual, logical = visual_rtl_votes(text)
    return visual >= min_votes and visual > 2 * logical


def bracket_balance(lines: list[str]) -> tuple[int, int]:
    """数括号配对：返回 (配对成功数, 配错数)。用来决定还原后要不要整篇镜像。"""
    good = bad = 0
    for line in lines:
        stack: list[str] = []
        for ch in line:
            if ch in _OPENERS:
                stack.append(_CLOSE_OF[ch])
            elif ch in _CLOSERS:
                if stack and stack[-1] == ch:
                    stack.pop()
                    good += 1
                else:
                    bad += 1
        bad += len(stack)
    return good, bad


# ── 格式感知的逐行处理 ──────────────────────────────────────────────────

_MD_PREFIX_RE = re.compile(r"^(\s*(?:(?:#{1,6}|>|[-*+]|\d+[.)])\s+)*)")
_MD_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_MD_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_HTML_TOKEN_RE = re.compile(r"(<[^>]*>)")
_JSON_CONTENT_RE = re.compile(r'^(\s*"content"\s*:\s*)(".*")(\s*,?\s*)$')


_MD_CELL_SPLIT_RE = re.compile(r"(?<!\\)\|")
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)


def _fix_html_line(line: str, mirror: bool) -> str:
    """标签之间的每段文本各自还原（<br> 也是标签，所以表格单元格里的多段各自独立、顺序不变）。"""
    return "".join(
        tok if tok.startswith("<") else visual_to_logical(tok, mirror)
        for tok in _HTML_TOKEN_RE.split(line)
    )


def _is_md_table_row(line: str) -> bool:
    return line.lstrip().startswith("|")


def _fix_md_line(line: str, mirror: bool) -> str:
    if _is_md_table_row(line):
        if _MD_TABLE_SEP_RE.match(line):
            return line
        return "|".join(_fix_html_line(cell, mirror) for cell in _MD_CELL_SPLIT_RE.split(line))
    m = _MD_PREFIX_RE.match(line)
    prefix = m.group(1) if m else ""
    return prefix + _fix_html_line(line[len(prefix):], mirror)


def _fix_json_line(line: str, mirror: bool) -> str:
    m = _JSON_CONTENT_RE.match(line)
    if not m:
        return line
    try:
        value = json.loads(m.group(2))
    except json.JSONDecodeError:
        return line
    if not isinstance(value, str) or not _has_rtl(value):
        return line
    fixed = "\n".join(visual_to_logical(part, mirror) for part in value.split("\n"))
    return m.group(1) + json.dumps(fixed, ensure_ascii=False) + m.group(3)


def restore_rtl_text(text: str, kind: str, mirror: bool = False) -> tuple[str, int]:
    """按格式逐行把视觉序还原为逻辑序。kind ∈ {'txt','md','html','json'}。返回 (新文本, 改动行数)。

    调用方应先用 looks_visual_rtl() 在文档级确认是视觉序再调用；本函数不做该判断。
    """
    fixer = {"md": _fix_md_line, "html": _fix_html_line, "json": _fix_json_line}.get(
        kind, lambda s, m: visual_to_logical(s, m)
    )
    out: list[str] = []
    changed = 0
    in_fence = False
    ends_with_newline = text.endswith("\n")
    for line in text.split("\n"):
        if kind == "md" and _MD_FENCE_RE.match(line):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence or not _has_rtl(line):
            out.append(line)
            continue
        fixed = fixer(line, mirror)
        if fixed != line:
            changed += 1
        out.append(fixed)
    result = "\n".join(out)
    if ends_with_newline and not result.endswith("\n"):
        result += "\n"
    return result, changed


def decide_mirror(text: str, kind: str) -> bool:
    """还原一遍后数括号：配错多于配对说明生成器写的是字形码位，需要镜像。"""
    plain, _ = restore_rtl_text(text, kind, mirror=False)
    rtl_lines = [ln for ln in plain.splitlines() if _has_rtl(ln)]
    good, bad = bracket_balance(rtl_lines)
    return bad > good and bad >= 2


def kind_of_suffix(suffix: str) -> str:
    s = suffix.lower()
    if s == ".md":
        return "md"
    if s in (".html", ".htm"):
        return "html"
    if s == ".json":
        return "json"
    return "txt"


# ── 参照第二遍转换（keep_line_breaks）按物理行还原 ───────────────────────
#
# 引擎默认把一个段落里的多个物理行用空格拼成一行；视觉序整行反转会把**行的顺序**也倒过来
# （四条脚注拼成一段后倒着排）。所以视觉序文档要用 keep_line_breaks=True 再转一遍拿到物理行，
# 逐物理行还原后再按第一遍的行结构拼回去——产物形态与普通转换完全一致，只是顺序对了。
# 两遍产物按「块 / 内容值 / 标签间文本」一一对齐并校验（去空白后相等），对不上就退回整行还原。

_WS_RE = re.compile(r"\s+")


def _ws(s: str) -> str:
    """比较用的规范化：<br> 视同空白（第二遍把单元格内的物理换行写成 <br>），空白折叠。"""
    return _WS_RE.sub(" ", _BR_RE.sub(" ", s)).strip()


def _align_lines(lines1: list[str], lines2: list[str]) -> list[list[str]] | None:
    """把 lines2（物理行）分组对齐到 lines1（拼接行）：每组拼起来去空白后等于对应的 lines1。"""
    groups: list[list[str]] = []
    j = 0
    for target in lines1:
        want = _ws(target)
        group: list[str] = []
        acc = ""
        while True:
            if acc == want:
                break
            if j >= len(lines2):
                return None
            group.append(lines2[j])
            acc = _ws(acc + " " + lines2[j])
            j += 1
            if not want.startswith(acc):
                return None
        groups.append(group)
    if j != len(lines2) and any(_ws(x) for x in lines2[j:]):
        return None
    return groups


def _fix_group(target: str, group: list[str], kind: str, mirror: bool) -> str:
    """把一组物理行逐行还原后拼回 target 的形态（保留 target 的首尾空白）。"""
    fixer = {"md": _fix_md_line, "html": _fix_html_line, "cell": _fix_html_line}.get(
        kind, lambda s, m: visual_to_logical(s, m)
    )
    if len(group) <= 1:
        return fixer(target, mirror)
    lead = target[: len(target) - len(target.lstrip())]
    trail = target[len(target.rstrip()):]
    joined = " ".join(fixer(ln, mirror).strip() for ln in group if ln.strip())
    return lead + joined + trail


def _restore_blocks_with_reference(text1: str, text2: str, kind: str, mirror: bool) -> tuple[str, int] | None:
    """md/txt：按空行分块对齐，块内按行对齐。返回 None 表示对不上。"""
    parts1 = re.split(r"(\n[ \t]*\n+)", text1)
    parts2 = re.split(r"(\n[ \t]*\n+)", text2)
    blocks1 = parts1[0::2]
    blocks2 = parts2[0::2]
    if len(blocks1) != len(blocks2):
        return None
    if any(_ws(a) != _ws(b) for a, b in zip(blocks1, blocks2)):
        return None
    changed = 0
    out = list(parts1)
    in_fence = False
    for i, (b1, b2) in enumerate(zip(blocks1, blocks2)):
        if kind == "md" and _MD_FENCE_RE.match(b1):
            in_fence = not in_fence
        if in_fence or not _has_rtl(b1):
            continue
        lines1 = b1.split("\n")
        if kind == "md" and any(_is_md_table_row(ln) for ln in lines1):
            fixed, n = _restore_md_table_with_reference(b1, b2, mirror)
        else:
            groups = _align_lines(lines1, b2.split("\n"))
            if groups is None:
                fixed, n = restore_rtl_text(b1, kind, mirror)
            else:
                new_lines = [_fix_group(t, g, kind, mirror) for t, g in zip(lines1, groups)]
                fixed = "\n".join(new_lines)
                n = sum(1 for a, b in zip(lines1, new_lines) if a != b)
        out[i * 2] = fixed
        changed += n
    return "".join(out), changed


def _restore_md_table_with_reference(b1: str, b2: str, mirror: bool) -> tuple[str, int]:
    """表格块：第二遍把单元格内的物理换行写成 <br>，按行→格→<br> 段三级对齐。对不上的格退回整格还原。"""
    rows1 = b1.split("\n")
    rows2 = b2.split("\n")
    if len(rows1) != len(rows2):
        return restore_rtl_text(b1, "md", mirror)
    out_rows: list[str] = []
    changed = 0
    for r1, r2 in zip(rows1, rows2):
        if not _has_rtl(r1) or _MD_TABLE_SEP_RE.match(r1) or not _is_md_table_row(r1):
            out_rows.append(_fix_md_line(r1, mirror) if _has_rtl(r1) else r1)
            changed += out_rows[-1] != r1
            continue
        cells1 = _MD_CELL_SPLIT_RE.split(r1)
        cells2 = _MD_CELL_SPLIT_RE.split(r2)
        if len(cells1) != len(cells2):
            fixed = _fix_md_line(r1, mirror)
        else:
            new_cells: list[str] = []
            for c1, c2 in zip(cells1, cells2):
                if not _has_rtl(c1):
                    new_cells.append(c1)
                    continue
                segs1 = _BR_RE.split(c1)
                segs2 = _BR_RE.split(c2)
                groups = _align_lines(segs1, segs2)
                if groups is None or _ws(c1) != _ws(" ".join(segs2)):
                    new_cells.append(_fix_html_line(c1, mirror))
                else:
                    new_cells.append("<br>".join(_fix_group(t, g, "cell", mirror) for t, g in zip(segs1, groups)))
            fixed = "|".join(new_cells)
        changed += fixed != r1
        out_rows.append(fixed)
    return "\n".join(out_rows), changed


def _restore_json_with_reference(text1: str, text2: str, mirror: bool) -> tuple[str, int] | None:
    lines1 = text1.split("\n")
    lines2 = text2.split("\n")
    if len(lines1) != len(lines2):
        return None
    out: list[str] = []
    changed = 0
    for l1, l2 in zip(lines1, lines2):
        m1 = _JSON_CONTENT_RE.match(l1)
        if not m1 or not _has_rtl(l1):
            out.append(l1)
            continue
        m2 = _JSON_CONTENT_RE.match(l2)
        try:
            v1 = json.loads(m1.group(2))
            v2 = json.loads(m2.group(2)) if m2 else None
        except json.JSONDecodeError:
            out.append(l1)
            continue
        if isinstance(v2, str) and _ws(v2) == _ws(v1):
            fixed = " ".join(visual_to_logical(p, mirror).strip() for p in v2.split("\n") if p.strip())
        else:
            fixed = visual_to_logical(v1, mirror)
        new = m1.group(1) + json.dumps(fixed, ensure_ascii=False) + m1.group(3)
        if new != l1:
            changed += 1
        out.append(new)
    return "\n".join(out), changed


def _restore_html_with_reference(text1: str, text2: str, mirror: bool) -> tuple[str, int] | None:
    toks1 = _HTML_TOKEN_RE.split(text1)
    toks2 = _HTML_TOKEN_RE.split(text2)
    if len(toks1) != len(toks2):
        return None
    if any(_ws(a) != _ws(b) for a, b in zip(toks1, toks2)):
        return None
    changed = 0
    out: list[str] = []
    for t1, t2 in zip(toks1, toks2):
        if t1.startswith("<") or not _has_rtl(t1):
            out.append(t1)
            continue
        lead = t1[: len(t1) - len(t1.lstrip())]
        trail = t1[len(t1.rstrip()):]
        fixed = lead + " ".join(visual_to_logical(p, mirror).strip() for p in t2.split("\n") if p.strip()) + trail
        if fixed != t1:
            changed += 1
        out.append(fixed)
    return "".join(out), changed


def restore_rtl_text_with_reference(text1: str, text2: str, kind: str, mirror: bool = False) -> tuple[str, int]:
    """用第二遍（保留物理行）的产物 text2 做参照，还原第一遍产物 text1。对不齐时退回逐行还原。"""
    if kind == "json":
        res = _restore_json_with_reference(text1, text2, mirror)
    elif kind == "html":
        res = _restore_html_with_reference(text1, text2, mirror)
    else:
        res = _restore_blocks_with_reference(text1, text2, kind, mirror)
    if res is None:
        return restore_rtl_text(text1, kind, mirror)
    return res
