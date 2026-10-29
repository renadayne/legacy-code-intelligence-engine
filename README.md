# LegacyGraph

Offline source code intelligence for legacy C++/MFC repositories on Windows.

## Quick start

Use an approved 64-bit Python 3.11+ interpreter. The commands below run directly from
this checkout; no frontend build tools, database service or cloud account are needed.

```powershell
python -m venv .venv
# Install once, only where your organization's dependency policy permits it.
.\.venv\Scripts\python.exe -m pip install libclang==18.1.1
.\.venv\Scripts\python.exe -m legacygraph scan --repo examples\mfc_demo --manifest examples\mfc_demo\manifest.json --db .legacygraph\demo.db
.\.venv\Scripts\python.exe -m legacygraph serve --db .legacygraph\demo.db
```

Open `http://127.0.0.1:8765`. Search for `OnSave`, select it, set depth to 2,
and inspect `OnSave → SaveOrder → Validate(int)`. Use inbound traversal to see
the MFC event registration. The bundled example uses MFC-like macro stubs;
it is not a substitute for validation with real MFC headers/source.

## Current capabilities

The implemented CLI/explorer MVP follows [the technical plan](docs/IMPLEMENTATION_PLAN.vi.md):

- **Zero Mandatory Third-Party Runtime Dependencies**: Core discovery, manifest import, and SQLite storage are built strictly on the Python `>=3.11` standard library (`sqlite3`, `hashlib`, `json`, `argparse`, `pathlib`, `shlex`, `dataclasses`, `enum`), with optional `libclang` (`pip install .[semantic]`) for semantic C++ AST parsing.
- **Read-Only Repository Discovery**: Scans source, header, resource (`.rc`, `.rc2`), and project metadata files without modifying the source tree or executing MSBuild/custom targets.
- **Path Containment & Output Exclusion**: Canonical path verification prevents path traversal or escaping the repository root; index and output directories (`.legacygraph/`, `legacygraph_out/`, SQLite `.db`/`.db-wal`/`.db-shm` files) are automatically excluded from discovery.
- **UTF-8 & Legacy Windows Encoding Awareness**: Detects UTF-8/UTF-16/UTF-32 BOMs, validates strict UTF-8, and falls back to legacy Windows codepages (`cp1252`, `cp932`, `latin-1`) while preserving raw byte sizes, SHA-256 content hashes, and exact 0-based line byte offsets.
- **Build Context & Compilation Database Import**: Imports either a LegacyGraph JSON build manifest or a Clang `compile_commands.json` database, preserving multiple compile commands/configurations (e.g. `Debug` vs `Release`, distinct defines/includes) for the same source file.
- **Versioned SQLite Storage & Atomic Generations**:
  - Schema for `repositories`, `files`, `file_versions`, `projects`, `build_contexts`, `translation_units`, `generations`, `parse_runs`, `generation_tu_runs`, `entities`, `symbol_occurrences`, `observations`, `relations`, `relation_evidence`, `relation_candidates`, `tu_dependencies`, and `diagnostics`.
  - Staging vs. published index generations (`begin_generation`, `record_parse_run`, `publish_generation`, `rollback_generation`) so failed or aborted indexing runs never corrupt the currently published snapshot.
  - Strict build-context isolation and content-hash invalidation across `.cpp` files, transitive header dependencies, build flags, and deleted files.
- **MFC evidence**: supported message maps are bound to exact semantic method
  identities only when active in the selected preprocessing context. Unknown
  handlers, overloads, custom macros and uncertain conditions stay explicit.
- **Bounded queries**: symbol search, caller/callee neighborhoods, expansion,
  collapse and dependency paths; the browser receives only graph fragments.
- **AI context packs**: original source, locations/hashes, signatures,
  relationships, event mappings, unresolved facts and omissions in Markdown/JSON.
- **Local graph explorer**: Vanilla JavaScript/SVG UI with filters, source/evidence
  inspection and context downloads. Assets are packaged locally, with no CDN.

## Semantic C++ Parsing & Incremental Indexing (Step 2)

