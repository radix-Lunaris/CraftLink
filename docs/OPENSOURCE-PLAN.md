# CraftLink 开源化改造 · 设计与执行基线

> 用户 2026-10-05 定稿。**这份文档是后续所有改动的唯一口径**，任何实现不得偏离；
> 要改口径必须先改这份文档并征求用户同意。
> 交付方式：**一次交付一个完整包**（不再一轮一轮让用户部署），但内部按 M1–M8
> 里程碑推进，每个里程碑必须过门禁（`check_deploy.py` 8 项 + 回归 + 真中继 e2e）。

## 0. 已锁定的决定（D1–D14）

| # | 决定 | 内容 |
|---|---|---|
| D1 | 面板设置存储 | **数据库为主 + 配置文件备份**（同一份设置写两处；DB 不可用时只读回退到配置） |
| D2 | 面板 SSL | **两者都要**：上传证书（默认、离线可用）+ Let's Encrypt 自动申请/续期（可选，需域名解析 + 80 端口） |
| D3 | Windows 安装器 | **Inno Setup**；另有"开发者构建器"`CraftLink-Builder.exe`，它生成面向用户的 `CraftLink-Setup.exe` |
| D4 | 数据库探测 | Docker 里能读到的 MySQL/MariaDB **直接复用**（建库/建账号，凭据从容器读，不硬编码）；探测不到或非 Docker → **提示手动配置**，不猜 |
| D5 | 现有凭据 | 用户线上那套（DB `mclink`/`mclink`、主机容器名）**一次性升级时保留**，便于平滑迁移；开源版**不硬编码**，改为部署时读取/生成 |
| D6 | SSH 终极管理台 | root-only、免密码快捷命令（默认名 `mclink`）：重设安全入口 / 解绑域名 / 改 root 密码 / 重置会话 / 查看状态 |
| D7 | 许可证 | **GPLv3**（`LICENSE` 进仓库与交付包）；`docs/`、回归网**保留** |
| D8 | 安装路径 | ★ **绝不写死 `/opt/mc-link`**。安装根目录由安装器/环境变量给定（默认 `/opt/mc-link` 只是默认值），脚本一律用 `${MCLINK_ROOT}` 之类变量推导 |
| D9 | 更新方式 | **原地升级**：发布包（`release-<ver>.tar.gz`）+ `current` 软链原子切换 + 保留上一版可回滚；**数据库、`.env`、用户数据不随发布包替换** |
| D10 | 中继地址 | 客户端**不硬编码**域名/端口，默认空；用户手填后记忆（存用户配置目录） |
| D11 | 登录记忆 | 用户名/密码记忆默认留空（用户主动勾选才记） |
| D12 | 单房间上限 | 2 → **64**（中继校验 + 面板表单 + 客户端显示三处同步；含资源说明） |
| D13 | `site/` 与哈希校验 | 从主交付**摘出为可选组件**（`optional/`），未来开发者自行决定用宝塔/1Panel 实现；主交付不含网站服务 |
| D14 | 服务端形态 | **全部 Docker**（中继、面板、可选 DB），compose 为唯一启动方式 |

## 1. 目录与配置约定（对 D8/D9 的展开）

```
<INSTALL_ROOT>/                 # 默认 /opt/mc-link，可自定义
├── .env                        # 凭据与运行参数（600，永不进发布包）
├── releases/
│   ├── 3.9.21/                 # 每个版本一个目录（发布包解到这里）
│   └── 3.9.20/                 # 上一版：回滚用
├── current -> releases/3.9.21  # 原子切换点（compose/rebuild 都指向它）
├── data/                       # 数据库卷、面板上传、日志（升级不动）
├── config/                     # 面板设置备份（D1）、SSL 证书
└── rebuild.sh                  # 稳定入口，内部用 current
```
* 所有脚本从**自身位置**推导 `INSTALL_ROOT`（`$(cd "$(dirname "$0")/.." && pwd)`），
  不出现任何硬编码绝对路径。
* 客户端用户数据（日志、记忆的服务器/账号）存 **用户可选目录**（安装器询问，
  默认 `%APPDATA%\CraftLink` / `~/.config/CraftLink`）。

## 2. 升级与迁移策略（对用户线上 3.9.20）

1. 备份：`mysqldump` + 打包 `.env` + `data/`；
2. 新装到 `releases/<新版本>/`，`.env` 由安装器**读取现有容器凭据**生成（D5），
   指向原有 MySQL 容器（数据不动）；
