"""列画像：类型推断、缺失、唯一性、数值分布。

设计原则是**"宁可说错，也不要说谎"**：

* 类型推断用 95% 规则：≥95% 的值能解析成数字，就认为这是数值列，
  剩下 5% 单独报成 ``脏值``，而不是整列退化成字符串让你以为数据没问题。
* 缺失值分两档：``NA/NaN/空串`` 这类是确定的缺失；``-``、``?``、``unknown``
  这类是**疑似**缺失（也可能真是合法值），分开统计，由你决定怎么处理。
* 全角数字（``１２３``）先做 NFKC 归一化再判断，中文 Excel 导出的表常踩这个坑。
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

__all__ = [
    "MISSING_TOKENS",
    "SUSPECT_TOKENS",
    "ColumnProfile",
    "is_missing",
    "is_suspect_missing",
    "normalize_text",
    "parse_float",
    "parse_int",
    "parse_datetime",
    "infer_kind",
    "profile_column",
    "profile_table",
    "quantile",
    "stdev",
]

#: 确定的缺失值写法
MISSING_TOKENS = frozenset(
    {
        "",
        "na",
        "n/a",
        "nan",
        "null",
        "none",
        "nil",
        "#n/a",
        "#na",
        "nd",
        "无",
        "空",
        "缺失",
        "未填",
        "(null)",
        "(empty)",
    }
)

#: 疑似缺失：既可能是缺测标记，也可能真是合法取值
SUSPECT_TOKENS = frozenset({"-", "--", "---", "?", "??", "unknown", "未知", "不详", "n.a.", "n.d.", "x"})

_THOUSANDS_RE = re.compile(r"^[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?$")
_NUMBER_RE = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")

_DT_FORMATS = (
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y/%m/%d",
    "%Y.%m.%d %H:%M:%S",
    "%Y.%m.%d",
    "%d.%m.%Y %H:%M:%S",
    "%d.%m.%Y",
    "%d/%m/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M:%S",
    "%Y年%m月%d日 %H:%M:%S",
    "%Y年%m月%d日",
)


def normalize_text(value) -> str:
    """NFKC 归一化：全角数字/字母、全角空格都会被折成半角。"""
    if value is None:
        return ""
    text = str(value)
    if not text:
        return ""
    # 快路径：纯 ASCII 且无多余空白时不必走 NFKC
    if text.isascii():
        return text.strip()
    return unicodedata.normalize("NFKC", text).strip()


def is_missing(value) -> bool:
    """确定的缺失。``None`` 与空串都算。"""
    if value is None:
        return True
    text = normalize_text(value)
    return text == "" or text.lower() in MISSING_TOKENS


def is_suspect_missing(value) -> bool:
    """疑似缺失标记。"""
    if value is None:
        return False
    text = normalize_text(value).lower()
    return text in SUSPECT_TOKENS


def parse_float(value) -> float | None:
    """尽量把值解析成浮点，失败返回 ``None``（不抛异常）。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return None if number != number else number
    text = normalize_text(value)
    if text == "":
        return None
    lowered = text.lower()
    if lowered in ("inf", "+inf", "infinity"):
        return float("inf")
    if lowered in ("-inf", "-infinity"):
        return float("-inf")
    if _THOUSANDS_RE.match(text):
        text = text.replace(",", "")
    if not _NUMBER_RE.match(text):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_int(value) -> int | None:
    number = parse_float(value)
    if number is None:
        return None
    if number != int(number):
        return None
    return int(number)


def parse_datetime(value) -> datetime | None:
    """解析常见日期时间写法。没有分隔符的 8 位数字不当日期（避免把 ID 认成日期）。"""
    if value is None:
        return None
    text = normalize_text(value)
    if len(text) < 6:
        return None
    if not any(sep in text for sep in ("-", "/", ".", ":", "T", "年")):
        return None
    candidate = text.replace("Z", "+00:00") if text.endswith("Z") else text
    try:
        return datetime.fromisoformat(candidate)
    except ValueError:
        pass
    for fmt in _DT_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------- 统计小工具


def quantile(sorted_values: list[float], q: float) -> float | None:
    """线性插值分位数（与 numpy/pandas 的默认做法一致）。"""
    if not sorted_values:
        return None
    if q <= 0:
        return sorted_values[0]
    if q >= 1:
        return sorted_values[-1]
    position = (len(sorted_values) - 1) * q
    low = int(position)
    high = min(low + 1, len(sorted_values) - 1)
    weight = position - low
    return sorted_values[low] * (1 - weight) + sorted_values[high] * weight


def stdev(values: list[float]) -> float | None:
    """样本标准差（ddof=1），点数不足返回 ``None``。"""
    count = len(values)
    if count < 2:
        return None
    mean = sum(values) / count
    variance = sum((value - mean) ** 2 for value in values) / (count - 1)
    if variance < 0:
        return 0.0
    return variance**0.5


# ---------------------------------------------------------------- 列画像


@dataclass
class ColumnProfile:
    name: str
    kind: str
    total: int = 0
    non_null: int = 0
    nulls: int = 0
    suspect_nulls: int = 0
    unique: int = 0
    unique_capped: bool = False
    top: list[tuple[str, int]] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)
    minimum: float | None = None
    maximum: float | None = None
    mean: float | None = None
    std: float | None = None
    median: float | None = None
    p05: float | None = None
    p25: float | None = None
    p75: float | None = None
    p95: float | None = None
    min_len: int | None = None
    max_len: int | None = None
    dirty_count: int = 0
    dirty: list[str] = field(default_factory=list)
    dt_min: str | None = None
    dt_max: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def null_pct(self) -> float:
        return (self.nulls / self.total * 100) if self.total else 0.0

    @property
    def is_numeric(self) -> bool:
        return self.kind in ("int", "float")

    @property
    def is_temporal(self) -> bool:
        return self.kind in ("datetime", "date")

    @property
    def is_id_like(self) -> bool:
        return self.non_null > 1 and self.unique == self.non_null


