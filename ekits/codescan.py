"""代码静态体检（AST + 文本 + 依赖可用性）。

不装 pylint、不联网、不执行被检查的代码。检查的是急救时真正会咬人的那些
模式，每条都写清"为什么危险"，而不是甩一句 "W0612 unused variable"。

四类检查：

1. **能不能跑**：``compile()`` 是否通过（语法错误）；导入的模块在当前环境
   是否存在（断网后最实际的问题：换了机器、包没装、名子拼错）。
2. **会不会静默出错**：裸 ``except``、吞异常的 ``except Exception: pass``、
   用 ``assert`` 做输入校验、``open()`` 不指定 ``encoding``。
3. **换环境就坏**：硬编码绝对路径、网络调用、已从 numpy/pandas 移除的 API。
4. **notebook 特有**：单元格执行顺序乱、存着报错输出、kernel 版本不一致。

误报豁免（也适用于你自己的代码）：

* 行级：在该行加 ``# noqa`` 或 ``# ek:ignore``，这一行的文本类检查全部跳过；
* 文件级：在文件前 40 行内写 ``# ek:ignore-file CS112,CS108``，这些编号在
  本文件内不再报告。本文件自己就用了 ``# ek:ignore-file CS112``，
  因为它需要把"已移除 API"的模式写成字符串，否则会自己举报自己。
"""

# ek:ignore-file CS112

from __future__ import annotations

import ast
import io
import json
import os
import re
import tokenize
from dataclasses import dataclass, field

from . import files as files_mod

__all__ = [
    "CodeIssue",
    "FileStats",
    "scan_python_file",
    "scan_python_source",
    "scan_notebook",
    "scan_tree",
    "collect_imports",
    "check_imports",
]

BUILTIN_NAMES = frozenset(
    {
        "list",
        "dict",
        "set",
        "str",
        "int",
        "float",
        "bool",
        "tuple",
        "bytes",
        "type",
        "id",
        "input",
        "open",
        "filter",
        "map",
        "sum",
        "min",
        "max",
        "abs",
        "all",
        "any",
        "len",
        "next",
        "object",
        "range",
        "format",
        "hash",
        "dir",
        "vars",
        "slice",
        "print",
    }
)

#: 网络相关模块：断网环境下会直接失败，静态标出来省得运行时才发现
NETWORK_MODULES = frozenset(
    {
        "socket",
        "ssl",
        "urllib",
        "http",
        "requests",
        "httpx",
        "aiohttp",
        "ftplib",
        "smtplib",
        "telnetlib",
        "paramiko",
        "websocket",
        "websockets",
        "grpc",
        "boto3",
        "googleapiclient",
        "huggingface_hub",
        "wandb",
    }
)

_ABS_PATH_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|/(?:home|Users|mnt|opt|srv|var|tmp)/)")

_TODO_RE = re.compile(r"\b(TODO|FIXME|XXX|HACK|BUG)\b", re.IGNORECASE)

#: 已从常见库移除的 API：文本匹配，会有少量误报，所以标成 info
#: （本文件与 demo.py 的字符串里含有这些模式本身，用文件级忽略指令关掉自己的误报）
_REMOVED_API = (
    (re.compile(r"\.ix\["), "pandas 的 .ix 已在 1.0 移除，改用 .loc / .iloc"),
    (re.compile(r"\bnp\.(float|int|bool|object|str)\b(?!\d)"), "numpy 的 np.float/np.int 等别名已在 1.24 移除，改用内置类型"),
    (re.compile(r"\bpd\.datetime\b"), "pandas 的 pd.datetime 已移除，改用 pd.Timestamp"),
    (re.compile(r"\.iteritems\("), "DataFrame.iteritems 已移除，改用 .items()"),
    (re.compile(r"\b(df|frame|dataframe)\.append\("), "DataFrame.append 已在 pandas 2.0 移除，改用 pd.concat"),
    (re.compile(r"\bnp\.NaN\b"), "np.NaN 已移除，改用 np.nan"),
    (re.compile(r"\bnp\.Inf\b"), "np.Inf 已移除，改用 np.inf"),
    (re.compile(r"\.applymap\("), "DataFrame.applymap 在 pandas 2.1 起被弃用，改用 .map"),
)


