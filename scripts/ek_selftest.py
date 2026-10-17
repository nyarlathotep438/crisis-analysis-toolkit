#!/usr/bin/env python
"""薄壳：离线自检（等同 `python -m ekits selftest`）。

工具真出事的时候，先跑这个确认"是工具坏了还是数据坏了"。
用法: python scripts/ek_selftest.py
"""

from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from ekits.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(["selftest", *sys.argv[1:]]))
