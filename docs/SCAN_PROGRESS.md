# Opt-in scan progress and monotonic wall timings

Add `--progress` to a **future** CLI scan invocation. No dependencies, telemetry,
network service, parser compatibility switches, concurrency or encoding-policy
changes are introduced by progress reporting. Existing processes keep running
their loaded code: replacing files does not instrument an already running scan.
Do not stop a scan or start a second scan on its database to install this feature.

Example with the public bundled fixture (from the checkout):

```sh
python -m legacygraph scan --repo examples/mfc_demo \
  --manifest examples/mfc_demo/manifest.json --db .legacygraph/demo.db \
  --json --progress > scan.json 2> scan-progress.log
```

Each progress line is prefixed `[progress] `, followed by a bounded JSON event,
written to **stderr and flushed immediately**, including when redirected. Stdout
remains the same single final JSON document, with all existing diagnostic records
and summary fields. No output changes occur without `--progress`. Existing exit
codes remain: 0 for no failed requested work, 3 for failed requested TUs, 2 for
unavailable libclang, 1 for other scan exceptions. Stdout write/flush failures
propagate rather than pretending output completed successfully.

## Reading events

- `phase` / `state`: `started` means execution entered an operation; `finished`
  means that operation returned; `failed` or `cancelled` means it did not finish.
- Scan phases include discovery, manifest import, inventory synchronization,
  invalidation, generation creation, TU processing, publication, and rollback.
- `schedule` reports requested, selected, reused, deleted and deactivated counts
  only once known. Deleted counts refer to selected missing main-file TUs;
  deactivated counts refer to manifest membership removal.
- A TU uses an opaque `tu` ordinal and `scheduled` count, without a filename or
  context identifier. `parse:finished` only means the parser returned a payload.
- Parser details distinguish input preparation, each actual `native_parse`
  start/end (`round`, `overlays`), overlay discovery, dependencies/diagnostics,
  AST/evidence work and parser source validation. Discovery-round compiler
  messages are never exposed or counted as final errors.
- Scanner augmentation, consumed inventory synchronization, source validation
  and persistence have their own boundaries. Only subsequent `tu:finished`
  reports the final validated/persisted status and diagnostic record counts.
  This outcome remains provisional until the scan transaction commits.
- Missing main files finish as failed without fake native parse events. No-op
  scans have no TU parse or publication events.
- `publication_transaction:finished` records the staging update inside the outer
  transaction; it is not proof of commit. `publication:finished committed=true`
  is emitted only after the outer transaction returns successfully. Its duration
  includes staging, status collection and commit. Scan errors/cancellation do not
  emit publication success; rollback restores prior state.

A final `scan:finished` event reports `completed`, `clean`, `errored` (parsed with
errors), `failed` (parser/configuration/deletion failure), `stale`, `reused`, and
bounded severity totals from **complete final diagnostic records**. These counts
refer to records, not distinct defects; progress adds no deduplication. Existing
parser/adapter deduplication behavior is unchanged. Warning volume cannot hide
error/fatal counters. No raw messages or examples are copied into progress.

Publication, requested-work outcomes and snapshot coverage are separate:

- `publication`: `published` or `unchanged` for a no-op.
- `requested_work`: `failed` if any requested TU failed, regardless of publication.
- `coverage`: `complete`, `partial`, `empty`, or `unknown`, comparing known
  verified snapshot runs with active requested TUs.
- `newly_verified`, `inherited_verified`, `verified_snapshot`: separate clean
  work performed now from verified runs inherited from a prior generation.
- `native_parse_calls` counts observed native invocations, not completed TUs.
  With an uninstrumented custom parser it is `null` (unknown), accompanied by
  `observed_native_parse_calls` for the calls actually visible to instrumentation.

On an engine exception or cancellation, `scan:failed` / `scan:cancelled` carries
bounded completed-TU counters with publication not confirmed and coverage unknown.
It is not a successful completion or a report that a new snapshot was committed.

## Timing scope and output completion

