"""All sources below are generated fixtures, never application source."""
import hashlib
import io
from contextlib import redirect_stdout
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from legacygraph.clang_parser import ClangParser, is_libclang_available
from legacygraph.index import IndexStore
from legacygraph.models import BuildContextSpec, ParseStatus, RelationKind, TranslationUnitSpec
from legacygraph.parser_input import SourceBuffer
from legacygraph.queries import QueryService
from legacygraph.scanner import scan_repository


class SourceBufferTests(unittest.TestCase):
    def test_character_boundaries_and_crlf_for_all_supported_boms(self):
        text = '/* あ😀 */ int run();\r\n'
        for encoding in ('utf-8-sig', 'utf-16', 'utf-32', 'utf-16-be', 'utf-32-be'):
            with self.subTest(encoding=encoding):
                raw = text.encode(encoding)
                if encoding.endswith('-be'):
                    raw = (b'\xfe\xff' if '16' in encoding else b'\x00\x00\xfe\xff') + raw
                buffer = SourceBuffer.create(raw, 'cp932')
                offset = buffer.original_offset(buffer.parser.index(b'int run'))
                self.assertEqual(buffer.text_slice(offset, len(raw)), 'int run();\r\n')
                self.assertEqual(buffer.position(offset), (1, offset + 1))
                self.assertEqual(buffer.position(len(raw)), (2, 1))

    def test_invalid_cp932_is_not_reinterpreted_as_another_codec(self):
        with self.assertRaises(UnicodeDecodeError):
            SourceBuffer.create(b'//\x81', 'cp932')
        with self.assertRaises(ValueError):
            SourceBuffer.create(b'int a;', 'iso2022_jp')


