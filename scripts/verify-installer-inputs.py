#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
校验"安装器输入"的内容 —— 在 Linux/WSL 上就能跑，不需要 Inno Setup。

它回答的问题是：**Builder 生成的东西，是不是真的能被客户端和 Inno Setup 正确理解？**

    用法：
        python3 scripts/verify-installer-inputs.py                    # 校验 installer/build
        python3 scripts/verify-installer-inputs.py --out <目录>
        python3 scripts/verify-installer-inputs.py --require-exe      # 主程序必须已构建
        python3 scripts/verify-installer-inputs.py --json             # CI 用

校验的东西（全部是"内容断言"，不是"文件存在就行"）：

  A. installer/CraftLink.iss：纯 ASCII、没有硬编码路径/地址、必需片段齐全、
     #ifdef/#endif 配平、卸载**不删**用户数据；
  B. installer/build/params.iss：UTF-8 **带 BOM**、必需 #define 齐全、模板用到的
     每个 {#X} 都有定义、默认布局下路径是相对路径、开关与主程序架构一致；
  C. staging/branding.json：UTF-8 **无 BOM**、字段集合恰好是文档里的 5 个、
     类型/范围正确、server 两个字段不会"只填一半"，并且**用客户端自己的解析器
     再读一遍**（client/mclink/config/branding.py）；
  D. 与客户端既有机制的对接点：data_dir.txt / MCLINK_DATA_DIR / %APPDATA%\\CraftLink
     这些名字必须与 client/mclink/config/userdirs.py 里的常量**逐字相同**（直接
     import 客户端模块比对，不是抄一遍）；
  E. 产物完整性：staging 里文件的 sha256 与 builder-report.json 记的一致
     （防止"生成后又被人手改过"）；主程序存在且是 PE（--require-exe 时是错误）。

退出码：0 = 全过；1 = 有错误；2 = 用法/环境问题。
"""

import argparse
import hashlib
import json
import os
import re
import sys

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPTS_DIR)
BUILDER_DIR = os.path.join(REPO_ROOT, "installer", "builder")

sys.path.insert(0, BUILDER_DIR)

import builder_core     # noqa: E402
import imageconv        # noqa: E402

PARAMS_FILENAME = "params.iss"
REPORT_FILENAME = "builder-report.json"
STAGING_DIRNAME = "staging"

BRANDING_FIELDS = {
    "app_name", "logo_file", "default_server_host", "default_server_port", "update_url",
}

TEMPLATE_REQUIRED = [
    '#include "build\\params.iss"',
    "[Setup]",
    "[Files]",
    "[Icons]",
    "[InstallDelete]",
    "[Registry]",
    "[Run]",
    "[Code]",
    "AppId={#AppIdLine}",
    "DefaultDirName={#DefaultDirNameLine}",
    "PrivilegesRequiredOverridesAllowed=",
    "SaveStringsToUTF8FileWithoutBOM(",
    "CreateInputDirPage(wpSelectDir",
    "{param:{#DataDirParamName}|",
    "{#PointerFileName}",
    "{#EnvVarName}",
    "UninstallSilent()",
    "{#MsgUninstallKeepData}",
    "CurStepChanged(CurStep: TSetupStep)",
    "CurUninstallStepChanged(CurUninstallStep: TUninstallStep)",
]

PARAMS_REQUIRED = [
    "AppIdLine", "AppName", "AppVersion", "AppVersionInfo", "AppPublisher",
    "DefaultDirNameLine", "OutputDirLine", "OutputBaseFilename", "StagingDirLine",
    "MainExeName", "LogoStagedPrefix", "ClientAppDirName", "ClientDefaultDataDir",
    "PointerFileName", "LogsSubdirName", "CacheSubdirName", "EnvVarName",
    "DataDirParamName", "DataDirDefault", "RegistryKeyName", "MinVersionLine",
    "DataDirPageCaption", "DataDirPageDescription", "DataDirPageSubCaption",
    "DataDirPagePrompt", "TaskDesktopIconDesc", "TaskSetEnvVarDesc",
    "AdditionalTasksDesc", "LaunchDesc", "MsgDataDirEmpty", "MsgDataDirInsideApp",
    "MsgDataDirRelative", "MsgDataDirUnwritable", "MsgPointerFailedEnvSet",
    "MsgPointerFailed", "MsgUninstallKeepData",
]

PATH_DEFINES = ("OutputDirLine", "StagingDirLine", "SetupIconLine", "LicenseFileLine")


class Verifier:
    def __init__(self, out_dir, iss_path, require_exe, allow_missing=False):
        self.out_dir = os.path.abspath(out_dir)
        self.iss_path = os.path.abspath(iss_path)
        self.require_exe = require_exe
        self.allow_missing = allow_missing
        self.errors = []
        self.warnings = []
        self.checks = []

    # ---------------------------------------------------------------- 记账

    def check(self, name, condition, detail=""):
        ok = bool(condition)
        self.checks.append({"name": name, "ok": ok, "detail": str(detail)})

        if not ok:
            self.errors.append(f"{name}" + (f" —— {detail}" if detail else ""))

        return ok

    def warn(self, name, detail=""):
        self.checks.append({"name": name, "ok": True, "detail": f"[提醒] {detail}"})
        self.warnings.append(f"{name}" + (f" —— {detail}" if detail else ""))

    # ------------------------------------------------------------ A. 模板

    def check_template(self):
        if not os.path.isfile(self.iss_path):
            self.check("模板 installer/CraftLink.iss 存在", False, self.iss_path)

            return ""

        raw = open(self.iss_path, "rb").read()

        self.check("模板存在", True, self.iss_path)
        self.check("模板是纯 ASCII（编码上没有歧义）",
                   _is_ascii(raw), "含非 ASCII 字节")
        self.check("模板没有 BOM（纯 ASCII 不需要）",
                   not raw.startswith(b"\xef\xbb\xbf"))

        text = raw.decode("ascii", errors="replace")
        missing = [item for item in TEMPLATE_REQUIRED if item not in text]
        self.check("模板必需片段齐全", not missing, "; ".join(missing))

        # 卸载不能删用户数据
        code = text.split("[Code]", 1)[-1]
        self.check("卸载不递归删目录（没有 DelTree）", "DelTree(" not in code)
        self.check("卸载不删数据目录（没有 DeleteFile 指向数据目录）",
                   "DeleteFile(" not in code)

        # 不硬编码路径：只有在注释里才允许出现盘符
        code_lines = []

        for line in text.splitlines():
            stripped = line.strip()

            if not stripped:
                continue

            if stripped.startswith(";") or stripped.startswith("//"):
                continue

            if stripped.startswith("{") and stripped.endswith("}"):
                continue                    # Pascal 注释（[Code] 里那种）

            code_lines.append(line)

        drive_hits = [line for line in code_lines if re.search(r"[A-Za-z]:\\\\?[A-Za-z]", line)]
        self.check("模板里没有硬编码盘符路径（注释除外）", not drive_hits,
                   "; ".join(drive_hits[:3]))

        url_hits = [line for line in code_lines if "http://" in line or "https://" in line]
        self.check("模板里没有硬编码网址", not url_hits, "; ".join(url_hits[:3]))

        # #ifdef / #endif 配平（配平错了 ISCC 会报一堆莫名其妙的错）
        depth = 0
        opened = 0

        for line in text.splitlines():
            stripped = line.strip()

            if stripped.startswith(("#ifdef", "#ifndef")):
                depth += 1
                opened += 1
            elif stripped.startswith("#endif"):
                depth -= 1

        self.check("#ifdef/#endif 配平", depth == 0, f"结尾 depth={depth}")
        self.check("模板里有条件编译开关（可选功能靠它）", opened >= 5, f"#ifdef 个数={opened}")

        self.check("写指针文件用的是 WithoutBOM 版本（否则客户端读到 BOM 会拼出坏路径）",
                   "SaveStringsToUTF8File(" not in text.replace(
                       "SaveStringsToUTF8FileWithoutBOM(", ""))

        return text

    # ------------------------------------------------------- B. params.iss

    def _params_text(self):
        path = os.path.join(self.out_dir, PARAMS_FILENAME)

        if not os.path.isfile(path):
            return None, None

        raw = open(path, "rb").read()

        return raw, raw.decode("utf-8-sig", errors="replace")

    def check_params(self, template_text):
        raw, text = self._params_text()

        if raw is None:
            self.check("params.iss 存在", False,
                       f"{self.out_dir}（先跑 CraftLink-Builder）")

            return None

        self.check("params.iss 存在", True)
        self.check("params.iss 是 UTF-8 **带 BOM**（中文品牌名才不会乱码）",
                   raw.startswith(b"\xef\xbb\xbf"))
        self.check("params.iss 能被 UTF-8 解码", text is not None)

        normalized = text.replace("\r\n", "\n")
        defines = _parse_defines(normalized)

        missing = [name for name in PARAMS_REQUIRED if name not in defines]
        self.check("必需 #define 齐全", not missing, "; ".join(missing))

        for flag in ("HasBranding", "DataDirPageEnabled"):
            self.check(f"开关 {flag} 存在", f"#define {flag}\n" in normalized)

        self.check("AppIdLine 是 {{GUID} 形式（Inno 的转义写法）",
                   re.match(r"^\{\{[0-9A-Fa-f-]{36}\}$", defines.get("AppIdLine", "")) is not None,
                   defines.get("AppIdLine"))

        # 模板用到的 {#X} 必须有定义（没打开的 #ifdef 里的除外）
        problems = _unresolved_template_defines(template_text or "", defines)
        self.check("模板用到的 {#X} 都有定义", not problems, "; ".join(problems[:6]))

        # 路径形态
        for name in PATH_DEFINES:
            value = defines.get(name)

            if not value:
                continue

            self.check(f"{name} 不是写死的绝对路径（默认布局用相对路径）",
                       not re.match(r"^(?:[A-Za-z]:[\\/]|/)", value), value)

        self.check("安装目录默认值由 {autopf} 推导（用户可改）",
                   "{autopf}" in defines.get("DefaultDirNameLine", ""),
                   defines.get("DefaultDirNameLine"))
        self.check("默认数据目录就是 %APPDATA%\\<客户端目录名>",
                   defines.get("DataDirDefault", "").startswith("{userappdata}\\"))
        self.check("指针文件目录 = 客户端默认目录（不是被指向的目录）",
                   defines.get("ClientDefaultDataDir", "").startswith("{userappdata}\\"))

        # 架构开关与主程序一致
        exe_path = os.path.join(self.out_dir, STAGING_DIRNAME, defines.get("MainExeName", ""))
        arch = builder_core.pe_architecture(exe_path)

        if arch:
            self.check(f"ArchX64 与主程序架构（{arch}）一致",
                       ("ArchX64" in normalized) == (arch == "x64"),
                       f"arch={arch}")

        return defines

    # ------------------------------------------------ C/D. branding.json

    def check_branding(self, defines, report):
        staging = os.path.join(self.out_dir, STAGING_DIRNAME)
        branding_path = os.path.join(staging, defines.get("BrandingFileName", "branding.json"))
        expected = bool(report.get("has_branding_json")) if report else None
        exists = os.path.isfile(branding_path)

        if expected is False and not exists:
            self.check("全默认品牌 -> 不放 branding.json（符合 CLIENT-BRANDING §7）", True)

            return

        if not self.check("branding.json 存在", exists, branding_path):
            return

        raw = open(branding_path, "rb").read()

        self.check("branding.json 是 UTF-8 且**没有 BOM**",
                   not raw.startswith(b"\xef\xbb\xbf"))

        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self.check("branding.json 是合法 JSON", False, str(exc))

            return

        self.check("branding.json 是合法 JSON", isinstance(data, dict))

        if not isinstance(data, dict):
            return

        self.check("字段集合恰好是文档里的 5 个（未知字段会被忽略，但别多写）",
                   set(data) == BRANDING_FIELDS, str(sorted(data)))
        self.check("app_name 非空字符串",
                   isinstance(data.get("app_name"), str) and data["app_name"].strip() != "",
                   repr(data.get("app_name")))
        self.check("logo_file 是字符串", isinstance(data.get("logo_file"), str))
        self.check("default_server_host 是字符串",
                   isinstance(data.get("default_server_host"), str))
        self.check("default_server_port 是整数",
                   isinstance(data.get("default_server_port"), int)
                   and not isinstance(data.get("default_server_port"), bool),
                   repr(data.get("default_server_port")))

        port = data.get("default_server_port")
        host = data.get("default_server_host")

        self.check("端口在 1-65535 或未设置（0）",
                   isinstance(port, int) and (port == 0 or 1 <= port <= 65535), repr(port))
        self.check("地址与端口不会只填一半（客户端 has_server_default 要求两者都有）",
                   (bool(host) and bool(port)) or (not host and not port),
                   f"host={host!r} port={port!r}")

        url = data.get("update_url")

        if url:
            self.check("update_url 是 http(s)", str(url).lower().startswith(("http://", "https://")), url)

        # logo 文件确实躺在 staging 里，而且真的是图片
        logo = data.get("logo_file") or ""

        if logo:
            self.check("logo_file 是相对路径（相对 branding.json，即安装目录）",
                       not os.path.isabs(logo), logo)
            logo_path = os.path.join(staging, logo)
            info = imageconv.sniff(logo_path)

            self.check("logo 文件在 staging 里且是真实图片",
                       os.path.isfile(logo_path) and info["kind"] in ("png", "ico"),
                       f"{logo_path}: {info}")
        else:
            self.check("没有 logo 时 logo_file 为空（用内置图标）", logo == "")

        # ★ 用客户端自己的解析器复读：这是"与客户端对接"最硬的证据
        self._check_with_client_parser(branding_path, data)

        # 品牌值必须与 report 里的输入一致（防止"报告一套、产物另一套"）
        if report:
            inputs = report.get("inputs") or {}
            self.check("branding.app_name == 输入 app_name",
                       data.get("app_name") == (inputs.get("app_name") or "CraftLink"),
                       f"{data.get('app_name')!r} != {inputs.get('app_name')!r}")
            self.check("branding.default_server_host == 输入 server_host",
                       (data.get("default_server_host") or "") == (inputs.get("server_host") or ""))
            self.check("branding.default_server_port == 输入 server_port",
                       int(data.get("default_server_port") or 0) == int(inputs.get("server_port") or 0))

            if not (inputs.get("server_host") or ""):
                self.check("输入没给地址 -> 产物里也没有地址（D10：不硬编码）",
                           not data.get("default_server_host") and not data.get("default_server_port"))

    def _check_with_client_parser(self, branding_path, data):
        client_dir = os.path.join(REPO_ROOT, "client")

        if client_dir not in sys.path:
            sys.path.insert(0, client_dir)

        try:
            from mclink.config import branding as client_branding
        except Exception as exc:  # noqa: BLE001
            self.warn("用客户端解析器复读 branding.json",
                      f"导不进客户端模块（{exc.__class__.__name__}: {exc}）")

            return

        parsed = client_branding.parse(
            data, source=branding_path, base_dir=os.path.dirname(branding_path)
        )

        self.check("客户端解析：app_name 一致", parsed.app_name == data["app_name"],
                   f"{parsed.app_name!r} != {data['app_name']!r}")
        self.check("客户端解析：default_server_host 一致",
                   parsed.default_server_host == (data.get("default_server_host") or ""))
        self.check("客户端解析：default_server_port 一致",
                   parsed.default_server_port == int(data.get("default_server_port") or 0))
        self.check("客户端解析：没有警告（字段/类型都合法）",
                   not parsed.warnings, "; ".join(parsed.warnings))

        if data.get("logo_file"):
            self.check("客户端解析：logo 落到真实文件上",
                       os.path.isfile(parsed.logo_file), parsed.logo_file)

    # ---------------------------------------------- D. 与客户端常量一致

    def check_client_contract(self, defines):
        facts = builder_core.read_client_facts(REPO_ROOT)

        if facts.missing():
            self.check("读得到客户端常量", False, ",".join(facts.missing()))

            return

        pairs = [
            ("ClientAppDirName", facts.app_dir_name, "userdirs.APP_DIR_NAME"),
            ("PointerFileName", facts.pointer_filename, "userdirs.POINTER_FILENAME"),
            ("EnvVarName", facts.env_data_dir, "userdirs.ENV_DATA_DIR"),
            ("LogsSubdirName", facts.logs_subdir, "userdirs.LOGS_SUBDIR"),
            ("CacheSubdirName", facts.cache_subdir, "userdirs.CACHE_SUBDIR"),
            ("BrandingFileName", facts.branding_filename, "branding.BRANDING_FILENAME"),
        ]

        for define, expected, origin in pairs:
            value = defines.get(define)
            self.check(f"{define} 与客户端 {origin} 逐字相同",
                       value == expected, f"{value!r} != {expected!r}")

        self.check("ClientDefaultDataDir 用的是客户端目录名",
                   defines.get("ClientDefaultDataDir") == f"{{userappdata}}\\{facts.app_dir_name}",
                   defines.get("ClientDefaultDataDir"))

        # 直接 import 客户端模块再比一遍（build_core 读错时这一条会抓到）
        client_dir = os.path.join(REPO_ROOT, "client")

        if client_dir not in sys.path:
            sys.path.insert(0, client_dir)

        try:
            from mclink.config import userdirs as client_userdirs

            self.check("import 客户端 userdirs：POINTER_FILENAME 一致",
                       defines.get("PointerFileName") == client_userdirs.POINTER_FILENAME)
            self.check("import 客户端 userdirs：ENV_DATA_DIR 一致",
                       defines.get("EnvVarName") == client_userdirs.ENV_DATA_DIR)
            self.check("import 客户端 userdirs：APP_DIR_NAME 一致",
                       defines.get("ClientAppDirName") == client_userdirs.APP_DIR_NAME)
        except Exception as exc:  # noqa: BLE001
            self.check("import 客户端 userdirs", False, f"{exc.__class__.__name__}: {exc}")

    # -------------------------------------------------- E. 产物完整性

    def check_artifacts(self, defines, report):
        staging = os.path.join(self.out_dir, STAGING_DIRNAME)
        main_exe = os.path.join(staging, defines.get("MainExeName", "CraftLink.exe"))
        exists = os.path.isfile(main_exe)

        if self.require_exe:
            self.check("主程序已在 staging（--require-exe）", exists,
                       f"{main_exe}（先跑 pyinstaller scripts/mclink-client.spec）")
        elif exists:
            self.check("主程序已在 staging", True)
        else:
            self.warn("主程序还没构建", f"{main_exe}（编译安装器前必须先构建客户端）")

        if exists:
            with open(main_exe, "rb") as fh:
                head = fh.read(2)

            self.check("主程序是 PE 文件（MZ）", head == b"MZ", head.hex())

        icon = defines.get("SetupIconLine")

        if icon:
            icon_path = os.path.join(REPO_ROOT, "installer", icon.replace("\\", os.sep))
            entries = []

            if os.path.isfile(icon_path):
                try:
                    entries = imageconv.ico_entries(icon_path)
                except imageconv.ImageError as exc:
                    self.check("安装器图标是合法 .ico", False, str(exc))

            self.check("安装器图标存在且是合法 .ico（至少一个尺寸）",
                       bool(entries), icon_path)

            if entries:
                self.check("图标里没有 PNG 压缩条目（老 Inno 只认位图条目）",
                           all(entry["format"] == "bmp" for entry in entries),
                           str([entry["format"] for entry in entries]))

        # report 的 sha256 必须与磁盘一致（生成后被手改过 = 现在就能发现）
        if report:
            mismatched = []

            for name, info in (report.get("staged") or {}).items():
                path = info.get("path") or os.path.join(staging, name)

                if not os.path.isfile(path):
                    mismatched.append(f"{name}: 文件没了")
                    continue

                digest = hashlib.sha256(open(path, "rb").read()).hexdigest()

                if digest != info.get("sha256"):
                    mismatched.append(f"{name}: sha256 不一致（生成后被改过？）")

            self.check("staging 文件与 report 记录一致", not mismatched, "; ".join(mismatched))
            self.check("report.ok 为真", bool(report.get("ok")))

            next_steps = os.path.join(self.out_dir, "NEXT-STEPS.txt")

            if os.path.isfile(next_steps):
                text = open(next_steps, encoding="utf-8").read()
                self.check("NEXT-STEPS.txt 里有 ISCC 命令", "ISCC" in text)
                self.check("NEXT-STEPS.txt 里有静默安装参数",
                           "/VERYSILENT" in text and "/DATA_DIR=" in text)
                self.check("NEXT-STEPS.txt 里说明了卸载保留数据",
                           "保留" in text or "keep" in text.lower())

    # ------------------------------------------------------------- 运行

    def run(self):
        report = None
        report_path = os.path.join(self.out_dir, REPORT_FILENAME)

        if os.path.isfile(report_path):
            try:
                report = json.load(open(report_path, encoding="utf-8"))
            except json.JSONDecodeError as exc:
                self.check("builder-report.json 是合法 JSON", False, str(exc))

        template_text = self.check_template()
        defines = self.check_params(template_text)

        if defines is None:
            return False

        self.check_branding(defines, report)
        self.check_client_contract(defines)
        self.check_artifacts(defines, report)

        # 输出目录里不许出现历史域名（硬编码服务器的老毛病）
        hits = []

        for base, _dirs, files in os.walk(self.out_dir):
            for name in files:
                if not name.endswith((".iss", ".json", ".txt")):
                    continue

                path = os.path.join(base, name)
                text = open(path, encoding="utf-8", errors="replace").read()

                for needle in ("superyk7" + "hero",):  # 拆开写：守卫不举报自己
                    if needle in text:
                        hits.append(f"{os.path.relpath(path, self.out_dir)}: {needle}")

        self.check("生成物里没有历史域名/硬编码地址", not hits, "; ".join(hits))

        return not self.errors


def _is_ascii(raw):
    try:
        raw.decode("ascii")

        return True
    except UnicodeDecodeError:
        return False


def _parse_defines(text):
    """把 `#define X "值"` 读成 dict（`#define X` 这种开关记为 True）。"""
    defines = {}

    for line in text.splitlines():
        match = re.match(r'^#define\s+([A-Za-z0-9_]+)\s*(.*)$', line.strip())

        if not match:
            continue

        name, raw = match.group(1), match.group(2).strip()

        if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
            defines[name] = raw[1:-1].replace('""', '"')
        elif raw == "":
            defines[name] = True
        else:
            defines[name] = raw

    return defines


def _unresolved_template_defines(template_text, defines):
    problems = []
    stack = []

    for number, line in enumerate(template_text.splitlines(), start=1):
        stripped = line.strip()

        if stripped.startswith(("#ifdef", "#ifndef")):
            parts = stripped.split()

            if len(parts) >= 2:
                stack.append(parts[1])

            continue

        if stripped.startswith("#endif"):
            if stack:
                stack.pop()

            continue

        for name in re.findall(r"\{#([A-Za-z0-9_]+)\}", line):
            if name in defines:
                continue

            if stack and any(flag not in defines for flag in stack):
                continue

            problems.append(f"第 {number} 行 {{#{name}}} 没有定义")

    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="校验 CraftLink 安装器输入（branding.json / params.iss / staging）"
    )
    parser.add_argument("--out", default=os.path.join(REPO_ROOT, "installer", "build"),
                        help="Builder 的输出目录（默认 installer/build）")
    parser.add_argument("--iss", default=os.path.join(REPO_ROOT, "installer", "CraftLink.iss"),
                        help="Inno Setup 模板路径")
    parser.add_argument("--require-exe", action="store_true",
                        help="主程序必须已在 staging（编译安装器时的用法）")
    parser.add_argument("--json", action="store_true", help="输出机器可读结果")
    parser.add_argument("--quiet", action="store_true", help="只输出错误")
    args = parser.parse_args(argv)

    if not os.path.isdir(args.out):
        message = (
            f"输出目录不存在：{args.out}\n"
            "先跑 Builder：installer/builder/craftlink_builder.py（或 CraftLink-Builder.exe）"
        )

        if args.json:
            print(json.dumps({"ok": False, "errors": [message], "warnings": [], "checks": []},
                             ensure_ascii=False, indent=2))
        else:
            print(f"✗ {message}", file=sys.stderr)

        return 2

    verifier = Verifier(args.out, args.iss, args.require_exe)
    ok = verifier.run()

    if args.json:
        print(json.dumps(
            {
                "ok": ok,
                "out_dir": verifier.out_dir,
                "errors": verifier.errors,
                "warnings": verifier.warnings,
                "checks": verifier.checks,
            },
            ensure_ascii=False, indent=2,
        ))

        return 0 if ok else 1

    if not args.quiet:
        print(f"校验安装器输入：{verifier.out_dir}")

    for item in verifier.checks:
        if item["ok"] and not args.quiet:
            suffix = f"  {item['detail']}" if item["detail"].startswith("[提醒]") else ""
            print(f"  ✓ {item['name']}{suffix}")

    for item in verifier.checks:
        if not item["ok"]:
            print(f"  ✗ {item['name']}  <- {item['detail']}", file=sys.stderr)

    passed = sum(1 for item in verifier.checks if item["ok"])

    if ok:
        print(f"\n✓ {passed}/{len(verifier.checks)} 通过（内容断言；Inno Setup 编译需在 Windows 上跑）")
    else:
        print(f"\n✗ {passed}/{len(verifier.checks)} 通过，{len(verifier.errors)} 个问题", file=sys.stderr)

    if verifier.warnings and not args.quiet:
        print("\n提醒：")

        for warning in verifier.warnings:
            print(f"  ! {warning}")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
