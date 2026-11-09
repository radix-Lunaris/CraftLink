# Windows 安装器 + 开发者构建器（M7）

> **口径来源**：`docs/OPENSOURCE-PLAN.md`（D3 Inno Setup / D8 不写死路径 / D10 中继地址默认空 /
> D11 登录记忆默认空 / 里程碑 M7）与 `docs/CLIENT-BRANDING.md`（§7 = 安装器接口）。
> **实现**：`installer/**`、`scripts/build-installer.sh`、`scripts/build-installer.cmd`、
> `scripts/build-builder.sh`、`scripts/verify-installer-inputs.py`。
> **没碰**：`panel/**`、`server/**`、`tests/**`、`install.sh`、`client/**`（只读）。

一句话：**开发者用 `CraftLink-Builder` 把"品牌 + 默认中继地址 + 目录默认值"固化下来，
生成安装器的输入；用户在 Windows 上双击 `CraftLink-Setup.exe` 装完即用。**
客户端里**没有**任何内置服务器地址（D10）——地址要么由 Builder 固化，要么由用户首次运行自己填。

---

## 1. 全景图

```
开发者在 Windows 上                           用户那边
────────────────────────────────────────      ─────────────────────────────
1) pyinstaller scripts/mclink-client.spec
   → client\dist\CraftLink.exe
                     │
2) CraftLink-Builder（GUI 或 --non-interactive）
   输入：软件名 / 中继域名或地址+端口 / logo /
         可选默认安装目录 / 可选默认数据目录
   → installer\build\
        ├── staging\CraftLink.exe             （主程序，从 dist 复制过来）
        ├── staging\CraftLink-console.exe     （诊断版，可选）
        ├── staging\branding.json             （品牌文件；全默认值时不生成）
        ├── staging\branding-logo.<ext>       （品牌 logo，可选）
        ├── setup-icon.ico                    （安装器/快捷方式图标）
        ├── params.iss                        （Inno Setup 编译参数，UTF-8 带 BOM）
        ├── builder-report.json               （机器可读清单 + sha256）
        └── NEXT-STEPS.txt                    （下一步怎么编译）
                     │
3) scripts\build-installer.sh（或 ISCC.exe installer\CraftLink.iss）
   → installer\build\out\CraftLink-Setup.exe ──────────► 双击安装
                                                         · 可改安装位置
                                                         · 可改数据目录
                                                         · 开始菜单/桌面快捷方式
                                                         · 静默安装
                                                         · 升级保留数据
                                                         · 卸载不删数据
```

| 谁 | 文件 | 作用 |
|---|---|---|
| 开发者 | `installer/builder/CraftLink-Builder.exe` | 交互式（GUI）或 `--non-interactive`（CI）收集输入并生成上表 |
| 开发者 | `scripts/build-installer.sh` | 一条命令：校验输入 → 找 `ISCC.exe` → 检查版本 → 编译 |
| 用户 | `CraftLink-Setup.exe` | 双击安装；卸载走"应用和功能" |

`installer/build/` 是**生成物目录**（`installer/.gitignore` 已忽略），不会进版本库。

---

## 2. 开发者流程（Windows）

### 2.0 环境准备

| 需要 | 版本 | 装法 |
|---|---|---|
| Python | 3.8+（与客户端打包同一个） | python.org，勾 *Add to PATH* |
| PyInstaller | 与客户端打包同一个 | `python -m pip install --upgrade pyinstaller` |
| **Inno Setup** | **6.3 或更新** | `winget install --id JRSoftware.InnoSetup -e`，或 <https://jrsoftware.org/isdl.php> |
| Git Bash（可选） | 任意 | 有它才能跑 `scripts/build-installer.sh`；没有就用 `scripts\build-installer.cmd` |

> **为什么必须 6.3+**：安装器要把"用户选的数据目录"写成客户端认识的指针文件
> `%APPDATA%\CraftLink\data_dir.txt`，且**必须是 UTF-8 无 BOM**（带 BOM 会让客户端把
> `\ufeffD:\...` 当成路径而失败）。模板用的是 6.3 起提供的
> `SaveStringsToUTF8FileWithoutBOM()`。老版本编译会报 `Unknown identifier`。

### 2.1 构建客户端 exe（这一步没变）

```bat
cd client
pyinstaller ..\scripts\mclink-client.spec --noconfirm --clean
cd ..
```

产物：`client\dist\CraftLink.exe` + `client\dist\CraftLink-console.exe`。
（**本轮的 M7 完全没有改 `scripts/mclink-client.spec`**，所以这条流程与以前一字不差。）

### 2.2 跑 Builder

**方式 A：图形界面**（推荐给人工发布）

```bat
scripts\build-builder.sh --check     :: 先确认 Python/PyInstaller/tkinter/自检
scripts\build-builder.sh             :: 打出 installer\builder\dist\CraftLink-Builder.exe
installer\builder\dist\CraftLink-Builder.exe
```

界面分四个页签：

