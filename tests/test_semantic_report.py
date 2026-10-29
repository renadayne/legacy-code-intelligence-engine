"""Report regressions using invented inputs only; no SDK/private fixtures."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from legacygraph.clang_parser import ClangParser, is_libclang_available
from legacygraph.cli import main
from legacygraph.models import BuildContextSpec, TranslationUnitSpec, ParseStatus, RelationKind
from legacygraph.build_context import load_build_manifest
from legacygraph.scanner import scan_repository
from legacygraph.index import IndexStore


class ContextExplanationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_explanation_requires_no_source_native_library_or_database(self):
        manifest = self.root / 'manifest.json'
        manifest.write_text(json.dumps({
            'contexts': [{'id': 'synthetic', 'compiler_family': 'msvc',
                          'extra_flags': ['/Zc:wchar_t-'], 'defines': ['UNICODE=0']}],
            'translation_units': [{'file': 'not-created.cpp', 'context_id': 'synthetic',
                                   'per_file_flags': ['/Zc:wchar_t'],
                                   'per_file_undefines': ['_UNICODE']}]}))
        output = io.StringIO()
        with patch.object(ClangParser, '_ensure_index', side_effect=AssertionError('must not parse')):
            with contextlib.redirect_stdout(output):
                code = main(['explain-context', '--repo', str(self.root), '--manifest', str(manifest)])
        self.assertEqual(code, 0)
        row = json.loads(output.getvalue())['translation_units'][0]
        self.assertEqual(row['wchar_mode'], 'native')
        self.assertEqual(row['wchar_selection'], 'explicit')
        self.assertEqual(row['command_line_macros'], {'UNICODE': 'defined', '_UNICODE': 'undefined'})
        self.assertFalse(row['source_preprocessing_verified'])
        self.assertEqual(row['original_build_match'], 'unknown')
        self.assertNotIn('not-created', output.getvalue())
        self.assertNotIn(str(self.root), output.getvalue())
        self.assertEqual(list(self.root.iterdir()), [manifest])

    def test_unknown_context_and_empty_manifest_do_not_claim_valid_configuration(self):
        parser = ClangParser(self.root)
        spec = TranslationUnitSpec('synthetic.cpp', BuildContextSpec(
            'Synthetic', 'Demo', 'Debug', 'x64', compiler_family='clang', extra_flags=('/Zc:wchar_t-',)))
        row = parser.explain_context(spec)
        self.assertFalse(row['configuration_supported'])
        self.assertEqual(row['wchar_mode'], 'unknown')
        self.assertIn('clang.unsupported_wchar_context', row['diagnostic_codes'])
        manifest = self.root / 'empty.json'
        manifest.write_text('{"translation_units": []}')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['explain-context', '--repo', str(self.root), '--manifest', str(manifest)]), 3)


@unittest.skipUnless(is_libclang_available()[0], 'Requires optional libclang')
class SemanticReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_diagnostic_suppression_cannot_promote_invalid_source(self):
        cases = [
            ('struct Options{unsigned mask;}; Options o{-1};', '-Wno-c++11-narrowing'),
            ('struct Text{Text();Text(const Text&);~Text();}; void call(int,...); '
             'void run(){Text t;call(1,t);}', '-Wno-non-pod-varargs'),
        ]
        for source, suppression in cases:
            (self.root / 'main.cpp').write_text(source)
            for flags in ((), (suppression,), ('-Wno-error=' + suppression[5:],), ('-w',)):
                with self.subTest(flags=flags):
                    spec = TranslationUnitSpec('main.cpp', BuildContextSpec(
                        'Synthetic', 'Demo', 'Debug', 'x64', extra_flags=flags))
                    result = ClangParser(self.root).parse(spec)
                    self.assertEqual(result.parse_status,
                                     ParseStatus.FAILED if flags else ParseStatus.PARSED_WITH_ERRORS)
                    self.assertTrue(result.diagnostics)
                    self.assertFalse(result.entities)
                    self.assertFalse(result.relations)

    def test_manifest_to_arguments_to_pointer_overloads(self):
        source = '''
        #ifndef _NATIVE_WCHAR_T_DEFINED
        typedef unsigned short wchar_t;
        #endif
        typedef wchar_t* LPWSTR;
        void convert(LPWSTR);
        int run(){unsigned short buffer[2];convert(buffer);return 1;}
        '''
        (self.root / 'main.cpp').write_text(source)
        manifest = self.root / 'manifest.json'
        manifest.write_text(json.dumps({
            'contexts': [{'id': 'synthetic', 'compiler_family': 'msvc', 'extra_flags': ['/Zc:wchar_t']}],
            'translation_units': [
                {'file': 'main.cpp', 'context_id': 'synthetic'},
                {'file': 'main.cpp', 'context_id': 'synthetic', 'per_file_flags': ['/Zc:wchar_t-']}]}))
        native, typedef = load_build_manifest(self.root, manifest).translation_units
        parser = ClangParser(self.root)
        self.assertEqual(parser.explain_context(native)['wchar_mode'], 'native')
        bad = parser.parse(native)
        self.assertEqual(bad.parse_status, ParseStatus.PARSED_WITH_ERRORS)
        self.assertTrue(any('no matching function' in d.message for d in bad.diagnostics))
        self.assertEqual(parser.explain_context(typedef)['wchar_mode'], 'typedef')
        good = parser.parse(typedef)
        self.assertEqual(good.parse_status, ParseStatus.PARSED_CLEAN, good.diagnostics)
        self.assertTrue(any(r.kind == RelationKind.CALLS and 'convert#*s#' in r.target_semantic_key
                            for r in good.relations))

    def test_variadic_method_requires_a_typed_or_explicit_pointer_argument(self):
        declarations = '''struct Text{Text();Text(const Text&);~Text();
            const char* c_str() const; operator const char*() const;};
            struct Sink{void format(int,...); void typed(const Text&);};'''
        spec = TranslationUnitSpec('main.cpp', BuildContextSpec('Synthetic', 'Demo', 'Debug', 'x64'))
        for expression, status in (
            ('sink.format(1,text);', ParseStatus.PARSED_WITH_ERRORS),
            ('sink.typed(text);sink.format(1,text.c_str());', ParseStatus.PARSED_CLEAN),
        ):
            (self.root / 'main.cpp').write_text(declarations +
                'void run(){Sink sink; Text text;' + expression + '}')
            payload = ClangParser(self.root).parse(spec)
            self.assertEqual(payload.parse_status, status, payload.diagnostics)
            if status == ParseStatus.PARSED_WITH_ERRORS:
                self.assertTrue(any('variadic method' in d.message for d in payload.diagnostics))
                self.assertFalse(payload.entities)
                self.assertFalse(payload.relations)

    def test_suppression_rejection_retracts_previously_verified_tu_and_preserves_noop(self):
        (self.root / 'main.cpp').write_text('int target(){return 1;} int run(){return target();}')
        manifest = self.root / 'manifest.json'
        manifest.write_text(json.dumps({'translation_units': [{'file': 'main.cpp',
                                                              'extra_flags': ['-Wno-c++11-narrowing']}]}))
        db = self.root / 'index.db'
        # Model the old permissive policy without loading private data or old source.
        from legacygraph import clang_parser
        with patch.object(clang_parser, '_SAFE_PASSTHROUGH_FLAGS',
                          clang_parser._SAFE_PASSTHROUGH_FLAGS | {'-Wno-c++11-narrowing'}):
            old = scan_repository(self.root, db, manifest, parser_version='0.3.1')
        self.assertEqual(old['clean_translation_units'], 1)
        context_key = load_build_manifest(self.root, manifest).contexts[0].context_key
        with IndexStore(db) as store:
            self.assertTrue(store.query_published_entities(context_key=context_key))
            self.assertTrue(store.query_published_relations(context_key=context_key))
        new = scan_repository(self.root, db, manifest)
        self.assertEqual(new['failed_translation_units'], 1)
        self.assertIn('parser_version_changed', new['invalidated_translation_units'][0]['reasons'])
        with IndexStore(db) as store:
            self.assertFalse(store.query_published_relations(context_key=context_key))
            self.assertFalse(store.query_published_entities(context_key=context_key))
        # Removing a suppression for valid source restores clean verification.
        manifest.write_text(json.dumps({'translation_units': [{'file': 'main.cpp'}]}))
        self.assertEqual(scan_repository(self.root, db, manifest)['clean_translation_units'], 1)
        self.assertTrue(scan_repository(self.root, db, manifest)['noop'])
