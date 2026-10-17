"""文件系统体检。

断网急救里"文件不见了""磁盘满了""这个目录怎么这么大""为什么删不掉"
是四类高频问题。这个模块用标准库把目录扫一遍，重点回答：

* 空间去哪了（按扩展名 / 按最大文件）
* 有没有零字节文件、疑似写了一半的文件
* 有没有内容完全相同的副本（副本是磁盘爆掉的常见原因）
* 有没有 Windows 上的"非法但能创建出来"的路径（末尾空格、保留名、超长路径）
* 有没有坏软链接 / 读不出来的文件

扫描是**只读**的，不会删任何东西。想清理请自己确认后再动手。
"""

from __future__ import annotations

import hashlib
import os
import time
from collections import Counter
from dataclasses import dataclass, field

__all__ = [
    "Entry",
    "DirSummary",
    "DuplicateGroup",
    "PathProblem",
    "scan",
    "summarize",
    "find_duplicates",
    "path_problems",
]

#: 默认跳过的目录：噪声大且与本工具目标无关
DEFAULT_IGNORE = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        "node_modules",
        ".venv",
        "venv",
        "env",
        ".mypy_cache",
        ".pytest_cache",
        ".ipynb_checkpoints",
        ".virtual_documents",
        ".idea",
        ".vscode",
        "$RECYCLE.BIN",
        "System Volume Information",
    }
)

_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)

_WINDOWS_BAD_CHARS = frozenset('<>:"|?*')


@dataclass
class Entry:
    path: str
    size: int
    mtime: float
    depth: int = 0
    is_link: bool = False
    link_broken: bool = False

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    @property
    def suffix(self) -> str:
        suffix = os.path.splitext(self.path)[1].lower()
        return suffix or "（无扩展名）"


@dataclass
class DuplicateGroup:
    size: int
    count: int
    paths: list[str] = field(default_factory=list)
    digest: str = ""

    @property
    def wasted(self) -> int:
        return self.size * (self.count - 1)


@dataclass
class PathProblem:
    path: str
    kind: str
    detail: str


@dataclass
class DirSummary:
    root: str
    n_files: int = 0
    n_dirs: int = 0
    total_size: int = 0
    truncated: bool = False
    unreadable_dirs: list[str] = field(default_factory=list)
    by_suffix: list[tuple[str, int, int]] = field(default_factory=list)  # (后缀, 数量, 总字节)
    largest: list[Entry] = field(default_factory=list)
    zero_byte: list[Entry] = field(default_factory=list)
    tiny_files: list[Entry] = field(default_factory=list)
    newest: list[Entry] = field(default_factory=list)
    oldest: list[Entry] = field(default_factory=list)
    age_buckets: list[tuple[str, int, int]] = field(default_factory=list)
    duplicates: list[DuplicateGroup] = field(default_factory=list)
    duplicate_wasted: int = 0
    problems: list[PathProblem] = field(default_factory=list)


