"""录 Web 服务的「上传 → 自动转换 → 结果出现」流程，输出帧序列到 frames/。

不是屏幕录制：Playwright 定时截图，再拼成 GIF。帧稳定、没有鼠标乱晃，
界面改了重跑一次就行。鼠标光标与点击波纹是 PIL 画上去的（按钮坐标来自
Playwright），让新用户看清该点哪里；原生文件对话框截不到，只示意到点击为止。

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
from PIL import Image, ImageDraw
from playwright.sync_api import sync_playwright

OUT = Path("frames"); OUT.mkdir(exist_ok=True)
PDF = sys.argv[1] if len(sys.argv) > 1 else "demo.pdf"   # 要上传的演示文档
# 英文版（服务以 MORSEL_LANG=en 起）：MORSEL_LANG=en python make_web_demo.py quarterly-report.pdf
OK = "OK" if os.environ.get("MORSEL_LANG") == "en" else "成功"   # 状态列的「成功」字样
MD = Path(PDF).stem + ".md"                                       # 等它出现在输出文件列表
frames = []


# 光标是画上去的（Playwright 截图不含鼠标）：告诉新用户该点哪里。
# 原生文件对话框截不到，「选文件」这步只能靠光标点过去 + 文件名出现来示意。
cursor = [640, 120]          # 当前光标位置（视口坐标），起手停在空白处
CLICK_RING = [0]             # >0 时在光标旁画点击波纹，每帧递减


def draw_cursor(path):
    im = Image.open(path).convert("RGBA")
    d = ImageDraw.Draw(im)
    x, y = cursor
    if CLICK_RING[0]:
        r = 10 + 6 * (3 - CLICK_RING[0])           # 波纹向外扩
        d.ellipse([x - r, y - r, x + r, y + r], outline="white", width=6)   # 白边垫底，蓝按钮上也看得见
        d.ellipse([x - r, y - r, x + r, y + r], outline=(31, 95, 208), width=3)
        CLICK_RING[0] -= 1
    arrow = [(x, y), (x, y + 17), (x + 4, y + 13), (x + 7, y + 20),
             (x + 10, y + 19), (x + 7, y + 12), (x + 12, y + 12)]
    d.polygon(arrow, fill="white", outline="black")
    im.convert("RGB").save(path)


def shot(page, tag):
    p = OUT / f"{len(frames):03d}_{tag}.png"
    page.screenshot(path=str(p))
    draw_cursor(p)
    frames.append(p)


def glide(page, tag, to, steps=6, pause=70):
    """光标从当前位置匀速滑到 to，每步截一帧。"""
    x0, y0 = cursor
    for i in range(1, steps + 1):
        cursor[0] = round(x0 + (to[0] - x0) * i / steps)
        cursor[1] = round(y0 + (to[1] - y0) * i / steps)
        shot(page, tag); page.wait_for_timeout(pause)


def center(page, selector, dx=None):
    box = page.locator(selector).first.bounding_box()
    x = box["x"] + (dx if dx is not None else box["width"] / 2)
    return [round(x), round(box["y"] + box["height"] / 2)]


with sync_playwright() as pw:
    b = pw.chromium.launch()
    pg = b.new_page(viewport={"width": 900, "height": 660}, device_scale_factor=1)
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://127.0.0.1:8220/", wait_until="networkidle")
    pg.wait_for_timeout(600)

    for _ in range(4):                       # 起手：空空的界面
        shot(pg, "idle"); pg.wait_for_timeout(250)

    glide(pg, "move", center(pg, "input[type=file]", dx=45))  # 滑到「选择文件」按钮
    CLICK_RING[0] = 3
    for _ in range(2):
        shot(pg, "click"); pg.wait_for_timeout(120)
    pg.set_input_files("input[type=file]", PDF)   # 选文件（对话框截不到，文件名直接出现）
    pg.wait_for_timeout(300)
    for _ in range(3):
        shot(pg, "picked"); pg.wait_for_timeout(250)

    UPLOAD = "button[type=submit], input[type=submit], .btn"
    glide(pg, "move", center(pg, UPLOAD))         # 滑到「上传」
    CLICK_RING[0] = 3
    for _ in range(2):
        shot(pg, "click"); pg.wait_for_timeout(120)
    pg.click(UPLOAD)                              # 点上传
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
    try:                                     # 最后悬停到 .md 下载链接，示意「点这里拿结果」
        glide(pg, "move", center(pg, f"a:has-text('{MD}')"))
    except Exception:
        pass
    for _ in range(4):                       # 停在输出文件列表
        shot(pg, "tail"); pg.wait_for_timeout(300)

    print("帧数:", len(frames), "| pageerrors:", errs)
    b.close()
