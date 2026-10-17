"""时间序列诊断。

时序数据出问题的方式和数据表不太一样，而且**很多问题一眼看不出来**：

* 采样间隔悄悄变成了 5 分钟（传感器省电策略生效），而你以为还是 1 分钟；
* 某个时段的数据整段缺失，但因为没有行，``dropna()`` 之后看不出任何异常；
* 数值被卡住不动（串口断线后保持最后值），画出来是一条平线，看起来"很稳"；
* 存在缓慢漂移，模型精度下降但找不到原因。

这个模块只回答"这条序列的时间轴有没有病、病在哪"，不做预测建模。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import profile as profile_mod

__all__ = [
    "Gap",
    "Flatline",
    "TsDiagnosis",
    "diagnose",
    "parse_times",
    "acf_peaks",
]

#: 自相关的最大滞后（以采样点计）。取太高会把噪声当周期。
_MAX_LAG = 200


@dataclass
class Gap:
    prev_index: int
    next_index: int
    prev_time: str
    next_time: str
    seconds: float
    expected_missing: int


@dataclass
class Flatline:
    start_index: int
    end_index: int
    length: int
    value: float
    seconds: float | None = None


@dataclass
class TsDiagnosis:
    n_total: int = 0
    n_parsed: int = 0
    n_unparsable: int = 0
    start: str | None = None
    end: str | None = None
    span_seconds: float = 0.0
    median_interval: float | None = None
    interval_cv: float | None = None
    regular_share: float | None = None
    interval_top: list[tuple[float, int]] = field(default_factory=list)
    n_duplicate_times: int = 0
    n_out_of_order: int = 0
    gaps: list[Gap] = field(default_factory=list)
    flatlines: list[Flatline] = field(default_factory=list)
    expected_points: float | None = None
    coverage: float | None = None
    n_values: int = 0
    value_min: float | None = None
    value_max: float | None = None
    value_mean: float | None = None
    value_median: float | None = None
    value_std: float | None = None
    slope_per_hour: float | None = None
    trend_r2: float | None = None
    half_delta_pct: float | None = None
    first_half_mean: float | None = None
    second_half_mean: float | None = None
    acf: list[tuple[float, float]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def has_time_axis(self) -> bool:
        return self.n_parsed >= 2 and self.median_interval is not None


def _to_naive_utc(moment: datetime) -> datetime:
    """统一去掉时区，保证 aware/naive 混用时不抛 TypeError。"""
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(timezone.utc).replace(tzinfo=None)


def parse_times(values) -> tuple[list[int], list[datetime], int]:
    """解析时间列。返回 (有效下标, 时间列表, 无法解析个数)。"""
    positions: list[int] = []
    moments: list[datetime] = []
    bad = 0
    for index, value in enumerate(values):
        if profile_mod.is_missing(value):
            continue
        moment = profile_mod.parse_datetime(value)
        if moment is None:
            bad += 1
            continue
        positions.append(index)
        moments.append(_to_naive_utc(moment))
    return positions, moments, bad


def _linfit(xs: list[float], ys: list[float]) -> tuple[float | None, float | None]:
    """最小二乘直线 ``y = a*x + b``，返回 (斜率, R²)。"""
    count = len(xs)
    if count < 2:
        return None, None
    mean_x = sum(xs) / count
    mean_y = sum(ys) / count
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx <= 0:
        return None, None
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    slope = sxy / sxx
    intercept = mean_y - slope * mean_x
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    if ss_tot <= 0:
        return slope, 1.0
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
    r2 = max(0.0, min(1.0, 1 - ss_res / ss_tot))
    return slope, r2


def acf_peaks(
    values: list[float],
    *,
    max_lag: int = _MAX_LAG,
    min_points: int = 30,
    min_corr: float = 0.25,
    top: int = 3,
) -> list[tuple[float, float]]:
    """自相关峰值 → 疑似周期（返回 [(相关系数, 滞后点数)]）。

    先去掉线性趋势，否则整体漂移会让自相关恒为接近 1，看不出真实周期。
    计算量 O(n·max_lag)，n 太大时自动降采样以保证应急场景下秒级返回。
    """
    series = list(values)
    if len(series) < min_points:
        return []
    if len(series) > 1500:
        step = len(series) // 1500 + 1
        series = series[::step]

    count = len(series)
    xs = list(range(count))
    slope, _ = _linfit([float(x) for x in xs], series)
    if slope is not None:
        intercept = (sum(series) / count) - slope * (sum(xs) / count)
        series = [value - (slope * index + intercept) for index, value in enumerate(series)]

    mean = sum(series) / count
    centered = [value - mean for value in series]
    variance = sum(value * value for value in centered)
    if variance <= 0:
        return []

    correlations: list[tuple[float, int]] = []
    for lag in range(2, min(max_lag, count // 2) + 1):
        numerator = sum(centered[index] * centered[index + lag] for index in range(count - lag))
        correlation = numerator / variance * (count / (count - lag))
        correlations.append((correlation, lag))

    peaks: list[tuple[float, int]] = []
    for position, (correlation, lag) in enumerate(correlations):
        if correlation < min_corr:
            continue
        previous = correlations[position - 1][0] if position > 0 else 0.0
        following = correlations[position + 1][0] if position + 1 < len(correlations) else 0.0
        if correlation >= previous and correlation >= following:
            peaks.append((correlation, lag))

    peaks.sort(reverse=True)
    chosen: list[tuple[float, float]] = []
    used: list[int] = []
    for correlation, lag in peaks:
        if any(abs(lag - taken) < 2 for taken in used):
            continue
        chosen.append((correlation, float(lag)))
        used.append(lag)
        if len(chosen) >= top:
            break
    return chosen


def _detect_flatlines(
    positions: list[int],
    moments: list[datetime],
    numbers: list[float],
    *,
    min_length: int = 5,
) -> list[Flatline]:
    """连续相同取值 → 传感器卡死/串口断线。"""
    flatlines: list[Flatline] = []
    if len(numbers) < min_length:
        return flatlines
    start = 0
    for offset in range(1, len(numbers) + 1):
        if offset < len(numbers) and numbers[offset] == numbers[start]:
            continue
        length = offset - start
        if length >= min_length:
            seconds = None
            if offset - 1 < len(moments) and start < len(moments):
                seconds = (moments[offset - 1] - moments[start]).total_seconds()
            flatlines.append(
                Flatline(
                    start_index=positions[start],
                    end_index=positions[offset - 1],
                    length=length,
                    value=numbers[start],
                    seconds=seconds,
                )
            )
        start = offset
    flatlines.sort(key=lambda item: -item.length)
    return flatlines


def diagnose(
    times,
    values,
    *,
    gap_factor: float = 1.5,
    flat_min: int = 5,
    regularity_tolerance: float = 0.1,
) -> TsDiagnosis:
    """对一条 (时间, 数值) 序列做全面体检。

    ``times`` 与 ``values`` 长度必须一致，允许含缺失值。
    """
    times = list(times)
    values = list(values)
    result = TsDiagnosis(n_total=len(values))
    if len(times) != len(values):
        result.notes.append(f"时间列与数值列长度不一致（{len(times)} vs {len(values)}），按较短者对齐")
        shorter = min(len(times), len(values))
        times, values = times[:shorter], values[:shorter]
        result.n_total = shorter

    positions, moments, bad = parse_times(times)
    result.n_parsed = len(moments)
    result.n_unparsable = bad
    if bad:
        result.notes.append(f"{bad} 个时间值无法解析，已从时序分析中排除")

    value_positions, numbers = _numeric_values(values)
    result.n_values = len(numbers)
    if numbers:
        ordered = sorted(numbers)
        result.value_min = ordered[0]
        result.value_max = ordered[-1]
        result.value_mean = sum(numbers) / len(numbers)
        result.value_median = profile_mod.quantile(ordered, 0.5)
        result.value_std = profile_mod.stdev(numbers)
        if result.value_std == 0:
            result.notes.append("数值在整个时间段内完全不变，疑似采集卡死")

    if len(moments) < 2:
        result.notes.append("有效时间点少于 2 个，无法做时间轴分析；请确认 --time 指定的列确实是时间")
        return result

    # 时间顺序
    out_of_order = sum(1 for previous, current in zip(moments, moments[1:]) if current < previous)
    result.n_out_of_order = out_of_order
    duplicates = len(moments) - len(set(moments))
    result.n_duplicate_times = duplicates

    order = sorted(range(len(moments)), key=lambda index: moments[index])
    sorted_times = [moments[index] for index in order]
    sorted_positions = [positions[index] for index in order]

    result.start = sorted_times[0].isoformat(sep=" ")
    result.end = sorted_times[-1].isoformat(sep=" ")
    result.span_seconds = (sorted_times[-1] - sorted_times[0]).total_seconds()

    diffs = [
        (sorted_times[index] - sorted_times[index - 1]).total_seconds()
        for index in range(1, len(sorted_times))
    ]
    positive = [value for value in diffs if value > 0]
    if not positive:
        result.notes.append("所有时间点都相同，无法推断采样间隔")
        return result

    median_interval = profile_mod.quantile(sorted(positive), 0.5)
    result.median_interval = median_interval
    if median_interval and median_interval > 0:
        # 变异系数只统计"规整段"，否则一处断档就能把它推到 0.7 以上，看起来像全程抖动
        inliers = [value for value in positive if abs(value - median_interval) <= median_interval * 0.2]
        if len(inliers) >= 2:
            mean_inlier = sum(inliers) / len(inliers)
            sd_inlier = profile_mod.stdev(inliers) or 0.0
            result.interval_cv = sd_inlier / mean_inlier if mean_inlier else None
        tolerance = median_interval * regularity_tolerance
        on_grid = sum(1 for value in positive if abs(value - median_interval) <= tolerance)
        result.regular_share = on_grid / len(positive)

        counter: dict[float, int] = {}
        for value in positive:
            key = round(value, 3)
            counter[key] = counter.get(key, 0) + 1
        result.interval_top = sorted(counter.items(), key=lambda item: -item[1])[:5]

        gaps = []
        for index, seconds in enumerate(diffs, start=1):
            if seconds > median_interval * gap_factor:
                gaps.append(
                    Gap(
                        prev_index=sorted_positions[index - 1],
                        next_index=sorted_positions[index],
                        prev_time=sorted_times[index - 1].isoformat(sep=" "),
                        next_time=sorted_times[index].isoformat(sep=" "),
                        seconds=seconds,
                        expected_missing=max(0, int(round(seconds / median_interval)) - 1),
                    )
                )
        gaps.sort(key=lambda gap: -gap.seconds)
        result.gaps = gaps[:20]
        if len(gaps) > 20:
            result.notes.append(f"共 {len(gaps)} 处时间间隔异常，只列出最长的 20 处")

        if result.span_seconds > 0:
            result.expected_points = result.span_seconds / median_interval + 1
            result.coverage = len(sorted_times) / result.expected_points

        if result.interval_top and len(result.interval_top) > 1:
            dominant, dominant_count = result.interval_top[0]
            second, second_count = result.interval_top[1]
            if second > 0 and second_count / len(positive) > 0.1 and abs(second - dominant) > dominant * 0.2:
                result.notes.append(
                    f"采样间隔不唯一：主间隔 {dominant:g}s（{dominant_count} 次），"
                    f"但另有 {second:g}s（{second_count} 次）—— 检查是否有变频采样或跨天拼接"
                )

    # 数值与时间的对齐（用于平直段、趋势）
    time_of_position = {position: moment for position, moment in zip(positions, moments)}
    pairs = [(position, value) for position, value in zip(value_positions, numbers) if position in time_of_position]
    if not pairs:
        return result

    aligned_positions = [item[0] for item in pairs]
    aligned_values = [item[1] for item in pairs]
    aligned_times = [time_of_position[item[0]] for item in pairs]

    order = sorted(range(len(aligned_times)), key=lambda index: aligned_times[index])
    aligned_positions = [aligned_positions[index] for index in order]
    aligned_values = [aligned_values[index] for index in order]
    aligned_times = [aligned_times[index] for index in order]

    result.flatlines = _detect_flatlines(aligned_positions, aligned_times, aligned_values, min_length=flat_min)[:5]

    base = aligned_times[0]
    seconds_axis = [(moment - base).total_seconds() for moment in aligned_times]
    slope, r2 = _linfit(seconds_axis, aligned_values)
    if slope is not None:
        result.slope_per_hour = slope * 3600
        result.trend_r2 = r2

    if len(aligned_values) >= 8:
        half = len(aligned_values) // 2
        first = aligned_values[:half]
        second = aligned_values[half:]
        result.first_half_mean = sum(first) / len(first)
        result.second_half_mean = sum(second) / len(second)
        if result.first_half_mean:
            result.half_delta_pct = (
                (result.second_half_mean - result.first_half_mean) / abs(result.first_half_mean) * 100
            )

    result.acf = acf_peaks(aligned_values)
    if result.acf and result.median_interval:
        result.notes.append(
            "疑似周期性：" + "、".join(
                f"滞后 {int(lag)} 点 ≈ {lag * result.median_interval:g}s（相关系数 {correlation:.2f}）"
                for correlation, lag in result.acf
            )
        )

    return result


def _numeric_values(values) -> tuple[list[int], list[float]]:
    positions: list[int] = []
    numbers: list[float] = []
    for index, value in enumerate(values):
        number = profile_mod.parse_float(value)
        if number is None or number != number or number in (float("inf"), float("-inf")):
            continue
        positions.append(index)
        numbers.append(number)
    return positions, numbers
