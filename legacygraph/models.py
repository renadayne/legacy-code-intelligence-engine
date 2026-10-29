"""Core data models and taxonomies for LegacyGraph."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class EntityKind(StrEnum):
    NAMESPACE = "namespace"
    CLASS = "class"
    STRUCT = "struct"
    ENUM = "enum"
    FUNCTION = "function"
    METHOD = "method"
    CONSTRUCTOR = "constructor"
    DESTRUCTOR = "destructor"
    VARIABLE = "variable"
    FIELD = "field"
    MACRO = "macro"
    TYPEDEF = "typedef"
    EVENT = "event"
    RESOURCE = "resource"
    FILE = "file"
    EXTERNAL_SYMBOL = "external_symbol"


class LinkageScope(StrEnum):
    EXTERNAL = "external"
    INTERNAL_TU = "internal_tu"
    PROJECT = "project"
    LOCAL = "local"
    CONFIG_SCOPED = "config_scoped"


class OccurrenceKind(StrEnum):
    DECLARATION = "declaration"
    DEFINITION = "definition"
    FORWARD_DECLARATION = "forward_declaration"
    MACRO_DEFINITION = "macro_definition"
    MACRO_INVOCATION = "macro_invocation"
    RESOURCE_DEFINITION = "resource_definition"
    REFERENCE = "reference"


class RelationKind(StrEnum):
    CALLS = "calls"
    MEMBER_OF = "member_of"
    INHERITS = "inherits"
    DECLARED_IN = "declared_in"
    DEFINED_IN = "defined_in"
    HANDLES_EVENT = "handles_event"
    INCLUDES = "includes"
    OVERRIDES = "overrides"
    REGISTERS_CALLBACK = "registers_callback"
    REFERENCES_STATE = "references_state"
    READS_STATE = "reads_state"
    WRITES_STATE = "writes_state"
    MAY_DISPATCH_TO = "may_dispatch_to"


class ResolutionStatus(StrEnum):
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    UNRESOLVED = "unresolved"


class DispatchKind(StrEnum):
    DIRECT = "direct"
    VIRTUAL = "virtual"
    INDIRECT = "indirect"
    EVENT_REGISTRATION = "event-registration"
    STATIC_INCLUDE = "static-include"
    STRUCTURAL = "structural"


class ExtractionQuality(StrEnum):
    SEMANTIC = "semantic"
    SYNTACTIC = "syntactic"
    RECOVERED = "recovered"
    MANUAL = "manual"


class ConditionState(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    UNKNOWN = "unknown"


class ResolutionReason(StrEnum):
    EXACT_SEMANTIC = "exact_semantic"
    STATIC_VIRTUAL_TARGET = "static_virtual_target"
    MFC_MACRO_EVIDENCE = "mfc_macro_evidence"
    MISSING_HEADER = "missing_header"
    DEPENDENT_TEMPLATE = "dependent_template"
    UNKNOWN_RECEIVER = "unknown_receiver"
    UNSUPPORTED_MACRO = "unsupported_macro"
    INDIRECT_FUNCTION_POINTER = "indirect_function_pointer"
    OVERLOAD_AMBIGUOUS = "overload_ambiguous"
    PARSE_RECOVERY = "parse_recovery"
    UNPARSED_BODY = "unparsed_body"


class DiagnosticSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    FATAL = "fatal"
    UNSUPPORTED = "unsupported"


class DiagnosticLocationQuality(StrEnum):
    EXACT = "exact"
    FALLBACK = "fallback"
    NONE = "none"
    UNKNOWN = "unknown"


class DiagnosticLocationProvenance(StrEnum):
    CONTAINED_FILE = "contained_file"
    EXTERNAL_FILE = "external_file"
    MISSING_FILE = "missing_file"
    MISSING_LOCATION = "missing_location"
    UNKNOWN = "unknown"


class DiagnosticClass(StrEnum):
    NARROWING_INITIALIZER = "narrowing_initializer"
    NON_TRIVIAL_VARARGS = "non_trivial_varargs"
    TYPE_IN_RESULT_DECLARATION = "type_in_result_declaration"
    WCHAR_POINTER_MISMATCH = "wchar_pointer_mismatch"
    CONST_QUALIFICATION_MISMATCH = "const_qualification_mismatch"
    ERROR_LIMIT_REACHED = "error_limit_reached"
    MISSING_INCLUDE = "missing_include"
    UNSUPPORTED_CONFIGURATION = "unsupported_configuration"
    ENCODING_FAILURE = "encoding_failure"
    SOURCE_MUTATION = "source_mutation"
    LITERAL_CONVERSION_WARNING = "literal_conversion_warning"
    COMMENT_WARNING = "comment_warning"
    PREPROCESSOR_EXTRA_TOKENS = "preprocessor_extra_tokens"
    PARSE_ISSUE = "parse_issue"
    LEXICAL_OR_PREPROCESSOR_ISSUE = "lexical_or_preprocessor_issue"
    SEMANTIC_ISSUE = "semantic_issue"
    OTHER_DIAGNOSTIC = "other_diagnostic"


BLOCKING_DIAGNOSTIC_SEVERITIES: frozenset[str] = frozenset(
    {
        DiagnosticSeverity.ERROR.value,
        DiagnosticSeverity.FATAL.value,
        DiagnosticSeverity.UNSUPPORTED.value,
    }
)

DIAGNOSTIC_COUNTS_NOTE = (
    "record_count and affected_tu_count measure diagnostic records and translation units, "
    "not proven distinct source defects."
)


class DependencyKind(StrEnum):
    MAIN_SOURCE = "main_source"
    DIRECT_INCLUDE = "direct_include"
    TRANSITIVE_INCLUDE = "transitive_include"
    FORCED_INCLUDE = "forced_include"
    PCH_HEADER = "pch_header"
    RESOURCE_INCLUDE = "resource_include"


class ParseStatus(StrEnum):
    PENDING = "pending"
    PARSED_CLEAN = "parsed_clean"
    PARSED_WITH_ERRORS = "parsed_with_errors"
    FAILED = "failed"
    STALE = "stale"


class GenerationStatus(StrEnum):
    STAGING = "staging"
    PUBLISHED = "published"
    ROLLED_BACK = "rolled_back"
    SUPERSEDED = "superseded"


@dataclass(frozen=True)
class SourceSpan:
    """Byte and line/column span in an original source file version."""

    repo_path: str
    byte_start: int
    byte_end: int
    start_line: int
    start_col: int
    end_line: int
    end_col: int

    def __post_init__(self) -> None:
        if self.byte_start < 0 or self.byte_end < self.byte_start:
            raise ValueError(f"Invalid byte span [{self.byte_start}, {self.byte_end}]")
        if self.start_line < 1 or self.end_line < self.start_line:
            raise ValueError(f"Invalid line span [{self.start_line}, {self.end_line}]")


@dataclass(frozen=True)
class FileInventoryRecord:
    """Discovered repository file and content metadata."""

    repo_path: str
    size_bytes: int
    content_sha256: str
    encoding: str
    has_bom: bool
    decode_clean: bool
    line_count: int
    line_byte_offsets: tuple[int, ...] = ()
    file_role: str = "source"


@dataclass(frozen=True)
class BuildContextSpec:
    """Explicit compilation context for a target/configuration/platform."""

    project_name: str
    target_name: str
    configuration: str
    platform: str
    working_directory: str = "."
    compiler_family: str = "msvc"
    toolset_version: str = ""
    architecture: str = "x64"
    language_standard: str = "c++17"
    include_dirs: tuple[str, ...] = ()
    system_include_dirs: tuple[str, ...] = ()
    defines: tuple[str, ...] = ()
    undefines: tuple[str, ...] = ()
    forced_includes: tuple[str, ...] = ()
    extra_flags: tuple[str, ...] = ()
    pch_header: str = ""
    sdk_version: str = ""
    mfc_mode: str = ""
    provenance: str = "manifest"

    @property
    def context_key(self) -> str:
        """Stable human-readable + hash discriminator for this context."""
        payload = {
            "project_name": self.project_name,
            "target_name": self.target_name,
            "configuration": self.configuration,
            "platform": self.platform,
            "working_directory": self.working_directory,
            "compiler_family": self.compiler_family,
            "toolset_version": self.toolset_version,
            "architecture": self.architecture,
            "language_standard": self.language_standard,
            "include_dirs": list(self.include_dirs),
            "system_include_dirs": list(self.system_include_dirs),
            "defines": list(self.defines),
            "undefines": list(self.undefines),
            "forced_includes": list(self.forced_includes),
            "extra_flags": list(self.extra_flags),
            "pch_header": self.pch_header,
            "sdk_version": self.sdk_version,
            "mfc_mode": self.mfc_mode,
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
        ).hexdigest()[:16]
        return f"{self.project_name}:{self.target_name}:{self.configuration}:{self.platform}:{digest}"

    @property
    def fingerprint(self) -> str:
        payload = {
            "context_key": self.context_key,
            "provenance": self.provenance,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True)
class TranslationUnitSpec:
    """A single compilation unit bound to a specific BuildContextSpec and per-file flags."""

    main_file_path: str
    build_context: BuildContextSpec
    per_file_include_dirs: tuple[str, ...] = ()
    per_file_defines: tuple[str, ...] = ()
    per_file_undefines: tuple[str, ...] = ()
    per_file_forced_includes: tuple[str, ...] = ()
    per_file_flags: tuple[str, ...] = ()
    response_files: tuple[str, ...] = ()
    command_origin: str = "manifest"

    @property
    def flags_fingerprint(self) -> str:
        payload = {
            "context_fingerprint": self.build_context.fingerprint,
            "main_file_path": self.main_file_path,
            "per_file_include_dirs": list(self.per_file_include_dirs),
            "per_file_defines": list(self.per_file_defines),
            "per_file_undefines": list(self.per_file_undefines),
            "per_file_forced_includes": list(self.per_file_forced_includes),
            "per_file_flags": list(self.per_file_flags),
            "response_files": list(self.response_files),
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
        ).hexdigest()

    @property
    def tu_key(self) -> str:
        return f"{self.build_context.context_key}::{self.main_file_path}::{self.flags_fingerprint[:12]}"


@dataclass(frozen=True)
class EntityRecord:
    """Logical symbol, class, event, or resource identity."""

    semantic_key: str
    kind: EntityKind
    name: str
    qualified_name: str
    linkage_scope: LinkageScope = LinkageScope.EXTERNAL
    scope_qualifier: str = ""
    parent_semantic_key: str | None = None
    is_external: bool = False


@dataclass(frozen=True)
class SymbolOccurrenceRecord:
    """Concrete source location where an entity is declared, defined, or referenced."""

    entity_semantic_key: str
    occurrence_kind: OccurrenceKind
    span: SourceSpan
    signature: str = ""
    parent_semantic_key: str | None = None
    condition_state: ConditionState = ConditionState.ACTIVE
    extraction_quality: ExtractionQuality = ExtractionQuality.SEMANTIC


@dataclass(frozen=True)
class ObservationRecord:
    """A parser or rule fact owned by a specific TU parse run."""

    observation_key: str
    observation_type: str
    span: SourceSpan
    subject_semantic_key: str | None = None
    condition_state: ConditionState = ConditionState.ACTIVE
    extraction_quality: ExtractionQuality = ExtractionQuality.SEMANTIC
    rule_id: str = ""
    parser_name: str = "core"
    parser_version: str = "0.1.0"
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RelationEvidenceRecord:
    """Provenance and source span backing a relation in a TU run."""

    observation_key: str
    span: SourceSpan
    evidence_snippet_hash: str = ""
    rule_id: str = ""
    parser_name: str = "core"
    parser_version: str = "0.1.0"
    notes: str = ""


@dataclass(frozen=True)
class CandidateTargetRecord:
    """Possible target for an ambiguous or unresolved relation (or virtual override candidate)."""

    candidate_semantic_key: str
    candidate_reason: str
    evidence_observation_key: str | None = None


@dataclass(frozen=True)
class RelationRecord:
    """Directed relationship with explicit resolution status, dispatch kind, quality, and evidence."""

    source_semantic_key: str
    target_semantic_key: str | None
    kind: RelationKind
    resolution_status: ResolutionStatus
    dispatch_kind: DispatchKind
    extraction_quality: ExtractionQuality
    condition_state: ConditionState = ConditionState.ACTIVE
    reason: ResolutionReason | str = ResolutionReason.EXACT_SEMANTIC
    unresolved_target_expr: str = ""
    evidence: tuple[RelationEvidenceRecord, ...] = ()
    candidates: tuple[CandidateTargetRecord, ...] = ()

    def __post_init__(self) -> None:
        if (
            self.resolution_status == ResolutionStatus.RESOLVED
            and not self.target_semantic_key
        ):
            raise ValueError("Resolved relation must specify a target_semantic_key.")
        if (
            self.resolution_status == ResolutionStatus.RESOLVED
            and not self.evidence
        ):
            raise ValueError("Resolved relation must have at least one RelationEvidenceRecord.")


@dataclass(frozen=True)
class TUDependencyRecord:
    """Header or source file consumed during a TU parse run."""

    repo_path: str
    dependency_kind: DependencyKind = DependencyKind.DIRECT_INCLUDE
    included_from_path: str | None = None
    include_line: int | None = None
    is_missing: bool = False
    missing_include_text: str = ""


def classify_diagnostic(
    *,
    code: str = "",
    message: str = "",
    diagnostic_option: str = "",
    severity: str | DiagnosticSeverity = "",
    missing_dependency: str = "",
    unsupported_feature: str = "",
) -> str:
    """Return a bounded repository diagnostic classification without altering severity."""
    opt = (diagnostic_option or "").strip().lower()
    msg = (message or "").strip().lower()
    c = (code or "").strip().lower()
    sev = (
        severity.value
        if isinstance(severity, DiagnosticSeverity)
        else str(severity or "").strip().lower()
    )

    if (
        sev == DiagnosticSeverity.UNSUPPORTED.value
        or unsupported_feature
        or c
        in (
            "clang.unsupported_flag",
            "clang.unsupported_response_file",
            "clang.binary_pch_unsupported",
            "clang.unsupported_wchar_context",
        )
    ):
        return DiagnosticClass.UNSUPPORTED_CONFIGURATION.value

    if c in ("clang.source_mutated_during_parse", "scanner.source_changed_after_parse"):
        return DiagnosticClass.SOURCE_MUTATION.value

    if (
        c == "clang.unsupported_encoding"
        or opt == "-winvalid-source-encoding"
        or "illegal character encoding in" in msg
    ):
        return DiagnosticClass.ENCODING_FAILURE.value

    if (
        missing_dependency
        or c in ("clang.main_file_missing", "scanner.main_file_deleted")
        or "file not found" in msg
    ):
        return DiagnosticClass.MISSING_INCLUDE.value

    if opt.startswith("-ferror-limit") or "too many errors emitted, stopping now" in msg:
        return DiagnosticClass.ERROR_LIMIT_REACHED.value

    if (
        opt in ("-wc++11-narrowing", "-wnarrowing")
        or "cannot be narrowed to type" in msg
    ):
        return DiagnosticClass.NARROWING_INITIALIZER.value

    if (
        opt == "-wnon-pod-varargs"
        or (
            (
                "cannot pass object of non-trivial type" in msg
                or "cannot pass object of non-pod type" in msg
            )
            and "variadic" in msg
        )
    ):
        return DiagnosticClass.NON_TRIVIAL_VARARGS.value

    if "result type" in msg:
        return DiagnosticClass.TYPE_IN_RESULT_DECLARATION.value

    if "discards qualifiers" in msg:
        return DiagnosticClass.CONST_QUALIFICATION_MISMATCH.value

    if "wchar_t" in msg and (
        "unsigned short" in msg or "ushort" in msg or "word" in msg
    ):
        return DiagnosticClass.WCHAR_POINTER_MISMATCH.value

    if (
        opt in ("-wwritable-strings", "-wc++11-compat-deprecated-writable-strings")
        or "conversion from string literal to" in msg
    ):
        return DiagnosticClass.LITERAL_CONVERSION_WARNING.value

    if opt == "-wcomment" or "within block comment" in msg:
        return DiagnosticClass.COMMENT_WARNING.value

    if opt == "-wextra-tokens" or "extra tokens at end of #" in msg:
        return DiagnosticClass.PREPROCESSOR_EXTRA_TOKENS.value

    if c in ("parse issue", "clang.parse_issue"):
        return DiagnosticClass.PARSE_ISSUE.value

    if c in ("lexical or preprocessor issue", "clang.lexical_or_preprocessor_issue"):
        return DiagnosticClass.LEXICAL_OR_PREPROCESSOR_ISSUE.value

    if c in ("semantic issue", "clang.semantic_issue"):
        return DiagnosticClass.SEMANTIC_ISSUE.value

    return DiagnosticClass.OTHER_DIAGNOSTIC.value


def infer_legacy_diagnostic_provenance(
    *,
    code: str = "",
    parse_scope: str = "tu",
    has_span: bool = False,
    byte_start: int | None = None,
    byte_end: int | None = None,
    location_quality: str = "",
    location_provenance: str = "",
) -> tuple[str, str]:
    """Normalize or conservatively infer location quality and provenance without claiming unverified exactness."""
    qual = (location_quality or "").strip().lower()
    prov = (location_provenance or "").strip().lower()
    c = (code or "").strip().lower()
    scope = (parse_scope or "tu").strip().lower()

    valid_quals = {item.value for item in DiagnosticLocationQuality}
    valid_provs = {item.value for item in DiagnosticLocationProvenance}
    if qual not in valid_quals:
        qual = DiagnosticLocationQuality.UNKNOWN.value
    if prov not in valid_provs:
        prov = DiagnosticLocationProvenance.UNKNOWN.value

    if not has_span:
        if prov == DiagnosticLocationProvenance.UNKNOWN.value:
            prov = (
                DiagnosticLocationProvenance.MISSING_FILE.value
                if c in ("clang.main_file_missing", "scanner.main_file_deleted")
                else DiagnosticLocationProvenance.MISSING_LOCATION.value
            )
        elif prov == DiagnosticLocationProvenance.CONTAINED_FILE.value:
            prov = DiagnosticLocationProvenance.MISSING_LOCATION.value
        return DiagnosticLocationQuality.NONE.value, prov

    if scope == "config" or c in (
        "clang.unsupported_flag",
        "clang.unsupported_response_file",
        "clang.binary_pch_unsupported",
        "clang.unsupported_wchar_context",
        "clang.source_mutated_during_parse",
        "scanner.source_changed_after_parse",
        "clang.parse_exception",
        "clang.unsupported_encoding",
    ):
        if prov in (
            DiagnosticLocationProvenance.UNKNOWN.value,
            DiagnosticLocationProvenance.CONTAINED_FILE.value,
        ):
            prov = DiagnosticLocationProvenance.MISSING_LOCATION.value
        return DiagnosticLocationQuality.FALLBACK.value, prov

    if c in ("clang.main_file_missing", "scanner.main_file_deleted"):
        return (
            DiagnosticLocationQuality.FALLBACK.value,
            DiagnosticLocationProvenance.MISSING_FILE.value,
        )

    if byte_start is not None and byte_end is not None and int(byte_end) > int(byte_start):
        if prov == DiagnosticLocationProvenance.CONTAINED_FILE.value:
            prov = DiagnosticLocationProvenance.UNKNOWN.value
        return DiagnosticLocationQuality.FALLBACK.value, prov

    if qual == DiagnosticLocationQuality.EXACT.value:
        if prov != DiagnosticLocationProvenance.CONTAINED_FILE.value:
            return DiagnosticLocationQuality.FALLBACK.value, prov
        return DiagnosticLocationQuality.EXACT.value, DiagnosticLocationProvenance.CONTAINED_FILE.value

    if qual == DiagnosticLocationQuality.FALLBACK.value:
        if prov == DiagnosticLocationProvenance.CONTAINED_FILE.value:
            prov = DiagnosticLocationProvenance.UNKNOWN.value
        return DiagnosticLocationQuality.FALLBACK.value, prov

    return DiagnosticLocationQuality.UNKNOWN.value, prov


def is_exact_diagnostic_location(
    *,
    has_span: bool,
    repo_path: str | None,
    line: int | None,
    location_quality: str,
    location_provenance: str,
) -> bool:
    return bool(
        has_span
        and repo_path
        and line is not None
        and location_quality == DiagnosticLocationQuality.EXACT.value
        and location_provenance == DiagnosticLocationProvenance.CONTAINED_FILE.value
    )


@dataclass(frozen=True)
class DiagnosticRecord:
    """Diagnostic or unsupported feature recorded during a TU parse run."""

    severity: DiagnosticSeverity
    code: str
    message: str
    span: SourceSpan | None = None
    parse_scope: str = "tu"
    missing_dependency: str = ""
    unsupported_feature: str = ""
    location_quality: str = DiagnosticLocationQuality.UNKNOWN.value
    location_provenance: str = DiagnosticLocationProvenance.UNKNOWN.value
    diagnostic_option: str = ""
    diagnostic_class: str = ""

    def __post_init__(self) -> None:
        qual, prov = infer_legacy_diagnostic_provenance(
            code=self.code,
            parse_scope=self.parse_scope,
            has_span=self.span is not None,
            byte_start=self.span.byte_start if self.span is not None else None,
            byte_end=self.span.byte_end if self.span is not None else None,
            location_quality=self.location_quality,
            location_provenance=self.location_provenance,
        )
        object.__setattr__(self, "location_quality", qual)
        object.__setattr__(self, "location_provenance", prov)
        if not self.diagnostic_class:
            object.__setattr__(
                self,
                "diagnostic_class",
                classify_diagnostic(
                    code=self.code,
                    message=self.message,
                    diagnostic_option=self.diagnostic_option,
                    severity=self.severity,
                    missing_dependency=self.missing_dependency,
                    unsupported_feature=self.unsupported_feature,
                ),
            )

    @property
    def is_exact_location(self) -> bool:
        return is_exact_diagnostic_location(
            has_span=self.span is not None,
            repo_path=self.span.repo_path if self.span is not None else None,
            line=self.span.start_line if self.span is not None else None,
            location_quality=self.location_quality,
            location_provenance=self.location_provenance,
        )


def format_diagnostic_record(
    diag: DiagnosticRecord,
    *,
    tu_key: str,
    main_file_path: str,
    context_key: str = "",
    parse_run_id: int | None = None,
    parse_status: str | None = None,
    reused: bool = False,
    is_inherited: bool = False,
    tu_snapshot_state: str = "unverified_current",
    content_sha256: str | None = None,
    diagnostic_id: int | None = None,
) -> dict[str, Any]:
    """Serialize a DiagnosticRecord into a dictionary with explicit provenance and classification."""
    span = diag.span
    entry: dict[str, Any] = {
        "tu_key": tu_key,
        "context_key": context_key,
        "main_file_path": main_file_path,
        "severity": diag.severity.value if isinstance(diag.severity, DiagnosticSeverity) else str(diag.severity),
        "code": diag.code,
        "message": diag.message,
        "parse_scope": diag.parse_scope,
        "repo_path": span.repo_path if span is not None else None,
        "line": span.start_line if span is not None else None,
        "col": span.start_col if span is not None else None,
        "start_line": span.start_line if span is not None else None,
        "start_col": span.start_col if span is not None else None,
        "end_line": span.end_line if span is not None else None,
        "end_col": span.end_col if span is not None else None,
        "byte_start": span.byte_start if span is not None else None,
        "byte_end": span.byte_end if span is not None else None,
        "location_quality": diag.location_quality,
        "location_provenance": diag.location_provenance,
        "is_exact_location": diag.is_exact_location,
        "diagnostic_option": diag.diagnostic_option,
        "diagnostic_class": diag.diagnostic_class,
        "missing_dependency": diag.missing_dependency,
        "unsupported_feature": diag.unsupported_feature,
    }
    if diagnostic_id is not None:
        entry["id"] = diagnostic_id
    if parse_run_id is not None:
        entry["parse_run_id"] = parse_run_id
    if parse_status is not None:
        entry["parse_status"] = parse_status
    if content_sha256 is not None:
        entry["content_sha256"] = content_sha256
    entry["reused"] = bool(reused or is_inherited)
    entry["is_inherited"] = bool(is_inherited or reused)
    entry["tu_snapshot_state"] = tu_snapshot_state
    return entry


def summarize_blocker_diagnostics(
    diagnostics: Any,
    *,
    blocking_only: bool = True,
    max_affected_tus: int | None = 100,
) -> list[dict[str, Any]]:
    """Group blocking diagnostics by exact contained location + classification + build context.

    Blocker summaries always include only blocking severities (`error`, `fatal`, `unsupported`).
    Fallback, external-file, missing-file, missing-location, and unknown-provenance
    diagnostics are never merged together even when they share a placeholder fallback span.
    """
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    group_meta: dict[tuple[Any, ...], tuple[bool, str, str, str, str, str, int | None, int | None]] = {}

    for idx, raw in enumerate(diagnostics):
        sev = str(raw.get("severity") or "").strip().lower()
        if sev not in BLOCKING_DIAGNOSTIC_SEVERITIES:
            continue
        start_line = raw.get("start_line") if raw.get("start_line") is not None else raw.get("line")
        start_col = raw.get("start_col") if raw.get("start_col") is not None else raw.get("col")
        repo_path = raw.get("repo_path")
        has_span = repo_path is not None and start_line is not None
        loc_qual, loc_prov = infer_legacy_diagnostic_provenance(
            code=str(raw.get("code") or ""),
            parse_scope=str(raw.get("parse_scope") or "tu"),
            has_span=has_span,
            byte_start=raw.get("byte_start"),
            byte_end=raw.get("byte_end"),
            location_quality=str(raw.get("location_quality") or ""),
            location_provenance=str(raw.get("location_provenance") or ""),
        )
        is_exact = bool(raw.get("is_exact_location", True)) and is_exact_diagnostic_location(
            has_span=has_span,
            repo_path=repo_path,
            line=int(start_line) if start_line is not None else None,
            location_quality=loc_qual,
            location_provenance=loc_prov,
        )
        diag_opt = str(raw.get("diagnostic_option") or "")
        diag_cls = str(raw.get("diagnostic_class") or "") or classify_diagnostic(
            code=str(raw.get("code") or ""),
            message=str(raw.get("message") or ""),
            diagnostic_option=diag_opt,
            severity=sev,
            missing_dependency=str(raw.get("missing_dependency") or ""),
            unsupported_feature=str(raw.get("unsupported_feature") or ""),
        )
        ctx_key = str(raw.get("context_key") or "")
        line_val = int(start_line) if start_line is not None else None
        col_val = int(start_col) if start_col is not None else (1 if line_val is not None else None)
        content_sha = str(raw.get("content_sha256") or "")
        file_ver_id = raw.get("file_version_id")

        if is_exact:
            gkey: tuple[Any, ...] = (
                "exact",
                ctx_key,
                str(repo_path),
                content_sha,
                file_ver_id if not content_sha else None,
                raw.get("byte_start"),
                raw.get("byte_end"),
                line_val,
                col_val,
                diag_cls,
                diag_opt,
                sev,
                str(raw.get("code") or ""),
            )
        else:
            gkey = ("unmerged", idx, raw.get("id"))

        groups.setdefault(gkey, []).append(raw)
        if gkey not in group_meta:
            group_meta[gkey] = (
                is_exact,
                loc_qual,
                loc_prov,
                diag_cls,
                diag_opt,
                ctx_key,
                line_val,
                col_val,
            )

    summaries: list[dict[str, Any]] = []
    for gkey, records in groups.items():
        is_exact, loc_qual, loc_prov, diag_cls, diag_opt, ctx_key, line_val, col_val = group_meta[gkey]
        first = records[0]
        tu_map: dict[str, dict[str, Any]] = {}
        for r in records:
            tkey = str(r.get("tu_key") or "")
            reused_flag = bool(r.get("reused") or r.get("is_inherited"))
            snap_state = str(
                r.get("tu_snapshot_state")
                or ("unverified_inherited" if reused_flag else "unverified_current")
            )
            if tkey not in tu_map:
                tu_map[tkey] = {
                    "tu_key": tkey,
                    "main_file_path": str(r.get("main_file_path") or ""),
                    "parse_run_id": r.get("parse_run_id"),
                    "parse_status": r.get("parse_status"),
                    "reused": reused_flag,
                    "is_inherited": bool(r.get("is_inherited", reused_flag)),
                    "tu_snapshot_state": snap_state,
                    "record_count": 1,
                }
            else:
                tu_map[tkey]["record_count"] += 1
                if not reused_flag:
                    tu_map[tkey]["reused"] = False
                    tu_map[tkey]["is_inherited"] = False
                    tu_map[tkey]["tu_snapshot_state"] = snap_state

        all_affected_tu_keys = sorted(k for k in tu_map if k)
        all_affected_tus = [tu_map[k] for k in all_affected_tu_keys]
        all_affected_main_files = sorted(
            {tu["main_file_path"] for tu in all_affected_tus if tu["main_file_path"]}
        )
        total_affected_tus = len(all_affected_tus)
        parsed_tu_count = sum(1 for tu in all_affected_tus if not tu["reused"])
        reused_tu_count = sum(1 for tu in all_affected_tus if tu["reused"])
        if max_affected_tus is not None and max_affected_tus > 0 and total_affected_tus > max_affected_tus:
            affected_tu_keys = all_affected_tu_keys[:max_affected_tus]
            affected_tus = all_affected_tus[:max_affected_tus]
            affected_main_files = all_affected_main_files[:max_affected_tus]
            affected_tus_truncated = True
            omitted_affected_tu_count = total_affected_tus - max_affected_tus
        else:
            affected_tu_keys = all_affected_tu_keys
            affected_tus = all_affected_tus
            affected_main_files = all_affected_main_files
            affected_tus_truncated = False
            omitted_affected_tu_count = 0
        end_line_val = (
            int(first["end_line"])
            if is_exact and first.get("end_line") is not None
            else line_val if is_exact else None
        )
        end_col_val = (
            int(first["end_col"])
            if is_exact and first.get("end_col") is not None
            else col_val if is_exact else None
        )

        summaries.append(
            {
                "context_key": ctx_key,
                "severity": str(first.get("severity") or ""),
                "code": str(first.get("code") or ""),
                "diagnostic_class": diag_cls,
                "diagnostic_option": diag_opt,
                "location_quality": loc_qual,
                "location_provenance": loc_prov,
                "is_exact_location": is_exact,
                "grouped_by_exact_location": is_exact,
                "repo_path": str(first["repo_path"]) if is_exact and first.get("repo_path") else None,
                "line": line_val if is_exact else None,
                "col": col_val if is_exact else None,
                "start_line": line_val if is_exact else None,
                "start_col": col_val if is_exact else None,
                "end_line": end_line_val,
                "end_col": end_col_val,
                "byte_start": int(first["byte_start"]) if is_exact and first.get("byte_start") is not None else None,
                "byte_end": int(first["byte_end"]) if is_exact and first.get("byte_end") is not None else None,
                "content_sha256": first.get("content_sha256") if is_exact else None,
                "fallback_repo_path": first.get("repo_path") if not is_exact else None,
                "fallback_line": line_val if not is_exact else None,
                "message": str(first.get("message") or ""),
                "missing_dependency": str(first.get("missing_dependency") or ""),
                "unsupported_feature": str(first.get("unsupported_feature") or ""),
                "record_count": len(records),
                "affected_tu_count": total_affected_tus,
                "parsed_tu_count": parsed_tu_count,
                "reused_tu_count": reused_tu_count,
                "affected_tus_truncated": affected_tus_truncated,
                "omitted_affected_tu_count": omitted_affected_tu_count,
                "affected_tu_keys": affected_tu_keys,
                "affected_main_files": affected_main_files,
                "affected_translation_units": affected_tus,
                "counts_are_not_defect_counts": True,
            }
        )

    summaries.sort(
        key=lambda item: (
            -int(item["affected_tu_count"]),
            -int(item["record_count"]),
            0 if item["is_exact_location"] else 1,
            str(item["context_key"]),
            str(item["repo_path"] or item["fallback_repo_path"] or ""),
            int(item["line"] or item["fallback_line"] or 0),
            int(item["col"] or 0),
            str(item["diagnostic_class"]),
            str(item["code"]),
            str(item["message"]),
            tuple(item["affected_tu_keys"]),
        )
    )
    return summaries


def build_diagnostic_summary(
    diagnostics: Any,
    blocker_summaries: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compute structured diagnostic and blocker summary statistics."""
    records = list(diagnostics)
    blockers = (
        blocker_summaries
        if blocker_summaries is not None
        else summarize_blocker_diagnostics(records, blocking_only=True)
    )
    by_severity: dict[str, int] = {}
    by_quality: dict[str, int] = {}
    by_provenance: dict[str, int] = {}
    by_class: dict[str, int] = {}
    exact_cnt = 0
    fallback_cnt = 0
    blocking_cnt = 0
    affected_blocking_tus: set[str] = set()

    for r in records:
        sev = str(r.get("severity") or "").strip().lower()
        by_severity[sev] = by_severity.get(sev, 0) + 1
        start_line = r.get("start_line") if r.get("start_line") is not None else r.get("line")
        has_span = r.get("repo_path") is not None and start_line is not None
        qual, prov = infer_legacy_diagnostic_provenance(
            code=str(r.get("code") or ""),
            parse_scope=str(r.get("parse_scope") or "tu"),
            has_span=has_span,
            byte_start=r.get("byte_start"),
            byte_end=r.get("byte_end"),
            location_quality=str(r.get("location_quality") or ""),
            location_provenance=str(r.get("location_provenance") or ""),
        )
        by_quality[qual] = by_quality.get(qual, 0) + 1
        by_provenance[prov] = by_provenance.get(prov, 0) + 1
        cls = str(r.get("diagnostic_class") or "") or classify_diagnostic(
            code=str(r.get("code") or ""),
            message=str(r.get("message") or ""),
            diagnostic_option=str(r.get("diagnostic_option") or ""),
            severity=sev,
            missing_dependency=str(r.get("missing_dependency") or ""),
            unsupported_feature=str(r.get("unsupported_feature") or ""),
        )
        by_class[cls] = by_class.get(cls, 0) + 1
        if qual == DiagnosticLocationQuality.EXACT.value and prov == DiagnosticLocationProvenance.CONTAINED_FILE.value:
            exact_cnt += 1
        else:
            fallback_cnt += 1
        if sev in BLOCKING_DIAGNOSTIC_SEVERITIES:
            blocking_cnt += 1
            if r.get("tu_key"):
                affected_blocking_tus.add(str(r["tu_key"]))

    return {
        "total_diagnostic_records": len(records),
        "blocking_diagnostic_records": blocking_cnt,
        "blocker_group_count": len(blockers),
        "exact_blocker_group_count": sum(1 for b in blockers if b.get("is_exact_location")),
        "unmerged_non_exact_blocker_count": sum(1 for b in blockers if not b.get("is_exact_location")),
        "affected_blocking_tu_count": len(affected_blocking_tus),
        "exact_location_records": exact_cnt,
        "non_exact_location_records": fallback_cnt,
        "by_severity": dict(sorted(by_severity.items())),
        "by_location_quality": dict(sorted(by_quality.items())),
        "by_location_provenance": dict(sorted(by_provenance.items())),
        "by_diagnostic_class": dict(sorted(by_class.items())),
        "counts_are_not_defect_counts": True,
        "counts_note": DIAGNOSTIC_COUNTS_NOTE,
    }


@dataclass(frozen=True)
class ParseRunPayload:
    """Complete bundle of TU-owned facts emitted by a parser/adapter for one TU."""

    tu_key: str
    parser_name: str
    parser_version: str
    mfc_rule_version: str
    parse_status: ParseStatus
    entities: tuple[EntityRecord, ...] = ()
    occurrences: tuple[SymbolOccurrenceRecord, ...] = ()
    observations: tuple[ObservationRecord, ...] = ()
    relations: tuple[RelationRecord, ...] = ()
    dependencies: tuple[TUDependencyRecord, ...] = ()
    diagnostics: tuple[DiagnosticRecord, ...] = ()
    has_incomplete_dependencies: bool = False
