"""Build manifest and Clang JSON Compilation Database import for LegacyGraph."""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .discovery import ensure_contained_path, resolve_repo_root
from .models import BuildContextSpec, TranslationUnitSpec


@dataclass(frozen=True)
class ManifestImportResult:
    """Result of importing a build manifest or compile_commands.json."""

    contexts: tuple[BuildContextSpec, ...]
    translation_units: tuple[TranslationUnitSpec, ...]
    warnings: tuple[str, ...]


def split_windows_command_line(command: str) -> list[str]:
    """Split a Windows/Clang/MSVC command line string into tokens following Windows C runtime rules."""
    if not command or not command.strip():
        return []
    tokens: list[str] = []
    cur: list[str] = []
    in_quotes = False
    has_token = False
    i = 0
    n = len(command)

    while i < n:
        ch = command[i]
        if ch in (" ", "\t", "\r", "\n") and not in_quotes:
            if has_token:
                tokens.append("".join(cur))
                cur = []
                has_token = False
            i += 1
            continue

        if ch == "\\":
            bs_count = 0
            while i < n and command[i] == "\\":
                bs_count += 1
                i += 1
            if i < n and command[i] == '"':
                cur.append("\\" * (bs_count // 2))
                has_token = True
                if bs_count % 2 == 0:
                    in_quotes = not in_quotes
                else:
                    cur.append('"')
                i += 1
            else:
                cur.append("\\" * bs_count)
                has_token = True
            continue

        if ch == '"':
            has_token = True
            if in_quotes and i + 1 < n and command[i + 1] == '"':
                cur.append('"')
                i += 2
            else:
                in_quotes = not in_quotes
                i += 1
            continue

        cur.append(ch)
        has_token = True
        i += 1

    if has_token:
        tokens.append("".join(cur))
    return tokens


def _normalize_dir_rel_or_external(repo_root: Path, working_dir: Path, raw_dir: str) -> str:
    candidate = Path(raw_dir)
    if not candidate.is_absolute():
        candidate = (working_dir / candidate).resolve(strict=False)
    else:
        candidate = candidate.resolve(strict=False)
    try:
        rel = candidate.relative_to(repo_root)
        return rel.as_posix()
    except ValueError:
        return candidate.as_posix()


def parse_compiler_arguments(
    args: list[str],
    *,
    repo_root: Path,
    working_dir: Path,
) -> dict[str, Any]:
    """Extract includes, defines, undefines, forced includes, response files, and flags from CLI tokens."""
    include_dirs: list[str] = []
    system_include_dirs: list[str] = []
    defines: list[str] = []
    undefines: list[str] = []
    forced_includes: list[str] = []
    response_files: list[str] = []
    extra_flags: list[str] = []
    language_standard = ""
    pch_header = ""

    # Skip compiler executable if first token looks like a compiler binary
    start_idx = 0
    if args:
        first_lower = Path(args[0]).name.lower()
        if any(
            first_lower.startswith(prefix)
            for prefix in ("cl", "clang", "clang++", "clang-cl", "gcc", "g++")
        ):
            start_idx = 1

    i = start_idx
    while i < len(args):
        token = args[i]
        if token.startswith("@") and len(token) > 1:
            response_files.append(token[1:])
            i += 1
            continue

        # Skip output file options (-o <file>, /Fo<file>, /Fd<file>, /Fe<file>)
        if token in ("-o", "/Fo", "/Fd", "/Fe", "-MF", "-MT", "-MQ"):
            i += 2
            continue
        if token.startswith(("/Fo", "/Fd", "/Fe", "/Fp")):
            i += 1
            continue
        if token in ("-c", "/c", "/nologo", "--"):
            if token == "--":
                break
            i += 1
            continue

        # Include directories
        if token in ("-I", "/I") and i + 1 < len(args):
            include_dirs.append(_normalize_dir_rel_or_external(repo_root, working_dir, args[i + 1]))
            i += 2
            continue
        if (token.startswith("-I") or token.startswith("/I")) and len(token) > 2:
            include_dirs.append(_normalize_dir_rel_or_external(repo_root, working_dir, token[2:]))
            i += 1
            continue
        if token in ("-isystem", "/external:I") and i + 1 < len(args):
            system_include_dirs.append(
                _normalize_dir_rel_or_external(repo_root, working_dir, args[i + 1])
            )
            i += 2
            continue
        if token.startswith("/external:I") and len(token) > len("/external:I"):
            system_include_dirs.append(
                _normalize_dir_rel_or_external(repo_root, working_dir, token[len("/external:I") :])
            )
            i += 1
            continue

        # Defines / Undefines
        if token in ("-D", "/D") and i + 1 < len(args):
            defines.append(args[i + 1])
            i += 2
            continue
        if (token.startswith("-D") or token.startswith("/D")) and len(token) > 2:
            defines.append(token[2:])
            i += 1
            continue
        if token in ("-U", "/U") and i + 1 < len(args):
            undefines.append(args[i + 1])
            i += 2
            continue
        if (token.startswith("-U") or token.startswith("/U")) and len(token) > 2:
            undefines.append(token[2:])
            i += 1
            continue

        # Forced includes
        if token in ("-include", "/FI") and i + 1 < len(args):
            forced_includes.append(
                _normalize_dir_rel_or_external(repo_root, working_dir, args[i + 1])
            )
            i += 2
            continue
        if token.startswith("/FI") and len(token) > 3:
            forced_includes.append(
                _normalize_dir_rel_or_external(repo_root, working_dir, token[3:])
            )
            i += 1
            continue

        # Precompiled headers
        if token.startswith("/Yu") and len(token) > 3:
            pch_header = token[3:]
            i += 1
            continue
        if token.startswith("/Yc") and len(token) > 3:
            pch_header = token[3:]
            i += 1
            continue

        # Language standard
        if token.startswith("-std="):
            language_standard = token[5:]
            i += 1
            continue
        if token.startswith("/std:") or token.startswith("-std:"):
            language_standard = token[5:]
            i += 1
            continue

        # Keep other compiler flags except input source file paths
        if token.startswith(("-", "/")):
            extra_flags.append(token)
        i += 1

    return {
        "include_dirs": tuple(include_dirs),
        "system_include_dirs": tuple(system_include_dirs),
        "defines": tuple(defines),
        "undefines": tuple(undefines),
        "forced_includes": tuple(forced_includes),
        "response_files": tuple(response_files),
        "extra_flags": tuple(extra_flags),
        "language_standard": language_standard or "c++17",
        "pch_header": pch_header,
    }


def load_compile_commands(
    repo_root: str | Path,
    compile_commands_path: str | Path,
    *,
    default_project: str = "default",
    default_target: str = "default",
    default_configuration: str = "Default",
    default_platform: str = "x64",
) -> ManifestImportResult:
    """Import a Clang JSON Compilation Database (`compile_commands.json`).

    Supports multiple compile commands for the same source file across distinct contexts/flags.
    """
    canonical_root = resolve_repo_root(repo_root)
    raw_entries = json.loads(Path(compile_commands_path).read_text(encoding="utf-8"))
    if not isinstance(raw_entries, list):
        raise ValueError("compile_commands.json must contain a top-level JSON array.")

    contexts_by_key: dict[str, BuildContextSpec] = {}
    tu_specs: list[TranslationUnitSpec] = []
    warnings: list[str] = []

    for idx, entry in enumerate(raw_entries):
        if not isinstance(entry, dict):
            warnings.append(f"Skipping non-object entry at index {idx}.")
            continue
        raw_dir = entry.get("directory") or str(canonical_root)
        raw_file = entry.get("file")
        if not raw_file:
            warnings.append(f"Entry {idx} missing 'file' field.")
            continue

        work_dir = Path(raw_dir)
        if not work_dir.is_absolute():
            work_dir = (canonical_root / work_dir).resolve(strict=False)
        else:
            work_dir = work_dir.resolve(strict=False)

        file_candidate = Path(raw_file)
        if not file_candidate.is_absolute():
            file_candidate = work_dir / file_candidate

        try:
            _, rel_main_file = ensure_contained_path(
                canonical_root, file_candidate, allow_nonexistent=True
            )
        except Exception as exc:
            warnings.append(f"Entry {idx} file '{raw_file}' skipped: {exc}")
            continue

        if "arguments" in entry and isinstance(entry["arguments"], list):
            tokens = [str(x) for x in entry["arguments"]]
        elif "command" in entry and isinstance(entry["command"], str):
            tokens = split_windows_command_line(entry["command"])
        else:
            warnings.append(f"Entry {idx} for '{rel_main_file}' missing 'arguments' or 'command'.")
            continue

        parsed = parse_compiler_arguments(tokens, repo_root=canonical_root, working_dir=work_dir)
        project_name = str(entry.get("project") or default_project)
        target_name = str(entry.get("target") or default_target)
        configuration = str(entry.get("configuration") or default_configuration)
        platform = str(entry.get("platform") or default_platform)

        # Infer Debug/Release from defines if not explicitly tagged
        if configuration == "Default":
            define_names = {d.split("=", 1)[0] for d in parsed["defines"]}
            if "_DEBUG" in define_names or "DEBUG" in define_names:
                configuration = "Debug"
            elif "NDEBUG" in define_names:
                configuration = "Release"

        try:
            rel_work_dir = work_dir.relative_to(canonical_root).as_posix()
        except ValueError:
            rel_work_dir = work_dir.as_posix()

        ctx = BuildContextSpec(
            project_name=project_name,
            target_name=target_name,
            configuration=configuration,
            platform=platform,
            working_directory=rel_work_dir,
            compiler_family=str(entry.get("compiler_family") or "msvc"),
            toolset_version=str(entry.get("toolset_version") or ""),
            architecture=platform,
            language_standard=parsed["language_standard"],
            include_dirs=parsed["include_dirs"],
            system_include_dirs=parsed["system_include_dirs"],
            defines=parsed["defines"],
            undefines=parsed["undefines"],
            forced_includes=parsed["forced_includes"],
            extra_flags=parsed["extra_flags"],
            pch_header=parsed["pch_header"],
            provenance="compile_commands",
        )
        contexts_by_key[ctx.context_key] = ctx
        tu_specs.append(
            TranslationUnitSpec(
                main_file_path=rel_main_file,
                build_context=ctx,
                response_files=parsed["response_files"],
                command_origin="compile_commands",
            )
        )

    return ManifestImportResult(
        contexts=tuple(contexts_by_key.values()),
        translation_units=tuple(tu_specs),
        warnings=tuple(warnings),
    )


def load_build_manifest(
    repo_root: str | Path,
    manifest_path: str | Path,
) -> ManifestImportResult:
    """Import either a LegacyGraph JSON build manifest or a `compile_commands.json` file."""
    canonical_root = resolve_repo_root(repo_root)
    path_obj = Path(manifest_path)
    raw_data = json.loads(path_obj.read_text(encoding="utf-8"))

    # Auto-detect Clang compilation database (top-level JSON array)
    if isinstance(raw_data, list):
        return load_compile_commands(canonical_root, path_obj)

    if not isinstance(raw_data, dict):
        raise ValueError("Build manifest must be a JSON object or compile_commands JSON array.")

    contexts_by_id: dict[str, BuildContextSpec] = {}
    contexts_by_key: dict[str, BuildContextSpec] = {}
    tu_specs: list[TranslationUnitSpec] = []
    warnings: list[str] = []

    raw_contexts = raw_data.get("contexts", [])
    for idx, c in enumerate(raw_contexts):
        if not isinstance(c, dict):
            warnings.append(f"Skipping invalid context entry at index {idx}.")
            continue
        ctx = BuildContextSpec(
            project_name=str(c.get("project_name") or c.get("project") or "default"),
            target_name=str(c.get("target_name") or c.get("target") or "default"),
            configuration=str(c.get("configuration") or "Debug"),
            platform=str(c.get("platform") or "x64"),
            working_directory=str(c.get("working_directory") or "."),
            compiler_family=str(c.get("compiler_family") or "msvc"),
            toolset_version=str(c.get("toolset_version") or ""),
            architecture=str(c.get("architecture") or c.get("platform") or "x64"),
            language_standard=str(c.get("language_standard") or "c++17"),
            include_dirs=tuple(str(x) for x in c.get("include_dirs", [])),
            system_include_dirs=tuple(str(x) for x in c.get("system_include_dirs", [])),
            defines=tuple(str(x) for x in c.get("defines", [])),
            undefines=tuple(str(x) for x in c.get("undefines", [])),
            forced_includes=tuple(str(x) for x in c.get("forced_includes", [])),
            extra_flags=tuple(str(x) for x in c.get("extra_flags", [])),
            pch_header=str(c.get("pch_header") or ""),
            sdk_version=str(c.get("sdk_version") or ""),
            mfc_mode=str(c.get("mfc_mode") or ""),
            provenance=str(c.get("provenance") or "manifest"),
        )
        ctx_id = str(c.get("id") or ctx.context_key)
        contexts_by_id[ctx_id] = ctx
        contexts_by_key[ctx.context_key] = ctx

    raw_tus = raw_data.get("translation_units", [])
    for idx, item in enumerate(raw_tus):
        if not isinstance(item, dict):
            warnings.append(f"Skipping non-object translation_unit at index {idx}.")
            continue
        raw_file = item.get("file") or item.get("main_file_path")
        if not raw_file:
            warnings.append(f"Translation unit at index {idx} missing 'file'.")
            continue

        try:
            _, rel_file = ensure_contained_path(canonical_root, str(raw_file), allow_nonexistent=True)
        except Exception as exc:
            warnings.append(f"Translation unit at index {idx} file '{raw_file}' skipped: {exc}")
            continue

        ctx_ref = item.get("context_id")
        if ctx_ref is not None:
            if str(ctx_ref) not in contexts_by_id:
                raise ValueError(
                    f"Translation unit at index {idx} references unknown context_id '{ctx_ref}'."
                )
            ctx = contexts_by_id[str(ctx_ref)]
        elif isinstance(item.get("context"), dict):
            c = item["context"]
            ctx = BuildContextSpec(
                project_name=str(c.get("project_name") or c.get("project") or "default"),
                target_name=str(c.get("target_name") or c.get("target") or "default"),
                configuration=str(c.get("configuration") or "Debug"),
                platform=str(c.get("platform") or "x64"),
                working_directory=str(c.get("working_directory") or "."),
                compiler_family=str(c.get("compiler_family") or "msvc"),
                toolset_version=str(c.get("toolset_version") or ""),
                architecture=str(c.get("architecture") or c.get("platform") or "x64"),
                language_standard=str(c.get("language_standard") or "c++17"),
                include_dirs=tuple(str(x) for x in c.get("include_dirs", [])),
                system_include_dirs=tuple(str(x) for x in c.get("system_include_dirs", [])),
                defines=tuple(str(x) for x in c.get("defines", [])),
                undefines=tuple(str(x) for x in c.get("undefines", [])),
                forced_includes=tuple(str(x) for x in c.get("forced_includes", [])),
                extra_flags=tuple(str(x) for x in c.get("extra_flags", [])),
                pch_header=str(c.get("pch_header") or ""),
                sdk_version=str(c.get("sdk_version") or ""),
                mfc_mode=str(c.get("mfc_mode") or ""),
                provenance=str(c.get("provenance") or "manifest"),
            )
            contexts_by_key[ctx.context_key] = ctx
        else:
            # Inline context attributes on the TU entry
            ctx = BuildContextSpec(
                project_name=str(item.get("project_name") or item.get("project") or "default"),
                target_name=str(item.get("target_name") or item.get("target") or "default"),
                configuration=str(item.get("configuration") or "Debug"),
                platform=str(item.get("platform") or "x64"),
                working_directory=str(item.get("working_directory") or "."),
                compiler_family=str(item.get("compiler_family") or "msvc"),
                toolset_version=str(item.get("toolset_version") or ""),
                architecture=str(item.get("architecture") or item.get("platform") or "x64"),
                language_standard=str(item.get("language_standard") or "c++17"),
                include_dirs=tuple(str(x) for x in item.get("include_dirs", [])),
                system_include_dirs=tuple(str(x) for x in item.get("system_include_dirs", [])),
                defines=tuple(str(x) for x in item.get("defines", [])),
                undefines=tuple(str(x) for x in item.get("undefines", [])),
                forced_includes=tuple(str(x) for x in item.get("forced_includes", [])),
                extra_flags=tuple(str(x) for x in item.get("extra_flags", [])),
                pch_header=str(item.get("pch_header") or ""),
                sdk_version=str(item.get("sdk_version") or ""),
                mfc_mode=str(item.get("mfc_mode") or ""),
                provenance=str(item.get("provenance") or "manifest"),
            )
            contexts_by_key[ctx.context_key] = ctx

        tu_specs.append(
            TranslationUnitSpec(
                main_file_path=rel_file,
                build_context=ctx,
                per_file_include_dirs=tuple(str(x) for x in item.get("per_file_include_dirs", [])),
                per_file_defines=tuple(str(x) for x in item.get("per_file_defines", [])),
                per_file_undefines=tuple(str(x) for x in item.get("per_file_undefines", [])),
                per_file_forced_includes=tuple(
                    str(x) for x in item.get("per_file_forced_includes", [])
                ),
                per_file_flags=tuple(str(x) for x in item.get("per_file_flags", [])),
                response_files=tuple(str(x) for x in item.get("response_files", [])),
                command_origin="manifest",
            )
        )

    return ManifestImportResult(
        contexts=tuple(contexts_by_key.values()),
        translation_units=tuple(tu_specs),
        warnings=tuple(warnings),
    )
