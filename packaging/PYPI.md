# PyPI 发布手册

两个包：**`aimorsel`**（真包，全部代码 + `morsel` 等四个命令）和 **`morsel`**（别名包，只声明
`dependencies = ["aimorsel>=X"]`，让 `pip install morsel` 也能装到）。两个名字都已在 PyPI 注册在同一账号下。

## 每次发版

```bash
# 0. 版本号单一来源：aimorsel/__init__.py 的 __version__；别名包 packaging/pypi-morsel-alias/pyproject.toml
#    的 version 与 dependencies 下限同步改。CHANGELOG.md 把 [Unreleased] 改成版本 + 日期。
pytest

# 1. 构建 + 自检（两个包）
rm -rf dist/pypi dist/pypi-alias
python -m build --outdir dist/pypi
( cd packaging/pypi-morsel-alias && python -m build --outdir ../../dist/pypi-alias )
python -m twine check dist/pypi/* dist/pypi-alias/*
tar tzf dist/pypi/aimorsel-*.tar.gz | awk -F/ '{print $2}' | sort | uniq -c   # sdist 里只该有 aimorsel/ + 几份根文件

# 2. 干净环境装一遍 wheel 再试命令（别跳过：CI 也做这一步，但本机先看一眼更快）
python -m venv /tmp/v && /tmp/v/bin/pip install dist/pypi/aimorsel-*.whl
( cd /tmp && /tmp/v/bin/morsel --version && /tmp/v/bin/morsel mcp --help )

# 3. 上传（需要 PyPI API token；先传真包再传别名包，别名包依赖真包）
python -m twine upload dist/pypi/*
python -m twine upload dist/pypi-alias/*
```

凭据：`~/.pypirc` 或环境变量 `TWINE_USERNAME=__token__` + `TWINE_PASSWORD=pypi-…`。
本仓库不存 token；建议用 PyPI 的 **Trusted Publishing**（GitHub Actions OIDC）把上传挪进
`release.yml`，这样以后推 tag 就自动发——配置方法见 PyPI 项目页 Publishing 一栏，
workflow 侧用 `pypa/gh-action-pypi-publish`。

## 上线后要补的事

- README 中英加 PyPI 徽章：`[![PyPI](https://img.shields.io/pypi/v/aimorsel)](https://pypi.org/project/aimorsel/)`
- 官网 `download/` 中英加 `pip install "aimorsel[all]"` 一段（现在只写了源码方式的 `pip install -e`）
- GitHub Release Notes 补一节 `pip install aimorsel` 并链到 PyPI 页面

## 坑

- hatch sdist 的 `include = ["aimorsel/"]` 按 gitignore 语义匹配**任何**含 `aimorsel/` 的路径，
  会把 `bench/results/out/aimorsel/…` 186 MB 卷进去；必须用 `only-include`（锚定到根）。
- 别名包没有任何模块，hatch 会拒绝打空 wheel，要 `[tool.hatch.build.targets.wheel] bypass-selection = true`。
- PyPI 的版本号**用过即废**：传错了只能删文件不能重传同号，先 `twine check` + 干净 venv 实测再传。
- JRE 不进 pip 包（51 MB 且平台相关）；pip 用户必须自备 Java 11+，README「安装」表里已写清。
