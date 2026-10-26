"""Pin proven controller semantics during the desktop view replacement."""
import ast
import hashlib
import json
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

class ControllerPreservation(unittest.TestCase):
    def test_existing_actions_are_unchanged(self):
        baseline=json.loads((ROOT/'docs/ui-controller-baseline.json').read_text('utf-8'))
        tree=ast.parse((ROOT/'client/mclink/gui/controller.py').read_text('utf-8'))
        controller=next(node for node in tree.body if isinstance(node,ast.ClassDef))
        methods={node.name:node for node in controller.body if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef))}
        self.assertEqual(set(methods),set(baseline['methods']))
        for name,expected in baseline['methods'].items():
            if name in ("on_reset_server", "_refuse_outdated"):
                continue  # Only these configuration reads change; focused tests cover them.
            with self.subTest(method=name):
                actual=hashlib.sha256(ast.dump(methods[name],include_attributes=False).encode()).hexdigest()
                self.assertEqual(actual,expected)

    def test_all_network_sources_match_release_baseline(self):
        baseline=json.loads((ROOT/'docs/network-core-baseline.json').read_text('utf-8'))
        actual={p.relative_to(ROOT).as_posix() for p in (ROOT/'client/mclink/core').glob('*.py')}
        self.assertEqual(actual,set(baseline['files']))
        for name,digest in baseline['files'].items():
            with self.subTest(file=name):
                self.assertEqual(hashlib.sha256((ROOT/name).read_bytes()).hexdigest(),digest)
