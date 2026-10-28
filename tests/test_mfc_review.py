"""Independent MFC/resource regressions found during integration review."""

import unittest

from legacygraph.mfc import scan_mfc, scan_resources


class MfcReviewTests(unittest.TestCase):
    def test_syntactic_handler_spelling_does_not_claim_semantic_binding(self):
        source = b'BEGIN_MESSAGE_MAP(CDlg,CWnd)\nON_COMMAND(ID_SAVE,OnMissing)\nEND_MESSAGE_MAP()'
        mapping = scan_mfc(source, 'sample.cpp')[0]
        self.assertEqual(mapping['resolution_status'], 'unresolved')
        self.assertEqual(mapping['reason'], 'handler_not_semantically_bound')

    def test_false_macro_is_not_assumed_to_be_zero(self):
        source = b'#define FALSE 1\n#if FALSE\nBEGIN_MESSAGE_MAP(CDlg,CWnd)\nON_COMMAND(ID_SAVE,OnSave)\nEND_MESSAGE_MAP()\n#endif'
        mapping = next(item for item in scan_mfc(source, 'sample.cpp') if item['kind'] == 'event_mapping')
        self.assertEqual(mapping['condition_state'], 'unknown')

    def test_utf16_resources_preserve_original_spans(self):
        raw = '#define IDC_ONE 100\r\n#define IDC_TWO (IDC_ONE + 2)\r\n'.encode('utf-16')
        items = scan_resources(raw, 'resource.h', 'utf-16')
        self.assertEqual([item['value'] for item in items], [100, 102])
        self.assertEqual([item['line'] for item in items], [1, 2])
        for item in items:
            original = raw[item['start_byte']:item['end_byte']].decode('utf-16-le')
            self.assertTrue(original.startswith('#define '))
            self.assertIn(item['symbol'], original)

    def test_utf16_mfc_offsets_can_be_matched_to_preprocessing(self):
        text = 'BEGIN_MESSAGE_MAP(CDlg,CWnd)\r\nON_COMMAND(ID_SAVE,OnSave)\r\nEND_MESSAGE_MAP()'
        raw = text.encode('utf-16')
        offset = raw.index('ON_COMMAND'.encode('utf-16-le'))
        begin = raw.index('BEGIN_MESSAGE_MAP'.encode('utf-16-le'))
        end = raw.index('END_MESSAGE_MAP'.encode('utf-16-le'))
        mapping = next(item for item in scan_mfc(raw, 'sample.cpp', 'utf-16', {begin, offset, end})
                       if item['kind'] == 'event_mapping')
        self.assertEqual(mapping['start_byte'], offset)
        self.assertEqual(mapping['condition_state'], 'active')
        self.assertEqual(mapping['line'], 2)

    def test_integer_division_does_not_round_through_float(self):
        items = scan_resources(b'#define BIG (9007199254740993 / 1)\n', 'resource.h')
        self.assertEqual(items[0]['value'], 9007199254740993)

    def test_uncertain_redefinition_and_undef_invalidate_old_value(self):
        raw = b'#define ID 1\n#ifdef MAYBE\n#define ID 2\n#endif\n#define NEXT (ID+1)\n#undef ID\n#define LAST (ID+2)\n'
        items = {item['symbol']: item for item in scan_resources(raw, 'resource.h')}
        self.assertIsNone(items['NEXT']['value'])
        self.assertIsNone(items['LAST']['value'])


if __name__ == '__main__':
    unittest.main()
