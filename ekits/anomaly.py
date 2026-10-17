"""异常点检测（纯 Python 实现，numpy 存在时无特殊依赖）。

为什么不用 ``scipy.stats.zscore`` 一行了事？因为急救场景下的数据几乎从不
满足"正态、无量纲、无漂移"这三个前提，直接套 z-score 会出现两种典型误判：

* **掩蔽效应**：异常点自身把均值和标准差拉大，导致 z 分数反而不到 3，
  一个 0.1% 的灾难性错误被吞掉。
* **漂移误报**：传感器整体抬升 20% 时，漂移期间所有点都会被判异常，
  你会在几百个"异常"里找不到真正的那一个跳变。

所以这里并排给出四种方法，并做**投票**（``consensus``）：四种方法都指到
同一个点，才是真正值得先看的点。方法各自的失效条件写在每个函数里。
"""

from __future__ import annotations

from dataclasses import dataclass

from . import profile as profile_mod

__all__ = [
    "Anomaly",
    "ChangeSegment",
    "MEDIAN_ABS_CONSTANT",
    "numeric_series",
    "median",
    "mad",
    "zscore_flags",
    "modified_zscore_flags",
    "iqr_flags",
    "rolling_zscore_flags",
    "jump_flags",
    "cusum_segments",
    "consensus",
    "METHODS",
]

#: 使 MAD 与正态分布标准差一致的常数（0.6745 的倒数）
MEDIAN_ABS_CONSTANT = 1.4826

METHODS = {
    "z": "z-score：均值±k倍标准差。快但会被异常点自身污染，且假设近似正态。",
    "modz": "修正 z-score：中位数+MAD。对掩蔽效应稳健，适合 n 较小的场景。",
    "iqr": "四分位距：超出 [Q1-k*IQR, Q3+k*IQR]。不假设分布，适合偏态数据。",
    "jump": "跳变检测：相邻差分的稳健 z 分数。专抓「某一刻突然错了」，对缓慢漂移不敏感。",
    "roll": "滚动 z-score：用前一窗口的均值/标准差衡量当前点。适合在线/流式数据。",
    "cusum": "累积和控制图：检测均值持续偏移的起点，是漂移类故障的首选。",
}


@dataclass
class Anomaly:
    index: int
    value: float
    score: float
    method: str
    note: str = ""


@dataclass
class ChangeSegment:
    start_index: int
    end_index: int
    direction: str
    magnitude: float = 0.0
    note: str = ""


# ---------------------------------------------------------------- 基础


def numeric_series(values) -> tuple[list[int], list[float]]:
    """抽出可解析的数值及其原始下标，跳过缺失/脏值/NaN/inf。"""
    positions: list[int] = []
    numbers: list[float] = []
    for index, value in enumerate(values):
        number = profile_mod.parse_float(value)
        if number is None:
            continue
        if number != number or number in (float("inf"), float("-inf")):
            continue
        positions.append(index)
        numbers.append(number)
    return positions, numbers


def median(values: list[float]) -> float | None:
    return profile_mod.quantile(sorted(values), 0.5) if values else None


def mad(values: list[float], center: float | None = None) -> float:
    """中位绝对偏差（未乘 1.4826）。"""
    if not values:
        return 0.0
    if center is None:
        center = median(values) or 0.0
    return median([abs(value - center) for value in values]) or 0.0


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


# ---------------------------------------------------------------- 各方法


def zscore_flags(values, threshold: float = 3.0, *, min_points: int = 8) -> list[Anomaly]:
    """经典 z-score。

    失效条件：n 很小（<8）时标准差不可靠；分布重尾时误报极多；
    异常点本身会抬高标准差（掩蔽）。所以它只适合"数据基本干净，只想抓明显错误"。
    """
    positions, numbers = numeric_series(values)
    if len(numbers) < min_points:
        return []
    mean = _mean(numbers)
    sd = profile_mod.stdev(numbers) or 0.0
    if sd <= 0:
        return []
    flags = []
    for position, number in zip(positions, numbers):
        score = abs(number - mean) / sd
        if score > threshold:
            flags.append(Anomaly(position, number, score, "z"))
    return flags


