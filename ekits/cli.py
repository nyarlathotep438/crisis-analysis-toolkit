"""命令行入口：``python -m ekits <命令>``。

设计目标：**每条命令都能单独用，也能被别的脚本串联**。

* 退出码：``0`` 没发现问题 / ``1`` 有警告 / ``2`` 有严重问题 / ``3`` 工具自身出错。
  所以可以直接 ``ek data_quality.py x.csv && 后续步骤``。
* 所有命令默认只读，写文件必须显式给 ``--out`` / ``--plot`` / ``--apply``。
* 报告可同时输出到终端（人类）和 ``--out``（文件/JSON/Markdown），内容同源。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter

from . import (
    __version__,
    anomaly,
    codescan,
    demo,
    deps,
    envreport,
    files,
    fmt,
    loader,
    plots,
    profile,
    quality,
    report,
    selftest,
    tables,
    textio,
    timeseries,
)

PROG = "ek"

KIND_CN = {
    "int": "整数",
    "float": "浮点",
    "bool": "布尔",
    "datetime": "日期时间",
    "date": "日期",
    "string": "文本",
    "empty": "空",
}

#: 分诊时给出的"下一步"建议，按规则编号索引
_ADVICE = {
    "DQ001": "文件是空的：先确认上传/生成是否成功（{f}）",
    "DQ011": "像二进制文件：用 `{p} encoding {f} --hex` 看文件头",
    "DQ012": "行尾混用：用 `{p} encoding {f} --convert utf-8 --out {f}.fixed` 统一",
    "DQ014": "解码有替换字符：用 `{p} encoding {f}` 确认真实编码后加 `--encoding`",
    "DQ015": "疑似乱码：先解决编码，再谈分析",
    "DQ102": "字段数不一致：字段里可能有未转义的分隔符或裸引号",
    "DQ104": "重复列名：重命名后重跑，否则按列名取值会静默出错",
    "DQ112": "缺失率过高：确认该列是否可用，别直接均值填充",
    "DQ113": "缺失率偏高：用 `{p} ts {f} --time <时间列> --value <列>` 看缺失是否成段",
    "DQ115": "数值列有脏值：用 `{p} profile {f} --columns <列>` 定位",
    "DQ117": "疑似哨兵值（-999 等）：先转成缺失再统计",
    "DQ130": "时间戳重复：合并/去重前先确认是否重复采集",
    "DQ131": "时间不单调：分析前先按时间排序",
    "DQ150": "存在重复行：确认是重复采集还是合法重复，再 drop_duplicates",
    "DQ161": "主键重复：会导致 merge 行数膨胀，优先修复",
    "CS001": "语法错误：文件根本 import 不了，先修这个",
    "CS101": "裸 except：会吞掉 Ctrl+C，改成 except Exception",
    "CS102": "异常被静默吞掉：错误之后的所有结果都不可信",
    "CS104": "open 没写 encoding：Windows/Linux 读出的中文不同",
    "CS110": "硬编码绝对路径：换机器就失败",
    "CS120": "缺依赖且断网无法安装：确认是否装在别的环境，或换标准库实现",
    "NB002": "notebook 里存着报错输出：说明上次没跑通",
    "NB003": "notebook 执行顺序乱：全选后从上到下重跑再下结论",
    "ENV001": "PYTHONHOME 被设置：先撤销它再看问题是否消失",
    "ENV007": "PATH 里多个 python：确认 pip 装到了哪个解释器",
    "ENV008": "工作目录不可写：任何落盘操作都会失败",
    "ENV010": "磁盘空间不足：先腾空间，报错往往与此无关地表现为各种失败",
}


# ---------------------------------------------------------------- 基础设施


def _harden_stdio() -> None:
    """把 stdout/stderr 编不过的字符变成替换符，而不是让报告崩在打印阶段。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def _emit(rep: report.Report, args) -> int:
    """统一出口：终端 + 可选文件。"""
    if getattr(args, "json", False):
        text = json.dumps(rep.to_dict(), ensure_ascii=False, indent=2, default=str)
    else:
        text = rep.render()
    if not getattr(args, "quiet", False):
        print(text)
    out = getattr(args, "out", None)
    if out:
        written = rep.dump(out)
        if not getattr(args, "quiet", False):
            print(f"\n报告已写入: {written}")
    return rep.exit_code()


def _add_common(sub: argparse.ArgumentParser, *, out_help: str | None = None) -> None:
    sub.add_argument("--json", action="store_true", help="以 JSON 输出（便于机器处理与存档）")
    sub.add_argument(
        "--out",
        metavar="文件",
        help=out_help or "把报告另存为文件；按后缀选格式：.txt（纯文本）/ .md（Markdown）/ .json",
    )
    sub.add_argument("-q", "--quiet", action="store_true", help="不在终端打印，只配合 --out 使用")


def _load(args, path: str):
    return loader.read_table(
        path,
        delimiter=getattr(args, "delimiter", None),
        encoding=getattr(args, "encoding", None),
        max_rows=getattr(args, "max_rows", loader.DEFAULT_MAX_ROWS),
    )


def _render_issues(rep: report.Report, issues, *, limit: int = 40, note_total: bool = True) -> None:
    """把 quality / codescan 的 Issue 统一灌进报告。"""
    shown = 0
    for issue in issues:
        if shown >= limit:
            break
        extra: dict = {}
        detail = getattr(issue, "detail", "") or ""
        hint = getattr(issue, "hint", "") or ""
        where = issue.where() if hasattr(issue, "where") else ""
        if detail:
            extra["细节"] = detail
        if hint:
            extra["建议"] = hint
        rep.add(issue.level, issue.code, issue.title if hasattr(issue, "title") else issue.message, where=where or None, **extra)
        shown += 1
    if note_total and len(issues) > limit:
        rep.note(f"共 {len(issues)} 条结论，仅显示前 {limit} 条（用 --limit 调整）")


def _range_text(prof: profile.ColumnProfile) -> str:
    if prof.is_numeric:
        return f"{fmt.num(prof.minimum)} ~ {fmt.num(prof.maximum)}"
    if prof.is_temporal:
        return f"{prof.dt_min} ~ {prof.dt_max}"
    if prof.kind == "empty":
        return "-"
    return f"长度 {prof.min_len}~{prof.max_len}"


def _extra_text(prof: profile.ColumnProfile) -> str:
    if prof.is_numeric:
        parts = [f"中位 {fmt.num(prof.median)}"]
        if prof.dirty_count:
            parts.append(f"脏值 {prof.dirty_count}")
        return "，".join(parts)
    if prof.is_temporal:
        return f"唯一 {prof.unique}"
    if prof.examples:
        return f"例 {prof.examples[0][:22]}"
    return ""


def _pick_columns(table, profiles, time_col=None, value_col=None) -> tuple[str | None, str | None]:
    """自动挑时间列与数值列，挑不出来返回 None 由调用方报错。"""
    if time_col is None:
        temporal = [item for item in profiles if item.is_temporal]
        if temporal:
            time_col = temporal[0].name
        else:
            lowered = {name.lower(): name for name in table.columns}
            for candidate in ("timestamp", "time", "datetime", "date", "采集时间", "时间", "日期", "ts"):
                if candidate in lowered:
                    time_col = lowered[candidate]
                    break
    if value_col is None:
        numeric = [item for item in profiles if item.is_numeric and item.non_null >= 3]
        if numeric:
            value_col = numeric[0].name
    return time_col, value_col


# ---------------------------------------------------------------- profile


def cmd_profile(args) -> int:
    rep = report.Report("数据结构速览", target=", ".join(args.paths))
    wanted = [item.strip() for item in args.columns.split(",")] if args.columns else None

    for path in args.paths:
        rep.section(os.path.basename(path))
        table = _load(args, path)
        rep.text(f"路径: {os.path.abspath(path)}")
        rep.text(
            f"规模: {fmt.human_count(len(table))} 行 × {table.n_cols} 列 · "
            f"文件 {fmt.human_bytes(table.size)} · 编码 {table.encoding} · 分隔符 {table.delimiter!r}"
        )
        for note in table.notes:
            rep.note(f"{os.path.basename(path)}: {note}")

        profiles = profile.profile_table(table)
        if wanted:
            missing = [name for name in wanted if name not in table.columns]
            if missing:
                rep.error("PROF000", f"指定的列不存在：{missing}", 现有列=table.columns[:20])
            profiles = [item for item in profiles if item.name in wanted]

        shown = profiles[: args.max_columns]
        if len(profiles) > len(shown):
            rep.note(f"共 {len(profiles)} 列，仅显示前 {len(shown)} 列（用 --columns 指定或调大 --max-columns）")

        rows = []
        for item in shown:
            rows.append(
                [
                    item.name,
                    KIND_CN.get(item.kind, item.kind),
                    fmt.human_count(item.non_null),
                    f"{item.null_pct:.1f}%" if item.nulls else "-",
                    fmt.human_count(item.unique) + ("+" if item.unique_capped else ""),
                    _range_text(item),
                    _extra_text(item),
                ]
            )
        rep.table(
            ["列名", "类型", "非空", "缺失", "唯一值", "取值范围", "中位/示例"],
            rows,
            caption="列画像",
        )

        for item in shown:
            for line in item.notes:
                rep.info("PROF001", f"{item.name}: {line}")

        if args.head and table.rows:
            rep.table(
                table.columns,
                table.head(args.head),
                caption=f"前 {min(args.head, len(table.rows))} 行",
                max_col=28,
            )

        if args.hist:
            numeric = [item for item in profiles if item.is_numeric and item.non_null >= 5]
            if numeric:
                target = numeric[0]
                values = [profile.parse_float(cell) for cell in table.column(target.name)]
                rep.text(f"数值分布（{target.name}）:")
                rep.text(tables.histogram([value for value in values if value is not None]))
            else:
                rep.note("没有可画直方图的数值列")

    return _emit(rep, args)