@dataclass
class CodeIssue:
    level: str
    code: str
    message: str
    path: str = ""
    line: int = 0
    hint: str = ""

    def where(self) -> str:
        if not self.path:
            return ""
        # 只显示最后两级，既够区分同名的 checkpoint 副本，又不至于把整条路径糊上去
        parts = [part for part in re.split(r"[\\/]+", self.path) if part]
        shown = "/".join(parts[-2:]) if len(parts) > 1 else self.path
        return f"{shown}:{self.line}" if self.line else shown


@dataclass
class FileStats:
    path: str
    lines: int = 0
    code_lines: int = 0
    functions: int = 0
    classes: int = 0
    todos: int = 0
    imports: list[str] = field(default_factory=list)


class _Visitor(ast.NodeVisitor):
    def __init__(self, path: str):
        self.path = path
        self.issues: list[CodeIssue] = []
        self.functions = 0
        self.classes = 0
        self.imports: list[str] = []

    # -- 小工具
    def _add(self, level: str, code: str, node, message: str, hint: str = "") -> None:
        line = getattr(node, "lineno", 0) or 0
        self.issues.append(CodeIssue(level, code, message, self.path, line, hint))

    def _is_constant_str(self, node) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        return None

    # -- 导入
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.imports.append(alias.name)
            if alias.name.split(".")[0] in NETWORK_MODULES:
                self._add(
                    "info",
                    "CS111",
                    node,
                    f"导入了网络模块 {alias.name}",
                    "断网环境下这条路径必然失败；确认它是不是可以延迟到真正需要时再 import",
                )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level and node.level > 0:
            self.generic_visit(node)
            return
        if node.module:
            self.imports.append(node.module)
            if node.module.split(".")[0] in NETWORK_MODULES:
                self._add(
                    "info",
                    "CS111",
                    node,
                    f"导入了网络模块 {node.module}",
                    "断网环境下这条路径必然失败",
                )
        for alias in node.names:
            if alias.name == "*":
                self._add(
                    "info",
                    "CS107",
                    node,
                    f"from {node.module or '.'} import *",
                    "会污染命名空间并让静态检查失效；改成显式导入",
                )
        self.generic_visit(node)

    # -- 函数 / 类
    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.functions += 1
        self._check_defaults(node)
        self._check_shadowing(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.functions += 1
        self._check_defaults(node)
        self._check_shadowing(node)
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.classes += 1
        self.generic_visit(node)

    def _check_defaults(self, node) -> None:
        for default in node.args.defaults + [item for item in node.args.kw_defaults if item is not None]:
            if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                self._add(
                    "warn",
                    "CS103",
                    node,
                    f"函数 {node.name} 使用了可变默认参数",
                    "默认值只在定义时创建一次，多次调用会共享同一个对象。改用 None 作默认值再在函数内初始化",
                )
            elif isinstance(default, ast.Call) and isinstance(default.func, ast.Name) and default.func.id in ("list", "dict", "set"):
                self._add(
                    "warn",
                    "CS103",
                    node,
                    f"函数 {node.name} 使用了可变默认参数 {default.func.id}()",
                    "同上：默认值在定义时求值一次，会在多次调用间共享",
                )

    def _check_shadowing(self, node) -> None:
        arguments = [arg.arg for arg in node.args.args + node.args.kwonlyargs + node.args.posonlyargs]
        shadowed = sorted({name for name in arguments if name in BUILTIN_NAMES})
        if shadowed:
            self._add(
                "info",
                "CS114",
                node,
                f"函数 {node.name} 的参数名遮蔽了内置函数 {shadowed}",
                "函数内就再也拿不到真正的内置函数了；改名如 list_ / items",
            )

    # -- 异常
    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.type is None:
            self._add(
                "warn",
                "CS101",
                node,
                "裸 except",
                "会同时吞掉 KeyboardInterrupt 和 SystemExit，Ctrl+C 都停不下来；至少写 except Exception",
            )
        elif isinstance(node.type, ast.Name) and node.type.id in ("Exception", "BaseException"):
            if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                self._add(
                    "warn",
                    "CS102",
                    node,
                    f"except {node.type.id}: pass 完全吞掉异常",
                    "数据事故最常见的起点：错误被静默忽略，之后所有结果都不可信。至少记录日志或重抛",
                )
            elif len(node.body) == 1 and isinstance(node.body[0], ast.Return) and node.body[0].value is None:
                self._add(
                    "info",
                    "CS102",
                    node,
                    f"except {node.type.id}: return None 隐藏了失败",
                    "返回 None 会让调用方误以为成功；考虑显式抛出或返回明确的结果对象",
                )
        self.generic_visit(node)

    # -- 调用
    def visit_Call(self, node: ast.Call) -> None:
        func_name = ""
        if isinstance(node.func, ast.Name):
            func_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            func_name = node.func.attr

        if func_name == "open":
            self._check_open(node)
        if func_name in ("eval", "exec"):
            self._add(
                "warn",
                "CS105",
                node,
                f"使用了 {func_name}()",
                "会执行任意代码；日志/数据里如果混进构造串就是远程执行漏洞。应急排查时优先确认输入来源可信",
            )
        if func_name in ("load", "loads") and isinstance(node.func, ast.Attribute):
            owner = node.func.value.id if isinstance(node.func.value, ast.Name) else ""
            if owner == "pickle":
                self._add(
                    "warn",
                    "CS106",
                    node,
                    "使用了 pickle 反序列化",
                    "pickle 加载即执行代码；如果文件来源不可信（比如网络下载的模型/中间件），换 numpy/json/parquet",
                )
            elif owner == "yaml":
                keywords = {keyword.arg for keyword in node.keywords if keyword.arg}
                if "Loader" not in keywords:
                    self._add(
                        "warn",
                        "CS106",
                        node,
                        "yaml.load() 未指定 Loader",
                        "旧式写法等价于 unsafe_load；改用 yaml.safe_load()",
                    )
        if func_name == "chdir":
            self._add(
                "info",
                "CS113",
                node,
                "调用了 os.chdir()",
                "改变全局工作目录，会让「相对路径」的语义随调用顺序漂移；用 pathlib 拼绝对路径更稳",
            )
        if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            owner = node.func.value.id
            if owner in ("logging",) and node.args and isinstance(node.args[0], ast.JoinedStr):
                self._add(
                    "info",
                    "CS109",
                    node,
                    "logging 里用了 f-string",
                    "即使日志级别不输出也会先做字符串拼接；改用 logging.info('x=%s', x)",
                )

        self.generic_visit(node)

    def _check_open(self, node: ast.Call) -> None:
        keywords = {keyword.arg for keyword in node.keywords if keyword.arg}
        if "encoding" in keywords:
            return
        mode = None
        if "mode" in keywords:
            for keyword in node.keywords:
                if keyword.arg == "mode":
                    mode = self._is_constant_str(keyword.value)
        elif len(node.args) >= 2:
            mode = self._is_constant_str(node.args[1])
        if mode and "b" in mode:
            return
        if mode is None and len(node.args) < 2:
            mode = "r"
        if mode is None:
            return
        self._add(
            "warn",
            "CS104",
            node,
            f"open(..., {mode!r}) 没有指定 encoding",
            "会使用系统默认编码（Windows 常是 GBK/cp936，Linux 是 UTF-8）。同一份代码在两台机器上"
            "读出的中文不同，甚至直接 UnicodeDecodeError。建议显式 encoding='utf-8'",
        )

    # -- assert / 字面量
    def visit_Assert(self, node: ast.Assert) -> None:
        self._add(
            "info",
            "CS108",
            node,
            "使用 assert 做检查",
            "python -O 会删除所有 assert；用 assert 做数据校验会在别人机器上失效。改用显式 if + raise",
        )
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        value = node.value
        if isinstance(value, str) and len(value) >= 3 and _ABS_PATH_RE.match(value):
            self._add(
                "warn",
                "CS110",
                node,
                f"硬编码绝对路径 {value[:60]!r}",
                "换机器/换盘符就会失败；改用 pathlib.Path 相对本文件或从配置读",
            )
        self.generic_visit(node)


def _comment_tokens(source: str) -> list[tuple[int, str]]:
    comments: list[tuple[int, str]] = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.COMMENT:
                comments.append((token.start[0], token.string))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    return comments


_FILE_IGNORE_RE = re.compile(r"#\s*ek:ignore-file\s+([A-Za-z0-9,\s]+)")
_LINE_IGNORE_RE = re.compile(r"#\s*(?:noqa\b|ek:ignore\b)", re.IGNORECASE)


def _file_ignores(source: str) -> set[str]:
    """读取文件级豁免指令：前 40 行内 ``# ek:ignore-file CS101,CS108``。"""
    codes: set[str] = set()
    for line in source.splitlines()[:40]:
        match = _FILE_IGNORE_RE.search(line)
        if match:
            for part in re.split(r"[,\s]+", match.group(1).strip()):
                if part:
                    codes.add(part.upper())
    return codes


def _textual_issues(source: str, path: str) -> list[CodeIssue]:
    """基于正则/分词的检查：注释里的 TODO，以及已移除的 API。"""
    issues: list[CodeIssue] = []
    lines = source.splitlines()

    for line_number, comment in _comment_tokens(source):
        if _LINE_IGNORE_RE.search(comment):
            continue
        match = _TODO_RE.search(comment)
        if match:
            issues.append(
                CodeIssue(
                    "info",
                    "CS115",
                    f"待办标记 {match.group(1).upper()}：{comment.strip()[:70]}",
                    path,
                    line_number,
                    "统计这些标记能快速看出哪些代码从来没被回过收",
                )
            )

    for line_number, line in enumerate(lines, start=1):
        if _LINE_IGNORE_RE.search(line):
            continue
        for pattern, hint in _REMOVED_API:
            if pattern.search(line):
                issues.append(
                    CodeIssue(
                        "warn",
                        "CS112",
                        f"可能使用了已移除的 API：{hint}",
                        path,
                        line_number,
                        "文本匹配，可能误报；升级库后这里最容易报 AttributeError（确认无误可在行尾加 # noqa）",
                    )
                )
                break

    return issues


def _ignored_lines(source: str) -> set[int]:
    """行级豁免：``# noqa`` / ``# ek:ignore`` 所在行号。"""
    return {
        number
        for number, line in enumerate(source.splitlines(), start=1)
        if _LINE_IGNORE_RE.search(line)
    }


def scan_python_source(source: str, path: str = "<string>") -> list[CodeIssue]:
    """检查一段 Python 源码（不执行）。"""
    ignored_codes = _file_ignores(source)
    ignored_lines = _ignored_lines(source)

    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        issues = [
            CodeIssue(
                "error",
                "CS001",
                f"语法错误：{exc.msg}",
                path,
                exc.lineno or 0,
                "这个文件根本 import 不了；先修语法再谈其他",
            )
        ]
        return [issue for issue in issues if issue.code not in ignored_codes]

    visitor = _Visitor(path)
    try:
        visitor.visit(tree)
    except RecursionError:
        visitor.issues.append(CodeIssue("warn", "CS099", "AST 递归过深，部分检查被跳过", path, 0))

    issues = visitor.issues + _textual_issues(source, path)
    return [
        issue
        for issue in issues
        if issue.code not in ignored_codes and issue.line not in ignored_lines
    ]


def scan_python_file(path: str | os.PathLike) -> tuple[list[CodeIssue], FileStats | None]:
    """读取并检查单个 .py 文件。返回 (问题列表, 文件统计)。"""
    target = os.fspath(path)
    raw = b""
    try:
        with open(target, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        return [CodeIssue("error", "CS002", f"无法读取：{exc}", target)], None

    issues: list[CodeIssue] = []
    if raw.startswith(b"\xef\xbb\xbf"):
        issues.append(
            CodeIssue(
                "info",
                "CS003",
                "源文件带 UTF-8 BOM",
                target,
                1,
                "大多数解释器能读，但某些构建/打包工具会把 BOM 当成代码；建议存成「UTF-8 无 BOM」",
            )
        )
        source = raw.decode("utf-8-sig", errors="replace")
    else:
        try:
            source = raw.decode("utf-8")
        except UnicodeDecodeError:
            # 再试 GB18030（中文 Windows 的老脚本）；都不行就带替换符继续
            try:
                source = raw.decode("gb18030")
                issues.append(
                    CodeIssue(
                        "warn",
                        "CS004",
                        "源文件不是 UTF-8（按 GB18030 读取成功）",
                        target,
                        1,
                        "Python 3 默认按 UTF-8 读源码，非 UTF-8 文件在别的机器上会 SyntaxError；转成 UTF-8",
                    )
                )
            except UnicodeDecodeError:
                source = raw.decode("utf-8", errors="replace")
                issues.append(
                    CodeIssue("warn", "CS005", "源文件编码无法确定", target, 1, "已按 UTF-8 带替换符读取，检查是否被破坏")
                )

    if "\r\n" in source:
        issues.append(
            CodeIssue("info", "CS006", "源文件使用 CRLF 换行", target, 1, "跨平台协作时会产生大面积无意义 diff")
        )

    issues.extend(scan_python_source(source, target))

    lines = source.splitlines()
    code_lines = sum(1 for line in lines if line.strip() and not line.strip().startswith("#"))
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return issues, FileStats(path=target, lines=len(lines), code_lines=code_lines)

    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            imports.append(node.module)

    stats = FileStats(
        path=target,
        lines=len(lines),
        code_lines=code_lines,
        functions=sum(1 for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))),
        classes=sum(1 for node in ast.walk(tree) if isinstance(node, ast.ClassDef)),
        todos=sum(1 for issue in issues if issue.code == "CS115"),
        imports=sorted(set(imports)),
    )
    return issues, stats


