# MC-Link 面板：藏起来 + 反向代理

> **已解决（2026-09-14）**：1Panel 里「代理路径」和「上游地址」**两个都要带
> 结尾斜杠**就成功了：
>
>     代理路径   /wzshapRvDhjmfdtm/
>     上游地址   http://127.0.0.1:19102/     ← 少了这个斜杠就是 404
>
> 换端口 = 只改上游地址那一行，面板侧不用动任何东西。

需求：不想让人直接访问 https://mc.example.com/ 就看到面板登录页，
想只通过一个"只有自己知道"的路径访问。

结论：**能做，而且不用改面板一行代码。** 面板里的链接、样式、fetch 全是
相对路径（index.php?... / assets/style.css / live.php），所以挂在任意子路径
下都能正常工作。


一、为什么会 404（你刚才踩的那个坑）
------------------------------------------------------------

### 真实案例（从公网实测的四个地址）

    https://域名/                              200  面板登录页  ← 根路径也暴露着面板
    https://域名/wzshapRvDhjmfdtm              200  面板登录页（被内部跳到 /index.php?action=login）
    https://域名/wzshapRvDhjmfdtm/             404  nginx
    https://域名/wzshapRvDhjmfdtm/index.php    404  nginx

结论很清楚：**`/wzshapRvDhjmfdtm/` 这条前缀根本没有走到面板**。
带斜杠的请求命中了另一条规则（1Panel 生成的那条，指向静态目录/root），
所以是宿主机 nginx 直接 404；而不带斜杠的请求没匹配到那条规则，
于是落回站点默认的 `location /`，那条是代理到面板的 —— 所以"能打开"。

也就是说：**"藏起来"这件事目前等于没做**（根路径照样能访问面板），
而前缀规则本身是坏的。修法是把它换成一条真正的反代规则（见第二节）。

### 常见的三种 404 原因

1) **`proxy_pass` 结尾少了那个 `/`。**

       proxy_pass http://127.0.0.1:19102;      <- 原样转发 /某个路径/index.php
       proxy_pass http://127.0.0.1:19102/;     <- 去掉前缀，转发 /index.php

   少了斜杠时，面板容器收到 `/某个路径/index.php`：文件不存在，
   而容器里的 `location ~ \.php$ { try_files $uri =404; }` 直接 404。
   更迷惑的是**同一个前缀不带文件名却正常**：那条落到容器的
   `location / { try_files ... /index.php; }` 被兜底成了首页 ——
   于是"页面能开、样式和按钮全 404"。

2) **前缀规则写成了静态目录/root，而不是 proxy_pass。**
   1Panel 里如果用了"代理目录"或路径填错位置，生成的就是这类规则：
   文件当然不存在 → 404（你现在的现象）。

3) **改的不是真正生效的那份配置。** 1Panel 管的 OpenResty 要在
   「网站 → 配置文件」里改并保存（它会自己 reload）；
   `nginx -t` / `nginx -T` 才能确认到底加载了什么。


二、推荐配置（http 段里的 server 块）
------------------------------------------------------------

假设你想用 /mc-8f3a2c/ 这个路径（换成你自己的随机串，别用 admin 这种）。
面板容器是 host 网络，监听 127.0.0.1:19102。

    # 0) 不带结尾斜杠的访问，先跳到带斜杠的形式
    #
    #    强烈建议加上这一条。浏览器在 /mc-8f3a2c（没有结尾斜杠）上会把最后
    #    一段当成"文件名"，于是页面里所有相对地址（assets/style.css、
    #    index.php?action=logout）都以 / 为基准解析 —— 现象就是
    #    「页面能打开，但没样式、点什么都 404」。
    #
    #    面板本身也会做这个跳转（见 panel_base_url()），但 nginx 这一层做掉
    #    更快，也不会闪一下。
    location = /mc-8f3a2c {
        return 301 /mc-8f3a2c/;
    }

    # 1) 先把这个路径放在所有 location 之前（^~ 表示命中后不再走正则）
    location ^~ /mc-8f3a2c/ {
        # 结尾的 / 很关键：它把 /mc-8f3a2c/ 这一段去掉再转发
        proxy_pass http://127.0.0.1:19102/;

        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # 面板拉实时数据用的是 fetch('live.php')，保持连接短、不缓存
        proxy_buffering off;
        proxy_read_timeout 60s;
    }

    # 2) 根路径：不暴露面板，直接 404（或 302 去你的主页）
    location = / {
        return 404;
    }

    location / {
        # 你的网站原来的规则，保持不动
        # ...
    }


三、可选：把登录页也藏起来，只允许特定 IP
------------------------------------------------------------

    location ^~ /mc-8f3a2c/ {
        # 只允许家里/公司的出口 IP，其它一律 404
        allow 1.2.3.4;
        allow 5.6.7.8;
        deny  all;

        proxy_pass http://127.0.0.1:19102/;
        # ...同上
    }

注意：`deny all` 返回的是 403，想伪装成"不存在"就用：

    location ^~ /mc-8f3a2c/ {
        if ($remote_addr !~ "^(1\.2\.3\.4|5\.6\.7\.8)$") { return 404; }
        proxy_pass http://127.0.0.1:19102/;
    }


四、验证步骤（照着做，不要猜）
------------------------------------------------------------

    # 1) 面板容器自己能不能访问（脱离反代）
    curl -s -o /dev/null -w '%{http_code}\n' \
        'http://127.0.0.1:19102/index.php?action=login'
    # 期望 200

    # 2) 反代路径对不对（注意结尾斜杠）
    curl -s -o /dev/null -w '%{http_code}\n' \
        'https://你的域名/mc-8f3a2c/index.php?action=login'
    # 期望 200

    # 3) 根路径应该不是面板
    curl -s -o /dev/null -w '%{http_code}\n' 'https://你的域名/'
    # 期望 404

    # 4) 看 nginx 到底匹配了哪条规则（关键排查手段）
    #    在 server 块里临时加：
    #        add_header X-Debug-Location "secret" always;
    #    然后 curl -I 看响应头有没有它


五、几个提醒
------------------------------------------------------------

* **一定要用带结尾斜杠的地址访问**：`https://域名/mc-8f3a2c/`。
  不带斜杠的话，浏览器解析相对地址的基准会变成网站根目录，
  表现就是"能打开页面，但没样式、按钮全 404"。
  面板现在会自己 301 到带斜杠的形式（panel_base_url / enforce_trailing_slash），
  所以即使忘了加 nginx 那条跳转也能用；但建议还是加上，省一次跳转。

* 页面 `<head>` 里现在有一行 `<base href="...">`，它就是相对地址的基准，
  由 `$_SERVER['REQUEST_URI']` 算出来（反代前缀下才算得对）。

* **安全靠的是"路径长且随机"，不是"藏起来"。** 面板的账号密码、
  root 唯一性、只读限制才是真正的防线（这些都已实现）。
  路径只是减少被扫描到的概率。

* 随机路径别写进文档/截图；换路径等于改这一行，随时可换。

* 面板里 `live.php` 的实时刷新是相对路径，挂子路径后照常工作，
  不需要额外配置。

* 如果你的域名根目录本来是个静态站，记得把 `location = /` 那条
  写在你原来规则的前面，否则会先命中老规则。
