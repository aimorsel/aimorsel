"""hatch_build.absolutize：PyPI 页面用的 README 里相对链接改写成 GitHub 绝对地址。"""
import sys
from pathlib import Path

import pytest

pytest.importorskip("hatchling")  # 构建依赖，不在运行时 extras 里；没装就跳过
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from hatch_build import absolutize  # noqa: E402

BLOB = "https://github.com/aimorsel/aimorsel/blob/v1.2.3/"
RAW = "https://raw.githubusercontent.com/aimorsel/aimorsel/v1.2.3/"


def test_images_go_to_raw_and_links_to_blob():
    out = absolutize("![Web UI](docs/images/web-ui-en.png) and [log](CHANGELOG.md#x)", "1.2.3")
    assert f"![Web UI]({RAW}docs/images/web-ui-en.png)" in out
    assert f"[log]({BLOB}CHANGELOG.md#x)" in out


def test_badge_wrapped_in_relative_link():
    out = absolutize("[![L](https://img.shields.io/x.svg)](LICENSE)", "1.2.3")
    assert out == f"[![L](https://img.shields.io/x.svg)]({BLOB}LICENSE)"


def test_absolute_anchor_and_title_untouched():
    src = '[a](https://x.y/z) [b](#install) [c](mailto:x@y.z) [d](/abs/path)'
    assert absolutize(src, "1.2.3") == src
    assert absolutize('[t](docs/a.md "Title")', "1.2.3") == f'[t]({BLOB}docs/a.md "Title")'