def scan(
    root: str | os.PathLike,
    *,
    ignore: frozenset[str] | set[str] | None = None,
    max_files: int = 200_000,
    follow_links: bool = False,
) -> tuple[list[Entry], list[str], list[str], bool]:
    """遍历目录。返回 (文件列表, 无法读取的目录, 统计备注, 是否被截断)。"""
    target = os.fspath(root)
    ignore_set = DEFAULT_IGNORE if ignore is None else frozenset(ignore)
    entries: list[Entry] = []
    unreadable: list[str] = []
    notes: list[str] = []
    truncated = False
    n_dirs = 0

    stack: list[tuple[str, int]] = [(os.path.abspath(target), 0)]
    while stack:
        directory, depth = stack.pop()
        try:
            iterator = os.scandir(directory)
        except OSError as exc:
            unreadable.append(f"{directory}（{exc.__class__.__name__}）")
            continue
        with iterator:
            for item in iterator:
                try:
                    if item.is_symlink():
                        broken = False
                        if follow_links:
                            broken = not os.path.exists(item.path)
                        else:
                            try:
                                broken = not os.path.exists(os.readlink(item.path)) and not os.path.exists(
                                    os.path.realpath(item.path)
                                )
                            except OSError:
                                broken = True
                        if item.is_dir(follow_symlinks=False):
                            if broken:
                                entries.append(
                                    Entry(item.path, 0, 0.0, depth + 1, is_link=True, link_broken=True)
                                )
                            continue  # 不进入软链接目录，避免环
                        entry = Entry(item.path, 0, 0.0, depth + 1, is_link=True, link_broken=broken)
                        if not broken:
                            try:
                                stat = item.stat(follow_symlinks=True)
                                entry.size = stat.st_size
                                entry.mtime = stat.st_mtime
                            except OSError:
                                pass
                        entries.append(entry)
                        continue

                    if item.is_dir(follow_symlinks=False):
                        n_dirs += 1
                        if item.name in ignore_set:
                            continue
                        stack.append((item.path, depth + 1))
                        continue

                    stat = item.stat(follow_symlinks=False)
                    entries.append(Entry(item.path, stat.st_size, stat.st_mtime, depth + 1))
                    if len(entries) >= max_files:
                        truncated = True
                        notes.append(f"文件数达到上限 {max_files}，已停止遍历")
                        stack.clear()
                        break
                except OSError as exc:
                    unreadable.append(f"{item.path}（{exc.__class__.__name__}）")

    notes.append(f"共 {len(entries)} 个文件 / {n_dirs} 个目录（已跳过 {sorted(ignore_set)[:4]} 等噪声目录）")
    return entries, unreadable, notes, truncated


def _age_bucket(mtime: float, now: float) -> str:
    age = now - mtime
    if age < 86400:
        return "今天"
    if age < 7 * 86400:
        return "一周内"
    if age < 30 * 86400:
        return "一月内"
    if age < 365 * 86400:
        return "一年内"
    return "超过一年"


def find_duplicates(
    entries: list[Entry],
    *,
    max_groups: int = 20,
    full_hash_limit: int = 16 * 1024 * 1024,
    max_files_per_group: int = 50,
) -> list[DuplicateGroup]:
    """按内容找重复文件。

    先按大小分组（大小不同必然内容不同），再对同大小组计算摘要：
    小文件做全文 sha256，大文件做"头部 64KB + 尾部 64KB + 大小"的指纹，
    并在结果里注明用了哪种方式，避免把小概率碰撞当成已证明的重复。
    """
    by_size: dict[int, list[Entry]] = {}
    for entry in entries:
        if entry.size <= 0 or entry.link_broken:
            continue
        by_size.setdefault(entry.size, []).append(entry)

    groups: list[DuplicateGroup] = []
    for size, bucket in by_size.items():
        if len(bucket) < 2:
            continue
        bucket = bucket[:max_files_per_group]
        digests: dict[str, list[str]] = {}
        for entry in bucket:
            digest = _digest(entry.path, size, full_hash_limit)
            if digest is None:
                continue
            digests.setdefault(digest, []).append(entry.path)
        for digest, paths in digests.items():
            if len(paths) < 2:
                continue
            groups.append(DuplicateGroup(size=size, count=len(paths), paths=sorted(paths), digest=digest[:16]))

    groups.sort(key=lambda group: -group.wasted)
    return groups[:max_groups]


