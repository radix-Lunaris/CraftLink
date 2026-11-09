#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CraftLink-Builder 自检 / 单测（离线，不需要 Windows，不需要 Inno Setup）。

跑法（三种都一样）：

    python3 installer/tests/builder_selftest.py
    python3 installer/builder/craftlink_builder.py --selftest
    CraftLink-Builder.exe --selftest

覆盖：

  1. 客户端常量是**读出来的**，不是手抄的（值与 client/** 一致）；
  2. branding.json：字段、UTF-8 无 BOM、回退规则，并用**客户端自己的解析器**
     （client/mclink/config/branding.py）再读一遍;
  3. params.iss：UTF-8 带 BOM、必需的 #define 齐全、路径形态；
  4. 输入校验：端口 / 带 http:// 的地址 / 数据目录落在安装目录里 / 输出目录保护；
  5. 图片：PNG 解码 + 生成 .ico（用独立的解析器验结构，不信任自己的写入器）；
  6. CLI：--non-interactive --json / --check / --print-json-template 的退出码与产物；
  7. GUI：真的建一次窗口、填表、点「生成」，断言产物（没有显示环境就跳过）；
  8. "不硬编码服务器地址/凭据"的静态扫描。
"""

import argparse
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(TESTS_DIR))
BUILDER_DIR = os.path.join(REPO_ROOT, "installer", "builder")

sys.path.insert(0, BUILDER_DIR)

import builder_core      # noqa: E402
import imageconv         # noqa: E402

APP_PNG = os.path.join(REPO_ROOT, "client", "mclink", "assets", "app.png")
APP_ICO = os.path.join(REPO_ROOT, "client", "mclink", "assets", "app.ico")
CLIENT_EXE = os.path.join(REPO_ROOT, "client", "dist", "CraftLink.exe")

RESULTS = []


def check(name, condition, detail=""):
    RESULTS.append((bool(condition), name, detail))

    mark = "✓" if condition else "✗"
    line = f"  {mark} {name}"

    if detail and not condition:
        line += f"  <- {detail}"

    print(line, flush=True)

    return bool(condition)


def section(title):
    print(f"\n== {title} ==", flush=True)


# ============================================================================
#  1. 客户端常量
# ============================================================================

def test_client_facts(repo_root):
    section("客户端常量（必须是读出来的）")

    facts = builder_core.read_client_facts(repo_root)

    check("读到了全部必需常量", not facts.missing(), ",".join(facts.missing()))
    check("app_dir_name == CraftLink", facts.app_dir_name == "CraftLink", facts.app_dir_name)
    check("pointer_filename == data_dir.txt", facts.pointer_filename == "data_dir.txt", facts.pointer_filename)
    check("env_data_dir == MCLINK_DATA_DIR", facts.env_data_dir == "MCLINK_DATA_DIR", facts.env_data_dir)
    check("logs_subdir == logs", facts.logs_subdir == "logs", facts.logs_subdir)
    check("cache_subdir == cache", facts.cache_subdir == "cache", facts.cache_subdir)
    check("branding_filename == branding.json", facts.branding_filename == "branding.json", facts.branding_filename)
    check("logo 白名单含 .png/.ico", ".png" in facts.logo_suffixes and ".ico" in facts.logo_suffixes,
          str(facts.logo_suffixes))
    check("版本号与 client 源码一致（形如 x.y.z）",
          bool(facts.app_version) and facts.app_version[0].isdigit(), facts.app_version)

    # 和客户端模块直接对照（不是"看起来对"）
    sys.path.insert(0, os.path.join(repo_root, "client"))
    try:
        from mclink.config import userdirs as client_userdirs
        from mclink.config import branding as client_branding

        check("与 client userdirs.APP_DIR_NAME 一致",
              facts.app_dir_name == client_userdirs.APP_DIR_NAME)
        check("与 client userdirs.POINTER_FILENAME 一致",
              facts.pointer_filename == client_userdirs.POINTER_FILENAME)
        check("与 client userdirs.ENV_DATA_DIR 一致",
              facts.env_data_dir == client_userdirs.ENV_DATA_DIR)
        check("与 client branding.BRANDING_FILENAME 一致",
              facts.branding_filename == client_branding.BRANDING_FILENAME)
    finally:
        sys.path.remove(os.path.join(repo_root, "client"))

    return facts


# ============================================================================
#  2. 小工具
# ============================================================================

def test_helpers():
    section("小工具（转义 / 版本号 / 目录名）")

    check("iss_text 转义双引号", builder_core.iss_text('a"b') == 'a""b')
    check("iss_text 转义单引号", builder_core.iss_text("a'b") == "a''b")
    check("iss_text 去掉花括号", builder_core.iss_text("a{b}c") == "a(b)c")
    check("iss_text 换行变空格", builder_core.iss_text("a\nb\tc") == "a b c")

    check("version_info_version 3.9.20 -> 3.9.20.0",
          builder_core.version_info_version("3.9.20") == "3.9.20.0")
    check("version_info_version v3.9.20-beta -> 3.9.20.0",
          builder_core.version_info_version("v3.9.20-beta") == "3.9.20.0")
    check("version_info_version 空 -> 1.0.0.0",
          builder_core.version_info_version("") == "1.0.0.0")

    check("非法目录字符被去掉",
          builder_core.sanitize_dir_name('方块/特快:V2*') == "方块特快V2")
    check("目录名为空时回退", builder_core.sanitize_dir_name("///") == "CraftLink")
    check("注册表键名只留安全字符",
          builder_core.sanitize_registry_key("A B\\C") == "A-B-C")

    check("Windows 路径包含判断（嵌套）",
          builder_core._win_path_contains("D:\\App", "D:\\App\\data"))
    check("Windows 路径包含判断（大小写/斜杠不敏感）",
          builder_core._win_path_contains("d:/app/", "D:\\APP\\Data"))
    check("Windows 路径包含判断（兄弟目录不算）",
          not builder_core._win_path_contains("D:\\App", "D:\\AppData"))

    check("绝对路径会被提示（escape hazard）",
          bool(builder_core.check_iss_path("C:\\temp\\x")) is True)
    check("普通相对路径不提示", builder_core.check_iss_path("build\\staging") == "")


# ============================================================================
#  3. 输入校验
# ============================================================================

def test_validation(repo_root, facts, workdir):
    section("输入校验")

    def run(**kwargs):
        inputs = builder_core.inputs_from_mapping(kwargs)

        return builder_core.validate(inputs, repo_root, facts)

    result = run(app_name="方块特快", server_host="relay.example.com", server_port=19100)
    check("合法输入没有错误", result.ok, "; ".join(result.errors))

    result = run(app_name="X", server_host="relay.example.com", server_port=70000)
    check("端口越界被拒", not result.ok)

    result = run(app_name="X", server_host="relay.example.com", server_port=0)
    check("有地址没端口被拒", not result.ok)

    result = run(app_name="X", server_host="https://relay.example.com", server_port=443)
    check("地址带 http:// 被拒", not result.ok)

    result = run(app_name="X", server_host="", server_port=0)
    check("地址留空是合法状态（有提醒）",
          result.ok and any("留空" in item for item in result.warnings))

    result = run(app_name="X", logo=os.path.join(workdir, "nope.png"))
    check("logo 不存在被拒", not result.ok)

    result = run(app_name="X", logo=APP_ICO)
    check(".ico logo 合法", result.ok, "; ".join(result.errors))

    result = run(app_name="X", default_install_dir="D:\\App", default_data_dir="D:\\App\\data")
    check("数据目录落在安装目录里被拒", not result.ok)

    result = run(app_name="X", default_install_dir="D:\\App", default_data_dir="D:\\AppData")
    check("数据目录是兄弟目录时合法", result.ok, "; ".join(result.errors))

    result = run(app_name="X", default_install_dir="relative\\path")
    check("默认安装目录必须绝对路径", not result.ok)

    result = run(app_name="X", app_id="not-a-guid")
    check("app-id 必须是 GUID", not result.ok)

    result = run(app_name="X", output_basename="bad name")
    check("安装包文件名不能有空格", not result.ok)

    result = run(app_name="X", update_url="ftp://x/y")
    check("更新地址必须是 http(s)", not result.ok)

    result = run(app_name="X", texts={"nope": "x"})
    check("未知文本键只是提醒", result.ok and any("文本键" in item for item in result.warnings))


# ============================================================================
#  4. branding.json + 客户端解析器复读
# ============================================================================

def test_branding(repo_root, facts, workdir):
    section("branding.json（并用客户端解析器复读）")

    out = os.path.join(workdir, "brand-full")
    inputs = builder_core.inputs_from_mapping(
        {
            "app_name": "方块特快",
            "server_host": "relay.example.com",
            "server_port": 19100,
            "update_url": "https://relay.example.com/download",
            "logo": APP_PNG,
            "out": out,
            "exe": CLIENT_EXE,
        }
    )
    report = builder_core.build(inputs, repo_root, out_dir=out, force=True)
    check("生成成功", report.get("ok"), "; ".join(report.get("errors") or []))

    branding_path = os.path.join(out, "staging", facts.branding_filename)
    check("branding.json 写到了 staging", os.path.isfile(branding_path))

    raw = open(branding_path, "rb").read()
    check("UTF-8 且**没有 BOM**", not raw.startswith(b"\xef\xbb\xbf"))
    check("能被 UTF-8 解码", raw.decode("utf-8") is not None)

    data = json.loads(raw.decode("utf-8"))
    check("字段集合恰好是文档里的 5 个（+ 无 _comment）",
          set(data) == {"app_name", "logo_file", "default_server_host",
                        "default_server_port", "update_url"}, str(sorted(data)))
    check("app_name 正确（中文）", data["app_name"] == "方块特快", data["app_name"])
    check("logo_file 是相对文件名", data["logo_file"] == "branding-logo.png", data["logo_file"])
    check("default_server_host 来自输入", data["default_server_host"] == "relay.example.com")
    check("default_server_port 是整数", data["default_server_port"] == 19100)
    check("update_url 来自输入", data["update_url"].startswith("https://"))
    check("logo 文件被复制进 staging",
          os.path.isfile(os.path.join(out, "staging", "branding-logo.png")))
    check("主程序被复制进 staging",
          os.path.isfile(os.path.join(out, "staging", "CraftLink.exe")))
    check("报告里记了 sha256",
          len(report.get("staged", {}).get("CraftLink.exe", {}).get("sha256", "")) == 64)

    # 用**客户端自己的解析器**验证字段真的会被这样理解
    sys.path.insert(0, os.path.join(repo_root, "client"))
    try:
        from mclink.config import branding as client_branding

        parsed = client_branding.parse(data, source=branding_path,
                                       base_dir=os.path.dirname(branding_path))
        check("客户端 app_name 解析一致", parsed.app_name == "方块特快", parsed.app_name)
        check("客户端 default_server_host 一致",
              parsed.default_server_host == "relay.example.com")
        check("客户端 default_server_port 一致", parsed.default_server_port == 19100)
        check("客户端 has_server_default() 为真", parsed.has_server_default())
        check("客户端把 logo 解析成 staging 里的绝对路径",
              os.path.isfile(parsed.logo_file)
              and os.path.basename(parsed.logo_file) == "branding-logo.png",
              parsed.logo_file)
        check("客户端 logo_image() 认得 .png（关于页图片）",
              parsed.logo_image() != "")
        check("客户端解析没有警告", not parsed.warnings, "; ".join(parsed.warnings))
    finally:
        sys.path.remove(os.path.join(repo_root, "client"))

    # --- 空服务器地址（D10 的默认状态）---
    out2 = os.path.join(workdir, "brand-empty-server")
    inputs = builder_core.inputs_from_mapping({"app_name": "CraftLink", "out": out2, "exe": CLIENT_EXE})
    report = builder_core.build(inputs, repo_root, out_dir=out2, force=True)
    check("地址/端口留空时也能生成", report.get("ok"), "; ".join(report.get("errors") or []))
    check("地址留空 -> 没有 branding.json（全默认值）",
          not report.get("has_branding_json"),
          str(report.get("outputs")))

    # --- 只给 logo 也要写 branding.json ---
    out3 = os.path.join(workdir, "brand-logo-only")
    inputs = builder_core.inputs_from_mapping(
        {"app_name": "CraftLink", "logo": APP_PNG, "out": out3, "exe": CLIENT_EXE}
    )
    report = builder_core.build(inputs, repo_root, out_dir=out3, force=True)
    check("只给 logo 时仍然写 branding.json", report.get("has_branding_json"))
    data = json.load(open(os.path.join(out3, "staging", facts.branding_filename), encoding="utf-8"))
    check("只给 logo 时地址保持空", data["default_server_host"] == "" and data["default_server_port"] == 0)

    # --- 端口非法不能悄悄写进去 ---
    out4 = os.path.join(workdir, "brand-bad-port")
    inputs = builder_core.inputs_from_mapping(
        {"app_name": "X", "server_host": "relay.example.com", "server_port": 70000,
         "out": out4, "exe": CLIENT_EXE}
    )
    report = builder_core.build(inputs, repo_root, out_dir=out4, force=True)
    check("端口非法 -> 生成失败", not report.get("ok"))

    # --- 输出目录保护 ---
    guard = os.path.join(workdir, "not-ours")
    os.makedirs(guard, exist_ok=True)
    open(os.path.join(guard, "user-file.txt"), "w", encoding="utf-8").write("x")
    inputs = builder_core.inputs_from_mapping({"app_name": "X", "out": guard, "exe": CLIENT_EXE})
    report = builder_core.build(inputs, repo_root, out_dir=guard, force=False)
    check("不认识的非空输出目录会被拒绝", not report.get("ok"))
    report = builder_core.build(inputs, repo_root, out_dir=guard, force=True)
    check("--force 后可以写", report.get("ok"))
    check("--force 不会删掉目录里原有的用户文件",
          os.path.isfile(os.path.join(guard, "user-file.txt")))

    return out


# ============================================================================
#  5. params.iss
# ============================================================================

def test_params(repo_root, facts, workdir, build_dir):
    section("params.iss（Inno 编译参数）")

    path = os.path.join(build_dir, builder_core.PARAMS_FILENAME)
    raw = open(path, "rb").read()

    check("params.iss 存在", os.path.isfile(path))
    check("params.iss 是 UTF-8 **带 BOM**（中文品牌名才不会乱码）",
          raw.startswith(b"\xef\xbb\xbf"))
    check("params.iss 能按 UTF-8 解码", raw.decode("utf-8-sig") is not None)

    text = raw.decode("utf-8-sig").replace("\r\n", "\n")
    required = [
        "AppIdLine", "AppName", "AppVersion", "AppVersionInfo", "AppPublisher",
        "DefaultDirNameLine", "OutputDirLine", "OutputBaseFilename",
        "StagingDirLine", "MainExeName", "BrandingFileName", "LogoStagedName",
        "LogoStagedPrefix", "ClientAppDirName", "ClientDefaultDataDir",
        "PointerFileName", "LogsSubdirName", "CacheSubdirName", "EnvVarName",
        "DataDirParamName", "DataDirDefault", "RegistryKeyName", "MinVersionLine",
    ]
    missing = [name for name in required if f"#define {name} " not in text]
    check("必需的 #define 都在", not missing, ",".join(missing))

    for flag in ("HasBranding", "HasLogo", "DataDirPageEnabled",
                 "DesktopIconTaskEnabled", "EnvVarTaskEnabled"):
        check(f"开了开关 {flag}", f"#define {flag}\n" in (text + "\n"))

    check("模板要求的每个 {#XXX} 都有对应定义",
          _template_defines_satisfied(repo_root, text))

    check("AppIdLine 是 {{GUID} 形式（Inno 的转义写法）",
          "#define AppIdLine \"{{" in text and text.count("AppIdLine") >= 1,
          [line for line in text.splitlines() if "AppIdLine" in line][:1])
    check("AppId 默认值与 builder_core 一致",
          builder_core.DEFAULT_APP_GUID in text)
    check("安装目录默认值用 {autopf}（不写死盘符）",
          '#define DefaultDirNameLine "{autopf}\\' in text,
          [line for line in text.splitlines() if "DefaultDirNameLine" in line][:1])
    check("数据目录默认值 = 客户端默认目录（%APPDATA%\\CraftLink）",
          f'#define DataDirDefault "{{userappdata}}\\{facts.app_dir_name}"' in text,
          [line for line in text.splitlines() if "DataDirDefault" in line][:1])
    check("指针文件目录也是客户端默认目录（不是被指向的目录）",
          f'#define ClientDefaultDataDir "{{userappdata}}\\{facts.app_dir_name}"' in text)
    check("环境变量名来自客户端", f'#define EnvVarName "{facts.env_data_dir}"' in text)
    check("指针文件名来自客户端", f'#define PointerFileName "{facts.pointer_filename}"' in text)
    check("参数里保留了 Inno 常量（没有被转义逻辑吃掉）",
          "{userappdata}" in text and "{autopf}" in text)

    # 默认布局（输出目录在 installer/ 下）必须是相对路径
    relative, is_absolute = builder_core._path_for_iss(
        os.path.join(repo_root, "installer", "build", "staging"),
        os.path.join(repo_root, "installer"),
    )
    check("默认布局下 staging 路径是相对路径", relative == "build\\staging" and not is_absolute, relative)

    # 报告的输入 == 参数里的值（防止"报告一套、编译另一套"）
    report = json.load(open(os.path.join(build_dir, builder_core.REPORT_FILENAME), encoding="utf-8"))
    check("report 里的 app_name 与 params 一致",
          f'#define AppName "{report["inputs"]["app_name"]}"' in text)
    check("report 里有 next_steps", bool(report.get("next_steps")))
    check("NEXT-STEPS.txt 与 report 一致",
          open(os.path.join(build_dir, builder_core.NEXT_STEPS_FILENAME), encoding="utf-8").read()
          == report["next_steps"])
    check("next_steps 里给了 ISCC 命令",
          "ISCC.exe" in report["next_steps"] and "installer\\CraftLink.iss" in report["next_steps"])
    check("next_steps 里给了静默安装参数",
          "/VERYSILENT" in report["next_steps"] and "/DATA_DIR=" in report["next_steps"])

    return text


def _template_defines_satisfied(repo_root, params_text):
    """
    模板里用到的每个 `{#X}` 都必须有定义 —— 除非它的**每一次**使用都在
    `#ifdef Y` 块里，而 Y 这次没有被打开（例如 --language-file 没给时的
    LanguageName）。

    这个检查就是把"编译时未知标识符"这种错误挡在 Linux 上（ISCC 在 Windows，
    我们在这里跑不了它）。
    """
    import re

    template = open(
        os.path.join(repo_root, "installer", "CraftLink.iss"), "r", encoding="ascii"
    ).read()

    defined = set(re.findall(r"^#define\s+([A-Za-z0-9_]+)", params_text, re.MULTILINE))
    problems = []
    stack = []

    for number, line in enumerate(template.splitlines(), start=1):
        stripped = line.strip()

        if stripped.startswith("#ifdef") or stripped.startswith("#ifndef"):
            parts = stripped.split()

            if len(parts) >= 2:
                stack.append(parts[1])

            continue

        if stripped.startswith("#endif"):
            if stack:
                stack.pop()

            continue

        for name in re.findall(r"\{#([A-Za-z0-9_]+)\}", line):
            if name in defined:
                continue

            # 只在没打开的 #ifdef 里用 -> 编译时那几行根本不存在，合法。
            if stack and any(flag not in defined for flag in stack):
                continue

            problems.append(f"第 {number} 行用到 {{#{name}}} 但没有定义")

    if problems:
        print("      " + "; ".join(problems[:6]))

    return not problems


# ============================================================================
#  6. 图片
# ============================================================================

def test_images(workdir):
    section("图片（PNG -> ICO）")

    if not os.path.isfile(APP_PNG):
        check("有 app.png 可测", False, APP_PNG)

        return

    info = imageconv.sniff(APP_PNG)
    check("app.png 被识别为 PNG", info["kind"] == "png" and info["width"] == 256, str(info))

    width, height, rgba = imageconv.decode_png(APP_PNG)
    check("解码尺寸正确", (width, height) == (256, 256))
    check("解码出 RGBA 长度正确", len(rgba) == 256 * 256 * 4)
    corner = rgba[0:4]
    center = rgba[(128 * 256 + 128) * 4:(128 * 256 + 128) * 4 + 4]
    check("左上角是透明的（PNG alpha 生效）", corner[3] == 0, str(corner))
    check("中心是不透明的彩色像素", center[3] == 255 and len(set(center[:3])) > 1, str(center))

    target = os.path.join(workdir, "setup-icon.ico")
    imageconv.write_ico_from_image(APP_PNG, target)
    check("生成了 .ico", os.path.isfile(target))

    entries = imageconv.ico_entries(target)
    sizes = [entry["width"] for entry in entries]
    check("包含 16/32/48/256 四个尺寸", sizes == [16, 32, 48, 256], str(sizes))
    check("条目都是位图（不用 PNG 压缩条目）",
          all(entry["format"] == "bmp" for entry in entries))

    # 独立解析：不调用写入器，直接按 ICO/BMP 规范把像素读回来对比
    data = open(target, "rb").read()
    check("文件头是 00 00 01 00", data[:4] == b"\x00\x00\x01\x00")

    count = struct.unpack_from("<H", data, 4)[0]
    bad = []

    for index in range(count):
        bw, bh, colors, reserved, planes, bpp, size, offset = struct.unpack_from(
            "<BBBBHHII", data, 6 + 16 * index
        )
        side = bw or 256
        header = struct.unpack_from("<IiiHHIIiiII", data, offset)
        payload_end = offset + size

        if not (reserved == 0 and planes == 1 and bpp == 32
                and header[0] == 40 and header[1] == side and header[2] == side * 2
                and payload_end <= len(data)):
            bad.append((side, reserved, planes, bpp, header[:3], payload_end, len(data)))
            continue

        expected = imageconv.resize_rgba(rgba, width, height, side, side)
        mismatch = 0

        for y in range(side):
            for x in range(side):
                pointer = offset + 40 + ((side - 1 - y) * side + x) * 4
                source = (y * side + x) * 4

                if (data[pointer + 2], data[pointer + 1], data[pointer],
                        data[pointer + 3]) != tuple(expected[source:source + 4]):
                    mismatch += 1

        if mismatch:
            bad.append((side, f"{mismatch} 个像素不一致"))

    check("每个尺寸的位图头/偏移/像素都能被独立读回", not bad, str(bad))

    # 独立工具（file(1)）也认 —— 有些环境没有 file，就当提示不算失败
    file_tool = shutil.which("file")

    if file_tool:
        output = subprocess.run([file_tool, target], capture_output=True, text=True).stdout
        check("file(1) 认这是 Windows 图标资源",
              "MS Windows icon resource" in output, output.strip())
    else:
        print("  · 跳过 file(1) 校验（本机没有 file）")

    # 坏输入要明确报错，而不是产出坏图标
    junk = os.path.join(workdir, "junk.png")
    open(junk, "wb").write(b"not a png at all")

    try:
        imageconv.decode_png(junk)
        check("垃圾输入会抛 ImageError", False)
    except imageconv.ImageError as exc:
        check("垃圾输入会抛 ImageError", True, str(exc))

    if os.path.isfile(APP_ICO):
        copied = os.path.join(workdir, "copied.ico")
        imageconv.write_ico_from_image(APP_ICO, copied)
        check("已有 .ico 原样复制",
              open(copied, "rb").read() == open(APP_ICO, "rb").read())


# ============================================================================
#  7. PE 架构
# ============================================================================

def test_pe():
    section("主程序架构（决定安装器的架构开关）")

    if not os.path.isfile(CLIENT_EXE):
        print("  · 跳过：client/dist/CraftLink.exe 还没构建")
        check("主程序不存在时不猜架构", builder_core.pe_architecture(CLIENT_EXE) == "")

        return

    arch = builder_core.pe_architecture(CLIENT_EXE)
    check("从 PE 头读出 x64", arch == "x64", arch)


# ============================================================================
#  8. CLI
# ============================================================================

def test_cli(repo_root, workdir):
    section("CLI（--non-interactive / --check / --print-json-template）")

    entry = os.path.join(BUILDER_DIR, "craftlink_builder.py")

    # 冻结成 exe 之后（CraftLink-Builder.exe --selftest）就没有源码文件了，
    # 直接再调自己一次。
    frozen = bool(getattr(sys, "frozen", False))
    base_command = [sys.executable] + ([] if frozen else [entry])

    def run(*args):
        return subprocess.run(
            [*base_command, *args],
            capture_output=True, text=True, cwd=repo_root,
        )

    result = run("--version")
    check("--version 退出码 0", result.returncode == 0, result.stderr)

    result = run("--print-json-template")
    check("--print-json-template 输出合法 JSON",
          result.returncode == 0 and isinstance(json.loads(result.stdout.split("\n# 用法")[0]), dict),
          result.stderr)

    out = os.path.join(workdir, "cli-out")
    payload = {
        "app_name": "方块特快",
        "server_host": "relay.example.com",
        "server_port": 19100,
        "logo": APP_PNG,
        "out": out,
        "exe": CLIENT_EXE,
    }
    json_path = os.path.join(workdir, "cli-input.json")
    json.dump(payload, open(json_path, "w", encoding="utf-8"), ensure_ascii=False)

    result = run("--non-interactive", "--json", json_path)
    check("--non-interactive --json 退出码 0", result.returncode == 0, result.stderr)
    check("生成了 params.iss", os.path.isfile(os.path.join(out, "params.iss")))
    check("生成了 builder-report.json", os.path.isfile(os.path.join(out, "builder-report.json")))
    check("stdout 里给了下一步命令", "ISCC" in result.stdout, result.stdout[-400:])

    # 参数覆盖 JSON
    out2 = os.path.join(workdir, "cli-out2")
    result = run("--non-interactive", "--json", json_path, "--out", out2,
                 "--server-host", "", "--server-port", "0")
    check("命令行参数覆盖 JSON 生效", result.returncode == 0, result.stderr)
    data = json.load(open(os.path.join(out2, "staging", "branding.json"), encoding="utf-8"))
    check("覆盖后地址为空", data["default_server_host"] == "")

    # --check 不写文件
    out3 = os.path.join(workdir, "cli-check")
    result = run("--check", "--out", out3, "--app-name", "X",
                 "--server-host", "relay.example.com", "--server-port", "19100")
    check("--check 通过时退出码 0", result.returncode == 0, result.stderr)
    check("--check 不写任何文件", not os.path.exists(out3))

    result = run("--check", "--app-name", "X", "--server-host", "relay.example.com",
                 "--server-port", "70000")
    check("--check 发现错误时退出码 2", result.returncode == 2, result.stdout)

    result = run("--non-interactive", "--json", os.path.join(workdir, "nope.json"))
    check("JSON 不存在时退出码非 0", result.returncode != 0)


# ============================================================================
#  9. GUI
# ============================================================================

def test_gui(repo_root, workdir):
    section("GUI（真的建窗口、填表、点生成）")

    try:
        import tkinter
    except ImportError:
        print("  · 跳过：没有 tkinter")
        return

    try:
        probe = tkinter.Tk()
        probe.destroy()
    except Exception as exc:  # noqa: BLE001
        print(f"  · 跳过：没有可用显示（{exc}）")
        return

    import builder_gui

    class Recorder:
        """替掉模态弹窗：自检里没人点"确定"。"""

        def __init__(self):
            self.calls = []

        def error(self, title, message):
            self.calls.append(("error", title, message))

        def info(self, title, message):
            self.calls.append(("info", title, message))

        def warning(self, title, message):
            self.calls.append(("warning", title, message))

    dialogs = Recorder()
    out = os.path.join(workdir, "gui-out")
    app = builder_gui.BuilderApp(repo_root=repo_root, out_dir=out, dialogs=dialogs)

    try:
        app.vars["app_name"].set("方块特快")
        app.vars["server_host"].set("relay.example.com")
        app.vars["server_port"].set("19100")
        app.vars["logo"].set(APP_PNG)
        app.vars["exe"].set(CLIENT_EXE)
        app.vars["default_install_dir"].set("D:\\Apps\\Kuaikuai")

        collected = app.collect_inputs()
        check("GUI 表单 -> Inputs 正确", collected.app_name == "方块特快"
              and collected.server_port == 19100
              and collected.default_install_dir == "D:\\Apps\\Kuaikuai")
        check("GUI 的目标输出目录不被预填值覆盖", collected.out_dir == out, collected.out_dir)

        report = app.generate()
        check("GUI 点「生成」成功", bool(report and report.get("ok")),
              str((report or {}).get("errors")))
        check("GUI 生成物存在", os.path.isfile(os.path.join(out, "params.iss")),
              out)
        check("GUI 文本区拿到了下一步",
              "ISCC" in app.log_widget.get("1.0", "end"))
        check("GUI 记住最后一次报告（复制按钮用）", app.last_report is not None)
        check("GUI 复制「下一步」不炸", _copy_ok(app))

        app.vars["server_port"].set("70000")
        bad = app.generate()
        check("GUI 遇到非法端口不会写坏产物", not bad)
        check("GUI 用弹窗告诉了用户原因",
              any(kind == "error" for kind, _title, _message in dialogs.calls),
              str(dialogs.calls[-1:]))
    finally:
        try:
            app.root.destroy()
        except Exception:
            pass


def _copy_ok(app):
    try:
        app.copy_next_steps()

        return True
    except Exception:  # noqa: BLE001
        return False


# ============================================================================
#  10. 不许硬编码地址/凭据
# ============================================================================

def test_no_hardcoded(repo_root):
    section("静态扫描：不硬编码服务器地址/凭据")

    targets = []
    installer_dir = os.path.join(repo_root, "installer")

    for base, _dirs, files in os.walk(installer_dir):
        if os.sep + "build" in base:
            continue

        for name in files:
            if name.endswith((".py", ".iss", ".md", ".json", ".sh", ".cmd")):
                targets.append(os.path.join(base, name))

    for name in ("build-installer.sh", "verify-installer-inputs.py", "build-builder.sh"):
        path = os.path.join(repo_root, "scripts", name)

        if os.path.isfile(path):
            targets.append(path)

    # ★ 2026-10-05（集成修复）：禁止清单本身**不能**把这个域名写成字面量 ——
    #   这个检查会扫自己所在的目录，写全了就等于"守卫自己举报自己"（实测 134/135）。
    #   拆成两段拼出来：扫描器看不见连起来的整串，检查能力一点没减。
    _legacy_domain = "superyk7" + "hero"
    forbidden = (_legacy_domain, "www." + _legacy_domain + ".online")

    hits = []

    for path in targets:
        # 本文件自己就带着这些"禁用词"字面量（它就是来查这些词的）。
        if os.path.abspath(path) == os.path.abspath(__file__):
            continue

        try:
            text = open(path, "r", encoding="utf-8", errors="replace").read()
        except OSError:
            continue

        for needle in forbidden:
            if needle in text:
                hits.append(f"{os.path.relpath(path, repo_root)}: {needle}")

    check("安装器相关文件里没有历史域名", not hits, "; ".join(hits))

    check("Builder 默认输入里没有服务器地址",
          builder_core.Inputs().server_host == ""
          and builder_core.Inputs().server_port == 0)
    check("Builder 默认输出名不含路径", builder_core.Inputs().output_basename == "CraftLink-Setup")
    check("默认安装目录由安装器用 {autopf} 推导（不在 Python 里写死）",
          not builder_core.Inputs().default_install_dir)
    check("模板是纯 ASCII（编码上没有歧义）",
          _is_ascii(os.path.join(repo_root, "installer", "CraftLink.iss")))
    check("模板里没有绝对盘符路径",
          "C:\\" not in open(os.path.join(repo_root, "installer", "CraftLink.iss"),
                             encoding="ascii").read())


def _is_ascii(path):
    data = open(path, "rb").read()

    try:
        data.decode("ascii")

        return True
    except UnicodeDecodeError:
        return False


# ============================================================================
#  入口
# ============================================================================

def main(argv=None):
    parser = argparse.ArgumentParser(description="CraftLink-Builder 自检")
    parser.add_argument("--repo-root", default=REPO_ROOT)
    parser.add_argument("--keep", action="store_true", help="保留临时目录（排查用）")
    args = parser.parse_args(argv)

    repo_root = os.path.abspath(args.repo_root)

    print(f"{builder_core.BUILDER_NAME} {builder_core.BUILDER_VERSION} 自检")
    print(f"仓库根：{repo_root}")
    print(f"Python：{sys.version.split()[0]}  平台：{sys.platform}")

    workdir = tempfile.mkdtemp(prefix="craftlink-builder-selftest-")

    try:
        facts = test_client_facts(repo_root)
        test_helpers()
        test_validation(repo_root, facts, workdir)

        build_dir = os.path.join(workdir, "brand-full")
        test_branding(repo_root, facts, workdir)
        test_params(repo_root, facts, workdir, build_dir)
        test_images(workdir)
        test_pe()
        test_cli(repo_root, workdir)
        test_gui(repo_root, workdir)
        test_no_hardcoded(repo_root)
    finally:
        if args.keep:
            print(f"\n临时目录保留在：{workdir}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)

    passed = sum(1 for ok, _name, _detail in RESULTS if ok)
    total = len(RESULTS)
    failed = [name for ok, name, _detail in RESULTS if not ok]

    print("\n" + "=" * 60)
    print(f"{passed}/{total} 通过")

    if failed:
        print("失败项：")

        for name in failed:
            print(f"  ✗ {name}")

        return 1

    print("自检全部通过 ✓（尚未覆盖的：Inno Setup 编译与 Windows 上的真实安装，" 
          "见 docs/INSTALLER.md 的「必须在 Windows 上验证」）")

    return 0


if __name__ == "__main__":
    sys.exit(main())
