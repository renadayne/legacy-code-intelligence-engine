# LegacyGraph Local Web Explorer & HTTP API (`legacygraph.web`)

`legacygraph.web` provides an offline, zero-dependency local HTTP server (`ThreadingHTTPServer`) and single-page SVG graph explorer for inspecting build-context-scoped C++/MFC call, inheritance, and event-registration graphs.

Review notes: graph API responses preserve null unresolved targets and never
manufacture placeholder symbols. Unknown expressions are displayed in the
evidence panel. Context changes clear prior graph/source state and invalidate
outstanding responses. Source freshness, incomplete paths, traversal truncation,
and insufficient export budgets are displayed explicitly. Pass seed arrays in
JSON exports, or repeat `seeds` query parameters; commas inside a seed key are
part of its identity.

## Server Entrypoints & Test Injection

```python
from legacygraph.web import create_server, serve

# Create a non-blocking server instance (ephemeral port=0 supported for tests)
server = create_server(
    db_path=".legacygraph/index.db",
    host="127.0.0.1",
    port=8765,
    service_factory=None,  # Optional callable: (store: IndexStore | None) -> QueryService | () -> QueryService
)

# Or run blocking until interrupted
serve(db_path=".legacygraph/index.db", host="127.0.0.1", port=8765)
```

### Lazy Backend Integration & `service_factory` Injection

- When `service_factory=None`, each request opens a fresh, thread-isolated SQLite `IndexStore(db_path)` in read-only mode (`PRAGMA query_only = ON; BEGIN DEFERRED;`) and lazily imports `QueryService(store)` from `legacygraph.queries` (and `ContextExporter(service)` from `legacygraph.export` / `legacygraph.queries`).
- For unit testing or standalone UI verification before `legacygraph.queries` is merged, pass `service_factory=lambda store: FakeQueryService(store)` (or a zero-argument factory `lambda: FakeQueryService()`).
- For `POST /api/export`, the handler resolves the exporter in order:
  1. `service.exporter.export(...)` if present
  2. `ContextExporter(service).export(...)` via lazy import from `.export`, `.queries`, or `.context`
  3. `service.export(...)` if callable directly on the service instance

---

## Security Model

1. **Loopback-Only Binding**: `create_server` validates `host` and binds exclusively to IPv4 loopback (`127.0.0.0/8` or `localhost` resolved to `127.0.0.1`). Non-loopback addresses (`0.0.0.0`, `::`, LAN/WAN IPs) raise `ValueError`.
2. **Host & Origin Validation**:
   - Every request must include a `Host` header matching `127.0.0.1:<actual_port>` or `localhost:<actual_port>`.
   - Any request with a foreign or `null` `Origin` header, or a cross-site `Sec-Fetch-Site` header, is rejected with `403 Forbidden`.
3. **Per-Server Session Token**:
   - Each `LegacyGraphHTTPServer` instance generates `server.session_token = secrets.token_urlsafe(32)`.
   - Serving `/` or `/index.html` injects the token into `<meta name="legacygraph-token" content="...">`.
   - Every `/api/*` endpoint requires header `X-LegacyGraph-Token: <session_token>` verified with `secrets.compare_digest`.
4. **Strict Static Asset Whitelist & CSP**:
   - Only `/`, `/index.html`, `/static/app.js` (`/app.js`), and `/static/style.css` (`/style.css`) are served.
   - Any other non-API path or traversal sequence (`..`, `%2e%2e`, backslashes) returns `404 Not Found` without filesystem lookup.
   - All responses set `Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`, `X-Content-Type-Options: nosniff`, and `X-Frame-Options: DENY`.
5. **Symbol-Key-Only Source Access**:
   - `/api/snippet` retrieves source strictly by `semantic_key` (or `id`) and `context_key`. Any query or JSON parameter named `path`, `file`, `filepath`, `repo_path`, `filename`, or `full_path` is rejected with `400 Bad Request` (`arbitrary_path_forbidden`).
