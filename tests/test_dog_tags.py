import base64
from email.message import Message
from gzip import compress
from io import BytesIO
import json
from threading import Event
import time
import unittest
from unittest.mock import Mock, patch
from urllib.request import Request

from wows_training import dog_tags


_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
_CDN = "//wows-gloss-icons.wgcdn.co/icons/dog_tags/medium/"
_PROFILE = "https://vortex.worldofwarships.asia/api/accounts/123/"
_GLOSSARY = "https://vortex.worldofwarships.asia/api/graphql/glossary/"


def _account(tag, **extra):
    return {"status": "ok", "data": {"123": {"dog_tag": tag, **extra}}}


class DogTagTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(dog_tags, "_glossaries", {})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_fetches_only_official_layers_in_browser_order_and_caches_glossary(self):
        tag = {"symbol_id": 7, "background_id": 8, "background_color_id": 9,
               "border_color_id": 10, "texture_id": 11}
        components = [
            {"id": "7", "icons": {"medium": _CDN + "symbol.png"}},
            {"id": "8", "isColorizable": True, "showClanTag": True, "clanTag": {"y": 18}},
            {"id": "9", "color": "0x2048A1"},
            {"id": "10", "color": "0x753614"},
            {"id": "11", "textureData": [{"id": "8",
                 "background": {"medium": _CDN + "PCNA004/texture.png"},
                 "border": {"medium": _CDN + "PCNA004/border.png"}}]},
        ]
        seen = []

        def read(request, _limit, media_type, _cancel, _deadline):
            url = request.full_url
            seen.append(url)
            self.assertFalse(request.has_header("Authorization"))
            self.assertFalse(request.has_header("Cookie"))
            if url == _PROFILE:
                self.assertEqual(request.get_method(), "GET")
                return json.dumps(_account(tag)).encode()
            if url == _GLOSSARY:
                self.assertEqual(request.get_method(), "POST")
                self.assertIn("dogTagComponents", json.loads(request.data)["query"])
                return json.dumps({"data": {"dogTagComponents": components}}).encode()
            self.assertIn(url, ["https:" + _CDN + suffix for suffix in
                               ("PCNA004/texture.png", "PCNA004/border.png", "symbol.png")])
            self.assertEqual(media_type, "image/png")
            return _PNG

        with patch.object(dog_tags, "_read_bytes", side_effect=read):
            layers = dog_tags.fetch_dog_tag("asia", 123)
            self.assertEqual(layers, dog_tags.fetch_dog_tag("asia", "123"))
        self.assertEqual(layers, (
            dog_tags.DogTagLayer("background", _PNG, "#2048a1"),
            dog_tags.DogTagLayer("border", _PNG, "#753614"),
            dog_tags.DogTagLayer("symbol", _PNG, position_y=1.0),
        ))
        self.assertEqual(seen[:5], [_PROFILE, _GLOSSARY, "https:" + _CDN + "PCNA004/texture.png",
                                    "https:" + _CDN + "PCNA004/border.png", "https:" + _CDN + "symbol.png"])
        self.assertEqual(seen.count(_GLOSSARY), 1)

    def test_standalone_symbol_has_no_invented_background(self):
        profile = json.dumps(_account({"symbol_id": 7, "background_id": 0, "texture_id": 0})).encode()
        glossary = json.dumps({"data": {"dogTagComponents": [{"id": "7", "type": "patch",
                               "icons": {"medium": _CDN + "patch.png"}}]}}).encode()
        with patch.object(dog_tags, "_read_bytes", side_effect=(profile, glossary, _PNG)) as reader:
            self.assertEqual(dog_tags.fetch_dog_tag("asia", 123), (dog_tags.DogTagLayer("symbol", _PNG),))
        self.assertEqual(reader.call_count, 3)

    def test_hidden_or_wrong_account_never_queries_for_assets(self):
        for response in (_account({"symbol_id": 7}, hidden_profile=True),
                         _account({"symbol_id": 7}, id=582),
                         {"status": "ok", "data": {"582": {"dog_tag": {"symbol_id": 7}}}}):
            with self.subTest(response=response), patch.object(dog_tags, "_read_bytes") as reader:
                reader.return_value = json.dumps(response).encode()
                self.assertIsNone(dog_tags.fetch_dog_tag("asia", "123"))
                reader.assert_called_once()
                self.assertEqual(reader.call_args.args[0].full_url, _PROFILE)

    def test_invalid_ids_and_untrusted_asset_links_do_not_trigger_http(self):
        with patch.object(dog_tags, "_read_bytes") as reader:
            self.assertIsNone(dog_tags.fetch_dog_tag("elsewhere", 123))
            self.assertIsNone(dog_tags.fetch_dog_tag("asia", "123?secret=x"))
            for url in ("https://unknown.test/icons/dog_tags/medium/patch.png",
                        "https://wows-gloss-icons.wgcdn.co@unknown.test/icons/dog_tags/medium/patch.png",
                        "http:" + _CDN + "patch.png", _CDN + "%2e%2e/patch.png", _CDN + "patch.png?q=1"):
                with self.subTest(url=url), self.assertRaises(ValueError):
                    dog_tags._layer_png(url, None, time.monotonic() + 1)
            reader.assert_not_called()

    def test_cancellation_interrupts_without_ejecting_the_workbench(self):
        cancel = Event()
        cancel.set()
        with patch.object(dog_tags, "build_opener") as opener:
            with self.assertRaises(InterruptedError):
                dog_tags.fetch_dog_tag("asia", "123", cancel)
            opener.assert_not_called()

    def test_gzip_body_is_decoded_with_an_independent_inflated_size_bound(self):
        payload = b"z" * 800
        compressed = compress(payload)
        for limit in (100, 1024):
            with self.subTest(limit=limit), patch.object(dog_tags, "build_opener") as opener:
                response = Mock(length=None, headers=Message())
                response.headers["Content-Type"] = "application/json"
                response.headers["Content-Encoding"] = "gzip"
                response.read1.side_effect = BytesIO(compressed).read1
                response.__enter__ = Mock(return_value=response)
                response.__exit__ = Mock(return_value=False)
                opener.return_value.open.return_value = response
                if limit < len(payload):
                    with self.assertRaisesRegex(ValueError, "Decoded"):
                        dog_tags._read_bytes(Request(_PROFILE), limit, "application/json", None,
                                             time.monotonic() + 1)
                else:
                    self.assertEqual(dog_tags._read_bytes(Request(_PROFILE), limit, "application/json", None,
                                                          time.monotonic() + 1), payload)