| 页签 | 字段 | 说明 |
|---|---|---|
| 品牌 | 软件名 / logo / 安装器图标 / 版本号 / 发布者 / 更新地址 | 软件名进窗口标题、开始菜单、卸载信息；logo 见 §3.3 |
| 默认中继服务器 | 域名或 IP / 端口 | **留空 = 没有内置服务器**（D10，客户端会引导用户填）；填了就是新装用户的预填值与"恢复默认"目标 |
| 目录 | 默认安装目录 / 默认用户数据目录 / 输出目录 | 都是**默认值**，用户在安装向导里随时能改 |
| 高级 | 主程序 exe / 诊断版 exe / 安装包文件名 / 许可证 / 三个开关 | 一般不用动 |

点「生成安装器输入」后，右侧文本区直接给出**下一步怎么编译 `CraftLink-Setup.exe`**
（同一份内容也写进 `NEXT-STEPS.txt`，点「复制『下一步』」可整段拷走）。

**方式 B：命令行 / CI**

```bat
:: 看有哪些参数
installer\builder\dist\CraftLink-Builder.exe --help

:: 用 JSON（推荐，字段见 installer\examples\builder-input.example.json）
installer\builder\dist\CraftLink-Builder.exe --non-interactive --json installer\examples\builder-input.example.json

:: 直接给参数（不带 --json 也行）
installer\builder\dist\CraftLink-Builder.exe --non-interactive ^
    --app-name "方块特快" ^
    --server-host relay.example.com --server-port 19100 ^
    --logo client\mclink\assets\app.png ^
    --default-install-dir "D:\Apps\CraftLink" ^
    --default-data-dir "D:\CraftLinkData" ^
    --out installer\build

:: 只校验，不写文件（CI 里当门禁用）
... --check

:: 打印 JSON 模板 / 离线自检 / 版本
... --print-json-template
... --selftest
... --version
```

`--non-interactive` 的输入字段（也接受 JSON 里的同名键，kebab-case 或 snake_case 都行）：

| 参数 | 默认 | 校验规则 |
|---|---|---|
| `--app-name` | `CraftLink`（读客户端 `DEFAULT_APP_NAME`） | 空则用默认值；目录非法字符会被清掉并提醒 |
| `--server-host` | **空** | 域名/IPv4/`[IPv6]`；**不接受** `http://`；填了它就必须填端口 |
| `--server-port` | `0`（= 未设置） | 1–65535；与 host 必须同时有或同时无 |
| `--update-url` | 空 | 必须 `http(s)://` 开头 |
| `--logo` | 空 | 必须是客户端 `branding.py` 白名单里的后缀：`.png/.gif/.ppm/.pgm`（关于页图片）或 `.ico`（窗口图标）；文件必须存在 |
| `--icon` | 空 | 必须是 `.ico`（安装器/快捷方式图标）；不给就用 logo 生成或内置图标 |
| `--default-install-dir` | 空（= `{autopf}\<软件名>`） | 绝对路径；不能与数据目录互相嵌套 |
| `--default-data-dir` | 空（= `%APPDATA%\CraftLink`） | 同上；不能落在安装目录里 |
| `--exe` / `--console-exe` | `client/dist/CraftLink.exe` / 自动探测 | 不存在只提醒；编译脚本会拦 |
| `--app-version` | 读 `client/mclink/config/settings.py` 的 `APP_VERSION` | 非数字开头会提醒 |
| `--app-publisher` / `--publisher-url` | `CraftLink` / 空 | 进卸载信息与注册表留档 |
| `--app-id` | 仓库固定 GUID | 必须是 GUID；换品牌独立发布时换掉（决定"升级替换"还是"并存安装"） |
| `--output-basename` | `CraftLink-Setup` | 只允许字母数字与 `._-` |
| `--license-file` | 仓库根 `LICENSE`（存在才用） | 存在则安装向导显示许可页 |
| `--language-file` / `--language-name` | 空（英文界面） | 必须是本机 Inno Setup 里真实存在的 `.isl`（见 FAQ §7.4） |
| `--min-windows` | `10.0` | 装到更低版本系统会被拦住 |
| `--out` | `installer/build` | 非空且不像 Builder 目录时会拒绝（加 `--force` 可强写） |
| `--data-dir-page` / `--desktop-icon-task` / `--env-var-task` | 全开 | `--no-xxx` 可关（关掉就没有对应的向导页/选项） |
| `--texts` | 内置中文 | JSON 对象或 `.json` 路径，覆盖安装器界面文本 |

### 2.3 编译安装器（一条命令）

```bat
:: 有 Git Bash（推荐）
bash scripts/build-installer.sh
bash scripts/build-installer.sh --dry-run          :: 只检查 + 打印命令
bash scripts/build-installer.sh --iscc "D:\Tools\Inno Setup 6\ISCC.exe"
bash scripts/build-installer.sh --out D:\build\cl --name CraftLink-Setup
bash scripts/build-installer.sh --skip-verify      :: 不推荐

:: 没有 Git Bash
scripts\build-installer.cmd
```

