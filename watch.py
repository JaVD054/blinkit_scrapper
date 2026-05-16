"""
Blinkit availability watcher — polls a list of product URLs via Playwright
and sends a Telegram notification when any product comes in stock.

Usage:
    python3 watch.py --pin 110070
    python3 watch.py --pin 110070 --file products.txt --interval 300

Telegram credentials are read from .env (TG_TOKEN / TG_CHAT).
Add products to products.txt — one URL per line, # for comments.
"""

import argparse
import logging
import os
import subprocess
import sys
import time
from datetime import datetime
from typing import Optional

import requests as _requests
from dotenv import load_dotenv
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn
from rich.table import Table
from rich import box

load_dotenv()

from blinkit_client import Product, pin_to_latlon
from playwright_client import BlinkitPlaywrightClient

console = Console()

_PIN_FALLBACK: dict[str, tuple[float, float]] = {
    "110070": (28.5188, 77.1548),
    "400001": (18.9387, 72.8353),
    "560001": (12.9716, 77.5946),
    "600001": (13.0827, 80.2707),
    "700001": (22.5726, 88.3639),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_urls(path: str) -> list[str]:
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        console.print(f"[bold red]Error:[/bold red] '{path}' not found.")
        sys.exit(1)
    urls = [l.strip() for l in lines if l.strip() and not l.startswith("#")]
    if not urls:
        console.print(f"[bold red]Error:[/bold red] No product URLs found in '{path}'.")
        sys.exit(1)
    return urls


def resolve_location(args: argparse.Namespace) -> tuple[float, float]:
    if args.lat is not None and args.lon is not None:
        return args.lat, args.lon
    pin = args.pin.strip()
    if pin in _PIN_FALLBACK:
        lat, lon = _PIN_FALLBACK[pin]
        console.print(f"[dim]Using cached coordinates for {pin}: ({lat}, {lon})[/dim]")
        return lat, lon
    console.print(f"[dim]Resolving PIN {pin} via Nominatim…[/dim]")
    return pin_to_latlon(pin)


def _notify_desktop(product: Product) -> None:
    msg = f"{product.product_name} — ₹{product.price:.0f}"
    try:
        subprocess.run(
            ["notify-send", "--urgency=critical", "--expire-time=0",
             "Blinkit: Back in Stock!", msg],
            check=False, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


def _notify_telegram(token: str, chat_id: str, product: Product, url: str) -> None:
    text = (
        f"*Blinkit: Back in Stock!*\n"
        f"[{product.product_name}]({url})\n"
        f"Status: *{product.availability_status}*\n"
        f"Price: ₹{product.price:.0f}"
    )
    try:
        _requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text,
                  "parse_mode": "Markdown", "disable_web_page_preview": False},
            timeout=10,
        )
    except Exception as exc:
        console.print(f"[yellow]Telegram send failed: {exc}[/yellow]")


def notify(product: Product, url: str, tg_token: Optional[str], tg_chat: Optional[str]) -> None:
    print("\a", end="", flush=True)
    _notify_desktop(product)
    if tg_token and tg_chat:
        _notify_telegram(tg_token, tg_chat, product, url)


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def build_status_table(results: list[tuple[str, Optional[Product]]], ts: str) -> Table:
    table = Table(
        title=f"Blinkit Watch  [{ts}]",
        box=box.ROUNDED,
        header_style="bold cyan",
        border_style="bright_black",
        row_styles=["", "dim"],
    )
    table.add_column("#", width=3, justify="right", style="bold")
    table.add_column("Product", min_width=30)
    table.add_column("Brand", min_width=12)
    table.add_column("Price (₹)", justify="right")
    table.add_column("Status", min_width=16)

    for i, (url, product) in enumerate(results, 1):
        if product is None:
            table.add_row(str(i), f"[dim]{url}[/dim]", "—", "—", "[red]Fetch failed[/red]")
            continue
        if product.availability_status == "Out of Stock":
            status_cell = f"[red]{product.availability_status}[/red]"
        elif product.availability_status == "In Stock":
            status_cell = f"[green]{product.availability_status}[/green]"
        else:
            status_cell = f"[yellow]{product.availability_status}[/yellow]"
        table.add_row(
            str(i),
            product.product_name,
            product.brand,
            f"[bold green]{product.price:.0f}[/bold green]",
            status_cell,
        )
    return table


# ---------------------------------------------------------------------------
# Watch loop
# ---------------------------------------------------------------------------

