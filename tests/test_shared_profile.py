from email.message import Message
from io import BytesIO
from pathlib import Path
import sys
from threading import Event
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wows_training.shared_profile import SharedProfileError, load_shared_profile, parse_share_link


CODE = "test-share-only-abc123"
URL = f"https://profile.worldofwarships.asia/statistics/123/ac/{CODE}/"


def response(record, account="123"):
    return {"status": "ok", "data": {account: record}}


class SharedProfileTests(unittest.TestCase):
    def test_parser_binds_https_profile_to_the_selected_account_and_region(self):
        link = parse_share_link(f" {URL}\n", "asia", 123)
        self.assertEqual((link.region, link.account_id, link.access_code), ("asia", "123", CODE))
        self.assertNotIn(CODE, repr(link))
        self.assertEqual(parse_share_link(URL.replace(".asia", ".com"), "com", "123").region, "na")
        for target, region, account in (
            (URL, "eu", 123), (URL, "asia", 456), (URL.replace("https:", "http:"), "asia", 123),
            (URL.replace(".asia", ".asia.attacker.invalid"), "asia", 123),
            (URL.replace(".asia", ".asia:443"), "asia", 123),
            (URL.replace("profile.", "other@profile."), "asia", 123),
            (URL.replace("123", "0123"), "asia", 123), (URL + "?return=example", "asia", 123),
            (URL + "#fragment", "asia", 123), (URL.replace("/ac/", "/ac/%2F"), "asia", 123),
            (URL.replace("/ac/", "/\nac/"), "asia", 123),
            ("https://profile.worldofwarships.asia/statistics/123", "asia", 123),
        ):
            with self.subTest(target=target, region=region, account=account):
                with self.assertRaises(SharedProfileError) as failure:
                    parse_share_link(target, region, account)
                self.assertNotIn(CODE, str(failure.exception))

    def test_load_shares_only_the_code_with_official_vortex_and_projects_modes(self):
        seen = []
        stats = {"battles_count": 8, "wins": 5, "survived": 4, "original_exp": 5112, "premium_exp": 6800,
                 "damage_dealt": 10000, "scouting_damage": 99, "hits_by_main": 12, "shots_by_main": 55,
                 "hits_by_tpd": 3, "shots_by_tpd": 11, "tpd_agro": 200,
                 "private_extra": CODE}

        def get(request, cancel):
            self.assertIsNone(cancel)
            url = urlsplit(request.full_url)
            self.assertEqual(url.hostname, "vortex.worldofwarships.asia")
            self.assertEqual(request.get_method(), "GET")
            self.assertEqual(request.get_header("Origin"), "https://profile.worldofwarships.asia")
            self.assertFalse(request.has_header("Cookie"))
            self.assertEqual(parse_qs(url.query), {"ac": [CODE]})
            seen.append(url.path)
            if url.path.endswith("ships/"):
                return response({"statistics": {"42": {"pvp": {"battles_count": 8, "wins": 5}}}})
            if url.path.endswith("achievements/"):
                return response({"statistics": {"achievements": {"456": {"count": 2, "extra": CODE}}}})
            return response({"name": "Captain\u202e", "created_at": 1730918424.5,
                             "statistics": {"pvp": stats, "pvp_solo": {"battles_count": 0}, "rank_info": {}}})

        value = load_shared_profile(URL, "asia", 123, transport=get)
        self.assertEqual(seen, ["/api/accounts/123/", "/api/accounts/123/ships/", "/api/accounts/123/achievements/"])
        self.assertEqual(value["nickname"], "Captain")
        self.assertEqual(value["account_created_at"], 1730918424)
        self.assertEqual(value["modes"], {
            "pvp": {"battles": 8, "wins": 5, "survived_battles": 4, "xp": 6800, "damage_dealt": 10000,
                    "damage_scouting": 99, "torpedo_agro": 200, "main_battery": {"hits": 12, "shots": 55},
                    "torpedoes": {"hits": 3, "shots": 11}},
            "pvp_solo": {"battles": 0},
        })
        self.assertEqual(value["ships"], [{"ship_id": "42", "modes": {"pvp": {"battles": 8, "wins": 5}}}])
        self.assertEqual(value["achievements"], {"456": 2})
        self.assertNotIn(CODE, repr(value))

    def test_hidden_or_wrong_account_is_a_terminal_rejection(self):
        for document in (response({"hidden_profile": True, "statistics": {"pvp": {"battles_count": 99}}}),
                         response({"statistics": {}}, account="456"),
                         response({"id": "456", "statistics": {}})):
            with self.subTest(document=document), patch("wows_training.shared_profile._request_json", return_value=document) as transport:
                with self.assertRaises(SharedProfileError):
                    load_shared_profile(URL, "asia", 123)
                transport.assert_called_once()

    def test_optional_failures_remain_unknown_and_reflected_tokens_are_removed(self):
        with patch("wows_training.shared_profile._request_json", side_effect=[
            response({"name": CODE, "statistics": {"pvp": {"battles_count": 5}}}),
            ValueError(URL), HTTPError(URL, 403, URL, None, None),
        ]):
            value = load_shared_profile(URL, "asia", 123)
        self.assertEqual(value["nickname"], "123")
        self.assertIsNone(value["ships"])
        self.assertIsNone(value["achievements"])
        self.assertNotIn(CODE, repr(value))

    def test_network_and_redirect_errors_do_not_disclose_the_url(self):
        headers = Message()
        headers["Location"] = f"https://other.invalid/{CODE}"
        for error in (HTTPError(URL, 302, URL, headers, BytesIO()), HTTPError(URL, 403, URL, None, None),
                      OSError(URL), ValueError(URL)):
            with self.subTest(error=type(error)), patch("wows_training.shared_profile.build_opener") as opener:
                opener.return_value.open.side_effect = error
                with self.assertRaises(SharedProfileError) as caught:
                    load_shared_profile(URL, "asia", 123)
                opener.return_value.open.assert_called_once()
                self.assertIsNone(opener.call_args.args[0].redirect_request(None, None, 302, "", headers, headers["Location"]))
                self.assertNotIn(CODE, str(caught.exception))
                self.assertTrue(caught.exception.__suppress_context__)

    def test_cancelled_lookup_makes_no_requests(self):
        cancelled = Event()
        cancelled.set()
        with patch("wows_training.shared_profile._request_json") as transport:
            with self.assertRaises(InterruptedError):
                load_shared_profile(URL, "asia", 123, cancel=cancelled)
        transport.assert_not_called()
