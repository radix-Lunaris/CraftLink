"""
CraftLink-Builder -- 图形界面（tkinter，标准库，离线可用）。

界面就是一条流水线：

    填表（软件名 / 中继地址+端口 / logo / 默认目录…）
        → 生成（branding.json + logo + Inno 参数）
        → 文本区给出"下一步怎么编译出 CraftLink-Setup.exe"（同内容写进
          NEXT-STEPS.txt）

刻意不做的事：

  * 不内置任何服务器地址（留空 = 客户端首次运行时引导用户填，D10）；
  * 不写死安装路径（默认值只是默认值，安装器里用户随时能改）；
  * 不碰注册表、不碰用户数据目录（Builder 只写自己那个输出目录）。

GUI 的所有控件操作都收敛到 `collect_inputs()` / `generate()` 两个方法，
所以 installer/tests/builder_selftest.py 能在无人工点击的情况下把它跑一遍。
"""

import os
import subprocess
import sys
import tkinter as tk

import builder_core

from tkinter import filedialog, messagebox, ttk

#: 仓库根：installer/builder/builder_gui.py -> 上两级。
DEFAULT_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _RealDialogs:
    """真实弹窗（默认）。自检时换成记录器，免得模态框把自动化测试卡住。"""

    def error(self, title, message):
        messagebox.showerror(title, message)

    def info(self, title, message):
        messagebox.showinfo(title, message)

    def warning(self, title, message):
        messagebox.showwarning(title, message)