def _fetch_with_progress(
    client: BlinkitPlaywrightClient, urls: list[str]
) -> list[tuple[str, Optional[Product]]]:
    results: list[tuple[str, Optional[Product]]] = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=28),
        TextColumn("[cyan]{task.completed}[/cyan]/[cyan]{task.total}[/cyan]"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Starting browser…", total=len(urls))

        def on_result(done: int, total: int, url: str, product: Optional[Product]) -> None:
            name = product.product_name[:32] if product else "fetch failed"
            progress.update(task, completed=done, description=f"[dim]{name}[/dim]")
            results.append((url, product))

        client.fetch_all(urls, on_result=on_result)

    return results


def _countdown(seconds: int) -> None:
    with Live(console=console, refresh_per_second=2) as live:
        for remaining in range(seconds, 0, -1):
            mins, secs = divmod(remaining, 60)
            live.update(
                f"[dim]  Next check in [bold white]{mins:02d}:{secs:02d}[/bold white]"
                f"  — edit [italic]products.txt[/italic] to add/remove products[/dim]"
            )
            time.sleep(1)


def watch(
    urls: list[str],
    lat: float,
    lon: float,
    interval: int,
    tg_token: Optional[str],
    tg_chat: Optional[str],
) -> None:
    client = BlinkitPlaywrightClient(lat, lon)
    last_status: dict[str, Optional[str]] = {url: None for url in urls}

    channels = ["terminal"]
    if tg_token and tg_chat:
        channels.append("Telegram")

    console.print(
        Panel(
            f"[bold]Products:[/bold] {len(urls)}\n"
            f"[bold]Location:[/bold] ({lat:.4f}, {lon:.4f})\n"
            f"[bold]Interval:[/bold] {interval}s\n"
            f"[bold]Notify via:[/bold] {', '.join(channels)}\n"
            f"[dim]Press Ctrl+C to stop.[/dim]",
            title="[bold cyan]Blinkit Availability Watcher[/bold cyan]",
            border_style="cyan",
        )
    )

    while True:
        ts = datetime.now().strftime("%H:%M:%S")
        console.print(f"\n[bold]Checking {len(urls)} product(s) at {ts}…[/bold]")

        results = _fetch_with_progress(client, urls)
        console.print(build_status_table(results, ts))

        for url, product in results:
            if product is None:
                continue
            status = product.availability_status
            in_stock = status != "Out of Stock"

            if in_stock and last_status[url] != status:
                console.print(
                    Panel(
                        f"[bold green]{product.product_name}[/bold green] is now "
                        f"[bold green]{status}[/bold green]!\n"
                        f"Price: ₹{product.price:.0f}  |  {url}",
                        title="[bold green]IN STOCK — BUY NOW[/bold green]",
                        border_style="green",
                    )
                )
                notify(product, url, tg_token, tg_chat)

            last_status[url] = status

        _countdown(interval)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="blinkit-watch",
        description="Watch Blinkit products and notify when they come in stock.",
    )
    parser.add_argument(
        "--file", "-f", default="products.txt",
        help="Path to product URL list (default: products.txt)",
    )
    parser.add_argument(
        "--interval", "-i", type=int, default=300,
        help="Poll interval in seconds (default: 300)",
    )

    loc = parser.add_mutually_exclusive_group(required=True)
    loc.add_argument("--pin", help="Indian PIN code, e.g. 110070")
    loc.add_argument("--lat", type=float, help="Latitude (use with --lon)")
    parser.add_argument("--lon", type=float)

    tg = parser.add_argument_group("Telegram (read from .env by default)")
    tg.add_argument("--tg-token", default=os.getenv("TG_TOKEN"))
    tg.add_argument("--tg-chat", default=os.getenv("TG_CHAT"))

    args = parser.parse_args()
    if args.lat is not None and args.lon is None:
        parser.error("--lat requires --lon")
    if args.lon is not None and args.lat is None:
        parser.error("--lon requires --lat")
    return args


def _setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    file_handler = logging.FileHandler("blinkit.log", mode="w", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.WARNING)
    stream_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)


def main() -> None:
    args = parse_args()
    _setup_logging()

    urls = load_urls(args.file)

    try:
        lat, lon = resolve_location(args)
    except ValueError as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        sys.exit(1)

    try:
        watch(urls, lat, lon, args.interval, args.tg_token, args.tg_chat)
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped.[/dim]")


if __name__ == "__main__":
    main()
