"""评测指标（bench/PLAN.md §3）。

所有函数只吃纯 Python 对象（字符串 / 列表），不碰文件，方便单测。
文本比较前统一 ``norm_text``：NFKC 之外**不改字**，只折叠空白、去 Markdown 装饰符，
让「引擎输出的 Markdown」和「真值纯文本」站在同一基线上。
"""
from __future__ import annotations

import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path

try:  # 可选加速：rapidfuzz（C 实现的编辑距离）
    from rapidfuzz.distance import Levenshtein as _Lev
except Exception:  # pragma: no cover
    _Lev = None

# 复用主项目的兼容码位表（康熙部首等）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    from aimorsel.morsel import _COMPAT_MAP  # type: ignore
except Exception:  # pragma: no cover
    _COMPAT_MAP = {}
try:
    from aimorsel.rtl_text import visual_rtl_votes as _rtl_votes  # type: ignore
except Exception:  # pragma: no cover
    _rtl_votes = None

# 公式：真值解析器（from_latex / from_html）统一把公式剥成一个空格并记 note.math_stripped，
# 所以**产物侧必须同样剥掉**——否则「正确输出了 $…$」反而被当成凭空多出的字符扣分
# （维基公式页实测单份最多 +0.076，HTML 子集 0.856→0.858）。两侧同口径后，PDF 里根本
# 还原不出 LaTeX 的公式段也不再算进任何引擎的分母，五个引擎公平。
# ⚠️ `$` 也是货币符号：行内式只有定界符内含 LaTeX 特征（反斜杠 / `^` / `_`）才算公式，
# 否则 `$100 与 $200` 会被当成一段公式吃掉中间的正文。花括号**不算**特征——
# `花了 $5 买 {苹果} 和 $10` 会被整段吞掉，而只含 `{}` 没有 `\`/`^`/`_` 的公式实际不存在。
# 块级式同样要判据，而且内部**不许出现 `$`、不许跨空行、限长 200**——OCR 产物里
# 这三条都是被实测逼出来的：① 德语法条的 `§§` 被认成 `$$`，早期写法 `\$\$.+?\$\$` + re.S
# 把两处游离 `$$` 之间的 **7306 个字符**整段吃掉（char_sim 0.939→0.492）；
# ② 只加「内部无 `$`」还不够，另一份 OCR 产物里 `$$` 后跟着整段正文（`Conventions_` 这种
# 误认的下划线正好满足特征），又吃掉 300 字符。真正的 display 公式短、且不含空行。勿回退。
_TEX_BODY = r"(?:[^$\n]|\n(?![ \t]*\n))"   # 非 `$`、可跨单个换行、不跨空行
_TEX_BLOCK = re.compile(rf"\$\${_TEX_BODY}{{0,200}}[\\^_]{_TEX_BODY}{{0,200}}\$\$")
# 限长 60 是**测出来的甜点**（在 wiki 公式页与 markitdown 的货币密集页上扫 25/40/60/300）：
# 300 时维基公式页收益最大（zh +0.083）但 markitdown 的 `[链接_路径](…)$货币$` 会被跨段吃掉
# （wiki_en_stock_market −0.006）；60 保住九成收益（zh +0.076）而误伤归零（−0.0004）。
_TEX_INLINE = re.compile(r"\$[^$\n]{0,60}?[\\^_][^$\n]{0,60}?\$")
_MD_DECOR = re.compile(r"[#*_`>|]+")
# 表格分隔行（|---|:--:|）与行首项目符号/编号（- • * 1. 1)）：结构装饰，不算正文差异
_MD_TABLE_SEP = re.compile(r"^[ \t]*\|?[ \t]*:?-{2,}:?[ \t]*(\|[ \t]*:?-{2,}:?[ \t]*)*\|?[ \t]*$", re.M)
_MD_BULLET = re.compile(r"^[ \t]*(?:(?:[-*+•·]|\d{1,3}[.)])[ \t]+)+", re.M)
_MD_IMG = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
# ⚠️ 必须限制成「合法标签名开头」：原来写成 `<[^>]+>` 时，物理/数学正文里的
# `q < qc … T > Tc` 会被当成一个标签，把中间整段吃掉——实测一份 arXiv 产物
# 归一化后从 61312 字缩到 22268 字，凭空「丢」了 39044 字，char_sim 因此严重失真。
_HTML_TAG = re.compile(r"</?[A-Za-z][A-Za-z0-9-]*(\s[^<>]*)?/?>")
_WS = re.compile(r"\s+")
_CJK = r"[\u4e00-\u9fff\u3400-\u4dbf\u3040-\u30ff\u3000-\u303f\uff00-\uffef]"
_CJK_SPACE = re.compile(rf"(?<={_CJK}) +(?={_CJK})")


