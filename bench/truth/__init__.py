"""A 档真值解析器（bench/PLAN.md §2）。

三个解析器，输入「有语义的源文件」，输出与 ``bench/make_synthetic.py`` **完全同一套**
真值 JSON schema（`bench/metrics.py::score_document` 直接消费）：

```
{
  "text":       str,            # 全文（阅读顺序，块之间以空格相连）
  "headings":   [str, ...],     # 标题文本（**没有可靠标题时整个键省略**，见下）
  "heading_levels": [int, ...], # 与 headings 平行的层级（1-6）；score_document 忽略，留给 heading_level_acc
  "paragraphs": [str, ...],     # 正文段落（order_tau 的锚点）
  "tables":     [[[str]]],      # [表][行][单元格]
  "note":       {...}           # 取舍记录（math_stripped / infobox_dropped / …），非指标
}
```

**关键约定：真值里「有这个键」= 这份文档要算这项指标。**
`score_document` 用 `if "headings" in truth` 判断，所以**抓不到标题时必须省略键，
而不是写 `[]`**——写空数组会让所有引擎的 heading_f1 变成 0（惩罚它们抓到了真标题），
省略键才是「这份文档不评这项」。同理 tables 抓不到就省略。

这些解析器**刻意不 import 项目根的 `format_adapters.py`**：真值必须与被测代码解耦，
否则等于拿被测程序的输出当自己的真值。
"""
from __future__ import annotations

import re

__all__ = ["norm_ws", "build_truth", "clean_cell", "row_text"]

_WS = re.compile(r"[ \t\r\f\v ​]+")
_NL = re.compile(r"\s*\n\s*")


def norm_ws(s: str) -> str:
    """折叠空白（含 NBSP / 零宽空格），保留字符本身。真值文本里不留多余空白。"""
    if not s:
        return ""
    s = s.replace("­", "")  # soft hyphen
    s = _NL.sub(" ", s)
    s = _WS.sub(" ", s)
    return s.strip()


def clean_cell(s: str) -> str:
    return norm_ws(s)


def row_text(row: list[str]) -> str:
    return " ".join(c for c in (norm_ws(c) for c in row) if c)


def build_truth(
    blocks: list[tuple[str, object]],
    *,
    note: dict | None = None,
    with_headings: bool = True,
    with_tables: bool = True,
) -> dict:
    """把 ``[(kind, payload)]`` 阅读顺序块序列组装成真值 dict。

    kind ∈ {"heading", "para", "table"}：
      - heading payload = (text, level)
      - para    payload = text
      - table   payload = [[cell]]

    ``with_headings=False`` / ``with_tables=False`` 时**省略对应键**（不评该指标），
    用于 SEC EDGAR 这类无语义标签、标题不可靠的文档。
    """
    text_parts: list[str] = []
    headings: list[str] = []
    levels: list[int] = []
    paragraphs: list[str] = []
    tables: list[list[list[str]]] = []
    for kind, payload in blocks:
        if kind == "heading":
            t, lvl = payload  # type: ignore[misc]
            t = norm_ws(t)
            if not t:
                continue
            headings.append(t)
            levels.append(max(1, min(6, int(lvl))))
            text_parts.append(t)
        elif kind == "para":
            t = norm_ws(payload)  # type: ignore[arg-type]
            if not t:
                continue
            paragraphs.append(t)
            text_parts.append(t)
        elif kind == "table":
            rows = [[clean_cell(c) for c in r] for r in payload]  # type: ignore[union-attr]
            rows = [r for r in rows if any(r)]
            if not rows:
                continue
            tables.append(rows)
            text_parts.extend(row_text(r) for r in rows)
        else:  # pragma: no cover - 调用方 bug
            raise ValueError(f"未知块类型 {kind!r}")

    truth: dict = {"text": " ".join(p for p in text_parts if p)}
    if with_headings:
        truth["headings"] = headings
        truth["heading_levels"] = levels
    if with_tables:
        truth["tables"] = tables
    truth["paragraphs"] = paragraphs
    if note:
        truth["note"] = note
    return truth
