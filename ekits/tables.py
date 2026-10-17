"""终端表格与纯文本图形渲染。

两个刻意为之的决定：

* **表格边框只用 ASCII**（``+ - |``）。中文 Windows 控制台常是 cp936，
  画框字符 ``│┼`` 在重定向到文件时会直接抛 ``UnicodeEncodeError``，
  应急工具绝不能在打印报告这一步崩掉。
* **宽度按东亚宽度计算**。``len("数据")`` 是 2，但它在终端里占 4 列，
  用 ``len`` 对齐会让纯中文表格整体错位 —— 这是中文报告最常见的小毛病。
"""

from __future__ import annotations

import sys
import unicodedata

__all__ = [
    "display_width",
    "elide",
    "render",
    "sparkline",
    "bar",
    "histogram",
]

#: 方块字符在 cp936/cp437 控制台下无法编码，降级到 ASCII 渐变
_RAMP_UNICODE = "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"  # ▁▂▃▄▅▆▇█
_RAMP_ASCII = " .:-=+*#%@"
_ramp_cache: str | None = None


def _char_width(char: str) -> int:
    """单个字符在等宽终端里占的列数（0 / 1 / 2）。"""
    if char == "\t":
        return 4
    if unicodedata.combining(char):
        return 0
    if unicodedata.east_asian_width(char) in ("W", "F"):
        return 2
    return 1


def display_width(text) -> int:
    """字符串在终端中的显示宽度。"""
    return sum(_char_width(ch) for ch in str(text))


def _can_encode(text: str) -> bool:
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def _ramp() -> str:
    global _ramp_cache
    if _ramp_cache is None:
        _ramp_cache = _RAMP_UNICODE if _can_encode(_RAMP_UNICODE) else _RAMP_ASCII
    return _ramp_cache


def elide(text: str, limit: int | None) -> str:
    """按显示宽度截断，超出部分用 ``…`` 收尾。"""
    text = str(text)
    if not limit or limit <= 0:
        return text
    if display_width(text) <= limit:
        return text
    out: list[str] = []
    used = 0
    budget = max(limit - 1, 1)
    for char in text:
        width = _char_width(char)
        if used + width > budget:
            break
        out.append(char)
        used += width
    return "".join(out) + "\u2026"


def _align(text: str, width: int, align: str) -> str:
    pad = width - display_width(text)
    if pad <= 0:
        return text
    if align == ">":
        return " " * pad + text
    if align == "^":
        left = pad // 2
        return " " * left + text + " " * (pad - left)
    return text + " " * pad


def _flatten(cell) -> str:
    """单元格里的换行会破坏表格结构，统一压成单行。"""
    if cell is None:
        return ""
    text = str(cell)
    if "\n" in text or "\r" in text or "\t" in text:
        for bad in ("\r\n", "\r", "\n", "\t"):
            text = text.replace(bad, " ")
        text = " ".join(text.split())
    return text


def _looks_numeric(column: list[str]) -> bool:
    seen = 0
    for cell in column:
        if cell == "":
            continue
        seen += 1
        try:
            float(cell.replace(",", ""))
        except ValueError:
            return False
        if seen >= 40:
            break
    return seen > 0


def render(
    headers,
    rows,
    *,
    aligns=None,
    max_col: int = 48,
    empty_text: str = "（无数据）",
) -> str:
    """渲染等宽表格，返回可直接 ``print`` 的多行字符串。

    ``aligns`` 可为每列指定 ``"<"`` / ``">"`` / ``"^"``；未指定时按内容
    自动判定（数字右对齐，其余左对齐）。
    """
    headers = [_flatten(h) for h in headers]
    ncol = len(headers)
    if ncol == 0:
        return empty_text

    body: list[list[str]] = []
    for row in rows:
        cells = [_flatten(cell) for cell in row]
        if len(cells) < ncol:
            cells += [""] * (ncol - len(cells))
        elif len(cells) > ncol:
            cells = cells[:ncol]
        body.append([elide(cell, max_col) for cell in cells])

    head = [elide(text, max_col) for text in headers]
    widths = [display_width(text) for text in head]
    for cells in body:
        for index, cell in enumerate(cells):
            widths[index] = max(widths[index], display_width(cell))

    if aligns is None:
        aligns = []
        for index in range(ncol):
            column = [cells[index] for cells in body]
            aligns.append(">" if _looks_numeric(column) else "<")
    else:
        aligns = list(aligns)
    aligns += ["<"] * (ncol - len(aligns))

    rule = "+" + "+".join("-" * (width + 2) for width in widths) + "+"
    lines = [rule]
    lines.append(
        "|"
        + "|".join(
            " " + _align(text, widths[index], "^") + " "
            for index, text in enumerate(head)
        )
        + "|"
    )
    lines.append(rule)
    for cells in body:
        lines.append(
            "|"
            + "|".join(
                " " + _align(cell, widths[index], aligns[index]) + " "
                for index, cell in enumerate(cells)
            )
            + "|"
        )
    if not body:
        lines.append(" " + empty_text)
    else:
        lines.append(rule)
    return "\n".join(lines)