# ---------------------------------------------------------------- quality


def cmd_quality(args) -> int:
    rep = report.Report("数据体检", target=args.path)
    table = _load(args, args.path)

    rep.section("一、原始文件")
    rep.table(
        ["项目", "值"],
        [
            ["大小", fmt.human_bytes(table.size)],
            ["编码", table.encoding],
            ["分隔符", repr(table.delimiter)],
            ["行 × 列", f"{fmt.human_count(len(table))} × {table.n_cols}"],
            ["读取方式", table.via],
            ["截断行数", str(table.truncated_rows)],
        ],
    )

    raw_issues = quality.check_raw_file(args.path, encoding=args.encoding)
    profiles = profile.profile_table(table)
    data_issues = quality.check_table(
        table,
        key=args.key,
        missing_warn=args.missing_warn,
        missing_error=args.missing_error,
        profiles=profiles,
    )
    all_issues = raw_issues + data_issues

    rep.section("二、检查结论")
    counts = Counter(issue.level for issue in all_issues)
    rep.text(
        "命中 "
        f"{len(all_issues)} 条：严重 {counts.get('error', 0)} · 警告 {counts.get('warn', 0)} · "
        f"提示 {counts.get('info', 0)} · 正常 {counts.get('ok', 0)}"
    )
    _render_issues(rep, all_issues, limit=args.limit)

    rep.section("三、列概览")
    rep.table(
        ["列名", "类型", "缺失率", "唯一值", "取值范围"],
        [
            [
                item.name,
                KIND_CN.get(item.kind, item.kind),
                f"{item.null_pct:.1f}%",
                fmt.human_count(item.unique),
                _range_text(item),
            ]
            for item in profiles[: args.max_columns]
        ],
    )
    if len(profiles) > args.max_columns:
        rep.note(f"共 {len(profiles)} 列，仅显示前 {args.max_columns} 列")

    if not any(issue.level in ("warn", "error") for issue in all_issues):
        rep.ok("DQ000", "没有发现明显的结构性问题", 说明="仍建议抽查前几行与关键列的分布")
    return _emit(rep, args)


# ---------------------------------------------------------------- anomaly


def cmd_anomaly(args) -> int:
    rep = report.Report("异常点扫描", target=args.path)
    table = _load(args, args.path)
    profiles = profile.profile_table(table)
    time_col, _ = _pick_columns(table, profiles, time_col=args.time)

    methods = [item.strip() for item in args.methods.split(",") if item.strip()] if args.methods else [
        "z",
        "modz",
        "iqr",
        "jump",
    ]

    if args.all:
        numeric = [item for item in profiles if item.is_numeric and item.non_null >= 8]
        if not numeric:
            rep.error("ANOM000", "没有足够的数值列可扫描", 说明="至少需要 8 个可解析数值")
            return _emit(rep, args)
        rep.section("各数值列异常点数量")
        rows = []
        for item in numeric[: args.max_columns]:
            values = table.column(item.name)
            flags: dict[str, list] = {}
            for method in methods:
                flags[method] = _run_method(method, values, args)
            merged = anomaly.consensus(flags, top=5)
            rows.append(
                [
                    item.name,
                    fmt.human_count(item.non_null),
                    "，".join(f"{key}:{len(value)}" for key, value in flags.items()),
                    str(len(merged)),
                    "，".join(str(entry["index"] + 1) for entry in merged[:3]),
                ]
            )
        rep.table(["列名", "有效值", "各方法命中数", "去重后", "最可疑行号"], rows)
        rep.note("行号从 1 开始，指「数据行」序号（不含表头）；CSV 第 N 行 = 行号 + 1")
        return _emit(rep, args)

    value_col = args.column
    if value_col is None:
        _, value_col = _pick_columns(table, profiles)
        if value_col is None:
            rep.error("ANOM001", "无法确定要分析的数值列", 说明="用 --column 显式指定一个数值列")
            return _emit(rep, args)
    if value_col not in table.columns:
        rep.error("ANOM002", f"列不存在：{value_col}", 现有列=table.columns[:20])
        return _emit(rep, args)

    values = table.column(value_col)
    flags: dict[str, list] = {}
    for method in methods:
        flags[method] = _run_method(method, values, args)

    rep.section(f"一、{value_col} 的异常点")
    counts = {key: len(value) for key, value in flags.items()}
    rep.text("各方法命中：" + "  ".join(f"{key}={count}" for key, count in counts.items()))
    rep.text("方法说明（各自假设不同，结论靠投票而非单一判据）：")
    for method in methods:
        note_text = anomaly.METHODS.get(method)
        if note_text:
            rep.text(f"  * {method:<5} {note_text}")

    merged = anomaly.consensus(flags, top=args.top)
    if not merged:
        rep.ok("ANOM003", "这些方法都没有命中异常点", 说明="要么数据确实干净，要么异常类型不在这些方法的假设内（如整段文本错位）")
    else:
        rows = []
        for entry in merged:
            position = entry["index"]
            row_number = position + 1
            stamp = "-"
            if time_col:
                try:
                    stamp = str(table.column(time_col)[position])
                except (KeyError, IndexError):
                    stamp = "-"
            rows.append(
                [
                    row_number,
                    stamp,
                    tables.elide(str(table.column(value_col)[position]), 24),
                    "，".join(sorted(set(entry["methods"]))),
                    entry["votes"],
                    fmt.num(entry["max_score"]),
                    "；".join(entry["notes"])[:40],
                ]
            )
        rep.table(
            ["数据行号", "时间", "原始值", "命中方法", "票数", "最高得分", "说明"],
            rows,
            caption=f"按可疑程度排序（前 {len(rows)} 个）",
        )
        rep.note("票数 ≥ 2 的点优先人工核对原始记录；只被一种方法命中的点多半是该方法本身的偏好")

    rep.section("二、数值基线")
    numeric_only = [profile.parse_float(cell) for cell in values]
    numeric_only = [value for value in numeric_only if value is not None]
    if numeric_only:
        ordered = sorted(numeric_only)
        rep.table(
            ["指标", "值"],
            [
                ["有效值数", fmt.human_count(len(numeric_only))],
                ["最小值", fmt.num(ordered[0])],
                ["P05", fmt.num(profile.quantile(ordered, 0.05))],
                ["P25", fmt.num(profile.quantile(ordered, 0.25))],
                ["中位数", fmt.num(profile.quantile(ordered, 0.5))],
                ["P75", fmt.num(profile.quantile(ordered, 0.75))],
                ["P95", fmt.num(profile.quantile(ordered, 0.95))],
                ["最大值", fmt.num(ordered[-1])],
                ["均值", fmt.num(sum(numeric_only) / len(numeric_only))],
                ["标准差", fmt.num(profile.stdev(numeric_only))],
            ],
        )
        rep.text("分布直方图:")
        rep.text(tables.histogram(numeric_only))
        rep.text("形状（sparkline）:")
        rep.text(tables.sparkline([profile.parse_float(cell) for cell in values], width=70))
    else:
        rep.error("ANOM004", "该列没有可解析的数值")

    if args.plot:
        try:
            written = plots.plot_series(
                args.plot,
                _times_for_plot(table, time_col),
                [profile.parse_float(cell) for cell in values],
                title=f"{os.path.basename(args.path)} · {value_col}",
                anomalies=[(entry["index"], entry["value"]) for entry in merged],
                ylabel=value_col,
            )
            _plot_note(rep, written)
        except (plots.PlotUnavailable, ValueError) as exc:
            rep.warn("ANOM005", "出图失败", 原因=str(exc).splitlines()[0], 建议="用文本 sparkline 代替")

    return _emit(rep, args)


def _run_method(method: str, values, args) -> list:
    if method == "z":
        return anomaly.zscore_flags(values, threshold=args.threshold)
    if method == "modz":
        return anomaly.modified_zscore_flags(values, threshold=max(args.threshold, 3.5))
    if method == "iqr":
        return anomaly.iqr_flags(values, k=args.iqr_k)
    if method == "jump":
        return anomaly.jump_flags(values, threshold=args.threshold + 1)
    if method == "roll":
        return anomaly.rolling_zscore_flags(values, window=args.window, threshold=args.threshold)
    if method == "cusum":
        # CUSUM 返回的是"区间"，但共识投票用的是"点"：把每个区间的终点当作
        # 可疑点（漂移真正被确认的那一刻），区间本身写在说明里。
        segments = anomaly.cusum_segments(values)
        positions, numbers = anomaly.numeric_series(values)
        value_of = dict(zip(positions, numbers))
        return [
            anomaly.Anomaly(
                segment.end_index,
                value_of.get(segment.end_index, float("nan")),
                segment.magnitude,
                "cusum",
                f"{segment.direction}偏移：行 {segment.start_index + 1} → {segment.end_index + 1}",
            )
            for segment in segments
        ]
    return []


