#!/usr/bin/env python
"""薄壳：生成"带病"演练数据（等同 `python -m ekits demo`）。

用法: python scripts/make_demo_data.py [--out-dir samples]
"""

from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from ekits.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(["demo", *sys.argv[1:]]))
