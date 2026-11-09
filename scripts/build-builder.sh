#!/usr/bin/env bash
# ---------------------------------------------------------------------------
#  把开发者构建器打成 CraftLink-Builder.exe（Windows 上跑）
#
#  用法：
#      bash scripts/build-builder.sh                 # 检查环境 + 打包
#      bash scripts/build-builder.sh --check         # 只检查环境
#      bash scripts/build-builder.sh --clean         # 先清掉 build/dist
#
#  产物：
#      installer/builder/dist/CraftLink-Builder.exe
#
#  注意：这一步和客户端打包（scripts/mclink-client.spec）**互不影响**：
#  这个脚本只读 client/**（读版本号与内置图标），不碰它的 dist/。
# ---------------------------------------------------------------------------
set -u

say()  { printf '%s\n' "$*"; }
info() { printf '  %s\n' "$*"; }
ok()   { printf '  ✓ %s\n' "$*"; }
warn() { printf '  ! %s\n' "$*" >&2; }
die()  { printf '\n✗ %s\n' "$*" >&2; exit 1; }

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
BUILDER_DIR="$REPO_ROOT/installer/builder"

CHECK_ONLY=0
CLEAN=0

while [ $# -gt 0 ]; do
    case "$1" in
        --check) CHECK_ONLY=1; shift ;;
        --clean) CLEAN=1; shift ;;
        -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "不认识的参数：$1" ;;
    esac
done

say "=============================================================="
say " CraftLink-Builder 打包（PyInstaller）"
say "=============================================================="
info "仓库根：$REPO_ROOT"

PY=""
for candidate in python3 python py; do
    if command -v "$candidate" >/dev/null 2>&1; then
        PY="$candidate"
        break
    fi
done

[ -n "$PY" ] || die "找不到 python。装 Python 3.8+（Windows 上勾 Add to PATH）后重试。"
ok "python：$($PY -c 'import sys; print(sys.version.split()[0], sys.executable)')"

if ! "$PY" -c "import PyInstaller, sys; print('pyinstaller', PyInstaller.__version__)" 2>/dev/null; then
    die "没装 PyInstaller。装：
    $PY -m pip install --upgrade pyinstaller

（客户端打包 scripts/mclink-client.spec 也用同一个 PyInstaller，装一次两边都能用。）"
fi

ok "$($PY -c "import PyInstaller; print('PyInstaller', PyInstaller.__version__)")"

"$PY" -c "import tkinter" 2>/dev/null && ok "tkinter 可用（GUI 模式）" \
    || warn "tkinter 不可用：打出来的 exe 只能当 CLI 用（--non-interactive）"

SELF_TEST=0
if "$PY" "$REPO_ROOT/installer/tests/builder_selftest.py" >/tmp/craftlink-builder-selftest.log 2>&1; then
    SELF_TEST=1
    ok "Builder 自检通过（installer/tests/builder_selftest.py）"
else
    warn "Builder 自检失败：先修它（日志：/tmp/craftlink-builder-selftest.log）"
    tail -n 20 /tmp/craftlink-builder-selftest.log >&2 || true
fi

if [ "$CHECK_ONLY" = "1" ]; then
    say ""
    say "（--check：只检查环境，不打包）"
    [ "$SELF_TEST" = "1" ] || exit 1
    exit 0
fi

if [ "$CLEAN" = "1" ]; then
    rm -rf "$BUILDER_DIR/build" "$BUILDER_DIR/dist"
    info "已清掉 installer/builder/{build,dist}"
fi

cd "$BUILDER_DIR" || die "进不去 $BUILDER_DIR"

say ""
say "-- 打包 --"
info "cd $BUILDER_DIR && pyinstaller CraftLink-Builder.spec --noconfirm --clean"

if ! "$PY" -m PyInstaller CraftLink-Builder.spec --noconfirm --clean; then
    die "PyInstaller 失败（完整输出见上面）。常见原因：
  * PyInstaller 太老：$PY -m pip install --upgrade pyinstaller
  * 杀软拦了 bootloader：把仓库目录加白名单后重试"
fi

EXE="$BUILDER_DIR/dist/CraftLink-Builder.exe"

say ""
if [ -f "$EXE" ]; then
    SIZE="$(wc -c < "$EXE" | tr -d ' ')"
    ok "产物：$EXE（$SIZE 字节）"
    say ""
    say "下一步："
    info "\"$EXE\"                     # 双击/直接跑 = 图形界面"
    info "\"$EXE\" --non-interactive --json installer\\examples\\builder-input.example.json"
    info "\"$EXE\" --selftest          # 离线自检"
    info "然后： bash scripts/build-installer.sh    （编译 CraftLink-Setup.exe）"
else
    die "没看到产物 $EXE"
fi
