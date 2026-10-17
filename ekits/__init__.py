"""EMERGENCYKITS —— 断网应急诊断工具箱（纯标准库内核）。

三条不可让步的设计约束（详见 docs/02-设计说明与约定.md）：

1. **零第三方依赖**：任何命令只用 Python 标准库就能运行。
   断网、换机器、虚拟环境损坏、conda 崩掉时，工具箱本身必须还能起来。
2. **可选增强**：检测到 numpy / pandas / matplotlib 时自动启用更快的
   数值路径与出图能力；检测不到就退化到纯 Python / 纯文本输出。
3. **只读默认**：所有诊断命令默认不改动任何输入文件，不写临时垃圾，
   不访问网络。写文件必须显式指定 ``--out`` / ``--apply`` / ``--plot``。
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["__version__"]