def modified_zscore_flags(values, threshold: float = 3.5, *, min_points: int = 8) -> list[Anomaly]:
    """修正 z-score（Iglewicz-Hoaglin）。

    用中位数和 MAD 代替均值与标准差，因此少数异常点**不会**把判据拉偏，
    适合"我怀疑数据里有几个大错，但不知道在哪"的场景。
    """
    positions, numbers = numeric_series(values)
    if len(numbers) < min_points:
        return []
    center = median(numbers)
    if center is None:
        return []
    spread = mad(numbers, center) * MEDIAN_ABS_CONSTANT
    if spread <= 0:
        # 中位数周围一半以上的点取值完全相同，MAD 退化为 0，改用平均绝对偏差
        spread = sum(abs(number - center) for number in numbers) / len(numbers)
        if spread <= 0:
            return []
    flags = []
    for position, number in zip(positions, numbers):
        score = abs(number - center) / spread
        if score > threshold:
            flags.append(Anomaly(position, number, score, "modz"))
    return flags


def iqr_flags(values, k: float = 1.5, *, min_points: int = 8) -> list[Anomaly]:
    """四分位距法（Tukey 箱线图规则）。``k=1.5`` 是"离群"，``k=3.0`` 是"极端"。

    退化处理：当四分之一以上的值完全相同、IQR 被压成 0 时，箱线图规则在数学上
    失效（任何不同值都"超出"零宽的箱体）。此时如果只有不超过 10% 的点与中位数
    不同，就改用平均绝对偏差作尺度重新划界，并在说明里注明"已降级"；
    如果不同的点太多，直接返回空 —— 那说明这是离散取值，不是异常。
    """
    positions, numbers = numeric_series(values)
    if len(numbers) < min_points:
        return []
    ordered = sorted(numbers)
    q1 = profile_mod.quantile(ordered, 0.25)
    q3 = profile_mod.quantile(ordered, 0.75)
    center = profile_mod.quantile(ordered, 0.5)
    if q1 is None or q3 is None or center is None:
        return []

    spread = q3 - q1
    fallback_note = ""
    if spread <= 0:
        non_constant = sum(1 for number in numbers if number != center)
        if non_constant == 0 or non_constant / len(numbers) > 0.10:
            return []
        spread = sum(abs(number - center) for number in numbers) / len(numbers)
        if spread <= 0:
            return []
        q1 = q3 = center
        fallback_note = "IQR 为 0，已改用平均绝对偏差"

    low = q1 - k * spread
    high = q3 + k * spread
    flags = []
    for position, number in zip(positions, numbers):
        if number < low:
            flags.append(Anomaly(position, number, (low - number) / spread, "iqr", fallback_note or "低于下界"))
        elif number > high:
            flags.append(Anomaly(position, number, (number - high) / spread, "iqr", fallback_note or "高于上界"))
    return flags


def rolling_zscore_flags(
    values,
    *,
    window: int = 20,
    threshold: float = 3.0,
    min_window: int = 5,
) -> list[Anomaly]:
    """滚动 z-score：只用**当前点之前**的窗口做基准，适合流式/在线判断。

    失效条件：漂移会让基准窗口自身也被污染，因此它抓"突刺"好用，
    抓"缓慢走偏"要靠 ``cusum_segments``。
    """
    window = max(window, min_window)
    positions, numbers = numeric_series(values)
    if len(numbers) <= window:
        return []
    flags = []
    for offset in range(window, len(numbers)):
        history = numbers[offset - window : offset]
        mean = _mean(history)
        sd = profile_mod.stdev(history) or 0.0
        if sd <= 0:
            # 窗口内完全不变，此时任何变化都是异常
            if numbers[offset] != mean:
                flags.append(Anomaly(positions[offset], numbers[offset], float("inf"), "roll", "窗口内此前完全恒定"))
            continue
        score = abs(numbers[offset] - mean) / sd
        if score > threshold:
            flags.append(Anomaly(positions[offset], numbers[offset], score, "roll"))
    return flags


