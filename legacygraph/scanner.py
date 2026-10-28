"""Incremental semantic indexing orchestrator for LegacyGraph (`legacygraph.scanner`)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Iterable

from .build_context import load_build_manifest
from .clang_parser import (
    CLANG_PARSER_NAME,
    CLANG_PARSER_VERSION,
    DEFAULT_MFC_RULE_VERSION,
    ClangParser,
)
from .discovery import discover_repository_files, inspect_source_file, resolve_repo_root
from .index import IndexStore
from .models import FileInventoryRecord, ParseRunPayload, ParseStatus, TranslationUnitSpec

SCANNER_VERSION = CLANG_PARSER_VERSION


def _deactivate_unlisted_translation_units(
    store: IndexStore, active_tu_keys: set[str]
) -> list[str]:
    """Ensure only translation units present in the authoritative manifest remain active across the repository."""
    repo_id = store.get_active_repository_id()
    deactivated: list[str] = []
    with store.atomic_update():
        rows = store._conn.execute(
            """
            SELECT tu.id, tu.tu_key
            FROM translation_units tu
            JOIN build_contexts bc ON bc.id = tu.build_context_id
            JOIN projects p ON p.id = bc.project_id
            WHERE p.repository_id = ? AND tu.is_active = 1
            ORDER BY tu.tu_key
            """,
            (repo_id,),
        ).fetchall()
        for r in rows:
            tkey = str(r["tu_key"])
            if tkey not in active_tu_keys:
                store._conn.execute(
                    "UPDATE translation_units SET is_active = 0 WHERE id = ?",
                    (int(r["id"]),),
                )
                deactivated.append(tkey)
    return deactivated


def _get_tracked_dependency_files(
    store: IndexStore,
    repo_root: Path,
    *,
    preferred_legacy_encoding: str = "cp1252",
) -> list[FileInventoryRecord]:
    """Re-inspect any non-default-extension files already tracked in `files` so sync_file_inventory does not mark them deleted."""
    repo_id = store.get_active_repository_id()
    rows = store._conn.execute(
        "SELECT repo_path FROM files WHERE repository_id = ? AND is_deleted = 0",
        (repo_id,),
    ).fetchall()
    extra: list[FileInventoryRecord] = []
    for r in rows:
        rel_p = str(r["repo_path"])
        try:
            rec = inspect_source_file(
                repo_root,
                rel_p,
                preferred_legacy_encoding=preferred_legacy_encoding,
            )
            extra.append(rec)
        except Exception:
            continue
    return extra


def _ensure_consumed_files_synced(
    store: IndexStore,
    repo_root: Path,
    payload: ParseRunPayload,
    *,
    preferred_legacy_encoding: str = "cp1252",
) -> list[str]:
    """Ensure any repo-contained files consumed by a parse run (including non-standard extensions) have synced file_versions."""
    candidate_paths: set[str] = set()
    for dep in payload.dependencies:
        if not dep.is_missing and dep.repo_path:
            candidate_paths.add(dep.repo_path)
        if dep.included_from_path:
            candidate_paths.add(dep.included_from_path)
    for occ in payload.occurrences:
        candidate_paths.add(occ.span.repo_path)
    for obs in payload.observations:
        candidate_paths.add(obs.span.repo_path)
    for rel in payload.relations:
        for ev in rel.evidence:
            candidate_paths.add(ev.span.repo_path)
    for diag in payload.diagnostics:
        if diag.span is not None:
            candidate_paths.add(diag.span.repo_path)

    repo_id = store.get_active_repository_id()
    records_to_sync: list[FileInventoryRecord] = []
    for rel_p in sorted(candidate_paths):
        if not rel_p or rel_p == ".":
            continue
        row = store._conn.execute(
            "SELECT current_version_id, is_deleted FROM files WHERE repository_id = ? AND repo_path = ?",
            (repo_id, rel_p),
        ).fetchone()
        if row is None or row["current_version_id"] is None or bool(row["is_deleted"]):
            try:
                rec = inspect_source_file(
                    repo_root,
                    rel_p,
                    preferred_legacy_encoding=preferred_legacy_encoding,
                )
                records_to_sync.append(rec)
            except Exception:
                continue

    if records_to_sync:
        store.sync_file_inventory(records_to_sync, mark_missing_deleted=False)
    return [r.repo_path for r in records_to_sync]


def scan_repository(
    repo_root: str | Path,
    db_path: str | Path,
    manifest_path: str | Path,
    *,
    force: bool = False,
    source_revision: str = "",
    exclude_dirs: Iterable[str] = (),
    preferred_legacy_encoding: str = "cp1252",
    parser: ClangParser | None = None,
    parser_version: str = CLANG_PARSER_VERSION,
    mfc_rule_version: str = DEFAULT_MFC_RULE_VERSION,
    on_before_publish: Callable[[IndexStore, int], None] | None = None,
) -> dict[str, Any]:
    """Run incremental or full semantic indexing for a repository and atomically publish a verified generation.

    Steps:
    1. Discover repository files and sync content-hash versions.
    2. Import the authoritative build manifest / compile_commands.json and deactivate removed TUs across all contexts.
    3. Compute invalidated TUs (or select all active TUs when `force=True`).
    4. Stage a new generation, parse only invalidated TUs, and record clean vs failed/error runs (retracting failed current TUs from the verified graph while surfacing diagnostics).
    5. Atomically publish the staging generation; roll back on any unexpected exception so the previous published snapshot remains intact.
    """
    canonical_root = resolve_repo_root(repo_root)
    resolved_db = Path(db_path)
    if not resolved_db.is_absolute():
        resolved_db = (Path.cwd() / resolved_db).resolve(strict=False)
    else:
        resolved_db = resolved_db.resolve(strict=False)

    resolved_manifest = Path(manifest_path)
    if not resolved_manifest.is_absolute():
        resolved_manifest = (Path.cwd() / resolved_manifest).resolve(strict=True)
    else:
        resolved_manifest = resolved_manifest.resolve(strict=True)

    output_exclusions: list[Path] = [resolved_db]
    try:
        rel_man = resolved_manifest.relative_to(canonical_root)
        if rel_man.parts:
            output_exclusions.append(resolved_manifest)
    except ValueError:
        pass

    discovery = discover_repository_files(
        canonical_root,
        output_paths_to_exclude=output_exclusions,
        extra_excluded_dir_names=exclude_dirs,
        preferred_legacy_encoding=preferred_legacy_encoding,
    )
    imported = load_build_manifest(canonical_root, resolved_manifest)

    # Also inspect any main source files or forced includes referenced by the manifest that might have non-default extensions
    discovered_map: dict[str, FileInventoryRecord] = {f.repo_path: f for f in discovery.files}
    for spec in imported.translation_units:
        if spec.main_file_path not in discovered_map:
            try:
                rec = inspect_source_file(
                    canonical_root,
                    spec.main_file_path,
                    preferred_legacy_encoding=preferred_legacy_encoding,
                )
                discovered_map[rec.repo_path] = rec
            except Exception:
                pass

    with IndexStore(resolved_db) as store:
        store.initialize_schema(canonical_root, source_revision=source_revision)
        with store.atomic_update():
            for rec in _get_tracked_dependency_files(
                store, canonical_root, preferred_legacy_encoding=preferred_legacy_encoding
            ):
                if rec.repo_path not in discovered_map:
                    discovered_map[rec.repo_path] = rec
            sync_summary = store.sync_file_inventory(
                discovered_map.values(), mark_missing_deleted=True
            )

            for ctx in imported.contexts:
                store.register_build_context(ctx)
            active_specs_by_key: dict[str, TranslationUnitSpec] = {
                spec.tu_key: spec for spec in imported.translation_units
            }
            store.register_translation_units(
                imported.translation_units,
                deactivate_unlisted_for_contexts=False,
            )
            deactivated_tu_keys = _deactivate_unlisted_translation_units(
                store, set(active_specs_by_key.keys())
            )

            eff_parser_version = parser.parser_version if parser is not None else parser_version
            eff_mfc_version = parser.mfc_rule_version if parser is not None else mfc_rule_version

            invalidated_items = store.compute_invalidated_translation_units(
                parser_version=eff_parser_version,
                mfc_rule_version=eff_mfc_version,
            )
            invalidated_by_key = {item["tu_key"]: item for item in invalidated_items}

            # A newly added header can shadow an unchanged consumed header in
            # an earlier include directory. Reverse dependency hashes alone
            # cannot detect that change in include resolution.
            added_headers = [path for path in sync_summary["added"]
                             if discovered_map[path].file_role in ("header", "inline_header")]
            if added_headers:
                for key, spec in active_specs_by_key.items():
                    invalidated_by_key.setdefault(key, {
                        "tu_key": key, "context_key": spec.build_context.context_key,
                        "main_repo_path": spec.main_file_path,
                        "reasons": ["include_search_changed:new_header"],
                    })
                invalidated_items = [invalidated_by_key[key] for key in sorted(invalidated_by_key)]

            prev_published_id = store.get_published_generation_id()
            if force:
                tus_to_parse = [
                    spec for _, spec in sorted(active_specs_by_key.items(), key=lambda kv: kv[0])
                ]
            else:
                tus_to_parse = [
                    active_specs_by_key[tkey]
                    for tkey in sorted(invalidated_by_key.keys())
                    if tkey in active_specs_by_key
                ]

            # Incremental no-op: if nothing is invalidated, no TUs were deactivated, no files were deleted,
            # and a published generation already exists with matching versions, skip creating a redundant generation.
            if (
                not force
                and prev_published_id is not None
                and not tus_to_parse
                and not deactivated_tu_keys
                and not sync_summary["deleted"]
            ):
                status_after = store.get_status_summary()
                return {
                    "db_path": str(resolved_db),
                    "repo_root": str(canonical_root),
                    "manifest_path": str(resolved_manifest),
                    "parser_name": parser.parser_name if parser is not None else CLANG_PARSER_NAME,
                    "parser_version": eff_parser_version,
                    "mfc_rule_version": eff_mfc_version,
                    "generation_id": prev_published_id,
                    "previous_generation_id": prev_published_id,
                    "noop": True,
                    "discovered_files": len(discovered_map),
                    "sync": sync_summary,
                    "active_contexts": len(imported.contexts),
                    "active_translation_units": len(active_specs_by_key),
                    "deactivated_translation_units": deactivated_tu_keys,
                    "invalidated_translation_units": invalidated_items,
                    "parsed_translation_units": 0,
                    "reused_translation_units": len(active_specs_by_key),
                    "clean_translation_units": 0,
                    "failed_translation_units": 0,
                    "tu_results": [],
                    "diagnostics": [],
                    "warnings": list(imported.warnings),
                    "published_generation": status_after["published_generation"],
                }

            gen_id = store.begin_generation(
                parser_version=eff_parser_version,
                mfc_rule_version=eff_mfc_version,
                notes="scan_repository",
                inherit_from_published=True,
            )
            try:
                active_parser = (
                    parser
                    if parser is not None
                    else ClangParser(
                        canonical_root,
                        parser_version=eff_parser_version,
                        mfc_rule_version=eff_mfc_version,
                    )
                )
                tu_results: list[dict[str, Any]] = []
                surfaced_diagnostics: list[dict[str, Any]] = []
                clean_count = 0
                failed_count = 0

                for spec in tus_to_parse:
                    inv_info = invalidated_by_key.get(spec.tu_key)
                    reasons = inv_info["reasons"] if inv_info else (["forced"] if force else [])

                    if "main_file_deleted" in reasons:
                        store.retract_translation_unit_from_generation(gen_id, spec.tu_key)
                        failed_count += 1
                        diag_entry = {
                            "tu_key": spec.tu_key,
                            "main_file_path": spec.main_file_path,
                            "severity": "fatal",
                            "code": "scanner.main_file_deleted",
                            "message": f"Main source file '{spec.main_file_path}' is missing or deleted.",
                            "repo_path": spec.main_file_path,
                            "line": None,
                            "missing_dependency": spec.main_file_path,
                            "unsupported_feature": "",
                        }
                        surfaced_diagnostics.append(diag_entry)
                        tu_results.append(
                            {
                                "tu_key": spec.tu_key,
                                "context_key": spec.build_context.context_key,
                                "main_file_path": spec.main_file_path,
                                "parse_status": ParseStatus.FAILED.value,
                                "reasons": reasons,
                                "entity_count": 0,
                                "occurrence_count": 0,
                                "relation_count": 0,
                                "dependency_count": 0,
                                "diagnostic_count": 1,
                            }
                        )
                        continue

                    payload = active_parser.parse(spec)
                    extra_synced = _ensure_consumed_files_synced(
                        store,
                        canonical_root,
                        payload,
                        preferred_legacy_encoding=preferred_legacy_encoding,
                    )
                    for p in extra_synced:
                        if p not in sync_summary["added"] and p not in sync_summary["unchanged"]:
                            sync_summary["added"].append(p)

                    parse_run_id = store.record_parse_run(gen_id, payload)
                    if payload.parse_status == ParseStatus.PARSED_CLEAN:
                        clean_count += 1
                    else:
                        failed_count += 1

                    for d in payload.diagnostics:
                        surfaced_diagnostics.append(
                            {
                                "tu_key": spec.tu_key,
                                "main_file_path": spec.main_file_path,
                                "severity": d.severity.value,
                                "code": d.code,
                                "message": d.message,
                                "repo_path": d.span.repo_path if d.span else None,
                                "line": d.span.start_line if d.span else None,
                                "missing_dependency": d.missing_dependency,
                                "unsupported_feature": d.unsupported_feature,
                            }
                        )

                    tu_results.append(
                        {
                            "tu_key": spec.tu_key,
                            "context_key": spec.build_context.context_key,
                            "main_file_path": spec.main_file_path,
                            "parse_run_id": parse_run_id,
                            "parse_status": payload.parse_status.value,
                            "reasons": reasons,
                            "entity_count": len(payload.entities),
                            "occurrence_count": len(payload.occurrences),
                            "relation_count": len(payload.relations),
                            "dependency_count": len(payload.dependencies),
                            "diagnostic_count": len(payload.diagnostics),
                        }
                    )

                if on_before_publish is not None:
                    on_before_publish(store, gen_id)

                store.publish_generation(gen_id)
            except Exception:
                try:
                    store.rollback_generation(gen_id)
                except Exception:
                    pass
                raise

            status_after = store.get_status_summary()
            reused_count = max(0, len(active_specs_by_key) - len(tus_to_parse))
            return {
                "db_path": str(resolved_db),
                "repo_root": str(canonical_root),
                "manifest_path": str(resolved_manifest),
                "parser_name": active_parser.parser_name,
                "parser_version": eff_parser_version,
                "mfc_rule_version": eff_mfc_version,
                "generation_id": gen_id,
                "previous_generation_id": prev_published_id,
                "noop": False,
                "discovered_files": len(discovered_map),
                "sync": sync_summary,
                "active_contexts": len(imported.contexts),
                "active_translation_units": len(active_specs_by_key),
                "deactivated_translation_units": deactivated_tu_keys,
                "invalidated_translation_units": invalidated_items,
                "parsed_translation_units": len(tus_to_parse),
                "reused_translation_units": reused_count,
                "clean_translation_units": clean_count,
                "failed_translation_units": failed_count,
                "tu_results": tu_results,
                "diagnostics": surfaced_diagnostics,
                "warnings": list(imported.warnings),
                "published_generation": status_after["published_generation"],
            }
