#!/bin/zsh
# 兼容旧入口：真正的构建逻辑在跨平台的 packaging/build.py（macOS/Windows 通用）。
# 用法：packaging/build.sh；构建环境可用 MORSEL_PY_ENV=/path/to/env/bin 覆盖。
set -e
cd "$(dirname "$0")/.."
PY_BIN=${MORSEL_PY_ENV:-$(dirname "$(command -v python3)")}
exec "$PY_BIN/python3" packaging/build.py "$@"
