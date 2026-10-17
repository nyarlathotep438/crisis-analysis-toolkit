"""表格数据加载（stdlib 优先）。

支持的格式：

============  ==========================================================
格式           读取方式
============  ==========================================================
``.csv``       标准库 csv + 分隔符嗅探 + 编码探测
``.tsv/.tab``  同上，默认制表符
``.txt``       分隔符嗅探；单列（每行一个值）也支持
``.json``      list[dict] / dict-of-lists / 单条记录
``.jsonl``     每行一个 JSON 对象
``.xlsx/.xls`` 需要 pandas + openpyxl（缺失时给明确提示，不静默失败）
``.parquet``   需要 pandas + pyarrow
``.npy``       需要 numpy
============  ==========================================================

刻意不做的事：不猜你想怎么清洗数据。这里只负责"把文件变成行和列"，
空值判断、类型推断、异常检测交给 profile / quality / anomaly 模块，
这样每个环节都能被单独检查、单独解释。
"""

from __future__ import annotations

import csv
import io
import json
import os
from dataclasses import dataclass, field

from . import textio

__all__ = [
    "Table",
    "UnsupportedFormat",
    "read_table",
    "read_csv_table",
    "read_json_table",
    "read_jsonl_table",
    "read_lines_table",
    "read_numeric_column",
    "write_csv",
    "sniff_delimiter",
    "classify_file",
]

#: 默认最多读多少行，防止误指一个几十 GB 的文件把内存打满
DEFAULT_MAX_ROWS = 200_000

_DELIMITERS = (",", "\t", ";", "|")

TABLE_SUFFIXES = (".csv", ".tsv", ".tab", ".txt", ".dat", ".json", ".jsonl", ".ndjson")
PANDAS_SUFFIXES = (".xlsx", ".xls", ".xlsm", ".parquet", ".feather")
NUMPY_SUFFIXES = (".npy", ".npz")


class UnsupportedFormat(ValueError):
    """无法读取的格式。消息里会写清楚"缺哪个库 / 该换成什么命令"。"""


@dataclass
class Table:
    source: str
    columns: list[str]
    rows: list[list[str | None]]
    encoding: str = "utf-8"
    delimiter: str = ","
    size: int = 0
    truncated_rows: int = 0
    mismatched_rows: int = 0
    deduped_columns: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    via: str = "stdlib"

    def __len__(self) -> int:
        return len(self.rows)

    @property
    def n_cols(self) -> int:
        return len(self.columns)

    def head(self, count: int = 5) -> list[list[str | None]]:
        return self.rows[:count]

    def column(self, name: str) -> list[str | None]:
        """按列名取值；列名不存在时抛 KeyError 并给出近似建议。"""
        if name not in self.columns:
            close = [c for c in self.columns if name.lower() in c.lower() or c.lower() in name.lower()]
            hint = f"，你是不是想要：{close[:5]}" if close else ""
            raise KeyError(f"列 {name!r} 不存在{hint}。现有列：{self.columns[:20]}")
        index = self.columns.index(name)
        return [row[index] if index < len(row) else None for row in self.rows]

    def index_of(self, name: str) -> int:
        try:
            return self.columns.index(name)
        except ValueError:
            close = [c for c in self.columns if name.lower() in c.lower() or c.lower() in name.lower()]
            hint = f"，你是不是想要：{close[:5]}" if close else ""
            raise KeyError(f"列 {name!r} 不存在{hint}。现有列：{self.columns[:20]}") from None

    def describe(self) -> str:
        return f"{len(self.rows)} 行 × {self.n_cols} 列（{os.path.basename(self.source)}）"


# ---------------------------------------------------------------- 分隔符嗅探


