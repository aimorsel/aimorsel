#!/bin/bash
# D 阶段第二波：真实语料。与 nightrun.sh 的区别是 **docling 只跑 `docling=yes` 的分层子集**
# （由 merge_manifest.py 标记；9 s/份的它跑全量会拖到第二天中午）。
# 幂等：已有结果的 (engine, id) 自动跳过，可随时 Ctrl-C 后重跑同一条命令。
#
#   BENCH_PY=<py312 解释器> bash bench/nightrun2.sh [tag]
set -u
cd "$(dirname "$0")/.."
PY=${BENCH_PY:-python}
TAG=${1:-full}
# 必须 127.0.0.1，不能 localhost（见 PLAN 执行记录里的 502 bug）
export BENCH_OCR_URL=${BENCH_OCR_URL:-http://127.0.0.1:5002}
FAST="aimorsel,pdfplumber_txt,markitdown,pymupdf4llm"
NOOCR="pdf|docx|pptx|xlsx|html"
NEEDOCR="png|jpg|jpeg|tiff|webp|gif|scan-pdf"

echo "=== [$(date +%H:%M:%S)] wave 2a：不需要 OCR 的格式 × 4 个快引擎"
$PY -m bench.run --tag "$TAG" --engines "$FAST" --filter "format=$NOOCR"

echo "=== [$(date +%H:%M:%S)] wave 2b：不需要 OCR 的格式 × docling（仅分层子集）"
$PY -m bench.run --tag "$TAG" --engines docling --filter "format=$NOOCR,docling=yes"

echo "=== [$(date +%H:%M:%S)] wave 2c：图片/扫描件 × 3 个快引擎（多半直接 unsupported，秒过）"
$PY -m bench.run --tag "$TAG" --engines pdfplumber_txt,markitdown,pymupdf4llm --filter "format=$NEEDOCR"

echo "=== [$(date +%H:%M:%S)] wave 2d：图片/扫描件 × docling（仅分层子集）"
$PY -m bench.run --tag "$TAG" --engines docling --filter "format=$NEEDOCR,docling=yes"

echo "=== [$(date +%H:%M:%S)] wave 2e：图片/扫描件 × aimorsel，按语言分批换 OCR 服务"
$PY -m bench.run_ocr_batches --tag "$TAG"

echo "=== [$(date +%H:%M:%S)] 第二波完成"
