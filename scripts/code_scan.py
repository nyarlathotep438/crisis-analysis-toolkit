#!/usr/bin/env python
"""薄壳：代码静态体检（等同 `python -m ekits code`）。

用法: python scripts/code_scan.py 路径 [--no-imports] [--limit 60]
"""

from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from ekits.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(["code", *sys.argv[1:]]))
