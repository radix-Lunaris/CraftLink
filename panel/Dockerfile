# ============================================================
#  CraftLink Admin Panel  (PHP 8.2 + nginx, 独立容器)
# ============================================================
#
#  为什么不用服务器上现成的 web-php82？
#    web-php82 只有 php-fpm（端口 9000），需要 1Panel 那边额外配 nginx 代理，
#    而且不能把面板代码塞进正在运行的生产容器里。
#
#  M3/M4 之后这个容器是**自包含**的：nginx + php-fpm 都在里面，而且 nginx 的
#  站点配置由「面板设置」渲染（不再依赖 1Panel 的反向代理）：
#
#      /etc/nginx/nginx.conf              静态主配置（本文件 COPY 进去）
#      /etc/nginx/mclink-servers/*.conf   面板设置渲染出来的 server 块
#      /etc/nginx/mclink-nginx-test.conf.template  给 `nginx -t` 试跑候选配置用
#      /var/www/nginx-templates/          渲染模板（只读）
#      /var/www/bin/panel-agent.sh        root 代理：渲染 → nginx -t → 热加载
#      /var/www/bin/mclink-cert.sh        root 代理：openssl / certbot
#      /var/www/bin/mclink-panel.php      CLI：mclink 管理台与代理共用同一个写入口
#
#  网络（端口在容器内是固定的，宿主机映射由 compose 决定）：
#    80    HTTP（面板主入口；宿主机映射成 listen_ip:listen_port）
#    443   HTTPS（ssl_mode != off 时）
#    19103 急救入口（**只发布到宿主机回环**：安全入口 / 域名 / 证书配错时进去改）
#    * 1panel-network -> 连 MySQL(web-mysql80) 和中继(mc-relay)
#
#  [!] 关于 apk 源：
#    Alpine 官方 CDN（dl-cdn.alpinelinux.org）在国内经常 TLS 超时，
#    一次构建能卡 8 分钟。这里默认换成阿里云镜像源（阿里云 ECS 走内网，
#    通常几十秒完成）。
#
#    如果你的服务器不在阿里云，或者想换别的源，构建时覆盖即可：
#      docker compose build --build-arg APK_MIRROR=mirrors.tuna.tsinghua.edu.cn
#      docker compose build --build-arg APK_MIRROR=dl-cdn.alpinelinux.org
#
#  [!] certbot（Let's Encrypt 申请 / 续期）体积不小。不用 ACME、只要上传证书
#      的部署可以省掉它：
#      docker compose build --build-arg INSTALL_CERTBOT=0
#      （面板上选 ACME 时会明确报「镜像里没有 certbot」，其余功能不受影响）
# ============================================================

FROM php:8.2-fpm-alpine

LABEL org.opencontainers.image.title="mc-link-panel"
LABEL org.opencontainers.image.version="2.11.45"

ENV TZ=Asia/Shanghai

# 可覆盖：阿里云 / 清华 / 官方
ARG APK_MIRROR=mirrors.aliyun.com

# 1 = 装 certbot（ACME 申请 / 续期要用）；0 = 不装
ARG INSTALL_CERTBOT=1

# ---- 换源（必须在 apk add 之前）----
RUN set -eux; \
    if [ "$APK_MIRROR" != "dl-cdn.alpinelinux.org" ]; then \
        sed -i "s|dl-cdn.alpinelinux.org|${APK_MIRROR}|g" \
            /etc/apk/repositories; \
    fi; \
    echo "== 使用的 apk 源 =="; \
    cat /etc/apk/repositories

