#!/usr/bin/env python3
"""Top 10 S&P 500 and Nasdaq-100 gainers since the prior year-end close."""

import argparse
import datetime
import importlib.util
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
NASDAQ100_URL = "https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies"
INDEX_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
}
COLUMNS = ["Symbol", "Price", "Day %", "YTD"]
ROOT = Path(__file__).resolve().parents[1]


def bars_module():
    path = ROOT / "inside-day-screener" / "screener.py"
    spec = importlib.util.spec_from_file_location("inside_day_bars", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def wiki_tickers(url: str) -> set[str]:
    response = requests.get(url, headers=INDEX_HEADERS, timeout=30)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    best: set[str] = set()
    for table in soup.select("table.wikitable"):
        header_row = table.find("tr")
        if header_row is None:
            continue
        headers = [cell.get_text(" ", strip=True) for cell in header_row.find_all(["th", "td"])]
        key = next((index for index, name in enumerate(headers) if name in {"Symbol", "Ticker"}), None)
        if key is None:
            continue
        symbols: set[str] = set()
        for row in table.find_all("tr")[1:]:
            cells = row.find_all(["td", "th"])
            if len(cells) <= key:
                continue
            symbol = cells[key].get_text(" ", strip=True).split("[", 1)[0].strip().upper()
            symbol = symbol.replace(".", "-")
            if re.fullmatch(r"[A-Z][A-Z0-9-]{0,9}", symbol):
                symbols.add(symbol)
        if len(symbols) < 90:
            continue
        if len(symbols) > len(best):
            best = symbols
    if len(best) < 90:
        raise RuntimeError(f"No ticker table at {url}")
    return best


def session_closes(module, api_key: str, day: datetime.date) -> dict[str, float]:
    bars = module.parse_bars(module.fetch_grouped(api_key, day))
    return {symbol: bar[0] for symbol, bar in bars.items() if bar[0] > 0}


def session_on_or_before(module, api_key: str, day: datetime.date) -> tuple[datetime.date, dict[str, float]]:
    cursor = day
    for _ in range(12):
        if cursor.weekday() < 5:
            closes = session_closes(module, api_key, cursor)
            if closes:
                return cursor, closes
        cursor -= datetime.timedelta(days=1)
    raise RuntimeError(f"No daily bars on or before {day.isoformat()}")


def percent(current: float, previous: float) -> float:
    return (current - previous) / previous * 100


def format_percent(value: float) -> str:
    return f"{value:+.2f}%"


def price_series_holds(api_key: str, symbol: str, start: datetime.date, end: datetime.date) -> bool:
    """A ticker change shows up as one adjusted session several times the prior close."""
    url = f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/1/day/{start.isoformat()}/{end.isoformat()}"
    params = {"adjusted": "true", "sort": "asc", "limit": 50000, "apiKey": api_key}
    response = requests.get(url, params=params, timeout=30)
    if response.status_code == 429:
        time.sleep(15)
        response = requests.get(url, params=params, timeout=30)
    rows = response.json().get("results") or []
    previous = None
    for row in rows:
        close = row.get("c")
        if close is None or close <= 0:
            continue
        if previous is not None and close / previous > 4:
            return False
        previous = float(close)
    return previous is not None


def top_gainers(
    api_key: str,
    symbols: set[str],
    closes: dict[str, float],
    prior: dict[str, float],
    base: dict[str, float],
    start: datetime.date,
    end: datetime.date,
) -> list[list[str]]:
    ranked = []
    for symbol in symbols:
        close = closes.get(symbol)
        yesterday = prior.get(symbol)
        opening = base.get(symbol)
        if close is None or yesterday is None or opening is None:
            continue
        ranked.append((percent(close, opening), percent(close, yesterday), close, symbol))
    ranked.sort(reverse=True)
    rows = []
    for ytd, day, close, symbol in ranked:
        if not price_series_holds(api_key, symbol, start, end):
            print(f"Skipped {symbol}: year-to-date price series is not continuous", file=sys.stderr)
            continue
        rows.append([symbol, f"{close:.2f}", format_percent(day), format_percent(ytd)])
        if len(rows) == 10:
            break
    return rows


def leaderboards(screen_day: datetime.date) -> list[tuple[str, list[list[str]]]]:
    """S&P 500 and QQQ tables for screen_day. Each row is Symbol, Price, Day %, YTD."""
    module = bars_module()
    api_key = module.load_api_key()
    _, closes = session_on_or_before(module, api_key, screen_day)
    _, prior = session_on_or_before(module, api_key, screen_day - datetime.timedelta(days=1))
    year_end = datetime.date(screen_day.year - 1, 12, 31)
    base_day, base = session_on_or_before(module, api_key, year_end)
    groups = (
        ("S&P 500", wiki_tickers(SP500_URL)),
        ("QQQ", wiki_tickers(NASDAQ100_URL)),
    )
    return [
        (title, top_gainers(api_key, symbols, closes, prior, base, base_day, screen_day))
        for title, symbols in groups
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Print the S&P 500 and QQQ year-to-date leaders.")
    parser.add_argument("--date", default="", help="Session date, YYYY-MM-DD. Defaults to the last session.")
    args = parser.parse_args()
    if args.date:
        screen_day = datetime.date.fromisoformat(args.date)
    else:
        screen_day = datetime.datetime.now().date()
        if screen_day.weekday() >= 5:
            screen_day -= datetime.timedelta(days=screen_day.weekday() - 4)
    for title, rows in leaderboards(screen_day):
        print(title)
        print("  ".join(COLUMNS))
        for row in rows:
            print("  ".join(row))
        print()


if __name__ == "__main__":
    main()
