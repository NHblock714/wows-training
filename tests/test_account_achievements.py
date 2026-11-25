import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from wows_training.achievement_ship_rosters import ShipRoster
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
        self.assertEqual(len(rows), 66)

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

    def test_approved_exact_thresholds_and_official_award_keys(self):
        ship = _ship("42", 91, damage_dealt=114_514*91+46)
        fleet = {"status": "ok", "ships": [ship]}
        awards = {"PCH002_OneSoldierInTheField":10,"PCH010_Retribution":199}
        rows, _ = evaluate_challenges({"battles":91,"max_ships_spotted":13},fleet,awards)
        rules = {r["id"]:r for r in rows}
        self.assertEqual(rules["battle_spots"]["status"],"pending")
        self.assertEqual(rules["ship_damage_114514"]["status"],"pending")
        self.assertEqual(rules["solo_warrior"]["status"],"earned")
        self.assertEqual(rules["flesh_wound_200"]["status"],"pending")
        self.assertEqual(rules["historical_detonation"]["status"],"unknown")
        ship["statistics"]["damage_dealt"] -= 46
        awards["PCH010_Retribution"] = 200
        rows, _ = evaluate_challenges({"battles":91,"max_ships_spotted":12},fleet,awards)
        rules = {r["id"]:r for r in rows}
        for name in ("battle_spots","ship_damage_114514","flesh_wound_200"):
            self.assertEqual(rules[name]["status"],"earned")
        self.assertEqual(rules["battle_spots"]["rarity"],"罕见")

    def test_displayed_damage_award_records_all_played_matching_ships(self):
        target = 114_514
        ships = [
            _ship("41", 0, damage_dealt=target),
            _ship("43", 91, damage_dealt=target*91+46),
            _ship("42", 1, damage_dealt=target),
            _ship("44", 91, damage_dealt=target*91+45),
            _ship("45", 100, damage_dealt=target*100-50),
            _ship("46", 100, damage_dealt=target*100+50),
            _ship("47", 100, damage_dealt=target*100-51),
        ]
        native = {"44": {"display_name": "本地舰名"}}
        official = {"42": {"name": "刚出战的战舰"}, "44": {"name": "替代舰名"}, "45": {"name": "官方舰名"}}

        def result(fleet):
            battles = sum(ship["statistics"]["battles"] for ship in fleet)
            rows, _ = evaluate_challenges({"battles": battles}, {"status": "ok", "ships": fleet},
                                           encyclopedia=official, local_catalog=native)
            return next(row for row in rows if row["id"] == "ship_damage_114514")

        pending = result(ships[:2])
        self.assertEqual((pending["status"], pending["matched_ships"]), ("pending", []))
        first_battle = result(ships[:3])
        self.assertEqual((first_battle["status"], first_battle["matched_ships"]), ("earned", ["刚出战的战舰"]))
        earned = result(ships)
        self.assertEqual((earned["status"], earned["value"]), ("earned", target))
        self.assertEqual(earned["matched_ships"], ["刚出战的战舰", "本地舰名", "官方舰名", "46"])

    def test_zero_submarine_battles_distinguishes_missing_data_from_known_zero(self):
        fleet = {"status":"ok","ships":[_ship("42",1)]}
        rows,_ = evaluate_challenges({"battles":1},fleet)
        self.assertEqual(next(r for r in rows if r["id"]=="submarine_free")["status"],"unknown")
        rows,_ = evaluate_challenges({"battles":1},fleet,encyclopedia={"42":{"type":"Cruiser"}})
        self.assertEqual(next(r for r in rows if r["id"]=="submarine_free")["status"],"earned")
        rows,_ = evaluate_challenges({"battles":0},{"status":"no_battles","ships":[]})
        self.assertEqual(next(r for r in rows if r["id"]=="submarine_free")["status"],"earned")
        self.assertEqual(next(r for r in rows if r["id"]=="low_account_damage")["status"],"pending")

    def test_compound_rules_use_same_ship_and_or_gate(self):
        ships = [_ship("4179572528",450), _ship("3969857328",450), _ship("3655284528",1_100),
                 _ship("3750705136",299), _ship("3749623504",1), _ship("3425613520",40),
                 _ship("3655284688",68,max_damage_dealt=180_000)]
        fleet = {"status":"ok","ships":ships}
        rows,_ = evaluate_challenges({"battles":sum(s["statistics"]["battles"] for s in ships)},fleet)
        rules = {r["id"]:r["status"] for r in rows}
        self.assertEqual(rules["credit_ship_trio"],"earned")
        self.assertEqual(rules["german_bb_tech"],"pending")
        self.assertEqual(rules["german_bb_mecklenburg"],"earned")
        self.assertEqual(rules["incomparable_69"],"pending")
        # Removing the original Yoshino leaves only a B variant and fails the gate.
        fleet["ships"] = [s for s in ships if s["ship_id"]!="3749623504"]
        rows,_ = evaluate_challenges({"battles":sum(s["statistics"]["battles"] for s in fleet["ships"])},fleet)
        self.assertEqual(next(r for r in rows if r["id"]=="credit_ship_trio")["status"],"pending")

    def test_coverage_achievements_require_the_denominator_but_known_count_can_earn(self):
        fleet = {"status":"ok","ships":[_ship("42",10),_ship("43",10),_ship("44",0)]}
        ids = frozenset({"42","43"})
        with patch("wows_training.f1_ratings.challenges.historical_ship_roster",
                   return_value=ShipRoster(ids,False)):
            rows,_ = evaluate_challenges({"battles":20},fleet,region="asia")
        rules = {r["id"]:r for r in rows}
        self.assertEqual(rules["premium_1"]["status"],"earned")
        self.assertEqual(rules["premium_all"]["status"],"unknown")
        self.assertEqual(rules["high_tier_submarines_all"]["status"],"unknown")
        with patch("wows_training.f1_ratings.challenges.historical_ship_roster",
                   return_value=ShipRoster(ids,True)):
            rows,_ = evaluate_challenges({"battles":20},fleet,region="asia")
        rules = {r["id"]:r for r in rows}
        self.assertEqual(rules["premium_all"]["status"],"earned")
        self.assertEqual(rules["premium_all"]["rarity"],"神话")
        self.assertEqual(rules["high_tier_submarines_all"]["status"],"earned")

    def test_anniversary_and_rankings_use_confirmed_same_region_evidence(self):
        tiers = {sid:{"tier":tier} for sid,tier in zip(("42","43","44","45","46"), (9,10,11,9,10))}
        fleet = {"status":"ok","ships":[_ship(sid,80) for sid in tiers]}
        account = {"battles":400}
        created = int(datetime(2016,11,25,tzinfo=timezone.utc).timestamp())
        ranks = {"region":"eu","source":"wows-numbers","ship_ranks":{"42":10,"43":1,"44":2,"45":4,"46":5}}
        rows,_ = evaluate_challenges(account,fleet,created_at=created,as_of="2026-11-24T00:00:00Z",
                                     region="asia",encyclopedia=tiers,numbers_rankings=ranks)
        rules = {r["id"]:r["status"] for r in rows}
        self.assertEqual(rules["account_age"],"pending")
        self.assertEqual(rules["numbers_top10"],"unknown")
        ranks["region"] = "asia"
        rows,_ = evaluate_challenges(account,fleet,created_at=created,as_of="2026-11-25T00:00:00Z",
                                     region="asia",encyclopedia=tiers,numbers_rankings=ranks)
        rules = {r["id"]:r for r in rows}
        self.assertEqual(rules["account_age"]["status"],"earned")
        result = rules["numbers_top10"]
        self.assertEqual((result["status"],result["value"],result["target"]),("earned",5,5))

    def test_numbers_rankings_exclude_tier_eight_and_keep_unknown_tiers_unresolved(self):
        tiers = {sid:{"tier":tier} for sid,tier in zip(("42","43","44","45","46","47"), (8,9,10,11,9,10))}
        fleet = {"status":"ok","ships":[_ship(sid,80) for sid in tiers]}
        account = {"battles":480}
        ranks = {"region":"asia","source":"wows-numbers","ship_ranks":{"42":1,"43":3,"44":2,"45":10,"46":4,"47":11}}
        rows,_ = evaluate_challenges(account,fleet,region="asia",encyclopedia=tiers,numbers_rankings=ranks)
        result = next(row for row in rows if row["id"] == "numbers_top10")
        self.assertEqual((result["status"],result["value"],result["complete"]),("pending",4,True))
        del tiers["42"]
        rows,_ = evaluate_challenges(account,fleet,region="asia",encyclopedia=tiers,numbers_rankings=ranks)
        result = next(row for row in rows if row["id"] == "numbers_top10")
        self.assertEqual((result["status"],result["value"],result["complete"]),("unknown",4,False))
