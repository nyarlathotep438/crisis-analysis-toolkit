#!/usr/bin/env python
"""薄壳：编码与文本诊断 / 转码（等同 `python -m ekits encoding`）。

用法: python scripts/encoding_fix.py 文件 [--hex] [--convert utf-8 --out 新文件.csv]
"""

from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from ekits.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(["encoding", *sys.argv[1:]]))
