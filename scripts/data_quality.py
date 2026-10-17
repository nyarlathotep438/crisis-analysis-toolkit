#!/usr/bin/env python
"""薄壳：数据体检（等同 `python -m ekits quality`）。

用法: python scripts/data_quality.py 数据文件 [--key 主键列] [--out 报告.md]
"""

from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from ekits.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(["quality", *sys.argv[1:]]))
