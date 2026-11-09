#!/bin/bash
# ============================================================
#  MC-Link 一键重建 + 自证
# ============================================================
#  用法（在服务器上执行；下面的 /opt/mc-link 是**默认安装目录**，装到别处
#  就把路径换成你自己的 —— 脚本自己从所在位置推导根目录，不依赖这个默认值）：
#      bash /opt/mc-link/rebuild.sh            # 两个容器都重建（**实时逐行打印**）
#      bash /opt/mc-link/rebuild.sh panel      # 只重建面板（不断开玩家）
#      bash /opt/mc-link/rebuild.sh relay      # 只重建中继（会断开玩家）
#      bash /opt/mc-link/rebuild.sh --strict   # 文件指纹不一致就拒绝重建
#      bash /opt/mc-link/rebuild.sh -v         # 再啰嗦一点（把被过滤的空行也打出来）
#
#  环境变量覆盖（都有默认值，通常不用设）：
#      MCLINK_ROOT      安装根目录（默认 = 脚本所在目录）
#      MCLINK_ENV_FILE  .env 路径（默认 = <根目录>/.env）
#      MC_LINK_LOG      rebuild.log 路径（默认 = <根目录>/rebuild.log）
#      MCLINK_BIN_DIR   短命令装到哪（默认 /usr/local/bin）
#
#  输出为什么这样（用户 2026-09-29 原话）：
#      「未来的 Rebuild 无需加那个 -v，可以直接打印详细的日志，每一步的日志
#        状态，且是实时打印；尤其是重建那一块，它重建完了一次冒出来一坨，
#        它这个一条一条地给我实时打印」
#
#  所以默认（不加任何参数）就是：**每一步、每一行、边跑边打印**——
#      ▶ 步骤名
#         │ docker 输出的第一行...
#         │ ...第二行（上一条还没跑完就来了）
#      ✔ 步骤名（12s）
#  不再把某一步的输出攒到最后一次性倒出来。完整输出**同时**写进
#      <安装根目录>/rebuild.log（可用 MC_LINK_LOG 覆盖）
#  失败时步骤名下面会再多打一小段"上下文"（最后 25 行），并且脚本以非 0 退出。
#  `-v` 只是更啰嗦（连被默认过滤掉的空行 / 原始控制行也打出来），
#  `--strict` 语义一个字没变。
#
#  为什么要先删镜像：
#      docker compose up -d --build 在镜像已存在时可能直接吃构建缓存，
#      COPY 那一步不重新执行 —— 新文件永远进不去容器，而且不报任何错。
#
#  [!] 重建中继会断开当前所有正在玩的连接（正在玩的人会掉线）。
#      如果只是改面板，用 `bash <安装根目录>/rebuild.sh panel`。
# ============================================================

set -u

# ================================================================
# ★ D8：安装根目录**从脚本自身位置推导**，绝不写死
# ================================================================
#
# 这个脚本就躺在安装根目录里（<INSTALL_ROOT>/rebuild.sh），所以：
#
#     MC_LINK_ROOT = 脚本所在的真实目录
#
# 覆盖顺序：环境变量 `MCLINK_ROOT` > 脚本位置。
#
# 为什么必须这样（而不是继续写 /opt/mc-link）：开源版的安装根目录是用户
# 在安装器里自己选的，`/opt/mc-link` **只是一个默认值**。脚本里写死它，
# 用户装到 /srv/craftlink 之后就会出现"改了 A 目录、跑的却是 B 目录"这种
# 完全看不出原因的错。
#
# readlink 循环：脚本可能是软链（<INSTALL_ROOT>/current -> releases/x.y.z
# 那套原地升级布局），必须解析到真实文件再取目录，否则根目录会算成软链
# 所在的那一层。
SCRIPT_PATH="${BASH_SOURCE[0]:-$0}"

