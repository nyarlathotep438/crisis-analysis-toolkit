"""数值 / 字节 / 时长的稳定文本格式化。

诊断报告最怕两件事：浮点噪声（``0.30000000000000004``）和单位混乱
（``123456789`` 到底是字节还是毫秒）。这里集中处理，保证同一份数据在
任何机器上打印出来都一致、可读、可对比。
"""

from __future__ import annotations

__all__ = [
    "human_bytes",
    "human_count",
    "num",
    "pct",
    "duration",
    "ratio_text",
]

_SI_UNITS = ("B", "KB", "MB", "GB", "TB", "PB", "EB")
_BIN_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB")


def human_bytes(size, *, binary: bool = False, digits: int = 1) -> str:
    """把字节数变成 ``1.5 MB`` 这种人类可读形式。

    >>> human_bytes(0)
    '0 B'
    >>> human_bytes(1536)
    '1.5 KB'
    """
    if size is None:
        return "?"
    try:
        value = float(size)
    except (TypeError, ValueError):
        return "?"
    if value != value or value in (float("inf"), float("-inf")):  # NaN / inf
        return "?"
    sign = "-" if value < 0 else ""
    value = abs(value)
    base = 1024.0 if binary else 1000.0
    units = _BIN_UNITS if binary else _SI_UNITS
    if value < base:
        return f"{sign}{value:.0f} B"
    index = 0
    while value >= base and index < len(units) - 1:
        value /= base
        index += 1
    # 避免出现 "1000.0 KB" 这类别扭结果
    if value >= base - 0.05 and index < len(units) - 1:
        value /= base
        index += 1
    return f"{sign}{value:.{digits}f} {units[index]}"


def human_count(value) -> str:
    """整数的千分位写法。"""
    if value is None:
        return "?"
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "?"


def num(value, *, digits: int = 4) -> str:
    """把数字压缩到 ``digits`` 位有效数字，极端量级自动用科学计数法。"""
    if value is None:
        return "?"
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, int):
        return f"{value:,}"
    try:
        x = float(value)
    except (TypeError, ValueError):
        return str(value)
    if x != x:
        return "nan"
    if x == float("inf"):
        return "inf"
    if x == float("-inf"):
        return "-inf"
    if x == 0:
        return "0"
    magnitude = abs(x)
    if magnitude >= 1e7 or magnitude < 1e-4:
        return f"{x:.{max(digits - 1, 1)}e}"
    text = f"{x:.{digits}g}"
    return text


def pct(part, total, *, digits: int = 1) -> str:
    """``12.3%``；分母为零时返回 ``n/a`` 而不是崩溃。"""
    if total in (None, 0):
        return "n/a"
    try:
        return f"{float(part) / float(total) * 100:.{digits}f}%"
    except (TypeError, ValueError, ZeroDivisionError):
        return "n/a"


def ratio_text(part, total, *, digits: int = 1) -> str:
    """``0.98 (98.0%)`` 这种"比例 + 百分数"并列的写法。"""
    if total in (None, 0):
        return "n/a"
    try:
        value = float(part) / float(total)
    except (TypeError, ValueError, ZeroDivisionError):
        return "n/a"
    return f"{value:.{max(digits, 2)}f} ({value * 100:.{digits}f}%)"


def duration(seconds) -> str:
    """秒数 -> ``350 ms`` / ``4.2 s`` / ``3m 05s`` / ``2.5 h``。"""
    if seconds is None:
        return "?"
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return "?"
    if value != value or value in (float("inf"), float("-inf")):
        return "?"
    negative = value < 0
    value = abs(value)
    if value < 1e-3:
        text = f"{value * 1e6:.0f} us"
    elif value < 1.0:
        text = f"{value * 1e3:.0f} ms"
    elif value < 60:
        text = f"{value:.3g} s"
    elif value < 3600:
        minutes = int(value // 60)
        text = f"{minutes}m {value - minutes * 60:04.1f}s"
    elif value < 86400:
        hours = int(value // 3600)
        text = f"{hours}h {int((value - hours * 3600) // 60):02d}m"
    else:
        days = int(value // 86400)
        text = f"{days}d {(value - days * 86400) / 3600:04.1f}h"
    return ("-" + text) if negative else text
