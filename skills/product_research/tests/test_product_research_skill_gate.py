from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SKILL_PATH = ROOT / "skills" / "product_research" / "SKILL.md"
SCRIPT_PATH = ROOT / "skills" / "product_research" / "product_researcher.py"


class ProductResearchSkillGateTest(unittest.TestCase):
    def test_skill_metadata_contains_korean_shopping_triggers(self) -> None:
        text = SKILL_PATH.read_text(encoding="utf-8")
        frontmatter = text.split("---", 2)[1]
        description_match = re.search(r"description:\s*(.*)", frontmatter)
        self.assertIsNotNone(description_match)
        description = description_match.group(1)
        required_triggers = [
            "추천제품",
            "제품 추천",
            "모델명",
            "최저가",
            "가성비",
            "쿠폰",
            "행사",
            "공식몰",
            "브랜드스토어",
            "네이버쇼핑",
            "다나와",
            "쿠팡",
            "가격비교",
            "단가",
            "리뷰",
            "구매 링크",
        ]
        missing = [trigger for trigger in required_triggers if trigger not in description]
        self.assertEqual(missing, [], f"Missing product-research trigger terms: {missing}")

    def test_skill_body_requires_confidence_and_conditional_price_labels(self) -> None:
        text = SKILL_PATH.read_text(encoding="utf-8")
        required_phrases = [
            "official/brand store",
            "normal visible price",
            "conditional coupon/member/card price",
            "unit price",
            "official_store_parsed",
            "marketplace_search_parsed",
            "conditional_price_parsed",
            "search_path_only",
            "fetch_failed",
            "stable official-store/search URL",
            "accessory/compatible-part",
            "호환",
            "어댑터",
        ]
        missing = [phrase for phrase in required_phrases if phrase not in text]
        self.assertEqual(missing, [], f"Missing product-research workflow phrases: {missing}")

    def test_helper_exposes_research_confidence_contract(self) -> None:
        text = SCRIPT_PATH.read_text(encoding="utf-8")
        required_terms = [
            "conditional_price",
            "unit_price_per_100g",
            "confidence",
            "stable_search_urls",
            "naver_brand",
            "danawa",
            "official_store_parsed",
            "marketplace_search_parsed",
            "conditional_price_parsed",
            "search_path_only",
            "fetch_failed",
            "excluded_reason",
            "accessory_exclusion_reason",
        ]
        missing = [term for term in required_terms if term not in text]
        self.assertEqual(missing, [], f"Missing helper contract terms: {missing}")


if __name__ == "__main__":
    unittest.main()
