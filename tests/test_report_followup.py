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
from legacygraph.models import BuildContextSpec, ParseRunPayload, ParseStatus, TranslationUnitSpec
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

    def test_diagnostic_provenance_categories_and_no_external_path_leakage(self):
        # 1. Exact contained location after multibyte CP932 + CRLF in contained header
        header_bytes = '// あいう\r\nstruct Row{unsigned mask;};\r\nstatic Row sentinel{-1};\r\n'.encode('cp932')
        (self.root / 'contained.h').write_bytes(header_bytes)
        (self.root / 'main.cpp').write_bytes('#include "contained.h"\r\nint run(){return 0;}\r\n'.encode('cp932'))
        parser = ClangParser(self.root, preferred_legacy_encoding='cp932')
        exact_res = parser.parse(TranslationUnitSpec('main.cpp', self.context(typedef=True)))
        self.assert_status(exact_res, ParseStatus.PARSED_WITH_ERRORS)
        narrow = [d for d in exact_res.diagnostics if d.diagnostic_class == 'narrowing_initializer']
        self.assertEqual(len(narrow), 1)
        self.assertEqual(narrow[0].location_quality, 'exact')
        self.assertEqual(narrow[0].location_provenance, 'contained_file')
        self.assertTrue(narrow[0].is_exact_location)
        self.assertEqual(narrow[0].diagnostic_option, '-Wc++11-narrowing')
        self.assertEqual(narrow[0].span.repo_path, 'contained.h')
        self.assertEqual(narrow[0].span.start_line, 3)
        expected_offset = header_bytes.index(b'-1')
        self.assertEqual(narrow[0].span.byte_start, expected_offset)
        self.assertEqual(narrow[0].span.byte_end, expected_offset)

        # 2. External file outside repo_root (via -I outside, no symlink needed) + missing location (error limit)
        with tempfile.TemporaryDirectory() as outside:
            ext_dir = Path(outside)
            (ext_dir / 'ext_one.h').write_text('struct ExtA{unsigned a;}; static ExtA bad_a{-1};\n')
            (ext_dir / 'ext_two.h').write_text('struct ExtB{unsigned b;}; static ExtB bad_b{-2};\n')
            many_errors = '\n'.join(f'UnknownType{i} v{i};' for i in range(21))
            (self.root / 'ext_main.cpp').write_text(f'#include "ext_one.h"\n#include "ext_two.h"\n{many_errors}\n')
            manifest = self.root / 'manifest.json'
            db = self.root / 'ext.db'
            manifest.write_text(json.dumps({
                'contexts': [{
                    'id': 'ext_ctx', 'compiler_family': 'msvc', 'platform': 'Win32',
                    'language_standard': 'c++14', 'include_dirs': [str(ext_dir)],
                    'extra_flags': ['/Zc:wchar_t-'],
                }],
                'translation_units': [{'file': 'ext_main.cpp', 'context_id': 'ext_ctx'}],
            }))
            scan_out = scan_repository(self.root, db, manifest)
            self.assertEqual(scan_out['failed_translation_units'], 1)
            serialized = json.dumps(scan_out)
            self.assertNotIn(str(ext_dir), serialized)
            self.assertNotIn('ext_one.h', serialized)
            self.assertNotIn('ext_two.h', serialized)

            ext_diags = [d for d in scan_out['diagnostics'] if d['location_provenance'] == 'external_file']
            self.assertEqual(len(ext_diags), 2)
            for ed in ext_diags:
                self.assertEqual(ed['location_quality'], 'fallback')
                self.assertFalse(ed['is_exact_location'])
                self.assertEqual(ed['repo_path'], 'ext_main.cpp')
                self.assertEqual(ed['line'], 1)
                self.assertEqual(ed['diagnostic_class'], 'narrowing_initializer')
                self.assertEqual(ed['diagnostic_option'], '-Wc++11-narrowing')

            limit_diags = [d for d in scan_out['diagnostics'] if d['diagnostic_class'] == 'error_limit_reached']
            self.assertEqual(len(limit_diags), 1)
            self.assertEqual(limit_diags[0]['location_quality'], 'fallback')
            self.assertEqual(limit_diags[0]['location_provenance'], 'missing_location')
            self.assertFalse(limit_diags[0]['is_exact_location'])

            # Unrelated external/missing-location fallback records at ext_main.cpp:1 are never merged
            fb_summaries = [b for b in scan_out['blocker_summaries'] if not b['is_exact_location']]
            self.assertEqual(len(fb_summaries), 3)
            for b in fb_summaries:
                self.assertFalse(b['is_exact_location'])
                self.assertFalse(b['grouped_by_exact_location'])
                self.assertIsNone(b['repo_path'])
                self.assertIsNone(b['line'])
                self.assertEqual(b['fallback_repo_path'], 'ext_main.cpp')
                self.assertEqual(b['record_count'], 1)

        # 3. Missing/unresolvable file provenance (missing main file, missing forced include, unresolvable diag file)
        missing_main = parser.parse(TranslationUnitSpec('no_such_file.cpp', self.context(typedef=True)))
        self.assertEqual(missing_main.parse_status, ParseStatus.FAILED)
        self.assertEqual(missing_main.diagnostics[0].location_quality, 'none')
        self.assertEqual(missing_main.diagnostics[0].location_provenance, 'missing_file')
        self.assertFalse(missing_main.diagnostics[0].is_exact_location)
        self.assertEqual(missing_main.diagnostics[0].diagnostic_class, 'missing_include')

        forced_ctx = BuildContextSpec(
            'Generated', 'Demo', 'Debug', 'Win32', compiler_family='msvc',
            language_standard='c++14', forced_includes=('missing_forced_header.h',),
            extra_flags=('/Zc:wchar_t-',),
        )
        (self.root / 'forced_main.cpp').write_text('int ok(){return 1;}\n')
        forced_res = parser.parse(TranslationUnitSpec('forced_main.cpp', forced_ctx))
        self.assertEqual(forced_res.parse_status, ParseStatus.PARSED_WITH_ERRORS)
        self.assertEqual(forced_res.diagnostics[0].location_quality, 'fallback')
        self.assertEqual(forced_res.diagnostics[0].location_provenance, 'missing_file')
        self.assertFalse(forced_res.diagnostics[0].is_exact_location)
        self.assertEqual(forced_res.diagnostics[0].diagnostic_class, 'missing_include')

    def test_blocker_grouping_across_tus_contexts_and_inherited_snapshots_plus_cli_queries(self):
        (self.root / 'shared.h').write_text('struct Row{unsigned mask;}; static Row sentinel{-1};\n')
        (self.root / 'one.cpp').write_text('#include "shared.h"\nint one(){return 1;}\n')
        (self.root / 'two.cpp').write_text('#include "shared.h"\nint two(){return 2;}\n')
        (self.root / 'warn_clean.cpp').write_text('char* legacy_lit(){return "text";}\n')

        with tempfile.TemporaryDirectory() as outside:
            ext_dir = Path(outside)
            (ext_dir / 'ext1.h').write_text('struct E1{unsigned v;}; static E1 e1{-1};\n')
            (ext_dir / 'ext2.h').write_text('struct E2{unsigned v;}; static E2 e2{-2};\n')
            (self.root / 'fallback.cpp').write_text('#include "ext1.h"\n#include "ext2.h"\n')

            manifest = self.root / 'manifest.json'
            db = self.root / 'grouped.db'
            manifest.write_text(json.dumps({
                'contexts': [
                    {
                        'id': 'ctx_dbg', 'compiler_family': 'msvc', 'configuration': 'Debug',
                        'platform': 'Win32', 'language_standard': 'c++14',
                        'include_dirs': [str(ext_dir)], 'extra_flags': ['/Zc:wchar_t-'],
                    },
                    {
                        'id': 'ctx_rel', 'compiler_family': 'msvc', 'configuration': 'Release',
                        'platform': 'Win32', 'language_standard': 'c++14',
                        'extra_flags': ['/Zc:wchar_t-'],
                    },
                ],
                'translation_units': [
                    {'file': 'one.cpp', 'context_id': 'ctx_dbg'},
                    {'file': 'two.cpp', 'context_id': 'ctx_dbg'},
                    {'file': 'fallback.cpp', 'context_id': 'ctx_dbg'},
                    {'file': 'warn_clean.cpp', 'context_id': 'ctx_dbg'},
                    {'file': 'one.cpp', 'context_id': 'ctx_rel'},
                ],
            }))

            out_scan = io.StringIO()
            err_scan = io.StringIO()
            with contextlib.redirect_stdout(out_scan), contextlib.redirect_stderr(err_scan):
                rc = main([
                    'scan', '--repo', str(self.root), '--manifest', str(manifest),
                    '--db', str(db), '--progress', '--json',
                ])
            self.assertEqual(rc, 3)
            scan_doc = json.loads(out_scan.getvalue())
            # Progress remains strictly privacy-safe
            self.assertNotIn('shared.h', err_scan.getvalue())
            self.assertNotIn('sentinel', err_scan.getvalue())
            self.assertNotIn('narrow', err_scan.getvalue())
            self.assertNotIn(str(ext_dir), err_scan.getvalue())

            # Raw per-TU records are retained in full (3 shared.h records + 2 external fallback + 1 warning)
            raw_narrow = [
                d for d in scan_doc['diagnostics']
                if d['diagnostic_class'] == 'narrowing_initializer' and d['is_exact_location']
            ]
            self.assertEqual(len(raw_narrow), 3)

            blockers = scan_doc['blocker_summaries']
            # Top blocker is the shared.h exact blocker in ctx_dbg with 2 affected TUs
            top = blockers[0]
            self.assertTrue(top['is_exact_location'])
            self.assertTrue(top['grouped_by_exact_location'])
            self.assertEqual(top['repo_path'], 'shared.h')
            self.assertEqual(top['line'], 1)
            self.assertEqual(top['diagnostic_class'], 'narrowing_initializer')
            self.assertEqual(top['diagnostic_option'], '-Wc++11-narrowing')
            self.assertEqual(top['record_count'], 2)
            self.assertEqual(top['affected_tu_count'], 2)
            self.assertEqual(top['affected_main_files'], ['one.cpp', 'two.cpp'])
            self.assertTrue(top['counts_are_not_defect_counts'])

            # ctx_rel has its own separate exact blocker for shared.h
            rel_exact = [
                b for b in blockers
                if b['is_exact_location'] and b['context_key'] != top['context_key']
            ]
            self.assertEqual(len(rel_exact), 1)
            self.assertEqual(rel_exact[0]['affected_tu_count'], 1)
            self.assertEqual(rel_exact[0]['affected_main_files'], ['one.cpp'])

            # The two external fallback errors on fallback.cpp:1 are NOT merged together
            fb_blockers = [b for b in blockers if b['fallback_repo_path'] == 'fallback.cpp']
            self.assertEqual(len(fb_blockers), 2)
            for fb in fb_blockers:
                self.assertFalse(fb['is_exact_location'])
                self.assertFalse(fb['grouped_by_exact_location'])
                self.assertIsNone(fb['repo_path'])
                self.assertEqual(fb['record_count'], 1)
                self.assertEqual(fb['affected_tu_count'], 1)

            # Verify CLI status and CLI diagnostics queries
            out_stat = io.StringIO()
            with contextlib.redirect_stdout(out_stat):
                self.assertEqual(main(['status', '--db', str(db), '--json']), 0)
            stat_doc = json.loads(out_stat.getvalue())
            self.assertEqual(stat_doc['diagnostic_summary']['blocker_group_count'], 4)
            self.assertEqual(stat_doc['diagnostic_summary']['exact_blocker_group_count'], 2)
            self.assertEqual(stat_doc['diagnostic_summary']['unmerged_non_exact_blocker_count'], 2)

            out_diag = io.StringIO()
            with contextlib.redirect_stdout(out_diag):
                self.assertEqual(
                    main([
                        'diagnostics', '--db', str(db), '--context', top['context_key'],
                        '--class', 'narrowing_initializer', '--blocking-only', '--json',
                    ]),
                    0,
                )
            diag_doc = json.loads(out_diag.getvalue())
            self.assertEqual(diag_doc['total'], 4)
            self.assertEqual(len(diag_doc['blocker_summaries']), 3)
            self.assertEqual(diag_doc['blocker_summaries'][0]['affected_tu_count'], 2)

            # Touch only one.cpp so warn_clean.cpp is reused/inherited in the next generation;
            # also test inherited unverified TU relationship via a staged generation in IndexStore.
            (self.root / 'one.cpp').write_text('#include "shared.h"\nint one(){return 10;}\n')
            scan2 = scan_repository(self.root, db, manifest)
            self.assertEqual(scan2['reused_translation_units'], 1)
            with IndexStore(db) as store:
                all_ctx_diags = store.query_diagnostics(context_key=top['context_key'])
                warn_rows = [d for d in all_ctx_diags if d['main_file_path'] == 'warn_clean.cpp']
                self.assertTrue(warn_rows)
                self.assertTrue(all(d['is_inherited'] and d['reused'] for d in warn_rows))
                self.assertTrue(all(d['tu_snapshot_state'] == 'verified_inherited' for d in warn_rows))

                # Stage and publish a generation that updates only one.cpp, leaving two.cpp as unverified_inherited
                gen_next = store.begin_generation(
                    parser_version=scan2['parser_version'],
                    mfc_rule_version=scan2['mfc_rule_version'],
                    inherit_from_published=True,
                )
                one_spec = next(
                    s for s in load_build_manifest(self.root, manifest).translation_units
                    if s.main_file_path == 'one.cpp' and s.build_context.context_key == top['context_key']
                )
                one_payload = ClangParser(self.root).parse(one_spec)
                store.record_parse_run(gen_next, one_payload)
                store.publish_generation(gen_next)

                bundle = store.get_diagnostic_summary(context_key=top['context_key'])
                shared_group = bundle['blocker_summaries'][0]
                self.assertEqual(shared_group['affected_tu_count'], 2)
                self.assertEqual(shared_group['parsed_tu_count'], 1)
                self.assertEqual(shared_group['reused_tu_count'], 1)

    def test_multiline_trace_format_continuation_and_generated_unsigned_sentinel_controls(self):
        declarations = (
            '#ifndef _NATIVE_WCHAR_T_DEFINED\n'
            'typedef unsigned short wchar_t;\n'
            '#endif\n'
            'struct SynthString {\n'
            '    SynthString();\n'
            '    SynthString(const SynthString&);\n'
            '    ~SynthString();\n'
            '    operator const wchar_t*() const;\n'
            '    const wchar_t* GetString() const;\n'
            '};\n'
            'void SynthTrace(const wchar_t*, ...);\n'
            'struct SynthBuffer { void Format(const wchar_t*, ...); };\n'
            '#define SYNTH_TRACE(...) SynthTrace(__VA_ARGS__)\n'
        )
        multiline_bad = (
            declarations
            + 'void run() {\n'
            + '    SynthString text;\n'
            + '    SynthBuffer buf;\n'
            + '    buf.Format(\n'
            + '        L"%s %d",\n'
            + '        text,\n'
            + '        42);\n'
            + '    SYNTH_TRACE(\n'
            + '        L"%s\\n",\n'
            + '        text);\n'
            + '}\n'
        )
        bad_res = self.parse(multiline_bad, typedef=True)
        self.assert_status(bad_res, ParseStatus.PARSED_WITH_ERRORS)
        varargs_diags = [d for d in bad_res.diagnostics if d.diagnostic_class == 'non_trivial_varargs']
        self.assertEqual(len(varargs_diags), 2)
        for d in varargs_diags:
            self.assertEqual(d.diagnostic_option, '-Wnon-pod-varargs')
            self.assertEqual(d.location_quality, 'exact')
            self.assertEqual(d.location_provenance, 'contained_file')
            self.assertTrue(d.is_exact_location)
        # Format continuation diagnostic points specifically to the continuation line with `text`
        self.assertEqual({d.span.start_line for d in varargs_diags}, {19, 21})

        multiline_good = (
            declarations
            + 'void run() {\n'
            + '    SynthString text;\n'
            + '    SynthBuffer buf;\n'
            + '    buf.Format(\n'
            + '        L"%s %d",\n'
            + '        text.GetString(),\n'
            + '        42);\n'
            + '    SYNTH_TRACE(\n'
            + '        L"%s\\n",\n'
            + '        static_cast<const wchar_t*>(text));\n'
            + '}\n'
        )
        self.assert_status(self.parse(multiline_good, typedef=True), ParseStatus.PARSED_CLEAN)

        # Generated unsigned sentinel controls (synthetic fixture verification only)
        for expr in ('static_cast<unsigned>(-1)', '~0u'):
            self.assert_status(
                self.parse(f'struct Row{{unsigned mask;}}; static Row sentinel{{{expr}}};\n', typedef=True),
                ParseStatus.PARSED_CLEAN,
            )

        # Missing enum semicolon classification
        enum_bad = self.parse('void finish(); void run(){enum Choice{First,Last} finish();}', typedef=True)
        self.assert_status(enum_bad, ParseStatus.PARSED_WITH_ERRORS)
        classes = [d.diagnostic_class for d in enum_bad.diagnostics]
        self.assertIn('type_in_result_declaration', classes)

    def test_legacy_database_migration_and_conservative_backfill(self):
        import sqlite3
        from legacygraph.discovery import discover_repository_files
        from legacygraph.queries import QueryService

        (self.root / 'legacy.cpp').write_text('int x = 1;\nint y = 2;\n')
        db = self.root / 'legacy.db'
        ctx = self.context(typedef=True)
        spec = TranslationUnitSpec('legacy.cpp', ctx)

        with IndexStore(db) as store:
            store.initialize_schema(self.root)
            disc = discover_repository_files(self.root, output_paths_to_exclude=[db])
            store.sync_file_inventory(disc.files)
            store.register_build_context(ctx)
            store.register_translation_units([spec])
            gen_id = store.begin_generation()
            run_id = store.record_parse_run(
                gen_id,
                ParseRunPayload(
                    tu_key=spec.tu_key,
                    parser_name='clang',
                    parser_version='0.3.2',
                    mfc_rule_version='0.1.0',
                    parse_status=ParseStatus.PARSED_WITH_ERRORS,
                ),
            )
            store.publish_generation(gen_id)
            fvid = store._require_current_file_version_id(store.get_active_repository_id(), 'legacy.cpp')

        # Downgrade the diagnostics table to the pre-provenance 15-column schema
        conn = sqlite3.connect(str(db))
        try:
            conn.executescript(
                """
                DROP TABLE diagnostics;
                CREATE TABLE diagnostics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    parse_run_id INTEGER NOT NULL REFERENCES parse_runs(id) ON DELETE CASCADE,
                    file_version_id INTEGER REFERENCES file_versions(id) ON DELETE SET NULL,
                    severity TEXT NOT NULL,
                    code TEXT NOT NULL,
                    message TEXT NOT NULL,
                    parse_scope TEXT NOT NULL,
                    byte_start INTEGER,
                    byte_end INTEGER,
                    start_line INTEGER,
                    start_col INTEGER,
                    end_line INTEGER,
                    end_col INTEGER,
                    missing_dependency TEXT NOT NULL DEFAULT '',
                    unsupported_feature TEXT NOT NULL DEFAULT ''
                );
                """
            )
            # 1. Missing file with NULL span
            conn.execute(
                """
                INSERT INTO diagnostics(
                    parse_run_id, file_version_id, severity, code, message, parse_scope,
                    byte_start, byte_end, start_line, start_col, end_line, end_col,
                    missing_dependency, unsupported_feature
                ) VALUES (?, NULL, 'fatal', 'clang.main_file_missing', 'Main file missing', 'tu',
                          NULL, NULL, NULL, NULL, NULL, NULL, 'legacy.cpp', '')
                """,
                (run_id,),
            )
            # 2. Whole-file fallback span (byte_end > byte_start)
            conn.execute(
                """
                INSERT INTO diagnostics(
                    parse_run_id, file_version_id, severity, code, message, parse_scope,
                    byte_start, byte_end, start_line, start_col, end_line, end_col,
                    missing_dependency, unsupported_feature
                ) VALUES (?, ?, 'error', 'Semantic Issue',
                          'constant expression evaluates to -1 which cannot be narrowed to type ''unsigned int''',
                          'tu', 0, 22, 1, 1, 2, 1, '', '')
                """,
                (run_id, fvid),
            )
            # 3. Point span from legacy DB where provenance was not recorded
            conn.execute(
                """
                INSERT INTO diagnostics(
                    parse_run_id, file_version_id, severity, code, message, parse_scope,
                    byte_start, byte_end, start_line, start_col, end_line, end_col,
                    missing_dependency, unsupported_feature
                ) VALUES (?, ?, 'error', 'Semantic Issue',
                          'cannot pass object of non-trivial type ''S'' through variadic function',
                          'tu', 5, 5, 1, 6, 1, 6, '', '')
                """,
                (run_id, fvid),
            )
            conn.commit()
        finally:
            conn.close()

        # Re-open via IndexStore and verify automatic additive migration and conservative backfill
        with IndexStore(db) as migrated:
            stat = migrated.get_status_summary()
            self.assertEqual(stat['schema_version'], 1)
            q = QueryService(migrated).diagnostics(ctx.context_key)
            self.assertEqual(q['total'], 3)
            by_cls = {d['diagnostic_class']: d for d in q['diagnostics']}
            self.assertEqual(by_cls['missing_include']['location_quality'], 'none')
            self.assertEqual(by_cls['missing_include']['location_provenance'], 'missing_file')
            self.assertFalse(by_cls['missing_include']['is_exact_location'])

            self.assertEqual(by_cls['narrowing_initializer']['location_quality'], 'fallback')
            self.assertEqual(by_cls['narrowing_initializer']['location_provenance'], 'unknown')
            self.assertFalse(by_cls['narrowing_initializer']['is_exact_location'])

            # Legacy point span without recorded provenance remains conservative ('unknown', never claimed exact)
            self.assertEqual(by_cls['non_trivial_varargs']['location_quality'], 'unknown')
            self.assertEqual(by_cls['non_trivial_varargs']['location_provenance'], 'unknown')
            self.assertFalse(by_cls['non_trivial_varargs']['is_exact_location'])
            self.assertEqual(len(q['blocker_summaries']), 3)
