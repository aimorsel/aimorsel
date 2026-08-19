#!/usr/bin/env python3
"""macOS 代码签名 + 公证（notarization）。在 packaging/build.py 之后跑。

    # 1) 只签名（本机验证用，不联网）
    python packaging/sign_macos.py

    # 2) 签名 + 打 DMG + 公证 + staple（推荐的发布形态，装完离线也能开）
    python packaging/sign_macos.py --dmg --notarize --profile aimorsel-notary

    # 3) 签名 + 打 zip + 公证（zip 无法 staple，用户首次运行需联网）
    python packaging/sign_macos.py --zip --notarize --profile aimorsel-notary

    # 4) 只检查现有产物的签名状态
    python packaging/sign_macos.py --verify-only

前置：Apple Developer Program 会员 + 本机 keychain 里有「Developer ID Application」
证书；公证还需要 `xcrun notarytool store-credentials` 存好的 profile
（或用 --key/--key-id/--issuer 直接传 App Store Connect API key）。
详见 packaging/SIGNING.md。

为什么不能简单 `codesign --deep`：dist/morsel/ 不是 .app bundle，而是一堆散装
Mach-O（3 个 PyInstaller 可执行 + 捆绑 JRE 的 java/keytool/jspawnhelper +
两百多个 .so/.dylib）。--deep 对非 bundle 目录不生效，必须逐个签，且顺序是
先库后可执行。
"""

from __future__ import annotations

import argparse
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"
DIST = ROOT / "dist" / "morsel"
ENTITLEMENTS = PACKAGING / "entitlements.plist"

# 平台标签跟 build.py 共用一处，别各写一份——CI 里 macos-13 是 x86_64，
# 硬编码 arm64 会产出名字骗人的包。
sys.path.insert(0, str(PACKAGING))
from build import platform_label  # noqa: E402

# 产物名与 build.py 的 zip 保持一致。将来品牌改名 AImorsel 时，
# 这里和 build.py 的 make_zip 要一起改。
PRODUCT_NAME = "morsel"

# Mach-O / fat 二进制的魔数。0xcafebabe 同时也是 Java .class 的魔数，
# 所以魔数只用来筛候选，最终类型交给 file(1) 判断。
MACHO_MAGICS = {
    b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf",   # 64 位
    b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xce",   # 32 位
    b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",   # fat
}


def run(cmd, *, capture=False, check=True) -> subprocess.CompletedProcess:
    printable = " ".join(str(c) for c in cmd)
    if not capture:
        print("==>", printable, flush=True)
    return subprocess.run([str(c) for c in cmd], check=check, text=True,
                          capture_output=capture)


def find_identity(explicit: str | None) -> str:
    """确定签名身份：显式指定优先，否则从 keychain 里找唯一的 Developer ID Application。"""
    if explicit:
        return explicit
    out = run(["security", "find-identity", "-v", "-p", "codesigning"],
              capture=True, check=False).stdout
    names = re.findall(r'"(Developer ID Application:[^"]+)"', out)
    unique = sorted(set(names))
    if not unique:
        sys.exit(
            "keychain 里没有「Developer ID Application」证书。\n"
            "需要先加入 Apple Developer Program 并导入证书，步骤见 packaging/SIGNING.md。\n"
            f'当前 codesigning 身份：\n{out.strip() or "  （无）"}'
        )
    if len(unique) > 1:
        joined = "\n".join(f"  {n}" for n in unique)
        sys.exit(f"找到多个签名身份，请用 --identity 指定其中一个：\n{joined}")
    print(f"签名身份：{unique[0]}")
    return unique[0]


def classify_machos(root: Path) -> tuple[list[Path], list[Path]]:
    """遍历产物目录，返回 (库文件, 可执行文件)。符号链接跳过（jre 里有几十个）。"""
    candidates: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            with path.open("rb") as fh:
                if fh.read(4) in MACHO_MAGICS:
                    candidates.append(path)
        except OSError:
            continue

    libs: list[Path] = []
    execs: list[Path] = []
    # file(1) 批量调用，避免几百次进程启动；分批防 argv 超长。
    # ⚠️ 不能用 -b 后按行 zip 对齐：macOS 15 的 file 对 universal(fat) 二进制
    # 输出多行（每架构一行），行数不齐全体错位（CI 实测，本机 macOS 14 单行无恙）。
    # 改为不带 -b，按「路径: 描述」前缀归并，续行拼给上一个文件。
    for i in range(0, len(candidates), 200):
        batch = candidates[i:i + 200]
        out = run(["file", *batch], capture=True).stdout.splitlines()
        descs: dict[str, str] = {str(p): "" for p in batch}
        current: str | None = None
        for line in out:
            prefix, sep, rest = line.partition(": ")
            if sep and prefix in descs:
                current = prefix
                descs[prefix] += rest
            elif current is not None:
                descs[current] += " " + line.strip()  # fat 二进制的续行
        for path in batch:
            desc = descs[str(path)]
            if "Mach-O" not in desc:
                continue          # 例如 Java .class，魔数撞车
            if "executable" in desc:
                execs.append(path)
            else:                 # dynamically linked shared library / bundle
                libs.append(path)
    return libs, execs


