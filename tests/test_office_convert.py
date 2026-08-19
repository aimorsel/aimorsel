"""office 文档走完整转换管线（含下游加工与批量），全程不需要 Java。"""

from __future__ import annotations

import csv
import json

from aimorsel import morsel


def test_docx_full_pipeline(sample_docx, tmp_path):
    r = morsel.convert_one(
        sample_docx, tmp_path, ["markdown"],
        morsel.ConvertOptions(rag_chunks=True, export_tables=True, qa=True))
    assert r.ok and r.pages == 1 and not r.degraded
    dest = tmp_path / sample_docx.stem
    md = (dest / f"{sample_docx.stem}.md").read_text(encoding="utf-8")
    assert "# 年度报告" in md and "| 项目 | 金额 |" in md

    chunks = [json.loads(line) for line in
              (dest / f"{sample_docx.stem}.chunks.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any("年度报告" in c["heading_path"] for c in chunks)

    tables = sorted((dest / f"{sample_docx.stem}_tables").glob("*.csv"))
    assert len(tables) == 1
    rows = list(csv.reader(tables[0].open(encoding="utf-8-sig")))
    assert rows[1] == ["收入", "100"]

    assert (dest / f"{sample_docx.stem}.qa.csv").exists()


def test_xlsx_pages_filter(sample_xlsx, tmp_path):
    r = morsel.convert_one(sample_xlsx, tmp_path, ["markdown", "json"],
                           morsel.ConvertOptions())
    assert r.ok and r.pages == 2
    r2 = morsel.convert_one(sample_xlsx, tmp_path / "p1", ["markdown"],
                            morsel.ConvertOptions(pages="1"))
    assert r2.ok
    md = (tmp_path / "p1" / sample_xlsx.stem / f"{sample_xlsx.stem}.md").read_text(encoding="utf-8")
    assert "销售" in md and "成本" not in md


def test_pptx_page_markers(sample_pptx, tmp_path):
    r = morsel.convert_one(sample_pptx, tmp_path, ["markdown"],
                           morsel.ConvertOptions(page_markers=True))
    assert r.ok and r.pages == 2
    md = (tmp_path / sample_pptx.stem / f"{sample_pptx.stem}.md").read_text(encoding="utf-8")
    assert "第 2 页" in md


def test_decide_ocr_routing(sample_docx, sample_png):
    opts = morsel.ConvertOptions()
    assert morsel.decide_ocr(sample_docx, opts, server_ok=True) == (False, "")
    use_ocr, note = morsel.decide_ocr(sample_png, opts, server_ok=True)
    assert use_ocr and "图片" in note
    use_ocr, note = morsel.decide_ocr(sample_png, opts, server_ok=False)
    assert not use_ocr and "OCR 服务未启动" in note
    assert morsel.decide_ocr(sample_png, morsel.ConvertOptions(ocr_mode="off"),
                             server_ok=True) == (False, "")


def test_batch_resume_and_report(sample_docx, sample_xlsx, tmp_path):
    batch = [sample_docx, sample_xlsx]
    opts = morsel.ConvertOptions()
    s1 = morsel.execute_batch(batch, tmp_path, ["markdown", "json"], opts, log=lambda m: None)
    assert s1.succeeded == 2 and s1.failed == 0
    report = (tmp_path / "report.csv").read_text(encoding="utf-8-sig")
    assert report.count("成功") == 2

    s2 = morsel.execute_batch(batch, tmp_path, ["markdown", "json"], opts, log=lambda m: None)
    assert s2.skipped == 2

    # 源文件变化 -> 只重转变化的那个
    sample_docx.touch()
    s3 = morsel.execute_batch(batch, tmp_path, ["markdown", "json"], opts, log=lambda m: None)
    assert s3.skipped == 1 and s3.succeeded == 1

    # 选项变化 -> 签名失效全部重转
    s4 = morsel.execute_batch(batch, tmp_path, ["markdown"], opts, log=lambda m: None)
    assert s4.succeeded == 2


def test_batch_merge(sample_docx, sample_pptx, tmp_path):
    s = morsel.execute_batch([sample_docx, sample_pptx], tmp_path, ["markdown"],
                             morsel.ConvertOptions(), log=lambda m: None, merge=True)
    assert s.merged_path is not None
    merged = s.merged_path.read_text(encoding="utf-8")
    assert sample_docx.stem in merged and "年度报告" in merged and "项目启动" in merged


def test_bad_file_fails_without_breaking_batch(sample_docx, tmp_path):
    bad = tmp_path / "bad.docx"
    bad.write_bytes(b"broken")
    s = morsel.execute_batch([bad, sample_docx], tmp_path / "out", ["markdown"],
                             morsel.ConvertOptions(), log=lambda m: None)
    assert s.failed == 1 and s.succeeded == 1
    # 失败文件不进断点续传清单
    entries = morsel.load_manifest(tmp_path / "out")
    assert str(bad.resolve()) not in entries


def test_html_full_pipeline(tmp_path):
    src = tmp_path / "page.html"
    src.write_text("<html><head><title>T</title></head><body><h1>标题</h1><p>正文一。</p>"
                   "<table><tr><td>a</td><td>b</td></tr><tr><td>1</td><td>2</td></tr></table>"
                   "<ul><li>甲</li><li>乙</li></ul></body></html>", encoding="utf-8")
    out = tmp_path / "out"
    r = morsel.convert_one(src, out, ["markdown", "json", "text", "html"],
                           morsel.ConvertOptions(rag_chunks=True, export_tables=True))
    assert r.ok and not r.error, r.error
    md = (out / "page" / "page.md").read_text(encoding="utf-8")
    assert "# 标题" in md and "| a | b |" in md and "- 甲" in md
    tree = json.loads((out / "page" / "page.json").read_text(encoding="utf-8"))
    assert [k["type"] for k in tree["kids"]] == ["heading", "paragraph", "table", "list"]
    assert (out / "page" / "page.chunks.jsonl").exists()
    csvs = list((out / "page" / "page_tables").glob("*.csv"))
    assert len(csvs) == 1
    with csvs[0].open(encoding="utf-8-sig", newline="") as fh:
        assert list(csv.reader(fh)) == [["a", "b"], ["1", "2"]]


def test_html_compat_ligature_normalized(tmp_path):
    """bench #2：HTML 用数字实体写的连字（&#64257; = ﬁ）解码后必须被归一，产物 compat_residual 为 0，
    且分块吃的是修正后的 JSON。"""
    src = tmp_path / "lig.html"
    src.write_text("<html><body><h1>o&#64259;ce &#64257;le</h1><p>e&#64259;cient &#64258;ow 一</p></body></html>",
                   encoding="utf-8")
    out = tmp_path / "out"
    r = morsel.convert_one(src, out, ["markdown", "json", "text", "html"], morsel.ConvertOptions(rag_chunks=True))
    assert r.ok and not r.error, r.error
    for suffix in ("md", "json", "txt", "html", "chunks.jsonl"):
        text = (out / "lig" / f"lig.{suffix}").read_text(encoding="utf-8")
        assert "ﬀ" not in text and "ﬁ" not in text and "ﬂ" not in text and "ﬃ" not in text, suffix
        assert "office file" in text and "efficient flow" in text, suffix
    assert "修正 16 处兼容码位" in r.note  # 4 处 × 4 种文本产物（计数口径与 PDF 路径一致）
