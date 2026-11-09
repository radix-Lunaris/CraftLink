# 客户端日志（`docs/client-logs/`）

## 这里为什么**没有**日志文件

这个目录原来放的是 9 份**真实**客户端日志（2026-09-20 / 09-21 那几次排障
抓的）。它们已经从仓库里**移除**了 —— 不是为了少几个文件，而是那些日志里
含有**不该公开的个人数据**：

| 日志里的内容 | 为什么不能进公开仓库 |
|---|---|
| 客户端公网 IP（含玩家自己的和其它玩家的） | 个人数据；可定位到具体的人 |
| `client_id`（每台机器一个稳定标识） | 等于设备指纹 |
| 账号名 / 房间名 / 房间号 | 用户内容 |
| 中继域名与端口、拓扑 | 真实基础设施标识（D5/D10 之后不该出现在任何交付物里） |
| `C:\Users\<用户名>` 这类路径 | 本机用户名 |

结论：**真实日志不进仓库、不进交付包。** 需要排障时按下节脱敏后再贴。

## 要贴日志时怎么脱敏

```bash
# 1) 域名 -> example.com（中继地址、各条 [NAT]/[RELAY] 行都会命中）
sed -E 's/[A-Za-z0-9.-]*\.(online|com|cn|net)/example.com/g' client.log \
# 2) 公网 IP -> RFC 5737 文档地址（203.0.113.0/24）
  | sed -E 's/\b([0-9]{1,3}\.){3}[0-9]{1,3}\b/203.0.113.10/g' \
# 3) 账号名 / 房间名 / client_id
  | sed -E 's/(账号|account) +[^ |]+/\1 user/g; s/(房间|room) +[^ |]+/\1 room/g' \
  | sed -E 's/\b[0-9a-f]{16}\b/0123456789abcdef/g' \
  > client.sanitized.log
```

脱敏后**再自己读一遍**：`grep -nE '你的域名|你的IP|你的账号' client.sanitized.log`
应当为空。

## 日志长什么样（**合成示例**，不是任何一次真实运行）

下面这段是照着格式手写的，用来对照"哪一行是模式、哪一行是结果"：

```text
[18:39:28] server example.com, protocol version v2
[18:39:29] [NAT] probing example.com:3478 ...
[18:39:29] [NAT] 锥型 (NAT1-3) · 入站严格
[18:39:34] starting PLAYER mode | room room1 | account user | server example.com
[18:39:34] relay server : example.com
[18:39:35] [RELAY] connecting to relay example.com ...
[18:39:41] [direct] PUNCHED: peer=203.0.113.10:40879 plan=c2s sent=801 elapsed=1.7s
[18:39:41] P2P 已直连【玩家甲】203.0.113.10:40879 18ms
```

`docs/ISSUES-20260927.md` / `docs/LOG-AUDIT-20260927.md` 里引用 `[A:653]` 这种
行号，指的是**审计当时**那两份日志（`craftlink-client.log` 等，在当事人的
Windows 机器上），不是这个目录里的文件。

## 纪律

* `.gitignore` 已忽略 `docs/client-logs/*.txt` / `*.log` —— 真实日志掉进来也
  不会被提交（多一道保险，不是许可）。
* 新抓的日志放**本机**（Windows：`%APPDATA%\CraftLink\logs`），别往仓库里拷。
* `tests/real_identifiers_guard_test.py` 会扫描整个跟踪树：真实域名 / 备案号 /
  公网 IP / 明文凭据出现在任何文件里都会让回归网变红。