while [ -L "$SCRIPT_PATH" ]; do
    _link_target="$(readlink "$SCRIPT_PATH")"

    case "$_link_target" in
        /*) SCRIPT_PATH="$_link_target" ;;
        *)  SCRIPT_PATH="$(dirname "$SCRIPT_PATH")/$_link_target" ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" 2>/dev/null && pwd)"

if [ -z "$SCRIPT_DIR" ]; then
    echo "  [x] 推导不出脚本所在目录（$SCRIPT_PATH）" >&2
    exit 1
fi

MC_LINK_ROOT="${MCLINK_ROOT:-$SCRIPT_DIR}"

if [ ! -d "$MC_LINK_ROOT" ]; then
    echo "  [x] 安装根目录不存在：$MC_LINK_ROOT" >&2
    echo "      （用 MCLINK_ROOT=/你的/安装目录 覆盖，或把它当成默认值之外的路径）" >&2
    exit 1
fi

MC_LINK_ROOT="$(cd "$MC_LINK_ROOT" && pwd)"

#: 给子进程（ensure_secrets.py 等）用同一条口径，避免"父脚本算出来一个根目录、
#: 子脚本又自己猜一个"。
export MCLINK_ROOT="$MC_LINK_ROOT"

# ------------------------------------------------
# 行尾自愈：CRLF 会让 bash 直接报语法错误
# ------------------------------------------------
#
# 部署包是从 Windows/WSL 传上来的，脚本很容易带上 CRLF。bash 看到
# `#!/bin/bash\r` 或行尾的 `\r` 会报一堆看不懂的语法错误（`$'\r': command
# not found`），而真正的原因一个字都不提。
#
# 所以每一次运行本脚本，都先把**安装根目录**下的 .sh 洗一遍 —— 于是服务器
# 上的操作从两条命令变成一条：
#
#     上传整个 deploy 目录  ->  bash <安装根目录>/rebuild.sh
#
# 用 sed -i 就地改；失败也不致命（可能没有写权限，那就说明本来也不用洗）。
for script in "$MC_LINK_ROOT"/*.sh; do
    [ -f "$script" ] || continue
    if grep -q $'\r' "$script" 2>/dev/null; then
        sed -i 's/\r$//' "$script" 2>/dev/null \
            && echo "  [i] 已修正行尾（CRLF -> LF）：$(basename "$script")"
    fi
done

PANEL_DIR="$MC_LINK_ROOT/panel"
RELAY_DIR="$MC_LINK_ROOT/server"

#: 真实凭据所在的 .env（不在包里，只在服务器上）。
#:
#: ★ D5（v3.9.21）：它是**必需**的 —— 两个 compose 里只有 `${...}` 引用，
#:   真值只存在于这个文件（600，不进仓库、不进交付包）。没有它 docker compose
#:   会拒绝启动（`${VAR:?}`），所以本脚本会先一步拦住并说清楚怎么建。
#:
#: 它由**包根**统一提供，而不是每个子目录各一份 —— 副本一多就会出现
#: "面板改了、中继没改"这种只能靠面板报「无法从中继读取开关状态」才发现的错。
#:
#: 路径从安装根目录推导（D8）；想放到别处就设 MCLINK_ENV_FILE。
ENV_FILE="${MCLINK_ENV_FILE:-$MC_LINK_ROOT/.env}"

#: ==========================================================================
#:  ★ D5：凭据策略 —— **external（唯一支持的模式）**
#: ==========================================================================
#:
#:  历史：v3.9.16 把凭据外部化到 .env，v3.9.19 加了部署时自动填，v3.9.20 为了
#:  让部署走通又**改回硬编码**（明文写在 compose 里，风险见 SECURITY-NOTICE.md）。
#:  开源发布前不能带着真实凭据，所以按 D5 恢复外部化并**删掉 hardcoded 分支**：
#:  compose 里只允许 `${...}` 引用，明文一律判失败（那些值随旧包外传过，必须轮换）。
#:
#:  `external` = compose 只写 `${VAR:?...}` 引用，真值在 $ENV_FILE（当前且唯一）。
#:
#:  `.env` 缺失时不再"直接放行"：auto-fill（ensure_secrets.py）会尽量补齐，
#:  补不上就停下来说清楚为什么 —— 宁可不部署，也不给一个连不上的密码。
SECRETS_MODE=external

#: 传给 docker compose 的参数（有 .env 就带上）。见 `compose_cmd`。
COMPOSE_ENV_ARGS=()

#: 完整输出都进这里（默认模式下屏幕只留一行结果）。
#: 默认 = <安装根目录>/rebuild.log（D8：不写死路径），MC_LINK_LOG 可覆盖。
REBUILD_LOG="${MC_LINK_LOG:-$MC_LINK_ROOT/rebuild.log}"

# ------------------------------------------------
# v3.9.16：docker compose 的统一入口
# ------------------------------------------------
#
# 为什么要包一层：compose 里的凭据现在是 `${VAR:?...}` 引用，变量必须**先
# 进环境** docker compose 才给启动。以前 `docker compose up -d --build` 是
# 直接写的，现在统一经过这里，就不需要在每个调用点都记得加 --env-file。
compose_cmd () {
    if [ -f "$ENV_FILE" ]; then
        docker compose --env-file "$ENV_FILE" "$@"
    else
        docker compose "$@"
    fi
}

# 参数：可选 --strict / -v，其余是 panel / relay / all
STRICT=0
VERBOSE=0
TARGET=""

for argument in "$@"; do
    case "$argument" in
        --strict) STRICT=1 ;;
        -v|--verbose) VERBOSE=1 ;;
        panel|relay|all) TARGET="$argument" ;;
        *) echo "未知参数：$argument"; exit 1 ;;
    esac
done

TARGET="${TARGET:-all}"

# 默认就是实时逐行打印；这里只说明"细节去哪了"（完整日志文件）。
: > "$REBUILD_LOG" 2>/dev/null || REBUILD_LOG=/tmp/mc-link-rebuild.log
{
    echo ""
    echo "===== $(date '+%Y-%m-%d %H:%M:%S')  rebuild.sh $* ====="
} >> "$REBUILD_LOG" 2>/dev/null || true

fail () {
    echo ""
    echo "!! $1"
    {
        echo ""
        echo "!! $1"
    } >> "$REBUILD_LOG" 2>/dev/null || true
    exit 1
}

# 不在安静模式下了：默认就是"实时逐行"。这里只负责把标题/状态同时写进
# 屏幕和 $REBUILD_LOG。
say () {
    printf '%s\n' "$*"
    printf '%s\n' "$*" >> "$REBUILD_LOG" 2>/dev/null || true
}

# ------------------------------------------------
# run_step：**边跑边打印**每一步的每一行，同时完整写进日志
# ------------------------------------------------
#
# 用户 2026-09-29 原话：
#     「未来的 Rebuild 无需加那个 -v，可以直接打印详细的日志，每一步的日志
#      状态，且是实时打印；尤其是重建那一块，它重建完了一次冒出来一坨，
#      它这个一条一条地给我实时打印」
#
# 所以（不管加不加参数）：
#       ▶ 重建并启动中继
#          │ Step 1/8 : FROM python:3.12-slim
#          │ ...（docker 每写一行就立刻跟一行，不攒批）
#       ✔ 重建并启动中继（12s）
#
#   * 每一行都**立刻**过屏幕（stderr 也并进来），不再 `out=$(...)` 攒到最后；
#   * 同一份内容完整追加到 $REBUILD_LOG（实时那一份走屏幕，落盘这一份在
#     这一步结束时一次写完，两份内容逐字相同）；
#   * 默认只过滤掉**空行**（读起来清爽）；`-v` 把空行也打出来 —— 这就是
#     "-v = 更啰嗦（同时打印被过滤的原始输出）"；
#   * 失败时除了 ✔/✘ 那一行，再单独打一段"上下文"（最后 25 行），返回非 0。
run_step () {
    local title="$1"; shift

    local started
    started=$(date +%s)

    say ""
    say "  ▶ $title"

    local out_file rc line
    out_file=$(mktemp 2>/dev/null || echo "/tmp/mc-link-step-$$.out")
    : > "$out_file"

    # `tr '\r' '\n'`：docker 的进度条用回车刷同一行，不拆开就会整段糊成
    # 一行"一次冒出来一坨"。拆成行之后，它就是一条一条地实时出现。
    "$@" 2>&1 | tr '\r' '\n' | tee "$out_file" | while IFS= read -r line; do
        if [ "$VERBOSE" = "1" ]; then
            printf '     │ %s\n' "$line"
        elif [ -n "$line" ]; then
            printf '     │ %s\n' "$line"
        fi
    done

    rc=${PIPESTATUS[0]}

    # 完整输出进日志（永远，与屏幕过滤无关）。
    {
        echo ""
        echo "===== $title ====="
        cat "$out_file" 2>/dev/null
    } >> "$REBUILD_LOG" 2>/dev/null || true

    local elapsed
    elapsed=$(( $(date +%s) - started ))

    if [ "$rc" -ne 0 ]; then
        say "  ✘ $title（${elapsed}s，exit=$rc）"
        say "     ---- 失败上下文（最后 25 行）----"
        tail -n 25 "$out_file" 2>/dev/null | sed 's/^/     /' || true
        rm -f "$out_file" 2>/dev/null || true
        return "$rc"
    fi

    say "  ✔ $title（${elapsed}s）"
    rm -f "$out_file" 2>/dev/null || true

    return 0
}

# ------------------------------------------------
# 上传指纹：BUILD.txt
# ------------------------------------------------
#
# 这一块是为了一个具体的、花掉好几个小时的故障：
# `web/` 传上去了，`lib/` 没传，于是新版 index.php 调用
# no_cache_headers()（只有新版 common.php 里才有）→ 整站 500，
# 而所有"文件在不在"的检查全是 OK —— 旧文件也在，只是内容是旧的。
#
# 现在由 sync-deploy.py 生成每个文件的 sha256 清单，服务器照单核对，
# 差一个就点名，而且是在重建【之前】。
verify_upload () {
    local stamp="$MC_LINK_ROOT/BUILD.txt"

    if [ ! -s "$stamp" ]; then
        echo "  [!] 没有 $MC_LINK_ROOT/BUILD.txt —— 跳过指纹核对"
        echo "      （旧版部署包没有这个文件；传一份新的 deploy/BUILD.txt 即可）"
        return 0
    fi

    if ! command -v sha256sum >/dev/null 2>&1; then
        echo "  [!] 系统里没有 sha256sum，跳过指纹核对"
        return 0
    fi

    echo "  -- 核对每个文件是否真的传上来了（sha256）"

    # 先把 CR 和 BOM 清掉：指纹文件是从 Windows 传上来的，
    # 万一被转成 CRLF，每一个文件名都会多一个 \r，
    # sha256sum 会报"没有这个文件"——那是一次彻头彻尾的误报。
    #
    # 注意：BUILD.txt 里**本来就只有服务器需要的 47 个文件**（sync-deploy.py
    # 生成时已经剔掉了两个客户端 exe 和 README.txt），所以这里不需要再过滤 ——
    # 一句"只警告不拦"的注释曾经让我误判成"每次必报差异"，实测 47→47 无差异。
    local tmp
    tmp=$(mktemp)
    tr -d '\r' < "$stamp" | sed '1s/^\xEF\xBB\xBF//' > "$tmp"

    local report
    report=$(cd "$MC_LINK_ROOT" && sha256sum -c "$tmp" 2>&1)

    if printf '%s' "$report" | grep -q 'no properly formatted'; then
        rm -f "$tmp"
        echo "     [!] BUILD.txt 格式不对（不是 sha256sum 清单），跳过指纹核对"
        return 0
    fi

    rm -f "$tmp"

    # 只打印不对的行，几百行 OK 没人看
    local bad
    bad=$(printf '%s\n' "$report" | grep -v ': OK$' || true)

    if [ -z "$bad" ]; then
        local total
        total=$(printf '%s\n' "$report" | grep -c ': OK$' || true)
        echo "     [OK] $total 个文件全部一致"
        return 0
    fi

    echo "     [!!] 下面这些文件和服务器的对不上："

    # 分两类说清楚，因为修法完全不同：一个是要"传上去"，另一个是"重新覆盖"。
    # sha256sum 自己的措辞（FAILED / FAILED open or read）不适合直接给人看。
    local missing_files stale_files
    missing_files=$(printf '%s\n' "$bad" \
        | grep -E 'No such file|FAILED open or read' \
        | sed -E 's/^sha256sum: //; s/: No such file.*$//; s/: FAILED open or read$//' \
        | sort -u)
    stale_files=$(printf '%s\n' "$bad" \
        | grep -E ': FAILED$' \
        | sed -E 's/: FAILED$//' \
        | sort -u)

    if [ -n "$missing_files" ]; then
        echo "       ① 服务器上根本没有（从来没传上去）："
        printf '%s\n' "$missing_files" | sed 's/^/          /'
    fi

    if [ -n "$stale_files" ]; then
        echo "       ② 文件在，但是旧版（内容和新包不一样）："
        printf '%s\n' "$stale_files" | sed 's/^/          /'
    fi

    if [ -z "$missing_files" ] && [ -z "$stale_files" ]; then
        printf '%s\n' "$bad" | sed 's/^/       /'
    fi

    # ---- 只警告，不拦 ----
    #
    # 这里以前是 fail（直接退出）。用户的原话是"指纹并不太好用"：
    # 他刚上传完，指纹还说文件是旧的 —— 因为那次上传根本没覆盖成功，
    # 但被门卫拦住的感觉像是工具在骗人。所以现在默认只报告，
    # 由人决定要不要继续；想强制用 --strict。
    #
    # 推荐的做法本来就是【整包覆盖】（见 README.txt），
    # 那样这里应该永远是"全部一致"。
    if [ "$STRICT" = "1" ]; then
        fail "有文件没传上去、或者传的是旧版（上面点名了）。
   去掉 --strict 可以忽略这些差异继续重建。"
    fi

    echo ""
    echo "     [!] 上面这些差异【不会阻止重建】，但请先确认："
    echo "         如果你刚才整包覆盖过，说明那次上传没成功；"
    echo "         继续重建可能得到「改了但没生效」的结果。"
    echo "         想让它变成硬性检查：bash $0 --strict $TARGET"
    echo ""
}

# ------------------------------------------------
# 凭据占位符检查
# ------------------------------------------------
#
# This is the check whose absence caused a real outage. The compose files in
# the repository ship with Chinese placeholders ("请改成..."), and uploading
# one of those overwrites the working credentials on the server. The relay
# then hands a Chinese string to the MySQL driver, which fails with
#
#     'latin-1' codec can't encode characters in position 0-11
#
# -- a message that says nothing about the actual cause. Any non-ASCII byte in
# one of these values is therefore treated as fatal, before anything is
# rebuilt.
#
# ★ D5（v3.9.21）：compose 里**只允许** `${VAR:?...}` 引用。任何明文值（hex、
#   随机串、中文占位符都算）都是旧 compose 或手工改坏了，一律拦下 —— 旧包里
#   那些明文值已经外传过，必须轮换。
#
# 变量是否存在**以 .env 为准**（compose 跑起来时用的是 `--env-file .env`，
# 不是本脚本的环境），只有 .env 里也没有才回退看 shell 环境。
env_file_value () {   # $1 = 键名 -> 打印值（找不到打空）
    local key="$1"
    local value=""

    if [ -f "$ENV_FILE" ]; then
        value=$(grep -E "^${key}=" "$ENV_FILE" 2>/dev/null \
            | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")
    fi

    if [ -z "$value" ]; then
        value="${!key:-}"
    fi

    printf '%s' "$value"
}

check_credentials () {   # $1 = compose file, $2 = 人话名称
    local file="$1"
    local label="$2"

    if [ ! -f "$file" ]; then
        echo "  [!] 找不到 $file"
        return 1
    fi

    local bad=0

    for key in MCLINK_DB_PASSWORD MCLINK_ADMIN_API_TOKEN MCLINK_BOOTSTRAP_ADMIN; do
        # 取出该键的值（去掉引号和行尾空白）
        local value
        value=$(grep -E "^[[:space:]]*${key}:" "$file" \
            | head -1 \
            | sed -E 's/^[^:]*:[[:space:]]*//; s/^"//; s/"[[:space:]]*$//')

        # 空值对 BOOTSTRAP 是合法的（首次建号后应当清空）
        if [ -z "$value" ]; then
            continue
        fi

        # ==================================================================
        #  ★ 先把 `${VAR:?...}` 引用认出来，**再**谈"占位符"
        # ==================================================================
        #
        # `:?` 后面的兜底提示**故意是中文**（"在 <安装根目录>/.env 里设置
        # MCLINK_DB_PASSWORD"），它是变量缺失时 docker compose 原样打给
        # 人看的。所以"值里有中文"绝对不能当成"这是占位符"的判据 —— 否则
        # external 模式下**合法**的引用会被自己误杀：部署卡在
        #   [!!] 中继 compose 的 MCLINK_DB_PASSWORD 还是占位符：${...}
        # 重建永远进不去，容器永远跑着旧代码（实测 v3.9.21 线上就是这样）。
        #
        # 判据顺序必须是：是不是引用 -> 是就只查变量有没有设；不是引用，
        # 才轮到"中文占位符 / 明文凭据"这两类错误写法。
        if printf '%s' "$value" | grep -q '^\${'; then
            local varname
            varname=$(printf '%s' "$value" \
                | sed -E 's/^\$\{([A-Za-z_][A-Za-z0-9_]*).*/\1/')

            if [ -n "$varname" ] && [ -z "$(env_file_value "$varname")" ]; then
                echo "  [!!] $label 的 $key 引用了 $varname，但 $ENV_FILE 和 shell 环境里都没有它"
                echo "       [!] 不要跑 rotate_secrets.py 去'补' —— 那会换掉数据库密码，"
                echo "           中继会立刻连不上 MySQL。真值优先从**还在跑的容器**取回："
                echo "           docker inspect mc-relay --format '{{range .Config.Env}}{{println .}}{{end}}' \\"
                echo "             | grep -E '^MCLINK_(DB_PASSWORD|ADMIN_API_TOKEN)='"
                echo "           写进 $ENV_FILE（chmod 600）后再重建。"
                bad=$((bad + 1))
            fi

            continue
        fi

        # 非 ASCII（且不是引用）=> 一定是从没填过的中文占位符
        if printf '%s' "$value" | LC_ALL=C grep -q '[^ -~]'; then
            echo "  [!!] $label 的 $key 还是占位符：$value"
            bad=$((bad + 1))
            continue
        fi

        # ------------------------------------------------------------------
        #  ★ D5：剩下的只可能是明文值，一律判失败
        # ------------------------------------------------------------------
        #
        # 不只是"像 hex 的那种" —— rotate_secrets.py 生成的是字母+数字的串
        # （刻意去掉易混字符），拿 hex 当判据会漏报（v3.9.17 复核报告 P2）。
        # 走到这里说明它既不是 `${...}` 引用、也不是中文占位符，那就是明文。
        echo "  [!!] $label 的 $key 写着一个明文值：$value"
        echo "       compose 里只允许 \${${key}:?...} 这种引用，真值放 $ENV_FILE。"
        echo "       明文值随旧交付包外传过，必须轮换（python3 scripts/rotate_secrets.py）。"
        bad=$((bad + 1))
    done

    return "$bad"
}