def norm_text(s: str) -> str:
    """折叠空白、去 Markdown/HTML 装饰、去 BOM；不做 NFKC（避免把全角标点算作差异）。"""
    s = s.replace("﻿", "")
    # 公式先剥：内部的 \ ^ _ 正是下面几条正则要动的字符，晚剥就认不出来了
    s = _TEX_BLOCK.sub(" ", s)
    s = _TEX_INLINE.sub(" ", s)
    s = _MD_IMG.sub(" ", s)
    s = _MD_LINK.sub(r"\1", s)
    s = _HTML_TAG.sub(" ", s)
    s = _MD_TABLE_SEP.sub(" ", s)
    s = _MD_BULLET.sub(" ", s)
    s = _MD_DECOR.sub(" ", s)
    s = _WS.sub(" ", s)
    s = _CJK_SPACE.sub("", s)  # CJK 之间的空格无语义（换行处被引擎补的空格），单独用 cjk_inner_spaces 计数
    return s.strip()


_H_SPACE = re.compile(r"[ \t]+")


def cjk_inner_spaces(s: str) -> int:
    """CJK 字符之间被塞进的空格数（典型：换行处 "看 起来"）。真值文本里应为 0。

    ⚠️ **只数行内的水平空白**。原来先 `_WS.sub(" ", s)` 把换行也折成空格，于是
    `## 背景\n\n一貫性モデルは…` 这种**正常的段落/标题边界**被计成一次命中——
    抽检时实测一份完全没有行内空格的 pptx 产物被报成 3。这会系统性偏向
    「输出结构更少」的引擎，用它做跨引擎对比会得出反的结论。
    """
    return sum(len(_CJK_SPACE.findall(_H_SPACE.sub(" ", line))) for line in s.splitlines())


def _cjk_ratio(s: str) -> float:
    if not s:
        return 0.0
    cjk = sum(1 for ch in s if "一" <= ch <= "鿿" or "㐀" <= ch <= "䶿")
    return cjk / len(s)


def _tokens(s: str) -> list[str]:
    """CJK 按字、其余按词切——阿拉伯语/西语用词级，中文用字级，CER 才有可比性。"""
    out: list[str] = []
    buf = ""
    for ch in s:
        if "一" <= ch <= "鿿" or "㐀" <= ch <= "䶿" or "぀" <= ch <= "ヿ":
            if buf:
                out.append(buf)
                buf = ""
            out.append(ch)
        elif ch.isspace():
            if buf:
                out.append(buf)
                buf = ""
        else:
            buf += ch
    if buf:
        out.append(buf)
    return out


#: 超过这个规模就先截断再算距离（两边同样截断），并在结果里标 metrics_truncated。
#: 纯粹是防炸的保险绳：真实语料里 SEC 的 10-K 真值有 1.2 MB，指标一算就是分钟级。
MAX_METRIC_CHARS = 400_000
MAX_METRIC_TOKENS = 150_000


def _levenshtein(a, b) -> int:
    # ⚠️ 别加 isinstance(a, str) 限制：rapidfuzz 对 list[str] 一样支持，
    # 而词级 CER 传进来的正是 list。曾因这个限制退回下面的纯 Python DP，
    # 一份 1.2 MB 的 SEC 10-K 的词级 CER 跑了 20 分钟还没完，整批被一份文档卡死。
    # 实测同样 12 万 token 的比较：rapidfuzz 0.64 s，纯 DP 要几小时。
    if _Lev is not None:
        try:
            return _Lev.distance(a, b)
        except (TypeError, ValueError):
            pass
    # 通用 DP（序列可以是 list[str]），O(len(a)*len(b))，只在无 rapidfuzz 时兜底
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


