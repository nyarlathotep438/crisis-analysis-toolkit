"""系统 / 运行环境快照。

"同一份代码昨天能跑今天跑不了"这种事故，九成不是代码变了，而是**环境变了**：
换了 conda 环境、装了第二个 Python、PATH 被 Anaconda 抢走、
临时目录满了、代理没关导致所有请求卡住、或者本地有个 ``json.py``
把标准库顶掉了。

这个模块把这些都摆到一张屏幕上，并且对每一处可疑点给出判断，
而不只是"打印一堆环境变量让你自己看"。
"""

from __future__ import annotations

import locale
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime

from . import deps, fmt

__all__ = ["EnvSnapshot", "collect", "which_all"]


@dataclass
class EnvSnapshot:
    sections: list[tuple[str, list[tuple[str, str]]]] = field(default_factory=list)
    issues: list[tuple[str, str, str]] = field(default_factory=list)  # (level, code, message)
    notes: list[str] = field(default_factory=list)

    def add_section(self, title: str, rows: list[tuple[str, str]]) -> None:
        self.sections.append((title, rows))

    def issue(self, level: str, code: str, message: str) -> None:
        self.issues.append((level, code, message))


# ---------------------------------------------------------------- 小工具


def which_all(executable: str) -> list[str]:
    """在 PATH 中找出所有同名可执行文件（Windows 会补 .exe / .bat / .cmd）。"""
    found: list[str] = []
    names = [executable]
    if os.name == "nt" and not executable.lower().endswith((".exe", ".bat", ".cmd", ".com")):
        names = [f"{executable}{suffix}" for suffix in (".exe", ".bat", ".cmd", ".com")] + [executable]
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        for name in names:
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate):
                found.append(candidate)
    # 去重但保持顺序
    seen = set()
    unique = []
    for path in found:
        key = os.path.normcase(os.path.abspath(path))
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def _is_writable(directory: str) -> bool:
    """真实地写一个临时文件再删掉 —— 只查权限位在 Windows 上会骗人。"""
    if not directory or not os.path.isdir(directory):
        return False
    try:
        handle = tempfile.NamedTemporaryFile(dir=directory, prefix=".ek_probe_", delete=True)
    except OSError:
        return False
    try:
        handle.write(b"ok")
        handle.flush()
    except OSError:
        return False
    finally:
        handle.close()
    return True


def _stdlib_dir(path: str) -> bool:
    return os.path.isfile(os.path.join(path, "os.py")) and os.path.isfile(os.path.join(path, "abc.py"))


def _shadowed_stdlib(directories: list[str]) -> list[tuple[str, str]]:
    """本地同名文件覆盖标准库 —— 会导致"导入了一个不认识的 json/json.py"。

    必须排除解释器自身的安装目录：``...\\anaconda3\\DLLs`` 里的
    ``_socket.pyd``、``pyexpat.pyd`` 就是标准库本体，把它们报成"覆盖标准库"
    会让这条检查彻底失去信噪比（第一次运行就刷屏 20 条假警报）。
    """
    stdlib = getattr(sys, "stdlib_module_names", frozenset())
    if not stdlib:
        return []

    prefixes = []
    for base in {sys.prefix, getattr(sys, "base_prefix", sys.prefix)}:
        if base:
            prefixes.append(os.path.normcase(os.path.abspath(base)) + os.sep)

    hits: list[tuple[str, str]] = []
    for directory in directories:
        if not directory or not os.path.isdir(directory) or _stdlib_dir(directory):
            continue
        absolute = os.path.normcase(os.path.abspath(directory)) + os.sep
        if any(absolute.startswith(prefix) for prefix in prefixes):
            continue
        try:
            entries = os.listdir(directory)
        except OSError:
            continue
        for entry in entries:
            base, extension = os.path.splitext(entry)
            if extension not in (".py", ".pyc", ".pyd") or base not in stdlib:
                continue
            hits.append((os.path.join(directory, entry), base))
    return hits


