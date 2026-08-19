"""AImorsel · 文粒 —— 本地运行的文档结构化提取工具（PDF/Office/HTML/图片 → Markdown/JSON）。

子模块：
    morsel            命令行入口与核心转换逻辑（ConvertOptions / convert_one / execute_batch）
    morsel_gui        桌面图形界面（tkinter）
    morsel_web        Web 常驻服务（纯标准库 http.server）
    morsel_mcp        MCP Server（stdio 协议）
    format_adapters   docx/xlsx/pptx/HTML/图片 输入适配器
    rtl_text          RTL 视觉序 → 逻辑序还原
    i18n              界面文案中英切换
    ocr_setup         OCR 服务一键安装/启动
"""

__version__ = "1.0.1"
