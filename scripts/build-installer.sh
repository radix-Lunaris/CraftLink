#!/usr/bin/env bash
# ---------------------------------------------------------------------------
#  CraftLink -- 一条命令编译出面向用户的安装器（Windows 安装器 / M7）
#
#  用法（在 Windows 的 Git Bash / MSYS2 里跑；WSL 也支持但见下面的说明）：
#
#      bash scripts/build-installer.sh
#      bash scripts/build-installer.sh --params installer/build/params.iss
#      bash scripts/build-installer.sh --iscc "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
#      bash scripts/build-installer.sh --dry-run          # 只检查 + 打印命令
#
#  它做的事（顺序固定，任一步失败就停下并说清楚原因）：
#
#      1. 找 python（校验脚本要用）；
#      2. 检查 Builder 的产物 installer/build/params.iss 在不在
#         （不在就告诉你先跑 CraftLink-Builder）；
#      3. 跑 scripts/verify-installer-inputs.py --require-exe
#         （内容断言：branding.json / 参数 / 图标 / 与客户端常量一致性 / 主程序在不在）；
#      4. 找 ISCC.exe（--iscc > $ISCC > PATH > 标准安装位置），并检查版本 >= 6.3；
#      5. 调 ISCC.exe 编译 installer/CraftLink.iss；
#      6. 报产物路径 + 大小 + sha256 + 静默安装示例。
#
#  重要：Inno Setup 是 Windows 程序，**本脚本不能在纯 Linux 上完成编译**。
#  在 Linux 上跑它只会在第 4 步给出"怎么装 Inno Setup"的明确指引。
#
#  WSL 说明：WSL 里可以调用 Windows 的 ISCC.exe（互操作），脚本会用 wslpath 把
#  路径转成 Windows 形式。但仓库若在 WSL 文件系统里，ISCC 需要通过 \\wsl$ 访问，
#  更稳的做法是在 Windows 侧（Git Bash）编译，或把仓库放到 Windows 盘上。
# ---------------------------------------------------------------------------
set -u

# ------------------------------------------------------------------ 小工具

say()  { printf '%s\n' "$*"; }
info() { printf '  %s\n' "$*"; }
ok()   { printf '  ✓ %s\n' "$*"; }
warn() { printf '  ! %s\n' "$*" >&2; }
die()  { printf '\n✗ %s\n' "$*" >&2; exit 1; }

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
INSTALLER_DIR="$REPO_ROOT/installer"
ISS="$INSTALLER_DIR/CraftLink.iss"

PARAMS=""
OUT_OVERRIDE=""
NAME_OVERRIDE=""
ISCC_ARG=""
ALLOW_OLD=0
DRY_RUN=0
SKIP_VERIFY=0
VERBOSE=0

usage() {
    sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
}

while [ $# -gt 0 ]; do
    case "$1" in
        --params)      PARAMS="${2:-}"; shift 2 ;;
        --params=*)    PARAMS="${1#*=}"; shift ;;
        --out)         OUT_OVERRIDE="${2:-}"; shift 2 ;;
        --out=*)       OUT_OVERRIDE="${1#*=}"; shift ;;
        --name)        NAME_OVERRIDE="${2:-}"; shift 2 ;;
        --name=*)      NAME_OVERRIDE="${1#*=}"; shift ;;
        --iscc)        ISCC_ARG="${2:-}"; shift 2 ;;
        --iscc=*)      ISCC_ARG="${1#*=}"; shift ;;
        --allow-old-iscc) ALLOW_OLD=1; shift ;;
        --dry-run)     DRY_RUN=1; shift ;;
        --skip-verify) SKIP_VERIFY=1; shift ;;
        --verbose|-v)  VERBOSE=1; shift ;;
        -q|--quiet)    VERBOSE=0; shift ;;
        -h|--help)     usage ;;
        *)             die "不认识的参数：$1（--help 看用法）" ;;
    esac
done

[ -z "$PARAMS" ] && PARAMS="$INSTALLER_DIR/build/params.iss"

