"""打包版 GUI 入口。"""
import sys

from _bundled_jre import use_bundled_jre

use_bundled_jre()

from morsel_gui import main

sys.exit(main())
