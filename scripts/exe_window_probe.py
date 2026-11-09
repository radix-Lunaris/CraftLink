"""
Diagnose a frozen build that starts but never shows a window.

Everything is written to a log file (a windowed EXE has no console), with
timestamps so a hang is obvious.

Usage (source and frozen behave the same):
    python scripts/exe_window_probe.py
    dist\\MC-Link-console.exe --probe
"""

import os
import sys
import time

START = time.monotonic()


def log(message):
    line = f"[{time.monotonic() - START:7.2f}s] {message}"
    print(line, flush=True)

    path = os.environ.get("MCLINK_PROBE_LOG")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            pass


def main():
    log(f"start frozen={getattr(sys, 'frozen', False)} exe={sys.executable}")

    here = os.path.dirname(os.path.abspath(__file__))
    client_dir = os.path.join(os.path.dirname(here), "client")

    if os.path.isdir(client_dir) and client_dir not in sys.path:
        sys.path.insert(0, client_dir)

    log("importing tkinter")
    import tkinter as tk

    log("creating Tk root")
    root = tk.Tk()
    log(f"Tk created, screen={root.winfo_screenwidth()}x{root.winfo_screenheight()}")

    log("importing gui app")
    from mclink.gui import app as gui_app

    log("constructing MCLinkApp")
    application = gui_app.MCLinkApp(root)
    log("MCLinkApp constructed")

    log("calling run()'s pre-mainloop hooks")
    application._on_mode_change()
    application._on_role_change()
    log("hooks done")

    root.update_idletasks()
    root.update()
    log("first update done")

    root.deiconify()
    root.lift()
    log("deiconify + lift done")

    # Pump the event loop for a while exactly like mainloop() would, so a
    # crash inside a scheduled callback shows up here.
    deadline = time.monotonic() + float(
        os.environ.get("MCLINK_PROBE_SECONDS", "8")
    )
    ticks = 0

    while time.monotonic() < deadline:
        root.update_idletasks()
        root.update()
        ticks += 1
        time.sleep(0.05)

    log(f"event loop pumped {ticks} times")

    try:
        log(f"geometry={root.winfo_geometry()} mapped={root.winfo_ismapped()}")
        log(f"title={root.title()!r}")
    except Exception as e:
        log(f"introspection failed: {e}")

    root.destroy()
    log("destroyed, probe OK")

    return 0


if __name__ == "__main__":
    sys.exit(main())
