# Git 日常操作（中文）

后续请在工作区的 **MCLAN** 目录开发，建议直接把此目录作为编辑器工作区打开。旧版本文件夹用于对照，不再复制整套版本目录作为开发分支。

```powershell
cd MCLAN
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

把文件放进暂存区不会上传。commit 也只写本地；push 才会上传。当前 pre-push 会拒绝推送，等待用户另行明确批准，勿绕过保护。

误把文件加入暂存区时，用 `git restore --staged 文件名` 撤出暂存，保留实际文件内容。不要用 `reset --hard` 或 `clean -fd` 清理工作区。

较大功能可从 dev 创建 `feat/功能名`，修复创建 `fix/问题名`；完成后验证并合回 dev。现阶段也允许直接在 dev 做小范围、有验证的提交。

legacy 固定在 `48f138dcb8a650b5f68ea6e3dc6231205cbfc090`，标签 `archive/legacy-20261026` 同指该提交。原 main 的五次提交和原旧发行标签保留。不能将新源码覆盖到 legacy。

2026-10-26 按用户追加授权完成远端整理：原 main 改名 legacy，master 为默认分支；远端 master、dev 指向 `2dffb71d62e68600ad5f9297f31e23430b2da5c6`，该提交没有父提交、没有文件，是空占位。旧五次提交和旧发行记录完整保留。此次没有上传当前开发源码。

本地 dev 保存实际源码，不能用空的 origin/dev 覆盖。本地已更新远端跟踪引用。未来首次源码推送前，可以将空的 origin/dev 历史合入本地 dev（允许无关历史），验证合并结果后再推送；不强推、不镜像覆盖。正式源码发行与标签仍等待用户 VPS 验收及明确发布授权。

空分支初始化期间使用的临时精确推送许可已撤销，pre-push 恢复默认全部阻止。不要绕过钩子上传源码。

## 本地设置

仓库局部设置：`core.autocrlf=false`、`core.hooksPath=.githooks`、`pull.ff=only`，不改全局 Git 身份和凭据管理器。

普通源码／Shell 使用 LF，批处理使用 CRLF。`auto_udp.py` 在原交付中含混合行尾，为满足 29 核心文件字节冻结，单独标记 `-text`；不对它做换行标准化。

克隆到另一台电脑后执行 `git config --local core.hooksPath .githooks` 启用本地钩子，先安装 Python 依赖。GitHub CI 文件已准备，但本轮未触发远端运行。
