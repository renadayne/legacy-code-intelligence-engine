"""Versioned SQLite storage, generation staging/publication, and TU invalidation."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from . import SCHEMA_VERSION, __version__
from .discovery import discover_repository_files, resolve_repo_root
from .models import (
    BuildContextSpec,
    CandidateTargetRecord,
    ConditionState,
    DependencyKind,
    DiagnosticRecord,
    DiagnosticSeverity,
    DispatchKind,
    EntityKind,
    EntityRecord,
    ExtractionQuality,
    FileInventoryRecord,
    GenerationStatus,
    LinkageScope,
    OccurrenceKind,
    ObservationRecord,
    ParseRunPayload,
    ParseStatus,
    RelationEvidenceRecord,
    RelationKind,
    RelationRecord,
    ResolutionReason,
    ResolutionStatus,
    SourceSpan,
    SymbolOccurrenceRecord,
    TUDependencyRecord,
    TranslationUnitSpec,
)

SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS repositories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_root TEXT NOT NULL UNIQUE,
    source_revision TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repository_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    repo_path TEXT NOT NULL,
    file_role TEXT NOT NULL DEFAULT 'source',
    is_deleted INTEGER NOT NULL DEFAULT 0,
    current_version_id INTEGER,
    UNIQUE(repository_id, repo_path)
);

CREATE TABLE IF NOT EXISTS file_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    content_sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    encoding TEXT NOT NULL,
    has_bom INTEGER NOT NULL DEFAULT 0,
    decode_clean INTEGER NOT NULL DEFAULT 1,
    line_count INTEGER NOT NULL DEFAULT 1,
    line_byte_offsets_json TEXT NOT NULL DEFAULT '[]',
    recorded_at TEXT NOT NULL,
    UNIQUE(file_id, content_sha256)
);

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repository_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    project_name TEXT NOT NULL,
    target_name TEXT NOT NULL,
    UNIQUE(repository_id, project_name, target_name)
);

CREATE TABLE IF NOT EXISTS build_contexts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    context_key TEXT NOT NULL UNIQUE,
    configuration TEXT NOT NULL,
    platform TEXT NOT NULL,
    working_directory TEXT NOT NULL,
    compiler_family TEXT NOT NULL,
    toolset_version TEXT NOT NULL,
    architecture TEXT NOT NULL,
    language_standard TEXT NOT NULL,
    include_dirs_json TEXT NOT NULL,
    system_include_dirs_json TEXT NOT NULL,
    defines_json TEXT NOT NULL,
    undefines_json TEXT NOT NULL,
    forced_includes_json TEXT NOT NULL,
    extra_flags_json TEXT NOT NULL,
    pch_header TEXT NOT NULL,
    sdk_version TEXT NOT NULL,
    mfc_mode TEXT NOT NULL,
    provenance TEXT NOT NULL,
    fingerprint TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS translation_units (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    build_context_id INTEGER NOT NULL REFERENCES build_contexts(id) ON DELETE CASCADE,
    main_file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    tu_key TEXT NOT NULL UNIQUE,
    per_file_include_dirs_json TEXT NOT NULL,
    per_file_defines_json TEXT NOT NULL,
    per_file_undefines_json TEXT NOT NULL,
    per_file_forced_includes_json TEXT NOT NULL,
    per_file_flags_json TEXT NOT NULL,
    response_files_json TEXT NOT NULL,
    flags_fingerprint TEXT NOT NULL,
    command_origin TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS generations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repository_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    parent_generation_id INTEGER REFERENCES generations(id),
    status TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    mfc_rule_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    published_at TEXT,
    notes TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS parse_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    generation_id INTEGER NOT NULL REFERENCES generations(id) ON DELETE CASCADE,
    translation_unit_id INTEGER NOT NULL REFERENCES translation_units(id) ON DELETE CASCADE,
    main_file_version_id INTEGER NOT NULL REFERENCES file_versions(id) ON DELETE CASCADE,
    parser_name TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    mfc_rule_version TEXT NOT NULL,
    job_fingerprint TEXT NOT NULL,
    dependency_fingerprint TEXT NOT NULL,
    has_incomplete_dependencies INTEGER NOT NULL DEFAULT 0,
    parse_status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS generation_tu_runs (
    generation_id INTEGER NOT NULL REFERENCES generations(id) ON DELETE CASCADE,
    translation_unit_id INTEGER NOT NULL REFERENCES translation_units(id) ON DELETE CASCADE,
    parse_run_id INTEGER NOT NULL REFERENCES parse_runs(id) ON DELETE CASCADE,
    PRIMARY KEY (generation_id, translation_unit_id)
);

CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repository_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    semantic_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    linkage_scope TEXT NOT NULL,
    scope_qualifier TEXT NOT NULL DEFAULT '',
    parent_semantic_key TEXT,
    is_external INTEGER NOT NULL DEFAULT 0,
    UNIQUE(repository_id, semantic_key)
);

CREATE TABLE IF NOT EXISTS symbol_occurrences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    parse_run_id INTEGER NOT NULL REFERENCES parse_runs(id) ON DELETE CASCADE,
    entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    file_version_id INTEGER NOT NULL REFERENCES file_versions(id) ON DELETE CASCADE,
    occurrence_kind TEXT NOT NULL,
    signature TEXT NOT NULL DEFAULT '',
    parent_entity_id INTEGER REFERENCES entities(id) ON DELETE SET NULL,
    byte_start INTEGER NOT NULL,
    byte_end INTEGER NOT NULL,
    start_line INTEGER NOT NULL,
    start_col INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    end_col INTEGER NOT NULL,
    condition_state TEXT NOT NULL,
    extraction_quality TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    parse_run_id INTEGER NOT NULL REFERENCES parse_runs(id) ON DELETE CASCADE,
    observation_key TEXT NOT NULL,
    observation_type TEXT NOT NULL,
    subject_entity_id INTEGER REFERENCES entities(id) ON DELETE SET NULL,
    file_version_id INTEGER NOT NULL REFERENCES file_versions(id) ON DELETE CASCADE,
    byte_start INTEGER NOT NULL,
    byte_end INTEGER NOT NULL,
    start_line INTEGER NOT NULL,
    start_col INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    end_col INTEGER NOT NULL,
    condition_state TEXT NOT NULL,
    extraction_quality TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    parser_name TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(parse_run_id, observation_key)
);

CREATE TABLE IF NOT EXISTS relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    parse_run_id INTEGER NOT NULL REFERENCES parse_runs(id) ON DELETE CASCADE,
    build_context_id INTEGER NOT NULL REFERENCES build_contexts(id) ON DELETE CASCADE,
    source_entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    target_entity_id INTEGER REFERENCES entities(id) ON DELETE SET NULL,
    kind TEXT NOT NULL,
    resolution_status TEXT NOT NULL,
    dispatch_kind TEXT NOT NULL,
    extraction_quality TEXT NOT NULL,
    condition_state TEXT NOT NULL,
    reason TEXT NOT NULL,
    unresolved_target_expr TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS relation_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    relation_id INTEGER NOT NULL REFERENCES relations(id) ON DELETE CASCADE,
    observation_id INTEGER NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
    file_version_id INTEGER NOT NULL REFERENCES file_versions(id) ON DELETE CASCADE,
    byte_start INTEGER NOT NULL,
    byte_end INTEGER NOT NULL,
    start_line INTEGER NOT NULL,
    start_col INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    end_col INTEGER NOT NULL,
    evidence_snippet_hash TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    parser_name TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS relation_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    relation_id INTEGER NOT NULL REFERENCES relations(id) ON DELETE CASCADE,
    candidate_entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    candidate_reason TEXT NOT NULL,
    evidence_observation_id INTEGER REFERENCES observations(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS tu_dependencies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    parse_run_id INTEGER NOT NULL REFERENCES parse_runs(id) ON DELETE CASCADE,
    file_id INTEGER REFERENCES files(id) ON DELETE SET NULL,
    file_version_id INTEGER REFERENCES file_versions(id) ON DELETE SET NULL,
    dependency_kind TEXT NOT NULL,
    included_from_file_id INTEGER REFERENCES files(id) ON DELETE SET NULL,
    include_line INTEGER,
    is_missing INTEGER NOT NULL DEFAULT 0,
    missing_include_text TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS diagnostics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    parse_run_id INTEGER NOT NULL REFERENCES parse_runs(id) ON DELETE CASCADE,
    file_version_id INTEGER REFERENCES file_versions(id) ON DELETE SET NULL,
    severity TEXT NOT NULL,
    code TEXT NOT NULL,
    message TEXT NOT NULL,
    parse_scope TEXT NOT NULL,
    byte_start INTEGER,
    byte_end INTEGER,
    start_line INTEGER,
    start_col INTEGER,
    end_line INTEGER,
    end_col INTEGER,
    missing_dependency TEXT NOT NULL DEFAULT '',
    unsupported_feature TEXT NOT NULL DEFAULT ''
);

-- Indexes per Section 5.2 of IMPLEMENTATION_PLAN.vi.md
CREATE INDEX IF NOT EXISTS idx_files_repo_path ON files(repository_id, repo_path);
CREATE INDEX IF NOT EXISTS idx_entities_norm_name ON entities(repository_id, normalized_name);
CREATE INDEX IF NOT EXISTS idx_entities_qual_name ON entities(repository_id, qualified_name);
CREATE INDEX IF NOT EXISTS idx_entities_sem_key ON entities(repository_id, semantic_key);
CREATE INDEX IF NOT EXISTS idx_occurrences_entity ON symbol_occurrences(entity_id, parse_run_id);
CREATE INDEX IF NOT EXISTS idx_occurrences_file_ver ON symbol_occurrences(file_version_id, parse_run_id);
CREATE INDEX IF NOT EXISTS idx_relations_fwd ON relations(build_context_id, source_entity_id, kind, resolution_status, target_entity_id);
CREATE INDEX IF NOT EXISTS idx_relations_rev ON relations(build_context_id, target_entity_id, kind, resolution_status, source_entity_id);
CREATE INDEX IF NOT EXISTS idx_relations_run ON relations(parse_run_id);
CREATE INDEX IF NOT EXISTS idx_tu_deps_file ON tu_dependencies(file_id, parse_run_id);
CREATE INDEX IF NOT EXISTS idx_tu_deps_run ON tu_dependencies(parse_run_id);
CREATE INDEX IF NOT EXISTS idx_gen_tu_runs_run ON generation_tu_runs(parse_run_id);
"""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class IndexStore:
    """Versioned SQLite store managing contexts, TU-owned observations, and atomic generations."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).resolve(strict=False)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._configure_connection()

    def _configure_connection(self) -> None:
        self._conn.execute("PRAGMA foreign_keys = ON;")
        self._conn.execute("PRAGMA journal_mode = WAL;")
        self._conn.execute("PRAGMA synchronous = NORMAL;")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> IndexStore:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def initialize_schema(self, repo_root: str | Path, *, source_revision: str = "") -> int:
        """Create or verify the versioned schema and register the canonical repository root."""
        canonical_root = str(resolve_repo_root(repo_root))
        now = _utc_now_iso()
        with self._conn:
            self._conn.executescript(SCHEMA_DDL)
            existing_ver = self._get_meta("schema_version")
            if existing_ver is not None and int(existing_ver) != SCHEMA_VERSION:
                raise RuntimeError(
                    f"Database schema_version {existing_ver} is incompatible with expected {SCHEMA_VERSION}."
                )
            self._set_meta("schema_version", str(SCHEMA_VERSION))
            self._set_meta("tool_version", __version__)

            existing_root = self._conn.execute(
                "SELECT repo_root FROM repositories LIMIT 1"
            ).fetchone()
            if existing_root is not None and str(existing_root["repo_root"]) != canonical_root:
                raise ValueError(
                    "This index belongs to a different repository. "
                    "Use a separate database for each source root."
                )

            row = self._conn.execute(
                "SELECT id FROM repositories WHERE repo_root = ?",
                (canonical_root,),
            ).fetchone()
            if row is None:
                cur = self._conn.execute(
                    """
                    INSERT INTO repositories(repo_root, source_revision, created_at, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (canonical_root, source_revision, now, now),
                )
                repo_id = int(cur.lastrowid)
            else:
                repo_id = int(row["id"])
                self._conn.execute(
                    "UPDATE repositories SET updated_at = ?, source_revision = COALESCE(NULLIF(?, ''), source_revision) WHERE id = ?",
                    (now, source_revision, repo_id),
                )
            self._set_meta("active_repository_id", str(repo_id))
            return repo_id

    def _get_meta(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM schema_meta WHERE key = ?",
            (key,),
        ).fetchone()
        return str(row["value"]) if row is not None else None

    def _set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            """
            INSERT INTO schema_meta(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )

    def get_active_repository_id(self) -> int:
        val = self._get_meta("active_repository_id")
        if val is None:
            raise RuntimeError("IndexStore is not initialized with a repository.")
        return int(val)

    def get_repo_root(self) -> Path:
        repo_id = self.get_active_repository_id()
        row = self._conn.execute(
            "SELECT repo_root FROM repositories WHERE id = ?",
            (repo_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"Repository id {repo_id} not found.")
        return Path(str(row["repo_root"]))

    def get_published_generation_id(self) -> int | None:
        val = self._get_meta("published_generation_id")
        return int(val) if val is not None and val != "" else None

    def sync_file_inventory(
        self,
        records: Iterable[FileInventoryRecord],
        *,
        mark_missing_deleted: bool = True,
    ) -> dict[str, Any]:
        """Upsert file inventory and content-hash versions; return summary of added/modified/deleted/unchanged files."""
        repo_id = self.get_active_repository_id()
        now = _utc_now_iso()
        added: list[str] = []
        modified: list[str] = []
        unchanged: list[str] = []
        deleted: list[str] = []

        seen_paths: set[str] = set()
        with self._conn:
            existing_rows = self._conn.execute(
                """
                SELECT f.id, f.repo_path, f.is_deleted, f.current_version_id, fv.content_sha256
                FROM files f
                LEFT JOIN file_versions fv ON fv.id = f.current_version_id
                WHERE f.repository_id = ?
                """,
                (repo_id,),
            ).fetchall()
            existing_by_path = {str(r["repo_path"]): r for r in existing_rows}

            for rec in records:
                seen_paths.add(rec.repo_path)
                existing = existing_by_path.get(rec.repo_path)
                if existing is None:
                    cur_f = self._conn.execute(
                        """
                        INSERT INTO files(repository_id, repo_path, file_role, is_deleted, current_version_id)
                        VALUES (?, ?, ?, 0, NULL)
                        """,
                        (repo_id, rec.repo_path, rec.file_role),
                    )
                    file_id = int(cur_f.lastrowid)
                    ver_id = self._ensure_file_version(file_id, rec, now)
                    self._conn.execute(
                        "UPDATE files SET current_version_id = ? WHERE id = ?",
                        (ver_id, file_id),
                    )
                    added.append(rec.repo_path)
                else:
                    file_id = int(existing["id"])
                    was_deleted = bool(existing["is_deleted"])
                    prev_sha = existing["content_sha256"]
                    ver_id = self._ensure_file_version(file_id, rec, now)
                    self._conn.execute(
                        """
                        UPDATE files
                        SET file_role = ?, is_deleted = 0, current_version_id = ?
                        WHERE id = ?
                        """,
                        (rec.file_role, ver_id, file_id),
                    )
                    if was_deleted:
                        added.append(rec.repo_path)
                    elif prev_sha != rec.content_sha256:
                        modified.append(rec.repo_path)
                    else:
                        unchanged.append(rec.repo_path)

            if mark_missing_deleted:
                for path_str, row in existing_by_path.items():
                    if path_str not in seen_paths and not bool(row["is_deleted"]):
                        self._conn.execute(
                            "UPDATE files SET is_deleted = 1, current_version_id = NULL WHERE id = ?",
                            (int(row["id"]),),
                        )
                        deleted.append(path_str)

        return {
            "added": sorted(added),
            "modified": sorted(modified),
            "unchanged": sorted(unchanged),
            "deleted": sorted(deleted),
        }

    def _ensure_file_version(
        self, file_id: int, rec: FileInventoryRecord, recorded_at: str
    ) -> int:
        row = self._conn.execute(
            "SELECT id FROM file_versions WHERE file_id = ? AND content_sha256 = ?",
            (file_id, rec.content_sha256),
        ).fetchone()
        if row is not None:
            return int(row["id"])
        cur = self._conn.execute(
            """
            INSERT INTO file_versions(
                file_id, content_sha256, size_bytes, encoding, has_bom,
                decode_clean, line_count, line_byte_offsets_json, recorded_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                file_id,
                rec.content_sha256,
                rec.size_bytes,
                rec.encoding,
                1 if rec.has_bom else 0,
                1 if rec.decode_clean else 0,
                rec.line_count,
                json.dumps(list(rec.line_byte_offsets)),
                recorded_at,
            ),
        )
        return int(cur.lastrowid)

    def _ensure_file_placeholder(self, repo_path: str) -> int:
        repo_id = self.get_active_repository_id()
        row = self._conn.execute(
            "SELECT id FROM files WHERE repository_id = ? AND repo_path = ?",
            (repo_id, repo_path),
        ).fetchone()
        if row is not None:
            return int(row["id"])
        cur = self._conn.execute(
            """
            INSERT INTO files(repository_id, repo_path, file_role, is_deleted, current_version_id)
            VALUES (?, ?, 'source', 1, NULL)
            """,
            (repo_id, repo_path),
        )
        return int(cur.lastrowid)

    def register_build_context(self, ctx: BuildContextSpec) -> int:
        """Upsert a project and build context specification."""
        repo_id = self.get_active_repository_id()
        with self._conn:
            return self._register_build_context_in_tx(repo_id, ctx)

    def _register_build_context_in_tx(self, repo_id: int, ctx: BuildContextSpec) -> int:
        self._conn.execute(
            """
            INSERT INTO projects(repository_id, project_name, target_name)
            VALUES (?, ?, ?)
            ON CONFLICT(repository_id, project_name, target_name) DO NOTHING
            """,
            (repo_id, ctx.project_name, ctx.target_name),
        )
        proj_row = self._conn.execute(
            "SELECT id FROM projects WHERE repository_id = ? AND project_name = ? AND target_name = ?",
            (repo_id, ctx.project_name, ctx.target_name),
        ).fetchone()
        project_id = int(proj_row["id"])

        self._conn.execute(
            """
            INSERT INTO build_contexts(
                project_id, context_key, configuration, platform, working_directory,
                compiler_family, toolset_version, architecture, language_standard,
                include_dirs_json, system_include_dirs_json, defines_json, undefines_json,
                forced_includes_json, extra_flags_json, pch_header, sdk_version,
                mfc_mode, provenance, fingerprint
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(context_key) DO UPDATE SET
                fingerprint = excluded.fingerprint,
                provenance = excluded.provenance
            """,
            (
                project_id,
                ctx.context_key,
                ctx.configuration,
                ctx.platform,
                ctx.working_directory,
                ctx.compiler_family,
                ctx.toolset_version,
                ctx.architecture,
                ctx.language_standard,
                json.dumps(list(ctx.include_dirs)),
                json.dumps(list(ctx.system_include_dirs)),
                json.dumps(list(ctx.defines)),
                json.dumps(list(ctx.undefines)),
                json.dumps(list(ctx.forced_includes)),
                json.dumps(list(ctx.extra_flags)),
                ctx.pch_header,
                ctx.sdk_version,
                ctx.mfc_mode,
                ctx.provenance,
                ctx.fingerprint,
            ),
        )
        ctx_row = self._conn.execute(
            "SELECT id FROM build_contexts WHERE context_key = ?",
            (ctx.context_key,),
        ).fetchone()
        return int(ctx_row["id"])

    def register_translation_units(
        self,
        tu_specs: Iterable[TranslationUnitSpec],
        *,
        deactivate_unlisted_for_contexts: bool = False,
    ) -> list[int]:
        """Upsert translation units for their respective build contexts."""
        repo_id = self.get_active_repository_id()
        tu_ids: list[int] = []
        seen_by_context: dict[int, set[str]] = {}

        with self._conn:
            for spec in tu_specs:
                ctx_id = self._register_build_context_in_tx(repo_id, spec.build_context)
                file_id = self._ensure_file_placeholder(spec.main_file_path)
                self._conn.execute(
                    """
                    INSERT INTO translation_units(
                        build_context_id, main_file_id, tu_key,
                        per_file_include_dirs_json, per_file_defines_json,
                        per_file_undefines_json, per_file_forced_includes_json,
                        per_file_flags_json, response_files_json,
                        flags_fingerprint, command_origin, is_active
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                    ON CONFLICT(tu_key) DO UPDATE SET
                        flags_fingerprint = excluded.flags_fingerprint,
                        command_origin = excluded.command_origin,
                        is_active = 1
                    """,
                    (
                        ctx_id,
                        file_id,
                        spec.tu_key,
                        json.dumps(list(spec.per_file_include_dirs)),
                        json.dumps(list(spec.per_file_defines)),
                        json.dumps(list(spec.per_file_undefines)),
                        json.dumps(list(spec.per_file_forced_includes)),
                        json.dumps(list(spec.per_file_flags)),
                        json.dumps(list(spec.response_files)),
                        spec.flags_fingerprint,
                        spec.command_origin,
                    ),
                )
                row = self._conn.execute(
                    "SELECT id FROM translation_units WHERE tu_key = ?",
                    (spec.tu_key,),
                ).fetchone()
                tu_ids.append(int(row["id"]))
                seen_by_context.setdefault(ctx_id, set()).add(spec.tu_key)

            if deactivate_unlisted_for_contexts:
                for ctx_id, active_keys in seen_by_context.items():
                    rows = self._conn.execute(
                        "SELECT id, tu_key FROM translation_units WHERE build_context_id = ?",
                        (ctx_id,),
                    ).fetchall()
                    for r in rows:
                        if str(r["tu_key"]) not in active_keys:
                            self._conn.execute(
                                "UPDATE translation_units SET is_active = 0 WHERE id = ?",
                                (int(r["id"]),),
                            )
        return tu_ids

    def begin_generation(
        self,
        *,
        parser_version: str = "0.1.0",
        mfc_rule_version: str = "0.1.0",
        notes: str = "",
        inherit_from_published: bool = True,
    ) -> int:
        """Create a new staging generation, optionally inheriting active TU parse runs from the published generation."""
        repo_id = self.get_active_repository_id()
        parent_gen_id = self.get_published_generation_id() if inherit_from_published else None
        now = _utc_now_iso()
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO generations(
                    repository_id, parent_generation_id, status,
                    parser_version, mfc_rule_version, created_at, published_at, notes
                )
                VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    repo_id,
                    parent_gen_id,
                    GenerationStatus.STAGING.value,
                    parser_version,
                    mfc_rule_version,
                    now,
                    notes,
                ),
            )
            gen_id = int(cur.lastrowid)
            if parent_gen_id is not None:
                # Copy active TU -> parse_run mappings for TUs that are still active and whose main file is not deleted
                self._conn.execute(
                    """
                    INSERT INTO generation_tu_runs(generation_id, translation_unit_id, parse_run_id)
                    SELECT ?, gtr.translation_unit_id, gtr.parse_run_id
                    FROM generation_tu_runs gtr
                    JOIN translation_units tu ON tu.id = gtr.translation_unit_id
                    JOIN files f ON f.id = tu.main_file_id
                    WHERE gtr.generation_id = ? AND tu.is_active = 1 AND f.is_deleted = 0
                    """,
                    (gen_id, parent_gen_id),
                )
            return gen_id

    def retract_translation_unit_from_generation(self, generation_id: int, tu_key: str) -> bool:
        """Retract a translation unit's observations from the specified staging generation."""
        self._assert_generation_staging(generation_id)
        with self._conn:
            row = self._conn.execute(
                "SELECT id FROM translation_units WHERE tu_key = ?",
                (tu_key,),
            ).fetchone()
            if row is None:
                return False
            cur = self._conn.execute(
                "DELETE FROM generation_tu_runs WHERE generation_id = ? AND translation_unit_id = ?",
                (generation_id, int(row["id"])),
            )
            return cur.rowcount > 0

    def record_parse_run(self, generation_id: int, payload: ParseRunPayload) -> int:
        """Record a TU parse run into a staging generation and update generation_tu_runs if verified."""
        self._assert_generation_staging(generation_id)
        repo_id = self.get_active_repository_id()
        now = _utc_now_iso()

        with self._conn:
            tu_row = self._conn.execute(
                """
                SELECT tu.id AS tu_id, tu.build_context_id, tu.flags_fingerprint,
                       f.id AS file_id, f.repo_path, f.current_version_id, f.is_deleted,
                       fv.content_sha256
                FROM translation_units tu
                JOIN files f ON f.id = tu.main_file_id
                LEFT JOIN file_versions fv ON fv.id = f.current_version_id
                WHERE tu.tu_key = ?
                """,
                (payload.tu_key,),
            ).fetchone()
            if tu_row is None:
                raise KeyError(f"Translation unit not registered: {payload.tu_key}")
            if bool(tu_row["is_deleted"]) or tu_row["current_version_id"] is None:
                raise ValueError(
                    f"Cannot record parse run for deleted or unsynced main file '{tu_row['repo_path']}'."
                )

            tu_id = int(tu_row["tu_id"])
            build_context_id = int(tu_row["build_context_id"])
            main_file_id = int(tu_row["file_id"])
            main_file_ver_id = int(tu_row["current_version_id"])
            main_sha = str(tu_row["content_sha256"])

            # Compute dependency fingerprint across main file + all consumed dependency file versions
            dep_hashes: list[str] = [f"main:{tu_row['repo_path']}:{main_sha}"]
            resolved_deps: list[tuple[TUDependencyRecord, int | None, int | None, int | None]] = []
            has_incomplete_deps = payload.has_incomplete_dependencies

            for dep in payload.dependencies:
                if dep.is_missing:
                    has_incomplete_deps = True
                    dep_hashes.append(f"missing:{dep.missing_include_text or dep.repo_path}")
                    inc_from_id = (
                        self._get_file_id_by_path(repo_id, dep.included_from_path)
                        if dep.included_from_path
                        else None
                    )
                    resolved_deps.append((dep, None, None, inc_from_id))
                else:
                    dep_file_row = self._conn.execute(
                        """
                        SELECT f.id AS file_id, f.current_version_id, fv.content_sha256
                        FROM files f
                        LEFT JOIN file_versions fv ON fv.id = f.current_version_id
                        WHERE f.repository_id = ? AND f.repo_path = ? AND f.is_deleted = 0
                        """,
                        (repo_id, dep.repo_path),
                    ).fetchone()
                    if dep_file_row is None or dep_file_row["current_version_id"] is None:
                        has_incomplete_deps = True
                        dep_hashes.append(f"unindexed:{dep.repo_path}")
                        resolved_deps.append((dep, None, None, None))
                    else:
                        d_fid = int(dep_file_row["file_id"])
                        d_fvid = int(dep_file_row["current_version_id"])
                        d_sha = str(dep_file_row["content_sha256"])
                        dep_hashes.append(f"dep:{dep.repo_path}:{d_sha}")
                        inc_from_id = (
                            self._get_file_id_by_path(repo_id, dep.included_from_path)
                            if dep.included_from_path
                            else None
                        )
                        resolved_deps.append((dep, d_fid, d_fvid, inc_from_id))

            dep_fingerprint = hashlib.sha256(
                "\n".join(sorted(dep_hashes)).encode("utf-8")
            ).hexdigest()
            job_fingerprint = hashlib.sha256(
                json.dumps(
                    {
                        "tu_key": payload.tu_key,
                        "flags_fingerprint": str(tu_row["flags_fingerprint"]),
                        "dependency_fingerprint": dep_fingerprint,
                        "parser_name": payload.parser_name,
                        "parser_version": payload.parser_version,
                        "mfc_rule_version": payload.mfc_rule_version,
                    },
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()

            cur_run = self._conn.execute(
                """
                INSERT INTO parse_runs(
                    generation_id, translation_unit_id, main_file_version_id,
                    parser_name, parser_version, mfc_rule_version,
                    job_fingerprint, dependency_fingerprint,
                    has_incomplete_dependencies, parse_status, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    generation_id,
                    tu_id,
                    main_file_ver_id,
                    payload.parser_name,
                    payload.parser_version,
                    payload.mfc_rule_version,
                    job_fingerprint,
                    dep_fingerprint,
                    1 if has_incomplete_deps else 0,
                    payload.parse_status.value,
                    now,
                ),
            )
            parse_run_id = int(cur_run.lastrowid)

            # Always record main source in tu_dependencies
            self._conn.execute(
                """
                INSERT INTO tu_dependencies(
                    parse_run_id, file_id, file_version_id, dependency_kind,
                    included_from_file_id, include_line, is_missing, missing_include_text
                )
                VALUES (?, ?, ?, ?, NULL, NULL, 0, '')
                """,
                (parse_run_id, main_file_id, main_file_ver_id, DependencyKind.MAIN_SOURCE.value),
            )
            for dep, d_fid, d_fvid, inc_from_id in resolved_deps:
                self._conn.execute(
                    """
                    INSERT INTO tu_dependencies(
                        parse_run_id, file_id, file_version_id, dependency_kind,
                        included_from_file_id, include_line, is_missing, missing_include_text
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        parse_run_id,
                        d_fid,
                        d_fvid,
                        dep.dependency_kind.value,
                        inc_from_id,
                        dep.include_line,
                        1 if dep.is_missing else 0,
                        dep.missing_include_text,
                    ),
                )

            # Upsert entities
            entity_id_by_sem_key: dict[str, int] = {}
            for ent in payload.entities:
                ent_id = self._upsert_entity(repo_id, ent)
                entity_id_by_sem_key[ent.semantic_key] = ent_id

            # Insert occurrences
            for occ in payload.occurrences:
                ent_id = self._resolve_entity_id(repo_id, entity_id_by_sem_key, occ.entity_semantic_key)
                parent_id = (
                    self._resolve_entity_id(repo_id, entity_id_by_sem_key, occ.parent_semantic_key)
                    if occ.parent_semantic_key
                    else None
                )
                fvid = self._require_current_file_version_id(repo_id, occ.span.repo_path)
                self._conn.execute(
                    """
                    INSERT INTO symbol_occurrences(
                        parse_run_id, entity_id, file_version_id, occurrence_kind,
                        signature, parent_entity_id, byte_start, byte_end,
                        start_line, start_col, end_line, end_col,
                        condition_state, extraction_quality
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        parse_run_id,
                        ent_id,
                        fvid,
                        occ.occurrence_kind.value,
                        occ.signature,
                        parent_id,
                        occ.span.byte_start,
                        occ.span.byte_end,
                        occ.span.start_line,
                        occ.span.start_col,
                        occ.span.end_line,
                        occ.span.end_col,
                        occ.condition_state.value,
                        occ.extraction_quality.value,
                    ),
                )

            # Insert observations
            obs_id_by_key: dict[str, int] = {}
            for obs in payload.observations:
                subj_id = (
                    self._resolve_entity_id(repo_id, entity_id_by_sem_key, obs.subject_semantic_key)
                    if obs.subject_semantic_key
                    else None
                )
                fvid = self._require_current_file_version_id(repo_id, obs.span.repo_path)
                cur_obs = self._conn.execute(
                    """
                    INSERT INTO observations(
                        parse_run_id, observation_key, observation_type, subject_entity_id,
                        file_version_id, byte_start, byte_end, start_line, start_col,
                        end_line, end_col, condition_state, extraction_quality,
                        rule_id, parser_name, parser_version, details_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        parse_run_id,
                        obs.observation_key,
                        obs.observation_type,
                        subj_id,
                        fvid,
                        obs.span.byte_start,
                        obs.span.byte_end,
                        obs.span.start_line,
                        obs.span.start_col,
                        obs.span.end_line,
                        obs.span.end_col,
                        obs.condition_state.value,
                        obs.extraction_quality.value,
                        obs.rule_id,
                        obs.parser_name,
                        obs.parser_version,
                        json.dumps(obs.details, sort_keys=True),
                    ),
                )
                obs_id_by_key[obs.observation_key] = int(cur_obs.lastrowid)

            # Insert relations + evidence + candidates
            for rel in payload.relations:
                src_id = self._resolve_entity_id(repo_id, entity_id_by_sem_key, rel.source_semantic_key)
                tgt_id = (
                    self._resolve_entity_id(repo_id, entity_id_by_sem_key, rel.target_semantic_key)
                    if rel.target_semantic_key
                    else None
                )
                reason_val = (
                    rel.reason.value if isinstance(rel.reason, ResolutionReason) else str(rel.reason)
                )
                cur_rel = self._conn.execute(
                    """
                    INSERT INTO relations(
                        parse_run_id, build_context_id, source_entity_id, target_entity_id,
                        kind, resolution_status, dispatch_kind, extraction_quality,
                        condition_state, reason, unresolved_target_expr
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        parse_run_id,
                        build_context_id,
                        src_id,
                        tgt_id,
                        rel.kind.value,
                        rel.resolution_status.value,
                        rel.dispatch_kind.value,
                        rel.extraction_quality.value,
                        rel.condition_state.value,
                        reason_val,
                        rel.unresolved_target_expr,
                    ),
                )
                rel_id = int(cur_rel.lastrowid)

                for ev in rel.evidence:
                    if ev.observation_key not in obs_id_by_key:
                        raise KeyError(
                            f"Relation evidence references unknown observation_key '{ev.observation_key}' in parse run."
                        )
                    obs_id = obs_id_by_key[ev.observation_key]
                    fvid = self._require_current_file_version_id(repo_id, ev.span.repo_path)
                    self._conn.execute(
                        """
                        INSERT INTO relation_evidence(
                            relation_id, observation_id, file_version_id,
                            byte_start, byte_end, start_line, start_col, end_line, end_col,
                            evidence_snippet_hash, rule_id, parser_name, parser_version, notes
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            rel_id,
                            obs_id,
                            fvid,
                            ev.span.byte_start,
                            ev.span.byte_end,
                            ev.span.start_line,
                            ev.span.start_col,
                            ev.span.end_line,
                            ev.span.end_col,
                            ev.evidence_snippet_hash,
                            ev.rule_id,
                            ev.parser_name,
                            ev.parser_version,
                            ev.notes,
                        ),
                    )

                for cand in rel.candidates:
                    cand_ent_id = self._resolve_entity_id(
                        repo_id, entity_id_by_sem_key, cand.candidate_semantic_key
                    )
                    ev_obs_id = (
                        obs_id_by_key[cand.evidence_observation_key]
                        if cand.evidence_observation_key
                        else None
                    )
                    self._conn.execute(
                        """
                        INSERT INTO relation_candidates(
                            relation_id, candidate_entity_id, candidate_reason, evidence_observation_id
                        )
                        VALUES (?, ?, ?, ?)
                        """,
                        (rel_id, cand_ent_id, cand.candidate_reason, ev_obs_id),
                    )

            # Insert diagnostics
            for diag in payload.diagnostics:
                fvid = (
                    self._require_current_file_version_id(repo_id, diag.span.repo_path)
                    if diag.span is not None
                    else None
                )
                self._conn.execute(
                    """
                    INSERT INTO diagnostics(
                        parse_run_id, file_version_id, severity, code, message,
                        parse_scope, byte_start, byte_end, start_line, start_col,
                        end_line, end_col, missing_dependency, unsupported_feature
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        parse_run_id,
                        fvid,
                        diag.severity.value,
                        diag.code,
                        diag.message,
                        diag.parse_scope,
                        diag.span.byte_start if diag.span else None,
                        diag.span.byte_end if diag.span else None,
                        diag.span.start_line if diag.span else None,
                        diag.span.start_col if diag.span else None,
                        diag.span.end_line if diag.span else None,
                        diag.span.end_col if diag.span else None,
                        diag.missing_dependency,
                        diag.unsupported_feature,
                    ),
                )

            # Keep recovered/error runs for diagnostics, but only clean parses
            # participate in the current verified graph.
            if payload.parse_status != ParseStatus.PARSED_CLEAN:
                self._conn.execute(
                    "DELETE FROM generation_tu_runs WHERE generation_id = ? AND translation_unit_id = ?",
                    (generation_id, tu_id),
                )
            else:
                self._conn.execute(
                    """
                    INSERT INTO generation_tu_runs(generation_id, translation_unit_id, parse_run_id)
                    VALUES (?, ?, ?)
                    ON CONFLICT(generation_id, translation_unit_id) DO UPDATE SET
                        parse_run_id = excluded.parse_run_id
                    """,
                    (generation_id, tu_id, parse_run_id),
                )

            return parse_run_id

    def publish_generation(self, generation_id: int) -> None:
        """Atomically publish a staging generation as the current verified generation."""
        self._assert_generation_staging(generation_id)
        now = _utc_now_iso()
        prev_published = self.get_published_generation_id()
        with self._conn:
            if prev_published is not None and prev_published != generation_id:
                self._conn.execute(
                    "UPDATE generations SET status = ? WHERE id = ? AND status = ?",
                    (
                        GenerationStatus.SUPERSEDED.value,
                        prev_published,
                        GenerationStatus.PUBLISHED.value,
                    ),
                )
            self._conn.execute(
                "UPDATE generations SET status = ?, published_at = ? WHERE id = ?",
                (GenerationStatus.PUBLISHED.value, now, generation_id),
            )
            self._set_meta("published_generation_id", str(generation_id))

    def rollback_generation(self, generation_id: int) -> None:
        """Mark a staging generation as rolled back without affecting the published generation."""
        self._assert_generation_staging(generation_id)
        with self._conn:
            self._conn.execute(
                "UPDATE generations SET status = ? WHERE id = ?",
                (GenerationStatus.ROLLED_BACK.value, generation_id),
            )

    def _assert_generation_staging(self, generation_id: int) -> None:
        row = self._conn.execute(
            "SELECT status FROM generations WHERE id = ?",
            (generation_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Generation {generation_id} does not exist.")
        if str(row["status"]) != GenerationStatus.STAGING.value:
            raise ValueError(
                f"Generation {generation_id} has status '{row['status']}', expected 'staging'."
            )

    def _upsert_entity(self, repo_id: int, ent: EntityRecord) -> int:
        self._conn.execute(
            """
            INSERT INTO entities(
                repository_id, semantic_key, kind, name, normalized_name,
                qualified_name, linkage_scope, scope_qualifier, parent_semantic_key, is_external
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(repository_id, semantic_key) DO UPDATE SET
                kind = excluded.kind,
                name = excluded.name,
                normalized_name = excluded.normalized_name,
                qualified_name = excluded.qualified_name,
                linkage_scope = excluded.linkage_scope,
                scope_qualifier = excluded.scope_qualifier,
                parent_semantic_key = excluded.parent_semantic_key,
                is_external = excluded.is_external
            """,
            (
                repo_id,
                ent.semantic_key,
                ent.kind.value,
                ent.name,
                ent.name.lower(),
                ent.qualified_name,
                ent.linkage_scope.value,
                ent.scope_qualifier,
                ent.parent_semantic_key,
                1 if ent.is_external else 0,
            ),
        )
        row = self._conn.execute(
            "SELECT id FROM entities WHERE repository_id = ? AND semantic_key = ?",
            (repo_id, ent.semantic_key),
        ).fetchone()
        return int(row["id"])

    def _resolve_entity_id(
        self, repo_id: int, cache: dict[str, int], semantic_key: str
    ) -> int:
        if semantic_key in cache:
            return cache[semantic_key]
        row = self._conn.execute(
            "SELECT id FROM entities WHERE repository_id = ? AND semantic_key = ?",
            (repo_id, semantic_key),
        ).fetchone()
        if row is None:
            raise KeyError(f"Entity semantic_key '{semantic_key}' not registered.")
        ent_id = int(row["id"])
        cache[semantic_key] = ent_id
        return ent_id

    def _get_file_id_by_path(self, repo_id: int, repo_path: str) -> int | None:
        row = self._conn.execute(
            "SELECT id FROM files WHERE repository_id = ? AND repo_path = ?",
            (repo_id, repo_path),
        ).fetchone()
        return int(row["id"]) if row is not None else None

    def _require_current_file_version_id(self, repo_id: int, repo_path: str) -> int:
        row = self._conn.execute(
            "SELECT current_version_id FROM files WHERE repository_id = ? AND repo_path = ? AND is_deleted = 0",
            (repo_id, repo_path),
        ).fetchone()
        if row is None or row["current_version_id"] is None:
            raise KeyError(f"Active file version not found for '{repo_path}'.")
        return int(row["current_version_id"])

    def compute_invalidated_translation_units(
        self,
        *,
        generation_id: int | None = None,
        parser_version: str = "0.1.0",
        mfc_rule_version: str = "0.1.0",
    ) -> list[dict[str, Any]]:
        """Determine which active translation units require re-indexing relative to `generation_id` (or published generation).

        Handles:
        - Never-indexed TUs
        - Modified or deleted main source files
        - Modified or deleted transitive headers (`tu_dependencies`)
        - Changed build context or per-file flags (`flags_fingerprint`)
        - Changed parser/rule versions
        - TUs with incomplete dependencies when any header in the repository is added or modified
        """
        target_gen = (
            generation_id if generation_id is not None else self.get_published_generation_id()
        )
        repo_id = self.get_active_repository_id()

        tu_rows = self._conn.execute(
            """
            SELECT tu.id AS tu_id, tu.tu_key, tu.flags_fingerprint, tu.build_context_id,
                   bc.context_key, f.id AS main_file_id, f.repo_path AS main_repo_path,
                   f.is_deleted AS main_is_deleted, f.current_version_id AS main_current_version_id
            FROM translation_units tu
            JOIN build_contexts bc ON bc.id = tu.build_context_id
            JOIN files f ON f.id = tu.main_file_id
            WHERE bc.project_id IN (SELECT id FROM projects WHERE repository_id = ?)
              AND tu.is_active = 1
            ORDER BY tu.tu_key
            """,
            (repo_id,),
        ).fetchall()

        if target_gen is None:
            return [
                {
                    "tu_key": str(r["tu_key"]),
                    "context_key": str(r["context_key"]),
                    "main_repo_path": str(r["main_repo_path"]),
                    "reasons": ["no_published_generation"],
                }
                for r in tu_rows
            ]

        # Check if any header in the repository has changed since target_gen or been newly added
        any_header_changed = self._has_any_header_change_since_generation(repo_id, target_gen)

        invalidated: list[dict[str, Any]] = []
        for r in tu_rows:
            tu_id = int(r["tu_id"])
            reasons: list[str] = []

            if bool(r["main_is_deleted"]) or r["main_current_version_id"] is None:
                reasons.append("main_file_deleted")
                invalidated.append(
                    {
                        "tu_key": str(r["tu_key"]),
                        "context_key": str(r["context_key"]),
                        "main_repo_path": str(r["main_repo_path"]),
                        "reasons": reasons,
                    }
                )
                continue

            run_row = self._conn.execute(
                """
                SELECT pr.id AS parse_run_id, pr.main_file_version_id,
                       pr.parser_version, pr.mfc_rule_version,
                       pr.job_fingerprint, pr.dependency_fingerprint,
                       pr.has_incomplete_dependencies
                FROM generation_tu_runs gtr
                JOIN parse_runs pr ON pr.id = gtr.parse_run_id
                WHERE gtr.generation_id = ? AND gtr.translation_unit_id = ?
                """,
                (target_gen, tu_id),
            ).fetchone()

            if run_row is None:
                reasons.append("unindexed_in_generation")
            else:
                parse_run_id = int(run_row["parse_run_id"])
                if int(run_row["main_file_version_id"]) != int(r["main_current_version_id"]):
                    reasons.append("main_file_modified")
                if str(run_row["parser_version"]) != parser_version:
                    reasons.append("parser_version_changed")
                if str(run_row["mfc_rule_version"]) != mfc_rule_version:
                    reasons.append("mfc_rule_version_changed")

                # Check consumed dependencies
                dep_rows = self._conn.execute(
                    """
                    SELECT td.file_id, td.file_version_id, td.is_missing,
                           f.is_deleted, f.current_version_id, f.repo_path
                    FROM tu_dependencies td
                    LEFT JOIN files f ON f.id = td.file_id
                    WHERE td.parse_run_id = ?
                    """,
                    (parse_run_id,),
                ).fetchall()
                for d in dep_rows:
                    if bool(d["is_missing"]):
                        continue
                    if d["file_id"] is None:
                        continue
                    if bool(d["is_deleted"]):
                        reasons.append(f"dependency_deleted:{d['repo_path']}")
                    elif int(d["current_version_id"] or -1) != int(d["file_version_id"] or -2):
                        reasons.append(f"dependency_modified:{d['repo_path']}")

                # Check if flags fingerprint changed by recomputing expected job fingerprint
                expected_job_fp = hashlib.sha256(
                    json.dumps(
                        {
                            "tu_key": str(r["tu_key"]),
                            "flags_fingerprint": str(r["flags_fingerprint"]),
                            "dependency_fingerprint": str(run_row["dependency_fingerprint"]),
                            "parser_name": "core",
                            "parser_version": str(run_row["parser_version"]),
                            "mfc_rule_version": str(run_row["mfc_rule_version"]),
                        },
                        sort_keys=True,
                    ).encode("utf-8")
                ).hexdigest()
                # Also check against the actual parser_name stored on parse_runs
                actual_parser_row = self._conn.execute(
                    "SELECT parser_name FROM parse_runs WHERE id = ?",
                    (parse_run_id,),
                ).fetchone()
                actual_parser_name = str(actual_parser_row["parser_name"])
                expected_job_fp = hashlib.sha256(
                    json.dumps(
                        {
                            "tu_key": str(r["tu_key"]),
                            "flags_fingerprint": str(r["flags_fingerprint"]),
                            "dependency_fingerprint": str(run_row["dependency_fingerprint"]),
                            "parser_name": actual_parser_name,
                            "parser_version": str(run_row["parser_version"]),
                            "mfc_rule_version": str(run_row["mfc_rule_version"]),
                        },
                        sort_keys=True,
                    ).encode("utf-8")
                ).hexdigest()
                if expected_job_fp != str(run_row["job_fingerprint"]):
                    reasons.append("build_flags_changed")

                if bool(run_row["has_incomplete_dependencies"]) and any_header_changed:
                    reasons.append("incomplete_dependencies_conservative_invalidation")

            if reasons:
                invalidated.append(
                    {
                        "tu_key": str(r["tu_key"]),
                        "context_key": str(r["context_key"]),
                        "main_repo_path": str(r["main_repo_path"]),
                        "reasons": sorted(set(reasons)),
                    }
                )
        return invalidated

    def _has_any_header_change_since_generation(self, repo_id: int, generation_id: int) -> bool:
        """Check if any header file in the repo was added, deleted, or modified compared to dependencies in `generation_id`."""
        gen_row = self._conn.execute(
            "SELECT created_at FROM generations WHERE id = ?",
            (generation_id,),
        ).fetchone()
        if gen_row is None:
            return True
        gen_created = str(gen_row["created_at"])
        changed_header = self._conn.execute(
            """
            SELECT 1
            FROM files f
            LEFT JOIN file_versions fv ON fv.id = f.current_version_id
            WHERE f.repository_id = ?
              AND f.file_role IN ('header', 'inline_header')
              AND (f.is_deleted = 1 OR fv.recorded_at > ?)
            LIMIT 1
            """,
            (repo_id, gen_created),
        ).fetchone()
        return changed_header is not None

    def query_published_entities(
        self,
        *,
        context_key: str | None = None,
        name_prefix: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Query entities that have occurrences in the currently published generation."""
        pub_gen = self.get_published_generation_id()
        if pub_gen is None:
            return []

        params: list[Any] = [pub_gen]
        where_clauses = ["gtr.generation_id = ?"]
        if context_key is not None:
            where_clauses.append("bc.context_key = ?")
            params.append(context_key)
        if name_prefix is not None:
            where_clauses.append("e.normalized_name LIKE ?")
            params.append(f"{name_prefix.lower()}%")

        params.append(limit)
        sql = f"""
            SELECT DISTINCT
                e.semantic_key, e.kind, e.name, e.qualified_name,
                e.linkage_scope, e.scope_qualifier, bc.context_key
            FROM generation_tu_runs gtr
            JOIN translation_units tu ON tu.id = gtr.translation_unit_id
            JOIN build_contexts bc ON bc.id = tu.build_context_id
            JOIN symbol_occurrences so ON so.parse_run_id = gtr.parse_run_id
            JOIN entities e ON e.id = so.entity_id
            WHERE {' AND '.join(where_clauses)}
            ORDER BY e.qualified_name, e.semantic_key, bc.context_key
            LIMIT ?
        """
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def query_published_relations(
        self,
        *,
        context_key: str,
        source_semantic_key: str | None = None,
        target_semantic_key: str | None = None,
        kind: RelationKind | None = None,
        resolution_status: ResolutionStatus | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Query relations active in the published generation for a specific build context (strictly isolated by context)."""
        pub_gen = self.get_published_generation_id()
        if pub_gen is None:
            return []

        params: list[Any] = [pub_gen, context_key]
        where_clauses = ["gtr.generation_id = ?", "bc.context_key = ?"]

        if source_semantic_key is not None:
            where_clauses.append("src.semantic_key = ?")
            params.append(source_semantic_key)
        if target_semantic_key is not None:
            where_clauses.append("tgt.semantic_key = ?")
            params.append(target_semantic_key)
        if kind is not None:
            where_clauses.append("r.kind = ?")
            params.append(kind.value)
        if resolution_status is not None:
            where_clauses.append("r.resolution_status = ?")
            params.append(resolution_status.value)

        params.append(limit)
        sql = f"""
            SELECT
                r.id AS relation_id,
                bc.context_key,
                src.semantic_key AS source_semantic_key,
                tgt.semantic_key AS target_semantic_key,
                r.kind,
                r.resolution_status,
                r.dispatch_kind,
                r.extraction_quality,
                r.condition_state,
                r.reason,
                r.unresolved_target_expr,
                COUNT(re.id) AS evidence_count
            FROM generation_tu_runs gtr
            JOIN translation_units tu ON tu.id = gtr.translation_unit_id
            JOIN build_contexts bc ON bc.id = tu.build_context_id
            JOIN relations r ON r.parse_run_id = gtr.parse_run_id AND r.build_context_id = bc.id
            JOIN entities src ON src.id = r.source_entity_id
            LEFT JOIN entities tgt ON tgt.id = r.target_entity_id
            LEFT JOIN relation_evidence re ON re.relation_id = r.id
            WHERE {' AND '.join(where_clauses)}
            GROUP BY r.id
            ORDER BY src.semantic_key, r.kind, tgt.semantic_key, r.id
            LIMIT ?
        """
        rows = self._conn.execute(sql, params).fetchall()
        results: list[dict[str, Any]] = []
        for r in rows:
            item = dict(r)
            cand_rows = self._conn.execute(
                """
                SELECT e.semantic_key AS candidate_semantic_key, rc.candidate_reason
                FROM relation_candidates rc
                JOIN entities e ON e.id = rc.candidate_entity_id
                WHERE rc.relation_id = ?
                ORDER BY e.semantic_key
                """,
                (int(r["relation_id"]),),
            ).fetchall()
            item["candidates"] = [dict(c) for c in cand_rows]
            results.append(item)
        return results

    def get_status_summary(self) -> dict[str, Any]:
        """Return a structured status summary of the database, files, contexts, TUs, and generations."""
        repo_id = self.get_active_repository_id()
        repo_row = self._conn.execute(
            "SELECT repo_root, source_revision, updated_at FROM repositories WHERE id = ?",
            (repo_id,),
        ).fetchone()
        active_files = self._conn.execute(
            "SELECT COUNT(*) AS cnt, COALESCE(SUM(fv.size_bytes), 0) AS total_bytes, COALESCE(SUM(fv.line_count), 0) AS total_lines "
            "FROM files f LEFT JOIN file_versions fv ON fv.id = f.current_version_id "
            "WHERE f.repository_id = ? AND f.is_deleted = 0",
            (repo_id,),
        ).fetchone()
        deleted_files = self._conn.execute(
            "SELECT COUNT(*) AS cnt FROM files WHERE repository_id = ? AND is_deleted = 1",
            (repo_id,),
        ).fetchone()
        contexts_cnt = self._conn.execute(
            "SELECT COUNT(*) AS cnt FROM build_contexts bc JOIN projects p ON p.id = bc.project_id WHERE p.repository_id = ?",
            (repo_id,),
        ).fetchone()
        active_tus = self._conn.execute(
            "SELECT COUNT(*) AS cnt FROM translation_units tu JOIN build_contexts bc ON bc.id = tu.build_context_id JOIN projects p ON p.id = bc.project_id WHERE p.repository_id = ? AND tu.is_active = 1",
            (repo_id,),
        ).fetchone()

        pub_gen_id = self.get_published_generation_id()
        pub_gen_info: dict[str, Any] | None = None
        if pub_gen_id is not None:
            g_row = self._conn.execute(
                "SELECT id, status, parser_version, mfc_rule_version, created_at, published_at FROM generations WHERE id = ?",
                (pub_gen_id,),
            ).fetchone()
            if g_row is not None:
                active_runs_cnt = self._conn.execute(
                    "SELECT COUNT(*) AS cnt FROM generation_tu_runs WHERE generation_id = ?",
                    (pub_gen_id,),
                ).fetchone()
                pub_gen_info = {
                    **dict(g_row),
                    "verified_tu_runs": int(active_runs_cnt["cnt"]),
                }

        return {
            "schema_version": int(self._get_meta("schema_version") or SCHEMA_VERSION),
            "tool_version": self._get_meta("tool_version") or __version__,
            "db_path": str(self.db_path),
            "repo_root": str(repo_row["repo_root"]),
            "source_revision": str(repo_row["source_revision"]),
            "active_files": int(active_files["cnt"]),
            "deleted_files": int(deleted_files["cnt"]),
            "total_source_bytes": int(active_files["total_bytes"]),
            "total_source_lines": int(active_files["total_lines"]),
            "build_contexts": int(contexts_cnt["cnt"]),
            "active_translation_units": int(active_tus["cnt"]),
            "published_generation": pub_gen_info,
        }
