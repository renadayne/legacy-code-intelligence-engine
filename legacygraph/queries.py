"""Bounded graph query service and freshness-verified source snippet retrieval for LegacyGraph."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
import time
from typing import Any, Iterable, Iterator

from .discovery import PathContainmentError, detect_encoding_and_decode, ensure_contained_path
from .index import IndexStore
from .models import ConditionState, OccurrenceKind, RelationKind, ResolutionStatus

MAX_SEARCH_LIMIT = 500
MAX_SEARCH_OFFSET = 100_000
MAX_NEIGHBORS_LIMIT = 5_000
MAX_SUBGRAPH_DEPTH = 20
MAX_SUBGRAPH_NODES = 5_000
MAX_SUBGRAPH_EDGES = 20_000
MAX_PATH_DEPTH = 50
MAX_PATH_NODES = 100_000
MAX_PATH_EDGES = 500_000
MAX_PATH_TIMEOUT = 60.0
MAX_SNIPPET_LINES = 2_000
SQL_BATCH_SIZE = 400
MAX_EVIDENCE_PER_EDGE = 25
MAX_CANDIDATES_PER_EDGE = 50

_DEFAULT_GRAPH_SQL = (
    "(r.condition_state = 'active' AND r.resolution_status = 'resolved' AND r.target_entity_id IS NOT NULL "
    "AND r.extraction_quality != 'recovered' "
    "AND r.reason NOT IN ('unsupported_macro', 'parse_recovery', 'unparsed_body', 'missing_header') "
    "AND (r.extraction_quality IN ('semantic', 'manual') "
    "OR r.dispatch_kind IN ('structural', 'static-include') "
    "OR r.kind IN ('member_of', 'inherits', 'declared_in', 'defined_in', 'includes', 'overrides') "
    "OR ((r.dispatch_kind = 'event-registration' OR r.kind = 'handles_event') AND r.extraction_quality = 'syntactic')))"
)
_VISIBLE_NEIGHBOR_SQL = (
    f"(r.condition_state != 'inactive' AND ((r.resolution_status = 'resolved' AND {_DEFAULT_GRAPH_SQL}) "
    "OR r.resolution_status IN ('ambiguous', 'unresolved')))"
)

_OCCURRENCE_PRIORITY: dict[str, int] = {
    OccurrenceKind.DEFINITION.value: 0, OccurrenceKind.MACRO_DEFINITION.value: 1,
    OccurrenceKind.RESOURCE_DEFINITION.value: 2, OccurrenceKind.DECLARATION.value: 3,
    OccurrenceKind.FORWARD_DECLARATION.value: 4, OccurrenceKind.MACRO_INVOCATION.value: 5,
    OccurrenceKind.REFERENCE.value: 6,
}
_CONDITION_PRIORITY: dict[str, int] = {
    ConditionState.ACTIVE.value: 0, ConditionState.UNKNOWN.value: 1, ConditionState.INACTIVE.value: 2,
}


class QueryError(ValueError):
    """Raised when query arguments or bounds are invalid."""


class ContextNotFoundError(KeyError, ValueError):
    """Raised when a build context key is missing or unknown."""


class SymbolNotFoundError(KeyError, ValueError):
    """Raised when a symbol semantic key is not found in the requested context."""


def _validate_int_bound(name: str, value: Any, min_val: int, max_val: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"Parameter '{name}' must be an integer, got {type(value).__name__}.")
    if value < min_val or value > max_val:
        raise ValueError(f"Parameter '{name}'={value} out of allowed bounds [{min_val}, {max_val}].")
    return value


def _validate_timeout(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Parameter 'timeout_seconds' must be a positive number.")
    val = float(value)
    if math.isnan(val) or math.isinf(val) or val <= 0.0 or val > MAX_PATH_TIMEOUT:
        raise ValueError(f"Parameter 'timeout_seconds'={value} must be in (0, {MAX_PATH_TIMEOUT}].")
    return val


def _validate_direction(direction: Any) -> str:
    if not isinstance(direction, str) or direction not in ("out", "in", "both"):
        raise ValueError(f"Invalid direction '{direction}'; expected 'out', 'in', or 'both'.")
    return direction


def _validate_kinds(kinds: Iterable[str | RelationKind] | None) -> tuple[str, ...] | None:
    if kinds is None:
        return None
    if isinstance(kinds, (str, bytes)):
        raise ValueError("Parameter 'kinds' must be an iterable of relation kinds, not a string.")
    normalized: list[str] = []
    for item in kinds:
        val = item.value if isinstance(item, RelationKind) else RelationKind(str(item)).value
        if val not in normalized:
            normalized.append(val)
    if not normalized:
        raise ValueError("Parameter 'kinds' cannot be an empty collection.")
    return tuple(normalized)


def _validate_statuses(statuses: Iterable[str | ResolutionStatus] | None) -> tuple[str, ...] | None:
    if statuses is None:
        return None
    if isinstance(statuses, (str, bytes)):
        raise ValueError("Parameter 'statuses' must be an iterable of resolution statuses, not a string.")
    normalized: list[str] = []
    for item in statuses:
        val = item.value if isinstance(item, ResolutionStatus) else ResolutionStatus(str(item)).value
        if val not in normalized:
            normalized.append(val)
    if not normalized:
        raise ValueError("Parameter 'statuses' cannot be an empty collection.")
    return tuple(normalized)


def _escape_like(raw: str) -> str:
    return raw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class QueryService:
    """Read-only, context-isolated graph query and snippet service over IndexStore."""

    def __init__(self, store: IndexStore) -> None:
        if not isinstance(store, IndexStore):
            raise TypeError("QueryService requires an IndexStore instance.")
        self.store = store
        self._conn = store._conn
        self._tx_depth = 0
        self._started_tx = False
        self._pinned_gen_id: int | None = None

    @contextmanager
    def _read_transaction(self) -> Iterator[None]:
        if self._tx_depth == 0:
            self._started_tx = False
            if not self._conn.in_transaction:
                self._conn.execute("BEGIN DEFERRED")
                self._started_tx = True
            self._pinned_gen_id = self.store.get_published_generation_id()
        self._tx_depth += 1
        try:
            yield
        finally:
            self._tx_depth -= 1
            if self._tx_depth == 0:
                self._pinned_gen_id = None
                if self._started_tx and self._conn.in_transaction:
                    self._conn.rollback()
                self._started_tx = False

    def _require_context_key(self, context_key: Any) -> str:
        if not isinstance(context_key, str) or not context_key.strip():
            raise ValueError("A non-empty 'context_key' is required.")
        return context_key

    def _require_semantic_key(self, semantic_key: Any, param_name: str = "semantic_key") -> str:
        if not isinstance(semantic_key, str) or not semantic_key.strip():
            raise ValueError(f"A non-empty '{param_name}' is required.")
        return semantic_key

    def _get_snapshot_and_context(self, context_key: str | None = None) -> tuple[dict[str, Any], int | None, int | None]:
        repo_id = self.store.get_active_repository_id()
        repo_row = self._conn.execute("SELECT repo_root, source_revision FROM repositories WHERE id = ?", (repo_id,)).fetchone()
        source_revision = str(repo_row["source_revision"]) if repo_row else ""

        ctx_id: int | None = None
        if context_key is not None:
            self._require_context_key(context_key)
            ctx_row = self._conn.execute(
                "SELECT bc.id FROM build_contexts bc JOIN projects p ON p.id = bc.project_id WHERE p.repository_id = ? AND bc.context_key = ?",
                (repo_id, context_key),
            ).fetchone()
            if ctx_row is None:
                raise ContextNotFoundError(f"Unknown build context: '{context_key}'.")
            ctx_id = int(ctx_row["id"])

        gen_id = self._pinned_gen_id if self._tx_depth > 0 else self.store.get_published_generation_id()
        gen_row = (
            self._conn.execute(
                "SELECT id, status, parser_version, mfc_rule_version, created_at, published_at FROM generations WHERE id = ?",
                (gen_id,),
            ).fetchone()
            if gen_id is not None else None
        )

        ctx_clause = "AND tu.build_context_id = ?" if ctx_id is not None else ""
        ctx_params: list[Any] = [ctx_id] if ctx_id is not None else []
        active_tus = int(self._conn.execute(
            f"SELECT COUNT(*) AS cnt FROM translation_units tu JOIN build_contexts bc ON bc.id = tu.build_context_id "
            f"JOIN projects p ON p.id = bc.project_id JOIN files f ON f.id = tu.main_file_id "
            f"WHERE p.repository_id = ? AND tu.is_active = 1 AND f.is_deleted = 0 {ctx_clause}",
            [repo_id, *ctx_params],
        ).fetchone()["cnt"])

        if gen_id is not None:
            runs_row = self._conn.execute(
                f"SELECT COUNT(*) AS verified_cnt, COALESCE(SUM(CASE WHEN pr.parse_status != 'parsed_clean' OR pr.has_incomplete_dependencies != 0 THEN 1 ELSE 0 END), 0) AS incomplete_cnt "
                f"FROM generation_tu_runs gtr JOIN translation_units tu ON tu.id = gtr.translation_unit_id "
                f"JOIN files f ON f.id = tu.main_file_id JOIN parse_runs pr ON pr.id = gtr.parse_run_id "
                f"WHERE gtr.generation_id = ? AND tu.is_active = 1 AND f.is_deleted = 0 {ctx_clause}",
                [gen_id, *ctx_params],
            ).fetchone()
            verified_runs, incomplete_runs = int(runs_row["verified_cnt"]), int(runs_row["incomplete_cnt"])
        else:
            verified_runs, incomplete_runs = 0, 0

        unindexed_tus = max(0, active_tus - verified_runs)
        incomplete = bool(gen_id is None or gen_row is None or verified_runs == 0 or unindexed_tus > 0 or incomplete_runs > 0)
        snapshot = {
            "generation_id": int(gen_row["id"]) if gen_row is not None else None,
            "status": str(gen_row["status"]) if gen_row is not None else "unindexed",
            "parser_version": str(gen_row["parser_version"]) if gen_row is not None else "",
            "mfc_rule_version": str(gen_row["mfc_rule_version"]) if gen_row is not None else "",
            "created_at": str(gen_row["created_at"]) if gen_row is not None else None,
            "published_at": str(gen_row["published_at"]) if gen_row is not None else None,
            "source_revision": source_revision, "context_key": context_key,
            "active_translation_units": active_tus, "verified_tu_runs": verified_runs,
            "incomplete_tu_runs": incomplete_runs, "unindexed_active_tus": unindexed_tus,
            "incomplete": incomplete, "is_complete": not incomplete,
        }
        return snapshot, gen_id, ctx_id

    def _format_node(self, row: Any) -> dict[str, Any]:
        ent_id = int(row["id"])
        return {
            "id": ent_id, "entity_id": ent_id, "semantic_key": str(row["semantic_key"]),
            "name": str(row["name"]), "qualified_name": str(row["qualified_name"]), "kind": str(row["kind"]),
            "linkage_scope": str(row["linkage_scope"]), "scope_qualifier": str(row["scope_qualifier"]),
            "parent_semantic_key": str(row["parent_semantic_key"]) if row["parent_semantic_key"] is not None else None,
            "is_external": bool(row["is_external"]),
        }

    def _fetch_nodes_by_ids(self, entity_ids: Iterable[int]) -> dict[int, dict[str, Any]]:
        ids = sorted({int(x) for x in entity_ids})
        if not ids:
            return {}
        out: dict[int, dict[str, Any]] = {}
        for i in range(0, len(ids), SQL_BATCH_SIZE):
            batch = ids[i : i + SQL_BATCH_SIZE]
            ph = ",".join("?" for _ in batch)
            for r in self._conn.execute(
                f"SELECT id, semantic_key, kind, name, qualified_name, linkage_scope, scope_qualifier, parent_semantic_key, is_external FROM entities WHERE id IN ({ph})",
                batch,
            ).fetchall():
                out[int(r["id"])] = self._format_node(r)
        return out

    def _find_entity_in_context(self, gen_id: int | None, ctx_id: int, semantic_key: str) -> dict[str, Any] | None:
        if gen_id is None:
            return None
        repo_id = self.store.get_active_repository_id()
        row = self._conn.execute(
            "SELECT id, semantic_key, kind, name, qualified_name, linkage_scope, scope_qualifier, parent_semantic_key, is_external "
            "FROM entities WHERE repository_id = ? AND semantic_key = ?",
            (repo_id, semantic_key),
        ).fetchone()
        if row is None:
            return None
        ent_id = int(row["id"])
        present = self._conn.execute(
            "SELECT 1 FROM generation_tu_runs gtr JOIN translation_units tu ON tu.id = gtr.translation_unit_id "
            "WHERE gtr.generation_id = ? AND tu.build_context_id = ? AND tu.is_active = 1 AND ("
            "EXISTS (SELECT 1 FROM symbol_occurrences so WHERE so.parse_run_id = gtr.parse_run_id AND so.entity_id = ?) "
            "OR EXISTS (SELECT 1 FROM relations r WHERE r.parse_run_id = gtr.parse_run_id AND r.build_context_id = ? AND (r.source_entity_id = ? OR r.target_entity_id = ?)) "
            "OR EXISTS (SELECT 1 FROM relation_candidates rc JOIN relations r ON r.id = rc.relation_id WHERE r.parse_run_id = gtr.parse_run_id AND r.build_context_id = ? AND rc.candidate_entity_id = ?)"
            ") LIMIT 1",
            (gen_id, ctx_id, ent_id, ctx_id, ent_id, ent_id, ctx_id, ent_id),
        ).fetchone()
        return self._format_node(row) if present is not None else None

    def _build_frontier_where(
        self, *, gen_id: int, ctx_id: int, batch_ids: list[int], direction: str,
        kinds: tuple[str, ...] | None, statuses: tuple[str, ...] | None, extra_sql: str | None = None,
    ) -> tuple[str, list[Any]]:
        ph = ",".join("?" for _ in batch_ids)
        params: list[Any] = [gen_id, ctx_id, ctx_id]
        clauses = ["gtr.generation_id = ?", "tu.build_context_id = ?", "tu.is_active = 1", "r.build_context_id = ?"]
        if direction == "out":
            clauses.append(f"r.source_entity_id IN ({ph})")
            params.extend(batch_ids)
        elif direction == "in":
            clauses.append(f"r.target_entity_id IN ({ph})")
            params.extend(batch_ids)
        else:
            clauses.append(f"(r.source_entity_id IN ({ph}) OR r.target_entity_id IN ({ph}))")
            params.extend(batch_ids)
            params.extend(batch_ids)
        if kinds is not None:
            clauses.append(f"r.kind IN ({','.join('?' for _ in kinds)})")
            params.extend(kinds)
        if statuses is not None:
            clauses.append(f"r.resolution_status IN ({','.join('?' for _ in statuses)})")
            params.extend(statuses)
        if extra_sql:
            clauses.append(extra_sql)
        return " AND ".join(clauses), params

    def _count_frontier_anomalies(
        self, *, gen_id: int, ctx_id: int, entity_ids: list[int], direction: str,
        kinds: tuple[str, ...] | None = None, statuses: tuple[str, ...] | None = None,
    ) -> dict[str, int]:
        counts = {"inactive": 0, "unknown_cond": 0, "unresolved": 0, "non_default_resolved": 0}
        unique_ids = sorted(set(entity_ids))
        for i in range(0, len(unique_ids), SQL_BATCH_SIZE // 2):
            batch = unique_ids[i : i + SQL_BATCH_SIZE // 2]
            where_sql, params = self._build_frontier_where(
                gen_id=gen_id, ctx_id=ctx_id, batch_ids=batch, direction=direction, kinds=kinds, statuses=statuses
            )
            sql = (
                "SELECT "
                "COALESCE(SUM(CASE WHEN g.condition_state = 'inactive' THEN 1 ELSE 0 END), 0) AS inactive_cnt, "
                "COALESCE(SUM(CASE WHEN g.condition_state = 'unknown' THEN 1 ELSE 0 END), 0) AS unknown_cnt, "
                "COALESCE(SUM(CASE WHEN g.condition_state != 'inactive' AND g.resolution_status IN ('ambiguous', 'unresolved') THEN 1 ELSE 0 END), 0) AS unres_cnt, "
                "COALESCE(SUM(CASE WHEN g.condition_state = 'active' AND g.resolution_status = 'resolved' AND g.is_def = 0 THEN 1 ELSE 0 END), 0) AS non_def_cnt "
                "FROM ("
                f"SELECT r.condition_state, r.resolution_status, CASE WHEN {_DEFAULT_GRAPH_SQL} THEN 1 ELSE 0 END AS is_def "
                "FROM relations r JOIN generation_tu_runs gtr ON gtr.parse_run_id = r.parse_run_id "
                f"JOIN translation_units tu ON tu.id = gtr.translation_unit_id WHERE {where_sql} "
                "GROUP BY r.source_entity_id, r.target_entity_id, r.kind, r.resolution_status, "
                "r.dispatch_kind, r.extraction_quality, r.condition_state, r.reason, r.unresolved_target_expr"
                ") g"
            )
            row = self._conn.execute(sql, params).fetchone()
            if row is not None:
                counts["inactive"] += int(row["inactive_cnt"])
                counts["unknown_cond"] += int(row["unknown_cnt"])
                counts["unresolved"] += int(row["unres_cnt"])
                counts["non_default_resolved"] += int(row["non_def_cnt"])
        return counts

    def _query_grouped_relations(
        self, *, gen_id: int, ctx_id: int, entity_ids: list[int], direction: str,
        kinds: tuple[str, ...] | None = None, statuses: tuple[str, ...] | None = None,
        extra_sql: str | None = None, row_limit: int | None = None,
    ) -> list[Any]:
        unique_ids = sorted(set(entity_ids))
        if not unique_ids:
            return []
        collected: list[Any] = []
        for i in range(0, len(unique_ids), SQL_BATCH_SIZE // 2):
            batch = unique_ids[i : i + SQL_BATCH_SIZE // 2]
            where_sql, params = self._build_frontier_where(
                gen_id=gen_id, ctx_id=ctx_id, batch_ids=batch, direction=direction,
                kinds=kinds, statuses=statuses, extra_sql=extra_sql,
            )
            limit_sql = ""
            if row_limit is not None:
                rem = max(1, row_limit - len(collected))
                limit_sql = f" LIMIT {int(rem)}"
            sql = (
                "SELECT MIN(r.id) AS relation_id, MAX(r.id) AS max_relation_id, COUNT(r.id) AS rel_count, "
                "r.source_entity_id, r.target_entity_id, src.semantic_key AS source_semantic_key, "
                "tgt.semantic_key AS target_semantic_key, r.kind, r.resolution_status, "
                "r.dispatch_kind, r.extraction_quality, r.condition_state, r.reason, r.unresolved_target_expr, "
                f"CASE WHEN {_DEFAULT_GRAPH_SQL} THEN 1 ELSE 0 END AS is_default_edge "
                "FROM relations r JOIN generation_tu_runs gtr ON gtr.parse_run_id = r.parse_run_id "
                "JOIN translation_units tu ON tu.id = gtr.translation_unit_id "
                "JOIN entities src ON src.id = r.source_entity_id "
                "LEFT JOIN entities tgt ON tgt.id = r.target_entity_id "
                f"WHERE {where_sql} "
                "GROUP BY r.source_entity_id, r.target_entity_id, r.kind, r.resolution_status, "
                "r.dispatch_kind, r.extraction_quality, r.condition_state, r.reason, r.unresolved_target_expr "
                f"ORDER BY src.semantic_key, r.kind, COALESCE(tgt.semantic_key, ''), r.unresolved_target_expr, MIN(r.id){limit_sql}"
            )
            collected.extend(self._conn.execute(sql, params).fetchall())
            if row_limit is not None and len(collected) >= row_limit:
                break
        return collected

    def _hydrate_grouped_relations(self, grouped_rows: list[Any], context_key: str) -> list[dict[str, Any]]:
        if not grouped_rows:
            return []
        all_rel_ids: set[int] = set()
        ids_per_row: list[list[int]] = []
        for r in grouped_rows:
            min_id = int(r["relation_id"])
            max_id = int(r["max_relation_id"]) if r["max_relation_id"] is not None else min_id
            r_ids = [min_id] if min_id == max_id else [min_id, max_id]
            ids_per_row.append(r_ids)
            all_rel_ids.update(r_ids)

        rel_ids_sorted = sorted(all_rel_ids)
        evidence_by_rel: dict[int, list[dict[str, Any]]] = {}
        candidates_by_rel: dict[int, list[dict[str, Any]]] = {}

        for i in range(0, len(rel_ids_sorted), SQL_BATCH_SIZE):
            batch = rel_ids_sorted[i : i + SQL_BATCH_SIZE]
            ph = ",".join("?" for _ in batch)
            for ev in self._conn.execute(
                f"SELECT id, relation_id, observation_key, observation_type, repo_path, file_version_id, "
                f"content_sha256, byte_start, byte_end, start_line, start_col, end_line, end_col, "
                f"evidence_snippet_hash, rule_id, parser_name, parser_version, notes FROM ("
                f"SELECT re.id, re.relation_id, obs.observation_key, obs.observation_type, f.repo_path, re.file_version_id, "
                f"fv.content_sha256, re.byte_start, re.byte_end, re.start_line, re.start_col, re.end_line, re.end_col, "
                f"re.evidence_snippet_hash, re.rule_id, re.parser_name, re.parser_version, re.notes, "
                f"ROW_NUMBER() OVER (PARTITION BY re.relation_id ORDER BY f.repo_path, re.start_line, re.start_col, re.id) AS rn "
                f"FROM relation_evidence re JOIN observations obs ON obs.id = re.observation_id "
                f"JOIN file_versions fv ON fv.id = re.file_version_id JOIN files f ON f.id = fv.file_id "
                f"WHERE re.relation_id IN ({ph})"
                f") WHERE rn <= {MAX_EVIDENCE_PER_EDGE} ORDER BY repo_path, start_line, start_col, id",
                batch,
            ).fetchall():
                evidence_by_rel.setdefault(int(ev["relation_id"]), []).append({
                    "id": int(ev["id"]), "observation_key": str(ev["observation_key"]),
                    "observation_type": str(ev["observation_type"]),
                    "repo_path": str(ev["repo_path"]), "path": str(ev["repo_path"]),
                    "file_version_id": int(ev["file_version_id"]), "content_sha256": str(ev["content_sha256"]),
                    "byte_start": int(ev["byte_start"]), "byte_end": int(ev["byte_end"]),
                    "start_line": int(ev["start_line"]), "start_col": int(ev["start_col"]),
                    "end_line": int(ev["end_line"]), "end_col": int(ev["end_col"]),
                    "line": int(ev["start_line"]), "evidence_snippet_hash": str(ev["evidence_snippet_hash"]),
                    "rule_id": str(ev["rule_id"]), "parser_name": str(ev["parser_name"]),
                    "parser_version": str(ev["parser_version"]), "notes": str(ev["notes"]),
                })

            for cand in self._conn.execute(
                f"SELECT relation_id, entity_id, semantic_key, name, qualified_name, kind, "
                f"candidate_reason, evidence_observation_key FROM ("
                f"SELECT rc.relation_id, e.id AS entity_id, e.semantic_key, e.name, e.qualified_name, e.kind, "
                f"rc.candidate_reason, obs.observation_key AS evidence_observation_key, rc.id AS rc_id, "
                f"ROW_NUMBER() OVER (PARTITION BY rc.relation_id ORDER BY e.semantic_key, rc.id) AS rn "
                f"FROM relation_candidates rc JOIN entities e ON e.id = rc.candidate_entity_id "
                f"LEFT JOIN observations obs ON obs.id = rc.evidence_observation_id "
                f"WHERE rc.relation_id IN ({ph})"
                f") WHERE rn <= {MAX_CANDIDATES_PER_EDGE} ORDER BY semantic_key, rc_id",
                batch,
            ).fetchall():
                candidates_by_rel.setdefault(int(cand["relation_id"]), []).append({
                    "entity_id": int(cand["entity_id"]), "semantic_key": str(cand["semantic_key"]),
                    "candidate_semantic_key": str(cand["semantic_key"]), "name": str(cand["name"]),
                    "qualified_name": str(cand["qualified_name"]), "kind": str(cand["kind"]),
                    "reason": str(cand["candidate_reason"]), "candidate_reason": str(cand["candidate_reason"]),
                    "evidence_observation_key": str(cand["evidence_observation_key"]) if cand["evidence_observation_key"] is not None else None,
                })

        out: list[dict[str, Any]] = []
        for r, r_ids in zip(grouped_rows, ids_per_row):
            primary_id = r_ids[0]
            src_id = int(r["source_entity_id"])
            tgt_id = int(r["target_entity_id"]) if r["target_entity_id"] is not None else None
            src_key = str(r["source_semantic_key"])
            tgt_key = str(r["target_semantic_key"]) if r["target_semantic_key"] is not None and tgt_id is not None else None
            kind, status, dispatch = str(r["kind"]), str(r["resolution_status"]), str(r["dispatch_kind"])
            quality, cond, reason = str(r["extraction_quality"]), str(r["condition_state"]), str(r["reason"])
            unresolved_expr = str(r["unresolved_target_expr"] or "")
            is_virtual_static = bool(dispatch == "virtual" or reason == "static_virtual_target")
            is_event_reg = bool(dispatch == "event-registration" or kind == RelationKind.HANDLES_EVENT.value)
            target_label = "static_virtual_target" if is_virtual_static else ("event_registration" if is_event_reg else None)

            ev_list: list[dict[str, Any]] = []
            ev_seen: set[tuple[Any, ...]] = set()
            cand_list: list[dict[str, Any]] = []
            cand_seen: set[tuple[str, str]] = set()
            for rid in r_ids:
                for ev in evidence_by_rel.get(rid, ()):
                    ev_sig = (ev["repo_path"], ev["byte_start"], ev["byte_end"], ev["rule_id"], ev["parser_name"])
                    if ev_sig not in ev_seen:
                        ev_seen.add(ev_sig)
                        ev_list.append(ev)
                for cand in candidates_by_rel.get(rid, ()):
                    cand_sig = (cand["semantic_key"], cand["reason"])
                    if cand_sig not in cand_seen:
                        cand_seen.add(cand_sig)
                        cand_list.append(cand)

            out.append({
                "id": primary_id, "relation_id": primary_id, "ids": r_ids, "context_key": context_key,
                "source": src_key, "target": tgt_key,
                "source_semantic_key": src_key, "target_semantic_key": tgt_key,
                "source_id": src_id, "target_id": tgt_id,
                "kind": kind, "status": status, "resolution_status": status,
                "dispatch": dispatch, "dispatch_kind": dispatch,
                "extraction_quality": quality, "condition_state": cond, "reason": reason,
                "unresolved_target_expr": unresolved_expr,
                "is_virtual_static_target": is_virtual_static, "is_event_registration": is_event_reg,
                "target_label": target_label, "is_default_graph_edge": bool(r["is_default_edge"]),
                "indexed_relation_count": int(r["rel_count"]),
                "evidence_sampled": int(r["rel_count"]) > len(r_ids) or any(len(evidence_by_rel.get(rid, ())) >= MAX_EVIDENCE_PER_EDGE for rid in r_ids),
                "candidates_sampled": int(r["rel_count"]) > len(r_ids) or any(len(candidates_by_rel.get(rid, ())) >= MAX_CANDIDATES_PER_EDGE for rid in r_ids),
                "evidence": ev_list[:MAX_EVIDENCE_PER_EDGE], "candidates": cand_list[:MAX_CANDIDATES_PER_EDGE],
            })
        return out

    def contexts(self) -> list[dict[str, Any]]:
        """Return all registered build contexts with published generation coverage."""
        with self._read_transaction():
            repo_id = self.store.get_active_repository_id()
            rows = self._conn.execute(
                "SELECT bc.id, bc.context_key, p.project_name, p.target_name, bc.configuration, bc.platform, "
                "bc.working_directory, bc.compiler_family, bc.toolset_version, bc.architecture, "
                "bc.language_standard, bc.include_dirs_json, bc.system_include_dirs_json, bc.defines_json, "
                "bc.undefines_json, bc.forced_includes_json, bc.extra_flags_json, bc.pch_header, "
                "bc.sdk_version, bc.mfc_mode, bc.provenance, bc.fingerprint "
                "FROM build_contexts bc JOIN projects p ON p.id = bc.project_id WHERE p.repository_id = ? ORDER BY bc.context_key",
                (repo_id,),
            ).fetchall()
            result: list[dict[str, Any]] = []
            for r in rows:
                ctx_key = str(r["context_key"])
                snap, _, _ = self._get_snapshot_and_context(ctx_key)
                result.append({
                    "id": int(r["id"]), "context_key": ctx_key,
                    "project_name": str(r["project_name"]), "target_name": str(r["target_name"]),
                    "configuration": str(r["configuration"]), "platform": str(r["platform"]),
                    "working_directory": str(r["working_directory"]), "compiler_family": str(r["compiler_family"]),
                    "toolset_version": str(r["toolset_version"]), "architecture": str(r["architecture"]),
                    "language_standard": str(r["language_standard"]),
                    "include_dirs": json.loads(str(r["include_dirs_json"])),
                    "system_include_dirs": json.loads(str(r["system_include_dirs_json"])),
                    "defines": json.loads(str(r["defines_json"])),
                    "undefines": json.loads(str(r["undefines_json"])),
                    "forced_includes": json.loads(str(r["forced_includes_json"])),
                    "extra_flags": json.loads(str(r["extra_flags_json"])),
                    "pch_header": str(r["pch_header"]), "sdk_version": str(r["sdk_version"]),
                    "mfc_mode": str(r["mfc_mode"]), "provenance": str(r["provenance"]),
                    "fingerprint": str(r["fingerprint"]),
                    "published_generation_id": snap["generation_id"],
                    "active_translation_units": snap["active_translation_units"],
                    "published_tu_runs": snap["verified_tu_runs"],
                    "incomplete_tu_runs": snap["incomplete_tu_runs"],
                    "unindexed_active_tus": snap["unindexed_active_tus"],
                    "incomplete": snap["incomplete"], "is_complete": snap["is_complete"], "generation": snap,
                })
            return result

    def search(self, query: str = "", context_key: str | None = None, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """Exact/prefix escaped symbol search with bounded pagination."""
        if not isinstance(query, str):
            raise ValueError("Parameter 'query' must be a string.")
        limit = _validate_int_bound("limit", limit, 1, MAX_SEARCH_LIMIT)
        offset = _validate_int_bound("offset", offset, 0, MAX_SEARCH_OFFSET)

        with self._read_transaction():
            snapshot, gen_id, ctx_id = self._get_snapshot_and_context(context_key)
            if gen_id is None:
                return {
                    "generation": snapshot, "context_key": context_key, "query": query,
                    "limit": limit, "offset": offset, "total": 0, "truncated": False,
                    "has_more": False, "next_offset": None, "incomplete": True, "items": [], "results": [],
                }

            repo_id = self.store.get_active_repository_id()
            ctx_filter_sql = "AND tu.build_context_id = ?" if ctx_id is not None else ""
            ctx_params: list[Any] = [ctx_id] if ctx_id is not None else []
            presence_cte = f"""
                WITH active_runs AS (
                    SELECT gtr.parse_run_id, tu.build_context_id, bc.context_key
                    FROM generation_tu_runs gtr JOIN translation_units tu ON tu.id = gtr.translation_unit_id
                    JOIN build_contexts bc ON bc.id = tu.build_context_id
                    WHERE gtr.generation_id = ? AND tu.is_active = 1 {ctx_filter_sql}
                ),
                entity_contexts AS (
                    SELECT so.entity_id, ar.context_key FROM symbol_occurrences so JOIN active_runs ar ON ar.parse_run_id = so.parse_run_id
                    UNION SELECT r.source_entity_id AS entity_id, ar.context_key FROM relations r JOIN active_runs ar ON ar.parse_run_id = r.parse_run_id AND r.build_context_id = ar.build_context_id
                    UNION SELECT r.target_entity_id AS entity_id, ar.context_key FROM relations r JOIN active_runs ar ON ar.parse_run_id = r.parse_run_id AND r.build_context_id = ar.build_context_id WHERE r.target_entity_id IS NOT NULL
                    UNION SELECT rc.candidate_entity_id AS entity_id, ar.context_key FROM relation_candidates rc JOIN relations r ON r.id = rc.relation_id JOIN active_runs ar ON ar.parse_run_id = r.parse_run_id AND r.build_context_id = ar.build_context_id
                )
            """
            base_params: list[Any] = [gen_id, *ctx_params, repo_id]
            where_clauses = ["e.repository_id = ?", "e.id IN (SELECT entity_id FROM entity_contexts)"]
            q_clean = query.strip()
            if q_clean:
                q_lower = q_clean.lower()
                q_like = f"{_escape_like(q_lower)}%"
                where_clauses.append(
                    "(e.normalized_name = ? OR LOWER(e.qualified_name) = ? OR LOWER(e.semantic_key) = ? "
                    "OR e.normalized_name LIKE ? ESCAPE '\\' OR LOWER(e.qualified_name) LIKE ? ESCAPE '\\' OR LOWER(e.semantic_key) LIKE ? ESCAPE '\\')"
                )
                base_params.extend([q_lower, q_lower, q_lower, q_like, q_like, q_like])
                order_sql = (
                    "CASE WHEN e.semantic_key = ? OR e.normalized_name = ? OR LOWER(e.qualified_name) = ? THEN 0 "
                    "WHEN e.normalized_name LIKE ? ESCAPE '\\' THEN 1 ELSE 2 END ASC, e.qualified_name ASC, e.semantic_key ASC"
                )
                order_params: list[Any] = [q_clean, q_lower, q_lower, q_like]
            else:
                order_sql, order_params = "e.qualified_name ASC, e.semantic_key ASC", []

            total = int(self._conn.execute(
                f"{presence_cte} SELECT COUNT(*) AS cnt FROM entities e WHERE {' AND '.join(where_clauses)}",
                base_params,
            ).fetchone()["cnt"])

            rows = self._conn.execute(
                f"{presence_cte} SELECT e.id, e.semantic_key, e.kind, e.name, e.qualified_name, "
                f"e.linkage_scope, e.scope_qualifier, e.parent_semantic_key, e.is_external "
                f"FROM entities e WHERE {' AND '.join(where_clauses)} ORDER BY {order_sql} LIMIT ? OFFSET ?",
                [*base_params, *order_params, limit, offset],
            ).fetchall()

            ent_ids = [int(r["id"]) for r in rows]
            contexts_by_ent: dict[int, list[str]] = {}
            if ent_ids:
                ph = ",".join("?" for _ in ent_ids)
                for cr in self._conn.execute(
                    f"{presence_cte} SELECT entity_id, context_key FROM entity_contexts WHERE entity_id IN ({ph}) ORDER BY context_key",
                    [gen_id, *ctx_params, *ent_ids],
                ).fetchall():
                    contexts_by_ent.setdefault(int(cr["entity_id"]), []).append(str(cr["context_key"]))

            items = [{**self._format_node(r), "contexts": contexts_by_ent.get(int(r["id"]), [])} for r in rows]
            has_more = (offset + len(items)) < total
            return {
                "generation": snapshot, "context_key": context_key, "query": query,
                "limit": limit, "offset": offset, "total": total, "truncated": has_more,
                "has_more": has_more, "next_offset": (offset + len(items)) if has_more else None,
                "incomplete": snapshot["incomplete"], "items": items, "results": items,
            }

    def _inspect_file_freshness(
        self, *, repo_path: str, indexed_version_id: int, current_version_id: int | None, is_deleted: bool, indexed_sha256: str
    ) -> dict[str, Any]:
        repo_root = self.store.get_repo_root()
        resolved_path, norm_rel = ensure_contained_path(repo_root, repo_path, allow_nonexistent=True)
        if not resolved_path.exists() or not resolved_path.is_file():
            return {
                "repo_path": norm_rel, "resolved_path": None, "status": "missing", "is_stale": True,
                "indexed_file_version_id": indexed_version_id, "current_file_version_id": current_version_id,
                "indexed_content_sha256": indexed_sha256, "current_content_sha256": None, "raw_bytes": None,
            }
        resolved_real, norm_rel = ensure_contained_path(repo_root, repo_path, allow_nonexistent=False)
        raw_bytes = resolved_real.read_bytes()
        current_sha = hashlib.sha256(raw_bytes).hexdigest()
        is_stale = bool(
            is_deleted or current_sha != indexed_sha256
            or (current_version_id is not None and int(current_version_id) != indexed_version_id)
        )
        return {
            "repo_path": norm_rel, "resolved_path": resolved_real,
            "status": "stale" if is_stale else "ok", "is_stale": is_stale,
            "indexed_file_version_id": indexed_version_id, "current_file_version_id": current_version_id,
            "indexed_content_sha256": indexed_sha256, "current_content_sha256": current_sha,
            "raw_bytes": None if is_stale else raw_bytes,
        }

    def symbol(self, semantic_key: str, context_key: str) -> dict[str, Any]:
        """Return detailed entity metadata, context-scoped occurrences, and source details."""
        semantic_key = self._require_semantic_key(semantic_key)
        context_key = self._require_context_key(context_key)

        with self._read_transaction():
            snapshot, gen_id, ctx_id = self._get_snapshot_and_context(context_key)
            assert ctx_id is not None
            node = self._find_entity_in_context(gen_id, ctx_id, semantic_key)
            if node is None or gen_id is None:
                raise SymbolNotFoundError(f"Symbol '{semantic_key}' not found in context '{context_key}'.")

            occ_rows = self._conn.execute(
                "SELECT so.id, so.occurrence_kind, so.signature, so.condition_state, so.extraction_quality, "
                "pe.semantic_key AS parent_semantic_key, so.byte_start, so.byte_end, so.start_line, so.start_col, "
                "so.end_line, so.end_col, so.file_version_id, fv.content_sha256, fv.size_bytes, fv.encoding, "
                "fv.has_bom, fv.decode_clean, fv.line_count, f.repo_path, f.current_version_id, f.is_deleted "
                "FROM symbol_occurrences so JOIN generation_tu_runs gtr ON gtr.parse_run_id = so.parse_run_id "
                "JOIN translation_units tu ON tu.id = gtr.translation_unit_id "
                "JOIN file_versions fv ON fv.id = so.file_version_id JOIN files f ON f.id = fv.file_id "
                "LEFT JOIN entities pe ON pe.id = so.parent_entity_id "
                "WHERE gtr.generation_id = ? AND tu.build_context_id = ? AND tu.is_active = 1 AND so.entity_id = ? "
                "ORDER BY f.repo_path, so.start_line, so.start_col, so.id",
                (gen_id, ctx_id, node["id"]),
            ).fetchall()

            seen_occ: set[tuple[Any, ...]] = set()
            occurrences: list[dict[str, Any]] = []
            for r in occ_rows:
                occ_sig = (str(r["occurrence_kind"]), str(r["repo_path"]), int(r["byte_start"]), int(r["byte_end"]), str(r["condition_state"]), str(r["signature"]))
                if occ_sig in seen_occ:
                    continue
                seen_occ.add(occ_sig)
                try:
                    fresh = self._inspect_file_freshness(
                        repo_path=str(r["repo_path"]), indexed_version_id=int(r["file_version_id"]),
                        current_version_id=int(r["current_version_id"]) if r["current_version_id"] is not None else None,
                        is_deleted=bool(r["is_deleted"]), indexed_sha256=str(r["content_sha256"]),
                    )
                    file_status, curr_sha = fresh["status"], fresh["current_content_sha256"]
                except PathContainmentError:
                    file_status, curr_sha = "outside_root", None

                occurrences.append({
                    "id": int(r["id"]), "occurrence_kind": str(r["occurrence_kind"]),
                    "signature": str(r["signature"]), "condition_state": str(r["condition_state"]),
                    "extraction_quality": str(r["extraction_quality"]),
                    "parent_semantic_key": str(r["parent_semantic_key"]) if r["parent_semantic_key"] is not None else None,
                    "repo_path": str(r["repo_path"]), "path": str(r["repo_path"]),
                    "file_version_id": int(r["file_version_id"]), "indexed_file_version_id": int(r["file_version_id"]),
                    "current_file_version_id": int(r["current_version_id"]) if r["current_version_id"] is not None else None,
                    "content_sha256": str(r["content_sha256"]), "indexed_content_sha256": str(r["content_sha256"]),
                    "current_content_sha256": curr_sha, "size_bytes": int(r["size_bytes"]),
                    "encoding": str(r["encoding"]), "has_bom": bool(r["has_bom"]),
                    "decode_clean": bool(r["decode_clean"]), "line_count": int(r["line_count"]),
                    "byte_start": int(r["byte_start"]), "byte_end": int(r["byte_end"]),
                    "start_line": int(r["start_line"]), "start_col": int(r["start_col"]),
                    "end_line": int(r["end_line"]), "end_col": int(r["end_col"]),
                    "status": file_status, "is_stale": file_status != "ok",
                })

            occurrences.sort(key=lambda o: (
                _CONDITION_PRIORITY.get(o["condition_state"], 9),
                _OCCURRENCE_PRIORITY.get(o["occurrence_kind"], 99),
                o["repo_path"], o["start_line"], o["start_col"],
            ))
            has_unknown_cond = any(o["condition_state"] == ConditionState.UNKNOWN.value for o in occurrences)
            return {
                **node, "symbol": node, "node": node, "context_key": context_key, "generation": snapshot,
                "incomplete": bool(snapshot["incomplete"] or has_unknown_cond),
                "occurrences": occurrences, "source": occurrences[0] if occurrences else None,
            }

    def neighbors(
        self, semantic_key: str, context_key: str, direction: str = "out",
        kinds: Iterable[str | RelationKind] | None = None, statuses: Iterable[str | ResolutionStatus] | None = None, limit: int = 200,
    ) -> dict[str, Any]:
        """Return direct neighbors, evidence, unresolved expressions, and candidates in one context."""
        semantic_key = self._require_semantic_key(semantic_key)
        context_key = self._require_context_key(context_key)
        direction = _validate_direction(direction)
        norm_kinds = _validate_kinds(kinds)
        norm_statuses = _validate_statuses(statuses)
        limit = _validate_int_bound("limit", limit, 1, MAX_NEIGHBORS_LIMIT)

        with self._read_transaction():
            snapshot, gen_id, ctx_id = self._get_snapshot_and_context(context_key)
            assert ctx_id is not None
            center = self._find_entity_in_context(gen_id, ctx_id, semantic_key)
            if center is None or gen_id is None:
                raise SymbolNotFoundError(f"Symbol '{semantic_key}' not found in context '{context_key}'.")

            anomalies = self._count_frontier_anomalies(
                gen_id=gen_id, ctx_id=ctx_id, entity_ids=[center["id"]],
                direction=direction, kinds=norm_kinds, statuses=norm_statuses,
            )
            grouped_rows = self._query_grouped_relations(
                gen_id=gen_id, ctx_id=ctx_id, entity_ids=[center["id"]],
                direction=direction, kinds=norm_kinds, statuses=norm_statuses,
                extra_sql=_VISIBLE_NEIGHBOR_SQL, row_limit=limit + 1,
            )
            truncated = len(grouped_rows) > limit
            if truncated:
                grouped_rows = grouped_rows[:limit]
            visible_edges = self._hydrate_grouped_relations(grouped_rows, context_key)

            referenced_entity_ids: set[int] = {center["id"]}
            unresolved_edges: list[dict[str, Any]] = []
            self_edges_count = cycles_detected = 0
            for edge in visible_edges:
                if edge["source_id"] == edge["target_id"]:
                    self_edges_count += 1
                    cycles_detected += 1
                referenced_entity_ids.add(edge["source_id"])
                if edge["target_id"] is not None:
                    referenced_entity_ids.add(edge["target_id"])
                if edge["status"] in (ResolutionStatus.UNRESOLVED.value, ResolutionStatus.AMBIGUOUS.value):
                    unresolved_edges.append(edge)

            nodes_by_id = self._fetch_nodes_by_ids(referenced_entity_ids)
            nodes = [center] + [
                n for eid, n in sorted(nodes_by_id.items(), key=lambda kv: (kv[1]["qualified_name"], kv[1]["semantic_key"]))
                if eid != center["id"]
            ]
            unknown_or_nondef = anomalies["unknown_cond"] + anomalies["non_default_resolved"]
            incomplete = bool(snapshot["incomplete"] or anomalies["unresolved"] > 0 or unknown_or_nondef > 0)
            return {
                "generation": snapshot, "context_key": context_key, "symbol": center, "center": center,
                "direction": direction, "kinds": list(norm_kinds) if norm_kinds is not None else None,
                "statuses": list(norm_statuses) if norm_statuses is not None else None, "limit": limit,
                "truncated": truncated, "truncation_reason": "max_edges" if truncated else None,
                "incomplete": incomplete, "cycles_detected": cycles_detected, "self_edges_count": self_edges_count,
                "omitted_inactive_count": anomalies["inactive"], "omitted_unknown_condition_count": unknown_or_nondef,
                "nodes": nodes, "edges": visible_edges, "unresolved": unresolved_edges, "unresolved_edges": unresolved_edges,
            }

    def subgraph(
        self, seeds: list[str], context_key: str, direction: str = "out", depth: int = 1,
        kinds: Iterable[str | RelationKind] | None = None, statuses: Iterable[str | ResolutionStatus] | None = None,
        max_nodes: int = 100, max_edges: int = 300,
    ) -> dict[str, Any]:
        """Bounded multi-hop subgraph expansion within a single build context."""
        if isinstance(seeds, (str, bytes)) or not isinstance(seeds, Iterable):
            raise ValueError("Parameter 'seeds' must be a non-empty list of semantic keys.")
        seed_list: list[str] = []
        for s in seeds:
            sk = self._require_semantic_key(s, "seeds[]")
            if sk not in seed_list:
                seed_list.append(sk)
        if not seed_list:
            raise ValueError("Parameter 'seeds' cannot be empty.")

        context_key = self._require_context_key(context_key)
        direction = _validate_direction(direction)
        depth = _validate_int_bound("depth", depth, 0, MAX_SUBGRAPH_DEPTH)
        max_nodes = _validate_int_bound("max_nodes", max_nodes, 1, MAX_SUBGRAPH_NODES)
        max_edges = _validate_int_bound("max_edges", max_edges, 0, MAX_SUBGRAPH_EDGES)
        norm_kinds = _validate_kinds(kinds)
        norm_statuses = _validate_statuses(statuses)

        with self._read_transaction():
            snapshot, gen_id, ctx_id = self._get_snapshot_and_context(context_key)
            assert ctx_id is not None
            seed_nodes: list[dict[str, Any]] = []
            for sk in seed_list:
                node = self._find_entity_in_context(gen_id, ctx_id, sk)
                if node is None or gen_id is None:
                    raise SymbolNotFoundError(f"Seed symbol '{sk}' not found in context '{context_key}'.")
                seed_nodes.append(node)

            visited_nodes: dict[int, dict[str, Any]] = {}
            truncated, truncation_reason = False, None
            for sn in seed_nodes:
                if len(visited_nodes) >= max_nodes:
                    truncated, truncation_reason = True, "max_nodes"
                    break
                visited_nodes[sn["id"]] = sn

            kept_raw_rows: dict[tuple[Any, ...], Any] = {}
            cycles_detected = self_edges_count = unresolved_encountered = 0
            unknown_condition_encountered = inactive_encountered = recovered_encountered = 0
            frontier: list[int] = list(visited_nodes.keys())
            expanded_nodes: set[int] = set()

            for _hop in range(depth):
                if not frontier or (truncated and truncation_reason == "max_edges"):
                    break
                anom = self._count_frontier_anomalies(
                    gen_id=gen_id, ctx_id=ctx_id, entity_ids=frontier, direction=direction, kinds=norm_kinds
                )
                inactive_encountered += anom["inactive"]
                unknown_condition_encountered += anom["unknown_cond"]
                unresolved_encountered += anom["unresolved"]
                recovered_encountered += anom["non_default_resolved"]

                grouped_rows = self._query_grouped_relations(
                    gen_id=gen_id, ctx_id=ctx_id, entity_ids=frontier, direction=direction,
                    kinds=norm_kinds, statuses=norm_statuses, extra_sql=_DEFAULT_GRAPH_SQL,
                    row_limit=max_edges + max_nodes + 16,
                )
                expanded_nodes.update(frontier)
                next_frontier: list[int] = []

                for r in grouped_rows:
                    src_id, tgt_id = int(r["source_entity_id"]), int(r["target_entity_id"])
                    if (direction == "out" and src_id not in expanded_nodes) or (direction == "in" and tgt_id not in expanded_nodes):
                        continue
                    edge_sig = (src_id, tgt_id, str(r["kind"]), str(r["resolution_status"]), str(r["dispatch_kind"]), str(r["condition_state"]), str(r["reason"]))
                    if edge_sig in kept_raw_rows:
                        continue

                    other_ids = [eid for eid in (src_id, tgt_id) if eid not in visited_nodes]
                    if other_ids and (len(visited_nodes) + len(other_ids) > max_nodes):
                        truncated = True
                        truncation_reason = truncation_reason or "max_nodes"
                        continue
                    if len(kept_raw_rows) >= max_edges:
                        truncated = True
                        truncation_reason = truncation_reason or "max_edges"
                        break

                    if src_id == tgt_id:
                        self_edges_count += 1
                        cycles_detected += 1
                    elif not other_ids:
                        cycles_detected += 1

                    if other_ids:
                        new_nodes = self._fetch_nodes_by_ids(other_ids)
                        for nid in other_ids:
                            if nid in new_nodes:
                                visited_nodes[nid] = new_nodes[nid]
                                if nid not in next_frontier:
                                    next_frontier.append(nid)
                    kept_raw_rows[edge_sig] = r
                frontier = next_frontier

            hydrated_edges = self._hydrate_grouped_relations(list(kept_raw_rows.values()), context_key)
            incomplete = bool(snapshot["incomplete"] or unresolved_encountered > 0 or unknown_condition_encountered > 0 or recovered_encountered > 0)
            return {
                "generation": snapshot, "context_key": context_key, "seeds": seed_list,
                "direction": direction, "depth": depth, "max_nodes": max_nodes, "max_edges": max_edges,
                "truncated": truncated, "truncation_reason": truncation_reason, "incomplete": incomplete,
                "cycles_detected": cycles_detected, "self_edges_count": self_edges_count,
                "unresolved_encountered": unresolved_encountered,
                "unknown_condition_encountered": unknown_condition_encountered,
                "inactive_omitted": inactive_encountered,
                "nodes": list(visited_nodes.values()), "edges": hydrated_edges,
            }

    def find_path(
        self, source: str, target: str, context_key: str, direction: str = "out",
        kinds: Iterable[str | RelationKind] | None = None, statuses: Iterable[str | ResolutionStatus] | None = None,
        max_depth: int = 6, max_nodes: int = 10000, max_edges: int = 50000, timeout_seconds: float = 2,
    ) -> dict[str, Any]:
        """Bounded BFS shortest dependency path in a single context without claiming runtime reachability."""
        source = self._require_semantic_key(source, "source")
        target = self._require_semantic_key(target, "target")
        context_key = self._require_context_key(context_key)
        direction = _validate_direction(direction)
        norm_kinds = _validate_kinds(kinds)
        norm_statuses = _validate_statuses(statuses)
        max_depth = _validate_int_bound("max_depth", max_depth, 0, MAX_PATH_DEPTH)
        max_nodes = _validate_int_bound("max_nodes", max_nodes, 1, MAX_PATH_NODES)
        max_edges = _validate_int_bound("max_edges", max_edges, 0, MAX_PATH_EDGES)
        deadline = time.monotonic() + _validate_timeout(timeout_seconds)

        with self._read_transaction():
            snapshot, gen_id, ctx_id = self._get_snapshot_and_context(context_key)
            assert ctx_id is not None
            src_node = self._find_entity_in_context(gen_id, ctx_id, source)
            tgt_node = self._find_entity_in_context(gen_id, ctx_id, target)
            caveats = ["Static compile-context dependency path; does not prove runtime reachability."]

            def _build_res(
                status_val: str, found_val: bool, path_exists_val: bool | None,
                trunc_val: bool, trunc_reason: str | None, inc_val: bool,
                cycles: int, self_cnt: int, path_keys: list[str],
                nodes_list: list[dict[str, Any]], edges_list: list[dict[str, Any]],
            ) -> dict[str, Any]:
                return {
                    "generation": snapshot, "context_key": context_key, "source": source, "target": target,
                    "direction": direction, "status": status_val, "found": found_val,
                    "path_exists": path_exists_val, "runtime_reachability_proven": False,
                    "truncated": trunc_val, "truncation_reason": trunc_reason, "incomplete": inc_val,
                    "cycles_detected": cycles, "self_edges_count": self_cnt, "hop_count": len(edges_list),
                    "path": path_keys, "nodes": nodes_list, "edges": edges_list, "caveats": caveats,
                }

            if gen_id is None or src_node is None or tgt_node is None:
                is_inc = bool(snapshot["incomplete"] or gen_id is None)
                if is_inc:
                    caveats.append("Index or build context is incomplete; absence of path is not proven.")
                return _build_res("incomplete_index" if is_inc else "not_found_within_scope", False, None if is_inc else False, False, None, is_inc, 0, 0, [], [], [])

            src_id, tgt_id = src_node["id"], tgt_node["id"]
            if src_id == tgt_id:
                self_loop_row = self._conn.execute(
                    f"SELECT 1 FROM relations r JOIN generation_tu_runs gtr ON gtr.parse_run_id = r.parse_run_id "
                    f"JOIN translation_units tu ON tu.id = gtr.translation_unit_id "
                    f"WHERE gtr.generation_id = ? AND tu.build_context_id = ? AND tu.is_active = 1 "
                    f"AND r.build_context_id = ? AND r.source_entity_id = ? AND r.target_entity_id = ? AND {_DEFAULT_GRAPH_SQL} LIMIT 1",
                    (gen_id, ctx_id, ctx_id, src_id, src_id),
                ).fetchone()
                has_self = 1 if self_loop_row is not None else 0
                return _build_res("found", True, True, False, None, snapshot["incomplete"], has_self, has_self, [source], [src_node], [])

            visited: set[int] = {src_id}
            parent: dict[int, tuple[int, Any, str]] = {}
            frontier: list[int] = [src_id]
            nodes_visited, edges_examined = 1, 0
            cycles_detected = self_edges_count = unresolved_encountered = unknown_condition_encountered = recovered_encountered = 0
            truncated, truncation_reason, found = False, None, False
            found_self_loop_row: Any | None = None

            for _depth_step in range(1, max_depth + 1):
                if not frontier:
                    break
                if time.monotonic() > deadline:
                    truncated, truncation_reason = True, "timeout"
                    break

                anom = self._count_frontier_anomalies(
                    gen_id=gen_id, ctx_id=ctx_id, entity_ids=frontier, direction=direction, kinds=norm_kinds
                )
                unknown_condition_encountered += anom["unknown_cond"]
                unresolved_encountered += anom["unresolved"]
                recovered_encountered += anom["non_default_resolved"]

                grouped_rows = self._query_grouped_relations(
                    gen_id=gen_id, ctx_id=ctx_id, entity_ids=frontier, direction=direction,
                    kinds=norm_kinds, statuses=norm_statuses, extra_sql=_DEFAULT_GRAPH_SQL,
                    row_limit=max(1, max_edges - edges_examined + 1),
                )
                frontier_set = set(frontier)
                next_frontier: list[int] = []

                for r in grouped_rows:
                    if time.monotonic() > deadline:
                        truncated, truncation_reason = True, "timeout"
                        break
                    r_src, r_tgt = int(r["source_entity_id"]), int(r["target_entity_id"])
                    steps: list[tuple[int, int, str]] = []
                    if direction in ("out", "both") and r_src in frontier_set:
                        steps.append((r_src, r_tgt, "out"))
                    if r_tgt in frontier_set and (direction == "in" or (direction == "both" and r_src != r_tgt)):
                        steps.append((r_tgt, r_src, "in"))

                    for u, v, trav_dir in steps:
                        if edges_examined >= max_edges:
                            truncated, truncation_reason = True, "max_edges"
                            break
                        edges_examined += 1
                        if u == v:
                            self_edges_count += 1
                            cycles_detected += 1
                            if src_id == tgt_id and u == src_id and found_self_loop_row is None:
                                found_self_loop_row = r
                                found = True
                                break
                            continue
                        if v in visited:
                            cycles_detected += 1
                            continue
                        if v == tgt_id:
                            parent[v] = (u, r, trav_dir)
                            found = True
                            break
                        if nodes_visited >= max_nodes:
                            truncated = True
                            truncation_reason = truncation_reason or "max_nodes"
                            continue
                        visited.add(v)
                        nodes_visited += 1
                        parent[v] = (u, r, trav_dir)
                        next_frontier.append(v)

                    if found or truncated:
                        break
                if found or truncated:
                    break
                frontier = next_frontier

            if not found and src_id == tgt_id:
                return _build_res("found", True, True, False, None, snapshot["incomplete"], cycles_detected, self_edges_count, [source], [src_node], [])

            if not found and not truncated and frontier:
                anom = self._count_frontier_anomalies(
                    gen_id=gen_id, ctx_id=ctx_id, entity_ids=frontier, direction=direction, kinds=norm_kinds
                )
                unknown_condition_encountered += anom["unknown_cond"]
                unresolved_encountered += anom["unresolved"]
                recovered_encountered += anom["non_default_resolved"]
                frontier_set = set(frontier)
                rem_rows = self._query_grouped_relations(
                    gen_id=gen_id, ctx_id=ctx_id, entity_ids=frontier, direction=direction,
                    kinds=norm_kinds, statuses=norm_statuses, extra_sql=_DEFAULT_GRAPH_SQL, row_limit=len(visited) + 10,
                )
                for r in rem_rows:
                    r_src, r_tgt = int(r["source_entity_id"]), int(r["target_entity_id"])
                    next_cands: list[int] = []
                    if direction in ("out", "both") and r_src in frontier_set:
                        next_cands.append(r_tgt)
                    if direction in ("in", "both") and r_tgt in frontier_set:
                        next_cands.append(r_src)
                    if any(nxt not in visited for nxt in next_cands):
                        truncated, truncation_reason = True, "max_depth"
                        break

            incomplete = bool(snapshot["incomplete"] or unresolved_encountered > 0 or unknown_condition_encountered > 0 or recovered_encountered > 0)
            if unresolved_encountered > 0:
                caveats.append(f"Encountered {unresolved_encountered} unresolved/ambiguous relation(s) along search frontier.")
            if unknown_condition_encountered > 0:
                caveats.append(f"Encountered {unknown_condition_encountered} relation(s) with unknown preprocessor condition state.")

            if found:
                if src_id == tgt_id and found_self_loop_row is not None:
                    edges = self._hydrate_grouped_relations([found_self_loop_row], context_key)[:1]
                    if edges:
                        edges[0]["traversal_direction"] = "out"
                    return _build_res("found", True, True, False, None, incomplete, cycles_detected, self_edges_count, [source, target], [src_node], edges)

                rev_node_ids, rev_raw_rows, rev_dirs, cur = [tgt_id], [], [], tgt_id
                while cur != src_id:
                    prev_id, grp_row, trav_dir = parent[cur]
                    rev_raw_rows.append(grp_row)
                    rev_dirs.append(trav_dir)
                    rev_node_ids.append(prev_id)
                    cur = prev_id
                node_ids_ordered = list(reversed(rev_node_ids))
                edges_ordered = self._hydrate_grouped_relations(list(reversed(rev_raw_rows)), context_key)
                for edge_d, td in zip(edges_ordered, reversed(rev_dirs)):
                    edge_d["traversal_direction"] = td
                nodes_map = self._fetch_nodes_by_ids(node_ids_ordered)
                nodes_ordered = [nodes_map[nid] for nid in node_ids_ordered]
                return _build_res("found", True, True, False, None, incomplete, cycles_detected, self_edges_count, [n["semantic_key"] for n in nodes_ordered], nodes_ordered, edges_ordered)

            if truncated:
                caveats.append(f"Search truncated by '{truncation_reason}'; absence of path is not proven.")
                return _build_res("truncated", False, None, True, truncation_reason, incomplete, cycles_detected, self_edges_count, [], [], [])
            if incomplete:
                caveats.append("Index or search frontier has incomplete/unresolved facts; absence of path is not proven.")
                return _build_res("incomplete_index", False, None, False, None, True, cycles_detected, self_edges_count, [], [], [])
            return _build_res("not_found_within_scope", False, False, False, None, False, cycles_detected, self_edges_count, [], [], [])

    def snippet(self, semantic_key: str, context_key: str, max_lines: int = 200) -> dict[str, Any]:
        """Retrieve freshness-verified original source snippet for a symbol in a context."""
        semantic_key = self._require_semantic_key(semantic_key)
        context_key = self._require_context_key(context_key)
        max_lines = _validate_int_bound("max_lines", max_lines, 1, MAX_SNIPPET_LINES)

        with self._read_transaction():
            snapshot, gen_id, ctx_id = self._get_snapshot_and_context(context_key)
            assert ctx_id is not None
            node = self._find_entity_in_context(gen_id, ctx_id, semantic_key)
            if node is None or gen_id is None:
                raise SymbolNotFoundError(f"Symbol '{semantic_key}' not found in context '{context_key}'.")

            occ_rows = self._conn.execute(
                "SELECT so.id, so.occurrence_kind, so.signature, so.condition_state, so.extraction_quality, "
                "so.byte_start, so.byte_end, so.start_line, so.start_col, so.end_line, so.end_col, "
                "so.file_version_id, fv.content_sha256, fv.encoding, fv.has_bom, fv.decode_clean, fv.line_count, "
                "pr.parser_name, pr.parser_version, "
                "f.repo_path, f.current_version_id, f.is_deleted "
                "FROM symbol_occurrences so JOIN generation_tu_runs gtr ON gtr.parse_run_id = so.parse_run_id "
                "JOIN parse_runs pr ON pr.id = so.parse_run_id "
                "JOIN translation_units tu ON tu.id = gtr.translation_unit_id "
                "JOIN file_versions fv ON fv.id = so.file_version_id JOIN files f ON f.id = fv.file_id "
                "WHERE gtr.generation_id = ? AND tu.build_context_id = ? AND tu.is_active = 1 AND so.entity_id = ?",
                (gen_id, ctx_id, node["id"]),
            ).fetchall()

            if not occ_rows:
                return {
                    "generation": snapshot, "context_key": context_key, "semantic_key": semantic_key,
                    "symbol": node, "status": "missing", "is_stale": True, "reason": "no_source_occurrence",
                    "repo_path": None, "path": None, "indexed_file_version_id": None, "current_file_version_id": None,
                    "indexed_content_sha256": None, "current_content_sha256": None, "start_line": None, "end_line": None,
                    "line_count": 0, "truncated": False, "truncation_reason": None, "omitted_lines": 0, "content": None, "snippet": None,
                }

            chosen = min(occ_rows, key=lambda r: (
                _CONDITION_PRIORITY.get(str(r["condition_state"]), 9),
                _OCCURRENCE_PRIORITY.get(str(r["occurrence_kind"]), 99),
                str(r["repo_path"]), int(r["start_line"]), int(r["id"]),
            ))
            fresh = self._inspect_file_freshness(
                repo_path=str(chosen["repo_path"]), indexed_version_id=int(chosen["file_version_id"]),
                current_version_id=int(chosen["current_version_id"]) if chosen["current_version_id"] is not None else None,
                is_deleted=bool(chosen["is_deleted"]), indexed_sha256=str(chosen["content_sha256"]),
            )
            start_line, end_line = int(chosen["start_line"]), int(chosen["end_line"])

            if fresh["status"] != "ok" or fresh["raw_bytes"] is None:
                return {
                    "generation": snapshot, "context_key": context_key, "semantic_key": semantic_key,
                    "symbol": node, "status": fresh["status"], "is_stale": True, "reason": f"source_{fresh['status']}",
                    "repo_path": fresh["repo_path"], "path": fresh["repo_path"],
                    "occurrence_kind": str(chosen["occurrence_kind"]), "condition_state": str(chosen["condition_state"]),
                    "indexed_file_version_id": fresh["indexed_file_version_id"], "current_file_version_id": fresh["current_file_version_id"],
                    "indexed_content_sha256": fresh["indexed_content_sha256"], "current_content_sha256": fresh["current_content_sha256"],
                    "encoding": str(chosen["encoding"]), "start_line": start_line, "end_line": end_line, "original_end_line": end_line,
                    "byte_start": int(chosen["byte_start"]), "byte_end": int(chosen["byte_end"]),
                    "line_count": 0, "truncated": False, "truncation_reason": None, "omitted_lines": 0, "content": None, "snippet": None,
                }

            # File versions are keyed by original byte hash. Their inventory
            # codec can predate a corrected parser policy for those same bytes;
            # interpret new semantic evidence using its own frozen run policy.
            version = str(chosen["parser_version"])
            if chosen["parser_name"] == "clang" and ";input=" in version:
                from .parser_input import SourceBuffer
                fallback = version.rsplit(":", 1)[1]
                buffer = SourceBuffer.create(fresh["raw_bytes"], fallback)
                encoding, decoded_text = buffer.encoding, buffer.parser.decode("utf-8")
            else:
                encoding, _, _, decoded_text = detect_encoding_and_decode(
                    fresh["raw_bytes"], preferred_legacy_encoding=str(chosen["encoding"]),
                )
            all_lines = decoded_text.splitlines()
            span_lines = all_lines[max(0, start_line - 1) : min(len(all_lines), end_line)]
            truncated = len(span_lines) > max_lines
            omitted_lines = max(0, len(span_lines) - max_lines)
            if truncated:
                span_lines = span_lines[:max_lines]
            actual_end_line = start_line + len(span_lines) - 1 if span_lines else start_line
            content_text = "\n".join(span_lines)

            return {
                "generation": snapshot, "context_key": context_key, "semantic_key": semantic_key,
                "symbol": node, "status": "ok", "is_stale": False, "reason": None,
                "repo_path": fresh["repo_path"], "path": fresh["repo_path"],
                "occurrence_kind": str(chosen["occurrence_kind"]), "condition_state": str(chosen["condition_state"]),
                "signature": str(chosen["signature"]),
                "indexed_file_version_id": fresh["indexed_file_version_id"], "current_file_version_id": fresh["current_file_version_id"],
                "indexed_content_sha256": fresh["indexed_content_sha256"], "current_content_sha256": fresh["current_content_sha256"],
                "encoding": encoding, "start_line": start_line, "end_line": actual_end_line, "original_end_line": end_line,
                "byte_start": int(chosen["byte_start"]), "byte_end": int(chosen["byte_end"]),
                "line_count": len(span_lines), "truncated": truncated,
                "truncation_reason": "max_lines" if truncated else None, "omitted_lines": omitted_lines,
                "content": content_text, "snippet": content_text,
            }
