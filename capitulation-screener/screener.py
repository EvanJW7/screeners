#!/usr/bin/env python3
"""Capitulation screen: a sharp week down into a thin volume-profile zone.

First pass: over the last 20 sessions, average daily volume at least 5
million shares, and over the last year, average daily range at least 5%.
Range is that day's high-to-low as a percent of that day's close.

Then: the latest close is at least 20% below the close five sessions ago.
Leveraged ETFs are left out. Those names are checked against a volume profile built from the prior two
years, excluding that same week, so the profile is volume traded before the
decline. Each day's shares are spread evenly from that day's low to high.
A blue zone is a price area inside that range whose bars are shorter than
20% of the average bar. The latest close has to sit in one, or within
four bars of one, on the two-year profile or the three-year profile.
It also has to be more than 15% above the lowest price of the last two
years, and the market cap has to be above $2 billion.
"""

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

MIN_AVERAGE_VOLUME = 5_000_000
MIN_AVERAGE_RANGE = 5.0
RANGE_YEAR = timedelta(days=365)
RANGE_CACHE = Path(__file__).resolve().parent / "range-cache.json"
RANGE_WORKERS = 8
MIN_WEEK_DECLINE = 20.0
MIN_VOLUME_SESSIONS = 10
SESSION_GOAL = 20
WEEK_SESSIONS = 5
THIN_OF_AVERAGE = 0.20
NEAR_THIN_BARS = 4
ABOVE_TWO_YEAR_LOW = 0.15
MIN_MARKET_CAP = 2_000_000_000
BIN_COUNT = 100
PROFILE_YEARS = 2
PROFILE_YEARS_LONG = 3
KEY_FILE = Path.home() / ".config" / "two-day-screener" / "env"
EASTERN = ZoneInfo("America/New_York")
GROUPED_URL = "https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/{day}"
DAILY_URL = "https://api.polygon.io/v2/aggs/ticker/{symbol}/range/1/day/{start}/{end}"
TICKER_URL = "https://api.polygon.io/v3/reference/tickers/{symbol}"
NASDAQ_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
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
    "Week",
    "Avg Volume",
    "Avg Range",
    "Blue Zone",
    "Vs Average",
    "Timeframe",
]
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


def is_leveraged_etf(name: str) -> bool:
    """True for funds with more than 1x exposure. Inverse 1x funds stay in."""
    for match in re.finditer(r"(?<![A-Za-z])([-+]?\d+(?:\.\d+)?)x(?![A-Za-z])", name, re.I):
        if abs(float(match.group(1))) > 1:
            return True
    if re.search(r"leveraged", name, re.I):
        return True
    if re.search(r"ultra[\s-]?small", name, re.I):
        return False
    if re.search(r"ultra[\s-]?short", name, re.I):
        duration = r"income|bond|fixed|municipal|duration|government|investment|treasury|grade"
        return re.search(duration, name, re.I) is None
    return re.search(r"(?<![A-Za-z])ultra", name, re.I) is not None


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
            if parts[test_index] == "Y":
                continue
            if "warrant" in parts[name_index].lower():
                continue
            if parts[etf_index] == "Y" and is_leveraged_etf(parts[name_index]):
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
        symbol = normalize_symbol(str(row.get("T") or ""))
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
    """Recent sessions, newest first. Enough for a 20-day average and a 5-day decline."""
    sessions: list[tuple[date, dict[str, Bar]]] = []
    cursor = as_of
    looked = 0
    needed = SESSION_GOAL + WEEK_SESSIONS
    while looked < needed + 15 and len(sessions) < needed:
        if cursor.weekday() < 5:
            looked += 1
            rows = fetch_grouped(api_key, cursor)
            if rows:
                sessions.append((cursor, parse_bars(rows)))
                log(f"{cursor.isoformat()}: {len(sessions[-1][1])} symbols")
            else:
                log(f"{cursor.isoformat()}: no bars")
        cursor -= timedelta(days=1)
    if len(sessions) <= WEEK_SESSIONS:
        raise RuntimeError("Need more than five sessions to measure a one-week decline")
    return sessions