class BuilderApp:
    """主窗口。"""

    def __init__(self, repo_root=None, out_dir=None, inputs=None, root=None, dialogs=None):
        self.repo_root = os.path.abspath(
            repo_root
            or builder_core.find_repo_root(
                start=os.path.dirname(os.path.abspath(__file__))
            )
            or DEFAULT_REPO_ROOT
        )
        self.facts = builder_core.read_client_facts(self.repo_root)
        self.inputs = inputs or builder_core.Inputs()
        self.last_report = None
        self.dialogs = dialogs or _RealDialogs()

        self.root = root or tk.Tk()
        self.root.title(f"{builder_core.BUILDER_NAME} {builder_core.BUILDER_VERSION} —— CraftLink 安装器构建器")
        self.root.minsize(880, 720)

        self.vars = {}
        self._build_widgets()
        self._prefill()

        # 显式给的目标目录优先于表单默认值。
        if out_dir:
            self.vars["out_dir"].set(out_dir)

        self.log("准备就绪。填好上面的信息后点「生成安装器输入」。")
        self.log(f"仓库根目录：{self.repo_root}")

        if self.facts.missing():
            self.log(
                "!! 读不到客户端常量：" + ", ".join(self.facts.missing())
                + "（请确认 client/mclink/config/userdirs.py 在仓库里）",
                kind="error",
            )
        else:
            self.log(
                f"客户端常量来源：{self.facts.source}；版本 {self.facts.app_version or '未知'}；"
                f"数据目录名 {self.facts.app_dir_name}；环境变量 {self.facts.env_data_dir}"
            )

    # ------------------------------------------------------------------ UI

    def _entry_row(self, parent, row, label, key, browse=None, width=46, hint=""):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=3)

        variable = tk.StringVar()
        self.vars[key] = variable
        entry = ttk.Entry(parent, textvariable=variable, width=width)
        entry.grid(row=row, column=1, sticky="we", pady=3)

        if browse:
            def on_browse():
                if browse == "dir":
                    chosen = filedialog.askdirectory(title=label, mustexist=False)
                else:
                    chosen = filedialog.askopenfilename(
                        title=label,
                        filetypes=browse if isinstance(browse, list) else [],
                    )

                if chosen:
                    variable.set(os.path.normpath(chosen))

            ttk.Button(parent, text="浏览…", width=8, command=on_browse).grid(
                row=row, column=2, padx=(6, 0), pady=3
            )
        elif hint:
            ttk.Label(parent, text=hint, foreground="#666666").grid(
                row=row, column=2, sticky="w", padx=(6, 0)
            )

        return entry

    def _build_widgets(self):
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)

        ttk.Label(
            outer,
            text=(
                "三步：① 填下面的信息 → ② 点「生成安装器输入」 → ③ 按文本区里的命令"
                " 在 Windows 上编译出 CraftLink-Setup.exe"
            ),
            font=("", 10, "bold"),
        ).pack(anchor="w", pady=(0, 8))

        notebook = ttk.Notebook(outer)
        notebook.pack(fill="x")

        brand = ttk.Frame(notebook, padding=10)
        server = ttk.Frame(notebook, padding=10)
        dirs = ttk.Frame(notebook, padding=10)
        advanced = ttk.Frame(notebook, padding=10)
        notebook.add(brand, text="品牌")
        notebook.add(server, text="默认中继服务器")
        notebook.add(dirs, text="目录")
        notebook.add(advanced, text="高级")

        brand.columnconfigure(1, weight=1)
        self._entry_row(brand, 0, "软件名", "app_name")
        self._entry_row(
            brand, 1, "logo 文件（可选）", "logo", browse=[
                ("图片", "*.png *.gif *.ppm *.pgm *.ico"), ("所有文件", "*.*")
            ]
        )
        ttk.Label(
            brand,
            text=(
                "logo 留给客户端當关于页图片/窗口图标：.png/.gif 是「关于页图片」，"
                ".ico 是「窗口图标」（客户端只有一个 logo_file 字段，二选一）。"
                "给 .png 时 Builder 会自动再生成一个安装器用的 .ico。"
            ),
            foreground="#666666", wraplength=760, justify="left",
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self._entry_row(brand, 3, "安装器图标 .ico（可选）", "icon", browse=[
            ("图标", "*.ico"), ("所有文件", "*.*")
        ])
        self._entry_row(brand, 4, "版本号", "app_version", hint="留空 = 读客户端")
        self._entry_row(brand, 5, "发布者", "app_publisher")
        self._entry_row(brand, 6, "更新地址（可选）", "update_url",
                        hint="版本不一致时提示用户去哪里下载")

        server.columnconfigure(1, weight=1)
        self._entry_row(server, 0, "中继域名或 IP", "server_host")
        self._entry_row(server, 1, "中继端口", "server_port")
        ttk.Label(
            server,
            text=(
                "★ 留空就是「没有内置服务器」：客户端首次运行会引导用户自己填（D10）。"
                "填了就是新装用户的预填值和「恢复默认」的目标 —— 地址必须是中继服务器的"
                "域名/IP + 端口，不要带 http://。"
            ),
            foreground="#666666", wraplength=760, justify="left",
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))

        dirs.columnconfigure(1, weight=1)
        self._entry_row(dirs, 0, "默认安装目录（可选）", "default_install_dir", browse="dir")
        self._entry_row(dirs, 1, "默认用户数据目录（可选）", "default_data_dir", browse="dir")
        ttk.Label(
            dirs,
            text=(
                "都是**默认值**，用户安装时随时能改；留空则安装目录用系统程序目录下的"
                "软件名、数据目录用 %APPDATA%\\CraftLink。数据目录绝不能放在安装目录里面"
                "（升级会整目录替换）。"
            ),
            foreground="#666666", wraplength=760, justify="left",
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self._entry_row(dirs, 3, "输出目录", "out_dir")

        advanced.columnconfigure(1, weight=1)
        self._entry_row(advanced, 4, "主程序 exe", "exe", browse=[("可执行文件", "*.exe"), ("所有文件", "*.*")])
        self._entry_row(advanced, 5, "诊断版 exe（可选）", "console_exe", browse=[("可执行文件", "*.exe"), ("所有文件", "*.*")])
        self._entry_row(advanced, 6, "安装包文件名", "output_basename")
        self._entry_row(advanced, 7, "许可证文件（可选）", "license_file", browse=[("所有文件", "*.*")])

        self.bool_vars = {}
        for index, (key, text) in enumerate(
            (
                ("data_dir_page", "安装时询问用户数据目录（推荐）"),
                ("desktop_icon_task", "提供「创建桌面快捷方式」选项"),
                ("env_var_task", "提供「同时设置 MCLINK_DATA_DIR 环境变量」选项（高级）"),
            )
        ):
            variable = tk.BooleanVar(value=True)
            self.bool_vars[key] = variable
            ttk.Checkbutton(advanced, text=text, variable=variable).grid(
                row=8 + index, column=1, sticky="w", pady=2
            )

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=(10, 6))
        ttk.Button(buttons, text="生成安装器输入", command=self.generate).pack(side="left")
        ttk.Button(buttons, text="打开输出目录", command=self.open_out_dir).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="复制「下一步」", command=self.copy_next_steps).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="退出", command=self.root.destroy).pack(side="right")

        self.log_widget = tk.Text(outer, height=22, wrap="none", font=("Consolas", 9))
        self.log_widget.pack(fill="both", expand=True)
        scroll = ttk.Scrollbar(self.log_widget, command=self.log_widget.yview)
        scroll.pack(side="right", fill="y")
        self.log_widget.configure(yscrollcommand=scroll.set)

    def _prefill(self):
        facts = self.facts
        self.vars["app_name"].set(self.inputs.app_name or facts.default_app_name or "CraftLink")
        self.vars["app_version"].set(self.inputs.app_version or facts.app_version or "")
        self.vars["app_publisher"].set(self.inputs.app_publisher or "CraftLink")
        self.vars["output_basename"].set(self.inputs.output_basename or "CraftLink-Setup")
        self.vars["exe"].set(self.inputs.exe or os.path.join(self.repo_root, "client", "dist", "CraftLink.exe"))
        console = self.inputs.console_exe or os.path.join(
            self.repo_root, "client", "dist", "CraftLink-console.exe"
        )
        self.vars["console_exe"].set(console if os.path.isfile(console) else "")
        self.vars["out_dir"].set(
            self.inputs.out_dir or os.path.join(self.repo_root, "installer", "build")
        )
        license_path = self.inputs.license_file or os.path.join(self.repo_root, "LICENSE")
        self.vars["license_file"].set(license_path if os.path.isfile(license_path) else "")
        self.vars["server_port"].set(str(self.inputs.server_port or ""))

        for key in ("logo", "icon", "default_install_dir", "default_data_dir", "update_url"):
            self.vars[key].set(getattr(self.inputs, key, "") or "")

    # -------------------------------------------------------------- helpers

    def log(self, message, kind="info"):
        prefix = {"error": "✗ ", "warn": "! ", "ok": "✓ "}.get(kind, "· ")
        self.log_widget.insert("end", prefix + str(message) + "\n")
        self.log_widget.see("end")

        try:
            self.root.update_idletasks()
        except tk.TclError:
            pass

    def open_out_dir(self):
        path = self.vars["out_dir"].get().strip() or os.path.join(self.repo_root, "installer", "build")

        if not os.path.isdir(path):
            self.dialogs.info("还没生成", f"输出目录还不存在：\n{path}")
            return

        try:
            if sys.platform.startswith("win"):
                os.startfile(path)          # noqa: S606 - 用户主动点的按钮
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as exc:  # noqa: BLE001
            self.dialogs.warning("打不开", f"{path}\n{exc}")

    def copy_next_steps(self):
        if not self.last_report or not self.last_report.get("next_steps"):
            self.dialogs.info("还没生成", "先点「生成安装器输入」。")
            return

        self.root.clipboard_clear()
        self.root.clipboard_append(self.last_report["next_steps"])
        self.log("「下一步」已经复制到剪贴板。")

    def collect_inputs(self):
        """把表单读成 `builder_core.Inputs`（这就是 GUI 的唯一出口）。"""
        data = {key: variable.get() for key, variable in self.vars.items()}
        data.update({key: variable.get() for key, variable in self.bool_vars.items()})

        if not str(data.get("server_port") or "").strip():
            data["server_port"] = 0

        return builder_core.inputs_from_mapping(data)

    # ------------------------------------------------------------- 生成

    def generate(self):
        try:
            inputs = self.collect_inputs()
        except ValueError as exc:
            self.dialogs.error("输入有问题", str(exc))
            return None

        self.log("=" * 60)
        self.log(f"开始生成（输出目录：{inputs.out_dir or '(默认)'}）")

        try:
            report = builder_core.build(
                inputs, self.repo_root, out_dir=inputs.out_dir or None, force=True
            )
        except Exception as exc:  # noqa: BLE001
            import traceback

            self.log(traceback.format_exc(), kind="error")
            self.dialogs.error("生成失败", f"{exc.__class__.__name__}: {exc}")
            return None

        if not report.get("ok"):
            for message in report.get("errors", []):
                self.log(message, kind="error")

            self.dialogs.error("生成失败", "\n\n".join(report.get("errors", [])))
            return None

        self.last_report = report

        for warning in report.get("warnings", []):
            self.log(warning, kind="warn")

        for label, path in (report.get("outputs") or {}).items():
            if path:
                self.log(f"{label}：{path}", kind="ok")

        self.log("")
        self.log_widget.insert("end", report["next_steps"])
        self.log_widget.see("end")

        return report


def run(repo_root=None, out_dir=None):
    """启动 GUI（供入口脚本调用）。"""
    app = BuilderApp(repo_root=repo_root, out_dir=out_dir)
    app.root.mainloop()

    return 0
