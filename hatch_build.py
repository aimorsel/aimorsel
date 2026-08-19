"""hatchling 元数据钩子：给 PyPI 页面用的 README。

README.en.md 里的图片与链接写的是仓库相对路径（GitHub 上这样最省事），
但 PyPI 渲染时没有仓库上下文，相对路径全部变成裂图 / 死链。
这里在构建 wheel / sdist 时把它们改写成钉在当前版本 tag 上的 GitHub 绝对地址：
图片走 raw.githubusercontent.com（PyPI 会经 camo 代理转发），其余链接走 github.com/blob。
仓库里的 README 文件本身不改。
"""
from __future__ import annotations

import re
from pathlib import Path

from hatchling.metadata.plugin.interface import MetadataHookInterface

REPO = "aimorsel/aimorsel"
_REL = r"(?!(?:[a-z][a-z0-9+.-]*:|#|/))([^)\s]+?)(?:#([^)\s]*))?(\s+\"[^\"]*\")?\)"   # 相对路径：排除 scheme:/锚点/绝对路径
_IMG = re.compile(r"!\[([^\]]*)\]\(" + _REL)      # 图片：![alt](path)
_ANY = re.compile(r"\]\(" + _REL)                    # 其余链接：...](path)，含外层套着徽章的 [![..](..)](LICENSE)


def absolutize(text: str, version: str) -> str:
    raw = f"https://raw.githubusercontent.com/{REPO}/v{version}/"
    blob = f"https://github.com/{REPO}/blob/v{version}/"

    def tail(m: re.Match, start: int) -> str:
        path, frag, title = m.group(start), m.group(start + 1), m.group(start + 2) or ""
        return f"{path}{'#' + frag if frag else ''}{title})"

    text = _IMG.sub(lambda m: f"![{m.group(1)}]({raw}{tail(m, 2)}", text)
    return _ANY.sub(lambda m: f"]({blob}{tail(m, 1)}", text)


class CustomMetadataHook(MetadataHookInterface):
    def update(self, metadata: dict) -> None:
        root = Path(self.root)
        version = re.search(r'__version__\s*=\s*"([^"]+)"', (root / "aimorsel" / "__init__.py").read_text("utf-8")).group(1)
        text = (root / "README.en.md").read_text("utf-8")
        metadata["readme"] = {"content-type": "text/markdown", "text": absolutize(text, version)}
