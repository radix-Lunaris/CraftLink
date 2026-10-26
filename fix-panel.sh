#!/bin/bash
# 兼容旧面板修复入口，统一使用有实时日志和失败检查的主重建流程。
# 用法：bash /opt/mc-link/fix-panel.sh [--strict] [-v]
set -u
for argument in "$@"; do
    case "$argument" in
        --strict|-v|--verbose|--prebuilt-panel) ;;
        *) echo "未知参数：$argument（此入口只重建面板）" >&2; exit 2 ;;
    esac
done
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd) || exit 1
if [ ! -f "$SCRIPT_DIR/rebuild.sh" ]; then
    echo "缺少 $SCRIPT_DIR/rebuild.sh，请上传完整部署包。" >&2
    exit 1
fi
exec bash "$SCRIPT_DIR/rebuild.sh" panel "$@"
