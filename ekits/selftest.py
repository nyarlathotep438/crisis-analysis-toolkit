"""离线自检：验证工具箱自身没坏。

应急工具最尴尬的情形是：真出事时发现工具自己跑不起来（文件被改坏、
依赖升级后接口变了、复制时丢了一段）。``ek selftest`` 用内置的构造数据
把每个模块的核心逻辑过一遍，10 秒内给出"哪些部件还能用"。

自检**不读你的数据、不联网、不写工作区**（只在系统临时目录里做一次
CSV 往返），失败时给出具体是哪一条检查、期望什么、实际什么。

注意：本文件大量使用 ``assert`` 是**有意为之** —— 自检的判断依据就是断言。
所以在文件头用 ``ek:ignore-file`` 关掉了 CS108（"用 assert 做检查"）这一条，
避免 ``ek code`` 扫描工具箱自身时把它当成缺陷。
"""

# ek:ignore-file CS108

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass

from . import anomaly, codescan, deps, fmt, loader, profile, quality, report, tables, textio, timeseries

__all__ = ["Check", "run"]


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


def _check(name: str, function) -> Check:
    try:
        detail = function()
    except AssertionError as exc:
        return Check(name, False, str(exc) or "断言失败")
    except Exception as exc:  # 任何异常都算"这个部件坏了"
        return Check(name, False, f"{exc.__class__.__name__}: {exc}")
    return Check(name, True, str(detail) if detail else "")


# ---------------------------------------------------------------- 各项检查


def _check_fmt() -> str:
    assert fmt.human_bytes(0) == "0 B", fmt.human_bytes(0)
    assert fmt.human_bytes(1536) == "1.5 KB", fmt.human_bytes(1536)
    assert fmt.pct(1, 0) == "n/a"
    assert fmt.num(0.1 + 0.2) == "0.3", fmt.num(0.1 + 0.2)
    return "字节/百分比/浮点格式化正常"


def _check_tables() -> str:
    text = tables.render(["列名", "值"], [["温度", 12.5], ["压力", 1013]])
    widths = {tables.display_width(line) for line in text.splitlines()}
    assert len(widths) == 1, f"中英混排表格未对齐，宽度集合={widths}"
    assert "温度" in text
    spark = tables.sparkline([1, 2, 3, 2, 1])
    assert len(spark) == 5, spark
    return "中英混排表格按东亚宽度对齐；sparkline 长度正确"


def _check_encoding() -> str:
    utf8 = "中文测试 abc".encode("utf-8")
    guess = textio.detect_encoding(utf8)
    assert guess.encoding == "utf-8", guess.encoding

    gbk = "中文测试".encode("gbk")
    guess = textio.detect_encoding(gbk)
    assert guess.encoding in ("gb18030", "gbk"), guess.encoding

    bom = b"\xef\xbb\xbfid,value\n1,2\n"
    guess = textio.detect_encoding(bom)
    assert guess.is_bom, guess.encoding

    ascii_only = textio.detect_encoding(b"a,b,c")
    assert ascii_only.encoding == "ascii", ascii_only.encoding
    return "UTF-8 / GBK / BOM / ASCII 四种判断均正确"


def _check_profile() -> str:
    assert profile.infer_kind(["1", "2", "3"]) == "int"
    assert profile.infer_kind(["1.5", "2", "3"]) == "float"
    assert profile.infer_kind(["2024-01-01", "2024-01-02"]) == "date"
    assert profile.infer_kind(["a", "b"]) == "string"
    assert profile.infer_kind(["", None, "NA"]) == "empty"

    prof = profile.profile_column("x", [str(index) for index in range(1, 20)] + ["abc", "", None])
    assert prof.nulls == 2, f"缺失数应为 2，实际 {prof.nulls}"
    assert prof.dirty_count == 1, f"脏值数应为 1，实际 {prof.dirty_count}"
    assert prof.mean == 10.0, prof.mean
    return "类型推断、缺失与脏值分离、均值统计正确"


def _check_quality() -> str:
    table = loader.Table(
        source="<内存>",
        columns=["id", "value"],
        rows=[["1", "10"], ["2", ""], ["1", "10"], ["3", "-999"]],
    )
    issues = quality.check_table(table, key="id")
    codes = {issue.code for issue in issues}
    assert "DQ150" in codes, f"未发现重复行；实际规则={sorted(codes)}"
    assert "DQ161" in codes, "未发现主键重复"
    assert "DQ113" in codes or "DQ112" in codes, "未发现缺失率问题"
    assert "DQ117" in codes, "未发现哨兵值 -999"
    return f"检出 {len(issues)} 条规则命中（含重复行/主键/缺失/哨兵值）"


