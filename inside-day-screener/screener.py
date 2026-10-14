#!/usr/bin/env python3
"""Scan US-listed stocks for a 30% gain over two or three trading days, 500k volume on the screen day, a 20M day in that window, and a top-5 volume day over nine months.

The day before the inside day must trade at least 5% from low to high, so a gap that then sits in a tight range does not qualify.
"""

import argparse
import datetime
import os
import re
import smtplib
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, timedelta
from email.message import EmailMessage
from html import escape
from io import BytesIO, StringIO
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape
from zipfile import ZIP_DEFLATED, ZipFile
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

MIN_RETURN = 0.30
MIN_VOLUME = 500_000
SPIKE_VOLUME = 20_000_000
MIN_UP_DAY_RANGE = 0.05
MAX_FLOAT = 30_000_000
MAX_MARKET_CAP = 1_000_000_000
DETAIL_WORKERS = 6
KEY_FILE = Path.home() / ".config" / "two-day-screener" / "env"
EMAIL_STAMP = KEY_FILE.parent / "last-email-date"
EMAIL_PASSWORD_WARNING = KEY_FILE.parent / "last-password-warning"
EMAIL_LOG = KEY_FILE.parent / "email.log"
EASTERN = ZoneInfo("America/New_York")
SEND_AFTER = (16, 16)
GIVE_UP = (18, 0)
GROUPED_URL = "https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/{day}"
DAILY_URL = "https://api.polygon.io/v2/aggs/ticker/{symbol}/range/1/day/{start}/{end}"
TICKERS_URL = "https://api.polygon.io/v3/reference/tickers"
EXCLUDED_TYPES = {"ETN", "ETS", "ETV", "WARRANT"}

NASDAQ_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
FINVIZ_QUOTE_URL = "https://finviz.com/quote.ashx"
FINVIZ_HEADERS = {
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
    "3-Day %",
    "Volume",
    "Avg Volume",
    "Country",
    "Finviz Market Cap",
    "Finviz Shares Float",
    "Finviz Short % of Float",
]
RESULTS_PATH = Path(__file__).resolve().parent / "inside-day-screener-results.xlsx"
RESULTS_COLUMNS = [
    "Date",
    "Symbol",
    "Price",
    "% Change",
    "Next Day %",
    "Max Next Day",
    "3-Day %",
    "Volume",
    "Avg Volume",
    "Country",
    "Finviz Market Cap",
    "Finviz Shares Float",
    "Finviz Short % of Float",
]
BLANK_CELL = {"", "—", "-", "–"}


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def load_api_key() -> str:
    key = os.environ.get("POLYGON_API_KEY", "").strip()
    if key:
        return key
    if KEY_FILE.exists():
        for line in KEY_FILE.read_text(encoding="utf-8").splitlines():
            if line.startswith("POLYGON_API_KEY="):
                key = line.split("=", 1)[1].strip()
                if key:
                    return key
    raise RuntimeError(f"Polygon API key not found in {KEY_FILE}")


def read_symbol_file(url: str, cache_name: str) -> str:
    cache = KEY_FILE.parent / cache_name
    text = ""
    try:
        response = requests.get(url, headers=FINVIZ_HEADERS, timeout=30)
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
        for line in lines[1:]:
            parts = line.split("|")
            if len(parts) <= max(symbol_index, name_index, test_index):
                continue
            if parts[test_index] == "Y":
                continue
            if "warrant" in parts[name_index].lower():
                continue
            symbol = normalize_symbol(parts[symbol_index])
            if symbol and symbol != "N/A":
                symbols.append(symbol)
    return set(symbols)


