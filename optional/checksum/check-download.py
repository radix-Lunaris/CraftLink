#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""下载哈希校验（可选组件）：下载页里的版本信息必须和站点里真实的 exe 对得上。

    ★ 这个脚本**不属于主交付**。它是我们早期官网用的「发布前自检」，现在随
      `optional/checksum/` 一起留档，给未来的开发者参考。官方不再提供网站 /
      下载页 / 哈希校验这一整套附加能力，见 `optional/README.md`。

检查三件事：
  1. 下载页里存在指向客户端的下载链接
  2. 页面上的 SHA-256 与客户端文件的实际 SHA-256 一致
  3. 页面上的「大小(字节)」与文件实际字节数一致

⚠️ 故意设计成「exe 一换就报错」：
   发新版本时你会先把新 exe 覆盖到站点里，这时候这个脚本会立刻变红 ——
   它就是在提醒你「去把下载页里的三个数字改掉」。改完再跑一次，绿了就说明
   页面和文件对上了。

用法：

    # 默认：校验 ../site/download/CraftLink.exe 与 ../site/download.html
    python optional/checksum/check-download.py

    # 站点在别处 / 文件名不一样时自己指定
    python optional/checksum/check-download.py --site /path/to/site \
        --page download.html --file download/CraftLink.exe

返回 0 = 一致；返回 1 = 有不一致（按上面的提示改页面）。
"""

from __future__ import print_function

import argparse
import hashlib
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

#: 默认站点目录 = 本组件旁边的 optional/site/（两个可选组件成对留档）。
DEFAULT_SITE = os.path.join(os.path.dirname(HERE), "site")

DEFAULT_PAGE = "download.html"
DEFAULT_FILE = os.path.join("download", "CraftLink.exe")

# 页面里 SHA-256（64 位十六进制）与「大小(字节): 数字」的写法
SHA_RE = re.compile(r"SHA-256[:：]\s*([0-9a-fA-F]{64})")
SIZE_RE = re.compile(r"大小\(字节\)[:：]\s*([0-9][0-9,\s]*)")
LINK_RE = re.compile(r'href="(?:\./)?(?:download/)?CraftLink\.exe"')


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="下载页 SHA-256 / 字节数 与实际客户端文件的一致性自检"
    )
    parser.add_argument("--site", default=DEFAULT_SITE, help="站点根目录（含下载页）")
    parser.add_argument("--page", default=DEFAULT_PAGE, help="下载页文件名")
    parser.add_argument("--file", default=DEFAULT_FILE, help="客户端文件（相对站点根）")
    args = parser.parse_args(argv)

    site = os.path.abspath(args.site)
    page = os.path.join(site, args.page)
    exe = os.path.join(site, args.file)

    problems = []
    notes = []

    # --- 文件在不在 ---------------------------------------------------------
    if not os.path.isfile(page):
        print("[FAIL] 找不到页面：%s" % page)
        return 1
    if not os.path.isfile(exe):
        print("[FAIL] 找不到客户端文件：%s" % exe)
        print("       请把打包好的客户端放到 <%s>/%s"
              % ("站点根目录", os.path.dirname(args.file) or "."))
        return 1

    html = io.open(page, "r", encoding="utf-8").read()
    real_sha = sha256_of(exe)
    real_size = os.path.getsize(exe)
    real_mb = real_size / (1024.0 * 1024.0)

    print("站点目录 : %s" % site)
    print("客户端   : %s（%d 字节，约 %.2f MB）" % (args.file, real_size, real_mb))
    print("实际哈希 : %s" % real_sha)
    print("-" * 72)

    # --- 1) 下载链接 --------------------------------------------------------
    if LINK_RE.search(html):
        print("[ OK ] %s 里有客户端下载链接" % args.page)
    else:
        problems.append(
            "%s 里没有找到指向 %s 的下载链接"
            % (args.page, args.file.replace(os.sep, "/"))
        )

    # --- 2) SHA-256 --------------------------------------------------------
    found_sha = SHA_RE.search(html)
    if not found_sha:
        problems.append(
            "%s 里没有找到 SHA-256（应写成 \"SHA-256: <64 位十六进制>\"）" % args.page
        )
    else:
        page_sha = found_sha.group(1).lower()
        print("[ .. ] 页面哈希 : %s" % page_sha)
        if page_sha == real_sha:
            print("[ OK ] SHA-256 与 %s 一致" % args.file)
        else:
            problems.append(
                "%s 里的 SHA-256 与实际文件不一致\n"
                "        页面写的：%s\n"
                "        文件实际：%s" % (args.page, page_sha, real_sha)
            )

    # --- 3) 字节数 ----------------------------------------------------------
    found_size = SIZE_RE.search(html)
    if not found_size:
        problems.append("%s 里没有找到「大小(字节): <数字>」" % args.page)
    else:
        page_size = int(re.sub(r"[,\s]", "", found_size.group(1)))
        print("[ .. ] 页面字节 : %d" % page_size)
        if page_size == real_size:
            print("[ OK ] 字节数与实际文件一致")
        else:
            problems.append(
                "%s 里的字节数与实际文件不一致\n"
                "        页面写的：%d\n"
                "        文件实际：%d" % (args.page, page_size, real_size)
            )

    # --- 汇总 ---------------------------------------------------------------
    print("-" * 72)
    if problems:
        print("发现 %d 个不一致 —— 请更新 %s 后再发布：" % (len(problems), args.page))
        for item in problems:
            print("  - %s" % item)
        print("")
        print("提示：exe 换新之后这个检查变红是【故意的】，它就是提醒你同步")
        print("      版本号 / 大小(字节) / SHA-256 这三个数字。")
        return 1

    for note in notes:
        print(note)
    print("全部一致，可以发布。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