def jump_flags(values, threshold: float = 4.0, *, min_points: int = 8) -> list[Anomaly]:
    """相邻跳变检测：对一阶差分做稳健 z 检验。

    专治"某一采样点突然变成另一个量级"（仪器掉电、单位切换、串口误码），
    这类错误用整体 z-score 常常漏掉，因为它只占一个点。
    """
    positions, numbers = numeric_series(values)
    if len(numbers) < min_points:
        return []
    diffs = [numbers[index] - numbers[index - 1] for index in range(1, len(numbers))]
    center = median(diffs)
    if center is None:
        return []
    spread = mad(diffs, center) * MEDIAN_ABS_CONSTANT
    if spread <= 0:
        spread = sum(abs(item - center) for item in diffs) / len(diffs)
        if spread <= 0:
            return []
    flags = []
    for offset, diff in enumerate(diffs):
        score = abs(diff - center) / spread
        if score > threshold:
            # 用后一个点作为代表（异常发生在"到达"该点的时刻）
            index = offset + 1
            flags.append(
                Anomaly(
                    positions[index],
                    numbers[index],
                    score,
                    "jump",
                    f"相邻变化 {diff:+.6g}（典型变化 {center:+.6g}）",
                )
            )
    return flags


def cusum_segments(
    values,
    *,
    k: float = 0.5,
    h: float = 5.0,
    min_points: int = 20,
) -> list[ChangeSegment]:
    """累积和（CUSUM）变点检测，返回均值发生持续偏移的区间。

    ``k`` 是容忍度（以标准差计，0.5 表示"半个 sigma 以内的波动视为噪声"），
    ``h`` 是报警阈值（5 表示"累积到 5 个 sigma 才认为真的变了"）。
    """
    positions, numbers = numeric_series(values)
    if len(numbers) < min_points:
        return []
    mean = _mean(numbers)
    sd = profile_mod.stdev(numbers) or 0.0
    if sd <= 0:
        return []
    z_values = [(number - mean) / sd for number in numbers]

    segments: list[ChangeSegment] = []
    positive = 0.0
    negative = 0.0
    start_positive: int | None = None
    start_negative: int | None = None

    for offset, z_value in enumerate(z_values):
        previous_positive, previous_negative = positive, negative
        positive = max(0.0, positive + z_value - k)
        negative = min(0.0, negative + z_value + k)

        if positive <= 0:
            start_positive = None
        elif previous_positive <= 0:
            start_positive = offset
        if negative >= 0:
            start_negative = None
        elif previous_negative >= 0:
            start_negative = offset

        if positive > h:
            begin = start_positive if start_positive is not None else 0
            segments.append(
                ChangeSegment(
                    positions[begin],
                    positions[offset],
                    "上升",
                    positive,
                    f"区间内均值比整体高约 {(sum(numbers[begin : offset + 1]) / max(offset + 1 - begin, 1) - mean) / sd:.2f} sd",
                )
            )
            positive = 0.0
            start_positive = None
        if negative < -h:
            begin = start_negative if start_negative is not None else 0
            segments.append(
                ChangeSegment(
                    positions[begin],
                    positions[offset],
                    "下降",
                    -negative,
                    f"区间内均值比整体低约 {(sum(numbers[begin : offset + 1]) / max(offset + 1 - begin, 1) - mean) / sd:.2f} sd",
                )
            )
            negative = 0.0
            start_negative = None

    return segments


# ---------------------------------------------------------------- 汇总


def consensus(flags_by_method: dict[str, list[Anomaly]], *, top: int = 20) -> list[dict]:
    """把多个方法的结论合并成"按可疑程度排序"的一张表。

    排序依据：被几种方法同时命中（票数）→ 最高得分。
    票数 ≥ 2 的点，值得马上看原始行；只被一种方法命中的点，多半是方法本身的偏好。
    """
    merged: dict[int, dict] = {}
    for method, items in flags_by_method.items():
        for item in items:
            entry = merged.setdefault(
                item.index,
                {"index": item.index, "value": item.value, "methods": [], "max_score": 0.0, "notes": []},
            )
            entry["methods"].append(method)
            if item.score > entry["max_score"]:
                entry["max_score"] = item.score
            if item.note and item.note not in entry["notes"]:
                entry["notes"].append(item.note)

    rows = list(merged.values())
    for entry in rows:
        entry["votes"] = len(set(entry["methods"]))
    rows.sort(key=lambda entry: (-entry["votes"], -entry["max_score"]))
    return rows[:top]
