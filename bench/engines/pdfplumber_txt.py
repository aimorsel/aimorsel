"""下限参照：pdfplumber 逐页纯文本（相当于 AImorsel 的兜底档）。"""
from __future__ import annotations

from pathlib import Path

from bench.engines._base import main, unsupported


def run(path: Path, out_dir: Path) -> dict:
    if (u := unsupported(path, {".pdf"})):
        return u
    import pdfplumber

    parts = []
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            parts.append(page.extract_text() or "")
        n = len(pdf.pages)
    md = out_dir / (path.stem + ".md")
    md.write_text("\n\n".join(parts), encoding="utf-8")
    return {"md_path": str(md), "json_path": None, "pages": n}


if __name__ == "__main__":
    main(run)
