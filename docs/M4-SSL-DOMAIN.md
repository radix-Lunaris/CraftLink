# M4：SSL + 域名绑定 + 面板自带 nginx（去 1Panel 反代）

> 2026-10-05 · 面板工作流（`panel/**` + `scripts/**` + `docs/**`）
> 口径来源：`docs/OPENSOURCE-PLAN.md`（锁定版）M4 验收要点 + `docs/PANEL-SETTINGS.md` §10（nginx 接口）。
>
> 一句话：**面板容器自己跑 nginx，配置由「面板设置」渲染出来**；证书可以上传
> （离线可用）、可以自签（测试）、也可以交给 Let's Encrypt 自动申请与续期；
> 绑定域名之后只有该域名能访问；而且**任何一种配错都有一条本机急救通道**。

---

## 1. 交付内容（对应 M4 验收要点）

| M4 要点 | 落在哪 | 怎么验 |
|---|---|---|
| 上传证书（默认、离线可用）+ 热加载 | `panel/lib/panel_ssl.php`、`panel/bin/mclink-cert.sh`、`panel/web/views/settings.php` | `docs/panel-ssl-nginx-smoke.sh` 第 3、5 节（自签 + 导入 + `nginx -t` + reload） |
| Let's Encrypt 申请 / 续期（含 dry-run） | `panel/bin/mclink-cert.sh`（certbot）、`panel/bin/panel-agent.sh`（定时检查） | 冒烟第 5.6/5.7 节（桩 certbot：签发 / dry-run / **续期失败保住旧证书**） |
| 域名绑定后仅该域名可访问 | `panel/lib/panel_nginx.php`（Host 守卫 + PHP 兜底） | 冒烟第 4 节（`Host: evil.test` → 404，对域名 → 200） |
| 面板自带 nginx（去 1Panel 反代） | `panel/nginx/**`、`panel/bin/panel-agent.sh`、`panel/Dockerfile`、`panel/docker-compose.yml` | 冒烟第 2/3 节（**真 nginx** 跑渲染出来的配置，curl 一条条打） |
| 安全入口 / 未认证返回码落到模板 | `panel/nginx/templates/panel-*.template` | 冒烟第 4 节（入口内 200 / 入口外 404 / 403 / 444 / 200） |
| 不能因为配置错误把自己锁死 | 急救 server 块 + `mclink rescue/reset-entry/ssl-off` + 急救覆盖文件 | 冒烟第 4 节（急救入口）+ 第 6 节（急救命令） |

---

## 2. 链路：设置 → 模板 → 生效

```
  面板「设置」页（root 改 / 普通管理员默认只读 / 每条 root 放行开关）
        │  POST index.php?action=panel_settings_save|panel_ssl_*|panel_nginx_apply
        ▼
  panel_setting_save()                     ← lib/panel_settings.php（唯一写入口）
        │   数据库 panel_settings 表（主）+ config/panel-settings.json（备份）
        │   ── 数据库写不进去时 → <config>/panel-emergency-overrides.json（急救覆盖，优先级最高）
        ▼
  panel_agent_request('render'|'ssl-*')    ← lib/panel_agent.php
        │   写 <证书目录>/agent/requests/<id>.json（php-fpm 是 nobody，只能做到这里）
        ▼
  panel/bin/panel-agent.sh（root，supervisord 常驻，默认 2 秒一扫）
        ├─ php /var/www/bin/mclink-panel.php render --dir <候选目录>   ← 渲染（lib/panel_nginx.php）
        ├─ nginx -t -c /etc/nginx/mclink-nginx-test.conf              ← 候选配置试跑
        ├─ 通过 → 渲染到 /etc/nginx/mclink-servers → nginx -t（正式）→ nginx -s reload
        ├─ 不通过 → 回滚 / 保留上一份能跑的配置（**绝不把面板改成打不开**）
        ├─ mclink-cert.sh import|selfsigned|acme-issue|acme-renew     ← openssl / certbot
        │      └─ 装好证书 → mclink-panel.php set-cert（写设置）→ 再渲染一次
        └─ 写 <证书目录>/agent/results/<id>.json（面板读它显示成功 / 失败原因）
```

### 2.1 模板与渲染出来的文件

