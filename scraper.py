"""
Depop sold-listings scraper — Step 1 (broad crawl + brand leaderboard)

What this does:
  Crawls SOLD listings across general Depop categories/browse pages
  (not tied to any single brand — we don't know the trending brands
  in advance, that's the whole point), extracts brand + listing details
  from each one, and tallies counts into a top-15 brand leaderboard.
  Every listing is kept (grouped by brand) so a later UI step can do the
  "click a brand -> see its images/captions/listings" drill-down straight
  from this output, no extra scraping needed.

Output shape (written to --out):
  {
    "generated_at": "...",
    "leaderboard": [ {"brand": "Nike", "sales_count": 214}, ... top 15 ... ],
    "listings_by_brand": {
      "Nike": [ {title, price, currency, image_url, listing_url, sold}, ... ],
      ...
    }
  }

Why Playwright and not requests/BeautifulSoup:
  Depop is a JS-rendered React/Next.js app behind Cloudflare bot protection.
  Plain HTTP requests will almost always get blocked or return an empty
  shell page. A real (headless) browser is needed to render the page and
  look like an actual visitor.

Setup (run locally, not in this sandbox):
    pip install playwright
    playwright install chromium

Usage:
    python scraper.py --max-items 500 --out sold_trends.json

NOTE ON BRAND EXTRACTION:
  Depop's search UI has a brand *filter*, which strongly implies brand is
  a real structured field somewhere in their listing data — not something
  you have to guess from the title. This script tries that structured
  path first (via the embedded __NEXT_DATA__ JSON). Since I can't inspect
  the live payload from this sandbox, that extraction is a marked TODO —
  run with --debug, open the dumped HTML/JSON, find the real field path,
  and fill it in. Until then, it falls back to matching listing titles
  against a seed list of common resale brands (SEED_BRANDS below), which
  is inherently incomplete and will miss lesser-known/niche brands.

NOTE ON SELECTORS:
  Same caveat as brand extraction — CSS selectors below are best-guess
  placeholders. Use --debug to dump raw HTML and confirm/fix them against
  the live site in devtools.
"""

import argparse
import json
import re
import time
from collections import defaultdict
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from playwright.sync_api import sync_playwright, Page


# Generic sold-item browse pages across broad categories. Not brand-specific
# on purpose — we're sampling "what's selling" across the marketplace, not
# looking up a brand we already suspect is trending. Category slugs below
# are best-guess based on Depop's typical nav structure; confirm/adjust
# against the live site (View Source / devtools network tab) with --debug.
# Only 3 real top-level categories exist on Depop (confirmed via live nav
# inspection on 2026-09-02) — earlier guesses like "streetwear"/"denim"/etc.
# were not real category slugs and silently returned nothing.
DEFAULT_CATEGORIES = [
    "womens",
    "mens",
    "kids",
]

DEPOP_CATEGORY_URL = "https://www.depop.com/category/{category}/?sold=true"

# Seed list for the text-matching brand fallback. Necessarily incomplete —
# resale/streetwear/vintage/designer skew since that's most of Depop's
# volume. Extend this over time, or better: replace with the structured
# brand field once its real path in __NEXT_DATA__ is confirmed (see
# extract_brand_structured below).
SEED_BRANDS = [
    "Nike", "Adidas", "Zara", "Levi's", "Carhartt", "The North Face",
    "Stussy", "Supreme", "Ralph Lauren", "Tommy Hilfiger", "Calvin Klein",
    "Champion", "Reebok", "Vans", "Fila", "New Balance", "Puma",
    "Nasty Gal", "Urban Outfitters", "ASOS", "H&M", "Topshop",
    "Brandy Melville", "American Apparel", "Coach", "Michael Kors",
    "Gucci", "Prada", "Louis Vuitton", "Chanel", "Dior", "Burberry",
    "Patagonia", "Dickies", "Wrangler", "Diesel", "Guess", "Juicy Couture",
    "Ed Hardy", "Von Dutch", "Abercrombie & Fitch", "Hollister",
    "Free People", "Dr. Martens", "UGG", "Jordan", "Yeezy", "Off-White",
    "Bape", "Nike SB", "Columbia", "Timberland", "Converse",
    "Fred Perry", "Lacoste", "Adidas Originals", "Champion Reverse Weave",
    "Y2K", "Wrangler", "Lee", "Superdry", "Jack Wills", "Boohoo",
    "PrettyLittleThing", "Missguided", "River Island", "New Look",
    "Primark", "Shein", "Reformation", "Ganni", "Acne Studios",
]