def scan_notebook(path: str | os.PathLike) -> tuple[list[CodeIssue], dict]:
    """检查 Jupyter notebook 的常见陷阱。"""
    target = os.fspath(path)
    issues: list[CodeIssue] = []
    info: dict = {"cells": 0, "code_cells": 0, "error_outputs": 0, "never_run": 0}

    try:
        with open(target, "r", encoding="utf-8-sig") as handle:
            raw_text = handle.read()
    except OSError as exc:
        return [CodeIssue("error", "NB001", f"notebook 无法读取：{exc}", target)], info
    except UnicodeDecodeError as exc:
        return [
            CodeIssue(
                "error",
                "NB001",
                f"notebook 不是 UTF-8 编码：{exc}",
                target,
                0,
                "Jupyter 存盘一律是 UTF-8；出现别的编码说明文件被别的工具改写过，先确认完整性和编码",
            )
        ], info

    stripped = raw_text.lstrip()
    if not stripped.startswith("{"):
        return [
            CodeIssue(
                "error",
                "NB001",
                "这个 .ipynb 其实不是 JSON",
                target,
                0,
                f"首字符是 {stripped[:1]!r}。Jupyter 的 .virtual_documents / .ipynb_checkpoints 里常放着"
                "纯源码副本，它们不能当 notebook 分析；真实 notebook 一定以 { 开头",
            )
        ], info

    try:
        notebook = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        return [
            CodeIssue(
                "error",
                "NB001",
                f"notebook 无法解析：{exc}",
                target,
                0,
                "常见原因：文件被截断、被别的程序写过、或不是真正的 notebook",
            )
        ], info

    cells = notebook.get("cells", [])
    info["cells"] = len(cells)
    counts: list[int] = []
    hardcoded = 0

    for index, cell in enumerate(cells, start=1):
        if cell.get("cell_type") != "code":
            continue
        info["code_cells"] += 1

        source = cell.get("source", "")
        if isinstance(source, list):
            source = "".join(source)
        for line_number, line in enumerate(source.splitlines(), start=1):
            if _ABS_PATH_RE.search(line.strip().strip("\"'")) and len(line.strip()) > 3:
                hardcoded += 1
                if hardcoded <= 3:
                    issues.append(
                        CodeIssue(
                            "warn",
                            "NB005",
                            f"单元格 {index} 里有绝对路径：{line.strip()[:70]}",
                            target,
                            line_number,
                            "notebook 一旦换了目录就跑不了；用 Path.cwd() / 相对路径 / 配置",
                        )
                    )

        for output in cell.get("outputs", []):
            if output.get("output_type") == "error":
                info["error_outputs"] += 1
                name = output.get("ename", "?")
                value = " ".join(output.get("evalue", "").split())[:100]
                issues.append(
                    CodeIssue(
                        "error",
                        "NB002",
                        f"单元格 {index} 存着一条报错输出：{name}: {value}",
                        target,
                        0,
                        "说明这份 notebook 上次没跑通就被保存了；结论可能来自更早的运行",
                    )
                )

        execution = cell.get("execution_count")
        if execution is None:
            info["never_run"] = info.get("never_run", 0) + 1
        else:
            counts.append(execution)

    if counts != sorted(counts):
        issues.append(
            CodeIssue(
                "warn",
                "NB003",
                "单元格执行顺序不是从上到下",
                target,
                0,
                f"执行计数序列为 {counts[:12]}{' ...' if len(counts) > 12 else ''}。"
                "这是「重启后跑不通」的头号原因：变量状态来自更早的单元格。全选 + 从上到下重跑一遍验证",
            )
        )
    if info["never_run"]:
        issues.append(
            CodeIssue(
                "info",
                "NB004",
                f"{info['never_run']} 个代码单元格从未执行",
                target,
                0,
                "未被执行的代码不代表可用；要么删掉要么补跑",
            )
        )

    kernelspec = notebook.get("metadata", {}).get("kernelspec", {}) or {}
    declared = (notebook.get("metadata", {}).get("language_info", {}) or {}).get("version")
    kernel_name = kernelspec.get("name") or kernelspec.get("display_name")
    if declared:
        import sys

        current = ".".join(str(part) for part in sys.version_info[:3])
        if declared.split(".")[:2] != current.split(".")[:2]:
            issues.append(
                CodeIssue(
                    "info",
                    "NB006",
                    f"notebook 声明的 Python 版本是 {declared}，当前解释器是 {current}",
                    target,
                    0,
                    "跨小版本可能有行为差异（如 dict 顺序、typing 语法）；换环境前先对齐",
                )
            )
    info["kernel"] = kernel_name

    return issues, info


