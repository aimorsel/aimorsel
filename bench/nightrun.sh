#!/bin/bash
# D 阶段夜跑驱动。三波依序跑，可随时 Ctrl-C，**重跑同一条命令自动续跑**（已有结果的跳过）。
#
#   BENCH_PY=<py312 解释器> bash bench/nightrun.sh [tag]
#
# 为什么分三波：OCR 服务的识别语言是启动时固定的，需要 OCR 的输入（图片、扫描件）
# 必须按语言分批换服务（见 bench/run_ocr_batches.py），而不需要 OCR 的格式可以一口气跑完。
set -u
cd "$(dirname "$0")/.."
PY=${BENCH_PY:-python}
TAG=${1:-full}
# 曾必须用 127.0.0.1 而不是 localhost（urllib 经系统代理拿 502 → 判离线 → 图片静默空输出）；
# 2026-08-18 起主程序默认已是 127.0.0.1 且绕过代理（bench #4），这里保留显式值只是为了跑批可复现。
export BENCH_OCR_URL=${BENCH_OCR_URL:-http://127.0.0.1:5002}
NOOCR="pdf|docx|pptx|xlsx|html|txt|md"
NEEDOCR="png|jpg|jpeg|tiff|webp|gif|scan-pdf"

echo "=== [$(date +%H:%M:%S)] wave 1a：不需要 OCR 的格式 × 全部引擎"
$PY -m bench.run --tag "$TAG" --filter "format=$NOOCR"

echo "=== [$(date +%H:%M:%S)] wave 1b：图片/扫描件 × 非 aimorsel 引擎（各自带 OCR 或直接不支持）"
$PY -m bench.run --tag "$TAG" --engines pdfplumber_txt,markitdown,pymupdf4llm,docling \
    --filter "format=$NEEDOCR"

echo "=== [$(date +%H:%M:%S)] wave 1c：图片/扫描件 × aimorsel，按语言分批换 OCR 服务"
$PY -m bench.run_ocr_batches --tag "$TAG"

echo "=== [$(date +%H:%M:%S)] 全部完成"
