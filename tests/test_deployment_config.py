"""Deployment migration invariants, without a production Docker/DB dependency."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import contextlib
import io
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'client'))
import deploy_config as config
import migrate_config as migration
import check_secrets
import make_build


def models():
    env = dict(zip(config.DB_KEYS, ('database-custom', '3308', 'friends', 'frienduser')))
    env.update({key: 'fixture-' + key.lower() for key in config.SECRETS})
    relay = {'image': 'relay:fixture', 'container_name': 'relay-custom', 'environment': env,
             'ports': [{'target': 19100, 'published': '29100'}]}
    panel = {'image': 'panel:fixture', 'container_name': 'panel-custom', 'environment': dict(env),
             'ports': [{'target': 80, 'published': '29102', 'host_ip': '127.0.0.1'}]}
    panel['environment'].update(MCLINK_RELAY_API_URL='http://relay-custom:19101', MCLINK_PANEL_ICP='fixture-icp')
    return {'services': {'mc-panel': panel}}, {'services': {'mc-relay': relay},
        'networks': {'mclink': {'name': 'friends-network'}},
        'volumes': {'mc-link-status': {'name': 'friends-status'}}}


class DeploymentTests(unittest.TestCase):
    def test_roundtrip_idempotency_and_conflict_preserves_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env'
            values = {key: "space $ # ' \" \\ end" for key in config.SECRETS}
            config.write_env(path, values)
            self.assertEqual(config.read_env(path), values)
            before = path.read_bytes()
            config.write_env(path, values)
            self.assertEqual(path.read_bytes(), before)
            with self.assertRaises(config.ConfigError):
                config.write_env(path, {config.SECRETS[0]: 'different'})
            self.assertEqual(path.read_bytes(), before)

    def test_missing_and_conflicting_credentials_fail_read_only(self):
        pm, rm = models()
        panel, relay = pm['services']['mc-panel'], rm['services']['mc-relay']
        literal = {key: relay['environment'][key] for key in config.SECRETS}
        config.validate_models(panel, relay, literal)
        with self.assertRaises(config.ConfigError):
            config.validate_models(panel, relay, dict(literal, MCLINK_DB_HOST='unintended-environment-override'))
        for key in config.SECRETS:
            changed = copy.deepcopy(panel)
            changed['environment'][key] += '-wrong'
            with self.assertRaises(config.ConfigError):
                config.validate_models(changed, relay, literal)
        with tempfile.TemporaryDirectory() as directory, patch.object(config, 'compose_model') as compose:
            with self.assertRaises(config.ConfigError):
                config.preflight(Path(directory), Path(directory) / 'absent.env')
            compose.assert_not_called()

    def test_private_environment_inside_build_context_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(config, 'compose_model') as compose:
            root = Path(directory)
            env = root / 'panel/private.env'
            config.write_env(env, {key: 'fixture' for key in config.SECRETS})
            with self.assertRaises(config.ConfigError):
                config.preflight(root, env)
            compose.assert_not_called()

    def test_rebuild_missing_credentials_exits_before_log_or_container_actions(self):
        from test_panel_rebuild import bash_path
        if not bash_path():
            self.skipTest('Bash is required')
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / 'missing.env'
            logfile = Path(directory) / 'rebuild.log'
            env = dict(os.environ, MCLINK_HOME=str(ROOT), MCLINK_ENV_FILE=str(missing), MC_LINK_LOG=str(logfile))
            result = subprocess.run([bash_path(), (ROOT / 'rebuild.sh').as_posix(), 'all'], env=env,
                                    capture_output=True, timeout=15)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('缺少共享 .env', result.stdout.decode('utf-8'))
            self.assertFalse(logfile.exists())

    def test_migrate_exports_actual_public_and_private_values(self):
        pm, rm = models()
        with tempfile.TemporaryDirectory() as directory, patch.object(migration, 'compose_model', side_effect=[pm, rm]), patch.object(migration, 'docker_json', return_value=None):
            root = Path(directory)
            values = migration.collect(root, root / 'absent.env')
        self.assertEqual(values['MCLINK_DB_PORT'], '3308')
        self.assertEqual(values['MCLINK_PANEL_PORT'], '29102')
        self.assertEqual(values['MCLINK_RELAY_PUBLIC_PORT'], '29100')
        self.assertEqual(values['MCLINK_PANEL_CONTAINER'], 'panel-custom')
        self.assertEqual(values['MCLINK_DOCKER_NETWORK'], 'friends-network')
        self.assertEqual(values['MCLINK_PANEL_ICP'], 'fixture-icp')

    def test_running_container_drift_blocks_migration(self):
        pm, rm = models()
        live = [{'Config': {'Env': [config.SECRETS[0] + '=different']}}]
        with tempfile.TemporaryDirectory() as directory, patch.object(migration, 'compose_model', side_effect=[pm, rm]), patch.object(migration, 'docker_json', return_value=live):
            with self.assertRaises(config.ConfigError):
                migration.collect(Path(directory), Path(directory) / 'none')

    def test_custom_paths_are_absolute_and_independent_of_cwd(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / 'custom home'
            env = Path(directory) / 'private.env'
            with patch.dict(os.environ, {'MCLINK_HOME': str(home), 'MCLINK_ENV_FILE': str(env)}):
                self.assertEqual(config.paths(), (home.resolve(), env.resolve()))

    def test_public_diagnostics_never_emit_secrets(self):
        pm, rm = models()
        output = json.dumps(config.public_values(pm['services']['mc-panel'], rm['services']['mc-relay'], (pm, rm)))
        for key in config.SECRETS:
            self.assertNotIn(rm['services']['mc-relay']['environment'][key], output)
        self.assertIn('29102', output)

    def test_private_files_excluded_from_package_independently_of_git(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('.env', '.env.bak', '.env.example', 'panel/config.php', 'panel/config.local.php', 'panel/config.sample.php', 'dist/secret.txt', '.private/password.txt', 'credentials.dat', 'client/mclink_config.json', 'panel/lib/config.php'):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('fixture', 'utf-8')
            self.assertEqual(set(make_build.package_files(str(root))), {'.env.example', 'panel/config.sample.php'})

    def test_secret_scan_checks_blobs_not_redacted_names(self):
        name = 'server/docker-compose.yml'
        self.assertTrue(check_secrets.findings(name, (config.SECRETS[0] + ': fixture-secret').encode()))
        self.assertFalse(check_secrets.findings(name, (config.SECRETS[0] + ': "${' + config.SECRETS[0] + ':?Missing}"').encode()))
        self.assertTrue(check_secrets.findings('innocent.txt', b'private-fixture', ['private-fixture']))

    def test_staged_scanner_reads_index_even_after_worktree_is_cleaned(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(['git', 'init', '--quiet', str(root)], check=True, capture_output=True)
            path = root / 'compose.yml'
            fixture = 'fixture-only-not-a-live-password'
            path.write_text(config.SECRETS[0] + ': ' + fixture + '\n', 'utf-8')
            subprocess.run(['git', '-C', str(root), 'add', 'compose.yml'], check=True)
            path.write_text('# sanitized working tree\n', 'utf-8')
            output = io.StringIO()
            with patch.object(check_secrets, 'ROOT', root), patch.object(sys, 'argv', ['scan', '--staged', '--private-env', str(root / 'none')]), contextlib.redirect_stdout(output):
                self.assertEqual(check_secrets.main(), 1)
            self.assertIn('[BLOCK]', output.getvalue())
            self.assertNotIn(fixture, output.getvalue())

    def test_client_reset_and_update_use_central_defaults(self):
        from mclink.config import settings
        from mclink.gui.controller import ClientController
        from types import SimpleNamespace
        from unittest.mock import Mock
        fake = SimpleNamespace(host_var=Mock(), port_var=Mock(), log=Mock())
        ClientController.on_reset_server(fake)
        fake.host_var.set.assert_called_once_with(settings.DEFAULT_SERVER_HOST)
        fake.port_var.set.assert_called_once_with(str(settings.DEFAULT_SERVER_PORT))
        self.assertEqual(settings.update_url('https://updates.example.test/download'), 'https://updates.example.test/download')
        self.assertTrue(settings.update_url('file:///private').startswith('https://'))
        self.assertIn('cfgmod.update_url', __import__('inspect').getsource(ClientController._refuse_outdated))


if __name__ == '__main__':
    unittest.main()
