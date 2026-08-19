# 评测回流：issue 草稿（F 阶段）

> 2026-08-18 夜跑评测（731 份语料 × 5 引擎 = 3224 条记录）暴露的问题，逐条整理成可直接建
> GitHub issue 的草稿。**建 issue 前请用户过目**。数字全部来自 `bench/RESULTS.md`，
> 复现路径都在 `bench/` 里，语料 id 可在 `bench/corpus/manifest.jsonl` 查到。
>
> 优先级判据：P0 = 直接影响输出可用性且有量化证据；P1 = 真实缺陷但影响面窄或有绕法；
> P2 = 体验/健壮性。
>
> **2026-08-19 处置结果**：已修的条目（#0–#6 / #8–#12 / #14）**不建 issue**，
> 统一写进仓库根的 `CHANGELOG.md`（按四批列，每条链对应 commit）。
> 未修的上游缺陷与能力边界建成长期开着的 GitHub issue：
>
> | GitHub issue | 对应本文 | label |
> |---|---|---|
> | #1 图片 OCR 通道表格行内读序错乱（上游） | #7 | `upstream` + `known-limitation` |
> | #2 扫描件数字静默出错 | #13 | `known-limitation` |
> | #3 RTL lam-alef 连字歧义 | #0「已知未修」 | `known-limitation` |
> | #4 公式：HTML 保留 LaTeX、PDF 不还原 | #1 | `known-limitation` |
> | #5 图片/扫描件通道不做表格重建 | #7「附带」 | `known-limitation` |
>
> issue 正文为英文（面向公开仓库），内容取自下方对应条目。

---

## #0 [P0，本轮最严重] 阿拉伯语 PDF 输出的是**视觉序**，未做 bidi 逻辑序还原

> 🔗 未修部分（lam-alef 连字歧义）已建 **GitHub issue #3**，长期开着。

> **状态：已修（2026-08-18，未提交为 issue 前先修掉了）**。新模块 `rtl_text.py`（纯 stdlib、
> UAX#9 简化实现）+ `morsel.restore_rtl_products()` 接线，接在 `normalize_produced_files()`
> 之后、`_post_process` 之前，PDF 主路径与 pdfplumber 兜底路径都接。做法：文档级词形探测
> （逻辑序文档零改动）→ 用 `keep_line_breaks=True` 再转一遍拿物理行（否则多行段落的**行序**会倒）
> → 逐行还原并按第一遍的行/块/格结构拼回 → note 报「还原 N 行 RTL 文本的逻辑序」。
> 阿语 PDF 子集 10 份重跑：`rtl_visual_ratio` 从 0.98–1.0 全部降到 ≤0.02（残留是 lam-alef 连字
> 与源文本本身的乱码），`"المتحدة"` 逻辑序计数 65/0。指标 `rtl_visual_ratio` + 报告列
> 「RTL视觉序(份)」已加。**已知未修**：lam-alef 连字（لا/لأ/لإ/لآ）在视觉流里按逻辑序成对出现，
> 反转后与冠词 ال 不可区分（与 pymupdf4llm 同等水平，"الأمم" 会出成 "األمم"）；谐音符号位置随生成器。
> 单字母碎片标题（下方附带发现）未修，归入 #9。测试 `tests/test_rtl_text.py` 12 项。

**现象**　阿语 PDF 转出的文本里，每个阿语 run 都是**逐字符反转**的视觉序：
`الجمهورية اليمنية`（也门共和国）变成 `ةينميلا ةيروهمجلا`。
**字一个不少、渲染出来肉眼几乎看不出**（字形仍连成词），但 grep、分词、向量化、复制粘贴
**全部失效**——与当初「康熙部首」那次事故性质完全相同，只是发生在 RTL 上。

**码位级证据**（数 `"المتحدة"`「联合国」一词在逻辑序 / 反转序下的出现次数，样本 `un_a_res_77_1_ar`）

| | 逻辑序 | 反转序 |
|---|---|---|
| PDF 文字层原始 | 0 | 63 |
| **AImorsel** | **0** | **65** |
| pdfplumber_txt | 0 | 63 |
| markitdown | 0 | 63 |
| **docling** | **64** | 0 |
| **pymupdf4llm** | **68** | 0 |

**责任判定**　PDF 内容流本身就是视觉序（所以这不是「源文件坏了」能推掉的），
但 **docling 与 pymupdf4llm 都做了 bidi 重排**，我们和 markitdown / pdfplumber 没有。
⇒ 可修复的能力缺口，不是源文件问题，也不是指标问题。

**范围精确**　**HTML 路径没有这个问题**：`wiki_ar_diabetes`（阿语维基）顺序完全正确，
heading_f1 0.985、order_tau 0.957。⇒ 缺陷精确定位在 **PDF 文本抽取层**，
`format_adapters` 的 HTML 通道是干净的。
另：拉丁字母与数字未被反转（`A/RES/77/1`、`22-23391 (A)` 都正确），所以不是整行反转，
而是**没有按 UAX#9 还原逻辑序**。

**为什么危险**　`out_chars`、`length_ratio` 这类指标看起来完全正常，
**掩盖了产物根本不可用的事实**。评测里若不是做了人工抽检，这条会漏过去。

**期望**　阿语（以及希伯来语等 RTL）PDF 的输出为 Unicode 逻辑序。

