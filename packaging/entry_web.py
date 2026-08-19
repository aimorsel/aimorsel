"""打包版 Web 常驻服务入口。"""
import sys

from _bundled_jre import use_bundled_jre

use_bundled_jre()

from morsel_web import main

sys.exit(main())
