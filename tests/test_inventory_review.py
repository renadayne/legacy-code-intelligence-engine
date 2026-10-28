"""Independent inventory and publication regression checks."""

import tempfile
import unittest
from pathlib import Path

from legacygraph.discovery import compute_line_byte_offsets, inspect_source_file
from legacygraph.index import IndexStore
from legacygraph.models import BuildContextSpec, ParseRunPayload, ParseStatus, TranslationUnitSpec


class InventoryReviewTests(unittest.TestCase):
    def test_utf16_resource_line_offsets_are_original_byte_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = '#define IDC_ONE 1\r\n#define IDC_TWO 2\r\n'
            (root / 'resource.h').write_bytes(source.encode('utf-16'))
            record = inspect_source_file(root, 'resource.h')
            self.assertEqual(record.line_count, 2)
            self.assertEqual(record.line_byte_offsets, (0, 2 + len(source.splitlines(keepends=True)[0].encode('utf-16-le'))))

    def test_utf32_big_endian_line_boundaries(self):
        raw = 'a\nb\n'.encode('utf-32-be')
        self.assertEqual(compute_line_byte_offsets(raw, 'utf-32-be'), (0, 8))

    def test_recovered_or_stale_runs_do_not_publish_as_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'main.cpp').write_text('int value;\n', encoding='utf-8')
            spec = TranslationUnitSpec('main.cpp', BuildContextSpec('p', 't', 'Debug', 'x64'))
            with IndexStore(root / 'index.db') as store:
                store.initialize_schema(root)
                store.sync_file_inventory([inspect_source_file(root, 'main.cpp')])
                store.register_translation_units([spec])
                for status in (ParseStatus.PARSED_WITH_ERRORS, ParseStatus.STALE, ParseStatus.FAILED):
                    generation = store.begin_generation()
                    store.record_parse_run(generation, ParseRunPayload(spec.tu_key, 'review', '1', '1', status))
                    store.publish_generation(generation)
                    self.assertEqual(store.get_status_summary()['published_generation']['verified_tu_runs'], 0)


if __name__ == '__main__':
    unittest.main()
