# AImorsel（文粒）评测规划 —— bench/

> 状态：**规划稿，待执行**（2026-08-17 定）。执行时新开 session，按本文件逐阶段做，
> 每阶段结束更新本文件末尾「执行记录」。本目录将随开源仓库公开：**不放任何个人路径、
> 不放不可再分发的语料**（只放 manifest + 下载脚本 + 结果表）。

## 0. 目标与验收

回答三个问题，每个都要有数字：
1. **质量**：AImorsel 转出的 Markdown/JSON 有多忠实（文本、标题、表格、阅读顺序、码位）？
2. **对比**：与 markitdown / docling / pymupdf4llm / pdfplumber（必测）及 marker / MinerU
   （许可原因只对比、不集成，可选）相比处在什么位置？
3. **工程**：500+ 文档无人值守跑完，崩溃率 / 降级率 / 秒每页 / 峰值内存各是多少？

验收：`bench/RESULTS.md` 一张总表 + 分语言/分领域/分格式三张分表 + 失败清单；
数字可复现（同一 manifest + 同一脚本重跑，误差 < 1%）。

## 1. 语料（≥ 500 份，目标 600 留余量）

### 1.1 维度配额

| 维度 | 取值与目标份数 |
|---|---|
| **格式** | PDF 文字层 260 · PDF 扫描/图片化 60 · JPG/JPEG/PNG/TIFF 80 · HTML 60 · docx 50 · xlsx 30 · pptx 30 · 其他（含 WebP/GIF 多帧）10 —— 合计约 580 |
| **语言** | 中文 150 · 英语 150 · 西班牙语 50 · 德语 40 · 法语 40 · 阿拉伯语 50（RTL 重点）· 日语 30 · 俄语 30 · 混合/多语 40 |
| **领域** | IT/技术文档 · 数学/学术 · 工商/财报 · 法律/法规 · 教育/教材 · 政务/公文 · 医学 · 新闻/通用 —— 每领域 ≥ 50 |
| **版面难度** | 单栏 / 双栏 / 含表格 ≥ 120 份 / 含公式 ≥ 60 / 含图 / 含页眉页脚 / 竖排或 RTL |

### 1.2 来源（全部公开可下载；许可写进 manifest，仅可再分发的才允许入库）

| 来源 | 覆盖 | 许可 | 备注 |
|---|---|---|---|
| arXiv（按 license 过滤 CC-BY）| 数学/IT/物理 英文 PDF，**附 LaTeX 源码 = 天然真值** | CC-BY 子集 | 用 arXiv API/OAI，只取 CC-BY 4.0 |
| ChinaXiv / 中国政府网白皮书 / 国务院公报 | 中文学术 + 政务 PDF | 公开 | 政务 PDF 多为 Quartz/方正生成，正好测兼容码位 |
| UN ODS（联合国文件系统） | **同一份文件六种官方语言**（阿/中/英/法/俄/西）PDF + docx | 公开 | 阿拉伯语与跨语言对齐的主力来源 |
| EUR-Lex | 法律，24 种语言平行文本，PDF + HTML | 公开（可再用） | 法律 + 西/德/法；HTML 版即真值 |
| SEC EDGAR 10-K/10-Q | 工商/财报，英文 HTML + PDF，表格密集 | 公开 | 表格评测主力 |
| 巨潮资讯网年报 | 中文财报 PDF，表格密集 | 公开 | 中文表格评测主力 |
| OpenStax 教材 | 教育，英/西，PDF + HTML 双版本 | CC-BY | HTML 版作真值 |
| IETF RFC / W3C 规范 / Python 官方文档 | IT，txt/HTML/PDF 三形态 | 公开 | 同一内容多格式，测格式一致性 |
| 日本 e-Gov 法令 / 德国 gesetze-im-internet | 日语、德语法律 | 公开 | |
| DocLayNet（IBM） | 版面标注 PDF 页（金融/科技/法律/手册），**含人工版面真值** | CDLA-Permissive 1.0 | 标题/表格/阅读顺序真值来源 |
| FUNSD / SROIE / XFUND | 扫描表单、票据 JPG，**含 OCR 真值**；XFUND 含中/日/西/法/意/德/葡 | 研究许可（下载不入库） | OCR + 多语图片评测 |
| 自造图片集 | 从上面 PDF 语料抽 60 页渲染成 JPG/PNG（150/300 dpi、±3° 旋转、轻噪声、TIFF 多帧） | 与源相同 | 真值 = 源 PDF 文字层；`bench/make_images.py` 生成 |
| 自造 Office 集 | 用 python-docx/openpyxl/python-pptx 程序化生成 30+ 份带标题/表格/列表/多语文本的文件 | 自有 | 结构真值 100% 已知 |

**规矩**：
- `bench/manifest.jsonl` 一行一份：`{id, url, sha256, format, lang, domain, license, truth_type, truth_ref, notes}`；
- `bench/fetch.py` 按 manifest 下载到 `bench/corpus/`（.gitignore），校验 sha256，失败重试 3 次后记录跳过；
- 只有 license ∈ {CC-BY, CC0, 公有领域, CDLA-Permissive, 自造} 的才允许作为 fixture 入库，其余永远只留 URL。

## 2. 真值（ground truth）——分四档，manifest 里 `truth_type` 标明

| 档 | 适用 | 真值来源 | 能算的指标 |
|---|---|---|---|
| A 精确 | arXiv(LaTeX) / EUR-Lex & OpenStax & RFC(HTML 版) / 自造 Office / 自造图片 / FUNSD·XFUND | 源文件解析出的纯文本 + 标题列表 + 表格 | 全部指标 |
| B 版面标注 | DocLayNet | 人工标注的元素类别与 bbox | 标题召回、表格检出、阅读顺序 |
| C 引擎共识 | 无源的 PDF（政务/财报） | 5 个引擎输出两两对齐，取多数一致段落为伪真值 | 文本保真（相对）、异常检测 |
| D 人工抽检 | 每语言 × 每领域抽 2 份，共约 60 份 | 执行者对照原文打分（1–5，rubric 见 §3.4） | 主观质量校准 A/B/C 的结论 |

不做 LLM-as-judge 主评（成本高、不可复现）；可选做 60 份 D 档的辅助复核。

## 3. 指标（`bench/metrics.py`，每个都要有单元测试）

### 3.1 文本保真
- `char_sim`：去空白、NFC 后的字符级相似度（rapidfuzz `ratio`，0–1）
- `compat_residual`：残留兼容码位数（康熙部首/兼容表意/连字），应为 0
- `rtl_visual_ratio`（2026-08-18 加，issue #0 口径）：RTL 文本按视觉序存放的证据占比（0–1，无 RTL 为空）；报告列「RTL视觉序(份)」= 判成视觉序（>2/3）的份数 / 有 RTL 证据的份数，应为 0/N
- `cer`（仅图片/扫描）：字符错误率；阿拉伯语另报 `word_er`
- `garbage_ratio`：不可打印/替换字符 U+FFFD 占比

