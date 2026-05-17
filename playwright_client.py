"""
Playwright-based Blinkit client.

Strategy:
  1. Launch headless Chromium with a real Chrome UA.
  2. Pre-set lat/lon cookies so Blinkit routes to the right dark store.
  3. Intercept v1/layout/product/<prid> XHR responses and grab the JSON payload.
  4. For search: intercept v1/layout/search and fall back to DOM scraping if needed.
"""

import json
import logging
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from playwright.sync_api import sync_playwright, Page, Response, BrowserContext, Browser

from blinkit_client import Product

logger = logging.getLogger(__name__)


_PRODUCT_SELECTORS = [
    '[data-testid="product-item"]',
    '[class*="Product__container"]',
    '[class*="product-card"]',
    'div[class*="plp-product"]',
    'div[class*="ProductCard"]',
]

_ANTI_BOT_SCRIPT = (
    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
)

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# PDP response parser — shared by fetch_product_by_url and fetch_all
# ---------------------------------------------------------------------------

def _parse_pdp(data: dict) -> Optional[Product]:
    """Extract a Product from a v1/layout/product/<prid> API response."""
    # with open("debug_pdp_payload.json", "w", encoding="utf-8") as f:
    #     json.dump(data, f, ensure_ascii=False, indent=2)
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

    logger.debug(
        "_parse_pdp %s | brand=%s price=%s mrp=%s inventory=%s sold_out=%s → %s",
        product_name, brand, price, mrp, inventory, is_sold_out, status,
    )

    return Product(
        product_name=product_name,
        brand=brand,
        price=price,
        mrp=mrp,
        availability_status=status,
        delivery_eta="N/A",
    )


