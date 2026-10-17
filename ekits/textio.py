"""文本 / 编码 / 日志诊断。

断网急救里出现频率最高的数据事故，不是算法错了，而是**编码错了**：
CSV 是 GBK、Python 默认按 UTF-8 读，于是中文列名全变问号或乱码；
或者反过来，文件里混了 UTF-8 BOM，让第一列列名变成 ``\\ufeffid``，
按列名取数据时永远 KeyError。

这里提供三件事：

1. ``detect_encoding``：对一段字节给出"最可能是什么编码 + 把握有多大"，
   并说明判断依据，而不是只丢一个名字让你信。
2. ``analyze_text``：行尾、空字节、替换字符、超长行等纯文本体检。
3. ``scan_log``：不用正则拼日志分析器，按"签名归一化"把海量日志聚成几类。
"""

from __future__ import annotations

import codecs
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

__all__ = [
    "EncodingGuess",
    "DecodedText",
    "TextFacts",
    "LogScan",
    "detect_encoding",
    "read_decoded",
    "analyze_text",
    "scan_log",
    "signature",
    "looks_like_mojibake",
    "coerce_cp936",
]

#: 按"先验可能性"排序的候选编码。utf-8 单独特殊处理（严格解码成功即采信）。
_CANDIDATES = ("gb18030", "big5", "shift_jis", "cp1252")

_TS_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(?:[.,](\d{1,6}))?")

_LEVEL_RE = re.compile(
    r"\b(TRACE|DEBUG|INFO|INFORMATION|NOTICE|WARN(?:ING)?|ERROR|ERR|"
    r"CRIT(?:ICAL)?|FATAL|SEVERE|PANIC)\b",
    re.IGNORECASE,
)

_LEVEL_NORMALIZE = {
    "TRACE": "TRACE",
    "DEBUG": "DEBUG",
    "INFO": "INFO",
    "INFORMATION": "INFO",
    "NOTICE": "NOTICE",
    "WARN": "WARN",
    "WARNING": "WARN",
    "ERROR": "ERROR",
    "ERR": "ERROR",
    "CRIT": "CRITICAL",
    "CRITICAL": "CRITICAL",
    "FATAL": "FATAL",
    "SEVERE": "ERROR",
    "PANIC": "FATAL",
}

_EXCEPTION_RE = re.compile(r"\b([A-Z][A-Za-z0-9_]*(?:Error|Exception|Warning|Fault))\b")

_SUSPICIOUS = (
    "traceback (most recent call last)",
    "segmentation fault",
    "out of memory",
    "memoryerror",
    "no space left",
    "disk quota exceeded",
    "permission denied",
    "connection refused",
    "connection reset",
    "timed out",
    "killed",
    "core dumped",
    "corrupt",
    "checksum mismatch",
)

_SIGNATURE_SUBS = (
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\b"), "<ts>"),
    (
        re.compile(
            r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
        ),
        "<uuid>",
    ),
    (re.compile(r"\b0[xX][0-9a-fA-F]+\b"), "<hex>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"), "<ip>"),
    (re.compile(r"\b[0-9a-f]{16,}\b"), "<hash>"),
    (re.compile(r"\b\d+(?:\.\d+)?\b"), "<n>"),
    (re.compile(r"\s+"), " "),
)

_MOJIBAKE_SIGNS = (
    ("锟斤拷", "经典 GBK→UTF-8 二次损坏"),
    ("ï»¿", "UTF-8 BOM 被当成可见字符"),
    ("â€", "UTF-8 被按 cp1252/latin-1 解码"),
    ("Ã©", "UTF-8 被按 cp1252/latin-1 解码"),
    ("ãƒ", "UTF-8 被按 cp1252/latin-1 解码"),
    ("\ufffd", "解码时出现了替换字符，说明编码猜错或数据已损坏"),
)

_C1_RANGE = range(0x80, 0xA0)


@dataclass
class EncodingGuess:
    encoding: str
    method: str
    confidence: str  # "high" | "medium" | "low"
    notes: list[str] = field(default_factory=list)

    @property
    def is_bom(self) -> bool:
        return self.encoding.endswith("-sig")

    def describe(self) -> str:
        return f"{self.encoding}（{self.method}，把握 {self.confidence}）"