def _plot_note(rep: report.Report, written: str) -> None:
    """出图成功后的附注：顺带确认中文能不能正常显示。"""
    rep.note(f"图已写入: {written}")
    font = plots.cjk_font()
    if font is None:
        rep.warn(
            "PLOT010",
            "没找到可用的中文字体，图里的中文会显示成方块",
            建议="安装 SimHei / 微软雅黑 / Noto Sans CJK 任一款，或把 --title 换成英文",
        )
    else:
        rep.note(f"图内中文使用字体: {font}")


def _times_for_plot(table, time_col):
    if not time_col:
        return list(range(len(table.rows)))
    parsed = []
    for cell in table.column(time_col):
        moment = profile.parse_datetime(cell)
        parsed.append(moment)
    if any(item is not None for item in parsed):
        return parsed
    return list(range(len(table.rows)))


# ---------------------------------------------------------------- ts


def cmd_ts(args) -> int:
    rep = report.Report("时间序列诊断", target=args.path)
    table = _load(args, args.path)
    profiles = profile.profile_table(table)
    time_col, value_col = _pick_columns(table, profiles, time_col=args.time, value_col=args.value)

    if time_col is None:
        rep.error(
            "TS001",
            "找不到时间列",
            说明="本命令需要一列可解析的时间；用 --time 指定列名",
            现有列=table.columns[:20],
        )
        return _emit(rep, args)
    if value_col is None:
        rep.error("TS002", "找不到数值列", 说明="用 --value 指定要分析的数值列", 现有列=table.columns[:20])
        return _emit(rep, args)

    times = table.column(time_col)
    values = table.column(value_col)
    diag = timeseries.diagnose(times, values, gap_factor=args.gap_factor, flat_min=args.flat_min)

    rep.section("一、基本信息")
    rep.table(
        ["项目", "值"],
        [
            ["时间列", time_col],
            ["数值列", value_col],
            ["数据行数", fmt.human_count(diag.n_total)],
            ["有效时间点", fmt.human_count(diag.n_parsed)],
            ["时间范围", f"{diag.start} ~ {diag.end}"],
            ["跨度", fmt.duration(diag.span_seconds)],
            ["中位采样间隔", f"{fmt.num(diag.median_interval)} 秒" if diag.median_interval else "无法推断"],
            ["理论点数", fmt.num(diag.expected_points) if diag.expected_points else "?"],
            ["实际覆盖率", fmt.pct((diag.coverage or 0) * 100, 100) if diag.coverage is not None else "?"],
        ],
    )

    rep.section("二、时间轴健康度")
    flags: list[tuple[str, str, str]] = []
    if diag.n_duplicate_times:
        rep.warn("TS101", f"存在 {diag.n_duplicate_times} 个重复时间戳", 建议="重采样前先去重，否则同一时刻会被重复加权")
    if diag.n_out_of_order:
        rep.warn("TS102", f"存在 {diag.n_out_of_order} 处时间倒序", 建议="分析前按时间排序")
    if diag.n_unparsable:
        rep.info("TS103", f"{diag.n_unparsable} 个时间值无法解析", 建议="确认是否混入了别的日期格式")
    if diag.regular_share is not None:
        level = "ok" if diag.regular_share >= 0.95 else ("info" if diag.regular_share >= 0.8 else "warn")
        rep.add(
            level,
            "TS104",
            f"采样间隔规整度 {diag.regular_share * 100:.1f}%",
            **{
                "说明": f"以 {fmt.num(diag.median_interval)} 秒为准，偏差超过 {args.regularity_tolerance * 100:.0f}% 视为不规整",
                "间隔变异系数": fmt.num(diag.interval_cv),
            },
        )
    if diag.interval_top:
        rep.table(
            ["间隔(秒)", "出现次数", "占比"],
            [
                [fmt.num(seconds), fmt.human_count(count), fmt.pct(count, diag.n_parsed - 1)]
                for seconds, count in diag.interval_top
            ],
            caption="最常见的采样间隔",
        )

    rep.section("三、缺失与断档")
    if not diag.gaps:
        rep.ok("TS110", "没有发现明显的时间断档")
    else:
        total_missing = sum(gap.expected_missing for gap in diag.gaps)
        rep.warn(
            "TS111",
            f"发现 {len(diag.gaps)} 处时间断档，估计缺失 {fmt.human_count(total_missing)} 个采样点",
            建议="断档期间到底是「没采集」还是「采集了没存」，两种情况的处理完全不同",
        )
        rep.table(
            ["上一个点", "下一个点", "间隔", "估计缺失点数"],
            [
                [
                    gap.prev_time,
                    gap.next_time,
                    fmt.duration(gap.seconds),
                    str(gap.expected_missing),
                ]
                for gap in diag.gaps[:15]
            ],
        )
        if len(diag.gaps) > 15:
            rep.note(f"共 {len(diag.gaps)} 处，仅显示最长的 15 处")

    if diag.flatlines:
        rep.section("四、疑似传感器卡死")
        rep.warn(
            "TS120",
            f"存在 {len(diag.flatlines)} 段连续相同取值",
            建议="连续不变通常是串口断线/程序卡住后保持最后值，不是「数据很稳」",
        )
        rep.table(
            ["长度(点)", "开始行号", "结束行号", "持续时长", "取值"],
            [
                [
                    str(item.length),
                    str(item.start_index + 1),
                    str(item.end_index + 1),
                    fmt.duration(item.seconds) if item.seconds else "?",
                    fmt.num(item.value),
                ]
                for item in diag.flatlines
            ],
        )
    else:
        rep.section("四、疑似传感器卡死")
        rep.ok("TS121", "没有发现连续相同的平直段")

    rep.section("五、数值与趋势")
    numeric_values = [profile.parse_float(cell) for cell in values]
    numeric_values = [value for value in numeric_values if value is not None]
    ordered_values = sorted(numeric_values)
    p02 = profile.quantile(ordered_values, 0.02)
    p98 = profile.quantile(ordered_values, 0.98)
    rep.table(
        ["指标", "值"],
        [
            ["有效值", fmt.human_count(diag.n_values)],
            ["最小 / 最大", f"{fmt.num(diag.value_min)} / {fmt.num(diag.value_max)}"],
            ["P02 / P98", f"{fmt.num(p02)} / {fmt.num(p98)}"],
            ["均值 / 中位", f"{fmt.num(diag.value_mean)} / {fmt.num(diag.value_median)}"],
            ["标准差", fmt.num(diag.value_std)],
            ["线性趋势", f"{fmt.num(diag.slope_per_hour)} / 小时" if diag.slope_per_hour is not None else "无法计算"],
            ["趋势 R²", fmt.num(diag.trend_r2)],
            ["前半段 / 后半段均值", f"{fmt.num(diag.first_half_mean)} / {fmt.num(diag.second_half_mean)}"],
            ["后半段相对变化", f"{diag.half_delta_pct:+.2f}%" if diag.half_delta_pct is not None else "?"],
        ],
    )
    rep.text("形状（sparkline，从左到右为时间顺序；纵向按 P02~P98 裁剪，避免单个极值把图形压平）:")
    rep.text(tables.sparkline(numeric_values, width=80, clip=(0.02, 0.98)))

    if diag.trend_r2 is not None and diag.trend_r2 > 0.5 and diag.slope_per_hour:
        rep.warn(
            "TS130",
            f"存在明显线性趋势：{fmt.num(diag.slope_per_hour)}/小时（R²={diag.trend_r2:.2f}）",
            建议="漂移会让「整段」数据偏移；建模前先去趋势，或确认这是物理上的真实变化",
        )
    if diag.half_delta_pct is not None and abs(diag.half_delta_pct) > 10:
        rep.info(
            "TS131",
            f"后半段均值相对前半段变化 {diag.half_delta_pct:+.1f}%",
            建议="结合 sparkline 判断是阶跃还是渐变；阶跃通常是换了仪器/量程",
        )
    for correlation, lag in diag.acf:
        rep.info(
            "TS140",
            f"疑似周期：滞后 {int(lag)} 点（相关系数 {correlation:.2f}）",
            换算=f"约 {fmt.num(lag * (diag.median_interval or 0))} 秒",
        )

    for note in diag.notes:
        rep.note(note)

    if args.plot:
        try:
            written = plots.plot_series(
                args.plot,
                _times_for_plot(table, time_col),
                [profile.parse_float(cell) for cell in values],
                title=f"{os.path.basename(args.path)} · {value_col}",
                xlabel=time_col,
                ylabel=value_col,
                gap_index_spans=[(gap.prev_index, gap.next_index) for gap in diag.gaps],
            )
            _plot_note(rep, written)
        except (plots.PlotUnavailable, ValueError) as exc:
            rep.warn("TS150", "出图失败", 原因=str(exc).splitlines()[0])

    return _emit(rep, args)


# ---------------------------------------------------------------- plot


