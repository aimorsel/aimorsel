"""pdfplumber 兜底网 + 密度探测：monkeypatch 掉 Java 引擎，不需要真实 Java。"""

from __future__ import annotations

import pytest

from aimorsel import morsel


@pytest.fixture()
def java_boom(monkeypatch):
    """让底层引擎必炸，逼出兜底路径。"""
    def boom(**kwargs):
        raise RuntimeError("模拟 Java 引擎崩溃")
    monkeypatch.setattr(morsel.opendataloader_pdf, "convert", boom)


def test_probe_density(text_pdf, scanned_pdf):
    dense = morsel.probe_text_density(text_pdf)
    sparse = morsel.probe_text_density(scanned_pdf)
    assert dense is not None and dense > morsel.SCANNED_CHARS_PER_PAGE
    assert sparse is not None and sparse < morsel.SCANNED_CHARS_PER_PAGE


def test_fallback_degrades_to_markdown(java_boom, text_pdf, tmp_path):
    r = morsel.convert_one(text_pdf, tmp_path, ["markdown", "json"], morsel.ConvertOptions())
    assert r.ok and r.degraded
    assert r.error == "" and "降级转换" in r.note and "模拟 Java 引擎崩溃" in r.note
    assert r.pages == 2
    md = tmp_path / text_pdf.stem / f"{text_pdf.stem}.md"
    assert md.exists() and "Annual Report" in md.read_text(encoding="utf-8")


def test_fallback_txt_when_no_text_format(java_boom, text_pdf, tmp_path):
    r = morsel.convert_one(text_pdf, tmp_path, ["json"], morsel.ConvertOptions())
    assert r.ok and r.degraded
    assert (tmp_path / text_pdf.stem / f"{text_pdf.stem}.txt").exists()


def test_fallback_page_markers(java_boom, text_pdf, tmp_path):
    r = morsel.convert_one(text_pdf, tmp_path, ["markdown"],
                           morsel.ConvertOptions(page_markers=True))
    assert r.ok
    md = (tmp_path / text_pdf.stem / f"{text_pdf.stem}.md").read_text(encoding="utf-8")
    assert "第 2 页" in md


def test_scanned_pdf_stays_failed(java_boom, scanned_pdf, tmp_path):
    r = morsel.convert_one(scanned_pdf, tmp_path, ["markdown"], morsel.ConvertOptions())
    assert not r.ok and not r.degraded and r.error
    assert not (tmp_path / scanned_pdf.stem).exists()  # 空目录被清理


def test_no_pdfplumber_keeps_original_failure(java_boom, text_pdf, tmp_path, monkeypatch):
    monkeypatch.setattr(morsel, "pdfplumber", None)
    r = morsel.convert_one(text_pdf, tmp_path, ["markdown"], morsel.ConvertOptions())
    assert not r.ok and "模拟" in r.error


def test_degraded_marked_in_report(java_boom, text_pdf, tmp_path):
    s = morsel.execute_batch([text_pdf], tmp_path, ["markdown"],
                             morsel.ConvertOptions(), log=lambda m: None)
    assert s.succeeded == 1
    report = (tmp_path / "report.csv").read_text(encoding="utf-8-sig")
    assert "降级转换" in report
    # 降级成功计入清单，二轮续传跳过
    s2 = morsel.execute_batch([text_pdf], tmp_path, ["markdown"],
                              morsel.ConvertOptions(), log=lambda m: None)
    assert s2.skipped == 1


# ---- bench issue #3：结构损坏（截断 / xref 坏）的 PDF 先用 qpdf 修复再喂引擎

@pytest.fixture()
def truncated_pdf(text_pdf, tmp_path):
    """把 fixture 截掉尾部 15%：xref/trailer 没了，pdfminer 拒收、qpdf 能重建。"""
    raw = text_pdf.read_bytes()
    path = tmp_path / "trunc.pdf"
    path.write_bytes(raw[: int(len(raw) * 0.85)])
    return path


@pytest.fixture()
def strict_engine(monkeypatch):
    """模拟底层引擎：文件没有 %%EOF 就拒收，否则写一份 md（记录每次喂给它的输入路径）。"""
    calls: list = []

    def convert(**kwargs):
        src = morsel.Path(kwargs["input_path"])
        calls.append(src)
        if b"%%EOF" not in src.read_bytes()[-64:]:
            raise RuntimeError(f"'{src.name}' is not a valid PDF file (corrupted or truncated content).")
        (morsel.Path(kwargs["output_dir"]) / f"{src.stem}.md").write_text("# Annual Report\n", encoding="utf-8")

    monkeypatch.setattr(morsel.opendataloader_pdf, "convert", convert)
    return calls


def test_truncated_pdf_repaired_then_full_engine(strict_engine, truncated_pdf, tmp_path):
    pytest.importorskip("pikepdf")
    r = morsel.convert_one(truncated_pdf, tmp_path, ["markdown"], morsel.ConvertOptions())
    assert r.ok and not r.degraded and r.error == ""
    assert "已修复后转换" in r.note and "not a valid PDF" in r.note
    assert [p.name for p in r.produced] == ["trunc.md"]
    # 引擎被喂了两次：原件（拒收）→ 修复副本（同名、临时目录），副本用完即删
    assert len(strict_engine) == 2 and strict_engine[1].name == "trunc.pdf" and strict_engine[1] != truncated_pdf
    assert not strict_engine[1].exists() and not strict_engine[1].parent.exists()


@pytest.fixture()
def java_reject(monkeypatch):
    """引擎对任何文件都报「结构损坏」（触发修复层），修复副本也照样拒收。"""
    def boom(**kwargs):
        raise RuntimeError("'x.pdf' is not a valid PDF file (corrupted or truncated content).")
    monkeypatch.setattr(morsel.opendataloader_pdf, "convert", boom)