def average_volumes(sessions: list[tuple[date, dict[str, Bar]]]) -> dict[str, float]:
    sample = sessions[:SESSION_GOAL]
    totals: dict[str, float] = {}
    counts: dict[str, int] = {}
    for _, bars in sample:
        for symbol, (_, volume, _, _, _) in bars.items():
            totals[symbol] = totals.get(symbol, 0.0) + volume
            counts[symbol] = counts.get(symbol, 0) + 1
    return {
        symbol: totals[symbol] / counts[symbol]
        for symbol in totals
        if counts[symbol] >= MIN_VOLUME_SESSIONS
    }


def range_percent(close: float, high: float, low: float) -> float | None:
    if close <= 0 or high < low:
        return None
    return (high - low) / close * 100


def load_range_cache() -> dict[str, dict]:
    try:
        data = json.loads(RANGE_CACHE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_range_cache(cache: dict[str, dict]) -> None:
    temporary = RANGE_CACHE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(cache), encoding="utf-8")
    os.replace(temporary, RANGE_CACHE)


def fetch_range_rows(api_key: str, symbol: str, start: date, end: date) -> list[tuple[date, float]]:
    """Daily ranges from the last year, as a percent of that day's close."""
    for attempt in range(4):
        response = requests.get(
            DAILY_URL.format(symbol=symbol, start=start.isoformat(), end=end.isoformat()),
            params={"adjusted": "true", "sort": "asc", "limit": 50000, "apiKey": api_key},
            timeout=60,
        )
        if response.status_code == 429:
            time.sleep(15 * (attempt + 1))
            continue
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code != 200:
            message = str(body.get("message") or body.get("error") or response.status_code)
            raise RuntimeError(f"{symbol}: {message}")
        rows: list[tuple[date, float]] = []
        for row in body.get("results") or []:
            close, high, low, stamp = row.get("c"), row.get("h"), row.get("l"), row.get("t")
            if None in (close, high, low, stamp):
                continue
            percent = range_percent(float(close), float(high), float(low))
            if percent is None:
                continue
            session = datetime.fromtimestamp(stamp / 1000, timezone.utc).date()
            rows.append((session, percent))
        return rows
    raise RuntimeError(f"Polygon rate limit while fetching {symbol}")


def cache_covers(entry: dict, window_start: date, oldest_loaded: date) -> bool:
    if not entry.get("complete"):
        return False
    queried = entry.get("queried_start")
    if not isinstance(queried, str) or date.fromisoformat(queried) > window_start:
        return False
    rows = entry.get("rows")
    if not isinstance(rows, list) or not rows:
        return True
    newest = max(date.fromisoformat(row[0]) for row in rows)
    return newest >= oldest_loaded - timedelta(days=4)


def mean_range(values: dict[date, float]) -> float | None:
    if len(values) < MIN_VOLUME_SESSIONS:
        return None
    return sum(values.values()) / len(values)