# ---------- 3.1 文本保真 ----------

def char_sim(pred: str, truth: str) -> float:
    """字符级相似度 = 1 - 编辑距离/max(len)。两边先 norm_text。空对空 = 1。"""
    p, t = norm_text(pred), norm_text(truth)
    if not p and not t:
        return 1.0
    if not p or not t:
        return 0.0
    if max(len(p), len(t)) > MAX_METRIC_CHARS:
        p, t = p[:MAX_METRIC_CHARS], t[:MAX_METRIC_CHARS]
    if _Lev is not None:
        return float(_Lev.normalized_similarity(p, t))
    return 1.0 - _levenshtein(p, t) / max(len(p), len(t))


def cer(pred: str, truth: str) -> float:
    """错误率（越低越好）：CJK 主导的文本按字算，其余按词算（即 WER）。真值为空返回 0/1。"""
    p, t = norm_text(pred), norm_text(truth)
    if not t:
        return 0.0 if not p else 1.0
    if _cjk_ratio(t) > 0.3:
        return min(1.0, _levenshtein(p.replace(" ", ""), t.replace(" ", "")) / max(1, len(t.replace(" ", ""))))
    tp, tt = _tokens(p), _tokens(t)
    if max(len(tp), len(tt)) > MAX_METRIC_TOKENS:
        tp, tt = tp[:MAX_METRIC_TOKENS], tt[:MAX_METRIC_TOKENS]
    return min(1.0, _levenshtein(tp, tt) / max(1, len(tt)))


def compat_residual(pred: str) -> int:
    """输出里残留的兼容码位数（康熙部首/兼容表意/连字）。目标恒为 0。"""
    if not _COMPAT_MAP:
        return 0
    return sum(1 for ch in pred if ord(ch) in _COMPAT_MAP)


def rtl_visual_ratio(pred: str) -> float | None:
    """RTL 文本按视觉序（逐字符反转）存放的证据占比，0=全是逻辑序，1=全是视觉序；无 RTL 证据返回 None。

    证据来自词形统计（ة/ى 出现在词首、冠词 ال 反转成词尾 لا、希伯来尾形字母在词首、标点粘词首），
    与主项目 rtl_text.looks_visual_rtl 同一套判据。issue #0 的量化口径：目标恒为 0（或 None）。
    """
    if _rtl_votes is None:
        return None
    visual, logical = _rtl_votes(pred)
    if visual + logical == 0:
        return None
    return round(visual / (visual + logical), 3)


def length_ratio(pred: str, truth: str) -> float:
    """输出长度 / 真值长度。<0.8 疑似丢内容，>1.3 疑似重复/噪声。"""
    p, t = norm_text(pred), norm_text(truth)
    return len(p) / len(t) if t else (0.0 if not p else float("inf"))


# ---------- 3.2 结构 ----------

_H_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$", re.M)


def md_headings(md: str) -> list[str]:
    """从 Markdown 抽标题文本（忽略级别，只比文本）。"""
    return [norm_text(m.group(2)) for m in _H_RE.finditer(md)]


def _fuzzy_match_sets(pred: list[str], truth: list[str], thresh: float = 0.85) -> tuple[int, int, int]:
    """贪心一一匹配，返回 (tp, fp, fn)。"""
    used = [False] * len(truth)
    tp = 0
    for p in pred:
        best, bi = 0.0, -1
        for i, t in enumerate(truth):
            if used[i]:
                continue
            s = char_sim(p, t)
            if s > best:
                best, bi = s, i
        if bi >= 0 and best >= thresh:
            used[bi] = True
            tp += 1
    return tp, len(pred) - tp, len(truth) - tp


