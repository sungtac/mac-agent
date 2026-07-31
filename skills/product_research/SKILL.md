---
name: product-research
description: Research purchasable products and shopping decisions before recommendations: 추천제품, 제품 추천, 모델명, 최저가, 가성비, 쿠폰, 행사, 공식몰/브랜드스토어, 네이버쇼핑, 다나와, 쿠팡, 가격비교, 단가, 리뷰, 구매 링크, stable links, exact models, prices, coupons, official stores, marketplaces, and unit prices.
---

# Product Research

Use this skill when the user asks for product recommendations, exact model links, cheapest options, coupons, official-store deals, or price comparisons. Korean trigger examples include: `추천해줘`, `추천제품`, `모델 알려줘`, `최저가`, `링크 찾아줘`, `가성비`, `쿠폰`, `행사`, `공식스토어`, `네이버가 더 싼 것 같은데`, `단가 비교`, `리뷰 좋은 거`.

## Auto-click rule

If the request involves exact model names, prices, coupons, events, official stores, lowest price, unit price, or purchase links, run full research before making a recommendation unless the user explicitly says not to search.

Default command:

```bash
python3 "$HOME/mac-agent/skills/product_research/product_researcher.py" '<query>' \
  --out "$HOME/mac-agent/audit_reports/product_research_<slug>.json" \
  --json
python3 "$HOME/mac-agent/skills/product_research/product_research_answer_gate.py" "$HOME/mac-agent/audit_reports/product_research_<slug>.json"
```

For official/brand stores:

```bash
python3 "$HOME/mac-agent/skills/product_research/product_researcher.py" '<query>' \
  --naver-brand-url 'https://brand.naver.com/<store>' \
  --keyword '<product keyword>' \
  --out "$HOME/mac-agent/audit_reports/product_research_<slug>.json" \
  --json
python3 "$HOME/mac-agent/skills/product_research/product_research_answer_gate.py" "$HOME/mac-agent/audit_reports/product_research_<slug>.json"
```

Do not present a final recommendation as researched if the answer gate fails. Say what is blocked or downgrade to a non-price-verified general suggestion.

## Required workflow

1. Verify exact product identity before recommending.
   - Do not guess model numbers.
   - Confirm product name from at least one live source.
2. Check multiple source types when price matters:
   - official/brand store when available
   - Naver Shopping/search path
   - Danawa or another price-comparison page
   - Coupang/open-market path when relevant
3. Separate prices clearly:
   - normal visible price
   - conditional coupon/member/card price
   - shipping fee when visible
   - unit price for different pack sizes
4. Label confidence conservatively:
   - `official_store_parsed`: official/brand store data parsed, checkout not verified
   - `marketplace_search_parsed`: marketplace/search-result price parsed, seller/option/checkout not verified
   - `conditional_price_parsed`: coupon/member/card/mobile price parsed but eligibility not verified
   - `search_path_only`: stable search/store path only; no product-price proof
   - `fetch_failed`: source failed or blocked
5. Exclude accessory/compatible-part results when the user asked for the main product.
   - Examples: 호환, 날개, 부품, 리모컨, 커버, 어댑터, 케이블, 충전기, 전용, 보호회로.
   - If only accessory results are found, do not recommend them as the main product; report the blocker and try a broader/stable search path.
6. If direct product URLs may fail, give a stable official-store/search URL plus exact search term.
7. When the user corrects a price/link, widen the source scope instead of defending the first result.

## Final answer shape

- Research mode used and timestamp/source caveat
- Best pick by use case: cheapest / value / quality-first / trial
- Confirmed parsed price vs conditional price
- Coupon/event/card/member caveat
- Unit price math
- Link or stable search path
- Exclusions and remaining uncertainty

## Quality follow-up

Run `python3 scripts/skill_quality_audit.py --skill <this-skill> --run-tests --json` after creating or changing this skill.
