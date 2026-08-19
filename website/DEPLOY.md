# 官网部署说明（website/）

纯静态站，**无构建步骤**：手写 HTML + 一份 `assets/site.css` + 自托管字体。
不引框架、不挂 CDN、不上统计脚本，断网也能完整渲染。
本地预览直接双击 `index.html` 就行（页面之间用相对路径，本地和线上都通）。

设计体系与改动约束见 `DESIGN.md`——**改样式前先读那份**。

## 目录

```
website/
├── index.html            首页（中文）        en/index.html          首页（English）
├── download/index.html   下载               en/download/index.html
├── docs/index.html       快速开始            en/docs/index.html
├── docs/mcp/index.html   MCP 接入            en/docs/mcp/index.html
├── assets/site.css       唯一样式表
├── assets/fonts/         5 个 woff2 子集（80KB）+ 两份 OFL 许可全文
├── 404.html  robots.txt  sitemap.xml  _headers
└── DESIGN.md  DEPLOY.md
```

## 本地预览

```bash
cd website && python3 -m http.server 8000   # 然后开 http://localhost:8000
```

## 推荐：Cloudflare Pages（域名已在 Cloudflare）—— 已按此部署（2026-08-19）

实际走的是 **wrangler Direct Upload**，不连 GitHub 仓库（发布仓只接收导出结果，
没必要让 Cloudflare 盯着它）。项目名 `aimorsel`，项目域 <https://aimorsel.pages.dev>。

```bash
npx wrangler login                                   # 浏览器授权一次
# 从剔除了 *.md 的副本部署（DEPLOY.md / DESIGN.md 是开发文档，不该上站）
rsync -a --exclude='*.md' website/ /tmp/site/
npx wrangler pages deploy /tmp/site --project-name aimorsel --branch main --commit-dirty=true
```

- 首次建项目：`npx wrangler pages project create aimorsel --production-branch main`。
- 自定义域 `aimorsel.dev` / `www.aimorsel.dev` 已通过 Pages API 绑到项目；
  **wrangler 的 OAuth token 没有 DNS 写权限**，CNAME（`@` 与 `www` → `aimorsel.pages.dev`，开代理）
  要在 Dashboard 建，或用带 `Zone.DNS:Edit` 的 API token 建。
- `ai-morsel.com` 做 301 到 aimorsel.dev（Redirect Rule / Bulk Redirects；zone 里得有一条代理过的占位记录，
  比如 `A @ 192.0.2.1`，跳转规则才会在边缘命中）。
- `_headers` 会被自动读取：字体长缓存、CSS 一小时、全站加两个安全响应头（线上已核验）。
- **改了 `site.css` 必须同时换 HTML 里的版本参数**：9 个页面引用的是 `assets/site.css?v=<前 8 位 sha256>`。
  Cloudflare 边缘会按 `_headers` 把 CSS 缓存数小时（实测 max-age 14400、HIT），HTML 不缓存——只换 CSS 不改引用，
  用户拿到新 HTML + 旧 CSS，新版面全是裸的（2026-08-19 踩过，用户看到的是「没更新」）。wrangler 的 OAuth token 没有
  purge 权限，所以靠改 URL 而不是清缓存。一条命令：
  `H=$(shasum -a 256 website/assets/site.css | cut -c1-8); for f in $(grep -rl 'site.css' website --include='*.html'); do sed -i '' "s#site\.css?v=[0-9a-f]*\"#site.css?v=$H\"#" $f; done`
- 每次改站后重跑一遍线上自检：状态码 / hreflang / 下载直链 302 + Playwright
  （中英 × 桌面/手机：零横向溢出、零 pageerror、零外部请求、自托管字体已加载）。

## 备选：GitHub Pages

Settings → Pages → Deploy from branch → `main` / `website/`。
自定义域名填 aimorsel.dev，DNS 加 CNAME 指向 `<org>.github.io`。
注意 GitHub Pages **不读 `_headers`**，缓存策略会退回默认值。

## 发布前必须做完的事

**占位（现在故意留空，不填假值）**

- [x] 三个平台的下载直链——已填 v1.0.0 的 GitHub Release 直链
      （`download/index.html` 与两个首页的下载卡，中英各三处，共 12 个位置；出新版本时同步改）
- [x] 首次运行提示的措辞——已按 v1.0.0 实测写：macOS 已签名+公证双击直开；Windows 未签名，
      SmartScreen 提示一次走「更多信息 → 仍要运行」。**不要写 `xattr -d com.apple.quarantine`
      之类剥离 Gatekeeper 的命令**——我们签了名，写上去等于自毁「已公证」
- [x] 正式版本号——下载卡的 `.meta` 行写 v1.0.0（站上只此一处，新版本时连同直链一起改）

**上线前的一致性检查**

- [x] 站上写的 CLI 命令是 `morsel`，注册命令是 `morsel mcp`——更名已落到代码和 README（2026-08-19 核对）
- [x] 站上有完整的 MCP 章节，README 中英都有「MCP Server」一节（2026-08-19 补齐中文版）
- [x] 中英两套是手写的，逐页对一遍有没有漂移（2026-08-19 对过一次：结构/链接/代码块/数字零漂移；
      以后每次改站仍要重对）
- [ ] 首页 `#bench` 的评测数字与 `bench/RESULTS.md` 一致（重跑评测后中英首页 + README×2 四处一起改）
- [x] `https://github.com/aimorsel` 下的仓库已公开（2026-08-19），站上所有 GitHub 链接不再 404

**不要做的事**

- 不要为了加个动效引入 JS 框架或 CDN
- 不要把无出处的数据写上站（准确率、token 节省比例之类）。竞品对比已经填了，
  出处是 `bench/RESULTS.md`；再加新数字同样要能点到出处
- 不要在 HTML 里写 `style` 属性，样式一律进 `site.css`
