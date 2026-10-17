"""统一的诊断报告容器与渲染器。

所有命令都产出同一种 ``Report``，于是：

* 人类可读的终端输出（默认）
* 机器可解析的 JSON（``--json``）
* 可贴进笔记/投稿附录的 Markdown（``--out report.md``）

三者内容同源，不会出现"屏幕上说 3 个问题、导出文件里说 2 个"的情况。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from . import __version__
from . import fmt, tables

__all__ = ["Finding", "Report", "LEVEL_ORDER", "MARK", "worst_level", "exit_code_for"]

#: 由轻到重
LEVEL_ORDER = ("ok", "info", "warn", "error")
_RANK = {level: index for index, level in enumerate(LEVEL_ORDER)}

#: 纯 ASCII 标记：cp936 控制台、重定向到文件都不会崩
MARK = {"ok": "[ok]", "info": "[--]", "warn": "[!!]", "error": "[XX]"}

_LEVEL_CN = {"ok": "正常", "info": "提示", "warn": "警告", "error": "严重"}


def worst_level(levels) -> str:
    worst = "ok"
    for level in levels:
        if _RANK.get(level, 0) > _RANK[worst]:
            worst = level
    return worst


def exit_code_for(worst: str) -> int:
    """0 = 没发现问题；1 = 有警告；2 = 有严重问题。可安全用于 shell 串联。"""
    if worst == "error":
        return 2
    if worst == "warn":
        return 1
    return 0


@dataclass
class Finding:
    level: str
    code: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)
    where: str | None = None

    def as_dict(self) -> dict:
        payload = {
            "level": self.level,
            "code": self.code,
            "message": self.message,
        }
        if self.where:
            payload["where"] = self.where
        if self.detail:
            payload["detail"] = self.detail
        return payload


class Report:
    """一份诊断报告。典型用法::

        rep = Report("数据体检", target=path)
        rep.section("一、结构")
        rep.table(["列", "类型"], [["ts", "datetime"]])
        rep.warn("DQ004", "存在重复行", rows=12)
        rep.dump("out.md")
    """

    def __init__(self, title: str, *, target: str | None = None, tool: str | None = None):
        self.title = title
        self.target = target
        self.tool = tool or "ek"
        self.created = datetime.now()
        self._blocks: list[tuple] = []
        self.findings: list[Finding] = []
        self.notes: list[str] = []
        self._started = time.perf_counter()

    # ------------------------------------------------------------------ 写入

    def section(self, title: str) -> None:
        self._blocks.append(("section", title))

    def add(
        self,
        level: str,
        code: str,
        message: str,
        *,
        where: str | None = None,
        **detail: Any,
    ) -> Finding:
        if level not in _RANK:
            raise ValueError(f"未知级别: {level!r}")
        finding = Finding(level=level, code=code, message=message, detail=detail, where=where)
        self.findings.append(finding)
        self._blocks.append(("finding", finding))
        return finding

    def ok(self, code: str, message: str, **detail: Any) -> Finding:
        return self.add("ok", code, message, **detail)

    def info(self, code: str, message: str, **detail: Any) -> Finding:
        return self.add("info", code, message, **detail)

    def warn(self, code: str, message: str, **detail: Any) -> Finding:
        return self.add("warn", code, message, **detail)

    def error(self, code: str, message: str, **detail: Any) -> Finding:
        return self.add("error", code, message, **detail)

    def table(self, headers, rows, *, aligns=None, caption: str | None = None, max_col: int = 48) -> None:
        self._blocks.append(("table", list(headers), [list(r) for r in rows], aligns, caption, max_col))

    def text(self, line: str = "") -> None:
        self._blocks.append(("text", line))

    def note(self, line: str) -> None:
        """附注：不参与计数，渲染在报告尾部。"""
        self.notes.append(line)

    # ------------------------------------------------------------------ 统计

    def counts(self) -> dict[str, int]:
        result = {level: 0 for level in LEVEL_ORDER}
        for finding in self.findings:
            result[finding.level] = result.get(finding.level, 0) + 1
        return result

    def worst(self) -> str:
        return worst_level(f.level for f in self.findings)

    def exit_code(self) -> int:
        return exit_code_for(self.worst())

    def elapsed(self) -> float:
        return time.perf_counter() - self._started

    # ------------------------------------------------------------------ 渲染

    def _header_lines(self) -> list[str]:
        stamp = self.created.strftime("%Y-%m-%d %H:%M:%S")
        host = os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or "?"
        line = f"{self.tool} {__version__} · {self.title}"
        lines = ["=" * 74, line]
        if self.target:
            lines.append(f"目标: {self.target}")
        lines.append(f"时间: {stamp} · 主机: {host} · 离线运行，未访问网络")
        lines.append("=" * 74)
        return lines

    @staticmethod
    def _detail_text(finding: Finding) -> str:
        if not finding.detail:
            return ""
        parts = []
        for key, value in finding.detail.items():
            if isinstance(value, (list, tuple)):
                shown = ", ".join(str(item) for item in list(value)[:8])
                if len(value) > 8:
                    shown += f" …共 {len(value)} 项"
                parts.append(f"{key}={shown}")
            elif isinstance(value, dict):
                shown = ", ".join(f"{k}:{v}" for k, v in list(value.items())[:6])
                parts.append(f"{key}={{{shown}}}")
            else:
                parts.append(f"{key}={value}")
        return "  ".join(parts)

    def render(self) -> str:
        lines = self._header_lines()
        for block in self._blocks:
            kind = block[0]
            if kind == "section":
                lines.append("")
                lines.append("== " + block[1] + " " + "=" * max(2, 68 - tables.display_width(block[1])))
            elif kind == "finding":
                finding: Finding = block[1]
                head = f"{MARK[finding.level]} {finding.code}  {finding.message}"
                if finding.where:
                    head += f"  @ {finding.where}"
                lines.append(head)
                detail = self._detail_text(finding)
                if detail:
                    lines.append("         → " + detail)
            elif kind == "table":
                _, headers, rows, aligns, caption, max_col = block
                if caption:
                    lines.append("")
                    lines.append("-- " + caption)
                lines.append(tables.render(headers, rows, aligns=aligns, max_col=max_col))
                lines.append("")
            elif kind == "text":
                lines.append(block[1])

        counts = self.counts()
        lines.append("")
        lines.append("-" * 74)
        lines.append(
            "汇总: "
            + " · ".join(
                f"{_LEVEL_CN[level]} {counts[level]}" for level in ("error", "warn", "info", "ok")
            )
            + f" · 用时 {fmt.duration(self.elapsed())}"
        )
        if self.notes:
            lines.append("附注:")
            for note in self.notes:
                lines.append("  - " + note)
        return "\n".join(lines)

    @staticmethod
    def _md_cell(value) -> str:
        text = "" if value is None else str(value)
        text = text.replace("|", "\\|").replace("\n", " ")
        return text

    def render_markdown(self) -> str:
        lines = [f"# {self.title}", ""]
        if self.target:
            lines.append(f"- **目标**: `{self.target}`")
        lines.append(f"- **时间**: {self.created.strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"- **工具**: {self.tool} {__version__}（离线运行）")
        lines.append("")
        for block in self._blocks:
            kind = block[0]
            if kind == "section":
                lines.append(f"## {block[1]}")
                lines.append("")
            elif kind == "finding":
                finding: Finding = block[1]
                line = f"- **`{MARK[finding.level]}` {finding.code}** {finding.message}"
                if finding.where:
                    line += f" — `{finding.where}`"
                detail = self._detail_text(finding)
                if detail:
                    line += f"  \n  _{detail}_"
                lines.append(line)
            elif kind == "table":
                _, headers, rows, aligns, caption, max_col = block
                if caption:
                    lines.extend(["", f"*{caption}*", ""])
                lines.append("| " + " | ".join(self._md_cell(h) for h in headers) + " |")
                align_row = []
                for index, head in enumerate(headers):
                    align = (aligns[index] if aligns and index < len(aligns) else "<")
                    align_row.append("---:" if align == ">" else ":---")
                lines.append("| " + " | ".join(align_row) + " |")
                for row in rows:
                    cells = [self._md_cell(cell) for cell in row]
                    cells += [""] * (len(headers) - len(cells))
                    lines.append("| " + " | ".join(cells[: len(headers)]) + " |")
                lines.append("")
            elif kind == "text":
                lines.append(block[1])
        counts = self.counts()
        lines.append("")
        lines.append("---")
        lines.append(
            "**汇总**: "
            + " · ".join(f"{_LEVEL_CN[level]} {counts[level]}" for level in ("error", "warn", "info", "ok"))
        )
        if self.notes:
            lines.append("")
            lines.append("**附注**:")
            for note in self.notes:
                lines.append(f"- {note}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        counts = self.counts()
        blocks: list[dict] = []
        for block in self._blocks:
            kind = block[0]
            if kind == "section":
                blocks.append({"type": "section", "title": block[1]})
            elif kind == "finding":
                blocks.append({"type": "finding", **block[1].as_dict()})
            elif kind == "table":
                _, headers, rows, aligns, caption, _max = block
                blocks.append(
                    {
                        "type": "table",
                        "caption": caption,
                        "headers": headers,
                        "rows": [[None if c is None else str(c) for c in row] for row in rows],
                    }
                )
            elif kind == "text":
                blocks.append({"type": "text", "text": block[1]})
        return {
            "tool": self.tool,
            "version": __version__,
            "title": self.title,
            "target": self.target,
            "generated_at": self.created.isoformat(timespec="seconds"),
            "offline": True,
            "counts": counts,
            "worst": self.worst(),
            "findings": [finding.as_dict() for finding in self.findings],
            "notes": self.notes,
            "blocks": blocks,
        }

    # ------------------------------------------------------------------ 导出

    def dump(self, path: str | os.PathLike) -> str:
        """按后缀自动选择格式：``.json`` / ``.md`` / 其他（纯文本）。"""
        target = os.fspath(path)
        parent = os.path.dirname(os.path.abspath(target))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        suffix = os.path.splitext(target)[1].lower()
        if suffix == ".json":
            text = json.dumps(self.to_dict(), ensure_ascii=False, indent=2, default=str)
            encoding = "utf-8"
        elif suffix in (".md", ".markdown"):
            text = self.render_markdown()
            encoding = "utf-8"
        else:
            text = self.render()
            encoding = "utf-8"
        with open(target, "w", encoding=encoding, newline="\n") as handle:
            handle.write(text)
            if not text.endswith("\n"):
                handle.write("\n")
        return target