class BlinkitPlaywrightClient:
    def __init__(self, lat: float = 12.993042, lon: float = 77.668716, headless: bool = True) -> None:
        self.lat = lat
        self.lon = lon
        self.headless = headless
        self.store_name: str = ""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_all(
        self,
        urls: list[str],
        on_result=None,
        workers: int = 3,
    ) -> list[tuple[str, Optional[Product]]]:
        """
        Check availability for multiple product URLs in parallel.
        Returns [(url, Product | None), ...] in the same order as `urls`.

        on_result(done: int, total: int, url: str, product: Optional[Product])
        is called after each URL is fetched so callers can show live progress.
        """
        total = len(urls)
        results: list[tuple[str, Optional[Product]] | None] = [None] * total
        done_count = 0

        def _task(i: int, url: str):
            return i, url, self._fetch_isolated(url)

        with ThreadPoolExecutor(max_workers=min(workers, total)) as pool:
            futures = {pool.submit(_task, i, url): i for i, url in enumerate(urls)}
            for future in as_completed(futures):
                i, url, product = future.result()
                results[i] = (url, product)
                done_count += 1
                if on_result:
                    on_result(done_count, total, url, product)

        return results  # type: ignore[return-value]

    def fetch_product_by_url(self, url: str) -> Optional[Product]:
        """Single-product convenience wrapper around fetch_all."""
        results = self.fetch_all([url])
        return results[0][1] if results else None

    def search(self, query: str) -> list[Product]:
        with sync_playwright() as pw:
            browser = self._launch_browser(pw)
            context = self._new_context(browser)
            self._inject_location_cookies(context)

            captured: list[dict] = []
            all_urls: list[str] = []

            page = context.new_page()
            page.add_init_script(_ANTI_BOT_SCRIPT)
            self._force_location(page)
            page.on("response", lambda r: self._on_search_response(r, captured, all_urls))

            try:
                page.goto(
                    f"https://blinkit.com/s/?q={query}",
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )
                page.wait_for_timeout(5_000)
            except Exception:
                pass

            with open("debug_urls.txt", "w", encoding="utf-8") as f:
                f.write("\n".join(all_urls))

            products: list[Product] = []
            if captured:
                with open("debug_api_payloads.json", "w", encoding="utf-8") as f:
                    json.dump(captured, f, ensure_ascii=False, indent=2)
                for payload in captured:
                    products.extend(self._parse_search_payload(payload))
            else:
                products = self._scrape_dom(page, query)

            browser.close()
            return products

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_isolated(self, url: str) -> Optional[Product]:
        """Run a single product fetch in its own Playwright session (thread-safe)."""
        with sync_playwright() as pw:
            browser = self._launch_browser(pw)
            context = self._new_context(browser)
            self._inject_location_cookies(context)
            product = self._fetch_one(context, url)
            browser.close()
        return product

    def _launch_browser(self, pw) -> Browser:
        return pw.chromium.launch(
            headless=self.headless,
            args=["--no-sandbox", "--disable-setuid-sandbox",
                  "--disable-blink-features=AutomationControlled"],
        )

    def _new_context(self, browser: Browser) -> BrowserContext:
        return browser.new_context(
            user_agent=_DEFAULT_UA,
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            viewport={"width": 1280, "height": 800},
            extra_http_headers={"Accept-Language": "en-IN,en-US;q=0.9,en;q=0.8"},
            geolocation={"latitude": self.lat, "longitude": self.lon},
            permissions=["geolocation"],
        )

    def _inject_location_cookies(self, context: BrowserContext) -> None:
        for name, value in [("lat", str(self.lat)), ("lon", str(self.lon))]:
            context.add_cookies([{
                "name": name, "value": value,
                "domain": ".blinkit.com", "path": "/",
                "httpOnly": False, "secure": True, "sameSite": "None",
            }])

    def _force_location(self, page) -> None:
        """Intercept outgoing requests that carry lat/lon and rewrite them to our coordinates."""
        _lat, _lon = str(self.lat), str(self.lon)

        def _rewrite(route) -> None:
            try:
                url = route.request.url
                parsed = urllib.parse.urlparse(url)
                params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
                changed = False
                for lat_key in ("latitude", "lat"):
                    if lat_key in params:
                        params[lat_key] = [_lat]
                        changed = True
                for lon_key in ("longitude", "lon"):
                    if lon_key in params:
                        params[lon_key] = [_lon]
                        changed = True
                if changed:
                    new_query = urllib.parse.urlencode(params, doseq=True)
                    new_url = parsed._replace(query=new_query).geturl()
                    logger.debug("_force_location rewrote %s → %s", url, new_url)
                    route.continue_(url=new_url)
                else:
                    route.continue_()
            except Exception as exc:
                logger.debug("_force_location _rewrite error: %s", exc)
                try:
                    route.continue_()
                except Exception:
                    pass

        page.route("**/visibility*", _rewrite)
        page.route("**/location/info*", _rewrite)
        page.route("**/consumerweb/eta*", _rewrite)

    def _fetch_one(self, context: BrowserContext, url: str) -> Optional[Product]:
        """Navigate to a product page in an existing context and return the Product."""
        page = context.new_page()
        page.add_init_script(_ANTI_BOT_SCRIPT)
        self._force_location(page)

        # Capture location/info response reference only — body read after product resolves
        _loc_resp: list[Response] = []
        def _on_loc(r: Response) -> None:
            if "location/info" in r.url and r.status == 200 and not _loc_resp:
                _loc_resp.append(r)

        if not self.store_name:
            page.on("response", _on_loc)

        product = None
        try:
            with page.expect_response(
                lambda r: "v1/layout/product" in r.url and r.status == 200,
                timeout=60_000,
            ) as resp_info:
                page.goto(url, wait_until="domcontentloaded", timeout=60_000)

            # location/info fires a few seconds after the product API — wait for it
            if not self.store_name:
                page.wait_for_timeout(3_000)
                if _loc_resp:
                    try:
                        loc_data = _loc_resp[0].json()
                        polygons = loc_data.get("poi_data", {}).get("polygons", [])
                        if polygons:
                            name = polygons[0].get("name", "")
                            area = polygons[0].get("heuristics", "")
                            self.store_name = f"{name} — {area}" if area else name
                    except Exception:
                        pass

            data = resp_info.value.json()
            product = _parse_pdp(data)
        except Exception as exc:
            logger.warning("_fetch_one failed for %s: %s", url, exc)
        finally:
            page.close()
        return product

    def _on_search_response(self, response: Response, captured: list, all_urls: list) -> None:
        url = response.url
        if response.status != 200:
            return
        if "json" not in response.headers.get("content-type", ""):
            return
        all_urls.append(url)
        if not any(pat in url for pat in (
            "products/search", "v2/search", "v1/search", "api/search",
            "search?", "/search/", "listing", "v3/search", "v4/search", "v1/layout/search",
        )):
            return
        try:
            captured.append(response.json())
        except Exception:
            pass

    def _parse_search_payload(self, data: dict) -> list[Product]:
        snippets = data.get("response", {}).get("snippets") or []
        if snippets:
            products = []
            for snippet in snippets:
                if "product_card" not in snippet.get("widget_type", ""):
                    continue
                try:
                    products.append(self._map_snippet(snippet))
                except (KeyError, TypeError, ValueError):
                    continue
            return products

        raw: list = (
            data.get("products")
            or data.get("data", {}).get("products")
            or data.get("response", {}).get("products")
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

    def _map_snippet(self, snippet: dict) -> Product:
        logger.debug(f"_map_snippet {snippet}")
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
        brand = (
            cart_item.get("brand")
            or (d.get("brand_name") or {}).get("text")
            or "N/A"
        )
        price = float(cart_item.get("price") or 0)
        mrp = float(cart_item.get("mrp") or price)

        qty = cart_item.get("inventory") if cart_item else d.get("inventory")
        is_sold_out = d.get("is_sold_out", False)

        logger.debug(
            "_map_snippet %s | brand=%s price=%s mrp=%s qty=%s sold_out=%s",
            product_name, brand, price, mrp, qty, is_sold_out,
        )

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
            product_name=product_name, brand=brand,
            price=price, mrp=mrp,
            availability_status=status, delivery_eta=eta,
        )

    def _map_item(self, item: dict) -> Product:
        inventory = item.get("inventory") or {}
        qty = inventory.get("quantity") if inventory else item.get("quantity")
        is_available = item.get("is_available", True)

        if not is_available or qty == 0:
            status = "Out of Stock"
        elif isinstance(qty, int) and qty <= 3:
            status = f"Only {qty} left"
        else:
            status = "In Stock"

        eta_raw = (
            item.get("delivery_time") or item.get("delivery_eta")
            or item.get("eta") or item.get("promised_delivery_time")
        )
        eta = f"{int(eta_raw)} mins" if isinstance(eta_raw, (int, float)) else str(eta_raw or "N/A")

        price = float(item.get("price") or item.get("selling_price") or 0)
        mrp = float(item.get("mrp") or item.get("original_price") or price)

        return Product(
            product_name=item.get("name") or item.get("product_name") or "Unknown",
            brand=item.get("brand") or item.get("brand_name") or "N/A",
            price=price, mrp=mrp,
            availability_status=status, delivery_eta=eta,
        )

    def _scrape_dom(self, page: Page, query: str) -> list[Product]:
        products: list[Product] = []
        selector = None
        for sel in _PRODUCT_SELECTORS:
            if page.locator(sel).count() > 0:
                selector = sel
                break
        if selector is None:
            return products
        for el in page.locator(selector).all():
            try:
                text = el.inner_text()
                lines = [l.strip() for l in text.splitlines() if l.strip()]
                if not lines:
                    continue
                name = lines[0]
                brand = lines[1] if len(lines) > 1 else "N/A"
                price = mrp = 0.0
                for line in lines:
                    clean = line.replace("₹", "").replace(",", "").strip()
                    try:
                        val = float(clean)
                        if price == 0.0:
                            price = val
                        elif val > price:
                            mrp = val
                    except ValueError:
                        pass
                mrp = mrp or price
                products.append(Product(
                    product_name=name, brand=brand,
                    price=price, mrp=mrp,
                    availability_status="In Stock", delivery_eta="N/A",
                ))
            except Exception:
                continue
        return products
