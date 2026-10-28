"""Public CLI workflow using a real Clang-indexed multi-file sample."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from legacygraph.clang_parser import is_libclang_available
from legacygraph.cli import main
from legacygraph.scanner import scan_repository


@unittest.skipUnless(is_libclang_available()[0], 'Optional libclang semantic dependency is not installed')
class WorkflowTests(unittest.TestCase):
    def _cli(self, args):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            result = main(args)
        return result, output.getvalue(), errors.getvalue()

    def test_search_path_export_and_incremental_noop(self):
        example = Path(__file__).resolve().parents[1] / 'examples' / 'mfc_demo'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / 'index.db'
            scan_repository(example, database, example / 'manifest.json')
            common = ['--db', str(database)]
            result, output, errors = self._cli(['search', 'OnSave', *common])
            self.assertEqual(result, 0, errors)
            self.assertEqual(json.loads(output)['items'][0]['qualified_name'], 'COrderDlg::OnSave')
            result, output, errors = self._cli(['path', 'COrderDlg::OnSave', 'SaveOrder', *common, '--kinds', 'calls'])
            self.assertEqual(result, 0, errors)
            self.assertTrue(json.loads(output)['found'])
            self.assertEqual(json.loads(output)['hop_count'], 1)

            target = root / 'context.json'
            result, output, errors = self._cli(['export', 'COrderDlg::OnSave', *common, '--depth', '2',
                                              '--kinds', 'calls', '--format', 'json', '--output', str(target)])
            self.assertEqual(result, 0, errors)
            original = target.read_bytes()
            pack = json.loads(original)
            self.assertEqual(pack['status'], 'ok')
            self.assertTrue(pack['metadata']['within_budget'])
            self.assertIn('lastResult = SaveOrder(42)', json.dumps(pack))
            self.assertFalse(pack['metadata']['exact_token_budget_guaranteed'])
            result, _, _ = self._cli(['export', 'COrderDlg::OnSave', *common, '--format', 'json', '--output', str(target)])
            self.assertEqual(result, 1)
            self.assertEqual(target.read_bytes(), original)
            result, _, _ = self._cli(['export', 'COrderDlg::OnSave', *common, '--token-budget', '256'])
            self.assertEqual(result, 3)
            self.assertTrue(scan_repository(example, database, example / 'manifest.json')['noop'])
            result, output, errors = self._cli(['status', *common, '--check-invalidated', '--json'])
            self.assertEqual(result, 0, errors)
            self.assertEqual(json.loads(output)['invalidated_translation_units'], [])

    def test_failed_tu_produces_a_nonzero_scan_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'broken.cpp').write_text('#include "missing.h"\nint run() { return unknown(); }\n', encoding='utf-8')
            manifest = root / 'manifest.json'
            manifest.write_text(json.dumps({'translation_units': [{'file': 'broken.cpp', 'compiler_family': 'clang'}]}), encoding='utf-8')
            result, output, _ = self._cli(['scan', '--repo', str(root), '--manifest', str(manifest),
                                          '--db', str(root / 'index.db'), '--json'])
            self.assertEqual(result, 3)
            self.assertEqual(json.loads(output)['failed_translation_units'], 1)


if __name__ == '__main__':
    unittest.main()
