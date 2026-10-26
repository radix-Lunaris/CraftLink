# ============================================================
# MC-Link Relay Server  (V2)
# ============================================================
#
# Runtime dependencies (installed here, nothing to do on the host):
#   PyMySQL       MySQL driver (used inside a worker thread)
#   cryptography  required by MySQL 8.0 caching_sha2_password auth
#
# If the image cannot reach PyPI, set MCLINK_DB_ENABLED=0 in compose: the
# relay still works (frozen TCP path) but accounts and the panel are disabled.
#
# ---------------------------------------------------------------------------
# 国内构建加速（两处，都是网络来源，不碰任何功能）
# ---------------------------------------------------------------------------
#
# 1) pip 走国内镜像。默认清华 TUNA。原因是实测：从 PyPI 官方源拉
#    cryptography（4 MB）在国内 VPS 上要几分钟，而 pip 下载期间不打印
#    进度条，看起来像卡死。换成镜像后是秒级。
#
#    想换源不用改这个文件，用 build arg：
#        docker compose build --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple
#    或者在 server/.env 里写一行 PIP_INDEX_URL=...
#
#    所有国内镜像都是 PyPI 的完整镜像，包内容一致，只是来源不同。
#
# 2) 基础镜像（python:3.12-slim）走 Docker Hub，这一层**本文件管不了**。
#    如果连它也拉不动，在宿主机配一次 registry 镜像加速（一次性）：
#        /etc/docker/daemon.json:
#          { "registry-mirrors": ["https://docker.m.daocloud.io",
#                                 "https://dockerproxy.com"] }
#        然后 systemctl restart docker
#    （具体可用地址以你所在网络实测为准。）
# ============================================================

FROM python:3.12-slim

LABEL org.opencontainers.image.title="mc-link-relay"
LABEL org.opencontainers.image.description="MC-Link TCP relay for Minecraft LAN session sharing"
LABEL org.opencontainers.image.version="5.1.3"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    MCLINK_BIND=0.0.0.0 \
    MCLINK_PORT=19100 \
    MCLINK_STATUS_FILE=/app/status/status.json \
    MCLINK_BOOT_TS=0

WORKDIR /app

# 国内 PyPI 镜像。可用 --build-arg PIP_INDEX_URL=... 覆盖，
# 不改代码就能换源。见文件头部的说明。
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

COPY requirements.txt ./

RUN mkdir -p /app/logs
RUN pip install --no-cache-dir \
        --index-url "$PIP_INDEX_URL" \
        --timeout 30 \
        --retries 3 \
        -r requirements.txt

# Application code.
#
# ⚠️ 每次新增 .py 文件都必须加进下面这行！
#    漏掉的话容器里 import 会失败，服务起不来（或旧代码继续跑）。
#    目前文件清单：
#      relay_config.py relay_server.py healthcheck.py
#      admin_api.py auth.py db_schema.py db_store.py gatekeeper.py
#      settings_service.py  <- 面板开关（运行时设置）
#      stun_server.py       <- 内置 STUN 服务（UDP，打洞用）
#      reset_password.py    <- SSH 找回账号密码用
#                              docker exec -it mc-relay python reset_password.py
#      room_members.py      <- 房间成员 / host 身份（V3）
#      room_registry.py     <- 房间注册表（必须独立成文件：
#                              relay_server 以 __main__ 运行，
#                              `from relay_server import rooms` 会导入第二个空副本，
#                              面板关房就会一直回「房间不存在」）
COPY relay_config.py relay_server.py healthcheck.py ./
COPY admin_api.py auth.py db_schema.py db_store.py gatekeeper.py ./
COPY settings_service.py ./
COPY stun_server.py ./
COPY reset_password.py ./
COPY room_members.py room_registry.py room_names.py ./
COPY auto_relay.py ./
# 版本门：客户端与服务端必须同版本。这个文件由 scripts/sync-deploy.py
# 从 client/mclink/config/settings.py 的 APP_VERSION 生成；漏了这行，
# 容器里读到空字符串 = 门关着，旧客户端照用（正是用户不要的）。
COPY VERSION ./
# 服务端日志（文件 + 内存缓冲 + 面板下载）——漏了这行容器会直接起不来
COPY relay_log.py relay_events.py ./

# Writable directory for the status file. Owned by the unprivileged user so a
# mounted volume also gets the right owner on first use.
RUN mkdir -p /app/status \
    && useradd --create-home --uid 10001 mclink \
    && chown -R mclink:mclink /app

USER mclink

# 19100 = Minecraft relay (published to the host)
# 19101 = admin API for the PHP panel (Docker network ONLY, never published)
EXPOSE 19100
EXPOSE 19101

# Built-in STUN for hole punching: a CONSECUTIVE PAIR of UDP ports
# (3478 + 3479), which is MCLanP2P's layout -- the ported responder binds
# both, and the ported client expects both.
#
# WHY TWO: detecting a symmetric NAT needs two DIFFERENT destinations to
# compare mappings against, and a single-IP VPS can only offer that by
# varying the PORT. With only one the client cannot tell a cone NAT from a
# symmetric one.
#
# BOTH must also be published in docker-compose AND opened in the cloud
# security group. Missing one is SILENT: the client simply sees one
# destination less.
#
# Set MCLINK_STUN_ENABLED=0 to turn STUN off and behave exactly as before.
EXPOSE 3478/udp
EXPOSE 3479/udp

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD ["python", "healthcheck.py"]

CMD ["python", "-u", "relay_server.py"]
