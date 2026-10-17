#!/usr/bin/env python
"""薄壳：出图（等同 `python -m ekits plot`）。

用法: python scripts/plot_quick.py 数据文件 --column 列名 --plot-out 出图.png
"""

from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from ekits.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(["plot", *sys.argv[1:]]))