def scan_tree(
    root: str | os.PathLike,
    *,
    max_files: int = 3000,
    include_notebooks: bool = True,
) -> tuple[list[CodeIssue], list[FileStats], list[str]]:
    """递归检查目录下的 .py 与 .ipynb。返回 (问题, 统计, 已检查文件列表)。"""
    target = os.fspath(root)
    files: list[str] = []
    if os.path.isfile(target):
        files = [target]
    else:
        for directory, dirnames, filenames in os.walk(target):
            dirnames[:] = [name for name in sorted(dirnames) if name not in files_mod.DEFAULT_IGNORE]
            for filename in sorted(filenames):
                suffix = os.path.splitext(filename)[1].lower()
                if suffix == ".py" or (include_notebooks and suffix == ".ipynb"):
                    files.append(os.path.join(directory, filename))
                    if len(files) >= max_files:
                        break
            if len(files) >= max_files:
                break

    issues: list[CodeIssue] = []
    stats: list[FileStats] = []
    for path in files:
        if path.lower().endswith(".ipynb"):
            notebook_issues, _info = scan_notebook(path)
            issues.extend(notebook_issues)
        else:
            file_issues, file_stats = scan_python_file(path)
            issues.extend(file_issues)
            if file_stats is not None:
                stats.append(file_stats)

    order = {"error": 0, "warn": 1, "info": 2}
    issues.sort(key=lambda issue: (order.get(issue.level, 3), issue.code, issue.path, issue.line))
    return issues, stats, files


