"""商品卡片接收解析回归，只用合成消息，不请求闲鱼或发送消息。"""
from __future__ import annotations

import base64
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.api.routes.chat_new import _parse_message
from app.services.chat_new.push_message_parser import PushMessageParser
from common.utils.xianyu_item_card import normalize_item_card, parse_item_card
from common.utils.xianyu_message_parser import decode_first_content, interpret_content, parse_content_payloads


def item(**updates):
    result = {"itemId": 9007199254740993, "title": "手机支架", "price": "¥1.00",
              "mainPic": "http://img.alicdn.com/synthetic-card.jpg"}
    result.update(updates)
    return result


def content(raw_item=None):
    return {"contentType": 7, "atUsers": [], "itemCard": {"item": item() if raw_item is None else raw_item},
            "itemTip": "合成测试商品"}


def payload(decoded, encoded=False):
    text = json.dumps(decoded, ensure_ascii=False)
    return base64.b64encode(text.encode()).decode() if encoded else text


def history(decoded, encoded=True):
    return {"message": {"messageId": "synthetic-message", "createAt": 1794100000000,
                        "extension": {"senderUserId": "seller@goofish", "reminderTitle": "合成卖家"},
                        "content": {"custom": {"data": payload(decoded, encoded), "summary": "[宝贝]"}}}}


def push(decoded, encoded=False, standard=True):
    msg = {"1": {"1": "seller@goofish"}, "2": "synthetic-chat@goofish", "3": "synthetic-message",
           "5": 1794100000000, "6": {"3": {"1" if encoded else "5": payload(decoded, encoded), "2": "旧卡片占位"}}}
    if standard:
        msg["10"] = {"senderUserId": "seller@goofish", "senderNick": "合成卖家", "reminderContent": "[宝贝]"}
    return {"1": msg}


class ItemCardNormalizationTests(unittest.TestCase):
    def test_exact_safe_card_and_large_identifier_stays_string(self):
        card = normalize_item_card(item(url="javascript:alert(1)"))
        self.assertEqual(card, {
            "itemId": "9007199254740993", "title": "手机支架", "price": "¥1.00",
            "mainPic": "http://img.alicdn.com/synthetic-card.jpg",
            "url": "https://www.goofish.com/item?id=9007199254740993",
        })

    def test_invalid_ids_are_rejected_and_bigint_max_accepted(self):
        for value in (None, True, False, 0, -1, 1.0, "1.0", "01", "+1", "1?x=2", "١٢٣", "9223372036854775808"):
            with self.subTest(value=value):
                self.assertIsNone(normalize_item_card(item(itemId=value)))
        self.assertEqual(normalize_item_card(item(itemId="9223372036854775807"))["itemId"], "9223372036854775807")
        self.assertIsNone(normalize_item_card(item(itemId=10 ** 5000)))

    def test_price_formats_are_decimal_and_zero_is_valid(self):
        for value, expected in (("￥ 8.8", "¥8.80"), ("1", "¥1.00"), (0, "¥0.00"), (8.88, "¥8.88"), ("-0", "¥0.00")):
            with self.subTest(value=value):
                self.assertEqual(normalize_item_card(item(price=value))["price"], expected)
        for value in (None, True, "", "¥", "-1", "NaN", "Infinity", "8.888", "10000000000", {}):
            with self.subTest(value=value):
                self.assertEqual(normalize_item_card(item(price=value))["price"], "")
        self.assertEqual(normalize_item_card(item(price=10 ** 5000))["price"], "")

    def test_unsafe_image_urls_are_removed_and_relative_protocol_is_normalized(self):
        invalid = ["javascript:alert(1)", "data:image/png;base64,test", "file:///C:/secret", "ftp://host/a",
                   "https://user:pass@example.com/a", "https://user@example.com/a", "https:\\evil.com/a",
                   "https://example.com/a\n", "ht\ttps://example.com/a", "https://example.com:bad/a",
                   "https://example.com:65536/a", "/local.png", "https:///no-host", "https://example.com/a b"]
        for url in invalid:
            with self.subTest(url=url):
                self.assertEqual(normalize_item_card(item(mainPic=url))["mainPic"], "")
        self.assertEqual(normalize_item_card(item(mainPic="//img.alicdn.com/a.png"))["mainPic"], "https://img.alicdn.com/a.png")

    def test_incomplete_card_stays_safe_and_does_not_mutate_input(self):
        raw = {"itemId": "123", "title": {}, "mainPic": {"url": "https://unsafe-shape"}}
        before = json.loads(json.dumps(raw))
        card = normalize_item_card(raw)
        self.assertEqual(raw, before)
        self.assertEqual(card, {"itemId": "123", "title": "", "price": "", "mainPic": "", "url": "https://www.goofish.com/item?id=123"})


