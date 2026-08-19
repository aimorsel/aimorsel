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

## 推荐：Cloudflare Pages（域名已在 Cloudflare）

1. Dashboard → Workers & Pages → Create → Pages → 连接 GitHub 仓库
   （或 Direct Upload 直接拖 `website/` 文件夹——仓库还私有时用这个最省事）
2. 构建设置：Framework = **None**，Build command **留空**，Output directory = `website`
3. Custom domains 绑 **aimorsel.dev**；`ai-morsel.com` 做 301 跳到 aimorsel.dev
   （Bulk Redirects 或 Page Rule）
4. `_headers` 会被自动读取：字体长缓存、CSS 一小时、全站加两个安全响应头

## 备选：GitHub Pages

Settings → Pages → Deploy from branch → `main` / `website/`。
自定义域名填 aimorsel.dev，DNS 加 CNAME 指向 `<org>.github.io`。
注意 GitHub Pages **不读 `_headers`**，缓存策略会退回默认值。

## 发布前必须做完的事

**占位（现在故意留空，不填假值）**

- [ ] 三个平台的下载直链——首个正式 Release 出来后填进
      `download/index.html` 与两个首页的下载卡（中英各三处，共 12 个位置）
- [ ] 首次运行提示的措辞——签名/公证的实际行为，等首个正式版实跑后按真实情况写
      （`download/index.html` 里那条橙色提示条，中英各一处）
- [ ] 正式版本号（当前站上任何地方都没有版本号，这是有意的）

**上线前的一致性检查**

- [ ] 站上写的 CLI 命令是 `morsel`，注册命令是 `morsel mcp`——
      **确认更名已经落到代码和 README**，否则用户照着敲会失败
- [ ] 站上有完整的 MCP 章节，**确认 README 也补了 MCP**，否则点进 GitHub 对不上
- [ ] 中英两套是手写的，**逐页对一遍有没有漂移**（没有自动检查）
- [ ] 首页 `#bench` 的评测数字与 `bench/RESULTS.md` 一致（重跑评测后中英首页 + README×2 四处一起改）
- [ ] `https://github.com/aimorsel` 下的仓库已公开，站上所有 GitHub 链接不再 404

**不要做的事**

- 不要为了加个动效引入 JS 框架或 CDN
- 不要把无出处的数据写上站（准确率、token 节省比例之类）。竞品对比已经填了，
  出处是 `bench/RESULTS.md`；再加新数字同样要能点到出处
- 不要在 HTML 里写 `style` 属性，样式一律进 `site.css`
