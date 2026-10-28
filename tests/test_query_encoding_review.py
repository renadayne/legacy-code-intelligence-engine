"""Source retrieval must honor the encoding recorded by inventory."""

import tempfile
import unittest
from pathlib import Path

from legacygraph.discovery import inspect_source_file
from legacygraph.index import IndexStore
from legacygraph.models import (
    BuildContextSpec, EntityKind, EntityRecord, OccurrenceKind, ParseRunPayload,
    ParseStatus, SourceSpan, SymbolOccurrenceRecord, TranslationUnitSpec,
)
from legacygraph.queries import QueryService


class QueryEncodingReviewTests(unittest.TestCase):
    def test_cp932_source_is_not_redecoded_as_cp1252(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            text = 'void sample() { /* 日本語 */ }\n'
            raw = text.encode('cp932')
            (root / 'sample.cpp').write_bytes(raw)
            spec = TranslationUnitSpec('sample.cpp', BuildContextSpec('p', 't', 'Debug', 'x64'))
            with IndexStore(root / 'index.db') as store:
                store.initialize_schema(root)
                store.sync_file_inventory([inspect_source_file(root, 'sample.cpp', preferred_legacy_encoding='cp932')])
                store.register_translation_units([spec])
                generation = store.begin_generation()
                store.record_parse_run(generation, ParseRunPayload(
                    spec.tu_key, 'review', '1', '1', ParseStatus.PARSED_CLEAN,
                    entities=(EntityRecord('sample', EntityKind.FUNCTION, 'sample', 'sample'),),
                    occurrences=(SymbolOccurrenceRecord('sample', OccurrenceKind.DEFINITION,
                                                        SourceSpan('sample.cpp', 0, len(raw) - 1, 1, 1, 1, len(raw))),),
                ))
                store.publish_generation(generation)
                snippet = QueryService(store).snippet('sample', spec.build_context.context_key)
                self.assertEqual(snippet['encoding'], 'cp932')
                self.assertEqual(snippet['content'], text.rstrip('\n'))


if __name__ == '__main__':
    unittest.main()
