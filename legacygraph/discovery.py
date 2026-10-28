"""Read-only repository discovery, path containment, and encoding-aware hashing."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .models import FileInventoryRecord


DEFAULT_EXCLUDED_DIR_NAMES: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".vs",
        ".vscode",
        ".idea",
        ".legacygraph",
        "legacygraph_out",
        "__pycache__",
        ".venv",
        "venv",
        "node_modules",
        "build",
        "dist",
    }
)

SOURCE_EXTENSIONS: dict[str, str] = {
    ".c": "c_source",
    ".cc": "cpp_source",
    ".cpp": "cpp_source",
    ".cxx": "cpp_source",
    ".c++": "cpp_source",
    ".h": "header",
    ".hh": "header",
    ".hpp": "header",
    ".hxx": "header",
    ".inl": "inline_header",
    ".ipp": "inline_header",
    ".rc": "resource_script",
    ".rc2": "resource_script",
    ".dlg": "resource_dialog",
    ".idl": "idl",
    ".odl": "odl",
    ".def": "module_def",
    ".sln": "msbuild_solution",
    ".vcxproj": "msbuild_project",
    ".props": "msbuild_props",
    ".targets": "msbuild_targets",
}


class PathContainmentError(ValueError):
    """Raised when a path escapes the allowed repository root."""


def resolve_repo_root(repo_root: str | Path) -> Path:
    """Resolve the canonical repository root directory."""
    root = Path(repo_root).resolve(strict=True)
    if not root.is_dir():
        raise NotADirectoryError(f"Repository root is not a directory: {root}")
    return root


def ensure_contained_path(
    repo_root: str | Path, candidate: str | Path, *, allow_nonexistent: bool = False
) -> tuple[Path, str]:
    """Verify that `candidate` stays strictly within `repo_root` after canonical resolution.

    Returns `(resolved_path, normalized_repo_relative_posix_path)`.
    """
    canonical_root = resolve_repo_root(repo_root)
    candidate_path = Path(candidate)
    if not candidate_path.is_absolute():
        candidate_path = canonical_root / candidate_path

    resolved = candidate_path.resolve(strict=not allow_nonexistent)
    try:
        rel = resolved.relative_to(canonical_root)
    except ValueError as exc:
        raise PathContainmentError(
            f"Path '{candidate}' resolves to '{resolved}' outside repository root '{canonical_root}'."
        ) from exc

    rel_posix = rel.as_posix()
    if rel_posix == ".":
        return resolved, "."
    if rel_posix.startswith("../") or "/../" in f"/{rel_posix}/":
        raise PathContainmentError(
            f"Relative path '{rel_posix}' escapes repository root '{canonical_root}'."
        )
    return resolved, rel_posix


def compute_line_byte_offsets(raw_bytes: bytes) -> tuple[int, ...]:
    """Return 0-based starting byte offsets for each 1-based line in `raw_bytes`."""
    if not raw_bytes:
        return (0,)
    offsets = [0]
    pos = 0
    while True:
        idx = raw_bytes.find(b"\n", pos)
        if idx == -1:
            break
        if idx + 1 < len(raw_bytes):
            offsets.append(idx + 1)
        pos = idx + 1
    return tuple(offsets)


def detect_encoding_and_decode(
    raw_bytes: bytes,
    preferred_legacy_encoding: str = "cp1252",
) -> tuple[str, bool, bool, str]:
    """Detect BOM or UTF-8 / legacy Windows encoding without altering raw byte offsets.

    Returns `(encoding_name, has_bom, decode_clean, decoded_text)`.
    """
    if raw_bytes.startswith(b"\xef\xbb\xbf"):
        try:
            return "utf-8-sig", True, True, raw_bytes.decode("utf-8-sig", errors="strict")
        except UnicodeDecodeError:
            return "utf-8-sig", True, False, raw_bytes.decode("utf-8-sig", errors="replace")

    if raw_bytes.startswith(b"\xff\xfe\x00\x00"):
        try:
            return "utf-32-le", True, True, raw_bytes.decode("utf-32", errors="strict")
        except UnicodeDecodeError:
            return "utf-32-le", True, False, raw_bytes.decode("utf-32", errors="replace")

    if raw_bytes.startswith(b"\x00\x00\xfe\xff"):
        try:
            return "utf-32-be", True, True, raw_bytes.decode("utf-32", errors="strict")
        except UnicodeDecodeError:
            return "utf-32-be", True, False, raw_bytes.decode("utf-32", errors="replace")

    if raw_bytes.startswith(b"\xff\xfe"):
        try:
            return "utf-16-le", True, True, raw_bytes.decode("utf-16", errors="strict")
        except UnicodeDecodeError:
            return "utf-16-le", True, False, raw_bytes.decode("utf-16", errors="replace")

    if raw_bytes.startswith(b"\xfe\xff"):
        try:
            return "utf-16-be", True, True, raw_bytes.decode("utf-16", errors="strict")
        except UnicodeDecodeError:
            return "utf-16-be", True, False, raw_bytes.decode("utf-16", errors="replace")

    try:
        text = raw_bytes.decode("utf-8", errors="strict")
        return "utf-8", False, True, text
    except UnicodeDecodeError:
        pass

    fallback_encodings = [preferred_legacy_encoding, "cp1252", "cp932", "latin-1"]
    seen: set[str] = set()
    for enc in fallback_encodings:
        norm = enc.lower()
        if norm in seen:
            continue
        seen.add(norm)
        try:
            text = raw_bytes.decode(enc, errors="strict")
            return norm, False, True, text
        except UnicodeDecodeError:
            continue

    return "binary-or-mixed", False, False, raw_bytes.decode("latin-1", errors="replace")


def inspect_source_file(
    repo_root: str | Path,
    file_path: str | Path,
    *,
    preferred_legacy_encoding: str = "cp1252",
) -> FileInventoryRecord:
    """Read a source file in read-only mode, verify containment, and compute hash/line offsets."""
    resolved_path, rel_posix = ensure_contained_path(repo_root, file_path)
    raw_bytes = resolved_path.read_bytes()
    sha256_hex = hashlib.sha256(raw_bytes).hexdigest()
    encoding, has_bom, decode_clean, _ = detect_encoding_and_decode(
        raw_bytes, preferred_legacy_encoding=preferred_legacy_encoding
    )
    line_offsets = compute_line_byte_offsets(raw_bytes)
    ext = resolved_path.suffix.lower()
    file_role = SOURCE_EXTENSIONS.get(ext, "other")

    return FileInventoryRecord(
        repo_path=rel_posix,
        size_bytes=len(raw_bytes),
        content_sha256=sha256_hex,
        encoding=encoding,
        has_bom=has_bom,
        decode_clean=decode_clean,
        line_count=len(line_offsets),
        line_byte_offsets=line_offsets,
        file_role=file_role,
    )


@dataclass(frozen=True)
class DiscoveryResult:
    """Summary and file records from a read-only repository discovery pass."""

    repo_root: str
    files: tuple[FileInventoryRecord, ...]
    excluded_paths: tuple[str, ...]
    skipped_outside_root: tuple[str, ...]


def discover_repository_files(
    repo_root: str | Path,
    *,
    output_paths_to_exclude: Iterable[str | Path] = (),
    extra_excluded_dir_names: Iterable[str] = (),
    preferred_legacy_encoding: str = "cp1252",
    include_extensions: set[str] | None = None,
) -> DiscoveryResult:
    """Scan a repository in read-only mode while excluding output directories and uncontained links."""
    canonical_root = resolve_repo_root(repo_root)
    allowed_exts = (
        {ext.lower() for ext in include_extensions}
        if include_extensions is not None
        else set(SOURCE_EXTENSIONS.keys())
    )
    excluded_dir_names = {name.lower() for name in DEFAULT_EXCLUDED_DIR_NAMES} | {
        name.lower() for name in extra_excluded_dir_names
    }

    explicit_excluded_resolved: set[Path] = set()
    for out_path in output_paths_to_exclude:
        p = Path(out_path)
        if not p.is_absolute():
            p = canonical_root / p
        resolved_out = p.resolve(strict=False)
        explicit_excluded_resolved.add(resolved_out)
        # If the path is an SQLite DB file inside a dedicated output directory or file, exclude both
        if resolved_out.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
            explicit_excluded_resolved.add(Path(str(resolved_out) + "-wal"))
            explicit_excluded_resolved.add(Path(str(resolved_out) + "-shm"))

    discovered: list[FileInventoryRecord] = []
    excluded_records: list[str] = []
    skipped_outside: list[str] = []

    for current_dir, dirnames, filenames in os.walk(canonical_root, topdown=True, followlinks=False):
        current_path = Path(current_dir)
        kept_dirs: list[str] = []
        for d in sorted(dirnames):
            candidate_dir = current_path / d
            if d.lower() in excluded_dir_names:
                excluded_records.append(candidate_dir.relative_to(canonical_root).as_posix())
                continue
            try:
                resolved_dir, rel_dir = ensure_contained_path(canonical_root, candidate_dir)
            except PathContainmentError:
                skipped_outside.append(str(candidate_dir))
                continue
            if any(
                resolved_dir == ex or ex in resolved_dir.parents
                for ex in explicit_excluded_resolved
            ):
                excluded_records.append(rel_dir)
                continue
            kept_dirs.append(d)
        dirnames[:] = kept_dirs

        for fname in sorted(filenames):
            candidate_file = current_path / fname
            try:
                resolved_file, rel_file = ensure_contained_path(canonical_root, candidate_file)
            except (PathContainmentError, OSError):
                skipped_outside.append(str(candidate_file))
                continue

            if any(
                resolved_file == ex or ex in resolved_file.parents
                for ex in explicit_excluded_resolved
            ):
                excluded_records.append(rel_file)
                continue

            ext = resolved_file.suffix.lower()
            if ext not in allowed_exts:
                continue

            record = inspect_source_file(
                canonical_root,
                resolved_file,
                preferred_legacy_encoding=preferred_legacy_encoding,
            )
            discovered.append(record)

    discovered.sort(key=lambda r: r.repo_path)
    return DiscoveryResult(
        repo_root=str(canonical_root),
        files=tuple(discovered),
        excluded_paths=tuple(sorted(set(excluded_records))),
        skipped_outside_root=tuple(sorted(set(skipped_outside))),
    )