def sign(paths: list[Path], identity: str, *, entitlements: bool) -> None:
    """逐个签名。库不带 entitlements，可执行带（JVM 的 JIT 权限等）。

    --force：包内二进制出厂是 adhoc 签名（PyInstaller 和 jlink 都会 adhoc 签），
             不加会因「已有签名」而跳过。
    --timestamp：安全时间戳，公证的硬性要求，需要联网。
    --options runtime：启用 Hardened Runtime，同样是公证的硬性要求。
    """
    base = ["codesign", "--force", "--timestamp", "--options", "runtime",
            "--sign", identity]
    if entitlements:
        base += ["--entitlements", ENTITLEMENTS]
    for i, path in enumerate(paths, 1):
        rel = path.relative_to(ROOT)
        print(f"  [{i}/{len(paths)}] {rel}", flush=True)
        result = run([*base, path], capture=True, check=False)
        if result.returncode != 0:
            sys.exit(f"签名失败：{rel}\n{result.stderr.strip()}")


def verify(root: Path) -> None:
    """校验签名完整性，并确认关键可执行拿到了 runtime 标志和 entitlements。

    一次列全部问题再退出：公证被拒最常见的原因就是漏签某个库，
    一个个报错要来回好几轮。
    """
    print("\n--- 校验签名 ---")
    libs, execs = classify_machos(root)
    problems: list[str] = []

    for path in libs + execs:
        result = run(["codesign", "--verify", "--strict", path],
                     capture=True, check=False)
        if result.returncode != 0:
            lines = result.stderr.strip().splitlines()
            # codesign 的报错行是 "<绝对路径>: 原因"，只留原因
            reason = lines[0].split(": ", 1)[-1] if lines else "校验失败"
            problems.append(f"{path.relative_to(root)}：{reason}")
    ok_count = len(libs) + len(execs) - len(problems)
    print(f"签名完整 {ok_count}/{len(libs) + len(execs)} 个 Mach-O。")

    for path in execs:
        info = run(["codesign", "--display", "--verbose=2", path],
                   capture=True, check=False).stderr
        has_runtime = "runtime" in info
        ents = run(["codesign", "--display", "--entitlements", "-",
                    "--xml", path], capture=True, check=False).stdout
        has_jit = "allow-jit" in ents
        good = has_runtime and has_jit
        print(f"  {'✓' if good else '✗'} {path.relative_to(root)}  "
              f"runtime={'是' if has_runtime else '否'} "
              f"jit授权={'是' if has_jit else '否'}")
        if not good:
            problems.append(f"{path.relative_to(root)}：缺 Hardened Runtime 或 JIT 授权")

    if problems:
        listed = "\n".join(f"  - {p}" for p in problems)
        sys.exit(f"\n{len(problems)} 项待解决（公证会因此被拒）：\n{listed}")


def make_dmg(version: str) -> Path:
    """压缩 DMG。DMG 是唯一能 staple 公证票据、且拖出后仍有效的实用形态之一。"""
    tag = f"{version}-" if version else ""
    out = ROOT / "dist" / f"{PRODUCT_NAME}-{tag}{platform_label()}.dmg"
    out.unlink(missing_ok=True)
    run(["hdiutil", "create", "-volname", PRODUCT_NAME, "-srcfolder", DIST,
         "-ov", "-format", "UDZO", out])
    return out


def make_zip(version: str) -> Path:
    """ditto 打 zip（保留可执行位、符号链接和签名所依赖的扩展属性）。"""
    tag = f"{version}-" if version else ""
    out = ROOT / "dist" / f"{PRODUCT_NAME}-{tag}{platform_label()}.zip"
    out.unlink(missing_ok=True)
    run(["ditto", "-c", "-k", "--keepParent", DIST, out])
    return out


def notary_auth(args) -> list[str]:
    if args.profile:
        return ["--keychain-profile", args.profile]
    if args.key and args.key_id and args.issuer:
        return ["--key", args.key, "--key-id", args.key_id, "--issuer", args.issuer]
    sys.exit("公证需要凭据：给 --profile，或同时给 --key/--key-id/--issuer。"
             "见 packaging/SIGNING.md。")