def cmd_plot(args) -> int:
    rep = report.Report("快速出图", target=args.path)
    table = _load(args, args.path)
    profiles = profile.profile_table(table)
    out = args.plot_out or "quick_plot.png"
    try:
        if args.kind == "hist":
            column = args.column or _pick_columns(table, profiles)[1]
            if not column:
                rep.error("PLOT001", "找不到数值列", 说明="用 --column 指定")
                return _emit(rep, args)
            values = [profile.parse_float(cell) for cell in table.column(column)]
            written = plots.plot_value_hist(out, [value for value in values if value is not None], title=f"{column}", bins=args.bins)
        elif args.kind == "missing":
            nulls = []
            totals = []
            for item in profiles:
                nulls.append(item.nulls + item.suspect_nulls)
                totals.append(max(item.total, 1))
            written = plots.plot_null_ratio(out, [item.name for item in profiles], nulls, totals, title="各列缺失率")
        else:
            time_col, value_col = _pick_columns(table, profiles, time_col=args.time, value_col=args.column)
            if value_col is None:
                rep.error("PLOT002", "找不到数值列", 说明="用 --column 指定")
                return _emit(rep, args)
            values = [profile.parse_float(cell) for cell in table.column(value_col)]
            written = plots.plot_series(
                out,
                _times_for_plot(table, time_col),
                values,
                title=f"{os.path.basename(args.path)} · {value_col}",
                xlabel=time_col or "",
                ylabel=value_col,
            )
        rep.ok("PLOT000", f"图已生成：{written}", 说明="用图片查看器打开；本命令不会弹窗")
    except plots.PlotUnavailable as exc:
        rep.error("PLOT003", "没有可用的 matplotlib", 说明=str(exc).splitlines()[0])
    except ValueError as exc:
        rep.error("PLOT004", "无法出图", 原因=str(exc))
    return _emit(rep, args)


# ---------------------------------------------------------------- log


def cmd_log(args) -> int:
    rep = report.Report("日志诊断", target=args.path)
    decoded = textio.read_decoded(args.path, encoding=args.encoding)
    facts = textio.analyze_text(decoded)
    scan = textio.scan_log(decoded.text, top=args.top)

    rep.section("一、文件事实")
    rep.table(
        ["项目", "值"],
        [
            ["大小", fmt.human_bytes(decoded.size)],
            ["编码", decoded.guess.describe()],
            ["行数", fmt.human_count(facts.lines)],
            ["空行", f"{fmt.human_count(facts.blank_lines)}（{fmt.pct(facts.blank_lines, facts.lines)}）"],
            ["最长行", f"{fmt.human_count(facts.max_line_len)} 字符（第 {facts.max_line_no} 行）"],
            ["行尾", f"CRLF {facts.crlf} / LF {facts.lf_only} / CR {facts.cr_only}"],
            ["末尾换行", "有" if facts.ends_with_newline else "无"],
        ],
    )
    for note in decoded.guess.notes:
        rep.note("编码判断: " + note)
    if facts.line_ending_mix:
        rep.warn("LOG001", "行尾符混用", 建议="按行 grep/diff 时会出现幽灵差异；先统一行尾")
    if facts.replacement_chars:
        rep.error(
            "LOG002",
            f"存在 {facts.replacement_chars} 个解码替换字符",
            建议="用 --encoding 指定真实编码，例如中文 Windows 日志常见 gb18030",
        )
    for hint in facts.mojibake:
        rep.warn("LOG003", "疑似乱码", 细节=hint)
    if facts.has_null_bytes:
        rep.warn("LOG004", "文件含 NUL 字节", 建议="可能不是纯文本日志，确认是否二进制或已损坏")

    rep.section("二、级别分布")
    total = max(scan.non_empty_lines, 1)
    rows = []
    for level, count in sorted(scan.levels.items(), key=lambda item: -item[1]):
        rows.append([level, fmt.human_count(count), fmt.pct(count, total), tables.bar(count, max(scan.levels.values() or [1]), 22)])
    rep.table(["级别", "条数", "占比", "分布"], rows)
    errors = sum(count for level, count in scan.levels.items() if level in ("ERROR", "CRITICAL", "FATAL"))
    if errors:
        rep.warn(
            "LOG010",
            f"共 {fmt.human_count(errors)} 条 ERROR 及以上日志",
            建议="先看下面聚成同一类的签名，抽样几条完整堆栈，比逐条翻阅快得多",
        )
    else:
        rep.ok("LOG011", "没有 ERROR 及以上级别的日志", 说明="如果功能仍异常，可能是日志级别配置把错误藏起来了")

    if scan.first_ts and scan.last_ts:
        rep.text(
            f"时间范围（从日志内容里抽出的时间戳）: {scan.first_ts.isoformat(sep=' ')} ~ "
            f"{scan.last_ts.isoformat(sep=' ')}，跨度 {fmt.duration((scan.last_ts - scan.first_ts).total_seconds())}"
        )

    rep.section("三、错误签名聚类")
    if scan.signatures:
        rep.text("同一类错误被合并成一行（数字/路径/UUID/时间戳已被归一化）:")
        rep.table(
            ["次数", "占比(错误)", "典型内容"],
            [
                [fmt.human_count(count), fmt.pct(count, max(errors, 1)), tables.elide(sample, 96)]
                for count, sample in scan.signatures
            ],
            max_col=96,
        )
        if len(scan.signatures) == 1 and scan.signatures[0][0] > 10:
            rep.info("LOG020", "所有错误都是同一类", 建议="这类「重复报错」通常是同一次故障反复重试，修一处即可")
    else:
        rep.ok("LOG021", "没有可聚类的错误行")

    if scan.exceptions:
        rep.section("四、异常类型")
        rep.table(["异常/错误类", "出现次数"], [[name, fmt.human_count(count)] for name, count in scan.exceptions])

    if scan.suspicious:
        rep.section("五、危险关键词命中")
        rep.table(
            ["行号", "内容"],
            [[str(number), tables.elide(text, 96)] for number, text in scan.suspicious],
            max_col=96,
        )

    if args.grep:
        import re

        try:
            pattern = re.compile(args.grep)
        except re.error as exc:
            rep.error("LOG030", f"正则表达式无效：{exc}")
        else:
            matches = [
                (number, line)
                for number, line in enumerate(decoded.text.splitlines(), start=1)
                if pattern.search(line)
            ]
            rep.section(f"六、匹配 --grep {args.grep!r}")
            rep.text(f"命中 {len(matches)} 行")
            rep.table(
                ["行号", "内容"],
                [[str(number), tables.elide(line, 110)] for number, line in matches[: args.top]],
                max_col=110,
            )

    return _emit(rep, args)


# ---------------------------------------------------------------- encoding


def cmd_encoding(args) -> int:
    rep = report.Report("编码与文本诊断", target=args.path)
    decoded = textio.read_decoded(args.path, encoding=args.encoding)
    facts = textio.analyze_text(decoded)
    raw_head = b""
    try:
        with open(args.path, "rb") as handle:
            raw_head = handle.read(64)
    except OSError:
        pass

    rep.section("一、编码判断")
    rep.table(
        ["项目", "值"],
        [
            ["推断编码", decoded.guess.encoding],
            ["判断依据", decoded.guess.method],
            ["把握程度", decoded.guess.confidence],
            ["BOM", "有" if decoded.guess.is_bom else "无"],
            ["解码替换字符", fmt.human_count(facts.replacement_chars)],
            ["文件大小", fmt.human_bytes(decoded.size)],
        ],
    )
    for note in decoded.guess.notes:
        rep.info("ENC001", note)
    if decoded.guess.confidence != "high":
        rep.warn(
            "ENC002",
            "编码判断把握不高",
            建议="抽查前 20 行中文是否正常；必要时用 --encoding gb18030 或 utf-8 显式指定后再看",
        )

    rep.section("二、纯文本体检")
    rep.table(
        ["项目", "值"],
        [
            ["行数", fmt.human_count(facts.lines)],
            ["空行", fmt.human_count(facts.blank_lines)],
            ["行尾 CRLF/LF/CR", f"{facts.crlf} / {facts.lf_only} / {facts.cr_only}"],
            ["末尾换行", "有" if facts.ends_with_newline else "无"],
            ["最长行", f"{fmt.human_count(facts.max_line_len)} 字符（第 {facts.max_line_no} 行）"],
            ["非 ASCII 字符", fmt.human_count(facts.non_ascii_chars)],
            ["控制字符", fmt.human_count(facts.control_chars)],
            ["含 NUL", "是" if facts.has_null_bytes else "否"],
            ["疑似二进制", "是" if facts.looks_binary else "否"],
            ["首尾带空白的行", fmt.human_count(facts.trailing_space_lines)],
        ],
    )
    if facts.line_ending_mix:
        rep.warn("ENC010", "行尾符混用", 建议="用 --convert 统一成 CRLF 或 LF")
    if not facts.ends_with_newline and facts.lines > 1:
        rep.info("ENC011", "文件末尾没有换行符", 建议="最后一行可能被追加写入截断")
    for hint in facts.mojibake:
        rep.warn("ENC012", "疑似乱码", 细节=hint)
    if facts.looks_binary:
        rep.error(
            "ENC013",
            "这个文件看起来不是纯文本",
            建议="确认后缀是否与实际内容一致；压缩包/Excel/parquet 需要先解压或换命令",
        )

    if args.hex:
        rep.section("三、文件头（hex + 可打印）")
        rows = []
        for offset in range(0, len(raw_head), 16):
            chunk = raw_head[offset : offset + 16]
            hex_part = " ".join(f"{byte:02X}" for byte in chunk)
            ascii_part = "".join(chr(byte) if 32 <= byte < 127 else "." for byte in chunk)
            rows.append([f"{offset:04X}", hex_part, ascii_part])
        rep.table(["偏移", "十六进制", "ASCII"], rows)

    if args.convert:
        target_encoding = textio.coerce_cp936(args.convert)
        if not args.to and not args.inplace:
            rep.error(
                "ENC020",
                "转码需要指定输出位置",
                说明="用 --to 新文件.csv（推荐，不动原文件），或用 --inplace 覆盖（会先备份 .bak）",
                注意="报告本身用 --out 保存，两者不要混用",
            )
            return _emit(rep, args)
        try:
            target_path = _convert_file(args.path, decoded, target_encoding, args.to, args.inplace)
        except OSError as exc:
            rep.error("ENC021", "转码失败", 原因=str(exc))
        else:
            rep.ok(
                "ENC022",
                f"已转换：{decoded.guess.encoding} → {target_encoding}",
                输出=target_path,
                说明="转码只改编码不改内容；转完再跑一遍 data_quality 确认中文没被破坏",
            )

    return _emit(rep, args)