def year_average_ranges(
    api_key: str,
    symbols: list[str],
    sessions: list[tuple[date, dict[str, Bar]]],
) -> dict[str, float]:
    """Mean daily range over the last year for names that already passed the volume filter."""
    if not symbols or not sessions:
        return {}
    screen_day = sessions[0][0]
    window_start = screen_day - RANGE_YEAR
    oldest_loaded = sessions[-1][0]
    live: dict[str, dict[date, float]] = {symbol: {} for symbol in symbols}
    for day, bars in sessions:
        if day < window_start:
            continue
        for symbol in symbols:
            bar = bars.get(symbol)
            if bar is None:
                continue
            percent = range_percent(bar[0], bar[2], bar[3])
            if percent is not None:
                live[symbol][day] = percent
    cache = load_range_cache()
    wanted = set(symbols)
    needed = []
    for symbol in symbols:
        entry = cache.get(symbol)
        if not isinstance(entry, dict) or not cache_covers(entry, window_start, oldest_loaded):
            needed.append(symbol)
    fetched: dict[str, list[tuple[date, float]] | None] = {}
    if needed:
        log(f"Fetching one-year ranges for {len(needed)} names...")
        done = 0
        with ThreadPoolExecutor(max_workers=RANGE_WORKERS) as pool:
            jobs = {
                pool.submit(fetch_range_rows, api_key, symbol, window_start, screen_day): symbol
                for symbol in needed
            }
            for future in as_completed(jobs):
                symbol = jobs[future]
                done += 1
                if done % 100 == 0 or done == len(needed):
                    log(f"One-year ranges {done}/{len(needed)}")
                try:
                    fetched[symbol] = future.result()
                except Exception as error:
                    log(f"{symbol}: range history failed ({error})")
                    fetched[symbol] = None
    ranges: dict[str, float] = {}
    cutoff = (window_start - timedelta(days=10)).isoformat()
    for symbol in symbols:
        if symbol in fetched and fetched[symbol] is None:
            continue
        entry = cache.get(symbol)
        if not isinstance(entry, dict):
            entry = {"complete": False, "queried_start": None, "rows": []}
        if symbol in fetched:
            entry = {
                "complete": True,
                "queried_start": window_start.isoformat(),
                "rows": [[day.isoformat(), percent] for day, percent in fetched[symbol]],
            }
        if not entry.get("complete"):
            continue
        values: dict[date, float] = {}
        for raw_day, percent in entry.get("rows") or []:
            day = date.fromisoformat(raw_day)
            if window_start <= day <= screen_day:
                values[day] = float(percent)
        values.update(live[symbol])
        stored = {raw_day: float(percent) for raw_day, percent in entry.get("rows") or []}
        for day, percent in live[symbol].items():
            stored[day.isoformat()] = percent
        entry["rows"] = [[day, percent] for day, percent in sorted(stored.items()) if day >= cutoff]
        cache[symbol] = entry
        average = mean_range(values)
        if average is not None:
            ranges[symbol] = average
    for symbol in list(cache):
        if symbol not in wanted and isinstance(cache[symbol], dict):
            rows = cache[symbol].get("rows") or []
            if rows and max(row[0] for row in rows) < cutoff:
                cache.pop(symbol, None)
    save_range_cache(cache)
    return ranges


def fetch_daily(api_key: str, symbol: str, start: date, end: date) -> list[Bar]:
    for attempt in range(4):
        response = requests.get(
            DAILY_URL.format(symbol=symbol, start=start.isoformat(), end=end.isoformat()),
            params={"adjusted": "true", "sort": "asc", "limit": 50000, "apiKey": api_key},
            timeout=60,
        )
        if response.status_code == 429:
            time.sleep(15 * (attempt + 1))
            continue
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code != 200:
            message = str(body.get("message") or body.get("error") or response.status_code)
            raise RuntimeError(f"{symbol}: {message}")
        bars: list[Bar] = []
        for row in body.get("results") or []:
            close, volume, high, low, open_price = (
                row.get("c"),
                row.get("v"),
                row.get("h"),
                row.get("l"),
                row.get("o"),
            )
            if None in (close, volume, high, low, open_price):
                continue
            bars.append((float(close), float(volume), float(high), float(low), float(open_price)))
        return bars
    raise RuntimeError(f"Polygon rate limit while fetching {symbol}")


def fetch_dated_daily(api_key: str, symbol: str, start: date, end: date) -> list[tuple[date, Bar]]:
    """Adjusted daily bars with session dates, oldest first."""
    for attempt in range(4):
        response = requests.get(
            DAILY_URL.format(symbol=symbol, start=start.isoformat(), end=end.isoformat()),
            params={"adjusted": "true", "sort": "asc", "limit": 50000, "apiKey": api_key},
            timeout=60,
        )
        if response.status_code == 429:
            time.sleep(15 * (attempt + 1))
            continue
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code != 200:
            message = str(body.get("message") or body.get("error") or response.status_code)
            raise RuntimeError(f"{symbol}: {message}")
        bars: list[tuple[date, Bar]] = []
        for row in body.get("results") or []:
            close, volume, high, low, open_price, stamp = (
                row.get("c"),
                row.get("v"),
                row.get("h"),
                row.get("l"),
                row.get("o"),
                row.get("t"),
            )
            if None in (close, volume, high, low, open_price, stamp):
                continue
            session = datetime.fromtimestamp(stamp / 1000, timezone.utc).date()
            bars.append((
                session,
                (float(close), float(volume), float(high), float(low), float(open_price)),
            ))
        return bars
    raise RuntimeError(f"Polygon rate limit while fetching {symbol}")


