"""录 Web 服务的「上传 → 自动转换 → 结果出现」流程，输出帧序列到 frames/。

不是屏幕录制：Playwright 定时截图，再拼成 GIF。帧稳定、没有鼠标乱晃，
界面改了重跑一次就行。

先起一个**干净**的服务（状态别复用：日志和「最近转换」会累积上一轮的记录，
截出来像是转了两次）：
    rm -rf /tmp/aimorsel-demo && mkdir -p /tmp/aimorsel-demo/{raw,output}
    morsel web --port 8220 --input /tmp/aimorsel-demo/raw -o /tmp/aimorsel-demo/output --interval 2

    python docs/images/make_web_demo.py 演示文档.pdf

再把 frames/ 拼成 GIF（PIL 会自动合并重复帧，界面真实变化只有十几个画面）：
    imgs = [Image.open(f).convert("RGB").quantize(colors=128) for f in sorted(...)]
    imgs[0].save("web-demo.gif", save_all=True, append_images=imgs[1:],
                 duration=[...], loop=0, optimize=True, disposal=2)
"""
import os
import sys
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

OUT = Path("frames"); OUT.mkdir(exist_ok=True)
PDF = sys.argv[1] if len(sys.argv) > 1 else "demo.pdf"   # 要上传的演示文档
# 英文版（服务以 MORSEL_LANG=en 起）：MORSEL_LANG=en python make_web_demo.py quarterly-report.pdf
OK = "OK" if os.environ.get("MORSEL_LANG") == "en" else "成功"   # 状态列的「成功」字样
MD = Path(PDF).stem + ".md"                                       # 等它出现在输出文件列表
frames = []


def shot(page, tag):
    p = OUT / f"{len(frames):03d}_{tag}.png"
    page.screenshot(path=str(p))
    frames.append(p)


with sync_playwright() as pw:
    b = pw.chromium.launch()
    pg = b.new_page(viewport={"width": 900, "height": 660}, device_scale_factor=1)
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://127.0.0.1:8220/", wait_until="networkidle")
    pg.wait_for_timeout(600)

    for _ in range(4):                       # 起手：空空的界面
        shot(pg, "idle"); pg.wait_for_timeout(250)

    pg.set_input_files("input[type=file]", PDF)   # 选文件
    pg.wait_for_timeout(300)
    for _ in range(3):
        shot(pg, "picked"); pg.wait_for_timeout(250)

    pg.click("button[type=submit], input[type=submit], .btn")  # 点上传
    for _ in range(30):                      # 盯着它自己转完
        shot(pg, "run"); pg.wait_for_timeout(400)
        if pg.locator(f"text={OK}").count() and pg.locator(f"text={MD}").count():
            break

    for _ in range(4):                       # 结果先停一下
        shot(pg, "done"); pg.wait_for_timeout(300)

    # 往下滚，把「实时日志」和「输出文件」也走一遍——静态页面里这是唯一真实的动态，
    # 不加的话整段只有 6 个不同画面，看着像幻灯片
    # 每步都重新量高度：日志区还在增长，一次算死会滚不到底
    for i in range(1, 13):
        pg.evaluate(
            "([i, n]) => window.scrollTo(0,"
            " (document.body.scrollHeight - window.innerHeight) * i / n)", [i, 12])
        pg.wait_for_timeout(120)
        shot(pg, "scroll")
    for _ in range(4):                       # 停在输出文件列表
        shot(pg, "tail"); pg.wait_for_timeout(300)

    print("帧数:", len(frames), "| pageerrors:", errs)
    b.close()
