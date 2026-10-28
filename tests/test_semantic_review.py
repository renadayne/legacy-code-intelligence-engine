"""Independent semantic call-graph precision regressions."""

import tempfile
import unittest
from pathlib import Path

from legacygraph.clang_parser import ClangParser, is_libclang_available
from legacygraph.models import BuildContextSpec, ParseStatus, RelationKind, ResolutionStatus, TranslationUnitSpec


@unittest.skipUnless(is_libclang_available()[0], 'Optional libclang semantic dependency is not installed')
class SemanticReviewTests(unittest.TestCase):
    def _parse(self, text):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'sample.cpp').write_text(text, encoding='utf-8')
            spec = TranslationUnitSpec('sample.cpp', BuildContextSpec('p', 't', 'Debug', 'x64', compiler_family='clang'))
            payload = ClangParser(root).parse(spec)
            self.assertEqual(payload.parse_status, ParseStatus.PARSED_CLEAN)
            return payload

    def test_unevaluated_expressions_are_not_resolved_runtime_calls(self):
        payload = self._parse('int target(); int run() { (void)noexcept(target()); return sizeof(target()) + target(); }\n')
        entities = {entity.semantic_key: entity for entity in payload.entities}
        calls = [relation for relation in payload.relations
                 if relation.kind == RelationKind.CALLS
                 and relation.resolution_status == ResolutionStatus.RESOLVED
                 and entities[relation.source_semantic_key].qualified_name == 'run'
                 and entities[relation.target_semantic_key].qualified_name == 'target']
        self.assertEqual(len(calls), 1, 'sizeof/noexcept operands must not become confirmed direct call edges')

    def test_default_argument_is_not_a_call_executed_by_the_callee(self):
        payload = self._parse('int load(); int f(int value=load()) { return value; } int g() { return f(1); }\n')
        entities = {entity.semantic_key: entity for entity in payload.entities}
        fabricated = [relation for relation in payload.relations
                      if relation.kind == RelationKind.CALLS
                      and relation.resolution_status == ResolutionStatus.RESOLVED
                      and entities[relation.source_semantic_key].qualified_name == 'f'
                      and entities[relation.target_semantic_key].qualified_name == 'load']
        self.assertEqual(fabricated, [], 'Default argument expressions execute at call sites, not in f()')


if __name__ == '__main__':
    unittest.main()
