# v5.0.0 面板构建修正（2026-10-22）

本包已更新至 v5.0.1；本文件保留早期构建修正说明。当前镜像为 images/mc-link-panel-5.0.1-amd64.tar.gz，已真实构建、启动和载入验证，详情见 V5.0.1-CHANGES.md 与 panel-image.json。当前版本需同步更新中继和客户端，不能只更新面板后保留旧版版本号。

## 现场证据与结论范围

用户确认中继已可运行。面板日志停在镜像构建阶段，安装运行依赖约耗时 206 秒，随后又安装 PHP 扩展编译工具链；455.7 秒时仍在安装 mpfr4，未执行到应用页面。所提供片段没有最终 ERROR / exit code，不能据此判定最终失败属于下载、内存或编译错误。日志里没有旧容器、没有旧镜像的提示不是本次构建失败的证据。

此前面板 Dockerfile / Compose / 重建脚本与 3.9.20Full 一致。这次修正部署流程，不修改中继、客户端或原有手动网络核心。

## 修改

- 官方 PHP 8.2 FPM Alpine 已内置 curl、mbstring；不再额外安装 curl-dev、oniguruma-dev 并重复编译这两个扩展，只编译 pdo_mysql。
- 从同一个 php-base 派生编译与运行阶段，复制编译结果，避免扩展 ABI 不一致；最终镜像不包含编译工具链。
- 工具链安装单独成缓存层；PDO 编译默认单线程，构建参数 PHP_BUILD_JOBS 可覆盖。首次构建仍需下载 GCC 等工具，不能保证瞬间完成。
- 面板 Dockerfile 和 Compose 的默认 APK 源统一改为清华。本地完整 GCC 包约 60 MB 的对照下载耗时 4.17 秒；最终默认配置的全新镜像构建耗时 77.62 秒。可通过 APK_MIRROR 覆盖；不承诺任意 VPS 网络达到相同速度。
- 镜像构建中验证必要扩展、全部 PHP 文件语法、PHP-FPM 配置与 nginx 配置，失败直接停止。
- rebuild.sh panel 先 compose build mc-panel；失败即退出，不停止现有面板、不删旧镜像，也不启动旧镜像冒充新构建成功。构建成功后执行 up -d --no-build mc-panel。
- 登录页最多检查 15 次，每次 HTTP 超时 2 秒，间隔 1 秒；最终失败打印容器日志。
- 中继重建流程保持原样。仅修面板时使用 panel 参数。
- fix-panel.sh 兼容入口统一调用主脚本，不再自行停容器／删镜像或用未登录页面冒充管理 API 验证。
- --prebuilt-panel 校验随包镜像及配套面板源文件，再 docker load，随后复用启动／HTTP／容器代码／PHP 检查流程；校验、载入失败均不替换现有容器。

## 验证

- Git Bash 对 rebuild.sh 的 bash -n 通过。
- tests/test_panel_rebuild.py 九项通过，见 logs/r2-panel-script-tests.log。覆盖构建／启动／HTTP 失败，旧入口代理，以及离线镜像校验／载入失败和成功路径。
- tests/test_manual_baseline.py 通过：10 个原版手动网络核心固定指纹一致。
- network-r2 的完整 44 项回归通过；最终脚本增加离线路径后，9 项面板脚本测试单独通过。原始 3.9.20Full 的 89 项指纹仍一致。
- 在现有 WSL 中使用隔离的临时 Docker 实际构建最终 Dockerfile，未安装系统 Docker 服务。PHP 文件语法、必要扩展、PHP-FPM／nginx 配置检查通过；真实容器 HTTP 登录页返回正常内容和 CSRF 字段。
- 已导出 Linux amd64 镜像。又使用全新 Docker 数据目录实际执行 docker load，核对 image ID 并请求登录页通过。镜像、配套源文件和 SHA256 见 docs/panel-image.json、docs/panel-image.sha256；构建与运行记录见 logs/r2-panel-*.log／json。临时引擎验证后停止。
- 没有连接用户 VPS；生产数据库账号登录、站点反向代理／公网 HTTPS 仍由用户上传后验收，本地登录页验证不替代这些项目。

## 服务器操作

上传本次更新的 v5.0.0Full 服务端内容至原 /opt/mc-link，保留原 panel/config.php 和 .env。包含 images/、docs/panel-image.*、panel/、rebuild.sh 等配套文件。Linux amd64 VPS 推荐直接载入已验证镜像，跳过依赖下载／编译：

```bash
bash /opt/mc-link/rebuild.sh panel --prebuilt-panel
```

该命令只更新面板，不重启中继。镜像与源文件不一致时会明确拒绝，避免载入旧镜像冒充新代码。需要从源码构建，或非 amd64 主机时使用：

```bash
bash /opt/mc-link/rebuild.sh panel
```

若仍失败，请提供最终错误，不需要重新构建中继：

```bash
tail -n 100 /opt/mc-link/rebuild.log
docker logs --tail 80 mc-panel
```

## 依据

- [官方 PHP 基础镜像：内置 curl、mbstring 和 mysqlnd](https://github.com/docker-library/php/blob/master/8.2/alpine3.23/fpm/Dockerfile)
- [官方扩展编译助手：自动安装 PHPIZE_DEPS](https://github.com/docker-library/php/blob/master/docker-php-ext-install)
- [Docker 多阶段构建](https://docs.docker.com/build/building/multi-stage/)
- [Docker COPY 内容与缓存失效规则](https://docs.docker.com/build/cache/invalidation/)
- [Docker 直接载入 gzip 镜像并恢复标签](https://docs.docker.com/reference/cli/docker/image/load/)
