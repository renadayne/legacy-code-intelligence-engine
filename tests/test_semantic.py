"""Integration and unit tests for Step 2: Semantic C++ parsing (`ClangParser`) and incremental orchestrator (`scan_repository`)."""

from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from legacygraph.build_context import load_build_manifest
from legacygraph.clang_parser import ClangParser, is_libclang_available
from legacygraph.cli import main as cli_main
from legacygraph.index import IndexStore
from legacygraph.models import (
    BuildContextSpec,
    DispatchKind,
    EntityKind,
    LinkageScope,
    ParseStatus,
    RelationKind,
    ResolutionReason,
    ResolutionStatus,
    TranslationUnitSpec,
)
from legacygraph.scanner import scan_repository

_LIBCLANG_OK, _LIBCLANG_REASON = is_libclang_available()


@unittest.skipUnless(_LIBCLANG_OK, f"libclang unavailable: {_LIBCLANG_REASON}")
class SemanticParsingAndScannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.repo_dir = self.root / "repo"
        self.repo_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "index.db"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_mfc_demo_cross_file_overload_virtual_and_indirect_calls(self) -> None:
        demo_src = Path(__file__).resolve().parent.parent / "examples" / "mfc_demo"
        shutil.copytree(demo_src, self.repo_dir, dirs_exist_ok=True)
        manifest_path = self.repo_dir / "manifest.json"

        # Run via CLI scan --json
        out = io.StringIO()
        with redirect_stdout(out):
            rc = cli_main(
                [
                    "scan",
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
        summary = json.loads(out.getvalue())
        self.assertFalse(summary["noop"])
        self.assertEqual(summary["parsed_translation_units"], 2)
        self.assertEqual(summary["clean_translation_units"], 2)
        self.assertEqual(summary["failed_translation_units"], 0)

        imported = load_build_manifest(self.repo_dir, manifest_path)
        ctx_key = imported.contexts[0].context_key

        with IndexStore(self.db_path) as store:
            entities = {
                e["qualified_name"] + "|" + e["semantic_key"]: e
                for e in store.query_published_entities(context_key=ctx_key, limit=200)
            }
            by_sem_key = {e["semantic_key"]: e for e in entities.values()}
            calls = store.query_published_relations(
                context_key=ctx_key, kind=RelationKind.CALLS, limit=200
            )

        # 1. Both Validate(int) and Validate(double) exist with distinct USR-based semantic keys
        validate_ents = [
            e for e in by_sem_key.values() if e["name"] == "Validate" and e["kind"] == "function"
        ]
        self.assertEqual(len(validate_ents), 2)
        validate_int_key = [
            e["semantic_key"] for e in validate_ents if "#I#" in e["semantic_key"]
        ][0]
        validate_double_key = [
            e["semantic_key"] for e in validate_ents if "#d#" in e["semantic_key"]
        ][0]
        self.assertNotEqual(validate_int_key, validate_double_key)

        # 2. Cross-file identity: COrderDlg::OnSave in demo.cpp calls SaveOrder(int), which in model.cpp calls Validate(int)
        save_order_ents = [e for e in by_sem_key.values() if e["name"] == "SaveOrder"]
        self.assertEqual(len(save_order_ents), 1)
        save_order_key = save_order_ents[0]["semantic_key"]
        on_save_key = [
            e["semantic_key"]
            for e in by_sem_key.values()
            if e["qualified_name"] == "COrderDlg::OnSave"
        ][0]

        onsave_calls = [c for c in calls if c["source_semantic_key"] == on_save_key]
        self.assertEqual(len(onsave_calls), 1)
        self.assertEqual(onsave_calls[0]["target_semantic_key"], save_order_key)
        self.assertEqual(onsave_calls[0]["resolution_status"], ResolutionStatus.RESOLVED.value)
        self.assertEqual(onsave_calls[0]["dispatch_kind"], DispatchKind.DIRECT.value)
        self.assertGreaterEqual(onsave_calls[0]["evidence_count"], 1)

        save_order_calls = [c for c in calls if c["source_semantic_key"] == save_order_key]
        self.assertEqual(len(save_order_calls), 1)
        self.assertEqual(save_order_calls[0]["target_semantic_key"], validate_int_key)
        self.assertNotEqual(save_order_calls[0]["target_semantic_key"], validate_double_key)

        # 3. Virtual call: CallVirtual -> OrderService::Commit recorded as virtual static target
        call_virtual_key = [
            e["semantic_key"] for e in by_sem_key.values() if e["name"] == "CallVirtual"
        ][0]
        commit_key = [
            e["semantic_key"]
            for e in by_sem_key.values()
            if e["qualified_name"] == "OrderService::Commit"
        ][0]
        virt_calls = [c for c in calls if c["source_semantic_key"] == call_virtual_key]
        self.assertEqual(len(virt_calls), 1)
        self.assertEqual(virt_calls[0]["target_semantic_key"], commit_key)
        self.assertEqual(virt_calls[0]["resolution_status"], ResolutionStatus.RESOLVED.value)
        self.assertEqual(virt_calls[0]["dispatch_kind"], DispatchKind.VIRTUAL.value)
        self.assertEqual(virt_calls[0]["reason"], ResolutionReason.STATIC_VIRTUAL_TARGET.value)

        # 4. Indirect function pointer call: CallIndirect -> callback(5) is UNRESOLVED + INDIRECT
        call_indirect_key = [
            e["semantic_key"] for e in by_sem_key.values() if e["name"] == "CallIndirect"
        ][0]
        ind_calls = [c for c in calls if c["source_semantic_key"] == call_indirect_key]
        self.assertEqual(len(ind_calls), 1)
        self.assertIsNone(ind_calls[0]["target_semantic_key"])
        self.assertEqual(ind_calls[0]["resolution_status"], ResolutionStatus.UNRESOLVED.value)
        self.assertEqual(ind_calls[0]["dispatch_kind"], DispatchKind.INDIRECT.value)
        self.assertEqual(ind_calls[0]["reason"], ResolutionReason.INDIRECT_FUNCTION_POINTER.value)
        self.assertEqual(ind_calls[0]["unresolved_target_expr"], "callback(5)")

        # 5. Active macro offsets exposed on ClangParser (active macros in demo.cpp, excluding #if 0)
        parser = ClangParser(self.repo_dir)
        demo_tu = [t for t in imported.translation_units if t.main_file_path == "demo.cpp"][0]
        parser.parse(demo_tu)
        demo_bytes = (self.repo_dir / "demo.cpp").read_bytes()
        if0_offset = demo_bytes.find(b"#if 0")
        active_offsets = parser.last_macro_offsets.get("demo.cpp", set())
        self.assertTrue(len(active_offsets) >= 4)
        self.assertTrue(all(off < if0_offset for off in active_offsets))

    def test_conditional_contexts_internal_linkage_qualified_virtual_and_lambdas(self) -> None:
        (self.repo_dir / "shared.h").write_text(
            "#pragma once\n"
            "struct Base {\n"
            "    virtual int Compute(int x) { return x; }\n"
            "};\n"
            "struct Derived : Base {\n"
            "    int Compute(int x) override { return Base::Compute(x) + 1; }\n"
            "};\n",
            encoding="utf-8",
        )
        (self.repo_dir / "unit_a.cpp").write_text(
            '#include "shared.h"\n'
            "static int LocalHelper() { return 10; }\n"
            "int DebugTarget() { return 1; }\n"
            "int ReleaseTarget() { return 2; }\n"
            "int EntryA(Derived* d) {\n"
            "    auto lam = [&]() { return LocalHelper(); };\n"
            "#ifdef _DEBUG\n"
            "    int v = DebugTarget();\n"
            "#else\n"
            "    int v = ReleaseTarget();\n"
            "#endif\n"
            "    return v + lam() + d->Compute(3);\n"
            "}\n",
            encoding="utf-8",
        )
        (self.repo_dir / "unit_b.cpp").write_text(
            '#include "shared.h"\n'
            "static int LocalHelper() { return 20; }\n"
            "int RunB() { return LocalHelper(); }\n",
            encoding="utf-8",
        )

        manifest_path = self.repo_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "contexts": [
                        {
                            "id": "dbg",
                            "project_name": "App",
                            "target_name": "App",
                            "configuration": "Debug",
                            "platform": "x64",
                            "include_dirs": ["."],
                            "defines": ["_DEBUG"],
                        },
                        {
                            "id": "rel",
                            "project_name": "App",
                            "target_name": "App",
                            "configuration": "Release",
                            "platform": "x64",
                            "include_dirs": ["."],
                            "defines": ["NDEBUG"],
                        },
                    ],
                    "translation_units": [
                        {"file": "unit_a.cpp", "context_id": "dbg"},
                        {"file": "unit_b.cpp", "context_id": "dbg"},
                        {"file": "unit_a.cpp", "context_id": "rel"},
                    ],
                }
            ),
            encoding="utf-8",
        )

        scan_repository(self.repo_dir, self.db_path, manifest_path)
        imported = load_build_manifest(self.repo_dir, manifest_path)
        ctx_dbg = [c for c in imported.contexts if c.configuration == "Debug"][0]
        ctx_rel = [c for c in imported.contexts if c.configuration == "Release"][0]

        with IndexStore(self.db_path) as store:
            dbg_ents = store.query_published_entities(context_key=ctx_dbg.context_key, limit=200)
            dbg_calls = store.query_published_relations(
                context_key=ctx_dbg.context_key, kind=RelationKind.CALLS, limit=200
            )
            rel_calls = store.query_published_relations(
                context_key=ctx_rel.context_key, kind=RelationKind.CALLS, limit=200
            )
            inherits = store.query_published_relations(
                context_key=ctx_dbg.context_key, kind=RelationKind.INHERITS, limit=50
            )

        # Duplicate static functions in unit_a.cpp and unit_b.cpp must have distinct INTERNAL_TU keys
        helpers = [e for e in dbg_ents if e["name"] == "LocalHelper"]
        self.assertEqual(len(helpers), 2)
        self.assertEqual({h["linkage_scope"] for h in helpers}, {LinkageScope.INTERNAL_TU.value})
        self.assertEqual({h["scope_qualifier"] for h in helpers}, {"unit_a.cpp", "unit_b.cpp"})
        self.assertNotEqual(helpers[0]["semantic_key"], helpers[1]["semantic_key"])

        # Lambda inside EntryA owns the call to unit_a.cpp's LocalHelper, while EntryA calls operator()
        helper_a_key = [h["semantic_key"] for h in helpers if h["scope_qualifier"] == "unit_a.cpp"][0]
        entry_a_key = [e["semantic_key"] for e in dbg_ents if e["name"] == "EntryA"][0]
        lam_calls = [c for c in dbg_calls if c["target_semantic_key"] == helper_a_key]
        self.assertEqual(len(lam_calls), 1)
        self.assertNotEqual(lam_calls[0]["source_semantic_key"], entry_a_key)

        # Conditional context isolation: Debug calls DebugTarget, Release calls ReleaseTarget
        dbg_target_key = [e["semantic_key"] for e in dbg_ents if e["name"] == "DebugTarget"][0]
        rel_target_key = [e["semantic_key"] for e in dbg_ents if e["name"] == "ReleaseTarget"][0]
        entry_a_dbg_targets = {
            c["target_semantic_key"] for c in dbg_calls if c["source_semantic_key"] == entry_a_key
        }
        entry_a_rel_targets = {
            c["target_semantic_key"] for c in rel_calls if c["source_semantic_key"] == entry_a_key
        }
        self.assertIn(dbg_target_key, entry_a_dbg_targets)
        self.assertNotIn(rel_target_key, entry_a_dbg_targets)
        self.assertIn(rel_target_key, entry_a_rel_targets)
        self.assertNotIn(dbg_target_key, entry_a_rel_targets)

        # Qualified non-virtual call Base::Compute(x) inside Derived::Compute is classified as DIRECT,
        # whereas d->Compute(3) inside EntryA is classified as VIRTUAL
        base_compute_key = [
            e["semantic_key"] for e in dbg_ents if e["qualified_name"] == "Base::Compute"
        ][0]
        derived_compute_key = [
            e["semantic_key"] for e in dbg_ents if e["qualified_name"] == "Derived::Compute"
        ][0]
        qualified_call = [
            c
            for c in dbg_calls
            if c["source_semantic_key"] == derived_compute_key
            and c["target_semantic_key"] == base_compute_key
        ][0]
        self.assertEqual(qualified_call["dispatch_kind"], DispatchKind.DIRECT.value)
        self.assertEqual(qualified_call["reason"], ResolutionReason.EXACT_SEMANTIC.value)

        unqualified_virt = [
            c
            for c in dbg_calls
            if c["source_semantic_key"] == entry_a_key
            and c["target_semantic_key"] == derived_compute_key
        ][0]
        self.assertEqual(unqualified_virt["dispatch_kind"], DispatchKind.VIRTUAL.value)
        self.assertEqual(unqualified_virt["reason"], ResolutionReason.STATIC_VIRTUAL_TARGET.value)

        # Inheritance relation Derived -> Base recorded across both TUs that include shared.h
        self.assertGreaterEqual(len(inherits), 1)
        derived_key = [e["semantic_key"] for e in dbg_ents if e["name"] == "Derived"][0]
        base_key = [e["semantic_key"] for e in dbg_ents if e["name"] == "Base"][0]
        self.assertEqual(
            {(r["source_semantic_key"], r["target_semantic_key"]) for r in inherits},
            {(derived_key, base_key)},
        )

    def test_incremental_noop_header_invalidation_manifest_removal_and_crash_rollback(self) -> None:
        # Include a non-default extension header (.inc) and a standard header (.h)
        (self.repo_dir / "config.inc").write_text("constexpr int kFactor = 2;\n", encoding="utf-8")
        (self.repo_dir / "calc.h").write_text(
            '#pragma once\n#include "config.inc"\ninline int Scale(int x) { return x * kFactor; }\n',
            encoding="utf-8",
        )
        (self.repo_dir / "first.cpp").write_text(
            '#include "calc.h"\nint FirstMain() { return Scale(3); }\n',
            encoding="utf-8",
        )
        (self.repo_dir / "second.cpp").write_text(
            "int SecondOnly() { return 99; }\n",
            encoding="utf-8",
        )
        manifest_path = self.repo_dir / "manifest.json"
        manifest_data = {
            "contexts": [
                {
                    "id": "ctx1",
                    "project_name": "IncrementalDemo",
                    "target_name": "IncrementalDemo",
                    "configuration": "Debug",
                    "platform": "x64",
                    "include_dirs": ["."],
                }
            ],
            "translation_units": [
                {"file": "first.cpp", "context_id": "ctx1"},
                {"file": "second.cpp", "context_id": "ctx1"},
            ],
        }
        manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")

        # 1. Initial scan
        res1 = scan_repository(self.repo_dir, self.db_path, manifest_path)
        self.assertFalse(res1["noop"])
        self.assertEqual(res1["parsed_translation_units"], 2)
        gen1 = res1["generation_id"]

        # 2. Second scan with no changes -> incremental no-op
        res2 = scan_repository(self.repo_dir, self.db_path, manifest_path)
        self.assertTrue(res2["noop"])
        self.assertEqual(res2["parsed_translation_units"], 0)
        self.assertEqual(res2["reused_translation_units"], 2)
        self.assertEqual(res2["generation_id"], gen1)

        # 3. Mutate transitive non-default-extension header `config.inc` -> only `first.cpp` invalidated
        (self.repo_dir / "config.inc").write_text("constexpr int kFactor = 5;\n", encoding="utf-8")
        res3 = scan_repository(self.repo_dir, self.db_path, manifest_path)
        self.assertFalse(res3["noop"])
        self.assertEqual(res3["parsed_translation_units"], 1)
        self.assertEqual(res3["reused_translation_units"], 1)
        self.assertEqual(res3["tu_results"][0]["main_file_path"], "first.cpp")
        gen3 = res3["generation_id"]
        self.assertNotEqual(gen3, gen1)

        # 4. Simulate unexpected crash before publish -> staging rolled back, gen3 preserved
        (self.repo_dir / "second.cpp").write_text(
            "int SecondOnly() { return 100; }\n", encoding="utf-8"
        )

        def boom(_store: IndexStore, _gen_id: int) -> None:
            raise RuntimeError("Simulated crash before publish")

        with self.assertRaises(RuntimeError):
            scan_repository(
                self.repo_dir, self.db_path, manifest_path, on_before_publish=boom
            )

        with IndexStore(self.db_path) as store:
            self.assertEqual(store.get_published_generation_id(), gen3)

        # 5. Remove `second.cpp` from manifest (file still on disk) and reset `second.cpp` to original ->
        # `SecondOnly` is retracted from published entities even without re-parsing `first.cpp`
        (self.repo_dir / "second.cpp").write_text(
            "int SecondOnly() { return 99; }\n", encoding="utf-8"
        )
        manifest_data["translation_units"] = [{"file": "first.cpp", "context_id": "ctx1"}]
        manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")
        res4 = scan_repository(self.repo_dir, self.db_path, manifest_path)
        self.assertEqual(len(res4["deactivated_translation_units"]), 1)
        self.assertEqual(res4["parsed_translation_units"], 0)

        with IndexStore(self.db_path) as store:
            ents = store.query_published_entities(limit=100)
            ent_names = {e["name"] for e in ents}
            self.assertIn("FirstMain", ent_names)
            self.assertNotIn("SecondOnly", ent_names)

        # 6. Flag change invalidates TU (`per_file_defines`)
        manifest_data["translation_units"] = [
            {"file": "first.cpp", "context_id": "ctx1", "per_file_defines": ["EXTRA_MODE=1"]}
        ]
        manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")
        res5 = scan_repository(self.repo_dir, self.db_path, manifest_path)
        self.assertFalse(res5["noop"])
        self.assertEqual(res5["parsed_translation_units"], 1)

        # 7. Header deletion invalidates dependent TU and retracts it when parse fails
        (self.repo_dir / "config.inc").unlink()
        res6 = scan_repository(self.repo_dir, self.db_path, manifest_path)
        self.assertEqual(res6["failed_translation_units"], 1)
        self.assertEqual(res6["published_generation"]["verified_tu_runs"], 0)

    def test_failed_tu_retraction_and_stale_recovery_and_flag_rejection(self) -> None:
        (self.repo_dir / "good_then_bad.cpp").write_text(
            "int TargetFn() { return 1; }\nint CallerFn() { return TargetFn(); }\n",
            encoding="utf-8",
        )
        manifest_path = self.repo_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "contexts": [
                        {
                            "id": "c1",
                            "project_name": "P",
                            "target_name": "T",
                            "configuration": "Debug",
                            "platform": "x64",
                        }
                    ],
                    "translation_units": [{"file": "good_then_bad.cpp", "context_id": "c1"}],
                }
            ),
            encoding="utf-8",
        )

        # 1. Clean initial scan publishes CallerFn -> TargetFn
        res1 = scan_repository(self.repo_dir, self.db_path, manifest_path)
        self.assertEqual(res1["clean_translation_units"], 1)

        # 2. Introduce missing include error -> TU becomes PARSED_WITH_ERRORS, diagnostics surfaced,
        # and prior verified facts for this broken TU are retracted so stale ranges aren't mixed with new source!
        (self.repo_dir / "good_then_bad.cpp").write_text(
            '#include "nonexistent_header_123.h"\n'
            "int TargetFn() { return 1; }\n"
            "int CallerFn() { return TargetFn(); }\n",
            encoding="utf-8",
        )
        res2 = scan_repository(self.repo_dir, self.db_path, manifest_path)
        self.assertEqual(res2["clean_translation_units"], 0)
        self.assertEqual(res2["failed_translation_units"], 1)
        self.assertGreaterEqual(len(res2["diagnostics"]), 1)
        self.assertEqual(
            res2["published_generation"]["verified_tu_runs"], 0
        )

        # 3. Provide the missing header -> TU automatically invalidates and recovers to PARSED_CLEAN!
        (self.repo_dir / "nonexistent_header_123.h").write_text(
            "#pragma once\n", encoding="utf-8"
        )
        res3 = scan_repository(self.repo_dir, self.db_path, manifest_path)
        self.assertEqual(res3["clean_translation_units"], 1)
        self.assertEqual(res3["failed_translation_units"], 0)
        self.assertEqual(res3["published_generation"]["verified_tu_runs"], 1)

        # 4. Reject response files or unsupported flags (/clr) explicitly rather than silently parsing wrong
        ctx_bad = BuildContextSpec(
            project_name="P",
            target_name="T",
            configuration="Debug",
            platform="x64",
            extra_flags=("/clr",),
        )
        tu_bad = TranslationUnitSpec(
            main_file_path="good_then_bad.cpp",
            build_context=ctx_bad,
            response_files=("build.rsp",),
        )
        parser = ClangParser(self.repo_dir)
        bad_payload = parser.parse(tu_bad)
        self.assertEqual(bad_payload.parse_status, ParseStatus.FAILED)
        codes = {d.code for d in bad_payload.diagnostics}
        self.assertIn("clang.unsupported_response_file", codes)
        self.assertIn("clang.unsupported_flag", codes)


if __name__ == "__main__":
    unittest.main()
