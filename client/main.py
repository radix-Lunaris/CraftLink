"""Source and PyInstaller entry point for CraftLink."""
import argparse
import json
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="CraftLink Minecraft 联机工具")
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--self-test", action="store_true",
                        help="验证版本、AUTO 协议和打包模块，不启动网络或界面")
    parser.add_argument("--self-test-report", type=Path,
                        help="将自检结果写入 JSON 文件（用于无控制台版本）")
    parser.add_argument("--network-report", type=Path,
                        help="逐个检测 STUN/DNS 并保存报告；不更改网络设置")
    parser.add_argument("--relay-host", help="网络诊断的中继域名；默认使用已有配置")
    args = parser.parse_args()
    from mclink.config.settings import APP_VERSION, APP_REVISION
    if args.network_report:
        import asyncio
        from mclink.config.settings import load_config
        from mclink.core.network_diagnostics import collect
        result = asyncio.run(collect(args.relay_host or load_config().server_host))
        result.update(version=APP_VERSION, revision=APP_REVISION)
        args.network_report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 0
    if args.version:
        if sys.stdout:
            print(APP_VERSION)
        return 0
    if args.self_test or args.self_test_report:
        from mclink.core.auto_stream import CAPABILITY, Decoder, Frame, Kind
        from mclink.core.auto_peer import AutoPeer
        from mclink.core.engine import MCLinkEngine
        from mclink.gui.app import MODE_AUTO, mode_owner_label_text
        frame = Frame(Kind.DATA, "0" * 32, payload=b"CraftLink AUTO self-test")
        assert Decoder().feed(frame.encode()) == [frame]
        assert MODE_AUTO == "auto"
        assert "AUTO" in mode_owner_label_text("auto", False, True)
        assert AutoPeer and MCLinkEngine
        # Frozen smoke test builds every CTk page with an isolated local profile.
        import os
        import tempfile
        from unittest.mock import patch
        import customtkinter as ctk
        from mclink.gui.app import MCLinkApp
        with tempfile.TemporaryDirectory(prefix="craftlink-frozen-ui-") as profile:
            with patch.dict(os.environ, {"APPDATA": profile, "LOCALAPPDATA": profile}):
                root = ctk.CTk()
                root.withdraw()
                app = None
                try:
                    app = MCLinkApp(root)
                    assert not app._verify_form_bindings()
                    for page in app.PAGE_TITLES:
                        app.show_page(page)
                    root.update_idletasks()
                finally:
                    if app:
                        app.engine.shutdown()
                    root.destroy()
                    import logging
                    logging.shutdown()
        result = {"ui": "customtkinter", "ui_smoke": "ok", "version": APP_VERSION, "revision": APP_REVISION,
                  "protocol": CAPABILITY, "self_test": "ok"}
        encoded = json.dumps(result, ensure_ascii=False)
        if args.self_test_report:
            args.self_test_report.write_text(encoded + "\n", encoding="utf-8")
        if sys.stdout:
            print(encoded)
        return 0
    from mclink.gui.app import run
    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
