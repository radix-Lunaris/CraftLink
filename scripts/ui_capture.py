"""Capture only the owned application window, never a desktop rectangle."""
import sys


def save_window(window, destination):
    if sys.platform != 'win32':
        raise RuntimeError('Window-only audit captures require Windows; desktop capture is disabled.')
    from PIL import ImageGrab
    window.update_idletasks()
    # Tk's WM frame is the native outer window, not its child drawing canvas.
    image = ImageGrab.grab(window=int(window.frame(), 0))
    if image.width < 100 or image.height < 100:
        raise RuntimeError('Application window capture failed; no desktop fallback.')
    image.save(destination)
