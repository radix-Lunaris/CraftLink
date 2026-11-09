"""Address privacy without resetting existing client identities or settings."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import make_build
import check_secrets


class PrivateProfileTests(unittest.TestCase):
    def read_defaults(self, profile='', extra=None, load_existing=False):
        env = dict(os.environ, PYTHONPATH=str(ROOT / 'client'), MCLINK_CLIENT_PROFILE=profile, PYTHONUTF8='1')
        for key in ('MCLINK_DEFAULT_SERVER_HOST', 'MCLINK_DEFAULT_SERVER_PORT', 'MCLINK_UPDATE_URL'):
            env.pop(key, None)
        env.update(extra or {})
        code = "from mclink.config import defaults as d; import json; print(json.dumps([d.DEFAULT_SERVER_HOST,d.DEFAULT_SERVER_PORT,d.UPDATE_URL,d.update_url('file:///invalid')]))"
        if load_existing:
            code = "from mclink.config.settings import load_config; import json; c=load_config(); print(json.dumps([c.server_host,c.server_port,c.client_id]))"
        return subprocess.run([sys.executable, '-B', '-c', code], env=env, cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=15)

    def test_public_defaults_do_not_require_private_files(self):
        result = self.read_defaults()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), ['relay.example.invalid', 19100, 'https://updates.example.invalid', 'https://updates.example.invalid'])

    def test_private_profile_and_environment_priority(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'client-profile.local.json'
            path.write_text(json.dumps({'server_host': 'private.example.test', 'server_port': 29100, 'update_url': 'https://updates.example.test'}), 'utf-8')
            result = self.read_defaults(str(path))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), ['private.example.test', 29100, 'https://updates.example.test', 'https://updates.example.test'])
            result = self.read_defaults(str(path), {'MCLINK_DEFAULT_SERVER_HOST': 'override.example.test', 'MCLINK_DEFAULT_SERVER_PORT': '39100', 'MCLINK_UPDATE_URL': 'https://override.example.test'})
            self.assertEqual(json.loads(result.stdout)[:3], ['override.example.test', 39100, 'https://override.example.test'])

    def test_bad_explicit_profile_fails_without_echoing_content(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'client-profile.local.json'
            for data in ('not-json', '{"server_port":0}', '{"update_url":"https://user:secret@example.test"}', '{"password":"private-value"}'):
                path.write_text(data, 'utf-8')
                result = self.read_defaults(str(path))
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('private-value', result.stderr)
                self.assertNotIn('user:secret', result.stderr)
            result = self.read_defaults(str(path.parent / 'missing.json'))
            self.assertNotEqual(result.returncode, 0)

    def test_existing_user_settings_win_over_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); config = root / 'MC-Link/mclink_config.json'; config.parent.mkdir()
            config.write_text(json.dumps({'server_host':'saved.example.test','server_port':29100,'client_id':'unchanged-identity'}), 'utf-8')
            result = self.read_defaults(extra={'APPDATA':directory,'LOCALAPPDATA':directory,'XDG_CONFIG_HOME':directory}, load_existing=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), ['saved.example.test',29100,'unchanged-identity'])

    def test_ignore_and_package_rules_cover_private_profiles(self):
        names = ['client-profile.local.json', 'nested/address.local.json', 'publication-denylist.json', 'private/addresses.txt', 'copy.private.json', 'history.bundle', 'id_ed25519']
        result = subprocess.run(['git','-C',str(ROOT),'check-ignore','-z','--stdin'], input='\0'.join(names)+'\0', capture_output=True, text=True)
        self.assertEqual(set(filter(None,result.stdout.split('\0'))), set(names))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in names+['client-profile.example.json']:
                path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('{}','utf-8')
            self.assertEqual(make_build.package_files(str(root)), ['client-profile.example.json'])

    def test_domain_scan_is_case_insensitive_and_masks_values(self):
        self.assertTrue(check_secrets.findings('innocent.md',b'HTTPS://PRIVATE.EXAMPLE.TEST/path',['private.example.test']))
        self.assertFalse(check_secrets.findings('innocent.md',b'https://public.example.test',['private.example.test']))


if __name__ == '__main__':
    unittest.main()
