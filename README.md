# CraftLink

通用 TCP 端口联机工具，包含桌面客户端、房间管理、中继和管理面板。Minecraft 是使用场景之一，隧道建立不依赖游戏运行。

当前开发候选 **v5.1.3**；源码在 `dev` 分支开发，尚未完成用户 VPS 验收。本版完成自有地址隐私配置，继承六项桌面优化；见 [隐私配置说明](docs/V5.1.3-PRIVACY.md)。最新自动检查结果见 [GitHub Actions](https://github.com/radix-Lunaris/CraftLink/actions/workflows/checks.yml)，当前不是正式发行。

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

[工作区结构与 VPS 上传说明](docs/DEPLOY-UPLOAD.md) · [GitHub 开发分支](https://github.com/radix-Lunaris/CraftLink/tree/dev)

- `legacy`：旧远端提交和作者历史冻结归档。
- `dev`：当前候选开发；只提交源码、资源、模板、测试和必要文档。
- `master`：远端默认分支，目前仅含与 legacy 完全相同的 GPLv3 LICENSE；未来存放通过 VPS 验收的发行源码。
- EXE、镜像及候选包保留在本地忽略目录，未来经明确批准后通过 Release 分发。
- 远端原 `main` 已改名 `legacy`；开发源码已发布至 `dev`。默认分支仅补齐原许可证，未发布未经验收的应用源码或正式标签。
- pre-push 只允许一次授权记录指定的 dev 提交，检查历史敏感值及快进关系；其余推送拒绝。

沿用原仓库 GPLv3 许可证；历史作者及旧发行记录完整保留。见 [LICENSE](LICENSE)。