3. `current` 软链切到新版本 → `rebuild.sh up`；
4. 面板从"1Panel 反代"改为**自带 nginx 直听**：放行新端口、下线 1Panel 那条反代；
5. 回滚：`current` 指回旧版本目录 + `rebuild.sh up`。

## 3. 里程碑（内部；对外一次交付）

| 里程碑 | 内容 | 验收要点 |
|---|---|---|
| M1 面板基础 | 去掉仪表盘「正在连接」两行；左上角显示中继版本；上限 2→64（三处）；ICP 可配置空则隐藏；面板版本号 2.0.0→**2.11.45**（用户 2026-10-05 定；面板独立版本线，与客户端/中继的 3.9.2x 线分开走） | 面板截图 + 中继校验单测（65 人拒绝、64 人通过） |
| M2 面板设置页 | 新页「面板设置」+ 权限模型（root 可改 / 普通管理默认只读）+ 逐条 root 开关（默认全关）+ 标题/标签名/logo/ICP | 权限矩阵回归（root/普通管理 × 每条） |
| M3 网络与安全项 | 监听 IP/端口、URL 安全入口、未认证返回码、会话时长（默认 360 分钟）；面板自带 nginx（去 1Panel 依赖） | 安全入口错误时按配置返回码响应；会话时长生效 |
| M4 SSL 与域名 | 上传证书 + ACME；域名绑定后仅绑定域名可访问 | 自签证书 e2e；ACME 用 dry-run 验证流程 |
| M5 安装器（服务端） | 离线安装脚本：探测/复用 DB（D4）、确认端口与安全入口、写 `.env`、拉镜像/构建、起服务；SSH 终极管理台 | 全新机器 + 已有 DB 机器两条路径各跑一遍 |
| M6 客户端去硬编码 | 中继地址默认空 + 记忆；登录记忆默认空；开发者初始化引导（名字/logo/默认地址固化） | 首次运行引导 + 记忆文件校验 |
| M7 Windows 安装器 | Inno Setup 用户安装器（自定义安装位置/数据目录）+ Builder 生成器 | 安装/升级/卸载 + 静默安装 |
| M8 收尾 | `site/`+哈希校验摘到 `optional/`；全 Docker 化；HTML 图形化 README（含 3.9.20→新版迁移步骤）；LICENSE(GPLv3)/CONTRIBUTING | 门禁 8 项 + 回归 + e2e 全绿 |

## 4. 成本与更新方式的回答（用户提问）

* **更新不用重装**：发布 `release-<ver>.tar.gz` → 解到 `releases/<ver>` → 切
  `current` → `rebuild.sh up`。数据库/`.env`/用户数据都在版本目录之外，升级不碰。
* **Token 成本**：批量做（一次交付一版）比"一轮一交付"省很多；每版附一份
  版本说明（像 v3.9.18/19/20 的"修复说明"），下一轮只需读说明 + 差异，不必重读全仓。
* **回归网**：`tests/run_all.py`（包内，32 项，随包走）+ `tests/run_all_full.py`
  （工作区，57 套，重网，不随包）。

## 6. 进度（滚动更新）