def series_holds(bars: list[Bar]) -> bool:
    """False when one adjusted close is several times the prior close."""
    previous = None
    for close, _, _, _, _ in bars:
        if previous and previous > 0 and close / previous > 4:
            return False
        previous = close
    return True


def blue_zone(bars: list[Bar], price: float) -> tuple[str, float] | None:
    """Zone label and the close bar versus the average.

    The close passes when its own bar is under 20% of the average, or when a
    bar that thin sits within four bars of it. The percent is always the
    close's own bar.
    """
    if len(bars) < MIN_VOLUME_SESSIONS or not series_holds(bars):
        return None
    low = min(bar[3] for bar in bars)
    high = max(bar[2] for bar in bars)
    if low <= 0 or high < low:
        return None
    if high == low:
        return None
    width = (high - low) / BIN_COUNT
    volumes = [0.0] * BIN_COUNT
    for _, volume, bar_high, bar_low, _ in bars:
        if volume <= 0 or bar_high < bar_low:
            continue
        if bar_high == bar_low:
            index = min(BIN_COUNT - 1, max(0, int((bar_low - low) / width)))
            volumes[index] += volume
            continue
        start = min(BIN_COUNT - 1, max(0, int((bar_low - low) / width)))
        stop = min(BIN_COUNT - 1, max(0, int((bar_high - low) / width)))
        span = bar_high - bar_low
        for index in range(start, stop + 1):
            overlap = min(bar_high, low + (index + 1) * width) - max(bar_low, low + index * width)
            if overlap > 0:
                volumes[index] += volume * overlap / span
    average = sum(volumes) / len(volumes)
    if average <= 0:
        return None
    cutoff = average * THIN_OF_AVERAGE
    if price < low or price > high:
        return None
    index = min(BIN_COUNT - 1, max(0, int((price - low) / width)))
    nearby = range(max(0, index - NEAR_THIN_BARS), min(BIN_COUNT, index + NEAR_THIN_BARS + 1))
    thin = [bar for bar in nearby if volumes[bar] < cutoff]
    if not thin:
        return None
    anchor = min(thin, key=lambda bar: (abs(bar - index), bar))
    start = anchor
    while start > 0 and volumes[start - 1] < cutoff:
        start -= 1
    stop = anchor
    while stop + 1 < BIN_COUNT and volumes[stop + 1] < cutoff:
        stop += 1
    zone_low = low + start * width
    zone_high = low + (stop + 1) * width
    return f"${zone_low:.2f}–${zone_high:.2f}", volumes[index] / average * 100


def first_pass(
    sessions: list[tuple[date, dict[str, Bar]]],
    universe: set[str],
    ranges: dict[str, float],
    volumes: dict[str, float] | None = None,
) -> tuple[list[tuple[str, float, float, float, float]], int]:
    """Names with the volume, year range, and one-week decline, plus the liquid count."""
    if volumes is None:
        volumes = average_volumes(sessions)
    latest = sessions[0][1]
    base = sessions[WEEK_SESSIONS][1]
    liquid = 0
    rows = []
    for symbol in universe:
        average_volume = volumes.get(symbol)
        average_range = ranges.get(symbol)
        if average_volume is None or average_volume < MIN_AVERAGE_VOLUME:
            continue
        if average_range is None or average_range < MIN_AVERAGE_RANGE:
            continue
        liquid += 1
        current = latest.get(symbol)
        earlier = base.get(symbol)
        if current is None or earlier is None:
            continue
        price = current[0]
        base_close = earlier[0]
        if price <= 0 or base_close <= 0:
            continue
        change = (price - base_close) / base_close * 100
        if change > -MIN_WEEK_DECLINE:
            continue
        rows.append((symbol, price, change, average_volume, average_range, current[1]))
    rows.sort(key=lambda row: row[2])
    return rows, liquid


def market_cap(api_key: str, symbol: str, price: float) -> float | None:
    """Latest Polygon market cap, or price times shares outstanding when cap is blank."""
    for attempt in range(4):
        response = requests.get(
            TICKER_URL.format(symbol=symbol),
            params={"apiKey": api_key},
            timeout=30,
        )
        if response.status_code == 429:
            time.sleep(15 * (attempt + 1))
            continue
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            message = str(body.get("message") or body.get("error") or response.status_code)
            raise RuntimeError(f"{symbol}: {message}")
        result = body.get("results") or {}
        cap = result.get("market_cap")
        if cap is not None:
            return float(cap)
        shares = result.get("weighted_shares_outstanding") or result.get("share_class_shares_outstanding")
        if shares and price > 0:
            return float(shares) * price
        return None
    raise RuntimeError(f"Polygon rate limit while fetching {symbol} market cap")


