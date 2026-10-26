"""Existing GUI actions and state synchronization, preserved from v5.0.1.

This private UI controller is not a network API. Views implement the widget
builders; networking remains exclusively in MCLinkEngine and the existing core.
"""
from .presentation import *
from .presentation import _member_status_text, _filter_grade, _flow_format
from .widgets import Tooltip, TextHandler, RoundTile

class ClientController:
    def __init__(self, root):
        self.root = root
        self.engine = MCLinkEngine()

        self.config = cfgmod.load_config()

        #: 「基准配置」那三个数字有没有从磁盘装载过。
        #:
        #: 它区分两件完全不同的事（第一版混在一起，切一次语言就把三个数字清空）：
        #:   False -> 程序刚起来：按 config.first_run 决定"预填上一次的值"还是"留空"
        #:   True  -> 只是重建界面（切语言/主题）：一切以内存里的 _committed 为准
        self._config_loaded_from_disk = False

        self.log_queue = queue.Queue()
        self.running = False
        self.last_snapshot = {}
        self._last_ui_refresh = 0.0
        self._last_info_players = None
        self._last_rates = {}
        self._last_emit_errors = []

        #: 「房间被关闭」的弹窗只弹一次（ROOM REVOKED 和 room_closed 会先后到）。
        #: 每次成功进入房间时复位。
        self._room_close_notified = False

        # 版本门：同一次运行里只弹一次「需要更新」，避免重连刷屏。
        self._version_popup_shown = False

        #: 身份同步（IDENTITY，ISSUES 问题 5）：最后一次**被中继确认**的身份、
        #: 最后一次**尝试过**的身份、以及尝试的时刻（5 秒冷却用）。
        self._identity_sent = None
        self._identity_attempted = None
        self._identity_sent_at = 0.0

        #: 游客身份（ISSUES 问题 5/6，用户 2026-09-27 口径）。
        #:
        #: **登录会话级**：中继第一次给我们的「游客xxxxxx」记在这里，
        #: 退房 / 被踢 / 房间被关**都不销毁**（下次建房或加房还用这个名字）；
        #: 只有登出 / 换账号 / 重启软件才清掉。
        #: **绝不写进配置文件**（一次性的临时身份），所以它只是一份内存状态。
        self._guest_name = ""
        self._guest_token = ""

        #: File logging lives in `mclink.log` now (one process-wide logger), so
        #: there is no per-window logger object to hold here any more.
        self._config_file_path = None

        # V2 login session (AuthSession) or None while logged out.
        self.auth = None

        #: 界面正在重建（切换语言/主题）时，定时刷新先让路，避免碰到
        #: 已经销毁的控件。
        self._rebuilding = False

        #: V3 直连状态：最近一次本机 NAT 画像（由直连协调器填），供运行状态
        #: 卡片显示。诊断不再是独立栏目，所以它只是显示用的一份缓存。
        self._nat_profile = None

        #: 3.0.9 房间成员列表：窗口、按成员复用的行控件、正在重入的守卫。
        #: 行控件按成员 key 复用（只改文字），所以实时刷新不会闪、不会抢焦点。
        self.member_window = None
        self.member_table = None
        self.member_hint = None
        self.member_rows = {}
        #: 每个成员当前显示的名字（踢人确认用它，而不是 client_id）。
        self.member_names_by_key = {}
        self._member_list_filling = False

        #: 3.0.9「一人一行」的直连行（运行状态卡片里）：peer -> (frame, label)。
        self.hole_rows_frame = None
        self._hole_rows = {}

        #: 会话模式同步（非房主的热切换自动重建）的防抖状态。
        #: 见 `_sync_session_mode` / `_session_mode_rebuild_delta`。
        self._rebuild_in_flight = False
        self._last_session_mode_rebuild_at = 0.0
        self._session_mode_rebuild_target = None

        #: ★ 用户 2026-10-02：**模式热切换的重建是静默的**。
        #:
        #: 重建内部会 `stop_session()`（重建的必要一步），而"停止"那条路会把
        #: 连接按钮放掉、把状态栏写成"未连接"。可这是**房主换模式**触发的自动
        #: 重建，玩家什么都不该感觉到（用户原话：「连接按钮不应该被释放，且状态
        #: 栏需要正常显示，仅静默自动重连即可」）。这个标志为 True 期间，事件泵
        #: 每轮都把界面按回"在连接"，直到 `rebuild_done`（按新模式正常显示）或
        #: `rebuild_failed`（那时该报错就报错、该放手就放手）。
        self._mode_switch_silent = False
        self._mode_switch_silent_until = 0.0

        #: ★ 2026-10-03（热切换分析 P2）：收到 `CONN_MODE_CHANGED` 那一刻的
        #: **切换前**房间模式。中继先发那一行、后发 `HOST_STATE mode=<新>`，
        #: 所以"房间模式仍是这个旧值"= 新模式还没到 —— 那一刻绝不能拿旧模式
        #: 重建，也绝不能清掉待重建标记（旧代码清了，host 于是永不重建）。
        self._mode_rebuild_from = None

        #: "待重建"标记的期限（秒）。到期无条件清掉并记日志 —— 见 P4 回归的教训。
        self._mode_rebuild_pending_until = 0.0

        #: "下一次真正开始的重建请静默"。模式驱动的调用方只设这一个标记，由
        #: `_rebuild_tunnels` 在**通过了全部提前返回之后**消费它 —— 调用点直接设
        #: `_mode_switch_silent` 就是 2026-10-03 那个致命回归（重建被挡回 ->
        #: 标志永远没人清 -> 界面被硬按在"在连接"，停止/新房间全废）。
        self._mode_rebuild_silent_next = False

        #: 中继推过 `CONN_MODE_CHANGED` 但房间广播里的新模式可能还没到：
        #: 那一天的处理放在 `_on_conn_mode_changed` / `_maybe_rebuild_for_mode_change`。
        self._mode_rebuild_pending = False

        #: 「我点了开始连接」的上报状态（`STARTED 1/0`，PROTOCOL §8.6）：
        #: 同一个会话里同一个状态只发一次（None = 还没发过）。
        self._started_reported = None

        #: ★ v3.9.16：被推迟的 host-gone（模式重建窗口里收到的那一条）。
        #:
        #: 以前窗口期内这类事件被**丢弃**，那就是"host 断开后其余人不断开"的
        #: 直接原因。现在只缓存、不丢弃，由 `_flush_pending_host_gone()` 在
        #: 窗口结束后补跑。见 `HOST_GONE_DEFER_MAX_S`。
        self._pending_host_gone = None


        # ---- 外观：语言 + 主题（在任何控件被创建之前定下来）----
        i18n.set_language(getattr(self.config, "ui_language", "zh"))
        apply_theme(getattr(self.config, "ui_theme", "dark"))

        self._setup_file_logging()

        # Must run before any widget reads username_var / client_id_var: the
        # visible "身份" card that used to create them is gone, so they are
        # plain state now.
        self._init_identity_vars()

        self._build_window()
        self._build_styles()
        self._build_layout()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self._refresh_account_ui()

        # The startup line goes into the FILE through the structured logger, so
        # it is one parseable fact rather than a translated sentence: the log is
        # the audit record and does not follow the UI language.
        logmod.event(
            "client.started",
            version=cfgmod.version_line(),
            config=self._config_file_path or cfgmod.config_path(),
        )
        self.log(
            f"CraftLink started ({cfgmod.version_line()})",
            kind="info",
        )
        self.log(
            f"config file: {self._config_file_path or cfgmod.config_path()}",
            kind="dim",
        )

        self.root.after(TICK_MS, self._tick)


    def on_language_change(self, _event=None):
        chosen = self.language_var.get()

        for code, label in i18n.LANGUAGES:
            if label == chosen:
                self.config.ui_language = code
                break

        self._apply_appearance()


    def on_theme_change(self, _event=None):
        # 下拉框里显示的是【翻译过】的名字（暗色 / Dark / ダーク），
        # 存进配置的必须是 dark / light / system 这三个代码。
        chosen = self.theme_var.get()

        for code, label in theme_mod.THEME_LABELS:
            if chosen == tr(label) or chosen == code:
                self.config.ui_theme = code
                break

        self._apply_appearance()


    FORM_VARS = (
        "mode_var",
        "room_var",
        "host_var",
        "port_var",
        "mc_port_var",
        "local_port_var",
        "max_players_var",
        "username_var",
        "client_id_var",
    )


    def rebuild_ui(self):
        """用当前语言/配色重画整个窗口（控件重建，状态保留）。"""
        self._rebuilding = True

        # 表单里已经填好的东西先记下来，重建之后再放回去。
        form = {}

        for name in self.FORM_VARS:
            var = getattr(self, name, None)

            if var is not None:
                with contextlib.suppress(Exception):
                    form[name] = var.get()

        try:
            log_text = ""

            with contextlib.suppress(Exception):
                # 【原样】取出来（end-1c = 不含 Tk 自己那个收尾换行），重建后
                # 原样放回去。以前是 strip() 之后再补一个 "\n"，每切一次外观
                # 日志就多一个空行 —— 切十次就多十行。
                log_text = self.log_text.get("1.0", "end-1c")

            for child in list(self.root.winfo_children()):
                with contextlib.suppress(Exception):
                    child.destroy()

            # 成员窗口是 root 的子窗口，上面那轮一并销毁了 —— 引用也要清掉，
            # 否则后面的刷新会拿着一个已经死掉的控件去找 winfo_exists()。
            self.member_window = None
            self.member_table = None
            self.member_hint = None
            self.member_rows = {}
            self.member_names_by_key = {}

            # ============================================================================
            #  重建控件前必须清掉"控件登记表"
            # ============================================================================
            #
            # _tile_groups 里存的是瓷砖控件对象。控件被销毁之后，这份名单还留着
            # 死引用，下一次 _refresh_tiles() 就会去配置一个已经不存在的窗口，
            # Tcl 直接报 "bad window path name"。
            #
            # 以前没人发现，是因为那时候用的是 `widget.configure(bg=...)` 并且外面
            # 套了 suppress(TclError) —— 异常被吞掉了。圆角瓷砖换成方法调用之后，
            # 异常就冒出来了（测试立刻变红，这是好事）。
            self._tile_groups = []

            # 窗口本身的底色不在任何卡片里，重建子控件不会碰到它 ——
            # 少了这一行，亮色主题下窗口四周仍是深色。
            with contextlib.suppress(Exception):
                self.root.configure(bg=BG)

            self._build_styles()
            self._build_layout()

            # ---- 还原表单 ----
            for name, value in form.items():
                var = getattr(self, name, None)

                if var is not None:
                    with contextlib.suppress(Exception):
                        var.set(value)

            # 模式提示文字 / 身份对应的端口输入框，跟 run() 里初次构建后
            # 做的事完全一样 —— 不重放这两步，重建后的提示文字和显示哪一组
            # 端口框就会跟启动时不一致。
            with contextlib.suppress(Exception):
                self._on_mode_change()

            with contextlib.suppress(Exception):
                self._on_role_change()

            if log_text.strip() and hasattr(self, "log_text"):
                with contextlib.suppress(Exception):
                    self.log_text.configure(state="normal")
                    self.log_text.delete("1.0", "end")
                    self.log_text.insert("1.0", log_text)
                    self.log_text.see("end")
                    self.log_text.configure(state="disabled")

            # 把动态内容重新填回去（账号行、房间状态、状态卡、顶部提示）。
            self._refresh_account_ui()
            self._refresh_room_ui()
            self._refresh_status()

        finally:
            self._rebuilding = False


    def _setup_file_logging(self):
        """
        Hand file logging to `mclink.log` (the one process-wide logger).

        WHY THIS IS NOW A ONE-LINER: the file handler used to be built here, in
        the GUI, which meant (a) anything logging before the window existed had
        nowhere to go, and (b) this class and the core could disagree about the
        format. See `client/mclink/log.py` for the whole argument.
        """
        try:
            logmod.event(
                "client.log.start",
                file=logmod.log_path(),
                pid=os.getpid(),
                version=cfgmod.version_line(),
            )
        except Exception:
            pass


    def log(self, text, kind="normal"):
        """
        Thread safe: only appends to a queue.

        THE SCREEN AND THE FILE GET THE SAME STRING, BY CONSTRUCTION.

        This used to run `tr(text)` on the way to the screen, while the file got
        the untranslated text -- so a user's screenshot and the log file they
        sent back could say different things, and the one time that matters is
        exactly when something is broken and being investigated.

        The owner's instruction is explicit: the log records the RAW behaviour,
        not a translation of it. The UI keeps its own language for its own
        labels; the log does not follow the UI language at all.
        """
        with contextlib.suppress(queue.Full):
            self.log_queue.put_nowait((time.strftime("%H:%M:%S"), text, kind))

        # `dim` is the self-test / decoration tier: screen only, never the file,
        # so the file stays a record of events rather than of the window dressing.
        if kind != "dim":
            logmod.note(text)


    TWO_COLUMN_MIN_WIDTH = 960


    COLUMN_WEIGHTS = (3, 2)


    COLUMN_GAP = 20


    COLUMN_SLACK = 16


    FORM_WIDGETS = (
        ("room_var", "room_entry"),
        ("host_var", "host_entry"),
        ("port_var", "port_entry"),
        ("mc_port_var", "mc_port_entry"),
        ("local_port_var", "local_port_entry"),
        ("max_players_var", "max_players_entry"),
    )


    def _verify_form_bindings(self):
        """
        检查每个输入框绑的变量就是程序在读的那个变量。返回出问题的字段名列表。

        只在"名字对不上"时说话：控件可以不存在（某些卡片在不同版本里被挪过位置），
        但**只要控件在，它绑的变量就必须和 self.<name> 是同一个**。
        """
        broken = []

        for var_name, widget_name in self.FORM_WIDGETS:
            var = getattr(self, var_name, None)
            widget = getattr(self, widget_name, None)

            if var is None or widget is None:
                continue

            try:
                bound = str(widget.cget("textvariable") or "")
            except Exception:
                continue

            if bound != str(var):
                broken.append(
                    f"{widget_name} 绑的是 {bound or '(无)'}，"
                    f"程序读的是 {var}"
                )

        if broken:
            with contextlib.suppress(Exception):
                self.log(
                    "界面自检失败：输入框和程序读的不是同一个变量 —— "
                    + "；".join(broken),
                    kind="error",
                )

        return broken


    def _theme_label(self):
        """当前主题在界面上的名字（跟随语言翻译）。"""
        current = theme_mod.current()

        for code, label in theme_mod.THEME_LABELS:
            if code == current:
                return tr(label)

        return tr("暗色")


    def _on_tile_click(self, value, var, command):
        if var.get() == value:
            return

        var.set(value)
        self._refresh_tiles()

        if command is not None:
            command()


    def _init_identity_vars(self):
        """
        Hidden identity state.

        There used to be a visible "身份" card holding a username box and a
        read-only client id. It was removed because:

          * the username box did NOT rename the account -- it only changed the
            display name inside a room, which is confusing sitting next to the
            real "修改用户名" action in the account card, and the user judged it
            useless;
          * the client id is machine generated and never needs editing.

        Both values are still read in several places, so they remain as plain
        variables that no widget is bound to.
        """
        self.username_var = tk.StringVar(value=self.config.username or "")
        self.client_id_var = tk.StringVar(value=self.config.client_id)


    def _set_row_status(self, key, template, kind="ok", value=None):
        """
        把某一行的反馈文字**立刻**显示出来。

        反馈文字要走 tr()：三语言是这个软件已经验收的功能，动态生成的提示
        也不能例外（i18n 测试会检查整个界面不残留中文）。
        模板用 %s 占位，翻成什么语言都能正确代入数字。

        用 update_idletasks()：不刷的话，用户点完确定要等下一次事件循环才看到，
        视觉上仍然是"按了没反应"。
        """
        text = tr(template)

        if value is not None:
            with contextlib.suppress(Exception):
                text = text % value

        for variable, _entry, _button, status, _hint in getattr(
            self, "_config_rows", []
        ):
            if self._config_key_of(variable) != key:
                continue

            color = {"ok": OK, "warn": WARN, "error": BAD}.get(kind, FG_MUTED)

            with contextlib.suppress(Exception):
                status.configure(text=text, fg=color)
                status.update_idletasks()

            return


    def _restore_row_hint(self, key):
        """把某一行恢复成它的静态说明文字。"""
        for variable, _entry, _button, status, hint in getattr(
            self, "_config_rows", []
        ):
            if self._config_key_of(variable) != key:
                continue

            with contextlib.suppress(Exception):
                status.configure(text=hint, fg=FG_MUTED)

            return


    LOCKED_AFTER_CREATE = ("mc_port", "max_players")


    def _lock_key(self, key):
        """这个键在建房后是否被锁死？"""
        return key in self.LOCKED_AFTER_CREATE


    def _config_locked(self, key=None):
        """
        现在能不能改这个数字？

        - 房间已建（或已在房间里 / 连接在跑）→ mc_port 和 max_players 锁死。
        - local_port **永不锁定**：它和隧道没有任何关系（隧道数量只由人数推导），
          它只是本机监听端口，房主根本不用它。所以玩家随时可以改 ——
          但改了要重连（那是玩家自己的事，不影响房间，也不影响别人）。
        """
        if key is not None and not self._lock_key(key):
            return False

        with contextlib.suppress(Exception):
            if self.running:
                return True

        with contextlib.suppress(Exception):
            if self.engine.room_state() is not None:
                # 房间被服务端关掉（ROOM REVOKED）之后它已经不是"在建房时锁定
                # 参数"的那个房间了 —— 隧道也没了，参数必须能改，否则用户
                # 面对的是"房间没了、端口也动不了"（ISSUES 问题 4 的另一半）。
                if not getattr(
                    self.engine.room_state(), "revoked", False
                ):
                    return True

        return False


    def _config_lock_reason(self):
        with contextlib.suppress(Exception):
            if self.running:
                return "连接正在运行"

        return "你已经在这个房间里了"


    def _is_config_dirty(self, variable):
        """这个输入框里的字和"已提交"的值不一样吗？"""
        key = self._config_key_of(variable)

        if not key:
            return False

        return self._committed.get(key) != str(variable.get()).strip()


    def _on_config_edited(self):
        """
        输入框被改动 → 按钮变成"待提交"的样子。

        不做任何校验、不报错：打字过程中每敲一个字符都会走到这里，
        在这里弹窗是最烦人的做法。校验只在按「确定」时做。
        """
        self._sync_config_buttons()


    def _config_key_of(self, variable):
        for key, own in (
            ("mc_port", getattr(self, "mc_port_var", None)),
            ("local_port", getattr(self, "local_port_var", None)),
            ("max_players", getattr(self, "max_players_var", None)),
        ):
            if own is variable:
                return key

        return ""


    def _load_config_fields(self):
        """
        把上次用的值填进输入框。

        用户要求：**第一次打开是空的**（玩家自己填一次），之后记忆上一次的。

        "第一次"由 `config.first_run` 决定（没有任何配置文件 = 第一次跑）。
        注意：装载进来的值**同时**算"已提交" —— 上次能用，这次不该再让人
        按一遍确定；否则每次开软件都要重新提交三个数字，那是折磨。
        """
        if not getattr(self, "_config_loaded_from_disk", False):
            # ============================================================================
            #  第一次装载（程序刚起来）：按 first_run 决定要不要预填
            # ============================================================================
            #
            # 用户要求：第一次打开三个数字是**空的**（玩家自己填一次），
            # 之后每次打开记忆上一次提交的值。
            self._config_loaded_from_disk = True

            self._committed = {}

            if getattr(self.config, "first_run", False):
                for key, variable in (
                    ("mc_port", getattr(self, "mc_port_var", None)),
                    ("local_port", getattr(self, "local_port_var", None)),
                    ("max_players", getattr(self, "max_players_var", None)),
                ):
                    if variable is not None:
                        variable.set("")

                self._on_config_edited()
                return

            values = {
                "mc_port": getattr(self.config, "mc_port", None),
                "local_port": getattr(self.config, "local_port", None),
                "max_players": getattr(self.config, "max_players", None)
                or proto.DEFAULT_MAX_PLAYERS,
            }

            for key, variable in (
                ("mc_port", getattr(self, "mc_port_var", None)),
                ("local_port", getattr(self, "local_port_var", None)),
                ("max_players", getattr(self, "max_players_var", None)),
            ):
                if variable is not None and values.get(key) is not None:
                    variable.set(str(values[key]))

            # 装载进来的值就是"已提交"的值：上次能用，这次不用再按一次。
            self._committed = {
                "mc_port": str(values["mc_port"] or "").strip(),
                "local_port": str(values["local_port"] or "").strip(),
                "max_players": str(values["max_players"]).strip(),
            }

            self._on_config_edited()
            return

        # ============================================================================
        #  重建界面（切语言/切主题）：**只按内存里的事实恢复**
        # ============================================================================
        #
        # 这里**不能**再看 config.first_run，也不能从 config 反推"提交过没有" ——
        # 它是一个"这台机器第一次打开"的标记，在同一个 config 对象上永远是 True
        # （它只在 load_config() 里写一次）。第一版就是这样：切一次语言，
        # 三个数字全被清空、_committed 被重置成 {}，界面从「已生效」变成
        # 「未提交」—— 外观测试（切完语言/主题后控件指纹必须一致）直接把它抓红了。
        #
        # 内存里的 _committed 才是真相，重建只是把控件按它重画一遍。
        for key, variable in (
            ("mc_port", getattr(self, "mc_port_var", None)),
            ("local_port", getattr(self, "local_port_var", None)),
            ("max_players", getattr(self, "max_players_var", None)),
        ):
            value = self._committed.get(key, "")

            if variable is not None:
                variable.set(value)

        self._on_config_edited()


    def _commit_port(self, key, variable):
        """
        提交一个端口（按了确定或回车）。

        反馈分三层，用户按下去**立刻**能看到结果：
          1. 按钮旁边那一行文字变成「已生效：25565」（绿色）—— 成功
          2. 失败弹窗说清楚哪里不对，而且**已生效的值保持不变**
          3. 日志面板也留一条（事后可查）
        """
        if self._config_locked(key):
            self._config_locked_notice(key)
            self._mark_row_blocked(key)
            return

        name = (
            "主机 Host 局域网端口"
            if key == "mc_port"
            else "玩家 Player 本地端口"
        )

        raw = str(variable.get()).strip()

        if not raw:
            self._set_row_status(
                key, "✗ 还没填 —— 端口不能为空", kind="error"
            )
            messagebox.showwarning(
                "还没填",
                "这个端口不能为空。请先填一个 1-65535 之间的数字。",
                parent=self.root,
            )
            return

        try:
            value = int(raw)
        except (TypeError, ValueError):
            self._set_row_status(
                key, "✗ 「%s」不是数字", kind="error", value=raw
            )
            messagebox.showwarning(
                "不是数字",
                f"端口必须是数字，你填的是「{raw}」。",
                parent=self.root,
            )
            return

        if not (1 <= value <= 65535):
            self._set_row_status(
                key,
                "✗ %s 超出范围（只能是 1-65535）",
                kind="error",
                value=value,
            )
            messagebox.showwarning(
                "端口超出范围",
                f"端口只能是 1-65535，你填的是 {value}。",
                parent=self.root,
            )
            return

        # ============================================================================
        #  ★ 用户 2026-09-29（第三轮）第 1 条：本地代理端口的**热切换**
        # ============================================================================
        #
        # 规则（用户原话）：
        #   * 本地连接 Player 代理端口是**唯一允许热切换**的参数
        #     （房间人数 / host 代理端口建房即冻结 —— 见 LOCKED_AFTER_CREATE）；
        #   * 玩家**空闲**时改：直接改，什么都不用重建；
        #   * 玩家**连接中**时改：二级弹窗说清楚"换端口会导致游戏掉线、需要
        #     重新进游戏"，确认之后**自动重建这条隧道并自动重连** ——
        #     操作者原来是什么状态，确认后还是什么状态。
        #
        # 旧实现只在日志里说一句"下一次开始连接才会用新端口（要现在生效请先停止
        # 再开始）"：于是界面显示"已生效：新端口"，实际监听还在旧端口上，
        # 必须手动重连才生效 —— 用户看到的"热切换仅状态机显示生效、代理端口还是
        # 原来的"就是这里。
        # 只有**玩家**的会话才真的在监听这个端口（房主用的是 mc_port），
        # 所以只有玩家这一侧需要"确认 + 重建"。房主改了它不动任何东西
        # （重建房主会话会把所有玩家踢下线，那绝对不是用户要的）。
        port_hot_switch = (
            key == "local_port"
            and self.running
            and str(getattr(self.config, "role", "") or "") == "player"
        )

        if port_hot_switch:
            if not messagebox.askokcancel(
                tr("修改本地代理端口"),
                tr(
                    "把本地代理端口改成 {0} 需要重建这一条隧道：\n\n"
                    "· 当前游戏连接会断开，需要在 Minecraft 里重新进入一次；\n"
                    "· 隧道会自动重建、自动重连 —— 你不需要手动点任何东西；\n"
                    "· 改完之后你的连接状态和现在一样。\n\n"
                    "确定要现在修改吗？"
                ).replace("{0}", str(value)),
                parent=self.root,
            ):
                # 取消：界面退回"已生效"的旧值，什么都不动。
                previous = str(self._committed.get("local_port") or "")

                if previous:
                    variable.set(previous)

                self._set_row_status(
                    key, "已取消 —— 仍是 %s", kind="warn", value=previous
                )
                self._sync_config_buttons()
                return

        # 回写规范化后的数字（"025565" → "25565"），避免显示和实际不一致。
        variable.set(str(value))

        self._committed[key] = str(value)

        # 提交之后立刻存起来（下次打开就是"记忆上一次"）。
        with contextlib.suppress(Exception):
            self.config.mc_port = int(self._committed.get("mc_port") or 0)
            self.config.local_port = int(
                self._committed.get("local_port") or 0
            )
            cfgmod.save_config(self.config)

        self._on_config_edited()

        # ---- 成功：状态驱动的刷新，那一行立刻变成「✓ 已生效：<值>」----
        #
        # 反馈文字**不在这里写死**，而是由 _sync_config_buttons() 按当前状态
        # 统一算出来。理由见那个函数的说明：写死的文字在切语言/主题重建界面后
        # 会消失，于是"重建前后界面必须一致"的外观测试就会红 —— 那说明它
        # 本来就不是一个稳定的状态。
        self._sync_config_buttons()

        if port_hot_switch:
            # 已经弹窗确认过：现在真的把它用起来。
            #
            # 监听 socket 是在**会话创建那一刻**绑上去的，光改配置只是"下次用"。
            # 所以这里原地重建这一次会话（对玩家 = 他那一条隧道）：
            # `_rebuild_tunnels()` 会 stop -> start，用新的 local_port 重新监听，
            # 并按**房间当前模式**重新建立隧道、自动重连；`running` 不变，
            # 所以"原来是什么状态，改完还是什么状态"。
            self.log(
                f"{name} set to {value}; rebuilding this player's tunnel now "
                "(the new port takes effect immediately; the game reconnects "
                "automatically)",
                kind="warn",
            )
            self._rebuild_tunnels(mode=self._room_conn_mode() or None)
        elif key == "local_port":
            self.log(
                f"{name} set to {value} (idle: applied immediately, "
                "can be changed again at any time)",
                kind="ok",
            )
        else:
            self.log(
                f"{name} set to {value} (room not created; can be changed again at any time)", kind="ok"
            )


    def _mark_row_blocked(self, key):
        """被锁定/被拒绝时，把那一行的反馈也改掉（不能只弹窗）。"""
        self._set_row_status(
            key,
            "🔒 已锁定 —— 房间已建，要改请先删房重建",
            kind="warn",
        )


    def _commit_max_players(self):
        if self._config_locked("max_players"):
            self._config_locked_notice("max_players")
            self._mark_row_blocked("max_players")
            return

        raw = str(self.max_players_var.get()).strip()

        try:
            value = int(raw)
        except (TypeError, ValueError):
            self._set_row_status(
                "max_players",
                "✗ 「%s」不是数字",
                kind="error",
                value=raw,
            )
            messagebox.showwarning(
                "不是数字",
                f"人数上限必须是数字（{proto.MIN_MAX_PLAYERS}-"
                f"{proto.MAX_MAX_PLAYERS}）。",
                parent=self.root,
            )
            return

        if not (
            proto.MIN_MAX_PLAYERS <= value <= proto.MAX_MAX_PLAYERS
        ):
            self._set_row_status(
                "max_players",
                "✗ 超出范围（只能是 %d-%d 人）"
                % (proto.MIN_MAX_PLAYERS, proto.MAX_MAX_PLAYERS),
                kind="error",
            )
            messagebox.showwarning(
                "人数超范围",
                f"人数上限只能是 {proto.MIN_MAX_PLAYERS}-"
                f"{proto.MAX_MAX_PLAYERS} 人，你填的是 {value}。\n\n"
                "客户端支持的最大值是 "
                f"{proto.MAX_MAX_PLAYERS} 人（单个房间）。",
                parent=self.root,
            )
            return

        self.max_players_var.set(str(value))
        self._committed["max_players"] = str(value)

        with contextlib.suppress(Exception):
            self.config.max_players = value
            cfgmod.save_config(self.config)

        self._on_config_edited()

        # 成功反馈同样由状态驱动（会显示「✓ 已生效：N 人（建房时用）」那类文字
        # 是在 _sync_config_buttons 里统一算的）。
        self._sync_config_buttons()

        self.log(
            f"max players set to {value} (applies when the room is created, locked after that)", kind="ok"
        )


    def _config_locked_notice(self, key="max_players"):
        """
        建房之后想改这两个"房间命根子"数字：明确告诉他要先删房。

        用户定的原则：「房间人数和 host 代理端口，建房后锁定，就是房主本人
        和服务器管理员都不允许修改，这东西一改联机全掉了。」
        """
        what = (
            "主机 Host 的局域网端口"
            if key == "mc_port"
            else "人数上限"
        )

        self.log(
            f"once the room is created, {what} is locked (the tunnels are built "
            "at that exact moment) -- delete the room first, change it, then create it again.",
            kind="error",
        )

        messagebox.showwarning(
            "这个数字已经锁定",
            f"房间一旦建好，就不能再改{what}了。\n\n"
            "原因：隧道是在建房那一刻按这个数字建好的。"
            "再改只会在新参数上又建一批，两批隧道同时活着，"
            "玩家就会连到旧的那一批上（连接丢失）。\n\n"
            "这一条对谁都一样 —— 房主、接管的 host、服务器管理员都不能改。\n\n"
            "要换参数：先「删除我的房间」，改完再重新建房。",
            parent=self.root,
        )


    def _committed_value(self, key, default=0):
        """
        连接时读这个，不直接读输入框（输入框里可能停着改了没提交的值）。

        回退顺序：按过确定的值 → 输入框当前的字 → `default`。

        为什么允许回退到输入框：这个函数也被测试和"程序化设置"的路径用到
        （`mc_port_var.set("25565")` 这种）。不给回退，那些路径就会莫名其妙
        拿到 0；给了回退，用户在主界面上永远拿不到"没提交的值"，因为他必须
        按确定才会走到连接这一步。
        """
        raw = str(self._committed.get(key) or "").strip()

        if not raw:
            variable = getattr(self, f"{key}_var", None)

            if variable is not None:
                with contextlib.suppress(Exception):
                    raw = str(variable.get()).strip()

        if not raw:
            return int(default or 0)

        try:
            return int(raw)
        except (TypeError, ValueError):
            return int(default or 0)


    def _local_conn_state(self):
        """
        **我自己**现在的连接状态（`idle` / `pairing` / `connected`）。

        ============================================================================
          ★ 用户 2026-10-01 第 3 条：成员列表那一列改成"读本地状态 -> 上报"
        ============================================================================

        用户原话：「这里就只是一个显示问题，实际连接都是已经过验证的 OK 的，
        你别给我乱改实际连接的部分，只动状态展示部分」。所以这个函数**只读**
        已经存在的、界面本来就在读的那几个字段（会话状态 / 房间打洞状态），
        然后交给 `RoomSession.report_conn_state()` 上报给中继展示。

        判据（和状态卡同一套，绝不新发明）：
          * 不在房间里 -> `""`（什么都不报）；
          * 会话没跑 -> `idle`；
          * 会话状态不是 ONLINE（正在连接 / 正在自动重连）-> `pairing`；
          * ONLINE 且（非 STUN 模式，或 STUN 下洞已通/正在承载）-> `connected`。
        """
        state = None

        with contextlib.suppress(Exception):
            state = self.engine.room_state()

        if state is None or getattr(state, "revoked", False):
            return ""

        if not getattr(self, "running", False):
            return "idle"

        snapshot = {}

        with contextlib.suppress(Exception):
            snapshot = self.engine.snapshot() or {}

        if str(snapshot.get("state") or "") != "ONLINE":
            return "pairing"

        mode = str(
            snapshot.get("mode") or self._room_conn_mode() or MODE_RELAY
        )

        if mode not in (MODE_STUN, MODE_AUTO):
            return "connected"

        punch = {}

        with contextlib.suppress(Exception):
            punch = self.engine.room_direct_state() or {}

        if (
            punch.get("stun_ready")
            or punch.get("direct_carrying")
            or snapshot.get("direct_carrying")
        ):
            return "connected"

        # STUN 模式还没打通：如实说"配对中"，不假装已连接。
        return "pairing"


    def _report_local_conn_state(self):
        """把本地连接状态上报给中继（**纯显示**；失败什么都不做）。"""
        with contextlib.suppress(Exception):
            session = self.engine.room_current()

            if session is None:
                return

            value = self._local_conn_state()

            if value:
                session.report_conn_state(value)


    def _refresh_countdown_rows(self):
        """
        倒计时期间（每 150ms 一拍）只重画「逐目标行」+「打洞进度」两处文字。

        **纯显示**：不建控件、不抢焦点、不调引擎的任何动作。没有倒计时就是空操作
        （所以正常情况下这一拍什么都不做）。

        用户 2026-09-30（第三轮第 7 条）：起跑前的相对倒计时要走成
        `1.5s -> 1.2s -> 开打`，1 秒一拍的界面画不出这个。
        """
        engine = getattr(self, "engine", None)

        if engine is None:
            return

        try:
            room_punch = engine.room_direct_state() or {}
        except Exception:
            return

        if not room_punch.get("countdown"):
            return

        with contextlib.suppress(Exception):
            # 和 `_refresh_status` 走**同一个**视图构造器：两处读两份对象，
            # 正是"通路说直连尝试中、打洞进度说已直连"那种自相矛盾的来源。
            snapshot = self.engine.snapshot() or {}
            step_text, step_hint = punch_step_text(
                self._direct_status_view(snapshot)
            )
            self.status_vars["punch"].set(tr(step_text))
            self._set_status_color("punch", step_hint)

        self._render_hole_rows(
            room_punch.get("links"), punching=bool(room_punch.get("punching"))
        )


    def _set_status_color(self, key, color):
        """
        Recolour one value on the 运行状态 card.

        Never raises: if the card was not built (or Tk is shutting down) the
        lookup simply does nothing, because a colour is cosmetic and must not
        be able to break a status refresh.
        """
        widget = self.status_labels.get(key)

        if widget is None:
            return

        with contextlib.suppress(Exception):
            widget.configure(fg=color)


    def _set_enabled(self, button, enabled):
        if enabled:
            button.configure(
                state="normal",
                bg=ACCENT if getattr(button, "_primary", False) else CARD_ALT,
            )
        else:
            button.configure(state="disabled", bg=CARD)


    def _room_credentials(self):
        """The connection details every room call needs. May raise ValueError."""
        host = self.host_var.get().strip()

        if not host:
            raise ValueError("请填写中转服务器地址")

        return (
            host,
            self._port_or_default(),
            self.client_id_var.get().strip(),
        )


    def _current_room_name(self):
        """建房/加入的目标：输入框里填的那个【房间名】（中文/空格都行）。"""
        return self.room_var.get().strip()


    def _room_in_use(self):
        """我【现在真正在的】房间的显示名（不在房间里时返回空串）。"""
        session = self.engine.room_current()

        if session is None:
            return ""

        state = getattr(session, "state", None)

        if state is None:
            return ""

        return str(
            getattr(state, "room_name", "") or getattr(state, "room", "") or ""
        ).strip()


    def _active_room_name(self):
        """
        我【现在真正在的】房间名；不在房间里时退回输入框的内容。

        ============================================================================
         为什么必须和输入框分开
        ============================================================================

        The room input is cleared the moment a room is created or joined (that
        was a deliberate fix: a leftover name made people think they were still
        in the old room, and made "create" reuse a stale name).

        But 开始连接 and 删除房间 used to read THAT box. So after joining a room
        the box was empty, and pressing 开始连接 answered 「房间不能为空」 while
        the client was demonstrably inside a room. The session must always be
        built for the room we are actually in -- never for whatever happens to
        be typed in a text field.
        """
        return self._room_in_use() or self.room_var.get().strip()


    def _active_room_token(self):
        """
        我真正在的那个房间的 room token（房间 ID）。

        发给中继的每一行都得用它：房间名可能是中文或带空格，直接塞进
        空格分隔的协议行会被切断。不在房间里时返回空串。
        """
        session = self.engine.room_current()
        state = getattr(session, "state", None) if session is not None else None

        return str(getattr(state, "room", "") or "").strip()


    def _confirm_room_is_free(self, room, host, port, client_id):
        """
        建房前检查这个房间名有没有人在用。

        Returns True when it is safe to go ahead. When somebody is already in
        that room the user is asked what they meant, because both readings are
        plausible:

          * "我要加入他" -> switch to 加入房间
          * "我名字打错了" -> 改个名字再建

        Doing nothing here was the bug: 创建 on an existing name either collided
        with the live room or (worse, before this) took it over.
        """
        try:
            # ============================================================================
            #  这个查询跑在界面线程上，所以给它一个短上限
            # ============================================================================
            #
            # fetch_rooms() 是同步的（submit → 等网络答案），默认 20 秒。网络
            # 一弱，界面就真的会卡住好几秒 —— 不只是"慢一下"，是整窗不响应。
            #
            # 建房流程本身不改（它要的就是"现在能不能建"这一个同步答案），只把
            # 等待上限压到几秒：查不到就当"不清楚"，让中继去当裁判（重复建房它
            # 会回 ERROR ROOM_EXISTS），这条兜底本来就是下面 except 的语义。
            rooms = self.engine.fetch_rooms(host, port, client_id, "", timeout=4)
        except Exception:
            # Cannot check (relay hiccup): let the relay be the judge -- it
            # refuses a duplicate create anyway.
            return True

        # 比对【房间名】：中继列表里的 room 是房间 ID，name 才是人看到的那个。
        existing = next(
            (
                r
                for r in rooms
                if r.display == room or r.room == room
            ),
            None,
        )

        if existing is None:
            return True

        who = existing.owner or existing.host or "别人"

        if messagebox.askyesno(
            "这个房间名已经有人用了",
            f"房间「{existing.display}」已经由「{who}」建好了，"
            f"现在里面 {existing.members} 人。\n\n"
            "点「是」= 直接加入他的房间（不再新建）\n"
            "点「否」= 取消，我换一个房间名",
            parent=self.root,
        ):
            self.log(f'room "{existing.display}" already exists; joining instead', kind="warn")
            self.on_join_room(existing.display)
            return False

        self.log(f'room name "{room}" is already taken; choose another name', kind="warn")

        return False


    def _validate_room_name(self, room):
        """
        房间名：随便取（中文 / 英文 / 空格 / emoji 都行），只要非空、不太长。

        ============================================================================
         为什么以前会"输入了自定义名称却提示房间号不合法"
        ============================================================================

        The old rule rejected spaces and "/" — those are protocol and URL
        concerns, and they were being pushed onto the person typing. They are
        not the user's problem any more: the name is percent-encoded into an
        ASCII room ID before it goes anywhere near the wire or the panel's
        admin API (see protocol.room_token / server/room_names.py).

        So the only remaining rules are the ones a human would recognise:
        写得下、不是空的、没有换行/制表符这类会把协议行切断的控制字符。
        """
        problem = proto.room_name_problem(room)

        if problem:
            return False, problem

        return True, ""


    def _configured_max_players(self):
        """
        「基准配置」里那个已提交的人数上限，夹到本客户端接受的范围。

        Used as the room's capacity -- not the server's number.

        读的是**按过确定**的值（_committed）；输入框里可能停着改了没提交的
        数字，那种值绝不能拿去建房（隧道是按它建的，建完就锁定了）。
        """
        value = self._committed_value(
            "max_players",
            getattr(self.config, "max_players", None)
            or proto.DEFAULT_MAX_PLAYERS,
        )

        return max(proto.MIN_MAX_PLAYERS, min(value, proto.MAX_MAX_PLAYERS))


    def _room_capacity(self):
        """
        这个房间的上限（中继给的权威数字），没有就返回 0。

        为什么必须有这个：房间人数在建房时就定下并锁死了（见 server 侧
        `set_room_capacity`），而**开几条隧道**必须等于这个数字。
        以前隧道数量只看本机那个输入框 —— 于是「房主建房时填 8、另一个人
        当 host 时本机填 3」，房间就只开 3 条隧道，第 4 个人永远配不上对，
        在 Minecraft 里就是"连不上"（用户报的正是这个）。
        """
        state = self.engine.room_state()

        if state is None:
            return 0

        try:
            return max(0, int(getattr(state, "max_players", 0) or 0))
        except (TypeError, ValueError):
            return 0


    def _capacity_within_server(self):
        """
        False (and a message) when our setting is above the server's ceiling.

        WHY THIS IS A LOCAL CHECK

        The server's number is a ceiling, and its room capacity code simply
        cannot give a room more slots than that. Sending 8 to a server limited
        to 6 used to produce the worst possible outcome: the room was created,
        the client opened 8 tunnels, and the connection hung until it timed out
        -- with nothing anywhere saying why.

        Refusing here, with both numbers named, turns that into one sentence.
        """
        server_cap = 0

        if self.auth is not None:
            server_cap = int(getattr(self.auth, "server_max_players", 0) or 0)

        if not server_cap:
            # Older relay: it did not tell us. Its own ceiling still applies,
            # but we cannot compare against a number we do not have.
            return True

        mine = self._configured_max_players()

        if mine <= server_cap:
            return True

        message = (
            f"你设置的人数上限是 {mine} 人，但中转服务器的上限是 {server_cap} 人。\n\n"
            f"房间人数不能超过服务器上限 —— 要么把这里改成 {server_cap} 或更小，"
            "要么请管理员在管理面板里把上限调高。"
        )

        self.log(
            f"max players {mine} exceeds the server limit {server_cap}; "
            "room creation blocked",
            kind="error",
        )

        messagebox.showwarning("人数超过服务器上限", message, parent=self.root)

        return False


    def on_create_room(self):
        """Create a room and become its OWNER."""
        self._open_room(create=True)


    def on_join_room(self, room=None):
        """
        加入一个房间。

        `room` 给了就用它（房间列表里选的那一行），否则用输入框里的房间名。
        这样"在列表里选一个 → 点加入"一定能进得去，不会反过来要求用户再
        手工把名字敲进输入框。
        """
        if room:
            self.room_var.set(room)

        if not self._current_room_name() and self.room_rows:
            # 输入框空着、但列表里选中了一行：就用那一行。
            selected = self._selected_room_row()

            if selected is not None:
                self.room_var.set(selected.display)

        self._open_room(create=False)


    def _open_room(self, create):
        room = self._current_room_name()

        ok, why = self._validate_room_name(room)

        if not ok:
            messagebox.showerror("房间名不合法", why, parent=self.root)
            return

        if not self.require_login():
            return

        try:
            host, port, client_id = self._room_credentials()
        except ValueError as e:
            messagebox.showerror(
                tr("参数错误"), tr(str(e)), parent=self.root
            )
            return

        if self.engine.room_current() is not None:
            messagebox.showinfo(
                "提示",
                "你已经在一个房间里了，请先点「退出房间」。",
                parent=self.root,
            )
            return

        action = "创建" if create else "加入"
        self.log(f'{action} room "{room}" ...', kind="info")

        # ---- 建房前先看一眼这个名字有没有人在用 ----
        #
        # 中继会拒绝重复建房（ERROR ROOM_EXISTS），但让用户在点下去之前就知道
        # 更好：这里查到有人在用，就直接问他是要加入还是换个名字。
        if create and not self._confirm_room_is_free(room, host, port, client_id):
            return

        # ---- 人数：客户端设置是实际值，服务器设置只是上限 ----
        #
        # 服务器上限 6 而本机设了 8 时，以前会"房间建得出来、连上去卡死超时"：
        # 中继按 6 个位置算，客户端却开了 8 条隧道。现在在建房之前就拦下来，
        # 并说清楚是谁限制了谁。
        if create and not self._capacity_within_server():
            return

        try:
            state = self.engine.open_room(
                host,
                port,
                client_id,
                self.auth.username if self.auth else "",
                self.auth.token if self.auth else "",
                room,
                create=create,
                # 只有建房的人把自己的容量带上去；加入的人沿用房间已有的。
                max_players=self._configured_max_players() if create else 0,
                # 建房时把当前选的连接模式带上去（房主之后仍可改）。
                conn_mode=self.mode_var.get() if create else "",
            )
        except RuntimeError as e:
            self.log(f"{action} room failed: {e}", kind="error")

            if self._refuse_outdated(str(e)):
                return

            messagebox.showerror(
                tr(f"{action}房间失败"), tr(str(e)), parent=self.root
            )
            return

        verb = "已创建" if state.is_owner else "已加入"
        shown = getattr(state, "room_name", "") or room

        # 新的房间 = 允许再弹一次"房间被关闭"的提示
        self._room_close_notified = False

        self.log(f'{verb} room "{shown}"', kind="ok")
        self._refresh_room_ui()
        self._refresh_room_list_soon()

        # 进去之后就把输入框清空：那块地方留着旧房间名，很容易让人以为
        # "已经在这个房间里了"，或者在下次建房时误用上一个名字。
        # （房间名本身在房间卡片的状态行里一直显示着，不会丢。）
        self.room_var.set("")


    def on_leave_room(self):
        session = self.engine.room_current()

        if session is None:
            self._refresh_room_ui()
            return

        # Walking out while a Minecraft session is running would leave the
        # tunnel pointing at a room we are no longer in.
        if self.running:
            if not messagebox.askokcancel(
                "退出房间",
                "当前连接仍在运行，退出房间会先停止连接。继续吗？",
                parent=self.root,
            ):
                return

            self.on_stop()

        if not messagebox.askokcancel(
            "退出房间",
            f"确定要离开房间 {self._current_room_name()} 吗？\n\n"
            + (
                "你是这个房间的所有者，离开后房间会被关闭。"
                if self._room_is_owner()
                else "离开后主机身份（如果你持有）会被释放。"
            ),
            parent=self.root,
        ):
            return

        state = self.engine.room_state()

        if state is not None and state.is_host:
            # Release first so the slot frees immediately rather than waiting
            # for the relay's watchdog.
            self.engine.release_host()

        self.engine.close_room()

        self.log("left the room", kind="info")
        self._refresh_room_ui()


    def on_claim_host(self):
        ok, message = self.engine.claim_host()

        if ok:
            self.log(f"host slot claimed: {message}", kind="ok")
        else:
            self.log(f"failed to claim the host slot: {message}", kind="warn")
            messagebox.showwarning("无法认领主机身份", message, parent=self.root)

        self._refresh_room_ui()


    def on_release_host(self):
        """
        释放主机身份。

        ============================================================================
          连接还在跑的时候必须拦住（用户明确要求）
        ============================================================================

        Releasing while Minecraft is being relayed would pull the position out
        from under a live world: the players who are in it are dropped, and the
        person clicking has not stopped anything. So the rule is 先停止、再释放
        — the button is disabled while a session runs, and this check makes it
        true even if the button state is stale.

        Stopping does NOT release the position any more either (see
        engine.stop_session): the position stays yours until you explicitly say
        otherwise, which is exactly what makes the two buttons meaningful.
        """
        if self.running:
            messagebox.showwarning(
                "请先停止连接",
                "当前连接还在运行。\n\n"
                "请先点「停止」，再释放主机身份 —— 否则正在你世界里的玩家"
                "会突然被断开。",
                parent=self.root,
            )
            return

        if not messagebox.askokcancel(
            "释放主机身份",
            "释放后房间里就没有主机了，其他人可以重新认领。\n\n"
            "（你仍然留在房间里，只是不再担任主机。）\n\n确定吗？",
            parent=self.root,
        ):
            return

        ok, message = self.engine.release_host()

        if ok:
            self.log(f"host slot released: {message}", kind="ok")
        else:
            self.log(f"failed to release the host slot: {message}", kind="warn")

        self._refresh_room_ui()
        self._refresh_room_list_soon()


    def _room_is_owner(self):
        state = self.engine.room_state()

        return bool(state is not None and state.is_owner)


    def _refresh_room_ui(self):
        """
        渲染房间状态，并【一次性】把房间相关的按钮状态全部对齐。

        ============================================================================
          为什么按钮状态必须都在这一个函数里
        ============================================================================

        Button states used to be set from four different places (build, start,
        stop, this). When a room was destroyed from the SERVER side, only some
        of them were updated — the窗口 kept looking like "you are still in a
        room", 开始连接 kept being clickable, and the only way out was to press
        退出房间 by hand. That is the "幽灵房" the user reported.

        One function, one truth: every button that depends on "am I in a room /
        am I hosting" is decided HERE, and every path that changes those facts
        calls it. Never raises.
        """
        state = self.engine.room_state()
        has_room = state is not None

        # Live NAT measurement follows the ROOM's mode (see _sync_room_nat):
        # it starts the moment we are in a STUN room and stops when we are not.
        # This is the one function every room-state change already goes
        # through, so hooking it here means no future room event can forget to
        # turn the measurement on or off.
        with contextlib.suppress(Exception):
            self._sync_room_nat()

        # ============================================================================
        #  ★ 房主热切换模式之后，**非房主**的会话也必须按新模式重建
        # ============================================================================
        #
        # 用户实测（审计问题 3/4）：房主把房间从 relay 切到 STUN，非房主这边
        # 什么都没发生，会话继续按旧模式搬数据（日志里 `[RELAY] connecting ...`
        # 一条接一条，而界面写着 STUN）。
        #
        # 原来这段逻辑写在 `_on_mode_change` 里，但它永远不会执行：
        #   * 非房主的模式磁贴被 `_refresh_tiles()` 锁死 -> 点击回调根本不进；
        #   * 就算进去了，`switched = self.config.mode != room_mode` 也是恒假 ——
        #     `_refresh_room_ui` 每秒（本函数上面那句 `self.config.mode =
        #     room_mode`）已经把 config 改成了房间的模式。
        #
        # 所以改到这条**每秒必跑**的同步路径上，判据换成"会话真正在用的模式"，
        # 并且带防抖（见 `_sync_session_mode`）。
        with contextlib.suppress(Exception):
            self._sync_session_mode()

        # 中继主动推过 `CONN_MODE_CHANGED`：等房间广播里的新模式一到就重建一次
        # （见 `_on_conn_mode_changed` 里的 P0 说明）。
        with contextlib.suppress(Exception):
            self._maybe_rebuild_for_mode_change()

        def enable(widget, on):
            # ★ 2026-10-03（分析结论 E2）：以前只改 `state`、不改 `bg` ——
            #   主按钮（开始连接）被释放后仍是"禁用灰底"，看着像没释放。
            #   `_set_enabled` 两样都改，这里统一走它。
            with contextlib.suppress(Exception):
                self._set_enabled(widget, on)

        # ---- 连接模式：跟着房间走，房主之外锁定 ----
        #
        # This is the ONE place that decides the selector's state, for the same
        # reason the room buttons are decided here: two places agreeing by
        # accident is how "displayed mode" and "actual mode" drift apart.
        room_mode = self._room_conn_mode()

        if room_mode is not None:
            if self.mode_var.get() != room_mode:
                self.mode_var.set(room_mode)

            # ================================================================
            #  ★ 2026-09-29（用户第 7 条）：模式变化必须**可归因**
            # ================================================================
            # 本地显示以中继的 `mode=` 为准，但**只跟着它显示**，绝不因为
            # 它去重建/回退。这里落一行日志，用户下次贴日志就能一眼看出
            # "是谁把模式改了"（中继广播 vs 自己的点击）。
            previous_mode = str(getattr(self.config, "mode", "") or "")

            if previous_mode and previous_mode != room_mode:
                self.log(
                    f"[mode] the relay now reports the room mode as "
                    f"{room_mode} (was {previous_mode}); the display follows the "
                    f"relay -- only the room owner can change the mode",
                    kind="warn",
                )

            self.config.mode = room_mode

            self.mode_owner_label.configure(
                text=mode_owner_label_text(
                    room_mode, self._am_room_owner(), True
                )
            )
        else:
            self.mode_owner_label.configure(
                text=mode_owner_label_text(None, False, False)
            )

        # Grey the tiles out for non-hosts by rebuilding them with the lock
        # flag, so a click cannot even start the flow.
        self._refresh_tiles()

        # ---- 房间入口 ----
        enable(self.create_room_button, not has_room)
        enable(self.join_room_button, not has_room)
        enable(self.leave_room_button, has_room)
        # 「房间成员」只在房间里可用：没房间时的列表没有意义，而且会让用户
        # 以为"这里能看到别处的房间"。
        enable(self.members_button, has_room)

        # 「删除我的房间」在房间里就能用（不必先开服）：中继仍然只允许所有者
        # 或当前主机执行，别人点会被它拒绝。
        # 删房 = 只有【房间所有者】能做（2026-09-24：host 不算 —— host 只是
        # 一个任何人都能认领的位置）。服务端也会拒（ERROR NOT_HOST），
        # 这里先灰掉，免得用户点了才发现。
        enable(
            self.delete_room_button,
            has_room and bool(getattr(state, "is_owner", False)),
        )

        # ---- 主机位置 ----
        # 释放必须先停止（用户要求）：连接还在跑时这个按钮是灰的，
        # on_release_host() 里还有第二道拦截。
        #
        # ============================================================================
        #  ★ 用户 2026-09-29：「连接状态 ≠ 身份/职位」—— 认领按钮只看**身份**
        # ============================================================================
        # 主机只是**停止连接**（大家断开），主机位**不释放**：所以这里必须看
        # `state.host_claimed`（中继广播的身份事实），**不能**因为它没连上就
        # 让别人的「认领主机身份」亮起来 —— 那正是用户点名不许出现的行为。
        enable(
            self.claim_host_button,
            bool(
                has_room and not state.is_host and not state.host_claimed
                and not self.running
            ),
        )
        enable(
            self.release_host_button,
            bool(has_room and state.is_host and not self.running),
        )

        # ---- 连接按钮 ----
        #
        # ★ 用户第 3 条：自动重连窗口里也算"连接还在"（见 `_session_busy`），
        # 否则掉线那一瞬间按钮就被释放了 —— 而重连其实正在进行。
        enable(self.start_button, not self._session_busy())
        enable(self.stop_button, bool(self._session_busy()))

        # ---- 房间列表的「刷新」：永远可用（它是手动兜底，自动刷新才是常态）----
        enable(self.rooms_button, True)

        if not has_room:
            self.room_state_label.configure(
                text=tr("未加入房间"), fg=FG_MUTED
            )

            with contextlib.suppress(Exception):
                self.header_room_label.configure(
                    text=tr("◆ 未加入房间"), bg=CARD_ALT, fg=FG_MUTED
                )

            # ============================================================
            #  ★ 离开房间后，「基准配置」的确定按钮必须当场解锁
            # ============================================================
            #
            # 用户报告（ISSUES 问题 4）：在房间里 mc_port 的确定按钮是灰的
            # （房间参数锁死，设计如此），**退出房间之后它没有恢复**，于是
            # 即使离开了房间也改不了端口。
            #
            # 根因：按钮状态由 `_sync_config_buttons()` 决定，而它只在
            # 「界面建好 / 提交一次 / 改一个字 / 身份变更」时被调用过 ——
            # 房间状态变化这条最常见路径从来没调过它。房间开了它不会灰，
            # 房间关了它不会亮。这里补在 `_refresh_room_ui` 里：这个函数是
            # **每一次房间状态变化（含每秒自愈）**的必经之处，所以按钮状态
            # 和房间状态永远不会再分家。
            self._sync_config_buttons()

            return

        if state.revoked:
            self.room_state_label.configure(
                text=tr(f"房间已被关闭：{state.revoked_reason}"),
                fg=BAD,
            )

            # 房间被服务端关掉 = 等同于离开房间：同样要解锁。
            self._sync_config_buttons()

            return

        display = state.display

        # ---- 顶部那个醒目的"当前房间"----
        with contextlib.suppress(Exception):
            self.header_room_label.configure(
                text=f"◆ {display}",
                bg=TILE_ON,
                fg=FG,
            )

        if state.is_host:
            host_text = (
                "主机：你（正在开服）" if self.running
                else "主机：你（没在开服 —— 点「开始连接」或「释放主机身份」）"
            )
            host_color = OK
        elif state.host_claimed:
            # ★ 用户 2026-09-29：主机位在（身份）**不等于**它连上了（连接状态）。
            # 只是停止连接时，这里要说"已断开连接 / 未开服"，而不是含糊的
            # "正在开服"，更不是"暂无主机"。
            host_conn = host_conn_state(state)
            suffix = {
                "connected": "（正在开服，可以开始连接）",
                "idle": "（已断开连接 / 未开服）",
            }.get(host_conn, "")
            host_text = f"主机：{state.host_name or '别人'}{suffix}"
            host_color = FG_DIM
        else:
            host_text = "房间还没有主机，无法开始连接"
            host_color = WARN

        # 自己的显示名（中继分配的登录名 / 游客名）也要看得见 —— 否则免登录
        # 用户根本不知道别人在名单里看到的是谁。名字是数据，前缀走 tr()。
        own_name = getattr(state, "own_name", "") or state.username or state.client_id

        self.room_state_label.configure(
            text=(
                f"{display}　"
                f"房间内 {state.members} 人　"
                f"已加入世界 {state.world_players} 人\n"
                f"{host_text}"
                + ("　（你是房主）" if state.is_owner else "")
                + f"\n{tr('你的名字：')}{own_name}"
            ),
            fg=host_color,
        )

        # 在房间里：mc_port / max_players 按设计锁死（房间参数锁定是已验收的行为）。
        # 这里跟着房间状态同步，是为了**退出房间那一刻**同一个函数也能把它解开
        # —— 见上面 `not has_room` 分支的说明（ISSUES 问题 4）。
        self._sync_config_buttons()


    def on_show_members(self):
        """「房间成员」按钮：打开（或前置）成员列表窗口。"""
        self._open_member_window()


    def _close_member_window(self):
        """关掉成员列表窗口并把引用清干净（下次打开是全新的一扇窗）。"""
        win = getattr(self, "member_window", None)

        self.member_window = None
        self.member_table = None
        self.member_hint = None
        self.member_rows = {}
        self.member_names_by_key = {}

        if win is not None:
            with contextlib.suppress(Exception):
                win.destroy()


    def _render_member_rows(self, rows):
        """
        把 build_member_rows() 的结果画进表格。

        ============================================================================
          不闪 / 不抢焦点 / 没有空行
        ============================================================================
          * 控件按成员 key **复用**：见过的成员只改文字、只重排 grid 行号；
          * 走了的成员才销毁它那一行的控件（不会留下空白行）；
          * `_member_list_filling` 守卫：刷新期间被重入时直接返回，和房间列表
            的 `_room_list_filling` 是同一个教训（程序化重建触发了本该只属于
            用户操作的回调）。
        """
        table = getattr(self, "member_table", None)

        if table is None:
            return

        with contextlib.suppress(Exception):
            if not table.winfo_exists():
                return

        if getattr(self, "_member_list_filling", False):
            return

        self._member_list_filling = True

        try:
            wanted = [row.get("key") for row in rows]

            for key in list(self.member_rows):
                if key not in wanted:
                    widgets = self.member_rows.pop(key)
                    self.member_names_by_key.pop(key, None)

                    # 这一行的每一个格子（含踢出按钮 / 占位）都在
                    # `member_table` 里，必须逐个销毁 —— 没有行 Frame 可销毁。
                    with contextlib.suppress(Exception):
                        for cell in widgets["cells"].values():
                            cell.destroy()

                    for name in ("kick", "spacer"):
                        with contextlib.suppress(Exception):
                            widgets[name].destroy()

            for index, row in enumerate(rows):
                key = row.get("key")

                if not key:
                    continue

                widgets = self.member_rows.get(key)

                if widgets is None:
                    widgets = self._member_row_widgets(key, index)
                else:
                    # 行号重排：**每一格**都要跟着走（共用一套 grid）。
                    for cell in widgets["cells"].values():
                        with contextlib.suppress(Exception):
                            cell.grid_configure(row=index + 1)

                    for name in ("kick", "spacer"):
                        widget = widgets.get(name)

                        if widget is not None and widget.winfo_manager() == "grid":
                            with contextlib.suppress(Exception):
                                widget.grid_configure(row=index + 1)

                ping = row.get("ping_ms")
                # 用户名永远有值（中继真名 / 回退 client_id）；再兜一层 key，
                # 保证这一格**不可能是空的或「未知」**。
                name_text = (
                    row.get("name") or row.get("key") or tr("未知")
                )
                texts = {
                    "name": name_text,
                    # 身份 = 组合角色（`房主·主机` / `房主·玩家` / `玩家·主机` / `玩家`）；
                    # 老数据（没有 `roles`）退回单角色。
                    "role": (
                        roles_text(row.get("roles"))
                        if row.get("roles") else tr(role_text(row.get("role")))
                    ),
                    "status": _member_status_text(row),
                    # 拿不到就是 "-"：这里的每一格都必须是真实数据。
                    "nat": row.get("nat") or "-",
                    "inbound": row.get("inbound") or "-",
                    "addr": row.get("addr") or "-",
                    # 中转延迟：亚毫秒也标出来（审计问题 5），绝不写成 0。
                    "ping": p2p_rtt_text(ping),
                    # 对端延迟：适用才有数字，不适用 N/C（见 peer_latency_text）。
                    "peer_ping": peer_latency_text(
                        row.get("peer_rtt_ms"), row.get("peer_direct")
                    ),
                }

                # 踢人确认要用**这个成员显示的名字**（见 on_kick_member），
                # 所以在渲染时把当前这批名字记下来。
                self.member_names_by_key[key] = name_text

                for name, text in texts.items():
                    var = widgets["vars"].get(name)

                    if var is not None:
                        var.set(text)

                # 身份用颜色区分：房主橙、主机绿、其他灰（和别处一致）。
                role_colour = {
                    ROLE_OWNER: ROLE_COLORS.get("root", FG),
                    ROLE_HOST: OK,
                }.get(row.get("role"), FG)

                with contextlib.suppress(Exception):
                    widgets["cells"]["role"].configure(fg=role_colour)

                can_kick = bool(row.get("can_kick"))

                if widgets["kick_visible"] != can_kick:
                    with contextlib.suppress(Exception):
                        if can_kick:
                            widgets["spacer"].grid_forget()
                            widgets["kick"].grid(
                                row=0, column=widgets["kick_column"],
                                padx=2, pady=3,
                            )
                        else:
                            widgets["kick"].grid_forget()
                            widgets["spacer"].grid(
                                row=0, column=widgets["kick_column"],
                                padx=2, pady=3,
                            )

                    widgets["kick_visible"] = can_kick
        finally:
            self._member_list_filling = False


    def _refresh_member_list(self, force=False):
        """
        成员列表的一拍：数据来自引擎，渲染走 _render_member_rows。

        窗口没开时什么都不做（这是每秒钟都会被调到的函数）。
        """
        win = getattr(self, "member_window", None)

        if win is None:
            return

        try:
            if not win.winfo_exists():
                self._close_member_window()
                return
        except Exception:
            self._close_member_window()
            return

        state = self.engine.room_state()

        if state is None:
            with contextlib.suppress(Exception):
                self.member_hint.configure(text=tr("未加入房间"))

            self._render_member_rows([])
            return

        view = self._direct_status_view(self.last_snapshot or {})
        punch = self.engine.room_direct_state() or {}
        profile = (
            self.engine.room_nat_profile()
            or punch.get("nat_profile")
            or self._nat_profile
        )

        rows = build_member_rows(
            state=state,
            direct_rows=punch.get("links") or [],
            self_profile=profile,
            self_inbound_state=view.get("inbound_state") or "",
            self_ping_ms=self.engine.room_rtt_ms(),
            # ★ 每个对端自己广播的公网端点（DIRECT_ADDR，纯显示）：
            #   打洞实际用的地址优先，没有就显示这个。
            addr_rows=punch.get("addrs") or {},
            # ★ 全锥证据（对端首包证明）——纯展示，进「入站」那一档的判定。
            eif_proof=bool(punch.get("eif_proof")),
        )

        with contextlib.suppress(Exception):
            host = state.host_name if state.host_claimed else ""

            self.member_hint.configure(
                text=(
                    f"{state.display}  "
                    f"{tr('房间内')} {state.members}  "
                    f"{tr('主机')}: {host or '—'}"
                )
            )

        self._render_member_rows(rows)


    def on_kick_member(self, target):
        """
        踢出一个人。服务端是权威：这里只做"界面能点的"和"结果怎么说"。
        """
        target = (target or "").strip()

        if not target:
            return

        state = self.engine.room_state()

        # 界面第二道闸（第一道是按钮可见性）：即使状态在点之前变了也不乱发。
        if state is None or not (state.is_owner or state.is_host):
            with contextlib.suppress(Exception):
                messagebox.showwarning(
                    tr("踢人失败"),
                    tr("你没有踢人的权限（只有房主或当前主机可以）"),
                    parent=self.member_window or self.root,
                )
            return

        # 确认框里显示**这个成员的名字**（中继广播的真名 / 游客名），而不是
        # client_id：用户看不懂 cid。名字来自成员列表最近一次渲染。
        target_name = (
            (getattr(self, "member_names_by_key", {}) or {}).get(target)
            or target
        )

        if not messagebox.askokcancel(
            tr("踢出成员"),
            tr("确定要把「{0}」踢出房间吗？").replace("{0}", target_name),
            parent=self.member_window or self.root,
        ):
            return

        ok, message, code = self.engine.kick_member(target)

        if ok:
            self.log(
                f"member kicked: target={target} code={code}", kind="ok"
            )

            with contextlib.suppress(Exception):
                messagebox.showinfo(
                    tr("已踢出成员"),
                    tr("已踢出房主，房间已解散。")
                    if code == "OWNER_LEFT_ROOM"
                    else tr("该成员已被踢出房间。"),
                    parent=self.member_window or self.root,
                )
        else:
            self.log(
                f"kick failed: target={target} code={code} "
                f"message={message}",
                kind="warn",
            )

            # 服务端的原话就是中文的（describe_error 也兜了底），照原样显示。
            friendly = message

            if code == "KICK_FORBIDDEN":
                friendly = tr("你没有踢人的权限（只有房主或当前主机可以）")
            elif code == "KICK_NO_SUCH_MEMBER":
                friendly = tr("该成员不在房间里（可能已经离开）")

            with contextlib.suppress(Exception):
                messagebox.showwarning(
                    tr("踢人失败"), friendly,
                    parent=self.member_window or self.root,
                )

        self._refresh_room_ui()
        self._refresh_member_list(force=True)


    def _kick_reason_text(self, by, reason):
        """被踢的弹窗正文：由已翻译的固定句 + 中继给的数据拼成。"""
        # 中继的 reason 是给日志用的英文记号（kicked / owner_kicked）；界面上
        # 把它换成中文。认不出来的记号照原样显示（诚实：那是服务端给的）。
        known = {
            "kicked": tr("被踢出房间"),
            "owner_kicked": tr("房主被踢出（房间已解散）"),
        }

        lines = [tr("你已被踢出房间。")]
        lines.append(f"{tr('发起者：')}{by or tr('未知')}")

        if reason:
            lines.append(f"{tr('原因：')}{known.get(reason, reason)}")

        return "\n".join(lines)


    def _account_buttons(self):
        return (
            (self.login_button, self.rooms_button),
            (self.logout_button, self.rename_button),
        )


    def _refresh_account_ui(self):
        """Render the login state. Called whenever self.auth changes."""
        session = self.auth

        if session is not None:
            # Colour the account line by ROLE, so "root" is unmistakable at a
            # glance. The same three colours are used in the panel.
            role = session.effective_role
            colour = ROLE_COLORS.get(role, OK)

            self.account_label.configure(
                text=tr(f"已登录：{session.summary()}"), fg=colour
            )
            self.account_hint.configure(
                text=(
                    f"服务器：{session.server}　"
                    f"协议版本：v{session.protocol}"
                    + (
                        f"　{session.self_ops_text()}"
                        if session.self_ops_text()
                        else ""
                    )
                ),
                fg=FG_MUTED,
            )
            self._set_role_badge(role)
            self.login_button.configure(
                text=tr("切换账号"), state="normal"
            )
            self.rooms_button.configure(state="normal")
            self.logout_button.configure(state="normal")
            self.rename_button.configure(state="normal")
        else:
            self._set_role_badge(None)
            self.account_label.configure(text=tr("未登录"), fg=WARN)
            self.account_hint.configure(
                text=tr(
                    "当前服务器要求登录后才能建房 / 进房。"
                    "点「登录」输入账号密码。"
                ),
                fg=FG_MUTED,
            )
            self.login_button.configure(text=tr("登录"), state="normal")
            self.rooms_button.configure(state="normal")
            self.logout_button.configure(state="disabled")
            self.rename_button.configure(state="disabled")


    def _set_role_badge(self, role):
        """
        Show the role as a coloured chip next to the account.

        Kept as its own widget rather than baked into the label text so the
        colour can change independently -- a role change (an admin promoting
        somebody) should be visible without the text moving.
        """
        badge = getattr(self, "role_badge", None)

        if badge is None:
            return

        if not role:
            # 未登录时也要占住这块地方：留空会在标题栏右侧出现一小块
            # 说不清是什么的空隙（用户的原话："空着一小块很难看"）。
            #
            # tr() 必须在这里加：这个函数是在 rebuild_ui() 里 **翻译整棵树之后**
            # 才被调用的（见 rebuild_ui 的顺序），不在这里翻的话，每次切语言
            # 这枚徽章都会被打回中文。
            with contextlib.suppress(Exception):
                badge.configure(
                    text=tr(" 未登录 "),
                    bg=CARD,
                    fg=FG_MUTED,
                )
            return

        with contextlib.suppress(Exception):
            badge.configure(
                text=f" {tr(ROLE_LABELS.get(role, role))} ",
                bg=ROLE_COLORS.get(role, ACCENT),
                fg="#101623",
            )


    def on_login(self):
        from .auth_ui import ask_login  # local import: app.py must not import
        # auth_ui at module level (auth_ui imports the theme constants below)

        reason = ""

        if self.auth is not None:
            reason = "切换账号会断开当前连接（如果正在运行请先停止）。"

        if self.running:
            messagebox.showinfo(
                "提示", "请先点「停止」，再切换账号。", parent=self.root
            )
            return

        session = ask_login(
            self.root,
            self.engine,
            self.host_var.get().strip(),
            self._port_or_default(),
            username=self.config.username or self.username_var.get().strip(),
            client_id=self.client_id_var.get().strip(),
            reason=reason,
        )

        if session is None:
            return

        # 登录 = 游客那份临时身份就此作废（用户 2026-09-27 口径：换账号 /
        # 登录都销毁游客身份，下次连接由中继重新分配）。
        self._forget_guest_identity("an account signed in")

        self.auth = session

        # Keep the form in sync with the account that actually logged in.
        self.username_var.set(session.username)
        self.config.username = session.username

        if ":" in session.server:
            host, _, port = session.server.partition(":")
            self.host_var.set(host)
            self.port_var.set(port)

        self._refresh_account_ui()

        with contextlib.suppress(Exception):
            cfgmod.save_config(self._collect_config())

        # 登录时人**可能还在房间里**：名册里那颗名字必须跟着变，房间不重建
        # （用户要求，ISSUES 问题 5：身份绑设备）。失败只提示，不断连接。
        self._sync_identity()

        self.log(f"logged in: {session.summary()}", kind="ok")
        self.log(
            f"server {session.server}, protocol version v{session.protocol}",
            kind="dim",
        )

        # 剩余的自助修改次数（中继给的，客户端和面板共用同一个计数）。
        allowance = session.self_ops_text()

        if allowance:
            self.log(allowance, kind="dim")

        # ---- 上线状态上报 ----
        #
        # 面板的「在线账号」按真实连接统计，所以登录成功后要开一条常驻的
        # presence 连接：关软件（正常关或强杀）它就断，面板立刻不在线。
        # 旧版中继不认这条命令，会自动跳过，不影响任何联机功能。
        self.engine.start_presence(
            self.host_var.get().strip() or session.server.split(":")[0],
            self._port_or_default(),
            session,
            self.client_id_var.get().strip(),
            on_line=lambda text: self.log(text, kind="dim"),
        )


    def on_logout(self):
        if self.running:
            messagebox.showinfo(
                "提示", "请先点「停止」，再退出登录。", parent=self.root
            )
            return

        # ---- leaving the account means leaving its room ----
        #
        # A room's owner DOES NOT SURVIVE losing the account that owns it: the
        # relay records the owner from the token, so signing out while still
        # holding a room left an orphan behind -- the room stayed listed and
        # looked healthy, but it belonged to nobody and could not be deleted
        # from the client any more.
        #
        # So logging out now leaves the room first. That is also what the user
        # expects: "exit account" should not leave things running in the
        # background on a server they are no longer signed in to.
        room_state = self.engine.room_state()

        if room_state is not None:
            if not messagebox.askokcancel(
                "退出登录",
                f"你还在房间「{room_state.display}」里。\n\n"
                + (
                    "你是这个房间的所有者，退出登录会同时关闭该房间。\n"
                    if room_state.is_owner
                    else "退出登录会同时退出该房间。\n"
                )
                + "\n确定要退出登录吗？",
                parent=self.root,
            ):
                return
        elif not messagebox.askokcancel("退出登录", "确定要退出当前账号吗？"):
            return

        username = self.auth.username if self.auth else ""

        # 先把上线状态那条连接关掉：面板的「在线」就是它，主动登出之后
        # 不该等到 token 过期才消失。
        self.engine.stop_presence()

        if room_state is not None:
            # Release the host slot first so it frees immediately rather than
            # waiting for the relay's watchdog.
            if room_state.is_host:
                self.engine.release_host()

            self.log("leaving the room ...", kind="dim")
            self.engine.close_room()
            self._refresh_room_ui()

            self.log("left the room (logging out does not leave an unmanaged room behind)", kind="info")

        # Release the token on the relay too. Dropping it locally only would
        # leave the account showing as online in the panel until the token's
        # 12 hour TTL ran out.
        self.log("notifying the server of logout ...", kind="dim")
        released = self.engine.logout_session(
            self.host_var.get().strip(),
            self._port_or_default(),
            self.auth,
            self.client_id_var.get().strip(),
            timeout=8,
        )

        self.auth = None
        self._refresh_account_ui()

        # 登出 = 这次登录会话结束：游客身份随之销毁（用户 2026-09-27 口径）。
        # 放在 `self.auth = None` 之后，所以这次调用**一定**按"游客"算。
        self._forget_guest_identity("the account signed out")

        # 老版本可能把游客名写进了配置：现在清掉，免得下次连接"看起来还是同一个人"。
        if str(self.config.username or "").strip() not in ("", self.GUEST_NAME_PLACEHOLDER):
            self.config.username = self.GUEST_NAME_PLACEHOLDER
            self.username_var.set("")
            cfgmod.save_config(self.config)

        if released:
            self.log(f"logged out: {username} (the server is in sync)", kind="info")
        else:
            self.log(
                f"logged out: {username}"
                " (could not notify the server; it will expire automatically later)",
                kind="warn",
            )


    def on_rename(self):
        """
        Self-service rename.

        Deliberately allowed while a session is running: the relay patches the
        live room/session name and rebinds the token, so nobody is disconnected.
        """
        from .auth_ui import ask_rename  # local import, same reason as on_login

        if self.auth is None:
            messagebox.showinfo("提示", "请先登录账号再改名。", parent=self.root)
            return

        host = self.host_var.get().strip()

        if not host:
            messagebox.showerror("参数错误", "请先填写中转服务器地址", parent=self.root)
            return

        new_name = ask_rename(
            self.root,
            self.engine,
            host,
            self._port_or_default(),
            self.auth,
            self.client_id_var.get().strip(),
        )

        if not new_name:
            return

        # The engine patched the live session; the GUI only has to follow.
        self.username_var.set(new_name)
        self.config.username = new_name
        self._refresh_account_ui()

        with contextlib.suppress(Exception):
            cfgmod.save_config(self._collect_config())

        # 改名后房间里那颗名字也要立刻跟着变（同登录，见 _sync_identity）。
        self._sync_identity()

        self.log(f"renamed to: {new_name} (takes effect immediately; the connection is not dropped)", kind="ok")


    def require_login(self):
        """
        Return True when we may proceed; prompt for a login only if the
        RELAY says an account is required.

        This used to be a hardcoded local rule: no session -> always prompt.
        The consequence was that turning "必须登录" off in the admin panel had
        no visible effect at all, because the client never asked the server --
        it refused to start connecting and popped the login dialog on its own.
        The server side was working correctly the whole time.

        Now the client asks first (an unauthenticated ROOMS query carries the
        live switch), and only prompts when the answer is "yes" or "unknown".
        "unknown" means an unreachable or older relay, where prompting is the
        safe choice: a needless login prompt is a far smaller problem than
        letting the user connect and be refused.
        """
        if self.auth is not None:
            # Already signed in. Say so, and stop advertising a mode the
            # server may have left behind.
            self._set_account_mode_hint(True)
            return True

        host = self.host_var.get().strip()

        if not host:
            # Nothing to ask. Fall through to the prompt so the user gets a
            # usable error from the login dialog rather than silence.
            needs_account = True
            known = False
        else:
            needs_account = self.engine.relay_requires_account(
                host,
                self._port_or_default(),
                self.client_id_var.get().strip(),
            )

            known = needs_account is not None

            if needs_account is None:
                needs_account = True

        if not needs_account:
            self._set_account_mode_hint(False)
            return True

        # The server wants an account. Record that BEFORE prompting, because
        # the hint used to be written once and never corrected: after an admin
        # turned the switch back on, the account card kept claiming "服务器
        # 当前不要求登录" even while the login dialog was on screen demanding
        # one. Being told two opposite things at once is worse than being told
        # nothing.
        self._set_account_mode_hint(True, known=known)

        self.log("the server requires a login; showing the login window ...", kind="warn")
        self.on_login()

        return self.auth is not None


    def _set_account_mode_hint(self, requires_account, known=True):
        """
        Keep the account card's wording honest about the relay's live switch.

        Called on every attempt rather than once at startup, so a switch
        flipped in the panel is reflected the next time the user tries to
        connect -- instead of leaving a stale "不要求登录" on screen.
        """
        label = getattr(self, "account_hint", None)

        if label is None:
            return

        if self.auth is not None:
            # A signed-in account: the server's switch no longer matters for
            # this session, and saying "不要求登录" next to a logged-in user
            # was the confusing part.
            text = (
                f"服务器：{self.auth.server}　"
                f"协议版本：v{self.auth.protocol}"
            )
            colour = FG_MUTED
        elif requires_account:
            text = (
                "服务器要求登录后才能建房 / 进房。点「登录」输入账号密码。"
                + ("" if known else "（未能确认服务器状态，按需要登录处理）")
            )
            colour = FG_MUTED
        else:
            text = (
                "服务器当前不要求登录，可以直接建房 / 进房。\n"
                "需要账号时再点「登录」即可。"
            )
            colour = OK

        with contextlib.suppress(Exception):
            label.configure(text=text, fg=colour)


    def on_rooms(self):
        """刷新内嵌的房间列表（旧版这里是打开一个二级窗口）。"""
        self.on_rooms_refresh()


    def _refresh_room_list_soon(self):
        """
        立刻安排一次房间列表刷新（用于刚建房/刚进房/刚退出之后）。

        和 _tick 里的定时刷新是同一个入口；这里只是不等下一个周期，
        让用户点完按钮马上看到变化。
        """
        with contextlib.suppress(Exception):
            self.on_rooms_refresh()


    def on_rooms_refresh(self):
        """
        拉一次房间列表并填进主界面那张表。

        ============================================================================
          为什么是"自动刷新"而不是"点刷新"
        ============================================================================

        The list used to be a snapshot from whenever the user last pressed
        刷新: somebody releases the host position, the panel/room changes, and
        the table keeps showing the old row — which is how「释放了主機身份，
        列表还显示他在当主机」and「列表显示无人认领，其实位置还占着」both
        happened, in opposite directions.

        Now _tick refreshes it on a timer (ROOM_LIST_REFRESH_MS) whenever the
        window is visible and no request is already in flight. The 刷新 button
        still works; it just is not required any more.
        """
        # 本地导入：auth_ui 依赖本模块的主题常量，模块级导入会成环。
        from .auth_ui import AsyncTask

        host = self.host_var.get().strip()

        if not host:
            with contextlib.suppress(Exception):
                self.room_list_hint.configure(
                    text=tr("请先填写中转服务器地址")
                )
            return

        if self._room_list_busy:
            return

        self._room_list_busy = True
        self._room_list_fetched_at = time.monotonic()

        with contextlib.suppress(Exception):
            self.room_list_hint.configure(text=tr("正在获取 ..."))

        AsyncTask(
            self.root,
            self.engine.fetch_rooms,
            on_success=self._fill_room_list,
            on_error=self._room_list_failed,
        ).start(
            host,
            self._port_or_default(),
            self.client_id_var.get().strip(),
            self.auth.username if self.auth else "",
        )


    def _room_list_failed(self, message):
        self._room_list_busy = False

        with contextlib.suppress(Exception):
            self.room_list_hint.configure(
                text=tr(f"刷新失败：{message}")
            )

        # 自动刷新失败不要每次都刷屏：只在切换成失败时记一条。
        if not self._room_list_failed_once:
            self._room_list_failed_once = True
            self.log(f"failed to refresh the room list: {message}", kind="warn")


    def _fill_room_list(self, rooms):
        self._room_list_busy = False
        self._room_list_failed_once = False
        self.room_rows = list(rooms or [])

        # 刷新时保住用户已经选中的那一行（按房间 ID 找回），否则每次自动刷新
        # 都会把选择清掉，"选好了准备点加入"就白选了。
        keep = ""

        with contextlib.suppress(Exception):
            selected = self._selected_room_row()

            if selected is not None:
                keep = selected.room

        for item in self.rooms_tree.get_children():
            self.rooms_tree.delete(item)

        mine = (self.auth.username if self.auth else "") or ""

        for index, room in enumerate(self.room_rows):
            is_mine = bool(mine) and room.owner == mine

            if is_mine and room.players > 0:
                tag = "mine_busy"
            elif is_mine:
                tag = "mine"
            elif room.players > 0:
                tag = "busy"
            else:
                tag = "empty"

            self.rooms_tree.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    room.display,
                    room.owner or "-",
                    room.host_text,
                    room.members,
                    room.players_text(),
                    room.state_text,
                ),
                tags=(tag,),
            )

        if keep:
            # 程序化重选只是为了让"已经选好的那一行"在刷新后还在，
            # **不能**让它触发 on_room_row_selected 去改写输入框 ——
            # 否则用户正在敲的房间名每几秒被覆盖一次（用户报的第 2 个问题）。
            self._room_list_filling = True

            try:
                for index, room in enumerate(self.room_rows):
                    if room.room == keep:
                        self.rooms_tree.selection_set(str(index))
                        break
            finally:
                self._room_list_filling = False

        count = len(self.room_rows)
        busy = sum(1 for r in self.room_rows if r.players > 0)

        with contextlib.suppress(Exception):
            self.room_list_hint.configure(
                text=(
                    f"共 {count} 个房间，{busy} 个有玩家（自动刷新，双击一行即可加入）"
                    if count
                    else "现在没有房间（先点「创建房间」）"
                )
            )


    def _selected_room_row(self):
        """列表里当前选中的那一行（RoomInfo），没选返回 None。"""
        with contextlib.suppress(Exception):
            selection = self.rooms_tree.selection()

            if selection:
                return self.room_rows[int(selection[0])]

        return None


    def on_room_row_selected(self, _event=None):
        """
        用户【真的】点中一行 = 把房间名填进输入框。

        ============================================================================
          为什么必须区分"用户点的"和"刷新替我们选的"（用户 2026-09-24 报的）
        ============================================================================

        用户的原话：单击选中一行之后**取消不掉**（Tk 的 Treeview 就是这样），
        而列表每几秒自动刷新一次；为了让"选好了准备点加入"不被刷掉，
        `_fill_room_list` 刷新后会**重新选中**原来那一行 —— 于是
        `<<TreeviewSelect>>` 又触发一次，房间名每几秒就被写回输入框一遍。
        结果：正在输入框里敲新房间名，刚打几个字就被整个覆盖掉。

        所以：
          * 刷新期间程序化的重选（`_room_list_filling`）**不写输入框**；
          * 用户正在输入框里打字时（焦点在输入框）**不写输入框**；
          * 只有"人在列表上真的点了一下"才写。
        """
        if getattr(self, "_room_list_filling", False):
            return

        room = self._selected_room_row()

        if room is None:
            return

        if self.engine.room_current() is not None:
            # 已经在房间里了：只提示，不动输入框（避免看起来像换房成功了）。
            return

        # 焦点在房间名输入框里 = 用户正在敲字，别覆盖他的输入。
        with contextlib.suppress(Exception):
            if self.root.focus_get() is self.room_entry:
                return

        with contextlib.suppress(Exception):
            self.room_var.set(room.display)


    def on_join_selected_room(self):
        """双击列表里的一行 = 加入那个房间。"""
        room = self._selected_room_row()

        if room is None:
            return

        self._on_room_chosen(room.display)


    def _on_room_chosen(self, room):
        """
        A room was picked in the room list.

        Selecting a room and ENTERING it are the same gesture on a double
        click: the whole point of picking one from the list is to go there.
        """
        self.room_var.set(room)
        self.log(f"selected from the room list: {room}", kind="info")

        if self.engine.room_current() is not None:
            messagebox.showinfo(
                "提示",
                "你已经在一个房间里了，请先点「退出房间」再换。",
                parent=self.root,
            )
            return

        self.on_join_room(room)


    def on_copy_address(self):
        text = (self.addr_var.get() or "").strip()

        if not text or text == "—":
            self.log("no address to copy yet: click 'start connection' first.", kind="warn")
            messagebox.showinfo(
                "提示", "还没有连接地址。\n\n请先点「开始连接」。", parent=self.root
            )
            return

        if "127.0.0.1" not in text:
            self.log(
                f'the current address is "{text}", which is not a Minecraft '
                f"connection address (only player mode has a 127.0.0.1 address).",
                kind="warn",
            )
            messagebox.showinfo(
                "提示",
                "只有【玩家 Player】模式才有 Minecraft 连接地址。\n\n"
                "主机模式下这里显示的是本机世界端口，"
                "玩家连的是他们自己电脑上的 127.0.0.1 地址。",
                parent=self.root,
            )
            return

        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.root.update_idletasks()
        except Exception as e:
            self.log(f"copy failed: {e}", kind="error")
            return

        # 绿色日志行就是"复制成功"的提示（不弹窗，不打断操作）
        self.log(f"copied the Minecraft connection address: {text}", kind="ok")


    def _update_address(self, snapshot):
        """
        刷新「Minecraft 连接地址」。

        玩家模式 -> 127.0.0.1:<本地代理端口>（真正要复制给 Minecraft 的地址）
        主机模式 -> 本机世界端口（仅提示，不是给玩家用的地址）
        未启动   -> —
        """
        role = snapshot.get("role")
        state = snapshot.get("state")

        if role == "player" and snapshot.get("local_endpoint"):
            self.addr_var.set(str(snapshot["local_endpoint"]))

        elif role == "host" and state == STATE_ONLINE:
            self.addr_var.set(
                f"本机世界 {snapshot.get('mc_endpoint', '')}"
            )

        elif not role:
            self.addr_var.set("—")


    def on_delete_my_room(self):
        """
        删除（关闭）自己的房间。

        Who may do this, and when -- the rules, in one place:

          * 房间的所有者 (owner) 或 持有主机身份的人 (host) —— the relay decides
            and refuses anybody else with ERROR NOT_HOST.
          * 房间里还有 Minecraft 玩家时，客户端拒绝。This is a safety rail for
            players, not a permission: the panel's 「强制关闭」 exists for when
            you really do need to end it with people inside.
          * 只有房间成员、还没有人进世界时，允许删除：成员会收到
            「ROOM REVOKED」而不是被当成掉线。

        It also has to work while the client is NOT connected to a world: being
        in a room without hosting is a normal state now, and leaving the button
        dead in that state is why 「客户端也没法删房间」.
        """
        # 删的必须是"我现在在的那个房间"，不是输入框里可能已经空掉的内容。
        room = self._active_room_name()

        if not room:
            messagebox.showerror(
                "还没有进入房间",
                "你现在不在任何房间里。\n\n"
                "要删除房间，请先创建或加入它。",
                parent=self.root,
            )
            return

        if not self.require_login():
            return

        # Players in the WORLD may come from the live session; when there is no
        # session, the room only holds members.
        snapshot = self.engine.snapshot()
        players = int(snapshot.get("players") or 0)

        if players > 0:
            messagebox.showwarning(
                "不能删除",
                f"房间 {room} 里还有 {players} 名玩家。\n\n"
                "请先让玩家退出 Minecraft，或者由服主在管理面板强制关闭。",
                parent=self.root,
            )
            return

        if not messagebox.askyesno(
            "确认删除",
            f"确定要删除房间 {room} 吗？\n\n"
            "删除后房间立即关闭，其他玩家将无法再加入。",
            parent=self.root,
        ):
            return

        self.delete_room_button.configure(state="disabled")
        self.root.update_idletasks()

        self.log(f"deleting room {room} ...", kind="warn")

        # Run off the GUI thread: the request may wait on the control socket.
        import threading

        def worker():
            try:
                if self.running:
                    result = self.engine.session_delete_room(room)
                else:
                    result = self.engine.delete_room(
                        self.host_var.get().strip(),
                        self._port_or_default(),
                        room,
                        self.auth,
                        self.client_id_var.get().strip(),
                    )
            except Exception as e:  # pragma: no cover - defensive
                result = (False, str(e))

            self.root.after(0, lambda: self._on_delete_room_result(result))

        threading.Thread(target=worker, name="delete-room", daemon=True).start()


    def _on_delete_room_result(self, result):
        ok, message = result

        self.delete_room_button.configure(
            state="normal" if self.running else "disabled"
        )

        if ok:
            self.log(message, kind="ok")
            messagebox.showinfo("删除成功", message, parent=self.root)
        else:
            self.log(message, kind="error")
            messagebox.showwarning("删除失败", message, parent=self.root)


    def _port_or_default(self):
        try:
            return int(self.port_var.get())
        except (TypeError, ValueError):
            return 19100


    def _on_mode_change(self):
        mode = self.mode_var.get()

        # V5 支持三种房间策略；AUTO 内部路径迁移不走此模式切换入口。
        if mode not in (MODE_RELAY, MODE_STUN, MODE_AUTO):
            self.mode_var.set(MODE_RELAY)
            mode = MODE_RELAY

        # The ROOM's mode wins once we are in a room. Only the host may change
        # it, and changing it restarts every tunnel, so it goes through a
        # confirmation window (see _apply_room_mode).
        room_mode = self._room_conn_mode()

        if room_mode is not None and mode != room_mode:
            if not self._am_room_owner():
                # 非 Owner（**包括非 Owner 的 Host**）：把选择器弹回去并说清谁决定。
                #
                # NOTE: the "the host switched the mode, so rebuild OUR session"
                # work that used to live here was dead code (the tiles of a
                # non-host are disabled, AND `switched` was always False because
                # `_refresh_room_ui` writes `config.mode = room_mode` every
                # tick). It now lives on the per-tick sync path where it actually
                # runs: `_sync_session_mode()`.
                self.mode_var.set(room_mode)
                self.config.mode = room_mode
                self._refresh_tiles()
                self.log(
                    "the connection mode is decided by the host; currently "
                    f"{'relay' if room_mode == MODE_RELAY else 'STUN direct'}",
                    kind="warn",
                )
            else:
                # Host: confirm, because this disconnects everyone.
                if not self._confirm_mode_switch(room_mode, mode):
                    self.mode_var.set(room_mode)
                    self.config.mode = room_mode
                    self._refresh_tiles()
                    return

                self._apply_room_mode(mode)

        if mode == MODE_AUTO:
            self.mode_hint.configure(text=tr(
                "AUTO：开始连接后先用中继联机，后台逐玩家尝试直连；"
                "直连断开时自动使用备用中继，游戏连接尽量保持。"))
        elif mode == MODE_STUN:
            self.mode_hint.configure(
                text=tr(
                    "直连模式：只尝试 P2P 打洞直连，游戏数据不经中转服务器。"
                    "打不通时不会自动改用中转 —— 需要联机请手动切回"
                    "「Relay 中转」或「AUTO 自动」。"
                )
            )
        else:
            self.mode_hint.configure(
                text=tr(
                    "中转模式：玩家 → 中转服务器 → 主机。"
                    "所有游戏数据经由中转服务器转发，兼容性最好。"
                )
            )

        # 选路只有一个事实来源：这里把决定写进配置，连接时读它。
        with contextlib.suppress(Exception):
            self.config.mode = mode


    def _on_role_change(self):
        """
        原来叫"身份切换"，现在**没有身份选择器了**（用户要求删除）。

        ============================================================================
          身份选择器为什么被整个删掉（用户原话）
        ============================================================================

        「把整个身份配置那个东西做成基准配置，把主机 host 和玩家 player 这个
          选择器直接移除掉，免得它去影响到操作那一栏。」

        它有三个真问题，全都指向"它本来就不该存在"：

          1. 真实身份由「谁持有主机位置」决定（见 _decide_connect_role），
             选择器却让人以为"选了主机就一定是主机"。
          2. 它会把另一个端口输入框藏起来，而"该用哪个端口"恰恰取决于真实身份
             —— 于是它间接影响了能不能连上（用户报的"依旧会影响开服"）。
          3. 默认值是 host，第一次点没反应，要来回点两次（用户报的）。

        现在这个函数只做一件事：**按"房间在不在"决定三个确定按钮能不能按**
        （用户要求：建房之后不许再改这些数字）。名字保持不变，因为
        `_refresh_room_ui()` 和界面重建流程都在调它。
        """
        self._sync_config_buttons()


    def _sync_config_buttons(self):
        """
        按"这个键的当前状态"刷新三个「确定」按钮 **和每行的反馈文字**。

        mc_port / max_players：房间建了就禁用（改了就掉线）。
        local_port：永远可用 —— 它只是一个本机监听端口，和隧道无关，
        房主也不用它，所以玩家随时能改。

        ============================================================================
          反馈为什么是"状态驱动"而不是"提交时写一句话"
        ============================================================================

        第一版在提交成功时把那一行改成「✓ 已生效：25565」—— 外观测试立刻抓到
        它是个**不稳定状态**：切语言/切主题会重建界面，重建后那行变回静态提示，
        于是"重建前后必须一模一样"这条断言就红了。

        状态驱动就没这个问题，而且信息更实在 —— 每一行随时都显示它此刻的真实
        状态，不管是刚启动、刚提交、还是改了一半：

            未提交：改了请按「确定」        （你改了但没提交）
            🔒 已锁定 —— …                  （房间已建，改不了）
            ✓ 已生效：25565                  （这个值就是现在会用的值）
            （静态说明）                      （还没填过）
        """
        with contextlib.suppress(Exception):
            for variable, _entry, button, _status, hint in getattr(
                self, "_config_rows", []
            ):
                key = self._config_key_of(variable)
                locked = self._config_locked(key)
                dirty = self._is_config_dirty(variable)

                self._set_enabled(button, not locked)
                button.configure(
                    bg=ACCENT if (dirty and not locked) else CARD_ALT
                )

                if dirty:
                    self._set_row_status(
                        key, "未提交：改了请按「确定」", kind="warn"
                    )
                elif locked:
                    self._set_row_status(
                        key,
                        "🔒 已锁定 —— 房间已建，要改请先删房重建",
                        kind="warn",
                    )
                elif self._committed.get(key):
                    self._set_row_status(
                        key,
                        "✓ 已生效：%s" % self._committed.get(key),
                        kind="ok",
                    )
                else:
                    self._restore_row_hint(key)


    def on_random_room(self):
        import random
        import string

        room = "mc" + "".join(
            random.choice(string.ascii_lowercase + string.digits)
            for _ in range(6)
        )
        self.room_var.set(room)
        self.log(f"generated a random room name: {room}", kind="dim")


    def on_reset_server(self):
        self.host_var.set(cfgmod.DEFAULT_SERVER_HOST)
        self.port_var.set(str(cfgmod.DEFAULT_SERVER_PORT))
        self.log("relay server address restored to default", kind="dim")


    def _collect_config(self, role_override=None):
        """
        Build the config from the form.

        `role_override` is the CONNECTION role, decided by who holds the host
        position in the room (see _decide_connect_role). There is no longer any
        identity selector to disagree with it.

        ============================================================================
          端口只认「按过确定」的值（用户要求）
        ============================================================================

        用户原话：「每次修改的话都必须要按一下，就输入完新的之后不会立即生效，
        按右边的提交。」

        所以这里读的是 `_committed`（按过确定的那个数字），不是输入框里的字。
        输入框里可能停着一个还没提交的新端口 —— 那种值绝不能拿去建隧道，
        因为隧道是建房那一刻按这个数字建好的，用没提交的值建出来的隧道会
        和界面显示的对不上。
        """
        mc_port = self._committed_value(
            "mc_port", getattr(self.config, "mc_port", None)
        )
        local_port = self._committed_value(
            "local_port", getattr(self.config, "local_port", None)
        )

        if not mc_port:
            raise ValueError(
                "还没填「主机 Host：Minecraft 局域网端口」。\n\n"
                "请在上面填好，然后按右边的「确定」。"
            )

        if not local_port:
            raise ValueError(
                "还没填「玩家 Player：本地代理端口」。\n\n"
                "请在上面填好，然后按右边的「确定」。"
            )

        try:
            server_port = int(self.port_var.get())
        except (TypeError, ValueError):
            raise ValueError("中转服务器端口必须是数字")

        # 人数上限：超出就报错要求重填（而不是悄悄夹到极限值）
        #
        # Silently clamping was worse than an error: somebody typing 20 would
        # get a room for 8 and never be told why their friends could not join.
        max_players = self._committed_value(
            "max_players",
            getattr(self.config, "max_players", None)
            or proto.DEFAULT_MAX_PLAYERS,
        )

        if not (proto.MIN_MAX_PLAYERS <= max_players <= proto.MAX_MAX_PLAYERS):
            raise ValueError(
                f"人数上限只能是 {proto.MIN_MAX_PLAYERS}-"
                f"{proto.MAX_MAX_PLAYERS}，当前是 {max_players}。\n\n"
                "请在「基准配置」里改好，然后按右边的「确定」。"
            )

        # ---- 隧道数量跟随【房间】的配置，不跟随本机这个框 ----
        #
        # 这是用户报的那个 BUG：房间主人建房时填 8，但另一台机器当 host
        # 时本机填的是 3 —— 以前 max_tunnels 直接取本机这个框，于是只开
        # 3 条隧道；配对是"一条隧道配一个人"，第 4 个人永远配不上对，
        # 在 Minecraft 里就是连不上（房间却显示 8）。
        #
        # 现在：只要中继告诉了我们房间的容量，就以它为准。建房的人（owner）
        # 填的就是房间容量，两边天然一致；换谁当 host 都开一样多。
        # ============================================================================
        #  身份**必须**由调用方给出，这里不再有静默默认
        # ============================================================================
        #
        # 原来这里是 `role_override or "player"`。那个默认值制造了审计问题 3：
        # `_rebuild_tunnels()` 忘了传 role_override，于是房主切模式时被静默地
        # 重建成了 Player —— 房间瞬间没有 host，所有人都 NO_HOST。
        #
        # 现在：连接路径（开始连接 / 重建 / 自动重建）必须显式传一个已决定的
        # 身份；只有"纯粹为了保存表单值"的调用（登录成功、改名、退出时持久化）
        # 才允许省略，那种情况走同一个判定函数（拿不到房间就用上次用过的身份）。
        # 缺身份不再是静默降级，而是一行错误日志 + 响亮的报错。
        if role_override not in ("host", "player"):
            if role_override is None:
                role_override = self._safe_connect_role()
            else:
                message = (
                    "internal error: invalid connection role %r; refusing to "
                    "guess" % (role_override,)
                )
                self.log(message, kind="error")
                raise ValueError(message)

        connect_role = role_override
        room_capacity = self._room_capacity()

        if room_capacity and connect_role == "host":
            max_players = room_capacity

        self.config.server_host = self.host_var.get().strip()
        self.config.server_port = server_port
        self.config.username = self.username_var.get().strip()
        self.config.client_id = self.client_id_var.get().strip()
        self.config.role = connect_role
        # 连接用的是"我现在真正在的房间"，不是输入框里残留的文字。
        # 输入框在进房成功后会被清空，读它就会得到「房间不能为空」。
        self.config.room = self._active_room_name()
        self.config.mc_port = mc_port
        self.config.local_port = local_port
        self.config.max_players = max_players

        # max_tunnels is DERIVED in validated(); assigning it here as well
        # would be a second source of truth, which is how the two drifted
        # apart in the first place.

        return self.config.validated()


    def on_test_server(self):
        host = self.host_var.get().strip()

        try:
            port = int(self.port_var.get())
        except (TypeError, ValueError):
            messagebox.showerror("参数错误", "中转服务器端口必须是数字")
            return

        if not host:
            messagebox.showerror("参数错误", "请填写中转服务器地址")
            return

        self.server_status_label.configure(
            text=tr("● 测试中 ..."), fg=WARN
        )
        self.log(f"testing {host}:{port} ...", kind="info")

        self._set_enabled(self.test_button, False)

        # Never block the GUI thread on a socket.
        import threading

        def worker():
            try:
                ok, message, rtt = self.engine.test_relay(host, port)
            except Exception as e:  # pragma: no cover - defensive
                ok, message, rtt = False, f"测试失败：{e}", None

            self.root.after(0, lambda: self._on_test_result(ok, message, rtt))

        threading.Thread(target=worker, name="relay-probe", daemon=True).start()


    def _on_test_result(self, ok, message, rtt):
        self._set_enabled(self.test_button, True)

        if ok:
            self.server_status_label.configure(
                text=tr(f"● 服务器正常 {rtt:.0f} ms")
            if rtt
            else tr("● 服务器正常"),
                fg=OK,
            )
            self.log(message, kind="ok")
        else:
            self.server_status_label.configure(
                text=tr("● 服务器不可用"), fg=BAD
            )
            self.log(message, kind="error")


    def _room_state_obj(self):
        """The live RoomState, or None when not in a room."""
        try:
            return self.engine.room_state()
        except Exception:
            return None


    def _room_conn_mode(self):
        """The room's mode as the RELAY reports it, or None when not in a room."""
        state = self._room_state_obj()

        if state is None:
            return None

        mode = getattr(state, "conn_mode", None)

        return mode if mode in (MODE_RELAY, MODE_STUN, MODE_AUTO) else None


    def _sync_room_nat(self):
        """
        Start / stop the live NAT measurement + punch to match what the user is
        DOING, not just where they are standing.

        ============================================================================
          3.0.3：触发点是【点「开始连接」】，不是进房
        ============================================================================

        The owner's rule, repeated many times and now followed literally:

            clicking 开始连接 = 开始打洞

        The direct link is a long-lived NETWORK channel between the host and
        each player -- it exists while the host is running and is completely
        independent of any Minecraft connection or any other process. It must
        NOT wait for a game session to start punching, and it must NOT die when
        a game session ends.

        So the switch is:

            active = (room mode is STUN) AND (a session is running)

        Clicking 停止 stops the session, and with it the punch and every channel
        this side holds: on the host that is every player's channel; on a player
        it is only that player's own -- which is exactly the required lifetime
        ("host 断开 → 通道全关；玩家断开 → 只断自己那条").

        The reason it is safe to key off the GUI's `running` here: `on_start`
        and `on_stop` both end in `_refresh_room_ui()`, and `_tick` re-runs this
        every second, so the switch follows the button within one UI tick.
        """
        stun_room = self._room_conn_mode() in (MODE_STUN, MODE_AUTO)

        # ONE entry point, and it starts BOTH room-level jobs:
        #
        #   * the live NAT measurement (RoomNatMonitor), and
        #   * the punch (RoomDirectLink) -- on 开始连接, not on game traffic.
        #
        # It used to be two calls into two different implementations
        # (`watch_room_punch` and `watch_room_direct`), which meant two rival
        # punch loops fought over the same peer: 41 punches and 13 "ROOM LINK
        # UP" in one of the owner's sessions, with the player disconnected by
        # the relay four times. One owner per room-level job, or they fight.
        # ==================================================================
        #  ⑤ 模式一变就把房间级任务**整个重来**一遍
        # ==================================================================
        #
        # 用户 2026-09-25 报的："中转换 STUN 再 STUN 换中转回去，切换连接会导致
        # 隧道出现问题，切回 STUN 时日志出现拒绝连接 —— 明显还是走了中继。"
        #
        # 换模式时那些 carrier / 打洞链路是按**旧模式**的本地后端和端口建起来的
        # （`set_room_direct_backend` 指的还是上一个会话的地址），复用它们必然
        # 出现"界面说 STUN、数据走旧路"的撕裂。所以模式一变就全部关掉重建：
        # 下面那句 watch_room_nat(True) 会把它们按新配置重新拉起来。
        #
        # 只在**模式真的变了**时做（`_punch_mode`），否则每秒一次的刷新会把刚
        # 打好的洞反复关掉。
        mode_now = self._room_conn_mode()

        if mode_now is None:
            # ============================================================================
            #  ★ 用户 2026-09-29 第 7 条：房间模式**未知**绝不等于 relay
            # ============================================================================
            # 旧代码是 `self._room_conn_mode() or MODE_RELAY`：只要那一秒广播里没有
            # `mode=`（换模式正在拆旧通路 / 老中继 / 房间状态抖动），本函数就会把
            # `_punch_mode` 从 stun 改成 relay，紧接着 `watch_room_direct(False)`
            # **把刚打通的直连整个拆掉** —— 用户看到的就是"直连玩一会儿游戏断开、
            # 模式自己跳到中转（没有任何人操作）"。
            #
            # 现在：模式未知 **什么都不做**，保持上一次已知的模式（宁可不动作）。
            # `_punch_mode` 只会因为两种事实而改变：中继明确广播的 `mode=`，或者
            # Owner 的显式操作（`_apply_room_mode`）。客户端**绝不会**自己回退到
            # relay。
            mode_now = getattr(self, "_punch_mode", None)

        if mode_now is not None and mode_now != getattr(self, "_punch_mode", None):
            self._punch_mode = mode_now
            self.log(
                f"connection mode is now {mode_now}; rebuilding the room-level "
                f"NAT/punch work from scratch",
                kind="warn",
            )

            with contextlib.suppress(Exception):
                self.engine.watch_room_direct(False, log=self.log)

        # EXCEPTION SAFETY: `_render_hole_rows` reads `punching`/`ready` from the
        # room punch, so an exception here must not abort `_refresh_room_ui`
        # before that line -- a hole row would then keep claiming 打洞中 in relay
        # mode (audit problem 2). Never let a mode-sync failure hide the truth.
        #
        # `standalone_host`：只把中继地址**记下来**给冷启动用，它**不再**是启动
        # 测量的理由 —— 设计基准 §E（2026-09-27 用户逐条确认）明确推翻了那次
        # "打开软件就测"：「测量是在点『开始连接』之后按这个顺序做的（不是打开
        # 软件就测）」。唯一的上游开关就是第一个参数
        # （`stun_room and self.running` = 用户真的点了开始连接）。
        with contextlib.suppress(Exception):
            self.engine.watch_room_nat(
                bool(stun_room and self.running),
                log=self.log,
                standalone_host=getattr(self.config, "server_host", "") or "",
            )


    SESSION_MODE_REBUILD_COOLDOWN_S = 5.0


    def _sync_session_mode(self):
        """
        会话真正在用的模式 != 房间当前模式时，自动重建一次（带防抖）。

        ============================================================================
          为什么必须看 `session_mode()`，而不是 `config.mode`
        ============================================================================
        模式是在**会话创建那一刻**冻进 core 的（`session.py` 里的 config.mode）。
        房间热切换只改中继的广播和界面，正在跑的 core 不会自己变。而
        `_refresh_room_ui` 每秒都把 `self.config.mode` 同步成房间的模式，所以
        "config 和房间不一致"这个判据永远是假的 —— 只有"会话真正在用的模式"
        才是事实（审计问题 3/4：非房主侧那条自动重建是死代码）。

        防抖用 [冷却时间 + 目标模式] 而不是"只做一次"：
          * 冷却挡住每秒一次的刷新把重建打成死循环；
          * 重建失败（会话没起来 / 又断了）时，几秒后还允许再试一次，
            不会把用户永久卡在"旧模式"里。

        已经有一个重建在跑（`_rebuild_in_flight`）时绝不叠加第二次：审计的
        log2 里两次 teardown 交叠（33ms 内停旧起新，旧 host 还在收尾）。
        """
        delta = self._session_mode_rebuild_delta()

        if delta is None:
            return

        session_mode, room_mode = delta

        self._last_session_mode_rebuild_at = time.time()
        self._session_mode_rebuild_target = room_mode

        self.log(
            f"this session is still on the {session_mode} mode while the room "
            f"is now {room_mode}; rebuilding this connection to match "
            f"(automatic: every client rebuilds its own side; nobody has to reconnect by hand)",
            kind="warn",
        )
        # ★ 静默（用户 2026-10-02）：按钮不释放、状态栏正常，只悄悄重建。
        #   只设"下一次静默"标记 —— 真正武装在 `_rebuild_tunnels` 里（否则一次被
        #   挡回的重建就会把界面永久卡在"在连接"，见 2026-10-03 致命回归）。
        self._mode_rebuild_silent_next = True
        self._rebuild_tunnels(mode=room_mode)


    def _maybe_rebuild_for_mode_change(self):
        """
        中继推过 `CONN_MODE_CHANGED` 之后的**主动重建**（用户第 6 条 P0）。

        ============================================================================
          为什么要有这一条，而不是只靠 `_sync_session_mode()`
        ============================================================================
        `_sync_session_mode()` 的判据是"会话真正用的模式 != 房间当前模式"。这
        在"会话还活着"时是对的，但它漏掉一种：中继换模式时会把旧通路**拆掉**
        （`disconnect_game_connections`），会话可能已经停在 `session_mode()==''`。
        那时代码里 `if not session_mode: return None` 直接放弃 —— 界面还在
        "运行中"，但不会自己去按新模式重建，只能等自动重连的退避（慢，而且
        用户看到的是"热切换不能用了"）。

        这里补的正是那一条：只要 `conning_mode_changed` 打过标记、房间广播里的
        **新模式已经到了**，就立刻按新模式重建一次（`_rebuild_tunnels` 自己带
        防重入）。会话已经在新模式上 -> 清标记，不折腾。
        """
        if not getattr(self, "_mode_rebuild_pending", False):
            return False

        if not self.running or getattr(self, "_rebuild_in_flight", False):
            return False

        room_mode = self._room_conn_mode()

        if room_mode not in (MODE_RELAY, MODE_STUN, MODE_AUTO):
            # 广播还没到 / 不在房间：**等**，绝不拿旧模式去重建（那正是 bug）。
            return False

        # ★ P2（2026-10-03 分析）：中继先发 `CONN_MODE_CHANGED`、后发
        #   `HOST_STATE mode=<新>`。如果此刻房间模式**还是切换前那个**，说明
        #   新模式还没到 —— 继续等，**不要**清标记、更不要拿旧模式重建
        #   （旧代码就在这里把标记清了，host 于是永远不重建）。
        if room_mode == getattr(self, "_mode_rebuild_from", None):
            return False

        try:
            session_mode = str(self.engine.session_mode() or "")
        except Exception:
            session_mode = ""

        if session_mode == room_mode:
            # 会话已经是新模式了（比如 `_sync_session_mode` 刚重建完）：不重复。
            self._mode_rebuild_pending = False
            return False

        self._mode_rebuild_pending = False
        self._last_session_mode_rebuild_at = time.time()
        self._session_mode_rebuild_target = room_mode

        self.log(
            f"the relay announced a mode change and the room now reports "
            f"{room_mode} (this session is on {session_mode or 'none'}); "
            f"rebuilding every tunnel for the new mode",
            kind="warn",
        )
        self._mode_rebuild_silent_next = True
        self._rebuild_tunnels(mode=room_mode)

        return True


    def _session_mode_rebuild_delta(self):
        """
        (会话模式, 房间模式) 当且仅当"该自动重建一次"时；否则 None。

        拆成一个纯判定函数，回归测试才能直接钉住"只请求一次，不会每次都请求"
        （见 tests/room_host_session_test.py）。
        """
        if not self.running or getattr(self, "_rebuild_in_flight", False):
            return None

        room_mode = self._room_conn_mode()

        if room_mode not in (MODE_RELAY, MODE_STUN, MODE_AUTO):
            return None

        try:
            session_mode = str(self.engine.session_mode() or "")
        except Exception:
            return None

        # ★ 2026-10-03：P5（"session_mode 为空也重建"）**先不加** —— 它会让
        #   "会话一没就重建"，在用户主动停止 / 测试桩里都会立刻起一次重建，
        #   把 `_rebuild_in_flight` 卡住（回归网直接抓到）。模式切换那条路已经由
        #   P2（等新模式到）覆盖；需要 P5 时再连测试一起做。
        if not session_mode or session_mode == room_mode:
            return None


        now = time.time()

        # 同一个目标模式刚重建过 -> 让它先跑完；冷却过去之后允许重试一次。
        if (
            getattr(self, "_session_mode_rebuild_target", None) == room_mode
            and now - getattr(self, "_last_session_mode_rebuild_at", 0.0)
            < self.SESSION_MODE_REBUILD_COOLDOWN_S
        ):
            return None

        return session_mode, room_mode


    def _report_started(self, connecting):
        """
        把"我点了开始连接 / 我停了"报给中继（`STARTED <1|0>`，PROTOCOL §8.6）。

        ============================================================================
          ★ 用户 2026-09-28 第 5 条：房间里有人 ≠ 他已经开始连接
        ============================================================================

        中继的 `started=` 原来只按**连接**现算，而 STUN 模式是"先点开始打洞、
        游戏连接是之后的事" —— 只按连接算的话 Host 拉不到已经点了开始的玩家
        （见 `punch_flow.beacon_line` 的 `from=` 兜底）。显式上报把用户意图说清楚。

        **同一状态只发一次**（用户/协调者口径）：`_started_reported` 记住上次的
        值；进新房间时在 `room_joined` / `_teardown_room_locally` 里复位。

        放在后台线程：老中继会忽略这一行（等到超时），**绝不**让它冻住界面。
        """
        connecting = bool(connecting)

        if getattr(self, "_started_reported", None) == connecting:
            return False

        self._started_reported = connecting

        self.log(
            f"[room] reporting STARTED {1 if connecting else 0} to the relay "
            f"(I {'started' if connecting else 'stopped'} connecting)"
            + (
                ""
                if hasattr(self.engine, "room_set_started")
                else " (not supported by this build)"
            ),
            kind="dim",
        )

        engine = getattr(self, "engine", None)

        if engine is None or not hasattr(engine, "room_set_started"):
            return False

        def worker():
            with contextlib.suppress(Exception):
                engine.room_set_started(connecting)

        import threading

        threading.Thread(
            target=worker, name="started-report", daemon=True
        ).start()

        return True


    def _am_room_host(self):
        state = self._room_state_obj()

        return bool(state is not None and getattr(state, "is_host", False))


    def _am_room_owner(self):
        """
        我是不是房间的 **Owner**（建房的人）。

        设计基准 §A / AGENTS.md 规则 6：**只有 Owner 能切连接模式**；Host
        （非 Owner）无权。所以模式磁贴 / 提示的锁必须按 Owner 判，不能按 Host 判
        —— 按 Host 判会让一个非 Owner 的 Host 看到可点的磁贴，点下去被服务端
        `ERROR NOT_HOST` 拒掉（用户看到的是"能点但没反应"）。
        """
        state = self._room_state_obj()

        return bool(state is not None and getattr(state, "is_owner", False))


    def _confirm_mode_switch(self, old_mode, new_mode):
        """
        Second confirmation for the change that disconnects everyone.

        WHY IT IS NEEDED: switching transports restarts every tunnel, so every
        player in the world drops and has to reconnect. That is a small cost
        (they rejoin; nobody has to rebuild the room) but it is NOT something
        to do by mis-clicking a tile, so the host is asked explicitly and told
        what will happen.
        """
        label = {MODE_RELAY: "Relay 中转", MODE_STUN: "STUN 直连", MODE_AUTO: "AUTO 自动"}

        return messagebox.askokcancel(
            tr("切换连接模式"),
            tr(
                "将把房间的连接模式从「{0}」切换为「{1}」。\n\n"
                "切换房间模式会重建全部隧道，当前 Minecraft 连接会断开，"
                "正在游戏的玩家需要重新进入服务器；空闲成员保持空闲。"
                "AUTO 内部的直连与中继切换由后台完成，无需切换房间模式。\n\n"
                "确定要切换吗？"
            ).replace("{0}", tr(label.get(old_mode, old_mode)))
             .replace("{1}", tr(label.get(new_mode, new_mode))),
            parent=self.root,
        )


    def _apply_room_mode(self, mode):
        """
        Tell the relay, then rebuild every tunnel.

        WHY REBUILD: the transport is baked into each tunnel when it is
        established -- a relay tunnel is a TCP connection to the relay, a
        direct one is a punched UDP path. There is no in-place conversion, so
        the honest move is to drop them and let them come back on the new
        transport. The LOCAL side (proxy ports, Minecraft connection, room
        membership) is deliberately untouched: only the player-facing tunnels
        are rebuilt.
        """
        self.log(f"requested connection mode switch -> {mode}", kind="info")

        # ★ 用户 2026-10-02 现场：模式热切换后"客户端按钮状态被释放、但通道
        #   确实重建好了"，于是**下一次**切换因为 `self.running` 已经是 False
        #   而不再重建。根因是**同一个模式变化被重建了两次**：
        #     1. 本地这一次点击（`_apply_room_mode` -> `_rebuild_tunnels`）；
        #     2. 中继随后广播 `CONN_MODE_CHANGED` -> `_mode_rebuild_pending`
        #        -> 房间状态一到，`_maybe_rebuild_for_mode_change()` **又重建一次**。
        #   第二次重建的 stop 会把按钮放掉（running=False），而第一次的
        #   `rebuild_done` 已经把它补回来过 —— 净效果就是用户看到的"通了但按钮
        #   是释放的"，并且下一轮切换被 `if self.running` 挡住。
        #
        #   这里把"切换前我到底在不在连接"在**改动之前**就定死（local 侧没有
        #   中继的 `was_live=` 那么硬的事实，但此刻的 running 就是真相），并在
        #   本地重建开始时**清掉待重建标记** —— 一次模式变化只重建一次。
        was_live = bool(self.running)

        ok, message = self.engine.set_conn_mode(mode)

        if not ok:
            self.log(f"mode switch failed: {message}", kind="error")

            # Snap back to what the room actually has.
            actual = self._room_conn_mode() or MODE_RELAY
            self.mode_var.set(actual)
            self.config.mode = actual
            self._refresh_tiles()

            return

        self.config.mode = mode

        # Remember it, so anything rebuilt later uses the new mode.
        with contextlib.suppress(Exception):
            cfgmod.save_config(self.config)

        self._refresh_tiles()

        if was_live:
            self.log(
                "rebuilding the connection tunnels for the new mode; every "
                "client reconnects automatically on the new mode (nothing to do by hand)",
                kind="warn",
            )

            # 一次模式变化只重建一次：中继的广播随后到，不必再重建一遍
            # （它只负责把房间模式对齐）。
            self._mode_rebuild_pending = False
            # 模式**显式传入**（见 `_rebuild_tunnels` 的 P0 说明：不能到工作线程
            # 里再读一个每秒都会被改的共享字段）。
            #
            # 注意：模式切换**不**走 `_force_rebuild_links()`（用户 2026-09-30
            # 第 1 条只讲"触发自动重连后的行为"）：换模式本来就是"严格重建全部
            # 通路"，由 `_rebuild_tunnels` 自己的重建完成，这里保持原行为。
            #
            # ★ 静默（用户 2026-10-02）：房主点模式切换时，**所有人**（包括
            #   房主自己）的连接按钮都不该被释放、状态栏也不该掉到"未连接"。
            self._mode_rebuild_silent_next = True
            self._rebuild_tunnels(mode=mode)


    def _force_rebuild_links(self):
        """
        ★ 用户 2026-09-30 第 1 条：**自动重连一律重建通路**，不再判断原通路
        是否可复用。

        * 我是 host  -> 丢掉**所有** peer 的洞（重建到每个玩家的通路）；
        * 我是 player-> 只丢掉**到 host** 的那一条（其他玩家完全不受影响）；
        * 我不在房间里 / 没有打洞协调器 -> 什么都不做。

        **只影响调用它的那一次重连**：空闲的人不会走到这里（自动重连只由
        "我原本在连接"触发），所以"处于未连接态的玩家保持空闲"天然成立。
        """
        state = None

        with contextlib.suppress(Exception):
            state = self.engine.room_state()

        if state is None:
            return []

        if getattr(state, "is_host", False):
            peers = None
        else:
            host = str(
                getattr(state, "host_id", "")
                or getattr(state, "host_name", "")
                or ""
            ).strip()
            peers = [host] if host else []

        with contextlib.suppress(Exception):
            dropped = self.engine.rebuild_room_direct_links(peers)

            if dropped:
                self.log(
                    "[reconnect] forced path rebuild: "
                    + (
                        "every peer (I am the host)"
                        if peers is None
                        else f"only {', '.join(dropped)} (I am a player)"
                    ),
                    kind="warn",
                )

            return dropped or []

        return []


    def _rebuild_tunnels(self, mode=None, force_links=False):
        """
        Restart the connection so every tunnel is re-established.

        Runs on its own thread: stop+start is blocking (up to ~15s + ~20s) and
        must not freeze the window. The GUI is re-enabled when it finishes, on
        the GUI thread via the event queue.

        ============================================================================
          角色必须在【这里】重新决定，不能沿用表单默认
        ============================================================================
        用户实测（审计问题 3）：房主切模式时，重建把会话建成了 **Player** ——
        房间当场失去 host，所有人 `NO_HOST`。根因是这里调 `_collect_config()`
        没传 `role_override`，而它默认 `"player"`。

        所以身份和 `on_start` 走**同一个**规则：谁持有主机位置，谁就是 host。

        ============================================================================
          ★ 模式必须在【调用这一刻】定死，不能到工作线程里再读 `config.mode`
        ============================================================================
        用户实测（2026-09-28 P0）：房主热切换之后"连不上、隧道好像没有重建"。
        根因是**竞态**：`config.mode` 是界面线程每秒都会改的一个共享字段
        （`_refresh_room_ui` 把房间模式写进去），而重建的工作线程在几毫秒之后才
        读它。切模式那一瞬间中继的 `HOST_STATE mode=` 还没到，界面线程完全可能
        把 `config.mode` 又写回**旧模式**，于是新会话按旧传输协议建立：
        房间写着 STUN、会话还在走中继（或反过来），表现就是"隧道没有重建、
        怎么都连不上"。

        所以这里在**调用线程**上把目标模式快照下来（`mode` 参数，或房间当前
        模式），工作线程用 `config.mode = 这个快照` 显式覆盖 —— 重建结果只由
        "调用那一刻的事实"决定，不受之后任何一秒刷新影响。
        """
        import threading

        # 已经有一个重建在跑：绝不叠加第二次（审计 log2 里两次 teardown 交叠）。
        # 手动点模式、房主切模式、非房主自动重建，三条路都走这里。
        # ★ 静默标记在这里就**消费**掉，不论这次重建是否真的发生（2026-10-03
        #   致命回归）：一次被挡回的重建绝不能把标记留着 —— 那会让**之后某次
        #   完全无关的重建**也变成静默，界面被永远按在"在连接"（按钮不释放、
        #   停止无效、新房间连不上）。真正武装静默在下面通过全部 guard 之后。
        silent_next = bool(getattr(self, "_mode_rebuild_silent_next", False))
        self._mode_rebuild_silent_next = False

        if getattr(self, "_rebuild_in_flight", False):
            self.log(
                "a connection rebuild is already running; not starting another",
                kind="warn",
            )
            return

        # 角色在起线程之前就定好（同一个判定函数，和 on_start 共用一条规则）。
        role = self._safe_connect_role()

        # 目标模式也在起线程之前定死（见上面那段：这是 P0 的修复点）。
        target_mode = str(
            mode or self._room_conn_mode() or getattr(self.config, "mode", "")
            or MODE_RELAY
        )

        # 让本函数返回之后，每秒一次的自动重建判定**等这一次跑完**再重新评估。
        if silent_next:
            # 真正开始重建了才武装静默窗口（并且它有硬上限，见事件泵里的兜底）。
            self._mode_switch_silent = True
            self._mode_switch_silent_until = (
                time.monotonic() + MODE_SWITCH_SILENT_MAX_S
            )

        self._rebuild_in_flight = True

        def worker():
            # ★ 用户 2026-09-30 第 1 条：自动重连**一律重建通路**。
            #   放在这里（工作线程、stop 之前）而不是调用方那侧：
            #   * 离开 Tk 线程，和打洞循环的其它改写点同一条线程；
            #   * 在"停会话"之前把洞丢掉，紧接着的新会话就会重新打洞 ——
            #     绝不复用旧通路。
            if force_links:
                self._force_rebuild_links()

            clean = True

            try:
                clean = self.engine.stop_session(timeout=15)
            except Exception as e:
                self.engine._push("log", text=f"停止旧连接失败：{e}")
                clean = False

            if not clean:
                # 旧连接的收尾超时了。引擎已经走 abort() 把 socket 全部关掉
                # （否则新连接会在同一个本地端口上绑失败 —— 用户报的那一条），
                # 所以这里继续，但要说出来。
                self.engine._push(
                    "log",
                    text="旧连接收尾超时，已强制关闭它的 socket（本地端口已释放）",
                )

            try:
                # Reuse the real form-reading path, so the rebuilt session has
                # exactly the settings the user last committed (ports included)
                # -- not a hand-made subset that could drift.
                #
                # `role_override` is mandatory here: rebuilding as the wrong
                # role is the "host turns into a player and the room dies" bug.
                config = self._collect_config(role_override=role)

                # ★ P0（2026-09-28）：模式用**调用那一刻**的快照覆盖。这一行
                # 就是"房主切模式后新会话还走旧传输"的根治点 —— 界面线程随后
                # 任何一次把 config.mode 写回旧值都不会影响这一次重建。
                config.mode = target_mode

                self.engine.start_session(config, auth=self.auth, timeout=25)

                self.engine._push("log", text="已按新的连接模式重建隧道")
                # 让界面线程去重排房间级任务（tk 变量只能在界面线程碰）。
                self.engine._push("rebuild_done")
            except Exception as e:
                # 失败必须把界面恢复成"已停止"，否则用户看到的是"还在运行、
                # 但用什么都不通"的半死状态 —— 那正是他报的另一半问题。
                self.engine._push(
                    "log", text=f"按新模式重建失败：{e}（可手动重新连接）"
                )
                self.engine._push("rebuild_failed", text=str(e))
            finally:
                self._rebuild_in_flight = False

        threading.Thread(target=worker, name="mode-switch", daemon=True).start()


    IDENTITY_COOLDOWN_S = 5.0


    GUEST_NAME_PLACEHOLDER = "玩家"


    RECONNECT_BACKOFF_S = (0.5, 1.0, 2.0, 4.0, 8.0, 15.0)


    RECONNECT_WINDOW_S = 60.0


    RECONNECT_DEFER_LIMIT = 40


    def _reconnect_state_text(self):
        """界面四态：运行中 / 正在自动重连 / host 没开服 / 停止（终态原因）。"""
        phase = getattr(self, "_reconnect_phase", "")

        if phase == "reconnecting":
            left = max(
                0.0,
                getattr(self, "_reconnect_next_at", 0.0) - time.time(),
            )
            return f"正在自动重连（第 {self._reconnect_attempt} 次，{left:.0f}s 后）"

        if phase == "waiting_host":
            return "host 未开服（等待 host 开始连接）"

        # host 主动断开/释放之后：**不自动重连**，等 host 回来后用户手动点。
        if phase == "host_down":
            return "host 已断开连接（不自动重连，可手动「开始连接」）"

        if phase == "stopped":
            reason = getattr(self, "_reconnect_stop_reason", "") or ""
            return f"停止（{reason}）" if reason else "停止"

        return "运行中"


    @staticmethod
    def _is_no_host_reason(reason):
        """
        这个原因是不是"房间里暂时没有 host"。

        host 那边出事时，中继会给一个**可识别的原因**（ADDITIVE）：
        `HOST_LEFT`（闪断）/ `HOST_STOPPED`（主动停止开服）/ `HOST_RELEASED`
        （主动释放主机位）/ `NO_HOST`（房间里根本没有 host）。
        这个判据只用来回答"要不要走 host 那条路"；三条路各自怎么处理见
        `_on_host_gone()`（用户 2026-09-30 定稿）。
        """
        text = str(reason or "").upper()

        return (
            "HOST_LEFT" in text
            or "HOST_STOPPED" in text
            or "HOST_RELEASED" in text
            or "NO_HOST" in text
        )


    @staticmethod
    def _is_terminal_failure(reason):
        """
        这个原因是不是**终态**（绝不重连，只弹窗一次）。

        协调者口径的五类：用户点停止 / 主动退房、被踢、房间被销毁、
        版本不一致、登录失效（token 无效 / 被禁用）、免登录被要求登录。
        """
        text = str(reason or "").upper()

        for token in (
            "VERSION_MISMATCH",
            "KICKED",
            "ROOM REVOKED",
            "ROOM_REVOKED",
            "REVOKED",
            "AUTH_INVALID_TOKEN",
            "AUTH_FAILED",
            "AUTH_REQUIRED",
            "AUTH_INVALID",
            "AUTHENTICATION",          # "authentication required"（中继的英文说法）
            "NOT_LOGGED_IN",
            "ROOM_CLOSED",
            "OWNER_LEFT_ROOM",
        ):
            if token in text:
                return True

        return False


    def _reconnect_terminal(self, reason):
        """
        进入**终态**：不再重连，弹一次窗。

        终态一共五类（协调者口径）：用户点停止/主动退房、被踢、房间被销毁、
        版本不一致、登录失效（token 无效/被禁用）、免登录被要求登录。
        """
        self._reconnect_phase = "stopped"
        self._reconnect_stop_reason = str(reason or "")
        self._reconnect_attempt = 0
        self._reconnect_started_at = 0.0
        self._reconnect_next_at = 0.0

        if not str(reason or "").strip():
            return

        self.log(
            f"[reconnect] give up: terminal condition ({reason}) -- no retry",
            kind="warn",
        )


    def _reconnect_waiting_for_host(self, reason=""):
        """
        房间暂时**没有 host**：不重试，只等，按钮可点重试（用户口径）。

        这不是失败 —— 房主随时可能回来；玩家自己重试也没有意义
        （`ERROR NO_HOST` 已经由服务端说过一遍）。
        """
        self._reconnect_phase = "waiting_host"
        self._reconnect_attempt = 0
        self._reconnect_next_at = 0.0
        self.log(
            f"[reconnect] the host is not serving (reason={reason or 'no host'}); "
            f"not retrying by myself -- start again by hand once it is back",
            kind="warn",
        )


    def _on_core_host_gone(self, code="", reason=""):
        """
        【v3.9.16】核心层已经拆完会话之后发来的那一声（`kind="host_gone"`）。

        和房间事件（`host_stopped` 等）说的是同一件事，但**来源不同**：这条由
        `MCLinkEngine._on_host_gone_from_room` 直接 push，到达时会话与直连通路
        已经被停掉了。

        所以这里**无条件**处理，不看任何窗口 —— 它是"已经发生的事实"的通知，
        不是"要不要断开"的询问。
        """
        kind = {
            "HOST_LEFT": "left",
            "HOST_STOPPED": "stopped",
            "HOST_RELEASED": "released",
            "NO_HOST": "left",
        }.get(str(code or "").strip().upper(), "stopped")

        self._on_host_gone(kind, reason or code, forced=True)


    def _defer_host_gone(self, kind, reason=""):
        """
        【v3.9.16】模式重建窗口里到了一条 host-gone：**缓存**，等窗口结束补跑。

        ============================================================================
          和"丢弃"的区别就是这次修复的全部意义
        ============================================================================
        窗口期的语义本来是"这可能是我自己切模式时的 teardown，别把界面打乱"。
        但它被写成"什么都不做" —— 于是**真的** host 停服也被一起吞掉，中继
        已经把会话拆了，界面却还写着"已连接"（用户 10-03 实测）。

        现在只**推迟**界面的那一部分（提示 + 按钮重算）：
          * 断不断由核心层决定，早就断完了；
          * `_flush_pending_host_gone()` 在窗口一结束就补跑；
          * 还有 `HOST_GONE_DEFER_MAX_S` 兜底 —— 窗口万一没人清，也不会把
            这件事无限期压着（和 `MODE_SWITCH_SILENT_MAX_S` 同一条道理）。
        """
        self._pending_host_gone = {
            "kind": kind,
            "reason": reason,
            "at": time.monotonic(),
        }

        self.log(
            f"[host] host {kind} arrived during a mode-switch rebuild; the core "
            "has already torn the session down -- deferring only the UI part "
            f"(it runs as soon as the rebuild ends, {HOST_GONE_DEFER_MAX_S:g}s "
            "at the latest)",
            kind="warn",
        )


    def _flush_pending_host_gone(self):
        """
        【v3.9.16】窗口一结束（或兜底超时）就把缓存的那条 host-gone 补跑一次。

        每秒的 tick 里调，所以"窗口结束"最多晚一拍被发现 —— 而界面本来就
        是一秒一拍刷新的，这一拍用户看不出来。
        """
        pending = getattr(self, "_pending_host_gone", None)

        if not pending:
            return

        now = time.monotonic()

        # 还在窗口里？那就再等等（但绝不无限等，见下面的兜底）。
        windows_open = (
            getattr(self, "_rebuild_in_flight", False)
            or (
                getattr(self, "_mode_switch_silent", False)
                and now <= getattr(self, "_mode_switch_silent_until", 0.0)
            )
            or (
                getattr(self, "_mode_rebuild_pending", False)
                and now <= getattr(self, "_mode_rebuild_pending_until", 0.0)
            )
        )

        if windows_open and (
                now - float(pending.get("at", now)) < HOST_GONE_DEFER_MAX_S):
            return

        self._pending_host_gone = None

        self.log(
            "[host] the mode-switch window is over -- running the deferred "
            f"host-gone ({pending.get('kind')})",
            kind="warn",
        )

        self._on_host_gone(
            pending.get("kind", "stopped"),
            pending.get("reason", "") or "",
            forced=True,
        )


    def _on_host_gone(self, kind, reason="", forced=False):
        """
        host 那边出事的**唯一入口**（用户 2026-09-30 定稿，三种情况分开）。

        `forced=True` 表示"核心层已经拆完了，这里是补界面那一份"（v3.9.16）。
        和默认路径的区别只有一处：即使 `self.running` 已经是 False，也再调一次
        `engine.stop_session()` —— 它是幂等的，而"掉线处理先把 running 清成
        False、随后 host_stopped 才到"正是以前 `stop_session()` 被跳过的那条路。

        ============================================================================
          三种情况（`kind`）与两种身份（我是不是 host）
        ============================================================================
        * `"left"`     —— host **闪断**（中继 `HOST_LEFT`：他的成员连接断了，但
          身份/主机位还在宽限期里保留）。用户口径："迅速回来后可能只是丢了几个
          包…检测现行连接是否还可用，假如可用那就复用，假如不可用那就自动重连；
          原来房间里其他人处于连接态，那就直接重连，假如有人处于空闲态，那空闲态
          不重连。"
          -> 我们这边：**在连接态**（`running`）-> 走自动重连（`_rebuild_tunnels`
          会保住已经打通的洞、只重建死掉的，所以"能复用就复用"就是它）；
          **空闲态** -> 什么都不做，只重算按钮。
        * `"stopped"`  —— host **主动停止开服**（主机位保留、人还在房间）。
          -> 断开 + 重算按钮，**不自动重连、不弹窗**。
        * `"released"` —— host **主动释放主机位**。
          -> 断开 + 重算 + **弹窗「host 已断开连接」**，不自动重连。

        设计里**没有"房主已离线"这种说法**（用户 2026-09-30）：房主真离开
        房间 -> 房间直接销毁（`ROOM REVOKED`），根本走不到这里。所以这一条路
        上一句"房主…"都不会出现，"host 已断开连接"说的也**只是 host 这个角色**。

        我自己就是 host 时（身份在我手上），这几行说的是**我自己的**开服/主机位
        状态：只重算界面，不弹"host 已断开连接"（那是我自己刚做的动作）。
        """
        state = None

        with contextlib.suppress(Exception):
            state = self.engine.room_state()

        if state is not None and getattr(state, "is_host", False):
            # 我持有主机位：这是我的事，不是"别人家的 host 没了"。
            with contextlib.suppress(Exception):
                self._refresh_room_ui()

            return

        # ============================================================================
        #  ★ 用户 2026-10-03（重申；实测仍未生效的正是这一条）
        # ============================================================================
        #
        #  「host 断开连接时，房间内所有人一并一起断开连接且重算按钮状态」。
        #  旧行为：`left`（HOST_LEFT，host 闪断/掉线）在**连接态**走自动重连
        #  （`_trigger_auto_reconnect`）—— 界面于是继续显示"已连接"，可 host
        #  根本不在。用户现场："他断开连接，我这里应该也要断开连接并释放的，
        #  可是实际上并未，我这里还是已连接的状态"。
        #  现在：`left` 与 `stopped`/`released` 走**同一条断开路径**（下面那段），
        #  只是提示词不同；**不自动重连**，等 host 回来再手动点「开始连接」。
        host_left = kind == "left"

        # ---- 主动停止开服 / 主动释放主机位：断开 + 重算，**不自动重连** ----
        released = kind == "released"

        # 幂等：已经在这个状态里了（中继重复通知 / 停止与释放前后脚到），
        # 第二次只重算界面，不再停一遍、也不再弹一次窗。
        already_down = getattr(self, "_reconnect_phase", "") == "host_down"

        # ============================================================================
        #  ★ v3.9.16：`self.running` 不再决定"停不停"
        # ============================================================================
        #
        # 旧代码是"只有 `running` 为真时才去停"，也就是下面这种形状被反过来
        # 用成了闸门：
        # 而"掉线处理会先把 `self.running` 清成 False"（`_session_busy` 上方那段
        # 注释自己写着）—— 于是 PLAYER socket 掉线先被处理、`host_stopped` 后到
        # 时，这一步被整个跳过：只写状态、不断会话。用户看到的就是"host 断了、
        # 我这里还是已连接"。
        #
        # `stop_session()` 是幂等的（没有会话时立刻返回 True），所以无条件调
        # 一次没有副作用，代价可以忽略。
        if not already_down:
            with contextlib.suppress(Exception):
                self.engine.stop_session()

        self.running = False

        # 闸门：`host_down` 期间**不许自动重连**（`_can_auto_reconnect` 读它）。
        # 用户口径：这情况不要自动重连了，免得又弄出 BUG；host 回来之后由用户
        # 自己手动点「开始连接」。
        self._reconnect_phase = "host_down"
        self._reconnect_attempt = 0
        self._reconnect_started_at = 0.0
        self._reconnect_next_at = 0.0
        self._reconnect_stop_reason = ""

        self._host_down_hint(
            "host 已断开连接，host 位置暂无人认领"
            if released
            else ("host 已断开连接" if host_left else "host 已断开连接（未开服）")
        )
        self.log(
            "[reconnect] the host released the seat (HOST_RELEASED); stopping "
            "this session, clearing the old state, NOT retrying"
            if released
            else (
                "[reconnect] the host is gone (HOST_LEFT); stopping this "
                "session and recomputing the buttons, NOT retrying"
                if host_left
                else "[reconnect] the host stopped serving (HOST_STOPPED); "
                     "stopping this session, clearing the old state, NOT retrying"
            ),
            kind="warn",
        )

        with contextlib.suppress(Exception):
            self._refresh_room_ui()

        if released and not already_down:
            messagebox.showinfo(
                tr("host 已断开连接"),
                tr(
                    "host 已断开连接，host 位置暂无人认领。\n\n"
                    "如果还需要联机，请等有人认领 host 并开服之后，"
                    "再手动点「开始连接」。"
                ),
                parent=self.root,
            )


    def _host_down_hint(self, text):
        """把"host 那边怎么样"写到「开始连接」下面那行提示里（**不弹窗**）。"""
        with contextlib.suppress(Exception):
            self.connect_hint.configure(text=tr(text))


    def _on_host_left(self, reason=""):
        """兼容旧调用点（`room_lost` 的 no-host 原因）：按"host 闪断"处理。"""
        self._on_host_gone("left", reason)


    def _session_busy(self):
        """
        界面意义上的"这次连接还在（或者正在被重建）"。

        ============================================================================
          ★ 用户 2026-09-29 第 3 条：按钮状态必须跟着重连走
        ============================================================================
        用户原话：「直连换中转，会触发自动重连，但是按钮状态被错误的释放了；
        中转换直连，按钮状态未被释放，但是连接未自动重连」。

        根因就是这里：掉线处理会先把 `self.running` 清成 False（"世界那侧停了"），
        而按钮只看 `self.running` —— 于是**自动重连还在跑的那几秒**里，界面已经
        把「开始连接」放开了，看起来像"没在连"。反过来，重连成功之后没有任何
        地方把 `running` 恢复成 True，按钮就一直停在"已停止"（用户看到的另一半）。

        所以："忙碌" ≠ "running"：自动重连窗口内也算忙碌。
        """
        if self.running:
            return True

        # ★ 2026-10-03（分析结论 E1）：真正的"正在重建"是 `_rebuild_in_flight`
        #   这个**事实** —— worker 的 finally 一定会复位它，不会像别的开关那样
        #   被留下。把它算进来之后，模式热切换重建期间按钮**保持按下**（用户口径：
        #   按钮不许被释放），而静默窗口就不必再硬写 `running` 了。
        if getattr(self, "_rebuild_in_flight", False):
            return True

        return getattr(self, "_reconnect_phase", "") == "reconnecting"


    def _can_auto_reconnect(self):
        """现在允许自动重连吗（不满足就什么都不做）。"""
        phase = getattr(self, "_reconnect_phase", "running")

        # 正在自动重连的这一次**必须能接着跑**：掉线那一刻 `running` 会被清成
        # False（世界那侧停了），但重连本身还没结束 —— 拿 `running` 当闸门会把
        # 重连的第一次退避直接掐死（用户第 3 条的另一半）。
        if not self.running and phase != "reconnecting":
            return False

        if getattr(self, "_stopping_on_purpose", False):
            return False

        # `stopped` = 用户自己停的；`host_down` = host 主动停止/释放（用户
        # 2026-09-30 口径：**这情况不要自动重连**，等他回来手动点开始连接）。
        if phase in ("stopped", "host_down"):
            return False

        if getattr(self, "_rebuild_in_flight", False):
            return False

        return True


    def _reconnect_succeeded(self):
        """
        自动重连成功（会话重新起来了）：把界面恢复成"在连接"。

        以前**没有任何地方**做这件事，所以一次自动重连之后 `_reconnect_phase`
        永远停在 `reconnecting`、`running` 永远是 False —— 界面显示"已停止、可以
        点开始连接"，而连接其实已经回来了（用户第 3 条前半句）。
        """
        self._reconnect_phase = "running"
        self._reconnect_attempt = 0
        self._reconnect_started_at = 0.0
        self._reconnect_next_at = 0.0
        self._reconnect_stop_reason = ""
        self.running = True

        self.log("[reconnect] the session is back; buttons are live again", kind="ok")

        with contextlib.suppress(Exception):
            self._refresh_room_ui()

        with contextlib.suppress(Exception):
            self.root.after(0, self._refresh_room_ui)


    def _trigger_auto_reconnect(self, reason):
        """
        非用户意图的断开 -> 起一次（带退避的）会话重建。

        只做**一次调度**，真正的重建在后台线程里跑；到点没成功就进下一次退避，
        总窗口（`RECONNECT_WINDOW_S`）用完就停下 + 弹窗（用户口径）：
        「失败到底 → 停止（终态原因）」。
        """
        if not self._can_auto_reconnect():
            return False

        now = time.time()
        started = getattr(self, "_reconnect_started_at", 0.0) or 0.0

        if not started:
            started = now
            self._reconnect_started_at = started
            self._reconnect_attempt = 0

        if now - started > self.RECONNECT_WINDOW_S:
            reason_text = f"自动重连超时（{self.RECONNECT_WINDOW_S:.0f}s）"
            self._reconnect_terminal(reason_text)
            self._refresh_room_ui()
            messagebox.showwarning(
                tr("自动重连失败"),
                tr(
                    "已经重试了 {0} 次都没能恢复连接，已停止重试。\n\n"
                    "原因：{1}\n\n你可以手动点「开始连接」再试。"
                ).replace("{0}", str(self._reconnect_attempt)).replace(
                    "{1}", str(reason or "")
                ),
                parent=self.root,
            )
            return False

        attempt = int(getattr(self, "_reconnect_attempt", 0) or 0) + 1
        self._reconnect_attempt = attempt

        index = min(attempt - 1, len(self.RECONNECT_BACKOFF_S) - 1)
        delay = self.RECONNECT_BACKOFF_S[index]

        self._reconnect_phase = "reconnecting"
        self._reconnect_next_at = now + delay

        self.log(
            f"[reconnect] attempt {attempt} in {delay:.1f}s (reason={reason})",
            kind="warn",
        )

        self._refresh_room_ui()

        def worker():
            time.sleep(delay)

            if not self._can_auto_reconnect():
                return

            # 回到 Tk 主线程再动界面状态（后台线程碰 tkinter 会抛
            # "main thread is not in main loop"）。
            self.root.after(0, lambda: self._reconnect_session())

        threading.Thread(
            target=worker, name="reconnect-delay", daemon=True
        ).start()

        return True


    def _reconnect_session(self):
        """
        真正重建一次会话（**Tk 主线程**上调用 —— `root.after` 调度回来）。

        固定顺序（协调者口径）：
          ① `stop_session()` 并**确认本地代理端口真的释放**（失败就报错不往下）；
          ② 角色按"我是否持有 host 位"定（**绝不退化成 player**）；
          ③ 按**房间当前模式**（不是本机磁贴）重建；
          ④ 立刻重排房间级任务（NAT 监测 + 打洞）；
          ⑤ **不释放 host 位**（现行规则：只有"先停止再释放"能交出去）。

        这五条 `_rebuild_tunnels` 本来就全做到了（角色判定 / `_collect_config` /
        rebuild_done 后 `_sync_room_nat`）。所以这里只补一句可观测日志，然后调它
        —— 绝不另写一份平行实现。
        """
        # ================================================================
        #  ★ 另一次重建还在收尾时**不许静默放弃**
        # ================================================================
        #
        # `_rebuild_tunnels()` 遇到 `_rebuild_in_flight` 会直接返回。如果在这里
        # 什么都不做，这次自动重连就**停在"正在自动重连"上**：`running` 是 False、
        # `_reconnect_phase` 是 `reconnecting`，谁也不再来叫醒它（按钮永远不恢复、
        # 退避也不再继续）。所以这里自己等一小会儿再试（有界，免得卡死）。
        if getattr(self, "_rebuild_in_flight", False):
            deferred = int(getattr(self, "_reconnect_deferred", 0) or 0) + 1
            self._reconnect_deferred = deferred

            if deferred <= self.RECONNECT_DEFER_LIMIT:
                self.log(
                    f"[reconnect] another rebuild is still finishing; "
                    f"retrying in 0.5s ({deferred}/"
                    f"{self.RECONNECT_DEFER_LIMIT})",
                    kind="warn",
                )
                with contextlib.suppress(Exception):
                    self.root.after(500, self._reconnect_session)

                return

            self.log(
                "[reconnect] another rebuild never finished; giving up "
                "(manual reconnect needed)",
                kind="error",
            )
            self._reconnect_terminal("上一次重建一直没有结束")
            self._refresh_room_ui()

            return

        self._reconnect_deferred = 0

        role = "?"

        with contextlib.suppress(Exception):
            role = self._safe_connect_role()

        self.log(
            f"[reconnect] attempt {self._reconnect_attempt} rebuilding session "
            f"role={role} mode={self._room_conn_mode() or '?'}",
            kind="warn",
        )

        # ★ 用户 2026-09-30 第 1 条：自动重连**一律重建通路**（不再判断原通路
        #   是否可复用）：host 重建全部，player 只重建到 host 的那一条。
        #   真正的"丢洞"在 `_rebuild_tunnels` 的工作线程里做（见那里的说明）。
        self._rebuild_tunnels(
            mode=self._room_conn_mode() or None, force_links=True
        )


    def _is_guest(self):
        """现在是不是游客（免登录）身份。"""
        return self.auth is None


    def _remember_guest_name(self, name):
        """
        记住中继这次分配给我们的游客名（**登录会话级**，见 `__init__` 的说明）。

        只在**本来就没有游客名**时记住；已经有就不覆盖 —— 名字属于这次的登录
        会话，退房 / 被踢 / 房间被关之后再进房必须还是它（用户 2026-09-27 口径），
        换新名字只能由中继在**下一次连接**里给（那时我们已经重启或登出过了）。

        **绝不 `save_config`**：游客身份是一次性的，不许落盘。
        """
        name = str(name or "").strip()

        if not name or self._guest_name or not self._is_guest():
            return self._guest_name

        self._guest_name = name
        self.log(
            f"guest identity for this login session: {name} "
            f"(kept until logout or restart; never written to the config file)",
            kind="dim",
        )

        return self._guest_name


    def _forget_guest_identity(self, reason=""):
        """
        **销毁**游客身份：登出 / 换账号 / 重启软件才走这里。

        用户 2026-09-27 口径：游客身份是**登录会话级**的 ——
        退房 / 被踢 / 房间被关**不**销毁，只有"账号退出"才销毁
        （掉线重连导致登录状态丢失、换账号登录、重启软件）。

        顺带把可能残留在配置里的那个游客名清掉：老版本曾经把游客名写进
        `config.username`，那会让下次连接"看起来还是同一个人"。现在起
        `on_start` 也不会再写进去了，这里再兜一次。
        """
        had = bool(self._guest_name)

        self._guest_name = ""
        self._guest_token = ""

        # 引擎那份也要清：它下一次进房会把它放进握手行的 token 字段。
        with contextlib.suppress(Exception):
            self.engine._guest_token = ""

        if had:
            self.log(
                f"cleared the guest identity ({reason or 'login session ended'}); "
                f"the next connection gets a brand-new relay-assigned name",
                kind="warn",
            )


    def _configured_username(self):
        """
        这次连接该报给中继的名字：登录名 > 本次登录会话的游客名 > 表单/配置里的名字。

        游客名的用途就是"同一个登录会话里换个房间也不用换名字"；重启之后
        `_guest_name` 是空的（内存状态），于是又回到表单里的名字，由中继重新分配。
        """
        if self.auth is not None:
            return str(self.auth.username or "").strip()

        if self._guest_name:
            return self._guest_name

        return str(self.config.username or self.username_var.get() or "").strip()


    def _sync_identity(self):
        """
        房间已加入、且**登录状态真的变了**时，告诉中继"我这个 client_id 现在的
        显示名是这个"（`IDENTITY <用户名> <token> ver=<版本>`）。

        ============================================================================
          用户要求（ISSUES 问题 5）：玩家身份绑定设备
        ============================================================================

        用户原话：「玩家身份绑定设备，无论处于游客还是已登录的状态，加入房间的
        都是这个人，仅信息发生改变。」

        中继的显示名原来只在 CREATE/JOIN 握手那一刻取一次（`room.py::_request`），
        所以人在房间里登录之后名册里还是游客名（日志证据：craftlink-client.log
        12:22:59 `self_name='游客45teiy'`，直到重新握手才变 `root`）。

        三条硬规矩（照协调者给的格式）：
          1. **只在"房间已加入 + 登录/改名状态发生变化"时发一次** ——
             同一个 (用户名, token) 不重复发；再加 5 秒冷却兜底；
          2. 没有房间**不发**（本来就没有名册可更新）；
          3. 失败**绝不影响连接**：记一行英文日志 + 弹一句中文提示，
             名字保持原样（界面照旧显示中继给的那个）。

        跑在后台线程上：`room_send_identity` 要等中继应答（最多 8 秒），
        不能卡住 Tk 主循环。返回 True 表示这次真的发出去了。
        """
        state = self._room_state_obj()

        if state is None:
            return False

        username = self._configured_username()

        token = str(getattr(self.auth, "token", "") or "").strip()

        # 免登录 / 没名字：中继只按 client_id 记得我们，这条消息没有意义。
        if not username:
            return False

        signature = (username, token)

        if signature == getattr(self, "_identity_sent", None):
            return False

        now = time.time()

        if (
            now - getattr(self, "_identity_sent_at", 0.0)
            < self.IDENTITY_COOLDOWN_S
            and signature == getattr(self, "_identity_attempted", None)
        ):
            return False

        self._identity_attempted = signature
        self._identity_sent_at = now

        def worker():
            try:
                ok, message, name = self.engine.room_send_identity(
                    username, token
                )
            except Exception as e:  # noqa: BLE001 - 改名绝不能让界面崩
                ok, message, name = False, f"{type(e).__name__}: {e}", ""

            def report():
                if ok:
                    self._identity_sent = signature
                    self.log(
                        f"the relay accepted our display name: {name or username}",
                        kind="ok",
                    )
                    with contextlib.suppress(Exception):
                        self._refresh_room_ui()
                    with contextlib.suppress(Exception):
                        self._refresh_member_list(force=True)
                else:
                    # 老中继直接忽略这条行（超时）—— 说清楚，别让用户以为网络坏了。
                    self.log(
                        f"the relay did not confirm the display-name update "
                        f"({message}); keeping the current name",
                        kind="warn",
                    )
                    messagebox.showwarning(
                        tr("改名未生效"),
                        tr(
                            "房间里显示的名字没能更新：{0}\n\n"
                            "连接不受影响，房间里的名字会保持服务器给的那个"
                            "（可能是游客名）。"
                        ).replace("{0}", str(message or "")),
                        parent=self.root,
                    )

            self.root.after(0, report)

        threading.Thread(target=worker, name="identity", daemon=True).start()

        return True


    def _safe_connect_role(self):
        """
        这次连接该用哪个身份。同一个判定规则给 `on_start` / `_rebuild_tunnels` /
        模式自动重建用。

        **绝不静默降级**：判定失败（拿不到房间状态之类）也要留下一行日志，
        然后才退回 `config.role`（上次真正用过的身份），最后才退到 player。
        """
        state = self._room_state_obj()

        if state is None:
            # ================================================================
            #  ★ 没有房间时**不**去问 `_decide_connect_role`
            # ================================================================
            #
            # 用户日志（ISSUES 问题 1/4/6，两份日志各一次，都是退出流程）：
            #
            #     cannot decide the connection role
            #     (AttributeError: 'NoneType' object has no attribute 'is_host');
            #     falling back to the last used role
            #
            # `_decide_connect_role(state)` 的实现是 `state.is_host`，传 None
            # 进去必然 AttributeError。以前靠 `except` 兜住它 —— 能跑，但
            # 每次退出都往用户日志里写一条 "AttributeError"，看起来像故障，
            # 也让"角色判定失败"和"本来就没有房间"混成一件事（前者值得报警）。
            # 这里把"没有房间"当成一条正常分支，日志说人话、结论仍是回落。
            role = str(getattr(self.config, "role", "") or "")

            if role in ("host", "player"):
                self.log(
                    f"not in a room; keeping the last used role ({role})",
                    kind="warn",
                )
                return role

            self.log(
                "not in a room and no previously used role; using player",
                kind="warn",
            )
            return "player"

        try:
            role, _explanation = self._decide_connect_role(state)

            if role in ("host", "player"):
                return role
        except Exception as e:
            self.log(
                f"cannot decide the connection role ({type(e).__name__}: {e}); "
                f"falling back to the last used role",
                kind="warn",
            )

        previous = str(getattr(self.config, "role", "") or "")

        if previous in ("host", "player"):
            return previous

        self.log(
            "no room state and no previously used role; rebuilding as player",
            kind="warn",
        )

        return "player"


    def _decide_connect_role(self, state):
        """
        决定**这次连接用哪个身份**，以及要不要给用户一句解释。

        ============================================================================
          铁律：不再有任何"身份选择器"参与这件事（用户要求把它删掉了）
        ============================================================================

        规则只有一条：

            持有主机位置 = 以【主机 Host】连接（你来开世界）
            否则         = 以【玩家 Player】连接（你进别人的世界）

        用户看得到的三个数字（房主局域网端口 / 玩家本地端口 / 人数上限）是
        **基准配置**，任何时候都能改、和身份无关；真正选哪个端口由这里决定。

        返回 (connect_role, 解释文案或 None)。
        """
        connect_role = "host" if state.is_host else "player"

        if connect_role == "host":
            return connect_role, None

        # 不是主机 —— 如果房间里已经有别人在当主机，顺口说一句为什么。
        if state.host_claimed:
            return connect_role, (
                "这个房间已经有主机了，你将按【玩家 Player】连接。"
                "想自己开世界请换一个房间号。"
            )

        return connect_role, None


    def on_start(self):
        if self.running:
            return

        # ============================================================================
        #  没有"身份选择器"了 —— 身份由「谁持有主机位置」决定
        # ============================================================================
        #
        # 用户要求把身份选择器整个删掉（它会干扰操作栏、默认值还会让人点两次
        # 才有反应）。所以这里不再读任何"身份"控件，只看一件事：
        # **我持有主机位置吗？** 持有 = 以主机身份开世界，否则 = 以玩家身份进房。
        #
        # 三个数字（房主局域网端口 / 玩家本地端口 / 人数上限）在「基准配置」里
        # 随时可改，但**必须是按过「确定」的值** —— 连接时只读 _committed。

        # ---- V3: you must be IN the room first ----
        state = self.engine.room_state()

        if state is None:
            if messagebox.askokcancel(
                "还没有加入房间",
                f"你还没有进入房间 {self._current_room_name()}。\n\n"
                "要以主机身份开世界，需要先创建房间；"
                "要进别人的房间，需要先加入。\n\n现在就去吗？",
                parent=self.root,
            ):
                self.on_join_room()

            return

        if state.revoked:
            messagebox.showwarning(
                "房间已被关闭",
                f"{state.revoked_reason}\n\n请重新创建或加入房间。",
                parent=self.root,
            )
            return

        # ============================================================
        #  §B（设计基准，AGENTS.md 规则 6）：有 Host，但 Host 还没开始连接
        # ============================================================
        #
        # 基线原文："有 Host 但 Host 未开始连接：其他玩家点开始连接**不得进入
        # 正式 P2P 流程**，提示「Host 未连接，请等待 Host 开服」。"
        #
        # 打洞那一层（`punch_flow.select_targets`）已经会返回空目标把它挡住，
        # 但那只是"没打洞"；用户看到的是**界面毫无反应**。所以这里先给提示，
        # 两个地方共用同一个判据（`punch_flow.host_ready`），不可能互相矛盾。
        #
        # `host_ready() is None` = 判断不了（老中继没有 `started=` / 我不在房间 /
        # 没有 Host）-> **不许当成"没开服"**，否则老中继上谁都连不了。
        if not state.is_host and state.host_claimed:
            from mclink.core import punch_flow

            if punch_flow.host_ready(state) is False:
                messagebox.showwarning(
                    tr("Host 未连接"),
                    tr("Host 未连接，请等待 Host 开服"),
                    parent=self.root,
                )
                self.connect_hint.configure(text=tr("Host 未连接，请等待 Host 开服"))
                self.log(
                    "the host has claimed the slot but has not started yet; "
                    "refusing to start a session",
                    kind="warn",
                )
                return

        # ---- 房间有主机位置，但不在我手上 → 先问要不要认领 ----
        #
        # 用户要求删掉身份选择器，但**没有**要求删掉"认领主机身份"这件事：
        # 「认领主机身份 (host)」按钮是显式操作，它决定的是谁负责开世界，
        # 和选择器是两回事（一个决定事实，一个只是界面偏好）。
        if not state.is_host and not state.host_claimed:
            if not self._capacity_within_server():
                return

            # ============================================================
            #  ★ host 那边没开服：玩家侧不自动重试，给清晰提示
            # ============================================================
            #
            # 用户口径（2026-09-30）：host 主动断开/释放之后**不要自动重连**，
            # 等 host 回来之后用户自己手动点「开始连接」。这里只管"还没开服"：
            # 房间还在（host 位可能有人但没在开服，或者暂时没人认领）。
            if getattr(self, "_reconnect_phase", "") in ("waiting_host", "host_down"):
                messagebox.showwarning(
                    tr("host 未开服"),
                    tr(
                        "host 那边现在没有开服，连上也没用。\n\n"
                        "房间还在 —— 等 host 开始连接之后，你再手动点"
                        "「开始连接」。"
                    ),
                    parent=self.root,
                )
                self.connect_hint.configure(text=tr("host 已断开连接（未开服）"))
                return

            if not messagebox.askokcancel(
                "认领主机身份 (host)",
                "这个房间还没有主机。要以主机身份开始，需要先认领主机身份。\n\n"
                "现在认领吗？",
                parent=self.root,
            ):
                return

            ok, message = self.engine.claim_host()

            if not ok:
                messagebox.showerror("认领失败", message, parent=self.root)
                self.log(f"failed to claim the host slot: {message}", kind="error")
                return

            self.log(f"host slot claimed: {message}", kind="ok")
            self._refresh_room_ui()
            state = self.engine.room_state() or state

        # ============================================================================
        #  连接身份：持有主机位置 = 开世界；否则 = 进别人的房间
        # ============================================================================
        connect_role, explanation = self._decide_connect_role(state)

        if explanation:
            self.log(explanation, kind="warn")

        try:
            config = self._collect_config(role_override=connect_role)
        except ValueError as e:
            messagebox.showerror(tr("参数错误"), tr(str(e)))
            self.log(f"invalid parameters: {e}", kind="error")
            return

        # ---- V2: an account may be required before hosting or joining ----
        if not self.require_login():
            self.log("not logged in; connection cancelled.", kind="warn")
            return

        # ============================================================================
        #  账号是**可选**的：免登录（游客）也必须能连
        # ============================================================================
        #
        # 用户实测（审计问题 1）：web 面板把「必须登录」关掉之后，未登录加入房间、
        # 点「开始连接」只会反复看到「这个房间已经有主机了…」，永远进不去，而且
        # **一条日志都没有**。根因就是这里原来无条件写
        #
        #     config.username = self.auth.username
        #
        # 而 `require_login()` 在免登录时返回 True（那时根本没有 auth 对象，
        # 见 6293 行），于是 `self.auth.username` 抛 AttributeError；这一行又不在
        # 任何 try 里，Tk 回调把异常吞掉 —— 不落日志、不弹窗、不连接。
        #
        # 现在：有账号就用账号名（它是权威的）；没有账号就保留配置里 / 中继分配
        # 的游客名。并且整段包进 try，未来任何异常都会变成一行日志，而不是消失。
        try:
            if self.auth is not None:
                config.username = self.auth.username
                self.username_var.set(config.username)
            elif self._guest_name:
                # 同一个登录会话里换个房间：报**中继已经分配给我的那个游客名**，
                # 所以中继认得出"还是这个人"，名字不变（用户 2026-09-27 口径：
                # 退房 / 被踢 / 房间被关都不销毁游客身份）。
                config.username = self._guest_name
                self.log(
                    f"starting as the same guest for this login session "
                    f"({self._guest_name}); it is not written to the config file",
                    kind="dim",
                )
            else:
                # 只有"这一次登录会话还没拿到游客名"（首次连接 / 重启之后）
                # 才用表单里的名字 —— 中继会分配一个全新的「游客xxxxxx」。
                config.username = config.username or self.username_var.get().strip()
                self.log(
                    "starting without a login (guest); the relay assigns a "
                    "brand-new guest name for this connection",
                    kind="warn",
                )
        except Exception as e:
            self.log(
                f"account handling failed ({type(e).__name__}: {e}); "
                f"connecting as a guest with the configured display name",
                kind="error",
            )

        # 游客名字**绝不写进配置文件**（一次性身份）。`config.username` 会被
        # `validated()` 兜成占位符「玩家」，落盘的是那个占位符，不是游客名。
        saved_username = config.username

        if self.auth is None and self._guest_name:
            config.username = self.GUEST_NAME_PLACEHOLDER

        self._config_file_path = cfgmod.save_config(config)

        if self.auth is None and self._guest_name:
            config.username = saved_username

        self.log("=" * 46, kind="dim")
        self.log(
            f"starting {config.role.upper()} mode | "
            f"room {config.room} | "
            f"account {config.username} | "
            f"server {config.server_host}:{config.server_port}",
            kind="info",
        )

        try:
            self.engine.start_session(config, auth=self.auth)
        except Exception as e:
            self.log(f"start failed: {e}", kind="error")

            if self._refuse_outdated(str(e)):
                return

            messagebox.showerror(tr("启动失败"), tr(str(e)))
            return

        # ★ 用户 2026-10-03（host 交接现场）：这里是一次**全新连接**的开始，
        #   上一次 host 走掉时留下的陈旧阶段（host_down / waiting_host）以及
        #   模式重建的残留标记都必须清掉 —— 否则用户重新认领 host、重新连上之后，
        #   那个陈旧阶段会让**后续每一次重建**都被判成"host 不在"（现场表现：
        #   热切换不自动重连 + 按钮被释放）。不管 host 传了几手，流程都必须一样。
        self._reconnect_phase = "running"
        self._mode_switch_silent = False
        self._mode_rebuild_pending = False
        self._mode_rebuild_from = None
        self.running = True
        self._set_enabled(self.start_button, False)
        self._set_enabled(self.stop_button, True)
        self.delete_room_button.configure(state="normal")

        # ★ 用户 2026-09-28 第 5 条：告诉中继"我点了开始连接"（STARTED 1）。
        # 这样 Host 的目标表（`started=`）当场就有我，不用等游戏连接上来。
        self._report_started(True)

        # 主机位置相关的按钮要跟着变（连接跑起来时"释放身份"必须变灰）。
        self._refresh_room_ui()

        if config.role == "player":
            self.log(
                f"connect in Minecraft to 127.0.0.1:{config.local_port}",
                kind="ok",
            )


    def on_stop(self):
        # ★ 2026-10-03（分析结论 E4）：以前 `if not self.running: return` ——
        #   自动重连期间 `running=False` 但按钮是**可用**的（`_session_busy()`），
        #   于是"点停止毫无反应"。现在只要真的忙（在跑或正在重建/重连）就处理。
        if not self._session_busy():
            return

        # 终止在飞的重连/静默窗口，否则下一次刷新又把按钮按回去。
        self._reconnect_phase = "stopped"
        self._reconnect_stop_reason = "用户停止"
        self._reconnect_attempt = 0
        self._reconnect_started_at = 0.0
        self._reconnect_next_at = 0.0
        self._mode_switch_silent = False

        self.log("stopping ...", kind="warn")
        self.stop_button.configure(state="disabled")

        self.root.update_idletasks()

        clean = True

        try:
            # 注意：停止【不再】释放主机位置（用户的要求）。位置是你主动
            # 认领的，也只有「释放主机身份」能交出去 —— 停止只是不开服了，
            # 房间列表里那一列仍然要显示你的名字。
            # ★ v3.9.17：这是**用户点的停止**，必须让中继知道（发 `STOPPING`），
            #   它才能和"掉线/崩溃/模式重建"分开。别的调用点一律不传。
            clean = self.engine.stop_session(graceful=True)
        except Exception as e:
            self.log(f"error while stopping: {e}", kind="warn")
            clean = False

        self.running = False

        # ★ 用户 2026-09-28 第 5 条：告诉中继"我停了"（STARTED 0）—— 房间里
        # 有这个人，但他没在连接；Host 那边必须能区分（「等待对端连接…」）。
        self._report_started(False)

        # 房间级任务（NAT 监测 + 打洞）**立刻**停，不等下一个 UI tick：
        # 它们各自持有 UDP socket，留着就会和下一次连接抢端口、甚至继续打洞
        # （用户报的"断开之后无法再建立新的连接"里就有这一份）。
        with contextlib.suppress(Exception):
            self.engine.watch_room_nat(False, log=self.log)

        if not clean:
            # 拆除没在预算内完成 —— 强制关掉了所有 socket。用户应该知道
            # 这件事发生过（日志里能看到），但界面照常可用：端口已经交出来了。
            self.log(
                "old connection did not stop in time; its sockets were "
                "force-closed (the local port is free again)",
                kind="warn",
            )

        self.connect_hint.configure(text="")
        self.log("stopped", kind="info")

        # 所有按钮状态由这一个函数统一恢复（含开始/停止、认领/释放）。
        self._refresh_room_ui()
        self._refresh_room_list_soon()


    def on_copy_log(self):
        text = self.log_text.get("1.0", "end").strip()

        self.root.clipboard_clear()
        self.root.clipboard_append(text)

        self.log("log copied to the clipboard", kind="dim")


    def on_save_log(self):
        path = filedialog.asksaveasfilename(
            title=tr("保存日志"),
            defaultextension=".txt",
            initialfile=time.strftime("mclink-log-%Y%m%d-%H%M%S.txt"),
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
        )

        if not path:
            return

        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(self.log_text.get("1.0", "end"))
            self.log(f"log saved: {path}", kind="ok")
        except Exception as e:
            messagebox.showerror(tr("保存失败"), tr(str(e)))


    def on_clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")


    def on_open_config_dir(self):
        path = (
            os.path.dirname(self._config_file_path)
            if self._config_file_path
            else cfgmod.config_dir()
        )

        with contextlib.suppress(Exception):
            os.makedirs(path, exist_ok=True)

        try:
            if os.name == "nt":
                os.startfile(path)  # noqa: S606
            elif sys.platform == "darwin":
                os.system(f'open "{path}"')  # noqa: S605
            else:
                webbrowser.open(f"file://{path}")
        except Exception as e:
            messagebox.showinfo("配置目录", f"{path}\n\n({e})")


    def _report_room_emit_errors(self):
        """
        房间事件发不出去时，在日志里说清楚（只报一次）。

        这类错误以前是被静默吞掉的，代价是"房间已经没了，界面还蒙在鼓里"。
        宁可刷一条日志，也不要再出现那种情况。
        """
        session = self.engine.room_current()

        if session is None:
            return

        errors = list(getattr(session, "emit_errors", []) or [])

        if not errors or errors == self._last_emit_errors:
            return

        self._last_emit_errors = errors
        self.log(
            "房间事件没有送达界面（已按最新状态重画）：" + errors[-1],
            kind="warn",
        )


    def _maybe_auto_refresh_room_list(self):
        """
        房间列表的【自动刷新】（用户要求"实时刷新"，不是手动点）。

        每 ROOM_LIST_REFRESH_MS 拉一次。三条闸门，避免无谓的网络流量：
          * 窗口不可见（最小化/切到别的标签）时不拉
          * 上一次请求还没回来时不拉（_room_list_busy）
          * 没填服务器地址时不拉
        失败只记一次日志，不会每 1.5 秒刷屏。
        """
        if self._room_list_busy:
            return

        if time.monotonic() - self._room_list_fetched_at < ROOM_LIST_REFRESH_MS / 1000:
            return

        if not self.host_var.get().strip():
            return

        with contextlib.suppress(Exception):
            # 窗口最小化 / 被隐藏时没必要轮询。
            if self.root.state() == "iconic":
                return

        self.on_rooms_refresh()


    def _tick(self):
        try:
            # 界面正在重建（切换语言/主题）时先让路：这一拍里的控件可能
            # 已经被销毁，刷新它们只会抛异常。
            if getattr(self, "_rebuilding", False):
                return

            self._drain_events()
            self._append_logs()

            # ★ v3.9.16：窗口一结束就把推迟的 host-gone 补跑（被推迟 ≠ 被丢弃）。
            self._flush_pending_host_gone()

            # ============================================================================
            #  ★ 用户 2026-09-30（第三轮第 7 条）：起跑倒计时要有"毫秒感"
            # ============================================================================
            #
            # 状态卡是每 `UI_REFRESH_MS`（1 秒）重画一次的，而约定的喷包时刻只提前
            # 1.5 秒公告 —— 1 秒一拍就只剩一格可看，用户要的 `1.5s -> 1.2s -> ...`
            # 根本走不起来。
            #
            # 所以倒计时期间把**逐目标那一块**（纯文字、就地改 StringVar，不建控件
            # 也不抢焦点）按本 tick（150ms）重画一次；倒计时一结束就回到 1 秒一拍。
            # 只重画打洞那一块，不碰别的、也不触发任何引擎动作。
            self._refresh_countdown_rows()

            # ★ 用户 2026-10-01 第 3 条：和成员列表**同频**上报我自己的连接状态
            #   （纯显示：中继存下来、填进 HOST_STATE 的 `conn=`）。
            self._report_local_conn_state()

            now = time.time()
            if now - self._last_ui_refresh >= UI_REFRESH_MS / 1000:
                self._last_ui_refresh = now
                self._refresh_status()

                # ============================================================
                #  房间界面每秒自愈一次（事件丢了也不能卡在幽灵状态）
                # ============================================================
                #
                # 事件是"推送"：正常情况下一秒内就有，但推送是可以丢的
                # （emit 约定不符、事件队列满、界面正在重建……）。
                # 曾经就丢过一次：房间被服务器关掉，界面完全不知道，按钮一直
                # 停在"还在房间里"。所以这里每秒按引擎的**当前状态**重画一遍 ——
                # 丢事件最多让界面晚一秒，绝不会停在那儿。
                self._refresh_room_ui()
                self._report_room_emit_errors()

            self._maybe_auto_refresh_room_list()

        except Exception as e:  # never let the UI loop die
            with contextlib.suppress(Exception):
                self.log(f"UI refresh error: {e}", kind="warn")

        finally:
            self.root.after(TICK_MS, self._tick)


    def _drain_events(self):
        for event in self.engine.drain_events():
            kind = event.get("kind")

            if kind == "log":
                self.log(event.get("text", ""), kind="normal")

            elif kind == "event":
                self._handle_core_event(
                    event.get("name", ""),
                    event.get("data", {}) or {},
                )

            elif kind == "host_gone":
                # ============================================================
                #  ★ v3.9.16：核心层自己拆完之后的那一声"已经拆了"
                # ============================================================
                #
                # 它和房间事件（`host_stopped` 等）说的是同一件事，但来源不同：
                # 这条由 `MCLinkEngine._on_host_gone_from_room` 直接 push，
                # 此时会话与直连通路**已经被核心层停掉了**。
                #
                # 界面收到它只做两件事：对齐状态、放掉按钮。就算它和房间事件
                # 前后脚都到了也没关系 —— `_on_host_gone` 是幂等的。
                self._on_core_host_gone(
                    event.get("code", "") or "",
                    event.get("reason", "") or "",
                )

            # ★ 用户 2026-10-02：模式热切换的重建期间**界面不许掉到"未连接"**。
            #
            # `_rebuild_tunnels` 内部会 stop_session，停止那条路会把 `running`
            # 清掉、把阶段写成 stopped —— 玩家于是看到"按钮被释放 + 状态栏未连接"，
            # 可一两秒后隧道已经按新模式建好、游戏也能进了（用户现场）。这里在
            # 事件泵每轮之后把界面按回去，直到 `rebuild_done` / `rebuild_failed`。

            elif kind == "rebuild_done":
                # 重建完成：立刻按新模式重新对齐 NAT 监测 / 打洞，别让新会话
                # 在"洞还没开始打"的窗口里干等（用户报的换模式后连不上）。
                #
                # ★ 如果这一次重建属于**自动重连**，那它成功了：把界面从
                # "正在自动重连" 恢复成"在连接"（否则按钮永远停在已停止）。
                # ★ 用户 2026-10-03：`host_down` 可能是**交接之前**留下的陈旧状态。
                #   只有当"我此刻不是 host 且房间里确实没人认领 host"时，才保持断开；
                #   否则（我已经认领了 host / 别人已认领）必须先清掉它，再走正常点亮。
                _state = None

                with contextlib.suppress(Exception):
                    _state = self.engine.room_state()

                _host_present = bool(
                    getattr(_state, "is_host", False)
                    or getattr(_state, "host_claimed", False)
                )

                if (
                    getattr(self, "_reconnect_phase", "") == "host_down"
                    and _host_present
                ):
                    self._reconnect_phase = "running"

                if getattr(self, "_reconnect_phase", "") == "host_down":
                    # ★ 2026-10-03（分析实测的最后一条洞）：host 已经走了
                    #   （`_on_host_gone` 把阶段置成 host_down），这时**任何**
                    #   早先就在飞的重建完成，都不许把界面重新点亮成"已连接"。
                    #   否则用户看到的就是"host 断了、我这里还是已连接"。
                    self.running = False
                    self.log(
                        "[host] a rebuild finished while the host is gone "
                        "(host_down); keeping this side disconnected and the "
                        "buttons released",
                        kind="warn",
                    )
                elif getattr(self, "_reconnect_phase", "") == "reconnecting":
                    self._reconnect_succeeded()
                elif not self.running:
                    # ============================================================
                    #  ★ 用户 2026-09-29 第 3 条：重建成功 = 会话又活了，
                    #    界面必须回到"在连接"
                    # ============================================================
                    #
                    # `_rebuild_tunnels` 成功只说明 `start_session` 没抛异常；
                    # 它**不会**动 `self.running`。于是只要重建**触发那一刻**
                    # 之后 `running` 变成了 False（掉线处理 / 重连窗口 / 别的
                    # 路径），重建成功之后界面就停在"已停止"：
                    #
                    #   * 用户看到的：**状态机显示已连接，按钮却是释放的**；
                    #   * 更糟的是 `_sync_room_nat()` 的开关是
                    #     `stun_room and self.running` —— `running=False` 会把
                    #     刚重建起来的打洞**立刻关掉**，而且每秒都关，
                    #     于是「中转换 STUN 之后根本没有开始打洞」。
                    #
                    # 所以重建成功就是"在跑"这个事实的权威来源：把 running
                    # 补回来，再让 `_sync_room_nat()` 去按新模式拉起打洞。
                    self.running = True
                    self.log(
                        "[mode] the rebuild finished and the session is live "
                        "again; connection buttons re-armed and the punch is "
                        "started for the new mode",
                        kind="warn",
                    )

                # 静默期到此结束：新模式已经起来了，界面按正常状态显示。
                self._mode_switch_silent = False

                with contextlib.suppress(Exception):
                    self._sync_room_nat()

            elif kind == "rebuild_failed":
                # 失败也结束静默（否则界面卡在"在连接"）。
                self._mode_switch_silent = False
                # 换模式重建失败 / 自动重连重建失败。
                #
                # 用户口径（2026-09-27，docs/AUTO-RECONNECT-PLAN.md）：
                # "换模式重建失败→纳入同一套自动重试（不再只留一句
                # 可手动重新连接）"。但也**绝不**把界面卡在"看起来在跑、
                # 其实什么都没连上"：重试窗口内保持 running（按钮维持现状），
                # 到点没成功就进终态 + 弹窗 + 按钮恢复。
                reason = str(data.get("text", "") or "")

                if self._is_terminal_failure(reason):
                    self._reconnect_terminal(reason)
                    self.running = False
                    self._refresh_room_ui()
                    self.connect_hint.configure(text=tr("重建失败，可以重新连接"))
                else:
                    self._trigger_auto_reconnect(
                        f"rebuild failed: {reason}" if reason else "rebuild failed"
                    )
                    self.connect_hint.configure(
                        text=tr("重建失败，正在自动重试 ...")
                    )

            elif kind == "snapshot":
                self.last_snapshot = event.get("data", {}) or {}

            elif kind == "state":
                state = event.get("state")

                if state == STATE_ONLINE:
                    # 身份不再来自选择器（已删除）：由本次会话真正的角色决定。
                    session_role = getattr(self.config, "role", "") or ""

                    if session_role == "host":
                        self.log("host ready; waiting for players to join.", kind="ok")
                    else:
                        self.log("ready; you can enter Minecraft now.", kind="ok")

                elif state == STATE_ERROR:
                    reason = event.get("reason", "") or ""

                    # 版本门优先：这条错误要变成"需要更新"的弹窗 + 停会话，
                    # 而不是日志里一行普通错误。
                    if self._refuse_outdated(reason):
                        return

                    self.log(f"error: {reason}", kind="error")


            # ★ 2026-10-03（分析结论 E3）：静默窗口**只**负责"重建期间别把界面
            #   画成掉线"，绝不硬写 `running` / `_reconnect_phase` ——
            #   旧写法每 150ms 强写一次，会覆盖用户的**合法释放**（手动停止、
            #   被踢、host 掉了），而且到点只清标记、不复原事实，于是按钮
            #   永远按着、停止也无效。
            #   现在按钮的"按下"由 `_session_busy()` 按事实（running 或真的
            #   有重建在跑）重算；这里只负责到点/离房时收尾。
            if getattr(self, "_mode_rebuild_pending", False) and time.monotonic() > getattr(
                self, "_mode_rebuild_pending_until", 0.0
            ):
                self._mode_rebuild_pending = False
                self.log(
                    "[mode] the 'waiting for the new room mode' marker expired "
                    "(the broadcast never arrived); a real host stop is no "
                    "longer masked by it",
                    kind="warn",
                )

            if getattr(self, "_mode_switch_silent", False):
                expired = time.monotonic() > getattr(
                    self, "_mode_switch_silent_until", 0.0
                )
                no_room = self.engine.room_current() is None
                done = not getattr(self, "_rebuild_in_flight", False)

                if expired or no_room or done:
                    self._mode_switch_silent = False

                    if not getattr(self, "_rebuild_in_flight", False):
                        if not self.running and getattr(
                            self, "_reconnect_phase", ""
                        ) == "reconnecting":
                            # 重建结束了、会话也没起来 -> 如实回到停止态。
                            self._reconnect_phase = "stopped"

                        with contextlib.suppress(Exception):
                            self._refresh_room_ui()

                    self.log(
                        "[mode] silent-rebuild window closed "
                        f"({'expired' if expired else ('left the room' if no_room else 'rebuild finished')}); "
                        "buttons and status are recomputed from the real state",
                        kind="warn",
                    )


    def _refuse_outdated(self, text):
        """
        服务端说两边版本不一致时：弹「需要更新」并且**拒绝运行**。

        这里不是"提示一下然后继续用旧版" —— 旧版会带着已删除/已改名的
        控制字段继续打，制造出比报错更难查的怪状态。所以：不重连、不
        降级，把已经起来的会话停掉，只留一个明确的更新提示。

        返回 True 表示"这条错误已经被版本门吃掉了"，调用方不要再当普通
        错误处理（否则用户会同时看到两个弹窗）。
        """
        if not proto.is_version_mismatch(text):
            return False

        self.log(f"version rejected: {text}", kind="error")

        if getattr(self, "_version_popup_shown", False):
            self._stop_after_version_reject()
            return True

        self._version_popup_shown = True

        server, client = proto.version_mismatch_peers(text)

        self._stop_after_version_reject()

        # 下载地址放在 settings 里（换域名只改一处）。
        url = cfgmod.update_url(getattr(self.config, "update_url", None))
        from urllib.parse import urlsplit
        site = urlsplit(url).netloc

        # 日志正文英文（日志是分析材料，不跟界面语言走）；中文提示在那个
        # 弹窗里。
        self.log(f"version mismatch; update from {url}", kind="error")

        # 用 askyesno 而不是 showerror：用户的原话是"用户自己去网站下载"，
        # 那就顺手给一个能直接打开下载页的按钮，省得手打地址。
        open_site = messagebox.askyesno(
            "需要更新",
            "客户端与服务器的版本不一致，无法连接。\n\n"
            f"服务器版本：{server or '未知'}\n"
            f"本机版本：{client or '未知'}\n"
            f"本机程序版本：{cfgmod.APP_VERSION}\n\n"
            f"请到官网下载最新版：{site}\n"
            f"（{url}，HTTPS 443）\n\n"
            "现在打开下载页面吗？",
            parent=self.root,
        )

        if open_site:
            with contextlib.suppress(Exception):
                webbrowser.open(url)

        return True


    def _stop_after_version_reject(self):
        """版本门命中后把会话收干净（不释放主机身份、不重连）。"""
        if not self.running:
            return

        try:
            self.engine.stop_session()
        except Exception:
            pass

        self.running = False
        self._version_popup_shown = True

        try:
            self.connect_hint.configure(text="版本不一致，已停止")
            self._refresh_room_ui()
        except Exception:
            pass


    def _handle_core_event(self, name, data):
        # 版本门可以在任意一条 relay 回复里出现（HOST_TUNNEL / PLAYER 握手
        # 被拒时就是一条普通 core 错误），所以每个带 reason/message 的事件
        # 都先过一遍这道门。
        for _field in ("reason", "message", "text"):
            _val = str(data.get(_field, "") or "")

            if _val and self._refuse_outdated(_val):
                return

        if name == "control_online":
            self.log("control connection established (connected to the relay)", kind="ok")

        elif name == "control_offline":
            self.log(
                f"control connection closed: {data.get('reason', '')}; reconnecting ...",
                kind="warn",
            )

        elif name == "tunnel_ready":
            self.log(
                f"tunnel {data.get('tunnel')} ready (idle, waiting)", kind="dim"
            )

        elif name == "session_start":
            self.log(
                f"player connected: session={data.get('session')} "
                f"tunnel={data.get('tunnel')}",
                kind="ok",
            )

        elif name == "session_end":
            self.log(
                f"player disconnected: session={data.get('session')}", kind="info"
            )

        elif name == "info":
            players = data.get("players")
            if players and players != self._last_info_players:
                self._last_info_players = players
                self.log(f"room players: {players}", kind="info")

        elif name == "rates":
            # Live speed is rendered from the snapshot tick; this event is
            # kept for future charting / logging of speed peaks.
            self._last_rates = data

        # ---- V2: account events ----
        elif name == "auth_failed":
            self.log(f"login rejected by the server: {data.get('reason', '')}", kind="error")

        elif name == "auth_invalidated":
            reason = data.get("reason", "")
            self.log(f"login invalid: {reason}; please log in again", kind="error")
            self._handle_auth_lost(reason)

        elif name == "delete_result":
            if data.get("ok"):
                self.log(data.get("message", "房间已删除"), kind="ok")
            else:
                self.log(
                    f"failed to delete the room: {data.get('message', '')}", kind="error"
                )

        elif name == "account_renamed":
            # Emitted by the running session (the GUI path already updated the
            # form). A running room keeps working; only the display name moved.
            renamed = data.get("username", "")

            if renamed:
                self.username_var.set(renamed)
                self.config.username = renamed
                self._refresh_account_ui()
                self._sync_identity()
                self.log(
                    f"username changed to: {renamed} (the room is unaffected)", kind="ok"
                )

        elif name == "identity_updated":
            # 中继认了我们的新显示名（问题 5）。名字以**它回的**为准：
            # 刚刚登录 / 改名的这个 client_id 在房间名册里从此对得上号。
            confirmed = str(data.get("name", "") or "")

            if confirmed:
                self.username_var.set(confirmed)
                self.config.username = confirmed

            self._refresh_account_ui()
            self._refresh_room_ui()

            with contextlib.suppress(Exception):
                self._refresh_member_list(force=True)

            self.log(
                f"the relay updated our display name to {confirmed or '(unchanged)'}",
                kind="ok",
            )

        # ---- V3: room membership ----
        elif name == "room_joined":
            self.log("entered the room", kind="ok")
            # 新房间 = 新的"已通知过"周期
            self._room_close_notified = False

            # 新房间 = STARTED 上报也要重新来一次（中继那边的 connecting 标记是
            # 跟着成员对象走的，换房/重连之后必须重新说清楚）。
            self._started_reported = None

            if self.running:
                # 自动重连/断线重连之后会话可能还在跑：补一次 STARTED 1，
                # 否则 Host 的目标表里暂时没有我们（用户第 5 条的同一件事）。
                self._report_started(True)

            # ================================================================
            #  `_mode_rebuild_pending` **不在这里清**
            # ================================================================
            #
            # 旧代码在 `room_joined` 里无条件清掉它，理由是"进新房间时旧标记
            # 不该有意义"。但那正好把**同一次换模式**的重建吃掉：热切换会让
            # 玩家的成员连接抖一下（中继先拆旧通路再广播），紧接着的
            # `room_joined` 把"待重建"清掉 -> 按钮还显示在连接，会话却永远不
            # 按新模式重建（用户 2026-09-29 第 3/6 条："中转换直连……连接未自动
            # 重连，需要双端手动重连"）。
            #
            # 标记的清零点只剩 `_maybe_rebuild_for_mode_change()` 里那几个
            # （真的重建了 / 会话本来就在新模式上）；每秒一次的
            # `_refresh_room_ui()` 马上就会再判一次，这里什么都不用做。


            # 游客身份是**登录会话级**的：中继第一次给的「游客xxxxxx」记在内存里，
            # 退房 / 被踢 / 房间被关都**不销毁**（下次建房或加房还用这个名字），
            # 只有登出 / 换账号 / 重启才由中继重新分配。绝不写进配置文件。
            state = self._room_state_obj()

            if self._is_guest() and state is not None:
                own = str(getattr(state, "own_name", "") or "").strip()
                remembered = self._remember_guest_name(own)

                if remembered:
                    self.username_var.set(remembered)

            self._refresh_room_ui()
            self._refresh_room_list_soon()
            self._refresh_member_list()

            # 中继首次分配游客名时随 OK 回包给了 `guest=<token>`：存进**登录
            # 会话级**状态（内存），同一个会话里换房 / 重新进房时放回握手行，
            # 名字才不变（用户 2026-09-27 口径）。登出 / 换账号 / 重启清掉。
            with contextlib.suppress(Exception):
                if self._is_guest():
                    self.engine._guest_token = str(
                        getattr(self.engine.room_current(), "guest_token", "")
                        or self.engine._guest_token
                        or ""
                    )

        elif name == "conn_mode_changed":
            # ============================================================
            #  §D 末句（设计基准 / PROTOCOL §8.3）：换模式 = **严格重建全部通路**
            # ============================================================
            #
            # 中继在成员控制连接上推的不带 `ERROR` 前缀的那一行。它**不是**网络
            # 抖动：旧模式下的洞 / 隧道 / 每条连接的独立状态机全部作废（新模式的
            # NAT 映射和它们不是同一套），必须整体重建。
            #
            # 这里和"房间模式变了"那条路径共用同一个实现（`_sync_room_nat` /
            # 会话模式同步）：那一条是**每秒比对**出来的，可能晚一拍；这一条是
            # 中继**主动推**的，所以收到就立刻做一次，别等下一拍。
            #
            # "热切换后第一轮打洞白打"的根治也在这里：先把旧的一整套拆干净
            # （`watch_room_direct(False)` 会 close 掉 punch link + 状态机 +
            # 计时器），再按新模式从 §E 的第一步重新走，所以第一轮就是有效的一轮。
            self.log(
                f"[direct] the relay says the room mode changed "
                f"({data.get('reason', '') or 'CONN_MODE_CHANGED'}); "
                f"tearing down every path and rebuilding from scratch",
                kind="warn",
            )

            with contextlib.suppress(Exception):
                self.engine.watch_room_direct(False, log=self.log)

            self._punch_mode = None

            # ★ P0（2026-09-28）：这一行是"通知所有人重建"的**主动**触发点。
            #
            # 为什么不能在这里直接 `_rebuild_tunnels(mode=…)`：中继这一行是在
            # `HOST_STATE mode=<新模式>` **之前**发的（它先拆旧通路、再广播房间
            # 状态），所以此刻 `_room_conn_mode()` 还是旧模式 —— 拿它去重建等于
            # 按旧模式再建一遍（用户报的"隧道好像没有重建、连不上"）。
            #
            # 所以只打一个"待重建"标记；房间状态一到（下一次 `HOST_STATE` ->
            # `_refresh_room_ui`），`_maybe_rebuild_for_mode_change()` 用**已经
            # 确认的新模式**重建一次。每秒一次的 `_sync_session_mode` 是同一件
            # 事的兜底（会话还活着、只是模式对不上时它也会重建）。
            #
            # ★ 2026-09-29（服务端 §8.9 ADDITIVE）：中继现在会带 `was_live=0|1`
            # —— "切换前这一个成员**真的在连接吗**"。用户要的语义正是：
            #   原本空闲/未连接 -> **只换模式**，不自动重连；
            #   原本有连接（含正在打洞）-> 严格重建 + 自动重连。
            # `was_live=0` 是比"本地 running"更硬的事实，所以它优先；
            # 老中继没有这个 token -> 退回旧的 `self.running` 判据。
            was_live = str(data.get("was_live", "") or "")

            if mode_switch_should_reconnect(was_live, self.running):
                # ★ P2：记下**切换前**的房间模式（见 `_mode_rebuild_from`）。
                self._mode_rebuild_from = self._room_conn_mode()
                self._mode_rebuild_pending = True
                # ★ 期限（2026-10-03）：这个标记**必须会过期**。它曾经被留成永久真，
                #   结果把"真的 host 停服"和"掉线复位"全都吞掉了。
                self._mode_rebuild_pending_until = (
                    time.monotonic() + MODE_REBUILD_PENDING_MAX_S
                )
            else:
                self._mode_rebuild_pending = False

                if was_live == "0":
                    self.log(
                        "[mode] the relay says this member had NO connection "
                        "before the mode switch (was_live=0); only the mode "
                        "changes, no automatic reconnect",
                        kind="warn",
                    )

            self._refresh_room_ui()
            self._refresh_member_list()

        elif name == "host_state":
            # Pushed by the relay whenever the host slot or the roster changes,
            # so the UI is never more than one event behind reality.
            self._refresh_room_ui()
            # 名单变了（有人认领/释放主机、有人进出），房间列表也要跟着更新，
            # 否则"刚释放了身份，列表还显示我是主机"。
            self._refresh_room_list_soon()
            # ============================================================================
            #  host 可以换人：踢人权限必须跟着换，不许缓存
            # ============================================================================
            #
            # 用户要求：一个房间生命周期内 host 会转移，所以「踢出」按钮的可见性
            # 和「身份」列都必须在**每一次房间状态变化**时重新判定。成员列表的行
            # 控件是复用的（不重建、不闪），但每一格里写什么、按钮显不显示，
            # 都由这一刻的 RoomState 现算 —— 卸任的 host 下一次刷新就没按钮了。
            self._refresh_member_list()

        elif name == "host_taken":
            self.log(
                f"someone else claimed the host slot first: {data.get('reason', '')}",
                kind="warn",
            )
            self._refresh_room_ui()
            self._refresh_member_list()

        elif name == "room_revoked":
            # ============================================================================
            #  服务器把房间关了：本地必须【立刻】收拾干净（幽灵房的根治）
            # ============================================================================
            #
            # 真正替我们说 LEAVE 并关掉那条成员连接的是下面的
            # _teardown_room_locally() —— 它调用 engine.close_room()，
            # 也就是 room.py 的 leave()（LEAVE + 关 socket）。（room.py 里那个
            # _leave_after_revoke() 已经不在活动路径上了，别再指望它。）
            #
            # 界面不能等任何异步收尾：用户看到的是"房间已经被管理关闭了，可所有
            # 按钮还是房间内的状态，得手动点退出房间才恢复"。所以这里立刻把世界
            # 那侧停掉、把所有按钮恢复成"不在房间里"的样子。
            #
            # 另外留一个兜底定时器：万一连接没有自己退干净（网络半死、连接卡住），
            # 2.5 秒后强制把成员连接也关掉，绝不留幽灵状态。
            reason = data.get("reason", "管理员关闭了房间")

            self.log(f"room closed by the server: {reason}", kind="error")

            # 这里就弹窗：ROOM REVOKED 是**服务器关房的第一手通知**。
            # （以前只在后面的 room_closed 弹，但那条通知可能永远不来 ——
            #  界面这边会先把成员连接关掉，room.py 里发 room_closed 的那段
            #  代码跟着被取消。用户看到的就是"日志有、弹窗没有"。）
            self._teardown_room_locally(
                reason=reason,
                notify=True,
                title=tr("房间已被管理关闭"),
            )
            self._schedule_room_teardown_fallback()

        elif name == "kicked":
            # ============================================================================
            #  被踢（KICKED by=... reason=...）：只对【我这一个人】生效的关房
            # ============================================================================
            #
            # 和 ROOM REVOKED 走**同一条**拆除路径，只是措辞不同：房间还在，
            # 只是我进不去了。绝不能当普通断线处理 —— _explain_disconnect()
            # 会去问"房间还在不在"，答案是"在"（别人还在里面），于是它会判定
            # 成网络问题、把用户留在一个人家已经把他踢出去的房间里（幽灵态）。
            by = data.get("by", "") or ""
            reason = data.get("reason", "") or ""

            self.log(
                f"kicked from the room: by={by or '-'} reason={reason or '-'}",
                kind="error",
            )

            self._teardown_room_locally(
                reason="你已被踢出房间",
                notify=True,
                title=tr("你已被踢出房间"),
                message=(
                    self._kick_reason_text(by, reason)
                    + "\n\n"
                    + tr("你已被移出该房间，已自动退出。")
                ),
            )
            self._schedule_room_teardown_fallback()

        elif name == "room_closed":
            # 我们主动退出的那一步已经做完了：本地状态干净，按钮可用，
            # 可以立刻重新建房或加入别的房间 —— 不需要手动点「退出房间」。
            reason = data.get("reason", "房间已被关闭")

            self.log(f"automatically left the room: {reason}", kind="warn")

            self._teardown_room_locally(reason=reason, notify=True)

        elif name == "room_gone":
            # 中继明确说这个名字对应的房间不存在了（例如管理员刚删掉，
            # 或者中继重启过）。和 room_closed 一样清理，但用词不同。
            reason = data.get("reason", "房间不存在")

            self.log(f"room no longer exists: {reason}", kind="warn")

            self._teardown_room_locally(
                reason=reason, notify=True, title=tr("房间已不存在")
            )

        elif name in ("host_left", "host_stopped", "host_released"):
            # ============================================================================
            #  ★ 用户 2026-09-30 定稿：host 的三种情况**分开处理**
            # ============================================================================
            #
            # 中继在**成员控制连接**上发一行可识别的原因（核心转成这三个事件，
            # 见 `RoomSession._on_line`）：
            #
            #   `host_left`     = HOST_LEFT（host **闪断**，身份还在宽限期里）
            #                     -> 能复用就复用现有连接，不能用就自动重连；
            #                        原本空闲的人不重连。
            #   `host_stopped`  = HOST_STOPPED（host **主动停止开服**，位还在）
            #                     -> 断开 + 重算按钮，**不自动重连、不弹窗**。
            #   `host_released` = HOST_RELEASED（host **主动释放主机位**）
            #                     -> 断开 + 重算 + **弹窗「host 已断开连接」**，
            #                        不自动重连。
            #
            # 设计里**没有"房主已离线"这个概念**：房主真离开房间 = 房间直接销毁
            # （ROOM REVOKED 那条路），不会走到这里。
            kind = {
                "host_left": "left",
                "host_stopped": "stopped",
                "host_released": "released",
            }[name]

            # ★ P4（2026-10-03 热切换分析）：模式热切换时，**如果我是 host**，
            #   我自己的重建会先 `stop_session()` -> 关掉 `HOST <room>` 控制连接 ->
            #   中继于是给**所有人**广播 `HOST_STOPPED`（"host 主动停止，别重连"）。
            #   但这不是用户停服，是我在按新模式重建：谁要是把它当"host 停了"，
            #   就会停会话、把按钮放掉、进入 host_down（本房所有人一起被放掉）。
            #   所以模式重建窗口里/待重建标记还在时，这类事件只记日志、不改状态。
            # ★ 2026-10-03 修回归：以前这里还看 `_mode_rebuild_pending`，而那个
            #   标记**可能被留成永久真**（新模式广播没到就没人清它）—— 于是
            #   host 无论什么原因断开，这一支都被吞掉，**其他 player 永远不断开**。
            #   现在只认两个**会自己结束**的事实：
            #     * `_mode_switch_silent`：有界窗口（`MODE_SWITCH_SILENT_MAX_S`）；
            #     * `_rebuild_in_flight`：worker 的 finally 必复位。
            #   真实 host 停服（HOST_STOPPED / HOST_RELEASED）因此一定会被处理。
            # 有界的"待重建"：标记在 + **未过期**才算数（分析结论 c）。
            pending_armed = getattr(self, "_mode_rebuild_pending", False) and (
                time.monotonic()
                <= getattr(self, "_mode_rebuild_pending_until", 0.0)
            )

            # 静默窗口也要**按期限**算（分析建议）：闩锁住的标志不许吞掉真实停服。
            silent_armed = getattr(self, "_mode_switch_silent", False) and (
                time.monotonic()
                <= getattr(self, "_mode_switch_silent_until", 0.0)
            )

            # ============================================================
            #  ★ v3.9.16：**不再丢弃，只推迟**
            # ============================================================
            #
            # 以前窗口期内这类事件被降级成一行日志 —— 那正是"host 断开后房间内
            # 其余人不断开"的直接原因（用户 2026-09-30 定稿 → 10-02 → 10-03
            # 三次重申，实测仍未生效）。
            #
            # 现在拆成两层：
            #   * **断不断**由核心层决定 —— `RoomSession` 收到这几个码就调
            #     `MCLinkEngine._on_host_gone_from_room`，会话与直连通路当场停掉，
            #     界面这三道窗口管不着（见 core/room.py 的 `_host_gone_teardown`）；
            #   * 界面这层只剩"提示什么时候显示"：窗口期内**缓存**最后一次
            #     host-gone，等窗口结束（或兜底超时）`_flush_pending_host_gone()`
            #     立刻补跑。事件因此不可能被吃掉。
            if (
                silent_armed
                or getattr(self, "_rebuild_in_flight", False)
                or pending_armed
            ):
                self._defer_host_gone(kind, data.get("reason") or "")
            else:
                self._on_host_gone(kind, data.get("reason") or "")

        elif name == "room_lost":
            # ============================================================================
            #  掉线了，但房间**还在**（中继那边查过了）
            # ============================================================================
            #
            # 用户报的问题：「掉线之后开始连接立刻变回可用，但房间那一片还是
            # 老状态，得手动点一次退出房间才复位」。
            #
            # 原因是这里只刷新了连接那一侧：引擎里那个房间对象还在，界面就继续
            # 认为"我在房间里"。两边用的根本不是同一个事实来源。
            #
            # 现在的做法是**把事实重新对上**，而不是就地改界面：
            #   1. 先停掉世界那侧（数据通道已经断了，界面不该继续显示在转发）；
            #   2. 延迟一小会儿自动重新进房（顺带把主机位置抢回来）；
            #   3. 成功 → 房间状态由 room.py 的广播重新填满，两边一致；
            #      失败 → 彻底收拾干净，所有按钮回到"不在房间"。
            #
            # 关键是第 3 步的兜底：无论重连成功还是失败，**都不会再留下一个
            # 看起来还在房间、实际已经掉线的界面**。
            reason = data.get("reason", "")

            if reason:
                self.log(f"connection to the room lost: {reason}", kind="warn")

            # ================================================================
            #  ★ 我的房间连接丢了，而且中继给的原因是"host 那边"
            # ================================================================
            #
            # 设计里**没有"房主离线"这个概念**（用户 2026-09-30）：房主真离开
            # 房间 -> 房间直接销毁，那是 `ROOM REVOKED` 那条路。所以这里只可能是
            # host 闪断 -> 按 `_on_host_gone("left")` 处理（能复用就复用现有
            # 连接、不能用就自动重连；空闲态不重连）。
            if self._is_no_host_reason(reason):
                # 我的房间连接丢了、中继给的原因是"host 那边"：按 host 闪断
                # 处理（能复用就复用、不能用就自动重连），**不**再进
                # "等待房主上线"那个状态（用户 2026-09-30：设计里没有这个说法）。
                self._on_host_gone("left", reason)
                self._refresh_room_ui()
                self._schedule_room_recovery()
                return

            # ---- 普通掉线：世界那侧要停，会话要自动重建（用户已批准的框架） ----
            session_was_live = bool(self.running)

            if session_was_live:
                # 只停世界那侧，**不**走 on_stop()：用户没有点停止，这是
                # "非用户意图的断开"，要走自动重连（见 AUTO-RECONNECT-PLAN.md）。
                # 标成"有意的"会让 `_can_auto_reconnect` 直接返回 False。
                self.log(
                    "the room connection dropped while the session was live; "
                    "the world side is stopped and the session will be rebuilt",
                    kind="warn",
                )

                # ★ 顺序是 P0：`_trigger_auto_reconnect()` 的闸门看的就是
                # `self.running`（"掉线前我是不是在连接"），所以**必须先起重连、
                # 再清 running**。以前的顺序反了 —— 先清 running 再调用，闸门
                # 直接返回 False，整条"被动意外掉线自动重连"是**死代码**
                # （日志里写着 "the session will be rebuilt"，实际一次都没重建）。
                self._trigger_auto_reconnect(
                    f"room connection lost: {reason or 'unknown'}"
                )

            self.running = False
            self._refresh_room_ui()

            self._schedule_room_recovery()

        elif name == "session_ended":
            # ============================================================
            #  SESSION 探活（PROTOCOL §8.10）：**不是闪断**
            # ============================================================
            #
            # 被动掉线之后，成员连接已经断了，`KICKED` / `AUTH_INVALID_TOKEN`
            # 这类被动推送再也收不到。核心因此先做一次只读探活；这一条就是
            # "探活说我的房间身份已经结束"（auto_reconnect=0）：
            #
            #   KICKED / LEFT / TIMEOUT / ROOM_CLOSED / AUTH_*
            #
            # 用户口径（设计基准 §C / AGENTS.md 规则 6 补充）：**按退出房间处理**
            # —— 不自动重连、收拾干净、把话说清楚；`relogin=1` 时提示需要重新
            # 登录。自动重连仍然只允许「模式热切换」+「真·被动闪断」两类，这条
            # 路径把第二类里"其实是被踢/失效"的那些挡在外面。
            code = str(data.get("code", "") or "")
            relogin = bool(data.get("relogin", False))
            fallback = str(data.get("reason", "") or "")

            self.log(
                f"[reconnect] the relay's SESSION probe says my room identity "
                f"is gone (reason={code or '-'}, "
                f"relogin={1 if relogin else 0}); leaving the room "
                f"(no automatic reconnect)",
                kind="warn",
            )

            title, message = session_end_notice(code)

            # 已经有人告诉过用户了（例如 KICKED 被动推送先到、kicked 分支已经
            # 弹过窗并收拾干净）-> 这一条只负责把房间/登录态对齐，**不再弹第二个
            # 对话框**（用户要的是"告知一下"，不是被连点两次）。
            already_notified = bool(
                getattr(self, "_room_close_notified", False)
            )

            # 先退干净房间（不弹这个通用窗），保证按钮/房间状态复位。
            self._teardown_room_locally(
                reason=fallback or title,
                notify=False,
            )

            if relogin and not already_notified:
                # token 已失效：走"需要重新登录"那条既有路径（它自己会弹一次
                # 窗并把会话停掉）—— 只弹一次，绝不两个对话框叠着。
                self._handle_auth_lost(
                    str(data.get("detail", "") or "") or code
                )
            elif relogin:
                # 已经告知过房间没了，但登录态确实失效了：清掉它并留一行日志。
                self.auth = None
                self._refresh_account_ui()
                self.log(
                    "the relay says the sign-in is no longer valid; "
                    "please sign in again",
                    kind="warn",
                )
            elif not already_notified:
                with contextlib.suppress(Exception):
                    messagebox.showwarning(
                        title, message, parent=self.root
                    )

                self._room_close_notified = True

            self._schedule_room_teardown_fallback()

        elif name == "listen_failed":
            # 本地代理端口绑不上 = 会话核心自己退出（用户列的非用户意图断开之一）。
            # 以前只有一行日志，界面还显示"运行中" —— 现在走自动重连（换一次
            # 绑定仍失败就会用尽窗口并进终态，不会无限重试）。
            reason = str(data.get("reason", "") or "")

            self.log(f"failed to listen on the local port: {reason}", kind="error")

            if self.running:
                self._trigger_auto_reconnect(f"listen failed: {reason}")

        elif name == "listening":
            self.log(
                f"local proxy listening on {data.get('endpoint')}", kind="ok"
            )

        elif name == "minecraft_connected":
            self.log("Minecraft client connected to the local proxy", kind="ok")

        elif name == "relay_connected":
            self.log("data channel established with the host; forwarding data", kind="ok")

        elif name == "relay_error":
            self.log(f"forwarding failed: {data.get('reason', '')}", kind="error")

        elif name == "minecraft_disconnected":
            self.log("Minecraft client disconnected", kind="dim")


    def _teardown_room_locally(self, reason="", notify=False, title=None,
                               message=None):
        """
        房间没了（被管理关闭 / 被踢 / 不存在了）→ 把本地恢复到"不在任何房间里"。

        ============================================================================
          这个函数存在的唯一理由：不再出现"幽灵房"
        ============================================================================

        What the user saw before this existed:

          * the room was closed by an admin, the client kept looking connected
          * the buttons stayed in the "I am in a room" state
          * 开始连接 was still clickable, and only THEN did it say the room was
            closed
          * and the user still had to press 退出房间 by hand to get a clean UI

        So the teardown is unconditional and complete, whatever the relay said:
        stop the Minecraft side, drop the room session, clear the local room
        state, and re-render every button from scratch.

        `notify` 控制要不要弹窗告知用户。**只弹一次**：中继会先推
        ROOM REVOKED、随后客户端自己退出时又有一个 room_closed，两次都弹就是
        连续两个对话框（用户要的是"告知一下"，不是被连点两次）。

        `message` 是给"被踢"这类需要带发起者/原因的通知用的完整正文；给了就
        原样显示（调用方已经逐句 tr() 过），不再套默认那句。
        """
        if self.running:
            with contextlib.suppress(Exception):
                self.on_stop()

        # 成员连接：正常路径上 room.py 已经自己 LEAVE + 关掉了；这里再关一次
        # 是幂等的，为的是兜住"通知来了但连接没退"的情况。
        with contextlib.suppress(Exception):
            self.engine.close_room()

        self.room_var.set("")

        with contextlib.suppress(Exception):
            self.room_state_label.configure(
                text=tr("未加入房间"), fg=FG_MUTED
            )

        # 房间没了：STARTED 的"已上报"记忆一起清掉（下次进房重新报）。
        self._started_reported = None
        self._mode_rebuild_pending = False

        # 按钮状态重新算一遍（这是关键：以前就是这里漏了）。
        self._refresh_room_ui()
        self._refresh_room_list_soon()

        # 人在房间里被踢掉：成员窗口里的行要跟着空掉（窗口本身留着，用户可能
        # 还想看"刚才都有谁"）。
        self._refresh_member_list(force=True)

        if notify and not self._room_close_notified:
            self._room_close_notified = True

            with contextlib.suppress(Exception):
                body = message or (
                    tr(reason or "房间已被关闭")
                    + "\n\n"
                    + tr("你已经自动退出该房间，可以直接重新创建或加入房间。")
                )

                messagebox.showwarning(
                    title or tr("房间已被关闭"),
                    body,
                    parent=self.root,
                )


    def _schedule_room_recovery(self, delay_ms=900):
        """
        掉线之后（房间还在）：等一下、自动重新进房，失败就把本地收拾干净。

        ============================================================================
          为什么不能"就地改界面"
        ============================================================================

        界面上"我在不在房间里"只有一个来源：引擎里的房间对象。掉线之后那个对象
        还在，所以界面会一直显示在房间里，而连接那侧已经复位 —— 这就是用户看到的
        「开始连接能用了，房间那边还得手动退出」。

        所以这里动的是**事实**：重新进房——成功，房间状态被广播重新填满；失败，
        调用 _teardown_room_locally() 把房间对象扔掉。两条路都不会留下
        "看起来还在房间、其实已经掉线"的界面。

        用后台线程做：open_room() 是阻塞的（最长十几秒），不能卡住界面。
        """
        session = self.engine.room_current()

        if session is None:
            # 引擎里已经没有房间了：直接复位就行，不用重连。
            self._refresh_room_ui()
            self._refresh_room_list_soon()
            return

        # 掉线那一刻我们是不是主机 —— 重连成功了要把位置抢回来。
        with contextlib.suppress(Exception):
            self.engine._room_was_host = bool(session.state.is_host)

        def start():
            # 记下要重连的房间名（重连用得到，界面上也要显示）。
            state = self.engine.room_state()
            name = ""

            with contextlib.suppress(Exception):
                name = (state.room_name or state.room or "") if state else ""

            self.log(
                f"与房间「{name}」的连接断了，正在自动重连…"
                if name
                else "与房间的连接断了，正在自动重连…",
                kind="warn",
            )

            def worker():
                ok, message = self.engine.auto_rejoin()

                def finish():
                    if ok:
                        self.log(message, kind="ok")
                        self._room_close_notified = False
                        self._refresh_room_ui()
                        self._refresh_room_list_soon()
                        return

                    # 重连不上：房间确实回不去了，把本地收拾干净 ——
                    # 这一步就是"按钮状态跟着复位"，不再需要手动退出房间。
                    self.log(f"automatic rejoin failed: {message}", kind="warn")

                    self._teardown_room_locally(
                        reason=f"与房间的连接已断开（{message}）",
                        notify=True,
                        title=tr("与房间的连接已断开"),
                    )

                with contextlib.suppress(Exception):
                    self.root.after(0, finish)

            threading.Thread(
                target=worker, name="mclink-room-rejoin", daemon=True
            ).start()

        with contextlib.suppress(Exception):
            self.root.after(delay_ms, start)


    def _schedule_room_teardown_fallback(self, delay_ms=2500):
        """
        兜底：ROOM REVOKED 之后如果成员连接没有自己退干净，也要收干净。

        中继那边是"通知 → 等客户端自己退 → 超时强断"，客户端这边就不该赌
        哪一步一定成功。等一小会儿（给对方一个体面 LEAVE 的机会），如果本地
        还觉得自己在房间里，就强行收拾掉。
        """

        def check():
            with contextlib.suppress(Exception):
                if getattr(self, "_rebuilding", False):
                    self.root.after(delay_ms, check)
                    return

                self.engine.drain_events()

                if self.engine.room_current() is not None:
                    self.log(
                        "the connection did not exit by itself after the room-closed notification; local state was force-cleaned.",
                        kind="warn",
                    )
                    self._teardown_room_locally(notify=False)

        with contextlib.suppress(Exception):
            self.root.after(delay_ms, check)


    def _handle_auth_lost(self, reason=""):
        """The relay rejected our token: stop and require a fresh login."""
        self.auth = None
        self._refresh_account_ui()

        if self.running:
            try:
                self.engine.stop_session()
            except Exception:
                pass

            self.running = False
            self._set_enabled(self.start_button, True)
            self._set_enabled(self.stop_button, False)
            self.delete_room_button.configure(state="disabled")

        # 会话在这里被停掉了，_refresh_status 再也看不到 ERROR 状态
        # （引擎里已经没有会话了），所以页眉要在这里亲手改掉 —— 否则它会
        # 一直显示「已连接中转服务器」，而我们已经把连接关掉了。
        with contextlib.suppress(Exception):
            self.server_status_label.configure(
                text=tr("● 服务器不可用"), fg=BAD
            )

        messagebox.showwarning(
            "需要重新登录",
            f"{reason or '登录状态已失效'}\n\n请重新登录后再连接。",
            parent=self.root,
        )


    def _append_logs(self):
        lines = []

        for _ in range(60):
            try:
                lines.append(self.log_queue.get_nowait())
            except queue.Empty:
                break

        if not lines:
            return

        self.log_text.configure(state="normal")

        for stamp, text, kind in lines:
            self.log_text.insert("end", f"[{stamp}] {text}\n", kind or "normal")

        # Keep the widget bounded.
        line_count = int(self.log_text.index("end-1c").split(".")[0])
        if line_count > MAX_LOG_LINES:
            self.log_text.delete("1.0", f"{line_count - MAX_LOG_LINES}.0")

        self.log_text.see("end")
        self.log_text.configure(state="disabled")


    def _direct_status_view(self, snapshot):
        """
        ONE dict for every row that describes the direct path.

        Merges three sources in order of how live they are:

          1. the session snapshot (a running connection re-measures constantly);
          2. the ROOM's punch, which starts the moment we join a STUN room and is
             therefore the only thing that can answer before a game exists;
          3. the room's mode, so a STUN room shows STUN rows even while the local
             selector still reads relay.

        Anything that renders a direct-path row goes through here. Two rows
        reading two objects is how the owner got a screenshot where 通路 said
        "直连尝试中" directly above 打洞进度 saying "已直连 <ip:port>".
        """
        room_punch = self.engine.room_direct_state() or {}
        view = dict(snapshot)

        for key in ("direct_step", "direct_via", "direct_peer", "direct_attempts",
                    "direct_carrying", "flow"):
            if not view.get(key):
                view[key] = room_punch.get(key)

        # ============================================================
        #  入站判定：以【房间】那次探测为准，不是会话的
        # ============================================================
        #
        # 3.0.x 起，入站探测是「点开始连接」时房间那次打洞里跑的
        # （`room_direct._punch` → measure → probe_inbound），会话自己**不再**
        # 探测。可是会话快照里那两个字段是初始值 "unknown" / 一句英文 note ——
        # 它们是**非空字符串**，所以上面那句 `if not view.get(key)` 拦不住它们，
        # 会话的"unknown"就把房间真实测出来的结论顶掉了。
        #
        # 结果就是用户在日志里明明看到
        #   [nat] the relay's probe did not arrive from a host we never wrote to
        #   → 可打
        # 屏幕上却写着「未测（relay did not confirm the probe）」。
        # 对这两个字段，房间的结论优先（它有值就用它的）。
        for key in ("inbound_state", "inbound_note"):
            if room_punch.get(key):
                view[key] = room_punch[key]

        # ★ 用户 2026-10-01：全锥证据也走这里（纯展示：NAT 那两行要用它把
        #   「NAT1/2 未定」升级成「NAT1 全锥形」）。
        if room_punch.get("eif_proof"):
            view["eif_proof"] = True

        # Not a "falsy wins" field: it is a fact about the punched link, and it
        # decides whether the card says "the punch has not started" or "the
        # link is up and carrying".
        view["stun_ready"] = bool(room_punch.get("stun_ready"))
        view["punching"] = bool(room_punch.get("punching"))

        if self._room_conn_mode() in (MODE_STUN, MODE_AUTO):
            view["mode"] = self._room_conn_mode()

        return view


    def _status_peer_latency(self, view, room_punch):
        """
        运行状态卡片「对端延迟」那一格的 (rtt_ms, applicable)。

        适用 = STUN 直连且**至少一条对端链路已打通**。数据源和「通路」「打洞
        进度」是同一份 `room_direct` 状态，所以同一屏上的几行不会互相矛盾。

        选中哪一条：优先 `direct_peer`（「通路」正在显示的那个对端），否则第一
        条已打通的。一条都没有 -> (None, False) -> 界面显示 `N/C`。
        """
        links = [
            item for item in (room_punch.get("links") or [])
            if item.get("ready")
        ]

        if not links:
            return None, False

        wanted = view.get("direct_peer") or ""
        chosen = None

        for item in links:
            if wanted and item.get("peer") == wanted:
                chosen = item
                break

            if chosen is None:
                chosen = item

        if chosen is None:
            return None, False

        return chosen.get("rtt_ms"), True


    def _refresh_status(self):
        snapshot = self.engine.snapshot()

        if not snapshot:
            snapshot = self.last_snapshot

        self.last_snapshot = snapshot

        # 「Minecraft 连接地址」跟着状态实时更新（玩家模式显示 127.0.0.1:端口）
        self._update_address(snapshot)

        state = snapshot.get("state", STATE_IDLE)
        role = snapshot.get("role")

        state_text = snapshot.get("state_text") or snapshot.get("state") or "—"

        state_color = {
            STATE_IDLE: FG_DIM,
            STATE_CONNECTING: WARN,
            STATE_WAITING: WARN,
            STATE_ONLINE: OK,
            STATE_ERROR: BAD,
        }.get(state, FG)

        self.status_vars["state"].set(tr(state_text))

        # ---- 通路（V3）----
        #
        # 判定逻辑在 nat_diag_path_text（模块级纯函数，可离线测试）。
        # 这个显示错过两次，都是同一个方向：**断言了一条并不存在的通路**。
        connected = state in (STATE_WAITING, STATE_ONLINE)

        # ONE merged dict for every direct-path row, built BEFORE the first row
        # reads it. They used to read different objects and therefore disagreed
        # on the same screen: 通路 said "直连尝试中" while 打洞进度 said
        # "已直连 223.167.44.206:60831".
        view = self._direct_status_view(snapshot)

        # Read the ROOM's punch too, exactly like the two rows below do.
        #
        # This row used to look only at the session snapshot, so a host whose
        # punch had already succeeded but who had no game session yet saw:
        #
        #     通路       直连尝试中                    <- this row, session-only
        #     打洞进度   已直连 223.167.44.206:60831   <- the other rows, merged
        #
        # Two rows about the same fact, disagreeing on screen. The owner's
        # screenshot. One source of truth for both.
        path_text, hint = nat_diag_path_text(
            getattr(self.config, "mode", "relay"),
            view.get("direct_via"),
            connected,
            peer=view.get("direct_peer") or "",
            punch_ready=bool(view.get("stun_ready")),
            punching=bool(view.get("punching")),
            carrying=bool(view.get("direct_carrying")),
            # §G 第 1 层：有 flow 汇总时用「n/m」口径（用户点名要的版式）。
            # `view` 已经把会话与房间两份 `flow` 合过了（见 `_merge_direct_view`）。
            flow=view.get("flow") or None,
        )

        if view.get("mode") == MODE_AUTO:
            from ..core.connection_status import auto_summary
            path_text = auto_summary(snapshot.get("connection_rows", []))
            hint = "ok" if connected else "muted"

        path_color = {
            "ok": OK, "warn": WARN, "bad": BAD,
            "dim": FG_DIM, "muted": FG_MUTED,
        }.get(hint, FG)

        self.status_vars["path"].set(tr(path_text))
        self._set_status_color("path", path_color)

        # ---- 网络类型 / 直连尝试（取代原来的独立诊断卡片）----
        #
        # Three sources, in order of how LIVE they are:
        #   1. the session's snapshot (a running connection re-measures every
        #      round and every NAT_RECHECK_S);
        #   2. the ROOM monitor, which measures from the moment we join a STUN
        #      room -- so the card is live before "start connection" is pressed;
        #   3. the last profile we ever saw, so leaving a room does not blank
        #      the row out.
        # The ROOM-level punch is the source of truth while we are in a STUN
        # room: it starts before any session exists, so it is the only thing
        # that can report what is happening during those first seconds.
        room_punch = self.engine.room_direct_state() or {}

        nat_profile = (
            snapshot.get("nat_profile")
            or self.engine.room_nat_profile()
            or room_punch.get("nat_profile")
            or self._nat_profile
        )
        # ★ 用户 2026-10-01：把"对端首包证明的全锥"塞进 snapshot（**只读**：
        #   它只是一个显示用的布尔值，进界面的 NAT 文案/颜色，不进任何决策）。
        with contextlib.suppress(Exception):
            snapshot["eif_proof"] = bool(room_punch.get("eif_proof"))

        # ★ 两行同源（分析结论 B）：NAT 行也必须吃**合并后**的 view，
        #   否则 raw snapshot 的 inbound_state 还是初始 "unknown"，
        #   会出现"NAT 行说端口受限、入站行说中等"这种自相矛盾。
        nat_rows = nat_diag_status_values(
            nat_profile, view, getattr(self.config, "mode", MODE_RELAY)
        )

        for key, (text, hint_color) in nat_rows.items():
            # 「直连尝试」那一行已经不存在了（用户要求删除），
            # 所以 nat_rows 里即使还带着 "direct" 也不再写入界面。
            if key == "direct":
                continue

            if key in self.status_vars:
                self.status_vars[key].set(text)
                self._set_status_color(key, hint_color)

        # ---- 入站可达 / 打洞进度 ----
        #
        # 入站那一档要用他那一套（NAT_FILTER_LABEL 的四个挡位），而合成规则
        # 需要两个输入：STUN 判出的过滤行为（就在上面那个 nat_profile 里）和
        # 中继探测的结论（在 view 里）。所以这里把 profile 的 filtering 传进去。
        inbound_text, inbound_hint = inbound_status_text(
            view, getattr(nat_profile, "filtering", None)
        )
        step_text, step_hint = punch_step_text(view)

        self.status_vars["inbound"].set(tr(inbound_text))
        self._set_status_color("inbound", inbound_hint)
        self.status_vars["punch"].set(tr(step_text))
        self._set_status_color("punch", step_hint)

        # 3.0.9：一人一行（一个对端一条洞）。数据来自房间打洞协调器，和上面
        # 「通路」「打洞进度」是同一份来源，所以几行不会互相打架。
        #
        # 审计问题 2：**只有真的在打洞时才出行**。`room_punch["links"]` 现在
        # 已经带了这个门（room_direct.state()），这里再显式传一次 `punching`，
        # 多一道保险 —— relay 模式下绝不许出现「打洞中」。
        self._render_hole_rows(
            room_punch.get("links"), punching=bool(room_punch.get("punching"))
        )

        # 3.0.9：房间成员列表窗口开着的话，跟着这一拍一起刷新（没开就是空操作）。
        self._refresh_member_list()

        # The state text is the single most important thing on this card, so
        # it gets the strongest colour treatment.
        self._set_status_color("state", state_color)

        active = bool(role)

        if role == "host":
            self.status_vars["role"].set("主机 Host")
            # 本机视角：中继实际分配给我的 Minecraft 会话数 / 房间容量。
            #
            # 这一行以前挂在「已加入世界」上（然后又被下面那句无条件覆盖），
            # 于是房主端和玩家端对同一个房间显示两个不同的数字。现在它是
            # 自己的一行，谁也不会覆盖谁；容量也还在。
            self.status_vars["local_links"].set(
                f"{snapshot.get('players', 0)} / "
                f"{snapshot.get('max_players') or proto.DEFAULT_MAX_PLAYERS}"
            )
            # ================================================================
            #  ★ 用户 2026-09-29 第 6 条：这个数字的**名字**必须说清它是什么
            # ================================================================
            #
            # `tunnels_ready` 数的是**空闲等待中的**隧道（中继答了 WAITING、
            # 还没分配玩家）。旧文案写「隧道 n/m 就绪」，用户读成"我有 n 条隧道
            # 可用"，于是玩家一挂上来（idle -> 分配）数字就往下跌，
            # 「6/7、7/8 一直跳动，突然变成 0，又变成 1」——看起来像隧道掉了，
            # 其实是"空闲的少了、在用的多了"。
            #
            # 用**已有的**词「空闲隧道」（房间列表那列就是这个 key），并且把
            # 正在服务的条数一起说出来 —— 一升一降互相解释，不会再被误读。
            _idle = int(snapshot.get("tunnels_ready", 0) or 0)
            _total = int(snapshot.get("tunnels_total", 0) or 0)
            _busy = int(snapshot.get("players", 0) or 0)

            self.status_vars["traffic"].set(
                traffic_ledger_text(snapshot)
                + f"  |  {tr('空闲隧道')} {_idle}/{_total}"
                + f"  |  {tr('已加入世界')} {_busy}"
            )
        elif role == "player":
            self.status_vars["role"].set("玩家 Player")
            # 本机视角：已经和中继配对成功的 Minecraft 连接数。
            self.status_vars["local_links"].set(
                str(snapshot.get("active_connections", 0))
            )
            self.status_vars["traffic"].set(traffic_ledger_text(snapshot))
        else:
            self.status_vars["role"].set("—")
            self.status_vars["players"].set("—")
            self.status_vars["local_links"].set("—")
            # 审计问题 6：累计流量是**进程级**的，离开房间/没会话时也要显示
            # （只是没有会话相关的隧道信息）。
            self.status_vars["traffic"].set(traffic_ledger_text(snapshot))

        # 实时速度：任何模式 / 有没有会话都显示真实数字（没流量就是 0），
        # 不再是 `—`（审计问题 6）。
        self.status_vars["speed"].set(traffic_speed_text(snapshot))

        # 状态卡里的「房间」显示的是【房间名】（人在房间卡片里输入的那个），
        # 不是协议里的房间 ID —— 会话里的 snapshot["room"] 是房间 ID。
        room_state = self.engine.room_state()

        if room_state is not None and room_state.display:
            self.status_vars["room"].set(room_state.display)
        else:
            self.status_vars["room"].set(snapshot.get("room", "—") or "—")

        # ---- V3 counts ----
        #
        # Three different questions, deliberately shown separately:
        #   房间内在线 = how many people are IN the room (owner included,
        #                regardless of whether they are playing)
        #   已加入世界 = how many are actually in somebody's world (the host
        #                counts as one, plus every paired player) -- the RELAY's
        #                number, so host and players read the same value
        #   本地连接   = the local view: how many Minecraft sessions this machine
        #                has with the relay / the room capacity
        #
        # The room counts come from the room membership channel, not from the
        # Minecraft session, so they are correct even before anybody starts
        # playing.
        if room_state is None:
            self.status_vars["room_members"].set("—")
        else:
            self.status_vars["room_members"].set(
                f"{room_state.members} 人"
                + ("　(你是房间所有者)" if room_state.is_owner else "")
            )

        # ---- 已加入世界：只有一个来源 ----
        #
        # 这个数字必须和别的客户端显示的一样，所以它以中继为准：HOST_STATE
        # 里的 world= 是中继自己数出来的"真的在世界里"的人数（房主算一个，
        # 每条完成握手的隧道再加一个）。房主本地那条 len(active_sessions)
        # 只是"本机有几个会话"，它属于「本地连接」那一行，不能拿来当
        # 「已加入世界」——同一个房间两个客户端显示两个数字就是这么来的。
        #
        # 万一拿不到房间成员通道（旧中继没有 V3、或者还没进房间）才退回
        # 本会话自己的数字；一个数字都没有时显示「—」，绝不显示 0。
        if room_state is not None:
            self.status_vars["players"].set(str(room_state.world_players))
        else:
            world = snapshot.get("players")
            self.status_vars["players"].set(
                str(world) if world is not None else "—"
            )

        # ---- 状态文字的颜色（用户要"正常绿 / 警告黄 / 错误红"更明显）----
        #
        # 只改颜色，不改文字：一眼能看出当前是正常还是出问题了。
        with contextlib.suppress(Exception):
            state_text = str(self.status_vars["state"].get())

            if any(word in state_text for word in ("错误", "失败", "异常")):
                self.status_labels["state"].configure(fg=BAD)
            elif any(word in state_text for word in ("连接中", "等待", "重连", "测试")):
                self.status_labels["state"].configure(fg=WARN)
            elif any(word in state_text for word in ("运行中", "在线", "已连接")):
                self.status_labels["state"].configure(fg=OK)
            else:
                self.status_labels["state"].configure(fg=FG)

            # 延迟：优秀绿 / 一般黄 / 差红
            ping_text = str(self.status_vars["ping"].get())

            if "ms" in ping_text:
                with contextlib.suppress(Exception):
                    value = float(ping_text.split()[0])

                    if value < 80:
                        self.status_labels["ping"].configure(fg=OK)
                    elif value < 200:
                        self.status_labels["ping"].configure(fg=WARN)
                    else:
                        self.status_labels["ping"].configure(fg=BAD)
            else:
                self.status_labels["ping"].configure(fg=FG_MUTED)

        # ---- latency -------------------------------------------------
        #
        # The player side DOES have a measurement: its local proxy talks to the
        # relay too, and the log prints the number. The card used to force "—"
        # for players anyway, which looked like "the feature is missing here"
        # while the log right below it showed a value.
        #
        # So: show whatever the session measured. Only when there is genuinely
        # no reading does it stay a dash.
        ping = snapshot.get("relay_ping_ms")

        # 玩家端那条数据连接上不能发 PING（CONNECTED 之后全是原始字节），
        # 所以退回用 presence 通道量出来的往返时间 —— 都是"本机 ↔ 中继"。
        if role == "player" and ping is None:
            ping = self.engine.presence_rtt_ms()

        # 房间成员连接自己的 PING/PONG 也是**真实**的「本机 ↔ 中继」往返（中继
        # 把 token 原样回给我们，见 room.py 的 PONG 处理）。会话还没开始 / 玩家
        # 那条连接不能发 PING 时用它 ——「中转延迟始终有显示」就是靠这一条兜底。
        if ping is None:
            ping = self.engine.room_rtt_ms()

        if role == "player" and ping is None:
            self.status_vars["ping"].set("—")
            with contextlib.suppress(Exception):
                self.ping_label.configure(fg=FG_MUTED)
        else:
            self.status_vars["ping"].set(fmt_ping(ping))

            quality_text, quality_color = ping_quality(ping)

            if active and ping is not None:
                self.status_vars["ping"].set(
                    f"{fmt_ping(ping)}  ({quality_text})"
                )

            with contextlib.suppress(Exception):
                self.ping_label.configure(
                    fg=quality_color if active else FG_MUTED
                )

        # ---- 对端延迟（玩家 ↔ 主机）----
        #
        # 和中转延迟分成两行是刻意的：中转延迟总是有（没房间/没会话才是「—」），
        # 对端延迟**不适用时写 N/C**，不拿中转的数字冒充直连。
        # `view`/`room_punch` 和上面「通路」「打洞进度」是同一份数据。
        peer_rtt, peer_applicable = self._status_peer_latency(view, room_punch)

        self.status_vars["peer_ping"].set(
            peer_latency_text(peer_rtt, peer_applicable)
        )
        self._set_status_color(
            "peer_ping",
            ping_quality(peer_rtt)[1]
            if (peer_applicable and peer_rtt is not None)
            else FG_MUTED,
        )

        if self.running and role is None:
            # Session is gone (stopped): do not leave stale speed numbers.
            self.status_vars["speed"].set("—")

        # ---- colour by meaning ----
        #
        # Everything used to be plain white, so "运行中" and "错误" looked the
        # same until you actually read them. Role and room get the accent
        # colour, the live numbers get green when they are actually moving,
        # and a dash means "nothing to show" and stays muted.
        self._set_status_color("role", ACCENT if active else FG_MUTED)

        room_text = self.status_vars["room"].get()
        self._set_status_color(
            "room",
            ACCENT if (active and room_text not in ("—", "")) else FG_MUTED,
        )

        moving = state == STATE_ONLINE

        self._set_status_color("room_members", ACCENT if room_state else FG_MUTED)
        self._set_status_color("players", OK if (active or room_state) else FG_MUTED)
        self._set_status_color("local_links", OK if active else FG_MUTED)
        self._set_status_color("speed", OK if moving else FG_MUTED)
        self._set_status_color("traffic", FG_DIM if active else FG_MUTED)

        # ---- 「删除 / 关闭房间」按钮什么时候能点 ----
        #
        # 以前只有"正在连接"时才能点，于是在房间里（还没开始连接、或者已经
        # 停止）就完全点不了 —— 看起来像客户端没有删房功能。
        # 有账号 + （正在连接 或 在某个房间里）就够了；真正能不能删由中继
        # 判定（所有者或主机），被拒绝时会把服务器的原话显示出来。
        can_delete = (self.auth is not None) and (
            self.running or room_state is not None
        )

        self.delete_room_button.configure(
            state="normal" if can_delete else "disabled"
        )

        # Ping colour is decided by ping_quality() above; when there is no
        # reading at all it must not stay green from a previous tick.
        if role == "player" or ping is None or not active:
            self._set_status_color("ping", FG_MUTED)

        # Acknowledge the state in the header.
        #
        # 错误状态必须【先】判断：会话自己停了的时候 self.running 还是 True，
        # 后面两个分支都不命中，页眉就会一直挂着「已连接中转服务器」——
        # 用户看到的是"连着呢"，实际中继已经把我们拒之门外。
        if state == STATE_ERROR:
            self.server_status_label.configure(
                text=tr("● 服务器不可用"), fg=BAD
            )
        elif self.running and state == STATE_ONLINE:
            self.server_status_label.configure(
                text=tr("● 已连接中转服务器"), fg=OK
            )
        elif self.running and state == STATE_CONNECTING and role:
            self.server_status_label.configure(
                text=tr("● 正在连接 ..."), fg=WARN
            )

        # Live hint with the exact address for players.
        if role == "player" and state == STATE_ONLINE:
            endpoint = snapshot.get("local_endpoint", "")
            if endpoint:
                self.connect_hint.configure(
                    text=tr(f"在 Minecraft 中连接：{endpoint}"),
                    fg=OK,
                )
        elif role == "host" and state == STATE_ONLINE:
            self.connect_hint.configure(
                text=(
                    f"等待玩家加入。房间号 {snapshot.get('room', '')}；"
                    f"本机 MC {snapshot.get('mc_endpoint', '')}"
                ),
                fg=FG_DIM,
            )

        # Reset the button state if the session silently died.
        #
        # ★ P3（2026-10-03 热切换分析）：模式切换会**短暂**把会话拆掉
        #   （role=None / STATE_IDLE），这不是掉线。在这里清 `running` 会让
        #   后面三条重建路径（`_sync_session_mode` / `_maybe_rebuild_for_mode_change`
        #   / 中继广播那条）全部拒绝执行 —— 现场表现就是"房主自己重连了、
        #   host 没重连、状态也不释放"。模式切换窗口里不许清。
        if (
            self.running
            and role is None
            and state == STATE_IDLE
            and not getattr(self, "_mode_switch_silent", False)
            and not getattr(self, "_mode_rebuild_pending", False)
        ):
            self.running = False
            self._set_enabled(self.start_button, True)
            self._set_enabled(self.stop_button, False)


    def on_close(self):
        if self.running:
            if not messagebox.askokcancel(
                "退出",
                "当前连接仍在运行，确定要退出吗？",
            ):
                return

        # Persist the form values (best effort, must never block exit).
        with contextlib.suppress(Exception):
            self._config_file_path = cfgmod.save_config(self._collect_config())

        # ---- leave the room, so the host slot frees immediately ----
        #
        # Closing the window is how most people leave. The relay would notice
        # eventually via its watchdog, but "eventually" means the host slot
        # stays locked and nobody else can start playing. A polite LEAVE makes
        # it instant.
        with contextlib.suppress(Exception):
            if self.engine.room_current() is not None:
                self.log("leaving the room ...", kind="dim")
                self.engine.close_room(timeout=5)

        # ---- 上线状态：先关连接 ----
        #
        # 面板的「在线账号」按真实连接统计。这条连接一断，面板立刻显示离线；
        # 正常关窗走这条路，强杀进程则由对端读到 EOF 自动生效。
        with contextlib.suppress(Exception):
            self.engine.stop_presence(timeout=3)

        # ---- say goodbye to the account session ----
        #
        # Closing the window used to leave two traces behind on the server:
        #
        #   * the room: handled above and by the member watchdog.
        #   * the session token: NOT handled, so the panel counted the account
        #     as online for the token's full 12 hour life.
        with contextlib.suppress(Exception):
            if self.auth is not None:
                self.log("notifying the server of logout ...", kind="dim")
                self.engine.logout_session(
                    self.host_var.get().strip(),
                    self._port_or_default(),
                    self.auth,
                    self.client_id_var.get().strip(),
                    timeout=5,
                )

        # ============================================================================
        #  ★ 用户 2026-10-03：退出**不许"未响应一下"**
        # ============================================================================
        #
        # 上面这些收尾（离开房间 / 下线 / 登出 / 停会话）每一个都带秒级超时，
        # 在 UI 线程上串起来就是"点了叉，窗口卡住几秒才关"。用户口径：可以挪到
        # 后台、也可以给个"退出中"的状态，**但不能影响正常功能**。
        #
        # 做法：先把窗口画成"正在退出…"（立刻反馈），然后**在后台线程**跑收尾，
        # 完成后回 UI 线程销毁窗口；同时挂一个 8 秒硬兜底 —— 收尾再慢，窗口也
        # 一定会关，绝不会永久卡住。
        # ★ 2026-10-03（退出分析结论）：**先把要用的 tk 值快照下来**，
        #   再立刻销毁窗口 —— 销毁之后所有 tk 变量都失效了。
        host = ""
        port = 0
        client_id = ""
        with contextlib.suppress(Exception):
            host = self.host_var.get().strip()
        with contextlib.suppress(Exception):
            port = self._port_or_default()
        with contextlib.suppress(Exception):
            client_id = self.client_id_var.get().strip()

        auth = self.auth
        had_room = False
        with contextlib.suppress(Exception):
            had_room = self.engine.room_current() is not None
        running = bool(self.running)

        with contextlib.suppress(Exception):
            self.log("exiting ...", kind="dim")
            logmod.event(
                "client.exit",
                room=self._current_room_name() or "",
                running=running,
            )

        with contextlib.suppress(Exception):
            self.root.title(f"{self.root.title()} — {tr('正在退出…')}")
            self.root.update_idletasks()

        import threading

        # **窗口立刻关**（Tk 线程、同步）。以前 `close_room(timeout=5)` 和
        # `logout_session(timeout=5)` 都在 Tk 线程上跑 —— 中继不可达时正好各等满
        # 5 秒，就是用户看到的"未响应一下"。现在这两个也挪到后台。
        with contextlib.suppress(Exception):
            self.root.destroy()

        def _finish_exit():
            # 后台线程只碰引擎和日志文件，**绝不碰 tkinter**
            # （本文件 9237 那条教训：后台碰 tk 会抛 main thread is not in main loop）。
            if had_room:                      # 礼貌 LEAVE，主机位立刻空出来
                with contextlib.suppress(Exception):
                    self.engine.close_room(timeout=5)

            with contextlib.suppress(Exception):
                self.engine.stop_presence(timeout=3)

            if auth is not None:              # 登出（新开 TCP，慢，所以放这儿）
                with contextlib.suppress(Exception):
                    self.engine.logout_session(
                        host, port, auth, client_id, timeout=5
                    )

            with contextlib.suppress(Exception):
                self.engine.shutdown()

            # Windows：释放日志文件句柄（否则日志文件删不掉）。
            with contextlib.suppress(Exception):
                logmod.close()

        self._exit_thread = threading.Thread(
            target=_finish_exit, name="exit", daemon=True
        )
        self._exit_thread.start()

