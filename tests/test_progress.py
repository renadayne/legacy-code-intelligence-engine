"""Observable scan boundaries using invented sources and isolated databases."""
import io
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from legacygraph.clang_parser import ClangParser, is_libclang_available
from legacygraph.cli import main
from legacygraph.index import IndexStore
from legacygraph.models import DiagnosticRecord, DiagnosticSeverity, ParseRunPayload, ParseStatus
from legacygraph.progress import Progress, StderrProgress, write_json
from legacygraph.scanner import scan_repository


class Clock:
    def __init__(self): self.now = 0
    def __call__(self): return self.now


class ProgressUnitTests(unittest.TestCase):
    def test_deterministic_nested_durations_and_failed_end(self):
        clock = Clock(); events = []; progress = Progress(events.append, clock)
        with progress.phase('inclusive'):
            clock.now = 2
            with progress.phase('detail'): clock.now = 5
            clock.now = 8
        self.assertEqual([e['elapsed_s'] for e in events if e['state']=='finished'], [3,8])
        with self.assertRaises(ValueError):
            with progress.phase('broken'): raise ValueError('private message')
        self.assertEqual(events[-1]['state'],'failed')
        self.assertNotIn('private message',json.dumps(events))
        def slow_start(event):
            events.append(event)
            if event['state']=='started':clock.now += 10
        progress=Progress(slow_start,clock)
        with progress.phase('native_parse'):clock.now += 3
        self.assertEqual(events[-1]['elapsed_s'],3)

    def test_json_output_timings_and_write_failure(self):
        clock=Clock(); events=[]; p=Progress(events.append,clock)
        class Sink(io.StringIO):
            def write(self, text): clock.now += 2; return super().write(text)
            def flush(self): clock.now += 3
        real_dumps=json.dumps
        def dumps(*args,**kwargs): clock.now += 7; return real_dumps(*args,**kwargs)
        sink=Sink()
        with patch('json.dumps',side_effect=dumps): write_json({'ok':True},sink,p)
        self.assertEqual(sink.getvalue(),'{\n  "ok": true\n}\n')
        done=[e for e in events if e['state']=='finished']
        self.assertEqual([e['elapsed_s'] for e in done],[7,7])
        events.clear()
        class Broken:
            def write(self,text): raise OSError('private sink')
        with self.assertRaises(OSError): write_json({},Broken(),p)
        self.assertEqual(events[-1]['phase'],'cli_stdout_write_flush')
        self.assertEqual(events[-1]['state'],'failed')
        events.clear()
        class BrokenFlush(io.StringIO):
            def flush(self): raise OSError('private flush')
        with self.assertRaises(OSError): write_json({},BrokenFlush(),p)
        self.assertEqual(events[-1]['phase'],'cli_stdout_write_flush')
        self.assertEqual(events[-1]['state'],'failed')
        events.clear()
        class BadJSON: pass
        with self.assertRaises(TypeError): write_json({'bad':BadJSON()},sink,p)
        self.assertEqual(events[-1]['state'],'failed')

    def test_flush_each_event_and_disable_broken_observer(self):
        class Sink(io.StringIO):
            flushes=0
            def flush(self): self.flushes+=1
        sink=Sink(); observer=StderrProgress(sink)
        observer({'phase':'scan','state':'started'})
        observer({'phase':'scan','state':'finished'})
        self.assertEqual(sink.flushes,2)
        calls=[]
        def broken(event): calls.append(event); raise OSError('hidden')
        p=Progress(broken)
        p.emit('scan','started');p.emit('scan','finished')
        self.assertEqual(len(calls),1)


