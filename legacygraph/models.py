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