def _convert_file(path: str, decoded: textio.DecodedText, target_encoding: str, to_path: str | None, inplace: bool) -> str:
    text = decoded.text
    if inplace:
        backup = path + ".bak"
        if not os.path.exists(backup):
            with open(path, "rb") as source, open(backup, "wb") as backup_handle:
                backup_handle.write(source.read())
        target_path = path
    else:
        target_path = to_path or (path + ".converted")
    with open(target_path, "w", encoding=target_encoding, newline="") as handle:
        handle.write(text)
    return target_path


# ---------------------------------------------------------------- env


def cmd_env(args) -> int:
    rep = report.Report("环境与系统快照", target=os.getcwd())
    snapshot = envreport.collect(root=args.root or os.getcwd(), include_git=not args.no_git)

    for title, rows in snapshot.sections:
        rep.section(title)
        rep.table(["项目", "值"], rows, max_col=110)

    rep.section("问题判断")
    if not snapshot.issues:
        rep.ok("ENV000", "没有发现明显的环境问题")
    order = {"error": 0, "warn": 1, "info": 2}
    for level, code, message in sorted(snapshot.issues, key=lambda item: order.get(item[0], 3)):
        rep.add(level, code, message)

    for note in snapshot.notes:
        rep.note(note)
    return _emit(rep, args)


# ---------------------------------------------------------------- fs


def cmd_fs(args) -> int:
    root = args.path
    if not os.path.exists(root):
        rep = report.Report("目录体检", target=root)
        rep.error("FS001", "路径不存在", 路径=os.path.abspath(root))
        return _emit(rep, args)

    rep = report.Report("目录体检", target=os.path.abspath(root))
    entries, unreadable, notes, truncated = files.scan(
        root, max_files=args.max_files, follow_links=args.follow_links
    )
    summary = files.summarize(
        entries,
        root,
        unreadable=unreadable,
        truncated=truncated,
        top=args.top,
        want_duplicates=not args.no_duplicates,
    )

    rep.section("一、总体")
    rep.table(
        ["项目", "值"],
        [
            ["文件数", fmt.human_count(summary.n_files)],
            ["总大小", fmt.human_bytes(summary.total_size)],
            ["无法读取的目录", fmt.human_count(len(summary.unreadable_dirs))],
            ["是否被截断", "是（调大 --max-files）" if summary.truncated else "否"],
            ["零字节文件", fmt.human_count(len(summary.zero_byte))],
            ["小于 1 KiB 的文件", fmt.human_count(len(summary.tiny_files))],
            ["重复文件浪费空间", fmt.human_bytes(summary.duplicate_wasted)],
        ],
    )
    for note in notes:
        rep.note(note)
    if summary.unreadable_dirs:
        rep.warn("FS010", f"{len(summary.unreadable_dirs)} 个目录无法读取", 示例=summary.unreadable_dirs[:3])
    if summary.truncated:
        rep.info("FS011", "文件数达到上限，统计不完整", 建议="调大 --max-files 或缩小范围")

    rep.section("二、空间分布（按扩展名）")
    rep.table(
        ["扩展名", "文件数", "总大小", "占比"],
        [
            [suffix, fmt.human_count(count), fmt.human_bytes(size), tables.bar(size, summary.total_size, 20)]
            for suffix, count, size in summary.by_suffix
        ],
    )

    rep.section("三、最大的文件")
    rep.table(
        ["大小", "修改时间", "路径"],
        [
            [
                fmt.human_bytes(entry.size),
                _stamp(entry.mtime),
                tables.elide(_relative(entry.path, root), 84),
            ]
            for entry in summary.largest
        ],
        max_col=84,
    )

    rep.section("四、零字节 / 疑似写失败")
    if summary.zero_byte:
        rep.warn(
            "FS020",
            f"有 {len(summary.zero_byte)} 个零字节文件",
            建议="常见于磁盘写满、进程被 kill、网盘同步中断；对照修改时间定位事故窗口",
        )
        rep.table(
            ["修改时间", "路径"],
            [[_stamp(entry.mtime), tables.elide(_relative(entry.path, root), 84)] for entry in summary.zero_byte[:15]],
            max_col=84,
        )
    else:
        rep.ok("FS021", "没有零字节文件")
    if summary.tiny_files:
        rep.info("FS022", f"另有 {len(summary.tiny_files)} 个小于 1 KiB 的文件", 建议="小文件很多时打包成大文件能显著提速 I/O")

    if not args.no_duplicates:
        rep.section("五、重复内容")
        if summary.duplicates:
            rep.warn(
                "FS030",
                f"发现 {len(summary.duplicates)} 组内容相同的文件，浪费 {fmt.human_bytes(summary.duplicate_wasted)}",
                建议="确认哪一份是权威版本再删；用 (大小+内容摘要) 判定，不是靠文件名",
            )
            rows = []
            for group in summary.duplicates[: args.top]:
                rows.append(
                    [
                        fmt.human_bytes(group.size),
                        str(group.count),
                        fmt.human_bytes(group.wasted),
                        tables.elide(_relative(group.paths[0], root), 70),
                    ]
                )
            rep.table(["单个大小", "份数", "浪费", "其中一份"], rows, max_col=70)
            rep.note("每组只显示一个代表路径；完整列表用 --json 导出后查看")
        else:
            rep.ok("FS031", "没有发现内容重复的文件")

    rep.section("六、文件年龄")
    rep.table(
        ["时间段", "文件数", "总大小"],
        [[bucket, fmt.human_count(count), fmt.human_bytes(size)] for bucket, count, size in summary.age_buckets],
    )

    rep.section("七、路径问题")
    if summary.problems:
        counts = Counter(problem.kind for problem in summary.problems)
        rep.warn("FS040", f"发现 {len(summary.problems)} 处路径问题", 分类=dict(counts))
        rep.table(
            ["类型", "路径", "说明"],
            [
                [problem.kind, tables.elide(_relative(problem.path, root), 64), tables.elide(problem.detail, 48)]
                for problem in summary.problems[: args.top]
            ],
            max_col=64,
        )
        if any(problem.kind == "路径过长" for problem in summary.problems):
            rep.info(
                "FS041",
                "存在超过 259 字符的路径",
                建议="Windows 上需要开启长路径支持，或把项目移到更浅的目录（如 C:\\lab）",
            )
    else:
        rep.ok("FS042", "没有发现路径层面的问题")

    return _emit(rep, args)


def _stamp(mtime: float) -> str:
    import datetime as dt

    try:
        return dt.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
    except (OSError, ValueError, OverflowError):
        return "?"


def _relative(path: str, root: str) -> str:
    try:
        return os.path.relpath(path, root)
    except ValueError:
        return path


# ---------------------------------------------------------------- code


def cmd_code(args) -> int:
    target = args.path
    rep = report.Report("代码静态体检", target=os.path.abspath(target))
    issues, stats, checked = codescan.scan_tree(target, max_files=args.max_files, include_notebooks=not args.no_notebooks)

    rep.section("一、检查结论")
    counts = Counter(issue.level for issue in issues)
    rep.text(
        f"检查了 {len(checked)} 个文件，命中 {len(issues)} 条："
        f"严重 {counts.get('error', 0)} · 警告 {counts.get('warn', 0)} · 提示 {counts.get('info', 0)}"
    )
    _render_issues(rep, issues, limit=args.limit)

    if stats:
        rep.section("二、文件概览")
        rep.table(
            ["文件", "行数", "代码行", "函数", "类", "TODO"],
            [
                [
                    tables.elide(_relative(item.path, target if os.path.isdir(target) else os.path.dirname(target)), 52),
                    str(item.lines),
                    str(item.code_lines),
                    str(item.functions),
                    str(item.classes),
                    str(item.todos),
                ]
                for item in sorted(stats, key=lambda item: -item.lines)[: args.max_files]
            ],
            max_col=52,
        )
        total_todo = sum(item.todos for item in stats)
        if total_todo:
            rep.info("CS130", f"共 {total_todo} 处 TODO/FIXME", 建议="这些标记是「已知但没做」的清单，应急时先看它们")

    if not args.no_imports:
        rep.section("三、依赖可用性")
        imports = codescan.collect_imports(checked)
        local_dirs = [target] if os.path.isdir(target) else [os.path.dirname(target) or "."]
        missing = codescan.check_imports(imports, local_dirs=local_dirs)
        if missing:
            rep.warn(
                "CS121",
                f"有 {len(missing)} 个导入在当前环境里不可用",
                建议="断网无法安装：确认是否装在别的 conda 环境，或换成标准库实现",
            )
            _render_issues(rep, missing, limit=args.limit)
        elif imports:
            rep.ok("CS122", f"检查了 {len(imports)} 个顶层导入，都能在当前环境解析")
        else:
            rep.info(
                "CS123",
                "没有可统计的 .py 导入",
                说明="只检查了 notebook 或空目录；notebook 的导入不参与依赖解析",
            )
        if imports:
            rep.table(
                ["导入模块", "首次出现位置"],
                [[name, ", ".join(places[:3])] for name, places in sorted(imports.items())[: args.top]],
                caption=f"顶层导入清单（前 {min(len(imports), args.top)} 个）",
            )

    return _emit(rep, args)


