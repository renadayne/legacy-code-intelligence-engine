"""Independent API presentation regression checks."""

import unittest

from legacygraph.web import _normalize_graph_payload, _parse_csv_or_list


class WebReviewTests(unittest.TestCase):
    def test_unknown_target_remains_null_without_bypassing_node_budget(self):
        payload = _normalize_graph_payload({
            'nodes': [{'id': 1, 'semantic_key': 'seed', 'name': 'seed'}],
            'edges': [{'id': 2, 'source': 'seed', 'target': None, 'resolution_status': 'unresolved',
                       'unresolved_target_expr': 'callback()'}],
        })
        self.assertEqual(len(payload['nodes']), 1)
        self.assertIsNone(payload['edges'][0]['target'])
        self.assertEqual(payload['edges'][0]['unresolved_target_expr'], 'callback()')

    def test_seed_identity_can_contain_a_comma(self):
        seed = 'usr:demo:operator<int,double>'
        self.assertEqual(_parse_csv_or_list([seed], 'seeds'), [seed])


if __name__ == '__main__':
    unittest.main()