@dataclass
class Listing:
    brand: Optional[str]
    title: Optional[str]
    price: Optional[float]
    currency: Optional[str]
    image_url: Optional[str]
    listing_url: Optional[str]
    category: Optional[str] = None
    sold: bool = True


def extract_brand_structured(raw_item: dict) -> Optional[str]:
    """Preferred path: pull brand from a real structured field once we know
    where it lives in the listing JSON (Depop's brand filter dropdown
    implies this field exists). Placeholder key names below — inspect a
    real payload (--debug) and update these keys once confirmed."""
    for key in ("brand", "brandName", "brand_name"):
        if isinstance(raw_item, dict) and raw_item.get(key):
            return str(raw_item[key]).strip()
    return None


def guess_brand_from_title(title: str, seed_brands: list[str]) -> Optional[str]:
    """Fallback brand extraction via text matching against SEED_BRANDS.
    Picks the longest matching brand name to avoid a short brand name
    (e.g. 'Lee') matching inside an unrelated word."""
    if not title:
        return None
    lowered = title.lower()
    matches = [b for b in seed_brands if b.lower() in lowered]
    if not matches:
        return None
    return max(matches, key=len)


def extract_from_next_data(page: Page) -> Optional[list[dict]]:
    """Try to pull raw listing items out of Depop's embedded Next.js JSON
    state. Preferred over DOM scraping when it works, since it's not tied
    to fragile CSS class names AND is where a real structured brand field
    would live. Exact key path is a TODO — confirm via --debug."""
    try:
        raw = page.locator("script#__NEXT_DATA__").inner_text(timeout=3000)
    except Exception:
        return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None

    # TODO: confirm real path once you can inspect the live payload, e.g.:
    #   data["props"]["pageProps"]["products"]
    # Dump `data` to a file during a --debug run to find the right path.
    return None  # placeholder until the real path is confirmed


GENERIC_BRAND_LABELS = {"other", "no brand", "unbranded", "n/a", ""}


def extract_from_dom(page: Page, category: str, seed_brands: list[str]) -> list[Listing]:
    """Scrape listing cards from the rendered DOM using Depop's real markup
    (confirmed from a live debug capture on 2026-09-02). Depop doesn't use
    data-testid attributes on cards, but each card DOES print a plain-text
    brand name, which is far more reliable than guessing from the title.

    Real structure found:
      <li class="...listItem">
        <a class="...unstyledLink" href="/products/..." aria-label="Brand's ... description">
          <img class="..._mainImage_..." src="...">
        </a>
        <p class="...brandName">SHEIN</p>
        <p class="...sizeAttributeText">XL</p>
        <p class="...price" aria-description="Price">$4.90</p>
      </li>

    CSS module class names have a hashed prefix that may change between
    Depop deployments (e.g. "styles-module__DwMxCG__brandName") — matching
    on the stable suffix via [class*="..."] is more resilient than an
    exact class match.
    """
    listings: list[Listing] = []

    cards = page.locator('li[class*="listItem"]')
    count = cards.count()

    for i in range(count):
        card = cards.nth(i)

        link = card.locator('a[class*="unstyledLink"]').first
        aria_label = ""
        listing_url = None
        try:
            aria_label = link.get_attribute("aria-label", timeout=1000) or ""
            href = link.get_attribute("href", timeout=1000)
            if href:
                listing_url = href if href.startswith("http") else f"https://www.depop.com{href}"
        except Exception:
            pass

        brand = None
        try:
            brand_text = card.locator('p[class*="brandName"]').first.inner_text(timeout=1000).strip()
            if brand_text and brand_text.lower() not in GENERIC_BRAND_LABELS:
                brand = brand_text
        except Exception:
            pass
        if not brand:
            # Fallback: some listings have no brand tag set by the seller.
            # Try matching the aria-label text against the seed list instead
            # of leaving it fully unattributed.
            brand = guess_brand_from_title(aria_label, seed_brands)

        price_text = ""
        try:
            price_text = card.locator('p[class*="__price"]').first.inner_text(timeout=1000)
        except Exception:
            pass
        price, currency = parse_price(price_text)

        image_url = None
        try:
            image_url = card.locator('img[class*="_mainImage_"]').first.get_attribute("src", timeout=1000)
        except Exception:
            try:
                image_url = card.locator("img").first.get_attribute("src", timeout=1000)
            except Exception:
                pass

        listings.append(
            Listing(
                brand=brand,
                title=aria_label or None,
                price=price,
                currency=currency,
                image_url=image_url,
                listing_url=listing_url,
                category=category,
                sold=True,  # page was requested with the sold=true param
            )
        )

    return listings