| 模板（`panel/nginx/templates/`，只读） | 渲染成 | 内容 |
|---|---|---|
| `panel-server.conf.template` | `panel-server.conf` | 三个 server 块：主 HTTP、HTTPS（可选）、**急救入口** |
| `panel-locations.inc.template` | `panel-locations.inc` | 安全入口剥离 + 未认证返回码 + ACME 通道 + `include panel-backend.inc` |
| `panel-backend.inc.template` | `panel-backend.inc` | PHP（fastcgi/proxy）+ 静态文件 + 敏感路径拒绝 |
| `panel-rescue-locations.inc.template` | `panel-rescue-locations.inc` | 急救入口的路径规则（**不判**入口 / 域名 / 跳转） |
| `panel-acme.inc.template` | `panel-acme.inc` | `/.well-known/acme-challenge/`（明文直达，不参与跳转） |
| `panel-http-redirect.inc.template` | `panel-http-redirect.inc` | `force_https` 打开时：ACME 放行 + 其余 301 |
| `panel/nginx/nginx-main.conf` | `/etc/nginx/nginx.conf` | 静态主配置（`http{}` 公共设置 + `include …/mclink-servers/*.conf`） |
| `panel/nginx/nginx-test.conf.template` | `/etc/nginx/mclink-nginx-test.conf` | 只给 `nginx -t` 用的测试主配置（不监听、不影响在跑的 nginx） |

**容器内端口是固定的**：HTTP `80`、HTTPS `443`、急救 `19103`。
`listen_ip` / `listen_port` 是**宿主机**这一侧的绑定地址，由 compose 的
`ports` 决定（`scripts/panel-apply.sh` 按设置写 `.env` 并重建容器）。
这样"改错端口"永远改不到"容器里没人听"这件事上。

### 2.2 安全入口与未认证返回码（口径）

| 配置 | 入口之外 / Host 不对时 |
|---|---|
| `url_suffix` 为空 | nginx 不做路径限制（一切交给 PHP，PHP 按未认证返回码回话）—— 老行为 |
| `url_suffix` = `/secret` | 只有 `/secret/...` 能到面板，其余路径按下面的码处理 |
| `unauthenticated_code` = `login` | 在**入口之外 / Host 不对**时按 **404** 处理（否则安全入口白配）；入口之内仍然是"显示登录页" |
| `404` / `403` / `444` / `200` | 原样返回（444 = nginx 直接断连接） |

> Host 判定**不**放行 `127.0.0.1` / `localhost`：Host 头是客户端随便写的，
> "允许 Host: 127.0.0.1" 等于允许所有人。要本机访问就走急救入口。

---

## 3. 自锁保护与恢复路径（三层）

1. **本机急救入口**：`http://127.0.0.1:19103/`（宿主机回环，公网到不了）。
   它**不判**安全入口、**不判**域名、**不跳** HTTPS，而且 `/healthz` 直接 200
   （Docker 健康检查也走它 —— 面板配置改得很严时健康检查不会误报）。
2. **SSH 管理台**（`mclink`，root）：
   * `mclink reset-entry` —— 清空安全入口（**真的写设置并热加载**，走面板 CLI）
   * `mclink set-entry [PATH]` —— 设一个新的（不带参数则随机生成）
   * `mclink unbind-domain` —— 解绑域名
   * `mclink ssl-off` —— 关掉 HTTPS（证书文件一个都不删）
   * `mclink ssl-status` —— 打印监听 / 入口 / 域名 / SSL / 证书到期 / nginx 生效状态
   * `mclink apply-network` —— 把监听地址 / 端口 / SSL 落到宿主机（重建面板容器）
   * `mclink rescue` —— 打印急救入口地址
3. **渲染/生效失败也不改坏的配置**：候选配置先 `nginx -t` 试跑；正式目录再
   `nginx -t` 一次，失败就回滚成上一份；`nginx -s reload` 只在测试通过之后发。
   **证书坏了不会让 nginx 起不来**：证书文件读不到时渲染器直接按 HTTP 渲染
   （页面上给一句为什么），而不是生成一份引用不存在文件、`nginx -t` 必挂的配置。

### 3.1 急救覆盖（emergency overrides）

面板设置以数据库为主，但有一种很难受的情况：**数据库能读、写不进去**
（只读账号 / 磁盘满 / 表被锁）。这时 `mclink reset-entry` 会退一步，把这一条写进
`<config>/panel-emergency-overrides.json` —— 它的优先级**高于数据库**，
面板设置页顶部会显眼地列出它，并提供一个 root 按钮清除；在设置页里把那一项
重新保存一次也会自动清掉它（数据库里已经有正式值了）。
它**不会**被写回 `panel-settings.json`，所以一次急救不会变成永久配置。

---

## 4. SSL：两种模式的操作步骤

### 4.1 上传证书（默认，离线可用）

