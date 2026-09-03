import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus

from playwright.async_api import async_playwright


# -----------------------------
# CONFIGURATION
# -----------------------------

SEARCH_QUERIES = [
    "Nike hoodie",
    "Nike sweatshirt",
    "Carhartt jacket",
    "Levi's 501",
    "Ralph Lauren sweater",
]

RESULTS_PER_QUERY = 50

OUTPUT_DIR = Path("data/ebay_history")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# -----------------------------
# HELPERS
# -----------------------------

def clean_text(value):
    if not value:
        return None

    return re.sub(r"\s+", " ", value).strip()


def parse_price(value):
    if not value:
        return None

    match = re.search(r"[\d,]+(?:\.\d{1,2})?", value)

    if not match:
        return None

    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


# -----------------------------
# SCRAPER
# -----------------------------

async def scrape_query(page, query):
    print(f"\nSearching eBay for: {query}")

    # eBay Advanced Search:
    # LH_Sold=1      -> sold items
    # LH_Complete=1  -> completed items
    # _sop=13       -> ending soonest / completed relevance
    url = (
        "https://www.ebay.com/sch/i.html"
        f"?_nkw={quote_plus(query)}"
        "&LH_Sold=1"
        "&LH_Complete=1"
        "&_sop=13"
    )

    await page.goto(
        url,
        wait_until="domcontentloaded",
        timeout=60000
    )

    await page.wait_for_timeout(3000)

    # Scroll to encourage lazy-loaded results
    for _ in range(3):
        await page.mouse.wheel(0, 1200)
        await page.wait_for_timeout(500)

    cards = await page.locator("li.s-item").all()

    results = []

    for card in cards:
        try:
            title_el = card.locator(".s-item__title")
            price_el = card.locator(".s-item__price")
            link_el = card.locator("a.s-item__link")

            title = clean_text(
                await title_el.inner_text()
            ) if await title_el.count() else None

            price_text = clean_text(
                await price_el.inner_text()
            ) if await price_el.count() else None

            link = (
                await link_el.get_attribute("href")
            ) if await link_el.count() else None

            # Ignore eBay's fake/placeholder first result
            if not title or title.lower() in {
                "shop on ebay",
                "shop on ebay.com"
            }:
                continue

            # Extract item ID from URL
            item_id = None

            if link:
                match = re.search(r"/itm/(\d+)", link)

                if match:
                    item_id = match.group(1)

            # Try to find condition
            condition = None

            condition_el = card.locator(".SECONDARY_INFO")

            if await condition_el.count():
                condition = clean_text(
                    await condition_el.first.inner_text()
                )

            results.append({
                "item_id": item_id,
                "title": title,
                "price": parse_price(price_text),
                "price_raw": price_text,
                "condition": condition,
                "url": link,
                "query": query,
                "scraped_at": datetime.now(
                    timezone.utc
                ).isoformat(),
            })

        except Exception as e:
            print(f"Could not parse listing: {e}")

    # Remove duplicates
    unique = {}

    for item in results:
        key = item["item_id"] or item["url"] or item["title"]

        if key:
            unique[key] = item

    results = list(unique.values())

    print(f"Found {len(results)} sold listings")

    return results


# -----------------------------
# MAIN
# -----------------------------

async def main():

    all_results = []

    async with async_playwright() as p:

        browser = await p.chromium.launch(
            headless=True
        )

        page = await browser.new_page(
            viewport={
                "width": 1440,
                "height": 1000
            },
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            )
        )

        for query in SEARCH_QUERIES:

            try:
                results = await scrape_query(
                    page,
                    query
                )

                all_results.extend(results)

                # Small delay between searches
                await page.wait_for_timeout(2000)

            except Exception as e:

                print(
                    f"ERROR searching '{query}': {e}"
                )

        await browser.close()

    # -------------------------
    # SAVE SNAPSHOT
    # -------------------------

    timestamp = datetime.now(
        timezone.utc
    ).strftime("%Y-%m-%d_%H-%M-%S")

    output_file = (
        OUTPUT_DIR /
        f"ebay_{timestamp}.json"
    )

    with open(
        output_file,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            all_results,
            f,
            indent=2,
            ensure_ascii=False
        )

    print("\n-----------------------------")
    print("eBay scrape complete")
    print("-----------------------------")
    print(
        f"Total listings: {len(all_results)}"
    )
    print(
        f"Saved to: {output_file}"
    )


if __name__ == "__main__":
    asyncio.run(main())