**建议改法**　在 PDF 抽取之后、与 `normalize_compat_chars()` 同一位置增加一道 bidi 归一：
检测 RTL 字符占比超阈值的 run，按 UAX#9 还原逻辑序，并像兼容码位那样在 note 里报处理数量。
配套：`bench/metrics.py` 增加一个「RTL 逻辑序」检查项，让这类缺陷以后能被自动指标抓到
（当前所有文本指标都对字符顺序不敏感，是这次差点漏掉的原因）。

**附带发现（同一批样本，可并入或单列）**　`wb_ar_099071426153030997` 的产物里出现
`# ن`、`#### ر`、`#### ي ي ُ ر ر ي` —— **单字母碎片被判成标题**，污染 outline 与 RAG 分块。

---

## #1 [P1] HTML 路径把行内数学公式整块丢掉（原「PDF 插空格」结论已撤回）

> 🔗 「PDF 路径不还原公式」这条能力边界已建 **GitHub issue #4**，长期开着。

> **状态：已修（2026-08-18）**。`format_adapters._HtmlTreeBuilder` 把 `<math>` 从整段丢弃名单里拿出来，
> 以 LaTeX 文本形式保留：优先 `<annotation encoding="application/x-tex">`，其次 `alttext` 属性，最后 MathML
> 词元拼接；`{\displaystyle …}` 壳剥掉；行内写成 `$…$`，`display="block"` 写成独立一行 `$$…$$`；
> 只有回退图片（`<img class*=math alt=LaTeX>`）也收，与刚写出的公式相同则不重复。
> 复现件重跑：`需要知道 $n-1$ 的質因數`，全文 652 处公式回来。HTML 子集（172 份）重跑后：CJK HTML 的
> 「汉字 空格 汉字」总数 3960→3778，**残留是另一机制**——维基导航框/信息框里几十个链接被空白拼进一个
> 单元格（`| 阿拉伯文 中文 英文 … |`），这些空格在源码里就有、不是内容丢失，先不追。
> **真值解析器 `truth/from_html.py` 仍把公式剥成空格**（没动，避免在修 bug 的提交里改口径），所以我们
> ~~现在多出的 `$…$` 反而让 HTML char_sim 略降（0.828→0.825）；真值该不该保留公式列入 PLAN 待决。~~
> **已决（2026-08-19）**：真值**不改**，改在评分口径——`metrics.norm_text()` 把产物侧的公式也剥掉，
> 两侧同口径。理由：真值保留公式只对 9 份维基 HTML 有意义，对 39 份 arXiv PDF 有害（没有任何引擎
> 能从 PDF 字形还原 LaTeX，保留等于全体判零、区分度归零）。实测 HTML 子集 char_sim 0.856→0.858，
> 维基公式页单份 +0.024~+0.076；docling 0 份受影响（它不输出 `$` 公式）。详见 RESULTS.md 口径说明第 8 条。
> 提交前 adversarial verify 确认并修掉 5 条：回退图去重误伤重复公式（`_last_math` 现在遇正文/其他标签即清）、
> KaTeX/MathJax v2 页面公式重复输出（`aria-hidden="true"` 的非 img 元素整段跳过）、`_clean_tex` 贪婪剥并列壳
> （改括号配对扫描）、标题/列表/单元格里 `$$` 独立行劈断结构（降为行内）、未闭合 `<math>` 吞后文（遇块级标签收口）。
> 测试 `tests/test_adapters.py::test_html_math_kept_as_latex` / `test_html_math_edge_cases`。

> **撤回说明**：本条最初写的是「PDF 在 CJK 换行处插空格，比 pymupdf 多 8.5 倍」。
> 那个倍数来自 `cjk_inner_spaces` 指标的一个 bug（它先把换行折成空格，于是每个段落/标题
> 边界都被误计一次，系统性偏向输出结构更少的引擎）。**修掉指标并重算后**：
> 原生文字层 PDF 上 72 份共 22 处（均 0.3 处/份），pymupdf4llm 22、pdfplumber 21 —— **持平，
> 该缺陷在 PDF 路径上不成立**。详见 `bench/SPOTCHECK.md` 第四节。

**真正的问题在 HTML 路径，而且性质是内容丢失而非排版。**

**现象**　CJK 语种的 HTML 输入，产物里平均每份留下 104 处「汉字 空格 汉字」
（38 份共 3960 处；markitdown 在同一批文档上只有 0–16 处）。追下去发现空格不是凭空插的，
**是行内数学公式被整块丢弃后留下的空隙**：

```
产物：  除了前述可應用於任何自然數 之上的測試外……盧卡斯質數測試需要知道 的質因數
应为：  ……任何自然數 n 之上的測試外……需要知道 n−1 的質因數
```

**影响**　① 数学/科学类 HTML 的公式全部丢失，读者看到的是断句；
② CJK 之间留下的空格进一步破坏 grep 与分词。

**为什么自动指标没发现**　真值解析器 `truth/from_html.py` 出于「不让所有引擎一起虚低」
的考虑**也把公式剥成了空格**，于是双方对称失明，char_sim 察觉不到。**是人工抽检发现的。**

**期望**　至少保留公式的文本形式（MathML 的 `annotation` / `alttext` 属性通常有 LaTeX 原文），
或明确输出 `[公式]` 占位；无论如何不要在 CJK 之间留下裸空格。

**复现**　`python -m bench.run --engines aimorsel --ids wiki_zh_prime_number --force --tag repro`，
看产物里 `需要知道 的質因數`。

---

## #2 [P1] 兼容码位归一化只接在 PDF 路径，HTML/Office 输入会残留

