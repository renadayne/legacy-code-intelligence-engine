# LegacyGraph

Offline source code intelligence foundation for legacy C++/MFC repositories on Windows.

## Current Implementation Scope (Steps 1 & 2)

This package currently implements **Step 1 (Foundation)** and **Step 2 (Semantic C++ Parsing & Incremental Orchestrator)** of the design in `docs/IMPLEMENTATION_PLAN.vi.md`:

- **Zero Mandatory Third-Party Runtime Dependencies**: Core discovery, manifest import, and SQLite storage are built strictly on the Python `>=3.11` standard library (`sqlite3`, `hashlib`, `json`, `argparse`, `pathlib`, `shlex`, `dataclasses`, `enum`), with optional `libclang` (`pip install .[semantic]`) for semantic C++ AST parsing.
- **Read-Only Repository Discovery**: Scans source, header, resource (`.rc`, `.rc2`), and project metadata files without modifying the source tree or executing MSBuild/custom targets.
- **Path Containment & Output Exclusion**: Canonical path verification prevents path traversal or escaping the repository root; index and output directories (`.legacygraph/`, `legacygraph_out/`, SQLite `.db`/`.db-wal`/`.db-shm` files) are automatically excluded from discovery.
- **UTF-8 & Legacy Windows Encoding Awareness**: Detects UTF-8/UTF-16/UTF-32 BOMs, validates strict UTF-8, and falls back to legacy Windows codepages (`cp1252`, `cp932`, `latin-1`) while preserving raw byte sizes, SHA-256 content hashes, and exact 0-based line byte offsets.
- **Build Context & Compilation Database Import**: Imports either a LegacyGraph JSON build manifest or a Clang `compile_commands.json` database, preserving multiple compile commands/configurations (e.g. `Debug` vs `Release`, distinct defines/includes) for the same source file.
- **Versioned SQLite Storage & Atomic Generations**:
  - Schema for `repositories`, `files`, `file_versions`, `projects`, `build_contexts`, `translation_units`, `generations`, `parse_runs`, `generation_tu_runs`, `entities`, `symbol_occurrences`, `observations`, `relations`, `relation_evidence`, `relation_candidates`, `tu_dependencies`, and `diagnostics`.
  - Staging vs. published index generations (`begin_generation`, `record_parse_run`, `publish_generation`, `rollback_generation`) so failed or aborted indexing runs never corrupt the currently published snapshot.
  - Strict build-context isolation and content-hash invalidation across `.cpp` files, transitive header dependencies, build flags, and deleted files.

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

- **MFC Message-Map & Resource Integration**: Semantic C++ parsing exposes active macro byte offsets (`ClangParser.last_macro_offsets`) for preprocessor coordination, while dedicated MFC message-map and `.rc` resource rules are developed in a separate step.
- **No MSBuild Execution**: LegacyGraph does not invoke `msbuild.exe` or compilers automatically; build configurations are imported from explicit manifests or `compile_commands.json`.
- **No Graph Explorer UI / Context Pack Exporter Yet**: Query expansion, bounded path search, Markdown/JSON AI context packs, and local web graph visualization are scheduled for subsequent steps.

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