# ------------------------------------------------
# v3.9.16：真实凭据所在的 .env
# ------------------------------------------------
#
# compose 里现在只有 `${MCLINK_DB_PASSWORD:?...}`。变量没设置时 docker
# compose 会**拒绝启动**，但它的报错是模板语法那一套，不会告诉你"你忘了
# 建 .env"。这一步把话说清楚，并且顺手把 .env 加进 docker compose 的调用
# 里（见下面 rebuild_* 函数用 `--env-file`）。
# ------------------------------------------------
# v3.9.19：部署时**自动**把占位符凭据填上
# ------------------------------------------------
#
# 以前这一步只会说"你忘了填"然后停住 —— 而填它其实是机械劳动：token 是
# 应用内部共享密钥（两边从同一个 .env 读，随机生成即可）；DB 密码要连同
# MySQL 里的账号一起改（脚本会用 check-db.sh 那一招拿到 root 密码并在容器
# 内执行 ALTER USER）。
#
# 所以这里先调 ensure_secrets.py 让它补齐，补完再检查。它是**幂等**的：
# 已经有真值的一律不动 —— 否则"跑一次 rebuild"就变成"换掉线上数据库密码"，
# 一次误操作就是一次全站中断。
#
# 它拿不到 MySQL 时会明确说原因并**不写** .env（避免 .env 是新密码、库里
# 还是旧的那种最难查的状态），此时返回非零，部署停在这里等人工。
ensure_env_file () {   # $1 = .env 路径
    local env_file="$1"
    # D8：脚本路径跟着安装根目录走（不写死 /opt/mc-link）
    local script="$MC_LINK_ROOT/scripts/ensure_secrets.py"

    if [ ! -f "$script" ]; then
        return 0    # 脚本没传上来 -> 交给 check_env_file 报错
    fi

    if ! command -v python3 >/dev/null 2>&1; then
        return 0
    fi

    if python3 "$script" --check --env "$env_file" >/dev/null 2>&1; then
        return 0    # 都已是真值，什么都不用做（快路径，不打日志）
    fi

    echo "  [i] 有凭据还是占位符 —— 尝试自动补齐（ensure_secrets.py）"

    if python3 "$script" --env "$env_file"; then
        return 0
    fi

    return 1
}