When the optional `semantic` extra (`libclang==18.1.1`, the tested version) is installed in the local Python environment, LegacyGraph provides `ClangParser` (`legacygraph.clang_parser`) and `scan_repository` (`legacygraph.scanner`):

- **Semantic AST Extraction**: Extracts namespaces, classes, structs, enums, functions, methods, constructors, destructors, fields, typedefs, and file entities with original root-relative byte spans and signatures.
- **USR + Linkage Scope Identity**: External linkage symbols (`usr:<project>:<USR>`) are shared across translation units in the same project/context (connecting declarations and cross-file definitions without name-based guessing), while `static` and anonymous-namespace symbols (`tu:<project>:<main_file>:<USR>`) remain strictly isolated per translation unit.
- **Honest Call Classification & Evidence**:
  - Direct function/method/constructor calls resolve via Clang AST semantic identity (`dispatch_kind="direct"`, `reason="exact_semantic"`), distinguishing overloads (e.g. `Validate(int)` vs `Validate(double)`) and attributing calls inside lambdas to the lambda's `operator()` rather than the outer function.
  - Virtual method calls record the static target with `dispatch_kind="virtual"` and `reason="static_virtual_target"`, while explicitly qualified calls (`Base::Method()`) are classified as `direct`.
  - Function pointer and callback invocations (`CALL_EXPR` referencing a variable, parameter, or field) are never marked resolved to the variable; they are recorded as `resolution_status="unresolved"`, `dispatch_kind="indirect"`, `reason="indirect_function_pointer"`, preserving the callsite expression and evidence.
- **Safety & Incremental Invalidation**:
  - Never executes compiler commands from manifests (`compile_commands.json` or JSON manifests); unsupported flags or unexpanded response files (`@file.rsp`) emit explicit `unsupported` diagnostics. Precompiled header flags (`/Yu`) use header source rather than binary MSVC `.pch` files.
  - Verifies SHA-256 content hashes before and after parsing each consumed file to detect concurrent source mutation races (`ParseStatus.STALE`), and excludes `PARSED_WITH_ERRORS` / `FAILED` / `STALE` runs from the verified generation while surfacing all diagnostics in the scan summary.

## Honest Limitations (What Is Not Implemented Yet)

For legacy source parsing, see [parser encoding policy and original-byte evidence](docs/PARSER_ENCODING.md).
`scan --legacy-encoding cp932` now supplies strict UTF-8 parser overlays for contained
main files and consumed headers; original bytes remain unchanged. This analysis
model can change narrow-literal semantics and is not exact build emulation.

Use a stable source snapshot while indexing. Incremental hashing covers consumed
files inside the source root. SDK/external headers and environment changes are
not automatically tracked: pin the toolchain in the manifest and use `scan
--force` after changing it. The graph is a static, scoped index, not proof of
runtime reachability; implicit lifetime calls and execution of default arguments
are not fully modeled. Failed TUs are excluded and reported; `scan` exits with
code 3 when any requested TU fails (2 for unavailable libclang, 1 for other errors).

Review added atomic scan transactions (including manifest membership), new-header
include-shadowing invalidation, and verification against the source buffers
actually consumed by libclang rather than later disk contents.

- **Resource scope**: consumed resource headers are indexed. Standalone `.rc` /
  `.rc2` extraction is exposed by `legacygraph.mfc.scan_resources`; automatic
  project-wide resource compilation/configuration evaluation is not implemented.
- **No MSBuild Execution**: LegacyGraph does not invoke `msbuild.exe` or compilers automatically; build configurations are imported from explicit manifests or `compile_commands.json`.
- **Static analysis boundaries**: virtual edges name static targets; function
  pointer targets are unresolved. Full points-to analysis, state read/write
  dataflow, cross-project linking and an MCP transport remain future extensions.
- **Scale validation**: million-line enterprise repositories and real MFC/SDK
  compatibility still require the internal pilot; bundled tests do not establish
  those performance/coverage claims. Path deadlines are checked between query
  operations, so an individual SQLite operation may exceed the requested deadline.

## CLI Usage

Run via `python -m legacygraph`:

```powershell
# 1. Initialize a versioned SQLite index
python -m legacygraph init --repo path\to\repo --db .legacygraph\index.db

# 2. Discover source files and sync content-hash versions
python -m legacygraph discover --repo path\to\repo --db .legacygraph\index.db --json

# 3. Import a build manifest or compile_commands.json
python -m legacygraph scan-manifest --repo path\to\repo --db .legacygraph\index.db --manifest path\to\compile_commands.json

# 4. Run semantic C++ indexing and atomically publish a verified generation
python -m legacygraph scan --repo examples\mfc_demo --db .legacygraph\demo.db --manifest examples\mfc_demo\manifest.json --json

# 5. Inspect index status and pending/invalidated translation units
python -m legacygraph status --db .legacygraph\index.db --check-invalidated --json
```

## Running Tests

```powershell
python -m unittest discover -s tests -v
```

Semantic/integration tests explicitly skip when libclang is unavailable. Core
storage, query, export and HTTP tests require only the standard library.

## Queries and context export

```powershell
python -m legacygraph contexts --db .legacygraph\demo.db
python -m legacygraph search OnSave --db .legacygraph\demo.db
python -m legacygraph neighbors COrderDlg::OnSave --direction in --db .legacygraph\demo.db
python -m legacygraph path COrderDlg::OnSave SaveOrder --kinds calls --db .legacygraph\demo.db
python -m legacygraph export COrderDlg::OnSave --depth 2 --kinds calls --format json --output .legacygraph\context.json --db .legacygraph\demo.db
```

Use `--context <context_key>` when more than one active configuration is indexed.
Commands accept exact semantic keys or unambiguous symbol names; overloaded names
require selecting a `semantic_key` from search results. `--direction in` retrieves
callers. Export files are created exclusively and never overwrite existing files.

`--token-budget` is an estimate based on UTF-8 bytes, not an exact model tokenizer
limit. The final serialized pack is measured, optional context is pruned with
omissions, and mandatory content that cannot fit produces `budget_insufficient`
(CLI exit 3). Evidence and candidates are bounded samples, not exhaustive lists
of every callsite. Source hashes are checked before snippets are returned.

## Analyze your repository

Use [the sample manifest](examples/mfc_demo/manifest.json) as a schema example,
or import `compile_commands.json`. Point `--repo` at the source root and provide
the actual include search paths, defines, forced headers, project/configuration
and platform. Manifest file names are relative to the source root; include paths
in LegacyGraph manifests are resolved in the context's working directory.
Imported compiler commands are never executed. Unsupported flags and response
files produce diagnostics rather than silently changing the compilation setup.

`.sln`, `.vcxproj`, `.props` and `.targets` are inventoried; their full MSBuild
evaluation is not automatically emulated. Use an approved build-context export
or prepare an explicit manifest. Generated headers must already exist.

Re-run `scan` after changes for incremental updates. New headers conservatively
invalidate contexts because they may shadow prior include targets. Use
`--legacy-encoding cp932` (or another approved encoding) for non-UTF8 source;
the encoding overlay does not guarantee Clang accepts that project's dialect or ABI.

## Offline installation and deployment

Core runtime: Python standard library, including SQLite. Semantic indexing adds
the tested `libclang==18.1.1` package/native library. No installation or download
occurs when LegacyGraph runs. For an approved internal wheelhouse:

```powershell
python -m pip install --no-index --find-links C:\approved\wheelhouse "legacygraph[semantic]"
```

The wheelhouse must contain LegacyGraph and the compatible libclang wheel.
Building a wheel is a development task requiring setuptools; installing a
prebuilt wheel does not require a frontend toolchain. Windows SDK/MFC/MSVC headers
are not bundled and must be available under your existing approved toolchain.

The explorer binds only to IPv4 loopback and checks Host/Origin/session tokens.
Keep source, indexes and exported packs within approved local storage. Stop the
server with Ctrl+C. This is a local developer tool, not a shared production server.

Technical references: [query/export API](docs/QUERY_API.md),
[MFC support](docs/MFC_SUPPORT.md), [web API](docs/WEB_API.md),
[validation record](docs/VALIDATION.md).
