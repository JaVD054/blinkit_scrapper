"""
Single-run availability check — used by GitHub Actions.
Reads products.txt, checks each product once, sends Telegram if any are in stock.
"""

import os
import sys

import requests as _requests
from dotenv import load_dotenv
from rich.console import Console

load_dotenv()

from blinkit_client import pin_to_latlon
from playwright_client import BlinkitPlaywrightClient

console = Console()

_PIN_FALLBACK: dict[str, tuple[float, float]] = {
    "110070": (28.5188, 77.1548),
    "400001": (18.9387, 72.8353),
    "560001": (12.9716, 77.5946),
    "560016": (12.993042, 77.668716),
    "600001": (13.0827, 80.2707),
    "700001": (22.5726, 88.3639),
}


def resolve_pin(pin: str) -> tuple[float, float]:
    if pin in _PIN_FALLBACK:
        return _PIN_FALLBACK[pin]
    return pin_to_latlon(pin)


def load_urls(path: str = "products.txt") -> list[str]:
    with open(path, encoding="utf-8") as f:
        return [l.strip() for l in f if l.strip() and not l.startswith("#")]


def send_telegram(token: str, chat_id: str, product, url: str) -> None:
    text = (
        f"*Blinkit: Back in Stock!*\n"
        f"[{product.product_name}]({url})\n"
        f"Status: *{product.availability_status}*\n"
        f"Price: ₹{product.price:.0f}"
    )
    _requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
        timeout=10,
    )


def main() -> None:
    pin = os.getenv("BLINKIT_PIN", "560016")
    tg_token = os.getenv("TG_TOKEN")
    tg_chat = os.getenv("TG_CHAT")

    lat, lon = resolve_pin(pin)
    urls = load_urls()

    console.print(f"Checking {len(urls)} product(s) at PIN {pin} ({lat}, {lon})…")

    client = BlinkitPlaywrightClient(lat, lon, headless=True)
    results = client.fetch_all(urls)

    console.print(f"Store: [bold]{client.store_name or 'unknown'}[/bold]")

    found_any = False
    for url, product in results:
        if product is None:
            console.print(f"  [red]FAIL[/red] {url}")
            continue

        in_stock = product.availability_status != "Out of Stock"
        color = "green" if in_stock else "red"
        console.print(
            f"  [{color}]{product.availability_status}[/{color}]"
            f"  {product.product_name}  ₹{product.price:.0f}"
        )

        if in_stock and tg_token and tg_chat:
            send_telegram(tg_token, tg_chat, product, url)
            console.print(f"    → Telegram sent")
            found_any = True

    if not found_any:
        console.print("Nothing in stock this run.")


if __name__ == "__main__":
    main()
