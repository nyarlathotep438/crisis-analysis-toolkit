"""生成演练用的"脏数据"。

应急工具没被演练过就不算存在。这个模块造一批**刻意带病**的文件，
覆盖常见事故：时间空洞、重复时间戳、传感器卡死、缓慢漂移、哨兵值、
GBK 编码、分号分隔、行尾混用、日志里反复报同一个错、代码里的经典坑。

跑完之后 ``ek triage samples`` 应该能把这些病都指出来 ——
如果指不出来，说明工具箱退化需要修，而不是数据变干净了。
"""

# ek:ignore-file CS112
# 上面那行是给 `ek code` 看的豁免：本文件刻意把「已移除的 API」写成字面量，
# 否则检查器会把自己生成的内容当成自己的问题来报。

from __future__ import annotations

import datetime as dt
import os
import random

from . import loader

__all__ = ["generate"]


def generate(out_dir: str | os.PathLike, *, seed: int = 42) -> list[str]:
    """在 ``out_dir`` 下生成演练数据，返回写出的文件列表。"""
    target = os.fspath(out_dir)
    os.makedirs(target, exist_ok=True)
    rng = random.Random(seed)
    written: list[str] = []

    # ---------------------------------------------------------- 1. 传感器数据
    base = dt.datetime(2024, 3, 1, 0, 0, 0)
    rows: list[list] = []
    for minute in range(360):
        if 120 <= minute < 135:  # 一刻钟的数据整段缺失
            continue
        stamp = base + dt.timedelta(minutes=minute)
        value = 50 + 6 * rng.gauss(0, 1) + 0.02 * minute  # 缓慢漂移 + 噪声
        temperature = 21.5 + 0.8 * rng.gauss(0, 1)

        if minute in (40, 200):  # 孤立尖峰
            value += 95
        if minute == 260:  # 哨兵值（缺测标记）
            value = -999.0
        if 280 <= minute < 300:  # 传感器卡死：20 分钟一模一样
            value = 62.5
        if minute == 320:  # 脏值
            rows.append([stamp.strftime("%Y-%m-%d %H:%M:%S"), "N/A", f"{temperature:.3f}"])
            continue
        if minute == 330:  # 重复时间戳
            continue
        rows.append([stamp.strftime("%Y-%m-%d %H:%M:%S"), f"{value:.3f}", f"{temperature:.3f}"])
    # 补一行与上一行完全相同的时间戳，制造时间重复
    rows.append(list(rows[-1]))

    sensor_path = os.path.join(target, "sensor.csv")
    loader.write_csv(
        sensor_path,
        ["timestamp", "value", "temperature"],
        rows,
        encoding="utf-8",
    )
    written.append(sensor_path)

    # ---------------------------------------------------------- 2. GBK + 分号
    messy_path = os.path.join(target, "lab_measurements_gbk.csv")
    messy_rows = [
        ["样本编号", " 浓度(ppm) ", "备注", "浓度(ppm)"],  # 列名带空格 + 重复列名
        ["S001", "12.4", "正常", "12.4"],
        ["S002", "", "缺测", "x"],
        ["S003", "13.8 ", "轻微偏高 ", "13.8"],
        ["S004", "-999", "仪器异常", "-999"],
        ["S004", "-999", "仪器异常", "-999"],  # 整行重复
        ["S005", "1,204.5", "千分位", "1204.5"],  # 千分位数字
    ]
    with open(messy_path, "w", encoding="gbk", newline="") as handle:
        for row in messy_rows:
            handle.write(";".join(row) + "\r\n")
    written.append(messy_path)

    # ---------------------------------------------------------- 3. 日志
    log_path = os.path.join(target, "app.log")
    levels = ["INFO", "INFO", "INFO", "DEBUG", "WARN", "INFO", "ERROR"]
    messages = [
        "读取配置完成 path=C:\\lab\\config.yaml",
        "开始处理批次 batch_id=%(batch)s rows=%(rows)d",
        "写出结果到 output/result_%(batch)s.csv",
        "数据校验通过，共 %(rows)d 行",
        "缓存命中率 %(rate)d%%",
        "磁盘剩余空间偏低: 812 MB",
        "批次 %(batch)s 处理失败: KeyError('value')",
    ]
    log_lines = []
    stamp = base
    for index in range(240):
        stamp += dt.timedelta(seconds=17)
        level = levels[index % len(levels)]
        message = messages[index % len(messages)]
        message = message % {"batch": 1000 + index, "rows": 500 + index % 37, "rate": rng.randint(61, 98)}
        log_lines.append(f"{stamp.strftime('%Y-%m-%d %H:%M:%S')} {level:<5} {message}")
        if index == 150:
            log_lines.append(
                "Traceback (most recent call last):\n"
                '  File "pipeline.py", line 88, in run\n'
                "    frame = pd.read_csv(path)\n"
                "UnicodeDecodeError: 'utf-8' codec can't decode byte 0xd6 in position 12: invalid continuation byte"
            )
        if index == 220:
            log_lines.append("OSError: [Errno 28] No space left on device")
    with open(log_path, "w", encoding="utf-8", newline="") as handle:
        # 前 200 多行用 CRLF，尾部 12 行用 LF，且末尾不写换行 —— 制造真实的"行尾混用"
        handle.write("\r\n".join(log_lines))
        handle.write("\r\n")
        handle.write("\n".join(log_lines[:12]))
    written.append(log_path)

    # ---------------------------------------------------------- 4. 有病的小脚本
    code_path = os.path.join(target, "bad_pipeline.py")
    code = '''"""一个把教科书里所有坏习惯都犯了一遍的脚本（仅用于演练检查器）。"""
import os
import pickle
import urllib.request

DATA_DIR = "C:\\\\lab\\\\data"


def load(path, cache={}):
    try:
        with urllib.request.urlopen("http://example.com/data.csv") as response:
            raw = response.read()
    except Exception:
        pass

    frame = pickle.load(open(path, "rb"))
    text = open("C:\\\\lab\\\\config.txt")
    cache[path] = frame
    assert frame is not None
    for row in frame:
        print(f"处理 {row}")
    return frame


def summarise():
    try:
        import pandas as pd
    except:
        print("没有 pandas")
    frame.append({"a": 1})
    frame.ix[0:5]
    import json as json2
    eval("1+1")
    return None


if __name__ == "__main__":
    load(os.path.join(DATA_DIR, "x.csv"))
'''
    with open(code_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(code)
    written.append(code_path)

    # ---------------------------------------------------------- 5. 单列纯文本
    series_path = os.path.join(target, "values.txt")
    with open(series_path, "w", encoding="utf-8") as handle:
        for index in range(200):
            handle.write(f"{100 + 5 * rng.gauss(0, 1):.4f}\n")
    written.append(series_path)

    # ---------------------------------------------------------- 6. 说明
    readme_path = os.path.join(target, "README.txt")
    with open(readme_path, "w", encoding="utf-8") as handle:
        handle.write(
            "这批文件由 `ek demo` 生成，专门用来演练诊断工具。\n"
            "已知会埋进去的问题：\n"
            "  sensor.csv         时间空洞 15 分钟、重复时间戳、孤立尖峰、-999 哨兵值、\n"
            "                     传感器卡死 20 分钟、脏值 N/A、缓慢漂移\n"
            "  lab_measurements_gbk.csv  GBK 编码、分号分隔、列名带空格、重复列名、\n"
            "                     重复行、千分位数字、缺测\n"
            "  app.log            同一错误反复出现、UnicodeDecodeError、磁盘写满、\n"
            "                     行尾混用、文件末尾无换行\n"
            "  bad_pipeline.py    裸 except、空默认参数、open 缺 encoding、eval、\n"
            "                     已移除的 API、硬编码绝对路径、网络调用\n"
            "  values.txt         单列纯文本（没有时间列）\n"
        )
    written.append(readme_path)

    return written