脚本按顺序做六件事（任一步失败就停下并说清楚原因）：

1. 找 `python`（校验脚本要用）；
2. 检查 `installer\build\params.iss` 在不在——不在就告诉你去跑 Builder；
3. 跑 `scripts/verify-installer-inputs.py --require-exe`（内容断言，见 §6.1）；
4. 找 `ISCC.exe`：`--iscc` > `$ISCC` > `PATH` > `%ProgramFiles(x86)%\Inno Setup 6` >
   `%LocalAppData%\Programs\Inno Setup 6` > WSL 的 `/mnt/c/...`；找不到就给出 winget/官网装法；
5. 检查 Inno Setup **≥ 6.3**（读版本；读不到就提醒），并检查 `--language-file` 指向的 `.isl`
   在编译器的 `Languages\` 下真实存在；
6. 调 `ISCC.exe` 编译，最后报产物路径 + 大小 + sha256 + 静默安装示例。

产物：**`installer\build\out\CraftLink-Setup.exe`**。

### 2.4 手工编译（不想用脚本时）

```bat
cd installer
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" CraftLink.iss
```

只要 `installer\build\params.iss` 存在，模板就能独立编译（它固定 `#include "build\params.iss"`）。

---

## 3. Builder 的输入 / 输出契约

### 3.1 输出清单

| 文件 | 谁用 | 说明 |
|---|---|---|
| `staging\CraftLink.exe` | 安装器 → `{app}` | 从 `--exe` 复制；名字取源文件名（可用 `--exe-name` 改） |
| `staging\CraftLink-console.exe` | 安装器 → `{app}` | 诊断版（`--selfcheck`）；不存在就不装 |
| `staging\branding.json` | 客户端 | **UTF-8 无 BOM**，字段恰好 5 个；全默认值时不生成 |
| `staging\branding-logo.<ext>` | 客户端 | 统一改名，方便升级时用 `branding-logo.*` 清理旧图 |
| `setup-icon.ico` | 安装器（**不进 `{app}`**） | 来自 `--icon`，或由 PNG logo 生成，或用客户端内置 `app.ico` |
| `params.iss` | `CraftLink.iss` | 全部 `#define`（UTF-8 **带 BOM**，中文品牌名靠它） |
| `builder-report.json` | 人/CI/校验器 | 输入快照、客户端常量、每个文件的 sha256、开关、提醒 |
| `NEXT-STEPS.txt` | 人 | 下一步命令（与 GUI/CLI 打印的一致） |

### 3.2 `branding.json` 字段与回退

生成的字段与回退规则严格按 `docs/CLIENT-BRANDING.md` §2.2/§2.3：

```json
{
  "app_name": "方块特快",
  "logo_file": "branding-logo.png",
  "default_server_host": "relay.example.com",
  "default_server_port": 19100,
  "update_url": "https://relay.example.com/download"
}
```

* 文件**不存在** = 客户端全用默认值（`CraftLink` + 内置图标 + **空地址**）。所以
  "没有品牌需求"时 Builder 干脆不生成这个文件（`CLIENT-BRANDING.md` §7 第 1 条）；
* `logo_file` 用**相对文件名**（相对 `branding.json` 所在目录 = 安装目录）；
* `default_server_host` / `default_server_port` 留空就是留空——**绝不**由安装器补一个"猜的"地址；
* 端口只在 1–65535 之间才写，非法值直接报错而不是静默写 0。

### 3.3 logo 的取舍（客户端接口限制，见 §8）

客户端只有一个 `logo_file` 字段：`.png/.gif/.ppm/.pgm` → **关于页图片**；`.ico` → **窗口图标**。
两者只能选一个。Builder 的行为：

* 你给 `.png` → `logo_file` 指向它（关于页有品牌图），同时**自动生成** `setup-icon.ico`
  （安装器图标、快捷方式图标用），窗口图标仍是内置的；
* 你给 `.ico` → 窗口图标有品牌，关于页用内置图；安装器图标也用它；
* 你什么也不给 → 用客户端内置图标（`client/mclink/assets/app.ico`），并提醒你。

生成 `.ico` 用的是自带的纯标准库实现（`installer/builder/imageconv.py`：PNG 解码 + 面积平均缩放 +
32 位 BGRA 位图条目），**不引入 PIL**，离线可用；尺寸 16/32/48/256，刻意不写 PNG 压缩条目
（老版本 Inno/Windows 对 PNG 条目的支持不一致）。

---

## 4. 用户视角：安装 / 升级 / 卸载

### 4.1 双击安装（向导步骤）

