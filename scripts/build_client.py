"""Build both Windows clients from this checkout; never relabel old binaries."""
import os
import argparse
import hashlib
import json
import platform
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / "client"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT,
                        help="交付 EXE 的目录；根目录客户端正在运行时可另存")
    args = parser.parse_args()
    output = args.output_dir.resolve()
    if not output.is_relative_to(ROOT):
        raise SystemExit("EXE 输出目录必须位于当前交付包内")
    output.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        raise SystemExit("Windows 客户端必须在 Windows 上构建")
    sys.path.insert(0, str(CLIENT))
    from mclink.config.settings import APP_VERSION, APP_REVISION
    import PyInstaller
    import PIL
    version_tuple = tuple(int(v) for v in APP_VERSION.split(".")) + (0,)
    log_dir = ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / "client-build.log"
    print(f"Building CraftLink {APP_VERSION}; PyInstaller {PyInstaller.__version__}; Pillow {PIL.__version__}")
    artifacts = []
    # Work/spec files stay in a freshly allocated temp directory, outside the
    # delivery tree. The final executables are replaced only after each build.
    with tempfile.TemporaryDirectory(prefix="craftlink-v5-build-") as temporary:
        tmp = Path(temporary).resolve()
        if tmp.parent != Path(tempfile.gettempdir()).resolve():
            raise RuntimeError("unexpected temporary build directory")
        resource = tmp / "version.txt"
        resource.write_text(f'''VSVersionInfo(
  ffi=FixedFileInfo(filevers={version_tuple!r}, prodvers={version_tuple!r},
      mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'CraftLink'),
      StringStruct('FileDescription', 'CraftLink Minecraft 联机工具'),
      StringStruct('FileVersion', '{APP_VERSION}'),
      StringStruct('ProductVersion', '{APP_VERSION}'),
      StringStruct('ProductName', 'CraftLink')])]),
      VarFileInfo([VarStruct('Translation', [1033, 1200])])])
''', encoding="utf-8")
        with log_path.open("w", encoding="utf-8") as log:
            for name, console in (("CraftLink", False), ("CraftLink-console", True)):
                command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                    "--onefile", "--name", name, "--distpath", str(tmp / "dist"),
                    "--workpath", str(tmp / "work" / name), "--specpath", str(tmp),
                    "--paths", str(CLIENT), "--collect-all", "customtkinter", "--version-file", str(resource),
                    "--icon", str(CLIENT / "mclink/assets/app.ico"),
                    "--add-data", str(CLIENT / "mclink/assets") + os.pathsep + "mclink/assets",
                    "--console" if console else "--windowed", str(CLIENT / "main.py")]
                result = subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
                log.flush()
                if result.returncode:
                    print("\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-35:]))
                    return result.returncode
                built = tmp / "dist" / (name + ".exe")
                report = tmp / (name + "-self-test.json")
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                checked = subprocess.run([str(built), "--self-test-report", str(report)],
                    cwd=tmp, creationflags=flags, capture_output=True, timeout=45)
                state = json.loads(report.read_text(encoding="utf-8")) if report.exists() else {}
                if (checked.returncode or state.get("version") != APP_VERSION
                        or state.get("revision") != APP_REVISION or state.get("self_test") != "ok"):
                    print(f"{name} frozen self-test failed: {checked.stderr.decode(errors='replace')[-2000:]}")
                    return 1
                destination = output / built.name
                destination.write_bytes(built.read_bytes())
                artifacts.append({"file": destination.relative_to(ROOT).as_posix(), "bytes": destination.stat().st_size,
                    "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
                    "self_test": state})
                print(f"Built and self-tested: {destination.name} ({destination.stat().st_size} bytes)")
    git_state = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True)
    manifest = {"source_commit": git_state.stdout.strip() if git_state.returncode == 0 else None, "version": APP_VERSION, "revision": APP_REVISION, "python": platform.python_version(),
                "pyinstaller": PyInstaller.__version__, "pillow": PIL.__version__,
                "platform": platform.platform(), "artifacts": artifacts}
    (ROOT / "docs").mkdir(exist_ok=True)
    (ROOT / "docs/client-artifacts.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Build log: {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
