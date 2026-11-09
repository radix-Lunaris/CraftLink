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
            with self.subTest(method=name):
                actual=hashlib.sha256(ast.dump(methods[name],include_attributes=False).encode()).hexdigest()
                self.assertEqual(actual,expected)

    def test_all_network_sources_match_release_baseline(self):
        baseline=ROOT.parent/'v5.0.1Full'
        if not baseline.exists():self.skipTest('source baseline not included in standalone delivery')
        for folder in ('client/mclink/core','panel'):
            sources=[p for p in (baseline/folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts]
            self.assertTrue(sources)
            for path in sources:
                with self.subTest(file=str(path.relative_to(baseline))):
                    self.assertEqual(path.read_bytes(),(ROOT/path.relative_to(baseline)).read_bytes())