def load_listed_on(api_key: str, screen_day: date) -> set[str]:
    """Exchange-listed stocks on screen_day, including names later delisted or moved to OTC."""
    url = TICKERS_URL
    params: dict | None = {
        "market": "stocks",
        "date": screen_day.isoformat(),
        "limit": 1000,
        "apiKey": api_key,
    }
    symbols: list[str] = []
    while url:
        response = requests.get(url, params=params, timeout=60)
        try:
            body = response.json()
        except ValueError:
            body = {}
        message = str(body.get("message") or body.get("error") or "")
        if response.status_code != 200:
            raise RuntimeError(
                message or f"Polygon returned HTTP {response.status_code} for tickers on {screen_day.isoformat()}"
            )
        for row in body.get("results") or []:
            if row.get("type") in EXCLUDED_TYPES:
                continue
            name = str(row.get("name") or "")
            if "warrant" in name.lower():
                continue
            symbol = normalize_symbol(str(row.get("ticker") or ""))
            if symbol and symbol != "N/A":
                symbols.append(symbol)
        url = body.get("next_url")
        params = {"apiKey": api_key} if url else None
    return set(symbols)


def normalize_symbol(symbol: str) -> str:
    return symbol.strip().upper().replace(".", "-").replace("/", "-")


def recent_weekdays(count: int, as_of: date) -> list[date]:
    days: list[date] = []
    cursor = as_of
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return days


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


def parse_bars(rows: list[dict]) -> dict[str, tuple[float, float, float, float]]:
    bars: dict[str, tuple[float, float, float, float]] = {}
    for row in rows:
        symbol = normalize_symbol(str(row.get("T") or ""))
        close = row.get("c")
        volume = row.get("v")
        high = row.get("h")
        low = row.get("l")
        if not symbol or None in (close, volume, high, low):
            continue
        bars[symbol] = (float(close), float(volume), float(high), float(low))
    return bars


def load_sessions(
    api_key: str, as_of: date
) -> list[tuple[date, dict[str, tuple[float, float, float, float]]]]:
    sessions: list[tuple[date, dict[str, tuple[float, float, float, float]]]] = []
    for day in recent_weekdays(10, as_of):
        rows = fetch_grouped(api_key, day)
        if not rows:
            log(f"{day.isoformat()}: no bars")
            continue
        bars = parse_bars(rows)
        sessions.append((day, bars))
        log(f"{day.isoformat()}: {len(bars)} symbols")
        if len(sessions) == 4:
            break
    return sessions


def load_next_session(
    api_key: str, after: date
) -> tuple[date, dict[str, tuple[float, float, float, float]]] | None:
    """Return the next session after a historical screen date."""
    if after >= datetime.date.today():
        return None
    cursor = after + timedelta(days=1)
    while cursor <= datetime.date.today() and (cursor - after).days <= 10:
        if cursor.weekday() < 5:
            rows = fetch_grouped(api_key, cursor)
            if rows:
                bars = parse_bars(rows)
                log(f"Next session {cursor.isoformat()}: {len(bars)} symbols")
                return cursor, bars
            log(f"{cursor.isoformat()}: no bars")
        cursor += timedelta(days=1)
    return None


def traded_a_real_range(high: float, low: float) -> bool:
    """True when the high is at least 5% above the low.

    A gap that then trades in a tight range does not qualify.
    """
    if low <= 0:
        return False
    return (high - low) / low >= MIN_UP_DAY_RANGE


