"""Semantic/preprocessor/MFC integration using the real offline parser."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from legacygraph.clang_parser import is_libclang_available
from legacygraph.index import IndexStore
from legacygraph.models import RelationKind, ResolutionStatus
from legacygraph.scanner import scan_repository


@unittest.skipUnless(is_libclang_available()[0], 'Optional libclang semantic dependency is not installed')
class MfcPipelineReviewTests(unittest.TestCase):
    def test_real_demo_binds_only_two_active_registrations(self):
        example = Path(__file__).resolve().parents[1] / 'examples' / 'mfc_demo'
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'index.db'
            scan_repository(example, database, example / 'manifest.json')
            with IndexStore(database) as store:
                context = store._conn.execute('SELECT context_key FROM build_contexts').fetchone()[0]
                mappings = store.query_published_relations(context_key=context, kind=RelationKind.HANDLES_EVENT)
                self.assertEqual(len(mappings), 2)
                self.assertTrue(all(row['resolution_status'] == ResolutionStatus.RESOLVED for row in mappings))
                self.assertTrue(all(row['condition_state'] == 'active' and row['evidence_count'] for row in mappings))
                events = {row['semantic_key']: row['qualified_name'] for row in store.query_published_entities(limit=100)}
                save_mapping = next(row for row in mappings if 'ID_FILE_SAVE' in events[row['source_semantic_key']])
                self.assertEqual(events[save_mapping['target_semantic_key']], 'COrderDlg::OnSave')

    def test_source_change_during_adapter_does_not_publish_old_semantics(self):
        import legacygraph.scanner as scanner_module
        original = scanner_module.augment_payload
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'main.cpp'
            source.write_text('int original() { return 1; }\n', encoding='utf-8')
            manifest = root / 'manifest.json'
            manifest.write_text(json.dumps({'translation_units': [{'file': 'main.cpp', 'compiler_family': 'clang'}]}), encoding='utf-8')

            def mutate(*args, **kwargs):
                result = original(*args, **kwargs)
                source.write_text('int changed() { return 2; }\n', encoding='utf-8')
                return result

            database = root / '.legacygraph' / 'index.db'
            with patch.object(scanner_module, 'augment_payload', mutate):
                result = scan_repository(root, database, manifest)
            self.assertEqual(result['failed_translation_units'], 1)
            self.assertTrue(any(item['code'] == 'scanner.source_changed_after_parse' for item in result['diagnostics']))
            with IndexStore(database) as store:
                self.assertEqual(store.query_published_entities(), [])


if __name__ == '__main__':
    unittest.main()