def find_matches(
    api_key: str,
    sessions: list[tuple[date, dict[str, Bar]]],
    candidates: list[tuple[str, float, float, float, float]],
    adjust_bars=None,
) -> list[tuple]:
    profile_end = sessions[WEEK_SESSIONS][0]
    start_two = profile_end - timedelta(days=365 * PROFILE_YEARS)
    start_three = profile_end - timedelta(days=365 * PROFILE_YEARS_LONG)
    matches = []
    for symbol, price, change, average_volume, average_range, session_volume in candidates:
        cap = market_cap(api_key, symbol, price)
        if cap is None or cap <= MIN_MARKET_CAP:
            shown = "unavailable" if cap is None else f"${cap / 1_000_000_000:.2f}B"
            log(f"{symbol}: down {change:.1f}%, market cap {shown}")
            continue
        dated = fetch_dated_daily(api_key, symbol, start_three, profile_end)
        today_low = None
        if adjust_bars is not None:
            dated, today_low = adjust_bars(symbol, dated)
        bars_two = [bar for day, bar in dated if day >= start_two]
        bars_three = [bar for _day, bar in dated]
        zone_two = blue_zone(bars_two, price) if bars_two else None
        zone_three = blue_zone(bars_three, price) if bars_three else None
        if zone_two is None and zone_three is None:
            log(f"{symbol}: down {change:.1f}%, not in a blue zone on either profile")
            continue
        if not bars_two:
            log(f"{symbol}: down {change:.1f}%, no two-year profile")
            continue
        two_year_low = min(bar[3] for bar in bars_two)
        latest = sessions[0][1].get(symbol)
        if latest is not None:
            two_year_low = min(two_year_low, latest[3])
        if today_low is not None:
            two_year_low = min(two_year_low, today_low)
        if price <= two_year_low * (1 + ABOVE_TWO_YEAR_LOW):
            log(f"{symbol}: down {change:.1f}%, within 15% of the two-year low ${two_year_low:.2f}")
            continue
        if zone_two and zone_three:
            timeframe = "Both"
        elif zone_two:
            timeframe = "2 year"
        else:
            timeframe = "3 year"
        above_low = (price / two_year_low - 1) * 100
        prior = sessions[1][1].get(symbol)
        day_change = (price / prior[0] - 1) * 100 if prior is not None and prior[0] > 0 else None
        log(f"{symbol}: {timeframe}")
        matches.append((
            symbol, price, change, average_volume, average_range,
            timeframe,
            None if zone_two is None else zone_two[0],
            None if zone_two is None else zone_two[1],
            None if zone_three is None else zone_three[0],
            None if zone_three is None else zone_three[1],
            above_low, session_volume, cap, day_change,
        ))
    return matches


def format_volume(volume: float) -> str:
    if volume >= 1_000_000:
        return f"{volume / 1_000_000:.1f}M"
    return f"{volume:,.0f}"


def print_table(screen_day: date, matches: list[tuple]) -> None:
    rendered = []
    for symbol, price, change, average_volume, average_range, timeframe, zone_two, versus_two, _zone_three, _versus_three, *_rest in matches:
        rendered.append([
            screen_day.isoformat(),
            symbol,
            f"{price:.2f}",
            f"{change:.1f}%",
            format_volume(average_volume),
            f"{average_range:.1f}%",
            zone_two or "—",
            "—" if versus_two is None else f"{versus_two:.1f}%",
            timeframe,
        ])
    widths = [
        max(len(column), *(len(row[index]) for row in rendered))
        for index, column in enumerate(COLUMNS)
    ]
    print("  ".join(column.ljust(widths[index]) for index, column in enumerate(COLUMNS)))
    print("  ".join("-" * width for width in widths))
    for row in rendered:
        print("  ".join(row[index].ljust(widths[index]) for index in range(len(COLUMNS))))


