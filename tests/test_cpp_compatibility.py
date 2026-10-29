"""Invented C++ fixtures; no MFC SDK or application inputs are used."""
import json
import tempfile
import unittest
from pathlib import Path

from legacygraph.build_context import load_build_manifest
from legacygraph.clang_parser import ClangParser, is_libclang_available
from legacygraph.index import IndexStore
from legacygraph.models import BuildContextSpec, ParseStatus, RelationKind, TranslationUnitSpec
from legacygraph.scanner import scan_repository

WIDE_FIXTURE = '''
#ifndef _NATIVE_WCHAR_T_DEFINED
typedef unsigned short wchar_t;
#endif
int pick(unsigned short*);
#ifdef _NATIVE_WCHAR_T_DEFINED
int pick(wchar_t*);
#endif
int text(const wchar_t*); int text(const char*);
#ifdef UNICODE
#define SYNTH_TEXT(x) L##x
#else
#define SYNTH_TEXT(x) x
#endif
int run(){wchar_t p[2];return pick(p)+text(SYNTH_TEXT("x"));}
'''


@unittest.skipUnless(is_libclang_available()[0], 'Requires optional libclang')
class CppCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def parse(self,source,flags=(),per_file=(),defines=(),family='msvc'):
        (self.root/'main.cpp').write_text(source,encoding='utf-8')
        context=BuildContextSpec('Synthetic','Demo','Debug','x64',compiler_family=family,
                                 extra_flags=flags,defines=defines)
        return ClangParser(self.root).parse(TranslationUnitSpec('main.cpp',context,per_file_flags=per_file))

    def clean(self,payload): self.assertEqual(payload.parse_status,ParseStatus.PARSED_CLEAN,payload.diagnostics)
    def errored(self,payload):
        self.assertEqual(payload.parse_status,ParseStatus.PARSED_WITH_ERRORS,payload.diagnostics)
        self.assertFalse(payload.entities);self.assertFalse(payload.relations)

    def test_wchar_driver_flag_is_not_the_frontend_option(self):
        from clang import cindex
        index=cindex.Index.create();source='typedef unsigned short wchar_t; wchar_t value;'
        args=['-x','c++','--target=x86_64-pc-windows-msvc','-fms-extensions']
        bad=index.parse('invented.cpp',args=args+['-fno-wchar'],unsaved_files=[('invented.cpp',source)])
        self.assertTrue(any('unknown argument' in d.spelling for d in bad.diagnostics))
        good=index.parse('invented.cpp',args=args+['-Xclang','-fno-wchar'],unsaved_files=[('invented.cpp',source)])
        self.assertFalse(list(good.diagnostics))

    def test_native_typedef_unicode_and_nonunicode_overload_identity(self):
        for flags,native in [((),True),(('/Zc:wchar_t',),True),(('/Zc:wchar_t-',),False)]:
            for unicode in (False,True):
                with self.subTest(flags=flags,unicode=unicode):
                    payload=self.parse(WIDE_FIXTURE,flags,defines=('UNICODE',) if unicode else ())
                    self.clean(payload)
                    calls=[r.target_semantic_key for r in payload.relations if r.kind==RelationKind.CALLS]
                    self.assertTrue(any('pick#*'+('W' if native else 's')+'#' in key for key in calls))
                    self.assertTrue(any('text#*1'+(('W' if native else 's') if unicode else 'C')+'#' in key for key in calls))

    def test_type_width_is_not_type_identity_and_const_is_not_removed(self):
        mismatch='static_assert(sizeof(wchar_t)==sizeof(unsigned short), "width"); void convert(unsigned short*); void run(wchar_t* p){convert(p);}'
        self.errored(self.parse(mismatch))
        self.clean(self.parse('typedef unsigned short wchar_t;'+mismatch,('/Zc:wchar_t-',)))
        for flags in ((),('/Zc:wchar_t-',)):
            prefix='#ifndef _NATIVE_WCHAR_T_DEFINED\ntypedef unsigned short wchar_t;\n#endif\n'
            self.errored(self.parse(prefix+'void convert(wchar_t*); void run(const wchar_t* p){convert(p);}',flags))
            self.clean(self.parse(prefix+'void convert(const wchar_t*); void run(const wchar_t* p){convert(p);}',flags))
        # The SDK/source must supply the typedef; the tool must not inject one.
        self.errored(self.parse('wchar_t value;',('/Zc:wchar_t-',)))

    def test_ordered_per_file_override_and_unsupported_non_windows_context(self):
        for family in ('msvc', 'clang-cl'):
            for context_flag, file_flag, marker in (
                ('/Zc:wchar_t-', '/Zc:wchar_t', 'W'),
                ('/Zc:wchar_t', '/Zc:wchar_t-', 's'),
            ):
                with self.subTest(family=family, file_flag=file_flag):
                    payload = self.parse(WIDE_FIXTURE, (context_flag,), (file_flag,), family=family)
                    self.clean(payload)
                    targets = [r.target_semantic_key for r in payload.relations
                               if r.kind == RelationKind.CALLS]
                    self.assertTrue(any('pick#*' + marker + '#' in key for key in targets))
        for flag in ('/Zc:wchar_t', '/Zc:wchar_t-'):
            result = self.parse(WIDE_FIXTURE, (flag,), family='clang')
            self.assertEqual(result.parse_status, ParseStatus.FAILED)
            self.assertTrue(any(d.code == 'clang.unsupported_wchar_context' for d in result.diagnostics))

    def test_narrowing_is_an_error_in_both_wchar_models(self):
        for flags in ((),('/Zc:wchar_t-',)):
            payload=self.parse('struct Options{unsigned mask;}; Options value{-1};',flags)
            self.errored(payload)
            self.assertTrue(any('narrow' in d.message for d in payload.diagnostics))
            self.clean(self.parse('struct Options{unsigned mask;}; Options value{1u};',flags))

    def test_nontrivial_varargs_versus_typed_and_explicit_pointer_controls(self):
        declarations='''struct Text { Text(); Text(const Text&); ~Text();
            const char* c_str() const; operator const char*() const; };
            void variadic(int,...); void typed(const Text&);
        '''
        bad=self.parse(declarations+'void run(){Text t; variadic(1,t);}')
        self.errored(bad)
        self.assertTrue(any('non-trivial' in d.message and 'variadic' in d.message for d in bad.diagnostics))
        self.clean(self.parse(declarations+'void run(){Text t; typed(t); variadic(1,t.c_str());}'))

    def test_result_declaration_and_error_limit_remain_explicit(self):
        bad=self.parse('struct InventedResult{int value;} make(){return {};}')
        self.errored(bad)
        self.assertTrue(any('result type' in d.message for d in bad.diagnostics))
        self.clean(self.parse('struct InventedResult{int value;}; InventedResult make(){return {};}'))
        bad=self.parse('\n'.join(f'int value_{i}=absent_{i};' for i in range(30)))
        self.errored(bad)
        self.assertTrue(any('too many errors' in d.message for d in bad.diagnostics))

    def test_literal_constness_and_preprocessor_warnings_are_retained(self):
        result=self.parse('void take(char*); void run(){take("x");}\n/* outer /* nested */\n#if 1\n#endif trailing\n')
        self.assertTrue(any('string literal' in d.message for d in result.diagnostics))
        self.assertTrue(any('block comment' in d.message for d in result.diagnostics))
        self.assertTrue(any('extra tokens' in d.message for d in result.diagnostics))

    def test_manifest_context_isolation_and_policy_change_invalidates(self):
        (self.root/'main.cpp').write_text(WIDE_FIXTURE)
        manifest=self.root/'manifest.json';db=self.root/'index.db'
        def write(modes):
            manifest.write_text(json.dumps({'contexts':[{'id':mode,'project_name':'Synthetic',
                'extra_flags':['/Zc:wchar_t-' if mode=='typedef' else '/Zc:wchar_t'],
                'defines':['UNICODE']} for mode in modes],
                'translation_units':[{'file':'main.cpp','context_id':mode} for mode in modes]}))
        write(['native']);first=scan_repository(self.root,db,manifest)
        self.assertEqual(first['clean_translation_units'],1)
        self.assertTrue(scan_repository(self.root,db,manifest)['noop'])
        scan_repository(self.root,db,manifest,parser_version='0.3.0')
        upgraded=scan_repository(self.root,db,manifest)
        self.assertEqual(upgraded['clean_translation_units'],1)
        self.assertIn('parser_version_changed',upgraded['invalidated_translation_units'][0]['reasons'])
        write(['typedef']);changed=scan_repository(self.root,db,manifest)
        self.assertEqual(changed['clean_translation_units'],1)
        self.assertEqual(len(changed['deactivated_translation_units']),1)
        write(['native','typedef']);both=scan_repository(self.root,db,manifest)
        self.assertEqual(both['failed_translation_units'],0)
        specs=load_build_manifest(self.root,manifest).translation_units
        with IndexStore(db) as store:
            for spec in specs:
                calls=store.query_published_relations(context_key=spec.build_context.context_key,kind=RelationKind.CALLS)
                targets=[c['target_semantic_key'] for c in calls]
                marker='s' if '/Zc:wchar_t-' in spec.build_context.extra_flags else 'W'
                self.assertTrue(any('pick#*'+marker+'#' in key for key in targets))
        self.assertTrue(scan_repository(self.root,db,manifest)['noop'])

    def test_compilation_database_retains_type_mode(self):
        (self.root/'main.cpp').write_text(WIDE_FIXTURE)
        manifest=self.root/'compile_commands.json'
        manifest.write_text(json.dumps([{'directory':str(self.root),'file':'main.cpp',
            'arguments':['cl.exe','/c','/Zc:wchar_t-','main.cpp']}]))
        spec=load_build_manifest(self.root,manifest).translation_units[0]
        self.assertIn('/Zc:wchar_t-',spec.build_context.extra_flags)
        self.clean(ClangParser(self.root).parse(spec))