> **状态：已修（2026-08-18）**。`_convert_office()` 在 `_post_process` 之前也调 `normalize_produced_files()`
> （与 PDF 路径同一套、同一计数口径），note 报「修正 N 处兼容码位」。`sec_ibm_10k_20260224` 重跑后
> `compat_residual` 4 → 0，全库 aimorsel 残留列归零。测试 `tests/test_office_convert.py::test_html_compat_ligature_normalized`。

**现象**　`normalize_produced_files()` 目前只在 `_convert_pdf` 里调用。HTML 源文件里用
**数字实体**写的连字（`&#64257;` = ﬁ、`&#64256;` = ﬀ）经适配器解码后原样进入产物，不被归一。

**证据**　全库 731 份里唯一残留的是 `sec_ibm_10k_20260224`：产物含 **3 个 ﬁ + 1 个 ﬀ**
（`compat_residual = 4`），markitdown 同样 4 个。源 HTML 里 grep 得到 `&#64256;` ×1、
`&#64257;` ×3，确认是实体解码而来，不是我们生成的。

**为什么值得修**　原先「Office 路径不接（XML 文本无此问题）」这个假设对 HTML 不成立：
HTML 常常是从 PDF 转出来的，会继承排版连字。而「不残留兼容码位」是本项目对外的差异化点之一，
留 4 个和留 400 个在「能不能声称做到 0」上是一回事。

**期望**　任何输入路径的产物 `compat_residual` 都为 0。

**建议改法**　把归一化从 `_convert_pdf` 挪到 `_post_process()` 之前的公共位置（PDF 与适配器
两条路径都会经过），或在 `format_adapters` 渲染后统一调一次。

---

## #3 [P1] pdfplumber 兜底网对「xref 结构损坏」的 PDF 完全无效

> **状态：已修（2026-08-18）**，走的是建议里的**选项 A**（`pikepdf`/qpdf，MPL-2.0）。`_convert_pdf()` 在引擎
> 抛异常时先 `_repair_pdf()`：pikepdf 打开→重写到临时目录（同名副本），再喂一次引擎；成功即**完整结构树、
> 不算降级**，note「PDF 结构损坏，已修复后转换：<原错误>」；引擎连副本也拒收才落到 pdfplumber 兜底
> （先试副本再试原件）。4 份 arXiv 重跑全部 ok（7–17 页，修复 0.02–0.2 s），aimorsel 失败数 4→0、
> 成功率 99.5%→100%（RESULTS.md 已更新；这 4 份 char_sim 0.40–0.60，与真值是 LaTeX 源的其他 arXiv 件同量级）。
> **根因订正**：这 4 份不只是「xref 不规范」，是**没有 `%%EOF` 的截断下载**（尾部直接断在对象流中间，
> 一份大小正好 384 KiB）；mupdf/qpdf 靠扫描对象重建，pdfminer/pypdf（试过 `strict=False`，同样
> `Stream has ended unexpectedly`）都不行。pymupdf 仍不能用（AGPL）。
> 测试 `tests/test_fallback.py` 4 项（截掉尾部 15% 的 fixture + 「无 %%EOF 就拒收」的假引擎，含无 pikepdf
> 时保持原失败、成功路径不尝试修复）+ `tests/test_integration_java.py::test_real_truncated_pdf_repaired`。
> 提交前 adversarial verify 确认并修掉 4 条：任何引擎失败都触发修复（改为只对像结构损坏的错误修，
> `looks_structural_error`）、修复副本引擎肯吃但产物无字被记成完整成功（加密字典随 trailer 丢时副本是密文；
> 现在无字即扔掉产物走兜底/失败）、`_run_engine` 把无消息异常当成功（改返回 None/非空串）、无产物分支丢修复 note；
> 顺带：副本保持原件加密不落明文、压掉 pikepdf 密码 UserWarning。

**现象**　4 份真实 arXiv PDF（`arxiv_2107_10128` / `arxiv_2405_09619` / `arxiv_2410_22486` /
`arxiv_2501_01459`）转换彻底失败。文件本身没坏：`%PDF-1.4/1.5` 头正常、sha256 与下载时一致、
**pymupdf 能打开并读出首页 1259–4380 字**。根因是交叉引用表结构不规范——mupdf 会自愈，
pdfminer 严格拒收。

**关键点不是「我们失败了」，是「两层兜底等于一层」**：

| 引擎 | 结果 |
|---|---|
| AImorsel（Java 引擎 → pdfplumber 兜底） | ❌ 两层都失败 |
| pdfplumber_txt | ❌ `PdfminerException: Unexpected EOF` ×3 / `No /Root object!` ×1 |
| markitdown | ❌ `FileConversionException` |
| docling | ❌ `ConversionError` |
| pymupdf4llm | ✅ 全部成功 |

失败率上我们与两个竞品并列、不落后（这 4 份占 PDF 语料的 1.6%）。但设计上，
**兜底层选了一个比主引擎更严格的解析器，在「文件结构损坏」这个最需要兜底的失效模式上没有任何冗余。**

**期望**　结构轻度损坏的 PDF 至少能降级出纯文本。

**建议改法（都**未验证**，建 issue 时标明）**
- 选项 A：用 `pikepdf` / `qpdf`（**MPL-2.0，与 Apache-2.0 兼容**）先修复 xref 再重试现有两条路；
- 选项 B：加 `pypdf`（BSD，纯 Python）作第三层兜底。
- ⚠️ **不能用 pymupdf**：AGPL，与本项目 Apache-2.0 的 open-core 模式冲突。

---

## #4 [P1] OCR 服务健康检查用 `localhost`，在配了系统代理的机器上恒判离线