EXTENDED_HL_CACHE = Path(__file__).resolve().parent / "extended-hl-cache.json"
EXTENDED_START_MINUTE = 4 * 60
EXTENDED_END_MINUTE = 20 * 60


def load_extended_hl_cache() -> dict:
    try:
        data = json.loads(EXTENDED_HL_CACHE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_extended_hl_cache(cache: dict) -> None:
    temporary = EXTENDED_HL_CACHE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(cache), encoding="utf-8")
    os.replace(temporary, EXTENDED_HL_CACHE)


def minute_spans(api_key: str, symbol: str, start: date, end: date) -> dict[str, list[float]]:
    """4:00 a.m. to 8:00 p.m. Eastern high and low, keyed by session date."""
    origin = datetime(start.year, start.month, start.day, 4, 0, tzinfo=EASTERN)
    finish = datetime(end.year, end.month, end.day, 20, 0, tzinfo=EASTERN)
    spans: dict[str, list[float]] = {}
    url = (
        f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/1/minute/"
        f"{int(origin.timestamp() * 1000)}/{int(finish.timestamp() * 1000)}"
    )
    params = {"adjusted": "true", "sort": "asc", "limit": 50000, "apiKey": api_key}
    while url:
        body = None
        for attempt in range(5):
            response = requests.get(url, params=params, timeout=90)
            if response.status_code == 429:
                time.sleep(10 * (attempt + 1))
                continue
            try:
                body = response.json()
            except ValueError:
                body = {}
            if response.status_code != 200:
                message = str(body.get("message") or body.get("error") or response.status_code)
                raise RuntimeError(f"{symbol}: {message}")
            break
        else:
            raise RuntimeError(f"{symbol}: rate limit while fetching extended hours")
        for row in (body or {}).get("results") or []:
            stamp, high, low = row.get("t"), row.get("h"), row.get("l")
            if None in (stamp, high, low):
                continue
            when = datetime.fromtimestamp(stamp / 1000, EASTERN)
            minute = when.hour * 60 + when.minute
            if when.weekday() >= 5 or not (EXTENDED_START_MINUTE <= minute < EXTENDED_END_MINUTE):
                continue
            key = when.date().isoformat()
            high = float(high)
            low = float(low)
            prior = spans.get(key)
            if prior is None:
                spans[key] = [high, low]
            else:
                prior[0] = max(prior[0], high)
                prior[1] = min(prior[1], low)
        url = (body or {}).get("next_url")
        params = None
        if url and "apiKey=" not in url:
            url = f"{url}&apiKey={api_key}"
    return spans


