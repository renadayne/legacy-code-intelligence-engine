"""Tests for the LegacyGraph local web server, security controls, and REST API."""

from __future__ import annotations

import http.client
import json
import re
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

from legacygraph.index import IndexStore
from legacygraph.web import TOKEN_HEADER, create_server


class FakeExporter:
    """Fake ContextExporter verifying exact contract forwarding."""

    def __init__(self, service: FakeQueryService) -> None:
        self.service = service

    def export(
        self,
        seeds: list[str],
        context_key: str,
        format: str = "markdown",
        depth: int = 1,
        direction: str = "out",
        kinds: list[str] | None = None,
        token_budget: int = 8000,
        max_nodes: int = 100,
        max_edges: int = 300,
        target: str | None = None,
    ) -> dict[str, Any]:
        self.service.recorded_calls.append(
            (
                "export",
                {
                    "seeds": seeds,
                    "context_key": context_key,
                    "format": format,
                    "depth": depth,
                    "direction": direction,
                    "kinds": kinds,
                    "token_budget": token_budget,
                    "max_nodes": max_nodes,
                    "max_edges": max_edges,
                    "target": target,
                },
            )
        )
        content = (
            f"# Context Pack ({context_key})\nSeeds: {', '.join(seeds)}"
            if format == "markdown"
            else json.dumps({"seeds": seeds, "context_key": context_key})
        )
        return {
            "content": content,
            "context_key": context_key,
            "seeds": seeds,
            "token_estimate": 42,
            "truncated": False,
        }


