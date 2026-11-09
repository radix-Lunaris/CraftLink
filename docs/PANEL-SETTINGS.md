# 面板设置（M2 / M3）—— 实现说明、权限模型与验收清单

> 2026-10-05 · 面板工作流（只动 `panel/` 与 `docs/`）
> 口径来源：`docs/OPENSOURCE-PLAN.md`（锁定版）M1–M3 的**面板部分** + 用户本轮需求 1–10。
>
> 一句话：面板版本改成 **2.11.45**、仪表盘去掉「正在连接」、左上角显示**中继版本**、
> 新增**面板设置页**（root 全可改 / 普通管理员默认只读 / 每条一个只有 root 看得见的放行开关）、
> 设置**数据库为主 + 配置文件备份**、并把「自带 nginx」要用的四个值（监听 IP / 端口 /
> 安全入口 / 未认证返回码）读出来并校验好。
>
> **2026-10-05 更新（M4）**：§11 里"留给 M4"的六个键（`bound_domain` / `force_https` /
> `ssl_mode` / `ssl_cert_file` / `ssl_key_file` / `acme_email`）**已经实现**，
> 自带 nginx 也从"只读接口"变成了真的渲染 + `nginx -t` + 热加载。
> 新增的机制与操作步骤见 **[`docs/M4-SSL-DOMAIN.md`](M4-SSL-DOMAIN.md)**；
> 本文里凡是写着"下一个里程碑 / 未实现"的地方都以那份文档为准。

---

## 1. 交付内容（对应需求编号）

| # | 需求 | 落在哪 | 怎么验 |
|---|---|---|---|
| 1 | 面板版本号 2.0.0 → **2.11.45**，三处一致 | `panel/lib/common.php`（`PANEL_VERSION`，全站唯一一份）、页脚、`panel/Dockerfile` 的 image label、`panel/docker-compose.yml` 的 image tag | 登录面板看页脚 `v2.11.45`；设置页「面板版本：2.11.45」；`grep -rn 2.11.45 panel/` |
| 2 | 仪表盘去掉「正在连接」 | `panel/web/views/dashboard.php`（删掉那张卡；`web/live.php` **仍然**回 `starting_members`，数据没删，只是不渲染） | 打开仪表盘，卡片区里没有「正在连接」；`grep -c starting_members panel/web/views/dashboard.php` = 0、`panel/web/live.php` ≥ 1 |
| 3 | 左上角显示**中继版本** | `layout_header.php` + `common.php` 的 `relay_version_info()` | 登录后看左上角 `中继 <版本>`；读不到时显示 `中继 -`（鼠标悬停有来源说明） |
| 4 | 新页「面板设置」+ 逐条 root 放行开关 | `panel/lib/panel_settings.php`（新）、`panel/web/views/settings.php`（新）、`web/index.php` 的三个 POST 分支 | 见第 12 节验收清单 |
| 5 | 权限模型（服务端强制 + 判定函数） | `panel_setting_can_edit()` / `require_panel_setting_edit()` / `require_root()` | 见第 5 节 + 冒烟脚本第 6–7 节 |
| 6 | 自带 nginx 的配置读取层 | `panel_listen_config()` | 见第 10 节的接口约定 |

需求 5（域名绑定）与 6（SSL）**明确不做**，只留占位 —— 见第 11 节。

### 版本号的口径说明

锁定版计划里 M1 写的是 `2.0.0 → 2.1.0`，用户后来明确要求 **2.11.45**
（"面板版本号改成2.11.45吧开始迭代"），本轮按 **2.11.45** 实现；
计划文档由协调方同步改成 2.11.45。版本号只在 `common.php` 写一次，
页脚引用常量，Dockerfile / compose 各一处 —— 以后改版本是三个文件的事。

---

## 2. 文件清单

新增：

| 文件 | 作用 |
|---|---|
| `panel/lib/panel_settings.php` | 设置项注册表、校验、**DB + 配置文件双写**、权限判定、logo 上传与输出、监听配置读取 |
| `panel/web/views/settings.php` | 「面板设置」页（root 放行开关 / 普通管理员只读视图 / M4 占位） |
| `docs/panel-settings-selftest.php` | **可运行**自检（146 项，不需要数据库）：校验、权限矩阵、存储回退、logo 魔术字节、中继版本 |
| `docs/panel-settings-smoke.sh` | **端到端**冒烟（45 项，php 自带 server + curl）：渲染、未认证返回码五种配置、越权 POST 被拦、CSRF、中继版本回退 |
| `docs/PANEL-SETTINGS.md` | 本文 |

改动：

