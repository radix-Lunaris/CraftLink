"""Real-widget regression for the six v5.1.2 UI requests; isolated profile."""
import argparse
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'client'))


def run(capture=None):
    import customtkinter as ctk
    from PIL import ImageGrab
    from mclink.gui import app as view
    from mclink.core.connection_status import connection_rows
    with tempfile.TemporaryDirectory(prefix='mclan-512-ui-') as profile, patch.dict(os.environ,{'APPDATA':profile,'LOCALAPPDATA':profile}):
        root=ctk.CTk();root.withdraw();errors=[]
        root.report_callback_exception=lambda *args:errors.append(str(args))
        app=None
        results={}
        try:
            app=view.MCLinkApp(root)
            app._maybe_auto_refresh_room_list=lambda:None
            app._rebuilding=True
            root.geometry('1220x850+20+20');root.deiconify();root.update()
            def image(name):
                if capture:
                    capture.mkdir(parents=True,exist_ok=True)
                    root.lift();root.update();time.sleep(.3);root.update()
                    x,y=root.winfo_rootx(),root.winfo_rooty()
                    from ui_capture import save_window
                    save_window(root,capture/(name+'.png'))
            for theme in ('light','dark'):
                app.config.ui_theme=theme;app._apply_appearance();app._rebuilding=True
                assert app.header_room_label.cget('corner_radius')>=10
                assert app.role_badge.cget('corner_radius')>=10
                app.header_room_label.configure(text='◆ 示例房间',bg=view.TILE_ON,fg=view.FG)
                app.role_badge.configure(text=' 玩家 ',bg=view.CARD_ALT,fg=view.FG)
                assert app.header_room_label.cget('corner_radius')>=10
                fixtures=[]
                for i in range(18):
                    fixtures.append(SimpleNamespace(room='fixture-'+str(i),display='示例房间 '+str(i),
                        owner='房主 '+str(i),host_text='主机 '+str(i),members=3,players=2,
                        players_text=lambda:'2 / 8',state_text='已开服',))
                app._fill_room_list(fixtures);app.show_page('rooms');root.update()
                for column in app.rooms_tree['columns']:
                    assert app.rooms_tree.heading(column,'anchor')==app.rooms_tree.column(column,'anchor')
                bbox=app.rooms_tree.bbox('0','members')
                assert bbox and bbox[3]>30
                for column in app.rooms_tree['columns']:
                    cell=app.rooms_tree.bbox('0',column)
                    if cell and cell[0]+cell[2]//2<app.rooms_tree.winfo_width():
                        assert app.rooms_tree.identify_column(cell[0]+cell[2]//2)=='#'+str(app.rooms_tree['columns'].index(column)+1)
                image('rooms-'+theme)
                app.show_page('network')
                with patch.object(app.engine,'snapshot',return_value={'connection_rows':[]}):
                    app._render_hole_rows(None)
                root.update_idletasks()
                assert not app.hole_rows_frame.winfo_manager()
                state=SimpleNamespace(is_host=True,client_id='host',host_id='host',peer_ids=['a','b','c'],
                    member_names={'a':'玩家甲','b':'玩家乙','c':'未开始的成员'},
                    member_ips={'a':'203.0.113.10','b':'2001:db8::12'},
                    member_conns={'a':'connected','b':'connected'},started_ids=['a','b'],conn_mode='auto')
                rows=connection_rows(state,[dict(peer='a',active_path='direct',relay_ready=True,direct_ready=True,
                    switch_phase='direct',direct_endpoint='203.0.113.10:31000',direct_rtt_ms=24),
                    dict(peer='b',active_path='relay',relay_ready=True,direct_ready=False,switch_phase='retry_wait',relay_rtt_ms=68)])
                with patch.object(app.engine,'snapshot',return_value={'connection_rows':rows}),patch.object(app.engine,'room_state',return_value=state):
                    app._render_hole_rows(None);root.update_idletasks()
                    assert set(app._hole_rows)=={'a','b'}
                    saved=app._hole_rows['b']
                    for _ in range(5):app._render_hole_rows(None)
                    assert app._hole_rows['b'] is saved
                    assert '2001:db8::12' in saved[1].cget('text')
                    app.status_vars['state'].set('网络隧道已建立')
                    app.status_vars['path'].set('AUTO · 直连 1 人／中继 1 人')
                    app.status_vars['punch'].set('已完成 1／2，另一位玩家使用中继')
                    app._set_status_color('state',view.OK)
                    image('status-'+theme)
                with patch.object(app.engine,'snapshot',return_value={'connection_rows':[rows[1]]}),patch.object(app.engine,'room_state',return_value=state):
                    app._render_hole_rows(None);assert set(app._hole_rows)=={'b'}
                    assert app._hole_rows['b'] is saved
                with patch.object(app.engine,'snapshot',return_value={'connection_rows':[]}):
                    app._render_hole_rows(None)
                root.update_idletasks();assert not app._hole_rows
                assert not app.hole_rows_frame.winfo_manager()
                punch_end=app._punch_status_row.winfo_rooty()+app._punch_status_row.winfo_height()
                gap=app.status_labels['room'].winfo_rooty()-punch_end
                assert gap<40,gap
                for label in app.status_labels.values():assert label.cget('font')[-1]=='bold'
                assert app.status_labels['state'].cget('font')[1]>app.status_labels['role'].cget('font')[1]
                def luminance(hexcolor):
                    rgb=[int(hexcolor[i:i+2],16)/255 for i in (1,3,5)]
                    rgb=[x/12.92 if x<=.04045 else ((x+.055)/1.055)**2.4 for x in rgb]
                    return sum(x*y for x,y in zip(rgb,(.2126,.7152,.0722)))
                if theme=='light':
                    for color in (view.OK,view.WARN,view.FG_MUTED,view.NAT1,view.NAT2,view.NAT4_HARD):
                        a,b=luminance(app._status_ink(color)),luminance(view.CARD)
                        assert (max(a,b)+.05)/(min(a,b)+.05)>=4.48
                results[theme]={'rounded_badges':True,'table_alignment':True,'dynamic_rows':'0 -> 2 -> 1 -> 0','empty_gap_pixels':gap,'bold_status':True}
            original=app.on_start
            for name in app.PAGE_TITLES:
                app.show_page(name);app.running=False
                app.on_start=lambda:None
                app.start_button.invoke();assert app._active_page==name
                app.on_start=lambda:setattr(app,'running',True)
                app.start_button.invoke();assert app._active_page=='network'
                app.show_page('logs');app._refresh_status();assert app._active_page=='logs'
                app.running=False;app._refresh_room_ui()
            app.on_start=original
            app._open_member_window();win=app.member_window
            root.update();time.sleep(.3);root.update()
            assert str(win.transient())==str(root)
            assert not win.attributes('-topmost')
            assert abs((win.winfo_rootx()+win.winfo_width()/2)-(root.winfo_rootx()+root.winfo_width()/2))<40
            app._close_member_window()
            root.update();assert not errors,errors
            results.update(start_navigation='accepted click only; all five pages; no lock',members='centered owned window; not topmost',callback_errors=errors)
            print(json.dumps(results,ensure_ascii=False))
        finally:
            if app:app.engine.shutdown()
            root.destroy();logging.shutdown()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--capture',type=Path)
    run(parser.parse_args().capture)
