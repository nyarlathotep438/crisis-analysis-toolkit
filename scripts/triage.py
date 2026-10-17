#!/usr/bin/env python
"""薄壳：一键分诊（等同 `python -m ekits triage`）。

**这是应急时最先跑的那条命令**：给它一个文件或目录，它自己判断类型、
跑完所有相关检查，并给出处置建议。

用法: python scripts/triage.py 路径 [--out 分诊报告.md]
"""

from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from ekits.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(["triage", *sys.argv[1:]]))
