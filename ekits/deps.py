"""可选依赖探测。

工具箱的承诺是"断网也能跑"，所以所有第三方库都必须当成可选项：
能 import 就用它的快路径，import 不到就走纯 Python 实现或直接跳过这一节，
并且把"缺了什么"当成诊断结论的一部分打印出来 —— 而不是抛异常。
"""

from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass

__all__ = [
    "OPTIONAL_DEPS",
    "DepInfo",
    "have",
    "optional",
    "version_of",
    "status",
    "missing",
    "describe",
]


@dataclass(frozen=True)
class DepInfo:
    name: str
    purpose: str
    available: bool
    version: str | None


#: (模块名, 在工具箱里承担什么角色)
OPTIONAL_DEPS: tuple[tuple[str, str], ...] = (
    ("numpy", "向量化计算，加速异常检测与分位数"),
    ("pandas", "读取 xlsx/parquet、宽表运算"),
    ("matplotlib", "输出 PNG 图（无界面 Agg 后端）"),
    ("scipy", "精确分布与统计检验"),
    ("psutil", "内存/进程/磁盘明细"),
)

_cache: dict[str, object] = {}


def _load(name: str):
    """导入并缓存；任何异常都视为"不可用"（损坏的安装不算可用）。"""
    if name in _cache:
        return _cache[name]
    module = None
    try:
        module = importlib.import_module(name)
    except Exception:  # ImportError 之外还可能是 OSError、DLL 加载失败
        module = None
    _cache[name] = module
    return module


def have(name: str) -> bool:
    return _load(name) is not None


def optional(name: str, default=None):
    module = _load(name)
    return module if module is not None else default


def version_of(name: str) -> str | None:
    module = _load(name)
    if module is None:
        return None
    version = getattr(module, "__version__", None)
    if isinstance(version, str) and version:
        return version
    try:
        from importlib.metadata import version as _pkg_version

        return _pkg_version(name)
    except Exception:
        return None


def status() -> list[DepInfo]:
    return [
        DepInfo(name=name, purpose=purpose, available=have(name), version=version_of(name))
        for name, purpose in OPTIONAL_DEPS
    ]


def missing() -> list[str]:
    return [name for name, _ in OPTIONAL_DEPS if not have(name)]


def describe() -> str:
    """一行摘要，例如 ``numpy 2.1.0 / pandas 2.2.3 / matplotlib 缺失``。"""
    parts = []
    for info in status():
        parts.append(f"{info.name} {info.version}" if info.available else f"{info.name} 缺失")
    return " · ".join(parts)


def python_info() -> dict:
    return {
        "版本": sys.version.split()[0],
        "实现": sys.implementation.name,
        "可执行文件": sys.executable,
        "64 位": sys.maxsize > 2**32,
        "虚拟环境": sys.prefix != getattr(sys, "base_prefix", sys.prefix),
    }
