"""Generated AST profiles only. Never accepts application paths or manifests."""
import argparse
from contextlib import contextmanager
import cProfile
import importlib.metadata
import subprocess
import types
from statistics import mean
import json
from pathlib import Path
import platform
import pstats
import sys
import tempfile
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from legacygraph.clang_parser import ClangParser
from legacygraph.models import BuildContextSpec, TranslationUnitSpec


def fixture(parent, count, external, error, overlay):
    root=parent/'repo'; root.mkdir(exist_ok=True)
    headers=parent/'sdk' if external else root/'headers'; headers.mkdir(exist_ok=True)
    header='#pragma once\n'+''.join(
        f'#define SYNTH_VALUE_{i} {i}\nstruct SyntheticItem{i} {{ int value; int read() const {{ return value; }} }};\n'
        for i in range(count))
    (headers/'shared.h').write_text(header)
    (root/'first.h').write_text('#include "shared.h"\ninline int first(){return SYNTH_VALUE_0;}\n')
    (root/'second.h').write_text('#include "shared.h"\ninline int second(){return SYNTH_VALUE_1;}\n')
    text='#include "first.h"\n#include "second.h"\n'
    if overlay:text+='/* 日本 */ const wchar_t* text=L"あ";\n'
    text+='int entry(){return first()+second();}\n'
    if error:text+='struct Options{unsigned mask;}; Options options{-1};\n'
    (root/'main.cpp').write_bytes(text.encode('cp932' if overlay else 'utf-8'))
    spec=TranslationUnitSpec('main.cpp',BuildContextSpec('Synthetic','Demo','Debug','x64',
        compiler_family='msvc',include_dirs=(str(headers),)))
    return root,spec


def run(parser, spec, profile=False):
    durations={}; profiler=cProfile.Profile()
    original=parser._phase
    @contextmanager
    def phase(name,**fields):
        start=perf_counter()
        if name=='ast_evidence' and profile:profiler.enable()
        try:
            with original(name,**fields):yield
        finally:
            if name=='ast_evidence' and profile:profiler.disable()
            durations[name]=durations.get(name,0)+perf_counter()-start
    parser._phase=phase
    start=perf_counter();payload=parser.parse(spec);elapsed=perf_counter()-start
    parser._phase=original
    report={'parse_s':elapsed,'phases_s':durations,'status':payload.parse_status.value}
    if profile:
        rows=[]
        for (file,line,name),(cc,nc,tt,ct,callers) in pstats.Stats(profiler).stats.items():
            rows.append({'function':Path(file).name+':'+name,'calls':nc,'self_s':tt,'inclusive_s':ct})
        report['profile_top']=sorted(rows,key=lambda r:r['inclusive_s'],reverse=True)[:16]
        wanted={'_try_repo_relative','ensure_contained_path','get_children','location','file','name',
                '_visit_cursor','_cursor_span','_add_structural_relation','_extract_call_expr',
                '_get_instantiation','extent','referenced','get_usr','get_cstring'}
        report['profile_operations']=[r for r in rows if r['function'].split(':')[-1] in wanted]
    return payload,report


BASELINE = 'c889a85dd069448f2c52cba525d92900dddb0a60'


def baseline_parser():
    """Load the fixed, already-reviewed pre-change parser from local Git history."""
    source=subprocess.check_output(['git','show',BASELINE+':legacygraph/clang_parser.py'],
                                   cwd=Path(__file__).resolve().parents[1],text=True)
    module=types.ModuleType('legacygraph._benchmark_baseline')
    exec(compile(source,'baseline_clang_parser.py','exec'),module.__dict__)
    return module.ClangParser


def metadata(parser):
    return (parser.last_macro_offsets,parser.last_consumed_files,parser.last_file_hashes)


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--declarations',type=int,default=300)
    ap.add_argument('--compare',action='store_true',help='Paired runs against fixed local Git baseline c889a85; requires its history.')
    ap.add_argument('--repeats',type=int,default=3)
    args=ap.parse_args()
    if not 2<=args.declarations<=2000:ap.error('declarations must be 2..2000')
    if not 1<=args.repeats<=10:ap.error('repeats must be 1..10')
    baseline=baseline_parser() if args.compare else None
    rows=[]
    with tempfile.TemporaryDirectory(prefix='legacygraph-profile-') as temp:
        for external in (False,True):
            for error in (False,True):
                for overlay in (False,True):
                    parent=Path(temp)/str(len(rows));parent.mkdir()
                    root,spec=fixture(parent,args.declarations,external,error,overlay)
                    parser=ClangParser(root,preferred_legacy_encoding='cp932',parser_version='benchmark-equivalence')
                    payload,report=run(parser,spec,profile=True)
                    comparison={}
                    if baseline:
                        prior=baseline(root,preferred_legacy_encoding='cp932',parser_version='benchmark-equivalence')
                        prior_payload,prior_profile=run(prior,spec,profile=True)
                        assert prior_payload==payload and metadata(prior)==metadata(parser)
                        runs={'baseline':[],'changed':[]}
                        for repeat in range(args.repeats):
                            order=('baseline','changed') if repeat%2==0 else ('changed','baseline')
                            for label in order:
                                instance=(baseline if label=='baseline' else ClangParser)(root,preferred_legacy_encoding='cp932',parser_version='benchmark-equivalence')
                                actual,timings=run(instance,spec)
                                assert actual==payload and metadata(instance)==metadata(parser)
                                runs[label].append(timings)
                        comparison=dict(baseline_profile=prior_profile,
                            paired_runs=runs,paired_means={label:{
                                'parse_s':mean(r['parse_s'] for r in records),
                                'phases_s':{phase:mean(r['phases_s'][phase] for r in records)
                                            for phase in records[0]['phases_s']}}
                                for label,records in runs.items()},facts_equal=True)
                    rows.append(dict(external=external,error=error,overlay=overlay,
                        fixture_files=sum(p.is_file() for p in parent.rglob('*')),
                        fixture_bytes=sum(p.stat().st_size for p in parent.rglob('*') if p.is_file()),
                        **report,**comparison))
    print(json.dumps(dict(python=platform.python_version(),platform=platform.system(),
                         libclang=importlib.metadata.version('libclang'),baseline=BASELINE if baseline else None,
                         declarations=args.declarations,repeats=args.repeats,rows=rows),indent=2))


if __name__=='__main__':main()
