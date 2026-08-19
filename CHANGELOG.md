# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/); versions follow
[SemVer](https://semver.org/). 中英文混排：条目以英文为主，必要处附中文。

## [Unreleased]

### Added
- Full interface i18n (Chinese/English): CLI, GUI, web UI and MCP tool
  descriptions follow `MORSEL_LANG` / `config.toml [ui] lang` / system locale
- Bilingual README (`README.en.md`), UI screenshots and a conversion demo GIF
- `CONTRIBUTING.md`, issue/PR templates, this changelog

### Changed
- **Renamed to AImorsel / `morsel`**: modules are now `morsel.py`,
  `morsel_gui.py`, `morsel_web.py`, `morsel_mcp.py`; the packaged command is
  `morsel`, with `morsel gui` / `morsel web` / `morsel mcp` as subcommands (a
  real file with one of those names still wins — write `./gui` to force it).
  Environment variables moved from `PDF2MD_*` to `MORSEL_*`, and the managed OCR
  environment from `~/.pdf2md` to `~/.aimorsel`. Moving the old directory over is
  enough — no multi-GB re-download: the service is now started through the venv's
  `bin/python` when the console script's absolute shebang no longer resolves, and
  `is_installed()` no longer trusts that script. If the old directory is still
  there, installation stops and tells you to move it instead of silently
  downloading everything again
- Post-processing steps that change output without changing any option
  (compatibility code point normalization, RTL restoration, deskew, structure
  tidying) now participate in the resume signature via `PIPELINE_VERSION`, so
  previously converted files are redone after such a fix instead of being
  silently skipped

### Fixed

Findings from the 2026-08 benchmark run (731 documents x 5 engines), fixed in
four batches. Numbers below are before/after on the affected subset; full
methodology and per-subset tables are in `bench/RESULTS.md`.

Batch 1 — extraction correctness
- Arabic/Hebrew PDFs were emitted in **visual order**, so text looked fine but
  grep, tokenization and embeddings all failed -> logical-order restoration
  (`rtl_text.py`, simplified UAX#9, document-level detection so logical-order
  files are untouched) -> visual-order ratio 0.98-1.0 to <=0.02 on all 10
  Arabic PDFs (`94656bf`)
- HTML input dropped inline math entirely -> MathML is kept as LaTeX text
  (`$...$` / `$$...$$`, taken from the TeX annotation, `alttext`, or the MathML
  tokens) -> 652 formulas recovered on a single Wikipedia article (`fcc9371`)
- Structurally damaged / truncated PDFs failed outright, and the pdfplumber
  fallback could not read them either -> repair with pikepdf/qpdf first, then
  re-feed the engine (full structure tree, not a degraded result) -> 4 failing
  arXiv papers to 0, success rate 99.5% to 100% (`fcc9371`)
- Compatibility code point normalization ran only on the PDF path, so HTML
  numeric entities for ligatures survived -> normalize on the office/HTML path
  too -> residual 4 to 0 across the corpus (`5ecf142`)
- OCR health check used `localhost` and was answered by a system proxy, so the
  service was judged offline while running -> default to `127.0.0.1` and bypass
  proxies explicitly (`5ecf142`)
- Image input with no OCR available reported **success** with empty text ->
  count the characters actually produced, mark the result as degraded, record
  `needs_ocr` in the resume manifest and re-convert automatically (at most
  twice) once the OCR service is up (`7dd6bc3`)

Batch 2 — HTML/Office structure (`cb8bf30`)
- SEC filings leaked XBRL metadata into the body -> drop `ix:header` /
  `ix:hidden` and inline `display:none` / `visibility:hidden` elements ->
  HTML subset char_sim 0.825 to 0.856 (one filing shed 103k characters of
  noise; 20 filings, 1.0 MB total)
- Documents with no heading styles produced zero headings -> conservatively
  promote short numbered lines to headings (multilingual patterns: zh/ja
  chapter markers, Part/Chapter/Section/Article and their de/es/fr forms),
  only when the document has no headings at all -> docx heading_f1 0.583 to
  0.637

Batch 3 — image quality and structure noise (`a94118a`)
- Rotated/skewed scans lost content -> projection-based deskew before the
  engine (Pillow only, correction applied above 1.0 degrees) -> tiff subset
  char_sim 0.601 to 0.754, CER 0.499 to 0.320, heading_f1 0.813 to 0.871; of
  188 image files only 39 exceed the threshold and all are genuinely skewed
- Single characters, RFC 2119 keywords and filename-like strings were promoted
  to headings, and numbered clauses were rendered as lists -> `tidy_products()`
  demotes them in Markdown/JSON/HTML with identical, idempotent rules; the
  rules were validated against 6,632 ground-truth headings with 0 false hits
- A single image took 167.8 s -> traced to the benchmark harness running two
  documents against one CPU-bound OCR service; wall clock was identical at
  `--jobs 1`, so the harness was fixed rather than the converter

Batch 4 — measurement and documentation (`08895fd`, `2225764`)
- Numbers in scanned documents are silently misread, and text metrics do not
  reveal it -> added a digit-fidelity metric (multiset precision/recall/F1 over
  numeric tokens) and a README section on the limitation -> measured 0.773
  digit-precision on 20 real scanned financial reports while char_sim was 0.590
  (see #2)
- Table cells scrambled within a row on the image/OCR path -> traced to
  upstream reading-order re-application; no fix available in this layer
  (see #1)
- Correct formula output was being penalized by the scorer -> strip `$...$` on
  both the ground-truth and the output side -> HTML subset 0.856 to 0.858
- Benchmark only: a type check in the metric code forced word-level CER onto a
  pure-Python DP path, stalling a whole run on one large filing; fixed with a
  length guard (recorded for whoever finds a `metrics_truncated` flag)

Batch 5 — leftovers from the pre-benchmark smoke run
- List items came out with a doubled marker (`- • First point`): the engine
  keeps the bullet glyph inside the list-item content and the renderer adds its
  own `- ` on top. The glyph is now stripped in the tidy pass across Markdown /
  JSON / HTML alike, so RAG chunks and `get_section` are clean too. Measured on
  the 20 corpus documents whose ground truth contains the most bullet glyphs
  (SEC filings, Wikipedia): zero changes — those glyphs sit inside table cells
  and paragraphs, not at the start of list items. Scores are unaffected
  (`norm_text` already stripped leading markers on both sides); this is purely
  about what the user reads
- Blocks laid out side by side (a centred table next to a left-aligned list)
  can be emitted in an order that does not match visual reading order. Measured
  rather than guessed: fixing one such page would raise char_sim from 0.820 to
  1.000, but the geometry that triggers it is indistinguishable from a normal
  two-column page (the two blocks do not overlap horizontally), and a
  column-detection guard still misfired on 39 two-column arXiv papers. The
  affected shape is a synthetic-corpus template (48 files, one occurrence each)
  and is rare in real documents, so this stays an upstream reading-order
  limitation — same call as the image/OCR row-order issue above

## [0.1.0] - 2026-07-25

First packaged pre-release (internal preview). 首个打包预发布（内部预览）。

### Added
- **Conversion core**: PDF layout analysis via opendataloader-pdf — heading
  hierarchy, paragraphs, lists, tables, page numbers and coordinates;
  outputs Markdown / JSON structure tree / HTML / plain text / annotated PDF
- **Multi-format input**: docx / xlsx / pptx / HTML via lightweight adapters
  normalized to the same structure-tree schema; images route through OCR
- **Batch engineering**: resumable incremental batches (`.done.json`),
  parallel workers, `report.csv`, folder watching (`--watch`)
- **AI-oriented processing**: RAG chunking with heading paths and page ranges
  (`--rag-chunks`), table export to CSV, multi-document merge with TOC,
  per-page quality check (`--qa`)
- **OCR for scanned documents**: auto-detection (`--ocr auto`) with graceful
  degradation when the hybrid service is absent
- **Fallback net**: pdfplumber plain-text extraction when the engine fails,
  reported as "degraded" instead of failing the batch
- **Four frontends**: interactive CLI, tkinter GUI with drag & drop, local web
  service with folder watching, and an MCP server (8 tools including
  progressive disclosure: `get_outline` / `get_section` / `search_documents`)
- **Distribution**: self-contained builds with a trimmed JRE for macOS
  (arm64 / x86_64) and Windows x64; CI test matrix across 4 OS targets;
  macOS signing + notarization pipeline
- License: Apache-2.0
