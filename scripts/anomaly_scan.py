#!/usr/bin/env python
"""薄壳：异常点扫描（等同 `python -m ekits anomaly`）。

用法: python scripts/anomaly_scan.py 数据文件 --column 列名 [--all] [--plot 图.png]
"""

from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from ekits.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(["anomaly", *sys.argv[1:]]))
