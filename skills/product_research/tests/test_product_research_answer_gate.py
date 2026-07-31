from __future__ import annotations

import unittest

from skills.product_research.product_research_answer_gate import validate_report


class ProductResearchAnswerGateTest(unittest.TestCase):
    def valid_payload(self):
        return {
            "query": "닥터펠리스 하드볼",
            "generated_at": "2026-06-03T00:00:00Z",
            "sources_checked": [
                {"source": "danawa", "url": "https://search.danawa.com/x", "status": "http_200", "notes": "parsed"},
                {"source": "naver_brand", "url": "https://brand.naver.com/drfelis", "status": "http_200", "notes": "parsed"},
            ],
            "candidates": [
                {
                    "source": "naver_brand",
                    "name": "하드볼 8.3kg, 2개",
                    "url": "https://brand.naver.com/drfelis/products/1",
                    "price": 81800,
                    "conditional_price": 51100,
                    "condition": "coupon checkout unverified",
                    "shipping_fee": None,
                    "quantity_kg": 16.6,
                    "unit_price_per_100g": 307.8,
                    "confidence": "conditional_price_parsed",
                    "link_status": "direct_product_url_constructed_not_browser_verified",
                    "notes": "parsed",
                }
            ],
            "summary": {
                "unit_price_applicable": True,
                "unit_price_candidate_count": 1,
                "caveats": ["No checkout-page verification; coupon/card/membership/shipping can change final price."],
            },
        }

    def test_valid_payload_passes(self):
        self.assertEqual(validate_report(self.valid_payload()), [])

    def test_non_quantity_product_can_pass_with_unit_price_caveat(self):
        payload = self.valid_payload()
        payload["query"] = "BLDC 선풍기"
        payload["candidates"][0].update(
            {
                "name": "르젠 BLDC 선풍기",
                "price": 74210,
                "conditional_price": None,
                "condition": "",
                "quantity_kg": None,
                "unit_price_per_100g": None,
                "confidence": "marketplace_search_parsed",
            }
        )
        payload["summary"] = {
            "unit_price_applicable": False,
            "unit_price_candidate_count": 0,
            "caveats": [
                "No checkout-page verification; coupon/card/membership/shipping can change final price.",
                "Unit-price comparison was not applicable or quantity was not parsed for this product category.",
                "No coupon/member/card conditional price was parsed; check store coupon pages or checkout manually when coupon accuracy matters.",
            ],
        }
        self.assertEqual(validate_report(payload), [])

    def test_legacy_confirmed_label_fails(self):
        payload = self.valid_payload()
        payload["candidates"][0]["confidence"] = "confirmed"
        self.assertIn("overconfident legacy label 'confirmed' is not allowed", "\n".join(validate_report(payload)))

    def test_missing_unit_price_fails_when_applicable(self):
        payload = self.valid_payload()
        payload["candidates"][0]["unit_price_per_100g"] = None
        payload["summary"]["unit_price_candidate_count"] = 0
        self.assertIn("unit-price applicable but no unit-price evidence", "\n".join(validate_report(payload)))

    def test_only_excluded_priced_candidates_fail(self):
        payload = self.valid_payload()
        payload["candidates"][0]["excluded_reason"] = "candidate appears to be accessory/compatible part: 날개"
        issues = validate_report(payload)
        self.assertIn("no eligible priced candidates after accessory/low-relevance exclusions", issues)

    def test_malformed_collections_do_not_crash(self):
        issues = validate_report({"query": "x", "generated_at": "t", "sources_checked": "bad", "candidates": "bad", "summary": {}})
        self.assertIn("no candidates", issues)
        self.assertIn("fewer than two source probes", issues)


if __name__ == "__main__":
    unittest.main()