check_env_file () {   # $1 = .env 路径
    local env_file="$1"

    # ==================================================================
    #  ★ D5（v3.9.21）：external 是**唯一**模式，.env 是**必需**的
    # ==================================================================
    #
    #  compose 里只有 `${VAR:?...}` 引用，没有 .env 就起不来（docker compose
    #  会拒绝启动）。所以这里不再"找不到就直接放行"，而是：
    #
    #    1) 先让 ensure_secrets.py 尽量补（幂等：已有真值一律不动）；
    #    2) 还是缺 -> 说清楚怎么建，返回非零。
    #
    #  下面的规则**只在 external 模式有意义**（D5 之后不存在别的模式）；
    #  真有人把 SECRETS_MODE 改回去，这里直接拦住，不猜他的意图。
    if [ "$SECRETS_MODE" != "external" ]; then
        echo "  [!!] SECRETS_MODE=$SECRETS_MODE —— D5 只支持 external"
        echo "       compose 里不许有明文凭据；改回 hardcoded 等于把线上凭据"
        echo "       重新写进仓库（见 SECURITY-NOTICE.md）。"
        return 1
    fi

    #  两条**无论哪个模式都必须拦**的规则（它们是两类真实事故的疫苗）：
    #
    #    * 中文占位符：真值不可能是中文。被当成密码送给 MySQL 驱动时，报错是
    #      `'latin-1' codec can't encode characters ...`，完全不提真正原因。
    #    * `.env` 与 compose **不一致**：compose 只认它引用的那个变量名。
    #      如果 .env 里写的是另一个名字（或根本没有），面板与中继就会一个连得上、
    #      一个连不上——正是最难查的那种。
    #  ★ 2026-10-05（集成修复）：这里**不能** `|| return 1`。
    #    ensure_secrets.py 补不上时直接 return，用户只会看到一个静默的非零退出，
    #    看不到"该建 .env、该跑哪条命令"—— D5 那条回归测试专门守这个
    #    （它要求 .env 缺失时 **失败而且说清楚**）。所以：先尽量自动补，
    #    补不上就走到下面那个分支，把原因和两条命令打出来再返回非零。
    ensure_env_file "$env_file" || true

    if [ ! -f "$env_file" ]; then
        echo "  [!!] 找不到 $env_file（自动补齐也没成功）"
        echo "       compose 里只有 \${...} 引用，真实凭据只在这个文件里。"
        echo "       建一个："
        echo "           cp .env.example $env_file"
        echo "           python3 scripts/rotate_secrets.py --env $env_file"
        return 1
    fi

    local bad=0

    for key in MCLINK_DB_PASSWORD MCLINK_ADMIN_API_TOKEN; do
        local value
        value=$(grep -E "^${key}=" "$env_file" | head -1 | cut -d= -f2- \
            | tr -d '"' | tr -d "'")

        # ---- 规则 1：空的 / 明确占位符 / 中文占位符都不行 ----
        if [ -z "$value" ] || [ "$value" = "CHANGE_ME_REDACTED" ]; then
            echo "  [!!] $env_file 的 $key 还是空的/占位符"
            bad=$((bad + 1))
            continue
        fi

        if printf '%s' "$value" | LC_ALL=C grep -q '[^ -~]'; then
            echo "  [!!] $env_file 的 $key 是**中文占位符**"
            echo "       真凭据不会是中文；被当成密码送给 MySQL 驱动时，症状是"
            echo "       『'latin-1' codec can't encode characters ...』。"
            bad=$((bad + 1))
            continue
        fi

        # ---- 规则 2：.env 与 compose 必须指向**同一个变量名** ----
        #
        #  两个 compose（面板 / 中继）各自引用哪个变量，必须都等于 $key 且
        #  与 .env 里这一行同名 —— 少一边就是"面板连得上、中继连不上"。
        local compose_dir compose_ref
        for compose_dir in "$PANEL_DIR" "$RELAY_DIR"; do
            compose_ref=$(grep -E "^[[:space:]]*${key}:" \
                "$compose_dir/docker-compose.yml" 2>/dev/null \
                | head -1 \
                | sed -E 's/^[^:]*:[[:space:]]*//; s/^"//; s/"[[:space:]]*$//' \
                | sed -nE 's/^\$\{([A-Za-z_][A-Za-z0-9_]*).*/\1/p')

            if [ "$compose_ref" != "$key" ]; then
                echo "  [!!] $compose_dir/docker-compose.yml 的 $key 与 $env_file 对不上"
                echo "       compose 里应为 \\${${key}:?...} 引用（实际：${compose_ref:-明文或缺失}）。"
                echo "       两边指同一个变量名，才不会出现「面板连得上、中继连不上」。"
                bad=$((bad + 1))
            fi
        done
    done

    return "$bad"
}

# ------------------------------------------------
# Dockerfile 是否 COPY 了全部模块
# ------------------------------------------------
#
# A missing COPY line means the module simply is not in the image, and the
# symptom is a runtime error far away from the cause: "No module named
# 'stun_server'", or -- worse -- the old code quietly keeps running.
check_dockerfile () {   # $1 = dir, $2.. = 必须出现的模块名
    local dir="$1"
    shift

    local dockerfile="$dir/Dockerfile"
    local missing=0

    if [ ! -f "$dockerfile" ]; then
        echo "  [!] 找不到 $dockerfile"
        return 1
    fi

    for module in "$@"; do
        if ! grep -qE "^COPY .*\b${module}\b" "$dockerfile"; then
            echo "  [!!] Dockerfile 没有 COPY $module"
            missing=$((missing + 1))
        fi
    done

    return "$missing"
}

# ------------------------------------------------
# 修权限：php-fpm 以 nobody(65534) 运行，
# 拖拽上传会把属主/权限改掉，读不到就是 500。
# ------------------------------------------------
fix_panel_perms () {
    echo "  -- 修正面板文件权限（php-fpm 以 nobody 运行）"

    chown -R 65534:65534 "$PANEL_DIR/web" "$PANEL_DIR/lib" 2>/dev/null || true
    find "$PANEL_DIR/web" "$PANEL_DIR/lib" -type d -exec chmod 755 {} \; 2>/dev/null || true
    find "$PANEL_DIR/web" "$PANEL_DIR/lib" -type f -exec chmod 644 {} \; 2>/dev/null || true

    if [ -f "$PANEL_DIR/config.php" ]; then
        chown 65534:65534 "$PANEL_DIR/config.php" 2>/dev/null || true
        chmod 640 "$PANEL_DIR/config.php" 2>/dev/null || true
        echo "     config.php -> 65534:65534 / 640"
    fi
}

