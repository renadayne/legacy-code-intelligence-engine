"""Unit tests for standalone MFC evidence and resource extractor (`legacygraph/mfc.py`)."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


def _load_mfc_module():
    try:
        from legacygraph import mfc as mod

        return mod
    except ImportError:
        module_path = Path(__file__).resolve().parents[1] / "legacygraph" / "mfc.py"
        spec = importlib.util.spec_from_file_location("legacygraph_mfc", module_path)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod


mfc = _load_mfc_module()
scan_mfc = mfc.scan_mfc
scan_resources = mfc.scan_resources


class TestScanMfc(unittest.TestCase):
    def test_01_basic_core_macros_and_explicit_wm_registry(self):
        source = (
            b"BEGIN_MESSAGE_MAP(COrderDlg, CDialogEx)\n"
            b"    ON_WM_CREATE()\n"
            b"    ON_WM_DESTROY()\n"
            b"    ON_WM_PAINT()\n"
            b"    ON_WM_SIZE()\n"
            b"    ON_WM_CLOSE()\n"
            b"    ON_WM_TIMER()\n"
            b"    ON_COMMAND(ID_FILE_SAVE, &COrderDlg::OnFileSave)\n"
            b"    ON_BN_CLICKED(IDC_BTN_SUBMIT, OnSubmitClicked)\n"
            b"    ON_MESSAGE(WM_USER_REFRESH, &COrderDlg::OnUserRefresh)\n"
            b"    ON_NOTIFY(LVN_ITEMCHANGED, IDC_ORDER_LIST, &COrderDlg::OnOrderListChanged)\n"
            b"    ON_UPDATE_COMMAND_UI(ID_FILE_SAVE, &COrderDlg::OnUpdateFileSave)\n"
            b"END_MESSAGE_MAP()\n"
        )
        obs = scan_mfc(source, "OrderDlg.cpp")
        self.assertEqual(len(obs), 11)
        self.assertTrue(all(o["kind"] == "event_mapping" for o in obs))
        self.assertTrue(all(o["condition_state"] == "active" for o in obs))

        # Verify WM registry items
        wm_expected = [
            ("ON_WM_CREATE", "WM_CREATE", "OnCreate"),
            ("ON_WM_DESTROY", "WM_DESTROY", "OnDestroy"),
            ("ON_WM_PAINT", "WM_PAINT", "OnPaint"),
            ("ON_WM_SIZE", "WM_SIZE", "OnSize"),
            ("ON_WM_CLOSE", "WM_CLOSE", "OnClose"),
            ("ON_WM_TIMER", "WM_TIMER", "OnTimer"),
        ]
        for idx, (macro, wm_id, handler) in enumerate(wm_expected):
            self.assertEqual(obs[idx]["macro"], macro)
            self.assertEqual(obs[idx]["event_kind"], "wm_message")
            self.assertEqual(obs[idx]["event_id"], wm_id)
            self.assertEqual(obs[idx]["handler_spelling"], handler)
            self.assertEqual(obs[idx]["qualified_handler"], f"COrderDlg::{handler}")
            self.assertEqual(source[obs[idx]["start_byte"] : obs[idx]["end_byte"]].decode(), f"{macro}()")

        # Verify parameterized macros
        cmd = obs[6]
        self.assertEqual(cmd["macro"], "ON_COMMAND")
        self.assertEqual(cmd["event_kind"], "command")
        self.assertEqual(cmd["event_id"], "ID_FILE_SAVE")
        self.assertEqual(cmd["handler_spelling"], "&COrderDlg::OnFileSave")
        self.assertEqual(cmd["qualified_handler"], "COrderDlg::OnFileSave")

        bn = obs[7]
        self.assertEqual(bn["macro"], "ON_BN_CLICKED")
        self.assertEqual(bn["event_kind"], "bn_clicked")
        self.assertEqual(bn["event_id"], "IDC_BTN_SUBMIT")
        self.assertEqual(bn["notification_code"], "BN_CLICKED")
        self.assertEqual(bn["handler_spelling"], "OnSubmitClicked")
        self.assertEqual(bn["qualified_handler"], "COrderDlg::OnSubmitClicked")

        ntf = obs[9]
        self.assertEqual(ntf["macro"], "ON_NOTIFY")
        self.assertEqual(ntf["event_kind"], "notify")
        self.assertEqual(ntf["notification_code"], "LVN_ITEMCHANGED")
        self.assertEqual(ntf["event_id"], "IDC_ORDER_LIST")
        self.assertEqual(ntf["qualified_handler"], "COrderDlg::OnOrderListChanged")

    def test_02_owner_and_handler_qualification_forms(self):
        source = (
            b"BEGIN_MESSAGE_MAP(ui::CMainFrame, CFrameWnd)\n"
            b"    ON_COMMAND(ID_ONE, OnOne)\n"
            b"    ON_COMMAND(ID_TWO, &ui::CMainFrame::OnTwo)\n"
            b"    ON_COMMAND(ID_THREE, CBaseFrame::OnThree)\n"
            b"END_MESSAGE_MAP()\n"
        )
        obs = scan_mfc(source, "MainFrame.cpp")
        self.assertEqual(len(obs), 3)
        self.assertEqual(obs[0]["owner_class"], "ui::CMainFrame")
        self.assertEqual(obs[0]["handler_spelling"], "OnOne")
        self.assertEqual(obs[0]["handler_class"], "ui::CMainFrame")
        self.assertEqual(obs[0]["qualified_handler"], "ui::CMainFrame::OnOne")

        self.assertEqual(obs[1]["handler_spelling"], "&ui::CMainFrame::OnTwo")
        self.assertEqual(obs[1]["handler_class"], "ui::CMainFrame")
        self.assertEqual(obs[1]["qualified_handler"], "ui::CMainFrame::OnTwo")

        self.assertEqual(obs[2]["handler_spelling"], "CBaseFrame::OnThree")
        self.assertEqual(obs[2]["handler_class"], "CBaseFrame")
        self.assertEqual(obs[2]["qualified_handler"], "CBaseFrame::OnThree")

    def test_03_range_macros_preserve_symbolic_bounds(self):
        source = (
            b"BEGIN_MESSAGE_MAP(COrderDlg, CDialog)\n"
            b"    ON_COMMAND_RANGE(ID_CMD_FIRST, ID_CMD_LAST, &COrderDlg::OnCmdRange)\n"
            b"    ON_UPDATE_COMMAND_UI_RANGE(ID_CMD_FIRST, ID_CMD_LAST, OnUpdateCmdRange)\n"
            b"    ON_CONTROL_RANGE(BN_CLICKED, IDC_RAD_1, IDC_RAD_9, &COrderDlg::OnRadioRange)\n"
            b"    ON_NOTIFY_RANGE(NM_CLICK, IDC_LIST_FIRST, IDC_LIST_LAST, &COrderDlg::OnListNotifyRange)\n"
            b"END_MESSAGE_MAP()\n"
        )
        obs = scan_mfc(source, "RangeDlg.cpp")
        self.assertEqual(len(obs), 4)

        self.assertEqual(obs[0]["event_kind"], "command_range")
        self.assertEqual(obs[0]["event_id"], "ID_CMD_FIRST")
        self.assertEqual(obs[0]["event_id_end"], "ID_CMD_LAST")
        self.assertEqual(obs[0]["event_range"], ("ID_CMD_FIRST", "ID_CMD_LAST"))

        self.assertEqual(obs[1]["event_kind"], "update_command_ui_range")
        self.assertEqual(obs[1]["event_range"], ("ID_CMD_FIRST", "ID_CMD_LAST"))

        self.assertEqual(obs[2]["event_kind"], "control_range")
        self.assertEqual(obs[2]["notification_code"], "BN_CLICKED")
        self.assertEqual(obs[2]["event_range"], ("IDC_RAD_1", "IDC_RAD_9"))

        self.assertEqual(obs[3]["event_kind"], "notify_range")
        self.assertEqual(obs[3]["notification_code"], "NM_CLICK")
        self.assertEqual(obs[3]["event_range"], ("IDC_LIST_FIRST", "IDC_LIST_LAST"))

    def test_04_ignore_fake_macros_in_comments_strings_and_raw_strings(self):
        source = (
            b'// BEGIN_MESSAGE_MAP(CFake1, CDialog) ON_COMMAND(ID_FAKE, OnFake) END_MESSAGE_MAP()\n'
            b'/* Multiline comment:\n'
            b'BEGIN_MESSAGE_MAP(CFake2, CDialog)\n'
            b'    ON_WM_CREATE()\n'
            b'END_MESSAGE_MAP()\n'
            b'*/\n'
            b'const char* s1 = "BEGIN_MESSAGE_MAP(CFake3, CDialog) ON_WM_PAINT() END_MESSAGE_MAP()";\n'
            b'const char* s2 = R"delim(\n'
            b'BEGIN_MESSAGE_MAP(CFake4, CDialog)\n'
            b'    ON_COMMAND(ID_RAW_FAKE, &CFake4::OnRawFake)\n'
            b'END_MESSAGE_MAP()\n'
            b')delim";\n'
            b'const wchar_t* s3 = LR"(\n'
            b'BEGIN_MESSAGE_MAP(CFake5, CDialog)\n'
            b'    ON_WM_TIMER()\n'
            b'END_MESSAGE_MAP()\n'
            b')";\n'
            b'const char* s4 = u8R"xyz(\n'
            b'BEGIN_MESSAGE_MAP(CFake6, CDialog)\n'
            b'    ON_WM_SIZE()\n'
            b'END_MESSAGE_MAP()\n'
            b')xyz";\n'
            b"char c_quote = '\"';\n"
            b'BEGIN_MESSAGE_MAP(CRealDlg, CDialog)\n'
            b'    // ON_COMMAND(ID_COMMENTED, OnCommented)\n'
            b'    /* ON_WM_DESTROY() */\n'
            b'    ON_WM_CLOSE()\n'
            b'END_MESSAGE_MAP()\n'
        )
        obs = scan_mfc(source, "CommentsStrings.cpp")
        self.assertEqual(len(obs), 1)
        self.assertEqual(obs[0]["owner_class"], "CRealDlg")
        self.assertEqual(obs[0]["macro"], "ON_WM_CLOSE")

    def test_05_ignore_fake_macros_inside_define_bodies(self):
        source = (
            b"#define MY_WRAPPER_MAP(cls) \\\n"
            b"    BEGIN_MESSAGE_MAP(cls, CDialog) \\\n"
            b"        ON_COMMAND(ID_FAKE_IN_DEFINE, &cls::OnFake) \\\n"
            b"    END_MESSAGE_MAP()\n"
            b"\n"
            b"#define FAKE_ENTRY ON_COMMAND(ID_NEVER, &CReal::OnNever)\n"
            b"\n"
            b"BEGIN_MESSAGE_MAP(CReal, CDialog)\n"
            b"    ON_WM_PAINT()\n"
            b"END_MESSAGE_MAP()\n"
        )
        obs = scan_mfc(source, "DefineBodies.cpp")
        self.assertEqual(len(obs), 1)
        self.assertEqual(obs[0]["owner_class"], "CReal")
        self.assertEqual(obs[0]["macro"], "ON_WM_PAINT")

    def test_06_multiline_and_nested_parenthesized_macro_args(self):
        source = (
            b"BEGIN_MESSAGE_MAP(CComplexDlg, CDialog)\n"
            b"    ON_MESSAGE(\n"
            b"        (WM_USER + (100 + OFFSET_A)),\n"
            b"        &CComplexDlg::OnCustomMsg\n"
            b"    )\n"
            b"    ON_COMMAND_RANGE(\n"
            b"        MAKE_ID(CAT_A, 1),\n"
            b"        MAKE_ID(CAT_A, (1 + 9)),\n"
            b"        &CComplexDlg::OnMakeIdRange\n"
            b"    )\n"
            b"END_MESSAGE_MAP()\n"
        )
        obs = scan_mfc(source, "ComplexArgs.cpp")
        self.assertEqual(len(obs), 2)
        self.assertEqual(obs[0]["kind"], "event_mapping")
        self.assertEqual(obs[0]["event_id"], "(WM_USER + (100 + OFFSET_A))")
        self.assertEqual(obs[0]["qualified_handler"], "CComplexDlg::OnCustomMsg")
        self.assertEqual(obs[0]["line"], 2)
        self.assertEqual(obs[0]["end_line"], 5)

        self.assertEqual(obs[1]["event_range"], ("MAKE_ID(CAT_A, 1)", "MAKE_ID(CAT_A, (1 + 9))"))
        self.assertEqual(obs[1]["line"], 6)
        self.assertEqual(obs[1]["end_line"], 10)

    def test_07_conditional_compilation_without_preprocessing_info(self):
        source = (
            b"BEGIN_MESSAGE_MAP(CCondDlg, CDialog)\n"
            b"    ON_WM_CREATE()\n"
            b"#if 0\n"
            b"    ON_WM_DESTROY()\n"
            b"#else\n"
            b"    ON_WM_CLOSE()\n"
            b"#endif\n"
            b"#ifdef _DEBUG\n"
            b"    ON_COMMAND(ID_DBG_DUMP, &CCondDlg::OnDbgDump)\n"
            b"#if 0\n"
            b"    ON_WM_TIMER()\n"
            b"#endif\n"
            b"#else\n"
            b"    ON_COMMAND(ID_REL_DUMP, &CCondDlg::OnRelDump)\n"
            b"#endif\n"
            b"    ON_WM_PAINT()\n"
            b"END_MESSAGE_MAP()\n"
        )
        obs = scan_mfc(source, "CondDlg.cpp", active_macro_offsets=None)
        by_macro = {o["macro"] + ":" + (o["event_id"] or ""): o for o in obs}
        self.assertEqual(by_macro["ON_WM_CREATE:WM_CREATE"]["condition_state"], "active")
        self.assertEqual(by_macro["ON_WM_DESTROY:WM_DESTROY"]["condition_state"], "inactive")
        self.assertEqual(by_macro["ON_WM_CLOSE:WM_CLOSE"]["condition_state"], "active")
        self.assertEqual(by_macro["ON_COMMAND:ID_DBG_DUMP"]["condition_state"], "unknown")
        self.assertEqual(by_macro["ON_WM_TIMER:WM_TIMER"]["condition_state"], "inactive")
        self.assertEqual(by_macro["ON_COMMAND:ID_REL_DUMP"]["condition_state"], "unknown")
        self.assertEqual(by_macro["ON_WM_PAINT:WM_PAINT"]["condition_state"], "active")

    def test_08_conditional_compilation_with_active_macro_offsets(self):
        source = (
            b"BEGIN_MESSAGE_MAP(CCondDlg, CDialog)\n"
            b"#ifdef _DEBUG\n"
            b"    ON_COMMAND(ID_DBG, &CCondDlg::OnDbg)\n"
            b"#else\n"
            b"    ON_COMMAND(ID_REL, &CCondDlg::OnRel)\n"
            b"#endif\n"
            b"    ON_WM_PAINT()\n"
            b"END_MESSAGE_MAP()\n"
        )
        dbg_offset = source.index(b"ON_COMMAND(ID_DBG")
        paint_offset = source.index(b"ON_WM_PAINT()")
        map_offset = source.index(b"BEGIN_MESSAGE_MAP")
        end_offset = source.index(b"END_MESSAGE_MAP")

        obs = scan_mfc(
            source,
            "CondDlg.cpp",
            active_macro_offsets={map_offset, dbg_offset, paint_offset, end_offset},
        )
        self.assertEqual(len(obs), 3)
        self.assertEqual(obs[0]["event_id"], "ID_DBG")
        self.assertEqual(obs[0]["condition_state"], "active")
        self.assertEqual(obs[1]["event_id"], "ID_REL")
        self.assertEqual(obs[1]["condition_state"], "inactive")
        self.assertEqual(obs[2]["macro"], "ON_WM_PAINT")
        self.assertEqual(obs[2]["condition_state"], "active")

    def test_09_unknown_on_wm_and_custom_macros_produce_diagnostics_no_fabrication(self):
        source = (
            b"BEGIN_MESSAGE_MAP(CCustomDlg, CDialog)\n"
            b"    ON_WM_ERASEBKGND()\n"
            b"    ON_CUSTOM_REFLECT_EX(WM_FOO, OnFoo)\n"
            b"    MY_COMPANY_MAP_ENTRY(IDC_BAR, OnBar)\n"
            b"    ON_WM_SIZE()\n"
            b"END_MESSAGE_MAP()\n"
        )
        obs = scan_mfc(source, "CustomDlg.cpp")
        self.assertEqual(len(obs), 4)

        # First 3 must be diagnostics, not fabricated event_mappings
        for idx in range(3):
            self.assertEqual(obs[idx]["kind"], "diagnostic")
            self.assertIsNone(obs[idx]["qualified_handler"])
            self.assertEqual(obs[idx]["owner_class"], "CCustomDlg")

        self.assertEqual(obs[0]["macro"], "ON_WM_ERASEBKGND")
        self.assertEqual(obs[0]["reason"], "unsupported_macro")
        self.assertEqual(obs[1]["macro"], "ON_CUSTOM_REFLECT_EX")
        self.assertEqual(obs[1]["reason"], "unsupported_macro")
        self.assertEqual(obs[2]["macro"], "MY_COMPANY_MAP_ENTRY")
        self.assertEqual(obs[2]["reason"], "unknown_map_macro")

        # Valid entry after unknown macros still extracts cleanly
        self.assertEqual(obs[3]["kind"], "event_mapping")
        self.assertEqual(obs[3]["macro"], "ON_WM_SIZE")
        self.assertEqual(obs[3]["qualified_handler"], "CCustomDlg::OnSize")

    def test_10_malformed_macro_invocations_and_handlers_produce_diagnostics(self):
        source = (
            b"BEGIN_MESSAGE_MAP(CBrokenDlg, CDialog)\n"
            b"    ON_COMMAND(ID_ONLY_ONE_ARG)\n"
            b"    ON_WM_CREATE(EXTRA_ARG)\n"
            b"    ON_BN_CLICKED(IDC_OK, 12345)\n"
            b"    ON_NOTIFY(NM_CLICK, IDC_LIST)\n"
            b"END_MESSAGE_MAP()\n"
        )
        obs = scan_mfc(source, "BrokenDlg.cpp")
        self.assertEqual(len(obs), 4)
        self.assertTrue(all(o["kind"] == "diagnostic" for o in obs))
        self.assertEqual(obs[0]["reason"], "malformed_invocation")
        self.assertEqual(obs[1]["reason"], "malformed_invocation")
        self.assertEqual(obs[2]["reason"], "malformed_handler")
        self.assertEqual(obs[3]["reason"], "malformed_invocation")

    def test_11_malformed_or_unclosed_message_maps(self):
        source1 = (
            b"END_MESSAGE_MAP()\n"
            b"BEGIN_MESSAGE_MAP(COnlyOneArg)\n"
            b"BEGIN_MESSAGE_MAP(CUnclosedDlg, CDialog)\n"
            b"    ON_WM_CLOSE()\n"
        )
        obs1 = scan_mfc(source1, "Unclosed.cpp")
        kinds = [(o["kind"], o["macro"], o["reason"]) for o in obs1]
        self.assertIn(("diagnostic", "END_MESSAGE_MAP", "unexpected_end_message_map"), kinds)
        self.assertIn(("diagnostic", "BEGIN_MESSAGE_MAP", "malformed_message_map"), kinds)
        self.assertIn(("event_mapping", "ON_WM_CLOSE", "handler_not_semantically_bound"), kinds)
        self.assertIn(("diagnostic", "BEGIN_MESSAGE_MAP", "unclosed_message_map"), kinds)

    def test_12_byte_offsets_and_lines_with_multibyte_utf8_and_crlf(self):
        # Include Vietnamese UTF-8 multi-byte characters and Windows CRLF line endings
        source = (
            "// Chú thích tiếng Việt có dấu ăâđêôơư\r\n"
            "BEGIN_MESSAGE_MAP(CVietDlg, CDialog)\r\n"
            "    /* Bình luận nhiều byte: Tiếng Việt */\r\n"
            "    ON_COMMAND(ID_VIET_CMD, &CVietDlg::OnVietCmd)\r\n"
            "END_MESSAGE_MAP()\r\n"
        ).encode("utf-8")

        obs = scan_mfc(source, "VietDlg.cpp", encoding="utf-8")
        self.assertEqual(len(obs), 1)
        entry = obs[0]
        self.assertEqual(entry["line"], 4)
        self.assertEqual(entry["end_line"], 4)
        extracted_bytes = source[entry["start_byte"] : entry["end_byte"]]
        self.assertEqual(extracted_bytes, b"ON_COMMAND(ID_VIET_CMD, &CVietDlg::OnVietCmd)")

    def test_13_resource_header_defines_safe_constant_evaluation_and_unresolved(self):
        source = (
            b"#define IDC_STATIC (-1)\r\n"
            b"#define IDD_ORDER_DLG 102\r\n"
            b"#define WM_APP_BASE 0x8000U\r\n"
            b"#define IDC_BTN_SAVE (IDD_ORDER_DLG + 10)\r\n"
            b"#define IDC_SHIFTED ((1 << 4) | 0x03)\r\n"
            b"#define IDC_EXTERNAL_REF (EXTERNAL_BASE + 5)\r\n"
            b"#define FUNC_MACRO(x) ((x) + 1)\r\n"
        )
        obs = scan_resources(source, "resource.h")
        by_sym = {o["symbol"]: o for o in obs if o["kind"] == "resource_define"}

        self.assertNotIn("FUNC_MACRO", by_sym)
        self.assertEqual(by_sym["IDC_STATIC"]["value"], -1)
        self.assertEqual(by_sym["IDC_STATIC"]["resolution_status"], "resolved")

        self.assertEqual(by_sym["IDD_ORDER_DLG"]["value"], 102)
        self.assertEqual(by_sym["WM_APP_BASE"]["value"], 0x8000)
        self.assertEqual(by_sym["IDC_BTN_SAVE"]["value"], 112)
        self.assertEqual(by_sym["IDC_SHIFTED"]["value"], 19)

        # Unresolved constant expression is retained without fabricating a value
        ext = by_sym["IDC_EXTERNAL_REF"]
        self.assertIsNone(ext["value"])
        self.assertEqual(ext["value_expr"], "(EXTERNAL_BASE + 5)")
        self.assertEqual(ext["resolution_status"], "unresolved")
        self.assertEqual(ext["reason"], "unresolved_constant_expression")
        self.assertEqual(
            source[ext["start_byte"] : ext["end_byte"]],
            b"#define IDC_EXTERNAL_REF (EXTERNAL_BASE + 5)",
        )

    def test_14_rc_dialogs_controls_and_menus_extraction(self):
        source = (
            b"#define IDD_MAIN_DLG 200\n"
            b"#define IDC_SAVE_BTN 1001\n"
            b"#define IDC_NAME_EDIT 1002\n"
            b"#define IDC_ORDER_LIST 1003\n"
            b"#define ID_FILE_EXIT 40001\n"
            b"\n"
            b"IDD_MAIN_DLG DIALOGEX 0, 0, 320, 200\n"
            b'CAPTION "Order Entry"\n'
            b"BEGIN\n"
            b'    DEFPUSHBUTTON "&Save", IDC_SAVE_BTN, 200, 170, 50, 14\n'
            b"    EDITTEXT IDC_NAME_EDIT, 50, 20, 120, 14, ES_AUTOHSCROLL\n"
            b'    CONTROL "Orders", IDC_ORDER_LIST, "SysListView32", LVS_REPORT | WS_BORDER, 7, 40, 300, 120\n'
            b"END\n"
            b"\n"
            b"IDR_MAINFRAME MENU\n"
            b"BEGIN\n"
            b'    POPUP "&File"\n'
            b"    BEGIN\n"
            b'        MENUITEM "E&xit", ID_FILE_EXIT\n'
            b"    END\n"
            b"END\n"
        )
        obs = scan_resources(source, "App.rc")
        dialogs = [o for o in obs if o["kind"] == "resource_dialog"]
        menus = [o for o in obs if o["kind"] == "resource_menu"]
        controls = [o for o in obs if o["kind"] == "resource_control"]

        self.assertEqual(len(dialogs), 1)
        self.assertEqual(dialogs[0]["resource_id"], "IDD_MAIN_DLG")
        self.assertEqual(dialogs[0]["value"], 200)

        self.assertEqual(len(menus), 1)
        self.assertEqual(menus[0]["resource_id"], "IDR_MAINFRAME")

        self.assertEqual(len(controls), 4)
        self.assertEqual(controls[0]["control_kind"], "DEFPUSHBUTTON")
        self.assertEqual(controls[0]["resource_id"], "IDC_SAVE_BTN")
        self.assertEqual(controls[0]["value"], 1001)
        self.assertEqual(controls[0]["parent_id"], "IDD_MAIN_DLG")
        self.assertEqual(controls[0]["caption"], "&Save")

        self.assertEqual(controls[1]["control_kind"], "EDITTEXT")
        self.assertEqual(controls[1]["resource_id"], "IDC_NAME_EDIT")
        self.assertEqual(controls[1]["value"], 1002)

        self.assertEqual(controls[2]["control_kind"], "CONTROL")
        self.assertEqual(controls[2]["control_class"], "SysListView32")
        self.assertEqual(controls[2]["resource_id"], "IDC_ORDER_LIST")
        self.assertEqual(controls[2]["value"], 1003)

        self.assertEqual(controls[3]["control_kind"], "MENUITEM")
        self.assertEqual(controls[3]["resource_id"], "ID_FILE_EXIT")
        self.assertEqual(controls[3]["value"], 40001)
        self.assertEqual(controls[3]["parent_id"], "IDR_MAINFRAME")

    def test_15_multiple_message_maps_and_template_map_diagnostic(self):
        source = (
            b"BEGIN_TEMPLATE_MESSAGE_MAP(CTemplDlg, T, CDialog)\n"
            b"    ON_WM_PAINT()\n"
            b"END_MESSAGE_MAP()\n"
            b"\n"
            b"BEGIN_MESSAGE_MAP(CFirstDlg, CDialog)\n"
            b"    ON_WM_CREATE()\n"
            b"END_MESSAGE_MAP()\n"
            b"\n"
            b"BEGIN_MESSAGE_MAP(CSecondDlg, CDialog)\n"
            b"    ON_WM_DESTROY()\n"
            b"END_MESSAGE_MAP()\n"
        )
        obs = scan_mfc(source, "MultiMap.cpp")
        # Template message map produces diagnostic + unexpected END_MESSAGE_MAP diagnostic,
        # followed by 2 clean event_mappings for CFirstDlg and CSecondDlg
        mappings = [o for o in obs if o["kind"] == "event_mapping"]
        diags = [o for o in obs if o["kind"] == "diagnostic"]
        self.assertEqual(len(mappings), 2)
        self.assertEqual(mappings[0]["owner_class"], "CFirstDlg")
        self.assertEqual(mappings[0]["qualified_handler"], "CFirstDlg::OnCreate")
        self.assertEqual(mappings[1]["owner_class"], "CSecondDlg")
        self.assertEqual(mappings[1]["qualified_handler"], "CSecondDlg::OnDestroy")
        self.assertTrue(any(d["macro"] == "BEGIN_TEMPLATE_MESSAGE_MAP" and d["reason"] == "unsupported_macro" for d in diags))


if __name__ == "__main__":
    unittest.main()