def find_matches(
    universe: set[str],
    sessions: list[tuple[date, dict[str, tuple[float, float, float, float]]]],
    next_session: tuple[date, dict[str, tuple[float, float, float, float]]] | None = None,
) -> list[dict]:
    if len(sessions) < 4:
        raise RuntimeError("Need four trading sessions to measure a two-day or three-day move")
    latest_day, latest = sessions[0]
    previous_day, previous = sessions[1]
    two_day_base_day, two_day_base = sessions[2]
    three_day_base_day, three_day_base = sessions[3]
    log(
        f"Comparing {two_day_base_day.isoformat()} or {three_day_base_day.isoformat()} "
        f"close to {latest_day.isoformat()}"
    )
    matches: list[dict] = []
    for symbol, (price, volume, high, low) in latest.items():
        if symbol not in universe or symbol not in previous or price <= 0:
            continue
        previous_close, previous_volume, previous_high, previous_low = previous[symbol]
        if previous_close <= 0 or volume < MIN_VOLUME:
            continue
        two_day_change = None
        two_day_close = two_day_base.get(symbol, (None,))[0]
        if two_day_close:
            two_day_change = (price - two_day_close) / two_day_close * 100
        three_day_change = None
        three_day_close = three_day_base.get(symbol, (None,))[0]
        if three_day_close:
            three_day_change = (price - three_day_close) / three_day_close * 100
        two_day_hit = two_day_change is not None and two_day_change >= MIN_RETURN * 100
        three_day_hit = three_day_change is not None and three_day_change >= MIN_RETURN * 100
        prior_day_volume = two_day_base.get(symbol, (None, 0))[1] or 0
        if two_day_hit and max(volume, previous_volume) < SPIKE_VOLUME:
            two_day_hit = False
        if three_day_hit and max(volume, previous_volume, prior_day_volume) < SPIKE_VOLUME:
            three_day_hit = False
        if not two_day_hit and not three_day_hit:
            continue
        day_change = (price - previous_close) / previous_close * 100
        inside_day = high <= previous_high * 1.02 and low >= previous_low * 0.98
        if not inside_day or not traded_a_real_range(previous_high, previous_low):
            continue
        next_day_change = None
        next_day_max = None
        if next_session is not None and price > 0:
            next_bar = next_session[1].get(symbol)
            if next_bar is not None:
                next_close, _, next_high, _ = next_bar
                if next_close is not None:
                    next_day_change = (next_close - price) / price * 100
                if next_high is not None:
                    next_day_max = (next_high - price) / price * 100
        matches.append(
            {
                "date": latest_day.isoformat(),
                "symbol": symbol,
                "price": price,
                "volume": volume,
                "day_change": day_change,
                "three_day_change": three_day_change,
                "next_day_change": next_day_change,
                "next_day_max": next_day_max,
                "two_day_hit": two_day_hit,
                "three_day_hit": three_day_hit,
                "prior_high": previous_high,
            }
        )
    return matches


def fmt_price(value) -> str:
    if value is None:
        return "—"
    return f"{float(value):,.2f}"


def fmt_volume(value) -> str:
    if value is None:
        return "—"
    return f"{int(value):,}"


def months_before(day: date, months: int) -> date:
    month = day.month - months
    year = day.year
    while month <= 0:
        month += 12
        year -= 1
    for offset in range(4):
        try:
            return date(year, month, day.day - offset)
        except ValueError:
            continue
    return date(year, month, 1)


def fetch_daily_volumes(api_key: str, symbol: str, start: date, end: date) -> list[tuple[date, float]]:
    url = DAILY_URL.format(symbol=symbol, start=start.isoformat(), end=end.isoformat())
    params: dict | None = {"adjusted": "true", "sort": "asc", "limit": 50000, "apiKey": api_key}
    volumes: list[tuple[date, float]] = []
    while url:
        response = requests.get(url, params=params, timeout=60)
        try:
            body = response.json()
        except ValueError:
            body = {}
        message = str(body.get("message") or body.get("error") or "")
        if response.status_code == 404:
            return []
        if response.status_code != 200:
            raise RuntimeError(message or f"Polygon returned HTTP {response.status_code} for {symbol}")
        for row in body.get("results") or []:
            volume = row.get("v")
            timestamp = row.get("t")
            if volume is None or timestamp is None:
                continue
            bar_day = datetime.datetime.fromtimestamp(timestamp / 1000, datetime.timezone.utc).date()
            volumes.append((bar_day, float(volume)))
        url = body.get("next_url")
        params = {"apiKey": api_key} if url else None
    return volumes


def is_top_volume_day(history: list[tuple[date, float]], window: set[date]) -> bool:
    """True when a window session is one of the five highest volume days in history."""
    if not history:
        return False
    ranked = sorted((volume for _, volume in history), reverse=True)
    cutoff = ranked[min(4, len(ranked) - 1)]
    return any(volume >= cutoff for day, volume in history if day in window)


