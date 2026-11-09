# `installer/` —— Windows 用户安装器（M7）

这个目录是 **M7** 的全部实现：开发者用 Builder 生成"面向用户的安装器"，
用户在 Windows 上双击 `CraftLink-Setup.exe` 完成安装。

```
installer/
├── CraftLink.iss              Inno Setup 脚本模板（纯 ASCII；一切品牌值来自 build/params.iss）
├── builder/                   CraftLink-Builder（GUI + CLI，Python 标准库，离线）
│   ├── craftlink_builder.py   入口：不带参数双击 = GUI；--non-interactive = CLI
│   ├── builder_core.py        校验 + 生成 branding.json / params.iss / 报告 / 下一步
│   ├── builder_gui.py         tkinter 界面
│   ├── imageconv.py           PNG -> .ico（零依赖，供安装器图标用）
│   └── CraftLink-Builder.spec PyInstaller 打包（产出 CraftLink-Builder.exe）
├── build/                     ★ 生成物（不进版本库）：params.iss / staging/ / out/CraftLink-Setup.exe
├── examples/                  输入示例（--non-interactive --json）
└── tests/builder_selftest.py  离线自检（135 条；Linux/Windows 都能跑）
```

一眼流程（**在 Windows 上**）：

```bat
:: 1) 客户端主程序（这一步不变，就是原来的 PyInstaller 流程）
cd client
pyinstaller ..\scripts\mclink-client.spec --noconfirm --clean

:: 2) 生成"面向用户的安装器"的输入（GUI 或 CLI 二选一）
..\installer\builder\dist\CraftLink-Builder.exe
:: 或
..\installer\builder\dist\CraftLink-Builder.exe --non-interactive --json ..\installer\examples\builder-input.example.json

:: 3) 编译安装器（一条命令；自动找 ISCC.exe、校验、编译）
cd ..
bash scripts\build-installer.sh
:: 产物：installer\build\out\CraftLink-Setup.exe
```

完整说明（含用户安装/升级/卸载、静默参数、FAQ、必须在 Windows 上验证的清单）
见 **[docs/INSTALLER.md](../docs/INSTALLER.md)**。

离线自检（不需要 Windows、不需要 Inno Setup）：

```bash
python3 installer/tests/builder_selftest.py
python3 scripts/verify-installer-inputs.py --no-require-exe   # 校验已生成的安装器输入
```
