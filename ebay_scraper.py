import json
import os
import time
import base64
from datetime import datetime, timezone
from pathlib import Path

import requests


SEARCH_QUERIES = [
    "Nike hoodie",
    "Nike sweatshirt",
    "Carhartt jacket",
    "Levi's 501",
    "Ralph Lauren sweater",
]

OUTPUT_DIR = Path("data/ebay_history")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

EBAY_API_URL = "https://api.ebay.com"
EBAY_TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"


def get_access_token():
    app_id = os.environ.get("EBAY_APP_ID")
    cert_id = os.environ.get("EBAY_CERT_ID")

    if not app_id or not cert_id:
        raise RuntimeError(
            "Missing EBAY_APP_ID or EBAY_CERT_ID GitHub secrets."
        )

    credentials = f"{app_id}:{cert_id}"

    encoded_credentials = base64.b64encode(
        credentials.encode("utf-8")
    ).decode("utf-8")

    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": f"Basic {encoded_credentials}",
    }

    data = {
        "grant_type": "client_credentials",
        "scope": "https://api.ebay.com/oauth/api_scope",
    }

    response = requests.post(
        EBAY_TOKEN_URL,
        headers=headers,
        data=data,
        timeout=30,
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"eBay OAuth failed: "
            f"{response.status_code} {response.text}"
        )

    return response.json()["access_token"]


def search_ebay(access_token, query):
    url = f"{EBAY_API_URL}/buy/browse/v1/item_summary/search"

    headers = {
        "Authorization": f"Bearer {access_token}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
        "Accept": "application/json",
    }

    params = {
        "q": query,
        "limit": 200,
    }

    response = requests.get(
        url,
        headers=headers,
        params=params,
        timeout=30,
    )

    if response.status_code != 200:
        print(
            f"eBay search failed for {query}: "
            f"{response.status_code}"
        )
        print(response.text)
        return []

    data = response.json()

    results = []

    for item in data.get("itemSummaries", []):
        price = item.get("price", {})

        results.append({
            "item_id": item.get("itemId"),
            "title": item.get("title"),
            "price": price.get("value"),
            "currency": price.get("currency"),
            "condition": item.get("condition"),
            "url": item.get("itemWebUrl"),
            "image_url": (
                item.get("image", {})
                .get("imageUrl")
            ),
            "seller": (
                item.get("seller", {})
                .get("username")
            ),
            "query": query,
            "scraped_at": datetime.now(
                timezone.utc
            ).isoformat(),
        })

    return results


def main():

    print("-----------------------------")
    print("eBay API scraper starting")
    print("-----------------------------")

    access_token = get_access_token()

    print("Successfully authenticated with eBay.")

    all_results = []

    for query in SEARCH_QUERIES:

        print(f"\nSearching: {query}")

        results = search_ebay(
            access_token,
            query
        )

        print(
            f"Found {len(results)} listings"
        )

        all_results.extend(results)

        time.sleep(1)

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
    print("eBay API scrape complete")
    print("-----------------------------")
    print(
        f"Total listings: {len(all_results)}"
    )
    print(
        f"Saved to: {output_file}"
    )


if __name__ == "__main__":
    main()