> **状态：已修（2026-08-18）**。`DEFAULT_HYBRID_URL` 改 `http://127.0.0.1:5002`（config.toml / README 同步）；
> `check_ocr_server()` 用 `build_opener(ProxyHandler({}))` 显式绕过系统代理。测试
> `tests/test_core.py::test_check_ocr_server_bypasses_proxy`（设一个连不上的 http_proxy 仍能探到本地服务）。
> bench 数字不变——跑批时早已用 `BENCH_OCR_URL=127.0.0.1` 绕过。「检测失败不能静默降级」这半句归 #5。

**现象**　`DEFAULT_HYBRID_URL = "http://localhost:5002"`。本机实测 `urllib` 请求
`http://localhost:5002/health` 拿到 **502**（走了系统代理 / 先解析到 `::1`），
换 `http://127.0.0.1:5002/health` 立刻 200。于是 `check_ocr_server()` 判服务离线，
**OCR 静默不启用**，图片输入只提取版面、输出为空。

**为什么容易漏**　`curl localhost:5002/health` 是 200（curl 与 urllib 的代理处理不同），
肉眼排查会被骗过去。评测里若不是先跑了 `--sample 20`，我们自家引擎的图片一列会拿一片
`char_sim = 0.0` 进报告。修正后同一批图片 char_sim 升到 0.66–0.87。

**期望**　服务在线时就该被检测到；检测失败时不能静默降级。

**建议改法**　① 默认地址改 `127.0.0.1`；② `check_ocr_server()` 与探测请求显式
`urllib.request.build_opener(ProxyHandler({}))` 绕过代理（本地回环本来就不该走代理）。

---

## #5 [P1] 图片输入在 OCR 不可用时输出空文本，状态却是「成功」

> **状态：已修（2026-08-18）**。`_convert_image()` 转完数一遍产物文字量（`extracted_text_chars`：优先 JSON 的
> content，否则 md/txt/html 剔除图片引用、分页标记、`<head>/<style>/<script>` 后数字母/数字/汉字；**只看本次
> 写出的文件**——按 mtime 过滤，目录里上一轮换格式留下的旧 JSON 不算），为 0 → `degraded=True`，note 写明原因
> （未经 OCR / OCR 未产出文字（后端失败或未识别）/ OCR 已关闭）；日志标 `△`，report.csv 状态「降级转换」，
> CLI/GUI 汇总行数出「其中 N 个降级/无文字」，Web 面板状态列「△ 降级」，MCP `convert_pdf` 打 △ 并附 note、
> `read_pdf_markdown` 正文前插提示（缓存命中也查清单）。清单 `.done.json` 记 `needs_ocr`（+ 带 OCR 试过的
> `ocr_attempts`）：`should_skip(server_ok=True)` 且 attempts < 2 时不跳过，即 **OCR 服务上线后重跑/监听下一轮
> 自动补转**，试满 2 次仍无字视为空白图不再重转（`ocr_redo_available` 只在清单里有待补条目时才探测服务；
> `check_ocr_server` 现在吞一切异常——端口上是非 HTTP 程序时 BadStatusLine 不能把监听线程带死）。
> 服务仍离线时照常跳过，监听模式不会每轮重跑刷屏。测试 `tests/test_image_empty.py` 8 项。
> 提交前过了一轮 adversarial verify（3 维审查 + 逐条反驳），确认并修掉 5 条：html-only 时 `<title>` 被数成文字、
> 旧产物误导判空、OCR 后端失败被记成「未识别」且不补转、监听线程无保护、Web/MCP 入口没透出降级。
> bench 影响：`engines/aimorsel.py` 把 degraded 记 "degraded"，图片子集跑批时 OCR 在线，只有「OCR 认不出任何字」的图会从
> ok 变 degraded；现有结果未重跑（图片子集重跑一次要 30+ 分钟，且不改质量指标），下次全量重跑时自然更新。

**现象**　OCR 服务不可用时，图片输入走「仅提取版面」，产出一份只有几十字节、没有任何文字的
Markdown，`ConvertResult.ok = True`。批量报告里它计入成功，用户只有打开文件才发现是空的。
（与 #4 叠加时尤其危险：服务明明在线，却因检测 bug 判离线，然后静默产出一批空文件并全部报成功。）

**期望**　图片/扫描件在无 OCR 时应报「未完成」或至少是降级状态，报告里可区分。

**建议改法**　给 `ConvertResult` 增加一种「空产出」判定：图片路径 + 未走 OCR + 提取文本长度
低于阈值 → `degraded`（已有这个状态，兜底转换在用）并在 note 里写清原因。

---

## #6 [P2] 评测基建：指标里的一处类型判断会让整批卡死（**已在 bench 内修复，记录备查**）

`bench/metrics.py` 的 `_levenshtein()` 原本写着 `if _Lev is not None and isinstance(a, str)`，
把 `list[str]` 挡回了纯 Python 的 O(n·m) DP——而词级 CER 传进来的正是 list。
一份 1.2 MB 的 SEC 10-K 真值算了 20 分钟仍未完，六个 worker 全在等它，整批停摆。
rapidfuzz 本身支持任意序列（实测 12 万 token 比较 0.64 s，纯 DP 要数小时）。

**诊断特征值得记住**：`run.py` 父进程 99.9% CPU，但 `pgrep -f bench.engines` 一个子进程都没有
= 卡在指标而不是引擎。已修，并加了 40 万字符 / 15 万 token 的截断保险绳（超限打
`metrics_truncated` 标记）。此条不是产品缺陷，**建 issue 时可略**，留在这里是为了让下次
读到 `metrics_truncated` 的人知道它从哪来。

