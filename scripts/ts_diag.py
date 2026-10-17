#!/usr/bin/env python
"""薄壳：时间序列诊断（等同 `python -m ekits ts`）。

用法: python scripts/ts_diag.py 数据文件 --time 时间列 --value 数值列
"""

from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from ekits.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(["ts", *sys.argv[1:]]))
