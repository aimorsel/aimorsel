#!/usr/bin/env python3
"""把一次真实的 CLI 运行输出渲染成 README 首屏那张终端截图。

图是渲染出来的、不是屏幕截图——所以品牌或文案一变就能重跑这个脚本，不必再手工摆一次。
输入是 `morsel … 2>&1` 的真实输出（别手写，图要和实际行为一致）。

用法：
    morsel 报告.pdf report.pdf -f markdown,json --rag-chunks > out.txt 2>&1
    python docs/images/make_cli_png.py out.txt docs/images/cli.png --title "AImorsel — 批量转换"
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

MONO = "/System/Library/Fonts/Menlo.ttc"
CJK = "/System/Library/Fonts/PingFang.ttc"
BG, BAR, FG, DIM = "#1E2127", "#282C34", "#E6E6E6", "#8A9199"
BLUE, GREEN, YELLOW = "#61AFEF", "#98C379", "#E5C07B"
SCALE = 2                      # 2x 出图，README 里按一半宽度显示，视网膜屏不糊

# 上色规则：路径/产物蓝、成功绿、提示黄。只认这几类，别把整行涂花
PATTERNS = [
    (re.compile(r"(output/[^\s，。]*|http://[^\s]+)"), BLUE),
    (re.compile(r"(✓|成功 \d+ 个)"), GREEN),
    (re.compile(r"(RAG 分块（[^）]*）|已启用：)"), YELLOW),
]


def load(size: int) -> tuple[ImageFont.FreeTypeFont, ImageFont.FreeTypeFont]:
    return ImageFont.truetype(MONO, size), ImageFont.truetype(CJK, size, index=1)


def is_cjk(ch: str) -> bool:
    return ch >= "⺀"


def draw_line(d: ImageDraw.ImageDraw, xy, text, mono, cjk, fill):
    """逐字符排版：中文走 PingFang、其余走 Menlo，各按自己的实际宽度前进。

    早先按「汉字 = 两个等宽字符」推进，结果 PingFang 的汉字是正方形（宽 = 字号）、
    Menlo 两个字符是 1.2 倍字号，每个汉字后面都多出一道缝，中文整段看着散。
    """
    x, y = xy
    for ch in text:
        f = cjk if is_cjk(ch) else mono
        d.text((x, y), ch, font=f, fill=fill)
        x += f.getlength(ch)
    return x


def colorize(line: str) -> list[tuple[str, str]]:
    spans = [(line, FG)]
    for pat, color in PATTERNS:
        out: list[tuple[str, str]] = []
        for text, c in spans:
            if c != FG:
                out.append((text, c))
                continue
            last = 0
            for m in pat.finditer(text):
                if m.start() > last:
                    out.append((text[last:m.start()], FG))
                out.append((m.group(0), color))
                last = m.end()
            if last < len(text):
                out.append((text[last:], FG))
        spans = out
    return spans


def render(lines: list[str], title: str, out: Path, font_px: int = 15) -> None:
    size = font_px * SCALE
    mono, cjk = load(size)
    adv = mono.getlength("M")
    line_h = int(size * 1.75)
    pad = int(26 * SCALE)
    bar_h = int(38 * SCALE)

    def line_w(text: str) -> float:
        return sum((cjk if is_cjk(c) else mono).getlength(c) for c in text)

    width = int(max(line_w(l) for l in lines) + pad * 2)
    height = bar_h + pad + line_h * len(lines) + pad
    img = Image.new("RGB", (width, height), BG)
    d = ImageDraw.Draw(img)

    d.rectangle([0, 0, width, bar_h], fill=BAR)
    r = int(6 * SCALE)
    for i, c in enumerate(("#FF5F57", "#FEBC2E", "#28C840")):
        cx = pad + i * int(20 * SCALE)
        d.ellipse([cx - r, bar_h // 2 - r, cx + r, bar_h // 2 + r], fill=c)
    tf = ImageFont.truetype(CJK, int(size * 0.85), index=1)
    d.text((pad + int(88 * SCALE), bar_h // 2), title, font=tf, fill=DIM, anchor="lm")

    y = bar_h + pad
    for line in lines:
        x = pad
        for text, color in colorize(line):
            x = draw_line(d, (x, y), text, mono, cjk, color)
        y += line_h

    img.save(out)
    print(f"{out}  {img.width}x{img.height}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("input", type=Path, help="morsel 的真实输出（文本文件）")
    ap.add_argument("output", type=Path)
    ap.add_argument("--title", default="AImorsel — 批量转换")
    ap.add_argument("--command", default="", help="显示在最上方的命令行（不含 $）")
    a = ap.parse_args()

    lines = a.input.read_text(encoding="utf-8").rstrip("\n").split("\n")
    if a.command:
        lines = [f"$ {a.command}", ""] + lines
    render(lines, a.title, a.output)


if __name__ == "__main__":
    main()