---

## #7 [P1] 图片 OCR 通道的表格行内读序错乱、数值互换（docling 同题全对）

> 🔗 已建 **GitHub issue #1**（`upstream` + `known-limitation`，长期开着，**未报上游**）；
> 附带的「图片通道不做表格重建」单列为 **GitHub issue #5**。

> **状态：已定位（2026-08-19）——**是上游 opendataloader-pdf 的 Java 阅读顺序重排，
> **不是我们的锅，本轮不改**（要在我们这层修等于自己实现一遍版面重排，风险远大于收益）。
>
> **证据链**（三份样本各做两步，结论一致）：
> 1. **OCR 后端给的顺序是对的**。把我们包好的图片 PDF 直接 POST 到 `http://127.0.0.1:5011/v1/convert/file`，
>    看返回的 DoclingDocument JSON：三份的 `texts` 都是 `… Cash flow(l=97) → 230 M(l=262) → 190 M(l=379)`，
>    顺序与 bbox 全对。**后端无罪。**
> 2. **绕开我们全部后处理，错序就已经在了**。直接调
>    `opendataloader_pdf.convert(input_path=<包好的PDF>, format=["markdown","json"], hybrid="docling-fast",
>    hybrid_mode="full", hybrid_url="http://127.0.0.1:5011")`，产出的 md 是
>    `230 M / Cash flow / 190 M`（de）、`190 M / Cash flow / 230 M`（fr）——**与我们的最终产物逐字一致**。
>    我们的 Python 壳不参与排序（md 直接来自引擎；`tidy_products` 只降级标题、改列表标记；
>    `normalize_compat_chars` 只逐字符替换），错序在进我们手之前就发生了。
>
> **在哪一步坏的**　后端的 OpenAPI 说明写得很清楚：*"Only JSON output is provided - markdown and HTML
> are generated by Java processors for **consistent reading order application**"* ——Java 拿后端的
> items 重排一遍再出 md/html，错就错在这一遍。引擎产物 JSON 里的 bbox 与后端一字不差（`230 M`
> `[262.0, 242.0, 352.667, 274.333]`），只有**顺序**被换掉了。
>
> **触发条件（假说，未反编译确认）**　同一表格行三个格子的 OCR bbox 顶边有 **0.4–1.4 pt 抖动**。
> 三份的输出顺序都能被「按 top 严格降序、同 top 按 left」解释：de `230 M` t=274.3 最高、en `230 M`
> t=273.7 最高、fr `190 M` t=274.7 最高，各自排到了行首。**但同一页上面的 Revenue 行（top 差 0.4–0.7 pt）
> 却是对的**，所以不是纯 top 排序，更像行分组容差正好卡在 ~1 pt 的边界上。精确算法在 Java 侧。
>
> **最小复现**　起 hybrid 服务 → `format_adapters.image_to_pdf()` 把
> `bench/corpus/synthetic/syn_de_business_q70.jpg` 包成 PDF → 上面第 2 步那行 convert() → 看 md 的表格区。
> （诊断脚本 `dump_backend.py` / `raw_engine.py` 在 session scratchpad，不入库。）
>
> **下一步**　这条适合作为**上游 issue** 报给 opendataloader-pdf：合成语料、真值 100% 精确、
> 后端 JSON 与最终 md 的对照可直接附上。我们这边只在文档里承认「图片通道不做表格重建」。

**现象**　图片输入里，表格最后一行的单元格读序被打乱，法语那份连两个季度的值都互换了：

| 样本 | 我们的输出 | 应为 | docling |
|---|---|---|---|
| `syn_de_business_jpg` | `230 M / Cash flow / 190 M` | `Cash flow / 230 M / 190 M` | ✅ 正确 |
| `syn_en_law_jpg` | `230 M / Cash flow / 190 M` | 同上 | ✅ 正确 |
| `syn_fr_it_png` | `190 M / Cash flow / 230 M`（**值互换**） | 同上 | ✅ 正确 |

**3/3 复现，docling 在同一批图片上全部正确** —— 这是本轮抽检里唯一「同题对比下我们错、
对手全对」的结构性缺陷，也是最干净的一条：语料是合成的，真值 100% 精确。
数值互换意味着**输出的是错的事实**，比丢内容更糟。

**附带**　图片通道整体不做表格重建（`cell_f1 = 0`，表被拍平成逐行文本），这一条 docling 同样，
属共性能力缺口，可作为同一 issue 的背景。

---

## #8 [P1] 旋转退化图上内容腰斩，docling 保留量是我们的 2.2 倍

> **状态：已修（2026-08-19）**。图片进引擎前先做投影法倾斜校正
> （`format_adapters.detect_skew` / `deskew_image`，纯 Pillow 无 numpy）：灰度 → 长边缩到 800px →
> 在 ±5° 内先 0.5° 粗搜再 0.1° 细搜，取「每行平均亮度的方差」最大的角度（文字行摆平时行间留白，方差最大），
> 只取中间 80% 算投影以避开旋转留下的白色楔形；|角度| < 0.5° 不动。`image_to_pdf(deskew=True)` 逐帧转正
> 并返回转动角度，`_convert_image` 记 note「已校正 N 度倾斜」。开关 `ConvertOptions.deskew` / `--no-deskew` /
> config `[convert] deskew`。
> **A/B 实测（12 份 rotated 语料，同一真值、同一 OCR 服务，只切 deskew 开关）：char_sim 平均
> 0.333 → 0.530（+59%），11/12 提升**，最大 +0.35（`rendered_cninfo_301042` 0.361→0.708）；
> 唯一回退的是 `rendered_cninfo_002330`（0.169→0.138，两边都在 0.15 上下的低质量档）。
> 单文件文本量 633 → 977 字符。**全量重跑后 tiff 子集 char_sim 0.601 → 0.754、CER 0.499 → 0.320、
> heading_f1 0.813 → 0.871**（jpg 0.736 → 0.750）。
> **假阳性零**：全部 188 份图片语料测一遍，只有 39 份超过 0.5° 阈值——12 份 rotated 退化件、
> 21 份 `syn_*_skew.tiff`（**合成时就是按 1.5° 生成的**，见 make_synthetic.py）、6 份真实扫描件
> （xfund/funsd，0.5–0.8° 的真倾斜）。其余 149 份一律判 <0.5° 原样通过，正常图零改动、零重采样。
> 代价：每张图约 0.12 s 估角。测试 `tests/test_adapters.py::test_detect_skew` / `test_image_to_pdf_deskew`。

