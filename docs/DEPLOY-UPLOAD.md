# 工作区与 VPS 上传说明

项目名称为 CraftLink，远端为 https://github.com/radix-Lunaris/CraftLink ，当前开发在 dev。本地最新为 v5.1.4 字体候选，尚未推送，未标成正式发行。已发布的 v5.1.3 双平台 CI 已通过；新版验证见 V5.1.4-VALIDATION.json。

## 本地工作区

```text
工作区/
├─ CraftLink/              唯一源码仓库，在这里持续开发
│  ├─ client/              桌面客户端源码
│  ├─ server/              中继、房间管理服务与 Compose
│  ├─ panel/               管理面板源码与 Compose
│  ├─ scripts/、tests/     构建、迁移、检查与测试
│  ├─ docs/               协议、操作及验证记录
│  ├─ images/             本地面板离线镜像，Git 不跟踪
│  └─ dist/               本地候选包、EXE、哈希记录，Git 不跟踪
├─ mclan-private/          沿用原路径的私有配置与自用客户端，不上传
├─ v3.9.20Full/ 等        旧交付，仅作对照及回滚
├─ dsh/、backups/          历史参考与备份，不合入新版
└─ 密码表及路由器记录      私有材料，不上传
```

以后直接修改 CraftLink 中的源码，用 Git 提交和分支保存进度，不再复制整套源码建新版本目录。dist 可以生成带版本号的交付包以便验收和回滚；它们不是新的开发仓库。客户端用户配置目录、现有部署目录和协议标识沿用原值，项目改名不会更换账号身份或网络协议。

## 上传哪些内容

使用本地 `CraftLink/dist/v5.1.4Full/` 完整候选目录或同目录旁的候选压缩包，保持相对路径上传到现有 VPS 部署根目录（例如 `/opt/mc-link`）。该部署路径不必改名。

最稳妥的做法是上传候选目录全部内容，只跳过根目录的 `CraftLink.exe`、`CraftLink-console.exe`；EXE 在玩家电脑运行。保留 `server/`、`panel/`、`scripts/`、`docs/`、`images/`、其余源码及根目录脚本、requirements 和 BUILD.txt，便于完整校验。使用 `--prebuilt-panel` 时，`images/` 的离线镜像及 `docs/panel-image.json`、`docs/panel-image.sha256` 必须配套上传。

不要从 Git 源码目录整包上传，也不要上传 `.git/`、私有目录、密码表、日志、旧备份或自用地址文件。Git 不保存 EXE、镜像和交付压缩包，因此单独从 GitHub 下载源码不等于完整的离线部署包。

保留 VPS 原有 `.env`、`panel/config.php`、`panel/config.local.php` 和现有持久数据、卷；不使用 `.env.example` 覆盖真实 `.env`。候选包本来就不包含这些私有文件。

## 原部署尚未完成凭据外置时

在覆盖旧 Compose 之前，先备份原部署目录和数据库，再单独将新版 `scripts/deploy_config.py`、`scripts/migrate_config.py` 放在 VPS 的同一个临时目录，例如 `/tmp/craftlink-migration/`，执行：

```bash
python3 /tmp/craftlink-migration/migrate_config.py --from-dir /opt/mc-link --output /opt/mc-link/.env
```

工具从旧部署保值导出，发现冲突就停止；不要手动填占位密码或运行轮换脚本。已经完成共享 `.env` 迁移的部署保留现有文件，检查一致即可。

然后上传候选目录并执行：

```bash
cd /opt/mc-link
python3 scripts/deploy_config.py
bash rebuild.sh all --prebuilt-panel
```

预检失败不要继续重建。重建中继会断开正在使用的连接，需安排合适时间。中继与双方客户端版本须一致为 5.1.4。面板代码未改，随包沿用已验证的 5.1.1 面板镜像，文件名和哈希保持真实版本。

若 VPS 已是同版本、服务文件未变化，不必重复重建。首次从 5.1.3 更新到 5.1.4 时，严格版本校验要求同步中继版本；中继只有版本元数据变化，网络逻辑没有变化。正式使用前仍需 VPS 与真实公网验收。
