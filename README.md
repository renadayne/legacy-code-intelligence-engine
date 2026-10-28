# LegacyGraph

Offline source code intelligence foundation for legacy C++/MFC repositories on Windows.

## Current Implementation Scope (Step 1)

This package currently implements **Step 1** of the design in `docs/IMPLEMENTATION_PLAN.vi.md`:

- **Zero Mandatory Third-Party Runtime Dependencies**: Built strictly on the Python `>=3.11` standard library (`sqlite3`, `hashlib`, `json`, `argparse`, `pathlib`, `shlex`, `dataclasses`, `enum`).
- **Read-Only Repository Discovery**: Scans source, header, resource (`.rc`, `.rc2`), and project metadata files without modifying the source tree or executing MSBuild/custom targets.
- **Path Containment & Output Exclusion**: Canonical path verification prevents path traversal or escaping the repository root; index and output directories (`.legacygraph/`, `legacygraph_out/`, SQLite `.db`/`.db-wal`/`.db-shm` files) are automatically excluded from discovery.
- **UTF-8 & Legacy Windows Encoding Awareness**: Detects UTF-8/UTF-16/UTF-32 BOMs, validates strict UTF-8, and falls back to legacy Windows codepages (`cp1252`, `cp932`, `latin-1`) while preserving raw byte sizes, SHA-256 content hashes, and exact 0-based line byte offsets.
- **Build Context & Compilation Database Import**: Imports either a LegacyGraph JSON build manifest or a Clang `compile_commands.json` database, preserving multiple compile commands/configurations (e.g. `Debug` vs `Release`, distinct defines/includes) for the same source file.
- **Versioned SQLite Storage & Atomic Generations**:
  - Schema for `repositories`, `files`, `file_versions`, `projects`, `build_contexts`, `translation_units`, `generations`, `parse_runs`, `generation_tu_runs`, `entities`, `symbol_occurrences`, `observations`, `relations`, `relation_evidence`, `relation_candidates`, `tu_dependencies`, and `diagnostics`.
  - Staging vs. published index generations (`begin_generation`, `record_parse_run`, `publish_generation`, `rollback_generation`) so failed or aborted indexing runs never corrupt the currently published snapshot.
  - Strict build-context isolation and content-hash invalidation across `.cpp` files, transitive header dependencies, build flags, and deleted files.

## Honest Limitations (What Is Not Implemented Yet)

- **No C++/MFC Semantic Parser Yet (Upcoming Step 2)**: Step 1 does **not** parse C++ ASTs or MFC message maps yet, and intentionally does **not** fabricate resolved call edges with regexes. Every relation stored as `resolved` requires explicit parser/rule observation evidence.
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

# 4. Inspect index status and pending/invalidated translation units
python -m legacygraph status --db .legacygraph\index.db --check-invalidated --json
```

## Running Tests

```powershell
python -m unittest discover -s tests -v
```