**现象**　`degradation = rotated`（±3°）的 tiff：`rendered_cninfo_300513_p2_tiff` 只保留 49% 文本、
`300817` 只保留 46%，表格行首标签整列丢失，尾段截断在「公司不存在将」。
**同页 docling 输出 1453/1593 字符，是我们的 2.2–2.3 倍** —— 说明不是图片不可识别，是版面丢了。

**期望**　轻微旋转不应导致内容腰斩（真实扫描件几乎都有倾斜）。
**建议**　转 OCR 前加一道倾斜校正（deskew），或把整页兜底送 OCR 而不是按检出的版面块送。

---

## #9 [P1] 标题判定噪声：碎片被提升为标题、标题吞掉正文首句

> **状态：部分已修（2026-08-19）——「误升为标题」修了，「标题吞正文」没修。**
> `morsel.tidy_products()`（新）就地清理产物 md/json/html，`heading_is_noise()` 把三类明显不是标题的
> 降成正文：① **单字符**且不是 CJK 汉字（图注面板的 a/b/c、公式碎片 √/×、阿语单字母；「記」「序」这类
> CJK 单字是正经小标题，放过）；② RFC 2119 关键词（MUST/SHOULD/MAY/SHALL/REQUIRED/…，来自 RFC 正文的
> 强调词，31 处）；③ 文件名样式（`L_2016157EN.01000101.xml`，EUR-Lex 把源 XML 名当 `<title>` 补成首标题，43 处）。
> **安全性先量过再动手**：把规则套到全语料 **6,632 条真值标题**上，命中 **0** 条——这条规则只可能提高
> precision，不可能降 recall。全语料原有 7,581 个标题里命中 125 个。
> **没修「标题吞正文」**（`## § 2 Begriffsbestimmungen Im Sinne dieses Gesetzes ist`）：排期里设想的判据
> 「标题超 N 词且含句末标点」对这些例子根本不成立——它们连句末标点都没有（一个以 `ist` 结尾、一个以逗号结尾），
> 靠长度切又会误伤 233 处真的长标题（西语表单名、EUR-Lex 条标题）。**没有可靠信号就不动**，留作后续。
> 测试 `tests/test_core.py::test_heading_is_noise` / `test_tidy_products`。

两个方向的错，都会污染 `get_outline` 与 RAG 分块：

**误升为标题**　图注面板字母（`#### f`、`## a c`、`###### b`）、源文件内部名
（`# L_2019186EN.01005701.xml`）、正文关键词（`##### MUST`，来自 RFC 的 MUST/SHOULD）、
单字母碎片（`# ن`、`#### ر`，阿语 PDF）。

**标题吞正文**　`## § 2 Begriffsbestimmungen Im Sinne dieses Gesetzes ist` ——
把条文首句一起吞进标题行（`gesetze_de_ifg` 6 处）。

**注意评判口径**　这会压低 `heading_f1`，但**不能只看 heading_f1 判优劣**：
`gesetze_de_ifg` 的 heading_f1（0.727）低于 pymupdf4llm（0.909），可正文 char_sim
我们 0.967 高于它的 0.928 —— 是「粘连」不是「漏标」。

---

## #10 [P2] 扫描 PDF 通道把正文段落渲染成假列表

> **状态：已修（2026-08-19）**。同一个 `tidy_products()`：`list_paragraph_flags()` **按整个列表判**，
> 不逐项判——同一条法条里「(1) 短句。」和「(2) 长段落…」并排出现，逐项判会把一组条款一半留在列表里
> 一半变成段落。判据：一个列表里带 `(N)` 编号前缀的项 ≥2 个，且其中至少一个超过 80 字符 → 这些项全部
> 还原成段落，其余项留在列表里（列表就地切成「真列表 / 段落 / 真列表」）。单独一个 `(1) 甲` 放过。
> 全语料 2,390 行命中，集中在 EUR-Lex 与德语法条。**指标看不见这条**（`norm_text` 会把 `- ` 和 `#`
> 一起剥掉），它修的是下游：RAG 分块不再把条款切成列表项、`get_outline` 不再多出一层假列表。
> 三种产物（md / json / html）用同一套判据改写，且**幂等**（跑两遍第二遍零改动）。
> md 里降级出来的段落前后补空行（否则会被 Markdown 当成上一条列表项的续行）；
> html 只改**最内层**列表（嵌套列表用非贪婪正则会把外层开头配到内层结尾，两层 `<li>` 混成一组）；
> json 里降级的列表项**整份搬走**（`kids` 里常挂着整段嵌套内容，只抄 content 会把正文删掉）。
> 测试 `tests/test_core.py::test_list_paragraph_flags` / `test_tidy_keeps_list_item_subtree` /
> `test_tidy_markdown_keeps_blocks_separate` / `test_tidy_html_nested_list`。

