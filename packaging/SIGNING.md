# macOS 签名与公证操作手册

给 AImorsel 的 macOS 发行包做代码签名（codesign）+ 公证（notarization），
让用户下载后**双击就能用**，不必去「系统设置 → 隐私与安全性」手动放行。

脚本：`packaging/sign_macos.py`；授权项：`packaging/entitlements.plist`。
签名在 `packaging/build.py` 之后跑，**顺序不能颠倒**（签名后再改二进制会让签名失效）。

---

## 0. 为什么必须做

没签名没公证时，用户下载解压后双击，macOS 直接弹
「无法打开，因为无法验证开发者」，且**没有明显的绕过入口**
（要去系统设置里点「仍要打开」）。对小白等于用不了。

签名和公证是两件事，缺一不可：

| | 作用 | 需要什么 |
|---|---|---|
| **签名** codesign | 证明「这是谁发布的、发布后没被改过」 | Developer ID Application 证书 |
| **公证** notarization | 把包传给 Apple 扫恶意代码，通过后发一张票据 | App Store Connect 凭据 |

只签名不公证，Gatekeeper 照样拦。

---

## 1. 一次性准备（约 1-2 天，主要在等审核）

### 1.1 加入 Apple Developer Program

<https://developer.apple.com/programs/> — **99 美元/年**，个人账号即可
（公司账号要 D-U-N-S 编号，个人开源项目没必要）。

- 用现有 Apple ID 申请，需要开启双重认证
- 个人账号审核通常 24-48 小时，可能有电话/邮件验证
- 签名主体会显示成你的真实姓名（个人账号）或公司名（组织账号）。
  介意署真名的话，这是唯一需要在这一步就定的事

### 1.2 生成 Developer ID Application 证书

「Developer ID Application」是**给 App Store 之外分发的软件**用的类型，
别选成 Apple Development 或 Mac App Distribution。

1. 本机「钥匙串访问」→ 证书助理 → 从证书颁发机构请求证书 → 存到磁盘，
   得到 `CertificateSigningRequest.certSigningRequest`
2. <https://developer.apple.com/account/resources/certificates> → `+`
   → **Developer ID Application** → 上传上一步的 CSR → 下载 `.cer`
3. 双击 `.cer` 导入钥匙串
4. 验证：

> **证书类型页面的三个岔路**（实操时容易卡住）：
> - 这页是**单选（radio）**，不是多选勾选清单。选一个类型点 Continue 即可
> - **Services 那一整节全都不用选**（Apple Push Notification、Pass Type ID、
>   Website Push、Swift Package、WatchKit、VoIP、Apple Pay）——那些是给推送、
>   Wallet 卡券、Swift 包用的，与桌面应用分发无关。
>   **Developer ID Application 在 Software 分组**，往下滚接近底部，
>   紧邻 Developer ID Installer（Installer 那个是签 `.pkg` 用的，我们出 DMG 不需要）
> - 接着会问 **Sub-CA**：选 **G2 Sub-CA**（Xcode 11.4.1+ 起的当前标准）。
>   Previous Sub-CA 只为兼容老工具链，新证书没有理由选它
>
> 另外 Developer ID 证书**只有 Account Holder 能创建**——个人账号就是你自己，
> 不受影响；组织账号需要找账号持有人。


```bash
security find-identity -v -p codesigning
# 应出现：1) XXXXXX "Developer ID Application: 你的名字 (TEAMID)"
```

> **实操必踩：导入 `.cer` 后仍是 `0 valid identities found`**（2026-07-25 遇到）。
> 原因是**本机缺 Apple 的 G2 中间证书**，证书链断在半路，证书被判不可信。
> 诊断方法——去掉 `-v` 看全部 identity：
>
> ```bash
> security find-identity -p codesigning        # 若这里能列出，说明私钥配对没问题
> security find-certificate -a -c "Developer ID Certification Authority" | grep -c labl
> ```
>
> 第一条列得出、第二条返回 0，就是缺中间证书。补上即可：
>
> ```bash
> curl -fsSLO https://www.apple.com/certificateauthority/DeveloperIDG2CA.cer
> security import DeveloperIDG2CA.cer -k ~/Library/Keychains/login.keychain-db
> security find-identity -v -p codesigning     # 这次应显示 1 valid identities found
> ```
>
> 中间证书导进 login 钥匙串就够（它由系统已信任的 Apple Root CA 签发，
> 不需要单独设信任）。Xcode 装全的机器通常自带，纯命令行环境和 CI runner 常缺。
> 别被 `0 valid` 误导成「私钥没导进去」而重新申请证书——
> `find-identity` 不带 `-v` 能列出来就证明私钥是好的。

