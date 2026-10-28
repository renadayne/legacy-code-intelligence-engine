"""Integration tests for LegacyGraph QueryService against real SQLite IndexStore."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from legacygraph.discovery import PathContainmentError, inspect_source_file
from legacygraph.index import IndexStore
from legacygraph.models import (
    BuildContextSpec,
    CandidateTargetRecord,
    ConditionState,
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
)
from legacygraph.queries import (
    ContextNotFoundError,
    QueryService,
    SymbolNotFoundError,
)


def _span_for_lines(repo_path: str, text: str, start_line: int, end_line: int) -> SourceSpan:
    lines = text.splitlines(keepends=True)
    byte_start = sum(len(l.encode("utf-8")) for l in lines[: start_line - 1])
    byte_end = sum(len(l.encode("utf-8")) for l in lines[:end_line])
    end_col = len(lines[end_line - 1].rstrip("\r\n")) + 1 if end_line <= len(lines) else 1
    return SourceSpan(
        repo_path=repo_path,
        byte_start=byte_start,
        byte_end=max(byte_start, byte_end),
        start_line=start_line,
        start_col=1,
        end_line=end_line,
        end_col=max(1, end_col),
    )


class QueryServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo_root = self.root / "repo"
        self.repo_root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "out" / "index.db"

        self.header_text = (
            "#pragma once\n"
            "class COrderDlg {\n"
            "public:\n"
            "    void OnSave();\n"
            "    virtual void ValidateOrder();\n"
            "};\n"
        )
        self.cpp_text = (
            '#include "order_dlg.h"\n'
            "BEGIN_MESSAGE_MAP(COrderDlg, CDialog)\n"
            "    ON_BN_CLICKED(IDC_SAVE, &COrderDlg::OnSave)\n"
            "END_MESSAGE_MAP()\n"
            "void COrderDlg::OnSave() {\n"
            "    ValidateOrder();\n"
            "    SaveToDb();\n"
            "    pPlugin->RunHook();\n"
            "}\n"
            "void COrderDlg::ValidateOrder() {\n"
            "    CheckFields();\n"
            "}\n"
            "void SaveToDb() {\n"
            "    CommitTx();\n"
            "}\n"
            "void CommitTx() {}\n"
            "void CheckFields() {}\n"
            "void On_Save_Helper() {}\n"
            "void OnXSaveXHelper() {}\n"
        )
        self.rel_cpp_text = (
            '#include "order_dlg.h"\n'
            "void ReleaseOnlyOpt() {}\n"
            "void COrderDlg::OnSave() {\n"
            "    ReleaseOnlyOpt();\n"
            "}\n"
        )

        (self.repo_root / "src").mkdir(parents=True, exist_ok=True)
        self.header_path = self.repo_root / "src" / "order_dlg.h"
        self.cpp_path = self.repo_root / "src" / "order_dlg.cpp"
        self.rel_cpp_path = self.repo_root / "src" / "order_rel.cpp"

        self.header_path.write_text(self.header_text, encoding="utf-8")
        self.cpp_path.write_text(self.cpp_text, encoding="utf-8")
        self.rel_cpp_path.write_text(self.rel_cpp_text, encoding="utf-8")

        self.store = IndexStore(self.db_path)
        self.store.initialize_schema(self.repo_root, source_revision="rev-101")
        self.store.sync_file_inventory(
            [
                inspect_source_file(self.repo_root, "src/order_dlg.h"),
                inspect_source_file(self.repo_root, "src/order_dlg.cpp"),
                inspect_source_file(self.repo_root, "src/order_rel.cpp"),
            ]
        )

        self.debug_ctx = BuildContextSpec(
            project_name="Orders",
            target_name="OrdersApp",
            configuration="Debug",
            platform="x64",
            defines=("_DEBUG", "_UNICODE"),
            include_dirs=("src",),
            mfc_mode="shared",
        )
        self.release_ctx = BuildContextSpec(
            project_name="Orders",
            target_name="OrdersApp",
            configuration="Release",
            platform="x64",
            defines=("NDEBUG", "_UNICODE"),
            include_dirs=("src",),
            mfc_mode="shared",
        )
        self.debug_ctx_key = self.debug_ctx.context_key
        self.release_ctx_key = self.release_ctx.context_key

        self.debug_tu = TranslationUnitSpec(
            main_file_path="src/order_dlg.cpp",
            build_context=self.debug_ctx,
        )
        self.release_tu = TranslationUnitSpec(
            main_file_path="src/order_rel.cpp",
            build_context=self.release_ctx,
        )
        self.store.register_translation_units([self.debug_tu, self.release_tu])

        gen_id = self.store.begin_generation(
            parser_version="1.0.0", mfc_rule_version="1.0.0", notes="initial"
        )
        self._populate_debug_tu(gen_id)
        self._populate_release_tu(gen_id)
        self.store.publish_generation(gen_id)
        self.gen_id = gen_id
        self.service = QueryService(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def _populate_debug_tu(self, gen_id: int) -> None:
        h_rel = "src/order_dlg.h"
        c_rel = "src/order_dlg.cpp"

        entities = (
            EntityRecord("usr::COrderDlg", EntityKind.CLASS, "COrderDlg", "COrderDlg"),
            EntityRecord(
                "usr::COrderDlg::OnSave",
                EntityKind.METHOD,
                "OnSave",
                "COrderDlg::OnSave",
                parent_semantic_key="usr::COrderDlg",
            ),
            EntityRecord(
                "usr::COrderDlg::ValidateOrder",
                EntityKind.METHOD,
                "ValidateOrder",
                "COrderDlg::ValidateOrder",
                parent_semantic_key="usr::COrderDlg",
            ),
            EntityRecord(
                "usr::COrderExDlg::ValidateOrder",
                EntityKind.METHOD,
                "ValidateOrder",
                "COrderExDlg::ValidateOrder",
            ),
            EntityRecord("usr::SaveToDb", EntityKind.FUNCTION, "SaveToDb", "SaveToDb"),
            EntityRecord("usr::CommitTx", EntityKind.FUNCTION, "CommitTx", "CommitTx"),
            EntityRecord("usr::CheckFields", EntityKind.FUNCTION, "CheckFields", "CheckFields"),
            EntityRecord(
                "evt::COrderDlg::BN_CLICKED::IDC_SAVE",
                EntityKind.EVENT,
                "BN_CLICKED(IDC_SAVE)",
                "COrderDlg::BN_CLICKED::IDC_SAVE",
                linkage_scope=LinkageScope.CONFIG_SCOPED,
            ),
            EntityRecord(
                "usr::On_Save_Helper",
                EntityKind.FUNCTION,
                "On_Save_Helper",
                "On_Save_Helper",
            ),
            EntityRecord(
                "usr::OnXSaveXHelper",
                EntityKind.FUNCTION,
                "OnXSaveXHelper",
                "OnXSaveXHelper",
            ),
            EntityRecord(
                "usr::InactiveBranchFn",
                EntityKind.FUNCTION,
                "InactiveBranchFn",
                "InactiveBranchFn",
            ),
            EntityRecord(
                "usr::UnknownCondFn",
                EntityKind.FUNCTION,
                "UnknownCondFn",
                "UnknownCondFn",
            ),
            EntityRecord(
                "usr::DisconnectedNode",
                EntityKind.FUNCTION,
                "DisconnectedNode",
                "DisconnectedNode",
            ),
        )

        occurrences = (
            SymbolOccurrenceRecord(
                "usr::COrderDlg",
                OccurrenceKind.DEFINITION,
                _span_for_lines(h_rel, self.header_text, 2, 6),
                signature="class COrderDlg",
            ),
            SymbolOccurrenceRecord(
                "usr::COrderDlg::OnSave",
                OccurrenceKind.DECLARATION,
                _span_for_lines(h_rel, self.header_text, 4, 4),
                signature="void COrderDlg::OnSave()",
                parent_semantic_key="usr::COrderDlg",
            ),
            SymbolOccurrenceRecord(
                "usr::COrderDlg::OnSave",
                OccurrenceKind.DEFINITION,
                _span_for_lines(c_rel, self.cpp_text, 5, 9),
                signature="void COrderDlg::OnSave()",
                parent_semantic_key="usr::COrderDlg",
            ),
            SymbolOccurrenceRecord(
                "usr::COrderDlg::ValidateOrder",
                OccurrenceKind.DEFINITION,
                _span_for_lines(c_rel, self.cpp_text, 10, 12),
                signature="void COrderDlg::ValidateOrder()",
                parent_semantic_key="usr::COrderDlg",
            ),
            SymbolOccurrenceRecord(
                "usr::SaveToDb",
                OccurrenceKind.DEFINITION,
                _span_for_lines(c_rel, self.cpp_text, 13, 15),
                signature="void SaveToDb()",
            ),
            SymbolOccurrenceRecord(
                "usr::CommitTx",
                OccurrenceKind.DEFINITION,
                _span_for_lines(c_rel, self.cpp_text, 16, 16),
                signature="void CommitTx()",
            ),
            SymbolOccurrenceRecord(
                "usr::CheckFields",
                OccurrenceKind.DEFINITION,
                _span_for_lines(c_rel, self.cpp_text, 17, 17),
                signature="void CheckFields()",
            ),
            SymbolOccurrenceRecord(
                "usr::On_Save_Helper",
                OccurrenceKind.DEFINITION,
                _span_for_lines(c_rel, self.cpp_text, 18, 18),
                signature="void On_Save_Helper()",
            ),
            SymbolOccurrenceRecord(
                "usr::OnXSaveXHelper",
                OccurrenceKind.DEFINITION,
                _span_for_lines(c_rel, self.cpp_text, 19, 19),
                signature="void OnXSaveXHelper()",
            ),
            SymbolOccurrenceRecord(
                "usr::DisconnectedNode",
                OccurrenceKind.DEFINITION,
                _span_for_lines(c_rel, self.cpp_text, 17, 17),
                signature="void DisconnectedNode()",
            ),
        )

        obs_mfc = ObservationRecord(
            "obs::mfc_save",
            "mfc_message_map_entry",
            _span_for_lines(c_rel, self.cpp_text, 3, 3),
            subject_semantic_key="evt::COrderDlg::BN_CLICKED::IDC_SAVE",
            extraction_quality=ExtractionQuality.SYNTACTIC,
            rule_id="mfc.on_bn_clicked",
        )
        obs_call_val = ObservationRecord(
            "obs::call_validate",
            "call_expr",
            _span_for_lines(c_rel, self.cpp_text, 6, 6),
            subject_semantic_key="usr::COrderDlg::OnSave",
            rule_id="clang.call",
        )
        obs_call_db = ObservationRecord(
            "obs::call_savedb",
            "call_expr",
            _span_for_lines(c_rel, self.cpp_text, 7, 7),
            subject_semantic_key="usr::COrderDlg::OnSave",
            rule_id="clang.call",
        )
        obs_call_unres = ObservationRecord(
            "obs::call_unres",
            "call_expr",
            _span_for_lines(c_rel, self.cpp_text, 8, 8),
            subject_semantic_key="usr::COrderDlg::OnSave",
            rule_id="clang.call",
        )
        obs_call_commit = ObservationRecord(
            "obs::call_commit",
            "call_expr",
            _span_for_lines(c_rel, self.cpp_text, 14, 14),
            subject_semantic_key="usr::SaveToDb",
            rule_id="clang.call",
        )
        obs_call_check = ObservationRecord(
            "obs::call_check",
            "call_expr",
            _span_for_lines(c_rel, self.cpp_text, 11, 11),
            subject_semantic_key="usr::COrderDlg::ValidateOrder",
            rule_id="clang.call",
        )

        relations = (
            # MFC event registration (syntactic quality, valid event-registration)
            RelationRecord(
                source_semantic_key="evt::COrderDlg::BN_CLICKED::IDC_SAVE",
                target_semantic_key="usr::COrderDlg::OnSave",
                kind=RelationKind.HANDLES_EVENT,
                resolution_status=ResolutionStatus.RESOLVED,
                dispatch_kind=DispatchKind.EVENT_REGISTRATION,
                extraction_quality=ExtractionQuality.SYNTACTIC,
                reason=ResolutionReason.MFC_MACRO_EVIDENCE,
                evidence=(
                    RelationEvidenceRecord(
                        "obs::mfc_save",
                        obs_mfc.span,
                        rule_id="mfc.on_bn_clicked",
                        notes="ON_BN_CLICKED(IDC_SAVE, &COrderDlg::OnSave)",
                    ),
                ),
            ),
            # Virtual call (static target labelled + override candidate)
            RelationRecord(
                source_semantic_key="usr::COrderDlg::OnSave",
                target_semantic_key="usr::COrderDlg::ValidateOrder",
                kind=RelationKind.CALLS,
                resolution_status=ResolutionStatus.RESOLVED,
                dispatch_kind=DispatchKind.VIRTUAL,
                extraction_quality=ExtractionQuality.SEMANTIC,
                reason=ResolutionReason.STATIC_VIRTUAL_TARGET,
                evidence=(
                    RelationEvidenceRecord(
                        "obs::call_validate", obs_call_val.span, rule_id="clang.call"
                    ),
                ),
                candidates=(
                    CandidateTargetRecord(
                        "usr::COrderExDlg::ValidateOrder",
                        "virtual_override",
                        "obs::call_validate",
                    ),
                ),
            ),
            # Direct call OnSave -> SaveToDb
            RelationRecord(
                source_semantic_key="usr::COrderDlg::OnSave",
                target_semantic_key="usr::SaveToDb",
                kind=RelationKind.CALLS,
                resolution_status=ResolutionStatus.RESOLVED,
                dispatch_kind=DispatchKind.DIRECT,
                extraction_quality=ExtractionQuality.SEMANTIC,
                reason=ResolutionReason.EXACT_SEMANTIC,
                evidence=(
                    RelationEvidenceRecord(
                        "obs::call_savedb", obs_call_db.span, rule_id="clang.call"
                    ),
                ),
            ),
            # Unresolved indirect call on OnSave (target=None)
            RelationRecord(
                source_semantic_key="usr::COrderDlg::OnSave",
                target_semantic_key=None,
                kind=RelationKind.CALLS,
                resolution_status=ResolutionStatus.UNRESOLVED,
                dispatch_kind=DispatchKind.INDIRECT,
                extraction_quality=ExtractionQuality.SEMANTIC,
                reason=ResolutionReason.UNKNOWN_RECEIVER,
                unresolved_target_expr="pPlugin->RunHook()",
                evidence=(
                    RelationEvidenceRecord(
                        "obs::call_unres", obs_call_unres.span, rule_id="clang.call"
                    ),
                ),
                candidates=(
                    CandidateTargetRecord(
                        "usr::On_Save_Helper", "signature_match", "obs::call_unres"
                    ),
                ),
            ),
            # Inactive preprocessor branch relation (must be excluded from active graph)
            RelationRecord(
                source_semantic_key="usr::COrderDlg::OnSave",
                target_semantic_key="usr::InactiveBranchFn",
                kind=RelationKind.CALLS,
                resolution_status=ResolutionStatus.RESOLVED,
                dispatch_kind=DispatchKind.DIRECT,
                extraction_quality=ExtractionQuality.SEMANTIC,
                condition_state=ConditionState.INACTIVE,
                reason=ResolutionReason.EXACT_SEMANTIC,
                evidence=(
                    RelationEvidenceRecord(
                        "obs::call_savedb", obs_call_db.span, rule_id="clang.call"
                    ),
                ),
            ),
            # Unknown condition branch on ValidateOrder -> UnknownCondFn
            RelationRecord(
                source_semantic_key="usr::COrderDlg::ValidateOrder",
                target_semantic_key="usr::UnknownCondFn",
                kind=RelationKind.CALLS,
                resolution_status=ResolutionStatus.RESOLVED,
                dispatch_kind=DispatchKind.DIRECT,
                extraction_quality=ExtractionQuality.SEMANTIC,
                condition_state=ConditionState.UNKNOWN,
                reason=ResolutionReason.EXACT_SEMANTIC,
                evidence=(
                    RelationEvidenceRecord(
                        "obs::call_check", obs_call_check.span, rule_id="clang.call"
                    ),
                ),
            ),
            # ValidateOrder -> CheckFields
            RelationRecord(
                source_semantic_key="usr::COrderDlg::ValidateOrder",
                target_semantic_key="usr::CheckFields",
                kind=RelationKind.CALLS,
                resolution_status=ResolutionStatus.RESOLVED,
                dispatch_kind=DispatchKind.DIRECT,
                extraction_quality=ExtractionQuality.SEMANTIC,
                reason=ResolutionReason.EXACT_SEMANTIC,
                evidence=(
                    RelationEvidenceRecord(
                        "obs::call_check", obs_call_check.span, rule_id="clang.call"
                    ),
                ),
            ),
            # SaveToDb -> CommitTx
            RelationRecord(
                source_semantic_key="usr::SaveToDb",
                target_semantic_key="usr::CommitTx",
                kind=RelationKind.CALLS,
                resolution_status=ResolutionStatus.RESOLVED,
                dispatch_kind=DispatchKind.DIRECT,
                extraction_quality=ExtractionQuality.SEMANTIC,
                reason=ResolutionReason.EXACT_SEMANTIC,
                evidence=(
                    RelationEvidenceRecord(
                        "obs::call_commit", obs_call_commit.span, rule_id="clang.call"
                    ),
                ),
            ),
        )

        payload = ParseRunPayload(
            tu_key=self.debug_tu.tu_key,
            parser_name="clang",
            parser_version="1.0.0",
            mfc_rule_version="1.0.0",
            parse_status=ParseStatus.PARSED_CLEAN,
            entities=entities,
            occurrences=occurrences,
            observations=(
                obs_mfc,
                obs_call_val,
                obs_call_db,
                obs_call_unres,
                obs_call_commit,
                obs_call_check,
            ),
            relations=relations,
        )
        self.store.record_parse_run(gen_id, payload)

    def _populate_release_tu(self, gen_id: int) -> None:
        r_rel = "src/order_rel.cpp"
        entities = (
            EntityRecord("usr::COrderDlg", EntityKind.CLASS, "COrderDlg", "COrderDlg"),
            EntityRecord(
                "usr::COrderDlg::OnSave",
                EntityKind.METHOD,
                "OnSave",
                "COrderDlg::OnSave",
                parent_semantic_key="usr::COrderDlg",
            ),
            EntityRecord(
                "usr::ReleaseOnlyOpt",
                EntityKind.FUNCTION,
                "ReleaseOnlyOpt",
                "ReleaseOnlyOpt",
            ),
        )
        occurrences = (
            SymbolOccurrenceRecord(
                "usr::ReleaseOnlyOpt",
                OccurrenceKind.DEFINITION,
                _span_for_lines(r_rel, self.rel_cpp_text, 2, 2),
                signature="void ReleaseOnlyOpt()",
            ),
            SymbolOccurrenceRecord(
                "usr::COrderDlg::OnSave",
                OccurrenceKind.DEFINITION,
                _span_for_lines(r_rel, self.rel_cpp_text, 3, 5),
                signature="void COrderDlg::OnSave()",
            ),
        )
        obs_rel_call = ObservationRecord(
            "obs::rel_call",
            "call_expr",
            _span_for_lines(r_rel, self.rel_cpp_text, 4, 4),
            subject_semantic_key="usr::COrderDlg::OnSave",
            rule_id="clang.call",
        )
        relations = (
            RelationRecord(
                source_semantic_key="usr::COrderDlg::OnSave",
                target_semantic_key="usr::ReleaseOnlyOpt",
                kind=RelationKind.CALLS,
                resolution_status=ResolutionStatus.RESOLVED,
                dispatch_kind=DispatchKind.DIRECT,
                extraction_quality=ExtractionQuality.SEMANTIC,
                reason=ResolutionReason.EXACT_SEMANTIC,
                evidence=(
                    RelationEvidenceRecord(
                        "obs::rel_call", obs_rel_call.span, rule_id="clang.call"
                    ),
                ),
            ),
        )
        payload = ParseRunPayload(
            tu_key=self.release_tu.tu_key,
            parser_name="clang",
            parser_version="1.0.0",
            mfc_rule_version="1.0.0",
            parse_status=ParseStatus.PARSED_CLEAN,
            entities=entities,
            occurrences=occurrences,
            observations=(obs_rel_call,),
            relations=relations,
        )
        self.store.record_parse_run(gen_id, payload)

    def test_contexts_and_generation_snapshot(self) -> None:
        ctxs = self.service.contexts()
        self.assertEqual(len(ctxs), 2)
        by_key = {c["context_key"]: c for c in ctxs}
        self.assertIn(self.debug_ctx_key, by_key)
        self.assertIn(self.release_ctx_key, by_key)

        dbg = by_key[self.debug_ctx_key]
        self.assertEqual(dbg["configuration"], "Debug")
        self.assertEqual(dbg["published_generation_id"], self.gen_id)
        self.assertEqual(dbg["active_translation_units"], 1)
        self.assertEqual(dbg["published_tu_runs"], 1)
        self.assertTrue(dbg["is_complete"])
        self.assertFalse(dbg["incomplete"])
        self.assertEqual(dbg["generation"]["source_revision"], "rev-101")

    def test_search_exact_prefix_escaped_like_and_pagination(self) -> None:
        # Exact match ranked first
        res = self.service.search("OnSave", context_key=self.debug_ctx_key)
        self.assertGreaterEqual(res["total"], 1)
        self.assertEqual(res["items"][0]["semantic_key"], "usr::COrderDlg::OnSave")
        self.assertEqual(res["generation"]["generation_id"], self.gen_id)

        # Escaped LIKE: underscore must NOT act as single-char wildcard
        res_underscore = self.service.search("On_Save_", context_key=self.debug_ctx_key)
        keys = [it["semantic_key"] for it in res_underscore["items"]]
        self.assertEqual(keys, ["usr::On_Save_Helper"])
        self.assertNotIn("usr::OnXSaveXHelper", keys)

        # Escaped LIKE: '%' must NOT match everything
        res_percent = self.service.search("%", context_key=self.debug_ctx_key)
        self.assertEqual(res_percent["total"], 0)
        self.assertEqual(res_percent["items"], [])

        # Pagination
        page1 = self.service.search("", context_key=self.debug_ctx_key, limit=3, offset=0)
        self.assertEqual(len(page1["items"]), 3)
        self.assertTrue(page1["truncated"])
        self.assertTrue(page1["has_more"])
        self.assertEqual(page1["next_offset"], 3)

        page2 = self.service.search("", context_key=self.debug_ctx_key, limit=3, offset=3)
        self.assertEqual(len(page2["items"]), 3)
        self.assertEqual(
            set(it["semantic_key"] for it in page1["items"]).intersection(
                it["semantic_key"] for it in page2["items"]
            ),
            set(),
        )

    def test_strict_context_isolation(self) -> None:
        # ReleaseOnlyOpt must not appear in Debug search, symbol, neighbors, subgraph, or find_path
        dbg_search = self.service.search("ReleaseOnlyOpt", context_key=self.debug_ctx_key)
        self.assertEqual(dbg_search["total"], 0)

        with self.assertRaises(SymbolNotFoundError):
            self.service.symbol("usr::ReleaseOnlyOpt", self.debug_ctx_key)

        with self.assertRaises(SymbolNotFoundError):
            self.service.neighbors("usr::ReleaseOnlyOpt", self.debug_ctx_key)

        with self.assertRaises(SymbolNotFoundError):
            self.service.subgraph(["usr::ReleaseOnlyOpt"], self.debug_ctx_key)

        dbg_neigh = self.service.neighbors("usr::COrderDlg::OnSave", self.debug_ctx_key)
        dbg_targets = {e["target"] for e in dbg_neigh["edges"]}
        self.assertNotIn("usr::ReleaseOnlyOpt", dbg_targets)

        rel_neigh = self.service.neighbors("usr::COrderDlg::OnSave", self.release_ctx_key)
        rel_targets = {e["target"] for e in rel_neigh["edges"]}
        self.assertEqual(rel_targets, {"usr::ReleaseOnlyOpt"})

        cross_path = self.service.find_path(
            "usr::COrderDlg::OnSave", "usr::ReleaseOnlyOpt", self.debug_ctx_key
        )
        self.assertFalse(cross_path["found"])
        self.assertEqual(cross_path["status"], "not_found_within_scope")

    def test_symbol_occurrences_and_source_details(self) -> None:
        sym = self.service.symbol("usr::COrderDlg::OnSave", self.debug_ctx_key)
        self.assertEqual(sym["semantic_key"], "usr::COrderDlg::OnSave")
        self.assertEqual(sym["name"], "OnSave")
        self.assertEqual(sym["qualified_name"], "COrderDlg::OnSave")
        self.assertEqual(sym["kind"], "method")
        self.assertEqual(len(sym["occurrences"]), 2)
        # Primary source prefers active definition over declaration
        self.assertEqual(sym["source"]["occurrence_kind"], "definition")
        self.assertEqual(sym["source"]["repo_path"], "src/order_dlg.cpp")
        self.assertEqual(sym["source"]["start_line"], 5)
        self.assertEqual(sym["source"]["end_line"], 9)
        self.assertEqual(sym["source"]["status"], "ok")

    def test_neighbors_evidence_virtual_mfc_unresolved_and_conditions(self) -> None:
        out_res = self.service.neighbors(
            "usr::COrderDlg::OnSave", self.debug_ctx_key, direction="out"
        )
        self.assertTrue(out_res["incomplete"])  # Has unresolved call
        self.assertEqual(out_res["omitted_inactive_count"], 1)

        # Inactive target must not appear in nodes or visible edges
        node_keys = {n["semantic_key"] for n in out_res["nodes"]}
        self.assertNotIn("usr::InactiveBranchFn", node_keys)
        self.assertNotIn("unknown", {n["name"].lower() for n in out_res["nodes"]})

        edges_by_target = {e["target"]: e for e in out_res["edges"]}
        # 1. Virtual call labelled
        v_edge = edges_by_target["usr::COrderDlg::ValidateOrder"]
        self.assertTrue(v_edge["is_virtual_static_target"])
        self.assertEqual(v_edge["target_label"], "static_virtual_target")
        self.assertEqual(v_edge["dispatch"], "virtual")
        self.assertEqual(len(v_edge["candidates"]), 1)
        self.assertEqual(
            v_edge["candidates"][0]["semantic_key"], "usr::COrderExDlg::ValidateOrder"
        )
        self.assertEqual(v_edge["evidence"][0]["repo_path"], "src/order_dlg.cpp")
        self.assertEqual(v_edge["evidence"][0]["start_line"], 6)

        # 2. Unresolved call has explicit target=None and expression/candidates
        unres_edge = edges_by_target[None]
        self.assertIsNone(unres_edge["target"])
        self.assertEqual(unres_edge["status"], "unresolved")
        self.assertEqual(unres_edge["unresolved_target_expr"], "pPlugin->Invoke()" if False else "pPlugin->RunHook()")
        self.assertEqual(unres_edge["candidates"][0]["semantic_key"], "usr::On_Save_Helper")
        self.assertEqual(len(out_res["unresolved"]), 1)

        # 3. Inbound neighbors on OnSave: MFC event registration retains original direction
        in_res = self.service.neighbors(
            "usr::COrderDlg::OnSave", self.debug_ctx_key, direction="in"
        )
        self.assertEqual(len(in_res["edges"]), 1)
        mfc_edge = in_res["edges"][0]
        self.assertEqual(mfc_edge["source"], "evt::COrderDlg::BN_CLICKED::IDC_SAVE")
        self.assertEqual(mfc_edge["target"], "usr::COrderDlg::OnSave")
        self.assertEqual(mfc_edge["kind"], "handles_event")
        self.assertEqual(mfc_edge["target_label"], "event_registration")
        self.assertEqual(mfc_edge["evidence"][0]["line"], 3)

        # 4. Unknown condition on ValidateOrder is omitted from default graph and marks incomplete
        val_neigh = self.service.neighbors(
            "usr::COrderDlg::ValidateOrder", self.debug_ctx_key, direction="out"
        )
        self.assertTrue(val_neigh["incomplete"])
        self.assertEqual(val_neigh["omitted_unknown_condition_count"], 1)
        self.assertEqual([e["target"] for e in val_neigh["edges"]], ["usr::CheckFields"])

    def test_subgraph_cycles_self_edges_and_hub_bounds(self) -> None:
        # Add a cycle, self-edge, and hub to a new generation
        gen2 = self.store.begin_generation(parser_version="1.0.0", mfc_rule_version="1.0.0")
        c_rel = "src/order_dlg.cpp"
        sp = _span_for_lines(c_rel, self.cpp_text, 16, 16)
        hub_entities = [
            EntityRecord("usr::Hub", EntityKind.FUNCTION, "Hub", "Hub"),
            EntityRecord("usr::CycleA", EntityKind.FUNCTION, "CycleA", "CycleA"),
            EntityRecord("usr::CycleB", EntityKind.FUNCTION, "CycleB", "CycleB"),
        ] + [
            EntityRecord(f"usr::Leaf{i:02d}", EntityKind.FUNCTION, f"Leaf{i:02d}", f"Leaf{i:02d}")
            for i in range(15)
        ]
        obs = ObservationRecord("obs::hub", "call_expr", sp, subject_semantic_key="usr::Hub")
        ev = (RelationEvidenceRecord("obs::hub", sp, rule_id="clang.call"),)
        rels = [
            # Self-edge CycleA -> CycleA
            RelationRecord(
                "usr::CycleA",
                "usr::CycleA",
                RelationKind.CALLS,
                ResolutionStatus.RESOLVED,
                DispatchKind.DIRECT,
                ExtractionQuality.SEMANTIC,
                evidence=ev,
            ),
            # 2-cycle CycleA <-> CycleB
            RelationRecord(
                "usr::CycleA",
                "usr::CycleB",
                RelationKind.CALLS,
                ResolutionStatus.RESOLVED,
                DispatchKind.DIRECT,
                ExtractionQuality.SEMANTIC,
                evidence=ev,
            ),
            RelationRecord(
                "usr::CycleB",
                "usr::CycleA",
                RelationKind.CALLS,
                ResolutionStatus.RESOLVED,
                DispatchKind.DIRECT,
                ExtractionQuality.SEMANTIC,
                evidence=ev,
            ),
        ] + [
            RelationRecord(
                "usr::Hub",
                f"usr::Leaf{i:02d}",
                RelationKind.CALLS,
                ResolutionStatus.RESOLVED,
                DispatchKind.DIRECT,
                ExtractionQuality.SEMANTIC,
                evidence=ev,
            )
            for i in range(15)
        ]
        tu2 = TranslationUnitSpec(
            main_file_path=c_rel,
            build_context=self.debug_ctx,
            per_file_defines=("EXTRA_TU=1",),
        )
        self.store.register_translation_units([tu2])
        self.store.record_parse_run(
            gen2,
            ParseRunPayload(
                tu_key=tu2.tu_key,
                parser_name="clang",
                parser_version="1.0.0",
                mfc_rule_version="1.0.0",
                parse_status=ParseStatus.PARSED_CLEAN,
                entities=tuple(hub_entities),
                observations=(obs,),
                relations=tuple(rels),
            ),
        )
        self.store.publish_generation(gen2)

        cyc_sub = self.service.subgraph(["usr::CycleA"], self.debug_ctx_key, depth=4)
        self.assertEqual({n["semantic_key"] for n in cyc_sub["nodes"]}, {"usr::CycleA", "usr::CycleB"})
        self.assertGreaterEqual(cyc_sub["self_edges_count"], 1)
        self.assertGreaterEqual(cyc_sub["cycles_detected"], 2)
        self.assertFalse(cyc_sub["truncated"])

        # Hub capped by max_nodes
        hub_nodes_cap = self.service.subgraph(
            ["usr::Hub"], self.debug_ctx_key, depth=1, max_nodes=5, max_edges=100
        )
        self.assertEqual(len(hub_nodes_cap["nodes"]), 5)
        self.assertTrue(hub_nodes_cap["truncated"])
        self.assertEqual(hub_nodes_cap["truncation_reason"], "max_nodes")

        # Hub capped by max_edges
        hub_edges_cap = self.service.subgraph(
            ["usr::Hub"], self.debug_ctx_key, depth=1, max_nodes=100, max_edges=4
        )
        self.assertEqual(len(hub_edges_cap["edges"]), 4)
        self.assertTrue(hub_edges_cap["truncated"])
        self.assertEqual(hub_edges_cap["truncation_reason"], "max_edges")

        # Single logical relation with > MAX_EVIDENCE_PER_EDGE callsites has bounded evidence hydration
        gen3 = self.store.begin_generation(parser_version="1.0.0", mfc_rule_version="1.0.0")
        many_obs = tuple(
            ObservationRecord(f"obs::many_{i}", "call_expr", sp, subject_semantic_key="usr::CycleA")
            for i in range(60)
        )
        many_ev = tuple(
            RelationEvidenceRecord(f"obs::many_{i}", sp, rule_id="clang.call")
            for i in range(60)
        )
        self.store.record_parse_run(
            gen3,
            ParseRunPayload(
                tu_key=tu2.tu_key,
                parser_name="clang",
                parser_version="1.0.0",
                mfc_rule_version="1.0.0",
                parse_status=ParseStatus.PARSED_CLEAN,
                entities=(
                    EntityRecord("usr::CycleA", EntityKind.FUNCTION, "CycleA", "CycleA"),
                    EntityRecord("usr::CycleB", EntityKind.FUNCTION, "CycleB", "CycleB"),
                ),
                observations=many_obs,
                relations=(
                    RelationRecord(
                        "usr::CycleA",
                        "usr::CycleB",
                        RelationKind.CALLS,
                        ResolutionStatus.RESOLVED,
                        DispatchKind.DIRECT,
                        ExtractionQuality.SEMANTIC,
                        evidence=many_ev,
                    ),
                ),
            ),
        )
        self.store.publish_generation(gen3)
        bounded_ev_neigh = self.service.neighbors("usr::CycleA", self.debug_ctx_key)
        self.assertEqual(len(bounded_ev_neigh["edges"]), 1)
        self.assertLessEqual(len(bounded_ev_neigh["edges"][0]["evidence"]), 25)
        self.assertTrue(bounded_ev_neigh["edges"][0]["evidence_sampled"])

    def test_find_path_found_inbound_no_path_truncated_and_incomplete(self) -> None:
        # 1. Path found from MFC event -> OnSave -> SaveToDb -> CommitTx
        res = self.service.find_path(
            "evt::COrderDlg::BN_CLICKED::IDC_SAVE",
            "usr::CommitTx",
            self.debug_ctx_key,
        )
        self.assertTrue(res["found"])
        self.assertEqual(res["status"], "found")
        self.assertTrue(res["path_exists"])
        self.assertFalse(res["runtime_reachability_proven"])
        self.assertEqual(
            res["path"],
            [
                "evt::COrderDlg::BN_CLICKED::IDC_SAVE",
                "usr::COrderDlg::OnSave",
                "usr::SaveToDb",
                "usr::CommitTx",
            ],
        )
        self.assertEqual(len(res["edges"]), 3)

        # 2. Inbound path (CommitTx -> OnSave with direction='in') retains original edge directions
        in_path = self.service.find_path(
            "usr::CommitTx",
            "usr::COrderDlg::OnSave",
            self.debug_ctx_key,
            direction="in",
        )
        self.assertTrue(in_path["found"])
        self.assertEqual(
            in_path["path"],
            ["usr::CommitTx", "usr::SaveToDb", "usr::COrderDlg::OnSave"],
        )
        self.assertEqual(in_path["edges"][0]["source"], "usr::SaveToDb")
        self.assertEqual(in_path["edges"][0]["target"], "usr::CommitTx")
        self.assertEqual(in_path["edges"][1]["source"], "usr::COrderDlg::OnSave")
        self.assertEqual(in_path["edges"][1]["target"], "usr::SaveToDb")

        # 3. Truncated by max_depth (does not claim path_exists=False)
        trunc_res = self.service.find_path(
            "evt::COrderDlg::BN_CLICKED::IDC_SAVE",
            "usr::CommitTx",
            self.debug_ctx_key,
            max_depth=2,
        )
        self.assertFalse(trunc_res["found"])
        self.assertEqual(trunc_res["status"], "truncated")
        self.assertTrue(trunc_res["truncated"])
        self.assertEqual(trunc_res["truncation_reason"], "max_depth")
        self.assertIsNone(trunc_res["path_exists"])

        # 4. Incomplete frontier (OnSave has unresolved call, ValidateOrder has unknown condition)
        inc_res = self.service.find_path(
            "usr::COrderDlg::OnSave",
            "usr::UnknownCondFn",
            self.debug_ctx_key,
        )
        self.assertFalse(inc_res["found"])
        self.assertEqual(inc_res["status"], "incomplete_index")
        self.assertTrue(inc_res["incomplete"])
        self.assertIsNone(inc_res["path_exists"])

        # 5. Complete subgraph with no path (SaveToDb -> CheckFields has no unresolved/unknown edges)
        no_path = self.service.find_path(
            "usr::SaveToDb",
            "usr::CheckFields",
            self.debug_ctx_key,
        )
        self.assertFalse(no_path["found"])
        self.assertEqual(no_path["status"], "not_found_within_scope")
        self.assertFalse(no_path["truncated"])
        self.assertFalse(no_path["incomplete"])
        self.assertEqual(no_path["path_exists"], False)

        # 6. Inactive branch is not traversed and does not create a path from SaveToDb
        inact_path = self.service.find_path(
            "usr::SaveToDb",
            "usr::InactiveBranchFn",
            self.debug_ctx_key,
        )
        self.assertFalse(inact_path["found"])
        self.assertEqual(inact_path["status"], "not_found_within_scope")

        # 7. Zero-hop path (source == target) works without requiring a self-loop edge
        zero_hop = self.service.find_path(
            "usr::SaveToDb",
            "usr::SaveToDb",
            self.debug_ctx_key,
        )
        self.assertTrue(zero_hop["found"])
        self.assertEqual(zero_hop["status"], "found")
        self.assertEqual(zero_hop["path"], ["usr::SaveToDb"])
        self.assertEqual(zero_hop["edges"], [])

    def test_snippet_fresh_truncated_stale_missing_and_containment(self) -> None:
        # 1. Fresh snippet
        snip = self.service.snippet("usr::COrderDlg::OnSave", self.debug_ctx_key)
        self.assertEqual(snip["status"], "ok")
        self.assertFalse(snip["is_stale"])
        self.assertEqual(snip["indexed_content_sha256"], snip["current_content_sha256"])
        self.assertIn("ValidateOrder();", snip["content"])
        self.assertEqual(snip["start_line"], 5)
        self.assertEqual(snip["end_line"], 9)

        # 2. Truncated snippet with max_lines=2
        snip_short = self.service.snippet(
            "usr::COrderDlg::OnSave", self.debug_ctx_key, max_lines=2
        )
        self.assertEqual(snip_short["status"], "ok")
        self.assertTrue(snip_short["truncated"])
        self.assertEqual(snip_short["truncation_reason"], "max_lines")
        self.assertEqual(snip_short["line_count"], 2)
        self.assertEqual(snip_short["omitted_lines"], 3)

        # 3. Stale source file refused
        self.cpp_path.write_text(self.cpp_text + "\n// modified on disk\n", encoding="utf-8")
        stale_snip = self.service.snippet("usr::COrderDlg::OnSave", self.debug_ctx_key)
        self.assertEqual(stale_snip["status"], "stale")
        self.assertTrue(stale_snip["is_stale"])
        self.assertIsNone(stale_snip["content"])
        self.assertNotEqual(
            stale_snip["indexed_content_sha256"], stale_snip["current_content_sha256"]
        )

        # 4. Missing source file refused
        self.cpp_path.unlink()
        missing_snip = self.service.snippet("usr::COrderDlg::OnSave", self.debug_ctx_key)
        self.assertEqual(missing_snip["status"], "missing")
        self.assertTrue(missing_snip["is_stale"])
        self.assertIsNone(missing_snip["content"])
        self.assertIsNone(missing_snip["current_content_sha256"])

        # Restore file, then test path containment violation
        self.cpp_path.write_text(self.cpp_text, encoding="utf-8")
        outside_file = self.root / "outside_secret.cpp"
        outside_file.write_text("SECRET_TOKEN = 123;", encoding="utf-8")
        with self.store._conn:
            self.store._conn.execute(
                "UPDATE files SET repo_path = '../outside_secret.cpp' WHERE repo_path = 'src/order_dlg.cpp'"
            )
        with self.assertRaises(PathContainmentError):
            self.service.snippet("usr::COrderDlg::OnSave", self.debug_ctx_key)

    def test_invalid_bounds_and_arguments(self) -> None:
        with self.assertRaises(ValueError):
            self.service.search("x", limit=0)
        with self.assertRaises(ValueError):
            self.service.search("x", limit=10_000)
        with self.assertRaises(ValueError):
            self.service.search("x", offset=-1)
        with self.assertRaises(ValueError):
            self.service.search("x", limit=True)  # type: ignore[arg-type]
        with self.assertRaises(ContextNotFoundError):
            self.service.search("x", context_key="nonexistent:ctx")

        with self.assertRaises(ValueError):
            self.service.neighbors("usr::SaveToDb", "", direction="out")
        with self.assertRaises(ValueError):
            self.service.neighbors("usr::SaveToDb", self.debug_ctx_key, direction="sideways")
        with self.assertRaises(ValueError):
            self.service.neighbors("usr::SaveToDb", self.debug_ctx_key, limit=-5)
        with self.assertRaises(ValueError):
            self.service.neighbors("usr::SaveToDb", self.debug_ctx_key, kinds="calls")  # type: ignore[arg-type]

        with self.assertRaises(ValueError):
            self.service.subgraph("usr::SaveToDb", self.debug_ctx_key)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            self.service.subgraph(["usr::SaveToDb"], self.debug_ctx_key, depth=-1)
        with self.assertRaises(ValueError):
            self.service.subgraph(["usr::SaveToDb"], self.debug_ctx_key, max_nodes=0)
        with self.assertRaises(ValueError):
            self.service.subgraph(["usr::SaveToDb"], self.debug_ctx_key, max_edges=-1)

        with self.assertRaises(ValueError):
            self.service.find_path("usr::SaveToDb", "usr::CommitTx", self.debug_ctx_key, max_depth=-1)
        with self.assertRaises(ValueError):
            self.service.find_path("usr::SaveToDb", "usr::CommitTx", self.debug_ctx_key, max_nodes=0)
        with self.assertRaises(ValueError):
            self.service.find_path("usr::SaveToDb", "usr::CommitTx", self.debug_ctx_key, timeout_seconds=0)
        with self.assertRaises(ValueError):
            self.service.find_path("usr::SaveToDb", "usr::CommitTx", self.debug_ctx_key, timeout_seconds=999)
        with self.assertRaises(ValueError):
            self.service.find_path("usr::SaveToDb", "usr::CommitTx", self.debug_ctx_key, timeout_seconds=float("inf"))
        with self.assertRaises(ValueError):
            self.service.find_path("usr::SaveToDb", "usr::CommitTx", self.debug_ctx_key, timeout_seconds=float("nan"))

        with self.assertRaises(ValueError):
            self.service.snippet("usr::SaveToDb", self.debug_ctx_key, max_lines=0)
        with self.assertRaises(ValueError):
            self.service.snippet("usr::SaveToDb", self.debug_ctx_key, max_lines=99999)


if __name__ == "__main__":
    unittest.main()
