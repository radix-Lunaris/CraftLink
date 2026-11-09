import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "client"), str(ROOT / "server")]
from mclink.config.settings import ClientConfig
from db_store import MemoryStore
from settings_service import SettingsService, KNOWN_SETTINGS


class ClientCapacityTests(unittest.TestCase):
    def test_default_eight_and_configurable_sixty_four(self):
        self.assertEqual(ClientConfig(room="capacity-test", role="host").validated().max_players, 8)
        config = ClientConfig(room="capacity-test", role="host", max_players=64).validated()
        self.assertEqual((config.max_players, config.max_tunnels), (64, 64))
        for value in (1, 65):
            with self.assertRaises(ValueError):
                ClientConfig(room="capacity-test", role="host", max_players=value).validated()


class SettingsCapacityTests(unittest.IsolatedAsyncioTestCase):
    async def test_save_reload_and_reject_out_of_range(self):
        store = MemoryStore()
        settings = SettingsService(store, {"max_players": 8})
        await settings.load()
        self.assertEqual(settings.get_int("max_players"), 8)
        self.assertEqual(KNOWN_SETTINGS["max_players"]["maximum"], 64)
        success, _ = await settings.set("max_players", 64)
        self.assertTrue(success)
        success, _ = await settings.set("max_players", 65)
        self.assertFalse(success)
        restored = SettingsService(store, {"max_players": 8})
        await restored.load()
        self.assertEqual(restored.get_int("max_players"), 64)
