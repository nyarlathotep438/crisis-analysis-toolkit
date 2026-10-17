"""数据质量规则检查。

每条规则都有固定的编号（``DQ101`` 之类），这样你在笔记里写"上次是 DQ104
常量列拖垮了模型"时，下次可以直接按编号搜。规则只做**报告**，不改数据 ——
急救现场最怕的就是工具"顺手帮你修好了"，把现场破坏了。

严重程度分三档：

* ``error`` 数据已经不可用（空表、主键重复、列数错乱）
* ``warn``  能跑但结论不可信（缺失率过高、脏值、时间不单调）
* ``info``  值得知道但不必改（高基数列、首尾空白）
"""

from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass

from . import profile as profile_mod
from . import textio

__all__ = ["Issue", "check_table", "check_raw_file", "SENTINELS"]

#: 常见"缺测标记"数值：传感器/仪器的占位符
SENTINELS = (-999.0, -9999.0, -99999.0, -999999.0, 999.0, 9999.0, 99999.0, 999999.0, 1e20, -1e20)

_LEVEL_ORDER = {"info": 0, "warn": 1, "error": 2}


@dataclass
class Issue:
    level: str
    code: str
    title: str
    detail: str = ""
    hint: str = ""

    def sort_key(self) -> tuple[int, str]:
        return (-_LEVEL_ORDER.get(self.level, 0), self.code)


def _issue(level: str, code: str, title: str, detail: str = "", hint: str = "") -> Issue:
    return Issue(level=level, code=code, title=title, detail=detail, hint=hint)


# ---------------------------------------------------------------- 原始文件


def check_raw_file(path: str | os.PathLike, *, encoding: str | None = None) -> list[Issue]:
    """字节层面的体检：BOM、行尾、NUL、替换字符、超长行。"""
    issues: list[Issue] = []
    target = os.fspath(path)
    size = os.path.getsize(target)

    if size == 0:
        return [_issue("error", "DQ001", "空文件", f"{target} 大小为 0 字节", "确认文件是否真的下载/生成成功")]
    if size < 16:
        issues.append(_issue("warn", "DQ002", "文件过小", f"只有 {size} 字节", "可能只写入了表头就被中断"))

    try:
        decoded = textio.read_decoded(target, encoding=encoding)
    except OSError as exc:
        return [_issue("error", "DQ003", "无法读取", str(exc), "检查文件是否被其他进程独占（Windows 常见）")]

    facts = textio.analyze_text(decoded)
    guess = decoded.guess

    if guess.is_bom:
        issues.append(
            _issue(
                "warn",
                "DQ010",
                "文件带 BOM",
                f"编码判定为 {guess.encoding}（{guess.method}）",
                "用 pandas 读时建议 encoding='utf-8-sig'；某些 C 程序会把 BOM 当成第一列内容",
            )
        )

    if facts.has_null_bytes or facts.looks_binary:
        issues.append(
            _issue(
                "error",
                "DQ011",
                "疑似二进制文件",
                f"含 {facts.control_chars} 个控制字符，NUL 字节={facts.has_null_bytes}",
                "确认路径是不是指向了 .gz/.parquet/Excel 等二进制文件；压缩文件请先解压",
            )
        )

    if facts.line_ending_mix:
        issues.append(
            _issue(
                "warn",
                "DQ012",
                "行尾符混用",
                f"CRLF={facts.crlf} / LF={facts.lf_only} / CR={facts.cr_only}",
                "混用常见于「Windows 生成 + Linux 追加」；按行切分或 diff 时会出现幽灵差异",
            )
        )

    if facts.lines > 1 and not facts.ends_with_newline:
        issues.append(
            _issue(
                "info",
                "DQ013",
                "文件末尾没有换行符",
                "最后一行可能被追加写入截断",
                "逐行处理时最后一行容易被忽略（old-style 迭代器不会）",
            )
        )

    if facts.replacement_chars:
        issues.append(
            _issue(
                "error",
                "DQ014",
                "解码出现替换字符",
                f"共 {facts.replacement_chars} 个 U+FFFD；推断编码 {guess.encoding}（把握 {guess.confidence}）",
                "用 --encoding 指定真实编码（中文 Windows 的 GBK 文件常被误判）",
            )
        )

    for hint_text in facts.mojibake:
        issues.append(_issue("warn", "DQ015", "疑似乱码", hint_text, "先确认原始编码，再决定是否转码；不要在乱码上继续分析"))

    if facts.blank_lines and facts.lines and facts.blank_lines / facts.lines > 0.3:
        issues.append(
            _issue(
                "warn",
                "DQ016",
                "空行比例过高",
                f"{facts.blank_lines}/{facts.lines} 行为空（{facts.blank_lines / facts.lines * 100:.0f}%）",
                "可能是采集端写出了大量空记录",
            )
        )

    if facts.max_line_len > 5000:
        issues.append(
            _issue(
                "info",
                "DQ017",
                "存在超长行",
                f"第 {facts.max_line_no} 行长 {facts.max_line_len} 字符",
                "检查是否整份数据被写进了一行（缺少换行符），或某个字段内嵌了巨大 JSON",
            )
        )

    if guess.confidence == "low" and facts.non_ascii_chars:
        issues.append(
            _issue(
                "warn",
                "DQ018",
                "编码判断没把握",
                f"推断 {guess.encoding}；" + "；".join(guess.notes[:3]),
                "抽查前几行中文是否正常，必要时用 --encoding gb18030 / utf-8 显式指定",
            )
        )

    return issues


