"""
Blinkit price-availability CLI.

Quick test (hardcoded PIN 110070 = Vasant Kunj, Delhi):
    python main.py --query milk --pin 110070

Custom location via coordinates:
    python main.py --query "amul butter" --lat 28.6139 --lon 77.2090
"""

import argparse
import sys

from rich.console import Console
from rich.table import Table
from rich import box

from blinkit_client import BlinkitClient, Product, pin_to_latlon

console = Console()

# Hardcoded fallback for offline / quick testing so Nominatim is not called
_PIN_FALLBACK: dict[str, tuple[float, float]] = {
    "110070": (28.5188, 77.1548),  # Vasant Kunj, Delhi
    "400001": (18.9387, 72.8353),  # Mumbai CST
    "560001": (12.9716, 77.5946),  # Bangalore MG Road
    "600001": (13.0827, 80.2707),  # Chennai
    "700001": (22.5726, 88.3639),  # Kolkata
}


def resolve_location(args: argparse.Namespace) -> tuple[float, float]:
    """Return (lat, lon) from --lat/--lon, --pin, or the hardcoded fallback table."""
    if args.lat is not None and args.lon is not None:
        return args.lat, args.lon

    pin = args.pin.strip()
    if pin in _PIN_FALLBACK:
        lat, lon = _PIN_FALLBACK[pin]
        console.print(f"[dim]Using cached coordinates for {pin}: ({lat}, {lon})[/dim]")
        return lat, lon

    console.print(f"[dim]Resolving PIN {pin} via Nominatim…[/dim]")
    return pin_to_latlon(pin)


def build_table(products: list[Product], query: str) -> Table:
    table = Table(
        title=f'Blinkit search: "{query}"',
        box=box.ROUNDED,
        show_header=True,
        header_style="bold cyan",
        border_style="bright_black",
        row_styles=["", "dim"],
    )

    table.add_column("#", style="bold", width=3, justify="right")
    table.add_column("Product", min_width=28)
    table.add_column("Brand", min_width=14)
    table.add_column("Price (₹)", justify="right")
    table.add_column("MRP (₹)", justify="right")
    table.add_column("Status", min_width=16)
    table.add_column("ETA", justify="center")

    for i, p in enumerate(products, start=1):
        # Colour-code availability
        if p.availability_status == "In Stock":
            status_cell = f"[green]{p.availability_status}[/green]"
        elif p.availability_status == "Out of Stock":
            status_cell = f"[red]{p.availability_status}[/red]"
        else:
            status_cell = f"[yellow]{p.availability_status}[/yellow]"

        # Strikethrough MRP when there's a discount
        mrp_cell = (
            f"[strike]{p.mrp:.2f}[/strike]"
            if p.mrp > p.price
            else f"{p.mrp:.2f}"
        )

        table.add_row(
            str(i),
            p.product_name,
            p.brand,
            f"[bold green]{p.price:.2f}[/bold green]",
            mrp_cell,
            status_cell,
            p.delivery_eta,
        )

    return table


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="blinkit-bot",
        description="Check real-time Blinkit product availability and pricing.",
    )
    parser.add_argument("--query", "-q", required=True, help='Search term, e.g. "milk"')

    loc = parser.add_mutually_exclusive_group(required=True)
    loc.add_argument("--pin", help="Indian PIN code, e.g. 110070")
    loc.add_argument("--lat", type=float, help="Latitude (use with --lon)")

    parser.add_argument("--lon", type=float, help="Longitude (use with --lat)")
    parser.add_argument(
        "--results", "-n", type=int, default=20,
        help="Max results to display (default: 20)",
    )

    args = parser.parse_args()

    if args.lat is not None and args.lon is None:
        parser.error("--lat requires --lon")
    if args.lon is not None and args.lat is None:
        parser.error("--lon requires --lat")

    return args


def main() -> None:
    args = parse_args()

    # ---- resolve location ------------------------------------------------
    try:
        lat, lon = resolve_location(args)
    except ValueError as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        sys.exit(1)

    console.print(f"[dim]Querying Blinkit at ({lat:.4f}, {lon:.4f}) for '{args.query}'…[/dim]")

    # ---- fetch (requests → Playwright fallback on 403) -------------------
    client = BlinkitClient(lat, lon)
    try:
        products = client.search(args.query)
    except PermissionError:
        console.print("[yellow]requests blocked (403) — switching to Playwright…[/yellow]")
        try:
            from playwright_client import BlinkitPlaywrightClient
            pw_client = BlinkitPlaywrightClient(lat, lon)
            products = pw_client.search(args.query)
        except ImportError:
            console.print("[bold red]Error:[/bold red] playwright not installed. Run: pip install playwright && playwright install chromium")
            sys.exit(1)
    except ValueError as exc:
        msg = str(exc)
        if "not serviceable" in msg.lower():
            console.print("[bold red]Error:[/bold red] Location not serviceable by Blinkit.")
        else:
            console.print(f"[bold red]Error:[/bold red] {msg}")
        sys.exit(1)
    except ConnectionError as exc:
        console.print(f"[bold red]Network error:[/bold red] {exc}")
        sys.exit(1)

    # ---- output ----------------------------------------------------------
    if not products:
        console.print(
            f"[yellow]No products found for '{args.query}' at the specified location.[/yellow]"
        )
        sys.exit(0)

    products = products[: args.results]
    console.print(build_table(products, args.query))
    console.print(f"[dim]{len(products)} result(s) shown.[/dim]")


if __name__ == "__main__":
    main()
