"""Integration tests for LegacyGraph ContextExporter (Markdown/JSON, budget, freshness, privacy)."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from legacygraph.context import BYTES_PER_TOKEN_CONSERVATIVE, ContextExporter
from legacygraph.discovery import inspect_source_file
from legacygraph.index import IndexStore
from legacygraph.models import (
    BuildContextSpec,
    CandidateTargetRecord,
    DispatchKind,
    EntityKind,
    EntityRecord,
    ExtractionQuality,
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
from legacygraph.queries import ContextNotFoundError, QueryService


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


class ContextExporterTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo_root = self.root / "repo"
        (self.repo_root / "src").mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "out" / "index.db"

        self.hdr_text = (
            "#pragma once\n"
            "class CInvoiceDlg {\n"
            "public:\n"
            "    void OnApprove();\n"
            "    virtual void AuditAction();\n"
            "};\n"
        )
        # Include triple backticks in a comment to verify safe Markdown code fence escaping
        self.src_text = (
            '#include "invoice_dlg.h"\n'
            "void CInvoiceDlg::OnApprove() {\n"
            "    // Comment with ```embedded fence``` inside source\n"
            "    AuditAction();\n"
            "    WriteLedger();\n"
            "    pHook->Notify();\n"
            "}\n"
            "void CInvoiceDlg::AuditAction() {\n"
            "    RecordAuditLine();\n"
            "}\n"
            "void WriteLedger() {\n"
            "    FlushDisk();\n"
            "}\n"
            "void FlushDisk() {\n"
            "    // disk sync\n"
            "}\n"
            "void RecordAuditLine() {\n"
            "    // audit log\n"
            "}\n"
        )
        self.hdr_path = self.repo_root / "src" / "invoice_dlg.h"
        self.src_path = self.repo_root / "src" / "invoice_dlg.cpp"
        self.hdr_path.write_text(self.hdr_text, encoding="utf-8")
        self.src_path.write_text(self.src_text, encoding="utf-8")

        self.store = IndexStore(self.db_path)
        self.store.initialize_schema(self.repo_root, source_revision="git-abc999")
        self.store.sync_file_inventory(
            [
                inspect_source_file(self.repo_root, "src/invoice_dlg.h"),
                inspect_source_file(self.repo_root, "src/invoice_dlg.cpp"),
            ]
        )

        # Include host absolute paths in context to test source privacy sanitization
        self.ctx = BuildContextSpec(
            project_name="Billing",
            target_name="BillingUI",
            configuration="Debug",
            platform="x64",
            working_directory=str(self.repo_root),
            include_dirs=("src", "C:/Program Files/VendorSDK/include"),
            system_include_dirs=("C:/Program Files (x86)/Windows Kits/10/Include",),
            defines=("_DEBUG", "_AFXDLL"),
            pch_header="C:/SecretHostPath/stdafx.h",
            mfc_mode="shared",
        )
        self.ctx_key = self.ctx.context_key
        self.tu = TranslationUnitSpec(
            main_file_path="src/invoice_dlg.cpp",
            build_context=self.ctx,
        )
        self.store.register_translation_units([self.tu])

        gen_id = self.store.begin_generation(
            parser_version="1.0.0", mfc_rule_version="1.0.0"
        )
        self._populate_tu(gen_id)
        self.store.publish_generation(gen_id)
        self.gen_id = gen_id

        self.service = QueryService(self.store)
        self.exporter = ContextExporter(self.service)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def _populate_tu(self, gen_id: int) -> None:
        h_rel = "src/invoice_dlg.h"
        c_rel = "src/invoice_dlg.cpp"

        entities = (
            EntityRecord("usr::CInvoiceDlg", EntityKind.CLASS, "CInvoiceDlg", "CInvoiceDlg"),
            EntityRecord(
                "usr::CInvoiceDlg::OnApprove",
                EntityKind.METHOD,
                "OnApprove",
                "CInvoiceDlg::OnApprove",
                parent_semantic_key="usr::CInvoiceDlg",
            ),
            EntityRecord(
                "usr::CInvoiceDlg::AuditAction",
                EntityKind.METHOD,
                "AuditAction",
                "CInvoiceDlg::AuditAction",
                parent_semantic_key="usr::CInvoiceDlg",
            ),
            EntityRecord("usr::WriteLedger", EntityKind.FUNCTION, "WriteLedger", "WriteLedger"),
            EntityRecord("usr::FlushDisk", EntityKind.FUNCTION, "FlushDisk", "FlushDisk"),
            EntityRecord(
                "usr::RecordAuditLine",
                EntityKind.FUNCTION,
                "RecordAuditLine",
                "RecordAuditLine",
            ),
        )

        occurrences = (
            SymbolOccurrenceRecord(
                "usr::CInvoiceDlg",
                OccurrenceKind.DEFINITION,
                _span_for_lines(h_rel, self.hdr_text, 2, 6),
                signature="class CInvoiceDlg",
            ),
            SymbolOccurrenceRecord(
                "usr::CInvoiceDlg::OnApprove",
                OccurrenceKind.DEFINITION,
                _span_for_lines(c_rel, self.src_text, 2, 7),
                signature="void CInvoiceDlg::OnApprove()",
                parent_semantic_key="usr::CInvoiceDlg",
            ),
            SymbolOccurrenceRecord(
                "usr::CInvoiceDlg::AuditAction",
                OccurrenceKind.DEFINITION,
                _span_for_lines(c_rel, self.src_text, 8, 10),
                signature="void CInvoiceDlg::AuditAction()",
                parent_semantic_key="usr::CInvoiceDlg",
            ),
            SymbolOccurrenceRecord(
                "usr::WriteLedger",
                OccurrenceKind.DEFINITION,
                _span_for_lines(c_rel, self.src_text, 11, 13),
                signature="void WriteLedger()",
            ),
            SymbolOccurrenceRecord(
                "usr::FlushDisk",
                OccurrenceKind.DEFINITION,
                _span_for_lines(c_rel, self.src_text, 14, 16),
                signature="void FlushDisk()",
            ),
            SymbolOccurrenceRecord(
                "usr::RecordAuditLine",
                OccurrenceKind.DEFINITION,
                _span_for_lines(c_rel, self.src_text, 17, 19),
                signature="void RecordAuditLine()",
            ),
        )

        obs_audit = ObservationRecord(
            "obs::audit",
            "call_expr",
            _span_for_lines(c_rel, self.src_text, 4, 4),
            subject_semantic_key="usr::CInvoiceDlg::OnApprove",
        )
        obs_ledger = ObservationRecord(
            "obs::ledger",
            "call_expr",
            _span_for_lines(c_rel, self.src_text, 5, 5),
            subject_semantic_key="usr::CInvoiceDlg::OnApprove",
        )
        obs_hook = ObservationRecord(
            "obs::hook",
            "call_expr",
            _span_for_lines(c_rel, self.src_text, 6, 6),
            subject_semantic_key="usr::CInvoiceDlg::OnApprove",
        )
        obs_flush = ObservationRecord(
            "obs::flush",
            "call_expr",
            _span_for_lines(c_rel, self.src_text, 12, 12),
            subject_semantic_key="usr::WriteLedger",
        )

        relations = (
            RelationRecord(
                source_semantic_key="usr::CInvoiceDlg::OnApprove",
                target_semantic_key="usr::CInvoiceDlg::AuditAction",
                kind=RelationKind.CALLS,
                resolution_status=ResolutionStatus.RESOLVED,
                dispatch_kind=DispatchKind.VIRTUAL,
                extraction_quality=ExtractionQuality.SEMANTIC,
                reason=ResolutionReason.STATIC_VIRTUAL_TARGET,
                evidence=(RelationEvidenceRecord("obs::audit", obs_audit.span, rule_id="clang.call"),),
            ),
            RelationRecord(
                source_semantic_key="usr::CInvoiceDlg::OnApprove",
                target_semantic_key="usr::WriteLedger",
                kind=RelationKind.CALLS,
                resolution_status=ResolutionStatus.RESOLVED,
                dispatch_kind=DispatchKind.DIRECT,
                extraction_quality=ExtractionQuality.SEMANTIC,
                reason=ResolutionReason.EXACT_SEMANTIC,
                evidence=(RelationEvidenceRecord("obs::ledger", obs_ledger.span, rule_id="clang.call"),),
            ),
            RelationRecord(
                source_semantic_key="usr::CInvoiceDlg::OnApprove",
                target_semantic_key=None,
                kind=RelationKind.CALLS,
                resolution_status=ResolutionStatus.UNRESOLVED,
                dispatch_kind=DispatchKind.INDIRECT,
                extraction_quality=ExtractionQuality.SEMANTIC,
                reason=ResolutionReason.UNKNOWN_RECEIVER,
                unresolved_target_expr="pHook->Notify()",
                evidence=(RelationEvidenceRecord("obs::hook", obs_hook.span, rule_id="clang.call"),),
                candidates=(
                    CandidateTargetRecord("usr::RecordAuditLine", "candidate_hook", "obs::hook"),
                ),
            ),
            RelationRecord(
                source_semantic_key="usr::WriteLedger",
                target_semantic_key="usr::FlushDisk",
                kind=RelationKind.CALLS,
                resolution_status=ResolutionStatus.RESOLVED,
                dispatch_kind=DispatchKind.DIRECT,
                extraction_quality=ExtractionQuality.SEMANTIC,
                reason=ResolutionReason.EXACT_SEMANTIC,
                evidence=(RelationEvidenceRecord("obs::flush", obs_flush.span, rule_id="clang.call"),),
            ),
        )

        self.store.record_parse_run(
            gen_id,
            ParseRunPayload(
                tu_key=self.tu.tu_key,
                parser_name="clang",
                parser_version="1.0.0",
                mfc_rule_version="1.0.0",
                parse_status=ParseStatus.PARSED_CLEAN,
                entities=entities,
                occurrences=occurrences,
                observations=(obs_audit, obs_ledger, obs_hook, obs_flush),
                relations=relations,
            ),
        )

    def test_export_markdown_and_json_with_target_path_and_provenance(self) -> None:
        md_res = self.exporter.export(
            seeds=["usr::CInvoiceDlg::OnApprove"],
            context_key=self.ctx_key,
            format="markdown",
            depth=2,
            target="usr::FlushDisk",
            token_budget=8000,
        )
        self.assertEqual(md_res["status"], "ok")
        self.assertFalse(md_res["budget_insufficient"])
        self.assertIn("void CInvoiceDlg::OnApprove()", md_res["content"])
        self.assertIn("src/invoice_dlg.cpp:2-7", md_res["content"])
        self.assertIn("pHook->Notify()", md_res["content"])
        self.assertIn("static_virtual_target", md_res["content"])
        self.assertIn("usr::CInvoiceDlg::OnApprove -> usr::WriteLedger -> usr::FlushDisk", md_res["content"])
        # Embedded ``` in comment causes safe 4-backtick fence
        self.assertIn("````cpp", md_res["content"])

        meta = md_res["metadata"]
        self.assertEqual(meta["generation"]["generation_id"], self.gen_id)
        self.assertEqual(meta["generation"]["source_revision"], "git-abc999")
        self.assertFalse(meta["exact_token_budget_guaranteed"])
        self.assertEqual(meta["byte_size"], len(md_res["content"].encode("utf-8")))
        self.assertLessEqual(meta["estimated_tokens"], 8000)

        # JSON export
        json_res = self.exporter.export(
            seeds=["usr::CInvoiceDlg::OnApprove"],
            context_key=self.ctx_key,
            format="json",
            depth=2,
            target="usr::FlushDisk",
            token_budget=8000,
        )
        self.assertEqual(json_res["status"], "ok")
        parsed = json.loads(json_res["content"])
        self.assertEqual(parsed["status"], "ok")
        self.assertFalse(parsed["budget_insufficient"])
        self.assertTrue(parsed["path"]["found"])
        self.assertEqual(
            parsed["path"]["path"],
            ["usr::CInvoiceDlg::OnApprove", "usr::WriteLedger", "usr::FlushDisk"],
        )
        self.assertEqual(len(parsed["unresolved"]), 1)
        self.assertEqual(parsed["unresolved"][0]["unresolved_target_expr"], "pHook->Notify()")
        self.assertTrue(any(f["repo_path"] == "src/invoice_dlg.cpp" and f[" indexed_content_sha256" if False else "indexed_content_sha256"] for f in parsed["metadata"]["files"]))

    def test_budget_enforcement_including_serialization_overhead(self) -> None:
        full_res = self.exporter.export(
            seeds=["usr::CInvoiceDlg::OnApprove"],
            context_key=self.ctx_key,
            format="json",
            depth=2,
            token_budget=8000,
        )
        full_parsed = json.loads(full_res["content"])
        self.assertGreater(len(full_parsed["snippets"]), 1)

        # Measure full depth=2 pack and pick a tight budget that forces optional neighbor pruning
        # while still fitting mandatory seed snippet + parent declaration + metadata overhead.
        tight_budget = full_res["metadata"]["estimated_tokens"] - 40
        tight_res = self.exporter.export(
            seeds=["usr::CInvoiceDlg::OnApprove"],
            context_key=self.ctx_key,
            format="json",
            depth=2,
            token_budget=tight_budget,
        )
        self.assertEqual(tight_res["status"], "ok")
        self.assertFalse(tight_res["budget_insufficient"])
        raw_bytes_len = len(tight_res["content"].encode("utf-8"))
        self.assertEqual(raw_bytes_len, tight_res["metadata"]["byte_size"])
        self.assertLessEqual(raw_bytes_len, tight_budget * BYTES_PER_TOKEN_CONSERVATIVE)
        self.assertLessEqual(tight_res["metadata"]["estimated_tokens"], tight_budget)
        self.assertTrue(tight_res["metadata"]["truncated"])
        self.assertIn("token_budget", tight_res["metadata"]["truncation_reasons"])
        self.assertTrue(
            any(o["reason"] == "token_budget" for o in tight_res["metadata"]["omissions"])
        )
        parsed = json.loads(tight_res["content"])
        self.assertLess(len(parsed["snippets"]), len(full_parsed["snippets"]))
        self.assertEqual(parsed["snippets"][0]["semantic_key"], "usr::CInvoiceDlg::OnApprove")

    def test_root_seed_too_large_budget_insufficient_and_valid_json(self) -> None:
        # Add a huge seed function that cannot fit in a small token budget
        big_lines = ["void HugeSeedFunction() {"] + [
            f"    int local_var_{i:03d} = {i} * {i + 1}; // line padding for budget test"
            for i in range(120)
        ] + ["}"]
        big_text = "\n".join(big_lines) + "\n"
        big_path = self.repo_root / "src" / "huge_seed.cpp"
        big_path.write_text(big_text, encoding="utf-8")

        self.store.sync_file_inventory(
            [
                inspect_source_file(self.repo_root, "src/invoice_dlg.h"),
                inspect_source_file(self.repo_root, "src/invoice_dlg.cpp"),
                inspect_source_file(self.repo_root, "src/huge_seed.cpp"),
            ]
        )
        big_tu = TranslationUnitSpec(
            main_file_path="src/huge_seed.cpp", build_context=self.ctx
        )
        self.store.register_translation_units([big_tu])
        gen2 = self.store.begin_generation(parser_version="1.0.0", mfc_rule_version="1.0.0")
        self.store.record_parse_run(
            gen2,
            ParseRunPayload(
                tu_key=big_tu.tu_key,
                parser_name="clang",
                parser_version="1.0.0",
                mfc_rule_version="1.0.0",
                parse_status=ParseStatus.PARSED_CLEAN,
                entities=(
                    EntityRecord(
                        "usr::HugeSeedFunction",
                        EntityKind.FUNCTION,
                        "HugeSeedFunction",
                        "HugeSeedFunction",
                    ),
                ),
                occurrences=(
                    SymbolOccurrenceRecord(
                        "usr::HugeSeedFunction",
                        OccurrenceKind.DEFINITION,
                        _span_for_lines("src/huge_seed.cpp", big_text, 1, len(big_lines)),
                        signature="void HugeSeedFunction()",
                    ),
                ),
            ),
        )
        self.store.publish_generation(gen2)

        # 1. JSON with 400 token budget (cannot fit 120-line mandatory seed source)
        res_json = self.exporter.export(
            seeds=["usr::HugeSeedFunction"],
            context_key=self.ctx_key,
            format="json",
            token_budget=400,
        )
        self.assertEqual(res_json["status"], "budget_insufficient")
        self.assertTrue(res_json["budget_insufficient"])
        self.assertTrue(res_json["metadata"]["budget_insufficient"])
        self.assertTrue(
            any(
                o["reason"] == "budget_insufficient" and o["mandatory"]
                for o in res_json["metadata"]["omissions"]
            )
        )
        parsed = json.loads(res_json["content"])
        self.assertEqual(parsed["status"], "budget_insufficient")
        self.assertTrue(parsed["budget_insufficient"])

        # 2. Ultra-tiny budget (e.g. 50 tokens): JSON must STILL be valid JSON
        res_tiny = self.exporter.export(
            seeds=["usr::HugeSeedFunction"],
            context_key=self.ctx_key,
            format="json",
            token_budget=50,
        )
        self.assertEqual(res_tiny["status"], "budget_insufficient")
        self.assertTrue(res_tiny["budget_insufficient"])
        parsed_tiny = json.loads(res_tiny["content"])
        self.assertEqual(parsed_tiny["status"], "budget_insufficient")
        self.assertTrue(parsed_tiny["budget_insufficient"])

        # 3. Markdown budget_insufficient
        res_md = self.exporter.export(
            seeds=["usr::HugeSeedFunction"],
            context_key=self.ctx_key,
            format="markdown",
            token_budget=200,
        )
        self.assertEqual(res_md["status"], "budget_insufficient")
        self.assertTrue(res_md["budget_insufficient"])
        self.assertIn("budget_insufficient", res_md["content"])

    def test_stale_omissions_and_source_privacy(self) -> None:
        # 1. Source privacy: verify no host paths leak into exported content or metadata
        res = self.exporter.export(
            seeds=["usr::CInvoiceDlg::OnApprove"],
            context_key=self.ctx_key,
            format="json",
            token_budget=8000,
        )
        self.assertNotIn(str(self.repo_root), res["content"])
        self.assertNotIn("VendorSDK", res["content"])
        self.assertNotIn("Windows Kits", res["content"])
        self.assertNotIn("SecretHostPath", res["content"])
        self.assertEqual(res["metadata"]["context"]["include_dirs"], ["src"])
        self.assertEqual(res["metadata"]["context"]["working_directory"], ".")
        self.assertEqual(res["metadata"]["context"]["pch_header"], "")

        # 2. Modify source file on disk -> stale source body must be refused and recorded in omissions
        self.src_path.write_text(
            self.src_text + "\n// TAMPERED_UNINDEXED_SECRET_CODE\n", encoding="utf-8"
        )
        stale_res = self.exporter.export(
            seeds=["usr::CInvoiceDlg::OnApprove"],
            context_key=self.ctx_key,
            format="markdown",
            token_budget=8000,
        )
        self.assertNotIn("TAMPERED_UNINDEXED_SECRET_CODE", stale_res["content"])
        self.assertNotIn("pHook->Notify();\n}", stale_res["content"])
        self.assertTrue(
            any(
                o["item"] == "usr::CInvoiceDlg::OnApprove" and o["reason"] == "stale_source"
                for o in stale_res["metadata"]["omissions"]
            )
        )
        self.assertTrue(
            any(
                f["repo_path"] == "src/invoice_dlg.cpp" and f["status"] == "stale"
                for f in stale_res["metadata"]["files"]
            )
        )

    def test_invalid_export_arguments(self) -> None:
        with self.assertRaises(ValueError):
            self.exporter.export([], self.ctx_key)
        with self.assertRaises(ValueError):
            self.exporter.export("usr::CInvoiceDlg::OnApprove", self.ctx_key)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            self.exporter.export(["usr::CInvoiceDlg::OnApprove"], self.ctx_key, format="xml")
        with self.assertRaises(ValueError):
            self.exporter.export(["usr::CInvoiceDlg::OnApprove"], self.ctx_key, token_budget=0)
        with self.assertRaises(ContextNotFoundError):
            self.exporter.export(["usr::CInvoiceDlg::OnApprove"], "unknown:ctx")

    def test_markdown_fence_escaping_with_triple_backticks_in_source(self) -> None:
        fenced_text = (
            "void BacktickFn() {\n"
            "    // Comment containing ```cpp and ``` inside source\n"
            "    int x = 1;\n"
            "}\n"
        )
        fenced_path = self.repo_root / "src" / "fenced.cpp"
        fenced_path.write_text(fenced_text, encoding="utf-8")
        self.store.sync_file_inventory(
            [
                inspect_source_file(self.repo_root, "src/invoice_dlg.h"),
                inspect_source_file(self.repo_root, "src/invoice_dlg.cpp"),
                inspect_source_file(self.repo_root, "src/fenced.cpp"),
            ]
        )
        fenced_tu = TranslationUnitSpec(
            main_file_path="src/fenced.cpp", build_context=self.ctx
        )
        self.store.register_translation_units([fenced_tu])
        gen2 = self.store.begin_generation(parser_version="1.0.0", mfc_rule_version="1.0.0")
        self.store.record_parse_run(
            gen2,
            ParseRunPayload(
                tu_key=fenced_tu.tu_key,
                parser_name="clang",
                parser_version="1.0.0",
                mfc_rule_version="1.0.0",
                parse_status=ParseStatus.PARSED_CLEAN,
                entities=(
                    EntityRecord(
                        "usr::BacktickFn",
                        EntityKind.FUNCTION,
                        "BacktickFn",
                        "BacktickFn",
                    ),
                ),
                occurrences=(
                    SymbolOccurrenceRecord(
                        "usr::BacktickFn",
                        OccurrenceKind.DEFINITION,
                        _span_for_lines("src/fenced.cpp", fenced_text, 1, 4),
                        signature="void BacktickFn()",
                    ),
                ),
            ),
        )
        self.store.publish_generation(gen2)
        res = self.exporter.export(
            seeds=["usr::BacktickFn"],
            context_key=self.ctx_key,
            format="markdown",
            token_budget=8000,
        )
        self.assertEqual(res["status"], "ok")
        self.assertIn("````cpp", res["content"])


if __name__ == "__main__":
    unittest.main()
