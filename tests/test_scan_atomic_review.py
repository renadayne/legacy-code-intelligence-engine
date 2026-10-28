"""Publication must include manifest metadata and source versions atomically."""

import json
import tempfile
import unittest
from pathlib import Path

from legacygraph.clang_parser import is_libclang_available
from legacygraph.index import IndexStore
from legacygraph.scanner import scan_repository


@unittest.skipUnless(is_libclang_available()[0], 'Optional libclang semantic dependency is not installed')
class ScanAtomicReviewTests(unittest.TestCase):
    def test_failed_scan_restores_manifest_membership_and_versions(self):
        self._check_publication(fail=True)

    def test_concurrent_reader_sees_prior_manifest_until_publication(self):
        self._check_publication(fail=False)

    def _check_publication(self, fail):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'a.cpp').write_text('int a() { return 1; }\n', encoding='utf-8')
            (root / 'b.cpp').write_text('int b() { return 1; }\n', encoding='utf-8')
            manifest = root / 'manifest.json'
            definition = {'contexts': [{'id': 'demo', 'compiler_family': 'clang'}],
                          'translation_units': [{'file': name, 'context_id': 'demo'} for name in ('a.cpp', 'b.cpp')]}
            manifest.write_text(json.dumps(definition), encoding='utf-8')
            database = root / '.legacygraph' / 'index.db'
            initial = scan_repository(root, database, manifest)
            with IndexStore(database) as store:
                prior = store.get_status_summary()
                prior_symbols = store.query_published_entities()
                prior_versions = [tuple(row) for row in store._conn.execute('SELECT id, current_version_id FROM files ORDER BY id')]
            definition['translation_units'] = definition['translation_units'][1:]
            manifest.write_text(json.dumps(definition), encoding='utf-8')
            (root / 'b.cpp').write_text('int b() { return 2; }\n', encoding='utf-8')

            def before_publish(store, generation):
                with IndexStore(database) as reader:
                    self.assertEqual(reader.get_status_summary()['active_translation_units'], 2)
                    self.assertEqual(reader.get_published_generation_id(), initial['generation_id'])
                    self.assertEqual(reader.query_published_entities(), prior_symbols)
                if fail:
                    raise RuntimeError('Injected publication failure')

            if fail:
                with self.assertRaisesRegex(RuntimeError, 'Injected publication failure'):
                    scan_repository(root, database, manifest, on_before_publish=before_publish)
            else:
                scan_repository(root, database, manifest, on_before_publish=before_publish)
            with IndexStore(database) as store:
                if fail:
                    self.assertEqual(store.get_status_summary()['active_translation_units'], prior['active_translation_units'])
                    self.assertEqual(store.query_published_entities(), prior_symbols)
                    self.assertEqual([tuple(row) for row in store._conn.execute('SELECT id, current_version_id FROM files ORDER BY id')], prior_versions)
                else:
                    self.assertEqual(store.get_status_summary()['active_translation_units'], 1)


if __name__ == '__main__':
    unittest.main()