def _as_floats(values) -> list[float]:
    out: list[float] = []
    for value in values:
        if isinstance(value, bool) or value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number == number:  # 排除 NaN
            out.append(number)
    return out


def _bucket(values: list[float], width: int) -> list[float]:
    if width <= 0 or len(values) <= width:
        return values
    step = len(values) / width
    out = []
    for index in range(width):
        start = int(index * step)
        end = max(int((index + 1) * step), start + 1)
        chunk = values[start:end]
        if chunk:
            out.append(sum(chunk) / len(chunk))
    return out


def sparkline(values, width: int = 60, *, clip: tuple[float, float] | None = None) -> str:
    """把一串数字压成一行"折线"，用于无图形环境快速看形状。

    断网或没有 matplotlib 时，这是判断"有没有尖峰/断崖/整体抬升"最快的办法。

    ``clip`` 用于稳健显示：例如 ``clip=(0.02, 0.98)`` 会先把取值范围压到 P02~P98，
    这样单个 -999 哨兵值就不会把其余 99% 的数据压缩成一条平线。
    裁剪只影响**显示比例**，不改变数据。
    """
    numbers = _as_floats(values)
    if not numbers:
        return ""
    if width and len(numbers) > width:
        numbers = _bucket(numbers, width)
    ramp = _ramp()
    top = max(numbers)
    bottom = min(numbers)
    if clip is not None and len(numbers) >= 5:
        ordered = sorted(numbers)
        bottom = _quantile(ordered, clip[0])
        top = _quantile(ordered, clip[1])
    if top <= bottom:
        return ramp[len(ramp) // 2] * len(numbers)
    scale = (len(ramp) - 1) / (top - bottom)
    out = []
    for value in numbers:
        ratio = (value - bottom) * scale
        index = int(ratio)
        index = 0 if index < 0 else (len(ramp) - 1 if index > len(ramp) - 1 else index)
        out.append(ramp[index])
    return "".join(out)


def _quantile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    position = (len(sorted_values) - 1) * max(0.0, min(1.0, q))
    low = int(position)
    high = min(low + 1, len(sorted_values) - 1)
    weight = position - low
    return sorted_values[low] * (1 - weight) + sorted_values[high] * weight


def bar(value, maximum, width: int = 24, *, char: str = "#", fill: str = ".") -> str:
    """简单横向条形图，用来表达"占比"。"""
    try:
        fraction = float(value) / float(maximum)
    except (TypeError, ValueError, ZeroDivisionError):
        return ""
    fraction = min(max(fraction, 0.0), 1.0)
    filled = int(round(fraction * width))
    return char * filled + fill * (width - filled)


def histogram(values, *, bins: int = 10, width: int = 28, digits: int = 3) -> str:
    """等宽分箱直方图，纯文本。

    比只报 ``min/max/中位数`` 有用得多：双峰、长尾、极值扎堆一眼就能看出来。
    """
    from .fmt import num  # 局部导入，避免循环依赖

    numbers = _as_floats(values)
    if not numbers:
        return "（无可用数值）"
    if bins < 1:
        bins = 1
    low = min(numbers)
    high = max(numbers)
    if high == low:
        return f"{num(low)}  (全部 {len(numbers)} 个值相同)"
    span = high - low
    counts = [0] * bins
    for value in numbers:
        index = int((value - low) / span * bins)
        if index >= bins:
            index = bins - 1
        counts[index] += 1
    peak = max(counts)
    lines = []
    for index, count in enumerate(counts):
        left = low + span * index / bins
        right = low + span * (index + 1) / bins
        lines.append(
            f"[{num(left, digits=digits):>10} ~ {num(right, digits=digits):>10}) "
            f"{bar(count, peak, width)} {count:>6}"
        )
    return "\n".join(lines)