def infer_kind(values, *, threshold: float = 0.95) -> str:
    """推断列类型：``int`` / ``float`` / ``bool`` / ``datetime`` / ``date`` / ``string`` / ``empty``。

    ``threshold`` 是"允许多大比例的值解析失败还继续认定为该类型"。
    """
    cleaned = [normalize_text(value) for value in values if not is_missing(value)]
    if not cleaned:
        return "empty"

    lowered = {text.lower() for text in cleaned}
    bool_tokens = {"true", "false", "yes", "no", "t", "f", "y", "n", "是", "否"}
    if lowered <= bool_tokens and lowered & {"true", "false", "yes", "no", "是", "否"}:
        return "bool"

    sample = cleaned[:5000]
    total = len(sample)
    if total == 0:
        return "empty"

    as_int = sum(1 for text in sample if parse_int(text) is not None)
    if as_int / total >= threshold:
        return "int"

    as_float = sum(1 for text in sample if parse_float(text) is not None)
    if as_float / total >= threshold:
        return "float"

    as_dt = 0
    has_time_part = False
    for text in sample:
        moment = parse_datetime(text)
        if moment is not None:
            as_dt += 1
            if moment.hour or moment.minute or moment.second:
                has_time_part = True
    if as_dt / total >= threshold:
        return "datetime" if has_time_part else "date"

    return "string"


def profile_column(name: str, values, *, top_n: int = 5, max_unique: int = 100_000) -> ColumnProfile:
    """对单列做完整画像。``values`` 可以是任意可迭代对象（含 ``None``）。"""
    raw = list(values)
    profile = ColumnProfile(name=name, kind="empty", total=len(raw))

    non_missing: list[str] = []
    nulls = 0
    suspects = 0
    for value in raw:
        if is_missing(value):
            nulls += 1
            continue
        if is_suspect_missing(value):
            suspects += 1
            continue
        non_missing.append(normalize_text(value))

    profile.nulls = nulls
    profile.suspect_nulls = suspects
    profile.non_null = len(non_missing)

    if not non_missing:
        profile.kind = "empty"
        profile.notes.append("整列没有有效值")
        if suspects:
            profile.notes.append(f"另有 {suspects} 个疑似缺失标记（如 - / ? / unknown）")
        return profile

    kind = infer_kind(non_missing)

    # 唯一值统计
    counter: Counter[str] = Counter()
    capped = False
    for text in non_missing:
        counter[text] += 1
        if len(counter) > max_unique:
            capped = True
            break
    profile.unique = len(counter)
    profile.unique_capped = capped
    profile.top = counter.most_common(top_n) if not capped else []
    for text in non_missing:
        if len(profile.examples) >= 3:
            break
        if text not in profile.examples:
            profile.examples.append(text[:60])
    profile.min_len = min(len(text) for text in non_missing)
    profile.max_len = max(len(text) for text in non_missing)

    numeric: list[float] = []
    dirty: list[str] = []
    if kind in ("int", "float"):
        for text in non_missing:
            number = parse_float(text)
            if number is None or number in (float("inf"), float("-inf")):
                if len(dirty) < 5:
                    dirty.append(text[:40])
            else:
                numeric.append(number)
        profile.dirty_count = len(non_missing) - len(numeric)
        profile.dirty = dirty
        if numeric:
            ordered = sorted(numeric)
            profile.minimum = ordered[0]
            profile.maximum = ordered[-1]
            profile.mean = sum(numeric) / len(numeric)
            profile.std = stdev(numeric)
            profile.median = quantile(ordered, 0.5)
            profile.p05 = quantile(ordered, 0.05)
            profile.p25 = quantile(ordered, 0.25)
            profile.p75 = quantile(ordered, 0.75)
            profile.p95 = quantile(ordered, 0.95)
        if kind == "int":
            profile.notes.append("全部可解析为整数")
        if profile.dirty_count:
            profile.notes.append(f"{profile.dirty_count} 个值无法解析为数字（已排除在统计之外）")

    elif kind in ("datetime", "date"):
        moments = [parse_datetime(text) for text in non_missing]
        valid = [moment for moment in moments if moment is not None]
        if valid:
            profile.dt_min = min(valid).isoformat(sep=" ")
            profile.dt_max = max(valid).isoformat(sep=" ")
        invalid = len(non_missing) - len(valid)
        profile.dirty_count = invalid
        if invalid:
            profile.notes.append(f"{invalid} 个值无法解析为时间")

    # 浮点/时间列天然"每个值都不同"，唯此提示只对整数列与文本列有意义
    if profile.non_null > 1 and profile.unique == profile.non_null and kind in ("int", "string"):
        profile.notes.append("每个值都不同，疑似 ID / 唯一键")
    if profile.non_null == 1:
        profile.notes.append("只有 1 个有效值")
    if profile.unique == 1 and profile.non_null > 1:
        profile.notes.append(f"常量列（所有值都是 {profile.examples[0]!r}）")
    if profile.max_len and profile.max_len > 80 and kind == "string":
        profile.notes.append(f"长文本列（最长 {profile.max_len} 字符）")

    profile.kind = kind
    return profile


def profile_table(table, *, columns=None) -> list[ColumnProfile]:
    """对整张表做列画像。"""
    names = list(columns) if columns else list(table.columns)
    profiles = []
    for index, name in enumerate(table.columns):
        if name not in names:
            continue
        values = ((row[index] if index < len(row) else None) for row in table.rows)
        profiles.append(profile_column(name, values))
    return profiles