say "=============================================================="
say " CraftLink 安装器编译（Inno Setup）"
say "=============================================================="
info "仓库根：$REPO_ROOT"
info "参数文件：$PARAMS"

# --------------------------------------------------------------- 1. python

PY=""
for candidate in python3 python py; do
    if command -v "$candidate" >/dev/null 2>&1; then
        if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
            PY="$candidate"
            break
        fi
    fi
done

if [ -z "$PY" ]; then
    warn "找不到 python 3.8+：校验步骤会跳过（安装 Windows 版 Python 时记得勾 Add to PATH）"
else
    ok "python：$($PY -c 'import sys; print(sys.version.split()[0], sys.executable)')"
fi

# ------------------------------------------------------- 2. Builder 产物

if [ ! -f "$PARAMS" ]; then
    say ""
    die "找不到 Builder 生成的参数文件：$PARAMS

先跑开发者构建器（它负责生成 branding.json / logo / 这份参数）：

    installer\\builder\\dist\\CraftLink-Builder.exe            # 图形界面
    installer\\builder\\dist\\CraftLink-Builder.exe --non-interactive --json installer\\examples\\builder-input.example.json

（还没打包 Builder？先在 Windows 上跑： bash scripts/build-builder.sh）"
fi

PARAMS_DIR="$(cd "$(dirname "$PARAMS")" && pwd)"
PARAMS="$PARAMS_DIR/$(basename "$PARAMS")"
ok "参数文件存在（输出目录：$PARAMS_DIR）"

# 模板固定 #include "build\params.iss"：把参数放到那个位置（同一个文件就不动）。
mkdir -p "$INSTALLER_DIR/build"
if [ "$PARAMS" != "$INSTALLER_DIR/build/params.iss" ]; then
    cp -f "$PARAMS" "$INSTALLER_DIR/build/params.iss" \
        || die "无法把参数复制到 $INSTALLER_DIR/build/params.iss"
    info "已把参数同步到 installer/build/params.iss（模板固定读这个位置）"
fi

read_define() {
    # 从 params.iss 里读一个 #define 的字符串值（找不到就输出空）。
    sed -n "s/^#define $1 \"\(.*\)\"[[:space:]]*$/\1/p" \
        "$INSTALLER_DIR/build/params.iss" | head -n 1 | tr -d '\r'
}

OUTPUT_DIR_DEFINE="$(read_define OutputDirLine)"
OUTPUT_NAME="$(read_define OutputBaseFilename)"
APP_NAME_DEFINE="$(read_define AppName)"
[ -z "$OUTPUT_NAME" ] && OUTPUT_NAME="CraftLink-Setup"
[ -z "$OUTPUT_DIR_DEFINE" ] && OUTPUT_DIR_DEFINE="build\\out"

if [ -n "$NAME_OVERRIDE" ]; then
    OUTPUT_NAME="$NAME_OVERRIDE"
fi

# ------------------------------------------------------------- 3. 校验

if [ "$SKIP_VERIFY" = "1" ] || [ -z "$PY" ]; then
    [ "$SKIP_VERIFY" = "1" ] && warn "按要求跳过输入校验（--skip-verify）"
else
    say ""
    say "-- 校验安装器输入（内容断言）--"

    if "$PY" "$HERE/verify-installer-inputs.py" --out "$PARAMS_DIR" --require-exe; then
        ok "输入校验通过"
    else
        die "输入校验失败：先把上面 ✗ 的问题解决（大多数是主程序没构建：pyinstaller scripts/mclink-client.spec）"
    fi
fi

# ------------------------------------------------------------- 4. ISCC