def prf(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    p = tp / (tp + fp) if tp + fp else (1.0 if fn == 0 else 0.0)
    r = tp / (tp + fn) if tp + fn else 1.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    return p, r, f


def heading_f1(pred_headings: list[str], truth_headings: list[str]) -> float:
    if not pred_headings and not truth_headings:
        return 1.0
    return prf(*_fuzzy_match_sets(pred_headings, truth_headings))[2]


_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_SEP_ROW = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")


def md_tables(md: str) -> list[list[list[str]]]:
    """从 Markdown 抽管道表格 → [table][row][cell]（去掉分隔行）。"""
    tables, cur = [], []
    for line in md.splitlines():
        if _TABLE_ROW.match(line):
            if _SEP_ROW.match(line):
                continue
            cells = [norm_text(c) for c in line.strip().strip("|").split("|")]
            cur.append(cells)
        else:
            if cur:
                tables.append(cur)
                cur = []
    if cur:
        tables.append(cur)
    return tables


def cell_f1(pred_tables: list[list[list[str]]], truth_tables: list[list[list[str]]]) -> float:
    """所有表格的单元格拍平成多重集，按精确文本匹配算 F1（不惩罚合并/拆分表）。"""
    from collections import Counter

    pc = Counter(c for t in pred_tables for r in t for c in r if c)
    tc = Counter(c for t in truth_tables for r in t for c in r if c)
    if not pc and not tc:
        return 1.0
    tp = sum((pc & tc).values())
    return prf(tp, sum(pc.values()) - tp, sum(tc.values()) - tp)[2]


def table_count_diff(pred_tables, truth_tables) -> int:
    return len(pred_tables) - len(truth_tables)


# ---------- 3.3 阅读顺序 ----------

def order_tau(pred: str, truth_paragraphs: list[str], min_len: int = 12) -> float | None:
    """真值段落在输出里的出现位置的 Kendall τ（-1..1）。找不到 ≥2 个锚点时返回 None。"""
    p = norm_text(pred)
    pos = []
    for para in truth_paragraphs:
        key = norm_text(para)
        if len(key) < min_len:
            continue
        key = key[:40]  # 只用段首 40 字定位，减少 OCR 抖动影响
        i = p.find(key)
        if i >= 0:
            pos.append(i)
    n = len(pos)
    if n < 2:
        return None
    conc = disc = 0
    for i in range(n):
        for j in range(i + 1, n):
            if pos[j] > pos[i]:
                conc += 1
            elif pos[j] < pos[i]:
                disc += 1
    tot = conc + disc
    return (conc - disc) / tot if tot else 1.0


# ---------- 3.4 数字保真（issue #13）----------

# 数字串：允许千分位分隔符与小数点，但**不允许普通空格**——表格行里
# `1,234.00 5,678.00` 会被并成一个 token，宁可漏掉法式空格千分位也不能把两个数合并。
_NUM_RE = re.compile(r"[-−–—]?\d[\d,.'\u00a0\u202f]*\d|[-−–—]?\d")
_FULLWIDTH_NUM = str.maketrans("０１２３４５６７８９．，－＋％", "0123456789.,-+%")


def _canon_number(tok: str) -> str | None:
    """把一个数字串归一成规范形（去千分位、统一小数点、去无意义的前后导零），保留正负号。

    分隔符判定：同时出现 `,` 和 `.` 时**最后出现的那个是小数点**，另一个是千分位；
    只出现一种时：重复出现 = 千分位（`1.234.567`）；单个 `,` 后接正好 3 位 = 千分位（`1,234`）；
    其余（含单个 `.`）= 小数点（`18.22` / `3.141` / `9,54`）。这是英美式约定，
    德式 `1.234`（一千二百三十四）会被判成 1.234——但**真值与输出走同一套归一**，
    系统性误判在两侧同时发生、互相抵消，不影响比对结论。
    """
    tok = tok.translate(_FULLWIDTH_NUM)
    neg = tok[0] in "-−–—"
    body = tok.lstrip("+-−–—").replace("\u00a0", "").replace("\u202f", "").replace("'", "")
    body = body.rstrip(".,")           # 句末的点、行内的逗号不算数字的一部分
    if not body or not body[0].isdigit():
        return None
    has_c, has_d = "," in body, "." in body
    if has_c and has_d:
        dec = "," if body.rfind(",") > body.rfind(".") else "."
        grp = "." if dec == "," else ","
        body = body.replace(grp, "").replace(dec, ".")
    elif has_c or has_d:
        sep = "," if has_c else "."
        tail = body.rsplit(sep, 1)[1]
        # 只出现一次的 `.` 一律当小数点（否则 `3.141` 会被读成 3141）；
        # 只出现一次的 `,` 后接正好 3 位才当千分位。重复出现的一律是千分位。
        grouping = body.count(sep) > 1 or (sep == "," and len(tail) == 3)
        body = body.replace(sep, "") if grouping else body.replace(sep, ".")
    if not body.replace(".", "").isdigit() or body.count(".") > 1:
        return None
    if "." in body:
        ip, fp = body.split(".")
        fp = fp.rstrip("0")
        body = f"{ip}.{fp}" if fp else ip
    body = body.lstrip("0") or "0"
    if body.startswith("."):
        body = "0" + body
    return ("-" if neg and body != "0" else "") + body


def numbers(s: str) -> list[str]:
    """从文本里抽出全部数字串（已归一）。顺序即出现顺序。"""
    out = []
    for m in _NUM_RE.finditer(norm_text(s)):
        c = _canon_number(m.group(0))
        if c is not None:
            out.append(c)
    return out


def digit_stats(pred: str, truth: str) -> dict:
    """数字串一致性（**多重集**比对，不看位置）。

    issue #13：财报扫描件的 char_sim 有 0.6+，但 `132,704,932.32` 被认成 `132,701,932.32`、
    负号被吃掉——文本相似度对这类**静默错值**几乎不敏感（一个字符的差异），而它是后果最重的错。
    这里单拎数字出来算：`digit_recall` = 真值里的数字有多少被原样输出，
    `digit_precision` = 输出的数字里有多少在真值里存在（**1 - precision 就是凭空造出来的错值占比**）。
    多重集口径 → 同一个数出现两次要两次都对；**位置错乱不计入**（那是 order_tau / cell_f1 的事）。
    """
    cp, ct = Counter(numbers(pred)), Counter(numbers(truth))
    np_, nt = sum(cp.values()), sum(ct.values())
    tp = sum((cp & ct).values())
    prec = tp / np_ if np_ else (1.0 if not nt else 0.0)
    rec = tp / nt if nt else (1.0 if not np_ else 0.0)
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    return {"digit_f1": round(f1, 4), "digit_precision": round(prec, 4),
            "digit_recall": round(rec, 4), "digit_n_truth": nt, "digit_n_pred": np_}


# ---------- 汇总 ----------

def score_document(pred_md: str, truth: dict) -> dict:
    """truth: {"text": str, "headings": [..], "tables": [[[..]]], "paragraphs": [..]}（字段可缺）。"""
    out: dict = {"compat_residual": compat_residual(pred_md), "cjk_inner_spaces": cjk_inner_spaces(pred_md),
                 "rtl_visual_ratio": rtl_visual_ratio(pred_md)}
    if "text" in truth:
        if max(len(norm_text(pred_md)), len(norm_text(truth["text"]))) > MAX_METRIC_CHARS:
            # 超长文档只比前 MAX_METRIC_CHARS 个字符，报告里要能看出这一点
            out["metrics_truncated"] = True
        out["char_sim"] = round(char_sim(pred_md, truth["text"]), 4)
        out["cer"] = round(cer(pred_md, truth["text"]), 4)
        out["length_ratio"] = round(length_ratio(pred_md, truth["text"]), 3)
        ds = digit_stats(pred_md, truth["text"])
        if ds["digit_n_truth"] or ds["digit_n_pred"]:  # 两侧都没有数字的文档不产出这组指标（否则恒为 1.0，把均值抬虚）
            out.update(ds)
    if "headings" in truth:
        out["heading_f1"] = round(heading_f1(md_headings(pred_md), truth["headings"]), 4)
    if "tables" in truth:
        pt = md_tables(pred_md)
        out["cell_f1"] = round(cell_f1(pt, truth["tables"]), 4)
        out["table_count_diff"] = table_count_diff(pt, truth["tables"])
    if "paragraphs" in truth:
        tau = order_tau(pred_md, truth["paragraphs"])
        out["order_tau"] = None if tau is None else round(tau, 4)
    return out
