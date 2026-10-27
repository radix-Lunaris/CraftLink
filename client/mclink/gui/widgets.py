"""CustomTkinter view components; aliases adapt existing controller updates."""
import tkinter as tk
import customtkinter as ctk
from . import presentation as p


def stronger_font(font=('Microsoft YaHei UI', 13)):
    """Raise regular UI text one weight step, preserving sizes and bold text."""
    if isinstance(font, ctk.CTkFont):
        if font.cget('weight') == 'bold':
            return font
        result = font.copy()
        result.configure(weight='bold')
        return result
    if isinstance(font, (tuple, list)) and len(font) >= 2:
        if 'bold' in font[2:]:
            return font
        return (*font[:2], 'bold', *(part for part in font[2:] if part != 'normal'))
    return font


class Colors:
    """Translate the controller's existing text/surface colour updates."""
    def configure(self, require_redraw=False, **kwargs):
        if 'font' in kwargs:
            kwargs['font'] = stronger_font(kwargs['font'])
        if 'fg' in kwargs:
            kwargs['text_color'] = kwargs.pop('fg')
        if 'bg' in kwargs:
            kwargs['fg_color'] = kwargs.pop('bg')
            if isinstance(self, ctk.CTkButton) and 'text_color' not in kwargs:
                kwargs['text_color'] = 'white' if kwargs['fg_color'] in (p.ACCENT,p.ACCENT_DARK,p.BAD) else p.FG
        for name in ('activebackground', 'activeforeground', 'disabledforeground', 'relief', 'bd'):
            kwargs.pop(name, None)
        return super().configure(require_redraw=require_redraw, **kwargs)

    config = configure

    def cget(self, name):
        return super().cget({'fg': 'text_color', 'bg': 'fg_color'}.get(name, name))


class Label(Colors, ctk.CTkLabel):
    def __init__(self, master, **kwargs):
        for old, new in [('fg', 'text_color'), ('bg', 'fg_color')]:
            if old in kwargs:
                kwargs[new] = kwargs.pop(old)
        kwargs.setdefault('font', ('Microsoft YaHei UI', 13))
        kwargs['font'] = stronger_font(kwargs['font'])
        kwargs.setdefault('anchor', 'w')
        kwargs.setdefault('height', 24)
        super().__init__(master, **kwargs)


class Button(Colors, ctk.CTkButton):
    def __init__(self, master, text='', command=None, width=120, primary=False,
                 secondary=False, danger=False, icon='', **kwargs):
        self._primary = primary
        if 'bg' in kwargs:
            kwargs['fg_color'] = kwargs.pop('bg')
        if 'fg' in kwargs:
            kwargs['text_color'] = kwargs.pop('fg')
        kwargs.setdefault('fg_color', p.BAD if danger else (p.ACCENT if primary else p.CARD_ALT))
        kwargs.setdefault('hover_color', p.ACCENT_DARK if primary else p.BORDER)
        kwargs.setdefault('text_color', 'white' if primary or danger else p.FG)
        kwargs.setdefault('font', ('Microsoft YaHei UI', 13, 'bold' if primary else 'normal'))
        kwargs['font'] = stronger_font(kwargs['font'])
        kwargs.setdefault('height', 38)
        kwargs.setdefault('corner_radius', 9)
        super().__init__(master, text=text, command=command, width=max(64, width), **kwargs)


class Entry(ctk.CTkEntry):
    def __init__(self, master, **kwargs):
        kwargs.pop('style', None)
        kwargs.setdefault('height', 38)
        kwargs.setdefault('font', ('Microsoft YaHei UI', 13))
        kwargs['font'] = stronger_font(kwargs['font'])
        kwargs.setdefault('corner_radius', 8)
        super().__init__(master, **kwargs)


class Frame(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        if 'bg' in kwargs:
            kwargs['fg_color'] = kwargs.pop('bg')
        kwargs.setdefault('fg_color', 'transparent')
        kwargs.setdefault('corner_radius', 0)
        super().__init__(master, **kwargs)


class Card(ctk.CTkFrame):
    def __init__(self, master, title=None, **kwargs):
        kwargs.setdefault('fg_color', p.CARD)
        kwargs.setdefault('corner_radius', 14)
        kwargs.setdefault('border_width', 1)
        kwargs.setdefault('border_color', p.BORDER)
        super().__init__(master, **kwargs)
        if title:
            Label(self, text=title, font=('Microsoft YaHei UI', 15, 'bold')).pack(
                anchor='w', padx=22, pady=(18, 10))
        self.body = Frame(self)
        self.body.pack(fill='both', expand=True, padx=22, pady=(4 if title else 18, 20))


class Dialog(ctk.CTkToplevel):
    def configure(self, **kwargs):
        if 'bg' in kwargs:
            kwargs['fg_color'] = kwargs.pop('bg')
        return super().configure(**kwargs)


class EyeButton(Button):
    def __init__(self, master, revealed=False, **kwargs):
        self.revealed = revealed
        super().__init__(master, text='隐藏' if revealed else '显示', width=64, **kwargs)

    def set_revealed(self, revealed):
        self.revealed = revealed
        self.configure(text='隐藏' if revealed else '显示')


class Tooltip:
    def __init__(self, widget, text):
        self.widget, self.text, self.window = widget, text, None
        widget.bind('<Enter>', self.show, add='+')
        widget.bind('<Leave>', self.hide, add='+')

    def show(self, _event=None):
        if self.window is not None:
            return
        self.window = Dialog(self.widget)
        self.window.overrideredirect(True)
        self.window.geometry(f'+{self.widget.winfo_rootx()+12}+{self.widget.winfo_rooty()+36}')
        Label(self.window, text=self.text, wraplength=420, justify='left').pack(padx=12, pady=8)

    def hide(self, _event=None):
        if self.window:
            self.window.destroy()
            self.window = None


class TextHandler:
    def __init__(self, emit):
        self.emit = emit
    def write(self, text):
        if text.strip():
            self.emit(text.rstrip())
    def flush(self):
        pass


# Retained import names refer to CustomTkinter widgets, not the retired canvas UI.
RoundButton = Button
RoundTile = ctk.CTkFrame
class CheckBox(Colors, ctk.CTkCheckBox):
    def __init__(self,master,**kwargs):
        kwargs.pop('bg',None)
        if 'fg' in kwargs:kwargs['text_color']=kwargs.pop('fg')
        kwargs.pop('wraplength',None)
        text=kwargs.get('text','')
        if len(text)>30:text=text.replace('（','\n（',1)
        kwargs['text']=text
        kwargs['font']=stronger_font(kwargs.get('font',('Microsoft YaHei UI',13)))
        super().__init__(master,**kwargs)