签名可用性冒烟（不动项目产物，签个 `/bin/ls` 副本试）：

```bash
cp /bin/ls /tmp/smoke_bin
codesign --force --timestamp --options runtime \
  --entitlements packaging/entitlements.plist \
  --sign "Developer ID Application: 你的名字 (TEAMID)" /tmp/smoke_bin
codesign -dvvv /tmp/smoke_bin 2>&1 | grep -E "Authority|Timestamp|flags"
```

要能同时看到 `flags=0x10000(runtime)`、`Timestamp=...`（时间戳服务可达）
和三级 Authority 链，才算真的可以签。

> **立刻备份私钥**：钥匙串访问里选中该证书 → 右键导出为 `.p12`（设个强密码），
> 存到密码管理器。私钥丢了证书就废了，只能吊销重签。CI 也要用这个 `.p12`。
> 每个 team 最多 5 个 Developer ID Application 证书，别浪费。

### 1.3 准备公证凭据

推荐 **App Store Connect API Key**（比 Apple ID + 专用密码更适合 CI，且不受
密码变更影响）：

1. <https://appstoreconnect.apple.com/access/integrations/api> →
   Team Keys → 生成新 key，角色选 **Developer**（够用）
2. 下载 `AuthKey_XXXXXXXX.p8`——**只能下载一次**，立刻存好
3. 记下 **Key ID** 和页面上方的 **Issuer ID**（UUID）
4. 存进钥匙串，之后就只用 profile 名字：

```bash
xcrun notarytool store-credentials "aimorsel-notary" \
  --key ~/secure/AuthKey_XXXXXXXX.p8 \
  --key-id XXXXXXXX \
  --issuer xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
```

---

## 2. 本机走一遍

```bash
# 构建（无证书也能跑，产出 adhoc 签名的包）
python3 packaging/build.py --no-zip

# 签名 + 打 DMG + 公证 + 钉票据
python3 packaging/sign_macos.py --dmg --notarize \
  --profile aimorsel-notary --version v1.0.0
```

两个脚本的路径都基于自身位置（`__file__`）解析，**不依赖当前工作目录**，
所以从任何地方用绝对路径调用都可以：

```bash
python3 /path/to/aimorsel/packaging/sign_macos.py --dmg --notarize --profile aimorsel-notary
```

用相对路径时才需要先 `cd` 到项目根目录，否则报
`can't open file '//packaging/sign_macos.py'`。

耗时预期：签名阶段每个文件都要向 Apple 请求一次安全时间戳，237 个文件约几分钟。
公证 Apple 常说「90% 在 15 分钟内」，但**实测 40 分钟以上也会发生**
（2026-07-25 首次提交：162MB、内含 237 个待扫描二进制，等了 40+ 分钟仍 In Progress，
同时 Apple 系统状态页显示 Notary Service 无故障——纯粹是排队）。
所以 CI 里给 job 设了 `timeout-minutes: 90`：`notarytool --wait` 不会自己放弃，
而 GitHub Actions job 默认要 6 小时才超时，**macOS runner 按 10 倍计费**，
一次卡死就烧掉 3600 分钟额度。

卡住时的判断顺序：① `xcrun notarytool history --keychain-profile <名>` 看 Apple 侧
真实状态（别看脚本日志）；② <https://developer.apple.com/system-status/> 确认
Developer ID Notary Service 有没有故障；③ 都正常就只能等，`In Progress` 不是错误。

脚本做的事：

1. 扫出包内**全部 237 个 Mach-O**（231 个 `.so`/`.dylib` + 6 个可执行），
   先签库、后签可执行
2. 可执行带 `entitlements.plist`，库不带
3. 全部加 `--timestamp`（安全时间戳）和 `--options runtime`（Hardened Runtime），
   两者都是公证的硬性要求
4. 自检：逐个 `codesign --verify --strict`，并确认 6 个可执行确实拿到了
   runtime 标志和 JIT 授权——**一次列出所有问题**，不用来回试
5. 打 DMG → 签 DMG → 提交公证并等结果 → `stapler staple`

只想检查现有产物：

```bash
python3 packaging/sign_macos.py --verify-only
```

### 为什么不能 `codesign --deep`

`dist/morsel/` **不是 `.app` bundle**，只是一个装着散装二进制的普通目录。
`--deep` 只对 bundle 递归，对普通目录不生效，必须逐个签。
（另外 Apple 官方也不推荐 `--deep`，它会用同一套 entitlements 覆盖嵌套内容。）

### entitlements 为什么是这三项