def _digest(path: str, size: int, full_hash_limit: int) -> str | None:
    hasher = hashlib.sha256()
    try:
        if size <= full_hash_limit:
            with open(path, "rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    hasher.update(chunk)
            return "full:" + hasher.hexdigest()
        with open(path, "rb") as handle:
            hasher.update(handle.read(65536))
            handle.seek(-65536, os.SEEK_END)
            hasher.update(handle.read(65536))
        hasher.update(str(size).encode("ascii"))
        return "part:" + hasher.hexdigest()
    except OSError:
        return None


def path_problems(entries: list[Entry], *, check_readable: int = 200) -> list[PathProblem]:
    """路径层面的坑：Windows 长路径、保留名、末尾空格/点、坏软链接、大小写冲突。"""
    problems: list[PathProblem] = []
    seen_case: dict[str, str] = {}

    for entry in entries:
        name = entry.name
        full = os.path.abspath(entry.path)

        if entry.link_broken:
            problems.append(PathProblem(entry.path, "坏软链接", "指向的目标不存在"))

        if len(full) > 259 and os.name == "nt":
            problems.append(
                PathProblem(entry.path, "路径过长", f"{len(full)} 字符（>259），部分工具会读写失败")
            )

        if name != name.rstrip(" ."):
            problems.append(PathProblem(entry.path, "名字以空格或点结尾", "Windows 上无法正常删除/重命名"))

        stem = os.path.splitext(name)[0].upper()
        if stem in _WINDOWS_RESERVED:
            problems.append(PathProblem(entry.path, "Windows 保留名", f"{stem} 是设备名，某些 API 会拒绝访问"))

        bad = sorted({char for char in name if char in _WINDOWS_BAD_CHARS})
        if bad:
            problems.append(PathProblem(entry.path, "文件名含非法字符", f"{bad}"))

        key = os.path.normcase(full)
        if key in seen_case and seen_case[key] != full:
            problems.append(
                PathProblem(entry.path, "大小写仅不同的同名文件", f"与 {seen_case[key]} 冲突（Windows 不区分大小写）")
            )
        seen_case[key] = full

    for entry in entries[:check_readable]:
        if entry.link_broken or entry.size == 0:
            continue
        try:
            with open(entry.path, "rb") as handle:
                handle.read(1)
        except OSError as exc:
            problems.append(PathProblem(entry.path, "无法读取", exc.__class__.__name__ + ": " + str(exc)[:80]))

    return problems


def summarize(
    entries: list[Entry],
    root: str | os.PathLike,
    *,
    unreadable: list[str] | None = None,
    truncated: bool = False,
    top: int = 10,
    want_duplicates: bool = True,
) -> DirSummary:
    """把扫描结果压成一张可供报告渲染的摘要。"""
    now = time.time()
    summary = DirSummary(root=os.fspath(root), n_files=len(entries), truncated=truncated)
    summary.unreadable_dirs = list(unreadable or [])
    summary.total_size = sum(entry.size for entry in entries)

    suffix_counter: Counter[str] = Counter()
    suffix_bytes: Counter[str] = Counter()
    age_counter: Counter[str] = Counter()
    age_bytes: Counter[str] = Counter()

    for entry in entries:
        suffix_counter[entry.suffix] += 1
        suffix_bytes[entry.suffix] += entry.size
        bucket = _age_bucket(entry.mtime, now)
        age_counter[bucket] += 1
        age_bytes[bucket] += entry.size
        if entry.size == 0 and not entry.is_link:
            summary.zero_byte.append(entry)
        elif 0 < entry.size < 1024:
            summary.tiny_files.append(entry)

    summary.by_suffix = [(suffix, count, suffix_bytes[suffix]) for suffix, count in suffix_counter.most_common(15)]
    for order in ("今天", "一周内", "一月内", "一年内", "超过一年"):
        if age_counter.get(order):
            summary.age_buckets.append((order, age_counter[order], age_bytes[order]))

    real = [entry for entry in entries if not entry.link_broken]
    summary.largest = sorted(real, key=lambda entry: -entry.size)[:top]
    summary.newest = sorted(real, key=lambda entry: -entry.mtime)[:top]
    summary.oldest = sorted(real, key=lambda entry: entry.mtime)[:top]
    summary.zero_byte.sort(key=lambda entry: -entry.mtime)
    summary.tiny_files.sort(key=lambda entry: -entry.mtime)

    if want_duplicates:
        summary.duplicates = find_duplicates(entries)
        summary.duplicate_wasted = sum(group.wasted for group in summary.duplicates)

    summary.problems = path_problems(entries)
    return summary
