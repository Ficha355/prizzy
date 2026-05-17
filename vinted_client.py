import re
import requests
from urllib.parse import urlencode, urlparse

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


def _parse_member_identifier(profile_url: str) -> str:
    """Extract member login/id from a Vinted profile URL."""
    url = profile_url.strip()
    if not url.startswith("http"):
        url = "https://" + url
    path = urlparse(url).path.rstrip("/")
    parts = [p for p in path.split("/") if p]
    for i, part in enumerate(parts):
        if part in ("member", "membres", "mitglieder") and i + 1 < len(parts):
            return parts[i + 1]
    return parts[-1] if parts else profile_url


def get_seller_items(profile_url: str, max_items: int = 50) -> tuple[dict, list[dict]]:
    """
    Fetch active listings from a Vinted seller profile URL.
    Returns (user_info dict, items list).
    Raises ValueError if the seller cannot be resolved.
    """
    sess = _get_session()
    identifier = _parse_member_identifier(profile_url)

    resp = sess.get(f"{BASE_URL}/api/v2/users/{identifier}", timeout=10)
    if resp.status_code != 200:
        raise ValueError(f"Profil Vinted introuvable : « {identifier} »")

    user_data = resp.json().get("user", {})
    user_id = user_data.get("id")
    if not user_id:
        raise ValueError(f"Profil Vinted introuvable : « {identifier} »")

    user_info = {
        "id": user_id,
        "login": user_data.get("login", identifier),
        "item_count": user_data.get("items_count", 0),
        "feedback_count": user_data.get("positive_feedback_count", 0),
    }

    items = []
    page = 1
    while len(items) < max_items:
        per_page = min(96, max_items - len(items))
        resp = sess.get(
            f"{BASE_URL}/api/v2/users/{user_id}/items",
            params={"per_page": per_page, "page": page},
            timeout=15,
        )
        if resp.status_code != 200:
            break
        data = resp.json()
        batch = data.get("items", [])
        if not batch:
            break

        for item in batch:
            price_obj = item.get("price") or {}
            items.append({
                "id": item.get("id"),
                "title": item.get("title", ""),
                "price": float(price_obj.get("amount", 0) or 0),
                "brand": item.get("brand_title") or "—",
                "size": item.get("size_title") or "—",
                "condition": item.get("status") or "—",
                "url": f"{BASE_URL}{item.get('path', '')}",
                "views": item.get("view_count", 0),
                "favourites": item.get("favourite_count", 0),
            })

        total_pages = data.get("pagination", {}).get("total_pages", 1)
        if page >= total_pages:
            break
        page += 1

    return user_info, items
