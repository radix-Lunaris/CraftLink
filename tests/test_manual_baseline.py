"""The user's released transport core is a fixed compatibility contract."""
import hashlib
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ManualBaselineTests(unittest.TestCase):
    def test_released_network_modules_are_byte_identical(self):
        manifest = json.loads((ROOT / "docs/manual-core-baseline.json").read_text(encoding="utf-8"))
        for name, expected in manifest["sha256"].items():
            with self.subTest(module=name):
                current = hashlib.sha256((ROOT / "client/mclink/core" / name).read_bytes()).hexdigest()
                self.assertEqual(current, expected, "Released manual transport was modified")


if __name__ == "__main__":
    unittest.main()