### 3.2 结构
- `heading_p / heading_r / heading_f1`：标题文本集合匹配（模糊 ≥ 0.9）
- `heading_level_acc`：匹配上的标题层级正确率
- `table_detect_r`：表格检出召回；`cell_f1`：单元格文本 F1（表格对齐后逐格）
- `list_r`：列表项召回

### 3.3 阅读顺序与页
- `order_tau`：段落顺序 Kendall τ（对齐后）
- `page_attr_acc`：JSON 里 page number 正确率（A/B 档）
- 双栏文档单列 τ

### 3.4 工程 / 性能
- `wall_s`、`s_per_page`（冷/热各一次）、`peak_rss_mb`（`resource`/psutil 子进程采样）
- `status ∈ {ok, degraded, fail, timeout}`；每引擎每文档超时 300 s
- 输出体积、是否离线（跑批时断网或 `--offline` 环境变量校验无外联）
- RTL 专项：阿拉伯语行内字符顺序是否被反转（与真值 `char_sim` 对比 + 抽检）

D 档人工 rubric（1–5）：内容完整 / 结构正确 / 表格可用 / 顺序通顺 / 无乱码。

## 4. 对照引擎

| 引擎 | 许可 | 处理 | 备注 |
|---|---|---|---|
| **AImorsel**（本项目，默认参数 + `--ocr auto`） | Apache-2.0 | 主角 | 图片走 OCR 服务（`--setup-ocr` 装好，语言按语料分批 `--ocr-lang`） |
| markitdown | MIT | 必测 | 无版面分析，是「下限参照」 |
| docling | MIT | 必测 | 质量上限参照，慢；装在独立环境里 |
| pymupdf4llm | AGPL | 必测（只对比） | |
| pdfplumber 纯文本 | MIT | 必测 | 相当于我们的兜底档 |
| marker | GPL | 可选子集 50 份 | 53.9 s/页太慢，全量不跑 |
| MinerU | AGPL | 可选子集 50 份 | 中文学术强，值得看 |

各引擎统一包装成 `bench/engines/<name>.py: run(path, out_dir) -> {md_path, json_path|None, wall_s}`；
每引擎独立虚拟环境（`bench/envs/`，.gitignore），子进程调用，互不污染。

## 5. 跑批设计（`bench/run.py`）

- 输入 manifest，输出 `bench/results/<engine>/<id>.jsonl`（一文档一行：全部指标 + 状态 + 错误摘要）
- **可断点续跑**（已有结果跳过）、每文档子进程 + 超时、崩溃不中断整批
- 并发：本机 CPU 核数 − 2 个进程；OCR/docling 单独串行（内存大）
- 日志逐行 flush 到 `bench/logs/`，`--sample N` 只跑前 N 份用于估时
- `bench/report.py`：汇总 → `RESULTS.md`（总表 + 按格式/语言/领域分表 + 失败清单 + 6 张图）

## 6. 耗时估算与开始时刻（**启动前必须先 `--sample 20` 实测再定**）

粗估（CPU，M 系列 Mac）：AImorsel ~2 s/文档 · pdfplumber ~1 s · markitdown ~1 s ·
pymupdf4llm ~1 s · docling ~30–60 s · OCR 图片 ~6 s/页 × 140 份图片/扫描件。
600 文档 × 5 引擎 ≈ 600×(2+1+1+1+45) ≈ 30,000 s ≈ **8.3 h**，加 OCR ≈ 1 h，
加 marker/MinerU 子集 ≈ 1.5 h → **最坏约 11 h**。

⇒ 按「开始时刻 + 最坏耗时 < 下一个不可打扰窗口」：**建议 21:00 前开始**（次日 08:00 前结束），
不建议凌晨 01:00 起跑（会跑到中午）。docling 单独一条队列先跑，其余引擎 1 h 内完事。
无人值守前确认：Mac 不休眠（`caffeinate -i`）、OCR 服务已起、磁盘余量 ≥ 20 GB、
断网校验只在一次短跑里做（不然 fetch 也断）。

## 7. 执行阶段（新 session 照此顺序）

- [x] **A 语料**（2026-08-17 夜完成）：13 个采集脚本 + `merge_manifest.py` 校验合并 →
      **731 份**（真实 542 / 12 个来源 + 合成 189），全部 ≤8 MB ≤30 页、sha256 可校验、幂等可续跑。
      实际分布见下方「执行记录」，缺口是阿语 18/50、俄语 27/30、pptx 与 xlsx 只有合成件
- [~] **B 真值**：✅ 合成集生成器 `make_synthetic.py`（189 份 A 档真值）；✅ `truth/{from_html,from_latex,from_xml}.py`
      + `make_truth.py` 驱动 + 17 项单测（2026-08-17 夜）；✅ `make_images.py` 自造图片/扫描集 100 份；
      ⬜ **DocLayNet 转换仍未做**（磁盘不够，见「待决策」1）
- [x] **C 指标 + 引擎包装**（2026-08-17 白天完成）：`metrics.py` + 9 项单测；`engines/{aimorsel,pdfplumber_txt,markitdown,pymupdf4llm,docling}.py`；
      `run.py`（续跑/超时/并发/串行队列/--sample/--filter/--force）；`report.py`（总表 + 公平对比 + 两两对比 + 分维度 + 失败清单 + 图）；
      合成集 9 份 × 5 引擎冒烟通过
- [x] **D 全量跑批**（2026-08-17 23:15 → 08-18 02:27，含两波）：731 份 × 5 引擎 = **3224 条**，
      aimorsel 727/731 ok（99.5%）。见「执行记录」
- [x] **E 报告**：✅ `RESULTS.md`（含「读这份表之前」口径说明）+ **60 份人工抽检 `SPOTCHECK.md`**；
      ✅ 结论已回流（2026-08-19，#0–#14 全部处理完、全量重跑定稿之后）：README.md「评测：和别的工具比，
      处在什么位置」+ README.en.md「Benchmark: where we stand」+ 官网首页 `#bench`（中英各一份）。
      **跨引擎数字一律用逐对交集表**（docling 只跑 300 份分层子集，总表均值不可直接比）；
      77 份全员交集那张表照实写出（docling 0.924 / pymupdf4llm 0.918 > 我们 0.824）。
      重跑评测后 README×2 + 中英首页四处要一起改（已写进 `website/DEPLOY.md` 一致性检查）
- [~] **F 回流**：✅ `bench/ISSUES-draft.md` **15 条草稿**（待用户过目后建 issue）；⬜ 阈值型指标进 CI。原文：阈值型指标（如 compat_residual=0、fail 率 < 1%）加进 CI 的小样本回归（`tests/test_bench_smoke.py`，只跑 10 份入库 fixture）

## 执行记录

（执行时追加：日期 / 阶段 / 实际数字 / 偏离规划之处）