find_iscc() {
    if [ -n "$ISCC_ARG" ]; then
        if [ -f "$ISCC_ARG" ]; then
            printf '%s\n' "$ISCC_ARG"
            return 0
        fi

        printf '\n✗ --iscc 指向的文件不存在：%s\n' "$ISCC_ARG" >&2

        return 2
    fi

    if [ -n "${ISCC:-}" ] && [ -f "${ISCC}" ]; then
        printf '%s\n' "${ISCC}"
        return 0
    fi

    if command -v ISCC.exe >/dev/null 2>&1; then
        command -v ISCC.exe
        return 0
    fi

    if command -v iscc >/dev/null 2>&1; then
        command -v iscc
        return 0
    fi

    # 常见安装位置：
    #   * Git Bash/MSYS：$ProgramFiles(x86) 之类是 Windows 形式，用 cygpath 转一下；
    #   * WSL：Windows 盘在 /mnt/c 下（也兜一下 /c/... 这种 MSYS 写法）。
    local pf86 pf64 pf_any lad
    pf86="$(printenv 'ProgramFiles(x86)' 2>/dev/null || true)"
    pf64="$(printenv 'ProgramFiles' 2>/dev/null || true)"
    [ -z "$pf64" ] && pf64="$(printenv 'PROGRAMFILES' 2>/dev/null || true)"
    [ -z "$pf86" ] && pf86="$(printenv 'PROGRAMFILES(X86)' 2>/dev/null || true)"
    pf_any="$(printenv 'ProgramW6432' 2>/dev/null || true)"
    lad="$(printenv 'LOCALAPPDATA' 2>/dev/null || true)"

    if command -v cygpath >/dev/null 2>&1; then
        [ -n "$pf86" ] && pf86="$(cygpath -u "$pf86" 2>/dev/null || printf '%s' "$pf86")"
        [ -n "$pf64" ] && pf64="$(cygpath -u "$pf64" 2>/dev/null || printf '%s' "$pf64")"
        [ -n "$pf_any" ] && pf_any="$(cygpath -u "$pf_any" 2>/dev/null || printf '%s' "$pf_any")"
        [ -n "$lad" ] && lad="$(cygpath -u "$lad" 2>/dev/null || printf '%s' "$lad")"
    fi

    local candidate
    for candidate in \
        "$pf86/Inno Setup 6" \
        "$pf64/Inno Setup 6" \
        "$pf_any/Inno Setup 6" \
        "$lad/Programs/Inno Setup 6" \
        "$HOME/AppData/Local/Programs/Inno Setup 6" \
        "/c/Program Files (x86)/Inno Setup 6" \
        "/c/Program Files/Inno Setup 6" \
        "/mnt/c/Program Files (x86)/Inno Setup 6" \
        "/mnt/c/Program Files/Inno Setup 6" \
    ; do
        case "$candidate" in
            "/Inno Setup 6") continue ;;      # 环境变量为空时剩下的壳
        esac

        if [ -f "$candidate/ISCC.exe" ]; then
            printf '%s\n' "$candidate/ISCC.exe"
            return 0
        fi
    done

    return 1
}

say ""
say "-- 找 Inno Setup 编译器 --"

ISCC="$(find_iscc || true)"

if [ -z "$ISCC" ]; then
    say ""
    die "找不到 ISCC.exe（Inno Setup 6 的命令行编译器）。装它有三种办法：

  1) winget（Windows 10/11 自带）：
       winget install --id JRSoftware.InnoSetup -e

  2) 官网下载安装（选 Inno Setup 6.3 或更新）：
       https://jrsoftware.org/isdl.php

  3) 装在非默认位置：用 --iscc 指给我：
       bash scripts/build-installer.sh --iscc \"D:\\Tools\\Inno Setup 6\\ISCC.exe\"

装完不用重启：脚本会自动在
  %ProgramFiles(x86)%\\Inno Setup 6\\ISCC.exe
  %LOCALAPPDATA%\\Programs\\Inno Setup 6\\ISCC.exe
这些位置找。"
fi

ok "ISCC：$ISCC"

# --- 版本检查（>= 6.3：模板用到 SaveStringsToUTF8FileWithoutBOM）---
ISCC_VERSION=""

if command -v powershell.exe >/dev/null 2>&1; then
    ISCC_VERSION="$(powershell.exe -NoProfile -Command \
        "(Get-Item -LiteralPath '$ISCC').VersionInfo.ProductVersion" 2>/dev/null | tr -d '\r' | head -n 1)"