| 步骤 | 内容 | 备注 |
|---|---|---|
| 1 | 安装模式 | 默认"为所有用户安装"（需要管理员，装到系统程序目录）；选"仅为我"则不需要管理员，装到用户程序目录 |
| 2 | 许可协议 | 只在构建时存在 `LICENSE` 时出现 |
| 3 | **选择安装位置** | 默认 `系统程序目录\CraftLink`，**可改**（也支持 UNC 路径）|
| 4 | **用户数据目录** | 默认 `%APPDATA%\CraftLink`；可改；**不能选在安装目录里面**（会被当场拦下）|
| 5 | 附加任务 | 「创建桌面快捷方式」（默认不勾）、「同时设置 `MCLINK_DATA_DIR` 环境变量」（默认不勾，见 §4.3）|
| 6 | 安装 | 复制 exe/branding/logo → 创建开始菜单快捷方式 → 写卸载信息与支持记录 |
| 7 | 完成 | 可勾选"立即运行"（静默安装时不弹）|

### 4.2 目录、快捷方式、注册表

| 位置 | 内容 |
|---|---|
| `{app}`（安装目录） | `CraftLink.exe`、`CraftLink-console.exe`、`branding.json`、`branding-logo.*`、`unins000.exe` |
| 开始菜单 | `{开始菜单}\CraftLink`（名字 = 软件名） |
| 桌面 | 勾了任务才有 |
| `HKCU\Software\<发布者>\<软件名>` | `InstallDir` / `Version` / **`DataDir`**（支持用的留档；`DataDir` 卸载后**保留**） |
| `HKCU\Environment` | 只有勾了"设置环境变量"任务才写 `MCLINK_DATA_DIR`（卸载时删掉） |
| `…\Uninstall\{AppId}_is1` | Inno Setup 自动写的卸载信息（"应用和功能"里能看到） |

安装目录里**不会**出现日志/配置/缓存——客户端本来就不往那里写（`userdirs.py`），安装器也不放。

### 4.3 用户数据目录：和客户端**同一套**机制

安装器不发明第二套机制，只做两件事（`CLIENT-BRANDING.md` §7 第 3 条）：

1. **预建目录**：把你选的目录（以及 `logs/`、`cache/`）建出来；
2. **写指针文件**：如果你选的不是客户端的默认目录，就把绝对路径写进
   `<客户端默认目录>\data_dir.txt`（即 `%APPDATA%\CraftLink\data_dir.txt`）——
   **UTF-8 无 BOM、单行**，与客户端 `userdirs._write_pointer()` 的格式一致。
   选的就是默认目录时不写（客户端自己会用默认目录）。

客户端的解析优先级是 `MCLINK_DATA_DIR` 环境变量 > 指针文件 > 平台默认目录。
所以：

* 默认（不勾环境变量任务）：安装时选的数据目录会被客户端**读指针文件**认到，用户在客户端界面里
  「更改数据目录」也能正常工作（它改的就是这个指针文件）；
* 勾了"同时设置 `MCLINK_DATA_DIR`"：环境变量优先级更高，会**压过**客户端界面里的更改
  （客户端会在数据目录卡片上标注"由环境变量 MCLINK_DATA_DIR 指定"）。这是给批量部署准备的
  选项，默认不勾。

### 4.4 升级安装（覆盖安装同一个 `AppId`）

| 保留/变化的 | 行为 |
|---|---|
| 用户数据（日志、配置、记住的账号） | **完全不动**（它在数据目录里，不在 `{app}` 里） |
| 安装位置 | 向导默认带出上次的目录（`UsePreviousAppDir=yes`），可改 |
| 附加任务 | 带出上次的勾选（`UsePreviousTasks=yes`） |
| 程序文件 | 覆盖（`ignoreversion`） |
| `branding.json` / logo | **按新构建替换**（升级前先删旧的 `branding.json` 与 `branding-logo.*`，所以"新版故意不带品牌"时不会残留旧品牌） |
| 正在运行的客户端 | Restart Manager 会提示关闭（`CloseApplications=yes`，不自动重启） |
| 数据目录指针 | 若这次选的仍不是默认目录，指针被重写为新值；选默认目录时不动旧指针 ⚠️ 见 FAQ §7.5 |

### 4.5 卸载

* 走"设置 → 应用 → 已安装的应用"，或 `{app}\unins000.exe`；
* **默认不删用户数据**：卸载结束前弹一句"用户数据（日志、配置、记住的账号）已保留，位置：…"，
  并在注册表 `HKCU\Software\<发布者>\<软件名>\DataDir` 里留下记录（"留档"，便于支持与用户自查）；
* 环境变量任务写下的 `MCLINK_DATA_DIR` 会随卸载删除；
* 想彻底清掉数据（用户自己决定）：

```bat
:: 先看安装器当时记在哪（也可以直接看卸载时的提示）
reg query "HKCU\Software\CraftLink" /v DataDir

:: 默认位置 + 自定义位置 + 指针文件，一起清
rmdir /s /q "%APPDATA%\CraftLink"
rmdir /s /q "D:\CraftLinkData"
```

* 静默卸载：`"%ProgramFiles%\CraftLink\unins000.exe" /VERYSILENT /NORESTART`

### 4.6 静默安装 / 无人值守

Inno Setup 自带参数全部可用，外加一个自定义的 `/DATA_DIR`：