# ---------------------------------------------------------------- triage


def cmd_triage(args) -> int:
    target = args.target
    if not os.path.exists(target):
        rep = report.Report("一键分诊", target=os.path.abspath(target))
        rep.error("TRI001", "路径不存在", 路径=os.path.abspath(target))
        return _emit(rep, args)

    rep = report.Report("一键分诊", target=os.path.abspath(target))
    if os.path.isdir(target):
        _triage_directory(rep, target, args)
    else:
        _triage_file(rep, target, args)

    _advice_section(rep, target, args)
    return _emit(rep, args)


def _looks_tabular(path: str) -> bool:
    """``.txt`` 这种后缀既可能是数据也可能是说明文档，用内容快速判断一下。

    判据：能解析出 ≥2 列，或者单列里过半的值能当数字 —— 后者覆盖
    "每行一个测量值"的纯文本数据。纯说明文档会走文本体检而不是表格体检。
    """
    try:
        table = loader.read_table(path, max_rows=200)
    except Exception:  # 读不动就按文本处理，不在这里报错
        return False
    if table.n_cols >= 2:
        return True
    values = table.column(table.columns[0]) if table.columns else []
    if not values:
        return False
    numeric = sum(1 for value in values if profile.parse_float(value) is not None)
    return numeric >= max(3, len(values) * 0.5)


def _triage_file(rep: report.Report, path: str, args) -> None:
    kind = loader.classify_file(path)
    if kind == "text_or_table" and not _looks_tabular(path):
        kind = "text"
    rep.section(f"一、文件类型判定：{kind}")
    rep.text(f"大小 {fmt.human_bytes(os.path.getsize(path))}")

    if kind == "table":
        _triage_table(rep, path, args)
    elif kind == "log":
        _triage_log(rep, path, args)
    elif kind == "code":
        _triage_code(rep, path, args)
    elif kind == "notebook":
        _triage_notebook(rep, path, args)
    elif kind == "binary":
        rep.error("TRI010", "这是二进制文件，不能按文本/表格分析", 建议="确认后缀与实际内容是否一致")
        _triage_text(rep, path, args)
    else:
        _triage_text(rep, path, args)


def _triage_table(rep: report.Report, path: str, args) -> None:
    try:
        table = loader.read_table(path, max_rows=args.max_rows)
    except (loader.UnsupportedFormat, UnicodeDecodeError) as exc:
        rep.error("TRI020", "无法按表格读取", 原因=str(exc).splitlines()[0], 建议="试 `ek log` 或 `ek encoding` 看它到底是什么")
        return

    profiles = profile.profile_table(table)
    issues = quality.check_raw_file(path) + quality.check_table(table, key=args.key, profiles=profiles)
    rep.section("数据质量")
    rep.text(f"{len(table)} 行 × {table.n_cols} 列，编码 {table.encoding}，分隔符 {table.delimiter!r}")
    _render_issues(rep, issues, limit=args.limit)

    rep.section("列画像")
    rep.table(
        ["列名", "类型", "缺失", "唯一值", "范围"],
        [
            [
                item.name,
                KIND_CN.get(item.kind, item.kind),
                f"{item.null_pct:.1f}%",
                fmt.human_count(item.unique),
                tables.elide(_range_text(item), 44),
            ]
            for item in profiles[: args.max_columns]
        ],
        max_col=44,
    )

    numeric = [item for item in profiles if item.is_numeric and item.non_null >= 8]
    if numeric:
        rep.section("数值列异常扫描（每种方法各算一遍，取票数最高的点）")
        rows = []
        for item in numeric[:4]:
            values = table.column(item.name)
            flags = {
                "z": anomaly.zscore_flags(values, threshold=3.0),
                "modz": anomaly.modified_zscore_flags(values),
                "iqr": anomaly.iqr_flags(values),
                "jump": anomaly.jump_flags(values, threshold=5.0),
            }
            merged = anomaly.consensus(flags, top=3)
            rows.append(
                [
                    item.name,
                    "，".join(f"{key}:{len(value)}" for key, value in flags.items()),
                    "，".join(f"行{entry['index'] + 1}({entry['votes']}票)" for entry in merged) or "-",
                ]
            )
        rep.table(["列名", "各方法命中", "最可疑的点"], rows)

    time_col, value_col = _pick_columns(table, profiles)
    if time_col and value_col:
        rep.section("时间轴")
        diag = timeseries.diagnose(table.column(time_col), table.column(value_col))
        rows = [
            ["时间列 / 数值列", f"{time_col} / {value_col}"],
            ["采样间隔", f"{fmt.num(diag.median_interval)} 秒" if diag.median_interval else "无法推断"],
            ["规整度", fmt.pct((diag.regular_share or 0) * 100, 100)],
            ["覆盖率", fmt.pct((diag.coverage or 0) * 100, 100)],
            ["断档", f"{len(diag.gaps)} 处"],
            ["重复时间戳", str(diag.n_duplicate_times)],
            ["平直段", f"{len(diag.flatlines)} 段"],
            ["趋势", f"{fmt.num(diag.slope_per_hour)}/小时（R²={fmt.num(diag.trend_r2)}）"],
        ]
        rep.table(["项目", "值"], rows)
        if diag.gaps:
            rep.warn("TRI030", f"存在时间断档，最大一处 {fmt.duration(diag.gaps[0].seconds)}", 建议="确认是没采集还是没存盘")
        if diag.flatlines:
            rep.warn("TRI031", f"存在 {len(diag.flatlines)} 段疑似传感器卡死", 最长=f"{diag.flatlines[0].length} 点")
        rep.text("形状（纵向按 P02~P98 裁剪，避免单个极值把图形压平）:")
        rep.text(
            tables.sparkline(
                [profile.parse_float(cell) for cell in table.column(value_col)], width=76, clip=(0.02, 0.98)
            )
        )
    elif time_col is None and value_col is not None:
        rep.info("TRI032", "没有可识别的时间列，跳过时序分析", 建议="若本应有时序，用 `ek ts` 加 --time 指定列")

    for note in table.notes:
        rep.note(note)


def _triage_log(rep: report.Report, path: str, args) -> None:
    decoded = textio.read_decoded(path)
    facts = textio.analyze_text(decoded)
    scan = textio.scan_log(decoded.text)
    rep.section("日志概览")
    rep.table(
        ["项目", "值"],
        [
            ["编码", decoded.guess.describe()],
            ["行数", fmt.human_count(facts.lines)],
            ["级别分布", ", ".join(f"{k}={v}" for k, v in sorted(scan.levels.items(), key=lambda i: -i[1])[:6])],
            ["错误签名数", str(len(scan.signatures))],
            ["时间范围", f"{scan.first_ts} ~ {scan.last_ts}" if scan.first_ts else "未识别"],
        ],
    )
    for count, sample in scan.signatures[:5]:
        rep.warn("TRI040", f"错误签名 ×{count}", 典型=tables.elide(sample, 100))
    for number, text in scan.suspicious[:5]:
        rep.info("TRI041", f"第 {number} 行命中危险关键词", 内容=tables.elide(text, 100))
    if facts.replacement_chars:
        rep.error("TRI042", "日志解码出现替换字符", 建议="用 --encoding gb18030 再试一次")
    rep.note("完整分析请用 `ek log <文件>`")


def _triage_code(rep: report.Report, path: str, args) -> None:
    issues, stats, checked = codescan.scan_tree(path)
    rep.section("代码检查")
    _render_issues(rep, issues, limit=args.limit)


def _triage_notebook(rep: report.Report, path: str, args) -> None:
    issues, info = codescan.scan_notebook(path)
    rep.section("notebook 检查")
    rep.table(
        ["项目", "值"],
        [
            ["单元格", str(info.get("cells", 0))],
            ["代码单元格", str(info.get("code_cells", 0))],
            ["从未执行", str(info.get("never_run", 0))],
            ["存着报错输出", str(info.get("error_outputs", 0))],
            ["kernel", str(info.get("kernel") or "?")],
        ],
    )
    _render_issues(rep, issues, limit=args.limit)


