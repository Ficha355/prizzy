import requests
from urllib.parse import urlencode

BASE_URL = "https://www.vinted.fr"
API_URL = f"{BASE_URL}/api/v2/catalog/items"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    "Referer": f"{BASE_URL}/",
    "Origin": BASE_URL,
}


def _get_session() -> requests.Session:
    """Bootstrap a session with a valid CSRF token from Vinted's homepage."""
    session = requests.Session()
    session.headers.update(HEADERS)
    # Hit the homepage so Vinted sets the _vinted_fr_session cookie
    session.get(BASE_URL, timeout=10)
    return session


def search_items(query: str, per_page: int = 20, page: int = 1) -> dict:
    """
    Search Vinted catalog by text query.
    Returns the raw JSON response from the API.
    """
    session = _get_session()

    params = {
        "search_text": query,
        "per_page": per_page,
        "page": page,
        "order": "newest_first",
    }

    resp = session.get(API_URL, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    pagination = data.get("pagination", {})

    items = []
    for item in data.get("items", []):
        price_obj = item.get("price") or {}
        items.append({
            "id": item.get("id"),
            "title": item.get("title"),
            "price_amount": price_obj.get("amount"),
            "currency": price_obj.get("currency_code"),
            "brand_title": item.get("brand_title"),
            "size_title": item.get("size_title"),
            "status": item.get("status"),
            "url": f"{BASE_URL}{item.get('path', '')}",
            "photo": (item.get("photo") or {}).get("url"),
        })

    return {
        "total_entries": pagination.get("total_entries", 0),
        "total_pages": pagination.get("total_pages", 0),
        "page": page,
        "per_page": per_page,
        "items": items,
    }