| 参数 | 作用 |
|---|---|
| `/SILENT` | 显示进度但不问问题 |
| `/VERYSILENT` | 完全不显示界面 |
| `/DIR="D:\Apps\CraftLink"` | **安装目录** |
| `/DATA_DIR="D:\CraftLinkData"` | **用户数据目录**（自定义参数，见 §4.3） |
| `/TASKS="desktopicon,setenvvar"` | 勾选附加任务；`/MERGETASKS` 可叠加默认任务 |
| `/LOG="%TEMP%\cl-install.log"` | 写安装日志（排查必备） |
| `/NORESTART` | 绝不重启 |
| `/SUPPRESSMSGBOXES` | 抑制消息框（配合 `/SILENT`） |
| `/LANG=<name>` | 多语言时选语言（默认只装一种语言，用不上） |

```bat
:: 典型批量部署
CraftLink-Setup.exe /VERYSILENT /NORESTART /SUPPRESSMSGBOXES ^
   /DIR="D:\Apps\CraftLink" /DATA_DIR="D:\CraftLinkData" ^
   /TASKS="desktopicon" /LOG="%TEMP%\craftlink-install.log"

:: 静默卸载（保留数据）
"D:\Apps\CraftLink\unins000.exe" /VERYSILENT /NORESTART
```

---

## 5. 与客户端的对接点（全部由 `client/**` 派生，不手抄）

安装器**只读**客户端，绝不改它。下面这些名字都是构建时从客户端源码读出来、再写进
`params.iss` 的（读不到就直接报错，不允许"抄一份"）：

| 安装器里的 `#define` | 来源 | 值 |
|---|---|---|
| `ClientAppDirName` | `client/mclink/config/userdirs.py::APP_DIR_NAME` | `CraftLink` |
| `PointerFileName` | `userdirs.py::POINTER_FILENAME` | `data_dir.txt` |
| `EnvVarName` | `userdirs.py::ENV_DATA_DIR` | `MCLINK_DATA_DIR` |
| `LogsSubdirName` / `CacheSubdirName` | `userdirs.py::LOGS_SUBDIR` / `CACHE_SUBDIR` | `logs` / `cache` |
| `BrandingFileName` | `client/mclink/config/branding.py::BRANDING_FILENAME` | `branding.json` |
| `ClientDefaultDataDir` | 由 `APP_DIR_NAME` 拼出 | `{userappdata}\CraftLink` |
| `AppVersion`（默认） | `client/mclink/config/settings.py::APP_VERSION` | `3.9.20` |
| logo 后缀白名单 | `branding.py::_IMAGE_SUFFIXES` / `_ICON_SUFFIXES` | `.png .gif .ppm .pgm` / `.ico` |
| 安装器图标兜底 | `client/mclink/assets/app.ico` | 内置图标 |

约定（`CLIENT-BRANDING.md` §7）：

1. `branding.json` 放在**安装目录**、和 `CraftLink.exe` 同一层，UTF-8 无 BOM；
2. `logo_file` 指向的图片也复制到安装目录；
3. 数据目录由安装器询问，写 `data_dir.txt`（或环境变量）——**安装目录里不放运行期文件**。

客户端不做的事（安装器也别指望）：不写注册表、不写安装目录、不假设安装路径。

---

## 6. 验证

### 6.1 Linux / 沙箱里**已经**能验证的（本轮都跑过）

```bash
# (a) Builder 单测 / 自检：135 项（客户端常量、校验、branding.json、params.iss、
#     PNG->ICO、PE 架构、CLI、GUI 真开窗口点一遍、静态扫描）
python3 installer/tests/builder_selftest.py
python3 installer/builder/craftlink_builder.py --selftest

# (b) 生成物内容断言：68 项（模板片段/转义/BOM/字段/与客户端常量逐字一致/sha256/图标结构）
python3 scripts/verify-installer-inputs.py                 # 针对 installer/build
python3 scripts/verify-installer-inputs.py --require-exe   # 编译前（编译脚本会跑）
python3 scripts/verify-installer-inputs.py --json          # CI

# (c) 端到端（生成 → 校验 → 调用编译器：用假 ISCC 验证调用链与产物探测）
python3 installer/builder/craftlink_builder.py --non-interactive --out installer/build \
        --logo client/mclink/assets/app.png
bash scripts/build-installer.sh --dry-run
```

**没有被 Linux 覆盖的**：`.iss` 的真实编译、安装/升级/卸载行为、快捷方式、注册表、
静默参数、UAC。这些必须在 Windows 上过 §6.2。

### 6.2 必须在 **Windows** 上验证（逐条命令）

> 前提：先做完 §2.1–§2.3，产物在 `installer\build\out\CraftLink-Setup.exe`。
> 把下面的 `%REPO%` 换成仓库路径。

