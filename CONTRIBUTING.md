# 参与 CraftLink 开发（CONTRIBUTING）

> 目标：**任何一次修改都能被证明没改坏。** 这个项目的历史教训是"改好了"没有
> 证据 —— 同一个 bug（host 断开后房间没清干净）回归过三次。所以下面的门禁不是
> 形式，提交前请一条条跑完。

---

## 1. 环境

| 需要 | 说明 |
|---|---|
| Python | 3.10+（中继容器镜像用 3.12；`tests/` 与 `scripts/` 只用标准库，**不需要 pip install**） |
| Docker + compose | 只有跑服务端 / 面板时需要；纯回归网不需要 |
| Node / 前端工具链 | **不需要**，项目里没有前端构建步骤 |

仓库里没有任何需要编译的产物：客户端是纯 Python + tkinter，服务端是纯 Python，
面板是 PHP。

---

## 2. 目录速览

| 目录 | 是什么 |
|---|---|
| `server/` | 中继服务器（唯一握手解析点 + 房间/成员状态机） |
| `panel/` | PHP 管理面板（`panel/web/` 页面、`panel/lib/` 逻辑、`panel/nginx/` 模板） |
| `client/` | 客户端（`mclink/core/` 协议与打洞、`mclink/gui/` 界面、`mclink/config/` 配置与品牌） |
| `deploy/` | **交付包**：服务器上就是这个目录结构（由 `scripts/sync-deploy.py` 生成） |
| `tests/` | 回归网。`run_all.py` 随包交付；`run_all_full.py` 是工作区重网 |
| `scripts/` | 打包、门禁、版本号、部署同步 |
| `optional/` | **不随主交付发布**的留档（早期官网 + 下载哈希校验），见 `optional/README.md` |
| `docs/` | 设计与运维文档（协议、面板设置、SSL、部署指南…） |

---

## 3. 提交流程

1. Fork / 开分支：一个改动一个分支，**不要**在别人的分支上叠修改；
2. 改代码 —— 遵守下面的"硬规则"；
3. 跑回归（见第 4 节），全绿再提交；
4. 提交信息写清**为什么**改（现象 + 根因 + 验证方式），别只写"修复 bug"；
5. 提 PR：说明改了什么、怎么验证的、影响哪个平台（服务端 / 面板 / 客户端）；
6. 涉及协议、房间生命周期、打洞流程的改动，**必须在 PR 里贴出回归输出**。

---

## 4. 回归网怎么跑

```bash
# 1) 主力回归（随包交付，纯标准库、不需要网络）
XDG_CONFIG_HOME=$PWD/.testcfg python3 tests/run_all.py

# 2) 面板静态门禁（HTML/PHP/JS 契约，不需要数据库）
python3 tests/panel_static_test.py

# 3) 需要真中继的套件（挑你改动相关的跑；都是自己起 relay、自己关）
XDG_CONFIG_HOME=$PWD/.testcfg python3 tests/v2_control_test.py
XDG_CONFIG_HOME=$PWD/.testcfg python3 tests/room_settings_test.py
XDG_CONFIG_HOME=$PWD/.testcfg python3 tests/host_offline_owner_test.py
XDG_CONFIG_HOME=$PWD/.testcfg python3 tests/version_gate_test.py
```

* 工作区里还有一套更重的 `tests/run_all_full.py`（几十套、含打洞与 GUI），
  改到核心协议/打洞时再跑；
* 跑测试**务必**带 `XDG_CONFIG_HOME=$PWD/.testcfg`：不带的话客户端会往只读的
  `~/.config` 写日志，测试会以看不懂的方式失败；
* 测试会自己拉中继子进程、用完自己收；端口是固定值（如 19110/19120），
  同时跑多套会撞端口 —— 一套一套来。

---

## 5. 发版 / 打包门禁（**顺序固定，不要跳步**）

```bash
# ① 清掉源码树里的本地产物（__pycache__ 等），并重新生成 BUILD.txt / README.txt
python3 scripts/make_build.py --clean

# ② 门禁：版本号、无明文凭据、无 __pycache__、BUILD.txt、Dockerfile、编译、e2e、回归网
python3 scripts/check_deploy.py

# ③ 全绿之后才打戳（把"这一版已发"写进 LAST_VERSION）
python3 scripts/check_deploy.py --stamp

# ④ 最后才升版本号（唯一来源：client/mclink/config/settings.py 的 APP_VERSION）
python3 scripts/bump-version.py <x.y.z>
```

顺序的理由：

* `make_build.py` 必须在门禁**之前**跑 —— `check_deploy.py` 校验的是
  `BUILD.txt` 与源码树是否一致；先打戳再改文件，等于让指纹作废；
* `--stamp` 只能在**全绿之后** —— 它记录"上一版已发出"，先盖章会把没验过的
  版本当成已发；
* `bump-version.py` 放最后 —— 它会改客户端与服务端两个版本号，
  改完需要重新 `make_build.py --clean` + 门禁 + `--stamp`。

**版本号只有一个来源**：`client/mclink/config/settings.py` 的 `APP_VERSION`；
`server/VERSION`（以及 `deploy/server/VERSION`）由脚本生成，**不要手改**。

---

## 6. 硬规则（历史踩出来的，别违反）

1. **握手行必须带 `ver=`，挂在整行最后**：`CREATE` / `JOIN` / `HOST` /
   `HOST_TUNNEL` / `PLAYER` 五条都要。中继的版本门在唯一的握手解析点上，
   少一个字段就是 `VERSION_MISMATCH` —— 用户会"版本明明一样却连不上"。
   （测试里的裸 socket 握手行同样要带，见 `tests/v2_control_test.py` 等。）
2. **不硬编码路径**：安装根目录由环境变量/脚本自身位置推导，默认值只是默认值。
3. **不硬编码凭据、域名、IP**：真实值只存在于部署机的 `.env` / 面板设置里。
4. **客户端不预置中继地址**：默认空，用户填了才记；登录记忆默认不写盘。
5. **房间参数创建即冻结**：人数上限、连接模式等，创建后不可改。
6. **Owner 不可继承**：Owner 离开（主动/被踢/被清退）→ 房间立即销毁。
7. **"连接状态"和"房间身份"是两套状态**：掉线不等于离开房间，别用一个布尔混着表达。
8. **不许为了让测试变绿而放宽断言**：测试红了先查代码；要改断言，
   必须在 PR 里说明为什么旧断言是错的（而不是"改成能过的样子"）。
9. **`optional/` 里的东西不随包发布**：主交付的脚本/文档不许引用它下面的路径。
10. **不要提交二进制**（exe / zip / 证书 / 私钥）；`.gitignore` 已经挡了一部分。

---

## 7. 代码风格

* 只用标准库（服务端、客户端、测试、脚本都是）；
* 注释和文档用中文，命令、协议字段、日志键保持英文原文；
* 不要"顺手重构"已经验收过的部分：先改测试证明行为没变，再谈重构；
* 提交前把调试输出/临时文件删干净（`make_build.py --clean` 会帮你找
  `__pycache__`，门禁会拦）。

---

## 8. 许可证

提交即表示你同意以 **GPLv3**（见 `LICENSE`）授权你的贡献。