fi

if [ -z "$ISCC_VERSION" ] && command -v reg.exe >/dev/null 2>&1; then
    ISCC_VERSION="$(reg.exe query "HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\Inno Setup 6_is1" /v DisplayVersion 2>/dev/null \
        | sed -n 's/.*REG_SZ[[:space:]]*//p' | tr -d '\r' | head -n 1)"
fi

if [ -n "$ISCC_VERSION" ]; then
    info "ISCC 版本：$ISCC_VERSION"
    MAJOR="${ISCC_VERSION%%.*}"
    REST="${ISCC_VERSION#*.}"
    MINOR="${REST%%.*}"

    if [ "${MAJOR:-0}" -lt 6 ] 2>/dev/null || { [ "${MAJOR:-0}" -eq 6 ] && [ "${MINOR:-0}" -lt 3 ] 2>/dev/null; }; then
        if [ "$ALLOW_OLD" = "1" ]; then
            warn "Inno Setup $ISCC_VERSION < 6.3：模板需要 SaveStringsToUTF8FileWithoutBOM()，编译很可能报 Unknown identifier"
        else
            die "Inno Setup $ISCC_VERSION 太老（需要 6.3+，模板用它写 UTF-8 无 BOM 的指针文件）。
   升级： winget upgrade --id JRSoftware.InnoSetup -e
   确实要用老版本：加 --allow-old-iscc（大概率编译不过，见 docs/INSTALLER.md FAQ）"
        fi
    fi
else
    warn "读不到 ISCC 版本（没有 powershell/reg）。模板需要 Inno Setup 6.3+，若编译报 Unknown identifier 请升级。"
fi

# --- 语言文件存在性检查（用 --language 时最容易忘的一步）---
LANGUAGE_LINE="$(read_define LanguageLine)"

if [ -n "$LANGUAGE_LINE" ]; then
    case "$LANGUAGE_LINE" in
        compiler:*)
            ISCC_DIR="$(dirname "$ISCC")"
            LANG_FILE="$ISCC_DIR/${LANGUAGE_LINE#compiler:}"
            LANG_FILE="${LANG_FILE//\\//}"

            if [ -f "$LANG_FILE" ]; then
                ok "语言文件存在：$LANG_FILE"
            else
                die "参数里指定的 Inno 语言文件不存在：$LANG_FILE

Inno Setup 官方**不带**简体中文语言文件（只有二十来种官方语言）。两种做法：
  A) 继续用英文安装界面：重新跑 Builder 时不加 --language-file；
  B) 放一个中文 .isl 到 $ISCC_DIR\\Languages\\ 下
     （例如 ChineseSimplified.isl，来源见 docs/INSTALLER.md FAQ），
     然后重新跑 Builder：--language-file \"compiler:Languages\\ChineseSimplified.isl\" --language-name chinesesimplified"
            fi
            ;;
        *)
            [ -f "$LANGUAGE_LINE" ] && ok "语言文件存在：$LANGUAGE_LINE" \
                || die "语言文件不存在：$LANGUAGE_LINE"
            ;;
    esac
fi

# --------------------------------------------------------- 5. 调 ISCC

cd "$INSTALLER_DIR" || die "进不去 $INSTALLER_DIR"

ISCC_ARGS=""
[ "$VERBOSE" = "0" ] && ISCC_ARGS="$ISCC_ARGS /Qp"

OUT_FOR_CHECK="$INSTALLER_DIR/${OUTPUT_DIR_DEFINE//\\//}/$OUTPUT_NAME.exe"

if [ -n "$OUT_OVERRIDE" ]; then
    ISCC_ARGS="$ISCC_ARGS \"/O$OUT_OVERRIDE\""
    OUT_FOR_CHECK="$OUT_OVERRIDE/$OUTPUT_NAME.exe"
fi

if [ -n "$NAME_OVERRIDE" ]; then
    ISCC_ARGS="$ISCC_ARGS \"/F$NAME_OVERRIDE\""