def parse_price(price_text: str) -> tuple[Optional[float], Optional[str]]:
    if not price_text:
        return None, None
    match = re.search(r"([£$€])\s?([\d,]+\.?\d*)", price_text)
    if not match:
        return None, None
    symbol, amount = match.groups()
    currency_map = {"£": "GBP", "$": "USD", "€": "EUR"}
    try:
        value = float(amount.replace(",", ""))
    except ValueError:
        value = None
    return value, currency_map.get(symbol)


def scrape_category(
    page: Page,
    category: str,
    max_items_per_category: int,
    seed_brands: list[str],
    debug: bool = False,
    capture_network: bool = False,
) -> list[Listing]:
    url = DEPOP_CATEGORY_URL.format(category=category)

    captured_calls = []
    if capture_network:
        def on_response(response):
            try:
                ct = response.headers.get("content-type", "")
                url_l = response.url.lower()
                # Heuristic: anything that looks like a data API call rather
                # than a static asset (JS/CSS/image/font).
                if "json" in ct or any(k in url_l for k in ("/api/", "webapi", "graphql")):
                    entry = {"url": response.url, "status": response.status, "content_type": ct}
                    if "json" in ct:
                        try:
                            body = response.text()
                            entry["body_preview"] = body[:4000]
                        except Exception:
                            pass
                    captured_calls.append(entry)
            except Exception:
                pass
        page.on("response", on_response)

    page.goto(url, wait_until="networkidle", timeout=30000)
    time.sleep(2)  # let any lazy-loaded content settle

    if debug:
        Path(f"debug_{category}.html").write_text(page.content())
        print(f"Saved raw HTML for '{category}' to debug_{category}.html")

    # Scroll to trigger infinite-scroll loading until we have enough items
    # or stop seeing new content appear.
    prev_count = -1
    cards = page.locator('li[class*="listItem"]')
    for _ in range(15):
        current_count = cards.count()
        if current_count >= max_items_per_category or current_count == prev_count:
            break
        prev_count = current_count
        page.mouse.wheel(0, 4000)
        time.sleep(1.5)

    if capture_network:
        Path(f"network_{category}.json").write_text(json.dumps(captured_calls, indent=2))
        print(f"  [debug] captured {len(captured_calls)} api-like responses for '{category}'")

    structured = extract_from_next_data(page)
    if structured:
        listings = [
            Listing(
                brand=extract_brand_structured(item),
                title=item.get("title"),
                price=item.get("price"),
                currency=item.get("currency"),
                image_url=item.get("image_url"),
                listing_url=item.get("listing_url"),
                category=category,
            )
            for item in structured
        ]
    else:
        listings = extract_from_dom(page, category, seed_brands)

    return listings[:max_items_per_category]