```bat
:: ── 0) 环境自证 ─────────────────────────────────────────────
python --version
pyinstaller --version
"%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" >nul || echo 需要安装 Inno Setup 6.3+

:: ── 1) 离线自检（Linux 上也跑过，这里再跑一遍确认 Windows 路径/PE/tkinter 都对）──
python installer\tests\builder_selftest.py
installer\builder\dist\CraftLink-Builder.exe --selftest

:: ── 2) 生成 + 校验 + 编译 ────────────────────────────────────
installer\builder\dist\CraftLink-Builder.exe --non-interactive --json installer\examples\builder-input.example.json
python scripts\verify-installer-inputs.py --require-exe
bash scripts\build-installer.sh
:: 期望：出现 "[OK] 产物：...CraftLink-Setup.exe"，且 ISCC 退出码 0
dir installer\build\out

:: ── 3) 图形安装（人工过一遍）────────────────────────────────
installer\build\out\CraftLink-Setup.exe
::   期望顺序：安装模式 → (许可页) → 选择安装位置 → 用户数据目录 → 附加任务 → 安装 → 完成
::   在"用户数据目录"页故意填 %ProgramFiles%\CraftLink\data  → 必须被拒绝并提示
::   改成 D:\CraftLinkData → 继续

:: 3.1 安装目录内容
dir "%ProgramFiles%\CraftLink"
::   期望：CraftLink.exe / CraftLink-console.exe / branding.json / branding-logo.* / unins000.exe
type "%ProgramFiles%\CraftLink\branding.json"
::   期望：app_name = 你填的软件名；default_server_host/port = 你填的值（没填就是 ""/0）

:: 3.2 数据目录（★ 关键：无 BOM）
dir "D:\CraftLinkData"                        :: 期望 logs\ cache\
type "%APPDATA%\CraftLink\data_dir.txt"       :: 期望一行：D:\CraftLinkData
powershell -NoProfile -Command "Format-Hex -Path \"$env:APPDATA\CraftLink\data_dir.txt\" | Select-Object -First 2"
::   ★ 期望头两个字节是 44 3A（"D:"），**不是** EF BB BF
::     这一步就是在验证 SaveStringsToUTF8FileWithoutBOM 的行为

:: 3.3 快捷方式 + 卸载信息
dir "%ProgramData%\Microsoft\Windows\Start Menu\Programs" | findstr /i craftlink
reg query "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall" /s /f "CraftLink" | findstr /i "DisplayName DisplayVersion"
::   期望：开始菜单快捷方式存在；（勾了任务时）桌面也有

:: 3.4 启动客户端
"%ProgramFiles%\CraftLink\CraftLink.exe"
::   期望：窗口标题/页眉 = 软件名；「数据目录」卡片显示 D:\CraftLinkData；
::         地址框预填你固化的中继地址（或空 + 首次运行引导）；日志写到 D:\CraftLinkData\logs\
"%ProgramFiles%\CraftLink\CraftLink-console.exe" --selfcheck
"%ProgramFiles%\CraftLink\CraftLink-console.exe" --probe

:: ── 4) 升级安装：数据必须原地保留 ───────────────────────────
:: 先在客户端里改点设置并关掉（保证 D:\CraftLinkData 里有内容），然后：
installer\build\out\CraftLink-Setup.exe
::   期望：安装位置页默认带出上次的目录；装完 D:\CraftLinkData 里的
::         mclink_config.json / logs\ / credentials.dat* 一个都没变；branding.json 被覆盖为新值
dir "D:\CraftLinkData" /o-d | more

:: 4.1 品牌变更清理（换一个 app_name 重新跑 Builder + 编译后升级）
::   期望：{app} 里旧的 branding-logo.* 被清掉，只剩新 logo

:: ── 5) 卸载：默认不删用户数据 ───────────────────────────────
"%ProgramFiles%\CraftLink\unins000.exe"
::   期望：弹窗写明"用户数据已保留，位置：D:\CraftLinkData"
dir "%ProgramFiles%\CraftLink"                 :: 期望：不存在（除了你手动放进去的文件）
dir "D:\CraftLinkData"                         :: 期望：还在，内容完整
reg query "HKCU\Software\CraftLink" /v DataDir :: 期望：留档仍在

:: ── 6) 静默安装 / 静默卸载 ──────────────────────────────────
installer\build\out\CraftLink-Setup.exe /VERYSILENT /NORESTART /SUPPRESSMSGBOXES ^
    /DIR="D:\Apps\CraftLink" /DATA_DIR="D:\CraftLinkData2" /TASKS="desktopicon" ^
    /LOG="%TEMP%\cl-install.log"
echo 退出码=%errorlevel%                        :: 期望 0
findstr /i "error" "%TEMP%\cl-install.log"      :: 期望：没有致命错误
dir "D:\Apps\CraftLink"                         :: 期望：文件齐全
type "%APPDATA%\CraftLink\data_dir.txt"         :: 期望：D:\CraftLinkData2
dir "%UserProfile%\Desktop" | findstr /i craftlink   :: 期望：桌面快捷方式（/TASKS=desktopicon）
"D:\Apps\CraftLink\unins000.exe" /VERYSILENT /NORESTART
::   期望：静默卸载干净（用户数据仍保留）

:: 6.1 环境变量任务（可选）
installer\build\out\CraftLink-Setup.exe /VERYSILENT /TASKS="setenvvar" /DATA_DIR="D:\CraftLinkData3"
::   打开一个**新** cmd（环境变量需要新进程）：
cmd /c "echo %MCLINK_DATA_DIR%"                 :: 期望：D:\CraftLinkData3
::   启动客户端 →「数据目录」卡片应显示"由环境变量 MCLINK_DATA_DIR 指定"

:: ── 7) 中文 / 空格路径 ──────────────────────────────────────
installer\build\out\CraftLink-Setup.exe /VERYSILENT /NORESTART ^
    /DIR="C:\Program Files\方块 特快" /DATA_DIR="D:\用户 数据\方块特快"
::   期望：装得上；品牌显示正确；data_dir.txt 里中文路径正确（再用 Format-Hex 确认无 BOM）；
::         中文目录里客户端能正常起、日志能写

:: ── 8) 杀软 / 签名相关的现实检查 ────────────────────────────
powershell -NoProfile -Command "Start-MpScan -ScanType CustomScan -ScanPath '%REPO%\installer\build\out'"
::   期望：Defender 不报（PyInstaller onefile 偶发误报，见 FAQ §7.1）
```

