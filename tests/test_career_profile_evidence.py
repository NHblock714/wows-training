"""Identity and scope checks for the anonymous account analysis contract."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from wows_training.ai_analysis import AIError, career_profile_skill, prepare_evidence, tactical_system_prompt, validate_response
from wows_training.career_profile_evidence import (CAREER_PROFILE_CONTRACT, LEGACY_CAREER_PROFILE_CONTRACT, PROFILE_QUESTION,
                                                  build_career_profile_evidence)


def sample():
    overall = {"status": "ok", "statistics": {"battles": 100, "wins": 53, "damage_dealt": 7_225_500,
                                               "main_battery": {"shots": 1000, "hits": 250}}}
    fleet = [
        {"ship_id": "4273911792", "statistics": {"battles": 80, "wins": 45, "damage_dealt": 6_000_000}},
        {"ship_id": "4179506992", "statistics": {"battles": 10, "wins": 4}},
    ]
    modes = {"pvp": {"account": overall, "ships": {"status": "ok", "ships": fleet}},
             "pvp_solo": {"account": {"status": "ok", "statistics": {"battles": 70}}},
             "rank_solo": {"account": {"status": "no_battles", "statistics": {"battles": 0}}}}
    target = {"kind": "player", "region": "asia", "id": 123456789, "name": "AccountPrivacyFixture"}
    record = {"id": "private-snapshot-key", "kind": "career_snapshot", "payload": {
        "account_id": "123456789", "nickname": target["name"], "schema_version": 1, "region": "asia",
        "source_type": "official_query", "account_status": "ok", "mode_results": modes,
        "timestamp": "2026-11-22T17:43:12+00:00", "time_kind": "fetched_at_utc",
        "source": {"name": "private-provider-data", "path": r"C:\private\name"}}}
    profile = {**record["payload"], "snapshot_id": record["id"], "mode": "pvp", "ships_status": "ok",
               "ships": [{"ship_id": "4273911792", "name": target["name"], "species": "Cruiser", "tier": 10, "battles": 80},
                         {"ship_id": "4179506992", "name": target["name"], "species": "Destroyer", "tier": 10, "battles": 10}],
               "rating": {"pr": 1550.125, "covered_battles": 80, "total_battles": 90}}
    return profile, target, record


class CareerProfileEvidenceTests(unittest.TestCase):
    def test_private_identity_and_unrelated_request_data_do_not_reach_preview(self):
        profile, target, record = sample()
        analysis = build_career_profile_evidence(profile, target, record)
        prepared = prepare_evidence(analysis, target["name"], anonymize=False, plan="private plan",
                                    own_conditions=record["id"], images=[{"unexpected": True}])
        text = json.dumps(prepared, ensure_ascii=False)
        for value in (target["name"], "123456789", record["id"], "private plan", "private-provider-data", "unexpected"):
            self.assertNotIn(value, text)
        self.assertEqual(prepared["payload"]["question"], PROFILE_QUESTION)
        self.assertTrue(prepared["preview"]["anonymize"])
        self.assertEqual(prepare_evidence(analysis, "")["prepared_hash"], prepared["prepared_hash"])
        analysis["evidence"][-1]["details"]["name"] = target["name"]
        with self.assertRaisesRegex(AIError, "匿名快照"):
            prepare_evidence(analysis, "")

    def test_divisions_and_samples_do_not_use_overall_or_missing_data_as_denominators(self):
        result = {row["kind"]: row["details"] for row in build_career_profile_evidence(*sample())["evidence"]}
        modes = {row["mode"]: row for row in result["career_mode_activity"]["support"]}
        self.assertEqual(modes["pvp"]["count"], 100)
        self.assertEqual(modes["pvp_solo"]["scope"], "随机战分项")
        self.assertEqual(modes["rank_solo"]["count"], 0)
        self.assertIsNone(modes["pvp_div3"]["count"])
        classes = result["career_ship_classes"]
        self.assertEqual(classes["denominator"], 90)
        self.assertEqual(classes["support"][0]["share"], .8889)
        self.assertEqual(result["career_pr_coverage"]["coverage_ratio"], .8889)
        self.assertIn({"metric": "main_battery_hit_rate", "label": "主炮命中率", "value": .25, "unit": "比例"},
                      result["career_random_summary"]["support"])
        self.assertTrue(all(row["ship_name"] != "AccountPrivacyFixture" for row in result["career_favorite_ships"]["support"]))

    def test_wrong_identity_stale_snapshot_and_partial_fleet(self):
        profile, target, record = sample()
        with self.assertRaisesRegex(ValueError, "不一致"):
            build_career_profile_evidence(profile, {**target, "region": "eu"}, record)
        with self.assertRaisesRegex(ValueError, "不一致"):
            build_career_profile_evidence({**profile, "snapshot_id": "different"}, target, record)
        with self.assertRaisesRegex(ValueError, "不一致"):
            build_career_profile_evidence({**profile, "timestamp": "2026-11-20"}, target, record)
        record["payload"]["mode_results"]["pvp"]["ships"] = {"status": "not_fetched", "ships": []}
        rows = {row["kind"]: row for row in build_career_profile_evidence(profile, target, record)["evidence"]}
        self.assertNotIn("career_favorite_ships", rows)
        self.assertEqual(rows["career_ship_classes"]["details"]["status"], "unknown")
        self.assertEqual(rows["career_pr_coverage"]["details"]["status"], "unknown")
        with self.assertRaisesRegex(ValueError, "隐藏"):
            build_career_profile_evidence(profile, target, {**record, "payload": {**record["payload"], "account_status": "private"}})

    def test_raw_modes_include_all_fleet_tiers_and_keep_unknown_mode_fleets(self):
        profile, target, record = sample()
        profile.update(mode='rank_solo', ships=[], ships_status='not_fetched')
        raw = record['payload']
        raw['rating_references'] = {'local_catalog': {'1': {'tier': 2, 'species': 'Cruiser'},
                                                      '2': {'tier': 7, 'species': 'Destroyer'},
                                                      '3': {'tier': 11, 'species': 'Battleship'}}}
        raw['mode_results']['pve'] = {
            'account': {'status': 'ok', 'statistics': {'battles': 20, 'damage_dealt': 100_000, 'wins': 9,
                                                      'second_battery': {'hits': 111}}},
            'ships': {'status': 'ok', 'ships': [{'ship_id': str(i), 'mode': 'pve', 'statistics': {'battles': count}}
                                               for i, count in ((1, 7), (2, 4), (3, 1), (4, 3))]}}
        raw['mode_results']['oper_div'] = {'account': {'status': 'ok', 'statistics': {
            'battles': 13, 'wins_by_tasks': {'5': 7}}}}
        prepared = prepare_evidence(build_career_profile_evidence(profile, target, record), '')
        details = {r['id']: r['details'] for r in prepared['payload']['evidence']}
        by_mode = {r['mode']: r for r in details['P09']['support']}
        self.assertEqual(by_mode['pve']['coverage_ratio'], .75)
        self.assertEqual({r['label']: r['count'] for r in by_mode['pve']['tier_distribution']},
                         {'2 级': 7, '7 级': 4, '11 级': 1, '等级未知': 3})
        self.assertEqual(by_mode['pvp']['denominator'], 90)
        self.assertEqual(by_mode['oper_div']['status'], 'unknown')
        accounts = {r['mode']: {m['metric']: m['value'] for m in r['support']} for r in details['P08']['support']}
        self.assertEqual(accounts['pve']['second_battery_hits'], 111)
        self.assertEqual(accounts['oper_div']['wins_by_tasks_5'], 7)

    def test_mode_style_uses_tracked_and_weapon_specific_denominators(self):
        profile, target, record = sample()
        stats = record["payload"]["mode_results"]["pvp"]["account"]["statistics"]
        stats.update(battles_since_510=40, survived_battles=60, damage_scouting=200000,
                     art_agro=2500000, torpedo_agro=1500000, capture_points=320,
                     second_battery={"hits": 500, "shots": 1000}, torpedoes={"hits": 36, "shots": 1200},
                     aircraft={"frags": 4})
        prepared = prepare_evidence(build_career_profile_evidence(profile, target, record), "")
        details = next(row["details"] for row in prepared["payload"]["evidence"] if row["id"] == "P11")
        pvp = next(row for row in details["support"] if row["mode"] == "pvp")
        fields = {row["metric"]: row for row in pvp["support"]}
        self.assertEqual((fields["main_battery_hit_rate"]["value"], fields["main_battery_hit_rate"]["denominator"]), (.25, 1000))
        self.assertEqual(fields["second_battery_hit_rate"]["value"], .5)
        self.assertEqual(fields["torpedo_hit_rate"]["value"], .03)
        self.assertEqual(fields["survival_rate"]["value"], .6)
        self.assertEqual((fields["avg_scouting_damage"]["value"], fields["avg_scouting_damage"]["coverage_ratio"]), (5000, .4))
        self.assertEqual(fields["avg_potential_damage"]["value"], 100000)
        self.assertEqual(fields["avg_capture_points"]["value"], 8)
        self.assertNotIn("aircraft_hit_rate", fields)
        self.assertNotIn("damage_received_to_dealt_ratio", fields)
        self.assertIn("实际承伤／造伤比", pvp["unknown"])

    def test_class_style_pairs_actual_damage_fields_and_weights_hits(self):
        profile, target, record = sample()
        pvp = record["payload"]["mode_results"]["pvp"]
        pvp["account"]["statistics"].update(damage_received=14451000)
        record["payload"]["rating_references"] = {"local_catalog": {
            "1": {"tier": 8, "species": "Cruiser"},
            "2": {"tier": 10, "species": "Cruiser"},
            "3": {"tier": 10, "species": "Cruiser"},
        }}
        pvp["ships"]["ships"] = [
            {"ship_id": "1", "statistics": {"battles": 25, "damage_received": 90000, "damage_dealt": 30000,
                                             "main_battery": {"hits": 10, "shots": 100}}},
            {"ship_id": "2", "statistics": {"battles": 65, "damage_dealt": 500000,
                                             "main_battery": {"hits": 450, "shots": 900}}},
            {"ship_id": "3", "statistics": {"battles": 10, "damage_received": 80000,
                                             "main_battery": {"hits": 2, "shots": 1}}},
        ]
        prepare = prepare_evidence(build_career_profile_evidence(profile, target, record), "")
        rows = {row["id"]: row["details"] for row in prepare["payload"]["evidence"]}
        measurement = {m["metric"]: m for m in rows["P11"]["support"][0]["support"]}
        self.assertEqual(measurement["damage_received_to_dealt_ratio"]["value"], 2)
        class_row, = rows["P12"]["support"]
        self.assertEqual((class_row["mode"], class_row["species"], class_row["count"]), ("pvp", "巡洋舰", 100))
        self.assertEqual({row["label"]: row["share"] for row in class_row["tier_distribution"]}, {"8 级": .25, "10 级": .75})
        class_fields = {row["metric"]: row for row in class_row["support"]}
        ratio = class_fields["damage_received_to_dealt_ratio"]
        self.assertEqual((ratio["numerator"], ratio["denominator"], ratio["value"], ratio["coverage_ratio"]),
                         (90000, 30000, 3, .25))
        hit = class_fields["main_battery_hit_rate"]
        self.assertEqual((hit["numerator"], hit["denominator"], hit["value"], hit["battle_count"]),
                         (460, 1000, .46, 90))

    def test_regional_baseline_weights_match_ship_mix_and_tracking_scope(self):
        profile, target, record = sample()
        entry = record['payload']['mode_results']['pvp']
        entry['account']['statistics']['battles_since_510'] = 90
        first, second = entry['ships']['ships']
        first['statistics'].update(frags=80, damage_scouting=2_000_000, battles_since_510=50)
        second['statistics'].update(frags=3, damage_dealt=400_000, damage_scouting=100_000, battles_since_510=10)
        reference = {'mode': 'pvp', 'version': '0.15.9', 'provider': 'Ship Tool', 'snapshot_date': '2026-11-05',
                     'population': 'test', 'fields': ['games', 'damage', 'frags', 'games_510', 'spot'],
                     'regions': {'asia': {'source_url': 'https://shiptool.st/', 'ships': {
                         first['ship_id']: [100, 1_000_000, 60, 50, 1_000_000],
                         second['ship_id']: [20, 1_200_000, 7, 10, 50_000]}},
                         'na': {'source_url': 'https://shiptool.st/', 'ships': {
                             first['ship_id']: [250, 4_000_000, 300, 11, 100]}}}}
        with patch('wows_training.career_profile_evidence._regional_random_sample', return_value=reference):
            result = prepare_evidence(build_career_profile_evidence(profile, target, record), '')
            details = next(r['details'] for r in result['payload']['evidence'] if r['id'] == 'P10')
        self.assertEqual((details['status'], details['region'], details['mode']), ('available', 'asia', 'pvp'))
        values = {r['metric']: r for r in details['support']}
        damage, scout = values['avg_damage'], values['avg_scouting_damage']
        self.assertEqual((damage['value'], damage['reference_value'], damage['count'], damage['denominator'], damage['battle_count']),
                         (71111.11, 15555.56, 90, 100, 120))
        self.assertEqual((scout['value'], scout['reference_value'], scout['count'], scout['denominator'], scout['battle_count']),
                         (35000, 17500, 60, 90, 60))
        self.assertEqual(values['avg_frags']['reference_value'], .57)

    def test_profile_response_uses_career_contract_and_validates_every_reference(self):
        prepared = prepare_evidence(build_career_profile_evidence(*sample()), "")
        self.assertEqual(prepared["payload"]["report_contract"], CAREER_PROFILE_CONTRACT)
        prompt = tactical_system_prompt(contract=CAREER_PROFILE_CONTRACT)
        self.assertIn("随机战单排、双排和三排", prompt)
        self.assertIn(career_profile_skill()["body"], prompt)
        self.assertNotIn("战术图", prompt)
        response = {
            "observations": [
                {"text": "该快照显示巡洋舰场次最多。", "evidence_ids": ["P04"]},
                {"text": "主炮提供了可观察的伤害贡献。", "evidence_ids": ["P02"]},
                {"text": "积累主要来自随机战单排。", "evidence_ids": ["P03"]}],
            "interpretations": [{"text": "随机战参战积累集中在巡洋舰。", "evidence_ids": ["P04", "P05"]}],
            "suggestions": [],
        }
        if any(item["id"] == "P10" and item["details"].get("status") == "available"
               for item in prepared["payload"]["evidence"]):
            response["observations"][1]["evidence_ids"].append("P10")
        valid = validate_response(prepared, json.dumps(response))
        self.assertEqual(valid["status"], "completed")
        self.assertNotIn("notice", valid)
        bad_citation = deepcopy(response)
        bad_citation["observations"][0]["evidence_ids"] = ["P99"]
        self.assertEqual(validate_response(prepared, json.dumps(bad_citation))["status"], "needs_review")
        invalid_count = {**response, "interpretations": []}
        self.assertEqual(validate_response(prepared, json.dumps(invalid_count))["status"], "needs_review")
        replay = deepcopy(prepared)
        replay["payload"]["plan"] = "wrong account"
        with self.assertRaisesRegex(AIError, "匿名快照"):
            validate_response(replay, json.dumps(response))

    def test_verified_comparison_used_for_style_only_when_available(self):
        analysis = build_career_profile_evidence(*sample())
        analysis["evidence"] = [row for row in analysis["evidence"] if row["id"] != "P10"]
        comparison = {"id": "P10", "kind": "career_server_comparison", "label": "同服同舰基准", "text": "匹配样本",
                      "details": {"status": "available", "support": [
                          {"metric": "avg_damage", "value": 8, "reference_value": 7, "difference": 1,
                           "count": 90, "denominator": 100, "battle_count": 7_421, "coverage_ratio": .9}]}}
        analysis["evidence"].append(comparison)
        answer = {"observations": [{"text": "偏好巡洋舰。", "evidence_ids": ["P04"]},
                                   {"text": "参战以主炮换血为主要可见贡献。", "evidence_ids": ["P02"]},
                                   {"text": "主要参战模式是随机战。", "evidence_ids": ["P03"]}],
                  "interpretations": [{"text": "账号以随机战巡洋舰为主。", "evidence_ids": ["P04", "P03"]}],
                  "suggestions": []}
        prepared = prepare_evidence(analysis, "")
        omitted = validate_response(prepared, json.dumps(answer))
        self.assertIn("career_comparison_not_cited", omitted["validation_errors"])
        answer["observations"][1]["evidence_ids"].append("P10")
        self.assertEqual(validate_response(prepared, json.dumps(answer))["status"], "completed")
        answer["observations"][1]["evidence_ids"].remove("P10")
        comparison["details"]["status"] = "unknown"
        self.assertEqual(validate_response(prepare_evidence(analysis, ""), json.dumps(answer))["status"], "completed")

    def test_previous_career_contract_is_still_readable(self):
        analysis = build_career_profile_evidence(*sample())
        analysis["report_contract"] = LEGACY_CAREER_PROFILE_CONTRACT
        analysis['evidence'] = [row for row in analysis['evidence'] if row['id'] in {'P01', 'P02', 'P03', 'P04', 'P05', 'P06', 'P07'}]
        response = {"observations": [{"text": "巡洋舰累计场次较多。", "evidence_ids": ["P04"]}],
                    "interpretations": [{"text": "舰种使用较集中。", "evidence_ids": ["P04"]}],
                    "suggestions": [{"text": "可关注常用舰的历史指标。", "evidence_ids": ["P06"]}]}
        self.assertEqual(validate_response(prepare_evidence(analysis, ""), json.dumps(response))["status"], "completed")


if __name__ == "__main__":
    unittest.main()
