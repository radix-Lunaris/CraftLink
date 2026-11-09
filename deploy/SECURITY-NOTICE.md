# 安全风险告知：凭据为硬编码

> **状态：已记录，接受。** 记录于 2026-10-05，v3.9.20。
> 这是**已知且被接受**的风险，不是遗漏。

---

## 一、是什么

`panel/docker-compose.yml` 与 `server/docker-compose.yml` 里**直接写着明文凭据**：

| 键 | 位置 |
|---|---|
| `MCLINK_DB_PASSWORD` | `panel/docker-compose.yml:67`、`server/docker-compose.yml:110` |
| `MCLINK_ADMIN_API_TOKEN` | `panel/docker-compose.yml:69`、`server/docker-compose.yml:135` |

也就是说：**这个交付包里带着能直接连数据库、能直接调管理 API 的真实凭据**。

## 二、风险

**这个 zip 只要外传一次，数据库与管理 API 就一起交出去了。**

具体场景：拿去给别人排查问题、上传到任何网盘/工单系统、发到群里、备份到不
受控的地方。拿到的人不需要任何其他信息，就能读改 `mclink` 库里的全部数据，
并通过 19101 端口的管理 API 操作中继（改开关、踢房间）。

这两个值**已经随 v3.9.15 及更早的包外传过**（v3.9.16 的复核报告里点名过这一点），
现在又回到了包里。

## 三、为什么这么做

v3.9.16 把凭据外部化到 `.env`，v3.9.19 又加了"部署时自动补齐"。**实际部署仍然
卡住**。用户在 2026-10-05 决定：改回硬编码，让部署能走通，风险**仅记录**，
此后再处理。

## 四、改回来的步骤（以后要做的时候照这个做）

1. **先轮换值**——把字符串删掉不算补救，那些值可能已经被复制走了：

   ```bash
   python3 scripts/rotate_secrets.py
   ```

   它会生成新随机值并打印**必须在 MySQL 里执行**的那条 SQL。SQL 要人工执行
   （脚本故意不碰数据库：改库密码需要 DBA 权限，写错会当场把面板搞挂）。

2. 把两个 compose 里的明文换回引用：

   ```
   MCLINK_DB_PASSWORD: "${MCLINK_DB_PASSWORD:?MISSING: set ... in /opt/mc-link/.env}"
   MCLINK_ADMIN_API_TOKEN: "${MCLINK_ADMIN_API_TOKEN:?MISSING: set ... in /opt/mc-link/.env}"
   ```

3. 把 `rebuild.sh` 顶部的 `SECRETS_MODE` 改回 `external`。

4. 验收（这一步会把明文重新判成失败）：

   ```bash
   python3 scripts/check_deploy.py --strict-secrets
   ```

   全绿之后把本文件删掉或改成"已修复"。

## 五、当前为了降低伤害做了什么

* `scripts/check_deploy.py` 每次跑都会**醒目地打印**这个风险与检出位置
  （策略是记录式，不拦部署）。改回引用后它会自己变成 `[ OK ]`，零成本。
* `rebuild.sh` 在 `hardcoded` 模式下仍会拦**中文占位符**（那会让 MySQL 驱动报
  `latin-1 codec can't encode`，报错完全不提真正原因）。
* 若服务器上同时存在 `.env` 且里面的值与 compose 明文不一致，`rebuild.sh` 会
  拦住——两边不同值会变成"面板连得上、中继连不上"这种极难查的错。

## 六、一句话

**别把这个包外传。** 要发给别人排查时，先把那四个值替换成占位符再发，或者只发
你需要的那个文件。
