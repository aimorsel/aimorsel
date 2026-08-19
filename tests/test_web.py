"""Web 服务的纯函数单元测试（不起真实服务器）。"""

from __future__ import annotations

from aimorsel import morsel_web


def _multipart(filename: str, payload: bytes, boundary: str = "XBOUND") -> tuple[bytes, str]:
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="files"; filename="{filename}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + payload + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def test_parse_multipart_files():
    body, ctype = _multipart("报告.docx", b"PAYLOAD")
    files = morsel_web.parse_multipart_files(body, ctype)
    assert files == [("报告.docx", b"PAYLOAD")]


def test_parse_multipart_no_boundary():
    assert morsel_web.parse_multipart_files(b"x", "text/plain") == []


def test_parse_multipart_skips_nameless():
    boundary = "XBOUND"
    body = (f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="field"\r\n\r\n').encode() \
        + b"value" + f"\r\n--{boundary}--\r\n".encode()
    assert morsel_web.parse_multipart_files(body, f"multipart/form-data; boundary={boundary}") == []


def test_page_template_renders():
    """模板里的 {{ }} 转义必须能被 render_page 正常还原（踩过的坑）。"""
    html = morsel_web.render_page()
    assert "{{" not in html and "}}" not in html
    assert "上传文件" in html


def test_page_template_renders_english(monkeypatch):
    from aimorsel import i18n

    monkeypatch.setenv("MORSEL_LANG", "en")
    monkeypatch.setattr(i18n, "_lang", None)
    html = morsel_web.render_page()
    assert 'lang="en"' in html and "Upload files" in html
    monkeypatch.setattr(i18n, "_lang", None)  # 还原解析缓存，别影响后续用例
