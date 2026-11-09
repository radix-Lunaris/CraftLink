# ============================================================
#  CraftLink Admin Panel  (PHP 8.2 + nginx, 独立容器)
# ============================================================
#
#  为什么不用服务器上现成的 web-php82？
#    web-php82 只有 php-fpm（端口 9000），需要 1Panel 那边额外配 nginx 代理，
#    而且不能把面板代码塞进正在运行的生产容器里。
#    这个容器自带 nginx + php-fpm，是一个自包含的小服务：
#    web-nginx（跑在 host 网络）反代到 127.0.0.1:19102 即可。
#
#  网络：
#    * 1panel-network  -> 连 MySQL(web-mysql80) 和中继(mc-relay)
#    * 端口 19102 绑定到宿主机 127.0.0.1，只给本机 nginx 用，不对外
#
#  [!] 关于 apk 源：
#    Alpine 官方 CDN（dl-cdn.alpinelinux.org）在国内经常 TLS 超时，
#    一次构建能卡 8 分钟。这里默认换成阿里云镜像源（阿里云 ECS 走内网，
#    通常几十秒完成）。
#
#    如果你的服务器不在阿里云，或者想换别的源，构建时覆盖即可：
#      docker compose build --build-arg APK_MIRROR=mirrors.tuna.tsinghua.edu.cn
#      docker compose build --build-arg APK_MIRROR=dl-cdn.alpinelinux.org
# ============================================================

FROM php:8.2-fpm-alpine

LABEL org.opencontainers.image.title="mc-link-panel"
LABEL org.opencontainers.image.version="2.0.0"

ENV TZ=Asia/Shanghai

# 可覆盖：阿里云 / 清华 / 官方
ARG APK_MIRROR=mirrors.aliyun.com

# ---- 换源（必须在 apk add 之前）----
RUN set -eux; \
    if [ "$APK_MIRROR" != "dl-cdn.alpinelinux.org" ]; then \
        sed -i "s|dl-cdn.alpinelinux.org|${APK_MIRROR}|g" \
            /etc/apk/repositories; \
    fi; \
    echo "== 使用的 apk 源 =="; \
    cat /etc/apk/repositories

# ---- 系统依赖 ----
#   nginx        : 直接在本容器里提供 HTTP
#   curl-dev     : 编译 php curl 扩展（调用中继管理 API）
#   oniguruma-dev: 编译 mbstring
#   supervisor   : 同时跑 nginx + php-fpm
RUN apk add --no-cache \
        nginx \
        supervisor \
        curl-dev \
        oniguruma-dev \
        tzdata \
    && docker-php-ext-install -j"$(nproc)" pdo_mysql curl mbstring \
    && rm -rf /var/cache/apk/*

# ---- 面板代码 ----
# 目录结构必须和源码一致：
#     /var/www/lib/     <- lib/ 的内容（在 web 根目录之外，HTTP 访问不到）
#     /var/www/panel/   <- web/ 的内容（nginx 的 root）
#
# 代码里写的是 require __DIR__ . '/../lib/common.php'，
# 所以 __DIR__ 是 /var/www/panel 时必须存在 /var/www/lib。
#
# [!] 这里踩过一次坑：如果写成 `COPY web /var/www/panel`，只有 web/ 的内容
#    进容器，lib/ 整个丢失，页面会 500 并报
#    "Failed opening required '/var/www/panel/../lib/common.php'"。
COPY lib /var/www/lib
COPY web /var/www/panel

# ---- nginx 站点配置 ----
COPY nginx.conf /etc/nginx/http.d/default.conf
COPY supervisord.conf /etc/supervisord.conf

# ---- php-fpm 内存优化配置 ----
# ondemand + 最多 3 个 worker：空闲时几乎不占内存。
# 服务器总内存只有 1.58 GB 且无 swap，这里必须抠。
COPY php-fpm-pool.conf /usr/local/etc/php-fpm.d/zz-mclink.conf

# ---- 运行用户 ----
# 面板要写 session，交给 php-fpm 的运行用户
#
# [!] php-fpm 以 nobody(65534) 运行，所以宿主机上挂进来的 config.php
#    必须是它读得到的：chown 65534:65534 config.php && chmod 640 config.php
#    （chmod 600 root:root 会让容器读不到，页面直接 500）
RUN mkdir -p /var/lib/php/sessions /run/nginx \
    && chown -R nobody:nobody /var/www/lib /var/www/panel /var/lib/php/sessions

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1/login.php || exit 1

CMD ["/usr/bin/supervisord", "-c", "/etc/supervisord.conf"]