| 里程碑 | 状态 | 证据 / 备注 |
|---|---|---|
| M1 面板部分（去「正在连接」/ 中继版本显示 / ICP 空则不渲染 / 版本 2.11.45） | ✅ 面板侧完成 | `tests/panel_static_test.py` **276/0**（M4 之后；M1 当时 261/0）；`docs/panel-settings-selftest.php` **206/0**；`docs/panel-settings-smoke.sh` **74/0** |
| M2 面板设置页 + 权限模型 + 每条 root 开关 | ✅ 面板侧完成 | 表 `panel_settings`（懒建，`CREATE TABLE IF NOT EXISTS`）；DB 主 + `config/panel-settings.json` 备份；权限在**服务端**强制（`require_panel_setting_edit()` / `require_root()` / CSRF）；审计码 5 个（安全入口与 logo 值不写审计） |
| M3 设置项 ①②③④⑦⑧⑨⑩ + 自带 nginx 的配置读取层 | ✅ 面板侧完成（nginx 容器化留给服务端） | nginx 接口写在 `docs/PANEL-SETTINGS.md` §10 |
| M1/M5 服务端+客户端部分（64 人上限 / 管理 API 版本 / 路径去硬编码 / install.sh / mclink 管理台） | ✅ 完成 | 门禁 **8/8**；`tests/run_all.py` **37/37**（新增 5 条）；`tests/m1_capacity_version_test.py` **18/0**（真中继：64 进 / 65 `ROOM_FULL`）；`tests/install_sh_test.py` **44/0**（假 docker：全新机 / 复用现有 DB / 重跑幂等 / 失败不写 `.env`）；`BUILD.txt` 411 条指纹一致 |
| M4 SSL + 域名绑定 + 面板自带 nginx | ✅ 面板侧完成 | 六个设置项进注册表（DB+JSON 双写 + 逐条 root 放行开关 + 校验）；`panel/nginx/templates/**` → 渲染 → root 代理 `nginx -t` → 热加载（失败保留上一份）；上传 / 自签 / ACME（申请 + 续期 + dry-run，续期失败不动旧证书）；域名绑定（nginx Host 守卫 + PHP 兜底）；本机急救入口 + 急救覆盖 + `mclink reset-entry/set-entry/unbind-domain/ssl-off/apply-network/rescue`。证据：`docs/panel-ssl-nginx-smoke.sh` **157/0**（真 nginx + 自签证书 + 桩 certbot/docker）、`docs/panel-settings-smoke.sh` **74/0**、`docs/panel-settings-selftest.php` **206/0**、`tests/panel_static_test.py` **276/0**、`tests/install_sh_test.py` **44/0**、`check_deploy.py` **全部通过**。说明见 `docs/M4-SSL-DOMAIN.md` |
| M6 客户端去硬编码 + 品牌可定制 | ✅ 客户端侧完成 | D10：`server_host=""` / `server_port=0`，全仓清掉 `www.example.com` / 端口 `19100` / `UPDATE_URL` 域名，首次运行给非模态引导、卡片常驻提示、"恢复默认"读 `branding.json`；D11：登录记忆默认不写盘、旧目录凭据迁移时清理；数据目录 Windows `%APPDATA%\CraftLink` / Linux `~/.config/CraftLink`，指针文件在默认目录、界面可改并即时迁移、拒绝写安装目录；`branding.json`（`app_name`/`logo_file`/`default_server_host`/`default_server_port`/`update_url`）含逐项回退，详见 `docs/CLIENT-BRANDING.md`。证据：`tests/m6_client_config_test.py` **78/0**；`tests/run_all.py` **41/41**（M6 新增 4 条）；`tests/credentials_test.py` **40/0** |
| M7 Inno Setup 用户安装器 + Builder | ⏳ 排队 | 依赖 M6 的 `branding.json` 接口 |
| M8 收尾（可选组件摘出 / HTML 说明书 / GPLv3 / 迁移清单） | ✅ 完成（仓库侧） | D13：`site/` 与哈希校验摘到 **`optional/site/` + `optional/checksum/`**（各带 README，写明"不再提供、可自行用宝塔/1Panel 实现"；里面的备案号/域名/哈希全部换成占位符，exe 不进库）。主交付不再引用它们：`scripts/make_build.py` 把整个 `optional/` 排除出 BUILD.txt/README.txt（实测包清单 **455 条，0 条 optional**）；`.gitignore` 加 `optional/site/download/*.exe` 等。`README.html`（自包含、无外链、无 JS，含「从 3.9.20 升级到本版本」章）；`LICENSE`（GPLv3 全文 674 行）+ `CONTRIBUTING.md` + `SECURITY.md` + `CODE_OF_CONDUCT.md`；`tests/run_all.py` **42/42**（新增 1 条跳过式检查）、`tests/panel_static_test.py` **276/0** |

**已确认的既有问题（非本轮引入，收尾时处理）**：
1. `tests/room_settings_test.py`、`tests/v2_control_test.py` 在工作区失败 —— 原因是
   `server/VERSION` 存在，而这两套的握手行没有 `ver=`（对照实验：临时移走
   `server/VERSION` 后 `room_settings_test` 105/0）。修法：给它们的握手行补
   `ver=<server/VERSION>`（这是"我们那套 57 套网 22 红"的主要来源之一）。
2. `deploy_scripts_test` 4 条红：`deploy/HANDOVER.md` 未生成（需跑 `sync-deploy.py`）
   + `check_deploy.py` 缺"实时逐行"静态断言（root 与 deploy 两份都缺）→ 收尾统一处理。
