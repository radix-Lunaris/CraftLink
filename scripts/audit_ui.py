"""Exercise the real desktop without remote services; optional visual captures."""
import argparse
import contextlib
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'client'))

def run(capture=None):
    with tempfile.TemporaryDirectory(prefix='craftlink-ctk-audit-') as profile:
        with patch.dict(os.environ,{'APPDATA':profile,'LOCALAPPDATA':profile}):
            import customtkinter as ctk
            from PIL import ImageGrab
            from mclink.gui.app import MCLinkApp
            from mclink.gui import auth_ui, i18n
            root=ctk.CTk(); root.withdraw(); errors=[]
            root.report_callback_exception=lambda *exc:errors.append(str(exc))
            app=None
            def screenshot(name,window=None):
                if capture:
                    capture.mkdir(parents=True,exist_ok=True)
                    root.update(); time.sleep(.35); root.update()
                    window=window or root
                    x,y=window.winfo_rootx(),window.winfo_rooty()
                    from ui_capture import save_window
                    save_window(window,capture/(name+'.png'))
            try:
                app=MCLinkApp(root)
                # Disable only the test's periodic remote room fetch; no network fixtures.
                app._maybe_auto_refresh_room_list=lambda:None
                engine=app.engine
                assert not app._verify_form_bindings()
                for key in app.PAGE_TITLES:
                    app.show_page(key); root.update_idletasks()
                    assert app._active_page==key
                # Commit actual controller settings, then rebuild both theme/language.
                app.mc_port_var.set('25566'); app._commit_port('mc_port',app.mc_port_var)
                app.local_port_var.set('11452'); app._commit_port('local_port',app.local_port_var)
                app.max_players_var.set('64'); app._commit_max_players()
                before=dict(app._committed)
                app.room_var.set('preserved-room')
                for theme in ('light','dark','system'):
                    app.config.ui_theme=theme; app._apply_appearance(); root.update_idletasks()
                    assert app.engine is engine and app._committed==before
                    assert app.room_var.get()=='preserved-room'
                    assert not app._verify_form_bindings()
                for language in ('en','ja','zh'):
                    app.config.ui_language=language; app._apply_appearance()
                    root.update_idletasks()
                    assert app.engine is engine and app._committed==before
                    assert app.page_title.cget('text')==i18n.tr(app.PAGE_TITLES[app._active_page])
                app.config.ui_theme='dark'; app._apply_appearance()
                if capture:
                    root.deiconify(); root.geometry('1220x850+25+25'); root.update()
                    app.show_page('home'); root.update(); screenshot('desktop-dark')
                    app.show_page('network'); root.update(); screenshot('desktop-network')
                    app.config.ui_theme='light'; app._apply_appearance()
                    app.show_page('settings'); root.update(); screenshot('desktop-light-settings')
                    app.config.ui_theme='dark'; app._apply_appearance()
                app._open_member_window()
                member_window=app.member_window
                app._open_member_window()
                assert app.member_window is member_window
                row=dict(key='fixture-peer',name='Test Player',role='player',roles=['player'],
                         nat='NAT3',inbound='—',addr='203.0.113.7',ping_ms=32,
                         peer_rtt_ms=25,peer_direct=True,can_kick=True)
                app._render_member_rows([row]); root.update_idletasks()
                widgets=app.member_rows['fixture-peer']
                assert int(widgets['kick'].grid_info()['row'])==1
                app._render_member_rows([dict(row,can_kick=False)])
                assert app.member_rows['fixture-peer'] is widgets
                assert widgets['vars']['addr'].get()=='203.0.113.7'
                app._render_member_rows([]); assert not app.member_rows
                app._close_member_window()
                dialog=auth_ui.LoginDialog(root,engine,'localhost',12710,'Test Player','test-id')
                root.update_idletasks()
                if capture:
                    root.update(); screenshot('dialog-login',dialog)
                assert isinstance(dialog,ctk.CTkToplevel)
                dialog._set_busy(True); assert dialog.login_button.cget('state')=='disabled'
                dialog._set_busy(False); dialog.password_var.set('not-a-real-password')
                dialog._on_eye_click(); assert dialog.password_entry.cget('show')==''
                dialog._on_eye_click(); assert dialog.password_entry.cget('show')
                dialog._status('Test status',auth_ui.WARN)
                dialog.grab_release(); dialog.destroy()
                session=SimpleNamespace(username='Test Player',self_ops_text=lambda:'3 / 3',summary=lambda:'Test Player')
                dialog=auth_ui.RenameDialog(root,engine,'localhost',12710,session,'test-id')
                dialog._set_busy(True); dialog._set_busy(False)
                dialog._close()
                with patch.object(auth_ui.RoomsDialog,'refresh'),patch.object(auth_ui.RoomsDialog,'_schedule_auto_refresh'):
                    dialog=auth_ui.RoomsDialog(root,engine,'localhost',12710,session,'test-id')
                    root.update_idletasks(); dialog._close()
                from mclink.gui import app as app_module
                with patch.object(app_module,'acquire_single_instance',return_value=(False,'already running')),patch.object(app_module.messagebox,'showwarning'):
                    assert app_module.run()==2
                app._refresh_status(); app._refresh_room_ui(); root.update_idletasks()
                assert not errors,errors
                print(json.dumps({'desktop_audit':'ok','pages':5,'themes':3,'languages':3,
                    'dialogs':3,'form_state_preserved':True,'member_row_reuse':True,'callback_errors':errors}))
            finally:
                if app:app.engine.shutdown()
                root.destroy(); logging.shutdown()

if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--capture',type=Path)
    run(parser.parse_args().capture)