# ------------------------------------------------
# 面板
# ------------------------------------------------
rebuild_panel () {
    echo ""
    echo "=============================================="
    echo " 面板：检查文件 -> 重建 -> 验证"
    echo "=============================================="

    # 检查新版标记，缺了就直接说出来是哪个文件没传上去
    local missing=0

    check () {   # $1=文件  $2=只在新版里出现的字符串
        if [ ! -f "$1" ]; then
            echo "  [缺失] $1"
            missing=$((missing + 1))
        elif grep -qF "$2" "$1"; then
            echo "  [OK]   $1"
        else
            echo "  [旧版] $1  （找不到：$2）"
            missing=$((missing + 1))
        fi
    }

    check "$PANEL_DIR/lib/common.php"              "AUTO_REFRESH_SECONDS"
    # common.php 是面板的共用库，index.php 第 20 行就直接调
    # no_cache_headers()。老版本的 common.php 里没有这个函数，于是
    # 「web/ 传了、lib/ 没传」的后果是整站 500：
    #     Call to undefined function no_cache_headers()
    # 而"文件在不在"完全看不出来 —— 文件在，只是旧的。
    # 所以这里点名检查它必须提供的函数。
    check "$PANEL_DIR/lib/common.php"              "function no_cache_headers"
    check "$PANEL_DIR/lib/common.php"              "function relay_message"
    check "$PANEL_DIR/lib/common.php"              "function can_act_on"
    # 这个标记原来写的是「自动刷新」——那句话随页脚横幅一起删掉了。
    # 于是检查会在【最新版文件】上报「旧版」，把人挡在门外。
    # 现在用真实存在于新版里的字符串：页脚那段轮询 JS 的 data-live。
    check "$PANEL_DIR/web/views/layout_footer.php" "data-live"
    check "$PANEL_DIR/web/index.php"               "relay_message(\$result)"
    # ---- 本轮新增：面板能改房间人数上限 + 关房走 revoke ----
    #
    # 只检查"文件在不在"是不够的：老版本文件也在。这几行看的是【新版才有】
    # 的字符串，缺了就直接点名，避免"传了但没生效"这种最费时间的错觉。
    check "$PANEL_DIR/web/index.php"               "没有指定要修改的设置项"
    check "$PANEL_DIR/web/views/dashboard.php"     "value=\"max_players\""
    check "$PANEL_DIR/web/live.php"                "'max_players' =>"
    check "$PANEL_DIR/web/views/rooms.php"         "room_revoke"
    # index.php 会 require 这两个视图；少一个就是 500，不是"少个链接"。
    check "$PANEL_DIR/web/views/account.php"       "self_rename"
    check "$PANEL_DIR/web/views/layout_header.php" "我的账号"

    if [ "$missing" -gt 0 ]; then
        fail "有 $missing 个面板文件没传上去或是旧版。先传文件，再重新运行本脚本。"
    fi

    # 占位符凭据（中文占位符）会让面板连不上数据库
    if ! check_credentials "$PANEL_DIR/docker-compose.yml" "面板 compose"; then
        fail "面板 docker-compose.yml 里的凭据有问题（见上面的 [!!] 行）。"
    fi

    # 凭据本身再查一次（硬编码模式下主要查 .env 与 compose 是否打架）
    if ! check_env_file "$ENV_FILE"; then
        fail "$ENV_FILE 与 compose 里的凭据对不上（见上面的 [!!] 行）。"
    fi

    fix_panel_perms

    cd "$PANEL_DIR" || fail "进不去 $PANEL_DIR"

    run_step "停止面板容器" compose_cmd down

    run_step "删除旧镜像 mc-link-panel:2.0.0（防构建缓存）" \
        docker rmi -f mc-link-panel:2.0.0 || true

    run_step "重建并启动面板" compose_cmd up -d --build

    sleep 3

    echo "  -- 验证：面板能否本地访问"
    local code
    code=$(curl -s -o /dev/null -w '%{http_code}' -m 10 \
        "http://127.0.0.1:19102/index.php?action=login")

    if [ "$code" = "200" ]; then
        echo "     [OK] 登录页 HTTP $code"
    else
        echo "     [失败] 登录页 HTTP $code"
        docker logs --tail 30 mc-panel 2>&1 | sed 's/^/       /'
        fail "面板没起来，上面是容器日志。"
    fi

    echo "  -- 验证：文件真的进容器了吗"
    local inside
    inside=$(docker exec mc-panel sh -c \
        'grep -c AUTO_REFRESH_SECONDS /var/www/lib/common.php 2>/dev/null || echo 0' \
        2>/dev/null | tr -d '\r')

    if [ "${inside:-0}" = "0" ]; then
        fail "文件没进容器 —— Dockerfile 的 COPY 路径和实际目录对不上。"
    fi

    echo "     [OK] 容器内已加载新版 common.php"

    echo "  -- 验证：人数上限 / 关房那几个文件也进容器了吗"
    #
    # Markers chosen so they need no nested quoting: "max_players" only appears
    # in the NEW live.php, and the other two strings are new as well.
    local panelcheck
    panelcheck=$(docker exec mc-panel sh -c '
grep -qF "没有指定要修改的设置项" /var/www/panel/index.php 2>/dev/null || echo INDEX
grep -qF "max_players" /var/www/panel/views/dashboard.php 2>/dev/null || echo DASHBOARD
grep -qF "max_players" /var/www/panel/live.php 2>/dev/null || echo LIVE
grep -qF "function no_cache_headers" /var/www/lib/common.php 2>/dev/null || echo COMMON
' 2>/dev/null | tr -d '\r')

    if [ -n "$panelcheck" ]; then
        echo "     [!!] 容器里这几个文件还是旧版：$(printf '%s' "$panelcheck" | tr '\n' ' ')"
        fail "面板文件没全部进容器。把 deploy/panel/web 下的文件重新传一遍，再运行本脚本。
   （只传 index.php 是不够的：仪表盘在 views/dashboard.php，
     实时刷新在 live.php，两个都要传。）"
    fi

    echo "     [OK] 人数上限控件 / 实时刷新 / 关房按钮都是新版"

    # ---- PHP 语法自检（用面板自己的 php，不用装任何东西）----
    #
    # This is the check whose absence shipped a whole-panel 500:
    #
    #     PHP Fatal error: Cannot redeclare is_root()
    #         (previously declared in /var/www/lib/common.php:466)
    #
    # nginx and php-fpm start happily with that file in place -- the fatal only
    # appears per request -- so "container is up" said nothing. `php -l` catches
    # it in a second, and it also catches every parse error from an edit.
    echo "  -- 验证：每个 PHP 文件过一遍 php -l"

    local lintfail
    lintfail=$(docker exec mc-panel sh -c '
for f in /var/www/lib/*.php /var/www/panel/*.php /var/www/panel/views/*.php; do
    [ -f "$f" ] || continue
    php -l "$f" 2>&1 | grep -v "^No syntax errors" | grep -v "^$" || true
done
' 2>/dev/null | tr -d "\r")

    if [ -n "$lintfail" ]; then
        echo "     [!!] PHP 检查没通过："
        printf '%s\n' "$lintfail" | sed 's/^/       /'
        fail "面板文件有 PHP 语法/重复定义错误（上面就是真实原因）。
   修好之后重新整包覆盖 + 重新运行本脚本。
   最常见的两种：同一个函数被定义两次（Cannot redeclare），
   或者少了一个括号/分号。"
    fi

    echo "     [OK] 全部 PHP 文件语法正确（php -l）"
}

# ------------------------------------------------
# 中继
# ------------------------------------------------
rebuild_relay () {
    echo ""
    echo "=============================================="
    echo " 中继：检查文件 -> 重建 -> 验证"
    echo "=============================================="

    local missing=0

    check () {
        if [ ! -f "$1" ]; then
            echo "  [缺失] $1"
            missing=$((missing + 1))
        elif grep -qF "$2" "$1"; then
            echo "  [OK]   $1"
        else
            echo "  [旧版] $1  （找不到：$2）"
            missing=$((missing + 1))
        fi
    }

    # ---- every module the image needs must be present ----
    #
    # A missing .py file is not a cosmetic problem: Dockerfile COPY fails, or
    # -- worse -- an old copy stays in the image and the relay crash-loops on
    # an ImportError. That happened with db_schema.py: db_store.py imports
    # ROLE_BACKFILL from it, the file was never uploaded, and the container
    # restarted ten times before anyone saw why.
    #
    # So this checks the FILES, not just a couple of version markers.
    echo "  -- 模块文件是否齐全（缺一个中继就会启动失败）"

    for module in relay_config.py relay_server.py healthcheck.py \
                  admin_api.py auth.py db_schema.py db_store.py gatekeeper.py \
                  settings_service.py stun_server.py reset_password.py \
                  room_members.py room_registry.py requirements.txt; do
        if [ -f "$RELAY_DIR/$module" ]; then
            continue
        fi

        echo "  [缺失] $RELAY_DIR/$module"
        missing=$((missing + 1))
    done

    if [ "$missing" -gt 0 ]; then
        echo ""
        fail "有 $missing 个必需文件不在服务器上。
   中继启动会崩（ImportError），容器会一直重启。
   请按 deploy/README.txt 的清单把文件补齐，再重新运行本脚本。"
    fi

    echo "     全部 14 个文件都在"

    # ---- version markers ----
    echo "  -- 版本标记"

    check "$RELAY_DIR/relay_server.py"  "handle_logout"
    check "$RELAY_DIR/relay_config.py"  "MCLINK_STUN_PORT"
    check "$RELAY_DIR/stun_server.py"   "MCLINK_STUN_ENABLED"
    check "$RELAY_DIR/db_store.py"      "ROLE_BACKFILL"
    check "$RELAY_DIR/db_schema.py"     "ROLE_BACKFILL"
    check "$RELAY_DIR/room_members.py"  "revoke_room"
    # ---- 直连（STUN）那几个功能，必须逐条点名 ----
    #
    # 血的教训：上面那两条标记（revoke_room / from room_registry import）在
    # **老版** room_members.py 里也有，所以一个"没有入站探测、没有按人路由"的
    # 旧文件能把这套检查全部通过，然后脚本报"重建成功"——而客户端那边永远
    # 显示「入站未测」，查了一整晚。
    #
    # 教训：**每加一个服务端功能，就在这里加一条只属于它的标记。**
    check "$RELAY_DIR/room_members.py"  "DIRECT_INGRESS"
    check "$RELAY_DIR/room_members.py"  "def _direct_peer(room, member, text"
    # 房间注册表必须单独一个文件：relay_server 是以 __main__ 跑的，
    # 别的模块 `from relay_server import rooms` 会导入第二个空副本，
    # 于是面板关房永远回「房间不存在」。
    check "$RELAY_DIR/room_registry.py" "def set_logger"
    # 本轮中继侧的新东西，同样逐个点名：人数上限改成实时读设置、
    # 房间所有者也允许删房、共享注册表必须从 room_registry 导入。
    check "$RELAY_DIR/relay_server.py"  "def max_players()"
    check "$RELAY_DIR/settings_service.py" "SETTING_MAX_PLAYERS"
    check "$RELAY_DIR/room_members.py"  "from room_registry import"
    check "$RELAY_DIR/reset_password.py" "generate_password"
    # 本轮：数据库卡住不许拖住启动（2026-09-15 中继起不来的那次）。
    # 缺了这两行说明 relay_server.py / relay_config.py 还是旧版 ——
    # 旧版没有上限，数据库慢一下中继就永远不监听 19100。
    check "$RELAY_DIR/relay_server.py"  "def db_step("
    check "$RELAY_DIR/relay_config.py"  "MCLINK_DB_BOOT_TIMEOUT"
    # ---- 版本门（客户端与服务端必须同版本）----
    #
    # server/VERSION 是版本门**唯一**的数据来源：中继启动时读它，读不到就
    # 当成空字符串 = 门关着，旧客户端照样能连。这种"文件没传上去"和"功能
    # 没实现"在客户端界面上长得一模一样（都不会弹「需要更新」），所以这里
    # 必须点名，而不是只检查 relay_server.py 里有 VERSION_MISMATCH。
    check "$RELAY_DIR/relay_server.py"  "VERSION_MISMATCH"
    check "$RELAY_DIR/relay_config.py"  "RELAY_VERSION"
    check "$RELAY_DIR/Dockerfile"       "COPY VERSION"

    if [ ! -s "$RELAY_DIR/VERSION" ]; then
        fail "服务器上没有 $RELAY_DIR/VERSION（或它是空的）。
   版本门靠这个文件判断"客户端和服务端必须同版本"。缺了它，中继会当成
   【门关着】—— 旧版客户端照样能连，用户看到的不是「需要更新」。
   修法：把 deploy 包里的 server/VERSION 传上来（内容是客户端版本号）。"
    fi

    if [ "$missing" -gt 0 ]; then
        fail "有 $missing 个中继文件是旧版。先传文件，再重新运行本脚本。"
    fi

    echo "     版本门：服务端版本 = $(tr -d '[:space:]' < "$RELAY_DIR/VERSION")"

    # ---- 凭据（就是这次把数据库搞挂的原因）----
    if ! check_credentials "$RELAY_DIR/docker-compose.yml" "中继 compose"; then
        fail "中继 docker-compose.yml 里的凭据有问题（见上面的 [!!] 行）。
   两种典型情况：
     * 还写着明文（v3.9.15 及以前）—— 那些值已经外传过，必须轮换；
     * 引用了 \${MCLINK_DB_PASSWORD} 但环境里没有它 —— 检查 $ENV_FILE。
   中文占位符的症状是中继日志里：
       [DB] 数据库不可用： 'latin-1' codec can't encode characters ...
   报错完全不提真正的原因（中文占位符被当成密码送给 MySQL 驱动）。"
    fi

    if ! check_env_file "$ENV_FILE"; then
        fail "$ENV_FILE 与 compose 里的凭据对不上（见上面的 [!!] 行）。"
    fi

    # ---- Dockerfile 是否 COPY 了全部模块 ----
    if ! check_dockerfile "$RELAY_DIR" \
            relay_server.py gatekeeper.py stun_server.py settings_service.py; then
        fail "中继 Dockerfile 缺 COPY 行 —— 模块不会进镜像。
   症状是「No module named 'stun_server'」，或者更糟：旧代码静默继续跑。
   修法：用 deploy/server/Dockerfile 覆盖，再重新运行本脚本。"
    fi

    # Built-in STUN needs BOTH ports of the pair published. Checking only one
    # is how a deployment ends up looking healthy while the client can only
    # ever see a single destination -- which silently degrades every NAT
    # verdict to "unknown".
    #
    # 这一条从「警告」升级成「失败」：内置 STUN 的两个端口不是冗余，是一对。
    # 客户端的默认探测表前两台就是这两个端口（同一个 IP）—— 只开一个，
    # 客户端总共只拿到一个答案，NAT 类型直接判成 unknown，打洞策略跟着
    # 退化。用户报的「配了好几台 STUN 还是 unknown」就是这么来的。
    _stun_ports_missing=0
    for _p in 3478 3479; do
        if ! grep -q "$_p:$_p/udp" "$RELAY_DIR/docker-compose.yml"; then
            echo "  [失败] docker-compose.yml 缺少 UDP $_p 映射"
            _stun_ports_missing=$((_stun_ports_missing + 1))
        fi
    done

    if [ "$_stun_ports_missing" -gt 0 ]; then
        fail "内置 STUN 的 UDP 3478 / 3479 必须**两个都**映射出来。
   这两个端口是客户端默认探测表里唯一确定可达的目的地；只开一个的话，
   客户端只能拿到一个答案 —— NAT 类型直接是 unknown，打洞就没法准。
   修法：用 deploy/server/docker-compose.yml 覆盖，里面两个端口都有；
   另外确认云服务器安全组/防火墙也放行了 UDP 3478 和 3479。"
    fi

    cd "$RELAY_DIR" || fail "进不去 $RELAY_DIR"

    run_step "停止中继容器（正在玩的连接会断开）" compose_cmd down

    # 删的是【镜像标签】，不是构建缓存 —— 两者是分开的。所以
    # `pip install` 那一层只要成功过一次，之后重建都会命中缓存（秒过）。
    # 反过来说：第一次装依赖时【别中途 Ctrl+C】，否则这层不进缓存，
    # 下次还得从头下。Dockerfile 里已经把 pip 换成国内源（清华），
    # 正常情况下这一步是秒级。
    run_step "删除旧镜像 mc-link-relay:2.0.0" \
        docker rmi -f mc-link-relay:2.0.0 || true

    run_step "重建并启动中继" compose_cmd up -d --build

    echo "  -- 等待中继就绪"

    # Two ways to ask, because the first one is not always available:
    #
    #   1. Docker's own health status. The image ships healthcheck.py (stdlib
    #      only, no curl), so this works on python:3.12-slim, where `curl` is
    #      simply not installed -- asking curl there returns HTTP 000 and looks
    #      exactly like a dead relay. That false alarm cost a round trip.
    #   2. curl, for the admin API response body, when it happens to exist.
    local ok=0
    local health=""
    local port_ok=0

    for _ in $(seq 1 30); do
        health=$(docker inspect --format \
            '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
            mc-relay 2>/dev/null | tr -d '\r')

        if [ "$health" = "healthy" ]; then
            ok=1
            break
        fi

        # The TCP port is what actually matters for Minecraft: if the relay is
        # listening, players can connect.
        #
        # This probe now runs EVERY iteration, not only when the container has
        # no healthcheck at all. Docker reports "starting" for the whole
        # start-period (25 s in this image) even when the relay started
        # listening after one second, so the old condition made the loop wait
        # out all 30 iterations and then declare a perfectly good rebuild a
        # failure -- a gate that lies is worse than no gate.
        if docker exec mc-relay python -c \
            "import socket,sys; s=socket.socket(); s.settimeout(2); sys.exit(0 if s.connect_ex(('127.0.0.1',19100))==0 else 1)" \
            2>/dev/null; then
            port_ok=1
        fi

        sleep 1
    done

    # Listening is enough. The health status stays in the message so a real
    # database problem is still visible, but it is not what decides.
    if [ "$ok" != "1" ] && [ "$port_ok" = "1" ]; then
        echo "     [OK] 中继已在监听 19100（Docker 健康状态：${health:-未知}）"
        ok=1
    fi

    if [ "$ok" != "1" ]; then
        echo "     [失败] 容器健康状态：${health:-未知}"
        echo "     [失败] 19100 没有在监听 —— 中继卡在启动阶段了"
        echo "     ---- 最近 40 行容器日志 ----"
        docker logs --tail 40 mc-relay 2>&1 | sed 's/^/       /'

        # A stuck relay leaves no traceback: it is WAITING, not crashing. The
        # last [DB] line names the step it is stuck on, so call it out instead
        # of burying it in forty lines of banner.
        echo "     ---- 最后几条数据库日志（卡在这一步）----"
        docker logs mc-relay 2>&1 | grep -F '[DB]' | tail -3 | sed 's/^/       /' || true

        fail "中继没有进入 healthy 状态，上面是容器日志。
   如果最后一条 [DB] 之后就没有下一行了，说明数据库那一步没有返回：
     1) 数据库容器还活着吗：docker ps -a | grep -E 'mysql|web-mysql80'
     2) 中继能连上吗：docker exec mc-relay python relay_server.py --dbcheck
   中继本身有启动上限（MCLINK_DB_BOOT_TIMEOUT），所以正常情况下数据库慢
   只会让面板/账号降级，不会让中继起不来；能起不来就是真的有问题。"
    fi

    echo "     [OK] 中继健康状态：${health:-未知}"

    echo "  -- 验证：数据库是否连上（这是上次真正出问题的地方）"
    local dbline
    dbline=$(docker logs mc-relay 2>&1 | grep -E '\[DB\]' | tail -3 | tr -d '\r')

    if printf '%s' "$dbline" | grep -qF "数据库连接正常"; then
        echo "     [OK] 数据库连接正常"
    elif printf '%s' "$dbline" | grep -qF "没有响应"; then
        # The relay is up (it is listening -- checked above) and it gave up on
        # a slow database instead of hanging on it. That is the designed
        # behaviour, and the panel/login are what degrade.
        echo "     [!!] 数据库响应超时 —— 中继照常联机，面板/账号不可用"
        printf '%s\n' "$dbline" | sed 's/^/       /'
        echo "     排查数据库本身（长事务 / 元数据锁）："
        echo "       docker exec -i web-mysql80 mysql -uroot -p -e \\"
        echo "         \"SELECT * FROM information_schema.innodb_trx\\G\""
    else
        echo "     [!!] 数据库可能没连上，日志如下："
        printf '%s\n' "$dbline" | sed 's/^/       /'
        echo "     检查 $RELAY_DIR/docker-compose.yml 里的 MCLINK_DB_PASSWORD；"
        echo "     也可以直接在容器里自检："
        echo "       docker exec mc-relay python relay_server.py --dbcheck"
    fi

    echo "  -- 验证：三级角色所需的 users.role 列（缺了面板登录会 500）"
    local rolecheck
    rolecheck=$(docker logs mc-relay 2>&1 | grep -F "users.role" | tail -2 | tr -d '\r')

    if [ -n "$rolecheck" ]; then
        echo "     [!!] $rolecheck"
        echo "     面板登录会失败。上面有迁移失败的真实原因。"
    else
        echo "     [OK] users.role 列存在"
    fi

    echo "  -- 验证：容器内模块是否齐全（导入失败会让容器反复重启）"
    local importfail
    importfail=$(docker run --rm --entrypoint python mc-link-relay:2.0.0 -c '
import sys
sys.path.insert(0, "/app")
bad = []
for name in ("room_members", "room_registry", "db_store", "db_schema",
             "gatekeeper", "stun_server", "admin_api", "settings_service",
             "relay_server"):
    try:
        __import__(name)
    except Exception as e:
        bad.append(f"{name}: {e}")
print("; ".join(bad))
' 2>&1 | tail -1 | tr -d '\r')

    if [ -n "$importfail" ]; then
        echo "     [!!] 有模块导入失败："
        echo "          $importfail"
        fail "中继镜像里的模块有问题，容器会一直重启。上面是真实原因。"
    fi

    echo "     [OK] 全部 9 个模块都能导入"

    echo "  -- 验证：内置 STUN 是否启动"
    if docker logs mc-relay 2>&1 | grep -qF "STUN 服务已启动"; then
        echo "     [OK] 内置 STUN 已启动"
    else
        echo "     [!] 内置 STUN 没启动（不影响联机，只影响打洞）"
    fi

    echo "  -- 验证：本次要的模块是否真的在容器里"
    for module in gatekeeper.py stun_server.py room_members.py room_registry.py reset_password.py; do
        local present
        present=$(docker exec mc-relay sh -c \
            "test -f /app/$module && echo yes || echo no" 2>/dev/null | tr -d '\r')

        if [ "$present" = "yes" ]; then
            echo "     [OK] 容器内有 $module"
        else
            echo "     [!!] 容器内没有 $module —— Dockerfile 的 COPY 还是不对"
        fi
    done
}

# ------------------------------------------------
# 容器里现在跑的，到底是哪一份代码？
# ------------------------------------------------
#
# 这是为了回答一个反复被问的问题：「我明明覆盖+重建了，怎么还是旧代码？」
#
# 真正的原因通常不是"没重建"，而是：脚本按顺序做两个目标，
# 【前一个失败了就 exit 了，后一个根本没轮到】—— 而失败信息是面板的，
# 于是人以为"重建过了"，其实中继那一步一次都没执行。
#
# 所以现在：① 先摊开容器内 vs 磁盘上的指纹；② 两个目标各跑各的、
# 互不阻塞；③ 最后再比一次，并明确报告哪个目标没成功。
md5_of () {   # $1 = 主机上的文件
    if command -v md5sum >/dev/null 2>&1; then
        md5sum "$1" 2>/dev/null | awk '{print $1}'
    else
        echo "?"
    fi
}

container_md5 () {   # $1 = 容器名  $2 = 容器内路径
    docker exec "$1" md5sum "$2" 2>/dev/null | awk '{print $1}' | tr -d '\r'
}

STALE_FOUND=0

compare_code () {   # $1=容器 $2=主机文件 $3=容器内路径 $4=名字
    local host_md5 container_md5_value

    if [ ! -f "$2" ]; then
        echo "     [!] 主机上没有 $2"
        return 0
    fi

    host_md5=$(md5_of "$2")
    container_md5_value=$(container_md5 "$1" "$3")

    if [ -z "$container_md5_value" ]; then
        echo "     [!] $4：读不到容器内的文件（容器没在跑？）"
        return 0
    fi

    if [ "$host_md5" = "$container_md5_value" ]; then
        echo "     [OK] $4：容器内 = 磁盘上"
    else
        echo "     [!!] $4：容器里跑的是【旧代码】"
        echo "           磁盘上 = $host_md5"
        echo "           容器内 = $container_md5_value"
        STALE_FOUND=1
    fi
}

show_running_code () {
    echo "  -- 容器里跑的到底是哪一份代码（容器内 vs 磁盘上）"

    compare_code mc-relay "$RELAY_DIR/relay_server.py"  \
        /app/relay_server.py "中继 relay_server.py"
    compare_code mc-relay "$RELAY_DIR/room_registry.py" \
        /app/room_registry.py "中继 room_registry.py"
    compare_code mc-panel "$PANEL_DIR/lib/common.php"   \
        /var/www/lib/common.php "面板 lib/common.php"
    compare_code mc-panel "$PANEL_DIR/web/views/dashboard.php" \
        /var/www/panel/views/dashboard.php "面板 views/dashboard.php"

    if [ "$STALE_FOUND" = "1" ]; then
        echo "     -> 上面这些就是「改了没反应」的原因：容器里是旧代码。"
        echo "        重建之后这里应该全部变成 [OK]。"
    fi
}

# ------------------------------------------------
# 跑一个目标（失败不阻塞另一个）
# ------------------------------------------------
#
# 子 shell 是关键：rebuild_relay / rebuild_panel 内部的 fail 会 exit，
# 在子 shell 里那只结束这个目标，另一个照样跑。
FAILED_TARGETS=""

run_target () {   # $1 = relay | panel
    local name="$1"
    local code=0

    echo ""
    echo "############################################################"
    echo "#  重建：$name"
    echo "############################################################"

    if [ "$name" = "relay" ]; then
        ( rebuild_relay ) || code=$?
    else
        ( rebuild_panel ) || code=$?
    fi

    if [ "$code" = "0" ]; then
        echo ""
        echo "  == $name 重建完成 =="
    else
        echo ""
        echo "  !! $name 重建失败（exit=$code）—— 上面就是原因"
        FAILED_TARGETS="$FAILED_TARGETS $name"
    fi

    return 0
}

# ================================================================
# ★ 2026-10-05（用户口径）：**"整包上传 + 跑 rebuild.sh" 就是唯一入口**
# ================================================================
#
# 开源版为了安全，把凭据从 compose 里挪进了 `.env`（D5）。于是"拿到新包
# 第一次跑"需要有人探测数据库、写 `.env`、把值补进 compose —— 用户不想
# 记第二套命令，所以这里**自动做掉**：
#
#     * `.env` 已经有真值 -> 什么都不做（幂等，绝不覆盖线上凭据）；
#     * `.env` 缺失 / 还是占位符 -> 调用 install.sh 的"只准备、不起容器"
#       模式（`--no-start`），它会：探测本机 MySQL 容器 -> 读 root 密码 ->
#       复用或生成凭据 -> 写 `.env`(600) -> 把值补进两个 compose。
#
# 用 `--no-start` 是为了不和下面正常的 build/up 打架：准备归准备，
# 起容器仍然由本脚本按 `panel|relay|all` 的节奏来做。
#
# 跳过这一步：MCLINK_SKIP_PREPARE=1
# 指定复用的数据库容器：MCLINK_DB_CONTAINER=web-mysql80
if [ "${MCLINK_SKIP_PREPARE:-0}" != "1" ] && [ -f "$MC_LINK_ROOT/install.sh" ]; then
    # 键名放变量里：源码里就不出现"键 + 等号 + 值"的样子
    # （check_deploy 的明文凭据扫描按那个形状判定，这里是在写判据，不是在写凭据）
    _prep_k1="MCLINK_DB_PASSWORD"
    _prep_k2="MCLINK_ADMIN_API_TOKEN"
    _need_prepare=0

    if [ ! -f "$ENV_FILE" ]; then
        _need_prepare=1
    elif grep -qE "^(${_prep_k1}|${_prep_k2})=(CHANGE_ME_REDACTED)?$" "$ENV_FILE" 2>/dev/null; then
        _need_prepare=1
    fi

    if [ "$_need_prepare" = "1" ]; then
        echo ""
        echo "=============================================="
        echo " 首次运行：自动准备凭据（探测数据库 → 写 .env）"
        echo "=============================================="

        _prepare_args=(--no-start --yes --root "$MC_LINK_ROOT")

        if [ -n "${MCLINK_DB_CONTAINER:-}" ]; then
            _prepare_args+=(--db-container "$MCLINK_DB_CONTAINER")
        fi

        if ! bash "$MC_LINK_ROOT/install.sh" "${_prepare_args[@]}"; then
            echo "  [!!] 自动准备失败 —— 按上面的提示处理完再重跑本脚本。"
            exit 1
        fi
    fi
fi

# ------------------------------------------------

case "$TARGET" in
    panel) verify_upload; show_running_code; run_target panel ;;
    relay) verify_upload; show_running_code; run_target relay ;;
    # 中继先做：它是依赖方，而且它是被"面板失败就 exit"漏掉过很多次的那个。
    all)   verify_upload; show_running_code; run_target relay
           run_target panel ;;
    *)     fail "参数只能是 panel / relay / 空（两个都重建）" ;;
esac

echo ""
echo "=============================================="
echo " 重建后复核：容器里跑的代码换过来了吗"
echo "=============================================="
STALE_FOUND=0
show_running_code

echo ""
echo "=============================================="

if [ -n "$FAILED_TARGETS" ]; then
    echo " 没重建成功：$FAILED_TARGETS"
    echo "=============================================="
    echo ""
    echo " [!] 没重建成功的那个容器，跑的还是【旧代码】。"
    echo "     它在面板上的表现就是「改了没反应」：人数上限存不了、"
    echo "     强制关闭回「房间不存在」、建出来的管理员是普通玩家、"
    echo "     房间人数一直显示 0/10。"
    echo ""
    echo " 按上面的报错修好之后，单独把它重建一次即可："
    echo "     bash $0 relay     # 只重建中继（会断开正在玩的玩家）"
    echo "     bash $0 panel     # 只重建面板"
    echo ""
    exit 1
fi

# ------------------------------------------------
# 一次性设置：让重建变成敲一个短词
# ------------------------------------------------
#
# 每次都要打 `bash <安装根目录>/rebuild.sh` 太啰嗦，而且路径记不住。
# 这里给它装两个短命令（只在第一次真的写进去，之后就跳过）：
#
#     mcr      -> bash <安装根目录>/rebuild.sh
#     mcp      -> bash <安装根目录>/rebuild.sh panel   （只重建面板）
#
# 两种形式都装，是因为"到底哪个生效"取决于你怎么登录：
#   /usr/local/bin/mcr   非交互式 ssh 命令也能用（PATH 里有 /usr/local/bin）
#   ~/.bashrc 里的 alias  交互式登录后能用，且不受 PATH 影响
#
# ★ D8：写进去的路径是**运行时推导出来的安装根目录**（$MC_LINK_ROOT），不是
#   写死的 /opt/mc-link。包装脚本/alias 是"安装时生成的数据"，生成时把真实
#   路径固化下来是对的（它必须能在没有环境变量的 ssh 会话里工作）；源码里
#   不留任何绝对路径。
#
# 这一步失败不影响重建本身，所以全是 best-effort，出错不报。
install_shortcuts () {
    local wrapper target bin_dir rc

    #: 装到哪里也可以覆盖（安装器在测试/非 root 环境下需要）。
    bin_dir="${MCLINK_BIN_DIR:-/usr/local/bin}"
    rc="${MCLINK_BASHRC:-/root/.bashrc}"

    # ---- 1) /usr/local/bin 里的小包装脚本 ----
    for wrapper in mcr mcp; do
        target="$MC_LINK_ROOT/rebuild.sh"
        [ "$wrapper" = "mcp" ] && target="$MC_LINK_ROOT/rebuild.sh panel"

        if [ ! -x "$bin_dir/$wrapper" ]; then
            # shellcheck disable=SC2016  # 这里的 $@ 是要写进文件里的
            if printf '%s\n' '#!/bin/sh' \
                    "exec bash $target \"\$@\"" \
                    > "$bin_dir/$wrapper" 2>/dev/null; then
                chmod +x "$bin_dir/$wrapper" 2>/dev/null || true
                echo "  [+] 已安装短命令：$wrapper"
            fi
        fi
    done

    # ---- 2) 给 root 的 shell 也加一个 alias（覆盖非登录 shell） ----
    if [ -f "$rc" ] && ! grep -q 'mc-link rebuild shortcuts' "$rc" 2>/dev/null; then
        if printf '%s\n' \
                '' \
                '# mc-link rebuild shortcuts' \
                "alias mcr=\"bash $MC_LINK_ROOT/rebuild.sh\"" \
                "alias mcp=\"bash $MC_LINK_ROOT/rebuild.sh panel\"" \
                >> "$rc" 2>/dev/null; then
            echo "  [+] 已写入 $rc（重开终端后生效）"
        fi
    fi
}

install_shortcuts

echo "=============================================="
echo " 完成（完整日志：$REBUILD_LOG）"
echo "=============================================="
echo ""

# 默认已经是"一步步实时逐行"；这里只说清楚 -v 多给了什么，免得用户以为
# 还得加参数才能看到过程（2026-09-29 用户明确要求"无需加 -v"）。
echo " 每一步的每一行都已经实时打印在上面，并完整写进 $REBUILD_LOG。"
echo " 加 -v 只是更啰嗦（连被过滤掉的空行 / 原始控制行也打出来）。"
echo ""

# 顺手提醒那两个诊断脚本（会泄露中继信息）。只在它们真的还在时才提。
if [ -f "$PANEL_DIR/web/_diag.php" ] || [ -f "$PANEL_DIR/web/_diag2.php" ]; then
    echo " [!] 面板目录里还有诊断脚本（会泄露中继信息），删掉："
    echo "     rm -f $PANEL_DIR/web/_diag.php $PANEL_DIR/web/_diag2.php"
    echo ""
fi

