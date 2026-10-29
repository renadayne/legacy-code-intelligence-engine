"""Independent generated x86/C++14 controls for the post-0.3.2 report."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from legacygraph.build_context import load_build_manifest
from legacygraph.clang_parser import ClangParser, is_libclang_available
from legacygraph.cli import main
from legacygraph.index import IndexStore
from legacygraph.models import BuildContextSpec, ParseStatus, TranslationUnitSpec
from legacygraph.scanner import scan_repository


class ExpectationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest = self.root / 'manifest.json'

    def explain(self, flags, per_file=(), expected='typedef', family='msvc'):
        self.manifest.write_text(json.dumps({
            'contexts': [{'id': 'generated', 'compiler_family': family,
                          'platform': 'Win32', 'language_standard': 'c++14', 'extra_flags': flags}],
            'translation_units': [{'file': 'absent.cpp', 'context_id': 'generated',
                                   'per_file_flags': per_file}]}))
        before = self.manifest.read_bytes()
        output = io.StringIO()
        with patch.object(ClangParser, '_ensure_index', side_effect=AssertionError('no parse')):
            with contextlib.redirect_stdout(output):
                code = main(['explain-context', '--repo', str(self.root),
                             '--manifest', str(self.manifest), '--expect-wchar-mode', expected])
        self.assertEqual(self.manifest.read_bytes(), before)
        self.assertEqual(list(self.root.iterdir()), [self.manifest])
        return code, json.loads(output.getvalue())['translation_units'][0]

    def test_default_mismatch_and_explicit_correction(self):
        code, row = self.explain([])
        self.assertEqual(code, 3)
        self.assertFalse(row['wchar_expectation_matches'])
        self.assertEqual(row['wchar_selection_scope'], 'default')
        self.assertIsNone(row['wchar_selected_flag'])
        code, row = self.explain(['/Zc:wchar_t-'])
        self.assertEqual(code, 0)
        self.assertTrue(row['wchar_expectation_matches'])
        self.assertEqual(row['target'], 'i686-pc-windows-msvc')
        self.assertEqual(row['language_standard'], 'c++14')
        self.assertEqual(row['wchar_selection_scope'], 'context')
        self.assertEqual(row['original_build_match'], 'unknown')
        self.assertEqual(self.explain([], expected='native')[0], 0)

    def test_per_file_override_wins_and_unsupported_is_not_a_match(self):
        code, row = self.explain(['/Zc:wchar_t-'], ['/Zc:wchar_t'])
        self.assertEqual(code, 3)
        self.assertEqual(row['wchar_selection_scope'], 'translation_unit')
        self.assertEqual(row['wchar_selected_flag'], '/Zc:wchar_t')
        self.assertEqual(self.explain(['/Zc:wchar_t'], ['/Zc:wchar_t-'])[0], 0)
        code, row = self.explain(['/Zc:wchar_t-'], family='clang')
        self.assertEqual(code, 3)
        self.assertFalse(row['wchar_expectation_matches'])
        self.assertEqual(row['wchar_mode'], 'unknown')

    def test_mixed_contexts_check_every_imported_tu(self):
        self.manifest.write_text(json.dumps({'translation_units': [
            {'file': 'one.cpp', 'extra_flags': ['/Zc:wchar_t-']},
            {'file': 'two.cpp', 'extra_flags': ['/Zc:wchar_t']}]}))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(['explain-context', '--repo', str(self.root),
                         '--manifest', str(self.manifest), '--expect-wchar-mode', 'typedef'])
        self.assertEqual(code, 3)
        rows = json.loads(output.getvalue())['translation_units']
        self.assertEqual([r['wchar_expectation_matches'] for r in rows], [True, False])


@unittest.skipUnless(is_libclang_available()[0], 'Requires optional libclang')
class ReportFollowupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def context(self, typedef=False, debug=True):
        return BuildContextSpec('Generated', 'Demo', 'Debug' if debug else 'Release', 'Win32',
                                compiler_family='msvc', language_standard='c++14',
                                defines=('UNICODE', '_UNICODE') + (('_DEBUG',) if debug else ()),
                                extra_flags=('/Zc:wchar_t-',) if typedef else ())

    def parse(self, source, typedef=False, debug=True):
        (self.root / 'main.cpp').write_text(source)
        return ClangParser(self.root).parse(TranslationUnitSpec('main.cpp', self.context(typedef, debug)))

    def assert_status(self, result, status):
        self.assertEqual(result.parse_status, status, result.diagnostics)
        if status != ParseStatus.PARSED_CLEAN:
            self.assertFalse(result.entities)
            self.assertFalse(result.relations)

    def test_missing_enum_semicolon_has_secondary_errors_in_both_modes(self):
        for typedef in (False, True):
            bad = self.parse('void finish(); void run(){enum Choice{First,Last} finish();}', typedef)
            self.assert_status(bad, ParseStatus.PARSED_WITH_ERRORS)
            self.assertTrue(any('result type' in d.message for d in bad.diagnostics))
            self.assertTrue(any('return type' in d.message for d in bad.diagnostics))
            good = self.parse('void finish(); void run(){enum Choice{First,Last}; finish();}', typedef)
            self.assert_status(good, ParseStatus.PARSED_CLEAN)

    def test_debug_trace_and_format_member_keep_real_errors(self):
        declarations = '''
        struct GeneratedText {GeneratedText();GeneratedText(const GeneratedText&);~GeneratedText();
            operator const char*() const; const char* data() const;};
        void trace(const char*,...); void typed(const GeneratedText&);
        struct Writer {void format(const char*,...);};
        #ifdef _DEBUG
        #define SYNTH_TRACE(...) trace(__VA_ARGS__)
        #else
        #define SYNTH_TRACE(...) ((void)0)
        #endif
        '''
        for typedef in (False, True):
            for call in ('SYNTH_TRACE("%s",text);', 'writer.format("%s",text);'):
                bad = self.parse(declarations + 'void run(){GeneratedText text; Writer writer;' + call + '}', typedef)
                self.assert_status(bad, ParseStatus.PARSED_WITH_ERRORS)
                self.assertTrue(any('non-trivial' in d.message for d in bad.diagnostics))
            self.assert_status(self.parse(declarations + '''void run(){GeneratedText text; Writer writer;
                SYNTH_TRACE("%s",text.data()); writer.format("%s",text.data()); typed(text);}''', typedef),
                ParseStatus.PARSED_CLEAN)
            # Release is a different context, not a workaround for debug acceptance.
            self.assert_status(self.parse(declarations +
                'void run(){GeneratedText text; SYNTH_TRACE("%s",text);}', typedef, debug=False),
                ParseStatus.PARSED_CLEAN)
            self.assert_status(self.parse(declarations +
                'void run(){GeneratedText text; Writer writer;writer.format("%s",text);}', typedef, debug=False),
                ParseStatus.PARSED_WITH_ERRORS)

    def test_x86_unicode_pointer_model_does_not_remove_const(self):
        prefix = '''
        #ifndef _NATIVE_WCHAR_T_DEFINED
        typedef unsigned short wchar_t;
        #endif
        #ifdef UNICODE
        typedef wchar_t SyntheticChar;
        #else
        typedef char SyntheticChar;
        #endif
        void convert(unsigned short*); void wide(wchar_t*);
        '''
        for typedef in (False, True):
            for body in ('SyntheticChar buffer[2];convert(buffer);',
                         'unsigned short buffer[2];wide((unsigned short*)buffer);'):
                self.assert_status(self.parse(prefix + 'void run(){' + body + '}', typedef),
                    ParseStatus.PARSED_CLEAN if typedef else ParseStatus.PARSED_WITH_ERRORS)
            self.assert_status(self.parse(prefix +
                'void run(const wchar_t* text){wide(text);}', typedef), ParseStatus.PARSED_WITH_ERRORS)

    def test_context_correction_does_not_clear_shared_header_narrowing(self):
        (self.root / 'shared.h').write_text('struct Row{unsigned mask;}; static Row sentinel{-1};\n')
        for filename in ('one.cpp', 'two.cpp'):
            (self.root / filename).write_text('#include "shared.h"\n')
        (self.root / 'clean.cpp').write_text('int isolated(){return 1;}')
        manifest = self.root / 'manifest.json'
        db = self.root / 'index.db'
        for flags in ([], ['/Zc:wchar_t-']):
            manifest.write_text(json.dumps({'contexts': [{
                'id': 'generated', 'compiler_family': 'msvc', 'platform': 'Win32',
                'language_standard': 'c++14', 'extra_flags': flags}], 'translation_units': [
                {'file': name, 'context_id': 'generated'} for name in ('one.cpp', 'two.cpp', 'clean.cpp')]}))
            result = scan_repository(self.root, db, manifest)
            self.assertEqual(result['failed_translation_units'], 2)
            self.assertEqual(result['clean_translation_units'], 1)
            narrowing = [d for d in result['diagnostics'] if 'narrowed' in d['message']]
            self.assertEqual(len(narrowing), 2)
            self.assertEqual({(d['repo_path'], d['line']) for d in narrowing}, {('shared.h', 1)})
            self.assertEqual([r['parse_status'] for r in result['tu_results']].count('parsed_with_errors'), 2)
            context_key = load_build_manifest(self.root, manifest).contexts[0].context_key
            with IndexStore(db) as store:
                names = {e['name'] for e in store.query_published_entities(context_key=context_key)}
                self.assertIn('isolated', names)
                self.assertNotIn('sentinel', names)
        # An independently corrected generated control verifies incremental recovery.
        (self.root / 'shared.h').write_text('struct Row{unsigned mask;}; static Row sentinel{1u};\n')
        fixed = scan_repository(self.root, db, manifest)
        self.assertEqual(fixed['failed_translation_units'], 0)
        self.assertEqual(fixed['clean_translation_units'], 2)
        self.assertTrue(scan_repository(self.root, db, manifest)['noop'])
