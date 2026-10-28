"""Regression cases added during independent build-context review."""

import json
import tempfile
import unittest
from pathlib import Path

from legacygraph.build_context import load_build_manifest, split_windows_command_line
from legacygraph.models import BuildContextSpec, TranslationUnitSpec


class BuildContextReviewTests(unittest.TestCase):
    def test_windows_attached_quoted_include_is_one_argument(self):
        command = r'cl.exe /I"C:\Program Files\SDK\include" /DDEBUG /c main.cpp'
        self.assertEqual(
            split_windows_command_line(command),
            ['cl.exe', r'/IC:\Program Files\SDK\include', '/DDEBUG', '/c', 'main.cpp'],
        )

    def test_windows_escaped_quotes_and_empty_argument(self):
        self.assertEqual(
            split_windows_command_line(r'tool.exe "" /DMSG=\"hello\"'),
            ['tool.exe', '', '/DMSG="hello"'],
        )

    def test_duplicate_define_order_changes_context_identity(self):
        first = BuildContextSpec('p', 't', 'Debug', 'x64', defines=('MODE=1', 'MODE=2'))
        second = BuildContextSpec('p', 't', 'Debug', 'x64', defines=('MODE=2', 'MODE=1'))
        self.assertNotEqual(first.context_key, second.context_key)

    def test_duplicate_per_file_define_order_changes_tu_identity(self):
        context = BuildContextSpec('p', 't', 'Debug', 'x64')
        first = TranslationUnitSpec('main.cpp', context, per_file_defines=('MODE=1', 'MODE=2'))
        second = TranslationUnitSpec('main.cpp', context, per_file_defines=('MODE=2', 'MODE=1'))
        self.assertNotEqual(first.tu_key, second.tu_key)

    def test_unknown_explicit_context_id_is_not_silently_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / 'manifest.json'
            manifest.write_text(json.dumps({
                'contexts': [{'id': 'known', 'configuration': 'Release'}],
                'translation_units': [{'file': 'main.cpp', 'context_id': 'typo'}],
            }), encoding='utf-8')
            with self.assertRaises(ValueError):
                load_build_manifest(root, manifest)


if __name__ == '__main__':
    unittest.main()
