"""Independent dependency invalidation and consumed-source consistency tests."""

import json
import tempfile
import unittest
from pathlib import Path

from legacygraph.clang_parser import ClangParser, is_libclang_available
from legacygraph.models import BuildContextSpec, ParseStatus, TranslationUnitSpec
from legacygraph.scanner import scan_repository


@unittest.skipUnless(is_libclang_available()[0], 'Optional libclang semantic dependency is not installed')
class IncrementalReviewTests(unittest.TestCase):
    def test_new_header_in_earlier_include_directory_invalidates_consumers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'first').mkdir()
            (root / 'second').mkdir()
            (root / 'second' / 'value.h').write_text('inline int selected() { return 1; }\n', encoding='utf-8')
            (root / 'main.cpp').write_text('#include "value.h"\nint run() { return selected(); }\n', encoding='utf-8')
            manifest = root / 'manifest.json'
            manifest.write_text(json.dumps({
                'contexts': [{'id': 'c', 'compiler_family': 'clang', 'include_dirs': ['first', 'second']}],
                'translation_units': [{'file': 'main.cpp', 'context_id': 'c'}],
            }), encoding='utf-8')
            database = root / '.legacygraph' / 'index.db'
            scan_repository(root, database, manifest)
            (root / 'first' / 'value.h').write_text('inline int selected() { return 2; }\n', encoding='utf-8')
            result = scan_repository(root, database, manifest)
            self.assertFalse(result['noop'])
            self.assertEqual(result['parsed_translation_units'], 1)
            self.assertEqual(result['failed_translation_units'], 0)

    def test_header_change_after_clang_parse_is_not_hashed_as_the_consumed_version(self):
        from clang import cindex
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            header = root / 'value.h'
            header.write_text('inline int original() { return 1; }\n', encoding='utf-8')
            (root / 'main.cpp').write_text('#include "value.h"\nint run() { return original(); }\n', encoding='utf-8')
            actual_index = cindex.Index.create()

            class MutatingIndex:
                def parse(self, *args, **kwargs):
                    translation_unit = actual_index.parse(*args, **kwargs)
                    header.write_text('inline int changed() { return 22; }\n', encoding='utf-8')
                    return translation_unit

            parser = ClangParser(root)
            parser._index = MutatingIndex()
            spec = TranslationUnitSpec('main.cpp', BuildContextSpec('p', 't', 'Debug', 'x64', compiler_family='clang'))
            result = parser.parse(spec)
            self.assertEqual(result.parse_status, ParseStatus.STALE)
            self.assertTrue(any(d.code == 'clang.source_mutated_during_parse' for d in result.diagnostics))


if __name__ == '__main__':
    unittest.main()