def sniff_delimiter(sample: str) -> str:
    """猜分隔符。先试 ``csv.Sniffer``，失败再按"出现次数的稳定性"打分。"""
    lines = [line for line in sample.splitlines() if line.strip()][:30]
    if not lines:
        return ","
    try:
        dialect = csv.Sniffer().sniff("\n".join(lines[:10]), delimiters="".join(_DELIMITERS))
        return dialect.delimiter
    except csv.Error:
        pass

    best = ","
    best_score = -1.0
    for candidate in _DELIMITERS:
        counts = [line.count(candidate) for line in lines]
        non_zero = [count for count in counts if count > 0]
        if not non_zero:
            continue
        mode = max(set(non_zero), key=non_zero.count)
        consistency = non_zero.count(mode) / len(counts)
        # 稳定性优先，其次偏向出现次数多的（1 个分隔符 = 2 列，也可能是真表）
        score = consistency * 10 + min(mode, 6) * 0.1
        if score > best_score:
            best, best_score = candidate, score
    return best


def _dedupe_headers(headers: list[str]) -> tuple[list[str], list[str]]:
    """重复列名加后缀并报告 —— 重复列名会让 ``df['x']`` 静默拿到 DataFrame。"""
    seen: dict[str, int] = {}
    duplicates: list[str] = []
    out: list[str] = []
    for name in headers:
        if name in seen:
            seen[name] += 1
            duplicates.append(name)
            out.append(f"{name}.{seen[name]}")
        else:
            seen[name] = 1
            out.append(name)
    return out, duplicates


# ---------------------------------------------------------------- 各格式读取


def read_csv_table(
    path: str | os.PathLike,
    *,
    delimiter: str | None = None,
    encoding: str | None = None,
    max_rows: int | None = DEFAULT_MAX_ROWS,
    has_header: bool = True,
) -> Table:
    decoded = textio.read_decoded(path, encoding=encoding)
    text = decoded.text
    notes: list[str] = []
    if decoded.truncated:
        notes.append("文件很大，只读了前 64 MiB")
    if decoded.bad_bytes:
        notes.append(f"解码出现 {decoded.bad_bytes} 个替换字符，编码可能猜错（可用 --encoding 指定）")

    actual_delimiter = delimiter or sniff_delimiter(text[:65536])
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=actual_delimiter)

    columns: list[str] = []
    rows: list[list[str | None]] = []
    mismatched = 0
    truncated_rows = 0
    deduped: list[str] = []

    for index, raw_row in enumerate(reader):
        if index == 0 and has_header:
            columns = [str(cell).strip().lstrip("\ufeff") for cell in raw_row]
            columns, duplicates = _dedupe_headers(columns)
            if duplicates:
                deduped = sorted(set(duplicates))
                notes.append(f"存在重复列名 {deduped}，已加后缀区分")
            if raw_row and raw_row[0].startswith("\ufeff"):
                notes.append("首列名带 BOM（\\ufeff），已自动剥掉")
            continue
        if max_rows is not None and len(rows) >= max_rows:
            truncated_rows += 1
            continue
        if not columns:
            columns = [f"col{i + 1}" for i in range(len(raw_row))]
            notes.append("没有表头，已自动命名为 col1..colN")
        if len(raw_row) != len(columns):
            mismatched += 1
        cells = [cell for cell in raw_row[: len(columns)]]
        cells += [None] * (len(columns) - len(cells))
        rows.append(cells)

    if not columns:
        raise UnsupportedFormat(f"{path} 里没有可解析的表格内容（文件为空或全是空行？）")

    return Table(
        source=os.fspath(path),
        columns=columns,
        rows=rows,
        encoding=decoded.guess.encoding,
        delimiter=actual_delimiter,
        size=decoded.size,
        truncated_rows=truncated_rows,
        mismatched_rows=mismatched,
        deduped_columns=deduped,
        notes=notes,
    )


def read_lines_table(path: str | os.PathLike, *, encoding: str | None = None) -> Table:
    """每行一个值的纯文本（传感器导出、``values.txt`` 之类）。"""
    decoded = textio.read_decoded(path, encoding=encoding)
    lines = decoded.text.splitlines()
    notes = ["按「单列纯文本」读取，列名固定为 value"]
    header = ""
    rows: list[list[str | None]] = []
    non_empty = [line for line in lines if line.strip()]
    if non_empty and not _looks_numeric(non_empty[0]):
        header = non_empty[0].strip()
        non_empty = non_empty[1:]
        notes.append(f"首行不像数字，当作列名：{header!r}")
    for line in non_empty:
        rows.append([line.strip()])
    if header:
        notes.append("如果首行其实是数据而不是列名，请用 --has-header 关闭表头推断")
    return Table(
        source=os.fspath(path),
        columns=[header or "value"],
        rows=rows,
        encoding=decoded.guess.encoding,
        delimiter="",
        size=decoded.size,
        notes=notes,
    )


