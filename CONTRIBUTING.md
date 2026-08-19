# Contributing / 参与开发

Thanks for your interest! Issues and pull requests are welcome in **English or
Chinese**. 欢迎用中文或英文提 issue 和 PR。

## Dev setup / 开发环境

Python 3.10+ and Java 11+ (the layout engine is a Java JAR shipped inside
`opendataloader-pdf`).

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest        # full suite, ~3s; Java integration tests auto-skip if java is absent
```

## Architecture in five rules / 架构五条规矩

1. **One source of truth for options.** Every conversion parameter lives in the
   `ConvertOptions` dataclass in `morsel.py`. CLI, interactive mode, GUI and Web
   all build this object and pass it to `convert_one()`. Adding a parameter =
   add a field + translate it in `to_convert_kwargs()` + wire one line per UI.
2. **One batch engine.** Resume, parallelism, CSV report, OCR probing and
   logging all live in `execute_batch()`. CLI and GUI are thin wrappers over it.
3. **One structure-tree schema.** `format_adapters.py` normalizes docx/xlsx/pptx/HTML
   into the same JSON tree the Java engine emits (schema documented at the top
   of that file). Downstream features (chunking, table export, QA, MCP) consume
   only that schema — new input formats must produce it, then everything else
   works for free.
4. **Failures never kill a batch.** `convert_one()` returns a `ConvertResult`
   instead of raising; the pdfplumber fallback catches engine failures and marks
   the row "degraded".
5. **User-facing strings go through `tr()`.** Never write user-visible f-strings.
   Use `tr("中文原文", **kwargs)` (Chinese text is the key) and add the English
   translation to `_EN` in `i18n.py` — `tests/test_i18n.py` fails on missing
   translations, matching what CI will tell you.

## Tests / 测试

- `tests/test_core.py` — pure logic, no Java
- `tests/test_adapters.py`, `test_office_convert.py` — office formats, no Java
- `tests/test_fallback.py` — fallback net with the engine monkeypatched
- `tests/test_integration_java.py` — real engine, auto-skipped without `java`
- CI runs the suite on Linux / macOS (arm64 + x86_64) / Windows × Python 3.10 & 3.12

Please add tests with your change; keep the suite fast (mock the engine unless
you are specifically testing it). Test fixtures are generated in `conftest.py`
or committed under `tests/data/` — do not use platform-specific tools
(e.g. `cupsfilter`) to create them.

## Pull requests / 提交 PR

- One topic per PR; explain **why**, not just what.
- `pytest` must pass; CI runs the matrix on your PR.
- Comments in the codebase are mostly Chinese — either language is fine for new
  code, but keep user-facing strings bilingual via `tr()` (rule 5).

## Release engineering / 发布工程

Packaging lives in `packaging/` (`build.py` builds a self-contained bundle with
a trimmed JRE; `sign_macos.py` signs/notarizes on macOS). The release pipeline
is `.github/workflows/release.yml` — pushing a `v*` tag builds all three
platforms and drafts a GitHub Release.
