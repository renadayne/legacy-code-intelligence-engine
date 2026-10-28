"""Command-line interface for LegacyGraph (`python -m legacygraph`)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .build_context import load_build_manifest
from .discovery import discover_repository_files, resolve_repo_root
from .index import IndexStore


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="legacygraph",
        description="Offline source code intelligence foundation for legacy C++/MFC repositories.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # init
    p_init = subparsers.add_parser(
        "init",
        help="Initialize a versioned LegacyGraph SQLite database for a repository.",
    )
    p_init.add_argument(
        "--repo",
        default=".",
        help="Path to the target repository root (default: current directory).",
    )
    p_init.add_argument(
        "--db",
        default=".legacygraph/index.db",
        help="Path to the SQLite index file (default: .legacygraph/index.db).",
    )
    p_init.add_argument(
        "--revision",
        default="",
        help="Optional source revision / commit identifier.",
    )
    p_init.add_argument(
        "--json",
        action="store_true",
        help="Emit output in JSON format.",
    )

    # discover
    p_disc = subparsers.add_parser(
        "discover",
        help="Scan repository source files in read-only mode and sync content-hash versions.",
    )
    p_disc.add_argument(
        "--repo",
        default=".",
        help="Path to the target repository root.",
    )
    p_disc.add_argument(
        "--db",
        default=None,
        help="Optional SQLite index path to persist discovered file inventory and versions.",
    )
    p_disc.add_argument(
        "--exclude-dir",
        action="append",
        default=[],
        help="Additional directory names to exclude from discovery.",
    )
    p_disc.add_argument(
        "--legacy-encoding",
        default="cp1252",
        help="Fallback encoding for non-UTF8 legacy files (default: cp1252).",
    )
    p_disc.add_argument(
        "--json",
        action="store_true",
        help="Emit output in JSON format.",
    )

    # scan-manifest
    p_man = subparsers.add_parser(
        "scan-manifest",
        help="Import a build manifest or compile_commands.json into the SQLite index.",
    )
    p_man.add_argument(
        "--repo",
        default=".",
        help="Path to the target repository root.",
    )
    p_man.add_argument(
        "--db",
        default=".legacygraph/index.db",
        help="Path to the SQLite index file.",
    )
    p_man.add_argument(
        "--manifest",
        required=True,
        help="Path to a LegacyGraph JSON build manifest or Clang compile_commands.json.",
    )
    p_man.add_argument(
        "--deactivate-unlisted",
        action="store_true",
        help="Deactivate previously registered TUs in the same build context if omitted from the manifest.",
    )
    p_man.add_argument(
        "--json",
        action="store_true",
        help="Emit output in JSON format.",
    )

    # status
    p_stat = subparsers.add_parser(
        "status",
        help="Report index database status, file inventory, build contexts, and invalidated TUs.",
    )
    p_stat.add_argument(
        "--db",
        default=".legacygraph/index.db",
        help="Path to the SQLite index file.",
    )
    p_stat.add_argument(
        "--check-invalidated",
        action="store_true",
        help="Include list of translation units invalidated relative to the published generation.",
    )
    p_stat.add_argument(
        "--json",
        action="store_true",
        help="Emit output in JSON format.",
    )

    return parser


def _cmd_init(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo)
    db_path = Path(args.db)
    if not db_path.is_absolute():
        db_path = (Path.cwd() / db_path).resolve(strict=False)

    with IndexStore(db_path) as store:
        store.initialize_schema(repo_root, source_revision=args.revision)
        summary = store.get_status_summary()

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"Initialized LegacyGraph index at: {summary['db_path']}")
        print(f"Repository root: {summary['repo_root']}")
        print(f"Schema version:  {summary['schema_version']}")
    return 0


def _cmd_discover(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo)
    output_exclusions: list[Path] = []
    db_path: Path | None = None
    if args.db:
        db_path = Path(args.db)
        if not db_path.is_absolute():
            db_path = (Path.cwd() / db_path).resolve(strict=False)
        output_exclusions.append(db_path)

    discovery = discover_repository_files(
        repo_root,
        output_paths_to_exclude=output_exclusions,
        extra_excluded_dir_names=args.exclude_dir,
        preferred_legacy_encoding=args.legacy_encoding,
    )

    sync_diff: dict[str, list[str]] | None = None
    if db_path is not None:
        with IndexStore(db_path) as store:
            store.initialize_schema(repo_root)
            sync_diff = store.sync_file_inventory(discovery.files)

    payload = {
        "repo_root": discovery.repo_root,
        "discovered_count": len(discovery.files),
        "excluded_paths": list(discovery.excluded_paths),
        "skipped_outside_root": list(discovery.skipped_outside_root),
        "files": [
            {
                "repo_path": f.repo_path,
                "file_role": f.file_role,
                "size_bytes": f.size_bytes,
                "line_count": f.line_count,
                "encoding": f.encoding,
                "has_bom": f.has_bom,
                "decode_clean": f.decode_clean,
                "content_sha256": f.content_sha256,
            }
            for f in discovery.files
        ],
    }
    if sync_diff is not None:
        payload["sync"] = sync_diff

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"Repository root:  {discovery.repo_root}")
        print(f"Discovered files: {len(discovery.files)}")
        if sync_diff is not None:
            print(
                f"Index sync:       +{len(sync_diff['added'])} added, "
                f"~{len(sync_diff['modified'])} modified, "
                f"-{len(sync_diff['deleted'])} deleted, "
                f"={len(sync_diff['unchanged'])} unchanged"
            )
    return 0


def _cmd_scan_manifest(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root(args.repo)
    db_path = Path(args.db)
    if not db_path.is_absolute():
        db_path = (Path.cwd() / db_path).resolve(strict=False)
    manifest_path = Path(args.manifest)
    if not manifest_path.is_absolute():
        manifest_path = (Path.cwd() / manifest_path).resolve(strict=True)

    imported = load_build_manifest(repo_root, manifest_path)
    with IndexStore(db_path) as store:
        store.initialize_schema(repo_root)
        for ctx in imported.contexts:
            store.register_build_context(ctx)
        tu_ids = store.register_translation_units(
            imported.translation_units,
            deactivate_unlisted_for_contexts=args.deactivate_unlisted,
        )
        invalidated = store.compute_invalidated_translation_units()

    payload = {
        "db_path": str(db_path),
        "manifest_path": str(manifest_path),
        "imported_contexts": [
            {
                "context_key": c.context_key,
                "project_name": c.project_name,
                "target_name": c.target_name,
                "configuration": c.configuration,
                "platform": c.platform,
                "provenance": c.provenance,
            }
            for c in imported.contexts
        ],
        "imported_translation_units": len(tu_ids),
        "warnings": list(imported.warnings),
        "invalidated_translation_units": len(invalidated),
    }

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"Imported contexts:          {len(imported.contexts)}")
        print(f"Imported translation units: {len(tu_ids)}")
        print(f"Pending / invalidated TUs:  {len(invalidated)}")
        for w in imported.warnings:
            print(f"Warning: {w}", file=sys.stderr)
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    if not db_path.is_absolute():
        db_path = (Path.cwd() / db_path).resolve(strict=False)
    if not db_path.exists():
        print(f"Error: Database file does not exist: {db_path}", file=sys.stderr)
        return 1

    with IndexStore(db_path) as store:
        summary = store.get_status_summary()
        if args.check_invalidated:
            summary["invalidated_translation_units"] = (
                store.compute_invalidated_translation_units()
            )

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"Database:                  {summary['db_path']}")
        print(f"Repository root:           {summary['repo_root']}")
        print(f"Schema version:            {summary['schema_version']}")
        print(f"Active files:              {summary['active_files']} ({summary['total_source_lines']} lines)")
        print(f"Deleted files tracked:     {summary['deleted_files']}")
        print(f"Build contexts:            {summary['build_contexts']}")
        print(f"Active translation units:  {summary['active_translation_units']}")
        pub = summary["published_generation"]
        if pub:
            print(
                f"Published generation:      #{pub['id']} "
                f"({pub['verified_tu_runs']} verified TUs, published at {pub['published_at']})"
            )
        else:
            print("Published generation:      None")
        if args.check_invalidated:
            inv = summary["invalidated_translation_units"]
            print(f"Invalidated / pending TUs: {len(inv)}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "init":
        return _cmd_init(args)
    if args.command == "discover":
        return _cmd_discover(args)
    if args.command == "scan-manifest":
        return _cmd_scan_manifest(args)
    if args.command == "status":
        return _cmd_status(args)
    parser.print_help()
    return 1