def _looks_numeric(text: str) -> bool:
    try:
        float(text.replace(",", ""))
        return True
    except (TypeError, ValueError):
        return False


def _json_records_to_table(
    records: list[dict], path: str, encoding: str, size: int, notes: list[str]
) -> Table:
    columns: list[str] = []
    for record in records:
        for key in record:
            if key not in columns:
                columns.append(str(key))
    columns, duplicates = _dedupe_headers(columns)
    if duplicates:
        notes.append(f"JSON 记录里出现重复键 {sorted(set(duplicates))}")

    rows: list[list[str | None]] = []
    for record in records:
        row: list[str | None] = []
        for key in columns:
            value = record.get(key)
            if value is None:
                row.append(None)
            elif isinstance(value, (str, int, float, bool)):
                row.append(str(value))
            else:
                row.append(json.dumps(value, ensure_ascii=False))
        rows.append(row)
    return Table(
        source=path,
        columns=columns,
        rows=rows,
        encoding=encoding,
        delimiter="",
        size=size,
        deduped_columns=sorted(set(duplicates)),
        notes=notes,
        via="json",
    )


def read_json_table(path: str | os.PathLike, *, encoding: str | None = None) -> Table:
    decoded = textio.read_decoded(path, encoding=encoding)
    notes: list[str] = []
    try:
        payload = json.loads(decoded.text)
    except json.JSONDecodeError as exc:
        raise UnsupportedFormat(f"{path} 不是合法 JSON：{exc}") from exc

    if isinstance(payload, list):
        if not payload:
            raise UnsupportedFormat(f"{path} 是空数组")
        if all(isinstance(item, dict) for item in payload):
            return _json_records_to_table(payload, decoded.path, decoded.guess.encoding, decoded.size, notes)
        table = Table(
            source=decoded.path,
            columns=["value"],
            rows=[[json.dumps(item, ensure_ascii=False) if not isinstance(item, (str, int, float)) else str(item)] for item in payload],
            encoding=decoded.guess.encoding,
            delimiter="",
            size=decoded.size,
            notes=["顶层是标量数组，按单列 value 读取"],
            via="json",
        )
        return table

    if isinstance(payload, dict):
        # dict of lists（列式存储）最常见
        if payload and all(isinstance(value, list) for value in payload.values()):
            lengths = {len(value) for value in payload.values()}
            if len(lengths) == 1:
                length = lengths.pop()
                columns, duplicates = _dedupe_headers([str(key) for key in payload])
                rows = []
                for index in range(length):
                    row = []
                    for value in payload.values():
                        cell = value[index]
                        row.append(None if cell is None else (json.dumps(cell, ensure_ascii=False) if isinstance(cell, (dict, list)) else str(cell)))
                    rows.append(row)
                return Table(
                    source=decoded.path,
                    columns=columns,
                    rows=rows,
                    encoding=decoded.guess.encoding,
                    delimiter="",
                    size=decoded.size,
                    notes=["按 dict-of-lists（列式）读取"],
                    via="json",
                )
            notes.append("dict 各列长度不一致，按「整条记录」处理")
        # 找嵌套的记录数组
        for key, value in payload.items():
            if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
                notes.append(f"顶层是对象，已取其中的 {key!r} 数组作为表")
                return _json_records_to_table(value, decoded.path, decoded.guess.encoding, decoded.size, notes)
        notes.append("顶层是单个对象，按 1 行记录读取")
        return _json_records_to_table([payload], decoded.path, decoded.guess.encoding, decoded.size, notes)

    raise UnsupportedFormat(f"{path} 的 JSON 顶层类型是 {type(payload).__name__}，无法当作表格")