| 授权项 | 谁需要 |
|---|---|
| `allow-jit` | 捆绑 JRE 的 JVM 要可写可执行内存。OpenJDK 官方二进制也是这么签的 |
| `allow-unsigned-executable-memory` | 同上，JVM 需要两项一起给，少一个 java 会直接崩 |
| `disable-library-validation` | PyInstaller 运行时 `dlopen` 两百多个 `.so` |

三项都是「自助生效」的豁免，不需要 Apple 单独审批。

### 已知要签的特殊文件

- `_internal/jre/lib/jspawnhelper` — Java 起子进程的辅助程序。
  漏签它，转换 PDF 时 JVM 起不来
- `_internal/tkinterdnd2/tkdnd/osx-x64/libtkdnd2.9.4.dylib` —
  **出厂完全没有签名**的 x86_64 库（arm64 包里是死代码，但公证照样会因它被拒）。
  脚本会一并签上

---

## 3. 分发形态：这一步要做个决定

公证票据能不能「钉」进产物，决定了用户**离线时**能否直接打开。
Apple 不支持给 zip 钉票据（格式里没有存放位置）。

| 形态 | 能钉票据 | 从包里拖出后仍带票据 | 用户体验 |
|---|---|---|---|
| zip | ❌ | — | 首次运行**必须联网**让 Gatekeeper 在线核验 |
| DMG 装文件夹（**当前**） | ✅ DMG 本身 | ❌ | 直接从 DMG 运行免联网；拖出来的可执行需在线核验 |
| **`.app` + DMG** | ✅ 两者都能 | ✅ | 双击即用，拖到哪都离线可用 |

**2026-07-25 实测**（已公证 + staple 的 DMG，打上 Safari 的 quarantine 标记模拟下载）：

- `spctl -a -t install <dmg>` → `accepted` / `source=Notarized Developer ID`
- **挂载 DMG 直接运行包内 `morsel`：完全无障碍**，真实转换成功，
  用的是包内 `_internal/jre/bin/java`。DMG 的票据让这一步不需要联网
- **把文件夹拖出 DMG 后**：复制品**继承了 `com.apple.quarantine`**，
  签名仍然有效（`codesign --verify --strict` 通过），联网下能正常运行——
  但它没有可存放票据的位置，所以这一步靠的是 Gatekeeper **在线**核验。
  这正是「只有 `.app` 拖出后仍带票据」这个结论的实测依据
- `spctl -a -vvv <包内可执行>` 会报
  `rejected (the code is valid but does not seem to be an app)`。
  **这不是签名或公证问题**——注意 `the code is valid`，spctl 的默认评估针对
  `.app` bundle，对裸 Unix 可执行文件语义不适用。程序实际能跑就是证明。
  这条也从另一个角度说明：做成 `.app` 才能让各种系统工具的判断都对得上

现状还有个体验问题：`morsel-gui` 是个裸 Unix 可执行文件（`console=False`），
Finder 里双击它 macOS 会拿 Terminal 打开——对小白很怪。

**建议**：先用 `--dmg` 上线（离线场景只影响首次运行），
下一步把 GUI 包成 `AImorsel.app`（PyInstaller 的 `BUNDLE`），
CLI 放进 `AImorsel.app/Contents/MacOS/`，一次解决 staple 和双击体验两个问题。
那时 DMG 里放 `.app` + Applications 的符号链接，就是标准 mac 安装观感。

---

## 4. 验证成果（模拟真实用户）

下载来的文件带 `com.apple.quarantine` 属性，Gatekeeper 才会介入。
本地构建的产物没有这个属性，所以**必须手动加上再测**，否则测不出真实行为：

```bash
# 1) 票据确实钉上了
xcrun stapler validate dist/morsel-v1.0.0-macos-arm64.dmg

# 2) Gatekeeper 放行
spctl -a -vvv -t install dist/morsel-v1.0.0-macos-arm64.dmg

# 3) 模拟「从浏览器下载」，这是最接近真实的一步
xattr -w com.apple.quarantine "0083;00000000;Safari;" /tmp/test.dmg
open /tmp/test.dmg          # 不该弹「无法验证开发者」

# 4) 断网再试一次，验证 staple 是否真的生效
```

最有说服力的验证是**换一台没装过开发工具、没登过你 Apple ID 的 Mac**。

---

## 5. 接进 CI（`release.yml`）

需要 **5 个** GitHub Secrets：

| Secret | 内容 |
|---|---|
| `MACOS_CERT_P12` | 证书 `.p12` 的 base64 |
| `MACOS_CERT_PASSWORD` | 导出 `.p12` 时设的密码 |
| `MACOS_NOTARY_KEY_P8` | `AuthKey_*.p8` 的 base64 |
| `MACOS_NOTARY_KEY_ID` | API Key ID（就在 `.p8` 文件名里） |
| `MACOS_NOTARY_ISSUER` | Issuer UUID（App Store Connect，全团队共用一个） |

