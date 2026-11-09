# Git 日常操作（中文）

后续请在工作区的 **CraftLink** 目录开发，建议直接把此目录作为编辑器工作区打开。旧版本文件夹用于对照，不再复制整套版本目录作为开发分支。原本地目录及 GitHub 仓库已从旧名称改为 CraftLink，Git 历史保留。

```powershell
cd CraftLink
# 看当前分支、哪些文件变了
git status
git branch
# 看尚未提交的改动
git diff
# 指定要提交的文件，先不要盲目提交整个工作区
git add 文件名
git diff --cached
python -B scripts/check_secrets.py --staged
git commit -m "fix: 简述这次修复"
```

把文件放进暂存区不会上传。commit 也只写本地；push 才会上传。pre-push 要求新的单次精确提交授权，勿绕过保护。2026-10-27 用户授权首次将脱敏源码推送至 dev，不授权 master、legacy 或正式标签。

误把文件加入暂存区时，用 `git restore --staged 文件名` 撤出暂存，保留实际文件内容。不要用 `reset --hard` 或 `clean -fd` 清理工作区。

较大功能可从 dev 创建 `feat/功能名`，修复创建 `fix/问题名`；完成后验证并合回 dev。现阶段也允许直接在 dev 做小范围、有验证的提交。

legacy 固定在 `48f138dcb8a650b5f68ea6e3dc6231205cbfc090`，标签 `archive/legacy-20261026` 同指该提交。原 main 的五次提交和原旧发行标签保留。不能将新源码覆盖到 legacy。

2026-10-26 按用户追加授权完成远端整理：原 main 改名 legacy，master 为默认分支；远端 master、dev 指向 `2dffb71d62e68600ad5f9297f31e23430b2da5c6`，该提交没有父提交、没有文件，是空占位。旧五次提交和旧发行记录完整保留。此次没有上传当前开发源码。

本地 dev 保存实际源码，不能用空的 origin/dev 覆盖。首次发布将空的 origin/dev 历史合入本地 dev（允许无关历史），保持源码树不变，再进行普通快进推送；不强推、不镜像覆盖。正式源码发行与标签仍等待用户 VPS 验收及明确发布授权。

空分支初始化期间的许可已撤销。当前 scripts/check_push.py 要求忽略目录 logs 下的 push-approval.json 指定准确的本地提交与远端旧提交，只允许单独更新 CraftLink 仓库 dev，并强制先做可达历史敏感扫描。验证远端推送成功后，该记录标记 consumed，之后再次默认拒绝。不要将授权记录提交到 Git。

## 本地设置

2026-10-27 按用户要求，master 单独添加原 legacy 的 GPLv3 LICENSE；文件 Git blob 与 dev、legacy 都是 `94a9ed024d3859793618152ea559a168bbcbb5e2`。默认分支仍未放应用源码。CI 修复仅涉及运行编码与测试配置／启动顺序，应用版本保持 5.1.3，未生成新的客户端业务版本。

仓库局部设置：`core.autocrlf=false`、`core.hooksPath=.githooks`、`pull.ff=only`，不改全局 Git 身份和凭据管理器。

普通源码／Shell 使用 LF，批处理使用 CRLF。`auto_udp.py` 在原交付中含混合行尾，为满足 29 核心文件字节冻结，单独标记 `-text`；不对它做换行标准化。

克隆到另一台电脑后执行 `git config --local core.hooksPath .githooks` 启用本地钩子，先安装 Python 依赖。远端 CI 会在 dev 推送时运行；CI 通过与生产验收分别记录，不将本地隐私检查冒充网络回归通过。
