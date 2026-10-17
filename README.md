# Emergency kits

An emergency toolkit for rapidly identifying data issues, currently under development...
用于快速查找数据问题的应急工具箱，目前正在搭建中…

---

## 这是什么

一套**断网可用**的诊断工具箱：给断网环境下的数据、代码、系统问题做「急诊分诊」。

三条底线决定了它的形态：

| 原则 | 含义 |
| --- | --- |
| **零依赖内核** | 只装 Python 就能跑。断网、换机器、虚拟环境损坏、conda 崩掉时它仍然能起来 |
| **可选增强** | 检测到 numpy / pandas / matplotlib 就自动用更快的路径和出图；检测不到就退化为纯 Python / 纯文本 |
| **只读默认** | 所有诊断默认不改动你的文件、不写临时垃圾、不访问网络。写文件必须显式 `--out` / `--to` / `--plot` |

## 30 秒上手

```bat
:: Windows
ek.cmd selftest            :: 先确认工具箱自己是好的（11 项自检）
ek.cmd demo                :: 生成一批「带病」的演练数据到 samples\
ek.cmd triage samples      :: 一键分诊：自动判断类型、跑完所有相关检查、给处置建议
```

```bash
# Linux / macOS
sh ek.sh selftest
sh ek.sh demo
sh ek.sh triage samples
```

不想用启动器也可以（效果完全一样）：

```bat
python -m ekits triage 你的数据.csv
python scripts/triage.py 你的数据.csv
```

## 命令一览

| 命令 | 一句话 | 典型用法 |
| --- | --- | --- |
| `triage` | **应急第一条命令**：自动识别文件类型并跑完相关检查 | `ek triage D:\labdata` |
| `profile` | 结构速览：行列、类型、缺失、分位数、文本直方图 | `ek profile data.csv --hist` |
| `quality` | 数据体检：编码、行尾、缺失、重复、哨兵值、主键 | `ek quality data.csv --key id` |
| `anomaly` | 异常点扫描：6 种方法投票，按可疑程度排序 | `ek anomaly data.csv --column value` |
| `ts` | 时序诊断：采样间隔、断档、卡死、漂移、周期 | `ek ts data.csv --time ts --value val` |
| `plot` | 出图：折线 / 直方图 / 缺失率（无界面，只写文件） | `ek plot data.csv --column val` |
| `log` | 日志诊断：级别分布、错误签名聚类、危险关键词 | `ek log app.log --grep ERROR` |
| `encoding` | 编码与文本体检；可转码 | `ek encoding a.csv --hex` |
| `env` | 环境快照与问题判断：解释器、PATH、编码、磁盘、代理 | `ek env` |
| `fs` | 目录体检：空间分布、零字节、重复内容、路径问题 | `ek fs D:\labdata` |
| `code` | 代码静态体检：语法、反模式、已移除 API、依赖可用性 | `ek code src\` |
| `selftest` | 离线自检：确认是工具坏了还是数据坏了 | `ek selftest` |
| `demo` | 生成「带病」演练数据 | `ek demo --out-dir samples` |

## 目录结构

```
EMERGENCYKITS/
├── ek.cmd / ek.sh          # 启动器（不切换工作目录，只加 PYTHONPATH）
├── ekits/                  # 工具箱内核（19 个模块，纯标准库）
│   ├── cli.py              #   命令行入口与 13 个子命令
│   ├── report.py           #   统一报告容器（终端 / JSON / Markdown 同源）
│   ├── tables.py fmt.py    #   中文宽度对齐的表格、纯文本 sparkline
│   ├── loader.py           #   读 csv/tsv/txt/json/jsonl（xlsx/parquet 走 pandas 可选路径）
│   ├── profile.py quality.py anomaly.py timeseries.py plots.py
│   ├── textio.py           #   编码探测、文本体检、日志签名聚类
│   ├── envreport.py files.py codescan.py
│   └── selftest.py demo.py
├── scripts/                # 13 个薄壳脚本，可以单独运行
└── docs/                   # 详细文档（建议按顺序读）
```

## 文档索引

| 文档 | 内容 |
| --- | --- |
| [00-快速开始](docs/00-快速开始.md) | 第一次使用、三种调用方式、常见问题 |
| [01-工具清单与决策树](docs/01-工具清单与决策树.md) | 什么时候用哪条命令、规则编号索引 |
| [02-设计说明与约定](docs/02-设计说明与约定.md) | 为什么这么设计、输出格式与退出码约定 |
| [03-数据诊断手册](docs/03-数据诊断手册.md) | 按症状查：乱码、行数变多、缺失、异常、时间轴 |
| [04-代码诊断手册](docs/04-代码诊断手册.md) | 报错、依赖缺失、notebook 跑不通 |
| [05-系统诊断手册](docs/05-系统诊断手册.md) | 磁盘、权限、PATH、编码、路径过长 |
| [06-离线应急流程](docs/06-离线应急流程.md) | 事前准备、事中五步、事后归档（含一页纸速查） |
| [07-本次工作说明](docs/07-本次工作说明.md) | **做了什么、为什么这么做、验证到什么程度、还有什么没做** |

## 退出码

可以直接拿来做串联判断（`ek quality x.csv && 后续步骤`）：

| 码 | 含义 |
| --- | --- |
| `0` | 没发现问题 |
| `1` | 发现警告（数据能用但结论不可信） |
| `2` | 发现严重问题（数据/代码已不可用） |
| `3` | 工具自身或用法出错（参数写错、文件读不了） |

## 它不做什么

明确边界，免得在关键时刻指望错东西：

- **不改数据**：不填充缺失、不去重、不修异常。它只报告现场，清理由你决定。
- **不联网**：没有 ping、没有 DNS、没有 pip index 访问。断网与否行为一致。
- **不做统计推断**：不跑假设检验、不建模。异常检测是启发式的，结论需要人判断。
- **不替代专业工具**：系统级深度排查仍需 `pytest` / `py-spy` / `perf` / 事件查看器。
  这里是「在什么都没有的时候，先看清现场」的那一层。
