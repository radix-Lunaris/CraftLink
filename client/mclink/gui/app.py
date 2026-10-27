"""CustomTkinter desktop views; existing actions live in controller.py."""
import contextlib
import tkinter as tk
from tkinter import ttk, messagebox
import customtkinter as ctk
from PIL import Image
from . import presentation as p, controller
from .presentation import *
from .controller import ClientController
from .widgets import Label, Button, Entry, Frame, Card, Dialog, Tooltip, EyeButton
from .widgets import RoundButton, CheckBox, RoundTile


class MCLinkApp(ClientController):
    PAGE_TITLES = {'home':'联机工作台','rooms':'房间大厅','network':'连接状态',
                   'logs':'运行日志','settings':'设置与账号'}

    def __init__(self, root):
        self._active_page = 'home'
        super().__init__(root)
        self._on_mode_change()
        self._on_role_change()
        self._refresh_room_ui()
        self._refresh_status()

    def _palette(self):
        p.apply_theme(getattr(self.config,'ui_theme','dark'))
        for name in ('BG','CARD','CARD_ALT','BORDER','FG','FG_DIM','FG_MUTED','ACCENT',
                     'ACCENT_DARK','OK','WARN','BAD','LOCKED','TILE_ON','TILE_OFF',
                     'LOG_BG','LOG_FG','HEADER_BG','SHADOW','NAT1','NAT2','NAT4_EASY','NAT4_HARD'):
            globals()[name]=getattr(p,name)
            setattr(controller,name,getattr(p,name))
        ctk.set_appearance_mode(getattr(self.config,'ui_theme','dark'))

    def _build_window(self):
        self._palette()
        self.root.title(cfgmod.version_line())
        self.root.geometry('1220x850')
        self.root.minsize(920,660)
        with contextlib.suppress(Exception):
            self.root.iconbitmap(asset_path('app.ico'))

    def _build_styles(self):
        # Native data grid, themed to match CTk; the shell and forms are CTk.
        style=ttk.Style(self.root)
        style.theme_use('clam')
        scale=getattr(self.root,'_get_window_scaling',lambda:1.0)()
        table_font=('Microsoft YaHei UI',-round(13*scale),'bold')
        style.configure('Craft.Treeview',background=CARD,fieldbackground=CARD,
                        foreground=FG,borderwidth=0,rowheight=round(36*scale),font=table_font)
        style.configure('Craft.Treeview.Heading',background=CARD_ALT,foreground=FG_DIM,
                        relief='flat',font=table_font,padding=(round(8*scale),round(6*scale)))
        style.map('Craft.Treeview',background=[('selected',ACCENT)],foreground=[('selected','white')])

    def _apply_appearance(self):
        i18n.set_language(getattr(self.config,'ui_language','zh'))
        self._palette()
        with contextlib.suppress(Exception):
            cfgmod.save_config(self.config)
        self.rebuild_ui()
        with contextlib.suppress(Exception):
            from . import auth_ui
            auth_ui.refresh_palette()

    def _button(self,parent,text,command,width=12,primary=False,secondary=False,danger=False,icon='',**kwargs):
        return Button(parent,text=tr(text),command=command,width=max(74,width*9),
                      primary=primary,secondary=secondary,danger=danger)

    def _build_layout(self):
        self._palette()
        self.root.configure(**({'fg_color':BG} if isinstance(self.root,ctk.CTk) else {'bg':BG}))
        self._tile_groups=[]
        self._config_rows=[]
        shell=Frame(self.root,fg_color=BG)
        shell.pack(fill='both',expand=True)
        shell.grid_columnconfigure(1,weight=1)
        shell.grid_rowconfigure(0,weight=1)
        self.sidebar=ctk.CTkFrame(shell,width=204,corner_radius=0,fg_color=CARD)
        self.sidebar.grid(row=0,column=0,sticky='ns')
        self.sidebar.grid_propagate(False)
        self._build_sidebar()
        right=Frame(shell)
        right.grid(row=0,column=1,sticky='nsew',padx=26,pady=20)
        right.grid_columnconfigure(0,weight=1)
        right.grid_rowconfigure(1,weight=1)
        self._build_header(right)
        self.workspace=Frame(right)
        self.workspace.grid(row=1,column=0,sticky='nsew',pady=(18,0))
        self.workspace.grid_columnconfigure(0,weight=1)
        self.workspace.grid_rowconfigure(0,weight=1)
        self.pages={}
        for name in self.PAGE_TITLES:
            page=(Frame(self.workspace) if name in ('logs','rooms') else ctk.CTkScrollableFrame(
                self.workspace,fg_color='transparent',corner_radius=0,scrollbar_button_color=BORDER))
            page.grid(row=0,column=0,sticky='nsew')
            self.pages[name]=page
        self._build_status_page(self.pages['network'])
        self._build_settings_page(self.pages['settings'])
        self._build_home(self.pages['home'])
        self._build_rooms_page(self.pages['rooms'])
        self._build_logs_page(self.pages['logs'])
        self._load_config_fields()
        for var in (self.mc_port_var,self.local_port_var,self.max_players_var):
            var.trace_add('write',lambda *_:self._on_config_edited())
        self._verify_form_bindings()
        self.show_page(self._active_page)
        i18n.translate_widget_tree(self.root)

    def _build_sidebar(self):
        brand=Frame(self.sidebar)
        brand.pack(fill='x',padx=20,pady=(28,24))
        self.logo_image=ctk.CTkImage(Image.open(asset_path('app.png')),size=(34,34))
        ctk.CTkLabel(brand,image=self.logo_image,text='').pack(side='left',padx=(0,10))
        Label(brand,text='CraftLink',font=('Segoe UI',19,'bold')).pack(anchor='w')
        Label(self.sidebar,text=f'v{cfgmod.APP_VERSION}',text_color=FG_MUTED,
              font=('Segoe UI',12)).pack(anchor='w',padx=24,pady=(0,20))
        self.nav_buttons={}
        for key,title in self.PAGE_TITLES.items():
            button=Button(self.sidebar,text=tr(title),width=164,command=lambda name=key:self.show_page(name),
                          anchor='w',fg_color='transparent',text_color=FG_DIM,height=44)
            button.pack(fill='x',padx=14,pady=4)
            self.nav_buttons[key]=button
        footer=Frame(self.sidebar)
        footer.pack(side='bottom',fill='x',padx=18,pady=22)
        self.start_button=self._button(footer,'开始连接',self._on_start_clicked,primary=True)
        self.start_button.pack(fill='x',pady=(0,8))
        self.stop_button=self._button(footer,'停止',self.on_stop)
        self.stop_button.pack(fill='x')
        self.stop_button.configure(state='disabled')
        Label(footer,text=tr('网络通道独立于游戏'),text_color=FG_MUTED,
              font=('Microsoft YaHei UI',11)).pack(pady=(16,0))

    def _build_header(self,parent):
        header=Frame(parent)
        header.grid(row=0,column=0,sticky='ew')
        header.grid_columnconfigure(0,weight=1)
        self.page_title=Label(header,text='',font=('Microsoft YaHei UI',24,'bold'))
        self.page_title.grid(row=0,column=0,sticky='w')
        self.server_status_label=Label(header,text=tr('● 未测试服务器'),text_color=WARN)
        self.server_status_label.grid(row=0,column=1,sticky='e')
        self.header_room_label=Label(header,text='',text_color=FG_DIM,fg_color=CARD_ALT,
                                     corner_radius=12,border_width=1,border_color=BORDER,height=32,
                                     font=('Microsoft YaHei UI',13,'bold'))
        self.header_room_label.grid(row=1,column=0,sticky='w',pady=(6,0))
        self.role_badge=Label(header,text=tr('未登录'),text_color=FG_MUTED,fg_color=CARD_ALT,
                              corner_radius=12,border_width=1,border_color=BORDER,height=32,
                              font=('Microsoft YaHei UI',13,'bold'))
        self.role_badge.grid(row=1,column=1,sticky='e',pady=(6,0))
        self.aurora=self._aurora_canvas=None

    def show_page(self,name):
        self._active_page=name if name in self.pages else 'home'
        for key,page in self.pages.items():
            if key==self._active_page:
                page.grid()
            else:
                page.grid_remove()
            self.nav_buttons[key].configure(fg_color=ACCENT if key==self._active_page else 'transparent',
                                            text_color='white' if key==self._active_page else FG_DIM)
        self.page_title.configure(text=tr(self.PAGE_TITLES[self._active_page]))

    def _build_home(self,parent):
        hero=Card(parent,title=tr('当前连接'))
        hero.pack(fill='x',pady=(0,16))
        Label(hero.body,textvariable=self.status_vars['state'],text_color=ACCENT,
              font=('Microsoft YaHei UI',23,'bold')).pack(anchor='w')
        Label(hero.body,textvariable=self.status_vars['path'],text_color=FG_DIM,
              wraplength=700).pack(fill='x',pady=(7,14))
        self.addr_var=tk.StringVar(value='—')
        address=Frame(hero.body)
        address.pack(fill='x')
        self.addr_label=Label(address,textvariable=self.addr_var,font=('Consolas',17),text_color=FG)
        self.addr_label.pack(side='left',fill='x',expand=True)
        self.copy_button=self._button(address,'复制',self.on_copy_address,width=7)
        self.copy_button.pack(side='right')
        self.connect_hint=Label(hero.body,text='',text_color=FG_MUTED,wraplength=700,justify='left')
        self.connect_hint.pack(fill='x',pady=(10,0))
        mode=Card(parent,title=tr('连接模式'))
        mode.pack(fill='x',pady=(0,16))
        self.mode_var=tk.StringVar(value=self.config.mode)
        self._mode_buttons={}
        row=Frame(mode.body)
        row.pack(fill='x')
        for key,title in [('relay','中继'),('stun','P2P 直连'),('auto','AUTO')]:
            button=Button(row,text=tr(title),command=lambda key=key:self._select_mode(key),width=140)
            button.pack(side='left',padx=(0,10))
            self._mode_buttons[key]=button
        self.mode_owner_label=Label(mode.body,text='',text_color=FG_MUTED,wraplength=700,justify='left')
        self.mode_owner_label.pack(fill='x',pady=(10,0))
        self.mode_hint=Label(mode.body,text='',text_color=FG_DIM,wraplength=700,justify='left')
        self.mode_hint.pack(fill='x',pady=(6,0))
        room=Card(parent,title=tr('房间与身份'))
        room.pack(fill='x',pady=(0,16))
        self.room_var=tk.StringVar(value=self.config.room)
        entry_row=Frame(room.body)
        entry_row.pack(fill='x')
        self.room_entry=Entry(entry_row,textvariable=self.room_var,placeholder_text=tr('房间名'))
        self.room_entry.pack(side='left',fill='x',expand=True,padx=(0,10))
        self.create_room_button=self._button(entry_row,'创建房间',self.on_create_room,width=10,primary=True)
        self.create_room_button.pack(side='left',padx=(0,8))
        self.join_room_button=self._button(entry_row,'加入房间',self.on_join_room,width=10)
        self.join_room_button.pack(side='left')
        self.room_entry.bind('<Return>',lambda e:self.on_join_room())
        self.room_state_label=Label(room.body,text='',wraplength=700,justify='left',text_color=FG_DIM)
        self.room_state_label.pack(fill='x',pady=(12,10))
        row=Frame(room.body)
        row.pack(fill='x')
        self.leave_room_button=self._button(row,'退出房间',self.on_leave_room,width=9)
        self.leave_room_button.pack(side='left',padx=(0,8))
        self.members_button=self._button(row,'房间成员',self.on_show_members,width=9)
        self.members_button.pack(side='left',padx=(0,8))
        self.delete_room_button=self._button(row,'删除我的房间',self.on_delete_my_room,width=13,danger=True)
        self.delete_room_button.pack(side='left')
        row=Frame(room.body)
        row.pack(fill='x',pady=(12,0))
        self.claim_host_button=self._button(row,'认领主机身份 (host)',self.on_claim_host,width=20)
        self.claim_host_button.pack(side='left',padx=(0,10))
        self.release_host_button=self._button(row,'释放主机身份',self.on_release_host,width=14)
        self.release_host_button.pack(side='left')
        Tooltip(self.release_host_button,tr('请先停止连接，再释放 Host 身份。'))

    def _select_mode(self,value):
        self.mode_var.set(value)
        self._on_mode_change()

    def _refresh_tiles(self):
        state=self.engine.room_state()
        locked=state is not None and not state.is_owner
        for value,button in getattr(self,'_mode_buttons',{}).items():
            button.configure(state='disabled' if locked else 'normal',
                             fg_color=ACCENT if value==self.mode_var.get() else CARD_ALT,
                             text_color='white' if value==self.mode_var.get() else FG)

    def _build_settings_page(self, parent):
        card=Card(parent,title=tr('中转服务器'))
        card.pack(fill='x',pady=(0,16))
        self.host_var=tk.StringVar(value=self.config.server_host)
        self.port_var=tk.StringVar(value=str(self.config.server_port))
        row=Frame(card.body); row.pack(fill='x')
        self.host_entry=Entry(row,textvariable=self.host_var)
        self.host_entry.pack(side='left',fill='x',expand=True,padx=(0,10))
        self.port_entry=Entry(row,textvariable=self.port_var,width=100)
        self.port_entry.pack(side='left')
        row=Frame(card.body); row.pack(fill='x',pady=(12,0))
        self.test_button=self._button(row,'测试服务器',self.on_test_server)
        self.test_button.pack(side='left',padx=(0,10))
        self._button(row,'恢复默认',self.on_reset_server).pack(side='left')
        card=Card(parent,title=tr('代理与房间配置'))
        card.pack(fill='x',pady=(0,16))
        self.role_var=tk.StringVar(value=self.config.role)
        self.mc_port_var=tk.StringVar()
        self.local_port_var=tk.StringVar()
        self.max_players_var=tk.StringVar()
        for key,title,hint,var,commit in [
            ('mc_port','Host 目标端口','Host 本机服务监听端口',self.mc_port_var,
             lambda:self._commit_port('mc_port',self.mc_port_var)),
            ('local_port','Player 本地代理端口','应用连接本机代理端口',self.local_port_var,
             lambda:self._commit_port('local_port',self.local_port_var)),
            ('max_players','人数上限','2–64 人（含 Host），默认 8；建房后锁定',self.max_players_var,
             self._commit_max_players)]:
            self._build_config_row(card.body,title,hint,var,commit,key)
        card=Card(parent,title=tr('账号'))
        card.pack(fill='x',pady=(0,16))
        self.account_label=Label(card.body,text=tr('未登录'),font=('Microsoft YaHei UI',16,'bold'))
        self.account_label.pack(fill='x')
        self.account_hint=Label(card.body,text='',wraplength=620,justify='left')
        self.account_hint.pack(fill='x',pady=(4,12))
        row=Frame(card.body); row.pack(fill='x')
        for attr,text,callback in [('login_button','登录',self.on_login),
                ('logout_button','退出登录',self.on_logout),('rename_button','修改用户名',self.on_rename)]:
            button=self._button(row,text,callback,primary=attr=='login_button')
            button.pack(side='left',padx=(0,10)); setattr(self,attr,button)
        card=Card(parent,title=tr('外观'))
        card.pack(fill='x',pady=(0,16))
        self.language_var=tk.StringVar(value=i18n.language_label(self.config.ui_language))
        self.theme_var=tk.StringVar(value=self._theme_label())
        for label,var,values,callback in [
            ('语言',self.language_var,[label for _code,label in i18n.LANGUAGES],self.on_language_change),
            ('主题',self.theme_var,[tr(label) for _code,label in theme_mod.THEME_LABELS],self.on_theme_change)]:
            row=Frame(card.body); row.pack(fill='x',pady=6)
            Label(row,text=tr(label),width=100).pack(side='left')
            ctk.CTkOptionMenu(row,variable=var,values=values,command=callback,
                             width=220,height=36,font=('Microsoft YaHei UI',13,'bold'),
                             dropdown_font=('Microsoft YaHei UI',13,'bold')).pack(side='left')

    def _build_config_row(self,parent,label,hint,variable,commit,key=''):
        Label(parent,text=tr(label),fg=FG_DIM).pack(fill='x',pady=(10,5))
        row=Frame(parent); row.pack(fill='x')
        entry=Entry(row,textvariable=variable)
        entry.pack(side='left',fill='x',expand=True,padx=(0,10))
        setattr(self,key+'_entry',entry)
        button=self._button(row,'确定',commit,width=7,primary=True)
        button.pack(side='left')
        entry.bind('<Return>',lambda _event:commit())
        status=Label(parent,text=tr(hint),fg=FG_MUTED,wraplength=630,justify='left')
        status.pack(fill='x',pady=(4,2))
        self._config_rows.append((variable,entry,button,status,tr(hint)))
        return entry

    def _build_rooms_page(self,parent):
        card=Card(parent,title=tr('房间大厅')); card.pack(fill='both',expand=True)
        row=Frame(card.body); row.pack(fill='x',pady=(0,14))
        self.rooms_button=self._button(row,'刷新房间',self.on_rooms_refresh)
        self.rooms_button.pack(side='left',padx=(0,10))
        self._button(row,'加入选中房间',self.on_join_selected_room,primary=True,width=16).pack(side='left')
        cols=('room','owner','host','members','players','state')
        table=Frame(card.body,height=0); table.pack(fill='both',expand=True)
        table.grid_columnconfigure(0,weight=1); table.grid_rowconfigure(0,weight=1)
        self.rooms_tree=ttk.Treeview(table,columns=cols,show='headings',height=10,style='Craft.Treeview')
        scale=getattr(self.root,'_get_window_scaling',lambda:1.0)()
        for key,title,width in zip(cols,['房间','房主','主机','房间内在线','已加入世界','状态'],[170,120,120,110,110,160]):
            anchor='center' if key in ('members','players') else 'w'
            self.rooms_tree.heading(key,text=tr(title),anchor=anchor)
            self.rooms_tree.column(key,width=round(width*scale),minwidth=round(85*scale),anchor=anchor)
        self.rooms_tree.grid(row=0,column=0,sticky='nsew')
        vertical=ctk.CTkScrollbar(table,orientation='vertical',command=self.rooms_tree.yview)
        vertical.grid(row=0,column=1,sticky='ns')
        scroll=ctk.CTkScrollbar(table,orientation='horizontal',command=self.rooms_tree.xview)
        scroll.grid(row=1,column=0,sticky='ew')
        self.rooms_tree.configure(xscrollcommand=scroll.set,yscrollcommand=vertical.set)
        self.rooms_tree.bind('<<TreeviewSelect>>',self.on_room_row_selected)
        self.rooms_tree.bind('<Double-1>',lambda _event:self.on_join_selected_room())
        for tag,color in [('busy',FG),('empty',FG_MUTED),('mine',ACCENT),('mine_busy',ACCENT)]:
            self.rooms_tree.tag_configure(tag,foreground=color)
        self.room_list_hint=Label(card.body,text=tr('点击刷新获取房间列表'),fg=FG_MUTED)
        self.room_list_hint.pack(fill='x',pady=(12,0))
        self.room_rows=[]; self._room_list_busy=False; self._room_list_filling=False
        self._room_list_failed_once=False; self._room_list_fetched_at=0

    def _build_status_page(self,parent):
        card=Card(parent,title=tr('运行状态')); card.pack(fill='x')
        self.status_vars={}; self.status_labels={}; self._hole_rows={}
        for key,title in [('state','状态'),('role','身份'),('path','通路'),('nat','网络类型'),
                ('inbound','入站可达'),('punch','打洞进度'),('room','房间'),
                ('room_members','房间内在线'),('players','已加入世界'),('local_links','本地连接'),
                ('ping','中转延迟'),('peer_ping','对端延迟'),('speed','实时速度'),('traffic','累计流量')]:
            row=Frame(card.body); row.pack(fill='x',pady=5)
            Label(row,text=tr(title),width=120,fg=self._status_ink(FG_DIM),
                  font=('Microsoft YaHei UI',13,'bold')).pack(side='left',anchor='n')
            var=tk.StringVar(value='—'); self.status_vars[key]=var
            label=Label(row,textvariable=var,wraplength=600,justify='left',
                        font=('Microsoft YaHei UI',16 if key in ('state','path','punch') else 13,'bold'))
            label.pack(side='left',fill='x',expand=True); self.status_labels[key]=label
            if key=='punch':
                self._punch_status_row=row
                self.hole_rows_frame=Frame(card.body,height=0)
        self.ping_label=self.status_labels['ping']; self.peer_ping_label=self.status_labels['peer_ping']
        self.speed_label=self.status_labels['speed']

    def _on_start_clicked(self):
        was_running=self.running
        self.on_start()
        if not was_running and self.running:
            self.show_page('network')

    def _status_ink(self,color):
        color={'ok':OK,'warn':WARN,'muted':FG_MUTED,'dim':FG_MUTED,'bad':BAD,'error':BAD,'info':ACCENT}.get(color,color)
        if not isinstance(color,str) or not color.startswith('#') or len(color)!=7:
            color=FG
        if theme_mod.resolved()!='light' or not isinstance(color,str) or len(color)!=7 or not color.startswith('#'):
            return color
        def luminance(rgb):
            linear=[v/12.92 if v<=0.04045 else ((v+0.055)/1.055)**2.4 for v in rgb]
            return sum(a*b for a,b in zip(linear,(.2126,.7152,.0722)))
        rgb=[int(color[i:i+2],16)/255 for i in (1,3,5)]
        background=luminance([int(CARD[i:i+2],16)/255 for i in (1,3,5)])
        for _ in range(24):
            ink=luminance(rgb)
            if (max(background,ink)+.05)/(min(background,ink)+.05)>=4.5:break
            rgb=[v*.9 for v in rgb]
        return '#'+''.join(f'{round(v*255):02x}' for v in rgb)

    def _set_status_color(self,key,color):
        widget=self.status_labels.get(key)
        if widget is not None:
            widget.configure(fg=self._status_ink(color))

    def _render_hole_rows(self,links,punching=False):
        snapshot=self.engine.snapshot() or {}
        state=self.engine.room_state()
        started=set(getattr(state,'started_ids',None) or [])
        conns=getattr(state,'member_conns',{}) or {}
        links=snapshot.get('connection_rows',links) or []
        links=[item for item in links if item.get('peer')
               and item.get('switch_phase') not in ('ended','reset','closed')
               and (item.get('active_path') in ('relay','direct','mixed','reconnecting')
                    or item.get('ready') or item['peer'] in started
                    or conns.get(item['peer']) in ('waiting','pairing','connected'))]
        wanted={item['peer'] for item in links}
        for peer in list(self._hole_rows):
            if peer not in wanted:self._hole_rows.pop(peer)[0].destroy()
        if not links:
            self.hole_rows_frame.pack_forget()
            return
        self.hole_rows_frame.pack(fill='x',padx=(12,0),pady=(2,4),after=self._punch_status_row)
        names=getattr(state,'member_names',{}) if state else {}
        for item in links:
            peer=item['peer']
            if 'display_text' in item:text,hint=item['display_text'],item.get('display_hint','muted')
            else:text,hint=hole_row_text(item,fallback_name=names.get(str(peer),str(peer)))
            color=self._status_ink({'ok':OK,'warn':WARN,'muted':FG_MUTED}.get(hint,FG))
            if peer not in self._hole_rows:
                row=Frame(self.hole_rows_frame,height=0)
                label=Label(row,text=text,fg=color,wraplength=520,justify='left',
                            font=('Microsoft YaHei UI',14,'bold'))
                label.pack(side='left'); self._hole_rows[peer]=(row,label)
            row,label=self._hole_rows[peer]; label.configure(text=text,fg=color)
            row.pack(fill='x',pady=2)

    def _build_logs_page(self,parent):
        row=Frame(parent); row.pack(fill='x',pady=(0,12))
        for title,callback in [('复制',self.on_copy_log),('保存日志',self.on_save_log),
                ('清空日志',self.on_clear_log),('打开配置目录',self.on_open_config_dir)]:
            self._button(row,title,callback).pack(side='left',padx=(0,10))
        self.log_text=ctk.CTkTextbox(parent,fg_color=LOG_BG,text_color=LOG_FG,
                                   font=('Consolas',12,'bold'),wrap='word',corner_radius=12)
        self.log_text.pack(fill='both',expand=True)
        for tag,color in [('normal',LOG_FG),('info',FG),('ok',OK),('warn',WARN),('error',BAD),('dim',FG_MUTED)]:
            self.log_text.tag_config(tag,foreground=color)
        self.log_text.configure(state='disabled')

    def _open_member_window(self):
        existing=getattr(self,'member_window',None)
        if existing is not None and existing.winfo_exists():
            existing.deiconify(); existing.lift(); existing.focus_set()
            return
        win=Dialog(self.root); win.title(tr('房间成员')); win.geometry('1200x650'); win.minsize(900,500)
        win.transient(self.root)
        self.member_window=win
        self.member_hint=Label(win,text='',fg=FG_DIM); self.member_hint.pack(fill='x',padx=22,pady=18)
        self.member_table=ctk.CTkScrollableFrame(win,fg_color=CARD,corner_radius=12)
        self.member_table.pack(fill='both',expand=True,padx=22,pady=(0,22))
        for index,(_key,title,width) in enumerate(MEMBER_COLUMNS):
            self.member_table.grid_columnconfigure(index,weight=1)
            Label(self.member_table,text=tr(title),fg=FG_DIM,width=max(45,width*6),
                  font=('Microsoft YaHei UI',12,'bold')).grid(row=0,column=index,sticky='ew',padx=4,pady=10)
        self.member_rows={}; self.member_names_by_key={}; self._member_list_filling=False
        win.protocol('WM_DELETE_WINDOW',self._close_member_window)
        self._refresh_member_list(force=True)
        win.update_idletasks()
        x=max(0,min(self.root.winfo_rootx()+(self.root.winfo_width()-win.winfo_width())//2,
                    win.winfo_screenwidth()-win.winfo_width()))
        y=max(0,min(self.root.winfo_rooty()+(self.root.winfo_height()-win.winfo_height())//2,
                    win.winfo_screenheight()-win.winfo_height()))
        win.geometry(f'+{x}+{y}')
        win.lift(); win.focus_set()
        win._present_after=win.after(250,lambda:self._present_member_window(win))

    def _close_member_window(self):
        win=getattr(self,'member_window',None)
        if win is not None:
            with contextlib.suppress(Exception):win.after_cancel(getattr(win,'_present_after',''))
        super()._close_member_window()

    def _present_member_window(self,win):
        if win.winfo_exists():
            win.lift(); win.focus_set()

    def _render_member_rows(self,rows):
        super()._render_member_rows(rows)
        # Place the action cell on the same shared-grid row as its member.
        for index,row in enumerate(rows):
            widgets=self.member_rows.get(row.get('key'))
            if widgets:
                widget=widgets['kick'] if row.get('can_kick') else widgets['spacer']
                widget.grid(row=index+1,column=widgets['kick_column'],padx=4,pady=8)

    def _member_row_widgets(self,key,index):
        variables={}; cells={}
        for column,(name,_title,width) in enumerate(MEMBER_COLUMNS):
            if name=='kick':continue
            var=tk.StringVar(); variables[name]=var
            label=Label(self.member_table,textvariable=var,width=max(45,width*6),
                        wraplength=max(45,width*6),justify='left',font=('Microsoft YaHei UI',12))
            label.grid(row=index+1,column=column,sticky='ew',padx=4,pady=8); cells[name]=label
        kick=self._button(self.member_table,'踢出',lambda:self.on_kick_member(key),width=6,danger=True)
        spacer=Label(self.member_table,text='',width=60)
        widgets={'frame':cells.get('name'),'cells':cells,'vars':variables,'kick':kick,
                 'spacer':spacer,'kick_column':len(MEMBER_COLUMNS)-1,'kick_visible':None}
        self.member_rows[key]=widgets
        return widgets


def run():
    ok,why=acquire_single_instance()
    if not ok:
        root=ctk.CTk(); root.withdraw()
        messagebox.showwarning('CraftLink',why,parent=root)
        root.destroy(); return 2
    root=ctk.CTk(); app=MCLinkApp(root)
    root.mainloop()
    thread=getattr(app,'_exit_thread',None)
    if thread:thread.join(timeout=4)
    return 0
