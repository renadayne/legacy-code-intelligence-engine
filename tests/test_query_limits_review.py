"""Independent check that a small query budget also bounds fetched adjacency."""

import tempfile
import tracemalloc
import unittest
from pathlib import Path

from legacygraph.discovery import inspect_source_file
from legacygraph.index import IndexStore
from legacygraph.models import (
    BuildContextSpec, EntityKind, EntityRecord, OccurrenceKind, ObservationRecord,
    ParseRunPayload, ParseStatus, RelationEvidenceRecord, RelationKind,
    RelationRecord, ResolutionStatus, DispatchKind, ExtractionQuality,
    SourceSpan, SymbolOccurrenceRecord, TranslationUnitSpec,
)
from legacygraph.queries import QueryService


class AdjacencyBudgetReviewTests(unittest.TestCase):
    def test_one_neighbor_does_not_materialize_every_callsite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'sample.cpp').write_text('void source(); void target();\n', encoding='utf-8')
            context = BuildContextSpec('p', 't', 'Debug', 'x64')
            spec = TranslationUnitSpec('sample.cpp', context)
            span = SourceSpan('sample.cpp', 0, 14, 1, 1, 1, 15)
            evidence = RelationEvidenceRecord('call', span)
            targets = tuple(EntityRecord(f'target{i}', EntityKind.FUNCTION, f'target{i}', f'target{i}')
                            for i in range(6000))
            relations = tuple(RelationRecord('source', entity.semantic_key, RelationKind.CALLS,
                                            ResolutionStatus.RESOLVED, DispatchKind.DIRECT,
                                            ExtractionQuality.SEMANTIC, evidence=(evidence,))
                              for entity in targets)
            with IndexStore(root / 'index.db') as store:
                store.initialize_schema(root)
                store.sync_file_inventory([inspect_source_file(root, 'sample.cpp')])
                store.register_translation_units([spec])
                generation = store.begin_generation()
                store.record_parse_run(generation, ParseRunPayload(
                    spec.tu_key, 'review', '1', '1', ParseStatus.PARSED_CLEAN,
                    entities=(EntityRecord('source', EntityKind.FUNCTION, 'source', 'source'),) + targets,
                    occurrences=(SymbolOccurrenceRecord('source', OccurrenceKind.DECLARATION, span),),
                    observations=(ObservationRecord('call', 'callsite', span),),
                    relations=relations,
                ))
                store.publish_generation(generation)
                service = QueryService(store)
                tracemalloc.start()
                try:
                    result = service.neighbors('source', context.context_key, limit=1)
                    _, peak = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()
                self.assertTrue(result['truncated'])
                self.assertLess(peak, 3_000_000,
                                'A limit=1 adjacency query materialized thousands of callsites before applying its budget')


if __name__ == '__main__':
    unittest.main()
