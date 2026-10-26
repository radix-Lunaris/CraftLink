# MCLAN / CraftLink

通用 TCP 端口联机工具，包含桌面客户端、房间管理、中继和管理面板。Minecraft 是使用场景之一，隧道建立不依赖游戏运行。

当前开发候选 **v5.1.3**；在本地 `dev` 开发，尚未完成用户 VPS 验收，当前源码未上传远端。本版完成自有地址隐私配置，继承六项桌面优化；见 [隐私配置说明](docs/V5.1.3-PRIVACY.md)。

## 开发

```powershell
python -m pip install -r requirements-build.txt
python -m pip install -r server/requirements.txt
python -B client/main.py
python -B tests/run_all.py
python -B scripts/audit_ui.py
```

原有用户配置目录和 client_id 不变。用户配置可增加 `update_url`（HTTPS），服务器地址和端口沿用已有字段。
公开源码默认使用不可路由的 `.invalid` 示例地址。自用地址可通过外部 `client-profile.local.json` 或环境变量提供，读取顺序及打包边界见 [隐私配置说明](docs/V5.1.3-PRIVACY.md)。已有用户设置不重置，私有地址不会编译进公开 EXE。

## 部署与已有配置迁移

先读 [迁移说明](docs/V5.1.1-CHANGES.md)。已有部署必须在覆盖旧 Compose **之前**，将现有值导出到共享 `.env`；不能直接拿占位符模板覆盖线上配置。

```bash
python3 /临时工具目录/migrate_config.py --from-dir /opt/mc-link --output /opt/mc-link/.env
# 上传候选包，保留 .env 和现有 PHP 本地配置
python3 /opt/mc-link/scripts/deploy_config.py
bash /opt/mc-link/rebuild.sh all --prebuilt-panel
```

新部署需自行提供实际数据库账号与对应密码，不自动创建账号或改密码。容器与公共参数见 `.env.example`。
部署入口从脚本位置推导根目录，支持 `MCLINK_HOME` 和 `MCLINK_ENV_FILE`。Docker Compose 必须支持 `config --format json`。

## Git 与发行

[Git 中文操作说明](docs/GIT-WORKFLOW.md) · [v5.1.1 配置与迁移](docs/V5.1.1-CHANGES.md)

- `legacy`：旧远端提交和作者历史冻结归档。
- `dev`：当前候选开发；只提交源码、资源、模板、测试和必要文档。
- `master`：远端默认分支，目前为空占位；未来存放通过 VPS 验收的发行源码。
- EXE、镜像及候选包保留在本地忽略目录，未来经明确批准后通过 Release 分发。
- 远端原 `main` 已改名 `legacy`；远端 `master`、`dev` 仅含同一个无文件的初始化提交，未上传当前源码。本地 `dev` 继续保留开发进度。
- 本地 pre-push 阻止源码推送；源码公开发布仍需另行授权。

沿用原仓库 GPLv3 许可证；历史作者及旧发行记录完整保留。见 [LICENSE](LICENSE)。