```bash
# 服务器上（可选：先看当前状态）
mclink ssl-status

# 面板里：面板设置 → 域名与 SSL
#   1) 绑定域名             panel.example.com（或留空 = 不限制）
#   2) SSL 模式             上传证书（离线可用，推荐）
#   3) 证书文件 / 私钥文件  留空也行 —— 下面上传时会自动填上
#   4) 保存
#   5) 「上传证书并启用」选 fullchain.pem + privkey.pem → 上传
```

* 面板只做**结构校验**（PEM 头尾、拒绝加密私钥、大小上限），真正的
  **深度校验**（密钥是否配对 / 有没有过期 / 覆盖域名）由 root 代理用 openssl 做；
  校验不过 → **旧证书继续用**，结果里写明原因。
* 私钥安装后是 `0600 root`（nginx 的 master 是 root，读得到；php-fpm 读不到，
  这正是我们要的）。
* 也可以手填一个已经挂进容器里的路径（例如 certbot 的 `fullchain.pem`）——
  只要那个路径在容器里可读。

### 4.2 Let's Encrypt（可选）

前置条件（面板会把这些写清楚，做不到就别选它）：

1. `绑定域名` 填的是**具体域名**（HTTP-01 不支持 `*.example.com` 通配）；
2. 该域名解析到这台机器；
3. **80 端口**能从公网进来（`scripts/panel-apply.sh` 会在 SSL 打开时加上
   `0.0.0.0:80:80` 映射；如果 80 被 1Panel 的 nginx 占着，得先让出来）；
4. `Let's Encrypt 账号邮箱` 填了合法邮箱。

操作：

```
面板设置 → 域名与 SSL
  SSL 模式 = Let's Encrypt 自动申请 / 续期
  绑定域名 = 你的域名；邮箱 = 你的邮箱 → 保存
  「先用 dry-run 验证」（默认勾选）→ 用 Let's Encrypt 申请     ← 只验流程，不签发
  去掉勾选 → 用 Let's Encrypt 申请                              ← 正式签发并安装
  之后任何时候：检查并续期（一键续期）
```

**续期**：容器里的根代理每 6 小时（`MCLINK_ACME_CHECK_INTERVAL`）检查一次
证书到期时间，剩不到 30 天（`MCLINK_ACME_RENEW_DAYS`）就自动续期；容器启动时
也会查一次。certbot 的账号与续期状态放在**证书卷**里
（`<证书目录>/certbot`），所以重建容器不会丢。

**失败时怎么保住旧证书**：`certbot renew` 的退出码被单独取（不经过管道，
避免"管道退出码是 sed 的"这种吞错）；失败 → 写 `acme.json` 的 `last_ok:false`
+ 日志，**一个字节都不动**已安装的证书；成功 → 新证书先过一遍校验（能解析 /
密钥配对 / 未过期 / 内容与旧的不同）才原子替换。所以"续期失败掉 HTTPS"不会发生。

### 4.3 自签证书（只用于测试）

面板设置里点「生成自签证书（测试用）」：不需要域名解析、不需要 80 端口，
本机就能把 TLS 链路跑通（浏览器会提示不受信任，这是预期的）。
证书里带 `DNS:<域名>, DNS:localhost, IP:127.0.0.1` 三个 SAN。

### 4.4 强制 HTTPS

`force_https = 1` 时：HTTP 侧只保留 ACME 校验 + 301 跳转。
**证书没就绪（模式没选 / 路径空 / 文件读不到）时不会跳转** —— 否则等于把面板
跳到一个打不开的地址上。HSTS 故意不加（自签证书 + HSTS 会让浏览器把面板
彻底拉黑，那是另一种自锁）。

---

## 5. 与 `mclink` 管理台对接的接口

管理台不再自己改 `.env` 重启容器，而是调用容器里的**同一个写入口**：

```
mclink <命令>  →  docker exec mc-panel php /var/www/bin/mclink-panel.php <子命令>
```

| 管理台命令 | 面板 CLI | 效果 |
|---|---|---|
| `mclink reset-entry` | `reset-entry` | `url_suffix=''` + 重新渲染 + 热加载 |
| `mclink set-entry [P]` | `set-entry --path P` | 设置新的安全入口 |
| `mclink unbind-domain` | `unbind-domain` | `bound_domain=''` + 渲染 + 热加载 |
| `mclink ssl-off` | `ssl-off` | `ssl_mode=off` + 渲染 + 热加载（证书保留） |
| `mclink ssl-status` | `status` | 打印设置 / 证书 / nginx / 根代理状态 |
| `mclink apply-network` | （宿主侧）`scripts/panel-apply.sh` | 写 `.env` + 生成 80/443 override + 重建面板容器 |
| `mclink rescue` | （宿主侧读 `.env`） | 打印 `http://127.0.0.1:<急救端口>/` |

