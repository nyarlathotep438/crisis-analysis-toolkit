#!/usr/bin/env python
"""薄壳：数据结构速览（等同 `python -m ekits profile`）。

用法: python scripts/data_profile.py 数据文件 [--columns a,b] [--hist] [--out 报告.md]
"""

from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from ekits.cli import main  # noqa: E402  必须在 sys.path 调整之后导入

if __name__ == "__main__":
    sys.exit(main(["profile", *sys.argv[1:]]))
