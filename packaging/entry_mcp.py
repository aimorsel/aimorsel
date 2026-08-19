"""打包版 MCP Server 入口。

`morsel mcp` 子命令是常规用法；这个独立可执行是**逃生舱**——当工作目录下正好
有名为 `mcp` 的文件或目录时，子命令会让位给它（见 morsel._dispatch_subcommand）。
"""
import sys

from _bundled_jre import use_bundled_jre

use_bundled_jre()

from morsel_mcp import main

sys.exit(main())