def notarize(target: Path, auth: list[str]) -> None:
    """提交公证并等待结果。被拒时自动拉日志——日志是唯一能看出哪个文件出问题的地方。"""
    print(f"\n--- 提交公证：{target.name} ---"
          "（Apple 侧排队，实测 40 分钟以上也属正常，本包 162MB / 237 个二进制）\n"
          "  另开终端可查真实状态：xcrun notarytool history --keychain-profile <名>")
    result = run(["xcrun", "notarytool", "submit", target, *auth,
                  "--wait", "--output-format", "plist"],
                 capture=True, check=False)
    try:
        info = plistlib.loads(result.stdout.encode())
    except Exception:
        sys.exit(f"无法解析 notarytool 输出：\n{result.stdout}\n{result.stderr}")

    status = info.get("status", "unknown")
    submission_id = info.get("id", "")
    print(f"公证状态：{status}（id={submission_id}）")
    if status != "Accepted":
        if submission_id:
            print("\n--- 公证日志（被拒原因在 issues 里）---")
            run(["xcrun", "notarytool", "log", submission_id, *auth], check=False)
        sys.exit("公证未通过。常见原因：漏签某个 .so、缺 --timestamp、缺 Hardened Runtime。")


def staple(target: Path) -> None:
    """把票据钉进产物。zip 没有存放票据的位置，Apple 不支持，只能靠联网在线校验。"""
    if target.suffix == ".zip":
        print("\nzip 无法 staple 公证票据（格式没有存放位置），"
              "用户首次运行需联网让 Gatekeeper 在线核验。")
        return
    run(["xcrun", "stapler", "staple", target])
    run(["xcrun", "stapler", "validate", target])
    print("票据已钉入，离线也能通过 Gatekeeper。")


def main() -> int:
    parser = argparse.ArgumentParser(description="macOS 签名 + 公证")
    parser.add_argument("--identity", help="签名身份；省略则自动找唯一的 Developer ID Application")
    parser.add_argument("--version", default="", help="写进产物文件名的版本号")
    parser.add_argument("--dmg", action="store_true", help="打 DMG（可 staple，推荐）")
    parser.add_argument("--zip", action="store_true", help="打 zip（无法 staple）")
    parser.add_argument("--notarize", action="store_true", help="提交公证并 staple")
    parser.add_argument("--profile", help="notarytool 的 keychain profile 名")
    parser.add_argument("--key", help="App Store Connect API key 的 .p8 路径")
    parser.add_argument("--key-id", help="API key ID")
    parser.add_argument("--issuer", help="API key 的 issuer UUID")
    parser.add_argument("--verify-only", action="store_true", help="只校验现有签名")
    args = parser.parse_args()

    # stdout 重定向到文件/管道时是块缓冲，而这个脚本要跑十几分钟（237 次时间戳
    # 请求 + 公证等待）。不改成行缓冲的话日志全憋在缓冲区，从外部看像卡死了。
    sys.stdout.reconfigure(line_buffering=True)

    if sys.platform != "darwin":
        sys.exit("这个脚本只在 macOS 上有意义。")
    if not DIST.is_dir():
        sys.exit(f"没找到 {DIST}，先跑 python packaging/build.py")
    if not shutil.which("xcrun"):
        sys.exit("找不到 xcrun：请装 Xcode Command Line Tools（xcode-select --install）")

    if args.verify_only:
        verify(DIST)
        return 0

    if not ENTITLEMENTS.is_file():
        sys.exit(f"缺少 {ENTITLEMENTS}")
    identity = find_identity(args.identity)

    libs, execs = classify_machos(DIST)
    print(f"\n--- 签名 {len(libs)} 个库 ---")
    sign(libs, identity, entitlements=False)
    print(f"\n--- 签名 {len(execs)} 个可执行（带 entitlements）---")
    sign(execs, identity, entitlements=True)
    verify(DIST)

    targets = []
    if args.dmg:
        targets.append(make_dmg(args.version))
    if args.zip:
        targets.append(make_zip(args.version))
    if not targets:
        print("\n签名完成（未打包，需要发行包请加 --dmg 或 --zip）。")
        return 0

    if args.notarize:
        auth = notary_auth(args)
        for target in targets:
            # DMG 本身也要签名，否则公证会以「未签名」被拒
            if target.suffix == ".dmg":
                run(["codesign", "--force", "--timestamp", "--sign", identity, target])
            notarize(target, auth)
            staple(target)

    print("\n完成：")
    for target in targets:
        size = target.stat().st_size / 1024 / 1024
        print(f"  {target}（{size:.0f}MB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
