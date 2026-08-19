"""docling（IBM）：质量上限参照，慢。用独立环境的解释器（见 bench/engines.toml）。"""
from __future__ import annotations

from pathlib import Path

from bench.engines._base import main, unsupported

# run.py 通过环境变量 BENCH_DOC_LANG 传文档语言；EasyOCR 语言组合有限制，一律 [<lang>, en]
_EASYOCR = {"zh": "ch_sim", "zh-tw": "ch_tra", "ja": "ja", "ko": "ko", "ar": "ar", "ru": "ru",
            "es": "es", "de": "de", "fr": "fr", "it": "it", "pt": "pt", "en": "en"}

EXTS = {".pdf", ".docx", ".xlsx", ".pptx", ".html", ".htm", ".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".webp"}
_CONVERTER = None


def run(path: Path, out_dir: Path) -> dict:
    global _CONVERTER
    if (u := unsupported(path, EXTS)):
        return u
    from docling.document_converter import DocumentConverter

    if _CONVERTER is None:
        import os
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import EasyOcrOptions, PdfPipelineOptions
        from docling.document_converter import ImageFormatOption, PdfFormatOption

        lang = os.environ.get("BENCH_DOC_LANG", "en").lower()
        codes = sorted({_EASYOCR.get(lang, "en"), "en"})
        po = PdfPipelineOptions(do_ocr=True, ocr_options=EasyOcrOptions(lang=codes))
        _CONVERTER = DocumentConverter(format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=po),
            InputFormat.IMAGE: ImageFormatOption(pipeline_options=po),
        })
    result = _CONVERTER.convert(str(path))
    doc = result.document
    md = out_dir / (path.stem + ".md")
    md.write_text(doc.export_to_markdown(), encoding="utf-8")
    js = out_dir / (path.stem + ".json")
    try:
        import json
        js.write_text(json.dumps(doc.export_to_dict(), ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        js = None
    pages = getattr(doc, "num_pages", None)
    pages = pages() if callable(pages) else pages
    return {"md_path": str(md), "json_path": str(js) if js else None, "pages": pages}


if __name__ == "__main__":
    main(run)