- **2026-08-17 白天 · C 完成 + B 合成集**：白天在主 session 做完了不需要无人值守的部分（指标/引擎/跑批/报告/合成语料），
  夜间 session 只剩 A（真实语料采集）→ B 剩余解析器 → D 全量 → E。
  冒烟实测（M 系列 Mac，9 份合成文档）：aimorsel 0.6 s/PDF、pdfplumber 0.07 s、markitdown 0.4–0.7 s、pymupdf4llm 0.8 s、
  **docling 10–17 s/文档、峰值 1.2 GB**（含图片 OCR）；§6 估算基本成立。
  **冒烟已暴露的 AImorsel 问题（回流 F 待建 issue）**：
  ① 中文 PDF 换行处被塞空格（"看 起来"），指标 `cjk_inner_spaces` 专门计数，可在 `_post_process` 前做 CJK 间空格清理；
  ② 合成 PDF 里「表格→列表」的阅读顺序被引擎倒成「列表→表格」（pymupdf4llm/pdfplumber 顺序正确）；
  ③ 列表项渲染成 `- • xxx` 双项目符号；
  ④ ~~HTML 输入不支持~~ → **已解决（2026-08-17 用户拍板加）**：`format_adapters._parse_html`（纯 stdlib html.parser）
     归一化到结构树，全入口接线，4 项单测；夜跑 HTML 一列会有 aimorsel 分数；
  ⑤ 图片在 OCR 服务离线时输出空文本但状态 ok（评测里应算 fail 更诚实——夜跑必须先起 OCR 服务）。
  **2026-08-17 下午追加**：HTML 适配器落地后重跑合成 HTML 子集（21 份）：aimorsel 全 ok，
  heading_f1 / cell_f1 / order_tau 均 1.0；`norm_text` 顺手补了「表格分隔行 + 行首项目符号/编号」剥离
  （原先 `| --- |` 与 `- ` 被算进正文差异，所有引擎都吃亏，HTML 子集 char_sim 0.95→1.0）。
  docling 侧：默认 EasyOCR 只开英文，中文图片 sim 0.12；包装已按 manifest 的 lang 传 `BENCH_DOC_LANG` 配语言后升到 0.3–0.9。

### 2026-08-17 夜跑 · D 第一波起跑（合成 189）

**编排决定**（用户 22:30 拍板）：夜跑开始时已 22:25，过了「20:30 未完成 A 就先起跑」这条线，
因此**不等 A**：立刻用合成 189 份起 D 第一波，A 采集与 B 解析器**并行**做（4 个子 agent），
真实语料入 manifest 后重跑同一条命令续跑第二波。**docling 只跑分层子集 300 份**
（全部合成 189 + 真实里按格式×语言分层随机约 110），理由：真实件预计 30–180 s/份，全量跑不完；
`report.py` 已有「所有引擎都成功的交集」公平对比表，子集不会让对比失真，但**报告里必须标注
docling 的样本量与抽样方式**。

**起跑前实测估时（`--sample 20 --tag est`，100 任务 3.4 min）**，均值 s/份：

| 引擎 | 均值 | pdf | docx | pptx | xlsx | html | png | jpg | tiff |
|---|---|---|---|---|---|---|---|---|---|
| aimorsel | 0.6 | 0.8 | 0.1 | 0.1 | 0.2 | 0.0 | 1.2 | 1.2 | 1.1 |
| pdfplumber_txt | 0.0 | 0.1 | — 不支持 | | | | | | |
| markitdown | 0.7 | 1.1 | 1.2 | 1.0 | 1.0 | 1.0 | 不支持 | | |
| pymupdf4llm | 0.6 | 2.2 | 不支持 | | | | | | |
| **docling** | **9.0** | 11.9 | 3.5 | 3.2 | 3.2 | 3.3 | 13.6 | 13.6 | 13.7 |

外推：合成 189 份 → docling 串行 ≈ **27 min**，其余 4 引擎并发 ≈ 4 min，
aimorsel 图片 OCR 分语言批 ≈ 20–35 min（含 7 次换语言起服务、新语言要下 EasyOCR 模型）
→ **第一波约 50–60 min**。真实语料第二波（≈400 份，docling 只跑 110）预估 2.5–3.5 h。
起跑 23:20 + 最坏 5 h → **04:30 前结束**，远早于 09:00 这条线。

**三波编排（`bench/nightrun.sh`，可 Ctrl-C，重跑同一命令自动续）**：
1a 不需要 OCR 的格式 × 全部引擎 → 1b 图片/扫描件 × 非 aimorsel 引擎 →
1c 图片/扫描件 × aimorsel，**按语言分批换 OCR 服务**（`bench/run_ocr_batches.py`：
每种语言在 5011 端口起一个临时 hybrid 服务、`BENCH_OCR_URL` 指过去、跑完关掉，
**不动用户常驻的 5002 服务**）。新增 manifest 字段约定：**扫描/图片化 PDF 的 `format` 记
`scan-pdf`**（这样 OCR 波能靠 format 选出来，报告也能单独切片）。

**起跑前抓到的真 bug（F 待建 issue，已确认可复现）**：
`--sample 20` 里 aimorsel 的 6 份图片全是 `status=ok / used_ocr=False / char_sim=0.0`，
note 写「OCR 服务未启动」——**而服务明明在线**。根因：`DEFAULT_HYBRID_URL = "http://localhost:5002"`，
本机 `urllib` 请求 `http://localhost:5002/health` 拿到 **502**（走了系统代理/先解析到 ::1），
换 `http://127.0.0.1:5002/health` 立刻 200；`curl localhost` 反而正常，所以肉眼排查很容易被骗过。
影响：**任何配了系统 HTTP 代理的机器上 OCR 会静默失效**，用户只看到「图片转出来是空的」。
建议改法：默认地址改 `127.0.0.1`，且 `check_ocr_server` 与探测请求显式
`build_opener(ProxyHandler({}))` 绕过代理。这条同时验证了原有第 ⑤ 条（离线时空输出仍算 ok）
在评测里必须算 fail —— 否则我们自家引擎的图片列会拿一片 0.0 分而毫无告警。
（bench 侧的规避：`nightrun.sh` 统一 `export BENCH_OCR_URL=http://127.0.0.1:5002`，
`engines/aimorsel.py` 读该环境变量覆盖 `hybrid_url`；那 6 条脏结果已删除，交 1c 重跑。）

**本轮对 bench 基础设施的改动**：`run.py --filter` 支持 `|` 取或（`format=png|jpg|tiff`）；
`engines/aimorsel.py` 认 `BENCH_OCR_URL`；新增 `run_ocr_batches.py`、`nightrun.sh`；
白天冒烟的 74 行旧结果（其间 `norm_text` 改过、docling 语言参数中途才加，口径不一致）
已归档到 `results/smoke-archive/`，重新干净跑。py312 补装了 `matplotlib`（report.py 出图用）。

### 2026-08-18 00:00 · A/B 完成，D 第二波在跑

**第一波结果（合成 189 × 5 引擎 = 945 条，23:15→23:48，33 min）**：aimorsel **189/189 ok，
是唯一吃下 8 种格式的引擎**；docling 189 ok（1 份模型下载被采集流量挤断，`retry_failed` 后转 ok）；
markitdown 126 ok + 63 unsupported（图片全不支持）；pdfplumber_txt / pymupdf4llm 各 42 ok + 147 unsupported（只吃 PDF）。
**公平对比只剩 42 份**（五引擎都成功的交集 = 只有 PDF），在这 42 份上：