**兜底**：面板容器没跑 / 镜像是老版（没有 `mclink-panel.php`）时，管理台会退回
老行为（改 `.env` + 重启 + 明确提示"老镜像不读这个值"）。老行为的提示里会说清
真正的入口是急救通道。CLI 的 `status --json` 也给脚本用（`panel-apply.sh` 就读它）。

---

## 6. 本轮实际验证到什么程度

```
# 1) 语法 + 仓库门禁（静态检查）
find panel -name '*.php' | while read f; do php -l "$f"; done   # 全部 No syntax errors
python3 tests/panel_static_test.py                              # 276 passed / 0 failed

# 2) 面板设置自检（注册表 / 校验 / 权限 / 存储回退 / M4 新键）—— 206 项
php docs/panel-settings-selftest.php

# 3) 面板设置端到端冒烟（php 自带 server + curl）—— 74 项
PHP_BIN=php bash docs/panel-settings-smoke.sh

# 4) M4 专项冒烟（真 nginx + 自签证书 + 桩 certbot + 桩 docker）—— 157 项
PHP_BIN=php NGINX_BIN=/usr/sbin/nginx bash docs/panel-ssl-nginx-smoke.sh
```

`docs/panel-ssl-nginx-smoke.sh` 里**真的**验过的东西：

* **真 nginx**（1.28）跑渲染出来的配置：`nginx -t` 通过（**包括生产主配置
  `nginx/nginx-main.conf`**）；用 curl 打出来
  安全入口内外（200 / 404 / 403 / 444）、域名对不对（200 / 404）、
  ACME 明文通道（200 + 内容正确）、强制 HTTPS 跳转（301 + Location 带端口）、
  **HTTPS 握手**（自签证书，`openssl s_client` 看到的就是我们那张 CN）、
  急救入口（`/healthz` 200、登录页 200、Host 随便写）、`nginx -s reload` 热加载。
* **根代理整条链路**：请求文件 → 渲染 → `nginx -t` → 原子替换 → reload → 结果文件；
  `nginx -t` 失败时**生效配置一个字节都没动**。
* **证书**：自签生成（密钥 0600）、上传导入、`status.json`（面板读出到期天数 /
  签发者）、桩 certbot 的 dry-run / 正式签发 / **续期失败保住旧证书**。
* **急救**：`reset-entry` / `unbind-domain` / `ssl-off` / 清除急救覆盖，
  以及"数据库写不进去 → 写急救覆盖并标 `degraded=1`"这条兜底。
* **管理台对接**：用桩 `docker exec` 跑真的 `scripts/mclink-console.sh`，
  确认 `reset-entry` / `set-entry` / `unbind-domain` / `ssl-off` / `ssl-status`
  / `apply-network --dry-run` 都真的改到了设置（不是只打了一行字）。
* `scripts/panel-apply.sh`：真跑过 `--dry-run` 与 `--no-restart`，确认 `.env`
  只动网络键（凭据行原样保留）、override 在 `ssl_mode=off` 时不加 80/443。

### 6.1 只能在真机验证的（本地没有 docker / 域名 / Let's Encrypt）

| 项 | 为什么本地验不了 | 真机上怎么验（一条命令） |
|---|---|---|
| 容器构建（certbot / nginx / 权限） | 没有 docker | `cd <安装根>/panel && docker compose build` |
| 端口映射与 80/443 冲突 | 没有 docker、没有 1Panel | `mclink apply-network`，再看 `docker ps` 的 PORTS |
| 容器内渲染 + 热加载 | 没有 docker | `docker exec mc-panel php /var/www/bin/mclink-panel.php status` 与 `docker logs mc-panel` |
| 真的 Let's Encrypt 签发 / 续期 | 需要公网域名 + 80 端口 + LE 配额 | 面板里先 dry-run，再正式签；`docker exec mc-panel sh /var/www/bin/mclink-cert.sh inspect --cert … --key …` |
| 浏览器里的证书信任链 | 自签证书浏览器一定警告 | 用真证书打开面板，看锁头 |
| 重建容器后证书/ACME 状态还在 | 需要真卷 | `docker compose up -d --force-recreate` 后 `mclink ssl-status` |
| 宿主机 chown 65534 的权限 | 需要真宿主 | 按 `docker-compose.yml` 注释 `mkdir -p … && chown 65534:65534 …` |

---

## 6.2 目录与权限（一眼看懂谁写谁读）

