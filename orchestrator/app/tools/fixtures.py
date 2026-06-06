"""Canned page content for FIXTURE_MODE — lets us develop and demo without
hitting live web tools.

URL matching is substring-based (cheap and works for hackathon-grade fixtures).
The seed data here mirrors what a real product page extraction would return so
the downstream agents see realistic shapes.
"""

FIXTURE_PAGES: dict[str, str] = {
    "ebay.com/itm/iphone-15-pro": """
Apple iPhone 15 Pro 256GB - Natural Titanium - Unlocked
Condition: Used - Excellent
Price: $689.00
Free shipping (USPS Priority, ~3 days)
Returns: 30-day money-back guarantee (buyer pays return)
Seller: tech_resale_pros (4,127 feedback, 99.6% positive)
Battery health: 92%
Carrier: Unlocked / GSM+CDMA
Item location: Brooklyn, NY, United States
""".strip(),
    "ebay.com/itm/iphone-15-pro-128": """
Apple iPhone 15 Pro 128GB - Blue Titanium - Unlocked
Condition: Used - Very Good
Price: $549.00
Standard shipping ($8.99, ~5 days)
Returns: 14-day money-back guarantee
Seller: bestcellsource (812 feedback, 98.9% positive)
Battery health: 90%
""".strip(),
    "ebay.com/itm/SCAM": """
iPhone 15 Pro Max 1TB BRAND NEW SEALED
Price: $299.99
Ships from: Shenzhen, China
Returns: No returns accepted
Seller: hot_deals_2026 (12 feedback, 67% positive)
""".strip(),
    "swappa.com/listing/view/iphone-15-pro": """
iPhone 15 Pro 256GB · Carrier Unlocked · Natural Titanium
Condition: Mint
Price: $715
Shipping: Free, 2-3 business days
Returns: Swappa Protection, 14-day money-back
Battery health: 94%
Buyer protection included.
""".strip(),
    "amazon.com/iphone-15-pro": """
Apple iPhone 15 Pro, 256GB, Natural Titanium - Unlocked (Renewed Premium)
Price: $759.00
FREE Returns within 90 days
Ships from and sold by Amazon Renewed.
Battery health: minimum 90% capacity guaranteed.
Known issue (per recent reviews): Some renewed units arrive with Wi-Fi 6E
intermittent disconnects after iOS 18.2 update.
""".strip(),
}


def fixture_fetch(url: str) -> str:
    """Return canned page content for a URL substring match.

    Falls through to a generic stub so callers don't have to handle None.
    """
    for needle, body in FIXTURE_PAGES.items():
        if needle in url:
            return body
    return f"[FIXTURE] No canned content for {url}; generic product page placeholder."
