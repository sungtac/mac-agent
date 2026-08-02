from __future__ import annotations

import unittest
import json
import tempfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import patch

from skills.product_research.product_researcher import (
    ProductCandidate,
    extract_prices_excluding_shipping,
    infer_shipping_fee,
    parse_danawa_html,
    parse_naver_brand_html,
    parse_quantity_kg,
    rank_candidates,
    unit_price_per_100g,
    accessory_exclusion_reason,
)


class ProductResearcherTest(unittest.TestCase):
    def test_fetch_rejects_local_destinations(self):
        with self.assertRaises(ValueError):
            from skills.product_research.product_researcher import fetch_text
            fetch_text("http://127.0.0.1:9/internal")

    def test_fetch_rejects_url_credentials(self):
        with self.assertRaises(ValueError):
            from skills.product_research.product_researcher import fetch_text
            fetch_text("https://user:password@example.com/products")

    def test_fetch_rejects_hosts_outside_research_allowlist(self):
        with self.assertRaises(ValueError):
            from skills.product_research.product_researcher import fetch_text
            fetch_text("https://example.com/products")

    def test_brand_scrape_rejects_untrusted_host(self):
        from skills.product_research.product_researcher import scrape_naver_brand
        candidates, probe = scrape_naver_brand("https://example.com/store", ["제품"])
        self.assertEqual(probe.status, "url_rejected")
        self.assertEqual(candidates[0].confidence, "fetch_failed")

    def test_rejected_brand_url_does_not_echo_url_credentials(self):
        from skills.product_research.product_researcher import scrape_naver_brand
        candidates, probe = scrape_naver_brand("https://user:password@example.com/store", ["제품"])
        self.assertNotIn("password", candidates[0].url)
        self.assertNotIn("password", probe.url)

    def test_fetch_rejects_redirect_to_local_destination(self):
        from skills.product_research import product_researcher

        class RedirectHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:9/internal")
                self.end_headers()

            def log_message(self, *_args):
                return

        server = HTTPServer(("127.0.0.1", 0), RedirectHandler)
        Thread(target=server.handle_request, daemon=True).start()
        original_validate = product_researcher._validate_public_http_url
        calls = 0

        def validate_after_initial(url, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return None
            return original_validate(url, **kwargs)

        try:
            with patch.object(product_researcher, "_validate_public_http_url", side_effect=validate_after_initial):
                with self.assertRaises(ValueError):
                    product_researcher.fetch_text(f"http://127.0.0.1:{server.server_port}/redirect")
        finally:
            server.server_close()

    def test_parse_quantity_kg_single(self):
        self.assertEqual(parse_quantity_kg("하드볼 8.3kg, 1개"), 8.3)

    def test_parse_quantity_kg_count_comma(self):
        self.assertEqual(parse_quantity_kg("하드볼 8.3kg, 4개"), 33.2)

    def test_parse_quantity_kg_count_x(self):
        self.assertEqual(parse_quantity_kg("미친모래 6.3kg X 3개"), 18.9)

    def test_unit_price_per_100g(self):
        self.assertEqual(unit_price_per_100g(51100, 16.6), 307.8)

    def test_rank_prefers_lower_unit_price(self):
        a = ProductCandidate(source="a", name="A", url="u", price=1000, quantity_kg=1, unit_price_per_100g=100)
        b = ProductCandidate(source="b", name="B", url="u", price=900, quantity_kg=0.5, unit_price_per_100g=180)
        self.assertEqual(rank_candidates([b, a])[0].name, "A")

    def test_shipping_fee_should_not_be_considered_lower_than_product_price(self):
        text = "닥터펠리스 하드볼 4.3kg 4개 66,190 원 배송비 3,000원"
        self.assertEqual(extract_prices_excluding_shipping(text)[0], 66190)
        self.assertEqual(infer_shipping_fee(text), 3000)

    def test_parse_danawa_fixture_uses_conservative_confidence(self):
        html = 'prod_main_info"> 닥터펠리스 하드볼 4.3kg 4개 66,190 원 배송비 3,000원 <div class="x">'
        candidates = parse_danawa_html("닥터펠리스 하드볼", "https://search.danawa.com/dsearch.php?query=x", html)
        self.assertEqual(candidates[0].price, 66190)
        self.assertEqual(candidates[0].shipping_fee, 3000)
        self.assertEqual(candidates[0].confidence, "marketplace_search_parsed")
        self.assertEqual(candidates[0].link_status, "stable_search_url_only")

    def test_accessory_result_is_excluded_when_query_did_not_ask_for_part(self):
        reason = accessory_exclusion_reason("르젠 LZEF-DC02 선풍기", "[호환] 르젠 LZEF-DC02 사용 선풍기 날개")
        self.assertIn("accessory", reason)

    def test_accessory_result_not_excluded_when_query_asks_for_part(self):
        reason = accessory_exclusion_reason("르젠 LZEF-DC02 선풍기 날개", "[호환] 르젠 LZEF-DC02 사용 선풍기 날개")
        self.assertEqual(reason, "")

    def test_parse_danawa_fixture_marks_accessory_candidate(self):
        html = 'prod_main_info"> [호환] 호환 르젠 LZEF-DC02 사용 선풍기 날개 9,900 원 무료배송 <div class="x">'
        candidates = parse_danawa_html("르젠 LZEF-DC02 선풍기", "https://search.danawa.com/dsearch.php?query=x", html)
        self.assertIn("accessory", candidates[0].excluded_reason)

    def test_parse_naver_brand_fixture_extracts_conditional_price(self):
        fixture = (
            '하드볼 {"productNo":11329531694,"name":"김명철 미야옹철 수의사 고양이모래 벤토나이트 카사바 카사벤토 하드볼 8.3kg, 2개",'
            '"salePrice":81800,"discountedSalePrice":51100,"mobileDiscountedSalePrice":51100}'
        )
        candidates = parse_naver_brand_html("https://brand.naver.com/drfelis", ["하드볼"], fixture)
        self.assertEqual(candidates[0].conditional_price, 51100)
        self.assertEqual(candidates[0].unit_price_per_100g, 307.8)
        self.assertEqual(candidates[0].confidence, "conditional_price_parsed")
        self.assertIn("checkout", candidates[0].condition)

    def test_report_writes_are_atomic_and_owner_only(self):
        from skills.product_research import product_researcher

        payload = {
            "query": "sample",
            "sources_checked": [],
            "summary": {
                "candidate_count": 0,
                "priced_candidate_count": 0,
                "conditional_price_count": 0,
                "excluded_candidate_count": 0,
                "unit_price_applicable": False,
                "unit_price_candidate_count": 0,
                "caveats": [],
            },
            "candidates": [],
        }
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "report.md"
            product_researcher.write_markdown(path, payload)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertIn("Product Research Report", path.read_text(encoding="utf-8"))

            json_path = Path(td) / "report.json"
            product_researcher._atomic_write_text(json_path, json.dumps(payload) + "\n")
            self.assertEqual(json_path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
