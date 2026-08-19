#!/usr/bin/env python3
"""汇总 results/<engine>.jsonl → RESULTS.md（总表 + 按格式/语言/领域分表 + 失败清单 + 图）。

    python -m bench.report [--out bench/RESULTS.md]
"""
from __future__ import annotations

import argparse
import json
import statistics as st
from collections import defaultdict
from pathlib import Path

BENCH = Path(__file__).resolve().parent
QUALITY = ["char_sim", "cer", "heading_f1", "cell_f1", "order_tau", "digit_f1"]
OCR_FORMATS = {"png", "jpg", "jpeg", "tiff", "webp", "gif", "scan-pdf"}  # 走 OCR 的输入
DIGIT_DENSE_MIN = 30  # 数字保真专项的入选门槛（真值里的数字串条数）
ENGINE_ORDER = ["aimorsel", "docling", "pymupdf4llm", "markitdown", "pdfplumber_txt", "marker", "mineru"]


def load() -> list[dict]:
    rows = []
    for f in sorted((BENCH / "results").glob("*.jsonl")):
        for l in f.read_text(encoding="utf-8").splitlines():
            if l.strip():
                rows.append(json.loads(l))
    return rows


def mean(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return round(st.mean(xs), 3) if xs else None


def median(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return round(st.median(xs), 2) if xs else None


def p95(xs):
    xs = sorted(x for x in xs if isinstance(x, (int, float)))
    return round(xs[int(len(xs) * 0.95) - 1] if len(xs) >= 20 else xs[-1], 1) if xs else None


def fmt(v):
    if v is None:
        return "–"
    if isinstance(v, float):
        return f"{v:.1f}" if v > 5 else f"{v:.3f}"
    return str(v)


def summarize(rows: list[dict]) -> dict:
    n = len(rows)
    by = defaultdict(int)
    for r in rows:
        by[r.get("status")] += 1
    ok = [r for r in rows if r.get("status") in ("ok", "degraded")]
    out = {"n": n, "ok%": round(100 * len(ok) / n, 1) if n else None,
           "degraded": by.get("degraded", 0), "fail": by.get("fail", 0), "timeout": by.get("timeout", 0),
           "unsupported": by.get("unsupported", 0)}
    for k in QUALITY:
        out[k] = mean([r.get(k) for r in ok])
    out["compat"] = sum(r.get("compat_residual") or 0 for r in ok)
    out["cjk_sp"] = sum(r.get("cjk_inner_spaces") or 0 for r in ok)
    # RTL 视觉序：有 RTL 证据的文档里，被判成视觉序（比例 > 2/3）的份数 / 有 RTL 证据的份数
    rtl = [r.get("rtl_visual_ratio") for r in ok if r.get("rtl_visual_ratio") is not None]
    out["rtl_vis"] = f"{sum(1 for x in rtl if x > 2 / 3)}/{len(rtl)}" if rtl else None
    out["s/page"] = median([r.get("s_per_page") for r in ok])
    out["wall_med"] = median([r.get("wall_s") for r in ok])
    out["wall_p95"] = p95([r.get("wall_s") for r in ok])
    out["rss_p95"] = p95([r.get("peak_rss_mb") for r in ok])
    return out


COLS = [("n", "n"), ("ok%", "成功%"), ("degraded", "降级"), ("fail", "失败"), ("timeout", "超时"), ("unsupported", "不支持"),
        ("char_sim", "字符相似↑"), ("cer", "CER↓"), ("heading_f1", "标题F1↑"), ("cell_f1", "单元格F1↑"), ("order_tau", "顺序τ↑"), ("digit_f1", "数字F1↑"),
        ("compat", "兼容码位残留"), ("cjk_sp", "CJK内空格"), ("rtl_vis", "RTL视觉序(份)"), ("s/page", "秒/页(中位)"), ("wall_med", "秒/文档(中位)"),
        ("wall_p95", "秒/文档(p95)"), ("rss_p95", "峰值MB(p95)")]


def table(groups: dict[str, list[dict]], first_col: str) -> str:
    lines = ["| " + first_col + " | " + " | ".join(c[1] for c in COLS) + " |",
             "|" + "---|" * (len(COLS) + 1)]
    for key, rows in groups.items():
        s = summarize(rows)
        lines.append(f"| {key} | " + " | ".join(fmt(s[c[0]]) for c in COLS) + " |")
    return "\n".join(lines)


def engines_sorted(names) -> list[str]:
    return sorted(names, key=lambda e: (ENGINE_ORDER.index(e) if e in ENGINE_ORDER else 99, e))


#: 读数字之前必须知道的口径。写在这里而不是手改 RESULTS.md——那个文件每次重跑都会覆盖。
CAVEATS = """## 读这份表之前（口径说明）

这些不是脚注，是**不知道就会读错**的八件事：

1. **docling 只跑了分层子集**（约 300 份，其余引擎跑全量 731 份）。它 ~9 s/份，全量跑不完。
   抽样按 (格式, 语言) 轮转、固定种子，可复现。**跨引擎比较一律看「公平对比」那张交集表**，
   总表里 docling 的 n 与别人不同，直接比均值是错的。
2. **arXiv 那批（44 份）的真值把公式统一剥成了空格**（单篇最多剥掉 1356 处）。
   评分时**产物侧也剥**（见下面第 8 条），所以「正确输出成 `$…$` 的公式」不再被倒扣；
   但 PDF 里的公式早已是字形，引擎只能抽成**裸文本**（没有 `$` 定界符，剥不掉），
   那部分仍算差异，于是 char_sim 只有 0.38–0.54。
   **那不是文本保真度，是「这篇公式有多密」**。看 `domain = math` 的分表时务必记得。
3. **char_sim 会被 Markdown 表格标记拉低**。表格密集的文档（SEC 10-K、财报）里，
   产物的 `|` 分隔符算进了字符差异——实测出现过 char_sim 0.94 而词级 CER 0.0005 的组合。
   **表格密集文档以 CER 与 cell_f1 为准。**
4. **`cell_f1 = 0` 未必是引擎的错**。真值生成时丢弃了维基信息框、把 EUR-Lex 的单行版面表
   降级成了段落（否则从 PDF 转换的引擎会被结构性判零），而引擎照实输出了这些表格。
   看到 cell_f1 = 0 先分辨是谁的问题。
5. **超长文档的指标是截断比对的**：任一侧超过 40 万字符（或 15 万 token）时两边同样截断，
   该行会带 `metrics_truncated`。不截断的话一份 1.2 MB 的 SEC 10-K 能把整批卡住。

6. **pymupdf4llm 在无文字层的 PDF 上会自动调用 Tesseract OCR**（本机装了 tesseract，
   它自己打印 `Using Tesseract for OCR processing`）。所以它在 `scan-pdf` 一列的分数
   **是 Tesseract 的 OCR 成绩，不是文本抽取**，且**换一台没装 tesseract 的机器数字会变**。
   跨引擎读 scan-pdf 那一行时务必知道这件事。

7. **`数字F1` 是多重集比对，不看位置**：同一个数在文档里出现两次就要两次都对，但**行内顺序错乱不计入**
   （那是 `顺序τ` 与 `单元格F1` 的事，见 ISSUES-draft #7）。归一化按英美式约定：单个 `.` 一律当小数点，
   单个 `,` 后接正好 3 位当千分位；德/法式 `1.234`（一千二百三十四）会被读成 1.234——但**真值与输出走同一套归一**，
   系统性误判两侧同时发生、互相抵消。负号丢失会同时算一次假数和一次漏数（它确实两处都错）。

8. **公式两侧同剥**（2026-08-19 起）：真值解析器把公式换成一个空格，所以 `norm_text` 也把产物里的
   `$…$` / `$$…$$` 剥掉——否则「正确输出了公式」反而被当成凭空多出的字符（维基公式页实测最多 +0.076）。
   判据**故意保守**，因为 `$` 也是货币符号：行内式要求定界符内含 `\`/`^`/`_` 且跨度 ≤60 字符，
   块级式还要求内部无 `$`、不跨空行。松一点就出事——早期写法把 OCR 误认的德语 `§§` 当成公式块，
   一份扫描件被吃掉 7306 个字符（char_sim 0.939→0.492）。全语料净影响：**aimorsel 18/626 份受影响、
   均值 +0.0004**；docling 0 份、其余引擎 ≤6 份且 |Δ| < 0.003（它们本来就不输出 `$` 公式）。

另有两处**语料本身的局限**，不是引擎表现：阿拉伯语只有文字层 PDF、**没有阿语图片/扫描件**
（所以 RTL 的 OCR 路径未被覆盖）；pptx/xlsx **只有合成语料**，没有真实样本。

**指标口径修订记录**（2026-08-18 抽检期间发现并已重算全部结果）：
- `norm_text` 的 HTML 标签正则原为 `<[^>]+>`，会把物理正文里 `q < qc … T > Tc` 之间整段吃掉
  （一份 arXiv 产物凭空少 39044 字）。已收紧为合法标签名。
- `cjk_inner_spaces` 原先先把换行折成空格，导致**每个段落/标题边界都被误计一次**，
  系统性偏向「输出结构更少」的引擎。已改为只数行内水平空白。
  **修正前据此得出的「我们比 pymupdf 多 8.5 倍」是错的，见 ISSUES-draft.md #1。**

"""


def truth_incomplete_for_digits(ids: set[str]) -> set[str]:
    """真值本身对「数字」就不完整的文档：arXiv 的公式被剥成空格、维基的信息框被丢掉。

    这两类在口径说明 2、4 里已经写明，对 char_sim 只是拉低分数，对 `数字准` 却是**结构性归零**
    （输出里照实存在的公式内数字、信息框数字，真值里根本没有）。数字保真专项必须把它们排除，
    否则榜首全是这类真值口径问题，真正的静默错值（财报扫描件）反而看不见。
    """
    out: set[str] = set()
    mf = BENCH / "corpus" / "manifest.jsonl"
    if not mf.exists():
        return out
    for line in mf.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        if d["id"] not in ids or not d.get("truth"):
            continue
        tp = BENCH / "corpus" / d["truth"]
        if not tp.exists():
            continue
        try:
            note = json.loads(tp.read_text(encoding="utf-8")).get("note") or {}
        except Exception:  # noqa: BLE001
            continue
        if isinstance(note, dict) and (note.get("math_stripped") or note.get("wikipedia_cleanup")):
            out.add(d["id"])
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(BENCH / "RESULTS.md"))
    a = ap.parse_args()
    rows = load()
    if not rows:
        print("results/ 为空")
        return 1
    engines = engines_sorted({r["engine"] for r in rows})
    docs = {r["id"] for r in rows}
    md = ["# 评测结果（自动生成，勿手改）", "",
          f"文档 {len(docs)} 份 × 引擎 {len(engines)} 个 = {len(rows)} 条记录。指标定义见 PLAN.md §3；",
          "「成功%」= (ok+degraded)/n；质量指标只在成功记录上取均值，且仅对带真值的文档计算。", ""]
    md += CAVEATS.split("\n")
    md += ["## 总表（各引擎全部记录；不支持的格式也计入 n）", "", table({e: [r for r in rows if r["engine"] == e] for e in engines}, "引擎"), ""]
    # 公平对比：所有引擎都成功的文档子集（否则「不支持图片」的引擎会因少算 0 分而显得更好）
    ok_ids = {}
    for e in engines:
        ok_ids[e] = {r["id"] for r in rows if r["engine"] == e and r.get("status") in ("ok", "degraded")}
    common = set.intersection(*ok_ids.values()) if ok_ids else set()
    md += [f"## 公平对比（{len(common)} 份所有引擎都成功的文档）", "",
           table({e: [r for r in rows if r["engine"] == e and r["id"] in common] for e in engines}, "引擎"), ""]
    # AImorsel vs 每个对手的两两共同集
    md += ["## AImorsel 两两对比（各自与 AImorsel 都成功的文档）", "", "| 对手 | 共同份数 | AImorsel 相似 | 对手相似 | AImorsel CER | 对手 CER | AImorsel 秒/文档 | 对手 秒/文档 |", "|---|---|---|---|---|---|---|---|"]
    for e in engines:
        if e == "aimorsel" or "aimorsel" not in ok_ids:
            continue
        both = ok_ids["aimorsel"] & ok_ids[e]
        A = summarize([r for r in rows if r["engine"] == "aimorsel" and r["id"] in both])
        B = summarize([r for r in rows if r["engine"] == e and r["id"] in both])
        md.append(f"| {e} | {len(both)} | {fmt(A['char_sim'])} | {fmt(B['char_sim'])} | {fmt(A['cer'])} | {fmt(B['cer'])} | {fmt(A['wall_med'])} | {fmt(B['wall_med'])} |")
    md.append("")
    for dim, title in (("format", "按格式"), ("lang", "按语言"), ("domain", "按领域"), ("truth_type", "按真值档")):
        md += [f"## {title}", ""]
        vals = sorted({str(r.get(dim)) for r in rows})
        for v in vals:
            sub = [r for r in rows if str(r.get(dim)) == v]
            md += [f"### {dim} = {v}（{len({r['id'] for r in sub})} 份）", "",
                   table({e: [r for r in sub if r["engine"] == e] for e in engines if any(r["engine"] == e for r in sub)}, "引擎"), ""]
    # 失败清单
    bad = [r for r in rows if r.get("status") in ("fail", "timeout", "missing")]
    md += ["## 失败 / 超时清单", "", f"共 {len(bad)} 条。", ""]
    if bad:
        md += ["| 引擎 | 文档 | 状态 | 说明 |", "|---|---|---|---|"]
        md += [f"| {r['engine']} | {r['id']} | {r['status']} | {str(r.get('note') or '')[:120].replace('|', '/')} |" for r in bad[:300]]
        if len(bad) > 300:
            md.append(f"\n（只列前 300 条，其余见 results/*.jsonl）")
    # 数字保真专项（issue #13）：真值里数字够多、且真值本身对数字完整的文档才有统计意义
    dense = {r["id"] for r in rows if (r.get("digit_n_truth") or 0) >= DIGIT_DENSE_MIN}
    dropped = truth_incomplete_for_digits(dense)
    dense -= dropped
    if dense:
        md += ["", f"## 数字保真专项（真值含 ≥{DIGIT_DENSE_MIN} 个数字串的 {len(dense)} 份文档）", "",
               f"（另有 {len(dropped)} 份数字密集文档被排除：真值本身对数字就不完整——arXiv 那批把公式剥成了空格、",
               "维基那批丢了信息框，输出里照实存在的数字在真值里找不到，`数字准` 会被结构性打到 0.03。见口径说明 2、4。）", ""] + [
               "财报扫描件的 char_sim 有 0.6+，但金额被静默改一位数、负号被吃掉——文本相似度对这类错值几乎不敏感。",
               "**`数字准` = 输出的数字里有多少真的在原文里；`1 - 数字准` 就是凭空造出来的错值占比。**",
               "多重集口径，不看位置；负号丢失会同时算一次假数和一次漏数（它确实是两处都错）。", "",
               "| 引擎 | 份数 | 数字F1↑ | 数字准↑ | 数字全↑ | 字符相似↑ |", "|---|---|---|---|---|---|"]
        for e in engines:
            sub = [r for r in rows if r["engine"] == e and r["id"] in dense and r.get("digit_f1") is not None]
            if not sub:
                continue
            md.append(f"| {e} | {len(sub)} | {fmt(mean([r['digit_f1'] for r in sub]))} | {fmt(mean([r['digit_precision'] for r in sub]))} "
                      f"| {fmt(mean([r['digit_recall'] for r in sub]))} | {fmt(mean([r.get('char_sim') for r in sub]))} |")
        # 各引擎的 n 不同（docling 只跑分层子集），跨引擎读数必须看共同子集——同口径说明 1
        have = {e: {r["id"] for r in rows if r["engine"] == e and r["id"] in dense and r.get("digit_f1") is not None}
                for e in engines}
        have = {e: v for e, v in have.items() if v}
        cd = set.intersection(*have.values()) if have else set()
        if cd:
            md += ["", f"上表各引擎的 n 不同（docling 只跑分层子集）。**跨引擎读数看这张共同子集表（{len(cd)} 份）**：", "",
                   "| 引擎 | 数字F1↑ | 数字准↑ | 数字全↑ | 字符相似↑ |", "|---|---|---|---|---|"]
            for e in engines:
                sub = [r for r in rows if r["engine"] == e and r["id"] in cd and r.get("digit_f1") is not None]
                if sub:
                    md.append(f"| {e} | {fmt(mean([r['digit_f1'] for r in sub]))} | {fmt(mean([r['digit_precision'] for r in sub]))} "
                              f"| {fmt(mean([r['digit_recall'] for r in sub]))} | {fmt(mean([r.get('char_sim') for r in sub]))} |")

        # 同一指标下「OCR 通道」与「有文字层」的对照——#13 的要害就在这条差距上
        ai = [r for r in rows if r["engine"] == "aimorsel" and r["id"] in dense and r.get("digit_f1") is not None]
        split = [("OCR 通道（图片 / 扫描件）", [r for r in ai if r.get("format") in OCR_FORMATS]),
                 ("有文字层（pdf / html / office）", [r for r in ai if r.get("format") not in OCR_FORMATS])]
        if all(sub for _, sub in split):
            md += ["", "AImorsel 按通道拆开（同一子集、同一指标）：", "",
                   "| 通道 | 份数 | 数字F1↑ | 数字准↑ | 数字全↑ | 字符相似↑ |", "|---|---|---|---|---|---|"]
            md += [f"| {name} | {len(sub)} | {fmt(mean([r['digit_f1'] for r in sub]))} | {fmt(mean([r['digit_precision'] for r in sub]))} "
                   f"| {fmt(mean([r['digit_recall'] for r in sub]))} | {fmt(mean([r.get('char_sim') for r in sub]))} |"
                   for name, sub in split]

        worst = sorted([r for r in rows if r["engine"] == "aimorsel" and r["id"] in dense
                        and isinstance(r.get("digit_f1"), float)], key=lambda r: r["digit_f1"])[:15]
        if worst:
            md += ["", "AImorsel 数字最不可信的 15 份：", "",
                   "**怎么读**：`数字准` 低而 `数字全` 接近 1、长度比 > 1.3 —— 是真值不完整（漏了参考文献之类），",
                   "输出里多出来的数字是真实存在的，不是错值。**`数字准`、`数字全` 双低才是真把数认错**",
                   "（错值同时制造一个假数和一个漏数），扫描件/图片那几行就是这种。", "",
                   "| 文档 | 格式 | 数字F1↑ | 数字准↑ | 数字全↑ | 长度比 | 字符相似↑ | 真值数字数 |", "|---|---|---|---|---|---|---|---|"]
            md += [f"| {r['id']} | {r.get('format')} | {r['digit_f1']:.3f} | {fmt(r.get('digit_precision'))} | {fmt(r.get('digit_recall'))} "
                   f"| {fmt(r.get('length_ratio'))} | {fmt(r.get('char_sim'))} | {r.get('digit_n_truth')} |" for r in worst]
        md.append("")

    # AImorsel 最差样本（回流用）
    ai = sorted([r for r in rows if r["engine"] == "aimorsel" and isinstance(r.get("char_sim"), float)], key=lambda r: r["char_sim"])[:30]
    if ai:
        md += ["", "## AImorsel 字符相似度最低的 30 份（回流排查用）", "", "| 文档 | 格式 | 语言 | 相似 | CER | 说明 |", "|---|---|---|---|---|---|"]
        md += [f"| {r['id']} | {r.get('format')} | {r.get('lang')} | {r['char_sim']:.3f} | {fmt(r.get('cer'))} | {str(r.get('note') or '')[:80]} |" for r in ai]
    charts = make_charts(rows, engines)
    if charts:
        md += ["", "## 图", ""] + [f"![{c.stem}]({c.relative_to(BENCH)})" for c in charts]
    Path(a.out).write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"→ {a.out}（{len(rows)} 条记录，{len(engines)} 引擎）")
    return 0