### 6.3 发版前验收清单

- [ ] `python3 installer/tests/builder_selftest.py` → `135/135 通过`
- [ ] `python3 scripts/verify-installer-inputs.py --require-exe` → 全过
- [ ] `bash scripts/build-installer.sh` → ISCC 退出码 0，产物存在
- [ ] §6.2 的 3（图形安装）在**干净**用户/目录上跑通
- [ ] §6.2 的 4（升级）验证过数据保留 + 品牌替换
- [ ] §6.2 的 5（卸载）验证过数据保留与提示
- [ ] §6.2 的 6（静默）+ 7（中文空格路径）各跑一次
- [ ] `branding.json` 里没有你不想要的服务器地址（`type installer\build\staging\branding.json`）
- [ ] 记录 `CraftLink-Setup.exe` 的 sha256（编译脚本会打印）

---

## 7. 常见问题（FAQ）

### 7.1 杀软误报 / SmartScreen 拦截

* **成因**：PyInstaller onefile 自解压 + 安装器没有代码签名 + 新文件没有信誉，三者叠加。
* **能做的**（按性价比排序）：
  1. 给 `CraftLink-Setup.exe` 与 `CraftLink.exe` **买证书签名**（`ISCC.exe /Ssigntool=...` 可挂签名工具，
     需要 `signtool.exe` 与证书）；
  2. 提交误报申诉：<https://www.microsoft.com/wdsi/filesubmission>（Defender），
     其它厂商各有入口；
  3. 保持"文件名/发布者/版本号"稳定（每次发布都改 GUID/发布者会重新积累信誉）；
  4. 先用 `scripts\verify-installer-inputs.py` 自证产物没被动过手脚（有 sha256）。
* 本仓库**不做**签名（没有证书），这一点写在"遗留项"里。

### 7.2 权限 / UAC

* 默认"为所有用户安装"→ 需要管理员（装到系统程序目录）；不想被 UAC 打扰就在第一个对话框选
  "仅为我安装"（装到用户程序目录，不需要管理员）。
* 静默安装要提权时，从**已提权**的 cmd/PowerShell 里跑，或由部署系统给管理员上下文。
* 数据目录写在 `%APPDATA%`（每用户），所以"管理员装的、普通用户用"也各自有独立数据。

### 7.3 路径含中文 / 空格 / 很长

* 安装器与客户端都按 Unicode 处理：安装目录、数据目录、软件名都可以是中文与空格（§6.2 的第 7 条专门验它）。
* 注意两点：
  * 安装器写指针文件用 `SaveStringsToUTF8FileWithoutBOM`，**必须**是 6.3+，否则中文路径会坏；
  * 极长路径（>260）没有专门支持：把安装/数据目录放在较浅的位置（如 `D:\CraftLink`）。

### 7.4 Inno Setup 版本 / 中文安装界面

* 模板需要 **6.3+**（`SaveStringsToUTF8FileWithoutBOM`）；`scripts/build-installer.sh` 会先检查版本，
  太老会直接告诉你怎么升（`winget upgrade --id JRSoftware.InnoSetup -e`）。