| 文件 | 改了什么 |
|---|---|
| `panel/lib/common.php` | `PANEL_VERSION` / `PANEL_DEFAULT_TITLE`；require 新库；会话时长与超时（`current_user()`）；`require_login()` 走未认证返回码；`require_root()`；中继版本取值 `relay_version_info()` / `relay_version_text()`；`relay_feature_check()` 多回一个 `version`；新增 5 个审计 action |
| `panel/lib/i18n.php` | 新文案的中英文；顺手补齐之前漏在英文表外的旧文案（房间页连接状态、仪表盘两条到期提示等），英文表从 506 条补到 664 条 |
| `panel/web/index.php` | `case 'logo'`（免登录取图）；`case 'settings'`；三个 POST 分支（保存 / 放行开关 / 初始化）；登录时写会话计时起点 |
| `panel/web/views/layout_header.php` | 站名/标签名取自设置；favicon（上传的 logo）；左上角中继版本标签；导航加「面板设置」 |
| `panel/web/views/layout_footer.php` | 版本号走常量；备案号改成「设置 > config > 空」，**空 → 整块不渲染**（去掉内置的硬编码备案号） |
| `panel/web/views/dashboard.php` | 删掉「正在连接」卡片 |
| `panel/web/login.php` | 站名 / 标签名 / favicon 跟设置一致 |
| `panel/web/assets/style.css` | 新类（`.relay-version` `.setting-*` `.logo-preview` `.logo-img` `.settings-storage`）+ 亮色覆盖 |
| `panel/Dockerfile` | image label 2.11.45；建好并 chown `/var/www/config`、`/var/www/data/panel-uploads` |
| `panel/docker-compose.yml` | image tag 2.11.45；两条卷（设置备份 / logo 上传）+ 两个容器内路径环境变量 + 权限说明 |
| `panel/config.sample.php` | 新增可选键 `relay.version_file`、`panel.settings_file`、`panel.upload_dir`（都不含凭据） |

---

## 3. 设置项参考表

| 键 | 页面上的名字 | 类型 | 默认值 | 生效方式 | 需求 |
|---|---|---|---|---|---|
| `listen_ip` | 面板监听 IP | text（IPv4/IPv6） | `127.0.0.1` | 自带 nginx（M3 服务端部分）读它；面板已能读+校验 | 2 |
| `listen_port` | 面板监听端口 | number 1–65535 | `19102`（当前部署的实际对外端口） | 同上 | 1 |
| `url_suffix` | URL 安全入口后缀 | text（字母数字点下划线短横线，≤64） | `''`（不用安全入口 = 老行为） | 同上 | 3 |
| `unauthenticated_code` | 未认证时的 HTTP 返回码 | select：`login`(默认) / `404` / `403` / `444` / `200` | `login`（显示登录页，老行为） | **面板已经在用**（未登录访问受保护页时），nginx 也会用 | 4 |
| `session_minutes` | 会话时长（分钟） | number 5–10080 | **`360`** | **立即生效**（每个请求都读） | 7 |
| `panel_brand` | 面板左上角名称 | text ≤96 字节 | `''` → config/MCLINK_PANEL_TITLE → `CraftLink 管理面板` | 立即生效（页面渲染时读） | 8 |
| `panel_tab_title` | 网页标签名 | text ≤96 字节 | `''` → 跟左上角名称一样 | 立即生效 | 8 |
| `panel_logo` | 面板 logo | image（上传） | `''` → 内置草方块 SVG | 立即生效（左上角 + favicon 同一张图） | 9 |
| `icp_registered` | 是否展示备案号 | select：`''`(自动) / `1`(显示) / `0`(不显示) | `''`（自动：有备案号才显示） | 立即生效 | 10 |
| `icp_number` | ICP 备案号 | text ≤64 字节 | `''` → config/MCLINK_PANEL_ICP → 空 | 立即生效；**空 → 页脚连那一块都不渲染** | 10 |
| `bound_domain` | 绑定域名（M4） | text（主机名，可 `*.` 通配） | `''`（不绑定） | 自带 nginx 渲染 + 热加载；PHP 再兜一层 | M4 |
| `force_https` | 强制 HTTPS（M4） | select：`0` / `1` | `0` | 同上（证书没就绪时不跳转） | M4 |
| `ssl_mode` | SSL 模式（M4） | select：`off` / `upload` / `selfsigned` / `acme` | `off` | 同上 | M4 |
| `ssl_cert_file` | 证书文件（M4） | text（容器内绝对路径） | `''` | 同上；上传证书时自动填 | M4 |
| `ssl_key_file` | 私钥文件（M4） | text（容器内绝对路径） | `''` | 同上；私钥 0600 root | M4 |
| `acme_email` | Let's Encrypt 邮箱（M4） | text（邮箱） | `''` | 申请 / 续期时用 | M4 |
| `<键>__admin_edit` | （root 才看得见的放行开关） | `0` / `1` | **全部 `0`（关闭）** | 立即生效 | 4 |