def _triage_text(rep: report.Report, path: str, args) -> None:
    decoded = textio.read_decoded(path)
    facts = textio.analyze_text(decoded)
    rep.section("文本体检")
    rep.table(
        ["项目", "值"],
        [
            ["编码", decoded.guess.describe()],
            ["行数 / 空行", f"{fmt.human_count(facts.lines)} / {fmt.human_count(facts.blank_lines)}"],
            ["最长行", f"{fmt.human_count(facts.max_line_len)} 字符"],
            ["行尾 CRLF/LF/CR", f"{facts.crlf} / {facts.lf_only} / {facts.cr_only}"],
            ["替换字符", fmt.human_count(facts.replacement_chars)],
        ],
    )
    for hint in facts.mojibake:
        rep.warn("TRI050", "疑似乱码", 细节=hint)


def _triage_directory(rep: report.Report, path: str, args) -> None:
    data_files: list[str] = []
    log_files: list[str] = []
    code_files: list[str] = []
    notebook_files: list[str] = []
    text_files: list[str] = []

    for directory, dirnames, filenames in os.walk(path):
        dirnames[:] = [name for name in sorted(dirnames) if name not in files.DEFAULT_IGNORE]
        for name in sorted(filenames):
            full = os.path.join(directory, name)
            suffix = os.path.splitext(name)[1].lower()
            if suffix in loader.TABLE_SUFFIXES:
                # .txt 既可能是数据也可能是说明文档，按内容分流
                if suffix in (".txt", ".dat") and not _looks_tabular(full):
                    text_files.append(full)
                else:
                    data_files.append(full)
            elif suffix in (".log", ".out", ".err", ".trace"):
                log_files.append(full)
            elif suffix == ".py":
                code_files.append(full)
            elif suffix == ".ipynb":
                notebook_files.append(full)

    rep.section("一、目录概况")
    rep.text(
        f"数据文件 {len(data_files)} · 日志 {len(log_files)} · Python 脚本 {len(code_files)} · "
        f"notebook {len(notebook_files)} · 其他文本 {len(text_files)}"
        "（已跳过 .git / __pycache__ 等噪声目录）"
    )
    rep.table(
        ["类别", "文件", "大小"],
        [
            ["数据", tables.elide(os.path.basename(item), 60), fmt.human_bytes(os.path.getsize(item))]
            for item in data_files[:8]
        ]
        + [["日志", tables.elide(os.path.basename(item), 60), fmt.human_bytes(os.path.getsize(item))] for item in log_files[:5]]
        + [["代码", tables.elide(os.path.basename(item), 60), fmt.human_bytes(os.path.getsize(item))] for item in code_files[:8]],
        max_col=60,
    )

    entries, unreadable, _notes, truncated = files.scan(path, max_files=args.max_files)
    summary = files.summarize(entries, path, unreadable=unreadable, truncated=truncated, top=5, want_duplicates=True)
    rep.info(
        "TRI100",
        f"目录共 {fmt.human_count(summary.n_files)} 个文件，合计 {fmt.human_bytes(summary.total_size)}",
        零字节文件=len(summary.zero_byte),
        重复浪费=fmt.human_bytes(summary.duplicate_wasted),
        路径问题=len(summary.problems),
    )
    if summary.zero_byte:
        rep.warn("TRI101", f"{len(summary.zero_byte)} 个零字节文件", 示例=summary.zero_byte[:3])
    if summary.duplicates:
        rep.info("TRI102", f"{len(summary.duplicates)} 组重复文件，浪费 {fmt.human_bytes(summary.duplicate_wasted)}")

    if data_files:
        # 大文件通常信息量最大，优先分析；空文件留到最后（它本身就会报错）
        data_files.sort(key=lambda item: -os.path.getsize(item))
        rep.section(f"二、数据文件逐个分诊（前 {min(len(data_files), args.files)} 个）")
        for item in data_files[: args.files]:
            rep.text(f"---- {os.path.basename(item)} ----")
            _triage_table(rep, item, args)

    if log_files:
        rep.section("三、日志")
        for item in log_files[:3]:
            rep.text(f"---- {os.path.basename(item)} ----")
            _triage_log(rep, item, args)

    if code_files or notebook_files:
        rep.section("四、代码与 notebook")
        issues, stats, _checked = codescan.scan_tree(path, max_files=args.files * 20)
        _render_issues(rep, issues, limit=args.limit)


def _advice_section(rep: report.Report, target: str, args) -> None:
    rep.section("处置建议")
    codes = {finding.code for finding in rep.findings}
    seen = set()
    count = 0
    for code, template in _ADVICE.items():
        if code not in codes or code in seen:
            continue
        seen.add(code)
        text = template.format(f=target, p="python scripts/")
        rep.text(f"* {text}")
        count += 1
        if count >= 8:
            break

    if not count:
        rep.ok("TRI200", "没有需要立刻处置的问题")
        rep.text("* 建议仍然抽查：前 5 行、最后 5 行、关键列的分位数")

    rep.text("")
    rep.text("常用后续命令：")
    rep.text(f"  python scripts/data_profile.py  {target}")
    rep.text(f"  python scripts/data_quality.py  {target}")
    rep.text(f"  python scripts/ts_diag.py       {target} --time <时间列> --value <数值列>")
    rep.text(f"  python scripts/code_scan.py     {target}")
    rep.text(f"  python scripts/log_scan.py      {target}")
    if not deps.have("matplotlib"):
        rep.note("当前环境没有 matplotlib，出图命令会退化为文本 sparkline；不装也能看形状")


# ---------------------------------------------------------------- selftest


def cmd_selftest(args) -> int:
    rep = report.Report("离线自检", target="工具自身")
    checks = selftest.run()
    rows = [["通过" if item.ok else "失败", item.name, tables.elide(item.detail, 70)] for item in checks]
    rep.section("自检结果")
    rep.table(["结果", "部件", "说明"], rows, max_col=70)

    failed = [item for item in checks if not item.ok]
    for item in failed:
        rep.error("SELFFAIL", f"{item.name} 未通过", 详情=item.detail)
    if not failed:
        rep.ok("SELFPASS", f"{len(checks)} 项自检全部通过", 说明="工具箱核心逻辑可用")
    rep.note("自检不读你的数据、不联网，只在系统临时目录做一次 CSV 往返")
    return _emit(rep, args)


# ---------------------------------------------------------------- demo


def cmd_demo(args) -> int:
    rep = report.Report("生成演练数据", target=args.out_dir)
    written = demo.generate(args.out_dir)
    rep.section("已生成")
    rep.table(
        ["文件", "大小"],
        [[tables.elide(path, 60), fmt.human_bytes(os.path.getsize(path))] for path in written],
        max_col=60,
    )
    rep.ok("DEMO000", f"共生成 {len(written)} 个文件", 目录=os.path.abspath(args.out_dir))
    rep.text("")
    rep.text("下一步（按顺序跑，观察输出是否符合预期）：")
    rep.text(f"  python scripts/triage.py       {args.out_dir}")
    rep.text(f"  python scripts/data_quality.py {os.path.join(args.out_dir, 'sensor.csv')}")
    rep.text(
        f"  python scripts/ts_diag.py      {os.path.join(args.out_dir, 'sensor.csv')} --time timestamp --value value"
    )
    rep.text(f"  python scripts/log_scan.py     {os.path.join(args.out_dir, 'app.log')}")
    rep.text(f"  python scripts/code_scan.py    {os.path.join(args.out_dir, 'bad_pipeline.py')}")
    return _emit(rep, args)


# ---------------------------------------------------------------- 参数解析


