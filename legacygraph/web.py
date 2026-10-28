"""Local read-only HTTP server and web API for the LegacyGraph explorer."""

from __future__ import annotations

import inspect
import ipaddress
import json
import secrets
import sqlite3
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .index import IndexStore
from .models import DispatchKind, EntityKind, RelationKind, ResolutionStatus

STATIC_DIR = Path(__file__).resolve().parent / "static"
TOKEN_PLACEHOLDER = "__LEGACYGRAPH_SESSION_TOKEN__"
TOKEN_HEADER = "X-LegacyGraph-Token"
MAX_BODY_BYTES = 65_536
MAX_STRING_LEN = 1_024
MAX_LIST_ITEMS = 64

VALID_DIRECTIONS = frozenset({"out", "in", "both"})
VALID_FORMATS = frozenset({"markdown", "json"})
VALID_RELATION_KINDS = frozenset(k.value for k in RelationKind)
VALID_STATUSES = frozenset(s.value for s in ResolutionStatus)
FORBIDDEN_PATH_PARAMS = frozenset({"path", "file", "filepath", "repo_path", "filename", "full_path"})

CSP_POLICY = (
    "default-src 'none'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'"
)


class ApiError(Exception):
    """Structured API error mapped to an HTTP status and safe JSON payload."""

    def __init__(self, status: HTTPStatus, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _validate_loopback_host(host: str) -> str:
    cleaned = (host or "").strip()
    if not cleaned:
        raise ValueError("Host must be a non-empty IPv4 loopback address.")
    if cleaned.lower() == "localhost":
        return "127.0.0.1"
    try:
        addr = ipaddress.ip_address(cleaned)
    except ValueError as exc:
        raise ValueError(f"Invalid loopback IP address: {cleaned}") from exc
    if not isinstance(addr, ipaddress.IPv4Address) or not addr.is_loopback:
        raise ValueError("Server may only bind to an IPv4 loopback address (127.0.0.0/8).")
    return str(addr)


def _clamp_int(
    raw: Any,
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    if raw is None or raw == "":
        return default
    if isinstance(raw, bool):
        raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_parameter", f"Parameter '{name}' must be an integer.")
    try:
        val = int(str(raw).strip())
    except (ValueError, TypeError) as exc:
        raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_parameter", f"Parameter '{name}' must be an integer.") from exc
    if val < minimum or val > maximum:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "parameter_out_of_range",
            f"Parameter '{name}' must be between {minimum} and {maximum}.",
        )
    return val


def _clean_str(raw: Any, name: str, *, required: bool = False, max_len: int = MAX_STRING_LEN) -> str | None:
    if raw is None:
        if required:
            raise ApiError(HTTPStatus.BAD_REQUEST, "missing_parameter", f"Parameter '{name}' is required.")
        return None
    if not isinstance(raw, str):
        raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_parameter", f"Parameter '{name}' must be a string.")
    val = raw.strip()
    if len(val) > max_len:
        raise ApiError(HTTPStatus.BAD_REQUEST, "parameter_too_long", f"Parameter '{name}' exceeds maximum length.")
    if "\x00" in val:
        raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_parameter", f"Parameter '{name}' contains invalid characters.")
    if not val:
        if required:
            raise ApiError(HTTPStatus.BAD_REQUEST, "missing_parameter", f"Parameter '{name}' is required.")
        return None
    return val


def _parse_csv_or_list(
    raw: Any,
    name: str,
    *,
    allowed: frozenset[str] | None = None,
    max_items: int = MAX_LIST_ITEMS,
) -> list[str] | None:
    if raw is None or raw == "":
        return None
    items: list[str] = []
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        items.extend(parts)
    elif isinstance(raw, list):
        for entry in raw:
            if not isinstance(entry, str):
                raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_parameter", f"List '{name}' must contain strings.")
            for p in ([entry] if name == "seeds" else entry.split(",")):
                s = p.strip()
                if s:
                    items.append(s)
    else:
        raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_parameter", f"Parameter '{name}' must be a list or CSV string.")
    if len(items) > max_items:
        raise ApiError(HTTPStatus.BAD_REQUEST, "parameter_out_of_range", f"Parameter '{name}' exceeds {max_items} items.")
    for item in items:
        if len(item) > MAX_STRING_LEN or "\x00" in item:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_parameter", f"Invalid value in '{name}'.")
        if allowed is not None and item not in allowed:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_parameter", f"Unsupported value '{item}' in '{name}'.")
    return items or None


def _normalize_node(node: dict[str, Any]) -> dict[str, Any]:
    sem_key = str(node.get("semantic_key") or node.get("id") or "")
    node_id = str(node.get("id") or sem_key)
    name = str(node.get("name") or node.get("qualified_name") or sem_key)
    qual = str(node.get("qualified_name") or name)
    kind = str(node.get("kind") or EntityKind.EXTERNAL_SYMBOL.value)
    return {
        **node,
        "id": node_id,
        "semantic_key": sem_key or node_id,
        "name": name,
        "qualified_name": qual,
        "kind": kind,
    }


def _normalize_edge(edge: dict[str, Any], idx: int) -> dict[str, Any]:
    src = str(edge.get("source") or edge.get("source_semantic_key") or "")
    tgt_raw = edge.get("target") if "target" in edge else edge.get("target_semantic_key")
    tgt = str(tgt_raw) if tgt_raw else None
    kind = str(edge.get("kind") or RelationKind.CALLS.value)
    status = str(edge.get("resolution_status") or ResolutionStatus.RESOLVED.value)
    dispatch = str(edge.get("dispatch_kind") or (
        DispatchKind.EVENT_REGISTRATION.value
        if kind in (RelationKind.HANDLES_EVENT.value, RelationKind.REGISTERS_CALLBACK.value)
        else DispatchKind.DIRECT.value
    ))
    edge_id = str(edge.get("id") or edge.get("relation_id") or f"e:{idx}:{src}:{kind}:{tgt or 'unresolved'}")
    evidence = edge.get("evidence")
    if not isinstance(evidence, list):
        evidence = []
    return {
        **edge,
        "id": edge_id,
        "source": src,
        "target": tgt,
        "kind": kind,
        "resolution_status": status,
        "dispatch_kind": dispatch,
        "evidence": evidence,
    }


def _normalize_graph_payload(payload: dict[str, Any]) -> dict[str, Any]:
    raw_nodes = payload.get("nodes")
    if not isinstance(raw_nodes, list):
        raw_nodes = []
    raw_edges = payload.get("edges")
    if not isinstance(raw_edges, list):
        raw_edges = payload.get("relations") if isinstance(payload.get("relations"), list) else []
    nodes = [_normalize_node(n) for n in raw_nodes if isinstance(n, dict)]
    edges = [_normalize_edge(e, i) for i, e in enumerate(raw_edges) if isinstance(e, dict)]

    # Presentation must preserve the query's bounds and unknown targets.
    # Unresolved expressions belong in the evidence panel, not invented nodes.

    return {
        **payload,
        "nodes": nodes,
        "edges": edges,
        "truncated": bool(payload.get("truncated", False)),
    }


def _create_default_query_service(store: IndexStore) -> Any:
    try:
        from .queries import QueryService  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ApiError(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "query_service_unavailable",
            "QueryService module is not installed in this worktree.",
        ) from exc
    return QueryService(store)


def _invoke_exporter(service: Any, **kwargs: Any) -> dict[str, Any]:
    if hasattr(service, "exporter") and hasattr(service.exporter, "export"):
        return dict(service.exporter.export(**kwargs))
    for mod_name in (".export", ".queries", ".context"):
        try:
            module = __import__(f"{__package__}{mod_name}", fromlist=["ContextExporter"])
            exporter_cls = getattr(module, "ContextExporter", None)
            if exporter_cls is not None:
                return dict(exporter_cls(service).export(**kwargs))
        except ImportError:
            continue
    if hasattr(service, "export") and callable(service.export):
        return dict(service.export(**kwargs))
    raise ApiError(
        HTTPStatus.SERVICE_UNAVAILABLE,
        "exporter_unavailable",
        "ContextExporter is not available.",
    )


class LegacyGraphHTTPServer(ThreadingHTTPServer):
    """Threaded loopback HTTP server holding session state and database configuration."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        *,
        db_path: Path,
        bound_host: str,
        service_factory: Callable[..., Any] | None = None,
    ) -> None:
        super().__init__(server_address, handler_class)
        self.db_path = db_path
        self.bound_host = bound_host
        self.service_factory = service_factory
        self.session_token = secrets.token_urlsafe(32)


class LegacyGraphRequestHandler(BaseHTTPRequestHandler):
    """Strict local request handler serving whitelisted static assets and JSON API routes."""

    server: LegacyGraphHTTPServer

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(5.0)

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _validate_host_and_origin(self) -> None:
        port = self.server.server_port
        allowed_hosts = {
            f"127.0.0.1:{port}",
            f"localhost:{port}",
            f"{self.server.bound_host}:{port}",
        }
        if port == 80:
            allowed_hosts.update({"127.0.0.1", "localhost", self.server.bound_host})

        host_hdr = (self.headers.get("Host") or "").strip().lower()
        if not host_hdr or host_hdr not in allowed_hosts:
            raise ApiError(HTTPStatus.FORBIDDEN, "invalid_host", "Requests must target the local loopback Host header.")

        origin_hdr = self.headers.get("Origin")
        if origin_hdr is not None:
            origin_clean = origin_hdr.strip().lower()
            allowed_origins = {f"http://{h}" for h in allowed_hosts}
            if origin_clean not in allowed_origins:
                raise ApiError(HTTPStatus.FORBIDDEN, "invalid_origin", "Cross-origin requests are not permitted.")

        sec_site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if sec_site and sec_site not in ("same-origin", "none"):
            raise ApiError(HTTPStatus.FORBIDDEN, "cross_site_blocked", "Cross-site requests are not permitted.")

    def _require_session_token(self) -> None:
        provided = (self.headers.get(TOKEN_HEADER) or "").strip()
        if not provided or not secrets.compare_digest(provided, self.server.session_token):
            raise ApiError(HTTPStatus.FORBIDDEN, "invalid_token", "Missing or invalid session token.")

    def _send_bytes(self, status: HTTPStatus, content_type: str, body: bytes) -> None:
        self.send_response(int(status))
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", CSP_POLICY)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, "application/json; charset=utf-8", raw)

    def _send_error_json(self, err: ApiError) -> None:
        self._send_json(
            err.status,
            {
                "ok": False,
                "error": err.message,
                "code": err.code,
            },
        )

    def _open_request_context(self) -> tuple[IndexStore | None, Any]:
        store: IndexStore | None = None
        if self.server.db_path.exists():
            try:
                store = IndexStore(self.server.db_path)
                store._conn.execute("PRAGMA query_only = ON;")
                store._conn.execute("BEGIN DEFERRED;")
            except Exception:
                if store is not None:
                    store.close()
                store = None

        factory = self.server.service_factory
        if factory is not None:
            sig = inspect.signature(factory)
            if len(sig.parameters) == 0:
                return store, factory()
            return store, factory(store)

        if store is None:
            raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "database_unavailable", "Index database is not available.")
        return store, _create_default_query_service(store)

    def _check_no_forbidden_path_params(self, params: dict[str, Any]) -> None:
        for key in params:
            if key.lower() in FORBIDDEN_PATH_PARAMS:
                raise ApiError(
                    HTTPStatus.BAD_REQUEST,
                    "arbitrary_path_forbidden",
                    "Source retrieval by file path is forbidden; query by symbol semantic_key/id only.",
                )

    def do_GET(self) -> None:
        try:
            self._validate_host_and_origin()
            parsed = urlparse(self.path)
            route = parsed.path

            if route in ("/", "/index.html"):
                html_path = STATIC_DIR / "index.html"
                html_text = html_path.read_text(encoding="utf-8").replace(
                    TOKEN_PLACEHOLDER, self.server.session_token
                )
                self._send_bytes(HTTPStatus.OK, "text/html; charset=utf-8", html_text.encode("utf-8"))
                return

            if route in ("/static/app.js", "/app.js"):
                js_bytes = (STATIC_DIR / "app.js").read_bytes()
                self._send_bytes(HTTPStatus.OK, "application/javascript; charset=utf-8", js_bytes)
                return

            if route in ("/static/style.css", "/style.css"):
                css_bytes = (STATIC_DIR / "style.css").read_bytes()
                self._send_bytes(HTTPStatus.OK, "text/css; charset=utf-8", css_bytes)
                return

            if not route.startswith("/api/"):
                raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "Resource not found.")

            self._require_session_token()
            qs = parse_qs(parsed.query, keep_blank_values=True)
            flat_qs = {k: (v[0] if len(v) == 1 else v) for k, v in qs.items()}
            self._check_no_forbidden_path_params(flat_qs)
            self._dispatch_api_get(route, qs)
        except ApiError as err:
            self._send_error_json(err)
        except (KeyError, LookupError):
            self._send_error_json(ApiError(HTTPStatus.NOT_FOUND, "not_found", "Requested symbol or context was not found."))
        except ValueError:
            self._send_error_json(ApiError(HTTPStatus.BAD_REQUEST, "invalid_request", "Invalid request parameters."))
        except Exception:
            self._send_error_json(ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error", "Internal server error."))

    def do_POST(self) -> None:
        try:
            self._validate_host_and_origin()
            parsed = urlparse(self.path)
            if parsed.path != "/api/export":
                if parsed.path.startswith("/api/"):
                    raise ApiError(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", "Method not allowed for this endpoint.")
                raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "Resource not found.")

            self._require_session_token()
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_content_type", "Content-Type must be application/json.")

            clen_raw = self.headers.get("Content-Length")
            if clen_raw is None:
                raise ApiError(HTTPStatus.BAD_REQUEST, "missing_content_length", "Content-Length header is required.")
            try:
                clen = int(clen_raw.strip())
            except ValueError as exc:
                raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_content_length", "Invalid Content-Length header.") from exc
            if clen < 0 or clen > MAX_BODY_BYTES:
                if MAX_BODY_BYTES < clen <= MAX_BODY_BYTES * 2:
                    # Drain a bounded, modest excess so Windows does not reset
                    # the socket before the client can receive its 413 response.
                    self.rfile.read(clen)
                raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "payload_too_large", "Request body exceeds maximum size limit.")

            raw_body = self.rfile.read(clen) if clen > 0 else b"{}"
            try:
                body = json.loads(raw_body.decode("utf-8"))
            except Exception as exc:
                raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_json", "Request body must be valid UTF-8 JSON.") from exc
            if not isinstance(body, dict):
                raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_json", "JSON payload must be an object.")

            self._check_no_forbidden_path_params(body)
            self._handle_export(body)
        except ApiError as err:
            self._send_error_json(err)
        except (KeyError, LookupError):
            self._send_error_json(ApiError(HTTPStatus.NOT_FOUND, "not_found", "Requested symbol or context was not found."))
        except ValueError:
            self._send_error_json(ApiError(HTTPStatus.BAD_REQUEST, "invalid_request", "Invalid request parameters."))
        except Exception:
            self._send_error_json(ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error", "Internal server error."))

    def do_PUT(self) -> None:
        self._send_error_json(ApiError(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", "Method not allowed."))

    def do_DELETE(self) -> None:
        self._send_error_json(ApiError(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", "Method not allowed."))

    def do_PATCH(self) -> None:
        self._send_error_json(ApiError(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", "Method not allowed."))

    def _q1(self, qs: dict[str, list[str]], key: str) -> str | None:
        vals = qs.get(key)
        return vals[0] if vals else None

    def _dispatch_api_get(self, route: str, qs: dict[str, list[str]]) -> None:
        store, service = self._open_request_context()
        try:
            if route == "/api/status":
                status_data: dict[str, Any] = {}
                if store is not None:
                    try:
                        raw_stat = store.get_status_summary()
                        status_data.update(raw_stat)
                        status_data.pop("db_path", None)
                        repo_root = status_data.pop("repo_root", "")
                        status_data["repo_name"] = Path(str(repo_root)).name if repo_root else ""
                    except Exception:
                        pass
                if hasattr(service, "status") and callable(service.status):
                    svc_stat = service.status()
                    if isinstance(svc_stat, dict):
                        status_data.update(svc_stat)
                        status_data.pop("db_path", None)
                        status_data.pop("repo_root", None)
                self._send_json(HTTPStatus.OK, {"ok": True, **status_data})
                return

            if route == "/api/contexts":
                raw_ctx = service.contexts()
                if isinstance(raw_ctx, dict):
                    ctx_list = raw_ctx.get("contexts", [])
                    self._send_json(HTTPStatus.OK, {"ok": True, **raw_ctx, "contexts": list(ctx_list)})
                elif isinstance(raw_ctx, list):
                    self._send_json(HTTPStatus.OK, {"ok": True, "contexts": raw_ctx})
                else:
                    self._send_json(HTTPStatus.OK, {"ok": True, "contexts": []})
                return

            if route == "/api/search":
                query = _clean_str(self._q1(qs, "query") or self._q1(qs, "q") or "", "query", required=False) or ""
                context_key = _clean_str(self._q1(qs, "context_key"), "context_key", required=False)
                limit = _clamp_int(self._q1(qs, "limit"), "limit", default=50, minimum=1, maximum=200)
                offset = _clamp_int(self._q1(qs, "offset"), "offset", default=0, minimum=0, maximum=10_000)
                res = service.search(query=query, context_key=context_key, limit=limit, offset=offset)
                raw_items = res.get("items") if isinstance(res, dict) and "items" in res else (
                    res.get("symbols") if isinstance(res, dict) and "symbols" in res else (res if isinstance(res, list) else [])
                )
                norm_items = [_normalize_node(item) for item in (raw_items or []) if isinstance(item, dict)]
                base = dict(res) if isinstance(res, dict) else {}
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        **base,
                        "items": norm_items,
                        "symbols": norm_items,
                        "count": len(norm_items),
                    },
                )
                return

            if route == "/api/symbol":
                sem_key = _clean_str(
                    self._q1(qs, "semantic_key") or self._q1(qs, "id"),
                    "semantic_key",
                    required=True,
                )
                context_key = _clean_str(self._q1(qs, "context_key"), "context_key", required=False)
                assert sem_key is not None
                res = service.symbol(sem_key, context_key)
                if not res:
                    raise ApiError(HTTPStatus.NOT_FOUND, "symbol_not_found", "Symbol not found.")
                norm = _normalize_node(res.get("symbol", res) if isinstance(res, dict) else {})
                self._send_json(HTTPStatus.OK, {"ok": True, **(res if isinstance(res, dict) else {}), "symbol": norm})
                return

            if route == "/api/neighbors":
                sem_key = _clean_str(
                    self._q1(qs, "semantic_key") or self._q1(qs, "id"),
                    "semantic_key",
                    required=True,
                )
                context_key = _clean_str(self._q1(qs, "context_key"), "context_key", required=True)
                direction = _clean_str(self._q1(qs, "direction") or "out", "direction", required=True) or "out"
                if direction not in VALID_DIRECTIONS:
                    raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_direction", "Direction must be 'out', 'in', or 'both'.")
                kinds = _parse_csv_or_list(qs.get("kinds"), "kinds", allowed=VALID_RELATION_KINDS)
                statuses = _parse_csv_or_list(qs.get("statuses"), "statuses", allowed=VALID_STATUSES)
                limit = _clamp_int(self._q1(qs, "limit"), "limit", default=200, minimum=1, maximum=500)
                assert sem_key is not None and context_key is not None
                res = service.neighbors(
                    sem_key,
                    context_key,
                    direction=direction,
                    kinds=kinds,
                    statuses=statuses,
                    limit=limit,
                )
                self._send_json(HTTPStatus.OK, {"ok": True, **_normalize_graph_payload(dict(res))})
                return

            if route == "/api/subgraph":
                seeds = _parse_csv_or_list(qs.get("seeds"), "seeds", max_items=50)
                if not seeds:
                    raise ApiError(HTTPStatus.BAD_REQUEST, "missing_parameter", "Parameter 'seeds' is required.")
                context_key = _clean_str(self._q1(qs, "context_key"), "context_key", required=True)
                direction = _clean_str(self._q1(qs, "direction") or "out", "direction", required=True) or "out"
                if direction not in VALID_DIRECTIONS:
                    raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_direction", "Direction must be 'out', 'in', or 'both'.")
                depth = _clamp_int(self._q1(qs, "depth"), "depth", default=1, minimum=1, maximum=6)
                kinds = _parse_csv_or_list(qs.get("kinds"), "kinds", allowed=VALID_RELATION_KINDS)
                statuses = _parse_csv_or_list(qs.get("statuses"), "statuses", allowed=VALID_STATUSES)
                max_nodes = _clamp_int(self._q1(qs, "max_nodes"), "max_nodes", default=100, minimum=1, maximum=500)
                max_edges = _clamp_int(self._q1(qs, "max_edges"), "max_edges", default=300, minimum=1, maximum=1500)
                assert context_key is not None
                res = service.subgraph(
                    seeds,
                    context_key,
                    direction=direction,
                    depth=depth,
                    kinds=kinds,
                    statuses=statuses,
                    max_nodes=max_nodes,
                    max_edges=max_edges,
                )
                self._send_json(HTTPStatus.OK, {"ok": True, **_normalize_graph_payload(dict(res))})
                return

            if route == "/api/path":
                source = _clean_str(self._q1(qs, "source"), "source", required=True)
                target = _clean_str(self._q1(qs, "target"), "target", required=True)
                context_key = _clean_str(self._q1(qs, "context_key"), "context_key", required=True)
                direction = _clean_str(self._q1(qs, "direction") or "out", "direction", required=True) or "out"
                if direction not in VALID_DIRECTIONS:
                    raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_direction", "Direction must be 'out', 'in', or 'both'.")
                kinds = _parse_csv_or_list(qs.get("kinds"), "kinds", allowed=VALID_RELATION_KINDS)
                statuses = _parse_csv_or_list(qs.get("statuses"), "statuses", allowed=VALID_STATUSES)
                max_depth = _clamp_int(self._q1(qs, "max_depth"), "max_depth", default=6, minimum=1, maximum=12)
                max_nodes = _clamp_int(self._q1(qs, "max_nodes"), "max_nodes", default=10_000, minimum=1, maximum=50_000)
                max_edges = _clamp_int(self._q1(qs, "max_edges"), "max_edges", default=50_000, minimum=1, maximum=200_000)
                timeout_seconds = _clamp_int(self._q1(qs, "timeout_seconds"), "timeout_seconds", default=2, minimum=1, maximum=10)
                assert source is not None and target is not None and context_key is not None
                res = service.find_path(
                    source,
                    target,
                    context_key,
                    direction=direction,
                    kinds=kinds,
                    statuses=statuses,
                    max_depth=max_depth,
                    max_nodes=max_nodes,
                    max_edges=max_edges,
                    timeout_seconds=timeout_seconds,
                )
                self._send_json(HTTPStatus.OK, {"ok": True, **_normalize_graph_payload(dict(res))})
                return

            if route == "/api/snippet":
                sem_key = _clean_str(
                    self._q1(qs, "semantic_key") or self._q1(qs, "id"),
                    "semantic_key",
                    required=True,
                )
                context_key = _clean_str(self._q1(qs, "context_key"), "context_key", required=False)
                max_lines = _clamp_int(self._q1(qs, "max_lines"), "max_lines", default=200, minimum=1, maximum=500)
                assert sem_key is not None
                res = service.snippet(sem_key, context_key, max_lines=max_lines)
                if not res:
                    raise ApiError(HTTPStatus.NOT_FOUND, "snippet_not_found", "Snippet not found for symbol.")
                self._send_json(HTTPStatus.OK, {"ok": True, **dict(res)})
                return

            raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "Unknown API endpoint.")
        finally:
            if store is not None:
                store.close()

    def _handle_export(self, body: dict[str, Any]) -> None:
        allowed_fields = {
            "seeds",
            "context_key",
            "format",
            "depth",
            "direction",
            "kinds",
            "token_budget",
            "max_nodes",
            "max_edges",
            "target",
        }
        unknown = set(body.keys()) - allowed_fields
        if unknown:
            raise ApiError(HTTPStatus.BAD_REQUEST, "unknown_fields", f"Unsupported export fields: {sorted(unknown)}")

        seeds = _parse_csv_or_list(body.get("seeds"), "seeds", max_items=50)
        if not seeds:
            raise ApiError(HTTPStatus.BAD_REQUEST, "missing_parameter", "Field 'seeds' is required.")
        context_key = _clean_str(body.get("context_key"), "context_key", required=True)
        fmt = (_clean_str(body.get("format") or "markdown", "format", required=True) or "markdown").lower()
        if fmt not in VALID_FORMATS:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_format", "Export format must be 'markdown' or 'json'.")
        direction = _clean_str(body.get("direction") or "out", "direction", required=True) or "out"
        if direction not in VALID_DIRECTIONS:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_direction", "Direction must be 'out', 'in', or 'both'.")
        depth = _clamp_int(body.get("depth"), "depth", default=1, minimum=1, maximum=6)
        kinds = _parse_csv_or_list(body.get("kinds"), "kinds", allowed=VALID_RELATION_KINDS)
        token_budget = _clamp_int(body.get("token_budget"), "token_budget", default=8_000, minimum=256, maximum=64_000)
        max_nodes = _clamp_int(body.get("max_nodes"), "max_nodes", default=100, minimum=1, maximum=500)
        max_edges = _clamp_int(body.get("max_edges"), "max_edges", default=300, minimum=1, maximum=1500)
        target = _clean_str(body.get("target"), "target", required=False)

        store, service = self._open_request_context()
        try:
            res = _invoke_exporter(
                service,
                seeds=seeds,
                context_key=context_key,
                format=fmt,
                depth=depth,
                direction=direction,
                kinds=kinds,
                token_budget=token_budget,
                max_nodes=max_nodes,
                max_edges=max_edges,
                target=target,
            )
            self._send_json(HTTPStatus.OK, {"ok": True, "format": fmt, **dict(res)})
        finally:
            if store is not None:
                store.close()


def create_server(
    db_path: str | Path,
    host: str = "127.0.0.1",
    port: int = 8765,
    service_factory: Callable[..., Any] | None = None,
) -> LegacyGraphHTTPServer:
    """Create a loopback-bound ThreadingHTTPServer for the LegacyGraph explorer."""
    bound_ip = _validate_loopback_host(host)
    if not isinstance(port, int) or isinstance(port, bool) or port < 0 or port > 65535:
        raise ValueError(f"Invalid TCP port: {port}")
    resolved_db = Path(db_path).resolve(strict=False)
    return LegacyGraphHTTPServer(
        (bound_ip, port),
        LegacyGraphRequestHandler,
        db_path=resolved_db,
        bound_host=bound_ip,
        service_factory=service_factory,
    )


def serve(
    db_path: str | Path,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> None:
    """Start the local LegacyGraph explorer server and block until interrupted."""
    server = create_server(db_path, host=host, port=port)
    print(f"LegacyGraph Explorer: http://{server.bound_host}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