6. **Bounded Inputs & Safe JSON Errors**:
   - `POST /api/export` requires `Content-Type: application/json` and `0 <= Content-Length <= 65536`.
   - Numeric bounds (`limit`, `offset`, `depth`, `max_depth`, `max_nodes`, `max_edges`, `max_lines`, `token_budget`), list lengths, and enum values (`direction`, `kinds`, `statuses`, `format`) are validated.
   - Errors never expose stack traces or host filesystem paths (`db_path` and `repo_root` are stripped from `/api/status`).

---

## REST API Reference

All `/api/*` endpoints require `X-LegacyGraph-Token` and return UTF-8 JSON with `"ok": true` on success or `{"ok": false, "error": "<message>", "code": "<error_code>"}` on failure.

### `GET /api/status`
Returns index health, active file counts, build context counts, and published generation metadata (with host paths redacted).

### `GET /api/contexts`
Calls `service.contexts()` and returns:
```json
{
  "ok": true,
  "contexts": [
    {
      "context_key": "App:App:Debug:x64:0123456789abcdef",
      "project_name": "App",
      "target_name": "App",
      "configuration": "Debug",
      "platform": "x64"
    }
  ]
}
```

### `GET /api/search`
Query parameters:
- `query` (or `q`, optional, default `""`)
- `context_key` (optional)
- `limit` (int, `1..200`, default `50`)
- `offset` (int, `0..10000`, default `0`)

Normalizes backend responses returning either `items` or `symbols` so both `items` and `symbols` arrays are present and every node includes both `id` and `semantic_key`.

### `GET /api/symbol`
Query parameters:
- `semantic_key` (or `id`, required)
- `context_key` (optional)

Calls `service.symbol(semantic_key, context_key)` and returns normalized symbol details (`404` if not found).

### `GET /api/neighbors`
Query parameters:
- `semantic_key` (or `id`, required)
- `context_key` (required)
- `direction` (`out` | `in` | `both`, default `out`)
- `kinds` (optional CSV of `RelationKind` values)
- `statuses` (optional CSV of `ResolutionStatus` values: `resolved`, `ambiguous`, `unresolved`)
- `limit` (int, `1..500`, default `200`)

Calls `service.neighbors(...)` and returns normalized `{ok, nodes, edges, truncated}`.

### `GET /api/subgraph`
Query parameters:
- `seeds` (required CSV of symbol `semantic_key` / `id` values, max 50)
- `context_key` (required)
- `direction` (`out` | `in` | `both`, default `out`)
- `depth` (int, `1..6`, default `1`)
- `kinds` (optional CSV)
- `statuses` (optional CSV)
- `max_nodes` (int, `1..500`, default `100`)
- `max_edges` (int, `1..1500`, default `300`)

Calls `service.subgraph(...)` and returns normalized `{ok, nodes, edges, truncated}`.

### `GET /api/path`
Query parameters:
- `source` (required)
- `target` (required)
- `context_key` (required)
- `direction` (`out` | `in` | `both`, default `out`)
- `kinds` (optional CSV)
- `statuses` (optional CSV)
- `max_depth` (int, `1..12`, default `6`)
- `max_nodes` (int, `1..50000`, default `10000`)
- `max_edges` (int, `1..200000`, default `50000`)
- `timeout_seconds` (int, `1..10`, default `2`)

Calls `service.find_path(...)` and returns normalized `{ok, nodes, edges, truncated}`.

### `GET /api/snippet`
Query parameters:
- `semantic_key` (or `id`, required)
- `context_key` (optional)
- `max_lines` (int, `1..500`, default `200`)

Calls `service.snippet(semantic_key, context_key, max_lines=max_lines)` and returns `{ok, repo_path, start_line, end_line, content, truncated}`.

### `POST /api/export`
JSON request body:
```json
{
  "seeds": ["c:@S@CMainFrame@F@OnCreate#..."],
  "context_key": "App:App:Debug:x64:0123456789abcdef",
  "format": "markdown",
  "depth": 1,
  "direction": "out",
  "kinds": ["calls", "handles_event"],
  "token_budget": 8000,
  "max_nodes": 100,
  "max_edges": 300,
  "target": null
}
```
Calls `ContextExporter(service).export(...)` and returns `{ok, format, content, ...}`.
