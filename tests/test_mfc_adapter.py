"""Unit tests for MFC semantic payload adapter (`legacygraph/mfc_adapter.py`)."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from legacygraph.mfc import MFC_RULE_VERSION
from legacygraph.mfc_adapter import augment_payload
from legacygraph.models import (
    BuildContextSpec,
    ConditionState,
    DependencyKind,
    DiagnosticSeverity,
    DispatchKind,
    EntityKind,
    EntityRecord,
    ExtractionQuality,
    OccurrenceKind,
    ParseRunPayload,
    ParseStatus,
    RelationKind,
    ResolutionReason,
    ResolutionStatus,
    SourceSpan,
    SymbolOccurrenceRecord,
    TUDependencyRecord,
    TranslationUnitSpec,
)


def _make_spec(main_file: str = "src/OrderDlg.cpp", config: str = "Debug") -> TranslationUnitSpec:
    ctx = BuildContextSpec(
        project_name="OrderApp",
        target_name="OrderApp",
        configuration=config,
        platform="x64",
        defines=("_WINDOWS", "_AFXDLL"),
    )
    return TranslationUnitSpec(main_file_path=main_file, build_context=ctx)


def _make_payload(
    spec: TranslationUnitSpec,
    *,
    entities: tuple[EntityRecord, ...] = (),
    occurrences: tuple[SymbolOccurrenceRecord, ...] = (),
    dependencies: tuple[TUDependencyRecord, ...] = (),
    parse_status: ParseStatus = ParseStatus.PARSED_CLEAN,
) -> ParseRunPayload:
    return ParseRunPayload(
        tu_key=spec.tu_key,
        parser_name="libclang",
        parser_version="17.0.6",
        mfc_rule_version="",
        parse_status=parse_status,
        entities=entities,
        occurrences=occurrences,
        dependencies=dependencies,
    )


class TestMfcAdapter(unittest.TestCase):
    def test_01_active_unique_method_resolves_with_exact_evidence_and_no_guessing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src_dir = root / "src"
            src_dir.mkdir()
            cpp_path = src_dir / "OrderDlg.cpp"
            source = (
                b"// Comment fake: BEGIN_MESSAGE_MAP(CFake, CDialog) ON_WM_PAINT() END_MESSAGE_MAP()\r\n"
                b'const char* kRaw = R"(BEGIN_MESSAGE_MAP(CFake2, CDialog) ON_WM_CLOSE() END_MESSAGE_MAP())";\r\n'
                b"BEGIN_MESSAGE_MAP(COrderDlg, CDialogEx)\r\n"
                b"    ON_WM_CREATE()\r\n"
                b"    ON_COMMAND(ID_FILE_SAVE, &COrderDlg::OnFileSave)\r\n"
                b"    ON_COMMAND_RANGE(ID_CMD_FIRST, ID_CMD_LAST, OnCmdRange)\r\n"
                b"    ON_COMMAND(ID_WRONG_KIND, &COrderDlg::NotAMethod)\r\n"
                b"    ON_COMMAND(ID_SUFFIX_ONLY, &COrderDlg::OnSuffixOnly)\r\n"
                b"END_MESSAGE_MAP()\r\n"
            )
            cpp_path.write_bytes(source)

            spec = _make_spec("src/OrderDlg.cpp")
            entities = (
                EntityRecord("usr:class:COrderDlg", EntityKind.CLASS, "COrderDlg", "COrderDlg"),
                EntityRecord(
                    "usr:method:COrderDlg::OnCreate",
                    EntityKind.METHOD,
                    "OnCreate",
                    "COrderDlg::OnCreate",
                    parent_semantic_key="usr:class:COrderDlg",
                ),
                EntityRecord(
                    "usr:method:COrderDlg::OnFileSave",
                    EntityKind.METHOD,
                    "OnFileSave",
                    "COrderDlg::OnFileSave",
                    parent_semantic_key="usr:class:COrderDlg",
                ),
                EntityRecord(
                    "usr:method:COrderDlg::OnCmdRange",
                    EntityKind.METHOD,
                    "OnCmdRange",
                    "COrderDlg::OnCmdRange",
                    parent_semantic_key="usr:class:COrderDlg",
                ),
                # Same qualified_name but FUNCTION, not METHOD -> must NOT bind
                EntityRecord(
                    "usr:func:COrderDlg::NotAMethod",
                    EntityKind.FUNCTION,
                    "NotAMethod",
                    "COrderDlg::NotAMethod",
                ),
                # Suffix match inside namespace -> must NOT bind to COrderDlg::OnSuffixOnly
                EntityRecord(
                    "usr:method:ns::COrderDlg::OnSuffixOnly",
                    EntityKind.METHOD,
                    "OnSuffixOnly",
                    "ns::COrderDlg::OnSuffixOnly",
                ),
            )
            payload = _make_payload(spec, entities=entities)
            out = augment_payload(payload, spec, root)

            self.assertEqual(out.mfc_rule_version, MFC_RULE_VERSION)
            self.assertEqual(out.parse_status, ParseStatus.PARSED_CLEAN)
            self.assertTrue(any(d.repo_path == "src/OrderDlg.cpp" and d.dependency_kind == DependencyKind.MAIN_SOURCE for d in out.dependencies))

            event_entities = [e for e in out.entities if e.kind == EntityKind.EVENT]
            self.assertEqual(len(event_entities), 5)
            for ev in event_entities:
                self.assertEqual(ev.parent_semantic_key, "usr:class:COrderDlg")

            # Verify symbolic range preservation in event entity
            range_ev = next(e for e in event_entities if "ID_CMD_FIRST..ID_CMD_LAST" in e.name)
            self.assertIn("command_range:ID_CMD_FIRST..ID_CMD_LAST", range_ev.qualified_name)

            rels_by_expr_or_target = {
                (r.target_semantic_key or r.unresolved_target_expr): r
                for r in out.relations
                if r.kind == RelationKind.HANDLES_EVENT
            }
            self.assertEqual(len(rels_by_expr_or_target), 5)

            # 3 resolved exact METHOD matches
            for target_key in (
                "usr:method:COrderDlg::OnCreate",
                "usr:method:COrderDlg::OnFileSave",
                "usr:method:COrderDlg::OnCmdRange",
            ):
                rel = rels_by_expr_or_target[target_key]
                self.assertEqual(rel.resolution_status, ResolutionStatus.RESOLVED)
                self.assertEqual(rel.dispatch_kind, DispatchKind.EVENT_REGISTRATION)
                self.assertEqual(rel.extraction_quality, ExtractionQuality.SYNTACTIC)
                self.assertEqual(rel.condition_state, ConditionState.ACTIVE)
                self.assertEqual(rel.reason, ResolutionReason.MFC_MACRO_EVIDENCE)
                self.assertEqual(len(rel.evidence), 1)
                ev_rec = rel.evidence[0]
                self.assertEqual(ev_rec.span.repo_path, "src/OrderDlg.cpp")
                macro_slice = source[ev_rec.span.byte_start : ev_rec.span.byte_end]
                self.assertTrue(macro_slice.startswith(b"ON_"))

            # Non-METHOD and suffix-only entities remain UNRESOLVED with no candidates
            for unresolved_qname in ("COrderDlg::NotAMethod", "COrderDlg::OnSuffixOnly"):
                rel = rels_by_expr_or_target[unresolved_qname]
                self.assertEqual(rel.resolution_status, ResolutionStatus.UNRESOLVED)
                self.assertIsNone(rel.target_semantic_key)
                self.assertEqual(rel.candidates, ())

    def test_02_overloaded_and_missing_handlers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cpp_path = root / "Dlg.cpp"
            cpp_path.write_bytes(
                b"BEGIN_MESSAGE_MAP(CDlg, CDialog)\n"
                b"    ON_COMMAND(ID_OVERLOAD, &CDlg::OnOverload)\n"
                b"    ON_BN_CLICKED(IDC_MISSING, &CDlg::OnMissing)\n"
                b"END_MESSAGE_MAP()\n"
            )
            spec = _make_spec("Dlg.cpp")
            entities = (
                EntityRecord("usr:CDlg::OnOverload(void)", EntityKind.METHOD, "OnOverload", "CDlg::OnOverload"),
                EntityRecord("usr:CDlg::OnOverload(UINT)", EntityKind.METHOD, "OnOverload", "CDlg::OnOverload"),
            )
            out = augment_payload(_make_payload(spec, entities=entities), spec, root)
            self.assertEqual(len(out.relations), 2)

            ambig = next(r for r in out.relations if r.unresolved_target_expr == "CDlg::OnOverload")
            self.assertEqual(ambig.resolution_status, ResolutionStatus.AMBIGUOUS)
            self.assertIsNone(ambig.target_semantic_key)
            self.assertEqual(ambig.reason, ResolutionReason.OVERLOAD_AMBIGUOUS)
            self.assertEqual(
                {c.candidate_semantic_key for c in ambig.candidates},
                {"usr:CDlg::OnOverload(void)", "usr:CDlg::OnOverload(UINT)"},
            )

            missing = next(r for r in out.relations if r.unresolved_target_expr == "CDlg::OnMissing")
            self.assertEqual(missing.resolution_status, ResolutionStatus.UNRESOLVED)
            self.assertIsNone(missing.target_semantic_key)
            self.assertEqual(missing.candidates, ())

    def test_03_condition_states_active_inactive_unknown_and_missing_offset_dict_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            main_cpp = root / "CondDlg.cpp"
            header_h = root / "SharedMap.h"
            cpp_bytes = (
                b"BEGIN_MESSAGE_MAP(CCondDlg, CDialog)\n"
                b"    ON_COMMAND(ID_ACTIVE, &CCondDlg::OnActive)\n"
                b"#if 0\n"
                b"    ON_COMMAND(ID_DEAD, &CCondDlg::OnDead)\n"
                b"#endif\n"
                b"#ifdef _DEBUG\n"
                b"    ON_COMMAND(ID_DBG, &CCondDlg::OnDbg)\n"
                b"#else\n"
                b"    ON_COMMAND(ID_REL, &CCondDlg::OnRel)\n"
                b"#endif\n"
                b"    ON_COMMAND(ID_INACTIVE_OCC, &CCondDlg::OnInactiveOcc)\n"
                b"END_MESSAGE_MAP()\n"
            )
            hdr_bytes = (
                b"BEGIN_MESSAGE_MAP(CHdrDlg, CDialog)\n"
                b"    ON_COMMAND(ID_HDR_CMD, &CHdrDlg::OnHdrCmd)\n"
                b"END_MESSAGE_MAP()\n"
            )
            main_cpp.write_bytes(cpp_bytes)
            header_h.write_bytes(hdr_bytes)

            spec = _make_spec("CondDlg.cpp")
            entities = (
                EntityRecord("usr:CCondDlg::OnActive", EntityKind.METHOD, "OnActive", "CCondDlg::OnActive"),
                EntityRecord("usr:CCondDlg::OnDead", EntityKind.METHOD, "OnDead", "CCondDlg::OnDead"),
                EntityRecord("usr:CCondDlg::OnDbg", EntityKind.METHOD, "OnDbg", "CCondDlg::OnDbg"),
                EntityRecord("usr:CCondDlg::OnRel", EntityKind.METHOD, "OnRel", "CCondDlg::OnRel"),
                EntityRecord("usr:CCondDlg::OnInactiveOcc", EntityKind.METHOD, "OnInactiveOcc", "CCondDlg::OnInactiveOcc"),
                EntityRecord("usr:CHdrDlg::OnHdrCmd", EntityKind.METHOD, "OnHdrCmd", "CHdrDlg::OnHdrCmd"),
            )
            occurrences = (
                SymbolOccurrenceRecord(
                    entity_semantic_key="usr:CCondDlg::OnInactiveOcc",
                    occurrence_kind=OccurrenceKind.DECLARATION,
                    span=SourceSpan("CondDlg.cpp", 0, 10, 1, 1, 1, 11),
                    condition_state=ConditionState.INACTIVE,
                ),
            )
            deps = (TUDependencyRecord(repo_path="SharedMap.h", dependency_kind=DependencyKind.DIRECT_INCLUDE),)
            base_payload = _make_payload(spec, entities=entities, occurrences=occurrences, dependencies=deps)

            # Case A: active_macro_offsets=None -> #ifdef branches are UNKNOWN (unresolved with candidate)
            out_no_offsets = augment_payload(base_payload, spec, root, active_macro_offsets=None)
            obs_by_id = {
                o.details.get("event_id"): o
                for o in out_no_offsets.observations
                if o.observation_type == "mfc_event_mapping"
            }
            self.assertEqual(obs_by_id["ID_ACTIVE"].condition_state, ConditionState.ACTIVE)
            self.assertEqual(obs_by_id["ID_DEAD"].condition_state, ConditionState.INACTIVE)
            self.assertEqual(obs_by_id["ID_DBG"].condition_state, ConditionState.UNKNOWN)
            self.assertEqual(obs_by_id["ID_REL"].condition_state, ConditionState.UNKNOWN)
            self.assertEqual(obs_by_id["ID_INACTIVE_OCC"].condition_state, ConditionState.INACTIVE)

            # Inactive mappings (ID_DEAD and ID_INACTIVE_OCC) must NOT produce active or resolved relations
            for rel in out_no_offsets.relations:
                self.assertNotEqual(rel.target_semantic_key, "usr:CCondDlg::OnDead")
                self.assertNotEqual(rel.target_semantic_key, "usr:CCondDlg::OnInactiveOcc")
                self.assertNotEqual(rel.unresolved_target_expr, "CCondDlg::OnDead")
                self.assertNotEqual(rel.unresolved_target_expr, "CCondDlg::OnInactiveOcc")

            # Unknown branch with unique handler -> UNRESOLVED with candidate (not resolved)
            dbg_rel = next(r for r in out_no_offsets.relations if r.unresolved_target_expr == "CCondDlg::OnDbg")
            self.assertEqual(dbg_rel.condition_state, ConditionState.UNKNOWN)
            self.assertEqual(dbg_rel.resolution_status, ResolutionStatus.UNRESOLVED)
            self.assertIsNone(dbg_rel.target_semantic_key)
            self.assertEqual(len(dbg_rel.candidates), 1)
            self.assertEqual(dbg_rel.candidates[0].candidate_semantic_key, "usr:CCondDlg::OnDbg")

            # Case B: active_macro_offsets provided for CondDlg.cpp, but SharedMap.h is missing from dict
            active_offsets = {
                "CondDlg.cpp": {
                    cpp_bytes.index(b"BEGIN_MESSAGE_MAP"),
                    cpp_bytes.index(b"ON_COMMAND(ID_ACTIVE"),
                    cpp_bytes.index(b"ON_COMMAND(ID_DBG"),
                    cpp_bytes.index(b"END_MESSAGE_MAP"),
                }
            }
            out_with_offsets = augment_payload(base_payload, spec, root, active_macro_offsets=active_offsets)
            resolved_targets = {
                r.target_semantic_key
                for r in out_with_offsets.relations
                if r.resolution_status == ResolutionStatus.RESOLVED
            }
            # ID_ACTIVE and ID_DBG are confirmed active and resolved; ID_REL is inactive; SharedMap.h is unknown!
            self.assertEqual(resolved_targets, {"usr:CCondDlg::OnActive", "usr:CCondDlg::OnDbg"})
            hdr_rel = next(r for r in out_with_offsets.relations if r.unresolved_target_expr == "CHdrDlg::OnHdrCmd")
            self.assertEqual(hdr_rel.condition_state, ConditionState.UNKNOWN)
            self.assertEqual(hdr_rel.resolution_status, ResolutionStatus.UNRESOLVED)
            self.assertEqual(hdr_rel.candidates[0].candidate_semantic_key, "usr:CHdrDlg::OnHdrCmd")

    def test_04_unsupported_and_malformed_macros_emit_diagnostics_without_upgrading_or_failing_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cpp_path = root / "DiagDlg.cpp"
            cpp_path.write_bytes(
                b"BEGIN_MESSAGE_MAP(CDiagDlg, CDialog)\n"
                b"    ON_WM_ERASEBKGND()\n"
                b"    MY_CUSTOM_MACRO(ID_FOO, OnFoo)\n"
                b"    ON_COMMAND(ID_BAD_ONLY_ONE_ARG)\n"
                b"    ON_WM_PAINT()\n"
                b"END_MESSAGE_MAP()\n"
            )
            spec = _make_spec("DiagDlg.cpp")
            entities = (EntityRecord("usr:CDiagDlg::OnPaint", EntityKind.METHOD, "OnPaint", "CDiagDlg::OnPaint"),)

            # 1) Clean TU stays PARSED_CLEAN despite unsupported/warning MFC macros
            clean_out = augment_payload(_make_payload(spec, entities=entities, parse_status=ParseStatus.PARSED_CLEAN), spec, root)
            self.assertEqual(clean_out.parse_status, ParseStatus.PARSED_CLEAN)
            self.assertEqual(len(clean_out.diagnostics), 3)
            severities = {d.code: d.severity for d in clean_out.diagnostics}
            self.assertEqual(severities["mfc.unsupported_macro"], DiagnosticSeverity.UNSUPPORTED)
            self.assertEqual(severities["mfc.unknown_map_macro"], DiagnosticSeverity.UNSUPPORTED)
            self.assertEqual(severities["mfc.malformed_invocation"], DiagnosticSeverity.WARNING)

            # 2) TU with errors keeps PARSED_WITH_ERRORS (never upgraded to PARSED_CLEAN)
            err_out = augment_payload(
                _make_payload(spec, entities=entities, parse_status=ParseStatus.PARSED_WITH_ERRORS), spec, root
            )
            self.assertEqual(err_out.parse_status, ParseStatus.PARSED_WITH_ERRORS)

    def test_05_resources_in_consumed_files_only_and_containment_and_idempotence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "repo"
            root.mkdir()
            outside_file = base / "outside.cpp"
            outside_file.write_bytes(
                b"BEGIN_MESSAGE_MAP(COutside, CDialog)\nON_WM_CLOSE()\nEND_MESSAGE_MAP()\n"
            )
            # Unreferenced resource file in repo (must NOT be scanned)
            (root / "unreferenced_resource.h").write_bytes(b"#define ID_UNREFERENCED 9999\n")

            res_h = root / "resource.h"
            res_h.write_bytes(
                b"#define IDD_ORDER_DLG 102\r\n"
                b"#define IDC_SAVE_BTN (IDD_ORDER_DLG + 8)\r\n"
                b"#define IDC_EXT_UNRESOLVED (UNKNOWN_BASE + 1)\r\n"
            )
            app_rc = root / "App.rc"
            app_rc.write_bytes(
                b'IDD_ORDER_DLG DIALOGEX 0, 0, 200, 100\n'
                b"BEGIN\n"
                b'    DEFPUSHBUTTON "&Save", IDC_SAVE_BTN, 10, 10, 50, 14\n'
                b"END\n"
            )
            main_cpp = root / "OrderDlg.cpp"
            # Use CP1252 bytes in comments to verify legacy encoding detection + exact spans
            main_cpp.write_bytes(
                b"// Legacy CP1252 comment: \x93Smart Quotes\x94\r\n"
                b"BEGIN_MESSAGE_MAP(COrderDlg, CDialog)\r\n"
                b"    ON_BN_CLICKED(IDC_SAVE_BTN, &COrderDlg::OnSave)\r\n"
                b"END_MESSAGE_MAP()\r\n"
            )

            spec = _make_spec("OrderDlg.cpp")
            entities = (EntityRecord("usr:COrderDlg::OnSave", EntityKind.METHOD, "OnSave", "COrderDlg::OnSave"),)
            # Include duplicate shared header entries + outside path + missing file
            deps = (
                TUDependencyRecord(repo_path="resource.h", dependency_kind=DependencyKind.DIRECT_INCLUDE),
                TUDependencyRecord(repo_path="./resource.h", dependency_kind=DependencyKind.TRANSITIVE_INCLUDE),
                TUDependencyRecord(repo_path="App.rc", dependency_kind=DependencyKind.RESOURCE_INCLUDE),
                TUDependencyRecord(repo_path="../outside.cpp", dependency_kind=DependencyKind.DIRECT_INCLUDE),
                TUDependencyRecord(repo_path="nonexistent.h", dependency_kind=DependencyKind.DIRECT_INCLUDE, is_missing=True),
            )
            payload = _make_payload(spec, entities=entities, dependencies=deps)

            aug1 = augment_payload(payload, spec, root)
            aug2 = augment_payload(aug1, spec, root)
            # Idempotence check
            self.assertEqual(aug1, aug2)

            res_entities = {e.name: e for e in aug1.entities if e.kind == EntityKind.RESOURCE}
            self.assertIn("IDD_ORDER_DLG", res_entities)
            self.assertIn("IDC_SAVE_BTN", res_entities)
            self.assertIn("IDC_EXT_UNRESOLVED", res_entities)
            self.assertNotIn("ID_UNREFERENCED", res_entities)

            # Verify no outside.cpp entities/observations were ingested
            self.assertFalse(any(o.span.repo_path.endswith("outside.cpp") for o in aug1.observations))

            # Verify resource observations & cross-file constant propagation from resource.h -> App.rc
            res_obs = [o for o in aug1.observations if o.observation_type.startswith("resource_")]
            define_save = next(o for o in res_obs if o.observation_type == "resource_define" and o.details["symbol"] == "IDC_SAVE_BTN")
            self.assertEqual(define_save.details["value"], 110)
            ctrl_save = next(o for o in res_obs if o.observation_type == "resource_control" and o.details["resource_id"] == "IDC_SAVE_BTN")
            self.assertEqual(ctrl_save.details["value"], 110)
            unresolved_def = next(o for o in res_obs if o.observation_type == "resource_define" and o.details["symbol"] == "IDC_EXT_UNRESOLVED")
            self.assertIsNone(unresolved_def.details["value"])
            self.assertEqual(unresolved_def.details["resolution_status"], "unresolved")

            # Verify resource occurrences have exact spans and no runtime call relations were invented
            res_occs = [oc for oc in aug1.occurrences if oc.occurrence_kind == OccurrenceKind.RESOURCE_DEFINITION]
            self.assertEqual(len(res_occs), 5)  # 3 defines + 1 dialog + 1 control
            self.assertFalse(any(r.kind == RelationKind.CALLS for r in aug1.relations))

    def test_06_build_context_scoped_event_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Dlg.cpp").write_bytes(
                b"BEGIN_MESSAGE_MAP(CDlg, CDialog)\n"
                b"    ON_NOTIFY(LVN_ITEMCHANGED, IDC_LIST, &CDlg::OnListChanged)\n"
                b"END_MESSAGE_MAP()\n"
            )
            spec_dbg = _make_spec("Dlg.cpp", config="Debug")
            spec_rel = _make_spec("Dlg.cpp", config="Release")
            ent = (EntityRecord("usr:CDlg::OnListChanged", EntityKind.METHOD, "OnListChanged", "CDlg::OnListChanged"),)

            out_dbg1 = augment_payload(_make_payload(spec_dbg, entities=ent), spec_dbg, root)
            out_dbg2 = augment_payload(_make_payload(spec_dbg, entities=ent), spec_dbg, root)
            out_rel = augment_payload(_make_payload(spec_rel, entities=ent), spec_rel, root)

            ev_dbg1 = next(e for e in out_dbg1.entities if e.kind == EntityKind.EVENT)
            ev_dbg2 = next(e for e in out_dbg2.entities if e.kind == EntityKind.EVENT)
            ev_rel = next(e for e in out_rel.entities if e.kind == EntityKind.EVENT)

            self.assertEqual(ev_dbg1.semantic_key, ev_dbg2.semantic_key)
            self.assertNotEqual(ev_dbg1.semantic_key, ev_rel.semantic_key)
            self.assertIn("LVN_ITEMCHANGED", ev_dbg1.qualified_name)
            self.assertIn("IDC_LIST", ev_dbg1.qualified_name)


if __name__ == "__main__":
    unittest.main()
