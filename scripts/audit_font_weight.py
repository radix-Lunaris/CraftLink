"""Check app/dialog text weight while preserving the connection status fonts."""
import argparse
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'client'))


def run(capture=None):
    import customtkinter as ctk
    from tkinter import ttk
    from mclink.gui.app import MCLinkApp
    from mclink.gui.widgets import stronger_font
    from ui_capture import save_window
    regular = ('Microsoft YaHei UI', 13)
    bold = ('Microsoft YaHei UI', 13, 'bold')
    assert stronger_font(regular) == bold and stronger_font(bold) is bold
    with tempfile.TemporaryDirectory(prefix='craftlink-fonts-') as profile, patch.dict(os.environ, {'APPDATA':profile, 'LOCALAPPDATA':profile, 'XDG_CONFIG_HOME':profile, 'MCLINK_CLIENT_PROFILE':''}):
        root = ctk.CTk(); root.withdraw(); app = None; errors = []; counts = {}
        root.report_callback_exception = lambda *args: errors.append(str(args))
        try:
            app = MCLinkApp(root); app._rebuilding = True
            app._maybe_auto_refresh_room_list = lambda: None
            expected = {key: ('Microsoft YaHei UI', 16 if key in ('state','path','punch') else 13, 'bold') for key in app.status_labels}
            def inspect(widget):
                count = 0
                if isinstance(widget, (ctk.CTkLabel,ctk.CTkButton,ctk.CTkEntry,ctk.CTkCheckBox,ctk.CTkOptionMenu,ctk.CTkTextbox)):
                    if not isinstance(widget, ctk.CTkLabel) or widget.cget('text') or widget.cget('textvariable'):
                        font = widget.cget('font')
                        assert (font.cget('weight') == 'bold' if isinstance(font,ctk.CTkFont) else 'bold' in font[2:]), (type(widget).__name__,font)
                        count += 1
                for child in widget.winfo_children(): count += inspect(child)
                return count
            for theme in ('light','dark'):
                app.config.ui_theme = theme; app._apply_appearance(); app._rebuilding = True
                root.update_idletasks()
                assert {key:label.cget('font') for key,label in app.status_labels.items()} == expected
                assert 'bold' in str(ttk.Style(root).lookup('Craft.Treeview','font'))
                counts[theme] = inspect(root)
                assert counts[theme] > 40
                if capture:
                    capture.mkdir(parents=True,exist_ok=True); root.deiconify(); root.update()
                    for page in ('home','settings','network'):
                        app.show_page(page); root.update(); save_window(root,capture/(page+'-'+theme+'.png'))
            app._open_member_window(); root.update_idletasks()
            assert inspect(app.member_window) > 0
            app._close_member_window()
            assert not errors, errors
            print(json.dumps({'font_weight':'ok','checked_widgets':counts,'status_fonts':'unchanged','sizes_and_families':'preserved','callback_errors':errors}))
        finally:
            if app: app.engine.shutdown()
            root.destroy(); logging.shutdown()


if __name__ == '__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--capture',type=Path)
    run(parser.parse_args().capture)