**用 `gh` 管道写入，别走剪贴板**——base64 后的私钥不落盘、不进剪贴板、不进
shell 历史（值从 stdin 进，不出现在进程列表里）：

```bash
base64 < DeveloperID_Application.p12 | tr -d '\n' | gh secret set MACOS_CERT_P12
base64 < AuthKey_XXXXXXXX.p8         | tr -d '\n' | gh secret set MACOS_NOTARY_KEY_P8
printf '%s' "XXXXXXXX"                            | gh secret set MACOS_NOTARY_KEY_ID
printf '%s' "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" | gh secret set MACOS_NOTARY_ISSUER
```

`.p12` 密码别写进命令行（会进 shell 历史）。用静默读取：

```bash
read -rs -p "p12 密码: " PW && printf '%s' "$PW" | gh secret set MACOS_CERT_PASSWORD && unset PW
```

`gh secret list` 只能看到名称和更新时间——**值写进去就再也读不出来**，
所以本地那份备份不能丢。密码记不准时先验证再设（交互输入，不进历史）：

```bash
openssl pkcs12 -in DeveloperID_Application.p12 -noout   # 静默成功 = 密码正确
```

runner 上导入证书的关键三步（**`set-key-partition-list` 千万别漏**，
否则 codesign 会弹密码框、CI 直接挂到超时）：

```bash
security create-keychain -p "$TMP_PWD" build.keychain
security set-keychain-settings -lut 21600 build.keychain      # 别让它中途自动锁
security unlock-keychain -p "$TMP_PWD" build.keychain
security import cert.p12 -k build.keychain -P "$CERT_PWD" \
        -T /usr/bin/codesign -T /usr/bin/security
security set-key-partition-list -S apple-tool:,apple:,codesign: \
        -s -k "$TMP_PWD" build.keychain                        # ← 漏了 CI 会卡死
security list-keychains -d user -s build.keychain login.keychain-db
```

**已接进 `release.yml`**：配齐上面的 secrets 后，macOS 两个 runner 自动走
签名 → 公证 → DMG；没配就照旧出未签名 zip，fork 和 `workflow_dispatch` 不会红。

坑：`secrets` 上下文**在 step 的 `if:` 里不可用**（只有 `github`/`env`/`steps`
等能用）。所以 workflow 里先用一个 step 把「有没有配凭据」写进
`$GITHUB_OUTPUT`，后面的 step 判断 `steps.signing.outputs.enabled`。
直接写 `if: secrets.X != ''` 会恒为假、签名步骤永远被跳过。

冒烟测试排在签名**之后**：entitlements 配错会让程序秒退（`Killed: 9`），
而公证本身不校验 entitlements 对不对，只有真跑一次才能发现。

> Windows 签名是另一套（需要 OV/EV 代码签名证书，硬件 token 或云 HSM，
> 每年更贵）。本手册只管 macOS。

---

## 6. 排错

**公证被拒**——`sign_macos.py` 会自动拉日志。也可手动：

```bash
xcrun notarytool log <submission-id> --keychain-profile aimorsel-notary
```

日志的 `issues` 数组会精确指出哪个文件什么问题。按出现频率排：

| 日志里的话 | 原因 | 解法 |
|---|---|---|
| `not signed at all` | 漏签某个 `.so`/`.dylib` | 跑 `--verify-only` 定位；别手工签，用脚本全量签 |
| `does not include a secure timestamp` | 漏了 `--timestamp` | 脚本已带；手工签过的文件重签 |
| `not have the hardened runtime enabled` | 漏了 `--options runtime` | 同上 |
| `disallowed entitlement` | 用了需审批的授权项 | 只保留手册里那三项 |

**签名后程序跑不起来**

- `Killed: 9` / 秒退 → 大概率 entitlements 不对。先 `--verify-only`，
  再看 `log show --predicate 'sender == "kernel"' --last 5m` 里的 codesign 拒绝记录
- Java 起不来 → 确认 `jre/bin/java` 和 `jre/lib/jspawnhelper` 都签了、都有 JIT 授权
- `MORSEL_DEBUG_JAVA=1` 可打印实际用的 java 路径

**证书过期了怎么办**：加了 `--timestamp` 的签名**在证书过期后依然有效**，
已发布的包不受影响。只是不能用它签新东西了。

**改了 `config.toml` 会不会破坏签名**：不会。它不是 Mach-O，
而且这个包没有 bundle 的资源清单，改非二进制文件不影响任何签名。
同理，程序首次运行在旁边建 `raw/`、`output/` 也没影响。