def crawl_sold_listings(
    categories: list[str],
    max_items_per_category: int = 60,
    seed_brands: Optional[list[str]] = None,
    debug: bool = False,
    capture_network: bool = False,
    delay_between_categories: float = 3.0,
) -> list[Listing]:
    """Crawl sold listings across multiple general categories — this is the
    broad sample we tally into a brand leaderboard, rather than searching
    for brands we'd have to already know about."""
    seed_brands = seed_brands or SEED_BRANDS
    all_listings: list[Listing] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        )

        for category in categories:
            try:
                listings = scrape_category(
                    page, category, max_items_per_category, seed_brands,
                    debug=debug, capture_network=capture_network,
                )
                print(f"  {category}: {len(listings)} listings ({sum(1 for l in listings if l.brand)} with a brand match)")
                all_listings.extend(listings)
            except Exception as e:
                print(f"  {category}: failed ({e})")
            time.sleep(delay_between_categories)  # be polite between category pages

        browser.close()

    return all_listings


def build_leaderboard(listings: list[Listing], top_n: int = 15) -> dict:
    """Tally sold-listing counts per brand and keep the underlying listings
    per brand for the drill-down view (images/captions/listings on click)."""
    counts: dict[str, int] = defaultdict(int)
    listings_by_brand: dict[str, list[dict]] = defaultdict(list)

    for listing in listings:
        if not listing.brand:
            continue  # unattributed listings don't count toward any brand
        counts[listing.brand] += 1
        listings_by_brand[listing.brand].append(asdict(listing))

    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:top_n]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "leaderboard": [{"brand": brand, "sales_count": count} for brand, count in ranked],
        "listings_by_brand": {brand: listings_by_brand[brand] for brand, _ in ranked},
    }


def main():
    parser = argparse.ArgumentParser(
        description="Crawl sold Depop listings across categories and build a top-brand leaderboard."
    )
    parser.add_argument(
        "--categories",
        nargs="+",
        default=DEFAULT_CATEGORIES,
        help="Depop category slugs to crawl (default: a broad general set)",
    )
    parser.add_argument("--max-items-per-category", type=int, default=500)
    parser.add_argument("--top-n", type=int, default=15)
    parser.add_argument("--out", default="sold_trends.json")
    parser.add_argument(
        "--history-dir",
        default=None,
        help="If set, also saves a timestamped copy here (e.g. history/2026-08-31.json) for day-over-day trend tracking",
    )
    parser.add_argument("--debug", action="store_true", help="Save raw page HTML per category for selector debugging")
    parser.add_argument(
        "--capture-network",
        action="store_true",
        help="Log API-like network responses per category to network_<category>.json, to find the real pagination/data endpoint",
    )
    args = parser.parse_args()

    print(f"Crawling {len(args.categories)} categories...")
    listings = crawl_sold_listings(
        args.categories,
        max_items_per_category=args.max_items_per_category,
        debug=args.debug,
        capture_network=args.capture_network,
    )
    print(f"Total listings scraped: {len(listings)}")

    result = build_leaderboard(listings, top_n=args.top_n)

    out_path = Path(args.out)
    out_path.write_text(json.dumps(result, indent=2))

    if args.history_dir:
        history_dir = Path(args.history_dir)
        history_dir.mkdir(parents=True, exist_ok=True)
        dated_path = history_dir / f"{datetime.now(timezone.utc).date().isoformat()}.json"
        dated_path.write_text(json.dumps(result, indent=2))
        print(f"Saved dated snapshot to {dated_path}")

    print(f"\nTop {args.top_n} brands by sold-listing count:")
    for entry in result["leaderboard"]:
        print(f"  {entry['brand']}: {entry['sales_count']}")
    print(f"\nSaved full results (leaderboard + per-brand listings) to {out_path}")


if __name__ == "__main__":
    main()