3. 发版前顺序固定：`python3 scripts/make_build.py --clean` → `python3 scripts/check_deploy.py`
   →（全绿）`check_deploy.py --stamp` → `bump-version.py`。

| M4 SSL + 域名绑定 + 面板自带 nginx | ✅ 面板侧完成 | 见上一张表同名行的证据；说明文档 `docs/M4-SSL-DOMAIN.md`（含真机验收清单与遗留项） |
| M7 Inno Setup 用户安装器 + Builder | ⏳ 排队 | 依赖 M6 的 `branding.json` 接口（安装器要做三件事，见 `docs/CLIENT-BRANDING.md` §7） |
| M8 收尾（可选组件摘出 / HTML 说明书 / GPLv3 / 迁移清单） | ✅ 完成（仓库侧） | 见上一张表同名行的证据；老网缺口处理结果见下方。打包/门禁仍是**打包阶段的人**来做（本里程碑不跑 `make_build.py` / `check_deploy.py`） |

**M8 必须处理的老网缺口（已定位，非新引入）**：多套老用例用**裸 socket** 发握手行却**没带 `ver=<server/VERSION>`**，被中继版本门判 `VERSION_MISMATCH` —— 至少影响 `tests/room_settings_test.py`、`tests/v2_control_test.py`、`tests/host_offline_owner_test.py`（6 条）；修法统一：给这些握手行补 `ver=`。

> **[2026-10-05 M8 已修]** 三套的握手行全部补齐（`room_settings_test` 27 条、`v2_control_test` 25 条、`host_offline_owner_test` 3 条，`ver=` 一律挂行尾），**一条断言都没放宽**：
> `room_settings_test` **105 passed / 0 failed**、`v2_control_test` **100 / 0**、
> `host_offline_owner_test` **126 / 4**。剩的 4 条不是版本门造成的（它们直接调
> `_handle_core_event`，不发任何 socket）：`host 断开 + 空闲态`、`玩家正在重建时收到
> host_stopped / host_released / host_left` —— 是客户端 GUI 的 host_down / 重建守卫
> 旧缺口，改动落在 `client/**`（M8 范围外，留给客户端工作流）。

另一处：磁盘已有配置时「✓ 已生效：<值>」是插值文本，静态 i18n 键匹配不到（只在共享数据目录的跨套件污染下复现），收尾改成键驱动 —— **仍未做**。**BUILD.txt 是共享产物**：谁最后收工谁必须再跑一次 `python3 scripts/make_build.py --clean`，然后才跑门禁。

**开源发布前必须处理（安全，非 M8 代码）**：`panel/docker-compose.yml` 与 `server/docker-compose.yml` 里仍是**明文凭据**（v3.9.20 已记录并接受的风险，见 `SECURITY-NOTICE.md`）。推到公开仓库之前必须先 `python3 scripts/rotate_secrets.py` 轮换、再把 compose 改回 `${MCLINK_DB_PASSWORD:?}` 引用（`rebuild.sh` 的 `SECRETS_MODE=external`），否则等于把数据库与管理 API 一起公开。`SECURITY.md` 里也写了这一条（不含任何真实值）。

