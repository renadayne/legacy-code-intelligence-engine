"""Cache lifetime and containment checks using generated files only."""
from contextlib import contextmanager
from dataclasses import replace
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import legacygraph.clang_parser as module
from legacygraph.clang_parser import ClangParser,is_libclang_available
from legacygraph.models import BuildContextSpec,ParseStatus,TranslationUnitSpec


class UncachedParser(ClangParser):
    @contextmanager
    def _ast_path_scope(self):
        yield {}


@unittest.skipUnless(is_libclang_available()[0], 'Requires optional libclang')
class AstPathCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.parent=Path(self.temp.name);self.root=self.parent/'repo';self.root.mkdir()
        self.spec=TranslationUnitSpec('main.cpp',BuildContextSpec('Synthetic','Demo','Debug','x64'))

    def link(self,alias,target):
        try:alias.symlink_to(target)
        except (OSError,NotImplementedError) as exc:
            self.skipTest('Symlink capability unavailable; containment scenario unverified: '+str(exc))

    def test_same_facts_metadata_and_progress_with_bom_crlf_macros_and_contexts(self):
        from legacygraph.progress import Progress
        outside=self.parent/'external.h';outside.write_text('struct External {int value;};\n')
        header='''#pragma once
#define ACTIVE(x) x
#if 0
#define INACTIVE(x) x
#endif
inline int target(){return VALUE;}
'''
        (self.root/'inner.h').write_bytes(header.replace('\n','\r\n').encode('utf-16'))
        text=f'''#include "{outside.as_posix()}"
#include "inner.h"
/* 日本 */ int run(){{return ACTIVE(target());}}
'''
        (self.root/'main.cpp').write_bytes(text.replace('\n','\r\n').encode('cp932'))
        for value in ('1','2'):
            spec=replace(self.spec,build_context=replace(self.spec.build_context,defines=('VALUE='+value,)))
            cached=ClangParser(self.root,preferred_legacy_encoding='cp932')
            control=UncachedParser(self.root,preferred_legacy_encoding='cp932')
            events=[];cached._progress=Progress(events.append)
            first=cached.parse(spec);second=control.parse(spec)
            self.assertEqual(first.parse_status,ParseStatus.PARSED_CLEAN,first.diagnostics)
            self.assertEqual(first,second)
            for field in ('last_macro_offsets','last_consumed_files','last_file_hashes','macro_offsets_by_tu'):
                self.assertEqual(getattr(cached,field),getattr(control,field))
            self.assertEqual(len([e for e in events if e['phase']=='native_parse' and e['state']=='finished']),2)
            self.assertIsNone(cached._ast_paths)

    def test_cache_calls_resolver_once_per_spelling_then_resets_even_on_exception(self):
        source=self.root/'main.cpp';source.write_text('int value;')
        parser=ClangParser(self.root)
        with patch.object(module,'ensure_contained_path',wraps=module.ensure_contained_path) as resolver:
            with parser._ast_path_scope():
                for _ in range(10):self.assertEqual(parser._try_repo_relative(source),'main.cpp')
            self.assertEqual(resolver.call_count,1)
            parser._try_repo_relative(source)
            self.assertEqual(resolver.call_count,2)
        with self.assertRaises(RuntimeError):
            with parser._ast_path_scope():raise RuntimeError('synthetic')
        self.assertIsNone(parser._ast_paths)

    def test_cache_is_bounded_and_does_not_casefold_or_collapse_spellings(self):
        parser=ClangParser(self.root)
        with patch.object(module,'ensure_contained_path',side_effect=FileNotFoundError):
            with parser._ast_path_scope() as paths:
                for i in range(4100):parser._try_repo_relative('missing'+str(i))
                self.assertEqual(len(paths),4096)
        (self.root/'main.cpp').write_text('int value;')
        with parser._ast_path_scope() as paths:
            parser._try_repo_relative(self.root/'main.cpp')
            parser._try_repo_relative(self.root/'MAIN.cpp')
            self.assertEqual(len(paths),2) # Native filesystem determines equivalence.

    def test_symlink_retarget_during_ast_is_stale_even_with_identical_bytes(self):
        first=self.root/'first.h';second=self.parent/'outside.h'
        first.write_text('inline int target(){return 1;}\n');second.write_bytes(first.read_bytes())
        alias=self.root/'alias.h';self.link(alias,first)
        (self.root/'main.cpp').write_text('#include "alias.h"\nint run(){return target();}\n')
        parser=ClangParser(self.root);original=parser._phase
        @contextmanager
        def phase(name,**fields):
            with original(name,**fields):yield
            if name=='ast_evidence':
                self.assertIn(str(alias),parser._ast_paths)
                alias.unlink();alias.symlink_to(second)
        parser._phase=phase
        payload=parser.parse(self.spec)
        self.assertEqual(payload.parse_status,ParseStatus.STALE,payload.diagnostics)
        self.assertFalse(payload.entities);self.assertFalse(payload.relations)
        self.assertIsNone(parser._ast_paths)

    def test_negative_cache_revalidation_and_missing_files(self):
        (self.root/'main.cpp').write_text('int run(){return 1;}\n')
        missing=self.root/'new.h'
        parser=ClangParser(self.root);original=parser._phase
        @contextmanager
        def phase(name,**fields):
            if name=='ast_evidence':self.assertIsNone(parser._try_repo_relative(missing))
            with original(name,**fields):yield
            if name=='ast_evidence':missing.write_text('int now_present;')
        parser._phase=phase
        self.assertEqual(parser.parse(self.spec).parse_status,ParseStatus.STALE)

    def test_external_to_contained_retarget_and_next_parse_see_fresh_state(self):
        outside=self.parent/'external.h';inside=self.root/'inside.h'
        outside.write_text('inline int target(){return 1;}\n');inside.write_bytes(outside.read_bytes())
        alias=self.root/'alias.h';self.link(alias,outside)
        (self.root/'main.cpp').write_text('#include "alias.h"\nint run(){return target();}\n')
        parser=ClangParser(self.root);original=parser._phase
        @contextmanager
        def phase(name,**fields):
            with original(name,**fields):yield
            if name=='ast_evidence':
                self.assertIn(str(alias),parser._ast_paths)
                alias.unlink();alias.symlink_to(inside)
        parser._phase=phase
        payload=parser.parse(self.spec)
        self.assertEqual(payload.parse_status,ParseStatus.STALE)
        parser._phase=original
        fresh=parser.parse(self.spec)
        self.assertEqual(fresh.parse_status,ParseStatus.PARSED_CLEAN,fresh.diagnostics)
        self.assertIn('inside.h',parser.last_consumed_files)

    def test_error_path_still_keeps_macros_dependencies_and_source_freshness(self):
        (self.root/'inner.h').write_text('#define SYNTH_VALUE 1\n')
        (self.root/'main.cpp').write_text('#include "inner.h"\nint bad=absent;\n')
        parser=ClangParser(self.root);control=UncachedParser(self.root)
        payload=parser.parse(self.spec)
        self.assertEqual(payload,control.parse(self.spec))
        self.assertEqual(payload.parse_status,ParseStatus.PARSED_WITH_ERRORS)
        self.assertEqual(parser.last_macro_offsets,control.last_macro_offsets)
        self.assertEqual(parser.last_file_hashes,control.last_file_hashes)
        self.assertTrue(payload.dependencies);self.assertTrue(payload.diagnostics)
        original=parser._phase
        @contextmanager
        def phase(name,**fields):
            with original(name,**fields):yield
            if name=='ast_evidence':(self.root/'inner.h').unlink()
        parser._phase=phase
        self.assertEqual(parser.parse(self.spec).parse_status,ParseStatus.STALE)
