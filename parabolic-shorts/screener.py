#!/usr/bin/env python3
"""List US-listed stocks up at least 300% over the last five trading sessions.

The gain is the latest close versus the close five sessions earlier.
Warrants, test issues, and ETFs are left out.
"""

import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

MIN_GAIN = 300.0
SESSIONS_BACK = 5
KEY_FILE = Path.home() / ".config" / "two-day-screener" / "env"
EASTERN = ZoneInfo("America/New_York")
GROUPED_URL = "https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/{day}"
NASDAQ_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
}
Bar = tuple[float, float, float, float, float]


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


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


def normalize_symbol(symbol: str) -> str:
    return symbol.strip().upper().replace(".", "-").replace("/", "-")


def read_symbol_file(url: str, cache_name: str) -> str:
    cache = KEY_FILE.parent / cache_name
    text = ""
    try:
        response = requests.get(url, headers=HEADERS, timeout=30)
        response.raise_for_status()
        text = response.text
    except requests.RequestException:
        text = ""
    if "Security Name|" in text and not text.lstrip().startswith("<"):
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(text, encoding="utf-8")
        return text
    if cache.exists():
        log(f"Using saved {cache_name}; the live Nasdaq file was unavailable")
        return cache.read_text(encoding="utf-8")
    raise RuntimeError(f"Nasdaq symbol file {cache_name} was unavailable")


def load_symbols() -> set[str]:
    sources = [
        (NASDAQ_LISTED, "nasdaqlisted.txt", "Symbol"),
        (OTHER_LISTED, "otherlisted.txt", "ACT Symbol"),
    ]
    symbols: list[str] = []
    for url, cache_name, symbol_column in sources:
        lines = [
            line
            for line in read_symbol_file(url, cache_name).splitlines()
            if line and not line.startswith("File Creation Time")
        ]
        header = lines[0].split("|")
        symbol_index = header.index(symbol_column)
        name_index = header.index("Security Name")
        test_index = header.index("Test Issue")
        etf_index = header.index("ETF")
        for line in lines[1:]:
            parts = line.split("|")
            if len(parts) <= max(symbol_index, name_index, test_index, etf_index):
                continue
            if parts[test_index] == "Y" or parts[etf_index] == "Y":
                continue
            if "warrant" in parts[name_index].lower():
                continue
            symbol = normalize_symbol(parts[symbol_index])
            if symbol and symbol != "N/A":
                symbols.append(symbol)
    return set(symbols)


def fetch_grouped(api_key: str, day: date) -> list[dict]:
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
            return []
        if response.status_code != 200:
            raise RuntimeError(message or f"Polygon returned HTTP {response.status_code} for {day.isoformat()}")
        if body.get("status") not in {None, "OK", "DELAYED"}:
            raise RuntimeError(message or str(body.get("status")))
        return body.get("results") or []
    raise RuntimeError(f"Polygon rate limit while fetching {day.isoformat()}")


def parse_bars(rows: list[dict]) -> dict[str, Bar]:
    bars: dict[str, Bar] = {}
    for row in rows:
        raw = str(row.get("T") or "")
        if not raw or raw != raw.upper():
            continue
        symbol = normalize_symbol(raw)
        close, volume, high, low, open_price = (
            row.get("c"),
            row.get("v"),
            row.get("h"),
            row.get("l"),
            row.get("o"),
        )
        if not symbol or None in (close, volume, high, low, open_price):
            continue
        bars[symbol] = (float(close), float(volume), float(high), float(low), float(open_price))
    return bars


def load_sessions(api_key: str, as_of: date) -> list[tuple[date, dict[str, Bar]]]:
    """Recent sessions, newest first. Index 5 is five trading sessions before the latest."""
    sessions: list[tuple[date, dict[str, Bar]]] = []
    cursor = as_of
    looked = 0
    needed = SESSIONS_BACK + 1
    while looked < needed + 12 and len(sessions) < needed:
        if cursor.weekday() < 5:
            looked += 1
            rows = fetch_grouped(api_key, cursor)
            if rows:
                sessions.append((cursor, parse_bars(rows)))
                log(f"{cursor.isoformat()}: {len(sessions[-1][1])} symbols")
            else:
                log(f"{cursor.isoformat()}: no bars")
        cursor -= timedelta(days=1)
    if len(sessions) <= SESSIONS_BACK:
        raise RuntimeError("Need six sessions to measure a five-session gain")
    return sessions


def find_matches(
    universe: set[str],
    sessions: list[tuple[date, dict[str, Bar]]],
) -> list[tuple[str, float, float, float, float]]:
    _latest_day, latest = sessions[0]
    _base_day, base = sessions[SESSIONS_BACK]
    matches = []
    for symbol, (close, volume, _high, _low, _open) in latest.items():
        if symbol not in universe:
            continue
        prior = base.get(symbol)
        if prior is None or prior[0] <= 0:
            continue
        change = (close / prior[0] - 1.0) * 100.0
        if change < MIN_GAIN:
            continue
        matches.append((symbol, close, prior[0], change, volume))
    matches.sort(key=lambda row: row[3], reverse=True)
    return matches


def format_volume(volume: float) -> str:
    if volume >= 1_000_000:
        return f"{volume / 1_000_000:.1f}M"
    if volume >= 1_000:
        return f"{volume / 1_000:.1f}K"
    return f"{volume:.0f}"


def print_table(
    latest: date,
    base: date,
    matches: list[tuple[str, float, float, float, float]],
) -> None:
    print(f"{latest.isoformat()} close versus {base.isoformat()} close. Up at least {MIN_GAIN:.0f}%.")
    if not matches:
        print("No listed stocks cleared 300%.")
        return
    print(f"{'Symbol':<8} {'Last':>10} {'5 ago':>10} {'5-day':>10} {'Volume':>10}")
    for symbol, close, prior, change, volume in matches:
        print(f"{symbol:<8} {close:10.2f} {prior:10.2f} {change:9.0f}% {format_volume(volume):>10}")
    print(f"{len(matches)} names")


def main() -> None:
    as_of = datetime.now(EASTERN).date()
    api_key = load_api_key()
    universe = load_symbols()
    log(f"{len(universe)} listed stocks")
    sessions = load_sessions(api_key, as_of)
    matches = find_matches(universe, sessions)
    print_table(sessions[0][0], sessions[SESSIONS_BACK][0], matches)


if __name__ == "__main__":
    main()