* Inno Setup **官方不带简体中文**（`Languages\` 里只有二十来种官方语言）。所以：
  * 默认：安装界面是**英文**（但软件名、快捷方式名、我们的提示语都是中文——它们来自 `params.iss`）；
  * 想要全中文界面：把 `ChineseSimplified.isl` 放进
    `C:\Program Files (x86)\Inno Setup 6\Languages\`，然后重跑 Builder：
    ```
    ... --language-file "compiler:Languages\ChineseSimplified.isl" --language-name chinesesimplified
    ```
    来源：Inno Setup 官方"非官方翻译"页面（<https://jrsoftware.org/files/istrans/>）里的 Simplified Chinese。
    编译脚本会先检查这个文件是否存在，缺了就直接报错并给出上面的说明。

### 7.5 数据目录"没生效"

按可能性排序自查：

1. **是不是勾了环境变量任务**？`MCLINK_DATA_DIR` 优先级最高，会压过客户端界面里的更改。
   `echo %MCLINK_DATA_DIR%` 看有没有值；不要就删掉（`setx MCLINK_DATA_DIR ""` 或系统属性里删）。
2. **指针文件字节对不对**：`Format-Hex %APPDATA%\CraftLink\data_dir.txt`，开头不能是 `EF BB BF`；
   内容应是**一行绝对路径**。
3. **目录可写吗**：U 盘拔了/只读会退回默认目录，客户端会弹一次提示（不静默失败）。
4. **升级时换过默认数据目录**：如果这次你选的正好是客户端默认目录，安装器**不写**指针文件，
   而旧指针还指着老位置——此时请在客户端界面里点「恢复默认」（或手动删掉 `data_dir.txt`）。

### 7.6 升级后品牌没变

* 正常情况下 `[InstallDelete]` 会先删旧的 `branding.json` / `branding-logo.*` 再放新的。
* 如果你手改过 `{app}\branding.json`，升级会被覆盖（那是安装器的文件）；想自己定制请用
  `MCLINK_BRANDING_FILE` 环境变量指向别处（客户端支持，安装器不碰）。

### 7.7 编译报错对照表

| ISCC 报错 | 原因 / 处理 |
|---|---|
| `Could not open file build\params.iss` | 还没跑 Builder，或 `--params` 指到了别处。跑 Builder（默认写 `installer/build/params.iss`） |
| `Unknown identifier 'SAVESTRINGSTOUTF8FILEWITHOUTBOM'` | Inno Setup < 6.3，升级 |
| `Could not read messages file ...ChineseSimplified.isl` | 语言文件不在编译器的 `Languages\` 下（FAQ §7.4） |
| `Source file "...\staging\CraftLink.exe" does not exist` | 主程序没构建（`pyinstaller scripts/mclink-client.spec`），或 staging 被清过——重跑 Builder |
| `Parameter "AppId" ... invalid` | `--app-id` 不是 GUID |
| 编译通过但产物找不到 | `params.iss` 里 `OutputDirLine` 是相对路径（相对 `installer/`）；用 `dir installer\build\out` 看 |

### 7.8 "能不能不装 Inno Setup，直接给个 zip？"

可以，但那是另一条路（绿色版）：把 `client\dist\*.exe` + 一份 `branding.json` 打成一个 zip 发给用户，
用户自己解压——**这时没有开始菜单/卸载信息/数据目录询问**，数据目录用客户端的默认值或让用户在界面里改。
`branding.json` 可以直接用 Builder 生成的那份（`installer\build\staging\`）。详见
`docs/CLIENT-BRANDING.md`（改品牌不用重新打包）。

---

## 8. 遗留项 / 已知限制

| # | 事项 | 说明 / 影响 |
|---|---|---|
| 1 | **客户端只有一个 `logo_file`** | 窗口图标（`.ico`）与关于页图片（`.png`）不能同时来自品牌。安装器只能二选一（默认：给了 `.png` 就用于关于页，安装器图标另生成）。要两全其美需要客户端加一个字段（**未改客户端**） |
| 2 | `userdirs._read_pointer()` 不剥 `\ufeff` | 万一有人把 `data_dir.txt` 存成带 BOM 的 UTF-8，客户端会把 BOM 当成路径首字符 → 目录不可用 → 退回默认（有提示，不丢数据）。建议客户端加一行 `value.lstrip("\ufeff")` 兜底（**未改客户端**） |
| 3 | 环境变量任务与客户端界面「更改数据目录」冲突 | 勾了 `setenvvar` 后，客户端界面里的更改会被环境变量压过（界面有标注）。属于客户端 UX 细节（**未改客户端**） |
| 4 | 没有代码签名 | 见 FAQ §7.1；`signtool` 挂载点已在本文档说明 |
| 5 | 不附带中文 `.isl` | 默认英文安装界面；中文界面按 FAQ §7.4 自备文件（避免把第三方翻译文件进仓库） |
| 6 | 换品牌后旧快捷方式仍在 | 开始菜单/桌面快捷方式用软件名命名；改名后旧名字的快捷方式不会被认出来（Inno 不知道它）。换品牌建议先卸载旧版 |
| 7 | 升级时"选回默认数据目录"不会清旧指针 | 见 FAQ §7.5 第 4 条（安装器不去删用户可能还有用的指针） |
| 8 | `installer/builder/dist/*.exe` 不进版本库 | Builder 是源码交付（`scripts/build-builder.sh` 一条命令打出来），避免把 13MB+ 二进制塞进仓库 |
| 9 | `.iss` 语法与安装行为无法在 Linux 验证 | 已在 §6.2 写成逐条命令的 Windows 验收清单；Linux 侧用"模板片段断言 + 生成物内容断言"兜底 |
