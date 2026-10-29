"""Independent review regressions for diagnostic reporting contracts."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from legacygraph.clang_parser import ClangParser, is_libclang_available
from legacygraph.index import IndexStore
from legacygraph.models import BuildContextSpec, TranslationUnitSpec, summarize_blocker_diagnostics
from legacygraph.queries import QueryService
from legacygraph.scanner import scan_repository


class BlockerEvidenceReviewTests(unittest.TestCase):
    def test_unresolvable_location_never_claims_exact_file_evidence(self):
        from legacygraph.models import SourceSpan
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'main.cpp').write_text('int value;\n', encoding='utf-8')
            parser = ClangParser(root)
            fallback = SourceSpan('main.cpp', 0, 11, 1, 1, 2, 1)
            missing_path = root / 'absent.h'
            diagnostic = SimpleNamespace(location=SimpleNamespace(
                file=SimpleNamespace(name=str(missing_path)), offset=3, line=1, column=4))
            span, quality, provenance = parser._resolve_diagnostic_location(
                diagnostic, {'main.cpp': b'int value;\n'}, 'main.cpp', fallback)
            self.assertEqual(span, fallback)
            self.assertEqual(quality, 'fallback')
            self.assertEqual(provenance, 'missing_file')
            self.assertNotIn(str(root), json.dumps((span.repo_path, quality, provenance)))

    def test_same_location_in_different_source_versions_is_not_one_blocker(self):
        base = {
            'context_key': 'generated-context', 'severity': 'error',
            'code': 'Semantic Issue', 'message': 'synthetic narrowing',
            'diagnostic_class': 'narrowing_initializer',
            'diagnostic_option': '-Wc++11-narrowing',
            'repo_path': 'shared.h', 'line': 2, 'col': 10,
            'byte_start': 20, 'byte_end': 20,
            'location_quality': 'exact', 'location_provenance': 'contained_file',
            'is_exact_location': True,
        }
        records = [
            dict(base, tu_key='one', main_file_path='one.cpp', content_sha256='a' * 64),
            dict(base, tu_key='two', main_file_path='two.cpp', content_sha256='b' * 64),
        ]
        groups = summarize_blocker_diagnostics(records)
        self.assertEqual(len(groups), 2, 'Different indexed source bytes cannot share one evidence hash.')
        records[1]['content_sha256'] = records[0]['content_sha256']
        self.assertEqual(len(summarize_blocker_diagnostics(records)), 1)

    def test_shared_blocker_tu_details_are_bounded_without_losing_counts(self):
        records = [{
            'context_key': 'generated', 'severity': 'error', 'code': 'Semantic Issue',
            'diagnostic_class': 'narrowing_initializer', 'repo_path': 'shared.h',
            'line': 1, 'col': 10, 'byte_start': 9, 'byte_end': 9,
            'location_quality': 'exact', 'location_provenance': 'contained_file',
            'content_sha256': 'a' * 64, 'tu_key': f'tu-{i:03}',
            'main_file_path': f'unit-{i:03}.cpp',
        } for i in range(105)]
        group, = summarize_blocker_diagnostics(records, max_affected_tus=3)
        self.assertEqual(group['record_count'], 105)
        self.assertEqual(group['affected_tu_count'], 105)
        self.assertEqual(group['omitted_affected_tu_count'], 102)
        self.assertTrue(group['affected_tus_truncated'])
        for key in ('affected_tu_keys', 'affected_main_files', 'affected_translation_units'):
            self.assertEqual(len(group[key]), 3)


@unittest.skipUnless(is_libclang_available()[0], 'Requires optional libclang')
class DiagnosticQueryReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'main.cpp').write_text(
            'char* text(){return "text";}\n'
            'struct Row{unsigned mask;};\n'
            'Row one{-1};\nRow two{-2};\nRow three{-3};\n', encoding='utf-8')
        manifest = self.root / 'manifest.json'
        manifest.write_text(json.dumps({'translation_units': [{'file': 'main.cpp'}]}), encoding='utf-8')
        self.db = self.root / 'generated.db'
        scan_repository(self.root, self.db, manifest)

    def test_warning_filter_never_labels_warnings_as_blockers(self):
        with IndexStore(self.db) as store:
            result = QueryService(store).diagnostics(severity='warning')
        self.assertGreater(result['total'], 0)
        self.assertEqual(result['diagnostic_summary']['blocking_diagnostic_records'], 0)
        self.assertEqual(result['blocker_summaries'], [])
        self.assertEqual(result['diagnostic_summary']['blocker_group_count'], 0)

    def test_limit_bounds_blocker_payload_as_well_as_raw_records(self):
        with IndexStore(self.db) as store:
            result = QueryService(store).diagnostics(blocking_only=True, limit=1)
        self.assertGreater(result['total'], 1)
        self.assertEqual(len(result['diagnostics']), 1)
        self.assertLessEqual(len(result['blocker_summaries']), 1)
        self.assertTrue(result['truncated'])

    def test_forced_include_name_in_comment_does_not_make_fallback_exact(self):
        (self.root / 'forced.cpp').write_text(
            '// missing_forced_header.h is deliberately absent\nint value;\n', encoding='utf-8')
        context = BuildContextSpec(
            'Generated', 'Demo', 'Debug', 'Win32', compiler_family='msvc',
            language_standard='c++14', forced_includes=('missing_forced_header.h',))
        result = ClangParser(self.root).parse(TranslationUnitSpec('forced.cpp', context))
        missing = [d for d in result.diagnostics if d.missing_dependency]
        self.assertTrue(missing)
        self.assertTrue(all(not d.is_exact_location for d in missing))


if __name__ == '__main__':
    unittest.main()
