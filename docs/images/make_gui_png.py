"""起 GUI、摆好一批演示状态、截窗口。

不用 MORSEL_GUI_SMOKE（那个 800ms 就自动关），改成自己排一个定时器：
先把文件列表和选项摆成「正要转换」的样子，再截图，最后退出。

先造演示文件（列表会显示真实路径，所以放 /tmp 下，别让个人目录进 README）：
    mkdir -p /tmp/aimorsel-demo/文档 /tmp/aimorsel-demo/docs
    # 往里各放几个 pdf / 图片 / pptx / xlsx，中文目录放中文名、docs 放英文名

然后：
    python docs/images/make_gui_png.py docs/images/gui.png
    MORSEL_LANG=en python docs/images/make_gui_png.py docs/images/gui-en.png

需要 macOS 的屏幕录制权限（screencapture 按窗口 id 截图）。
"""
import os, subprocess, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # 仓库根
os.environ.setdefault("MORSEL_LANG", "zh")

from aimorsel import morsel_gui

OUT = sys.argv[1] if len(sys.argv) > 1 else "gui.png"

try:
    from tkinterdnd2 import TkinterDnD
    root = TkinterDnD.Tk()
except Exception:
    import tkinter as tk
    root = tk.Tk()

app = morsel_gui.ConverterApp(root)

# 摆出演示状态：几个真实存在的待转文件（列表会显示真实路径，所以用 /tmp 下的演示目录，
# 别用假路径——那会在 README 首屏留下一个看着像个人目录的字符串）
EN = os.environ["MORSEL_LANG"] == "en"
# 英文版用英文文件名：中文文件名放在英文 README 首屏会显得突兀（虽然确实支持）
demo = Path("/tmp/aimorsel-demo/docs" if EN else "/tmp/aimorsel-demo/文档")
app.pdfs.extend(sorted(demo.iterdir()))
app._refresh_list()
for attr in ("rag_var", "tables_var", "qa_var"):
    getattr(app, attr).set(True)
# 输出目录默认指向仓库里的 output/，截图会把本机绝对路径印进 README——换成演示路径
app.output_dir = Path("/tmp/aimorsel-demo/output")
app.output_label.config(text=str(app.output_dir))
if EN:
    app._log("Added 4 files (PDF / image / PPT / Excel)")
    app._log("Enabled: RAG chunks, table export to CSV, quality check")
else:
    app._log("已添加 4 个文件（PDF / 图片 / PPT / Excel）")
    app._log("已勾选：RAG 分块、表格导出 CSV、质量自检")

# 默认窗口高度放不下 RAG / 表格导出 / 质量自检那两行，截图里会缺掉最能说明
# 「面向 AI 场景」的几个选项。别猜一个高度——按内容实际所需高度来，
# 否则总会差那么半行（试过 1180，最后一行照样被切）。
# 装不下的根源是屏幕：Retina 下窗口最高只有约 916 逻辑像素，给再大的 geometry 也没用
# （试过 1180 / 1400 / reqheight，截出来都是同一个尺寸）。所以反过来收窄两个可伸缩区，
# 让「RAG 分块 / 表格导出 / 质量自检」那两行进得来——这几个选项是 GUI 最该被看见的部分。
# 只在截图脚本里临时收，不动产品代码。
app.listbox.config(height=4)
app.log_text.config(height=4)
root.update_idletasks()
# 英文控件更宽（760 下 "OCR service: online" 会被截成 "onlir"）
root.geometry(f"{860 if EN else 760}x{min(root.winfo_reqheight(), 900)}")

root.update_idletasks()
root.update()
root.lift()
root.attributes("-topmost", True)
root.update()


def capture():
    time.sleep(0.6)
    wid = root.winfo_id()
    # -l 按窗口 id 截，只要窗口本身、不带桌面背景
    r = subprocess.run(["screencapture", "-x", "-o", "-l", str(wid), OUT],
                       capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(OUT):
        subprocess.run(["screencapture", "-x", "-R",
                        f"{root.winfo_rootx()},{root.winfo_rooty()},"
                        f"{root.winfo_width()},{root.winfo_height()}", OUT])
    root.quit()


root.after(900, capture)
root.mainloop()
try:
    root.destroy()
except Exception:
    pass
print("窗口尺寸:", root.winfo_width(), "x", root.winfo_height() if root else "?")
