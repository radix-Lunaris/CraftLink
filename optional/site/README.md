# `optional/site/` —— 早期官网留档（**我们不再提供**）

> ## 这不是主交付的一部分
>
> * 官方发布包（`deploy/` 目录结构、`BUILD.txt` 清单）里**没有**这个目录，
>   服务器上也不需要它；
> * 我们**不再提供**「官网 / 下载页」这项附加能力，也不再维护这里的页面内容；
> * 未来开发者想给自己的部署做官网，请自行用 **宝塔** 或 **1Panel** 的
>   「静态网站」功能实现；这里是**我们原来的实现，供参考**（页面是自包含的
>   纯静态 HTML + 一份 CSS + 一份 JS，没有后端、没有数据库、没有构建步骤、
>   没有任何外部请求）。
>
> 官方现在只做**联机服务**（中继 + 面板 + 客户端）。交付包完整性那套
> （`BUILD.txt` + `sha256sum -c`）仍然随主交付走，和本目录无关。

## 1. 文件清单

```
optional/site/
├── index.html                 首页（hero、特性、三步开玩、版本摘要）
├── tech.html                  技术路线（中继原理、房间/主机模型、数据通路）
├── download.html              下载页（系统要求、文件名、SHA-256、常见问题）
├── faq.html                   常见问题
├── assets/
│   ├── style.css              全站唯一一份样式（暗色 / 亮色 / 跟随系统）
│   └── site.js                主题切换 + 手机端导航 + 滚动入场（无框架）
├── deploy/
│   └── nginx-site.conf        nginx 站点配置模板（80 → 443、HTTP/2、gzip、缓存）
├── download/
│   └── README.md              客户端 exe 放这里（**不进版本库**，见该文件）
└── README.md                  本文件
```

`download.html` 的「校验信息」由 `optional/checksum/check-download.py` 自检。

## 2. 页面内容里的"假数据"（**必须换成你自己的**）

| 位置 | 现在是什么 | 换成什么 |
|---|---|---|
| 4 个页面的页脚 | `你的备案号（占位）` | 你自己的备案号；没有备案就把这一行删掉 |
| `download.html` 的「校验信息」 | `SHA-256: 000…0`、`大小(字节): 0`、`版本: v0.0.0（示例）` | 你发布文件的真实哈希 / 字节数 / 版本 |
| 4 个页面的版本号、日期（`v3.0.0` / `2026-09-18`） | 我们 3.0.0 时代的旧内容 | 你自己的版本；不再需要就整段删掉 |
| `download.html` 里"约 12.4 MB（13,021,016 字节）" | 我们旧 exe 的真实尺寸 | 你自己文件的尺寸 |
| `deploy/nginx-site.conf` | `craftlink.example.com`、`/opt/1panel/www/sites/craftlink/index`、证书占位路径 | 你的域名、站点根目录、证书真实路径 |

> 页面是我方原样留档，**版本号 / 字节数 / 备案号都是占位或过时值**，
> 直接上线会显示错误信息。

## 3. 自己用宝塔 / 1Panel 搭站（两条路，任选）

### 3.1 1Panel（我们当年就是这么部署的）

1. 把 `optional/site/` 下除本 README 外的文件传到服务器上一个目录，例如
   `/opt/1panel/www/sites/<站点名>/index/`（保持 `assets/`、`download/` 结构）；
2. 1Panel → **网站** → **创建网站** → **静态网站**，根目录指向 `index.html`
   所在的那层；
3. 把 `deploy/nginx-site.conf` 的内容贴进站点「配置文件」，至少改
   `server_name` 和 `root` 两处；保存并重载 nginx；
4. 站点 → **HTTPS**：申请 Let's Encrypt 证书或手动导入，然后把配置里
   `ssl_certificate` / `ssl_certificate_key` 指向真实证书（建议 `fullchain.pem`）；
5. 验证：`nginx -t`；`curl -sI https://你的域名/` 应当 200；
   `curl -sI http://你的域名/` 应当 301 到 HTTPS。

> `deploy/nginx-site.conf` 里**没有任何面板入口 / 面板端口 / 密钥** ——
> 站点和后台互不影响，搭站时**不需要**动现有的面板配置。

### 3.2 宝塔

1. **网站 → 添加站点**（纯静态，不要 PHP）；
2. 把站点根目录指向你上传页面的目录；
3. 「配置文件」里贴同一份 `nginx-site.conf`（改 `server_name` / `root` / 证书路径）；
4. 「SSL」里申请或粘贴证书，开启"强制 HTTPS"；
5. 重载 nginx，同样用上面的 `curl` 命令验证。

> 也可以用任意静态托管（对象存储 + CDN、GitHub Pages 等）：页面没有任何
> 服务端逻辑，`download.html` 里的下载链接是**相对地址**，换域名不用改。

## 4. 发布新版本时（只动 exe + 三个数字）

1. 把新客户端覆盖到 `download/CraftLink.exe`（**不进版本库**，见
   `download/README.md`；版本号请和 `server/VERSION`、客户端 `APP_VERSION` 对齐）；
2. 在 `download.html` 的「校验信息」里更新三行：版本、`大小(字节)`、`SHA-256`；
3. 自检：

   ```bash
   python3 optional/checksum/check-download.py
   ```

   （默认就校验 `optional/site/download/CraftLink.exe` 与 `download.html`；
   站点在别处时用 `--site` / `--page` / `--file` 指定。）
   三个数字对不上它会**故意报红**，改完再跑一次，绿了再上传；
4. 只上传 `download/CraftLink.exe` 和 `download.html` 两个文件即可。

## 5. 纪律

* **不放**后台入口、管理员链接、面板端口、秘密路径、服务器 IP、密钥；
* 不写死真实域名 / 备案号（示例值只作占位）；
* 二进制（exe / zip）不进版本库。