def volume_profile(
    api_key: str,
    symbol: str,
    screen_day: date,
    two_day_dates: set[date],
    three_day_dates: set[date],
) -> tuple[float | None, bool, bool]:
    """Return the prior-year average volume and whether each window holds a top-5 day."""
    start = min(screen_day - timedelta(days=365), months_before(screen_day, 9))
    bars = fetch_daily_volumes(api_key, symbol, start, screen_day)
    prior = [volume for day, volume in bars if screen_day - timedelta(days=365) <= day < screen_day]
    average = sum(prior) / len(prior) if prior else None
    nine_month = [(day, volume) for day, volume in bars if day >= months_before(screen_day, 9)]
    return (
        average,
        is_top_volume_day(nine_month, two_day_dates),
        is_top_volume_day(nine_month, three_day_dates),
    )


def fmt_day_change(value) -> str:
    if value is None:
        return "—"
    return f"{float(value):+.2f}%"


def finviz_quote(session: requests.Session, ticker: str) -> tuple[dict[str, str], str]:
    response = session.get(
        FINVIZ_QUOTE_URL,
        params={"t": ticker},
        headers=FINVIZ_HEADERS,
        timeout=20,
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    cells = soup.select("td.snapshot-td2")
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


def fetch_row(match: dict) -> dict:
    session = requests.Session()
    try:
        snapshot, country = finviz_quote(session, match["symbol"])
    except Exception:
        snapshot, country = {}, ""
    return {
        "Date": match["date"],
        "Symbol": match["symbol"],
        "Country": country or "—",
        "Price": fmt_price(match["price"]),
        "Volume": fmt_volume(match["volume"]),
        "% Change": fmt_day_change(match["day_change"]),
        "Next Day %": fmt_day_change(match.get("next_day_change")),
        "Max Next Day": fmt_day_change(match.get("next_day_max")),
        "3-Day %": fmt_day_change(match["three_day_change"]),
        "Avg Volume": fmt_volume(match.get("average_volume")),
        "Finviz Market Cap": snapshot.get("Market Cap") or "—",
        "Finviz Shares Float": snapshot.get("Shs Float") or "—",
        "Finviz Short % of Float": snapshot.get("Short Float") or "—",
        "raw_price": match["price"],
        "prior_high": match.get("prior_high"),
        "raw_day_change": match["day_change"],
    }


def table_columns(include_next_day: bool) -> list[str]:
    columns = list(COLUMNS)
    if include_next_day:
        columns.insert(columns.index("% Change") + 1, "Next Day %")
        columns.insert(columns.index("Next Day %") + 1, "Max Next Day")
    return columns


def parse_size(text: str) -> float | None:
    cleaned = text.replace(",", "").strip()
    if not cleaned or cleaned in {"—", "-", "N/A"}:
        return None
    multiplier = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}.get(cleaned[-1], 1)
    if multiplier != 1:
        cleaned = cleaned[:-1]
    try:
        return float(cleaned) * multiplier
    except ValueError:
        return None


def over_size_limit(row: dict) -> bool:
    shares = parse_size(row["Finviz Shares Float"])
    market_cap = parse_size(row["Finviz Market Cap"])
    return (shares is not None and shares >= MAX_FLOAT) or (
        market_cap is not None and market_cap > MAX_MARKET_CAP
    )


def sort_percent(text: str) -> float:
    try:
        return float(text.replace("%", "").replace("+", "").replace(",", ""))
    except ValueError:
        return float("-inf")


def print_table(rows: list[dict], columns: list[str]) -> None:
    widths = {
        column: max(len(column), *(len(str(row[column])) for row in rows))
        for column in columns
    }

    def format_row(row: dict) -> str:
        return "  ".join(str(row[column]).center(widths[column]) for column in columns)

    print("  ".join(column.center(widths[column]) for column in columns))
    print("  ".join("-" * widths[column] for column in columns))
    for row in rows:
        print(format_row(row))