def read_jsonl_table(path: str | os.PathLike, *, encoding: str | None = None) -> Table:
    decoded = textio.read_decoded(path, encoding=encoding)
    records = []
    bad = 0
    for line in decoded.text.splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            bad += 1
    notes = [f"{bad} 行不是合法 JSON，已跳过"] if bad else []
    if not records:
        raise UnsupportedFormat(f"{path} 中没有合法 JSON 行")
    if not all(isinstance(item, dict) for item in records):
        raise UnsupportedFormat(f"{path} 不是「每行一个对象」的 JSONL")
    return _json_records_to_table(records, decoded.path, decoded.guess.encoding, decoded.size, notes)


def _read_via_pandas(path: str, max_rows: int | None) -> Table:
    from . import deps

    pandas = deps.optional("pandas")
    if pandas is None:
        raise UnsupportedFormat(
            f"读取 {os.path.splitext(path)[1]} 需要 pandas，但当前环境没有。\n"
            "断网时可选方案：\n"
            "  1) 先在联网机器上转成 CSV：python -c \"import pandas as pd; pd.read_excel('x.xlsx').to_csv('x.csv', index=False)\"\n"
            "  2) 或者用 Excel/WPS 另存为「CSV UTF-8」\n"
            "之后再对这个 CSV 跑本工具。"
        )
    frame = pandas.read_excel(path) if os.path.splitext(path)[1].lower() in (".xlsx", ".xls", ".xlsm") else pandas.read_parquet(path)
    if max_rows is not None and len(frame) > max_rows:
        frame = frame.head(max_rows)
    columns = [str(column) for column in frame.columns]
    rows = []
    for record in frame.itertuples(index=False, name=None):
        row = []
        for value in record:
            row.append(None if value is None or value != value else str(value))  # NaN -> None
        rows.append(row)
    return Table(
        source=path,
        columns=columns,
        rows=rows,
        size=os.path.getsize(path),
        delimiter="",
        notes=["通过 pandas 读取"],
        via="pandas",
    )


def read_table(
    path: str | os.PathLike,
    *,
    delimiter: str | None = None,
    encoding: str | None = None,
    max_rows: int | None = DEFAULT_MAX_ROWS,
    has_header: bool = True,
) -> Table:
    """按后缀选择读取器。所有可恢复的问题都记到 ``Table.notes`` 里。"""
    target = os.fspath(path)
    if not os.path.exists(target):
        raise FileNotFoundError(f"找不到文件：{target}")
    suffix = os.path.splitext(target)[1].lower()

    if suffix in NUMPY_SUFFIXES:
        from . import deps

        numpy = deps.optional("numpy")
        if numpy is None:
            raise UnsupportedFormat(f"读取 {suffix} 需要 numpy，但当前环境没有")
        array = numpy.load(target, allow_pickle=False)
        if isinstance(array, numpy.lib.npyio.NpzFile):
            keys = list(array.keys())
            array = array[keys[0]]
        values = numpy.atleast_2d(array)
        if values.shape[0] == 1 and values.shape[1] > 1 and values.ndim == 2:
            values = values.T
        columns = [f"col{index + 1}" for index in range(values.shape[1])]
        cap = max_rows if max_rows is not None else values.shape[0]
        rows = [[str(cell) for cell in values[index]] for index in range(min(cap, values.shape[0]))]
        return Table(source=target, columns=columns, rows=rows, size=os.path.getsize(target), delimiter="", notes=["通过 numpy 读取"], via="numpy")

    if suffix in PANDAS_SUFFIXES:
        try:
            return _read_via_pandas(target, max_rows)
        except UnsupportedFormat:
            raise
        except Exception as exc:  # pandas/引擎抛什么都有可能，统一翻译成可读的结论
            raise UnsupportedFormat(
                f"用 pandas 打开 {target} 失败：{exc}\n"
                "可能的原因：\n"
                "  1) 缺少引擎库（xlsx 需要 openpyxl，parquet 需要 pyarrow）—— 断网时装不上，"
                "请换一台能联网的机器转成 CSV；\n"
                "  2) 文件其实是别的格式或已损坏（用 `ek encoding 文件 --hex` 看文件头，"
                "zip 应以 50 4B 开头）；\n"
                "  3) 文件被 Excel/WPS 占用（Windows 上常见），先关掉再试。"
            ) from exc

    if suffix in (".json",):
        return read_json_table(target, encoding=encoding)
    if suffix in (".jsonl", ".ndjson"):
        return read_jsonl_table(target, encoding=encoding)

    if suffix in (".csv", ".tsv", ".tab"):
        if suffix in (".tsv", ".tab") and delimiter is None:
            delimiter = "\t"
        return read_csv_table(target, delimiter=delimiter, encoding=encoding, max_rows=max_rows, has_header=has_header)

    if suffix in (".txt", ".dat", ""):
        # 单列纯文本（每行一个测量值）的首行是数据而不是表头，需要回退重读
        table = read_csv_table(target, delimiter=delimiter, encoding=encoding, max_rows=max_rows, has_header=has_header)
        if table.n_cols == 1 and has_header and _looks_numeric(table.columns[0]):
            table = read_csv_table(
                target, delimiter=delimiter, encoding=encoding, max_rows=max_rows, has_header=False
            )
            if table.n_cols == 1:
                table.columns = ["value"]
            table.notes.append("首行看起来是数据而不是表头，已按「无表头」读取；列名记为 value")
        if table.n_cols == 1:
            table.notes.append("只解析出 1 列，可能是纯文本/单值列表")
        return table

    # 未知后缀：按 CSV 试读，失败就报清楚
    try:
        return read_csv_table(target, delimiter=delimiter, encoding=encoding, max_rows=max_rows, has_header=has_header)
    except UnsupportedFormat:
        raise
    except Exception as exc:
        raise UnsupportedFormat(f"无法把 {target} 当作表格读取：{exc}") from exc