class _Parser(argparse.ArgumentParser):
    """让"参数写错"返回 3 而不是 argparse 默认的 2。

    这样退出码才能无歧义地被脚本判断：0 正常 / 1 有警告 / 2 有严重问题 /
    3 工具或用法出错。否则 ``ek quality x.csv`` 返回 2 时，你分不清是
    "数据有严重问题"还是"我把参数打错了"。
    """

    def error(self, message):  # noqa: D102 - 覆盖父类行为
        self.print_usage(sys.stderr)
        print(f"{self.prog}: 参数错误: {message}", file=sys.stderr)
        sys.exit(3)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog=PROG,
        description="EMERGENCYKITS —— 断网应急诊断工具箱（纯标准库，只读默认）",
        epilog=(
            "退出码：0 没发现问题 / 1 有警告 / 2 有严重问题 / 3 工具自身出错\n"
            "所有命令都不联网；只有显式给出 --out / --plot / --apply 时才会写文件。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"EMERGENCYKITS {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<命令>")

    # profile
    sub_p = sub.add_parser("profile", help="数据结构速览：行列、类型、缺失、分位数、分布直方图")
    sub_p.add_argument("paths", nargs="+", metavar="文件", help="一个或多个数据文件")
    sub_p.add_argument("--columns", help="只看这些列，逗号分隔")
    sub_p.add_argument("--max-rows", type=int, default=loader.DEFAULT_MAX_ROWS, help="最多读取的行数")
    sub_p.add_argument("--max-columns", type=int, default=40, help="最多展示的列数")
    sub_p.add_argument("--head", type=int, default=5, help="展示前几行（0 表示不展示）")
    sub_p.add_argument("--delimiter", help="显式指定分隔符（默认自动嗅探）")
    sub_p.add_argument("--encoding", help="显式指定编码（默认自动探测）")
    sub_p.add_argument("--hist", action="store_true", help="对第一个数值列画文本直方图")
    _add_common(sub_p)
    sub_p.set_defaults(func=cmd_profile)

    # quality
    sub_q = sub.add_parser("quality", help="数据体检：编码、行尾、缺失、重复、哨兵值、主键")
    sub_q.add_argument("path", metavar="文件")
    sub_q.add_argument("--key", help="主键列名，用于唯一性与空值检查")
    sub_q.add_argument("--missing-warn", type=float, default=0.05, help="缺失率超过多少报警告（默认 0.05）")
    sub_q.add_argument("--missing-error", type=float, default=0.5, help="缺失率超过多少算严重（默认 0.5）")
    sub_q.add_argument("--max-rows", type=int, default=loader.DEFAULT_MAX_ROWS)
    sub_q.add_argument("--max-columns", type=int, default=40)
    sub_q.add_argument("--limit", type=int, default=40, help="最多展示多少条结论")
    sub_q.add_argument("--delimiter")
    sub_q.add_argument("--encoding")
    _add_common(sub_q)
    sub_q.set_defaults(func=cmd_quality)

    # anomaly
    sub_a = sub.add_parser("anomaly", help="异常点扫描：z / 修正z / IQR / 跳变 / 滚动 / CUSUM 投票")
    sub_a.add_argument("path", metavar="文件")
    sub_a.add_argument("--column", help="要分析的数值列（默认第一个数值列）")
    sub_a.add_argument("--time", help="时间列名，用于在结果里显示时间")
    sub_a.add_argument("--methods", help="方法列表，逗号分隔：z,modz,iqr,jump,roll,cusum")
    sub_a.add_argument("--threshold", type=float, default=3.0, help="z 类方法的阈值（默认 3.0）")
    sub_a.add_argument("--iqr-k", type=float, default=1.5, help="IQR 的倍数（默认 1.5，3.0 为极端）")
    sub_a.add_argument("--window", type=int, default=20, help="滚动窗口大小")
    sub_a.add_argument("--top", type=int, default=20, help="展示最可疑的前 N 个点")
    sub_a.add_argument("--all", action="store_true", help="扫描所有数值列，只给汇总")
    sub_a.add_argument("--max-columns", type=int, default=20)
    sub_a.add_argument("--max-rows", type=int, default=loader.DEFAULT_MAX_ROWS)
    sub_a.add_argument("--plot", metavar="PNG", help="同时输出折线图（需要 matplotlib）")
    sub_a.add_argument("--delimiter")
    sub_a.add_argument("--encoding")
    _add_common(sub_a)
    sub_a.set_defaults(func=cmd_anomaly)

    # ts
    sub_t = sub.add_parser("ts", help="时间序列诊断：采样间隔、断档、重复时间、卡死、漂移、周期")
    sub_t.add_argument("path", metavar="文件")
    sub_t.add_argument("--time", help="时间列名（默认自动识别）")
    sub_t.add_argument("--value", help="数值列名（默认第一个数值列）")
    sub_t.add_argument("--gap-factor", type=float, default=1.5, help="间隔超过中位数的几倍算断档")
    sub_t.add_argument("--flat-min", type=int, default=5, help="连续多少个相同值算卡死")
    sub_t.add_argument("--regularity-tolerance", type=float, default=0.1, help="判定「规整」的偏差比例")
    sub_t.add_argument("--max-rows", type=int, default=loader.DEFAULT_MAX_ROWS)
    sub_t.add_argument("--plot", metavar="PNG", help="同时输出折线图（需要 matplotlib）")
    sub_t.add_argument("--delimiter")
    sub_t.add_argument("--encoding")
    _add_common(sub_t)
    sub_t.set_defaults(func=cmd_ts)

    # plot
    sub_pl = sub.add_parser("plot", help="出图：折线 / 直方图 / 缺失率（需要 matplotlib）")
    sub_pl.add_argument("path", metavar="文件")
    sub_pl.add_argument("--kind", choices=("series", "hist", "missing"), default="series")
    sub_pl.add_argument("--column", help="数值列名")
    sub_pl.add_argument("--time", help="时间列名")
    sub_pl.add_argument("--bins", type=int, default=40)
    sub_pl.add_argument("--plot-out", metavar="PNG", default="quick_plot.png", help="输出图片路径")
    sub_pl.add_argument("--max-rows", type=int, default=loader.DEFAULT_MAX_ROWS)
    sub_pl.add_argument("--delimiter")
    sub_pl.add_argument("--encoding")
    _add_common(sub_pl)
    sub_pl.set_defaults(func=cmd_plot)

    # log
    sub_l = sub.add_parser("log", help="日志诊断：级别分布、错误签名聚类、异常类型、危险关键词")
    sub_l.add_argument("path", metavar="文件")
    sub_l.add_argument("--top", type=int, default=12, help="每张表展示多少条")
    sub_l.add_argument("--grep", help="额外用正则过滤并列出匹配行")
    sub_l.add_argument("--encoding")
    _add_common(sub_l)
    sub_l.set_defaults(func=cmd_log)

    # encoding
    sub_e = sub.add_parser("encoding", help="编码与文本体检；可转码（需显式 --out/--inplace）")
    sub_e.add_argument("path", metavar="文件")
    sub_e.add_argument("--encoding", help="跳过探测，直接按这个编码读")
    sub_e.add_argument("--hex", action="store_true", help="打印文件头 hex dump")
    sub_e.add_argument("--convert", metavar="目标编码", help="转码到指定编码，如 utf-8 / gb18030")
    sub_e.add_argument("--to", metavar="输出文件", help="转码后的新文件（推荐，不改动原文件）")
    sub_e.add_argument("--inplace", action="store_true", help="原地覆盖（会先写 .bak 备份）")
    _add_common(sub_e)
    sub_e.set_defaults(func=cmd_encoding)

    # env
    sub_env = sub.add_parser("env", help="环境快照与问题判断：解释器、PATH、编码、磁盘、代理")
    sub_env.add_argument("--root", help="以哪个目录为工作目录（默认当前目录）")
    sub_env.add_argument("--no-git", action="store_true", help="跳过 git 状态检查")
    _add_common(sub_env)
    sub_env.set_defaults(func=cmd_env)

    # fs
    sub_f = sub.add_parser("fs", help="目录体检：空间分布、大文件、零字节、重复内容、路径问题")
    sub_f.add_argument("path", metavar="目录")
    sub_f.add_argument("--max-files", type=int, default=200_000)
    sub_f.add_argument("--top", type=int, default=10, help="每张表展示多少条")
    sub_f.add_argument("--no-duplicates", action="store_true", help="跳过重复内容检测（更快）")
    sub_f.add_argument("--follow-links", action="store_true", help="跟随软链接")
    _add_common(sub_f)
    sub_f.set_defaults(func=cmd_fs)

    # code
    sub_c = sub.add_parser("code", help="代码静态体检：语法、反模式、已移除 API、依赖可用性、notebook")
    sub_c.add_argument("path", metavar="路径", help=".py 文件、.ipynb 文件或目录")
    sub_c.add_argument("--max-files", type=int, default=3000)
    sub_c.add_argument("--limit", type=int, default=60, help="最多展示多少条结论")
    sub_c.add_argument("--top", type=int, default=15, help="导入清单展示多少条")
    sub_c.add_argument("--no-imports", action="store_true", help="跳过导入可用性检查")
    sub_c.add_argument("--no-notebooks", action="store_true", help="跳过 .ipynb")
    _add_common(sub_c)
    sub_c.set_defaults(func=cmd_code)

    # triage
    sub_tr = sub.add_parser("triage", help="一键分诊：自动识别类型，跑完相关检查并给处置建议")
    sub_tr.add_argument("target", metavar="路径", help="文件或目录")
    sub_tr.add_argument("--files", type=int, default=3, help="目录模式下逐个分析的样本文件数")
    sub_tr.add_argument("--key", help="主键列名（仅对数据文件生效）")
    sub_tr.add_argument("--max-rows", type=int, default=loader.DEFAULT_MAX_ROWS)
    sub_tr.add_argument("--max-columns", type=int, default=25)
    sub_tr.add_argument("--max-files", type=int, default=200_000)
    sub_tr.add_argument("--limit", type=int, default=25, help="每类最多展示多少条结论")
    _add_common(sub_tr)
    sub_tr.set_defaults(func=cmd_triage)

    # selftest
    sub_s = sub.add_parser("selftest", help="离线自检：验证工具箱自身各部件可用")
    _add_common(sub_s)
    sub_s.set_defaults(func=cmd_selftest)

    # demo
    sub_d = sub.add_parser("demo", help="生成一批「带病」的演练数据")
    sub_d.add_argument("--out-dir", default="samples", help="输出目录（默认 ./samples）")
    _add_common(sub_d)
    sub_d.set_defaults(func=cmd_demo)

    return parser


def main(argv: list[str] | None = None) -> int:
    _harden_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0

    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130
    except (loader.UnsupportedFormat, FileNotFoundError, IsADirectoryError) as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 3
    except BrokenPipeError:
        # 输出被 head/less 之类截断是很正常的用法，不算错误
        return 0
    except Exception as exc:  # noqa: BLE001 - 顶层兜底，避免把堆栈糊在用户脸上
        import traceback

        print(f"[工具内部错误] {exc.__class__.__name__}: {exc}", file=sys.stderr)
        if os.environ.get("EK_DEBUG"):
            traceback.print_exc()
        else:
            print("（设置环境变量 EK_DEBUG=1 可打印完整调用栈，便于反馈问题）", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