def collect_imports(sources: list[str]) -> dict[str, list[str]]:
    """把一批 .py 文件的顶层导入名收集起来：``{模块名: [出现位置]}``。"""
    found: dict[str, list[str]] = {}
    for path in sources:
        if not path.lower().endswith(".py"):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                tree = ast.parse(handle.read(), filename=path)
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                names = [node.module]
            for name in names:
                top = name.split(".")[0]
                found.setdefault(top, [])
                if len(found[top]) < 5:
                    found[top].append(f"{os.path.basename(path)}:{getattr(node, 'lineno', 0)}")
    return found


def check_imports(imports: dict[str, list[str]], *, local_dirs: list[str] | None = None) -> list[CodeIssue]:
    """检查每个顶层导入在当前环境里是否真的可用。

    这是断网排障里性价比最高的一项：报 ``ModuleNotFoundError`` 之前就能知道
    缺的是标准库、第三方包，还是本地文件名字打错了。
    """
    import importlib.util
    import sys

    stdlib = getattr(sys, "stdlib_module_names", frozenset())
    search_dirs = list(local_dirs or []) + [os.getcwd()]
    local_modules = set()
    for directory in search_dirs:
        if not directory or not os.path.isdir(directory):
            continue
        try:
            for entry in os.listdir(directory):
                if entry.endswith(".py") or os.path.isdir(os.path.join(directory, entry, "")):
                    local_modules.add(os.path.splitext(entry)[0])
        except OSError:
            continue

    issues: list[CodeIssue] = []
    for name, places in sorted(imports.items()):
        if name in stdlib or name in local_modules or name.startswith("_"):
            continue
        try:
            spec = importlib.util.find_spec(name)
        except (ImportError, ValueError, ModuleNotFoundError):
            spec = None
        if spec is None:
            issues.append(
                CodeIssue(
                    "error",
                    "CS120",
                    f"导入的模块不可用：{name}（出现在 {', '.join(places[:3])}）",
                    "",
                    0,
                    "断网时无法 pip install。先确认：是不是拼错了？是不是装在另一个 conda 环境里？"
                    "能不能用标准库替代？",
                )
            )
    return issues
