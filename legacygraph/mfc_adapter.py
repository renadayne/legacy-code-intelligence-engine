"""Adapter bridging standalone MFC/resource scanner output into ParseRunPayload models."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
from typing import Any

from .discovery import (
    PathContainmentError,
    compute_line_byte_offsets,
    detect_encoding_and_decode,
    ensure_contained_path,
    resolve_repo_root,
)
from .mfc import MFC_RULE_VERSION, scan_mfc, scan_resources
from .models import (
    CandidateTargetRecord,
    ConditionState,
    DependencyKind,
    DiagnosticRecord,
    DiagnosticSeverity,
    DispatchKind,
    EntityKind,
    EntityRecord,
    ExtractionQuality,
    LinkageScope,
    ObservationRecord,
    OccurrenceKind,
    ParseRunPayload,
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

_CPP_SCAN_EXTENSIONS = frozenset({".c", ".cc", ".cpp", ".cxx", ".c++", ".h", ".hh", ".hpp", ".hxx", ".inl", ".ipp", ".inc", ".tpp", ".txx", ".tlh", ".tli"})
_RC_EXTENSIONS = frozenset({".rc", ".rc2", ".dlg"})
_HEADER_EXTENSIONS = frozenset({".h", ".hh", ".hpp", ".hxx"})


def _is_resource_file(rel_posix: str) -> bool:
    p = Path(rel_posix)
    ext = p.suffix.lower()
    return ext in _RC_EXTENSIONS or (ext in _HEADER_EXTENSIONS and "resource" in p.stem.lower())


def _make_span(
    rel_posix: str, line_offsets: tuple[int, ...], start_byte: int, end_byte: int, start_line: int, end_line: int
) -> SourceSpan:
    s_line = max(1, min(start_line, len(line_offsets)))
    e_line = max(s_line, min(end_line, len(line_offsets)))
    s_col = max(1, start_byte - line_offsets[s_line - 1] + 1)
    e_col = max(1, end_byte - line_offsets[e_line - 1] + 1)
    return SourceSpan(rel_posix, start_byte, max(start_byte, end_byte), s_line, s_col, e_line, e_col)


def _normalize_macro_offsets(
    canonical_root: Path, active_macro_offsets: dict[str, set[int]] | None
) -> dict[str, set[int]] | None:
    if active_macro_offsets is None:
        return None
    normalized: dict[str, set[int]] = {}
    for raw_key, offsets in active_macro_offsets.items():
        try:
            _, rel_posix = ensure_contained_path(canonical_root, raw_key.replace("\\", "/"), allow_nonexistent=True)
            normalized[rel_posix] = {offset for offset in offsets if isinstance(offset, int) and not isinstance(offset, bool) and offset >= 0}
        except (PathContainmentError, OSError, ValueError):
            continue
    return normalized


def _event_identity(context_key: str, item: dict[str, Any], owner_entity_key: str | None) -> EntityRecord:
    owner = item["owner_class"] or ""
    event_kind = item["event_kind"] or "event"
    event_id = item["event_id"]
    event_range = item["event_range"]
    notif = item["notification_code"]
    id_payload = {
        "context_key": context_key,
        "owner_class": owner,
        "event_kind": event_kind,
        "event_id": event_id,
        "event_id_end": item["event_id_end"],
        "event_range": list(event_range) if event_range else None,
        "notification_code": notif,
    }
    digest = hashlib.sha256(json.dumps(id_payload, sort_keys=True, ensure_ascii=True).encode("utf-8")).hexdigest()[:24]
    id_part = f"{event_range[0]}..{event_range[1]}" if event_range else (event_id or item["macro"])
    notif_part = f":{notif}" if notif and notif != event_id else ""
    name = f"{event_kind}{notif_part}:{id_part}"
    qualified_name = f"{owner}::{name}" if owner else name
    return EntityRecord(
        semantic_key=f"mfc:event:{digest}",
        kind=EntityKind.EVENT,
        name=name,
        qualified_name=qualified_name,
        linkage_scope=LinkageScope.CONFIG_SCOPED,
        scope_qualifier=owner,
        parent_semantic_key=owner_entity_key,
    )


def augment_payload(
    payload: ParseRunPayload,
    spec: TranslationUnitSpec,
    repo_root: Path | str,
    active_macro_offsets: dict[str, set[int]] | None = None,
    preferred_legacy_encoding: str = "cp1252",
) -> ParseRunPayload:
    """Augment a semantic TU ParseRunPayload with MFC event registrations and resource facts."""
    canonical_root = resolve_repo_root(repo_root)
    norm_offsets = _normalize_macro_offsets(canonical_root, active_macro_offsets)

    try:
        _, main_rel_posix = ensure_contained_path(canonical_root, spec.main_file_path, allow_nonexistent=True)
    except (PathContainmentError, OSError, ValueError):
        main_rel_posix = ""

    raw_candidates: list[tuple[str, DependencyKind]] = [(spec.main_file_path, DependencyKind.MAIN_SOURCE)]
    for dep in payload.dependencies:
        if not dep.is_missing and dep.repo_path:
            raw_candidates.append((dep.repo_path, dep.dependency_kind))

    consumed_files: list[tuple[Path, str, DependencyKind]] = []
    seen_paths: set[str] = set()
    for raw_path, dep_kind in raw_candidates:
        try:
            resolved_path, rel_posix = ensure_contained_path(canonical_root, raw_path, allow_nonexistent=False)
        except (PathContainmentError, OSError, ValueError):
            continue
        if not resolved_path.is_file() or rel_posix in seen_paths:
            continue
        seen_paths.add(rel_posix)
        if rel_posix == main_rel_posix:
            effective_kind = DependencyKind.MAIN_SOURCE
        elif _is_resource_file(rel_posix) and dep_kind == DependencyKind.DIRECT_INCLUDE:
            effective_kind = DependencyKind.RESOURCE_INCLUDE
        else:
            effective_kind = dep_kind
        consumed_files.append((resolved_path, rel_posix, effective_kind))

    methods_by_qname: dict[str, list[EntityRecord]] = {}
    classes_by_qname: dict[str, list[EntityRecord]] = {}
    for ent in payload.entities:
        if ent.kind == EntityKind.METHOD:
            bucket = methods_by_qname.setdefault(ent.qualified_name, [])
            if all(existing.semantic_key != ent.semantic_key for existing in bucket):
                bucket.append(ent)
        elif ent.kind in (EntityKind.CLASS, EntityKind.STRUCT):
            bucket = classes_by_qname.setdefault(ent.qualified_name, [])
            if all(existing.semantic_key != ent.semantic_key for existing in bucket):
                bucket.append(ent)

    occ_states_by_entity: dict[str, set[ConditionState]] = {}
    for occ in payload.occurrences:
        occ_states_by_entity.setdefault(occ.entity_semantic_key, set()).add(occ.condition_state)

    entities = list(payload.entities)
    occurrences = list(payload.occurrences)
    observations = list(payload.observations)
    relations = list(payload.relations)
    dependencies = list(payload.dependencies)
    diagnostics = list(payload.diagnostics)

    existing_dep_paths: set[str] = set()
    for dep in dependencies:
        try:
            _, norm_dep = ensure_contained_path(canonical_root, dep.repo_path, allow_nonexistent=True)
            existing_dep_paths.add(norm_dep)
        except (PathContainmentError, OSError, ValueError):
            existing_dep_paths.add(dep.repo_path.replace("\\", "/"))

    file_cache: dict[str, tuple[bytes, str, tuple[int, ...]]] = {}
    for resolved_path, rel_posix, dep_kind in consumed_files:
        try:
            raw_bytes = resolved_path.read_bytes()
        except OSError:
            continue
        encoding, _, _, _ = detect_encoding_and_decode(raw_bytes, preferred_legacy_encoding=preferred_legacy_encoding)
        scan_encoding = "latin-1" if encoding == "binary-or-mixed" else encoding
        try:
            b"".decode(scan_encoding)
        except LookupError:
            scan_encoding = "latin-1"
        file_cache[rel_posix] = (raw_bytes, scan_encoding, compute_line_byte_offsets(raw_bytes, scan_encoding))
        if rel_posix not in existing_dep_paths:
            dependencies.append(TUDependencyRecord(repo_path=rel_posix, dependency_kind=dep_kind))
            existing_dep_paths.add(rel_posix)

    shared_resource_symbols: dict[str, int] = {}
    for _, rel_posix, _ in sorted(consumed_files, key=lambda x: (0 if x[1].lower().endswith(".h") else 1)):
        if rel_posix not in file_cache or not _is_resource_file(rel_posix):
            continue
        raw_bytes, scan_encoding, line_offsets = file_cache[rel_posix]
        for res_item in scan_resources(raw_bytes, rel_posix, encoding=scan_encoding):
            if res_item["kind"] == "diagnostic":
                diagnostics.append(DiagnosticRecord(
                    DiagnosticSeverity.WARNING, f"mfc.{res_item['reason']}",
                    str(res_item.get("message", "Resource extraction failed")),
                    unsupported_feature=res_item["reason"],
                ))
                continue
            sym_name = res_item["symbol"] if res_item["kind"] == "resource_define" else res_item["resource_id"]
            cond = ConditionState(res_item["condition_state"])
            val = res_item.get("value")
            if val is None and sym_name in shared_resource_symbols:
                val = shared_resource_symbols[sym_name]
            if res_item["kind"] == "resource_define" and val is not None and cond == ConditionState.ACTIVE:
                shared_resource_symbols[sym_name] = val

            res_key = f"resource:{sym_name}"
            parent_id = res_item.get("parent_id")
            parent_key = f"resource:{parent_id}" if parent_id else None
            entities.append(
                EntityRecord(res_key, EntityKind.RESOURCE, sym_name, sym_name, LinkageScope.PROJECT, parent_semantic_key=parent_key)
            )
            span = _make_span(
                rel_posix, line_offsets, res_item["start_byte"], res_item["end_byte"], res_item["line"], res_item["end_line"]
            )
            sig = str(res_item.get("value_expr") or res_item.get("resource_type") or res_item.get("control_kind") or "")
            occurrences.append(
                SymbolOccurrenceRecord(
                    res_key, OccurrenceKind.RESOURCE_DEFINITION, span, sig, parent_key, cond, ExtractionQuality.SYNTACTIC
                )
            )
            obs_digest = hashlib.sha256(
                f"{payload.tu_key}:{rel_posix}:{res_item['start_byte']}:{res_item['end_byte']}:{res_item['kind']}:{sym_name}".encode("utf-8")
            ).hexdigest()[:24]
            details = dict(res_item)
            if val is not None and details.get("value") is None:
                details.update(value=val, resolution_status="resolved", reason=None)
            observations.append(
                ObservationRecord(
                    f"mfc:obs:{obs_digest}", res_item["kind"], span, res_key, cond, ExtractionQuality.SYNTACTIC,
                    f"mfc:{res_item['kind']}", "mfc", MFC_RULE_VERSION, details,
                )
            )

    for _, rel_posix, _ in consumed_files:
        if rel_posix not in file_cache or Path(rel_posix).suffix.lower() not in _CPP_SCAN_EXTENSIONS:
            continue
        raw_bytes, scan_encoding, line_offsets = file_cache[rel_posix]
        has_file_offsets = norm_offsets is not None and rel_posix in norm_offsets
        file_offsets = norm_offsets[rel_posix] if has_file_offsets and norm_offsets is not None else None
        missing_from_offset_dict = norm_offsets is not None and not has_file_offsets

        for item in scan_mfc(raw_bytes, rel_posix, encoding=scan_encoding, active_macro_offsets=file_offsets):
            raw_cond = "unknown" if (missing_from_offset_dict and item["condition_state"] == "active") else item["condition_state"]
            cond = ConditionState(raw_cond)
            span = _make_span(rel_posix, line_offsets, item["start_byte"], item["end_byte"], item["line"], item["end_line"])
            macro_name = item["macro"] or "MFC_MACRO"

            if item["kind"] == "diagnostic":
                reason_str = item["reason"] or "unsupported_macro"
                obs_digest = hashlib.sha256(
                    f"{payload.tu_key}:{rel_posix}:{item['start_byte']}:{item['end_byte']}:diag:{macro_name}:{reason_str}".encode("utf-8")
                ).hexdigest()[:24]
                details = {**item, "condition_state": cond.value}
                observations.append(
                    ObservationRecord(
                        f"mfc:obs:{obs_digest}", "mfc_diagnostic", span, None, cond, ExtractionQuality.SYNTACTIC,
                        f"mfc:{macro_name}", "mfc", MFC_RULE_VERSION, details,
                    )
                )
                is_unsupported = reason_str in ("unsupported_macro", "unknown_map_macro")
                diagnostics.append(
                    DiagnosticRecord(
                        DiagnosticSeverity.UNSUPPORTED if is_unsupported else DiagnosticSeverity.WARNING,
                        f"mfc.{reason_str}", item["message"] or reason_str, span, "mfc",
                        unsupported_feature=macro_name if is_unsupported else "",
                    )
                )
                continue

            owner_matches = classes_by_qname.get(item["owner_class"] or "", [])
            owner_key = owner_matches[0].semantic_key if len(owner_matches) == 1 else None
            event_ent = _event_identity(spec.build_context.context_key, item, owner_key)
            entities.append(event_ent)

            snippet_bytes = raw_bytes[item["start_byte"] : item["end_byte"]]
            occurrences.append(
                SymbolOccurrenceRecord(
                    event_ent.semantic_key, OccurrenceKind.MACRO_INVOCATION, span,
                    snippet_bytes.decode(scan_encoding, errors="replace"), owner_key, cond, ExtractionQuality.SYNTACTIC,
                )
            )

            q_handler = item["qualified_handler"] or ""
            matches = methods_by_qname.get(q_handler, [])
            if matches and cond == ConditionState.ACTIVE:
                if all(occ_states_by_entity.get(m.semantic_key) == {ConditionState.INACTIVE} for m in matches):
                    cond = ConditionState.INACTIVE
                elif all(
                    m.semantic_key in occ_states_by_entity and ConditionState.ACTIVE not in occ_states_by_entity[m.semantic_key]
                    for m in matches
                ):
                    cond = ConditionState.UNKNOWN

            obs_digest = hashlib.sha256(
                f"{payload.tu_key}:{rel_posix}:{item['start_byte']}:{item['end_byte']}:map:{macro_name}:{q_handler}".encode("utf-8")
            ).hexdigest()[:24]
            obs_key = f"mfc:obs:{obs_digest}"

            if cond in (ConditionState.INACTIVE, ConditionState.UNKNOWN) or not matches:
                res_status, target_key = ResolutionStatus.UNRESOLVED, None
            elif len(matches) == 1:
                res_status, target_key = ResolutionStatus.RESOLVED, matches[0].semantic_key
            else:
                res_status, target_key = ResolutionStatus.AMBIGUOUS, None

            details = {**item, "condition_state": cond.value, "resolution_status": res_status.value, "target_semantic_key": target_key}
            observations.append(
                ObservationRecord(
                    obs_key, "mfc_event_mapping", span, event_ent.semantic_key, cond, ExtractionQuality.SYNTACTIC,
                    f"mfc:{macro_name}", "mfc", MFC_RULE_VERSION, details,
                )
            )
            if cond == ConditionState.INACTIVE:
                continue

            evidence = RelationEvidenceRecord(
                obs_key, span, hashlib.sha256(snippet_bytes).hexdigest(), f"mfc:{macro_name}", "mfc",
                MFC_RULE_VERSION, f"{macro_name} -> {q_handler}",
            )
            reason = ResolutionReason.OVERLOAD_AMBIGUOUS if len(matches) > 1 else ResolutionReason.MFC_MACRO_EVIDENCE
            candidates = (
                ()
                if res_status == ResolutionStatus.RESOLVED
                else tuple(CandidateTargetRecord(m.semantic_key, reason.value, obs_key) for m in matches)
            )
            relations.append(
                RelationRecord(
                    event_ent.semantic_key, target_key, RelationKind.HANDLES_EVENT, res_status,
                    DispatchKind.EVENT_REGISTRATION, ExtractionQuality.SYNTACTIC, cond, reason,
                    "" if res_status == ResolutionStatus.RESOLVED else q_handler, (evidence,), candidates,
                )
            )

    return replace(
        payload,
        mfc_rule_version=payload.mfc_rule_version or MFC_RULE_VERSION,
        entities=tuple({e.semantic_key: e for e in entities}.values()),
        occurrences=tuple(dict.fromkeys(occurrences)),
        observations=tuple({o.observation_key: o for o in observations}.values()),
        relations=tuple(dict.fromkeys(relations)),
        dependencies=tuple(dict.fromkeys(dependencies)),
        diagnostics=tuple(dict.fromkeys(diagnostics)),
    )
