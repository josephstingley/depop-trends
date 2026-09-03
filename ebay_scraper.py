import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus

from playwright.async_api import async_playwright


SEARCH_QUERIES = [
    "Nike hoodie",
    "Nike sweatshirt",
    "Carhartt jacket",
    "Levi's 501",
    "Ralph Lauren sweater",
]

OUTPUT_DIR = Path("data/ebay_history")
DEBUG_DIR = Path("data/ebay_debug")

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
DEBUG_DIR.mkdir(parents=True, exist_ok=True)


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


async def scrape_query(page, query):
    print(f"\nSearching eBay for: {query}")

    url = (
        "https://www.ebay.com/sch/i.html"
        f"?_nkw={quote_plus(query)}"
        "&LH_Sold=1"
        "&LH_Complete=1"
        "&_sop=13"
        "&_ipg=120"
    )

    try:
        response = await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=60000
        )

        await page.wait_for_timeout(5000)

        print(f"HTTP status: {response.status if response else 'unknown'}")
        print(f"Page title: {await page.title()}")
        print(f"Final URL: {page.url}")

        # Save the actual HTML eBay gave us.
        safe_name = re.sub(
            r"[^a-zA-Z0-9_-]",
            "_",
            query
        )

        html_file = DEBUG_DIR / f"{safe_name}.html"

        html = await page.content()

        html_file.write_text(
            html,
            encoding="utf-8"
        )

        print(f"Saved debug HTML: {html_file}")

        # Look for common eBay result selectors.
        selectors = [
            "li.s-item",
            ".s-item",
            "[data-testid='item-card']",
            ".srp-results .s-item",
            "ul.srp-results > li"
        ]

        for selector in selectors:
            count = await page.locator(selector).count()
            print(
                f"Selector {selector}: {count} elements"
            )

        # Try the standard eBay result selector first.
        cards = page.locator("li.s-item")

        count = await cards.count()

        # Fallback selector.
        if count == 0:
            cards = page.locator(".s-item")
            count = await cards.count()

        print(f"Using {count} result cards")

        results = []

        for i in range(count):

            try:
                card = cards.nth(i)

                title = None
                price_text = None
                link = None
                condition = None

                # Title
                for selector in [
                    ".s-item__title",
                    "[role='heading']",
                    "h3"
                ]:
                    locator = card.locator(selector)

                    if await locator.count():
                        title = clean_text(
                            await locator.first.inner_text()
                        )

                        if title:
                            break

                # Price
                for selector in [
                    ".s-item__price",
                    "[class*='price']"
                ]:
                    locator = card.locator(selector)

                    if await locator.count():
                        price_text = clean_text(
                            await locator.first.inner_text()
                        )

                        if price_text:
                            break

                # Link
                for selector in [
                    "a.s-item__link",
                    "a"
                ]:
                    locator = card.locator(selector)

                    if await locator.count():
                        link = await locator.first.get_attribute(
                            "href"
                        )

                        if link:
                            break

                # Condition
                for selector in [
                    ".SECONDARY_INFO",
                    ".s-item__subtitle"
                ]:
                    locator = card.locator(selector)

                    if await locator.count():
                        condition = clean_text(
                            await locator.first.inner_text()
                        )

                        if condition:
                            break

                if not title:
                    continue

                if title.lower() in [
                    "shop on ebay",
                    "shop on ebay.com"
                ]:
                    continue

                item_id = None

                if link:
                    match = re.search(
                        r"/itm/(?:[^/]+/)?(\d+)",
                        link
                    )

                    if match:
                        item_id = match.group(1)

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
                    ).isoformat()
                })

            except Exception as e:
                print(
                    f"Error parsing result {i}: {e}"
                )

        # Remove duplicates.
        unique = {}

        for item in results:
            key = (
                item["item_id"]
                or item["url"]
                or item["title"]
            )

            unique[key] = item

        results = list(unique.values())

        print(
            f"Successfully extracted {len(results)} listings"
        )

        return results

    except Exception as e:
        print(
            f"ERROR loading eBay page: {e}"
        )

        return []


async def main():

    all_results = []

    async with async_playwright() as p:

        browser = await p.chromium.launch(
            headless=True
        )

        context = await browser.new_context(
            viewport={
                "width": 1440,
                "height": 1000
            },

            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),

            locale="en-US",

            extra_http_headers={
                "Accept-Language":
                    "en-US,en;q=0.9"
            }
        )

        page = await context.new_page()

        for query in SEARCH_QUERIES:

            results = await scrape_query(
                page,
                query
            )

            all_results.extend(results)

            await page.wait_for_timeout(
                3000
            )

        await browser.close()

    timestamp = datetime.now(
        timezone.utc
    ).strftime(
        "%Y-%m-%d_%H-%M-%S"
    )

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
