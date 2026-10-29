"""Semantic C++ AST parser backed by libclang for LegacyGraph."""

from __future__ import annotations

import ctypes
from contextlib import contextmanager, nullcontext
import hashlib
import re
from pathlib import Path
from typing import Any

from .discovery import ensure_contained_path, resolve_repo_root
from .mfc import MFC_RULE_VERSION
from .parser_input import SourceBuffer, encoding_name, policy_version
from .models import (
    ConditionState,
    DependencyKind,
    DiagnosticLocationProvenance,
    DiagnosticLocationQuality,
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
    ParseStatus,
    RelationEvidenceRecord,
    RelationKind,
    RelationRecord,
    ResolutionReason,
    ResolutionStatus,
    SourceSpan,
    SymbolOccurrenceRecord,
    TranslationUnitSpec,
    TUDependencyRecord,
)

CLANG_PARSER_NAME = "clang"
CLANG_PARSER_VERSION = "0.3.2"
DEFAULT_MFC_RULE_VERSION = MFC_RULE_VERSION

try:
    import clang.cindex as cindex  # type: ignore[import-untyped]
except Exception as _clang_import_exc:  # pragma: no cover - optional runtime dependency
    cindex = None  # type: ignore[assignment]
    _CLANG_IMPORT_ERROR: Exception | None = _clang_import_exc
else:
    _CLANG_IMPORT_ERROR = None


class LibclangUnavailableError(RuntimeError):
    """Raised when libclang Python bindings or native shared library are unavailable."""


def _compiled_file_bytes(translation_unit, path: Path) -> bytes | None:
    """Read Clang's consumed buffer, not a later version on disk."""
    try:
        function = cindex.conf.lib.clang_getFileContents
        function.argtypes = [cindex.TranslationUnit, cindex.File, ctypes.POINTER(ctypes.c_size_t)]
        function.restype = ctypes.c_void_p
        size = ctypes.c_size_t()
        pointer = function(translation_unit, translation_unit.get_file(str(path)), ctypes.byref(size))
        return ctypes.string_at(pointer, size.value) if pointer else None
    except Exception:
        return None


def is_libclang_available() -> tuple[bool, str]:
    """Check whether libclang is importable and its native DLL can create an Index."""
    if cindex is None:
        return False, f"clang.cindex import failed: {_CLANG_IMPORT_ERROR}"
    try:
        _ = cindex.Index.create()
        return True, "ok"
    except Exception as exc:  # pragma: no cover
        return False, f"libclang native library load failed: {exc}"


_STD_MAP: dict[str, str] = {
    "c++11": "-std=c++11",
    "c++14": "-std=c++14",
    "c++17": "-std=c++17",
    "c++20": "-std=c++20",
    "c++23": "-std=c++23",
    "c++latest": "-std=c++23",
    "gnu++14": "-std=gnu++14",
    "gnu++17": "-std=gnu++17",
    "gnu++20": "-std=gnu++20",
}

_SAFE_PASSTHROUGH_FLAGS: frozenset[str] = frozenset(
    {
        "-fms-extensions",
        "-fms-compatibility",
        "-fdelayed-template-parsing",
        "-fno-delayed-template-parsing",
        "-Wno-pragma-once-outside-header",
        "-Wno-microsoft-include",
        "-Wno-unknown-pragmas",
    }
)

_IGNORED_HARMLESS_FLAGS: frozenset[str] = frozenset(
    {
        "/nologo",
        "/c",
        "-c",
        "/W0",
        "/W1",
        "/W2",
        "/W3",
        "/W4",
        "/Wall",
        "/WX",
        "/WX-",
        "/Od",
        "/O1",
        "/O2",
        "/Ox",
        "/Ob0",
        "/Ob1",
        "/Ob2",
        "/Oi",
        "/Ot",
        "/Oy",
        "/Zi",
        "/Z7",
        "/Zd",
        "/MD",
        "/MDd",
        "/MT",
        "/MTd",
        "/LD",
        "/LDd",
        "/EHsc",
        "/EHa",
        "/EHs",
        "/GR",
        "/GR-",
        "/GS",
        "/GS-",
        "/Gy",
        "/Gy-",
        "/Gd",
        "/FS",
        "/MP",
        "/permissive-",
        "/permissive",
        "/Zc:inline",
        "/Zc:forScope",
        "/Zc:__cplusplus",
        "/utf-8",
        "-Wall",
        "-Wextra",
    }
)

_UNSUPPORTED_FATAL_FLAGS: frozenset[str] = frozenset(
    {
        "/clr",
        "/clr:pure",
        "/clr:safe",
        "/clr:netcore",
        "/ZW",
    }
)

_MISSING_HEADER_RE = re.compile(r"'([^']+)'\s+file not found", re.IGNORECASE)


