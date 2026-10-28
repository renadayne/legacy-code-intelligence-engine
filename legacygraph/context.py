"""AI context pack exporter (Markdown and JSON) with strict budget, freshness, and provenance."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from . import SCHEMA_VERSION, __version__
from .discovery import PathContainmentError
from .models import RelationKind
from .queries import (
    MAX_SNIPPET_LINES,
    MAX_SUBGRAPH_DEPTH,
    MAX_SUBGRAPH_EDGES,
    MAX_SUBGRAPH_NODES,
    QueryService,
    _validate_direction,
    _validate_int_bound,
    _validate_kinds,
)

MAX_TOKEN_BUDGET = 1_000_000
BYTES_PER_TOKEN_CONSERVATIVE = 3


def estimate_tokens_conservative(text: str) -> int:
    """Conservatively estimate LLM tokens from UTF-8 byte length (ceil(bytes / 3))."""
    if not text:
        return 0
    return (len(text.encode("utf-8")) + BYTES_PER_TOKEN_CONSERVATIVE - 1) // BYTES_PER_TOKEN_CONSERVATIVE


def _is_host_absolute_path(path_str: str) -> bool:
    if not path_str:
        return False
    if path_str.startswith(("/", "\\")) or (len(path_str) >= 2 and path_str[1] == ":"):
        return True
    return Path(path_str).is_absolute()


def _sanitize_repo_relative_list(items: Iterable[str]) -> list[str]:
    return [str(x) for x in items if not (_is_host_absolute_path(str(x)) or str(x).startswith("../") or "/../" in f"/{x}/")]


def _safe_markdown_fence(code: str, lang: str = "cpp") -> tuple[str, str]:
    ticks = "```"
    while ticks in code:
        ticks += "`"
    return f"{ticks}{lang}", ticks


def _edge_citation(edge: dict[str, Any]) -> str:
    ev_list = edge.get("evidence") or []
    if ev_list:
        ev = ev_list[0]
        p = ev.get("repo_path") or ev.get("path") or ""
        sl, el = ev.get("start_line") or ev.get("line"), ev.get("end_line")
        if p and sl:
            return f"{p}:{sl}-{el}" if el and el != sl else f"{p}:{sl}"
    return "no_span"


class ContextExporter:
    """Build bounded, provenance-verified Markdown/JSON context packs from a QueryService."""

    def __init__(self, service: QueryService) -> None:
        if not isinstance(service, QueryService):
            raise TypeError("ContextExporter requires a QueryService instance.")
        self.service = service

    def export(
        self, seeds: list[str], context_key: str, format: str = "markdown",
        depth: int = 1, direction: str = "out", kinds: Iterable[str | RelationKind] | None = None,
        token_budget: int = 8000, max_nodes: int = 100, max_edges: int = 300, target: str | None = None,
    ) -> dict[str, Any]:
        """Export a self-contained AI context pack within the serialized token/byte budget."""
        if isinstance(seeds, (str, bytes)) or not isinstance(seeds, Iterable):
            raise ValueError("Parameter 'seeds' must be a non-empty list of semantic keys.")
        seed_list: list[str] = []
        for s in seeds:
            if not isinstance(s, str) or not s.strip():
                raise ValueError("Each seed semantic_key must be a non-empty string.")
            if s not in seed_list:
                seed_list.append(s)
        if not seed_list:
            raise ValueError("Parameter 'seeds' cannot be empty.")

        if not isinstance(context_key, str) or not context_key.strip():
            raise ValueError("A non-empty 'context_key' is required.")
        if not isinstance(format, str) or format.lower() not in ("markdown", "json"):
            raise ValueError(f"Unsupported format '{format}'; expected 'markdown' or 'json'.")
        fmt = format.lower()

        depth = _validate_int_bound("depth", depth, 0, MAX_SUBGRAPH_DEPTH)
        direction = _validate_direction(direction)
        norm_kinds = _validate_kinds(kinds)
        token_budget = _validate_int_bound("token_budget", token_budget, 1, MAX_TOKEN_BUDGET)
        max_nodes = _validate_int_bound("max_nodes", max_nodes, 1, MAX_SUBGRAPH_NODES)
        max_edges = _validate_int_bound("max_edges", max_edges, 0, MAX_SUBGRAPH_EDGES)
        if target is not None and (not isinstance(target, str) or not target.strip()):
            raise ValueError("Parameter 'target' must be a non-empty string when provided.")

        max_bytes_cap = token_budget * BYTES_PER_TOKEN_CONSERVATIVE

        with self.service._read_transaction():
            ctx_records = {c["context_key"]: c for c in self.service.contexts()}
            if context_key not in ctx_records:
                self.service._get_snapshot_and_context(context_key)
            raw_ctx = ctx_records[context_key]
            generation_snap = raw_ctx["generation"]

            sanitized_context = {
                "context_key": raw_ctx["context_key"], "project_name": raw_ctx["project_name"],
                "target_name": raw_ctx["target_name"], "configuration": raw_ctx["configuration"],
                "platform": raw_ctx["platform"],
                "working_directory": "." if _is_host_absolute_path(raw_ctx["working_directory"]) else raw_ctx["working_directory"],
                "compiler_family": raw_ctx["compiler_family"], "toolset_version": raw_ctx["toolset_version"],
                "architecture": raw_ctx["architecture"], "language_standard": raw_ctx["language_standard"],
                "include_dirs": _sanitize_repo_relative_list(raw_ctx["include_dirs"]),
                "defines": list(raw_ctx["defines"]), "undefines": list(raw_ctx["undefines"]),
                "forced_includes": _sanitize_repo_relative_list(raw_ctx["forced_includes"]),
                "pch_header": "" if _is_host_absolute_path(raw_ctx["pch_header"]) else raw_ctx["pch_header"],
                "mfc_mode": raw_ctx["mfc_mode"], "provenance": raw_ctx["provenance"], "fingerprint": raw_ctx["fingerprint"],
            }

            omissions: list[dict[str, Any]] = []
            caveats: list[str] = [
                "Tokenizer absent; token count is estimated conservatively from UTF-8 bytes (ceil(bytes/3)); exact model token budget is not guaranteed.",
                "Relationships reflect static compile-context analysis and do not prove runtime reachability.",
                "Evidence and candidate lists are bounded representative samples; inspect original source for additional call sites.",
            ]
            files_map: dict[str, dict[str, Any]] = {}
            truncation_reasons: list[str] = []

            def _record_file_meta(snip_or_occ: dict[str, Any]) -> None:
                rp = snip_or_occ.get("repo_path")
                if not rp or _is_host_absolute_path(str(rp)):
                    return
                files_map[str(rp)] = {
                    "repo_path": str(rp), "status": str(snip_or_occ.get("status", "unknown")),
                    "indexed_content_sha256": snip_or_occ.get("indexed_content_sha256"),
                    "current_content_sha256": snip_or_occ.get("current_content_sha256"),
                    "encoding": snip_or_occ.get("encoding"),
                }

            def _fetch_safe_snippet(sem_key: str, *, is_mandatory_seed: bool) -> dict[str, Any] | None:
                try:
                    snip = self.service.snippet(sem_key, context_key, max_lines=MAX_SNIPPET_LINES)
                except PathContainmentError:
                    omissions.append({
                        "item": sem_key, "kind": "seed_snippet" if is_mandatory_seed else "neighbor_snippet",
                        "reason": "path_outside_repo_root", "mandatory": False, "repo_path": None,
                    })
                    caveats.append(f"Refused source retrieval for '{sem_key}' due to path containment policy.")
                    return None

                _record_file_meta(snip)
                if snip["status"] != "ok" or snip["content"] is None:
                    reason = "external_symbol_no_source" if snip.get("reason") == "no_source_occurrence" else f"{snip['status']}_source"
                    omissions.append({
                        "item": sem_key, "kind": "seed_snippet" if is_mandatory_seed else "neighbor_snippet",
                        "reason": reason, "mandatory": False, "repo_path": snip.get("repo_path"),
                        "indexed_content_sha256": snip.get("indexed_content_sha256"),
                        "current_content_sha256": snip.get("current_content_sha256"),
                    })
                    if snip["status"] in ("stale", "missing") and snip.get("repo_path"):
                        caveats.append(
                            f"Source for '{sem_key}' at '{snip['repo_path']}' is {snip['status']} "
                            f"(indexed SHA {snip.get('indexed_content_sha256')}, current SHA {snip.get('current_content_sha256')}); body omitted."
                        )
                    return None

                if snip.get("truncated"):
                    if "max_lines" not in truncation_reasons:
                        truncation_reasons.append("max_lines")
                    omissions.append({
                        "item": sem_key, "kind": "snippet_lines", "reason": "max_lines",
                        "mandatory": False, "repo_path": snip["repo_path"], "omitted_lines": snip["omitted_lines"],
                    })

                return {
                    "semantic_key": sem_key, "repo_path": snip["repo_path"],
                    "citation": f"{snip['repo_path']}:{snip['start_line']}-{snip['end_line']}",
                    "start_line": snip["start_line"], "end_line": snip["end_line"],
                    "occurrence_kind": snip.get("occurrence_kind", ""), "signature": snip.get("signature", ""),
                    "condition_state": snip.get("condition_state", "active"),
                    "indexed_content_sha256": snip["indexed_content_sha256"],
                    "current_content_sha256": snip["current_content_sha256"],
                    "content": snip["content"], "mandatory": is_mandatory_seed,
                }

            seed_details: list[dict[str, Any]] = []
            mandatory_snippets: list[dict[str, Any]] = []
            seen_snippet_spans: set[tuple[str, int, int]] = set()
            seed_unresolved: list[dict[str, Any]] = []
            parent_keys_to_fetch: list[str] = []

            for sk in seed_list:
                sym = self.service.symbol(sk, context_key)
                for occ in sym.get("occurrences", ()):
                    _record_file_meta(occ)
                primary_occ = sym.get("source")
                citation = f"{primary_occ['repo_path']}:{primary_occ['start_line']}-{primary_occ['end_line']}" if primary_occ and primary_occ.get("repo_path") else None
                seed_details.append({
                    "id": sym["id"], "semantic_key": sym["semantic_key"], "name": sym["name"],
                    "qualified_name": sym["qualified_name"], "kind": sym["kind"], "linkage_scope": sym["linkage_scope"],
                    "is_external": sym["is_external"], "parent_semantic_key": sym.get("parent_semantic_key"),
                    "citation": citation, "signature": primary_occ.get("signature", "") if primary_occ else "",
                })
                if sym.get("parent_semantic_key") and sym["parent_semantic_key"] not in seed_list and sym["parent_semantic_key"] not in parent_keys_to_fetch:
                    parent_keys_to_fetch.append(sym["parent_semantic_key"])

                snip_entry = _fetch_safe_snippet(sk, is_mandatory_seed=True)
                if snip_entry is not None:
                    seen_snippet_spans.add((snip_entry["repo_path"], snip_entry["start_line"], snip_entry["end_line"]))
                    mandatory_snippets.append(snip_entry)

                neigh = self.service.neighbors(sk, context_key, direction=direction, kinds=norm_kinds, limit=max(1, max_edges))
                if neigh.get("omitted_unknown_condition_count", 0) > 0:
                    caveats.append(f"Seed '{sk}' has {neigh['omitted_unknown_condition_count']} relation(s) with unknown preprocessor condition state.")
                for u_edge in neigh.get("unresolved", ()):
                    seed_unresolved.append({
                        "source": u_edge["source"], "target": u_edge["target"], "kind": u_edge["kind"],
                        "status": u_edge["status"], "dispatch": u_edge["dispatch"], "reason": u_edge["reason"],
                        "condition_state": u_edge["condition_state"], "unresolved_target_expr": u_edge["unresolved_target_expr"],
                        "citation": _edge_citation(u_edge),
                        "candidates": [{"semantic_key": c["semantic_key"], "qualified_name": c["qualified_name"], "reason": c["reason"]} for c in u_edge.get("candidates", ())],
                    })
                    caveats.append(f"Unresolved/ambiguous {u_edge['kind']} from '{u_edge['source']}' expr='{u_edge['unresolved_target_expr']}' reason='{u_edge['reason']}' at {_edge_citation(u_edge)}.")

            mandatory_path: dict[str, Any] | None = None
            if target is not None:
                p_res = self.service.find_path(
                    seed_list[0], target, context_key, direction=direction, kinds=norm_kinds,
                    max_depth=max(depth, 6), max_nodes=max(max_nodes, 1000), max_edges=max(max_edges, 5000),
                )
                if p_res["truncated"] and p_res["truncation_reason"] and p_res["truncation_reason"] not in truncation_reasons:
                    truncation_reasons.append(p_res["truncation_reason"])
                for c in p_res.get("caveats", ()):
                    if c not in caveats:
                        caveats.append(c)
                mandatory_path = {
                    "source": seed_list[0], "target": target, "status": p_res["status"], "found": p_res["found"],
                    "path_exists": p_res["path_exists"], "runtime_reachability_proven": False,
                    "truncated": p_res["truncated"], "truncation_reason": p_res["truncation_reason"],
                    "incomplete": p_res["incomplete"], "path": p_res["path"],
                    "edges": [{
                        "id": e["id"], "ids": e["ids"], "source": e["source"], "target": e["target"],
                        "kind": e["kind"], "status": e["status"], "dispatch": e["dispatch"],
                        "reason": e["reason"], "target_label": e.get("target_label"),
                        "citation": _edge_citation(e), "evidence": e.get("evidence", []),
                    } for e in p_res.get("edges", ())],
                }

            sub = self.service.subgraph(
                seed_list, context_key, direction=direction, depth=depth,
                kinds=norm_kinds, max_nodes=max_nodes, max_edges=max_edges,
            )
            if sub["truncated"] and sub["truncation_reason"]:
                if sub["truncation_reason"] not in truncation_reasons:
                    truncation_reasons.append(sub["truncation_reason"])
                omissions.append({"item": "subgraph", "kind": "subgraph_expansion", "reason": sub["truncation_reason"], "mandatory": False})

            seed_set = set(seed_list)
            optional_nodes = [
                {"id": n["id"], "semantic_key": n["semantic_key"], "name": n["name"], "qualified_name": n["qualified_name"], "kind": n["kind"], "is_external": n["is_external"]}
                for n in sub.get("nodes", ()) if n["semantic_key"] not in seed_set
            ]

            optional_edges: list[dict[str, Any]] = []
            for e in sub.get("edges", ()):
                if e.get("is_virtual_static_target"):
                    msg = f"Edge '{e['source']}' -> '{e['target']}' is a static virtual target; runtime dispatch may invoke an override candidate."
                    if msg not in caveats:
                        caveats.append(msg)
                if e.get("is_event_registration"):
                    msg = f"Edge '{e['source']}' -> '{e['target']}' is an MFC event registration ({e['kind']}), not a direct function call."
                    if msg not in caveats:
                        caveats.append(msg)
                optional_edges.append({
                    "id": e["id"], "ids": e["ids"], "source": e["source"], "target": e["target"],
                    "kind": e["kind"], "status": e["status"], "dispatch": e["dispatch"],
                    "condition_state": e["condition_state"], "reason": e["reason"],
                    "is_virtual_static_target": e["is_virtual_static_target"], "target_label": e["target_label"],
                    "citation": _edge_citation(e),
                    "evidence": [{"repo_path": ev["repo_path"], "start_line": ev["start_line"], "end_line": ev["end_line"], "rule_id": ev["rule_id"], "parser_name": ev["parser_name"], "parser_version": ev["parser_version"]} for ev in e.get("evidence", ())],
                    "candidates": [{"semantic_key": c["semantic_key"], "qualified_name": c["qualified_name"], "reason": c["reason"]} for c in e.get("candidates", ())],
                })

            optional_snippets: list[dict[str, Any]] = []
            candidate_snip_keys: list[str] = list(parent_keys_to_fetch)
            if mandatory_path and mandatory_path.get("path"):
                for pk in mandatory_path["path"]:
                    if pk not in seed_set and pk not in candidate_snip_keys:
                        candidate_snip_keys.append(pk)
            for on in optional_nodes:
                if on["semantic_key"] not in candidate_snip_keys:
                    candidate_snip_keys.append(on["semantic_key"])

            for nk in candidate_snip_keys[:20]:
                try:
                    snip_opt = _fetch_safe_snippet(nk, is_mandatory_seed=False)
                except Exception:
                    continue
                if snip_opt is not None:
                    span_key = (snip_opt["repo_path"], snip_opt["start_line"], snip_opt["end_line"])
                    if span_key not in seen_snippet_spans:
                        seen_snippet_spans.add(span_key)
                        optional_snippets.append(snip_opt)

            is_incomplete = bool(
                generation_snap["incomplete"] or sub["incomplete"] or len(seed_unresolved) > 0
                or (mandatory_path is not None and mandatory_path["incomplete"])
            )
            if generation_snap["incomplete"]:
                caveats.append("Build context has incomplete or unindexed translation unit runs in the published generation.")

            query_spec = {
                "seeds": seed_list, "target": target, "context_key": context_key, "direction": direction,
                "depth": depth, "kinds": list(norm_kinds) if norm_kinds is not None else None,
                "token_budget": token_budget, "max_nodes": max_nodes, "max_edges": max_edges,
            }

            kept_opt_snippets = list(optional_snippets)
            kept_opt_nodes = list(optional_nodes)
            kept_opt_edges = list(optional_edges)
            kept_unresolved = list(seed_unresolved)
            budget_omissions: list[dict[str, Any]] = []

            def _build_pack(
                *, status_val: str, budget_insufficient_val: bool,
                inc_mand_snippets: list[dict[str, Any]], inc_opt_snippets: list[dict[str, Any]],
                inc_opt_nodes: list[dict[str, Any]], inc_opt_edges: list[dict[str, Any]],
                inc_unresolved: list[dict[str, Any]], extra_omissions: list[dict[str, Any]],
            ) -> tuple[str, dict[str, Any]]:
                all_omissions = omissions + extra_omissions
                all_trunc_reasons = list(truncation_reasons)
                if extra_omissions and "token_budget" not in all_trunc_reasons and not budget_insufficient_val:
                    all_trunc_reasons.append("token_budget")
                if budget_insufficient_val and "budget_insufficient" not in all_trunc_reasons:
                    all_trunc_reasons.append("budget_insufficient")

                meta: dict[str, Any] = {
                    "schema_version": SCHEMA_VERSION, "tool_version": __version__, "format": fmt,
                    "status": status_val, "budget_insufficient": budget_insufficient_val,
                    "generation": {
                        "generation_id": generation_snap["generation_id"], "status": generation_snap["status"],
                        "parser_version": generation_snap["parser_version"], "mfc_rule_version": generation_snap["mfc_rule_version"],
                        "published_at": generation_snap["published_at"], "source_revision": generation_snap["source_revision"],
                        "incomplete": generation_snap["incomplete"], "is_complete": generation_snap["is_complete"],
                    },
                    "context": sanitized_context, "query": query_spec,
                    "token_budget": token_budget, "estimated_tokens": 0, "byte_size": 0, "max_bytes_cap": max_bytes_cap,
                    "token_counting_mode": "estimated_conservative_bytes", "tokenizer": "absent",
                    "exact_token_budget_guaranteed": False, "within_budget": True,
                    "truncated": bool(all_trunc_reasons), "truncation_reasons": all_trunc_reasons,
                    "incomplete": is_incomplete, "runtime_reachability_proven": False,
                    "files": sorted(files_map.values(), key=lambda f: f["repo_path"]),
                    "omissions": all_omissions, "caveats": caveats,
                }
                rendered = ""
                for _ in range(3):
                    if fmt == "json":
                        payload = {
                            "status": status_val, "budget_insufficient": budget_insufficient_val,
                            "metadata": meta, "seeds": seed_details, "path": mandatory_path,
                            "nodes": seed_details + inc_opt_nodes, "edges": inc_opt_edges,
                            "unresolved": inc_unresolved, "snippets": inc_mand_snippets + inc_opt_snippets,
                        }
                        rendered = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
                    else:
                        rendered = self._render_markdown(
                            meta=meta, seeds=seed_details, path_info=mandatory_path,
                            opt_nodes=inc_opt_nodes, opt_edges=inc_opt_edges,
                            unresolved=inc_unresolved, snippets=inc_mand_snippets + inc_opt_snippets,
                        )
                    b_size = len(rendered.encode("utf-8"))
                    est_tok = estimate_tokens_conservative(rendered)
                    within = est_tok <= token_budget and b_size <= max_bytes_cap
                    if meta["byte_size"] == b_size and meta["estimated_tokens"] == est_tok and meta["within_budget"] == within:
                        break
                    meta["byte_size"], meta["estimated_tokens"], meta["within_budget"] = b_size, est_tok, within
                return rendered, meta

            while True:
                content_str, meta_dict = _build_pack(
                    status_val="ok", budget_insufficient_val=False,
                    inc_mand_snippets=mandatory_snippets, inc_opt_snippets=kept_opt_snippets,
                    inc_opt_nodes=kept_opt_nodes, inc_opt_edges=kept_opt_edges,
                    inc_unresolved=kept_unresolved, extra_omissions=budget_omissions,
                )
                if meta_dict["within_budget"]:
                    return {"status": "ok", "budget_insufficient": False, "format": fmt, "content": content_str, "metadata": meta_dict}
                if kept_opt_snippets:
                    d = kept_opt_snippets.pop()
                    budget_omissions.append({"item": d["semantic_key"], "kind": "neighbor_snippet", "reason": "token_budget", "mandatory": False, "repo_path": d["repo_path"]})
                    continue
                if kept_opt_nodes:
                    d = kept_opt_nodes.pop()
                    budget_omissions.append({"item": d["semantic_key"], "kind": "subgraph_node", "reason": "token_budget", "mandatory": False})
                    continue
                if kept_opt_edges:
                    d = kept_opt_edges.pop()
                    budget_omissions.append({"item": f"{d['source']}->{d['target']}", "kind": "subgraph_edge", "reason": "token_budget", "mandatory": False})
                    continue
                if kept_unresolved:
                    d = kept_unresolved.pop()
                    budget_omissions.append({"item": f"{d['source']}:{d['unresolved_target_expr']}", "kind": "unresolved_relation", "reason": "token_budget", "mandatory": False})
                    continue
                if len(budget_omissions) > 1:
                    budget_omissions = [{
                        "item": f"{len(budget_omissions)}_optional_items",
                        "kind": "optional_expansion",
                        "reason": "token_budget",
                        "mandatory": False,
                    }]
                    continue
                break

            mandatory_req_tokens, mandatory_req_bytes = meta_dict["estimated_tokens"], meta_dict["byte_size"]
            insufficient_omissions = [
                {
                    "item": ms["semantic_key"], "kind": "mandatory_seed_source", "reason": "budget_insufficient",
                    "mandatory": True, "repo_path": ms["repo_path"], "citation": ms["citation"],
                    "required_estimated_tokens": mandatory_req_tokens, "required_bytes": mandatory_req_bytes,
                }
                for ms in mandatory_snippets
            ] or [{
                "item": ",".join(seed_list), "kind": "mandatory_pack_envelope", "reason": "budget_insufficient",
                "mandatory": True, "required_estimated_tokens": mandatory_req_tokens, "required_bytes": mandatory_req_bytes,
            }]
            caveats.append(f"Mandatory seed/path content requires ~{mandatory_req_tokens} estimated tokens ({mandatory_req_bytes} bytes), exceeding token_budget={token_budget}.")

            content_str, meta_dict = _build_pack(
                status_val="budget_insufficient", budget_insufficient_val=True,
                inc_mand_snippets=[], inc_opt_snippets=[], inc_opt_nodes=[], inc_opt_edges=[], inc_unresolved=[],
                extra_omissions=budget_omissions + insufficient_omissions,
            )

            if not meta_dict["within_budget"]:
                if fmt == "json":
                    content_str = json.dumps({
                        "status": "budget_insufficient", "budget_insufficient": True, "context_key": context_key,
                        "generation_id": generation_snap["generation_id"], "seeds": seed_list,
                        "token_budget": token_budget, "required_estimated_tokens": mandatory_req_tokens,
                    }, separators=(",", ":"), ensure_ascii=False)
                else:
                    content_str = (
                        f"# LegacyGraph Context Pack (budget_insufficient)\n- status: budget_insufficient\n"
                        f"- context: {context_key}\n- generation_id: {generation_snap['generation_id']}\n"
                        f"- seeds: {', '.join(seed_list)}\n- token_budget: {token_budget} (required: {mandatory_req_tokens})\n"
                    )
                meta_dict["byte_size"] = len(content_str.encode("utf-8"))
                meta_dict["estimated_tokens"] = estimate_tokens_conservative(content_str)
                meta_dict["within_budget"] = meta_dict["estimated_tokens"] <= token_budget and meta_dict["byte_size"] <= max_bytes_cap

            return {"status": "budget_insufficient", "budget_insufficient": True, "format": fmt, "content": content_str, "metadata": meta_dict}

    def _render_markdown(
        self, *, meta: dict[str, Any], seeds: list[dict[str, Any]], path_info: dict[str, Any] | None,
        opt_nodes: list[dict[str, Any]], opt_edges: list[dict[str, Any]],
        unresolved: list[dict[str, Any]], snippets: list[dict[str, Any]],
    ) -> str:
        gen, ctx = meta["generation"], meta["context"]
        lines: list[str] = [
            "# LegacyGraph Context Pack", "", "## Provenance & Budget",
            f"- status: `{meta['status']}`",
            f"- budget_insufficient: `{meta['budget_insufficient']}`",
            f"- generation_id: `{gen['generation_id']}` (status: `{gen['status']}`, complete: `{gen['is_complete']}`)",
            f"- parser_version: `{gen['parser_version']}` | mfc_rule_version: `{gen['mfc_rule_version']}`",
            f"- source_revision: `{gen['source_revision']}`",
            f"- context_key: `{ctx['context_key']}` (`{ctx['project_name']}` / `{ctx['target_name']}` / `{ctx['configuration']}` / `{ctx['platform']}`)",
            f"- defines: `{', '.join(ctx['defines']) if ctx['defines'] else 'none'}`",
            f"- token_budget: `{meta['token_budget']}` | estimated_tokens: `{meta['estimated_tokens']}` | byte_size: `{meta['byte_size']}` / `{meta['max_bytes_cap']}`",
            f"- token_counting_mode: `{meta['token_counting_mode']}` (exact_guaranteed: `{meta['exact_token_budget_guaranteed']}`)",
            f"- truncated: `{meta['truncated']}` ({', '.join(meta['truncation_reasons']) or 'none'}) | incomplete: `{meta['incomplete']}`",
            "", "## Seeds",
        ]
        for s in seeds:
            cit = f" at `{s['citation']}`" if s.get("citation") else ""
            lines.append(f"- `{s['semantic_key']}` (`{s['kind']}` `{s['qualified_name']}`){cit}")

        if path_info is not None:
            lines.extend([
                "", "## Dependency Path",
                f"- source: `{path_info['source']}` -> target: `{path_info['target']}`",
                f"- status: `{path_info['status']}` | found: `{path_info['found']}` | runtime_reachability_proven: `False`",
            ])
            if path_info["path"]:
                lines.append(f"- chain: `{' -> '.join(path_info['path'])}`")
            for pe in path_info.get("edges", ()):
                lbl = f" [{pe['target_label']}]" if pe.get("target_label") else ""
                lines.append(f"  - `{pe['source']}` --(`{pe['kind']}` / `{pe['dispatch']}`{lbl})--> `{pe['target']}` (`{pe['citation']}`)")

        if snippets:
            lines.extend(["", "## Source Snippets (Original)"])
            for snip in snippets:
                lines.append(f"### `{snip['semantic_key']}` (`{snip['citation']}`, sha256: `{snip['indexed_content_sha256'][:16]}...`)")
                open_fence, close_fence = _safe_markdown_fence(snip["content"], "cpp")
                lines.extend([open_fence, snip["content"], close_fence])

        if opt_edges:
            lines.extend(["", "## Relationships & Evidence"])
            for e in opt_edges:
                lbl = f" [{e['target_label']}]" if e.get("target_label") else ""
                lines.append(f"- `{e['source']}` -> `{e['target']}` | kind: `{e['kind']}` | status: `{e['status']}` | dispatch: `{e['dispatch']}`{lbl} | evidence: `{e['citation']}`")
                for cand in e.get("candidates", ()):
                    lines.append(f"  - candidate: `{cand['semantic_key']}` (`{cand['qualified_name']}`, reason: `{cand['reason']}`)")

        if unresolved:
            lines.extend(["", "## Unresolved & Ambiguous Facts"])
            for u in unresolved:
                lines.append(f"- `{u['source']}` (`{u['kind']}` / `{u['status']}`) expr: `{u['unresolved_target_expr']}` | reason: `{u['reason']}` | condition: `{u['condition_state']}` | evidence: `{u['citation']}`")
                for cand in u.get("candidates", ()):
                    lines.append(f"  - candidate: `{cand['semantic_key']}` (`{cand['qualified_name']}`, reason: `{cand['reason']}`)")

        if opt_nodes:
            lines.extend(["", "## Related Symbols"])
            for n in opt_nodes:
                lines.append(f"- `{n['semantic_key']}` (`{n['kind']}` `{n['qualified_name']}`)")

        if meta["files"]:
            lines.extend(["", "## File Provenance"])
            for f in meta["files"]:
                lines.append(f"- `{f['repo_path']}` | status: `{f['status']}` | indexed_sha256: `{f['indexed_content_sha256']}` | current_sha256: `{f['current_content_sha256']}`")

        if meta["omissions"]:
            lines.extend(["", "## Omissions"])
            for om in meta["omissions"]:
                lines.append(f"- item: `{om.get('item')}` | kind: `{om.get('kind')}` | reason: `{om.get('reason')}` | mandatory: `{om.get('mandatory')}`")

        if meta["caveats"]:
            lines.extend(["", "## Caveats"])
            for cav in meta["caveats"]:
                lines.append(f"- {cav}")

        return "\n".join(lines) + "\n"
