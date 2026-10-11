#!/usr/bin/env python3
"""Print the 10 tickers with the most option contracts traded on a session.

Volume is the OCC total for equity and ETF options, calls and puts on every
exchange. OCC lists both sides of each trade, so the count is half of that sum,
which matches OCC's published contract volume. Index options such as SPX, and
index funds such as SPY, QQQ, and IWM are left out, as are EWZ and TLT.
"""

import argparse
import os
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

TOP_N = 10
EASTERN = ZoneInfo("America/New_York")
OCC_URL = "https://marketdata.theocc.com/volume-query"
GROUPED_URL = "https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/{day}"
FINVIZ_QUOTE_URL = "https://finviz.com/quote.ashx"
KEY_FILE = Path.home() / ".config" / "two-day-screener" / "env"
NASDAQ_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
INDEX_ROOTS = {"SPX", "SPXW", "NDX", "NDXP", "RUT", "RUTW", "VIX", "XSP", "DJX", "OEX"}
SKIPPED = {"EWZ", "TLT"}
INDEX_NAME = re.compile(
    r"S&P 500|S&P 100|Russell 2000|Russell 1000|Russell 3000|Dow Jones|Nasdaq-?100|\bQQQ\b",
    re.IGNORECASE,
)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
}
COLUMNS = [
    "Date",
    "Symbol",
    "Price",
    "% Change",
    "Options Volume",
    "Volume",
    "Country",
    "Market Cap",
    "Short Float",
]


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def load_listings() -> dict[str, tuple[str, str]]:
    """Symbol to (ETF flag, security name) from the Nasdaq trader files."""
    listings: dict[str, tuple[str, str]] = {}
    sources = [(NASDAQ_LISTED, "Symbol"), (OTHER_LISTED, "ACT Symbol")]
    for url, symbol_column in sources:
        response = requests.get(url, headers=HEADERS, timeout=30)
        response.raise_for_status()
        lines = [
            line
            for line in response.text.splitlines()
            if line and not line.startswith("File Creation Time")
        ]
        header = lines[0].split("|")
        symbol_index = header.index(symbol_column)
        name_index = header.index("Security Name")
        etf_index = header.index("ETF")
        for line in lines[1:]:
            parts = line.split("|")
            if len(parts) <= max(symbol_index, name_index, etf_index):
                continue
            symbol = parts[symbol_index].strip().upper()
            if symbol and symbol != "N/A":
                listings[symbol] = (parts[etf_index], parts[name_index])
    return listings


def is_index(symbol: str, listings: dict[str, tuple[str, str]]) -> bool:
    if symbol in INDEX_ROOTS:
        return True
    listed = listings.get(symbol)
    if listed is None:
        return False
    etf, name = listed
    return etf == "Y" and INDEX_NAME.search(name) is not None


