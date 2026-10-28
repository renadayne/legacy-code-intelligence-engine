# LegacyGraph Query & AI Context Export API (`docs/QUERY_API.md`)

This document specifies the public API, return schemas, and behavioral guarantees of `legacygraph.queries` (`QueryService`) and `legacygraph.context` (`ContextExporter`) for integration with the CLI, UI, and MFC adapters.

---

## 1. Exceptions (`legacygraph.queries`)

- `QueryError(ValueError)`: Base class for domain-level query errors.
- `ContextNotFoundError(QueryError)`: Raised when `context_key` does not exist in `build_contexts` or has no published generation (`published_generation_id IS NULL`).
- `SymbolNotFoundError(QueryError)`: Raised when a queried `semantic_key` does not exist within the published generation for the specified `context_key`.
- `PathContainmentError(QueryError)`: Raised when a file path stored in the index resolves outside `store.repo_root` (or traverses symlinks/reparse points escaping the repository root).

Invalid parameter types or out-of-range bounds (`limit <= 0`, `offset < 0`, `depth < 0`, non-finite numbers, boolean passed as integer, invalid `direction` or `format`) raise `ValueError`.

---

## 2. `QueryService(store: IndexStore)`

All query operations execute inside a single read transaction (`IndexStore.read_snapshot()`) against the published generation for the requested `context_key`, ensuring consistency between index metadata and file verification.

### 2.1 `contexts() -> list[dict[str, Any]]`

Lists all registered build contexts ordered by `(project_path, configuration, platform, context_key)`.

Each item contains:
- `context_key` (`str`)
- `project_path` (`str`)
- `configuration` (`str`)
- `platform` (`str`)
- `toolset` (`str`)
- `compiler_family` (`str`)
- `language_standard` (`str`)
- `active_defines_Hash` (`str`)
- `include_dirs` (`list[str]`): Repository-relative paths only (host paths outside repo are redacted).
- `published_generation_id` (`int | None`)
- `generation` (`dict | None`): `{generation_id, status, created_at, published_at, source_revision, parser_version, mfc_rule_version}`
- `active_translation_units` (`int`)
- `published_tu_runs` (`int`)
- `failed_tu_runs` (`int`)
- `partial_tu_runs` (`int`)
- `is_complete` (`bool`): `True` only when `published_generation_id` is present, all active TUs are published, and there are zero failed/partial TU runs.
- `incomplete` (`bool`): `not is_complete`

---

### 2.2 `search(query: str = "", context_key: str | None = None, limit: int = 50, offset: int = 0) -> dict[str, Any]`