class ClangParser:
    """Semantic C++ parser for LegacyGraph translation units using libclang."""

    def __init__(
        self,
        repo_root: str | Path,
        *,
        parser_version: str = CLANG_PARSER_VERSION,
        mfc_rule_version: str = DEFAULT_MFC_RULE_VERSION,
        preferred_legacy_encoding: str = "cp1252",
        parser_input_policy: str = "utf8-overlay",
    ) -> None:
        self.repo_root = resolve_repo_root(repo_root)
        self.parser_name = CLANG_PARSER_NAME
        self.preferred_legacy_encoding = encoding_name(preferred_legacy_encoding)
        self.parser_input_policy = parser_input_policy
        self.parser_version = policy_version(parser_version, preferred_legacy_encoding, parser_input_policy)
        self._source_buffers: dict[str, SourceBuffer] = {}
        self.mfc_rule_version = mfc_rule_version
        self.last_macro_offsets: dict[str, set[int]] = {}
        self.active_macro_offsets: dict[str, set[int]] = {}
        self.macro_offsets_by_tu: dict[str, dict[str, set[int]]] = {}
        self.last_consumed_files: set[str] = set()
        self.last_file_hashes: dict[str, str] = {}
        self._index: Any | None = None
        self._progress = None
        self._ast_paths: dict[str, str | None] | None = None

    def _ensure_index(self) -> Any:
        if self._index is not None:
            return self._index
        ok, reason = is_libclang_available()
        if not ok:
            raise LibclangUnavailableError(
                f"Semantic C++ parsing requires optional dependency 'libclang': {reason}"
            )
        self._index = cindex.Index.create()
        return self._index

    def _resolve_context_working_dir(self, spec: TranslationUnitSpec) -> Path:
        raw_wd = spec.build_context.working_directory or "."
        wd = Path(raw_wd)
        if not wd.is_absolute():
            wd = (self.repo_root / wd).resolve(strict=False)
        else:
            wd = wd.resolve(strict=False)
        return wd

    def _resolve_include_or_header_path(
        self,
        raw_path: str,
        *,
        working_dir: Path,
        command_origin: str,
        include_dirs: list[Path] | None = None,
    ) -> Path:
        cand = Path(raw_path)
        if cand.is_absolute():
            return cand.resolve(strict=False)
        if command_origin == "compile_commands":
            # load_compile_commands normalizes relative paths relative to repo_root
            p_root = (self.repo_root / cand).resolve(strict=False)
            if p_root.exists() or include_dirs is None:
                return p_root
        else:
            p_wd = (working_dir / cand).resolve(strict=False)
            if p_wd.exists():
                return p_wd
            p_root = (self.repo_root / cand).resolve(strict=False)
            if p_root.exists():
                return p_root
        if include_dirs:
            for inc in include_dirs:
                p_inc = (inc / cand).resolve(strict=False)
                if p_inc.exists():
                    return p_inc
        if command_origin == "compile_commands":
            return (self.repo_root / cand).resolve(strict=False)
        return (working_dir / cand).resolve(strict=False)

    @contextmanager
    def _ast_path_scope(self):
        """Cache exact path spellings only during this AST walk, never across TUs.

        Every cached result is checked again during source validation. Repointed
        symlinks, newly present paths and changed containment invalidate the TU.
        The cap bounds retention for unusually broad include graphs.
        """
        paths: dict[str, str | None] = {}
        self._ast_paths = paths
        try:
            yield paths
        finally:
            self._ast_paths = None

    def _try_repo_relative(self, path: str | Path) -> str | None:
        spelling = str(path)
        cache = self._ast_paths
        if cache is not None and spelling in cache:
            return cache[spelling]
        try:
            _, rel = ensure_contained_path(self.repo_root, path, allow_nonexistent=False)
            result = rel if rel != "." else None
        except Exception:
            result = None
        if cache is not None and len(cache) < 4096:
            cache[spelling] = result
        return result

    def _build_clang_args(
        self, spec: TranslationUnitSpec, main_span: SourceSpan | None
    ) -> tuple[list[str], list[TUDependencyRecord], list[DiagnosticRecord], bool]:
        ctx = spec.build_context
        working_dir = self._resolve_context_working_dir(spec)
        args: list[str] = ["-x", "c++"]
        forced_deps: list[TUDependencyRecord] = []
        diagnostics: list[DiagnosticRecord] = []
        fatal_config = False

        if spec.response_files:
            for rsp in spec.response_files:
                diagnostics.append(
                    DiagnosticRecord(
                        severity=DiagnosticSeverity.UNSUPPORTED,
                        code="clang.unsupported_response_file",
                        message=f"Response file '@{rsp}' is not expanded automatically; provide explicit manifest arguments.",
                        span=main_span,
                        parse_scope="tu",
                        unsupported_feature=f"@{rsp}",
                    )
                )
            fatal_config = True

        std_key = (ctx.language_standard or "c++17").lower().strip()
        args.append(_STD_MAP.get(std_key, "-std=c++17"))

        arch = (ctx.architecture or ctx.platform or "x64").lower()
        if ctx.compiler_family.lower() in ("msvc", "clang-cl"):
            if arch in ("win32", "x86", "i386", "i686"):
                args.extend(["--target=i686-pc-windows-msvc", "-fms-extensions", "-fms-compatibility"])
            else:
                args.extend(["--target=x86_64-pc-windows-msvc", "-fms-extensions", "-fms-compatibility"])
        args.append("-fno-delayed-template-parsing")
        args.append("-Wno-pragma-once-outside-header")

        resolved_include_dirs: list[Path] = []
        for raw_inc in (*ctx.include_dirs, *spec.per_file_include_dirs):
            inc_path = self._resolve_include_or_header_path(
                raw_inc, working_dir=working_dir, command_origin=spec.command_origin
            )
            resolved_include_dirs.append(inc_path)
            args.extend(["-I", str(inc_path)])

        for raw_sys in ctx.system_include_dirs:
            sys_path = self._resolve_include_or_header_path(
                raw_sys, working_dir=working_dir, command_origin=spec.command_origin
            )
            resolved_include_dirs.append(sys_path)
            args.extend(["-isystem", str(sys_path)])

        for d in (*ctx.defines, *spec.per_file_defines):
            args.append(f"-D{d}")
        for u in (*ctx.undefines, *spec.per_file_undefines):
            args.append(f"-U{u}")

        # Forced includes and /Yu or /Yc precompiled header source fallback (never binary MSVC .pch)
        forced_list: list[tuple[str, DependencyKind]] = [
            (fi, DependencyKind.FORCED_INCLUDE)
            for fi in (*ctx.forced_includes, *spec.per_file_forced_includes)
        ]
        if ctx.pch_header:
            forced_list.append((ctx.pch_header, DependencyKind.PCH_HEADER))

        for raw_hdr, dep_kind in forced_list:
            if raw_hdr.lower().endswith(".pch"):
                diagnostics.append(
                    DiagnosticRecord(
                        severity=DiagnosticSeverity.UNSUPPORTED,
                        code="clang.binary_pch_unsupported",
                        message=f"Binary MSVC precompiled header '{raw_hdr}' is unsupported; specify header source via pch_header or /Yu.",
                        span=main_span,
                        parse_scope="tu",
                        unsupported_feature=raw_hdr,
                    )
                )
                fatal_config = True
                continue
            hdr_path = self._resolve_include_or_header_path(
                raw_hdr,
                working_dir=working_dir,
                command_origin=spec.command_origin,
                include_dirs=resolved_include_dirs,
            )
            args.extend(["-include", str(hdr_path) if hdr_path.exists() else raw_hdr])
            rel_hdr = self._try_repo_relative(hdr_path) if hdr_path.exists() else None
            if rel_hdr is not None:
                forced_deps.append(
                    TUDependencyRecord(
                        repo_path=rel_hdr,
                        dependency_kind=dep_kind,
                        included_from_path=spec.main_file_path,
                        include_line=None,
                        is_missing=False,
                    )
                )
            elif not hdr_path.exists():
                forced_deps.append(
                    TUDependencyRecord(
                        repo_path="",
                        dependency_kind=dep_kind,
                        included_from_path=spec.main_file_path,
                        include_line=None,
                        is_missing=True,
                        missing_include_text=raw_hdr,
                    )
                )

        native_wchar = True
        all_extra_flags = (*ctx.extra_flags, *spec.per_file_flags)
        for flag in all_extra_flags:
            if not flag:
                continue
            if flag.startswith("@"):
                diagnostics.append(
                    DiagnosticRecord(
                        severity=DiagnosticSeverity.UNSUPPORTED,
                        code="clang.unsupported_response_file",
                        message=f"Response file flag '{flag}' is unsupported.",
                        span=main_span,
                        parse_scope="tu",
                        unsupported_feature=flag,
                    )
                )
                fatal_config = True
                continue
            if flag in _UNSUPPORTED_FATAL_FLAGS or flag.startswith("/clr"):
                diagnostics.append(
                    DiagnosticRecord(
                        severity=DiagnosticSeverity.UNSUPPORTED,
                        code="clang.unsupported_flag",
                        message=f"Unsupported compiler mode flag '{flag}'.",
                        span=main_span,
                        parse_scope="tu",
                        unsupported_feature=flag,
                    )
                )
                fatal_config = True
                continue
            if flag in ("/Zc:wchar_t", "/Zc:wchar_t-"):
                if ctx.compiler_family.lower() not in ("msvc", "clang-cl"):
                    diagnostics.append(DiagnosticRecord(
                        severity=DiagnosticSeverity.UNSUPPORTED,
                        code="clang.unsupported_wchar_context",
                        message="/Zc:wchar_t requires an explicit msvc or clang-cl Windows context.",
                        span=main_span, parse_scope="tu", unsupported_feature=flag,
                    ))
                    fatal_config = True
                else:
                    # Last context/per-file setting wins. Built-in wchar_t is
                    # Clang's default; there is no supported -fwchar inverse.
                    native_wchar = not flag.endswith("-")
                continue
            if flag.startswith("/Fp"):
                continue
            if flag in _IGNORED_HARMLESS_FLAGS or flag.startswith(("/Fo", "/Fd", "/Fe", "/Fa", "/FR")):
                continue
            if flag in _SAFE_PASSTHROUGH_FLAGS or flag.startswith("-fms-"):
                args.append(flag)
                continue
            diagnostics.append(
                DiagnosticRecord(
                    severity=DiagnosticSeverity.UNSUPPORTED,
                    code="clang.unsupported_flag",
                    message=f"Unrecognized or unsupported compiler flag '{flag}' rejected.",
                    span=main_span,
                    parse_scope="tu",
                    unsupported_feature=flag,
                )
            )
            fatal_config = True

        if not native_wchar:
            # Verified libclang 18.1.1 frontend option; driver -fno-wchar fails.
            args.extend(["-Xclang", "-fno-wchar"])
        return args, forced_deps, diagnostics, fatal_config

    def explain_context(self, spec: TranslationUnitSpec) -> dict[str, Any]:
        """Describe selected analysis settings without parsing source or exposing paths.

        These are command-line settings, not macro state after source/header
        preprocessing and not confirmation of the original build's settings.
        """
        args, _, diagnostics, fatal = self._build_clang_args(spec, None)
        wchar_settings = [(scope, flag)
                          for scope, flags in (("context", spec.build_context.extra_flags),
                                               ("translation_unit", spec.per_file_flags))
                          for flag in flags if flag in ("/Zc:wchar_t", "/Zc:wchar_t-")]
        macros = {}
        for name in ("UNICODE", "_UNICODE"):
            definitions = [arg[2:] for arg in args if arg.startswith("-D")
                           and arg[2:].split("=", 1)[0] == name]
            macros[name] = ("undefined" if "-U" + name in args else
                            "defined" if definitions else "unspecified")
        return {
            "configuration_supported": not fatal,
            "language_standard": next(arg[5:] for arg in args if arg.startswith("-std=")),
            "target": next((arg[9:] for arg in args if arg.startswith("--target=")), "host-default"),
            "wchar_mode": ("unknown" if fatal else "typedef" if "-fno-wchar" in args else "native"),
            "wchar_selection": "explicit" if wchar_settings else "default",
            "wchar_selection_scope": wchar_settings[-1][0] if wchar_settings else "default",
            "wchar_selected_flag": wchar_settings[-1][1] if wchar_settings else None,
            "command_line_macros": macros,
            "diagnostic_codes": sorted({d.code for d in diagnostics}),
            "source_preprocessing_verified": False,
            "original_build_match": "unknown",
        }

    def _phase(self, name, **fields):
        return (self._progress.phase(name, **fields) if self._progress is not None
                else nullcontext())

    def _original_offset(self, relative: str, offset: int) -> int:
        buffer = self._source_buffers.get(relative)
        if buffer is None or self.parser_input_policy == "native":
            return offset
        return buffer.original_offset(offset)

    def _decode_slice(self, relative: str, raw: bytes, start: int, end: int) -> str:
        buffer = self._source_buffers.get(relative)
        if buffer is None:
            buffer = SourceBuffer.create(raw, self.preferred_legacy_encoding)
        return buffer.text_slice(start, end)

    def _parse_input(self, idx, main_abs, args, options, originals, hashes):
        """Discover includes using Clang, then freeze contained source views.

        Reparse only when a consumed header needs a new UTF-8 overlay. Discovery
        diagnostics are never published. Each final consumed buffer is verified
        against its frozen view; original disk hashes remain the evidence basis.
        """
        mismatches = set()
        overlays = {}
        with self._phase("parser_input_preparation"):
            for relative, raw in originals.items():
                buffer = SourceBuffer.create(raw, self.preferred_legacy_encoding)
                self._source_buffers[relative] = buffer
                if self.parser_input_policy == "utf8-overlay":
                    overlays[str(self.repo_root / relative)] = buffer.parser
        for round_number in range(1, 65):
            with self._phase('native_parse', round=round_number, overlays=len(overlays)):
                if self._progress is not None:
                    self._progress.native_calls += 1
                tu = idx.parse(str(main_abs), args=args, options=options,
                               unsaved_files=list(overlays.items()))
            with self._phase("overlay_discovery"):
                consumed_paths = {str(main_abs)}
                consumed_paths.update(inc.include.name for inc in tu.get_includes()
                                      if inc.include is not None)
                changed = False
                for path in sorted(consumed_paths):
                    relative = self._try_repo_relative(path)
                    if relative is None:
                        continue  # Never transcode SDK/external headers.
                    consumed = _compiled_file_bytes(tu, Path(path))
                    if consumed is None and relative in originals:
                        mismatches.add(relative)
                        continue
                    if relative in originals:
                        buffer = self._source_buffers[relative]
                        expected = (buffer.parser if self.parser_input_policy == "utf8-overlay"
                                    else buffer.original)
                        if consumed != expected:
                            mismatches.add(relative)
                    else:
                        # Clang rejects UTF-16/32 BOMs before exposing a buffer. This
                        # round is discovery only: freeze disk bytes for the next
                        # overlay round, which must expose the exact UTF-8 view.
                        unavailable = consumed is None
                        raw = (self.repo_root / relative).read_bytes() if unavailable else consumed
                        originals[relative] = raw
                        hashes[relative] = hashlib.sha256(raw).hexdigest()
                        buffer = SourceBuffer.create(raw, self.preferred_legacy_encoding)
                        self._source_buffers[relative] = buffer
                        if self.parser_input_policy == "utf8-overlay" and (unavailable or buffer.parser != raw):
                            overlays[str(self.repo_root / relative)] = buffer.parser
                            changed = True
                        elif unavailable:
                            mismatches.add(relative)
            if not changed:
                return tu, mismatches
        raise ValueError("Parser input include discovery exceeded 64 overlay rounds")

    def parse(self, spec: TranslationUnitSpec) -> ParseRunPayload:
        """Parse a single translation unit with libclang and return its ParseRunPayload."""
        self.last_file_hashes = {}
        self._source_buffers = {}
        self.last_macro_offsets = {}
        self.last_consumed_files = set()
        idx = self._ensure_index()
        try:
            main_abs, main_rel = ensure_contained_path(
                self.repo_root, spec.main_file_path, allow_nonexistent=False
            )
        except Exception as exc:
            return ParseRunPayload(
                tu_key=spec.tu_key,
                parser_name=self.parser_name,
                parser_version=self.parser_version,
                mfc_rule_version=self.mfc_rule_version,
                parse_status=ParseStatus.FAILED,
                diagnostics=(
                    DiagnosticRecord(
                        severity=DiagnosticSeverity.FATAL,
                        code="clang.main_file_missing",
                        message=f"Main translation unit file unavailable: {exc}",
                        span=None,
                        parse_scope="tu",
                        missing_dependency=spec.main_file_path,
                    ),
                ),
                has_incomplete_dependencies=True,
            )

        pre_bytes_by_rel: dict[str, bytes] = {}
        pre_hash_by_rel: dict[str, str] = {}
        main_bytes = main_abs.read_bytes()
        pre_bytes_by_rel[main_rel] = main_bytes
        pre_hash_by_rel[main_rel] = hashlib.sha256(main_bytes).hexdigest()

        main_default_span = self._make_whole_file_span(main_rel, main_bytes)
        clang_args, forced_deps, config_diags, fatal_config = self._build_clang_args(
            spec, main_default_span
        )

        for fdep in forced_deps:
            if not fdep.is_missing and fdep.repo_path and fdep.repo_path not in pre_hash_by_rel:
                try:
                    p_abs, _ = ensure_contained_path(self.repo_root, fdep.repo_path)
                    b = p_abs.read_bytes()
                    pre_bytes_by_rel[fdep.repo_path] = b
                    pre_hash_by_rel[fdep.repo_path] = hashlib.sha256(b).hexdigest()
                except OSError:
                    pass

        if fatal_config:
            self.last_macro_offsets = {main_rel: set()}
            self.active_macro_offsets = self.last_macro_offsets
            self.macro_offsets_by_tu[spec.tu_key] = self.last_macro_offsets
            self.last_consumed_files = set(pre_hash_by_rel.keys())
            return ParseRunPayload(
                tu_key=spec.tu_key,
                parser_name=self.parser_name,
                parser_version=self.parser_version,
                mfc_rule_version=self.mfc_rule_version,
                parse_status=ParseStatus.FAILED,
                dependencies=tuple(forced_deps),
                diagnostics=tuple(config_diags),
                has_incomplete_dependencies=True,
            )

        options = cindex.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD
        try:
            tu, consumed_buffer_mismatches = self._parse_input(
                idx, main_abs, clang_args, options, pre_bytes_by_rel, pre_hash_by_rel
            )
        except Exception as exc:
            diag = DiagnosticRecord(
                severity=DiagnosticSeverity.FATAL,
                code="clang.unsupported_encoding" if isinstance(exc, (UnicodeError, ValueError, LookupError)) else "clang.parse_exception",
                message=f"libclang failed to parse '{main_rel}': {exc}",
                span=main_default_span,
                parse_scope="tu",
            )
            return ParseRunPayload(
                tu_key=spec.tu_key,
                parser_name=self.parser_name,
                parser_version=self.parser_version,
                mfc_rule_version=self.mfc_rule_version,
                parse_status=ParseStatus.FAILED,
                dependencies=tuple(forced_deps),
                diagnostics=(*config_diags, diag),
                has_incomplete_dependencies=True,
            )

        with self._phase("dependencies_diagnostics"):
            # Collect dependencies, diagnostics, and active macro offsets
            dep_map: dict[tuple[str, str, str | None, int | None, bool, str], TUDependencyRecord] = {}
            for fdep in forced_deps:
                key = (
                    fdep.repo_path,
                    fdep.dependency_kind.value,
                    fdep.included_from_path,
                    fdep.include_line,
                    fdep.is_missing,
                    fdep.missing_include_text,
                )
                dep_map[key] = fdep

            forced_repo_paths = {d.repo_path for d in forced_deps if not d.is_missing and d.repo_path}
            for inc in tu.get_includes():
                inc_file = inc.include
                if inc_file is None:
                    continue
                rel_inc = self._try_repo_relative(inc_file.name)
                if rel_inc is None or rel_inc == main_rel:
                    continue
                if rel_inc not in pre_hash_by_rel:
                    try:
                        inc_abs, _ = ensure_contained_path(self.repo_root, rel_inc)
                        raw_b = _compiled_file_bytes(tu, inc_abs)
                        if raw_b is None:
                            consumed_buffer_mismatches.add(rel_inc)
                            raw_b = inc_abs.read_bytes()
                        pre_bytes_by_rel[rel_inc] = raw_b
                        pre_hash_by_rel[rel_inc] = hashlib.sha256(raw_b).hexdigest()
                    except OSError:
                        pass
                src_rel = self._try_repo_relative(inc.source.name) if inc.source is not None else None
                if src_rel is None:
                    kind = (
                        DependencyKind.PCH_HEADER
                        if (spec.build_context.pch_header and rel_inc.endswith(spec.build_context.pch_header))
                        else DependencyKind.FORCED_INCLUDE
                    )
                    src_rel = main_rel
                    inc_line = None
                elif inc.depth == 1 and src_rel == main_rel and rel_inc not in forced_repo_paths:
                    kind = DependencyKind.DIRECT_INCLUDE
                    inc_line = int(inc.location.line) if inc.location.line else None
                elif rel_inc in forced_repo_paths:
                    continue
                else:
                    kind = DependencyKind.TRANSITIVE_INCLUDE
                    inc_line = int(inc.location.line) if inc.location.line else None

                rec = TUDependencyRecord(
                    repo_path=rel_inc,
                    dependency_kind=kind,
                    included_from_path=src_rel,
                    include_line=inc_line,
                    is_missing=False,
                )
                dep_map[
                    (
                        rec.repo_path,
                        rec.dependency_kind.value,
                        rec.included_from_path,
                        rec.include_line,
                        rec.is_missing,
                        rec.missing_include_text,
                    )
                ] = rec

            diagnostics: list[DiagnosticRecord] = list(config_diags)
            has_error_or_fatal = False
            has_incomplete_deps = any(d.is_missing for d in forced_deps)
            missing_forced_headers = {
                fd.missing_include_text for fd in forced_deps if fd.is_missing and fd.missing_include_text
            }

            for d in tu.diagnostics:
                sev_num = int(d.severity)
                if sev_num <= int(cindex.Diagnostic.Note):
                    sev = DiagnosticSeverity.INFO
                elif sev_num == int(cindex.Diagnostic.Warning):
                    sev = DiagnosticSeverity.WARNING
                elif sev_num == int(cindex.Diagnostic.Error):
                    sev = DiagnosticSeverity.ERROR
                    has_error_or_fatal = True
                else:
                    sev = DiagnosticSeverity.FATAL
                    has_error_or_fatal = True

                msg = str(d.spelling)
                missing_dep = ""
                m_inc = _MISSING_HEADER_RE.search(msg)
                if m_inc:
                    missing_dep = m_inc.group(1)
                    has_incomplete_deps = True

                diag_option = str(getattr(d, "option", "") or "").strip()
                d_span, loc_quality, loc_provenance = self._resolve_diagnostic_location(
                    d,
                    pre_bytes_by_rel,
                    main_rel,
                    main_default_span,
                    missing_dep=missing_dep,
                    missing_forced_headers=missing_forced_headers,
                )
                if missing_dep:
                    inc_from = d_span.repo_path if d_span else main_rel
                    inc_line = (
                        d_span.start_line
                        if (d_span and loc_quality == DiagnosticLocationQuality.EXACT.value)
                        else None
                    )
                    mrec = TUDependencyRecord(
                        repo_path="",
                        dependency_kind=DependencyKind.DIRECT_INCLUDE,
                        included_from_path=inc_from,
                        include_line=inc_line,
                        is_missing=True,
                        missing_include_text=missing_dep,
                    )
                    dep_map[
                        (
                            mrec.repo_path,
                            mrec.dependency_kind.value,
                            mrec.included_from_path,
                            mrec.include_line,
                            mrec.is_missing,
                            mrec.missing_include_text,
                        )
                    ] = mrec

                diagnostics.append(
                    DiagnosticRecord(
                        severity=sev,
                        code=str(d.category_name or "clang.diagnostic"),
                        message=msg,
                        span=d_span,
                        parse_scope="tu",
                        missing_dependency=missing_dep,
                        location_quality=loc_quality,
                        location_provenance=loc_provenance,
                        diagnostic_option=diag_option,
                    )
                )

        with self._ast_path_scope() as ast_paths, self._phase("ast_evidence"):
            # Extract active macro offsets andAST facts
            macro_offsets: dict[str, set[int]] = {rel: set() for rel in pre_bytes_by_rel}
            entities_by_key: dict[str, EntityRecord] = {}
            occurrences: list[SymbolOccurrenceRecord] = []
            observations: dict[str, ObservationRecord] = {}
            relations: list[RelationRecord] = []
            seen_occurrence_keys: set[tuple[str, str, str, int, int]] = set()
            seen_relation_keys: set[tuple[str, str | None, str, str, str, str, str, int, int]] = set()
            file_entity_keys: dict[str, str] = {}

            def ensure_file_entity(repo_path: str) -> str:
                if repo_path in file_entity_keys:
                    return file_entity_keys[repo_path]
                fkey = f"file:{repo_path}"
                entities_by_key[fkey] = EntityRecord(
                    semantic_key=fkey,
                    kind=EntityKind.FILE,
                    name=Path(repo_path).name,
                    qualified_name=repo_path,
                    linkage_scope=LinkageScope.PROJECT,
                    scope_qualifier=repo_path,
                    is_external=False,
                )
                file_entity_keys[repo_path] = fkey
                return fkey

            ensure_file_entity(main_rel)

            # Include relations from preprocessing records
            for dep in dep_map.values():
                if not dep.is_missing and dep.repo_path and dep.included_from_path:
                    src_f = ensure_file_entity(dep.included_from_path)
                    tgt_f = ensure_file_entity(dep.repo_path)
                    inc_span = self._make_line_span(
                        dep.included_from_path,
                        pre_bytes_by_rel.get(dep.included_from_path, b""),
                        dep.include_line or 1,
                    )
                    obs_key = f"obs:include:{dep.included_from_path}:{dep.include_line or 0}:{dep.repo_path}"
                    observations[obs_key] = ObservationRecord(
                        observation_key=obs_key,
                        observation_type="include_directive",
                        span=inc_span,
                        subject_semantic_key=src_f,
                        rule_id="clang.include",
                        parser_name=self.parser_name,
                        parser_version=self.parser_version,
                        details={"target_path": dep.repo_path, "kind": dep.dependency_kind.value},
                    )
                    relations.append(
                        RelationRecord(
                            source_semantic_key=src_f,
                            target_semantic_key=tgt_f,
                            kind=RelationKind.INCLUDES,
                            resolution_status=ResolutionStatus.RESOLVED,
                            dispatch_kind=DispatchKind.STATIC_INCLUDE,
                            extraction_quality=ExtractionQuality.SEMANTIC,
                            reason=ResolutionReason.EXACT_SEMANTIC,
                            evidence=(
                                RelationEvidenceRecord(
                                    observation_key=obs_key,
                                    span=inc_span,
                                    evidence_snippet_hash=hashlib.sha256(
                                        dep.repo_path.encode("utf-8")
                                    ).hexdigest()[:16],
                                    rule_id="clang.include",
                                    parser_name=self.parser_name,
                                    parser_version=self.parser_version,
                                ),
                            ),
                        )
                    )

            # Walk top-level and first-party cursors
            for top_cursor in tu.cursor.get_children():
                ckind = top_cursor.kind
                loc_file = top_cursor.location.file
                if loc_file is None:
                    continue
                rel_path = self._try_repo_relative(loc_file.name)
                if rel_path is None:
                    # Skip deep traversal of SDK/external system headers outside repo_root
                    continue
                if rel_path not in pre_bytes_by_rel:
                    try:
                        p_abs, _ = ensure_contained_path(self.repo_root, rel_path)
                        b = p_abs.read_bytes()
                        pre_bytes_by_rel[rel_path] = b
                        pre_hash_by_rel[rel_path] = hashlib.sha256(b).hexdigest()
                        macro_offsets.setdefault(rel_path, set())
                    except OSError:
                        continue

                if ckind in (
                    cindex.CursorKind.MACRO_INSTANTIATION,
                    cindex.CursorKind.MACRO_DEFINITION,
                ):
                    macro_offsets.setdefault(rel_path, set()).add(
                        self._original_offset(rel_path, int(top_cursor.extent.start.offset)))
                    continue
                if ckind == cindex.CursorKind.INCLUSION_DIRECTIVE:
                    continue

                if not has_error_or_fatal:
                    self._visit_cursor(
                        top_cursor,
                        spec=spec,
                        main_rel=main_rel,
                        enclosing_callable_key=None,
                        enclosing_entity_key=None,
                        pre_bytes_by_rel=pre_bytes_by_rel,
                        entities_by_key=entities_by_key,
                        occurrences=occurrences,
                        observations=observations,
                        relations=relations,
                        seen_occurrence_keys=seen_occurrence_keys,
                        seen_relation_keys=seen_relation_keys,
                        ensure_file_entity=ensure_file_entity,
                    )

        with self._phase("parser_source_validation"):
            # Verify source files did not mutate during parsing
            mutated_files: list[str] = list(consumed_buffer_mismatches)
            # Re-resolve cached spellings with the same canonical containment
            # check, including negatives. Never trust a path from an old walk.
            for spelling, expected in ast_paths.items():
                if self._try_repo_relative(spelling) != expected:
                    mutated_files.append(expected or "AST path containment changed")
            for rel_p, before_sha in list(pre_hash_by_rel.items()):
                try:
                    p_abs, _ = ensure_contained_path(self.repo_root, rel_p, allow_nonexistent=False)
                    after_bytes = p_abs.read_bytes()
                    after_sha = hashlib.sha256(after_bytes).hexdigest()
                    if after_sha != before_sha:
                        mutated_files.append(rel_p)
                except Exception:
                    mutated_files.append(rel_p)

        self.last_macro_offsets = macro_offsets
        self.active_macro_offsets = macro_offsets
        self.macro_offsets_by_tu[spec.tu_key] = macro_offsets
        self.last_consumed_files = set(pre_hash_by_rel.keys())
        self.last_file_hashes = dict(pre_hash_by_rel)

        if mutated_files:
            diagnostics.append(
                DiagnosticRecord(
                    severity=DiagnosticSeverity.ERROR,
                    code="clang.source_mutated_during_parse",
                    message=f"Source file(s) changed during parse: {', '.join(sorted(mutated_files))}",
                    span=main_default_span,
                    parse_scope="tu",
                )
            )
            return ParseRunPayload(
                tu_key=spec.tu_key,
                parser_name=self.parser_name,
                parser_version=self.parser_version,
                mfc_rule_version=self.mfc_rule_version,
                parse_status=ParseStatus.STALE,
                dependencies=tuple(dep_map.values()),
                diagnostics=tuple(diagnostics),
                has_incomplete_dependencies=True,
            )

        if has_error_or_fatal:
            # Do not publish recovered calls from broken TUs as clean verified facts
            return ParseRunPayload(
                tu_key=spec.tu_key,
                parser_name=self.parser_name,
                parser_version=self.parser_version,
                mfc_rule_version=self.mfc_rule_version,
                parse_status=ParseStatus.PARSED_WITH_ERRORS,
                dependencies=tuple(dep_map.values()),
                diagnostics=tuple(diagnostics),
                has_incomplete_dependencies=has_incomplete_deps,
            )

        return ParseRunPayload(
            tu_key=spec.tu_key,
            parser_name=self.parser_name,
            parser_version=self.parser_version,
            mfc_rule_version=self.mfc_rule_version,
            parse_status=ParseStatus.PARSED_CLEAN,
            entities=tuple(entities_by_key.values()),
            occurrences=tuple(occurrences),
            observations=tuple(observations.values()),
            relations=tuple(relations),
            dependencies=tuple(dep_map.values()),
            diagnostics=tuple(diagnostics),
            has_incomplete_dependencies=has_incomplete_deps,
        )

    def _visit_cursor(
        self,
        cursor: Any,
        *,
        spec: TranslationUnitSpec,
        main_rel: str,
        enclosing_callable_key: str | None,
        enclosing_entity_key: str | None,
        pre_bytes_by_rel: dict[str, bytes],
        entities_by_key: dict[str, EntityRecord],
        occurrences: list[SymbolOccurrenceRecord],
        observations: dict[str, ObservationRecord],
        relations: list[RelationRecord],
        seen_occurrence_keys: set[tuple[str, str, str, int, int]],
        seen_relation_keys: set[tuple[str, str | None, str, str, str, str, str, int, int]],
        ensure_file_entity: Any,
    ) -> None:
        loc_file = cursor.location.file
        if loc_file is None:
            return
        rel_path = self._try_repo_relative(loc_file.name)
        if rel_path is None:
            return
        if rel_path not in pre_bytes_by_rel:
            try:
                p_abs, _ = ensure_contained_path(self.repo_root, rel_path)
                pre_bytes_by_rel[rel_path] = p_abs.read_bytes()
            except OSError:
                return

        ckind = cursor.kind
        # Do not extract runtime calls from unevaluated operands (sizeof, alignof, noexcept) or parameter default arguments
        if ckind == cindex.CursorKind.PARM_DECL:
            return
        if ckind == cindex.CursorKind.CXX_UNARY_EXPR:
            toks = [t.spelling for t in cursor.get_tokens()]
            if toks and toks[0] in ("sizeof", "alignof", "_Alignof", "__alignof", "__alignof__", "noexcept"):
                return
        if ckind in (
            cindex.CursorKind.TYPE_ALIAS_DECL,
            cindex.CursorKind.TYPEDEF_DECL,
        ):
            # Still record the typedef entity below, but do not traverse child expressions as runtime calls
            pass

        next_callable_key = enclosing_callable_key
        next_entity_key = enclosing_entity_key

        # Handle lambda expression: create/resolve its operator() callable entity so calls inside lambda
        # are attributed to the lambda rather than the enclosing function.
        if ckind == cindex.CursorKind.LAMBDA_EXPR:
            lam_key = self._register_lambda_entity(
                cursor,
                spec=spec,
                main_rel=main_rel,
                rel_path=rel_path,
                enclosing_entity_key=enclosing_entity_key,
                pre_bytes_by_rel=pre_bytes_by_rel,
                entities_by_key=entities_by_key,
                occurrences=occurrences,
                seen_occurrence_keys=seen_occurrence_keys,
            )
            if lam_key is not None:
                next_callable_key = lam_key
                next_entity_key = lam_key

        elif self._is_entity_cursor_kind(ckind):
            ent = self._ensure_cursor_entity(
                cursor,
                spec=spec,
                main_rel=main_rel,
                entities_by_key=entities_by_key,
            )
            if ent is not None:
                next_entity_key = ent.semantic_key
                if ent.kind in (
                    EntityKind.FUNCTION,
                    EntityKind.METHOD,
                    EntityKind.CONSTRUCTOR,
                    EntityKind.DESTRUCTOR,
                ):
                    next_callable_key = ent.semantic_key

                span = self._cursor_span(cursor, rel_path, pre_bytes_by_rel[rel_path])
                if span is not None:
                    is_def = bool(cursor.is_definition())
                    if is_def:
                        occ_kind = OccurrenceKind.DEFINITION
                    elif ckind in (
                        cindex.CursorKind.CLASS_DECL,
                        cindex.CursorKind.STRUCT_DECL,
                        cindex.CursorKind.CLASS_TEMPLATE,
                    ) and not any(True for _ in cursor.get_children()):
                        occ_kind = OccurrenceKind.FORWARD_DECLARATION
                    else:
                        occ_kind = OccurrenceKind.DECLARATION

                    sig = self._cursor_signature(cursor, rel_path, pre_bytes_by_rel[rel_path], span)
                    occ_dedup = (
                        ent.semantic_key,
                        occ_kind.value,
                        span.repo_path,
                        span.byte_start,
                        span.byte_end,
                    )
                    if occ_dedup not in seen_occurrence_keys:
                        seen_occurrence_keys.add(occ_dedup)
                        occurrences.append(
                            SymbolOccurrenceRecord(
                                entity_semantic_key=ent.semantic_key,
                                occurrence_kind=occ_kind,
                                span=span,
                                signature=sig,
                                parent_semantic_key=ent.parent_semantic_key,
                                condition_state=ConditionState.ACTIVE,
                                extraction_quality=ExtractionQuality.SEMANTIC,
                            )
                        )

                    # Record DECLARED_IN / DEFINED_IN relation to the file entity
                    file_key = ensure_file_entity(rel_path)
                    rel_kind = RelationKind.DEFINED_IN if is_def else RelationKind.DECLARED_IN
                    self._add_structural_relation(
                        source_key=ent.semantic_key,
                        target_key=file_key,
                        kind=rel_kind,
                        span=span,
                        rule_id=f"clang.{rel_kind.value}",
                        obs_type="declaration_location",
                        observations=observations,
                        relations=relations,
                        seen_relation_keys=seen_relation_keys,
                    )

                    # Record MEMBER_OF if semantic parent is a class/struct
                    if ent.parent_semantic_key and ent.parent_semantic_key in entities_by_key:
                        parent_ent = entities_by_key[ent.parent_semantic_key]
                        if parent_ent.kind in (EntityKind.CLASS, EntityKind.STRUCT) and ent.kind in (
                            EntityKind.METHOD,
                            EntityKind.CONSTRUCTOR,
                            EntityKind.DESTRUCTOR,
                            EntityKind.FIELD,
                            EntityKind.VARIABLE,
                            EntityKind.CLASS,
                            EntityKind.STRUCT,
                            EntityKind.ENUM,
                            EntityKind.TYPEDEF,
                        ):
                            self._add_structural_relation(
                                source_key=ent.semantic_key,
                                target_key=parent_ent.semantic_key,
                                kind=RelationKind.MEMBER_OF,
                                span=span,
                                rule_id="clang.member_of",
                                obs_type="member_declaration",
                                observations=observations,
                                relations=relations,
                                seen_relation_keys=seen_relation_keys,
                            )

                    # Record OVERRIDES for virtual methods if exposed by libclang binding
                    if ckind == cindex.CursorKind.CXX_METHOD and hasattr(
                        cursor, "get_overridden_cursors"
                    ):
                        for overridden in cursor.get_overridden_cursors():
                            ov_ent = self._ensure_cursor_entity(
                                overridden,
                                spec=spec,
                                main_rel=main_rel,
                                entities_by_key=entities_by_key,
                            )
                            if ov_ent is not None:
                                self._add_structural_relation(
                                    source_key=ent.semantic_key,
                                    target_key=ov_ent.semantic_key,
                                    kind=RelationKind.OVERRIDES,
                                    span=span,
                                    rule_id="clang.overrides",
                                    obs_type="method_override",
                                    observations=observations,
                                    relations=relations,
                                    seen_relation_keys=seen_relation_keys,
                                )

        elif ckind == cindex.CursorKind.CXX_BASE_SPECIFIER:
            base_decl = cursor.referenced or cursor.type.get_declaration()
            if base_decl is not None and enclosing_entity_key is not None:
                base_ent = self._ensure_cursor_entity(
                    base_decl,
                    spec=spec,
                    main_rel=main_rel,
                    entities_by_key=entities_by_key,
                )
                span = self._cursor_span(cursor, rel_path, pre_bytes_by_rel[rel_path])
                if base_ent is not None and span is not None:
                    self._add_structural_relation(
                        source_key=enclosing_entity_key,
                        target_key=base_ent.semantic_key,
                        kind=RelationKind.INHERITS,
                        span=span,
                        rule_id="clang.inherits",
                        obs_type="base_specifier",
                        observations=observations,
                        relations=relations,
                        seen_relation_keys=seen_relation_keys,
                    )

        elif ckind == cindex.CursorKind.CALL_EXPR:
            caller_key = enclosing_callable_key or enclosing_entity_key
            if caller_key is not None:
                self._extract_call_expr(
                    cursor,
                    caller_key=caller_key,
                    spec=spec,
                    main_rel=main_rel,
                    rel_path=rel_path,
                    raw_bytes=pre_bytes_by_rel[rel_path],
                    entities_by_key=entities_by_key,
                    occurrences=occurrences,
                    observations=observations,
                    relations=relations,
                    seen_occurrence_keys=seen_occurrence_keys,
                    seen_relation_keys=seen_relation_keys,
                )

        if ckind in (
            cindex.CursorKind.TYPE_ALIAS_DECL,
            cindex.CursorKind.TYPEDEF_DECL,
        ):
            return

        var_decl_name_offset = (
            int(cursor.location.offset)
            if ckind in (cindex.CursorKind.VAR_DECL, cindex.CursorKind.FIELD_DECL)
            else None
        )
        for child in cursor.get_children():
            if (
                var_decl_name_offset is not None
                and child.extent is not None
                and int(child.extent.end.offset) <= var_decl_name_offset
            ):
                # Skip type-specifier expressions (such as decltype(expr)) preceding the variable/field identifier
                continue
            self._visit_cursor(
                child,
                spec=spec,
                main_rel=main_rel,
                enclosing_callable_key=next_callable_key,
                enclosing_entity_key=next_entity_key,
                pre_bytes_by_rel=pre_bytes_by_rel,
                entities_by_key=entities_by_key,
                occurrences=occurrences,
                observations=observations,
                relations=relations,
                seen_occurrence_keys=seen_occurrence_keys,
                seen_relation_keys=seen_relation_keys,
                ensure_file_entity=ensure_file_entity,
            )

    def _extract_call_expr(
        self,
        cursor: Any,
        *,
        caller_key: str,
        spec: TranslationUnitSpec,
        main_rel: str,
        rel_path: str,
        raw_bytes: bytes,
        entities_by_key: dict[str, EntityRecord],
        occurrences: list[SymbolOccurrenceRecord],
        observations: dict[str, ObservationRecord],
        relations: list[RelationRecord],
        seen_occurrence_keys: set[tuple[str, str, str, int, int]],
        seen_relation_keys: set[tuple[str, str | None, str, str, str, str, str, int, int]],
    ) -> None:
        span = self._cursor_span(cursor, rel_path, raw_bytes)
        if span is None:
            return
        snippet_bytes = raw_bytes[span.byte_start : span.byte_end]
        snippet_text = self._decode_slice(rel_path, raw_bytes, span.byte_start, span.byte_end).strip()
        snippet_hash = hashlib.sha256(snippet_bytes).hexdigest()[:16]

        ref = cursor.referenced
        children = list(cursor.get_children())
        first_child = children[0] if children else None

        # Ignore trivial built-in/implicit empty constructor invocations on value initialization if zero-width
        if span.byte_end <= span.byte_start:
            return

        # Case 1: Function pointer or variable/parameter/field call target
        if ref is not None and ref.kind in (
            cindex.CursorKind.VAR_DECL,
            cindex.CursorKind.PARM_DECL,
            cindex.CursorKind.FIELD_DECL,
            getattr(cindex.CursorKind, "TEMPLATE_NON_TYPE_PARAMETER", cindex.CursorKind.PARM_DECL),
        ):
            expr_text = snippet_text or str(ref.spelling or "indirect_call")
            obs_key = f"obs:call:{rel_path}:{span.byte_start}:{span.byte_end}:indirect"
            observations[obs_key] = ObservationRecord(
                observation_key=obs_key,
                observation_type="call_expr",
                span=span,
                subject_semantic_key=caller_key,
                rule_id="clang.indirect_call",
                parser_name=self.parser_name,
                parser_version=self.parser_version,
                details={
                    "dispatch": DispatchKind.INDIRECT.value,
                    "pointer_symbol": str(ref.spelling or ""),
                    "expr": expr_text,
                },
            )
            r_dedup = (
                caller_key,
                None,
                RelationKind.CALLS.value,
                ResolutionStatus.UNRESOLVED.value,
                DispatchKind.INDIRECT.value,
                ResolutionReason.INDIRECT_FUNCTION_POINTER.value,
                expr_text,
                span.byte_start,
                span.byte_end,
            )
            if r_dedup not in seen_relation_keys:
                seen_relation_keys.add(r_dedup)
                relations.append(
                    RelationRecord(
                        source_semantic_key=caller_key,
                        target_semantic_key=None,
                        kind=RelationKind.CALLS,
                        resolution_status=ResolutionStatus.UNRESOLVED,
                        dispatch_kind=DispatchKind.INDIRECT,
                        extraction_quality=ExtractionQuality.SEMANTIC,
                        condition_state=ConditionState.ACTIVE,
                        reason=ResolutionReason.INDIRECT_FUNCTION_POINTER,
                        unresolved_target_expr=expr_text,
                        evidence=(
                            RelationEvidenceRecord(
                                observation_key=obs_key,
                                span=span,
                                evidence_snippet_hash=snippet_hash,
                                rule_id="clang.indirect_call",
                                parser_name=self.parser_name,
                                parser_version=self.parser_version,
                            ),
                        ),
                    )
                )
            return

        # Case 2: ref is None (either dependent template call, indirect dereferenced function pointer, or unknown receiver)
        if ref is None:
            is_indirect_ptr = False
            if first_child is not None:
                fc_type = str(first_child.type.spelling or "")
                if "(*)" in fc_type or (
                    first_child.type.kind == cindex.TypeKind.POINTER
                    and first_child.type.get_pointee().kind
                    in (cindex.TypeKind.FUNCTIONPROTO, cindex.TypeKind.FUNCTIONNOPROTO)
                ):
                    is_indirect_ptr = True
                else:
                    for desc in first_child.walk_preorder():
                        if desc.referenced is not None and desc.referenced.kind in (
                            cindex.CursorKind.VAR_DECL,
                            cindex.CursorKind.PARM_DECL,
                            cindex.CursorKind.FIELD_DECL,
                        ):
                            is_indirect_ptr = True
                            break

            if is_indirect_ptr:
                dispatch = DispatchKind.INDIRECT
                reason = ResolutionReason.INDIRECT_FUNCTION_POINTER
                rule_id = "clang.indirect_call"
            elif cursor.type.kind == cindex.TypeKind.DEPENDENT or (
                first_child is not None and first_child.type.kind == cindex.TypeKind.DEPENDENT
            ):
                dispatch = DispatchKind.DIRECT
                reason = ResolutionReason.DEPENDENT_TEMPLATE
                rule_id = "clang.dependent_call"
            else:
                dispatch = DispatchKind.DIRECT
                reason = ResolutionReason.UNKNOWN_RECEIVER
                rule_id = "clang.unresolved_call"

            expr_text = snippet_text or str(cursor.spelling or "unresolved_call")
            obs_key = f"obs:call:{rel_path}:{span.byte_start}:{span.byte_end}:unresolved"
            observations[obs_key] = ObservationRecord(
                observation_key=obs_key,
                observation_type="call_expr",
                span=span,
                subject_semantic_key=caller_key,
                rule_id=rule_id,
                parser_name=self.parser_name,
                parser_version=self.parser_version,
                details={"reason": reason.value, "expr": expr_text},
            )
            r_dedup = (
                caller_key,
                None,
                RelationKind.CALLS.value,
                ResolutionStatus.UNRESOLVED.value,
                dispatch.value,
                reason.value,
                expr_text,
                span.byte_start,
                span.byte_end,
            )
            if r_dedup not in seen_relation_keys:
                seen_relation_keys.add(r_dedup)
                relations.append(
                    RelationRecord(
                        source_semantic_key=caller_key,
                        target_semantic_key=None,
                        kind=RelationKind.CALLS,
                        resolution_status=ResolutionStatus.UNRESOLVED,
                        dispatch_kind=dispatch,
                        extraction_quality=ExtractionQuality.SEMANTIC,
                        condition_state=ConditionState.ACTIVE,
                        reason=reason,
                        unresolved_target_expr=expr_text,
                        evidence=(
                            RelationEvidenceRecord(
                                observation_key=obs_key,
                                span=span,
                                evidence_snippet_hash=snippet_hash,
                                rule_id=rule_id,
                                parser_name=self.parser_name,
                                parser_version=self.parser_version,
                            ),
                        ),
                    )
                )
            return

        # Case 3: Referenced cursor is a callable function/method/constructor/destructor/template
        target_ent = self._ensure_cursor_entity(
            ref,
            spec=spec,
            main_rel=main_rel,
            entities_by_key=entities_by_key,
        )
        if target_ent is None:
            return

        is_virtual_call = False
        if ref.kind == cindex.CursorKind.CXX_METHOD and bool(ref.is_virtual_method()):
            # Check if explicitly qualified with Class::method() which suppresses virtual dispatch in C++
            explicitly_qualified = False
            if first_child is not None and first_child.kind == cindex.CursorKind.MEMBER_REF_EXPR:
                fc_children = list(first_child.get_children())
                has_type_ref = any(c.kind == cindex.CursorKind.TYPE_REF for c in fc_children)
                if has_type_ref:
                    toks = [t.spelling for t in first_child.get_tokens()]
                    if "::" in toks:
                        explicitly_qualified = True
            if not explicitly_qualified:
                is_virtual_call = True

        dispatch = DispatchKind.VIRTUAL if is_virtual_call else DispatchKind.DIRECT
        reason = (
            ResolutionReason.STATIC_VIRTUAL_TARGET
            if is_virtual_call
            else ResolutionReason.EXACT_SEMANTIC
        )
        rule_id = "clang.virtual_call" if is_virtual_call else "clang.direct_call"

        # Record symbol reference occurrence at the callsite
        occ_dedup = (
            target_ent.semantic_key,
            OccurrenceKind.REFERENCE.value,
            span.repo_path,
            span.byte_start,
            span.byte_end,
        )
        if occ_dedup not in seen_occurrence_keys:
            seen_occurrence_keys.add(occ_dedup)
            occurrences.append(
                SymbolOccurrenceRecord(
                    entity_semantic_key=target_ent.semantic_key,
                    occurrence_kind=OccurrenceKind.REFERENCE,
                    span=span,
                    signature=snippet_text,
                    parent_semantic_key=caller_key,
                    condition_state=ConditionState.ACTIVE,
                    extraction_quality=ExtractionQuality.SEMANTIC,
                )
            )

        obs_key = f"obs:call:{rel_path}:{span.byte_start}:{span.byte_end}:{target_ent.semantic_key}"
        observations[obs_key] = ObservationRecord(
            observation_key=obs_key,
            observation_type="call_expr",
            span=span,
            subject_semantic_key=caller_key,
            rule_id=rule_id,
            parser_name=self.parser_name,
            parser_version=self.parser_version,
            details={
                "target": target_ent.semantic_key,
                "dispatch": dispatch.value,
                "reason": reason.value,
            },
        )
        r_dedup = (
            caller_key,
            target_ent.semantic_key,
            RelationKind.CALLS.value,
            ResolutionStatus.RESOLVED.value,
            dispatch.value,
            reason.value,
            "",
            span.byte_start,
            span.byte_end,
        )
        if r_dedup not in seen_relation_keys:
            seen_relation_keys.add(r_dedup)
            relations.append(
                RelationRecord(
                    source_semantic_key=caller_key,
                    target_semantic_key=target_ent.semantic_key,
                    kind=RelationKind.CALLS,
                    resolution_status=ResolutionStatus.RESOLVED,
                    dispatch_kind=dispatch,
                    extraction_quality=ExtractionQuality.SEMANTIC,
                    condition_state=ConditionState.ACTIVE,
                    reason=reason,
                    evidence=(
                        RelationEvidenceRecord(
                            observation_key=obs_key,
                            span=span,
                            evidence_snippet_hash=snippet_hash,
                            rule_id=rule_id,
                            parser_name=self.parser_name,
                            parser_version=self.parser_version,
                        ),
                    ),
                )
            )

    def _add_structural_relation(
        self,
        *,
        source_key: str,
        target_key: str,
        kind: RelationKind,
        span: SourceSpan,
        rule_id: str,
        obs_type: str,
        observations: dict[str, ObservationRecord],
        relations: list[RelationRecord],
        seen_relation_keys: set[tuple[str, str | None, str, str, str, str, str, int, int]],
    ) -> None:
        obs_key = f"obs:{kind.value}:{span.repo_path}:{span.byte_start}:{source_key}->{target_key}"
        observations[obs_key] = ObservationRecord(
            observation_key=obs_key,
            observation_type=obs_type,
            span=span,
            subject_semantic_key=source_key,
            rule_id=rule_id,
            parser_name=self.parser_name,
            parser_version=self.parser_version,
            details={"target": target_key},
        )
        r_dedup = (
            source_key,
            target_key,
            kind.value,
            ResolutionStatus.RESOLVED.value,
            DispatchKind.STRUCTURAL.value,
            ResolutionReason.EXACT_SEMANTIC.value,
            "",
            span.byte_start,
            span.byte_end,
        )
        if r_dedup not in seen_relation_keys:
            seen_relation_keys.add(r_dedup)
            relations.append(
                RelationRecord(
                    source_semantic_key=source_key,
                    target_semantic_key=target_key,
                    kind=kind,
                    resolution_status=ResolutionStatus.RESOLVED,
                    dispatch_kind=DispatchKind.STRUCTURAL,
                    extraction_quality=ExtractionQuality.SEMANTIC,
                    condition_state=ConditionState.ACTIVE,
                    reason=ResolutionReason.EXACT_SEMANTIC,
                    evidence=(
                        RelationEvidenceRecord(
                            observation_key=obs_key,
                            span=span,
                            evidence_snippet_hash=hashlib.sha256(
                                f"{source_key}->{target_key}".encode("utf-8")
                            ).hexdigest()[:16],
                            rule_id=rule_id,
                            parser_name=self.parser_name,
                            parser_version=self.parser_version,
                        ),
                    ),
                )
            )

    def _register_lambda_entity(
        self,
        cursor: Any,
        *,
        spec: TranslationUnitSpec,
        main_rel: str,
        rel_path: str,
        enclosing_entity_key: str | None,
        pre_bytes_by_rel: dict[str, bytes],
        entities_by_key: dict[str, EntityRecord],
        occurrences: list[SymbolOccurrenceRecord],
        seen_occurrence_keys: set[tuple[str, str, str, int, int]],
    ) -> str | None:
        span = self._cursor_span(cursor, rel_path, pre_bytes_by_rel[rel_path])
        if span is None:
            return None
        type_decl = cursor.type.get_declaration() if cursor.type else None
        op_cursor = None
        if type_decl is not None and type_decl.kind != cindex.CursorKind.NO_DECL_FOUND:
            for ch in type_decl.get_children():
                if ch.kind == cindex.CursorKind.CXX_METHOD and ch.spelling == "operator()":
                    op_cursor = ch
                    break
        if op_cursor is not None:
            ent = self._ensure_cursor_entity(
                op_cursor,
                spec=spec,
                main_rel=main_rel,
                entities_by_key=entities_by_key,
                override_parent_key=enclosing_entity_key,
            )
            if ent is not None:
                occ_dedup = (
                    ent.semantic_key,
                    OccurrenceKind.DEFINITION.value,
                    span.repo_path,
                    span.byte_start,
                    span.byte_end,
                )
                if occ_dedup not in seen_occurrence_keys:
                    seen_occurrence_keys.add(occ_dedup)
                    occurrences.append(
                        SymbolOccurrenceRecord(
                            entity_semantic_key=ent.semantic_key,
                            occurrence_kind=OccurrenceKind.DEFINITION,
                            span=span,
                            signature="lambda operator()",
                            parent_semantic_key=enclosing_entity_key,
                        )
                    )
                return ent.semantic_key

        sem_key = f"tu:{spec.build_context.project_name}:{main_rel}:lambda@{rel_path}:{span.byte_start}"
        ent = EntityRecord(
            semantic_key=sem_key,
            kind=EntityKind.FUNCTION,
            name="operator()",
            qualified_name=f"<lambda@{rel_path}:{span.start_line}>::operator()",
            linkage_scope=LinkageScope.INTERNAL_TU,
            scope_qualifier=main_rel,
            parent_semantic_key=enclosing_entity_key,
            is_external=False,
        )
        entities_by_key[sem_key] = ent
        return sem_key

    def _is_entity_cursor_kind(self, ckind: Any) -> bool:
        return ckind in (
            cindex.CursorKind.NAMESPACE,
            cindex.CursorKind.CLASS_DECL,
            cindex.CursorKind.STRUCT_DECL,
            cindex.CursorKind.CLASS_TEMPLATE,
            cindex.CursorKind.ENUM_DECL,
            cindex.CursorKind.FUNCTION_DECL,
            cindex.CursorKind.FUNCTION_TEMPLATE,
            cindex.CursorKind.CXX_METHOD,
            cindex.CursorKind.CONSTRUCTOR,
            cindex.CursorKind.DESTRUCTOR,
            cindex.CursorKind.FIELD_DECL,
            cindex.CursorKind.TYPEDEF_DECL,
            cindex.CursorKind.TYPE_ALIAS_DECL,
            cindex.CursorKind.VAR_DECL,
        )

    def _map_cursor_kind(self, ckind: Any) -> EntityKind | None:
        if ckind == cindex.CursorKind.NAMESPACE:
            return EntityKind.NAMESPACE
        if ckind in (cindex.CursorKind.CLASS_DECL, cindex.CursorKind.CLASS_TEMPLATE):
            return EntityKind.CLASS
        if ckind == cindex.CursorKind.STRUCT_DECL:
            return EntityKind.STRUCT
        if ckind == cindex.CursorKind.ENUM_DECL:
            return EntityKind.ENUM
        if ckind in (cindex.CursorKind.FUNCTION_DECL, cindex.CursorKind.FUNCTION_TEMPLATE):
            return EntityKind.FUNCTION
        if ckind == cindex.CursorKind.CXX_METHOD:
            return EntityKind.METHOD
        if ckind == cindex.CursorKind.CONSTRUCTOR:
            return EntityKind.CONSTRUCTOR
        if ckind == cindex.CursorKind.DESTRUCTOR:
            return EntityKind.DESTRUCTOR
        if ckind == cindex.CursorKind.FIELD_DECL:
            return EntityKind.FIELD
        if ckind in (cindex.CursorKind.TYPEDEF_DECL, cindex.CursorKind.TYPE_ALIAS_DECL):
            return EntityKind.TYPEDEF
        if ckind == cindex.CursorKind.VAR_DECL:
            return EntityKind.VARIABLE
        return None

    def _ensure_cursor_entity(
        self,
        cursor: Any,
        *,
        spec: TranslationUnitSpec,
        main_rel: str,
        entities_by_key: dict[str, EntityRecord],
        override_parent_key: str | None = None,
    ) -> EntityRecord | None:
        canonical = cursor.canonical if cursor.canonical is not None else cursor
        ckind = canonical.kind
        ent_kind = self._map_cursor_kind(ckind)
        if ent_kind is None:
            return None

        # Skip local function variables (keep global/namespace/class-static variables)
        if ckind == cindex.CursorKind.VAR_DECL:
            sem_parent = canonical.semantic_parent
            if sem_parent is not None and sem_parent.kind in (
                cindex.CursorKind.FUNCTION_DECL,
                cindex.CursorKind.FUNCTION_TEMPLATE,
                cindex.CursorKind.CXX_METHOD,
                cindex.CursorKind.CONSTRUCTOR,
                cindex.CursorKind.DESTRUCTOR,
                cindex.CursorKind.LAMBDA_EXPR,
            ):
                return None

        usr = str(canonical.get_usr() or cursor.get_usr() or "")
        if not usr:
            return None

        loc_file = canonical.location.file or cursor.location.file
        rel_loc = self._try_repo_relative(loc_file.name) if loc_file is not None else None
        is_external = rel_loc is None
        if is_external and ent_kind not in (
            EntityKind.FUNCTION,
            EntityKind.METHOD,
            EntityKind.CONSTRUCTOR,
            EntityKind.DESTRUCTOR,
            EntityKind.CLASS,
            EntityKind.STRUCT,
        ):
            ent_kind = EntityKind.EXTERNAL_SYMBOL

        linkage, scope_qual, sem_key = self._compute_identity(
            canonical,
            usr=usr,
            spec=spec,
            main_rel=main_rel,
        )

        if sem_key in entities_by_key:
            return entities_by_key[sem_key]

        parent_key = override_parent_key
        sem_parent = canonical.semantic_parent
        if (
            parent_key is None
            and sem_parent is not None
            and sem_parent.kind != cindex.CursorKind.TRANSLATION_UNIT
            and self._is_entity_cursor_kind(sem_parent.kind)
        ):
            parent_ent = self._ensure_cursor_entity(
                sem_parent,
                spec=spec,
                main_rel=main_rel,
                entities_by_key=entities_by_key,
            )
            if parent_ent is not None:
                parent_key = parent_ent.semantic_key

        name = str(canonical.spelling or cursor.spelling or "<anonymous>")
        qual_name = self._build_qualified_name(canonical, name)

        ent = EntityRecord(
            semantic_key=sem_key,
            kind=ent_kind,
            name=name,
            qualified_name=qual_name,
            linkage_scope=linkage,
            scope_qualifier=scope_qual,
            parent_semantic_key=parent_key,
            is_external=is_external,
        )
        entities_by_key[sem_key] = ent
        return ent

    def _compute_identity(
        self,
        cursor: Any,
        *,
        usr: str,
        spec: TranslationUnitSpec,
        main_rel: str,
    ) -> tuple[LinkageScope, str, str]:
        project = spec.build_context.project_name
        is_internal = (
            cursor.linkage == cindex.LinkageKind.INTERNAL
            or "@aN@" in usr
            or usr.endswith("@aN")
            or "@Sa@" in usr
            or usr.endswith("@Sa")
        )
        cur_p = cursor.semantic_parent
        while cur_p is not None and cur_p.kind != cindex.CursorKind.TRANSLATION_UNIT:
            if cur_p.linkage == cindex.LinkageKind.INTERNAL or (
                cur_p.kind == cindex.CursorKind.NAMESPACE and not cur_p.spelling
            ):
                is_internal = True
                break
            cur_p = cur_p.semantic_parent

        if is_internal:
            return (
                LinkageScope.INTERNAL_TU,
                main_rel,
                f"tu:{project}:{main_rel}:{usr}",
            )
        return (
            LinkageScope.EXTERNAL,
            project,
            f"usr:{project}:{usr}",
        )

    def _build_qualified_name(self, cursor: Any, fallback_name: str) -> str:
        parts: list[str] = []
        cur = cursor
        while cur is not None and cur.kind != cindex.CursorKind.TRANSLATION_UNIT:
            sp = str(cur.spelling or "")
            if cur.kind == cindex.CursorKind.NAMESPACE and not sp:
                parts.append("(anonymous namespace)")
            elif sp:
                parts.append(sp)
            cur = cur.semantic_parent
        if not parts:
            return fallback_name
        parts.reverse()
        return "::".join(parts)

    def _cursor_span(self, cursor: Any, rel_path: str, raw_bytes: bytes) -> SourceSpan | None:
        ext = cursor.extent
        if ext is None or ext.start.file is None or ext.end.file is None:
            return None
        start_rel = self._try_repo_relative(ext.start.file.name)
        end_rel = self._try_repo_relative(ext.end.file.name)
        if start_rel != rel_path or end_rel != rel_path:
            return None
        b_start = self._original_offset(rel_path, int(ext.start.offset))
        b_end = max(b_start, self._original_offset(rel_path, int(ext.end.offset)))
        buffer = self._source_buffers.get(rel_path)
        if buffer is not None:
            s_line, s_col = buffer.position(b_start)
            e_line, e_col = buffer.position(b_end)
        else:
            s_line, s_col = max(1, int(ext.start.line)), max(1, int(ext.start.column))
            e_line, e_col = max(s_line, int(ext.end.line)), max(1, int(ext.end.column))
        return SourceSpan(
            repo_path=rel_path,
            byte_start=b_start,
            byte_end=b_end,
            start_line=s_line,
            start_col=s_col,
            end_line=e_line,
            end_col=e_col,
        )

    def _cursor_signature(
        self, cursor: Any, rel_path: str, raw_bytes: bytes, span: SourceSpan
    ) -> str:
        text = self._decode_slice(rel_path, raw_bytes, span.byte_start, span.byte_end)[:512]
        brace_idx = text.find("{")
        if brace_idx != -1:
            text = text[:brace_idx]
        semi_idx = text.find(";")
        if semi_idx != -1:
            text = text[:semi_idx]
        compact = " ".join(text.split()).strip()
        if compact:
            return compact[:240]
        return str(cursor.displayname or cursor.spelling or "")

    def _make_whole_file_span(self, repo_path: str, raw_bytes: bytes) -> SourceSpan:
        line_count = max(1, raw_bytes.count(b"\n") + 1)
        return SourceSpan(
            repo_path=repo_path,
            byte_start=0,
            byte_end=len(raw_bytes),
            start_line=1,
            start_col=1,
            end_line=line_count,
            end_col=1,
        )

    def _make_line_span(self, repo_path: str, raw_bytes: bytes, line_no: int) -> SourceSpan:
        buffer = self._source_buffers.get(repo_path)
        if buffer is None:
            try:
                buffer = SourceBuffer.create(raw_bytes, self.preferred_legacy_encoding)
            except (UnicodeError, ValueError, LookupError):
                return self._make_whole_file_span(repo_path, raw_bytes)
        starts = buffer.line_starts
        idx = max(0, min(line_no - 1, len(starts) - 1))
        start_b = starts[idx]
        end_b = starts[idx + 1] if idx + 1 < len(starts) else len(raw_bytes)
        end_line, end_col = buffer.position(end_b)
        return SourceSpan(repo_path, start_b, end_b, idx + 1, 1, end_line, end_col)

    def _resolve_diagnostic_location(
        self,
        diag: Any,
        pre_bytes_by_rel: dict[str, bytes],
        main_rel: str,
        fallback: SourceSpan,
        *,
        missing_dep: str = "",
        missing_forced_headers: set[str] | None = None,
    ) -> tuple[SourceSpan, str, str]:
        loc = getattr(diag, "location", None)
        loc_file = getattr(loc, "file", None) if loc is not None else None
        if loc is None or loc_file is None:
            provenance = (
                DiagnosticLocationProvenance.MISSING_FILE.value
                if missing_dep
                else DiagnosticLocationProvenance.MISSING_LOCATION.value
            )
            return fallback, DiagnosticLocationQuality.FALLBACK.value, provenance

        raw_name = str(getattr(loc_file, "name", "") or "").strip()
        if not raw_name or (raw_name.startswith("<") and raw_name.endswith(">")):
            provenance = (
                DiagnosticLocationProvenance.MISSING_FILE.value
                if missing_dep
                else DiagnosticLocationProvenance.MISSING_LOCATION.value
            )
            return fallback, DiagnosticLocationQuality.FALLBACK.value, provenance

        if (
            missing_dep
            and missing_forced_headers
            and int(getattr(loc, "offset", 0) or 0) == 0
            and (
                missing_dep in missing_forced_headers
                or any(fh.endswith(missing_dep) for fh in missing_forced_headers)
            )
        ):
            return (
                fallback,
                DiagnosticLocationQuality.FALLBACK.value,
                DiagnosticLocationProvenance.MISSING_FILE.value,
            )

        rel_p = self._try_repo_relative(raw_name)
        if rel_p is not None:
            raw_b = pre_bytes_by_rel.get(rel_p)
            if raw_b is None:
                try:
                    p_abs, _ = ensure_contained_path(
                        self.repo_root, rel_p, allow_nonexistent=False
                    )
                    raw_b = p_abs.read_bytes()
                    pre_bytes_by_rel[rel_p] = raw_b
                except Exception:
                    return (
                        fallback,
                        DiagnosticLocationQuality.FALLBACK.value,
                        DiagnosticLocationProvenance.MISSING_FILE.value,
                    )
            b_off = self._original_offset(rel_p, int(loc.offset))
            buffer = self._source_buffers.get(rel_p)
            if buffer is not None:
                line, col = buffer.position(b_off)
            else:
                line, col = max(1, int(loc.line)), max(1, int(loc.column))
            return (
                SourceSpan(
                    repo_path=rel_p,
                    byte_start=b_off,
                    byte_end=b_off,
                    start_line=line,
                    start_col=col,
                    end_line=line,
                    end_col=col,
                ),
                DiagnosticLocationQuality.EXACT.value,
                DiagnosticLocationProvenance.CONTAINED_FILE.value,
            )

        try:
            cand = Path(raw_name)
            if not cand.is_absolute():
                cand = self.repo_root / cand
            resolved = cand.resolve(strict=True)
            exists_outside = resolved.is_file()
        except Exception:
            exists_outside = False

        provenance = (
            DiagnosticLocationProvenance.EXTERNAL_FILE.value
            if exists_outside
            else DiagnosticLocationProvenance.MISSING_FILE.value
        )
        return fallback, DiagnosticLocationQuality.FALLBACK.value, provenance

    def _diagnostic_span(
        self,
        diag: Any,
        pre_bytes_by_rel: dict[str, bytes],
        main_rel: str,
        fallback: SourceSpan,
    ) -> SourceSpan:
        loc = diag.location
        if loc is not None and loc.file is not None:
            rel_p = self._try_repo_relative(loc.file.name)
            if rel_p is not None:
                raw_b = pre_bytes_by_rel.get(rel_p)
                if raw_b is None:
                    try:
                        p_abs, _ = ensure_contained_path(self.repo_root, rel_p)
                        raw_b = p_abs.read_bytes()
                        pre_bytes_by_rel[rel_p] = raw_b
                    except OSError:
                        raw_b = b""
                b_off = self._original_offset(rel_p, int(loc.offset))
                buffer = self._source_buffers.get(rel_p)
                if buffer is not None:
                    line, col = buffer.position(b_off)
                else:
                    line, col = max(1, int(loc.line)), max(1, int(loc.column))
                return SourceSpan(
                    repo_path=rel_p,
                    byte_start=b_off,
                    byte_end=b_off,
                    start_line=line,
                    start_col=col,
                    end_line=line,
                    end_col=col,
                )
        return fallback
