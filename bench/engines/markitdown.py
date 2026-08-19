"""markitdown（微软）：无版面分析，支持 pdf/docx/xlsx/pptx/html/图片(需 LLM，这里不配)。"""
from __future__ import annotations

from pathlib import Path

from bench.engines._base import main, unsupported

EXTS = {".pdf", ".docx", ".xlsx", ".pptx", ".html", ".htm", ".csv", ".json", ".xml", ".txt"}


def run(path: Path, out_dir: Path) -> dict:
    if (u := unsupported(path, EXTS)):
        return u
    from markitdown import MarkItDown

    result = MarkItDown().convert(str(path))
    md = out_dir / (path.stem + ".md")
    md.write_text(result.text_content or "", encoding="utf-8")
    return {"md_path": str(md), "json_path": None, "pages": None}


if __name__ == "__main__":
    main(run)