# ---------------------------------------------------------------- 表格内容


def check_table(
    table,
    *,
    key: str | None = None,
    missing_warn: float = 0.05,
    missing_error: float = 0.5,
    profiles=None,
) -> list[Issue]:
    """表格结构与内容检查。``key`` 指定主键列时额外做唯一性检查。"""
    issues: list[Issue] = []
    rows = table.rows
    columns = table.columns
    total = len(rows)

    if not rows:
        issues.append(
            _issue(
                "error",
                "DQ101",
                "表里没有数据行",
                f"列有 {len(columns)} 个，但 0 行",
                "确认分隔符是否猜对（--delimiter 可显式指定）；也可能是文件只写了表头",
            )
        )
    if table.mismatched_rows:
        issues.append(
            _issue(
                "error",
                "DQ102",
                "字段数与表头不一致",
                f"{table.mismatched_rows} 行的列数不等于 {len(columns)}",
                "常见原因：字段内含未转义的分隔符，或引号缺失；检查对应的行",
            )
        )
    if table.truncated_rows:
        issues.append(
            _issue(
                "info",
                "DQ103",
                "只分析了部分行",
                f"受 --max-rows 限制，另有 {table.truncated_rows} 行未读取",
                "结论只对已读取部分成立；需要全量时调大 --max-rows",
            )
        )

    duplicated_names = list(getattr(table, "deduped_columns", []) or [])
    if duplicated_names:
        issues.append(
            _issue(
                "warn",
                "DQ104",
                "存在重复列名",
                f"已自动加后缀：{duplicated_names[:6]}",
                "重复列名会让 df['x'] 返回 DataFrame 而不是 Series，是最隐蔽的一类 bug",
            )
        )

    badly_named = [
        name for name in columns if name != name.strip() or "  " in name or name == "" or name.lower() in ("unnamed: 0", "index")
    ]
    if badly_named:
        issues.append(
            _issue(
                "info",
                "DQ105",
                "列名不规范",
                f"{badly_named[:6]}",
                "首尾空格/空列名/Unnamed 列会造成按名取值失败；建议统一重命名",
            )
        )

    if profiles is None:
        profiles = profile_mod.profile_table(table)

    numeric_columns: dict[str, list[float]] = {}
    for prof in profiles:
        index = table.index_of(prof.name)
        if prof.kind == "empty":
            issues.append(_issue("error", "DQ110", f"整列为空：{prof.name}", "该列没有任何有效值", "确认这一列是否本来就不该存在"))
            continue

        if prof.unique == 1 and prof.non_null > 1:
            issues.append(
                _issue(
                    "warn",
                    "DQ111",
                    f"常量列：{prof.name}",
                    f"{prof.non_null} 个值全是 {prof.examples[0] if prof.examples else '?'}",
                    "对建模/相关性毫无信息量；若本应随时间变化，说明采集卡住了",
                )
            )

        if prof.null_pct >= missing_error * 100:
            issues.append(
                _issue(
                    "error",
                    "DQ112",
                    f"缺失率过高：{prof.name}",
                    f"{prof.nulls}/{prof.total} 行缺失（{prof.null_pct:.1f}%）",
                    "这一列基本不可用；检查采集逻辑或先确认缺失的生成机制（MCAR/MAR/MNAR）",
                )
            )
        elif prof.null_pct >= missing_warn * 100:
            issues.append(
                _issue(
                    "warn",
                    "DQ113",
                    f"缺失率偏高：{prof.name}",
                    f"{prof.nulls}/{prof.total} 行缺失（{prof.null_pct:.1f}%）",
                    "确认缺失是否有系统性（集中在某段时间/某个分组），不要直接均值填充",
                )
            )

        if prof.suspect_nulls:
            issues.append(
                _issue(
                    "info",
                    "DQ114",
                    f"疑似缺失标记：{prof.name}",
                    f"{prof.suspect_nulls} 个值形如 - ? unknown 等",
                    "如果它们是缺测标记，会在求均值时被当成真实数值（如 -999），务必先确认",
                )
            )

        if prof.dirty_count:
            issues.append(
                _issue(
                    "warn",
                    "DQ115",
                    f"数值列含脏值：{prof.name}",
                    f"{prof.dirty_count} 个值无法解析为数字，例如 {prof.dirty}",
                    "按当前实现这些值已被排除出统计；需要时可用 --column 单独查看",
                )
            )

        if prof.is_numeric and prof.non_null:
            values: list[float] = []
            for row in rows:
                cell = row[index] if index < len(row) else None
                number = profile_mod.parse_float(cell)
                if number is not None and number not in (float("inf"), float("-inf")):
                    values.append(number)
            numeric_columns[prof.name] = values
            if values:
                ordered = sorted(values)
                q1 = profile_mod.quantile(ordered, 0.25) or 0.0
                q3 = profile_mod.quantile(ordered, 0.75) or 0.0
                iqr = q3 - q1
                if iqr > 0:
                    low = q1 - 3.0 * iqr
                    high = q3 + 3.0 * iqr
                    extreme = sum(1 for value in values if value < low or value > high)
                    if extreme:
                        share = extreme / len(values)
                        issues.append(
                            _issue(
                                "warn" if share > 0.05 else "info",
                                "DQ116",
                                f"极端离群值：{prof.name}",
                                f"{extreme} 个值超出 [Q1-3IQR, Q3+3IQR]，占 {share * 100:.1f}%",
                                "先判断是真实极值还是录入错误；用 anomaly 子命令可看具体位置",
                            )
                        )
                hits = [value for value in values if value in SENTINELS]
                if hits:
                    issues.append(
                        _issue(
                            "warn",
                            "DQ117",
                            f"疑似哨兵值：{prof.name}",
                            f"{len(hits)} 个值属于 {sorted(set(hits))[:5]}",
                            "传感器常用 -999/9999 表示缺测；混入计算会严重偏移均值",
                        )
                    )

                if prof.p05 is not None and prof.p95 is not None and prof.p95 - prof.p05 == 0 and prof.minimum != prof.maximum:
                    issues.append(
                        _issue(
                            "info",
                            "DQ118",
                            f"分布高度集中：{prof.name}",
                            "5%~95% 分位区间宽度为 0，但极值不同",
                            "极少数尖峰主导了取值范围；直接看 min/max 会被误导",
                        )
                    )

        if prof.kind == "string":
            blank_edges = 0
            control = 0
            samples: list[str] = []
            for row in rows:
                cell = row[index] if index < len(row) else None
                if cell is None:
                    continue
                text = str(cell)
                if text != text.strip():
                    blank_edges += 1
                    if len(samples) < 3:
                        samples.append(repr(text[:30]))
                if any(ord(char) < 32 and char not in "\t" for char in text):
                    control += 1
            if blank_edges:
                issues.append(
                    _issue(
                        "warn",
                        "DQ120",
                        f"字符串首尾有空白：{prof.name}",
                        f"{blank_edges} 个值，例如 {samples}",
                        "分组统计时 'A' 与 'A ' 会被当成两类；用 .str.strip() 归一化",
                    )
                )
            if control:
                issues.append(
                    _issue(
                        "warn",
                        "DQ121",
                        f"字符串含控制字符：{prof.name}",
                        f"{control} 个值含不可见控制字符",
                        "常见于从网页/PDF 复制的数据；会让相等比较莫名其妙地失败",
                    )
                )

        if prof.is_temporal:
            moments = []
            parse_fail = 0
            for row in rows:
                cell = row[index] if index < len(row) else None
                moment = profile_mod.parse_datetime(cell)
                if moment is None:
                    if not profile_mod.is_missing(cell):
                        parse_fail += 1
                    continue
                moments.append(moment)
            if moments:
                duplicates = len(moments) - len(set(moments))
                if duplicates:
                    issues.append(
                        _issue(
                            "warn",
                            "DQ130",
                            f"时间戳重复：{prof.name}",
                            f"{duplicates} 个重复时间点",
                            "重复时间会在重采样/滚动窗口里被错误加权；确认是否重复采集或合并表时产生",
                        )
                    )
                out_of_order = sum(1 for prev, curr in zip(moments, moments[1:]) if curr < prev)
                if out_of_order:
                    issues.append(
                        _issue(
                            "warn",
                            "DQ131",
                            f"时间不单调：{prof.name}",
                            f"{out_of_order} 处时间倒序",
                            "画图和做时序建模前应先按时间排序，否则会出现来回折线",
                        )
                    )
                if parse_fail:
                    issues.append(
                        _issue(
                            "info",
                            "DQ132",
                            f"时间列有无法解析的值：{prof.name}",
                            f"{parse_fail} 个",
                            "确认这些行的原始内容（可能是不同的时间格式混在一列）",
                        )
                    )

        if prof.is_id_like and prof.non_null > 100 and prof.kind == "string":
            issues.append(
                _issue(
                    "info",
                    "DQ140",
                    f"高基数列：{prof.name}",
                    f"{prof.unique} 个唯一值 / {prof.non_null} 行",
                    "如果是 ID 就不该参与统计；如果是本应重复的类别，说明编码方案不规范",
                )
            )

    # ---------------- 行级检查
    if rows:
        seen: Counter[tuple] = Counter()
        for row in rows:
            seen[tuple("" if cell is None else str(cell) for cell in row)] += 1
        duplicate_groups = sum(1 for count in seen.values() if count > 1)
        duplicate_rows = sum(count - 1 for count in seen.values() if count > 1)
        if duplicate_rows:
            examples = []
            for row_key, count in seen.items():
                if count > 1 and len(examples) < 3:
                    examples.append(row_key[:4])
            issues.append(
                _issue(
                    "warn",
                    "DQ150",
                    "存在完全重复的行",
                    f"{duplicate_rows} 行是重复的（涉及 {duplicate_groups} 组），约占 {duplicate_rows / total * 100:.1f}%；示例 {examples}",
                    "重复行会让统计量偏向被复制的那些点；用 .drop_duplicates() 前先确认它们是否真的重复（如多次实验的合法重复）",
                )
            )

    # ---------------- 主键检查
    if key:
        try:
            values = table.column(key)
        except KeyError as exc:
            issues.append(_issue("error", "DQ160", "主键列不存在", str(exc), "用 profile 子命令先看列名"))
        else:
            counter = Counter("" if value is None else str(value) for value in values)
            duplicated = {value: count for value, count in counter.items() if count > 1}
            if duplicated:
                top = sorted(duplicated.items(), key=lambda item: -item[1])[:5]
                issues.append(
                    _issue(
                        "error",
                        "DQ161",
                        f"主键不唯一：{key}",
                        f"{len(duplicated)} 个键值重复，最频繁的：{top}",
                        "主键重复会让 merge 产生笛卡尔积，行数凭空翻倍 —— 这是最常见的「数据变多了」事故",
                    )
                )
            else:
                issues.append(_issue("ok", "DQ162", f"主键唯一：{key}", f"{len(values)} 个值互不相同"))
            if any((value is None or str(value).strip() == "") for value in values):
                issues.append(_issue("error", "DQ163", f"主键存在空值：{key}", "主键列不允许缺失", "定位空主键所在的行"))

    issues.sort(key=lambda issue: issue.sort_key())
    return issues
