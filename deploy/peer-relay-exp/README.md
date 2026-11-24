# 玩家转发实验测试栈

以项目 `craftlink-peer-relay-exp` 部署于 `/opt/craftlink-peer-relay-exp`。在测试机外构建中继和面板镜像，并记录镜像 ID、源码提交和 SHA-256。客户端测试配置使用本次部署对应的域名、端口和严格同版客户端。

部署前检查 Docker Engine 和 Compose、主机可用内存、端口、网络子网、UFW 及云安全组。三个容器的内存上限合计 1152 MiB，还需为 Docker 和现有服务保留余量。2026-11-24 的初次盘点显示，临时实例未安装 Docker，可用内存约 437 MiB。该实例需要增加内存余量才能运行完整测试栈。

在部署目录创建权限为 `0600` 的 `.env`，指定以下配置。

| 变量 | 配置 |
|---|---|
| `EXPERIMENT_RELAY_IMAGE`、`EXPERIMENT_PANEL_IMAGE` | 本次测试的专用镜像引用 |
| `EXPERIMENT_DATABASE_IMAGE` | 对应平台的 MySQL 8 镜像摘要 |
| `EXPERIMENT_DB_PASSWORD`、`EXPERIMENT_DB_ROOT_PASSWORD`、`EXPERIMENT_ADMIN_TOKEN` | 分别生成的随机值 |
| `EXPERIMENT_BOOTSTRAP_ADMIN` | 首次部署的实验管理账号，初始化后从配置中清除 |
| `EXPERIMENT_BIND_IPV4` | 宿主机实际网卡的 IPv4 |
| `EXPERIMENT_RELAY_PORT` | TCP/UDP 外部端口，默认 19100 |
| `EXPERIMENT_STUN_PORT`、`EXPERIMENT_STUN_PORT2` | 连续 UDP 端口，默认 3478/3479。使用其他端口前需要配套客户端支持。 |
| `EXPERIMENT_PANEL_PORT` | 回环端口，默认 19102，通过 SSH 隧道访问 |

在实例上运行 `docker compose config --quiet` 验证插值。为实验端口补充 UFW 规则，并检查 Docker 转发链是否实际执行预期过滤。从外部验证中继 TCP、两个 STUN UDP 端口，以及管理端口不可达。数据库与管理 API 通过项目网络连接。启动前后记录其他服务的状态及监听端口。

回滚时运行 `docker compose stop` 停止此栈，撤回本次新增的防火墙规则。保留实验卷和日志以便排查。