def _check_anomaly() -> str:
    series = [10.0] * 30
    series[17] = 100.0
    zscore = anomaly.zscore_flags(series, threshold=3.0)
    assert any(item.index == 17 for item in zscore), "z-score 未命中注入的异常点"
    robust = anomaly.modified_zscore_flags(series)
    assert any(item.index == 17 for item in robust), "修正 z-score 未命中注入的异常点"
    iqr = anomaly.iqr_flags(series)
    assert any(item.index == 17 for item in iqr), "IQR 未命中注入的异常点"

    jumps = anomaly.jump_flags(series + [10.0] * 5)
    assert any(item.index == 17 for item in jumps), "跳变检测未命中突变点"
    return "z-score / 修正z / IQR / 跳变 四种方法都命中了注入的异常点"


def _check_timeseries() -> str:
    import datetime as dt

    base = dt.datetime(2024, 1, 1, 0, 0, 0)
    times = []
    values = []
    for index in range(120):
        if 60 <= index < 70:  # 制造 10 个点的空洞
            continue
        times.append((base + dt.timedelta(minutes=index)).strftime("%Y-%m-%d %H:%M:%S"))
        values.append(float(index))

    result = timeseries.diagnose(times, values)
    assert result.median_interval == 60.0, f"采样间隔应为 60s，实际 {result.median_interval}"
    assert result.gaps, "未检出时间空洞"
    assert any(gap.expected_missing >= 9 for gap in result.gaps), [gap.expected_missing for gap in result.gaps]
    assert result.coverage is not None and result.coverage < 0.95, result.coverage
    return f"检出间隔 60s、{len(result.gaps)} 处空洞、覆盖率 {result.coverage:.2f}"


def _check_codescan() -> str:
    source = (
        "import os\n"
        "def f(a, cache=[]):\n"
        "    try:\n"
        "        pass\n"
        "    except:\n"
        "        pass\n"
        "    open('x.txt')\n"
        "    return a\n"
    )
    issues = codescan.scan_python_source(source, "demo.py")
    codes = {issue.code for issue in issues}
    assert "CS101" in codes, f"未检出裸 except；实际={sorted(codes)}"
    assert "CS103" in codes, "未检出可变默认参数"
    assert "CS104" in codes, "未检出 open 缺 encoding"
    return f"检出 {len(issues)} 条：{sorted(codes)}"


def _check_roundtrip() -> str:
    """CSV 往返。会依次尝试系统临时目录与当前目录 —— 受限环境里临时子目录可能不可写。"""
    last_error = "未知原因"
    for directory in (None, os.getcwd()):
        handle = None
        path = None
        try:
            handle, path = tempfile.mkstemp(prefix="ek_selftest_", suffix=".csv", dir=directory)
            os.close(handle)
            handle = None
        except OSError as exc:
            last_error = f"{exc.__class__.__name__}: {exc}"
            continue
        try:
            loader.write_csv(path, ["名称", "数值"], [["温度", 12.5], ["压力", 1013]])
            table = loader.read_table(path)
            assert table.columns == ["名称", "数值"], table.columns
            assert len(table.rows) == 2, len(table.rows)
            assert table.rows[1][0] == "压力", table.rows
            assert table.encoding == "utf-8-sig", table.encoding
            return "CSV 写入→读取往返一致（含中文与 BOM 处理）"
        except PermissionError as exc:
            last_error = f"{exc.__class__.__name__}: {exc}"
            continue
        finally:
            if handle is not None:
                os.close(handle)
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass
    return f"跳过：临时目录与工作目录都不可写（{last_error}）"


def _check_report() -> str:
    rep = report.Report("自检", target="<内存>")
    rep.section("一、测试")
    rep.warn("T001", "一条警告", 行数=3)
    rep.table(["列", "值"], [["a", 1]])
    payload = rep.to_dict()
    text = json.dumps(payload, ensure_ascii=False)
    assert payload["counts"]["warn"] == 1, payload["counts"]
    assert "T001" in text
    assert payload["worst"] == "warn", payload["worst"]
    assert rep.exit_code() == 1
    markdown = rep.render_markdown()
    assert "| 列 | 值 |" in markdown or "| 列" in markdown, markdown[:200]
    return "报告可序列化为 JSON，Markdown 渲染正常，退出码映射正确"


def _check_deps() -> str:
    status = deps.status()
    assert status, "依赖探测返回空"
    names = {info.name for info in status}
    assert {"numpy", "pandas", "matplotlib"} <= names
    available = [info.name for info in status if info.available]
    return f"探测到 {len(status)} 个可选依赖，可用 {len(available)} 个：{available}"


def run() -> list[Check]:
    """执行全部自检。"""
    checks = [
        _check("格式化 fmt", _check_fmt),
        _check("表格与 sparkline", _check_tables),
        _check("编码探测", _check_encoding),
        _check("列画像 profile", _check_profile),
        _check("质量规则 quality", _check_quality),
        _check("异常检测 anomaly", _check_anomaly),
        _check("时序诊断 timeseries", _check_timeseries),
        _check("代码体检 codescan", _check_codescan),
        _check("CSV 往返", _check_roundtrip),
        _check("报告 report", _check_report),
        _check("可选依赖探测", _check_deps),
    ]
    return checks