def below_prior_high(row: dict) -> float | None:
    price = row.get("raw_price")
    prior_high = row.get("prior_high")
    if price is None or not prior_high:
        return None
    return (prior_high - price) / prior_high * 100


def day_phrase(change: float) -> str:
    if change >= 1:
        return f"after a {change:.0f}% up day"
    if change <= -1:
        return f"on a {abs(change):.0f}% down day"
    return "on a flat day"


def next_day_pick(rows: list[dict]) -> str:
    """Name the setup holding closest to yesterday's high.

    Quiet inside days that stayed near the prior high have been the screen's
    larger next-day moves. A green bounce near the bottom of a wide prior day
    has had less follow-through.
    """
    ranked = [row for row in rows if below_prior_high(row) is not None]
    if not ranked:
        return ""
    ranked.sort(key=lambda row: below_prior_high(row))
    winner = ranked[0]
    gap = below_prior_high(winner)
    text = (
        f"{winner['Symbol']} has the most potential for the next day. "
        f"It closed {gap:.0f}% below yesterday's high {day_phrase(winner['raw_day_change'])}."
    )
    if len(ranked) > 1:
        runner = ranked[1]
        text += (
            f" {runner['Symbol']} closed {below_prior_high(runner):.0f}% below yesterday's high "
            f"{day_phrase(runner['raw_day_change'])}."
        )
    text += " The larger next-day moves on this screen have been quiet inside days that held near the prior high."
    return text


def print_elapsed(started: float) -> None:
    elapsed = time.perf_counter() - started
    print()
    print(f"Completed in {elapsed:.2f} seconds")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Scan for a 30% gain over two or three days, 500k screen-day volume, and a top-5 volume day. "
            "The prior day's high must be at least 5% above its low."
        )
    )
    parser.add_argument(
        "--date",
        help="Run the screen as of this date (YYYY-MM-DD). A past date adds the next day's close and high versus this close.",
    )
    parser.add_argument(
        "--email",
        action="store_true",
        help="Send the combined after-close email at 4:16 PM Eastern. Used by the weekday job.",
    )
    return parser.parse_args()


def run_screen(as_of: date) -> int:
    started = time.perf_counter()
    api_key = load_api_key()
    log("Loading US-listed symbols...")
    universe = load_symbols()
    sessions = load_sessions(api_key, as_of)
    if len(sessions) < 4:
        raise RuntimeError("Need four trading sessions to measure a two-day or three-day move")
    screen_day = sessions[0][0]
    if screen_day < datetime.date.today():
        listed_then = load_listed_on(api_key, screen_day)
        added = len(listed_then - universe)
        universe |= listed_then
        log(f"Added {added} symbols listed on {screen_day.isoformat()} that are not in the current file")
    log(f"Universe has {len(universe)} symbols")
    next_session = load_next_session(api_key, sessions[0][0])
    matches = find_matches(universe, sessions, next_session)
    columns = table_columns(next_session is not None)
    if not matches:
        print("No stocks matched.")
        print_elapsed(started)
        return 0

    screen_day = sessions[0][0]
    two_day_dates = {sessions[0][0], sessions[1][0]}
    three_day_dates = two_day_dates | {sessions[2][0]}
    log(f"Checking 9-month volume ranks for {len(matches)} candidates...")
    kept: list[dict] = []
    with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as pool:
        profiles = {
            pool.submit(volume_profile, api_key, match["symbol"], screen_day, two_day_dates, three_day_dates): match
            for match in matches
        }
        for future in as_completed(profiles):
            match = profiles[future]
            try:
                average, two_day_top, three_day_top = future.result()
            except Exception:
                continue
            match["average_volume"] = average
            if match["two_day_hit"] and not two_day_top:
                match["two_day_hit"] = False
            if match["three_day_hit"] and not three_day_top:
                match["three_day_hit"] = False
            if match["two_day_hit"] or match["three_day_hit"]:
                kept.append(match)
    matches = kept
    if not matches:
        print("No stocks matched.")
        print_elapsed(started)
        return 0

    log(f"Fetching Finviz details for {len(matches)} matches...")
    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as pool:
        futures = [pool.submit(fetch_row, match) for match in matches]
        for future in as_completed(futures):
            rows.append(future.result())
    sized = [row for row in rows if not over_size_limit(row)]
    dropped = len(rows) - len(sized)
    if dropped:
        log(f"Dropped {dropped} names with 30M or more float, or over $1B market cap")
    rows = sized
    if not rows:
        print("No stocks matched.")
        print_elapsed(started)
        return 0
    if next_session is not None:
        rows.sort(key=lambda row: sort_percent(row["Next Day %"]), reverse=True)
    else:
        rows.sort(key=lambda row: sort_percent(row["% Change"]), reverse=True)
    print_table(rows, columns)
    print_elapsed(started)
    save_screen_results(rows)
    pick = next_day_pick(rows)
    if pick:
        log(f"NEXT_DAY_PICK {pick}")
    return 0


