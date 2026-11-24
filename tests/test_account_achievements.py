import unittest

from wows_training.career_catalog import current_ship_catalog
from wows_training.f1_ratings.challenges import evaluate_challenges


def _ship(ship_id, battles, **stats):
    return {"ship_id": str(ship_id), "statistics": {"battles": battles, **stats}}


class AccountAchievementTests(unittest.TestCase):
    def test_current_black_friday_ids_exclude_arkansas_beta_and_count_distinct_played_ships(self):
        fleet = {"status": "ok", "ships": [
            _ship(3332356080, 9),   # Hornet B
            _ship(3340678352, 3),   # Incheon B
            _ship(4281284592, 7),   # Arkansas Beta, not Black Friday
            _ship(3353327600, 12),  # AL Hornet
            _ship(3332323152, 0),   # Flandre B, no random battles
        ]}
        rows, collections = evaluate_challenges({"battles": 31}, fleet, {})
        by_id = {row["id"]: row for row in collections}
        self.assertEqual(by_id["black_friday"]["ships"], 2)
        self.assertEqual(by_id["black_friday"]["battles"], 12)
        self.assertTrue(by_id["black_friday"]["complete"])
        self.assertEqual(by_id["azur_lane"]["ships"], 1)
        self.assertEqual(current_ship_catalog()["3340678352"]["species"], "Cruiser")
        self.assertEqual(len(rows), 21)

    def test_incomplete_classes_prevent_false_pending_rates_or_false_completion(self):
        account = {"battles": 20, "max_frags_battle": 8}
        fleet = {"status": "ok", "ships": [
            _ship("42", 10, main_battery={"shots": 160, "hits": 120}),
            _ship("43", 5, damage_dealt=20002),
        ]}
        rows, collections = evaluate_challenges(account, fleet, encyclopedia={"43": {"tier": 9}})
        by_id = {row["id"]: row for row in rows}
        self.assertEqual(by_id["battle_frags"]["status"], "earned")
        self.assertEqual(by_id["main_battery_accuracy"]["status"], "unknown")
        self.assertEqual(by_id["high_tier_low_damage"]["status"], "unknown")
        self.assertIsNone(by_id["high_tier_low_damage"]["value"])
        self.assertEqual(by_id["solo_warrior"]["status"], "unknown")
        self.assertEqual(by_id["ship_battles"]["value"], 10)
        self.assertTrue(by_id["ship_battles"]["is_lower_bound"])
        self.assertIsNone(collections[0]["ships"])

    def test_missing_damage_cannot_trigger_low_damage_rule_and_rules_keep_raw_precision(self):
        account = {"battles": 15}
        ships = [_ship("1", 5, damage_dealt=20001), _ship("2", 10, main_battery={"shots": 100, "hits": 60})]
        fleet = {"status": "ok", "ships": ships}
        tags = {str(i): {"tier": 8, "type": "Cruiser"} for i in (1, 2)}
        rows, _ = evaluate_challenges(account, fleet, {}, encyclopedia=tags)
        by_id = {row["id"]: row for row in rows}
        self.assertNotEqual(by_id["high_tier_low_damage"]["status"], "earned")
        self.assertEqual(by_id["main_battery_accuracy"]["status"], "earned")
        ships[1]["statistics"]["damage_dealt"] = 90000
        rows, _ = evaluate_challenges(account, fleet, {}, encyclopedia=tags)
        self.assertEqual(next(row for row in rows if row["id"] == "high_tier_low_damage")["status"], "pending")
