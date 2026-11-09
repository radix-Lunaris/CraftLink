#!/usr/bin/env bash
# ---------------------------------------------------------------------------
#  校验"安装器输入"的内容（薄壳，真正干活的是同名 .py）
#
#  用法：
#      bash scripts/verify-installer-inputs.sh                 # 校验 installer/build
#      bash scripts/verify-installer-inputs.sh --require-exe   # 主程序必须已构建
#      bash scripts/verify-installer-inputs.sh --out <目录> --json
#
#  为什么要有这个 .sh：Windows 侧（Git Bash / MSYS）习惯用 .sh 入口，而校验
#  逻辑必须是跨平台的 Python。两条入口都指向同一份实现，不会出现"两套标准"。
# ---------------------------------------------------------------------------
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
PY_SCRIPT="$HERE/verify-installer-inputs.py"

if [ ! -f "$PY_SCRIPT" ]; then
    echo "✗ 找不到 $PY_SCRIPT" >&2
    exit 2
fi

PY=""

for candidate in python3 python py; do
    if command -v "$candidate" >/dev/null 2>&1; then
        PY="$candidate"
        break
    fi
done

if [ -z "$PY" ]; then
    echo "✗ 找不到 python（校验脚本需要 Python 3.8+；Windows 上装 Python 时勾选 Add to PATH）" >&2
    exit 2
fi

exec "$PY" "$PY_SCRIPT" "$@"