@dataclass
class DecodedText:
    path: str
    size: int
    text: str
    guess: EncodingGuess
    bad_bytes: int = 0
    truncated: bool = False

    @property
    def lines(self) -> list[str]:
        return self.text.splitlines()


@dataclass
class TextFacts:
    size: int = 0
    encoding: str = ""
    bom: bool = False
    has_null_bytes: bool = False
    looks_binary: bool = False
    lines: int = 0
    blank_lines: int = 0
    crlf: int = 0
    lf_only: int = 0
    cr_only: int = 0
    ends_with_newline: bool = True
    max_line_len: int = 0
    max_line_no: int = 0
    non_ascii_chars: int = 0
    replacement_chars: int = 0
    control_chars: int = 0
    tabs: int = 0
    trailing_space_lines: int = 0
    mojibake: list[str] = field(default_factory=list)

    @property
    def line_ending_mix(self) -> bool:
        kinds = sum(1 for value in (self.crlf, self.lf_only, self.cr_only) if value > 0)
        return kinds > 1


@dataclass
class LogScan:
    total_lines: int = 0
    non_empty_lines: int = 0
    levels: dict[str, int] = field(default_factory=dict)
    signatures: list[tuple[int, str]] = field(default_factory=list)
    exceptions: list[tuple[int, str]] = field(default_factory=list)
    first_ts: datetime | None = None
    last_ts: datetime | None = None
    suspicious: list[tuple[int, str]] = field(default_factory=list)
    max_line_len: int = 0
    max_line_no: int = 0


# ---------------------------------------------------------------- 编码探测


def _looks_ascii(data: bytes) -> bool:
    return all(byte < 0x80 for byte in data)


def _bom_guess(data: bytes) -> EncodingGuess | None:
    # UTF-32 的 BOM 以 UTF-16 LE 的 BOM 开头，必须先判断 UTF-32
    for bom, name in (
        (codecs.BOM_UTF32_LE, "utf-32"),
        (codecs.BOM_UTF32_BE, "utf-32"),
        (codecs.BOM_UTF8, "utf-8-sig"),
        (codecs.BOM_UTF16_LE, "utf-16"),
        (codecs.BOM_UTF16_BE, "utf-16"),
    ):
        if data.startswith(bom):
            return EncodingGuess(
                encoding=name,
                method=f"文件头 BOM = {bom.hex(' ').upper()}",
                confidence="high",
                notes=["带 BOM 的文件在按字节比较/拼接时容易出意外，注意第一列列名可能含 \\ufeff"],
            )
    return None


def _utf16_no_bom_hint(data: bytes) -> bool:
    """没有 BOM 的 UTF-16 在 ASCII 内容里会有一半的 0x00。"""
    sample = data[:4096]
    if len(sample) < 4:
        return False
    zeros = sample.count(0)
    return zeros / len(sample) > 0.25


def _printable_score(text: str) -> float:
    """粗略判断一段解码结果"像不像正常文本"，1.0 最好。"""
    if not text:
        return 0.0
    sample = text[:20000]
    good = 0.0
    for char in sample:
        code = ord(char)
        if char in "\t\n\r":
            good += 1.0
        elif code < 0x80:
            good += 1.0 if code >= 0x20 else 0.0
        elif code in _C1_RANGE:
            good += 0.0
        elif 0x4E00 <= code <= 0x9FFF:  # CJK 统一表意文字
            good += 1.0
        elif 0x3000 <= code <= 0x303F:  # CJK 标点
            good += 1.0
        elif 0xFF00 <= code <= 0xFFEF:  # 全角字符
            good += 0.8
        elif 0xAC00 <= code <= 0xD7AF:  # 韩文
            good += 1.0
        elif 0x0400 <= code <= 0x04FF:  # 西里尔
            good += 0.9
        elif 0x00A0 <= code <= 0x024F:  # 拉丁扩展
            good += 0.6
        else:
            good += 0.3
    return good / len(sample)


