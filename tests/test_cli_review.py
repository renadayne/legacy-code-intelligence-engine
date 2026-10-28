"""Independent CLI regression tests for supported output layouts."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from legacygraph.cli import main
from legacygraph.index import IndexStore


class CliReviewTests(unittest.TestCase):
    def test_database_cannot_replace_an_empty_source_file(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'empty.cpp'
            source.touch()
            with self.assertRaises(ValueError):
                IndexStore(source)
            self.assertEqual(source.read_bytes(), b'')

    def test_index_cannot_be_rebound_to_a_different_source_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = root / 'first', root / 'second'
            first.mkdir()
            second.mkdir()
            with IndexStore(root / 'index.db') as store:
                store.initialize_schema(first)
                with self.assertRaises(ValueError):
                    store.initialize_schema(second)
                self.assertEqual(store.get_repo_root(), first.resolve())

    def test_database_in_source_root_does_not_exclude_all_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'main.cpp').write_text('int main() { return 0; }\n', encoding='utf-8')
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = main(['discover', '--repo', str(root), '--db', str(root / 'index.db'), '--json'])
            self.assertEqual(result, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload['discovered_count'], 1)
            self.assertEqual(payload['files'][0]['repo_path'], 'main.cpp')


if __name__ == '__main__':
    unittest.main()
