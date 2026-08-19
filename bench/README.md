# bench/ —— AImorsel（文粒）评测基准

规划与验收标准见 [PLAN.md](PLAN.md)（末尾「执行记录」是每轮跑批的实际数字与踩坑）；
结果见 [RESULTS.md](RESULTS.md)，人工抽检见 [SPOTCHECK.md](SPOTCHECK.md)，
评测暴露的问题见 [ISSUES-draft.md](ISSUES-draft.md)。本文只讲怎么跑。

```bash
# 0) 依赖（主项目 py312 环境）
pip install rapidfuzz 'markitdown[docx,xlsx,pptx,pdf]' pymupdf4llm matplotlib
#    docling 装在独立环境里（它拖 torch，别污染主环境），解释器路径写进 bench/engines.toml（复制 engines.example.toml）

# 1) 语料
python -m bench.make_synthetic     # 合成集 189 份（7 语 × 领域 × 9 格式，全带精确真值）
python -m bench.fetchers.rfc       # 真实语料：13 个采集脚本，逐个跑（都幂等，重跑不重复下载）
python -m bench.make_images        # 从已下载的真实 PDF 渲染图片/扫描件 100 份
python -m bench.make_truth         # 由 truth_src（HTML/LaTeX/官方 XML）生成真值 JSON
python -m bench.merge_manifest --verify-sha   # 分片 → corpus/manifest.jsonl（校验/去重/标 docling 子集）

# 2) 先估时：每引擎前 20 份
python -m bench.run --sample 20 --tag est

# 3) 全量（幂等，Ctrl-C 后重跑同一条命令自动续）
BENCH_PY=<py312 解释器> bash bench/nightrun.sh full    # 合成集：三波（非 OCR → 图片 → 图片×OCR 分语言）
BENCH_PY=<py312 解释器> bash bench/nightrun2.sh full   # 真实语料：docling 只跑 docling=yes 的分层子集

# 4) 收尾与报告
python -m bench.retry_failed       # 重跑 fail/timeout（unsupported 是能力结论、degraded 是兜底成功，都不重跑）
python -m bench.report             # → RESULTS.md（+ results/charts/*.png）
```

## 脚本一览

| 脚本 | 作用 |
|---|---|
| `make_synthetic.py` | 合成语料 189 份，真值 100% 精确 |
| `fetchers/*.py` | 13 个真实语料采集脚本，共用 `common.py` / `_common.py`（**两个功能重叠，待合并**） |
| `make_images.py` | 拿真实 PDF 渲染成 png/jpg/tiff + 图片化 PDF，真值取源 PDF 文字层 |
| `truth/{from_html,from_latex,from_xml}.py` + `make_truth.py` | 由真值源生成真值 JSON。**不 import `format_adapters`**——真值必须与被测代码解耦 |
| `merge_manifest.py` | 分片 → manifest：枚举校验 / ≤8 MB ≤30 页 / sha256 去重与校验 / 分层抽样标 `docling=yes` |
| `run.py` | 跑批：续跑、超时、并发、串行队列、`--sample/--filter/--ids/--force` |
| `run_ocr_batches.py` | 需要 OCR 的输入按语言分批：每种语言在 5011 端口起临时服务，**不动用户常驻的 5002** |
| `nightrun.sh` / `nightrun2.sh` | 三波 / 五波驱动。**改脚本前先确认它没在运行**——bash 按字节偏移读脚本，改运行中的脚本会跑歪 |
| `retry_failed.py` | 重跑失败件 |
| `rescore.py` | **指标改了之后**用已落盘的产物重算，不重跑引擎（几分钟 vs 几小时） |
| `report.py` | 汇总出 `RESULTS.md`，含「读这份表之前」的口径说明 |
| `metrics.py` | 指标，单测在 `tests/`（32 项） |

目录：`corpus/manifest.jsonl` 语料清单 · `corpus/manifest.d/*.jsonl` 采集分片 ·
`results/<engine>.jsonl` 一文档一行 · `results/out/<engine>/<id>/` 原始产物 · `logs/`。
`corpus/ results/ logs/ envs/ engines.toml` 都不入库。

真值 JSON：`{"text", "headings", "heading_levels", "tables", "paragraphs", "note"}`，字段可缺。
**缺某个字段 = 这份不评那项指标**；⚠️ 写空数组 `[]` 会惩罚做对的引擎（`heading_f1` 判 0），
抓不准就整个省略该键。

## 跑批前必看的三件事

1. **OCR 服务地址用 `127.0.0.1` 不要用 `localhost`** —— 配了系统代理的机器上 urllib 请求
   `localhost` 会拿到 502，`check_ocr_server` 判离线后图片静默输出空文本且状态仍是 ok
   （ISSUES-draft #4）。`nightrun*.sh` 已统一 `export BENCH_OCR_URL=http://127.0.0.1:5002`。
2. **卡死的诊断特征**：`run.py` 父进程 99.9% CPU 但 `pgrep -f bench.engines` 为空 = 卡在**指标**
   而不是引擎（历史上被一份 1.2 MB 的 SEC 10-K 卡了 20 分钟）。
3. **无人值守前**：`caffeinate -dims` 包住、机器插电、磁盘 ≥ 20 GB、
   docling 首次会下载模型（先联网跑一遍 `--sample`）。
