"""Bounded, local-only scan events. No source labels or diagnostic messages."""
from collections import Counter
from contextlib import contextmanager
from time import perf_counter


class Progress:
    def __init__(self, observer=None, clock=perf_counter, scope="scan"):
        self.scope = scope
        self.observer = observer
        self.clock = clock
        self.start = clock()
        self.native_calls = 0
        self.native_instrumentation_complete = True
        self.publication_start = None
        self.tu = {}
        self.outcomes = Counter()
        self.severities = Counter()

    def emit(self, phase, state, **fields):
        if self.observer is None:
            return
        event = dict(phase=phase, state=state, **self.tu, **fields)
        event[f'elapsed_{self.scope}_s'] = self.clock()-self.start
        try:
            self.observer(event)
        except Exception:
            # Reporting is best-effort. A broken observer/sink must never turn a
            # committed scan into failure or affect transactional semantics.
            self.observer = None

    @contextmanager
    def phase(self, name, **fields):
        self.emit(name, 'started', **fields)
        # Exclude delivery of the start line from the measured operation;
        # parent phases still include nested reporting overhead.
        start = self.clock()
        try:
            yield
        except BaseException as exc:
            self.emit(name, 'cancelled' if isinstance(exc, (KeyboardInterrupt, SystemExit)) else 'failed',
                      elapsed_s=self.clock()-start, **fields)
            raise
        else:
            self.emit(name, 'finished', elapsed_s=self.clock()-start, **fields)

    def tu_result(self, status, severities, start):
        self.outcomes[status] += 1
        self.severities.update(severities)
        self.emit('tu', 'finished', status=status, diagnostic_records=dict(severities),
                  location_quality='unknown', elapsed_s=self.clock()-start)

    def interrupted(self, state):
        self.tu = {}
        self.emit('scan', state, elapsed_s=self.clock()-self.start,
                  completed=sum(self.outcomes.values()), clean=self.outcomes['parsed_clean'],
                  errored=self.outcomes['parsed_with_errors'], failed=self.outcomes['failed'],
                  stale=self.outcomes['stale'], diagnostic_records=dict(self.severities),
                  publication='not_confirmed', coverage='unknown', verified_snapshot=None)

    def complete(self, result):
        outcomes = Counter(row['parse_status'] for row in result['tu_results'])
        severities = Counter(d['severity'] for d in result['diagnostics'])
        verified = (result['published_generation'] or {}).get('verified_tu_runs')
        requested = result['active_translation_units']
        clean = outcomes['parsed_clean']
        coverage = ('unknown' if verified is None else 'empty' if requested == 0 else
                    'complete' if verified == requested else 'partial')
        self.emit('scan', 'finished', noop=result['noop'],
                  elapsed_s=self.clock()-self.start,
                  requested=requested, completed=len(result['tu_results']), clean=clean,
                  errored=outcomes['parsed_with_errors'], failed=outcomes['failed'], stale=outcomes['stale'],
                  reused=result['reused_translation_units'],
                  native_parse_calls=self.native_calls if self.native_instrumentation_complete else None,
                  observed_native_parse_calls=self.native_calls,
                  diagnostic_records=dict(severities), location_quality='unknown',
                  publication='unchanged' if result['noop'] else 'published',
                  requested_work='failed' if result['failed_translation_units'] else 'succeeded',
                  coverage=coverage, verified_snapshot=verified,
                  newly_verified=clean,
                  inherited_verified=None if verified is None else max(0, verified-clean))


class StderrProgress:
    """One flushed line per event; raw source/compiler details never enter here."""
    def __init__(self, stream):
        self.stream = stream

    def __call__(self, event):
        import json
        self.stream.write('[progress] ' + json.dumps(event, sort_keys=True) + '\n')
        self.stream.flush()


def write_json(summary, stdout, progress):
    """Same JSON text as print(json.dumps(...)); no second retained JSON copy."""
    with progress.phase('cli_json_serialization'):
        text = __import__('json').dumps(summary, indent=2)
    with progress.phase('cli_stdout_write_flush'):
        stdout.write(text)
        stdout.write('\n')
        stdout.flush()