Searches entities across a specific published `context_key` (or across all published contexts when `context_key=None`) using exact/prefix/substring ranking and escaped `LIKE ... ESCAPE '\'` so user input containing `%`, `_`, or `\` never acts as a SQL wildcard.

- **Bounds**: `1 <= limit <= 500`, `0 <= offset <= 100_000`.
- **Return dict**:
  - `query` (`str`), `context_key` (`str | None`), `generation` (`dict | None`)
  - `limit` (`int`), `offset` (`int`), `total` (`int`), `count` (`int`)
  - `truncated` (`bool`), `has_more` (`bool`), `next_offset` (`int | None`), `incomplete` (`bool`)
  - `items` (`list[dict]`): Each item has `id`, `semantic_key`, `name`, `qualified_name`, `kind`, `parent_semantic_key`, `usr`, `is_placeholder`, `context_key`, `generation_id`, and `location` (`{repo_path, start_line, end_line, occurrence_kind, condition_status, condition_expr}` or `None`).

---

### 2.3 `symbol(semantic_key: str, context_key: str) -> dict[str, Any]`

Returns symbol metadata, all deduplicated occurrences within `context_key`, and primary source span details (preferring active definitions over declarations).

- **Return dict**:
  - `id`, `semantic_key`, `name`, `qualified_name`, `kind`, `parent_semantic_key`, `usr`, `is_placeholder`
  - `context_key`, `generation`, `incomplete` (`bool`)
  - `occurrences` (`list[dict]`): `{occurrence_id, occurrence_kind, is_Definition, access_specifier, signature, type_repr, condition_status, condition_expr, repo_path, file_id, indexed_content_sha256, start_line, start_col, end_line, end_col, start_offset, end_offset}`
  - `source` (`dict | None`): Primary occurrence location plus live file status `{status: "ok"|"stale"|"missing", is_stale: bool, indexed_content_sha256, current_content_sha256, ...}`.

---

### 2.4 `neighbors(semantic_key: str, context_key: str, direction: str = "out", kinds: Iterable[str] | None = None, statuses: Iterable[str] | None = None, limit: int = 200) -> dict[str, Any]`

Returns immediate neighbors of `semantic_key` in `context_key` (`direction` in `{"out", "in", "both"}`, `1 <= limit <= 2000`).

- **Default Graph Filtering**:
  - Only active (`condition_status = 'active'`) relations are included; `inactive` and `unknown` preprocessor branch relations are excluded and counted in `omitted_inactive_count` and `omitted_unknown_condition_count`.
  - Default `statuses=None` includes both resolved (`resolved`) and unresolved/ambiguous (`unresolved`, `ambiguous`, `approximate`) relations in `neighbors` so callers can inspect unresolved call expressions and candidate targets.
  - Only semantic/structural edges (`extraction_quality IN ('semantic', 'structural')`) or valid event registrations (`kind IN ('registers_event', 'handles_event')`) are included unless explicitly filtered.
  - Virtual static targets (`dispatch == "virtual"`) are marked with `is_virtual_static_target: True` and `target_label: "static_virtual_target"`.
  - Unresolved relations have `target: None` (never fake `"unknown"` entity nodes) and populate `unresolved_target_expr` and `candidates`.
  - All edges retain their original `source` -> `target` direction even when queried with `direction="in"`.
- **SQL & Memory Bounds**:
  - Grouping and pagination (`LIMIT limit + 1`) happen in SQL before hydrating evidence (`MAX_EVIDENCE_PER_EDGE = 25`) and candidates (`MAX_CANDIDATES_PER_EDGE = 50`).

---

### 2.5 `subgraph(seeds: list[str], context_key: str, direction: str = "out", depth: int = 1, kinds: Iterable[str] | None = None, statuses: Iterable[str] | None = None, max_nodes: int = 100, max_edges: int = 300) -> dict[str, Any]`

Performs bounded BFS expansion from `seeds` (`0 <= depth <= 10`, `1 <= max_nodes <= 5_000`, `1 <= max_edges <= 20_000`).

- Default `statuses=None` traverses only `resolved` edges (`target_semantic_key IS NOT NULL`).
- Tracks `cycles_detected` and `self_edges_count`.
- Enforces `max_nodes` and `max_edges` directly in per-hop SQL `row_limit` as well as result assembly, setting `truncated: True` and `truncation_reason` (`"max_nodes"` or `"max_edges"`).
- Returns `{seeds, context_key, generation, direction, depth, nodes, edges, truncated, truncation_reason, incomplete, omitted_inactive_count, omitted_unknown_condition_count, omitted_unresolved_count, cycles_detected, self_edges_count}`.

---

### 2.6 `find_path(source: str, target: str, context_key: str, direction: str = "out", kinds: Iterable[str] | None = None, statuses: Iterable[str] | None = None, max_depth: int = 6, max_nodes: int = 10_000, max_edges: int = 50_000, timeout_seconds: float = 2.0) -> dict[str, Any]`

Finds a shortest static path from `source` to `target` within `context_key`.

- **Zero-hop (`source == target`)**: Returns `status="found"`, `found=True`, `path=[source]`, `edges=[]` immediately after verifying `source` exists in `context_key`.
- **Honest Non-Existence & Reachability Semantics**:
  - `runtime_reachability_proven` is always `False` (static call/event paths do not prove dynamic runtime reachability).
  - When found: `status="found"`, `found=True`, `path_exists=True`.
  - When search hits `max_depth`, `max_nodes`, `max_edges`, or `timeout_seconds`: `status="truncated"`, `found=False`, `truncated=True`, `path_exists=None` (never claims no path exists).
  - When no path is found and the traversed subgraph or index is incomplete (unresolved calls, unknown preprocessor conditions, or incomplete TU runs): `status="incomplete_index"`, `found=False`, `incomplete=True`, `path_exists=None`.
  - Only when the explored component is complete and untruncated: `status="not_found_within_scope"`, `found=False`, `path_exists=False`.

---

### 2.7 `snippet(semantic_key: str, context_key: str, max_lines: int = 200) -> dict[str, Any]`

Retrieves verified source code for `semantic_key` in `context_key` (`1 <= max_lines <= 2000`).

- Verifies path containment inside `store.repo_root` (rejecting `..` traversal and symlinks escaping `repo_root` with `PathContainmentError`).
- Computes current file SHA-256 and compares against `indexed_content_sha256` from the published generation snapshot.
- If the file is missing (`status="missing"`) or modified on disk (`status="stale"`), `is_stale=True` and `content=None` (refuses stale source bodies).
- If fresh (`status="ok"`), returns exact lines `start_line..end_line` (capped at `max_lines` with `truncated=True`, `truncation_reason="max_lines"`, and `omitted_lines`).

---

## 3. `ContextExporter(service: QueryService)`

### 3.1 `export(seeds: list[str], context_key: str, format: str = "markdown", depth: int = 1, direction: str = "out", kinds: Iterable[str] | None = None, token_budget: int = 8000, max_nodes: int = 100, max_edges: int = 300, target: str | None = None) -> dict[str, Any]`

Exports a self-contained AI context pack in `"markdown"` or `"json"` format (`1 <= token_budget <= 200_000`).

- **Source Privacy**: Never leaks host filesystem paths (`repo_root`, external SDK include directories, external working directories, or external PCH paths). All citations use `repo_path:start_line-end_line`.
- **Mandatory vs. Optional Content**:
  - Mandatory: Seed entities, seed primary source snippets (when fresh), and `find_path(seed, target)` path nodes/edges/snippets when `target` is specified and found.
  - Optional: Parent class declarations of seed methods, expanded subgraph neighbor nodes/edges/snippets, and unresolved call candidates.
- **Conservative Token & Byte Budget Enforcement**:
  - Uses conservative estimator `BYTES_PER_TOKEN_CONSERVATIVE = 3` (`max_byte_size = token_budget * 3`) applied to the **final serialized output string** (including all JSON framing/escaping or Markdown headers/metadata).
  - Metadata always states `token_estimation_mode: "conservative_heuristic_bytes_div_3"` and `exact_tokenizer_available: False`.
  - Prunes optional items first (recording `"token_budget"` in `metadata.omissions` and `metadata.truncation_reasons`).
  - If mandatory seed/path content still exceeds `max_byte_size`, sets `status="budget_insufficient"`, `budget_insufficient=True`, and records mandatory omissions rather than silently dropping seeds. JSON output remains strictly valid JSON (`json.loads(res["content"])`) even under tiny budgets.
- **Safe Markdown Code Fences**:
  - Automatically widens code fences (` ````cpp `, etc.) if source snippets contain triple backticks so comments/strings inside C++ code cannot prematurely close the Markdown code block.