| 路径（容器内） | 谁写 | 谁读 | 权限 |
|---|---|---|---|
| `/etc/nginx/mclink-servers/*.conf` | 根代理（root） | nginx master(root) + 面板（只读状态） | 644 |
| `/var/www/config/panel-settings.json` | php-fpm(nobody) | 面板、根代理 | 644（目录 65534） |
| `/var/www/config/panel-emergency-overrides.json` | root（`mclink`） | php-fpm | 644 |
| `/var/www/certs/agent/requests/` | **php-fpm** | 根代理 | 目录归 nobody（根代理启动时 `chown`） |
| `/var/www/certs/incoming/` | **php-fpm**（上传的证书） | 根代理 | 同上 |
| `/var/www/certs/*.crt` `/var/www/certs/*.key` | 根代理 | nginx master(root)；面板只读 crt | crt 644 / **key 600 root** |
| `/var/www/certs/status.json`、`agent/results/*.json`、`agent.log` | 根代理 | 面板 | 644 |

> 根代理在启动时会 `prepare_dirs()`：把 `agent/requests/` 与 `incoming/` 这两个
> **面板要写的目录** `chown` 成 php-fpm 的用户（默认 65534，可用
> `MCLINK_PANEL_WEB_UID` 覆盖）。没有这一步的话，root 先建出来的 755 目录会让
> 面板写不进去（表现是"队列目录不可写"）。

## 7. 运维速查

```bash
# 面板现在到底是什么状态（监听 / 入口 / 域名 / SSL / 证书 / 根代理）
mclink ssl-status

# 我进不去了
mclink rescue                 # 打印 http://127.0.0.1:19103/
curl -sS http://127.0.0.1:19103/healthz        # 应该回 ok

# 安全入口写错了
mclink reset-entry            # 清空（任何路径都能进）
mclink set-entry              # 随机生成一个新的

# 域名绑错了 / 换域名了
mclink unbind-domain

# 证书坏了 / 浏览器打不开
mclink ssl-off                # 回纯 HTTP，证书文件不动

# 改了监听端口 / 开了 SSL 之后让宿主机跟着变
mclink apply-network

# 面板里的 nginx 配置长什么样
docker exec mc-panel sh -c 'cat /etc/nginx/mclink-servers/panel-server.conf'
docker exec mc-panel sh -c 'tail -n 40 /var/www/certs/agent/agent.log'
```

---

## 8. 已知限制与遗留项

1. **80 / 443 不在基础 compose 里**：需要 SSL 时由 `scripts/panel-apply.sh`
   生成 `panel/docker-compose.panel-ssl.yml`。如果这两个端口被 1Panel 的 nginx
   占着，`docker compose up` 会报 `port is already allocated` —— 这是刻意的
   （不静默顶掉你正在跑的网站）。要么让出端口，要么改
   `MCLINK_PANEL_HTTP_PORT` / `MCLINK_PANEL_HTTPS_PORT`（但 HTTP-01 只认 80）。
2. **HTTP-01 only**：没有做 DNS-01，所以通配符域名 + Let's Encrypt 不支持
   （用上传证书，或者以后加 DNS 插件）。
3. **一次只能绑一个域名**（可以写 `*.example.com` 通配）。多域名的证书可以
   上传，但"哪些域名能访问面板"只认这一条设置。
4. **容器内监听端口固定**（80/443/19103）。这是刻意的（见 §2.1），
   代价是"想用 host 网络直接听 19102"这种玩法不支持。
5. **`mem_limit: 128m` 在 ACME 续期那一刻偏紧**（certbot 是单独的 python 进程，
   峰值约 60 MB）。真遇到 OOM 调到 256m；或者 `INSTALL_CERTBOT=0` 只上传证书。
6. **审计只记"改了哪一条"**：证书内容、私钥、ACME 密钥永远不进日志；
   安全入口的值也不进（只记"改了 / 清空了"）。
7. `tests/panel_static_test.py` 的内置函数白名单没有 `openssl_*` / `exec` /
   `sleep` 这些：所以证书的深度校验在 shell（openssl CLI）那一侧、
   CLI 的等待用忙等（每次几秒、调用频率极低）。要改成原生 PHP 实现，
   得先动测试网的白名单 —— 那属于测试工作流。
8. `deploy/**`（发布包）里的 `panel/` 是 `scripts/sync-deploy.py` 生成的：
   已经把它改成会带上 `panel/bin` 与 `panel/nginx`，但**发布前要重跑一次**
   `python3 scripts/sync-deploy.py`，否则包里没有根代理和模板。
