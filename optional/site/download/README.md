# `optional/site/download/` —— 客户端文件放这里（**不进版本库**）

这个目录在仓库里是**空的**（只有一个本说明）：客户端 exe 是二进制产物，
每次发版都会变，不进版本库、也不随 `optional/` 一起提交。

自己搭站时：

1. 把打包好的客户端放到本目录，文件名用 `CraftLink.exe`
   （`download.html` 里的下载链接和 `optional/checksum/check-download.py`
   的默认值都指向这个名字；换名字要同步改这两处）；
2. 按 `optional/site/README.md` 第 4 节更新 `download.html` 里的
   版本 / 字节数 / SHA-256，然后跑：

   ```bash
   python3 optional/checksum/check-download.py
   ```

`.gitignore` 已经挡掉了 `optional/site/download/` 下的 `*.exe` / `*.zip`，
不会被误提交。
