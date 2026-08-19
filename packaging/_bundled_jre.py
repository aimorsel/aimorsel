"""打包版共用逻辑：把随包捆绑的精简 JRE 插到 PATH 最前，摆脱对系统 Java 的依赖。"""

import os
import sys
from pathlib import Path


def use_bundled_jre() -> None:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    jre_bin = base / "jre" / "bin"
    if jre_bin.is_dir():
        os.environ["PATH"] = str(jre_bin) + os.pathsep + os.environ.get("PATH", "")
        os.environ["JAVA_HOME"] = str(jre_bin.parent)
    if os.environ.get("MORSEL_DEBUG_JAVA"):
        import shutil
        print(f"[debug] java = {shutil.which('java')}", flush=True)