fi

say ""
say "-- 编译 --"

# 调用方式：
#   * Windows / Git Bash：直接用找到的路径（MSYS 自己会做参数路径转换）；
#   * WSL 且 ISCC 在 Windows 盘上（/mnt/c/...）：**用 Linux 路径启动**（WSL 互操作
#     只认 Linux 路径），但传给它的参数必须是 Windows 形式 -> wslpath -w；
#     同时把工作目录换到 /mnt/c，避免 Windows 程序拿到 UNC 工作目录。
RUN_ISCC="$ISCC"
RUN_ISS="$ISS"
NEED_WIN_ARGS=0

case "$ISCC" in
    /mnt/*) NEED_WIN_ARGS=1 ;;
esac

if [ "$NEED_WIN_ARGS" = "1" ]; then
    if command -v wslpath >/dev/null 2>&1; then
        warn "ISCC 在 Windows 盘上（WSL 环境）：参数会自动转成 Windows 路径。"
        warn "更稳的做法是在 Windows 侧用 Git Bash 跑同一个脚本（见 docs/INSTALLER.md）。"
        RUN_ISS="$(wslpath -w "$ISS")"

        if [ -n "$OUT_OVERRIDE" ]; then
            ISCC_ARGS="$ISCC_ARGS \"/O$(wslpath -w "$OUT_OVERRIDE")\""
        fi

        cd /mnt/c 2>/dev/null || true
    else
        die "在 WSL 里发现了 Windows 的 ISCC.exe，但找不到 wslpath（无法转换路径）。
请在 Windows 侧的 Git Bash 里跑： bash scripts/build-installer.sh"
    fi
fi

# shellcheck disable=SC2086
CMD="\"$RUN_ISCC\"$ISCC_ARGS \"$RUN_ISS\""
info "$CMD"

if [ "$DRY_RUN" = "1" ]; then
    say ""
    say "（--dry-run：只检查，不编译）"
    exit 0
fi

# shellcheck disable=SC2086
if ! eval "$CMD"; then
    die "ISCC 编译失败。常见原因：

  * 【ISPP 报 'Could not open file build\\params.iss'】参数文件没同步 —— 用 --params 指定 Builder 的输出，或直接跑 Builder 生成默认位置；
  * 【Unknown identifier 'SaveStringsToUTF8FileWithoutBOM'】Inno Setup < 6.3 —— 升级；
  * 【Could not read messages file】语言文件不存在 —— 见上面的提示，或不用 --language-file；
  * 【Source file ... does not exist】staging 里的文件没了 —— 重新跑 Builder，再跑一次本脚本。
完整 FAQ： docs/INSTALLER.md"
fi

say ""
say "-- 编译完成 --"

if [ -f "$OUT_FOR_CHECK" ]; then
    SIZE="$(wc -c < "$OUT_FOR_CHECK" | tr -d ' ')"
    ok "产物：$OUT_FOR_CHECK（$SIZE 字节）"

    if [ -n "$PY" ]; then
        HASH="$("$PY" -c 'import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$OUT_FOR_CHECK" 2>/dev/null || true)"
        [ -n "$HASH" ] && info "sha256：$HASH"
    fi

    say ""
    say "静默安装示例（批量部署用）："
    info "\"$OUT_FOR_CHECK\" /VERYSILENT /NORESTART /DIR=\"D:\\Apps\\CraftLink\" /DATA_DIR=\"D:\\CraftLinkData\" /LOG=\"%TEMP%\\craftlink-install.log\""
    info "\"$OUT_FOR_CHECK\" /SILENT /TASKS=\"desktopicon\""
    say ""
    say "发给用户前请在 Windows 上过一遍 docs/INSTALLER.md 的「验收清单」（安装/升级/卸载/静默）。"
else
    warn "ISCC 返回成功，但没在预期位置找到产物：$OUT_FOR_CHECK（检查 params.iss 里的 OutputDir/OutputBaseFilename）"
fi
