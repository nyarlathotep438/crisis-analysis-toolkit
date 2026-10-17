"""出图（可选依赖 matplotlib）。

三条硬规则：

1. **后端强制 ``Agg``**：断网救援常在 SSH、远程桌面、无 X server 的环境里，
   ``plt.show()`` 会直接卡死或报 TclError。这里只写文件，永不弹窗。
2. **缺库给出替代方案**，不抛裸 ImportError。
3. **降采样**：几十万点的图既慢又看不出版面问题，超过阈值先抽稀。

坐标约定（很容易踩坑，所以写死在这里）：整条线、异常点、缺失区间**共用同一套
x 坐标** —— 时间列可解析时用 `` datetime``，否则用原始行号。绝不混用
"行号 + 日期"，否则 matplotlib 会把行号当成 1970 年的天数，图就废了。
"""

from __future__ import annotations

import datetime as _dt
import os

from . import fmt

__all__ = [
    "PlotUnavailable",
    "available",
    "cjk_font",
    "plot_series",
    "plot_value_hist",
    "plot_null_ratio",
]

#: 常见的中文字体，按优先级排列。matplotlib 默认的 DejaVu Sans 没有汉字字形，
#: 不指定就会把"温度"画成两个方块 —— 中文环境里这是最常见的出图事故。
_CJK_FONT_CANDIDATES = (
    "Microsoft YaHei",
    "微软雅黑",
    "SimHei",
    "黑体",
    "Noto Sans CJK SC",
    "Source Han Sans SC",
    "Source Han Sans CN",
    "WenQuanYi Micro Hei",
    "WenQuanYi Zen Hei",
    "PingFang SC",
    "Heiti SC",
    "STHeiti",
    "Arial Unicode MS",
)

_cjk_cache: dict[str, str | None] = {}


class PlotUnavailable(RuntimeError):
    """matplotlib 不可用。"""


def available() -> bool:
    from . import deps

    return deps.have("matplotlib")


def _pyplot():
    try:
        import matplotlib

        matplotlib.use("Agg", force=True)
        import matplotlib.pyplot as plt
    except Exception as exc:  # ImportError / 后端初始化失败都归到这里
        raise PlotUnavailable(
            "没有可用的 matplotlib，无法出图。\n"
            "断网时的替代方案（都不用装东西）：\n"
            "  1) python scripts/ts_diag.py 数据文件 --time ts --value val   # 打印文本折线（sparkline）\n"
            "  2) python scripts/data_profile.py 数据文件 --hist              # 打印分箱直方图与分位数\n"
            "  3) 用 --out 导出成 CSV，再用 Excel/WPS 画图\n"
            f"底层错误：{exc}"
        ) from exc

    if "font" not in _cjk_cache:
        _configure_cjk(matplotlib)
    return plt


def _configure_cjk(matplotlib) -> None:
    """挑一个能显示汉字的字体；一个都找不到就退回英文并说明原因。"""
    from matplotlib import font_manager

    chosen = None
    try:
        installed = {font.name for font in font_manager.fontManager.ttflist}
    except Exception:
        installed = set()
    for name in _CJK_FONT_CANDIDATES:
        if name in installed:
            chosen = name
            break
    if chosen:
        matplotlib.rcParams["font.sans-serif"] = [chosen, "DejaVu Sans"]
    # 负号用 ASCII 减号：很多中文字体缺 U+2212 字形，刻度会变成方块
    matplotlib.rcParams["axes.unicode_minus"] = False
    _cjk_cache["font"] = chosen


def cjk_font() -> str | None:
    """返回当前用于图内中文的字体名；``None`` 表示没找到中文字体。"""
    if "font" not in _cjk_cache:
        try:
            import matplotlib
        except Exception:
            return None
        _configure_cjk(matplotlib)
    return _cjk_cache.get("font")


def _prepare_output(out_path: str) -> str:
    target = os.fspath(out_path)
    parent = os.path.dirname(os.path.abspath(target))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    if not os.path.splitext(target)[1]:
        target += ".png"
    return target


