"""Unit tests for LegacyGraph Step 1: discovery, manifest import, SQLite storage, context isolation, invalidation, and generation safety."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from legacygraph.build_context import load_build_manifest, load_compile_commands
from legacygraph.cli import main as cli_main
from legacygraph.discovery import (
    PathContainmentError,
    discover_repository_files,
    ensure_contained_path,
    inspect_source_file,
)
from legacygraph.index import IndexStore
from legacygraph.models import (
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


class LegacyGraphStep1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.repo_dir = self.root / "sample_repo"
        self.repo_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.repo_dir / ".legacygraph" / "index.db"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_sample_repo_files(self) -> None:
        inc_dir = self.repo_dir / "include"
        src_dir = self.repo_dir / "src"
        inc_dir.mkdir(parents=True, exist_ok=True)
        src_dir.mkdir(parents=True, exist_ok=True)

        (inc_dir / "OrderDlg.h").write_text(
            "#pragma once\nclass COrderDlg {\npublic:\n    void OnSave();\n};\n",
            encoding="utf-8",
        )
        (src_dir / "OrderDlg.cpp").write_text(
            '#include "OrderDlg.h"\nvoid COrderDlg::OnSave() {}\n',
            encoding="utf-8",
        )
        (src_dir / "Helper.cpp").write_text(
            '#include "OrderDlg.h"\nstatic int ComputeTax() { return 42; }\n',
            encoding="utf-8",
        )

    def test_discovery_excludes_outputs_and_handles_legacy_encodings(self) -> None:
        self._write_sample_repo_files()
        # Write a legacy CP1252 file with non-UTF8 byte (0x93 smart quote / 0xE9 é)
        legacy_rc = self.repo_dir / "src" / "Legacy.rc"
        legacy_bytes = b"// Legacy Windows CP1252 comment: Caf\xe9 \x93Save\x94\r\nIDD_ORDER DIALOG 0, 0, 100, 100\r\n"
        legacy_rc.write_bytes(legacy_bytes)

        # Write a UTF-16 LE BOM header
        utf16_h = self.repo_dir / "include" / "UnicodeRes.h"
        utf16_h.write_bytes("#define IDS_TITLE 101\r\n".encode("utf-16"))

        # Create output files inside .legacygraph and a custom output dir to ensure exclusion
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path.write_text("dummy", encoding="utf-8")
        custom_out = self.repo_dir / "custom_out"
        custom_out.mkdir()
        (custom_out / "generated_dump.cpp").write_text("// should be excluded\n", encoding="utf-8")

        result = discover_repository_files(
            self.repo_dir,
            output_paths_to_exclude=[self.db_path, custom_out],
        )
        paths = {f.repo_path: f for f in result.files}
        self.assertIn("include/OrderDlg.h", paths)
        self.assertIn("src/OrderDlg.cpp", paths)
        self.assertIn("src/Helper.cpp", paths)
        self.assertIn("src/Legacy.rc", paths)
        self.assertIn("include/UnicodeRes.h", paths)
        self.assertNotIn("custom_out/generated_dump.cpp", paths)

        rc_info = paths["src/Legacy.rc"]
        self.assertEqual(rc_info.size_bytes, len(legacy_bytes))
        self.assertEqual(rc_info.encoding, "cp1252")
        self.assertFalse(rc_info.has_bom)
        self.assertTrue(rc_info.decode_clean)
        self.assertEqual(rc_info.line_byte_offsets[0], 0)
        self.assertEqual(rc_info.line_byte_offsets[1], legacy_bytes.find(b"\n") + 1)

        u16_info = paths["include/UnicodeRes.h"]
        self.assertEqual(u16_info.encoding, "utf-16-le")
        self.assertTrue(u16_info.has_bom)
        self.assertTrue(u16_info.decode_clean)

    def test_path_containment_blocks_escape(self) -> None:
        self._write_sample_repo_files()
        outside_file = self.root / "outside.cpp"
        outside_file.write_text("int x = 0;\n", encoding="utf-8")

        with self.assertRaises(PathContainmentError):
            ensure_contained_path(self.repo_dir, "../outside.cpp")

        with self.assertRaises(PathContainmentError):
            inspect_source_file(self.repo_dir, outside_file)

    def test_compile_commands_and_manifest_import_multi_context(self) -> None:
        self._write_sample_repo_files()
        cc_path = self.repo_dir / "compile_commands.json"
        compile_commands = [
            {
                "directory": str(self.repo_dir),
                "command": 'cl.exe /nologo /c /Iinclude /D_DEBUG /D_UNICODE /Fo"Debug\\\\OrderDlg.obj" src\\OrderDlg.cpp',
                "file": "src/OrderDlg.cpp",
            },
            {
                "directory": str(self.repo_dir),
                "command": 'cl.exe /nologo /c /Iinclude /DNDEBUG /D_UNICODE /Fo"Release\\\\OrderDlg.obj" src\\OrderDlg.cpp',
                "file": "src/OrderDlg.cpp",
            },
        ]
        cc_path.write_text(json.dumps(compile_commands, indent=2), encoding="utf-8")

        imported = load_compile_commands(self.repo_dir, cc_path)
        self.assertEqual(len(imported.contexts), 2)
        self.assertEqual(len(imported.translation_units), 2)
        configs = {c.configuration for c in imported.contexts}
        self.assertEqual(configs, {"Debug", "Release"})
        tu_keys = {tu.tu_key for tu in imported.translation_units}
        self.assertEqual(len(tu_keys), 2)

        # Also test structured LegacyGraph JSON manifest format
        manifest_path = self.repo_dir / "manifest.json"
        manifest_data = {
            "contexts": [
                {
                    "id": "win32_dbg",
                    "project_name": "OrderApp",
                    "target_name": "OrderApp",
                    "configuration": "Debug",
                    "platform": "Win32",
                    "include_dirs": ["include"],
                    "defines": ["WIN32", "_DEBUG", "_AFXDLL"],
                }
            ],
            "translation_units": [
                {
                    "file": "src/OrderDlg.cpp",
                    "context_id": "win32_dbg",
                    "per_file_defines": ["FEATURE_FAST_SAVE=1"],
                },
                {
                    "file": "src/Helper.cpp",
                    "context_id": "win32_dbg",
                },
            ],
        }
        manifest_path.write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")
        man_imported = load_build_manifest(self.repo_dir, manifest_path)
        self.assertEqual(len(man_imported.contexts), 1)
        self.assertEqual(len(man_imported.translation_units), 2)

    def test_persistence_context_isolation_and_evidence_enforcement(self) -> None:
        self._write_sample_repo_files()
        discovery = discover_repository_files(
            self.repo_dir, output_paths_to_exclude=[self.db_path]
        )

        ctx_debug = BuildContextSpec(
            project_name="OrderApp",
            target_name="OrderApp",
            configuration="Debug",
            platform="x64",
            include_dirs=("include",),
            defines=("_DEBUG",),
        )
        ctx_release = BuildContextSpec(
            project_name="OrderApp",
            target_name="OrderApp",
            configuration="Release",
            platform="x64",
            include_dirs=("include",),
            defines=("NDEBUG",),
        )
        tu_debug = TranslationUnitSpec(
            main_file_path="src/OrderDlg.cpp", build_context=ctx_debug
        )
        tu_release = TranslationUnitSpec(
            main_file_path="src/OrderDlg.cpp", build_context=ctx_release
        )

        # Verify model rejects a RESOLVED relation without evidence
        with self.assertRaises(ValueError):
            RelationRecord(
                source_semantic_key="usr:COrderDlg::OnSave",
                target_semantic_key="usr:AuditLog",
                kind=RelationKind.CALLS,
                resolution_status=ResolutionStatus.RESOLVED,
                dispatch_kind=DispatchKind.DIRECT,
                extraction_quality=ExtractionQuality.SEMANTIC,
                evidence=(),
            )

        with IndexStore(self.db_path) as store:
            store.initialize_schema(self.repo_dir, source_revision="rev-001")
            store.sync_file_inventory(discovery.files)
            store.register_translation_units([tu_debug, tu_release])

            gen1 = store.begin_generation(notes="initial index")

            span_onsave = SourceSpan(
                repo_path="src/OrderDlg.cpp",
                byte_start=22,
                byte_end=48,
                start_line=2,
                start_col=1,
                end_line=2,
                end_col=27,
            )
            obs_call_dbg = ObservationRecord(
                observation_key="obs:call:debug_trace",
                observation_type="call_expr",
                span=span_onsave,
                subject_semantic_key="usr:COrderDlg::OnSave",
                rule_id="clang.direct_call",
            )
            obs_call_rel = ObservationRecord(
                observation_key="obs:call:fast_commit",
                observation_type="call_expr",
                span=span_onsave,
                subject_semantic_key="usr:COrderDlg::OnSave",
                rule_id="clang.direct_call",
            )
            obs_unresolved = ObservationRecord(
                observation_key="obs:call:plugin_hook",
                observation_type="call_expr",
                span=span_onsave,
                subject_semantic_key="usr:COrderDlg::OnSave",
                rule_id="clang.unresolved_call",
            )

            entities_dbg = (
                EntityRecord(
                    semantic_key="usr:COrderDlg::OnSave",
                    kind=EntityKind.METHOD,
                    name="OnSave",
                    qualified_name="COrderDlg::OnSave",
                ),
                EntityRecord(
                    semantic_key="usr:DebugTrace",
                    kind=EntityKind.FUNCTION,
                    name="DebugTrace",
                    qualified_name="DebugTrace",
                ),
                EntityRecord(
                    semantic_key="usr:IPlugin::Execute_ImplA",
                    kind=EntityKind.METHOD,
                    name="Execute",
                    qualified_name="PluginA::Execute",
                ),
            )
            payload_dbg = ParseRunPayload(
                tu_key=tu_debug.tu_key,
                parser_name="test_adapter",
                parser_version="0.1.0",
                mfc_rule_version="0.1.0",
                parse_status=ParseStatus.PARSED_CLEAN,
                entities=entities_dbg,
                occurrences=(
                    SymbolOccurrenceRecord(
                        entity_semantic_key="usr:COrderDlg::OnSave",
                        occurrence_kind=OccurrenceKind.DEFINITION,
                        span=span_onsave,
                        signature="void COrderDlg::OnSave()",
                    ),
                ),
                observations=(obs_call_dbg, obs_unresolved),
                relations=(
                    RelationRecord(
                        source_semantic_key="usr:COrderDlg::OnSave",
                        target_semantic_key="usr:DebugTrace",
                        kind=RelationKind.CALLS,
                        resolution_status=ResolutionStatus.RESOLVED,
                        dispatch_kind=DispatchKind.DIRECT,
                        extraction_quality=ExtractionQuality.SEMANTIC,
                        reason=ResolutionReason.EXACT_SEMANTIC,
                        evidence=(
                            RelationEvidenceRecord(
                                observation_key="obs:call:debug_trace",
                                span=span_onsave,
                                evidence_snippet_hash="abc123",
                                rule_id="clang.direct_call",
                            ),
                        ),
                    ),
                    RelationRecord(
                        source_semantic_key="usr:COrderDlg::OnSave",
                        target_semantic_key=None,
                        kind=RelationKind.CALLS,
                        resolution_status=ResolutionStatus.UNRESOLVED,
                        dispatch_kind=DispatchKind.INDIRECT,
                        extraction_quality=ExtractionQuality.SEMANTIC,
                        reason=ResolutionReason.INDIRECT_FUNCTION_POINTER,
                        unresolved_target_expr="pHook->Execute()",
                        evidence=(
                            RelationEvidenceRecord(
                                observation_key="obs:call:plugin_hook",
                                span=span_onsave,
                                rule_id="clang.unresolved_call",
                            ),
                        ),
                        candidates=(
                            CandidateTargetRecord(
                                candidate_semantic_key="usr:IPlugin::Execute_ImplA",
                                candidate_reason="interface_implementor",
                                evidence_observation_key="obs:call:plugin_hook",
                            ),
                        ),
                    ),
                ),
                dependencies=(
                    TUDependencyRecord(
                        repo_path="include/OrderDlg.h",
                        dependency_kind=DependencyKind.DIRECT_INCLUDE,
                        included_from_path="src/OrderDlg.cpp",
                        include_line=1,
                    ),
                ),
            )

            entities_rel = (
                EntityRecord(
                    semantic_key="usr:COrderDlg::OnSave",
                    kind=EntityKind.METHOD,
                    name="OnSave",
                    qualified_name="COrderDlg::OnSave",
                ),
                EntityRecord(
                    semantic_key="usr:FastCommit",
                    kind=EntityKind.FUNCTION,
                    name="FastCommit",
                    qualified_name="FastCommit",
                ),
            )
            payload_rel = ParseRunPayload(
                tu_key=tu_release.tu_key,
                parser_name="test_adapter",
                parser_version="0.1.0",
                mfc_rule_version="0.1.0",
                parse_status=ParseStatus.PARSED_CLEAN,
                entities=entities_rel,
                occurrences=(
                    SymbolOccurrenceRecord(
                        entity_semantic_key="usr:COrderDlg::OnSave",
                        occurrence_kind=OccurrenceKind.DEFINITION,
                        span=span_onsave,
                        signature="void COrderDlg::OnSave()",
                    ),
                ),
                observations=(obs_call_rel,),
                relations=(
                    RelationRecord(
                        source_semantic_key="usr:COrderDlg::OnSave",
                        target_semantic_key="usr:FastCommit",
                        kind=RelationKind.CALLS,
                        resolution_status=ResolutionStatus.RESOLVED,
                        dispatch_kind=DispatchKind.DIRECT,
                        extraction_quality=ExtractionQuality.SEMANTIC,
                        reason=ResolutionReason.EXACT_SEMANTIC,
                        evidence=(
                            RelationEvidenceRecord(
                                observation_key="obs:call:fast_commit",
                                span=span_onsave,
                                evidence_snippet_hash="def456",
                                rule_id="clang.direct_call",
                            ),
                        ),
                    ),
                ),
                dependencies=(
                    TUDependencyRecord(
                        repo_path="include/OrderDlg.h",
                        dependency_kind=DependencyKind.DIRECT_INCLUDE,
                        included_from_path="src/OrderDlg.cpp",
                        include_line=1,
                    ),
                ),
            )

            store.record_parse_run(gen1, payload_dbg)
            store.record_parse_run(gen1, payload_rel)
            store.publish_generation(gen1)

        # Re-open from disk to verify persistence and strict context isolation
        with IndexStore(self.db_path) as store2:
            self.assertEqual(store2.get_published_generation_id(), gen1)
            dbg_rels = store2.query_published_relations(context_key=ctx_debug.context_key)
            rel_rels = store2.query_published_relations(context_key=ctx_release.context_key)

            self.assertEqual(len(dbg_rels), 2)
            dbg_targets = {r["target_semantic_key"] for r in dbg_rels}
            self.assertEqual(dbg_targets, {"usr:DebugTrace", None})

            unresolved_rel = [r for r in dbg_rels if r["resolution_status"] == "unresolved"][0]
            self.assertEqual(unresolved_rel["unresolved_target_expr"], "pHook->Execute()")
            self.assertEqual(len(unresolved_rel["candidates"]), 1)
            self.assertEqual(
                unresolved_rel["candidates"][0]["candidate_semantic_key"],
                "usr:IPlugin::Execute_ImplA",
            )

            self.assertEqual(len(rel_rels), 1)
            self.assertEqual(rel_rels[0]["target_semantic_key"], "usr:FastCommit")
            self.assertEqual(rel_rels[0]["evidence_count"], 1)

            # No invalidation when nothing changed
            inv = store2.compute_invalidated_translation_units()
            self.assertEqual(inv, [])

    def test_invalidation_on_header_change_file_delete_and_rollback_safety(self) -> None:
        self._write_sample_repo_files()
        ctx = BuildContextSpec(
            project_name="OrderApp",
            target_name="OrderApp",
            configuration="Debug",
            platform="x64",
            include_dirs=("include",),
            defines=("_DEBUG",),
        )
        tu_order = TranslationUnitSpec(
            main_file_path="src/OrderDlg.cpp", build_context=ctx
        )
        tu_helper = TranslationUnitSpec(
            main_file_path="src/Helper.cpp", build_context=ctx
        )

        span_order = SourceSpan("src/OrderDlg.cpp", 0, 10, 1, 1, 1, 10)
        span_helper = SourceSpan("src/Helper.cpp", 0, 10, 1, 1, 1, 10)

        with IndexStore(self.db_path) as store:
            store.initialize_schema(self.repo_dir)
            disc1 = discover_repository_files(
                self.repo_dir, output_paths_to_exclude=[self.db_path]
            )
            store.sync_file_inventory(disc1.files)
            store.register_translation_units([tu_order, tu_helper])

            gen1 = store.begin_generation(notes="gen1")
            store.record_parse_run(
                gen1,
                ParseRunPayload(
                    tu_key=tu_order.tu_key,
                    parser_name="test",
                    parser_version="0.1.0",
                    mfc_rule_version="0.1.0",
                    parse_status=ParseStatus.PARSED_CLEAN,
                    entities=(
                        EntityRecord(
                            semantic_key="usr:COrderDlg::OnSave",
                            kind=EntityKind.METHOD,
                            name="OnSave",
                            qualified_name="COrderDlg::OnSave",
                        ),
                    ),
                    occurrences=(
                        SymbolOccurrenceRecord(
                            entity_semantic_key="usr:COrderDlg::OnSave",
                            occurrence_kind=OccurrenceKind.DEFINITION,
                            span=span_order,
                        ),
                    ),
                    dependencies=(
                        TUDependencyRecord(
                            repo_path="include/OrderDlg.h",
                            dependency_kind=DependencyKind.DIRECT_INCLUDE,
                        ),
                    ),
                ),
            )
            store.record_parse_run(
                gen1,
                ParseRunPayload(
                    tu_key=tu_helper.tu_key,
                    parser_name="test",
                    parser_version="0.1.0",
                    mfc_rule_version="0.1.0",
                    parse_status=ParseStatus.PARSED_CLEAN,
                    entities=(
                        EntityRecord(
                            semantic_key="tu:src/Helper.cpp:ComputeTax",
                            kind=EntityKind.FUNCTION,
                            name="ComputeTax",
                            qualified_name="ComputeTax",
                            linkage_scope=LinkageScope.INTERNAL_TU,
                            scope_qualifier="src/Helper.cpp",
                        ),
                    ),
                    occurrences=(
                        SymbolOccurrenceRecord(
                            entity_semantic_key="tu:src/Helper.cpp:ComputeTax",
                            occurrence_kind=OccurrenceKind.DEFINITION,
                            span=span_helper,
                        ),
                    ),
                    dependencies=(
                        TUDependencyRecord(
                            repo_path="include/OrderDlg.h",
                            dependency_kind=DependencyKind.DIRECT_INCLUDE,
                        ),
                    ),
                ),
            )
            store.publish_generation(gen1)

            # 1. Start a staging generation (gen2), overwrite OrderDlg with new symbol, then ROLLBACK.
            # Published generation must remain gen1 with original data untouched!
            gen2_aborted = store.begin_generation(notes="will rollback")
            store.record_parse_run(
                gen2_aborted,
                ParseRunPayload(
                    tu_key=tu_order.tu_key,
                    parser_name="test",
                    parser_version="0.1.0",
                    mfc_rule_version="0.1.0",
                    parse_status=ParseStatus.PARSED_CLEAN,
                    entities=(
                        EntityRecord(
                            semantic_key="usr:CorruptedSymbol",
                            kind=EntityKind.FUNCTION,
                            name="CorruptedSymbol",
                            qualified_name="CorruptedSymbol",
                        ),
                    ),
                    occurrences=(
                        SymbolOccurrenceRecord(
                            entity_semantic_key="usr:CorruptedSymbol",
                            occurrence_kind=OccurrenceKind.DEFINITION,
                            span=span_order,
                        ),
                    ),
                ),
            )
            store.rollback_generation(gen2_aborted)

            self.assertEqual(store.get_published_generation_id(), gen1)
            pub_entities = {
                e["semantic_key"]
                for e in store.query_published_entities(context_key=ctx.context_key)
            }
            self.assertEqual(
                pub_entities,
                {"usr:COrderDlg::OnSave", "tu:src/Helper.cpp:ComputeTax"},
            )

            # 2. Modify shared header OrderDlg.h -> both TUs must be invalidated via reverse dependency
            (self.repo_dir / "include" / "OrderDlg.h").write_text(
                "#pragma once\nclass COrderDlg {\npublic:\n    void OnSave();\n    void OnCancel();\n};\n",
                encoding="utf-8",
            )
            disc2 = discover_repository_files(
                self.repo_dir, output_paths_to_exclude=[self.db_path]
            )
            sync_diff = store.sync_file_inventory(disc2.files)
            self.assertEqual(sync_diff["modified"], ["include/OrderDlg.h"])

            inv_after_header = store.compute_invalidated_translation_units()
            self.assertEqual(len(inv_after_header), 2)
            for item in inv_after_header:
                self.assertIn("dependency_modified:include/OrderDlg.h", item["reasons"])

            # 3. Delete src/Helper.cpp -> sync marks deleted; new generation automatically retracts deleted TU
            (self.repo_dir / "src" / "Helper.cpp").unlink()
            disc3 = discover_repository_files(
                self.repo_dir, output_paths_to_exclude=[self.db_path]
            )
            sync_diff_del = store.sync_file_inventory(disc3.files)
            self.assertEqual(sync_diff_del["deleted"], ["src/Helper.cpp"])

            gen3 = store.begin_generation(notes="after helper deleted")
            # Re-parse remaining OrderDlg.cpp with updated header
            store.record_parse_run(
                gen3,
                ParseRunPayload(
                    tu_key=tu_order.tu_key,
                    parser_name="test",
                    parser_version="0.1.0",
                    mfc_rule_version="0.1.0",
                    parse_status=ParseStatus.PARSED_CLEAN,
                    entities=(
                        EntityRecord(
                            semantic_key="usr:COrderDlg::OnSave",
                            kind=EntityKind.METHOD,
                            name="OnSave",
                            qualified_name="COrderDlg::OnSave",
                        ),
                    ),
                    occurrences=(
                        SymbolOccurrenceRecord(
                            entity_semantic_key="usr:COrderDlg::OnSave",
                            occurrence_kind=OccurrenceKind.DEFINITION,
                            span=span_order,
                        ),
                    ),
                    dependencies=(
                        TUDependencyRecord(
                            repo_path="include/OrderDlg.h",
                            dependency_kind=DependencyKind.DIRECT_INCLUDE,
                        ),
                    ),
                    diagnostics=(
                        DiagnosticRecord(
                            severity=DiagnosticSeverity.INFO,
                            code="reindexed",
                            message="Re-indexed cleanly after header modification",
                            span=span_order,
                        ),
                    ),
                ),
            )
            store.publish_generation(gen3)

            pub_entities_after_del = {
                e["semantic_key"]
                for e in store.query_published_entities(context_key=ctx.context_key)
            }
            self.assertEqual(pub_entities_after_del, {"usr:COrderDlg::OnSave"})

    def test_cli_init_discover_scan_manifest_and_status(self) -> None:
        self._write_sample_repo_files()
        manifest_path = self.repo_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "contexts": [
                        {
                            "id": "x64_debug",
                            "project_name": "OrderApp",
                            "target_name": "OrderApp",
                            "configuration": "Debug",
                            "platform": "x64",
                            "include_dirs": ["include"],
                            "defines": ["_DEBUG"],
                        }
                    ],
                    "translation_units": [
                        {"file": "src/OrderDlg.cpp", "context_id": "x64_debug"},
                        {"file": "src/Helper.cpp", "context_id": "x64_debug"},
                    ],
                }
            ),
            encoding="utf-8",
        )

        # 1. init
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli_main(
                ["init", "--repo", str(self.repo_dir), "--db", str(self.db_path), "--json"]
            )
        self.assertEqual(rc, 0)
        init_out = json.loads(buf.getvalue())
        self.assertEqual(init_out["schema_version"], 1)

        # 2. discover
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli_main(
                ["discover", "--repo", str(self.repo_dir), "--db", str(self.db_path), "--json"]
            )
        self.assertEqual(rc, 0)
        disc_out = json.loads(buf.getvalue())
        self.assertEqual(disc_out["discovered_count"], 3)
        self.assertEqual(len(disc_out["sync"]["added"]), 3)

        # 3. scan-manifest
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli_main(
                [
                    "scan-manifest",
                    "--repo",
                    str(self.repo_dir),
                    "--db",
                    str(self.db_path),
                    "--manifest",
                    str(manifest_path),
                    "--json",
                ]
            )
        self.assertEqual(rc, 0)
        man_out = json.loads(buf.getvalue())
        self.assertEqual(man_out["imported_translation_units"], 2)
        self.assertEqual(man_out["invalidated_translation_units"], 2)

        # 4. status
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli_main(
                ["status", "--db", str(self.db_path), "--check-invalidated", "--json"]
            )
        self.assertEqual(rc, 0)
        stat_out = json.loads(buf.getvalue())
        self.assertEqual(stat_out["active_files"], 3)
        self.assertEqual(stat_out["build_contexts"], 1)
        self.assertEqual(stat_out["active_translation_units"], 2)
        self.assertEqual(len(stat_out["invalidated_translation_units"]), 2)


if __name__ == "__main__":
    unittest.main()
