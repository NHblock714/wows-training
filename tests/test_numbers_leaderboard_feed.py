from http.client import IncompleteRead
from io import BytesIO
import json
from pathlib import Path
import sys
from threading import Event
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wows_training import numbers_leaderboard_feed as feed


def leaderboard():
    return {
        "schema_version": 1,
        "source": "wows-numbers",
        "region": "asia",
        "mode": "pvp",
        "order": "pr_desc",
        "observed_on": "2022-04-11",
        "ships": {
            "42": {
                "source_url": "https://asia.wows-numbers.com/zh-tw/ship/42,Ship-A/",
                "complete_top10": True,
                "rows": [{"account_id": "123", "rank": 1}, {"account_id": "456", "rank": 1}],
            },
            "43": {
                "source_url": "https://asia.wows-numbers.com/ship/43%2CShip-B/?p=1&order=rating__desc#leaderboard",
                "complete_top10": False,
                "rows": [{"account_id": "123", "rank": 10}],
            },
            "44": {
                "source_url": "https://asia.wows-numbers.com/ship/44,Ship-C/",
                "complete_top10": False,
                "rows": [{"account_id": "456", "rank": 4}],
            },
        },
    }


class NumbersLeaderboardFeedTests(unittest.TestCase):
    def test_preserves_exact_ranks_and_checks_only_complete_boards(self):
        doc = leaderboard()
        rankings = feed.account_rankings(doc, "asia", 123)
        self.assertEqual(rankings["account_id"], "123")
        self.assertEqual(rankings["observed_on"], "2022-04-11")
        self.assertEqual(rankings["ship_ranks"], {"42": 1, "43": 10})
        self.assertEqual(rankings["checked_ship_ids"], ["42"])
        self.assertEqual(set(rankings["source_urls"]), {"42", "43"})
        unknown = feed.account_rankings(doc, "asia", "789")
        self.assertEqual(unknown["ship_ranks"], {})
        self.assertEqual(unknown["checked_ship_ids"], ["42"])
        self.assertEqual(set(unknown["source_urls"]), {"42"})

    def test_rejects_wrong_regions_modes_and_dates(self):
        for key, value in (("region", "eu"), ("schema_version", True), ("mode", "pvp_solo"),
                           ("order", "damage_desc"), ("source", "other"),
                           ("observed_on", "20220411"), ("observed_on", "2022-02-30"),
                           ("observed_on", "9999-01-01")):
            with self.subTest(key=key, value=value):
                document = dict(leaderboard(), **{key: value})
                with self.assertRaises(ValueError):
                    feed.account_rankings(document, "asia", "123")

    def test_rejects_wrong_ship_region_and_custom_table_urls(self):
        for url in (
            "https://na.wows-numbers.com/ship/42,Ship-A/",
            "https://asia.wows-numbers.com.evil.example/ship/42,Ship-A/",
            "http://asia.wows-numbers.com/ship/42,Ship-A/",
            "https://asia.wows-numbers.com/ship/999,Ship-A/",
            "https://asia.wows-numbers.com/ship/42,Ship-A/?order=rating__desc&p=2",
            "https://asia.wows-numbers.com/ship/42,Ship-A/?order=win_rate__desc&p=1",
            "https://asia.wows-numbers.com/ship/42,Ship-A/?order=rating__desc&p=1&search=a",
            "https://asia.wows-numbers.com/ship/42,Ship-A/?order=rating__desc&order=rating__desc",
            "https://asia.wows-numbers.com/ship/42,Ship-A/?type=solo",
            "https://asia.wows-numbers.com/ship/42,Ship-A/type,solo/",
        ):
            with self.subTest(url=url):
                doc = leaderboard()
                doc["ships"]["42"]["source_url"] = url
                with self.assertRaises(ValueError):
                    feed.account_rankings(doc, "asia", "123")

    def test_rejects_duplicate_accounts_invalid_rows_and_empty_complete_board(self):
        for rows in ([], [{"account_id": "0123", "rank": 1}], [{"account_id": 123, "rank": 1}],
                     [{"account_id": "123", "rank": True}], [{"account_id": "123", "rank": 11}],
                     [{"account_id": "123", "rank": 1}, {"account_id": "123", "rank": 2}]):
            with self.subTest(rows=rows):
                doc = leaderboard()
                doc["ships"]["42"]["rows"] = rows
                with self.assertRaises(ValueError):
                    feed.account_rankings(doc, "asia", "123")
        doc = leaderboard()
        doc["ships"]["43"]["rows"] = []
        self.assertEqual(feed.account_rankings(doc, "asia", "123")["ship_ranks"], {"42": 1})

    def test_fetch_uses_fixed_get_without_credentials_or_redirects(self):
        document = leaderboard()
        with patch.object(feed, "build_opener") as opener:
            opener.return_value.open.return_value = BytesIO(json.dumps(document).encode("utf-8"))
            self.assertEqual(feed.fetch_numbers_feed("asia"), document)
        request = opener.return_value.open.call_args.args[0]
        self.assertEqual(request.full_url,
                         "https://raw.githubusercontent.com/NHblock714/wows-training/main/data/numbers/asia.json")
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)
        self.assertFalse(request.has_header("Authorization"))
        self.assertIsNone(opener.call_args.args[0].redirect_request(None, None, 301, "", {}, "https://example.com"))
        with self.assertRaises(ValueError):
            feed.feed_url("asia/../../token")

    def test_malformed_responses_and_cancellation(self):
        with patch.object(feed, "_MAX_BYTES", 64), patch.object(feed, "build_opener") as opener:
            for raw in (b"x" * 65, b'{"dup": 1, "dup": 2}', b'{"rank": NaN}', b"\xff"):
                with self.subTest(raw=raw):
                    opener.return_value.open.return_value = BytesIO(raw)
                    with self.assertRaises(ValueError):
                        feed.fetch_numbers_feed("asia")
            cancel = Event()
            cancel.set()
            opener.reset_mock()
            with self.assertRaises(InterruptedError):
                feed.fetch_numbers_feed("asia", cancel)
            opener.assert_not_called()

    def test_bounds_the_number_of_ships_and_rows(self):
        doc = leaderboard()
        with patch.object(feed, "_MAX_SHIPS", 2):
            with self.assertRaises(ValueError):
                feed.account_rankings(doc, "asia", "123")
        with patch.object(feed, "_MAX_ROWS", 1):
            with self.assertRaises(ValueError):
                feed.account_rankings(doc, "asia", "123")

    def test_truncated_http_body_surfaces_as_transport_failure(self):
        class InterruptedResponse(BytesIO):
            def read(self, _size):
                raise IncompleteRead(b'{"ships":', 2)

        with patch.object(feed, "build_opener") as opener:
            opener.return_value.open.return_value = InterruptedResponse()
            with self.assertRaises(OSError) as failure:
                feed.fetch_numbers_feed("asia")
            self.assertIsInstance(failure.exception.__cause__, IncompleteRead)

            truncated = BytesIO(b"{}")
            truncated.length = 2
            opener.return_value.open.return_value = truncated
            with self.assertRaises(OSError):
                feed.fetch_numbers_feed("asia")


if __name__ == "__main__":
    unittest.main()