@unittest.skipUnless(is_libclang_available()[0], 'Optional libclang required')
class ParserEncodingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.spec = TranslationUnitSpec('main.cpp', BuildContextSpec(
            'Synthetic', 'Demo', 'Debug', 'x86', compiler_family='msvc'))

    def parser(self, **kwargs):
        return ClangParser(self.root, preferred_legacy_encoding='cp932', **kwargs)

    def test_independent_native_wide_and_narrow_failure_and_overlay_recovery(self):
        from clang import cindex
        index = cindex.Index.create()
        for kind, declaration, severity in (
            ('wide', 'const wchar_t* text = L"あ";\n', cindex.Diagnostic.Error),
            ('narrow', 'const char* text = "あ";\n', cindex.Diagnostic.Warning),
        ):
            with self.subTest(kind=kind):
                raw = declaration.encode('cp932')
                unit = index.parse('synthetic.cpp', args=['-x', 'c++', '--target=i686-pc-windows-msvc'],
                                   unsaved_files=[('synthetic.cpp', raw)])
                self.assertIn((severity, 'illegal character encoding in string literal'),
                              [(d.severity, d.spelling) for d in unit.diagnostics])
                (self.root / 'main.cpp').write_bytes(raw)
                payload = self.parser().parse(self.spec)
                self.assertEqual(payload.parse_status, ParseStatus.PARSED_CLEAN, payload.diagnostics)
                self.assertFalse(payload.diagnostics)
                self.assertEqual((self.root / 'main.cpp').read_bytes(), raw)
        (self.root / 'main.cpp').write_bytes('const wchar_t* text=L"あ";'.encode('cp932'))
        payload = self.parser(parser_input_policy='native').parse(self.spec)
        self.assertEqual(payload.parse_status, ParseStatus.PARSED_WITH_ERRORS)

    def test_mixed_transitive_headers_original_spans_columns_signatures_and_hashes(self):
        sources = {
            'main.cpp': ('#include "wide.h"\r\n#include "utf.h"\r\n'
                         '/* あ */ int run() { const wchar_t* s=L"あ"; return target(); }\r\n', 'cp932'),
            'wide.h': ('#include "odd.fragment"\r\n/* 日本 */ inline int target() { return leaf(); }\r\n', 'cp932'),
            'odd.fragment': ('/* 日本 */ inline int leaf() { const wchar_t* s=L"あ"; return 1; }\r\n', 'cp932'),
            'utf.h': ('// Ελληνικά\r\ninline int other() { return 3; }\r\n', 'utf-8-sig'),
        }
        raw_by_path = {}
        for path, (text, codec) in sources.items():
            raw_by_path[path] = text.encode(codec)
            (self.root / path).write_bytes(raw_by_path[path])
        parser = self.parser()
        payload = parser.parse(self.spec)
        self.assertEqual(payload.parse_status, ParseStatus.PARSED_CLEAN, payload.diagnostics)
        entities = {e.semantic_key: e.name for e in payload.entities}
        for name, path, expected in [('run', 'main.cpp', 'int run()'),
                                     ('target', 'wide.h', 'inline int target()'),
                                     ('leaf', 'odd.fragment', 'inline int leaf()')]:
            occ = next(o for o in payload.occurrences if entities[o.entity_semantic_key] == name)
            raw = raw_by_path[path]
            self.assertEqual(occ.span.byte_start, raw.index(expected.encode('ascii')))
            self.assertEqual(occ.span.start_col, occ.span.byte_start - raw.rfind(b'\n', 0, occ.span.byte_start))
            self.assertEqual(occ.signature, expected)
        call = next(o for o in payload.observations if o.observation_type == 'call_expr' and o.span.repo_path == 'main.cpp')
        raw = raw_by_path['main.cpp']
        self.assertEqual(raw[call.span.byte_start:call.span.byte_end], b'target()')
        self.assertEqual(call.span.byte_start, raw.index(b'target()'))
        for relation in payload.relations:
            if relation.kind == RelationKind.CALLS:
                for ev in relation.evidence:
                    original = raw_by_path[ev.span.repo_path][ev.span.byte_start:ev.span.byte_end]
                    self.assertEqual(ev.evidence_snippet_hash, hashlib.sha256(original).hexdigest()[:16])
        self.assertEqual(parser.last_file_hashes,
                         {p: hashlib.sha256(b).hexdigest() for p, b in raw_by_path.items()})

    def test_signature_with_legacy_parameter_and_diagnostic_after_multibyte_text(self):
        raw = '/* あ */ void take(const wchar_t* s=L"日本");\r\n'.encode('cp932')
        (self.root / 'main.cpp').write_bytes(raw)
        payload = self.parser().parse(self.spec)
        self.assertEqual(payload.parse_status, ParseStatus.PARSED_CLEAN, payload.diagnostics)
        self.assertIn('L"日本"', next(o.signature for o in payload.occurrences if 'take' in o.signature))
        raw = '/* あ */ int value = missing_name;\r\n'.encode('cp932')
        (self.root / 'main.cpp').write_bytes(raw)
        payload = self.parser().parse(self.spec)
        self.assertEqual(payload.parse_status, ParseStatus.PARSED_WITH_ERRORS)
        diagnostic = next(d for d in payload.diagnostics if 'missing_name' in d.message)
        self.assertEqual(diagnostic.span.byte_start, raw.index(b'missing_name'))
        self.assertEqual(diagnostic.span.start_col, raw.index(b'missing_name') + 1)
        self.assertFalse(payload.entities)

    def test_utf16_main_and_header_are_mapped_to_original_evidence(self):
        for codec in ('utf-16', 'utf-32', 'utf-8-sig'):
            with self.subTest(codec=codec):
                main = '#include "wide.h"\r\n/* あ */ int run() { return target(); }\r\n'.encode(codec)
                header = '/* あ */ inline int target() { return 1; }\r\n'.encode(codec)
                (self.root / 'main.cpp').write_bytes(main)
                (self.root / 'wide.h').write_bytes(header)
                payload = self.parser().parse(self.spec)
                self.assertEqual(payload.parse_status, ParseStatus.PARSED_CLEAN, payload.diagnostics)
                entities = {e.semantic_key:e.name for e in payload.entities}
                run = next(o for o in payload.occurrences if entities[o.entity_semantic_key]=='run')
                source_codec = 'utf-16-le' if codec == 'utf-16' else 'utf-32-le' if codec == 'utf-32' else 'utf-8'
                self.assertEqual(run.span.byte_start, main.index('int run'.encode(source_codec)))
                self.assertEqual(run.span.start_line, 2)
                self.assertEqual(run.span.start_col, len('/* あ */ '.encode('utf-16-le' if codec=='utf-16' else 'utf-32-le' if codec=='utf-32' else 'utf-8'))+1)

    def test_mutation_and_wrong_consumed_overlay_never_publish_verified_facts(self):
        from clang import cindex
        for changed in ('main.cpp', 'wide.h'):
            with self.subTest(changed=changed):
                (self.root / 'main.cpp').write_bytes('#include "wide.h"\nint run() { return target(); }\n'.encode('cp932'))
                (self.root / 'wide.h').write_bytes('inline int target() { const wchar_t* s=L"あ"; return 1; }\n'.encode('cp932'))
                actual = cindex.Index.create()
                class MutatingIndex:
                    def parse(inner, *args, **kwargs):
                        unit = actual.parse(*args, **kwargs)
                        (self.root / changed).write_bytes(b'int changed();\n')
                        return unit
                parser = self.parser()
                parser._index = MutatingIndex()
                payload = parser.parse(self.spec)
                self.assertEqual(payload.parse_status, ParseStatus.STALE)
                self.assertFalse(payload.entities)
        (self.root / 'main.cpp').write_bytes('const wchar_t* text=L"あ";\n'.encode('cp932'))
        import legacygraph.clang_parser as module
        original = module._compiled_file_bytes
        with patch.object(module, '_compiled_file_bytes', side_effect=lambda tu, path: original(tu, path)+b' '):
            payload = self.parser().parse(self.spec)
        self.assertEqual(payload.parse_status, ParseStatus.STALE)

    def test_invalid_included_encoding_is_explicit_failure(self):
        (self.root / 'main.cpp').write_text('#include "wide.h"\nint run();\n')
        (self.root / 'wide.h').write_bytes(b'//\x81')
        result = self.parser().parse(self.spec)
        self.assertEqual(result.parse_status, ParseStatus.FAILED)
        self.assertEqual(result.diagnostics[-1].code, 'clang.unsupported_encoding')

    def test_external_headers_are_not_transcoded_and_symlink_containment_is_preserved(self):
        with tempfile.TemporaryDirectory() as outside:
            header = Path(outside) / 'external.h'
            header.write_bytes('const wchar_t* external_text=L"あ";\n'.encode('cp932'))
            (self.root / 'escape.h').symlink_to(header)
            (self.root / 'main.cpp').write_text('#include "escape.h"\nint run();\n')
            parser = self.parser()
            result = parser.parse(self.spec)
            self.assertEqual(result.parse_status, ParseStatus.PARSED_WITH_ERRORS)
            self.assertNotIn('escape.h', parser.last_consumed_files)
            self.assertFalse(result.entities)

    def test_mfc_macro_offsets_comments_and_inactive_branches(self):
        example = Path(__file__).resolve().parents[1] / 'examples' / 'mfc_demo'
        shutil.copytree(example, self.root, dirs_exist_ok=True)
        source = (self.root / 'demo.cpp').read_text()
        source = 'const wchar_t* text=L"日本";\n' + source
        source = source.replace('    ON_COMMAND(ID_FILE_SAVE', '    /* あ */ ON_COMMAND(ID_FILE_SAVE')
        source = source.replace('void COrderDlg::OnSave()', '/* 日本 */ void COrderDlg::OnSave()')
        source += '\n// ON_COMMAND(ID_FILE_SAVE, &COrderDlg::OnCancel)\n'
        raw = source.replace('\n','\r\n').encode('cp932')
        (self.root / 'demo.cpp').write_bytes(raw)
        manifest = self.root / 'manifest.json'
        parser = self.parser()
        database = self.root / '.legacygraph' / 'index.db'
        result = scan_repository(self.root, database, manifest, preferred_legacy_encoding='cp932', parser=parser)
        self.assertEqual(result['failed_translation_units'], 0, result['diagnostics'])
        # Parser's last TU is model.cpp; parse demo independently for its offsets.
        from legacygraph.build_context import load_build_manifest
        spec = next(s for s in load_build_manifest(self.root, manifest).translation_units if s.main_file_path=='demo.cpp')
        parser.parse(spec)
        active = raw.index(b'ON_COMMAND(ID_FILE_SAVE')
        inactive = raw.index(b'ON_COMMAND(ID_FILE_SAVE', active+1)
        self.assertIn(active, parser.last_macro_offsets['demo.cpp'])
        self.assertNotIn(inactive, parser.last_macro_offsets['demo.cpp'])
        with IndexStore(database) as store:
            mappings = store.query_published_relations(context_key=spec.build_context.context_key, kind=RelationKind.HANDLES_EVENT)
            self.assertEqual(len(mappings), 2)
            self.assertTrue(all(r['resolution_status']=='resolved' for r in mappings))
            row = store._conn.execute("SELECT content_sha256 FROM file_versions fv JOIN files f ON f.current_version_id=fv.id WHERE f.repo_path='demo.cpp'").fetchone()
            self.assertEqual(row[0], hashlib.sha256(raw).hexdigest())
            symbol = next(e for e in store.query_published_entities(limit=100) if e['qualified_name']=='COrderDlg::OnSave')
            snippet = QueryService(store).snippet(symbol['semantic_key'], spec.build_context.context_key)
            self.assertIn('/* 日本 */ void COrderDlg::OnSave()', snippet['content'])
        self.assertEqual((self.root / 'demo.cpp').read_bytes(),raw)

    def test_incremental_policy_change_header_change_and_partial_coverage(self):
        (self.root / 'main.cpp').write_bytes('int run() { const wchar_t* text=L"あ"; return 1; }\n'.encode('cp932'))
        (self.root / 'bad.cpp').write_bytes(b'int bad() { return absent; }\n')
        manifest = self.root / 'manifest.json'
        manifest.write_text(json.dumps({'contexts':[{'id':'c','compiler_family':'msvc'}],
                                       'translation_units':[{'file':'main.cpp','context_id':'c'}]}))
        database = self.root / '.legacygraph' / 'index.db'
        def scan(**kwargs):
            return scan_repository(self.root,database,manifest,**kwargs)
        first=scan(preferred_legacy_encoding='cp932')
        self.assertEqual(first['clean_translation_units'],1)
        self.assertTrue(scan(preferred_legacy_encoding='cp932')['noop'])
        from legacygraph.cli import main as cli_main
        output = io.StringIO()
        with redirect_stdout(output):
            rc = cli_main(['status', '--db', str(database), '--check-invalidated',
                           '--legacy-encoding', 'cp932', '--parser-input', 'utf8-overlay', '--json'])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(output.getvalue())['invalidated_translation_units'], [])
        output = io.StringIO()
        with redirect_stdout(output):
            rc = cli_main(['scan', '--repo', str(self.root), '--db', str(database),
                           '--manifest', str(manifest), '--legacy-encoding', 'cp932',
                           '--parser-input', 'utf8-overlay', '--json'])
        self.assertEqual(rc, 0)
        self.assertTrue(json.loads(output.getvalue())['noop'])
        # Alias normalization must not cause invalidation.
        self.assertTrue(scan(preferred_legacy_encoding='ms932')['noop'])
        changed=scan(preferred_legacy_encoding='cp1252')
        self.assertFalse(changed['noop'])
        self.assertIn('parser_version_changed',changed['invalidated_translation_units'][0]['reasons'])
        scan(preferred_legacy_encoding='cp932')
        with IndexStore(database) as store:
            context = store._conn.execute('SELECT context_key FROM build_contexts').fetchone()[0]
            symbol = next(e for e in store.query_published_entities(limit=100) if e['name']=='run')
            # Simulate an old inventory entry with a wrong yet lossless codec.
            store._conn.execute("UPDATE file_versions SET encoding='cp1252' WHERE file_id=(SELECT id FROM files WHERE repo_path='main.cpp')")
            snippet=QueryService(store).snippet(symbol['semantic_key'],context)
            self.assertEqual(snippet['encoding'],'cp932')
            self.assertIn('return 1',snippet['content'])
            self.assertIn('L"あ"',snippet['content'])
        native=scan(preferred_legacy_encoding='cp932',parser_input_policy='native')
        self.assertEqual(native['failed_translation_units'],1)
        self.assertEqual(native['published_generation']['verified_tu_runs'],0)
        manifest.write_text(json.dumps({'contexts':[{'id':'c','compiler_family':'msvc'}],
                                       'translation_units':[{'file':p,'context_id':'c'} for p in ('main.cpp','bad.cpp')]}))
        partial=scan(preferred_legacy_encoding='cp932')
        self.assertEqual(partial['clean_translation_units'],1)
        self.assertEqual(partial['failed_translation_units'],1)
        self.assertEqual(partial['published_generation']['verified_tu_runs'],1)
        (self.root / 'main.cpp').write_text('#include "wide.h"\nint run() {return target();}\n')
        (self.root / 'wide.h').write_bytes('inline int target() {const wchar_t* s=L"あ";return 1;}\n'.encode('cp932'))
        scan(preferred_legacy_encoding='cp932')
        (self.root / 'wide.h').write_bytes('inline int target() {const wchar_t* s=L"日本";return 2;}\n'.encode('cp932'))
        updated=scan(preferred_legacy_encoding='cp932')
        self.assertEqual(updated['clean_translation_units'],1)
        self.assertTrue(any('dependency' in reason for row in updated['invalidated_translation_units'] for reason in row['reasons']))