def make_charts(rows, engines) -> list[Path]:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return []
    # 图表标题是中文，DejaVu Sans 没有 CJK 字形，不指定就是一堆豆腐块。
    # 按 macOS → Windows → Linux 常见中文字体依次退，都没有时至少不报错（只是缺字）。
    from matplotlib.font_manager import findfont, FontProperties
    for cand in ("PingFang SC", "Hiragino Sans GB", "Heiti SC", "Microsoft YaHei",
                 "Noto Sans CJK SC", "Source Han Sans SC", "Arial Unicode MS"):
        try:
            if Path(findfont(FontProperties(family=cand), fallback_to_default=False)).exists():
                plt.rcParams["font.sans-serif"] = [cand, "DejaVu Sans"]
                plt.rcParams["axes.unicode_minus"] = False
                break
        except Exception:  # noqa: BLE001,PERF203
            continue
    out = BENCH / "results" / "charts"
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    specs = [("char_sim", "字符相似度（越高越好）"), ("cer", "CER（越低越好）"), ("heading_f1", "标题 F1"),
             ("cell_f1", "表格单元格 F1"), ("wall_med", "每文档耗时中位数(s)"), ("rss_p95", "峰值内存 p95 (MB)")]
    for key, title in specs:
        vals = [summarize([r for r in rows if r["engine"] == e]).get(key) for e in engines]
        if not any(isinstance(v, (int, float)) for v in vals):
            continue
        fig, ax = plt.subplots(figsize=(7, 3.2))
        ax.bar(engines, [v or 0 for v in vals], color=["#c0392b" if e == "aimorsel" else "#7f8c8d" for e in engines])
        ax.set_title(title)
        ax.tick_params(axis="x", rotation=20)
        fig.tight_layout()
        p = out / f"{key}.png"
        fig.savefig(p, dpi=120)
        plt.close(fig)
        paths.append(p)
    return paths


if __name__ == "__main__":
    raise SystemExit(main())