def fetch_volumes(day: date) -> dict[str, int]:
    """Contract volume by underlying. Empty when that session has no report."""
    response = requests.get(
        OCC_URL,
        params={
            "reportDate": day.strftime("%Y%m%d"),
            "format": "csv",
            "volumeQueryType": "O",
            "symbolType": "ALL",
            "reportType": "D",
            "accountType": "ALL",
            "productKind": "OSTK",
            "porc": "BOTH",
        },
        headers=HEADERS,
        timeout=120,
    )
    if response.status_code != 200:
        raise RuntimeError(f"OCC returned HTTP {response.status_code} for {day.isoformat()}")
    wanted = day.strftime("%m/%d/%Y")
    totals: Counter[str] = Counter()
    for line in response.text.splitlines():
        line = line.strip().rstrip(",")
        if not line or line.startswith("quantity"):
            continue
        parts = line.split(",")
        if len(parts) < 7 or parts[6] != wanted:
            continue
        symbol = parts[1].strip()
        if not symbol:
            continue
        totals[symbol] += int(parts[0])
    return {symbol: quantity // 2 for symbol, quantity in totals.items()}


def latest_session(as_of: date) -> tuple[date, dict[str, int]]:
    cursor = as_of
    for _ in range(12):
        if cursor.weekday() < 5:
            log(f"Loading option volume for {cursor.isoformat()}...")
            volumes = fetch_volumes(cursor)
            if volumes:
                log(f"{cursor.isoformat()}: {len(volumes)} symbols")
                return cursor, volumes
            log(f"{cursor.isoformat()}: no option volume")
        cursor -= timedelta(days=1)
    raise RuntimeError(f"No option volume found on or before {as_of.isoformat()}")


def load_api_key() -> str:
    key = os.environ.get("POLYGON_API_KEY", "").strip()
    if key:
        return key
    if KEY_FILE.exists():
        for line in KEY_FILE.read_text(encoding="utf-8").splitlines():
            if line.startswith("POLYGON_API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
                if key:
                    return key
    raise RuntimeError(f"Polygon API key not found in {KEY_FILE}")


def fetch_grouped(api_key: str, day: date) -> dict[str, tuple[float, float]]:
    """Close and share volume by symbol for one session."""
    for attempt in range(4):
        response = requests.get(
            GROUPED_URL.format(day=day.isoformat()),
            params={"adjusted": "true", "apiKey": api_key},
            timeout=60,
        )
        if response.status_code == 429:
            time.sleep(15 * (attempt + 1))
            continue
        try:
            body = response.json()
        except ValueError:
            body = {}
        message = str(body.get("message") or body.get("error") or "")
        if response.status_code == 403 and "before end of day" in message.lower():
            return {}
        if response.status_code != 200:
            raise RuntimeError(message or f"Polygon returned HTTP {response.status_code} for {day.isoformat()}")
        bars: dict[str, tuple[float, float]] = {}
        for row in body.get("results") or []:
            symbol = str(row.get("T") or "").strip().upper()
            close = row.get("c")
            volume = row.get("v")
            if not symbol or close is None or volume is None:
                continue
            bars[symbol] = (float(close), float(volume))
        return bars
    raise RuntimeError(f"Polygon rate limit while fetching {day.isoformat()}")


def previous_bars(api_key: str, screen_day: date) -> dict[str, tuple[float, float]]:
    cursor = screen_day - timedelta(days=1)
    for _ in range(10):
        if cursor.weekday() < 5:
            bars = fetch_grouped(api_key, cursor)
            if bars:
                return bars
        cursor -= timedelta(days=1)
    return {}


def lookup_bar(bars: dict[str, tuple[float, float]], symbol: str) -> tuple[float | None, float | None]:
    for candidate in (symbol, symbol.replace(".", "-"), symbol.replace("-", ".")):
        bar = bars.get(candidate)
        if bar is not None:
            return bar
    return None, None


def finviz_quote(ticker: str) -> tuple[dict[str, str], str]:
    last_error: Exception | None = None
    session = requests.Session()
    for attempt in range(5):
        try:
            response = session.get(FINVIZ_QUOTE_URL, params={"t": ticker}, headers=HEADERS, timeout=20)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            cells = soup.select("td.snapshot-td2")
            if not cells:
                time.sleep(1.5 * (attempt + 1))
                continue
            snapshot: dict[str, str] = {}
            for index in range(0, len(cells) - 1, 2):
                label = cells[index].get_text(" ", strip=True)
                value = cells[index + 1].get_text(" ", strip=True)
                if label:
                    snapshot[label] = value if value and value != "-" else "—"
            country_link = soup.select_one("a.quote-header_category[href*='geo_']")
            country = country_link.get_text(" ", strip=True) if country_link else ""
            if country == "United States":
                country = "USA"
            return snapshot, country
        except requests.RequestException as error:
            last_error = error
            time.sleep(1.5 * (attempt + 1))
    if last_error:
        raise last_error
    return {}, ""


def fmt_price(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.2f}"


def fmt_change(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:+.2f}%"


def fmt_volume(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{int(value):,}"


def quote_rows(
    api_key: str,
    screen_day: date,
    ranked: list[tuple[str, int]],
) -> list[dict]:
    log("Fetching prices and volume...")
    bars = fetch_grouped(api_key, screen_day)
    prior = previous_bars(api_key, screen_day)
    log("Fetching country, market cap, and short float...")
    quotes: dict[str, tuple[dict[str, str], str]] = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(finviz_quote, symbol): symbol for symbol, _ in ranked}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                quotes[symbol] = future.result()
            except Exception:
                quotes[symbol] = ({}, "")
    rows = []
    for symbol, options_volume in ranked:
        price, volume = lookup_bar(bars, symbol)
        prior_close, _ = lookup_bar(prior, symbol)
        change = (price - prior_close) / prior_close * 100 if price and prior_close else None
        snapshot, country = quotes.get(symbol, ({}, ""))
        rows.append(
            {
                "Date": screen_day.isoformat(),
                "Symbol": symbol,
                "Price": fmt_price(price),
                "% Change": fmt_change(change),
                "Volume": fmt_volume(volume),
                "Options Volume": fmt_volume(options_volume),
                "Country": country or "—",
                "Market Cap": snapshot.get("Market Cap") or "—",
                "Short Float": snapshot.get("Short Float") or "—",
            }
        )
    return rows


def print_table(rows: list[dict]) -> None:
    widths = {column: max(len(column), *(len(str(row[column])) for row in rows)) for column in COLUMNS}
    print("  ".join(column.center(widths[column]) for column in COLUMNS))
    print("  ".join("-" * widths[column] for column in COLUMNS))
    for row in rows:
        print("  ".join(str(row[column]).center(widths[column]) for column in COLUMNS))


def run_screen(as_of: date) -> int:
    screen_day, volumes = latest_session(as_of)
    listings = load_listings()
    ranked = [
        item
        for item in sorted(volumes.items(), key=lambda item: item[1], reverse=True)
        if item[0] not in SKIPPED and not is_index(item[0], listings)
    ][:TOP_N]
    print_table(quote_rows(load_api_key(), screen_day, ranked))
    return 0


def mark_handled(today: str) -> None:
    """Record that the combined daily email included this screen."""
    stamp = Path.home() / ".config" / "options-volume-screener" / "last-email-date"
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(today + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Print the 10 non-index tickers with the most option volume on a session."
    )
    parser.add_argument("--date", help="Session date, YYYY-MM-DD. Defaults to the latest session.")
    args = parser.parse_args()
    as_of = date.fromisoformat(args.date) if args.date else datetime.now(EASTERN).date()
    return run_screen(as_of)


if __name__ == "__main__":
    raise SystemExit(main())