| 引擎 | char_sim | heading_f1 | cell_f1 | 秒/文档 | 峰值 MB(p95) |
|---|---|---|---|---|---|
| aimorsel | **0.840** | 1.000 | 1.000 | 0.74 | 106 |
| docling | 1.000 | 1.000 | 1.000 | 10.7 | 1162 |
| pymupdf4llm | 1.000 | 0.997 | 1.000 | 1.73 | 425 |
| markitdown | 1.000 | 0.000 | 0.000 | 0.73 | 142 |
| pdfplumber_txt | 1.000 | 0.000 | 0.000 | 0.09 | 33 |

⇒ **结构与 docling 并列第一且快 14 倍、内存低 11 倍，但纯文本保真 0.840 输给连结构都不做的 pdfplumber。**
`cjk_inner_spaces` 在这 42 份上我们与 docling 同为 64，**所以不是**已知第 ① 条 CJK 空格在扣分，
更可能是第 ②（表格/列表顺序倒置）与第 ③（`- •` 双项目符号）——两者都直接扣字符级相似度。
**这是「修一个 bug 换一个名次」的量级，F 阶段列为最高优先。**

**A 阶段最终语料 639 份（真实 450 + 合成 189）**，`merge_manifest` 校验 0 错误：

| 维度 | 实际 | PLAN §1.1 目标 | 差 |
|---|---|---|---|
| 格式 | pdf 201 · html 172 · png 66 · jpg 41 · tiff 41 · scan-pdf 40 · docx 36 · pptx 21 · xlsx 21 | 见 §1.1 | pptx/xlsx **只有合成件** |
| 语言 | en 220 · zh 127 · de 86 · es 67 · fr 58 · ja 56 · ru 17+ · **ar 8+** | ar 50 · ru 30 | **阿语缺口最大**（UN 那批已补 ar/ru 各 5，World Bank 在采） |
| 领域 | law 205 · business 129 · it 113 · math 90 · edu 44 · gov 42 · med 8 · news 8 | 每领域 ≥50 | med/news 偏少 |
| 真值 | A 586 · C 53 | — | A 档 92% |

来源实际份数：synthetic 189 · rendered 100 · eurlex 64 · wikipedia 64 · arxiv 44 · rfc 40 ·
egov_jp 30 · govcn 30 · un 30 · funsd 25 · cninfo 20 · sec 20 · gesetze_de 15。

**采集期间的四个硬发现（都写进了各自 fetcher 的注释）**：
1. **EUR-Lex 的 202 是 AWS WAF 的 JS 挑战页**，浏览器 UA/cookie/退避重试全无效；改走出版局
   **CELLAR 内容协商**（`publications.europa.eu/resource/celex/{CELEX}` + `Accept: application/pdf`
   或 `application/xhtml+xml`）彻底绕开。
2. **arXiv OAI 换地址了**：`export.arxiv.org/oai2` **301 到 `oaipmh.arxiv.org/oai`** 且 301 响应体 0 字节
   ——不跟随重定向会静默拿到空结果。首字节要等约 90 s，Fastly 先回 `503 first byte timeout`（`Retry-After: 0`）。
   另：arXiv 允许「PDF only」投稿，e-print 端点直接回 PDF，这种无 LaTeX 源，已识别并降为 C 档。
3. **e-Gov 不提供法令 PDF**（`file_type` 只接受 xml/json/html/docx/rtf），`laws.e-gov.go.jp/law/<id>`
   只是 800 字节 SPA 外壳不能当语料 → 改收 docx 15 + 官方 HTML 15，顺带补上 docx 与 ja 两个缺口。
4. **SEC**：带 URL 括号的 UA 直接 403；Archives 对大文档不返回 Content-Length（分块传输，
   HEAD 与 `Range: bytes=0-0` 都问不出体积），只能读到上限即弃。

**关于「兼容码位」这条差异化能力的重要更正（原计划的测点全部落空，但另有替代）**：
- 中国政府网 30 份 + 巨潮 20 份中文 PDF 的 producer 全是 **WPS 23 / PDFlib 4 / Adobe 3**，
  **没有一份 Quartz 或方正**；四个兼容区正则扫描 **命中 0 处**。又加扫 cninfo 70 份中文季报找
  Quartz producer，**也是 0**。⇒ **中国政府网与 A 股披露这两条链路采不到兼容码位测点。**
  另：`gov.cn/gongbao/` 全是 HTML 无 PDF 版，另试六个省级政府公报页也都无直链。
- 我试过用 macOS `cupsfilter`（Quartz PDFContext）自造测点，**结论是否**：源文本先断言 0 处兼容码位，
  转出后仍是 0 处 —— **CUPS 的 texttopdf 这条路复现不出该缺陷**（用户当初那份是 Cocoa 应用
  「打印成 PDF」的 PingFang **Type 3 子集**，路径不同）。已撤掉该脚本与语料，别再走这条路。
- **替代测点已自然存在**：arXiv 的 Type1 字体带 **ﬁ/ﬂ 连字（U+FB00–FB04）**，跑批日志里
  aimorsel 报「修正 184 处兼容码位」，rendered 那批也有 11 份共 186 处。**同一个功能、同一个
  `compat_residual` 指标**，只是命中的是连字区而非康熙部首区。E 阶段要专门切一张
  「arXiv 子集上各引擎的 compat_residual」——预期只有 aimorsel 为 0，这是可发表的差异化数字。

**arXiv 那 42–44 份的 char_sim 会很低（实测 0.38–0.54），但不能当文本保真度读**：
B 阶段为了不让所有引擎一起虚低，把公式统一剥成空格（单篇实测剥掉 1356 处），
而引擎会把公式当文本输出 → 「多输出的部分」被算成差异。**E 阶段必须把 `domain=math`
单独切表并标注这个口径**，否则总表被它拖歪。

**B 阶段交付**：`bench/truth/{from_html,from_latex,from_xml}.py` + `bench/make_truth.py`，
26 项单测全绿（原有 9 项 metrics + 新增 17），项目原有 75 项 pytest 照旧全绿。
真值 270 份生成成功。三处取舍见上（公式剥离）与下：
- **无语义标题的文档「省略 `headings` 键」而不是写空数组**——`score_document` 用
  `"headings" in truth` 判断要不要评这项，写空数组会让抓对标题的引擎 heading_f1 = 0（惩罚正确行为）。
  20 份 SEC 10-K 与 64 份 EUR-Lex 全部走这条路。
- **EUR-Lex 把每条 recital 包进单行两列 `<table>`**（64 份共 4754 个），留在真值 `tables` 里会让
  从 PDF 转的引擎 cell_f1 直接归零（同一真值被 `_html` 与 `_pdf` 两行共用）→ 单行表与
  「短编号 + 长正文」两列表**降级成段落**，SEC 仍保留中位 102 张真表格。
- 独立交叉验证：用 `from_html` 解合成 HTML，与 `make_synthetic.py` 独立生成的真值比
  **char_sim = 1.0000、cell_f1 = 1.000**。