> 设置项里的文案全部走 `t()`（中英双语），键名是**数据**、永远不翻译。
>
> 页面上每一行还会写一句「当前生效：…」—— 设置留空时到底回退到了哪个值
> （config.php / 环境变量 / 内置默认）在页面上直接看得见，验收不用猜。

---

## 4. 存储：数据库为主 + 配置文件备份（D1）

### 4.1 数据库（主）

升级时**不需要迁移脚本**：面板第一次读 / 写设置时执行一次幂等的建表语句
（`panel_settings_ensure_schema()`，`CREATE TABLE IF NOT EXISTS`），失败就
静默回退到配置文件（读）/ 明确报错（写）。

```sql
CREATE TABLE IF NOT EXISTS panel_settings (
    name       VARCHAR(64)  NOT NULL,
    value      TEXT         NULL,
    updated_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                            ON UPDATE CURRENT_TIMESTAMP,
    updated_by VARCHAR(32)  NOT NULL DEFAULT '',
    PRIMARY KEY (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

* 写入：`INSERT ... ON DUPLICATE KEY UPDATE value = VALUES(value), updated_by = VALUES(updated_by)`
* 值为 `TEXT`（空串是**有效值**：清空备案号就是空；判定一律看"键在不在"，不用 `empty()`）
* 设置页底部有一个 root 专属按钮「初始化 / 从配置文件导入」＝再跑一次建表 +
  把配置文件里、数据库里还没有的键导进来（数据库曾经不可用时用它搬回去）。

### 4.2 配置文件（备份）

* JSON：`{"schema":1,"panel_version":"2.11.45","updated_at":...,"updated_by":...,"settings":{...}}`
* 写入方式：原子写（先写 `.tmp` 再 `rename`），文件里**不含任何凭据**
* 路径候选（第一个存在的用于读，第一个可写的用于写）：
  1. 环境变量 `MCLINK_PANEL_SETTINGS_FILE`（最明确）
  2. `config.php` 的 `panel.settings_file`
  3. 环境变量 `MCLINK_CONFIG_DIR` + `/panel-settings.json`
  4. 环境变量 `MCLINK_ROOT` + `/config/panel-settings.json`（**没有硬编码 `/opt/mc-link`**，那只是一个排在后面的默认候选）
  5. `/var/www/config/panel-settings.json`（容器里推荐的挂载点，compose 已挂）
  6. `/opt/mc-link/config/panel-settings.json`（D8 的默认安装根，仅默认值）
  7. `panel/config/…`、`<仓库根>/config/…`（仓库 / 开发布局）
* 读：DB 有值就用 DB 的值，没有的键用文件里的，再没有用注册表默认值
  → "数据库不可用 = 只读回退到配置文件"就是这条。
* 写：**先写数据库，成功之后才写配置文件**。数据库写失败时**不写文件**、
  原样报错 —— 否则会出现"库里的旧值 / 文件里的新值"两份真相。
  文件写失败只是**警告**（设置已经进库了），页面上会明说。

### 4.3 权限（宿主机）

php-fpm 以 `nobody(65534)` 运行，所以宿主机上这两个目录要先建好并 chown：

```bash
mkdir -p config data/panel-uploads
chown 65534:65534 config data/panel-uploads
```

镜像里已经建好并 chown（不带卷启动也能用），但挂上宿主机目录后以宿主机的
权限为准。设置页顶部会直接显示"配置文件备份：<路径>（存在 / 还没有，可写 / 不可写）"。

---

## 5. 权限模型（服务端强制）

### 5.1 判定函数（唯一口径）

```php
panel_setting_can_edit(string $key, ?string $role = null): bool
//   root  → 永远 true（包括那条放行开关本身）
//   admin → 该条的 <键>__admin_edit 开关是不是 '1'（默认 '0'）
//   player→ false

