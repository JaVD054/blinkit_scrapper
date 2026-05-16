"""
Core HTTP client for Blinkit's internal search API.

Location routing works by injecting lat/lon cookies — Blinkit's CDN
reads these to pin the request to the nearest dark store.

If you hit a 403, the bot-detection wall is up; switch to main.py's
--playwright flag (Playwright approach, not yet implemented here) or
rotate your User-Agent.
"""

import re
import time
import requests
from dataclasses import dataclass
from typing import Optional


@dataclass
class Product:
    product_name: str
    brand: str
    price: float
    mrp: float
    availability_status: str
    delivery_eta: str


class BlinkitClient:
    SEARCH_URL = "https://blinkit.com/v1/layout/search"
    PDP_BASE_URL = "https://blinkit.com/v1/layout/product"

    # Chrome 124 desktop fingerprint
    _HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-IN,en-US;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://blinkit.com/",
        "Origin": "https://blinkit.com",
        "Connection": "keep-alive",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "Sec-CH-UA": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Platform": '"Windows"',
        # Blinkit internal routing headers
        "app_client": "consumer_web",
        "web-version": "3.0.1",
    }

    def __init__(self, lat: float, lon: float) -> None:
        self.lat = lat
        self.lon = lon
        self.session = requests.Session()
        self.session.headers.update(self._HEADERS)
        # Blinkit reads these cookies to determine which dark store to query
        self.session.cookies.set("lat", str(lat), domain="blinkit.com")
        self.session.cookies.set("lon", str(lon), domain="blinkit.com")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def search(self, query: str, max_retries: int = 3) -> list[Product]:
        """
        Search Blinkit for `query` at the configured lat/lon.

        Raises:
            PermissionError  – 403, bot-detection wall hit
            ConnectionError  – network failure after all retries
            ValueError       – location outside serviceable area
        """
        params = {"q": query, "search_type": "type_to_search"}

        for attempt in range(max_retries):
            try:
                resp = self.session.get(self.SEARCH_URL, params=params, timeout=15)
            except requests.ConnectionError as exc:
                if attempt == max_retries - 1:
                    raise ConnectionError(f"Network error after {max_retries} attempts: {exc}") from exc
                time.sleep(2 ** attempt)
                continue

            if resp.status_code == 403:
                raise PermissionError(
                    "Blinkit blocked the request (403 Forbidden). "
                    "Bot-detection may be active — try rotating User-Agent or use Playwright."
                )

            if resp.status_code == 429:
                delay = 2 ** (attempt + 1)
                print(f"[yellow]Rate limited. Retrying in {delay}s…[/yellow]")
                time.sleep(delay)
                continue

            resp.raise_for_status()

            try:
                data = resp.json()
            except ValueError as exc:
                raise ValueError(f"Non-JSON response (status {resp.status_code})") from exc

            return self._parse_products(data)

        return []

    def fetch_product(self, prid: int) -> "Optional[Product]":
        """
        Fetch a single product by its Blinkit product ID.

        Raises:
            PermissionError  – 403, bot-detection wall hit
            ConnectionError  – network failure
        """
        url = f"{self.PDP_BASE_URL}/{prid}"
        try:
            resp = self.session.get(url, timeout=15)
        except requests.ConnectionError as exc:
            raise ConnectionError(f"Network error: {exc}") from exc

        if resp.status_code == 403:
            raise PermissionError("Blinkit blocked the request (403 Forbidden).")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()

        try:
            data = resp.json()
        except ValueError:
            return None

        return self._parse_pdp(data)

    def _parse_pdp(self, data: dict) -> "Optional[Product]":
        seo = (
            data.get("response", {})
            .get("tracking", {})
            .get("le_meta", {})
            .get("custom_data", {})
            .get("seo", {})
        )
        if not seo:
            return None

        product_name = seo.get("product_name", "Unknown")
        brand = seo.get("brand", "N/A")
        price = float(seo.get("price") or 0)
        mrp = float(seo.get("mrp") or price)
        inventory = seo.get("inventory", 0)

        footer_models = (
            data.get("response", {})
            .get("page_level_components", {})
            .get("sticky", {})
            .get("footer_snippet_models", [])
        )
        is_sold_out = False
        if footer_models:
            is_sold_out = footer_models[0].get("snippet", {}).get("data", {}).get("is_sold_out", False)

        if is_sold_out or inventory == 0:
            status = "Out of Stock"
        elif isinstance(inventory, int) and inventory <= 3:
            status = f"Only {inventory} left"
        else:
            status = "In Stock"

        return Product(
            product_name=product_name,
            brand=brand,
            price=price,
            mrp=mrp,
            availability_status=status,
            delivery_eta="N/A",
        )

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _parse_products(self, data: dict) -> list[Product]:
        if data.get("error_code") in ("LOCATION_NOT_SERVICEABLE", "OUT_OF_RANGE"):
            raise ValueError("Location not serviceable by Blinkit.")

        # Layout/snippet API (v1/layout/search)
        snippets = data.get("response", {}).get("snippets") or []
        if snippets:
            products: list[Product] = []
            for snippet in snippets:
                if "product_card" not in snippet.get("widget_type", ""):
                    continue
                try:
                    products.append(self._map_snippet(snippet))
                except (KeyError, TypeError, ValueError):
                    continue
            return products

        # Legacy flat-list fallback
        raw: list = (
            data.get("products")
            or data.get("data", {}).get("products")
            or data.get("response", {}).get("products")
            or data.get("objects")
            or []
        )
        if not raw and isinstance(data.get("objects"), list):
            raw = [o for o in data["objects"] if o.get("type") == "product"]

        products = []
        for item in raw:
            try:
                products.append(self._map_item(item))
            except (KeyError, TypeError, ValueError):
                continue

        return products

    def _map_snippet(self, snippet: dict) -> "Product":
        d = snippet["data"]
        cart_item = (
            d.get("atc_action", {}).get("add_to_cart", {}).get("cart_item") or {}
        )
        product_name = (
            cart_item.get("product_name")
            or cart_item.get("display_name")
            or (d.get("display_name") or {}).get("text")
            or (d.get("name") or {}).get("text")
            or "Unknown"
        )
        brand = cart_item.get("brand") or (d.get("brand_name") or {}).get("text") or "N/A"
        price = float(cart_item.get("price") or 0)
        mrp = float(cart_item.get("mrp") or price)

        qty = cart_item.get("inventory") if cart_item else d.get("inventory")
        is_sold_out = d.get("is_sold_out", False)

        if is_sold_out or qty == 0:
            status = "Out of Stock"
        elif isinstance(qty, int) and qty <= 3:
            status = f"Only {qty} left"
        else:
            status = "In Stock"

        eta = "N/A"
        eta_img = (d.get("eta_tag") or {}).get("image", {}).get("url", "")
        if eta_img:
            m = re.search(r"(\d+)-mins", eta_img)
            eta = f"{m.group(1)} mins" if m else "N/A"
        if eta == "N/A":
            eta_id = cart_item.get("eta_identifier") or d.get("eta_identifier") or ""
            if eta_id == "express":
                eta = "~10 mins"

        return Product(
            product_name=product_name,
            brand=brand,
            price=price,
            mrp=mrp,
            availability_status=status,
            delivery_eta=eta,
        )

    def _map_item(self, item: dict) -> Product:
        # ---- availability ------------------------------------------------
        inventory = item.get("inventory") or {}
        qty = inventory.get("quantity") if inventory else item.get("quantity")
        is_available = item.get("is_available", True)

        if not is_available or qty == 0:
            status = "Out of Stock"
        elif isinstance(qty, int) and qty <= 3:
            status = f"Only {qty} left"
        else:
            status = "In Stock"

        # ---- delivery ETA ------------------------------------------------
        eta_raw = (
            item.get("delivery_time")
            or item.get("delivery_eta")
            or item.get("eta")
            or item.get("promised_delivery_time")
        )
        if eta_raw is None:
            eta = "N/A"
        elif isinstance(eta_raw, (int, float)):
            eta = f"{int(eta_raw)} mins"
        else:
            eta = str(eta_raw)

        # ---- price -------------------------------------------------------
        price = float(item.get("price") or item.get("selling_price") or 0)
        mrp = float(item.get("mrp") or item.get("original_price") or price)

        return Product(
            product_name=item.get("name") or item.get("product_name") or "Unknown",
            brand=item.get("brand") or item.get("brand_name") or "N/A",
            price=price,
            mrp=mrp,
            availability_status=status,
            delivery_eta=eta,
        )


# ------------------------------------------------------------------
# PIN code → (lat, lon) via OpenStreetMap Nominatim (no API key needed)
# ------------------------------------------------------------------

def pin_to_latlon(pin: str) -> tuple[float, float]:
    """
    Resolve an Indian PIN code to (lat, lon) using Nominatim.
    Raises ValueError if the PIN is not found or network fails.
    """
    url = "https://nominatim.openstreetmap.org/search"
    params = {
        "postalcode": pin,
        "country": "India",
        "format": "json",
        "limit": 1,
    }
    headers = {"User-Agent": "blinkit-price-bot/1.0"}

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        results = resp.json()
    except requests.RequestException as exc:
        raise ValueError(f"Geocoding request failed: {exc}") from exc

    if not results:
        raise ValueError(f"PIN code '{pin}' not found in Nominatim. Check the code or pass --lat/--lon directly.")

    return float(results[0]["lat"]), float(results[0]["lon"])
