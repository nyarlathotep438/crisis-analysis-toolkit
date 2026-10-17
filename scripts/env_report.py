#!/usr/bin/env python
"""薄壳：环境快照与问题判断（等同 `python -m ekits env`）。

用法: python scripts/env_report.py [--root 目录] [--out 环境报告.md]
"""

from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from ekits.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(["env", *sys.argv[1:]]))