def read_numeric_column(table: Table, name: str) -> list[float | None]:
    """取一列转成浮点，无法解析的变 ``None``（由调用方决定怎么报）。"""
    from .profile import parse_float

    return [parse_float(value) for value in table.column(name)]


def write_csv(
    path: str | os.PathLike,
    headers,
    rows,
    *,
    encoding: str = "utf-8-sig",
    delimiter: str = ",",
) -> str:
    """写 CSV。默认 ``utf-8-sig``：Windows 上双击用 Excel 打开不会乱码。"""
    target = os.fspath(path)
    parent = os.path.dirname(os.path.abspath(target))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    with open(target, "w", encoding=encoding, newline="") as handle:
        writer = csv.writer(handle, delimiter=delimiter)
        writer.writerow(["" if head is None else head for head in headers])
        for row in rows:
            writer.writerow(["" if cell is None else cell for cell in row])
    return target


def classify_file(path: str | os.PathLike) -> str:
    """粗分文件类型：``table`` / ``log`` / ``text`` / ``code`` / ``notebook`` / ``binary``。

    只看后缀 + 头部少量字节，不做全量解析 —— 分诊要在毫秒级完成。
    """
    target = os.fspath(path)
    suffix = os.path.splitext(target)[1].lower()
    if suffix in TABLE_SUFFIXES or suffix in PANDAS_SUFFIXES or suffix in NUMPY_SUFFIXES:
        if suffix in (".txt", ".dat"):
            return "text_or_table"
        return "table"
    if suffix == ".ipynb":
        return "notebook"
    if suffix in (".py", ".pyw", ".toml", ".cfg", ".ini", ".yaml", ".yml", ".json5", ".sh", ".ps1", ".bat", ".cmd", ".r", ".jl", ".m", ".sql"):
        return "code"
    if suffix in (".log", ".out", ".err", ".trace", ".evtx"):
        return "log"
    if suffix in (".md", ".rst", ".tex", ".csv.bak"):
        return "text"
    # 后缀不认识：看头部是不是文本
    try:
        with open(target, "rb") as handle:
            head = handle.read(4096)
    except OSError:
        return "unknown"
    if b"\x00" in head:
        return "binary"
    try:
        head.decode("utf-8")
    except UnicodeDecodeError:
        return "text"
    return "text"