def email_log(message: str) -> None:
    EMAIL_LOG.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(EASTERN).isoformat(timespec="seconds")
    with EMAIL_LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"{stamp} {message}\n")


def load_env() -> dict[str, str]:
    values: dict[str, str] = {}
    if KEY_FILE.exists():
        for line in KEY_FILE.read_text(encoding="utf-8").splitlines():
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    for key in ("POLYGON_API_KEY", "GMAIL_APP_PASSWORD", "EMAIL_TO"):
        override = os.environ.get(key, "").strip()
        if override:
            values[key] = override
    return values


def already_handled(today: str) -> bool:
    return EMAIL_STAMP.exists() and EMAIL_STAMP.read_text(encoding="utf-8").strip() == today


def mark_handled(today: str) -> None:
    EMAIL_STAMP.parent.mkdir(parents=True, exist_ok=True)
    EMAIL_STAMP.write_text(today + "\n", encoding="utf-8")


def split_row(line: str, widths: list[int]) -> list[str]:
    cells: list[str] = []
    pos = 0
    for index, width in enumerate(widths):
        cells.append(line[pos : pos + width].strip())
        pos += width
        if index < len(widths) - 1:
            pos += 2
    return cells


def parse_table(body: str) -> tuple[list[str], list[list[str]], str]:
    elapsed = ""
    lines: list[str] = []
    for line in body.splitlines():
        if line.startswith("Completed in"):
            elapsed = line
            continue
        lines.append(line)
    while lines and not lines[0].strip():
        lines.pop(0)
    if len(lines) < 2 or "  " not in lines[1]:
        return [], [], elapsed
    widths = [len(part) for part in lines[1].split("  ")]
    columns = split_row(lines[0], widths)
    rows: list[list[str]] = []
    for line in lines[2:]:
        if not line.strip():
            rows.append([""] * len(columns))
            continue
        rows.append(split_row(line, widths))
    while rows and not any(rows[-1]):
        rows.pop()
    return columns, rows, elapsed


