#!/usr/bin/env python
"""薄壳：日志诊断（等同 `python -m ekits log`）。

用法: python scripts/log_scan.py 日志文件 [--grep 正则] [--encoding gb18030]
"""

from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from ekits.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(["log", *sys.argv[1:]]))
