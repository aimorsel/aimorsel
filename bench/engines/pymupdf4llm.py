"""pymupdf4llm（AGPL，只作对比不分发）。"""
from __future__ import annotations

from pathlib import Path

from bench.engines._base import main, unsupported


def run(path: Path, out_dir: Path) -> dict:
    if (u := unsupported(path, {".pdf", ".xps", ".epub"})):
        return u
    import pymupdf4llm
    import pymupdf

    text = pymupdf4llm.to_markdown(str(path), show_progress=False)
    with pymupdf.open(str(path)) as doc:
        n = doc.page_count
    md = out_dir / (path.stem + ".md")
    md.write_text(text, encoding="utf-8")
    return {"md_path": str(md), "json_path": None, "pages": n}


if __name__ == "__main__":
    main(run)