**踩到的两个坑（已修，勿重犯）**：
① 采集 agent 还在下载时就合并 manifest，收进截断的 arXiv PDF 一跑就 fail →
`merge_manifest --verify-sha` 重算 sha256 剔掉没下完的，**收尾必跑**；
② `run.py` 的 `missing` 分支早返回**不打日志**，我用错 `--manifest`（指向分片导致相对路径解析到错目录）
时看到「完成 3 任务」却没有任何结果行，白排查了一轮 —— 已补日志。

### 2026-08-18 00:20 · 第二波跑出的第一个真 bug（F 待建 issue，已精确定位）

**4 份真实 arXiv PDF 我们完全失败，而 pymupdf 读得好好的**
（`arxiv_2107_10128` / `arxiv_2405_09619` / `arxiv_2410_22486` / `arxiv_2501_01459`，占 PDF 语料 1.6%）。
排查过程：先怀疑文件截断 → `merge_manifest --verify-sha` 全部通过、`%PDF-1.4/1.5` 头正常、
253 份 PDF 用 pymupdf 逐份打开**全部 0 失败**（这 4 份首页能读出 1259–4380 字）。
再逐库对照：

| 库 | 结果 |
|---|---|
| pymupdf | ok（有 `FT_New_Memory_Face … unknown file format` / `object is not a stream` 告警但能恢复） |
| pdfplumber (pdfminer) | **拒收**：`Unexpected EOF` ×3、`No /Root object!` ×1 |

⇒ 根因不是文件坏，是**交叉引用表结构不规范**，mupdf 会自愈、pdfminer 更严格直接拒。

**01:03 更正（结论变了，别引用上一版说法）**：后续跑批显示这 4 份**同样让
pdfplumber_txt（PdfminerException）、markitdown（FileConversionException）、docling（ConversionError）失败**，
**只有 pymupdf4llm 成功**（它就是 mupdf）。所以这不是 AImorsel 独有的缺陷，而是
**整个 pdfminer 家族 + docling 都栽在同一处，mupdf 是唯一能恢复的**。
对我们的结论因此变成：失败率数字上我们与两个竞品并列（不落后），
但**「Java 引擎 + pdfplumber」这张兜底网在这个失效模式上等于只有一层**——
两层用的都是严格解析器。改法与许可考量同下。
**关键教训：pdfplumber 兜底网对这一类根本无效** —— 我们的两层（Java 引擎 + pdfplumber）
在「结构损坏」这个失效模式上不是互补的，pdfminer 比 Java 引擎**更**严格。
建议改法（**注意许可**）：pymupdf 是 AGPL，**不能**直接引入 Apache-2.0 的核心；
可选 ① 用 `pikepdf`/`qpdf`（MPL-2.0，许可兼容）先修 xref 再重试原有两条路；
② 加 `pypdf`（BSD，纯 Python）作第三层兜底 —— 两者都**未验证**（本机都没装，
按约束没擅自安装），建 issue 时写成「待验证的候选方案」。

### 待决策（留给用户，不阻塞夜跑）

1. **DocLayNet（B 档版面真值）今晚不做**：正式集约 28 GB，本机磁盘只剩 25 GB。
   要么清盘再补，要么改用 HF 上的小子集（只取 ~100 页），要么放弃版面真值那一档指标
   （`heading_*`/`order_tau` 仍有 A 档真值可算，只是少了人工版面标注这一路交叉验证）。
2. **真实 pptx 语料**：公开可下的真实 pptx 极少，可能只能靠合成集的 21 份。
   要不要接受「pptx 一列只有合成语料」这个口径写进报告。
3. **marker / MinerU 子集 50 份**（PLAN §4 的可选项）：两者都要装几个 GB 依赖，
   按约束未擅自安装。要跑就得先拍板装。
4. **兼容码位的中文测点缺失**（见上）：要验证「康熙部首」那条主路径，需要用户手上**当初那份
   macOS Quartz + PingFang Type 3 子集的 3.5 万字中文 PDF**（或用 Preview/TextEdit「打印成 PDF」
   现做一份）。本机 `cupsfilter` 复现不出。当前只有 arXiv 的 ﬁ/ﬂ 连字这一路替代测点。
5. **`bench/corpus/` 整体被 gitignore，manifest.jsonl 与 manifest.d/ 跟着不入库**，
   与 PLAN §1「只放 manifest + 下载脚本 + 结果表」的本意冲突。建议把 manifest 挪到
   `bench/manifest.jsonl`（语料仍不入库），或在 `bench/.gitignore` 里反向放行。
   按约束我没有 `git add -f`，也没改 .gitignore。
6. **`bench/fetchers/` 下有两个功能重叠的公用模块** `common.py` 与 `_common.py`
   （两个 agent 并行各写了一个，分别被 3 个 / 6 个 fetcher 依赖）。合并是纯清理活，留待白天。

### 续跑与接手（明早新 session 直接照抄）

```bash
cd <仓库根目录>
# 续跑（幂等，已有结果自动跳过）
BENCH_PY=<python 解释器路径> bash bench/nightrun.sh full
# 看进度
tail -30 bench/logs/full.log ; grep -cE "fail|timeout" bench/logs/full.log
wc -l bench/results/*.jsonl
# 出报告
python -m bench.report
```


### 2026-08-18 02:27–04:00 · D 收尾 + E 完成 + F 草稿

**D 最终结果**（731 份 × 5 引擎 = 3224 条，覆盖度 100% 无缺口）

| 引擎 | n | ok | 不支持 | fail |
|---|---|---|---|---|
| **aimorsel** | 731 | **727（99.5%）** | 0 | 4 |
| docling（300 份分层子集） | 300 | 299 | 0 | 1 |
| markitdown | 731 | 539 | 188 | 4 |
| pymupdf4llm | 731 | 293 | 438 | 0 |
| pdfplumber_txt | 731 | 289 | 438 | 4 |

公平对比（77 份五引擎都成功）：aimorsel char_sim 0.823 / heading_f1 0.965 / cell_f1 0.942 /
0.86 s / 398 MB；docling 0.924 / 0.980 / 0.944 / 11.2 s / 1352 MB；pymupdf4llm 0.918 / 0.976 / 0.984 /
2.1 s / 545 MB；markitdown 与 pdfplumber 的 heading_f1 均为 0。
⇒ **结构第一梯队、速度比 docling 快 13 倍内存 1/5，文本保真列第三。**

**wave 编排实测**：2a 快引擎 × 503 份非 OCR 格式 13 min；2b docling 非 OCR 子集 7 min；
2c 快引擎 × 图片（判 unsupported）1 min；2d docling × 图片子集 21 min；
2e aimorsel × 165 份图片/扫描件按 7 个语言批换 OCR 服务 57 min。**没有 ar 批**——
阿语语料全是文字层 PDF，没有阿语图片/扫描件（语料缺口，非失败）。

**E：60 份人工抽检 → `bench/SPOTCHECK.md`**（放 bench/ 而非 results/，因为后者整个被 gitignore）。
抽检最大的价值不是分数，而是**三条自动指标完全看不见的缺陷**：
① **阿语 PDF 输出视觉序、未做 bidi 还原**（逻辑序 0 次 / 反转序 65 次，docling 与 pymupdf4llm 都正确）——
本轮最严重，性质与当年「康熙部首」事故相同；② 财报扫描件数字静默错认（`4`→`1`、负号丢失）；
③ HTML 路径把行内数学公式整块丢掉。以及**四类「分数低但引擎是对的」假信号**（详见该文件）。