> **[2026-10-05 D5 已执行 · 本轮]** 上面这条已经不是待办了，记录一下做了什么：
>
> * **凭据外部化**：两个 compose 里只剩 `${MCLINK_DB_PASSWORD:?MISSING: ...}` /
>   `${MCLINK_ADMIN_API_TOKEN:?MISSING: ...}` 引用；`rebuild.sh` 回到
>   `SECRETS_MODE=external` 并且**删掉 hardcoded 分支**（明文一律判失败）。
>   `deploy/` 镜像里的同样两处也改了（门禁扫描整个仓库）。
> * **老部署升级路径**（D5 的"一次性升级时保留"）：`install.sh` 新增
>   `adopt_credentials_from_running` —— 优先级 `已有 .env 真值 > 运行中的
>   mc-relay / mc-panel 容器环境 > 随机生成`，读到的值写进 `.env`
>   （**不改数据库密码**）；`patch_composes` 改成只写**引用**（老部署的明文
>   副本会被就地改成引用，新包本来就是引用 = 空操作）。
> * **备案号**：`panel/docker-compose.yml` 改成 `${MCLINK_PANEL_ICP:-}`
>   （默认空 -> 页脚不渲染，符合 M1）；`config.sample.php` 与页脚注释里的
>   真实备案号换成占位符。
> * **真实标识符清洗**：真实域名（含 `mc.` / `www.` 子域）→ `example.com`；
>   审计文档里的真机公网 IP → `203.0.113.x`；`docs/DEPLOY_GUIDE.md` 里那行
>   `MYSQL_ROOT_PASSWORD=<真值>` 改成"从容器里读"；
>   `docs/client-logs/**` 的 9 份**真机日志**（含玩家公网 IP、client_id、
>   账号/房间名）移出仓库，只留 `docs/client-logs/README.md`（脱敏方法 +
>   合成示例），`.gitignore` 加了两道保险。
> * **回归网**：`tests/run_all.py` 里 3 条"硬编码是预期行为"的断言**翻转**
>   成新口径（引用存在、明文没有、`.env` 供值、`--strict-secrets` 绿、
>   `check_env_file` 的行为用真 bash 跑 6 种输入），`tests/install_sh_test.py`
>   与之配套翻转（compose 里是引用、不含真值）；新增
>   `tests/real_identifiers_guard_test.py`（域名/备案号/IP/明文凭据出现在跟踪树
>   就红，失败信息带文件:行号）。
> * **仍然待人做**：线上 `python3 scripts/rotate_secrets.py` 轮换那两个走过
>   旧包的值（旧值已外传，不轮换等于没修）；`client/**` 注释/文档串里的真机 IP
>   （`223.166.x.x` / `220.x.x.x` 那几条日志样例）留给客户端工作流拆片段；
>   `deploy/` 整棵树（含旧客户端源码里的域名、ICP）在打包前应跑一次
>   `python3 scripts/sync-deploy.py` 或干脆不进公开仓库。


**待交接（打包时必须做）**：
1. `deploy/panel/` 要带上两个新文件（`lib/panel_settings.php`、`views/settings.php`）；
2. 服务端把 `/health` 与状态文件改成真实 `RELAY_VERSION`（3.9.20），面板即自动显示真值；
3. `tests/panel_static_test.py` 的备案号断言改为"空则不渲染"（由服务端/测试工作流执行）；
4. 打包时为 `config/` 与 `data/panel-uploads/` 目录 `chown 65534:65534`，
   并把 `config/panel-settings.json` 加进 `.gitignore`（**已加**）。
5. M8 新增的根文件：`LICENSE`（GPLv3，D7 要求进仓库**与交付包**）、
   `CONTRIBUTING.md`、`SECURITY.md`、`CODE_OF_CONDUCT.md`、`README.html`。
   它们都进包；**`optional/` 不进包** —— `scripts/make_build.py` 已把整个目录排除，
   tar/zip 打包时也别手工塞进去。
6. 公开仓库之前：先轮换 `panel/docker-compose.yml` / `server/docker-compose.yml`
   里的明文凭据（`python3 scripts/rotate_secrets.py`）并改回 `.env` 引用
   （`SECRETS_MODE=external`），否则不要把仓库推公开。


* **面板工作流**（只动 `panel/`）：M1 面板部分 + M2 设置页/权限模型 + M3 配置读取。
  已确认：版本号 **2.11.45**（单一 `const PANEL_VERSION`，改号是三行的事）；
  ICP 默认空 → 页脚不渲染（**规格优先**，删掉硬编码的旧备案号，只在注释/文档里留档）；
  logo 上传用静态门禁白名单内的内置函数实现（magic byte 嗅探 + 文件读写），
  "仅 root 可改"必须在**服务端**强制。
* **服务端+客户端工作流**（`server/ client/ scripts/ tests/` + compose）：
  64 人上限、管理 API 版本字段、路径去硬编码、`install.sh`、`mclink` SSH 管理台。
  已确认：**管理 API 现在返回硬编码的 `2.0.0`**（`server/admin_api.py:358`、
  `server/relay_server.py:6615`）→ 必须改为读 `server/VERSION`（面板左上角显示的就是它）；
  `tests/panel_static_test.py` 里那条"页脚必须含备案号"的断言要**改为**"空则不渲染"。
* 面板读取中继版本：**API 优先**（`/health` 的 `version`），失败回退读 `server/VERSION`
  （路径可配置，不写死 `/opt/mc-link`）；两者不一致时在提示里显示出来。

