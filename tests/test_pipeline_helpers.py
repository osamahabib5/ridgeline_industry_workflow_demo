import unittest

import pipeline


class ExtractionHelperTests(unittest.TestCase):
    def test_quantity_parser_avoids_product_dimensions(self):
        self.assertEqual(
            pipeline._extract_quantity_unit("20 ea 6204-2RS bearings"),
            (20, "EA"),
        )
        self.assertEqual(
            pipeline._extract_quantity_unit("6205 bearings sealed qty 24"),
            (24, None),
        )
        self.assertEqual(
            pipeline._extract_quantity_unit('1/2-13 x 2" grade 8 hex bolts, 2 boxes'),
            (2, "BX"),
        )
        self.assertEqual(pipeline._extract_quantity_unit("RL159429 x 3"), (3, None))

    def test_forwarded_updates_override_older_item_values(self):
        message = """Please quote per Rick's changes below.
Rick - make the motor 5HP not 3, and bump the gaskets to 40.
- 3 HP TEFC motor, 1800 rpm, 3 phase - qty 2
- 2 inch flange gaskets 150# - qty 20
- food grade grease cartridges - 3 cases
"""
        items = pipeline._extract_text_items(message)
        self.assertEqual(len(items), 3)
        self.assertIn("5 HP", items[0]["as_written"])
        self.assertEqual(items[1]["quantity"], 40)
        self.assertEqual(items[2]["quantity"], 3)


class MatchingHelperTests(unittest.TestCase):
    def test_direct_and_discontinued_matches_are_validated(self):
        products, cross_references, _ = pipeline._load_catalog()

        direct = pipeline._match_item(
            {"as_written": "RL159429 x 3", "part_number": "RL159429"},
            products,
            cross_references,
        )
        self.assertEqual(direct["method"], "ridgeline_part_number")
        self.assertEqual(direct["confidence"], 0.95)

        replacement = pipeline._match_item(
            {"as_written": "RL150432", "part_number": "RL150432"},
            products,
            cross_references,
        )
        self.assertEqual(replacement["part"], "RL120374")
        self.assertIn("discontinued_replaced", replacement["flags"])


if __name__ == "__main__":
    unittest.main()