**抽检期间发现并修掉两个指标 bug，已用 `bench/rescore.py` 重算全部 3224 条**
（拿已落盘的产物重算，不重跑引擎，几分钟）：
1. `norm_text` 的 `<[^>]+>` 把物理正文 `q < qc … T > Tc` 之间整段吃掉（一份 arXiv 少 39044 字）；
2. `cjk_inner_spaces` 先把换行折成空格 → **每个段落/标题边界都误计一次**，系统性偏向
   「输出结构更少」的引擎。**修正后「PDF 上比 pymupdf 多 8.5 倍 CJK 空格」这个结论直接消失**
   （原生 PDF 72 份共 22 处，pymupdf 22、pdfplumber 21，持平）。
   ⇒ **教训：任何「我们比对手差 N 倍」的结论，先去看指标实现。**

**另一处口径**：pymupdf4llm 在无文字层 PDF 上会**自动调 Tesseract OCR**（本机装了 tesseract，
它自己打印 `Using Tesseract for OCR processing`），所以它 scan-pdf 那一列是 OCR 成绩、
且换台机器数字会变。已写进 `RESULTS.md` 的口径说明。

**F：`bench/ISSUES-draft.md` 共 15 条草稿**（#0 RTL bidi 最高优先 → #14 性能方差），
每条带证据、复现命令与改法建议，**待用户过目后再建 issue**。附一节「bench 自身要修的」
（真值侧 4 处问题 + 已修的 2 个指标 bug），免得下次把它们当成引擎缺陷。

**下一步（明早）**：① 用户过 issue 草稿 → 建 issue；② 修 #0/#1/#2 后**重跑受影响子集**
再更新 README 与官网结论（当前数字会变，别急着上站）；③ 剩余可选项：DocLayNet、
marker/MinerU 子集、阿语图片语料、真实 pptx/xlsx。

### 2026-08-18 18:30 · #0 已修：RTL 视觉序还原落地，阿语子集重跑

- 修法与要点见 `CHANGELOG.md`（Batch 1）；代码 `rtl_text.py` + `morsel.restore_rtl_products()`；
  ISSUES-draft #0 顶部已标「已修」。
- 指标：新增 `rtl_visual_ratio`（metrics.py，run/rescore 都写入），报告新列「RTL视觉序(份)」
  = 判成视觉序的份数 / 有 RTL 证据的份数。
- 重跑：`bench.run --engines aimorsel --filter format=pdf,lang=ar --force`（10 份，0.2 分钟，
  每份多一遍 keep_line_breaks 转换，耗时约翻倍），随后 `bench.rescore` 全量重算 + `bench.report`。
- 结果（RESULTS.md 已更新）：**aimorsel 0/25、pymupdf4llm 0/10、docling 0/6、markitdown 10/25、
  pdfplumber_txt 10/10**——修前 aimorsel 是 10/25（10 份阿语 PDF 全中）。其余指标不变
  （文本指标对字符顺序不敏感，这正是 #0 差点漏掉的原因）。
- 手工核对：`un_a_res_77_1_ar` 中 "المتحدة" 逻辑序/反转序 = 11 段落级出现/0；括号方向对照
  渲染页面确认（`(أ)`、`[…(A/77/L.3)]`）；世行表格单元格多段顺序正确。
- 仍未修（同一批发现）：lam-alef 连字（"الأمم"→"األمم"，pymupdf4llm 同样）；单字母碎片标题（#9）。

### 2026-08-18 20:40 · #2 / #4 已修

- #2：`_convert_office()` 接 `normalize_produced_files()`（`_post_process` 之前）。重跑
  `bench.run --engines aimorsel --ids sec_ibm_10k_20260224 --force` → compat_residual 4 → 0；
  `bench.report` 后 aimorsel「兼容码位残留」列全部归 0（其余指标不变）。
- #4：`DEFAULT_HYBRID_URL` → `127.0.0.1`，`check_ocr_server` 绕过代理。bench 数字不变（跑批脚本早已用
  `BENCH_OCR_URL` 绕过），不重跑。#5「空输出仍报成功」仍开着。
- pytest 114 项全过。

### 2026-08-18 21:05 · #5 已修

- 图片输入产物无文字 → `degraded`（不再报「成功」），note 写原因；清单记 `needs_ocr`（+`ocr_attempts`），OCR 服务上线后
  重跑/监听自动补转（最多试 2 次），离线时不重复跑；CLI/GUI/Web/MCP 四入口都透出。改法与边界见 ISSUES-draft #5
  顶部「已修」块、`README.md`「扫描件与 OCR」节。
- 提交前 adversarial verify（13 个 agent）确认 5 条并已修：html-only `<title>` 计入文字、旧产物误导判空、
  OCR 后端失败不补转、监听线程被 BadStatusLine 带死、Web/MCP 未透出降级。
- 真实引擎验证：OCR 离线下转一张 png → 日志 `△`、report「Degraded」、`.done.json` 带 `needs_ocr: true`、md 只有一张图引用。
- bench 结果未重跑（图片子集当时 OCR 在线，状态口径变化只影响「OCR 认不出字」的图，不影响质量指标）。
- pytest 122 项全过（+8）。

### 2026-08-18 21:30 · #1 / #3 已修

- #1：HTML 适配器不再整块丢 `<math>`，公式以 `$LaTeX$` / `$$…$$` 保留（annotation > alttext > 词元；
  回退图 alt 去重）。重跑 `bench.run --engines aimorsel --filter format=html --force`（172 份，1.8 分钟）：
  CJK HTML「汉字 空格 汉字」3960→3778，残留是导航框/信息框链接列表拼格（源码里就有的空白，另一机制）。
  HTML char_sim 0.828→0.825——**真值仍剥公式**，我们现在多出的公式被当成多余字符；
  **待决**：`truth/from_html.py` 是否改为保留 annotation LaTeX（改了要全引擎 rescore，markitdown/docling 会掉）。
- #3：引擎拒收 → `pikepdf`（qpdf）修复副本 → 再喂引擎（完整结构树，不算降级）→ 仍失败才 pdfplumber。
  根因订正为「无 %%EOF 的截断下载」。重跑 4 份 arXiv 全 ok，aimorsel 失败 4→0、成功率 100%；
  `bench.rescore` + `bench.report` 已跑，RESULTS.md 更新。新增可选依赖 `pikepdf>=8`（requirements +
  PyInstaller hiddenimports）。
- 提交前 adversarial verify（15 个 agent）确认 9 条独立缺陷并已修（HTML 5：去重误伤、KaTeX/MathJax 重复、
  `_clean_tex` 贪婪、`$$` 劈断标题/列表、未闭合 math 吞后文；PDF 4：无差别修复、修复后空产物记成功、
  空消息异常当成功、无产物分支丢 note）；1 条驳回（未闭合 math 属既有行为——但顺手也修了）。
- pytest 133 项全过（+11）。


## 2026-08-18 22:00 · #7–#14 修复排期（已由用户拍板，新 session 按此执行）