All durations use `perf_counter` seconds; wall-clock publication timestamps are
not process durations. `elapsed_scan_s` is relative to the engine scan entry;
`elapsed_s` is the inclusive wall duration of the named phase/TU, excluding
delivery of its own start event. Parent phases include nested reporting costs. Nested phases
must **not** be added to their parent totals. Native parse-call time excludes
subsequent lazy AST traversal and validation. Counts are not time estimates: no
ETA or percentage-of-runtime prediction is made.

The final engine event precedes CLI output. With `--json --progress`, separate
`cli_json_serialization` and `cli_stdout_write_flush` events measure the existing
JSON serialization and final stdout write/flush. These events have `elapsed_cli_s`
relative to CLI scan entry, covering engine time plus output. Serialization
retains one JSON string just as before; no second retained copy is made for
measurement. A slow output consumer can delay output completion after the engine
returns. These events enable diagnosis; they do not establish a known bottleneck.
No successful output-end event is emitted after failed serialization/write/flush.

Start/end events are **not a heartbeat**. A pending native operation may emit no
further event until it returns; silence proves neither a hang nor healthy progress.
This implementation adds no watchdog, background thread/process or auto-retry.

## Library API, observer failures and privacy

`scan_repository(..., on_event=callback)` optionally receives structured events;
the library never writes progress itself. An optional `clock` callable supports
deterministic tests. Custom `parse(spec)` implementations require no changes;
scanner phases still work, but private parser instrumentation is enabled only
for the built-in parser implementation. Existing parser observer state is restored
when a parse returns or raises.

Ordinary observer exceptions (including a broken stderr sink) disable reporting
for that reporter and do not affect scan results, commit or rollback. A separately
created CLI output reporter may attempt reporting once more. Scanner exceptions
are still propagated. KeyboardInterrupt/SystemExit are cancellations, not ordinary
observer failures; transaction scopes retain their existing rollback behavior.
Output errors on stdout propagate even if the engine already committed; this
cannot undo an already committed snapshot.

Default events contain opaque ordinals and fixed aggregate fields only: no repo
roots, TU/context keys, paths, source text, symbols, raw compiler messages or raw
exception text. Location provenance is `unknown`; no main-file fallback explanation
is invented. The existing final diagnostic JSON and existing CLI error messages
remain separate and may contain sensitive information. Keep those local. No
source-relative progress label option or diagnostic-family inference is added.
No private-source acceptance or compatibility claim follows from a successful
scan event. Narrowing/list-initialization and ABI errors remain compiler errors.

## Verification and overhead measurement

13 focused tests cover stdout/stderr separation and unchanged exit codes; actual
multi-round CP932 include overlays; a synchronously observed start before a blocked
custom parser; deterministic nested, TU, engine and output timings; source changes
after parsing; missing/reused/no-op TUs; rollback/cancellation; observer and output
failures; privacy; enabled/disabled semantic evidence and invalidation equivalence;
and 2,000 warning records alongside a synthetic narrowing error and another clean
TU. Existing semantic, encoding, atomic storage, context isolation and HTTP tests
also pass (108 tests including the new focused cases).

Run the existing generated-input benchmark with optional paired measurement:

```sh
python scripts/benchmark_encoding.py --functions 500 --progress-overhead
```

Observed on Linux, Python 3.12.14, libclang 18.1.1, a 3-file / 38,561-byte generated
fixture: three forced scans per mode, alternating order, averaged **0.622101 s**
without reporting and **0.601507 s** with progress flushed to a temporary file.
The observed difference was **-0.020594 s (~-3.3%)**. Three reported scans wrote
20,482 bytes in total. Real scheduling, filesystem, terminal and pipe behavior
can dominate this difference; a negative observed difference is measurement
noise, not evidence that progress accelerates scans. These samples do not isolate
a causal reporting overhead and are not a controlled performance
study or a private-workload speed claim. The optional benchmark performs extra
scans only on its own generated temporary fixture and database, never on user
paths. Instrumentation adds bounded work per phase/TU/round, no extra AST traversal
or source hashing for display, and retains no unbounded event history.