def test_truncated_pdf_repair_then_pdfplumber_when_engine_still_fails(java_reject, truncated_pdf, tmp_path):
    pytest.importorskip("pikepdf")
    # 引擎连修复副本也吃不下 → 用修复副本走 pdfplumber 兜底（原件 pdfminer 是打不开的）
    assert morsel._pdfplumber_page_texts(truncated_pdf, morsel.ConvertOptions()) is None
    r = morsel.convert_one(truncated_pdf, tmp_path, ["markdown"], morsel.ConvertOptions())
    assert r.ok and r.degraded and "降级转换" in r.note
    assert "Annual Report" in (tmp_path / "trunc" / "trunc.md").read_text(encoding="utf-8")


def test_truncated_pdf_without_pikepdf_keeps_failure(java_reject, truncated_pdf, tmp_path, monkeypatch):
    monkeypatch.setattr(morsel, "pikepdf", None)
    assert morsel._repair_pdf(truncated_pdf, morsel.ConvertOptions()) is None
    r = morsel.convert_one(truncated_pdf, tmp_path, ["markdown"], morsel.ConvertOptions())
    assert not r.ok and "not a valid PDF" in r.error
    assert not (tmp_path / "trunc").exists()


def test_non_structural_engine_error_skips_repair(java_boom, text_pdf, tmp_path, monkeypatch):
    """缺 Java / OOM / 崩溃之类的失败不像结构损坏：不修复、不二次起引擎，直接兜底。"""
    monkeypatch.setattr(morsel, "_repair_pdf", lambda *a, **k: pytest.fail("非结构错误不该尝试修复"))
    assert not morsel.looks_structural_error("模拟 Java 引擎崩溃")
    assert not morsel.looks_structural_error("java.lang.OutOfMemoryError")
    assert morsel.looks_structural_error("'a.pdf' is not a valid PDF file (corrupted or truncated content).")
    r = morsel.convert_one(text_pdf, tmp_path, ["markdown"], morsel.ConvertOptions())
    assert r.ok and r.degraded


def test_run_engine_empty_exception_message_is_still_failure(text_pdf, tmp_path, monkeypatch):
    def boom(**kwargs):
        raise RuntimeError()
    monkeypatch.setattr(morsel.opendataloader_pdf, "convert", boom)
    err = morsel._run_engine(text_pdf, tmp_path, ["markdown"], morsel.ConvertOptions(), False)
    assert err == "RuntimeError"


def test_repaired_copy_without_text_is_not_success(truncated_pdf, tmp_path, monkeypatch):
    """qpdf 修出来的副本引擎肯吃、但产物没有一个字（内容流是密文/坏的）→ 不算成功，产物扔掉。"""
    pytest.importorskip("pikepdf")

    def convert(**kwargs):
        src = morsel.Path(kwargs["input_path"])
        if b"%%EOF" not in src.read_bytes()[-64:]:
            raise RuntimeError(f"'{src.name}' is not a valid PDF file (corrupted or truncated content).")
        (morsel.Path(kwargs["output_dir"]) / f"{src.stem}.md").write_text("", encoding="utf-8")

    monkeypatch.setattr(morsel.opendataloader_pdf, "convert", convert)
    r = morsel.convert_one(truncated_pdf, tmp_path, ["markdown"], morsel.ConvertOptions())
    # 空产物被删；修复副本能被 pdfplumber 读 → 降级成功；结果里不能有「已修复后转换」
    assert r.ok and r.degraded and "已修复后转换" not in r.note
    assert "Annual Report" in (tmp_path / "trunc" / "trunc.md").read_text(encoding="utf-8")


def test_repair_keeps_encryption_and_note_survives_no_output(text_pdf, tmp_path, monkeypatch):
    pikepdf = pytest.importorskip("pikepdf")
    enc = tmp_path / "enc.pdf"
    with pikepdf.open(text_pdf) as doc:
        doc.save(enc, encryption=pikepdf.Encryption(user="u1", owner="o1"))
    opts = morsel.ConvertOptions(password="u1")
    copy = morsel._repair_pdf(enc, opts)
    assert copy is not None and copy.name == "enc.pdf"
    with pikepdf.open(copy, password="u1") as doc:
        assert doc.is_encrypted  # 副本保持加密，临时目录里不落明文
    morsel.shutil.rmtree(copy.parent)
    # 引擎：原件拒收、副本接受但什么都不写 → 走「无产物」分支，修复 note 不能丢
    seen: list = []

    def convert(**kwargs):
        seen.append(kwargs["input_path"])
        if len(seen) == 1:
            raise RuntimeError("'enc.pdf' is not a valid PDF file (corrupted or truncated content).")

    monkeypatch.setattr(morsel.opendataloader_pdf, "convert", convert)
    r = morsel.convert_one(enc, tmp_path, ["markdown"], opts)
    assert len(seen) == 2
    assert r.ok and r.degraded and "已修复后转换" in r.note and "降级转换" in r.note


def test_repair_not_attempted_when_engine_succeeds(text_pdf, tmp_path, monkeypatch):
    def convert(**kwargs):
        (morsel.Path(kwargs["output_dir"]) / f"{text_pdf.stem}.md").write_text("ok\n", encoding="utf-8")

    monkeypatch.setattr(morsel.opendataloader_pdf, "convert", convert)
    monkeypatch.setattr(morsel, "_repair_pdf", lambda *a, **k: pytest.fail("成功路径不该尝试修复"))
    r = morsel.convert_one(text_pdf, tmp_path, ["markdown"], morsel.ConvertOptions())
    assert r.ok and not r.degraded and r.note == ""