def column_letter(index: int) -> str:
    letters = ""
    number = index + 1
    while number:
        number, remainder = divmod(number - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def spreadsheet_bytes(columns: list[str], rows: list[list[str]], protect: bool = True) -> bytes:
    """Excel file with centered cells and a locked header row."""
    xml_rows: list[str] = []
    sheet_rows = [columns, *rows]
    for row_number, row in enumerate(sheet_rows, start=1):
        style = "1" if row_number == 1 else "0"
        cells: list[str] = []
        for index, value in enumerate(row):
            ref = f"{column_letter(index)}{row_number}"
            text = f"<is><t>{xml_escape(value)}</t></is>" if value else "<is><t></t></is>"
            cells.append(f'<c r="{ref}" t="inlineStr" s="{style}">{text}</c>')
        xml_rows.append(f'<row r="{row_number}">{"".join(cells)}</row>')
    widths = []
    for index, column in enumerate(columns):
        longest = len(column)
        for row in rows:
            if index < len(row):
                longest = max(longest, len(row[index]))
        width = max(longest, len(column)) * 1.3 + 4
        widths.append(
            f'<col min="{index + 1}" max="{index + 1}" width="{width:.2f}" customWidth="1"/>'
        )
    last_column = column_letter(max(len(columns) - 1, 0))
    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<cols>{''.join(widths)}</cols>"
        f"<sheetData>{''.join(xml_rows)}</sheetData>"
        + (
            '<sheetProtection sheet="1" objects="1" scenarios="1" selectLockedCells="1" selectUnlockedCells="1"/>'
            if protect
            else ""
        )
        + f'<autoFilter ref="A1:{last_column}{max(len(sheet_rows), 1)}"/>'
        "</worksheet>"
    )
    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="2"><fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill></fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="2">'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1" applyProtection="1">'
        '<alignment horizontal="center" vertical="center"/>'
        '<protection locked="0"/></xf>'
        '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1" applyAlignment="1" applyProtection="1">'
        '<alignment horizontal="center" vertical="center"/>'
        '<protection locked="1"/></xf>'
        "</cellXfs></styleSheet>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        "</Types>"
    )
    package_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Screen" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        "</Relationships>"
    )
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as workbook_zip:
        workbook_zip.writestr("[Content_Types].xml", content_types)
        workbook_zip.writestr("_rels/.rels", package_rels)
        workbook_zip.writestr("xl/workbook.xml", workbook)
        workbook_zip.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        workbook_zip.writestr("xl/styles.xml", styles)
        workbook_zip.writestr("xl/worksheets/sheet1.xml", sheet)
    return buffer.getvalue()


def column_index(letters: str) -> int:
    index = 0
    for char in letters:
        index = index * 26 + ord(char.upper()) - 64
    return index - 1


def read_result_rows(path: Path) -> list[list[str]]:
    """Read stored rows, aligned to RESULTS_COLUMNS. Missing columns stay blank."""
    namespace = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with ZipFile(path) as workbook:
        sheet = ET.fromstring(workbook.read("xl/worksheets/sheet1.xml"))
        shared: list[str] = []
        if "xl/sharedStrings.xml" in workbook.namelist():
            strings = ET.fromstring(workbook.read("xl/sharedStrings.xml"))
            for item in strings.findall("m:si", namespace):
                shared.append("".join(node.text or "" for node in item.findall(".//m:t", namespace)))
    parsed: list[list[str]] = []
    for row in sheet.findall("m:sheetData/m:row", namespace):
        cells: dict[int, str] = {}
        for cell in row.findall("m:c", namespace):
            ref = cell.get("r") or ""
            letters = "".join(char for char in ref if char.isalpha())
            if not letters:
                continue
            kind = cell.get("t")
            if kind == "inlineStr":
                value = "".join(node.text or "" for node in cell.findall(".//m:t", namespace))
            elif kind == "s":
                node = cell.find("m:v", namespace)
                value = shared[int(node.text)] if node is not None and node.text else ""
            else:
                node = cell.find("m:v", namespace)
                value = node.text if node is not None and node.text else ""
            cells[column_index(letters)] = value
        if not cells:
            continue
        width = max(cells) + 1
        parsed.append([cells.get(index, "") for index in range(width)])
    if not parsed:
        return []
    headers = parsed[0]
    if "Date" not in headers or "Symbol" not in headers:
        raise ValueError(f"{path.name} is missing Date or Symbol columns")
    stored: list[list[str]] = []
    for row in parsed[1:]:
        by_name = {headers[index]: value for index, value in enumerate(row) if index < len(headers)}
        if not by_name.get("Date") or not by_name.get("Symbol"):
            continue
        stored.append([by_name.get(column, "—") or "—" for column in RESULTS_COLUMNS])
    return stored


def result_key(row: list[str]) -> tuple[str, str]:
    return row[0], row[1].upper()


