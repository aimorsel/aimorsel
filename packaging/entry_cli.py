"""打包版 CLI 入口。"""
import sys

from _bundled_jre import use_bundled_jre

use_bundled_jre()

from morsel import main

sys.exit(main())