判据：只有落在 Python 层（适配器 / 图片预处理 / 结构树后处理）的先动；落在 Java 引擎或 OCR 模型的先定位或只写文档。
每批一个 commit；触碰 morsel.py / format_adapters.py 且 diff >100 行 → 提交前跑 adversarial verify（常设授权）。

| 批 | issue | 修在哪 | 做法 | 回归 |
|---|---|---|---|---|
| 1 | #11 XBRL 隐藏元数据 + #12 无样式文档零标题 | `format_adapters.py` | #11：`ix:hidden` / `display:none` 并入现有 skip 机制（按触发标签名计深度）。#12：保守启发式——整篇零标题且「短行+编号模式」（第N条/第N章/Article N/§ N）≥3 处才提升，只提升匹配行 | `--filter format=html`，重点 egov_jp / sec_vz |
| 2 | #8 旋转图腰斩 + #14 单图 167 s 定位 | `_convert_image` | #8：Pillow 投影法 deskew（灰度→±5° 步进 0.5°→水平投影方差最大角度），角度 ≥0.5° 才旋转，正常图零改动。#14：通道内加计时日志跑 funsd_82200067_0069，查清是重采样/重试/后端排队再决定 | cninfo 300513/300817 tiff、funsd 两张 |
| 3 | #9 标题噪声 + #10 假列表 | 新 `_tidy_structure()` 挂 `_post_process` 之前 | 单字符/全大写关键词/文件名样标题降为段落；标题超 N 词且含句末标点则首句切回正文；`(N)` 开头且段落长度的 list item 改回 paragraph | **全量 731 份**对比 heading_f1 / char_sim，任一规则拉低整体 heading_f1 即撤掉 |
| 4 | #13 数字错认 + #7 OCR 表格读序定位 | README + bench | #13：README「已知限制」写「扫描财报数字请勿直接采信」，bench 加数字串一致性指标。#7：起 hybrid 服务跑 syn 三张图，对照后端原始 JSON 与最终 Markdown：后端对、最终错 → 上游 issue；我们的拍平有份 → 拉回第 3 批 | syn_de_business_jpg / syn_en_law_jpg / syn_fr_it_png |

第 3 批完成时**一并 bump 断点续传签名**（#0–#5 与本轮全部修复至今都没 bump，老产物永远不刷新）。

### 2026-08-18 23:10 · 第 1 批（#11 / #12）已修

- #11：`ix:header`/`ix:hidden` 与行内 `display:none`/`visibility:hidden` 并入 HTML skip 机制；顺带修掉两个
  「skip 关不掉→吞掉整篇后文」的隐患（void 元素、可省略的结束标签）。#12：`_promote_numbered_headings()`
  给零标题文档做保守的编号行提升（多语言编号，页眉去重）。改法与边界见 ISSUES-draft #11/#12 顶部「已修」块。
- 重跑 `--filter format=html`（172 份 0.6 min）+ egov docx 15 份 → `bench.report`。
  **HTML：char_sim 0.825→0.856、heading_f1 0.823→0.839；docx heading_f1 0.583→0.637；
  总表 char_sim 0.771→0.780、heading_f1 0.770→0.780。** 无任何指标下降。
- 提交前 adversarial verify（15 个 agent，3 维 × 逐条反驳）：确认 5 条并全部修掉——void 元素吞后文、
  可省略结束标签吞后文、`display:none !important` 漏判、编号模式只认英文（EUR-Lex de/es 零提升）、
  法语 "Article premier" 漏掉、MSFT 10-K 把页眉 "PART III" 提成标题（改为光秃编号出现 2 次即判页眉）；
  驳回 4 条（维基旗帜排序键与 McDonald's 藏在 `ix:hidden` 里的附注属**正确**丢弃，与真值口径一致；
  签名未 bump 属计划内，第 3 批统一处理；「#12 没覆盖 SEC」不是缺陷）。
- pytest 137 项全过（+4）。

### 2026-08-19 · 第 2、3 批（#8 / #14 / #9 / #10）已修

**#8 图片倾斜校正**：`format_adapters.detect_skew/deskew_image`，投影法（纯 Pillow），±5° 内粗搜+细搜，
阈值 **1.0°**（实测定的：0.5–0.8° 的真实扫描件转正后 char_sim 全部下降，1.4° 以上一致大涨）。
A/B 对照（12 份 rotated 语料，只切 deskew 开关）：**char_sim 0.333 → 0.530（+59%），11/12 提升**。
188 份图片语料只有 39 份超阈值且全是真倾斜（含 21 份合成时就按 1.5° 生成的 `syn_*_skew.tiff`），零假阳性。

**#14 单图 167 s**：定位为**跑批 harness 的并发设置**，不是转换缺陷——`run_ocr_batches` 传 `--jobs 2`，
两份文档抢同一个单实例 CPU-bound OCR 服务；同一文件单跑 13.1 s、jobs=2 跑 33.4 s，而 4 份文件
jobs=1 与 jobs=2 **墙钟时间相同（约 65 s）**。已改 jobs=1（+`--force` 透传）。加 `MORSEL_DEBUG_TIMING=1` 分段计时。

**#9/#10 结构噪声清理**：`morsel.tidy_products()`，PDF 与 office/HTML 两条路径都接，在 `_post_process` 之前。
#9 只做「误升为标题」（单字符非 CJK / RFC 2119 关键词 / 文件名样，全语料命中 125 处），
**「标题吞正文」不做**——排期设想的判据（超 N 词 + 句末标点）对实际样本根本不成立，没有可靠信号就不动。
#10 按整个列表判定「(N) …」条款段落（全语料 2,390 行）。
**动手前先量安全性**：降级规则套到全语料 6,632 条真值标题上命中 0 条 → 只可能提高 precision。

**断点续传签名已 bump**（新增 `PIPELINE_VERSION=2` 进签名）——#0–#12 的修复此前都不会让旧产物失效。

**全量重跑（731 份 × aimorsel，非 OCR 波 503 份 2.8 min + 按语言 7 批 OCR 228 份 46 min，
01:24 结束，731/731 成功、0 失败 0 超时）**：

| 切面 | char_sim | CER | heading_f1 | cell_f1 |
|---|---|---|---|---|
| **总表 731 份** | 0.780 → **0.792** | 0.288 → **0.274** | 0.780 → **0.786** | 0.598 → **0.604** |
| tiff 41 份（deskew 主战场） | 0.601 → **0.754** | 0.499 → **0.320** | 0.813 → **0.871** | – |
| jpg 81 份 | 0.736 → **0.750** | 0.324 → 0.309 | – | – |
| pdf 253 份 | 0.780 → 0.781 | – | 0.683 → **0.689** | 0.628 → **0.648** |
| lang=zh 143 份 | 0.687 → **0.722** | 0.335 → 0.297 | 0.933 → 0.922 ↓ | – |
| lang=de 94 份 | 0.874 → 0.881 | 0.180 → 0.171 | 0.834 → **0.853** | 0.693 → 0.708 |
| domain=business 186 份 | 0.666 → **0.687** | 0.382 → 0.358 | 0.885 → **0.914** | – |