class FakeQueryService:
    """Injectable fake QueryService matching the queries.py contract."""

    def __init__(self, store: IndexStore | None = None) -> None:
        self.store = store
        self.recorded_calls: list[tuple[str, dict[str, Any]]] = []
        self.exporter = FakeExporter(self)
        self.use_symbols_key_in_search = False

    def contexts(self) -> list[dict[str, Any]]:
        self.recorded_calls.append(("contexts", {}))
        return [
            {
                "context_key": "MFCApp:MFCApp:Debug:x64:1122334455667788",
                "project_name": "MFCApp",
                "target_name": "MFCApp",
                "configuration": "Debug",
                "platform": "x64",
            }
        ]

    def search(
        self,
        query: str = "",
        context_key: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        self.recorded_calls.append(
            ("search", {"query": query, "context_key": context_key, "limit": limit, "offset": offset})
        )
        rows = [
            {
                "semantic_key": "sym:CMainFrame::OnCreate",
                "name": "OnCreate",
                "qualified_name": "CMainFrame::OnCreate",
                "kind": "method",
            },
            {
                "id": "sym:CMainFrame::InitializeToolbar",
                "name": "InitializeToolbar",
                "qualified_name": "CMainFrame::InitializeToolbar",
                "kind": "method",
            },
        ]
        if query:
            rows = [r for r in rows if query.lower() in r["qualified_name"].lower()]
        key_name = "symbols" if self.use_symbols_key_in_search else "items"
        return {key_name: rows[:limit], "truncated": False}

    def symbol(self, semantic_key: str, context_key: str | None = None) -> dict[str, Any]:
        self.recorded_calls.append(("symbol", {"semantic_key": semantic_key, "context_key": context_key}))
        if semantic_key == "sym:missing":
            raise KeyError("Symbol not found in C:\\secret\\path\\db.sqlite")
        if semantic_key == "sym:crash":
            raise RuntimeError("Unexpected failure at D:\\internal\\secret.cpp")
        return {
            "symbol": {
                "semantic_key": semantic_key,
                "name": semantic_key.split("::")[-1],
                "qualified_name": semantic_key.replace("sym:", ""),
                "kind": "method",
                "linkage_scope": "external",
            },
            "context_key": context_key,
        }

    def neighbors(
        self,
        semantic_key: str,
        context_key: str,
        direction: str = "out",
        kinds: list[str] | None = None,
        statuses: list[str] | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        self.recorded_calls.append(
            (
                "neighbors",
                {
                    "semantic_key": semantic_key,
                    "context_key": context_key,
                    "direction": direction,
                    "kinds": kinds,
                    "statuses": statuses,
                    "limit": limit,
                },
            )
        )
        return {
            "context_key": context_key,
            "nodes": [
                {
                    "semantic_key": semantic_key,
                    "name": "OnCreate",
                    "qualified_name": "CMainFrame::OnCreate",
                    "kind": "method",
                },
                {
                    "semantic_key": "sym:CMainFrame::InitializeToolbar",
                    "name": "InitializeToolbar",
                    "qualified_name": "CMainFrame::InitializeToolbar",
                    "kind": "method",
                },
            ],
            "edges": [
                {
                    "id": "rel:1",
                    "source": semantic_key,
                    "target": "sym:CMainFrame::InitializeToolbar",
                    "kind": "calls",
                    "resolution_status": "resolved",
                    "dispatch_kind": "direct",
                    "evidence": [{"repo_path": "src/MainFrm.cpp", "start_line": 42, "rule_id": "cpp.call"}],
                },
                {
                    "id": "rel:2",
                    "source": semantic_key,
                    "target": None,
                    "kind": "calls",
                    "resolution_status": "unresolved",
                    "dispatch_kind": "indirect",
                    "unresolved_target_expr": "pPlugin->Invoke()",
                    "evidence": [{"repo_path": "src/MainFrm.cpp", "start_line": 55, "rule_id": "cpp.call"}],
                },
            ],
            "truncated": False,
        }

    def subgraph(
        self,
        seeds: list[str],
        context_key: str,
        direction: str = "out",
        depth: int = 1,
        kinds: list[str] | None = None,
        statuses: list[str] | None = None,
        max_nodes: int = 100,
        max_edges: int = 300,
    ) -> dict[str, Any]:
        self.recorded_calls.append(
            (
                "subgraph",
                {
                    "seeds": seeds,
                    "context_key": context_key,
                    "direction": direction,
                    "depth": depth,
                    "kinds": kinds,
                    "statuses": statuses,
                    "max_nodes": max_nodes,
                    "max_edges": max_edges,
                },
            )
        )
        return {
            "context_key": context_key,
            "nodes": [
                {
                    "semantic_key": seeds[0],
                    "name": "WM_CREATE",
                    "qualified_name": "WM_CREATE",
                    "kind": "event",
                },
                {
                    "semantic_key": "sym:CMainFrame::OnCreate",
                    "name": "OnCreate",
                    "qualified_name": "CMainFrame::OnCreate",
                    "kind": "method",
                },
            ],
            "edges": [
                {
                    "id": "rel:evt1",
                    "source": "sym:CMainFrame::OnCreate",
                    "target": seeds[0],
                    "kind": "handles_event",
                    "resolution_status": "resolved",
                    "dispatch_kind": "event-registration",
                    "evidence": [{"repo_path": "src/MainFrm.cpp", "start_line": 18, "rule_id": "mfc.on_wm_create"}],
                }
            ],
            "truncated": False,
        }

    def find_path(
        self,
        source: str,
        target: str,
        context_key: str,
        direction: str = "out",
        kinds: list[str] | None = None,
        statuses: list[str] | None = None,
        max_depth: int = 6,
        max_nodes: int = 10000,
        max_edges: int = 50000,
        timeout_seconds: int = 2,
    ) -> dict[str, Any]:
        self.recorded_calls.append(
            (
                "find_path",
                {
                    "source": source,
                    "target": target,
                    "context_key": context_key,
                    "direction": direction,
                    "kinds": kinds,
                    "statuses": statuses,
                    "max_depth": max_depth,
                    "max_nodes": max_nodes,
                    "max_edges": max_edges,
                    "timeout_seconds": timeout_seconds,
                },
            )
        )
        return {
            "nodes": [
                {"semantic_key": source, "name": source, "qualified_name": source, "kind": "method"},
                {"semantic_key": target, "name": target, "qualified_name": target, "kind": "method"},
            ],
            "edges": [
                {
                    "id": "rel:path1",
                    "source": source,
                    "target": target,
                    "kind": "calls",
                    "resolution_status": "resolved",
                    "dispatch_kind": "direct",
                    "evidence": [],
                }
            ],
            "truncated": False,
        }

    def snippet(
        self,
        semantic_key: str,
        context_key: str | None = None,
        max_lines: int = 200,
    ) -> dict[str, Any]:
        self.recorded_calls.append(
            ("snippet", {"semantic_key": semantic_key, "context_key": context_key, "max_lines": max_lines})
        )
        if semantic_key == "sym:missing":
            return {}
        return {
            "semantic_key": semantic_key,
            "repo_path": "src/MainFrm.cpp",
            "start_line": 40,
            "end_line": 46,
            "symbol_start_line": 41,
            "symbol_end_line": 45,
            "content": "int CMainFrame::OnCreate(LPCREATESTRUCT lpCreateStruct)\n{\n    InitializeToolbar();\n    return 0;\n}",
            "truncated": False,
        }


class WebServerTestCase(unittest.TestCase):
    """End-to-end HTTP tests against a live ThreadingHTTPServer on an ephemeral port."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_root = Path(self.temp_dir.name) / "repo"
        self.repo_root.mkdir(parents=True, exist_ok=True)
        self.db_path = Path(self.temp_dir.name) / "index.db"

        with IndexStore(self.db_path) as store:
            store.initialize_schema(self.repo_root, source_revision="rev1")

        self.fake_service = FakeQueryService()
        self.seen_store_conns: list[int] = []
        self._lock = threading.Lock()

        def factory(store: IndexStore | None) -> FakeQueryService:
            if store is not None:
                with self._lock:
                    self.seen_store_conns.append(id(store._conn))
            return self.fake_service

        self.server = create_server(
            self.db_path,
            host="127.0.0.1",
            port=0,
            service_factory=factory,
        )
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp_dir.cleanup()

    def _request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | str | None = None,
        include_token: bool = True,
        host_header: str | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            req_headers: dict[str, str] = {
                "Host": host_header if host_header is not None else f"127.0.0.1:{self.port}",
            }
            if include_token:
                req_headers[TOKEN_HEADER] = self.server.session_token
            if headers:
                req_headers.update(headers)
            payload = body.encode("utf-8") if isinstance(body, str) else body
            conn.request(method, path, body=payload, headers=req_headers)
            resp = conn.getresponse()
            raw = resp.read()
            resp_headers = {k.lower(): v for k, v in resp.getheaders()}
            return resp.status, resp_headers, raw
        finally:
            conn.close()

    def test_rejects_non_loopback_binding(self) -> None:
        for bad_host in ("0.0.0.0", "::", "::1", "192.168.1.50", "example.com", ""):
            with self.subTest(bad_host=bad_host):
                with self.assertRaises(ValueError):
                    create_server(self.db_path, host=bad_host, port=0)

    def test_index_and_static_whitelist_with_csp_and_token(self) -> None:
        status, headers, raw = self._request("GET", "/", include_token=False)
        self.assertEqual(status, 200)
        self.assertIn("default-src 'none'", headers.get("content-security-policy", ""))
        self.assertIn("script-src 'self'", headers.get("content-security-policy", ""))
        self.assertEqual(headers.get("x-content-type-options"), "nosniff")
        self.assertEqual(headers.get("x-frame-options"), "DENY")

        html = raw.decode("utf-8")
        self.assertNotIn("__LEGACYGRAPH_SESSION_TOKEN__", html)
        match = re.search(r'<meta\s+name="legacygraph-token"\s+content="([^"]+)"', html)
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.group(1), self.server.session_token)

        for asset_path, expected_mime in (
            ("/static/app.js", "application/javascript"),
            ("/static/style.css", "text/css"),
        ):
            st, hdrs, body = self._request("GET", asset_path, include_token=False)
            self.assertEqual(st, 200)
            self.assertIn(expected_mime, hdrs.get("content-type", ""))
            self.assertGreater(len(body), 100)

    def test_blocks_path_traversal_and_unlisted_assets(self) -> None:
        for bad_path in (
            "/static/../web.py",
            "/static/%2e%2e/web.py",
            "/..%2flegacygraph/web.py",
            "/static/secret.txt",
            "/legacygraph/web.py",
            "/api/unknown",
        ):
            with self.subTest(bad_path=bad_path):
                status, _, raw = self._request("GET", bad_path)
                self.assertEqual(status, 404)
                data = json.loads(raw.decode("utf-8"))
                self.assertFalse(data["ok"])

    def test_enforces_token_host_and_origin_headers(self) -> None:
        # Missing token
        status, _, _ = self._request("GET", "/api/status", include_token=False)
        self.assertEqual(status, 403)

        # Wrong token
        status, _, _ = self._request(
            "GET",
            "/api/status",
            headers={TOKEN_HEADER: "wrong-token-value"},
        )
        self.assertEqual(status, 403)

        # Foreign Host header (DNS rebinding protection)
        status, _, _ = self._request(
            "GET",
            "/api/status",
            host_header="evil.attacker.invalid:80",
        )
        self.assertEqual(status, 403)

        # Foreign Origin header
        status, _, _ = self._request(
            "GET",
            "/api/status",
            headers={"Origin": "http://evil.attacker.invalid"},
        )
        self.assertEqual(status, 403)

        # Null Origin header
        status, _, _ = self._request(
            "GET",
            "/api/status",
            headers={"Origin": "null"},
        )
        self.assertEqual(status, 403)

        # Cross-site Sec-Fetch-Site
        status, _, _ = self._request(
            "GET",
            "/api/status",
            headers={"Sec-Fetch-Site": "cross-site"},
        )
        self.assertEqual(status, 403)

        # Matching local Origin succeeds
        status, _, _ = self._request(
            "GET",
            "/api/status",
            headers={"Origin": f"http://127.0.0.1:{self.port}"},
        )
        self.assertEqual(status, 200)

    def test_api_endpoints_and_normalization(self) -> None:
        # /api/status strips local filesystem paths
        st, _, raw = self._request("GET", "/api/status")
        self.assertEqual(st, 200)
        stat_data = json.loads(raw.decode("utf-8"))
        self.assertTrue(stat_data["ok"])
        self.assertNotIn("db_path", stat_data)
        self.assertNotIn("repo_root", stat_data)
        self.assertEqual(stat_data["repo_name"], "repo")

        # /api/contexts
        st, _, raw = self._request("GET", "/api/contexts")
        self.assertEqual(st, 200)
        ctx_data = json.loads(raw.decode("utf-8"))
        self.assertEqual(len(ctx_data["contexts"]), 1)
        ctx_key = ctx_data["contexts"][0]["context_key"]

        # /api/search with 'items' and with 'symbols'
        for use_symbols in (False, True):
            self.fake_service.use_symbols_key_in_search = use_symbols
            st, _, raw = self._request(
                "GET",
                f"/api/search?query=OnCreate&context_key={ctx_key}&limit=10&offset=0",
            )
            self.assertEqual(st, 200)
            s_data = json.loads(raw.decode("utf-8"))
            self.assertEqual(len(s_data["items"]), 1)
            self.assertEqual(s_data["items"], s_data["symbols"])
            self.assertEqual(s_data["items"][0]["id"], "sym:CMainFrame::OnCreate")
            self.assertEqual(s_data["items"][0]["semantic_key"], "sym:CMainFrame::OnCreate")

        # /api/symbol
        st, _, raw = self._request(
            "GET",
            f"/api/symbol?semantic_key=sym:CMainFrame::OnCreate&context_key={ctx_key}",
        )
        self.assertEqual(st, 200)
        sym_data = json.loads(raw.decode("utf-8"))
        self.assertEqual(sym_data["symbol"]["name"], "OnCreate")

        # Unknown targets stay null; the API must not manufacture graph nodes.
        st, _, raw = self._request(
            "GET",
            f"/api/neighbors?semantic_key=sym:CMainFrame::OnCreate&context_key={ctx_key}&direction=out&kinds=calls&statuses=resolved,unresolved",
        )
        self.assertEqual(st, 200)
        nb_data = json.loads(raw.decode("utf-8"))
        self.assertEqual(len(nb_data["edges"]), 2)
        unresolved_nodes = [n for n in nb_data["nodes"] if n.get("is_unresolved_placeholder")]
        self.assertEqual(unresolved_nodes, [])
        unknown_edges = [edge for edge in nb_data["edges"] if edge["resolution_status"] == "unresolved"]
        self.assertEqual(unknown_edges[0]["target"], None)
        self.assertEqual(unknown_edges[0]["unresolved_target_expr"], "pPlugin->Invoke()")

        # /api/subgraph preserves event-registration dispatch_kind
        st, _, raw = self._request(
            "GET",
            f"/api/subgraph?seeds=sym:WM_CREATE&context_key={ctx_key}&direction=in&depth=2&max_nodes=50&max_edges=120",
        )
        self.assertEqual(st, 200)
        sg_data = json.loads(raw.decode("utf-8"))
        self.assertEqual(sg_data["edges"][0]["dispatch_kind"], "event-registration")

        # /api/path
        st, _, raw = self._request(
            "GET",
            f"/api/path?source=sym:A&target=sym:B&context_key={ctx_key}&direction=out&max_depth=4",
        )
        self.assertEqual(st, 200)
        path_data = json.loads(raw.decode("utf-8"))
        self.assertEqual(len(path_data["edges"]), 1)

        # /api/snippet by semantic_key
        st, _, raw = self._request(
            "GET",
            f"/api/snippet?semantic_key=sym:CMainFrame::OnCreate&context_key={ctx_key}&max_lines=100",
        )
        self.assertEqual(st, 200)
        snip_data = json.loads(raw.decode("utf-8"))
        self.assertIn("InitializeToolbar", snip_data["content"])

        # POST /api/export
        export_body = json.dumps(
            {
                "seeds": ["sym:CMainFrame::OnCreate"],
                "context_key": ctx_key,
                "format": "markdown",
                "depth": 2,
                "direction": "out",
                "kinds": ["calls", "handles_event"],
                "token_budget": 4096,
                "max_nodes": 80,
                "max_edges": 200,
                "target": "sym:CMainFrame::InitializeToolbar",
            }
        )
        st, _, raw = self._request(
            "POST",
            "/api/export",
            headers={"Content-Type": "application/json"},
            body=export_body,
        )
        self.assertEqual(st, 200)
        exp_data = json.loads(raw.decode("utf-8"))
        self.assertTrue(exp_data["ok"])
        self.assertIn("# Context Pack", exp_data["content"])

    def test_forbids_arbitrary_file_path_parameters_and_enforces_limits(self) -> None:
        # Arbitrary path parameter on /api/snippet must be rejected
        for forbidden_param in ("path", "file", "filepath", "repo_path"):
            st, _, raw = self._request(
                "GET",
                f"/api/snippet?semantic_key=sym:A&{forbidden_param}=C:/Windows/win.ini",
            )
            self.assertEqual(st, 400)
            err = json.loads(raw.decode("utf-8"))
            self.assertEqual(err["code"], "arbitrary_path_forbidden")

        # Out-of-bounds numeric parameters rejected
        st, _, _ = self._request("GET", "/api/search?limit=99999")
        self.assertEqual(st, 400)

        st, _, _ = self._request("GET", "/api/subgraph?seeds=sym:A&context_key=ctx&max_nodes=9999")
        self.assertEqual(st, 400)

        # Invalid relation kind rejected
        st, _, _ = self._request("GET", "/api/neighbors?semantic_key=sym:A&context_key=ctx&kinds=sql_inject")
        self.assertEqual(st, 400)

        # Oversized POST body rejected with 413
        huge_body = "x" * 70_000
        st, _, _ = self._request(
            "POST",
            "/api/export",
            headers={"Content-Type": "application/json"},
            body=huge_body,
        )
        self.assertEqual(st, 413)

        # Unknown JSON field in POST /api/export rejected
        st, _, _ = self._request(
            "POST",
            "/api/export",
            headers={"Content-Type": "application/json"},
            body=json.dumps({"seeds": ["sym:A"], "context_key": "ctx", "unexpected_field": 1}),
        )
        self.assertEqual(st, 400)

        # Unsupported HTTP method returns 405 JSON
        st, _, raw = self._request("DELETE", "/api/status")
        self.assertEqual(st, 405)
        self.assertFalse(json.loads(raw.decode("utf-8"))["ok"])

    def test_errors_do_not_leak_tracebacks_or_paths_and_connections_are_per_request(self) -> None:
        st, _, raw = self._request("GET", "/api/symbol?semantic_key=sym:missing&context_key=ctx")
        self.assertEqual(st, 404)
        text_404 = raw.decode("utf-8")
        self.assertNotIn("Traceback", text_404)
        self.assertNotIn("C:\\secret", text_404)

        st, _, raw = self._request("GET", "/api/symbol?semantic_key=sym:crash&context_key=ctx")
        self.assertEqual(st, 500)
        text_500 = raw.decode("utf-8")
        self.assertNotIn("Traceback", text_500)
        self.assertNotIn("D:\\internal", text_500)

        # Verify multiple requests created distinct SQLite connection instances
        self._request("GET", "/api/contexts")
        self._request("GET", "/api/contexts")
        self.assertGreaterEqual(len(self.seen_store_conns), 2)


if __name__ == "__main__":
    unittest.main()