两份德语法条里，`(1)` `(3)` 开头的条款**段落**被一律渲染成 `- ` 列表项
（`- (1) Dieses Gesetz gilt für…`）。法条编号段落不是列表，下游按列表处理会切错块。

---

## #11 [P2] HTML 通道把 XBRL 隐藏元数据当正文输出

> **状态：已修（2026-08-18）**。`_HTML_SKIP` 加进 `ix:header` / `ix:hidden`，并新增 `_html_hidden()`：
> 除原有的 `hidden` 属性外，行内 `style` 里声明 `display:none` / `visibility:hidden`（含 `!important`）
> 的元素整段丢弃——SEC 财报把 XBRL 事实塞在 `<div style="display:none">` 里，一份三千多处。
> 口径与 `bench/truth/from_html.py` 一致（真值本来就丢这些），所以是往真值收敛而不是背离。
> 实测：`sec_ibm_10k_20260224` 少掉一整块 103,672 字符的 `CHX00000511432025FY…us-gaap:Revenues…`，
> 20 份 SEC 财报共减 1.0 MB 噪声；维基页面顺带丢掉编辑者才看得见的模板告警与旗帜排序键。
> **HTML 子集 char_sim 0.825 → 0.856**。
> 附带修掉两个会「吞掉整篇后文」的隐患（adversarial verify 实测复现，`kids` 为空且无任何报错）：
> ① 空元素（`<img style="display:none">`）没有结束标签，起了 skip 就再也关不掉 → `_HTML_VOID` 直接丢标签本身；
> ② HTML 允许省略 `</p></li></td></tr></option>` 等结束标签，html.parser 不会补 → skip 区域改用
> `_skip_stack` + `_HTML_IMPLIED_END` 表识别隐式闭合，祖先结束标签先到时就地收口。
> 测试 `tests/test_adapters.py::test_html_hidden_metadata_dropped` / `_void_tag_` / `_omitted_end_tag`。

`sec_vz_10k_20260217` 产物开头有 1000+ 字符
（`00007327122025FYFALSE…us-gaap:CommonStockMember…`），真值里没有。
markitdown 同样泄漏，属 HTML 通道共性，但对 RAG 是纯噪声。

---

## #12 [P2] 无标题样式的 Office/HTML 文档输出零标题

> **状态：已修（2026-08-18）**。`format_adapters._promote_numbered_headings()`，在 `parse_office()` 出口
> 统一调用（docx/xlsx/pptx/HTML 一视同仁）。**保守到近乎胆小**：整篇零标题（或只有一个由 `<title>` 补出的
> 一级标题）才动；只提升顶层段落里「短行（≤60 字符）+ 编号」且编号后面没有句末标点的那几行；命中不足 3 处
> 一律不动；反复出现的当页眉丢掉（光秃编号如 "PART III" 出现 2 次即判页眉，带标题文字的允许「目次 + 正文」两次）。
> 编号模式含 第N編/章/節/款/条 与 Part/Chapter/Title/Section/Article/§ 及其 de/es/fr 写法
> （Artikel / Artículo / Kapitel / Capítulo / Abschnitt / Sección / Chapitre，法语 "Article premier" 单列）——
> **只认英文的话 EUR-Lex 同一份指令的 de/es 版一个层级都拿不到**（adversarial verify 抓到）。
> 层级：編/章/Part=2、節/Section=3、款/条/Article/§=4，与补出的文档标题(1)错开。
> 实测：egov_jp 每份多 6–30 个章节标题（**docx 子集 heading_f1 0.583 → 0.637**，ja 语言组 0.408 → 0.478），
> EUR-Lex 32 份 html 各 9–29 个，SEC 财报拿到 PART I–IV。
> **未做**：条文标题（真值的 `第一条 （目的）` 是「条号 + 前一行的括号小标题」拼出来的，要合并两个段落，
> 超出「只提升匹配行」的授权）；SEC 的 `Item 1.` 没进模式表（正文引用太多，误升风险大）。
> 测试 `tests/test_adapters.py::test_promote_numbered_headings`。

`egov_jp` 的 docx / html 产物只有 0–1 个 Markdown 标题，而官方 XML 里有 57–117 个条标题
（`第一条（目的）`）。源文件确实没有 h 标签与标题样式（markitdown 同为 0），
所以**主要是口径问题**；但「一个层级都不给」让下游分块无从下手。
可考虑：对「短行 + 编号模式」（`第N条`、`第N章`、`Article N`）做启发式提升。

---

## #13 [P2] 财报扫描件的数字被静默错认（OCR 边界，但后果最重）

> 🔗 已建 **GitHub issue #2**，长期开着。