# ---- 系统依赖 ----
#   nginx        : 直接在本容器里提供 HTTP / HTTPS（M3：去掉 1Panel 反代）
#   openssl      : 证书生成 / 校验（面板自签、上传校验、ACME 部署都靠它）
#   supervisor   : 同时跑 nginx + php-fpm + 根代理
#   curl-dev     : 编译 php curl 扩展（调用中继管理 API）
#   oniguruma-dev: 编译 mbstring
#   certbot      : Let's Encrypt 申请 / 续期（可选，见 INSTALL_CERTBOT）
RUN set -eux; \
    apk add --no-cache \
        nginx \
        openssl \
        supervisor \
        curl-dev \
        oniguruma-dev \
        tzdata; \
    if [ "$INSTALL_CERTBOT" = "1" ]; then \
        apk add --no-cache certbot; \
    fi; \
    docker-php-ext-install -j"$(nproc)" pdo_mysql curl mbstring; \
    rm -rf /var/cache/apk/*

# ---- 面板代码 ----
# 目录结构必须和源码一致：
#     /var/www/lib/     <- lib/ 的内容（在 web 根目录之外，HTTP 访问不到）
#     /var/www/panel/   <- web/ 的内容（nginx 的 root）
#     /var/www/bin/     <- bin/ 的内容（root 代理 + CLI + 证书工具）
#
# 代码里写的是 `require '/var/www/lib/common.php'`（或按 __DIR__ 推），
# 所以 __DIR__ 是 /var/www/panel 时必须存在 /var/www/lib。
#
# [!] 这里踩过一次坑：如果写成 `COPY web /var/www/panel`，只有 web/ 的内容
#    进容器，lib/ 整个丢失，页面会 500 并报
#    "Failed opening required '/var/www/panel/../lib/common.php'"。
COPY lib /var/www/lib
COPY web /var/www/panel
COPY bin /var/www/bin

# ---- nginx 配置 ----
# 主配置是静态的（只做 http 层的公共设置 + include 渲染出来的 server 块）；
# 站点配置由面板设置渲染，构建期先渲染一份默认的（纯 HTTP，能起得来），
# 容器每次启动时根代理再按当时的设置渲染一遍。
COPY nginx/nginx-main.conf /etc/nginx/nginx.conf
COPY nginx/nginx-test.conf.template /etc/nginx/mclink-nginx-test.conf.template
COPY nginx/templates /var/www/nginx-templates

COPY supervisord.conf /etc/supervisord.conf

# ---- php-fpm 内存优化配置 ----
# ondemand + 最多 3 个 worker：空闲时几乎不占内存。
# 服务器总内存只有 1.58 GB 且无 swap，这里必须抠。
COPY php-fpm-pool.conf /usr/local/etc/php-fpm.d/zz-mclink.conf

# ---- 运行用户与目录 ----
#   * php-fpm 以 nobody(65534) 运行：面板要写 session、设置备份（D1）、
#     上传的 logo 和"待安装的证书"。
#   * nginx 的 master 是 root（supervisord 拉起来的），所以它能读 0600 的私钥；
#     worker 是 nginx 用户。
#   * 根代理（panel-agent.sh）是 root：渲染 /etc/nginx、跑 `nginx -t`、
#     `nginx -s reload`、openssl、certbot，都在它那一侧。
#
# [!] 宿主机上挂进来的目录要 chown 65534:65534（见 docker-compose.yml）：
#     config / data/panel-uploads / data/panel-certs
RUN set -eux; \
    mkdir -p /var/lib/php/sessions /run/nginx \
        /var/www/config /var/www/data/panel-uploads \
        /var/www/certs/agent /var/www/acme/.well-known/acme-challenge \
        /etc/nginx/mclink-servers; \
    chown -R nobody:nobody /var/www/lib /var/www/panel \
        /var/lib/php/sessions /var/www/config /var/www/data /var/www/certs; \
    chmod +x /var/www/bin/*.sh; \
    rm -f /etc/nginx/http.d/default.conf; \
    php /var/www/bin/mclink-panel.php render --dir /etc/nginx/mclink-servers

EXPOSE 80 443 19103

# 健康检查走**急救入口**：它不判安全入口 / 域名 / 是否跳转，所以"面板配置
# 改得很严"时健康检查不会误报（真正的对外入口挂了的话 19103 也照样能进，
# 这正是急救入口存在的意义）。
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD wget -q -O /dev/null "http://127.0.0.1:${MCLINK_PANEL_RESCUE_PORT:-19103}/healthz" || exit 1

CMD ["/usr/bin/supervisord", "-c", "/etc/supervisord.conf"]