def detect_encoding(data: bytes, *, sample: int = 262144) -> EncodingGuess:
    """判断字节流的编码，返回带理由和置信度的结论。

    策略：BOM → 纯 ASCII → 严格 UTF-8 → UTF-16 零字节特征 → 其他候选按
    "解码后可打印比例"打分。UTF-8 之所以优先，是因为它的字节结构约束强，
    严格解码成功的概率性证据最强。
    """
    if not data:
        return EncodingGuess("utf-8", "空文件", "low", ["文件为空，编码无从判断"])

    head = data[:sample]

    bom = _bom_guess(head)
    if bom is not None:
        return bom

    if _looks_ascii(head):
        return EncodingGuess("ascii", "全部字节 < 0x80", "high", [])

    if _utf16_no_bom_hint(head):
        for name in ("utf-16-le", "utf-16-be"):
            try:
                head.decode(name)
            except UnicodeDecodeError:
                continue
            shown = data.decode(name, errors="replace")
            if _printable_score(shown) > 0.95:
                return EncodingGuess(
                    name,
                    "无 BOM 但大量 0x00，且该端序解码可读",
                    "medium",
                    ["无 BOM 的 UTF-16 很脆弱，建议转换存成 UTF-8"],
                )

    try:
        head.decode("utf-8")
    except UnicodeDecodeError as exc:
        utf8_error = f"UTF-8 严格解码在第 {exc.start} 字节失败：{exc.reason}"
    else:
        return EncodingGuess("utf-8", "UTF-8 严格解码成功", "high", [])

    scored: list[tuple[float, str]] = []
    for name in _CANDIDATES:
        try:
            decoded = head.decode(name, errors="strict")
        except (UnicodeDecodeError, LookupError):
            continue
        scored.append((_printable_score(decoded), name))

    if not scored:
        return EncodingGuess(
            "latin-1",
            "所有候选都严格解码失败，退到 latin-1（永不失败）",
            "low",
            [utf8_error, "内容可能是二进制或已损坏"],
        )

    scored.sort(reverse=True)
    best_score, best_name = scored[0]
    notes = [utf8_error]
    if len(scored) > 1 and scored[1][0] >= best_score - 1e-9:
        notes.append(f"{best_name} 与 {scored[1][1]} 得分相同，无法区分，请人工抽查几行")
        confidence = "low"
    elif best_score > 0.985:
        confidence = "medium"
    else:
        confidence = "low"
    notes.append(f"解码后可打印比例 {best_score:.3f}")
    return EncodingGuess(best_name, "候选编码打分最高", confidence, notes)


def coerce_cp936(name: str) -> str:
    """``cp936`` / ``936`` / ``gbk`` 统一成 Python 认识的规范名。"""
    lowered = name.strip().lower()
    if lowered in ("936", "cp936", "ms936", "gbk", "gb2312", "gb_2312", "windows-936"):
        return "gb18030"
    return lowered


def read_decoded(
    path: str | os.PathLike,
    *,
    encoding: str | None = None,
    limit: int | None = 64 * 1024 * 1024,
) -> DecodedText:
    """读文件并解码，解码失败时用 ``errors="replace"`` 兜底并统计坏字节数。"""
    target = os.fspath(path)
    size = os.path.getsize(target)
    with open(target, "rb") as handle:
        raw = handle.read(limit) if limit else handle.read()
    truncated = bool(limit) and size > len(raw)

    guess = (
        EncodingGuess(coerce_cp936(encoding), "命令行显式指定", "high", [])
        if encoding
        else detect_encoding(raw)
    )

    try:
        text = raw.decode(guess.encoding, errors="strict")
        bad = 0
    except (UnicodeDecodeError, LookupError):
        text = raw.decode(guess.encoding, errors="replace")
        bad = text.count("\ufffd")
        if guess.confidence == "high":
            guess.notes.append("按推断编码严格解码失败，已用替换字符兜底")
            guess.confidence = "medium"

    return DecodedText(
        path=target,
        size=size,
        text=text,
        guess=guess,
        bad_bytes=bad,
        truncated=truncated,
    )


# ---------------------------------------------------------------- 文本体检


