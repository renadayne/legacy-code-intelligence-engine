# Validation record

Date: 2026-10-28. This records executed checks, not enterprise-scale certification.

## Environment

- Windows x64; Python 3.12.14; SQLite 3.53.1.
- Semantic environment: optional `libclang==18.1.1` Windows wheel in `.venv`.
- A separate Python environment without libclang was used for core tests and
  installed-package query checks.
- Runtime source/data stayed local; package download was an explicit development
  setup step. No npm, frontend build service, graph database server or cloud AI
  service was used.

## Automated verification

Run from the repository root:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The suite covers storage and generation rollback; manifest/config isolation;
Windows quoting and order-sensitive defines; Unicode byte spans; source path
containment; real Clang overload/linkage/virtual/indirect-call behavior;
unevaluated expressions and parameter defaults; incremental changes, deletion,
include shadowing and source-mutation races; MFC activity and semantic binding;
bounded graph/path queries, evidence sampling, source freshness and context
serialization; public CLI workflows; HTTP routing and local access boundaries.

Semantic tests explicitly skip without libclang; core query/export/server tests
remain runnable. Test counts and final command outcomes are reported in the
implementation handoff rather than treated as a permanent performance metric.

Independent review regressions were added before accepting implementations.
One 6,000-edge hub fixture caught approximately 20 MB of Python allocation for
`neighbors(limit=1)` before SQL-level bounds were applied. The accepted test
requires peak traced Python allocation below 3 MB for that workload. This does
not measure native SQLite/Clang RSS or prove million-line capacity.

## Executed end-to-end workflow

1. Scanned `examples/mfc_demo` using its explicit manifest: 5 source/header files,
   53 physical source lines, 2 clean translation units, 0 parser diagnostics.
2. Bound 2 active MFC-like registrations; the map inside `#if 0` produced no
   active handler edge. Retained indirect callback uncertainty separately.
3. Queried `COrderDlg::OnSave → SaveOrder → Validate(int)` across files.
4. Exported valid JSON and Markdown with original source, evidence, file hashes,
   configuration provenance, estimated token usage and explicit caveats.
5. Repeated the scan unchanged and confirmed incremental no-op behavior.
6. Opened the real local explorer in a browser: search, depth-2 graph (3 nodes /
   2 edges), source/evidence panel, dependency path and context download worked.
   Browser console had no errors.
7. Exercised browser fixtures for stale source, truncated path, insufficient
   budget and context changes, confirming no stale source or prior-context graph
   was presented as current.

## Offline package verification

A wheel was built with no package index, no dependency resolution and no frontend
build tools:

```powershell
python -m pip wheel . --no-index --no-build-isolation --no-deps --no-cache-dir --wheel-dir .legacygraph\wheels
```

The wheel was installed into an isolated local target without libclang. Its CLI
queried the existing demo index successfully. `index.html`, `app.js`, and
`style.css` were present in the installed package.

## Still requires an internal pilot

- Real MSVC/MFC/Windows SDK headers, project macros and generated-source setup.
- A representative company module and an approved agent/developer investigating
  a historical bug; the bundled sample uses macro stubs.
- Cold/warm throughput, whole-process/native memory and p95/p99 query behavior on
  1–5 million LoC, including high-fan-out headers and multiple configurations.
- Dependency/license/policy approval in the actual deployment environment.

The benchmark protocol and proposed acceptance targets are documented in
`IMPLEMENTATION_PLAN.vi.md`. Full state/alias dataflow, automatic MSBuild
evaluation, complete project-wide resource compilation and MCP transport are
documented future extensions, not implemented capabilities.