class ExtendedHours:
    """Cached 4:00 a.m. to 8:00 p.m. highs and lows. Today's session is always read live."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.cache = load_extended_hl_cache()
        self.dirty = False

    def save(self) -> None:
        if self.dirty:
            save_extended_hl_cache(self.cache)
            self.dirty = False

    def spans(
        self,
        symbol: str,
        start: date,
        end: date,
        live_day: date | None = None,
    ) -> dict[date, tuple[float, float]]:
        entry = self.cache.get(symbol)
        if not isinstance(entry, dict):
            entry = {"queried_start": None, "queried_end": None, "days": {}}
        days = entry.get("days")
        if not isinstance(days, dict):
            days = {}
        stable_end = end if live_day is None or end < live_day else live_day - timedelta(days=1)
        queried_start = entry.get("queried_start")
        queried_end = entry.get("queried_end")
        q0 = date.fromisoformat(queried_start) if isinstance(queried_start, str) else None
        q1 = date.fromisoformat(queried_end) if isinstance(queried_end, str) else None
        if stable_end >= start and not (q0 is not None and q1 is not None and q0 <= start and q1 >= stable_end):
            if q0 is None or q1 is None or (q0 > start and q1 < stable_end):
                pieces = [(start, stable_end)]
            else:
                pieces = []
                if q0 > start:
                    pieces.append((start, min(stable_end, q0 - timedelta(days=1))))
                if q1 < stable_end:
                    pieces.append((max(start, q1 + timedelta(days=1)), stable_end))
            fetched = 0
            for piece_start, piece_end in pieces:
                if piece_start > piece_end:
                    continue
                found = minute_spans(self.api_key, symbol, piece_start, piece_end)
                days.update(found)
                fetched += len(found)
            cutoff = (start - timedelta(days=10)).isoformat()
            days = {day: pair for day, pair in days.items() if day >= cutoff}
            entry = {
                "queried_start": start.isoformat() if q0 is None else min(q0, start).isoformat(),
                "queried_end": stable_end.isoformat() if q1 is None else max(q1, stable_end).isoformat(),
                "days": days,
            }
            self.cache[symbol] = entry
            self.dirty = True
            log(f"{symbol}: extended hours through {entry['queried_end']}, {fetched} new sessions")
        result: dict[date, tuple[float, float]] = {}
        for raw_day, pair in days.items():
            day = date.fromisoformat(raw_day)
            if start <= day <= end and isinstance(pair, list) and len(pair) == 2:
                result[day] = (float(pair[0]), float(pair[1]))
        if live_day is not None and start <= live_day <= end:
            live = minute_spans(self.api_key, symbol, live_day, live_day)
            pair = live.get(live_day.isoformat())
            if pair:
                result[live_day] = (float(pair[0]), float(pair[1]))
        return result


def with_live_quotes(
    sessions: list[tuple[date, dict[str, Bar]]],
    quotes: dict[str, dict],
    as_of: date,
) -> tuple[list[tuple[date, dict[str, Bar]]], list[tuple[date, dict[str, Bar]]]]:
    """Use the live price for the week test. The volume average stays on completed sessions."""
    def paint(bars: dict[str, Bar]) -> dict[str, Bar]:
        painted = dict(bars)
        for symbol, quote in quotes.items():
            if quote.get("day") != as_of or quote.get("price", 0) <= 0:
                continue
            current = painted.get(symbol)
            if current is None:
                continue
            _close, volume, high, low, open_price = current
            traded = quote.get("volume") or 0
            if traded > 0:
                volume = traded
            painted[symbol] = (float(quote["price"]), float(volume), high, low, open_price)
        return painted

    if sessions[0][0] == as_of:
        updated = [(as_of, paint(sessions[0][1]))] + sessions[1:]
        return updated, updated
    return [(as_of, paint(sessions[0][1]))] + sessions, sessions


def run_screen(
    as_of: date,
    quotes: dict[str, dict] | None = None,
    adjust_bars=None,
) -> tuple[date, date, list[tuple], int, int]:
    api_key = load_api_key()
    universe = load_symbols()
    sessions = load_sessions(api_key, as_of)
    volume_sessions = sessions
    if quotes:
        sessions, volume_sessions = with_live_quotes(sessions, quotes, as_of)
    screen_day = sessions[0][0]
    base_day = sessions[WEEK_SESSIONS][0]
    volumes = average_volumes(volume_sessions)
    volume_names = [
        symbol for symbol in universe if volumes.get(symbol, 0) >= MIN_AVERAGE_VOLUME
    ]
    ranges = year_average_ranges(api_key, volume_names, volume_sessions)
    candidates, liquid = first_pass(sessions, universe, ranges, volumes)
    log(
        f"{screen_day.isoformat()} vs {base_day.isoformat()}: "
        f"{liquid} names average 5M shares and a 5% range over the last year, "
        f"{len(candidates)} are down 20% on the week"
    )
    matches = find_matches(api_key, sessions, candidates, adjust_bars)
    return screen_day, base_day, matches, liquid, len(candidates)


def main() -> None:
    parser = argparse.ArgumentParser(description="Capitulation screen into a thin volume-profile zone.")
    parser.add_argument("--date", help="Session to screen, YYYY-MM-DD. Defaults to today.")
    args = parser.parse_args()
    if args.date:
        as_of = date.fromisoformat(args.date)
    else:
        as_of = datetime.now(EASTERN).date()
    screen_day, _base_day, matches, liquid, declined = run_screen(as_of)
    if not matches:
        print(
            f"No names on {screen_day.isoformat()} are down 20% over the last week "
            f"and trading in a thin volume zone above a $2 billion market cap. {liquid} names average at least 5M shares "
            f"and a 5% daily range over the last year; {declined} of those are down 20% on the week."
        )
        return
    print_table(screen_day, matches)


if __name__ == "__main__":
    main()
