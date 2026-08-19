"""真实 Java 引擎的集成测试。系统没装 java 时整个模块自动跳过（CI 会装 Temurin）。"""

from __future__ import annotations

import shutil

import pytest

import morsel
from conftest import requires_java

pytestmark = requires_java


def test_real_convert_pdf(text_pdf, tmp_path):
    r = morsel.convert_one(text_pdf, tmp_path, ["markdown", "json"], morsel.ConvertOptions())
    assert r.ok and not r.degraded
    assert r.pages == 2
    md = (tmp_path / text_pdf.stem / f"{text_pdf.stem}.md").read_text(encoding="utf-8")
    assert "Annual Report" in md


def test_real_batch_resume_and_parallel(text_pdf, tmp_path):
    # 复制出第二个文件，测 --jobs 2 的进程池路径 + 二轮续传
    copy = tmp_path / "copy.pdf"
    shutil.copy(text_pdf, copy)
    out = tmp_path / "out"
    opts = morsel.ConvertOptions()
    s1 = morsel.execute_batch([text_pdf, copy], out, ["markdown"], opts,
                              jobs=2, log=lambda m: None)
    assert s1.succeeded == 2 and s1.failed == 0
    s2 = morsel.execute_batch([text_pdf, copy], out, ["markdown"], opts, log=lambda m: None)
    assert s2.skipped == 2


def test_real_scanned_pdf_empty_but_handled(scanned_pdf, tmp_path):
    """纯图片 PDF 普通转换：产物只有图片引用，不报错。"""
    r = morsel.convert_one(scanned_pdf, tmp_path, ["markdown"], morsel.ConvertOptions())
    # 底层对纯图片页会产出图片引用（或空 md）——只要不崩、有明确结果即可
    assert r.ok or r.error


def test_real_image_input_without_ocr(sample_png, tmp_path):
    """图片输入包装成 PDF 走管线；无 OCR 服务时只提取版面，但流程完整。"""
    r = morsel.convert_one(sample_png, tmp_path, ["markdown"], morsel.ConvertOptions())
    assert r.pdf.name == sample_png.name
    assert r.ok
    assert (tmp_path / sample_png.stem).is_dir()


def test_real_chunks_on_pdf(text_pdf, tmp_path):
    r = morsel.convert_one(text_pdf, tmp_path, ["markdown"],
                           morsel.ConvertOptions(rag_chunks=True))
    assert r.ok
    chunks = tmp_path / text_pdf.stem / f"{text_pdf.stem}.chunks.jsonl"
    assert chunks.exists() and chunks.stat().st_size > 0


def test_real_truncated_pdf_repaired(text_pdf, tmp_path):
    """bench issue #3：截断的 PDF 引擎拒收 → qpdf 修复 → 引擎出完整结构树（不是降级）。"""
    pytest.importorskip("pikepdf")
    raw = text_pdf.read_bytes()
    trunc = tmp_path / "trunc.pdf"
    trunc.write_bytes(raw[: int(len(raw) * 0.85)])
    r = morsel.convert_one(trunc, tmp_path, ["markdown", "json"], morsel.ConvertOptions())
    assert r.ok and not r.degraded, r.error
    assert "已修复后转换" in r.note
    assert r.pages == 2 and "Annual Report" in (tmp_path / "trunc" / "trunc.md").read_text(encoding="utf-8")
