# 安全告知：凭据硬编码（v3.9.20）→ **已修复**（v3.9.21 / D5）

> **状态：已修复。** 记录于 2026-10-05（v3.9.20），修复于开源发布前（D5）。
> 本文件保留下来，是因为"曾经带着明文凭据外传过"这件事**必须留档** ——
> 删掉字符串不等于补救，见下面第四节。

---

## 一、是什么（历史）

`panel/docker-compose.yml` 与 `server/docker-compose.yml` 里曾经**直接写着明文凭据**
（v3.9.20，用户 2026-10-05 为了"让部署能走通"做的取舍）：

| 键 | 当时的位置 |
|---|---|
| `MCLINK_DB_PASSWORD` | `panel/docker-compose.yml`、`server/docker-compose.yml` |
| `MCLINK_ADMIN_API_TOKEN` | 同上 |

也就是说：**那个交付包里带着能直接连数据库、能直接调管理 API 的真实凭据**。

## 二、风险（当时）

**那个 zip 只要外传一次，数据库与管理 API 就一起交出去了。**
具体场景：拿去给别人排查问题、上传到网盘/工单系统、发到群里、备份到不受控的
地方。拿到的人不需要任何其他信息，就能读改 `mclink` 库里的全部数据，并通过
19101 端口的管理 API 操作中继（改开关、踢房间）。

这两个值**已经随 v3.9.15 ~ v3.9.20 的包外传过**（v3.9.16 的复核报告里点名过）。

## 三、修了什么（D5：开源版绝不硬编码）

1. **两个 compose 里只剩引用**：`${MCLINK_DB_PASSWORD:?MISSING: ...}`、
   `${MCLINK_ADMIN_API_TOKEN:?MISSING: ...}`。真值只存在于服务器的 `.env`
   （600，不进仓库、不进交付包）。面板 compose 里的备案号也改成
   `${MCLINK_PANEL_ICP:-}`（默认空 -> 页脚不渲染）。
2. **`rebuild.sh` 回到 `SECRETS_MODE=external`**，并且**删掉了 hardcoded 分支**：
   compose 里出现任何明文（hex、随机串、中文占位符都算）一律判失败。同时保留
   两条老规则：中文占位符要拦、`.env` 与两个 compose 引用的变量名必须一致
   （否则就是"面板连得上、中继连不上"）。
3. **`install.sh` 兼容老部署升级**：
   * 先把线上凭据读进 `.env`（优先级：已有 `.env` 真值 > 运行中的
     `mc-relay` / `mc-panel` 容器环境 > 随机生成）——**数据库密码不会被改**；
   * 再把**部署副本**里的两个 compose 写成 `${VAR:?...}` 引用（老部署的明文
     副本会被就地改成引用；新包本来就是引用，等于空操作）。
4. **门禁与回归网**：
   * `python3 scripts/check_deploy.py --strict-secrets` 的「包内无明文凭据」为绿；
   * `tests/run_all.py` 里那两条"硬编码是预期行为"的断言**翻转**成新口径
     （仓库里没有明文、两个 compose 都有引用、`.env` 提供值、严格门禁为绿）；
   * 新增 `tests/real_identifiers_guard_test.py`：真实域名 / 备案号 / 公网 IP /
     明文凭据出现在跟踪树里就让回归网变红（失败信息带文件与行号）。
5. `deploy/` 镜像里的同样两处明文也改成了引用（门禁扫描整个仓库）。

## 四、线上**必须**做的一件事：轮换（轮换过的值不能再当凭据）

上面第 3 条保证升级时**沿用**旧值（D5：平滑迁移）。但旧值已经随旧包外传过，
所以公开仓库/交付包之前，线上应当**主动换掉**它们：

```bash
# 1) 生成新值并写进 .env（打印出来的 SQL 要在 MySQL 里执行，脚本故意不代做）
python3 scripts/rotate_secrets.py

# 2) 重建两个容器让新值生效
bash rebuild.sh all
```

`rotate_secrets.py` 会把新值写进 `.env` 并打印 `ALTER USER` 语句 —— 数据库那边
必须人工执行（改错就是一次全站中断，脚本不替你做这个决定）。

> 不轮换的直接后果：任何手里有 v3.9.15 ~ v3.9.20 旧包的人，仍然能连你的库、
> 调你的管理 API。换掉之后旧包里的值就是废字符串。

## 五、怎么验收

```bash
# 仓库/包里不许有明文凭据（新口径：有明文就是失败）
python3 scripts/check_deploy.py --strict-secrets

# 回归网（含真实标识符守卫）
python3 tests/run_all.py
```

## 六、当前为了降低伤害还做了什么

* `scripts/check_deploy.py` 仍然会扫描并点名任何明文凭据（`--strict-secrets`
  直接判失败）。
* `rebuild.sh` 在重建之前就拦住：明文、中文占位符、`.env` 与 compose 不一致、
  `.env` 缺失。
* `.gitignore` 忽略客户端的真实日志目录内容（`docs/client-logs/*.txt`），并
  留了 `docs/client-logs/README.md` 说明脱敏方法。

## 七、一句话

**仓库里再也不许出现真实凭据或真实基础设施标识**；线上那套值请在升级后
主动轮换一次 —— 它们已经不是秘密了。