@unittest.skipUnless(is_libclang_available()[0], 'Requires optional libclang')
class ScanProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.manifest=self.root/'manifest.json'; self.db=self.root/'.legacygraph'/'test.db'
        self.fixture({'sensitive_main.cpp':'int private_symbol() {return 1;}\n'})

    def fixture(self,sources):
        for path,text in sources.items():
            (self.root/path).write_bytes(text if isinstance(text,bytes) else text.encode())
        self.manifest.write_text(json.dumps({'contexts':[{'id':'private_context','compiler_family':'clang'}],
            'translation_units':[{'file':name,'context_id':'private_context'} for name in sources if name.endswith('.cpp')]}))

    def scan(self,**kwargs): return scan_repository(self.root,self.db,self.manifest,**kwargs)
    def final(self,events): return next(e for e in reversed(events) if e['phase']=='scan' and e['state']=='finished')
    def events_for(self,events,phase,state=None): return [e for e in events if e['phase']==phase and (state is None or e['state']==state)]

    def test_cli_progress_json_separation_and_default_silence(self):
        args=['scan','--repo',str(self.root),'--manifest',str(self.manifest),'--db',str(self.db),'--json']
        out=io.StringIO();err=io.StringIO()
        with redirect_stdout(out),redirect_stderr(err): code=main(args+['--progress'])
        self.assertEqual(code,0);self.assertFalse(json.loads(out.getvalue())['noop'])
        events=[json.loads(line.removeprefix('[progress] ')) for line in err.getvalue().splitlines()]
        self.assertEqual(events[-1]['phase'],'cli_stdout_write_flush')
        self.assertEqual(events[-1]['state'],'finished')
        self.assertLess(next(i for i,e in enumerate(events) if e['phase']=='scan' and e['state']=='finished'),next(i for i,e in enumerate(events) if e['phase']=='cli_json_serialization'))
        self.assertNotIn(str(self.root),err.getvalue())
        self.assertNotIn('sensitive_main',err.getvalue());self.assertNotIn('private_symbol',err.getvalue())
        self.assertNotIn('private_context',err.getvalue())
        out=io.StringIO();err=io.StringIO()
        with redirect_stdout(out),redirect_stderr(err): code=main(args)
        self.assertEqual(code,0); self.assertTrue(json.loads(out.getvalue())['noop']);self.assertEqual(err.getvalue(),'')
        (self.root/'sensitive_main.cpp').write_text('struct V{unsigned n;}; V value{-1};')
        for enabled in (False,True):
            out=io.StringIO();err=io.StringIO()
            with redirect_stdout(out),redirect_stderr(err): code=main(args+['--force']+(['--progress'] if enabled else []))
            self.assertEqual(code,3)
            document=json.loads(out.getvalue())
            self.assertEqual(document['failed_translation_units'],1)
            if enabled:
                events=[json.loads(line.removeprefix('[progress] ')) for line in err.getvalue().splitlines()]
                self.assertEqual(self.final(events)['requested_work'],'failed')
            else:self.assertEqual(err.getvalue(),'')

    def test_start_is_delivered_before_blocking_custom_parser_and_no_private_hooks(self):
        entered=threading.Event();release=threading.Event();events=[];failures=[]
        class Custom:
            parser_name='fake';parser_version='1';mfc_rule_version='1'
            def parse(inner,spec):
                self.assertTrue(any(e['phase']=='parse' and e['state']=='started' for e in events))
                entered.set()
                if not release.wait(5): raise RuntimeError('test timeout')
                return ParseRunPayload(spec.tu_key,'fake','1','1',ParseStatus.PARSED_CLEAN)
        def run():
            try:self.scan(parser=Custom(),on_event=events.append)
            except BaseException as exc:failures.append(exc)
        worker=threading.Thread(target=run);worker.start()
        try:self.assertTrue(entered.wait(5));self.assertFalse(self.events_for(events,'parse','finished'))
        finally:release.set();worker.join(5)
        self.assertFalse(worker.is_alive());self.assertFalse(failures)
        self.assertIsNone(self.final(events)['native_parse_calls'])

    def test_scanner_clock_and_final_validated_tu_duration(self):
        clock=Clock();events=[]
        class Custom:
            parser_name='fake';parser_version='1';mfc_rule_version='1'
            def parse(self,spec):
                clock.now += 5
                return ParseRunPayload(spec.tu_key,'fake','1','1',ParseStatus.PARSED_CLEAN)
        self.scan(parser=Custom(),on_event=events.append,clock=clock)
        self.assertEqual(self.events_for(events,'parse','finished')[0]['elapsed_s'],5)
        self.assertEqual(self.events_for(events,'tu','finished')[0]['elapsed_s'],5)
        self.assertEqual(self.final(events)['elapsed_s'],5)

    def test_overlay_rounds_are_actual_calls_and_no_preliminary_diagnostics(self):
        self.fixture({'sensitive_main.cpp':'#include "private_header.h"\nint private_symbol(){return helper();}\n',
                      'private_header.h':'inline int helper(){const wchar_t* s=L"あ";return 1;}\n'.encode('cp932')})
        events=[];result=self.scan(preferred_legacy_encoding='cp932',on_event=events.append)
        self.assertEqual(result['failed_translation_units'],0)
        calls=self.events_for(events,'native_parse','finished')
        self.assertEqual([e['round'] for e in calls],[1,2])
        self.assertEqual(self.final(events)['native_parse_calls'],2)
        self.assertEqual(self.final(events)['diagnostic_records'],{})
        self.assertNotIn('illegal character',json.dumps(events))

    def test_stale_after_parse_then_persistence_then_final_result(self):
        import legacygraph.scanner as module
        original=module.augment_payload
        def mutate(*args,**kwargs):
            result=original(*args,**kwargs)
            (self.root/'sensitive_main.cpp').write_text('int changed(){return 2;}\n')
            return result
        events=[]
        with patch.object(module,'augment_payload',mutate):result=self.scan(on_event=events.append)
        self.assertEqual(result['failed_translation_units'],1)
        completed=self.events_for(events,'tu','finished')[0]
        self.assertEqual(completed['status'],'stale')
        self.assertLess(events.index(self.events_for(events,'parse','finished')[0]),events.index(completed))
        self.assertLess(events.index(self.events_for(events,'persistence','finished')[0]),events.index(completed))
        self.assertEqual(self.final(events)['coverage'],'partial')
        self.assertEqual(self.final(events)['stale'],1)

    def test_noop_reused_deleted_and_inherited_coverage(self):
        self.fixture({'sensitive_main.cpp':'int a(){return 1;}','b.cpp':'int b(){return 2;}'})
        self.scan();events=[];self.scan(on_event=events.append)
        final=self.final(events)
        self.assertTrue(final['noop']);self.assertEqual(final['reused'],2)
        self.assertEqual(final['inherited_verified'],2)
        self.assertFalse(self.events_for(events,'parse'));self.assertFalse(self.events_for(events,'publication'))
        (self.root/'b.cpp').write_text('int b(){return missing;}')
        events=[];self.scan(on_event=events.append);final=self.final(events)
        self.assertEqual((final['newly_verified'],final['inherited_verified'],final['errored']),(0,1,1))
        self.assertEqual(final['publication'],'published');self.assertEqual(final['requested_work'],'failed')
        self.assertEqual(final['coverage'],'partial')
        (self.root/'sensitive_main.cpp').unlink()
        events=[];self.scan(on_event=events.append)
        self.assertEqual(self.events_for(events,'schedule')[0]['deleted'],1)
        deleted=self.events_for(events,'tu','finished')[1] if self.events_for(events,'tu','finished')[0]['status']=='parsed_with_errors' else self.events_for(events,'tu','finished')[0]
        self.assertEqual(deleted['status'],'failed');self.assertEqual(deleted['diagnostic_records'],{'fatal':1})

    def test_rollback_error_and_cancellation_never_emit_successful_publication(self):
        initial=self.scan()
        for exception in (RuntimeError('private error'),KeyboardInterrupt()):
            events=[]
            def fail(store,gen):raise exception
            with self.assertRaises(type(exception)):self.scan(force=True,on_event=events.append,on_before_publish=fail)
            self.assertFalse(self.events_for(events,'publication','finished'))
            self.assertEqual(events[-1]['state'],'cancelled' if isinstance(exception,KeyboardInterrupt) else 'failed')
            self.assertTrue(self.events_for(events,'rollback','finished'))
            with IndexStore(self.db) as store:self.assertEqual(store.get_published_generation_id(),initial['generation_id'])
        events=[]
        with patch.object(IndexStore,'publish_generation',side_effect=RuntimeError('private')):
            with self.assertRaises(RuntimeError):self.scan(force=True,on_event=events.append)
        self.assertTrue(self.events_for(events,'publication','started'))
        self.assertFalse(self.events_for(events,'publication','finished'))

    def test_broken_observer_does_not_mask_parser_failure_or_affect_success(self):
        def broken(event):raise OSError('private destination')
        result=self.scan(on_event=broken);self.assertEqual(result['clean_translation_units'],1)
        class BrokenParser:
            parser_name='fake';parser_version='1';mfc_rule_version='1'
            def parse(self,spec):raise ValueError('scanner failure')
        with self.assertRaisesRegex(ValueError,'scanner failure'):self.scan(parser=BrokenParser(),force=True,on_event=broken)
        with IndexStore(self.db) as store:self.assertEqual(store.get_published_generation_id(),result['generation_id'])

    def test_many_warnings_and_narrowing_error_remain_visible_without_raw_progress(self):
        self.fixture({'sensitive_main.cpp':'struct V{unsigned n;}; V value{-1};\nint private_symbol(){return 1;}\n',
                      'other.cpp':'const wchar_t* text=L"あ";\n'.encode('cp932')})
        parser=ClangParser(self.root,preferred_legacy_encoding='cp932')
        real_parse=parser.parse
        def parse(spec):
            payload=real_parse(spec)
            return replace(payload,diagnostics=payload.diagnostics+tuple(
                DiagnosticRecord(DiagnosticSeverity.WARNING,f'invented.warning.{i}','private_identifier /* source */') for i in range(1000)))
        parser.parse=parse
        events=[];result=self.scan(parser=parser,preferred_legacy_encoding='cp932',on_event=events.append)
        self.assertEqual(result['failed_translation_units'],1)
        self.assertEqual(self.final(events)['diagnostic_records']['warning'],2000)
        self.assertGreaterEqual(self.final(events)['diagnostic_records']['error'],1)
        self.assertEqual(self.final(events)['location_quality'],'unknown')
        text=json.dumps(events)
        self.assertLess(len(text),40000);self.assertNotIn('private_identifier',text)
        self.assertNotIn('narrow',text);self.assertNotIn('encoding_resolved',text)
        self.assertGreaterEqual(len(result['diagnostics']),2001)
        self.assertEqual(self.final(events)['clean'],1)

    def test_semantic_equivalence_and_no_invalidation_from_observer(self):
        self.fixture({'sensitive_main.cpp':'#include "private.h"\nint private_symbol(){return helper();}\n',
                      'private.h':'inline int helper(){const wchar_t* s=L"あ";return 1;}\n'.encode('cp932')})
        def snapshot():
            with IndexStore(self.db) as store:
                return ([dict(r) for r in store._conn.execute('SELECT semantic_key,name FROM entities ORDER BY semantic_key')],
                        [tuple(r) for r in store._conn.execute('SELECT byte_start,byte_end,evidence_snippet_hash FROM relation_evidence ORDER BY id')],
                        [tuple(r) for r in store._conn.execute('SELECT source_entity_id,target_entity_id,kind,resolution_status,dispatch_kind,condition_state,reason FROM relations ORDER BY id')])
        initial=self.scan(preferred_legacy_encoding='cp932');before=snapshot();events=[]
        instrumented=self.scan(force=True,preferred_legacy_encoding='cp932',on_event=events.append)
        self.assertEqual(initial['failed_translation_units'],instrumented['failed_translation_units'])
        self.assertEqual(initial['published_generation']['verified_tu_runs'],instrumented['published_generation']['verified_tu_runs'])
        self.assertTrue(before[1])
        # Existing content-addressed entity records are reused. Compare the
        # repeated run's evidence values rather than generation row IDs.
        after=snapshot();self.assertEqual(before[0],after[0])
        self.assertEqual(set(before[1]),set(after[1]))
        self.assertEqual(set(before[2]),set(after[2]))
        self.assertTrue(self.scan(preferred_legacy_encoding='cp932',on_event=events.append)['noop'])
