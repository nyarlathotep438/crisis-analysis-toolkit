#!/usr/bin/env python
"""薄壳：目录体检（等同 `python -m ekits fs`）。

用法: python scripts/fs_check.py 目录 [--no-duplicates] [--top 20]
"""

from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from ekits.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(["fs", *sys.argv[1:]]))
