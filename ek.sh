#!/usr/bin/env sh
# ============================================================
#  EMERGENCYKITS 启动器（Linux / macOS）
#  用法：  sh ek.sh profile data.csv
#          sh ek.sh triage  samples
#          sh ek.sh selftest
#  说明：  只把本目录追加到 PYTHONPATH，不切换工作目录。
# ============================================================
set -u

DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

PY=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        PY="$candidate"
        break
    fi
done

if [ -z "$PY" ]; then
    echo "[错误] 找不到 python3 / python，请确认已安装 Python 3.9+" >&2
    exit 3
fi

PYTHONPATH="$DIR${PYTHONPATH:+:$PYTHONPATH}" exec "$PY" -m ekits "$@"
