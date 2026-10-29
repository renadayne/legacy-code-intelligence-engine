"""Measure generated source only; no application paths or data are accepted.

Run from the checkout: python scripts/benchmark_encoding.py --functions 500
No before/after performance claim is implied by these timings.
"""
import argparse
import json
from pathlib import Path
import platform
import sys
import tempfile
from time import perf_counter
import tracemalloc

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from legacygraph.clang_parser import ClangParser
from legacygraph.models import BuildContextSpec, ParseStatus, TranslationUnitSpec
from legacygraph.parser_input import SourceBuffer
from legacygraph.scanner import scan_repository


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--functions', type=int, default=500)
    ap.add_argument('--progress-overhead', action='store_true', help='Measure paired forced synthetic scans with/without flushed progress.')
    arguments = ap.parse_args()
    count = arguments.functions
    if not 1 <= count <= 10000:
        ap.error('--functions must be 1..10000')
    with tempfile.TemporaryDirectory(prefix='legacygraph-synthetic-') as directory:
        root = Path(directory)
        sources = {
            'main.cpp': ('#include "legacy.h"\n#include "utf.h"\n' + ''.join(
                f'/* 日本 */ int sample_{i}() {{ const wchar_t* text=L"あ"; return target(); }}\r\n'
                for i in range(count))).encode('cp932'),
            'legacy.h': '/* 日本 */ inline int target() { const wchar_t* text=L"あ"; return 1; }\r\n'.encode('cp932'),
            'utf.h': '// Ελληνικά\ninline int utf_target() { return 2; }\n'.encode('utf-8-sig'),
        }
        for path, raw in sources.items():
            (root / path).write_bytes(raw)
        start = perf_counter()
        views = [SourceBuffer.create(raw, 'cp932') for raw in sources.values()]
        mapping_seconds = perf_counter() - start
        # Trace allocation separately so tracing does not contaminate wall timings.
        tracemalloc.start()
        _ = [SourceBuffer.create(raw, 'cp932') for raw in sources.values()]
        _, mapping_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        parser = ClangParser(root, preferred_legacy_encoding='cp932')
        from clang import cindex
        actual = cindex.Index.create()
        native_times = []
        class TimedIndex:
            def parse(self, *args, **kwargs):
                start = perf_counter()
                unit = actual.parse(*args, **kwargs)
                native_times.append(perf_counter() - start)
                return unit
        parser._index = TimedIndex()
        spec = TranslationUnitSpec('main.cpp', BuildContextSpec('Synthetic','Demo','Debug','x86',compiler_family='msvc'))
        start = perf_counter()
        result = parser.parse(spec)
        parse_seconds = perf_counter() - start
        assert result.parse_status == ParseStatus.PARSED_CLEAN, result.diagnostics
        manifest = root / 'manifest.json'
        manifest.write_text(json.dumps({'translation_units':[{'file':'main.cpp','compiler_family':'msvc'}]}))
        db = root / '.legacygraph' / 'index.db'
        start = perf_counter()
        cold = scan_repository(root,db,manifest,preferred_legacy_encoding='cp932')
        cold_seconds = perf_counter() - start
        start = perf_counter()
        warm = scan_repository(root,db,manifest,preferred_legacy_encoding='cp932')
        warm_seconds = perf_counter() - start
        assert cold['failed_translation_units'] == 0 and warm['noop']
        progress_measurements = {}
        if arguments.progress_overhead:
            from legacygraph.progress import StderrProgress
            timings = {"disabled": [], "enabled": []}
            with (root / 'progress.log').open('w') as sink:
                observer = StderrProgress(sink)
                for repetition in range(3):
                    for enabled in ((False, True) if repetition % 2 == 0 else (True, False)):
                        start = perf_counter()
                        measured = scan_repository(root, db, manifest, force=True,
                            preferred_legacy_encoding='cp932', on_event=observer if enabled else None)
                        elapsed = perf_counter()-start
                        assert measured['failed_translation_units'] == 0
                        timings['enabled' if enabled else 'disabled'].append(elapsed)
            off = sum(timings['disabled'])/3
            on = sum(timings['enabled'])/3
            progress_measurements = dict(
                forced_scan_progress_disabled_mean_s=round(off,6),
                forced_scan_progress_enabled_mean_s=round(on,6),
                observed_progress_difference_s=round(on-off,6),
                progress_log_bytes=(root/'progress.log').stat().st_size,
                repetitions_per_mode=3,
            )
        print(json.dumps({
            **progress_measurements,
            'platform':platform.system(), 'python':platform.python_version(),
            'functions':count, 'files':len(sources),
            'original_bytes':sum(map(len,sources.values())),
            'buffer_mapping_seconds':round(mapping_seconds,6),
            'buffer_mapping_peak_python_bytes':mapping_peak,
            'semantic_parse_seconds':round(parse_seconds,6),
            'libclang_parse_calls':len(native_times),
            'libclang_parse_seconds':round(sum(native_times),6),
            'cold_scan_seconds':round(cold_seconds,6),
            'unchanged_scan_seconds':round(warm_seconds,6),
            'occurrences':len(result.occurrences),
            'diagnostics':len(result.diagnostics), 'warm_noop':warm['noop'],
        },indent=2))


if __name__ == '__main__':
    main()