> **状态：已处理（2026-08-19）——写进文档 + 加了专项指标。改不动模型，但不能让人不知道。**
>
> 1. **README 新增「⚠️ 已知限制：扫描件里的数字不可直接采信」**（在「扫描件与 OCR ›
>    真实效果如何」下面，另在常见问题里加了一条指路）：列出四组真实错值样本，
>    结论一句话——**检索/定位可以用，凡是要进表格、进计算、进报表的数字一律以原件为准**；
>    并写明有文字层的 PDF 不受此限（那条路径不经过 OCR）。
> 2. **bench 新增「数字串一致性」专项指标** `metrics.digit_stats()`：从真值与输出各自抽出
>    全部数字串（`numbers()`，含千分位/小数/正负号，**归一化后**比对），做**多重集** P/R/F1。
>    `digit_precision` 是关键——**`1 - 数字准` 就是凭空造出来的错值占比**。
>    口径：位置错乱不计入（那是 #7 / order_tau 的事）；负号丢失同时算一次假数和一次漏数
>    （它确实两处都错）；分隔符按英美式约定归一，真值与输出走同一套、系统性误判两侧抵消。
>    不允许数字串跨普通空格合并（表格行 `1,234.00 5,678.00` 若并成一个 token 指标就废了）。
>    接进 `score_document` → `report.py` 的所有表加「数字F1↑」列，另出一节
>    **「数字保真专项（真值含 ≥30 个数字串的文档）」**（跨引擎表 + AImorsel 造错值最多的 15 份）。
>    单测 4 项（`bench/tests/test_metrics.py`），含本 issue 的真实错值样本。
> 3. **量出来的结论**（20 份真实上市公司财报扫描件）：`数字F1` 0.658、**`数字准` 0.773
>    ——输出的数字里 22.7% 在原文中根本不存在**、`数字全` 0.593；同一批的 char_sim 是 0.590。
>    **文本指标「中等偏上」而数字已经不能用**，这正是本条的要害：改一位数在字符层面只是一个字符的差异。
>    单份最差 `rendered_cninfo_300513`（tiff）`数字准` 仅 0.360。
> 4. **没有重跑转换**：本批只改了 bench 指标，转换管线一个字没动，重跑必然产出同样的产物
>    （这 20 份的产物是 08-19 01:15 第 3 批修复后跑的）。用 `python -m bench.rescore`
>    在已落盘产物上重算全部引擎的指标——这正是 rescore 存在的理由。
>
> **典型错法**（指标抓到的假数/漏数明细，`688347`）：`-160,655,048.90` → 负号丢失、
> `132,704,932.32` → `132,701,932.32`、`45,248,081,505.77` → `45,218,081,505.77`、
> `9.33` → 裂成 `9` 和 `33`。

`132,704,932.32` → `132,701,932.32`、`431,549,976.68` → `431,519,976.68`、
`45,248,081,505.77` → `45,218,081,505.77`（`688347` 系统性 `4`→`1`）、
`-248,151.42` → `248,151.42`（负号丢失）。

char_sim 0.61–0.67 看起来「中等偏上」，**但对财报而言这是静默错值**。
属 OCR 后端能力边界（docling 同件也错），改不动模型，但**必须写进文档的已知限制**：
财务数字请勿直接采信扫描件的转换结果。另建议 bench 增加一个「数字串一致性」专项指标。

---

## #14 [P2] 单张图片耗时 167.8 秒（同类只要 16.7 秒）

> **状态：已定位并修掉根因（2026-08-19）——是 bench 跑批的并发设置，不是转换缺陷。**
> 同一份 `funsd_82200067_0069`：单独串行跑 **13.1 s**，`--jobs 2` 跑 **33.4 s**（2.5×），
> 夜跑记录 167.8 s（还叠加了整机负载）。产物完全一致（char_sim 0.0231 两边相同），
> 与图片内容无关。根因是 `run_ocr_batches.py` 给 `bench.run` 传 `--jobs 2`：OCR 后端是
> **单实例 CPU-bound 服务**，两份文档并发只是互相抢核——实测 4 份图片 jobs=1 与 jobs=2
> **墙钟时间相同（约 65 s）**，并发一点没省，只是把单份耗时翻了 2.5 倍、把 p95 彻底搞脏。
> 已改成 `--jobs 1`（并顺手加了 `--force` 透传，方便按语言重跑）。
> 顺带加了 `MORSEL_DEBUG_TIMING=1` 计时钩子（`morsel._timing`），图片通道会分别打印
> `image_to_pdf` 与 `engine+ocr` 两段耗时——实测 22.18 s 里 0.15 s 是包 PDF，其余全在引擎+OCR。
> **教训与 #4 同类：跑批 harness 的设置会伪装成被测对象的缺陷，先复现单跑再下结论。**

`funsd_82200067_0069` 单张 PNG 耗时 **167.8 s**，同批 `funsd_82253362_3364` 只要 16.7 s，
相差 10 倍，原因未查（是否触发了重试或大图重采样）。10 倍方差会让批量任务的耗时预估失真。

---

## 附：bench 自身要修的（不是产品 issue）

抽检同时发现真值侧四处问题，**修的是 bench 不是引擎**，记在这里免得下次又当成引擎缺陷：

1. `make_synthetic.py` 给 `syn_en_it.pptx` / `syn_ja_it.pptx` 的真值**多写了三行源文件里
   根本不存在的列表项**（核实过 pptx 的 zip 内容），拉低了这两份的分数。
2. `rendered_eurlex_32013r0524_es_p7_tiff` 的**真值分栏顺序是错的**（右栏 5、6 排到左栏 1 前），
   `order_tau 0.53` 是真值的错，产物反而对。
3. `rfc9297` 的真值把 RFC 页眉 running head 当成表格，引擎不输出它反被判 `cell_f1 = 0`。
4. `xlsx` 的 Notes 表被真值当段落，`cell_f1` 封顶 0.774。

以及两个**已修**的指标 bug（`norm_text` 的标签正则、`cjk_inner_spaces` 的换行折叠），
详见 `bench/SPOTCHECK.md` 第四节。

---

## 抽检原始结论

60 份人工判读的完整表格与失败模式归纳见 **`bench/SPOTCHECK.md`**。
