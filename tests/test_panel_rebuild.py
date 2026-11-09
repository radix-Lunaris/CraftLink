"""Exercise the real rebuild functions with fake Docker/HTTP, without deploying."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def bash_path():
    # Windows' system bash.exe is WSL, not necessarily an installed shell.
    if os.name == "nt":
        git = shutil.which("git")
        candidate = Path(git).parent.parent / "bin/bash.exe" if git else None
        return str(candidate) if candidate and candidate.is_file() else None
    return shutil.which("bash")


@unittest.skipUnless(bash_path(), "Bash is required for rebuild behavior tests")
class PanelRebuildTests(unittest.TestCase):
    def test_legacy_entry_delegates_panel_and_preserves_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            entry = Path(directory) / "fix-panel.sh"
            entry.write_bytes((ROOT / "fix-panel.sh").read_bytes())
            (Path(directory) / "rebuild.sh").write_text(
                'printf "%s\\n" "$@"\nexit 37\n', encoding="utf-8", newline="\n")
            result = subprocess.run([bash_path(), entry.as_posix(), "--strict", "-v"],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 37)
            self.assertEqual(result.stdout.splitlines(), ["panel", "--strict", "-v"])

    def test_legacy_panel_entry_cannot_be_used_to_restart_relay(self):
        result = subprocess.run([bash_path(), (ROOT / "fix-panel.sh").as_posix(), "relay"],
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 2)

    def run_rebuild(self, scenario):
        source = (ROOT / "rebuild.sh").read_text(encoding="utf-8-sig")

        def function(name):
            start = source.index(name + " () {")
            end = source.index("\n}\n", start) + 3
            return source[start:end]

        with tempfile.TemporaryDirectory() as directory:
            for name in ("lib", "web"):
                shutil.copytree(ROOT / "panel" / name, Path(directory) / name)
            script = Path(directory) / "run.sh"
            script.write_text(
                "set -u\nVERBOSE=0\nPANEL_DIR=.\nENV_FILE=unused\nMCLINK_HOME=.\nPANEL_CONTAINER=mc-panel\nPANEL_URL=http://127.0.0.1:19102/index.php?action=login\n"
                "REBUILD_LOG=./rebuild.log\n: > actions\n: > polls\n"
                f"SCENARIO={scenario}\n"
                + ("PANEL_PREBUILT=1\n" if scenario.startswith("prebuilt_") else "")
                + function("fail") + function("say") + function("run_step")
                + function("rebuild_panel")
                + r'''
# All host checks succeed in this fixture; no real Docker or curl is called.
grep () { return 0; }
check_credentials () { return 0; }
check_env_file () { return 0; }
fix_panel_perms () { return 0; }
verify_panel_image () {
    echo verify >> actions
    [ "$SCENARIO" != prebuilt_mismatch ]
}
compose_cmd () {
    printf 'compose %s\n' "$*" >> actions
    if [ "$SCENARIO" = build_fail ] && [ "$1" = build ]; then
        echo 'fixture build failed' >&2
        return 42
    fi
    if [ "$SCENARIO" = start_fail ] && [ "$1" = up ]; then
        return 43
    fi
}
sleep () { :; }
curl () {
    echo poll >> polls
    if [ "$SCENARIO" = http_fail ]; then printf 503; return; fi
    if [ ! -e ready ]; then touch ready; printf 503; else printf 200; fi
}
docker () {
    printf 'docker %s\n' "$*" >> actions
    if [ "$SCENARIO" = prebuilt_load_fail ] && [ "$1" = load ]; then return 44; fi
    case "$*" in
        *'grep -c AUTO_REFRESH_SECONDS'*) echo 1 ;;
    esac
}
( rebuild_panel )
''', encoding="utf-8", newline="\n")
            result = subprocess.run([bash_path(), script.as_posix()], cwd=directory,
                                    text=True, encoding="utf-8", errors="replace",
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    timeout=30)
            actions = (Path(directory) / "actions").read_text(encoding="utf-8").splitlines()
            polls = (Path(directory) / "polls").read_text(encoding="utf-8").splitlines()
            return result, actions, polls

    def test_build_failure_preserves_existing_container(self):
        result, actions, polls = self.run_rebuild("build_fail")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(actions, ["compose build mc-panel"])
        self.assertEqual(polls, [])
        self.assertIn("fixture build failed", result.stdout)

    def test_start_failure_is_not_reported_as_http_failure(self):
        result, actions, polls = self.run_rebuild("start_fail")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(actions, ["compose build mc-panel", "compose up -d --no-build mc-panel"])
        self.assertEqual(polls, [])

    def test_waits_for_http_without_stopping_or_deleting_images(self):
        result, actions, polls = self.run_rebuild("success")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(len(polls), 2)
        self.assertEqual(actions[:2], ["compose build mc-panel", "compose up -d --no-build mc-panel"])
        self.assertFalse(any(" down" in a or "rmi" in a or "relay" in a for a in actions))

    def test_http_failure_has_bounded_retries_and_container_logs(self):
        result, actions, polls = self.run_rebuild("http_fail")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(len(polls), 15)
        self.assertIn("docker logs --tail 30 mc-panel", actions)

    def test_prebuilt_source_mismatch_preserves_existing_container(self):
        result, actions, polls = self.run_rebuild("prebuilt_mismatch")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(actions, ["verify"])
        self.assertEqual(polls, [])

    def test_prebuilt_load_failure_does_not_replace_container(self):
        result, actions, polls = self.run_rebuild("prebuilt_load_fail")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(len(actions), 2)
        self.assertEqual(actions[0], "verify")
        self.assertTrue(actions[1].startswith("docker load -i "))
        self.assertEqual(polls, [])

    def test_prebuilt_success_skips_compilation_and_checks_http(self):
        result, actions, polls = self.run_rebuild("prebuilt_success")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(actions[0], "verify")
        self.assertTrue(actions[1].startswith("docker load -i "))
        self.assertEqual(actions[2], "compose up -d --no-build mc-panel")
        self.assertFalse(any("compose build" in action for action in actions))
        self.assertEqual(len(polls), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