**两处小幅回退**：`lang=zh` heading_f1 −0.011、`domain=edu` heading_f1 −0.009。都落在被 deskew 改写了
OCR 输入的图片子集上（转正后识别出的标题文字略有出入），同组 char_sim 分别 +0.035 / +0.023；
**不是 tidy 造成的**——降级规则对全语料 6,632 条真值标题命中 0 条，只可能提高 precision。
按排期的判据（「任一规则拉低整体 heading_f1 即撤掉」）整体 heading_f1 上行，两条规则都保留。

**耗时顺带大降**（不是本次改动的功劳，是 #14 那条 jobs=1）：总表 秒/文档 2.10 → 1.59、
p95 **49.2 → 18.4**、scan-pdf p95 125.4 → 54.6。夜跑那份 p95 是被并发抢核抬上去的。

**提交前 adversarial verify（12 个 agent）确认 5 条并全部修掉**：
① json 降级列表项丢 `kids`（一份德语法条**凭空少 1,143 字正文**，md 里还在——最严重的一条）；
② deskew 异常把「能转的文件」变成「读取图片失败」（1×1 占位图/横幅条裁完中间 80% 就没了）；
③ tidy 没接 office/HTML 路径，而 #9 的样板 `L_2019186EN.01005701.xml` 恰恰出在那条路径；
④ md 降级行不补空行会被当成上一条列表项的续行，块结构比不清理还糟；
⑤ html 嵌套列表用非贪婪正则会把外层开头配到内层结尾。驳回 2 条。

### 2026-08-19 · 第 4 批（#13 / #7）完成 —— ISSUES-draft #0–#14 全部处理完毕

**#13 财报扫描件数字静默错认**：改不动 OCR 模型，做两件事——写进文档 + 量出来。
- README 新增「⚠️ 已知限制：扫描件里的数字不可直接采信」（四组真实错值样本 + 一条常见问题指路）。
- `bench/metrics.py` 加 `numbers()` / `digit_stats()`：多重集 P/R/F1，**`1 - 数字准` = 凭空造出来的错值占比**。
  归一化按英美式约定（单个 `.` 一律小数点，单个 `,` 后接 3 位才是千分位），真值与输出同一套、系统性误判两侧抵消；
  **不允许数字串跨普通空格合并**（表格行 `1,234.00 5,678.00` 并成一个 token 指标就废了）。4 项单测。
- `report.py`：所有表加「数字F1↑」列 + 新一节「数字保真专项」。
  **专项子集必须排除真值本身对数字不完整的文档**（arXiv 剥公式 `math_stripped`、维基丢信息框 `wikipedia_cleanup`）——
  不排除的话榜首全是 `数字准` 0.03 的真值口径问题，真正的静默错值反而看不见。
  最差榜**按 digit_f1 排而不是按 precision**：`准低 + 全≈1 + 长度比>1.3` 是真值不完整，**准/全双低才是真把数认错**。
- **结论**（rescore 全量重算，char_sim 等旧指标一位不变）：数字密集子集 256 份里
  **OCR 通道 数字F1 0.718 / 数字全 0.658 vs 有文字层 0.891 / 0.953**；
  20 份真实财报扫描件 **数字F1 0.658、数字准 0.773（22.7% 的输出数字原文里没有）、数字全 0.593**，
  同批 char_sim 0.590 —— **文本指标「中等偏上」而数字已经不能用**，这就是本条的要害。
- **没有重跑转换**：本批一行转换代码都没动，重跑必然产出同样产物（这 20 份是 08-19 01:15 第 3 批修复后跑的）。
  指标改了就用 `python -m bench.rescore`，这正是它存在的理由。

**#7 图片 OCR 通道表格行内错序**：**已定位为上游 opendataloader-pdf 的 Java 阅读顺序重排，不改**。
证据两步：① 把包好的图片 PDF 直接 POST 到后端 `/v1/convert/file`，三份的 DoclingDocument
`texts` 顺序与 bbox 全对（Cash flow → 230 M → 190 M）；② 绕开我们全部后处理直接调
`opendataloader_pdf.convert(hybrid="docling-fast", hybrid_mode="full")`，产出的 md 已经错序、
且与我们的最终产物逐字一致。后端 OpenAPI 自己写着 "markdown and HTML are generated by Java
processors for consistent reading order application" —— 错就错在这一遍。
触发条件（假说）：同一行三个格子的 OCR bbox 顶边有 0.4–1.4 pt 抖动，行分组容差正好卡在边界上。
适合报成上游 issue（合成语料、真值精确、后端 JSON 与最终 md 可直接附上）。

### 2026-08-19 · 公式口径已决（原 #1 遗留的「真值该不该保留公式」）

**结论：真值不改，改评分口径。** `metrics.norm_text()` 现在把产物侧的 `$…$` / `$$…$$` 也剥成空格，
与真值解析器（`from_latex` / `from_html` 一律把公式换成一个空格）对齐。

为什么不是「真值保留公式」：剥公式的真值一共 48 份 = **39 份 arXiv PDF + 9 份维基 HTML**，两类处境相反——
维基 HTML 我们能正确输出 `$tex$`（annotation 里就有 LaTeX），保留有意义；arXiv 是 PDF，公式早已是字形，
**没有任何引擎还原得出 LaTeX**，保留等于给五个引擎全体判零、区分度归零。改真值还要重建 48 份 + 全量 rescore，
收益只覆盖 9 份。改评分口径则一个函数搞定，不动真值。

判据**故意保守**（`$` 也是货币符号），三条限制都是被实测逼出来的，勿放松：
- 行内式：定界符内须含 `\` / `^` / `_`，且跨度 ≤ **60** 字符。60 是扫出来的甜点（试过 25/40/60/300）——
  300 时维基收益最大但 markitdown 的 `[链接_路径](…)$货币$` 被跨段吃掉（−0.006），60 保住九成收益、误伤归零。
- 块级式：内部**不许出现 `$`**。OCR 把德语法条的 `§§` 认成 `$$`，早期写法 `\$\$.+?\$\$` + re.S
  把两处游离 `$$` 之间的 **7306 个字符**整段吃掉（char_sim 0.939→0.492）。
- 块级式：**不许跨空行**。只加「内部无 `$`」还不够——另一份 OCR 产物里 `$$` 后跟着整段正文
  （`Conventions_` 这种误认的下划线正好满足特征），又吃掉 300 字符。真 display 公式短且不含空行。

净影响（相对旧口径，全量 rescore）：**aimorsel 18/626 份受影响、均值 +0.0004**，单份最好 wiki_zh +0.076、
最差 rendered_arxiv_2203_07767_p2_jpg −0.015（OCR 乱码公式区，可接受）；docling **0 份**受影响，
markitdown / pdfplumber_txt / pymupdf4llm 各 ≤6 份且 |Δ| < 0.003 —— 它们本来就不输出 `$` 公式。
子集口径：HTML **char_sim 0.856→0.858、数字F1 0.776→0.779**。
单测 `bench/tests/test_metrics.py` 加 2 项（公式剥离 + 货币不误伤），bench/tests 30→32，全套 150→152。
