"""bench #5：图片输入在 OCR 不可用时产物无文字，必须标 degraded 而不是「成功」；
OCR 服务上线后重跑要自动补转。monkeypatch 掉 PDF 引擎，免 Java。"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from aimorsel import morsel


@pytest.fixture()
def fake_engine(monkeypatch):
    """替身 _convert_pdf：use_ocr=False 只写一张图的版面（无文字），use_ocr=True 写出识别文字。"""
    class Calls(list):
        state = {"ocr_finds_text": True}
    calls = Calls()
    state = calls.state  # 测试可翻成 False 模拟「服务在线但 OCR 认不出字 / 后端失败退回 Java」

    def fake_convert_pdf(pdf: Path, out_root: Path, formats, options, use_ocr=False):
        calls.append(use_ocr)
        dest = out_root / pdf.stem
        dest.mkdir(parents=True, exist_ok=True)
        produced = []
        if use_ocr and state["ocr_finds_text"]:
            md, tree = "Hello AImorsel\n", {"kids": [{"type": "paragraph", "content": "Hello AImorsel", "page number": 1}]}
        else:
            md = f"![image]({pdf.stem}_images/imageFile1.png)\n\n---\n\n**— 第 1 页 —**\n\n"
            tree = {"number of pages": 1, "kids": [{"type": "image", "content": "", "page number": 1}]}
        if "markdown" in formats:
            p = dest / f"{pdf.stem}.md"; p.write_text(md, encoding="utf-8"); produced.append(p)
        if "json" in formats:
            p = dest / f"{pdf.stem}.json"; p.write_text(json.dumps(tree), encoding="utf-8"); produced.append(p)
        if "html" in formats:  # 引擎的 HTML 产物 <title> 里是文件名——不能被数成文字
            body = "<p>Hello AImorsel</p>" if (use_ocr and state["ocr_finds_text"]) else f"<img src='{pdf.stem}_images/imageFile1.png'>"
            p = dest / f"{pdf.stem}.html"
            p.write_text(f"<html><head><title>{pdf.stem}.pdf</title><style>p{{color:red}}</style></head><body>{body}</body></html>", encoding="utf-8")
            produced.append(p)
        # 与真实 _convert_pdf 一致：produced 是目录里现存全部文件（含上一轮的旧产物）
        produced = sorted(q for q in dest.rglob("*") if q.is_file())
        return morsel.ConvertResult(pdf=pdf, ok=True, produced=produced, pages=1, used_ocr=use_ocr)

    monkeypatch.setattr(morsel, "_convert_pdf", fake_convert_pdf)
    return calls


def test_extracted_text_chars(tmp_path):
    md = tmp_path / "a.md"
    md.write_text("![img](a_images/imageFile1.png)\n\n---\n\n**— 第 1 页 —**\n\n**— Page 2 —**\n<img src='x.png'>\n", encoding="utf-8")
    assert morsel.extracted_text_chars([md]) == 0
    md.write_text("# 你好 ab\n![img](a.png)\n", encoding="utf-8")
    assert morsel.extracted_text_chars([md]) == 4
    js = tmp_path / "a.json"
    js.write_text('{"kids":[{"type":"image","content":""},{"kids":[{"content":"x1"}]}]}', encoding="utf-8")
    assert morsel.extracted_text_chars([js, md]) == 2  # 有 JSON 优先读 JSON
    assert morsel.extracted_text_chars([tmp_path / "a.png"]) == -1  # 没有文本产物：无法判断


def test_image_without_ocr_is_degraded_not_success(fake_engine, sample_png, tmp_path):
    r = morsel.convert_one(sample_png, tmp_path, ["markdown"], morsel.ConvertOptions(), use_ocr=False)
    assert r.ok and r.degraded and r.needs_ocr and r.pdf == sample_png
    assert "未经 OCR" in r.note and r.error == ""
    assert (tmp_path / sample_png.stem / f"{sample_png.stem}.md").exists()
    # 走了 OCR 但仍无文字：降级，但不算「等 OCR」（服务上线也不会自动重转）
    r2 = morsel.convert_one(sample_png, tmp_path, ["markdown"], morsel.ConvertOptions(), use_ocr=True)
    assert r2.ok and not r2.degraded and not r2.needs_ocr and fake_engine == [False, True]


def test_image_ocr_produced_nothing(fake_engine, sample_png, tmp_path):
    # 走了 OCR 仍无字（可能是后端失败被 hybrid_fallback 静默退回 Java）：降级，也算「等 OCR」再试
    fake_engine.state["ocr_finds_text"] = False
    r = morsel.convert_one(sample_png, tmp_path, ["markdown"], morsel.ConvertOptions(), use_ocr=True)
    assert r.ok and r.degraded and r.needs_ocr and "OCR 未产出文字" in r.note


def test_html_only_output_detects_empty(fake_engine, sample_png, tmp_path):
    r = morsel.convert_one(sample_png, tmp_path, ["html"], morsel.ConvertOptions(), use_ocr=False)
    assert r.degraded and r.needs_ocr  # <title>photo.pdf</title> 不能被数成文字
    r2 = morsel.convert_one(sample_png, tmp_path, ["html"], morsel.ConvertOptions(), use_ocr=True)
    assert r2.ok and not r2.degraded


def test_stale_products_from_previous_round_are_ignored(fake_engine, sample_png, tmp_path):
    import os, time
    # 上一轮（OCR 在线、含 json）留下的有字 JSON 还在目录里；本轮只要 markdown、OCR 离线 → 仍须判空
    r1 = morsel.convert_one(sample_png, tmp_path, ["markdown", "json"], morsel.ConvertOptions(), use_ocr=True)
    assert not r1.degraded
    stale = tmp_path / sample_png.stem / f"{sample_png.stem}.json"
    old = time.time() - 10
    os.utime(stale, (old, old))
    r2 = morsel.convert_one(sample_png, tmp_path, ["markdown"], morsel.ConvertOptions(), use_ocr=False)
    assert stale in r2.produced and r2.degraded and r2.needs_ocr
    # 反向：旧的空 JSON + 本次 OCR 有字 → 不能误判降级
    os.utime(stale, (old, old))
    r3 = morsel.convert_one(sample_png, tmp_path, ["markdown"], morsel.ConvertOptions(), use_ocr=True)
    assert not r3.degraded


def test_batch_marks_degraded_and_redoes_when_ocr_comes_online(fake_engine, sample_png, tmp_path, monkeypatch):
    online = {"ok": False}
    monkeypatch.setattr(morsel, "check_ocr_server", lambda url, timeout=2.0: online["ok"])
    logs: list[str] = []
    opts = morsel.ConvertOptions()  # ocr_mode=auto

    s1 = morsel.execute_batch([sample_png], tmp_path, ["markdown", "json"], opts, log=logs.append)
    assert s1.succeeded == 1 and s1.failed == 0
    assert any("△" in line and sample_png.name in line for line in logs)
    rows = list(csv.DictReader((tmp_path / "report.csv").open(encoding="utf-8-sig")))
    assert rows[0]["状态"] == "降级转换" and "未经 OCR" in rows[0]["说明"] and "OCR 服务未启动" in rows[0]["说明"]
    entry = next(iter(morsel.load_manifest(tmp_path).values()))
    assert entry.get("needs_ocr") is True

    # 服务仍离线：不重转（监听模式下不会每轮刷屏重跑）
    s2 = morsel.execute_batch([sample_png], tmp_path, ["markdown", "json"], opts, log=logs.append)
    assert s2.skipped == 1 and fake_engine == [False]

    # 服务上线：清单里等 OCR 的图片自动补转，补转后清单不再带 needs_ocr
    online["ok"] = True
    s3 = morsel.execute_batch([sample_png], tmp_path, ["markdown", "json"], opts, log=logs.append)
    assert s3.skipped == 0 and s3.succeeded == 1 and fake_engine == [False, True]
    rows = list(csv.DictReader((tmp_path / "report.csv").open(encoding="utf-8-sig")))
    assert rows[0]["状态"] == "成功" and rows[0]["OCR"] == "是"
    entry = next(iter(morsel.load_manifest(tmp_path).values()))
    assert "needs_ocr" not in entry
    s4 = morsel.execute_batch([sample_png], tmp_path, ["markdown", "json"], opts, log=logs.append)
    assert s4.skipped == 1

    # 服务在线但 OCR 认不出字（或后端失败退回 Java）：最多自动重试 MAX_OCR_ATTEMPTS 次，之后视为空白图不再重转
    fake_engine.state["ocr_finds_text"] = False
    fake_engine.clear()
    blank = tmp_path / "blank"
    for i in range(morsel.MAX_OCR_ATTEMPTS + 2):
        s = morsel.execute_batch([sample_png], blank, ["markdown"], opts, log=logs.append)
    entry = next(iter(morsel.load_manifest(blank).values()))
    assert fake_engine == [True] * morsel.MAX_OCR_ATTEMPTS and s.skipped == 1
    assert entry["needs_ocr"] and entry["ocr_attempts"] == morsel.MAX_OCR_ATTEMPTS
    fake_engine.state["ocr_finds_text"] = True

    # ocr_mode=off：图片就是只提版面，不探测服务、不标等 OCR、正常记清单
    fake_engine.clear()
    off = morsel.ConvertOptions(ocr_mode="off")
    s5 = morsel.execute_batch([sample_png], tmp_path / "off", ["markdown"], off, log=logs.append)
    assert s5.results[0].degraded and "OCR 已关闭" in s5.results[0].note
    entry = next(iter(morsel.load_manifest(tmp_path / "off").values()))
    assert entry.get("needs_ocr") is True  # 关着 OCR 也如实记录「无文字」；只有服务在线且 OCR 未关才会补转
    assert not morsel.ocr_redo_available(morsel.load_manifest(tmp_path / "off"), off)


def test_ocr_redo_available_only_probes_when_needed(monkeypatch):
    def boom(url, timeout=2.0):
        raise AssertionError("不该探测")
    monkeypatch.setattr(morsel, "check_ocr_server", boom)
    opts = morsel.ConvertOptions()
    assert not morsel.ocr_redo_available({}, opts)
    assert not morsel.ocr_redo_available({"/x.png": {"signature": "s"}}, opts)
    monkeypatch.setattr(morsel, "check_ocr_server", lambda url, timeout=2.0: True)
    assert morsel.ocr_redo_available({"/x.png": {"signature": "s", "needs_ocr": True}}, opts)
    assert not morsel.ocr_redo_available({"/x.png": {"needs_ocr": True}}, morsel.ConvertOptions(ocr_mode="off"))


def test_check_ocr_server_survives_non_http_peer():
    import socket, threading
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
    port = srv.getsockname()[1]

    def serve():
        conn, _ = srv.accept()
        conn.recv(1024); conn.sendall(b"garbage\r\n\r\n"); conn.close()
    threading.Thread(target=serve, daemon=True).start()
    assert morsel.check_ocr_server(f"http://127.0.0.1:{port}") is False  # BadStatusLine 不是 OSError，也得吞掉
    srv.close()