def analyze_text(decoded: DecodedText) -> TextFacts:
    raw_facts = TextFacts(
        size=decoded.size,
        encoding=decoded.guess.encoding,
        bom=decoded.guess.is_bom,
    )
    text = decoded.text
    if not text:
        return raw_facts

    raw_facts.lines = text.count("\n") + (0 if text.endswith("\n") else 1)
    raw_facts.crlf = text.count("\r\n")
    raw_facts.lf_only = text.count("\n") - raw_facts.crlf
    raw_facts.cr_only = text.count("\r") - raw_facts.crlf
    raw_facts.ends_with_newline = text.endswith(("\n", "\r"))
    raw_facts.has_null_bytes = "\x00" in text
    raw_facts.mojibake = looks_like_mojibake(text)

    non_ascii = 0
    control = 0
    tabs = 0
    replacement = 0
    trailing = 0
    blank = 0
    longest = 0
    longest_no = 0

    for number, line in enumerate(text.splitlines(), start=1):
        if line.strip() == "":
            blank += 1
        length = len(line)
        if length > longest:
            longest = length
            longest_no = number
        if line != line.rstrip():
            trailing += 1
        tabs += line.count("\t")
        for char in line:
            code = ord(char)
            if code > 0x7F:
                non_ascii += 1
                if char == "\ufffd":
                    replacement += 1
            elif code < 0x20 and char != "\t":
                control += 1

    raw_facts.blank_lines = blank
    raw_facts.max_line_len = longest
    raw_facts.max_line_no = longest_no
    raw_facts.non_ascii_chars = non_ascii
    raw_facts.replacement_chars = replacement + decoded.bad_bytes
    raw_facts.control_chars = control
    raw_facts.tabs = tabs
    raw_facts.trailing_space_lines = trailing
    raw_facts.looks_binary = bool(
        decoded.size and (raw_facts.has_null_bytes or (non_ascii > 0 and control > non_ascii / 4))
    )
    return raw_facts


def looks_like_mojibake(text: str) -> list[str]:
    """已知的乱码指纹（启发式，只用于提示"值得人工看一眼"）。"""
    hits: list[str] = []
    for needle, reason in _MOJIBAKE_SIGNS:
        if needle in text:
            hits.append(f"含 {needle!r}：{reason}")
    return hits


# ---------------------------------------------------------------- 日志诊断


def signature(line: str, *, limit: int = 120) -> str:
    """把一行日志归一化成"模板"，用于把上万条日志聚成几十类。"""
    text = line.strip()
    for pattern, replacement in _SIGNATURE_SUBS:
        text = pattern.sub(replacement, text)
    return text[:limit]


def scan_log(text: str, *, top: int = 12) -> LogScan:
    """统计日志级别分布、错误签名聚类、异常类型与时间范围。"""
    result = LogScan()
    level_counter: Counter[str] = Counter()
    signature_counter: Counter[str] = Counter()
    signature_sample: dict[str, str] = {}
    exception_counter: Counter[str] = Counter()
    suspicious: list[tuple[int, str]] = []

    for number, line in enumerate(text.splitlines(), start=1):
        result.total_lines += 1
        stripped = line.strip()
        if not stripped:
            continue
        result.non_empty_lines += 1
        if len(line) > result.max_line_len:
            result.max_line_len = len(line)
            result.max_line_no = number

        match = _LEVEL_RE.search(line)
        if match:
            level = _LEVEL_NORMALIZE.get(match.group(1).upper(), match.group(1).upper())
            level_counter[level] += 1
        else:
            level_counter["(无级别)"] += 1

        if match and _LEVEL_NORMALIZE.get(match.group(1).upper()) in ("ERROR", "CRITICAL", "FATAL"):
            sig = signature(line)
            signature_counter[sig] += 1
            signature_sample.setdefault(sig, stripped[:200])

        for exc in _EXCEPTION_RE.findall(line):
            exception_counter[exc] += 1

        lower = stripped.lower()
        for needle in _SUSPICIOUS:
            if needle in lower:
                suspicious.append((number, stripped[:200]))
                break

        for timestamp in _TS_RE.finditer(line):
            try:
                moment = datetime.fromisoformat(f"{timestamp.group(1)}T{timestamp.group(2)}")
            except ValueError:
                continue
            if result.first_ts is None or moment < result.first_ts:
                result.first_ts = moment
            if result.last_ts is None or moment > result.last_ts:
                result.last_ts = moment

    result.levels = dict(level_counter)
    result.signatures = [
        (count, signature_sample.get(sig, sig))
        for sig, count in signature_counter.most_common(top)
    ]
    result.exceptions = exception_counter.most_common(top)
    result.suspicious = suspicious[:top]
    return result