class ItemCardMessageTests(unittest.TestCase):
    def setUp(self):
        self.parser = PushMessageParser("synthetic-account", "seller")

    def test_common_tuple_contract_and_plain_base64_payloads(self):
        decoded = content()
        expected = ("[宝贝] 手机支架", [], "item")
        self.assertEqual(interpret_content(decoded), expected)
        self.assertEqual(len(interpret_content(decoded)), 3)
        for encoded in (False, True):
            raw = payload(decoded, encoded)
            self.assertEqual(decode_first_content([raw]), decoded)
            self.assertEqual(parse_content_payloads([raw]), expected)

    def test_realtime_standard_legacy_and_no_reminder_card_match_history(self):
        expected = parse_item_card(content())
        for encoded in (False, True):
            historical = _parse_message(history(content(), encoded), "seller")
            for standard in (False, True):
                with self.subTest(encoded=encoded, standard=standard):
                    event = self.parser.parse(push(content(), encoded, standard))
                    live = event["message"]
                    self.assertEqual(event["cid"], "synthetic-chat")
                    for message in (live, historical):
                        self.assertEqual(message["type"], "item")
                        self.assertEqual(message["text"], "[宝贝] 手机支架")
                        self.assertEqual(message["images"], [])
                        self.assertEqual(message["itemCard"], expected)
                        self.assertEqual(message["messageId"], "synthetic-message")
                        self.assertTrue(message["isSelf"])

    def test_malformed_cards_keep_placeholder_without_crashing(self):
        invalid = [{"contentType": 7}, {"contentType": 7, "itemCard": []},
                   {"contentType": 7, "itemCard": {"item": "bad"}}, content(item(itemId="javascript:bad"))]
        for decoded in invalid:
            with self.subTest(decoded=decoded):
                self.assertEqual(interpret_content(decoded), ("[宝贝]", [], "item"))
                for message in (_parse_message(history(decoded), "seller"), self.parser.parse(push(decoded))["message"]):
                    self.assertEqual(message["type"], "item")
                    self.assertEqual(message["text"], "[宝贝]")
                    self.assertNotIn("itemCard", message)

    def test_safe_fields_survive_unsafe_link_and_image_in_history_and_push(self):
        decoded = content(item(mainPic="javascript:alert(1)", url="https://evil.invalid/", title="普通商品"))
        for message in (_parse_message(history(decoded), "seller"), self.parser.parse(push(decoded))["message"]):
            self.assertEqual(message["itemCard"]["mainPic"], "")
            self.assertEqual(message["itemCard"]["url"], "https://www.goofish.com/item?id=9007199254740993")
            self.assertEqual(message["text"], "[宝贝] 普通商品")

    def test_existing_text_image_voice_and_generic_cards_keep_their_contract(self):
        cases = [({"contentType": 1, "text": {"text": "测试文本"}}, ("测试文本", [], "text")),
                 ({"contentType": 2, "image": {"pics": [{"url": "https://example.com/test.png"}]}}, ("", ["https://example.com/test.png"], "image")),
                 ({"contentType": 3, "audio": {}}, ("[语音消息]", [], "text"))]
        for decoded, expected in cases:
            with self.subTest(decoded=decoded):
                self.assertEqual(interpret_content(decoded), expected)
                message = _parse_message(history(decoded), "seller")
                self.assertEqual((message["text"], message["images"], message["type"]), expected)
                self.assertNotIn("itemCard", message)
        generic = _parse_message(history({"title": "普通交易卡片"}), "seller")
        self.assertEqual((generic["type"], generic["text"]), ("card", "普通交易卡片"))
        self.assertEqual(self.parser._decode_content({}), ("", [], "text"))


if __name__ == "__main__":
    unittest.main()