def _git_info(root: str) -> list[tuple[str, str]] | None:
    """仓库状态。断网也能用，超时 5 秒，git 不存在就返回 None。"""
    if not shutil.which("git"):
        return None
    rows: list[tuple[str, str]] = []

    def run(*arguments: str) -> str | None:
        try:
            completed = subprocess.run(
                ["git", *arguments],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if completed.returncode != 0:
            return None
        return completed.stdout.strip()

    inside = run("rev-parse", "--is-inside-work-tree")
    if inside != "true":
        return None
    rows.append(("分支", run("rev-parse", "--abbrev-ref", "HEAD") or "?"))
    rows.append(("提交", run("rev-parse", "--short", "HEAD") or "?"))
    status = run("status", "--porcelain")
    if status is None:
        rows.append(("工作区", "无法获取"))
    elif status == "":
        rows.append(("工作区", "干净"))
    else:
        rows.append(("工作区", f"有 {len(status.splitlines())} 处未提交改动"))
    return rows


def _memory_total() -> str:
    psutil = deps.optional("psutil")
    if psutil is not None:
        try:
            memory = psutil.virtual_memory()
            return f"总 {fmt.human_bytes(memory.total)}，可用 {fmt.human_bytes(memory.available)}"
        except Exception:  # noqa: CS102 - 可选依赖探测失败就退回标准库方案，属于预期
            pass
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return f"总 {fmt.human_bytes(pages * page_size)}"
    except (AttributeError, ValueError, OSError):
        return "不可得（装 psutil 可获得）"


# ---------------------------------------------------------------- 主入口


def collect(*, root: str | None = None, include_git: bool = True) -> EnvSnapshot:
    """收集环境快照与问题清单。``root`` 默认取当前工作目录。"""
    snapshot = EnvSnapshot()
    root = root or os.getcwd()

    # ---- Python 解释器
    python_rows = [
        ("版本", sys.version.split()[0]),
        ("实现", f"{platform.python_implementation()} {sys.version_info.releaselevel}"),
        ("可执行文件", sys.executable),
        ("架构", "64 位" if sys.maxsize > 2**32 else "32 位"),
        ("sys.prefix", sys.prefix),
        ("base_prefix", getattr(sys, "base_prefix", sys.prefix)),
        ("虚拟环境", "是" if sys.prefix != getattr(sys, "base_prefix", sys.prefix) else "否（系统解释器）"),
        ("优化模式", "是（assert 被禁用）" if sys.flags.optimize else "否"),
    ]
    snapshot.add_section("Python 解释器", python_rows)

    # ---- 平台
    platform_rows = [
        ("操作系统", f"{platform.system()} {platform.release()}"),
        ("版本", platform.version()[:90]),
        ("机器", platform.machine()),
        ("CPU 核心", str(os.cpu_count() or "?")),
        ("主机名", platform.node() or "?"),
        ("启动时间", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    ]
    snapshot.add_section("平台", platform_rows)

    # ---- 编码与区域
    stdio_encoding = getattr(sys.stdout, "encoding", None)
    encoding_rows = [
        ("文件系统编码", sys.getfilesystemencoding()),
        ("标准输出编码", str(stdio_encoding)),
        ("首选编码", locale.getpreferredencoding(False)),
        ("区域(locale)", ".".join(part for part in locale.getlocale() if part) or "未设置"),
        ("时区", os.environ.get("TZ") or timezone_name()),
        ("当前目录", os.getcwd()),
    ]
    snapshot.add_section("编码与区域", encoding_rows)

    # ---- 依赖
    dep_rows = []
    for info in deps.status():
        if info.available:
            dep_rows.append((info.name, f"{info.version or '?'} —— {info.purpose}"))
        else:
            dep_rows.append((info.name, f"缺失 —— {info.purpose}"))
    try:
        from importlib.metadata import distributions

        total = sum(1 for _ in distributions())
        dep_rows.append(("已安装包总数", str(total)))
    except Exception:
        dep_rows.append(("已安装包总数", "无法统计"))
    pip_spec = None
    try:
        import importlib.util

        pip_spec = importlib.util.find_spec("pip") is not None
    except Exception:
        pip_spec = None
    dep_rows.append(("pip 可用", "是" if pip_spec else "否（断网时无法安装任何东西）"))
    snapshot.add_section("可选依赖", dep_rows)

    # ---- 关键路径
    path_rows = []
    seen_paths: dict[str, int] = {}
    for index, entry in enumerate(sys.path):
        if entry == "":
            path_rows.append((f"sys.path[{index}]", "（空字符串 = 当前目录，导入优先级最高，风险最大）"))
            continue
        exists = os.path.isdir(entry)
        marker = "" if exists else "  [不存在]"
        path_rows.append((f"sys.path[{index}]", entry + marker))
        key = os.path.normcase(os.path.abspath(entry)) if exists else entry
        seen_paths[key] = seen_paths.get(key, 0) + 1
    snapshot.add_section("解释器搜索路径", path_rows)

    # ---- PATH
    path_env = os.environ.get("PATH", "")
    entries = [entry for entry in path_env.split(os.pathsep) if entry]
    pythons = which_all("python")
    path_rows = [
        ("PATH 条目数", str(len(entries))),
        ("PATH 长度", f"{len(path_env)} 字符"),
        ("python 可执行文件数", str(len(pythons))),
    ]
    for index, candidate in enumerate(pythons[:6]):
        path_rows.append((f"python[{index}]", candidate))
    skipped = [entry for entry in entries if not os.path.isdir(entry)]
    if skipped:
        path_rows.append(("PATH 中不存在的目录", f"{len(skipped)} 个：{skipped[:4]}"))
    snapshot.add_section("PATH", path_rows)

    # ---- 磁盘与临时目录
    disk_rows = []
    for label, target in (("工作目录", root), ("临时目录", tempfile.gettempdir())):
        try:
            usage = shutil.disk_usage(target)
        except OSError as exc:
            disk_rows.append((label, f"无法获取：{exc}"))
            continue
        used_share = usage.used / usage.total if usage.total else 0
        disk_rows.append(
            (
                f"{label} ({target})",
                f"总 {fmt.human_bytes(usage.total)} / 可用 {fmt.human_bytes(usage.free)} / 已用 {used_share * 100:.1f}%",
            )
        )
    disk_rows.append(("内存", _memory_total()))
    snapshot.add_section("磁盘与内存", disk_rows)

    # ---- 环境变量（只挑会惹事的那几个）
    watched = (
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONIOENCODING",
        "PYTHONUTF8",
        "PYTHONSTARTUP",
        "CONDA_PREFIX",
        "CONDA_DEFAULT_ENV",
        "VIRTUAL_ENV",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "REQUESTS_CA_BUNDLE",
        "PIP_INDEX_URL",
        "OMP_NUM_THREADS",
        "TMPDIR",
    )
    env_rows = []
    for name in watched:
        value = os.environ.get(name)
        if value is None:
            continue
        shown = value if len(value) <= 110 else value[:107] + "..."
        env_rows.append((name, shown))
    if not env_rows:
        env_rows.append(("（无）", "以上环境变量均未设置，属于较干净的状态"))
    snapshot.add_section("相关环境变量", env_rows)

    # ---- 仓库状态
    if include_git:
        git_rows = _git_info(root)
        if git_rows:
            snapshot.add_section("版本库", git_rows)
        else:
            snapshot.add_section("版本库", [("状态", "不是 git 仓库，或未安装 git")])

    # ---- 问题判断
    _diagnose(snapshot, root, entries, pythons, stdio_encoding, pip_spec)

    snapshot.notes.append("本命令不联网：没有 ping、没有 DNS 查询、没有 pip index 访问。")
    return snapshot


def timezone_name() -> str:
    try:
        moment = datetime.now().astimezone()
        offset = moment.utcoffset()
        name = moment.tzname() or "?"
        return f"{name} (UTC{'+' if offset and offset.total_seconds() >= 0 else '-'}{abs(offset.total_seconds()) / 3600:.0f})"
    except Exception:
        return "未知"


def _diagnose(
    snapshot: EnvSnapshot,
    root: str,
    path_entries: list[str],
    pythons: list[str],
    stdio_encoding: str | None,
    pip_spec: bool | None,
) -> None:
    """把观察结果变成结论。宁可多说一句，也别让人自己去猜。"""

    if os.environ.get("PYTHONHOME"):
        snapshot.issue(
            "error",
            "ENV001",
            f"设置了 PYTHONHOME={os.environ['PYTHONHOME']}。这会让解释器去找错误的类库目录，"
            "典型症状是 import 随机失败、pip 装到别处。断网排障时请先撤销它。",
        )

    pythonpath = os.environ.get("PYTHONPATH")
    if pythonpath:
        missing = [entry for entry in pythonpath.split(os.pathsep) if entry and not os.path.isdir(entry)]
        if missing:
            snapshot.issue("warn", "ENV002", f"PYTHONPATH 中有 {len(missing)} 个不存在的目录：{missing[:3]}")
        snapshot.issue(
            "info",
            "ENV003",
            f"设置了 PYTHONPATH={pythonpath}。它会被插入 sys.path 前部，可能覆盖已安装的同名包。",
        )

    proxy_vars = [name for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy") if os.environ.get(name)]
    if proxy_vars:
        snapshot.issue(
            "warn",
            "ENV004",
            f"设置了代理变量 {proxy_vars}。断网环境下任何走代理的请求都会长时间卡住"
            "（表现像「程序假死」），而不是快速失败。排查网络相关代码前建议先 unset 掉。",
        )

    if pip_spec is False:
        snapshot.issue("info", "ENV005", "当前解释器没有 pip：断网时无法安装任何包，只能靠已有环境。")

    if stdio_encoding:
        normalized = stdio_encoding.lower().replace("-", "").replace("_", "")
        if normalized not in ("utf8", "utf8mb4", "ascii"):
            snapshot.issue(
                "warn",
                "ENV006",
                f"标准输出编码是 {stdio_encoding}。输出中文或特殊符号时可能抛 UnicodeEncodeError，"
                "重定向到文件时尤其容易。可先执行 chcp 65001，或设置 PYTHONIOENCODING=utf-8。",
            )

    if len(pythons) > 1:
        snapshot.issue(
            "warn",
            "ENV007",
            f"PATH 里有 {len(pythons)} 个 python 可执行文件，最先命中的是 {pythons[0]}。"
            f"当前运行的是 {sys.executable}。两者不一致时，会出现"
            "「明明 pip install 了却 import 不到」的经典现象。",
        )

    if not _is_writable(root):
        snapshot.issue(
            "error",
            "ENV008",
            f"当前工作目录不可写：{root}。任何需要落盘的操作都会失败（包括本工具的 --out 与 matplotlib 字体缓存）。",
        )
    temp_dir = tempfile.gettempdir()
    if not _is_writable(temp_dir):
        snapshot.issue(
            "error",
            "ENV009",
            f"临时目录不可写：{temp_dir}。会让 tempfile、matplotlib、pandas 的中间文件全部失败。",
        )

    for label, target in (("工作目录", root), ("临时目录", temp_dir)):
        try:
            usage = shutil.disk_usage(target)
        except OSError:
            continue
        share = usage.free / usage.total if usage.total else 0
        if usage.free < 512 * 1024 * 1024 or share < 0.05:
            snapshot.issue(
                "error",
                "ENV010",
                f"{label} 剩余空间只有 {fmt.human_bytes(usage.free)}（{share * 100:.1f}%）。"
                "数据和缓存写不进去时的报错往往莫名其妙，先看这里。",
            )
        elif usage.free < 2 * 1024 * 1024 * 1024:
            snapshot.issue("warn", "ENV011", f"{label} 剩余空间偏低：{fmt.human_bytes(usage.free)}。")

    for shadow_path, module_name in _shadowed_stdlib([root, os.getcwd(), *sys.path[:3]]):
        snapshot.issue(
            "warn",
            "ENV012",
            f"{shadow_path} 与标准库模块 {module_name!r} 同名，可能覆盖标准库。"
            f"典型症状：import {module_name} 后报 AttributeError。把它改名即可。",
        )

    if any(entry == "" for entry in sys.path):
        snapshot.issue(
            "info",
            "ENV013",
            "sys.path 含空字符串（当前目录优先）。在数据目录里运行脚本时，"
            "同名 .py 文件会覆盖已安装的包 —— 这也是「在别处能跑」的常见原因。",
        )

    duplicates = {}
    for entry in path_entries:
        key = os.path.normcase(os.path.abspath(entry))
        duplicates[key] = duplicates.get(key, 0) + 1
    repeated = [key for key, count in duplicates.items() if count > 1]
    if repeated:
        snapshot.issue("info", "ENV014", f"PATH 中有 {len(repeated)} 个重复目录，例如 {repeated[:2]}。")

    if sys.flags.optimize:
        snapshot.issue(
            "warn",
            "ENV015",
            "解释器运行在 -O 优化模式：assert 语句被整段删除。"
            "用 assert 做输入校验的代码在别人机器上会报错，在你这里不会。",
        )

    if os.name == "nt" and len(os.environ.get("PATH", "")) > 2047:
        snapshot.issue(
            "info",
            "ENV016",
            f"PATH 长度 {len(os.environ.get('PATH', ''))} 字符，接近 Windows 的历史限制，"
            "后面追加的路径可能被静默截断。",
        )

    if not os.path.isdir(os.path.join(sys.prefix, "Lib")) and os.name == "nt":
        snapshot.issue("info", "ENV017", f"sys.prefix 下没有 Lib 目录：{sys.prefix}。确认这是完整的 Python 安装。")

    for name in ("sitecustomize", "usercustomize"):
        try:
            import importlib.util

            spec = importlib.util.find_spec(name)
        except Exception:
            spec = None
        if spec is not None and spec.origin:
            snapshot.issue(
                "info",
                "ENV018",
                f"存在 {name} ({spec.origin})，它会在解释器启动时自动执行，可能改变默认行为。",
            )

    if str(root).startswith("\\\\"):
        snapshot.issue(
            "warn",
            "ENV019",
            f"工作目录是网络路径（UNC）：{root}。文件锁、权限、速度都不可靠，"
            "大文件读写失败时优先怀疑这里。",
        )