panel_setting_can_toggle_admin_edit(string $key, ?string $role = null): bool  // 只有 root
panel_setting_page_visible(?string $role = null): bool                       // root + admin
panel_setting_admin_edit_enabled(string $key): bool                          // 放行开关当前状态
panel_setting_admin_edit_key(string $key): string                            // '<键>__admin_edit'
```

### 5.2 判定矩阵（每条设置 × 角色 × 开关状态）

| 角色 | 放行开关 | 能看到设置页 | 能改这一条 | 能拨放行开关 |
|---|---|---|---|---|
| root | 关（默认） | ✅ | ✅（root 不需要放行） | ✅ |
| root | 开 | ✅ | ✅ | ✅ |
| admin | 关（默认） | ✅（只读） | ❌ | ❌ |
| admin | 开 | ✅ | ✅ | ❌ |
| player | — | ❌（403） | ❌ | ❌ |

`docs/panel-settings-selftest.php` 第 3 节把这张表**逐条断言**了一遍
（10 条设置 × 3 角色 + 放行开关前后）。

### 5.3 在哪儿强制

| 层 | 位置 | 作用 |
|---|---|---|
| 页面 | `views/settings.php` | 没权限时控件 `disabled`、不渲染返回给普通管理员的放行按钮（**只是方便**） |
| 控制器 | `web/index.php` → `case 'panel_settings_save'` → `require_panel_setting_edit($key)` | **真正的闸门**：一次只改一条，每次 POST 都按"这条 × 当前角色 × 放行开关"判定，没过就 403 + 提示 + 跳回设置页 |
| 控制器 | `case 'panel_setting_admin_edit'` → `require_root()` | 放行开关本身只归 root |
| 控制器 | `case 'panel_settings_init'` → `require_root()` | 初始化 / 导入只归 root |
| 控制器 | 整个页面 → `require_admin()` | 玩家看不到设置页 |
| 校验 | `panel_setting_validate()` | 越界 / 垃圾值在写库之前就被拒（页面上的 min/max 只是方便） |
| CSRF | 全局 `check_csrf()` | 所有写操作都带 CSRF；没有 token 的 POST 直接 400 |

### 5.4 审计

新增 5 个 action（都进「面板操作」分类）：`panel_setting_changed`、
`panel_setting_grant`、`panel_setting_revoke`、`panel_settings_init`、
`panel_logo_changed`。**值不进日志**：`url_suffix`（路径级秘密）只记"更新了 /
清空了"，logo 只记"更新了"，绝不记文件名内容。

---

## 6. 未认证返回码（隐藏面板特征）

`panel_unauthenticated_reply()`（在 `require_login()` 里被调用）：

| 配置 | 行为 |
|---|---|
| `login`（默认） | 302 跳到登录页 —— **和以前一模一样** |
| `404` / `403` | 只发状态码 + **空正文**，看不出这里有一个管理面板 |
| `444` | 发 444 + 空正文；真正"直接断连接"由自带 nginx（M3 服务端部分）完成，PHP 这边把状态码原样发出去 |
| `200` | 200 空页 |

**登录页本身不受影响**（`index.php?action=login` / `login.php` 不走这个判定），
否则配了隐藏码之后谁都登不进来。安全入口（`url_suffix`）才是隐藏入口的正解，
由自带 nginx 按第 10 节的接口执行。

---

## 7. 会话时长

* 设置键 `session_minutes`，默认 **360** 分钟；范围 5–10080。
* 生效点：`start_session()`（cookie 生命周期 + `session.gc_maxlifetime`）
  和 `current_user()`（空闲超时判定，每次请求刷新 `last_seen_at`）。
* 升级前建立的老会话没有 `last_seen_at`：第一次访问补上时间戳，从那之后
  才开始计时 —— 升级本身不会把所有人踢下线。
* 改完立即生效（下一个请求开始按新时长算），**不需要重启容器**。

---

## 8. logo（左上角 + favicon 同一张图）

* 上传 → 存到上传目录（候选路径见 `panel_logo_dir_candidates()`：
  `MCLINK_PANEL_UPLOAD_DIR` → `config.php` 的 `panel.upload_dir` →
  `<MCLINK_ROOT>/data/panel-uploads` → `/var/www/data/panel-uploads` →
  `/opt/mc-link/data/panel-uploads`（默认值） → 仓库 / 容器兜底路径）。
* 文件名叫 `logo-<16 位随机十六进制>.<ext>`，**类型只看魔术字节**：
  PNG / JPEG / GIF / WebP / ICO 通过，**SVG 拒绝**（内嵌脚本），
  其它一律拒绝；上限 1 MB。
* 输出走 `index.php?action=logo&v=<filemtime>`：**不需要登录**（登录页的
  favicon 也要用它），带 `Content-Type` / `Content-Length` /
  `Cache-Control: public, max-age=300` / `X-Content-Type-Options: nosniff`。
  地址里带 mtime，换图之后浏览器不会拿到旧图。
* 没上传时：页头还是内置的等宽草方块内联 SVG，页面上**不发 favicon 标签**
  （浏览器用默认图标）—— 也就是"默认当前"。
* 清空：设置页上的「清除」按钮 → 删文件 + 清空设置。

---

## 9. 左上角的中继版本

取值顺序（`relay_version_info()`，实现按需求"**能用接口就用接口**"）：

1. 中继管理接口 `/stats` 的 `relay.version`（中继以后加了这个字段就优先用它）
2. 中继管理接口 `/health` 的 `version`（**现在的接口字段**）
3. 包内 `VERSION` 文件 —— 路径候选：`MCLINK_RELAY_VERSION_FILE` →
   `config.php` 的 `relay.version_file` → `MCLINK_ROOT/current/server/VERSION`
   → `MCLINK_ROOT/server/VERSION` → `/var/www/relay/VERSION`（容器推荐挂载点）
   → `/opt/mc-link/current/server/VERSION`（默认值） → 仓库 / 发布包布局。
   **绝不写死 `/opt/mc-link`。**
4. 都读不到 → 显示 `中继 -`（**绝不编一个版本号**）

只有"看起来像版本号"的内容才算数（`^[A-Za-z0-9][A-Za-z0-9._+-]{0,31}$`），
所以不会把一行日志当成版本号显示。

**已发现的缺陷（已上报给服务端工作流）**：`server/admin_api.py` 的 `/health`
把 `version` 写死成 `"2.0.0"`（`server/relay_server.py` 的状态文件同样），
而中继真正的版本由 `server/VERSION` 决定（当前 `3.9.20`，`relay_config.RELAY_VERSION`
用它做版本门）。所以在这台服务器上按"接口优先"会显示 `中继 2.0.0`。
服务端把 `/health` 改成回 `RELAY_VERSION` 之后，页面自动显示真版本；
在那之前，鼠标悬停在版本标签上会写出"接口报的版本和包内 VERSION 文件不一致"
（页面不隐藏这个矛盾，也不擅自改取值顺序）。

---

## 10. 自带 nginx（去 1Panel 依赖）的接口

> **M4 更新**：这一节描述的读取层仍然有效（`panel_listen_config()` 还是唯一的
> 取值口径），但"真正的 nginx 模板"已经落地在面板这一侧了：见
> `docs/M4-SSL-DOMAIN.md` §2 与 `panel/nginx/templates/**`。
> 下面这段接口约定没变，所以服务端 / 其它工具照旧可以只读 `panel-settings.json`。

面板这一层**只做读取 + 校验**（`panel_listen_config()`），真正的 nginx 容器 /
模板改动留给服务端工作流。数据形状就是下面这个数组，也是配置文件
`config/panel-settings.json` 里那几个键：

```php
[
  'ip'           => '127.0.0.1',   // listen_ip
  'port'         => 19102,         // listen_port
  'entry_suffix' => '',            // url_suffix（不带斜杠；'' = 不用安全入口）
  'entry_path'   => '',            // '' 或 '/<suffix>'
  'unauth_code'  => 'login',       // login / 404 / 403 / 444 / 200
  'nginx_listen' => '127.0.0.1:19102',
]
```

服务端 nginx 模板要用的就是这四件事：

1. `listen <ip>:<port>;`（`nginx_listen`）
2. 安全入口：`location /<entry_suffix>/ { … fastcgi_pass 127.0.0.1:9000; }`，
   其余路径 `return <unauth_code>;`（`444` 就是"直接关连接"，不是合法的
   HTTP 状态码，nginx 自己认识它；`200` 用 `return 200;` 空正文）
3. 未认证返回码同时是**面板自己**的行为（第 6 节），所以"nginx 没拦住"时
   面板也不会漏出登录页
4. 值从哪来：面板设置的**配置备份 JSON**（`config/panel-settings.json`，
   键在 `settings` 对象里）或 `panel_settings` 表 —— 服务端只读文件即可，
   不需要连数据库、也不需要面板的 token

校验规则在服务端也要再写一遍（面板只是尽力把坏值挡在门外）：
`listen_ip` 必须是合法 IP；`listen_port` 1–65535；`url_suffix` 只能
`[A-Za-z0-9._-]{1,64}`；`unauth_code` 只能是那五个之一。

---

## 11. 留给 M4（SSL / 域名）的东西 —— ✅ M4 已实现（2026-10-05）

> 下面这段是 M3 当时的口径，保留作为历史记录。六个键现在都在
> `panel_setting_defs()` 里、有校验、有 DB+JSON 双写、有逐条 root 放行开关；
> 实现与操作步骤见 `docs/M4-SSL-DOMAIN.md`。

设置页上有「域名与 SSL（下一个里程碑，暂未实现）」一节，只列出**键名**和一句
说明，**不提供任何输入控件、不写任何值**（避免"半实现"）：

* 域名绑定：`bound_domain`、`force_https`
  → 绑定之后只允许用该域名访问面板，直接 IP 访问按第 6 节的返回码处理
* SSL：`ssl_mode`、`ssl_cert_file`、`ssl_key_file`、`acme_email`
  → 上传证书（离线可用）+ ACME 自动申请 / 续期

代码里对应的钩子是 `panel_setting_placeholders()`；`panel_setting_defs()` 里
刻意**没有**这些键，所以它们现在既不能读也不能写。

---

## 12. 人工验收清单（点哪个页面，看什么）

> 前置：面板 2.11.45 已上传 / 重建（`bash rebuild.sh panel`，不断开玩家），
> 数据库是可用的（默认路径）。root 账号 + 一个普通管理员账号 + 一个玩家账号。

### A. 版本号（需求 1）

1. 登录面板，拉到页脚 → 看到 `CraftLink 管理面板 v2.11.45`。
2. 进「面板设置」→ 顶部「存储状态」里写着 `面板版本：2.11.45`。
3. 浏览器标签标题 = 设置的「网页标签名」（默认 `仪表盘 - CraftLink 管理面板`…）。
4. 服务器上 `docker inspect mc-panel --format '{{index .Config.Labels "org.opencontainers.image.version"}}'` → `2.11.45`。

### B. 仪表盘（需求 2）

5. 打开「仪表盘」→ 第一排卡片是：在线房间 / 房间内在线 / 已加入世界 /
   已连接（按成员状态）/ 空闲隧道 / 在线账号 —— **没有「正在连接」**。
6. 停在一个开着服的环境里等 10 秒（自动刷新）→ 数字照常跳，「正在连接」不会冒出来。

### C. 左上角中继版本（需求 3）

7. 页头左上角，品牌名右边有一个小药丸：`中继 3.9.20`（服务端 `VERSION` 的值）。
   * 如果显示 `中继 2.0.0`：说明中继接口还报着写死的版本号 —— 鼠标悬停会看到
     "接口上报的版本（2.0.0）和包内 VERSION 文件（3.9.20）不一致"，等中继更新即可。
   * 如果显示 `中继 -`：中继没回应、也没读到 VERSION 文件 → 按下面两件事排查：
     `docker exec mc-relay cat VERSION` 有没有值；
     `docker exec mc-panel cat /var/www/relay/VERSION`（没挂卷就看不到，属正常）。
8. 悬停药丸 → tooltip 写出**来源**（`中继管理接口（/health）` / `中继 VERSION 文件：…`）。
9. 停掉中继容器，刷新页面 → 显示 `中继 -`，页面其它部分照常（不报 500）。

### D. 面板设置：可见性 / 权限（需求 4、5）

10. **root** 登录 → 导航里有「面板设置」；打开：
    * 顶部「存储状态」：数据库可用（N 条设置）+ 配置文件备份路径（可写）；
    * 每一行右边都有一个 `只有 root 可改 / 普通管理员可改` 药丸 + 一个按钮；
    * 底部有「初始化 / 从配置文件导入」；
    * 「域名与 SSL（下一个里程碑，暂未实现）」只列键名 + `未实现`。
11. **普通管理员**登录 → 导航里也有「面板设置」，能打开，但：
    * 输入框是灰的（disabled）、没有「保存」按钮；
    * 每一行写着「只读：这一项现在只允许根管理员修改。」；
    * **看不到**任何「放行给普通管理员」按钮。
12. **玩家**登录 → 导航里没有「面板设置」；直接敲 `index.php?action=settings` → 403「需要管理员权限。」。

### E. 逐条放行开关（需求 4）

13. root 在「会话时长」那一行点「放行给普通管理员」→ 提示"已放行…"，
    药丸变成 `普通管理员可改`。
14. 换普通管理员登录（或刷新）→ 该行输入框变成可编辑、出现「保存」按钮；
    其它行仍然是只读。
15. 普通管理员把会话时长改成 60 保存 → 提示"已保存「会话时长（分钟）」。"。
16. root 点「收回放行」→ 普通管理员再刷新 → 该行又变回只读。
17. root 重新登录 → 「操作日志」页能看到 `修改面板设置` / `放行普通管理员修改面板设置` /
    `收回普通管理员修改面板设置的权限` 三种记录，操作者是**具体账号**、来源 IP 是浏览器 IP。

### F. 每条设置的生效（需求 1–10）

18. **左上角名称 + 网页标签名**：改成 `我的服务器` / `我的服务器面板` → 保存 →
    页头文字和浏览器标签立刻变（不用重启）。
19. **logo**：上传一张 PNG → 页头图标变成它，浏览器标签图标也是**同一张**；
    刷新、换页面都在；点「清除」→ 回到内置草方块，favicon 标签消失。
    故意上传一个 `.svg` 或改名的假图片 → 明确报错（只支持 PNG/JPEG/GIF/WebP/ICO）。
20. **ICP**：把「是否展示备案号」设为「不显示」→ 页脚备案号那一块**整个消失**（版权行还在）；
    设回「自动」+ 填一个号 → 备案号以**纯文本**出现在页脚（不是链接）。
    清空备案号 → 页脚只剩版权行。
21. **会话时长**：先设成 5（最小值），登录后在设置页里放一会儿（>5 分钟）不动，
    再点任意页面 → 跳回登录页；重新登录后把设置页顶部的值改回 360。
    （想快验：把服务器时间/`session_minutes` 调小后，用 `docker exec mc-panel ls /var/lib/php/sessions` 看会话文件被清掉。）
22. **未认证返回码**：设成 `404` → 退出登录 → 直接访问 `index.php?action=dashboard`
    → 浏览器显示 **404 且页面空白**（不是登录页）；设成 `403` → 403；设成 `444` →
    连接被直接关闭（curl 报空回复）；设成 `200` → 200 空页；设回 `login` → 又回到登录页。
23. **监听 IP / 端口 / 安全入口**：在设置页里改 → 保存成功；页面底部「网络与安全」
    那一行同步显示新的 `监听：…；安全入口：…；未认证返回码：…`。
    真正让新端口 / 安全入口生效属于服务端 nginx（M3），**这一轮不改容器**。

### G. 数据库不可用时的兜底（D1）

24. 停掉数据库（或把面板的 `MCLINK_DB_HOST` 指到连不上的地址）→ 打开设置页：
    顶部写「数据库：不可用 —— 现在只读回退到配置文件备份，保存会报错」，
    但**设置页仍然能打开**，各项显示的是配置文件里的值（或默认值）。
25. 此时保存任意一项 → 明确报「保存失败：无法写入数据库…」，**不要**出现"看起来成功了"。
26. 恢复数据库 → root 点「初始化 / 从配置文件导入」→ 提示导入了 N 条。

### H. 向后兼容

27. 老页面照旧：仪表盘 / 房间（强制关闭、踢人）/ 账号 / 操作日志 / 我的账号 /
    语言切换（中文 ↔ English）/ 主题切换（亮色 ↔ 暗色）；英文界面下**没有中文漏出**。
28. `config.php` 里原来的 `panel.title` / `panel.icp` 仍然有效（设置页留空时用它们）。
29. 升级前已经登录的浏览器**不会**被踢下线（老会话没有计时起点，第一次访问补上）。

---

## 13. 自动化验证（本轮实际跑过的）

```bash
# 1) 语法：所有 PHP 文件
find panel -name '*.php' | while read f; do php -l "$f"; done

# 2) 仓库门禁（静态检查，261 项）—— 本轮 261 passed / 0 failed
python3 tests/panel_static_test.py

# 3) 面板设置自检：注册表 / 校验 / 权限矩阵 / 存储回退 / logo / 中继版本（146 项）
php docs/panel-settings-selftest.php

# 4) 端到端冒烟：php 自带 server + curl，渲染 + 未认证返回码 + 越权 POST + 中继版本（45 项）
PHP_BIN=php bash docs/panel-settings-smoke.sh

# 5) 部署门禁（不属于本工作流；本轮只有 BUILD.txt 清单过期，见下）
python3 scripts/check_deploy.py
```

本轮的实测结果：

* `php -l`：`panel/**/*.php` + 两个 docs 脚本，全部 `No syntax errors`（PHP 8.5 CLI；目标运行时 8.2）
* `tests/panel_static_test.py`：**261 passed / 0 failed**
  （改动前基线是 252 passed / 1 failed —— 英文表漏了 5 条文案，本轮一起补齐）
* `docs/panel-settings-selftest.php`：**146 项通过 / 0 失败**
* `docs/panel-settings-smoke.sh`：**45 项通过 / 0 失败**
* `scripts/check_deploy.py`：唯一未通过的是 `BUILD.txt 覆盖全部文件`
  （356 缺 / 25 多）—— BUILD.txt 是发版时由 `scripts/sync-deploy.py` 重新生成的
  文件清单，属于部署工作流；本轮新增的
  `panel/lib/panel_settings.php`、`panel/web/views/settings.php` 需要在下一次
  `sync-deploy` 时进包（`deploy/panel/**` 也要同步）。

---

## 14. 口径不清的地方与我的选择

| # | 模糊点 | 我的选择 | 理由 |
|---|---|---|---|
| 1 | 计划 M1 写 2.1.0，用户要求 2.11.45 | 按 **2.11.45**（协调方确认，计划同步改） | 用户口径优先 |
| 2 | "未认证时的 HTTP 返回码"要不要包含"显示登录页" | 默认 `login`（= 现在的行为），另外提供 404/403/444/200 | 默认值必须向后兼容：老部署升级后行为不变 |
| 3 | 444 在 PHP 里能不能真的"断开连接" | PHP 只发 444 + 空正文；真正断连接由自带 nginx 做 | 444 是 nginx 的私有语义，PHP 没有等价物；写清在文档里 |
| 4 | `url_suffix` 面板要不要自己强制校验请求路径 | **不**在面板里强制（只读取 + 校验 + 提供给 nginx） | 真实部署里 1Panel 反代会把前缀从 URI 上剥掉（见 `docs/HIDE_PANEL_ENTRY.md`），面板看不到前缀就会把所有请求拒掉 = 自锁 |
| 5 | ICP 默认值 | **默认空 → 页脚不渲染**（去掉内置的 `你的备案号` 兜底）；老部署由 `MCLINK_PANEL_ICP` 提供值，行为不变 | 需求 10 + M1「ICP 可配置空则隐藏」 |
| 6 | 普通管理员"可见但不可编辑"具体长什么样 | 输入框 disabled + 只读说明 + 没有保存按钮；**不隐藏**那一行 | 需求原话"可见但不可编辑"；藏起来会让人以为面板坏了 |
| 7 | 放行开关放在哪一层 | 和设置同一张表（键 `<键>__admin_edit`），默认全 `0` | 复用同一套存储与审计，不引入第二张表 / 第二种权限源 |
| 8 | logo 存哪 | 磁盘文件（可配置目录）+ DB/JSON 里只存**文件名**，输出走 `index.php?action=logo` | 图片不进数据库 / JSON（会把备份文件撑大）；也不用给 nginx 加目录映射，反代前缀下地址依然是相对地址 |
| 9 | logo 允许的格式 | PNG / JPEG / GIF / WebP / ICO；**不收 SVG** | SVG 能内嵌脚本，面板的图片地址是免登录可访问的 |
| 10 | 会话时长是"绝对到期"还是"闲置到期" | **闲置到期**（每次请求刷新 `last_seen_at`） | "会话时长"通常指闲置多久掉线；绝对到期会把正在操作的人也踢掉 |
| 11 | 中继版本：接口写死 2.0.0，文件是 3.9.20 | **仍按接口优先**（需求明确），但页面用 tooltip 指出不一致 | 不擅自改口径；服务端修 `/health` 后自动正确 |
| 12 | 设置名 / 标签名留空是什么语义 | 留空 = 用 config.php / 环境变量里的值（再不行用内置默认） | 保证"默认 = 当前值"，改坏了清空即可恢复 |
| 13 | 面板 compose 里那两条明文凭据 | 原样保留（D5：老用户的凭据一次性保留） | 换成 `${...}` 会让老部署直接起不来；M5 安装器写 `.env` 时统一处理 |

---

## 15. 已知限制 / 待办（明确不在本轮）

* ~~监听端口 / 监听 IP / 安全入口**改完不会立刻生效**~~ —— **M4 已解决**：
  安全入口 / 未认证返回码 / 域名 / SSL **改完几秒内自动生效**（root 代理渲染 +
  `nginx -t` + 热加载）；只有 `listen_ip` / `listen_port` 还需要
  `mclink apply-network` 重建容器（宿主机端口映射是 compose 的事）。
* `panel_settings` 表由面板自己懒加载创建；如果面板账号没有 `CREATE` 权限，
  初始化会明确报错，这时可以先在数据库里手工执行第 4.1 节的 SQL。
* 配置文件备份的权限由部署时的 `chown 65534:65534` 保证（面板不调用 `chmod`）。
* 面板目前没有单元测试框架，所以本轮的可验证手段是：`php -l` +
  仓库静态门禁 + 上面两个**可运行**脚本（自检 / 冒烟）+ 本清单。
