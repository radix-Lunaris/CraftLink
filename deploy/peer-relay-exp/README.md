# CraftLink 轻量实验服

- 客户端入口由测试实例的私有配置指定，使用与中继同版的实验客户端。
- 部署目录 `/opt/craftlink-peer-relay-exp`，Compose 项目 `craftlink-peer-relay-exp`。
- 单个中继容器，内存上限 128 MiB，0.5 CPU，带日志轮转。账号文件和状态使用独立卷。轻量面板由中继进程提供。
- 本次实例盘点发现约 437 MiB 可用内存，MCSync 在运行，Docker 尚未安装。轻量配置的容器限额在当前余量内，仍需测量 Docker 开销和实际峰值。

## SSH 账号命令

登录测试机后进入部署目录。以下命令在中继已经运行时使用。

```sh
cd /opt/craftlink-peer-relay-exp
docker compose exec relay python file_accounts.py list
docker compose exec relay python file_accounts.py add alice
docker compose exec relay python file_accounts.py delete alice
docker compose restart relay
```

添加时在终端输入密码两次。每批增删完成后重启实验中继，应用新增账号并撤销被删除账号的会话。重启会断开该实验服的在线玩家。账号卷中保存密码哈希。首次部署可用 `docker compose run --rm --no-deps relay python file_accounts.py add alice` 预建账号，再启动服务。

## 面板和网络

使用 SSH 本地转发 `-L 19102:127.0.0.1:19102`，然后打开 `http://127.0.0.1:19102/`。输入部署时生成的 `EXPERIMENT_ADMIN_TOKEN`，可查看状态、列出房间、关闭房间。令牌存放在仅部署用户可读的实验 `.env` 中。

设置 `EXPERIMENT_RELAY_IMAGE`、实例实际网卡地址 `EXPERIMENT_BIND_IPV4` 和随机 `EXPERIMENT_ADMIN_TOKEN`，用 `docker compose config --quiet` 预检。启动前后核对 MCSync 和 SSH。实验入口使用 TCP/UDP 19100 与 UDP 3478、3479，面板端口 19102 仅绑定回环。按 Docker 实际运行方式配置 UFW 与转发过滤，从外网验证游戏端口可达以及面板不可达，再复查阿里云安全组。

停止实验服时执行 `docker compose stop`，撤回本次增补的规则，保留账号卷及日志。