def _to_float(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


def plot_series(
    out_path: str,
    x_values,
    y_values,
    *,
    title: str = "",
    xlabel: str = "",
    ylabel: str = "",
    anomalies: list[tuple[int, float]] | None = None,
    gap_index_spans: list[tuple[int, int]] | None = None,
    max_points: int = 20000,
) -> str:
    """画一条序列，可叠加异常点（红点）与缺失区间（灰色竖条）。

    ``anomalies`` 与 ``gap_index_spans`` 都用**原始行号**（0 开始）指定，
    本函数负责把它们映射到同一套 x 坐标上，调用方不用操心时间/行号的差别。
    """
    plt = _pyplot()
    target = _prepare_output(out_path)

    xs_full = list(x_values)
    ys_full = list(y_values)
    count = min(len(xs_full), len(ys_full))

    is_time = False
    for index in range(min(count, 60)):
        if isinstance(xs_full[index], _dt.datetime):
            is_time = True
            break

    # 行号 -> x 坐标（时间模式下丢掉无法解析时间的行，否则 matplotlib 会抛错）
    x_of_index: dict[int, object] = {}
    for index in range(count):
        if is_time:
            candidate = xs_full[index]
            if isinstance(candidate, _dt.datetime):
                x_of_index[index] = candidate
        else:
            x_of_index[index] = index
    if not x_of_index:
        raise ValueError("没有可用于横轴的坐标（时间列全为空？）")

    indices = sorted(x_of_index)
    step = 1
    if len(indices) > max_points:
        step = len(indices) // max_points + 1
    shown = indices[::step]

    ax_x = [x_of_index[index] for index in shown]
    ax_y = [_to_float(ys_full[index]) for index in shown]

    figure, axes = plt.subplots(figsize=(12, 4.2), dpi=110)
    axes.plot(ax_x, ax_y, linewidth=0.9, color="#1f6fb4")

    if gap_index_spans:
        for start, end in gap_index_spans:
            left = x_of_index.get(start)
            right = x_of_index.get(end)
            if left is None or right is None:
                continue
            axes.axvspan(left, right, color="#c8c8c8", alpha=0.45, linewidth=0, zorder=0)

    if anomalies:
        points = [(x_of_index[index], _to_float(value)) for index, value in anomalies if index in x_of_index]
        points = [(x, y) for x, y in points if y is not None]
        if points:
            axes.scatter(
                [item[0] for item in points],
                [item[1] for item in points],
                s=26,
                color="#d62728",
                zorder=5,
                label="异常点",
            )
            axes.legend(loc="best", fontsize=8)

    if title:
        axes.set_title(title)
    axes.set_xlabel(xlabel or ("时间" if is_time else "行号"))
    if ylabel:
        axes.set_ylabel(ylabel)
    axes.grid(True, alpha=0.25, linewidth=0.6)
    if is_time:
        figure.autofmt_xdate(rotation=30)
    figure.tight_layout()
    figure.savefig(target)
    plt.close(figure)
    return target


def plot_value_hist(out_path: str, values, *, title: str = "", bins: int = 40) -> str:
    """数值分布直方图 —— 看双峰、长尾、断崖比看均值有用得多。"""
    plt = _pyplot()
    target = _prepare_output(out_path)

    numbers = [_to_float(value) for value in values]
    numbers = [value for value in numbers if value is not None]
    if not numbers:
        raise ValueError("没有可用的数值，无法画直方图")

    figure, axes = plt.subplots(figsize=(9, 4), dpi=110)
    axes.hist(
        numbers,
        bins=min(bins, max(5, len(numbers) // 2)),
        color="#4c72b0",
        edgecolor="white",
        linewidth=0.4,
    )
    mean = sum(numbers) / len(numbers)
    axes.axvline(mean, color="#d62728", linestyle="--", linewidth=1.0, label=f"均值 {fmt.num(mean)}")
    if title:
        axes.set_title(title)
    axes.set_ylabel("频数")
    axes.legend(loc="best", fontsize=8)
    axes.grid(True, axis="y", alpha=0.25, linewidth=0.6)
    figure.tight_layout()
    figure.savefig(target)
    plt.close(figure)
    return target


def plot_null_ratio(out_path: str, columns, null_counts, totals, *, title: str = "") -> str:
    """各列缺失率横向条形图。"""
    plt = _pyplot()
    target = _prepare_output(out_path)

    ratios = []
    for count, total in zip(null_counts, totals):
        ratios.append(count / total if total else 0.0)
    if not ratios:
        raise ValueError("没有可画的列")

    figure, axes = plt.subplots(figsize=(9, max(2.5, 0.32 * len(columns))), dpi=110)
    positions = list(range(len(columns)))
    axes.barh(positions, ratios, color="#dd8452", edgecolor="none")
    axes.set_yticks(positions)
    axes.set_yticklabels([str(column)[:28] for column in columns], fontsize=8)
    axes.invert_yaxis()
    axes.set_xlabel("缺失率")
    axes.set_xlim(0, 1)
    if title:
        axes.set_title(title)
    axes.grid(True, axis="x", alpha=0.25, linewidth=0.6)
    figure.tight_layout()
    figure.savefig(target)
    plt.close(figure)
    return target