def save_screen_results(rows: list[dict], path: Path = RESULTS_PATH) -> None:
    """Add this run to the results spreadsheet. The same date and symbol is kept once."""
    try:
        stored = read_result_rows(path) if path.exists() else []
    except (OSError, ValueError, ET.ParseError, KeyError) as error:
        log(f"Could not read {path.name}: {error}")
        return
    existing = {result_key(row): index for index, row in enumerate(stored)}
    added = 0
    updated = 0
    for row in rows:
        cells = [str(row.get(column, "—") or "—") for column in RESULTS_COLUMNS]
        key = result_key(cells)
        index = existing.get(key)
        if index is None:
            existing[key] = len(stored)
            stored.append(cells)
            added += 1
            continue
        current = stored[index]
        for column in ("Next Day %", "Max Next Day"):
            position = RESULTS_COLUMNS.index(column)
            if current[position] in BLANK_CELL and cells[position] not in BLANK_CELL:
                current[position] = cells[position]
                updated += 1
    if added == 0 and updated == 0:
        log(f"No new rows. {len(rows)} already in {path}")
        return
    stored.sort(key=result_key)
    temporary = path.with_suffix(".xlsx.tmp")
    try:
        temporary.write_bytes(spreadsheet_bytes(RESULTS_COLUMNS, stored, protect=False))
        temporary.replace(path)
    except OSError as error:
        log(f"Could not update {path.name}: {error}")
        return
    finally:
        if temporary.exists():
            temporary.unlink()
    if added:
        log(f"Saved {added} new row(s) to {path} ({len(stored)} total)")
    if updated:
        log(f"Filled in next-day results for {updated} cell(s) already in {path}")


def html_table(columns: list[str], rows: list[list[str]], elapsed: str, note: str = "") -> str:
    header = "".join(
        "<th style=\"border:1px solid #d0d0d0;padding:8px 12px;background:#f3f3f3;"
        "text-align:center;white-space:nowrap;\">"
        f"{escape(column)}</th>"
        for column in columns
    )
    body_rows: list[str] = []
    for row in rows:
        if not any(row):
            body_rows.append(
                f'<tr><td colspan="{len(columns)}" style="height:12px;border:none;"></td></tr>'
            )
            continue
        cells = "".join(
            "<td style=\"border:1px solid #d0d0d0;padding:8px 12px;text-align:center;white-space:nowrap;\">"
            f"{escape(value) or '&nbsp;'}</td>"
            for value in row
        )
        body_rows.append(f"<tr>{cells}</tr>")
    footer = ""
    if elapsed:
        footer += f"<p style=\"font-family:Arial,sans-serif;\">{escape(elapsed)}</p>"
    if note:
        footer += f"<p style=\"font-family:Arial,sans-serif;\">{escape(note)}</p>"
    return (
        "<table style=\"border-collapse:collapse;font-family:Arial,sans-serif;font-size:14px;\">"
        f"<thead><tr>{header}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>{footer}"
    )


def send_email(screen_date: str, body: str, password: str, note: str = "") -> None:
    recipient = load_env().get("EMAIL_TO", "").strip()
    if not recipient:
        raise RuntimeError("EMAIL_TO is not set")
    columns, rows, elapsed = parse_table(body)
    if note:
        body = f"{body}\n\n{note}"
    message = EmailMessage()
    message["From"] = recipient
    message["To"] = recipient
    message["Subject"] = f"Stock screen {screen_date}"
    message.set_content(body)
    if columns:
        message.add_alternative(html_table(columns, rows, elapsed, note), subtype="html")
        message.add_attachment(
            spreadsheet_bytes(columns, rows),
            maintype="application",
            subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename=f"stock-screen-{screen_date}.xlsx",
        )
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(recipient, password)
        smtp.send_message(message)


def send_scheduled_email() -> int:
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "daily-email.py"
    spec = importlib.util.spec_from_file_location("daily_email", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.send_scheduled_email()


def main() -> int:
    args = parse_args()
    if args.email:
        try:
            return send_scheduled_email()
        except Exception as error:
            email_log(f"Email run failed: {error}")
            raise
    as_of = date.fromisoformat(args.date) if args.date else date.today()
    return run_screen(as_of)


if __name__ == "__main__":
    raise SystemExit(main())
