import unittest

from wows_training.replay_grid import location_label, map_cell, map_location


class ReplayGridTests(unittest.TestCase):
    bounds = {"min_x": 0, "max_x": 100, "min_z": 0, "max_z": 100}

    def test_grid_uses_north_first_and_left_to_right_columns(self):
        self.assertEqual(map_cell(self.bounds, 100, 100), "A10")
        self.assertEqual(map_cell(self.bounds, 0, 0), "J1")
        self.assertEqual(map_cell(self.bounds, 53, 64), "D6")
        self.assertIsNone(map_cell(self.bounds, 101, 50))
        self.assertIsNone(map_cell(None, 50, 50))

    def test_capture_zone_is_a_relative_location_without_relabeling_the_ship_cell(self):
        cap = {"name": "B", "x": 65, "z": 64, "radius": 8}
        location = map_location(self.bounds, 53, 64, [cap])
        self.assertEqual(location, {"map_cell": "D6", "cap_name": "B",
                                    "cap_cell": "D7", "cap_relative_position": "西侧"})
        self.assertEqual(location_label(location), "D6 · B 点西侧")
        self.assertEqual(map_location(self.bounds, 64, 64, [cap])["cap_relative_position"], "圈内")
        self.assertEqual(map_location(None, 64, 64, [cap]), {})
